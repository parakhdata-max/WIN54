"""
Backoffice UI Module
Streamlit UI components for backoffice management

Contains:
- Product info display and editing
- Power editing UI
- Lens parameters editing
- Boxing parameters editing
- Quantity management (CLEAN ARCHITECTURE)
- Allocation window
- Supplier order section
- Document generation (job cards, lab orders, labels)
- Order detail rendering
- Status update modal
- 🔐 Billing safeguards (lock, pricing freeze, debug toggle)

=============================================================================
 CLEAN QUANTITY ARCHITECTURE - SINGLE SOURCE OF TRUTH
=============================================================================

MASTER FIELDS (per line):
    billing_qty       What customer ordered (MASTER)
    allocated_qty     What's from stock
    pending_qty       CALCULATED: billing_qty - allocated_qty
    batch_allocation  Stock breakdown
    manufacturing_route  STOCK / VENDOR / INHOUSE / EXTERNAL_LAB

WORKFLOW:
    User changes quantity
        
    billing_qty updated
        
    Stock state reset (batch_allocation, allocated_qty, batch_status)
        
    refresh_line_state(line) called
        
    Allocation engine runs (clean recomputation)
        
    allocated_qty + pending_qty calculated
        
    Pricing engine runs
        
    billing_total updated

DELETED FIELDS:
     order_qty (replaced by pending_qty calculation)
     final_qty (replaced by billing_qty)
     Manual pending calculations in UI

RULES:
    - UI ONLY edits billing_qty
    - Workflow Engine sets allocation + route
    - Pricing Engine calculates billing_total
    - Allocation Window ONLY edits batch split
    - NO create_allocation_record() calls in UI
    - NO manual route setting in UI

=============================================================================
 🔐 BILLING SAFEGUARDS (Prevents Future Corruption)
=============================================================================

1. BILLING LOCK (Save Validation):
   - Prevents saving orders with total_billing <= 0
   - Catches silent corruption before it hits database
   - Shows clear error message to user

2. PRICING FREEZE (Optional):
   - Sets pricing_locked=True on all lines after save
   - Prevents accidental recomputation after billing finalized
   - Can be checked before running pricing engine

3. DEBUG TOGGLE:
   - Hidden checkbox in Billing tab: "🔍 Debug Pricing"
   - Shows full JSON of each line item
   - Includes: billing_qty, allocated_qty, pending_qty, unit_price,
     billing_total, manufacturing_route, batch_allocation, pricing_locked
   - Helps troubleshoot pricing issues

USAGE:
    # Before saving
    if total_billing <= 0:
        st.error("Billing invalid. Cannot save.")
        return
    
    # After saving (optional)
    for line in all_lines:
        line["pricing_locked"] = True
    
    # In debug mode
    if st.checkbox("Debug Pricing"):
        st.json(line)

=============================================================================
"""

import streamlit as st
import pandas as pd
import datetime
import uuid
from typing import Dict, List, Optional
from modules.workflow.status import OrderStatus

# Import core dependencies
from modules.sql_adapter import read_product_master
from modules.documents.job_engine import generate_job_card_data
from modules.supplier_orders_management import (
    get_vendor_routed_lines,
    create_supplier_order_from_lines,
    add_supplier_order_button_to_backoffice
)
from .backoffice_logic import refresh_line_state


# Import from other backoffice modules
from .backoffice_helpers import (
    fmt_num,
    fmt_signed,
    get_display_order_id,
    get_display_label,
    power_key,
    sync_power_to_ui,
    force_power_refresh
)
from .backoffice_logic import (
    update_manufacturing_power,
    update_batch_allocation,
    update_line_billing,
    recalculate_order_totals,
    guard_price_mutation,
)
from .backoffice_helpers import load_orders_from_database
from .backoffice_panels import (
    render_power_edit_ui,
    render_lens_params_edit_ui,
    render_boxing_params_edit_ui,
    render_allocation_window,
)

# Assignment panel — supplier / job-card allocation before save
try:
    from .assignment_panel import (
        render_assignment_panel,
        init_assignment_state,
    )
    _ASSIGNMENT_PANEL_AVAILABLE = True
except ImportError:
    _ASSIGNMENT_PANEL_AVAILABLE = False

# Import sidebar component
try:
    from .backoffice_sidebar import render_backoffice_sidebar
except ImportError:
    # Sidebar is optional - if not available, just skip it
    render_backoffice_sidebar = None


# ==========================================================
# SESSION STATE INITIALISATION
# ==========================================================

