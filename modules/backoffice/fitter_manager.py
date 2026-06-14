"""
Fitter Management Panel
========================
Three tabs:
  1. Fitters & Rate Chart  — add/edit fitters and their rates per fitting type
  2. Pending Payouts       — jobs done, payment outstanding; grouped by fitter
  3. Payment History       — record payments, view history

Called from backoffice settings / accounts tab.
"""

import streamlit as st
from datetime import date, datetime
from typing import List, Dict, Optional
import pandas as pd


# ── DB helpers ────────────────────────────────────────────────────────────────

def _q(sql: str, params: dict = None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB error: {e}")
        return []


def _w(sql: str, params: dict = None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        st.error(f"DB write error: {e}")
        return False


def _fmt(v) -> str:
    try:
        return f"₹{float(v):,.2f}"
    except Exception:
        return "₹0.00"


def _download_df(df: pd.DataFrame, label: str, filename: str, key: str):
    if df is None or df.empty:
        return
    st.download_button(
        label,
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name=filename,
        mime="text/csv",
        key=key,
        use_container_width=True,
    )


# ── Data fetchers ─────────────────────────────────────────────────────────────

def fetch_fitters(active_only: bool = False) -> List[Dict]:
    where = "WHERE is_active = TRUE" if active_only else ""
    return _q(f"SELECT id, fitter_name AS name, contact AS phone, address, is_active, notes, fitter_type, created_at FROM fitters {where} ORDER BY fitter_name")


def fetch_fitting_types() -> List[Dict]:
    return _q("SELECT * FROM fitting_types WHERE is_active=TRUE ORDER BY sort_order")


def fetch_rate_chart(fitter_id: str) -> List[Dict]:
    return _q("""
        SELECT frc.id, frc.fitting_type_code, ft.label AS fitting_type_label,
               frc.rate, frc.effective_from, frc.effective_to
        FROM fitter_rate_chart frc
        JOIN fitting_types ft ON ft.code = frc.fitting_type_code
        WHERE frc.fitter_id = %(fid)s::uuid
          AND (frc.effective_to IS NULL OR frc.effective_to >= CURRENT_DATE)
        ORDER BY ft.sort_order
    """, {"fid": fitter_id})


def fetch_pending_payouts() -> List[Dict]:
    """All registered provider assignments with unpaid balance, grouped by provider."""
    return _q("""
        WITH fa AS (
            SELECT DISTINCT ON (order_line_id, job_master_id)
                   *
            FROM fitting_assignments
            WHERE status IN ('PENDING', 'SENT', 'RECEIVED', 'DONE')
              AND payment_status IN ('UNPAID', 'PARTIALLY_PAID')
            ORDER BY order_line_id, job_master_id,
                     updated_at DESC NULLS LAST,
                     created_at DESC NULLS LAST
        )
        SELECT fa.id, fa.order_no, fa.eye_side, fa.fitting_type_code,
               COALESCE(st.service_name, ft.label, fa.fitting_type_code) AS fitting_type_label,
               fa.rate_applied, fa.sent_date, fa.received_date,
               fa.status, fa.payment_status, fa.paid_amount,
               fa.remarks,
               fi.id AS fitter_id, fi.fitter_name, fi.contact AS fitter_phone,
               o.order_type, o.party_name, p.product_name,
               CASE
                   WHEN COALESCE(ol.lens_params->>'assigned_provider_pair_qty','') ~ '^[0-9]+(\\.[0-9]+)?$'
                       THEN (ol.lens_params->>'assigned_provider_pair_qty')::numeric
                   WHEN COALESCE(ol.lens_params->>'service_qty_factor','') ~ '^[0-9]+(\\.[0-9]+)?$'
                       THEN (ol.lens_params->>'service_qty_factor')::numeric
                   WHEN UPPER(COALESCE(ol.eye_side,'')) IN ('R','L','RE','LE') THEN 0.5
                   ELSE COALESCE(ol.quantity, 1)::numeric
               END AS pair_qty,
               CASE
                   WHEN COALESCE(ol.lens_params->>'assigned_provider_pcs_qty','') ~ '^[0-9]+$'
                       THEN (ol.lens_params->>'assigned_provider_pcs_qty')::integer
                   WHEN COALESCE(ol.lens_params->>'service_qty_factor','') ~ '^[0-9]+(\\.[0-9]+)?$'
                       THEN ROUND((ol.lens_params->>'service_qty_factor')::numeric * 2)::integer
                   WHEN UPPER(COALESCE(ol.eye_side,'')) IN ('R','L','RE','LE') THEN 1
                   ELSE GREATEST(1, COALESCE(ol.quantity, 1) * 2)
               END AS pcs_qty,
               COALESCE(NULLIF(ol.lens_params->>'assigned_provider_pair_rate','')::numeric, fa.rate_applied) AS pair_rate,
               (fa.rate_applied - COALESCE(fa.paid_amount, 0)) AS balance_due
        FROM fa
        JOIN fitters fi ON fi.id = fa.fitter_id
        LEFT JOIN fitting_types ft ON ft.code = fa.fitting_type_code
        LEFT JOIN service_types st ON st.service_code = fa.fitting_type_code
        LEFT JOIN order_lines ol ON ol.id = fa.order_line_id
        LEFT JOIN products p ON p.id = ol.product_id
        LEFT JOIN orders o ON o.id = ol.order_id
        ORDER BY fi.fitter_name, fa.created_at DESC
    """)


def fetch_payment_history(fitter_id: Optional[str] = None) -> List[Dict]:
    where = "WHERE fp.fitter_id = %(fid)s::uuid" if fitter_id else ""
    params = {"fid": fitter_id} if fitter_id else {}
    return _q(f"""
        SELECT fp.*, fi.fitter_name
        FROM fitter_payments fp
        JOIN fitters fi ON fi.id = fp.fitter_id
        {where}
        ORDER BY fp.payment_date DESC, fp.created_at DESC
        LIMIT 200
    """, params)


def get_rate_for(fitter_id: str, fitting_type_code: str) -> float:
    rows = _q("""
        SELECT rate FROM fitter_rate_chart
        WHERE fitter_id = %(fid)s::uuid
          AND fitting_type_code = %(ftc)s
          AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
        ORDER BY effective_from DESC LIMIT 1
    """, {"fid": fitter_id, "ftc": fitting_type_code})
    return float(rows[0]["rate"]) if rows else 0.0


# ── Tab 1: Fitters & Rate Chart ───────────────────────────────────────────────

def _fetch_all_fitting_types() -> List[Dict]:
    """Fetch ALL fitting types including inactive — for management view."""
    return _q("SELECT * FROM fitting_types ORDER BY sort_order, label")


def _render_fitters_tab():
    st.markdown("#### 👷 Fitters & Rate Chart")

    # ── Section 1: Fitting Types ─────────────────────────────────────
    with st.expander("⚙️ Fitting Types — manage types & active/inactive",
                     expanded=False):
        all_types = _fetch_all_fitting_types()

        # List all types with active toggle
        for ft in all_types:
            ftc    = ft["code"]
            active = bool(ft.get("is_active", True))
            c1, c2, c3 = st.columns([3, 1, 1])
            c1.markdown(
                f"{'🟢' if active else '🔴'} **{ft['label']}**"
                f" <span style='color:#6b7280;font-size:0.75rem'>({ftc})</span>",
                unsafe_allow_html=True
            )
            with c2:
                if active:
                    if st.button("Deactivate", key=f"deact_ft_{ftc}",
                                 use_container_width=True):
                        _w("UPDATE fitting_types SET is_active=FALSE WHERE code=%(c)s",
                           {"c": ftc})
                        st.rerun()
                else:
                    if st.button("Activate", key=f"act_ft_{ftc}",
                                 use_container_width=True):
                        _w("UPDATE fitting_types SET is_active=TRUE WHERE code=%(c)s",
                           {"c": ftc})
                        st.rerun()

        st.markdown("---")
        st.markdown("**➕ Add New Fitting Type**")
        nt1, nt2, nt3 = st.columns([3, 2, 1])
        new_ft_label = nt1.text_input("Label", key="new_ft_label",
                                      placeholder="e.g. Nylor")
        new_ft_code  = nt2.text_input("Code", key="new_ft_code",
                                      placeholder="e.g. NYLOR")
        with nt3:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if st.button("Add", key="add_ft_btn", use_container_width=True,
                         type="primary"):
                if not new_ft_label.strip() or not new_ft_code.strip():
                    st.error("Both required")
                else:
                    code_clean = new_ft_code.strip().upper().replace(" ", "_")
                    _w("""
                        INSERT INTO fitting_types (code, label, sort_order)
                        VALUES (%(c)s, %(l)s,
                            (SELECT COALESCE(MAX(sort_order),0)+10 FROM fitting_types))
                        ON CONFLICT (code) DO UPDATE SET is_active=TRUE, label=%(l)s
                    """, {"c": code_clean, "l": new_ft_label.strip()})
                    st.success(f"✅ '{new_ft_label}' added")
                    st.rerun()

    st.markdown("---")

    # ── Section 2: Add New Fitter ─────────────────────────────────────
    with st.expander("➕ Add New Fitter", expanded=False):
        c1, c2 = st.columns(2)
        new_name  = c1.text_input("Name *", key="new_fitter_name")
        new_phone = c2.text_input("Phone", key="new_fitter_phone")
        new_notes = st.text_input("Notes", key="new_fitter_notes",
                                  placeholder="optional")
        if st.button("💾 Save Fitter", key="save_new_fitter", type="primary",
                     use_container_width=True):
            if not new_name.strip():
                st.error("Name required")
            else:
                _w("""
                    INSERT INTO fitters (fitter_name, contact, notes)
                    VALUES (%(n)s, %(p)s, %(nt)s)
                """, {"n": new_name.strip(), "p": new_phone.strip() or None,
                      "nt": new_notes.strip() or None})
                st.success(f"✅ '{new_name}' added")
                st.rerun()

    st.markdown("---")

    # ── Section 3: Per-fitter rate chart ──────────────────────────────
    fitters      = fetch_fitters(active_only=False)   # show all including inactive
    active_types = fetch_fitting_types()               # only active types in rate grid

    for fitter in fitters:
        fid    = str(fitter["id"])
        fname  = fitter["name"]
        phone  = fitter.get("phone") or ""
        active = bool(fitter.get("is_active", True))
        rates  = fetch_rate_chart(fid)
        rate_map = {r["fitting_type_code"]: r for r in rates}

        _badge = "🟢" if active else "🔴"
        _status = "Active" if active else "Inactive"
        with st.expander(
            f"{_badge} {fname}{'  📞 ' + phone if phone else ''}  ·  {_status}",
            expanded=active   # auto-expand active fitters
        ):
            # ── Fitter details ──
            d1, d2, d3 = st.columns([3, 1, 1])
            new_ph = d1.text_input("Phone", value=phone, key=f"ph_{fid}",
                                   label_visibility="collapsed",
                                   placeholder="Phone number")
            if d2.button("Update Phone", key=f"upd_ph_{fid}",
                         use_container_width=True):
                _w("UPDATE fitters SET contact=%(p)s WHERE id=%(id)s::uuid",
                   {"p": new_ph.strip() or None, "id": fid})
                st.success("✅ Updated")
                st.rerun()
            if d3.button(
                "🔴 Deactivate" if active else "🟢 Activate",
                key=f"toggle_{fid}", use_container_width=True
            ):
                _w("UPDATE fitters SET is_active=%(a)s WHERE id=%(id)s::uuid",
                   {"a": not active, "id": fid})
                st.rerun()

            if not active:
                st.caption("⚠️ Inactive — will not appear in fitting assignment dropdown.")

            st.markdown("**Rate Chart** — ₹ per fitting type")
            st.caption("Set 0 to block. Changing a rate keeps old jobs' rates intact.")

            # Rate grid — active fitting types only
            _cols = st.columns(3)
            updated_rates = {}
            for i, ft in enumerate(active_types):
                ftc = ft["code"]
                existing_rate = float(rate_map.get(ftc, {}).get("rate", 0) or 0)
                with _cols[i % 3]:
                    updated_rates[ftc] = st.number_input(
                        ft["label"],
                        min_value=0.0, step=5.0,
                        value=existing_rate,
                        format="%.2f",
                        key=f"rate_{fid}_{ftc}"
                    )

            # Rate history
            with st.expander("📈 Rate History", expanded=False):
                hist = _q("""
                    SELECT ft.label, frc.rate,
                           frc.effective_from, frc.effective_to
                    FROM fitter_rate_chart frc
                    JOIN fitting_types ft ON ft.code = frc.fitting_type_code
                    WHERE frc.fitter_id = %(fid)s::uuid
                    ORDER BY frc.effective_from DESC
                    LIMIT 30
                """, {"fid": fid})
                if hist:
                    for h in hist:
                        eff_to   = str(h.get("effective_to") or "present")[:10]
                        eff_from = str(h.get("effective_from") or "")[:10]
                        st.caption(
                            f"**{h['label']}** — ₹{h['rate']:.2f} "
                            f"({eff_from} → {eff_to})"
                        )
                else:
                    st.caption("No history yet.")

            if st.button("💾 Save Rates", key=f"save_rates_{fid}",
                         type="primary", use_container_width=True):
                for ftc, rate_val in updated_rates.items():
                    existing = rate_map.get(ftc)
                    if existing and float(existing.get("rate", 0)) != rate_val:
                        _w("UPDATE fitter_rate_chart SET effective_to=CURRENT_DATE-1 "
                           "WHERE id=%(id)s::uuid", {"id": str(existing["id"])})
                        _w("""
                            INSERT INTO fitter_rate_chart
                                (fitter_id, fitting_type_code, rate, effective_from)
                            VALUES (%(fid)s::uuid, %(ftc)s, %(r)s, CURRENT_DATE)
                            ON CONFLICT (fitter_id, fitting_type_code, effective_from)
                            DO UPDATE SET rate=%(r)s
                        """, {"fid": fid, "ftc": ftc, "r": rate_val})
                    elif not existing and rate_val > 0:
                        _w("""
                            INSERT INTO fitter_rate_chart
                                (fitter_id, fitting_type_code, rate, effective_from)
                            VALUES (%(fid)s::uuid, %(ftc)s, %(r)s, CURRENT_DATE)
                            ON CONFLICT (fitter_id, fitting_type_code, effective_from)
                            DO UPDATE SET rate=%(r)s
                        """, {"fid": fid, "ftc": ftc, "r": rate_val})
                st.success("✅ Rates saved")
                st.rerun()


# ── Tab 2: Pending Payouts ────────────────────────────────────────────────────

def _render_payouts_tab():
    st.markdown("#### 💰 Service Provider Payouts")
    st.caption("Registered fitting/colouring assignments appear here automatically. Pay after completion; details can be exported/shared for provider verification.")

    rows = fetch_pending_payouts()
    if not rows:
        st.info("No pending payouts.")
        return

    # Group by fitter
    by_fitter: dict = {}
    for r in rows:
        fn = r["fitter_name"]
        by_fitter.setdefault(fn, {"fitter_id": str(r["fitter_id"]), "phone": r.get("fitter_phone") or "", "rows": []})
        by_fitter[fn]["rows"].append(r)

    for fname, data in by_fitter.items():
        fitter_rows = data["rows"]
        total_due   = sum(float(r.get("balance_due") or 0) for r in fitter_rows)

        with st.expander(
            f"👷 {fname}  —  {len(fitter_rows)} job(s)  |  Due: **{_fmt(total_due)}**",
            expanded=True
        ):
            export_df = pd.DataFrame(fitter_rows)
            export_cols = [c for c in [
                "order_no", "order_type", "party_name", "eye_side", "product_name",
                "fitting_type_label", "status", "pcs_qty", "pair_qty", "pair_rate", "rate_applied", "paid_amount", "balance_due",
                "sent_date", "received_date", "remarks"
            ] if c in export_df.columns]
            if export_cols:
                _download_df(
                    export_df[export_cols],
                    "⬇ Export Payout Details CSV",
                    f"service_payout_{fname.replace(' ', '_')}.csv",
                    f"payout_export_{data['fitter_id']}",
                )
            _wa_phone = "".join(ch for ch in str(data.get("phone") or "") if ch.isdigit())
            if len(_wa_phone) >= 10:
                try:
                    import urllib.parse as _pay_up
                    _lines = [
                        f"Payout verification - {fname}",
                        f"Jobs: {len(fitter_rows)}",
                        f"Total due: Rs.{total_due:.2f}",
                        "",
                    ]
                    for r in fitter_rows:
                        _lines.append(
                            f"{r.get('order_no')} | {r.get('fitting_type_label')} | "
                            f"{r.get('pcs_qty')} pcs ({float(r.get('pair_qty') or 0):g} pair) | "
                            f"{r.get('status')} | Rs.{float(r.get('rate_applied') or 0):.2f}"
                        )
                    _lines.append("")
                    _lines.append("Please verify the above service work and payout amount.")
                    st.link_button(
                        "📲 Send payout verification on WhatsApp",
                        f"https://wa.me/91{_wa_phone[-10:]}?text={_pay_up.quote(chr(10).join(_lines))}",
                        use_container_width=True,
                    )
                except Exception as _wa_e:
                    st.caption(f"WhatsApp summary unavailable: {_wa_e}")
            else:
                st.caption("Add provider mobile to send payout verification on WhatsApp.")
            # Table of jobs
            for r in fitter_rows:
                c1, c2, c3, c4, c5, c6 = st.columns([2.1, 2.4, 1.5, 1.8, 1.4, 1.5])
                c1.markdown(f"**{r['order_no']}** {r.get('eye_side','') or ''}<br><span style='color:#64748b'>{r.get('order_type') or ''} · {r.get('party_name') or ''}</span>", unsafe_allow_html=True)
                c2.markdown(f"{r.get('fitting_type_label', '')}<br><span style='color:#64748b'>{r.get('product_name') or ''}</span><br><span style='color:#94a3b8;font-size:0.75rem'>{r.get('status') or ''}</span>", unsafe_allow_html=True)
                c3.markdown(f"Qty: **{r.get('pcs_qty') or 0} pcs**<br><span style='color:#64748b'>{float(r.get('pair_qty') or 0):g} pair</span>", unsafe_allow_html=True)
                c4.markdown(f"Pair Rate: {_fmt(r.get('pair_rate') or 0)}<br><span style='color:#64748b'>Total {_fmt(r['rate_applied'])}</span>", unsafe_allow_html=True)
                c5.markdown(f"Paid: {_fmt(r.get('paid_amount') or 0)}")
                c6.markdown(f"**Due: {_fmt(r.get('balance_due') or 0)}**")

            st.markdown("---")

            # Quick pay section
            st.markdown(f"**Pay {fname}**")
            p1, p2, p3 = st.columns(3)
            pay_amount = p1.number_input(
                "Amount",
                min_value=0.0, max_value=float(total_due) + 0.01,
                value=float(total_due),
                format="%.2f",
                key=f"pay_amt_{fname}"
            )
            pay_mode = p2.selectbox(
                "Mode",
                ["CASH", "UPI", "BANK"],
                key=f"pay_mode_{fname}"
            )
            pay_ref = p3.text_input("Ref / UTR", key=f"pay_ref_{fname}",
                                    placeholder="optional")
            pay_note = st.text_input("Notes", key=f"pay_note_{fname}",
                                     placeholder="optional")

            if st.button(
                f"✅ Record Payment of {_fmt(pay_amount)} to {fname}",
                key=f"pay_btn_{fname}",
                type="primary",
                use_container_width=True
            ):
                if pay_amount <= 0:
                    st.error("Enter amount > 0")
                else:
                    _record_payment(
                        fitter_id=data["fitter_id"],
                        fitter_rows=fitter_rows,
                        amount=pay_amount,
                        mode=pay_mode,
                        reference=pay_ref,
                        notes=pay_note
                    )
                    st.success(f"✅ Payment of {_fmt(pay_amount)} recorded for {fname}")
                    st.rerun()


def _record_payment(fitter_id, fitter_rows, amount, mode, reference, notes):
    """Distribute payment across assignments oldest-first, update statuses."""
    remaining = float(amount)
    import uuid as _uuid_pay

    # Record in fitter_payments
    _payment_id = str(_uuid_pay.uuid4())
    _w("""
        INSERT INTO fitter_payments
            (id, fitter_id, payment_date, amount, payment_mode, reference_no, notes)
        VALUES
            (%(id)s::uuid, %(fid)s::uuid, CURRENT_DATE, %(amt)s, %(mode)s, %(ref)s, %(notes)s)
    """, {
        "id": _payment_id, "fid": fitter_id, "amt": amount, "mode": mode,
        "ref": reference or None, "notes": notes or None
    })
    _provider_name = ""
    try:
        _provider_name = str(fitter_rows[0].get("fitter_name") or "")
    except Exception:
        _provider_name = ""
    _pno = f"SVCPAY-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        from modules.sql_adapter import run_write as _rw_pay
        _rw_pay("""
            INSERT INTO payments
                (id, payment_no, party_name, payment_date, payment_mode,
                 amount, reference_no, remarks, payment_type, is_advance, created_by)
            VALUES
                (%(id)s::uuid, %(pno)s, %(pn)s, CURRENT_DATE, %(mode)s,
                 %(amt)s, %(ref)s, %(remarks)s, 'DISBURSEMENT', FALSE, %(by)s)
            ON CONFLICT (id) DO NOTHING
        """, {
            "id": _payment_id, "pno": _pno, "pn": _provider_name,
            "mode": mode, "amt": amount, "ref": reference or None,
            "remarks": f"SERVICE_PROVIDER - {notes or ''}".strip(),
            "by": st.session_state.get("user_name", "Staff"),
        })
        from modules.accounting.accounts_engine import post_disbursement_jv
        post_disbursement_jv(
            payment_no=_pno,
            payment_id=_payment_id,
            payee=_provider_name or "Service Provider",
            amount=float(amount),
            category="EXPENSE",
            payment_mode=mode,
            voucher_date=date.today(),
            created_by=st.session_state.get("user_name", "Staff"),
        )
    except Exception as _exp_e:
        st.caption(f"Expense posting skipped: {_exp_e}")

    # Distribute across assignments oldest-first
    for r in sorted(fitter_rows, key=lambda x: str(x.get("created_at", ""))):
        if remaining <= 0:
            break
        balance = float(r.get("balance_due") or 0)
        if balance <= 0:
            continue
        payment_this = min(remaining, balance)
        new_paid = float(r.get("paid_amount") or 0) + payment_this
        new_status = "PAID" if new_paid >= float(r["rate_applied"]) - 0.01 else "PARTIALLY_PAID"
        _w("""
            UPDATE fitting_assignments
            SET paid_amount    = %(pa)s,
                payment_status = %(ps)s,
                paid_date      = CURRENT_DATE
            WHERE id = %(id)s::uuid
        """, {"pa": new_paid, "ps": new_status, "id": str(r["id"])})
        remaining -= payment_this


# ── Tab 3: Payment History ────────────────────────────────────────────────────

def _render_history_tab():
    st.markdown("#### 📋 Service Provider Payment History")

    fitters = fetch_fitters()
    fitter_options = {"All fitters": None}
    for f in fitters:
        fitter_options[f["name"]] = str(f["id"])

    selected_name = st.selectbox("Filter by fitter", list(fitter_options.keys()),
                                 key="hist_fitter_filter")
    fid_filter = fitter_options[selected_name]

    history = fetch_payment_history(fid_filter)
    if not history:
        st.info("No payment records found.")
        return

    total_paid = sum(float(r.get("amount") or 0) for r in history)
    st.metric("Total Paid (shown)", _fmt(total_paid))
    hist_df = pd.DataFrame(history)
    if not hist_df.empty:
        _download_df(
            hist_df,
            "⬇ Export Payment History CSV",
            "service_provider_payment_history.csv",
            "svc_payment_history_export",
        )
    st.markdown("---")

    for r in history:
        c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 2])
        pd = str(r.get("payment_date", ""))[:10]
        c1.markdown(f"**{r['fitter_name']}**")
        c2.markdown(f"📅 {pd}")
        c3.markdown(f"**{_fmt(r['amount'])}**")
        c4.markdown(r.get("payment_mode", ""))
        c5.markdown(r.get("reference_no") or r.get("notes") or "—")


