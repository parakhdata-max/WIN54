"""
modules/retailer/backoffice_orders.py
=======================================
Back Office — Retailer Order Queue

Shows all retailer orders:
  - Submitted (needs confirmation)
  - Confirmed (ready to pack/dispatch)
  - Full history with filters

Add to your existing back office nav:
    from modules.retailer.backoffice_orders import render_backoffice_orders
"""

import streamlit as st
import pandas as pd
from datetime import datetime


def render_backoffice_orders():
    """Back office order management — drop into your existing app nav."""
    st.title("📦 Retailer Orders")

    # Top metrics
    _render_order_metrics()

    st.divider()

    # Filter bar
    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        status_filter = st.selectbox(
            "Status",
            ["All", "SUBMITTED", "CONFIRMED", "DISPATCHED", "DELIVERED", "CANCELLED"],
            key="bo_status_filter",
        )
    with fc2:
        pay_filter = st.selectbox(
            "Payment",
            ["All", "CASH", "UPI", "CREDIT"],
            key="bo_pay_filter",
        )
    with fc3:
        search = st.text_input("Search party / order no", key="bo_search",
                               placeholder="Party name or ORD-...")
    with fc4:
        days = st.selectbox("Period", [1, 7, 30, 90], index=1,
                            format_func=lambda d: f"Last {d} day(s)",
                            key="bo_days")

    orders = _fetch_orders(status_filter, pay_filter, search, days)

    if not orders:
        st.info("No orders found for selected filters.")
        return

    st.caption(f"**{len(orders)}** order(s) found")
    st.divider()

    for order in orders:
        _render_order_card(order)


# ── Metrics strip ─────────────────────────────────────────────────────────────

