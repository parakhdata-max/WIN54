"""
modules/pricing/price_resolver.py
==================================
Centralized Price Field Resolver — DV ERP

Single source of truth for: "which DB column holds the price for this order type?"

  WHOLESALE  →  selling_price    (trade / distributor price)
  RETAIL     →  mrp              (maximum retail / counter price, usually GST-inclusive)
  PURCHASE   →  purchase_rate    (cost / inward price)
  ONLINE     →  online_price     (e-commerce price, falls back to mrp if not set)
  default    →  selling_price → mrp

Usage:
    from modules.pricing.price_resolver import resolve_price_for_order_type

    # In wholesale_punching.py (adding other items):
    unit_price = resolve_price_for_order_type(product, "WHOLESALE")

    # In retail_punching.py (building batch list):
    'selling_price': resolve_price_for_order_type(batch_row, "RETAIL")

    # In backoffice_logic.py (auto-allocation):
    raw_price = resolve_price_for_order_type(stock_row, order_type)

Extending for new channels (e.g., B2B portal):
    Just add a new elif branch — no punching code changes needed.
"""

import math


# ─────────────────────────────────────────────────────────────────────────────
# FIELD PRIORITY MAP
# Key   = order_type string (uppercase)
# Value = ordered list of DB column names to try (first non-zero wins)
# ─────────────────────────────────────────────────────────────────────────────
_PRICE_FIELDS: dict[str, list[str]] = {
    "WHOLESALE": ["selling_price", "mrp", "unit_price", "price"],
    "RETAIL":    ["mrp", "selling_price", "unit_price", "price"],
    "PURCHASE":  ["purchase_rate", "cost_price", "mrp"],
    "ONLINE":    ["online_price", "mrp", "selling_price", "unit_price"],
}
_DEFAULT_FIELDS = ["selling_price", "mrp", "unit_price", "price"]


def _safe_float(val) -> float:
    """Convert any DB value to float safely. Returns 0.0 for None/NaN/invalid."""
    try:
        f = float(val)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return 0.0


def resolve_price_for_order_type(row: dict, order_type: str) -> float:
    """
    Returns the correct base price (per-unit, pre-normalization) from a DB row.

    Args:
        row:        dict-like from DB — product, batch, or inventory_stock row.
                    Must have at least one of: selling_price, mrp, purchase_rate,
                    online_price, unit_price, price, cost_price.
        order_type: "WHOLESALE" | "RETAIL" | "PURCHASE" | "ONLINE" | any string.
                    Case-insensitive.

    Returns:
        float: First non-zero value from the priority list, or 0.0 if none found.

    NOTE: This returns the RAW price from DB (may be BOX price for box products).
    Always pass through normalize_to_pcs_price() afterwards.
    """
    ot     = str(order_type or "RETAIL").upper().strip()
    fields = _PRICE_FIELDS.get(ot, _DEFAULT_FIELDS)

    for field in fields:
        val = _safe_float(row.get(field))
        if val > 0:
            return val

    return 0.0


def get_price_field_name(order_type: str) -> str:
    """
    Returns the PRIMARY price field name for an order type.
    Useful for logging/display: 'Using mrp for RETAIL order'.

    Args:
        order_type: "WHOLESALE" | "RETAIL" | "PURCHASE" | "ONLINE"

    Returns:
        str: primary field name, e.g. "mrp"
    """
    ot     = str(order_type or "RETAIL").upper().strip()
    fields = _PRICE_FIELDS.get(ot, _DEFAULT_FIELDS)
    return fields[0] if fields else "selling_price"
