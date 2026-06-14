"""
Billing Gate
============
Controlled write panel — updates billed_qty on order_lines only.

SCHEMA (from DB backup):
  order_lines: id(uuid), order_id(uuid), product_id(uuid),
               sph, cyl, axis, add_power, eye_side,
               quantity(int), unit_price, total_price, status,
               allocated_qty(int), ready_qty(int), billed_qty(int),
               dispatched_qty(int)

  orders:      id(uuid), order_no(text), status(text), total_value

  DB FUNCTION: recalculate_order_status(p_order_id uuid) → text
               Called after billing to derive new order status from data.

GOVERNANCE:
  - billed_qty is the ONLY writable field in this panel
  - billed_qty <= allocated_qty enforced by UI max_value (overbilling impossible)
  - unit_price / total_price are DISPLAY ONLY — never mutated here
  - After saving, recalculate_order_status() is called so status is always DB-derived
  - Lock button appears only when ALL lines are fully billed
"""

import streamlit as st
import pandas as pd
from typing import Dict, List
from datetime import date, timedelta
import uuid as _uuid

from .event_logger import log_event, EventType


def _q(sql, params):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _write(sql, params):
    """Database write helper"""
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params)
        return True
    except Exception as e:
        st.error(f"Database write error: {e}")
        return False


def _fetch_order_lines(order_id_text: str) -> List[Dict]:
    """
    Fetch order_lines with product name for a given orders.order_no.
    Returns full line data including current billed_qty from DB.
    """
    return _q("""
        SELECT ol.id, ol.eye_side,
               ol.quantity, ol.unit_price, ol.total_price, ol.status,
               ol.allocated_qty, ol.ready_qty, ol.billed_qty, ol.dispatched_qty,
               COALESCE(p.product_name,
                        ol.lens_params->>'service_display_name',
                        ol.lens_params->>'display_product_name',
                        ol.lens_params->>'service_description',
                        'Service') AS product_name,
               COALESCE(p.box_size, 1) AS box_size,
               COALESCE(p.unit, CASE WHEN COALESCE(ol.is_service_line,FALSE) THEN 'SERVICE' ELSE 'PCS' END) AS unit,
               COALESCE(ol.is_service_line, FALSE)   AS is_service_line,
               COALESCE(o.order_source, '')           AS order_source
        FROM order_lines ol
        JOIN orders o   ON o.id  = ol.order_id
        LEFT JOIN products p ON p.id  = ol.product_id
        WHERE o.order_no = %(ono)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        ORDER BY ol.eye_side, ol.id
    """, {"ono": order_id_text})


def _write_billed_qty(line_id: str, billed_qty: int,
                       order_id_text: str, product_name: str) -> bool:
    """
    Write billed_qty to order_lines.
    This is the ONLY field the billing gate writes.
    """
    try:
        from modules.sql_adapter import run_write
        run_write(
            "UPDATE order_lines SET billed_qty = %(qty)s WHERE id = %(lid)s::uuid",
            {"qty": billed_qty, "lid": line_id}
        )
        log_event(EventType.BILLING_WRITTEN, order_id=order_id_text,
                  details={"line_id": line_id, "product": product_name,
                           "billed_qty": billed_qty}, source="user")
        return True
    except Exception as e:
        st.error(f"Billing write failed: {e}")
        return False


def _recalculate_order_status(order_uuid: str, order_id_text: str) -> None:
    """
    Call DB function to re-derive order status from data.
    GOVERNANCE: Python never sets status directly — DB computes it.
    """
    try:
        from modules.sql_adapter import run_scalar
        run_scalar("SELECT public.recalculate_order_status(%(id)s::uuid)",
                   {"id": order_uuid})
        log_event(EventType.ORDER_STATUS_CHANGED, order_id=order_id_text,
                  details={"trigger": "billing_save"}, source="system")
    except Exception:
        pass   # Non-fatal — status will sync on next load


