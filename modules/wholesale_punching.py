"""
Wholesale Punching Module - CLEAN VERSION

Flow:
Party → Power → Product → Cart → Submit
"""

import streamlit as st
try:
    from modules.ophthalmic_billing import (
        render_ophthalmic_selector as _oph_selector,
        ophthalmic_unit_price      as _oph_price,
        ophthalmic_display_name    as _oph_name,
    )
    _HAS_OPH_BILLING = True
except Exception:
    _oph_selector = None
    _oph_price    = None
    _oph_name     = None
    _HAS_OPH_BILLING = False
try:
    from modules.power_intelligence_ui import (
        render_power_intelligence_panel as _pi_panel,
        render_range_check as _rc,
    )
    from modules.power_intelligence import is_colour_product
except Exception:
    _pi_panel = None
    _rc = None
    def is_colour_product(x): return False
try:
    from modules.core.shared_punching import render_boxing_params as _shared_render_boxing_params
    _HAS_SHARED_BOXING = True
except ImportError:
    _HAS_SHARED_BOXING = False
try:
    from modules.core.kb_helpers import autofocus_scan, enter_to_submit, kb_legend
    _KB = True
except ImportError:
    _KB = False
    def autofocus_scan(*a, **k): pass
    def enter_to_submit(): pass
    def kb_legend(*a, **k): pass
import pandas as pd
import datetime
import uuid
import numpy as np
import copy

from typing import Dict, List, Optional, Tuple

from modules.db.order_repository import save_order
from modules.session_manager import reset_after_submit
from modules.quantity_engine import QuantityEngine
try:
    from modules.core.eye_side_normalizer import normalize_eye_side
except ImportError:
    def normalize_eye_side(v, **_):  # noqa: F811
        if not v:
            return "B"
        _k = str(v).strip().upper()
        if _k in ("R", "RIGHT", "RE"):   return "R"
        if _k in ("L", "LEFT", "LE"):    return "L"
        if _k in ("S", "SVC", "SERVICE", "SERVICES"): return "SERVICE"
        return "B"

# ✅ Import Order Header (IMPORTANT)
from modules.ui_order_header import render_order_header
from modules.ui_helpers import format_quantity_display

# ✅ Centralized price resolver — picks the right DB column per order type
from modules.core.price_qty_governor import (
    resolve_price            as resolve_price_for_order_type,  # drop-in alias
    normalize_to_pcs_price,
    normalize_box_total,     # BOX-safe total: avoids rounding via box_price x boxes
    is_box_product,
    normalize_qty,
    reverse_qty,
    get_pcs_price,
    compute_line_gst,
    check_sync,
    PAIR_TO_PCS,
)

# ============================================================================
# TAX ENGINE INTEGRATION — single source of truth for GST on every line
# ============================================================================

def _stamp_line_tax(line: dict, order_type: str = "WHOLESALE") -> dict:
    """
    Pass one cart line through tax_engine.apply_taxes().
    Writes gst_amount, gst_percent_used, tax_hash directly onto the line dict.
    IMPORTANT: uses direct assignment (not setdefault) so it overwrites the
    initialised 0.0 value that every line is built with.
    """
    # Defensive hydration: fetch gst_percent from DB if absent/zero
    if not float(line.get("gst_percent") or 0):
        pid = line.get("product_id")
        if pid:
            try:
                _df = execute_query(
                    "SELECT gst_percent FROM products WHERE id = %s LIMIT 1",
                    "stamp_gst_hydrate",
                    params=(str(pid),),
                )
                if _df is not None and not _df.empty:
                    _fetched = float(_df.iloc[0].get("gst_percent") or 0)
                    if _fetched:
                        line["gst_percent"] = _fetched
            except Exception:
                pass

    try:
        from modules.pricing.tax_engine import apply_taxes
        pseudo_order = {
            "order_type": order_type,
            "lines": [line],
            "net_value": float(line.get("total_price") or 0),
        }
        apply_taxes(pseudo_order)
    except Exception:
        # Fallback: direct assignment so 0.0 initialiser is always overwritten
        pct   = float(line.get("gst_percent") or 0)
        total = float(line.get("total_price") or 0)
        line["gst_amount"] = round(total * pct / 100, 2)
    return line




def _stamp_cart_line_discount(line: dict) -> None:
    """
    Apply discount engine + re-stamp GST on net price for a single cart line.
    Called immediately after a line is added to cart so UI shows correct price.
    Zero-risk: any failure leaves the line untouched.
    """
    try:
        from modules.pricing.discount_engine import apply_discounts
        _pn = str(st.session_state.get("retail_patient_name") or "").strip()
        _pi = ""
        if _pn:
            try:
                from modules.sql_adapter import run_query as _rqd
                _rr = _rqd(
                    "SELECT id::text AS id FROM parties "
                    "WHERE party_name=%s AND COALESCE(is_active,TRUE)=TRUE LIMIT 1",
                    (_pn,)
                ) or []
                if _rr:
                    _pi = str(_rr[0].get("id") or "")
            except Exception:
                pass
        _pc = str(st.session_state.get("_ws_promo_code") or "").strip()
        if _pc:
            line["promo_code"] = _pc
        apply_discounts([line], party_id=_pi, order_type="WHOLESALE")
        # Re-stamp GST on net (gross - discount) so tax is correct from add-to-cart
        if float(line.get("discount_amount") or 0) > 0:
            _gross = float(line.get("total_price") or 0)
            line["billing_total"] = round(
                _gross - float(line.get("discount_amount", 0)), 2
            )
            _stamp_line_tax(line, "WHOLESALE")
    except Exception:
        pass

def _hydrate_product_gst(product_row: dict) -> dict:
    """
    Fetch gst_percent from products and prices (selling_price, mrp) from
    inventory_stock for this product_id.

    Prices live in inventory_stock (not products table) — we read the
    latest active batch MAX price so the governor always gets real values.
    """
    pid = product_row.get("product_id")
    if not pid:
        return product_row

    # Cache in session_state to avoid repeated DB queries per session
    cache_key = f"_product_cache_{pid}"
    if cache_key in st.session_state:
        cached = st.session_state[cache_key]
        # Merge cached values into product_row without overwriting existing
        for k, v in cached.items():
            if k not in product_row or not product_row.get(k):
                product_row[k] = v
        return product_row

    try:
        df = execute_query(
            """
            SELECT
                p.gst_percent,
                COALESCE(i.selling_price, 0) AS selling_price,
                COALESCE(i.mrp, 0)           AS mrp,
                COALESCE(i.purchase_rate, 0) AS purchase_rate
            FROM products p
            LEFT JOIN LATERAL (
                SELECT selling_price, mrp, purchase_rate
                FROM inventory_stock
                WHERE product_id = p.id
                  AND COALESCE(is_active, TRUE) = TRUE
                  AND (selling_price > 0 OR mrp > 0)
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
            ) i ON true
            WHERE p.id = %s
            LIMIT 1
            """,
            "product_gst_hydrate",
            params=(str(pid),),
        )
        if df is not None and not df.empty:
            row = df.iloc[0]
            product_row["gst_percent"] = float(row.get("gst_percent") or 0)
            product_row.setdefault("discount_percent", 0.0)
            # Only update price fields if they are missing/zero in the row
            # (preserves batch-level prices when already present)
            if not float(product_row.get("selling_price") or 0):
                product_row["selling_price"] = float(row.get("selling_price") or 0)
            if not float(product_row.get("mrp") or 0):
                product_row["mrp"] = float(row.get("mrp") or 0)
            if not float(product_row.get("purchase_rate") or 0):
                product_row["purchase_rate"] = float(row.get("purchase_rate") or 0)
            # Cache the fetched data
            st.session_state[cache_key] = {
                "gst_percent": product_row["gst_percent"],
                "discount_percent": product_row.get("discount_percent", 0.0),
                "selling_price": product_row["selling_price"],
                "mrp": product_row["mrp"],
                "purchase_rate": product_row["purchase_rate"],
            }
    except Exception:
        pass

    return product_row

    # Already hydrated this render cycle — skip DB round-trip
    if product_row.get("_gst_hydrated"):
        return product_row

    try:
        df = execute_query(
            """
            SELECT
                p.gst_percent,
                COALESCE(i.selling_price, 0) AS selling_price,
                COALESCE(i.mrp, 0)           AS mrp,
                COALESCE(i.purchase_rate, 0) AS purchase_rate
            FROM products p
            LEFT JOIN LATERAL (
                SELECT selling_price, mrp, purchase_rate
                FROM inventory_stock
                WHERE product_id = p.id
                  AND COALESCE(is_active, TRUE) = TRUE
                  AND (selling_price > 0 OR mrp > 0)
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
            ) i ON true
            WHERE p.id = %s
            LIMIT 1
            """,
            "product_gst_hydrate",
            params=(str(pid),),
        )
        if df is not None and not df.empty:
            row = df.iloc[0]
            product_row["gst_percent"] = float(row.get("gst_percent") or 0)
            product_row.setdefault("discount_percent", 0.0)
            # Only update price fields if they are missing/zero in the row
            # (preserves batch-level prices when already present)
            if not float(product_row.get("selling_price") or 0):
                product_row["selling_price"] = float(row.get("selling_price") or 0)
            if not float(product_row.get("mrp") or 0):
                product_row["mrp"] = float(row.get("mrp") or 0)
            if not float(product_row.get("purchase_rate") or 0):
                product_row["purchase_rate"] = float(row.get("purchase_rate") or 0)
    except Exception:
        pass  # hydration is best-effort

    product_row["_gst_hydrated"] = True
    return product_row




# Import required modules (NO PATIENT FUNCTIONS)

from modules.ui_product_selector import render_product_selector

from modules.sql_adapter import (
    read_product_batch,
    get_power_wise_stock,
    execute_query,
)

from modules.batch_manager import (
    check_stock_availability,
    get_batches_fifo,
    allocate_batches_fifo,
    create_allocation_record,
    get_stock_display,
)
from modules.loaders.ophthalmic_adapter import (
    check_ophthalmic_availability,
    get_ophthalmic_for_punching,
    deduct_ophthalmic_stock,
    IN_STOCK as OPHL_IN_STOCK,
    RX_ORDER as OPHL_RX_ORDER,
)

from modules.core.order_engine import convert_cart_to_order
from modules.validation_gateway import validate_before_submit

# ============================================================================
# SESSION STATE INITIALIZATION (WHOLESALE VERSION)
# ============================================================================

def initialize_session_state():
    """Initialize required session state variables for WHOLESALE"""

    defaults = {

        # 🔹 Party Info (reusing retail keys to avoid engine changes)
        'retail_patient_name': '',
        # ── Wholesale end-customer fields (for authenticity card) ──
        'ws_end_customer_name':   '',
        'ws_end_customer_mobile': '',
        'ws_end_customer_order_no': '',
        'retail_case_no': '',

        # 🔹 Power (NO old Rx in wholesale)
        'retail_new_rx_r': {},
        'retail_new_rx_l': {},

        # 🔹 Cart & Product
        'retail_order_lines': [],
        'retail_selected_product': None,
        'retail_current_allocation': None,
        'retail_show_batch_editor': False,
        'retail_pending_eyes': [],

        # 🔹 Quantity Engine
        'retail_final_qty_R': 0,
        'retail_final_qty_L': 0,

        # 🔹 Provisional Order
        'retail_provisional_order_id': None,
        'retail_provisional_order_created_at': None,

        # 🔹 Last order snapshot
        'last_order_snapshot': [],

        # 🔹 Lens Parameters
        'retail_lens_params': {
            'frame_type': '',
            'thickness': '',
            'tinted': '',
            'corridor': '',
            'diameter': '',
            'fitting_height': '',
            'instructions': '',
        },

        # 🔹 Boxing Parameters
        'retail_boxing_params': {
            'a_box': None,
            'b_box': None,
            'ed': None,
            'ed_axis': None,
            'dbl': None,
            'r_pd': None,
            'l_pd': None,
            'ipd': None,
            'fitting_ht_r': None,
            'fitting_ht_l': None,
            'panto': None,
            'tilt': None,
            'bvd': None,
        },
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # 🔹 Garbage cleanup (keep quantity engine clean)
    for k in list(st.session_state.keys()):
        if k.startswith("retail_qe_be_") and k not in [
            "retail_qe_be_R_box",
            "retail_qe_be_R_pcs",
            "retail_qe_be_R_pair",
            "retail_qe_be_L_box",
            "retail_qe_be_L_pcs",
            "retail_qe_be_L_pair",
        ]:
            del st.session_state[k]


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _get_cached_stock_availability(pid, sph, cyl, axis, add_power, eye_side, required_qty=1, coating=None):
    """Cache stock availability checks in session_state to reduce DB round-trips."""
    cache_key = f"_stock_cache_{pid}_{str(sph)}_{str(cyl)}_{str(axis)}_{str(add_power)}_{eye_side}_{required_qty}_{coating or ''}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    result = check_stock_availability(pid, sph=sph, cyl=cyl, axis=axis, add_power=add_power,
                                      eye_side=eye_side, required_qty=required_qty, coating=coating)
    st.session_state[cache_key] = result
    return result

# is_box_product, normalize_to_pcs_price imported from modules.core.price_qty_governor

def clear_allocation_state():
    """Clear allocation-related session state but preserve pending eyes"""
    st.session_state.retail_current_allocation = None
    st.session_state.retail_show_batch_editor = False
    st.session_state.pop("_alloc_lock", None)
    # Clear stock availability cache to force fresh queries
    for _k in list(st.session_state.keys()):
        if _k.startswith("_stock_cache_"):
            del st.session_state[_k]
    # DON'T clear pending_eyes here - it's managed in finalize flow

# ============================================================================
# WHOLESALE RESET ENGINE
# ============================================================================

def reset_from_stage(stage: str):
    """
    Smart reset engine - Wholesale version
    Stages: PARTY, RX, PRODUCT, ALL
    """

    # ---------------- PARTY ----------------
    if stage in ["PARTY", "ALL"]:
        st.session_state.retail_patient_name = ""
        st.session_state.retail_case_no = ""
        st.session_state.wh_roletype = None
        st.session_state.wh_order_date = None

    # ---------------- RX ----------------
    if stage in ["RX", "PARTY", "ALL"]:
        st.session_state.retail_new_rx_r = {}
        st.session_state.retail_new_rx_l = {}
        # Also clear widget keys so number_input shows 0, not stale values
        for _eye in ("R", "L"):
            for _field in ("sph", "cyl", "axis", "add"):
                st.session_state.pop(f"wh_{_field}_{_eye}", None)

        # Remove retail-only flags if present
        st.session_state.pop("use_same_power_R", None)
        st.session_state.pop("use_same_power_L", None)

    # ---------------- PRODUCT ----------------
    if stage in ["PRODUCT", "RX", "PARTY", "ALL"]:
        st.session_state.retail_selected_product = None

        st.session_state.retail_final_qty_R = 0
        st.session_state.retail_final_qty_L = 0

        st.session_state.retail_current_allocation = None
        st.session_state.retail_show_batch_editor = False
        st.session_state.retail_pending_eyes = []

        st.session_state["reset_product_selector"] = True

    # ---------------- CART ----------------
    if stage in ["ALL"]:
        st.session_state.retail_order_lines = []

        st.session_state.retail_provisional_order_id = None
        st.session_state.retail_provisional_order_created_at = None

    # ---------------- PARAMS ----------------
    if stage in ["ALL"]:
        st.session_state.retail_lens_params = {
            'frame_type': '',
            'thickness': '',
            'tinted': '',
            'corridor': '',
            'diameter': '',
            'fitting_height': '',
            'instructions': '',
        }

        st.session_state.retail_boxing_params = {
            'a_box': None,
            'b_box': None,
            'ed': None,
            'ed_axis': None,
            'dbl': None,
            'r_pd': None,
            'l_pd': None,
            'ipd': None,
            'fitting_ht_r': None,
            'fitting_ht_l': None,
            'panto': None,
            'tilt': None,
            'bvd': None,
        }


# ============================================================================
# DUPLICATE ORDER (UNCHANGED - SAFE FOR WHOLESALE)
# ============================================================================

def duplicate_current_order():
    """Duplicate current order"""
    if not st.session_state.retail_order_lines:
        return

    st.session_state.retail_order_lines = copy.deepcopy(
        st.session_state.retail_order_lines
    )

    st.session_state.retail_provisional_order_id = None
    st.session_state.retail_provisional_order_created_at = None

# ============================================================================
# WHOLESALE CONTROL BAR
# ============================================================================

def _render_post_save_actions(order_no, party, mobile, total, order_type, advance=0.0, delivery="", on_account=True, lines=None):
    try:
        from modules.post_save_actions import render_post_save_actions
        render_post_save_actions(order_no, party, mobile, total, order_type, advance, delivery, on_account=on_account, lines=lines or [])
    except Exception as _psa_ex:
        import traceback as _tb
        st.warning(f"⚠️ Post-save panel error: {_psa_ex}")
        st.code(_tb.format_exc(), language="text")


