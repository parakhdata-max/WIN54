"""
backoffice_panels.py
====================
UI panel components extracted from backoffice_ui.py for maintainability.

Contains:
    render_power_edit_ui          — inline power editing with workflow trigger
    render_lens_params_edit_ui    — lens parameter editing
    render_boxing_params_edit_ui  — boxing/frame measurement editing
    render_allocation_window      — batch allocation editor

These panels are imported by backoffice_ui.py and called directly.
No state or business logic lives here — only UI rendering.
"""

import streamlit as st
import pandas as pd
from typing import Dict, List, Optional

from .backoffice_helpers import (
    fmt_signed,
    get_display_order_id,
    power_key,
    sync_power_to_ui,
    force_power_refresh,
)
from .backoffice_logic import (
    update_manufacturing_power,
    update_line_billing,
    recalculate_order_totals,
    refresh_line_state,
)


def render_power_edit_ui(line: Dict, line_idx: int, order: Dict):
    """
    Renders power editing UI with automatic workflow triggering
    """
    # ── HARD LOCK: job card saved or in production ─────────────────────
    import json as _jbp
    _lp = line.get("lens_params") or {}
    if isinstance(_lp, str):
        try: _lp = _jbp.loads(_lp)
        except: _lp = {}
    _surf = line.get("surfacing_data") or (_lp.get("surfacing_data") if isinstance(_lp, dict) else None)
    if _surf:
        # Check stage — show appropriate message
        _stage_msg = ""
        try:
            from modules.sql_adapter import run_query as _rqbp
            _lid_bp = (line.get("line_id") or line.get("id") or "").strip()
            if _lid_bp:
                _rows_bp = _rqbp(
                    "SELECT current_stage FROM job_master WHERE order_line_id=%(l)s::uuid LIMIT 1",
                    {"l": _lid_bp}
                )
                if _rows_bp:
                    _stage_msg = f" (Stage: {_rows_bp[0].get('current_stage', '?')})"
        except Exception:
            pass
        st.markdown(
            f"<div style='background:#1a0a00;border:2px solid #f97316;"
            f"border-radius:8px;padding:10px 16px;margin:8px 0'>"
            f"<div style='color:#fb923c;font-weight:700'>🔒 Power editing locked</div>"
            f"<div style='color:#fed7aa;font-size:0.82rem;margin-top:4px'>"
            f"A blank has been allocated and job card saved{_stage_msg}.<br>"
            f"Go to <b>Documents → Job Cards</b> to cancel the job card first.</div>"
            f"</div>",
            unsafe_allow_html=True
        )
        # Clear the editing state so next render shows read-only again
        import streamlit as _stbp
        _stbp.session_state.pop("bo_editing_line", None)
        return

    st.markdown(f"#### Edit Power - Line #{line_idx + 1}")

    # =====================================================
    # Detect if Contact Lens (Define First)
    # =====================================================

    main_group = str(line.get("main_group", "")).lower()
    product_type = str(line.get("product_type", "")).lower()

    is_contact = (
        "contact" in main_group or
        product_type == "contact_lens"
    )

    # Store original values for change detection
    original_sph = line.get('sph')
    original_cyl = line.get('cyl', 0)
    original_axis = line.get('axis')
    original_add = line.get('add_power')

    # ── Row 1: SPH + CYL ──────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        new_sph = st.number_input(
            "SPH",
            value=float(original_sph) if original_sph is not None else 0.0,
            step=0.25,
            format="%.2f",
            key=f"edit_sph_{line_idx}_{order.get('order_id', '')}"
        )
    with col2:
        raw_cyl = st.text_input(
            "CYL",
            value="" if pd.isna(line.get("cyl")) or line.get("cyl") in (None, 0) else str(line.get("cyl")),
            key=f"cyl_{line_idx}"
        )
        try:
            new_cyl = float(raw_cyl) if raw_cyl.strip() != "" else None
        except ValueError:
            st.warning("Invalid CYL value")
            new_cyl = None

    # ── Row 2: AXIS + ADD ─────────────────────────────────────────
    col3, col4 = st.columns(2)
    with col3:
        raw_axis = st.text_input(
            "AXIS",
            value="" if pd.isna(line.get("axis")) or line.get("axis") in (None, 0) else str(line.get("axis")),
            key=f"axis_{line_idx}"
        )
        try:
            new_axis = int(raw_axis) if raw_axis.strip() != "" else None
        except ValueError:
            st.warning("Invalid AXIS value")
            new_axis = None

    with col4:
        raw_add = st.text_input(
            "ADD",
            value="" if pd.isna(line.get("add_power")) or line.get("add_power") in (None, 0) else str(line.get("add_power")),
            key=f"add_{line_idx}"
        )
        try:
            new_add = float(raw_add) if raw_add.strip() != "" else None
        except ValueError:
            st.warning("Invalid ADD value")
            new_add = None
    
    # =====================================================
    # Manufacturing Power Control (MOVED UP)
    # =====================================================

    st.markdown("##### Manufacturing Power Control")

    # Vertex option (only for contact lenses)
    if is_contact:
        # Track previous state
        old_use_effective = line.get("use_effective_power", False)
        
        use_effective = st.checkbox(
            "Use Effective (Vertex) Power",
            value=old_use_effective,
            key=f"eff_power_{line_idx}"
        )

        #  FIX: If effective power toggle changed, recalculate and UPDATE sph_out fields
        if use_effective != old_use_effective:

            # DO NOT touch session_state for widget key
            line["use_effective_power"] = use_effective
            line["effectivity_applied"] = use_effective
            
            # Clear manual override to allow recalculation
            line["manual_power_override"] = False
            
            #  CRITICAL: Update the actual sph_out values (not preview)
            # This makes the values appear in the editable SPH OUT fields
            update_manufacturing_power(line)

            
            # Clear any old preview values
            line.pop("_preview_sph_out", None)
            line.pop("_preview_cyl_out", None)
            line.pop("_preview_axis_out", None)
            
            st.success(f" Effective power calculated: SPH OUT = {fmt_signed(line['sph_out'])}")
            st.rerun()
        else:
            line["use_effective_power"] = use_effective
        
        #  NEW: Show "Calculate Effective Power" button
        if use_effective:
            if st.button(" Calculate Effective Power", key=f"calc_eff_{line_idx}", use_container_width=True):
                # Clear manual override
                line["manual_power_override"] = False
                
                #  CRITICAL: Update the actual sph_out values (not preview)
                update_manufacturing_power(line)

                
                st.success(f" Effective power calculated: SPH OUT = {fmt_signed(line['sph_out'])}")
                st.rerun()

    # =====================================================
    # Recalculate Option (Only for Contact Lenses)
    # =====================================================
    
    #  Show indicator if effective power was applied
    if line.get("effectivity_applied"):
        st.info(" These values are calculated using vertex distance correction (effective power). You can edit them manually if needed.")
    elif line.get("manual_power_override"):
        st.success(" **MANUAL OVERRIDE ACTIVE**: These manually entered values will be used for batch allocation and manufacturing.")

    # =====================================================
    # Manufacturing Power Inputs (FINAL FIX)
    # =====================================================

    # ── Manufacturing OUT: SPH + CYL row, then AXIS ──────────────────
    sph_key = power_key(order, line_idx, "sph_out")
    cyl_key = power_key(order, line_idx, "cyl_out")
    axis_key = power_key(order, line_idx, "axis_out")
    col_a, col_b = st.columns(2)
    col_c, _ = st.columns(2)

    #  ALWAYS sync UI from line BEFORE rendering widgets
    st.session_state[sph_key] = float(line.get("sph_out") or 0)
    st.session_state[cyl_key] = float(line.get("cyl_out") or 0)

    axis = line.get("axis_out")
    st.session_state[axis_key] = int(axis) if axis is not None else 0

    # Render widgets WITHOUT value=
    with col_a:
        sph_out = st.number_input(
            "SPH OUT",
            step=0.25,
            format="%.2f",
            key=sph_key
        )

    with col_b:
        cyl_out = st.number_input(
            "CYL OUT",
            step=0.25,
            format="%.2f",
            key=cyl_key
        )

    with col_c:
        axis_out = st.number_input(
            "AXIS OUT",
            min_value=0,
            max_value=180,
            step=1,
            key=axis_key
        )

    # Save old values for comparison
    old_sph_out = float(line.get("sph_out") or 0)
    old_cyl_out = float(line.get("cyl_out") or 0)
    old_axis_out = int(line.get("axis_out") or 0)

    # Detect manual override (but DON'T save values yet - wait for Apply Changes)
    manual_edit_detected = (
        sph_out != old_sph_out or
        cyl_out != old_cyl_out or
        axis_out != old_axis_out
    )

    # Action buttons
    col_save, col_cancel = st.columns(2)
    
    with col_save:
        if st.button(
            " Apply Changes",
            type="primary",
            use_container_width=True,
            key=f"save_power_{line_idx}"
        ):

            # Detect RX change
            power_changed = (
                new_sph != original_sph or
                new_cyl != original_cyl or
                new_axis != original_axis or
                new_add != original_add
            )

            if not power_changed and not manual_edit_detected:
                st.info("No changes detected")
                return

            # =====================================================
            # 1 Update RX (if changed)
            # =====================================================

            if power_changed:

                line["sph"] = new_sph
                line["cyl"] = new_cyl
                line["axis"] = new_axis
                line["add_power"] = new_add if new_add != 0 else None

                # RX changed  clear old override
                line["manual_power_override"] = False

            # =====================================================
            # 2 Manufacturing Power Update (FINAL STABLE)
            # =====================================================
            # PRIORITY ORDER:
            # 1. Manual Override (user edited SPH OUT/CYL OUT/AXIS OUT) - HIGHEST PRIORITY
            # 2. Contact lens with effective power calculation
            # 3. Ophthalmic lens (copy from RX)

            use_eff = line.get("use_effective_power", False)


            # A) Manual override (user typed in SPH OUT) - TAKES PRECEDENCE OVER EVERYTHING
            if manual_edit_detected:

                line["sph_out"] = float(sph_out)
                line["cyl_out"] = float(cyl_out)
                line["axis_out"] = int(axis_out)

                line["manual_power_override"] = True
                line["effectivity_applied"] = False
                
                #  Manual values will be used for batch allocation


            # B) Contact lens  use engine (with/without effectivity)
            elif is_contact:

                line["manual_power_override"] = False
                update_manufacturing_power(line)


            # C) Ophthalmic  copy RX
            else:

                line["sph_out"] = float(line["sph"])
                line["cyl_out"] = float(line["cyl"] or 0)
                line["axis_out"] = int(line["axis"]) if line["axis"] is not None else None

                line["manual_power_override"] = False
                line["effectivity_applied"] = False


            # =====================================================
            # 3 Clear Preview
            # =====================================================

            line.pop("_preview_sph_out", None)
            line.pop("_preview_cyl_out", None)
            line.pop("_preview_axis_out", None)


            # =====================================================
            # 4 Run Workflow
            # =====================================================

            refresh_line_state(line)
            #  FIX 2  Recalculate order totals for BOTH eyes (R + L sync)
            if order:
                recalculate_order_totals(order)

