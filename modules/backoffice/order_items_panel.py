"""
order_items_panel.py
=====================
Order Items Panel — Tab 1 content + shared billing/status helpers.

EXTRACTED FROM: backoffice_shell.py (Issue 2 — shell must be pure router)

RULE (Issue 1 — ctx everywhere)
---------------------------------
  All public functions accept ctx: BackofficeContext as first argument.
  Private helpers (_render_*) may accept derived values but never raw `st`.

WHAT THIS PANEL OWNS
--------------------
  - Tab 1: Order line items (eye blocks, qty, allocation)
  - Product change dialog
  - Billing summary table
  - GST verification footer
  - Save button with guards
  - Status update panel (Tab 3)
  - Billing summary tab (Tab 4)

WHAT IT DOES NOT OWN
---------------------
  - Tab routing (→ backoffice_shell.py)
  - Fulfillment / supplier (→ fulfillment/)
  - Documents (→ lab_output_layer.py)
  - Debug overlay (→ devtools/)
  - Assignment panel (→ assignment_panel.py)
"""

import datetime
import streamlit as st
import pandas as pd
from typing import Dict, List

from modules.workflow.status import OrderStatus
from modules.core.price_qty_governor import (
    normalize_to_pcs_price,
    reverse_qty,
    compute_line_gst,
    box_qty_label,
    pair_qty_label,
    PAIR_TO_PCS,
)
from .kernel import BackofficeContext, after_save_hook
from .backoffice_helpers import (
    fmt_signed,
    get_display_order_id,
)
from .backoffice_logic import (
    update_manufacturing_power,
    refresh_line_state,
    recalculate_order_totals,
)
from .backoffice_panels import (
    render_power_edit_ui,
    render_allocation_window,
)


# ═══════════════════════════════════════════════════════════════════════
# TAB 1 — ORDER ITEMS  (ctx-first)
# ═══════════════════════════════════════════════════════════════════════

def render_order_items_panel(ctx: BackofficeContext) -> None:
    """
    Full Tab 1 content: line items, billing table, save button.

    Called by backoffice_shell.py inside `with tab1:`.
    """
    order     = ctx.order
    all_lines = ctx.all_lines

    st.markdown("### 📦 Order Line Items")

    # Sort — Right eye before Left
    all_lines.sort(key=_eye_sort_key)

    # Group by product (R + L together)
    product_groups = _build_product_groups(order, all_lines)

    for product_id, group in product_groups.items():
        st.markdown(f"#### 👁️ {group['product_name']}")
        if group["brand"] != "N/A":
            st.caption(f"Brand: {group['brand']}")
        _render_product_sync_option(group, product_id)
        st.markdown("---")

        has_right = bool(group["R"])
        has_left  = bool(group["L"])

        if has_right and has_left:
            col_r, col_l = st.columns(2)
        elif has_right:
            col_r = st.container(); col_l = None
        elif has_left:
            col_r = None; col_l = st.container()
        else:
            col_r = col_l = None

        if has_right:
            with col_r:
                _render_eye_block(ctx, group["R"], group["R_idx"], "R")
        if has_left:
            with col_l:
                _render_eye_block(ctx, group["L"], group["L_idx"], "L")

    # Allocation window
    if ctx.session.get("bo_show_allocation_window", False):
        line_idx = ctx.session.get("bo_allocation_line_idx")
        if line_idx is not None and line_idx < len(all_lines):
            render_allocation_window(all_lines[line_idx], line_idx, order)

    # Summary section
    st.markdown("---")
    st.markdown("### 📊 Order Summary")

    if st.button("🔍 System Health Check", use_container_width=True, key="health_check_btn"):
        try:
            from .backoffice_helpers import run_system_health_check
            issues = run_system_health_check(order)
        except ImportError:
            issues = []
        if not issues:
            st.success("✅ System OK. No issues found.")
        else:
            st.error("⚠️ Issues Found:")
            for i in issues:
                st.write("•", i)

    _render_billing_summary_table(ctx)

    # Debug overlay — gated by ctx.is_debug_pricing (system flag + session toggle)
    from .devtools.debug_overlay import render_debug_overlay_safe
    render_debug_overlay_safe(ctx)

    # Assignment panel
    from .fulfillment import render_assignment_panel_block
    render_assignment_panel_block(ctx)

    # GST verification
    _render_gst_verification(ctx)

    # Save button
    render_save_button(ctx)


