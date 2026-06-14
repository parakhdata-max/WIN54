"""
modules/retail_punching_ui.py
==============================
Order line UI, billing finalisation, receipts,
and the render_retail_punching entry point.
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
from decimal import Decimal, ROUND_HALF_UP
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
    from modules.wholesale_punching import (
        _make_service_cart_line,
        _service_master_rows,
        _service_ui_meta,
        _uploaded_image_b64,
    )
except Exception:
    _make_service_cart_line = None
    def _service_master_rows(order_type: str = "RETAIL", party_id: str = "") -> list:
        return []
    def _service_ui_meta(service_group: str) -> dict:
        return {
            "label": str(service_group or "Service").title(),
            "icon": "🧾",
            "color": "#64748b",
            "default_gst": 18,
        }
    def _uploaded_image_b64(uploaded_file) -> str:
        return ""


def _retail_cash_round(value) -> float:
    """Retail counter collection is always nearest rupee, never paise."""
    try:
        return float(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return float(round(float(value or 0)))


def _retail_line_payable_total(lines) -> float:
    """Customer-payable total for retail punching, rounded to whole rupee."""
    try:
        raw = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in (lines or []))
        return _retail_cash_round(raw)
    except Exception:
        return 0.0
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
from modules import retail_punching_rx as _retail_rx_mod
for _retail_mod in (_retail_data_mod, _retail_rx_mod):
    globals().update({k: v for k, v in vars(_retail_mod).items() if not k.startswith("__")})

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
            if raw_pcs <= 0 and int(allocation.get("available_qty") or 0) > 0:
                raw_pcs = 1

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

            # Streamlit keeps number_input values by key across reruns. When a
            # previous contact-lens line left retail_qe_be_R_box=1, the batch
            # editor could ignore the new main quantity (for example 4 boxes)
            # and silently finalize 1 box. Seed the editor keys once per
            # allocation line/eye so the batch editor mirrors the quantity
            # chosen on the first screen.
            _be_seed_id = f"{allocation.get('line_id')}:{allocation.get('eye_side')}:{raw_pcs}"
            _be_seed_key = f"_retail_be_qty_seed_{allocation.get('eye_side')}"
            if st.session_state.get(_be_seed_key) != _be_seed_id:
                st.session_state[f"retail_qe_be_{allocation['eye_side']}_box"] = int(box)
                st.session_state[f"retail_qe_be_{allocation['eye_side']}_pcs"] = int(pcs)
                st.session_state[f"retail_qe_be_{allocation['eye_side']}_pair"] = float(pair)
                st.session_state[_be_seed_key] = _be_seed_id

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
                    _pcs_max = max(0, int(allocation.get('available_qty') or 0))
                    _pcs_val = min(max(0, int(pcs or 0)), _pcs_max)
                    if _pcs_max > 0 and _pcs_val <= 0:
                        _pcs_val = 1
                    _pcs_key = f"retail_qe_be_{allocation['eye_side']}_pcs"
                    if _pcs_max > 0 and int(st.session_state.get(_pcs_key, 0) or 0) <= 0:
                        st.session_state[_pcs_key] = _pcs_val
                    _pcs_min = 1 if _pcs_max > 0 and engine.mode in ["PCS_ONLY", "NO_ONLY"] else 0
                    user_input["pcs"] = st.number_input(
                        "Qty (PCS)",
                        min_value=_pcs_min,
                        max_value=_pcs_max,
                        value=max(_pcs_min, _pcs_val),
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
            batch_quantities = []
            remaining = punched_qty

            for idx, batch in enumerate(batches):
                available_qty = int(batch['available_qty'])
                default_qty = min(int(remaining), available_qty)
                batch_quantities.append({
                    'batch_no': batch['batch_no'],
                    'batch_id': batch.get('batch_id'),
                    'expiry_date': str(batch['expiry_date']),
                    'allocated_qty': default_qty,
                    'available_qty': batch['available_qty'],
                    # ✅ RETAIL: mrp first (resolver), then allocation fallback
                    'selling_price': resolve_price_for_order_type(batch, "RETAIL") or allocation.get('unit_price', 0),
                    # ✅ FIX: unit+box_size so compute_weighted_price normalises BOX→PCS
                    'unit':     str(product.get('unit', '') or ''),
                    'box_size': int(product.get('box_size', 0) or 0),
                })
                remaining -= default_qty

            _auto_allocated = sum(int(b.get('allocated_qty') or 0) for b in batch_quantities)
            _used_batches = [b for b in batch_quantities if int(b.get('allocated_qty') or 0) > 0]
            _batch_label = ", ".join(
                f"{b.get('batch_no') or 'Batch'}: {int(b.get('allocated_qty') or 0)}"
                for b in _used_batches[:3]
            )
            if len(_used_batches) > 3:
                _batch_label += f" +{len(_used_batches)-3} more"

            st.caption(
                f"📦 FIFO allocation: {_auto_allocated}/{punched_qty} pcs"
                + (f" · {_batch_label}" if _batch_label else "")
            )

            with st.expander("Edit batch allocation", expanded=False):
                st.caption(f"Only open this when you need to override FIFO allocation across {len(batches)} batch(es).")
                remaining = punched_qty
                edited_batches = []

                for idx, batch in enumerate(batches):
                    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])

                    with col1:
                        st.write(f"**Batch {idx+1}**")
                        st.caption(f"No: {batch['batch_no']}")

                    with col2:
                        available = int(batch['available_qty'])
                        display_text = format_quantity_display(available, product)
                        st.write(f"**Available:** {display_text}")
                        st.caption(f"Exp: {batch['expiry_date']}")

                    with col3:
                        available_qty = int(batch['available_qty'])
                        default_qty = min(int(remaining), available_qty)
                        batch_qty = st.number_input(
                            "Allocate Qty",
                            min_value=0,
                            max_value=min(available_qty, punched_qty),
                            value=default_qty,
                            step=1,
                            key=f"batch_qty_{allocation['line_id']}_{allocation['eye_side']}_{idx}",
                            help=f"Allocate from this batch (max: {available_qty})"
                        )
                        edited_batches.append({
                            'batch_no': batch['batch_no'],
                            'batch_id': batch.get('batch_id'),
                            'expiry_date': str(batch['expiry_date']),
                            'allocated_qty': batch_qty,
                            'available_qty': batch['available_qty'],
                            'selling_price': resolve_price_for_order_type(batch, "RETAIL") or allocation.get('unit_price', 0),
                            'unit':     str(product.get('unit', '') or ''),
                            'box_size': int(product.get('box_size', 0) or 0),
                        })
                        remaining -= batch_qty

                    with col4:
                        if batch_qty > 0:
                            st.success(f"✓ {batch_qty}")
                        else:
                            st.caption("Skip")

                batch_quantities = edited_batches

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
                    if next_eye != allocation.get("eye_side"):
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
                            if st.button("🔒 OK — keep existing (remove from cart to change)",
                                         width="stretch", key="fin_dup_block2",
                                         type="primary"):
                                clear_allocation_state()
                                st.session_state.retail_pending_eyes = []
                                st.rerun()
                            st.stop()

                        enter_to_submit()
                        if st.button("✅ Finalize & Add to Cart  [Enter]", type="primary", width='stretch', key="finalize_add_to_cart_active"):

                            # Create provisional order if needed
                            provisional_id = create_provisional_order()

                            added_current = _finalize_retail_stock_allocation_to_cart(
                                allocation, product, batch_quantities, punched_qty, provisional_id
                            )
                            if not added_current:
                                st.error("❌ Could not add this eye. Check price and allocation.")
                                st.stop()

                            # Remove the current eye from the front if it is still queued.
                            if (
                                st.session_state.get("retail_pending_eyes")
                                and st.session_state.retail_pending_eyes[0] == allocation.get("eye_side")
                            ):
                                st.session_state.retail_pending_eyes.pop(0)

                            # Clear current allocation before auto-processing the next queued eye.
                            clear_allocation_state()
                            auto_added = _auto_finalize_pending_stock_eyes(provisional_id)

                            # ✅ AUTO MERGE CART (prevents duplicate lines instantly)
                            try:
                                st.session_state.retail_order_lines = merge_order_lines(
                                    st.session_state.retail_order_lines,
                                    party_id="", order_type="RETAIL"
                                )
                            except Exception:
                                pass  # Malformed legacy line — keep cart intact

                            if auto_added:
                                st.success(f"✅ Added {auto_added + 1} eye(s) to cart successfully!")
                            else:
                                st.success("✅ Added to cart successfully!")

                            # If a pending eye could not be auto-finalized, keep its editor open.
                            if not st.session_state.get("retail_pending_eyes"):
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
                # ── Guard: one-shot flag prevents triple-add on Streamlit reruns.
                # Key = product_id + eye_side + power (stable across reruns).
                _rx_key = ("_rx_added_"
                    + str(allocation.get("product_id",""))
                    + str(allocation.get("eye_side",""))
                    + str(allocation.get("sph",""))
                    + str(allocation.get("cyl",""))
                    + str(allocation.get("axis","")))
                if st.session_state.get(_rx_key):
                    # Already added this product+eye+power in this session.
                    # Clear allocation and move to next eye if any.
                    clear_allocation_state()
                    _has_more = bool(st.session_state.get("retail_pending_eyes"))
                    if _has_more:
                        _next = st.session_state.retail_pending_eyes.pop(0)
                        prepare_allocation(_next)
                    st.rerun()
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
                # Ophthalmic RX: 1 pc per eye always (pair = R + L)
                rx_qty = 1
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
                    'lens_params':          {
                        **dict(st.session_state.retail_lens_params or {}),
                        **_oph_lens_params(allocation.get('oph_spec') or {}),
                    },
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
                # Set flag so reruns skip this block for this product+eye+power
                st.session_state[_rx_key] = True
                st.success(f"✅ RX lens auto-added to cart ({rx_qty} pc · ₹{total_price:,.2f})")

            else:
                # ── AUTO: No stock / unknown — add as Pending, backoffice raises PO ──
                req_qty = int(allocation.get('requested_qty', 0))
                # Guard: stable key prevents re-add on reruns.
                _pend_key = ("_pend_added_"
                    + str(allocation.get("product_id",""))
                    + str(allocation.get("eye_side",""))
                    + str(allocation.get("sph",""))
                    + str(allocation.get("cyl","")))
                if st.session_state.get(_pend_key):
                    clear_allocation_state()
                    st.rerun()
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
                    'lens_params':          {
                        **dict(st.session_state.retail_lens_params or {}),
                        **_oph_lens_params(allocation.get('oph_spec') or {}),
                    },
                    'boxing_params':        dict(st.session_state.retail_boxing_params or {}),
                    'requested_qty':        req_qty,
                    'billing_qty':          0,
                    'order_qty':            req_qty,
                    'display_qty':          format_quantity_display(req_qty, product),
                    'batch_allocation':     [],
                    'unit_price':           allocation.get('unit_price', 0),
                    'total_price':          round(float(allocation.get('unit_price') or 0) * req_qty, 2),
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
                st.session_state[_pend_key] = True
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
            _cart_total = sum(float(l.get("total_price") or 0) for l in st.session_state.get("retail_order_lines", []))
            _cart_count = len(st.session_state.get("retail_order_lines", []))
            st.markdown(f"### 🛒 Current Order · {_cart_count} line(s) · {_fmt_mrp(_cart_total)}")

        with col2:
            if st.session_state.get("retail_provisional_order_created_at"):
                created = datetime.datetime.fromisoformat(st.session_state.retail_provisional_order_created_at)
                st.caption(f"Started {created.strftime('%I:%M %p')} · ID {str(st.session_state.retail_provisional_order_id)[:8]}…")

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
    _line_total = float(line.get("billing_total") or line.get("total_price") or 0)
    _pname_display = _line_product_with_spec(line)
    with st.expander(
        f"{idx}. {line['brand']} - {_pname_display} | "
        f"Qty: {line.get('display_qty', line['billing_qty'])} | "
        f"{_fmt_mrp(_line_total)} | {line['status']}",
        expanded=False
    ):
        col1, col2, col3 = st.columns([3, 2, 1])

        with col1:
            st.write(f"**Product:** {_pname_display}")
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
                    clear_retail_cart_completely(set_consult_removed=True)

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


def _merge_retail_pending_services_into_cart() -> None:
    """Make staged fitting/colouring/courier charges real cart lines before save."""
    if _make_service_cart_line is None:
        return
    try:
        pending = st.session_state.get("_retail_pending_charges") or []
        if not pending:
            return
        cart = list(st.session_state.get("retail_order_lines") or [])
        cart_line_ids = {str(l.get("line_id") or l.get("id") or "") for l in cart}
        cart_service_codes = set()
        for line in cart:
            lens_params = line.get("lens_params") or {}
            if isinstance(lens_params, str):
                try:
                    import json as _ret_svc_json
                    lens_params = _ret_svc_json.loads(lens_params)
                except Exception:
                    lens_params = {}
            if (
                bool(line.get("is_service_line"))
                or str(line.get("eye_side", "")).upper().strip() in ("S", "SERVICE")
            ):
                cart_service_codes.add(
                    str(
                        lens_params.get("service_code")
                        or lens_params.get("charge_type")
                        or lens_params.get("service_type")
                        or ""
                    ).upper()
                )
        for charge in pending:
            charge_line_id = str(charge.get("line_id") or "")
            charge_code = str(charge.get("service_code") or charge.get("type") or "").upper()
            if (
                (charge_line_id and charge_line_id in cart_line_ids)
                or (charge_code and charge_code in cart_service_codes)
            ):
                continue
            svc_line = _make_service_cart_line(charge, "RETAIL")
            cart.append(svc_line)
            cart_line_ids.add(str(svc_line.get("line_id") or ""))
            cart_service_codes.add(str((svc_line.get("lens_params") or {}).get("service_code") or "").upper())
        st.session_state.retail_order_lines = cart
    except Exception:
        pass


def finalize_retail_order_to_backoffice():
    """Convert retail cart to backoffice order"""
    _merge_retail_pending_services_into_cart()
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

    _raw_total_billing = sum(
        float(line.get("billing_total") or line.get("total_price") or 0)
        for line in st.session_state.retail_order_lines
    )
    total_billing = _retail_cash_round(_raw_total_billing)
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
        _case_txt = f" · Case {st.session_state.retail_case_no}" if st.session_state.get("retail_case_no") else ""
        st.caption(
            f"Patient: {st.session_state.retail_patient_name} · "
            f"{st.session_state.retail_patient_mobile}{_case_txt}"
        )

    # ── Patient ID Card (Evolis CR80 + TSC 75×50 sticker) ────────────────────
    with st.expander("💳 Patient ID Card — Evolis card / TSC 75×50 sticker", expanded=False):
        try:
            from modules.printing.patient_card_printer import render_patient_card_for_order
            render_patient_card_for_order(key_prefix="hdr")
        except Exception as _pc_hdr_ex:
            st.caption(f"Patient card unavailable: {_pc_hdr_ex}")

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

    # Use gst_amount from lines (already stamped on net after discount)
    _gst_total_pre = sum(float(l.get("gst_amount") or 0) for l in st.session_state.retail_order_lines)
    _base_pre = _raw_total_billing - _gst_total_pre

    st.markdown(f"### 🧾 Provisional Summary · {_fmt_mrp(total_billing)}")
    with st.expander(
        f"{total_items} line{'s' if total_items!=1 else ''} · Base ₹{_base_pre:,.2f} · GST ₹{_gst_total_pre:,.2f} · open details",
        expanded=False
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
                _rc[5].write(f"{_gp:.0f}%"); _rc[6].write(f"₹{_ga:,.2f}"); _rc[7].write(_fmt_mrp(_tot))
                _del_sum_key = f"sum_del_{_l['line_id']}_{_l.get('eye_side','X')}_{_l.get('sph','')}"
                if _rc[8].button("🗑️", key=_del_sum_key, help="Remove this line"):
                    push_undo()
                    if str(_l.get("eye_side","")).upper() in ("SERVICE","S") or bool(_l.get("is_service_line")):
                        st.session_state["_consult_fee_removed"] = True
                    st.session_state.retail_order_lines = [
                        _x for _x in st.session_state.retail_order_lines
                        if not (_x['line_id'] == _l['line_id'] and _x.get('eye_side') == _l.get('eye_side'))
                    ]
                    if not st.session_state.get("retail_order_lines"):
                        clear_retail_cart_completely(set_consult_removed=True)
                    st.rerun()
        st.markdown("---")
        _fc=st.columns([1,4,2,2,2,1,2,2,1])
        _fc[1].markdown("**TOTAL**"); _fc[6].markdown(f"**₹{_gst_total_pre:,.2f}**"); _fc[7].markdown(f"**{_fmt_mrp(total_billing)}**")
        st.caption(f"Base ₹{_base_pre:,.2f}  ·  GST ₹{_gst_total_pre:,.2f}  ·  Grand Total {_fmt_mrp(total_billing)}")

    _mc=st.columns(3)
    _mc[0].metric("Total Items",total_items)
    _mc[1].metric("Base Value (excl. GST)",f"₹{_base_pre:,.2f}")
    _mc[2].metric("Grand Total (MRP)", _fmt_mrp(total_billing))
    st.caption(f"Provisional ID: {st.session_state.retail_provisional_order_id}")

    # ── GST slab breakup ─────────────────────────────────────────────────
    _gst_slabs = {}
    for _gl in st.session_state.retail_order_lines:
        _gp = float(_gl.get("gst_percent") or 0)
        _ga = float(_gl.get("gst_amount") or 0)
        _gt = float(_gl.get("billing_total") or _gl.get("total_price") or 0)
        _gst_slabs.setdefault(_gp, {"taxable": 0.0, "gst": 0.0, "total": 0.0})
        _gst_slabs[_gp]["gst"] += _ga
        _gst_slabs[_gp]["total"] += _gt
        _gst_slabs[_gp]["taxable"] += max(_gt - _ga, 0.0)
    if _gst_slabs:
        with st.expander("📊 GST Breakup", expanded=True):
            _gh = st.columns([1, 2, 2, 2])
            for _c, _t in zip(_gh, ["GST %", "Taxable", "GST", "Total"]):
                _c.markdown(f"**{_t}**")
            for _gp in sorted(_gst_slabs):
                _s = _gst_slabs[_gp]
                _gr = st.columns([1, 2, 2, 2])
                _gr[0].write(f"{_gp:g}%")
                _gr[1].write(f"₹{_s['taxable']:,.2f}")
                _gr[2].write(f"₹{_s['gst']:,.2f}")
                _gr[3].write(f"₹{_s['total']:,.2f}")

    st.info(
        "ℹ️ **Order total includes product and service lines currently in the cart.** "
        "Any later Backoffice charge edit will reflect in the final challan/invoice."
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
    _consult_existing_adv = float(st.session_state.get("_consult_paid_advance_amount") or 0)
    _consult_existing_mode = str(st.session_state.get("_consult_paid_advance_mode") or "").upper()
    _consult_existing_ref = str(st.session_state.get("_consult_paid_advance_ref") or "").strip()
    _existing_adv = max(float(st.session_state.get("_edit_existing_advance") or 0), _consult_existing_adv)
    _is_edit_mode = bool(st.session_state.get("_editing_order_id",""))

    # ── Consultation advance badge ─────────────────────────────────────────
    # When patient came from consultation, show the consultation receipt clearly
    # so staff knows ₹400 is already recorded and should NOT be collected again.
    _consult_src = st.session_state.get("_consult_prefill", {})
    _consult_ono = _consult_src.get("consult_order_id","") or st.session_state.get("_retail_consult_source_id","")
    # _consult_paid=True means consultation receipt was already settled independently.
    # In that case do NOT show advance badge — the ₹400 is done, retail starts clean.
    _consult_already_settled = bool(_consult_src.get("consult_paid", False))
    if _consult_existing_adv > 0 and _consult_ono and not _consult_already_settled:
        st.markdown(
            f"<div style='background:#052e16;border:1px solid #22c55e;"
            f"border-radius:7px;padding:9px 14px;margin-bottom:8px;"
            f"display:flex;justify-content:space-between;align-items:center'>"
            f"<div>"
            f"<div style='color:#4ade80;font-weight:700;font-size:0.82rem'>"
            f"✅ Consultation advance already paid</div>"
            f"<div style='color:#86efac;font-size:0.72rem;margin-top:2px'>"
            f"Will be deducted from order balance · no double collection"
            f"{' · ' + _consult_existing_mode if _consult_existing_mode else ''}"
            f"{' · Ref ' + _consult_existing_ref if _consult_existing_ref else ''}</div>"
            f"</div>"
            f"<div style='color:#4ade80;font-size:1.1rem;font-weight:900'>"
            f"₹{_consult_existing_adv:,.0f}</div>"
            f"</div>",
            unsafe_allow_html=True
        )
    elif _consult_ono and _consult_existing_adv == 0 and not _consult_already_settled:
        # Consultation fee was set to "charge to billing" — not yet collected
        _consult_fee_pending = float(_consult_src.get("consult_fee", 0) or 0)
        if _consult_fee_pending > 0:
            st.markdown(
                f"<div style='background:#0d0a1e;border:1px solid #6366f1;"
                f"border-radius:7px;padding:9px 14px;margin-bottom:8px;"
                f"display:flex;justify-content:space-between;align-items:center'>"
                f"<div>"
                f"<div style='color:#818cf8;font-weight:700;font-size:0.82rem'>"
                f"🩺 Consultation fee pending collection</div>"
                f"<div style='color:#a5b4fc;font-size:0.72rem;margin-top:2px'>"
                f"Collect ₹{_consult_fee_pending:,.0f} consultation fee as part of advance below</div>"
                f"</div>"
                f"<div style='color:#818cf8;font-size:1.1rem;font-weight:900'>"
                f"₹{_consult_fee_pending:,.0f}</div>"
                f"</div>",
                unsafe_allow_html=True
            )
            # Auto-seed the advance input to include consultation fee
            if not st.session_state.get("retail_advance_amount"):
                st.session_state["retail_advance_amount"] = _consult_fee_pending
                st.session_state["retail_collect_advance"] = True

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
            f"{_fmt_mrp(_existing_adv)}</span></div>",
            unsafe_allow_html=True
        )

    _adv_c0, _adv_c1 = st.columns([1, 3])
    with _adv_c0:
        _adv_collect_label = "💰 Collect Additional Payment" if _existing_adv > 0 else "💰 Collect Advance"
        # Use value= only, not key= — avoids conflict when session state sets it from prefill
        _adv_collect_default = bool(st.session_state.get("retail_collect_advance", False))
        _adv_collect = st.checkbox(_adv_collect_label, value=_adv_collect_default)
        st.session_state["retail_collect_advance"] = _adv_collect
    with _adv_c1:
        _remaining = max(total_billing - _existing_adv, 0)
        if _is_edit_mode and _existing_adv > 0:
            st.metric("Order Total (MRP)", _fmt_mrp(total_billing),
                      delta=f"-{_fmt_mrp(_existing_adv)} already paid",
                      delta_color="normal")
        else:
            st.metric("Order Total (MRP)", _fmt_mrp(total_billing))

    if _adv_collect:
        _ac1, _ac2, _ac3 = st.columns([1.5, 1.2, 1.5])
        with _ac1:
            _max_adv = max(float(total_billing) - _existing_adv, 0.0) if total_billing else 999999.0
            # Clamp current value to max before passing to widget
            _raw_adv_val = _retail_cash_round(st.session_state.get("retail_advance_amount") or 0.0)
            # In edit mode the session value might be the existing advance (already paid)
            # — cap it so new-advance input starts at 0 if existing covers it
            if _existing_adv > 0:
                _raw_adv_val = min(_raw_adv_val, _max_adv)
            _safe_adv_val = _retail_cash_round(min(_raw_adv_val, _max_adv if _max_adv > 0 else 999999.0))
            _label = "Additional Advance ₹" if _existing_adv > 0 else "Advance Amount ₹"
            _adv_amt = st.number_input(
                _label,
                min_value=0.0,
                max_value=max(_max_adv, 0.01),  # never pass 0 as max
                value=_safe_adv_val,
                step=1.0,
                format="%.0f",
                help="Maximum = Order Total minus already paid advance."
            )
            _adv_amt = _retail_cash_round(_adv_amt)
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
        _adv_balance = max(_retail_cash_round(total_billing - _total_paid), 0)
        st.markdown(
            f"<div style='background:#0a0f1a;border-radius:6px;padding:8px 14px;margin-top:4px;"
            f"display:flex;gap:28px'>"
            + (f"<span style='color:#4ade80;font-size:0.8rem'>"
               f"<b>Prev paid:</b> {_fmt_mrp(_existing_adv)}</span>" if _existing_adv > 0 else "")
            + f"<span style='color:#8b5cf6;font-size:0.8rem'>"
            f"<b>New advance:</b> ₹{(_adv_amt or 0):,.2f}</span>"
            f"<span style='color:#f59e0b;font-size:0.8rem'>"
            f"<b>Balance on Delivery:</b> {_fmt_mrp(_adv_balance)}</span>"
            f"</div>",
            unsafe_allow_html=True
        )
    else:
        if not _is_edit_mode:
            for _k in ("retail_advance_amount","retail_advance_mode","retail_advance_ref"):
                st.session_state.pop(_k, None)
        _adv_amt = 0.0
        if _existing_adv > 0:
            _balance = max(_retail_cash_round(total_billing - _existing_adv), 0)
            st.markdown(
                f"<div style='color:#94a3b8;font-size:0.8rem;padding:4px 0'>"
                f"Balance on delivery: {_fmt_mrp(_balance)} "
                f"(after {_fmt_mrp(_existing_adv)} already paid)</div>",
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
    _adv_check = _retail_cash_round(st.session_state.get("retail_advance_amount") or 0)
    _tot_check = _retail_line_payable_total(st.session_state.retail_order_lines)
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
        _last_receipt_key = st.session_state.get("_last_receipt_key", "")
        _snap_for_btn = (
            st.session_state.get("_receipt_snapshot") or
            (st.session_state.get(_last_receipt_key) if _last_receipt_key else None) or
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
            from modules.sql_adapter import run_query as _rq_pre, resolve_order_uuid as _resolve_order_uuid
            _edit_oid_pre_uuid = _resolve_order_uuid(_edit_oid_pre) or _resolve_order_uuid(_edit_ono_pre)
            if not _edit_oid_pre_uuid:
                raise ValueError(f"Could not resolve edit order reference: {_edit_oid_pre or _edit_ono_pre}")
            _orig = _rq_pre("""
                SELECT p.product_name, ol.eye_side, ol.sph, ol.cyl, ol.quantity, ol.unit_price,
                       ol.total_price
                FROM order_lines ol LEFT JOIN products p ON p.id=ol.product_id
                WHERE ol.order_id=%s::uuid AND COALESCE(ol.is_deleted,false)=false
            """, (_edit_oid_pre_uuid,)) or []
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
    _fa_blocked = (
        st.session_state.get("_rx_field_error_R", False)
        or st.session_state.get("_rx_field_error_L", False)
    )
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
            _fa_total = _retail_line_payable_total(st.session_state.get("retail_order_lines", []))
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
                _fa_total = _retail_line_payable_total(st.session_state.get("retail_order_lines", []))
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
                    advance_amount = _retail_cash_round(st.session_state.get("retail_advance_amount") or 0),
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
                _edit_existing_adv  = _retail_cash_round(st.session_state.get("_edit_existing_advance") or 0)
                _edit_new_adv       = _retail_cash_round(st.session_state.get("retail_advance_amount") or 0) if st.session_state.get("retail_collect_advance") else 0.0
                _edit_total_advance = _retail_cash_round(_edit_existing_adv + _edit_new_adv)
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
                st.session_state["_show_receipt_top"] = True
                if do_print:
                    st.session_state["_auto_print_receipt"] = True
                st.success(f"✅ Order {_edit_mode_ono} updated — changes logged")
                st.balloons()

                _edit_existing_adv2  = _retail_cash_round(st.session_state.get("_edit_existing_advance") or 0)
                _edit_new_adv2       = _retail_cash_round(st.session_state.get("retail_advance_amount") or 0) if st.session_state.get("retail_collect_advance") else 0.0
                _edit_total_adv2     = _retail_cash_round(_edit_existing_adv2 + _edit_new_adv2)
                _edit_grand2         = _retail_line_payable_total(st.session_state.get("retail_order_lines",[]))
                # Fix: read mobile from saved order in DB to avoid sticky session state number
                _edit_mob_verified = st.session_state.get("retail_patient_mobile","")
                try:
                    from modules.sql_adapter import run_query as _rq_em
                    _em_row = _rq_em(
                        "SELECT COALESCE(patient_mobile,'') AS m FROM orders WHERE order_no=%s LIMIT 1",
                        (_edit_mode_ono,)
                    ) or []
                    if _em_row:
                        _db_em = str(_em_row[0].get("m","") or "").strip()
                        if _db_em:
                            _edit_mob_verified = _db_em
                            st.session_state["retail_patient_mobile"] = _db_em
                except Exception:
                    pass
                st.session_state["_post_save_data"] = {
                    "order_no":      _edit_mode_ono,
                    "party_name":    st.session_state.get("retail_patient_name",""),
                    "mobile":        _edit_mob_verified,
                    "total":         _edit_grand2,
                    "advance":       _edit_total_adv2,
                    "order_type":    "RETAIL",
                    "delivery":      st.session_state.get("retail_delivery_date",""),
                    "lines":         list(st.session_state.get("retail_order_lines", [])),
                    # Duplicate snapshot
                    "_patient_name":  st.session_state.get("retail_patient_name",""),
                    "_patient_mobile":st.session_state.get("retail_patient_mobile",""),
                    "_patient_id":    st.session_state.get("retail_patient_id",""),
                    "_case_no":       st.session_state.get("retail_case_no",""),
                    "_rx_r":          dict(st.session_state.get("retail_new_rx_r") or {}),
                    "_rx_l":          dict(st.session_state.get("retail_new_rx_l") or {}),
                    "_product":       st.session_state.get("retail_selected_product"),
                    "_lens_params":   dict(st.session_state.get("retail_lens_params") or {}),
                    "_boxing_params": dict(st.session_state.get("retail_boxing_params") or {}),
                    "status_label":   "RECEIVED",
                }
                _receipt_restore = copy.deepcopy(_receipt_data)
                _post_save_restore = copy.deepcopy(st.session_state.get("_post_save_data") or {})
                _auto_print_restore = bool(do_print)

                # Reset after edit — keep receipt snapshot
                st.session_state.last_order_snapshot = list(
                    st.session_state.get("retail_order_lines",[])
                )
                industrial_reset("ALL")
                st.session_state["_receipt_snapshot"] = _receipt_restore
                st.session_state[_ono_key2] = _receipt_restore
                st.session_state["_last_receipt_key"] = _ono_key2
                st.session_state["_show_receipt_top"] = True
                if _post_save_restore:
                    st.session_state["_post_save_data"] = _post_save_restore
                if _auto_print_restore:
                    st.session_state["_auto_print_receipt"] = True
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
                # Verify mobile from patients table when we have a patient UUID.
                # Session state can hold a stale number from a previous consultation patient.
                if _pt_id and len(str(_pt_id)) > 10:
                    try:
                        from modules.sql_adapter import run_query as _rq_ptmob
                        _ptmob_row = _rq_ptmob(
                            "SELECT COALESCE(mobile,'') AS m FROM patients WHERE id=%s::uuid LIMIT 1",
                            (str(_pt_id),)
                        ) or []
                        if _ptmob_row:
                            _db_ptmob = str(_ptmob_row[0].get("m","") or "").strip()
                            if _db_ptmob:
                                _pt_mobile = _db_ptmob
                                st.session_state["retail_patient_mobile"] = _db_ptmob
                    except Exception:
                        pass
                order_info = {
                    "order_type":           "RETAIL",
                    "order_source":         "RETAIL",
                    # ── party fields — used by ALL validators ──
                    "party":                _pt_name,          # PartyValidator + NO_PATIENT
                    "party_name":           _pt_name,          # OrderValidator MISSING_FIELDS
                    "party_id":             _pt_id,            # DB FK + advance payment link
                    "patient_name":         _pt_name,
                    "patient_mobile":       _pt_mobile,
                    # ── order identity ──
                    "provisional_order_id": _prov_oid,         # _build_order_data → order_id
                    # For consultation-to-billing, this must be the consultation
                    # order UUID. convert_consultation_to_billing() and the
                    # converted guard use this exact link to prevent duplicate
                    # billing and to show the consultation as shifted.
                    "customer_order_no":    (
                        st.session_state.get("_retail_consult_source_id")
                        or st.session_state.get("retail_case_no", "")
                    ),
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
                    _lp_cl = _cl.get("lens_params") or {}
                    if isinstance(_lp_cl, str):
                        try:
                            import json as _ret_dedupe_json
                            _lp_cl = _ret_dedupe_json.loads(_lp_cl)
                        except Exception:
                            _lp_cl = {}
                    _is_svc_cl = (
                        bool(_cl.get("is_service_line"))
                        or str(_cl.get("eye_side","")).upper().strip() in ("S", "SERVICE")
                    )
                    if _is_svc_cl:
                        _cl_key = (
                            "SERVICE",
                            str(_lp_cl.get("service_code") or _lp_cl.get("charge_type") or _lp_cl.get("service_type") or _cl.get("line_id") or _cl.get("id") or "").upper(),
                            str(_cl.get("line_id") or _cl.get("id") or ""),
                        )
                    else:
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
                try:
                    from modules.backoffice.backoffice_helpers import ensure_order_production_refs
                    _res_oid_ref = result.get("order_id") or {}
                    _real_oid_ref = (
                        str(_res_oid_ref.get("order_db_id", ""))
                        if isinstance(_res_oid_ref, dict) else str(_res_oid_ref or "")
                    )
                    ensure_order_production_refs(order_no=_r_ono, order_id=_real_oid_ref)
                except Exception as _prod_ref_new_err:
                    import logging as _prod_ref_new_log
                    _prod_ref_new_log.getLogger(__name__).warning(
                        "Retail save production_ref fill failed for %s: %s",
                        _r_ono, _prod_ref_new_err,
                    )
                _r_party= st.session_state.get("retail_patient_name","")
                # Fix: read mobile from the saved order in DB, not from session state.
                # session state can still hold the previous patient's number when
                # coming from a consultation flow where _erp_patient_mob was not cleared.
                _r_mob  = st.session_state.get("retail_patient_mobile","")
                try:
                    from modules.sql_adapter import run_query as _rq_mob_fix
                    _mob_fix_row = _rq_mob_fix(
                        "SELECT COALESCE(patient_mobile,'') AS m FROM orders WHERE order_no=%s LIMIT 1",
                        (_r_ono,)
                    ) or []
                    if _mob_fix_row:
                        _db_mob = str(_mob_fix_row[0].get("m","") or "").strip()
                        if _db_mob:
                            _r_mob = _db_mob
                            st.session_state["retail_patient_mobile"] = _db_mob
                except Exception:
                    pass
                _r_adv  = (
                    _retail_cash_round(st.session_state.get("_consult_paid_advance_amount") or 0)
                    + (_retail_cash_round(st.session_state.get("retail_advance_amount") or 0) if st.session_state.get("retail_collect_advance") else 0.0)
                )
                _r_del  = str(st.session_state.get("retail_delivery_date",""))
                _r_tot  = _retail_line_payable_total(st.session_state.get("retail_order_lines",[]))
                st.success(f"✅ Order Received: {_r_ono} — Under Review")
                st.balloons()

                # ── Duplicate audit trail ─────────────────────────────────
                if st.session_state.get("_retail_duplicate_mode"):
                    try:
                        from modules.sql_adapter import run_write as _rw_dup
                        _src_ono_d = st.session_state.pop("_retail_source_order_no", "") or ""
                        _remarks_d = (
                            f"Duplicate of order {_src_ono_d}"
                            if _src_ono_d else "Punched as duplicate order"
                        )
                        _rw_dup("""
                            INSERT INTO order_status_history
                                (order_id, from_status, to_status, changed_by_name, remarks)
                            SELECT id, 'PUNCHED_DUPLICATE', 'UNDER_REVIEW',
                                   %(by)s, %(remarks)s
                            FROM orders WHERE order_no = %(ono)s LIMIT 1
                        """, {
                            "ono":     _r_ono,
                            "by":      st.session_state.get("user_name","Staff"),
                            "remarks": _remarks_d,
                        })
                    except Exception:
                        pass  # audit failure must never block order save
                # Store post-save context — rendered after rerun from _receipt_snapshot area
                st.session_state["_post_save_data"] = {
                    "order_no":       _r_ono,
                    "party_name":     _r_party,
                    "mobile":         _r_mob,
                    "total":          _r_tot,
                    "advance":        _r_adv,
                    "order_type":     "RETAIL",
                    "delivery":       _r_del,
                    "lines":          list(st.session_state.get("retail_order_lines", [])),
                    # Duplicate snapshot
                    "_patient_name":  st.session_state.get("retail_patient_name",""),
                    "_patient_mobile":st.session_state.get("retail_patient_mobile",""),
                    "_patient_id":    st.session_state.get("retail_patient_id",""),
                    "_case_no":       st.session_state.get("retail_case_no",""),
                    "_rx_r":          dict(st.session_state.get("retail_new_rx_r") or {}),
                    "_rx_l":          dict(st.session_state.get("retail_new_rx_l") or {}),
                    "_product":       st.session_state.get("retail_selected_product"),
                    "_lens_params":   dict(st.session_state.get("retail_lens_params") or {}),
                    "_boxing_params": dict(st.session_state.get("retail_boxing_params") or {}),
                    "status_label":   "RECEIVED",
                }

                # ── PERMANENT DEDUPE STAMP ────────────────────────────────────
                # Mark this provisional cart as confirmed. Button stays disabled
                # even if lock expires, even across rerenders.
                if _cart_fp:
                    st.session_state[_cart_fp] = str(result.get("order_no", "confirmed"))

                # ── Record advance if collected ───────────────────────────────
                _adv_amount = _retail_cash_round(st.session_state.get("retail_advance_amount") or 0)
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
                            rerun_on_success = False,
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

                # Transfer already-collected consultation fee payment to the new retail
                # order so it appears as advance against this provisional billing.
                _consult_paid_adv = float(st.session_state.get("_consult_paid_advance_amount") or 0)
                _consult_src_for_payment = st.session_state.get("_retail_consult_source_id", "")
                if _consult_paid_adv > 0 and _consult_src_for_payment and _real_uuid_r:
                    try:
                        from modules.sql_adapter import run_write as _rw_cpay, run_query as _rq_cpay
                        _consult_pay_uuid = str(_consult_src_for_payment)
                        if not (len(_consult_pay_uuid) == 36 and _consult_pay_uuid.count("-") == 4):
                            _cpay_rows = _rq_cpay(
                                "SELECT id::text FROM orders WHERE order_no=%s AND order_type='CONSULTATION' LIMIT 1",
                                (_consult_pay_uuid,)
                            ) or []
                            _consult_pay_uuid = _cpay_rows[0]["id"] if _cpay_rows else ""
                        if _consult_pay_uuid:
                            _rw_cpay("""
                                UPDATE payments
                                   SET order_id = %(rid)s::uuid,
                                       advance_for_order_id = %(rid)s::uuid,
                                       payment_type = 'ADVANCE',
                                       is_advance = TRUE,
                                       remarks = COALESCE(remarks, '') || ' | Transferred from consultation'
                                 WHERE advance_for_order_id = %(cid)s::uuid
                                   AND (
                                       payment_type = 'ADVANCE'
                                       OR COALESCE(payment_no,'') LIKE 'CPR-%%'
                                       OR COALESCE(remarks,'') ILIKE '%%consultation fee%%'
                                   )
                                   AND COALESCE(is_deleted, FALSE) = FALSE
                            """, {"rid": _real_uuid_r, "cid": _consult_pay_uuid})
                            _rw_cpay("""
                                UPDATE orders
                                   SET advance_amount = COALESCE(advance_amount, 0) + %(amt)s,
                                       advance_received = TRUE,
                                       payment_status = CASE
                                           WHEN COALESCE(total_value, 0) > 0
                                            AND COALESCE(advance_amount, 0) + %(amt)s >= COALESCE(total_value, 0) - 0.50
                                           THEN 'PAID'
                                           ELSE 'PARTIAL'
                                       END,
                                       updated_at = NOW()
                                 WHERE id = %(rid)s::uuid
                            """, {"rid": _real_uuid_r, "amt": _consult_paid_adv})
                    except Exception as _cpay_err:
                        import logging
                        logging.warning(f"[Retail] Consultation advance transfer failed: {_cpay_err}")

                st.session_state["last_confirmed_order"] = {
                    "id":             _real_uuid_r,
                    "order_no":       str(result.get("order_no", "")),
                    "status":         "PENDING",
                    "payment_status": "PENDING",   # FIX: payment tracking
                    "total":          _retail_line_payable_total(st.session_state.get("retail_order_lines", [])),
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
                            # Service-only punching now creates a real SERVICE
                            # order_line in the cart. Do not duplicate it into
                            # order_charges after confirm.
                            if _ch.get("line_id"):
                                continue
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
                _adv_snap = (
                    _retail_cash_round(st.session_state.get("_consult_paid_advance_amount") or 0)
                    + _retail_cash_round(st.session_state.get("retail_advance_amount") or 0)
                )
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
                _receipt_restore = copy.deepcopy(_receipt_data)
                _post_save_restore = copy.deepcopy(st.session_state.get("_post_save_data") or {})
                _auto_print_restore = bool(do_print)

                # ----------------------------
                # FULL ERP RESET (SAFE)
                # ----------------------------
                industrial_reset("ALL")
                st.session_state["_receipt_snapshot"] = _receipt_restore
                st.session_state[_ono_key] = _receipt_restore
                st.session_state["_last_receipt_key"] = _ono_key
                st.session_state["_show_receipt_top"] = True
                if _post_save_restore:
                    st.session_state["_post_save_data"] = _post_save_restore
                if _auto_print_restore:
                    st.session_state["_auto_print_receipt"] = True
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
                st.session_state.pop("_consult_paid_advance_amount", None)
                st.session_state.pop("_consult_paid_advance_mode", None)
                st.session_state.pop("_consult_paid_advance_ref", None)
                st.session_state.pop("_force_full_billing_mode", None)
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
    try:
        from modules.sql_adapter import resolve_order_uuid as _resolve_order_uuid
        order_id = _resolve_order_uuid(order_id) or _resolve_order_uuid(order_no) or ""
    except Exception:
        order_id = ""

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
    _edit_order_type = "RETAIL"
    _edit_party_id_db = ""
    try:
        from modules.sql_adapter import run_query as _rq_lock
        _lock_rows = _rq_lock(
            """
            SELECT status,
                   COALESCE(order_type, 'RETAIL') AS order_type,
                   COALESCE(party_id::text, '') AS party_id
            FROM orders
            WHERE id = %s::uuid
            LIMIT 1
            """,
            (str(order_id),)
        ) or []
        if _lock_rows:
            _current_status = str(_lock_rows[0].get("status", "")).upper()
            _edit_order_type = str(_lock_rows[0].get("order_type") or "RETAIL").upper()
            _edit_party_id_db = str(_lock_rows[0].get("party_id") or "")
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

        # Schema columns ensured by modules/db/migrations/0001_billing_columns.sql
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
                              st.session_state.get("selected_party_id") or
                              st.session_state.get("wh_party_id") or
                              st.session_state.get("_ws_party_id") or
                              _edit_party_id_db or "")
            # Stamp promo code on all lines at save time
            _pc_save = str(st.session_state.get("_retail_promo_code") or "").strip()
            if _pc_save:
                for _csl in cart_lines:
                    _csl["promo_code"] = _pc_save
            apply_discounts(cart_lines, party_id=_r_party_id, order_type=_edit_order_type)
            # Club offers — cart-level cross-product check, fires after apply_discounts
            try:
                from modules.pricing.club_engine import apply_club_offers
                apply_club_offers(cart_lines, order_type=_edit_order_type)
            except Exception: pass

            # ── Restamp net into total_price/billing_total/gst_amount ────
            # apply_discounts only writes discount_amount; without restamp,
            # the INSERT below picks up the gross total_price and the DB
            # ends up with discount_amount > 0 but total_price = gross.
            # This call mirrors what wholesale_punching does (via
            # _stamp_cart_line_discount → restamp_line_totals) so retail
            # and wholesale store the same net-based truth.
            try:
                from modules.pricing.discount_flow import restamp_line_totals
                for _csl2 in cart_lines:
                    restamp_line_totals(_csl2, _edit_order_type)
            except Exception:
                pass
        except Exception as _de:
            pass  # zero-risk: continues with product-level discount_percent

        # ── Insert new lines ───────────────────────────────────────────
        # Service lines get is_service_line=TRUE + allocated_qty=qty + status=READY
        # so backoffice billing readiness checks pass without requiring a batch
        # allocation step. The wholesale path already does this via
        # order_repository.save_order; retail goes through its own INSERT and
        # so we replicate the same logic here.
        # Detect once whether the schema has the service columns; if a previous
        # migration did not run, fall back to the legacy INSERT shape.
        try:
            _svc_col_check = run_query("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='order_lines'
                  AND column_name IN ('is_service_line','allocated_qty','ready_qty')
            """, ()) or []
            _have_svc_cols = len(_svc_col_check) >= 3
        except Exception:
            _have_svc_cols = False

        new_total = 0.0
        for ln in cart_lines:
            _lid  = str(uuid.uuid4())
            _pid  = str(ln.get("product_id") or "")
            _eye_raw = str(ln.get("eye_side","O") or "O").upper().strip()
            _eye = {"OTHER":"O","SERVICE":"S","R":"R","L":"L","B":"B"}.get(_eye_raw, _eye_raw[:1] or "O")
            _is_svc_line = (_eye == "S") or bool(ln.get("is_service_line"))
            _sph  = ln.get("sph")
            _cyl  = ln.get("cyl")
            _axis = ln.get("axis")
            _add  = ln.get("add_power")
            _qty  = int(ln.get("billing_qty") or ln.get("requested_qty") or ln.get("quantity") or 1)
            _up   = float(ln.get("unit_price", 0) or 0)
            _tot  = float(ln.get("total_price", 0) or 0)
            if _up > 0 and _tot == 0:
                _tot = round(_up * _qty, 2)  # recalculate if missing
            _btot = float(ln.get("billing_total") or _tot or 0)
            _gp   = float(ln.get("gst_percent", 0) or 0)
            _ga   = float(ln.get("gst_amount", 0) or 0)
            _is_exempt_line = bool(ln.get("is_gst_exempt")) or (
                _gp == 0 and "consult" in str(ln.get("product_name") or "").lower()
            )
            _lp   = ln.get("lens_params") or {}
            _bp   = ln.get("boxing_params") or {}
            _line_status = "READY" if _is_svc_line else "PENDING"
            _alloc_qty   = _qty if _is_svc_line else 0
            _ready_qty   = _qty if _is_svc_line else 0
            import json
            if _have_svc_cols:
                run_write("""
                    INSERT INTO order_lines (
                        id, order_id, product_id,
                        sph, cyl, axis, add_power, eye_side,
                        quantity, unit_price, total_price,
                        gst_percent, gst_amount,
                        is_gst_exempt,
                        discount_percent, discount_amount,
                        billing_total, discount_rule, applied_rule_ids,
                        status, lens_params, boxing_params, suggested_allocation,
                        is_service_line, allocated_qty, ready_qty
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, NULL,
                        %s, %s, %s
                    )
                """, (
                    _lid, order_id, _pid if _pid else None,
                    _sph, _cyl, _axis, _add, _eye,
                    _qty, _up, _tot,
                    _gp, _ga,
                    _is_exempt_line,
                    float(ln.get("discount_percent", 0)),
                    float(ln.get("discount_amount", 0)),
                    _btot,
                    str(ln.get("discount_rule") or ""),
                    str(ln.get("applied_rule_ids") or ""),
                    _line_status,
                    json.dumps(_lp), json.dumps(_bp),
                    _is_svc_line, _alloc_qty, _ready_qty,
                ))
            else:
                # Legacy schema fallback — service lines still discoverable via
                # eye_side='S' which the production_page bootstrap matches on.
                run_write("""
                    INSERT INTO order_lines (
                        id, order_id, product_id,
                        sph, cyl, axis, add_power, eye_side,
                        quantity, unit_price, total_price,
                        gst_percent, gst_amount,
                        is_gst_exempt,
                        discount_percent, discount_amount,
                        billing_total, discount_rule, applied_rule_ids,
                        status, lens_params, boxing_params, suggested_allocation
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, NULL
                    )
                """, (
                    _lid, order_id, _pid if _pid else None,
                    _sph, _cyl, _axis, _add, _eye,
                    _qty, _up, _tot,
                    _gp, _ga,
                    _is_exempt_line,
                    float(ln.get("discount_percent", 0)),
                    float(ln.get("discount_amount", 0)),
                    _btot,
                    str(ln.get("discount_rule") or ""),
                    str(ln.get("applied_rule_ids") or ""),
                    _line_status,
                    json.dumps(_lp), json.dumps(_bp),
                ))
            new_total += _btot  # use net billing_total, not gross total_price

        new_lines = len(cart_lines)
        new_total_header = round(new_total)

        # ── Update order header ────────────────────────────────────────
        _del_date = str(delivery_date)[:10] if delivery_date else None
        run_write("""
            UPDATE orders SET
                total_items  = %s,
                total_value  = %s,
                updated_at   = NOW()
            WHERE id = %s::uuid
        """, (new_lines, new_total_header, order_id))
        try:
            from modules.backoffice.backoffice_helpers import ensure_order_production_refs
            ensure_order_production_refs(order_no=order_no, order_id=order_id)
        except Exception as _prod_ref_err:
            import logging as _prod_ref_log
            _prod_ref_log.getLogger(__name__).warning(
                "Retail edit production_ref fill failed for %s: %s",
                order_no, _prod_ref_err,
            )

        # ── Detect changes for log ─────────────────────────────────────
        changes = []
        if orig_lines != new_lines:
            changes.append(f"Lines: {orig_lines} → {new_lines}")
        if abs(orig_total - new_total_header) > 0.01:
            changes.append(f"Value: ₹{orig_total:,.2f} → ₹{new_total_header:,.2f}")
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
                try:
                    from modules.db.order_number_registry import alloc_doc_number
                    _pno = alloc_doc_number("PAYMENT")
                except Exception:
                    _pno = f"ADV-EDIT-{order_id[:8]}"
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
                    """
                    UPDATE orders
                    SET payment_mode=%s,
                        advance_amount = COALESCE(advance_amount,0) + %s,
                        advance_received = TRUE,
                        payment_status = CASE
                            WHEN COALESCE(total_value,0) > 0
                             AND COALESCE(advance_amount,0) + %s >= COALESCE(total_value,0) - 0.50
                            THEN 'PAID'
                            ELSE 'PARTIAL'
                        END,
                        updated_at=NOW()
                    WHERE id=%s::uuid
                    """,
                    (advance_mode, advance_amount, advance_amount, order_id)
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


