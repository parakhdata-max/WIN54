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

# ---------------------------------------------------------------------------
# Power availability bridge for backoffice edits
# ---------------------------------------------------------------------------
try:
    from modules.batch_manager import check_stock_availability as _bo_check_stock_availability
except Exception:
    _bo_check_stock_availability = None

try:
    from modules.power_intelligence_ui import render_range_check as _bo_render_range_check
    from modules.power_intelligence import is_colour_product as _bo_is_colour_product
except Exception:
    _bo_render_range_check = None
    def _bo_is_colour_product(_name):
        return False


def _bo_power_availability_check(line: Dict, *, sph, cyl, axis, add_power) -> bool:
    """Validate edited power against Product + Power Range and stock availability.

    Returns True when save may continue. Out-of-range powers block save;
    out-of-stock powers only warn because backoffice may still route the line
    to supplier / RX production.
    """
    pid = str(line.get("product_id") or "").strip()
    pname = str(line.get("product_name") or "")
    eye = str(line.get("eye_side") or "").upper()[:1] or "R"
    if not pid:
        return True

    # 1) Product power range check — this uses the same Product + Power Range
    # manager logic already visible in retail/wholesale punching.
    # render_range_check signature: (product_id, product_name, sph, cyl, axis,
    # is_colour, eye). It does NOT accept add_power — the engine derives ADD
    # eligibility from the product master separately.
    in_range = True
    if _bo_render_range_check is not None:
        try:
            in_range = bool(_bo_render_range_check(
                product_id=pid,
                product_name=pname,
                sph=float(sph or 0),
                cyl=float(cyl or 0),
                axis=int(axis or 0),
                is_colour=_bo_is_colour_product(pname),
                eye={"R": "RIGHT", "L": "LEFT"}.get(eye, eye),
            ))
        except Exception:
            in_range = True

    if not in_range:
        st.error(
            "⛔ Edited power is outside the product power range configured "
            "in Product / Inventory Manager. Save blocked."
        )
        return False

    # 2) Stock availability check — warn only. Backoffice correction should not
    # be blocked just because the item needs supplier/RX procurement.
    if _bo_check_stock_availability is not None:
        try:
            lp = line.get("lens_params") or {}
            if isinstance(lp, str):
                import json as _json
                try:
                    lp = _json.loads(lp) or {}
                except Exception:
                    lp = {}
            coating = lp.get("coating") or line.get("coating")
            qty = int(line.get("billing_qty") or line.get("quantity") or 1)
            av = _bo_check_stock_availability(
                pid,
                sph=sph,
                cyl=cyl,
                axis=axis,
                add_power=add_power,
                eye_side=eye,
                required_qty=qty,
                coating=coating if coating else None,
            ) or {}
            avail_qty = int(av.get("available_qty") or av.get("qty") or 0)
            if avail_qty >= qty:
                st.success(f"✅ Power available in stock: {avail_qty} pcs")
            else:
                st.warning(
                    f"⚠️ Power is valid but stock is short: {avail_qty} available, "
                    f"{qty} required. Route to supplier/RX/procurement after saving."
                )
        except Exception:
            pass

    return True


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
        # Check job_master stage before locking.
        # If the job card is CANCELLED (rolled back from production), the lock
        # must NOT apply — surfacing_data in lens_params is stale and the order
        # is back in backoffice for re-editing. Clear the stale key and continue.
        _stage_msg   = ""
        _job_stage   = None
        _lid_bp      = (line.get("line_id") or line.get("id") or "").strip()
        try:
            from modules.sql_adapter import run_query as _rqbp
            if _lid_bp:
                _rows_bp = _rqbp(
                    "SELECT current_stage FROM job_master "
                    "WHERE order_line_id=%(l)s::uuid "
                    "ORDER BY updated_at DESC NULLS LAST LIMIT 1",
                    {"l": _lid_bp}
                )
                if _rows_bp:
                    _job_stage  = str(_rows_bp[0].get("current_stage") or "")
                    _stage_msg  = f" (Stage: {_job_stage})"
        except Exception:
            pass

        # CANCELLED job or no job rows at all — clear stale surfacing_data so power edit opens normally
        if not _job_stage or _job_stage in ("CANCELLED", "VOID", ""):
            try:
                import json as _jbp2
                from modules.sql_adapter import run_write as _rwbp
                if isinstance(_lp, dict) and _lid_bp:
                    _lp.pop("surfacing_data", None)
                    _lp.pop("blank_id", None)
                    _rwbp(
                        "UPDATE order_lines SET lens_params=%(lp)s::jsonb "
                        "WHERE id=%(lid)s::uuid",
                        {"lp": _jbp2.dumps(_lp), "lid": _lid_bp},
                    )
            except Exception:
                pass
            # Fall through — do NOT return, allow power edit to render below
        else:
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

    # ── Compact RX row: SPH + CYL + AXIS + ADD ────────────────────
    col1, col2, col3, col4 = st.columns(4)
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
    # Manufacturing power is job-card logic. Backoffice only edits RX here;
    # sph_out/cyl_out/axis_out are restamped internally during save.
    manual_edit_detected = False

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

            # F1: hard range check (SPH ±30, CYL ±10, AXIS 1-180, ADD 0.5-4.0)
            # before any product-range or stock check.  This mirrors the validator
            # in retail_punching_rx but applied to the backoffice edit path.
            if power_changed:
                from modules.backoffice.backoffice_helpers import validate_backoffice_power
                _pw_range_errs = validate_backoffice_power(new_sph, new_cyl, new_axis, new_add)
                for _pw_range_err in _pw_range_errs:
                    st.error(_pw_range_err)
                if _pw_range_errs:
                    return  # block save — user must correct values first

            # Backoffice power edits must be checked against the same Product +
            # Power Range / Inventory availability feature used while punching.
            if power_changed:
                if not _bo_power_availability_check(
                    line,
                    sph=new_sph,
                    cyl=new_cyl,
                    axis=new_axis,
                    add_power=new_add,
                ):
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
            # 2 Hidden manufacturing-power restamp
            # =====================================================
            # Backoffice is the final RX correction surface. Manufacturing
            # powers remain downstream/job-card data, so we do not expose
            # editable SPH OUT/CYL OUT/AXIS OUT here. Still restamp them so
            # job cards, allocation and production receive the corrected RX.
            if is_contact:
                line["manual_power_override"] = False
                update_manufacturing_power(line)
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

            # =====================================================
            # 5 Persist to DB (the missing piece — without this,
            #   reload pulls old powers back from order_lines)
            # =====================================================

            _persist_power_change(line, order)

            # F2: write to audit_log so power corrections appear in History tab.
            # original_sph/cyl/axis/add captured above before in-memory update.
            try:
                from modules.backoffice.audit_logger import audit, AuditAction
                audit(
                    AuditAction.PRICE_OVERRIDE,
                    entity="order_lines",
                    entity_id=str(line.get("line_id") or line.get("id") or ""),
                    order_id=str(order.get("id") or ""),
                    payload={
                        "action":    "power_edit",
                        "old_value": (
                            f"SPH {original_sph} CYL {original_cyl} "
                            f"AXIS {original_axis} ADD {original_add}"
                        ),
                        "new_value": (
                            f"SPH {new_sph} CYL {new_cyl} "
                            f"AXIS {new_axis} ADD {new_add}"
                        ),
                        "order_no":  str(order.get("order_no") or ""),
                    },
                )
            except Exception as _f2_err:
                import logging as _f2log
                _f2log.getLogger(__name__).debug(
                    "Power edit audit write failed (non-fatal): %s", _f2_err
                )

            # =====================================================
            # 6 Exit edit mode + rerun so user sees the saved values
            # =====================================================

            try:
                st.session_state.pop("bo_editing_line", None)
            except Exception:
                pass
            st.success(" Power saved.")
            st.rerun()

        with col_cancel:
            if st.button(
                " Cancel",
                use_container_width=True,
                key=f"cancel_power_{line_idx}"
            ):
                try:
                    st.session_state.pop("bo_editing_line", None)
                except Exception:
                    pass
                st.rerun()