def init_backoffice_state():
    """
    Initialise all bo_ session state keys used by backoffice_ui.
    Safe to call on every render — only sets keys that don't exist yet.
    """
    defaults = {
        "bo_view_mode":              "dashboard",
        "bo_selected_order_id":      None,
        "bo_active_orders":          [],
        "bo_orders_loaded":          False,
        "bo_editing_line":           None,
        "bo_show_allocation_window": False,
        "bo_allocation_line_idx":    None,
        "bo_product_change_modal":   {"active": False},
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

    # Assignment panel state
    if _ASSIGNMENT_PANEL_AVAILABLE:
        init_assignment_state()

def render_product_info_display(line: Dict, idx: int, eye_label: str, order: Dict):
    """
    Display product and brand information with edit capability
    
    Args:
        line: Line item dictionary
        idx: Line index  
        eye_label: 'R' or 'L' for right/left eye
        order: Parent order dict
    """
    st.markdown("####  Product Information")
    
    # Display mode - always show product info
    col_info, col_edit = st.columns([3, 1])
    
    with col_info:
        product_name = line.get('product_name', 'N/A')
        brand = line.get('brand', 'N/A')
        main_group = line.get('main_group', 'N/A')
        unit = line.get('unit', 'N/A')
        
        st.info(
            f"**Product:** {product_name}\n\n"
            f"**Brand:** {brand}\n\n"
            f"**Category:** {main_group}\n\n"
            f"**Unit:** {unit}"
        )
    
    with col_edit:
        if st.button(" Change", key=f"change_product_{eye_label}_{idx}", use_container_width=True):
            #  FIX: Set modal state instead of inline edit
            st.session_state['bo_product_change_modal'] = {
                'active': True,
                'line': line,
                'idx': idx,
                'eye_label': eye_label,
                'order': order
            }
            st.rerun()


def render_product_sync_option(group: Dict, product_id: str):
    """
    Render option to sync product change across both eyes
    
    Args:
        group: Product group containing R and L lines
        product_id: Current product ID
    """
    if group['R'] and group['L']:
        st.markdown("---")
        col_icon, col_label = st.columns([1, 5])
        
        with col_icon:
            st.markdown("")
        with col_label:
            sync_enabled = st.checkbox(
                "Apply product changes to both eyes simultaneously",
                value=st.session_state.get(f'sync_product_{product_id}', False),
                key=f'sync_product_{product_id}',
                help="When enabled, changing the product on one eye will automatically update both R and L"
            )
            
            if sync_enabled:
                st.caption(" Both R and L eyes will use the same product")


@st.dialog(" Change Product", width="large")
def product_change_dialog():
    """
     COMPACT DIALOG: Uses simple dropdowns instead of full product selector
    """
    modal_state = st.session_state.get('bo_product_change_modal', {})
    
    if not modal_state.get('active', False):
        return
    
    line = modal_state['line']
    idx = modal_state['idx']
    eye_label = modal_state['eye_label']
    order = modal_state['order']
    
    st.warning(f" Changing product for **{eye_label} Eye** - Line #{idx + 1}")
    st.caption("Current: " + line.get('product_name', 'N/A') + " | " + line.get('brand', 'N/A'))
    
    st.markdown("---")
    
    #  Simple compact product selection using just the product master
    from modules.sql_adapter import read_product_master
    products_df = read_product_master()
    
    if products_df.empty:
        st.error("No products available")
        if st.button("Close", key="close_product_modal_no_products"):
            st.session_state['bo_product_change_modal'] = {'active': False}
            st.rerun()
        return
    
    # Quick filters
    col1, col2 = st.columns(2)
    
    with col1:
        main_groups = [''] + sorted(products_df['main_group'].dropna().astype(str).unique().tolist())
        selected_group = st.selectbox("Category", main_groups, key="dialog_main_group")
    
    with col2:
        brands = [''] + sorted(products_df['brand'].dropna().astype(str).unique().tolist())
        selected_brand = st.selectbox("Brand", brands, key="dialog_brand")
    
    # Filter products
    filtered_df = products_df.copy()
    if selected_group:
        filtered_df = filtered_df[filtered_df['main_group'].astype(str) == selected_group]
    if selected_brand:
        filtered_df = filtered_df[filtered_df['brand'].astype(str) == selected_brand]
    
    # Product dropdown
    product_list = sorted(filtered_df['product_name'].dropna().astype(str).unique().tolist())
    
    if not product_list:
        st.warning("No products match the filters")
        if st.button("Close", key="close_product_modal_no_match"):
            st.session_state['bo_product_change_modal'] = {'active': False}
            st.rerun()
        return
    
    selected_product = st.selectbox("Select Product *", [''] + product_list, key="dialog_product")
    
    if selected_product:
        product_row = filtered_df[filtered_df['product_name'] == selected_product].iloc[0]
        
        st.success(f" **Selected:** {product_row['product_name']} | {product_row.get('brand', 'N/A')}")
        
        st.markdown("---")
        
        col_apply, col_cancel = st.columns(2)
        
        with col_apply:
            if st.button(" Apply Change", type="primary", use_container_width=True):
                old_product = line.get('product_name', 'Unknown')
                
                # Update product fields
                line['product_id'] = str(product_row['product_id'])
                line['product_name'] = str(product_row['product_name'])
                line['brand'] = str(product_row.get('brand', ''))
                line['main_group'] = str(product_row.get('main_group', ''))
                line['material'] = str(product_row.get('material', ''))
                line['type'] = str(product_row.get('type', ''))
                line['unit'] = str(product_row.get('unit', ''))
                line['box_size'] = int(product_row.get('box_size') or 1)
                
                # Product changed → zero unit_price so refresh_line_state
                # derives a fresh price from the new product's batch.
                line['unit_price']    = 0
                line['billing_total'] = 0
                line.pop('pricing_applied_at', None)
                # Determine product type for is_lens/is_contact flags
                main_group = str(product_row.get('main_group', '')).lower()
                line['is_contact'] = 'contact' in main_group
                line['is_lens'] = 'lens' in main_group or 'spectacle' in main_group
                
                #  FULL RESET before workflow
                line['batch_allocation'] = []
                line['allocated_qty'] = 0
                line['batch_status'] = 'PENDING'
                line['manufacturing_route'] = None
                line['supplier_order_id'] = None
                #  FIX: billing_qty remains unchanged during product change
                
                # Clear temp allocation state
                temp_key = f'temp_alloc_{eye_label}_{idx}'
                if temp_key in st.session_state:
                    del st.session_state[temp_key]
                
                # Recalculate manufacturing power
                update_manufacturing_power(line)
                

                
                # Log change
                if 'product_change_history' not in order:
                    order['product_change_history'] = []
                
                import datetime
                order['product_change_history'].append({
                    'timestamp': datetime.datetime.now().isoformat(),
                    'eye_side': line.get('eye_side'),
                    'old_product': old_product,
                    'new_product': product_row['product_name'],
                    'changed_by': 'backoffice_user'
                })
                
                #  Recalculate after product change
                refresh_line_state(line)
                
                order_no = None

                if order and isinstance(order, dict):
                    order_no = order.get("order_no")

                else:
                    st.warning(" No active order to reload")
                    order = None

                if order:
                    st.session_state.current_order = order
                    recalculate_order_totals(order)

                # Close dialog
                st.session_state['bo_product_change_modal'] = {'active': False}
                st.success(f" Product changed!")
                st.rerun()
        
        with col_cancel:
            if st.button(" Cancel", use_container_width=True, key="cancel_product_change_col"):
                st.session_state['bo_product_change_modal'] = {'active': False}
                st.rerun()
    else:
        if st.button(" Cancel", use_container_width=True, key="cancel_product_change_else"):
            st.session_state['bo_product_change_modal'] = {'active': False}
            st.rerun()


# ============================================================================
# ============================================================================

def show_supplier_order_section(order: Dict):
    """
    Enhanced supplier order section with diagnostics
    Shows button if vendor lines exist, otherwise shows why not
    
    SUPPORTS BOTH:
    - VENDOR route (external suppliers)
    - EXTERNAL_LAB route (lab orders that need supplier procurement)
    """
    from modules.supplier_orders_management import create_supplier_order_from_lines
    
    # Get ALL lines
    all_lines = []
    all_lines.extend(order.get('stock_lines', []))
    all_lines.extend(order.get('inhouse_lines', []))
    all_lines.extend(order.get('lab_order_lines', []))
    
    #  FIX: Include BOTH VENDOR and EXTERNAL_LAB routes
    vendor_lines = [
        line for line in all_lines 
        if line.get('manufacturing_route') in ['VENDOR', 'EXTERNAL_LAB'] and
        not line.get('supplier_order_id')  # Not already ordered
    ]
    
    # Always show section for visibility
    st.markdown("---")
    st.markdown("###  Supplier Orders")
    
    if vendor_lines:
        if st.button(
            f"📦 Create Supplier Order ({len(vendor_lines)} items)",
            type="primary",
            use_container_width=True,
            key="create_supplier_order_main_btn"
        ):
            create_supplier_order_from_lines(order, vendor_lines)
    else:
        # No vendor lines - show diagnostics
        if not all_lines:
            st.warning(" No order lines found in this order")
        else:
            # Check routing
            routes = {}
            already_ordered = 0
            
            for line in all_lines:
                route = line.get('manufacturing_route', 'NOT_SET')
                routes[route] = routes.get(route, 0) + 1
                
                if line.get('supplier_order_id'):
                    already_ordered += 1
            
            # Show diagnostics
            st.info(f" **Order has {len(all_lines)} line items:**")
            
            for route, count in routes.items():
                if route == 'VENDOR':
                    st.success(f" {count} items routed to VENDOR")
                elif route == 'EXTERNAL_LAB':
                    st.success(f" {count} items routed to EXTERNAL_LAB (supplier procurement needed)")
                elif route == 'STOCK':
                    st.info(f" {count} items routed to STOCK (from inventory)")
                elif route == 'INHOUSE':
                    st.info(f" {count} items routed to INHOUSE (manufacture internally)")
                elif route == 'LAB_ORDER':
                    st.info(f" {count} items routed to LAB_ORDER")
                else:
                    st.warning(f" {count} items with route: {route}")
            
            if already_ordered > 0:
                st.success(f" {already_ordered} items already have supplier orders")
            
            # Help message
            if 'VENDOR' not in routes and 'EXTERNAL_LAB' not in routes:
                st.info("""
                 **No vendor items in this order**
                
                Items are routed to vendors when:
                - Product is not in stock
                - Product cannot be manufactured in-house
                - Specific vendor is required
                
                To route items to vendor, check the manufacturing route settings.
                """)
            elif already_ordered == (routes.get('VENDOR', 0) + routes.get('EXTERNAL_LAB', 0)):
                st.success(" All vendor items already have supplier orders created")
    
    st.markdown("---")


# ============================================================================
# ============================================================================

def generate_job_cards(order: Dict):
    """
    Generate job cards with surfacing support.

    Layout logic:
    - If the order has exactly one R and one L line for the same product,
      render them SIDE BY SIDE (two columns) — this is the common progressive case.
    - Otherwise fall back to one expander per line (different products, or
      single-eye, or more than 2 lines).
    """

    from modules.documents.job_card_surfacing import (
        render_surfacing_job_card,
        render_job_card_print,
    )

    st.markdown("---")
    st.markdown("### 🔧 In-House Job Cards")

    inhouse_lines = order.get("inhouse_lines", [])

    if not inhouse_lines:
        st.warning("No in-house items requiring job cards")
        return

    # ── Group lines: try to pair R + L for same product ──────────────
    # Build a dict keyed by product_id (or product_name as fallback).
    # If a product has both R and L, show them side-by-side.
    from collections import defaultdict
    product_groups: dict = defaultdict(dict)   # {product_key: {"R": line, "L": line, ...}}

    for line in inhouse_lines:
        pk = line.get("product_id") or line.get("product_name") or "unknown"
        side = (line.get("eye_side") or "").upper().strip()
        if side in ("R", "L"):
            product_groups[pk][side] = line
        else:
            # Non-eye-specific — keep separately with a unique key
            product_groups[f"{pk}__{id(line)}"]["X"] = line

    rendered_line_ids = set()

    # ── Render paired R+L side-by-side, then any remaining singles ────
    for pk, sides in product_groups.items():
        r_line = sides.get("R")
        l_line = sides.get("L")

        if r_line is not None and l_line is not None:
            # ── Paired: show R and L in two columns ──────────────────
            product_name = r_line.get("product_name", "Unknown Product")

            with st.expander(
                f"👁️ {product_name} — Right & Left Eye",
                expanded=True,
            ):
                col_r, col_divider, col_l = st.columns([10, 1, 10])

                with col_r:
                    st.markdown(
                        "<div style='background:#1a3a2a;border-radius:8px;padding:6px 14px;"
                        "margin-bottom:10px;'>"
                        "<span style='color:#4ade80;font-weight:700;font-size:1rem;'>"
                        "👁 RIGHT EYE</span></div>",
                        unsafe_allow_html=True,
                    )
                    render_surfacing_job_card(r_line, order)
                    if r_line.get("surfacing_data"):
                        with st.expander("🖨 Print Preview — R", expanded=False):
                            render_job_card_print(r_line, order)

                with col_divider:
                    st.markdown(
                        "<div style='border-left:2px dashed #334155;height:100%;margin:0 auto;width:2px;'></div>",
                        unsafe_allow_html=True,
                    )

                with col_l:
                    st.markdown(
                        "<div style='background:#1a2a3a;border-radius:8px;padding:6px 14px;"
                        "margin-bottom:10px;'>"
                        "<span style='color:#60a5fa;font-weight:700;font-size:1rem;'>"
                        "👁 LEFT EYE</span></div>",
                        unsafe_allow_html=True,
                    )
                    render_surfacing_job_card(l_line, order)
                    if l_line.get("surfacing_data"):
                        with st.expander("🖨 Print Preview — L", expanded=False):
                            render_job_card_print(l_line, order)

            rendered_line_ids.add(id(r_line))
            rendered_line_ids.add(id(l_line))

        else:
            # ── Single line (one eye only, or non-eye-specific) ───────
            line = r_line or l_line or list(sides.values())[0]
            if id(line) in rendered_line_ids:
                continue
            eye_label = (line.get("eye_side") or "").upper().strip()
            eye_display = {"R": "Right Eye", "L": "Left Eye"}.get(eye_label, eye_label or "Lens")
            product_name = line.get("product_name", "Unknown Product")

            with st.expander(f"👁 {product_name} — {eye_display}", expanded=True):
                render_surfacing_job_card(line, order)
                if line.get("surfacing_data"):
                    with st.expander("🖨 Print Preview", expanded=False):
                        render_job_card_print(line, order)

            rendered_line_ids.add(id(line))

def generate_lab_orders(order: Dict):
    """Generate lab orders for external items"""
    st.markdown("---")
    st.markdown("###  Lab Orders")
    
    lab_lines = order.get('lab_order_lines', [])
    
    if not lab_lines:
        st.warning("No lab order items")
        return
    
    # Lab order summary
    st.markdown("#### Lab Order Summary")
    
    lab_data = []
    for line in lab_lines:
        #  FIX: Calculate pending qty for lab orders
        billing_qty = int(line.get('billing_qty', 0))
        allocated = int(line.get('allocated_qty', 0))
        pending = max(0, billing_qty - allocated)
        
        lab_data.append({
            'Product': line.get('product_name', 'N/A'),
            'Brand': line.get('brand', 'N/A'),
            'Eye': line.get('eye_side', 'N/A'),
            'SPH': fmt_signed(line.get('sph')),
            'CYL': fmt_signed(line.get('cyl')),
            'AXIS': line.get('axis', 'N/A'),
            'Qty': pending
        })
    
    st.dataframe(pd.DataFrame(lab_data))
    
    # Lab selection
    lab_name = st.selectbox(
        "Select Lab",
        ["Lab A - Premium Optics", "Lab B - Standard Optics", "Lab C - Express Optics"],
        key='lab_select'
    )
    
    expected_delivery = st.date_input(
        "Expected Delivery Date",
        value=datetime.date.today() + datetime.timedelta(days=7),
        key='lab_delivery_date'
    )
    
    from modules.utils.submit_guard import is_locked, guarded_submit
    if st.button(" Send Lab Order", type="primary", use_container_width=True,
                 disabled=is_locked("lab_order")):
        with guarded_submit("lab_order") as _allowed:
            if not _allowed:
                st.stop()
            try:
                from modules.backoffice.audit_logger import audit, AuditAction
                audit(AuditAction.LAB_ORDER_SENT, entity="orders",
                      entity_id=order.get("order_id"),
                      payload={"lab": lab_name, "delivery": str(expected_delivery)})
            except Exception:
                pass
            st.success(f" Lab order sent to {lab_name}")
            st.info(f"Expected delivery: {expected_delivery}")


def generate_labels(order: Dict):
    """Generate labels for stock items"""
    st.markdown("---")
    st.markdown("###  Product Labels")

    stock_lines = order.get('stock_lines', [])

    if not stock_lines:
        st.warning("No stock items requiring labels")
        return

    for idx, line in enumerate(stock_lines, 1):
        col1, col2 = st.columns([3, 2])

        with col1:
            st.markdown(f"#### Label #{idx}")
            st.text(f"Product: {line.get('product_name', 'N/A')}")
            st.text(f"Brand: {line.get('brand', 'N/A')}")
            st.text(f"Patient: {order.get('patient_name', 'N/A')}")
            st.text(f"Eye: {line.get('eye_side', 'N/A')}")

            #  FIX: Always render power in SPH CYL AXIS ADD format
            if line.get('sph') is not None:
                power_str = f"Power: SPH {fmt_signed(line.get('sph'))} | CYL {fmt_signed(line.get('cyl'))}"
                
                # Add AXIS if cylinder exists
                if abs(line.get('cyl') or 0) > 0.01:
                    power_str += f" | AXIS {line.get('axis', 'N/A')}"
                
                # Add ADD if present
                if line.get('add_power') is not None:
                    power_str += f" | ADD {fmt_signed(line.get('add_power'))}"
                
                st.text(power_str)

            # ===== BATCH INFO =====
            if line.get('batch_allocation'):
                batch_info = ", ".join(
                    b.get('batch_no', 'N/A') for b in line.get('batch_allocation', [])
                )
                st.text(f"Batch(es): {batch_info}")

        with col2:
            st.text(f"Order: {get_display_order_id(order)}")
            order_date = order.get("created_at")

            if order_date:
                if hasattr(order_date, "strftime"):
                    date_str = order_date.strftime("%Y-%m-%d")
                else:
                    date_str = str(order_date)[:10]
            else:
                date_str = "N/A"


            st.text(f"Date: {date_str}")

            st.text(f"Qty: {line.get('billing_qty', 0)}")

        st.markdown("---")

    st.success(f" {len(stock_lines)} label(s) ready for printing")

def render_qty_finalization_ui(line: Dict, line_idx: int, order: Dict):
    """
    Safe stub to prevent import errors.
    Quantity editing is now handled in main UI.
    """
    st.markdown("####  Quantity")

    current_qty = int(line.get("billing_qty", 1))

    new_qty = st.number_input(
        "Billing Quantity",
        min_value=1,
        value=current_qty,
        step=1,
        key=f"qty_stub_{line_idx}"
    )

    if new_qty != current_qty:
        line["billing_qty"] = new_qty

        #  CRITICAL FIX  Always update totals
        recalculate_order_totals(order)

        # Reset allocation so workflow recomputes
        line["batch_allocation"] = []
        line["allocated_qty"] = 0
        line["batch_status"] = "PENDING"

        refresh_line_state(line)

        st.success(f" Quantity updated to {new_qty}")
        st.rerun()


# ============================================================================
# ============================================================================

def render_order_detail():
    """
    Render detailed order view with all workflow components

    CRITICAL FIXES:
    1. Power editing triggers complete workflow
    2. Allocation window appears automatically
    3. Billing updates in real-time
    4. Ophthalmic job cards render correctly
    """

    # 
    # 1. Resolve order_id
    # 
    order_id = st.session_state.bo_selected_order_id

    if not order_id:
        st.warning("No order selected")
        if st.button(" Back to Dashboard", key="back_to_dashboard_no_order"):
            st.session_state.bo_view_mode = 'dashboard'
            st.rerun()
        return

    # Reset assignment panel state whenever a DIFFERENT order is opened
    _last_oid_key = "_bo_assignment_last_order_id"
    if st.session_state.get(_last_oid_key) != order_id:
        st.session_state[_last_oid_key]          = order_id
        st.session_state["bo_assignments"]        = {}
        st.session_state["bo_assignments_locked"] = False
        st.session_state["bo_shift_target"]       = None

    # 
    # 2. Find order in active list
    # 
    order = None
    for o in st.session_state.bo_active_orders:
        if get_display_order_id(o) == order_id:
            order = o
            break

    if not order:
        st.error("Order not found")
        if st.button(" Back to Dashboard", key="back_to_dashboard_order_not_found"):
            st.session_state.bo_view_mode = 'dashboard'
            st.rerun()
        return

    # 
    # 3. Lazy refresh  run workflow engine on lines that need it
    #    (lines loaded from DB have _needs_refresh=True)
    # 
    needs_rerun = False
    for line in order.get("lines", []):
        if line.get("_needs_refresh"):
            try:
                # refresh_line_state handles routing (STOCK/VENDOR) AND
                # recalculates billing_total via update_line_billing internally.
                # It does NOT change unit_price — that came from retail punching.
                refresh_line_state(line)
            except Exception as e:
                import logging
                logging.warning(f"[BO] refresh_line_state failed for line {line.get('product_name')}: {e}")
            finally:
                line["_needs_refresh"] = False
            needs_rerun = True

    if needs_rerun:
        # Recalculate order totals then re-categorize
        recalculate_order_totals(order)
        from .backoffice_helpers import categorize_order_lines
        categorize_order_lines(order)
    
    # =====================================================
    # RENDER SIDEBAR (if available)
    # =====================================================
    if render_backoffice_sidebar is not None:
        try:
            render_backoffice_sidebar(order)
        except Exception as e:
            import logging
            logging.warning(f"[BO] Sidebar render failed: {e}")

    # Header
    col1, col2 = st.columns([3, 1])
    
    with col1:
        st.title(f"📋 Order: {get_display_label(order)}")
    
    with col2:
        if st.button(" Back", use_container_width=True):
            st.session_state.bo_view_mode = 'dashboard'
            st.session_state.bo_editing_line = None
            st.session_state.bo_show_allocation_window = False
            st.rerun()
    
    # Order info
    col_a, col_b, col_c = st.columns(3)
    
    with col_a:
        st.metric("Patient", order.get('patient_name', 'N/A'))
    with col_b:
        st.metric("Status", order.get('status', 'PENDING'))

    with col_c:
        order_date = order.get('created_at', '')
        if order_date:
            date_str = str(order_date)[:10]
        else:
            date_str = 'N/A'

        st.metric("Date", date_str)

    
    #  NEW: Trigger product change dialog if modal is active
    if st.session_state.get('bo_product_change_modal', {}).get('active', False):
        product_change_dialog()
    
    # Build all_lines BEFORE tabs so every tab can access it
    all_lines = []
    all_lines.extend(order.get('stock_lines', []))
    all_lines.extend(order.get('inhouse_lines', []))
    all_lines.extend(order.get('lab_order_lines', []))

    # Tabs for different sections
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "📦 Order Items", 
        "📄 Documents", 
        "📊 Status", 
        "💰 Billing Summary",
        "🚚 Supplier Orders",
        "💳 Billing Gate"
    ])
    
    with tab1:
        st.markdown("###  Order Line Items")
        
        #  Sort lines - Right eye before Left eye
        def eye_sort_key(line):
            eye = line.get('eye_side', '').upper()
            if eye == 'RIGHT' or eye == 'R':
                return 0
            elif eye == 'LEFT' or eye == 'L':
                return 1
            else:
                return 2
        
        all_lines.sort(key=eye_sort_key)
        
        #  Group by product - R and L together
        product_groups = {}
        
        for idx, line in enumerate(all_lines):

            pair_id = (
                line.get('pair_id')
                or f"{get_display_order_id(order)}_{line.get('product_id','')}"
            )


            if pair_id not in product_groups:
                product_groups[pair_id] = {
                    'product_name': line.get('product_name', 'N/A'),
                    'brand': line.get('brand', 'N/A'),
                    'R': None,
                    'L': None,
                    'R_idx': None,
                    'L_idx': None
                }

            eye = line.get('eye_side', '').upper()

            if eye in ['RIGHT', 'R']:
                product_groups[pair_id]['R'] = line
                product_groups[pair_id]['R_idx'] = idx

            elif eye in ['LEFT', 'L']:
                product_groups[pair_id]['L'] = line
                product_groups[pair_id]['L_idx'] = idx

        # Display each product with R and L side by side
        for product_id, group in product_groups.items():
            # Compact product header
            st.markdown(f"####  {group['product_name']}")
            if 'brand' in group and group['brand'] != 'N/A':
                st.caption(f"Brand: {group['brand']}")
            
            #  NEW: Add product sync option
            render_product_sync_option(group, product_id)
            
            st.markdown("---")
            
            # ==================== RIGHT & LEFT EYE DYNAMIC LAYOUT ====================
            #  FIX: Dynamic column layout - stretch to fill horizontal space
            has_right = bool(group['R'])
            has_left = bool(group['L'])
            
            # Create columns only for eyes that have data
            if has_right and has_left:
                # Both eyes ordered - use 2 columns
                col_r, col_l = st.columns(2)
            elif has_right:
                # Only right eye - use full width
                col_r = st.container()
                col_l = None
            elif has_left:
                # Only left eye - use full width
                col_r = None
                col_l = st.container()
            else:
                # Neither eye ordered
                col_r = None
                col_l = None
            
            # ── Shared eye block renderer ────────────────────────────
            def _render_eye_block_ui(line, idx, eye_label):
                import math as _math

                eye_title = "👁 RIGHT EYE" if eye_label == "R" else "👁 LEFT EYE"
                eye_color = "#1a3a5c" if eye_label == "R" else "#1a3a2a"

                # Eye header bar
                st.markdown(
                    f"<div style='background:{eye_color};color:#fff;padding:8px 14px;"
                    f"border-radius:8px;font-weight:700;font-size:1rem;margin-bottom:10px'>"
                    f"{eye_title}</div>",
                    unsafe_allow_html=True,
                )

                with st.container(border=True):

                    # ── Product Info ──────────────────────────────────
                    st.markdown("##### 📦 Product")
                    col_info, col_edit = st.columns([3, 1])
                    with col_info:
                        st.info(
                            f"**{line.get('product_name','N/A')}**\n\n"
                            f"Brand: {line.get('brand','N/A')} · "
                            f"Cat: {line.get('main_group','N/A')} · "
                            f"Unit: {line.get('unit','N/A')}"
                        )
                    with col_edit:
                        if st.button("✏️ Change", key=f"change_product_{eye_label}_{idx}",
                                     use_container_width=True):
                            st.session_state['bo_product_change_modal'] = {
                                'active': True, 'line': line,
                                'idx': idx, 'eye_label': eye_label, 'order': order
                            }
                            st.rerun()

                    st.divider()

                    # ── Power ─────────────────────────────────────────
                    st.markdown("##### 🔭 Power")
                    is_editing = st.session_state.get('bo_editing_line') == idx

                    if not is_editing:
                        if line.get('sph') is not None:
                            _cyl = line.get('cyl')
                            _add = line.get('add_power')
                            _has_cyl = (_cyl is not None and
                                        not (isinstance(_cyl, float) and _math.isnan(_cyl))
                                        and abs(float(_cyl or 0)) > 0.01)
                            _has_add = (_add is not None and
                                        not (isinstance(_add, float) and _math.isnan(_add))
                                        and float(_add or 0) > 0)
                            parts = [f"SPH {fmt_signed(line.get('sph'))}"]
                            if _has_cyl:
                                parts.append(f"CYL {fmt_signed(_cyl)}")
                                if line.get('axis') is not None:
                                    parts.append(f"AXIS {line.get('axis')}")
                            if _has_add:
                                parts.append(f"ADD {fmt_signed(_add)}")
                            st.info("**RX:** " + " | ".join(parts))
                        if st.button("✏️ Edit Power", key=f"edit_{eye_label}_{idx}",
                                     use_container_width=True):
                            st.session_state.bo_editing_line = idx
                            st.rerun()
                    else:
                        render_power_edit_ui(line, idx, order)

                    st.divider()

                    # ── Quantity ──────────────────────────────────────
                    st.markdown("##### 📦 Quantity")
                    current_qty = int(line.get("billing_qty") or 1)
                    new_qty = st.number_input(
                        "Order Quantity", min_value=1,
                        value=current_qty, step=1,
                        key=f"clean_qty_{eye_label}_{idx}"
                    )
                    if new_qty != current_qty:
                        line["billing_qty"]      = int(new_qty)
                        line["batch_allocation"] = []
                        line["allocated_qty"]    = 0
                        line["batch_status"]     = "PENDING"
                        suggested = line.get("suggested_allocation")
                        if suggested:
                            if sum(b.get("allocated_qty", 0) for b in suggested) == int(new_qty):
                                line["batch_allocation"] = suggested
                                st.info("✅ Reusing suggested allocation from Retail")
                            else:
                                st.warning("⚠️ Qty changed — suggested allocation discarded")
                        refresh_line_state(line)
                        recalculate_order_totals(order)
                        from .backoffice_helpers import categorize_order_lines
                        categorize_order_lines(order)
                        st.success(f"✅ Quantity updated to {new_qty}")
                        st.rerun()

                    st.divider()

                    # ── Allocation ────────────────────────────────────
                    st.markdown("##### 🗂️ Allocation")
                    allocated   = int(line.get("allocated_qty") or 0)
                    billing_qty = int(line.get("billing_qty") or 0)
                    pending     = max(0, billing_qty - allocated)

                    a1, a2 = st.columns(2)
                    with a1:
                        if allocated > 0:
                            st.success(f"✅ {allocated} allocated")
                        else:
                            st.info("⬜ Not allocated")
                    with a2:
                        if pending > 0:
                            st.warning(f"⚠️ {pending} to order")

                    if st.button("🗂️ Manage Stock",
                                 key=f"alloc_{eye_label}_{idx}",
                                 use_container_width=True):
                        st.session_state.bo_show_allocation_window = True
                        st.session_state.bo_allocation_line_idx    = idx
                        st.rerun()

            # ── Render R and L using the shared helper ───────────────
            if has_right:
                with col_r:
                    _render_eye_block_ui(group['R'], group['R_idx'], 'R')

            if has_left:
                with col_l:
                    _render_eye_block_ui(group['L'], group['L_idx'], 'L')
        
        #  RENDER ALLOCATION WINDOW IF ACTIVE
        if st.session_state.get('bo_show_allocation_window', False):
            line_idx = st.session_state.get('bo_allocation_line_idx')
            
            if line_idx is not None and line_idx < len(all_lines):
                line = all_lines[line_idx]
                render_allocation_window(line, line_idx, order)

        # ========== FINAL SAVE TO ORDER ==========
        st.markdown("---")
        st.markdown("###  Order Summary")

        if st.button(" System Health Check", use_container_width=True):

            issues = run_system_health_check(order)

            if not issues:
                st.success(" System OK. No issues found.")
            else:
                st.error(" Issues Found:")
                for i in issues:
                    st.write("", i)
        
        # Calculate order totals with R/L breakdown
        total_items = len(all_lines)
        
        # Separate R and L lines - handle both short and full eye_side formats
        r_lines = [line for line in all_lines if line.get('eye_side', '').upper() in ['R', 'RIGHT']]
        l_lines = [line for line in all_lines if line.get('eye_side', '').upper() in ['L', 'LEFT']]
        other_lines = [line for line in all_lines if line.get('eye_side', '').upper() not in ['R', 'RIGHT', 'L', 'LEFT']]
        
        # Calculate R eye totals
        r_billing = sum(line.get('billing_total', 0) for line in r_lines)
        r_discount = sum(line.get('discount_amount', 0) for line in r_lines)
        
        # Calculate L eye totals
        l_billing = sum(line.get('billing_total', 0) for line in l_lines)
        l_discount = sum(line.get('discount_amount', 0) for line in l_lines)
        
        # Calculate other items totals
        other_billing = sum(line.get('billing_total', 0) for line in other_lines)
        other_discount = sum(line.get('discount_amount', 0) for line in other_lines)
        
        # Grand totals
        total_billing = r_billing + l_billing + other_billing
        total_discount = r_discount + l_discount + other_discount
        
        # Summary metrics row
        col_total1, col_total2, col_total3 = st.columns(3)
        with col_total1:
            st.metric("Total Items", total_items)
        with col_total2:
            st.metric("Total Discount", f"{total_discount:.2f}")
        with col_total3:
            st.metric("**Final Amount**", f"**{total_billing:.2f}**")
        
        # Detailed R/L Breakdown - Compact View
        st.markdown("---")
        
        # Right Eye Block
        with st.container(border=True):
            st.markdown("####  Right Eye")
            st.caption(f"{len(r_lines)} items | Subtotal: {r_billing:.2f}")
            
            if r_lines:
                # Header row
                hcols = st.columns([3, 2, 1, 1, 1, 1, 1])
                hcols[0].caption("Product")
                hcols[1].caption("Qty (Box+PCS)")
                hcols[2].caption("Unit Price")
                hcols[3].caption("Discount")
                hcols[4].caption("GST%")
                hcols[5].caption("GST Amt")
                hcols[6].caption("Total")
                st.markdown("<hr style='margin:2px 0 6px 0'>", unsafe_allow_html=True)
                for idx, line in enumerate(r_lines, 1):
                    # ── Box + PCS breakdown ──
                    qty        = int(line.get('billing_qty', 0) or 0)
                    box_size   = int(line.get('box_size') or 1)
                    unit       = str(line.get('unit') or 'PCS').upper()
                    if unit == 'BOX' and box_size > 1:
                        boxes    = qty // box_size
                        pcs_rem  = qty % box_size
                        qty_disp = f"{boxes}B" + (f"+{pcs_rem}P" if pcs_rem else "") + f" ({qty}pcs)"
                    else:
                        qty_disp = f"{qty} PCS"
                    # ── GST fields ──
                    gst_pct     = float(line.get('gst_percent_used') or line.get('gst_percent') or 0)
                    gst_amt     = float(line.get('gst_amount') or 0)
                    tax_inc     = line.get('tax_inclusive', True)
                    gst_label   = f"{gst_pct:.0f}%" + (" (incl)" if tax_inc else " (+)") if gst_pct else "⚠️ Not set"
                    disc_pct    = float(line.get('discount_percent') or 0)
                    unit_price  = float(line.get('unit_price') or 0)
                    total       = float(line.get('billing_total') or line.get('total_price') or 0)
                    lcols = st.columns([3, 2, 1, 1, 1, 1, 1])
                    lcols[0].write(f"{idx}. {line.get('product_name', 'N/A')}")
                    lcols[1].write(qty_disp)
                    lcols[2].write(f"₹{unit_price:,.2f}")
                    lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                    lcols[4].write(gst_label)
                    lcols[5].write(f"₹{gst_amt:,.2f}" if gst_amt else ("⚠️" if gst_pct else "—"))
                    lcols[6].write(f"₹{total:,.2f}")
            else:
                st.caption("No Right Eye items")
        
        # Left Eye Block
        with st.container(border=True):
            st.markdown("####  Left Eye")
            st.caption(f"{len(l_lines)} items | Subtotal: {l_billing:.2f}")
            
            if l_lines:
                # Header row
                hcols = st.columns([3, 2, 1, 1, 1, 1, 1])
                hcols[0].caption("Product")
                hcols[1].caption("Qty (Box+PCS)")
                hcols[2].caption("Unit Price")
                hcols[3].caption("Discount")
                hcols[4].caption("GST%")
                hcols[5].caption("GST Amt")
                hcols[6].caption("Total")
                st.markdown("<hr style='margin:2px 0 6px 0'>", unsafe_allow_html=True)
                for idx, line in enumerate(l_lines, 1):
                    qty        = int(line.get('billing_qty', 0) or 0)
                    box_size   = int(line.get('box_size') or 1)
                    unit       = str(line.get('unit') or 'PCS').upper()
                    if unit == 'BOX' and box_size > 1:
                        boxes    = qty // box_size
                        pcs_rem  = qty % box_size
                        qty_disp = f"{boxes}B" + (f"+{pcs_rem}P" if pcs_rem else "") + f" ({qty}pcs)"
                    else:
                        qty_disp = f"{qty} PCS"
                    gst_pct     = float(line.get('gst_percent_used') or line.get('gst_percent') or 0)
                    gst_amt     = float(line.get('gst_amount') or 0)
                    tax_inc     = line.get('tax_inclusive', True)
                    gst_label   = f"{gst_pct:.0f}%" + (" (incl)" if tax_inc else " (+)") if gst_pct else "⚠️ Not set"
                    disc_pct    = float(line.get('discount_percent') or 0)
                    unit_price  = float(line.get('unit_price') or 0)
                    total       = float(line.get('billing_total') or line.get('total_price') or 0)
                    lcols = st.columns([3, 2, 1, 1, 1, 1, 1])
                    lcols[0].write(f"{idx}. {line.get('product_name', 'N/A')}")
                    lcols[1].write(qty_disp)
                    lcols[2].write(f"₹{unit_price:,.2f}")
                    lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                    lcols[4].write(gst_label)
                    lcols[5].write(f"₹{gst_amt:,.2f}" if gst_amt else ("⚠️" if gst_pct else "—"))
                    lcols[6].write(f"₹{total:,.2f}")
            else:
                st.caption("No Left Eye items")
        
        # Other Items Section - Compact (if any)
        if other_lines:
            with st.container(border=True):
                st.markdown("####  Other Items")
                st.caption(f"{len(other_lines)} items | Subtotal: {other_billing:.2f}")
                
                # Header row
                hcols = st.columns([3, 2, 1, 1, 1, 1, 1])
                hcols[0].caption("Product")
                hcols[1].caption("Qty (Box+PCS)")
                hcols[2].caption("Unit Price")
                hcols[3].caption("Discount")
                hcols[4].caption("GST%")
                hcols[5].caption("GST Amt")
                hcols[6].caption("Total")
                st.markdown("<hr style='margin:2px 0 6px 0'>", unsafe_allow_html=True)
                for idx, line in enumerate(other_lines, 1):
                    qty        = int(line.get('billing_qty', 0) or 0)
                    box_size   = int(line.get('box_size') or 1)
                    unit       = str(line.get('unit') or 'PCS').upper()
                    if unit == 'BOX' and box_size > 1:
                        boxes    = qty // box_size
                        pcs_rem  = qty % box_size
                        qty_disp = f"{boxes}B" + (f"+{pcs_rem}P" if pcs_rem else "") + f" ({qty}pcs)"
                    else:
                        qty_disp = f"{qty} PCS"
                    gst_pct     = float(line.get('gst_percent_used') or line.get('gst_percent') or 0)
                    gst_amt     = float(line.get('gst_amount') or 0)
                    tax_inc     = line.get('tax_inclusive', True)
                    gst_label   = f"{gst_pct:.0f}%" + (" (incl)" if tax_inc else " (+)") if gst_pct else "⚠️ Not set"
                    disc_pct    = float(line.get('discount_percent') or 0)
                    unit_price  = float(line.get('unit_price') or 0)
                    total       = float(line.get('billing_total') or line.get('total_price') or 0)
                    lcols = st.columns([3, 2, 1, 1, 1, 1, 1])
                    lcols[0].write(f"{idx}. {line.get('product_name', 'N/A')}")
                    lcols[1].write(qty_disp)
                    lcols[2].write(f"₹{unit_price:,.2f}")
                    lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                    lcols[4].write(gst_label)
                    lcols[5].write(f"₹{gst_amt:,.2f}" if gst_amt else ("⚠️" if gst_pct else "—"))
                    lcols[6].write(f"₹{total:,.2f}")
        
        st.markdown("---")

        # ── Debug Overlay (only when debug_pricing enabled in sidebar) ──────────
        if st.session_state.get("debug_pricing"):
            try:
                from .debug_pricing_overlay import render_debug_overlay
                render_debug_overlay(order, all_lines)
            except Exception as _dbg:
                st.caption(f"Debug overlay error: {_dbg}")
        # ──────────────────────────────────────────────────────────────────────────

        # ══════════════════════════════════════════════════════════════════════
        # 🎯 SUPPLIER / JOB ASSIGNMENT PANEL
        # Must be confirmed before Save is allowed.
        # Shift button lets operator re-route any line without re-opening save.
        # ══════════════════════════════════════════════════════════════════════
        if _ASSIGNMENT_PANEL_AVAILABLE:
            render_assignment_panel(order, all_lines)
        # ══════════════════════════════════════════════════════════════════════

        # ── GST Verification Footer ───────────────────────────────────────────
        order_type  = order.get("order_type", "RETAIL")
        gst_total   = sum(float(l.get("gst_amount") or 0) for l in all_lines)
        taxable_val = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in all_lines)

        with st.container(border=True):
            st.caption("📊 Billing Verification Summary")
            vc = st.columns(4)
            if order_type == "RETAIL":
                vc[0].metric("MRP Total (incl. GST)",  f"₹{taxable_val:,.2f}")
                vc[1].metric("GST Extracted",           f"₹{gst_total:,.2f}",   help="GST back-calculated from MRP")
                vc[2].metric("Taxable Value",           f"₹{taxable_val - gst_total:,.2f}")
                vc[3].metric("Patient Pays",            f"₹{taxable_val:,.2f}")
            else:
                vc[0].metric("Subtotal (excl. GST)",   f"₹{taxable_val:,.2f}")
                vc[1].metric("GST Added",               f"₹{gst_total:,.2f}",   help="GST added on top of selling price")
                vc[2].metric("Grand Total",             f"₹{taxable_val + gst_total:,.2f}")
                vc[3].metric("Order Type",              order_type)
            st.caption(
                f"Source: {order.get('order_source', order_type)}  |  "                f"Tax treatment: {'GST inclusive in price' if order_type == 'RETAIL' else 'GST exclusive — added on top'}"            )
        # ─────────────────────────────────────────────────────────────────────

        from modules.utils.submit_guard import is_locked, guarded_submit
        if st.button(" SAVE TO ORDER", type="primary", use_container_width=True,
                     key="final_save_order", disabled=is_locked("final_save")):
            with guarded_submit("final_save") as _allowed:
                if not _allowed:
                    st.stop()
                # 🔐 Billing guard — never save with zero billing
                if total_billing <= 0:
                    st.error("❌ Billing total invalid. Cannot save with zero or negative billing.")
                    st.warning("Please check line items and pricing before saving.")
                    return   # guarded_submit __exit__ clears lock

                # 🎯 Assignment guard — warn if assignments not confirmed
                if _ASSIGNMENT_PANEL_AVAILABLE and not st.session_state.get("bo_assignments_locked", False):
                    st.warning(
                        "⚠️ Supplier / Job assignments not confirmed. "
                        "Scroll up and click **Confirm All Assignments** before saving."
                    )
                    return

                try:
                    # ── GST Recalculation — MUST succeed before save ──────────
                    try:
                        from modules.pricing.tax_engine import apply_taxes
                        tax_input = {
                            "order_type": order.get("order_type", "RETAIL"),
                            "net_value":  sum(
                                float(l.get("billing_total") or l.get("total_price") or 0)
                                for l in all_lines
                            ),
                            "lines": all_lines,
                        }
                        taxed = apply_taxes(tax_input)
                        order["tax_amount"]  = taxed["tax_amount"]
                        order["final_value"] = taxed["final_value"]
                    except Exception as _tax_err:
                        st.error(f"❌ GST recalculation failed — order NOT saved: {_tax_err}")
                        st.stop()   # lock auto-clears
                    # ─────────────────────────────────────────────────────────

                    from modules.persistence.order_persistence import save_order_to_db
                    from modules.sql_adapter import run_query

                    # ── Smart status advance on save ─────────────────────────
                    # If all lines are stock-allocated → jump to CONFIRMED
                    # (order is collected, nothing to procure)
                    _all_stock_now = all(
                        (l.get("manufacturing_route") or "STOCK") == "STOCK"
                        for l in all_lines
                    ) if all_lines else False
                    _has_vendor = any(
                        (l.get("manufacturing_route") or "") in ("VENDOR", "EXTERNAL_LAB", "INHOUSE")
                        for l in all_lines
                    )
                    _cur_status = order.get("status", "PENDING")
                    if _cur_status in ("PENDING", "PENDING_VALIDATION", "PROVISIONAL"):
                        if _all_stock_now:
                            order["status"] = "CONFIRMED"
                        elif _has_vendor:
                            order["status"] = "PENDING"   # keep — needs procurement
                    # ─────────────────────────────────────────────────────────

                    # ── Decide final status BEFORE save ──────────────────
                    # Use batch_status + allocation as the ground truth
                    # (manufacturing_route may be None on old orders)
                    _alloc_total  = sum(int(l.get("allocated_qty") or 0) for l in all_lines)
                    _bill_total   = sum(int(l.get("billing_qty") or 0) for l in all_lines)
                    _has_unstock  = any(
                        (l.get("manufacturing_route") or "").upper() in ("VENDOR", "EXTERNAL_LAB", "INHOUSE")
                        for l in all_lines
                    )
                    _cur_status = order.get("status") or "PENDING"
                    if _cur_status in ("PENDING", "PENDING_VALIDATION", "PROVISIONAL", ""):
                        # Stock order: fully allocated OR no vendor lines → CONFIRMED
                        if not _has_unstock:
                            order["status"] = "CONFIRMED"
                        # else keep PENDING — needs procurement

                    # Write status to DB directly FIRST (before save, so upsert picks it up)
                    _new_status = order["status"]
                    try:
                        run_query(
                            "UPDATE orders SET status=%(s)s, updated_at=NOW() WHERE order_no=%(n)s",
                            {"s": _new_status, "n": order.get("order_no")},
                        )
                    except Exception:
                        pass
                    # ─────────────────────────────────────────────────────

                    saved_id = save_order_to_db(order)
                    for line in all_lines:
                        line["pricing_locked"] = True

                    # Update the in-memory order in bo_active_orders so the
                    # detail view reflects the new status without a full reload
                    _ono = order.get("order_no")
                    for _o in st.session_state.get("bo_active_orders", []):
                        if _o.get("order_no") == _ono:
                            _o["status"]     = _new_status
                            _o["updated_at"] = str(datetime.datetime.now())[:16]
                            break

                    # Clear load cache so next dashboard Load gets fresh data
                    try:
                        from modules.backoffice.backoffice_helpers import load_orders_from_database
                        load_orders_from_database.clear()
                    except Exception:
                        pass

                    _disp_no = order.get("display_order_no") or order.get("order_no", saved_id)
                    st.success(
                        f"✅ Order **{_disp_no}** saved — "
                        f"Status: **{_new_status}** 🎯"
                    )
                    if _new_status == "CONFIRMED":
                        st.info("📦 All lines stock-allocated. Order is Collected ✓")
                    st.balloons()
                except Exception as _save_err:
                    try:
                        from modules.core.error_logger import log_error
                        log_error(_save_err, context="backoffice.save_to_order",
                                  payload={"order_no": order.get("order_no"),
                                           "order_type": order.get("order_type")})
                    except Exception:
                        pass
                    st.error(f"❌ Save failed: {_save_err}")
    
    with tab2:
        # Documents tab — smart default based on what lines this order has
        has_inhouse = bool(order.get('inhouse_lines'))
        has_lab     = bool(order.get('lab_order_lines'))
        has_stock   = bool(order.get('stock_lines'))

        # Default to the most relevant tab for this order type
        if has_stock and not has_inhouse and not has_lab:
            default_doc = 'Labels'
        elif has_inhouse and not has_lab:
            default_doc = 'Job Cards'
        elif has_lab and not has_inhouse:
            default_doc = 'Lab Orders'
        else:
            default_doc = 'All'

        doc_options = ['Job Cards', 'Lab Orders', 'Labels', 'All']
        doc_type = st.radio(
            "Document Type",
            doc_options,
            index=doc_options.index(default_doc),
            horizontal=True,
            key=f"doc_type_{order_id}"
        )

        shown_any = False

        if doc_type in ['Job Cards', 'All']:
            if has_inhouse:
                generate_job_cards(order)
                shown_any = True
            elif doc_type == 'Job Cards':
                st.info("No in-house manufacturing lines on this order — no job cards to generate.")

        if doc_type in ['Lab Orders', 'All']:
            if has_lab:
                generate_lab_orders(order)
                shown_any = True
            elif doc_type == 'Lab Orders':
                st.info("No external lab lines on this order — no lab orders to generate.")

        if doc_type in ['Labels', 'All']:
            if has_stock:
                generate_labels(order)
                shown_any = True
            elif doc_type == 'Labels':
                st.info("No stock lines on this order — no labels to generate.")

        if doc_type == 'All' and not shown_any:
            st.warning("No documents to generate for this order yet. Add and allocate line items first.")
    
    with tab3:
        from modules.backoffice.order_status_window import render_order_status_window
        render_order_status_window(order)
    
    with tab4:
        # Billing tab
        st.markdown("###  Billing Summary")
        
        # 🔐 Check if any pricing is locked
        locked_count = sum(1 for line in all_lines if line.get('pricing_locked', False))
        if locked_count > 0:
            st.info(f"🔒 {locked_count} of {len(all_lines)} line items have locked pricing")
        
        total_billing = sum(line.get('billing_total', 0) for line in all_lines)
        total_allocated = sum(line.get('allocated_qty', 0) for line in all_lines)
        
        col_x, col_y, col_z = st.columns(3)
        
        with col_x:
            st.metric("Total Items", len(all_lines))
        with col_y:
            st.metric("Total Allocated", total_allocated)
        with col_z:
            st.metric("Total Billing", f"{total_billing:.2f}")
        
        # Billing details
        
        billing_data = []
        for idx, line in enumerate(all_lines, 1):
            billing_data.append({
                'Line #': idx,
                'Product': line.get('product_name', 'N/A'),
                'Qty': int(line.get('billing_qty', 0) or 0),  #  FIX: Use billing_qty for billing
                'Unit Price': f"{line.get('unit_price', 0):.2f}",
                'Total': f"{line.get('billing_total', 0):.2f}",
                '🔒': '🔒' if line.get('pricing_locked', False) else ''
            })
        
        st.dataframe(pd.DataFrame(billing_data), use_container_width=True)
        
        # 📊 PRICING DEBUG TOGGLE
        st.markdown("---")
        if st.checkbox("🔍 Debug Pricing (Advanced)", key=f"debug_pricing_{order_id}"):
            st.markdown("####  Line Item Debug Information")
            st.caption("Shows detailed pricing calculations for each line item")
            
            for idx, line in enumerate(all_lines, 1):
                with st.expander(f"Line {idx}: {line.get('product_name', 'N/A')}", expanded=False):
                    debug_info = {
                        "Product ID": line.get('product_id', 'N/A'),
                        "Price Source": line.get('price_source', 'unknown'),
                        "Eye Side": line.get('eye_side', 'N/A'),
                        "Billing Qty": line.get('billing_qty', 0),
                        "Allocated Qty": line.get('allocated_qty', 0),
                        "Pending Qty": max(0, int(line.get('billing_qty', 0) or 0) - int(line.get('allocated_qty', 0) or 0)),
                        "Unit Price": line.get('unit_price', 0),
                        "Discount %": line.get('discount_percent', 0),
                        "Billing Total": line.get('billing_total', 0),
                        "Manufacturing Route": line.get('manufacturing_route', 'N/A'),
                        "Batch Allocation": line.get('batch_allocation', []),
                        "Pricing Locked": line.get('pricing_locked', False),
                    }
                    st.json(debug_info, expanded=False)
    
    # =====================================================
    # TAB 5: SUPPLIER ORDERS PANEL
    # =====================================================
    with tab5:
        try:
            from .supplier_panel import render_supplier_panel
            render_supplier_panel(order)
        except ImportError as e:
            st.error(f"❌ Supplier Panel module not found: {e}")
            st.info("📋 Place supplier_panel.py in modules/backoffice/ directory")
        except Exception as e:
            st.error(f"❌ Supplier Panel error: {e}")
            import traceback
            with st.expander("Debug Info"):
                st.code(traceback.format_exc())
    
    # =====================================================
    # TAB 6: BILLING GATE (CONTROLLED WRITE PANEL)
    # =====================================================
    with tab6:
        try:
            from .billing_gate import render_billing_gate
            render_billing_gate(order)
        except ImportError as e:
            st.error(f"❌ Billing Gate module not found: {e}")
            st.info("📋 Place billing_gate.py in modules/backoffice/ directory")
        except Exception as e:
            st.error(f"❌ Billing Gate error: {e}")
            import traceback
            with st.expander("Debug Info"):
                st.code(traceback.format_exc())


