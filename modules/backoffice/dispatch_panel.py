"""
dispatch_panel.py
=================
Dispatch UI for billed orders.

RULES:
  1. HARD GATE: No dispatch without at least one active challan or invoice.
     Gate is enforced by logistics_manager.billing_gate_check().
  2. Partial dispatch is supported — operator selects qty per line.
     Order stays DISPATCHED; remaining qty tracked in order_dispatch_lines.
  3. Logistics route is captured (carrier / route_code) — full UI stub.
     Route design window will be added later (placeholder exists).
  4. Order status transitions:
       BILLED → DISPATCHED  (on first dispatch, partial or full)
       DISPATCHED → DELIVERED  (on delivery confirmation when all lines done)

USED BY:
  billing_status_ui.py  — shown after billing documents confirmed
  order_status_window.py — shown in Update Status tab
"""

import streamlit as st
import datetime
from typing import Dict, List

from .logistics_manager import (
    LogisticsRoute,
    billing_gate_check,
    get_billed_lines_for_order,
    get_dispatch_history,
    get_dispatch_summary,
    create_dispatch_event,
    confirm_delivery,
)


# ──────────────────────────────────────────────────────────────
# MAIN ENTRY
# ──────────────────────────────────────────────────────────────

def render_dispatch_panel(order: Dict) -> None:
    """
    Full dispatch panel.
    Always call this AFTER billing documents are confirmed.
    Hard gate is re-checked here as a safety layer.
    """
    order_id = str(order.get("id") or "")
    order_no = order.get("order_no") or "—"
    status   = order.get("status") or "PENDING"

    st.markdown("---")
    st.markdown("### 🚚 Dispatch & Logistics")

    # ── HARD GATE: must have billing document ─────────────────────────────
    is_billed, gate_msg, billing_docs = billing_gate_check(order_id)
    if not is_billed:
        st.error(
            "🔒 **Dispatch is locked.** "
            "Create a Challan or Invoice before dispatching this order."
        )
        st.caption(gate_msg)
        return

    # Show which billing document is covering this dispatch
    _render_billing_docs_badge(billing_docs)

    # ── Dispatch summary bar ───────────────────────────────────────────────
    summary = get_dispatch_summary(order_id)
    _render_dispatch_summary_bar(summary)

    # ── Read-only for terminal states ─────────────────────────────────────
    if status in ("DELIVERED", "CLOSED"):
        _render_dispatch_history(order_id, order_no, read_only=True)
        return

    tab_new, tab_history, tab_deliver = st.tabs([
        "📦 New Dispatch",
        "📋 Dispatch History",
        "✅ Confirm Delivery",
    ])

    with tab_new:
        _render_new_dispatch_form(order, billing_docs, summary)

    with tab_history:
        _render_dispatch_history(order_id, order_no)

    with tab_deliver:
        _render_delivery_confirmation(order_id, order_no)


# ──────────────────────────────────────────────────────────────
# BILLING DOCS BADGE
# ──────────────────────────────────────────────────────────────

def _render_billing_docs_badge(billing_docs: List[Dict]) -> None:
    pills = ""
    for doc in billing_docs:
        t     = doc.get("doc_type", "DOC")
        no    = doc.get("doc_no", "—")
        amt   = float(doc.get("amount") or 0)
        color = "#059669" if t == "INVOICE" else "#0891b2"
        icon  = "🧾" if t == "INVOICE" else "📋"
        pills += (
            f"<span style='background:{color}22;border:1px solid {color}55;"
            f"color:{color};padding:3px 12px;border-radius:12px;"
            f"font-size:0.72rem;font-weight:700;margin-right:6px'>"
            f"{icon} {t} {no}  ₹{amt:,.2f}</span>"
        )
    st.markdown(
        f"<div style='margin-bottom:10px'>"
        f"<span style='color:#64748b;font-size:0.65rem;margin-right:6px'>"
        f"🔓 Billing verified:</span>{pills}</div>",
        unsafe_allow_html=True,
    )


# ──────────────────────────────────────────────────────────────
# SUMMARY BAR
# ──────────────────────────────────────────────────────────────

