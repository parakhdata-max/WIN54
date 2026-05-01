"""
modules/core/shared_punching.py
================================
Functions shared between retail_punching.py and wholesale_punching.py.

RULES:
  - Only put functions here when they are IDENTICAL or differ only in
    trivial whitespace/comment between both files.
  - When a function diverges (>10% diff), keep separate copies in each file.
  - This file has NO business logic — it only contains UI rendering helpers
    that both screens use identically.

CURRENTLY SHARED:
  - render_boxing_params()  — frame boxing measurements UI (6% diff, ws version used)

INTENTIONALLY NOT SHARED (diverged too much):
  - _hydrate_product_gst    109% diff — retail/ws fetch different price fields
  - render_product_selection  70% diff — different stock/pricing logic
  - render_batch_allocation_editor 68% diff
  - render_lens_params        60% diff — ws has corridor/fitting height extras
  - _stamp_line_tax           55% diff — different GST inclusion logic
  - initialize_session_state  70% diff — different session keys
  - render_power_entry       108% diff — retail has clinical sections, ws is simpler
  - reset_from_stage         103% diff — different stage maps
"""

import streamlit as st


# ══════════════════════════════════════════════════════════════════════════════
# BOXING PARAMS (Frame measurements)
# Shared between retail and wholesale — identical logic, same session keys.
# Uses wholesale version which has the rerun-guard fix.
# ══════════════════════════════════════════════════════════════════════════════

def render_boxing_params():
    """Boxing / Frame measurements inside a collapsible expander.

    Layout mirrors image-2 table structure:
    ┌─────────┬─────────┬──────┬─────────┬─────┐
    │  A Box  │  B Box  │  DBL │  ED     │ BVD │
    └─────────┴─────────┴──────┴─────────┴─────┘
    All fields are free-entry number_input (manual allowed).
    """
    if not st.session_state.retail_selected_product:
        return

    product_row = st.session_state.retail_selected_product.get('product_row', {})
    main_group  = str(product_row.get('main_group', '') or '').upper()

    # Only show for frame/spectacle products
    if not any(kw in main_group for kw in ['FRAME', 'SPECTACLE', 'OPHTHALMIC', 'EYEWEAR']):
        return

    with st.expander("📐 Frame / Boxing Measurements (optional)", expanded=False):
        st.caption("Enter frame boxing dimensions for clinical record / job card")

        col1, col2, col3, col4, col5 = st.columns(5)

        existing = st.session_state.get('retail_boxing_params', {})

        with col1:
            a_box = st.number_input(
                "A Box (mm)",
                min_value=0.0, max_value=80.0, step=0.5,
                value=float(existing.get('a_box') or 0.0),
                key="boxing_a_box",
                help="Horizontal lens width"
            )
        with col2:
            b_box = st.number_input(
                "B Box (mm)",
                min_value=0.0, max_value=60.0, step=0.5,
                value=float(existing.get('b_box') or 0.0),
                key="boxing_b_box",
                help="Vertical lens height"
            )
        with col3:
            dbl = st.number_input(
                "DBL (mm)",
                min_value=0.0, max_value=40.0, step=0.5,
                value=float(existing.get('dbl') or 0.0),
                key="boxing_dbl",
                help="Distance between lenses"
            )
        with col4:
            ed = st.number_input(
                "ED (mm)",
                min_value=0.0, max_value=80.0, step=0.5,
                value=float(existing.get('ed') or 0.0),
                key="boxing_ed",
                help="Effective diameter"
            )
        with col5:
            bvd = st.number_input(
                "BVD (mm)",
                min_value=0.0, max_value=20.0, step=0.5,
                value=float(existing.get('bvd') or 0.0),
                key="boxing_bvd",
                help="Back vertex distance"
            )

        if any([a_box, b_box, dbl, ed, bvd]):
            # Show computed values
            if a_box and b_box:
                ced = round((a_box ** 2 + b_box ** 2) ** 0.5, 1)
                st.caption(
                    f"Computed ED: **{ced} mm**"
                    + (f"  ·  Entered ED: {ed} mm" if ed else "")
                )

            if a_box and dbl:
                pd_guide = round((a_box + dbl) / 2, 1)
                st.caption(f"PD guide (monocular): ~{pd_guide} mm")

            # Persist — guarded to prevent rerun loop
            _bp_new = {
                'a_box': round(a_box, 1),
                'b_box': round(b_box, 1),
                'dbl':   round(dbl, 1),
                'ed':    round(ed, 1),
                'bvd':   round(bvd, 1),
            }
            if st.session_state.get('retail_boxing_params') != _bp_new:
                st.session_state.retail_boxing_params = _bp_new
