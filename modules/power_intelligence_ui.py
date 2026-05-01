"""
power_intelligence_ui.py
─────────────────────────
Streamlit UI component — Power Intelligence Panel.
Call render_power_intelligence_panel() after power is entered in wholesale punching.
"""

import streamlit as st
from modules.power_intelligence import power_intelligence_summary, check_power_in_product_range, find_nearest_powers, is_colour_product



def render_range_check(product_id, product_name: str,
                        sph: float, cyl: float = 0.0, axis: int = 0,
                        is_colour: bool = False, eye: str = "") -> bool:
    """
    Show range warning + Power Intelligence expander if power is out of range.
    Returns True if IN range, False if out of range.
    """
    if not product_id or not product_name:
        return True
    if sph == 0.0 and cyl == 0.0:
        return True  # blank prescription

    try:
        result = check_power_in_product_range(product_id, product_name, sph, cyl, axis)
        if not result.get('in_range', True):
            st.error(f"❌ **{result['reason']}**")

            # Show Power Intelligence as expander
            from modules.power_intelligence import power_intelligence_summary
            summary = power_intelligence_summary(sph, cyl, axis)
            orderable = summary.get('orderable', [])

            # Colour filter
            if not is_colour:
                orderable = [r for r in orderable
                             if not is_colour_product(r['product_name'])]
            else:
                orderable = [r for r in orderable
                             if is_colour_product(r['product_name'])]

            if orderable:
                eye_label = f"👁️ {eye} — " if eye else ""
                is_toric = cyl not in (None, 0.0, 0)
                power_str = f"SPH {sph:+.2f}"
                if is_toric:
                    power_str += f" / CYL {cyl:+.2f} / AXIS {axis}°"
                exp_label = f"🔍 Power Intelligence — {eye_label}{power_str}"

                with st.expander(exp_label, expanded=False):
                    st.caption(
                        f"Power outside range for **{product_name}**. "
                        f"Available from other brands:"
                    )
                    from collections import defaultdict
                    by_brand = defaultdict(list)
                    for p in orderable:
                        by_brand[p['brand']].append(p)
                    brand_order = ['Cooper Vision','Bausch & Lomb',
                                   'Johnson & Johnson','Silklens','CEPL']
                    all_brands = brand_order + [b for b in by_brand
                                                if b not in brand_order]
                    for brand in all_brands:
                        prods = by_brand.get(brand, [])
                        if not prods: continue
                        st.markdown(f"**{brand}**")
                        for p in prods:
                            mrp = p.get('mrp')
                            mrp_str = f" — ₹{int(mrp)}" if mrp else ""
                            st.caption(
                                f"&nbsp;&nbsp;&nbsp;• {p['product_name']}{mrp_str}"
                            )
            return False
    except Exception:
        pass
    return True