def _persist_power_change(line: Dict, order: Dict) -> None:
    """Write power edit to order_lines and bust loader caches.

    Mirrors the contract used by order_edit_view.py — writes RX (sph/cyl/
    axis/add) plus manufacturing OUT values into lens_params so reload sees
    them. Also resets allocation since changing power invalidates the
    previously chosen blank.

    Soft-fail: any DB error surfaces a Streamlit warning but does not raise.
    """
    line_id = str(line.get("line_id") or line.get("id") or "").strip()
    if not line_id or len(line_id) < 10:
        st.warning(" Power changed on screen but line has no DB id  skipping persist.")
        return

    # ── PRICE INTEGRITY PRE-CONDITION (power change) ──────────────────────
    # The power UPDATE itself does not write unit_price, but if the line is
    # ALREADY in a unit_price=0 state on screen (from an earlier broken edit
    # or stale session) the power save will quietly leave that broken row
    # in place. Run the shared write guard so a sibling line with the same
    # product mirrors its price into this line FIRST, then proceed.
    try:
        from modules.backoffice.backoffice_ui import _guard_line_price_before_write
        _g_ok, _g_msg, _, _ = _guard_line_price_before_write(
            line, order, all_lines=None, context="power-change",
        )
        if not _g_ok:
            # Error already shown to user. Do NOT persist power on a line
            # whose price is invalid — that would compound the corruption.
            return
    except Exception as _ge:
        # Guard import failure must not silently disable the gate. Log
        # and continue (the power UPDATE itself is unchanged), but make
        # the failure visible.
        import logging
        logging.getLogger(__name__).warning(
            "[power-change] price guard unavailable, save proceeding: %s", _ge
        )

    try:
        import json as _json
        from modules.sql_adapter import run_write as _rw, run_query as _rq

        # Merge in-memory lens_params over current DB copy
        _row = _rq(
            "SELECT COALESCE(lens_params,'{}')::text AS lp "
            "FROM order_lines WHERE id=%(lid)s::uuid LIMIT 1",
            {"lid": line_id}
        ) or []
        if _row:
            try:
                _lp_db = _json.loads(_row[0].get("lp") or "{}") or {}
            except Exception:
                _lp_db = {}
        else:
            _lp_db = {}

        _lp_mem = line.get("lens_params") or {}
        if isinstance(_lp_mem, str):
            try: _lp_mem = _json.loads(_lp_mem) or {}
            except Exception: _lp_mem = {}
        _lp_merged = {**_lp_db, **_lp_mem}

        # Stamp manufacturing OUT into lens_params so workflows downstream
        # (job card surfacing, blank picker) see the corrected powers.
        _lp_merged["sph_out"]                = line.get("sph_out")
        _lp_merged["cyl_out"]                = line.get("cyl_out")
        _lp_merged["axis_out"]               = line.get("axis_out")
        _lp_merged["manual_power_override"]  = bool(line.get("manual_power_override"))
        _lp_merged["effectivity_applied"]    = bool(line.get("effectivity_applied"))
        _lp_merged["use_effective_power"]    = bool(line.get("use_effective_power"))
        # Power changed  any prior batch allocation/surfacing is stale
        _lp_merged["batch_allocation"]       = []
        _lp_merged["batch_status"]           = "PENDING"
        _lp_merged.pop("surfacing_data", None)

        _sph  = line.get("sph")
        _cyl  = line.get("cyl")
        _axis = line.get("axis")
        _add  = line.get("add_power")

        _rw("""
            UPDATE order_lines
            SET sph              = %(sph)s,
                cyl              = %(cyl)s,
                axis             = %(axis)s,
                add_power        = %(add)s,
                lens_params      = %(lp)s::jsonb,
                allocated_qty    = 0,
                batch_status     = 'PENDING',
                suggested_allocation = NULL
            WHERE id = %(lid)s::uuid
        """, {
            "sph":  float(_sph) if _sph is not None else None,
            "cyl":  float(_cyl) if _cyl is not None else None,
            "axis": int(_axis)  if _axis is not None else None,
            "add":  float(_add) if _add is not None else None,
            "lp":   _json.dumps(_lp_merged),
            "lid":  line_id,
        })

        # ── Bust loader caches so dashboard + reopen show new powers ────
        _clear_order_loader_caches_for_panels()

        # ── FIX (Order-not-found after power change) ─────────────────────
        # Same id-type bug as product-change: after the cache clear the next
        # render falls to load_single_order(order_id), which casts to ::uuid
        # and returns nothing if order_id is the DISPLAY number, not the UUID.
        # Stash the real DB UUID of this order so the fallback path in
        # backoffice_ui.render_order_detail can resolve by UUID. Pure safety
        # — no existing logic changed.
        try:
            _real_db_id = (
                order.get("id")
                or order.get("order_id")
                or order.get("order_uuid")
            )
            if _real_db_id:
                st.session_state["bo_reload_db_id"] = str(_real_db_id)
        except Exception:
            pass

    except Exception as _e:
        import logging, traceback
        logging.getLogger(__name__).warning(
            "[power_change] DB write failed: " + str(_e) + "\n" + traceback.format_exc()
        )
        st.warning(f" Power changed on screen but DB write failed: {_e}")


