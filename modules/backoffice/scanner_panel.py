"""
modules/backoffice/scanner_panel.py
=====================================
Universal Scanner Panel — scan two barcodes to update order status.

Flow:
  1. Scan order barcode (order_no printed on job card / challan)
  2. Scan stage card   (laminated card per production stage)
  → Status updates instantly, WhatsApp fires if configured

Stage card barcodes (print and laminate one per stage):
  STAGE:CONFIRMED        → Order collected ✓
  STAGE:IN_PRODUCTION    → Sent to production
  STAGE:SUPPLIER_PO      → Sent to supplier
  STAGE:READY            → Ready for delivery
  STAGE:DISPATCHED       → Dispatched
  STAGE:DELIVERED        → Delivered
  STAGE:CLOSED           → Closed

Also handles:
  - Product scan  → shows product details
  - Party scan    → shows party account
"""

import streamlit as st

# ── Stage card definitions — print barcodes for these ─────────────────────────
STAGE_CARDS = [
    {"barcode": "STAGE:CONFIRMED",     "label": "Collected ✓",  "icon": "✅", "status": "CONFIRMED",     "color": "#6366f1"},
    {"barcode": "STAGE:IN_PRODUCTION", "label": "Production",   "icon": "⚙️", "status": "IN_PRODUCTION", "color": "#8b5cf6"},
    {"barcode": "STAGE:SUPPLIER_PO",   "label": "Supplier PO",  "icon": "🏭", "status": "IN_PRODUCTION", "color": "#a855f7"},
    {"barcode": "STAGE:READY",         "label": "Ready",        "icon": "📦", "status": "READY",         "color": "#10b981"},
    {"barcode": "STAGE:BILLED",        "label": "Billed",       "icon": "🧾", "status": "BILLED",        "color": "#059669"},
    {"barcode": "STAGE:DISPATCHED",    "label": "Dispatched",   "icon": "🚚", "status": "DISPATCHED",    "color": "#0891b2"},
    {"barcode": "STAGE:DELIVERED",     "label": "Delivered",    "icon": "✅", "status": "DELIVERED",     "color": "#10b981"},
    {"barcode": "STAGE:CLOSED",        "label": "Closed",       "icon": "🔒", "status": "CLOSED",        "color": "#334155"},
]
STAGE_MAP = {s["barcode"].upper(): s for s in STAGE_CARDS}


def _resolve_scan(code: str) -> dict:
    """Classify a scanned barcode — stage card, order, product, or party."""
    code = code.strip().upper()

    # Stage card
    if code in STAGE_MAP:
        return {"type": "stage", "data": STAGE_MAP[code]}

    # Order
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT id::text AS order_id, order_no, status,
                   COALESCE(party_name,'')   AS party_name,
                   COALESCE(patient_name,'') AS patient_name,
                   COALESCE(total_value,0)   AS total_value
            FROM orders
            WHERE UPPER(TRIM(order_no)) = %s
               OR UPPER(TRIM(COALESCE(customer_order_no,''))) = %s
            LIMIT 1
        """, (code, code))
        if rows:
            return {"type": "order", "data": rows[0]}
    except Exception:
        pass

    # Product / party via universal lookup
    try:
        from modules.ui_product_selector import scan_any
        r = scan_any(code)
        if r["type"] in ("product", "party"):
            return r
    except Exception:
        pass

    return {"type": "unknown", "code": code}


def _do_status_update(order: dict, stage: dict) -> bool:
    """Apply status change via scanner — records scan_source=SCANNER + timing."""
    try:
        from modules.backoffice.backoffice import _save_status_change
        from modules.security.roles import current_user_name
        try:
            user = current_user_name()
        except Exception:
            user = st.session_state.get("user", "scanner")
            if not isinstance(user, str):
                user = user.get("name", "scanner")

        new_status = stage["status"]
        success = _save_status_change(
            order,
            new_status,
            scan_source  = "SCANNER",
            scanned_by   = user,
        )
        return success
    except Exception as ex:
        st.error(f"Status update failed: {ex}")
        return False


def _render_stage_timeline(order_id: str):
    """Show full stage history with entry time, exit time, duration for one order."""
    if not order_id:
        return
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                from_status,
                to_status,
                changed_at,
                stage_entered_at,
                stage_exited_at,
                duration_minutes,
                scan_source,
                COALESCE(scanned_by_user, changed_by_name, '—') AS done_by,
                remarks
            FROM order_status_history
            WHERE order_id = %s::uuid
            ORDER BY changed_at ASC
        """, (order_id,)) or []
    except Exception as ex:
        st.caption(f"Timeline unavailable: {ex}")
        return

    if not rows:
        st.caption("No history yet")
        return

    STATUS_ICONS = {
        "PENDING":"⏳","CONFIRMED":"✅","IN_PRODUCTION":"⚙️",
        "READY":"📦","BILLED":"🧾","DISPATCHED":"🚚",
        "DELIVERED":"✅","CLOSED":"🔒",
    }
    SOURCE_ICONS = {"SCANNER":"📷","DASHBOARD":"🖥️","SYSTEM":"🤖","API":"🔌"}

    for r in rows:
        icon   = STATUS_ICONS.get(r.get("to_status",""), "•")
        src    = SOURCE_ICONS.get(r.get("scan_source",""), "•")
        dur    = r.get("duration_minutes")
        dur_str = f"{dur:.0f} min" if dur else "—"

        entered = str(r.get("stage_entered_at") or r.get("changed_at",""))[:16]
        exited  = str(r.get("stage_exited_at",""))[:16] if r.get("stage_exited_at") else "current"

        st.markdown(
            f"<div style='display:flex;align-items:flex-start;gap:10px;"
            f"padding:6px 0;border-bottom:0.5px solid #1e293b'>"
            f"<span style='font-size:16px;min-width:20px'>{icon}</span>"
            f"<div style='flex:1'>"
            f"<b style='color:#e2e8f0;font-size:12px'>{r.get('to_status','')}</b>"
            f"<span style='color:#64748b;font-size:10px;margin-left:8px'>"
            f"{src} {r.get('done_by','')}</span><br>"
            f"<span style='color:#475569;font-size:10px'>"
            f"In: {entered} → Out: {exited}</span>"
            f"</div>"
            f"<div style='text-align:right;min-width:60px'>"
            f"<span style='color:#22d3ee;font-size:11px;font-weight:700'>{dur_str}</span>"
            f"</div>"
            f"</div>",
            unsafe_allow_html=True
        )

    # Total time
    try:
        first_at = rows[0].get("stage_entered_at") or rows[0].get("changed_at")
        last_at  = rows[-1].get("changed_at")
        if first_at and last_at:
            from datetime import datetime
            def _parse(dt):
                if isinstance(dt, str):
                    return datetime.fromisoformat(dt.replace("Z",""))
                return dt
            total_min = (_parse(last_at) - _parse(first_at)).total_seconds() / 60
            st.markdown(
                f"<div style='padding:6px 0;color:#94a3b8;font-size:11px;text-align:right'>"
                f"Total time in production: <b style='color:#22d3ee'>{total_min:.0f} min"
                f" ({total_min/60:.1f} hrs)</b></div>",
                unsafe_allow_html=True
            )
    except Exception:
        pass


