"""
modules/founder/owner_dashboard.py  — Intelligence Layer
"""
import streamlit as st
from datetime import date, timedelta


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


def _section(title):
    st.markdown(
        "<div style='background:#0f172a;border-left:3px solid #6366f1;"
        "padding:6px 14px;border-radius:0 6px 6px 0;margin:16px 0 10px'>"
        "<span style='color:#a5b4fc;font-weight:800;font-size:0.82rem;"
        "letter-spacing:.06em;text-transform:uppercase'>" + title + "</span></div>",
        unsafe_allow_html=True,
    )


def render_owner_dashboard():
    st.markdown(
        "<div style='display:flex;align-items:center;gap:10px;margin-bottom:10px'>"
        "<span style='background:#7c3aed;color:#fff;font-size:0.7rem;font-weight:800;"
        "padding:3px 12px;border-radius:20px'>\U0001f451 OWNER DASHBOARD</span>"
        "<span style='color:#64748b;font-size:0.72rem'>Intelligence layer</span></div>",
        unsafe_allow_html=True,
    )

    today = date.today()
    dc1, dc2 = st.columns([2, 5])
    with dc1:
        _period = st.selectbox("Period",
            ["Today", "This Week", "This Month", "Custom"],
            label_visibility="collapsed", key="owner_period")
    if _period == "Today":
        d_from, d_to = today, today
    elif _period == "This Week":
        d_from = today - timedelta(days=today.weekday()); d_to = today
    elif _period == "This Month":
        d_from = today.replace(day=1); d_to = today
    else:
        with dc2:
            rng = st.date_input("Range",
                value=(today - timedelta(days=30), today),
                label_visibility="collapsed", key="owner_range")
            d_from = rng[0] if len(rng) == 2 else today
            d_to   = rng[1] if len(rng) == 2 else today

    # Alert detection
    _import_error = None
    alerts = []
    try:
        from modules.founder.erp_alerts import (
            run_alert_detection, get_active_alerts, dismiss_alert, wa_alert_link)
        with st.spinner("Scanning for alerts..."):
            run_alert_detection()
            alerts = get_active_alerts()
    except Exception as _ae:
        _import_error = str(_ae)
        import traceback as _tb
        _import_error += "\n" + _tb.format_exc()

    if _import_error:
        st.error("⚠️ Alert engine error — dashboard running without alerts")
        with st.expander("Show error detail"):
            st.code(_import_error)

    try:
        from modules.settings.shop_master import get_shop_info
        _shop = get_shop_info() or {}
        _owner_mob = str(_shop.get("owner_mobile") or _shop.get("mobile") or "")
    except Exception:
        _owner_mob = ""

    # Debug: check if views exist
    with st.expander("🔧 System Check", expanded=False):
        _view_check = [
            "v_staff_performance", "v_reorder_alert", "v_fast_moving",
            "v_dead_stock", "v_ar_aging", "v_po_overdue", "v_alert_low_margin",
        ]
        for _vn in _view_check:
            try:
                _vr = _q(f"SELECT 1 FROM {_vn} LIMIT 1")
                st.markdown(f"✅ `{_vn}`")
            except Exception as _ve:
                st.markdown(f"❌ `{_vn}` — {_ve}")

        _snap_test = _q(
            "SELECT COUNT(*) AS cnt FROM orders WHERE COALESCE(is_deleted,FALSE)=FALSE")
        st.markdown(f"📦 Orders in DB: {_snap_test[0]['cnt'] if _snap_test else '?'}")

    # 1. ALERTS
    _section("\U0001f6a8 Active Alerts")
    critical = [a for a in alerts if a.get("severity") == "CRITICAL"]
    warnings  = [a for a in alerts if a.get("severity") == "WARN"]
    if not alerts:
        st.success("\u2705 No active alerts")
    else:
        m1,m2,m3 = st.columns(3)
        m1.metric("\U0001f534 Critical", len(critical),
            delta="Action needed" if critical else None, delta_color="inverse")
        m2.metric("\U0001f7e1 Warnings", len(warnings))
        m3.metric("Total", len(alerts))

    _SCOL = {"CRITICAL":"#ef4444","WARN":"#f59e0b","INFO":"#3b82f6"}
    _ICON = {
        "LOW_MARGIN":"\U0001f4c9","DISCOUNT_ABUSE":"\U0001f39f\ufe0f",
        "PRICE_OUTLIER":"\U0001f50d","CASH_GAP":"\U0001f4b8",
        "STOCK_LOW":"\U0001f4e6","PO_OVERDUE":"\U0001f4cb","TALLY_UNSYNCED":"\U0001f4e4",
    }

    for a in alerts[:20]:
        col  = _SCOL.get(a.get("severity","WARN"), "#64748b")
        icon = _ICON.get(a.get("alert_type",""), "\u26a0\ufe0f")
        left, right = st.columns([8,1])
        with left:
            st.markdown(
                "<div style='background:#0f172a;border-left:4px solid " + col + ";"
                "border-radius:0 6px 6px 0;padding:8px 14px;margin:3px 0'>"
                "<div style='color:" + col + ";font-weight:700;font-size:0.8rem'>"
                + icon + " " + str(a.get("title","")) + "  "
                "<span style='color:#64748b;font-size:0.72rem;font-weight:400'>"
                + str(a.get("ref_value","")) + "</span></div>"
                "<div style='color:#94a3b8;font-size:0.72rem'>"
                + str(a.get("detail","")) + "</div></div>",
                unsafe_allow_html=True)
        with right:
            b1,b2 = st.columns(2)
            if _owner_mob:
                try:
                    url = wa_alert_link(a, _owner_mob)
                    b1.markdown(
                        "<a href='" + url + "' target='_blank' style='display:block;"
                        "background:#25d366;color:#fff;padding:5px;border-radius:4px;"
                        "text-align:center;font-size:0.72rem;text-decoration:none;"
                        "margin-top:6px'>\U0001f4f2</a>",
                        unsafe_allow_html=True)
                except Exception:
                    pass
            aid = str(a.get("id",""))
            if b2.button("\u2713", key="dm_" + aid, help="Dismiss"):
                try: dismiss_alert(aid); st.rerun()
                except Exception: pass

    # 2. SNAPSHOT
    _section("\U0001f4ca Business Snapshot")
    snap = _q(
        "SELECT COUNT(DISTINCT id) AS inv, COALESCE(SUM(grand_total),0) AS invoiced "
        "FROM invoices WHERE created_at::date BETWEEN %(f)s AND %(t)s "
        "AND status NOT IN ('CANCELLED','VOID')",
        {"f":str(d_from),"t":str(d_to)})
    ms = _q(
        "SELECT COALESCE(SUM(ol.total_price),0) AS rev, "
        "COALESCE(SUM(ol.cost_price*ol.quantity),0) AS cost "
        "FROM order_lines ol JOIN orders o ON o.id=ol.order_id "
        "WHERE o.created_at::date BETWEEN %(f)s AND %(t)s "
        "AND COALESCE(ol.is_deleted,FALSE)=FALSE",
        {"f":str(d_from),"t":str(d_to)})
    s = snap[0] if snap else {}
    m = ms[0]   if ms   else {}
    rev = float(m.get("rev") or 0)
    cst = float(m.get("cost") or 0)
    pft = rev - cst
    mgn = (pft/rev*100) if rev > 0 else 0
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("\U0001f4b0 Invoiced",    "\u20b9{:,.0f}".format(float(s.get("invoiced") or 0)))
    c2.metric("\U0001f4e6 Invoices",    str(int(s.get("inv") or 0)))
    c3.metric("\U0001f4c8 Revenue",     "\u20b9{:,.0f}".format(rev))
    c4.metric("\U0001f49a Profit",      "\u20b9{:,.0f}".format(pft))
    c5.metric("\U0001f4ca Margin",      "{:.1f}%".format(mgn),
        delta="Healthy" if mgn>=20 else ("Low" if mgn>=10 else "Critical"),
        delta_color="normal" if mgn>=20 else "inverse")

    # 3. STAFF
    _section("\U0001f464 Staff Performance")
    for r in _q("SELECT staff_name,orders,revenue,gross_profit,avg_margin_pct,discounts_given FROM v_staff_performance LIMIT 10"):
        gp = float(r.get("gross_profit") or 0)
        mg = float(r.get("avg_margin_pct") or 0)
        dc = int(r.get("discounts_given") or 0)
        mc = "#22c55e" if mg>=20 else ("#f59e0b" if mg>=10 else "#ef4444")
        st.markdown(
            "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:6px;"
            "padding:8px 14px;margin:3px 0;display:flex;justify-content:space-between'>"
            "<span style='color:#e2e8f0;font-weight:700'>" + str(r.get("staff_name","?")) + "</span>"
            "<span style='font-size:0.75rem'>"
            "<span style='color:#60a5fa'>\u20b9{:,.0f} rev</span>  "
            "<span style='color:#4ade80'>\u20b9{:,.0f} profit</span>  "
            "<span style='color:".format(float(r.get("revenue") or 0), gp)
            + mc + "'>{:.0f}% margin</span>".format(mg)
            + ("  <span style='color:#f59e0b'>\u26a0\ufe0f " + str(dc) + " discounts</span>" if dc>2 else "")
            + "</span></div>",
            unsafe_allow_html=True)

    # 4. STOCK
    _section("\U0001f4e6 Stock Intelligence")
    s1,s2,s3 = st.columns(3)
    with s1:
        st.caption("\U0001f534 Reorder Now")
        for r in _q("SELECT product_name, available FROM v_reorder_alert LIMIT 8"):
            av = int(r.get("available") or 0)
            c  = "#ef4444" if av<=0 else "#f59e0b"
            st.markdown("<span style='color:" + c + ";font-size:0.75rem'>" +
                ("\U0001f6ab " if av<=0 else "\u26a0\ufe0f ") +
                str(r.get("product_name",""))[:32] + " \u2014 " + str(av) + "</span>",
                unsafe_allow_html=True)
    with s2:
        st.caption("\U0001f525 Fast Moving 30d")
        for r in _q("SELECT product_name, units_sold FROM v_fast_moving LIMIT 6"):
            st.markdown("<span style='color:#60a5fa;font-size:0.75rem'>\u26a1 " +
                str(r.get("product_name",""))[:32] + " \u2014 " + str(r.get("units_sold","")) + " units</span>",
                unsafe_allow_html=True)
    with s3:
        st.caption("\U0001f422 Dead Stock 60d")
        dead = _q("SELECT product_name, stock_qty, stock_value FROM v_dead_stock LIMIT 6")
        if dead:
            for r in dead:
                st.markdown("<span style='color:#94a3b8;font-size:0.75rem'>\U0001f4a4 " +
                    str(r.get("product_name",""))[:28] + " \u20b9{:,.0f}".format(float(r.get("stock_value") or 0)) +
                    "</span>", unsafe_allow_html=True)
        else:
            st.success("\u2705 None")

    # 5. PROCUREMENT
    _section("\U0001f4cb Procurement")
    pos = _q(
        "SELECT status, COUNT(*) AS cnt, COALESCE(SUM(total_value),0) AS val "
        "FROM supplier_orders WHERE COALESCE(is_deleted,FALSE)=FALSE "
        "AND status NOT IN ('CANCELLED','CLOSED','RECEIVED') GROUP BY status ORDER BY cnt DESC")
    if pos:
        pcols = st.columns(max(len(pos),1))
        for i,r in enumerate(pos):
            pcols[i].metric(str(r["status"]), str(int(r["cnt"])),
                delta="\u20b9{:,.0f}".format(float(r["val"])))
    overdue = _q("SELECT po_no, supplier, days_pending FROM v_po_overdue LIMIT 5")
    if overdue:
        with st.expander("\u26a0\ufe0f " + str(len(overdue)) + " overdue POs"):
            for r in overdue:
                d = int(r.get("days_pending") or 0)
                c = "#ef4444" if d>21 else "#f59e0b"
                st.markdown("<span style='color:" + c + ";font-size:0.78rem'>\U0001f551 " +
                    str(r.get("po_no","")) + " \u00b7 " + str(r.get("supplier","")) +
                    " \u00b7 " + str(d) + "d overdue</span>", unsafe_allow_html=True)

    # 6. AR AGING
    _section("\U0001f4c5 AR Aging")
    aging = _q(
        "SELECT aging_bucket, COUNT(*) AS invoices, ROUND(SUM(balance_due),2) AS balance "
        "FROM v_ar_aging GROUP BY aging_bucket ORDER BY MIN(days_outstanding)")
    if aging:
        _AGC = {"0-30 days":"#22c55e","31-60 days":"#f59e0b","61-90 days":"#f97316","90+ days":"#ef4444"}
        acols = st.columns(len(aging))
        for i,r in enumerate(aging):
            col = _AGC.get(str(r.get("aging_bucket","")), "#64748b")
            with acols[i]:
                st.markdown(
                    "<div style='background:#0f172a;border:1px solid " + col + "44;"
                    "border-radius:8px;padding:10px;text-align:center'>"
                    "<div style='color:" + col + ";font-size:0.7rem;font-weight:700'>" +
                    str(r.get("aging_bucket","")) + "</div>"
                    "<div style='color:#f1f5f9;font-size:1.05rem;font-weight:800'>\u20b9{:,.0f}</div>".format(
                        float(r.get("balance") or 0))
                    + "<div style='color:#64748b;font-size:0.65rem'>" +
                    str(r.get("invoices","")) + " invoices</div></div>",
                    unsafe_allow_html=True)
    else:
        st.success("\u2705 No outstanding receivables")