# ── Summary stats ─────────────────────────────────────────────────────────────

def _render_summary():
    rows = _q("""
        SELECT fi.fitter_name AS name,
               COUNT(fa.id)                           AS total_jobs,
               SUM(fa.rate_applied)                  AS total_earned,
               SUM(COALESCE(fa.paid_amount,0))        AS total_paid,
               SUM(fa.rate_applied - COALESCE(fa.paid_amount,0))
                   FILTER (WHERE fa.payment_status != 'PAID') AS balance_due
        FROM fitting_assignments fa
        JOIN fitters fi ON fi.id = fa.fitter_id
        WHERE fa.status IN ('RECEIVED','DONE')
        GROUP BY fi.fitter_name
        ORDER BY balance_due DESC NULLS LAST
    """)
    if not rows:
        return
    st.markdown("#### 📊 Fitter Summary")
    cols = st.columns(len(rows)) if len(rows) <= 4 else st.columns(4)
    for i, r in enumerate(rows[:4]):
        with cols[i % 4]:
            st.metric(
                r["name"],
                _fmt(r.get("balance_due") or 0),
                delta=f"₹{float(r.get('total_earned') or 0):,.0f} total",
                delta_color="off"
            )

def _render_service_catalog_tab():
    st.markdown("#### 🧾 Service Master")
    st.caption("Services created here appear in Backoffice service dropdowns. Colouring/Fitting route to production; Courier/Other go direct to billing.")
    try:
        from modules.backoffice.service_master import fetch_service_types, seed_default_services, update_service_type
        seed_default_services()
    except Exception as e:
        st.error(f"Service master unavailable: {e}")
        return

    rows = fetch_service_types(active_only=False)
    groups = ["COLOURING", "FITTING", "COURIER", "OTHER"]

    with st.expander("➕ Add New Service", expanded=False):
        c1, c2, c3 = st.columns([2, 1, 1])
        name = c1.text_input("Service Name", placeholder="e.g. Home Consultation", key="svc_new_name")
        group = c2.selectbox("Group", groups, key="svc_new_group")
        gst = c3.number_input("GST %", min_value=0.0, max_value=28.0, value=18.0, step=0.5, key="svc_new_gst")
        c4, c5, c6 = st.columns(3)
        rp = c4.number_input("Retail ₹", min_value=0.0, step=10.0, key="svc_new_rp")
        wp = c5.number_input("Wholesale ₹", min_value=0.0, step=10.0, key="svc_new_wp")
        route_default = group if group in ("COLOURING", "FITTING") else ""
        route = c6.selectbox("Production Route", ["", "COLOURING", "FITTING"], index=["", "COLOURING", "FITTING"].index(route_default), key="svc_new_route")
        notes = st.text_input("Notes", key="svc_new_notes")
        if st.button("💾 Save Service", type="primary", use_container_width=True, key="svc_new_save"):
            if not name.strip():
                st.warning("Service name required")
            else:
                code = f"{group}_{name}".upper()
                import re as _re
                code = _re.sub(r"[^A-Z0-9]+", "_", code).strip("_")[:40]
                _w("""
                    INSERT INTO service_types
                        (service_code, service_group, service_name, retail_price,
                         wholesale_price, gst_percent, production_route, sort_order, notes)
                    VALUES
                        (%(c)s, %(g)s, %(n)s, %(rp)s, %(wp)s, %(gst)s, %(r)s,
                         (SELECT COALESCE(MAX(sort_order),0)+10 FROM service_types), %(nt)s)
                    ON CONFLICT (service_code) DO UPDATE SET
                        service_name=%(n)s, service_group=%(g)s,
                        retail_price=%(rp)s, wholesale_price=%(wp)s,
                        gst_percent=%(gst)s, production_route=%(r)s,
                        is_active=TRUE, notes=%(nt)s, updated_at=NOW()
                """, {"c": code, "g": group, "n": name.strip(), "rp": rp, "wp": wp, "gst": gst, "r": route, "nt": notes})
                st.success("Service saved")
                st.rerun()

    for group in groups:
        grp_rows = [r for r in rows if str(r.get("service_group")).upper() == group]
        with st.expander(f"{group.title()} · {len(grp_rows)}", expanded=(group in ("COLOURING", "FITTING"))):
            if not grp_rows:
                st.caption("No services yet.")
            for r in grp_rows:
                sid = str(r["id"])
                code = str(r.get("service_code") or "")
                with st.container():
                    st.caption(f"Code: {code} · Editing name/spelling/price here affects all new dropdowns.")
                    c1, c2, c3, c4, c5, c6 = st.columns([2.4, 1, 1, .8, 1, .8])
                    name = c1.text_input("Service Name", value=r.get("service_name") or "", key=f"svc_name_{sid}", label_visibility="collapsed")
                    rp = c2.number_input("Retail ₹", value=float(r.get("retail_price") or 0), min_value=0.0, step=10.0, key=f"svc_rp_{sid}", label_visibility="collapsed")
                    wp = c3.number_input("Wholesale ₹", value=float(r.get("wholesale_price") or 0), min_value=0.0, step=10.0, key=f"svc_wp_{sid}", label_visibility="collapsed")
                    gst = c4.number_input("GST %", value=float(r.get("gst_percent") or 0), min_value=0.0, max_value=28.0, step=0.5, key=f"svc_gst_{sid}", label_visibility="collapsed")
                    route = c5.selectbox("Route", ["", "COLOURING", "FITTING"], index=(["", "COLOURING", "FITTING"].index(str(r.get("production_route") or "")) if str(r.get("production_route") or "") in ["", "COLOURING", "FITTING"] else 0), key=f"svc_route_{sid}", label_visibility="collapsed")
                    _active_opts = ["Active", "Inactive"]
                    active_choice = c6.selectbox(
                        "Status",
                        _active_opts,
                        index=0 if bool(r.get("is_active", True)) else 1,
                        key=f"svc_active_{sid}",
                        label_visibility="collapsed",
                    )
                    active = active_choice == "Active"
                    b1, b2 = st.columns([1, 3])
                    if b1.button("Apply Changes", key=f"svc_save_{sid}", use_container_width=True, type="primary"):
                        if not str(name or "").strip():
                            st.warning("Service name cannot be blank.")
                        else:
                            update_service_type(
                                service_code=code,
                                service_name=name,
                                retail_price=rp,
                                wholesale_price=wp,
                                gst_percent=gst,
                                production_route=route,
                                is_active=active,
                                notes=r.get("notes") or "",
                            )
                            # Clear open service picker states so punching/backoffice redraws labels.
                            for _k in list(st.session_state.keys()):
                                if str(_k).startswith(("sc_add_", "ws_sc_add_", "svc_")) and "svc_save_" not in str(_k):
                                    st.session_state.pop(_k, None)
                            st.success("Service updated. New dropdowns will use this name and price.")
                            st.rerun()
                    b2.caption("Turn Active off to hide old/default rows from punching and backoffice without deleting history.")
                    st.markdown("---")