def render_scanner_panel():
    """Full scanner panel — scan order + stage to update status."""
    st.markdown(
        "<div style='background:#0f172a;border-left:4px solid #38bdf8;"
        "padding:10px 16px;border-radius:6px;margin-bottom:12px'>"
        "<b style='color:#38bdf8;font-size:1rem'>📷 Scanner Panel</b>"
        "<span style='color:#94a3b8;font-size:0.78rem;margin-left:10px'>"
        "Scan order barcode → scan stage card → status updates instantly</span>"
        "</div>",
        unsafe_allow_html=True
    )

    # ── Two-scan state machine ────────────────────────────────────────────────
    # State: waiting_order → waiting_stage → confirm → done
    state     = st.session_state.get("scanner_state", "waiting_order")
    held_order = st.session_state.get("scanner_held_order")
    held_stage = st.session_state.get("scanner_held_stage")

    # ── Scanner input — camera or manual ────────────────────────────────────
    sc1, sc2 = st.columns([4, 1])
    with sc2:
        if st.button("🔄 Reset", key="scanner_reset", use_container_width=True):
            for k in ["scanner_state","scanner_held_order","scanner_held_stage",
                      "scanner_panel_val","scanner_panel_input","cs_sp_result"]:
                st.session_state.pop(k, None)
            st.rerun()

    with sc1:
        st.caption(
            "Scan order barcode first, then scan a stage card"
            if state == "waiting_order"
            else "Now scan the stage card (Ready / Production / Dispatched…)"
        )

    # Camera scanner (mobile) with manual fallback
    try:
        from modules.printing.camera_scanner import render_camera_scanner_section
        scanned_val = render_camera_scanner_section(key="sp", height=320)
    except Exception as _ce:
        st.warning(f"Camera module unavailable: {_ce}")
        scanned_val = st.text_input(
            "Barcode", placeholder="Type barcode and press Enter",
            key="scanner_panel_input", label_visibility="collapsed",
        ).strip().upper()

    # Strictly validate — must be a non-empty string, never a Streamlit object
    scan_val = scanned_val if (
        isinstance(scanned_val, str) and scanned_val.strip()
    ) else None

    # ── Process scan ──────────────────────────────────────────────────────────
    if scan_val:
        result = _resolve_scan(scan_val)

        if result["type"] == "stage" and state == "waiting_order":
            # Stage scanned first — hold it, ask for order next
            st.session_state["scanner_held_stage"] = result["data"]
            st.session_state["scanner_state"]      = "waiting_order_after_stage"
            st.info(f"Stage card: **{result['data']['icon']} {result['data']['label']}** — now scan the order barcode")
            st.rerun()

        elif result["type"] == "order":
            st.session_state["scanner_held_order"] = result["data"]
            if held_stage or state == "waiting_order_after_stage":
                st.session_state["scanner_state"] = "confirm"
            else:
                st.session_state["scanner_state"] = "waiting_stage"
            st.rerun()

        elif result["type"] == "stage":
            st.session_state["scanner_held_stage"] = result["data"]
            if held_order:
                st.session_state["scanner_state"] = "confirm"
            else:
                st.session_state["scanner_state"] = "waiting_order"
            st.rerun()

        elif result["type"] == "product":
            d = result["data"]
            st.info(
                f"📦 **{d.get('product_name','')}** | {d.get('brand','')} | "
                f"₹{float(d.get('mrp',0)):.0f}"
                + (f" | SPH {d.get('sph','')} CYL {d.get('cyl','')}" if d.get('sph') else "")
            )

        elif result["type"] == "party":
            d = result["data"]
            st.info(f"🏢 **{d.get('party_name','')}** | {d.get('party_type','')} | {d.get('mobile','')}")

        else:
            st.warning(f"⚠️ Barcode **{scan_val}** not recognised")

    # ── Show current state ────────────────────────────────────────────────────
    state = st.session_state.get("scanner_state", "waiting_order")
    held_order = st.session_state.get("scanner_held_order")
    held_stage = st.session_state.get("scanner_held_stage")

    if held_order:
        _s = held_order.get("status","")
        _color = {"PENDING":"#f59e0b","IN_PRODUCTION":"#8b5cf6",
                  "READY":"#10b981","DISPATCHED":"#0891b2"}.get(_s, "#94a3b8")
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
            f"border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<b style='color:#e2e8f0'>📋 {held_order['order_no']}</b> "
            f"<span style='color:#94a3b8;font-size:0.8rem'>"
            f"{held_order.get('party_name','')} | {held_order.get('patient_name','')}</span> "
            f"<span style='color:{_color};font-weight:700;font-size:0.8rem'>[{_s}]</span>"
            f"</div>",
            unsafe_allow_html=True
        )

    if held_stage:
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
            f"border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<b style='color:#e2e8f0'>{held_stage['icon']} Stage: {held_stage['label']}</b> "
            f"<span style='color:#94a3b8;font-size:0.8rem'>→ {held_stage['status']}</span>"
            f"</div>",
            unsafe_allow_html=True
        )

    # ── Confirm and apply ─────────────────────────────────────────────────────
    if state == "confirm" and held_order and held_stage:
        _cur = held_order.get("status","")
        _new = held_stage["status"]

        st.markdown("---")
        st.markdown(
            f"### Update Order Status\n"
            f"**{held_order['order_no']}** — {held_order.get('party_name','')}  \n"
            f"**{_cur}** → **{_new}**"
        )

        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button(f"✅ Confirm: {held_stage['icon']} {held_stage['label']}",
                         type="primary", key="scanner_confirm", use_container_width=True):
                ok = _do_status_update(held_order, held_stage)
                if ok:
                    st.success(
                        f"✅ **{held_order['order_no']}** → **{_new}** "
                        f"updated successfully"
                    )
                    # Keep order for timeline display
                    st.session_state["scanner_last_order"] = held_order
                    for k in ["scanner_state","scanner_held_order","scanner_held_stage"]:
                        st.session_state.pop(k, None)
                    st.rerun()
        with cc2:
            if st.button("❌ Cancel", key="scanner_cancel", use_container_width=True):
                for k in ["scanner_state","scanner_held_order","scanner_held_stage"]:
                    st.session_state.pop(k, None)
                st.rerun()

    # ── Stage timeline for last scanned order ────────────────────────────────
    if held_order or st.session_state.get("scanner_last_order"):
        _tl_order = held_order or st.session_state.get("scanner_last_order")
        with st.expander(f"📊 Stage Timeline — {_tl_order.get('order_no','')}", expanded=True):
            _render_stage_timeline(_tl_order.get("order_id",""))

    # ── Stage cards reference ─────────────────────────────────────────────────
    with st.expander("🖨️ Stage Card Barcodes — Print & Laminate", expanded=False):
        st.caption("Print these barcodes on cards, laminate them. One card per stage.")
        cols = st.columns(4)
        for i, card in enumerate(STAGE_CARDS):
            with cols[i % 4]:
                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                    f"border-radius:8px;padding:10px;text-align:center;margin-bottom:8px'>"
                    f"<div style='font-size:1.5rem'>{card['icon']}</div>"
                    f"<div style='color:#e2e8f0;font-weight:700;font-size:0.85rem'>{card['label']}</div>"
                    f"<div style='color:#64748b;font-size:0.65rem;margin-top:4px;"
                    f"font-family:monospace'>{card['barcode']}</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )
        st.info(
            "Barcode content is the text in grey (e.g. `STAGE:CONFIRMED`). "
            "Generate barcodes at barcode.tec-it.com or use any Code128 barcode generator. "
            "Print → laminate → done."
        )
