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
from modules.core.price_qty_governor import (
    resolve_price   as resolve_price_for_order_type,
    normalize_to_pcs_price,
    is_box_product,
    get_pcs_price,
    reverse_qty,
    compute_line_gst,
    check_sync,
    PAIR_TO_PCS,
)
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
        if st.button(" Change", key=f"change_product_{eye_label}_{idx}", width='stretch'):
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
            if st.button(" Apply Change", type="primary", width='stretch'):
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
            if st.button(" Cancel", width='stretch', key="cancel_product_change_col"):
                st.session_state['bo_product_change_modal'] = {'active': False}
                st.rerun()
    else:
        if st.button(" Cancel", width='stretch', key="cancel_product_change_else"):
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
    all_lines.extend(order.get('service_lines', []))  # consultation fee lines
    
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
            width='stretch',
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

_STAGE_REJECT_REASONS = {
    "PRODUCTION_PICKED":   ["Blank broken on machine", "Wrong base curve", "Power out of tolerance",
                            "Blank scratched before surfacing", "Machine error", "Other"],
    "SURFACING_DONE":      ["Power out of tolerance", "Surface defect — scratches", "Prism error",
                            "Edge chipping", "Wrong axis", "Other"],
    "INSPECTION":          ["Power rejected at inspection", "Cosmetic defect", "Thickness out of spec",
                            "Wrong coating requested", "Other"],
    "HARDCOAT_PICKED":     ["Hardcoat adhesion failure", "Bubbling / peeling", "Contamination",
                            "Wrong coating applied", "Other"],
    "HARDCOAT_COMPLETED":  ["Hardcoat failed QC", "Crazing / cracking", "Colour tint bleed", "Other"],
    "COLOURING_PICKED":    ["Wrong tint shade", "Uneven colouring", "Lens cracked during tint", "Other"],
    "COLOURING_COMPLETED": ["Tint failed QC", "Colour mismatch", "Fading / streaking", "Other"],
    "ARC_SENT":            ["Lost in transit to ARC lab", "Wrong lens sent", "Other"],
    "ARC_RECEIVED":        ["ARC coating peeling", "Reflection test failed", "Delamination", "Other"],
    "PRODUCTION_COMPLETED":["Final inspection — power out of tolerance", "Cosmetic reject",
                            "Thickness issue", "Other"],
    "FINAL_QC":            ["Final QC failed", "Customer spec mismatch", "Cosmetic reject",
                            "Frame fitting issue", "Other"],
}


