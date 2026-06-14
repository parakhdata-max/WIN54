"""
modules/retail_punching_rx.py
==============================
Patient search, Rx power entry, product selection,
stock allocation for retail punching.
"""

"""
Retail Order Punching Module - FIXED VERSION
FEATURES:
- Case ID search with multiple patient handling
- Phone number search with multiple patient handling
- Power entry with "use same as old Rx" option
- Complete product selection flow (from old working version)
- Proper lens params and boxing params (from old working version)
- Single provisional order flow
"""

import streamlit as st

try:
    from modules.ophthalmic_billing import (
        render_ophthalmic_selector as _oph_selector,
        ophthalmic_unit_price as _oph_price,
        ophthalmic_display_name as _oph_name,
    )
    _HAS_OPH_BILLING = True
except Exception:
    _oph_selector = None
    _oph_price = None
    _oph_name = None
    _HAS_OPH_BILLING = False

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
    from modules.contact_lens_resolver import (
        line_for_eye as _cl_line_for_eye,
        resolve_for_selected_product as _cl_resolve_for_selected_product,
        should_show_resolution_notice as _cl_should_show_resolution_notice,
    )
except Exception:
    _cl_line_for_eye = None
    _cl_resolve_for_selected_product = None
    _cl_should_show_resolution_notice = None
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
    from modules.core.name_formatter import format_person_name
except Exception:
    def format_person_name(name):
        return " ".join(str(name or "").strip().split())
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

from modules import retail_punching_data as _retail_data_mod
globals().update({k: v for k, v in vars(_retail_data_mod).items() if not k.startswith("__")})

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


def _rx_input_text(val, is_axis: bool = False) -> str:
    """Initial text for editable RX boxes; blanks stay blank for backspace editing."""
    cleaned = _clean_rx_val(val, is_axis=is_axis)
    if cleaned is None:
        return ""
    try:
        return str(int(cleaned)) if is_axis else f"{float(cleaned):.2f}"
    except Exception:
        return str(val or "")


def _parse_rx_text(raw, *, is_axis: bool = False, min_value=None, max_value=None):
    """
    LEGACY silent parser — kept for backward-compatible callers.
    New code uses _validate_power_field() for strict validation with errors.
    Supports compact notation: 125 → +1.25, -125 → -1.25.
    """
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        _neg = txt.startswith("-")
        _abs = txt.lstrip("+-").replace(" ","")
        if "." not in _abs and _abs.isdigit() and len(_abs) >= 3 and not is_axis:
            _abs = _abs[:-2] + "." + _abs[-2:]
        val_str = ("-" if _neg else "") + _abs
        val = int(float(val_str)) if is_axis else round(float(val_str) * 4) / 4
        if min_value is not None:
            val = max(min_value, val)
        if max_value is not None:
            val = min(max_value, val)
        return val
    except Exception:
        return None


# ── Strict power field rules ──────────────────────────────────────────────────
_RX_RULES = {
    "sph":  {"min": -21.0,  "max": +21.0,  "step": 0.25, "label": "SPH"},
    "cyl":  {"min":  -8.0,  "max":  +8.0,  "step": 0.25, "label": "CYL"},
    "axis": {"min":    0,   "max":   180,  "step":    1, "label": "AXIS", "is_axis": True},
    "add":  {"min": +0.50,  "max": +4.00,  "step": 0.25, "label": "ADD"},
}


def _validate_power_field(raw: str, field: str):
    """
    Strict parser — returns (value, error_string).
    Supports compact notation: 125 → +1.25, -125 → -1.25.
    Empty input returns (None, None) — not all fields required.
    """
    rules  = _RX_RULES.get(field, {})
    _min   = rules.get("min")
    _max   = rules.get("max")
    _lbl   = rules.get("label", field.upper())
    _is_ax = rules.get("is_axis", False)

    txt = str(raw or "").strip()
    if not txt:
        return None, None

    _neg = txt.startswith("-")
    _abs = txt.lstrip("+-").replace(" ", "")

    # Compact: 125 → 1.25, 200 → 2.00 (only for non-axis)
    if "." not in _abs and _abs.isdigit() and len(_abs) >= 3 and not _is_ax:
        _abs = _abs[:-2] + "." + _abs[-2:]

    try:
        val = float(("-" if _neg else "") + _abs)
    except ValueError:
        return None, f"{_lbl}: '{txt}' is not a valid number"

    if _is_ax:
        val = int(round(val))
        if _min is not None and val < _min:
            return None, f"{_lbl}: {val}° is below minimum ({int(_min)}°)"
        if _max is not None and val > _max:
            return None, f"{_lbl}: {val}° exceeds maximum ({int(_max)}°)"
        return val, None

    # 0.25 step check
    _r4 = round(val * 4) / 4
    if abs(_r4 - val) > 0.001:
        return None, f"{_lbl}: {val:+.3f} is not a 0.25-step value (nearest: {_r4:+.2f})"
    val = _r4
    if _min is not None and val < _min:
        return None, f"{_lbl}: {val:+.2f} is below minimum ({_min:+.2f})"
    if _max is not None and val > _max:
        return None, f"{_lbl}: {val:+.2f} exceeds maximum ({_max:+.2f})"
    return val, None


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


def _fmt_mrp(value) -> str:
    """Display customer-facing MRP rounded to whole rupees; GST math stays untouched."""
    try:
        return f"₹{float(value or 0):,.0f}"
    except Exception:
        return "₹0"


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


def clear_retail_cart_completely(set_consult_removed: bool = True):
    """
    Hard wipe of ALL cart-related state.
    Safe to call after delete-last-line, Clear All, New Order, Duplicate.
    Clears persistent cart, crash snapshot, payment locks, confirmed fingerprints.
    """
    # Cart + order identity
    st.session_state["retail_order_lines"]                   = []
    st.session_state["retail_provisional_order_id"]          = None
    st.session_state["retail_provisional_order_created_at"]  = None
    st.session_state["retail_pending_eyes"]                   = []
    st.session_state.pop("_retail_finalized_eyes", None)
    # Persistent / crash recovery
    st.session_state.pop("_persistent_cart", None)
    st.session_state.pop("_crash_snapshot", None)
    # Payment locks
    st.session_state.pop("_frozen_payment_total", None)
    st.session_state.pop("_retail_payment_locked", None)
    # Service charge form/state must never bleed into the next order.
    st.session_state.pop("_retail_pending_charges", None)
    st.session_state.pop("_sc_add_type", None)
    st.session_state.pop("_sc_add_order_token", None)
    for _k in list(st.session_state.keys()):
        if _k.startswith("sc_"):
            st.session_state.pop(_k, None)
    # Confirmed cart fingerprints — cleared so button re-enables for new order
    for _k in list(st.session_state.keys()):
        if _k.startswith("_confirmed_cart_") or _k.startswith("_receipt_data_"):
            st.session_state.pop(_k, None)
    # Params
    st.session_state["retail_lens_params"] = {
        "frame_type": "", "thickness": "", "corridor": "",
        "diameter": "", "fitting_height": "", "instructions": "",
    }
    st.session_state["retail_boxing_params"] = {
        "a_box": None, "b_box": None, "ed": None, "ed_axis": None,
        "dbl": None, "r_pd": None, "l_pd": None, "ipd": None,
        "fitting_ht_r": None, "fitting_ht_l": None,
        "panto": None, "tilt": None, "bvd": None,
    }
    # Service / consultation fee tracking
    if set_consult_removed:
        st.session_state["_consult_fee_removed"] = True
    st.session_state.pop("_consult_fee_lines", None)
    st.session_state.pop("_retail_consult_source_id", None)
    # Allocation
    st.session_state.pop("retail_current_allocation", None)
    st.session_state.pop("retail_show_batch_editor", None)
    st.session_state.pop("_alloc_lock", None)