# ═══════════════════════════════════════════════════════════════════════
# TAB 3 — STATUS PANEL  (ctx-first)
# ═══════════════════════════════════════════════════════════════════════

def render_status_panel(ctx: BackofficeContext) -> None:
    """Status update panel for Tab 3."""
    order = ctx.order

    st.markdown("---")
    st.markdown("### 📊 Update Order Status")

    current_status = order.get("status", "PENDING")
    status_values  = [s.value for s in OrderStatus]

    new_status = st.selectbox(
        "New Status",
        status_values,
        index=status_values.index(current_status) if current_status in status_values else 0,
        key=f"status_update_{ctx.order_id}",
    )

    notes = st.text_area("Status Update Notes", key=f"status_notes_{ctx.order_id}")

    col1, col2 = st.columns(2)

    with col1:
        if st.button("✅ Update Status", type="primary", use_container_width=True, key="status_update_btn"):
            for o in ctx.session.get("bo_active_orders", []):
                if get_display_order_id(o) == get_display_order_id(order):
                    o["status"]     = new_status
                    o["updated_at"] = datetime.datetime.now().isoformat()
                    if notes:
                        o.setdefault("status_history", []).append({
                            "timestamp": datetime.datetime.now().isoformat(),
                            "status":    new_status,
                            "notes":     notes,
                        })
            ctx.record("status_updated", {"new_status": new_status})
            st.success(f"✅ Status updated to: {new_status}")
            st.rerun()

    with col2:
        if st.button("Cancel", use_container_width=True, key="status_cancel_btn"):
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════
# TAB 4 — BILLING SUMMARY TAB  (ctx-first)
# ═══════════════════════════════════════════════════════════════════════

def render_billing_summary_tab(ctx: BackofficeContext) -> None:
    """Billing summary tab (Tab 4) content — includes service charges."""
    order     = ctx.order
    all_lines = ctx.all_lines
    order_id  = str(order.get("id") or "")

    st.markdown("### 💰 Billing Summary")

    locked_count = sum(1 for l in all_lines if l.get("pricing_locked", False))
    if locked_count:
        st.info(f"🔒 {locked_count} of {len(all_lines)} line items have locked pricing")

    total_billing   = sum(
        l.get("billing_total", 0) or
        (lambda _up, _qty, _gst, _ot: (
            compute_line_gst(_up, _qty, _gst, _ot)["grand_total"]
            if _up > 0 and _qty > 0 else 0.0
        ))(
            normalize_to_pcs_price(float(l.get("unit_price") or 0), l),
            int(l.get("billing_qty") or 0),
            float(l.get("gst_percent") or 0),
            str(order.get("order_type") or "RETAIL").upper()
        )
        for l in all_lines
    )
    total_allocated = sum(l.get("allocated_qty", 0) for l in all_lines)

    # Load service charges
    _svc_charges = []
    _svc_total   = 0.0
    try:
        from modules.backoffice.order_charges_panel import fetch_charges
        _svc_charges = fetch_charges(order_id) or []
        _svc_total   = round(sum(float(c.get("total_amount") or 0) for c in _svc_charges), 2)
    except Exception:
        pass

    _grand_total = round(total_billing + _svc_total, 2)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Items",      len(all_lines))
    c2.metric("Total Allocated",  total_allocated)
    c3.metric("Lens Total",       f"₹{total_billing:,.2f}")
    c4.metric("**Grand Total**",  f"₹{_grand_total:,.2f}",
              delta=f"+₹{_svc_total:,.2f} services" if _svc_total > 0 else None,
              delta_color="off")

    billing_rows = [
        {
            "Line #":     idx,
            "Product":    l.get("product_name", "N/A"),
            "Eye":        l.get("eye_side", ""),
            "Qty":        int(l.get("billing_qty", 0) or 0),
            "Unit Price": f"₹{normalize_to_pcs_price(float(l.get('unit_price', 0) or 0), l):.2f}",
            "Total":      f"₹{float(l.get('billing_total', 0) or 0):.2f}",
            "🔒":         "🔒" if l.get("pricing_locked") else "",
        }
        for idx, l in enumerate(all_lines, 1)
    ]
    st.dataframe(pd.DataFrame(billing_rows), use_container_width=True)

    # Service charges breakdown
    if _svc_charges:
        st.markdown("**Service Charges**")
        svc_rows = [
            {
                "Type":        c.get("charge_type", ""),
                "Description": c.get("description", ""),
                "Base":        f"₹{float(c.get('amount') or 0):,.2f}",
                "GST %":       f"{float(c.get('gst_percent') or 0):.0f}%",
                "Total":       f"₹{float(c.get('total_amount') or 0):,.2f}",
            }
            for c in _svc_charges
        ]
        st.dataframe(pd.DataFrame(svc_rows), use_container_width=True)

        st.markdown(
            f"<div style='background:#0d2818;border:1px solid #10b981;border-radius:6px;"
            f"padding:8px 16px;display:flex;justify-content:space-between;margin-top:4px'>"
            f"<span style='color:#6ee7b7'>Lens Total ₹{total_billing:,.2f}  +  "
            f"Services ₹{_svc_total:,.2f}</span>"
            f"<span style='color:#10b981;font-weight:900;font-size:1.1rem'>"
            f"Grand Total ₹{_grand_total:,.2f}</span></div>",
            unsafe_allow_html=True
        )
    elif not _svc_charges:
        st.caption("No service charges added yet. Add fitting / colouring / courier charges in the Service Charges section above.")

    # Debug toggle — system flag gated
    from .devtools.debug_overlay import render_billing_debug_toggle
    render_billing_debug_toggle(ctx)