# ============================================================================
# LENS PARAMETERS & BOXING EDIT UI
# ============================================================================

def render_lens_params_edit_ui(order: Dict):
    """
    Render editable lens parameters (shared across R & L eyes).
    These come from retail_punching as JSON fields stored on the order.
    """
    st.markdown("####  Lens Parameters")
    
    # Get current lens_params from order (comes from retail as JSON)
    lens_params = order.get('lens_params') or {}
    
    # Frame Type (radio, 3 options)
    frame_options = ["Full", "Supra", "Rimless"]
    current_frame = lens_params.get('frame_type', 'Full')
    if current_frame not in frame_options:
        current_frame = "Full"
    
    col1, col2 = st.columns(2)
    
    with col1:
        frame_type = st.radio(
            "Frame Type",
            options=frame_options,
            index=frame_options.index(current_frame),
            horizontal=True,
            key=f"bo_frame_type_{get_display_order_id(order)}"
        )
    
    with col2:
        # Thickness (radio)
        thickness_options = ["Regular", "Thin", "Cartier Thick"]
        current_thick = lens_params.get('thickness', 'Regular')
        if current_thick not in thickness_options:
            current_thick = "Regular"
        
        thickness = st.radio(
            "Thickness",
            options=thickness_options,
            index=thickness_options.index(current_thick),
            horizontal=True,
            key=f"bo_thickness_{get_display_order_id(order)}"
        )
    
    col1, col2 = st.columns(2)
    
    with col1:
        # Tinted (radio)
        tint_options = ["No", "Yes"]
        current_tint = lens_params.get('tinted', 'No')
        if current_tint not in tint_options:
            current_tint = "No"
        
        tinted = st.radio(
            "Tinted",
            options=tint_options,
            index=tint_options.index(current_tint),
            horizontal=True,
            key=f"bo_tinted_{get_display_order_id(order)}"
        )
    
    with col2:
        # Corridor (dropdown)
        corridor_options = ["", "Short", "Medium", "Long"]
        current_cor = lens_params.get('corridor', '')
        if current_cor not in corridor_options:
            current_cor = ""
        
        corridor = st.selectbox(
            "Corridor (Progressive)",
            options=corridor_options,
            index=corridor_options.index(current_cor),
            key=f"bo_corridor_{get_display_order_id(order)}"
        )
    
    st.markdown("---")
    
    col1, col2 = st.columns(2)
    
    with col1:
        # Diameter (dropdown)
        dia_options = ["", "55", "60", "65", "70", "75"]
        current_dia = lens_params.get('diameter', '')
        if current_dia not in dia_options:
            current_dia = ""
        
        diameter = st.selectbox(
            "Diameter",
            options=dia_options,
            index=dia_options.index(current_dia),
            key=f"bo_diameter_{get_display_order_id(order)}"
        )
    
    with col2:
        # Fitting Height (dropdown)
        fh_options = ["", "12", "14", "16", "18", "20", "22", "24"]
        current_fh = lens_params.get('fitting_height', '')
        if current_fh not in fh_options:
            current_fh = ""
        
        fitting_height = st.selectbox(
            "Fitting Height",
            options=fh_options,
            index=fh_options.index(current_fh),
            key=f"bo_fitting_height_{get_display_order_id(order)}"
        )
    
    st.markdown("---")
    
    # Instructions (text area)
    instructions = st.text_area(
        " Instructions to Lab",
        value=lens_params.get('instructions', ''),
        height=90,
        placeholder="Any special instructions for the lab...",
        key=f"bo_instructions_{get_display_order_id(order)}"
    )
    
    # Save button
    from modules.utils.submit_guard import is_locked, guarded_submit
    _oid = get_display_order_id(order)
    if st.button(" Save Lens Parameters", type="primary", use_container_width=True,
                 key=f"save_lens_params_{_oid}", disabled=is_locked(f"lens_{_oid}")):
        with guarded_submit(f"lens_{_oid}") as _allowed:
            if not _allowed:
                st.stop()
            order['lens_params'] = {
                'frame_type': frame_type,
                'thickness': thickness,
                'tinted': tinted,
                'corridor': corridor,
                'diameter': diameter,
                'fitting_height': fitting_height,
                'instructions': instructions,
            }
            st.success(" Lens parameters updated!")
            st.rerun()