def _render_dispatch_summary_bar(summary: Dict) -> None:
    billed     = summary.get("total_billed", 0)
    dispatched = summary.get("total_dispatched", 0)
    remaining  = summary.get("total_remaining", 0)
    pct = int(100 * dispatched / billed) if billed else 0

    status_color = (
        "#10b981" if remaining == 0 and billed > 0
        else "#f59e0b" if dispatched > 0
        else "#3b82f6"
    )
    status_label = (
        "✅ Fully Dispatched"
        if remaining == 0 and billed > 0
        else f"⚡ Partial — {remaining} unit(s) still pending"
        if dispatched > 0
        else "📦 Not yet dispatched"
    )

    c1, c2, c3, c4 = st.columns(4)
    for col, val, label, color in [
        (c1, str(billed),     "Billed Qty",    "#3b82f6"),
        (c2, str(dispatched), "Dispatched",    "#8b5cf6"),
        (c3, str(remaining),  "Remaining",     "#f59e0b" if remaining else "#10b981"),
        (c4, f"{pct}%",       "Dispatch %",    status_color),
    ]:
        col.markdown(
            f"<div style='background:#1e293b;border-radius:8px;padding:8px 12px;"
            f"text-align:center;border-top:3px solid {color}'>"
            f"<div style='color:{color};font-size:1.1rem;font-weight:800'>{val}</div>"
            f"<div style='color:#64748b;font-size:0.65rem'>{label}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    st.markdown(
        f"<div style='color:{status_color};font-size:0.78rem;font-weight:600;"
        f"margin:6px 0 10px'>{status_label}</div>",
        unsafe_allow_html=True,
    )


# ──────────────────────────────────────────────────────────────
# NEW DISPATCH FORM
# ──────────────────────────────────────────────────────────────