# ═══════════════════════════════════════════════════════════════════════
# PRODUCT CHANGE DIALOG  (ctx-first via session modal state)
# ═══════════════════════════════════════════════════════════════════════

@st.dialog("✏️ Change Product", width="large")
def product_change_dialog(ctx: BackofficeContext) -> None:
    """Product change dialog — reads order from ctx, writes back into it."""
    modal = ctx.session.get("bo_product_change_modal", {})
    if not modal.get("active", False):
        return

    line      = modal["line"]
    idx       = modal["idx"]
    eye_label = modal["eye_label"]
    order     = ctx.order

    st.warning(f"⚠️ Changing product for **{eye_label} Eye** — Line #{idx + 1}")
    st.caption(f"Current: {line.get('product_name', 'N/A')} | {line.get('brand', 'N/A')}")
    st.markdown("---")

    from modules.sql_adapter import read_product_master
    products_df = read_product_master()

    if products_df.empty:
        st.error("No products available")
        if st.button("Close", key="dialog_close_empty"):
            ctx.session["bo_product_change_modal"] = {"active": False}
            st.rerun()
        return

    col1, col2 = st.columns(2)
    with col1:
        main_groups    = [""] + sorted(products_df["main_group"].dropna().astype(str).unique().tolist())
        selected_group = st.selectbox("Category", main_groups, key="dialog_main_group")
    with col2:
        brands         = [""] + sorted(products_df["brand"].dropna().astype(str).unique().tolist())
        selected_brand = st.selectbox("Brand", brands, key="dialog_brand")

    filtered = products_df.copy()
    if selected_group:
        filtered = filtered[filtered["main_group"].astype(str) == selected_group]
    if selected_brand:
        filtered = filtered[filtered["brand"].astype(str) == selected_brand]

    product_list = sorted(filtered["product_name"].dropna().astype(str).unique().tolist())

    if not product_list:
        st.warning("No products match the filters")
        if st.button("Close", key="dialog_close_noproducts"):
            ctx.session["bo_product_change_modal"] = {"active": False}
            st.rerun()
        return

    selected_product = st.selectbox("Select Product *", [""] + product_list, key="dialog_product")

    if selected_product:
        product_row = filtered[filtered["product_name"] == selected_product].iloc[0]
        st.success(f"✅ **Selected:** {product_row['product_name']} | {product_row.get('brand', 'N/A')}")
        st.markdown("---")

        col_apply, col_cancel = st.columns(2)

        with col_apply:
            if st.button("✅ Apply Change", type="primary", use_container_width=True, key="dialog_apply"):
                old_product = line.get("product_name", "Unknown")
                _apply_product_change(line, product_row, eye_label, idx, order, ctx)
                ctx.record("product_changed", {"old": old_product, "new": product_row["product_name"]})
                ctx.session["bo_product_change_modal"] = {"active": False}
                st.success("✅ Product changed!")
                st.rerun()

        with col_cancel:
            if st.button("❌ Cancel", use_container_width=True, key="dialog_cancel"):
                ctx.session["bo_product_change_modal"] = {"active": False}
                st.rerun()
    else:
        if st.button("❌ Cancel", use_container_width=True, key="dialog_cancel_empty"):
            ctx.session["bo_product_change_modal"] = {"active": False}
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════
# SAVE BUTTON  (ctx-first — uses kernel hooks)
# ═══════════════════════════════════════════════════════════════════════