def _open_html_print(html: str, filename: str = "print.html") -> str:
    """
    Save HTML to temp file and open in default browser via ShellExecute.
    Blob/window.open is blocked by Streamlit iframe sandbox.
    ShellExecute opens the file at OS level — bypasses all iframe restrictions.
    """
    import os, tempfile, uuid
    base, ext = os.path.splitext(filename or "print.html")
    if not ext:
        ext = ".html"
    safe_base = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in base) or "print"
    tmp = os.path.join(tempfile.gettempdir(), f"{safe_base}_{uuid.uuid4().hex[:8]}{ext}")
    with open(tmp, "w", encoding="utf-8") as _f:
        _f.write(html)
    try:
        os.startfile(tmp)  # Windows: most reliable from local Streamlit process
        return tmp
    except Exception:
        pass
    try:
        import win32api
        win32api.ShellExecute(0, "open", tmp, None, ".", 1)
        return tmp
    except Exception:
        pass
    try:
        import webbrowser
        webbrowser.open("file:///" + tmp.replace(os.sep, "/"), new=2)
        return tmp
    except Exception as _ex:
        try:
            import streamlit as _st
            _st.warning(f"Could not open print dialog: {_ex}. File saved at: {tmp}")
        except Exception:
            pass
        return tmp


def _line_product_with_spec(line: dict) -> str:
    """Product display name including ophthalmic index/coating/treatment."""
    pname = str(line.get("product_name") or "")
    lp = line.get("lens_params") or {}
    if not isinstance(lp, dict):
        lp = {}
    spec_src = lp or line.get("oph_spec") or {}
    if not isinstance(spec_src, dict):
        spec_src = {}
    suffix = str(spec_src.get("display_suffix") or "").strip()
    if suffix and suffix.lower() not in pname.lower():
        return f"{pname} {suffix}".strip()
    idx = spec_src.get("lens_index") or spec_src.get("index") or spec_src.get("index_value") or ""
    coating = spec_src.get("coating") or spec_src.get("coating_type") or ""
    treatment = spec_src.get("treatment") or ""
    spec = []
    if idx:
        spec.append(f"Index {idx}")
    if coating:
        spec.append(str(coating))
    if treatment and str(treatment).strip().lower() != "clear":
        spec.append(str(treatment))
    spec_txt = " | ".join(spec)
    if spec_txt and spec_txt.lower() not in pname.lower():
        return f"{pname} | {spec_txt}"
    return pname


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
    from modules.printing.patient_card_printer import readable_barcode_code as _readable_bc
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
    order_bc = _readable_bc("O", order_no, 14)
    pat_bc_svg   = _bsvg(pat_bc,   width=220, height=48) if pat_bc else ""
    order_bc_svg = _bsvg(order_bc, width=220, height=48)

    # ── UPI QR code ───────────────────────────────────────────────────────
    shop_upi = shop_info.get("shop_upi_id", "").strip()
    _upi_qr_html = ""
    if shop_upi:
        try:
            import urllib.parse as _url_qr
            import qrcode as _qr_mod, io as _io_qr, base64 as _b64_qr
            _qr_due = max(
                sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in lines) - adv_amt,
                0.0,
            )
            _upi_str = "upi://pay?" + _url_qr.urlencode({
                "pa": shop_upi,
                "pn": shop or "DV Optical",
                "am": f"{_qr_due:.2f}",
                "tn": order_no,
                "cu": "INR",
            })
            _qr = _qr_mod.QRCode(
                version=None,
                error_correction=_qr_mod.constants.ERROR_CORRECT_M,
                box_size=3, border=2)
            _qr.add_data(_upi_str)
            _qr.make(fit=True)
            _img = _qr.make_image(fill_color="black", back_color="white")
            _buf = _io_qr.BytesIO()
            _img.save(_buf, format="PNG")
            _b64 = _b64_qr.b64encode(_buf.getvalue()).decode()
            _upi_qr_html = (
                "<div style='text-align:center'>"
                "<div style='width:52px;height:52px;margin:0 auto;"
                "background-image:url(data:image/png;base64,{});background-size:contain;"
                "background-repeat:no-repeat;-webkit-print-color-adjust:exact;"
                "print-color-adjust:exact;color-adjust:exact'></div>"
                "<div style='font-size:5.5pt;color:#64748b;margin-top:2px'>Scan to Pay</div>"
                "<div style='font-size:5.5pt;font-weight:700;color:#0f172a;margin-top:1px'>{}</div>"
                "</div>"
            ).format(_b64, shop_upi)
        except Exception:
            _upi_qr_html = (
                "<div style='font-size:6pt;color:#64748b;text-align:center'>"
                "<b>UPI:</b><br>{}</div>".format(shop_upi)
            )

    # ── Totals ─────────────────────────────────────────────────────────────
    raw_grand_total = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in lines)
    grand_total = _retail_cash_round(raw_grand_total)
    gst_total   = sum(
        round(float(l.get("billing_total") or l.get("total_price") or 0) -
              float(l.get("billing_total") or l.get("total_price") or 0) / (1 + float(l.get("gst_percent",0) or 0) / 100), 2)
        for l in lines
    )
    base_total  = round(raw_grand_total - gst_total, 2)
    balance     = _retail_cash_round(grand_total - adv_amt)

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
        pname = _line_product_with_spec(l)
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
@page {{ size: 210mm 148mm; margin: 4mm 5mm; }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{
  width: 210mm;
  min-height: 148mm;
  font-family: Arial, Helvetica, sans-serif;
  font-size: 9pt;
  color: #0f172a;
  background: white;
  margin: 0;
}}

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