def _clear_order_loader_caches_for_panels() -> None:
    """Same cache-clear pattern used everywhere else after order_lines writes."""
    try:
        from . import order_loader as _ol
        for _fn_name in ("load_single_order", "load_orders_from_database", "load_orders_summary"):
            _fn = getattr(_ol, _fn_name, None)
            if _fn is not None and hasattr(_fn, "clear"):
                try:
                    _fn.clear()
                except Exception:
                    pass
    except Exception:
        pass
    try:
        st.session_state["bo_orders_loaded"] = False
    except Exception:
        pass


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
                # F4: capture old allocation BEFORE overwriting so we can
                # transfer allocated_qty on inventory_stock after the DB write.
                _old_alloc_f4 = list(line.get("batch_allocation") or [])

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

                from .backoffice_helpers import categorize_order_lines, apply_schemes_to_order_lines, apply_cart_schemes_to_order_lines
                try:
                    apply_schemes_to_order_lines(
                        order,
                        party_id=str(order.get("party_id") or order.get("customer_id") or "")
                    )
                    apply_cart_schemes_to_order_lines(
                        order,
                        party_id=str(order.get("party_id") or order.get("customer_id") or "")
                    )
                except Exception:
                    pass
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
                    else:
                        # F4: Transfer allocated_qty on inventory_stock for changed batches.
                        # Release old allocations, claim new ones — by batch_no + product_id.
                        # Phase-3: use allocated_qty consistently because dispatch
                        # decrements allocated_qty, while old reserved_qty values
                        # were never released.
                        # Batch number + product_id are what the allocation dict carries.
                        try:
                            from modules.sql_adapter import run_write as _rw_rsv
                            _pid_rsv = str(line.get("product_id") or "")
                            for _oa in _old_alloc_f4:
                                _obn  = str(_oa.get("batch_no") or "")
                                _oqty = int(_oa.get("allocated_qty") or 0)
                                if _obn and _oqty > 0 and _pid_rsv:
                                    _rw_rsv(
                                        """
                                        UPDATE inventory_stock
                                        SET allocated_qty = GREATEST(
                                                COALESCE(allocated_qty, 0) - %(qty)s, 0),
                                            updated_at    = NOW()
                                        WHERE batch_no   = %(bn)s
                                          AND product_id = %(pid)s::uuid
                                        """,
                                        {"qty": _oqty, "bn": _obn, "pid": _pid_rsv},
                                    )
                            for _na in temp_alloc:
                                _nbn  = str(_na.get("batch_no") or "")
                                _nqty = int(_na.get("allocated_qty") or 0)
                                if _nbn and _nqty > 0 and _pid_rsv:
                                    _rw_rsv(
                                        """
                                        UPDATE inventory_stock
                                        SET allocated_qty = COALESCE(allocated_qty, 0) + %(qty)s,
                                            updated_at    = NOW()
                                        WHERE batch_no   = %(bn)s
                                          AND product_id = %(pid)s::uuid
                                        """,
                                        {"qty": _nqty, "bn": _nbn, "pid": _pid_rsv},
                                    )
                        except Exception as _rsv_err:
                            import logging as _rsvlog
                            _rsvlog.getLogger(__name__).warning(
                                "[alloc_window] allocated_qty update failed (non-fatal): %s",
                                _rsv_err,
                            )
                            st.warning(
                                "⚠️ Batch allocated_qty update failed — stock counts may drift. "
                                "Run stock reconciliation from System Health."
                            )

                        # F4 audit: log batch change so it appears in History tab
                        try:
                            from modules.backoffice.audit_logger import audit, AuditAction
                            _old_bns = ", ".join(
                                str(a.get("batch_no") or "") for a in _old_alloc_f4
                            ) or "none"
                            _new_bns = ", ".join(
                                str(a.get("batch_no") or "") for a in temp_alloc
                            ) or "none"
                            audit(
                                AuditAction.PRICE_OVERRIDE,
                                entity="order_lines",
                                entity_id=_line_id_aw,
                                order_id=str(order.get("id") or ""),
                                payload={
                                    "action":    "batch_change",
                                    "old_value": _old_bns,
                                    "new_value": _new_bns,
                                    "order_no":  str(order.get("order_no") or ""),
                                },
                            )
                        except Exception as _f4a_err:
                            import logging as _f4alog
                            _f4alog.getLogger(__name__).debug(
                                "Batch change audit write failed (non-fatal): %s", _f4a_err
                            )

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