def render_save_button(ctx: BackofficeContext) -> None:
    """Save button with guards — billing, assignment, GST recalculation."""
    order     = ctx.order
    all_lines = ctx.all_lines

    from modules.utils.submit_guard import is_locked, guarded_submit
    from .fulfillment import check_assignment_guard

    if st.button(
        "💾 SAVE TO ORDER",
        type="primary",
        use_container_width=True,
        key="final_save_order",
        disabled=is_locked("final_save"),
    ):
        with guarded_submit("final_save") as _allowed:
            if not _allowed:
                st.stop()

            total_billing = sum(l.get("billing_total", 0) for l in all_lines)

            # Guard 1: billing
            if total_billing <= 0:
                st.error("❌ Billing total invalid. Cannot save with zero or negative billing.")
                st.warning("Check line items and pricing before saving.")
                return

            # Guard 2: assignments
            # COUNTER_SALE orders: stock verified at cart build, all lines are STOCK-routed
            # with allocated_qty=quantity. check_assignment_guard auto-passes via
            # is_assignment_confirmed(session, all_lines) in decision_engine.
            if not check_assignment_guard(ctx):
                return

            # Guard 3: GST recalculation
            try:
                from modules.pricing.tax_engine import apply_taxes
                taxed = apply_taxes({
                    "order_type": order.get("order_type", "RETAIL"),
                    "net_value":  sum(float(l.get("billing_total") or 0) for l in all_lines),
                    "lines":      all_lines,
                })
                order["tax_amount"]  = taxed["tax_amount"]
                order["final_value"] = taxed["final_value"]
            except Exception as _tax_err:
                st.error(f"❌ GST recalculation failed — order NOT saved: {_tax_err}")
                st.stop()

            # Save
            try:
                from modules.persistence.order_persistence import save_order_to_db
                saved_id = save_order_to_db(order)
                for line in all_lines:
                    line["pricing_locked"] = True

                # Kernel post-save hook (audit flush, WhatsApp stub, etc.)
                after_save_hook(ctx, str(saved_id))

                st.success(f"✅ Order {order.get('order_no', saved_id)} saved successfully!")
                st.balloons()

            except Exception as _save_err:
                try:
                    from modules.core.error_logger import log_error
                    log_error(
                        _save_err,
                        context="backoffice.save_to_order",
                        payload={"order_no": order.get("order_no")},
                    )
                except Exception:
                    pass
                st.error(f"❌ Save failed: {_save_err}")


# ═══════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _eye_sort_key(line: Dict) -> int:
    eye = line.get("eye_side", "").upper()
    return 0 if eye in ("RIGHT", "R") else (1 if eye in ("LEFT", "L") else 2)


def _build_product_groups(order: Dict, all_lines: List[Dict]) -> dict:
    groups = {}
    for idx, line in enumerate(all_lines):
        pair_id = (
            line.get("pair_id")
            or f"{get_display_order_id(order)}_{line.get('product_id', '')}_{line.get('eye_side', '')}"
        )
        if pair_id not in groups:
            groups[pair_id] = {
                "product_name": line.get("product_name", "N/A"),
                "brand":        line.get("brand", "N/A"),
                "R": None, "L": None, "R_idx": None, "L_idx": None,
            }
        eye = line.get("eye_side", "").upper()
        if eye in ("RIGHT", "R"):
            groups[pair_id]["R"]     = line
            groups[pair_id]["R_idx"] = idx
        elif eye in ("LEFT", "L"):
            groups[pair_id]["L"]     = line
            groups[pair_id]["L_idx"] = idx
    return groups


def _render_product_sync_option(group: Dict, product_id: str) -> None:
    if group["R"] and group["L"]:
        st.markdown("---")
        _, col_label = st.columns([1, 5])
        with col_label:
            sync_on = st.checkbox(
                "Apply product changes to both eyes simultaneously",
                value=st.session_state.get(f"sync_product_{product_id}", False),
                key=f"sync_product_{product_id}",
                help="When enabled, changing on one eye updates both R and L",
            )
            if sync_on:
                st.caption("🔗 Both R and L eyes will use the same product")



