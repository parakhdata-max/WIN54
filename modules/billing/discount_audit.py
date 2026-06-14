"""
modules/pricing/discount_audit.py
===================================
Discount Audit — Survey, Leakage Report, and Billing Gate.

Three modes:
  1. ORDER AUDIT     — per-order: expected vs applied, line-by-line
  2. LEAKAGE REPORT  — date-range summary: orders where discount was zero
                       but a rule should have fired
  3. BILLING GATE    — called from billing_hub before raising challan/invoice;
                       warns or blocks if discount anomaly detected

Entry points:
  render_discount_audit()         → full standalone UI
  billing_gate_check(order_ids)   → returns (ok, issues) for billing_hub
"""
from __future__ import annotations
import json
import datetime as _dt
import html    as _hesc
import streamlit as st


# ── DB helpers ────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}"); return []

def _q_safe(sql, params=None):
    """Silent version — returns [] on error, no st.error"""
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


# ── Audit engine ─────────────────────────────────────────────────────────────

def _audit_order_lines(order_id: str) -> list[dict]:
    """
    For every non-deleted line on an order, compute:
      - applied discount (what's in DB)
      - expected discount (re-run engine dry-run)
      - status: OK | CANCELLED | ZERO_NO_RULE | MISMATCH | MARGIN_BLOCKED
    """
    rows = _q_safe("""
        SELECT
            ol.id::text         AS line_id,
            o.order_no,
            o.order_type,
            o.party_id::text    AS party_id,
            COALESCE(p.product_name,'Lens')  AS product_name,
            COALESCE(p.brand,'')             AS brand,
            COALESCE(p.main_group,'')        AS main_group,
            ol.eye_side,
            COALESCE(ol.quantity,1)          AS quantity,
            COALESCE(ol.unit_price,0)        AS unit_price,
            COALESCE(ol.discount_percent,0)  AS discount_percent,
            COALESCE(ol.discount_amount,0)   AS discount_amount,
            COALESCE(ol.applied_rule_ids,'') AS applied_rule_ids,
            COALESCE(ol.total_price,0)       AS total_price,
            COALESCE(ol.gst_percent,0)       AS gst_percent,
            ol.lens_params,
            ol.product_id::text AS product_id
        FROM order_lines ol
        JOIN orders o   ON o.id = ol.order_id
        JOIN products p ON p.id = ol.product_id
        WHERE ol.order_id = %(oid)s::uuid
          AND COALESCE(ol.is_deleted,FALSE) = FALSE
          AND COALESCE(ol.is_service_line,FALSE) = FALSE
          AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
        ORDER BY ol.eye_side
    """, {"oid": order_id})

    if not rows:
        return []

    order_type = rows[0].get("order_type","WHOLESALE")
    party_id   = rows[0].get("party_id","")

    # Dry-run: re-apply discount engine to get expected values
    # We work on copies so we don't mutate DB data
    import copy
    dry_lines = copy.deepcopy(rows)
    try:
        from modules.pricing.discount_engine import apply_discounts
        apply_discounts(dry_lines, party_id=party_id, order_type=order_type)
    except Exception as _e:
        for dl in dry_lines:
            dl["discount_amount"]  = 0.0
            dl["discount_percent"] = 0.0
            dl["discount_rule"]    = f"engine_error:{_e}"
            dl["applied_rule_ids"] = ""

    # Build audit result per line
    result = []
    for orig, dry in zip(rows, dry_lines):
        lp = orig.get("lens_params") or {}
        if isinstance(lp, str):
            try: lp = json.loads(lp)
            except: lp = {}

        applied_amt  = float(orig.get("discount_amount") or 0)
        expected_amt = float(dry.get("discount_amount") or 0)
        applied_rule = str(orig.get("applied_rule_ids") or "")
        expected_rule= str(dry.get("discount_rule") or dry.get("applied_rule_ids") or "")
        disc_status  = str(lp.get("discount_status") or "").upper()
        margin_blocked = bool(dry.get("margin_blocked"))
        margin_pct   = float(dry.get("margin_pct") or 0)
        prev_disc    = lp.get("discount_previous") or {}

        gross = round(float(orig.get("unit_price",0)) * int(orig.get("quantity",1)), 2)

        # Classify
        if disc_status == "CANCELLED":
            status = "CANCELLED"
            flag   = "⚠️"
            note   = (
                f"Discount manually cancelled by {lp.get('discount_cancelled_by','?')}. "
                + (f"Was: ₹{float(prev_disc.get('discount_amount',0)):,.2f}" if prev_disc else "")
            )
        elif margin_blocked and applied_amt < expected_amt:
            status = "MARGIN_BLOCKED"
            flag   = "🛡️"
            note   = f"Margin guard capped discount (margin {margin_pct:.1f}% < min)"
        elif applied_amt == 0 and expected_amt == 0:
            status = "NO_DISCOUNT"
            flag   = "—"
            note   = "No rule applies to this line"
        elif applied_amt == 0 and expected_amt > 0:
            status = "MISSED"
            flag   = "❌"
            note   = f"Discount NOT applied — engine expects ₹{expected_amt:,.2f} via '{expected_rule}'"
        elif abs(applied_amt - expected_amt) > 0.5:
            status = "MISMATCH"
            flag   = "🔶"
            note   = (
                f"Applied ₹{applied_amt:,.2f} but engine now gives ₹{expected_amt:,.2f} "
                f"(rule may have changed since order was punched)"
            )
        else:
            status = "OK"
            flag   = "✅"
            note   = f"₹{applied_amt:,.2f} applied · rule: {applied_rule or '—'}"

        result.append({
            "line_id":       orig["line_id"],
            "order_no":      orig["order_no"],
            "product_name":  orig["product_name"],
            "eye_side":      orig["eye_side"],
            "gross":         gross,
            "applied_amt":   applied_amt,
            "expected_amt":  expected_amt,
            "applied_rule":  applied_rule,
            "expected_rule": expected_rule,
            "status":        status,
            "flag":          flag,
            "note":          note,
            "disc_status_lp": disc_status,
            "prev_disc":     prev_disc,
            "party_id":      party_id,
            "order_id":      order_id,
            "order_type":    order_type,
        })
    return result