def render_boxing_params_edit_ui(order: Dict):
    """
    Render editable boxing/frame measurements (shared across R & L eyes).
    These come from retail_punching as JSON fields stored on the order.
    """
    st.markdown("####  Boxing / Frame Measurements")
    
    # Get current boxing_params from order
    boxing_params = order.get('boxing_params') or {}
    
    # Row 1: A Box | B Box | ED | ED Axis | DBL
    st.markdown("##### Frame Dimensions")
    c1, c2, c3, c4, c5 = st.columns(5)
    
    with c1:
        a_box = st.number_input(
            "A Box (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('a_box') or 0.0),
            step=0.1, format="%.1f",
            key=f"bo_a_box_{get_display_order_id(order)}"
        )
    
    with c2:
        b_box = st.number_input(
            "B Box (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('b_box') or 0.0),
            step=0.1, format="%.1f",
            key=f"bo_b_box_{get_display_order_id(order)}"
        )
    
    with c3:
        ed = st.number_input(
            "ED (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('ed') or 0.0),
            step=0.1, format="%.1f",
            key=f"bo_ed_{get_display_order_id(order)}"
        )
    
    with c4:
        ed_axis = st.number_input(
            "ED Axis ()", min_value=0, max_value=180,
            value=int(boxing_params.get('ed_axis') or 0),
            step=1,
            key=f"bo_ed_axis_{get_display_order_id(order)}"
        )
    
    with c5:
        dbl = st.number_input(
            "DBL (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('dbl') or 0.0),
            step=0.1, format="%.1f",
            key=f"bo_dbl_{get_display_order_id(order)}"
        )
    
    st.markdown("##### PD & Fitting Heights")
    # Row 2: R PD | L PD | IPD | Fitting Ht R | Fitting Ht L
    c1, c2, c3, c4, c5 = st.columns(5)
    
    with c1:
        r_pd = st.number_input(
            "R PD (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('r_pd') or 0.0),
            step=0.5, format="%.1f",
            key=f"bo_r_pd_{get_display_order_id(order)}"
        )
    
    with c2:
        l_pd = st.number_input(
            "L PD (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('l_pd') or 0.0),
            step=0.5, format="%.1f",
            key=f"bo_l_pd_{get_display_order_id(order)}"
        )
    
    with c3:
        ipd = st.number_input(
            "IPD (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('ipd') or 0.0),
            step=0.5, format="%.1f",
            key=f"bo_ipd_{get_display_order_id(order)}"
        )
    
    with c4:
        fitting_ht_r = st.number_input(
            "Fitting Ht R (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('fitting_ht_r') or 0.0),
            step=0.5, format="%.1f",
            key=f"bo_fitting_ht_r_{get_display_order_id(order)}"
        )
    
    with c5:
        fitting_ht_l = st.number_input(
            "Fitting Ht L (mm)", min_value=0.0, max_value=99.9,
            value=float(boxing_params.get('fitting_ht_l') or 0.0),
            step=0.5, format="%.1f",
            key=f"bo_fitting_ht_l_{get_display_order_id(order)}"
        )
    
    st.markdown("##### Angles & Distances")
    # Row 3: Panto | Tilt | BVD
    c1, c2, c3 = st.columns(3)
    
    with c1:
        panto = st.number_input(
            "Panto ()", min_value=0.0, max_value=25.0,
            value=float(boxing_params.get('panto') or 0.0),
            step=0.5, format="%.1f",
            key=f"bo_panto_{get_display_order_id(order)}"
        )
    
    with c2:
        tilt = st.number_input(
            "Tilt ()", min_value=0.0, max_value=25.0,
            value=float(boxing_params.get('tilt') or 0.0),
            step=0.5, format="%.1f",
            key=f"bo_tilt_{get_display_order_id(order)}"
        )
    
    with c3:
        bvd = st.number_input(
            "BVD (mm)", min_value=0.0, max_value=30.0,
            value=float(boxing_params.get('bvd') or 0.0),
            step=0.5, format="%.1f",
            key=f"bo_bvd_{get_display_order_id(order)}"
        )
    
    # Save button
    from modules.utils.submit_guard import is_locked, guarded_submit
    _oid_box = get_display_order_id(order)
    if st.button(" Save Boxing Parameters", type="primary", use_container_width=True,
                 key=f"save_boxing_params_{_oid_box}", disabled=is_locked(f"boxing_{_oid_box}")):
        with guarded_submit(f"boxing_{_oid_box}") as _allowed:
            if not _allowed:
                st.stop()
            order['boxing_params'] = {
                'a_box': round(a_box, 1),
                'b_box': round(b_box, 1),
                'ed': round(ed, 1),
                'ed_axis': int(ed_axis),
                'dbl': round(dbl, 1),
                'r_pd': round(r_pd, 1),
                'l_pd': round(l_pd, 1),
                'ipd': round(ipd, 1),
                'fitting_ht_r': round(fitting_ht_r, 1),
                'fitting_ht_l': round(fitting_ht_l, 1),
                'panto': round(panto, 1),
                'tilt': round(tilt, 1),
                'bvd': round(bvd, 1),
            }
            st.success(" Boxing parameters updated!")
            st.rerun()