def _is_blank_locked(line: Dict) -> bool:
    """
    Returns True if a blank has been allocated AND job card saved for this line.

    Primary check: lens_params.surfacing_data in DB (reliable — written by save_job_card_line).
    Fallback: blank_allocations table (may not exist in all deployments).
    Secondary: session state jc_computed_ key (blank chosen but not yet saved).

    Once locked, power and product edits are blocked until job card is reset/cancelled.
    """
    import streamlit as _stlk
    import json as _jbl
    lid = (line.get("line_id") or line.get("id") or "").strip()

    # 1. In-memory: surfacing_data already on line dict (unpacked by order_loader)
    if line.get("surfacing_data"):
        return True

    # 1b. lens_params blob on dict (covers all reload paths)
    lp_raw = line.get("lens_params") or {}
    if isinstance(lp_raw, str):
        try: lp_raw = _jbl.loads(lp_raw)
        except: lp_raw = {}
    if isinstance(lp_raw, dict) and lp_raw.get("surfacing_data"):
        return True

    # 2. Session state: blank chosen in this session but not yet saved
    #    (lock on selection, not just on DB save)
    if lid:
        eye = (line.get("eye_side") or "X").upper()
        _ck = f"jc_computed_jc_{lid}_{eye}"
        _pk_prefix = f"selected_blank_jc_pair_"
        pid = str(line.get("product_id") or "")
        ono = str(line.get("order_no") or "")
        _pair_k = f"selected_blank_jc_pair_{pid}_{ono}_{lid[:8]}"
        if _stlk.session_state.get(_ck) or _stlk.session_state.get(_pair_k):
            return True

    # 3. DB check: lens_params in order_lines (catches reloaded orders)
    if lid:
        try:
            import json as _jlk
            from modules.sql_adapter import run_query as _rqlk
            rows = _rqlk(
                "SELECT lens_params FROM order_lines WHERE id=%(l)s::uuid LIMIT 1",
                {"l": lid}
            )
            if rows:
                lp = rows[0].get("lens_params") or {}
                if isinstance(lp, str):
                    try: lp = _jlk.loads(lp)
                    except: lp = {}
                if lp.get("surfacing_data"):
                    return True
        except Exception:
            pass

    # 4. blank_allocations table (may not exist — best effort)
    if lid:
        try:
            from modules.sql_adapter import run_query as _rq2
            rows = _rq2(
                "SELECT id FROM blank_allocations WHERE order_line_id=%(l)s::uuid LIMIT 1",
                {"l": lid}
            )
            if rows:
                return True
        except Exception:
            pass  # table may not exist

    return False


def _render_blank_lock_banner(line: Dict) -> None:
    """Show a lock banner for blank-allocated lines."""
    eye = (line.get("eye_side") or "").upper()[:1]
    eye_label = {"R": "Right Eye", "L": "Left Eye"}.get(eye, "This line")
    st.markdown(
        f"<div style='background:#1a1000;border:1px solid #f59e0b;"
        f"border-radius:6px;padding:8px 14px;margin-bottom:8px'>"
        f"<span style='color:#fbbf24;font-weight:700'>🔒 {eye_label} — Blank Allocated</span>"
        f"<span style='color:#fde68a;font-size:0.8rem;margin-left:10px'>"
        f"Product and power are locked. Go to <b>Documents → Job Cards</b> "
        f"to cancel or reset the job card first.</span>"
        f"</div>",
        unsafe_allow_html=True
    )