def _leakage_query(dfrom, dto, party_id="", min_gross=0.0) -> list[dict]:
    """Orders with at least one line where discount=0 but engine expects >0."""
    w = [
        "o.created_at::date BETWEEN %(df)s AND %(dt)s",
        "COALESCE(ol.is_deleted,FALSE) = FALSE",
        "COALESCE(ol.is_service_line,FALSE) = FALSE",
        "UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')",
        "COALESCE(ol.discount_amount,0) = 0",
        "UPPER(COALESCE(o.order_type,'WHOLESALE')) != 'RETAIL'",
    ]
    p = {"df": str(dfrom), "dt": str(dto)}
    if party_id:
        w.append("o.party_id = %(pid)s::uuid")
        p["pid"] = party_id
    if min_gross > 0:
        w.append("ol.unit_price * ol.quantity >= %(mg)s")
        p["mg"] = min_gross

    return _q_safe(f"""
        SELECT
            o.id::text             AS order_id,
            o.order_no,
            COALESCE(o.patient_name, o.party_name,'—') AS party_name,
            o.order_type,
            o.created_at::date::text AS order_date,
            COUNT(ol.id)           AS zero_disc_lines,
            SUM(ol.unit_price * ol.quantity) AS gross_value
        FROM orders o
        JOIN order_lines ol ON ol.order_id = o.id
        WHERE {' AND '.join(w)}
        GROUP BY o.id, o.order_no, o.patient_name, o.party_name, o.order_type, o.created_at
        ORDER BY o.created_at DESC
        LIMIT 200
    """, p)


# ── Billing gate (called from billing_hub) ────────────────────────────────────