def render_billing_gate(order: Dict):
    """
    Render billing gate panel for an order.
    Shows party billing preference and integrates challan/invoice generation.
    """
    order_id_text = order.get("order_no") or order.get("order_id", "")
    order_uuid = order.get("id")
    if not order_id_text:
        st.error("❌ Order ID missing")
        return

    # ── Get Party Billing Preference ────────────────────────────────────
    party_id = order.get("party_id")
    billing_preference = "CHALLAN"  # default
    party_name = order.get("party_name", "Unknown Party")
    
    if party_id:
        try:
            from modules.billing.challan_invoice_manager import get_party_billing_preference
            billing_preference = get_party_billing_preference(str(party_id))
        except Exception:
            pass

    # ── Party Information Header ───────────────────────────────────────────
    # Retail orders always use Challan regardless of party setting
    _is_retail_order = str(order.get("order_type","")).upper() == "RETAIL"
    _eff_pref = "C" if _is_retail_order else billing_preference

    st.markdown("### 🏢 Party Information")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Party Name", party_name)
    with col2:
        _pref_label = "C — Challan" if _eff_pref == "C" else "I — Direct Invoice"
        st.metric("Doc Preference", _pref_label)
    with col3:
        if _eff_pref == "C":
            st.markdown(
                "📋 **Challan** → Invoice later<br>"
                "<small>Create challan here, convert to invoice from Challan Dashboard</small>",
                unsafe_allow_html=True)
        else:
            st.markdown(
                "🧾 **Direct Invoice**<br>"
                "<small>Wholesale party — challan + invoice created together</small>",
                unsafe_allow_html=True)
    
    # Retail orders use direct invoice; wholesale uses challan workflow
    # Both need the billing gate for billed_qty tracking
    order_type = order.get("order_type", "").upper()

    # ── Detect COUNTER_SALE (bulk_order) orders ───────────────────────────
    # For these, stock was verified + FIFO-allocated at cart build time.
    # allocated_qty = quantity already; no manual gate needed.
    _order_src = str(order.get("order_source") or "").upper()
    _is_counter_sale = (_order_src == "COUNTER_SALE")
    if not _is_counter_sale:
        # Also check from DB in case session order dict lacks order_source
        try:
            _src_row = _q("""
                SELECT order_source FROM orders
                WHERE order_no = %(ono)s OR id::text = %(oid)s
                LIMIT 1
            """, {"ono": order_id_text, "oid": str(order.get("id") or "")})
            if _src_row:
                _is_counter_sale = str(_src_row[0].get("order_source") or "").upper() == "COUNTER_SALE"
        except Exception:
            pass

    st.markdown("---")

    st.markdown("### 💰 Billing Gate")

    # ── Fetch fresh line data from DB (billed_qty is always DB-read) ────
    db_lines = _fetch_order_lines(order_id_text)

    # Fall back to session lines if DB unavailable
    if not db_lines:
        all_lines = (order.get("stock_lines", []) +
                     order.get("inhouse_lines", []) +
                     order.get("lab_order_lines", []) +
                     order.get("service_lines", []))
        if not all_lines:
            st.info("No line items found for billing.")
            return
        db_lines = [{
            "id":           str(l.get("id") or ""),
            "eye_side":     l.get("eye_side", ""),
            "is_service_line": bool(l.get("is_service_line", False)),
            "product_name": l.get("product_name", ""),
            "quantity":     int(l.get("billing_qty") or 0),
            "unit_price":   float(l.get("unit_price") or 0),
            "total_price":  float(l.get("billing_total") or 0),
            "allocated_qty": int(l.get("allocated_qty") or 0),
            "billed_qty":   int(l.get("billed_qty") or 0),
        } for l in all_lines]

    # ── Billing lock check ────────────────────────────────────────────
    try:
        from modules.backoffice.order_status_live import get_live_status
        _live = get_live_status(order)
    except Exception:
        _live = order.get("status") or "PENDING"
    is_locked = _live in ("BILLED", "DELIVERED", "CLOSED")

    # ── Derived totals ────────────────────────────────────────────────
    total_allocated = sum(int(l.get("allocated_qty") or 0) for l in db_lines)
    total_billed    = sum(int(l.get("billed_qty")    or 0) for l in db_lines)
    total_amount    = sum(float(l.get("total_price") or 0) for l in db_lines)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Items",        len(db_lines))
    m2.metric("Allocated",    total_allocated)
    m3.metric("Billed",       total_billed)
    m4.metric("Amount",       f"₹{total_amount:,.2f}")

    # Billing state badge
    if is_locked:
        st.markdown("<span style='background:#374151;color:#fff;border-radius:5px;"
                    "padding:4px 12px;font-size:0.8rem;font-weight:700'>🔒 Billing Locked</span>",
                    unsafe_allow_html=True)
    elif total_billed == 0:
        st.markdown("<span style='background:#f59e0b;color:#fff;border-radius:5px;"
                    "padding:4px 12px;font-size:0.8rem;font-weight:700'>⏳ Ready to Bill</span>",
                    unsafe_allow_html=True)
    elif total_billed < total_allocated:
        st.markdown("<span style='background:#3b82f6;color:#fff;border-radius:5px;"
                    "padding:4px 12px;font-size:0.8rem;font-weight:700'>⚡ Partially Billed</span>",
                    unsafe_allow_html=True)
    else:
        st.markdown("<span style='background:#10b981;color:#fff;border-radius:5px;"
                    "padding:4px 12px;font-size:0.8rem;font-weight:700'>✅ All Lines Billed</span>",
                    unsafe_allow_html=True)

    if is_locked:
        st.warning("🔒 Billing is locked — no further writes permitted.")

    st.markdown("---")

    with st.expander("ℹ️ Billing Rules", expanded=False):
        st.markdown(
            "**Writable:** `billed_qty` only\n\n"
            "**Locked (read-only):** `unit_price`, `total_price`, discount\n\n"
            "**Prevented:** billed > allocated (hard UI limit), manual status edits\n\n"
            "**After save:** `recalculate_order_status()` derives new status from data"
        )

    st.markdown("#### 📋 Line-wise Billing")

    pending_writes = {}   # line_id → proposed billed_qty
    has_errors     = False

    for line in db_lines:
        lid          = str(line.get("id", ""))
        product_name = line.get("product_name", "Unknown")
        eye_side     = str(line.get("eye_side", "")).strip()
        qty          = int(line.get("quantity") or 0)
        allocated    = int(line.get("allocated_qty") or 0)
        unit_price   = float(line.get("unit_price") or 0)
        total_price  = float(line.get("total_price") or 0)
        prev_billed  = int(line.get("billed_qty") or 0)
        _is_svc      = bool(line.get("is_service_line")) or eye_side in ("S", "SERVICE")
        eye_label    = {"R": "👁️ R", "L": "👁️ L"}.get(eye_side,
                        "⚙️ Service" if _is_svc else eye_side)

        # SERVICE lines (consultation fee, eye testing) are auto-billed —
        # allocated_qty was set to qty at save time, so they're always ready.
        # Register them as fully billed and render a read-only badge instead
        # of an editable number_input.
        if _is_svc:
            pending_writes[lid] = allocated or qty
            with st.container(border=True):
                _sc1, _sc2, _sc3 = st.columns([4, 2, 2])
                _sc1.markdown(f"**{eye_label} — {product_name}**")
                _sc2.caption(f"Qty: {qty}  ·  ₹{unit_price:,.2f}")
                _sc3.success("✅ Auto-billed (Service)")
            continue   # skip the box/product logic below

        # COUNTER_SALE lines — stock was FIFO-verified at cart build time.
        # allocated_qty = quantity; no operator input needed.
        # Auto-fill billed_qty = allocated and show a read-only badge.
        _line_src = str(line.get("order_source") or "").upper()
        if _is_counter_sale or _line_src == "COUNTER_SALE":
            _auto_billed = allocated if allocated > 0 else qty
            pending_writes[lid] = _auto_billed
            with st.container(border=True):
                _cc1, _cc2, _cc3, _cc4 = st.columns([4, 2, 2, 2])
                _eye_lbl_cs = {"R": "👁️ R", "L": "👁️ L", "B": "👁️ Pair"}.get(
                    eye_side.upper(), eye_side or "—"
                )
                _cc1.markdown(f"**{_eye_lbl_cs} — {product_name}**")
                _cc2.caption(f"Qty: {qty}  ·  ₹{unit_price:,.2f}")
                _cc3.metric("Allocated", alloc_display if allocated else f"{qty} PCS")
                _cc4.success(f"✅ Auto-billed ({_auto_billed})")
            continue   # no manual input needed for counter-sale lines
        
        # Box logic for quantity display
        box_size = int(line.get("box_size") or 1)
        unit = str(line.get("unit") or "PCS").upper()
        
        # Enhanced box detection - check multiple conditions
        is_box_product = (
            (unit == "BOX" and box_size > 1) or  # Explicit BOX unit
            (box_size > 1 and qty == box_size) or      # Qty matches box size exactly
            (box_size > 1 and allocated == box_size)    # Allocated matches box size exactly
        )
        
        if is_box_product:
            # Format quantity as box+pcs
            if qty == box_size:
                qty_display = f"1 BOX"
            elif qty > box_size:
                boxes = qty // box_size
                pcs_rem = qty % box_size
                if pcs_rem == 0:
                    qty_display = f"{boxes} BOX"
                else:
                    qty_display = f"{boxes} BOX + {pcs_rem} PCS"
            else:
                qty_display = f"{qty} PCS"
            
            if allocated == box_size:
                alloc_display = f"1 BOX"
            elif allocated > box_size:
                boxes = allocated // box_size
                pcs_rem = allocated % box_size
                if pcs_rem == 0:
                    alloc_display = f"{boxes} BOX"
                else:
                    alloc_display = f"{boxes} BOX + {pcs_rem} PCS"
            else:
                alloc_display = f"{allocated} PCS"
        else:
            qty_display = f"{qty} PCS"
            alloc_display = f"{allocated} PCS"

        with st.container(border=True):
            st.markdown(f"**{eye_label} {product_name}**")

            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Order Qty", qty_display)
            p2.metric("Allocated", alloc_display)
            p3.metric("Unit Price 🔒", f"₹{unit_price:.2f}")
            p4.metric("Line Total 🔒", f"₹{total_price:,.2f}")

            if is_locked:
                st.info(f"Billed: **{prev_billed}** (locked)")
                pending_writes[lid] = prev_billed
            else:
                ic, sc = st.columns([2, 3])
                with ic:
                    new_billed = st.number_input(
                        "Billed Qty",
                        min_value=0,
                        max_value=allocated,   # ← GOVERNANCE HARD LIMIT
                        value=prev_billed,
                        key=f"bq_{lid}",
                        help=f"Cannot exceed allocated qty ({allocated})"
                    )
                    pending_writes[lid] = new_billed
                with sc:
                    if new_billed > allocated:
                        st.error(f"⚠️ {new_billed} > allocated ({allocated}) — overbilling blocked")
                        has_errors = True
                    elif new_billed == allocated and allocated > 0:
                        st.success("✅ Fully billed")
                    elif new_billed > 0:
                        st.info(f"⚡ Partial ({new_billed}/{allocated})")
                    else:
                        st.caption("Not yet billed")

    # ── Save controls ─────────────────────────────────────────────────
    if not is_locked:
        st.markdown("---")
        sc, lc = st.columns(2)

        with sc:
            from modules.utils.submit_guard import is_locked, guarded_submit
            if st.button("💾 Save Billing", type="primary",
                         disabled=has_errors or is_locked("billing"),
                         use_container_width=True):
                with guarded_submit("billing") as _allowed:
                    if not _allowed:
                        st.stop()
                    ok = True
                    for idx, (lid, bq) in enumerate(pending_writes.items()):
                        pname = db_lines[idx].get("product_name", "") if idx < len(db_lines) else ""
                        if not _write_billed_qty(lid, bq, order_id_text, pname):
                            ok = False
                    if ok:
                        _recalculate_order_status(order_uuid, order_id_text)
                        st.success("✅ Billing saved")
                        st.rerun()

        with lc:
            all_fully_billed = all(
                pending_writes.get(str(l.get("id", "")), 0) >= int(l.get("allocated_qty") or 0) > 0
                for l in db_lines
            )
            if all_fully_billed and not has_errors:
                if st.button("🔐 Save & Lock", use_container_width=True,
                             help="Locks billing — triggers BILLED status"):
                    ok = True
                    for idx, (lid, bq) in enumerate(pending_writes.items()):
                        pname = db_lines[idx].get("product_name", "") if idx < len(db_lines) else ""
                        if not _write_billed_qty(lid, bq, order_id_text, pname):
                            ok = False
                    if ok:
                        _recalculate_order_status(order_uuid, order_id_text)
                        log_event(EventType.BILLING_LOCKED, order_id=order_id_text,
                                  details={}, source="user")
                        st.success("🔐 Billing locked")
                        st.rerun()
            else:
                st.button("🔐 Lock Billing", disabled=True, use_container_width=True,
                          help="Bill all allocated lines to unlock")

    # ── Summary table ─────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 📊 Summary")
    rows = []
    for i, l in enumerate(db_lines):
        rows.append({
            "#":         i + 1,
            "Eye":       str(l.get("eye_side", "")).strip(),
            "Product":   l.get("product_name", "N/A")[:35],
            "Order Qty": int(l.get("quantity") or 0),
            "Allocated": int(l.get("allocated_qty") or 0),
            "Billed":    int(l.get("billed_qty") or 0),
            "Unit ₹":    f"₹{float(l.get('unit_price') or 0):.2f}",
            "Total ₹":   f"₹{float(l.get('total_price') or 0):,.2f}",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.metric("Grand Total", f"₹{total_amount:,.2f}")
    # ── Document generation (Challan / Invoice) is handled in billing_status_ui.py
    # The Billing Gate's only job is tracking billed_qty.
    # Go to the Billing Summary tab → Make Challan to generate documents.