def render_wholesale_controls():
    """
    Global control bar for wholesale order management
    """

    st.markdown(
        "<div style='display:flex;align-items:center;gap:8px;margin:4px 0 6px'>"
        "<span style='background:#374151;color:#e5e7eb;font-size:0.68rem;font-weight:800;"
        "padding:2px 10px;border-radius:20px;letter-spacing:.05em'>⚙️ ORDER CONTROLS</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)

    # Reset Party
    with c1:
        if st.button("🔄 Reset Party", width='stretch'):
            reset_from_stage("PARTY")
            st.rerun()

    # Reset Power
    with c2:
        if st.button("🔄 Reset Power", width='stretch'):
            reset_from_stage("RX")
            # Clear power widget keys so they re-seed to zero
            for _eye in ('R', 'L'):
                for _wk in (f'wh_sph_{_eye}', f'wh_cyl_{_eye}', f'wh_axis_{_eye}', f'wh_add_{_eye}', f'_wh_rx_seeded_{_eye}'):
                    st.session_state.pop(_wk, None)
            st.rerun()

    # Reset Product
    with c3:
        if st.button("🔄 Reset Product", width='stretch'):
            reset_from_stage("PRODUCT")
            st.rerun()

    # New Order
    with c4:
        if st.button("🧹 New Order", width='stretch'):
            reset_from_stage("ALL")
            # Clear power widget keys
            for _eye in ('R', 'L'):
                for _wk in (f'wh_sph_{_eye}', f'wh_cyl_{_eye}', f'wh_axis_{_eye}', f'wh_add_{_eye}', f'_wh_rx_seeded_{_eye}'):
                    st.session_state.pop(_wk, None)
            # Clear party/patient selection widget keys
            for _pk in ('ws_party_select', 'ws_party_dropdown', 'ws_party_name_input',
                        'retail_patient_name', 'retail_patient_mobile', 'retail_patient_id',
                        'retail_case_no', '_ws_party_confirmed', '_ws_party_id',
                        'ws_ec_name_input', 'ws_ec_mobile_input', 'ws_ec_ono_input'):
                st.session_state.pop(_pk, None)
            # Clear end-customer state
            st.session_state['ws_end_customer_name']     = ''
            st.session_state['ws_end_customer_mobile']   = ''
            st.session_state['ws_end_customer_order_no'] = ''
            st.rerun()


# ============================================================================
# PROVISIONAL ORDER (UNCHANGED)
# ============================================================================

def create_provisional_order():
    """Create a new provisional order"""
    if not st.session_state.retail_provisional_order_id:
        provisional_id = f"PO-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:6]}"
        st.session_state.retail_provisional_order_id = provisional_id
        st.session_state.retail_provisional_order_created_at = datetime.datetime.now().isoformat()
        return provisional_id

    return st.session_state.retail_provisional_order_id


def clear_provisional_order():
    """Clear provisional order after confirmation"""

    st.session_state.retail_provisional_order_id = None
    st.session_state.retail_provisional_order_created_at = None
    st.session_state.retail_order_lines = []

    reset_from_stage("ALL")

# ============================================================================
# POWER ENTRY (WHOLESALE VERSION)
# ============================================================================

def render_power_entry():
    """Wholesale Power Entry - no patient dependency"""

    st.markdown(
        "<div style='display:flex;align-items:center;gap:8px;margin:4px 0 2px'>"
        "<span style='background:#6366f1;color:#fff;font-size:0.68rem;font-weight:800;"
        "padding:2px 10px;border-radius:20px;letter-spacing:.05em'>👓 POWER ENTRY</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown("### 👁️ RIGHT EYE")
    render_eye_power_section("R")

    st.markdown("---")

    st.markdown("### 👁️ LEFT EYE")
    render_eye_power_section("L")


def render_eye_power_section(eye: str):

    new_rx_key = f"retail_new_rx_{eye.lower()}"
    new_rx = st.session_state.get(new_rx_key, {})
    _k_sph  = f"wh_sph_{eye}"
    _k_cyl  = f"wh_cyl_{eye}"
    _k_axis = f"wh_axis_{eye}"
    _k_add  = f"wh_add_{eye}"

    # ── Seed widget keys from prefilled rx BEFORE widget renders ─────────
    # Seed when: widget key missing (first load) OR new_rx changed since last seed
    # The _seed_hash tracks which rx was last seeded — if rx changes (new patient/edit),
    # we must re-seed even though the widget key already exists.
    _seed_hash_key = f"_wh_rx_seeded_{eye}"

    # Only seed widget keys if they don't exist yet (first load or after reset).
    # We intentionally do NOT re-seed on rx value changes here because
    # the user's typed values live in the widget keys — overwriting them
    # on rerun wipes in-progress input (the core bug: hash mismatch on
    # every render triggered a re-seed that cleared SPH/CYL).
    # External prefill (e.g. from patient record) should clear widget keys
    # before calling this function so the "not in state" branch fires.
    if _k_sph not in st.session_state:
        if new_rx:
            # Prefill from existing rx (patient lookup, order edit, etc.)
            st.session_state[_k_sph]  = float(new_rx.get("sph")  or 0.0)
            st.session_state[_k_cyl]  = float(new_rx.get("cyl")  or 0.0)
            st.session_state[_k_axis] = int(  new_rx.get("axis") or 0)
            st.session_state[_k_add]  = float(new_rx.get("add")  or 0.0)
        else:
            # No prefill — blank slate
            st.session_state[_k_sph]  = 0.0
            st.session_state[_k_cyl]  = 0.0
            st.session_state[_k_axis] = 0
            st.session_state[_k_add]  = 0.0
        st.session_state[_seed_hash_key] = str(new_rx)

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        sph = st.number_input("SPH", key=_k_sph,
                              min_value=-20.0, max_value=20.0, step=0.25, format="%.2f")
    with col2:
        cyl = st.number_input("CYL", key=_k_cyl,
                              min_value=-6.0, max_value=6.0, step=0.25, format="%.2f")
    with col3:
        axis = st.number_input("AXIS", key=_k_axis,
                               min_value=0, max_value=180, step=1)
    with col4:
        add_power = st.number_input("ADD", key=_k_add,
                                    min_value=0.0, max_value=3.5, step=0.25, format="%.2f")

    # Only update session_state if values changed (prevents rerun loop)
    _new_rx_val = {"sph": sph, "cyl": cyl, "axis": axis, "add": add_power}
    if st.session_state.get(new_rx_key) != _new_rx_val:
        st.session_state[new_rx_key] = _new_rx_val

# ============================================================
# PAGE EXECUTION (WHOLESALE)
# ============================================================

# ============================================================================
# PRODUCT SELECTION - FROM OLD WORKING VERSION
# ============================================================================

def render_product_selection():
    """Render product selection - matching old working version exactly"""

    # One-time product cache refresh
    if not st.session_state.get("_product_cache_refreshed"):
        st.session_state.retail_selected_product = None
        st.session_state["_product_cache_refreshed"] = True
    
    if st.session_state.get("reset_product_selector"):
        for k in list(st.session_state.keys()):
            if k.startswith("ps_"):
                del st.session_state[k]

        st.session_state["reset_product_selector"] = False

    # Tell ui_product_selector this is a WHOLESALE order
    # so ophthalmic spec prices show WLP not SRP
    st.session_state["_current_order_type"] = "WHOLESALE"

    result = render_product_selector()

    if result:
        # Handle UUID product_id as string
        current_product_id = str(result['product_row']['product_id'])
        
        if st.session_state.retail_selected_product:
            old_product_id = str(st.session_state.retail_selected_product['product_row']['product_id'])
            if current_product_id != old_product_id:
                clear_allocation_state()
                # Reset quantities when product changes
                st.session_state.retail_final_qty_R = 0
                st.session_state.retail_final_qty_L = 0
                
                # Reset lens and boxing params to avoid leakage
                st.session_state.retail_lens_params = {}
                st.session_state.retail_boxing_params = {}

        # Only update if product actually changed — writing every render causes infinite rerun loop
        _new_pid = str(result['product_row']['product_id'])
        _cur = st.session_state.retail_selected_product
        _old_pid = str(_cur['product_row']['product_id']) if _cur else ''
        if _new_pid != _old_pid:
            st.session_state.retail_selected_product = result
        
        # ===============================
        # PRODUCT ADD FLOW (LENS + OTHER)
        # ===============================

        # Lens / Contact Lens Flow (Needs Rx + Batch)
        if result['is_lens'] or result['is_contact']:

            st.markdown("---")
            st.markdown("### ➕ Add Product to Order")
    
            product = _hydrate_product_gst(result['product_row'])
            product_name = product['product_name']

            st.info(f"**Selected:** {product_name} (ID: {current_product_id})")

            # ===============================
            # POWER-WISE STOCK DISPLAY
            # ===============================

            # Get product id
            pid = current_product_id
            product = _hydrate_product_gst(result['product_row'])

            # Right eye stock
            if st.session_state.get("retail_new_rx_r"):
                rx_r = st.session_state.retail_new_rx_r
                _sph_r = rx_r.get("sph") or 0
                _cyl_r = rx_r.get("cyl") or 0
                _ax_r  = rx_r.get("axis") or 0
                _power_r = f"{_sph_r:+.2f}" if not _cyl_r else f"{_sph_r:+.2f}/{_cyl_r:+.2f}" + (f"×{_ax_r}" if _ax_r else "")

                # Use cached check_stock_availability — product-type-aware (CL/ophthalmic/frame)
                _sel_coat_r = None
                _active_spec_r = (st.session_state.get(f"_oph_spec_{str(current_product_id)}")
                                  or st.session_state.get(f"_oph_spec_ws_{str(current_product_id)[:8]}"))
                if _active_spec_r and _active_spec_r.get("complete"):
                    _sel_coat_r = _active_spec_r.get("coating")
                # Pass coating only when selected — without coating shows combined stock
                _avail_r = _get_cached_stock_availability(
                    pid,
                    sph=rx_r.get("sph"), cyl=rx_r.get("cyl"),
                    axis=rx_r.get("axis"), add_power=rx_r.get("add"),
                    eye_side="R", required_qty=1,
                    coating=_sel_coat_r if _sel_coat_r else None,
                )
                # Label shows whether coating is filtered or combined
                _r_coat_label = f" [{_sel_coat_r}]" if _sel_coat_r else " [all coatings]"
                r_qty = int(_avail_r.get("available_qty") or 0)

                if r_qty > 0:
                    r_disp = format_quantity_display(r_qty, product)
                    st.success(f"👁️ RIGHT ({_power_r}){_r_coat_label}: {r_qty} PCS")
                else:
                    _in_range_r = True
                    if _rc:
                        try:
                            _in_range_r = _rc(
                                product_id   = str(current_product_id or ''),
                                product_name = str(product_name or ''),
                                sph  = float(rx_r.get("sph") or 0),
                                cyl  = float(rx_r.get("cyl") or 0),
                                axis = int(rx_r.get("axis") or 0),
                                is_colour = is_colour_product(str(product_name or '')),
                                eye = "RIGHT",
                            )
                        except Exception:
                            _in_range_r = True
                    if _in_range_r:
                        st.warning(f"👁️ RIGHT ({_power_r}): Out of Stock")
                        if _pi_panel:
                            try:
                                _pi_panel(
                                    sph=float(rx_r.get("sph") or 0),
                                    cyl=float(rx_r.get("cyl") or 0),
                                    axis=int(rx_r.get("axis") or 0),
                                    add_power=float(rx_r.get("add") or 0),
                                    selected_product=product_name,
                                    eye="RIGHT",
                                    product_id=str(current_product_id or ""),
                                    is_colour=is_colour_product(str(product_name or "")),
                                )
                            except Exception:
                                pass


            # Left eye stock
            if st.session_state.get("retail_new_rx_l"):
                rx_l = st.session_state.retail_new_rx_l
                _sph_l = rx_l.get("sph") or 0
                _cyl_l = rx_l.get("cyl") or 0
                _ax_l  = rx_l.get("axis") or 0
                _power_l = f"{_sph_l:+.2f}" if not _cyl_l else f"{_sph_l:+.2f}/{_cyl_l:+.2f}" + (f"×{_ax_l}" if _ax_l else "")

                _active_spec_l = (st.session_state.get(f"_oph_spec_{str(current_product_id)}")
                                      or st.session_state.get(f"_oph_spec_ws_{str(current_product_id)[:8]}"))
                _sel_coat_l = _active_spec_l.get("coating") if (_active_spec_l and _active_spec_l.get("complete")) else None
                _avail_l = _get_cached_stock_availability(
                    pid,
                    sph=rx_l.get("sph"), cyl=rx_l.get("cyl"),
                    axis=rx_l.get("axis"), add_power=rx_l.get("add"),
                    eye_side="L", required_qty=1,
                    coating=_sel_coat_l if _sel_coat_l else None,
                )
                _l_coat_label = f" [{_sel_coat_l}]" if _sel_coat_l else " [all coatings]"
                l_qty = int(_avail_l.get("available_qty") or 0)

                if l_qty > 0:
                    l_disp = format_quantity_display(l_qty, product)
                    st.success(f"👁️ LEFT ({_power_l}){_l_coat_label}: {l_qty} PCS")
                else:
                    _in_range_l = True
                    if _rc:
                        try:
                            _in_range_l = _rc(
                                product_id   = str(current_product_id or ''),
                                product_name = str(product_name or ''),
                                sph  = float(rx_l.get("sph") or 0),
                                cyl  = float(rx_l.get("cyl") or 0),
                                axis = int(rx_l.get("axis") or 0),
                                is_colour = is_colour_product(str(product_name or '')),
                                eye = "LEFT",
                            )
                        except Exception:
                            _in_range_l = True
                    if _in_range_l:
                        st.warning(f"👁️ LEFT ({_power_l}): Out of Stock")
                        if _pi_panel:
                            try:
                                _pi_panel(
                                    sph=float(rx_l.get("sph") or 0),
                                    cyl=float(rx_l.get("cyl") or 0),
                                    axis=int(rx_l.get("axis") or 0),
                                    add_power=float(rx_l.get("add") or 0),
                                    selected_product=product_name,
                                    eye="LEFT",
                                    product_id=str(current_product_id or ""),
                                    is_colour=is_colour_product(str(product_name or "")),
                                )
                            except Exception:
                                pass

            # Detect available eyes from saved power
            available_eyes = []

            if st.session_state.retail_new_rx_r:
                available_eyes.append('R')

            if st.session_state.retail_new_rx_l:
                available_eyes.append('L')
    
            # No power entered yet
            if not available_eyes:
                st.warning("⚠️ Please save power for Right/Left eye first")
                return
    
            # Auto-detect eye mode (old behavior)
            has_r = bool(st.session_state.retail_new_rx_r)
            has_l = bool(st.session_state.retail_new_rx_l)

            if has_r and has_l:
                eye_mode = "BOTH"
                st.info("👁️ Both eyes detected → R then L")

            elif has_r:
                eye_mode = "R"
                st.info("👁️ Only RIGHT eye detected")

            elif has_l:
                eye_mode = "L"
                st.info("👁️ Only LEFT eye detected")

            else:
                st.warning("⚠️ Please save power first")
                return
            
            # ===============================
            # Quantity Engine (R/L)
            # ===============================
            st.markdown("---")
            st.markdown("#### 📊 Enter Quantity for Each Eye")
            
            # Initialize QuantityEngine
            # Normalize product for QuantityEngine
            qe_product = dict(product)

            # If box_size > 1 → treat as BOX product
            if int(qe_product.get("box_size", 0) or 0) > 1:

                qe_product["unit"] = "BOX"

                # Safe normalization for PostgreSQL 't'/'f' values
                val = str(qe_product.get("allow_loose", "")).lower()
                qe_product["allow_loose"] = val in ["true", "t", "1", "yes"]


            engine = QuantityEngine(qe_product)

            schema = engine.get_ui_schema()
            
            # Show mode info
            st.info(f"📦 Billing Mode: **{schema['label']}**")
            
            def render_eye_qty(eye):
                """Render quantity inputs for one eye using QuantityEngine"""
                st.markdown(f"##### 👁️ {eye} EYE Quantity")
                
                user_input = {}
                cols = st.columns(3)
                idx = 0


                # ===============================
                # AUTO DEFAULT QTY
                # ===============================

                # Preserve previous values on rerun
                default_pair = st.session_state.get(f"retail_qe_ps_{eye}_pair", 0.0)
                default_pcs = st.session_state.get(f"retail_qe_ps_{eye}_pcs", 0)
                default_box = st.session_state.get(f"retail_qe_ps_{eye}_box", 0)

                # Only set smart defaults if no previous values exist
                if default_pair == 0.0 and default_pcs == 0 and default_box == 0:
                    # If both eyes → auto half pair
                    has_both = (
                        st.session_state.get("retail_new_rx_r")
                        and st.session_state.get("retail_new_rx_l")
                    )

                    # PAIR products
                    if engine.mode in ["PAIR_ONLY", "PAIR_FLEX"]:

                        if has_both:
                            default_pair = 0.5   # R = 0.5, L = 0.5
                        else:
                            default_pair = 1.0   # Single eye → full pair


                    # PCS / NO / FLEX
                    elif engine.mode in ["PCS_ONLY", "NO_ONLY"]:

                        default_pcs = 1
                        default_box = 0

                    elif engine.mode == "BOX_ONLY":

                        default_box = 1
                        default_pcs = 0

                    elif engine.mode == "FLEX":

                        default_box = 1
                        default_pcs = 0

                
                if schema["box"]:
                    with cols[idx]:
                        user_input["box"] = st.number_input(
                            "Qty (BOX)",
                            min_value=0,
                            max_value=100,
                            value=default_box,
                            step=1,
                            key=f"retail_qe_ps_{eye}_box"
                        )
                    idx += 1


                if schema["pcs"]:
                    with cols[idx]:
                        user_input["pcs"] = st.number_input(
                            "Qty (PCS)",
                            min_value=0,
                            max_value=1000,
                            value=default_pcs,
                            step=1,
                            key=f"retail_qe_ps_{eye}_pcs"
                        )
                    idx += 1

                if schema["pair"]:
                    with cols[idx]:
                        user_input["pair"] = st.number_input(
                            "Qty (PAIR)",
                            min_value=0.0,
                            max_value=100.0,
                            value=default_pair,
                            step=schema.get("pair_step", 1),
                            format=schema.get("pair_format", "%.0f"),
                            key=f"retail_qe_ps_{eye}_pair"
                        )

                    idx += 1


                # Process with QuantityEngine
                result = engine.process(user_input)
                
                # Show validation errors
                if not result["is_valid"]:
                    for e in result["errors"]:
                        st.error(f"⚠️ {eye}: {e}")
                    return 0
                
                # Show final quantity
                final_pcs = result["final_pcs"]
                
                if final_pcs > 0:
                    col1, col2 = st.columns(2)
                    with col1:
                        st.success(f"✅ {eye} - Final: **{final_pcs} PCS**")
                    with col2:
                        mode_label = result.get('mode', 'PCS_ONLY')
                        st.caption(f"Mode: {mode_label}")
                
                return final_pcs
            
            # RIGHT EYE
            if has_r:
                qty_r = render_eye_qty("R")
                if st.session_state.get("retail_final_qty_R") != qty_r:
                    st.session_state.retail_final_qty_R = qty_r
            
            # LEFT EYE
            if has_l:
                st.markdown("---")
                qty_l = render_eye_qty("L")
                if st.session_state.get("retail_final_qty_L") != qty_l:
                    st.session_state.retail_final_qty_L = qty_l
            
            st.markdown("---")

            # Block Add if Zero
            block_add = False
            
            if has_r and st.session_state.retail_final_qty_R <= 0:
                st.warning("⚠️ Enter RIGHT eye quantity")
                block_add = True
            
            if has_l and st.session_state.retail_final_qty_L <= 0:
                st.warning("⚠️ Enter LEFT eye quantity")
                block_add = True

            col1, col2 = st.columns([3, 1])

            with col2:
                st.write("")
                st.write("")

                enter_to_submit()
                if st.button("➕ Add to Cart  [Enter]", type="primary", width='stretch', disabled=block_add):

                    clear_allocation_state()

                    # Sync qty from widget keys
                    for _sync_eye in ('R', 'L'):
                        _box_k = f"retail_qe_ps_{_sync_eye}_box"
                        _pcs_k = f"retail_qe_ps_{_sync_eye}_pcs"
                        _box_v = int(st.session_state.get(_box_k, 0) or 0)
                        _pcs_v = int(st.session_state.get(_pcs_k, 0) or 0)
                        _prod_box_size = int(st.session_state.get("retail_selected_product", {}).get("product_row", {}).get("box_size") or 1)
                        _total_pcs = _box_v * _prod_box_size + _pcs_v
                        if _total_pcs > 0:
                            if _sync_eye == 'R': st.session_state.retail_final_qty_R = _total_pcs
                            else:                st.session_state.retail_final_qty_L = _total_pcs

                    # ── Ophthalmic fast-path: add both eyes in ONE press ──────────
                    _oph_fastpath_spec = (
                        st.session_state.get(f"_oph_spec_{str(current_product_id)}")
                        or st.session_state.get(f"_oph_spec_ws_{str(current_product_id)[:8]}")
                    )
                    _mg_check = str(product.get("main_group","")).lower()
                    _is_oph_fast = (
                        _oph_fastpath_spec and _oph_fastpath_spec.get("complete")
                        and any(k in _mg_check for k in ["ophthalmic","progressive","sv rx","sv stock",
                                                          "single vision","bifocal","reading","spectacle"])
                    )

                    if _is_oph_fast and _HAS_OPH_BILLING:
                        provisional_id = create_provisional_order()
                        _fp  = _oph_fastpath_spec.get("price", {})
                        _pair_sell = float(_fp.get("selling") or _fp.get("wlp") or 0)
                        _pair_mrp  = float(_fp.get("srp") or _pair_sell)
                        _unit_p    = round(_pair_sell / 2, 2)  # per lens
                        _prod_c    = dict(product)
                        _prod_c["selling_price"] = _unit_p
                        _prod_c["mrp"]           = round(_pair_mrp / 2, 2)
                        _prod_c["box_size"]      = 1
                        _pname_oph = (_oph_name(str(product.get("product_name","")), _oph_fastpath_spec)
                                      if _oph_name else str(product.get("product_name","")))
                        _eyes_to_add = ['R','L'] if eye_mode == "BOTH" else [eye_mode]
                        _rx_map = {
                            'R': st.session_state.get("wholesale_rx_r") or {},
                            'L': st.session_state.get("wholesale_rx_l") or {},
                        }
                        _qty_map = {
                            'R': int(st.session_state.get("retail_final_qty_R") or 1),
                            'L': int(st.session_state.get("retail_final_qty_L") or 1),
                        }
                        for _eye in _eyes_to_add:
                            _rx = _rx_map[_eye]
                            _qty_e = _qty_map[_eye]
                            _line = {
                                'line_id':             str(uuid.uuid4()),
                                'provisional_order_id':provisional_id,
                                'product_id':          str(current_product_id),
                                'product_name':        _pname_oph,
                                'brand':               str(product.get('brand','')),
                                'main_group':          str(product.get('main_group','')),
                                'eye_side':            _eye,
                                'sph':                 _rx.get('sph'),
                                'cyl':                 _rx.get('cyl'),
                                'axis':                _rx.get('axis'),
                                'add_power':           _rx.get('add'),
                                'lens_params':         dict(st.session_state.get('retail_lens_params') or {}),
                                'boxing_params':       dict(st.session_state.get('retail_boxing_params') or {}),
                                'requested_qty':       _qty_e,
                                'billing_qty':         0,
                                'order_qty':           _qty_e,
                                'display_qty':         f"{_qty_e} Pcs",
                                'batch_allocation':    [],
                                'suggested_allocation':[],
                                'unit_price':          _unit_p,
                                'total_price':         round(_unit_p * _qty_e, 2),
                                'unit':                'PCS',
                                'box_size':            1,
                                'gst_percent':         float(product.get('gst_percent') or 0),
                                'gst_amount':          0,
                                'discount_percent':    0.0,
                                'purchase_rate':       round(float(_fp.get("purchase") or 0) / 2, 2),
                                'status':              'Pending',
                                'created_at':          datetime.datetime.now().isoformat(),
                            }
                            _stamp_line_tax(_line, "WHOLESALE")
                            _stamp_cart_line_discount(_line)
                            st.session_state.retail_order_lines.append(_line)
                        clear_allocation_state()
                        st.session_state.retail_pending_eyes = []
                        st.success(f"✅ {', '.join(_eyes_to_add)} added — ₹{_unit_p:,.2f}/lens")
                        st.rerun()

                    else:
                        # Non-ophthalmic: standard batch allocation flow
                        if eye_mode == "BOTH":
                            st.session_state.retail_pending_eyes = ['R', 'L']
                        else:
                            st.session_state.retail_pending_eyes = [eye_mode]
                        st.session_state.retail_show_batch_editor = True
                        st.session_state._retail_finalized_eyes = []
                        st.rerun()


        # -------------------------------------------------
        # OTHER PRODUCTS (Frame / Service / Accessory)
        # -------------------------------------------------
        else:

            st.markdown("---")
            st.markdown("### ➕ Add Item to Order")

            product = _hydrate_product_gst(result['product_row'])
            product_name = product['product_name']

            st.info(f"**Selected:** {product_name} (ID: {current_product_id})")

            qty = st.number_input(
                "Quantity",
                min_value=1,
                step=1,
                value=1,
                key=f"other_qty_{current_product_id}"
            )

            enter_to_submit()
            if st.button("➕ Add Item to Cart  [Enter]", type="primary", width='stretch'):

                provisional_id = create_provisional_order()

                # ✅ Always hydrate from DB first so gst_percent is never 0 by omission
                product = _hydrate_product_gst(product)

                # ✅ For ophthalmic: use spec price; for others: centralized resolver
                _active_oph_spec = (
                    st.session_state.get(f"_oph_spec_{str(product_id)}")
                    or st.session_state.get(f"_oph_spec_ws_{str(product_id)[:8]}")
                )
                if _active_oph_spec and _active_oph_spec.get("complete"):
                    _price_d    = _active_oph_spec.get("price", {})
                    _pair_sell  = float(_price_d.get("selling") or _price_d.get("wlp") or 0)
                    _pair_mrp   = float(_price_d.get("srp") or _pair_sell)
                    unit_price  = round(_pair_sell / 2, 2)   # per lens
                    product     = dict(product)
                    product["selling_price"] = unit_price
                    product["mrp"]           = round(_pair_mrp / 2, 2)
                    product["box_size"]      = 1
                else:
                    unit_price = resolve_price_for_order_type(product, "WHOLESALE")

                # ── Warn if selling_price is missing → fell back to MRP ──────────
                _sp = float(product.get("selling_price") or 0)
                _mp = float(product.get("mrp") or 0)
                if _sp <= 0 and _mp > 0 and unit_price == _mp:
                    _pname_w = str(product.get("product_name","product"))
                    st.warning(
                        f"⚠️ **{_pname_w}** has no trade price (selling_price) set — "
                        f"using MRP ₹{_mp:,.2f} as fallback. "
                        f"Please update the product's selling price in the product master."
                    )

                # Pricing — use centralized normalizer
                # normalize_box_total avoids per-PCS rounding error for BOX products
                # (e.g. 500/6 = 83.33 x 12 = 999.96 vs correct 2 x 500 = 1000)
                pcs_price   = normalize_to_pcs_price(unit_price, product)
                total_price = normalize_box_total(unit_price, qty, product)

                # GST % from product master; tax engine stamps gst_amount below
                gst_pct    = float(product.get('gst_percent') or 0)
                discount   = float(product.get('discount_percent') or 0)

                box_size   = max(1, int(product.get('box_size') or 1))
                display_qty = pcs_to_box_display(qty, box_size)

                line = {
                    'line_id': str(uuid.uuid4()),
                    'provisional_order_id': provisional_id,

                    'product_id': current_product_id,
                    'product_name': product_name,
                    'brand': product.get('brand', ''),
                    'main_group': product.get('main_group', ''),

                    # No power for other items
                    # normalize_eye_side('B') → 'B' (canonical non-eye-specific)
                    # Replacing 'OTHER' which was invisible to backoffice grouping
                    'eye_side': normalize_eye_side('B'),
                    'sph': None,
                    'cyl': None,
                    'axis': None,
                    'add_power': None,

                    'lens_params': {},
                    'boxing_params': {},

                    'requested_qty': qty,
                    'billing_qty': qty,
                    'order_qty': 0,

                    'display_qty': display_qty,

                    'batch_allocation': [],
                    'suggested_allocation': [],

                    'unit_price': pcs_price,
                    'total_price': total_price,
                    'unit': str(product.get('unit') or 'PCS').upper(),
                    'box_size': box_size,

                    'gst_percent': gst_pct,
                    'gst_amount':  0.0,
                    'discount_percent': discount,
                    # Phase 2D: carry purchase_rate (cost) for margin guard in engine
                    'purchase_rate': float(product.get('purchase_rate') or 0),

                    'status': 'Complete',
                    'created_at': datetime.datetime.now().isoformat()
                }

                _stamp_line_tax(line, "WHOLESALE")   # ← single source of truth

                # ── CONSOLIDATE: merge qty if same OTHER product ──
                merged = False
                for existing in st.session_state.retail_order_lines:
                    if (
                        existing['product_id'] == line['product_id']
                        and existing['eye_side'] == 'B'
                    ):
                        existing['requested_qty'] += line['requested_qty']
                        existing['billing_qty'] += line['billing_qty']
                        existing['total_price'] = normalize_box_total(existing['unit_price'], existing['billing_qty'], existing)
                        _stamp_line_tax(existing, "WHOLESALE")   # re-stamp after qty merge
                        existing['display_qty']  = pcs_to_box_display(existing['billing_qty'], int(existing.get('box_size') or 1))
                        merged = True
                        break
                if not merged:
                    _stamp_cart_line_discount(line)
                    st.session_state.retail_order_lines.append(line)

                st.success("✅ Quantity merged!" if merged else "✅ Item added to cart")

                st.rerun()

def prepare_allocation(eye_side: str, qty: int = None):
    """Prepare allocation with batch details"""
    if not st.session_state.retail_selected_product:
        st.error("❌ No product selected")
        return
    
    # Get quantity from session state - QuantityEngine final result
    if eye_side == "R":
        qty = int(st.session_state.get("retail_final_qty_R", 0) or 0)
    elif eye_side == "L":
        qty = int(st.session_state.get("retail_final_qty_L", 0) or 0)
    else:
        qty = 0

    # If qty is still 0 (e.g. first render before QuantityEngine updates state),
    # fall back to 1 PCS so allocation doesn't create a zero-qty line.
    # This only applies to non-service lines; service lines correctly have qty set.
    if qty <= 0:
        qty = 1

    
    product = _hydrate_product_gst(dict(st.session_state.retail_selected_product['product_row']))
    product_id = str(product['product_id'])
    
    if eye_side == 'R':
        rx = st.session_state.retail_new_rx_r
    elif eye_side == 'L':
        rx = st.session_state.retail_new_rx_l
    else:
        rx = {'sph': None, 'cyl': None, 'axis': None, 'add': None}
    
    def get_value_or_none(rx_dict, key):
        val = rx_dict.get(key)
        if val is None or val == '':
            return None
        if key != 'axis' and val == 0:
            return None
        return val
    
    sph_val = get_value_or_none(rx, 'sph')
    cyl_val = get_value_or_none(rx, 'cyl')
    axis_val = get_value_or_none(rx, 'axis')
    add_val = get_value_or_none(rx, 'add')
    
    batches_df = get_batches_fifo(product_id, sph_val, cyl_val, axis_val, add_val, eye_side)
    total_available = batches_df['available_qty'].sum() if not batches_df.empty else 0
    
    total_available = int(total_available or 0)
    qty = int(qty or 0)

    # ── Ophthalmic lens: read spec from ui_product_selector ─────────────────
    # Spec was already selected in product selector (ui_product_selector.py)
    # Key used by ui_product_selector: f"_oph_spec_{product_id}"
    _selected     = st.session_state.get("retail_selected_product", {})
    _product_row  = _selected.get("product_row", _selected)  # unwrap if nested
    _mg = str(
        _product_row.get("main_group")
        or product.get("main_group", "")
    ).lower()
    _is_ophthalmic = (
        _selected.get("is_ophthal")
        or _product_row.get("is_ophthal")
        or any(k in _mg for k in ["ophthalmic","progressive","sv rx","sv stock",
                                   "single vision","bifocal","reading","spectacle"])
        or ((_selected.get("is_lens") or _product_row.get("is_lens"))
            and not (_selected.get("is_contact") or _product_row.get("is_contact"))
            and not any(k in _mg for k in ["contact","solution","frame"]))
    )

    # ONE session key — shared by selector and prepare_allocation
    _spec_sess_key = f"_oph_spec_{str(product_id)}"    # matches ui_product_selector
    _ws_spec_key   = f"_oph_spec_ws_{str(product_id)[:8]}"  # legacy/alternate

    _oph_spec  = (st.session_state.get(_spec_sess_key)
                  or st.session_state.get(_ws_spec_key))
    ophl_avail = None

    if _is_ophthalmic and _HAS_OPH_BILLING and _oph_selector:
        _rx_r = st.session_state.get("wholesale_rx_r") or {}
        _rx_l = st.session_state.get("wholesale_rx_l") or {}

        if eye_side == "R" or not (_oph_spec and _oph_spec.get("complete")):
            # R eye: show selector UI
            _oph_spec = _oph_selector(
                product_id   = str(product_id),
                product_name = str(product.get("product_name","")),
                rx_r         = _rx_r,
                rx_l         = _rx_l,
                order_type   = "WHOLESALE",
                key_prefix   = f"ws_{str(product_id)[:8]}",
            )
            if _oph_spec and _oph_spec.get("complete"):
                # Save under BOTH keys so ui_product_selector and prepare_allocation agree
                st.session_state[_spec_sess_key] = _oph_spec
                st.session_state[_ws_spec_key]   = _oph_spec
        else:
            # L eye: show brief summary, no re-render of selector
            _p = _oph_spec.get("price", {})
            _s = float(_p.get("selling") or _p.get("wlp") or 0)
            st.info(
                f"📐 Same as RIGHT: **{_oph_spec.get('index')}** "
                f"| {_oph_spec.get('coating')}"
                + (f"  ·  ₹{_s:,.0f}/pair" if _s else "")
            )

        if _oph_spec and _oph_spec.get("complete"):
            _qty_field = "qty_r" if eye_side == "R" else "qty_l"
            _stk_field = "stock_r" if eye_side == "R" else "stock_l"
            total_available = int(
                _oph_spec.get(_stk_field, {}).get(_qty_field, 0) or 0)
            if total_available == 0:
                st.info("📋 RX order basis — will be ordered from lab")
        else:
            total_available = 0
    elif _is_ophthalmic:
        ophl_avail = check_ophthalmic_availability(
            product_id, sph=sph_val, cyl=cyl_val,
            axis=axis_val, add_power=add_val, eye_side=eye_side
        )
        if ophl_avail["status"] == "STOCK":
            total_available = ophl_avail["available_qty"]
            if total_available < qty:
                st.warning(f"⚠️ Only {total_available} in stock. Remaining → RX order.")
        elif ophl_avail["status"] == "RX":
            st.info(f"📋 RX order lens — ₹{ophl_avail['selling_price']:.0f} per piece")
            total_available = 0
        else:
            st.warning(ophl_avail["message"])
            total_available = 0
    else:
        if total_available < qty:
            st.warning(f"⚠️ Only {total_available} units in stock. Remaining will be ordered.")
    
    allocation = create_allocation_record(
        product_id=product_id,
        sph=sph_val,
        cyl=cyl_val,
        axis=axis_val,
        add_power=add_val,
        eye_side=eye_side,
        required_qty=qty,
	    pricing_mode="WHOLESALE" 
    )
    
    temp_allocation = {
        'line_id': str(uuid.uuid4()),
        'product_id': product_id,
        'product_name': (
            _oph_name(str(product.get('product_name','')), _active_oph_spec)
            if _oph_name and _active_oph_spec and _active_oph_spec.get("complete")
            else str(product.get('product_name',''))
        ),
        'brand': product.get('brand', ''),
        'main_group': product.get('main_group', ''),
        'eye_side': eye_side,
        'sph': sph_val,
        'cyl': cyl_val,
        'axis': axis_val,
        'add_power': add_val,
        'requested_qty': int(qty),
        'available_qty': total_available,
        'batches': allocation.get('batches', []),
        # ── ophthalmic adapter fields ──────────────────────────────────────
        'lens_item_type': (ophl_avail['status'] if ophl_avail else 'STOCK'),
        'ophl_stock_id':  (ophl_avail.get('stock_id') if ophl_avail else None),
        # ✅ Price resolution order:
        #   1. _oph_spec.price.selling / 2  (ophthalmic spec — pair price ÷ 2)
        #   2. ophl_avail.selling_price     (from inventory_stock — exact power row)
        #   3. resolver WHOLESALE           (selling_price → mrp from product master)
        #   4. product.unit_price           (legacy fallback)
        'unit_price': normalize_to_pcs_price(
            (
                float(_oph_spec.get("price", {}).get("selling") or _oph_spec.get("price", {}).get("wlp") or 0) / 2
                if _oph_spec and _oph_spec.get("complete") else 0
            )
            or (float(ophl_avail.get('selling_price') or 0) if ophl_avail else 0)
            or resolve_price_for_order_type(product, "WHOLESALE")
            or float(product.get('unit_price') or 0),
            product
        ),
        'gst_percent': float(product.get('gst_percent') or 0),   # no 18 default — TaxValidator will flag
    }
    
    st.session_state.retail_current_allocation = temp_allocation
    st.session_state.retail_show_batch_editor = True

# ============================================================================
# LENS PARAMETERS - FROM OLD WORKING VERSION
# ============================================================================

def render_lens_params():
    """Lens Parameters — collapsible expander, rendered after product selection.

    ● Frame Type      : Full / Supra / Rimless          (radio, 3 only)
    ● Thickness       : Regular / Thin / Cartier Thick  (radio)
    ● Tinted          : No / Yes                        (radio)
    ● Corridor        : Short / Medium / Long           (dropdown, prog only)
    ● Diameter        : 55 / 60 / 65 / 70 / 75         (dropdown)
    ● Fitting Height  : 12 / 14 / 16 / 18 / 20 / 22 / 24  (dropdown)
    ● Instructions    : free-text for lab               (textarea)
    """
    if not st.session_state.retail_selected_product:
        return
    
    lp = st.session_state.retail_lens_params or {}

    with st.expander("👓 Lens Parameters", expanded=False):
        st.markdown("---")

        # ── Frame Type  (3 options only) ──
        frame_options = ["Full", "Supra", "Rimless"]
        current_frame = lp.get('frame_type') or "Full"
        if current_frame not in frame_options:
            current_frame = "Full"
        frame_type = st.radio(
            "Frame Type",
            options=frame_options,
            index=frame_options.index(current_frame),
            horizontal=True,
            key="lp_frame_type"
        )

        # ── Thickness ──
        thickness_options = ["Regular", "Thin", "Cartier Thick"]
        current_thick = lp.get('thickness') or "Regular"
        if current_thick not in thickness_options:
            current_thick = "Regular"
        thickness = st.radio(
            "Thickness",
            options=thickness_options,
            index=thickness_options.index(current_thick),
            horizontal=True,
            key="lp_thickness"
        )

        # ── Fitting Service ──────────────────────────────────────────
        st.markdown("**🔧 Fitting Service**")
        fitting_required = st.checkbox(
            "Fitting Required",
            value=bool(lp.get("fitting_required", False)),
            key="lp_fitting_required"
        )
        fitting_type = ""
        if fitting_required:
            fit_type_options = ["Full Rim", "Supra", "Three Piece"]
            current_fit_type = lp.get("fitting_type") or "Full Rim"
            if current_fit_type not in fit_type_options:
                current_fit_type = "Full Rim"
            fitting_type = st.radio(
                "Fitting Type",
                options=fit_type_options,
                index=fit_type_options.index(current_fit_type),
                horizontal=True,
                key="lp_fitting_type"
            )
            st.info("ℹ️ Fitting price will be confirmed after generating the order.")

        st.markdown("---")

        # ── Colouring / Tint ─────────────────────────────────────────
        st.markdown("**🎨 Colouring / Tint**")
        _COLOUR_LIST = [
            "None",
            "Brown 10%", "Brown 20%", "Brown 30%", "Brown 50%", "Brown 75%",
            "Grey 10%", "Grey 20%", "Grey 30%", "Grey 50%", "Grey 75%",
            "Green 10%", "Green 20%", "Green 30%",
            "Blue 10%", "Blue 20%",
            "Pink 10%", "Pink 20%", "Rose 20%",
            "Yellow 10%", "Amber 20%",
            "Gradient Brown", "Gradient Grey", "Gradient Green",
            "Gradient Blue", "Gradient Pink",
            "Photochromic Brown", "Photochromic Grey",
            "Solid Brown", "Solid Grey", "Solid Black",
            "Other (Manual)",
        ]
        current_colour = lp.get("colour") or "None"
        if current_colour not in _COLOUR_LIST:
            _manual_prefill = current_colour
            current_colour  = "Other (Manual)"
        else:
            _manual_prefill = lp.get("colour_manual") or ""

        colour = st.selectbox(
            "Colour",
            options=_COLOUR_LIST,
            index=_COLOUR_LIST.index(current_colour),
            key="lp_colour"
        )
        colour_manual = ""
        if colour == "Other (Manual)":
            colour_manual = st.text_input(
                "Specify colour",
                value=_manual_prefill,
                placeholder="e.g. Violet 15%, Custom gradient…",
                key="lp_colour_manual"
            )

        tint_sample_b64 = lp.get("tint_sample_b64") or ""
        if colour != "None":
            st.markdown("📷 **Tint Sample** *(optional)*")
            _uploaded = st.file_uploader(
                "Upload from camera or file",
                type=["jpg", "jpeg", "png", "webp"],
                key="lp_tint_sample",
                label_visibility="collapsed",
                help="Take a photo with your mobile camera or attach an image file"
            )
            if _uploaded is not None:
                import base64
                tint_sample_b64 = base64.b64encode(_uploaded.read()).decode()
                st.image(_uploaded, caption="Tint sample", width=180)
            elif tint_sample_b64:
                import base64
                _img_bytes = base64.b64decode(tint_sample_b64)
                st.image(_img_bytes, caption="Saved tint sample", width=180)
            st.info("ℹ️ Colouring price will be confirmed after generating the order.")

        tinted = "Yes" if colour != "None" else "No"

        # ── Corridor (progressive only) ──
        corridor_options = ["", "Short", "Medium", "Long"]
        current_cor = lp.get('corridor') or ""
        if current_cor not in corridor_options:
            current_cor = ""
        corridor = st.selectbox(
            "Corridor (Progressive)",
            options=corridor_options,
            index=corridor_options.index(current_cor),
            key="lp_corridor"
        )

        st.markdown("---")

        # ── Diameter + Fitting Height side-by-side ──
        col1, col2 = st.columns(2)

        with col1:
            dia_options = ["", "55", "60", "65", "70", "75"]
            current_dia = lp.get('diameter') or ""
            if current_dia not in dia_options:
                current_dia = ""
            diameter = st.selectbox(
                "Diameter",
                options=dia_options,
                index=dia_options.index(current_dia),
                key="lp_diameter"
            )

        with col2:
            fh_options = ["", "12", "14", "16", "18", "20", "22", "24"]
            current_fh = lp.get('fitting_height') or ""
            if current_fh not in fh_options:
                current_fh = ""
            fitting_height = st.selectbox(
                "Fitting Height",
                options=fh_options,
                index=fh_options.index(current_fh),
                key="lp_fitting_height"
            )

        st.markdown("---")

        # ── Instructions to Lab (free-text) ──
        instructions = st.text_area(
            "📝 Instructions to Lab",
            value=lp.get('instructions') or "",
            height=90,
            placeholder="Any special instructions for the lab...",
            key="lp_instructions"
        )

        # persist — guarded so write only fires when values actually change (prevents rerun loop)
        _lp_new = {
            'frame_type':       frame_type,
            'thickness':        thickness,
            'tinted':           tinted,
            'corridor':         corridor,
            'diameter':         diameter,
            'fitting_height':   fitting_height,
            'instructions':     instructions,
            'fitting_required': fitting_required,
            'fitting_type':     fitting_type,
            'colour':           colour if colour != "Other (Manual)" else colour_manual,
            'colour_manual':    colour_manual,
            'tint_sample_b64':  tint_sample_b64,
        }
        if st.session_state.get('retail_lens_params') != _lp_new:
            st.session_state.retail_lens_params = _lp_new

# ============================================================================
# BOXING PARAMETERS - FROM OLD WORKING VERSION
# ============================================================================

def render_boxing_params():
    """Frame boxing measurements — shared with wholesale. See modules/core/shared_punching.py"""
    if _HAS_SHARED_BOXING:
        _shared_render_boxing_params()
    # else: fallback — function body kept below for safety
# ============================================================================
# BATCH ALLOCATION EDITOR - FROM OLD WORKING VERSION
# ============================================================================

# ============================================================================
# BATCH ALLOCATION EDITOR - WITH QUANTITY ENGINE
# ============================================================================

def render_batch_allocation_editor():
    """
    Render batch allocation editor with QuantityEngine
    
    Features:
    - Dynamic inputs based on product (BOX/PCS/PAIR)
    - QuantityEngine for quantity normalization
    - Automatic validation
    - FIFO batch allocation
    """
    
    # Ensure allocation is always prepared when batch editor opens
    if (
        st.session_state.retail_show_batch_editor
        and not st.session_state.retail_current_allocation
        and not st.session_state.get("_alloc_lock")
    ):
        st.session_state["_alloc_lock"] = True

        if st.session_state.retail_pending_eyes:

            next_eye = st.session_state.retail_pending_eyes.pop(0)

            prepare_allocation(next_eye)

        else:
            # Fallback: detect eye from saved power (old behavior)
            if st.session_state.retail_new_rx_r:
                prepare_allocation('R')
            elif st.session_state.retail_new_rx_l:
                prepare_allocation('L')
    
    if not st.session_state.retail_show_batch_editor or not st.session_state.retail_current_allocation:
        return

    allocation = st.session_state.retail_current_allocation

    # Eye indicator
    eye_indicator = {
        'R': '👁️ RIGHT EYE',
        'L': '👁️ LEFT EYE',
        'B': '👁️👁️ BOTH EYES'
    }

    _eye_lbl  = eye_indicator.get(allocation["eye_side"], allocation["eye_side"])
    _prod_lbl = f"{allocation['brand']} · {allocation['product_name']}"
    with st.expander(f"📦 Batch Allocation — {_eye_lbl}  ·  {_prod_lbl}", expanded=True):
        st.info(f"**{_eye_lbl}** | {_prod_lbl}  ·  FIFO auto-selected")

        if allocation.get('sph') is not None and allocation['eye_side'] != 'B':
            st.write(f"**Power:** SPH {allocation['sph']} | CYL {allocation['cyl']} | AXIS {allocation['axis']} | ADD {allocation['add_power']}")

        # Get product details for QuantityEngine
        product = _hydrate_product_gst(dict(st.session_state.retail_selected_product['product_row']))

        box_size = int(product.get('box_size', 0) or 0)

        # Safe normalization for PostgreSQL 't'/'f' values (lowercase)
        val = str(product.get("allow_loose", "")).lower()
        allow_loose = val in ["true", "t", "1", "yes"]

        # Get available batches
        batches_df = get_batches_fifo(
            allocation['product_id'],
            allocation.get('sph'),
            allocation.get('cyl'),
            allocation.get('axis'),
            allocation.get('add_power'),
            allocation.get('eye_side')
        )

        batches = []
        if not batches_df.empty:
            for _, row in batches_df.iterrows():
                batches.append({
                    'batch_id': str(row.get('batch_id')),
                    'batch_no': row.get('batch_no'),
                    'expiry_date': row.get('expiry_date'),
                    'available_qty': row.get('available_qty'),
                    # ✅ WHOLESALE: selling_price is the trade price from DB
                    'selling_price': resolve_price_for_order_type(dict(row), "WHOLESALE")
                })

        if batches:
            # Show available stock
            st.markdown("### 📊 Available Stock (FIFO Order)")

            batch_cols = st.columns(min(len(batches), 4))

            for idx, batch in enumerate(batches[:4]):  # Show max 4 batches in summary
                with batch_cols[idx]:
                    # Format stock display using centralized function
                    available = int(batch['available_qty'])
                    display_text = format_quantity_display(available, product)

                    st.metric(f"Batch {idx+1}", display_text)
                    st.caption(f"No: {batch['batch_no']}")
                    st.caption(f"Exp: {batch['expiry_date']}")

            if len(batches) > 4:
                st.caption(f"... and {len(batches)-4} more batches")

            st.markdown("---")

            # ===============================
            # Quantity Engine in Batch Editor
            # ===============================
            st.markdown("### ✏️ Enter Quantity")

            # Initialize QuantityEngine
            # Normalize product for QuantityEngine (same as main screen)
            qe_product = dict(product)

            if int(qe_product.get("box_size", 0) or 0) > 1:
                qe_product["unit"] = "BOX"

                # Safe normalization for PostgreSQL 't'/'f' values
                val = str(qe_product.get("allow_loose", "")).lower()
                qe_product["allow_loose"] = val in ["true", "t", "1", "yes"]

            engine = QuantityEngine(qe_product)

            schema = engine.get_ui_schema()

            st.info(f"📦 Mode: **{schema['label']}**")

            # Get pre-filled value from allocation
            # Hard enforce PCS
            raw_pcs = int(allocation.get("requested_qty", 0) or 0)

            if raw_pcs < 0:
                raw_pcs = 0

            box = 0
            pcs = 0
            pair = 0.0

            if engine.mode in ["BOX_ONLY", "FLEX"] and engine.box_size > 0:
                box = int(raw_pcs // engine.box_size)
                pcs = int(raw_pcs % engine.box_size)

            elif engine.mode in ["PCS_ONLY"]:
                pcs = raw_pcs

            elif engine.mode in ["PAIR_ONLY", "PAIR_FLEX"]:
                pair = float(raw_pcs / PAIR_TO_PCS)

            # Build user input based on schema
            user_input = {}
            cols = st.columns(3)
            idx = 0

            if schema["box"]:
                with cols[idx]:
                    user_input["box"] = st.number_input(
                        "Qty (BOX)",
                        min_value=0,
                        max_value=100,
                        value=box,
                        step=1,
                        key=f"retail_qe_be_{allocation['eye_side']}_box"

                    )
                idx += 1

            if schema["pcs"]:
                with cols[idx]:
                    user_input["pcs"] = st.number_input(
                        "Qty (PCS)",
                        min_value=0,
                        max_value=max(0, int(allocation['available_qty'])),
                        value=int(pcs), 
                        step=1,
                        key=f"retail_qe_be_{allocation['eye_side']}_pcs"

                    )
                idx += 1

            if schema["pair"]:
                with cols[idx]:
                    user_input["pair"] = st.number_input(
                        "Qty (PAIR)",
                        min_value=0.0,
                        max_value=100.0,
                        value=float(pair) if engine.mode in ["PAIR_ONLY", "PAIR_FLEX"] else 0.0,
                        step=schema.get("pair_step", 1),
                        format=schema.get("pair_format", "%.0f"),
                        key=f"retail_qe_be_{allocation['eye_side']}_pair"
                    )

                idx += 1

            # Process with QuantityEngine
            result = engine.process(user_input)

            # Enforce no loose pieces if not allowed
            if not allow_loose and engine.box_size > 1:

                pcs = user_input.get("pcs", 0)

                if pcs > 0:
                    st.error("❌ Loose pieces not allowed for this product. Use full boxes only.")
                    return

            # Show validation errors
            if not result["is_valid"]:
                for e in result["errors"]:
                    st.error(e)

                if st.button("❌ Cancel", width='stretch', key="ws_cancel_validation_error"):
                    clear_allocation_state()
                    st.session_state.retail_pending_eyes = []
                    st.rerun()

                return

            # Use QuantityEngine result
            punched_qty = result["final_pcs"]

            # Only proceed if quantity entered
            if punched_qty == 0:
                st.markdown("---")
                st.info("👆 Please enter quantity above to continue with batch allocation")

                if st.button("❌ Cancel", width='stretch', key="ws_cancel_zero_qty"):
                    clear_allocation_state()
                    st.session_state.retail_pending_eyes = []
                    st.rerun()

                return

            # Show final quantity summary
            st.markdown("---")
            st.success(f"✅ Final Quantity: **{punched_qty} PCS** | Mode: {result['mode']}")

            # ============ BATCH ALLOCATION ============
            st.markdown("---")
            st.markdown("### 📦 Batch-wise Quantity Allocation")

            st.caption(f"Allocating {punched_qty} units across {len(batches)} available batch(es)")

            batch_quantities = []
            remaining = punched_qty

            for idx, batch in enumerate(batches):
                col1, col2, col3, col4 = st.columns([2, 2, 2, 2])

                with col1:
                    st.write(f"**Batch {idx+1}**")
                    st.caption(f"No: {batch['batch_no']}")

                with col2:
                    # Format available stock using centralized function
                    available = int(batch['available_qty'])
                    display_text = format_quantity_display(available, product)

                    st.write(f"**Available:** {display_text}")
                    st.caption(f"Exp: {batch['expiry_date']}")

                with col3:
                    available_qty = int(batch['available_qty'])
                    default_qty = min(
                        int(remaining),
                        int(available_qty)
                    )
                    batch_qty = st.number_input(
                        "Allocate Qty",
                        min_value=0,
                        max_value=min(available_qty, punched_qty),
                        value=default_qty,
                        step=1,
                        key=f"batch_qty_{allocation['line_id']}_{allocation['eye_side']}_{idx}",
                        help=f"Allocate from this batch (max: {available_qty})"
                    )
                    batch_quantities.append({
                        'batch_no': batch['batch_no'],
                        'batch_id': batch.get('batch_id'),
                        'expiry_date': str(batch['expiry_date']),
                        'allocated_qty': batch_qty,
                        'available_qty': batch['available_qty'],
                        # ✅ WHOLESALE: re-resolve from batch row (selling_price = trade price)
                        'selling_price': resolve_price_for_order_type(batch, "WHOLESALE")
                    })
                    remaining -= batch_qty

                with col4:
                    if batch_qty > 0:
                        st.success(f"✓ {batch_qty}")
                    else:
                        st.caption("Not used")

            # Prevent negative remaining quantity
            remaining = max(0, remaining)

            total_allocated = sum(b['allocated_qty'] for b in batch_quantities)

            # Validation
            st.markdown("---")

            if total_allocated != punched_qty:
                st.error(f"❌ Allocation mismatch: Total allocated ({total_allocated}) must equal billing quantity ({punched_qty})")
            else:
                st.success(f"✅ Perfect allocation: {total_allocated} units distributed across batches")

            # Calculate pending quantity (to order from supplier)
            pending_qty = max(0, punched_qty - total_allocated)

            # Show final summary
            col1, col2, col3 = st.columns(3)

            with col1:
                st.metric(
                    "Billing Qty",
                    punched_qty,
                    help="Units from stock - will appear on invoice",
                    delta=None
                )

            with col2:
                st.metric(
                    "Order Qty",
                    pending_qty,
                    help="Units to order from supplier - will appear on PO",
                    delta=None
                )

            with col3:
                status = "READY" if pending_qty == 0 else "PARTIAL"
                st.metric(
                    "Status",
                    status,
                    help="READY = Full stock | PARTIAL = Need to order more"
                )

            st.markdown("---")

            # Next action message
            if st.session_state.retail_pending_eyes:
                next_eye = st.session_state.retail_pending_eyes[0]
                eye_name = "RIGHT" if next_eye == 'R' else "LEFT"
                st.info(f"ℹ️ After finalizing, you'll configure **{eye_name}** eye")

            # Action buttons
            col1, col2 = st.columns(2)

            with col1:
                if st.button("❌ Cancel", width='stretch', key="ws_cancel_batch_allocation"):
                    clear_allocation_state()
                    st.session_state.retail_pending_eyes = []  # Clear all pending eyes on cancel
                    st.rerun()

            with col2:
                can_finalize = (total_allocated == punched_qty)

                if can_finalize:
                    enter_to_submit()
                    if st.button("✅ Finalize & Add to Cart  [Enter]", type="primary", width='stretch', key="ws_finalize_add_to_cart_active"):

                        # Create provisional order if needed
                        provisional_id = create_provisional_order()

                        # -------------------------------
                        # GET RAW PRICE (SAFE)
                        # -------------------------------

                        # -------------------------------
                        # CALCULATE WEIGHTED PRICE
                        # -------------------------------

                        total_value = 0
                        total_units = 0

                        for bq in batch_quantities:
                            qty = int(bq.get("allocated_qty") or 0)
                            raw_price = resolve_price_for_order_type(
                                dict(bq), "WHOLESALE"
                            )

                            # Use centralized price normalizer
                            price = normalize_to_pcs_price(raw_price, product)

                            if qty > 0 and price > 0:
                                total_value += qty * price
                                total_units += qty

                        # -------------------------------
                        # CALCULATE WEIGHTED PCS PRICE (FINAL TRUTH)
                        # -------------------------------

                        if total_units > 0:
                            # Batch selling_price is already normalized to PER PCS above
                            pcs_price = total_value / total_units

                        else:
                            # Emergency fallback → use resolver (WHOLESALE: selling_price → mrp)
                            raw_price = resolve_price_for_order_type(product, "WHOLESALE")
                            pcs_price = normalize_to_pcs_price(raw_price, product)

                        pcs_price = round(pcs_price, 2)
                        # Use normalize_box_total to avoid per-PCS rounding for BOX products
                        # raw_price here is the BOX price from inventory_stock
                        _raw_for_total = resolve_price_for_order_type(product, "WHOLESALE")
                        total_price = normalize_box_total(_raw_for_total, punched_qty, product)                         if _raw_for_total > 0 else round(pcs_price * punched_qty, 2)

                        # GST % from product master; tax engine stamps gst_amount below
                        gst_pct    = float(product.get('gst_percent') or allocation.get('gst_percent') or 0)
                        discount   = float(product.get('discount_percent') or 0)

                        box_size_l  = max(1, int(product.get('box_size') or 1))
                        display_qty_l = pcs_to_box_display(punched_qty, box_size_l)

                        # -------------------------------
                        # BUILD LINE ITEM
                        # -------------------------------

                        line = {
                            'line_id': str(uuid.uuid4()),
                            'provisional_order_id': provisional_id,

                            'product_id': allocation['product_id'],
                            'product_name': allocation['product_name'],
                            'brand': allocation['brand'],
                            'main_group': allocation['main_group'],

                            'eye_side': allocation['eye_side'],
                            'sph': allocation.get('sph'),
                            'cyl': allocation.get('cyl'),
                            'axis': allocation.get('axis'),
                            'add_power': allocation.get('add_power'),

                            'lens_params': dict(st.session_state.retail_lens_params or {}),
                            'boxing_params': dict(st.session_state.retail_boxing_params or {}),

                            'requested_qty': punched_qty,
                            'billing_qty': total_allocated,
                            'order_qty': pending_qty,

                            'display_qty': display_qty_l,

                            'batch_allocation': batch_quantities,
                            'suggested_allocation': list(batch_quantities),

                            'unit_price': pcs_price,
                            'total_price': total_price,
                            'unit': str(product.get('unit') or 'PCS').upper(),
                            'box_size': box_size_l,

                            'gst_percent': gst_pct,
                            'gst_amount':  0.0,
                            'discount_percent': discount,
                            # Phase 2D: carry purchase_rate (cost) for margin guard in engine
                            'purchase_rate': float(product.get('purchase_rate') or 0),

                            'status': 'Complete' if pending_qty == 0 else 'Partial',
                            'created_at': datetime.datetime.now().isoformat()
                        }

                        _stamp_line_tax(line, "WHOLESALE")   # ← single source of truth

                        # ── CONSOLIDATE: merge qty into existing line if same product+eye+power ──
                        merged = False
                        for existing in st.session_state.retail_order_lines:
                            if (
                                existing['product_id'] == line['product_id']
                                and existing['eye_side'] == line['eye_side']
                                and existing.get('sph') == line.get('sph')
                                and existing.get('cyl') == line.get('cyl')
                                and existing.get('axis') == line.get('axis')
                                and existing.get('add_power') == line.get('add_power')
                            ):
                                existing['requested_qty'] += line['requested_qty']
                                existing['billing_qty'] += line['billing_qty']
                                existing['order_qty'] += line['order_qty']
                                existing['total_price'] = round(existing['unit_price'] * existing['billing_qty'], 2)
                                _stamp_line_tax(existing, "WHOLESALE")   # re-stamp after qty merge
                                for new_ba in line['batch_allocation']:
                                    found_batch = False
                                    for ex_ba in existing['batch_allocation']:
                                        if ex_ba['batch_no'] == new_ba['batch_no']:
                                            ex_ba['allocated_qty'] += new_ba['allocated_qty']
                                            found_batch = True
                                            break
                                    if not found_batch:
                                        existing['batch_allocation'].append(dict(new_ba))
                                existing['suggested_allocation'] = list(existing['batch_allocation'])
                                existing['display_qty'] = format_quantity_display(existing['billing_qty'], product)
                                existing['status'] = 'Complete' if existing['order_qty'] == 0 else 'Partial'
                                merged = True
                                break

                        if not merged:
                            _stamp_cart_line_discount(line)
                            st.session_state.retail_order_lines.append(line)

                        st.success("✅ Quantity merged into existing line!" if merged else "✅ Added to cart successfully!")

                        # Check if more eyes pending BEFORE clearing allocation
                        has_more_eyes = bool(st.session_state.retail_pending_eyes)

                        # Clear current allocation
                        clear_allocation_state()

                        # If more eyes pending, prepare next
                        if has_more_eyes:
                            next_eye = st.session_state.retail_pending_eyes.pop(0)
                            prepare_allocation(next_eye)
                        else:
                            # Clear pending eyes only when no more eyes to process
                            st.session_state.retail_pending_eyes = []

                        st.rerun()
                else:
                    st.button(
                        "✅ Finalize & Add to Cart",
                        type="primary",
                        width='stretch',
                        disabled=True,
                        help="Fix allocation mismatch first",
                        key="ws_finalize_add_to_cart_disabled"
                    )

        else:
            # No batches available
            st.warning("⚠️ No stock available for this power combination")
            st.info("This product will need to be ordered from supplier")

            # Allow adding to cart as "To Order"
            col1, col2 = st.columns(2)

            with col1:
                if st.button("❌ Cancel", width='stretch', key="ws_cancel_no_stock_to_order"):
                    clear_allocation_state()
                    st.session_state.retail_pending_eyes = []  # Clear all pending eyes on cancel
                    st.rerun()

            with col2:
                enter_to_submit()
                if st.button("➕ Add to Cart (To Order)  [Enter]", type="primary", width='stretch'):

                    provisional_id = create_provisional_order()

                    line = {
                        'line_id': allocation['line_id'],
                        'provisional_order_id': provisional_id,

                        'product_id': allocation['product_id'],
                        'product_name': allocation['product_name'],
                        'brand': allocation['brand'],
                        'main_group': allocation['main_group'],

                        'eye_side': allocation['eye_side'],
                        'sph': allocation.get('sph'),
                        'cyl': allocation.get('cyl'),
                        'axis': allocation.get('axis'),
                        'add_power': allocation.get('add_power'),

                        'lens_params': dict(st.session_state.retail_lens_params or {}),
                        'boxing_params': dict(st.session_state.retail_boxing_params or {}),

                        'requested_qty': int(allocation.get('requested_qty', 0)),
                        'billing_qty': 0,
                        'order_qty': int(allocation.get('requested_qty', 0)),

                        # Display quantity (BOX+PCS format)
                        'display_qty': format_quantity_display(
                            int(allocation.get('requested_qty', 0)),
                            product
                        ),

                        'batch_allocation': [],
                        'suggested_allocation': [],  # ✅ empty — no stock allocated

                        # ✅ unit_price from allocation (already resolved via resolver in prepare_allocation)
                        # total_price IS set — Pending means no stock allocated yet, not zero-value
                        'unit_price': allocation.get('unit_price', 0),
                        'total_price': round(
                            float(allocation.get('unit_price') or 0) * int(allocation.get('requested_qty', 0)), 2
                        ),
                        'unit': str(product.get('unit') or 'PCS').upper(),
                        'box_size': max(1, int(product.get('box_size') or 1)),

                        'gst_percent': float(product.get('gst_percent') or allocation.get('gst_percent') or 0),
                        'gst_amount':  0,   # stamped by _stamp_line_tax below
                        'discount_percent': float(product.get('discount_percent') or 0),
                        # Phase 2D: carry purchase_rate (cost) for margin guard in engine
                        'purchase_rate': float(product.get('purchase_rate') or 0),

                        'status': 'Pending',
                        'created_at': datetime.datetime.now().isoformat()
                    }

                    _stamp_line_tax(line, "WHOLESALE")   # ← stamp GST amount on Pending line

                    # ── CONSOLIDATE: merge qty into existing line if same product+eye+power ──
                    merged = False
                    for existing in st.session_state.retail_order_lines:
                        if (
                            existing['product_id'] == line['product_id']
                            and existing['eye_side'] == line['eye_side']
                            and existing.get('sph') == line.get('sph')
                            and existing.get('cyl') == line.get('cyl')
                            and existing.get('axis') == line.get('axis')
                            and existing.get('add_power') == line.get('add_power')
                        ):
                            existing['requested_qty'] += line['requested_qty']
                            existing['order_qty'] += line['order_qty']
                            existing['merged'] = True
                            merged = True
                            break
                    if not merged:
                        _stamp_cart_line_discount(line)
                        st.session_state.retail_order_lines.append(line)

                    st.success("✅ Quantity merged (To Order)!" if merged else "✅ Added to cart (To Order)")

                    # Check if more eyes pending BEFORE clearing allocation
                    has_more_eyes = bool(st.session_state.retail_pending_eyes)

                    # Clear current allocation
                    clear_allocation_state()

                    # If more eyes pending, prepare next
                    if has_more_eyes:
                        next_eye = st.session_state.retail_pending_eyes.pop(0)
                        prepare_allocation(next_eye)
                    else:
                        # Clear pending eyes only when no more eyes to process
                        st.session_state.retail_pending_eyes = []

                    st.rerun()


    # ============================================================================
    # ORDER SUMMARY - COLLAPSIBLE BOX/PCS BREAKDOWN
    # ============================================================================

def pcs_to_box_display(pcs: int, box_size: int) -> str:
    """Convert piece count to human-friendly box+pcs string.
    
    Examples (box_size=6):
      6 pcs  → 1 Box
      9 pcs  → 1 Box 3 Pcs
      3 pcs  → 3 Pcs
    """
    if box_size <= 1:
        return f"{pcs} Pcs"
    boxes = pcs // box_size
    loose = pcs % box_size
    parts = []
    if boxes:
        parts.append(f"{boxes} Box" if boxes == 1 else f"{boxes} Boxes")
    if loose:
        parts.append(f"{loose} Pc" if loose == 1 else f"{loose} Pcs")
    return " ".join(parts) if parts else "0 Pcs"


def _get_line_box_size(line: dict) -> int:
    """Return box_size for a line. Read directly from line dict first."""
    # Lines carry box_size from the product master — always use it directly.
    bs = int(line.get("box_size") or 0)
    if bs > 1:
        return bs
    # Fallback: session product cache (only reliable for current selection)
    selected = st.session_state.get("retail_selected_product")
    if selected:
        prod = selected.get("product_row", {})
        if str(prod.get("product_id", "")) == str(line.get("product_id", "")):
            bs = int(prod.get("box_size") or 1)
            if bs > 1:
                return bs
    return 1


def render_order_summary():
    """Collapsible order summary — one row per cart line, with box/pcs qty and box price."""
    lines = st.session_state.get("retail_order_lines", [])
    if not lines:
        return

    # Use pcs_price * billing_qty for subtotal to ensure edit mode accuracy
    # Use stored values — stamped by _stamp_line_tax (governor-correct)
    subtotal_sum = sum(float(l.get("total_price") or 0) for l in lines)
    gst_sum      = sum(float(l.get("gst_amount") or 0) for l in lines)
    invoice_sum  = round(subtotal_sum + gst_sum, 2)
    total_lines  = len(lines)

    with st.expander(
        f"📊 Order Summary  —  {total_lines} line(s)  |  Subtotal: ₹{subtotal_sum:,.2f}  |  Invoice Total: ₹{invoice_sum:,.2f}",
        expanded=False
    ):
        st.markdown("#### Order Summary (Product-wise)")

        # ── Header ──
        hcols = st.columns([1, 4, 2, 2, 2, 2])
        hcols[0].markdown("**Eye**")
        hcols[1].markdown("**Product / Power**")
        hcols[2].markdown("**Qty**")
        hcols[3].markdown("**Box Price ₹**")
        hcols[4].markdown("**Unit Price ₹**")
        hcols[5].markdown("**Total ₹**")
        st.markdown("---")

        # Group lines by eye section for visual clarity
        r_lines     = [l for l in lines if l.get("eye_side") == "R"]
        l_lines     = [l for l in lines if l.get("eye_side") == "L"]
        other_lines = [l for l in lines if l.get("eye_side") not in ("R", "L")]

        def _render_line_row(line):
            eye   = line.get("eye_side", "")
            eye_label = {"R": "👁️ R", "L": "👁️ L", "B": "👁️👁️ B"}.get(eye, eye)

            pname = f"**{line.get('brand', '')}** — {line.get('product_name', '')}"

            # Power string (only for lens/contact lines)
            sph = line.get("sph")
            if sph is not None:
                power_str = (
                    f"SPH {line.get('sph')} | CYL {line.get('cyl')} | "
                    f"AXIS {line.get('axis')} | ADD {line.get('add_power')}"
                )
            else:
                power_str = ""

            # Qty as box+pcs
            total_pcs   = int(line.get("billing_qty", 0) or line.get("requested_qty", 0))
            unit_price  = float(line.get("unit_price", 0))
            box_size    = _get_line_box_size(line)
            qty_display = pcs_to_box_display(total_pcs, box_size)
            box_price   = round(unit_price * box_size, 2)
            # Recalculate line_total from pcs_price * qty — fixes edit mode stale totals
            line_total  = round(unit_price * total_pcs, 2) if unit_price > 0 else float(line.get("total_price", 0))
            status      = line.get("status", "")
            status_icon = "✅" if status == "Complete" else ("⏳" if status == "Partial" else "🔄")

            rcols = st.columns([1, 4, 2, 2, 2, 2])
            rcols[0].markdown(eye_label)
            with rcols[1]:
                st.markdown(pname)
                if power_str:
                    st.caption(power_str)
                st.caption(f"{status_icon} {status}")
            rcols[2].write(qty_display)
            if box_size > 1:
                rcols[3].write(f"₹{box_price:,.2f}")
            else:
                rcols[3].write("—")
            rcols[4].write(f"₹{unit_price:,.2f}")
            rcols[5].write(f"₹{line_total:,.2f}")

        for group_label, group_lines in [
            ("👁️ RIGHT EYE", r_lines),
            ("👁️ LEFT EYE",  l_lines),
            ("🔹 OTHER",      other_lines),
        ]:
            if not group_lines:
                continue
            st.markdown(f"**{group_label}**")
            for line in group_lines:
                _render_line_row(line)
            st.markdown("")   # small spacer

        st.markdown("---")
        # Grand total row
        gcols = st.columns([1, 4, 2, 2, 2, 2])
        gcols[1].markdown("**SUBTOTAL**")
        gcols[5].markdown(f"**₹{subtotal_sum:,.2f}**")

        gst_row = st.columns([1, 4, 2, 2, 2, 2])
        gst_row[1].markdown("**GST**")
        gst_row[5].markdown(f"**₹{gst_sum:,.2f}**")

        inv_row = st.columns([1, 4, 2, 2, 2, 2])
        inv_row[1].markdown("**INVOICE TOTAL**")
        inv_row[5].markdown(f"**₹{invoice_sum:,.2f}**")

# ============================================================================
# ORDER LINES DISPLAY - FROM OLD WORKING VERSION
# ============================================================================

def render_order_lines():
    """Display finalized order lines with provisional order info"""
    
    if st.session_state.retail_provisional_order_id:
        col1, col2, col3, col4 = st.columns([2, 2, 1, 1])
        
        with col1:
            st.markdown(
                "<span style='font-size:0.82rem;font-weight:800;color:#60a5fa'>🛒 Current Order</span>",
                unsafe_allow_html=True)
            st.caption(f"Provisional ID: **{st.session_state.retail_provisional_order_id}**")
        
        with col2:
            if st.session_state.retail_provisional_order_created_at:
                created = datetime.datetime.fromisoformat(st.session_state.retail_provisional_order_created_at)
                st.caption(f"Started: {created.strftime('%I:%M %p')}")
        
        with col3:
            if st.button("📄 Duplicate", width='stretch', help="Duplicate this order"):
                duplicate_current_order()
                st.success("✅ Order duplicated!")
                st.rerun()
        
        with col4:
            if st.button("🗑️ Clear All", width='stretch', type="secondary"):
                if st.session_state.retail_order_lines:
                    clear_provisional_order()
                    st.session_state.retail_pending_eyes = []
                    st.rerun()
    
    if not st.session_state.retail_order_lines:
        st.info("No items in cart. Add products using the form above.")
        return
    
    r_lines = [l for l in st.session_state.retail_order_lines if l['eye_side'] == 'R']
    l_lines = [l for l in st.session_state.retail_order_lines if l['eye_side'] == 'L']
    other_lines = [l for l in st.session_state.retail_order_lines if l['eye_side'] not in ['R', 'L']]
    
    if r_lines:
        st.markdown("#### 👁️ RIGHT EYE")
        for idx, line in enumerate(r_lines, 1):
            render_order_line_item(line, idx, 'R')
    
    if l_lines:
        st.markdown("#### 👁️ LEFT EYE")
        for idx, line in enumerate(l_lines, 1):
            render_order_line_item(line, idx, 'L')
    
    if other_lines:
        st.markdown("#### 🔹 OTHER ITEMS")
        for idx, line in enumerate(other_lines, 1):
            render_order_line_item(line, idx, 'OTHER')

def render_order_line_item(line: Dict, idx: int, eye_group: str):
    """
    Render a single wholesale cart line — clearly shows:
      Product / Brand / Eye / Power
      Qty (Box + Pcs breakdown)   Billing Qty | To Order
      Unit Price | Subtotal | Discount | GST % | GST Amt | Grand Total per line
      Batch allocation summary
      Lens + Boxing params (collapsed inline)
    """
    # ── Work on a local copy so display never mutates session_state (prevents rerun loop) ──
    line = dict(line)   # shallow copy — display reads only, never writes back
    # ── Pre-calculate all values before rendering ────────────────────────────
    eye_side    = normalize_eye_side(line.get('eye_side', ''))
    from modules.core.eye_side_normalizer import display_eye_side
    eye_label   = display_eye_side(eye_side)
    status      = line.get('status', '')
    status_icon = {'Complete': '✅', 'Partial': '⏳', 'Pending': '🔄'}.get(status, '🔄')

    box_size    = max(1, int(line.get('box_size') or 1))
    billing_qty = int(line.get('billing_qty') or 0)
    order_qty   = int(line.get('order_qty') or 0)
    req_qty     = int(line.get('requested_qty') or 0)

    # Qty in box+pcs format
    billing_display = pcs_to_box_display(billing_qty, box_size)
    order_display   = pcs_to_box_display(order_qty,   box_size)

    unit_price  = float(line.get('unit_price') or 0)
    # Always recalculate subtotal from unit_price * billing_qty for edit mode accuracy
    _bq_calc    = int(line.get('billing_qty') or line.get('requested_qty') or 1)
    subtotal    = round(unit_price * _bq_calc, 2) if unit_price > 0 else float(line.get('total_price') or 0)
    # Keep total_price in sync so downstream (finalize, save) uses correct value
    if unit_price > 0 and subtotal != float(line.get('total_price') or 0):
        line['total_price'] = subtotal
    box_price   = round(unit_price * box_size, 2)

    gst_pct     = float(line.get('gst_percent') or 0)
    # Stamp gst_amount if missing or stale (covers old orders with gst_amount=0)
    gst_amount  = float(line.get('gst_amount') or 0)
    if gst_pct > 0 and gst_amount == 0 and subtotal > 0:
        gst_amount = round(subtotal * gst_pct / 100, 2)
        line['gst_amount'] = gst_amount

    disc_pct    = float(line.get('discount_percent') or 0)
    disc_amount = round(subtotal * disc_pct / 100, 2)

    grand_total = round(subtotal - disc_amount + gst_amount, 2)

    expander_label = (
        f"{idx}. {line.get('brand','')} — {line['product_name']}  "
        f"{eye_label}  |  "
        f"Qty: {billing_display}"
        + (f" + Order: {order_display}" if order_qty > 0 else "")
        + f"  |  Subtotal: ₹{subtotal:,.2f}  |  Grand: ₹{grand_total:,.2f}"
        + f"  |  {status_icon} {status}"
    )

    with st.expander(expander_label, expanded=True):

        # ── ROW 1: Product info + delete ────────────────────────────────────
        info_col, del_col = st.columns([11, 1])
        with info_col:
            st.markdown(
                f"**{line['brand']}** — {line['product_name']}  "
                f"&nbsp;&nbsp;`{line.get('main_group','')}`"
            )
            if line.get('sph') is not None and eye_side not in ('B',):
                st.caption(
                    f"SPH {line['sph']} | CYL {line.get('cyl')} | "
                    f"AXIS {line.get('axis')} | ADD {line.get('add_power')}"
                )
        with del_col:
            if st.button("🗑️", key=f"del_{line['line_id']}", help="Remove line"):
                st.session_state.retail_order_lines = [
                    l for l in st.session_state.retail_order_lines
                    if l['line_id'] != line['line_id']
                ]
                if not st.session_state.retail_order_lines:
                    clear_provisional_order()
                    st.session_state.retail_pending_eyes = []
                st.rerun()

        st.markdown("---")

        # ── ROW 2: Qty metrics ───────────────────────────────────────────────
        q1, q2, q3 = st.columns(3)
        q1.metric("📦 Billing Qty",  billing_display, help="Units from stock → goes to invoice")
        q2.metric("🛒 To Order",     order_display,   help="Units to procure → goes to PO")
        q3.metric("📋 Requested",    pcs_to_box_display(req_qty, box_size))

        # Box price row only for box products
        if box_size > 1:
            st.caption(
                f"Box size: **{box_size} PCS**  |  "
                f"Unit price (per PCS): ₹{unit_price:,.2f}  |  "
                f"Box price: ₹{box_price:,.2f}"
            )

        st.markdown("---")

        # ── ROW 3: Pricing — GST% is editable, writes back to cart line ────────
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        p1.metric("💰 Unit Price", f"₹{unit_price:,.2f}", help="Per PCS (ex-GST)")
        p2.metric("🧾 Subtotal",   f"₹{subtotal:,.2f}",   help="unit_price × billing_qty")

        if disc_pct > 0:
            p3.metric(f"🏷️ Disc ({disc_pct:.2f}%)", f"−₹{disc_amount:,.2f}")
        else:
            p3.metric("🏷️ Discount", "—")

        with p4:
            if gst_pct == 0:
                st.caption("⚠️ Missing from product master")
            _gst_key = f"gst_pct_input_{line['line_id']}"
            _gst_kw  = {"min_value":0.0,"max_value":100.0,"step":1.0,
                        "format":"%.0f","help":"Edit if product master value is wrong/missing"}
            if _gst_key not in st.session_state:
                _gst_kw["value"] = gst_pct   # only set default on first render
            new_gst_pct = st.number_input("🔢 GST %", key=_gst_key, **_gst_kw)
            # Write back and re-stamp via tax engine on user edit
            if new_gst_pct != gst_pct:
                for cart_line in st.session_state.retail_order_lines:
                    if cart_line['line_id'] == line['line_id']:
                        cart_line['gst_percent'] = new_gst_pct
                        _stamp_line_tax(cart_line, "WHOLESALE")   # re-stamp → updates gst_amount
                        break
                st.rerun()

        # Read stamped value — never recompute in UI
        live_gst_amount = float(line.get('gst_amount') or 0)
        live_grand      = round(subtotal - disc_amount + live_gst_amount, 2)

        p5.metric("📊 GST Amt",    f"₹{live_gst_amount:,.2f}", help="Added on top (exclusive)")
        p6.metric("✅ Grand Total", f"₹{live_grand:,.2f}",      help="Subtotal − Disc + GST")

        # ── Phase 2C: Discount Breakdown + Scheme Info ───────────────────────
        # Engine stamps these fields — UI only reads and displays.
        # discount_breakdown: list of {type, label, icon, value} per rule fired.
        # scheme_info:        BOGO/slab human-readable description.
        breakdown   = line.get("discount_breakdown") or []
        scheme_info = line.get("scheme_info") or {}

        if breakdown:
            st.markdown("**🏷️ Discounts Applied**")
            bd_cols = st.columns(min(len(breakdown), 4))
            for bi, bd in enumerate(breakdown):
                with bd_cols[bi % 4]:
                    st.markdown(
                        f"{bd.get('icon','🏷️')} **{bd.get('label','')}**  \n"
                        f"`−{bd.get('value', 0):.2f}%`"
                    )
            if len(breakdown) > 1:
                st.caption(f"Total discount: {disc_pct:.2f}%  ·  −₹{disc_amount:,.2f}")

        if scheme_info.get("type") == "bogo":
            st.success(
                f"🎁 {scheme_info.get('description','')}  "
                f"— {scheme_info.get('free_qty', 0)} unit(s) free on this line"
            )

        promo_code = str(line.get("promo_code") or "").strip()
        if promo_code and any(bd.get("type") == "promo_code" for bd in breakdown):
            st.info(f"🎟️ Coupon applied: **{promo_code}**")

        # ── Phase 2D: Margin Warning ─────────────────────────────────────────
        # Engine stamps margin_status and margin_blocked — UI only reads.
        # Soft protection: warn the operator, never block the order.
        _margin_status  = line.get("margin_status", "ok")
        _margin_pct     = float(line.get("margin_pct") or 0)
        _margin_blocked = bool(line.get("margin_blocked", False))

        if _margin_blocked:
            st.error(
                f"🛑 **Margin Hard Stop** — discount was capped to protect minimum margin. "
                f"Net margin: **{_margin_pct:.1f}%**"
            )
        elif _margin_status == "soft_warning":
            st.warning(
                f"⚠️ **Low Margin** — net margin is only **{_margin_pct:.1f}%**. "
                f"Consider reviewing the discount."
            )

        # ── ROW 4: Batch allocation ──────────────────────────────────────────
        if line.get('batch_allocation'):
            st.markdown("**📦 Batch Allocation**")
            ba = line['batch_allocation']
            cols = st.columns(min(len(ba), 6))
            for bi, batch in enumerate(ba):
                with cols[bi % 6]:
                    bqty = int(batch.get('allocated_qty') or 0)
                    st.caption(f"**{batch.get('batch_no', '')}**")
                    st.caption(pcs_to_box_display(bqty, box_size))
                    st.caption(f"Exp: {batch.get('expiry_date', '')}")

        # ── ROW 5: Lens + Boxing params (only if filled) ─────────────────────
        lp = {k: v for k, v in (line.get('lens_params') or {}).items() if v}
        bp_label_map = {
            'a_box': 'A', 'b_box': 'B', 'ed': 'ED', 'ed_axis': 'ED°',
            'dbl': 'DBL', 'r_pd': 'RPD', 'l_pd': 'LPD', 'ipd': 'IPD',
            'fitting_ht_r': 'FHR', 'fitting_ht_l': 'FHL',
            'panto': 'Panto', 'tilt': 'Tilt', 'bvd': 'BVD',
        }
        bp = {bp_label_map.get(k, k): v for k, v in (line.get('boxing_params') or {}).items() if v}

        if lp or bp:
            st.markdown("---")
            if lp:
                import base64 as _b64c
                _tint_b64_cart = lp.pop("tint_sample_b64", None)
                _colour_manual = lp.pop("colour_manual", None)
                # Clean up false-y values and booleans
                _lp_display = {}
                for k, v in lp.items():
                    if v is None or v == "" or v == "None" or v == "No" or v is False:
                        continue
                    if v is True: v = "Yes"
                    _lp_display[k.replace("_", " ").title()] = v

                if _lp_display:
                    _badge_html = "".join(
                        f"<span style='background:#1e293b;color:#94a3b8;border:1px solid #334155;"
                        f"padding:3px 9px;border-radius:20px;font-size:0.68rem;margin:2px'>"
                        f"{k}: <b style='color:#e2e8f0'>{v}</b></span>"
                        for k, v in _lp_display.items()
                    )
                    st.markdown(
                        f"<div style='display:flex;flex-wrap:wrap;gap:4px;padding:4px 0'>"
                        f"<span style='color:#60a5fa;font-size:0.7rem;font-weight:700;"
                        f"padding:3px 6px'>👓 Lens</span>"
                        + _badge_html + "</div>",
                        unsafe_allow_html=True)

                # Tint sample image
                if _tint_b64_cart:
                    try:
                        _ti1, _ti2 = st.columns([1, 4])
                        with _ti1:
                            st.image(_b64c.b64decode(_tint_b64_cart), width=80, caption="Tint")
                        with _ti2:
                            _col_val = lp.get("colour") or (_colour_manual or "")
                            st.markdown(
                                f"<div style='color:#f9a8d4;font-size:0.75rem;padding-top:6px'>"
                                f"🎨 Tint sample attached"
                                + (f"<br><span style='color:#64748b'>{_col_val}</span>" if _col_val else "")
                                + "</div>", unsafe_allow_html=True)
                    except Exception:
                        st.caption("🎨 Tint sample attached")

            if bp:
                _bp_html = "".join(
                    f"<span style='background:#1e293b;color:#94a3b8;border:1px solid #334155;"
                    f"padding:3px 9px;border-radius:20px;font-size:0.68rem;margin:2px'>"
                    f"{k}: <b style='color:#e2e8f0'>{v}</b></span>"
                    for k, v in bp.items()
                )
                st.markdown(
                    f"<div style='display:flex;flex-wrap:wrap;gap:4px;padding:2px 0'>"
                    f"<span style='color:#34d399;font-size:0.7rem;font-weight:700;"
                    f"padding:3px 6px'>📐 Boxing</span>"
                    + _bp_html + "</div>",
                    unsafe_allow_html=True)

# ============================================================================
# FINALIZE TO BACKOFFICE - WHOLESALE VERSION
# ============================================================================

def finalize_wholesale_order():
    """Convert wholesale cart to backoffice order"""
    if not st.session_state.retail_order_lines:
        return

    # ── Stamp lines that are missing gst_amount (only when genuinely 0, not already set) ──
    # IMPORTANT: only mutate when gst_amount is truly missing so loop fires ONCE then stops.
    for _l in st.session_state.retail_order_lines:
        _has_price = float(_l.get("unit_price") or 0) > 0
        _needs_stamp = _has_price and float(_l.get("gst_percent") or 0) > 0 and float(_l.get("gst_amount") or 0) == 0
        if _needs_stamp:
            _stamp_line_tax(_l, "WHOLESALE")   # stamps gst_amount — fires only ONCE per line

    # ── Stamp discounts into cart lines for correct UI display ──────────────
    # Idempotent: only runs when at least one priced line has no discount yet.
    _needs_disc = any(
        float(_l.get("discount_percent") or 0) == 0
        and float(_l.get("unit_price") or 0) > 0
        for _l in st.session_state.retail_order_lines
    )
    if _needs_disc:
        try:
            from modules.pricing.discount_engine import apply_discounts
            _pn = str(st.session_state.get("retail_patient_name") or "").strip()
            _pi = ""
            if _pn:
                try:
                    from modules.sql_adapter import run_query as _rq_f
                    _pr = _rq_f(
                        "SELECT id::text AS id FROM parties "
                        "WHERE party_name=%s AND COALESCE(is_active,TRUE)=TRUE LIMIT 1",
                        (_pn,)
                    ) or []
                    if _pr: _pi = str(_pr[0].get("id") or "")
                except Exception: pass
            _pc_fin = str(st.session_state.get("_ws_promo_code") or "").strip()
            if _pc_fin:
                for _pfl in st.session_state.retail_order_lines:
                    _pfl["promo_code"] = _pc_fin
            apply_discounts(
                st.session_state.retail_order_lines,
                party_id=_pi, order_type="WHOLESALE"
            )
            # Club offers — fire after apply_discounts (cart-level, cross-product)
            try:
                from modules.pricing.club_engine import apply_club_offers
                apply_club_offers(
                    st.session_state.retail_order_lines,
                    order_type="WHOLESALE"
                )
            except Exception: pass
            # Re-stamp GST on net price after discount
            for _dl in st.session_state.retail_order_lines:
                _disc = float(_dl.get("discount_amount") or 0)
                if _disc > 0:
                    _net = round(float(_dl.get("total_price") or 0) - _disc, 2)
                    _dl["billing_total"] = _net
                    _stamp_line_tax(_dl, "WHOLESALE")
        except Exception: pass

    st.markdown("---")
    _wf1, _wf2 = st.columns([6, 1])
    with _wf1:
        st.subheader("✅ Finalize Order")
    with _wf2:
        if st.button("🗑️ Clear All", key="ws_clr_finalize",
                     width='stretch', help="Clear all lines"):
            clear_provisional_order()
            st.session_state.retail_pending_eyes = []
            st.rerun()

    lines       = st.session_state.retail_order_lines
    total_items = len(lines)
    has_r = any(l["eye_side"] == "R" for l in lines)
    has_l = any(l["eye_side"] == "L" for l in lines)

    # ── Promo Code input ─────────────────────────────────────────────────────
    _promo_input = st.text_input(
        "🎟️ Promo Code (optional)",
        value=st.session_state.get("_ws_promo_code", ""),
        placeholder="Enter discount code if applicable",
        key="ws_promo_code_input"
    )
    if _promo_input.strip().upper() != st.session_state.get("_ws_promo_code", ""):
        st.session_state["_ws_promo_code"] = _promo_input.strip().upper()
        for _pl in st.session_state.retail_order_lines:
            _pl["discount_percent"] = 0.0
            _pl["discount_amount"]  = 0.0
        st.rerun()

    # ── Party / eye status bar ──────────────────────────────────────────────
    _pcols = st.columns([3, 1])
    with _pcols[0]:
        st.write(f"**Party:** {st.session_state.retail_patient_name}")
        if st.session_state.get("retail_case_no"):
            st.write(f"**Customer Order No:** {st.session_state.retail_case_no}")
        _eye_badges = []
        if has_r: _eye_badges.append("✅ Right Eye")
        if has_l: _eye_badges.append("✅ Left Eye")
        if _eye_badges:
            st.caption("  |  ".join(_eye_badges))
    with _pcols[1]:
        if has_r and not has_l:
            st.warning("⚠️ Only R eye")
        elif has_l and not has_r:
            st.warning("⚠️ Only L eye")

    # ── Totals ──────────────────────────────────────────────────────────────
    # Use stored values — stamped by _stamp_line_tax (governor-correct)
    subtotal_sum = sum(float(l.get("total_price") or 0) for l in lines)
    gst_sum      = sum(float(l.get("gst_amount") or 0) for l in lines)
    grand_total  = round(subtotal_sum + gst_sum, 2)

    # ── Collapsible per-eye order summary ────────────────────────────────────
    subtotal_sum = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in lines)
    grand_total  = round(subtotal_sum + gst_sum, 2)
    _exp_label = (
        f"📋 {total_items} line{'s' if total_items != 1 else ''}  ·  "
        f"Subtotal ₹{subtotal_sum:,.2f}  ·  "
        f"GST ₹{gst_sum:,.2f}  ·  "
        f"Grand Total ₹{grand_total:,.2f}  —  click to expand / collapse"
    )
    with st.expander(_exp_label, expanded=True):

        # Column headers — [Eye | Product/Power | Qty | Pair View | Unit Price | GST% | GST Amt | Grand Total]
        _hcols = st.columns([1, 3, 2, 2, 2, 1, 2, 2])
        _hcols[0].markdown("**Eye**")
        _hcols[1].markdown("**Product / Power**")
        _hcols[2].markdown("**Qty**")
        _hcols[3].markdown("**Pair View**")
        _hcols[4].markdown("**Unit Price**")
        _hcols[5].markdown("**GST%**")
        _hcols[6].markdown("**GST Amt**")
        _hcols[7].markdown("**Grand Total**")
        st.markdown("---")

        # Group by eye for visual separation
        for _group_label, _group_eye in [("👁️ RIGHT EYE", "R"), ("👁️ LEFT EYE", "L"), ("🔹 OTHER", "OTHER")]:
            _group_lines = [l for l in lines if l.get("eye_side") == _group_eye]
            if not _group_lines:
                continue
            st.markdown(f"**{_group_label}**")
            for _l in _group_lines:
                _eye_icon = {"R": "👁️ R", "L": "👁️ L"}.get(_l.get("eye_side", ""), "🔹")
                _pname    = f"**{_l.get('brand','')}** {_l.get('product_name','')}"
                _sph      = _l.get("sph")
                _power    = ""
                if _sph is not None and _l.get("eye_side") not in ("OTHER", "B"):
                    _power = (
                        f"SPH {_l.get('sph')} | CYL {_l.get('cyl')} | "
                        f"AXIS {_l.get('axis')} | ADD {_l.get('add_power')}"
                    )

                _box_size = int(_l.get("box_size") or 1)
                _bqty     = int(_l.get("billing_qty") or _l.get("requested_qty") or 0)

                try:
                    _qty_disp = pcs_to_box_display(_bqty, _box_size)
                except Exception:
                    _qty_disp = f"{_bqty} Pcs"

                # Price display — BOX products show box price; others show /pc /pair
                _up_disp = float(_l.get("unit_price", 0))
                _box_sz2 = int(_l.get("box_size") or 1)
                if _l.get("eye_side") in ("R", "L") and _bqty:
                    _pairs    = _bqty / PAIR_TO_PCS
                    _pair_str = f"{_bqty} pc ({int(_pairs) if _pairs == int(_pairs) else _pairs:.1f} pair)"
                    if _box_sz2 > 1:
                        _box_p2 = round(_up_disp * _box_sz2, 2)
                        _pair_price_str = f"₹{_box_p2:,.2f}/Box\n₹{_up_disp:,.2f}/pc"
                    else:
                        _pair_price_str = f"₹{_up_disp:,.2f}/pc\n₹{_up_disp*2:,.2f}/pair"
                else:
                    _pair_str = "—"
                    if _box_sz2 > 1:
                        _box_p2 = round(_up_disp * _box_sz2, 2)
                        _pair_price_str = f"₹{_box_p2:,.2f}/Box\n₹{_up_disp:,.2f}/pc"
                    else:
                        _pair_price_str = f"₹{_up_disp:,.2f}"

                _unit_p   = float(_l.get("unit_price", 0))
                # Recalculate from pcs_price * billing_qty — always correct
                _bqty2    = int(_l.get("billing_qty") or _l.get("requested_qty") or 0)
                _gross    = round(_unit_p * _bqty2, 2) if _unit_p > 0 else float(_l.get("total_price", 0))
                _net      = float(_l.get("billing_total") or _gross)  # Use net after discount
                _gst_p    = float(_l.get("gst_percent", 0) or 0)
                _gst_a    = round(_net * _gst_p / 100, 2) if _gst_p > 0 else float(_l.get("gst_amount") or 0)
                _grand    = round(_net + _gst_a, 2)

                _status   = _l.get("status", "")
                _s_icon   = "✅" if _status == "Complete" else ("⏳" if _status == "Partial" else "🔄")

                _rcols = st.columns([1, 3, 2, 2, 2, 1, 2, 2, 0.5])
                _rcols[0].write(_eye_icon)
                with _rcols[1]:
                    st.write(_pname)
                    if _power:
                        st.caption(_power)
                    st.caption(f"{_s_icon} {_status}")
                _rcols[2].write(_qty_disp)
                _rcols[3].caption(_pair_str)
                _rcols[4].write(_pair_price_str)
                _rcols[5].write(f"{_gst_p:.0f}%")
                _rcols[6].write(f"₹{_gst_a:,.2f}")
                _rcols[7].write(f"₹{_grand:,.2f}")
                _ws_del = f"ws_sdel_{_l['line_id']}_{_l.get('eye_side','X')}"
                if _rcols[8].button("🗑️", key=_ws_del, help="Remove line"):
                    st.session_state.retail_order_lines = [
                        _x for _x in st.session_state.retail_order_lines
                        if _x["line_id"] != _l["line_id"]
                    ]
                    if not st.session_state.retail_order_lines:
                        clear_provisional_order()
                        st.session_state.retail_pending_eyes = []
                    st.rerun()

            # Show any "to order" lines for this eye
            _po_lines = [l for l in lines if l.get("eye_side") == _group_eye and int(l.get("order_qty") or 0) > 0]
            for _l in _po_lines:
                _oqty = int(_l.get("order_qty", 0))
                try:
                    _oqty_disp = pcs_to_box_display(_oqty, int(_l.get("box_size") or 1))
                except Exception:
                    _oqty_disp = f"{_oqty} Pcs"
                st.caption(f"  ⚠️ {_l.get('product_name')} — {_oqty_disp} to order from supplier")

        st.markdown("---")
        # Footer totals row — [Eye | Product | Qty | Pair | UnitPrice | GST% | GST Amt | Grand Total]
        _fcols = st.columns([1, 3, 2, 2, 2, 1, 2, 2])
        _fcols[1].markdown("**TOTAL**")
        _fcols[4].markdown(f"**{total_items} lines**")
        _fcols[6].markdown(f"**₹{gst_sum:,.2f}**")
        _fcols[7].markdown(f"**₹{grand_total:,.2f}**")
        st.caption(
            f"Subtotal (excl. GST): ₹{subtotal_sum:,.2f}  ·  "
            f"GST (added on top): ₹{gst_sum:,.2f}  ·  "
            f"Grand Total: ₹{grand_total:,.2f}"
        )
        st.caption("Wholesale: prices are GST-exclusive. GST % is from product master.")

    # ── Summary metrics below the table ────────────────────────────────────
    _mcols = st.columns(3)
    _mcols[0].metric("Total Items", total_items)
    _mcols[1].metric("Subtotal (excl. GST)", f"₹{subtotal_sum:,.2f}")
    _mcols[2].metric("Grand Total (incl. GST)", f"₹{grand_total:,.2f}")

    st.caption(f"Provisional: {st.session_state.retail_provisional_order_id}")

    # Validation before submit
    if not st.session_state.retail_order_lines:
        st.error("❌ No items in cart")
        return

    # ══════════════════════════════════════════════════════════════════════
    # 💰 ADVANCE + BALANCE SECTION
    # ══════════════════════════════════════════════════════════════════════
    st.markdown("""
    <div style='background:#0f172a;border:1px solid #1e293b;border-radius:10px;
                padding:14px 18px;margin:10px 0;border-left:4px solid #8b5cf6'>
      <div style='color:#a78bfa;font-size:0.62rem;letter-spacing:.08em;
                  text-transform:uppercase;margin-bottom:3px'>
        💼 Wholesale — Payment Terms
      </div>
      <div style='color:#e2e8f0;font-size:0.85rem'>
        Collect advance now · Balance on account or delivery
      </div>
    </div>
    """, unsafe_allow_html=True)

    _WS_PAY_METHODS = ["CASH","UPI","NEFT","RTGS","CHEQUE","CREDIT"]
    _ws_existing_adv = float(st.session_state.get("_edit_existing_advance") or 0)
    _ws_is_edit = bool(st.session_state.get("_editing_order_id",""))

    if _ws_is_edit and _ws_existing_adv > 0:
        st.markdown(
            f"<div style='background:#14532d;border:1px solid #22c55e;border-radius:6px;"
            f"padding:8px 14px;margin-bottom:6px;display:flex;justify-content:space-between'>"
            f"<span style='color:#86efac;font-size:0.82rem;font-weight:700'>"
            f"✅ Previously Paid (on file)</span>"
            f"<span style='color:#4ade80;font-size:1rem;font-weight:900'>"
            f"₹{_ws_existing_adv:,.2f}</span></div>",
            unsafe_allow_html=True
        )

    _wsc_l, _wsc_r = st.columns([2, 1])
    with _wsc_l:
        # Radio: mutually exclusive — only one payment mode at a time
        _ws_pay_opts = [
            "📒 Balance on Account (Credit — no payment now)",
            "💰 Collect Advance + Balance on Account",
            "✅ Full Payment Now",
        ]
        _ws_pay_default = st.session_state.get("wh_payment_option", 0)
        _ws_pay_sel = st.radio(
            "Payment Terms",
            options=range(len(_ws_pay_opts)),
            format_func=lambda i: _ws_pay_opts[i],
            index=_ws_pay_default,
            key="wh_payment_option_radio",
            horizontal=False,
            label_visibility="collapsed",
        )
        # (wh_payment_option managed by radio key — no manual write to avoid rerun)
        # Derive legacy flags from radio selection
        _ws_collect     = _ws_pay_sel in (1, 2)   # collect advance or full
        _ws_on_account  = _ws_pay_sel in (0, 1)   # on account (balance posted)
        _ws_full_now    = _ws_pay_sel == 2
    with _wsc_r:
        st.metric("Order Total (incl. GST)", f"₹{grand_total:,.2f}")

    _ws_adv_amt  = 0.0
    _ws_adv_mode = "CASH"
    _ws_adv_ref  = ""
    if _ws_collect:
        _wsa1, _wsa2, _wsa3 = st.columns([1.5, 1.2, 1.5])
        with _wsa1:
            _ws_max = max(grand_total - _ws_existing_adv, 0.0)
            # For Full Payment: pre-fill entire balance. Only set on first render to avoid loop.
            _adv_kw = {"min_value":0.0,"max_value":max(_ws_max,0.01),"step":1.0,
                       "help":"Collect partial or full advance now"}
            if "wh_advance_amount" not in st.session_state:
                _adv_kw["value"] = _ws_max if _ws_full_now else 0.0
            _ws_adv_amt = st.number_input(
                "Payment Amount ₹" if not (_ws_is_edit and _ws_existing_adv > 0) else "Additional Payment ₹",
                key="wh_advance_amount", **_adv_kw
            )
        with _wsa2:
            _ws_adv_mode = st.selectbox("Payment Mode", _WS_PAY_METHODS,
                index=_WS_PAY_METHODS.index(st.session_state.get("wh_advance_mode","CASH"))
                      if st.session_state.get("wh_advance_mode") in _WS_PAY_METHODS else 0,
                key="wh_advance_mode")
        with _wsa3:
            if _ws_adv_mode in ("UPI","NEFT","RTGS","CHEQUE"):
                _ws_adv_ref = st.text_input("TXN / Cheque Ref",
                    key="wh_advance_ref")

        _ws_total_paid = _ws_existing_adv + _ws_adv_amt
        _ws_balance = max(round(grand_total - _ws_total_paid, 2), 0)
        st.markdown(
            f"<div style='background:#0a0f1a;border-radius:6px;padding:8px 14px;margin-top:4px;"
            f"display:flex;gap:24px'>"
            + (f"<span style='color:#4ade80;font-size:0.8rem'><b>Prev paid:</b> ₹{_ws_existing_adv:,.2f}</span>" if _ws_existing_adv > 0 else "")
            + f"<span style='color:#8b5cf6;font-size:0.8rem'><b>New advance:</b> ₹{_ws_adv_amt:,.2f}</span>"
            f"<span style='color:#f59e0b;font-size:0.8rem'><b>Balance:</b> ₹{_ws_balance:,.2f}"
            f"{'  (balance on account)' if _ws_on_account else '  (balance on delivery)'}</span>"
            f"</div>",
            unsafe_allow_html=True
        )
    else:
        _ws_balance = max(round(grand_total - _ws_existing_adv, 2), 0)
        if _ws_on_account:
            st.caption(f"Full balance ₹{_ws_balance:,.2f} will be posted to party account (credit).")
        else:
            st.caption(f"Balance ₹{_ws_balance:,.2f} due on delivery.")

    # ── Edit mode warning ──────────────────────────────────────────────────
    _ws_edit_oid = st.session_state.get("_editing_order_id","")
    _ws_edit_ono = st.session_state.get("_editing_order_no","")
    if _ws_edit_oid:
        st.info(f"✏️ Editing order {_ws_edit_ono} — will be updated in place.", icon="✏️")
        _ws_confirm_label = f"💾 Save Changes to {_ws_edit_ono}"
    else:
        _ws_confirm_label = "💾 Confirm & Send to Backoffice"

    # ── FULL_ADVANCE billing category gate ───────────────────────────────────
    # If party has FULL_ADVANCE billing_category, confirm is blocked until
    # full payment is recorded. Order can be punched and edited freely.
    _fa_blocked = False
    _fa_reason  = ""
    try:
        # ── Billing gate via service layer ────────────────────────────────
        from modules.billing.services.challan_service import (
            get_party_billing_preference, validate_challan_gate,
        )
        from modules.sql_adapter import run_query as _rq_fa
        _fa_pid = (st.session_state.get("retail_patient_id") or
                   st.session_state.get("wh_party_id") or "")
        if _fa_pid:
            _fa_bc   = get_party_billing_preference(_fa_pid)
            _fa_total = sum(float(l.get("total_price", 0))
                            for l in st.session_state.get("retail_order_lines", []))
            _fa_prov  = st.session_state.get("retail_provisional_order_id") or ""
            _fa_pname = (st.session_state.get("retail_patient_name") or
                         st.session_state.get("wh_party_name") or "this party")
            _fa_paid_r = _rq_fa("""
                SELECT COALESCE(SUM(amount),0) AS paid FROM payments
                WHERE party_id=%s::uuid
                  AND COALESCE(is_deleted,FALSE)=FALSE
                  AND (payment_date::date = CURRENT_DATE
                       OR (advance_for_order_id IS NOT NULL
                           AND advance_for_order_id::text = %s))
            """, (_fa_pid, _fa_prov))
            _fa_paid = float(_fa_paid_r[0]["paid"] if _fa_paid_r else 0)
            _allowed, _msg = validate_challan_gate(
                party_id=_fa_pid,
                billing_category=_fa_bc,
                advance_paid=_fa_paid,
            )
            if not _allowed or (_fa_bc == "FULL_ADVANCE" and _fa_paid < _fa_total - 0.01):
                _fa_total = sum(float(l.get("total_price",0))
                                for l in st.session_state.get("retail_order_lines",[]))
                # Check ALL payments for this party today (linked or unlinked to order)
                # Payment can be recorded at any point before confirm — counter, advance, etc.
                _fa_prov = st.session_state.get("retail_provisional_order_id") or ""
                _fa_paid_r = _rq_fa("""
                    SELECT COALESCE(SUM(amount),0) AS paid FROM payments
                    WHERE party_id=%s::uuid
                      AND COALESCE(is_deleted,FALSE)=FALSE
                      AND (
                          payment_date::date = CURRENT_DATE
                          OR (advance_for_order_id IS NOT NULL
                              AND advance_for_order_id::text = %s)
                      )
                """, (_fa_pid, _fa_prov))
                _fa_paid = float(_fa_paid_r[0]["paid"] if _fa_paid_r else 0)
                _fa_pname   = st.session_state.get("retail_patient_name") or                               st.session_state.get("wh_party_name") or "this party"
                if _fa_paid < _fa_total - 0.01:
                    _fa_pending = round(_fa_total - _fa_paid, 2)
                    _fa_blocked = True
                    _fa_reason  = (
                        f"❌ **Full Advance required — Order cannot be confirmed.**\n\n"
                        f"Party: **{_fa_pname}**  ·  "
                        f"Total: **₹{_fa_total:,.2f}**  ·  "
                        f"Paid: ₹{_fa_paid:,.2f}  ·  "
                        f"**Pending: ₹{_fa_pending:,.2f}**\n\n"
                        f"👉 Go to **💰 Payment** → select party → "
                        f"record ₹{_fa_pending:,.2f} against this order → "
                        f"come back and confirm."
                    )
    except Exception:
        pass  # gate is best-effort — never block on error

    if _fa_blocked:
        st.error(_fa_reason)
    from modules.utils.submit_guard import is_locked, guarded_submit
    enter_to_submit()
    kb_legend()
    if st.button(_ws_confirm_label + "  [Enter]", type="primary", width='stretch',
                 disabled=is_locked("ws_confirm") or _fa_blocked):
        with guarded_submit("ws_confirm") as _allowed:
            if not _allowed:
                st.stop()

            # ══════════════════════════════════════════════════════════════
            # EDIT MODE — bypass pipeline, update order directly (no ghost)
            # ══════════════════════════════════════════════════════════════
            if _ws_edit_oid and _ws_edit_ono:
                from modules.retail_punching import _update_order_in_place, _build_edit_receipt_snapshot
                with st.spinner("Saving changes..."):
                    _upd = _update_order_in_place(
                        order_id       = _ws_edit_oid,
                        order_no       = _ws_edit_ono,
                        cart_lines     = st.session_state.retail_order_lines,
                        patient_name   = st.session_state.retail_patient_name,
                        patient_mobile = st.session_state.get("retail_patient_mobile",""),
                        advance_amount = _ws_adv_amt,
                        advance_mode   = _ws_adv_mode,
                        advance_ref    = _ws_adv_ref,
                        collect_advance= _ws_collect,
                        delivery_date  = "",
                        delivery_time  = "",
                        user_name      = st.session_state.get("username","Staff"),
                    )
                if "error" in _upd:
                    st.error(f"❌ Update failed: {_upd['error']}")
                    if "traceback" in _upd:
                        st.code(_upd["traceback"], language="text")
                    st.stop()

                _ws_ono = _ws_edit_ono
                _edit_oid_for_adv = _ws_edit_oid  # capture before pop
                # Capture existing advance BEFORE clearing edit state
                _ws_existing_adv_captured = float(st.session_state.get("_edit_existing_advance") or 0)
                st.session_state.pop("_editing_order_id", None)
                st.session_state.pop("_editing_order_no", None)
                st.session_state.pop("_edit_existing_advance", None)

                # Total paid = previous advance + new payment this edit
                _final_advance2   = round(_ws_existing_adv_captured + (_ws_adv_amt if _ws_collect else 0), 2)
                _grand2 = sum(float(l.get("total_price",0)) for l in st.session_state.get("retail_order_lines",[]))

                # Fetch ALL advance records for this order (for receipt + WhatsApp)
                _adv_records_edit = []
                try:
                    from modules.sql_adapter import run_query as _rq_adv_e
                    _adv_rows_e = _rq_adv_e(
                        "SELECT amount, payment_date::text AS date, payment_mode AS mode "
                        "FROM payments WHERE advance_for_order_id = %s::uuid "
                        "AND payment_type = 'ADVANCE' ORDER BY payment_date",
                        (_edit_oid_for_adv,)
                    ) or []
                    _adv_records_edit = [{"amount": float(r["amount"]), "date": str(r["date"]), "mode": str(r["mode"])} for r in _adv_rows_e]
                except Exception:
                    pass

                st.success(f"✅ Order {_ws_ono} updated")
                st.balloons()

                st.session_state["_post_save_data"] = {
                    "order_no":        _ws_ono,
                    "party_name":      st.session_state.get("retail_patient_name",""),
                    "mobile":          st.session_state.get("retail_patient_mobile",""),
                    "total":           _grand2,
                    "advance":         _final_advance2,
                    "order_type":      "WHOLESALE",
                    "delivery":        "",
                    "on_account":      _ws_on_account,
                    "lines":           list(st.session_state.get("retail_order_lines", [])),
                    "advance_records": _adv_records_edit,
                }

                import time; time.sleep(0.4)

                _WHOLESALE_KEYS2 = [
                    "_lock_ws_confirm","retail_patient_name","retail_case_no",
                    "retail_new_rx_r","retail_new_rx_l","retail_order_lines",
                    "retail_selected_product","retail_current_allocation",
                    "retail_show_batch_editor","retail_pending_eyes",
                    "retail_final_qty_R","retail_final_qty_L",
                    "retail_provisional_order_id","retail_provisional_order_created_at",
                    "last_order_snapshot","retail_lens_params","retail_boxing_params",
                    "wh_roletype","wh_order_date","_product_cache_refreshed",
                    "_alloc_lock","_scroll_to_alloc","reset_product_selector",
                ]
                for _k in _WHOLESALE_KEYS2:
                    st.session_state.pop(_k, None)
                for _k in list(st.session_state.keys()):
                    if (_k.startswith("ps_") or _k.startswith("retail_qe_")
                            or _k.startswith("batch_qty_")
                            or _k.startswith("wh_sph_") or _k.startswith("wh_cyl_")
                            or _k.startswith("wh_axis_") or _k.startswith("wh_add_")
                            or _k.startswith("other_qty_")):
                        st.session_state.pop(_k, None)

                # Clear edit-mode keys so post-save panel renders (not edit mode re-entry)
                st.session_state.pop("_editing_order_id", None)
                st.session_state.pop("_editing_order_no", None)
                st.session_state.pop("_edit_existing_advance", None)
                reset_after_submit("wholesale")

            # ══════════════════════════════════════════════════════════════
            # NEW ORDER — go through full pipeline
            # ══════════════════════════════════════════════════════════════
            from modules.core.order_pipeline import OrderPipeline
            pipeline = OrderPipeline()

            # ── Normalize cart lines before pipeline ─────────────────────
            try:
                from modules.core.order_normalizer import normalize_lines
                st.session_state.retail_order_lines, _norm_warns = normalize_lines(
                    st.session_state.retail_order_lines, order_type="WHOLESALE"
                )
                if _norm_warns:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        f"[WHOLESALE] Normalizer warnings: {_norm_warns}"
                    )
            except Exception as _ne:
                pass  # Normalizer is defensive — never block the order
            # ─────────────────────────────────────────────────────────────

            # End-customer fields — stored in extra_data JSONB, NOT in structured columns
            # This keeps patient_name/mobile clean (those are the dealer, not end customer)
            _ec_name   = str(st.session_state.get("ws_end_customer_name", "") or "").strip()
            _ec_mobile = str(st.session_state.get("ws_end_customer_mobile", "") or "").strip()
            _ec_ono    = str(st.session_state.get("ws_end_customer_order_no", "") or "").strip()

            order_info = {
                "order_type":    "WHOLESALE",
                "order_source":  "wholesale_punching",
                "party":         st.session_state.retail_patient_name,  # dealer/retailer
                "party_name":    st.session_state.retail_patient_name,
                "patient_name":  "",   # not used for wholesale (dealer name is in party)
                "patient_mobile": "",  # not used for wholesale
                "customer_order_no": _ec_ono or st.session_state.get("retail_case_no", ""),
                "lens_params":   dict(st.session_state.retail_lens_params or {}),
                "boxing_params": dict(st.session_state.retail_boxing_params or {}),
                "notes": "",
                # End-customer stored in JSONB — isolated from order views/WhatsApp
                "extra_data": {
                    "end_customer": {
                        "name":   _ec_name,
                        "mobile": _ec_mobile,
                        "ref":    _ec_ono,
                    }
                } if (_ec_name or _ec_mobile or _ec_ono) else {},
            }

            # ── Deduplicate cart before submit ───────────────────────────
            _seen_wh = set()
            _deduped_wh = []
            for _wcl in st.session_state.retail_order_lines:
                _wk = (
                    str(_wcl.get("product_id","") or ""),
                    str(_wcl.get("eye_side","") or "").upper().strip()[:1],
                    str(_wcl.get("sph","") or ""),
                    str(_wcl.get("cyl","") or ""),
                )
                if _wk not in _seen_wh:
                    _seen_wh.add(_wk)
                    _deduped_wh.append(_wcl)
            st.session_state.retail_order_lines = _deduped_wh

            with st.spinner("Processing order..."):
                try:
                    result = pipeline.submit_retail(
                        cart_lines=st.session_state.retail_order_lines,
                        order_info=order_info,
                        user_name="Wholesale Desk"
                    )
                except Exception as _submit_err:
                    import traceback
                    st.error(f"❌ Order save failed: {_submit_err}")
                    st.code(traceback.format_exc(), language="text")
                    st.stop()

            if result["status"] == "REJECTED":
                st.error("❌ Order Validation Failed")
                for err in result.get("errors", []):
                    st.write("•", err)
                st.stop()   # lock auto-clears via guarded_submit __exit__

            elif result.get("status") == "CONFIRMED":

                _ws_ono = str(result.get("order_no",""))

                if False:  # edit mode handled above — this block is NEW ORDER only
                    pass
                else:
                    # ── Record advance for new order ──────────────────────
                    if _ws_collect and _ws_adv_amt > 0:
                        try:
                            from modules.sql_adapter import run_write as _rw_wsa, run_query as _rq_wsa
                            import uuid as _uuid_wsa, datetime as _dt_wsa
                            _res_oid_wa = result.get("order_id") or {}
                            _real_uuid_wa = (str(_res_oid_wa.get("order_db_id","")) if isinstance(_res_oid_wa,dict) else str(_res_oid_wa))
                            if not _real_uuid_wa or len(_real_uuid_wa) < 10:
                                _lwr2 = _rq_wsa("SELECT id FROM orders WHERE order_no=%s LIMIT 1",(_ws_ono,)) or []
                                _real_uuid_wa = str(_lwr2[0]["id"]) if _lwr2 else ""
                            if _real_uuid_wa and len(_real_uuid_wa) > 10:
                                _pno_w = (_rq_wsa("SELECT generate_payment_no() AS pno") or [{}])[0].get("pno","ADV-WS")
                                # Lookup party_id from parties table — needed for party ledger
                                _ws_party_name = st.session_state.get("retail_patient_name","")
                                _pid_rows = _rq_wsa(
                                    "SELECT id FROM parties WHERE party_name=%(n)s AND is_active=TRUE LIMIT 1",
                                    {"n": _ws_party_name}
                                ) or []
                                _ws_party_id = str(_pid_rows[0]["id"]) if _pid_rows else None
                                _rw_wsa("""
                                    INSERT INTO payments (
                                        id, payment_no, party_id, party_name,
                                        challan_id, invoice_id, order_id,
                                        payment_date, payment_mode, amount,
                                        reference_no, remarks, payment_type,
                                        is_advance, advance_for_order_id, created_by
                                    ) VALUES (
                                        %(pid)s, %(pno)s, %(party_id)s::uuid, %(pname)s,
                                        NULL, NULL, %(oid)s::uuid,
                                        %(pdate)s, %(mode)s, %(amt)s,
                                        %(ref)s, %(rmk)s, %(ptype)s,
                                        true, %(oid)s::uuid, %(user)s
                                    )
                                """, {
                                    "pid":      str(_uuid_wsa.uuid4()),
                                    "pno":      _pno_w,
                                    "party_id": _ws_party_id,
                                    "pname":    _ws_party_name,
                                    "oid":      _real_uuid_wa,
                                    "pdate":    _dt_wsa.date.today(),
                                    "mode":     _ws_adv_mode,
                                    "amt":      _ws_adv_amt,
                                    "ref":      _ws_adv_ref or None,
                                    "rmk":      "Wholesale advance at punching",
                                    "ptype":    "ADVANCE",
                                    "user":     st.session_state.get("username","Staff"),
                                })
                                # Update orders.advance_amount and advance_received
                                _rw_wsa("""
                                    UPDATE orders
                                    SET advance_amount   = COALESCE(advance_amount,0) + %(amt)s,
                                        advance_received = TRUE,
                                        updated_at       = NOW()
                                    WHERE id = %(oid)s::uuid
                                """, {"amt": _ws_adv_amt, "oid": _real_uuid_wa})
                        except Exception as _wpa_ex:
                            st.warning(f"⚠️ Order saved but advance not recorded: {_wpa_ex}")

                st.success(f"✅ Order Confirmed: {_ws_ono}")
                st.balloons()

                # Store for post-rerun rendering
                # If full payment now, advance = total
                _final_advance = (
                    float(grand_total) if _ws_full_now
                    else (float(_ws_adv_amt) if _ws_collect else 0.0)
                )

                # Fetch ALL advance records for this order (for receipt + WhatsApp)
                _adv_records_new = []
                try:
                    from modules.sql_adapter import run_query as _rq_adv_n
                    if _real_uuid_wa and len(_real_uuid_wa) > 10:
                        _adv_rows_n = _rq_adv_n(
                            "SELECT amount, payment_date::text AS date, payment_mode AS mode "
                            "FROM payments WHERE advance_for_order_id = %s::uuid "
                            "AND payment_type = 'ADVANCE' ORDER BY payment_date",
                            (_real_uuid_wa,)
                        ) or []
                        _adv_records_new = [{"amount": float(r["amount"]), "date": str(r["date"]), "mode": str(r["mode"])} for r in _adv_rows_n]
                except Exception:
                    pass

                st.session_state["_post_save_data"] = {
                    "order_no":        _ws_ono,
                    "party_name":      st.session_state.get("retail_patient_name",""),
                    "mobile":          st.session_state.get("retail_patient_mobile",""),
                    "total":           float(grand_total),
                    "advance":         _final_advance,
                    "order_type":      "WHOLESALE",
                    "delivery":        "",
                    "on_account":      _ws_on_account,
                    "lines":           list(st.session_state.get("retail_order_lines", [])),
                    "advance_records": _adv_records_new,
                }

                import time
                time.sleep(0.5)

                # ── Full session wipe ─────────────────────────────────────
                # _post_save_data intentionally kept — rendered below after rerun
                _WHOLESALE_KEYS = [
                    "_lock_ws_confirm",
                    "retail_patient_name", "retail_case_no",
                    "retail_new_rx_r", "retail_new_rx_l",
                    "retail_order_lines",
                    "retail_selected_product", "retail_current_allocation",
                    "retail_show_batch_editor", "retail_pending_eyes",
                    "retail_final_qty_R", "retail_final_qty_L",
                    "retail_provisional_order_id", "retail_provisional_order_created_at",
                    "last_order_snapshot", "retail_lens_params", "retail_boxing_params",
                    "wh_roletype", "wh_order_date",
                    "_product_cache_refreshed", "_alloc_lock",
                    "_scroll_to_alloc", "reset_product_selector",
                ]
                for _k in _WHOLESALE_KEYS:
                    st.session_state.pop(_k, None)
                for _k in list(st.session_state.keys()):
                    if (
                        _k.startswith("ps_")
                        or _k.startswith("retail_qe_")
                        or _k.startswith("batch_qty_")
                        or _k.startswith("wh_sph_") or _k.startswith("wh_cyl_")
                        or _k.startswith("wh_axis_") or _k.startswith("wh_add_")
                        or _k.startswith("other_qty_")
                    ):
                        st.session_state.pop(_k, None)
                # ─────────────────────────────────────────────────────────

                # ── Persist confirmed order ref for post-confirm adder ─────
                _res_oid_w = result.get("order_id") or {}
                _real_uuid_w = (
                    str(_res_oid_w.get("order_db_id") or "")
                    if isinstance(_res_oid_w, dict)
                    else str(_res_oid_w)
                )
                if not _real_uuid_w or len(_real_uuid_w) < 10:
                    try:
                        from modules.sql_adapter import run_query as _rq_lw
                        _lwr = _rq_lw("SELECT id FROM orders WHERE order_no=%(n)s LIMIT 1",
                                      {"n": str(result.get("order_no",""))})
                        _real_uuid_w = str(_lwr[0]["id"]) if _lwr else ""
                    except Exception:
                        _real_uuid_w = ""
                st.session_state["last_confirmed_order"] = {
                    "id":       _real_uuid_w,
                    "order_no": str(result.get("order_no", "")),
                    "status":   "PENDING",
                }
                # ─────────────────────────────────────────────────────────

                reset_after_submit("wholesale")
                st.rerun()

            else:
                st.error("❌ Order Failed")
                st.write(result)


# ============================================================================
# CART RENDERING
# ============================================================================

def render_cart():
    """Render the shopping cart with all order lines"""
    st.markdown(
        "<div style='display:flex;align-items:center;gap:8px;margin:6px 0 4px'>"
        "<span style='background:#1e40af;color:#bfdbfe;font-size:0.68rem;font-weight:800;"
        "padding:2px 10px;border-radius:20px;letter-spacing:.05em'>🛒 ORDER CART</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    
    render_order_lines()
    render_order_summary()

    # ── Add extra line to cart before submitting ──────────────────────────
    try:
        from modules.backoffice.order_line_adder import render_cart_line_adder
        render_cart_line_adder(order_type="WHOLESALE")
    except Exception as _ale:
        st.caption(f"Add line: {_ale}")


# ============================================================================
# SUBMIT SECTION
# ============================================================================

def render_submit_section():
    """Render the final submit section"""
    finalize_wholesale_order()


# ============================================================================
# MAIN ENTRY FUNCTION
# ============================================================================

def render_wholesale_punching():

    initialize_session_state()

    # 🎨 Wholesale theme — tight, space-efficient
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap');
    .stApp { background:#f5f7fa !important; font-family:'DM Sans',sans-serif !important; }
    .block-container { padding-top:0.2rem !important; padding-bottom:0.8rem !important; max-width:1100px !important; }
    h1,h2,h3,h4,h5 { font-family:'DM Sans',sans-serif !important; font-weight:700 !important;
                      margin-top:0.4rem !important; margin-bottom:0.2rem !important; }
    .element-container { margin-bottom:4px !important; }
    .stHorizontalBlock { gap:8px !important; }
    div[data-testid="stVerticalBlock"] > div { gap:4px !important; }
    .stMarkdown { margin-bottom:2px !important; }
    .stButton > button { font-family:'DM Sans',sans-serif !important; font-size:0.78rem !important;
                         font-weight:600 !important; padding:5px 12px !important; border-radius:7px !important; }
    .stButton > button[kind="primary"] { background:linear-gradient(135deg,#6366f1 0%,#4f46e5 100%) !important;
                                         color:#fff !important; border:none !important; }
    .stTextInput > div > div > input, .stNumberInput > div > div > input,
    .stSelectbox > div > div > div { font-family:'DM Mono',monospace !important; font-size:0.82rem !important;
                                      border-radius:7px !important; }
    .streamlit-expanderHeader { font-size:0.82rem !important; font-weight:700 !important; padding:6px 12px !important; }
    .streamlit-expanderContent { padding:8px 12px !important; }
    .stTabs [data-baseweb="tab"] { font-size:0.78rem !important; font-weight:600 !important; padding:6px 16px !important; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown(
        "<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px'>"
        "<span style='background:#6366f1;color:#fff;font-size:0.7rem;font-weight:800;"
        "padding:3px 10px;border-radius:20px;letter-spacing:.06em;text-transform:uppercase'>"
        "🏭 Wholesale Punching</span>"
        "<span style='color:#94a3b8;font-size:0.72rem;letter-spacing:.04em'>Order Entry</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    # ── CL Advisor hint — shown at page top whenever power was calculated ──
    _cl_ws = st.session_state.get("_last_cl_result")
    if _cl_ws and not st.session_state.get("_cl_hint_dismissed"):
        _prod_w = _cl_ws.get("product", "")
        _r_w    = _cl_ws.get("R", {})
        _l_w    = _cl_ws.get("L", {})
        def _fmt_w(d):
            if not d.get("ok"): return "—"
            s = f"SPH {float(d['sph']):+.2f}"
            if d.get("cyl") and float(d.get("cyl",0)) != 0.0:
                s += f" / {float(d['cyl']):+.2f} / {d['axis']}°"
            return s
        _wb1, _wb2, _wb3, _wb4 = st.columns([3.5, 3.5, 1, 1])
        _wb1.markdown(
            f"<div style='background:#0f172a;border-left:3px solid #6366f1;"
            f"border-radius:4px;padding:5px 10px;font-size:0.8rem'>"
            f"<span style='color:#6366f1;font-weight:700'>👁️ CL</span> "
            f"<span style='color:#94a3b8'>{_prod_w}</span></div>",
            unsafe_allow_html=True
        )
        _wb2.markdown(
            f"<div style='background:#0f172a;border-left:3px solid #1e293b;"
            f"border-radius:4px;padding:5px 10px;font-size:0.8rem'>"
            f"<span style='color:#10b981'>R {_fmt_w(_r_w)}</span>"
            f"&nbsp;&nbsp;"
            f"<span style='color:#10b981'>L {_fmt_w(_l_w)}</span></div>",
            unsafe_allow_html=True
        )
        with _wb3:
            if st.button("↗ Apply", key="wh_cl_hint_apply_top",
                         width='stretch', help="Fill power fields"):
                for eye, d in [("R", _r_w), ("L", _l_w)]:
                    if d.get("ok"):
                        st.session_state[f"retail_new_rx_{eye.lower()}"] = {
                            "sph":  float(d["sph"]),
                            "cyl":  float(d.get("cyl") or 0),
                            "axis": int(d.get("axis") or 0),
                            "add":  0.0,
                        }
                        for _wk in (f"wh_sph_{eye}", f"wh_cyl_{eye}",
                                    f"wh_axis_{eye}", f"wh_add_{eye}"):
                            st.session_state.pop(_wk, None)
                st.rerun()
        with _wb4:
            if st.button("✕", key="wh_cl_hint_dismiss_top",
                         width='stretch'):
                st.session_state["_cl_hint_dismissed"] = True
                st.rerun()

    # ── Post-save panel — persists across rerun via session state ────────
    _ws_psd = st.session_state.get("_post_save_data")
    if _ws_psd:
        _render_post_save_actions(
            _ws_psd.get("order_no",""),
            _ws_psd.get("party_name",""),
            _ws_psd.get("mobile",""),
            float(_ws_psd.get("total",0)),
            _ws_psd.get("order_type","WHOLESALE"),
            float(_ws_psd.get("advance",0)),
            _ws_psd.get("delivery",""),
            on_account=bool(_ws_psd.get("on_account", True)),
            lines=_ws_psd.get("lines", []),
        )
        if st.button("✕ Dismiss", key="ws_dismiss_post_save"):
            st.session_state.pop("_post_save_data", None)
            st.rerun()
        st.markdown("---")

    # ── Edit mode: bypass header widget — party already set from prefill ──
    _ws_edit_oid_top = st.session_state.get("_editing_order_id","")
    _ws_edit_ono_top = st.session_state.get("_editing_order_no","")
    _ws_prefill_party = st.session_state.get("retail_patient_name","")

    if _ws_edit_oid_top and _ws_prefill_party:
        # Show edit banner and skip header widget
        st.markdown(
            f"<div style='background:#fff7ed;border:2px solid #f97316;"
            f"border-radius:8px;padding:10px 16px;margin-bottom:10px;"
            f"display:flex;justify-content:space-between;align-items:center'>"
            f"<div><span style='font-size:0.75rem;color:#c2410c;font-weight:700;"
            f"text-transform:uppercase;letter-spacing:.06em'>✏️ EDITING ORDER</span>"
            f"<span style='font-family:monospace;font-size:1rem;font-weight:900;"
            f"color:#ea580c;margin-left:10px'> {_ws_edit_ono_top}</span></div>"
            f"<span style='font-size:0.75rem;color:#9a3412'>Make changes — "
            f"use Confirm to save. Changes logged.</span></div>",
            unsafe_allow_html=True
        )
        # Party/case already in session from prefill — no header needed
    else:
        header = render_order_header()

        if not header or not header.get("party"):
            st.warning("Please select Role Type and Party to continue.")
            return

        # Guard: only write if changed — unconditional writes every render trigger rerun loop
        _hdr_party  = header["party"]
        _hdr_case   = header.get("customer_order_no") or ""
        _hdr_role   = header.get("roletype")
        _hdr_date   = header.get("order_date")

        # Party changed — clear old power and order lines so previous customer's
        # data doesn't leak into the new order
        if st.session_state.get("retail_patient_name") != _hdr_party:
            st.session_state.retail_patient_name = _hdr_party
            # Clear old power
            st.session_state.retail_new_rx_r = {}
            st.session_state.retail_new_rx_l = {}
            st.session_state.retail_right_sph = ""
            st.session_state.retail_right_cyl = ""
            st.session_state.retail_right_axis = ""
            st.session_state.retail_right_add = ""
            st.session_state.retail_left_sph = ""
            st.session_state.retail_left_cyl = ""
            st.session_state.retail_left_axis = ""
            st.session_state.retail_left_add = ""
            # Clear cart so previous party's lines don't carry over
            st.session_state.pop("wholesale_order_lines", None)
            st.session_state.pop("wholesale_cart", None)
            st.session_state.pop("wh_order_lines", None)
            st.session_state.pop("retail_patient_id", None)

        if st.session_state.get("retail_case_no") != _hdr_case:
            st.session_state.retail_case_no = _hdr_case
        if st.session_state.get("wh_roletype") != _hdr_role:
            st.session_state.wh_roletype = _hdr_role
        if st.session_state.get("wh_order_date") != _hdr_date:
            st.session_state.wh_order_date = _hdr_date

    render_wholesale_controls()
    render_power_entry()
    render_product_selection()
    render_lens_params()
    render_boxing_params()
    render_batch_allocation_editor()
    render_cart()

    # ── In edit mode: show add-line panel for the order being edited ──────
    if _ws_edit_oid_top:
        try:
            from modules.sql_adapter import run_query as _rq_em
            from modules.backoffice.order_line_adder import (
                render_add_line_panel, render_line_delete_panel, render_mirror_panel)
            import json as _jem
            _em_lines = _rq_em("""
                SELECT ol.id AS line_id, ol.order_id, ol.product_id,
                       p.product_name, p.brand, ol.eye_side,
                       ol.sph, ol.cyl, ol.axis, ol.add_power,
                       ol.quantity, ol.unit_price, ol.total_price,
                       ol.lens_params, ol.billed_qty, ol.allocated_qty
                FROM order_lines ol
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
            """, {"oid": _ws_edit_oid_top}) or []
            for _el in _em_lines:
                _raw = _el.get("lens_params")
                if isinstance(_raw, str):
                    try: _el["lens_params"] = _jem.loads(_raw)
                    except: _el["lens_params"] = {}
                elif not isinstance(_raw, dict):
                    _el["lens_params"] = {}
            # Pass status=PENDING so order_line_adder doesn't block on CONFIRMED
            _em_mock = {
                "id": _ws_edit_oid_top,
                "order_no": _ws_edit_ono_top,
                "status": "PENDING",   # unlock add/delete for edit mode
                "lines": _em_lines,
                "stock_lines": _em_lines,
                "inhouse_lines": [],
                "lab_order_lines": [],
            }
            st.markdown("---")
            st.markdown("##### ✏️ Modify Existing Lines")
            render_line_delete_panel(_em_mock)
            render_mirror_panel(_em_mock)
            render_add_line_panel(_em_mock)
        except Exception as _em_err:
            st.caption(f"Edit panel: {_em_err}")

    # ── End Customer Details (for authenticity card / production) ───────
    with st.expander("🪪 End Customer Details (Authenticity Card)", expanded=False):
        st.caption("These details are stored with the order for lens authenticity card generation in production.")
        _ec_c1, _ec_c2, _ec_c3 = st.columns(3)
        with _ec_c1:
            _ec_name = st.text_input(
                "Customer Name",
                value=st.session_state.get("ws_end_customer_name", ""),
                placeholder="End customer / patient name",
                key="ws_ec_name_input",
            )
            st.session_state["ws_end_customer_name"] = _ec_name
        with _ec_c2:
            _ec_mobile = st.text_input(
                "Mobile Number",
                value=st.session_state.get("ws_end_customer_mobile", ""),
                placeholder="10-digit mobile",
                key="ws_ec_mobile_input",
                max_chars=15,
            )
            st.session_state["ws_end_customer_mobile"] = _ec_mobile
        with _ec_c3:
            _ec_ono = st.text_input(
                "Customer Order / Ref No.",
                value=st.session_state.get("ws_end_customer_order_no", ""),
                placeholder="Retailer's order ref (optional)",
                key="ws_ec_ono_input",
            )
            st.session_state["ws_end_customer_order_no"] = _ec_ono

    render_submit_section()

    # ── Post-confirm order editor ─────────────────────────────────────────
    _lco_w = st.session_state.get("last_confirmed_order")
    if _lco_w and _lco_w.get("id"):
        st.markdown(
            "<div style='color:#a78bfa;font-size:0.72rem;font-weight:700;"
            "letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px'>"
            f"📋 Last Order: {_lco_w.get('order_no','')}</div>",
            unsafe_allow_html=True,
        )
        try:
            from modules.sql_adapter import run_query as _rq_lcow
            from modules.backoffice.order_line_adder import (
                render_mirror_panel, render_add_line_panel,
                render_line_delete_panel)
            _lcow_lines = _rq_lcow("""
                SELECT ol.id AS line_id, ol.order_id, ol.product_id,
                       p.product_name, p.brand, ol.eye_side,
                       ol.sph, ol.cyl, ol.axis, ol.add_power,
                       ol.quantity, ol.unit_price, ol.total_price,
                       ol.lens_params, ol.billed_qty, ol.allocated_qty
                FROM order_lines ol
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
            """, {"oid": _lco_w["id"]}) or []
            import json as _jlcow
            for _ll in _lcow_lines:
                _raw = _ll.get("lens_params")
                if isinstance(_raw, str):
                    try: _ll["lens_params"] = _jlcow.loads(_raw)
                    except: _ll["lens_params"] = {}
                elif not isinstance(_raw, dict):
                    _ll["lens_params"] = {}
            _lcow_mock = {
                "id": _lco_w["id"],
                "order_no": _lco_w.get("order_no", ""),
                "status": "PENDING",
                "lines": _lcow_lines,
                "stock_lines": _lcow_lines,
                "inhouse_lines": [],
                "lab_order_lines": [],
            }
            render_line_delete_panel(_lcow_mock)
            render_mirror_panel(_lcow_mock)
            render_add_line_panel(_lcow_mock)
        except Exception as _lcow_err:
            st.caption(f"Order editor: {_lcow_err}")
    # ──────────────────────────────────────────────────────────────────────