def billing_gate_check(order_ids: list[str]) -> tuple[bool, list[dict]]:
    """
    Check discount status for a list of order IDs before billing.

    Returns:
      (all_ok: bool, issues: list[dict])
      issues is empty when all_ok is True.
      Each issue has: order_no, line_id, product_name, eye_side, flag, note, status
    """
    issues = []
    for oid in (order_ids or []):
        try:
            audit_lines = _audit_order_lines(oid)
            for al in audit_lines:
                if al["status"] in ("MISSED", "CANCELLED", "MISMATCH"):
                    issues.append(al)
        except Exception:
            pass
    return (len(issues) == 0), issues


# ── UI helpers ────────────────────────────────────────────────────────────────

_STATUS_COLOR = {
    "OK":             "#10b981",
    "NO_DISCOUNT":    "#475569",
    "MISSED":         "#ef4444",
    "CANCELLED":      "#f59e0b",
    "MISMATCH":       "#f97316",
    "MARGIN_BLOCKED": "#8b5cf6",
}


def _status_badge(status: str) -> str:
    color = _STATUS_COLOR.get(status, "#64748b")
    return (
        f"<span style='background:{color}22;color:{color};"
        f"font-size:0.65rem;font-weight:700;padding:2px 8px;"
        f"border-radius:4px;white-space:nowrap'>{status}</span>"
    )


def _render_audit_lines(audit_lines: list[dict], order_no: str = ""):
    """Render a table of audit lines."""
    if not audit_lines:
        st.caption("No lines found.")
        return

    # Summary counts
    by_status = {}
    for al in audit_lines:
        by_status[al["status"]] = by_status.get(al["status"], 0) + 1

    _mc = st.columns(len(by_status) or 1)
    for _col, (status, cnt) in zip(_mc, by_status.items()):
        color = _STATUS_COLOR.get(status, "#64748b")
        _col.markdown(
            f"<div style='background:{color}22;border:1px solid {color}44;"
            f"border-radius:6px;padding:6px 10px;text-align:center'>"
            f"<div style='color:{color};font-size:1rem;font-weight:800'>{cnt}</div>"
            f"<div style='color:{color};font-size:0.65rem;font-weight:600'>{status}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin:6px 0'></div>", unsafe_allow_html=True)

    # Column headers
    _hc = st.columns([0.4, 2.5, 0.5, 1, 1, 1, 3])
    for _col, _lbl in zip(_hc, ["","Product","Eye","Gross ₹","Applied ₹","Expected ₹","Status / Note"]):
        _col.markdown(
            f"<div style='font-size:0.65rem;font-weight:700;color:#475569;"
            f"text-transform:uppercase;border-bottom:1px solid #1e3a5f;"
            f"padding-bottom:3px'>{_lbl}</div>",
            unsafe_allow_html=True,
        )

    for _al in audit_lines:
        _rc = st.columns([0.4, 2.5, 0.5, 1, 1, 1, 3])
        color = _STATUS_COLOR.get(_al["status"], "#64748b")
        _rc[0].markdown(
            f"<div style='font-size:1rem;padding-top:4px;text-align:center'>{_al['flag']}</div>",
            unsafe_allow_html=True,
        )
        _rc[1].markdown(
            f"<div style='font-size:0.8rem;color:#e2e8f0;font-weight:600;"
            f"padding-top:4px'>{_hesc.escape(str(_al['product_name']))}</div>",
            unsafe_allow_html=True,
        )
        _rc[2].markdown(
            f"<div style='font-size:0.78rem;color:#94a3b8;padding-top:4px'>"
            f"{str(_al.get('eye_side','') or '').upper()}</div>",
            unsafe_allow_html=True,
        )
        _rc[3].markdown(
            f"<div style='font-size:0.78rem;color:#94a3b8;padding-top:4px'>"
            f"₹{_al['gross']:,.2f}</div>",
            unsafe_allow_html=True,
        )
        _rc[4].markdown(
            f"<div style='font-size:0.82rem;color:{color};font-weight:700;"
            f"padding-top:4px'>₹{_al['applied_amt']:,.2f}</div>",
            unsafe_allow_html=True,
        )
        _rc[5].markdown(
            f"<div style='font-size:0.78rem;color:#94a3b8;padding-top:4px'>"
            f"₹{_al['expected_amt']:,.2f}</div>",
            unsafe_allow_html=True,
        )
        _rc[6].markdown(
            f"<div style='padding-top:2px'>{_status_badge(_al['status'])}"
            f"<span style='font-size:0.7rem;color:#64748b;margin-left:8px'>"
            f"{_hesc.escape(str(_al['note']))}</span></div>",
            unsafe_allow_html=True,
        )

    # Fix actions for MISSED / CANCELLED lines
    _fixable   = [al for al in audit_lines if al["status"] == "MISSED"]
    _cancelled = [al for al in audit_lines if al["status"] == "CANCELLED"]

    if _fixable or _cancelled:
        st.markdown("<div style='margin:8px 0'></div>", unsafe_allow_html=True)
        _fa1, _fa2 = st.columns(2)
        oid = audit_lines[0].get("order_id","")

        if _fixable:
            if _fa1.button(
                f"🔧 Apply Missing Discount ({len(_fixable)} line(s))",
                key=f"da_apply_{oid[:8]}",
                use_container_width=True, type="primary",
            ):
                try:
                    from modules.pricing.discount_flow import reinstate_order_discount
                    summary = reinstate_order_discount(oid)
                    st.success(
                        f"✅ Discount applied — "
                        f"₹{summary['discount']:,.2f} on ₹{summary['gross']:,.2f}"
                    )
                    st.rerun()
                except Exception as _fe:
                    st.error(f"Apply failed: {_fe}")

        if _cancelled:
            if _fa2.button(
                f"↩ Reinstate Cancelled Discount ({len(_cancelled)} line(s))",
                key=f"da_reinstate_{oid[:8]}",
                use_container_width=True,
            ):
                try:
                    from modules.pricing.discount_flow import reinstate_order_discount
                    summary = reinstate_order_discount(oid)
                    st.success(
                        f"✅ Discount reinstated — ₹{summary['discount']:,.2f}"
                    )
                    st.rerun()
                except Exception as _re:
                    st.error(f"Reinstate failed: {_re}")


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE UI
# ═══════════════════════════════════════════════════════════════════════════════