def _render_eye_block(ctx: BackofficeContext, line: Dict, idx: int, eye_label: str) -> None:
    """Single eye block — product info, power, qty, allocation."""
    order = ctx.order

    label = "### 👁 RIGHT EYE" if eye_label == "R" else "### 👁 LEFT EYE"
    st.markdown(label)

    _locked = _is_blank_locked(line)

    with st.container(border=True):
        if _locked:
            _render_blank_lock_banner(line)

        # Product info
        st.markdown("#### 📦 Product Information")
        col_info, col_edit = st.columns([3, 1])
        with col_info:
            # Build frame description line from lens_params (SKU / Frame Group / Colour)
            _lp = line.get('lens_params') or {}
            _lp_filled = {k: v for k, v in _lp.items() if v}
            # Fallback for old orders where lens_params was saved empty
            if not _lp_filled and line.get('frame_group'):
                if line.get('batch_no'):    _lp_filled['batch_no']    = line['batch_no']
                if line.get('frame_group'): _lp_filled['frame_group'] = line['frame_group']
                if line.get('colour_mix'):  _lp_filled['colour_mix']  = line['colour_mix']
            _lp_label_map = {'batch_no': 'SKU', 'frame_group': 'Frame Group', 'colour_mix': 'Colour'}
            _lp_parts = [
                f"{_lp_label_map.get(k, k.replace('_',' ').title())}: {v}"
                for k, v in _lp_filled.items()
                if k in ('batch_no', 'frame_group', 'colour_mix')
            ]
            _frame_desc = "  \n**Frame:** " + " | ".join(_lp_parts) if _lp_parts else ""
            _qty_display = int(line.get('billing_qty') or line.get('requested_qty') or 1)
            st.info(
                f"**Product:** {line.get('product_name', 'N/A')}\n\n"
                f"**Brand:** {line.get('brand', 'N/A')} | "
                f"**Category:** {line.get('main_group', 'N/A')} | "
                f"**Qty:** {_qty_display}"
                + _frame_desc
            )
        with col_edit:
            if not _locked:
                if st.button("✏️ Change", key=f"change_product_{eye_label}_{idx}", use_container_width=True):
                    ctx.session["bo_product_change_modal"] = {
                        "active": True, "line": line, "idx": idx,
                        "eye_label": eye_label, "order": order,
                    }
                    st.rerun()
            else:
                st.caption("🔒 Locked")

        st.markdown("---")

        # Power
        st.markdown("#### 🔭 Power")
        is_editing = ctx.session.get("bo_editing_line") == idx
        if _locked:
            _render_power_display(line)
            st.caption("🔒 Power locked — blank allocated")
        elif not is_editing:
            _render_power_display(line)
            if st.button("✏️ Edit", key=f"edit_{eye_label}_{idx}", use_container_width=True):
                ctx.session["bo_editing_line"] = idx
                st.rerun()
        else:
            render_power_edit_ui(line, idx, order)

        st.markdown("---")

        # Qty
        _render_qty_block(ctx, line, idx, eye_label)

        # Allocation display
        _render_allocation_display(ctx, line, idx, eye_label)


def _render_power_display(line: Dict) -> None:
    if line.get("sph") is None:
        return
    import math as _m
    cyl = line.get("cyl")
    add = line.get("add_power")
    has_cyl = cyl is not None and not (isinstance(cyl, float) and _m.isnan(cyl)) and abs(float(cyl or 0)) > 0.01
    has_add = add is not None and not (isinstance(add, float) and _m.isnan(add)) and float(add or 0) > 0
    parts = [f"SPH {fmt_signed(line.get('sph'))}"]
    if has_cyl:
        parts.append(f"CYL {fmt_signed(cyl)}")
        if line.get("axis") is not None:
            parts.append(f"AXIS {line.get('axis')}")
    if has_add:
        parts.append(f"ADD {fmt_signed(add)}")
    st.info("**RX:** " + " | ".join(parts))


def _render_qty_block(ctx: BackofficeContext, line: Dict, idx: int, eye_label: str) -> None:
    order = ctx.order
    st.markdown("#### 📦 Quantity")
    current_qty = int(line.get("billing_qty") or 1)
    new_qty = st.number_input(
        "Order Quantity", min_value=1, value=current_qty, step=1,
        key=f"clean_qty_{eye_label}_{idx}",
    )
    if new_qty != current_qty:
        line["billing_qty"]      = int(new_qty)
        line["batch_allocation"] = []
        line["allocated_qty"]    = 0
        line["batch_status"]     = "PENDING"
        suggested = line.get("suggested_allocation")
        if suggested:
            suggested_total = sum(b.get("allocated_qty", 0) for b in suggested)
            if suggested_total == int(new_qty):
                line["batch_allocation"] = suggested
                st.info("✅ Reusing suggested allocation from Retail")
            else:
                st.warning("⚠️ Quantity changed — suggested allocation discarded")
        refresh_line_state(line)
        recalculate_order_totals(order)
        try:
            from .backoffice_helpers import categorize_order_lines
            categorize_order_lines(order)
        except ImportError:
            pass
        ctx.record("qty_changed", {"line": line.get("product_name"), "qty": new_qty})
        st.success(f"✅ Quantity updated to {new_qty}")
        st.rerun()


def _render_allocation_display(ctx: BackofficeContext, line: Dict, idx: int, eye_label: str) -> None:
    st.markdown("---")
    st.markdown("#### 📦 Allocation")
    allocated   = int(line.get("allocated_qty") or 0)
    billing_qty = int(line.get("billing_qty") or 0)
    pending     = max(0, billing_qty - allocated)
    if allocated > 0:
        st.success(f"✅ Allocated: {allocated}")
    if pending > 0:
        st.warning(f"⚠️ To Order: {pending}")
    if allocated == 0 and pending == 0:
        st.info("ℹ️ Not allocated")
    if st.button("🗂️ Manage Stock", key=f"alloc_{eye_label}_{idx}", use_container_width=True):
        ctx.session["bo_show_allocation_window"] = True
        ctx.session["bo_allocation_line_idx"]    = idx
        st.rerun()