# Backward-compatible alias
def clear_provisional_order():
    """Alias — kept so existing callers still work."""
    clear_retail_cart_completely(set_consult_removed=False)

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
    st.session_state.retail_patient_name = format_person_name(patient.get('patient_name') or patient.get('master_name', ''))
    _raw_mob = patient.get('mobile_number') or patient.get('mobile', '')
    import math as _math
    st.session_state.retail_patient_mobile = (
        "" if (
            not _raw_mob
            or (isinstance(_raw_mob, float) and _math.isnan(_raw_mob))
            or str(_raw_mob).strip() in ("nan", "None", "-", "")
        ) else str(_raw_mob).strip()
    )

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
                        st.session_state.retail_patient_name = format_person_name(row['patient_name'])
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

    # ── CONSULTATION EDIT MODE: patient context is locked and may be TEMP ────
    _ps_consult_oid = st.session_state.get("_editing_consult_order_id", "")
    if _ps_consult_oid:
        _pn = st.session_state.get("retail_patient_name", "—")
        _pm = st.session_state.get("retail_patient_mobile", "")
        if str(st.session_state.get("retail_patient_id") or "").upper().startswith("TEMP-"):
            _pm = ""
            st.session_state["retail_patient_mobile"] = ""
        st.markdown(
            f"<div style='background:#082f2a;border:1px solid #14b8a6;border-radius:8px;"
            f"padding:10px 14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center'>"
            f"<div><span style='font-size:1rem;font-weight:700;color:#5eead4'>🩺 {_pn}</span>"
            f"<span style='color:#94a3b8;font-size:0.82rem;margin-left:10px'>📞 {_pm or '—'}</span></div>"
            f"<span style='background:#134e4a;color:#5eead4;padding:2px 10px;border-radius:12px;"
            f"font-size:0.7rem;font-weight:700'>CONSULTATION EDIT</span></div>",
            unsafe_allow_html=True,
        )
        return

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
            st.session_state.pop("_ps_last_selected_pid", None)
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

    if "patient_search_mode" not in st.session_state:
        st.session_state["patient_search_mode"] = "Name / Mobile"

    search_mode = st.radio(
        "Search by:",
        options=["Name / Mobile", "Case ID", "New Walk-in"],
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
        if (
            _cur_pid.upper().startswith("TEMP-")
            and not st.session_state.get("_editing_consult_order_id")
        ):
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

    # Build display label: "Name · Mobile · Case ID"
    def _make_label(row):
        nm  = str(row.get("patient_name") or "—")
        mob = str(row.get("mobile_number") or "—")
        cid = str(row.get("record_no")     or "—")
        return f"{nm}  ·  📱{mob}  ·  🪪{cid}"

    df = patients_df.copy()
    df["_label"] = df.apply(_make_label, axis=1)
    df = df.drop_duplicates("patient_id")
    _labels = df["_label"].tolist()

    _chosen_label = st.selectbox(
        "Search patient by name, mobile, or case",
        options=_labels,
        index=None,
        placeholder="Start typing name / mobile / case...",
        key="ps_smart_dropdown",
        label_visibility="collapsed",
    )

    if not _chosen_label:
        st.caption("Start typing, then select a patient from the list.")
        return

    # Match chosen label back to row
    _row = df[df["_label"] == _chosen_label]
    if _row.empty:
        return
    _patient = _row.iloc[0]
    _chosen_pid = str(_patient.get("patient_id") or _patient.get("id") or "").strip()
    if not _chosen_pid:
        st.error("Selected patient has no patient ID. Refresh patient database and try again.")
        return
    if (
        st.session_state.get("_ps_last_selected_pid") == _chosen_pid
        and str(st.session_state.get("retail_patient_id") or "") == _chosen_pid
    ):
        return

    # Selecting the search result is the action. No extra "Use" click.
    st.session_state["_ps_last_selected_pid"] = _chosen_pid
    set_patient_from_record(_patient.to_dict())
    st.session_state.retail_selected_case_record_no = None
    st.session_state.retail_case_visits = None
    st.session_state.retail_selected_visit_id = None
    return

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
                    st.session_state["retail_patient_name"]   = format_person_name(_ex_name)
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
                _name = format_person_name(_name)
                try:
                    _rw_np("UPDATE patients SET master_name=%s, updated_at=NOW() WHERE id=%s::uuid",
                           (_name, _new_pid))
                except Exception:
                    _rw_np("UPDATE patients SET master_name=%s WHERE id=%s::uuid",
                           (_name, _new_pid))
            else:
                _name = format_person_name(_name)
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
        tab_v, tab_c, tab_o, tab_p = st.tabs([
            "📋 Visit History",
            "🩺 Clinical History",
            "📦 Order History",
            "👤 Patient Details",
        ])

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
                    _vname = format_person_name(v.get('visit_name') or '')
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

                        # ── Action buttons ─────────────────────────────────────
                        _vbtn1, _vbtn2 = st.columns(2)
                        with _vbtn1:
                            if st.button(f"✅ Use this visit's power",
                                          key=f"use_v_{v.get('visit_id',i)}",
                                          use_container_width=True):
                                from modules.retail_punching import apply_visit_power_to_rx
                                apply_visit_power_to_rx(v)
                                st.success("✅ Power loaded")
                                st.rerun()
                        with _vbtn2:
                            _edit_key = f"rp_edit_visit_{v.get('visit_id',i)}"
                            if st.button("✏️ Edit / correct this record",
                                         key=f"rp_edit_visit_btn_{v.get('visit_id',i)}",
                                         use_container_width=True,
                                         help="Correct power, case ref, or notes for this visit"):
                                # Toggle edit panel
                                st.session_state[_edit_key] = not st.session_state.get(_edit_key, False)
                                st.rerun()

                        # ── Inline visit edit panel ────────────────────────────
                        if st.session_state.get(_edit_key):
                            _vid_e = v.get("visit_id","")
                            st.markdown(
                                "<div style='background:#0d1a2e;border:1px solid #3b82f6;"
                                "border-radius:6px;padding:10px 14px;margin-top:4px'>"
                                "<span style='font-size:0.75rem;font-weight:700;color:#60a5fa'>"
                                "✏️ Editing visit record</span></div>",
                                unsafe_allow_html=True
                            )

                            _ee1, _ee2 = st.columns(2)
                            with _ee1:
                                st.markdown("**Right eye**")
                                _e_rs  = st.number_input("SPH", value=float(v.get("right_sph") or 0),
                                    step=0.25, format="%.2f", key=f"e_rs_{_vid_e}")
                                _e_rc  = st.number_input("CYL", value=float(v.get("right_cyl") or 0),
                                    step=0.25, format="%.2f", key=f"e_rc_{_vid_e}")
                                _e_ra  = st.number_input("AXIS", value=int(float(v.get("right_axis") or 0)),
                                    step=5, min_value=0, max_value=180, key=f"e_ra_{_vid_e}")
                                _e_rad = st.number_input("ADD", value=float(v.get("right_add_power") or 0),
                                    step=0.25, format="%.2f", key=f"e_rad_{_vid_e}")
                            with _ee2:
                                st.markdown("**Left eye**")
                                _e_ls  = st.number_input("SPH", value=float(v.get("left_sph") or 0),
                                    step=0.25, format="%.2f", key=f"e_ls_{_vid_e}")
                                _e_lc  = st.number_input("CYL", value=float(v.get("left_cyl") or 0),
                                    step=0.25, format="%.2f", key=f"e_lc_{_vid_e}")
                                _e_la  = st.number_input("AXIS", value=int(float(v.get("left_axis") or 0)),
                                    step=5, min_value=0, max_value=180, key=f"e_la_{_vid_e}")
                                _e_lad = st.number_input("ADD", value=float(v.get("left_add_power") or 0),
                                    step=0.25, format="%.2f", key=f"e_lad_{_vid_e}")

                            _e_case = st.text_input(
                                "Case paper / reference no",
                                value=v.get("case_no","") or "",
                                key=f"e_case_{_vid_e}",
                                placeholder="e.g. 19483"
                            )
                            _e_notes = st.text_area(
                                "Notes / findings",
                                value="",
                                key=f"e_notes_{_vid_e}",
                                placeholder="Diagnosis, advice, clinical findings...",
                                height=60,
                            )

                            _esave, _ecancel = st.columns(2)
                            with _esave:
                                if st.button("💾 Save corrections",
                                             key=f"e_save_{_vid_e}",
                                             type="primary",
                                             use_container_width=True):
                                    try:
                                        from modules.sql_adapter import run_write as _rw_ve
                                        _rw_ve("""
                                            UPDATE patient_visits SET
                                                right_sph  = %s, right_cyl  = %s,
                                                right_axis = %s, right_add  = %s,
                                                left_sph   = %s, left_cyl   = %s,
                                                left_axis  = %s, left_add   = %s,
                                                record_no  = COALESCE(NULLIF(%s,''), record_no)
                                            WHERE id = %s::uuid
                                        """, (
                                            _e_rs, _e_rc, _e_ra, _e_rad,
                                            _e_ls, _e_lc, _e_la, _e_lad,
                                            _e_case.strip(),
                                            _vid_e,
                                        ))
                                        # Save notes to patient_clinicals if provided
                                        if _e_notes.strip() and _vid_e:
                                            try:
                                                import uuid as _uuid_nc
                                                from modules.sql_adapter import run_write as _rw_nc
                                                _rw_nc("""
                                                    INSERT INTO patient_clinicals
                                                        (id, patient_id, visit_id, doctor_notes, created_at)
                                                    VALUES (%s::uuid, %s::uuid, %s::uuid, %s, NOW())
                                                    ON CONFLICT DO NOTHING
                                                """, (str(_uuid_nc.uuid4()), str(pid), _vid_e, _e_notes.strip()))
                                            except Exception:
                                                pass
                                        st.success("✅ Visit record corrected")
                                        st.session_state.pop(_edit_key, None)
                                        st.rerun()
                                    except Exception as _ve_ex:
                                        st.error(f"Save failed: {_ve_ex}")
                            with _ecancel:
                                if st.button("✕ Cancel",
                                             key=f"e_cancel_{_vid_e}",
                                             use_container_width=True):
                                    st.session_state.pop(_edit_key, None)
                                    st.rerun()

                # ── Link old records / same-patient search ─────────────────────
                st.markdown("---")
                with st.expander("🔗 Find & link old records (same patient, different name/spelling)", expanded=False):
                    st.markdown(
                        "<div style='font-size:0.78rem;color:#94a3b8;margin-bottom:8px'>"
                        "Search for old records under a different spelling — e.g. "
                        "<b>Aarya Tiwari</b> for <b>Arya Tiwari</b>. "
                        "Select the matching record and merge it so both histories appear together."
                        "</div>",
                        unsafe_allow_html=True
                    )

                    # ── Part A: Show likely duplicates automatically ────────────
                    _auto_dups = []
                    try:
                        from modules.patient_merge import find_likely_duplicates, ensure_patient_merge_schema
                        ensure_patient_merge_schema()
                        _auto_dups = find_likely_duplicates(str(pid), limit=5)
                    except Exception:
                        pass

                    if _auto_dups:
                        st.markdown(
                            f"<div style='font-size:0.75rem;color:#f59e0b;margin-bottom:6px'>"
                            f"⚠️ {len(_auto_dups)} likely duplicate(s) found automatically:</div>",
                            unsafe_allow_html=True
                        )
                        for _dup in _auto_dups:
                            _dup_id   = str(_dup.get("id",""))
                            _dup_name = _dup.get("name","—")
                            _dup_mob  = _dup.get("mobile","") or "no mobile"
                            _dup_vc   = int(_dup.get("visit_count",0) or 0)
                            _dup_rec  = _dup.get("record_no","") or ""
                            st.markdown(
                                f"<div style='background:#1a1a0a;border:1px solid #854d0e;"
                                f"border-radius:5px;padding:5px 10px;margin-bottom:4px;"
                                f"font-size:0.78rem;color:#fde68a'>"
                                f"<b>{_dup_name}</b>"
                                + (f" · #{_dup_rec}" if _dup_rec else "")
                                + f" · {_dup_mob} · {_dup_vc} visit(s)</div>",
                                unsafe_allow_html=True
                            )
                            _merge_auto_key = f"rp_merge_auto_{_dup_id[:8]}"
                            _confirm_auto   = f"rp_merge_confirm_{_dup_id[:8]}"
                            if not st.session_state.get(_confirm_auto):
                                if st.button(
                                    f"🔗 Merge '{_dup_name}' → keep current patient as primary",
                                    key=_merge_auto_key,
                                    use_container_width=True,
                                ):
                                    st.session_state[_confirm_auto] = True
                                    st.rerun()
                            else:
                                st.warning(
                                    f"⚠️ **{_dup_name}**'s {_dup_vc} visit(s) will move here. "
                                    f"'{_dup_name}' will be soft-deleted and saved as alias."
                                )
                                _cc1, _cc2 = st.columns(2)
                                with _cc1:
                                    if st.button("✅ Yes, merge",
                                                 key=f"rp_merge_yes_{_dup_id[:8]}",
                                                 type="primary",
                                                 use_container_width=True):
                                        try:
                                            from modules.patient_merge import merge_patients
                                            _r = merge_patients(str(pid), _dup_id,
                                                                notes="Merged from retail history panel")
                                            if _r.get("ok"):
                                                st.success(
                                                    f"✅ Merged '{_dup_name}' into current patient. "
                                                    f"{_r['visits']} visit(s) now visible in history. "
                                                    f"Old name saved as alias — searchable."
                                                )
                                                st.session_state.pop(_confirm_auto, None)
                                                st.rerun()
                                            else:
                                                st.error(_r.get("error","Merge failed"))
                                        except Exception as _me:
                                            st.error(f"Merge error: {_me}")
                                with _cc2:
                                    if st.button("❌ Cancel",
                                                 key=f"rp_merge_no_{_dup_id[:8]}",
                                                 use_container_width=True):
                                        st.session_state.pop(_confirm_auto, None)
                                        st.rerun()

                    # ── Part B: Manual search for old name ─────────────────────
                    st.markdown("**Or search manually:**")
                    _link_q = st.text_input(
                        "Search by old name / mobile / case no",
                        key="rp_link_search_q",
                        placeholder="e.g. Aarya Tiwari / 9876543210 / Case 1234"
                    )
                    if _link_q and len(_link_q) >= 2:
                        try:
                            from modules.patient_merge import search_patients as _sp_link
                            _link_results = _sp_link(_link_q, limit=8)
                            # Exclude current patient
                            _link_results = [r for r in _link_results
                                             if str(r.get("id","")) != str(pid)]
                        except Exception as _lqe:
                            _link_results = []
                            st.caption(f"Search unavailable: {_lqe}")

                        if not _link_results:
                            st.caption("No other records found for that search.")
                        else:
                            for _lr in _link_results:
                                _lr_id   = str(_lr.get("id",""))
                                _lr_name = _lr.get("name","—")
                                _lr_mob  = _lr.get("mobile","") or "no mobile"
                                _lr_vc   = int(_lr.get("visit_count",0) or 0)
                                _lr_rec  = _lr.get("record_no","") or ""
                                _lr_del  = _lr.get("is_deleted",False)
                                if _lr_del:
                                    continue  # skip already-merged records

                                st.markdown(
                                    f"<div style='background:#0f1623;border:1px solid #334155;"
                                    f"border-radius:5px;padding:5px 10px;margin-bottom:4px;"
                                    f"font-size:0.78rem;color:#e2e8f0'>"
                                    f"<b>{_lr_name}</b>"
                                    + (f" · #{_lr_rec}" if _lr_rec else "")
                                    + f" · {_lr_mob} · {_lr_vc} visit(s)</div>",
                                    unsafe_allow_html=True
                                )
                                _ml_key  = f"rp_merge_manual_{_lr_id[:8]}"
                                _ml_conf = f"rp_merge_mconf_{_lr_id[:8]}"
                                if not st.session_state.get(_ml_conf):
                                    if st.button(
                                        f"🔗 Merge '{_lr_name}' → keep current patient",
                                        key=_ml_key,
                                        use_container_width=True,
                                    ):
                                        st.session_state[_ml_conf] = True
                                        st.rerun()
                                else:
                                    st.warning(
                                        f"⚠️ '{_lr_name}' ({_lr_vc} visit(s)) will merge here. "
                                        f"Cannot be undone without admin help."
                                    )
                                    _mc1, _mc2 = st.columns(2)
                                    with _mc1:
                                        if st.button("✅ Confirm merge",
                                                     key=f"rp_merge_myes_{_lr_id[:8]}",
                                                     type="primary",
                                                     use_container_width=True):
                                            try:
                                                from modules.patient_merge import merge_patients
                                                _r2 = merge_patients(str(pid), _lr_id,
                                                                     notes=f"Linked via manual search: {_link_q}")
                                                if _r2.get("ok"):
                                                    st.success(
                                                        f"✅ {_r2['visits']} visit(s) from "
                                                        f"'{_lr_name}' now in history. "
                                                        f"Old name saved as alias."
                                                    )
                                                    st.session_state.pop(_ml_conf, None)
                                                    st.session_state.pop("rp_link_search_q", None)
                                                    st.rerun()
                                                else:
                                                    st.error(_r2.get("error","Merge failed"))
                                            except Exception as _me2:
                                                st.error(f"Merge error: {_me2}")
                                    with _mc2:
                                        if st.button("❌ Cancel",
                                                     key=f"rp_merge_mno_{_lr_id[:8]}",
                                                     use_container_width=True):
                                            st.session_state.pop(_ml_conf, None)
                                            st.rerun()
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
                        COALESCE(pc.doctor_notes, pc.ortho_remarks, '') AS remarks
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
                # Prefer party_id (UUID) for reliable match, but keep a name/mobile
                # fallback so older retail orders saved before party_id linking remain visible.
                _pid_str = str(pid) if pid else ""
                import math as _math
                _mob_safe = "" if (
                    not mob
                    or (isinstance(mob, float) and _math.isnan(mob))
                    or str(mob).strip() in ("nan", "-", "None", "")
                ) else str(mob).strip()
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
                    WHERE (
                            o.party_id = %s::uuid
                         OR (
                                o.party_id IS NULL
                            AND LOWER(TRIM(COALESCE(o.patient_name, o.party_name, ''))) = LOWER(TRIM(%s))
                            AND COALESCE(o.patient_mobile,'') = COALESCE(%s,'')
                         )
                    )
                      AND COALESCE(o.is_deleted,false)=false
                    GROUP BY o.id, o.order_no, o.order_type,
                             o.created_at, o.status, o.is_converted, o.total_value
                    ORDER BY o.created_at DESC
                """, (_pid_str, name or "", _mob_safe)) or []
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
                                help="Pre-loads patient + Rx only; consultation receipt stays separate",
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
                                        _cons_uid = _res.get("consult_order_id") or _oid_cons
                                        if not (len(str(_cons_uid)) == 36 and str(_cons_uid).count("-") == 4):
                                            try:
                                                from modules.sql_adapter import run_query as _rq_cuid
                                                _cuid_rows = _rq_cuid(
                                                    "SELECT id::text FROM orders WHERE order_no=%s AND order_type='CONSULTATION' LIMIT 1",
                                                    (_oid_cons,)
                                                ) or []
                                                if _cuid_rows:
                                                    _cons_uid = _cuid_rows[0]["id"]
                                            except Exception:
                                                pass
                                        st.session_state["_consult_prefill"] = {
                                            "patient_name":     _res["patient_name"],
                                            "patient_mobile":   _res.get("patient_mobile", ""),
                                            "patient_id":       _res.get("patient_id", ""),
                                            "consult_order_id": _cons_uid,  # UUID, not order_no
                                            "rx_r": {"sph": _rxd.get("sph_r", 0), "cyl": _rxd.get("cyl_r", 0),
                                                     "axis": _rxd.get("ax_r", 0), "add": _rxd.get("add_r", 0)},
                                            "rx_l": {"sph": _rxd.get("sph_l", 0), "cyl": _rxd.get("cyl_l", 0),
                                                     "axis": _rxd.get("ax_l", 0), "add": _rxd.get("add_l", 0)},
                                            "order_lines": [],
                                            "include_consult_fee": False,
                                        }
                                        st.session_state.pop("_consult_fee_lines", None)
                                        st.session_state.pop("_consult_paid_advance_amount", None)
                                        st.session_state.pop("_consult_paid_advance_mode", None)
                                        st.session_state.pop("_consult_paid_advance_ref", None)
                                        st.session_state["_erp_mode"] = "CONSULT_BILLING"
                                        st.session_state["_visit_mode_default"] = 0
                                        st.session_state["_force_full_billing_mode"] = True
                                        st.session_state.pop("retail_visit_mode", None)
                                        st.toast(f"Loading {_res['patient_name']}…")
                                        safe_rerun()
                                except Exception as _be:
                                    st.error(f"Error: {_be}")

        # ── TAB 4: Patient Details (edit) ─────────────────────────────────────
        with tab_p:
            _pt_pid = str(pid) if pid else ""

            # Load full patient record including all extra and medical fields
            _pt = {}
            if _pt_pid and len(_pt_pid) > 10:
                try:
                    from modules.sql_adapter import run_query as _rq_pt, run_write as _rw_pt_schema
                    # Ensure optional patient-history columns exist before SELECT.
                    for _col in [
                        "alt_mobile TEXT", "email TEXT", "dob DATE",
                        "anniversary_date DATE", "occupation TEXT",
                        "diabetes BOOLEAN DEFAULT FALSE",
                        "hypertension BOOLEAN DEFAULT FALSE",
                        "thyroid BOOLEAN DEFAULT FALSE",
                        "cardiac_history BOOLEAN DEFAULT FALSE",
                        "asthma BOOLEAN DEFAULT FALSE",
                        "drug_allergy TEXT", "current_medication TEXT",
                        "surgery_history TEXT", "family_history TEXT",
                        "systemic_notes TEXT",
                    ]:
                        try:
                            _rw_pt_schema(f"ALTER TABLE patients ADD COLUMN IF NOT EXISTS {_col}")
                        except Exception:
                            pass
                    _pt_rows = _rq_pt("""
                        SELECT
                            master_name,
                            COALESCE(mobile,'')               AS mobile,
                            COALESCE(alt_mobile,'')           AS alt_mobile,
                            COALESCE(email,'')                AS email,
                            COALESCE(record_no,'')            AS record_no,
                            dob, anniversary_date,
                            COALESCE(occupation,'')           AS occupation,
                            COALESCE(diabetes,FALSE)          AS diabetes,
                            COALESCE(hypertension,FALSE)      AS hypertension,
                            COALESCE(thyroid,FALSE)           AS thyroid,
                            COALESCE(cardiac_history,FALSE)   AS cardiac_history,
                            COALESCE(asthma,FALSE)            AS asthma,
                            COALESCE(drug_allergy,'')         AS drug_allergy,
                            COALESCE(current_medication,'')   AS current_medication,
                            COALESCE(surgery_history,'')      AS surgery_history,
                            COALESCE(family_history,'')       AS family_history,
                            COALESCE(systemic_notes,'')       AS systemic_notes
                        FROM patients WHERE id=%s::uuid LIMIT 1
                    """, (_pt_pid,)) or []
                    if _pt_rows:
                        _pt = dict(_pt_rows[0])
                except Exception as _pt_ex:
                    st.caption(f"Could not load patient details: {_pt_ex}")

            if not _pt:
                st.info("No patient selected or patient record not found.")
            else:
                # ── Medical flags badge ────────────────────────────────────────
                _flags = []
                if _pt.get("diabetes"):        _flags.append("🩸 DM")
                if _pt.get("hypertension"):    _flags.append("💊 HTN")
                if _pt.get("thyroid"):         _flags.append("🦋 Thyroid")
                if _pt.get("cardiac_history"): _flags.append("❤️ Cardiac")
                if _pt.get("asthma"):          _flags.append("🫁 Asthma")
                _allergy = (_pt.get("drug_allergy") or "").strip()
                if _allergy: _flags.append(f"⚠️ Allergy: {_allergy}")
                if _flags:
                    st.markdown(
                        "<div style='background:#1a0a00;border:1px solid #b45309;"
                        "border-radius:5px;padding:5px 10px;font-size:0.76rem;"
                        "color:#fbbf24;margin-bottom:8px'>"
                        "<b>Medical flags:</b> " + " &nbsp;·&nbsp; ".join(_flags) + "</div>",
                        unsafe_allow_html=True
                    )

                # ── Edit form ──────────────────────────────────────────────────
                st.markdown(
                    "<div style='font-size:0.75rem;font-weight:700;color:#94a3b8;"
                    "text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px'>"
                    "Contact & Personal</div>",
                    unsafe_allow_html=True
                )
                _pd1, _pd2 = st.columns(2)
                with _pd1:
                    _pt_name = st.text_input(
                        "Full name *",
                        value=_pt.get("master_name","") or "",
                        key="rp_pt_name"
                    )
                    _pt_mob = st.text_input(
                        "Primary mobile",
                        value=_pt.get("mobile","") or "",
                        key="rp_pt_mob"
                    )
                    _pt_alt = st.text_input(
                        "Alternate mobile",
                        value=_pt.get("alt_mobile","") or "",
                        key="rp_pt_alt",
                        placeholder="Second contact number"
                    )
                with _pd2:
                    _pt_email = st.text_input(
                        "Email",
                        value=_pt.get("email","") or "",
                        key="rp_pt_email",
                        placeholder="patient@example.com"
                    )
                    _pt_occ = st.text_input(
                        "Occupation",
                        value=_pt.get("occupation","") or "",
                        key="rp_pt_occ",
                        placeholder="e.g. Teacher, Engineer"
                    )

                _pd3, _pd4 = st.columns(2)
                with _pd3:
                    _dob_s = str(_pt.get("dob","") or "")[:10]
                    _pt_dob = st.text_input(
                        "Date of birth (YYYY-MM-DD)",
                        value=_dob_s,
                        key="rp_pt_dob",
                        placeholder="1990-06-15"
                    )
                with _pd4:
                    _ann_s = str(_pt.get("anniversary_date","") or "")[:10]
                    _pt_ann = st.text_input(
                        "Anniversary (YYYY-MM-DD)",
                        value=_ann_s,
                        key="rp_pt_ann",
                        placeholder="2015-02-20"
                    )

                st.markdown(
                    "<div style='font-size:0.75rem;font-weight:700;color:#94a3b8;"
                    "text-transform:uppercase;letter-spacing:.06em;margin:8px 0 6px'>"
                    "Medical / Systemic History</div>",
                    unsafe_allow_html=True
                )
                _pm1, _pm2, _pm3, _pm4, _pm5 = st.columns(5)
                with _pm1:
                    _pt_dm  = st.checkbox("Diabetes",
                        value=bool(_pt.get("diabetes",False)), key="rp_pt_dm")
                with _pm2:
                    _pt_htn = st.checkbox("Hypertension",
                        value=bool(_pt.get("hypertension",False)), key="rp_pt_htn")
                with _pm3:
                    _pt_thy = st.checkbox("Thyroid",
                        value=bool(_pt.get("thyroid",False)), key="rp_pt_thy")
                with _pm4:
                    _pt_crd = st.checkbox("Cardiac",
                        value=bool(_pt.get("cardiac_history",False)), key="rp_pt_crd")
                with _pm5:
                    _pt_ast = st.checkbox("Asthma",
                        value=bool(_pt.get("asthma",False)), key="rp_pt_ast")

                _pm6, _pm7 = st.columns(2)
                with _pm6:
                    _pt_allergy = st.text_input(
                        "Drug allergy",
                        value=_pt.get("drug_allergy","") or "",
                        key="rp_pt_allergy",
                        placeholder="e.g. Penicillin, Sulfa drugs"
                    )
                    _pt_meds = st.text_area(
                        "Current medication",
                        value=_pt.get("current_medication","") or "",
                        key="rp_pt_meds",
                        placeholder="Metformin 500mg, Amlodipine 5mg...",
                        height=75,
                    )
                with _pm7:
                    _pt_surg = st.text_area(
                        "Surgery history",
                        value=_pt.get("surgery_history","") or "",
                        key="rp_pt_surg",
                        placeholder="Cataract surgery 2018 RE...",
                        height=75,
                    )
                    _pt_fam = st.text_input(
                        "Family ocular history",
                        value=_pt.get("family_history","") or "",
                        key="rp_pt_fam",
                        placeholder="e.g. Glaucoma in father"
                    )
                _pt_sysnotes = st.text_area(
                    "Other systemic notes",
                    value=_pt.get("systemic_notes","") or "",
                    key="rp_pt_sysnotes",
                    placeholder="Any other relevant medical information...",
                    height=60,
                )

                # ── Save ───────────────────────────────────────────────────────
                if st.button("💾 Save patient details", key="rp_pt_save",
                             type="primary", use_container_width=True):
                    if not _pt_name.strip():
                        st.warning("Name cannot be blank")
                    else:
                        try:
                            from modules.sql_adapter import run_write as _rw_pt, run_query as _rq_pt2

                            # Ensure new columns exist (idempotent)
                            for _col in [
                                "alt_mobile TEXT", "email TEXT", "dob DATE",
                                "anniversary_date DATE", "occupation TEXT",
                                "diabetes BOOLEAN DEFAULT FALSE",
                                "hypertension BOOLEAN DEFAULT FALSE",
                                "thyroid BOOLEAN DEFAULT FALSE",
                                "cardiac_history BOOLEAN DEFAULT FALSE",
                                "asthma BOOLEAN DEFAULT FALSE",
                                "drug_allergy TEXT", "current_medication TEXT",
                                "surgery_history TEXT", "family_history TEXT",
                                "systemic_notes TEXT",
                            ]:
                                try:
                                    _rw_pt(f"ALTER TABLE patients ADD COLUMN IF NOT EXISTS {_col}")
                                except Exception:
                                    pass

                            # Save old name as alias before overwriting
                            _old_name = (_pt.get("master_name") or "").strip()
                            _new_name = format_person_name(_pt_name)
                            if _old_name and _old_name.lower() != _new_name.lower():
                                try:
                                    _rw_pt("""
                                        INSERT INTO patient_aliases
                                            (patient_id, alias_type, alias_value)
                                        VALUES (%s::uuid, 'name', %s)
                                        ON CONFLICT (alias_type, alias_value) DO NOTHING
                                    """, (_pt_pid, _old_name))
                                except Exception:
                                    pass  # patient_aliases may not exist yet

                            def _pd_safe(s):
                                if not s or not str(s).strip(): return None
                                try:
                                    import datetime as _dtp
                                    return _dtp.date.fromisoformat(str(s).strip()[:10])
                                except Exception: return None

                            _rw_pt("""
                                UPDATE patients SET
                                    master_name        = %s,
                                    mobile             = %s,
                                    alt_mobile         = NULLIF(%s,''),
                                    email              = NULLIF(%s,''),
                                    dob                = %s,
                                    anniversary_date   = %s,
                                    occupation         = NULLIF(%s,''),
                                    diabetes           = %s,
                                    hypertension       = %s,
                                    thyroid            = %s,
                                    cardiac_history    = %s,
                                    asthma             = %s,
                                    drug_allergy       = NULLIF(%s,''),
                                    current_medication = NULLIF(%s,''),
                                    surgery_history    = NULLIF(%s,''),
                                    family_history     = NULLIF(%s,''),
                                    systemic_notes     = NULLIF(%s,'')
                                WHERE id = %s::uuid
                            """, (
                                _new_name,
                                _pt_mob.strip(),
                                _pt_alt.strip(),
                                _pt_email.strip(),
                                _pd_safe(_pt_dob),
                                _pd_safe(_pt_ann),
                                _pt_occ.strip(),
                                bool(_pt_dm),
                                bool(_pt_htn),
                                bool(_pt_thy),
                                bool(_pt_crd),
                                bool(_pt_ast),
                                _pt_allergy.strip(),
                                _pt_meds.strip(),
                                _pt_surg.strip(),
                                _pt_fam.strip(),
                                _pt_sysnotes.strip(),
                                _pt_pid,
                            ))
                            # Update session state so header reflects new name/mobile
                            st.session_state["retail_patient_name"]   = _new_name
                            st.session_state["retail_patient_mobile"] = _pt_mob.strip()
                            st.success(
                                f"✅ Saved — {_new_name}"
                                + (" · Medical flags updated" if any([_pt_dm,_pt_htn,_pt_thy,_pt_crd,_pt_ast]) else "")
                            )
                            st.rerun()
                        except Exception as _save_ex:
                            st.error(f"Save failed: {_save_ex}")

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

    # Live widgets: values write to session_state immediately. The old billing
    # form required a separate "Save Powers" click, which caused missed saves
    # and final-order errors when staff typed power then directly finalized.
    st.markdown("<div style='background:#fff3e0;border-left:3px solid #ff8c42;padding:4px 10px;border-radius:4px;margin:4px 0'><b>👁️ RIGHT EYE</b></div>", unsafe_allow_html=True)
    render_eye_power_section('R', "Right Eye")

    # ── Same power both eyes ─────────────────────────────────────────────
    _copy_rl = st.checkbox(
        "↔ Same power both eyes (copy R → L)",
        key="retail_copy_same_rl",
        help="When checked, Left eye mirrors Right eye power. Uncheck to enter different powers.",
    )
    if _copy_rl:
        # Mirror R widget values into L widget keys
        _rc = st.session_state.get("rx_reset_counter", 0)
        for _rf, _lf in (
            (f"new_sph_R_{_rc}",  f"new_sph_L_{_rc}"),
            (f"new_cyl_R_{_rc}",  f"new_cyl_L_{_rc}"),
            (f"new_axis_R_{_rc}", f"new_axis_L_{_rc}"),
            (f"new_add_R_{_rc}",  f"new_add_L_{_rc}"),
        ):
            _v = st.session_state.get(_rf, "")
            if _v is not None:
                st.session_state[_lf] = _v
        # Also mirror structured rx dict
        _rx_r = st.session_state.get("retail_new_rx_r") or {}
        if _rx_r:
            st.session_state["retail_new_rx_l"] = dict(_rx_r)

    st.markdown("<hr style='margin:6px 0;border-color:#ffe0b2'>", unsafe_allow_html=True)
    st.markdown("<div style='background:#fff3e0;border-left:3px solid #ff8c42;padding:4px 10px;border-radius:4px;margin:4px 0'><b>👁️ LEFT EYE</b></div>", unsafe_allow_html=True)

    if _copy_rl:
        # Read-only mirror of R
        _rx_r = st.session_state.get("retail_new_rx_r") or {}
        _parts = []
        if _rx_r.get("sph") is not None: _parts.append(f"SPH {float(_rx_r['sph']):+.2f}")
        if _rx_r.get("cyl") and abs(float(_rx_r["cyl"])) > 0.01: _parts.append(f"CYL {float(_rx_r['cyl']):+.2f}")
        if _rx_r.get("axis"): _parts.append(f"AX {int(_rx_r['axis'])}°")
        if _rx_r.get("add") and float(_rx_r.get("add",0)) > 0: _parts.append(f"ADD +{float(_rx_r['add']):.2f}")
        st.markdown(
            f"<div style='background:#e8f5e9;border:1px solid #4caf50;border-radius:6px;"
            f"padding:7px 14px;font-size:0.82rem;color:#2e7d32;font-weight:600'>"
            f"↔ Same as R: {'  ·  '.join(_parts) if _parts else '—'}"
            f"</div>",
            unsafe_allow_html=True,
        )
        # Still update retail_new_rx_l so the cart sees the values
        if _rx_r:
            st.session_state["retail_new_rx_l"] = dict(_rx_r)
    else:
        render_eye_power_section('L', "Left Eye")

    st.session_state["_rx_powers_confirmed"] = True

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
        sph_raw = st.text_input(
            "SPH",
            key=f"new_sph_{eye}_{_rc}",
            value=_rx_input_text(new_rx.get('sph')),
            placeholder="e.g. -1.25 or -125"
        )
    with col2:
        cyl_raw = st.text_input(
            "CYL",
            key=f"new_cyl_{eye}_{_rc}",
            value=_rx_input_text(new_rx.get('cyl')),
            placeholder="e.g. -0.75"
        )
    with col3:
        axis_raw = st.text_input(
            "AXIS",
            key=f"new_axis_{eye}_{_rc}",
            value=_rx_input_text(new_rx.get('axis'), is_axis=True),
            placeholder="e.g. 90"
        )
    with col4:
        add_raw = st.text_input(
            "ADD",
            key=f"new_add_{eye}_{_rc}",
            value=_rx_input_text(new_rx.get('add')),
            placeholder="e.g. 2.50"
        )

    # ── Strict validation ─────────────────────────────────────────────────
    sph,       _err_sph  = _validate_power_field(sph_raw,  "sph")
    cyl,       _err_cyl  = _validate_power_field(cyl_raw,  "cyl")
    axis,      _err_axis = _validate_power_field(axis_raw, "axis")
    add_power, _err_add  = _validate_power_field(add_raw,  "add")

    _rx_errors = [e for e in (_err_sph, _err_cyl, _err_axis, _err_add) if e]
    for _re in _rx_errors:
        st.error(f"⚠️ {_re}")
    # Store flag so Add to Cart / Confirm can block
    st.session_state[f"_rx_field_error_{eye}"] = bool(_rx_errors)

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
    # Retail must always force retail pricing display. Without this, a prior
    # Wholesale page visit can leave _current_order_type="WHOLESALE" in
    # Streamlit session_state and leak WLP into the retail selector.
    st.session_state["_current_order_type"] = "RETAIL"
    st.session_state["pricing_mode"] = "RETAIL"

    # ── Duplicate mode banner ─────────────────────────────────────────────
    if st.session_state.get("_retail_duplicate_mode"):
        _dup_prod = st.session_state.get("retail_selected_product")
        _dup_pname = ""
        if _dup_prod:
            _dup_pname = str((_dup_prod.get("product_row") or {}).get("product_name","") or "")
        st.markdown(
            "<div style='background:#1a1a2e;border:2px solid #6366f1;"
            "border-radius:8px;padding:8px 14px;margin-bottom:8px'>"
            "<div style='color:#a5b4fc;font-weight:800;font-size:0.85rem'>"
            "📄 Duplicate Mode — Patient & Power loaded</div>"
            "<div style='color:#64748b;font-size:0.72rem;margin-top:2px'>"
            + (f"Product: <b style='color:#e2e8f0'>{_dup_pname}</b> — clear below to pick different. "
               if _dup_pname else "No product — select below. ")
            + "Lens params & boxing carried over.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        if _dup_prod:
            _dc1, _dc2 = st.columns([3,1])
            _dc1.markdown(
                f"<div style='background:#0f172a;border:1px solid #334155;"
                f"border-radius:5px;padding:5px 10px;font-size:0.8rem;"
                f"color:#94a3b8'>✅ Using: <b style='color:#e2e8f0'>{_dup_pname}</b></div>",
                unsafe_allow_html=True,
            )
            if _dc2.button("🗑 Change", key="retail_dup_clear_product",
                           use_container_width=True):
                st.session_state["retail_selected_product"] = None
                st.session_state["reset_product_selector"] = True
                st.rerun()

    def _sync_visible_rx_from_widgets():
        _rc = st.session_state.get("rx_reset_counter", 0)
        for _eye, _state_key in (("R", "retail_new_rx_r"), ("L", "retail_new_rx_l")):
            _rx = {
                "sph": _parse_rx_text(st.session_state.get(f"new_sph_{_eye}_{_rc}", "")),
                "cyl": _parse_rx_text(st.session_state.get(f"new_cyl_{_eye}_{_rc}", "")),
                "axis": _parse_rx_text(
                    st.session_state.get(f"new_axis_{_eye}_{_rc}", ""),
                    is_axis=True,
                    min_value=0,
                    max_value=180,
                ),
                "add": _parse_rx_text(st.session_state.get(f"new_add_{_eye}_{_rc}", "")),
            }
            if any(_rx.get(k) is not None for k in ("sph", "cyl", "axis", "add")):
                st.session_state[_state_key] = _rx

    _sync_visible_rx_from_widgets()

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
        elif result.get("oph_spec") and result.get("oph_spec", {}).get("complete"):
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
            _cl_resolved = None
            if result.get("is_contact") and _cl_resolve_for_selected_product:
                try:
                    _cl_resolved = _cl_resolve_for_selected_product(
                        product,
                        st.session_state.get("retail_new_rx_r"),
                        st.session_state.get("retail_new_rx_l"),
                    )
                    st.session_state["_cl_resolver_result"] = _cl_resolved
                    if _cl_should_show_resolution_notice and _cl_should_show_resolution_notice(
                        _cl_resolved, str(current_product_id)
                    ):
                        with st.expander("🧠 Contact lens product intelligence", expanded=True):
                            for _ln in _cl_resolved.get("lines", []):
                                _route = str(_ln.get("route") or "")
                                _pname = _ln.get("product_name") or "Not found"
                                _msg = _ln.get("message") or ""
                                if _route == "STOCK":
                                    st.success(f"{_ln.get('eye')}: {_pname} — {_msg}")
                                elif _route == "SUPPLIER_ORDER":
                                    st.warning(f"{_ln.get('eye')}: {_pname} — {_msg}")
                                else:
                                    st.error(f"{_ln.get('eye')}: {_msg}")
                except Exception as _cl_ex:
                    st.caption(f"Contact lens resolver skipped: {_cl_ex}")

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
                    _oph_spec = result.get("oph_spec") or st.session_state.get(f"_oph_spec_{pid}") or {}
                    if _oph_spec and _oph_spec.get("complete"):
                        st.session_state[f"_oph_spec_{pid}"] = _oph_spec
                        _per_lens = _oph_spec_per_lens_price(_oph_spec)
                        _stk_r = (_oph_spec.get("stock_r") or {}).get("status", "")
                        if _stk_r == "STOCK":
                            _qty_r = int((_oph_spec.get("stock_r") or {}).get("qty_r") or 0)
                            st.success(f"👁️ RIGHT [{power_label}] → {_qty_r} PCS · ₹{_per_lens:,.0f}/lens")
                        else:
                            st.info(f"👁️ RIGHT [{power_label}] → 📋 RX Order · ₹{_per_lens:,.0f}/lens")
                    else:
                        st.warning("Select lens Index and Coating above to carry price into billing.")
                else:
                    # Contact lens → existing power-wise stock
                    r_qty = get_power_wise_stock(
                        pid, rx_r.get("sph"), rx_r.get("cyl"),
                        rx_r.get("axis"), rx_r.get("add")
                    )
                    if r_qty > 0:
                        r_disp = format_quantity_display(r_qty, product)
                        st.success(f"👁️ RIGHT [{power_label}] available → {r_disp} ({r_qty} PCS)")
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
                    _oph_spec = result.get("oph_spec") or st.session_state.get(f"_oph_spec_{pid}") or {}
                    if _oph_spec and _oph_spec.get("complete"):
                        _per_lens = _oph_spec_per_lens_price(_oph_spec)
                        _stk_l = (_oph_spec.get("stock_l") or {}).get("status", "")
                        if _stk_l == "STOCK":
                            _qty_l = int((_oph_spec.get("stock_l") or {}).get("qty_l") or 0)
                            st.success(f"👁️ LEFT [{power_label}] → {_qty_l} PCS · ₹{_per_lens:,.0f}/lens")
                        else:
                            st.info(f"👁️ LEFT [{power_label}] → 📋 RX Order · ₹{_per_lens:,.0f}/lens")
                else:
                    l_qty = get_power_wise_stock(
                        pid, rx_l.get("sph"), rx_l.get("cyl"),
                        rx_l.get("axis"), rx_l.get("add")
                    )
                    if l_qty > 0:
                        l_disp = format_quantity_display(l_qty, product)
                        st.success(f"👁️ LEFT [{power_label}] available → {l_disp} ({l_qty} PCS)")
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
                st.warning("⚠️ Enter power for Right/Left eye first. It is saved automatically as you type.")
                return

            # Auto-detect eye mode (old behavior)
            has_r = bool(st.session_state.retail_new_rx_r)
            has_l = bool(st.session_state.retail_new_rx_l)

            if has_r and has_l:
                eye_mode = "BOTH"
                st.info("👁️ Both eyes detected → R then L")
                _eye_choice = st.radio(
                    "Eyes to add",
                    ["Both eyes", "Right eye only", "Left eye only"],
                    horizontal=True,
                    key="retail_lens_eye_choice",
                    help="Use one-eye option when patient wants only one box/lens.",
                )
                selected_eyes = (
                    ["R", "L"] if _eye_choice == "Both eyes"
                    else ["R"] if _eye_choice == "Right eye only"
                    else ["L"]
                )

            elif has_r:
                eye_mode = "R"
                st.info("👁️ Only RIGHT eye detected")
                selected_eyes = ["R"]

            elif has_l:
                eye_mode = "L"
                st.info("👁️ Only LEFT eye detected")
                selected_eyes = ["L"]

            else:
                st.warning("⚠️ Enter power first. It is saved automatically as you type.")
                return

            # ===============================
            # Quantity Engine (R/L)
            # ===============================
            st.markdown("---")

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

            with st.expander("📊 Enter Quantity for Each Eye", expanded=False):
                st.caption("This is the selected order quantity. Stock shown above is only availability.")
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
                            _pcs_key = f"retail_qe_ps_{eye}_pcs"
                            if engine.mode in ["PCS_ONLY", "NO_ONLY"] and int(st.session_state.get(_pcs_key, 0) or 0) <= 0:
                                st.session_state[_pcs_key] = 1
                            user_input["pcs"] = st.number_input(
                                "Qty (PCS)",
                                min_value=_pcs_min,
                                max_value=1000,
                                value=_pcs_default,
                                step=1,
                                key=_pcs_key
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
                if has_r and "R" in selected_eyes:
                    qty_r = render_eye_qty("R")
                    st.session_state.retail_final_qty_R = qty_r

                # LEFT EYE
                if has_l and "L" in selected_eyes:
                    st.markdown("---")
                    qty_l = render_eye_qty("L")
                    st.session_state.retail_final_qty_L = qty_l

            st.markdown("---")

            # Block Add if Zero
            block_add = False
            # Block if power field validation errors
            if st.session_state.get("_rx_field_error_R") or st.session_state.get("_rx_field_error_L"):
                st.error("⚠️ Fix power entry errors before adding to cart.")
                block_add = True

            if "R" in selected_eyes and st.session_state.retail_final_qty_R <= 0:
                st.warning("⚠️ Enter RIGHT eye quantity")
                block_add = True

            if "L" in selected_eyes and st.session_state.retail_final_qty_L <= 0:
                st.warning("⚠️ Enter LEFT eye quantity")
                block_add = True

            if result.get('is_lens') and not result.get('is_contact'):
                _oph_ready = bool((result.get("oph_spec") or {}).get("complete"))
                _oph_price_ready = _oph_spec_per_lens_price(result.get("oph_spec") or {}) > 0
                if not (_oph_ready and _oph_price_ready):
                    st.warning("⚠️ Select lens specification with price before adding.")
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
                    st.session_state.retail_pending_eyes = list(selected_eyes)

                    # Default path: auto-allocate FIFO and add to cart immediately.
                    # Staff only sees the batch editor when FIFO cannot complete
                    # or when they explicitly need to override allocation.
                    provisional_id = create_provisional_order()
                    auto_added = _auto_finalize_pending_stock_eyes(provisional_id)
                    if st.session_state.get("retail_pending_eyes") and result.get("is_contact"):
                        auto_added += _auto_finalize_pending_vendor_eyes(provisional_id)

                    if auto_added > 0 and not st.session_state.get("retail_pending_eyes"):
                        clear_allocation_state()
                        st.session_state.retail_show_batch_editor = False
                        try:
                            st.toast(f"✅ Auto-allocated {auto_added} eye(s) by FIFO")
                        except Exception:
                            pass
                    else:
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
                _frame_stock_ok = False
                _frame_stock_available = 0

                # Resolve price — mrp first, then selling_price
                _frame_mrp = float(product.get('mrp') or 0)
                if not _frame_mrp:
                    _frame_mrp = float(product.get('selling_price') or 0)
                try:
                    from modules.sql_adapter import run_query as _rq_frame_stock
                    _fs_rows = _rq_frame_stock(
                        """SELECT COALESCE(SUM(
                                  GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0))
                               ),0) AS available_qty
                           FROM inventory_stock
                           WHERE product_id = %s::uuid
                             AND (%s = '' OR UPPER(TRIM(batch_no)) = UPPER(TRIM(%s)))
                             AND COALESCE(is_active, TRUE) = TRUE""",
                        (str(current_product_id), str(sku_code or ""), str(sku_code or ""))
                    ) or []
                    _frame_stock_available = int(float((_fs_rows[0].get("available_qty") if _fs_rows else 0) or 0))
                    _frame_stock_ok = _frame_stock_available >= 1
                except Exception:
                    _frame_stock_ok = False

                # Use a short clean key (no UUID hyphens) to avoid Streamlit key issues
                _price_key = "frame_price_override"

                _fc1, _fc2 = st.columns([3, 2])
                with _fc1:
                    st.info(
                        f"**{product_name}** | SKU: `{sku_code}` | "
                        f"📍 {loc}"
                    )
                    if _frame_stock_ok:
                        st.success(f"✅ Frame inventory available: {_frame_stock_available} pc")
                    else:
                        st.warning(
                            "⚠️ Frame not available in inventory. You may still take the order, "
                            "but it will go as supplier/vendor arrangement."
                        )
                        st.checkbox(
                            "Proceed without stock — arrange frame from vendor",
                            key="frame_allow_vendor_order",
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
                    _allow_frame_vendor = bool(st.session_state.get("frame_allow_vendor_order"))
                    if not _frame_stock_ok and not _allow_frame_vendor:
                        st.error(
                            "❌ Frame stock is not available. Tick the confirmation if you want "
                            "to proceed and arrange it from vendor."
                        )
                        st.stop()
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
                _frame_stock_id = ""
                if is_frame and sku_code and _frame_stock_ok:
                    try:
                        from modules.sql_adapter import run_query as _rq_fstock
                        _srow = _rq_fstock(
                            """SELECT id::text AS stock_id, batch_no
                               FROM inventory_stock
                               WHERE product_id = %s::uuid
                                 AND UPPER(TRIM(batch_no)) = UPPER(TRIM(%s))
                                 AND COALESCE(is_active, TRUE) = TRUE
                                 AND GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0)) >= 1
                               ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                               LIMIT 1""",
                            (str(current_product_id), sku_code)
                        ) or []
                        _frame_stock_id = str(_srow[0].get("stock_id") or "") if _srow else ""
                    except Exception:
                        _frame_stock_id = ""

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
                                 AND UPPER(TRIM(batch_no)) = UPPER(TRIM(%s))
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
                            'manufacturing_route': 'STOCK' if _frame_stock_ok else 'VENDOR',
                            'stock_source':        'STOCK' if _frame_stock_ok else 'VENDOR',
                            **({'stock_id': _frame_stock_id} if _frame_stock_id else {}),
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
                        [{
                            'batch_no': sku_code,
                            'allocated_qty': 1,
                            'selling_price': pcs_price,
                            'qty': 1,
                            **({'stock_id': _frame_stock_id, 'batch_id': _frame_stock_id} if _frame_stock_id else {}),
                        }]
                        if is_frame and sku_code and _frame_stock_ok else []
                    ),
                    'suggested_allocation': (
                        [{
                            'batch_no': sku_code,
                            'allocated_qty': 1,
                            'selling_price': pcs_price,
                            'qty': 1,
                            **({'stock_id': _frame_stock_id, 'batch_id': _frame_stock_id} if _frame_stock_id else {}),
                        }]
                        if is_frame and sku_code and _frame_stock_ok else []
                    ),
                    'allocated_qty': 1 if (is_frame and _frame_stock_ok) else 0,
                    'ready_qty': 1 if (is_frame and _frame_stock_ok) else 0,
                    'batch_status': 'ALLOCATED' if (is_frame and _frame_stock_ok) else 'PENDING',

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
                    'manufacturing_route': ('STOCK' if _frame_stock_ok else 'VENDOR') if is_frame else None,
                    'stock_source':        ('STOCK' if _frame_stock_ok else 'VENDOR') if is_frame else None,

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
                                     AND UPPER(TRIM(batch_no)) = UPPER(TRIM(%s))
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

    selected_result = st.session_state.get("retail_selected_product", {})
    if selected_result.get("is_contact") and _cl_resolve_for_selected_product and _cl_line_for_eye:
        try:
            _cl_res = st.session_state.get("_cl_resolver_result") or _cl_resolve_for_selected_product(
                product,
                st.session_state.get("retail_new_rx_r"),
                st.session_state.get("retail_new_rx_l"),
            )
            _cl_line = _cl_line_for_eye(_cl_res, eye_side)
            _cl_product = _cl_line.get("product_row") if _cl_line else None
            if _cl_product and _cl_line.get("product_id"):
                _old_pid = str(product.get("product_id") or product.get("id") or "")
                _new_pid = str(_cl_line.get("product_id") or "")
                if _new_pid and _new_pid != _old_pid:
                    st.info(
                        f"🧠 {eye_side} eye mapped to **{_cl_line.get('product_name')}** "
                        "from entered power."
                    )
                product = _hydrate_product_gst(dict(_cl_product))
                product_id = str(product["product_id"])
        except Exception as _cl_ex:
            st.caption(f"Contact lens resolver allocation skipped: {_cl_ex}")

    # FIX: Frames have no eye side — passing eye_side='R'/'L' to get_batches_fifo
    # filters against NULL/OTHER stock rows and returns empty, showing "No stock available".
    # For frames, pass eye_side=None so the query matches on product_id + SKU only.
    _selected_for_alloc = st.session_state.get("retail_selected_product", {})
    _is_frame_alloc = _selected_for_alloc.get("is_frame", False)
    _batches_eye_side = None if _is_frame_alloc else eye_side

    # ── Ophthalmic lens: use adapter for availability + price ──────────────
    _is_ophthalmic = (
        selected_result.get("is_lens") and not selected_result.get("is_contact")
    )

    ophl_avail = None
    oph_spec = _oph_spec_for_product(product_id) if _is_ophthalmic else {}
    oph_coating = oph_spec.get("coating") if oph_spec else None
    batches_df = get_batches_fifo(
        product_id, sph_val, cyl_val, axis_val, add_val, _batches_eye_side,
        coating=oph_coating if _is_ophthalmic else None,
    )
    total_available = batches_df['available_qty'].sum() if not batches_df.empty else 0

    total_available = int(total_available or 0)
    qty = int(qty or 0)
    if qty <= 0 and total_available > 0:
        qty = 1
        if eye_side == "R":
            st.session_state.retail_final_qty_R = 1
        elif eye_side == "L":
            st.session_state.retail_final_qty_L = 1

    oph_spec_unit_price = _oph_spec_per_lens_price(oph_spec)
    if _is_ophthalmic:
        ophl_avail = check_ophthalmic_availability(
            product_id, sph=sph_val, cyl=cyl_val,
            axis=axis_val, add_power=add_val, eye_side=eye_side
        )
        if oph_spec_unit_price > 0:
            ophl_avail = dict(ophl_avail or {})
            ophl_avail["selling_price"] = oph_spec_unit_price
            ophl_avail["mrp"] = oph_spec_unit_price
            ophl_avail["purchase_rate"] = (
                _safe_price(((oph_spec.get("price") or {}).get("purchase"))) / 2
            )
        if ophl_avail["status"] == "STOCK":
            total_available = max(total_available, int(ophl_avail.get("available_qty") or 0))
            if total_available < qty:
                st.warning(f"⚠️ Only {total_available} in stock. Remaining → RX order.")
        elif ophl_avail["status"] == "RX":
            if total_available > 0:
                ophl_avail["status"] = "STOCK"
            else:
                st.info(f"📋 RX order lens — ₹{ophl_avail['mrp']:.0f} per piece")
                total_available = 0
        else:
            if oph_spec_unit_price > 0:
                if total_available > 0:
                    ophl_avail["status"] = "STOCK"
                else:
                    ophl_avail["status"] = "RX"
                    ophl_avail["message"] = "📋 RX order from selected lens specification"
                    st.info(f"📋 RX order lens — ₹{oph_spec_unit_price:.0f} per piece")
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

    _db_price_raw = 0.0
    try:
        from modules.core.price_source_resolver import resolve_db_price

        _db_price = resolve_db_price(
            product_id,
            "RETAIL",
            product=product,
            prefer_batch=not batches_df.empty,
            index_value=(oph_spec or {}).get("index"),
            coating=(oph_spec or {}).get("coating") or oph_coating,
            treatment=(oph_spec or {}).get("treatment"),
        )
        _db_price_raw = float(_db_price.get("raw_price") or 0)
    except Exception:
        _db_price_raw = 0.0

    temp_allocation = {
        'line_id': str(uuid.uuid4()),
        'product_id': product_id,
        'product_name': (
            _oph_name(str(product.get('product_name', '')), oph_spec)
            if _oph_name and oph_spec and oph_spec.get("complete")
            else product['product_name']
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
        'batches': (
            batches_df.to_dict("records")
            if _is_ophthalmic and not batches_df.empty
            else allocation.get('batches', [])
        ),
        'coating': oph_coating,
        # ── ophthalmic adapter fields ──────────────────────────────────────
        'lens_item_type': (
            ophl_avail['status'] if ophl_avail else 'STOCK'
        ),   # 'STOCK' | 'RX' | 'UNAVAILABLE'
        'ophl_stock_id': (
            ophl_avail.get('stock_id') if ophl_avail else None
        ),
        'unit_price': normalize_to_pcs_price(
            oph_spec_unit_price
            or (resolve_price_for_order_type(ophl_avail, "RETAIL") if ophl_avail else 0)
            or allocation.get('price', 0)
            or _db_price_raw
            or _reference_unit_price(product, "RETAIL"),
            product
        ),
        'oph_spec': dict(oph_spec or {}),
        'gst_percent': float(product.get('gst_percent') or 0),
    }

    st.session_state.retail_current_allocation = temp_allocation
    st.session_state.retail_show_batch_editor = True

# ============================================================================
# LENS PARAMETERS - FROM OLD WORKING VERSION
# ============================================================================

def _finalize_retail_stock_allocation_to_cart(
    allocation: dict,
    product: dict,
    batch_quantities: list,
    punched_qty: int,
    provisional_id: str,
) -> bool:
    """Add one fully allocated stock eye to retail cart. Returns False if invalid."""
    total_allocated = sum(int(b.get('allocated_qty') or 0) for b in (batch_quantities or []))
    if not allocation or not product or punched_qty <= 0 or total_allocated != punched_qty:
        return False

    pending_qty = max(0, int(punched_qty) - int(total_allocated))
    pcs_price = money(compute_weighted_price(batch_quantities))
    if pcs_price == 0.0:
        pcs_price = money(_safe_price(allocation.get('unit_price', 0)))
    if pcs_price <= 0:
        return False

    total_price = round(pcs_price * int(punched_qty), 2)

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
        'lens_params': {
            **dict(st.session_state.retail_lens_params or {}),
            **_oph_lens_params(allocation.get('oph_spec') or {}),
            'manufacturing_route': 'STOCK',
            'stock_source': 'STOCK',
            'batch_allocation': batch_quantities,
            'batch_status': 'ALLOCATED',
        },
        'boxing_params': dict(st.session_state.retail_boxing_params or {}),
        'requested_qty': int(punched_qty),
        'billing_qty': int(total_allocated),
        'order_qty': pending_qty,
        'display_qty': format_quantity_display(int(punched_qty), product),
        'batch_allocation': batch_quantities,
        'suggested_allocation': list(batch_quantities),
        'allocated_qty': int(total_allocated),
        'ready_qty': int(total_allocated),
        'batch_status': 'ALLOCATED',
        'manufacturing_route': 'STOCK',
        'stock_source': 'STOCK',
        'unit_price': round(pcs_price, 2),
        'total_price': total_price,
        'box_size': int(product.get('box_size', 0) or 0),
        'unit': str(product.get('unit', '') or ''),
        'gst_percent': float(allocation.get('gst_percent') or 0),
        'gst_amount': 0.0,
        'purchase_rate': float(allocation.get('purchase_rate') or product.get('purchase_rate') or 0),
        'pricing_applied_at': datetime.datetime.now().isoformat(),
        'pricing_source': 'batch_weighted',
        'status': 'Complete' if pending_qty == 0 else 'Partial',
        'created_at': datetime.datetime.now().isoformat()
    }

    _stamp_line_tax(line, "RETAIL")
    _stamp_cart_line_discount(line)
    st.session_state.retail_order_lines.append(line)

    _fin_set = st.session_state.get("_retail_finalized_eyes", set())
    _fin_set.add(allocation.get("eye_side", ""))
    st.session_state._retail_finalized_eyes = _fin_set
    return True


def _auto_finalize_pending_stock_eyes(provisional_id: str) -> int:
    """
    After the user finalizes the first eye, auto-add any remaining queued eye
    that can be fully allocated by FIFO without extra user choices.
    """
    added = 0
    while st.session_state.get("retail_pending_eyes"):
        next_eye = st.session_state.retail_pending_eyes[0]
        prepare_allocation(next_eye)
        allocation = st.session_state.get("retail_current_allocation")
        if not allocation:
            break

        product = _hydrate_product_gst(dict(st.session_state.retail_selected_product['product_row']))
        qty = int(allocation.get("requested_qty", 0) or 0)
        if qty <= 0 or allocation.get("lens_item_type") != "STOCK":
            break

        _is_frame_be = st.session_state.get("retail_selected_product", {}).get("is_frame", False)
        _be_eye_side = None if _is_frame_be else allocation.get('eye_side')
        batches_df = get_batches_fifo(
            allocation['product_id'],
            allocation.get('sph'),
            allocation.get('cyl'),
            allocation.get('axis'),
            allocation.get('add_power'),
            _be_eye_side,
            coating=allocation.get('coating'),
        )
        if batches_df.empty:
            break

        remaining = qty
        batch_quantities = []
        for _, row in batches_df.iterrows():
            available_qty = int(row.get('available_qty') or 0)
            batch_qty = min(remaining, available_qty)
            if batch_qty <= 0:
                continue
            batch = dict(row)
            batch_quantities.append({
                'batch_no': batch.get('batch_no'),
                'batch_id': str(batch.get('batch_id')) if batch.get('batch_id') else None,
                'expiry_date': str(batch.get('expiry_date')),
                'allocated_qty': batch_qty,
                'available_qty': available_qty,
                'selling_price': resolve_price_for_order_type(batch, "RETAIL") or allocation.get('unit_price', 0),
                'unit': str(product.get('unit', '') or ''),
                'box_size': int(product.get('box_size', 0) or 0),
            })
            remaining -= batch_qty
            if remaining <= 0:
                break

        if remaining > 0:
            break

        if not _finalize_retail_stock_allocation_to_cart(
            allocation, product, batch_quantities, qty, provisional_id
        ):
            break

        st.session_state.retail_pending_eyes.pop(0)
        clear_allocation_state()
        added += 1

    return added


def _auto_finalize_pending_vendor_eyes(provisional_id: str) -> int:
    """
    Add selected contact-lens eyes as vendor/pending lines when FIFO stock cannot
    complete them. This keeps both-eye contact lens orders as R + L lines while
    still allowing one-eye orders from the eye selector.
    """
    added = 0
    product = _hydrate_product_gst(dict(st.session_state.retail_selected_product['product_row']))
    while st.session_state.get("retail_pending_eyes"):
        next_eye = st.session_state.retail_pending_eyes[0]
        prepare_allocation(next_eye)
        allocation = st.session_state.get("retail_current_allocation")
        if not allocation:
            break

        qty = int(allocation.get("requested_qty", 0) or 0)
        if qty <= 0:
            break

        unit_price = money(_safe_price(allocation.get("unit_price", 0)))
        if unit_price <= 0:
            unit_price = money(_reference_unit_price(product, "RETAIL"))
        if unit_price <= 0:
            break

        line = {
            'line_id': str(uuid.uuid4()),
            'provisional_order_id': provisional_id,
            'product_id': allocation['product_id'],
            'product_name': allocation['product_name'],
            'brand': allocation.get('brand', ''),
            'main_group': allocation.get('main_group', ''),
            'eye_side': allocation.get('eye_side'),
            'sph': allocation.get('sph'),
            'cyl': allocation.get('cyl'),
            'axis': allocation.get('axis'),
            'add_power': allocation.get('add_power'),
            'lens_params': {
                **dict(st.session_state.retail_lens_params or {}),
                'manufacturing_route': 'VENDOR',
                'stock_source': 'VENDOR',
                'batch_status': 'PENDING',
            },
            'boxing_params': dict(st.session_state.retail_boxing_params or {}),
            'requested_qty': qty,
            'billing_qty': qty,
            'order_qty': qty,
            'display_qty': format_quantity_display(qty, product),
            'batch_allocation': [],
            'suggested_allocation': [],
            'allocated_qty': 0,
            'ready_qty': 0,
            'batch_status': 'PENDING',
            'manufacturing_route': 'VENDOR',
            'stock_source': 'VENDOR',
            'unit_price': round(unit_price, 2),
            'total_price': round(unit_price * qty, 2),
            'box_size': int(product.get('box_size', 0) or 0),
            'unit': str(product.get('unit', '') or ''),
            'gst_percent': float(allocation.get('gst_percent') or product.get('gst_percent') or 0),
            'gst_amount': 0.0,
            'purchase_rate': float(allocation.get('purchase_rate') or product.get('purchase_rate') or 0),
            'pricing_applied_at': datetime.datetime.now().isoformat(),
            'pricing_source': 'reference_vendor',
            'status': 'Pending',
            'created_at': datetime.datetime.now().isoformat(),
        }

        _stamp_line_tax(line, "RETAIL")
        _stamp_cart_line_discount(line)
        st.session_state.retail_order_lines.append(line)
        st.session_state.retail_pending_eyes.pop(0)
        clear_allocation_state()
        added += 1

    return added