def show_status_update_modal(order: Dict):
    """Show modal for updating order status"""
    st.markdown("---")
    st.markdown("###  Update Order Status")
    
    current_status = order.get('status', 'PENDING')

    
    new_status = st.selectbox(
        "New Status",
        [s.value for s in OrderStatus],
        index=[s.value for s in OrderStatus].index(current_status) if current_status in [s.value for s in OrderStatus] else 0,
        key=f"status_update_{get_display_order_id(order)}"
    )
    
    notes = st.text_area(
        "Status Update Notes",
        key=f"status_notes_{get_display_order_id(order)}"
    )
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button(" Update Status", type="primary", use_container_width=True):
            # Update order status
            for o in st.session_state.bo_active_orders:
                if get_display_order_id(o) == get_display_order_id(order):
                    o['status'] = new_status
                    o['updated_at'] = datetime.datetime.now().isoformat()
                    if notes:
                        if 'status_history' not in o:
                            o['status_history'] = []
                        o['status_history'].append({
                            'timestamp': datetime.datetime.now().isoformat(),
                            'status': new_status,
                            'notes': notes
                        })
            
            st.success(f" Status updated to: {new_status}")
            st.rerun()
    
    with col2:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


# render_backoffice_dashboard, render_backoffice_management, and
# run_system_health_check live in backoffice.py and backoffice_helpers.py
# They are NOT duplicated here — import from those modules instead.