def _render_inline_reject(lid: str, current_stage: str, line: dict, order: dict):
    """
    Inline reject panel — shown inside _render_job_stage_controls at any stage.
    Handles full pipeline reset: blank restore + job reset + log entry.
    After reset, pipeline restarts from JOB_CREATED → staff selects new blank.
    """
    import streamlit as st
    from modules.security.roles import current_user

    _eye = str(line.get("eye_side") or "?").upper()[:1]
    _eye_label = "RIGHT" if _eye == "R" else "LEFT" if _eye == "L" else _eye

    # Get blank_id from lens_params.surfacing_data
    try:
        import json as _j
        _lp = line.get("lens_params") or {}
        if isinstance(_lp, str):
            _lp = _j.loads(_lp)
        _surf = _lp.get("surfacing_data") or {}
        _blank_id   = str(_surf.get("blank_id") or "")
        _blank_label = f"{_surf.get('blank_brand','')} {_surf.get('blank_material','')} {_surf.get('base_curve','')}D"
        _blank_label = _blank_label.strip() or "—"
    except Exception:
        _blank_id, _blank_label = "", "—"

    _stage_color = "#f59e0b"

    st.markdown(
        f"<div style='background:#1a0f00;border:1px solid {_stage_color}55;"
        f"border-radius:8px;padding:10px 14px;margin:6px 0'>"
        f"<div style='color:{_stage_color};font-weight:700;font-size:0.85rem'>"
        f"↩️ Reject {_eye_label} Eye — Stage: {current_stage}</div>"
        f"<div style='color:#94a3b8;font-size:0.75rem;margin-top:3px'>"
        f"Blank: {_blank_label} &nbsp;·&nbsp; "
        f"Blank will be restored to stock. Job resets to JOB_CREATED. "
        f"Staff selects a new blank to restart.</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Stage-specific reasons
    _reasons = _STAGE_REJECT_REASONS.get(current_stage, ["Blank failed", "Other"])
    _reason_opts = ["— Select reason —"] + _reasons
    _reason = st.selectbox(
        "Rejection reason",
        _reason_opts,
        key=f"inline_rej_reason_{lid}",
        label_visibility="collapsed",
    )
    _other = ""
    if _reason == "Other":
        _other = st.text_input(
            "Specify",
            key=f"inline_rej_other_{lid}",
            placeholder="Describe what failed...",
            label_visibility="collapsed",
        )
    _final_reason = _other.strip() if _reason == "Other" else _reason

    _notes = st.text_area(
        "Additional notes (optional)",
        key=f"inline_rej_notes_{lid}",
        height=60,
        placeholder="e.g. crack appeared after hardcoat, sent to bin 3",
        label_visibility="collapsed",
    )

    _disabled = _final_reason in ("", "— Select reason —")
    _c1, _c2 = st.columns(2)
    with _c1:
        if st.button(
            "✅ Confirm — Reject & Restart Pipeline",
            key=f"inline_rej_confirm_{lid}",
            type="primary",
            width='stretch',
            disabled=_disabled,
        ):
            _execute_pipeline_rejection(
                line_id=lid,
                blank_id=_blank_id,
                eye_side=_eye,
                stage=current_stage,
                reason=_final_reason,
                notes=_notes.strip(),
                user=(current_user() or {}).get("name", "lab"),
            )
            st.session_state.pop(f"_rej_panel_{lid}", None)

    with _c2:
        if st.button("← Cancel", key=f"inline_rej_cancel_{lid}",
                     width='stretch'):
            st.session_state.pop(f"_rej_panel_{lid}", None)
            st.rerun()


def _execute_pipeline_rejection(line_id, blank_id, eye_side, stage, reason, notes, user):
    """
    Full atomic pipeline rejection at any stage:
    1. Restore blank to blank_inventory (+1) — only if blank was physically picked
    2. Reset job_master → JOB_CREATED, increment reprocess_count
    3. Clear blank_allocations row
    4. Wipe surfacing keys from order_lines.lens_params
    5. Log to job_rejection_log
    6. Restore order_line.allocated_qty/batch_status if production not started
    """
    import streamlit as st
    from modules.sql_adapter import run_transaction_fn, QueryError

    # Stages where blank was physically picked — blank needs restoring
    _blank_consumed_stages = {
        "PRODUCTION_PICKED", "SURFACING_DONE", "INSPECTION",
        "HARDCOAT_PICKED", "HARDCOAT_COMPLETED",
        "COLOURING_PICKED", "COLOURING_COMPLETED",
        "ARC_SENT", "ARC_RECEIVED",
        "PRODUCTION_COMPLETED", "FINAL_QC",
        "READY_TO_BILL",
    }

    _full_reason = reason + (f" | Notes: {notes}" if notes else "")

    try:
        # Step 1: Restore blank inventory if blank was physically consumed
        _restored = False
        if blank_id and stage in _blank_consumed_stages:
            from modules.sql_adapter import update_blank_quantity
            _restored = update_blank_quantity(
                blank_id=blank_id,
                qty_change=+1,
                eye_side=eye_side if eye_side in ("R", "L") else None,
            )
            if not _restored:
                st.warning(
                    "⚠️ Blank could not be restored to inventory — "
                    "check blank_inventory manually. Pipeline still reset."
                )

        # Steps 2-5: Reset job + clear allocation + log (one transaction)
        def _reset_tx(conn, cursor):
            # 2. Reset job_master
            cursor.execute("""
                UPDATE job_master
                SET    current_stage   = 'JOB_CREATED',
                       is_closed       = FALSE,
                       reprocess_count = COALESCE(reprocess_count, 0) + 1,
                       coating_path    = NULL
                WHERE  order_line_id   = %(lid)s::uuid
                  AND  COALESCE(is_closed, FALSE) = FALSE
            """, {"lid": line_id})

            # 3. Clear blank_allocations
            cursor.execute("""
                DELETE FROM blank_allocations
                WHERE  order_line_id = %(lid)s::uuid
            """, {"lid": line_id})

            # 4. Wipe surfacing keys from lens_params (preserve other data)
            cursor.execute("""
                UPDATE order_lines
                SET    lens_params = COALESCE(lens_params, '{}')::jsonb
                       - 'blank_id' - 'blank_brand' - 'blank_material'
                       - 'blank_colour' - 'base_curve' - 'diameter'
                       - 'sph_surf' - 'cyl_surf' - 'frame_type'
                       - 'job_card_wip' - 'surfacing_data',
                       batch_status = 'PENDING',
                       allocated_qty = 0,
                       ready_qty     = 0
                WHERE  id = %(lid)s::uuid
            """, {"lid": line_id})

            # 5. Ensure rejection log table exists + insert
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS job_rejection_log (
                    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    order_line_id      UUID,
                    blank_id           UUID,
                    eye_side           CHAR(1),
                    stage_at_rejection TEXT,
                    reason             TEXT,
                    notes              TEXT,
                    blank_restored     BOOLEAN DEFAULT FALSE,
                    rejected_by        TEXT,
                    rejected_at        TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Add blank_restored + notes columns if missing
            for col, typ in [("notes", "TEXT"), ("blank_restored", "BOOLEAN DEFAULT FALSE")]:
                try:
                    cursor.execute(f"ALTER TABLE job_rejection_log ADD COLUMN IF NOT EXISTS {col} {typ}")
                except Exception:
                    pass

            cursor.execute("""
                INSERT INTO job_rejection_log
                    (order_line_id, blank_id, eye_side, stage_at_rejection,
                     reason, notes, blank_restored, rejected_by)
                VALUES
                    (%(lid)s::uuid, %(bid)s::uuid, %(eye)s, %(stage)s,
                     %(reason)s, %(notes)s, %(restored)s, %(by)s)
            """, {
                "lid":      line_id,
                "bid":      blank_id or "00000000-0000-0000-0000-000000000000",
                "eye":      eye_side[:1] if eye_side else None,
                "stage":    stage,
                "reason":   reason,
                "notes":    notes or "",
                "restored": _restored,
                "by":       user,
            })

            # ── Write to job_stage_events so Rejection Report picks it up ──
            cursor.execute("""
                SELECT id FROM job_master
                WHERE order_line_id = %(lid)s::uuid LIMIT 1
            """, {"lid": line_id})
            _jm_r = cursor.fetchone()
            if _jm_r:
                _jm_id2 = _jm_r[0] if isinstance(_jm_r, (list,tuple)) else _jm_r.get("id")
                if _jm_id2:
                    cursor.execute("""
                        INSERT INTO job_stage_events
                            (id, job_id, stage_code, department, remarks, created_at)
                        VALUES
                            (gen_random_uuid(), %(jid)s::uuid,
                             'REJECTED', 'LAB', %(reason)s, NOW())
                    """, {"jid": str(_jm_id2), "reason": reason})

        run_transaction_fn(_reset_tx)

        _restore_msg = "Blank restored to stock. " if _restored else ""
        st.success(
            f"✅ {_restore_msg}"
            f"Pipeline reset to JOB_CREATED. "
            f"Select a new blank to restart {'R' if eye_side=='R' else 'L'} eye production."
        )
        st.rerun()

    except Exception as e:
        st.error(f"❌ Rejection failed: {e}")



def _render_job_stage_controls(line: Dict, order: Dict, compact: bool = False) -> None:
    """
    Inline production stage advance panel for a single order_line.

    Queries job_master for this line, shows:
      - Current stage badge + progress dots
      - One button per allowed next stage (from job_stage_transitions table,
        falling back to a hardcoded map)

    Works in any context (inside expanders, columns, etc.).
    compact=True → uses a single horizontal row instead of a full card.
    """
    from modules.sql_adapter import run_query as _rqjs

    lid = (line.get("line_id") or line.get("id") or "").strip()
    if not lid:
        return

    # ── Fetch job row ──────────────────────────────────────────────────────
    try:
        job_rows = _rqjs(
            "SELECT id::text AS job_id, current_stage, is_closed, "
            "total_qty, blank_allocated_qty, coating_path, updated_at "
            "FROM job_master WHERE order_line_id = %(lid)s::uuid "
            "AND NOT COALESCE(is_closed, FALSE) "
            "ORDER BY created_at DESC LIMIT 1",
            {"lid": lid}
        )
    except Exception:
        return

    if not job_rows:
        # Show closed job summary if no open job
        try:
            closed = _rqjs(
                "SELECT current_stage FROM job_master "
                "WHERE order_line_id = %(lid)s::uuid AND is_closed = TRUE "
                "ORDER BY updated_at DESC LIMIT 1",
                {"lid": lid}
            )
            if closed:
                st.markdown(
                    "<div style='background:#0a1f12;border:1px solid #10b98155;"
                    "border-radius:8px;padding:8px 14px;margin:6px 0;"
                    "color:#4ade80;font-size:0.78rem;font-weight:700'>"
                    f"✅ Job complete — {closed[0].get('current_stage','READY_FOR_PACK')}"
                    "</div>",
                    unsafe_allow_html=True,
                )
        except Exception:
            pass
        return

    job     = job_rows[0]
    job_id  = job["job_id"]
    stage   = job.get("current_stage") or "JOB_CREATED"
    coating = job.get("coating_path") or ""

    # ── Fetch allowed next stages from DB transition table ─────────────────
    _STD_NEXT_INLINE = {
        "JOB_CREATED":          ["JOB_PRINTED"],
        "JOB_PRINTED":          ["PRODUCTION_PICKED"],
        "BLANK_ALLOCATED":      ["PRODUCTION_PICKED"],
        "PRODUCTION_PICKED":    ["SURFACING_DONE", "HARDCOAT_PICKED"],
        "SURFACING_DONE":       ["INSPECTION", "HARDCOAT_PICKED"],
        "INSPECTION":           ["HARDCOAT_PICKED", "HARDCOAT_COMPLETED"],
        "HARDCOAT_PICKED":      ["HARDCOAT_COMPLETED"],
        "HARDCOAT_COMPLETED":   ["ARC_SENT", "COLOURING_PICKED", "PRODUCTION_COMPLETED"],
        "COLOURING_PICKED":     ["COLOURING_COMPLETED"],
        "COLOURING_COMPLETED":  ["ARC_SENT", "PRODUCTION_COMPLETED"],
        "ARC_SENT":             ["ARC_RECEIVED"],
        "ARC_RECEIVED":         ["FINAL_QC", "PRODUCTION_COMPLETED"],
        "PRODUCTION_COMPLETED": ["FINAL_QC", "READY_FOR_PACK"],
        "FINAL_QC":             ["READY_FOR_PACK", "AWAITING_FITTING"],
        "READY_FOR_PACK":       ["READY_TO_BILL", "AWAITING_FITTING"],  # READY_TO_BILL = skip fitting, go to billing
        "AWAITING_FITTING":     ["SENT_TO_FITTER"],
        "SENT_TO_FITTER":       ["RECEIVED_FROM_FITTER"],
        "RECEIVED_FROM_FITTER": ["FITTING_DONE"],
        "FITTING_DONE":         ["DISPATCHED"],
    }
    _STAGE_LABELS_INLINE = {
        "JOB_CREATED":          ("📋", "Job Created",           "#64748b"),
        "JOB_PRINTED":          ("🖨",  "Job Card Printed",      "#3b82f6"),
        "BLANK_ALLOCATED":      ("🎯", "Blank Allocated",        "#8b5cf6"),
        "PRODUCTION_PICKED":    ("⚙️", "Picked for Production",  "#f59e0b"),
        "SURFACING_DONE":       ("✨", "Surfacing Done",          "#a855f7"),
        "INSPECTION":           ("🔍", "Inspection",             "#0d9488"),
        "HARDCOAT_PICKED":      ("🛡",  "Picked for Hardcoat",   "#06b6d4"),
        "HARDCOAT_COMPLETED":   ("🛡",  "Hardcoat Done",         "#0891b2"),
        "COLOURING_PICKED":     ("🎨", "Picked for Colouring",   "#ec4899"),
        "COLOURING_COMPLETED":  ("🎨", "Colouring Done",         "#be185d"),
        "ARC_SENT":             ("⚗️", "Sent for ARC",           "#7c3aed"),
        "ARC_RECEIVED":         ("⚗️", "ARC Received",           "#6d28d9"),
        "PRODUCTION_COMPLETED": ("✅", "Production Done",        "#10b981"),
        "FINAL_QC":             ("🔬", "Final QC",               "#059669"),
        "READY_FOR_PACK":       ("📦", "Ready for Packing",      "#10b981"),
        "AWAITING_FITTING":     ("⏳", "Awaiting Fitting",       "#f59e0b"),
        "SENT_TO_FITTER":       ("🔧", "Sent to Fitter",         "#d97706"),
        "RECEIVED_FROM_FITTER": ("📬", "Received from Fitter",   "#ea580c"),
        "FITTING_DONE":         ("✅", "Fitting Done",            "#16a34a"),
        "DISPATCHED":           ("🚚", "Dispatched",             "#0891b2"),
    }

    try:
        tr_rows = _rqjs(
            "SELECT to_stage_code FROM job_stage_transitions "
            "WHERE from_stage_code = %(s)s AND allowed = TRUE "
            "ORDER BY to_stage_code",
            {"s": stage}
        )
        allowed_next = [r["to_stage_code"] for r in tr_rows if r.get("to_stage_code")]
    except Exception:
        allowed_next = []
    if not allowed_next:
        allowed_next = _STD_NEXT_INLINE.get(stage, [])

    # ── Current stage info ─────────────────────────────────────────────────
    icon, label, clr = _STAGE_LABELS_INLINE.get(stage, ("⚙️", stage, "#64748b"))
    coating_badge = (
        f"<span style='background:#7c3aed22;color:#c4b5fd;"
        f"border:1px solid #7c3aed55;border-radius:12px;"
        f"padding:1px 8px;font-size:0.65rem;font-weight:700;margin-left:6px'>"
        f"⚗️ {coating}</span>"
        if coating else ""
    )

    st.markdown(
        f"<div style='background:#0f172a;border:1px solid {clr}55;"
        f"border-radius:8px;padding:8px 14px;margin:8px 0 4px;"
        f"display:flex;align-items:center;gap:10px'>"
        f"<span style='background:{clr}22;color:{clr};border:1.5px solid {clr}66;"
        f"border-radius:20px;padding:3px 12px;font-size:0.78rem;font-weight:800'>"
        f"{icon} {label}</span>"
        f"{coating_badge}"
        f"<span style='color:#475569;font-size:0.65rem;margin-left:auto'>"
        f"🧱 Blank {int(job.get('blank_allocated_qty') or 0)}/"
        f"{int(job.get('total_qty') or 0)}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # ── Advance buttons + Reject button ──────────────────────────────────
    # Reject is shown alongside advance buttons at any post-pick stage
    _REJECTABLE_STAGES = {
        "PRODUCTION_PICKED", "SURFACING_DONE", "INSPECTION",
        "HARDCOAT_PICKED", "HARDCOAT_COMPLETED",
        "COLOURING_PICKED", "COLOURING_COMPLETED",
        "ARC_SENT", "ARC_RECEIVED",
        "PRODUCTION_COMPLETED", "FINAL_QC",
    }

    if not allowed_next and stage not in _REJECTABLE_STAGES:
        st.caption("No further stage transitions for this job.")
        return

    order_id = str(order.get("id") or "")

    # Show reject button if at a rejectable stage
    if stage in _REJECTABLE_STAGES:
        from modules.security.roles import has_role as _hr_rej
        if _hr_rej("admin", "manager", "lab"):
            _rej_key = f"_rej_panel_{lid}"
            _rej_col1, _rej_col2 = st.columns([3, 1])
            with _rej_col2:
                if st.button(
                    "↩️ Reject",
                    key=f"rej_open_{lid}",
                    width='stretch',
                    help="Blank failed at this stage — return to stock, restart pipeline"
                ):
                    st.session_state[_rej_key] = not st.session_state.get(_rej_key, False)
                    st.rerun()

            if st.session_state.get(_rej_key):
                _render_inline_reject(lid, stage, line, order)

    if not allowed_next:
        return

    btn_cols = st.columns(min(len(allowed_next), 3))
    for col, next_stage in zip(btn_cols, allowed_next):
        ns_icon, ns_label, ns_clr = _STAGE_LABELS_INLINE.get(
            next_stage, ("→", next_stage, "#3b82f6")
        )
        with col:
            if st.button(
                f"{ns_icon} → {ns_label}",
                key=f"jc_adv_{lid}_{next_stage}",
                width='stretch',
                type="primary",
            ):
                # Try DB function first, fall back to manual
                ok, msg = False, ""
                try:
                    res = _rqjs(
                        "SELECT advance_job_stage(%(jid)s::uuid, %(ns)s, NULL::uuid) AS r",
                        {"jid": job_id, "ns": next_stage}
                    )
                    result = str((res[0].get("r") or "") if res else "")
                    if result.startswith("ERROR"):
                        ok, msg = False, result
                    else:
                        ok, msg = True, result
                except Exception:
                    pass

                if not ok:
                    # Manual fallback
                    try:
                        from modules.sql_adapter import run_write as _rw
                        # PRODUCTION_PICKED blank guard
                        if next_stage == "PRODUCTION_PICKED":
                            ba = _rqjs(
                                "SELECT 1 FROM blank_allocations "
                                "WHERE order_line_id = %(lid)s::uuid LIMIT 1",
                                {"lid": lid}
                            )
                            if not ba:
                                st.error("❌ Blank not selected — fill in the job card above first")
                                return

                        # READY_FOR_PACK side effects
                        if next_stage == "READY_FOR_PACK":
                            info = _rqjs(
                                "SELECT total_qty FROM job_master WHERE id = %(jid)s::uuid",
                                {"jid": job_id}
                            )
                            qty = int((info[0].get("total_qty") or 0)) if info else 0
                            if qty > 0:
                                _rw(
                                    "UPDATE order_lines SET "
                                    "ready_qty = COALESCE(ready_qty,0) + %(q)s, "
                                    "WHERE id = %(lid)s::uuid",
                                    {"q": qty, "lid": lid}
                                )
                            _rw(
                                "UPDATE job_master SET is_closed=TRUE, updated_at=NOW() "
                                "WHERE id=%(jid)s::uuid",
                                {"jid": job_id}
                            )

                        # DISPATCHED/DELIVERED side effects - ensure ready_qty is set
                        elif next_stage in ("DISPATCHED", "DELIVERED") and lid:
                            # Check if ready_qty is already set for this line
                            current = _rqjs(
                                "SELECT COALESCE(ready_qty,0) as rq, quantity as qty "
                                "FROM order_lines WHERE id = %(lid)s::uuid",
                                {"lid": lid}
                            )
                            if current and int(current[0].get("rq", 0)) == 0:
                                qty = int(current[0].get("qty", 0))
                                if qty > 0:
                                    _rw(
                                        "UPDATE order_lines SET "
                                        "ready_qty = %(q)s, "
                                        "WHERE id = %(lid)s::uuid",
                                        {"q": qty, "lid": lid}
                                    )

                        _rw(
                            "UPDATE job_master SET current_stage=%(ns)s, updated_at=NOW() "
                            "WHERE id=%(jid)s::uuid",
                            {"jid": job_id, "ns": next_stage}
                        )
                        # Log event
                        try:
                            _rw(
                                "INSERT INTO job_stage_events "
                                "(id, job_id, stage_id, stage_code, department, created_at) "
                                "SELECT gen_random_uuid(), %(jid)s::uuid, "
                                "COALESCE((SELECT id FROM job_stage_master "
                                "WHERE stage_code=%(ns)s LIMIT 1), gen_random_uuid()), "
                                "%(ns)s, 'backoffice', NOW()",
                                {"jid": job_id, "ns": next_stage}
                            )
                        except Exception:
                            pass
                        ok, msg = True, f"Advanced to {next_stage}"
                    except Exception as _e:
                        ok, msg = False, str(_e)

                if ok:
                    st.success(f"✅ {ns_icon} Stage → **{ns_label}**")
                    try:
                        from modules.backoffice.backoffice_helpers import load_orders_from_database
                        load_orders_from_database.clear()
                    except Exception:
                        pass
                    st.rerun()
                else:
                    st.error(f"❌ {msg}")


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
        save_job_card_line,
    )

    def _get_line_key(ln):
        """Mirror of _line_key() from job_card_surfacing for use in backoffice_ui."""
        lid = (ln.get("line_id") or ln.get("id") or "").strip()
        eye = (ln.get("eye_side") or "X").upper().strip()
        ono = (ln.get("order_no") or "").strip()
        return f"jc_{lid}_{eye}" if lid else f"jc_{ono}_{eye}"

    st.markdown("---")
    st.markdown("### 🔧 In-House Job Cards")

    inhouse_lines = order.get("inhouse_lines", [])

    # Warn if any line has no route saved — job cards should only show after assignment
    _unrouted = [l for l in (order.get("lines") or [])
                 if not str(l.get("manufacturing_route") or "").strip()
                 and str(l.get("eye_side") or "").upper() in ("R","L")
                 and not l.get("is_service_line")]
    if _unrouted:
        st.warning(
            "⚠️ **Routes not saved yet.** "
            "Go to the Assignment tab, set routes (In-house / External Lab / Supplier / Stock), "
            "click **✅ Confirm All Assignments** — then come back here to fill job cards. "
            "Job cards shown below may include lines not assigned to in-house."
        )

    if not inhouse_lines:
        st.info("No in-house lines. Assign routes in the Assignment tab first.")
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
            product_name = r_line.get("product_name", "Unknown Product")
            category_name = r_line.get("main_group") or r_line.get("category") or "—"

            with st.expander(
                f"👁️ {product_name}  ·  {category_name}  — R + L",
                expanded=True,
            ):
                # ── Product / category header ─────────────────────────
                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #3b82f644;"
                    f"border-radius:10px;padding:10px 18px;margin-bottom:12px;"
                    f"display:flex;gap:20px;align-items:center'>"
                    f"<div><div style='color:#60a5fa;font-size:1.1rem;font-weight:800'>"
                    f"{product_name}</div>"
                    f"<div style='color:#94a3b8;font-size:0.78rem'>{category_name}</div></div>"
                    f"<div style='color:#334155;font-size:1.5rem'>|</div>"
                    f"<div style='color:#64748b;font-size:0.75rem'>"
                    f"Brand: {r_line.get('brand','—')}</div>"
                    f"</div>",
                    unsafe_allow_html=True)

                col_r, col_divider, col_l = st.columns([10, 1, 10])

                with col_r:
                    st.markdown(
                        "<div style='background:#0f2230;border-left:4px solid #4ade80;"
                        "border-radius:0 8px 8px 0;padding:6px 14px;margin-bottom:10px'>"
                        "<span style='color:#4ade80;font-weight:700;font-size:1rem'>"
                        "👁 RIGHT EYE</span></div>",
                        unsafe_allow_html=True)
                    render_surfacing_job_card(r_line, order)

                    _render_job_stage_controls(r_line, order)
                with col_divider:
                    st.markdown(
                        "<div style='border-left:2px dashed #334155;height:100%;"
                        "margin:0 auto;width:2px'></div>",
                        unsafe_allow_html=True)

                with col_l:
                    st.markdown(
                        "<div style='background:#0f1e30;border-left:4px solid #60a5fa;"
                        "border-radius:0 8px 8px 0;padding:6px 14px;margin-bottom:10px'>"
                        "<span style='color:#60a5fa;font-weight:700;font-size:1rem'>"
                        "👁 LEFT EYE</span></div>",
                        unsafe_allow_html=True)
                    render_surfacing_job_card(l_line, order)
                    _render_job_stage_controls(l_line, order)

                # ── PRINT BUTTON for both eyes ───────────────────────
                st.markdown("---")

                # Safe key — pk may contain slashes/spaces
                import re as _re
                _pair_pk = _re.sub(r"[^a-zA-Z0-9_]", "_", str(pk))

                # Reload both lines fresh from DB — in-memory dicts are stale after save
                def _reload_line_fresh(ln):
                    try:
                        from modules.sql_adapter import run_query as _rq3
                        import json as _j3
                        _lid3 = (ln.get("line_id") or ln.get("id") or "").strip()
                        if not _lid3: return ln
                        rows = _rq3(
                            "SELECT lens_params FROM order_lines WHERE id=%(l)s::uuid LIMIT 1",
                            {"l": _lid3})
                        if not rows: return ln
                        lp = rows[0].get("lens_params") or {}
                        if isinstance(lp, str):
                            try: lp = _j3.loads(lp)
                            except: return ln
                        fresh = dict(ln)
                        if lp.get("surfacing_data"):
                            fresh["surfacing_data"] = lp["surfacing_data"]
                        return fresh
                    except Exception:
                        return ln

                _r_fresh = _reload_line_fresh(r_line)
                _l_fresh = _reload_line_fresh(l_line)

                # Check if computed (filled in cards) or already saved in DB
                _r_lk   = f"jc_computed_{_get_line_key(_r_fresh)}"
                _l_lk   = f"jc_computed_{_get_line_key(_l_fresh)}"
                _r_computed = _r_lk in st.session_state
                _l_computed = _l_lk in st.session_state
                _r_saved_db = bool(_r_fresh.get("surfacing_data") or
                                   (_r_fresh.get("lens_params") or {}).get("surfacing_data"))
                _l_saved_db = bool(_l_fresh.get("surfacing_data") or
                                   (_l_fresh.get("lens_params") or {}).get("surfacing_data"))
                _r_ready    = _r_computed or _r_saved_db
                _l_ready    = _l_computed or _l_saved_db
                _both_ready = _r_ready and _l_ready
                _both_saved = _r_saved_db and _l_saved_db

                # ── Status badges ─────────────────────────────────────
                def _badge(ready, saved, eye):
                    if saved:   return f"✅ {eye} Saved", "#4ade80", "#0d2818"
                    if ready:   return f"📝 {eye} Ready to save", "#facc15", "#1a1500"
                    return f"⏳ {eye} — fill card above", "#f59e0b", "#1a0f00"

                _r_label, _r_col, _r_bg = _badge(_r_ready, _r_saved_db, "RE")
                _l_label, _l_col, _l_bg = _badge(_l_ready, _l_saved_db, "LE")
                st.markdown(
                    f"<div style='display:flex;gap:8px;padding:8px 0 4px'>"
                    f"<span style='background:{_r_bg};color:{_r_col};border:1px solid {_r_col};"
                    f"border-radius:20px;padding:3px 12px;font-size:0.8rem;font-weight:700'>{_r_label}</span>"
                    f"<span style='background:{_l_bg};color:{_l_col};border:1px solid {_l_col};"
                    f"border-radius:20px;padding:3px 12px;font-size:0.8rem;font-weight:700'>{_l_label}</span>"
                    f"</div>",
                    unsafe_allow_html=True)

                # ── Single Save Both button ───────────────────────────
                _btn1, _btn2 = st.columns(2)
                with _btn1:
                    _save_help = None if _both_ready else "Fill in both R and L job cards above first"
                    if st.button(
                        "💾 Save Both Job Cards & Update Inventory",
                        type="primary",
                        key=f"save_both_{_pair_pk}",
                        width='stretch',
                        disabled=not _both_ready,
                        help=_save_help,
                    ):
                        from modules.documents.job_card_surfacing import save_job_card_line
                        _errs = []
                        _msgs = []

                        # ── FIX: Merge in-memory computed surfacing_data into
                        # the fresh line before saving. _reload_line_fresh() only
                        # returns data already committed to DB. When a user fills
                        # the job card (including a base change) the selections
                        # live in st.session_state[jc_computed_<key>] and must
                        # be injected here, otherwise save_job_card_line has
                        # nothing to write and silently loses the work.
                        def _inject_computed(fr, lk):
                            if not fr.get("surfacing_data") and lk in st.session_state:
                                fr = dict(fr)
                                fr["surfacing_data"] = st.session_state[lk]
                            return fr

                        _r_to_save = _inject_computed(_r_fresh, _r_lk)
                        _l_to_save = _inject_computed(_l_fresh, _l_lk)

                        for _ln, _fr in (("RE", _r_to_save), ("LE", _l_to_save)):
                            _ok, _msg = save_job_card_line(_fr, order)
                            if _ok:
                                _msgs.append(_msg)
                            else:
                                _errs.append(f"{_ln}: {_msg}")
                        if _errs:
                            for _e in _errs:
                                st.error(f"❌ {_e}")
                        if _msgs:
                            for _m in _msgs:
                                st.success(_m)
                        if not _errs:
                            st.balloons()
                        st.rerun()

                with _btn2:
                    if st.button("🖨️ Print Both Job Cards",
                                 type="primary" if _both_saved else "secondary",
                                 key=f"print_pair_{_pair_pk}",
                                 width='stretch',
                                 disabled=not _both_saved,
                                 help=None if _both_saved else "Save both eyes first"):
                        st.session_state[f"show_print_pair_{_pair_pk}"] = True
                        st.rerun()

                # ── Print preview — R and L side by side ─────────────
                _print_key = f"show_print_pair_{_pair_pk}"
                if st.session_state.get(_print_key):
                    with st.expander("🖨 Print Preview — Both Eyes", expanded=True):
                        from modules.documents.job_card_surfacing import render_job_card_print_pair
                        render_job_card_print_pair(_r_fresh, _l_fresh, order)

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
                _render_job_stage_controls(line, order)
                if line.get("surfacing_data"):
                    with st.expander("🖨 Print Preview", expanded=False):
                        render_job_card_print(line, order)

            rendered_line_ids.add(id(line))

def generate_lab_orders(order: Dict):
    """
    Lab / Vendor order panel — shows VENDOR + EXTERNAL_LAB lines.
    Allows marking lines as ready when supplier has delivered.
    """
    st.markdown("---")
    st.markdown("### 🏭 Supplier / External Lab Orders")

    # Only VENDOR and EXTERNAL_LAB lines — INHOUSE lines are handled by
    # generate_job_cards() and must never appear here.
    # The caller (Documents tab) already sets order["lab_order_lines"] to
    # the vendor-only subset before calling this function.
    all_vendor = list(order.get("lab_order_lines", []))

    # Deduplicate by line_id — guards against any double-entry from upstream
    _seen_lids: set = set()
    _deduped: list = []
    for _ln in all_vendor:
        _dedup_lid = str(_ln.get("line_id") or _ln.get("id") or id(_ln))
        if _dedup_lid not in _seen_lids:
            _seen_lids.add(_dedup_lid)
            _deduped.append(_ln)
    all_vendor = _deduped

    if not all_vendor:
        st.info("No vendor / external lab lines for this order.")
        return

    order_id = str(order.get("id") or order.get("order_id") or "")

    # Load suppliers from parties table
    _suppliers = []
    try:
        from modules.sql_adapter import run_query as _rq_sup
        _sup_rows = _rq_sup("""
            SELECT id::text, party_name, mobile
            FROM parties
            WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR')
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY party_name
        """) or []
        _suppliers = [{"id": r["id"], "name": r["party_name"]} for r in _sup_rows]
    except Exception:
        pass

    if not _suppliers:
        _suppliers = [{"id": "", "name": "External Lab / Supplier"}]

    for _vi, line in enumerate(all_vendor):
        # _vi is the loop index — appended to every key so even blank/duplicate
        # line_ids (edge case: new unsaved lines) never collide.
        _lid    = str(line.get("line_id") or line.get("id") or f"v{_vi}")
        _key_sfx = f"{_lid}_{_vi}"  # unique suffix: uuid + position
        _eye    = str(line.get("eye_side") or "").upper()
        _pname  = str(line.get("product_name") or "").split(" | ")[0]
        _route  = str(line.get("manufacturing_route") or "VENDOR").upper()
        _needed = int(line.get("billing_qty") or line.get("quantity") or 0)
        _ready  = int(line.get("ready_qty") or 0)
        _supp   = str(line.get("supplier_name") or line.get("supplier_id") or "")
        _sph    = fmt_signed(line.get("sph")) if line.get("sph") is not None else "—"
        _cyl    = fmt_signed(line.get("cyl")) if line.get("cyl") is not None else ""

        _done   = _ready >= _needed
        _bg     = "#0f2a1a" if _done else "#1a0f00"
        _bdr    = "#22c55e" if _done else "#f59e0b"
        _color  = "#86efac" if _done else "#fcd34d"
        _status_lbl = f"✅ Ready ({_ready}/{_needed})" if _done else f"⏳ {_ready}/{_needed} received"

        st.markdown(
            f"<div style='background:{_bg};border:1px solid {_bdr};"
            f"border-radius:8px;padding:8px 12px;margin-bottom:6px'>"
            f"<div style='display:flex;justify-content:space-between'>"
            f"<span style='color:#e2e8f0;font-weight:700;font-size:0.85rem'>"
            f"{'👁 '+_eye+' — ' if _eye and _eye not in ('O','OTHER','') else '🖼 '}{_pname}"
            f"</span>"
            f"<span style='color:{_color};font-size:0.78rem;font-weight:700'>{_status_lbl}</span>"
            f"</div>"
            f"<div style='color:#64748b;font-size:0.72rem;margin-top:3px'>"
            f"{_route}" + (f" · {_sph}" + (f" {_cyl}" if _cyl else "") if _sph != '—' else "")
            + (f" · Supplier: {_supp}" if _supp else "") + "</div></div>",
            unsafe_allow_html=True
        )

        if not _done:
            with st.expander(f"📦 Mark as Received / Update", expanded=False):
                _mc1, _mc2 = st.columns(2)
                _recv_qty = _mc1.number_input(
                    "Qty Received", min_value=0, max_value=_needed,
                    value=_ready, step=1,
                    key=f"lab_recv_{_key_sfx}"
                )
                _sup_ids  = [s["id"] for s in _suppliers]
                _sup_lbls = {s["id"]: s["name"] for s in _suppliers}
                _def_sup  = _sup_ids[0] if _sup_ids else ""
                _sel_sup  = _mc2.selectbox(
                    "Supplier / Lab",
                    _sup_ids, format_func=lambda x: _sup_lbls.get(x, x),
                    key=f"lab_sup_{_key_sfx}"
                )
                _exp_del = st.date_input(
                    "Expected Delivery",
                    value=datetime.date.today() + datetime.timedelta(days=7),
                    key=f"lab_exp_{_key_sfx}"
                )
                if st.button(
                    f"✅ Update — {_recv_qty} pcs received",
                    key=f"lab_upd_{_key_sfx}",
                    type="primary",
                    disabled=(_recv_qty == _ready)
                ):
                    try:
                        from modules.sql_adapter import run_write as _rw_lab
                        _rw_lab("""
                            UPDATE order_lines
                            SET ready_qty   = %(rq)s,
                                supplier_id = CASE WHEN %(sid)s = '' THEN supplier_id
                                                   ELSE %(sid)s::uuid END
                            WHERE id = %(lid)s::uuid
                        """, {"rq": _recv_qty, "sid": _sel_sup or "", "lid": _lid})
                        line["ready_qty"] = _recv_qty
                        if _recv_qty >= _needed:
                            st.success(f"✅ {_pname} — all {_recv_qty} pcs received from {_sup_lbls.get(_sel_sup,'supplier')}")
                        else:
                            st.info(f"Updated: {_recv_qty}/{_needed} received")
                        st.rerun()
                    except Exception as _le: st.error(f"Update failed: {_le}")

    # ── Bulk supplier assignment (all unassigned lines) ───────────────────
    _unassigned = [l for l in all_vendor if not l.get("supplier_id") and not l.get("supplier_name")]
    if _unassigned:
        st.markdown("---")
        st.markdown("**📋 Assign Supplier for unassigned lines:**")
        _sup_ids  = [s["id"] for s in _suppliers]
        _sup_lbls = {s["id"]: s["name"] for s in _suppliers}
        _bulk_sup = st.selectbox(
            "Supplier / Lab",
            _sup_ids, format_func=lambda x: _sup_lbls.get(x, x),
            key=f"lab_bulk_sup_{order_id[:8]}"
        )
        if st.button("✅ Assign to all pending lines",
                     key=f"lab_bulk_assign_{order_id[:8]}",
                     type="primary"):
            try:
                import json as _jbulk
                from modules.sql_adapter import run_write as _rw_bulk
                for _ul in _unassigned:
                    _bl_id = str(_ul.get("line_id") or _ul.get("id") or "")
                    _lp_bl = _ul.get("lens_params") or {}
                    if isinstance(_lp_bl, str):
                        try: _lp_bl = _jbulk.loads(_lp_bl)
                        except: _lp_bl = {}
                    _lp_bl["supplier_id"]   = _bulk_sup
                    _lp_bl["supplier_name"] = _sup_lbls.get(_bulk_sup,"")
                    _rw_bulk("""
                        UPDATE order_lines
                        SET lens_params = %(lp)s::jsonb
                        WHERE id = %(lid)s::uuid
                    """, {"lp": _jbulk.dumps(_lp_bl), "lid": _bl_id})
                st.success(f"✅ Assigned {_sup_lbls.get(_bulk_sup)} to {len(_unassigned)} lines")
                st.rerun()
            except Exception as _be: st.error(f"Failed: {_be}")


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
                    str(b.get('batch_no') or 'N/A') for b in line.get('batch_allocation', [])
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


def _calc_line_display_total(line: dict) -> tuple:
    """
    Compute (subtotal, gst_amt, grand_total) for display from live line fields.
    unit_price in DB = price per BOX for BOX unit, price per PCS otherwise.
    """
    upr      = float(line.get("unit_price") or 0)
    bq       = int(line.get("billing_qty") or line.get("quantity") or 0)
    box_sz   = int(line.get("box_size") or 1)
    unit     = str(line.get("unit") or "PCS").upper()
    gst_pct  = float(line.get("gst_percent_used") or line.get("gst_percent") or 0)
    tax_inc  = line.get("tax_inclusive", True)

    if upr <= 0:
        # Fallback to stored values
        sub = float(line.get("billing_total") or line.get("total_price") or 0)
        gst = float(line.get("gst_amount") or 0)
        return sub, gst, sub + gst

    # unit_price is ALWAYS per-PCS (normalize_to_pcs_price divides at punch time;
    # wholesale enrichment below also stores per-PCS after dividing by box_size).
    sub = round(bq * upr, 2)

    if tax_inc and gst_pct:
        # RETAIL: MRP is GST-inclusive — back-calculate tax out of price
        # sub = total paid by customer (GST already inside)
        # taxable_base = sub × 100 / (100 + rate)
        # grand_total  = sub  (not sub + gst — that would double-count)
        gst  = round(sub * gst_pct / (100 + gst_pct), 2)
        base = round(sub - gst, 2)
        return base, gst, sub          # total = sub (inclusive)
    else:
        # WHOLESALE/PURCHASE: price is ex-GST, tax added on top
        gst = round(sub * gst_pct / 100, 2)
        return sub, gst, sub + gst     # total = sub + gst (exclusive)

def render_order_detail():
    """
    Render detailed order view with all workflow components

    CRITICAL FIXES:
    1. Power editing triggers complete workflow
    2. Allocation window appears automatically
    3. Billing updates in real-time
    4. Ophthalmic job cards render correctly
    """

    # Pre-initialize all_lines so it is never unbound regardless of execution path.
    # The real population happens below after categorize_order_lines() is called.
    all_lines = []

    # 
    # 1. Resolve order_id
    # 
    order_id = st.session_state.bo_selected_order_id

    # If order_id looks like a UUID, resolve it to order_no
    # (production page previously set UUID; now sets order_no, but old session may have UUID)
    import re as _re_oid
    if order_id and _re_oid.match(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        str(order_id), _re_oid.IGNORECASE
    ):
        try:
            from modules.sql_adapter import run_query as _rq_oid
            _ono_row = _rq_oid(
                "SELECT order_no FROM orders WHERE id=%(oid)s::uuid LIMIT 1",
                {"oid": order_id}
            )
            if _ono_row and _ono_row[0].get("order_no"):
                order_id = _ono_row[0]["order_no"]
                st.session_state.bo_selected_order_id = order_id
        except Exception:
            pass

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

    # Fallback: reload from DB if not in active list (e.g. after page reload / cache clear)
    if not order:
        try:
            from .backoffice_helpers import load_orders_from_database, categorize_order_lines
            _fresh = load_orders_from_database(limit=500, include_closed=True)
            for o in _fresh:
                if get_display_order_id(o) == order_id:
                    order = o
                    # Merge back into session so subsequent reruns find it
                    _existing_nos = {get_display_order_id(x) for x in st.session_state.bo_active_orders}
                    if order_id not in _existing_nos:
                        st.session_state.bo_active_orders.append(o)
                    break
        except Exception as _reload_err:
            pass

    if not order:
        st.error(f"Order {order_id} not found — it may have been deleted or is outside the current load window.")
        if st.button("↩ Back to Dashboard", key="back_to_dashboard_order_not_found"):
            st.session_state.bo_view_mode = 'dashboard'
            st.rerun()
        return

    # ── Live DB line refresh ─────────────────────────────────────────────
    # Always re-fetch order_lines from DB before rendering.
    # Prevents stale cache issues when lines were added/changed in another tab.
    try:
        from modules.backoffice.order_edit_view import _load_lines as _boev_load
        _live_lines = _boev_load(str(order.get("id") or ""))
        if _live_lines:
            # Restore extra fields from lens_params that _load_lines doesn't fetch
            import json as _jll
            for _ll in _live_lines:
                _lp = _ll.get("lens_params") or {}
                if isinstance(_lp, str):
                    try: _lp = _jll.loads(_lp)
                    except: _lp = {}
                _ll["lens_params"] = _lp
                # Restore display_product_name, batch_no, colour, frame_group
                for _attr in ("display_product_name","batch_no","colour_mix","frame_group",
                              "batch_allocation","manufacturing_route",
                              "batch_status","billing_qty","gst_percent","is_service_line"):
                    # allocated_qty handled separately below — DB column is authoritative
                    if _lp.get(_attr) is not None and not _ll.get(_attr):
                        _ll[_attr] = _lp[_attr]
                if _lp.get("display_product_name") and not _ll.get("product_name"):
                    _ll["product_name"] = _lp["display_product_name"]
                # billing_qty = quantity for lines from _load_lines
                if not _ll.get("billing_qty"):
                    _ll["billing_qty"] = _ll.get("quantity", 0)
                # Restore batch_allocation — priority:
                # 1. suggested_allocation column (written by allocation window)
                # 2. lens_params batch_allocation (written by full order save)
                _sa_col = _ll.get("suggested_allocation")
                if _sa_col and not _ll.get("batch_allocation"):
                    import json as _jsa
                    if isinstance(_sa_col, str):
                        try: _sa_col = _jsa.loads(_sa_col)
                        except: _sa_col = []
                    if isinstance(_sa_col, list) and _sa_col:
                        _ll["batch_allocation"] = _sa_col
                if not _ll.get("batch_allocation") and _lp.get("batch_allocation"):
                    _ll["batch_allocation"] = _lp["batch_allocation"]
                # Sanitise batch_allocation: coerce all values to Python natives
                # (lens_params parsed from JSON/pandas may contain numpy types)
                _ba_raw = _ll.get("batch_allocation") or []
                if _ba_raw:
                    _ll["batch_allocation"] = [
                        {k: (str(v) if k == "batch_no" else
                             int(v) if k in ("allocated_qty","qty") else
                             float(v) if k == "selling_price" else v)
                         for k, v in _ba.items()}
                        for _ba in _ba_raw if isinstance(_ba, dict)
                    ]
                    # Recompute allocated_qty from batch_allocation — batch_allocation
                    # is the authoritative source after allocation window saves it
                    _ba_sum = sum(
                        int(b.get("allocated_qty", 0))
                        for b in _ll["batch_allocation"]
                        if isinstance(b, dict)
                    )
                    if _ba_sum > 0:
                        # batch_allocation has data → use it regardless of DB column
                        _ll["allocated_qty"] = _ba_sum
                        _ll["batch_status"]  = _lp.get("batch_status", "ALLOCATED")
                        _ll["manufacturing_route"] = (
                            _lp.get("manufacturing_route")
                            or _ll.get("manufacturing_route")
                            or "STOCK"
                        )
                if not _ll.get("manufacturing_route") and _lp.get("manufacturing_route"):
                    _ll["manufacturing_route"] = _lp["manufacturing_route"]
            order["lines"] = _live_lines
    except Exception as _ll_err:
        pass  # Non-fatal: fall back to cached lines

    # ── GST enrichment — fill gst_percent for any line still showing 0 ──
    # Uses the same resolve_gst_percent() as the tax engine (single source of truth).
    # Priority: ol.gst_percent (DB) → product_gst_history table → main_group heuristic
    import datetime as _dt_gst
    _bill_date_gst = _dt_gst.date.today()
    _gst_lookup_fn = None
    try:
        from modules.backoffice.backoffice_helpers import _make_gst_lookup_public
        _gst_lookup_fn = _make_gst_lookup_public()
    except Exception:
        # _make_gst_lookup is nested — build inline
        try:
            from modules.sql_adapter import run_query as _rq_gst
            _gst_rows = _rq_gst("""
                SELECT product_id::text, gst_percent, effective_from
                FROM product_gst_history
                ORDER BY effective_from DESC
            """) or []
            _gst_hist = {}
            for _gr in _gst_rows:
                _pid_h = str(_gr.get("product_id") or "")
                _eff_h = _gr.get("effective_from")
                if isinstance(_eff_h, str):
                    try: _eff_h = _dt_gst.date.fromisoformat(_eff_h[:10])
                    except: _eff_h = None
                _gst_hist.setdefault(_pid_h, []).append(
                    (_eff_h, float(_gr.get("gst_percent") or 0))
                )
            for _pid_h in _gst_hist:
                _gst_hist[_pid_h].sort(
                    key=lambda x: x[0] or _dt_gst.date.min, reverse=True
                )
            def _gst_lookup_fn(product_id, bill_date):
                entries = _gst_hist.get(str(product_id), [])
                bd = bill_date if isinstance(bill_date, _dt_gst.date) else _dt_gst.date.today()
                for eff, pct in entries:
                    if eff is None or eff <= bd:
                        return pct
                return entries[-1][1] if entries else None
        except Exception:
            _gst_lookup_fn = None

    try:
        from modules.pricing.tax_engine import resolve_gst_percent as _resolve_gst
        _has_resolve = True
    except Exception:
        _has_resolve = False

    for _gl in order.get("lines", []):
        _gl_gst = float(_gl.get("gst_percent") or 0)
        if _gl_gst > 0:
            _gl["gst_percent_used"] = _gl_gst
            continue
        # Use tax engine resolver (same as apply_taxes uses)
        if _has_resolve:
            try:
                _gl_gst = _resolve_gst(_gl, _bill_date_gst, _gst_lookup_fn, order)
            except Exception:
                _gl_gst = 0.0
        # Final fallback: Indian GST heuristic by product category
        if not _gl_gst:
            _mg_gst = str(_gl.get("main_group") or "").lower()
            if "ophthalmic" in _mg_gst:
                _gl_gst = 12.0  # HSN 9001
            elif "contact" in _mg_gst or "rgp" in _mg_gst:
                _gl_gst = 12.0  # HSN 9001
            elif "frame" in _mg_gst or "sunglass" in _mg_gst:
                _gl_gst = 5.0   # HSN 9003
            elif "solution" in _mg_gst:
                _gl_gst = 18.0  # HSN 3004
            elif "accessory" in _mg_gst or "accessories" in _mg_gst:
                _gl_gst = 18.0
        if _gl_gst > 0:
            _gl["gst_percent"]      = _gl_gst
            _gl["gst_percent_used"] = _gl_gst

    # Ensure line categories are always populated (stock/inhouse/lab buckets)
    try:
        from .backoffice_helpers import categorize_order_lines
        categorize_order_lines(order)
    except Exception:
        pass

    # Resolve order_type early — used throughout this function
    # Must be defined before any section that references it (e.g. Other Items header)
    order_type = str(order.get("order_type") or "RETAIL").upper()

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

    # ── Sticky order banner ───────────────────────────────────────────────────
    _order_no   = order.get("order_no") or order.get("order_id") or "—"
    _seq_no     = order.get("display_order_no")
    _seq_label  = f"#{int(_seq_no):04d}" if _seq_no else ""
    _patient    = order.get("patient_name") or "—"
    # Single source of truth — get_live_status() computes from DB state
    try:
        from modules.backoffice.order_status_live import get_live_status as _gls_hdr, compute_order_status as _cos_hdr
        _ord_status = _cos_hdr(order, write=True)   # compute + persist if changed
    except Exception:
        _ord_status = str(order.get("status") or "PENDING").upper()
    _ord_date   = str(order.get("created_at") or "")[:10]

    # Billing-frozen flag — blocks all line edits when order is in billing pipeline
    # CONFIRMED onwards: backoffice is the single authority.
    # Line-level edits (power, product swap for lenses) are frozen.
    # Frame SKU/colour/price change remains allowed via assignment panel.
    # Billing pipeline statuses freeze EVERYTHING including frame changes.
    _CONFIRMED_FROZEN_STATUSES = {"CONFIRMED", "IN_PRODUCTION", "READY"}
    _BILLING_FROZEN_STATUSES = {
        "READY_FOR_BILLING", "PARTIALLY_BILLED",
        "BILLED", "DISPATCHED", "DELIVERED", "CLOSED",
    }
    _is_confirmed_frozen = _ord_status.upper() in _CONFIRMED_FROZEN_STATUSES
    _billing_frozen = _ord_status.upper() in _BILLING_FROZEN_STATUSES

    # Override: lock if ANY active challan exists for this order
    # Covers consultation orders that skip normal status flow
    if not _billing_frozen:
        try:
            from modules.sql_adapter import run_query as _rq_chk
            _challan_exists = _rq_chk("""
                SELECT 1 FROM challans c
                WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
                  AND c.status NOT IN ('CANCELLED','VOID')
                  AND COALESCE(c.is_deleted, FALSE) = FALSE
                LIMIT 1
            """, {"oid": str(order.get("id") or ""), "ono": str(order.get("order_no") or "")})
            if _challan_exists:
                _billing_frozen = True
        except Exception:
            pass
    # Combined: any frozen state blocks lens-level edits
    _any_frozen = _is_confirmed_frozen or _billing_frozen

    # ── Smart Workflow Guide ─────────────────────────────────────────────────
    # Context-aware step guide — shows operator exactly what to do next
    def _render_workflow_guide(all_lines=None):
        all_lines = all_lines or []
        # Use get_live_status() — the single source of truth
        try:
            from modules.backoffice.order_status_live import get_live_status as _gls
            _st = _gls(order).upper()
        except Exception:
            _st = _ord_status.upper()

        if _st in ("CLOSED","CANCELLED","DELIVERED"):
            return

        # ── Live line-level state computation ─────────────────────────────
        _all_order_lines = [l for l in all_lines
                            if not str(l.get("eye_side","")).upper() in ("S","SERVICE")
                            and not l.get("is_service_line")]

        # Confirmed: order is locked
        _confirmed = _st not in ("PENDING","PROVISIONAL","UNDER_REVIEW")

        # Assignment: all lines have a route set
        _all_assigned = _confirmed and all(
            str(l.get("manufacturing_route") or
                (l.get("lens_params") or {}).get("manufacturing_route") or "")
            for l in _all_order_lines
        )

        # Production: compute per-route live state
        _prod_lines = [l for l in _all_order_lines
                       if str(l.get("manufacturing_route") or
                              (l.get("lens_params") or {}).get("manufacturing_route") or
                              "STOCK").upper() in ("VENDOR","EXTERNAL_LAB","INHOUSE")]
        _stock_only = not _prod_lines  # only stock lines → no production step

        _prod_total  = len(_prod_lines)
        _prod_ready  = sum(
            1 for l in _prod_lines
            if int(l.get("ready_qty") or 0) >= int(l.get("quantity") or l.get("billing_qty") or 1)
        )
        _prod_done   = (_prod_total == 0) or (_prod_ready >= _prod_total)

        # Billing: count billed vs total
        _billed_total = len(_all_order_lines)
        _billed_count = sum(1 for l in _all_order_lines
                            if int(l.get("billed_qty") or 0) > 0)
        _fully_billed = _billed_count >= _billed_total and _billed_total > 0
        _part_billed  = 0 < _billed_count < _billed_total

        # ── Build steps based on actual state ─────────────────────────────
        if _st in ("PENDING","PROVISIONAL","UNDER_REVIEW"):
            _steps = [
                ("1", "Review Lines",   "Check products, powers, prices", False, True),
                ("2", "Assign Routes",  "Set route per line in Assignment", False, False),
                ("3", "Confirm Order",  "Save to Order → locks the order", True,  False),
            ]
        elif not _confirmed:
            _steps = [
                ("1", "Confirm",  "Save order to confirm", True, False),
            ]
        elif _stock_only:
            # Stock-only: Confirm → Billing (no production step)
            _steps = [
                ("1", "Confirmed",  f"✅ {_billed_count}/{_billed_total} lines", False, True),
                ("2", "Billing",
                 ("✅ Fully billed" if _fully_billed else
                  f"⚡ {_billed_count}/{_billed_total} billed" if _part_billed else
                  "Create Challan → Invoice in Billing Summary tab"),
                 not _fully_billed, _fully_billed),
            ]
        else:
            # Has production lines: Confirm → Production → Billing
            _prod_hint = (
                f"✅ All {_prod_total} line(s) received/ready" if _prod_done else
                f"{_prod_ready}/{_prod_total} line(s) ready — check Supplier/Lab tabs"
            )
            _bill_hint = (
                "✅ Fully billed" if _fully_billed else
                f"⚡ {_billed_count}/{_billed_total} billed — Create Challan for remaining" if _part_billed else
                "Create Challan → Invoice in Billing Summary tab"
            )
            _steps = [
                ("1", "Confirmed",   "✅",         False, True),
                ("2", "Production",  _prod_hint,   not _prod_done, _prod_done),
                ("3", "Billing",     _bill_hint,   _prod_done and not _fully_billed, _fully_billed),
            ]

        # ── Render compact horizontal stepper ─────────────────────────────
        _cols = st.columns(len(_steps))
        for i, (num, title, hint, is_current, done) in enumerate(_steps):
            with _cols[i]:
                _bg    = "#0d2040" if is_current else ("#0f2a1a" if done else "#0f172a")
                _border= "#6366f1" if is_current else ("#22c55e" if done else "#1e293b")
                _color = "#a5b4fc" if is_current else ("#86efac" if done else "#475569")
                _icon  = "▶" if is_current else ("✅" if done else "○")
                st.markdown(
                    f"<div style='background:{_bg};border:1.5px solid {_border};"
                    f"border-radius:8px;padding:8px 10px;text-align:center;min-height:52px'>"
                    f"<div style='color:{_color};font-weight:700;font-size:0.8rem'>"
                    f"{_icon} {num}. {title}</div>"
                    f"<div style='color:#64748b;font-size:0.65rem;margin-top:2px'>{hint}</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )

    # Build all_lines HERE so every block below (banners, supplier reassign, tabs) can access it
    try:
        from .backoffice_helpers import categorize_order_lines
        categorize_order_lines(order)
    except Exception:
        pass

    all_lines = []
    all_lines.extend(order.get('stock_lines', []))
    all_lines.extend(order.get('inhouse_lines', []))
    all_lines.extend(order.get('lab_order_lines', []))
    # SERVICE lines (consultation fee, eye testing) — re-include so they
    # appear in billing summary and billing_status_panel. Each sub-panel
    # excludes them individually via eye_side checks.
    all_lines.extend(order.get('service_lines', []))

    # Render workflow guide now that all_lines is available
    _render_workflow_guide(all_lines=all_lines)

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

    # Banner for CONFIRMED stage (partial freeze — frame SKU still editable)
    if _is_confirmed_frozen and not _billing_frozen:
        _conf_stage_msgs = {
            "CONFIRMED":     "Order confirmed by backoffice. Power and product changes are locked. Frame SKU/colour can still be changed in Assignment.",
            "IN_PRODUCTION": "Order is in production. All line edits are locked. Use Release to edit.",
            "READY":         "Order is ready for dispatch. All line edits are locked.",
        }
        _conf_msg = _conf_stage_msgs.get(_ord_status.upper(), "Order is confirmed — edits locked in retail punching.")
        st.markdown(
            f"<div style='background:#0f1e3a;border-left:4px solid #6366f1;"
            f"border-radius:6px;padding:8px 14px;margin-bottom:10px;"
            f"color:#a5b4fc;font-size:0.8rem'>🔒 {_conf_msg}</div>",
            unsafe_allow_html=True,
        )

    if _billing_frozen:
        _freeze_msg = {
            "READY_FOR_BILLING": "Order is in the billing queue — all edits are locked until recalled.",
            "PARTIALLY_BILLED":  "Order is partially billed — all edits are locked until recalled.",
            "BILLED":            "Order is billed. Raise a Credit Note to make corrections.",
            "DISPATCHED":        "Order has been dispatched. No further edits allowed.",
            "DELIVERED":         "Order has been delivered. No further edits allowed.",
            "CLOSED":            "Order is closed.",
        }.get(_ord_status.upper(), "Order is locked — no edits allowed at this stage.")
        st.markdown(
            f"<div style='background:#1a0a0a;border-left:4px solid #f59e0b;"
            f"border-radius:0 8px 8px 0;padding:10px 16px;margin-bottom:10px;"
            f"display:flex;align-items:center;gap:12px'>"
            f"<span style='font-size:1.1rem'>🔒</span>"
            f"<span style='color:#fbbf24;font-size:0.82rem;font-weight:600'>"
            f"{_freeze_msg}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Supplier Reassign — available on ANY confirmed/locked order ───────────
    # Surgical override: only changes supplier_id in lens_params + audit log.
    # Power, price, qty, route — all untouched. No full unlock needed.
    if _any_frozen and _ord_status.upper() not in ("BILLED","DISPATCHED","DELIVERED","CLOSED","CANCELLED"):
        _vendor_lines = [
            l for l in all_lines
            if str(l.get("manufacturing_route") or
                   (l.get("lens_params") or {}).get("manufacturing_route") or
                   "").upper() in ("VENDOR","EXTERNAL_LAB")
        ]
        if _vendor_lines:
            with st.expander("🔄 Reassign Supplier (supplier OOS / rejected)", expanded=False):
                st.caption(
                    "Only supplier changes — powers, prices, routes are untouched. "
                    "Use this when your assigned supplier can't fulfil the order."
                )
                # Load suppliers
                try:
                    from modules.sql_adapter import run_query as _rq_rs
                    _rs_rows = _rq_rs("""
                        SELECT id::text, party_name FROM parties
                        WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR')
                          AND COALESCE(is_active,TRUE)=TRUE
                        ORDER BY party_name
                    """, {}) or []
                    _rs_ids  = [r["id"] for r in _rs_rows]
                    _rs_lbls = {r["id"]: r["party_name"] for r in _rs_rows}
                except Exception:
                    _rs_ids = []; _rs_lbls = {}

                for _vl in _vendor_lines:
                    _vl_lid   = str(_vl.get("line_id") or "")
                    _vl_eye   = str(_vl.get("eye_side","")).upper()
                    _vl_pname = str(_vl.get("product_name","")).split(" | ")[0]
                    _vl_lp    = _vl.get("lens_params") or {}
                    if isinstance(_vl_lp, str):
                        import json as _jrs
                        try: _vl_lp = _jrs.loads(_vl_lp)
                        except: _vl_lp = {}
                    _cur_sup  = str(_vl_lp.get("supplier_name") or _vl_lp.get("supplier_id") or "—")
                    _eye_lbl  = f"👁 {_vl_eye} " if _vl_eye not in ("O","OTHER","") else "🖼 "

                    _rc1, _rc2, _rc3, _rc4 = st.columns([2, 2, 1, 1])
                    _rc1.markdown(f"**{_eye_lbl}{_vl_pname}**")
                    _rc1.caption(f"Current: {_cur_sup}")

                    if _rs_ids:
                        _cur_id  = str(_vl_lp.get("supplier_id") or "")
                        _def_idx = _rs_ids.index(_cur_id) if _cur_id in _rs_ids else 0
                        _new_sup = _rc2.selectbox(
                            "New Supplier",
                            _rs_ids, index=_def_idx,
                            format_func=lambda x: _rs_lbls.get(x,x),
                            key=f"rs_sel_{_vl_lid}",
                            label_visibility="collapsed"
                        )
                        _rs_reason = _rc3.selectbox(
                            "Reason",
                            ["OOS","Rejected","Quality","Other"],
                            key=f"rs_rsn_{_vl_lid}",
                            label_visibility="collapsed"
                        )
                        if _rc4.button("✅", key=f"rs_save_{_vl_lid}",
                                       type="primary",
                                       use_container_width=True,
                                       disabled=(_new_sup == _cur_id)):
                            try:
                                import json as _jrs2
                                from modules.sql_adapter import run_write as _rw_rs
                                _vl_lp["supplier_id"]   = _new_sup
                                _vl_lp["supplier_name"] = _rs_lbls.get(_new_sup,"")
                                _rw_rs("""
                                    UPDATE order_lines
                                    SET lens_params = %(lp)s::jsonb
                                    WHERE id = %(lid)s::uuid
                                """, {"lp": _jrs2.dumps(_vl_lp), "lid": _vl_lid})
                                # Audit log
                                try:
                                    from modules.backoffice.audit_logger import audit, AuditAction
                                    audit(AuditAction.PRODUCT_CHANGED,
                                          entity="order_lines", entity_id=_vl_lid,
                                          order_id=str(order.get("id","")),
                                          user_id=st.session_state.get("user_name","backoffice"),
                                          payload={"action":"supplier_reassigned",
                                                   "old":_cur_sup,
                                                   "new":_rs_lbls.get(_new_sup,""),
                                                   "reason":_rs_reason})
                                except Exception: pass
                                st.success(f"✅ Reassigned to {_rs_lbls.get(_new_sup)}")
                                st.rerun()
                            except Exception as _rse: st.error(str(_rse))

    st.markdown(
        f"<div style='background:#0f172a;border:2px solid #3b82f6;border-radius:10px;"
        f"padding:10px 18px;margin-bottom:12px;display:flex;align-items:center;"
        f"justify-content:space-between;flex-wrap:wrap;gap:8px'>"
        f"<div style='display:flex;align-items:center;gap:16px'>"
        f"<span style='font-size:1.6rem;font-weight:900;color:#60a5fa;"
        f"font-family:monospace;letter-spacing:1px'>{_order_no}</span>"
        f"{'<span style="font-size:0.9rem;color:#94a3b8;font-family:monospace">' + _seq_label + '</span>' if _seq_label else ''}"
        f"<span style='font-size:1rem;color:#e2e8f0;font-weight:600'>👤 {_patient}</span>"
        f"</div>"
        f"<div style='display:flex;align-items:center;gap:10px'>"
        f"<span style='font-size:0.75rem;color:#64748b'>{_ord_date}</span>"
        f"<span style='background:#1e3a5f;color:#93c5fd;padding:3px 10px;"
        f"border-radius:4px;font-size:0.78rem;font-weight:700'>{_ord_status}</span>"
        f"</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    _hc1, _hc2 = st.columns([6, 1])
    with _hc2:
        if st.button("← Back", width='stretch'):
            st.session_state.bo_view_mode = "dashboard"
            st.session_state.bo_editing_line = None
            st.session_state.bo_show_allocation_window = False
            st.rerun()

    # ── Status + party row ────────────────────────────────────────────────────
    # Use get_live_status — single source of truth from order_status_live
    try:
        from modules.backoffice.order_status_live import get_live_status as _gls_ui, status_badge_html as _sbh
        _norm_st = _gls_ui(order)
        _status_badge_html = _sbh(_norm_st, size="0.78rem")
    except Exception:
        _norm_st = str(order.get("status") or "PENDING").upper()
        _status_badge_html = f"<span style='background:#64748b;color:#fff;padding:2px 12px;border-radius:14px;font-size:0.78rem;font-weight:700'>{_norm_st}</span>"
    _party    = order.get("patient_name") or order.get("party_name") or "—"
    _otype    = (order.get("order_type") or "RETAIL").upper()
    _TC = {"RETAIL":"#0891b2","WHOLESALE":"#8b5cf6","PURCHASE":"#f59e0b"}
    _tc = _TC.get(_otype, "#64748b")
    st.markdown(
        f"<div style='display:flex;align-items:center;gap:10px;margin:6px 0 10px'>"
        f"<span style='color:#cbd5e1;font-size:1rem;font-weight:700'>{_party}</span>"
        f"{_status_badge_html}"
        f"<span style='background:{_tc}22;color:{_tc};padding:2px 9px;border-radius:10px;"
        f"font-size:0.65rem;font-weight:700'>{_otype}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # ── Live timestamp timeline (collapsible) ─────────────────────────────────
    _STATION_ORDER = [
        ("PENDING",       "📥", "Order Received",       "#3b82f6"),
        ("UNDER_REVIEW",  "🔍", "Under Review",         "#f59e0b"),
        ("CONFIRMED",     "✅", "Confirmed",             "#6366f1"),
        ("IN_PRODUCTION", "⚙️", "In Production",        "#8b5cf6"),
        ("READY",         "📦", "Ready",                "#10b981"),
        ("BILLED",        "🧾", "Billed",               "#059669"),
        ("DISPATCHED",    "🚚", "Dispatched",           "#0891b2"),
        ("DELIVERED",     "✅", "Delivered",            "#10b981"),
        ("CLOSED",        "🔒", "Closed",               "#334155"),
    ]

    def _get_ts_map(order_no):
        try:
            from modules.sql_adapter import run_query
            rows = run_query("""
                SELECT h.to_status, MIN(h.changed_at) AS changed_at,
                       (array_agg(h.changed_by_name ORDER BY h.changed_at))[1] AS by
                FROM order_status_history h
                JOIN orders o ON o.id = h.order_id
                WHERE o.order_no = %(ono)s
                GROUP BY h.to_status
            """, {"ono": order_no}) or []
            _MAP = {"PENDING_VALIDATION":"PENDING","PROVISIONAL":"PENDING","ORDER_SAVED":"PENDING"}
            result = {}
            for r in rows:
                sts = _MAP.get((r.get("to_status") or "").upper(),
                               (r.get("to_status") or "").upper())
                ts  = str(r.get("changed_at") or "")[:16].replace("T"," ")
                by  = r.get("by") or "system"
                if sts and sts not in result:
                    result[sts] = {"ts": ts, "by": by}
            # Always have PENDING from created_at
            if "PENDING" not in result:
                _ca = order.get("created_at") or order.get("order_date") or ""
                if _ca:
                    result["PENDING"] = {
                        "ts": str(_ca)[:16].replace("T"," "),
                        "by": "system"
                    }
            return result
        except Exception:
            return {}

    _ts_map   = _get_ts_map(_order_no)

    # Strip BILLED from timeline if no actual billing document exists
    if "BILLED" in _ts_map:
        try:
            from modules.sql_adapter import run_query as _rq_tl
            _oid_tl = str(order.get("id") or "")
            _ono_tl = str(order.get("order_no") or "")
            _bdocs = _rq_tl("""
                SELECT 1 FROM challans
                WHERE (order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR order_ids::text[] @> ARRAY[%(ono)s::text])
                  AND status NOT IN ('CANCELLED','VOID')
                UNION ALL
                SELECT 1 FROM invoices
                WHERE (order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR order_ids::text[] @> ARRAY[%(ono)s::text])
                  AND status NOT IN ('CANCELLED','VOID')
                LIMIT 1
            """, {"oid": _oid_tl, "ono": _ono_tl}) or []
            if not _bdocs:
                _ts_map.pop("BILLED", None)
        except Exception:
            pass

    _cur_idx  = next((i for i,(k,*_) in enumerate(_STATION_ORDER) if k == _norm_st), 0)
    # Always show received + current timestamps inline above the expander
    _recv_ts  = (_ts_map.get("PENDING") or {}).get("ts") or str(order.get("created_at",""))[:16]
    _conf_ts  = (_ts_map.get("CONFIRMED") or {}).get("ts") or ""

    _ts_pills = (
        f"<span style='font-size:0.68rem;color:#94a3b8'>📥 Received: "
        f"<b style='color:#3b82f6'>{_recv_ts or '—'}</b></span>"
    )
    if _conf_ts:
        _ts_pills += (
            f"&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"<span style='font-size:0.68rem;color:#94a3b8'>✅ Collected: "
            f"<b style='color:#6366f1'>{_conf_ts}</b></span>"
        )
    st.markdown(f"<div style='margin-bottom:6px'>{_ts_pills}</div>", unsafe_allow_html=True)

    # ── Live production train (order-level + inhouse job pipeline) ────────────
    try:
        from modules.backoffice.order_status_window import _render_train_inline
        _render_train_inline(order)
    except Exception as _te:
        # Fallback: basic status badge if train fails
        st.caption(f"Status: {order.get('status','PENDING')}")

    
    #  NEW: Trigger product change dialog if modal is active
    if st.session_state.get('bo_product_change_modal', {}).get('active', False):
        product_change_dialog()
    
    # ── Stamp tax_inclusive on every line from order_type ───────────────────────
    # tax_inclusive is NOT persisted to DB — must be derived here on every load.
    # RETAIL  : MRP is GST-inclusive  → True  (back-calculate GST from price)
    # WHOLESALE/PURCHASE: price is GST-exclusive → False (GST added on top)
    _order_type_enrich = (order.get("order_type") or "RETAIL").upper()
    _tax_inc_flag = (_order_type_enrich == "RETAIL")
    for _line in all_lines:
        _line["tax_inclusive"] = _tax_inc_flag

    if _order_type_enrich == "WHOLESALE":
        try:
            from modules.sql_adapter import run_query as _rq
        except ImportError:
            _rq = None

        for line in all_lines:
            # Skip SERVICE lines — consultation fee price must not be overwritten
            # by the wholesale selling_price enrichment loop.
            if str(line.get("eye_side","")).upper() in ("SERVICE","S"):
                continue
            if not line.get("product_id") or not _rq:
                continue
            try:
                pid = str(line["product_id"])

                # Priority 1: inventory_stock.selling_price (batch-level wholesale price)
                _inv = _rq("""
                    SELECT selling_price, mrp
                    FROM inventory_stock
                    WHERE product_id = %(pid)s::uuid AND is_active = true
                    ORDER BY created_at DESC
                    LIMIT 1
                """, {"pid": pid})

                if _inv and _inv[0].get("selling_price"):
                    raw_price = float(_inv[0]["selling_price"] or 0)
                else:
                    # Priority 2: products table selling_price or unit_price
                    _prod = _rq("""
                        SELECT selling_price, unit_price, mrp
                        FROM products
                        WHERE id = %(pid)s::uuid
                        LIMIT 1
                    """, {"pid": pid})
                    if _prod:
                        row = _prod[0]
                        raw_price = float(
                            row.get("selling_price") or
                            row.get("unit_price") or
                            row.get("mrp") or 0
                        )
                    else:
                        raw_price = 0.0

                if raw_price <= 0:
                    continue  # Don't overwrite with zero — keep existing

                # Divide BOX price by box_size so unit_price is always per-PCS
                _box_sz = max(1, int(line.get("box_size") or 1))
                new_price = round(raw_price / _box_sz, 2)

                line["selling_price"] = new_price
                line["unit_price"]    = new_price

            except Exception as _pe:
                import logging
                logging.warning(f"[BO] Price enrich failed for {line.get('product_name')}: {_pe}")

    # Rebuild r_lines and l_lines AFTER price enrichment
    r_lines = [line for line in all_lines if line.get('eye_side', '').upper() in ['R', 'RIGHT']]
    l_lines = [line for line in all_lines if line.get('eye_side', '').upper() in ['L', 'LEFT']]
    other_lines = [line for line in all_lines if line.get('eye_side', '').upper() not in ['R', 'RIGHT', 'L', 'LEFT']]

    # ── Pending payment claims from customers ─────────────────────────
    try:
        from modules.billing.payment_link_manager import render_pending_claims_dashboard
        render_pending_claims_dashboard()
    except Exception:
        pass

    # Tabs for different sections
    _show_dispatch = _ord_status.upper() in (
        "BILLED","DISPATCHED","DELIVERED","CLOSED","PARTIALLY_BILLED"
    )

    # ── Jump to Billing tab if navigated from Production page ────────────
    # production_page._go_to_billing() sets bo_jump_to_billing=True.
    # We inject a JS snippet that programmatically clicks the 4th tab button
    # (index 3 = "💰 Billing Summary") once, then clears the flag.
    _jump_billing = st.session_state.pop("bo_jump_to_billing", False)
    if _jump_billing:
        st.components.v1.html("""
        <script>
        setTimeout(function() {
            var tabs = window.parent.document.querySelectorAll('[data-baseweb="tab"]');
            if (tabs && tabs.length > 3) { tabs[3].click(); }
        }, 300);
        </script>
        """, height=0)

    if _show_dispatch:
        tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
            "📦 Order Items",
            "📄 Documents",
            "📊 Status",
            "💰 Billing Summary",
            "🚚 Supplier Orders",
            "🚀 Dispatch",
        ])
    else:
        tab1, tab2, tab3, tab4, tab5 = st.tabs([
            "📦 Order Items",
            "📄 Documents",
            "📊 Status",
            "💰 Billing Summary",
            "🚚 Supplier Orders",
        ])
        tab6 = None
    
    with tab1:
        st.markdown("###  Order Line Items")

        # ── Payment status strip (read-only) ──────────────────────────
        try:
            from modules.billing.payment_manager import render_payment_strip
            render_payment_strip(order, all_lines)
        except Exception:
            pass
        # ──────────────────────────────────────────────────────────────

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
            # Skip OTHER/SERVICE lines — they render in the frame card below
            _eye_pg = str(line.get('eye_side') or '').upper()
            if _eye_pg not in ('R','RIGHT','L','LEFT'):
                continue

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

            if _eye_pg in ('RIGHT','R'):
                product_groups[pair_id]['R'] = line
                product_groups[pair_id]['R_idx'] = idx
            elif _eye_pg in ('LEFT','L'):
                product_groups[pair_id]['L'] = line
                product_groups[pair_id]['L_idx'] = idx

        # Display each product with R and L side by side
        if not product_groups:
            if not all_lines:
                st.warning("⚠️ No order lines found in DB for this order. Check if lines were saved correctly.")
            elif not other_lines:
                # all_lines exist but none are R/L and none are B/OTHER — genuinely unexpected
                st.warning(f"⚠️ {len(all_lines)} lines found but no product groups built. Check eye_side values.")
                for _dl in all_lines[:3]:
                    st.caption(f"Line: product={_dl.get('product_name','?')} eye={_dl.get('eye_side','?')} route={_dl.get('manufacturing_route','?')}")
            # else: all lines are B/OTHER (frames, accessories, solutions) — they render
            # in the FRAMES & OTHER ITEMS section below. No warning needed.

        for product_id, group in product_groups.items():
            # Compact product header — base name only (strip SKU suffix)
            _hdr_name = str(group['product_name'] or '').split(' | ')[0]
            _hdr_brand = str(group.get('brand') or '')
            st.markdown(
                f"<div style='display:flex;align-items:baseline;gap:10px;margin-bottom:2px'>"
                f"<span style='font-weight:700;font-size:1rem;color:#e2e8f0'>{_hdr_name}</span>"
                + (f"<span style='font-size:0.72rem;color:#475569'>{_hdr_brand}</span>" if _hdr_brand and _hdr_brand != 'N/A' else "")
                + "</div>",
                unsafe_allow_html=True
            )
            
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
                import math as _math, json as _lpj, base64 as _b64

                # ── Parse lens_params once ───────────────────────────
                _lp = line.get("lens_params") or {}
                if isinstance(_lp, str):
                    try: _lp = _lpj.loads(_lp)
                    except: _lp = {}

                # ── Line-level freeze check (single source of truth) ─
                # True once a blank is allocated + job card saved,
                # OR when the order is in the billing pipeline.
                # All edit paths below check this flag — nothing bypasses it.
                _line_frozen = (
                    _any_frozen  # CONFIRMED+ or billing pipeline = frozen
                    or bool(
                        line.get("surfacing_data") or
                        (isinstance(_lp, dict) and _lp.get("surfacing_data"))
                    )
                )

                _raw_colour  = str(_lp.get("colour") or "").strip()
                _colour      = "" if _raw_colour.lower() in ("none","no","") else _raw_colour
                _fit_req     = bool(_lp.get("fitting_required"))
                _fit_type    = str(_lp.get("fitting_type") or "").strip()
                _instruct    = str(_lp.get("instructions") or "").strip()
                _tint_b64    = str(_lp.get("tint_sample_b64") or "").strip()
                _frame_t     = str(_lp.get("frame_type") or "").strip()
                _thick       = str(_lp.get("thickness") or "").strip()

                eye_title = "👁 RIGHT EYE" if eye_label == "R" else "👁 LEFT EYE"
                eye_color = "#0f2744"     if eye_label == "R" else "#0f2a1a"
                eye_border= "#3b82f644"  if eye_label == "R" else "#10b98144"

                st.markdown(
                    f"<div style='background:{eye_color};border-left:4px solid "
                    f"{'#3b82f6' if eye_label=='R' else '#10b981'};"
                    f"color:#e2e8f0;padding:8px 14px;border-radius:8px;"
                    f"font-weight:700;font-size:0.95rem;margin-bottom:8px'>"
                    f"{eye_title}</div>",
                    unsafe_allow_html=True,
                )

                with st.container(border=True):

                    # ══ ROW 1: Product + Power ════════════════════════
                    _r1a, _r1b = st.columns([3, 2])
                    with _r1a:
                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid #1e293b;"
                            f"border-radius:8px;padding:10px 14px'>"
                            f"<div style='color:#60a5fa;font-weight:700;font-size:0.88rem'>"
                            f"{line.get('product_name','—')}</div>"
                            f"<div style='color:#475569;font-size:0.7rem;margin-top:3px'>"
                            f"{line.get('brand','—')} · {line.get('main_group','—')} · {line.get('unit','PCS')}"
                            f"</div></div>",
                            unsafe_allow_html=True)
                        if not _line_frozen:
                            if st.button("✏️ Change Product",
                                         key=f"change_product_{eye_label}_{idx}",
                                         width='stretch'):
                                st.session_state['bo_product_change_modal'] = {
                                    'active': True, 'line': line,
                                    'idx': idx, 'eye_label': eye_label, 'order': order
                                }
                                st.rerun()
                        else:
                            st.caption("🔒 Locked")

                    with _r1b:
                        _cyl = line.get('cyl')
                        _add_p = line.get('add_power')
                        _has_cyl = (_cyl is not None and
                                    not (isinstance(_cyl, float) and _math.isnan(_cyl))
                                    and abs(float(_cyl or 0)) > 0.01)
                        _has_add = (_add_p is not None and
                                    not (isinstance(_add_p, float) and _math.isnan(_add_p))
                                    and float(_add_p or 0) > 0)
                        _pw = []
                        if line.get('sph') is not None:
                            _pw.append(f"<span style='color:#94a3b8'>SPH</span> <b>{fmt_signed(line.get('sph'))}</b>")
                        if _has_cyl:
                            _pw.append(f"<span style='color:#94a3b8'>CYL</span> <b>{fmt_signed(_cyl)}</b>")
                            if line.get('axis') is not None:
                                _pw.append(f"<span style='color:#94a3b8'>AX</span> <b>{line.get('axis')}</b>")
                        if _has_add:
                            _pw.append(f"<span style='color:#94a3b8'>ADD</span> <b>{fmt_signed(_add_p)}</b>")
                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid #1e293b;"
                            f"border-radius:8px;padding:10px 14px'>"
                            f"<div style='color:#64748b;font-size:0.62rem;margin-bottom:4px'>🔭 RX POWER</div>"
                            f"<div style='color:#e2e8f0;font-size:0.82rem;line-height:2'>"
                            f"{'  &nbsp; '.join(_pw) if _pw else '<span style="color:#334155">—</span>'}"
                            f"</div></div>",
                            unsafe_allow_html=True)
                        is_editing = st.session_state.get('bo_editing_line') == idx
                        if _line_frozen:
                            # Clear stale editing state so form doesn't sneak through
                            if is_editing:
                                st.session_state.bo_editing_line = None
                            _freeze_reason = (
                                "Order in billing queue — recall to CONFIRMED to edit"
                                if _billing_frozen else
                                "Power locked — job card saved"
                            )
                            st.markdown(
                                f"<div style='background:#1a0a00;border:1px solid #f97316;"
                                f"border-radius:6px;padding:5px 10px;margin-top:6px'>"
                                f"<span style='color:#fb923c;font-size:0.78rem;font-weight:700'>"
                                f"🔒 {_freeze_reason}</span></div>",
                                unsafe_allow_html=True)
                        elif not is_editing:
                            if st.button("✏️ Edit Power",
                                         key=f"edit_{eye_label}_{idx}",
                                         width='stretch'):
                                st.session_state.bo_editing_line = idx
                                st.rerun()
                        else:
                            render_power_edit_ui(line, idx, order)

                    # ══ ROW 2: Lens spec badges ═══════════════════════
                    _badges = []
                    if _frame_t and _frame_t.lower() not in ("full", "full rim"):
                        _badges.append(f"<span style='background:#1e293b;color:#94a3b8;padding:3px 9px;border-radius:20px;font-size:0.7rem'>🖼 {_frame_t}</span>")
                    if _thick and _thick.lower() not in ("regular",""):
                        _badges.append(f"<span style='background:#1e293b;color:#94a3b8;padding:3px 9px;border-radius:20px;font-size:0.7rem'>📏 {_thick}</span>")
                    if _fit_req:
                        _fit_lbl = f"Fitting" + (f" · {_fit_type}" if _fit_type else "")
                        _badges.append(f"<span style='background:#2d1b69;color:#c4b5fd;border:1px solid #7c3aed;padding:3px 9px;border-radius:20px;font-size:0.7rem;font-weight:700'>🔧 {_fit_lbl}</span>")
                    if _colour:
                        _badges.append(f"<span style='background:#4a0526;color:#f9a8d4;border:1px solid #be185d;padding:3px 9px;border-radius:20px;font-size:0.7rem;font-weight:700'>🎨 {_colour}</span>")

                    if _badges:
                        st.markdown(
                            "<div style='display:flex;flex-wrap:wrap;gap:6px;padding:6px 0'>"
                            + "".join(_badges) + "</div>",
                            unsafe_allow_html=True)

                    # Tint sample image
                    if _tint_b64:
                        try:
                            _img_bytes = _b64.b64decode(_tint_b64)
                            _ti1, _ti2 = st.columns([1, 3])
                            with _ti1:
                                st.image(_img_bytes, caption="🎨 Tint", width=100)
                            with _ti2:
                                st.markdown(
                                    f"<div style='color:#f9a8d4;font-size:0.75rem;padding-top:8px'>"
                                    f"Tint sample attached<br>"
                                    f"<span style='color:#64748b'>Match colour before processing</span>"
                                    f"</div>", unsafe_allow_html=True)
                        except Exception:
                            st.caption("⚠️ Tint image could not be loaded")

                    # Lab instructions
                    if _instruct:
                        st.markdown(
                            f"<div style='background:#1a1200;border-left:3px solid #f59e0b;"
                            f"padding:6px 12px;border-radius:0 6px 6px 0;margin:4px 0'>"
                            f"<span style='color:#f59e0b;font-size:0.68rem;font-weight:700'>📝 LAB NOTES</span>"
                            f"<div style='color:#fcd34d;font-size:0.78rem;margin-top:2px'>{_instruct}</div>"
                            f"</div>", unsafe_allow_html=True)

                    # ══ ROW 3: Qty + Allocation ═══════════════════════
                    _r3a, _r3b = st.columns([1, 2])
                    with _r3a:
                        st.markdown("<div style='color:#64748b;font-size:0.65rem;margin-bottom:2px'>📦 QTY</div>", unsafe_allow_html=True)
                        current_qty = int(line.get("billing_qty") or 1)
                        new_qty = st.number_input(
                            "Qty", min_value=1, value=current_qty, step=1,
                            key=f"clean_qty_{eye_label}_{idx}",
                            label_visibility="collapsed")
                        if new_qty != current_qty:
                            line["billing_qty"]      = int(new_qty)
                            line["batch_allocation"] = []
                            line["allocated_qty"]    = 0
                            line["batch_status"]     = "PENDING"
                            suggested = line.get("suggested_allocation")
                            if suggested:
                                if sum(b.get("allocated_qty", 0) for b in suggested) == int(new_qty):
                                    line["batch_allocation"] = suggested
                                else:
                                    st.warning("⚠️ Qty changed — allocation discarded")
                            refresh_line_state(line)
                            recalculate_order_totals(order)
                            from .backoffice_helpers import categorize_order_lines
                            categorize_order_lines(order)
                            st.rerun()

                    with _r3b:
                        allocated   = int(line.get("allocated_qty") or 0)
                        billing_qty = int(line.get("billing_qty") or 0)
                        pending     = max(0, billing_qty - allocated)
                        _alloc_ok   = allocated >= billing_qty > 0
                        _ac = "#10b981" if _alloc_ok else ("#f59e0b" if allocated > 0 else "#ef4444")
                        _al = f"✅ {allocated}/{billing_qty}" if _alloc_ok else (
                              f"⚡ {allocated}/{billing_qty}" if allocated > 0 else "⬜ Not allocated")
                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid {_ac}44;"
                            f"border-radius:8px;padding:8px 10px'>"
                            f"<div style='color:#64748b;font-size:0.62rem'>🗂️ ALLOCATION</div>"
                            f"<div style='color:{_ac};font-weight:700;font-size:0.8rem'>{_al}</div>"
                            + (f"<div style='color:#f59e0b;font-size:0.65rem'>⚠️ {pending} to order</div>" if pending > 0 else "")
                            + "</div>", unsafe_allow_html=True)
                        if st.button("🗂️ Manage Stock",
                                     key=f"alloc_{eye_label}_{idx}",
                                     width='stretch'):
                            st.session_state.bo_show_allocation_window = True
                            st.session_state.bo_allocation_line_idx    = idx
                            st.rerun()

                    # ══ Edit Lens Params (fitting / colour / instructions) ═
                    _lp_edit_key = f"lp_edit_{eye_label}_{idx}"
                    _lp_exp_label = "✏️ Edit Fitting / Colouring / Instructions"
                    if _fit_req or _colour:
                        _lp_exp_label = (
                            "✏️ Edit Lens Params"
                            + (" · 🔧" if _fit_req else "")
                            + (f" · 🎨 {_colour}" if _colour else "")
                        )
                    # ── Lens Params lock — reuse _line_frozen computed above ────
                    _lp_locked = _line_frozen
                    if _lp_locked:
                        _lp_exp_label = "🔒 " + _lp_exp_label

                    with st.expander(_lp_exp_label, expanded=False):
                        if _lp_locked:
                            st.markdown(
                                "<div style='background:#1a0a00;border:1px solid #f97316;"
                                "border-radius:6px;padding:8px 14px;margin-bottom:8px'>"
                                "<span style='color:#fb923c;font-weight:700'>🔒 Locked — job card saved</span>"
                                "<span style='color:#fed7aa;font-size:0.8rem;margin-left:8px'>"
                                "Cancel job card first (Documents → Job Cards).</span>"
                                "</div>",
                                unsafe_allow_html=True
                            )
                        _COLOURS = [
                            "None",
                            "Brown 10%","Brown 20%","Brown 30%","Brown 40%",
                            "Brown 50%","Brown 60%","Brown 70%","Brown 75%",
                            "Grey 20%","Grey 30%","Grey 40%","Grey 50%",
                            "Grey 60%","Grey 75%",
                            "Green 30%","Green 50%",
                            "Blue 30%","Blue 50%",
                            "Pink / Rose 20%","Pink / Rose 40%",
                            "Yellow / Amber",
                            "Gradient Brown","Gradient Grey","Gradient Blue",
                            "Gradient Green","Gradient Pink",
                            "Photochromic Brown","Photochromic Grey",
                            "Solid Black","Solid Brown","Solid Grey",
                            "Other (Manual)",
                        ]
                        _cur_colour = _lp.get("colour") or "None"
                        if _cur_colour not in _COLOURS:
                            _cur_colour = "Other (Manual)"
                        _cur_fit   = bool(_lp.get("fitting_required"))
                        _cur_ftype = _lp.get("fitting_type") or "Full Rim"
                        _cur_inst  = _lp.get("instructions") or ""
                        _cur_frame = _lp.get("frame_type") or "Full"
                        _cur_thick = _lp.get("thickness") or "Regular"
                        _cur_b64   = _lp.get("tint_sample_b64") or ""

                        import json as _lpj2, base64 as _b64e

                        _ea, _eb = st.columns(2)
                        with _ea:
                            _e_colour = st.selectbox(
                                "🎨 Colour / Tint",
                                options=_COLOURS,
                                index=_COLOURS.index(_cur_colour),
                                key=f"lpe_colour_{eye_label}_{idx}")
                            if _e_colour == "Other (Manual)":
                                _e_colour_manual = st.text_input(
                                    "Colour description",
                                    value=_lp.get("colour_manual") or "",
                                    key=f"lpe_cmanual_{eye_label}_{idx}")
                            else:
                                _e_colour_manual = ""
                        with _eb:
                            _e_fit = st.checkbox(
                                "🔧 Fitting Required",
                                value=_cur_fit,
                                key=f"lpe_fit_{eye_label}_{idx}")
                            if _e_fit:
                                _e_ftype = st.radio(
                                    "Fitting Type",
                                    ["Full Rim","Supra","Three Piece"],
                                    index=["Full Rim","Supra","Three Piece"].index(
                                        _cur_ftype if _cur_ftype in ["Full Rim","Supra","Three Piece"]
                                        else "Full Rim"),
                                    horizontal=True,
                                    key=f"lpe_ftype_{eye_label}_{idx}")
                            else:
                                _e_ftype = ""

                        _ef1, _ef2 = st.columns(2)
                        with _ef1:
                            _e_frame = st.radio(
                                "Frame Type",
                                ["Full","Rimless","Supra"],
                                index=["Full","Rimless","Supra"].index(
                                    _cur_frame if _cur_frame in ["Full","Rimless","Supra"] else "Full"),
                                horizontal=True,
                                key=f"lpe_frame_{eye_label}_{idx}")
                        with _ef2:
                            _e_thick = st.radio(
                                "Thickness",
                                ["Regular","Thin","Cartier Thick"],
                                index=["Regular","Thin","Cartier Thick"].index(
                                    _cur_thick if _cur_thick in ["Regular","Thin","Cartier Thick"] else "Regular"),
                                horizontal=True,
                                key=f"lpe_thick_{eye_label}_{idx}")

                        _e_inst = st.text_area(
                            "📝 Lab Instructions",
                            value=_cur_inst,
                            height=60,
                            key=f"lpe_inst_{eye_label}_{idx}")

                        # Tint sample
                        _e_b64 = _cur_b64
                        _up = st.file_uploader(
                            "🖼 Tint Sample Photo",
                            type=["jpg","jpeg","png","webp"],
                            key=f"lpe_tint_{eye_label}_{idx}",
                            help="Attach colour reference photo")
                        if _up:
                            _e_b64 = _b64e.b64encode(_up.read()).decode()
                            st.image(_b64e.b64decode(_e_b64), width=120, caption="Preview")
                        elif _cur_b64:
                            try:
                                st.image(_b64e.b64decode(_cur_b64), width=100, caption="Current sample")
                            except Exception:
                                st.caption("⚠️ Could not display current sample")

                        if st.button("💾 Save Lens Params",
                                     key=f"lpe_save_{eye_label}_{idx}",
                                     type="primary",
                                     width='stretch',
                                     disabled=_lp_locked):
                            _new_lp = {
                                **_lp,
                                "colour":          _e_colour if _e_colour != "Other (Manual)" else _e_colour_manual,
                                "colour_manual":   _e_colour_manual,
                                "fitting_required": _e_fit,
                                "fitting_type":    _e_ftype,
                                "frame_type":      _e_frame,
                                "thickness":       _e_thick,
                                "instructions":    _e_inst,
                                "tint_sample_b64": _e_b64,
                                "tinted":          _e_colour not in ("None", ""),
                            }
                            _line_id = str(line.get("id") or "")
                            if _line_id:
                                try:
                                    from modules.sql_adapter import run_write as _rw_lp
                                    _rw_lp(
                                        "UPDATE order_lines SET lens_params=%(lp)s::jsonb "
                                        "WHERE id=%(id)s::uuid",
                                        {"lp": _lpj2.dumps(_new_lp), "id": _line_id}
                                    )
                                    line["lens_params"] = _new_lp
                                    st.success("✅ Saved")
                                    st.rerun()
                                except Exception as _lpe_err:
                                    st.error(f"Save failed: {_lpe_err}")
                            else:
                                st.warning("⚠️ Line ID missing — save order first")

                    # ══ Remove Line (always available for unbilled lines) ══
                    _line_id_str  = str(line.get("line_id") or line.get("id") or "")
                    _billed_qty   = int(line.get("billed_qty") or 0)
                    _alloc_qty    = int(line.get("allocated_qty") or 0)

                    if _billed_qty == 0:
                        _del_confirm_key = f"bo_del_confirm_{_line_id_str}"
                        with st.expander("🗑️ Remove this line", expanded=False):
                            if _billed_qty > 0:
                                st.caption("🔒 Already billed — use Credit Note to correct")
                            elif st.session_state.get(_del_confirm_key):
                                st.warning(
                                    f"Remove **{eye_label} — {line.get('product_name', '')}** "
                                    f"from this order? This cannot be undone."
                                )
                                _dc1, _dc2 = st.columns(2)
                                with _dc1:
                                    if st.button(
                                        "✅ Yes, Remove",
                                        key=f"bo_del_yes_{_line_id_str}",
                                        type="primary",
                                        width='stretch',
                                    ):
                                        try:
                                            from modules.sql_adapter import run_write as _rw_del, run_query as _rq_del
                                            # Step 1: Fetch batch_allocation to reverse stock
                                            _line_for_del = next(
                                                (l for l in all_lines if str(l.get("line_id","") or l.get("id","")) == _line_id_str),
                                                {}
                                            )
                                            _batch_alloc = _line_for_del.get("batch_allocation") or []
                                            _lp_del = _line_for_del.get("lens_params") or {}
                                            if isinstance(_lp_del, str):
                                                import json as _jdel
                                                try: _lp_del = _jdel.loads(_lp_del)
                                                except: _lp_del = {}
                                            if not _batch_alloc:
                                                _batch_alloc = _lp_del.get("batch_allocation") or []

                                            # Step 2: Atomic soft-delete — RETURNING ensures
                                            # idempotency (double-click / retry safe)
                                            _deleted_rows = _rq_del("""
                                                UPDATE order_lines
                                                SET is_deleted = TRUE,
                                                    deleted_at = NOW(),
                                                    deleted_by = 'backoffice_edit',
                                                    stock_reversed = TRUE
                                                WHERE id = %(lid)s::uuid
                                                  AND COALESCE(billed_qty, 0) = 0
                                                  AND COALESCE(is_deleted, FALSE) = FALSE
                                                RETURNING id
                                            """, {"lid": _line_id_str})

                                            # Step 3: Only reverse stock if this delete
                                            # actually fired (guards double-click / retry)
                                            if _deleted_rows:
                                                for _ba in _batch_alloc:
                                                    _ba_pid  = str(_line_for_del.get("product_id") or "")
                                                    _ba_bno  = str(_ba.get("batch_no") or "")
                                                    _ba_qty  = int(_ba.get("allocated_qty") or _ba.get("qty") or 0)
                                                    if _ba_pid and _ba_qty > 0:
                                                        try:
                                                            # Per stock flow doc: delete = release SOFT reservation only
                                                            # allocated_qty ↓ — NOT quantity (that only changes at dispatch)
                                                            _rw_del("""
                                                                UPDATE inventory_stock
                                                                SET allocated_qty = GREATEST(0, COALESCE(allocated_qty, 0) - %(qty)s)
                                                                WHERE product_id = %(pid)s::uuid
                                                                  AND (%(bno)s = '' OR batch_no = %(bno)s)
                                                                LIMIT 1
                                                            """, {"pid": _ba_pid, "bno": _ba_bno, "qty": _ba_qty})
                                                        except Exception:
                                                            pass

                                            # Audit log the deletion
                                            try:
                                                from modules.backoffice.audit_logger import audit, AuditAction
                                                from modules.security.roles import current_user as _cu
                                                _del_user = (_cu() or {}).get("name","backoffice")
                                                audit(
                                                    AuditAction.PRODUCT_CHANGED,
                                                    entity    = "order_lines",
                                                    entity_id = _line_id_str,
                                                    order_id  = str(order.get("id","")),
                                                    user_id   = _del_user,
                                                    payload   = {
                                                        "action":       "line_deleted",
                                                        "product":      line.get("product_name",""),
                                                        "eye_side":     eye_label,
                                                        "qty_restored": sum(
                                                            int(_ba.get("allocated_qty") or 0)
                                                            for _ba in (_line_for_del.get("batch_allocation") or [])
                                                        ),
                                                    }
                                                )
                                            except Exception:
                                                pass

                                            st.session_state.pop(_del_confirm_key, None)
                                            st.success(f"✅ {eye_label} line removed — stock restored")
                                            st.rerun()
                                        except Exception as _del_err:
                                            st.error(f"Delete failed: {_del_err}")
                                with _dc2:
                                    if st.button(
                                        "← Cancel",
                                        key=f"bo_del_no_{_line_id_str}",
                                        width='stretch',
                                    ):
                                        st.session_state.pop(_del_confirm_key, None)
                                        st.rerun()
                            else:
                                _is_last_unbilled = (
                                    sum(
                                        1 for _al in all_lines
                                        if not _al.get("is_deleted")
                                        and int(_al.get("billed_qty") or 0) == 0
                                    ) <= 1
                                )
                                if _is_last_unbilled:
                                    st.warning("⚠️ Last unbilled line — order will be empty after removal")
                                if st.button(
                                    "🗑️ Remove Line",
                                    key=f"bo_del_btn_{_line_id_str}",
                                    width='stretch',
                                    help="Remove this line (unbilled only)",
                                ):
                                    st.session_state[_del_confirm_key] = True
                                    st.rerun()

                    # ══ JSON debug ════════════════════════════════════
                    with st.expander("🔍 Raw JSON", expanded=False):
                        st.json({
                            "line_id":       str(line.get("id") or ""),
                            "eye_side":      line.get("eye_side"),
                            "sph":           line.get("sph"),
                            "cyl":           None if (isinstance(line.get("cyl"), float) and _math.isnan(line.get("cyl") or 0)) else line.get("cyl"),
                            "axis":          line.get("axis"),
                            "add":           line.get("add_power"),
                            "qty":           line.get("billing_qty"),
                            "alloc_qty":     line.get("allocated_qty"),
                            "unit_price":    line.get("unit_price"),
                            "route":         line.get("manufacturing_route"),
                            "lens_params":   _lp,
                            "boxing_params": line.get("boxing_params") or {},
                        })

            # ── Render R and L using the shared helper ───────────────
            if has_right:
                with col_r:
                    try:
                        _render_eye_block_ui(group['R'], group['R_idx'], 'R')
                    except Exception as _re:
                        import traceback
                        st.error(f"R Eye render error: {_re}")
                        st.code(traceback.format_exc())

            if has_left:
                with col_l:
                    try:
                        _render_eye_block_ui(group['L'], group['L_idx'], 'L')
                    except Exception as _le:
                        import traceback
                        st.error(f"L Eye render error: {_le}")
                        st.code(traceback.format_exc())
        
        # ══════════════════════════════════════════════════════
        # ORDER-LEVEL SERVICE CHARGES (Fitting / Colouring / Courier)
        # One panel per order — not per eye
        # ══════════════════════════════════════════════════════
        _FITTING_PRICES = {
            "Full Rim SV":              30,
            "Full Rim V2":              70,
            "Full Rim Poly":            60,
            "Full Rim High Index":     100,
            "Supra SV":                 60,
            "Supra V2":                100,
            "Supra Poly":              120,
            "Supra High Index":        150,
            "Rimless SV / Poly":       100,
            "Rimless V2":              140,
            "Rimless V2 High Index":   180,
            "Custom / Manual":           0,
        }
        _COLOURING_PRICES = {
            "Solid Tint":              100,
            "Gradient Tint":           120,
            "Custom / Manual":           0,
        }

        _oid_sv = str(order.get("id") or "")
        _ckey_sv = f"svc_charges_{_oid_sv}"
        if _ckey_sv not in st.session_state:
            try:
                from modules.backoffice.order_charges_panel import fetch_charges
                st.session_state[_ckey_sv] = fetch_charges(_oid_sv)
            except Exception:
                st.session_state[_ckey_sv] = []
        _oc_sv   = st.session_state.get(_ckey_sv, [])
        _ctot_sv = sum(float(x.get("total_amount") or 0) for x in _oc_sv)
        _fc_fit  = next((x for x in _oc_sv if x["charge_type"] == "FITTING"),   None)
        _fc_col  = next((x for x in _oc_sv if x["charge_type"] == "COLOURING"), None)
        _fc_cour = next((x for x in _oc_sv if x["charge_type"] == "COURIER"),   None)

        # Collect fitting/colour hints from any line
        _any_fit_req = any(
            bool((l.get("lens_params") or {}).get("fitting_required"))
            for l in all_lines)
        _any_colours = list({
            str((l.get("lens_params") or {}).get("colour") or "").strip()
            for l in all_lines
            if str((l.get("lens_params") or {}).get("colour") or "").strip().lower()
               not in ("", "none", "no")
        })

        _svc_pending = bool((_any_fit_req and not _fc_fit) or (_any_colours and not _fc_col))
        _svc_lbl = (
            f"💰 Service Charges  ✅  ₹{_ctot_sv:,.0f}" if _ctot_sv > 0 else
            "💰 Service Charges  ⚠️ pricing required"    if _svc_pending else
            "💰 Service Charges"
        )

        with st.expander(_svc_lbl, expanded=bool(_svc_pending and _ctot_sv == 0)):

            # ── Fitting row ─────────────────────────────────────────
            st.markdown(
                "<div style='background:#0f172a;border:1px solid #334155;"
                "border-radius:8px;padding:10px 14px;margin-bottom:8px'>",
                unsafe_allow_html=True)
            _fh1, _fh2 = st.columns([0.4, 3.6])
            with _fh1:
                st.markdown("<div style='font-size:1.5rem;text-align:center;padding-top:6px'>🔧</div>",
                            unsafe_allow_html=True)
            with _fh2:
                if _fc_fit:
                    _fb = float(_fc_fit.get("amount") or 0)
                    _ft = float(_fc_fit.get("total_amount") or 0)
                    _fg = float(_fc_fit.get("gst_percent") or 0)
                    _fd = _fc_fit.get("description") or "Fitting"
                    _fc1sv, _fc2sv = st.columns([3, 1])
                    with _fc1sv:
                        st.markdown(
                            f"<div style='color:#c4b5fd;font-weight:700'>{_fd}</div>"
                            f"<div style='color:#64748b;font-size:0.72rem'>"
                            f"₹{_fb:,.0f} + {_fg:.0f}% GST = "
                            f"<span style='color:#10b981;font-weight:700'>₹{_ft:,.0f}</span></div>",
                            unsafe_allow_html=True)
                    with _fc2sv:
                        if st.button("🗑 Remove", key=f"del_fit_{_oid_sv}",
                                     width='stretch'):
                            try:
                                from modules.backoffice.order_charges_panel import delete_charge
                                delete_charge(str(_fc_fit["id"]))
                            except Exception: pass
                            st.session_state.pop(_ckey_sv, None); st.rerun()
                else:
                    _fit_form_key = f"fit_form_{_oid_sv}"
                    if not st.session_state.get(_fit_form_key):
                        _hint = ""
                        if _any_fit_req:
                            _lp0 = (all_lines[0].get("lens_params") or {}) if all_lines else {}
                            _ft0 = _lp0.get("fitting_type") or ""
                            _hint = f"Fitting required · {_ft0}" if _ft0 else "Fitting required"
                        if _hint:
                            st.markdown(f"<div style='color:#a78bfa;font-size:0.72rem;margin-bottom:4px'>{_hint}</div>",
                                        unsafe_allow_html=True)
                        if st.button("＋ Add Fitting Charge", key=f"open_fit_{_oid_sv}",
                                     width='stretch'):
                            st.session_state[_fit_form_key] = True; st.rerun()
                    else:
                        _fit_options = list(_FITTING_PRICES.keys())
                        def _on_fit_sel_change():
                            _sel = st.session_state.get(f"fit_sel_{_oid_sv}")
                            _p   = _FITTING_PRICES.get(_sel, 0)
                            st.session_state[f"fit_amt_{_oid_sv}"] = float(_p)

                        _fit_sel = st.selectbox("Fitting Type", _fit_options,
                                                key=f"fit_sel_{_oid_sv}",
                                                on_change=_on_fit_sel_change)
                        _fit_preset = _FITTING_PRICES[_fit_sel]
                        # Seed amount in session_state if not yet set
                        if f"fit_amt_{_oid_sv}" not in st.session_state:
                            st.session_state[f"fit_amt_{_oid_sv}"] = float(_fit_preset)
                        _ff1, _ff2, _ff3 = st.columns([2, 1, 1])
                        with _ff1:
                            _fit_amt = st.number_input(
                                "Amount ₹", min_value=0.0, step=5.0,
                                key=f"fit_amt_{_oid_sv}",
                                help="Pre-filled from price master — edit if needed")
                        with _ff2:
                            _fit_gst = st.number_input("GST %", min_value=0.0,
                                                        max_value=28.0, value=18.0,
                                                        step=0.5, key=f"fit_gst_{_oid_sv}")
                        with _ff3:
                            if _fit_amt > 0:
                                _fit_tot = _fit_amt + round(_fit_amt * _fit_gst / 100, 2)
                                st.markdown(
                                    f"<div style='color:#10b981;font-size:0.75rem;padding-top:28px'>"
                                    f"Total: ₹{_fit_tot:,.0f}</div>",
                                    unsafe_allow_html=True)
                        _fs1, _fs2 = st.columns(2)
                        with _fs1:
                            if st.button("✅ Save Fitting", type="primary",
                                         key=f"fit_save_{_oid_sv}", width='stretch'):
                                if _fit_amt > 0:
                                    try:
                                        from modules.backoffice.order_charges_panel import save_charge
                                        save_charge(_oid_sv, "FITTING",
                                                    f"Fitting · {_fit_sel}",
                                                    _fit_amt, _fit_gst, "", "",
                                                    st.session_state.get("user_name","System"))
                                    except Exception as _e: st.error(str(_e))
                                    st.session_state.pop(_fit_form_key, None)
                                    st.session_state.pop(_ckey_sv, None); st.rerun()
                                else: st.error("Enter amount > 0")
                        with _fs2:
                            if st.button("✕ Cancel", key=f"fit_cancel_{_oid_sv}",
                                         width='stretch'):
                                st.session_state.pop(_fit_form_key, None); st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

            # ── Colouring row ───────────────────────────────────────
            st.markdown(
                "<div style='background:#0f172a;border:1px solid #334155;"
                "border-radius:8px;padding:10px 14px;margin-bottom:8px'>",
                unsafe_allow_html=True)
            _ch1, _ch2 = st.columns([0.4, 3.6])
            with _ch1:
                st.markdown("<div style='font-size:1.5rem;text-align:center;padding-top:6px'>🎨</div>",
                            unsafe_allow_html=True)
            with _ch2:
                if _fc_col:
                    _cb = float(_fc_col.get("amount") or 0)
                    _ct2= float(_fc_col.get("total_amount") or 0)
                    _cg = float(_fc_col.get("gst_percent") or 0)
                    _cd = _fc_col.get("description") or "Colouring"
                    _cc1sv, _cc2sv = st.columns([3, 1])
                    with _cc1sv:
                        st.markdown(
                            f"<div style='color:#f9a8d4;font-weight:700'>{_cd}</div>"
                            f"<div style='color:#64748b;font-size:0.72rem'>"
                            f"₹{_cb:,.0f} + {_cg:.0f}% GST = "
                            f"<span style='color:#10b981;font-weight:700'>₹{_ct2:,.0f}</span></div>",
                            unsafe_allow_html=True)
                    with _cc2sv:
                        if st.button("🗑 Remove", key=f"del_col_{_oid_sv}",
                                     width='stretch'):
                            try:
                                from modules.backoffice.order_charges_panel import delete_charge
                                delete_charge(str(_fc_col["id"]))
                            except Exception: pass
                            st.session_state.pop(_ckey_sv, None); st.rerun()
                else:
                    _col_form_key = f"col_form_{_oid_sv}"
                    if not st.session_state.get(_col_form_key):
                        if _any_colours:
                            _col_hint = "  ·  ".join(_any_colours)
                            st.markdown(
                                f"<div style='color:#f9a8d4;font-size:0.72rem;margin-bottom:4px'>"
                                f"🎨 {_col_hint}</div>", unsafe_allow_html=True)
                        if st.button("＋ Add Colouring Charge", key=f"open_col_{_oid_sv}",
                                     width='stretch'):
                            st.session_state[_col_form_key] = True; st.rerun()
                    else:
                        _col_options = list(_COLOURING_PRICES.keys())
                        def _on_col_sel_change():
                            _sel = st.session_state.get(f"col_sel_{_oid_sv}")
                            _p   = _COLOURING_PRICES.get(_sel, 0)
                            st.session_state[f"col_amt_{_oid_sv}"] = float(_p)

                        _col_sel = st.selectbox("Colouring Type", _col_options,
                                                key=f"col_sel_{_oid_sv}",
                                                on_change=_on_col_sel_change)
                        _col_preset = _COLOURING_PRICES[_col_sel]
                        # Seed amount in session_state if not yet set
                        if f"col_amt_{_oid_sv}" not in st.session_state:
                            st.session_state[f"col_amt_{_oid_sv}"] = float(_col_preset)
                        # Show colour from lens_params as info
                        if _any_colours:
                            st.markdown(
                                f"<div style='color:#f9a8d4;font-size:0.72rem;margin-bottom:4px'>"
                                f"Colour ordered: {' · '.join(_any_colours)}</div>",
                                unsafe_allow_html=True)
                        _cf1, _cf2, _cf3 = st.columns([2, 1, 1])
                        with _cf1:
                            _col_amt = st.number_input(
                                "Amount ₹", min_value=0.0, step=5.0,
                                key=f"col_amt_{_oid_sv}",
                                help="Pre-filled from price master — edit if needed")
                        with _cf2:
                            _col_gst = st.number_input("GST %", min_value=0.0,
                                                        max_value=28.0, value=18.0,
                                                        step=0.5, key=f"col_gst_{_oid_sv}")
                        with _cf3:
                            if _col_amt > 0:
                                _col_tot = _col_amt + round(_col_amt * _col_gst / 100, 2)
                                st.markdown(
                                    f"<div style='color:#10b981;font-size:0.75rem;padding-top:28px'>"
                                    f"Total: ₹{_col_tot:,.0f}</div>",
                                    unsafe_allow_html=True)
                        _cs1, _cs2 = st.columns(2)
                        with _cs1:
                            if st.button("✅ Save Colouring", type="primary",
                                         key=f"col_save_{_oid_sv}", width='stretch'):
                                if _col_amt > 0:
                                    _col_desc = f"Colouring · {_col_sel}"
                                    if _any_colours:
                                        _col_desc += f" ({', '.join(_any_colours)})"
                                    try:
                                        from modules.backoffice.order_charges_panel import save_charge
                                        save_charge(_oid_sv, "COLOURING", _col_desc,
                                                    _col_amt, _col_gst, "", "",
                                                    st.session_state.get("user_name","System"))
                                    except Exception as _e: st.error(str(_e))
                                    st.session_state.pop(_col_form_key, None)
                                    st.session_state.pop(_ckey_sv, None); st.rerun()
                                else: st.error("Enter amount > 0")
                        with _cs2:
                            if st.button("✕ Cancel", key=f"col_cancel_{_oid_sv}",
                                         width='stretch'):
                                st.session_state.pop(_col_form_key, None); st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

            # ── Courier row ─────────────────────────────────────────
            st.markdown(
                "<div style='background:#0f172a;border:1px solid #334155;"
                "border-radius:8px;padding:10px 14px'>",
                unsafe_allow_html=True)
            _kr1, _kr2 = st.columns([0.4, 3.6])
            with _kr1:
                st.markdown("<div style='font-size:1.5rem;text-align:center;padding-top:6px'>📦</div>",
                            unsafe_allow_html=True)
            with _kr2:
                if _fc_cour:
                    _kb  = float(_fc_cour.get("amount") or 0)
                    _kt  = float(_fc_cour.get("total_amount") or 0)
                    _kg  = float(_fc_cour.get("gst_percent") or 0)
                    _kcc = _fc_cour.get("courier_company") or ""
                    _ktn = _fc_cour.get("tracking_no") or ""
                    _kc1, _kc2 = st.columns([3, 1])
                    with _kc1:
                        st.markdown(
                            f"<div style='color:#7dd3fc;font-weight:700'>"
                            f"📦 {_kcc}" + (f" · {_ktn}" if _ktn else "") + "</div>"
                            f"<div style='color:#64748b;font-size:0.72rem'>"
                            f"₹{_kb:,.0f} + {_kg:.0f}% GST = "
                            f"<span style='color:#10b981;font-weight:700'>₹{_kt:,.0f}</span></div>",
                            unsafe_allow_html=True)
                    with _kc2:
                        if st.button("🗑 Remove", key=f"del_cour_{_oid_sv}",
                                     width='stretch'):
                            try:
                                from modules.backoffice.order_charges_panel import delete_charge
                                delete_charge(str(_fc_cour["id"]))
                            except Exception: pass
                            st.session_state.pop(_ckey_sv, None); st.rerun()
                else:
                    _COURIER_PRICES = {
                        "₹40  — Local":    40,
                        "₹60  — Standard": 60,
                        "₹80  — Express":  80,
                        "₹100 — Priority": 100,
                        "₹150 — Urgent":   150,
                        "Custom / Manual":   0,
                    }
                    _cour_form_key = f"cour_form_{_oid_sv}"
                    if not st.session_state.get(_cour_form_key):
                        if st.button("＋ Add Courier Charges", key=f"open_cour_{_oid_sv}",
                                     width='stretch'):
                            st.session_state[_cour_form_key] = True; st.rerun()
                    else:
                        _kf1, _kf2 = st.columns(2)
                        with _kf1:
                            try:
                                from modules.backoffice.order_charges_panel import fetch_courier_companies
                                _clist = fetch_courier_companies()
                            except Exception:
                                _clist = ["Blue Dart","DTDC","Delhivery","Ekart","Other"]
                            _k_cc = st.selectbox("Courier Company", _clist, key=f"cour_cc_{_oid_sv}")
                        with _kf2:
                            _k_tn = st.text_input("Tracking No.", placeholder="AWB / docket no.",
                                                   key=f"cour_tn_{_oid_sv}")

                        def _on_cour_sel_change():
                            _sel = st.session_state.get(f"cour_sel_{_oid_sv}")
                            _p   = _COURIER_PRICES.get(_sel, 0)
                            st.session_state[f"cour_amt_{_oid_sv}"] = float(_p)

                        _cour_sel = st.selectbox("Charge Slab", list(_COURIER_PRICES.keys()),
                                                  key=f"cour_sel_{_oid_sv}",
                                                  on_change=_on_cour_sel_change)
                        if f"cour_amt_{_oid_sv}" not in st.session_state:
                            st.session_state[f"cour_amt_{_oid_sv}"] = float(_COURIER_PRICES[_cour_sel])

                        _kf3, _kf4, _kf5 = st.columns([2, 1, 1])
                        with _kf3:
                            _k_amt = st.number_input("Courier Charges ₹", min_value=0.0,
                                                      step=10.0, key=f"cour_amt_{_oid_sv}")
                        with _kf4:
                            _k_gst = st.number_input("GST %", min_value=0.0, max_value=28.0,
                                                      value=18.0, step=0.5, key=f"cour_gst_{_oid_sv}")
                        with _kf5:
                            if _k_amt > 0:
                                _k_tot = _k_amt + round(_k_amt * _k_gst / 100, 2)
                                st.markdown(
                                    f"<div style='color:#10b981;font-size:0.75rem;padding-top:28px'>"
                                    f"Total: ₹{_k_tot:,.0f}</div>", unsafe_allow_html=True)
                        _ks1, _ks2 = st.columns(2)
                        with _ks1:
                            if st.button("✅ Save Courier", type="primary",
                                         key=f"cour_save_{_oid_sv}", width='stretch'):
                                if _k_amt > 0:
                                    try:
                                        from modules.backoffice.order_charges_panel import save_charge
                                        save_charge(_oid_sv, "COURIER",
                                                    f"Courier · {_k_cc}",
                                                    _k_amt, _k_gst, _k_cc, _k_tn,
                                                    st.session_state.get("user_name","System"))
                                    except Exception as _e: st.error(str(_e))
                                    st.session_state.pop(_cour_form_key, None)
                                    st.session_state.pop(_ckey_sv, None); st.rerun()
                                else: st.error("Enter amount > 0")
                        with _ks2:
                            if st.button("✕ Cancel", key=f"cour_cancel_{_oid_sv}",
                                         width='stretch'):
                                st.session_state.pop(_cour_form_key, None); st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

            # ── Charges total ────────────────────────────────────────
            if _ctot_sv > 0:
                st.markdown(
                    f"<div style='background:#0d1f0d;border:1px solid #10b98144;"
                    f"border-radius:8px;padding:10px 16px;margin-top:4px;"
                    f"display:flex;justify-content:space-between;align-items:center'>"
                    f"<span style='color:#64748b;font-size:0.78rem'>Total Service Charges (excl. GST)</span>"
                    f"<span style='color:#10b981;font-weight:800;font-size:1.1rem'>"
                    f"₹{_ctot_sv:,.2f}</span></div>",
                    unsafe_allow_html=True)

        #  RENDER ALLOCATION WINDOW IF ACTIVE
        if st.session_state.get('bo_show_allocation_window', False):
            line_idx = st.session_state.get('bo_allocation_line_idx')
            
            if line_idx is not None and line_idx < len(all_lines):
                line = all_lines[line_idx]
                render_allocation_window(line, line_idx, order)

        # ========== FINAL SAVE TO ORDER ==========
        st.markdown("---")
        st.markdown("###  Order Summary")

        if st.button(" System Health Check", width='stretch'):

            issues = run_system_health_check(order)

            if not issues:
                st.success(" System OK. No issues found.")
            else:
                st.error(" Issues Found:")
                for i in issues:
                    st.write("", i)
        
        # Calculate order totals with R/L breakdown
        total_items = len(all_lines)
        
        # Use live _calc_line_display_total[2] — correct inclusive/exclusive total
        r_billing  = round(sum(_calc_line_display_total(l)[2] for l in r_lines), 2)
        r_discount = sum(float(line.get('discount_amount', 0) or 0) for line in r_lines)
        
        l_billing  = round(sum(_calc_line_display_total(l)[2] for l in l_lines), 2)
        l_discount = sum(float(line.get('discount_amount', 0) or 0) for line in l_lines)
        
        other_billing  = round(sum(_calc_line_display_total(l)[2] for l in other_lines), 2)
        other_discount = sum(float(line.get('discount_amount', 0) or 0) for line in other_lines)
        
        # Grand totals
        # Live billing total — same computation as footer and line display
        total_billing  = sum(_calc_line_display_total(l)[2] for l in all_lines)
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
                    _, _gst_d, total = _calc_line_display_total(line)
                    lcols = st.columns([3, 2, 1, 1, 1, 1, 1])
                    lcols[0].write(f"{idx}. {line.get('product_name', 'N/A')}")
                    lcols[1].write(qty_disp)
                    lcols[2].write(f"₹{unit_price:,.2f}")
                    lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                    lcols[4].write(gst_label)
                    lcols[5].write(f"₹{_gst_d:,.2f}" if _gst_d else ("⚠️" if gst_pct else "—"))
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
                    _, _gst_d, total = _calc_line_display_total(line)
                    lcols = st.columns([3, 2, 1, 1, 1, 1, 1])
                    lcols[0].write(f"{idx}. {line.get('product_name', 'N/A')}")
                    lcols[1].write(qty_disp)
                    lcols[2].write(f"₹{unit_price:,.2f}")
                    lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                    lcols[4].write(gst_label)
                    lcols[5].write(f"₹{_gst_d:,.2f}" if _gst_d else ("⚠️" if gst_pct else "—"))
                    lcols[6].write(f"₹{total:,.2f}")
            else:
                st.caption("No Left Eye items")
        
        # Other Items Section - Compact (if any)
        # ═══════════════════════════════════════════════════════
        # 🖼 FRAMES & OTHER ITEMS — unified card panel
        # Same card design as R/L lens cards.
        # Each card: product info + attributes + allotment + edit
        # ═══════════════════════════════════════════════════════
        if other_lines:
            st.markdown(
                "<div style='background:#0a1628;border-left:4px solid #8b5cf6;"
                "color:#c4b5fd;padding:8px 14px;border-radius:8px;"
                "font-weight:700;font-size:0.95rem;margin-bottom:8px'>"
                "🖼 FRAMES & OTHER ITEMS</div>",
                unsafe_allow_html=True,
            )

            for _oi, line in enumerate(other_lines):
                _oid_o  = str(order.get("id") or "")
                _lid_o  = str(line.get("line_id") or line.get("id") or f"o{_oi}")
                _pname  = str(line.get("product_name") or "—")
                _brand  = str(line.get("brand") or "")
                _mg     = str(line.get("main_group") or "")
                _qty    = int(line.get("billing_qty") or 0)
                _uprice = float(line.get("unit_price") or 0)
                _billed = int(line.get("billed_qty") or 0)
                _lp_o   = line.get("lens_params") or {}
                if isinstance(_lp_o, str):
                    import json as _jlo; 
                    try: _lp_o = _jlo.loads(_lp_o)
                    except: _lp_o = {}
                _colour = str(_lp_o.get("colour_mix") or line.get("colour_mix") or "").strip()
                _group  = str(_lp_o.get("frame_group") or line.get("frame_group") or "").strip()
                _sku    = str(_lp_o.get("batch_no") or line.get("batch_no") or "").strip()
                _alloc_qty = int(line.get("allocated_qty") or 0)
                # Route resolution priority:
                #   1. bo_assignments session state (operator confirmed)
                #   2. manufacturing_route on line dict (loaded from DB / set by save)
                #   3. If batch_no/SKU set → STOCK (frame was picked from inventory)
                #   4. Otherwise → VENDOR (needs to be ordered)
                _lk_o   = f"{line.get('product_id','unk')}_{line.get('eye_side','B')}_{_oi}"
                _asgn_o = st.session_state.get("bo_assignments", {}).get(_lk_o, {})
                _route_raw = (
                    _asgn_o.get("route")
                    or line.get("manufacturing_route")
                    or ("STOCK" if _sku else "VENDOR")
                )
                _route  = str(_route_raw).upper()
                _sub_d, _gst_a, _total = _calc_line_display_total(line)
                _gst_pct = float(line.get("gst_percent_used") or line.get("gst_percent") or 0)
                _tax_inc = line.get("tax_inclusive", True)
                _frozen_o = _any_frozen or _billing_frozen
                _is_frm = ("frame" in _mg.lower() or "sunglass" in _mg.lower()
                           or (str(line.get("eye_side","")).upper() == "OTHER" and _sku))
                _prod_id_o = str(line.get("product_id") or "")

                with st.container(border=True):

                    # ══ ROW 1: Product info + Route/Stock status (2 cols) ══════
                    _r1a, _r1b = st.columns([3, 2])

                    with _r1a:
                        # Line 1: product name + brand + group
                        _base_name = _pname.split(" | ")[0]
                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid #1e293b;"
                            f"border-radius:8px;padding:10px 14px'>"
                            f"<div style='color:#a78bfa;font-weight:700;font-size:0.88rem'>"
                            f"{_base_name}</div>"
                            f"<div style='color:#475569;font-size:0.7rem;margin-top:3px'>"
                            f"{_brand + ' · ' if _brand else ''}{_mg} · {line.get('unit','PCS')}"
                            f"</div></div>",
                            unsafe_allow_html=True
                        )

                    with _r1b:
                        # Route status badge — reflects actual manufacturing_route
                        # STOCK: fully allocated from inventory → show green
                        # VENDOR: going to supplier → show amber, no allotment count
                        # PARTIAL: some from stock, rest from vendor → show orange
                        # Frame is "from stock" if:
                        #   a) route is STOCK AND fully allocated (normal lens path), OR
                        #   b) route is STOCK AND batch_no is set (frame picked from inventory —
                        #      allocated_qty starts at 0 until save writes it to DB)
                        _is_fully_allocated = (
                            _route == "STOCK"
                            and (_alloc_qty >= _qty or bool(_sku))
                        )
                        _is_vendor = _route in ("VENDOR", "PENDING")
                        if _is_fully_allocated:
                            _badge_bg    = "#0f2a1a"; _badge_bdr = "#22c55e"
                            _badge_color = "#86efac"
                            _badge_icon  = "📦"; _badge_label = "From Stock"
                            if _alloc_qty >= _qty:
                                _badge_sub = f"Allotted: {_alloc_qty}/{_qty} pcs" + (f" · SKU: <code>{_sku}</code>" if _sku else "")
                            else:
                                # batch_no set but not yet written to DB — will be committed on Save
                                _badge_sub = (f"SKU: <code>{_sku}</code> · " if _sku else "") + "Pending save"
                        elif _is_vendor:
                            _badge_bg    = "#1a1000"; _badge_bdr = "#f59e0b"
                            _badge_color = "#fcd34d"
                            _badge_icon  = "🏭"; _badge_label = "Via Supplier"
                            _supp_name   = str(line.get("supplier_name") or "")
                            _badge_sub   = _supp_name if _supp_name else "Assign supplier below"
                        else:
                            _badge_bg    = "#1a0f00"; _badge_bdr = "#f97316"
                            _badge_color = "#fb923c"
                            _badge_icon  = "📦"; _badge_label = _route.replace("_"," ").title()
                            _badge_sub   = f"Allotted: {_alloc_qty}/{_qty} pcs"
                        st.markdown(
                            f"<div style='background:{_badge_bg};border:1px solid {_badge_bdr};"
                            f"border-radius:8px;padding:10px 14px'>"
                            f"<div style='color:{_badge_color};font-weight:700;font-size:0.8rem'>"
                            f"{_badge_icon} {_badge_label}</div>"
                            f"<div style='color:{_badge_color};font-size:0.72rem;margin-top:3px'>"
                            f"{_badge_sub}</div></div>",
                            unsafe_allow_html=True
                        )

                    # ══ ROW 2: Attribute badges (1 line) ════════════════════
                    _attr_badges = []
                    if _sku:   _attr_badges.append(f"<span style='background:#1e1b4b;color:#a5b4fc;border:1px solid #4f46e5;padding:2px 9px;border-radius:20px;font-size:0.7rem'>SKU: {_sku}</span>")
                    if _group: _attr_badges.append(f"<span style='background:#1e293b;color:#94a3b8;padding:2px 9px;border-radius:20px;font-size:0.7rem'>🏷 {_group}</span>")
                    if _colour:_attr_badges.append(f"<span style='background:#4a0526;color:#f9a8d4;border:1px solid #be185d;padding:2px 9px;border-radius:20px;font-size:0.7rem'>🎨 {_colour}</span>")
                    _attr_badges.append(f"<span style='background:#0f172a;color:#64748b;padding:2px 9px;border-radius:20px;font-size:0.7rem'>Qty: {_qty} · ₹{_uprice:,.0f} · {_gst_pct:.0f}%{'i' if _tax_inc else '+'} · <b style=\'color:#e2e8f0\'>₹{_total:,.0f}</b></span>")
                    if _attr_badges:
                        st.markdown(
                            "<div style='display:flex;flex-wrap:wrap;gap:5px;padding:4px 0'>"
                            + "".join(_attr_badges) + "</div>",
                            unsafe_allow_html=True
                        )

                    # ══ ROW 3: Edit controls (collapsed expanders) ══════════
                    if _frozen_o:
                        st.caption("🔒 " + ("Order confirmed — locked" if _is_confirmed_frozen else "In billing pipeline"))
                    elif _billed > 0:
                        st.caption(f"🔒 Billed ({_billed} pcs) — use Credit Note to adjust")
                    else:
                        from modules.sql_adapter import run_query as _rq_frm

                        # ── Edit controls in 2 expanders ────────────────────
                        _ec1, _ec2 = st.columns(2)

                        with _ec1:
                            with st.expander("🔄 Change Product", expanded=False):
                                # Cascading: main_group → product → attributes
                                try:
                                    _groups = _rq_frm("""
                                        SELECT DISTINCT main_group FROM products
                                        WHERE COALESCE(is_active, true) = true
                                          AND main_group IS NOT NULL
                                        ORDER BY main_group
                                    """) or []
                                    _group_list = [r["main_group"] for r in _groups]
                                except Exception:
                                    _group_list = [_mg] if _mg else []

                                _sel_mg = st.selectbox(
                                    "Category",
                                    _group_list,
                                    index=(_group_list.index(_mg) if _mg in _group_list else 0),
                                    key=f"cp_mg_{_lid_o}_{_oid_o[:6]}"
                                )

                                # Products in that main_group with stock
                                try:
                                    _prod_rows = _rq_frm("""
                                        SELECT DISTINCT ON (p.id)
                                               p.id::text AS product_id,
                                               p.product_name, p.brand
                                        FROM products p
                                        JOIN inventory_stock ist ON ist.product_id = p.id
                                        WHERE p.main_group = %(mg)s
                                          AND COALESCE(ist.quantity, 0) > 0
                                          AND COALESCE(ist.is_active, true) = true
                                          AND COALESCE(p.is_active, true) = true
                                        ORDER BY p.id, p.product_name
                                    """, {"mg": _sel_mg}) or []
                                except Exception:
                                    _prod_rows = []

                                if not _prod_rows:
                                    st.info(f"No stock in {_sel_mg}")
                                else:
                                    _p_ids  = [r["product_id"] for r in _prod_rows]
                                    _p_lbls = {r["product_id"]: r["product_name"] for r in _prod_rows}
                                    _def_pid = _prod_id_o if _prod_id_o in _p_ids else _p_ids[0]
                                    _new_pid = st.selectbox(
                                        "Product",
                                        _p_ids,
                                        index=_p_ids.index(_def_pid),
                                        format_func=lambda x: _p_lbls.get(x, x),
                                        key=f"cp_pid_{_lid_o}_{_oid_o[:6]}"
                                    )

                                    # SKUs for selected product
                                    try:
                                        _sku_rows2 = _rq_frm("""
                                            SELECT batch_no,
                                                   COALESCE(frame_group,'')  AS frame_group,
                                                   COALESCE(colour_mix,'')   AS colour_mix,
                                                   COALESCE(mrp, selling_price, 0)::numeric AS mrp,
                                                   quantity::int AS quantity
                                            FROM inventory_stock
                                            WHERE product_id = %(pid)s::uuid
                                              AND COALESCE(quantity,0) > 0
                                              AND COALESCE(is_active,true) = true
                                            ORDER BY frame_group, colour_mix, batch_no
                                        """, {"pid": _new_pid}) or []
                                    except Exception:
                                        _sku_rows2 = []

                                    if _sku_rows2:
                                        _sk2_ids  = [r["batch_no"] for r in _sku_rows2]
                                        _sk2_map  = {r["batch_no"]: r for r in _sku_rows2}
                                        _sk2_lbls = {
                                            r["batch_no"]: " | ".join(filter(None,[
                                                r["batch_no"], r["frame_group"],
                                                r["colour_mix"], f"Qty:{r['quantity']}"
                                            ])) for r in _sku_rows2
                                        }
                                        _def_sk2 = _sku if _sku in _sk2_ids else _sk2_ids[0]
                                        _sel_sk2 = st.selectbox(
                                            "SKU / Colour",
                                            _sk2_ids,
                                            index=_sk2_ids.index(_def_sk2),
                                            format_func=lambda x: _sk2_lbls.get(x,x),
                                            key=f"cp_sku_{_lid_o}_{_oid_o[:6]}"
                                        )
                                        _sel_sk2_row = _sk2_map.get(_sel_sk2, {})
                                        _cp_price = st.number_input(
                                            "Price ₹", min_value=0.0,
                                            value=float(_sel_sk2_row.get("mrp") or _uprice) or _uprice,
                                            step=50.0, format="%.2f",
                                            key=f"cp_price_{_lid_o}_{_oid_o[:6]}"
                                        )
                                        if st.button(
                                            "✅ Apply",
                                            key=f"cp_apply_{_lid_o}_{_oid_o[:6]}",
                                            type="primary", disabled=(_cp_price==0)
                                        ):
                                            _nn2 = " | ".join(filter(None,[
                                                _p_lbls[_new_pid], _sel_sk2,
                                                _sel_sk2_row.get("frame_group",""),
                                                _sel_sk2_row.get("colour_mix","")
                                            ]))
                                            line.update({
                                                "product_id":   _new_pid,
                                                "product_name": _nn2,
                                                "batch_no":     _sel_sk2,
                                                "colour_mix":   _sel_sk2_row.get("colour_mix",""),
                                                "frame_group":  _sel_sk2_row.get("frame_group",""),
                                                "unit_price":   _cp_price,
                                                "total_price":  _cp_price * _qty,
                                                "billing_total":_cp_price * _qty,
                                            })
                                            _lp2 = dict(_lp_o)
                                            _lp2.update({
                                                "batch_no":_sel_sk2,
                                                "colour_mix":_sel_sk2_row.get("colour_mix",""),
                                                "frame_group":_sel_sk2_row.get("frame_group",""),
                                                "display_product_name":_nn2,
                                            })
                                            line["lens_params"]      = _lp2
                                            line["batch_allocation"] = [{"batch_no":_sel_sk2,
                                                "allocated_qty":_qty,"selling_price":_cp_price,"qty":_qty}]
                                            try:
                                                from modules.backoffice.audit_logger import audit, AuditAction
                                                from modules.security.roles import current_user as _cuu
                                                audit(AuditAction.PRODUCT_CHANGED,
                                                      entity="order_lines",entity_id=_lid_o,
                                                      order_id=_oid_o,
                                                      user_id=(_cuu() or {}).get("name","backoffice"),
                                                      payload={"action":"product_replaced",
                                                               "old_product":_pname,"new_product":_nn2,
                                                               "old_price":_uprice,"new_price":_cp_price})
                                            except Exception: pass
                                            st.success(f"✅ {_nn2}")
                                            st.rerun()

                        with _ec2:
                            with st.expander("✏️ Price / Qty", expanded=False):
                                _pe1, _pe2 = st.columns(2)
                                _ep = _pe1.number_input("Price ₹",min_value=0.0,
                                    value=_uprice,step=10.0,format="%.2f",
                                    key=f"ep_{_lid_o}_{_oid_o[:6]}")
                                _eq = _pe2.number_input("Qty",min_value=1,
                                    value=max(_qty,1),step=1,
                                    key=f"eq_{_lid_o}_{_oid_o[:6]}")
                                if st.button("✅ Update",key=f"eupd_{_lid_o}_{_oid_o[:6]}",
                                             type="primary"):
                                    line.update({"unit_price":_ep,"billing_qty":_eq,
                                                 "total_price":_ep*_eq,"billing_total":_ep*_eq})
                                    st.success(f"₹{_ep:,.0f} × {_eq}")
                                    st.rerun()

        # ── Add Product panel (any non-R/L line can be added here) ─────────
        if not _billing_frozen:
            with st.expander("➕ Add Product to Order", expanded=False):
                st.caption("Add a frame, accessory, solution, or any other product to this order.")
                from modules.sql_adapter import run_query as _rq_add
                try:
                    _add_groups = _rq_add("""
                        SELECT DISTINCT p.main_group FROM products p
                        JOIN inventory_stock ist ON ist.product_id = p.id
                        WHERE COALESCE(p.is_active,true)=true
                          AND COALESCE(ist.quantity,0) > 0
                          AND COALESCE(ist.is_active,true)=true
                          AND p.main_group NOT IN ('Ophthalmic Lens','Contact Lens','RGP Lens')
                        ORDER BY p.main_group
                    """) or []
                    _add_group_list = [r["main_group"] for r in _add_groups]
                except Exception:
                    _add_group_list = []

                if _add_group_list:
                    _add_mg = st.selectbox("Category",_add_group_list,
                                           key=f"add_mg_{str(order.get('id',''))[:8]}")
                    try:
                        _add_prods = _rq_add("""
                            SELECT DISTINCT ON (p.id)
                                   p.id::text AS product_id, p.product_name, p.brand
                            FROM products p
                            JOIN inventory_stock ist ON ist.product_id = p.id
                            WHERE p.main_group=%(mg)s
                              AND COALESCE(ist.quantity,0)>0
                              AND COALESCE(ist.is_active,true)=true
                              AND COALESCE(p.is_active,true)=true
                            ORDER BY p.id, p.product_name
                        """, {"mg":_add_mg}) or []
                    except Exception:
                        _add_prods = []

                    if _add_prods:
                        _ap_ids  = [r["product_id"] for r in _add_prods]
                        _ap_lbls = {r["product_id"]: r["product_name"] for r in _add_prods}
                        _ap_sel  = st.selectbox("Product",_ap_ids,
                            format_func=lambda x: _ap_lbls.get(x,x),
                            key=f"add_pid_{str(order.get('id',''))[:8]}")
                        try:
                            _add_skus = _rq_add("""
                                SELECT batch_no,
                                       COALESCE(frame_group,'')  AS frame_group,
                                       COALESCE(colour_mix,'')   AS colour_mix,
                                       COALESCE(mrp,selling_price,0)::numeric AS mrp,
                                       quantity::int AS quantity
                                FROM inventory_stock
                                WHERE product_id=%(pid)s::uuid
                                  AND COALESCE(quantity,0)>0
                                  AND COALESCE(is_active,true)=true
                                ORDER BY frame_group,colour_mix
                            """, {"pid":_ap_sel}) or []
                        except Exception:
                            _add_skus = []

                        if _add_skus:
                            _as_ids = [r["batch_no"] for r in _add_skus]
                            _as_map = {r["batch_no"]: r for r in _add_skus}
                            _as_lbls= {r["batch_no"]: " | ".join(filter(None,[
                                r["batch_no"],r["frame_group"],r["colour_mix"],
                                f"Qty:{r['quantity']}"
                            ])) for r in _add_skus}
                            _as_sel = st.selectbox("SKU / Colour / Size",_as_ids,
                                format_func=lambda x: _as_lbls.get(x,x),
                                key=f"add_sku_{str(order.get('id',''))[:8]}")
                            _as_row = _as_map.get(_as_sel,{})
                            _ac1,_ac2,_ac3 = st.columns(3)
                            _add_qty   = _ac1.number_input("Qty",min_value=1,value=1,step=1,
                                key=f"add_qty_{str(order.get('id',''))[:8]}")
                            _add_price = _ac2.number_input("Price ₹",min_value=0.0,
                                value=float(_as_row.get("mrp") or 0),step=50.0,format="%.2f",
                                key=f"add_price_{str(order.get('id',''))[:8]}")
                            _ac3.metric("Total",f"₹{_add_price*_add_qty:,.0f}")

                            if st.button("➕ Add to Order",
                                key=f"add_btn_{str(order.get('id',''))[:8]}",
                                type="primary",
                                disabled=(_add_price==0)
                            ):
                                import uuid as _uuid_add
                                _new_line = {
                                    "line_id":           str(_uuid_add.uuid4()),
                                    "product_id":        _ap_sel,
                                    "product_name":      " | ".join(filter(None,[
                                        _ap_lbls[_ap_sel],_as_sel,
                                        _as_row.get("frame_group",""),
                                        _as_row.get("colour_mix","")
                                    ])),
                                    "brand":             next((r["brand"] for r in _add_prods if r["product_id"]==_ap_sel),""),
                                    "main_group":        _add_mg,
                                    "eye_side":          "OTHER",
                                    "batch_no":          _as_sel,
                                    "colour_mix":        _as_row.get("colour_mix",""),
                                    "frame_group":       _as_row.get("frame_group",""),
                                    "billing_qty":       _add_qty,
                                    "unit_price":        _add_price,
                                    "total_price":       _add_price * _add_qty,
                                    "billing_total":     _add_price * _add_qty,
                                    "gst_percent":       5.0,
                                    "tax_inclusive":     True,
                                    "manufacturing_route":"STOCK",
                                    "batch_status":      "ALLOCATED",
                                    "allocated_qty":     _add_qty,
                                    "batch_allocation":  [{"batch_no":_as_sel,
                                        "allocated_qty":_add_qty,"selling_price":_add_price,"qty":_add_qty}],
                                    "lens_params": {
                                        "batch_no":_as_sel,
                                        "colour_mix":_as_row.get("colour_mix",""),
                                        "frame_group":_as_row.get("frame_group",""),
                                        "display_product_name":" | ".join(filter(None,[
                                            _ap_lbls[_ap_sel],_as_sel,
                                            _as_row.get("frame_group",""),
                                            _as_row.get("colour_mix","")
                                        ]))
                                    },
                                    "is_new_line": True,
                                }
                                all_lines.append(_new_line)
                                other_lines.append(_new_line)
                                st.success(f"✅ Added: {_new_line['product_name']}")
                                st.rerun()

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

        # ── GST Verification Footer — always computed live from _calc_line_display_total
        # Same function used by line rows → guaranteed to match what's displayed ──
        # order_type already resolved at function top — reuse it here
        _taxable_live   = 0.0   # ex-GST base
        _gst_total_live = 0.0
        _mrp_total_live = 0.0   # what customer actually pays (total col)
        for _fl in all_lines:
            _base, _g, _tot = _calc_line_display_total(_fl)
            _taxable_live   += _base
            _gst_total_live += _g
            _mrp_total_live += _tot
        _taxable_live   = round(_taxable_live, 2)
        _gst_total_live = round(_gst_total_live, 2)
        _mrp_total_live = round(_mrp_total_live, 2)

        # ── Load service charges for summary ─────────────────────────────────
        _oid_bvs = str(order.get("id") or "")
        _bvs_key = f"svc_charges_{_oid_bvs}"
        if _bvs_key not in st.session_state:
            try:
                from modules.backoffice.order_charges_panel import fetch_charges
                st.session_state[_bvs_key] = fetch_charges(_oid_bvs)
            except Exception:
                st.session_state[_bvs_key] = []
        _bvs_charges = st.session_state.get(_bvs_key, [])
        _svc_base    = sum(float(x.get("amount") or 0)        for x in _bvs_charges)
        _svc_gst     = sum(float(x.get("gst_amount") or 0)    for x in _bvs_charges)
        _svc_total   = sum(float(x.get("total_amount") or 0)  for x in _bvs_charges)
        _svc_base    = round(_svc_base, 2)
        _svc_gst     = round(_svc_gst, 2)
        _svc_total   = round(_svc_total, 2)

        _invoice_total = round(_mrp_total_live + _svc_total, 2)

        with st.container(border=True):
            st.caption("📊 Billing Verification Summary")
            if order_type == "RETAIL":
                vc = st.columns(4)
                vc[0].metric("MRP Total (incl. GST)",  f"₹{_mrp_total_live:,.2f}")
                vc[1].metric("GST Extracted",           f"₹{_gst_total_live:,.2f}", help="GST back-calculated from MRP")
                vc[2].metric("Taxable Value",           f"₹{_taxable_live:,.2f}")
                vc[3].metric("Patient Pays",            f"₹{_mrp_total_live:,.2f}")
            else:
                vc = st.columns(4)
                vc[0].metric("Subtotal (excl. GST)",   f"₹{_taxable_live:,.2f}")
                vc[1].metric("GST Added",               f"₹{_gst_total_live:,.2f}", help="GST added on top of selling price")
                vc[2].metric("Grand Total",             f"₹{_mrp_total_live:,.2f}")
                vc[3].metric("Order Type",              order_type)

            # ── Per-product line breakdown ─────────────────────────────
            if all_lines:
                st.markdown(
                    "<div style='height:6px'></div>"
                    "<div style='color:#64748b;font-size:0.7rem;font-weight:600;"
                    "letter-spacing:.05em;padding:4px 0 2px'>PRODUCT LINES</div>",
                    unsafe_allow_html=True,
                )
                _bvs_hcols = st.columns([0.5, 3, 1.5, 1, 1, 1.5, 1.5])
                for _hh, _hl in zip(_bvs_hcols, ["Eye","Product","Power","Qty","GST%","Unit ₹","Total ₹"]):
                    _hh.markdown(f"<span style='color:#475569;font-size:0.65rem'>{_hl}</span>",
                                 unsafe_allow_html=True)
                st.markdown("<hr style='margin:2px 0;border-color:#1e293b'>", unsafe_allow_html=True)
                for _bvl in all_lines:
                    _bvl_eye  = str(_bvl.get("eye_side","")).upper()
                    _bvl_icon = {"R":"👁️R","L":"👁️L","SERVICE":"🩺","S":"🩺","B":"👁️👁️"}.get(_bvl_eye,"🔹")
                    _bvl_name = str(_bvl.get("product_name",""))
                    _bvl_pwr  = ""

                    # Helper: reject NaN/Inf/None → clean float or None
                    def _bvl_real(v):
                        if v is None: return None
                        try:
                            import math as _bm
                            f = float(v)
                            return None if (_bm.isnan(f) or _bm.isinf(f)) else f
                        except (TypeError, ValueError): return None

                    _bvl_lp  = _bvl.get("lens_params") or {}
                    _bvl_mg  = str(_bvl.get("main_group") or "").lower()
                    _is_frame_line = (
                        _bvl_eye == "OTHER"
                        or "frame" in _bvl_mg
                        or bool(_bvl_lp.get("batch_no"))  # saved by order_persistence
                    )

                    if _is_frame_line:
                        # ── Frame: show SKU | Frame Group | Colour ──────────
                        _bvl_sku  = str(_bvl_lp.get("batch_no") or _bvl.get("batch_no") or "").strip()
                        _bvl_fgrp = str(_bvl_lp.get("frame_group") or _bvl.get("frame_group") or "").strip()
                        _bvl_fcol = str(_bvl_lp.get("colour_mix")  or _bvl.get("colour_mix")  or "").strip()
                        _bvl_desc_parts = [p for p in [_bvl_sku, _bvl_fgrp, _bvl_fcol] if p]
                        _bvl_pwr = " | ".join(_bvl_desc_parts) if _bvl_desc_parts else ""

                    elif _bvl_eye not in ("SERVICE", "S", "B"):
                        # ── Lens: show SPH / CYL / AXIS ────────────────────
                        _bvl_sph_f = _bvl_real(_bvl.get("sph"))
                        if _bvl_sph_f is not None:
                            _sign = lambda v: (f"+{v:.2f}" if v > 0 else f"{v:.2f}") if v else "0.00"
                            _bvl_cyl_f = _bvl_real(_bvl.get("cyl"))
                            _bvl_ax_f  = _bvl_real(_bvl.get("axis"))
                            _bvl_pwr   = f"S{_sign(_bvl_sph_f)}"
                            if _bvl_cyl_f: _bvl_pwr += f" C{_sign(_bvl_cyl_f)}"
                            if _bvl_ax_f:  _bvl_pwr += f" A{int(_bvl_ax_f)}"
                    _bvl_qty  = int(_bvl.get("billing_qty") or _bvl.get("quantity") or 0)
                    _bvl_gst  = float(_bvl.get("gst_percent_used") or _bvl.get("gst_percent") or 0)
                    _bvl_up   = float(_bvl.get("unit_price") or 0)
                    _bvl_tot  = float(_bvl.get("billing_total") or _bvl.get("total_price") or 0)
                    _bvl_row  = st.columns([0.5, 3, 1.5, 1, 1, 1.5, 1.5])
                    _bvl_row[0].markdown(f"<span style='font-size:0.78rem'>{_bvl_icon}</span>",
                                         unsafe_allow_html=True)
                    _bvl_row[1].markdown(f"<span style='color:#e2e8f0;font-size:0.75rem'>{_bvl_name}</span>",
                                         unsafe_allow_html=True)
                    _bvl_row[2].markdown(f"<span style='color:#94a3b8;font-size:0.7rem'>{_bvl_pwr or '—'}</span>",
                                         unsafe_allow_html=True)
                    _bvl_row[3].markdown(f"<span style='color:#e2e8f0;font-size:0.75rem'>{_bvl_qty}</span>",
                                         unsafe_allow_html=True)
                    _bvl_row[4].markdown(f"<span style='color:#94a3b8;font-size:0.75rem'>{_bvl_gst:.0f}%</span>",
                                         unsafe_allow_html=True)
                    _bvl_row[5].markdown(f"<span style='color:#94a3b8;font-size:0.75rem'>₹{_bvl_up:,.2f}</span>",
                                         unsafe_allow_html=True)
                    _bvl_row[6].markdown(f"<span style='color:#10b981;font-size:0.78rem;font-weight:700'>₹{_bvl_tot:,.2f}</span>",
                                         unsafe_allow_html=True)
                st.markdown("<hr style='margin:4px 0;border-color:#1e293b'>", unsafe_allow_html=True)

            # ── Service charges breakdown row ─────────────────────────
            if _bvs_charges:
                st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
                _sc_cols = st.columns(len(_bvs_charges) + 1)
                for _sci, _sch in enumerate(_bvs_charges):
                    _ico = {"FITTING":"🔧","COLOURING":"🎨","COURIER":"📦"}.get(
                           _sch.get("charge_type",""), "➕")
                    _sc_cols[_sci].metric(
                        f"{_ico} {_sch.get('description') or _sch.get('charge_type','')}",
                        f"₹{float(_sch.get('total_amount') or 0):,.2f}",
                        help=f"Base ₹{float(_sch.get('amount') or 0):,.2f} + "
                             f"{float(_sch.get('gst_percent') or 0):.0f}% GST")
                _sc_cols[-1].metric("Services Total", f"₹{_svc_total:,.2f}")

                st.markdown(
                    f"<div style='background:#0d1f0d;border:1px solid #10b98155;"
                    f"border-radius:8px;padding:10px 16px;margin-top:6px;"
                    f"display:flex;justify-content:space-between;align-items:center'>"
                    f"<div>"
                    f"<span style='color:#64748b;font-size:0.72rem'>Lens Total</span>"
                    f"<span style='color:#94a3b8;font-weight:700;margin-left:8px'>"
                    f"₹{_mrp_total_live:,.2f}</span>"
                    f"<span style='color:#475569;margin:0 10px'>+</span>"
                    f"<span style='color:#64748b;font-size:0.72rem'>Service Charges</span>"
                    f"<span style='color:#a78bfa;font-weight:700;margin-left:8px'>"
                    f"₹{_svc_total:,.2f}</span>"
                    f"</div>"
                    f"<div>"
                    f"<span style='color:#64748b;font-size:0.78rem;margin-right:10px'>"
                    f"Invoice Total</span>"
                    f"<span style='color:#10b981;font-weight:800;font-size:1.2rem'>"
                    f"₹{_invoice_total:,.2f}</span>"
                    f"</div></div>",
                    unsafe_allow_html=True)

            st.caption(
                f"Source: {order.get('order_source', order_type)}  |  "
                f"Tax treatment: {'GST inclusive in price' if order_type == 'RETAIL' else 'GST exclusive — added on top'}"
            )
        # ─────────────────────────────────────────────────────────────────────

        # ─────────────────────────────────────────────────────────────────────
        # "Send to Billing Dashboard" removed — Save & Confirm handles this in one step.
        # After save, stock orders auto-advance to CONFIRMED; vendor orders go to UNDER_REVIEW.

        # ── Cancel Order panel (only for PENDING / UNDER_REVIEW) ────────────
        try:
            from modules.backoffice.backoffice import render_cancel_order_panel
            render_cancel_order_panel(order)
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────

        from modules.utils.submit_guard import is_locked, guarded_submit
        _save_frozen = _billing_frozen  # frozen if in billing pipeline
        if _save_frozen:
            st.markdown(
                "<div style='background:#1a0a0a;border:1px solid #f59e0b44;"
                "border-radius:8px;padding:8px 14px;text-align:center;"
                "color:#78716c;font-size:0.8rem'>"
                "🔒 SAVE DISABLED — Order is in the billing pipeline</div>",
                unsafe_allow_html=True,
            )
        # ── Payment collection window (before final save) ───────────────
        # Shows compact advance recording: Cash / UPI / Card / Bank
        # Staff can record payment here before clicking Save & Confirm
        _order_id_for_pay = str(order.get("id") or order.get("order_id") or "")
        _pay_status_key   = f"_bo_advance_recorded_{_order_id_for_pay[:8]}"
        _adv_recorded     = st.session_state.get(_pay_status_key, False)

        if not _billing_frozen and _order_id_for_pay:
            with st.expander(
                "💰 Collect Payment (before saving)",
                expanded=st.session_state.get(f"_bo_pay_open_{_order_id_for_pay[:8]}", False)
            ):
                # ── Payment details helper ───────────────────────────────────
                _pay_party   = str(order.get("party_name") or order.get("patient_name") or "Customer")
                # Auto-load mobile from order (patient_mobile → party_mobile → DB lookup)
                _pay_mobile = str(
                    order.get("patient_mobile") or
                    order.get("mobile") or
                    order.get("party_mobile") or ""
                )
                # If still empty, try DB lookup via party_id
                if not _pay_mobile and order.get("party_id"):
                    try:
                        from modules.sql_adapter import run_query as _rq_mob
                        _mob_row = _rq_mob(
                            "SELECT COALESCE(mobile,'') AS mob FROM parties WHERE id=%s::uuid LIMIT 1",
                            (str(order["party_id"]),)
                        ) or []
                        if _mob_row: _pay_mobile = str(_mob_row[0].get("mob") or "")
                    except Exception: pass
                # Sanitise to 10-digit Indian mobile
                _pay_mob_clean = "".join(x for x in _pay_mobile if x.isdigit())
                if _pay_mob_clean.startswith("91") and len(_pay_mob_clean) == 12:
                    _pay_mob_clean = _pay_mob_clean[2:]
                elif _pay_mob_clean.startswith("0") and len(_pay_mob_clean) == 11:
                    _pay_mob_clean = _pay_mob_clean[1:]
                _pay_wa_mob = ("91"+_pay_mob_clean) if (len(_pay_mob_clean)==10 and _pay_mob_clean[0] in "6789") else ""

                # Pre-seed session state for all mobile inputs so value= actually shows
                # Streamlit ignores value= if key already exists in session_state
                for _mob_key in [
                    f"pt2_wa_mob_{_order_id_for_pay[:8]}",
                    f"pt3_mob_{_order_id_for_pay[:8]}",
                ]:
                    if _mob_key not in st.session_state or not st.session_state[_mob_key]:
                        st.session_state[_mob_key] = _pay_mob_clean

                # Balance due
                _pay_total   = float(total_billing)
                _pay_due     = _pay_total  # before any advances

                # ── Helper to save payment to DB ─────────────────────────────
                def _save_advance(amount, mode, ref=""):
                    from modules.sql_adapter import run_write as _rw_p
                    import uuid as _up
                    _rw_p("""
                        INSERT INTO payments
                            (id, payment_no, payment_date, payment_mode,
                             amount, reference_no, remarks,
                             order_id, advance_for_order_id,
                             payment_type, created_at)
                        VALUES
                            (%s::uuid, %s, CURRENT_DATE, %s,
                             %s, %s, %s,
                             %s::uuid, %s::uuid,
                             'ADVANCE', NOW())
                        ON CONFLICT DO NOTHING
                    """, (
                        str(_up.uuid4()),
                        f"ADV-{_order_id_for_pay[:8].upper()}",
                        mode, float(amount), ref or None,
                        f"Advance — {_pay_party}",
                        _order_id_for_pay, _order_id_for_pay,
                    ))

                # ── 4 tabs ────────────────────────────────────────────────────
                _ptab1, _ptab2, _ptab3, _ptab4 = st.tabs([
                    "💵 Cash / Card",
                    "📱 UPI + QR Code",
                    "🔗 Payment Link",
                    "🏦 Bank Transfer",
                ])

                # ══ TAB 1: Cash / Card ═══════════════════════════════════════
                with _ptab1:
                    _t1c1, _t1c2 = st.columns(2)
                    _t1_amt  = _t1c1.number_input(
                        "Amount ₹", min_value=0.0,
                        value=float(_pay_due), step=50.0, format="%.2f",
                        key=f"pt1_amt_{_order_id_for_pay[:8]}"
                    )
                    _t1_mode = _t1c2.selectbox(
                        "Mode", ["Cash", "Card (POS)", "Cheque"],
                        key=f"pt1_mode_{_order_id_for_pay[:8]}"
                    )
                    _t1_ref = st.text_input(
                        "Reference / Cheque No. (optional)",
                        key=f"pt1_ref_{_order_id_for_pay[:8]}"
                    )
                    if st.button("✅ Record Cash/Card Payment",
                                 key=f"pt1_save_{_order_id_for_pay[:8]}",
                                 type="primary", use_container_width=True,
                                 disabled=_t1_amt <= 0):
                        try:
                            _save_advance(_t1_amt, _t1_mode, _t1_ref)
                            st.session_state[_pay_status_key] = True
                            st.success(f"✅ ₹{_t1_amt:,.0f} via {_t1_mode} recorded")
                            st.rerun()
                        except Exception as _e1: st.error(f"Failed: {_e1}")

                # ══ TAB 2: UPI + QR ══════════════════════════════════════════
                with _ptab2:
                    try:
                        from modules.billing.payment_link_manager import get_upi_id as _get_upi, get_upi_name as _get_uname
                        _upi_id   = _get_upi()
                        _upi_name = _get_uname()
                    except Exception:
                        _upi_id = _upi_name = ""

                    _t2_amt = st.number_input(
                        "Amount ₹", min_value=0.0,
                        value=float(_pay_due), step=50.0, format="%.2f",
                        key=f"pt2_amt_{_order_id_for_pay[:8]}"
                    )

                    if _upi_id:
                        import urllib.parse as _uparse_pay
                        _upi_link = (
                            f"upi://pay?pa={_uparse_pay.quote(_upi_id)}"
                            f"&pn={_uparse_pay.quote(_upi_name or 'DV Optical')}"
                            f"&am={_t2_amt:.2f}"
                            f"&tn={_uparse_pay.quote('Order '+str(order.get('order_no','')))}"
                            f"&cu=INR"
                        )
                        _qr_url = (
                            f"https://upiqr.in/api/qr?vpa={_uparse_pay.quote(_upi_id)}"
                            f"&name={_uparse_pay.quote(_upi_name or 'DV Optical')}"
                            f"&amount={_t2_amt:.2f}"
                            f"&trxnote={_uparse_pay.quote('Order '+str(order.get('order_no','')))}"
                        )
                        # QR + UPI ID display
                        _qrc1, _qrc2 = st.columns([1, 1])
                        with _qrc1:
                            _qr_html = (
                                "<div style='text-align:center'>"
                                + f"<img src='{_qr_url}' width='160' "
                                + "style='border-radius:8px;background:white;padding:6px'>"
                                + f"<div style='font-size:0.72rem;color:#64748b;margin-top:4px'>"
                                + f"Scan to Pay ₹{_t2_amt:,.0f}</div></div>"
                            )
                            st.markdown(_qr_html, unsafe_allow_html=True)
                        with _qrc2:
                            st.markdown(
                                f"<div style='background:#0f172a;border:1px solid #1e293b;"
                                f"border-radius:8px;padding:12px;text-align:center'>"
                                f"<div style='color:#64748b;font-size:0.65rem;margin-bottom:6px'>UPI ID</div>"
                                f"<div style='font-family:monospace;font-size:0.9rem;"
                                f"color:#f1f5f9;font-weight:700'>{_upi_id}</div>"
                                f"<div style='color:#64748b;font-size:0.65rem;margin-top:6px'>{_upi_name}</div>"
                                f"</div>",
                                unsafe_allow_html=True
                            )
                            st.link_button("📲 Open UPI App", _upi_link, use_container_width=True)

                        # ── WhatsApp — always shown, with mobile input if missing ──
                        st.markdown("---")
                        _wa2_mob_input = st.text_input(
                            "📱 Customer WhatsApp",
                            value=_pay_mob_clean,
                            placeholder="10-digit mobile",
                            key=f"pt2_wa_mob_{_order_id_for_pay[:8]}"
                        )
                        _wa2_mob_clean = "".join(x for x in _wa2_mob_input if x.isdigit())
                        _wa2_wa_mob = ("91"+_wa2_mob_clean) if (len(_wa2_mob_clean)==10 and _wa2_mob_clean[0] in "6789") else ""
                        if _wa2_wa_mob and _upi_id:
                            _wa_upi_msg = (
                                f"Hi {_pay_party},\n\n"
                                f"Please pay *₹{_t2_amt:,.0f}* for your order "
                                f"*{order.get('order_no','')}* via UPI:\n\n"
                                f"📲 UPI ID: *{_upi_id}*\n"
                                f"Name: {_upi_name}\n\n"
                                f"After payment, please share the transaction ID. Thank you! 🙏"
                            )
                            from modules.wa_hub import wa_link as _wa_link_hub
                            _wa_upi_url = _wa_link_hub(_wa2_wa_mob, _wa_upi_msg)
                            if _wa_upi_url:
                                st.link_button(
                                    "📲 Send UPI ID + Amount via WhatsApp",
                                    _wa_upi_url,
                                    use_container_width=True
                                )
                        elif not _wa2_wa_mob:
                            st.caption("Enter valid mobile above to enable WhatsApp send")
                    else:
                        st.info("⚙️ No UPI ID configured. Set it in Billing → Payment Link settings.")

                    st.markdown("---")
                    _t2_ref = st.text_input("UPI Transaction ID (after payment)",
                                            key=f"pt2_ref_{_order_id_for_pay[:8]}")
                    if st.button("✅ Record UPI Payment",
                                 key=f"pt2_save_{_order_id_for_pay[:8]}",
                                 type="primary", use_container_width=True,
                                 disabled=(_t2_amt <= 0 or not _t2_ref)):
                        try:
                            _save_advance(_t2_amt, "UPI", _t2_ref)
                            st.session_state[_pay_status_key] = True
                            st.success(f"✅ ₹{_t2_amt:,.0f} via UPI ({_t2_ref}) recorded")
                            st.rerun()
                        except Exception as _e2: st.error(f"Failed: {_e2}")

                # ══ TAB 3: Payment Link ═══════════════════════════════════════
                with _ptab3:
                    st.caption("Generate a payment link and send via WhatsApp. Customer pays online and confirms.")
                    _t3_mob = st.text_input(
                        "Customer Mobile",
                        value=_pay_mob_clean,
                        key=f"pt3_mob_{_order_id_for_pay[:8]}"
                    )
                    _t3_amt = st.number_input(
                        "Amount ₹", min_value=0.0,
                        value=float(_pay_due), step=50.0, format="%.2f",
                        key=f"pt3_amt_{_order_id_for_pay[:8]}"
                    )
                    _t3_hrs = st.slider("Link valid for (hours)", 1, 168, 48,
                                        key=f"pt3_hrs_{_order_id_for_pay[:8]}")

                    # Show WA preview note
                    _t3_mob_clean = "".join(x for x in _t3_mob if x.isdigit())
                    _t3_wa_ok = len(_t3_mob_clean)==10 and _t3_mob_clean[0] in "6789"
                    if _t3_wa_ok:
                        st.markdown(
                            f"<div style='background:#0d2010;border:1px solid #22c55e33;"
                            f"border-radius:6px;padding:6px 12px;font-size:0.75rem;color:#86efac'>"
                            f"📲 WhatsApp link will be sent to +91 {_t3_mob_clean} after generation"
                            f"</div>",
                            unsafe_allow_html=True
                        )
                    else:
                        st.caption("Enter a valid 10-digit mobile to enable WhatsApp send")

                    if st.button("🔗 Generate Link + Send via WhatsApp",
                                 key=f"pt3_gen_{_order_id_for_pay[:8]}",
                                 type="primary", use_container_width=True,
                                 disabled=(_t3_amt <= 0 or not _t3_wa_ok)):
                        try:
                            from modules.billing.payment_link_manager import (
                                create_payment_link as _cpl, mark_link_sent as _mls
                            )
                            from modules.security.roles import current_user_name as _cun
                            try: _by_pl = _cun() or "staff"
                            except: _by_pl = "staff"
                            if not isinstance(_by_pl, str):
                                _by_pl = getattr(_by_pl, "name", "staff")

                            _pl_result = _cpl(
                                order_id     = _order_id_for_pay,
                                order_no     = str(order.get("order_no","")),
                                party_name   = _pay_party,
                                mobile       = _t3_mob.strip(),
                                amount       = float(_t3_amt),
                                description  = f"Order {order.get('order_no','')} — Balance Payment",
                                expiry_hours = _t3_hrs,
                                created_by   = _by_pl,
                            )
                            _mls(_pl_result["token"])
                            st.success(f"✅ Link generated — Token: `{_pl_result['token']}`")
                            st.code(_pl_result["url"])
                            st.markdown(
                                "<div style='background:#0d2010;border:2px solid #22c55e;"
                                "border-radius:8px;padding:10px;text-align:center;"
                                "margin-top:6px'>"
                                "<span style='color:#86efac;font-weight:700'>✅ Link ready — tap to send</span>"
                                "</div>",
                                unsafe_allow_html=True
                            )
                            st.link_button(
                                "📲 Send Payment Link via WhatsApp",
                                _pl_result["whatsapp_url"],
                                use_container_width=True
                            )
                        except Exception as _e3:
                            st.error(f"Link generation failed: {_e3}")

                # ══ TAB 4: Bank Transfer ════════════════════════════════════
                with _ptab4:
                    st.caption("Share bank details for NEFT/RTGS/IMPS transfers.")
                    try:
                        from modules.settings.shop_master import get_unit_info as _gui_bank
                        _shop_bank = _gui_bank("retail") or {}
                        _bank_name = _shop_bank.get("bank_name","")
                        _bank_acc  = _shop_bank.get("bank_account","")
                        _bank_ifsc = _shop_bank.get("bank_ifsc","")
                        _bank_branch = _shop_bank.get("bank_branch","")
                    except Exception:
                        _bank_name = _bank_acc = _bank_ifsc = _bank_branch = ""

                    _t4_amt = st.number_input(
                        "Amount ₹", min_value=0.0,
                        value=float(_pay_due), step=50.0, format="%.2f",
                        key=f"pt4_amt_{_order_id_for_pay[:8]}"
                    )

                    if _bank_acc:
                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid #1e293b;"
                            f"border-radius:8px;padding:14px 16px'>"
                            + (f"<div style='color:#94a3b8;font-size:0.8rem'><b>Bank:</b> {_bank_name}</div>" if _bank_name else "")
                            + (f"<div style='color:#94a3b8;font-size:0.8rem'><b>Account No:</b> {_bank_acc}</div>" if _bank_acc else "")
                            + (f"<div style='color:#94a3b8;font-size:0.8rem'><b>IFSC:</b> {_bank_ifsc}</div>" if _bank_ifsc else "")
                            + (f"<div style='color:#94a3b8;font-size:0.8rem'><b>Branch:</b> {_bank_branch}</div>" if _bank_branch else "")
                            + f"<div style='color:#f59e0b;font-size:0.9rem;font-weight:700;margin-top:6px'>"
                            f"Amount: ₹{_t4_amt:,.2f}</div>"
                            + "</div>",
                            unsafe_allow_html=True
                        )
                        # WA to send bank details
                        if _pay_wa_mob:
                            import urllib.parse as _up4
                            _wa_bank_msg = (
                                f"Hi {_pay_party},\n\n"
                                f"Please transfer *₹{_t4_amt:,.0f}* for Order *{order.get('order_no','')}*:\n\n"
                                + (f"Bank: {_bank_name}\n" if _bank_name else "")
                                + (f"Account: *{_bank_acc}*\n" if _bank_acc else "")
                                + (f"IFSC: {_bank_ifsc}\n" if _bank_ifsc else "")
                                + (f"Branch: {_bank_branch}\n" if _bank_branch else "")
                                + f"\nAfter transfer, please share the transaction reference."
                            )
                            _wa_bank_url = f"https://wa.me/{_pay_wa_mob}?text={_up4.quote(_wa_bank_msg)}"
                            st.link_button(
                                "📲 Send Bank Details via WhatsApp",
                                _wa_bank_url,
                                use_container_width=True
                            )
                    else:
                        st.info("⚙️ No bank details configured. Add them in Shop Master settings.")

                    st.markdown("---")
                    _t4_ref = st.text_input("Transaction Reference / UTR No.",
                                            key=f"pt4_ref_{_order_id_for_pay[:8]}")
                    if st.button("✅ Record Bank Transfer",
                                 key=f"pt4_save_{_order_id_for_pay[:8]}",
                                 type="primary", use_container_width=True,
                                 disabled=(_t4_amt <= 0 or not _t4_ref)):
                        try:
                            _save_advance(_t4_amt, "NEFT/RTGS", _t4_ref)
                            st.session_state[_pay_status_key] = True
                            st.success(f"✅ ₹{_t4_amt:,.0f} via NEFT/RTGS ({_t4_ref}) recorded")
                            st.rerun()
                        except Exception as _e4: st.error(f"Failed: {_e4}")

            if _adv_recorded:
                st.success("✅ Payment recorded — order will save with payment linked")

        # ── Recheck dialog — shown after SAVE pressed, before commit ─────────
        if st.session_state.get(f"_bo_recheck_{_order_id_for_pay[:8]}"):
            st.warning(
                "⚠️ **Final check before saving.**  \n"
                "Once confirmed, **order lines cannot be changed**.  \n"
                f"**Order:** {order.get('order_no','?')}  ·  "
                f"**Lines:** {len(all_lines)}  ·  "
                f"**Total:** ₹{total_billing:,.2f}"
            )
            _rc1, _rc2 = st.columns(2)
            with _rc1:
                if st.button("✅ Yes — Confirm & Save",
                             key=f"rc_yes_{_order_id_for_pay[:8]}",
                             type="primary", use_container_width=True):
                    st.session_state.pop(f"_bo_recheck_{_order_id_for_pay[:8]}", None)
                    st.session_state[f"_bo_confirmed_{_order_id_for_pay[:8]}"] = True
                    st.rerun()
            with _rc2:
                if st.button("✏️ No — Go back and edit",
                             key=f"rc_no_{_order_id_for_pay[:8]}",
                             use_container_width=True):
                    st.session_state.pop(f"_bo_recheck_{_order_id_for_pay[:8]}", None)
                    st.info("Make changes above, then click Save again.")
                    st.rerun()

        # ── SAVE button or proceed if already confirmed ───────────────────────
        if st.session_state.get(f"_bo_confirmed_{_order_id_for_pay[:8]}"):
            st.session_state.pop(f"_bo_confirmed_{_order_id_for_pay[:8]}", None)
            _do_save_now = True
        else:
            _do_save_now = False

        if not _billing_frozen and not st.session_state.get(f"_bo_recheck_{_order_id_for_pay[:8]}"):
            if not _do_save_now:
                if st.button("✅ Save & Confirm Order", type="primary", width='stretch',
                             key="final_save_order_btn", disabled=is_locked("final_save")):
                    st.session_state[f"_bo_recheck_{_order_id_for_pay[:8]}"] = True
                    st.rerun()

        if _do_save_now or (not _billing_frozen and False):
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

                # ═══════════════════════════════════════════════════════════
                # PRE-SAVE ALLOCATION GUARD
                # Ensures every STOCK-routed line has a valid batch_allocation
                # before we write to DB. Catches any sync gaps between the
                # allocation window and the assignment panel.
                # ═══════════════════════════════════════════════════════════
                _alloc_issues = []
                _assignments_ss = st.session_state.get("bo_assignments", {})

                for _gi, _gl in enumerate(all_lines):
                    _g_eye   = str(_gl.get("eye_side","")).upper()
                    _g_name  = str(_gl.get("product_name",""))[:28]
                    _g_route = str(_gl.get("manufacturing_route") or "").upper()
                    _g_lk    = None
                    # Find the session-state assignment key for this line
                    for _gk in _assignments_ss:
                        if str(_gi) in str(_gk):
                            _g_lk = _gk
                            break

                    if _g_route == "STOCK":
                        # --- Sync pass: pull allocation from session-state assignment ---
                        _g_asgn = _assignments_ss.get(_g_lk, {}) if _g_lk else {}
                        _g_ba_asgn = _g_asgn.get("batch_allocation") or []
                        _g_ba_line = _gl.get("batch_allocation") or []
                        _g_aq_line = int(_gl.get("allocated_qty") or 0)
                        _g_qty     = int(_gl.get("billing_qty") or 0)

                        # Use whichever source has data
                        _g_ba_final = _g_ba_asgn or _g_ba_line
                        _g_aq_final = sum(int(b.get("allocated_qty",0)) for b in _g_ba_final
                                          if isinstance(b, dict)) if _g_ba_final else _g_aq_line

                        if _g_ba_final:
                            # Write back to line so persistence picks it up
                            _gl["batch_allocation"] = _g_ba_final
                            _gl["allocated_qty"]    = _g_aq_final
                            _gl["batch_status"]     = "ALLOCATED" if _g_aq_final >= _g_qty else "PARTIAL"
                        else:
                            # No allocation anywhere — check DB as last resort
                            _g_lid = str(_gl.get("line_id") or _gl.get("id") or "")
                            if _g_lid and len(_g_lid) > 10:
                                try:
                                    from modules.sql_adapter import run_query as _rq_g
                                    _g_db = _rq_g(
                                        "SELECT allocated_qty, "
                                        "COALESCE(lens_params->>'batch_allocation','[]') AS ba_json "
                                        "FROM order_lines WHERE id=%(lid)s::uuid LIMIT 1",
                                        {"lid": _g_lid}
                                    ) or []
                                    if _g_db:
                                        import json as _jg
                                        _g_aq_db = int(_g_db[0].get("allocated_qty") or 0)
                                        try:
                                            _g_ba_db = _jg.loads(_g_db[0].get("ba_json") or "[]")
                                        except Exception:
                                            _g_ba_db = []
                                        if _g_ba_db or _g_aq_db > 0:
                                            _gl["batch_allocation"] = _g_ba_db
                                            _gl["allocated_qty"]    = _g_aq_db or sum(
                                                int(b.get("allocated_qty",0)) for b in _g_ba_db
                                                if isinstance(b, dict)
                                            )
                                            _gl["batch_status"] = "ALLOCATED"
                                        else:
                                            _alloc_issues.append(
                                                f"{'RE' if _g_eye=='R' else 'LE' if _g_eye=='L' else _g_eye} "
                                                f"{_g_name} — STOCK route but not allocated"
                                            )
                                except Exception:
                                    pass

                    elif _g_route in ("VENDOR","EXTERNAL_LAB","INHOUSE"):
                        # Non-stock lines: ensure no stale allocation lingers
                        if _gl.get("batch_allocation"):
                            _gl["batch_allocation"] = []
                            _gl["allocated_qty"]    = 0

                if _alloc_issues:
                    st.error("❌ **Save blocked — allocation incomplete:**")
                    for _issue in _alloc_issues:
                        st.warning(f"• {_issue}")
                    st.info("Open **🗂️ Manage Stock** for each flagged line, allocate the batch, then save.")
                    return
                # ═══════════════════════════════════════════════════════════

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
                    # ── Status advance rules ─────────────────────────────
                    # STOCK only → CONFIRMED (already collected from shelf)
                    # VENDOR/INHOUSE/EXTERNAL_LAB → CONFIRMED once supplier
                    #   assigned (supplier_id set on line).  The assignment
                    #   panel's "Confirm All" is the procurement confirmation.
                    # If no supplier set yet → stay PENDING.
                    # RULE: Any save from backoffice moves order to CONFIRMED.
                    # PENDING / UNDER_REVIEW / PROVISIONAL → CONFIRMED on save.
                    # Staff has reviewed lines, pricing, allocation — save = confirm.
                    # Already CONFIRMED or beyond → leave status unchanged.
                    _CONFIRMABLE = {
                        "PENDING", "PENDING_VALIDATION", "PROVISIONAL",
                        "UNDER_REVIEW", "ORDER_SAVED", "",
                    }
                    if _cur_status.upper() in _CONFIRMABLE:
                        # ── FULL_ADVANCE gate: check payment before confirming ──
                        _fa_gate_ok = True
                        try:
                            from modules.core.business_rules import get_billing_category
                            from modules.sql_adapter import run_query as _rq_fa_bo
                            _fa_pid_bo = str(order.get("party_id") or "")
                            if _fa_pid_bo:
                                _fa_row_bo = _rq_fa_bo(
                                    "SELECT COALESCE(billing_category,payment_mode,'ON_COMPLETION') AS bc "
                                    "FROM parties WHERE id=%s::uuid LIMIT 1", (_fa_pid_bo,)
                                )
                                _fa_bc_bo = (_fa_row_bo[0]["bc"] if _fa_row_bo else None) or "ON_COMPLETION"
                                _fa_cfg_bo = get_billing_category(_fa_bc_bo)
                                if _fa_cfg_bo.get("requires_full_pay_before_confirm"):
                                    _fa_oid_bo = str(order.get("order_id") or order.get("id") or "")
                                    _fa_total_bo = float(order.get("total_value") or 0)
                                    _fa_paid_r_bo = _rq_fa_bo("""
                                        SELECT COALESCE(SUM(amount),0) AS paid FROM payments
                                        WHERE party_id=%s::uuid
                                          AND COALESCE(is_deleted,FALSE)=FALSE
                                          AND (
                                              (advance_for_order_id IS NOT NULL
                                               AND advance_for_order_id::text = %s)
                                              OR payment_date::date = CURRENT_DATE
                                          )
                                    """, (_fa_pid_bo, _fa_oid_bo))
                                    _fa_paid_bo = float(_fa_paid_r_bo[0]["paid"] if _fa_paid_r_bo else 0)
                                    if _fa_paid_bo < _fa_total_bo - 0.01:
                                        _fa_gate_ok = False
                                        _fa_pending = round(_fa_total_bo - _fa_paid_bo, 2)
                                        _fa_ono_disp = order.get("order_no") or _fa_oid_bo
                                        _fa_pname   = order.get("party_name") or ""
                                        st.error(
                                            f"❌ **Full Advance required — Order cannot be confirmed.**\n\n"
                                            f"**Party:** {_fa_pname}  \n"
                                            f"**Order:** #{_fa_ono_disp}  \n"
                                            f"**Order Total:** ₹{_fa_total_bo:,.2f}  \n"
                                            f"**Paid:** ₹{_fa_paid_bo:,.2f}  \n"
                                            f"**Pending:** ₹{_fa_pending:,.2f}  \n\n"
                                            f"👉 Go to **💰 Billing → Payment** → select party **{_fa_pname}** "
                                            f"→ select order **#{_fa_ono_disp}** → record ₹{_fa_pending:,.2f} "
                                            f"→ come back and save to confirm."
                                        )
                        except Exception:
                            pass  # gate is best-effort
                        if _fa_gate_ok:
                            order["status"] = "CONFIRMED"
                    # ─────────────────────────────────────────────────────

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

                    # Reset reassignment ack so next edit triggers the warning again
                    _oid_warn = get_display_order_id(order)
                    st.session_state.pop(f"reassign_ack_{_oid_warn}", None)
                    st.session_state.pop(f"reassign_warned_{_oid_warn}", None)

                    # ── Write edit log entry (JSONL file — zero DB load) ───
                    try:
                        from modules.security.roles import current_user_name as _cur_user
                        _log_user = _cur_user()
                        if not isinstance(_log_user, str):
                            _log_user = getattr(_log_user, "name", str(_log_user))
                    except Exception:
                        _log_user = st.session_state.get("user_name", "system")
                    try:
                        from modules.backoffice.edit_log_panel import log_edit as _log_edit
                        _log_edit(
                            event    = "ORDER_SAVED",
                            order_no = order.get("order_no", ""),
                            party    = order.get("party_name") or order.get("patient_name", ""),
                            by       = _log_user,
                            category = "SAVE",
                            detail   = {
                                "from_status": _old_status,
                                "to_status":   _new_status,
                                "line_count":  len(all_lines),
                            },
                            remarks  = f"Saved: {_old_status} → {_new_status}",
                        )
                    except Exception:
                        pass  # logging must never crash the save

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
                    _old_status = _cur_status  # Status before save
                    _new_status = order["status"]  # Status after save logic
                    # Verify display status against billing documents — never show BILLED if no docs
                    try:
                        from .backoffice import _determine_workflow_status, _order_lines as _ols
                        _display_new_status = _determine_workflow_status(order, _new_status, _ols(order))
                    except Exception:
                        _display_new_status = _new_status

                    # Check if status actually changed or was already confirmed
                    if _old_status == "CONFIRMED" and _new_status == "CONFIRMED":
                        # Order was already confirmed, show appropriate message
                        confirmed_at = order.get("confirmed_at") or order.get("updated_at") or order.get("created_at")
                        if confirmed_at:
                            try:
                                # Format timestamp nicely
                                if isinstance(confirmed_at, str):
                                    # Try to parse timestamp
                                    if "T" in confirmed_at:  # ISO format
                                        dt = datetime.datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
                                    else:
                                        dt = datetime.datetime.strptime(confirmed_at[:19], "%Y-%m-%d %H:%M:%S")
                                    formatted_time = dt.strftime("%d %b %Y at %I:%M %p")
                                    st.success(f"✅ Order **{_disp_no}** saved — Already confirmed on {formatted_time}")
                                else:
                                    st.success(f"✅ Order **{_disp_no}** saved — Status: CONFIRMED (already confirmed)")
                            except Exception:
                                st.success(f"✅ Order **{_disp_no}** saved — Status: CONFIRMED (already confirmed)")
                        else:
                            st.success(f"✅ Order **{_disp_no}** saved — Status: CONFIRMED (already confirmed)")
                    elif _old_status != _new_status and _new_status == "CONFIRMED":
                        # Status just changed to CONFIRMED - show original success message
                        st.success(
                            f"✅ Order **{_disp_no}** saved — "
                            f"Status: **{_display_new_status}** 🎯"
                        )
                        st.info("📦 All lines stock-allocated. Order is Collected ✓")
                        # ── WhatsApp notification ─────────────────────────
                        try:
                            from modules.wa_hub import wa_panel, wa_order_confirmed
                            from modules.settings.shop_master import get_unit_info
                            _wa_party  = order.get("party_name") or order.get("patient_name","")
                            _wa_mobile = order.get("patient_mobile","") or order.get("mobile","") or order.get("party_mobile","")
                            _wa_otype  = order.get("order_type","WHOLESALE")
                            _wa_shop   = get_unit_info("wholesale" if "WHOLE" in _wa_otype.upper() else "retail")
                            _wa_msg    = wa_order_confirmed(
                                party      = _wa_party,
                                order_no   = _disp_no,
                                total      = order.get("total_value", 0),
                                lines      = order.get("order_lines") or [],
                                shop_name  = _wa_shop.get("shop_name","DV Optical"),
                                phone      = _wa_shop.get("shop_phone",""),
                            )
                            wa_panel(_wa_mobile, _wa_msg,
                                     key=f"wa_bo_confirmed_{_disp_no}",
                                     title="📲 WhatsApp — Order Confirmed",
                                     expanded=True)
                        except Exception:
                            pass
                        st.balloons()
                    else:
                        # Run silent integrity check after every save
                        try:
                            from modules.backoffice.audit_logger import check_order_integrity
                            _ic_r = check_order_integrity(str(order.get("id","")))
                            if not _ic_r.get("ok"):
                                st.warning("⚠️ Integrity warning — check Status tab.")
                        except Exception:
                            pass
                        # Regular save with status change or no change
                        st.success(
                            f"✅ Order **{_disp_no}** saved — "
                            f"Status: **{_display_new_status}** 🎯"
                        )
                        if _display_new_status == "CONFIRMED":
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
                st.markdown("""
                <div style='background:linear-gradient(135deg,#1e3a5f,#0f172a);border:1px solid #3b82f6;
                    border-radius:12px;padding:20px;margin:10px 0'>
                    <h4 style='color:#60a5fa;margin:0 0 10px'>🔧 Job Card Management</h4>
                    <p style='color:#94a3b8;font-size:0.9rem'>
                        Job card creation, blank selection, and printing are now handled by 
                        <strong>Production staff</strong> in the <strong>Production → In-house Lab</strong> tab.
                    </p>
                    <p style='color:#f59e0b;font-size:0.85rem'>
                        ➜ Navigate to Production tab to manage job cards and blanks.
                    </p>
                </div>
                """, unsafe_allow_html=True)
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

        # ── Audit Trail + Integrity ───────────────────────────────────────
        _oid_t3 = str(order.get("id") or order.get("order_id") or "")

        # Integrity check (silent, shown only when issues found)
        if _oid_t3:
            try:
                from modules.backoffice.audit_logger import check_order_integrity
                _ic = check_order_integrity(_oid_t3)
                if not _ic.get("ok"):
                    st.markdown("---")
                    st.error("⚠️ **Integrity Issues Detected**")
                    for _issue in (_ic.get("issues") or []):
                        st.caption(f"• {_issue}")
                    st.caption("Investigate before proceeding with billing.")
            except Exception:
                pass

        # History panel — all audit_log events for this order
        st.markdown("---")
        with st.expander("📋 Change History (Audit Trail)", expanded=False):
            if not _oid_t3:
                st.caption("Order ID not resolved.")
            else:
                try:
                    from modules.backoffice.audit_logger import get_audit_trail
                    _trail = get_audit_trail(_oid_t3, limit=50)
                    if not _trail:
                        st.caption("No change history recorded yet.")
                    else:
                        _EVENT_COLORS = {
                            "save_order":       "#6366f1",
                            "status_changed":   "#8b5cf6",
                            "product_changed":  "#ef4444",
                            "price_override":   "#f59e0b",
                            "qty_changed":      "#f97316",
                            "payment_created":  "#10b981",
                            "payment_reversed": "#dc2626",
                            "stock_adjusted":   "#0ea5e9",
                            "invoice_created":  "#22c55e",
                        }
                        _EVENT_LABELS = {
                            "save_order":       "💾 Order Saved",
                            "status_changed":   "🔄 Status Changed",
                            "product_changed":  "📦 Line Changed",
                            "price_override":   "💰 Price Changed",
                            "qty_changed":      "🔢 Qty Changed",
                            "payment_created":  "✅ Payment Recorded",
                            "payment_reversed": "↩️ Payment Reversed",
                            "stock_adjusted":   "📊 Stock Adjusted",
                            "invoice_created":  "🧾 Invoice Created",
                        }
                        for _ev in _trail:
                            _ev_type = str(_ev.get("event") or "").lower()
                            _ev_ts   = str(_ev.get("created_at") or "")[:16]
                            _ev_user = str(_ev.get("user_id") or "system")
                            _ev_pay  = _ev.get("payload") or {}
                            _color   = _EVENT_COLORS.get(_ev_type, "#475569")
                            _label   = _EVENT_LABELS.get(_ev_type, f"⚙️ {_ev_type}")

                            # Build detail line from payload
                            _detail = ""
                            if isinstance(_ev_pay, dict):
                                _act = _ev_pay.get("action","")
                                if _act == "frame_sku_changed":
                                    _detail = (
                                        f"SKU: `{_ev_pay.get('old_sku','?')}` → "
                                        f"`{_ev_pay.get('new_sku','?')}` | "
                                        f"₹{float(_ev_pay.get('old_price',0)):,.2f} → "
                                        f"₹{float(_ev_pay.get('new_price',0)):,.2f}"
                                    )
                                elif _act == "line_deleted":
                                    _detail = (
                                        f"{_ev_pay.get('product','')} "
                                        f"[{_ev_pay.get('eye_side','')}] deleted"
                                        + (f" · {_ev_pay.get('qty_restored',0)} pcs restored" if _ev_pay.get('qty_restored') else "")
                                    )
                                elif _act == "order_cancelled":
                                    _detail = (
                                        f"Reason: {_ev_pay.get('reason','—')}"
                                        + (f" · Refund ₹{float(_ev_pay.get('refund_amount',0)):,.0f}" if _ev_pay.get('refund_amount') else "")
                                    )
                                elif _ev_pay.get("from_status") or _ev_pay.get("to_status"):
                                    _detail = (
                                        f"{_ev_pay.get('from_status','?')} → {_ev_pay.get('to_status','?')}"
                                    )
                                elif _ev_pay.get("diff"):
                                    _diff_d = _ev_pay["diff"]
                                    _detail = " | ".join(
                                        f"{k}: {v.get('old','?')} → {v.get('new','?')}"
                                        for k, v in list(_diff_d.items())[:3]
                                    )

                            st.markdown(
                                f"<div style='border-left:3px solid {_color};"
                                f"padding:4px 10px;margin-bottom:5px;'>"
                                f"<span style='color:{_color};font-weight:600;"
                                f"font-size:0.78rem'>{_label}</span> "
                                f"<span style='color:#6b7280;font-size:0.72rem'>"
                                f"— {_ev_ts} by {_ev_user}</span>"
                                + (f"<br><span style='color:#94a3b8;font-size:0.72rem'>"
                                   f"{_detail}</span>" if _detail else "")
                                + "</div>",
                                unsafe_allow_html=True
                            )
                except Exception as _trail_e:
                    st.caption(f"Audit trail unavailable: {_trail_e}")
    
    with tab4:
        # ═════════════════════════════════════════════════════════════
        # LIVE BILLING STATUS — automatic from challan/invoice system
        # ═════════════════════════════════════════════════════════════
        
        # Query live billing status from challan and invoice tables
        from modules.sql_adapter import run_query as _rq
        try:
            _order_id = order.get("id")
            
            # Get challan status - check if order is referenced in any challan
            _challan_sql = """
                SELECT c.challan_no, c.status, c.total_amount, c.created_at,
                       p.party_name, c.remarks
                FROM challans c
                LEFT JOIN parties p ON p.id = c.party_id
                WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text] OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
                AND c.status NOT IN ('CANCELLED','VOID')
                ORDER BY c.created_at DESC
                LIMIT 5
            """
            _challans = _rq(_challan_sql, {"oid": str(_order_id or ""), "ono": str(order.get("order_no") or "")}) if _rq else []
            
            # Get invoice status - check if order is referenced in any invoice
            _invoice_sql = """
                SELECT i.invoice_no, i.status, i.total_amount, i.created_at,
                       p.party_name, i.remarks
                FROM invoices i
                LEFT JOIN parties p ON p.id = i.party_id
                WHERE (i.order_ids::text[] @> ARRAY[%(oid)s::text] OR i.order_ids::text[] @> ARRAY[%(ono)s::text])
                AND i.status NOT IN ('CANCELLED','VOID')
                ORDER BY i.created_at DESC
                LIMIT 5
            """
            _invoices = _rq(_invoice_sql, {"oid": str(_order_id or ""), "ono": str(order.get("order_no") or "")}) if _rq else []
            
        except Exception as e:
            _challans = []
            _invoices = []
            st.caption(f"⚠️ Billing query error: {e}")
        
        # Use the new billing status UI module
        try:
            from .billing_status_ui import render_billing_status_panel
            render_billing_status_panel(order, all_lines)
        except ImportError:
            # billing_status_ui not yet deployed — show placeholder
            import streamlit as _st_bsu
            _st_bsu.info("🧾 Billing Status panel loading — please redeploy billing_status_ui.py")
        except Exception as _bsu_e:
            import streamlit as _st_bsu
            _st_bsu.error(f"Billing status error: {_bsu_e}")

        # ── Full Payment Provisioning (view, edit, void, re-record) ──
        try:
            from modules.billing.payment_manager import render_payment_provisioning
            render_payment_provisioning(order, all_lines)
        except Exception as _ape:
            st.caption(f"Payment panel error: {_ape}")

        # ── Payment Link (send to customer via WhatsApp) ──────────────
        try:
            from modules.billing.payment_link_manager import render_payment_link_panel
            render_payment_link_panel(order, all_lines)
        except Exception as _plm_err:
            st.caption(f"Payment link panel: {_plm_err}")


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

    if tab6 is not None:
        with tab6:
            try:
                from modules.backoffice.dispatch_panel import render_dispatch_panel
                render_dispatch_panel(order)
            except Exception as _dp_err:
                st.error(f"Dispatch panel error: {_dp_err}")
                import traceback as _tb_dp
                with st.expander("Debug"):
                    st.code(_tb_dp.format_exc())
    
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
            if st.button(" Update Status", type="primary", width='stretch'):
                # Update order status
                for o in st.session_state.bo_active_orders:
                    if get_display_order_id(o) == get_display_order_id(order):
                        o['status'] = new_status
                        o['updated_at'] = datetime.datetime.now().isoformat()
                        if notes:
                            if 'status_history' not in o:
                                o['status_history'] = []
                            o['status_history'].append({
                                'status': new_status,
                                'notes': notes,
                                'timestamp': datetime.datetime.now().isoformat(),
                                'user': st.session_state.get('user_name', 'Unknown')
                            })
                        st.success(f"✅ Status updated to {new_status}")
                        st.rerun()
                        break


# render_backoffice_dashboard, render_backoffice_management, and
# run_system_health_check live in backoffice.py and backoffice_helpers.py
# They are NOT duplicated here — import from those modules instead.