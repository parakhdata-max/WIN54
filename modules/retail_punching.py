"""
Retail Order Punching
try:
    from modules.ophthalmic_billing import (
        render_ophthalmic_selector as _oph_selector,
        ophthalmic_unit_price as _oph_price,
    )
    _HAS_OPH_BILLING = True
except Exception:
    _oph_selector = None
    _HAS_OPH_BILLING = False
 Module - FIXED VERSION
FEATURES:
- Case ID search with multiple patient handling
- Phone number search with multiple patient handling  
- Power entry with "use same as old Rx" option
- Complete product selection flow (from old working version)
- Proper lens params and boxing params (from old working version)
- Single provisional order flow
"""

import streamlit as st

# ── Power Intelligence (optional) ────────────────────────────────────────────
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
# ─────────────────────────────────────────────────────────────────────────────
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
from modules.db.order_repository import save_order
from typing import Dict, List, Optional, Tuple
from modules.session_manager import reset_after_submit
from modules.quantity_engine import QuantityEngine
# Clinical module
from modules.clinical_exam import (
    initialize_clinical_state,
    render_clinical_examination
)

from modules.core.dedupe_guard import merge_order_lines
try:
    from modules.core.eye_side_normalizer import normalize_eye_side
except ImportError:
    # Fallback: inline normalizer until eye_side_normalizer.py is deployed.
    # Maps O/OTHER/None → B so frame routing works without the external module.
    def normalize_eye_side(v, **_):  # noqa: F811
        if not v:
            return "B"
        _k = str(v).strip().upper()
        if _k in ("R", "RIGHT", "RE"):
            return "R"
        if _k in ("L", "LEFT", "LE"):
            return "L"
        if _k in ("S", "SVC", "SERVICE", "SERVICES"):
            return "SERVICE"
        return "B"  # B / BOTH / O / OTHER / FRAME / None → B
from modules.core.crash_recovery import save_runtime_snapshot, restore_after_crash
from modules.core.persistent_cart import persist_cart, restore_cart
from modules.core.undo_stack import push_undo, undo_last
from modules.core.perf_layer import freeze_heavy_blocks, freeze_ui, unfreeze_ui
from modules.core.session_replay import record_step, export_replay
from modules.pricing.pricing_engine import compute_weighted_price, money
# ✅ Centralized price resolver — picks the right DB column per order type
from modules.core.price_qty_governor import (
    resolve_price            as resolve_price_for_order_type,  # drop-in alias
    normalize_to_pcs_price,
    is_box_product,
    normalize_qty,
    reverse_qty,
    get_pcs_price,
    compute_line_gst,
    check_sync,
    PAIR_TO_PCS,
)

# Import required modules 
try:
    from modules.ui_product_selector import render_product_selector
    from modules.sql_adapter import (
        search_patients,
        read_patients,
        read_patient_visits,
        get_patient_last_visit,
        read_product_batch,
        execute_query,
	    get_power_wise_stock, 
    )
    from modules.batch_manager import (
        check_stock_availability,
        get_batches_fifo,
        allocate_batches_fifo,
        create_allocation_record,
        get_stock_display
    )
    from modules.loaders.ophthalmic_adapter import (
        check_ophthalmic_availability,
        get_ophthalmic_for_punching,
        deduct_ophthalmic_stock,
        promote_rx_to_stock,
        IN_STOCK as OPHL_IN_STOCK,
        RX_ORDER as OPHL_RX_ORDER,
    )
    from modules.core.order_engine import convert_cart_to_order
    from modules.validation_gateway import validate_before_submit
except ImportError:
    from modules.ui_product_selector import render_product_selector
    from modules.sql_adapter import (
        search_patients,
        read_patients,
        read_patient_visits,
        get_patient_last_visit,
        read_product_batch,
        execute_query,
	get_power_wise_stock   # 👈 ADD THIS

    )
    
# ============================================================================
# NEW: CASE ID SEARCH FUNCTIONS
# ============================================================================
def search_cases_by_record_no(search_term: str) -> pd.DataFrame:
    """
    Search patient visits by record_no (Case ID)
    Returns matching cases with patient info
    """
    if not search_term or len(search_term.strip()) == 0:
        return pd.DataFrame()

    sql = """
        SELECT DISTINCT
            pv.record_no,
            p.id as patient_id,
            p.master_name as patient_name,
            p.mobile as mobile_number,
            p.created_at,
            COUNT(pv.id) as visit_count,
            MIN(pv.visit_date) as first_visit_date,
            MAX(pv.visit_date) as last_visit_date
        FROM patient_visits pv
        INNER JOIN patients p ON pv.patient_id = p.id
        WHERE pv.record_no ILIKE %s
        GROUP BY pv.record_no, p.id, p.master_name, p.mobile, p.created_at
        ORDER BY MAX(pv.visit_date) DESC
        LIMIT 20
    """

    params = (f"%{search_term.strip()}%",)

    return execute_query(sql, "case_search", params=params)


def get_all_visits_for_case(record_no: str) -> pd.DataFrame:
    """
    Get all visits for a specific case record_no
    """
    record_clean = record_no.replace("'", "''")
    
    sql = f"""
        SELECT
            pv.id as visit_id,
            pv.patient_id,
            pv.record_no as case_no,
            pv.visit_date,
            pv.visit_name,
            pv.right_sph,
            pv.right_cyl,
            pv.right_axis,
            pv.right_add as right_add_power,
            pv.left_sph,
            pv.left_cyl,
            pv.left_axis,
            pv.left_add as left_add_power,
            pv.created_at,
            p.master_name as patient_name,
            p.mobile as mobile_number
        FROM patient_visits pv
        INNER JOIN patients p ON pv.patient_id = p.id
        WHERE pv.record_no = '{record_clean}'
        ORDER BY pv.visit_date DESC, pv.created_at DESC
    """
    
    return execute_query(sql, "case_visits")

def apply_visit_power_to_rx(visit_row):
    """
    Auto-fill power details from selected visit to Old Rx.
    Sanitizes raw DB / pandas values (NaN, None, 'NaN') to clean
    floats/ints before storing, so number_input widgets never see NaN.
    """
    def _rx_dict(sph_key, cyl_key, axis_key, add_key):
        return {
            'sph':  _clean_rx_val(visit_row.get(sph_key)),
            'cyl':  _clean_rx_val(visit_row.get(cyl_key)),
            'axis': _clean_rx_val(visit_row.get(axis_key), is_axis=True),
            'add':  _clean_rx_val(visit_row.get(add_key)),
        }

    rx_r = _rx_dict('right_sph', 'right_cyl', 'right_axis', 'right_add_power')
    rx_l = _rx_dict('left_sph',  'left_cyl',  'left_axis',  'left_add_power')

    # Right Eye — populate old Rx AND new Rx (user can override)
    st.session_state.retail_old_rx_r = rx_r
    st.session_state.retail_new_rx_r = dict(rx_r)

    # Left Eye — populate old Rx AND new Rx
    st.session_state.retail_old_rx_l = rx_l
    st.session_state.retail_new_rx_l = dict(rx_l)

    # 🩺 Load clinical for this visit
    from modules.clinical_exam import load_clinical_examination
    load_clinical_examination(
        visit_row.get("patient_id"),
        visit_row.get("visit_id")
    )

# ============================================================================
# SESSION STATE INITIALIZATION
# ============================================================================

def _render_post_save_actions(order_no, party, mobile, total, order_type, advance=0.0, delivery="", on_account=True, lines=None):
    try:
        from modules.post_save_actions import render_post_save_actions
        render_post_save_actions(order_no, party, mobile, total, order_type, advance, delivery, on_account=on_account, lines=lines or [])
    except Exception as _psa_ex:
        import traceback as _tb
        st.warning(f"⚠️ Post-save panel error: {_psa_ex}")
        st.code(_tb.format_exc(), language="text")


def initialize_session_state():
    """Initialize all required session state variables"""
    # 🏷️ Schema version stamp — bump when session structure changes
    st.session_state["_retail_schema_v"] = 3
    defaults = {
        'retail_patient_id': None,
        'retail_patient_name': '',
        'retail_patient_mobile': '',
        'retail_case_no': '',
        'retail_old_rx_r': {},
        'retail_old_rx_l': {},
        'retail_new_rx_r': {},
        'retail_new_rx_l': {},
        'retail_order_lines': [],
        'retail_selected_product': None,
        'retail_show_optometry': False,
        'retail_all_patients': None,
        'retail_current_allocation': None,
        'retail_show_batch_editor': False,
        'retail_pending_eyes': [],
        '_retail_finalized_eyes': set(),   # tracks R/L already added this product selection
        
        # NEW: Quantity Engine - Single Source of Truth
        'retail_final_qty_R': 0,
        'retail_final_qty_L': 0,
        
        # NEW: Track current provisional order
        'retail_provisional_order_id': None,
        'retail_provisional_order_created_at': None,
        
        # NEW: Last order snapshot for reuse
        'last_order_snapshot': [],
        
        # NEW: Case ID search state
        'retail_search_mode': 'Patient Name',
        'retail_case_search_results': None,
        'retail_selected_case_record_no': None,
        'retail_case_visits': None,
        'retail_selected_visit_id': None,

        # Lens Parameters (job-card, shared across both eyes)
        'retail_lens_params': {
            'frame_type': '',
            'thickness': '',
            'tinted': '',
            'corridor': '',
            'diameter': '',
            'fitting_height': '',
            'instructions': '',
        },

        # Boxing / Frame measurements (shared across both eyes)
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
    
    # Deep cleanup every 50 reloads to prevent session state bloat
    if st.session_state.get("_gc_counter", 0) > 50:
        for k in list(st.session_state.keys()):
            if (
                k.startswith("retail_qe_")
                or k.startswith("batch_qty_")
            ):
                del st.session_state[k]
        
        st.session_state["_gc_counter"] = 0
    else:
        st.session_state["_gc_counter"] = st.session_state.get("_gc_counter", 0) + 1
    
    # Safety cleanup
    if len(st.session_state) > 500:
        for k in list(st.session_state.keys()):
            if k.startswith("retail_qe_"):
                del st.session_state[k]
    
    # Hard cleanup every reload
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
    # 🔒 HARD GUARANTEE KEYS (never allow missing)
    # Runs AFTER defaults loop AND after crash/undo restores that may skip keys
    mandatory_keys = {
        "retail_selected_product": None,
        "retail_order_lines": [],
        "retail_current_allocation": None,
        "retail_show_batch_editor": False,
    }
    for k, v in mandatory_keys.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # Clinical state
    initialize_clinical_state()
# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def _stamp_cart_line_discount(line: dict) -> None:
    """
    Stamp discount + recalculate GST on net price immediately at add-to-cart.
    For RETAIL: MRP is inclusive — discount reduces net, GST back-calculated on net.
    Zero-risk: any failure leaves line unchanged.
    """
    try:
        from modules.pricing.discount_engine import apply_discounts
        # Retail: match on order_type/channel. Patient-specific rules not supported
        # (patients ≠ parties). Promo code rules are fully supported.
        _pc_r = str(st.session_state.get("_retail_promo_code") or "").strip()
        if _pc_r:
            line["promo_code"] = _pc_r
        apply_discounts([line], party_id="", order_type="RETAIL")
        disc = float(line.get("discount_amount") or 0)
        if disc > 0:
            gross = float(line.get("total_price") or 0)
            net   = round(gross - disc, 2)
            line["billing_total"] = net
            # Re-stamp GST on net price
            _stamp_line_tax(line, "RETAIL")
    except Exception:
        pass


def _hydrate_product_gst(product_row: dict) -> dict:
    """Fetch gst_percent + prices from inventory_stock (prices live there, not in products)."""
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
    # Not cached, query DB
    try:
        _df = execute_query(
            """
            SELECT p.gst_percent,
                   COALESCE(p.discount_percent, 0)    AS discount_percent,
                   COALESCE(MAX(i.selling_price), 0)  AS selling_price,
                   COALESCE(MAX(i.mrp), 0)            AS mrp,
                   COALESCE(MAX(i.purchase_rate), 0)  AS purchase_rate
            FROM products p
            LEFT JOIN inventory_stock i
                   ON i.product_id = p.id
                  AND COALESCE(i.is_active, TRUE) = TRUE
            WHERE p.id = %s
            GROUP BY p.gst_percent, p.discount_percent
            LIMIT 1
            """,
            "retail_gst_hydrate", params=(str(pid),))
        if _df is not None and not _df.empty:
            _r = _df.iloc[0]
            product_row["gst_percent"]      = float(_r.get("gst_percent") or 0)
            product_row["discount_percent"] = float(_r.get("discount_percent") or 0)
            if not float(product_row.get("selling_price") or 0):
                product_row["selling_price"] = float(_r.get("selling_price") or 0)
            if not float(product_row.get("mrp") or 0):
                product_row["mrp"] = float(_r.get("mrp") or 0)
            if not float(product_row.get("purchase_rate") or 0):
                product_row["purchase_rate"] = float(_r.get("purchase_rate") or 0)
            # Cache the fetched data
            st.session_state[cache_key] = {
                "gst_percent": product_row["gst_percent"],
                "discount_percent": product_row["discount_percent"],
                "selling_price": product_row["selling_price"],
                "mrp": product_row["mrp"],
                "purchase_rate": product_row["purchase_rate"],
            }
    except Exception:
        pass
    return product_row


def _stamp_line_tax(line: dict, order_type: str = "RETAIL") -> dict:
    """
    Pass one cart line through tax_engine.apply_taxes().
    Writes gst_amount (and gst_percent if 0) onto the line dict.
    Mirrors wholesale_punching._stamp_line_tax but defaults to RETAIL.
    """
    if not float(line.get("gst_percent") or 0):
        pid = line.get("product_id")
        if pid:
            try:
                _df = execute_query(
                    "SELECT gst_percent FROM products WHERE id = %s LIMIT 1",
                    "retail_stamp_gst_hydrate", params=(str(pid),),
                )
                if _df is not None and not _df.empty:
                    _fetched = float(_df.iloc[0].get("gst_percent") or 0)
                    if _fetched:
                        line["gst_percent"] = _fetched
            except Exception:
                pass
    try:
        from modules.pricing.tax_engine import apply_taxes
        pseudo_order = {"order_type": order_type, "lines": [line], "net_value": float(line.get("total_price") or 0)}
        apply_taxes(pseudo_order)
    except Exception:
        pct = float(line.get("gst_percent") or 0)
        total = float(line.get("total_price") or 0)
        line["gst_amount"] = round(total * pct / 100, 2)
    return line


def _gst_percent_for_display(line: dict) -> float:
    pct = float(line.get("gst_percent") or 0)
    if pct:
        return pct
    pid = line.get("product_id")
    if not pid:
        return 0.0
    # Cache in session_state so we only query once per product per session
    _cache_key = f"_gst_cache_{pid}"
    if _cache_key in st.session_state:
        return st.session_state[_cache_key]
    try:
        _df = execute_query("SELECT gst_percent FROM products WHERE id = %s LIMIT 1",
                            "retail_gst_display_hydrate", params=(str(pid),))
        result = float(_df.iloc[0].get("gst_percent") or 0) if _df is not None and not _df.empty else 0.0
    except Exception:
        result = 0.0
    st.session_state[_cache_key] = result
    return result


def _build_clinical_sections() -> dict:
    """
    Read clinical data from Streamlit session state.

    Key naming convention (set by clinical_exam module):
        clinical_va_*     → Visual Acuity
        clinical_sle_*    → Slit Lamp Examination
        clinical_ortho_*  → Orthoptic Examination

    Boolean / control keys are skipped (Saved, Mode, Btn, Tag, Doctor).
    Labels are humanised: 'clinical_va_dist_aided_r' → 'Distance Aided R'
    """
    # Keys whose values are booleans or UI-control strings — skip them
    _SKIP_SUFFIXES = {"saved", "mode", "btn", "tag", "doctor_mode", "photo_tag"}

    _VA_LABELS = {
        "dist_unaided_r": "Distance Unaided  R",
        "dist_unaided_l": "Distance Unaided  L",
        "dist_aided_r":   "Distance Aided  R",
        "dist_aided_l":   "Distance Aided  L",
        "near_r":         "Near Vision  R",
        "near_l":         "Near Vision  L",
    }
    _SLE_LABELS = {
        "lids":        "Lids",
        "conjunctiva": "Conjunctiva",
        "cornea":      "Cornea",
        "ac":          "AC",
        "iris":        "Iris",
        "lens":        "Lens",
        "vitreous":    "Vitreous",
    }
    _ORTHO_LABELS = {
        "ct_dist":    "Cover Test (Distance)",
        "ct_near":    "Cover Test (Near)",
        "nystagmus":  "Nystagmus",
        "motility":   "Ocular Motility",
        "convergence":"Convergence",
        "binocular":  "Binocular Balance",
        "worth":      "Worth 4 Dot",
    }

    sections = {
        "va":    {},   # Visual Acuity
        "sle":   {},   # Slit Lamp
        "ortho": {},   # Orthoptic
        "other": {},   # Anything else clinical
    }

    for raw_key, val in st.session_state.items():
        k = raw_key.lower()
        if not k.startswith("clinical_"):
            continue
        # Skip booleans and control flags
        if isinstance(val, bool) or str(val).lower() in ("true","false","none",""):
            continue
        suffix = k[len("clinical_"):]  # strip 'clinical_' prefix
        if any(suffix == s or suffix.endswith("_" + s) for s in _SKIP_SUFFIXES):
            continue

        if suffix.startswith("va_"):
            sub = suffix[3:]  # strip 'va_'
            label = _VA_LABELS.get(sub, sub.replace("_", " ").title())
            sections["va"][label] = val

        elif suffix.startswith("sle_"):
            sub = suffix[4:]
            label = _SLE_LABELS.get(sub, sub.replace("_", " ").title())
            sections["sle"][label] = val

        elif suffix.startswith("ortho_"):
            sub = suffix[6:]
            label = _ORTHO_LABELS.get(sub, sub.replace("_", " ").title())
            sections["ortho"][label] = val

        else:
            label = suffix.replace("_", " ").title()
            sections["other"][label] = val

    return sections


# is_box_product, normalize_to_pcs_price imported from modules.core.price_qty_governor

def _safe_price(val) -> float:
    """Convert a DB price value to float safely. Returns 0.0 for None/NaN/empty."""
    try:
        import math
        f = float(val)
        return 0.0 if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return 0.0

def clear_allocation_state():
    """Clear allocation-related session state but preserve pending eyes"""
    st.session_state.retail_current_allocation = None
    st.session_state.retail_show_batch_editor = False
    st.session_state.pop("_alloc_lock", None)
    # DON'T clear pending_eyes here - it's managed in finalize flow

# ============================================================
# 🔒 INDUSTRIAL RESET ENGINE v2
# Stage-isolated. No cascade. No widget destruction.
# ============================================================

RESET_MAP = {
    "PATIENT": [
        "retail_patient_id",
        "retail_patient_name",
        "retail_patient_mobile",
        "retail_case_no",
        "retail_case_search_results",
        "retail_selected_case_record_no",
        "retail_case_visits",
        "retail_selected_visit_id",
        "retail_search_mode",
        # ✅ FIX: Streamlit widget keys — these hold the visible input values
        # and survive session_state dict clears unless explicitly deleted.
        "case_id_search_input",
        "patient_search_mode",
        "patient_name_dropdown",
        "patient_mobile_dropdown",
        "new_patient_name",
        "new_patient_mobile",
    ],
    "RX": [
        "retail_old_rx_r",
        "retail_old_rx_l",
        "retail_new_rx_r",
        "retail_new_rx_l",
        "use_same_power_R",
        "use_same_power_L",
    ],
    "PRODUCT": [
        "retail_selected_product",
        "retail_final_qty_R",
        "retail_final_qty_L",
        "retail_current_allocation",
        "retail_show_batch_editor",
        "retail_pending_eyes",
    ],
    "CART": [
        "retail_order_lines",
        "retail_provisional_order_id",
        "retail_provisional_order_created_at",
        "retail_delivery_date",
        "retail_delivery_time",
    ],
    "PARAMS": [
        "retail_lens_params",
        "retail_boxing_params",
    ],
}


def industrial_reset(stage: str):
    """
    Stage-isolated reset — only clears its own layer.
    Never touches other layers. Use 'ALL' to wipe everything.
    """
    if stage == "ALL":
        # Preserve edit mode keys before wiping — restore after so an ongoing
        # edit is never accidentally turned into a new order by an internal reset
        _preserve_edit_oid = st.session_state.get("_editing_order_id", "")
        _preserve_edit_ono = st.session_state.get("_editing_order_no", "")
        _preserve_edit_adv = st.session_state.get("_edit_existing_advance", None)
        # Preserve SERVICE lines (consultation fee) — ONLY if consultation session active
        # (_consult_fee_lines is set in app.py when converting consultation → retail)
        # Without this guard, old service lines bleed into the next patient's order.
        _has_active_consult = bool(st.session_state.get("_consult_fee_lines"))
        _preserve_svc_lines = []
        if _has_active_consult:
            _preserve_svc_lines = [
                l for l in (st.session_state.get("retail_order_lines") or [])
                if str(l.get("eye_side","")).upper() in ("SERVICE","S")
                or bool(l.get("is_service_line"))
            ]

        for s in RESET_MAP:
            industrial_reset(s)
        # Also clear product selector and quantity engine widgets
        for k in list(st.session_state.keys()):
            if k.startswith("ps_") or k.startswith("retail_qe_"):
                del st.session_state[k]
        # 🔥 Kill persisted cart AND crash snapshot so neither resurrect old order
        st.session_state.pop("_persistent_cart", None)
        st.session_state.pop("_crash_snapshot", None)

        # Restore edit mode keys if they were set (must survive product/cart resets)
        if _preserve_edit_oid:
            st.session_state["_editing_order_id"] = _preserve_edit_oid
            st.session_state["_editing_order_no"] = _preserve_edit_ono
            if _preserve_edit_adv is not None:
                st.session_state["_edit_existing_advance"] = _preserve_edit_adv

        # Restore consultation service lines — only while consultation session is active
        # Once the order is saved (safe_rerun after save clears _retail_consult_source_id),
        # service lines are NOT restored so the next patient starts fresh.
        _active_consult = bool(st.session_state.get("_retail_consult_source_id",""))
        if _preserve_svc_lines and _active_consult:
            existing = st.session_state.get("retail_order_lines") or []
            existing_ids = {str(l.get("line_id","")) for l in existing}
            for _sl in _preserve_svc_lines:
                if str(_sl.get("line_id","")) not in existing_ids:
                    existing.append(_sl)
            st.session_state["retail_order_lines"] = existing
        # NOTE: Do NOT clear _confirmed_cart_* here — those fingerprints must
        # survive the post-confirm reset so the button stays disabled.
        # They are cleared only in the "New Order" / explicit new-cart path below.
        # ── Clear ALL submit locks (prevents zombie locks after order/reset) ──
        try:
            from modules.utils.submit_guard import clear_all_locks
            clear_all_locks()
        except Exception:
            for k in list(st.session_state.keys()):
                if k.startswith("_lock_"):
                    st.session_state.pop(k, None)
        # Clear CL hint dismissed flag so next patient sees it fresh if calc was used
        st.session_state.pop("_cl_hint_dismissed", None)
        return

    keys = RESET_MAP.get(stage, [])

    # For CART reset: preserve SERVICE/consultation lines ONLY if active consultation
    _svc_preserve = []
    if stage == "CART" and st.session_state.get("_consult_fee_lines"):
        _svc_preserve = [
            l for l in (st.session_state.get("retail_order_lines") or [])
            if str(l.get("eye_side","")).upper() in ("SERVICE","S")
            or bool(l.get("is_service_line"))
        ]

    for k in keys:
        st.session_state.pop(k, None)

    # Restore service lines after CART reset (only if consultation active)
    if stage == "CART" and _svc_preserve:
        st.session_state["retail_order_lines"] = list(_svc_preserve)

    # RX: hard power isolation — no cascade into product/cart/patient
    if stage == "RX":
        # Clear only power layers
        st.session_state["retail_old_rx_r"] = {}
        st.session_state["retail_old_rx_l"] = {}
        st.session_state["retail_new_rx_r"] = {}
        st.session_state["retail_new_rx_l"] = {}
        st.session_state.pop("_rx_powers_confirmed", None)

        # Remove checkboxes
        st.session_state.pop("use_same_power_R", None)
        st.session_state.pop("use_same_power_L", None)

        # 🔥 HARD WIDGET ROTATION
        st.session_state["rx_reset_counter"] = (
            st.session_state.get("rx_reset_counter", 0) + 1
        )

        # 🔥 Kill ghost allocation safely
        st.session_state.pop("retail_current_allocation", None)
        st.session_state.pop("retail_show_batch_editor", None)
        st.session_state["retail_pending_eyes"] = []
        st.session_state.pop("_alloc_lock", None)

        # 🔥 DO NOT TOUCH:
        # - product
        # - cart
        # - patient
    if stage == "PRODUCT":
        st.session_state["reset_product_selector"] = True


# Backward-compatible alias — all existing calls still work
def reset_from_stage(stage: str):
    industrial_reset(stage)


# ============================================================
# 🧠 ERP-GRADE SESSION ISOLATION LAYER
# Prevents invalid cross-state combinations on every render
# ============================================================

def assert_session_integrity():
    # Allocation without product → kill allocation
    if (
        st.session_state.get("retail_current_allocation")
        and not st.session_state.get("retail_selected_product")
    ):
        st.session_state.retail_current_allocation = None
        st.session_state.retail_show_batch_editor = False

    # Ghost batch editor protection
    if (
        st.session_state.get("retail_show_batch_editor")
        and not st.session_state.get("retail_selected_product")
    ):
        st.session_state.retail_show_batch_editor = False



# ============================================================
# ⚡ STREAMLIT RERENDER STABILIZATION
# Prevents double-trigger ghost reruns on button clicks
# ============================================================

def safe_rerun():
    if st.session_state.get("_rerun_in_progress"):
        return
    st.session_state["_rerun_in_progress"] = True
    st.rerun()


def duplicate_current_order():
    """
    Duplicate current order - allows same patient, new Rx/product
    Uses deepcopy to prevent nested dict reference issues
    """
    if not st.session_state.get("retail_order_lines"):
        return
    
    st.session_state.retail_order_lines = copy.deepcopy(
        st.session_state.retail_order_lines
    )
    
    st.session_state.retail_provisional_order_id = None
    st.session_state.retail_provisional_order_created_at = None

def render_retail_controls():
    """
    Global control bar for order management
    """
    st.markdown("### ⚙️ Order Controls")
    
    c1, c2, c3, c4, c5 = st.columns(5)
    
    # Reset Patient only
    with c1:
        if st.button("🔄 Reset Patient", width='stretch'):
            industrial_reset("PATIENT")
            safe_rerun()

    # Reset Power only
    with c2:
        if st.button("🔄 Reset Power", width='stretch'):
            industrial_reset("RX")
            safe_rerun()

    # Reset Product only
    with c3:
        if st.button("🔄 Reset Product", width='stretch'):
            industrial_reset("PRODUCT")
            safe_rerun()

    # Reuse Last Order
    with c4:
        has_last_order = 'last_order_snapshot' in st.session_state and st.session_state.last_order_snapshot
        if st.button("♻️ Reuse Last", width='stretch', disabled=not has_last_order):
            if has_last_order:
                st.session_state.retail_order_lines = list(
                    st.session_state.last_order_snapshot
                )
                safe_rerun()

    # Reset Everything
    with c5:
        if st.button("🧹 New Order", width='stretch'):
            industrial_reset("ALL")
            for _clr_k in (
                "_receipt_snapshot", "_last_receipt_key",
                "_post_save_data", "_show_receipt_top",
                "_editing_order_id", "_editing_order_no",
                "_edit_existing_advance",
            ):
                st.session_state.pop(_clr_k, None)
            for _k in list(st.session_state.keys()):
                if _k.startswith("_confirmed_cart_") or _k.startswith("_receipt_data_"):
                    st.session_state.pop(_k, None)
            safe_rerun()

def create_provisional_order():
    """Create a new provisional order for this cart session"""
    if not st.session_state.get("retail_provisional_order_id"):
        provisional_id = f"PO-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:6]}"
        st.session_state.retail_provisional_order_id = provisional_id
        st.session_state.retail_provisional_order_created_at = datetime.datetime.now().isoformat()
        return provisional_id
    return st.session_state.retail_provisional_order_id

def _is_rx_blank(val) -> bool:
    """True for any value that means 'not recorded': None, NaN, inf, '', 'nan', 'NaN', 'None'."""
    if val is None:
        return True
    try:
        import math
        f = float(val)
        return math.isnan(f) or math.isinf(f)
    except (TypeError, ValueError):
        return str(val).strip().lower() in ('', 'none', 'n/a', 'nan', 'nat')


def _clean_rx_val(val, is_axis: bool = False):
    """
    Sanitize a raw DB/pandas RX value into a clean Python float (or int for axis),
    returning None for any blank/NaN/NaT value.
    Used when storing values into session state.
    """
    if _is_rx_blank(val):
        return None
    try:
        f = float(val)
        if is_axis:
            i = int(f)
            return None if i == 0 else i
        return f
    except (TypeError, ValueError):
        return None


def _fmt_rx_val(val, decimals: int = 2) -> str:
    """
    Format an RX power value (SPH / CYL / ADD) for display.
    Handles None, NaN (float or numpy), 'NaN' string, 0.0 (plano).
    • blank/nan  → '—'
    • +1.50      → '+1.50'
    • -2.75      → '-2.75'
    • 0.0        → '0.00'
    """
    if _is_rx_blank(val):
        return '—'
    try:
        f = float(val)
        if f == 0.0:
            return f'0.{"0" * decimals}'
        return f'+{f:.{decimals}f}' if f > 0 else f'{f:.{decimals}f}'
    except (TypeError, ValueError):
        return str(val)


def _fmt_rx_axis(val) -> str:
    """Format AXIS (integer degrees). Zero / blank / nan → '—'."""
    if _is_rx_blank(val):
        return '—'
    try:
        i = int(float(val))
        return '—' if i == 0 else str(i)
    except (TypeError, ValueError):
        return str(val)


def format_power_label(rx: dict) -> str:
    """Universal power formatter — always shows 2 decimal places with sign."""
    if not rx:
        return ""

    sph  = rx.get("sph")
    cyl  = rx.get("cyl")
    axis = rx.get("axis")
    add  = rx.get("add")

    sph_s  = _fmt_rx_val(sph)
    cyl_s  = _fmt_rx_val(cyl)
    axis_s = _fmt_rx_axis(axis)
    add_s  = _fmt_rx_val(add)

    # Toric
    if cyl not in (None, 0, 0.0):
        return f"{sph_s} / {cyl_s} × {axis_s}"

    # Multifocal
    if add not in (None, 0, 0.0):
        return f"{sph_s} ADD {add_s}"

    # Spherical
    return sph_s


def format_quantity_display(pcs: int, product: dict) -> str:
    """
    Format PCS quantity for display.
    For box products (unit='BOX' and box_size > 1), show as BOX + PCS.
    For all other products, show as PCS.
    
    Internal storage is ALWAYS in PCS regardless of display format.
    """
    # Handle zero quantity - always show as PCS
    if pcs <= 0:
        return "0 PCS"
    
    # Only convert to BOX display if it's a box product
    if is_box_product(product):
        box_size = int(product.get('box_size', 0) or 0)
        boxes = pcs // box_size
        loose = pcs % box_size

        if loose == 0:
            return f"{boxes} BOX"
        elif boxes == 0:
            return f"{loose} PCS"
        else:
            return f"{boxes} BOX + {loose} PCS"

    return f"{pcs} PCS"


def clear_provisional_order():
    """Clear provisional order after confirmation"""
    st.session_state.retail_provisional_order_id = None
    st.session_state.retail_provisional_order_created_at = None
    st.session_state.retail_order_lines = []
    st.session_state.retail_lens_params = {
        'frame_type': '', 'thickness': '', 'tinted': '',
        'corridor': '', 'diameter': '', 'fitting_height': '',
        'instructions': '',
    }
    st.session_state.retail_boxing_params = {
        'a_box': None, 'b_box': None, 'ed': None, 'ed_axis': None,
        'dbl': None, 'r_pd': None, 'l_pd': None, 'ipd': None,
        'fitting_ht_r': None, 'fitting_ht_l': None,
        'panto': None, 'tilt': None, 'bvd': None,
    }

def set_patient_from_record(patient):
    """Set patient information from database record"""
    # Force patient_id to plain Python str — DB may return uuid.UUID object
    # or pandas/numpy type; isinstance(uuid_obj, str) == False breaks _pid_is_valid
    _raw_id = patient.get('patient_id') or patient.get('id')
    _new_pid = str(_raw_id).strip() if _raw_id is not None else None

    # Clear old receipt if switching to a different patient
    _cur_pid = st.session_state.get("retail_patient_id")
    if _cur_pid and _new_pid and _cur_pid != _new_pid:
        st.session_state.pop("_receipt_snapshot", None)
        st.session_state.pop("_last_receipt_key", None)
        for _k in list(st.session_state.keys()):
            if _k.startswith("_confirmed_cart_"):
                st.session_state.pop(_k, None)

    st.session_state.retail_patient_id = _new_pid
    st.session_state.retail_patient_name = patient.get('patient_name') or patient.get('master_name', '')
    st.session_state.retail_patient_mobile = patient.get('mobile_number') or patient.get('mobile', '')
    
    # 🔥 HARD RESET CROSS-SEARCH STATE (Prevents infinite loop from stale case search)
    # When selecting from name/mobile search, clear any lingering case search state
    st.session_state.retail_case_no = ''
    st.session_state.retail_selected_case_record_no = None
    st.session_state.retail_case_visits = None
    st.session_state.retail_selected_visit_id = None
    
    # 🩺 AUTO-LOAD CLINICAL DATA (Step 8)
    from modules.clinical_exam import load_clinical_examination
    load_clinical_examination(
        st.session_state.retail_patient_id,
        st.session_state.get("retail_selected_visit_id")
    )

def clear_loaded_visit():
    """Clear auto-loaded visit power and go back to manual entry"""

    st.session_state.retail_selected_visit_id = None

    st.session_state.retail_old_rx_r = {}
    st.session_state.retail_old_rx_l = {}

    st.session_state.retail_new_rx_r = {}
    st.session_state.retail_new_rx_l = {}

    # Reset use-same checkboxes
    st.session_state.pop("use_same_power_R", None)
    st.session_state.pop("use_same_power_L", None)


# ============================================================================
# CASE ID SEARCH UI
# ============================================================================

def render_case_id_search():
    """
    Render Case ID search with dropdown
    Shows all visits for matching cases
    """
    st.markdown("#### 🔍 Case ID Search")
    
    col1, col2 = st.columns([3, 1])
    
    with col1:
        case_search_term = st.text_input(
            "Search by Case ID (Record No)",
            placeholder="Type Case ID to search...",
            key="case_id_search_input",
            help="Start typing to see matching cases"
        )
    
    with col2:
        if st.button("🔄 Clear Search", width='stretch'):
            st.session_state.retail_case_search_results = None
            st.session_state.retail_selected_case_record_no = None
            st.session_state.retail_case_visits = None
            st.session_state.retail_selected_visit_id = None
            st.rerun()
    
    # Search for cases
    if case_search_term and len(case_search_term) >= 1:
        with st.spinner("Searching cases..."):
            search_results = search_cases_by_record_no(case_search_term)
            st.session_state.retail_case_search_results = search_results
        
        if not search_results.empty:
            st.markdown(f"**Found {len(search_results)} matching case(s):**")
            
            # HANDLE MULTIPLE PATIENTS WITH SAME CASE ID
            for idx, row in search_results.iterrows():
                case_col1, case_col2, case_col3 = st.columns([2, 2, 1])
                
                with case_col1:
                    st.write(f"**📋 Case:** {row['record_no']}")
                    st.caption(f"Patient: {row['patient_name']}")
                
                with case_col2:
                    st.write(f"**📱 Mobile:** {row['mobile_number']}")
                    st.caption(f"Last Visit: {row['last_visit_date']}")
                
                with case_col3:
                    if st.button("Select", key=f"select_case_{idx}", width='stretch'):
                        st.session_state.retail_selected_case_record_no = row['record_no']
                        st.session_state.retail_patient_id = str(row['patient_id']).strip() if row['patient_id'] is not None else None
                        st.session_state.retail_patient_name = row['patient_name']
                        st.session_state.retail_patient_mobile = row['mobile_number']
                        st.session_state.retail_case_no = row['record_no']
                        st.rerun()
                
                st.markdown("---")
        else:
            st.info("No cases found matching this search term.")
    
    # Show all visits for selected case
    if st.session_state.get("retail_selected_case_record_no"):
        # Visit history now shown in tabs via render_patient_info_display
        # render_case_visits_selection()  # suppressed - handled by tabs
        pass

def render_case_visits_selection():
    """
    Display all visits for selected case
    Allow user to select a visit and auto-fill power details
    """
    st.markdown("---")
    st.markdown(f"#### 📝 Visits for Case: {st.session_state.retail_selected_case_record_no}")
    
    if st.session_state.get("retail_case_visits") is None:
        with st.spinner("Loading visits..."):
            visits = get_all_visits_for_case(st.session_state.retail_selected_case_record_no)
            st.session_state.retail_case_visits = visits
    
    visits_df = st.session_state.retail_case_visits
    
    if visits_df.empty:
        st.warning("No visits found for this case.")
        return
    
    st.info(f"**Patient:** {st.session_state.retail_patient_name} | **Mobile:** {st.session_state.retail_patient_mobile}")
    st.markdown(f"**Total Visits:** {len(visits_df)}")
    
    # Display each visit as expandable card
    for idx, visit in visits_df.iterrows():
        visit_date = visit.get('visit_date', 'N/A')
        visit_name = visit.get('visit_name', 'Visit')
        
        with st.expander(
            f"🗓️ {visit_name} - {visit_date}",
            expanded=(idx == 0)  # Expand first (latest) visit by default
        ):
            # ── Compact RX display — R and L in one line ──────────────────
            rs = _fmt_rx_val(visit.get('right_sph')); rc = _fmt_rx_val(visit.get('right_cyl'))
            ra = _fmt_rx_axis(visit.get('right_axis')); rad = _fmt_rx_val(visit.get('right_add_power'))
            ls = _fmt_rx_val(visit.get('left_sph'));  lc = _fmt_rx_val(visit.get('left_cyl'))
            la = _fmt_rx_axis(visit.get('left_axis')); lad = _fmt_rx_val(visit.get('left_add_power'))

            _add_r = f" ADD {rad}" if rad != '—' else ''
            _add_l = f" ADD {lad}" if lad != '—' else ''

            st.markdown(
                f"<div style='background:#f8fafc;border-radius:6px;padding:8px 12px;"
                f"font-size:12px;margin-bottom:8px'>"
                f"<table style='width:100%;border-collapse:collapse'>"
                f"<tr style='color:#475569;font-size:11px'>"
                f"<th style='padding:2px 8px;text-align:left'>Eye</th>"
                f"<th style='padding:2px 8px'>SPH</th>"
                f"<th style='padding:2px 8px'>CYL</th>"
                f"<th style='padding:2px 8px'>AXIS</th>"
                f"<th style='padding:2px 8px'>ADD</th>"
                f"<th style='padding:2px 8px'>VA</th>"
                f"</tr>"
                f"<tr style='background:#eff6ff'>"
                f"<td style='padding:3px 8px;font-weight:700;color:#1e40af'>R</td>"
                f"<td style='padding:3px 8px;text-align:center;font-weight:600'>{rs}</td>"
                f"<td style='padding:3px 8px;text-align:center;font-weight:600'>{rc}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{ra}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{rad}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{visit.get('va_distance_aided_r','—') or '—'}</td>"
                f"</tr>"
                f"<tr style='background:#f0fdf4'>"
                f"<td style='padding:3px 8px;font-weight:700;color:#166534'>L</td>"
                f"<td style='padding:3px 8px;text-align:center;font-weight:600'>{ls}</td>"
                f"<td style='padding:3px 8px;text-align:center;font-weight:600'>{lc}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{la}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{lad}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{visit.get('va_distance_aided_l','—') or '—'}</td>"
                f"</tr>"
                f"</table></div>",
                unsafe_allow_html=True
            )

            # Clinical summary (collapsible)
            visit_id_for_clinical = visit.get('visit_id')
            if visit_id_for_clinical:
                with st.expander("🩺 Clinical findings", expanded=False):
                    try:
                        from modules.clinical_exam import render_clinical_summary_in_history
                        render_clinical_summary_in_history(
                            st.session_state.retail_patient_id,
                            visit_id_for_clinical
                        )
                    except Exception as _ce:
                        st.caption(f"No detailed clinical findings saved")

            _col_btn = st.columns([3, 1])
            with _col_btn[1]:
                visit_id = visit['visit_id']
                is_selected = (
                    st.session_state.get("retail_selected_visit_id") == visit_id
                )

                button_label = "🟢 Using This Visit" if is_selected else "✅ Use This Visit"

                if st.button(
                    button_label,
                    key=f"use_visit_{visit_id}",
                    width='stretch',
                    help="Load / Remove power from this visit"
                ):

                    # If already selected → UNDO
                    if is_selected:

                        clear_loaded_visit()
                        st.info("↩️ Returned to manual power entry")

                    # Else → Apply visit
                    else:

                        apply_visit_power_to_rx(visit)
                        st.session_state.retail_selected_visit_id = visit_id

                        st.success(f"✅ Power loaded from visit: {visit_name}")

                    st.rerun()


# ============================================================================
# PATIENT SELECTION
# ============================================================================

def _resolve_patient_scan(code: str) -> dict:
    """
    Resolve a scanned patient barcode to a patient record.
    Tries: barcode column → record_no → mobile exact match.
    Returns patient dict or None.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                id::text        AS patient_id,
                master_name     AS patient_name,
                mobile,
                ''                         AS relation,
                COALESCE(gender,'')        AS gender,
                record_no,
                barcode
            FROM patients
            WHERE UPPER(TRIM(COALESCE(barcode,''))) = %s
               OR UPPER(TRIM(COALESCE(record_no,''))) = %s
               OR TRIM(mobile) = %s
               -- Also resolve old/merged patient IDs via alias table
               OR id IN (
                   SELECT master_id FROM patient_barcode_alias
                   WHERE UPPER(TRIM(COALESCE(old_record_no,''))) = %s
                      OR old_barcode = %s
               )
            ORDER BY
                CASE WHEN UPPER(TRIM(COALESCE(barcode,''))) = %s THEN 0
                     WHEN UPPER(TRIM(COALESCE(record_no,''))) = %s THEN 1
                     ELSE 2 END
            LIMIT 1
        """, (code, code, code, code, code)) or []
        return rows[0] if rows else None
    except Exception:
        return None


