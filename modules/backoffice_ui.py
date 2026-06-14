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



def _bo_repricing_for_product(new_pid: str, line: Dict, order: Dict):
    """Resolve unit_price + gst_percent for a product change in backoffice.

    Retail uses MRP first; wholesale uses selling_price first. Falls back safely.
    Returns (unit_price_per_piece, gst_percent).
    """
    try:
        from modules.sql_adapter import run_query as _rq_re
        if not new_pid:
            return 0.0, 0.0
        _ot = str(order.get("order_type") or "RETAIL").upper()
        _rows = _rq_re("""
            SELECT
                COALESCE(p.gst_percent, 0)         AS gst_percent,
                COALESCE(MAX(i.selling_price), 0)  AS selling_price,
                COALESCE(MAX(i.mrp), 0)            AS mrp
            FROM products p
            LEFT JOIN inventory_stock i
                   ON i.product_id = p.id
                  AND COALESCE(i.is_active, TRUE) = TRUE
            WHERE p.id = %s::uuid
            GROUP BY p.gst_percent
            LIMIT 1
        """, (new_pid,)) or []
        if not _rows:
            return 0.0, 0.0
        _r = _rows[0]
        _gst = float(_r.get("gst_percent") or 0)
        _sp  = float(_r.get("selling_price") or 0)
        _mrp = float(_r.get("mrp") or 0)
        _price = (_mrp or _sp) if _ot == "RETAIL" else (_sp or _mrp)
        try:
            from modules.core.price_qty_governor import normalize_to_pcs_price
            if _price > 0:
                _price = normalize_to_pcs_price(_price, line)
        except Exception:
            pass
        return float(_price or 0), _gst
    except Exception:
        return 0.0, 0.0


def _bo_refresh_order_total_value(order_id: str) -> None:
    """Recompute orders.total_value from active order_lines after product/pricing edits."""
    try:
        from modules.sql_adapter import run_write as _rw_h, run_query as _rq_h
        if not order_id or len(str(order_id)) < 10:
            return
        _rows = _rq_h("""
            SELECT COALESCE(SUM(COALESCE(ol.billing_total, ol.total_price, 0)), 0) AS net_total
            FROM order_lines ol
            WHERE ol.order_id = %(oid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """, {"oid": order_id}) or []
        if _rows:
            _net = float(_rows[0].get("net_total") or 0)
            _rw_h(
                "UPDATE orders SET total_value=%(tv)s, updated_at=NOW() WHERE id=%(oid)s::uuid",
                {"tv": round(_net, 2), "oid": order_id},
            )
    except Exception:
        pass