def _render_service_providers_tab():
    st.markdown("#### 🧑‍🔧 Service Providers & Purchase Rates")
    st.caption("Set fitter/colouring/courier provider purchase rates. These are used later in production/service assignment.")
    try:
        from modules.backoffice.service_master import (
            fetch_service_types,
            fetch_providers,
            fetch_provider_rates,
            fetch_courier_rate_options,
            save_courier_rate_option,
            seed_default_services,
        )
        seed_default_services()
    except Exception as e:
        st.error(f"Service master unavailable: {e}")
        return

    service_rows = fetch_service_types(active_only=True)
    provider_types = ["FITTING", "COLOURING", "COURIER", "OTHER"]
    with st.expander("➕ Add Provider", expanded=False):
        c1, c2, c3 = st.columns(3)
        pname = c1.text_input("Provider Name", key="sp_new_name")
        ptype = c2.selectbox("Type", provider_types, key="sp_new_type")
        phone = c3.text_input("Phone", key="sp_new_phone")
        g1, g2, g3 = st.columns([1.4, 1, 1])
        gstin_new = g1.text_input("GSTIN", key="sp_new_gstin", placeholder="Blank for non-GST courier/provider")
        gst_reg_new = g2.checkbox("GST Provider", value=False, key="sp_new_gstreg")
        gst_pct_new = g3.number_input(
            "Provider GST %",
            min_value=0.0,
            max_value=28.0,
            value=5.0 if gst_reg_new and ptype == "COURIER" else 0.0,
            step=1.0,
            key="sp_new_gstpct",
        )
        notes = st.text_input("Notes", key="sp_new_notes")
        if st.button("💾 Save Provider", type="primary", use_container_width=True, key="sp_new_save"):
            if not pname.strip():
                st.warning("Provider name required")
            else:
                import uuid as _uuid_sp
                _pid_new = str(_uuid_sp.uuid4())
                _w("""
                    INSERT INTO service_providers (
                        id, provider_name, provider_type, contact, notes,
                        gstin, gst_registered, default_gst_percent
                    )
                    VALUES (
                        %(id)s::uuid, %(n)s, %(t)s, %(p)s, %(nt)s,
                        %(gstin)s, %(gr)s, %(gp)s
                    )
                """, {
                    "id": _pid_new, "n": pname.strip(), "t": ptype,
                    "p": phone.strip() or None, "nt": notes.strip() or None,
                    "gstin": gstin_new.strip().upper() or None,
                    "gr": bool(gst_reg_new), "gp": float(gst_pct_new or 0),
                })
                _w("""
                    INSERT INTO fitters (id, fitter_name, fitter_type, contact, is_active, notes)
                    VALUES (%(id)s::uuid, %(n)s, %(t)s, %(p)s, TRUE, %(nt)s)
                    ON CONFLICT (id) DO UPDATE SET
                        fitter_name=%(n)s, fitter_type=%(t)s, contact=%(p)s,
                        notes=%(nt)s, is_active=TRUE, updated_at=NOW()
                """, {"id": _pid_new, "n": pname.strip(), "t": ptype, "p": phone.strip() or None, "nt": notes.strip() or None})
                st.success("Provider saved")
                st.rerun()

    providers = fetch_providers(active_only=False)
    for group in provider_types:
        group_rows = [p for p in providers if str(p.get("provider_type") or "").upper() == group]
        with st.expander(f"{group.title()} Providers · {len(group_rows)}", expanded=(group in ("FITTING", "COLOURING"))):
            if not group_rows:
                st.caption("No providers in this group.")
                continue
            for p in group_rows:
                pid = str(p["id"])
                with st.container():
                    st.markdown(f"**{'🟢' if p.get('is_active') else '🔴'} {p.get('provider_name')}**")
                    d1, d2, d3 = st.columns([2, 1, 1])
                    phone = d1.text_input("Phone", value=p.get("contact") or "", key=f"sp_phone_{pid}")
                    ptype = d2.selectbox("Type", provider_types, index=provider_types.index(p.get("provider_type")) if p.get("provider_type") in provider_types else 0, key=f"sp_type_{pid}")
                    active = d3.checkbox("Active", value=bool(p.get("is_active", True)), key=f"sp_active_{pid}")
                    g1, g2, g3 = st.columns([1.4, 1, 1])
                    gstin = g1.text_input("GSTIN", value=p.get("gstin") or "", key=f"sp_gstin_{pid}", placeholder="Blank for non-GST provider")
                    gst_reg = g2.checkbox("GST Provider", value=bool(p.get("gst_registered", False)), key=f"sp_gstreg_{pid}")
                    gst_pct = g3.number_input("Provider GST %", min_value=0.0, max_value=28.0, step=1.0,
                                              value=float(p.get("default_gst_percent") or (5 if gst_reg and str(ptype).upper()=="COURIER" else 0)),
                                              key=f"sp_gstpct_{pid}")
                    if st.button("Save Provider Details", key=f"sp_save_{pid}", use_container_width=True):
                        _w("""
                            UPDATE service_providers
                            SET contact=%(p)s, provider_type=%(t)s, is_active=%(a)s,
                                gstin=%(gstin)s, gst_registered=%(gr)s,
                                default_gst_percent=%(gp)s, updated_at=NOW()
                            WHERE id=%(id)s::uuid
                        """, {
                            "p": phone.strip() or None, "t": ptype, "a": active,
                            "gstin": gstin.strip().upper() or None,
                            "gr": bool(gst_reg), "gp": float(gst_pct or 0),
                            "id": pid,
                        })
                        _w("""
                            INSERT INTO fitters (id, fitter_name, fitter_type, contact, is_active)
                            VALUES (%(id)s::uuid, %(n)s, %(t)s, %(p)s, %(a)s)
                            ON CONFLICT (id) DO UPDATE SET
                                fitter_name=%(n)s, fitter_type=%(t)s, contact=%(p)s,
                                is_active=%(a)s, updated_at=NOW()
                        """, {"id": pid, "n": p.get("provider_name"), "t": ptype, "p": phone.strip() or None, "a": active})
                        st.success("Provider updated")
                        st.rerun()

                    rates = {r["service_code"]: r for r in fetch_provider_rates(pid)}
                    matching_services = [
                        svc for svc in service_rows
                        if str(svc.get("service_group") or "").upper() == str(ptype or "").upper()
                    ]
                    st.markdown(f"**Purchase Rate Chart — {ptype.title()} only**")
                    if not matching_services:
                        st.caption("No active service is defined for this provider type.")
                        continue
                    cols = st.columns(3)
                    for i, svc in enumerate(matching_services):
                        code = svc["service_code"]
                        old = float(rates.get(code, {}).get("purchase_rate") or 0)
                        with cols[i % 3]:
                            val = st.number_input(svc["service_name"], min_value=0.0, step=5.0, value=old, key=f"spr_{pid}_{code}")
                            if val != old:
                                st.session_state[f"spr_dirty_{pid}"] = True
                    if st.button("💾 Save Rates", key=f"spr_save_{pid}", use_container_width=True):
                        for svc in matching_services:
                            code = svc["service_code"]
                            key = f"spr_{pid}_{code}"
                            if key not in st.session_state:
                                continue
                            val = float(st.session_state.get(key) or 0)
                            if val <= 0:
                                continue
                            _w("""
                                INSERT INTO service_provider_rates (provider_id, service_code, purchase_rate, effective_from)
                                VALUES (%(pid)s::uuid, %(c)s, %(r)s, CURRENT_DATE)
                                ON CONFLICT (provider_id, service_code, effective_from)
                                DO UPDATE SET purchase_rate=%(r)s, is_active=TRUE, updated_at=NOW()
                            """, {"pid": pid, "c": code, "r": val})
                        st.session_state.pop(f"spr_dirty_{pid}", None)
                        st.success("Rates saved")
                        st.rerun()
                    if str(ptype or "").upper() == "COURIER":
                        st.markdown("**Parcel / Size Charge Options**")
                        slab_rows = fetch_courier_rate_options(pid, active_only=False)
                        if not slab_rows:
                            st.caption("No parcel slabs yet. Add options like Small packet, Medium parcel, Large parcel.")
                        for slab in slab_rows:
                            sid = str(slab.get("id") or "")
                            s1, s2, s3, s4, s5 = st.columns([1.8, 1, 1, 1, 0.8])
                            lbl = s1.text_input("Slab", value=slab.get("option_label") or "", key=f"cro_lbl_{sid}")
                            code = s2.text_input("Code", value=slab.get("parcel_size_code") or "", key=f"cro_code_{sid}")
                            amt = s3.number_input("₹", min_value=0.0, step=5.0, value=float(slab.get("charge_base") or 0), key=f"cro_amt_{sid}")
                            gst = s4.number_input("GST %", min_value=0.0, max_value=28.0, step=0.5, value=float(slab.get("gst_percent") or 18), key=f"cro_gst_{sid}")
                            active_slab = s5.checkbox("Active", value=bool(slab.get("is_active", True)), key=f"cro_act_{sid}")
                            notes_slab = st.text_input("Notes", value=slab.get("notes") or "", key=f"cro_notes_{sid}")
                            if st.button("Save Slab", key=f"cro_save_{sid}", use_container_width=True):
                                save_courier_rate_option(
                                    provider_id=pid,
                                    option_id=sid,
                                    option_label=lbl,
                                    parcel_size_code=code,
                                    charge_base=amt,
                                    gst_percent=gst,
                                    is_active=active_slab,
                                    notes=notes_slab,
                                )
                                st.success("Courier slab saved")
                                st.rerun()
                        with st.expander("➕ Add Courier Parcel Slab", expanded=False):
                            n1, n2, n3, n4 = st.columns([1.7, 1, 1, 1])
                            new_lbl = n1.text_input("Slab / parcel size", key=f"cro_new_lbl_{pid}", placeholder="Small packet / 1kg / Local")
                            new_code = n2.text_input("Code", key=f"cro_new_code_{pid}", placeholder="S / 1KG")
                            default_gst = float(p.get("default_gst_percent") or 18) if bool(p.get("gst_registered")) else 18.0
                            new_amt = n3.number_input("Charge ₹", min_value=0.0, step=5.0, key=f"cro_new_amt_{pid}")
                            new_gst = n4.number_input("GST %", min_value=0.0, max_value=28.0, step=0.5, value=default_gst, key=f"cro_new_gst_{pid}")
                            new_notes = st.text_input("Notes", key=f"cro_new_notes_{pid}")
                            if st.button("➕ Save New Courier Slab", key=f"cro_new_save_{pid}", use_container_width=True):
                                if not new_lbl.strip():
                                    st.warning("Slab name is required.")
                                else:
                                    save_courier_rate_option(
                                        provider_id=pid,
                                        option_label=new_lbl,
                                        parcel_size_code=new_code,
                                        charge_base=new_amt,
                                        gst_percent=new_gst,
                                        is_active=True,
                                        notes=new_notes,
                                    )
                                    st.success("Courier slab added")
                                    st.rerun()
                    st.markdown("---")