/* ── Barcode footer — same as job card ── */
.bc-footer {{ display:flex; gap:6mm; align-items:flex-start;
              padding:3mm 6mm; border-top:1px dashed #cbd5e1;
              background:#f8fafc; margin-top:auto; }}
.bc-box {{ border:1px solid #000; padding:1mm 2mm; flex:1;
           text-align:center; font-size:6pt;
           min-height:18mm; overflow:hidden; }}
.bc-label {{ font-size:6.5pt; color:#64748b; font-weight:700;
             text-transform:uppercase; letter-spacing:.06em;
             margin-bottom:1px; }}

/* ── Footer ── */
.page-footer {{ text-align:center; font-size:7pt; color:#94a3b8;
                border-top:1px dashed #e2e8f0; padding:3px 10px; }}

@media print {{
  @page {{ size: 210mm 148mm; margin: 4mm 5mm; }}
  html, body {{
    width: 210mm !important;
    height: 148mm !important;
    margin: 0 !important;
    overflow: hidden !important;
  }}
  body {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; color-adjust: exact; }}
  div {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
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
        • Service charges shown on this receipt are included in the order total<br>
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
  { f'<div class="bc-box"><div class="bc-label">Patient ID</div>{pat_bc_svg}</div>' if pat_bc_svg else "" }
  <div class="bc-box">
    <div class="bc-label">Order No — {order_no}</div>
    {order_bc_svg}
  </div>
  { f'<div class="bc-box" style="max-width:70px;min-height:unset">{_upi_qr_html}</div>' if _upi_qr_html else "" }
  <div style="flex:1;text-align:right;font-size:7.5pt;color:#475569;padding-top:2mm">
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
        _html = _build_confirmation_receipt_html(snap, _si)
        _print_file = f"confirmation_{str(order_no or 'receipt')}.html"
        try:
            _open_html_print(_html, _print_file)
            st.success("✅ Print receipt opened in browser")
            return
        except Exception as _os_open_err:
            st.caption(f"Direct browser open fallback used: {_os_open_err}")
        try:
            # Last resort only: this can be blocked by iframe/popup policies.
            import streamlit.components.v1 as _comp
            import base64 as _b64
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
            st.success("✅ Print receipt opened")
        except Exception as _pe:
            st.error(f"Print failed: {_pe}")

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
        _adv = _retail_cash_round(snap.get("advance_amount") or 0)
        _tot = _retail_line_payable_total(snap.get("lines", []))
        _bal = _retail_cash_round(_tot - _adv)
        if _adv > 0:
            st.metric("Balance Due", f"₹{_bal:,.2f}")
        else:
            st.metric("Total Due", f"₹{_tot:,.2f}")

    # ── Mini summary ────────────────────────────────────────────────────
    with st.expander(f"📋 Receipt Preview — {order_no}", expanded=False):
        _lines = snap.get("lines", [])
        _tot   = _retail_line_payable_total(_lines)
        _adv   = _retail_cash_round(snap.get("advance_amount") or 0)
        st.caption(f"**Patient:** {snap.get('patient_name','')}  "
                   f"**Mobile:** {snap.get('patient_mobile','')}  "
                   f"**Confirmed:** {snap.get('confirmed_at','')}")
        for _l in _lines:
            _eye = _l.get("eye_side","")
            _pn  = _l.get("product_name","")
            _qty = _l.get("display_qty") or str(_l.get("billing_qty",0))
            _tot_l = float(_l.get("billing_total") or _l.get("total_price") or 0)
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;"
                f"font-size:0.8rem;padding:2px 0;border-bottom:1px solid #f1f5f9'>"
                f"<span><b style='color:#64748b'>{_eye}</b> &nbsp; {_line_product_with_spec(_l)}</span>"
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
        st.session_state["_force_full_billing_mode"] = True
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
        _cpb_include_fee = bool(_cp_bridge.get("include_consult_fee"))
        _cpb_paid_amt = float(_cp_bridge.get("consult_paid_amount") or 0)
        _cpb_paid_mode = str(_cp_bridge.get("payment_mode") or "").upper()
        _cpb_paid_ref = str(_cp_bridge.get("payment_ref") or _cp_bridge.get("consult_paid_ref") or "").strip()
        if _cpb_name:
            st.session_state["retail_patient_name"]   = _cpb_name
            st.session_state["retail_patient_mobile"] = _cpb_mobile
        if _cpb_pid and len(str(_cpb_pid)) > 10:
            st.session_state["retail_patient_id"] = str(_cpb_pid)
        if _cpb_oid:
            st.session_state["retail_case_no"]            = _cpb_oid
            st.session_state["_retail_consult_source_id"] = _cpb_oid
        if _cpb_include_fee and _cpb_paid_amt > 0:
            st.session_state["_consult_paid_advance_amount"] = _cpb_paid_amt
            st.session_state["_consult_paid_advance_mode"] = _cpb_paid_mode or "CASH"
            st.session_state["_consult_paid_advance_ref"] = _cpb_paid_ref
        else:
            st.session_state.pop("_consult_paid_advance_amount", None)
            st.session_state.pop("_consult_paid_advance_mode", None)
            st.session_state.pop("_consult_paid_advance_ref", None)
        _cpb_lines = _cp_bridge.get("order_lines") or []

        # ── Separate SERVICE lines (fee) from product/lens lines ──────────
        # SERVICE lines must ONLY go to _consult_fee_lines — they are injected
        # into the order at submit time with dedup protection.
        # Putting them into retail_order_lines directly causes:
        #   (a) cart shows only ₹400 when products haven't been added yet
        #   (b) double-inject when the cart and _consult_fee_lines both fire
        _cpb_svc_lines  = [l for l in _cpb_lines
                           if _cpb_include_fee
                           and (str(l.get("eye_side","")).upper() in ("SERVICE","S")
                                or bool(l.get("is_service_line")))]
        _cpb_prod_lines = [l for l in _cpb_lines
                           if not (str(l.get("eye_side","")).upper() in ("SERVICE","S")
                                   or bool(l.get("is_service_line")))]

        # Set fee lines (idempotent — only if not already set from _set_consult_billing_state)
        if _cpb_svc_lines and not st.session_state.get("_consult_fee_lines"):
            st.session_state["_consult_fee_lines"] = _cpb_svc_lines

        # Set product lines into cart only if cart is currently empty
        if _cpb_prod_lines and not st.session_state.get("retail_order_lines"):
            import datetime as _dt_cpb, uuid as _uuid_cpb
            st.session_state["retail_order_lines"] = _cpb_prod_lines
            if not st.session_state.get("retail_provisional_order_id"):
                st.session_state["retail_provisional_order_id"] = (
                    f"PO-CONS-{_dt_cpb.datetime.now().strftime('%Y%m%d%H%M%S')}"
                    f"-{str(_uuid_cpb.uuid4())[:6].upper()}"
                )

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

        # ── Clear stale WhatsApp widget state ────────────────────────────
        # When patient changes via consultation bridge, old WA phone/message
        # values stored in Streamlit widget state by key must be purged.
        # Otherwise the previous patient's number persists in the WA text_input.
        for _wa_stale_k in list(st.session_state.keys()):
            if (str(_wa_stale_k).startswith("consult_wa_mobile_display") or
                    str(_wa_stale_k).startswith("consult_wa_mob_edited_") or
                    str(_wa_stale_k).startswith("consult_wa_url_") or
                    str(_wa_stale_k).startswith("consult_wa_msg_") or
                    str(_wa_stale_k).startswith("consult_wa_mobile_")):
                st.session_state.pop(_wa_stale_k, None)

    # Apply _erp_rx_r / _erp_rx_l if set (from order_edit_view edit button)
    # Also clear retail_visit_mode HERE (unconditionally) so the radio always
    # respects _visit_mode_default even before retail_patient_id is set
    if st.session_state.get("_force_consultation_tab") or \
       st.session_state.get("_editing_consult_order_id"):
        st.session_state.pop("retail_visit_mode", None)
        st.session_state["_visit_mode_default"] = 1
        st.session_state.pop("_force_full_billing_mode", None)
        st.session_state.pop("last_confirmed_order", None)

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
        st.session_state["_force_full_billing_mode"] = True
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
    clear_orphan_retail_cart()

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

    # ── Post-save actions first — WhatsApp should not be hidden below receipt ─
    _post_save_rendered_top = False
    _psd_top = st.session_state.get("_post_save_data")
    if _psd_top:
        st.markdown(
            "<div style='background:#0a1628;border:2px solid #22c55e;"
            "border-radius:8px;padding:4px 8px;margin-bottom:8px'>"
            "<div style='color:#4ade80;font-size:0.72rem;font-weight:700;"
            "text-align:center;margin-bottom:4px'>Order saved ✓ Send / print / next action</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        _pa1, _pa2 = st.columns(2)
        with _pa1:
            if st.button("📄 Duplicate Order", key="retail_dup_top",
                         use_container_width=True,
                         help="Same patient & power — pick a different product"):
                _src_ono = _psd_top.get("order_no", "")
                duplicate_current_order()
                st.session_state.pop("_post_save_data", None)
                if _src_ono:
                    st.session_state["_retail_source_order_no"] = _src_ono
                st.rerun()
        with _pa2:
            if st.button("➕ New Order", key="retail_new_top",
                         type="primary", use_container_width=True):
                industrial_reset("ALL")
                for _clr_k in (
                    "_receipt_snapshot", "_last_receipt_key",
                    "_post_save_data", "_show_receipt_top",
                    "_editing_order_id", "_editing_order_no",
                    "_edit_existing_advance",
                    "_retail_duplicate_mode", "_retail_source_order_no",
                ):
                    st.session_state.pop(_clr_k, None)
                for _k in list(st.session_state.keys()):
                    if (_k.startswith("_confirmed_cart_") or
                        _k.startswith("_receipt_data_") or
                        _k.startswith("lp_") or _k.startswith("bp_") or
                        _k.startswith("ps_")):
                        st.session_state.pop(_k, None)
                clear_retail_cart_completely(set_consult_removed=True)
                st.rerun()

        _render_post_save_actions(
            _psd_top.get("order_no", ""),
            _psd_top.get("party_name", ""),
            _psd_top.get("mobile", ""),
            float(_psd_top.get("total", 0)),
            _psd_top.get("order_type", "RETAIL"),
            float(_psd_top.get("advance", 0)),
            _psd_top.get("delivery", ""),
            lines=_psd_top.get("lines", []),
            status_label=_psd_top.get("status_label", "RECEIVED"),
        )
        if st.button("✕ Dismiss", key="retail_dismiss_post_save_top"):
            st.session_state.pop("_post_save_data", None)
            st.rerun()
        st.markdown("---")
        _post_save_rendered_top = True

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

    # ── Post-save actions — top Duplicate / New Order bar ────────────────
    _psd = st.session_state.get("_post_save_data")
    if _psd and not _post_save_rendered_top:
        # ── What next? banner ────────────────────────────────────────────
        st.markdown(
            "<div style='background:#0a1628;border:2px solid #22c55e;"
            "border-radius:8px;padding:4px 8px;margin-bottom:8px'>"
            "<div style='color:#4ade80;font-size:0.72rem;font-weight:700;"
            "text-align:center;margin-bottom:4px'>Order saved ✓ What next?</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        _pa1, _pa2 = st.columns(2)

        # ── Duplicate ─────────────────────────────────────────────────────
        with _pa1:
            if st.button("📄 Duplicate Order", key="retail_dup_top",
                         use_container_width=True,
                         help="Same patient & power — pick a different product"):
                _src_ono = _psd.get("order_no","")
                duplicate_current_order()
                st.session_state.pop("_post_save_data", None)
                if _src_ono:
                    st.session_state["_retail_source_order_no"] = _src_ono
                st.rerun()

        # ── New Order ────────────────────────────────────────────────────
        with _pa2:
            if st.button("➕ New Order", key="retail_new_top",
                         type="primary", use_container_width=True):
                _src_ono_n = _psd.get("order_no","")
                industrial_reset("ALL")
                for _clr_k in (
                    "_receipt_snapshot", "_last_receipt_key",
                    "_post_save_data", "_show_receipt_top",
                    "_editing_order_id", "_editing_order_no",
                    "_edit_existing_advance",
                    "_retail_duplicate_mode", "_retail_source_order_no",
                ):
                    st.session_state.pop(_clr_k, None)
                for _k in list(st.session_state.keys()):
                    if (_k.startswith("_confirmed_cart_") or
                        _k.startswith("_receipt_data_") or
                        _k.startswith("lp_") or _k.startswith("bp_") or
                        _k.startswith("ps_")):
                        st.session_state.pop(_k, None)
                clear_retail_cart_completely(set_consult_removed=True)
                st.rerun()

        st.markdown("<div style='margin:4px 0'></div>", unsafe_allow_html=True)

        _render_post_save_actions(
            _psd.get("order_no",""),
            _psd.get("party_name",""),
            _psd.get("mobile",""),
            float(_psd.get("total",0)),
            _psd.get("order_type","RETAIL"),
            float(_psd.get("advance",0)),
            _psd.get("delivery",""),
            lines=_psd.get("lines", []),
            status_label=_psd.get("status_label","RECEIVED"),
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

        # Mode defaults must be handled BEFORE radio renders.
        _consult_edit_mode = bool(st.session_state.get("_editing_consult_order_id"))
        if _consult_edit_mode:
            st.session_state["_visit_mode_default"] = 1
            st.session_state.pop("_force_full_billing_mode", None)
            st.session_state.pop("retail_visit_mode", None)

        if st.session_state.get("_force_full_billing_mode"):
            st.session_state["_visit_mode_default"] = 0
            st.session_state.pop("retail_visit_mode", None)

        # Clear widget key so _visit_mode_default controls the radio (not stale widget state)
        if st.session_state.get("_force_consultation_tab"):
            st.session_state.pop("retail_visit_mode", None)
            st.session_state.pop("_force_consultation_tab", None)
        _default_idx = st.session_state.get("_visit_mode_default", 1)  # default Consultation

        if _consult_edit_mode:
            _visit_mode = "🩺 Consultation Only"
            st.session_state["_visit_mode_default"] = 1
            st.info("🩺 Editing consultation record — product billing is closed here.")
        else:
            _visit_mode = st.radio(
                "Visit type",
                ["🛍️ Full Billing", "🩺 Consultation Only"],
                index=_default_idx,
                horizontal=True,
                key="retail_visit_mode",
            )
            # Persist choice for next rerun. This is now the only visible visit-mode control.
            st.session_state["_visit_mode_default"] = (
                0 if _visit_mode == "🛍️ Full Billing" else 1
            )

        st.markdown("---")
    else:
        _visit_mode = "🛍️ Full Billing"
        st.session_state["_visit_mode_default"] = 0

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

    _full_billing_mode = (_visit_mode != "🩺 Consultation Only")

    if _full_billing_mode:
        # 6b️⃣ Add extra line to cart before submitting
        try:
            from modules.backoffice.order_line_adder import render_cart_line_adder
            render_cart_line_adder(order_type="RETAIL")
        except Exception as _ale:
            st.caption(f"Add line: {_ale}")


        # 6c️⃣ Service Charges
        # Rendered once a patient is selected. This allows fitting/colouring-only
        # punching where no product line exists yet.
        # Position is stable: after cart, before Finalize button.
        # Fresh retail orders -> session-staged, flushed to DB on Confirm
        _sc_cart      = st.session_state.get("retail_order_lines") or []
        _sc_consult_id = st.session_state.get("_retail_consult_source_id", "")
        if _sc_cart or st.session_state.get("retail_patient_id"):
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
                _pending = st.session_state.get("_retail_pending_charges", [])
                _svc_party_id = str(
                    st.session_state.get("retail_party_id")
                    or st.session_state.get("selected_party_id")
                    or st.session_state.get("retail_patient_id")
                    or ""
                )
                if _svc_party_id.upper().startswith("TEMP-"):
                    _svc_party_id = ""
                _svc_rows = _service_master_rows("RETAIL", party_id=_svc_party_id)
                def _service_pick_key(_svc: dict) -> str:
                    _raw = (
                        _svc.get("service_code")
                        or _svc.get("id")
                        or _svc.get("service_name")
                        or _svc.get("name")
                        or _svc.get("service_group")
                        or "SERVICE"
                    )
                    return str(_raw).upper().strip().replace(" ", "_")
                _svc_by_code = {_service_pick_key(s): s for s in _svc_rows}

                # Display existing pending charges
                if _pending:
                    for _pi, _pc in enumerate(_pending):
                        _svc_cur = _svc_by_code.get(str(_pc.get("service_code") or _pc.get("type") or "").upper(), {})
                        _cfg2 = _service_ui_meta(_pc.get("service_group") or _pc.get("type") or _svc_cur.get("service_group"))
                        _c1, _c2, _c3, _c4 = st.columns([0.4, 3.5, 1.5, 0.6])
                        _c1.markdown(
                            f"<div style='font-size:1.2rem;text-align:center'>"
                            f"{_cfg2['icon']}</div>",
                            unsafe_allow_html=True)
                        _c2.markdown(
                            f"<span style='color:#e2e8f0;font-size:0.82rem;"
                            f"font-weight:600'>{_pc['desc']}</span>"
                            f"<br><span style='color:#64748b;font-size:0.7rem'>"
                            f"{_svc_cur.get('service_group') or _pc.get('type') or _cfg2['label']}</span>",
                            unsafe_allow_html=True)
                        _c3.markdown(
                            f"<span style='color:#10b981;font-weight:700'>"
                            f"₹{float(_pc['amt']) * float(_pc.get('qty_factor') or 1):,.0f}</span>",
                            unsafe_allow_html=True)
                        if _c4.button("🗑",
                                      key=f"sc_del_{_pi}",
                                      help="Remove charge"):
                            _old_lid = str(_pc.get("line_id") or "")
                            _pending.pop(_pi)
                            st.session_state["_retail_pending_charges"] = _pending
                            if _old_lid:
                                st.session_state.retail_order_lines = [
                                    _l for _l in (st.session_state.get("retail_order_lines") or [])
                                    if str(_l.get("line_id") or "") != _old_lid
                                ]
                            st.rerun()
                    st.markdown(
                        "<hr style='margin:4px 0;border-color:#1e293b'>",
                        unsafe_allow_html=True)

                # Add-new charge type buttons
                _sc_add_key    = "_sc_add_type"
                _sc_token_key  = "_sc_add_order_token"
                _sc_cart_token = "|".join([
                    str(st.session_state.get("retail_provisional_order_id") or ""),
                    str(st.session_state.get("retail_patient_id") or ""),
                    ",".join(str(_l.get("line_id") or "") for _l in _sc_cart),
                ])
                if st.session_state.get(_sc_add_key) and st.session_state.get(_sc_token_key) != _sc_cart_token:
                    st.session_state.pop(_sc_add_key, None)
                    st.session_state.pop(_sc_token_key, None)
                _existing_codes = {str(c.get("service_code") or c.get("type") or "").upper() for c in _pending}
                _addable       = [s for s in _svc_rows if _service_pick_key(s) not in _existing_codes]
                if not st.session_state.get(_sc_add_key):
                    if _addable:
                        for _grp_name in ("FITTING", "COLOURING", "COURIER", "OTHER"):
                            _grp_items = [s for s in _addable if str(s.get("service_group") or "").upper() == _grp_name]
                            if not _grp_items:
                                continue
                            _gmeta = _service_ui_meta(_grp_name)
                            with st.expander(f"{_gmeta['icon']} {_grp_name.title()} Services", expanded=(_grp_name in ("FITTING", "COLOURING"))):
                                _cols_sc = st.columns(min(3, len(_grp_items)))
                                for _ai, _svc in enumerate(_grp_items):
                                    _at = _service_pick_key(_svc)
                                    with _cols_sc[_ai % len(_cols_sc)]:
                                        st.caption(f"₹{float(_svc.get('default_price') or 0):,.0f} · GST {float(_svc.get('gst_percent') or 0):g}%")
                                        if st.button(
                                            f"+ {_svc.get('service_name') or _at}",
                                            key=f"sc_add_{_at}",
                                            width='stretch'):
                                            st.session_state[_sc_add_key] = _at
                                            st.session_state[_sc_token_key] = _sc_cart_token
                                            st.rerun()
                    else:
                        st.caption("🟢 All charge types added.")
                else:
                    _sc_ct  = st.session_state[_sc_add_key]
                    _sc_svc = _svc_by_code.get(str(_sc_ct).upper(), {})
                    _sc_group = str(_sc_svc.get("service_group") or _sc_ct or "MISC").upper()
                    _sc_cfg = _service_ui_meta(_sc_group)
                    st.markdown(
                        f"<div style='background:#0f172a;"
                        f"border:1px solid {_sc_cfg['color']}44;"
                        f"border-radius:8px;padding:10px;margin:4px 0'>"
                        f"<span style='color:{_sc_cfg['color']};font-weight:700'>"
                        f"{_sc_cfg['icon']} Add {_sc_svc.get('service_name') or _sc_cfg['label']}</span></div>",
                        unsafe_allow_html=True)
                    _sc_courier_provider_id = ""
                    _sc_courier_provider_name = ""
                    _sc_courier_rate_option_id = ""
                    _sc_courier_rate_option_label = ""
                    _sc_courier_parcel_size = ""
                    if _sc_group == "COURIER":
                        try:
                            from modules.backoffice.service_master import fetch_providers as _rt_fetch_providers
                            from modules.backoffice.service_master import fetch_courier_rate_options as _rt_fetch_courier_slabs
                            _rt_couriers = _rt_fetch_providers("COURIER", active_only=True) or []
                        except Exception:
                            _rt_couriers = []
                            _rt_fetch_courier_slabs = lambda *_a, **_k: []
                        _rt_provider_ids = [""] + [str(_p.get("id") or "") for _p in _rt_couriers]

                        def _rt_fmt_courier(_pid):
                            if not _pid:
                                return "— Select Courier Provider —"
                            _p = next((_x for _x in _rt_couriers if str(_x.get("id") or "") == str(_pid)), {})
                            return str(_p.get("provider_name") or _pid)

                        _sc_courier_provider_id = st.selectbox(
                            "Courier provider",
                            _rt_provider_ids,
                            format_func=_rt_fmt_courier,
                            key=f"sc_courier_provider_{_sc_ct}",
                        )
                        _rt_provider = next(
                            (_x for _x in _rt_couriers if str(_x.get("id") or "") == str(_sc_courier_provider_id)),
                            {},
                        )
                        _sc_courier_provider_name = str(_rt_provider.get("provider_name") or "")
                        _rt_slabs = _rt_fetch_courier_slabs(_sc_courier_provider_id, active_only=True) if _sc_courier_provider_id else []
                        _rt_slab_ids = [""] + [str(_s.get("id") or "") for _s in _rt_slabs]
                        _rt_slab_idx = 0
                        if _rt_slabs:
                            _rt_lowest = min(_rt_slabs, key=lambda _s: float(_s.get("charge_base") or 0))
                            _rt_lowest_id = str(_rt_lowest.get("id") or "")
                            _rt_slab_idx = _rt_slab_ids.index(_rt_lowest_id) if _rt_lowest_id in _rt_slab_ids else 0

                        def _rt_fmt_slab(_sid):
                            if not _sid:
                                return "Provider default / manual"
                            _s = next((_x for _x in _rt_slabs if str(_x.get("id") or "") == str(_sid)), {})
                            _code = str(_s.get("parcel_size_code") or "")
                            return (
                                f"{_s.get('option_label') or ''}"
                                + (f" · {_code}" if _code else "")
                                + f" — ₹{float(_s.get('charge_base') or 0):,.2f}"
                            )

                        _sc_courier_rate_option_id = st.selectbox(
                            "Courier charge slab / parcel size",
                            _rt_slab_ids,
                            index=_rt_slab_idx,
                            format_func=_rt_fmt_slab,
                            key=f"sc_courier_slab_{_sc_ct}",
                        )
                        _rt_slab = next(
                            (_x for _x in _rt_slabs if str(_x.get("id") or "") == str(_sc_courier_rate_option_id)),
                            {},
                        )
                        if _rt_slab:
                            _sc_svc["default_price"] = float(_rt_slab.get("charge_base") or 0)
                            _sc_svc["gst_percent"] = float(_rt_slab.get("gst_percent") or _sc_svc.get("gst_percent") or 18)
                            _sc_courier_rate_option_label = str(_rt_slab.get("option_label") or "")
                            _sc_courier_parcel_size = str(_rt_slab.get("parcel_size_code") or "")
                            st.caption("Lowest courier slab is auto-selected. Change dropdown if parcel is bigger.")
                    _sc_c1, _sc_c2, _sc_cq, _sc_c3 = st.columns([3, 1.3, 1.1, 1.3])
                    with _sc_c1:
                        _sc_desc = st.text_input(
                            "Description",
                            value=str(_sc_svc.get("service_name") or ""),
                            placeholder=f"{_sc_cfg['label']} charge",
                            key="sc_desc")
                    with _sc_c2:
                        _sc_amt = st.number_input(
                            "₹ Rate / pair", min_value=0.0, step=10.0,
                            value=float(_sc_svc.get("default_price") or 0),
                            key="sc_amt")
                    with _sc_cq:
                        # Auto-detect pair vs single from current cart
                        _cart_lines_now = st.session_state.get("retail_order_lines") or []
                        _cart_eyes = {str(l.get("eye_side","")).upper()[:1]
                                      for l in _cart_lines_now
                                      if not l.get("is_service_line")}
                        _auto_qty = 1.0 if ("R" in _cart_eyes and "L" in _cart_eyes) else (
                                    0.5 if _cart_eyes else 1.0)
                        _auto_idx = [0.5, 1.0, 1.5, 2.0, 3.0].index(_auto_qty) if _auto_qty in [0.5, 1.0, 1.5, 2.0, 3.0] else 1
                        _sc_qty_factor = st.selectbox(
                            "Qty ⚠️",
                            [0.5, 1.0, 1.5, 2.0, 3.0],
                            index=_auto_idx,
                            format_func=lambda v: (
                                f"{v:g} pair — 1 eye only" if v == 0.5 else
                                f"{v:g} pair — both eyes" if v == 1.0 else
                                f"{v:g} pair"
                            ),
                            key=f"sc_qty_factor_{_sc_ct}",
                            help="Auto-set from cart. 0.5 pair = 1 eye only. 1 pair = both eyes.",
                        )
                    with _sc_c3:
                        _sc_gst = st.number_input(
                            "GST %", min_value=0.0, max_value=28.0,
                            value=float(_sc_svc.get("gst_percent") if _sc_svc else _sc_cfg["default_gst"]),
                            step=0.5, key="sc_gst")
                    _sc_instruction = ""
                    _sc_photo_b64 = ""
                    _sc_photo_name = ""
                    if _sc_group in ("COLOURING", "FITTING"):
                        _sc_instruction = st.text_area(
                            "Special instruction for production / provider",
                            placeholder="Tint shade, sample reference, fitting note, urgency...",
                            key=f"sc_instr_{_sc_ct}",
                            height=70,
                        )
                        if _sc_group == "COLOURING":
                            _sc_photo = st.file_uploader(
                                "Colour sample photograph",
                                type=["jpg", "jpeg", "png", "webp"],
                                key=f"sc_colour_sample_{_sc_ct}",
                            )
                            if _sc_photo:
                                _sc_photo_b64 = _uploaded_image_b64(_sc_photo)
                                _sc_photo_name = _sc_photo.name
                    _sc_s1, _sc_s2 = st.columns(2)
                    with _sc_s1:
                        enter_to_submit()
                        if st.button(f"✅ Add {_sc_qty_factor:g} pair  [Enter]", type="primary",
                                     width='stretch', key="sc_confirm"):
                            if _sc_amt <= 0:
                                st.error("Enter amount > 0")
                            elif _make_service_cart_line is None:
                                st.error("Service charge engine is unavailable. Restart Streamlit and try again.")
                            else:
                                import uuid as _sc_uuid
                                _charge = {
                                    "line_id": str(_sc_uuid.uuid4()),
                                    "type": _sc_group,
                                    "service_group": _sc_group,
                                    "service_code": _service_pick_key(_sc_svc) if _sc_svc else str(_sc_ct).upper(),
                                    "service_def": _sc_svc,
                                    "desc": _sc_desc or _sc_svc.get("service_name") or _sc_cfg["label"],
                                    "amt":  _sc_amt,
                                    "gst":  _sc_gst,
                                    "qty_factor": _sc_qty_factor,
                                    "instruction": _sc_instruction,
                                    "colour_sample_photo": _sc_photo_b64,
                                    "colour_sample_filename": _sc_photo_name,
                                    "courier_provider_id": _sc_courier_provider_id,
                                    "courier_provider_name": _sc_courier_provider_name,
                                    "courier_rate_option_id": _sc_courier_rate_option_id,
                                    "courier_rate_option_label": _sc_courier_rate_option_label,
                                    "courier_parcel_size": _sc_courier_parcel_size,
                                }
                                _pending.append(_charge)
                                st.session_state["_retail_pending_charges"] = _pending
                                _svc_line = _make_service_cart_line(_charge, "RETAIL")
                                _cart_now = list(st.session_state.get("retail_order_lines") or [])
                                _cart_now.append(_svc_line)
                                st.session_state.retail_order_lines = _cart_now
                                st.session_state.pop(_sc_add_key, None)
                                st.rerun()
                    with _sc_s2:
                        if st.button("✕ Cancel",
                                     width='stretch', key="sc_cancel"):
                            st.session_state.pop(_sc_add_key, None)
                            st.session_state.pop(_sc_token_key, None)
                            st.rerun()

                # Running total
                if _pending:
                    _sc_total = sum(
                        float(c["amt"]) * float(c.get("qty_factor") or 1) * (1 + float(c["gst"]) / 100)
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
    _lco = st.session_state.get("last_confirmed_order") if _full_billing_mode else None
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