def render_power_intelligence_panel(sph: float, cyl: float = 0.0,
                                     axis: int = 0, add_power: float = 0.0,
                                     selected_product: str = None,
                                     eye: str = "",
                                     product_id: str = None,
                                     is_colour: bool = False):
    """
    Renders the Power Intelligence Panel inside a collapsible expander.
    eye: "RIGHT", "LEFT", or "" for generic label.
    """

    is_toric = cyl not in (None, 0.0, 0)
    power_str = f"SPH {sph:+.2f}"
    if is_toric:
        power_str += f" / CYL {cyl:+.2f} / AXIS {axis}°"
    if add_power:
        power_str += f" / ADD {add_power:+.2f}"

    # Don't fire on blank/default prescription (all zeros = not entered yet)
    if sph == 0.0 and cyl == 0.0 and axis == 0 and add_power == 0.0:
        st.caption("ℹ️ Enter prescription to check availability across brands.")
        return

    # Build expander label
    is_toric = cyl not in (None, 0.0, 0)
    power_str = f"SPH {sph:+.2f}"
    if is_toric: power_str += f" / CYL {cyl:+.2f} / AXIS {axis}°"
    if add_power: power_str += f" / ADD {add_power:+.2f}"
    eye_label = f"👁️ {eye} — " if eye else ""
    _exp_label = f"🔍 Power Intelligence — {eye_label}{power_str}"

    with st.expander(_exp_label, expanded=False):
        with st.spinner("Checking availability..."):
            result = power_intelligence_summary(sph, cyl, axis, add_power)

        instock   = result['instock']
        orderable = result['orderable']
        total_qty = result['total_instock_qty']


        col1, col2 = st.columns(2)

        # ── IN STOCK ─────────────────────────────────────────────────────────────
        with col1:
            if instock:
                st.markdown(f"**✅ In Stock ({len(instock)} batch rows · {total_qty} pcs)**")
                # Group by product
                from collections import defaultdict
                by_prod = defaultdict(list)
                for r in instock:
                    by_prod[r['product_name']].append(r)

                for pname, batches in sorted(by_prod.items()):
                    prod_qty = sum(int(b.get('quantity', 0)) for b in batches)
                    is_selected = (selected_product and selected_product.lower() in pname.lower())
                    prefix = "👉 " if is_selected else "• "
                    brand = batches[0].get('brand', '')
                    st.markdown(f"{prefix}**{pname}** — {prod_qty} pcs")
                    for b in batches:
                        expiry = b.get('expiry_date', '')
                        if hasattr(expiry, 'strftime'):
                            expiry = expiry.strftime('%b %Y')
                        st.caption(f"&nbsp;&nbsp;&nbsp;Batch: {b.get('batch_no','—')} · Qty: {b.get('quantity')} · Exp: {expiry}")
            else:
                st.markdown("**❌ Not in Stock**")
                st.caption("No physical stock found for this power.")
                # ── Nearest available powers ────────────────────────
                if product_id:
                    try:
                        nearest = find_nearest_powers(
                            product_id, selected_product or "",
                            sph, cyl, axis, n=2
                        )
                        if nearest:
                            st.markdown("**📍 Nearest available:**")
                            for _np in nearest:
                                _arr = "⬆️" if _np["direction"]=="above" else "⬇️"
                                _ps = f"SPH {_np['sph']:+.2f}"
                                if _np['cyl'] and abs(_np['cyl'])>0.01:
                                    _ps += f" / CYL {_np['cyl']:+.2f} / AXIS {_np['axis']}°"
                                _exp = f" · Exp: {_np['expiry']}" if _np['expiry'] else ""
                                st.success(
                                    f"{_arr} {_ps} — **{_np['qty']} pcs**"
                                    f" · Batch: {_np['batch_no'] or '—'}{_exp}"
                                )
                    except Exception:
                        pass

        # ── ORDERABLE ─────────────────────────────────────────────────────────────
        with col2:
            # Filter colour/clear match
            if not is_colour:
                orderable = [r for r in orderable if not is_colour_product(r['product_name'])]
            else:
                orderable = [r for r in orderable if is_colour_product(r['product_name'])]
            if orderable:
                st.markdown(f"**📦 Available to Order ({len(orderable)} products)**")
                # Group by brand
                from collections import defaultdict
                by_brand = defaultdict(list)
                for r in orderable:
                    by_brand[r['brand']].append(r)

                brand_order = ['Cooper Vision', 'Bausch & Lomb', 'Johnson & Johnson']
                all_brands = brand_order + [b for b in by_brand if b not in brand_order]

                for brand in all_brands:
                    prods = by_brand.get(brand, [])
                    if not prods: continue
                    st.markdown(f"**{brand}**")
                    for p in prods:
                        wear = p.get('wear_schedule', '')
                        ptype = p.get('type', '')
                        box = p.get('box_size', 1)
                        mrp_str = f"₹{int(p['mrp'])}" if p.get('mrp') else ''
                        detail = ' · '.join(filter(None, [wear, f"{box}pk", mrp_str]))
                        st.caption(f"&nbsp;&nbsp;&nbsp;• {p['product_name']} — {detail}")
            else:
                st.markdown("**📦 No other brands available**")
                st.caption("This power is outside all known brand ranges.")

        # ── VERDICT ──────────────────────────────────────────────────────────────
        if not instock and not orderable:
            st.error("⚠️ This power is not available in any brand. Please verify the prescription.")
        elif not instock and orderable:
            st.info(f"📋 Not in our stock — {len(orderable)} brands available to order.")
        elif instock and not selected_product:
            st.success(f"✅ Available in stock — {total_qty} pcs across {len(set(r['product_name'] for r in instock))} products.")
