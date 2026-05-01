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
    """All fitting_assignments that are DONE but UNPAID or PARTIALLY_PAID, grouped by fitter."""
    return _q("""
        SELECT fa.id, fa.order_no, fa.eye_side, fa.fitting_type_code,
               ft.label AS fitting_type_label,
               fa.rate_applied, fa.sent_date, fa.received_date,
               fa.status, fa.payment_status, fa.paid_amount,
               fa.remarks,
               fi.id AS fitter_id, fi.fitter_name,
               (fa.rate_applied - COALESCE(fa.paid_amount, 0)) AS balance_due
        FROM fitting_assignments fa
        JOIN fitters fi ON fi.id = fa.fitter_id
        JOIN fitting_types ft ON ft.code = fa.fitting_type_code
        WHERE fa.status IN ('RECEIVED', 'DONE')
          AND fa.payment_status IN ('UNPAID', 'PARTIALLY_PAID')
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
    st.markdown("#### 💰 Pending Payouts")

    rows = fetch_pending_payouts()
    if not rows:
        st.info("No pending payouts.")
        return

    # Group by fitter
    by_fitter: dict = {}
    for r in rows:
        fn = r["fitter_name"]
        by_fitter.setdefault(fn, {"fitter_id": str(r["fitter_id"]), "rows": []})
        by_fitter[fn]["rows"].append(r)

    for fname, data in by_fitter.items():
        fitter_rows = data["rows"]
        total_due   = sum(float(r.get("balance_due") or 0) for r in fitter_rows)

        with st.expander(
            f"👷 {fname}  —  {len(fitter_rows)} job(s)  |  Due: **{_fmt(total_due)}**",
            expanded=True
        ):
            # Table of jobs
            for r in fitter_rows:
                c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 2])
                c1.markdown(f"**{r['order_no']}** {r.get('eye_side','') or ''}")
                c2.markdown(r.get("fitting_type_label", ""))
                c3.markdown(f"Rate: {_fmt(r['rate_applied'])}")
                c4.markdown(f"Paid: {_fmt(r.get('paid_amount') or 0)}")
                c5.markdown(f"**Due: {_fmt(r.get('balance_due') or 0)}**")

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

    # Record in fitter_payments
    _w("""
        INSERT INTO fitter_payments
            (fitter_id, payment_date, amount, payment_mode, reference_no, notes)
        VALUES
            (%(fid)s::uuid, CURRENT_DATE, %(amt)s, %(mode)s, %(ref)s, %(notes)s)
    """, {
        "fid": fitter_id, "amt": amount, "mode": mode,
        "ref": reference or None, "notes": notes or None
    })

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
    st.markdown("#### 📋 Payment History")

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


# ── Main entry point ──────────────────────────────────────────────────────────

def render_fitter_management():
    st.markdown("## 🧵 Fitter Management")
    _render_summary()
    st.markdown("---")

    tab1, tab2, tab3 = st.tabs(["👷 Fitters & Rates", "💰 Pending Payouts", "📋 Payment History"])
    with tab1:
        _render_fitters_tab()
    with tab2:
        _render_payouts_tab()
    with tab3:
        _render_history_tab()
