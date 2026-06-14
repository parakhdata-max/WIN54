"""
modules/retail_punching_data.py
================================
Data helpers, session state initialisation, pricing utilities,
tax stamping, and reset functions for retail punching.
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

def _render_post_save_actions(order_no, party, mobile, total, order_type, advance=0.0, delivery="", on_account=True, lines=None, status_label="RECEIVED", end_customer_name=""):
    try:
        from modules.post_save_actions import render_post_save_actions
        render_post_save_actions(order_no, party, mobile, total, order_type, advance, delivery, on_account=on_account, lines=lines or [], status_label=status_label, end_customer_name=end_customer_name)
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
        'retail_search_mode': 'Name / Mobile',
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

    NOTE: Writes the NET value to BOTH `billing_total` (in-memory UI field)
    AND `total_price` (the column persisted to order_lines). Earlier the
    function only updated billing_total, which left total_price as the
    gross — the INSERT a few screens later then wrote gross to DB and
    backoffice/reports/accounting all saw a number that ignored the
    discount. Wholesale already does this via restamp_line_totals; this
    brings retail into line.
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
            if net < 0:
                net = 0.0
            # Write net to BOTH fields so the downstream INSERT picks up net
            line["billing_total"] = net
            line["total_price"]   = net
            # Re-stamp GST on net price (writes line.gst_amount on net)
            _stamp_line_tax(line, "RETAIL")
    except Exception:
        pass


def _ensure_service_product_id(charge_type: str = "MISC", label: str = "Service Charge") -> str:
    """Find/create a lightweight Services product used by service-only punching."""
    try:
        import uuid as _uuid
        from modules.sql_adapter import run_query as _rq_sp, run_write as _rw_sp
        _ct = str(charge_type or "MISC").upper().strip()
        _label = str(label or _ct.title()).strip()
        _prod_name = f"{_label} Charge" if not _label.lower().endswith("charge") else _label
        _rows = _rq_sp("""
            SELECT id::text AS id
            FROM products
            WHERE LOWER(product_name) = LOWER(%s)
              AND COALESCE(is_active, TRUE) = TRUE
            LIMIT 1
        """, (_prod_name,)) or []
        if _rows:
            return str(_rows[0]["id"])
        _pid = str(_uuid.uuid4())
        _rw_sp("""
            INSERT INTO products
                (id, product_name, main_group, category, unit,
                 gst_percent, is_active, created_at)
            VALUES (%s::uuid, %s, 'Services', 'Services', 'S',
                    %s, TRUE, NOW())
            ON CONFLICT (id) DO NOTHING
        """, (_pid, _prod_name, 18 if _ct in ("FITTING", "COLOURING", "COURIER", "MISC") else 0))
        return _pid
    except Exception:
        return ""


def _service_master_rows(order_type: str = "RETAIL", party_id: str = "") -> list:
    """Active services visible in punching; falls back to legacy service groups."""
    try:
        from modules.backoffice.service_master import fetch_service_types, service_price
        rows = fetch_service_types(active_only=True)
        out = []
        for r in rows:
            rr = dict(r)
            rr["default_price"] = service_price(rr, order_type, party_id=party_id)
            out.append(rr)
        if out:
            return out
    except Exception:
        pass
    from modules.core.business_rules import SERVICE_CHARGE_TYPES as _SCT
    return [
        {
            "service_code": k,
            "service_group": k,
            "service_name": v.get("label", k.title()),
            "gst_percent": v.get("default_gst", 0),
            "production_route": "COLOURING" if k == "COLOURING" else ("FITTING" if k == "FITTING" else ""),
            "default_price": 0.0,
        }
        for k, v in _SCT.items()
    ]


def _service_ui_meta(service_group: str) -> dict:
    from modules.core.business_rules import SERVICE_CHARGE_TYPES as _SCT
    group = str(service_group or "MISC").upper()
    return _SCT.get(group, _SCT["MISC"])


def _uploaded_image_b64(uploaded_file) -> str:
    if not uploaded_file:
        return ""
    try:
        import base64 as _b64
        return _b64.b64encode(uploaded_file.read()).decode("ascii")
    except Exception:
        return ""


def _make_service_cart_line(charge: dict, order_type: str = "RETAIL") -> dict:
    """Build one billable SERVICE cart line for fitting/colouring-only orders."""
    import uuid as _uuid
    import datetime as _dt
    from modules.core.business_rules import SERVICE_CHARGE_TYPES as _SCT

    _svc_def = charge.get("service_def") or {}
    _ct = str(charge.get("service_group") or charge.get("type") or _svc_def.get("service_group") or "MISC").upper().strip()
    _code = str(charge.get("service_code") or _svc_def.get("service_code") or _ct).upper().strip()
    _cfg = _SCT.get(_ct, _SCT["MISC"])
    _desc = str(charge.get("desc") or _svc_def.get("service_name") or _cfg["label"])
    _factor = float(charge.get("qty_factor") or 1)
    _rate = float(charge.get("amt") if charge.get("amt") is not None else 0)
    _amt = round(_rate * _factor, 2)
    _gst = float(charge.get("gst") if charge.get("gst") is not None else (_svc_def.get("gst_percent") if _svc_def else _cfg.get("default_gst", 0)))
    _gst_amt = round(_amt * _gst / 100, 2)
    _total = round(_amt + _gst_amt, 2)
    _pid = _ensure_service_product_id(_ct, _cfg["label"])
    _prod_route = str(charge.get("service_production_type") or _svc_def.get("production_route") or "").upper()
    if not _prod_route:
        _prod_route = "COLOURING" if _ct == "COLOURING" else ("FITTING" if _ct == "FITTING" else "")
    _mfg_route = "INHOUSE" if _prod_route == "COLOURING" else ("FITTING" if _prod_route == "FITTING" else "STOCK")
    return {
        "line_id": str(charge.get("line_id") or _uuid.uuid4()),
        "provisional_order_id": None,
        "product_id": _pid,
        "product_name": _desc,
        "brand": "Service",
        "main_group": "Services",
        "category": "Services",
        "unit": "SERVICE",
        "batch_no": "",
        "eye_side": "SERVICE",
        "sph": None, "cyl": None, "axis": None, "add_power": None,
        "lens_params": {
            "charge_type": _ct,
            "service_type": _ct,
            "service_group": _ct,
            "service_code": _code,
            "service_description": _desc,
            "service_origin": f"{order_type.lower()}_punching",
            "service_production_type": _prod_route,
            "manufacturing_route": _mfg_route,
            "batch_status": "PENDING" if _prod_route else "READY",
            "service_instruction": str(charge.get("instruction") or ""),
            "colour_sample_photo": str(charge.get("colour_sample_photo") or ""),
            "colour_sample_filename": str(charge.get("colour_sample_filename") or ""),
            "service_qty_factor": _factor,
            "service_rate_per_pair": _rate,
        },
        "boxing_params": {},
        "requested_qty": 1,
        "billing_qty": 1,
        "quantity": 1,
        "order_qty": 0,
        "display_qty": f"{_factor:g} SERVICE",
        "batch_allocation": [],
        "allocated_qty": 0 if _prod_route else 1,
        "ready_qty": 0 if _prod_route else 1,
        "unit_price": _amt,
        "billing_total": _total,
        "total_price": _total,
        "gst_percent": _gst,
        "gst_amount": _gst_amt,
        "is_service_line": True,
        "status": "PENDING" if _prod_route else "READY",
        "created_at": _dt.datetime.now().isoformat(),
    }


@st.cache_data(ttl=120, show_spinner=False)
def _cached_product_price_snapshot(product_id: str) -> dict:
    """Small product/stock price snapshot for retail product hydration."""
    try:
        from modules.core.price_source_resolver import resolve_db_price

        _resolved = resolve_db_price(str(product_id), "RETAIL", product={"product_id": str(product_id)}, prefer_batch=True)
        if _resolved.get("found"):
            return {
                "gst_percent": float(_resolved.get("gst_percent") or 0),
                "unit": str(_resolved.get("unit") or "PCS"),
                "box_size": int(float(_resolved.get("box_size") or 1)),
                "discount_percent": 0.0,
                "selling_price": float(_resolved.get("selling_price") or 0),
                "mrp": float(_resolved.get("mrp") or 0),
                "purchase_rate": float(_resolved.get("purchase_rate") or 0),
                "price_source": str(_resolved.get("source") or ""),
            }
    except Exception:
        pass

    try:
        _df = execute_query(
            """
            SELECT p.gst_percent,
                   COALESCE(p.unit, 'PCS')             AS unit,
                   GREATEST(COALESCE(p.box_size, 1), 1) AS box_size,
                   COALESCE(p.discount_percent, 0)    AS discount_percent,
                   COALESCE(MAX(i.selling_price), 0)  AS selling_price,
                   COALESCE(MAX(i.mrp), 0)            AS mrp,
                   COALESCE(MAX(i.purchase_rate), 0)  AS purchase_rate
            FROM products p
            LEFT JOIN inventory_stock i
                   ON i.product_id = p.id
                  AND COALESCE(i.is_active, TRUE) = TRUE
            WHERE p.id = %s
            GROUP BY p.gst_percent, p.discount_percent, p.unit, p.box_size
            LIMIT 1
            """,
            "retail_cached_product_snapshot", params=(str(product_id),))
        if _df is not None and not _df.empty:
            _r = _df.iloc[0]
            return {
                "gst_percent": float(_r.get("gst_percent") or 0),
                "unit": str(_r.get("unit") or "PCS"),
                "box_size": int(float(_r.get("box_size") or 1)),
                "discount_percent": float(_r.get("discount_percent") or 0),
                "selling_price": float(_r.get("selling_price") or 0),
                "mrp": float(_r.get("mrp") or 0),
                "purchase_rate": float(_r.get("purchase_rate") or 0),
                "price_source": "product_stock_snapshot",
            }
    except Exception:
        pass
    return {}


def _hydrate_product_gst(product_row: dict) -> dict:
    """Fetch gst_percent + prices from inventory_stock (prices live there, not in products)."""
    pid = product_row.get("product_id")
    if not pid:
        return product_row
    # Cache in session_state to avoid repeated DB queries per session
    cache_key = f"_product_cache_v2_{pid}"
    if cache_key in st.session_state:
        cached = st.session_state[cache_key]
        # Merge cached values into product_row without overwriting existing
        for k, v in cached.items():
            if k not in product_row or not product_row.get(k):
                product_row[k] = v
        return product_row
    snap = _cached_product_price_snapshot(str(pid))
    for k, v in snap.items():
        if k in ("unit", "box_size", "discount_percent", "price_source"):
            product_row[k] = product_row.get(k) or v
        elif not float(product_row.get(k) or 0):
            product_row[k] = v
    if snap:
        st.session_state[cache_key] = snap
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


def _reference_unit_price(product: dict, order_type: str = "RETAIL") -> float:
    """Fallback raw product price from product/inventory for RX or to-order lines."""
    raw = resolve_price_for_order_type(product or {}, order_type)
    if raw <= 0 and product and product.get("product_id"):
        try:
            from modules.core.price_source_resolver import resolve_db_price

            resolved = resolve_db_price(
                str(product.get("product_id")),
                order_type,
                product=product,
                prefer_batch=True,
            )
            raw = float(resolved.get("raw_price") or 0)
        except Exception:
            raw = 0
    return raw if raw > 0 else 0.0


def _oph_spec_for_product(product_id: str) -> dict:
    selected = st.session_state.get("retail_selected_product") or {}
    spec = selected.get("oph_spec") or st.session_state.get(f"_oph_spec_{product_id}") or {}
    return spec if isinstance(spec, dict) and spec.get("complete") else {}


def _oph_spec_pair_price(spec: dict) -> float:
    price = (spec or {}).get("price") or {}
    # Retail ophthalmic billing must use SRP/MRP first.  The selector's
    # ``selling`` field can contain WLP when the same spec object is shared
    # between wholesale and retail paths, so do not let it override SRP here.
    return (
        _safe_price(price.get("srp"))
        or _safe_price(price.get("selling"))
        or _safe_price(price.get("wlp"))
        or 0.0
    )


def _oph_spec_per_lens_price(spec: dict) -> float:
    pair_price = _oph_spec_pair_price(spec)
    return round(pair_price / 2, 2) if pair_price > 0 else 0.0


def _oph_lens_params(spec: dict) -> dict:
    if not spec:
        return {}
    return {
        "lens_index": spec.get("index"),
        "coating": spec.get("coating"),
        "treatment": spec.get("treatment"),
        "display_suffix": spec.get("display_suffix", ""),
        "ophthalmic_price_pair": _oph_spec_pair_price(spec),
        "ophthalmic_price_per_lens": _oph_spec_per_lens_price(spec),
    }

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
        # Clear duplicate mode flags
        st.session_state.pop("_retail_duplicate_mode", None)
        st.session_state.pop("_retail_source_order_no", None)
        # Clear same-power checkbox
        st.session_state.pop("retail_copy_same_rl", None)
        # Clear service-charge form state so a half-added old service does not
        # appear inside the next order.
        st.session_state.pop("_retail_pending_charges", None)
        st.session_state.pop("_sc_add_type", None)
        st.session_state.pop("_sc_add_order_token", None)
        for _k in list(st.session_state.keys()):
            if _k.startswith("sc_"):
                st.session_state.pop(_k, None)
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


def clear_orphan_retail_cart():
    """
    Clear cart/payment state if a restored cart has no active patient context.
    This catches stale carts restored from page switches or hot reloads.
    """
    has_cart = bool(st.session_state.get("retail_order_lines"))
    has_patient = bool(st.session_state.get("retail_patient_id"))
    allowed_context = any(
        st.session_state.get(k)
        for k in (
            "_editing_order_id",
            "_order_edit_prefill",
            "_consult_prefill",
            "_retail_consult_source_id",
            "_editing_consult_order_id",
        )
    )
    if not has_cart or has_patient or allowed_context:
        return

    for key in (
        "retail_order_lines",
        "retail_provisional_order_id",
        "retail_provisional_order_created_at",
        "_persistent_cart",
        "_crash_snapshot",
        "_frozen_payment_total",
    ):
        st.session_state.pop(key, None)

    st.session_state["retail_order_lines"] = []
    st.session_state["retail_patient_name"] = ""
    st.session_state["retail_patient_mobile"] = ""
    st.session_state["retail_case_no"] = ""



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
    Smart duplicate — wholesale-style.
    Restores: patient, power, product, lens/boxing params, authenticity card.
    Clears: cart, order identity, service charges, payment locks.
    Sets _retail_duplicate_mode=True so power is locked and product is changeable.
    """
    _snap = st.session_state.get("_post_save_data") or {}

    def _fmt_rx_str(field, val):
        try:
            fv = float(val)
            if field == "axis": return str(int(fv)) if fv else ""
            if field == "add":  return f"+{fv:.2f}" if fv > 0 else ""
            return f"{fv:+.2f}" if fv else ""
        except Exception: return str(val)

    # 1. Restore power
    for _eye, _rk in (("r", "retail_new_rx_r"), ("l", "retail_new_rx_l")):
        _rx = _snap.get(f"_rx_{_eye}") or {}
        if _rx:
            st.session_state[_rk] = dict(_rx)
            _rc = st.session_state.get("rx_reset_counter", 0)
            for _f in ("sph","cyl","axis","add"):
                _v = _rx.get(_f)
                if _v is not None:
                    st.session_state[f"new_{_f}_{_eye.upper()}_{_rc}"] = _fmt_rx_str(_f, _v)
        st.session_state[f"retail_old_rx_{_eye}"] = {}  # clear old so "use same" doesn't fire

    # 2. Restore patient
    if _snap.get("_patient_name"):
        st.session_state["retail_patient_name"]   = _snap["_patient_name"]
        st.session_state["retail_patient_mobile"] = _snap.get("_patient_mobile","")
        st.session_state["retail_patient_id"]     = _snap.get("_patient_id","")
        st.session_state["retail_case_no"]        = _snap.get("_case_no","")

    # 3. Restore product
    _product = _snap.get("_product")
    if _product:
        st.session_state["retail_selected_product"] = _product
        st.session_state["_product_cache_refreshed"] = True
        st.session_state["reset_product_selector"] = False
    else:
        st.session_state["retail_selected_product"] = None
        st.session_state["reset_product_selector"]  = True

    # 4. Restore lens + boxing params
    _lp = _snap.get("_lens_params")
    st.session_state["retail_lens_params"] = dict(_lp) if _lp else {
        "frame_type":"","thickness":"","corridor":"",
        "diameter":"","fitting_height":"","instructions":"",
    }
    _bp = _snap.get("_boxing_params")
    st.session_state["retail_boxing_params"] = dict(_bp) if _bp else {
        "a_box":None,"b_box":None,"ed":None,"ed_axis":None,"dbl":None,
        "r_pd":None,"l_pd":None,"ipd":None,"fitting_ht_r":None,
        "fitting_ht_l":None,"panto":None,"tilt":None,"bvd":None,
    }

    # 5. Cart + order identity clear
    clear_retail_cart_completely(set_consult_removed=True)

    # 6. Mark duplicate mode
    st.session_state["_retail_duplicate_mode"] = True