def _render_new_dispatch_form(
    order: Dict,
    billing_docs: List[Dict],
    summary: Dict,
) -> None:
    order_id = str(order.get("id") or "")
    order_no = order.get("order_no") or "—"

    if summary.get("total_remaining", 0) == 0 and summary.get("total_billed", 0) > 0:
        st.success("✅ All billed quantities have been dispatched.")
        st.caption("Use the **Confirm Delivery** tab to mark delivery received.")
        return

    billed_lines = get_billed_lines_for_order(order_id)
    dispatchable = [l for l in billed_lines if int(l.get("remaining_qty") or 0) > 0]

    if not dispatchable:
        st.info("No lines with pending dispatch qty.")
        return

    st.markdown("#### 🚚 Logistics Route")

    route_labels = LogisticsRoute.labels()
    col_r1, col_r2 = st.columns(2)
    with col_r1:
        selected_route_label = st.selectbox(
            "Route / Carrier *",
            route_labels,
            key="dp_route_label",
            help="Select carrier or delivery method. Additional routes can be configured later.",
        )
        route_code = LogisticsRoute.code_for_label(selected_route_label)
    with col_r2:
        route_info   = LogisticsRoute.get(route_code)
        rt_type      = route_info.get("type", "") if route_info else ""
        needs_tracking = rt_type in ("COURIER", "POSTAL", "TRANSPORT")
        tracking_no  = st.text_input(
            "Tracking / Docket No" + (" *" if needs_tracking else ""),
            key="dp_tracking",
            placeholder="Tracking number" if needs_tracking else "Optional for this route",
        )

    col_c1, col_c2 = st.columns(2)
    with col_c1:
        carrier_name = st.text_input(
            "Carrier / Agent",
            key="dp_carrier",
            placeholder="e.g. Raju courier, vehicle no…",
        )
    with col_c2:
        dispatch_date = st.date_input(
            "Dispatch Date *",
            value=datetime.date.today(),
            key="dp_date",
        )

    col_d1, col_d2 = st.columns(2)
    with col_d1:
        dispatched_by = st.text_input(
            "Dispatched By *",
            value=st.session_state.get("user_name", ""),
            key="dp_by",
        )
    with col_d2:
        remarks = st.text_input(
            "Remarks",
            key="dp_remarks",
            placeholder="Packing notes, special instructions…",
        )

    # Billing doc ref (auto-pick first active doc)
    billing_doc_ref = ""
    if billing_docs:
        billing_doc_ref = f"{billing_docs[0].get('doc_type','')} {billing_docs[0].get('doc_no','')}".strip()

    # ── Dispatch mode ──────────────────────────────────────────────────────
    st.markdown("#### 📋 Lines to Dispatch")
    dispatch_mode = st.radio(
        "Mode",
        ["📦 Full dispatch (all remaining)", "⚡ Partial dispatch (custom qty per line)"],
        key="dp_mode",
        horizontal=True,
    )
    is_partial_mode = "Partial" in dispatch_mode

    line_qtys: Dict[str, int] = {}

    for line in dispatchable:
        lid       = str(line["id"])
        pname     = line.get("product_name") or "Lens"
        eye       = str(line.get("eye_side") or "")
        brand     = line.get("brand") or ""
        sph_val   = line.get("sph")
        remaining = int(line.get("remaining_qty") or 0)
        billed_q  = int(line.get("billing_qty") or 0)
        already   = int(line.get("already_dispatched") or 0)

        sph_str  = f"  SPH {float(sph_val):+.2f}" if sph_val else ""
        eye_icon = {"R": "👁 R", "RIGHT": "👁 R", "L": "👁 L", "LEFT": "👁 L"}.get(
            eye.upper(), f"👁 {eye}" if eye else "👁"
        )
        label = f"{eye_icon}  {pname}  {brand}{sph_str}"

        if is_partial_mode:
            with st.container(border=True):
                ll, lr = st.columns([5, 3])
                with ll:
                    st.markdown(
                        f"<div style='font-size:0.82rem;color:#cbd5e1;font-weight:600'>{label}</div>"
                        f"<div style='font-size:0.68rem;color:#64748b'>"
                        f"Billed: {billed_q}  ·  Already sent: {already}  ·  Remaining: {remaining}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                with lr:
                    qty = st.number_input(
                        "Qty", min_value=0, max_value=remaining,
                        value=remaining, step=1, key=f"dp_qty_{lid}",
                        label_visibility="collapsed",
                    )
        else:
            qty = remaining
            st.markdown(
                f"<div style='padding:3px 0;color:#94a3b8;font-size:0.78rem'>"
                f"✔ {label}  —  **{remaining} units**</div>",
                unsafe_allow_html=True,
            )
        line_qtys[lid] = qty

    # Preview totals
    total_dispatching = sum(line_qtys.values())
    remaining_after   = summary.get("total_remaining", 0) - total_dispatching
    will_be_partial   = remaining_after > 0

    if total_dispatching > 0:
        disp_color = "#f59e0b" if will_be_partial else "#10b981"
        st.markdown(
            f"<div style='background:{disp_color}18;border:1px solid {disp_color}44;"
            f"border-radius:8px;padding:8px 14px;margin:8px 0;"
            f"color:{disp_color};font-weight:700;font-size:0.82rem'>"
            f"{'⚡ Partial dispatch' if will_be_partial else '📦 Full dispatch'}:  "
            f"{total_dispatching} unit(s)"
            + (f"  ·  {remaining_after} unit(s) will remain pending" if will_be_partial else "  ·  all billed qty dispatched")
            + "</div>",
            unsafe_allow_html=True,
        )

    # Tracking URL preview
    if tracking_no and route_code:
        t_url = LogisticsRoute.tracking_url(route_code, tracking_no.strip())
        if t_url:
            st.markdown(
                f"<div style='font-size:0.72rem;color:#3b82f6'>"
                f"🔗 <a href='{t_url}' target='_blank' rel='noopener'>Track this shipment</a></div>",
                unsafe_allow_html=True,
            )

    # ── Submit ─────────────────────────────────────────────────────────────
    if st.button(
        "🚚 Save Dispatch",
        type="primary",
        width='stretch',
        key="dp_submit",
        disabled=(total_dispatching == 0),
    ):
        errors = []
        if not dispatched_by.strip():
            errors.append("Dispatched By is required")
        if needs_tracking and not tracking_no.strip():
            errors.append(f"Tracking number is required for {selected_route_label}")
        if total_dispatching <= 0:
            errors.append("Dispatch quantity must be at least 1")

        if errors:
            for e in errors:
                st.error(f"❌ {e}")
        else:
            ok, msg = create_dispatch_event(
                order_id      = order_id,
                order_no      = order_no,
                route_code    = route_code,
                carrier_name  = carrier_name.strip() or selected_route_label,
                tracking_no   = tracking_no.strip(),
                dispatched_by = dispatched_by.strip(),
                dispatch_date = dispatch_date,
                line_qtys     = {k: v for k, v in line_qtys.items() if v > 0},
                billing_doc_ref = billing_doc_ref,
                remarks       = remarks.strip(),
            )
            if ok:
                st.success(msg)
                # ── WhatsApp notification ─────────────────────────────
                try:
                    from modules.wa_hub import wa_panel, wa_dispatched
                    from modules.settings.shop_master import get_unit_info
                    _sh  = get_unit_info("wholesale")
                    _mob = order.get("mobile","") or order.get("party_mobile","")
                    _msg = wa_dispatched(
                        party      = order.get("party_name",""),
                        order_no   = order_no,
                        courier    = carrier_name.strip(),
                        tracking   = tracking_no.strip(),
                        shop_name  = _sh.get("shop_name","DV Optical"),
                        phone      = _sh.get("shop_phone",""),
                    )
                    wa_panel(_mob, _msg,
                             key=f"wa_dispatch_{order_no}",
                             title="📲 WhatsApp — Order Dispatched",
                             expanded=True)
                except Exception:
                    pass
                st.rerun()
            else:
                st.error(f"❌ {msg}")


# ──────────────────────────────────────────────────────────────
# DISPATCH HISTORY
# ──────────────────────────────────────────────────────────────

def _render_dispatch_history(
    order_id: str,
    order_no: str,
    read_only: bool = False,
) -> None:
    history = get_dispatch_history(order_id)

    if not history:
        st.info("No dispatch events recorded yet.")
        return

    for ev in history:
        ev_status   = (ev.get("status") or "DISPATCHED").upper()
        is_partial  = bool(ev.get("is_partial"))
        carrier     = ev.get("carrier_name") or ev.get("route_code") or "—"
        tracking    = ev.get("tracking_no") or "—"
        dispatch_no = ev.get("dispatch_no") or "—"
        dispatched_at = str(ev.get("dispatched_at") or "")[:10]
        dispatched_by = ev.get("dispatched_by") or "system"
        billing_ref = ev.get("billing_doc_ref") or ""
        remarks     = ev.get("remarks") or ""
        delivered_at = str(ev.get("delivered_at") or "")[:10]

        ev_color = {
            "DISPATCHED": "#3b82f6",
            "DELIVERED":  "#10b981",
            "CANCELLED":  "#ef4444",
        }.get(ev_status, "#64748b")

        ev_icon = {
            "DISPATCHED": "🚚",
            "DELIVERED":  "✅",
            "CANCELLED":  "❌",
        }.get(ev_status, "•")

        with st.container(border=True):
            h1, h2 = st.columns([5, 3])
            with h1:
                st.markdown(
                    f"<div style='font-weight:700;font-family:monospace;font-size:0.9rem'>"
                    f"{dispatch_no}</div>"
                    f"<div style='color:#94a3b8;font-size:0.75rem;margin-top:2px'>"
                    f"🚚 {carrier}  ·  "
                    f"{'🔢 ' + str(tracking) if tracking != '—' else 'no tracking'}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                if billing_ref:
                    st.caption(f"📋 {billing_ref}")
                if delivered_at:
                    st.caption(f"✅ Delivered: {delivered_at}")
            with h2:
                partial_badge = " ⚡ PARTIAL" if (is_partial and ev_status == "DISPATCHED") else ""
                st.markdown(
                    f"<span style='background:{ev_color};color:#fff;padding:2px 10px;"
                    f"border-radius:12px;font-size:0.72rem;font-weight:700'>"
                    f"{ev_icon} {ev_status}{partial_badge}</span>",
                    unsafe_allow_html=True,
                )
                st.caption(f"{dispatched_at} · {dispatched_by}")

            # Tracking URL
            route_code_ev = ev.get("route_code") or ""
            if route_code_ev and tracking != "—":
                t_url = LogisticsRoute.tracking_url(route_code_ev, str(tracking))
                if t_url:
                    st.markdown(
                        f"<div style='font-size:0.72rem'>"
                        f"🔗 <a href='{t_url}' target='_blank' rel='noopener'>Track shipment</a></div>",
                        unsafe_allow_html=True,
                    )

            lines = ev.get("lines") or []
            if lines:
                with st.expander(f"📋 {len(lines)} line(s)", expanded=False):
                    for dl in lines:
                        eye   = dl.get("eye_side") or ""
                        pname = dl.get("product_name") or "Lens"
                        brand = dl.get("brand") or ""
                        dqty  = int(dl.get("dispatched_qty") or 0)
                        rqty  = int(dl.get("remaining_qty") or 0)
                        sph   = dl.get("sph") or ""
                        eye_icon = {"R": "👁R", "RIGHT": "👁R",
                                    "L": "👁L", "LEFT": "👁L"}.get(str(eye).upper(), eye)
                        st.markdown(
                            f"<div style='font-size:0.78rem;padding:2px 0;color:#cbd5e1'>"
                            f"{eye_icon}  {pname}  {brand}"
                            f"{'  SPH ' + str(sph) if sph else ''}"
                            f"  — <b>{dqty} dispatched</b>"
                            + (f"  ·  {rqty} remaining" if rqty else "")
                            + "</div>",
                            unsafe_allow_html=True,
                        )

            if remarks:
                st.caption(f"📝 {remarks}")


# ──────────────────────────────────────────────────────────────
# DELIVERY CONFIRMATION
# ──────────────────────────────────────────────────────────────

def _render_delivery_confirmation(order_id: str, order_no: str) -> None:
    history = get_dispatch_history(order_id)
    pending = [
        ev for ev in history
        if (ev.get("status") or "").upper() == "DISPATCHED"
    ]

    if not pending:
        st.info("No dispatches awaiting delivery confirmation.")
        return

    st.markdown("#### ✅ Confirm Delivery")
    st.caption(
        "Mark a dispatch event as delivered. "
        "When all lines are confirmed, order advances to DELIVERED."
    )

    for ev in pending:
        dispatch_no = ev.get("dispatch_no") or "—"
        carrier     = ev.get("carrier_name") or ev.get("route_code") or "—"
        tracking    = ev.get("tracking_no") or "—"
        is_partial  = bool(ev.get("is_partial"))
        dispatch_id = str(ev.get("id") or "")
        dispatched_at = str(ev.get("dispatched_at") or "")[:10]

        with st.container(border=True):
            st.markdown(
                f"**{dispatch_no}** · {carrier}"
                + (f" · {tracking}" if tracking != "—" else "")
                + ("  ⚡ *partial shipment*" if is_partial else "")
                + f"  <span style='color:#64748b;font-size:0.72rem'> sent {dispatched_at}</span>",
                unsafe_allow_html=True,
            )
            c1, c2 = st.columns(2)
            with c1:
                delivery_date = st.date_input(
                    "Delivery Date",
                    value=datetime.date.today(),
                    key=f"del_date_{dispatch_id}",
                )
            with c2:
                confirmed_by = st.text_input(
                    "Confirmed By",
                    value=st.session_state.get("user_name", ""),
                    key=f"del_by_{dispatch_id}",
                )
            notes = st.text_input(
                "Notes",
                key=f"del_notes_{dispatch_id}",
                placeholder="Recipient name, condition of goods, etc.",
            )

            if st.button(
                f"✅ Mark Delivered — {dispatch_no}",
                key=f"del_confirm_{dispatch_id}",
                width='stretch',
                type="primary",
            ):
                ok, msg = confirm_delivery(
                    order_id    = order_id,
                    dispatch_id = dispatch_id,
                    delivery_date = delivery_date,
                    confirmed_by  = confirmed_by.strip() or "system",
                    notes         = notes.strip(),
                )
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
