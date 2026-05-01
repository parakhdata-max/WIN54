from typing import Optional
"""
price_dropdown_ui.py
─────────────────────
Streamlit UI component — Price Version Dropdown.
Used in wholesale, retail and bulk billing screens.

Call render_price_selector() after product + power is confirmed.
"""

import streamlit as st
from modules.price_governor import get_billing_price, validate_price


def render_price_selector(product_id, party_type: str = 'WHOLESALE',
                           batch_purchase_rate: float = None,
                           sph=None, cyl=None, axis=None,
                           key_prefix: str = 'price') -> dict:
    """
    Renders price selector for a product in billing screens.

    Returns dict:
      {'mrp': float, 'selling_price': float, 'purchase_rate': float,
       'valid': bool, 'error': str|None, 'warning': str|None}
    """
    pricing = get_billing_price(
        product_id, party_type=party_type,
        batch_purchase_rate=batch_purchase_rate,
        sph=sph, cyl=cyl, axis=axis
    )

    mrp            = pricing['mrp']
    selling_price  = pricing['selling_price']
    purchase_rate  = pricing['purchase_rate']
    show_dropdown  = pricing['show_dropdown']
    price_options  = pricing['price_options']
    mismatch       = pricing['mismatch']

    selected_mrp   = mrp
    selected_sell  = selling_price
    selected_pr    = purchase_rate

    # ── Price mismatch warning ─────────────────────────────────────────────
    if mismatch:
        st.warning(mismatch['message'])

    # ── Price dropdown — only shown when old-price stock exists ───────────
    if show_dropdown and price_options:
        st.markdown("**💰 Select Price Version:**")
        labels = [o['label'] for o in price_options]

        # Default to current price (index 0)
        sel_idx = st.selectbox(
            "Price",
            options=range(len(labels)),
            format_func=lambda i: labels[i],
            index=0,
            key=f"{key_prefix}_price_version",
            label_visibility="collapsed",
        )
        selected = price_options[sel_idx]
        selected_mrp  = selected['mrp']
        selected_sell = selected['selling_price']
        selected_pr   = selected['purchase_rate']

        if not selected['is_current']:
            st.info(
                f"📦 Old price selected — batch purchased at "
                f"₹{selected_pr:,.2f}. "
                f"Selling at ₹{selected_sell:,.2f} / MRP ₹{selected_mrp:,.2f}."
            )
    else:
        # No dropdown — show current price as info
        if mrp > 0:
            st.caption(f"💰 MRP ₹{mrp:,.2f} · Trade ₹{selling_price:,.2f}")

    # ── Manual price override (when no price row exists) ──────────────────
    if mrp == 0 and selling_price == 0:
        st.markdown("**💰 Enter Price (no price on record):**")
        col1, col2 = st.columns(2)
        with col1:
            selected_mrp = st.number_input(
                "MRP ₹", min_value=0.0, step=1.0,
                key=f"{key_prefix}_mrp_manual", format="%.2f"
            )
        with col2:
            selected_sell = st.number_input(
                "Selling Price ₹", min_value=0.0, step=1.0,
                key=f"{key_prefix}_sell_manual", format="%.2f"
            )
        selected_pr = 0.0

    # ── Validate ──────────────────────────────────────────────────────────
    validation = validate_price(selected_sell, selected_mrp, selected_pr, party_type)

    if validation['error']:
        st.error(validation['error'])
    elif validation['warning']:
        st.warning(validation['warning'])

    return {
        'mrp':           selected_mrp,
        'selling_price': selected_sell,
        'purchase_rate': selected_pr,
        'valid':         validation['valid'],
        'error':         validation['error'],
        'warning':       validation['warning'],
    }


def render_price_mismatch_alert(product_id, batch_purchase_rate: float,
                                 order_mrp: float) -> Optional[dict]:
    """
    Called at INVOICE time to check if order price ≠ batch price.
    Returns selected price or None if no mismatch.
    """
    from modules.price_governor import detect_price_mismatch
    mismatch = detect_price_mismatch(product_id, order_mrp, batch_purchase_rate)

    if not mismatch:
        return None

    st.warning(mismatch['message'])

    col1, col2 = st.columns(2)
    choice = None

    with col1:
        if st.button(
            f"Keep new price — MRP ₹{mismatch['current_mrp']:,.0f}",
            key=f"pm_new_{product_id}"
        ):
            choice = {
                'mrp':           mismatch['current_mrp'],
                'selling_price': mismatch['current_sell'],
                'purchase_rate': mismatch['current_purchase'],
            }

    with col2:
        if mismatch.get('old_mrp') and st.button(
            f"Use old price — MRP ₹{mismatch['old_mrp']:,.0f}",
            key=f"pm_old_{product_id}"
        ):
            choice = {
                'mrp':           mismatch['old_mrp'],
                'selling_price': mismatch['old_sell'],
                'purchase_rate': mismatch['batch_purchase'],
            }

    return choice