@st.dialog(" Change Product", width="large")
def product_change_dialog():
    """
    Product change dialog — aligned with punching system.
    Shows: Category → Brand → Product → Index → Coating → Treatment
    For frames: Brand → Product (model number)
    Writes change to DB immediately.
    """
    import json as _pcd_json
    modal_state = st.session_state.get("bo_product_change_modal", {})
    if not modal_state.get("active", False):
        return

    line      = modal_state["line"]
    idx       = modal_state["idx"]
    eye_label = modal_state["eye_label"]
    order     = modal_state["order"]
    order_id  = str(order.get("id") or order.get("order_id") or "")
    line_id   = str(line.get("line_id") or line.get("id") or "")

    st.warning(f"Changing product for **{eye_label} Eye** — Line #{idx + 1}")
    st.caption("Current: " + line.get("product_name","N/A") + " | " + line.get("brand","N/A"))

    # ── Load product master ───────────────────────────────────────────
    try:
        from modules.sql_adapter import read_product_master
        products_df = read_product_master()
    except Exception as _e:
        st.error(f"Could not load products: {_e}"); return
    if products_df is None or products_df.empty:
        st.error("No products available"); return

    # ── Is current line a frame? ──────────────────────────────────────
    _cur_mg = str(line.get("main_group") or "").lower()
    _is_frame = "frame" in _cur_mg or "sunglass" in _cur_mg

    st.markdown("---")

    # ── Category filter ───────────────────────────────────────────────
    _groups = [""] + sorted(products_df["main_group"].dropna().astype(str).unique())
    _default_g = str(line.get("main_group") or "")
    _g_idx = _groups.index(_default_g) if _default_g in _groups else 0
    _sel_group = st.selectbox("Category", _groups, index=_g_idx, key="pcd_group")
    _is_frame_sel = "frame" in _sel_group.lower() or "sunglass" in _sel_group.lower()

    # ── Brand filter ──────────────────────────────────────────────────
    _pf = products_df.copy()
    if _sel_group:
        _pf = _pf[_pf["main_group"].astype(str) == _sel_group]
    _brands = [""] + sorted(_pf["brand"].dropna().astype(str).unique())
    _def_b = str(line.get("brand") or "")
    _b_idx = _brands.index(_def_b) if _def_b in _brands else 0
    _sel_brand = st.selectbox("Brand", _brands, index=_b_idx, key="pcd_brand")
    if _sel_brand:
        _pf = _pf[_pf["brand"].astype(str) == _sel_brand]

    # ── Product (model for frames) ────────────────────────────────────
    _prod_list = [""] + sorted(_pf["product_name"].dropna().astype(str).unique())
    _def_p = str(line.get("product_name",""))
    _p_idx = _prod_list.index(_def_p) if _def_p in _prod_list else 0
    _prod_label = "Frame Model" if _is_frame_sel else "Select Product *"
    _sel_prod = st.selectbox(_prod_label, _prod_list, index=_p_idx, key="pcd_prod")

    if not _sel_prod:
        _pc1, _pc2 = st.columns(2)
        if _pc1.button("Cancel", key="pcd_cancel_nosel"):
            st.session_state["bo_product_change_modal"] = {"active": False}
            st.rerun()
        return

    _prod_row = _pf[_pf["product_name"].astype(str) == _sel_prod]
    if _prod_row.empty:
        st.warning("Product not found"); return
    _prod_row = _prod_row.iloc[0]

    # ── Lens parameters (Index / Coating / Treatment) — not shown for frames ──
    _new_index    = ""
    _new_coating  = ""
    _new_treatment= ""

    if not _is_frame_sel:
        st.markdown("**Lens Specification**")
        _lp_cur = line.get("lens_params") or {}
        if isinstance(_lp_cur, str):
            try: _lp_cur = _pcd_json.loads(_lp_cur)
            except: _lp_cur = {}

        _li1, _li2, _li3 = st.columns(3)
        _new_index = _li1.text_input(
            "Index",
            value=str(_lp_cur.get("lens_index") or _lp_cur.get("index") or
                      _prod_row.get("index_value") or ""),
            key="pcd_index",
        )
        _new_coating = _li2.text_input(
            "Coating",
            value=str(_lp_cur.get("coating") or _prod_row.get("coating_type") or ""),
            key="pcd_coating",
        )
        _new_treatment = _li3.text_input(
            "Treatment / Material",
            value=str(_lp_cur.get("treatment") or _lp_cur.get("material") or
                      _prod_row.get("material") or ""),
            key="pcd_treatment",
            help="e.g. Clear, Photochromic, Tinted",
        )

    # ── Preview ───────────────────────────────────────────────────────
    _preview_parts = [str(_prod_row["product_name"])]
    if _new_index:    _preview_parts.append(f"Idx {_new_index}")
    if _new_coating:  _preview_parts.append(_new_coating)
    if _new_treatment and not _is_frame_sel: _preview_parts.append(_new_treatment)
    st.success("Selected: " + " | ".join(_preview_parts))

    st.markdown("---")
    _pa1, _pa2 = st.columns(2)

    with _pa1:
        if st.button("✅ Apply Change", type="primary",
                     use_container_width=True, key="pcd_apply"):
            try:
                from modules.sql_adapter import run_write as _pcd_rw, run_query as _pcd_rq

                # ── Update lens_params with new index/coating/treatment ────
                _lp_new = dict(_lp_cur) if not _is_frame_sel else {}
                if _new_index:    _lp_new["lens_index"] = _new_index; _lp_new["index"] = _new_index
                if _new_coating:  _lp_new["coating"]    = _new_coating
                if _new_treatment:_lp_new["treatment"]  = _new_treatment; _lp_new["material"] = _new_treatment

                # Product changed: clear stale production/allocation metadata.
                _lp_new["manufacturing_route"] = None
                _lp_new["batch_allocation"]    = []
                _lp_new["batch_status"]        = "PENDING"
                _lp_new.pop("surfacing_data", None)

                _new_pid = str(_prod_row["product_id"])

                # ── Mutate in-memory line FIRST so pricing/discount engines see new product ──
                line["product_id"]   = _new_pid
                line["product_name"] = str(_prod_row["product_name"])
                line["brand"]        = str(_prod_row.get("brand", ""))
                line["main_group"]   = str(_prod_row.get("main_group", ""))
                line["material"]     = str(_prod_row.get("material", ""))
                line["lens_params"]  = _lp_new
                line["manufacturing_route"] = None
                line["batch_allocation"]    = []
                line["allocated_qty"]       = 0
                line["batch_status"]        = "PENDING"
                line["suggested_allocation"] = None

                # Clear old product discount attribution before new-rule evaluation.
                line["discount_percent"] = 0.0
                line["discount_amount"]  = 0.0
                line["discount_rule"]    = ""
                line["applied_rule_ids"] = ""

                # ── Re-resolve price + GST for the new product ──
                _new_unit_price, _new_gst_pct = _bo_repricing_for_product(_new_pid, line, order)
                line["unit_price"]  = _new_unit_price
                line["gst_percent"] = _new_gst_pct
                _qty_pcd = int(line.get("billing_qty") or line.get("quantity") or 1)
                line["quantity"] = _qty_pcd
                line["total_price"]   = round(_new_unit_price * _qty_pcd, 2)
                line["billing_total"] = line["total_price"]

                # ── Re-apply discount engine for NEW brand/product ──
                try:
                    from modules.pricing.discount_flow import apply_order_discounts
                    _ot_pcd = str(order.get("order_type") or "RETAIL").upper()
                    _party_id = str(order.get("party_id") or "").strip()
                    if not _party_id:
                        _party_name = str(order.get("party_name") or order.get("patient_name") or "").strip()
                        if _party_name:
                            try:
                                _r = _pcd_rq(
                                    "SELECT id::text AS id FROM parties "
                                    "WHERE party_name=%s AND COALESCE(is_active,TRUE)=TRUE LIMIT 1",
                                    (_party_name,),
                                ) or []
                                if _r:
                                    _party_id = str(_r[0].get("id") or "")
                            except Exception:
                                pass
                    apply_order_discounts([line], party_id=_party_id, order_type=_ot_pcd)
                except Exception as _de:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[product_change_dialog] discount re-eval failed: {_de}"
                    )

                if float(line.get("discount_amount") or 0) > 0:
                    _lp_new["discount_status"] = "APPLIED"
                else:
                    _lp_new.pop("discount_status", None)

                # ── Persist complete pricing/discount state to DB ──
                _pcd_params = {
                    "pid": _new_pid,
                    "lp":  _pcd_json.dumps(_lp_new),
                    "up":  float(line.get("unit_price") or 0),
                    "tp":  float(line.get("billing_total") or line.get("total_price") or 0),
                    "gp":  float(line.get("gst_percent") or 0),
                    "ga":  float(line.get("gst_amount") or 0),
                    "dp":  float(line.get("discount_percent") or 0),
                    "da":  float(line.get("discount_amount") or 0),
                    "dr":  str(line.get("discount_rule") or ""),
                    "ari": str(line.get("applied_rule_ids") or ""),
                    "lid": line_id,
                }
                try:
                    _pcd_rw("""
                        UPDATE order_lines
                        SET product_id           = %(pid)s::uuid,
                            lens_params          = %(lp)s::jsonb,
                            unit_price           = %(up)s,
                            total_price          = %(tp)s,
                            billing_total        = %(tp)s,
                            gst_percent          = %(gp)s,
                            gst_amount           = %(ga)s,
                            discount_percent     = %(dp)s,
                            discount_amount      = %(da)s,
                            discount_rule        = %(dr)s,
                            applied_rule_ids     = %(ari)s,
                            allocated_qty        = 0,
                            batch_status         = 'PENDING',
                            suggested_allocation = NULL,
                            updated_at           = NOW()
                        WHERE id = %(lid)s::uuid
                    """, _pcd_params)
                except Exception:
                    _pcd_rw("""
                        UPDATE order_lines
                        SET product_id           = %(pid)s::uuid,
                            lens_params          = %(lp)s::jsonb,
                            unit_price           = %(up)s,
                            total_price          = %(tp)s,
                            gst_percent          = %(gp)s,
                            gst_amount           = %(ga)s,
                            discount_percent     = %(dp)s,
                            discount_amount      = %(da)s,
                            discount_rule        = %(dr)s,
                            applied_rule_ids     = %(ari)s,
                            allocated_qty        = 0,
                            batch_status         = 'PENDING',
                            suggested_allocation = NULL,
                            updated_at           = NOW()
                        WHERE id = %(lid)s::uuid
                    """, _pcd_params)

                _bo_refresh_order_total_value(order_id)

                # ── Clear all relevant caches ──
                try:
                    from modules.backoffice.order_loader import (
                        load_single_order, load_orders_from_database, load_orders_summary
                    )
                    for _fn in (load_single_order, load_orders_from_database, load_orders_summary):
                        try: _fn.clear()
                        except Exception: pass
                except Exception:
                    pass
                try:
                    from modules.backoffice.backoffice_helpers import load_orders_from_database as _boh_load
                    _boh_load.clear()
                    st.session_state["bo_orders_loaded"] = False
                except Exception:
                    pass

                st.session_state["bo_product_change_modal"] = {"active": False}

                # ── Force fresh DB reload on next render ──────────────────
                # bo_active_orders holds the cached order dict with stale line data.
                # Removing it here means render_order_detail falls through to
                # load_single_order(order_id) on the next rerun — guaranteeing
                # the UI shows the just-persisted product/price/discount values.
                st.session_state["bo_active_orders"] = [
                    o for o in st.session_state.get("bo_active_orders", [])
                    if str(o.get("id") or o.get("order_id") or "") != str(order_id)
                ]
                st.session_state["bo_orders_loaded"] = False

                _disc_msg = ""
                _da_show = float(line.get("discount_amount") or 0)
                if _da_show > 0:
                    _rule_show = str(line.get("discount_rule") or "rule")
                    _disc_msg = f" · Discount applied: {_rule_show} ({_da_show:.2f})"
                st.success(f"✅ Product changed to {_prod_row['product_name']}{_disc_msg}")
                st.rerun()

            except Exception as _pe:
                st.error(f"Product change failed: {_pe}")

    with _pa2:
        if st.button("Cancel", use_container_width=True, key="pcd_cancel"):
            st.session_state["bo_product_change_modal"] = {"active": False}
            st.rerun()


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
                        _r_pk = f"bo_jc_print_r_{r_line.get('line_id','')[:8]}"
                        if st.button("🖨 Print Job Card — R", key=_r_pk+"_btn",
                                     use_container_width=True):
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
                        _l_pk = f"bo_jc_print_l_{l_line.get('line_id','')[:8]}"
                        if st.button("🖨 Print Job Card — L", key=_l_pk+"_btn",
                                     use_container_width=True):
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
                    _s_pk = f"bo_jc_print_s_{line.get('line_id','')[:8]}"
                    if st.button("🖨 Print Job Card", key=_s_pk+"_btn",
                                 use_container_width=True):
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
        if get_display_order_id(o) == order_id or str(o.get("order_id","")) == str(order_id):
            order = o
            break

    # Lazy load: if we only have a summary row (no lines), load full detail now
    if order is not None and not order.get("lines") and not order.get("_existed_in_db"):
        try:
            from modules.backoffice.order_loader import load_single_order as _lso
            _full = _lso(str(order.get("id") or order.get("order_id") or order_id))
            if _full:
                order = _full
                # Update the session state entry so next open is instant
                for _i, _o in enumerate(st.session_state.bo_active_orders):
                    if get_display_order_id(_o) == order_id:
                        st.session_state.bo_active_orders[_i] = _full
                        break
        except Exception as _le:
            pass  # fall through with summary row — UI will show what it has

    if not order:
        # Not in session list — try direct DB load
        try:
            from modules.backoffice.order_loader import load_single_order as _lso2
            order = _lso2(str(order_id))
        except Exception:
            pass

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
        # Show status + production sub-stage in brackets when in-house order
        _disp_status = order.get("status", "PENDING")
        _inhouse_lns  = order.get("inhouse_lines") or []
        # Show stage in brackets for ANY order status that has inhouse lines — not just
        # "IN PRODUCTION". Orders at READY_TO_BILL, DISPATCHED, etc. also have stages.
        if _inhouse_lns:
            try:
                from modules.sql_adapter import run_query as _rq_stg
                _jm_stgs = _rq_stg("""
                    SELECT ol.eye_side,
                           COALESCE(jm.current_stage, 'JOB_CREATED') AS stage
                    FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    LEFT JOIN job_master jm ON jm.order_line_id = ol.id
                    WHERE o.order_no = %(ono)s
                      AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                    ORDER BY ol.eye_side
                """, {"ono": order.get("order_no", "")})
                if _jm_stgs:
                    _STAGE_SHORT = {
                        "JOB_CREATED":       "Job Created",
                        "PRINTED":           "Printed",
                        "JOB_PRINTED":       "Printed",
                        "PRODUCTION_PICKED": "In Production",
                        "PRODUCTION_DONE":   "Production Done",
                        "INSPECTION":        "Inspection",
                        "BLANK_ALLOCATED":   "Blank Allocated",
                        "HARDCOAT_PICKED":   "Hardcoat Picked",
                        "HARDCOAT_DONE":     "Hardcoat Done",
                        "COLOURING_PICKED":  "Colouring Picked",
                        "COLOURING_DONE":    "Colouring Done",
                        "ARC_SENT":          "ARC Sent",
                        "ARC_RECEIVED":      "ARC Received",
                        "FINAL_QC":          "Final QC",
                        "READY_FOR_PACK":    "Ready for Pack",
                        "READY_TO_BILL":     "Ready to Bill",
                        "FITTING_PENDING":   "Fitting Pending",
                        "FITTING_DONE":      "Fitting Done",
                        "REJECTED":          "Rejected",
                    }
                    _parts = []
                    for _jr in _jm_stgs:
                        _e = str(_jr.get("eye_side") or "").upper()
                        _s = str(_jr.get("stage") or "JOB_CREATED").upper()
                        _short = _STAGE_SHORT.get(_s, _s.replace("_"," ").title())
                        _e_label = {"R":"RE","L":"LE"}.get(_e, _e)
                        _parts.append(f"{_e_label}: {_short}")
                    if _parts:
                        _disp_status = f"{order.get('status','PENDING')} ({' | '.join(_parts)})"
            except Exception:
                pass
        st.metric("Status", _disp_status)

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
    # Auto-jump to billing tab if coming from production page OR after challan creation
    _jump_billing = (
        st.session_state.pop("bo_jump_to_billing", False)
        or st.session_state.pop("bo_show_billing_tab", False)
    )

    # When jumped from production page, highlight the billing tab path
    if _jump_billing:
        st.info(
            "💰 **Navigated from Production page** — "
            "open the **Billing Summary** tab below to create challan.",
            icon="💰"
        )

    # Streamlit always opens the first tab and has no official API to switch tabs.
    # When coming from Production → Open Billing, create the Billing Summary tab
    # first but keep variable tab4 bound to that tab, so the existing billing code
    # renders immediately instead of landing on Order Items.
    if _jump_billing:
        tab4, tab1, tab2, tab3, tab5, tab6 = st.tabs([
            "💰 Billing Summary ◀",
            "📦 Order Items",
            "📄 Documents",
            "📊 Status",
            "🚚 Supplier Orders",
            "💳 Billing Gate",
        ])
    else:
        tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
            "📦 Order Items",
            "📄 Documents",
            "📊 Status",
            "💰 Billing Summary",
            "🚚 Supplier Orders",
            "💳 Billing Gate",
        ])
    
    # Consume the one-shot print-suppression guard now that Backoffice has loaded.
    st.session_state.pop("_navigating_to_billing", None)

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

                    # ── Optical measurements from punching/backoffice ─────
                    try:
                        import json as _meas_json
                        _lp_meas = line.get("lens_params") or {}
                        if isinstance(_lp_meas, str):
                            try: _lp_meas = _meas_json.loads(_lp_meas)
                            except Exception: _lp_meas = {}
                        _bp_meas = line.get("boxing_params") or {}
                        if isinstance(_bp_meas, str):
                            try: _bp_meas = _meas_json.loads(_bp_meas)
                            except Exception: _bp_meas = {}
                        _surf_meas = (_lp_meas.get("surfacing_data") or {}) if isinstance(_lp_meas, dict) else {}

                        _meas_items = []
                        def _add_meas(label, value):
                            if value not in (None, "", "None", "nan"):
                                _meas_items.append(f"<b>{label}</b> {value}")

                        _add_meas("DIA", _lp_meas.get("diameter") or _surf_meas.get("diameter") or _bp_meas.get("diameter") or _bp_meas.get("dia"))
                        _add_meas("Frame", _lp_meas.get("frame_type") or _surf_meas.get("frame_type"))
                        _add_meas("FH", _lp_meas.get("fitting_height") or _surf_meas.get("fitting_height") or _bp_meas.get("fitting_height") or _bp_meas.get("fh"))
                        _add_meas("PD", _bp_meas.get("pd") or _bp_meas.get("pd_right") or _bp_meas.get("pd_left"))
                        _add_meas("Corridor", _lp_meas.get("corridor") or _bp_meas.get("corridor"))
                        _add_meas("BC", _surf_meas.get("base_curve") or _lp_meas.get("base_curve"))
                        if _meas_items:
                            st.markdown(
                                "<div style='display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 2px 0'>"
                                + "".join(
                                    f"<span style='background:#eef2ff;color:#312e81;border:1px solid #c7d2fe;"
                                    f"border-radius:6px;padding:3px 7px;font-size:0.75rem'>{x}</span>"
                                    for x in _meas_items
                                )
                                + "</div>",
                                unsafe_allow_html=True,
                            )
                    except Exception:
                        pass

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
        # Blocked once a blank is assigned (job card saved) or challan exists.
        # ══════════════════════════════════════════════════════════════════════
        _order_status_upper = str(order.get("status","")).upper()
        _pipeline_locked_statuses = {
            "IN_PRODUCTION","READY","CHALLANED","INVOICED","DISPATCHED","DELIVERED","CLOSED"
        }
        # Check if any challan exists for this order
        _has_challan_lock = False
        try:
            from modules.sql_adapter import run_query as _rq_chk
            _ch_rows = _rq_chk("""
                SELECT 1 FROM challans c
                WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
                  AND c.status NOT IN ('CANCELLED','VOID')
                LIMIT 1
            """, {"oid": str(order.get("id") or ""), "ono": str(order.get("order_no") or "")})
            _has_challan_lock = bool(_ch_rows)
        except Exception:
            pass

        _order_items_frozen = (
            _order_status_upper in _pipeline_locked_statuses
            or _has_challan_lock
        )

        if _order_items_frozen:
            _freeze_reason = (
                "🧾 Challan exists — order items are locked."
                if _has_challan_lock else
                f"⚙️ Order is {_order_status_upper} — blank assignment locked once production starts."
            )
            st.markdown(
                f"<div style='background:#1a0a00;border:1px solid #f97316;"
                f"border-radius:8px;padding:10px 16px;margin:8px 0'>"
                f"<span style='color:#fb923c;font-weight:700;font-size:0.85rem'>"
                f"🔒 {_freeze_reason}</span>"
                f"<span style='color:#78350f;font-size:0.78rem;display:block;margin-top:4px'>"
                f"To modify: cancel the challan first, then unlock from the assignment panel.</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        elif _ASSIGNMENT_PANEL_AVAILABLE:
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
        if _order_items_frozen:
            st.button(
                "🔒 SAVE LOCKED — Order in pipeline or challan exists",
                type="secondary", use_container_width=True,
                key="final_save_frozen", disabled=True,
                help="Cancel challan or use supervisor override to edit"
            )
        elif st.button(" SAVE TO ORDER", type="primary", use_container_width=True,
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
                if not _jump_billing:
                    generate_job_cards(order)
                    shown_any = True
                else:
                    st.info("📄 Job cards available — navigate here manually after billing.")
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
        # ── Billing Summary ───────────────────────────────────────────────
        st.markdown("### 💰 Billing Summary")

        # ── Pricing summary ───────────────────────────────────────────────
        locked_count    = sum(1 for line in all_lines if line.get("pricing_locked", False))
        total_billing   = sum(float(line.get("billing_total") or 0) for line in all_lines)
        total_discount  = sum(float(line.get("discount_amount") or 0) for line in all_lines)
        total_allocated = sum(int(line.get("allocated_qty") or 0) for line in all_lines)

        if locked_count > 0:
            st.info(f"🔒 {locked_count} of {len(all_lines)} line(s) have locked pricing")

        _mc1, _mc2, _mc3, _mc4 = st.columns(4)
        _mc1.metric("Lines",          len(all_lines))
        _mc2.metric("Allocated",      total_allocated)
        _mc3.metric("Discount",       f"₹{total_discount:.2f}")
        _mc4.metric("Billing Total",  f"₹{total_billing:.2f}")

        # ── Line table with discount ──────────────────────────────────────
        def _prod_billing_label(line: dict) -> str:
            """Build full product label with index and coating for billing table."""
            import json as _bj_lbl
            _nm = str(line.get("product_name") or "N/A")
            _lp = line.get("lens_params") or {}
            if isinstance(_lp, str):
                try: _lp = _bj_lbl.loads(_lp)
                except: _lp = {}
            _coat = str(line.get("coating_type") or line.get("coating") or
                        _lp.get("coating_type") or _lp.get("coating") or "").strip()
            _idx  = str(line.get("index_value") or line.get("lens_index") or
                        _lp.get("index_value") or _lp.get("lens_index") or "").strip()
            label = _nm
            if _idx and _idx not in _nm:
                label = f"{label} ({_idx})"
            if _coat and _coat not in _nm:
                label = f"{label} {_coat}"
            return label


        billing_data = []
        for _bi, line in enumerate(all_lines, 1):
            _bqty   = int(line.get("billing_qty") or 0)
            _bprice = float(line.get("unit_price") or 0)
            _bdisc  = float(line.get("discount_amount") or 0)
            _btotal = float(line.get("billing_total") or 0)
            _bgross = _bqty * _bprice
            billing_data.append({
                "#":        _bi,
                "Product":  _prod_billing_label(line),
                "Eye":      str(line.get("eye_side", "")).upper(),
                "Qty":      _bqty,
                "Unit ₹":   f"{_bprice:.2f}",
                "Gross ₹":  f"{_bgross:.2f}",
                "Disc ₹":   f"{_bdisc:.2f}" if _bdisc else "—",
                "Total ₹":  f"{_btotal:.2f}",
                "Route":    str(line.get("manufacturing_route") or "—").upper(),
                "🔒":       "🔒" if line.get("pricing_locked") else "",
            })
        st.dataframe(pd.DataFrame(billing_data), use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── INLINE BILL NOW ───────────────────────────────────────────────
        # Check which lines are ready to bill (not yet on any challan)
        _bill_ready = []
        _bill_blocked = ""
        try:
            from modules.sql_adapter import run_query as _rq_b4
            _BILL_READY_STAGES = {
                # Strict billing gate:
                # READY_FOR_PACK is not billable. Packing must advance to READY_TO_BILL first.
                "READY_TO_BILL", "READY_FOR_BILLING",
            }
            for _bl in all_lines:
                _bl_id    = str(_bl.get("line_id") or _bl.get("id") or "")
                _bl_route = str(_bl.get("manufacturing_route") or "").upper()
                _bl_price = float(_bl.get("billing_total") or 0)
                if _bl_price <= 0:
                    continue  # skip zero-value lines

                # Already billed?
                _already = _rq_b4("""
                    SELECT 1 FROM challan_lines cl
                    JOIN challans c ON c.id = cl.challan_id
                    WHERE cl.order_line_id = %(lid)s::uuid
                      AND c.status NOT IN ('CANCELLED','VOID')
                    LIMIT 1
                """, {"lid": _bl_id}) if _bl_id else []
                if _already:
                    continue

                if _bl_route != "INHOUSE":
                    # Non-inhouse: always billable
                    _bill_ready.append(_bl)
                else:
                    # Inhouse: check job stage
                    _jm = _rq_b4("""
                        SELECT current_stage, is_closed FROM job_master
                        WHERE order_line_id = %(lid)s::uuid LIMIT 1
                    """, {"lid": _bl_id}) if _bl_id else []
                    if _jm:
                        _stg   = str(_jm[0].get("current_stage") or "").upper()
                        _clsd  = bool(_jm[0].get("is_closed"))
                        if _clsd or _stg in _BILL_READY_STAGES:
                            _bill_ready.append(_bl)
                        else:
                            _bill_blocked = (
                                f"{str(_bl.get('product_name',''))[:20]} "
                                f"[{_bl_route}]: stage {_stg or 'NOT STARTED'}"
                            )
                    else:
                        # No job card yet — not ready
                        _bill_blocked = (
                            f"{str(_bl.get('product_name',''))[:20]}: "
                            "Job card not created yet"
                        )
        except Exception as _b4e:
            st.caption(f"Billing readiness check: {_b4e}")

        _bill_total = sum(float(l.get("billing_total") or 0) for l in _bill_ready)
        _bill_lbl = (
            f"💰 Bill Now — {len(_bill_ready)} line(s) · ₹{_bill_total:,.2f}"
            if _bill_ready else "💰 Billing (not ready)"
        )

        with st.expander(
            _bill_lbl,
            expanded=bool(_bill_ready) and not _bill_blocked and _jump_billing
        ):
            if _bill_blocked:
                st.warning(
                    "Cannot bill — INHOUSE job not ready: "
                    + _bill_blocked
                    + ". Advance to Ready for Pack then Ready to Bill first."
                )
            if not _bill_ready:
                if not _bill_blocked:
                    st.info("No unbilled lines ready on this order.")
            else:
                # Show what will be billed
                for _rl in _bill_ready:
                    _rl_eye   = str(_rl.get("eye_side","")).upper()
                    _rl_name  = str(_rl.get("product_name","")).split(" | ")[0][:35]
                    _rl_disc  = float(_rl.get("discount_amount") or 0)
                    _rl_total = float(_rl.get("billing_total") or 0)
                    st.markdown(
                        f"<div style='display:flex;justify-content:space-between;"
                        f"padding:3px 0;border-bottom:1px solid #1e293b;font-size:0.8rem'>"
                        f"<span>{_rl_eye} {_rl_name}</span>"
                        f"<span style='color:#10b981;font-weight:700'>₹{_rl_total:,.2f}"
                        + (f" <span style='color:#64748b;font-size:0.7rem'>(-₹{_rl_disc:.2f})</span>"
                           if _rl_disc > 0 else "")
                        + "</span></div>",
                        unsafe_allow_html=True,
                    )

                st.markdown(
                    f"<div style='text-align:right;color:#10b981;font-weight:800;"
                    f"font-size:1rem;padding:8px 0'>Total: ₹{_bill_total:,.2f}</div>",
                    unsafe_allow_html=True,
                )

                _xc1, _xc2 = st.columns(2)
                _chal_no  = _xc1.text_input("Challan No",
                                              key=f"bo_chal_{order_id}",
                                              placeholder="Auto-generated if blank")
                _remarks  = _xc2.text_input("Remarks",
                                              key=f"bo_rem_{order_id}",
                                              placeholder="Optional")

                _do_chal = st.button(
                    f"🧾 Create Challan — ₹{_bill_total:,.2f}",
                    key=f"bo_do_chal_{order_id}",
                    type="primary", use_container_width=True,
                )
                _do_inv = st.button(
                    "🧾 → 📄 Challan + Invoice",
                    key=f"bo_do_inv_{order_id}",
                    use_container_width=True,
                )

                if _do_chal or _do_inv:
                    try:
                        from modules.billing.challan_invoice_manager import create_challan
                        from modules.sql_adapter import run_query as _rq_ch

                        # ── Repair missing blank_allocations ─────────────────────────
                        # Job card saves blank selection into lens_params.surfacing_data
                        # but may not always write a row to blank_allocations (older
                        # pipeline versions, network blip during save, etc.).
                        # Billing readiness checks blank_allocations — if the row is
                        # absent the challan gate says "no blank allocated" even though
                        # the technician did allot the blank.
                        # This runs silently before challan creation; it is idempotent
                        # (ON CONFLICT DO UPDATE) so safe to call every time.
                        def _repair_missing_blank_allocations(line_ids: list) -> None:
                            import json as _rj
                            from modules.sql_adapter import run_query as _rq_rep, run_write as _rw_rep
                            for _lid in line_ids:
                                if not _lid:
                                    continue
                                try:
                                    _lrows = _rq_rep(
                                        "SELECT lens_params, eye_side FROM order_lines "
                                        "WHERE id = %(lid)s::uuid LIMIT 1",
                                        {"lid": _lid},
                                    )
                                    if not _lrows:
                                        continue
                                    _lp = _lrows[0].get("lens_params") or {}
                                    if isinstance(_lp, str):
                                        try:
                                            _lp = _rj.loads(_lp)
                                        except Exception:
                                            _lp = {}
                                    _surf     = (_lp.get("surfacing_data") or {}) if isinstance(_lp, dict) else {}
                                    _blank_id = _surf.get("blank_id") or _surf.get("selected_blank_id")
                                    if not _blank_id:
                                        continue
                                    # Check if allocation row already exists
                                    _existing = _rq_rep(
                                        "SELECT 1 FROM blank_allocations "
                                        "WHERE order_line_id = %(lid)s::uuid LIMIT 1",
                                        {"lid": _lid},
                                    )
                                    if _existing:
                                        continue  # already allocated — nothing to repair
                                    # Write the missing row
                                    _rw_rep("""
                                        INSERT INTO blank_allocations
                                            (id, order_line_id, blank_id, eye_side,
                                             base_selected, allocated_at)
                                        VALUES (
                                            gen_random_uuid(),
                                            %(lid)s::uuid,
                                            %(bid)s::uuid,
                                            %(eye)s,
                                            %(base)s,
                                            NOW()
                                        )
                                        ON CONFLICT (order_line_id) DO UPDATE SET
                                            blank_id      = EXCLUDED.blank_id,
                                            eye_side      = EXCLUDED.eye_side,
                                            base_selected = EXCLUDED.base_selected,
                                            allocated_at  = NOW()
                                    """, {
                                        "lid":  _lid,
                                        "bid":  str(_blank_id),
                                        "eye":  _lrows[0].get("eye_side") or "",
                                        "base": _surf.get("base_curve") or _surf.get("base_selected"),
                                    })
                                except Exception:
                                    pass  # non-fatal — challan creation will surface any hard block

                        _repair_line_ids = [
                            str(l.get("line_id") or l.get("id", ""))
                            for l in _bill_ready
                        ]
                        _repair_missing_blank_allocations(_repair_line_ids)

                        # Compute base amount + tax from per-line GST
                        _t_base = 0.0
                        _t_tax  = 0.0
                        for _l in _bill_ready:
                            _bt  = float(_l.get("billing_total") or 0)
                            _gst = float(_l.get("gst_percent") or 0)
                            if _gst:
                                _base = round(_bt / (1 + _gst / 100), 2)
                                _t_base += _base
                                _t_tax  += round(_bt - _base, 2)
                            else:
                                _t_base += _bt

                        _challan_no_out = create_challan(
                            party_id     = str(order.get("party_id") or ""),
                            order_ids    = [str(order.get("id") or order_id)],
                            total_amount = round(_t_base, 2),
                            total_tax    = round(_t_tax,  2),
                            remarks      = _remarks.strip() or "",
                            line_ids     = [str(l.get("line_id") or l.get("id", ""))
                                            for l in _bill_ready],
                        )
                        if _challan_no_out:
                            st.success(
                                f"✅ Challan {_challan_no_out} created · "
                                f"₹{_bill_total:,.2f}"
                            )
                            # Direct navigation button to Challan Dashboard
                            if st.button("📋 View in Challan Dashboard →",
                                         key=f"go_chal_dash_{order_id}",
                                         use_container_width=True):
                                st.session_state["_sidebar_page"]  = "🧾  Challan & Invoice"
                                st.session_state["active_module"]  = "Challan & Invoice Dashboard"
                                st.session_state["bo_show_billing_tab"] = False
                                st.rerun()
                            if _do_inv:
                                try:
                                    from modules.billing.challan_invoice_manager import (
                                        create_invoice,
                                    )
                                    # Fetch the newly created challan's UUID
                                    _ch_rows = _rq_ch(
                                        "SELECT id::text AS challan_id FROM challans "
                                        "WHERE challan_no = %(n)s LIMIT 1",
                                        {"n": _challan_no_out},
                                    )
                                    _ch_id = (_ch_rows[0]["challan_id"]
                                              if _ch_rows else None)
                                    if _ch_id:
                                        _inv_no = create_invoice(
                                            challan_id   = _ch_id,
                                            party_id     = str(order.get("party_id") or ""),
                                            order_ids    = [str(order.get("id") or order_id)],
                                            total_amount = round(_t_base, 2),
                                            total_tax    = round(_t_tax,  2),
                                            remarks      = _remarks.strip() or "",
                                        )
                                        if _inv_no:
                                            st.success(f"📄 Invoice {_inv_no} created")
                                        else:
                                            st.warning("Challan created — invoice creation failed, retry from Challan Dashboard")
                                    else:
                                        st.warning("Challan created — could not auto-create invoice (challan not found in DB)")
                                except Exception as _ie:
                                    st.warning(f"Invoice error: {_ie}")
                            import time; time.sleep(0.4)
                            # Keep billing tab active on next render
                            st.session_state["bo_show_billing_tab"] = True
                            st.rerun()
                        else:
                            st.error("Challan creation failed — check logs")
                    except Exception as _ce:
                        _err_msg = str(_ce)
                        st.error(f"❌ Billing error: {_err_msg}")
                        # Show detailed reason if it's a readiness block
                        if "not ready" in _err_msg.lower() or "stage:" in _err_msg.lower():
                            st.info(
                                "💡 To fix: go to Production → In-house Lab → "
                                "advance the job to **Ready to Bill**, then return here."
                            )
                        elif "already has an active challan" in _err_msg:
                            st.info("💡 Refresh the page — a challan may already exist for this order.")

        # ── Existing Challans + Payment Collection ────────────────────────
        # billing_status_ui renders: challan list, payment status, 
        # Convert to Invoice button, payment balance check
        st.markdown("---")
        try:
            from modules.backoffice.billing_status_ui import render_billing_status_panel
            render_billing_status_panel(order, all_lines)
        except ImportError:
            st.info(
                "💡 Challan management available in the **💳 Billing Gate** tab. "
                "Use that tab to view existing challans, collect payment, and convert to invoice."
            )
        except Exception as _bse:
            st.warning(f"Billing status panel error: {_bse}")

        # ── Pricing debug toggle ──────────────────────────────────────────
        st.markdown("---")
        if st.checkbox("🔍 Debug Pricing (Advanced)", key=f"debug_pricing_{order_id}"):
            st.markdown("#### Line Item Debug")
            for _di, line in enumerate(all_lines, 1):
                with st.expander(f"Line {_di}: {line.get('product_name', 'N/A')}", expanded=False):
                    st.json({
                        "product_id":           line.get("product_id", "N/A"),
                        "price_source":         line.get("price_source", "unknown"),
                        "eye_side":             line.get("eye_side", "N/A"),
                        "billing_qty":          line.get("billing_qty", 0),
                        "allocated_qty":        line.get("allocated_qty", 0),
                        "pending_qty":          max(0, int(line.get("billing_qty") or 0) -
                                                     int(line.get("allocated_qty") or 0)),
                        "unit_price":           line.get("unit_price", 0),
                        "discount_amount":      line.get("discount_amount", 0),
                        "discount_percent":     line.get("discount_percent", 0),
                        "billing_total":        line.get("billing_total", 0),
                        "manufacturing_route":  line.get("manufacturing_route", "N/A"),
                        "batch_allocation":     line.get("batch_allocation", []),
                        "pricing_locked":       line.get("pricing_locked", False),
                    }, expanded=False)
    
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