def render_discount_audit():
    st.markdown(
        "<div style='background:#0a1628;border:1px solid #1e3a5f;"
        "border-left:4px solid #f59e0b;border-radius:8px;"
        "padding:10px 16px;margin-bottom:14px'>"
        "<span style='color:#fbbf24;font-size:1rem;font-weight:800'>"
        "🔍 Discount Audit</span>"
        "<span style='color:#475569;font-size:0.78rem;margin-left:10px'>"
        "Verify discount application — per order or batch leakage report. "
        "Fix missed or cancelled discounts before billing.</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    _tab = st.radio(
        "Audit mode",
        ["📋 Order Audit", "📊 Leakage Report", "📜 Decision Log"],
        horizontal=True, key="da_tab", label_visibility="collapsed",
    )

    # ── TAB 1: Order Audit ───────────────────────────────────────────────────
    if _tab == "📋 Order Audit":
        with st.container(border=True):
            _oa1, _oa2 = st.columns([5, 1])
            _ono = _oa1.text_input(
                "Order No", placeholder="e.g. R/2627/0120",
                key="da_ono", label_visibility="collapsed",
            )
            _go  = _oa2.button("Audit", key="da_go", use_container_width=True,
                               type="primary")

        if not (_go and _ono.strip()):
            st.caption("Enter an order number and click Audit.")
            return

        # Resolve order_no → order_id
        _ord_rows = _q(
            "SELECT id::text, order_no, order_type, party_id::text FROM orders "
            "WHERE LOWER(order_no) = LOWER(%(n)s) LIMIT 1",
            {"n": _ono.strip()},
        )
        if not _ord_rows:
            st.error(f"Order **{_ono.strip()}** not found.")
            return

        _ord = _ord_rows[0]
        _oid = _ord["id"]

        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
            f"border-radius:6px;padding:8px 14px;margin-bottom:8px'>"
            f"<span style='color:#a5b4fc;font-weight:700'>{_ord['order_no']}</span>"
            f"<span style='color:#475569;font-size:0.78rem;margin-left:10px'>"
            f"{str(_ord.get('order_type','?')).upper()}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        with st.spinner("Running discount audit…"):
            _audit_lines = _audit_order_lines(_oid)

        if not _audit_lines:
            st.info("No billable lines found on this order.")
            return

        _render_audit_lines(_audit_lines, _ord["order_no"])

        # Show decision log if table exists
        _decisions = _q_safe("""
            SELECT applied_rule_name, discount_amount, net_amount,
                   margin_pct, margin_status, created_at::text
            FROM discount_decisions
            WHERE invoice_id = %(oid)s OR line_id LIKE %(lid)s
            ORDER BY created_at DESC LIMIT 20
        """, {"oid": _oid, "lid": f"{_oid}%"})
        if _decisions:
            with st.expander("📜 Decision Log (discount_decisions table)"):
                for _d in _decisions:
                    st.caption(
                        f"{_d.get('applied_rule_name','—')} · "
                        f"₹{float(_d.get('discount_amount',0)):,.2f} off · "
                        f"Net ₹{float(_d.get('net_amount',0)):,.2f} · "
                        f"Margin {float(_d.get('margin_pct') or 0):.1f}% · "
                        f"{str(_d.get('created_at',''))[:16]}"
                    )

    # ── TAB 2: Leakage Report ────────────────────────────────────────────────
    elif _tab == "📊 Leakage Report":
        with st.container(border=True):
            _lf1, _lf2, _lf3, _lf4, _lf5 = st.columns([2, 2, 2, 1.5, 1])
            _dfrom = _lf1.date_input(
                "From",
                value=_dt.date.today() - _dt.timedelta(days=30),
                key="da_lfrom", label_visibility="collapsed",
                format="DD/MM/YYYY",
            )
            _dto = _lf2.date_input(
                "To", value=_dt.date.today(),
                key="da_lto", label_visibility="collapsed",
                format="DD/MM/YYYY",
            )

            # Party filter
            _parties = _q_safe(
                "SELECT id::text AS id, party_name FROM parties "
                "WHERE UPPER(COALESCE(party_type,'')) NOT IN ('RETAIL','PATIENT') "
                "AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name"
            )
            _party_opts = {"": "All Parties"}
            _party_opts.update({p["id"]: p["party_name"] for p in _parties})
            _sel_party = _lf3.selectbox(
                "Party", list(_party_opts.keys()),
                format_func=lambda x: _party_opts.get(x, x),
                key="da_lparty", label_visibility="collapsed",
            )
            _min_gross = _lf4.number_input(
                "Min Gross ₹", value=0.0, step=100.0,
                key="da_lmin", label_visibility="collapsed",
            )
            _lrun = _lf5.button("Run", key="da_lrun", use_container_width=True,
                                type="primary")

        if not _lrun:
            st.caption("Set filters and click Run to generate the leakage report.")
            return

        with st.spinner("Scanning orders for discount leakage…"):
            _leak_rows = _leakage_query(_dfrom, _dto, _sel_party, _min_gross)

        if not _leak_rows:
            st.success("✅ No orders with zero discount found in this period.")
            return

        # Summary metrics
        _total_gross = sum(float(r.get("gross_value",0)) for r in _leak_rows)
        _m1, _m2, _m3 = st.columns(3)
        _m1.metric("Orders with zero discount", len(_leak_rows))
        _m2.metric("Total gross value", f"₹{_total_gross:,.0f}")
        _m3.metric("Avg per order", f"₹{_total_gross/max(len(_leak_rows),1):,.0f}")

        st.markdown("---")
        st.markdown(
            "<div style='color:#fbbf24;font-size:0.78rem;margin-bottom:6px'>"
            "⚠️ These orders have at least one line with discount=0. "
            "Click any order to run a full audit and apply missing discounts.</div>",
            unsafe_allow_html=True,
        )

        for _lr in _leak_rows:
            _loid  = _lr["order_id"]
            _lono  = _lr.get("order_no","—")
            _lpty  = _hesc.escape(str(_lr.get("party_name","—")))
            _ldate = str(_lr.get("order_date",""))[:10]
            _lgross= float(_lr.get("gross_value",0))
            _llines= int(_lr.get("zero_disc_lines",0))

            _lrow1, _lrow2 = st.columns([6, 1])
            with _lrow1:
                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                    f"border-left:3px solid #f59e0b;border-radius:6px;"
                    f"padding:7px 12px;margin-bottom:3px'>"
                    f"<span style='color:#fbbf24;font-weight:700;font-family:monospace'>{_lono}</span>"
                    f"<span style='color:#94a3b8;font-size:0.73rem;margin-left:10px'>"
                    f"{_lpty} · {_ldate} · {_llines} line(s) · "
                    f"<b style='color:#e2e8f0'>₹{_lgross:,.2f}</b></span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            with _lrow2:
                if st.button("Audit", key=f"da_laudit_{_loid[:8]}",
                             use_container_width=True):
                    st.session_state["da_ono"] = _lono
                    st.session_state["da_tab"] = "📋 Order Audit"
                    st.rerun()

    # ── TAB 3: Decision Log ──────────────────────────────────────────────────
    elif _tab == "📜 Decision Log":
        # Check if table exists
        _tbl_exists = _q_safe("""
            SELECT 1 FROM information_schema.tables
            WHERE table_name='discount_decisions' LIMIT 1
        """)
        if not _tbl_exists:
            st.info(
                "The `discount_decisions` table does not exist yet. "
                "It is created automatically when the DecisionLogger is first used."
            )
            return

        with st.container(border=True):
            _dl1, _dl2, _dl3 = st.columns([3, 2, 1])
            _dl_party = _dl1.text_input(
                "Party / Product", placeholder="Filter by rule name or party",
                key="da_dlf", label_visibility="collapsed",
            )
            _dl_from = _dl2.date_input(
                "From", value=_dt.date.today() - _dt.timedelta(days=7),
                key="da_dlfrom", label_visibility="collapsed",
                format="DD/MM/YYYY",
            )
            _dl_run = _dl3.button("Load", key="da_dlrun", use_container_width=True)

        if not _dl_run:
            st.caption("Set filters and click Load.")
            return

        _dl_w = ["dd.created_at::date >= %(df)s"]
        _dl_p = {"df": str(_dl_from)}
        if _dl_party.strip():
            _dl_w.append(
                "(LOWER(COALESCE(dd.applied_rule_name,'')) LIKE %(flt)s "
                "OR LOWER(COALESCE(dd.party_id::text,'')) LIKE %(flt)s)"
            )
            _dl_p["flt"] = f"%{_dl_party.strip().lower()}%"

        _dl_rows = _q_safe(f"""
            SELECT
                dd.applied_rule_name,
                dd.applied_rule_type,
                dd.discount_amount,
                dd.net_amount,
                dd.gross_amount,
                dd.margin_pct,
                dd.margin_status,
                dd.rules_evaluated_count,
                dd.created_at::text
            FROM discount_decisions dd
            WHERE {' AND '.join(_dl_w)}
            ORDER BY dd.created_at DESC
            LIMIT 200
        """, _dl_p)

        if not _dl_rows:
            st.info("No decision records found.")
            return

        # Aggregate by rule
        _rule_stats: dict = {}
        for _dr in _dl_rows:
            _rn = str(_dr.get("applied_rule_name") or "no_rule")
            if _rn not in _rule_stats:
                _rule_stats[_rn] = {"fires":0,"total_disc":0.0,"avg_margin":[],"hard_stops":0}
            _rule_stats[_rn]["fires"]      += 1
            _rule_stats[_rn]["total_disc"] += float(_dr.get("discount_amount") or 0)
            if _dr.get("margin_pct"):
                _rule_stats[_rn]["avg_margin"].append(float(_dr["margin_pct"]))
            if str(_dr.get("margin_status","")).lower() == "hard_stop":
                _rule_stats[_rn]["hard_stops"] += 1

        st.markdown("**Rule effectiveness summary**")
        for _rn, _rs in sorted(_rule_stats.items(), key=lambda x: -x[1]["fires"]):
            _avg_m = (
                sum(_rs["avg_margin"]) / len(_rs["avg_margin"])
                if _rs["avg_margin"] else None
            )
            _color = "#10b981" if _rn != "no_rule" else "#ef4444"
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                f"border-left:3px solid {_color};border-radius:5px;"
                f"padding:6px 12px;margin-bottom:3px;font-size:0.78rem'>"
                f"<b style='color:{_color}'>{_hesc.escape(_rn)}</b>"
                f"<span style='color:#94a3b8;margin-left:12px'>"
                f"Fired {_rs['fires']}× · "
                f"Total discount ₹{_rs['total_disc']:,.2f}"
                + (f" · Avg margin {_avg_m:.1f}%" if _avg_m else "")
                + (f" · <span style='color:#ef4444'>{_rs['hard_stops']} hard stops</span>"
                   if _rs["hard_stops"] else "")
                + f"</span></div>",
                unsafe_allow_html=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# BILLING GATE WIDGET (called from billing_hub.py)