def _render_billing_summary_table(ctx: BackofficeContext) -> None:
    """R/L breakdown billing table — includes service charges in grand total."""
    all_lines = ctx.all_lines
    order_id  = str(ctx.order.get("id") or "")

    r_lines     = [l for l in all_lines if l.get("eye_side", "").upper() in ("R", "RIGHT")]
    l_lines     = [l for l in all_lines if l.get("eye_side", "").upper() in ("L", "LEFT")]
    other_lines = [l for l in all_lines if l.get("eye_side", "").upper() not in ("R", "RIGHT", "L", "LEFT")]

    r_billing     = sum(l.get("billing_total", 0) for l in r_lines)
    l_billing     = sum(l.get("billing_total", 0) for l in l_lines)
    other_billing = sum(l.get("billing_total", 0) for l in other_lines)
    total_billing = r_billing + l_billing + other_billing
    total_disc    = sum(l.get("discount_amount", 0) for l in all_lines)

    # Fetch service charges
    _svc_total = 0.0
    if order_id:
        try:
            from modules.backoffice.order_charges_panel import fetch_charges
            _svc_charges = fetch_charges(order_id) or []
            _svc_total   = round(sum(float(c.get("total_amount") or 0) for c in _svc_charges), 2)
        except Exception:
            pass

    _grand = round(total_billing + _svc_total, 2)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Items",    len(all_lines))
    c2.metric("Total Discount", f"₹{total_disc:.2f}")
    c3.metric("Lens Total",     f"₹{total_billing:.2f}")
    c4.metric("**Grand Total**",  f"₹{_grand:.2f}",
              delta=f"+₹{_svc_total:,.2f} services" if _svc_total > 0 else None,
              delta_color="normal")

    st.markdown("---")

    def _eye_table(lines: List[Dict], label: str, subtotal: float) -> None:
        with st.container(border=True):
            st.markdown(f"#### {label}")
            st.caption(f"{len(lines)} items | Subtotal: ₹{subtotal:.2f}")
            if not lines:
                st.caption(f"No {label} items")
                return
            headers = ["Product", "Qty", "Unit Price", "Discount", "GST%", "GST Amt", "Total"]
            hcols   = st.columns([3, 2, 1, 1, 1, 1, 1])
            for h, t in zip(hcols, headers):
                h.caption(t)
            st.markdown("<hr style='margin:2px 0 6px 0'>", unsafe_allow_html=True)
            for n, line in enumerate(lines, 1):
                qty   = int(line.get("billing_qty", 0) or 0)
                # Use governor reverse_qty for consistent box/pair/pcs display
                _rqty = reverse_qty(qty, line)
                qty_d = _rqty["display"]
                gst_pct  = float(line.get("gst_percent_used") or line.get("gst_percent") or 0)
                gst_amt  = float(line.get("gst_amount") or 0)
                tax_inc  = line.get("tax_inclusive", True)
                gst_lbl  = f"{gst_pct:.0f}%" + (" (incl)" if tax_inc else " (+)") if gst_pct else "⚠️ Not set"
                disc_pct = float(line.get("discount_percent") or 0)
                up       = normalize_to_pcs_price(float(line.get("unit_price") or 0), line)
                tot      = float(line.get("billing_total") or line.get("total_price") or 0)
                lcols    = st.columns([3, 2, 1, 1, 1, 1, 1])
                lcols[0].write(f"{n}. {line.get('product_name', 'N/A')}")
                lcols[1].write(qty_d)
                lcols[2].write(f"₹{up:,.2f}")
                lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                lcols[4].write(gst_lbl)
                lcols[5].write(f"₹{gst_amt:,.2f}" if gst_amt else ("⚠️" if gst_pct else "—"))
                lcols[6].write(f"₹{tot:,.2f}")

    _eye_table(r_lines,     "👁 Right Eye",    r_billing)
    _eye_table(l_lines,     "👁 Left Eye",     l_billing)
    if other_lines:
        _eye_table(other_lines, "📋 Other Items", other_billing)

    st.markdown("---")