def _render_order_metrics():
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'SUBMITTED')  AS pending,
                COUNT(*) FILTER (WHERE status = 'CONFIRMED')  AS confirmed,
                COUNT(*) FILTER (WHERE status = 'DISPATCHED') AS dispatched,
                COUNT(*) FILTER (WHERE payment_status = 'PAID'
                                   AND punched_at >= NOW() - INTERVAL '1 day') AS paid_today,
                SUM(net_amount) FILTER (WHERE payment_status = 'PAID'
                                          AND punched_at >= NOW() - INTERVAL '1 day') AS revenue_today
            FROM retailer_orders
            WHERE punched_at >= NOW() - INTERVAL '30 days'
        """) or [{}]
        r = rows[0]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("🔵 Pending",    r.get("pending",    0), help="Submitted, needs confirmation")
        c2.metric("🟢 Confirmed",  r.get("confirmed",  0))
        c3.metric("🚚 Dispatched", r.get("dispatched", 0))
        c4.metric("✅ Paid Today", r.get("paid_today", 0))
        c5.metric("💰 Revenue Today",
                  f"₹ {float(r.get('revenue_today') or 0):,.0f}")
    except Exception:
        pass


# ── Fetch orders ──────────────────────────────────────────────────────────────

def _fetch_orders(status: str, payment: str, search: str, days: int):
    try:
        from modules.sql_adapter import run_query
        _where = [f"punched_at >= NOW() - INTERVAL '{days} days'"]
        _params: list = []

        if status != "All":
            _where.append("status = %s")
            _params.append(status)
        if payment != "All":
            _where.append("payment_method = %s")
            _params.append(payment)
        if search.strip():
            _where.append("(LOWER(party_name) LIKE %s OR LOWER(order_no) LIKE %s)")
            _s = f"%{search.lower().strip()}%"
            _params += [_s, _s]

        return run_query(f"""
            SELECT id, order_no, party_name, mobile, status,
                   payment_method, payment_status,
                   subtotal, discount_amount, net_amount, scheme_applied,
                   notes, punched_at, confirmed_at, dispatched_at,
                   confirmed_by,
                   TO_CHAR(punched_at, 'DD-Mon-YYYY HH24:MI') AS punched_display
            FROM retailer_orders
            WHERE {' AND '.join(_where)}
            ORDER BY punched_at DESC
            LIMIT 200
        """, tuple(_params)) or []
    except Exception as e:
        st.error(f"DB error: {e}")
        return []


# ── Order card ────────────────────────────────────────────────────────────────

STATUS_ICON = {
    "SUBMITTED":  "🔵",
    "CONFIRMED":  "🟢",
    "DISPATCHED": "🟡",
    "DELIVERED":  "✅",
    "CANCELLED":  "🔴",
    "DRAFT":      "⚪",
}

def _render_order_card(order: dict):
    icon = STATUS_ICON.get(order.get("status", ""), "⚪")
    pay  = order.get("payment_method", "")
    paid = "✅" if order.get("payment_status") == "PAID" else "⏳"

    with st.expander(
        f"{icon} **{order['order_no']}** &nbsp;|&nbsp; "
        f"{order.get('party_name','')} &nbsp;|&nbsp; "
        f"₹ {float(order.get('net_amount') or 0):,.0f} &nbsp;|&nbsp; "
        f"{pay} {paid} &nbsp;|&nbsp; "
        f"{order.get('punched_display','')}",
        expanded=(order.get("status") == "SUBMITTED"),
    ):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Status",   f"{icon} {order.get('status','')}")
        c2.metric("Payment",  f"{pay} — {paid}")
        c3.metric("Net",      f"₹ {float(order.get('net_amount') or 0):,.2f}")
        c4.metric("Discount", f"₹ {float(order.get('discount_amount') or 0):,.2f}")

        if order.get("scheme_applied"):
            st.caption(f"🏷️ {order['scheme_applied']}")
        if order.get("notes"):
            st.caption(f"📝 {order['notes']}")

        # Order lines
        lines = _fetch_order_lines(str(order["id"]))
        if lines:
            st.dataframe(pd.DataFrame(lines), use_container_width=True, hide_index=True)

        st.markdown("---")

        # Action buttons
        status = order.get("status", "")
        _render_order_actions(order, status)


def _render_order_actions(order: dict, status: str):
    oid   = str(order["id"])
    o_no  = order["order_no"]
    user  = st.session_state.get("user", "backoffice")

    col1, col2, col3, col4 = st.columns(4)

    # CONFIRM
    with col1:
        if status == "SUBMITTED":
            if st.button("✅ Confirm", key=f"confirm_{oid}",
                         type="primary", use_container_width=True):
                _update_status(oid, "CONFIRMED", confirmed_by=user)
                st.success(f"✅ {o_no} confirmed")
                st.rerun()

    # DISPATCH
    with col2:
        if status == "CONFIRMED":
            if st.button("🚚 Dispatch", key=f"dispatch_{oid}",
                         use_container_width=True):
                _update_status(oid, "DISPATCHED")
                st.success(f"🚚 {o_no} dispatched")
                st.rerun()

    # DELIVER
    with col3:
        if status == "DISPATCHED":
            if st.button("📦 Mark Delivered", key=f"deliver_{oid}",
                         use_container_width=True):
                _update_status(oid, "DELIVERED")
                st.success(f"✅ {o_no} delivered")
                st.rerun()

    # CANCEL
    with col4:
        if status in ("SUBMITTED", "CONFIRMED"):
            if st.button("❌ Cancel", key=f"cancel_{oid}",
                         use_container_width=True):
                _update_status(oid, "CANCELLED")
                st.warning(f"❌ {o_no} cancelled")
                st.rerun()


# ── DB helpers ────────────────────────────────────────────────────────────────

def _fetch_order_lines(order_id: str):
    try:
        from modules.sql_adapter import run_query
        return run_query("""
            SELECT product_name AS "Product", main_group AS "Group",
                   qty AS "Qty", unit_price AS "Unit ₹",
                   discount_pct AS "Disc %", line_total AS "Total ₹"
            FROM retailer_order_lines
            WHERE order_id = %s
            ORDER BY product_name
        """, (order_id,)) or []
    except Exception:
        return []


def _update_status(order_id: str, status: str, confirmed_by: str = None):
    try:
        from modules.sql_adapter import run_write
        if status == "CONFIRMED":
            run_write("""
                UPDATE retailer_orders
                SET status = %s, confirmed_at = NOW(),
                    confirmed_by = %s, updated_at = NOW()
                WHERE id = %s
            """, (status, confirmed_by or "backoffice", order_id))
        elif status == "DISPATCHED":
            run_write("""
                UPDATE retailer_orders
                SET status = %s, dispatched_at = NOW(), updated_at = NOW()
                WHERE id = %s
            """, (status, order_id))
        elif status == "DELIVERED":
            run_write("""
                UPDATE retailer_orders
                SET status = %s, delivered_at = NOW(), updated_at = NOW()
                WHERE id = %s
            """, (status, order_id))
        else:
            run_write("""
                UPDATE retailer_orders
                SET status = %s, updated_at = NOW()
                WHERE id = %s
            """, (status, order_id))
    except Exception as e:
        st.error(f"Update failed: {e}")