def _render_party_service_rules_tab():
    st.markdown("#### 🧾 Party Service Rules")
    st.caption("Use this for party-specific courier/service charges, such as 50% courier charge or fixed bus service charge.")
    try:
        from modules.backoffice.service_master import (
            fetch_service_types,
            fetch_party_service_rates,
            service_price,
            upsert_party_service_rate,
        )
    except Exception as e:
        st.error(f"Service rule module unavailable: {e}")
        return

    term = st.text_input("Search Party", key="psr_party_search", placeholder="Type party name or mobile")
    if not term.strip():
        st.info("Search and select a party to define automatic service charges.")
        return
    parties = _q(
        """
        SELECT id::text, party_name, party_type, COALESCE(mobile,'') AS mobile
        FROM parties
        WHERE COALESCE(is_active, TRUE)=TRUE
          AND (party_name ILIKE %(q)s OR COALESCE(mobile,'') ILIKE %(q)s)
        ORDER BY party_name
        LIMIT 50
        """,
        {"q": f"%{term.strip()}%"},
    )
    if not parties:
        st.warning("No party found.")
        return

    labels = [f"{p['party_name']} · {p.get('party_type') or ''} · {p.get('mobile') or ''}" for p in parties]
    sel_i = st.selectbox("Party", range(len(parties)), format_func=lambda i: labels[i], key="psr_party_sel")
    party = parties[int(sel_i)]
    party_id = str(party["id"])

    services = fetch_service_types(active_only=True)
    existing = {r["service_code"]: r for r in fetch_party_service_rates(party_id)}
    st.markdown(f"**Rules for {party['party_name']}**")
    try:
        from modules.backoffice.service_master import fetch_providers
        courier_providers = fetch_providers("COURIER", active_only=True)
    except Exception:
        courier_providers = []
    pref_rows = _q("""
        SELECT preferred_courier_provider_id::text AS provider_id,
               COALESCE(preferred_courier_name,'') AS provider_name
        FROM parties
        WHERE id=%(pid)s::uuid
        LIMIT 1
    """, {"pid": party_id}) or []
    current_pref_id = str((pref_rows[0].get("provider_id") if pref_rows else "") or "")
    with st.expander("🚚 Preferred Courier Partner", expanded=True):
        if not courier_providers:
            st.info("No active COURIER providers found. Add courier companies in Service Management → Providers & Rates.")
        labels = ["— No preferred courier —"] + [
            f"{p['provider_name']} · {'GST' if p.get('gst_registered') else 'Non-GST'}"
            + (f" · {float(p.get('default_gst_percent') or 0):g}%" if p.get("gst_registered") else "")
            for p in courier_providers
        ]
        ids = [""] + [str(p["id"]) for p in courier_providers]
        idx = ids.index(current_pref_id) if current_pref_id in ids else 0
        pick = st.selectbox("Default courier for this party", range(len(labels)),
                            format_func=lambda i: labels[i], index=idx,
                            key=f"psr_pref_courier_{party_id}")
        if st.button("💾 Save Preferred Courier", key=f"psr_pref_courier_save_{party_id}", use_container_width=True):
            chosen_id = ids[int(pick)]
            chosen_name = ""
            if chosen_id:
                chosen_name = next((p["provider_name"] for p in courier_providers if str(p["id"]) == chosen_id), "")
            _w("""
                UPDATE parties
                SET preferred_courier_provider_id = NULLIF(%(cid)s,'')::uuid,
                    preferred_courier_name = NULLIF(%(cn)s,'')
                WHERE id=%(pid)s::uuid
            """, {"cid": chosen_id, "cn": chosen_name, "pid": party_id})
            st.success("Preferred courier saved")
            st.rerun()

    for group in ("COURIER", "FITTING", "COLOURING", "OTHER"):
        rows = [s for s in services if str(s.get("service_group") or "").upper() == group]
        if not rows:
            continue
        with st.expander(f"{group.title()} Rules", expanded=(group == "COURIER")):
            for svc in rows:
                code = str(svc.get("service_code") or "")
                old = existing.get(code, {})
                st.caption(
                    f"{svc.get('service_name')} · Master Retail ₹{float(svc.get('retail_price') or 0):,.0f} "
                    f"· Master Wholesale ₹{float(svc.get('wholesale_price') or 0):,.0f}"
                )
                c1, c2, c3, c4, c5 = st.columns([1.2, 1, 1, 1, 1])
                mode_opts = ["FIXED", "PERCENT_OF_MASTER"]
                mode = c1.selectbox(
                    "Mode", mode_opts,
                    index=mode_opts.index(old.get("price_mode")) if old.get("price_mode") in mode_opts else 0,
                    key=f"psr_mode_{party_id}_{code}", label_visibility="collapsed",
                )
                rp = c2.number_input("Retail", min_value=0.0, step=10.0,
                                     value=float(old.get("retail_price") or 0),
                                     key=f"psr_rp_{party_id}_{code}",
                                     label_visibility="collapsed",
                                     disabled=(mode == "PERCENT_OF_MASTER"))
                wp = c3.number_input("Wholesale", min_value=0.0, step=10.0,
                                     value=float(old.get("wholesale_price") or 0),
                                     key=f"psr_wp_{party_id}_{code}",
                                     label_visibility="collapsed",
                                     disabled=(mode == "PERCENT_OF_MASTER"))
                pct = c4.number_input("%", min_value=0.0, max_value=500.0, step=5.0,
                                      value=float(old.get("price_percent") or (50 if mode == "PERCENT_OF_MASTER" else 0)),
                                      key=f"psr_pct_{party_id}_{code}",
                                      label_visibility="collapsed",
                                      disabled=(mode == "FIXED"))
                status = c5.selectbox("Status", ["Active", "Inactive"],
                                      index=0 if bool(old.get("is_active", True)) else 1,
                                      key=f"psr_active_{party_id}_{code}",
                                      label_visibility="collapsed")
                resolved = service_price(svc, "WHOLESALE", party_id=party_id)
                st.caption(f"Current party wholesale result: ₹{resolved:,.2f}")
                if st.button("Save Rule", key=f"psr_save_{party_id}_{code}", use_container_width=True):
                    upsert_party_service_rate(
                        party_id=party_id,
                        service_code=code,
                        price_mode=mode,
                        retail_price=rp,
                        wholesale_price=wp,
                        price_percent=pct,
                        is_active=(status == "Active"),
                        notes="",
                    )
                    st.success("Party service rule saved")
                    st.rerun()
                st.markdown("---")


# ── Main entry point ──────────────────────────────────────────────────────────

def render_fitter_management():
    st.markdown("## 🔧 Service Management")
    _render_summary()
    st.markdown("---")

    tab0, tabp, tabr, tab2, tab3 = st.tabs([
        "🧾 Service Master",
        "🧑‍🔧 Providers & Rates",
        "🏷 Party Service Rules",
        "💰 Provider Payouts",
        "📋 Payment History",
    ])
    with tab0:
        _render_service_catalog_tab()
    with tabp:
        _render_service_providers_tab()
    with tabr:
        _render_party_service_rules_tab()
    with tab2:
        _render_payouts_tab()
    with tab3:
        _render_history_tab()
    if st.session_state.get("_show_legacy_fitter_table", False):
        with st.expander("⚙️ Compatibility: old fitter table", expanded=False):
            st.caption("Hidden by default. New work should use Providers & Rates above.")
            _render_fitters_tab()