def _render_gst_verification(ctx: BackofficeContext) -> None:
    order      = ctx.order
    all_lines  = ctx.all_lines
    order_id   = str(order.get("id") or "")
    order_type = order.get("order_type", "RETAIL")

    gst_total   = sum(float(l.get("gst_amount") or 0) for l in all_lines)
    taxable_val = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in all_lines)

    # Fetch service charges — same source of truth as billing summary
    svc_base = 0.0; svc_gst = 0.0; svc_total = 0.0
    if order_id:
        try:
            from modules.backoffice.order_charges_panel import fetch_charges
            for c in (fetch_charges(order_id) or []):
                svc_base  += float(c.get("amount")       or 0)
                svc_gst   += float(c.get("gst_amount")   or 0)
                svc_total += float(c.get("total_amount")  or 0)
            svc_base  = round(svc_base, 2)
            svc_gst   = round(svc_gst, 2)
            svc_total = round(svc_total, 2)
        except Exception:
            pass

    with st.container(border=True):
        st.caption("📊 Billing Verification Summary")
        vc = st.columns(4)
        if order_type == "RETAIL":
            lens_mrp    = round(taxable_val, 2)
            lens_gst    = round(gst_total, 2)
            patient_pays = round(lens_mrp + svc_total, 2)
            vc[0].metric("MRP Total (incl. GST)", f"₹{lens_mrp:,.2f}")
            vc[1].metric("GST Extracted",          f"₹{lens_gst:,.2f}",
                         help="Back-calculated from MRP")
            vc[2].metric("Services",               f"₹{svc_total:,.2f}",
                         help="Fitting / colouring / courier added in backoffice")
            vc[3].metric("Patient Pays",           f"₹{patient_pays:,.2f}",
                         delta=f"+₹{svc_total:,.2f} services" if svc_total > 0 else None,
                         delta_color="normal")
        else:
            subtotal    = round(taxable_val, 2)
            grand_lens  = round(taxable_val + gst_total, 2)
            grand_total = round(grand_lens + svc_total, 2)
            vc[0].metric("Subtotal (excl. GST)",  f"₹{subtotal:,.2f}")
            vc[1].metric("GST Added",              f"₹{gst_total:,.2f}",
                         help="Added on top of selling price")
            vc[2].metric("Services",               f"₹{svc_total:,.2f}",
                         help="Fitting / colouring / courier added in backoffice")
            vc[3].metric("Grand Total",            f"₹{grand_total:,.2f}",
                         delta=f"+₹{svc_total:,.2f} services" if svc_total > 0 else None,
                         delta_color="normal")
        st.caption(
            f"Source: {order.get('order_source', order_type)}  |  "
            f"Tax: {'GST inclusive' if order_type == 'RETAIL' else 'GST exclusive — added on top'}"
            + (f"  |  Services: ₹{svc_base:,.2f} base + ₹{svc_gst:,.2f} GST" if svc_total > 0 else "")
        )


def _apply_product_change(line: Dict, product_row, eye_label: str, idx: int, order: Dict, ctx: BackofficeContext) -> None:
    """Apply a product change to a line dict."""
    line["product_id"]   = str(product_row["product_id"])
    line["product_name"] = str(product_row["product_name"])
    line["brand"]        = str(product_row.get("brand", ""))
    line["main_group"]   = str(product_row.get("main_group", ""))
    line["material"]     = str(product_row.get("material", ""))
    line["type"]         = str(product_row.get("type", ""))
    line["unit"]         = str(product_row.get("unit", ""))
    line["box_size"]     = int(product_row.get("box_size") or 1)
    line["unit_price"]   = 0
    line["billing_total"] = 0
    line.pop("pricing_applied_at", None)
    mg_lower = str(product_row.get("main_group", "")).lower()
    line["is_contact"] = "contact" in mg_lower
    line["is_lens"]    = "lens" in mg_lower or "spectacle" in mg_lower
    line["batch_allocation"]    = []
    line["allocated_qty"]       = 0
    line["batch_status"]        = "PENDING"
    line["manufacturing_route"] = None
    line["supplier_order_id"]   = None
    temp_key = f"temp_alloc_{eye_label}_{idx}"
    if temp_key in ctx.session:
        del ctx.session[temp_key]
    update_manufacturing_power(line)
    order.setdefault("product_change_history", []).append({
        "timestamp":   datetime.datetime.now().isoformat(),
        "eye_side":    line.get("eye_side"),
        "new_product": product_row["product_name"],
        "changed_by":  ctx.user.get("name", "backoffice_user"),
    })
    refresh_line_state(line)
    ctx.session["current_order"] = order
    recalculate_order_totals(order)