# ============================================================================
# ============================================================================

def render_allocation_window(line: Dict, line_idx: int, order: Dict):

    from modules.batch_manager import get_available_stock
    from modules.utils.power_normalizer import normalize_power
    from modules.loaders.ophthalmic_adapter import (
        get_ophthalmic_for_punching, check_ophthalmic_availability,
        IN_STOCK as OPHL_IN_STOCK, RX_ORDER as OPHL_RX_ORDER,
    )

    st.markdown("---")
    st.markdown("###  Batch Allocation Editor")

    col_info, col_close = st.columns([4, 1])

    with col_info:
        st.markdown(f"**Product:** {line.get('product_name', 'N/A')}")
        st.caption(
            f"Eye: {line.get('eye_side')} | "
            f"Power: SPH {fmt_signed(line.get('sph'))} "
            f"CYL {fmt_signed(line.get('cyl'))}"
        )

    with col_close:
        if st.button(" Close", key=f"close_alloc_{line_idx}", width="stretch"):
            st.session_state.bo_show_allocation_window = False
            st.session_state.bo_allocation_line_idx = None
            st.rerun()

    st.markdown("---")

    col_batches, col_calc = st.columns([3, 2])

    # ======================================================
    # LEFT PANEL  STOCK
    # ======================================================
    with col_batches:
        st.markdown("####  Available Batches")

        product_id = str(line.get("product_id"))

        #  UNIVERSAL POWER NORMALIZATION (R + L FIX)
        sph = normalize_power(line.get("sph"))
        cyl = normalize_power(line.get("cyl"))
        axis = normalize_power(line.get("axis"))
        add_power = normalize_power(line.get("add_power"))
        eye_side = line.get("eye_side")

        # Axis 0 must behave like NULL
        if axis == 0:
            axis = None

        stock_df = get_available_stock(
            product_id=product_id,
            sph=sph,
            cyl=cyl,
            axis=axis,
            add_power=add_power,
            eye_side=eye_side
        )

        if stock_df.empty:
            # ── Ophthalmic RX line — show price info, no batch allocation needed ──
            _item_type = line.get('lens_item_type', '')
            if _item_type == 'RX':
                st.info(
                    f"📋 **RX Order Lens** — ordered per job, no shelf stock.  \n"
                    f"Price: ₹{float(line.get('unit_price') or 0):.0f} per piece  \n"
                    f"Status will update when lens is received from supplier."
                )
                avail = check_ophthalmic_availability(
                    product_id, sph=sph, cyl=cyl, axis=axis,
                    add_power=add_power, eye_side=eye_side or 'B'
                )
                if avail['status'] == 'STOCK':
                    st.success(f"✅ Stock now available: {avail['available_qty']} unit(s) · ₹{avail['selling_price']:.0f}")
                    st.caption("You can now allocate from stock or mark as fulfilled.")
            else:
                st.warning("⚠️ No batches available for this power")
            return

        key = f"temp_allocation_{line_idx}"
        if key not in st.session_state:
            st.session_state[key] = line.get("batch_allocation", []).copy()

        temp_alloc = st.session_state[key]

        # Product metadata needed for BOXPCS price normalization
        prod_unit     = str(line.get("unit") or "PCS").upper()
        prod_box_size = int(line.get("box_size") or 1)

        #  Fallback: infer box_size from existing allocation if not on line 
        # If line.box_size wasn't hydrated from product master, derive it by
        # comparing the batch selling_price (raw BOX price) against the line's
        # known unit_price (already PCS-normalized by retail).
        if prod_box_size == 1 and prod_unit == "BOX":
            existing_alloc = line.get("batch_allocation", [])
            if existing_alloc:
                batch_raw   = float(existing_alloc[0].get("selling_price") or 0)
                line_pcs    = float(line.get("unit_price") or 0)
                if batch_raw > 0 and line_pcs > 0 and batch_raw > line_pcs:
                    inferred = round(batch_raw / line_pcs)
                    if inferred > 1:
                        prod_box_size = inferred

        is_box_prod = (prod_unit == "BOX" and prod_box_size > 1)

        # Render batches
        for _, row in stock_df.iterrows():

            batch_no      = row.get("batch_no")
            available_qty = int(row.get("available_qty", 0) or 0)

            # Price column priority depends on order_type:
            #   RETAIL    → mrp first (GST-inclusive, what patient pays)
            #   WHOLESALE → selling_price first (trade price)
            _ot = str(order.get("order_type") or "RETAIL").upper()
            _price_cols = (
                ("unit_price", "mrp", "selling_price", "price", "rate")
                if _ot == "RETAIL"
                else ("unit_price", "selling_price", "price", "rate", "mrp")
            )
            raw_price = 0.0
            for price_col in _price_cols:
                val = row.get(price_col)
                if val is not None:
                    try:
                        v = float(val)
                        if v > 0:
                            raw_price = v
                            break
                    except (TypeError, ValueError):
                        pass

            # Last resort  product master / historical price
            if raw_price == 0:
                from .backoffice_helpers import get_max_historical_price
                from modules.utils.power_normalizer import normalize_power as _np
                raw_price = get_max_historical_price(
                    product_id=str(line.get("product_id")),
                    sph=_np(line.get("sph_out") or line.get("sph")),
                    cyl=_np(line.get("cyl_out") or line.get("cyl")),
                    axis=_np(line.get("axis_out") or line.get("axis")),
                    add_power=_np(line.get("add_power")),
                    eye_side=line.get("eye_side"),
                ) or 0.0

            # 
            # DB stores price per BOX; billing is always per PCS
            # 
            if is_box_prod and prod_box_size > 0:
                pcs_price = round(raw_price / prod_box_size, 2)
            else:
                pcs_price = round(raw_price, 2)

            existing = next(
                (a for a in temp_alloc if a.get("batch_no") == batch_no),
                None
            )
            current_qty = existing.get("allocated_qty", 0) if existing else 0

            with st.container(border=True):

                c1, c2 = st.columns([3, 2])

                with c1:
                    st.markdown(f"**{batch_no}**")
                    st.caption(f"Available: {available_qty}")
                    st.caption(f"Price: {pcs_price:.2f}")

                with c2:
                    qty = st.number_input(
                        "Allocate",
                        min_value=0,
                        max_value=available_qty,
                        value=current_qty,
                        key=f"alloc_{line_idx}_{batch_no}",
                        label_visibility="collapsed"
                    )

                    if qty > 0:
                        if existing:
                            existing["allocated_qty"] = qty
                            # Always sync pcs_price  existing entry may have stale/None price
                            existing["selling_price"] = pcs_price
                            existing["unit_price"]    = pcs_price
                        else:
                            temp_alloc.append({
                                "batch_no":      batch_no,
                                "allocated_qty":  qty,
                                #  selling_price = PCS price (what pricing engine reads)
                                #  unit_price    = same (fallback for display)
                                "selling_price":  pcs_price,
                                "unit_price":     pcs_price,
                                # Pass PCS unit+box_size so engine's normalize_to_pcs is a no-op
                                "unit":           "PCS",
                                "box_size":       1,
                                "product_id":     line.get("product_id"),
                                "product_name":   line.get("product_name"),
                            })
                    else:
                        if existing:
                            temp_alloc.remove(existing)

        st.session_state[key] = temp_alloc

    # ======================================================
    # RIGHT PANEL  CALCULATIONS
    # ======================================================
    with col_calc:
        st.markdown("####  Live Calculations")

        temp_alloc = st.session_state.get(f"temp_allocation_{line_idx}", [])

        total_alloc = sum(a.get("allocated_qty", 0) for a in temp_alloc)
        billing_qty = int(line.get("billing_qty", 1))
        pending = max(0, billing_qty - total_alloc)

        total_cost = sum(
            int(a.get("allocated_qty") or 0) * float(a.get("selling_price") or a.get("unit_price") or 0)
            for a in temp_alloc
        )

        st.metric("Required", billing_qty)
        st.metric("Allocated", total_alloc)
        st.metric("Pending", pending)
        st.metric("Cost", f"{total_cost:.2f}")

        st.markdown("---")

        from modules.utils.submit_guard import is_locked, guarded_submit
        _alloc_key = f"alloc_{line_idx}"
        if st.button(" Save Allocation", type="primary", width="stretch",
                     disabled=is_locked(_alloc_key)):
            with guarded_submit(_alloc_key) as _allowed:
                if not _allowed:
                    st.stop()
                # Do NOT call refresh_line_state — it wipes manual allocation
                line["batch_allocation"] = temp_alloc.copy()
                line["allocated_qty"]    = total_alloc

                billing_qty = int(line.get("billing_qty", 0))
                if total_alloc == 0:
                    line["batch_status"]        = "PENDING"
                    line["manufacturing_route"] = "VENDOR"
                elif total_alloc < billing_qty:
                    line["batch_status"]        = "PARTIAL"
                    line["manufacturing_route"] = "VENDOR"
                else:
                    line["batch_status"]        = "ALLOCATED"
                    line["manufacturing_route"] = "STOCK"

                # reprice_from_batch=True: derive unit_price from chosen batch
                line.pop("billing_total", None)
                line.pop("pricing_applied_at", None)
                update_line_billing(line, reprice_from_batch=True)
                recalculate_order_totals(order)

                from .backoffice_helpers import categorize_order_lines
                categorize_order_lines(order)

                # ── Write allocation to DB immediately ─────────────────────────
                # The in-memory line dict is correct but the order reload from DB
                # would reset allocated_qty to 0 unless we persist it now.
                _line_id_aw = str(line.get("line_id") or line.get("id") or "")
                # Fallback: look up line_id from DB if not in line dict
                if not _line_id_aw or len(_line_id_aw) <= 10:
                    try:
                        _oid_aw = str(order.get("id") or "")
                        _pid_aw = str(line.get("product_id") or "")
                        _eye_aw = str(line.get("eye_side") or "")
                        if _oid_aw and _pid_aw:
                            from modules.sql_adapter import run_query as _rq_lid
                            _lid_rows = _rq_lid("""
                                SELECT id::text FROM order_lines
                                WHERE order_id=%(oid)s::uuid
                                  AND product_id=%(pid)s::uuid
                                  AND UPPER(eye_side)=UPPER(%(eye)s)
                                  AND COALESCE(is_deleted,FALSE)=FALSE
                                LIMIT 1
                            """, {"oid": _oid_aw, "pid": _pid_aw, "eye": _eye_aw}) or []
                            if _lid_rows:
                                _line_id_aw = _lid_rows[0]["id"]
                                line["line_id"] = _line_id_aw  # cache it
                    except Exception:
                        pass
                if _line_id_aw and len(_line_id_aw) > 10:
                    try:
                        import json as _jawn
                        from modules.sql_adapter import run_write as _rw_aw, run_query as _rq_aw
                        # Fetch current lens_params from DB
                        _lp_aw_row = _rq_aw(
                            "SELECT COALESCE(lens_params,'{}')::text AS lp "
                            "FROM order_lines WHERE id=%(lid)s::uuid LIMIT 1",
                            {"lid": _line_id_aw}
                        ) or []
                        _lp_aw = _jawn.loads(_lp_aw_row[0]["lp"]) if _lp_aw_row else {}
                        _lp_aw["batch_allocation"]     = temp_alloc.copy()
                        _lp_aw["batch_status"]         = line["batch_status"]
                        _lp_aw["manufacturing_route"]  = line["manufacturing_route"]
                        # manufacturing_route lives in lens_params, not a column
                        import json as _jawn2
                        _rw_aw("""
                            UPDATE order_lines
                            SET allocated_qty       = %(aq)s,
                                batch_status        = %(bs)s,
                                suggested_allocation = %(sa)s::jsonb,
                                lens_params         = %(lp)s::jsonb
                            WHERE id = %(lid)s::uuid
                        """, {
                            "aq":  total_alloc,
                            "bs":  line["batch_status"],
                            "sa":  _jawn2.dumps(temp_alloc.copy()),
                            "lp":  _jawn.dumps(_lp_aw),
                            "lid": _line_id_aw,
                        })
                    except Exception as _aw_err:
                        import logging as _awlog, traceback as _awtb
                        _awlog.getLogger(__name__).warning(
                            "[alloc_window] DB write failed: "
                            + str(_aw_err) + "\n" + _awtb.format_exc()
                        )
                        st.warning(f"⚠️ Allocation saved to screen but DB write failed: {_aw_err}")

                # ── Sync bo_assignments so Confirm All sees the new allocation ──
                # Without this, assignment panel still has empty batch_allocation
                # and would overwrite the line's allocation on "Confirm All"
                _ba_sync = line.get("batch_allocation", [])
                _aq_sync = line.get("allocated_qty", 0)
                _rt_sync = line.get("manufacturing_route", "STOCK")
                _assignments = st.session_state.get("bo_assignments", {})
                # Find the assignment key for this line
                for _ak, _av in _assignments.items():
                    # Match by line position index
                    if str(line_idx) in str(_ak):
                        _av["batch_allocation"] = _ba_sync
                        _av["route"]            = _rt_sync
                        _av["confirmed"]        = _aq_sync > 0
                        break

                st.session_state.pop(f"temp_allocation_{line_idx}", None)
                st.success(" Allocation Saved")
                st.session_state.bo_show_allocation_window = False
                st.rerun()

        if st.button(" Reset", width="stretch"):
            st.session_state.pop(f"temp_allocation_{line_idx}", None)
            st.rerun()

# ============================================================================
# SUPPLIER ORDER INTEGRATION - ENHANCED WITH DIAGNOSTICS
# ============================================================================

