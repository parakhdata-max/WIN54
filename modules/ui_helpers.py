"""
UI Helper Utilities
====================
Contains UI-only formatting helpers.
No pricing logic here — all price/qty logic lives in:
    modules.core.price_qty_governor

is_box_product and format_quantity_display are re-exported from the governor
so existing callers don't need to change their imports.
"""

from modules.core.price_qty_governor import (
    is_box_product,          # re-export — same API as before
    reverse_qty,
    box_qty_label,
    pair_qty_label,
    PAIR_TO_PCS,
)


def format_quantity_display(qty_pcs: int, product: dict) -> str:
    """
    Convert PCS quantity into human-readable format.
    Delegates to governor's reverse_qty for consistent logic.

    Examples:
        12 PCS (box_size=6) → "2 BOX"
        8 PCS  (box_size=6) → "1 BOX + 2 PCS"
        5 PCS  (no box)     → "5 PCS"
        4 PCS  (PAIR unit)  → "2 Pair"
    """
    result = reverse_qty(int(qty_pcs or 0), product)
    return result["display"].upper() if result["display"] else "0"