def render_patient_selection():
    """Render patient selection with Case ID, Name, Phone, Barcode search"""

    # ── EDIT MODE: patient is locked — no search, no change ──────────────────
    _ps_edit_oid = st.session_state.get("_editing_order_id","")
    _ps_edit_ono = st.session_state.get("_editing_order_no","")
    if _ps_edit_oid:
        _pn = st.session_state.get("retail_patient_name","—")
        _pm = st.session_state.get("retail_patient_mobile","")
        st.markdown(
            f"<div style='background:#0f1e2e;border:1px solid #3b82f6;border-radius:8px;"
            f"padding:10px 14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center'>"
            f"<div><span style='font-size:1rem;font-weight:700;color:#93c5fd'>👤 {_pn}</span>"
            f"<span style='color:#64748b;font-size:0.82rem;margin-left:10px'>📞 {_pm or '—'}</span></div>"
            f"<span style='background:#1e3a5f;color:#60a5fa;padding:2px 10px;border-radius:12px;"
            f"font-size:0.7rem;font-weight:700'>✏️ EDITING {_ps_edit_ono}</span></div>",
            unsafe_allow_html=True,
        )
        render_patient_info_display()
        return   # ← no search UI, no Change Patient button in edit mode

    # ── If patient already selected (from consultation prefill or previous) ──
    if st.session_state.get("retail_patient_id") and st.session_state.get("retail_patient_name"):
        _pn  = st.session_state.retail_patient_name
        _pm  = st.session_state.get("retail_patient_mobile","")
        st.markdown(
            f"<div style='background:#f0fdf4;border:1px solid #22c55e;border-radius:8px;"
            f"padding:8px 14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center'>"
            f"<div><span style='font-size:1rem;font-weight:700;color:#166534'>👤 {_pn}</span>"
            f"<span style='color:#64748b;font-size:0.82rem;margin-left:10px'>📞 {_pm or '—'}</span></div>"
            f"<span style='font-size:0.75rem;color:#22c55e;font-weight:700'>✓ SELECTED</span></div>",
            unsafe_allow_html=True
        )
        if st.button("🔄 Change Patient", key="change_patient_btn", width='content'):
            from modules.retail_punching import industrial_reset
            industrial_reset("PATIENT")
            industrial_reset("RX")
            industrial_reset("CART")
            st.rerun()
        render_patient_info_display()
        return

    _ps_h1, _ps_h2 = st.columns([6, 1])
    with _ps_h1:
        st.markdown("<span style='font-size:1.05rem;font-weight:700;color:#1a1a2e'>👤 Patient Selection</span>", unsafe_allow_html=True)
    with _ps_h2:
        if st.button("🔄", key="refresh_patient_db", help="Reload patient database", width='stretch'):
            st.session_state.retail_all_patients = read_patients(include_temporary=False)
            st.session_state.retail_case_search_results = None
            st.session_state.retail_selected_case_record_no = None
            st.session_state.retail_case_visits = None
            st.rerun()

    # ── Barcode scanner — scan patient card (PAT000001) to auto-select ────────
    sc1, sc2 = st.columns([4, 1])
    with sc1:
        autofocus_scan("Scan")
        _scan_typed = st.text_input(
            "📷 Scan patient card",
            placeholder="Scan PAT barcode or type patient number",
            key="ps_patient_scanner_input",
            label_visibility="collapsed",
        ).strip().upper()
        if _scan_typed:
            st.session_state["ps_patient_scan_val"] = _scan_typed
    with sc2:
        if st.button("✕", key="ps_patient_scan_clear", width='stretch',
                     help="Clear scan"):
            st.session_state.pop("ps_patient_scan_val", None)
            st.session_state.pop("ps_patient_scanner_input", None)
            st.rerun()

    _scan_val = st.session_state.get("ps_patient_scan_val", "")
    if _scan_val:
        _patient_resolved = _resolve_patient_scan(_scan_val)
        if _patient_resolved:
            set_patient_from_record(_patient_resolved)
            st.success(
                f"✅ Patient: **{_patient_resolved.get('patient_name') or _patient_resolved.get('master_name','')}** | "
                f"📞 {_patient_resolved.get('mobile','—')} | "
                f"Barcode: {_scan_val}"
            )
            st.session_state.pop("ps_patient_scan_val", None)
            st.rerun()
        else:
            st.warning(f"⚠️ Patient barcode **{_scan_val}** not found — search manually below")

    import datetime as _dt_ps
    _cache_age_key = "retail_patients_loaded_at"
    _cache_stale = False
    if st.session_state.get("retail_all_patients") is None:
        _cache_stale = True
    else:
        _loaded_at = st.session_state.get(_cache_age_key)
        if _loaded_at:
            _age_mins = (_dt_ps.datetime.now() - _loaded_at).total_seconds() / 60
            if _age_mins > 5:
                _cache_stale = True  # auto-refresh every 5 minutes

    if _cache_stale:
        with st.spinner("Loading patient database..."):
            st.session_state.retail_all_patients = read_patients(include_temporary=False)
            st.session_state[_cache_age_key] = _dt_ps.datetime.now()

    patients_df = st.session_state.retail_all_patients

    search_mode = st.radio(
        "Search by:",
        options=["Case ID", "Name / Mobile", "New Walk-in"],
        horizontal=True,
        key="patient_search_mode"
    )
    st.session_state.retail_search_mode = search_mode

    # Clear new patient form state when user navigates away from New Walk-in
    if search_mode != "New Walk-in":
        st.session_state.pop("new_patient_name",   None)
        st.session_state.pop("new_patient_mobile", None)
        # Clear any name search result cache
        for _k in list(st.session_state.keys()):
            if str(_k).startswith("np_namesearch_"):
                del st.session_state[_k]
        # If retail_patient_id is set but retail_patient_name is also set
        # from new patient form (stale), verify it's a real DB patient
        # Simple guard: if no patient_id or it looks like a TEMP, wipe it
        _cur_pid = str(st.session_state.get("retail_patient_id") or "")
        if _cur_pid.upper().startswith("TEMP-"):
            st.session_state.pop("retail_patient_id",     None)
            st.session_state.pop("retail_patient_name",   None)
            st.session_state.pop("retail_patient_mobile", None)
            st.session_state.pop("retail_case_no",        None)

    if search_mode == "Case ID":
        render_case_id_search()
        return

    if search_mode == "New Walk-in":
        render_new_patient_form()
        return

    if patients_df.empty:
        st.warning("No patients found in database.")
        return

    # ── Smart search: Name OR Mobile in one box ──────────────────────────────
    _srch_raw = st.text_input(
        "🔍 Type name or mobile number",
        placeholder="e.g. Ramesh  or  9876543210",
        key="ps_smart_search",
        label_visibility="collapsed",
    ).strip()

    if not _srch_raw:
        st.caption("Start typing a name or mobile number to search patients.")
        return

    _srch_lo   = _srch_raw.lower()
    _is_mobile = _srch_raw.replace(" ", "").isdigit()

    # Build display label: "Name · Mobile · Case ID"
    def _make_label(row):
        nm  = str(row.get("patient_name") or "—")
        mob = str(row.get("mobile_number") or "—")
        cid = str(row.get("record_no")     or "—")
        return f"{nm}  ·  📱{mob}  ·  🪪{cid}"

    df = patients_df.copy()
    df["_label"] = df.apply(_make_label, axis=1)

    if _is_mobile:
        # Mobile search: starts-with match on mobile, then partial, sorted mobile-first
        _starts = df[df["mobile_number"].fillna("").str.startswith(_srch_raw)]
        _contains = df[
            df["mobile_number"].fillna("").str.contains(_srch_raw, na=False) &
            ~df["mobile_number"].fillna("").str.startswith(_srch_raw)
        ]
        _filtered = pd.concat([_starts, _contains]).drop_duplicates("patient_id")
    else:
        # Name search: starts-with match on name first, then partial
        _starts = df[df["patient_name"].fillna("").str.lower().str.startswith(_srch_lo)]
        _contains = df[
            df["patient_name"].fillna("").str.lower().str.contains(_srch_lo, na=False) &
            ~df["patient_name"].fillna("").str.lower().str.startswith(_srch_lo)
        ]
        _filtered = pd.concat([_starts, _contains]).drop_duplicates("patient_id")

    if _filtered.empty:
        _c1, _c2 = st.columns([3, 1])
        _c1.info(f"No patients found for **'{_srch_raw}'**")
        with _c2:
            if st.button("🔄 Refresh DB", key="ps_refresh_on_empty",
                         use_container_width=True,
                         help="Reload patient list from database"):
                st.session_state.pop("retail_all_patients", None)
                st.session_state.pop("retail_patients_loaded_at", None)
                st.rerun()
        st.caption("If this patient was just created, click Refresh DB above.")
        return

    _labels = _filtered["_label"].tolist()
    _placeholder = f"-- {len(_labels)} result{'s' if len(_labels)!=1 else ''} found --"
    _options = [_placeholder] + _labels

    _chosen_label = st.selectbox(
        "Select patient",
        options=_options,
        key="ps_smart_dropdown",
        label_visibility="collapsed",
    )

    if _chosen_label == _placeholder:
        return

    # Match chosen label back to row
    _row = _filtered[_filtered["_label"] == _chosen_label]
    if _row.empty:
        return
    _patient = _row.iloc[0]

    # ── Same-mobile check: are there other patients sharing this mobile? ────
    _this_mob = str(_patient.get("mobile_number") or "")
    _this_pid = str(_patient.get("patient_id") or "")
    _siblings = patients_df[
        (patients_df["mobile_number"].fillna("") == _this_mob) &
        (patients_df["patient_id"] != _this_pid) &
        (_this_mob != "")
    ]

    if not _siblings.empty:
        # Show all people on this mobile — let user pick which one OR add new
        st.markdown(
            f"<div style='background:#1c1408;border:1.5px solid #f59e0b;"
            f"border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<b style='color:#f59e0b'>📱 Mobile {_this_mob} is shared by {1+len(_siblings)} patients</b>"
            f"<span style='color:#94a3b8;font-size:0.78rem;margin-left:8px'>"
            f"Select who you want, or add a new patient with this number.</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        _all_on_mobile = pd.concat([_row, _siblings]).drop_duplicates("patient_id")
        for _si, _sp in _all_on_mobile.iterrows():
            _sp_name = str(_sp.get("patient_name") or "—")
            _sp_case = str(_sp.get("record_no")    or "—")
            _sp_pid  = str(_sp.get("patient_id")   or "")
            _sp_mob  = str(_sp.get("mobile_number") or "")
            _cc1, _cc2, _cc3 = st.columns([5, 1, 1])
            import pandas as _pd2
            _sp_fv  = _sp.get("first_visit_date") or _sp.get("first_visit")
            _sp_ca  = _sp.get("created_at")
            _sp_fv  = None if (_sp_fv is None or (_pd2.isna(_sp_fv) if not isinstance(_sp_fv, str) else False)) else _sp_fv
            _sp_ca  = None if (_sp_ca is None or (_pd2.isna(_sp_ca) if not isinstance(_sp_ca, str) else False)) else _sp_ca
            _sp_dt  = f"  ·  🗓️ {str(_sp_fv)[:10]}" if _sp_fv else (f"  ·  ➕ {str(_sp_ca)[:10]}" if _sp_ca else "")
            _cc1.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                f"border-radius:6px;padding:7px 12px;margin:2px 0'>"
                f"<span style='color:#93c5fd;font-weight:700;font-size:0.9rem'>{_sp_name}</span>"
                f"<span style='color:#475569;font-size:0.75rem'>"
                f"  ·  🪪 {_sp_case}  ·  📱 {_sp_mob}{_sp_dt}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
            with _cc2:
                if st.button("👤 Use", key=f"sm_use_{_sp_pid[:8]}", type="primary", width="stretch"):
                    set_patient_from_record(_sp.to_dict())
                    st.session_state.retail_selected_case_record_no = None
                    st.session_state.retail_case_visits = None
                    st.session_state.retail_selected_visit_id = None
                    st.rerun()
            with _cc3:
                if st.button("➕ New", key=f"sm_add_{_sp_pid[:8]}", width="stretch",
                             help="Add a new patient record with this same mobile number"):
                    # Pre-fill new patient form with this mobile
                    st.session_state["_prefill_new_mobile"] = _this_mob
                    st.session_state.pop("patient_search_mode", None)
                    st.rerun()

    else:
        # Single unambiguous patient — show confirm bar + Use button
        _c1, _c2 = st.columns([5, 1])
        import pandas as _pd3
        _pt_fv = _patient.get("first_visit_date") or _patient.get("first_visit")
        _pt_ca = _patient.get("created_at")
        _pt_fv = None if (_pt_fv is None or (_pd3.isna(_pt_fv) if not isinstance(_pt_fv, str) else False)) else _pt_fv
        _pt_ca = None if (_pt_ca is None or (_pd3.isna(_pt_ca) if not isinstance(_pt_ca, str) else False)) else _pt_ca
        _pt_dt = f"  ·  🗓️ {str(_pt_fv)[:10]}" if _pt_fv else (f"  ·  ➕ {str(_pt_ca)[:10]}" if _pt_ca else "")
        _c1.markdown(
            f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
            f"border-radius:6px;padding:7px 12px'>"
            f"<span style='color:#93c5fd;font-weight:700'>{_patient.get('patient_name','—')}</span>"
            f"<span style='color:#475569;font-size:0.75rem'>"
            f"  ·  🪪 {_patient.get('record_no','—')}  ·  📱 {_this_mob}{_pt_dt}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        with _c2:
            if st.button("👤 Use", key="ps_single_use", type="primary", width="stretch"):
                set_patient_from_record(_patient.to_dict())
                st.session_state.retail_selected_case_record_no = None
                st.session_state.retail_case_visits = None
                st.session_state.retail_selected_visit_id = None
                st.rerun()

def render_new_patient_form():
    """
    New patient registration.
    Flow: Enter Name + Mobile → Check DB (name AND mobile) → Confirm existing / Create new.
    Create button ALWAYS explicit — never auto-saves on keypress.
    """
    st.markdown(
        "<div style='display:flex;align-items:center;gap:8px;margin-bottom:6px'>"
        "<span style='background:#0369a1;color:#fff;font-size:0.68rem;font-weight:800;"
        "padding:2px 10px;border-radius:20px'>➕ New Patient</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2)
    with col1:
        patient_name = st.text_input("Patient Name *", key="new_patient_name",
                                      placeholder="Full name")

        # ── Duplicate warning ──────────────────────────────────────────────
        if patient_name and len(patient_name.strip()) >= 3:
            from modules.sql_adapter import run_query as _rq_dup
            _existing = _rq_dup(
                "SELECT p.id::text, p.master_name, p.record_no, p.mobile, p.created_at, "
                "(SELECT MIN(visit_date) FROM patient_visits WHERE patient_id=p.id) AS first_visit "
                "FROM patients p "
                "WHERE LOWER(TRIM(p.master_name)) LIKE LOWER(%s) LIMIT 5",
                (f"%{patient_name.strip()}%",)
            ) or []
            if _existing:
                st.warning(
                    f"⚠️ **{len(_existing)} existing patient(s)** found with similar name — "
                    "is this a revisit? Search above instead of creating new."
                )
                for _ep in _existing[:3]:
                    _fv = _ep.get('first_visit'); _ca2 = _ep.get('created_at')
                    _dt = f" | 🗓️ First: {str(_fv)[:10]}" if _fv else (f" | ➕ Added: {str(_ca2)[:10]}" if _ca2 else "")
                    st.caption(
                        f"  📋 {_ep['master_name']} | "
                        f"Rec: {_ep['record_no'] or '—'} | "
                        f"Mob: {_ep['mobile'] or '—'}{_dt}"
                    )

    with col2:
        _prefill_mob = st.session_state.pop("_prefill_new_mobile", None)
        patient_mobile = st.text_input(
            "Mobile Number",
            value=_prefill_mob or "",
            key="new_patient_mobile",
            max_chars=10,
            placeholder="10-digit mobile",
        )

    _name = patient_name.strip()
    _mob  = patient_mobile.strip()

    # Wait for name before doing anything
    if not _name:
        st.caption("Enter patient name and mobile to continue.")
        return

    # ── DB check strategy ────────────────────────────────────────────────────
    # Mobile (10 digits): auto-check on every render — fast indexed lookup
    # Name: ONLY on explicit Search button — avoids LIKE scan on every keystroke
    _dup_by_mobile = []
    _dup_by_name   = []

    # Auto mobile check
    if _mob and len(_mob) == 10:
        try:
            from modules.sql_adapter import run_query as _rq_chk
            _dup_by_mobile = _rq_chk(
                "SELECT p.id::text AS pid, p.master_name, p.record_no, p.mobile, "
                "p.created_at, "
                "(SELECT MIN(visit_date) FROM patient_visits WHERE patient_id=p.id) AS first_visit "
                "FROM patients p WHERE p.mobile = %s LIMIT 5",
                (_mob,)
            ) or []
        except Exception:
            pass

    # Name search — only on explicit button press
    _name_search_key = f"np_namesearch_{hash(_name) % 99999}"
    if not _dup_by_mobile and not _mob:
        if st.button("🔍 Search by Name", key="np_name_search_btn",
                     use_container_width=False):
            st.session_state[_name_search_key] = True

        if st.session_state.get(_name_search_key):
            try:
                from modules.sql_adapter import run_query as _rq_chk2
                _dup_by_name = _rq_chk2(
                    "SELECT p.id::text AS pid, p.master_name, p.record_no, p.mobile, "
                    "p.created_at, "
                    "(SELECT MIN(visit_date) FROM patient_visits WHERE patient_id=p.id) AS first_visit "
                    "FROM patients p WHERE LOWER(p.master_name) LIKE LOWER(%s) LIMIT 5",
                    (f"%{_name}%",)
                ) or []
            except Exception:
                pass

    _all_dups = _dup_by_mobile or _dup_by_name

    # Show existing patients
    if _all_dups:
        _cnt = len(_all_dups)
        _hdr = (f"📱 Mobile {_mob} already registered with {_cnt} patient{'s' if _cnt>1 else ''}"
                if _dup_by_mobile
                else f"🔍 Found {_cnt} patient{'s' if _cnt>1 else ''} with similar name")
        st.markdown(
            f"<div style='background:#1c1408;border:2px solid #f59e0b;"
            f"border-radius:8px;padding:10px 14px;margin:6px 0'>"
            f"<b style='color:#f59e0b'>{_hdr}</b>"
            f"<br><span style='color:#94a3b8;font-size:0.78rem'>"
            f"Select <b style='color:#4ade80'>👤 Use</b> to open existing, "
            f"or click <b style='color:#60a5fa'>✅ Create New</b> below for a new record.</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        for _i, _ex in enumerate(_all_dups):
            _ex_name = str(_ex.get("master_name") or "?")
            _ex_case = str(_ex.get("record_no")   or "—")
            _ex_mob  = str(_ex.get("mobile")       or "—")
            _ex_pid  = str(_ex.get("pid")          or "")
            _ca, _cb = st.columns([5, 1])
            _ex_fv  = _ex.get("first_visit")
            _ex_ca  = _ex.get("created_at")
            _ex_date_str = ""
            if _ex_fv:
                _ex_date_str = f"  ·  🗓️ First visit: {str(_ex_fv)[:10]}"
            elif _ex_ca:
                _ex_date_str = f"  ·  ➕ Added: {str(_ex_ca)[:10]}"
            _ca.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                f"border-radius:6px;padding:8px 12px;margin:2px 0'>"
                f"<span style='color:#60a5fa;font-weight:700'>{_ex_name}</span>"
                f"<span style='color:#475569;font-size:.78rem'>"
                f"  ·  🪪 {_ex_case}  ·  📱 {_ex_mob}{_ex_date_str}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
            with _cb:
                if st.button("👤 Use", key=f"np_use_{_i}", type="primary",
                             use_container_width=True):
                    st.session_state["retail_patient_id"]     = _ex_pid
                    st.session_state["retail_patient_name"]   = _ex_name
                    st.session_state["retail_patient_mobile"] = _ex_mob
                    st.session_state["retail_case_no"]        = _ex_case
                    for _k in ("retail_old_rx_r","retail_old_rx_l",
                               "retail_new_rx_r","retail_new_rx_l"):
                        st.session_state[_k] = {}
                    st.session_state.pop("new_patient_name",   None)
                    st.session_state.pop("new_patient_mobile", None)
                    # Switch to Name/Mobile mode — patient selected, no longer in new form
                    st.session_state.pop("patient_search_mode", None)
                    st.session_state["retail_search_mode"]  = "Name / Mobile"
                    st.rerun()

        st.markdown(
            "<div style='color:#64748b;font-size:0.75rem;margin:6px 0 4px'>"
            "Not the right patient? Click Create New below to add a separate record."
            "</div>", unsafe_allow_html=True,
        )
        st.markdown("---")

    # Create button — ALWAYS explicit, NEVER auto-triggered
    if not _mob:
        st.caption("⚠️ No mobile — patient will be created without a mobile number.")

    if st.button("✅ Create New Patient", type="primary", key="np_create_btn",
                 use_container_width=True, disabled=not _name):
        try:
            from modules.sql_adapter import run_query as _rq_np, run_write as _rw_np
            import uuid as _uuid_np, datetime as _dt_np, re as _re_np

            _new_pid = str(_uuid_np.uuid4())
            _mob_save = _mob or None

            # Case number
            try:
                _last    = _rq_np("SELECT record_no FROM patients ORDER BY created_at DESC LIMIT 1") or []
                _last_no = str(_last[0]["record_no"] or "") if _last else ""
                _nums    = _re_np.findall(r'\d+', _last_no)
                _next_n  = int(_nums[-1]) + 1 if _nums else 1
                _case_no = f"NEW/{_dt_np.date.today().strftime('%y')}/{str(_next_n).zfill(4)}"
            except Exception:
                _case_no = f"NEW/{_dt_np.datetime.now().strftime('%Y%m%d%H%M%S')}"

            # If same mobile exists — USE that patient (don't create duplicate)
            _existing_pid = None
            if _mob_save:
                try:
                    _emr = _rq_np(
                        "SELECT id::text AS pid, record_no, master_name FROM patients "
                        "WHERE mobile=%s AND COALESCE(is_temporary,FALSE)=FALSE LIMIT 1",
                        (_mob_save,)
                    ) or []
                    if _emr:
                        _existing_pid  = _emr[0]["pid"]
                        _case_no       = _emr[0].get("record_no") or _case_no
                        _existing_name = str(_emr[0].get("master_name") or "")
                        # If name differs, ask — don't silently update
                        if _existing_name.lower().strip() != _name.lower().strip():
                            st.warning(
                                f"📱 Mobile {_mob_save} is already registered to "
                                f"**{_existing_name}** (Case {_case_no}). "
                                f"Using their record — name updated to **{_name}**."
                            )
                except Exception:
                    pass

            if _existing_pid:
                _new_pid = _existing_pid
                try:
                    _rw_np("UPDATE patients SET master_name=%s, updated_at=NOW() WHERE id=%s::uuid",
                           (_name, _new_pid))
                except Exception:
                    _rw_np("UPDATE patients SET master_name=%s WHERE id=%s::uuid",
                           (_name, _new_pid))
            else:
                _rw_np(
                    "INSERT INTO patients "
                    "(id, master_name, mobile, record_no, is_temporary, created_at) "
                    "VALUES (%s, %s, %s, %s, FALSE, NOW()) ON CONFLICT (id) DO NOTHING",
                    (_new_pid, _name, _mob_save, _case_no)
                )

            st.session_state.retail_patient_id     = _new_pid
            st.session_state.retail_patient_name   = _name
            st.session_state.retail_patient_mobile = _mob
            st.session_state.retail_case_no        = _case_no
            for _k in ("retail_old_rx_r","retail_old_rx_l",
                       "retail_new_rx_r","retail_new_rx_l"):
                st.session_state[_k] = {}
            # Switch away from New Walk-in form — patient is now selected
            st.session_state.pop("new_patient_name",   None)
            st.session_state.pop("new_patient_mobile", None)
            st.session_state.pop("patient_search_mode", None)
            st.session_state["retail_search_mode"]  = "Name / Mobile"
            # Clear patient cache so newly created patient appears in search
            st.session_state.pop("retail_all_patients", None)
            st.session_state.pop("retail_patients_loaded_at", None)
            st.success(f"✅ Patient {'updated' if _existing_pid else 'created'}: "
                       f"{_name} · Case {_case_no}")
            st.rerun()

        except Exception as _np_err:
            import traceback as _tb_np
            st.error(f"❌ Patient save failed: {_np_err}")
            with st.expander("Show error detail"):
                st.code(_tb_np.format_exc())
            # Do NOT set retail_patient_id on failure — keeps form clean
            # User can retry or search for existing patient
def render_all_patient_visits():
    """
    Display all visits for selected patient (accessed by patient_id)
    Used for Name and Mobile search methods
    """
    if not st.session_state.get("retail_patient_id"):
        return
    
    st.markdown(f"<div style='background:#eff6ff;border-left:3px solid #3b82f6;padding:4px 10px;border-radius:4px;margin:6px 0'><b>📝 Visit History — {st.session_state.retail_patient_name}</b></div>", unsafe_allow_html=True)
    
    # Load visits by patient_id
    with st.spinner("Loading visit history..."):
        visits = read_patient_visits(st.session_state.retail_patient_id)
    
    if visits.empty:
        st.info("No visit history found for this patient.")
        return
    
    st.markdown(f"**Total Visits:** {len(visits)}")
    
    # Display each visit as expandable card
    for idx, visit_row in visits.iterrows():
        visit_date = visit_row.get('visit_date', 'N/A')
        visit_name = visit_row.get('visit_name', 'Visit')
        case_no = visit_row.get('case_no', 'N/A')
        
        with st.expander(
            f"🗓️ {visit_name} - {visit_date} (Case: {case_no})",
            expanded=(idx == 0)
        ):
            rs=_fmt_rx_val(visit_row.get('right_sph')); rc=_fmt_rx_val(visit_row.get('right_cyl'))
            ra=_fmt_rx_axis(visit_row.get('right_axis')); rad=_fmt_rx_val(visit_row.get('right_add_power'))
            ls=_fmt_rx_val(visit_row.get('left_sph'));  lc=_fmt_rx_val(visit_row.get('left_cyl'))
            la=_fmt_rx_axis(visit_row.get('left_axis')); lad=_fmt_rx_val(visit_row.get('left_add_power'))

            st.markdown(
                f"<div style='background:#f8fafc;border-radius:6px;padding:8px 12px;"
                f"font-size:12px;margin-bottom:8px'>"
                f"<table style='width:100%;border-collapse:collapse'>"
                f"<tr style='color:#475569;font-size:11px'>"
                f"<th style='padding:2px 8px;text-align:left'>Eye</th>"
                f"<th style='padding:2px 8px'>SPH</th><th style='padding:2px 8px'>CYL</th>"
                f"<th style='padding:2px 8px'>AXIS</th><th style='padding:2px 8px'>ADD</th>"
                f"</tr>"
                f"<tr style='background:#eff6ff'>"
                f"<td style='padding:3px 8px;font-weight:700;color:#1e40af'>R</td>"
                f"<td style='padding:3px 8px;text-align:center;font-weight:600'>{rs}</td>"
                f"<td style='padding:3px 8px;text-align:center;font-weight:600'>{rc}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{ra}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{rad}</td>"
                f"</tr>"
                f"<tr style='background:#f0fdf4'>"
                f"<td style='padding:3px 8px;font-weight:700;color:#166534'>L</td>"
                f"<td style='padding:3px 8px;text-align:center;font-weight:600'>{ls}</td>"
                f"<td style='padding:3px 8px;text-align:center;font-weight:600'>{lc}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{la}</td>"
                f"<td style='padding:3px 8px;text-align:center'>{lad}</td>"
                f"</tr></table></div>",
                unsafe_allow_html=True
            )
            if st.button(
                "✅ Use This Visit",
                key=f"use_patient_visit_{visit_row.get('visit_id', idx)}_{idx}",
                width='stretch',
                help="Load power to Old Rx fields"
            ):
                apply_visit_power_to_rx(visit_row)
                st.session_state.retail_case_no = case_no
                st.success(f"✅ Power details loaded from visit: {visit_name}")
                st.rerun()
            
            # 🩺 CLINICAL SUMMARY IN HISTORY (Step 9)
            from modules.clinical_exam import render_clinical_summary_in_history
            render_clinical_summary_in_history(
                st.session_state.retail_patient_id,
                visit_row.get("visit_id")
            )

def render_patient_info_display():
    """Display selected patient with tabbed history."""
    if not st.session_state.get("retail_patient_id"):
        return

    pid  = st.session_state.retail_patient_id
    name = st.session_state.retail_patient_name
    mob  = st.session_state.retail_patient_mobile

    # ── Patient header ────────────────────────────────────────────────────────
    hc1, hc2, hc3 = st.columns([3, 2, 1])
    with hc1:
        st.markdown(
            f"<div style='background:#f0fdf4;border-left:3px solid #22c55e;"
            f"padding:6px 12px;border-radius:4px'>"
            f"<b style='color:#166534'>{name}</b>"
            f"<span style='color:#64748b;font-size:12px;margin-left:8px'>{mob}</span>"
            f"</div>", unsafe_allow_html=True
        )
    with hc2:
        if st.session_state.get("retail_case_no"):
            st.caption(f"Case: {st.session_state.retail_case_no}")
    with hc3:
        if st.button("↩ Change", key="change_patient_info_btn", width='stretch'):
            for k in ["retail_patient_id","retail_patient_name","retail_patient_mobile",
                      "retail_case_no","retail_selected_case_record_no","retail_case_visits"]:
                st.session_state[k] = None if "id" in k or "no" in k or "visits" in k else ""
            st.rerun()

    # ── Patient history — collapsible (expand when needed) ───────────────────
    with st.expander("📋 Patient History  ·  Visits · Clinical · Orders", expanded=False):
        tab_v, tab_c, tab_o = st.tabs(["📋 Visit History", "🩺 Clinical History", "📦 Order History"])

        # ── TAB 1: Visit History ──────────────────────────────────────────────────
        with tab_v:
            _rfrsh_c1, _rfrsh_c2 = st.columns([5, 1])
            with _rfrsh_c2:
                if st.button("🔄", key="refresh_visit_history", help="Reload visit history", width='stretch'):
                    st.rerun()
            try:
                from modules.sql_adapter import run_query as _rq
                visits = _rq("""
                    SELECT id::text AS visit_id,
                           visit_date::text AS visit_date,
                           COALESCE(visit_name, record_no, '') AS visit_name,
                           COALESCE(record_no, '') AS case_no,
                           COALESCE(right_sph::text,'') AS right_sph,
                           COALESCE(right_cyl::text,'') AS right_cyl,
                           COALESCE(right_axis::text,'') AS right_axis,
                           COALESCE(right_add::text,'') AS right_add_power,
                           COALESCE(left_sph::text,'') AS left_sph,
                           COALESCE(left_cyl::text,'') AS left_cyl,
                           COALESCE(left_axis::text,'') AS left_axis,
                           COALESCE(left_add::text,'') AS left_add_power,
                           '' AS va_distance_aided_r,
                           '' AS va_distance_aided_l
                    FROM patient_visits
                    WHERE patient_id = %s
                    ORDER BY visit_date DESC, created_at DESC
                """, (pid,)) or []
            except Exception as _ex:
                st.error(f"Could not load visits: {_ex}")
                visits = []
    
            if not visits:
                st.info("No visits recorded yet.")
            else:
                st.caption(f"{len(visits)} visit(s) on record")
                for i, v in enumerate(visits):
                    _vname = v.get('visit_name') or ''
                    _cno   = v.get('case_no') or ''
                    # Build label: date · type · case_no (if different from type)
                    _label_parts = [v['visit_date']]
                    if _vname:
                        _label_parts.append(_vname)
                    if _cno and _cno != _vname:
                        _label_parts.append(f"Case {_cno}")
                    _expander_title = (
                        f"{'📌' if i==0 else '📅'} " + " · ".join(_label_parts)
                    )
                    with st.expander(_expander_title, expanded=(i == 0)):
                        rs=_fmt_rx_val(v.get('right_sph')); rc=_fmt_rx_val(v.get('right_cyl'))
                        ra=_fmt_rx_axis(v.get('right_axis')); rad=_fmt_rx_val(v.get('right_add_power'))
                        ls=_fmt_rx_val(v.get('left_sph'));  lc=_fmt_rx_val(v.get('left_cyl'))
                        la=_fmt_rx_axis(v.get('left_axis')); lad=_fmt_rx_val(v.get('left_add_power'))
    
                        st.markdown(
                            f"<table style='width:100%;border-collapse:collapse;font-size:12px'>"
                            f"<tr style='background:#f1f5f9;color:#475569'>"
                            f"<th style='padding:3px 8px;text-align:left'>Eye</th>"
                            f"<th style='padding:3px 8px'>SPH</th><th style='padding:3px 8px'>CYL</th>"
                            f"<th style='padding:3px 8px'>AXIS</th><th style='padding:3px 8px'>ADD</th>"
                            f"<th style='padding:3px 8px'>VA</th></tr>"
                            f"<tr style='background:#eff6ff'>"
                            f"<td style='padding:3px 8px;font-weight:700;color:#1e40af'>R</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{rs}</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{rc}</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{ra}</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{rad}</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{v.get('va_distance_aided_r') or '—'}</td>"
                            f"</tr>"
                            f"<tr style='background:#f0fdf4'>"
                            f"<td style='padding:3px 8px;font-weight:700;color:#166534'>L</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{ls}</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{lc}</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{la}</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{lad}</td>"
                            f"<td style='padding:3px 8px;text-align:center'>{v.get('va_distance_aided_l') or '—'}</td>"
                            f"</tr></table>",
                            unsafe_allow_html=True
                        )
                        # Clinical details collapsible
                        with st.expander("🩺 Clinical findings", expanded=False):
                            try:
                                from modules.clinical_exam import render_clinical_summary_in_history
                                render_clinical_summary_in_history(pid, v.get('visit_id'))
                            except Exception:
                                st.caption("No detailed clinical findings saved for this visit.")
    
                        # Use this visit button
                        if st.button(f"✅ Use this visit's power",
                                      key=f"use_v_{v.get('visit_id',i)}",
                                      width='stretch'):
                            from modules.retail_punching import apply_visit_power_to_rx
                            apply_visit_power_to_rx(v)
                            st.success("✅ Power loaded")
                            st.rerun()
    
        # ── TAB 2: Clinical History ───────────────────────────────────────────────
        with tab_c:
            try:
                from modules.sql_adapter import run_query as _rq2
                clinicals = _rq2("""
                    SELECT
                        pc.created_at::date::text           AS date,
                        COALESCE(pv.visit_date::text,'')  AS visit_date,
                        pc.va_distance_unaided_r            AS va_un_r,
                        pc.va_distance_unaided_l            AS va_un_l,
                        pc.va_distance_aided_r              AS va_aid_r,
                        pc.va_distance_aided_l              AS va_aid_l,
                        pc.va_near_r                        AS va_near_r,
                        pc.va_near_l                        AS va_near_l,
                        COALESCE(pc.sle_lids,'')          AS lids,
                        COALESCE(pc.sle_cornea,'')        AS cornea,
                        COALESCE(pc.sle_lens,'')          AS lens,
                        COALESCE(pc.sle_fundus,'')        AS fundus,
                        COALESCE(pc.ortho_cover_test_distance,'') AS cover,
                        COALESCE(pc.ortho_ocular_motility,'')    AS motility,
                        COALESCE(pc.ortho_convergence,'')        AS convergence,
                        COALESCE(pc.ortho_remarks,'')            AS remarks
                    FROM patient_clinicals pc
                    LEFT JOIN patient_visits pv ON pv.id = pc.visit_id
                    WHERE pc.patient_id = %s
                    ORDER BY pc.created_at DESC
                """, (pid,)) or []
            except Exception as _ex:
                clinicals = []
                st.error(f"Could not load clinical history: {_ex}")
    
            if not clinicals:
                st.info("No clinical examination findings recorded yet.")
                st.caption("Clinical findings are saved from the 🩺 Clinical Examination section during a visit.")
            else:
                st.caption(f"{len(clinicals)} clinical examination(s) on record")
                for i, c in enumerate(clinicals):
                    with st.expander(
                        f"🩺 {c.get('visit_date') or c.get('date','—')}",
                        expanded=(i == 0)
                    ):
                        st.markdown("**Visual Acuity**")
                        va_data = {
                            "": ["R", "L"],
                            "Distance Unaided": [c.get('va_un_r','—'), c.get('va_un_l','—')],
                            "Distance Aided":   [c.get('va_aid_r','—'), c.get('va_aid_l','—')],
                            "Near":             [c.get('va_near_r','—'), c.get('va_near_l','—')],
                        }
                        import pandas as _pd
                        st.dataframe(_pd.DataFrame(va_data), hide_index=True,
                                     width='stretch')
    
                        if any([c.get('lids'), c.get('cornea'), c.get('lens')]):
                            st.markdown("**Slit Lamp**")
                            if c.get('lids'):    st.caption(f"Lids: {c['lids']}")
                            if c.get('cornea'):  st.caption(f"Cornea: {c['cornea']}")
                            if c.get('lens'):    st.caption(f"Lens: {c['lens']}")
                            if c.get('fundus'):  st.caption(f"Fundus: {c['fundus']}")
    
                        if any([c.get('cover'), c.get('motility'), c.get('convergence')]):
                            st.markdown("**Orthoptic**")
                            if c.get('cover'):      st.caption(f"Cover test: {c['cover']}")
                            if c.get('motility'):   st.caption(f"Motility: {c['motility']}")
                            if c.get('convergence'):st.caption(f"Convergence: {c['convergence']}")
    
                        if c.get('remarks'):
                            st.markdown("**Remarks**")
                            st.info(c['remarks'])
    
        # ── TAB 3: Order History ──────────────────────────────────────────────────
        with tab_o:
            try:
                from modules.sql_adapter import run_query as _rq3
                import json as _ojson
                _pname = st.session_state.get("retail_patient_name", "")
                orders = _rq3("""
                    SELECT
                        o.id::text                        AS oid,
                        o.order_no,
                        o.order_type,
                        o.created_at::date::text          AS date,
                        o.status,
                        COALESCE(o.is_converted, false)   AS is_converted,
                        COALESCE(o.total_value, 0)        AS value,
                        -- Linked retail order (for converted consultations)
                        (SELECT r.order_no FROM orders r
                         WHERE r.customer_order_no = o.id::text
                           AND COALESCE(r.is_deleted,false)=false
                         LIMIT 1)                         AS linked_retail_no,
                        -- Product + power lines
                        COALESCE(json_agg(
                            json_build_object(
                                'eye',   ol.eye_side,
                                'prod',  p.product_name,
                                'brand', p.brand,
                                'qty',   ol.quantity,
                                'sph',   ol.sph,
                                'cyl',   ol.cyl,
                                'axis',  ol.axis,
                                'add',   ol.add_power,
                                'price', ol.total_price
                            ) ORDER BY ol.eye_side
                        ) FILTER (WHERE ol.id IS NOT NULL), '[]'::json) AS lines,
                        -- Payments: advance paid
                        COALESCE(SUM(pay.amount) FILTER (
                            WHERE COALESCE(pay.is_deleted,false)=false
                        ), 0)                             AS paid
                    FROM orders o
                    LEFT JOIN order_lines ol
                        ON ol.order_id=o.id
                        AND COALESCE(ol.is_deleted,false)=false
                    LEFT JOIN products p ON p.id=ol.product_id
                    LEFT JOIN payments pay
                        ON (pay.order_id=o.id OR pay.advance_for_order_id=o.id)
                        AND COALESCE(pay.is_deleted,false)=false
                    WHERE COALESCE(o.patient_name,'')=%s
                      AND COALESCE(o.is_deleted,false)=false
                    GROUP BY o.id, o.order_no, o.order_type,
                             o.created_at, o.status, o.is_converted, o.total_value
                    ORDER BY o.created_at DESC
                """, (_pname,)) or []
            except Exception as _ex:
                orders = []
                st.error(f"Could not load orders: {_ex}")
    
            if not orders:
                st.info("No orders on record for this patient.")
            else:
                STATUS_COLOR = {
                    "PENDING":"#f59e0b","UNDER_REVIEW":"#f59e0b","CONFIRMED":"#3b82f6",
                    "IN_PRODUCTION":"#8b5cf6","READY":"#10b981","BILLED":"#059669",
                    "DISPATCHED":"#0891b2","DELIVERED":"#166534","CLOSED":"#475569"
                }
    
                def _fmt_pw(sph, cyl, axis, add):
                    parts = []
                    if sph  is not None: parts.append(f"S:{float(sph):+.2f}")
                    if cyl  is not None: parts.append(f"C:{float(cyl):+.2f}")
                    if axis is not None: parts.append(f"A:{int(float(axis))}")
                    if add  is not None: parts.append(f"Add:{float(add):+.2f}")
                    return "  ".join(parts)
    
                def _order_card(o):
                    _sc   = STATUS_COLOR.get(o.get("status",""), "#475569")
                    _val  = float(o.get("value") or 0)
                    _paid = float(o.get("paid") or 0)
                    _bal  = max(_val - _paid, 0)
                    _lines = o.get("lines") or []
                    if isinstance(_lines, str):
                        try:    _lines = _ojson.loads(_lines)
                        except: _lines = []
    
                    st.markdown(
                        f"<div style='background:#1e293b;border-radius:8px;"
                        f"padding:10px 14px;margin-bottom:6px;border-left:3px solid {_sc}'>",
                        unsafe_allow_html=True)
    
                    c1, c2, c3, c4 = st.columns([2.5, 1, 1, 1.5])
                    c1.markdown(
                        f"**{o['order_no']}**  "
                        f"<span style='color:#64748b;font-size:0.7rem'>{o.get('date','')}</span>",
                        unsafe_allow_html=True)
                    c2.markdown(
                        f"<span style='background:{_sc}22;color:{_sc};padding:1px 7px;"
                        f"border-radius:4px;font-size:0.68rem;font-weight:700'>{o.get('status','')}</span>",
                        unsafe_allow_html=True)
                    c3.caption(f"₹{_val:,.0f}")
                    if _paid > 0:
                        bal_html = (f"<br><span style='color:#f59e0b;font-size:0.68rem'>Bal ₹{_bal:,.0f}</span>"
                                    if _bal > 0.5 else
                                    "<br><span style='color:#10b981;font-size:0.68rem'>✅ Paid</span>")
                        c4.markdown(
                            f"<span style='color:#10b981;font-size:0.68rem'>Adv ₹{_paid:,.0f}</span>{bal_html}",
                            unsafe_allow_html=True)
                    else:
                        c4.caption("—")
    
                    # Product + power lines
                    for ln in _lines:
                        if not ln.get("prod"):
                            continue
                        eye   = str(ln.get("eye") or "").upper()
                        ey_lb = {"R":"👁R","L":"👁L","B":"👁👁","SERVICE":"⚙️"}.get(eye, eye)
                        prod  = str(ln.get("prod") or "")
                        brand = str(ln.get("brand") or "")
                        qty   = ln.get("qty") or 0
                        pw    = _fmt_pw(ln.get("sph"),ln.get("cyl"),ln.get("axis"),ln.get("add"))
                        st.markdown(
                            f"<div style='font-size:0.7rem;color:#94a3b8;padding:2px 4px'>"
                            f"{ey_lb} <b style='color:#cbd5e1'>{brand} {prod}</b>"
                            + (f"  <span style='color:#64748b'>{pw}</span>" if pw else "")
                            + f"  Qty:{qty}</div>",
                            unsafe_allow_html=True)
    
                    st.markdown("</div>", unsafe_allow_html=True)
    
                # ── Retail / Wholesale orders ─────────────────────────────────
                real_orders = [o for o in orders
                               if str(o.get("order_type","")).upper() != "CONSULTATION"]
                consults    = [o for o in orders
                               if str(o.get("order_type","")).upper() == "CONSULTATION"]
    
                if real_orders:
                    st.caption(f"📦 {len(real_orders)} order(s)")
                    for o in real_orders:
                        _order_card(o)
    
                # ── Consultations ─────────────────────────────────────────────
                if consults:
                    if real_orders:
                        st.markdown("<hr style='border-color:#1e293b;margin:8px 0'>",
                                    unsafe_allow_html=True)
                    st.caption(f"🩺 {len(consults)} consultation(s)")
                    for o in consults:
                        _linked = o.get("linked_retail_no") or ""
                        # CONVERTED badge only when a real linked retail order exists.
                        # is_converted=TRUE alone is unreliable (set prematurely
                        # if staff clicked Bill but abandoned without saving).
                        _conv   = bool(_linked)
                        _bdr    = "#334155" if _conv else "#6366f1"
                        st.markdown(
                            f"<div style='background:#0f172a;border-radius:8px;"
                            f"padding:8px 14px;margin-bottom:5px;border-left:3px solid {_bdr}'>",
                            unsafe_allow_html=True)
                        c1, c2, c3 = st.columns([3, 2.5, 1])
                        c1.markdown(
                            f"<span style='color:{'#64748b' if _conv else '#e2e8f0'};"
                            f"font-size:0.82rem'>🩺 {o['order_no']}</span>"
                            f"<span style='color:#475569;font-size:0.65rem'> · {o.get('date','')}</span>",
                            unsafe_allow_html=True)
                        if _conv and _linked:
                            c2.markdown(
                                f"<span style='color:#475569;font-size:0.7rem'>"
                                f"🔄 Converted → <b style='color:#6366f1'>{_linked}</b></span>",
                                unsafe_allow_html=True)
                        elif _conv:
                            c2.markdown(
                                "<span style='color:#475569;font-size:0.7rem'>🔄 Converted to Order</span>",
                                unsafe_allow_html=True)
                        else:
                            c2.markdown(
                                "<span style='background:#6366f122;color:#818cf8;"
                                "padding:1px 7px;border-radius:4px;font-size:0.68rem'>"
                                "OPEN CONSULTATION</span>",
                                unsafe_allow_html=True)
                        c3.caption(f"₹{float(o.get('value',0)):,.0f}")
                        st.markdown("</div>", unsafe_allow_html=True)
    
                        # ── Bill button — only for unconverted consultations ──────────
                        if not _conv:
                            _oid_cons = str(o.get("oid", ""))
                            if st.button(
                                "➕ Add Products & Bill",
                                key=f"rp_bill_consult_{_oid_cons[:8]}",
                                width='stretch',
                                help="Pre-loads patient + Rx + consultation fee into cart",
                            ):
                                try:
                                    from modules.consultation import convert_consultation_to_billing
                                    import uuid as _uuid_rp, datetime as _dt_rp
                                    _res = convert_consultation_to_billing(_oid_cons)
                                    if "error" in _res:
                                        if _res.get("already_billed"):
                                            st.info(f"Already billed → {_res.get('billed_order_no','')}")
                                        else:
                                            st.error(f"Error: {_res['error']}")
                                    else:
                                        _rxd = _res.get("rx", {})
                                        _fee = float(_res.get("consult_fee", 0) or 0)
                                        _flines = []

                                        # Check if consultation fee was already collected
                                        # (i.e. advance payment exists for the consultation order)
                                        # If YES → don't add as a line (billing shows as "Previously Paid")
                                        # If NO  → add as billable line so it gets captured in this invoice
                                        _cons_already_paid = False
                                        if _fee > 0 and _oid_cons:
                                            try:
                                                from modules.sql_adapter import run_query as _rq_cadv
                                                # Resolve consultation UUID if needed
                                                _cons_uid = _oid_cons
                                                if not (len(_cons_uid) == 36 and _cons_uid.count("-") == 4):
                                                    _cuid_rows = _rq_cadv(
                                                        "SELECT id::text FROM orders WHERE order_no=%s AND order_type='CONSULTATION' LIMIT 1",
                                                        (_oid_cons,)
                                                    ) or []
                                                    if _cuid_rows: _cons_uid = _cuid_rows[0]["id"]
                                                _cadv = _rq_cadv("""
                                                    SELECT COALESCE(SUM(amount),0) AS tot
                                                    FROM payments
                                                    WHERE advance_for_order_id = %s::uuid
                                                      AND payment_type = 'ADVANCE'
                                                      AND COALESCE(is_deleted,FALSE) = FALSE
                                                """, (_cons_uid,)) or []
                                                _cons_already_paid = float((_cadv[0]["tot"] if _cadv else 0) or 0) >= (_fee - 0.01)
                                            except Exception:
                                                pass

                                        if _fee > 0 and not _cons_already_paid and _res.get("prod_id"):
                                            # Fee NOT yet collected — add as billable line to retail order
                                            _flines = [{
                                                "line_id":            str(_uuid_rp.uuid4()),
                                                "provisional_order_id": None,
                                                "product_id":         _res["prod_id"],
                                                "product_name":       _res.get("prod_name", "Consultation Fee"),
                                                "brand": "Service",   "main_group": "Services",
                                                "batch_no": "",       "eye_side": "SERVICE",
                                                "sph": None, "cyl": None, "axis": None, "add_power": None,
                                                "lens_params": {},    "boxing_params": {},
                                                "requested_qty": 1,   "billing_qty": 1,
                                                "order_qty": 0,       "display_qty": "1 SERVICE",
                                                "batch_allocation": [],
                                                "unit_price":  _fee,  "total_price": _fee,
                                                "gst_percent": 0.0,   "gst_amount": 0.0,
                                                "is_service_line": True, "status": "Complete",
                                                "created_at": _dt_rp.datetime.now().isoformat(),
                                            }]
                                        # If already paid → _flines stays [] 
                                        # Billing panel reads advance from payments table automatically
                                        st.session_state["_consult_prefill"] = {
                                            "patient_name":     _res["patient_name"],
                                            "patient_mobile":   _res.get("patient_mobile", ""),
                                            "patient_id":       _res.get("patient_id", ""),
                                            "consult_order_id": _cons_uid,  # UUID, not order_no
                                            "rx_r": {"sph": _rxd.get("sph_r", 0), "cyl": _rxd.get("cyl_r", 0),
                                                     "axis": _rxd.get("ax_r", 0), "add": _rxd.get("add_r", 0)},
                                            "rx_l": {"sph": _rxd.get("sph_l", 0), "cyl": _rxd.get("cyl_l", 0),
                                                     "axis": _rxd.get("ax_l", 0), "add": _rxd.get("add_l", 0)},
                                            "order_lines": _flines,
                                            "consult_fee": _fee,
                                        }
                                        st.toast(f"Loading {_res['patient_name']}…")
                                        safe_rerun()
                                except Exception as _be:
                                    st.error(f"Error: {_be}")
    
    # ============================================================================
# POWER ENTRY - WITH "USE SAME AS OLD RX" OPTION
# ============================================================================

def render_power_entry():
    """Render power entry section - R and L vertically with single save"""
    if not st.session_state.get("retail_patient_id"):
        return
    

    _pe_hc1, _pe_hc2 = st.columns([6, 1])
    with _pe_hc1:
        st.markdown("<span style='font-size:1.05rem;font-weight:700;color:#ff8c42'>👓 Power Entry</span>", unsafe_allow_html=True)
    with _pe_hc2:
        if st.button("🔄", key="refresh_power_entry", help="Reset power fields", width='stretch'):
            industrial_reset("RX")
            safe_rerun()

    # ── CL Calculator hint — only shown when sidebar calc was used ────────
    _cl = st.session_state.get("_last_cl_result")
    if _cl and not st.session_state.get("_cl_hint_dismissed"):
        _prod  = _cl.get("product", "")
        _r     = _cl.get("R", {})
        _l     = _cl.get("L", {})
        def _fmt(d):
            if not d.get("ok"): return "—"
            s = f"SPH {d['sph']:+.2f}"
            if d.get("cyl"): s += f" / {d['cyl']:+.2f} / {d['axis']}°"
            return s
        _h1, _h2, _h3, _h4 = st.columns([3, 3, 1, 1])
        _h1.caption(f"💡 **{_prod}** · R: {_fmt(_r)}")
        _h2.caption(f"L: {_fmt(_l)}")
        with _h3:
            if st.button("↗ Apply", key="cl_hint_apply",
                         help="Fill power fields with CL result"):
                if _r.get("ok"):
                    st.session_state["retail_new_rx_r"] = {
                        "sph": _r["sph"], "cyl": _r["cyl"],
                        "axis": _r["axis"], "add": 0.0,
                    }
                    # Force widget refresh
                    st.session_state["rx_reset_counter"] = (
                        st.session_state.get("rx_reset_counter", 0) + 1
                    )
                if _l.get("ok"):
                    st.session_state["retail_new_rx_l"] = {
                        "sph": _l["sph"], "cyl": _l["cyl"],
                        "axis": _l["axis"], "add": 0.0,
                    }
                safe_rerun()
        with _h4:
            if st.button("✕", key="cl_hint_dismiss",
                         help="Dismiss"):
                st.session_state["_cl_hint_dismissed"] = True
                safe_rerun()

    # ── Both eyes wrapped in a form so tabbing between fields
    #    In consultation/edit mode: no form wrapper — values update live each rerun.
    #    In billing mode: form wrapper so values only commit on "Save Powers".
    _rc_form = st.session_state.get("rx_reset_counter", 0)
    _in_consult_mode = (
        st.session_state.get("_visit_mode_default", 1) == 1 or
        st.session_state.get("_editing_consult_order_id")
    )

    if _in_consult_mode:
        # No form — widgets write to session_state live on every interaction
        st.markdown("<div style='background:#fff3e0;border-left:3px solid #ff8c42;padding:4px 10px;border-radius:4px;margin:4px 0'><b>👁️ RIGHT EYE</b></div>", unsafe_allow_html=True)
        render_eye_power_section('R', "Right Eye")
        st.markdown("<hr style='margin:6px 0;border-color:#ffe0b2'>", unsafe_allow_html=True)
        st.markdown("<div style='background:#fff3e0;border-left:3px solid #ff8c42;padding:4px 10px;border-radius:4px;margin:4px 0'><b>👁️ LEFT EYE</b></div>", unsafe_allow_html=True)
        render_eye_power_section('L', "Left Eye")
        # Auto-confirm — consultation close Save button handles the actual save
        st.session_state["_rx_powers_confirmed"] = True
    else:
        with st.form(key=f"rx_form_{_rc_form}", border=False):
            st.markdown("<div style='background:#fff3e0;border-left:3px solid #ff8c42;padding:4px 10px;border-radius:4px;margin:4px 0'><b>👁️ RIGHT EYE</b></div>", unsafe_allow_html=True)
            render_eye_power_section('R', "Right Eye")
            st.markdown("<hr style='margin:6px 0;border-color:#ffe0b2'>", unsafe_allow_html=True)
            st.markdown("<div style='background:#fff3e0;border-left:3px solid #ff8c42;padding:4px 10px;border-radius:4px;margin:4px 0'><b>👁️ LEFT EYE</b></div>", unsafe_allow_html=True)
            render_eye_power_section('L', "Left Eye")
            st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
            _submitted = st.form_submit_button(
                "💾 Save Powers",
                type="primary",
                width='stretch',
            )
        if _submitted:
            st.session_state["_rx_powers_confirmed"] = True
    # values written to session_state inside render_eye_power_section

def render_eye_power_section(eye: str, label: str):
    """
    Render power section for one eye
    Shows Old Rx (if available) and option to use same or enter new power
    """
    old_rx_key = f'retail_old_rx_{eye.lower()}'
    new_rx_key = f'retail_new_rx_{eye.lower()}'

    old_rx = st.session_state.get(old_rx_key, {})
    new_rx = st.session_state.get(new_rx_key, {})
    
    # Display Old Rx if available
    if any(old_rx.values()):
        st.markdown(f"<small style='color:#6b7280;font-weight:600'>📝 Old Rx — {label}</small>", unsafe_allow_html=True)
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("SPH", _fmt_rx_val(old_rx.get('sph')))
        with col2:
            st.metric("CYL", _fmt_rx_val(old_rx.get('cyl')))
        with col3:
            st.metric("AXIS", _fmt_rx_axis(old_rx.get('axis')))
        with col4:
            st.metric("ADD", _fmt_rx_val(old_rx.get('add')))
        
        # In consultation edit mode — force checkbox unchecked so fields are editable
        _in_consult_edit = bool(st.session_state.get("_editing_consult_order_id"))
        if _in_consult_edit:
            # Pop key so Streamlit uses value= parameter (not stale session state)
            st.session_state.pop(f"use_same_power_{eye}", None)
        use_same_power = st.checkbox(
            f"✅ Use same power as last Rx",
            key=f"use_same_power_{eye}",
            value=not _in_consult_edit,  # unchecked in edit mode
            help="Check to use the same power, uncheck to enter new power"
        )
        
        if use_same_power:
            # Copy old Rx to new Rx
            st.session_state[new_rx_key] = old_rx.copy()
            st.success(f"✓ Using same power as last Rx")
            return  # Don't show input fields
        else:
            st.info("Enter new power below")
    
    # Show input fields for new power
    st.markdown("<small style='color:#6b7280;font-weight:600'>🎯 New Power</small>", unsafe_allow_html=True)
    
    col1, col2, col3, col4 = st.columns(4)

    # Counter bumped on reset → forces brand-new widgets with value=0 (not old state)
    _rc = st.session_state.get("rx_reset_counter", 0)
    
    with col1:
        sph_value = float(_clean_rx_val(new_rx.get('sph')) or 0.0)
        sph = st.number_input(
            "SPH",
            min_value=-20.0,
            max_value=20.0,
            step=0.25,
            value=sph_value,
            key=f"new_sph_{eye}_{_rc}",
            format="%.2f"
        )
    
    with col2:
        cyl_value = float(_clean_rx_val(new_rx.get('cyl')) or 0.0)
        # FIX: Allow full CYL range -6.0 to +6.0 to handle positive cylinders
        cyl = st.number_input(
            "CYL",
            min_value=-6.0,
            max_value=6.0,
            step=0.25,
            value=cyl_value,
            key=f"new_cyl_{eye}_{_rc}",
            format="%.2f"
        )
    
    with col3:
        axis_value = int(_clean_rx_val(new_rx.get('axis'), is_axis=True) or 0)
        axis = st.number_input(
            "AXIS",
            min_value=0,
            max_value=180,
            step=1,
            value=axis_value,
            key=f"new_axis_{eye}_{_rc}"
        )
    
    with col4:
        add_value = float(_clean_rx_val(new_rx.get('add')) or 0.0)
        add_power = st.number_input(
            "ADD",
            min_value=0.0,
            max_value=3.5,
            step=0.25,
            value=add_value,
            key=f"new_add_{eye}_{_rc}",
            format="%.2f"
        )
    
    current_rx = {
        'sph': sph,
        'cyl': cyl,
        'axis': axis,
        'add': add_power
    }
    
    # Update new Rx in session state
    st.session_state[new_rx_key] = current_rx

    # Track snapshot for change detection (used externally if needed)
    st.session_state[f"_rx_snapshot_{eye}_{_rc}"] = current_rx

# ============================================================================
# PRODUCT SELECTION - FROM OLD WORKING VERSION
# ============================================================================

def render_product_selection():
    """Render product selection - matching old working version exactly"""

    # Prevent product wipe during RX reset
    if st.session_state.get("rx_reset_counter") and not st.session_state.get("retail_selected_product"):
        pass

    # One-time product cache refresh
    if not st.session_state.get("_product_cache_refreshed"):
        st.session_state.retail_selected_product = None
        st.session_state["_product_cache_refreshed"] = True
    
    if st.session_state.get("reset_product_selector"):
        for k in list(st.session_state.keys()):
            if k.startswith("ps_"):
                del st.session_state[k]

        st.session_state["reset_product_selector"] = False


    _ps_hc1, _ps_hc2 = st.columns([6, 1])
    with _ps_hc1:
        st.markdown("<span style='font-size:1.05rem;font-weight:700;color:#ff8c42'>📦 Product Selection</span>", unsafe_allow_html=True)
    with _ps_hc2:
        if st.button("🔄", key="refresh_product_selection", help="Reset product selection", width='stretch'):
            industrial_reset("PRODUCT")
            safe_rerun()
    
    result = render_product_selector()
    
    if result:
        # Handle UUID product_id as string
        current_product_id = str(result['product_row']['product_id'])
        
        if st.session_state.get("retail_selected_product"):
            old_product_id = str(st.session_state.retail_selected_product['product_row']['product_id'])
            if current_product_id != old_product_id:
                clear_allocation_state()
                # Reset quantities when product changes
                st.session_state.retail_final_qty_R = 0
                st.session_state.retail_final_qty_L = 0
                
                # Reset lens and boxing params to avoid leakage
                st.session_state.retail_lens_params = {}
                st.session_state.retail_boxing_params = {}

        # Guard: only update if product actually changed (prevents infinite rerun loop)
        _new_pid_r = str(result['product_row']['product_id'])
        _cur_r = st.session_state.retail_selected_product
        _old_pid_r = str(_cur_r['product_row']['product_id']) if _cur_r else ''
        if _new_pid_r != _old_pid_r:
            st.session_state.retail_selected_product = result
        
        # ===============================
        # PRODUCT ADD FLOW (LENS + OTHER)
        # ===============================

        # Lens / Contact Lens Flow (Needs Rx + Batch)
        if result['is_lens'] or result['is_contact']:

            st.markdown("---")
            st.markdown("### ➕ Add Product to Order")
    
            product = result['product_row']
            product_name = product['product_name']

            st.info(f"**Selected:** {product_name} (ID: {current_product_id})")

            # ===============================
            # POWER-WISE STOCK DISPLAY
            # ===============================

            # Get product id
            pid = current_product_id
            product = result['product_row']

            # Right eye stock
            if st.session_state.get("retail_new_rx_r"):
                rx_r = st.session_state.retail_new_rx_r

                power_label = format_power_label(rx_r) or "No Power"

                # Ophthalmic lens → use adapter (shows STOCK qty OR RX price)
                if result.get('is_lens') and not result.get('is_contact'):
                    # ── NEW: Index + Coating selector ─────────────────────
                    if _HAS_OPH_BILLING and _oph_selector:
                        _oph_spec_r = _oph_selector(
                            product_id   = str(pid),
                            product_name = str(result.get("product_name","")),
                            rx_r         = rx_r,
                            rx_l         = st.session_state.get("retail_new_rx_l", {}),
                            order_type   = "RETAIL",
                            key_prefix   = f"rt_{str(pid)[:8]}",
                        )
                        # Store spec for cart
                        st.session_state[f"_oph_spec_{pid}"] = _oph_spec_r
                    else:
                        # Legacy fallback
                        avail_r = check_ophthalmic_availability(
                            pid,
                            sph=rx_r.get("sph"), cyl=rx_r.get("cyl"),
                            axis=rx_r.get("axis"), add_power=rx_r.get("add"),
                            eye_side="R"
                        )
                        if avail_r["status"] == "STOCK":
                            r_disp = format_quantity_display(avail_r["available_qty"], product)
                            st.success(f"👁️ RIGHT [{power_label}] → {r_disp} · ₹{avail_r['mrp']:.0f}")
                        elif avail_r["status"] == "RX":
                            st.info(f"👁️ RIGHT [{power_label}] → 📋 RX Order · ₹{avail_r['mrp']:.0f}")
                        else:
                            st.warning(f"👁️ RIGHT [{power_label}] → {avail_r['message']}")
                else:
                    # Contact lens → existing power-wise stock
                    r_qty = get_power_wise_stock(
                        pid, rx_r.get("sph"), rx_r.get("cyl"),
                        rx_r.get("axis"), rx_r.get("add")
                    )
                    if r_qty > 0:
                        r_disp = format_quantity_display(r_qty, product)
                        st.success(f"👁️ RIGHT [{power_label}] → {r_disp} ({r_qty} PCS)")
                    else:
                        _in_range_r = True
                        if _rc:
                            try:
                                _in_range_r = _rc(
                                    product_id   = str(pid or ''),
                                    product_name = str(product.get("product_name","") if product else ""),
                                    sph  = float(rx_r.get("sph") or 0),
                                    cyl  = float(rx_r.get("cyl") or 0),
                                    axis = int(rx_r.get("axis") or 0),
                                    is_colour = is_colour_product(str(product.get("product_name","") if product else "")),
                                    eye = "RIGHT",
                                )
                            except Exception:
                                _in_range_r = True
                        if _in_range_r:
                            st.warning(f"👁️ RIGHT [{power_label}] → Out of Stock")
                            if _pi_panel:
                                try:
                                    _pi_panel(
                                        sph=float(rx_r.get("sph") or 0),
                                        cyl=float(rx_r.get("cyl") or 0),
                                        axis=int(rx_r.get("axis") or 0),
                                        add_power=float(rx_r.get("add") or 0),
                                        selected_product=product.get("product_name","") if product else "",
                                        eye="RIGHT",
                                        product_id=str(pid or ""),
                                        is_colour=is_colour_product(str(product.get("product_name","") if product else "")),
                                    )
                                except Exception:
                                    pass


            # Left eye stock
            if st.session_state.get("retail_new_rx_l"):
                rx_l = st.session_state.retail_new_rx_l

                power_label = format_power_label(rx_l) or "No Power"

                if result.get('is_lens') and not result.get('is_contact'):
                    # Selector shown with RIGHT eye — LEFT uses same spec
                    pass
                else:
                    l_qty = get_power_wise_stock(
                        pid, rx_l.get("sph"), rx_l.get("cyl"),
                        rx_l.get("axis"), rx_l.get("add")
                    )
                    if l_qty > 0:
                        l_disp = format_quantity_display(l_qty, product)
                        st.success(f"👁️ LEFT [{power_label}] → {l_disp} ({l_qty} PCS)")
                    else:
                        _in_range_l = True
                        if _rc:
                            try:
                                _in_range_l = _rc(
                                    product_id   = str(pid or ''),
                                    product_name = str(product.get("product_name","") if product else ""),
                                    sph  = float(rx_l.get("sph") or 0),
                                    cyl  = float(rx_l.get("cyl") or 0),
                                    axis = int(rx_l.get("axis") or 0),
                                    is_colour = is_colour_product(str(product.get("product_name","") if product else "")),
                                    eye = "LEFT",
                                )
                            except Exception:
                                _in_range_l = True
                        if _in_range_l:
                            st.warning(f"👁️ LEFT [{power_label}] → Out of Stock")
                            if _pi_panel:
                                try:
                                    _pi_panel(
                                        sph=float(rx_l.get("sph") or 0),
                                        cyl=float(rx_l.get("cyl") or 0),
                                        axis=int(rx_l.get("axis") or 0),
                                        add_power=float(rx_l.get("add") or 0),
                                        selected_product=product.get("product_name","") if product else "",
                                        eye="LEFT",
                                        product_id=str(pid or ""),
                                        is_colour=is_colour_product(str(product.get("product_name","") if product else "")),
                                    )
                                except Exception:
                                    pass

            # Detect available eyes from saved power
            available_eyes = []

            if st.session_state.get("retail_new_rx_r"):
                available_eyes.append('R')

            if st.session_state.get("retail_new_rx_l"):
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

            # Ophthalmic lenses are ALWAYS billed per PCS (per lens)
            # Never per PAIR — each eye is one line, qty=1 per lens
            # Force PCS_ONLY so default=1 pcs per eye, not 0.5/1 pair
            _is_ophthalmic_qe = (
                result.get("is_lens") and not result.get("is_contact")
            )
            if _is_ophthalmic_qe:
                qe_product["unit"] = "PCS"
                qe_product["box_size"] = 1

            # If box_size > 1 → treat as BOX product (non-ophthalmic)
            elif int(qe_product.get("box_size", 0) or 0) > 1:

                qe_product["unit"] = "BOX"

                # Safe normalization for PostgreSQL 't'/'f' values
                val = str(qe_product.get("allow_loose", "")).lower()
                qe_product["allow_loose"] = val in ["true", "t", "1", "yes"]


            engine = QuantityEngine(qe_product)

            schema = engine.get_ui_schema()

            # ── Clear stale qty state if product changed ────────────────────
            # Prevents qty from previous product (e.g. 7pcs contact lens)
            # carrying over to next product (e.g. 1pc ophthalmic RX)
            _prev_pid = st.session_state.get("_qe_last_product_id")
            _curr_pid = str(qe_product.get("product_id", ""))
            if _prev_pid != _curr_pid:
                for _eye in ("R", "L", "B"):
                    st.session_state.pop(f"retail_qe_ps_{_eye}_pair", None)
                    st.session_state.pop(f"retail_qe_ps_{_eye}_pcs", None)
                    st.session_state.pop(f"retail_qe_ps_{_eye}_box", None)
                # CRITICAL: also reset final_qty so RX auto-add path gets 1, not
                # the previous product's qty (e.g. 7 pcs from contact lens box)
                st.session_state["retail_final_qty_R"] = 0
                st.session_state["retail_final_qty_L"] = 0
                st.session_state["_qe_last_product_id"] = _curr_pid
            # ─────────────────────────────────────────────────────────────────

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
                default_pcs  = st.session_state.get(f"retail_qe_ps_{eye}_pcs",  1)   # default 1 pc
                default_box  = st.session_state.get(f"retail_qe_ps_{eye}_box",  0)

                # Only set smart defaults if no previous values exist
                if default_pair == 0.0 and default_pcs <= 1 and default_box == 0:
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
                        # FLEX mode: PCS = extra loose pcs on top of boxes → min 0
                        # PCS_ONLY mode: min 1 (can't order 0)
                        _pcs_min = 0 if engine.mode == "FLEX" else 1
                        _pcs_default = max(_pcs_min, default_pcs)
                        user_input["pcs"] = st.number_input(
                            "Qty (PCS)",
                            min_value=_pcs_min,
                            max_value=1000,
                            value=_pcs_default,
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
                st.session_state.retail_final_qty_R = qty_r
            
            # LEFT EYE
            if has_l:
                st.markdown("---")
                qty_l = render_eye_qty("L")
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

                    # Queue eyes — reset finalization guard for this fresh product selection
                    st.session_state._retail_finalized_eyes = set()
                    if eye_mode == "BOTH":
                        st.session_state.retail_pending_eyes = ['R', 'L']
                    else:
                        st.session_state.retail_pending_eyes = [eye_mode]

                    st.session_state.retail_show_batch_editor = True

                    st.rerun()


        # -------------------------------------------------
        # OTHER PRODUCTS (Frame / Service / Accessory)
        # -------------------------------------------------
        else:

            st.markdown("---")
            product      = result['product_row']
            product_name = product['product_name']
            is_frame     = result.get('is_frame', False)
            is_scanned_lens = (
                result.get('selected_sku') and
                product.get('sph') and
                not is_frame
            )

            if is_scanned_lens:
                # Scanner resolved a contact/ophthalmic lens with power — fast path
                st.markdown("### ➕ Add Scanned Lens to Order")
                sku_code = product.get('batch_no', '')
                _sph = product.get('sph','')
                _cyl = product.get('cyl','')
                _ax  = product.get('axis','')
                _exp = product.get('expiry_date','')
                st.success(
                    f"🔬 {product_name} | SPH {_sph} CYL {_cyl} AX {_ax} | "
                    f"Batch {sku_code}" + (f" | Exp {_exp[:7]}" if _exp else "")
                )
                eye = st.radio("Eye", ["R", "L", "B"], horizontal=True, key="scan_eye")
                qty = st.number_input("Qty", min_value=1, value=1, key="scan_qty")

            elif is_frame:
                # Frame: SKU already selected in product selector — qty always 1
                st.markdown("### ➕ Add Frame to Order")
                sku_code = result.get('selected_sku', product.get('batch_no', ''))
                loc      = product.get('location', '')

                # Resolve price — mrp first, then selling_price
                _frame_mrp = float(product.get('mrp') or 0)
                if not _frame_mrp:
                    _frame_mrp = float(product.get('selling_price') or 0)

                # Use a short clean key (no UUID hyphens) to avoid Streamlit key issues
                _price_key = "frame_price_override"

                _fc1, _fc2 = st.columns([3, 2])
                with _fc1:
                    st.info(
                        f"**{product_name}** | SKU: `{sku_code}` | "
                        f"📍 {loc}"
                    )
                with _fc2:
                    _frame_price_input = st.number_input(
                        "Frame Price ₹ (MRP)",
                        min_value=0.0,
                        value=_frame_mrp if _frame_mrp > 0 else 0.0,
                        step=50.0,
                        format="%.2f",
                        key=_price_key,
                        help="Edit if DB price is wrong or not set"
                    )
                    if _frame_price_input == 0:
                        st.warning("⚠️ Price is ₹0 — enter the frame price")

                # Store confirmed price in session for button handler
                st.session_state["_frame_price_confirmed"] = float(_frame_price_input)

                qty = 1   # one frame per SKU
            else:
                st.markdown("### ➕ Add Item to Order")
                st.info(f"**Selected:** {product_name} (ID: {current_product_id})")
                qty = st.number_input(
                    "Quantity", min_value=1, step=1, value=1,
                    key=f"other_qty_{current_product_id}"
                )
                sku_code = product.get('batch_no', '')

            enter_to_submit()
            if st.button("➕ Add to Cart  [Enter]", type="primary", width='stretch'):

                provisional_id = create_provisional_order()

                # For frames: read from the clean session key set by the number_input
                if is_frame:
                    _raw_price = float(
                        st.session_state.get("_frame_price_confirmed") or
                        st.session_state.get("frame_price_override") or 0
                    )
                    if _raw_price == 0:
                        st.error("❌ Cannot add frame with ₹0 price — please enter the price above")
                        st.stop()
                    unit_price = _raw_price
                else:
                    unit_price = resolve_price_for_order_type(product, "RETAIL") or 0

                pcs_price   = normalize_to_pcs_price(unit_price, product)
                total_price = round(pcs_price * qty, 2)

                # ── Frame display name: product_name + SKU + frame_group + colour ──
                # Lookup colour_mix and frame_group from inventory_stock for this SKU.
                # Result: "Butler 8308 | D10004 | Trendy | TR"
                _frame_display_name = product_name
                if is_frame and sku_code:
                    _fgrp = ''
                    _fcol = ''
                    try:
                        from modules.sql_adapter import run_query as _rq_fsku
                        _frow = _rq_fsku(
                            """SELECT COALESCE(frame_group, '') AS frame_group,
                                      COALESCE(colour_mix, '')   AS colour_mix
                               FROM inventory_stock
                               WHERE product_id = %s::uuid
                                 AND batch_no   = %s
                               LIMIT 1""",
                            (str(current_product_id), sku_code)
                        )
                        if _frow:
                            _fgrp = str(_frow[0].get('frame_group') or '').strip()
                            _fcol = str(_frow[0].get('colour_mix')  or '').strip()
                    except Exception:
                        pass
                    # Build: "Butler 8308 | D10004 | Trendy | TR"
                    _name_parts = [product_name, sku_code]
                    if _fgrp: _name_parts.append(_fgrp)
                    if _fcol: _name_parts.append(_fcol)
                    _frame_display_name = ' | '.join(_name_parts)

                line = {
                    'line_id': str(uuid.uuid4()),
                    'provisional_order_id': provisional_id,

                    'product_id':   current_product_id,
                    'product_name': _frame_display_name,
                    'brand':        product.get('brand', ''),
                    'main_group':   product.get('main_group', ''),
                    'batch_no':     sku_code,   # SKU for frames, batch for others
                    # Frame SKU attributes — persisted into lens_params by order_persistence
                    'frame_group':  _fgrp if is_frame else '',
                    'colour_mix':   _fcol if is_frame else '',

                    # Power — from scanner for lenses, empty for frames/accessories
                    # normalize_eye_side maps 'OTHER'→'B' so backoffice grouping
                    # always sees a valid canonical value (R / L / B / SERVICE).
                    'eye_side': eye if is_scanned_lens else normalize_eye_side('B'),
                    'sph':      product.get('sph')      if is_scanned_lens else None,
                    'cyl':      product.get('cyl')      if is_scanned_lens else None,
                    'axis':     product.get('axis')     if is_scanned_lens else None,
                    'add_power':product.get('add_power')if is_scanned_lens else None,

                    # FIX: Pack frame attributes into lens_params so cart display
                    # and backoffice can read SKU/frame_group/colour_mix.
                    # manufacturing_route=STOCK tells backoffice this is a stock item —
                    # no supplier prompt, no manual allocation needed.
                    'lens_params': (
                        {
                            'batch_no':           sku_code,
                            'frame_group':        _fgrp,
                            'colour_mix':         _fcol,
                            'manufacturing_route': 'STOCK',   # ← backoffice reads this directly
                            'stock_source':        'STOCK',   # ← belt-and-suspenders signal
                        }
                        if is_frame and sku_code else {}
                    ),
                    'boxing_params': {},

                    'requested_qty': qty,
                    'billing_qty':   qty,
                    'order_qty':     0,
                    'display_qty':   f"{qty} PCS",

                    # ✅ FIX: use allocated_qty + selling_price keys (what compute_weighted_price reads)
                    'batch_allocation': (
                        [{'batch_no': sku_code, 'allocated_qty': 1,
                          'selling_price': pcs_price, 'qty': 1}]
                        if is_frame and sku_code else []
                    ),

                    'unit_price':  pcs_price,
                    'total_price': total_price,

                    'gst_percent': float(product.get('gst_percent') or 0),
                    'gst_amount':  0.0,
                    # Phase 2D: carry purchase_rate (cost) for margin guard in engine
                    'purchase_rate': float(product.get('purchase_rate') or 0),

                    # ✅ FIX: mark as already priced so run_pricing() pipeline skips this line
                    'pricing_applied_at': datetime.datetime.now().isoformat(),
                    'pricing_source':     'manual_frame',

                    # ← routing signals read by assignment_panel / order_edit_view
                    'manufacturing_route': 'STOCK' if (is_frame and sku_code) else None,
                    'stock_source':        'STOCK' if (is_frame and sku_code) else None,

                    'status': 'Complete',
                    'created_at': datetime.datetime.now().isoformat()
                }

                _stamp_line_tax(line, "RETAIL")
                _stamp_cart_line_discount(line)

                st.session_state.retail_order_lines.append(line)

                # ✅ AUTO MERGE CART (prevents duplicate lines instantly)
                try:
                    st.session_state.retail_order_lines = merge_order_lines(
                        st.session_state.retail_order_lines,
                        party_id="", order_type="RETAIL"
                    )
                except Exception:
                    pass  # Malformed legacy line — keep cart intact

                # ── For frames: back-fill mrp in DB if it was missing/zero ──
                if is_frame and pcs_price > 0:
                    _db_mrp = float(product.get('mrp') or 0)
                    if _db_mrp == 0:
                        try:
                            from modules.sql_adapter import run_write
                            run_write(
                                """UPDATE inventory_stock
                                   SET mrp = %s, updated_at = NOW()
                                   WHERE product_id = %s::uuid
                                     AND batch_no = %s
                                     AND (mrp IS NULL OR mrp = 0)""",
                                (pcs_price, current_product_id, sku_code)
                            )
                        except Exception:
                            pass   # non-critical — cart still has the price

                st.success("✅ Item added to cart")

                st.rerun()

def prepare_allocation(eye_side: str, qty: int = None):
    """Prepare allocation with batch details"""
    if not st.session_state.get("retail_selected_product"):
        st.error("❌ No product selected")
        return
    
    # Get quantity from session state - QuantityEngine final result
    # ALWAYS use final PCS from QuantityEngine
    if eye_side == "R":
        qty = int(st.session_state.get("retail_final_qty_R", 0) or 0)

    elif eye_side == "L":
        qty = int(st.session_state.get("retail_final_qty_L", 0) or 0)

    else:
        qty = 0

    
    product = st.session_state.retail_selected_product['product_row']
    product = _hydrate_product_gst(product)
    product_id = str(product['product_id'])
    
    if eye_side == 'R':
        rx = st.session_state.retail_new_rx_r
    elif eye_side == 'L':
        rx = st.session_state.retail_new_rx_l
    else:
        rx = {'sph': None, 'cyl': None, 'axis': None, 'add': None}
    
    # Universal box product detection (not category-based)
    is_box = is_box_product(product)
    
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

    # FIX: Frames have no eye side — passing eye_side='R'/'L' to get_batches_fifo
    # filters against NULL/OTHER stock rows and returns empty, showing "No stock available".
    # For frames, pass eye_side=None so the query matches on product_id + SKU only.
    _selected_for_alloc = st.session_state.get("retail_selected_product", {})
    _is_frame_alloc = _selected_for_alloc.get("is_frame", False)
    _batches_eye_side = None if _is_frame_alloc else eye_side

    batches_df = get_batches_fifo(product_id, sph_val, cyl_val, axis_val, add_val, _batches_eye_side)
    total_available = batches_df['available_qty'].sum() if not batches_df.empty else 0
    
    total_available = int(total_available or 0)
    qty = int(qty or 0)

    # ── Ophthalmic lens: use adapter for availability + price ──────────────
    selected_result = st.session_state.get("retail_selected_product", {})
    _is_ophthalmic = (
        selected_result.get("is_lens") and not selected_result.get("is_contact")
    )

    ophl_avail = None
    if _is_ophthalmic:
        ophl_avail = check_ophthalmic_availability(
            product_id, sph=sph_val, cyl=cyl_val,
            axis=axis_val, add_power=add_val, eye_side=eye_side
        )
        if ophl_avail["status"] == "STOCK":
            total_available = ophl_avail["available_qty"]
            if total_available < qty:
                st.warning(f"⚠️ Only {total_available} in stock. Remaining → RX order.")
        elif ophl_avail["status"] == "RX":
            st.info(f"📋 RX order lens — ₹{ophl_avail['mrp']:.0f} per piece")
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
	    pricing_mode="RETAIL" 
    )
    
    temp_allocation = {
        'line_id': str(uuid.uuid4()),
        'product_id': product_id,
        'product_name': product['product_name'],
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
        'lens_item_type': (
            ophl_avail['status'] if ophl_avail else 'STOCK'
        ),   # 'STOCK' | 'RX' | 'UNAVAILABLE'
        'ophl_stock_id': (
            ophl_avail.get('stock_id') if ophl_avail else None
        ),
        'unit_price': normalize_to_pcs_price(
            resolve_price_for_order_type(ophl_avail, "RETAIL") if ophl_avail
            else allocation.get('price', 0),
            product
        ),
        'gst_percent': float(product.get('gst_percent') or 0),
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
    if not st.session_state.get("retail_selected_product"):
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

        # ── Tinted ──
        tint_options = ["No", "Yes"]
        current_tint = lp.get('tinted') or "No"
        if current_tint not in tint_options:
            current_tint = "No"
        tinted = st.radio(
            "Tinted",
            options=tint_options,
            index=tint_options.index(current_tint),
            horizontal=True,
            key="lp_tinted"
        )

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

        # persist
        st.session_state.retail_lens_params = {
            'frame_type': frame_type,
            'thickness': thickness,
            'tinted': tinted,
            'corridor': corridor,
            'diameter': diameter,
            'fitting_height': fitting_height,
            'instructions': instructions,
        }

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

        if st.session_state.get("retail_pending_eyes"):
            # Only pop the next eye if we are NOT already processing one
            # (i.e. no current_allocation in progress)
            if not st.session_state.get("retail_current_allocation"):
                next_eye = st.session_state.retail_pending_eyes.pop(0)
                prepare_allocation(next_eye)
            # else: wait — current eye is still in batch editor

        else:
            # Fallback: detect eye from saved power (old behavior)
            if st.session_state.get("retail_new_rx_r") and not st.session_state.get("retail_current_allocation"):
                prepare_allocation('R')
            elif st.session_state.get("retail_new_rx_l") and not st.session_state.get("retail_current_allocation"):
                prepare_allocation('L')
    
    if not st.session_state.get("retail_show_batch_editor") or not st.session_state.get("retail_current_allocation"):
        return

    allocation = st.session_state.retail_current_allocation

    # Eye indicator
    eye_indicator = {
        'R': '👁️ RIGHT EYE',
        'L': '👁️ LEFT EYE',
        'B': '👁️👁️ BOTH EYES'
    }

    _eye_label = eye_indicator.get(allocation['eye_side'], allocation['eye_side'])
    _prod_label = f"{allocation['brand']} · {allocation['product_name']}"
    _batch_expander_title = f"📦 Batch Allocation — {_eye_label}  ·  {_prod_label}"

    with st.expander(_batch_expander_title, expanded=True):
        st.info(f"**{_eye_label}** | {_prod_label}  ·  FIFO auto-selected")

        if allocation.get('sph') is not None and allocation['eye_side'] != 'B':
            power_label = format_power_label({
                "sph": allocation.get("sph"),
                "cyl": allocation.get("cyl"),
                "axis": allocation.get("axis"),
                "add": allocation.get("add_power"),
            })
            st.write(f"**Power:** {power_label}")

        # Get product details for QuantityEngine
        product = st.session_state.retail_selected_product['product_row']

        # Universal box product detection (not category-based)
        is_box = is_box_product(product)

        box_size = int(product.get('box_size', 0) or 0)

        # Safe normalization for PostgreSQL 't'/'f' values (lowercase)
        val = str(product.get("allow_loose", "")).lower()
        allow_loose = val in ["true", "t", "1", "yes"]

        # Get available batches
        # FIX: Frames have no eye side — pass None so stock lookup isn't filtered
        # by 'R'/'L', which would exclude frame rows stored with eye_side=NULL/OTHER.
        _is_frame_be = st.session_state.get("retail_selected_product", {}).get("is_frame", False)
        _be_eye_side = None if _is_frame_be else allocation.get('eye_side')
        batches_df = get_batches_fifo(
            allocation['product_id'],
            allocation.get('sph'),
            allocation.get('cyl'),
            allocation.get('axis'),
            allocation.get('add_power'),
            _be_eye_side
        )

        batches = []
        if not batches_df.empty:
            for _, row in batches_df.iterrows():
                batches.append({
                    'batch_id': str(row.get('batch_id')),
                    'batch_no': row.get('batch_no'),
                    'expiry_date': row.get('expiry_date'),
                    'available_qty': row.get('available_qty'),
                    # ✅ RETAIL: mrp is the sticker/counter price; resolver handles fallback chain
                    'selling_price': resolve_price_for_order_type(dict(row), "RETAIL") or allocation.get('unit_price', 0),
                    'mrp':           _safe_price(row.get('mrp')) or allocation.get('unit_price', 0),
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

            # Ophthalmic lenses billed per PCS (per lens) — force PCS_ONLY
            _sel2 = st.session_state.get("retail_selected_product", {})
            if _sel2.get("is_lens") and not _sel2.get("is_contact"):
                qe_product["unit"] = "PCS"
                qe_product["box_size"] = 1

            elif int(qe_product.get("box_size", 0) or 0) > 1:
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

                if st.button("❌ Cancel", width='stretch', key="cancel_validation_error"):
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

                if st.button("❌ Cancel", width='stretch', key="cancel_zero_qty"):
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
                        # ✅ RETAIL: mrp first (resolver), then allocation fallback
                        'selling_price': resolve_price_for_order_type(batch, "RETAIL") or allocation.get('unit_price', 0),
                        # ✅ FIX: unit+box_size so compute_weighted_price normalises BOX→PCS
                        'unit':     str(product.get('unit', '') or ''),
                        'box_size': int(product.get('box_size', 0) or 0),
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
            if st.session_state.get("retail_pending_eyes"):
                next_eye = st.session_state.retail_pending_eyes[0]
                eye_name = "RIGHT" if next_eye == 'R' else "LEFT"
                st.info(f"ℹ️ After finalizing, you'll configure **{eye_name}** eye")

            # Action buttons
            col1, col2 = st.columns(2)

            with col1:
                if st.button("❌ Cancel", width='stretch', key="cancel_batch_allocation"):
                    clear_allocation_state()
                    st.session_state.retail_pending_eyes = []  # Clear all pending eyes on cancel
                    st.rerun()

            with col2:
                can_finalize = (total_allocated == punched_qty)

                if can_finalize:
                    # ── Duplicate finalization guard ─────────────────────────
                    _cur_eye  = allocation.get("eye_side", "")
                    _fin_eyes = st.session_state.get("_retail_finalized_eyes", set())
                    _already  = _cur_eye in _fin_eyes

                    if _already:
                        st.warning(
                            f"⚠️ **{_cur_eye} Eye already finalized and added to cart.**  \n"
                            "Adding again will create a duplicate line. "
                            "To change this eye, remove the existing cart line first.",
                            icon="⚠️",
                        )
                        col_a, col_b = st.columns(2)
                        with col_a:
                            if col_a.button("🔒 Keep existing — don't add again",
                                            width='stretch', key="fin_dup_block"):
                                clear_allocation_state()
                                st.session_state.retail_pending_eyes = []
                                st.rerun()
                        with col_b:
                            _force = col_b.button("⚠️ Add anyway (override)",
                                                  width='stretch',
                                                  key="fin_dup_force",
                                                  type="secondary")
                        if not _force:
                            st.stop()

                    enter_to_submit()
                    if st.button("✅ Finalize & Add to Cart  [Enter]", type="primary", width='stretch', key="finalize_add_to_cart_active"):

                        # Create provisional order if needed
                        provisional_id = create_provisional_order()

                        # -------------------------------
                        # GET RAW PRICE (SAFE)
                        # -------------------------------

                        # -------------------------------
                        # CALCULATE WEIGHTED PRICE
                        # -------------------------------

                        # ✅ FIX: compute_weighted_price now has unit+box_size per batch
                        # and normalises BOX→PCS internally — result is already per-PCS.
                        pcs_price = money(compute_weighted_price(batch_quantities))

                        if pcs_price == 0.0:
                            # Fallback: raw BOX price from allocation, normalise manually
                            pcs_price = round(normalize_to_pcs_price(
                                money(_safe_price(allocation.get('unit_price', 0))), product
                            ), 2)

                        pcs_price = round(pcs_price, 2)
                        total_price = round(pcs_price * punched_qty, 2)


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

                            'display_qty': format_quantity_display(punched_qty, product),

                            'batch_allocation': batch_quantities,

                            # ✅ CRITICAL: Save suggested allocation so backoffice can reuse
                            # it when user confirms quantity without changing it.
                            'suggested_allocation': list(batch_quantities),

                            'unit_price': pcs_price,
                            'total_price': total_price,

                            # ✅ Save box metadata so display & normalizer don't need product_row
                            'box_size': int(product.get('box_size', 0) or 0),
                            'unit': str(product.get('unit', '') or ''),

                            'gst_percent': float(allocation.get('gst_percent') or 0),
                            'gst_amount':  0.0,    # ← stamped by _stamp_line_tax below
                            # Phase 2D: carry purchase_rate (cost) for margin guard in engine
                            'purchase_rate': float(allocation.get('purchase_rate') or product.get('purchase_rate') or 0),

                            # ✅ FIX: stamp so run_pricing() idempotency guard skips this line
                            'pricing_applied_at': datetime.datetime.now().isoformat(),
                            'pricing_source':     'batch_weighted',

                            'status': 'Complete' if pending_qty == 0 else 'Partial',
                            'created_at': datetime.datetime.now().isoformat()
                        }

                        _stamp_line_tax(line, "RETAIL")    # ← stamp GST amount (RETAIL order)
                        _stamp_cart_line_discount(line)

                        st.session_state.retail_order_lines.append(line)

                        # Mark this eye as finalized — prevents accidental double-add
                        _fin_set = st.session_state.get("_retail_finalized_eyes", set())
                        _fin_set.add(allocation.get("eye_side", ""))
                        st.session_state._retail_finalized_eyes = _fin_set

                        # ✅ AUTO MERGE CART (prevents duplicate lines instantly)
                        try:
                            st.session_state.retail_order_lines = merge_order_lines(
                                st.session_state.retail_order_lines,
                                party_id="", order_type="RETAIL"
                            )
                        except Exception:
                            pass  # Malformed legacy line — keep cart intact

                        st.success("✅ Added to cart successfully!")

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
                        key="finalize_add_to_cart_disabled"
                    )

        else:
            # ── No physical stock — auto-route to cart, no user confirmation needed ──
            # Backoffice handles fulfilment routing (RX supplier / PO) after order save.
            _lens_item_type = allocation.get('lens_item_type', 'UNAVAILABLE')
            _rx_price       = float(allocation.get('unit_price') or 0)

            if _lens_item_type == 'RX' and _rx_price > 0:
                # ── AUTO: Ophthalmic RX lens — add directly, backoffice will order ──
                power_label = format_power_label({
                    "sph": allocation.get("sph"), "cyl": allocation.get("cyl"),
                    "axis": allocation.get("axis"), "add": allocation.get("add_power"),
                })
                st.info(
                    f"📋 **RX Lens** — ₹{_rx_price:.0f}/pc · {power_label} · "
                    f"Eye: {allocation.get('eye_side', '—')}  \n"
                    "Adding to cart automatically — backoffice will place supplier order."
                )
                # Use requested_qty from allocation, but cap to 1 if it's suspiciously
                # large (may be leftover from previous product's qty session state).
                # For ophthalmic RX: 1 lens per eye is always correct.
                rx_qty = int(allocation.get('requested_qty', 1) or 1)
                if rx_qty <= 0 or rx_qty > 2:
                    rx_qty = 1  # Safety: ophthalmic RX is always 1 PCS per eye
                provisional_id = create_provisional_order()
                total_price = round(_rx_price * rx_qty, 2)
                line = {
                    'line_id':              allocation['line_id'],
                    'provisional_order_id': provisional_id,
                    'product_id':           allocation['product_id'],
                    'product_name':         allocation['product_name'],
                    'brand':                allocation['brand'],
                    'main_group':           allocation['main_group'],
                    'eye_side':             allocation['eye_side'],
                    'sph':                  allocation.get('sph'),
                    'cyl':                  allocation.get('cyl'),
                    'axis':                 allocation.get('axis'),
                    'add_power':            allocation.get('add_power'),
                    'lens_params':          dict(st.session_state.retail_lens_params or {}),
                    'boxing_params':        dict(st.session_state.retail_boxing_params or {}),
                    'lens_item_type':       'RX',
                    'ophl_stock_id':        allocation.get('ophl_stock_id'),
                    'requested_qty':        rx_qty,
                    'billing_qty':          0,
                    'order_qty':            rx_qty,
                    'display_qty':          format_quantity_display(rx_qty, product),
                    'batch_allocation':     [],
                    'unit_price':           _rx_price,
                    'total_price':          total_price,
                    'gst_percent':          float(allocation.get('gst_percent') or 0),
                    'gst_amount':           0.0,    # ← stamped below
                    # Phase 2D: carry purchase_rate (cost) for margin guard in engine
                    'purchase_rate':        float(allocation.get('purchase_rate') or 0),
                    'status':               'RX Order',
                    'created_at':           datetime.datetime.now().isoformat(),
                }
                _stamp_line_tax(line, "RETAIL")
                _stamp_cart_line_discount(line)
                st.session_state.retail_order_lines.append(line)
                try:
                    st.session_state.retail_order_lines = merge_order_lines(
                        st.session_state.retail_order_lines,
                        party_id="", order_type="RETAIL"
                    )
                except Exception:
                    pass
                st.success(f"✅ RX lens auto-added to cart ({rx_qty} pc · ₹{total_price:,.2f})")

            else:
                # ── AUTO: No stock / unknown — add as Pending, backoffice raises PO ──
                req_qty = int(allocation.get('requested_qty', 0))
                st.info(
                    f"⚠️ No stock available — adding to cart as **Pending**.  \n"
                    "Backoffice will raise a purchase order."
                )
                provisional_id = create_provisional_order()
                line = {
                    'line_id':              allocation['line_id'],
                    'provisional_order_id': provisional_id,
                    'product_id':           allocation['product_id'],
                    'product_name':         allocation['product_name'],
                    'brand':                allocation['brand'],
                    'main_group':           allocation['main_group'],
                    'eye_side':             allocation['eye_side'],
                    'sph':                  allocation.get('sph'),
                    'cyl':                  allocation.get('cyl'),
                    'axis':                 allocation.get('axis'),
                    'add_power':            allocation.get('add_power'),
                    'lens_params':          dict(st.session_state.retail_lens_params or {}),
                    'boxing_params':        dict(st.session_state.retail_boxing_params or {}),
                    'requested_qty':        req_qty,
                    'billing_qty':          0,
                    'order_qty':            req_qty,
                    'display_qty':          format_quantity_display(req_qty, product),
                    'batch_allocation':     [],
                    'unit_price':           allocation.get('unit_price', 0),
                    'total_price':          0,
                    'gst_percent':          float(allocation.get('gst_percent') or 0),
                    'gst_amount':           0.0,
                    # Phase 2D: carry purchase_rate (cost) for margin guard in engine
                    'purchase_rate':        float(allocation.get('purchase_rate') or 0),
                    'status':               'Pending',
                    'created_at':           datetime.datetime.now().isoformat(),
                }
                _stamp_line_tax(line, "RETAIL")
                _stamp_cart_line_discount(line)
                st.session_state.retail_order_lines.append(line)
                st.success("✅ Added to cart as Pending — backoffice will handle ordering.")

            # ── Common: advance to next pending eye or close allocation ──────────
            has_more_eyes = bool(st.session_state.retail_pending_eyes)
            clear_allocation_state()
            if has_more_eyes:
                next_eye = st.session_state.retail_pending_eyes.pop(0)
                prepare_allocation(next_eye)
            else:
                st.session_state.retail_pending_eyes = []
            st.rerun()

    # ============================================================================
    # ORDER LINES DISPLAY - FROM OLD WORKING VERSION
    # ============================================================================

def render_order_lines():
    """Display finalized order lines with provisional order info"""
    
    # ── Consultation fee banner — shown when converted from consultation ──────
    # _consult_fee_lines is set in app.py when consultation is converted to billing.
    # It persists until order is saved (cleared in post-save reset).
    _cfe_stored = st.session_state.get("_consult_fee_lines", [])
    if _cfe_stored:
        # Check if already in cart
        _cart_svc = [l for l in (st.session_state.get("retail_order_lines") or [])
                     if str(l.get("eye_side","")).upper() in ("SERVICE","S")
                     or bool(l.get("is_service_line"))]

        if not _cart_svc:
            # Not in cart yet — show Add button
            _total_fee = sum(float(l.get("total_price",0)) for l in _cfe_stored)
            st.markdown(
                f"<div style='background:#0f1e0f;border:1px solid #22c55e;"
                f"border-radius:8px;padding:10px 16px;margin-bottom:8px'>"
                f"<span style='color:#86efac;font-size:0.88rem'>"
                f"🩺 Consultation fee <b>₹{_total_fee:,.0f}</b> not yet added to order</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if st.button(
                f"➕ Add Consultation Fee ₹{_total_fee:,.0f} to Cart",
                key="add_consult_fee_to_cart",
                type="primary",
                width='stretch',
            ):
                _lines = list(st.session_state.get("retail_order_lines") or [])
                # Update provisional_order_id on the stored lines
                _prov = st.session_state.get("retail_provisional_order_id","")
                for _fl in _cfe_stored:
                    _fl2 = dict(_fl)
                    _fl2["provisional_order_id"] = _prov
                    _lines.append(_fl2)
                st.session_state["retail_order_lines"] = _lines
                st.success(f"✅ Consultation fee ₹{_total_fee:,.0f} added to cart")
                st.rerun()
        else:
            # Already in cart
            _fee_in_cart = _cart_svc[0]
            st.markdown(
                f"<div style='background:#0f1e0f;border:1px solid #22c55e33;"
                f"border-radius:6px;padding:6px 14px;margin-bottom:6px;"
                f"color:#86efac;font-size:0.72rem'>"
                f"✅ Consultation fee ₹{float(_fee_in_cart.get('total_price',0)):,.0f} included</div>",
                unsafe_allow_html=True,
            )

    if st.session_state.get("retail_provisional_order_id"):
        col1, col2, col3, col4, col5 = st.columns([2, 2, 1, 1, 1])
        
        with col1:
            st.subheader("🛒 Current Order")
            st.caption(f"Provisional ID: **{st.session_state.retail_provisional_order_id}**")
        
        with col2:
            if st.session_state.get("retail_provisional_order_created_at"):
                created = datetime.datetime.fromisoformat(st.session_state.retail_provisional_order_created_at)
                st.caption(f"Started: {created.strftime('%I:%M %p')}")
        
        with col3:
            if st.button("📄 Duplicate", width='stretch', help="Duplicate this order"):
                duplicate_current_order()
                st.success("✅ Order duplicated!")
                st.rerun()
        
        with col4:
            if st.button("↩️ Undo", width='stretch', help="Undo last delete"):
                if undo_last():
                    st.rerun()

        with col5:
            if st.button("🗑️ Clear All", width='stretch', type="secondary"):
                if st.session_state.get("retail_order_lines"):
                    clear_provisional_order()
                    st.rerun()
    else:
        st.subheader("🛒 Order Cart")
    
    if not st.session_state.get("retail_order_lines"):
        st.info("No items in cart. Add products using the form above.")
        return
    
    r_lines = [l for l in st.session_state.retail_order_lines if l['eye_side'] == 'R']
    l_lines = [l for l in st.session_state.retail_order_lines if l['eye_side'] == 'L']
    service_lines = [l for l in st.session_state.retail_order_lines if l.get('eye_side') == 'SERVICE']
    other_lines   = [l for l in st.session_state.retail_order_lines if l.get('eye_side') not in ['R', 'L', 'SERVICE']]

    if service_lines:
        st.markdown("#### 🏥 SERVICES")
        for idx, line in enumerate(service_lines, 1):
            render_order_line_item(line, idx, 'SERVICE')

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

    # 🧬 Debug Replay Export (bottom of cart)
    with st.expander("🧬 Debug Tools", expanded=False):
        if st.button("📥 Export Debug Replay"):
            st.download_button(
                label="⬇️ Download Replay File",
                data=export_replay(),
                file_name="retail_replay.json",
                mime="application/json"
            )

def render_order_line_item(line: Dict, idx: int, eye_group: str):
    """Render single order line item - FROM OLD WORKING VERSION"""
    with st.expander(
        f"{idx}. {line['brand']} - {line['product_name']} | "
        f"Qty: {line.get('display_qty', line['billing_qty'])} | "
        f"Order: {line['order_qty']} | Status: {line['status']}",
        expanded=True
    ):
        col1, col2, col3 = st.columns([3, 2, 1])
        
        with col1:
            st.write(f"**Product:** {line['product_name']}")
            st.write(f"**Brand:** {line['brand']} | **Category:** {line['main_group']}")
            
            if line.get('sph') is not None and line['eye_side'] != 'B':
                power_label = format_power_label({
                    "sph": line.get("sph"),
                    "cyl": line.get("cyl"),
                    "axis": line.get("axis"),
                    "add": line.get("add_power"),
                })
                st.write(f"**Power:** {power_label}")
            
            st.write(f"**Eye:** {line['eye_side']}")

            # ── Lens Parameters summary ──
            lp = line.get('lens_params') or {}
            lp_filled = {k: v for k, v in lp.items() if v}
            # Fallback: frame lines saved before lens_params fix have top-level keys
            if not lp_filled and line.get('frame_group'):
                if line.get('batch_no'):   lp_filled['batch_no']    = line['batch_no']
                if line.get('frame_group'):lp_filled['frame_group'] = line['frame_group']
                if line.get('colour_mix'): lp_filled['colour_mix']  = line['colour_mix']
            if lp_filled:
                # FIX: friendly labels for frame attributes stored in lens_params
                _lp_label = {
                    'batch_no': 'SKU', 'frame_group': 'Frame Group', 'colour_mix': 'Colour',
                }
                lp_parts = [
                    f"{_lp_label.get(k, k.replace('_', ' ').title())}: {v}"
                    for k, v in lp_filled.items()
                ]
                _lp_prefix = "**Frame:**" if 'batch_no' in lp_filled else "**Lens:**"
                st.write(f"{_lp_prefix} {' | '.join(lp_parts)}")

            # ── Boxing Parameters summary ──
            bp = line.get('boxing_params') or {}
            bp_filled = {k: v for k, v in bp.items() if v}
            if bp_filled:
                label_map = {
                    'a_box': 'A Box', 'b_box': 'B Box', 'ed': 'ED',
                    'ed_axis': 'ED Axis', 'dbl': 'DBL', 'r_pd': 'R PD',
                    'l_pd': 'L PD', 'ipd': 'IPD', 'fitting_ht_r': 'Fit Ht R',
                    'fitting_ht_l': 'Fit Ht L', 'panto': 'Panto',
                    'tilt': 'Tilt', 'bvd': 'BVD',
                }
                bp_parts = [f"{label_map.get(k, k)}: {v}" for k, v in bp_filled.items()]
                st.write(f"**Boxing:** {' | '.join(bp_parts)}")
        
        with col2:
            st.metric("Requested", line['requested_qty'])
            st.metric("Billing", line['billing_qty'], help="Will go to invoice")
            st.metric("To Order", line['order_qty'], help="Will go to PO")
        
        with col3:
            # ── Pair view (1 pc = 0.5 pair) — view only, no logic change ──────
            _eye = line.get('eye_side', '')
            if _eye in ('R', 'L'):
                _req = int(line.get('requested_qty', 0) or 0)
                _up  = float(line.get('unit_price', 0) or 0)
                if _req:
                    st.caption(format_pair_display(_req))
                st.metric("Unit Price", f"₹{_up:.2f}", help="Per PCS price")
                if _up:
                    st.caption(f"₹{_up*2:,.2f}/pair")
            else:
                st.metric("Unit Price", f"₹{line['unit_price']:.2f}", help="Per PCS price")

            _disc_pct = float(line.get("discount_percent") or 0)
            _disc_amt = float(line.get("discount_amount") or 0)
            _net      = float(line.get("billing_total") or line.get("total_price") or 0)
            _gst_amt  = float(line.get("gst_amount") or 0)

            if _disc_pct > 0:
                st.metric(
                    f"🏷️ Disc ({_disc_pct:.2f}%)",
                    f"−₹{_disc_amt:.2f}",
                    help="Discount applied by engine"
                )
            st.metric("🧾 Net", f"₹{_net:.2f}", help="After discount")
            if _gst_amt > 0:
                st.metric("📊 GST", f"₹{_gst_amt:.2f}")
            st.metric("Total", f"₹{line['total_price']:.2f}")

            # ✅ FIX: Composite key (line_id + eye_side + idx) prevents R/L eye
            # delete buttons from sharing the same Streamlit widget key.
            # Filter also matches eye_side so only the exact line is removed.
            _del_key = f"delete_{line['line_id']}_{line.get('eye_side','X')}_{idx}"
            if st.button("🗑️ Delete", key=_del_key, width='stretch', help="Remove this line from cart"):
                push_undo()  # 🕐 Save state before delete (enables undo)
                # If deleting a SERVICE (consultation fee) line, flag it so
                # the pre-save merge does NOT re-inject it
                if str(line.get("eye_side","")).upper() in ("SERVICE","S") or bool(line.get("is_service_line")):
                    st.session_state["_consult_fee_removed"] = True
                st.session_state.retail_order_lines = [
                    l for l in st.session_state.retail_order_lines
                    if not (l['line_id'] == line['line_id'] and l.get('eye_side') == line.get('eye_side'))
                ]

                if not st.session_state.get("retail_order_lines"):
                    clear_provisional_order()

                st.rerun()

        # ── Phase 2C: Discount Breakdown + Scheme Info ───────────────────────
        # Engine stamps discount_breakdown and scheme_info — UI only reads.
        breakdown   = line.get("discount_breakdown") or []
        scheme_info = line.get("scheme_info") or {}

        if breakdown:
            st.markdown("**🏷️ Discounts Applied**")
            bd_cols = st.columns(min(len(breakdown), 3))
            for bi, bd in enumerate(breakdown):
                with bd_cols[bi % 3]:
                    st.markdown(
                        f"{bd.get('icon','🏷️')} **{bd.get('label','')}**  \n"
                        f"`−{bd.get('value', 0):.2f}%`"
                    )
            if len(breakdown) > 1:
                _disc_pct = float(line.get("discount_percent") or 0)
                _disc_amt = float(line.get("discount_amount") or 0)
                st.caption(f"Total discount: {_disc_pct:.2f}%  ·  −₹{_disc_amt:.2f}")

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
        
        if line.get('batch_allocation'):
            st.markdown("**📦 Batch Allocation (For Billing):**")
            
            batch_cols = st.columns(len(line['batch_allocation']))
            for idx, batch in enumerate(line['batch_allocation']):
                with batch_cols[idx]:
                    st.caption(f"**Batch:** {batch.get('batch_no','—')}")
                    st.caption(f"**Qty:** {batch.get('allocated_qty') or batch.get('qty','—')}")
                    if batch.get('expiry_date'):
                        st.caption(f"**Exp:** {batch['expiry_date']}")

# ============================================================================
# FINALIZE TO BACKOFFICE - FROM OLD WORKING VERSION
# ============================================================================

# ============================================================
# 🧠 EYE MERGE ENGINE (Invoice-aligned summary)
# ============================================================

def build_invoice_summary(lines: list) -> dict:
    """
    Groups cart lines by:
    - Eye (R/L)
    - Product
    - Power

    Returns invoice-ready structure
    """

    summary = {}

    for line in lines:
        eye = line.get("eye_side", "OTHER")

        power_key = (
            line.get("product_name"),
            line.get("sph"),
            line.get("cyl"),
            line.get("axis"),
            line.get("add_power"),
        )

        if eye not in summary:
            summary[eye] = {}

        if power_key not in summary[eye]:
            summary[eye][power_key] = {
                "product": line.get("product_name"),
                "brand": line.get("brand"),
                "sph": line.get("sph"),
                "cyl": line.get("cyl"),
                "axis": line.get("axis"),
                "add": line.get("add_power"),
                "pcs": 0,
                "product_row": line  # used for box_size
            }

        summary[eye][power_key]["pcs"] += int(line.get("requested_qty", 0))

    return summary


def format_invoice_qty(total_pcs: int, product_row: dict) -> str:
    """
    Converts PCS → BOX + PCS display
    """
    if not total_pcs:
        return "0"

    box_size = int(product_row.get("box_size") or 1)

    if box_size <= 1:
        return f"{total_pcs} PCS"

    boxes = total_pcs // box_size
    loose = total_pcs % box_size

    if boxes and loose:
        return f"{boxes} BOX + {loose} PCS"
    elif boxes:
        return f"{boxes} BOX"
    else:
        return f"{loose} PCS"


def format_pair_display(pcs: int) -> str:
    """
    Optical pair display: 1 pc = 0.5 pair.
    Returns a compact string like '1 pc (0.5 pair)' or '2 pcs (1 pair)'.
    View-only — no logic changes.
    """
    if not pcs:
        return "0 pc"
    pairs = pcs / PAIR_TO_PCS
    pair_str = f"{pairs:.1f}".rstrip('0').rstrip('.')  # '1.0'→'1', '0.5'→'0.5'
    pc_word = "pc" if pcs == 1 else "pcs"
    return f"{pcs} {pc_word} ({pair_str} pair)"


def format_pair_price(unit_price_per_pc: float) -> str:
    """
    Optical pair price display: if ₹400/pc → ₹800/pair.
    View-only — no logic changes.
    """
    if not unit_price_per_pc:
        return "₹0"
    pair_price = unit_price_per_pc * 2
    return f"₹{unit_price_per_pc:,.2f}/pc  (₹{pair_price:,.2f}/pair)"


def finalize_retail_order_to_backoffice():
    """Convert retail cart to backoffice order"""
    if not st.session_state.get("retail_order_lines"):
        return

    # ── Promo code input — invalidates discounts if changed ───────────────────
    _r_promo = st.text_input(
        "🎟️ Promo Code (optional)",
        value=st.session_state.get("_retail_promo_code", ""),
        placeholder="Enter discount code if applicable",
        key="retail_promo_code_input"
    )
    if _r_promo.strip().upper() != st.session_state.get("_retail_promo_code", ""):
        st.session_state["_retail_promo_code"] = _r_promo.strip().upper()
        for _rpl in st.session_state.retail_order_lines:
            _rpl["discount_percent"] = 0.0
            _rpl["discount_amount"]  = 0.0
        st.rerun()

    # ── Stamp discount on any lines not yet discounted ────────────────────────
    _r_needs_disc = any(
        float(_l.get("discount_percent") or 0) == 0
        and float(_l.get("unit_price") or 0) > 0
        for _l in st.session_state.retail_order_lines
    )
    if _r_needs_disc:
        try:
            from modules.pricing.discount_engine import apply_discounts
            _pc_fin_r = str(st.session_state.get("_retail_promo_code") or "").strip()
            for _rfl in st.session_state.retail_order_lines:
                if _pc_fin_r: _rfl["promo_code"] = _pc_fin_r
            apply_discounts(st.session_state.retail_order_lines,
                           party_id="", order_type="RETAIL")
            # Club offers — cart-level, after apply_discounts
            try:
                from modules.pricing.club_engine import apply_club_offers
                apply_club_offers(st.session_state.retail_order_lines, order_type="RETAIL")
            except Exception: pass
            for _rdl in st.session_state.retail_order_lines:
                _rdisc = float(_rdl.get("discount_amount") or 0)
                if _rdisc > 0:
                    _rgross = float(_rdl.get("total_price") or 0)
                    _rdl["billing_total"] = round(_rgross - _rdisc, 2)
                    _stamp_line_tax(_rdl, "RETAIL")
        except Exception:
            pass

    # ── Run pricing pipeline on every render so GST/discount/tax are always current ──
    try:
        from modules.core.pricing_pipeline import run_pricing
        order_info = {
            "provisional_order_id": st.session_state.get("retail_provisional_order_id", ""),
            "order_type": "RETAIL",
        }
        priced_lines, _trace = run_pricing(
            list(st.session_state.retail_order_lines),
            order_info,
        )
        st.session_state.retail_order_lines = priced_lines
    except Exception as _pe:
        pass  # pricing pipeline failure must never crash the UI


    st.markdown(
        "<div style='display:flex;align-items:center;gap:8px;margin:6px 0 4px'>"
        "<span style='background:#22c55e;color:#fff;font-size:0.68rem;font-weight:800;"
        "padding:2px 10px;border-radius:20px;letter-spacing:.05em'>✅ FINALIZE ORDER</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    
    total_billing = sum(line['total_price'] for line in st.session_state.retail_order_lines)
    total_items = len(st.session_state.retail_order_lines)

    # ── FIX: Freeze payment total the moment it's computed ────────────────
    # Once a payment section has been opened, we lock the total so that
    # cart changes mid-flow can't silently shift the amount being collected.
    _frozen_key = "_frozen_payment_total"
    if _frozen_key not in st.session_state:
        st.session_state[_frozen_key] = total_billing
    _frozen_total = st.session_state[_frozen_key]
    # If cart changed vs frozen total, warn and re-freeze
    if abs(total_billing - _frozen_total) > 0.01:
        st.warning(
            f"⚠️ Cart total changed (₹{_frozen_total:,.2f} → ₹{total_billing:,.2f}). "
            f"Payment section updated. Verify advance amount before confirming.",
            icon="⚠️"
        )
        st.session_state[_frozen_key] = total_billing
        _frozen_total = total_billing
    # ─────────────────────────────────────────────────────────────────────

    has_r = any(l['eye_side'] == 'R' for l in st.session_state.retail_order_lines)
    has_l = any(l['eye_side'] == 'L' for l in st.session_state.retail_order_lines)
    
    # ── Edit mode: show frozen patient banner ────────────────────────────
    _fin_edit_oid = st.session_state.get("_editing_order_id","")
    _fin_edit_ono = st.session_state.get("_editing_order_no","")
    if _fin_edit_oid:
        st.markdown(
            f"<div style='background:#0f1e2e;border:1px solid #3b82f6;border-radius:8px;"
            f"padding:10px 16px;margin-bottom:8px'>"
            f"<span style='color:#60a5fa;font-size:0.72rem;font-weight:700;letter-spacing:.06em'>"
            f"✏️ EDITING ORDER {_fin_edit_ono}</span><br>"
            f"<span style='color:#e2e8f0;font-size:0.88rem;font-weight:600'>"
            f"👤 {st.session_state.get('retail_patient_name','—')}</span>"
            f"<span style='color:#64748b;font-size:0.78rem;margin-left:10px'>"
            f"{st.session_state.get('retail_patient_mobile','')}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.write(f"**Patient:** {st.session_state.retail_patient_name}")
        st.write(f"**Mobile:** {st.session_state.retail_patient_mobile}")
    if st.session_state.get("retail_case_no"):
        st.write(f"**Case No:** {st.session_state.retail_case_no}")

    if has_r and not has_l:
        st.warning("⚠️ Only R eye")
    elif has_l and not has_r:
        st.warning("⚠️ Only L eye")

    # ============================================================
    # 🧾 INVOICE-ALIGNED SUMMARY
    # ============================================================

    def build_invoice_summary_lines():
        summary = []
        for line in st.session_state.retail_order_lines:
            product = line.get("product_row") or line
            qty_display = format_quantity_display(
                int(line.get("billing_qty", line.get("requested_qty", 0))),
                product
            )
            power = format_power_label({
                "sph": line.get("sph"),
                "cyl": line.get("cyl"),
                "axis": line.get("axis"),
                "add": line.get("add_power"),
            })
            summary.append({
                "eye": line["eye_side"],
                "product": line["product_name"],
                "brand": line.get("brand", ""),
                "power": power,
                "qty_display": qty_display,
                "amount": line["total_price"]
            })
        return summary

    # ══════════════════════════════════════════════════════════════════════════
    # [1] COLLAPSIBLE — Provisional Order Summary
    # ══════════════════════════════════════════════════════════════════════════

    # In edit mode: show notice that backoffice is the source of truth
    _fin_is_edit = bool(st.session_state.get("_editing_order_id",""))
    if _fin_is_edit:
        _edit_ono_disp = st.session_state.get("_editing_order_no","")
        st.info(
            f"✏️ **Editing order {_edit_ono_disp}** — "
            "Save here to update. "
            "View final confirmed lines in Backoffice after saving."
        )

    st.markdown("### 🧾 Provisional Order Summary")
    # Use gst_amount from lines (already stamped on net after discount)
    _gst_total_pre = sum(float(l.get("gst_amount") or 0) for l in st.session_state.retail_order_lines)
    _base_pre = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in st.session_state.retail_order_lines) - _gst_total_pre

    with st.expander(
        f"📋 {total_items} line{'s' if total_items!=1 else ''}  ·  Base ₹{_base_pre:,.2f}  ·  GST ₹{_gst_total_pre:,.2f}  ·  Total ₹{total_billing:,.2f}  —  click to expand/collapse",
        expanded=True
    ):
        _ic = st.columns(3)
        _ic[0].write(f"**Date:** {datetime.datetime.now().strftime('%d %b %Y')}")
        _ic[1].write(f"**ID:** `{st.session_state.retail_provisional_order_id}`")
        _ic[2].write(f"**Patient:** {st.session_state.retail_patient_name}")
        st.markdown("---")
        _hcols = st.columns([1,4,2,2,2,1,2,2,1])
        for _hc,_ht in zip(_hcols,["**Eye**","**Product / Power**","**Qty**","**Pair View**","**Unit Price**","**GST%**","**GST Amt**","**Total (MRP)**",""]):
            _hc.markdown(_ht)
        st.markdown("---")
        for _grp_lbl,_grp_eye in [("👁️ RIGHT EYE","R"),("👁️ LEFT EYE","L"),("🔹 OTHER","OTHER"),("🏥 SERVICES","SERVICE")]:
            _gl = [l for l in st.session_state.retail_order_lines if l.get("eye_side")==_grp_eye]
            if not _gl: continue
            st.markdown(f"**{_grp_lbl}**")
            for _l in _gl:
                _pnm = f"**{_l.get('brand','')}** {_l.get('product_name','')}"
                _pwr = ""
                if _l.get("sph") is not None and _l.get("eye_side") not in ("OTHER","B"):
                    _pwr = format_power_label({"sph":_l.get("sph"),"cyl":_l.get("cyl"),"axis":_l.get("axis"),"add":_l.get("add_power")})
                _qty_d = _l.get("display_qty") or format_quantity_display(int(_l.get("billing_qty",_l.get("requested_qty",0))),_l)
                _up=float(_l.get("unit_price",0)); _tot=float(_l.get("billing_total") or _l.get("total_price",0))
                _pcs_price = _up  # keep PCS price for pair view calculation
                # When qty is shown as BOX, display box price in unit column
                # but keep _pcs_price (per-PCS) for the pair view column.
                _bsz = int(_l.get("box_size", 0) or 0)
                _req = int(_l.get("requested_qty", 0) or 0)
                if _bsz > 1 and _req > 0 and (_req % _bsz == 0):
                    _boxes = _req // _bsz
                    if _boxes > 0:
                        _up = round(_tot / _boxes, 2)  # box price for unit col display
                _gp=_gst_percent_for_display(_l); _ga=float(_l.get("gst_amount") or 0)
                _sicon="✅" if _l.get("status")=="Complete" else ("⏳" if _l.get("status")=="Partial" else "🔄")
                # Pair view: always use PCS price (₹/pc and ₹/pair = pcs_price × 2)
                _pair_d = format_pair_display(_req) if _l.get("eye_side") in ("R","L") and _req else "—"
                _pair_price_d = f"₹{_pcs_price:,.2f}/pc\n₹{_pcs_price*2:,.2f}/pair" if _l.get("eye_side") in ("R","L") and _pcs_price else f"₹{_up:,.2f}"
                _rc=st.columns([1,4,2,2,2,1,2,2,1])
                _rc[0].write({"R":"👁️ R","L":"👁️ L"}.get(_l.get("eye_side",""),"🔹"))
                with _rc[1]:
                    st.write(_pnm)
                    if _pwr: st.caption(_pwr)
                    st.caption(f"{_sicon} {_l.get('status','')}")
                _rc[2].write(_qty_d)
                _rc[3].caption(_pair_d)
                _rc[4].write(_pair_price_d)
                _rc[5].write(f"{_gp:.0f}%"); _rc[6].write(f"₹{_ga:,.2f}"); _rc[7].write(f"₹{_tot:,.2f}")
                _del_sum_key = f"sum_del_{_l['line_id']}_{_l.get('eye_side','X')}_{_l.get('sph','')}"
                if _rc[8].button("🗑️", key=_del_sum_key, help="Remove this line"):
                    push_undo()
                    if str(_l.get("eye_side","")).upper() in ("SERVICE","S") or bool(_l.get("is_service_line")):
                        st.session_state["_consult_fee_removed"] = True
                    st.session_state.retail_order_lines = [
                        _x for _x in st.session_state.retail_order_lines
                        if not (_x['line_id'] == _l['line_id'] and _x.get('eye_side') == _l.get('eye_side'))
                    ]
                    st.rerun()
        st.markdown("---")
        _fc=st.columns([1,4,2,2,2,1,2,2,1])
        _fc[1].markdown("**TOTAL**"); _fc[6].markdown(f"**₹{_gst_total_pre:,.2f}**"); _fc[7].markdown(f"**₹{total_billing:,.2f}**")
        st.caption(f"Base ₹{_base_pre:,.2f}  ·  GST ₹{_gst_total_pre:,.2f}  ·  Grand Total ₹{total_billing:,.2f}")

    _mc=st.columns(3)
    _mc[0].metric("Total Items",total_items)
    _mc[1].metric("Base Value (excl. GST)",f"₹{_base_pre:,.2f}")
    _mc[2].metric("Grand Total (MRP)",f"₹{total_billing:,.2f}")
    st.caption(f"Provisional ID: {st.session_state.retail_provisional_order_id}")

    # ── Service charges note ──────────────────────────────────────────────
    # Fitting / colouring / courier charges are added AFTER punch in backoffice.
    # This note ensures staff know the above total is product price only.
    st.info(
        "ℹ️ **Lens + Frame price only** — fitting, colouring and courier charges (if any) "
        "will be added in Backoffice and reflected in the final challan/invoice."
    )

    # ══════════════════════════════════════════════════════════════════════════
    # [2] COLLAPSIBLE — Clinical Findings
    # Reads clinical_va_*, clinical_sle_*, clinical_ortho_* from session state.
    # ══════════════════════════════════════════════════════════════════════════
    with st.expander("🩺 Clinical Findings (Visual Acuity + Examination)", expanded=False):
        _rx_r = st.session_state.get("retail_new_rx_r") or st.session_state.get("retail_old_rx_r") or {}
        _rx_l = st.session_state.get("retail_new_rx_l") or st.session_state.get("retail_old_rx_l") or {}

        st.markdown(f"**Patient:** {st.session_state.retail_patient_name}  |  "
                    f"**Date:** {datetime.datetime.now().strftime('%d %b %Y')}")
        st.markdown("---")

        # ── Prescription ──────────────────────────────────────────────────────
        st.markdown("#### 📋 Prescription (New Rx)")
        _prx_c = st.columns(5)
        for _h,_t in zip(_prx_c,["**Eye**","**SPH**","**CYL**","**AXIS**","**ADD**"]): _h.markdown(_t)
        for _lbl,_rxd in [("👁️ R (Right)",_rx_r),("👁️ L (Left)",_rx_l)]:
            _pc=st.columns(5); _pc[0].write(_lbl)
            for _i,_k in enumerate(["sph","cyl","axis","add"]): _pc[_i+1].write(str(_rxd.get(_k,"—") or "—"))

        # ── Clinical sections from session state ──────────────────────────────
        _clin = _build_clinical_sections()

        def _render_clin_section(icon_title: str, data: dict):
            if not data:
                return
            st.markdown("---")
            st.markdown(f"#### {icon_title}")
            # Two-column grid — left column for R/left items, right for L/right
            _items = list(data.items())
            _cols = st.columns(2)
            for i, (label, val) in enumerate(_items):
                _cols[i % 2].write(f"**{label}:** {val}")

        _render_clin_section("👁️ Visual Acuity",       _clin["va"])
        _render_clin_section("🔬 Slit Lamp Examination", _clin["sle"])
        _render_clin_section("🔭 Orthoptic Examination", _clin["ortho"])

        # Anything clinical that didn't fit the three main sections
        if _clin["other"]:
            _render_clin_section("📋 Other Findings", _clin["other"])

        if not any(_clin.values()):
            st.info("No clinical findings recorded for this visit. "
                    "Complete the Clinical Examination section above first.")

    # ══════════════════════════════════════════════════════════════════════════
    # [3] COLLAPSIBLE — Patient Barcode Label
    # ══════════════════════════════════════════════════════════════════════════
    with st.expander("🏷️ Patient Barcode Label", expanded=False):
        try:
            from modules.core.barcode_label import render_patient_label
            _rx_r2 = st.session_state.get("retail_new_rx_r") or st.session_state.get("retail_old_rx_r") or {}
            _rx_l2 = st.session_state.get("retail_new_rx_l") or st.session_state.get("retail_old_rx_l") or {}
            render_patient_label(
                {"id":str(st.session_state.get("retail_patient_id","")),
                 "name":st.session_state.retail_patient_name,
                 "mobile":st.session_state.retail_patient_mobile},
                _rx_r2, _rx_l2
            )
        except Exception as _le:
            st.caption(f"Barcode label unavailable: {_le}")

    # ══════════════════════════════════════════════════════════════════════
    # 📅 DELIVERY DATE & TIME
    # ══════════════════════════════════════════════════════════════════════
    _today      = datetime.date.today()
    _default_dd = _today + datetime.timedelta(days=2)

    st.markdown(
        "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:10px;"
        "padding:12px 18px;margin:10px 0;border-left:4px solid #10b981'>"
        "<div style='color:#10b981;font-size:0.62rem;letter-spacing:.08em;"
        "text-transform:uppercase;margin-bottom:3px'>📅 Expected Delivery</div>"
        "<div style='color:#e2e8f0;font-size:0.82rem'>"
        "Auto-set to +2 days · 6 PM — edit if needed</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    _dd_c1, _dd_c2 = st.columns([2, 1])
    with _dd_c1:
        _delivery_date = st.date_input(
            "Delivery Date",
            value=st.session_state.get("retail_delivery_date", _default_dd),
            min_value=_today,
            key="retail_delivery_date",
            help="Suggested: order date + 2 days",
        )
    with _dd_c2:
        _TIME_OPTIONS = [
            "8:00 AM","9:00 AM","10:00 AM","11:00 AM","12:00 PM",
            "1:00 PM","2:00 PM","3:00 PM","4:00 PM","5:00 PM",
            "6:00 PM","7:00 PM","8:00 PM",
        ]
        _default_time = st.session_state.get("retail_delivery_time", "6:00 PM")
        if _default_time not in _TIME_OPTIONS:
            _default_time = "6:00 PM"
        _delivery_time = st.selectbox(
            "Delivery Time",
            _TIME_OPTIONS,
            index=_TIME_OPTIONS.index(_default_time),
            key="retail_delivery_time",
            help="Expected collection time",
        )

    # Values are automatically persisted via widget keys retail_delivery_date / retail_delivery_time
    _delivery_date = st.session_state.get("retail_delivery_date", _default_dd)
    _delivery_time = st.session_state.get("retail_delivery_time", "6:00 PM")

    # ══════════════════════════════════════════════════════════════════════
    # 💰 ADVANCE PAYMENT — collected at order punching time
    # ══════════════════════════════════════════════════════════════════════
    _ADV_METHODS = ["CASH", "UPI", "NEFT", "RTGS", "CHEQUE", "CARD"]
    st.markdown("""
    <div style='background:#0f172a;border:1px solid #1e293b;border-radius:10px;
                padding:14px 18px;margin:10px 0;border-left:4px solid #8b5cf6'>
      <div style='color:#94a3b8;font-size:0.62rem;letter-spacing:.08em;
                  text-transform:uppercase;margin-bottom:3px'>
        🛍️ Retail — Advance + Balance
      </div>
      <div style='color:#e2e8f0;font-size:0.85rem'>
        Collect advance now · Balance will be collected on delivery via challan
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Show existing advance if in edit mode
    _existing_adv = float(st.session_state.get("_edit_existing_advance") or 0)
    _is_edit_mode = bool(st.session_state.get("_editing_order_id",""))

    # ── CONFIRMED freeze check in retail edit mode ────────────────────────
    # If the order being edited is CONFIRMED or beyond, show a lock banner.
    # Only admin/manager role can proceed (enforced by OrderGuard upstream).
    # This is a VISUAL reminder — the actual gate is in order_edit_view + OrderGuard.
    if _is_edit_mode:
        _edit_oid_for_check = st.session_state.get("_editing_order_id","")
        _edit_status_check  = st.session_state.get("_editing_order_status","")
        if not _edit_status_check and _edit_oid_for_check:
            try:
                from modules.sql_adapter import run_query as _rq_chk
                _chk = _rq_chk("SELECT status FROM orders WHERE id=%s::uuid LIMIT 1",
                                (_edit_oid_for_check,)) or []
                if _chk:
                    _edit_status_check = str(_chk[0].get("status","")).upper()
                    st.session_state["_editing_order_status"] = _edit_status_check
            except Exception:
                pass

        _CONFIRMED_LOCK_STATUSES = {
            "CONFIRMED","IN_PRODUCTION","READY","READY_FOR_BILLING",
            "BILLED","DISPATCHED","DELIVERED","CLOSED"
        }
        if _edit_status_check in _CONFIRMED_LOCK_STATUSES:
            st.markdown(
                f"<div style='background:#1a0a0a;border:2px solid #ef4444;"
                f"border-radius:8px;padding:12px 16px;margin-bottom:12px'>"
                f"<div style='color:#ef4444;font-weight:700;font-size:0.95rem'>"
                f"⚠️ CONFIRMED ORDER — RESTRICTED EDIT MODE</div>"
                f"<div style='color:#94a3b8;font-size:0.78rem;margin-top:4px'>"
                f"This order is <b>{_edit_status_check}</b>. "
                f"Backoffice is the authority — all changes here will require "
                f"re-confirmation. Only Admins and Managers can modify.</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    if _is_edit_mode and _existing_adv > 0:
        st.markdown(
            f"<div style='background:#14532d;border:1px solid #22c55e;border-radius:6px;"
            f"padding:8px 14px;margin-bottom:6px;display:flex;justify-content:space-between'>"
            f"<span style='color:#86efac;font-size:0.82rem;font-weight:700'>"
            f"✅ Previously Paid (on file)</span>"
            f"<span style='color:#4ade80;font-size:1rem;font-weight:900'>"
            f"₹{_existing_adv:,.2f}</span></div>",
            unsafe_allow_html=True
        )

    _adv_c0, _adv_c1 = st.columns([1, 3])
    with _adv_c0:
        _adv_collect_label = "💰 Collect Additional Payment" if (_is_edit_mode and _existing_adv > 0) else "💰 Collect Advance"
        # Use value= only, not key= — avoids conflict when session state sets it from prefill
        _adv_collect_default = bool(st.session_state.get("retail_collect_advance", False))
        _adv_collect = st.checkbox(_adv_collect_label, value=_adv_collect_default)
        st.session_state["retail_collect_advance"] = _adv_collect
    with _adv_c1:
        _remaining = max(total_billing - _existing_adv, 0)
        if _is_edit_mode and _existing_adv > 0:
            st.metric("Order Total (MRP)", f"₹{total_billing:,.2f}",
                      delta=f"-₹{_existing_adv:,.2f} already paid",
                      delta_color="normal")
        else:
            st.metric("Order Total (MRP)", f"₹{total_billing:,.2f}")

    if _adv_collect:
        _ac1, _ac2, _ac3 = st.columns([1.5, 1.2, 1.5])
        with _ac1:
            _max_adv = max(float(total_billing) - _existing_adv, 0.0) if total_billing else 999999.0
            # Clamp current value to max before passing to widget
            _raw_adv_val = float(st.session_state.get("retail_advance_amount") or 0.0)
            # In edit mode the session value might be the existing advance (already paid)
            # — cap it so new-advance input starts at 0 if existing covers it
            if _is_edit_mode and _existing_adv > 0:
                _raw_adv_val = min(_raw_adv_val, _max_adv)
            _safe_adv_val = min(_raw_adv_val, _max_adv if _max_adv > 0 else 999999.0)
            _label = "Additional Advance ₹" if (_is_edit_mode and _existing_adv > 0) else "Advance Amount ₹"
            _adv_amt = st.number_input(
                _label,
                min_value=0.0,
                max_value=max(_max_adv, 0.01),  # never pass 0 as max
                value=_safe_adv_val,
                step=1.0,
                help="Maximum = Order Total minus already paid advance."
            )
            st.session_state["retail_advance_amount"] = _adv_amt
        with _ac2:
            _adv_mode = st.selectbox(
                "Payment Mode", _ADV_METHODS,
                index=_ADV_METHODS.index(st.session_state.get("retail_advance_mode","CASH"))
                      if st.session_state.get("retail_advance_mode") in _ADV_METHODS else 0,
                key="retail_advance_mode"
            )
        with _ac3:
            _adv_ref = ""
            if _adv_mode in ("UPI","NEFT","RTGS","CHEQUE"):
                _adv_ref = st.text_input("TXN / Cheque Ref",
                                          value=st.session_state.get("retail_advance_ref",""),
                                          key="retail_advance_ref")
        _total_paid = _existing_adv + (_adv_amt or 0)
        _adv_balance = max(round(total_billing - _total_paid, 2), 0)
        st.markdown(
            f"<div style='background:#0a0f1a;border-radius:6px;padding:8px 14px;margin-top:4px;"
            f"display:flex;gap:28px'>"
            + (f"<span style='color:#4ade80;font-size:0.8rem'>"
               f"<b>Prev paid:</b> ₹{_existing_adv:,.2f}</span>" if _existing_adv > 0 else "")
            + f"<span style='color:#8b5cf6;font-size:0.8rem'>"
            f"<b>New advance:</b> ₹{(_adv_amt or 0):,.2f}</span>"
            f"<span style='color:#f59e0b;font-size:0.8rem'>"
            f"<b>Balance on Delivery:</b> ₹{_adv_balance:,.2f}</span>"
            f"</div>",
            unsafe_allow_html=True
        )
    else:
        if not _is_edit_mode:
            for _k in ("retail_advance_amount","retail_advance_mode","retail_advance_ref"):
                st.session_state.pop(_k, None)
        _adv_amt = 0.0
        if _existing_adv > 0:
            _balance = max(round(total_billing - _existing_adv, 2), 0)
            st.markdown(
                f"<div style='color:#94a3b8;font-size:0.8rem;padding:4px 0'>"
                f"Balance on delivery: ₹{_balance:,.2f} "
                f"(after ₹{_existing_adv:,.2f} already paid)</div>",
                unsafe_allow_html=True
            )
        else:
            st.caption("☝️ Tick the box above to record advance payment now.")

    # ── Edit mode: recover patient_id if session was partially wiped ─────
    _edit_mode_id_check = st.session_state.get("_editing_order_id","")
    if _edit_mode_id_check and not st.session_state.get("retail_patient_id"):
        _pname_check = st.session_state.get("retail_patient_name","")
        if _pname_check:
            try:
                from modules.sql_adapter import run_query as _rq_pid
                # Try to get party_id directly from the order being edited
                _pid_rows = _rq_pid(
                    "SELECT party_id::text AS pid FROM orders WHERE id=%s::uuid LIMIT 1",
                    (_edit_mode_id_check,)
                ) or []
                if _pid_rows and _pid_rows[0].get("pid"):
                    st.session_state["retail_patient_id"] = str(_pid_rows[0]["pid"])
                else:
                    # Fallback: lookup by name in patients table
                    _pid_rows2 = _rq_pid(
                        "SELECT id::text AS pid FROM patients WHERE master_name ILIKE %s LIMIT 1",
                        (_pname_check,)
                    ) or []
                    if _pid_rows2:
                        st.session_state["retail_patient_id"] = str(_pid_rows2[0]["pid"])
                    else:
                        # Last resort: use a placeholder so save doesn't block
                        st.session_state["retail_patient_id"] = _edit_mode_id_check
            except Exception:
                pass

    # Validation before submit
    if not st.session_state.get("retail_patient_id"):
        if st.session_state.get("_editing_order_id"):
            # Edit mode — show frozen patient info instead of error
            st.warning("⚠️ Patient details not loaded — save may still proceed for edit.")
        else:
            st.error("❌ Patient not selected")
            return
    
    if not st.session_state.get("retail_order_lines"):
        st.error("❌ No items in cart")
        return

    # ── Consultation conversion guard ─────────────────────────────────────
    # If this order is being created from a consultation conversion, the cart
    # must have at least ONE non-SERVICE product line. A SERVICE-only cart means
    # staff clicked Save without adding any products — which would create a
    # "converted" retail order containing nothing but the consultation fee,
    # marking the consultation as CONVERTED with no real order behind it.
    _is_consult_conversion = bool(st.session_state.get("_retail_consult_source_id", ""))
    _editing_existing      = bool(st.session_state.get("_editing_order_id", ""))
    if _is_consult_conversion and not _editing_existing:
        _product_lines = [
            l for l in st.session_state.retail_order_lines
            if str(l.get("eye_side", "")).upper() not in ("SERVICE", "S")
            and not l.get("is_service_line")
        ]
        if not _product_lines:
            st.warning(
                "⚠️ **Add at least one product before saving.**\n\n"
                "This order is being converted from a consultation. "
                "The consultation fee is already in the cart — "
                "please add the spectacle / lens lines before confirming.",
                icon="🛍️",
            )
            return

    # ── Advance > order total check ───────────────────────────────────────
    _adv_check = float(st.session_state.get("retail_advance_amount") or 0)
    _tot_check  = sum(float(l.get("total_price",0)) for l in st.session_state.retail_order_lines)
    if st.session_state.get("retail_collect_advance") and _adv_check > _tot_check:
        st.error(
            f"❌ Advance ₹{_adv_check:,.2f} exceeds Order Total ₹{_tot_check:,.2f}. "
            f"Please reduce the advance amount."
        )
        return

    from modules.utils.submit_guard import is_locked, guarded_submit

    # ── Cart fingerprint — blocks re-submit of same cart ──────────────────
    # Hash the provisional order ID (unique per cart session). If this ID was
    # already confirmed, the button stays permanently disabled for this session.
    _prov_id    = st.session_state.get("retail_provisional_order_id", "")
    _cart_fp    = f"_confirmed_cart_{_prov_id}" if _prov_id else None
    _already_confirmed = bool(_cart_fp and st.session_state.get(_cart_fp))
    _btn_disabled = is_locked("retail_confirm") or _already_confirmed

    if _already_confirmed:
        # Only show this block if we have actual receipt data for THIS order
        _snap_for_btn = (
            st.session_state.get("_receipt_snapshot") or
            st.session_state.get(st.session_state.get("_last_receipt_key","")) or
            None
        )
        if _snap_for_btn:
            _ac1, _ac2 = st.columns([3, 2])
            with _ac1:
                st.success(f"✅ Order {_snap_for_btn.get('order_no','')} confirmed — print below")
            with _ac2:
                if st.button("🖨️ Print Confirmation Receipt",
                             key="print_receipt_from_confirm_area",
                             width='stretch',
                             type="primary"):
                    try:
                        from modules.settings.shop_master import get_unit_info
                        _si = get_unit_info("retail")
                    except Exception:
                        _si = {}
                    try:
                        _html2 = _build_confirmation_receipt_html(_snap_for_btn, _si)
                        _open_html_print(_html2)
                        st.success("✅ Print dialog opened")
                    except Exception as _pe2:
                        st.error(f"Print failed: {_pe2}")
        else:
            # Stale fingerprint — clear it so button re-enables for new order
            if _cart_fp:
                st.session_state.pop(_cart_fp, None)

    # ── Edit mode: show change summary before confirm ────────────────────
    _edit_oid_pre = st.session_state.get("_editing_order_id","")
    _edit_ono_pre = st.session_state.get("_editing_order_no","")
    if _edit_oid_pre:
        try:
            from modules.sql_adapter import run_query as _rq_pre
            _orig = _rq_pre("""
                SELECT p.product_name, ol.eye_side, ol.sph, ol.cyl, ol.quantity, ol.unit_price,
                       ol.total_price
                FROM order_lines ol LEFT JOIN products p ON p.id=ol.product_id
                WHERE ol.order_id=%s::uuid AND COALESCE(ol.is_deleted,false)=false
            """, (_edit_oid_pre,)) or []
            _cur  = st.session_state.get("retail_order_lines",[])
            _orig_n = len(_orig); _cur_n = len(_cur)
            _orig_val = sum(float(r.get("total_price",0)) for r in _orig)
            _cur_val  = sum(float(l.get("total_price",0)) for l in _cur)
            _diff = _cur_val - _orig_val
            _diff_str = f" ({"+" if _diff>=0 else ""}₹{_diff:,.2f})" if abs(_diff)>0.01 else ""
            st.info(
                f"✏️ **Editing order {_edit_ono_pre}** — order will be updated in place. "
                f"Original: {_orig_n} lines ₹{_orig_val:,.2f}  →  "
                f"New: {_cur_n} lines ₹{_cur_val:,.2f}{_diff_str}. "
                f"Changes logged with your user ID.",
                icon="✏️"
            )
        except Exception:
            st.info(f"✏️ Editing order {_edit_ono_pre} — will be updated in place.")
        _confirm_label = f"💾 Save Changes to {_edit_ono_pre}"
    else:
        _confirm_label = "💾 Confirm & Send to Backoffice"

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
    enter_to_submit()
    kb_legend()

    # ── CONFIRM ACTION HELPER — shared by both buttons ─────────────────────
    def _run_confirm_pipeline(do_print: bool):
        """All save/pipeline logic. Called from within a button's if-block."""
        with guarded_submit("retail_confirm") as _allowed:
            if not _allowed:
                st.stop()

            # ✅ FINAL GLOBAL DEDUPE SAFETY (DO NOT REMOVE)
            try:
                st.session_state.retail_order_lines = merge_order_lines(
                    st.session_state.retail_order_lines,
                    party_id="", order_type="RETAIL"
                )
            except Exception:
                pass  # Malformed legacy line — keep cart intact

            from modules.core.order_pipeline import OrderPipeline
            pipeline = OrderPipeline()

            # ── Normalize cart lines before pipeline ─────────────────────────
            # Guarantees gst_percent, unit_price, billing_total are typed
            # correctly on every line regardless of how the cart was built.
            try:
                from modules.core.order_normalizer import normalize_lines
                st.session_state.retail_order_lines, _norm_warns = normalize_lines(
                    st.session_state.retail_order_lines, order_type="RETAIL"
                )
                if _norm_warns:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        f"[RETAIL] Normalizer warnings: {_norm_warns}"
                    )
            except Exception as _ne:
                pass  # Normalizer is defensive — never block the order
            # ─────────────────────────────────────────────────────────────────

            _edit_mode_oid = st.session_state.get("_editing_order_id","")
            _edit_mode_ono = st.session_state.get("_editing_order_no","")

            # ── Fallback: recover edit reference from cart lines ──────────────
            # order_edit_view.py embeds _edit_order_id / _edit_order_no into every
            # cart line at load time. If session state was wiped between render and
            # confirm click, we recover from the lines so an edit never creates a
            # new order accidentally.
            if not _edit_mode_oid:
                for _cl in st.session_state.get("retail_order_lines", []):
                    _candidate = str(_cl.get("_edit_order_id") or "").strip()
                    if _candidate and len(_candidate) > 10:
                        _edit_mode_oid = _candidate
                        _edit_mode_ono = str(_cl.get("_edit_order_no") or "")
                        # Restore to session state for consistency
                        st.session_state["_editing_order_id"] = _edit_mode_oid
                        st.session_state["_editing_order_no"] = _edit_mode_ono
                        break

            # ── SAFETY: ensure patient name is set before we hit any validator ─────
            # If name is somehow empty but ID exists, look it up from DB.
            # This is a last-resort guard — the UI pre-checks should never let
            # an empty name reach here, but belt-and-suspenders for edge cases.
            if not st.session_state.get("retail_patient_name"):
                _guard_pid = st.session_state.get("retail_patient_id", "")
                if _guard_pid and len(str(_guard_pid)) > 10:
                    try:
                        from modules.sql_adapter import run_query as _rq_guard
                        _guard_rows = _rq_guard(
                            "SELECT master_name FROM patients WHERE id=%s::uuid LIMIT 1",
                            (str(_guard_pid),)
                        ) or []
                        if _guard_rows and _guard_rows[0].get("master_name"):
                            st.session_state["retail_patient_name"] = _guard_rows[0]["master_name"]
                    except Exception:
                        pass
                # If still empty after lookup, show clear error and stop
                if not st.session_state.get("retail_patient_name"):
                    st.error("❌ Patient name is missing — please re-select the patient and try again.")
                    st.stop()

            # ── EDIT MODE: update existing order in-place ─────────────────────
            if _edit_mode_oid and _edit_mode_ono:
                result = _update_order_in_place(
                    order_id   = _edit_mode_oid,
                    order_no   = _edit_mode_ono,
                    cart_lines = st.session_state.retail_order_lines,
                    patient_name   = st.session_state.retail_patient_name,
                    patient_mobile = st.session_state.retail_patient_mobile,
                    advance_amount = float(st.session_state.get("retail_advance_amount") or 0),
                    advance_mode   = st.session_state.get("retail_advance_mode","CASH"),
                    advance_ref    = st.session_state.get("retail_advance_ref",""),
                    collect_advance= bool(st.session_state.get("retail_collect_advance")),
                    delivery_date  = st.session_state.get("retail_delivery_date",""),
                    delivery_time  = st.session_state.get("retail_delivery_time","6:00 PM"),
                    user_name      = st.session_state.get("username","Staff"),
                )
                if "error" in result:
                    # 🔴 Lock error gets a specific, clear message — not a generic failure
                    if result.get("locked"):
                        st.error(
                            f"🔒 **Order Locked — Cannot Edit**\n\n"
                            f"{result['error']}\n\n"
                            f"This order has been confirmed and is now read-only. "
                            f"Any changes must be done via Backoffice by a supervisor."
                        )
                        # Clear the edit mode so UI doesn't stay stuck in edit
                        st.session_state.pop("_editing_order_id", None)
                        st.session_state.pop("_editing_order_no", None)
                        st.session_state.pop("retail_order_lines", None)
                        try:
                            from modules.utils.submit_guard import clear_lock
                            clear_lock("retail_confirm")
                        except Exception:
                            pass
                    else:
                        st.error(f"❌ Update failed: {result['error']}")
                    st.stop()
                # Clear edit mode
                st.session_state.pop("_editing_order_id", None)
                st.session_state.pop("_editing_order_no", None)
                if _cart_fp:
                    st.session_state[_cart_fp] = _edit_mode_ono
                # Build receipt snapshot for print
                _edit_existing_adv  = float(st.session_state.get("_edit_existing_advance") or 0)
                _edit_new_adv       = float(st.session_state.get("retail_advance_amount") or 0) if st.session_state.get("retail_collect_advance") else 0.0
                _edit_total_advance = round(_edit_existing_adv + _edit_new_adv, 2)
                _receipt_data = _build_edit_receipt_snapshot(
                    order_no       = _edit_mode_ono,
                    patient_name   = st.session_state.retail_patient_name,
                    patient_mobile = st.session_state.retail_patient_mobile,
                    lines          = st.session_state.retail_order_lines,
                    advance_amount = _edit_total_advance,
                    advance_mode   = st.session_state.get("retail_advance_mode","CASH"),
                    delivery_date  = st.session_state.get("retail_delivery_date",""),
                    delivery_time  = st.session_state.get("retail_delivery_time","6:00 PM"),
                )
                st.session_state["_receipt_snapshot"] = _receipt_data
                _ono_key2 = f"_receipt_data_{_edit_mode_ono}"
                st.session_state[_ono_key2] = _receipt_data
                st.session_state["_last_receipt_key"] = _ono_key2
                if do_print:
                    st.session_state["_auto_print_receipt"] = True
                st.success(f"✅ Order {_edit_mode_ono} updated — changes logged")
                st.balloons()

                _edit_existing_adv2  = float(st.session_state.get("_edit_existing_advance") or 0)
                _edit_new_adv2       = float(st.session_state.get("retail_advance_amount") or 0) if st.session_state.get("retail_collect_advance") else 0.0
                _edit_total_adv2     = round(_edit_existing_adv2 + _edit_new_adv2, 2)
                _edit_grand2         = sum(float(l.get("total_price",0)) for l in st.session_state.get("retail_order_lines",[]))
                st.session_state["_post_save_data"] = {
                    "order_no":   _edit_mode_ono,
                    "party_name": st.session_state.get("retail_patient_name",""),
                    "mobile":     st.session_state.get("retail_patient_mobile",""),
                    "total":      _edit_grand2,
                    "advance":    _edit_total_adv2,
                    "order_type": "RETAIL",
                    "delivery":   st.session_state.get("retail_delivery_date",""),
                    "lines":      list(st.session_state.get("retail_order_lines", [])),
                }

                # Reset after edit — keep receipt snapshot
                st.session_state.last_order_snapshot = list(
                    st.session_state.get("retail_order_lines",[])
                )
                industrial_reset("ALL")
                # Do NOT wipe patient_name — receipt guard needs it to show print
                for _pk in [
                    "retail_patient_id",
                    "retail_case_no","retail_old_rx_r","retail_old_rx_l",
                    "retail_new_rx_r","retail_new_rx_l","retail_order_lines",
                    "_persistent_cart","_crash_snapshot",
                ]:
                    st.session_state.pop(_pk, None)
                st.session_state["retail_patient_id"]  = None
                st.session_state["retail_order_lines"] = []
                # Clear edit-mode keys so post-save panel renders (not edit mode re-entry)
                st.session_state.pop("_editing_order_id", None)
                st.session_state.pop("_editing_order_no", None)
                st.session_state.pop("_edit_existing_advance", None)
                # Clear consultation service lines — order is saved, fresh start
                st.session_state.pop("_consult_svc_lines", None)
                st.session_state.pop("_consult_fee_lines", None)
                st.session_state.pop("_retail_consult_source_id", None)
                st.session_state.pop("_consult_fee_lines", None)
                st.session_state.pop("_consult_fee_removed", None)
                st.session_state.pop("_consult_fee_consumed", None)
                # Keep retail_patient_name so render_confirmation_receipt shows
                safe_rerun()

            else:
                # ── NEW ORDER: normal pipeline ───────────────────────────────
                _prov_oid  = st.session_state.get("retail_provisional_order_id", "")
                # Guarantee a provisional ID exists — if session was cleared, create one now
                if not _prov_oid:
                    _prov_oid = create_provisional_order()
                _pt_name   = st.session_state.get("retail_patient_name", "")
                _pt_mobile = st.session_state.get("retail_patient_mobile", "")
                _pt_id     = st.session_state.get("retail_patient_id")
                order_info = {
                    "order_type":           "RETAIL",
                    "order_source":         "retail_punching",
                    # ── party fields — used by ALL validators ──
                    "party":                _pt_name,          # PartyValidator + NO_PATIENT
                    "party_name":           _pt_name,          # OrderValidator MISSING_FIELDS
                    "party_id":             _pt_id,            # DB FK + advance payment link
                    "patient_name":         _pt_name,
                    "patient_mobile":       _pt_mobile,
                    # ── order identity ──
                    "provisional_order_id": _prov_oid,         # _build_order_data → order_id
                    "customer_order_no":    st.session_state.get("retail_case_no", ""),
                    # ── optics ──
                    "lens_params":          dict(st.session_state.get("retail_lens_params") or {}),
                    "boxing_params":        dict(st.session_state.get("retail_boxing_params") or {}),
                }

                # ── Merge consultation fee SERVICE lines into cart before submit ──
                # _consult_fee_lines is stored separately (survives ALL cart resets).
                # Only inject if:
                #   (a) user has NOT explicitly deleted them (_consult_fee_removed not set)
                #   (b) the exact line_id is not already in cart (prevents double-inject)
                _fee_lines_to_merge = st.session_state.get("_consult_fee_lines", [])
                if (_fee_lines_to_merge
                        and not st.session_state.get("_consult_fee_removed")
                        and not st.session_state.get("_consult_fee_consumed")):
                    _cart_line_ids = {
                        str(l.get("line_id", ""))
                        for l in st.session_state.retail_order_lines
                    }
                    _cart_svc_pids = {
                        str(l.get("product_id", ""))
                        for l in st.session_state.retail_order_lines
                        if str(l.get("eye_side", "")).upper() in ("SERVICE", "S")
                        or bool(l.get("is_service_line"))
                    }
                    # ── FIX: name-based dedup fallback (audit point #3) ───────
                    # If two paths created fee lines with different product UUIDs
                    # (one from _set_consult_billing_state, one from convert_consultation_to_billing),
                    # the product_id check above would miss it. Add a name-based guard.
                    _cart_svc_names = {
                        str(l.get("product_name", "")).strip().lower()
                        for l in st.session_state.retail_order_lines
                        if str(l.get("eye_side", "")).upper() in ("SERVICE", "S")
                        or bool(l.get("is_service_line"))
                    }
                    for _fl in _fee_lines_to_merge:
                        _fl_lid  = str(_fl.get("line_id", ""))
                        _fl_pid  = str(_fl.get("product_id", ""))
                        _fl_name = str(_fl.get("product_name", "")).strip().lower()
                        # Skip if already in cart by line_id, product_id, OR product name
                        if (_fl_lid in _cart_line_ids
                                or _fl_pid in _cart_svc_pids
                                or (_fl_name and _fl_name in _cart_svc_names)):
                            continue
                        # Stamp current provisional_order_id so pipeline links correctly
                        _fl2 = dict(_fl)
                        _fl2["provisional_order_id"] = _prov_oid
                        st.session_state.retail_order_lines.append(_fl2)
                        _cart_line_ids.add(_fl_lid)
                        _cart_svc_pids.add(_fl_pid)
                        _cart_svc_names.add(_fl_name)
                    # ─────────────────────────────────────────────────────────
                st.session_state["_consult_fee_consumed"] = True  # prevent re-inject on same order

                # ── Deduplicate cart before submit ──────────────────────────────
                # Prevents duplicate order_lines when cart was built from multiple sources
                # (consultation fee merge + existing cart lines + any race conditions).
                # Keep first occurrence of each (product_id, eye_side) combination.
                _seen_cart = set()
                _deduped_cart = []
                for _cl in st.session_state.retail_order_lines:
                    _cl_key = (
                        str(_cl.get("product_id","") or ""),
                        str(_cl.get("eye_side","") or "").upper(),
                    )
                    if _cl_key not in _seen_cart:
                        _seen_cart.add(_cl_key)
                        _deduped_cart.append(_cl)
                st.session_state.retail_order_lines = _deduped_cart

                with st.spinner("Processing order..."):
                    try:
                        result = pipeline.submit_retail(
                            cart_lines=st.session_state.retail_order_lines,
                            order_info=order_info,
                            user_name="Retail Desk"
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
                        st.stop()

                if result["status"] != "CONFIRMED":
                    st.stop()

                _r_ono  = str(result.get("order_no",""))
                _r_party= st.session_state.get("retail_patient_name","")
                _r_mob  = st.session_state.get("retail_patient_mobile","")
                _r_adv  = float(st.session_state.get("retail_advance_amount") or 0) if st.session_state.get("retail_collect_advance") else 0.0
                _r_del  = str(st.session_state.get("retail_delivery_date",""))
                _r_tot  = sum(float(l.get("total_price",0)) for l in st.session_state.get("retail_order_lines",[]))
                st.success(f"✅ Order Confirmed: {_r_ono}")
                st.balloons()
                # Store post-save context — rendered after rerun from _receipt_snapshot area
                st.session_state["_post_save_data"] = {
                    "order_no":    _r_ono,
                    "party_name":  _r_party,
                    "mobile":      _r_mob,
                    "total":       _r_tot,
                    "advance":     _r_adv,
                    "order_type":  "RETAIL",
                    "delivery":    _r_del,
                    "lines":       list(st.session_state.get("retail_order_lines", [])),
                }

                # ── PERMANENT DEDUPE STAMP ────────────────────────────────────
                # Mark this provisional cart as confirmed. Button stays disabled
                # even if lock expires, even across rerenders.
                if _cart_fp:
                    st.session_state[_cart_fp] = str(result.get("order_no", "confirmed"))

                # ── Record advance if collected ───────────────────────────────
                _adv_amount = float(st.session_state.get("retail_advance_amount") or 0)
                if st.session_state.get("retail_collect_advance") and _adv_amount > 0:
                    try:
                        from modules.billing.payment_manager import _submit_payment
                        from modules.sql_adapter import run_query as _rq_adv

                        # result["order_id"] is a dict from save_order:
                        # {"order_db_id": "<uuid>", "display_order_no": N}
                        _res_oid = result.get("order_id") or {}
                        if isinstance(_res_oid, dict):
                            _real_order_uuid = str(_res_oid.get("order_db_id") or "")
                        else:
                            _real_order_uuid = str(_res_oid)
                        # Fallback: look up by order_no if UUID not in result
                        if not _real_order_uuid or len(_real_order_uuid) < 10:
                            _ono_rows = _rq_adv(
                                "SELECT id FROM orders WHERE order_no=%(n)s LIMIT 1",
                                {"n": str(result.get("order_no",""))}
                            )
                            _real_order_uuid = str(_ono_rows[0]["id"]) if _ono_rows else ""

                        if not _real_order_uuid or len(_real_order_uuid) < 10:
                            raise ValueError(f"Could not resolve order UUID from result: {result}")

                        _pt_id  = str(st.session_state.get("retail_patient_id") or "")
                        _pt_rows = _rq_adv(
                            "SELECT id FROM parties WHERE id::text=%(id)s LIMIT 1",
                            {"id": _pt_id}
                        ) if _pt_id else []

                        _submit_payment(
                            order_id     = _real_order_uuid,
                            party_id     = str(_pt_rows[0]["id"]) if _pt_rows else None,
                            party_name   = st.session_state.get("retail_patient_name",""),
                            amount       = _adv_amount,
                            method       = st.session_state.get("retail_advance_mode","CASH"),
                            pay_date     = __import__("datetime").date.today(),
                            ref_no       = st.session_state.get("retail_advance_ref",""),
                            remarks      = "Advance at order punching",
                            payment_type = "ADVANCE",
                            challan_id   = None,
                            invoice_id   = None,
                        )
                        st.success(
                            f"💰 Advance ₹{_adv_amount:,.2f} recorded via "
                            f"{st.session_state.get('retail_advance_mode','CASH')}"
                        )
                    except Exception as _adv_err:
                        st.warning(f"⚠️ Order saved but advance not recorded: {_adv_err}")
                # ─────────────────────────────────────────────────────────────

                # ----------------------------
                # SAVE LAST ORDER SNAPSHOT
                # ----------------------------
                st.session_state.last_order_snapshot = copy.deepcopy(
                    st.session_state.retail_order_lines
                )

                # ── Persist confirmed order ref for post-confirm adder ─────────
                _res_oid_r = result.get("order_id") or {}
                _real_uuid_r = (
                    str(_res_oid_r.get("order_db_id") or "")
                    if isinstance(_res_oid_r, dict)
                    else str(_res_oid_r)
                )
                if not _real_uuid_r or len(_real_uuid_r) < 10:
                    try:
                        from modules.sql_adapter import run_query as _rq_lr
                        _lr = _rq_lr("SELECT id FROM orders WHERE order_no=%(n)s LIMIT 1",
                                     {"n": str(result.get("order_no",""))})
                        _real_uuid_r = str(_lr[0]["id"]) if _lr else ""
                    except Exception:
                        _real_uuid_r = ""

                st.session_state["last_confirmed_order"] = {
                    "id":             _real_uuid_r,
                    "order_no":       str(result.get("order_no", "")),
                    "status":         "PENDING",
                    "payment_status": "PENDING",   # FIX: payment tracking
                    "total":          float(sum(float(l.get("total_price",0)) for l in st.session_state.get("retail_order_lines",[]))),
                }
                # Clear frozen total so next order starts fresh
                st.session_state.pop("_frozen_payment_total", None)

                # Flush pending service charges to DB now that we have the real order UUID
                _pend_charges = st.session_state.pop("_retail_pending_charges", [])
                if _pend_charges and _real_uuid_r:
                    try:
                        from modules.backoffice.order_charges_panel import save_charge as _sc
                        _svc_user = st.session_state.get("user_name", "Retail")
                        # FIX: dedup by charge type — only one of each type per order (audit point #5)
                        _seen_charge_types = set()
                        for _ch in _pend_charges:
                            _ctype = str(_ch.get("type", "")).strip().upper()
                            if _ctype in _seen_charge_types:
                                import logging as _sc_log
                                _sc_log.getLogger(__name__).warning(
                                    f"[Retail] Skipping duplicate service charge type: {_ctype}"
                                )
                                continue
                            _seen_charge_types.add(_ctype)
                            _sc(
                                _real_uuid_r,
                                _ch["type"],
                                _ch["desc"],
                                float(_ch["amt"]),
                                float(_ch["gst"]),
                                created_by=_svc_user,
                            )
                    except Exception as _pce:
                        import logging
                        logging.warning(f"[Retail] Pending charges flush failed: {_pce}")

                # Mark consultation as CONVERTED now that retail order is saved
                _consult_src_for_conv = st.session_state.get("_retail_consult_source_id", "")
                _new_order_no_for_conv = str(result.get("order_no","")) or str(_real_uuid_r)
                if _consult_src_for_conv and len(_consult_src_for_conv) > 10:
                    try:
                        from modules.sql_adapter import run_write as _rw_conv, run_query as _rq_conv
                        # ── Resolve to UUID if still an order_no ─────────────
                        _is_conv_uuid = (
                            len(str(_consult_src_for_conv)) == 36
                            and str(_consult_src_for_conv).count("-") == 4
                            and not str(_consult_src_for_conv).upper().startswith(("CONS-","R/","W/"))
                        )
                        if not _is_conv_uuid:
                            _conv_id_rows = _rq_conv(
                                "SELECT id::text FROM orders WHERE order_no = %s LIMIT 1",
                                (_consult_src_for_conv,)
                            ) or []
                            if _conv_id_rows:
                                _consult_src_for_conv = _conv_id_rows[0]["id"]
                            else:
                                _consult_src_for_conv = ""  # can't resolve — skip
                        try:
                            _rw_conv("ALTER TABLE orders ADD COLUMN IF NOT EXISTS linked_retail_no TEXT")
                        except Exception:
                            pass
                        if _consult_src_for_conv:
                            # Mark consultation as CONVERTED using UUID (reliable)
                            _rw_conv("""
                                UPDATE orders
                                   SET is_converted     = true,
                                       linked_retail_no = %(rno)s,
                                       updated_at       = NOW()
                                 WHERE id = %(cid)s::uuid
                                   AND order_type = 'CONSULTATION'
                                   AND COALESCE(is_converted, false) = false
                            """, {"cid": _consult_src_for_conv, "rno": _new_order_no_for_conv})
                    except Exception as _conv_err:
                        import logging
                        logging.warning(f"[Retail] Mark consultation CONVERTED failed: {_conv_err}")

                # ── Save full receipt data BEFORE reset so print survives wipe ──
                _adv_snap = float(st.session_state.get("retail_advance_amount") or 0)
                _receipt_data = {
                    "order_no":       str(result.get("order_no", "")),
                    "order_id":       _real_uuid_r,
                    "patient_name":   st.session_state.get("retail_patient_name", ""),
                    "patient_mobile": st.session_state.get("retail_patient_mobile", ""),
                    "case_no":        st.session_state.get("retail_case_no", ""),
                    "rx_r":           dict(st.session_state.get("retail_new_rx_r") or {}),
                    "rx_l":           dict(st.session_state.get("retail_new_rx_l") or {}),
                    "lines":          copy.deepcopy(st.session_state.get("retail_order_lines", [])),
                    "advance_amount": _adv_snap,
                    "advance_mode":   st.session_state.get("retail_advance_mode", "CASH"),
                    "advance_ref":    st.session_state.get("retail_advance_ref", ""),
                    "confirmed_at":   datetime.datetime.now().strftime("%d %b %Y  %I:%M %p"),
                    "delivery_date":  st.session_state.get("retail_delivery_date", ""),
                    "delivery_time":  st.session_state.get("retail_delivery_time", "6:00 PM"),
                }
                # _receipt_snapshot: used by render_confirmation_receipt() at page top
                st.session_state["_receipt_snapshot"] = _receipt_data
                # Persistent copy keyed to order_no — survives industrial_reset
                # Only the Dismiss button clears _receipt_snapshot; this persists for the session
                _ono_key = f"_receipt_data_{str(result.get('order_no',''))}"
                st.session_state[_ono_key] = _receipt_data
                st.session_state["_last_receipt_key"] = _ono_key
                st.session_state["_show_receipt_top"] = True
                # ── If pressed via "Confirm, Print & Add to Backoffice" auto-open print ──
                if do_print:
                    st.session_state["_auto_print_receipt"] = True
                # ─────────────────────────────────────────────────────────────

                # ----------------------------
                # FULL ERP RESET (SAFE)
                # ----------------------------
                industrial_reset("ALL")
                initialize_clinical_state()
                # NOTE: do NOT call reset_after_submit here — it fires st.rerun()
                # a second time, which races with safe_rerun() and can clear
                # _receipt_snapshot before the next render reads it.

                # ── HARD PATIENT WIPE after order save ───────────────────────────
                # Guarantees no stale patient lingers even if crash/cart restore runs
                for _pk in [
                    "retail_patient_id", "retail_patient_name", "retail_patient_mobile",
                    "retail_case_no", "retail_case_search_results",
                    "retail_selected_case_record_no", "retail_case_visits",
                    "retail_selected_visit_id", "retail_all_patients",
                    "case_id_search_input", "patient_search_mode",
                    "patient_name_dropdown", "patient_mobile_dropdown",
                    "new_patient_name", "new_patient_mobile",
                    "retail_old_rx_r", "retail_old_rx_l",
                    "retail_new_rx_r", "retail_new_rx_l",
                    "_persistent_cart", "_crash_snapshot",
                ]:
                    st.session_state.pop(_pk, None)
                # Re-init patient defaults
                st.session_state["retail_patient_id"]     = None
                st.session_state["retail_patient_name"]   = ""
                st.session_state["retail_patient_mobile"] = ""
                st.session_state["retail_case_no"]        = ""
                st.session_state["retail_old_rx_r"]       = {}
                st.session_state["retail_old_rx_l"]       = {}
                st.session_state["retail_new_rx_r"]       = {}
                st.session_state["retail_new_rx_l"]       = {}
                st.session_state["retail_order_lines"]    = []
                # Clear consultation service lines — order saved, fresh start
                st.session_state.pop("_consult_svc_lines", None)
                st.session_state.pop("_retail_consult_source_id", None)
                st.session_state.pop("_consult_fee_lines", None)
                # ─────────────────────────────────────────────────────────────

                # Clear submit lock BEFORE rerun so print button works on next render
                try:
                    from modules.utils.submit_guard import clear_lock
                    clear_lock("retail_confirm")
                except Exception:
                    pass
                safe_rerun()

    # ── TWO BUTTONS — both call _run_confirm_pipeline, flag controls print ──
    # PRIMARY: Confirm + Print + Backoffice (the recommended single-click flow)
    _cp_label = (
        "✅ Confirm, Print & Add to Backoffice"
        if not st.session_state.get("_editing_order_id")
        else "✅ Save, Print & Update Backoffice"
    )
    if st.button(
        _cp_label,
        key="retail_confirm_and_print",
        type="primary",
        width='stretch',
        disabled=_btn_disabled or _fa_blocked,
        help="Saves order to Backoffice AND immediately opens the print confirmation receipt",
    ):
        _run_confirm_pipeline(do_print=True)

    # SECONDARY: plain confirm without auto-print
    if st.button(
        _confirm_label,
        key="retail_confirm_plain",
        type="secondary",
        width='stretch',
        disabled=_btn_disabled or _fa_blocked,
        help="Save order to Backoffice only — print separately using the receipt panel above",
    ):
        _run_confirm_pipeline(do_print=False)

    # ── FIX: Cancel / Go Back path (audit point #4) ──────────────────────
    # If user does NOT want to confirm, this button safely discards any partial
    # lock state and returns to edit mode — nothing is saved.
    if not _btn_disabled:
        st.markdown("")
        if st.button(
            "✖ Cancel — Go Back & Edit",
            key="retail_cancel_confirm",
            type="secondary",
            width='stretch',
            help="Discard confirmation — return to cart editing. Nothing is saved.",
        ):
            # Clear any lock that guarded_submit may have partially acquired
            try:
                from modules.utils.submit_guard import clear_lock
                clear_lock("retail_confirm")
            except Exception:
                pass
            # Ensure order is not marked locked in session
            st.session_state.pop("_order_locked", None)
            st.session_state.pop("_frozen_payment_total", None)
            # Clear cart fingerprint so confirm button re-enables
            _prov_cancel = st.session_state.get("retail_provisional_order_id", "")
            if _prov_cancel:
                _fp_cancel = f"_confirmed_cart_{_prov_cancel}"
                st.session_state.pop(_fp_cancel, None)
            st.info("↩️ Cancelled — cart is still editable. Make changes and confirm when ready.")
            st.rerun()
    # ─────────────────────────────────────────────────────────────────────


# ============================================================================
# CONFIRMATION RECEIPT — A5 Landscape, shown after order confirm
# ============================================================================

def _update_order_in_place(
    order_id, order_no, cart_lines,
    patient_name, patient_mobile,
    advance_amount, advance_mode, advance_ref, collect_advance,
    delivery_date, delivery_time, user_name
) -> dict:
    """
    Update an existing order in-place:
    1. Soft-delete current order_lines
    2. Insert new order_lines
    3. Update orders header (totals, delivery, payment_mode)
    4. Log change to order_status_history
    5. Record advance payment if collected
    """
    # ── Resolve order_id: must be a UUID, not an order_no string ──────────
    # If order_id looks like an order_no (e.g. CONS-*, R/*), look up the real UUID
    _is_uuid = (order_id and len(str(order_id)) == 36
                and str(order_id).count("-") == 4
                and not str(order_id).upper().startswith(("CONS-", "R/", "W/")))
    if not _is_uuid and order_id:
        try:
            from modules.sql_adapter import run_query as _rq_resolve
            _id_rows = _rq_resolve(
                "SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
                (str(order_id),)
            ) or []
            if _id_rows:
                order_id = _id_rows[0]["id"]
            elif order_no:
                _id_rows2 = _rq_resolve(
                    "SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
                    (str(order_no),)
                ) or []
                if _id_rows2:
                    order_id = _id_rows2[0]["id"]
        except Exception:
            pass

    # Hard guard — if we still don't have a real UUID, abort cleanly
    _resolved_is_uuid = (
        order_id
        and len(str(order_id)) == 36
        and str(order_id).count("-") == 4
        and not str(order_id).upper().startswith(("CONS-", "R/", "W/"))
    )
    if not _resolved_is_uuid:
        return {
            "error": (
                f"Could not resolve a valid order UUID from '{order_id}' / '{order_no}'. "
                "Edit aborted — no changes made."
            ),
            "locked": False,
        }

    # ── 🔴 DB-LEVEL LOCK CHECK — must be FIRST, before any DB writes ──────
    # This is the real edit path. order_repository.save_order() is only used
    # for NEW orders. All edits come here. Lock must be enforced HERE too.
    _LOCKED = {"CONFIRMED", "BILLED", "DISPATCHED", "DELIVERED"}
    try:
        from modules.sql_adapter import run_query as _rq_lock
        _lock_rows = _rq_lock(
            "SELECT status FROM orders WHERE id = %s::uuid LIMIT 1",
            (str(order_id),)
        ) or []
        if _lock_rows:
            _current_status = str(_lock_rows[0].get("status", "")).upper()
            if _current_status in _LOCKED:
                return {
                    "error": (
                        f"Order {order_no} is locked (status={_current_status}). "
                        f"Confirmed orders cannot be modified. "
                        f"Contact a supervisor if changes are essential."
                    ),
                    "locked": True,
                }
    except Exception as _lock_err:
        import logging as _lock_log
        _lock_log.getLogger(__name__).error(
            f"[_update_order_in_place] Lock check failed for {order_id}: {_lock_err}"
        )
        # Safety: if we can't check the lock, REFUSE the edit rather than allow it
        return {
            "error": f"Could not verify order lock status: {_lock_err}. Edit aborted for safety.",
            "locked": True,
        }
    # ──────────────────────────────────────────────────────────────────────

    try:
        from modules.sql_adapter import run_write, run_query
        import uuid, datetime

        # ── Snapshot original state for change log ─────────────────────
        orig = run_query("""
            SELECT ol.eye_side, p.product_name, ol.sph, ol.cyl,
                   ol.quantity, ol.unit_price, ol.total_price
            FROM order_lines ol
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE ol.order_id = %s::uuid
              AND COALESCE(ol.is_deleted, false) = false
        """, (order_id,)) or []
        orig_total = sum(float(r.get("total_price",0)) for r in orig)
        orig_lines = len(orig)

        # ── Soft-delete existing lines ─────────────────────────────────
        run_write("""
            UPDATE order_lines SET is_deleted = true
            WHERE order_id = %s::uuid
        """, (order_id,))

        # ── Apply discount rules before inserting lines ────────────────
        try:
            from modules.pricing.discount_engine import apply_discounts
            _r_party_id = str(st.session_state.get("retail_party_id") or
                              st.session_state.get("selected_party_id") or "")
            # Stamp promo code on all lines at save time
            _pc_save = str(st.session_state.get("_retail_promo_code") or "").strip()
            if _pc_save:
                for _csl in cart_lines:
                    _csl["promo_code"] = _pc_save
            apply_discounts(cart_lines, party_id=_r_party_id, order_type="RETAIL")
            # Club offers — cart-level cross-product check, fires after apply_discounts
            try:
                from modules.pricing.club_engine import apply_club_offers
                apply_club_offers(cart_lines, order_type="RETAIL")
            except Exception: pass
        except Exception as _de:
            pass  # zero-risk: continues with product-level discount_percent

        # ── Insert new lines ───────────────────────────────────────────
        new_total = 0.0
        for ln in cart_lines:
            _lid  = str(uuid.uuid4())
            _pid  = str(ln.get("product_id") or "")
            _eye_raw = str(ln.get("eye_side","O") or "O").upper().strip()
            _eye = {"OTHER":"O","SERVICE":"S","R":"R","L":"L","B":"B"}.get(_eye_raw, _eye_raw[:1] or "O")
            _sph  = ln.get("sph")
            _cyl  = ln.get("cyl")
            _axis = ln.get("axis")
            _add  = ln.get("add_power")
            _qty  = int(ln.get("billing_qty") or ln.get("requested_qty") or ln.get("quantity") or 1)
            _up   = float(ln.get("unit_price", 0) or 0)
            _tot  = float(ln.get("total_price", 0) or 0)
            if _up > 0 and _tot == 0:
                _tot = round(_up * _qty, 2)  # recalculate if missing
            _gp   = float(ln.get("gst_percent", 0) or 0)
            _ga   = float(ln.get("gst_amount", 0) or 0)
            _lp   = ln.get("lens_params") or {}
            _bp   = ln.get("boxing_params") or {}
            import json
            run_write("""
                INSERT INTO order_lines (
                    id, order_id, product_id,
                    sph, cyl, axis, add_power, eye_side,
                    quantity, unit_price, total_price,
                    gst_percent, gst_amount,
                    discount_percent, discount_amount,
                    applied_rule_ids,
                    status, lens_params, boxing_params, suggested_allocation
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s,
                    %s, %s, %s, NULL
                )
            """, (
                _lid, order_id, _pid if _pid else None,
                _sph, _cyl, _axis, _add, _eye,
                _qty, _up, _tot,
                _gp, _ga,
                float(ln.get("discount_percent", 0)),
                float(ln.get("discount_amount", 0)),
                str(ln.get("applied_rule_ids") or ""),
                "PENDING",
                json.dumps(_lp), json.dumps(_bp),
            ))
            new_total += _tot

        new_lines = len(cart_lines)

        # ── Update order header ────────────────────────────────────────
        _del_date = str(delivery_date)[:10] if delivery_date else None
        run_write("""
            UPDATE orders SET
                total_items  = %s,
                total_value  = %s,
                updated_at   = NOW()
            WHERE id = %s::uuid
        """, (new_lines, new_total, order_id))

        # ── Detect changes for log ─────────────────────────────────────
        changes = []
        if orig_lines != new_lines:
            changes.append(f"Lines: {orig_lines} → {new_lines}")
        if abs(orig_total - new_total) > 0.01:
            changes.append(f"Value: ₹{orig_total:,.2f} → ₹{new_total:,.2f}")
        if advance_amount > 0:
            changes.append(f"Advance: ₹{advance_amount:,.2f} ({advance_mode})")
        change_summary = ", ".join(changes) if changes else "Updated"

        # ── Log to order_status_history ────────────────────────────────
        run_write("""
            INSERT INTO order_status_history
                (history_id, order_id, from_status, to_status,
                 changed_by_name, remarks)
            VALUES
                (gen_random_uuid(), %s::uuid, 'EDIT_SOURCE', 'EDITED',
                 %s, %s)
        """, (
            order_id, user_name,
            f"Edited by {user_name} at "
            f"{datetime.datetime.now().strftime('%d %b %Y %H:%M')}. "
            f"{change_summary}."
        ))

        # ── Record advance if collected ────────────────────────────────
        # NOTE: Do NOT call _submit_payment here — it calls st.rerun()
        # internally which kills the rest of execution. Write directly.
        if collect_advance and advance_amount > 0:
            try:
                _pno_rows = run_query("SELECT generate_payment_no() AS pno") or []
                _pno = _pno_rows[0]["pno"] if _pno_rows else f"ADV-EDIT-{order_id[:8]}"
                _pay_id = str(uuid.uuid4())
                run_write("""
                    INSERT INTO payments (
                        id, payment_no, party_id, party_name,
                        challan_id, invoice_id, order_id,
                        payment_date, payment_mode, amount,
                        reference_no, remarks, payment_type,
                        is_advance, advance_for_order_id, created_by
                    ) VALUES (
                        %s, %s, NULL, %s,
                        NULL, NULL, %s::uuid,
                        %s, %s, %s,
                        %s, %s, %s,
                        true, %s::uuid, %s
                    )
                """, (
                    _pay_id, _pno, patient_name,
                    order_id,
                    datetime.date.today(), advance_mode, advance_amount,
                    advance_ref or None, "Additional payment at order edit", "ADVANCE",
                    order_id, user_name,
                ))
                # Also update order payment_mode
                run_write(
                    "UPDATE orders SET payment_mode=%s WHERE id=%s::uuid",
                    (advance_mode, order_id)
                )
                changes.append(f"Advance ₹{advance_amount:,.2f} ({advance_mode}) recorded")
            except Exception as _pay_ex:
                changes.append(f"Advance note: {_pay_ex}")

        return {"success": True, "order_no": order_no, "total": new_total,
                "changes": changes}

    except Exception as ex:
        import traceback
        return {"error": str(ex), "traceback": traceback.format_exc()}


def _build_edit_receipt_snapshot(
    order_no, patient_name, patient_mobile,
    lines, advance_amount, advance_mode,
    delivery_date, delivery_time
) -> dict:
    """Build receipt snapshot dict for edited order — same format as new order."""
    import datetime
    return {
        "order_no":       order_no,
        "patient_name":   patient_name,
        "patient_mobile": patient_mobile,
        "lines":          list(lines),
        "advance_amount": advance_amount,
        "advance_mode":   advance_mode,
        "advance_ref":    "",
        "delivery_date":  delivery_date,
        "delivery_time":  delivery_time,
        "confirmed_at":   datetime.datetime.now().strftime("%d %b %Y %I:%M %p"),
        "case_no":        "",
    }


def _open_html_print(html: str, filename: str = "print.html") -> None:
    """
    Save HTML to temp file and open in default browser via ShellExecute.
    Blob/window.open is blocked by Streamlit iframe sandbox.
    ShellExecute opens the file at OS level — bypasses all iframe restrictions.
    """
    import os, tempfile
    tmp = os.path.join(tempfile.gettempdir(), filename)
    with open(tmp, "w", encoding="utf-8") as _f:
        _f.write(html)
    try:
        import win32api
        win32api.ShellExecute(0, "open", tmp, None, ".", 1)
    except Exception:
        try:
            import webbrowser
            webbrowser.open("file:///" + tmp.replace(os.sep, "/"))
        except Exception as _ex:
            import streamlit as _st
            _st.warning(f"Could not open print dialog: {_ex}. File saved at: {tmp}")


def _build_confirmation_receipt_html(snap: dict, shop_info: dict) -> str:
    """
    A5 Landscape (210mm × 148mm) Confirmation Receipt.
    Sections:
      • Header: shop name + CONFIRMATION RECEIPT label
      • Patient band: name, mobile, case no, date/time
      • Order items table: eye | product | power | qty | unit price | total
      • Totals: base, GST, grand total
      • Advance payment band (if any)
      • Balance due band
      • Dual barcode footer: patient barcode | order barcode
    """
    from modules.printing.patient_card_printer import barcode_svg as _bsvg
    from modules.printing.patient_card_printer import ensure_patient_id

    order_no  = snap.get("order_no", "")
    pat_name  = snap.get("patient_name", "")
    pat_mob   = snap.get("patient_mobile", "")
    case_no   = snap.get("case_no", "")
    conf_at   = snap.get("confirmed_at", "")
    lines     = snap.get("lines", [])
    adv_amt   = float(snap.get("advance_amount") or 0)
    adv_mode  = snap.get("advance_mode", "")
    adv_ref   = snap.get("advance_ref", "")
    rx_r      = snap.get("rx_r") or {}
    rx_l      = snap.get("rx_l") or {}

    # Delivery date/time
    _del_date = snap.get("delivery_date", "")
    _del_time = snap.get("delivery_time", "6:00 PM")
    if _del_date:
        try:
            import datetime as _dt_r
            if hasattr(_del_date, "strftime"):
                _del_date_str = _del_date.strftime("%A, %d %b %Y")
            else:
                _del_date_str = str(_del_date)
        except Exception:
            _del_date_str = str(_del_date)
    else:
        _del_date_str = ""
    _delivery_str = f"{_del_date_str}  ·  {_del_time}" if _del_date_str else _del_time

    shop      = shop_info.get("shop_name", "")
    addr      = ", ".join(filter(None, [
        shop_info.get("shop_address",""), shop_info.get("shop_address2",""),
        shop_info.get("shop_city",""), shop_info.get("shop_pincode","")
    ]))
    phone     = shop_info.get("shop_phone","")
    gstin     = shop_info.get("shop_gstin","")
    footer_txt = shop_info.get("print_footer","This order is subject to availability.")

    # ── Patient barcode ───────────────────────────────────────────────────
    pat_id = snap.get("order_id","")  # best effort — may not be patient UUID
    try:
        pat_bc = ensure_patient_id(pat_id) if pat_id and len(pat_id) > 10 else ""
    except Exception:
        pat_bc = ""
    pat_bc_svg   = _bsvg(pat_bc, width=170, height=40)   if pat_bc   else ""
    order_bc_svg = _bsvg(order_no, width=170, height=40)

    # ── Totals ─────────────────────────────────────────────────────────────
    grand_total = sum(float(l.get("total_price") or 0) for l in lines)
    gst_total   = sum(
        round(float(l.get("total_price",0)) -
              float(l.get("total_price",0)) / (1 + float(l.get("gst_percent",0) or 0) / 100), 2)
        for l in lines
    )
    base_total  = round(grand_total - gst_total, 2)
    balance     = round(grand_total - adv_amt, 2)

    # ── RX summary line ────────────────────────────────────────────────────
    def _fv(v):
        if v is None: return "—"
        try:
            f = float(v)
            if f != f: return "—"   # nan
            return f"+{f:.2f}" if f > 0 else f"{f:.2f}"
        except: return "—"
    def _fa(v):
        if not v: return "—"
        try: return str(int(float(v))) if float(v) else "—"
        except: return "—"

    rx_lines_html = ""
    if rx_r or rx_l:
        def _rx_row(eye, rx, bg):
            s=_fv(rx.get("sph")); c=_fv(rx.get("cyl"))
            a=_fa(rx.get("axis")); d=_fv(rx.get("add"))
            if all(v=="—" for v in [s,c,a,d]): return ""
            return (f"<tr style='background:{bg}'>"
                    f"<td style='padding:3px 7px;font-weight:700'>{eye}</td>"
                    f"<td style='padding:3px 7px;text-align:center'>{s}</td>"
                    f"<td style='padding:3px 7px;text-align:center'>{c}</td>"
                    f"<td style='padding:3px 7px;text-align:center'>{a}</td>"
                    f"<td style='padding:3px 7px;text-align:center'>{d}</td></tr>")
        rr = _rx_row("R", rx_r, "#eff6ff")
        rl = _rx_row("L", rx_l, "#f0fdf4")
        if rr or rl:
            rx_lines_html = f"""
            <div style="margin:6px 0 8px">
              <div style="font-size:8pt;font-weight:700;color:#1e3a5f;
                          border-bottom:1px solid #e2e8f0;padding-bottom:2px;margin-bottom:4px">
                Prescription (Rx)
              </div>
              <table style="border-collapse:collapse;font-size:8pt;width:auto">
                <tr style="background:#1e3a5f;color:#fff">
                  <th style="padding:3px 7px;text-align:left">Eye</th>
                  <th style="padding:3px 7px">SPH</th>
                  <th style="padding:3px 7px">CYL</th>
                  <th style="padding:3px 7px">AXIS</th>
                  <th style="padding:3px 7px">ADD</th>
                </tr>
                {rr}{rl}
              </table>
            </div>"""

    # ── Order lines rows — merge R+L if same product ─────────────────────────
    def _pw(l):
        """Format power string for a line."""
        parts = []
        if l.get("sph") is not None:
            parts.append(_fv(l.get("sph")))
        if l.get("cyl") not in (None, 0, 0.0):
            parts.append(f"/{_fv(l.get('cyl'))}×{_fa(l.get('axis'))}")
        if l.get("add_power") not in (None, 0, 0.0):
            parts.append(f"ADD {_fv(l.get('add_power'))}")
        return " ".join(parts) if parts else "—"

    # Group consecutive R+L lines with same product_id into pairs
    rows_html = ""
    used = set()
    row_idx = 0
    for i, l in enumerate(lines):
        if i in used:
            continue
        eye_i = str(l.get("eye_side","")).upper()
        pid_i = str(l.get("product_id",""))
        pname = l.get("product_name","")
        brand = l.get("brand","")
        prod_disp = f"<b>{brand}</b> {pname}" if brand else pname
        gp  = float(l.get("gst_percent") or 0)
        bg  = "#ffffff" if row_idx % 2 == 0 else "#f8fafc"

        # Look for matching opposite eye with same product
        pair = None
        if eye_i in ("R","L") and pid_i:
            opp = "L" if eye_i == "R" else "R"
            for j, m in enumerate(lines):
                if j <= i or j in used:
                    continue
                if (str(m.get("eye_side","")).upper() == opp and
                        str(m.get("product_id","")) == pid_i):
                    pair = (j, m)
                    break

        if pair:
            j, m = pair
            used.add(i); used.add(j)
            # R first, L second
            r_line = l if eye_i == "R" else m
            l_line = m if eye_i == "R" else l
            r_pw  = _pw(r_line)
            l_pw  = _pw(l_line)
            r_qty = int(r_line.get("billing_qty", r_line.get("requested_qty",1)) or 1)
            l_qty = int(l_line.get("billing_qty", l_line.get("requested_qty",1)) or 1)
            tot   = float(r_line.get("total_price",0)) + float(l_line.get("total_price",0))
            up    = float(r_line.get("unit_price",0))
            qty_disp = f"{r_qty+l_qty} PCS (pair)"
            power_disp = (
                f"<span style='color:#1d4ed8'>R: {r_pw}</span>"
                f"<br><span style='color:#065f46'>L: {l_pw}</span>"
            )
            rows_html += (
                f"<tr style='background:{bg}'>"
                f"<td style='padding:4px 8px;text-align:center;font-size:7.5pt;"
                f"color:#475569'>R+L</td>"
                f"<td style='padding:4px 8px'>{prod_disp}</td>"
                f"<td style='padding:4px 8px;font-family:monospace;font-size:7.5pt'>"
                f"{power_disp}</td>"
                f"<td style='padding:4px 8px;text-align:center'>{qty_disp}</td>"
                f"<td style='padding:4px 8px;text-align:right'>₹{up:,.2f}/pc</td>"
                f"<td style='padding:4px 8px;text-align:center;color:#64748b'>{gp:.0f}%</td>"
                f"<td style='padding:4px 8px;text-align:right;font-weight:700'>₹{tot:,.2f}</td>"
                f"</tr>"
            )
        else:
            # Single line — show normally
            used.add(i)
            eye_lbl = {"R":"👁 R","L":"👁 L","B":"Both","S":"Service",
                       "O":"—","OTHER":"—","SERVICE":"Service"}.get(eye_i, eye_i)
            power = _pw(l)
            qty  = l.get("display_qty") or f"{l.get('billing_qty',l.get('requested_qty',0))} PCS"
            up   = float(l.get("unit_price") or 0)
            tot  = float(l.get("total_price") or 0)
            rows_html += (
                f"<tr style='background:{bg}'>"
                f"<td style='padding:4px 8px;text-align:center;color:#475569'>{eye_lbl}</td>"
                f"<td style='padding:4px 8px'>{prod_disp}</td>"
                f"<td style='padding:4px 8px;font-family:monospace;font-size:8pt;"
                f"color:#475569'>{power}</td>"
                f"<td style='padding:4px 8px;text-align:center'>{qty}</td>"
                f"<td style='padding:4px 8px;text-align:right'>₹{up:,.2f}</td>"
                f"<td style='padding:4px 8px;text-align:center;color:#64748b'>{gp:.0f}%</td>"
                f"<td style='padding:4px 8px;text-align:right;font-weight:700'>₹{tot:,.2f}</td>"
                f"</tr>"
            )
        row_idx += 1

    # ── Advance band ───────────────────────────────────────────────────────
    adv_html = ""
    if adv_amt > 0:
        adv_ref_str = f" · Ref: {adv_ref}" if adv_ref else ""
        adv_html = f"""
        <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:4px;
                    padding:6px 12px;margin:8px 0;display:flex;
                    justify-content:space-between;align-items:center">
          <div>
            <span style="font-size:9pt;font-weight:700;color:#166534">
              ✅ Advance Collected
            </span>
            <span style="font-size:8pt;color:#64748b;margin-left:8px">
              {adv_mode}{adv_ref_str}
            </span>
          </div>
          <span style="font-size:11pt;font-weight:900;color:#166534">
            ₹{adv_amt:,.2f}
          </span>
        </div>
        <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:4px;
                    padding:6px 12px;margin:0 0 8px;display:flex;
                    justify-content:space-between;align-items:center">
          <span style="font-size:9pt;font-weight:700;color:#c2410c">
            ⏳ Balance Due on Delivery
          </span>
          <span style="font-size:11pt;font-weight:900;color:#c2410c">
            ₹{balance:,.2f}
          </span>
        </div>"""
    else:
        adv_html = f"""
        <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:4px;
                    padding:6px 12px;margin:8px 0;display:flex;
                    justify-content:space-between;align-items:center">
          <span style="font-size:9pt;font-weight:700;color:#c2410c">
            ⏳ Amount Due on Delivery
          </span>
          <span style="font-size:11pt;font-weight:900;color:#c2410c">
            ₹{grand_total:,.2f}
          </span>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: 210mm 148mm landscape; margin: 5mm 6mm; }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: Arial, Helvetica, sans-serif; font-size: 9pt;
        color: #0f172a; background: white; }}

/* ── Header ── */
.hdr {{ display:flex; justify-content:space-between; align-items:flex-start;
        background:#0f172a; color:#fff; padding:5px 10px; margin-bottom:0; }}
.shop-name {{ font-size:14pt; font-weight:900; letter-spacing:.03em; }}
.shop-sub  {{ font-size:7pt; color:#94a3b8; margin-top:2px; }}
.doc-title {{ text-align:right; }}
.doc-title .label {{ font-size:13pt; font-weight:900; letter-spacing:.05em;
                     color:#fbbf24; }}
.doc-title .order  {{ font-family:monospace; font-size:10pt; font-weight:700;
                      color:#e2e8f0; margin-top:2px; }}

/* ── Patient band ── */
.patient-band {{ display:flex; justify-content:space-between;
                 background:#f1f5f9; padding:5px 10px;
                 border-bottom:1px solid #e2e8f0; font-size:8.5pt; }}
.pat-name {{ font-size:11pt; font-weight:900; color:#0f172a; }}
.pat-sub  {{ font-size:7.5pt; color:#475569; margin-top:1px; }}

/* ── Main layout: 2 columns ── */
.body-cols {{ display:flex; gap:8px; padding:6px 10px; }}
.col-left  {{ flex:1.4; }}
.col-right {{ flex:0.6; border-left:1px solid #e2e8f0; padding-left:8px; }}

/* ── Items table ── */
table.items {{ width:100%; border-collapse:collapse; font-size:8pt; }}
table.items th {{
    background:#0f172a; color:#fff;
    padding:4px 8px; text-align:center; font-size:7.5pt;
}}
table.items th:nth-child(2) {{ text-align:left; }}
table.items td {{ border-bottom:0.3px solid #e2e8f0; }}

/* ── Totals ── */
.totals-block {{ margin-top:6px; border:1px solid #e2e8f0;
                 border-radius:4px; overflow:hidden; font-size:8.5pt; }}
.tot-row {{ display:flex; justify-content:space-between;
            padding:3px 8px; }}
.tot-row.grand {{ background:#0f172a; color:#fff;
                  font-size:10pt; font-weight:900; padding:5px 8px; }}

/* ── Barcode footer ── */
.bc-footer {{ display:flex; gap:20px; align-items:center;
              padding:4px 10px; border-top:1px dashed #cbd5e1;
              background:#f8fafc; margin-top:auto; }}
.bc-item {{ text-align:center; }}
.bc-label {{ font-size:6.5pt; color:#64748b; font-weight:700;
             text-transform:uppercase; letter-spacing:.06em;
             margin-bottom:1px; }}

/* ── Footer ── */
.page-footer {{ text-align:center; font-size:7pt; color:#94a3b8;
                border-top:1px dashed #e2e8f0; padding:3px 10px; }}

@media print {{
  @page {{ size: 210mm 148mm landscape; margin: 5mm 6mm; }}
  body {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
}}
</style>
</head><body>

<!-- ── HEADER ── -->
<div class="hdr">
  <div>
    <div class="shop-name">{shop.upper()}</div>
    <div class="shop-sub">{addr}{' &nbsp;·&nbsp; ' + phone if phone else ''}
      {' &nbsp;·&nbsp; GSTIN: ' + gstin if gstin else ''}
    </div>
  </div>
  <div class="doc-title">
    <div class="label">CONFIRMATION RECEIPT</div>
    <div class="order">{order_no}</div>
    <div style="font-size:7pt;color:#94a3b8;margin-top:1px">{conf_at}</div>
  </div>
</div>

<!-- ── PATIENT BAND ── -->
<div class="patient-band">
  <div>
    <div class="pat-name">{pat_name}</div>
    <div class="pat-sub">
      📞 {pat_mob or "—"}
      { f"&nbsp;·&nbsp; Case: <b>{case_no}</b>" if case_no else "" }
    </div>
  </div>
  <div style="text-align:right;font-size:7.5pt;color:#475569">
    <div><b>Order:</b> {order_no}</div>
    <div><b>Confirmed:</b> {conf_at}</div>
    <div style="margin-top:2px;font-size:7pt;color:#f59e0b;font-weight:700">
      ⚠️ Confirmation receipt — not a tax invoice
    </div>
  </div>
</div>

<!-- ── BODY ── -->
<div class="body-cols">

  <!-- LEFT: items + totals -->
  <div class="col-left">

    <table class="items">
      <thead>
        <tr>
          <th style="width:5%">Eye</th>
          <th style="width:32%;text-align:left">Product</th>
          <th style="width:18%">Power</th>
          <th style="width:9%">Qty</th>
          <th style="width:12%;text-align:right">Rate</th>
          <th style="width:6%">GST</th>
          <th style="width:13%;text-align:right">Amount</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>

    <!-- Totals -->
    <div class="totals-block" style="margin-top:5px">
      <div class="tot-row" style="background:#f8fafc">
        <span>Base (excl. GST)</span>
        <span>₹{base_total:,.2f}</span>
      </div>
      <div class="tot-row" style="background:#f8fafc">
        <span>GST</span>
        <span>₹{gst_total:,.2f}</span>
      </div>
      <div class="tot-row grand">
        <span>GRAND TOTAL (MRP)</span>
        <span>₹{grand_total:,.2f}</span>
      </div>
    </div>

    {adv_html}

  </div>

  <!-- RIGHT: Rx + notes -->
  <div class="col-right">

    {rx_lines_html}

    { f"""<div style="background:#f0fdf4;border:2px solid #22c55e;border-radius:6px;
                padding:7px 10px;margin-bottom:8px;text-align:center">
      <div style="font-size:7pt;font-weight:700;color:#166534;letter-spacing:.08em;
                  text-transform:uppercase;margin-bottom:2px">📅 Expected Delivery</div>
      <div style="font-size:10pt;font-weight:900;color:#15803d">{_del_date_str}</div>
      <div style="font-size:9pt;font-weight:700;color:#166534">{_del_time}</div>
    </div>""" if _del_date_str else "" }

    <div style="background:#eff6ff;border-radius:4px;padding:6px 8px;
                font-size:7.5pt;margin-bottom:8px">
      <div style="font-weight:700;color:#1e3a5f;margin-bottom:3px">
        📋 Order Notes
      </div>
      <div style="color:#475569;line-height:1.6">
        • Products will be ready as per production schedule<br>
        • Fitting / colouring charges added at delivery<br>
        • Present this receipt to collect your order<br>
        • For queries call: {phone or "the store"}
      </div>
    </div>

    <div style="background:#fef9c3;border:1px solid #fde68a;border-radius:4px;
                padding:5px 8px;font-size:7.5pt;color:#92400e">
      <b>⚠ Not a Tax Invoice</b><br>
      Final challan/invoice issued at delivery.
    </div>

  </div>
</div>

<!-- ── BARCODE FOOTER ── -->
<div class="bc-footer">
  { f'<div class="bc-item"><div class="bc-label">Patient ID</div>{pat_bc_svg}</div>' if pat_bc_svg else "" }
  <div class="bc-item">
    <div class="bc-label">Order No</div>
    {order_bc_svg}
  </div>
  <div style="flex:1;text-align:right;font-size:7.5pt;color:#475569">
    {f"<b>Advance:</b> ₹{adv_amt:,.2f} ({adv_mode})" if adv_amt > 0 else ""}
    { f"<br><b>Balance on delivery:</b> ₹{balance:,.2f}" if adv_amt > 0 else f"<br><b>Due on delivery:</b> ₹{grand_total:,.2f}" }
  </div>
</div>

<!-- ── FOOTER ── -->
<div class="page-footer">
  {footer_txt} &nbsp;·&nbsp; {shop} &nbsp;·&nbsp; {addr}
</div>

<script>window.onload = function() {{ window.print(); }}</script>
</body></html>"""


def render_confirmation_receipt():
    """
    Render the post-confirm Confirmation Receipt panel with print button.
    Only shows if receipt belongs to the currently active patient.
    FIX: Always tries DB → session → UI as data source (audit point #8).
    """
    snap = st.session_state.get("_receipt_snapshot")
    if not snap:
        return

    # ── FIX: DB fallback — enrich session snap with live DB data ─────────
    # If snap has an order_id, try to refresh critical fields from DB so the
    # receipt always reflects the saved record, not stale session state.
    _snap_order_id = snap.get("order_id", "")
    if _snap_order_id and len(str(_snap_order_id)) > 10:
        try:
            from modules.sql_adapter import run_query as _rq_rcpt
            _db_row = _rq_rcpt(
                """
                SELECT order_no, patient_name, patient_mobile,
                       total_value, payment_status
                  FROM orders
                 WHERE id = %s::uuid LIMIT 1
                """,
                (str(_snap_order_id),)
            ) or []
            if _db_row:
                _dbr = _db_row[0]
                # Only override if DB has the field and session is empty/stale
                if _dbr.get("order_no"):
                    snap["order_no"]      = _dbr["order_no"]
                if _dbr.get("patient_name"):
                    snap["patient_name"]  = _dbr["patient_name"]
                if _dbr.get("patient_mobile"):
                    snap["patient_mobile"]= _dbr["patient_mobile"]
        except Exception:
            pass  # non-fatal — session data is the fallback
    # ─────────────────────────────────────────────────────────────────────

    # Guard: don't show a ghost receipt from a different patient
    _snap_patient = str(snap.get("patient_name","")).strip().lower()
    _cur_patient  = str(st.session_state.get("retail_patient_name","")).strip().lower()
    if _snap_patient and _cur_patient and _snap_patient != _cur_patient:
        # Different patient loaded — silently discard stale receipt
        st.session_state.pop("_receipt_snapshot", None)
        return

    order_no = snap.get("order_no", "")

    st.markdown("---")
    # ── Banner ──────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='background:linear-gradient(90deg,#0f172a,#1e3a5f);"
        f"border-radius:10px;padding:12px 18px;margin-bottom:10px;"
        f"border-left:4px solid #fbbf24'>"
        f"<div style='color:#fbbf24;font-size:0.68rem;letter-spacing:.1em;"
        f"text-transform:uppercase;font-weight:700'>✅ Order Confirmed</div>"
        f"<div style='color:#f1f5f9;font-family:monospace;font-size:1.1rem;"
        f"font-weight:900;margin-top:2px'>{order_no}</div>"
        f"<div style='color:#94a3b8;font-size:0.72rem;margin-top:1px'>"
        f"{snap.get('patient_name','')} &nbsp;·&nbsp; {snap.get('confirmed_at','')}"
        f"</div></div>",
        unsafe_allow_html=True,
    )

    # ── Helper: fire the browser print dialog ───────────────────────────
    def _fire_print_dialog():
        try:
            from modules.settings.shop_master import get_unit_info
            _si = get_unit_info("retail")
        except Exception:
            _si = {}
        try:
            import streamlit.components.v1 as _comp
            import base64 as _b64
            _html = _build_confirmation_receipt_html(snap, _si)
            _b64_html = _b64.b64encode(_html.encode("utf-8")).decode()
            _comp.html(
                f"""<script>
                var w = window.open('about:blank','_print','width=900,height=700');
                if(w) {{
                    w.document.write(atob('{_b64_html}'));
                    w.document.close();
                    setTimeout(function(){{ w.print(); }}, 400);
                }} else {{
                    var b = new Blob([atob('{_b64_html}')],{{type:'text/html'}});
                    var u = URL.createObjectURL(b);
                    window.open(u,'_blank');
                }}
                </script>""",
                height=0,
            )
            st.success("✅ Print dialog opened")
        except Exception as _pe:
            st.error(f"Print failed: {_pe}")
            try:
                _open_html_print(_html)
            except Exception:
                pass

    # ── Auto-print if triggered by "Confirm, Print & Add to Backoffice" ──
    if st.session_state.pop("_auto_print_receipt", False):
        st.info("🖨️ Auto-opening print dialog for order confirmation receipt…")
        _fire_print_dialog()

    # ── Print button ────────────────────────────────────────────────────
    _pr_c1, _pr_c2, _pr_c3 = st.columns([2, 2, 1])
    with _pr_c1:
        if st.button("🖨️ Print Confirmation Receipt",
                     key="print_confirmation_receipt",
                     type="primary",
                     width='stretch',
                     help="A5 Landscape — opens browser print dialog"):
            _fire_print_dialog()

    with _pr_c2:
        if st.button("🗑️ Dismiss Receipt",
                     key="dismiss_receipt",
                     width='stretch',
                     help="Clear this confirmation panel"):
            st.session_state.pop("_receipt_snapshot", None)
            st.session_state.pop("_post_save_data", None)
            st.rerun()

    with _pr_c3:
        _adv = float(snap.get("advance_amount") or 0)
        _tot = sum(float(l.get("total_price",0)) for l in snap.get("lines",[]))
        _bal = round(_tot - _adv, 2)
        if _adv > 0:
            st.metric("Balance Due", f"₹{_bal:,.2f}")
        else:
            st.metric("Total Due", f"₹{_tot:,.2f}")

    # ── Mini summary ────────────────────────────────────────────────────
    with st.expander(f"📋 Receipt Preview — {order_no}", expanded=False):
        _lines = snap.get("lines", [])
        _tot   = sum(float(l.get("total_price",0)) for l in _lines)
        _adv   = float(snap.get("advance_amount") or 0)
        st.caption(f"**Patient:** {snap.get('patient_name','')}  "
                   f"**Mobile:** {snap.get('patient_mobile','')}  "
                   f"**Confirmed:** {snap.get('confirmed_at','')}")
        for _l in _lines:
            _eye = _l.get("eye_side","")
            _pn  = _l.get("product_name","")
            _qty = _l.get("display_qty") or str(_l.get("billing_qty",0))
            _tot_l = float(_l.get("total_price",0))
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;"
                f"font-size:0.8rem;padding:2px 0;border-bottom:1px solid #f1f5f9'>"
                f"<span><b style='color:#64748b'>{_eye}</b> &nbsp; {_pn}</span>"
                f"<span style='font-family:monospace'>{_qty} &nbsp; ₹{_tot_l:,.2f}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        st.markdown(
            f"<div style='text-align:right;font-weight:700;padding:4px 0;"
            f"font-size:0.9rem'>Total: ₹{_tot:,.2f}"
            + (f" &nbsp; Advance: ₹{_adv:,.2f}" if _adv else "") +
            f"</div>",
            unsafe_allow_html=True,
        )


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_retail_punching():
    """Main render function for retail order punching"""
    
    # 🎨 Premium Retail Punching Theme
    mild_orange_css = """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap');

    /* ── Base ─────────────────────────────────────────────────── */
    .stApp {
        background: #f7f3ef !important;
        font-family: 'DM Sans', sans-serif !important;
    }
    .block-container {
        padding-top: 0.2rem !important;
        padding-bottom: 1rem !important;
        max-width: 1100px !important;
    }

    /* ── Typography tightening ────────────────────────────────── */
    h1, h2, h3, h4, h5 {
        font-family: 'DM Sans', sans-serif !important;
        color: #1a1a2e !important;
        font-weight: 700 !important;
        margin-top: 0rem !important;
        margin-bottom: 0.25rem !important;
        border-bottom: none !important;
        padding-bottom: 0 !important;
    }
    p, label, div[data-testid="stMarkdownContainer"] p {
        font-size: 0.85rem !important;
        color: #374151 !important;
        line-height: 1.4 !important;
    }

    /* ── Reduce vertical whitespace ──────────────────────────── */
    .element-container { margin-bottom: 4px !important; }
    .stHorizontalBlock { gap: 8px !important; }
    div[data-testid="stVerticalBlock"] > div { gap: 4px !important; }
    .stMarkdown { margin-bottom: 2px !important; }

    /* ── Buttons ──────────────────────────────────────────────── */
    .stButton > button {
        font-family: 'DM Sans', sans-serif !important;
        font-size: 0.78rem !important;
        font-weight: 600 !important;
        padding: 5px 12px !important;
        border-radius: 7px !important;
        transition: all 0.2s ease !important;
        letter-spacing: 0.02em !important;
    }
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #e94560 0%, #c62a47 100%) !important;
        color: #fff !important;
        border: none !important;
        box-shadow: 0 2px 10px rgba(233,69,96,0.35) !important;
    }
    .stButton > button[kind="primary"]:hover {
        background: linear-gradient(135deg, #ff6b81 0%, #e94560 100%) !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 4px 16px rgba(233,69,96,0.45) !important;
    }
    .stButton > button:not([kind="primary"]) {
        background: #fff !important;
        color: #e94560 !important;
        border: 1.5px solid #e94560 !important;
    }
    .stButton > button:not([kind="primary"]):hover {
        background: #fff0f3 !important;
        border-color: #c62a47 !important;
    }

    /* ── Inputs ───────────────────────────────────────────────── */
    .stTextInput > div > div > input,
    .stNumberInput > div > div > input,
    .stSelectbox > div > div > div {
        font-family: 'DM Mono', monospace !important;
        font-size: 0.82rem !important;
        background: #fff !important;
        border: 1.5px solid #e2d9d0 !important;
        border-radius: 7px !important;
        color: #1a1a2e !important;
        padding: 5px 10px !important;
        transition: border-color 0.2s !important;
    }
    .stTextInput > div > div > input:focus,
    .stNumberInput > div > div > input:focus {
        border-color: #e94560 !important;
        box-shadow: 0 0 0 3px rgba(233,69,96,0.12) !important;
    }
    .stTextInput > label, .stNumberInput > label,
    .stSelectbox > label, .stRadio > label {
        font-size: 0.75rem !important;
        font-weight: 600 !important;
        color: #6b7280 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.05em !important;
        margin-bottom: 2px !important;
    }

    /* ── Cards / Expanders ────────────────────────────────────── */
    .streamlit-expanderHeader {
        background: linear-gradient(90deg,#1a1a2e,#16213e) !important;
        color: #e2e8f0 !important;
        border-radius: 8px !important;
        font-size: 0.82rem !important;
        font-weight: 700 !important;
        padding: 6px 12px !important;
        border: none !important;
    }
    .streamlit-expanderContent {
        background: #fff !important;
        border: 1.5px solid #e2d9d0 !important;
        border-top: none !important;
        border-radius: 0 0 8px 8px !important;
        padding: 8px 12px !important;
    }

    /* ── Metrics ──────────────────────────────────────────────── */
    div[data-testid="metric-container"] {
        background: #fff !important;
        border: 1.5px solid #e2d9d0 !important;
        border-radius: 9px !important;
        padding: 6px 10px !important;
        box-shadow: 0 1px 4px rgba(0,0,0,0.06) !important;
    }
    div[data-testid="metric-container"] label {
        font-size: 0.68rem !important;
        color: #9ca3af !important;
        text-transform: uppercase !important;
        letter-spacing: 0.06em !important;
    }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] {
        font-family: 'DM Mono', monospace !important;
        font-size: 1.1rem !important;
        font-weight: 700 !important;
        color: #1a1a2e !important;
    }

    /* ── Tabs ─────────────────────────────────────────────────── */
    .stTabs [data-baseweb="tab-list"] {
        background: #fff !important;
        border-bottom: 2px solid #e2d9d0 !important;
        border-radius: 0 !important;
        gap: 0 !important;
    }
    .stTabs [data-baseweb="tab"] {
        font-size: 0.78rem !important;
        font-weight: 600 !important;
        color: #6b7280 !important;
        padding: 6px 16px !important;
        border-radius: 0 !important;
    }
    .stTabs [aria-selected="true"] {
        color: #e94560 !important;
        border-bottom: 2.5px solid #e94560 !important;
    }

    /* ── Alerts ───────────────────────────────────────────────── */
    .stSuccess {
        background: #f0fdf4 !important;
        border-left: 3px solid #22c55e !important;
        border-radius: 7px !important;
        font-size: 0.8rem !important;
        padding: 6px 10px !important;
    }
    .stWarning {
        background: #fffbeb !important;
        border-left: 3px solid #f59e0b !important;
        border-radius: 7px !important;
        font-size: 0.8rem !important;
        padding: 6px 10px !important;
    }
    .stInfo {
        background: #eff6ff !important;
        border-left: 3px solid #3b82f6 !important;
        border-radius: 7px !important;
        font-size: 0.8rem !important;
        padding: 6px 10px !important;
    }
    .stError {
        background: #fef2f2 !important;
        border-left: 3px solid #ef4444 !important;
        border-radius: 7px !important;
        font-size: 0.8rem !important;
        padding: 6px 10px !important;
    }

    /* ── Radio ────────────────────────────────────────────────── */
    .stRadio > div { gap: 6px !important; flex-wrap: wrap !important; }
    .stRadio > div > label {
        font-size: 0.78rem !important;
        font-weight: 600 !important;
        background: #fff !important;
        border: 1.5px solid #e2d9d0 !important;
        border-radius: 20px !important;
        padding: 3px 12px !important;
        cursor: pointer !important;
        transition: all 0.15s !important;
        color: #374151 !important;
        text-transform: none !important;
        letter-spacing: 0 !important;
    }
    .stRadio > div > label:has(input:checked) {
        background: #1a1a2e !important;
        border-color: #1a1a2e !important;
        color: #fff !important;
    }

    /* ── DataFrames ───────────────────────────────────────────── */
    .stDataFrame {
        border: 1.5px solid #e2d9d0 !important;
        border-radius: 8px !important;
        font-size: 0.8rem !important;
        overflow: hidden !important;
    }

    /* ── Checkbox ─────────────────────────────────────────────── */
    .stCheckbox > label {
        font-size: 0.82rem !important;
        font-weight: 500 !important;
        color: #374151 !important;
        text-transform: none !important;
        letter-spacing: 0 !important;
    }


    </style>
    """
    
    # Apply the premium theme
    st.markdown(mild_orange_css, unsafe_allow_html=True)

    # ── Re-entry reset guard ───────────────────────────────────────────
    # CONSULT_BILLING: converting consultation to billing — force Full Billing mode
    if st.session_state.get("_erp_mode") == "CONSULT_BILLING":
        st.session_state["_visit_mode_default"] = 0
        st.session_state.pop("retail_visit_mode", None)
        st.session_state.pop("_editing_consult_order_id", None)
        st.session_state.pop("_force_consultation_tab", None)
        # Carry the consultation UUID so is_converted gets set after retail save
        _consult_billing_oid = (
            st.session_state.get("_erp_consult_oid","") or
            st.session_state.get("_erp_order_id","")
        )
        if _consult_billing_oid:
            st.session_state["_retail_consult_source_id"] = _consult_billing_oid
        st.session_state.pop("_erp_mode", None)

    # Fires a full state wipe whenever the user clicks 'Retail Order' in
    # the sidebar — even when already on Retail (same-page re-entry).
    # app.py bumps _retail_entry_count on every sidebar click so this
    # guard detects the change and resets exactly once per click.
    # Consultation prefill keys (_consult_fee_lines, _retail_consult_source_id)
    # survive because they are not prefixed retail_ and are not in RESET_MAP.
    _cur_entry = st.session_state.get("_retail_entry_count", 0)
    _lst_entry = st.session_state.get("_retail_last_entry_seen", -1)
    if _cur_entry != _lst_entry:
        st.session_state["_retail_last_entry_seen"] = _cur_entry
        # GUARD: never wipe when a consultation is being loaded into retail.
        # _consult_prefill or _retail_consult_source_id means we just came from
        # a "Bill Now" click — wiping would destroy the patient + fee lines.
        _has_incoming_consult = bool(
            st.session_state.get("_consult_prefill") or
            st.session_state.get("_retail_consult_source_id") or
            st.session_state.get("_consult_fee_lines") or
            st.session_state.get("_editing_consult_order_id") or  # edit mode from Orders
            st.session_state.get("_force_consultation_tab") or    # fresh new consultation
            st.session_state.get("_visit_mode_default") == 1      # consultation mode active
        )
        # Only wipe if there is stale state AND no consultation in progress
        if not _has_incoming_consult and (
            st.session_state.get("retail_patient_id") or
            st.session_state.get("retail_order_lines")
        ):
            industrial_reset("ALL")
            for _clr_k in (
                "_receipt_snapshot", "_last_receipt_key",
                "_post_save_data", "_show_receipt_top",
                "_editing_order_id", "_editing_order_no",
                "_edit_existing_advance",
            ):
                st.session_state.pop(_clr_k, None)
            for _k in list(st.session_state.keys()):
                if _k.startswith("_confirmed_cart_") or _k.startswith("_receipt_data_"):
                    st.session_state.pop(_k, None)
            st.rerun()

    # ── CONSULTATION PREFILL BRIDGE ───────────────────────────────────────────
    # Must run BEFORE initialize_session_state() so the patient/fee data lands
    # first and init's setdefault logic never overwrites it.
    # Handles rerun races where _consult_prefill wasn't consumed by app.py yet.
    _cp_bridge = st.session_state.get("_consult_prefill")
    if _cp_bridge:
        _cpb_name   = _cp_bridge.get("patient_name", "")
        _cpb_mobile = _cp_bridge.get("patient_mobile", "") or _cp_bridge.get("mobile", "")
        _cpb_pid    = _cp_bridge.get("patient_id", "")
        _cpb_oid    = _cp_bridge.get("consult_order_id", "")
        if _cpb_name:
            st.session_state["retail_patient_name"]   = _cpb_name
            st.session_state["retail_patient_mobile"] = _cpb_mobile
        if _cpb_pid and len(str(_cpb_pid)) > 10:
            st.session_state["retail_patient_id"] = str(_cpb_pid)
        if _cpb_oid:
            st.session_state["retail_case_no"]            = _cpb_oid
            st.session_state["_retail_consult_source_id"] = _cpb_oid
        _cpb_lines = _cp_bridge.get("order_lines") or []
        if _cpb_lines and not st.session_state.get("retail_order_lines"):
            import datetime as _dt_cpb, uuid as _uuid_cpb
            # ── FIX: Double-charge guard (audit point #3) ─────────────────
            # _consult_fee_lines may already be set (from _set_consult_billing_state
            # called after consultation save). If so, strip SERVICE lines from
            # _cpb_lines before putting them in the cart — they will be injected
            # once at submit time from _consult_fee_lines instead.
            # This prevents the same ₹200 fee appearing twice.
            _existing_svc_pids = {
                str(l.get("product_id", ""))
                for l in st.session_state.get("_consult_fee_lines", [])
                if l.get("product_id")
            }
            if _existing_svc_pids:
                # Already have fee lines stored — filter them out of prefill cart
                _cpb_lines_safe = [
                    l for l in _cpb_lines
                    if not (
                        str(l.get("eye_side", "")).upper() in ("SERVICE", "S")
                        or bool(l.get("is_service_line"))
                        or str(l.get("product_id", "")) in _existing_svc_pids
                    )
                ]
            else:
                _cpb_lines_safe = _cpb_lines
            # ─────────────────────────────────────────────────────────────
            st.session_state["retail_order_lines"] = _cpb_lines_safe
            if not st.session_state.get("retail_provisional_order_id"):
                st.session_state["retail_provisional_order_id"] = (
                    f"PO-CONS-{_dt_cpb.datetime.now().strftime('%Y%m%d%H%M%S')}"
                    f"-{str(_uuid_cpb.uuid4())[:6].upper()}"
                )
        _cpb_fee_lines = [l for l in _cpb_lines
                          if str(l.get("eye_side","")).upper() in ("SERVICE","S")
                          or bool(l.get("is_service_line"))]
        if _cpb_fee_lines and not st.session_state.get("_consult_fee_lines"):
            st.session_state["_consult_fee_lines"] = _cpb_fee_lines

        # Apply RX from _consult_prefill into retail power fields
        _cpb_rx_r = _cp_bridge.get("rx_r") or {}
        _cpb_rx_l = _cp_bridge.get("rx_l") or {}
        if _cpb_rx_r:
            st.session_state["retail_old_rx_r"] = dict(_cpb_rx_r)
            st.session_state["retail_new_rx_r"] = dict(_cpb_rx_r)
        if _cpb_rx_l:
            st.session_state["retail_old_rx_l"] = dict(_cpb_rx_l)
            st.session_state["retail_new_rx_l"] = dict(_cpb_rx_l)

        st.session_state.pop("_consult_prefill", None)   # consume once

    # Apply _erp_rx_r / _erp_rx_l if set (from order_edit_view edit button)
    # Also clear retail_visit_mode HERE (unconditionally) so the radio always
    # respects _visit_mode_default even before retail_patient_id is set
    if st.session_state.get("_force_consultation_tab") or \
       st.session_state.get("_editing_consult_order_id"):
        st.session_state.pop("retail_visit_mode", None)
        st.session_state["_visit_mode_default"] = 1

    _erp_rx_r = st.session_state.pop("_erp_rx_r", None)
    _erp_rx_l = st.session_state.pop("_erp_rx_l", None)
    if _erp_rx_r:
        st.session_state["retail_old_rx_r"] = dict(_erp_rx_r)
        st.session_state["retail_new_rx_r"] = dict(_erp_rx_r)
    if _erp_rx_l:
        st.session_state["retail_old_rx_l"] = dict(_erp_rx_l)
        st.session_state["retail_new_rx_l"] = dict(_erp_rx_l)

    # Root 4: if no patient_id but _erp_patient_name set (e.g. old record without party_id)
    # still populate name + mobile so consultation screen renders
    if not st.session_state.get("retail_patient_id"):
        _erp_pname = st.session_state.get("_erp_patient_name","")
        _erp_pmob  = st.session_state.get("_erp_patient_mob","")
        if _erp_pname:
            st.session_state["retail_patient_name"]   = _erp_pname
            st.session_state["retail_patient_mobile"] = _erp_pmob
            # Use a placeholder ID so consultation screen renders
            # (will show patient name but no DB-linked visit)
            if not st.session_state.get("retail_patient_id"):
                import uuid as _uuid_r4
                st.session_state["retail_patient_id"] = f"TEMP-{str(_uuid_r4.uuid4())[:8].upper()}"
    # ─────────────────────────────────────────────────────────────────────────

    # ── Final mode guard: billing conversion always = Full Billing ──────────
    # If we have a consult source ID but are NOT in consultation edit mode,
    # we are converting consultation → billing. Force Full Billing radio.
    if (st.session_state.get("_retail_consult_source_id")
            and not st.session_state.get("_editing_consult_order_id")
            and not st.session_state.get("_force_consultation_tab")):
        st.session_state["_visit_mode_default"] = 0   # Full Billing
        st.session_state.pop("retail_visit_mode", None)  # clear stale widget key

    # ⚠️  BOOT SEQUENCE — ORDER IS CRITICAL, DO NOT REORDER
    # 1. Init must run first → guarantees all keys exist
    # 2. Integrity check runs on fully-initialised state
    # 3. Restores run last → they may overwrite keys, but keys always exist first
    initialize_session_state()   # ALWAYS FIRST
    assert_session_integrity()   # 🧠 ERP isolation layer

    # 🔄 Restore state after crash or browser reload
    restore_after_crash()
    restore_cart()

    # _consult_prefill and _order_edit_prefill are applied in app.py
    # after handle_page_switch — values already set before this function runs

    # ── Edit mode banner ─────────────────────────────────────────────────────
    _edit_oid = st.session_state.get("_editing_order_id","")
    _edit_ono = st.session_state.get("_editing_order_no","")
    if _edit_oid and _edit_ono:
        st.markdown(
            f"<div style='background:#fff7ed;border:2px solid #f97316;"
            f"border-radius:8px;padding:10px 16px;margin-bottom:10px;"
            f"display:flex;justify-content:space-between;align-items:center'>"
            f"<div><span style='font-size:0.75rem;color:#c2410c;font-weight:700;"
            f"text-transform:uppercase;letter-spacing:.06em'>✏️ EDITING ORDER</span>"
            f"<span style='font-family:monospace;font-size:1rem;font-weight:900;"
            f"color:#ea580c;margin-left:10px'> {_edit_ono}</span></div>"
            f"<span style='font-size:0.75rem;color:#9a3412'>Make changes below — "
            f"use Confirm to save. Changes logged with your user ID.</span></div>",
            unsafe_allow_html=True
        )

    # 🔒 HARD KEY GUARANTEE AFTER RESTORE (CRITICAL)
    # Restores may overwrite or skip keys → re-guarantee after every restore
    mandatory_keys = {
        "retail_selected_product": None,
        "retail_order_lines": [],
        "retail_current_allocation": None,
        "retail_show_batch_editor": False,
    }
    for k, v in mandatory_keys.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # 🔒 Streamlit first-run rerun stabilizer
    # Reset every render — must be False at render start so safe_rerun()
    # works correctly after confirm/reset. The old guard (only set if missing)
    # left it stuck True after the first rerun, silently swallowing all
    # subsequent safe_rerun() calls → cart never cleared after order save.
    st.session_state["_rerun_in_progress"] = False

    # ── Compact page badge (no wasted vertical space) ─────────────────────────
    st.markdown(
        "<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px'>"
        "<span style='background:#e94560;color:#fff;font-size:0.7rem;font-weight:800;"
        "padding:3px 10px;border-radius:20px;letter-spacing:.06em;text-transform:uppercase'>"
        "🛍️ Retail Punching</span>"
        "<span style='color:#94a3b8;font-size:0.72rem;letter-spacing:.04em'>Order Entry</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Confirmation Receipt — shown at TOP after order confirm ──────────────
    # If _receipt_snapshot was cleared (e.g. by a widget rerender or rerun),
    # restore it from the persistent order-keyed store so Print always works.
    if not st.session_state.get("_receipt_snapshot"):
        _lrk = st.session_state.get("_last_receipt_key", "")
        if _lrk and st.session_state.get(_lrk):
            st.session_state["_receipt_snapshot"] = st.session_state[_lrk]

    if st.session_state.get("_receipt_snapshot"):
        render_confirmation_receipt()
        st.markdown("---")

    # Post-save actions — independent of receipt, keyed to _post_save_data
    _psd = st.session_state.get("_post_save_data")
    if _psd:
        _render_post_save_actions(
            _psd.get("order_no",""),
            _psd.get("party_name",""),
            _psd.get("mobile",""),
            float(_psd.get("total",0)),
            _psd.get("order_type","RETAIL"),
            float(_psd.get("advance",0)),
            _psd.get("delivery",""),
            lines=_psd.get("lines", []),
        )
        if st.button("✕ Dismiss", key="retail_dismiss_post_save"):
            st.session_state.pop("_post_save_data", None)
            st.rerun()
        st.markdown("---")

    # ── CL Advisor hint — shown at page top whenever power was calculated ──
    # Displayed BEFORE patient selection so power is visible immediately
    # when navigating from CL Advisor page.
    _cl_top = st.session_state.get("_last_cl_result")
    if _cl_top and not st.session_state.get("_cl_hint_dismissed"):
        _prod_t = _cl_top.get("product", "")
        _r_t    = _cl_top.get("R", {})
        _l_t    = _cl_top.get("L", {})
        def _fmt_t(d):
            if not d.get("ok"): return "—"
            s = f"SPH {float(d['sph']):+.2f}"
            if d.get("cyl") and float(d.get("cyl",0)) != 0.0:
                s += f" / {float(d['cyl']):+.2f} / {d['axis']}°"
            return s
        _tb1, _tb2, _tb3, _tb4 = st.columns([3.5, 3.5, 1, 1])
        _tb1.markdown(
            f"<div style='background:#0f172a;border-left:3px solid #6366f1;"
            f"border-radius:4px;padding:5px 10px;font-size:0.8rem'>"
            f"<span style='color:#6366f1;font-weight:700'>👁️ CL</span> "
            f"<span style='color:#94a3b8'>{_prod_t}</span></div>",
            unsafe_allow_html=True
        )
        _tb2.markdown(
            f"<div style='background:#0f172a;border-left:3px solid #1e293b;"
            f"border-radius:4px;padding:5px 10px;font-size:0.8rem'>"
            f"<span style='color:#10b981'>R {_fmt_t(_r_t)}</span>"
            f"&nbsp;&nbsp;"
            f"<span style='color:#10b981'>L {_fmt_t(_l_t)}</span></div>",
            unsafe_allow_html=True
        )
        with _tb3:
            if st.button("↗ Apply", key="cl_hint_apply_top",
                         help="Fill power fields with CL result",
                         width='stretch'):
                if _r_t.get("ok"):
                    st.session_state["retail_new_rx_r"] = {
                        "sph":  float(_r_t["sph"]),
                        "cyl":  float(_r_t.get("cyl") or 0),
                        "axis": int(_r_t.get("axis") or 0),
                        "add":  0.0,
                    }
                    st.session_state["rx_reset_counter"] = (
                        st.session_state.get("rx_reset_counter", 0) + 1
                    )
                if _l_t.get("ok"):
                    st.session_state["retail_new_rx_l"] = {
                        "sph":  float(_l_t["sph"]),
                        "cyl":  float(_l_t.get("cyl") or 0),
                        "axis": int(_l_t.get("axis") or 0),
                        "add":  0.0,
                    }
                safe_rerun()
        with _tb4:
            if st.button("✕", key="cl_hint_dismiss_top",
                         help="Dismiss", width='stretch'):
                st.session_state["_cl_hint_dismissed"] = True
                safe_rerun()

    # 1️⃣ Patient
    render_patient_selection()

    # ── Mode toggle — use flag BEFORE widget renders ─────────────────────
    if st.session_state.get("retail_patient_id"):

        # Mode switch flags — must be handled BEFORE radio renders
        if st.session_state.pop("_upgrade_to_billing", False):
            st.session_state["_visit_mode_default"] = 0   # Full Billing
        if st.session_state.pop("_downgrade_to_consult", False):
            st.session_state["_visit_mode_default"] = 1   # Consultation Only

        # Clear widget key so _visit_mode_default controls the radio (not stale widget state)
        if st.session_state.get("_force_consultation_tab"):
            st.session_state.pop("retail_visit_mode", None)
            st.session_state.pop("_force_consultation_tab", None)
        _default_idx = st.session_state.get("_visit_mode_default", 1)  # default Consultation

        _mc1, _mc2 = st.columns([3, 2])
        with _mc1:
            _visit_mode = st.radio(
                "Visit type",
                ["🛍️ Full Billing", "🩺 Consultation Only"],
                index=_default_idx,
                horizontal=True,
                key="retail_visit_mode",
            )
            # Persist choice for next rerun
            st.session_state["_visit_mode_default"] = (
                0 if _visit_mode == "🛍️ Full Billing" else 1
            )

        with _mc2:
            if _visit_mode == "🩺 Consultation Only":
                st.caption("")
                if st.button(
                    "➕ Add spectacles to this visit",
                    key="consult_upgrade_billing",
                    width='stretch',
                    help="Switches to full billing — all clinical data preserved",
                ):
                    st.session_state["_upgrade_to_billing"] = True
                    st.session_state.pop("retail_visit_mode", None)
                    st.rerun()
            else:
                # Downgrade to consultation — only if cart is empty
                _cart = st.session_state.get("retail_order_lines", [])
                if not _cart:
                    st.caption("")
                    if st.button(
                        "🩺 Move to consultation only",
                        key="billing_downgrade_consult",
                        width='stretch',
                        help="No products — switch to consultation. Cart is empty.",
                    ):
                        st.session_state["_downgrade_to_consult"] = True
                        st.session_state.pop("retail_visit_mode", None)
                        st.rerun()
                else:
                    st.caption(f"🛒 {len(_cart)} item(s) in cart")

        st.markdown("---")
    else:
        _visit_mode = "🛍️ Full Billing"
        st.session_state["_visit_mode_default"] = 1

    # 🩺 Clinical Examination (expander — always shown)
    render_clinical_examination()

    # 2️⃣ Power Entry (always shown)
    render_power_entry()

    # Auto-confirm powers in consultation mode — no Save Powers gate
    if _visit_mode == "🩺 Consultation Only" or st.session_state.get("_editing_consult_order_id"):
        st.session_state["_rx_powers_confirmed"] = True

    # ════════════════════════════════════════════════════════════════════════
    # PART A — CONSULTATION ONLY
    # ════════════════════════════════════════════════════════════════════════
    if st.session_state.get("retail_patient_id") and _visit_mode == "🩺 Consultation Only":
        try:
            from modules.consultation import render_consultation_close
            render_consultation_close()
        except Exception as _ce:
            import traceback
            st.error(f"Consultation module error: {_ce}")
            st.code(traceback.format_exc())

    # ════════════════════════════════════════════════════════════════════════
    # PART B — FULL BILLING
    # ════════════════════════════════════════════════════════════════════════
    else:
        # 3️⃣ Product
        render_product_selection()

        # 4️⃣ Lens + Boxing (Before Allocation)
        render_lens_params()
        render_boxing_params()

        # 5️⃣ Allocation (MUST be here)
        render_batch_allocation_editor()

        # 6️⃣ Cart
        render_order_lines()

    # 6b️⃣ Add extra line to cart before submitting
    try:
        from modules.backoffice.order_line_adder import render_cart_line_adder
        render_cart_line_adder(order_type="RETAIL")
    except Exception as _ale:
        st.caption(f"Add line: {_ale}")


    # 6c️⃣ Service Charges
    # Rendered ALWAYS once cart has lines, AFTER order lines table.
    # Position is stable: after cart, before Finalize button.
    # Consultation orders -> DB-backed (real UUID known)
    # Fresh retail orders -> session-staged, flushed to DB on Confirm
    _sc_cart      = st.session_state.get("retail_order_lines") or []
    _sc_consult_id = st.session_state.get("_retail_consult_source_id", "")
    if _sc_cart:
        st.markdown("🧾 **Service Charges** — Fitting · Colouring · Courier")
        if _sc_consult_id:
            st.caption("ℹ️ Saved to consultation order — appears in Backoffice Billing Summary.")
            try:
                from modules.backoffice.order_charges_panel import render_order_charges_panel
                from modules.sql_adapter import run_query as _rq_scid
                _sc_svc = [
                    l for l in _sc_cart
                    if str(l.get("eye_side","")).upper() in ("SERVICE","S")
                    or bool(l.get("is_service_line"))
                ]
                # ── Resolve to UUID — _sc_consult_id may be order_no like CONS-* ──
                _sc_uuid = _sc_consult_id
                _is_sc_uuid = (
                    len(str(_sc_uuid)) == 36
                    and str(_sc_uuid).count("-") == 4
                    and not str(_sc_uuid).upper().startswith(("CONS-", "R/", "W/"))
                )
                if not _is_sc_uuid:
                    try:
                        _sc_id_rows = _rq_scid(
                            "SELECT id::text FROM orders WHERE order_no = %s LIMIT 1",
                            (_sc_consult_id,)
                        ) or []
                        if _sc_id_rows:
                            _sc_uuid = _sc_id_rows[0]["id"]
                            # Cache the resolved UUID back so subsequent reruns are instant
                            st.session_state["_retail_consult_source_id"] = _sc_uuid
                    except Exception:
                        pass
                render_order_charges_panel(
                    {"id": _sc_uuid, "service_lines": _sc_svc}, [])
            except Exception as _scp_e:
                st.caption(f"Service charges error: {_scp_e}")
        else:
            st.caption("ℹ️ Charges saved with order on Confirm.")
            from modules.core.business_rules import SERVICE_CHARGE_TYPES as _SCT
            _pending = st.session_state.get("_retail_pending_charges", [])

            # Display existing pending charges
            if _pending:
                for _pi, _pc in enumerate(_pending):
                    _cfg2 = _SCT.get(_pc["type"], _SCT["MISC"])
                    _c1, _c2, _c3, _c4 = st.columns([0.4, 3.5, 1.5, 0.6])
                    _c1.markdown(
                        f"<div style='font-size:1.2rem;text-align:center'>"
                        f"{_cfg2['icon']}</div>",
                        unsafe_allow_html=True)
                    _c2.markdown(
                        f"<span style='color:#e2e8f0;font-size:0.82rem;"
                        f"font-weight:600'>{_pc['desc']}</span>"
                        f"<br><span style='color:#64748b;font-size:0.7rem'>"
                        f"{_cfg2['label']}</span>",
                        unsafe_allow_html=True)
                    _c3.markdown(
                        f"<span style='color:#10b981;font-weight:700'>"
                        f"₹{float(_pc['amt']):,.0f}</span>",
                        unsafe_allow_html=True)
                    if _c4.button("🗑",
                                  key=f"sc_del_{_pi}",
                                  help="Remove charge"):
                        _pending.pop(_pi)
                        st.session_state["_retail_pending_charges"] = _pending
                        st.rerun()
                st.markdown(
                    "<hr style='margin:4px 0;border-color:#1e293b'>",
                    unsafe_allow_html=True)

            # Add-new charge type buttons
            _sc_add_key    = "_sc_add_type"
            _existing_types = {c["type"] for c in _pending}
            _addable       = [ct for ct in ("FITTING", "COLOURING", "COURIER", "MISC")
                              if ct not in _existing_types]
            if not st.session_state.get(_sc_add_key):
                if _addable:
                    _cols_sc = st.columns(len(_addable))
                    for _ai, _at in enumerate(_addable):
                        _acfg = _SCT.get(_at, _SCT["MISC"])
                        with _cols_sc[_ai]:
                            if st.button(
                                f"{_acfg['icon']} + {_acfg['label']}",
                                key=f"sc_add_{_at}",
                                width='stretch'):
                                st.session_state[_sc_add_key] = _at
                                st.rerun()
                else:
                    st.caption("🟢 All charge types added.")
            else:
                _sc_ct  = st.session_state[_sc_add_key]
                _sc_cfg = _SCT.get(_sc_ct, _SCT["MISC"])
                st.markdown(
                    f"<div style='background:#0f172a;"
                    f"border:1px solid {_sc_cfg['color']}44;"
                    f"border-radius:8px;padding:10px;margin:4px 0'>"
                    f"<span style='color:{_sc_cfg['color']};font-weight:700'>"
                    f"{_sc_cfg['icon']} Add {_sc_cfg['label']}</span></div>",
                    unsafe_allow_html=True)
                _sc_c1, _sc_c2, _sc_c3 = st.columns([3, 1.5, 1.5])
                with _sc_c1:
                    _sc_desc = st.text_input(
                        "Description",
                        placeholder=f"{_sc_cfg['label']} charge",
                        key="sc_desc")
                with _sc_c2:
                    _sc_amt = st.number_input(
                        "₹ Amount", min_value=0.0, step=10.0, key="sc_amt")
                with _sc_c3:
                    _sc_gst = st.number_input(
                        "GST %", min_value=0.0, max_value=28.0,
                        value=float(_sc_cfg["default_gst"]),
                        step=0.5, key="sc_gst")
                _sc_s1, _sc_s2 = st.columns(2)
                with _sc_s1:
                    enter_to_submit()
                    if st.button("✅ Add Charge  [Enter]", type="primary",
                                 width='stretch', key="sc_confirm"):
                        if _sc_amt <= 0:
                            st.error("Enter amount > 0")
                        else:
                            _pending.append({
                                "type": _sc_ct,
                                "desc": _sc_desc or _sc_cfg["label"],
                                "amt":  _sc_amt,
                                "gst":  _sc_gst,
                            })
                            st.session_state["_retail_pending_charges"] = _pending
                            st.session_state.pop(_sc_add_key, None)
                            st.rerun()
                with _sc_s2:
                    if st.button("✕ Cancel",
                                 width='stretch', key="sc_cancel"):
                        st.session_state.pop(_sc_add_key, None)
                        st.rerun()

            # Running total
            if _pending:
                _sc_total = sum(
                    float(c["amt"]) * (1 + float(c["gst"]) / 100)
                    for c in _pending)
                st.markdown(
                    f"<div style='background:#0d1f0d;"
                    f"border:1px solid #10b98144;border-radius:8px;"
                    f"padding:8px 14px;margin-top:6px;"
                    f"display:flex;justify-content:space-between'>"
                    f"<span style='color:#94a3b8;font-size:0.78rem'>"
                    f"Service Charges (incl. GST)</span>"
                    f"<span style='color:#10b981;font-weight:800'>"
                    f"₹{_sc_total:,.2f}</span></div>",
                    unsafe_allow_html=True)

    # 7️⃣ Finalize
    finalize_retail_order_to_backoffice()

    # ── Post-confirm order editor ─────────────────────────────────────────
    _lco = st.session_state.get("last_confirmed_order")
    if _lco and _lco.get("id"):
        st.markdown("---")
        st.markdown(
            "<div style='color:#60a5fa;font-size:0.72rem;font-weight:700;"
            "letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px'>"
            f"📋 Last Order: {_lco.get('order_no','')}</div>",
            unsafe_allow_html=True,
        )
        try:
            from modules.sql_adapter import run_query as _rq_lco
            from modules.backoffice.order_line_adder import (
                render_mirror_panel, render_add_line_panel,
                render_line_delete_panel)
            # Fetch live lines for this order
            _lco_lines = _rq_lco("""
                SELECT ol.id AS line_id, ol.order_id, ol.product_id,
                       p.product_name, p.brand, ol.eye_side,
                       ol.sph, ol.cyl, ol.axis, ol.add_power,
                       ol.quantity, ol.unit_price, ol.total_price,
                       ol.lens_params, ol.billed_qty, ol.allocated_qty
                FROM order_lines ol
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
            """, {"oid": _lco["id"]}) or []
            # Parse lens_params
            import json as _jlco
            for _ll in _lco_lines:
                _raw = _ll.get("lens_params")
                if isinstance(_raw, str):
                    try: _ll["lens_params"] = _jlco.loads(_raw)
                    except: _ll["lens_params"] = {}
                elif not isinstance(_raw, dict):
                    _ll["lens_params"] = {}
            _lco_mock = {
                "id": _lco["id"],
                "order_no": _lco.get("order_no", ""),
                "status": "PENDING",
                "lines": _lco_lines,
                "stock_lines": _lco_lines,
                "inhouse_lines": [],
                "lab_order_lines": [],
            }
            render_line_delete_panel(_lco_mock)
            render_mirror_panel(_lco_mock)
            render_add_line_panel(_lco_mock)
        except Exception as _lco_err:
            st.caption(f"Order editor: {_lco_err}")

        # ── FIX: Payment Status — Mark as Received (audit point #2) ──────
        # Tracks whether cash/payment was actually received, separate from
        # order status. Prevents financial mismatch where order is CONFIRMED
        # but money hasn't actually been collected yet.
        _pay_status_key = f"_pay_received_{_lco.get('order_no','')}"
        _pay_received   = st.session_state.get(_pay_status_key, False)
        _lco_total      = float(_lco.get("total", 0))

        st.markdown("---")
        if _pay_received:
            st.success("✅ **Payment Received** — confirmed and recorded")
        else:
            _pr_c1, _pr_c2, _pr_c3 = st.columns([2, 1, 1])
            with _pr_c1:
                st.warning(
                    f"⏳ **Payment Pending** — ₹{_lco_total:,.2f} not yet marked as received",
                    icon="💰"
                )
            with _pr_c2:
                if st.button(
                    "✅ Mark Payment Received",
                    key=f"mark_pay_rcv_{_lco.get('order_no','')}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        from modules.sql_adapter import run_write as _rw_pay_rcv
                        _rw_pay_rcv(
                            """
                            UPDATE orders
                               SET payment_status = 'RECEIVED',
                                   updated_at     = NOW()
                             WHERE id = %s::uuid
                            """,
                            (_lco["id"],)
                        )
                        st.session_state[_pay_status_key] = True
                        # Update in-memory LCO
                        _lco["payment_status"] = "RECEIVED"
                        st.session_state["last_confirmed_order"] = _lco
                        st.success("✅ Payment marked as received in DB")
                        st.rerun()
                    except Exception as _pre:
                        # Fallback: mark in session only if DB write fails
                        st.session_state[_pay_status_key] = True
                        st.warning(f"DB update failed ({_pre}) — marked in session only")
                        st.rerun()
            with _pr_c3:
                st.caption("Mark when cash / UPI / bank transfer confirmed received")
        # ─────────────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────────────────

    # 💾 Auto-save state snapshots at end of every render
    save_runtime_snapshot()
    persist_cart()
    record_step("render_complete")