# ═══════════════════════════════════════════════════════════════════════════════

def render_billing_gate(order_ids: list[str]) -> bool:
    """
    Render discount gate check inline in billing_hub.
    Returns True if billing should proceed, False if blocked.

    Rules:
      MISSED    → warning + require confirmation (not hard block)
      CANCELLED → warning + require confirmation
      MISMATCH  → info only, does not block
      OK        → proceed silently
    """
    if not order_ids:
        return True

    with st.spinner("Checking discount status…"):
        _all_ok, _issues = billing_gate_check(order_ids)

    if _all_ok:
        return True

    # Group by severity
    _missed    = [i for i in _issues if i["status"] == "MISSED"]
    _cancelled = [i for i in _issues if i["status"] == "CANCELLED"]
    _mismatch  = [i for i in _issues if i["status"] == "MISMATCH"]

    if _missed or _cancelled:
        st.markdown(
            f"<div style='background:#1a0a0a;border:2px solid #ef4444;"
            f"border-radius:8px;padding:10px 14px;margin:8px 0'>"
            f"<div style='color:#f87171;font-weight:800;font-size:0.88rem'>"
            f"⚠️ Discount Issue Detected</div>"
            f"<div style='color:#fca5a5;font-size:0.78rem;margin-top:4px'>"
            + (f"{len(_missed)} line(s) have missed discount (₹"
               f"{sum(i['expected_amt'] for i in _missed):,.2f} expected but not applied). "
               if _missed else "")
            + (f"{len(_cancelled)} line(s) had discount manually cancelled."
               if _cancelled else "")
            + f"</div></div>",
            unsafe_allow_html=True,
        )

        for _iss in _missed + _cancelled:
            st.caption(
                f"{_iss['flag']} {_iss['order_no']} · "
                f"{_iss['eye_side']} {_iss['product_name']} · "
                f"{_iss['note']}"
            )

        _bg1, _bg2, _bg3 = st.columns(3)

        # Fix and re-check
        if _bg1.button("🔧 Fix Discounts First", key="bg_fix",
                       use_container_width=True, type="primary"):
            for _iss in _missed + _cancelled:
                try:
                    from modules.pricing.discount_flow import reinstate_order_discount
                    reinstate_order_discount(_iss["order_id"])
                except Exception:
                    pass
            st.success("✅ Discounts fixed — proceed with billing.")
            st.rerun()

        # Proceed anyway
        _confirmed = _bg2.checkbox(
            "Proceed without discount", key="bg_confirm"
        )
        if _confirmed:
            st.warning(
                "⚠️ Billing will proceed without discount. "
                "This cannot be undone after challan is raised."
            )
            return True

        if _bg3.button("↩ Cancel", key="bg_cancel", use_container_width=True):
            st.rerun()

        return False  # Block until confirmed or fixed

    # Mismatch only → info, don't block
    if _mismatch:
        st.info(
            f"ℹ️ {len(_mismatch)} line(s) have a discount amount that differs from "
            f"current rules (rule may have changed since order was punched). "
            f"Billing will use the amount already applied to the order."
        )
    return True
