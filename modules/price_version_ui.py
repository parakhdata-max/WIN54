"""
price_version_ui.py
────────────────────
Billing price selector — shown in wholesale, retail, bulk order
when multiple price versions are available for a product.

Usage:

    selected_price = render_price_selector(product_id, product_name, party_type)
    # Returns dict with mrp, selling_price, purchase_rate
    # or None if only one price (no dropdown needed)
"""

import streamlit as st
from modules.price_manager import get_billing_prices, format_price_label


def render_price_selector(
    product_id: str,
    product_name: str,
    party_type: str = "WHOLESALE",
    key_prefix: str = ""
) -> dict:
    """
    Renders price version selector if multiple versions exist.
    Always returns a price dict — current price if only one version.

    Anti-under-billing: old price only shown if batch stock exists at that rate.
    Current price is always default and cannot be deselected.

    Returns:
        {
            'mrp': float,
            'selling_price': float,
            'purchase_rate': float,
            'is_current': bool,
            'price_row_id': str,
        }
    """
    prices = get_billing_prices(str(product_id))

    if not prices:
        return {}

    # Current price is always index 0 (sorted by get_billing_prices)
    current = prices[0]

    if len(prices) == 1:
        # Only one price — no dropdown, return current silently
        return _to_price_dict(current)

    # Multiple prices — show dropdown
    old_prices = [p for p in prices if not (p.get('is_current') or p.get('is_price_current'))]

    st.markdown("---")
    col1, col2 = st.columns([3, 1])

    with col1:
        st.markdown(f"**💰 Price Version — {product_name}**")

    with col2:
        st.caption("🔒 Old = has physical stock")

    # Build options
    labels = [format_price_label(p) for p in prices]
    key = f"price_ver_{key_prefix}_{product_id}"

    # Default always current (index 0) — cannot be changed to something higher
    selected_idx = st.selectbox(
        "Select price:",
        options=range(len(labels)),
        format_func=lambda i: labels[i],
        index=0,
        key=key,
        help=(
            "Current price is the default. "
            "Old price is only available if you have physical stock "
            "received at that purchase rate."
        )
    )

    selected = prices[selected_idx]
    selected_price = _to_price_dict(selected)

    # Anti-under-billing guard — validate selection
    current_mrp = float(current.get('mrp') or 0)
    selected_mrp = float(selected.get('mrp') or 0)

    if selected_mrp < current_mrp:
        batch_qty = int(selected.get('batch_qty') or 0)
        if batch_qty <= 0:
            st.error(
                f"❌ Old price ₹{selected_mrp:.0f} not allowed — "
                f"no stock received at that purchase rate. "
                f"Using current price ₹{current_mrp:.0f}."
            )
            return _to_price_dict(current)
        else:
            st.warning(
                f"⚠️ Old price selected — ₹{selected_mrp:.0f} MRP "
                f"({batch_qty} pcs available at old purchase rate). "
                f"Ensure correct batch is being billed."
            )

    st.markdown("---")
    return selected_price


def get_selected_price(product_id: str, key_prefix: str = "") -> dict:
    """
    Read currently selected price from session state without re-rendering.
    Use after render_price_selector has been called.
    """
    key = f"price_ver_{key_prefix}_{product_id}"
    prices = get_billing_prices(str(product_id))
    if not prices:
        return {}
    idx = st.session_state.get(key, 0)
    if isinstance(idx, int) and idx < len(prices):
        return _to_price_dict(prices[idx])
    return _to_price_dict(prices[0])


def _to_price_dict(row: dict) -> dict:
    return {
        'mrp':           float(row.get('mrp') or 0),
        'selling_price': float(row.get('selling_price') or 0),
        'purchase_rate': float(row.get('purchase_rate') or 0),
        'is_current':    bool(row.get('is_current') or row.get('is_price_current')),
        'price_row_id':  str(row.get('price_row_id') or row.get('id') or ''),
        'effective_from': str(row.get('effective_from') or ''),
    }