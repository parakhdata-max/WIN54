"""
price_manager.py
─────────────────
Price versioning system for DV ERP.

Key functions:
  get_current_price(product_id)
    → Returns current PRICE row for a product

  get_billing_prices(product_id)
    → Returns all price versions valid for billing
      (current always + old only if batch stock exists at that purchase_rate)

  upsert_product_price(product_id, mrp, selling_price, purchase_rate, ...)
    → Create or update price row
    → Auto-versions when purchase_rate changes

  price_change_detected(product_id, inbound_purchase_rate)
    → True if incoming batch has different purchase_rate than current
    → Used by CLENS/OPHLENS loader to trigger price version
"""

from typing import Optional, List, Dict
from modules.sql_adapter import run_query, execute_query


def get_current_price(product_id: str) -> Optional[Dict]:
    """Get current active PRICE row for a product."""
    try:
        rows = run_query("""
            SELECT id, mrp, selling_price, purchase_rate,
                   effective_from, price_source, is_price_current
            FROM inventory_stock
            WHERE product_id = %s
              AND stock_type = 'PRICE'
              AND is_price_current = TRUE
              AND is_active = TRUE
            LIMIT 1
        """, (str(product_id),))
        return rows[0] if rows else None
    except Exception:
        return None


def get_billing_prices(product_id: str) -> List[Dict]:
    """
    Get all price versions valid for billing.
    - Always includes current price
    - Includes old prices ONLY if batch stock still exists at that purchase_rate
    - Sorted: current first, then old descending by date
    Tries DB function first, falls back to Python implementation.
    """
    try:
        rows = run_query("""
            SELECT * FROM get_billing_prices(%s::uuid)
        """, (str(product_id),))
        if rows is not None:
            return rows
    except Exception:
        pass

    # Fallback: Python implementation (used before DB function is created)
    try:
        curr = get_current_price(product_id)
        if not curr:
            return []
        # Get old prices with batch stock
        old_rows = run_query("""
            SELECT pr.id, pr.mrp, pr.selling_price, pr.purchase_rate,
                   pr.effective_from, pr.price_source, pr.is_price_current,
                   pr.is_price_current AS is_current,
                   COALESCE(
                       (SELECT SUM(b.quantity) FROM inventory_stock b
                        WHERE b.product_id = pr.product_id
                          AND b.stock_type IN ('BATCH','PURCHASE') AND b.quantity > 0
                          AND ABS(COALESCE(b.purchase_rate,0)-COALESCE(pr.purchase_rate,0))
                              <= GREATEST(0.01, COALESCE(pr.purchase_rate,0)*0.02)),
                       0
                   )::bigint AS batch_qty
            FROM inventory_stock pr
            WHERE pr.product_id = %s AND pr.stock_type = 'PRICE'
              AND COALESCE(pr.is_active, TRUE) = TRUE
              AND pr.is_price_current = FALSE
        """, (str(product_id),)) or []

        result = [curr]
        for r in old_rows:
            if int(r.get('batch_qty') or 0) > 0:
                result.append(r)
        return result
    except Exception:
        curr = get_current_price(product_id)
        return [curr] if curr else []


def price_change_detected(product_id: str, inbound_purchase_rate: float) -> bool:
    """
    Check if an incoming batch has a different purchase_rate than current.
    Used by loaders to know when to create a new price version.
    """
    if not inbound_purchase_rate or inbound_purchase_rate <= 0:
        return False
    curr = get_current_price(product_id)
    if not curr:
        return True  # No price yet — need to create one
    current_rate = float(curr.get('purchase_rate') or 0)
    return abs(current_rate - inbound_purchase_rate) > 0.01


def upsert_product_price(
    product_id: str,
    mrp: float,
    selling_price: float = 0,
    purchase_rate: float = 0,
    effective_from: str = None,   # 'YYYY-MM-DD' or None = today
    price_source: str = 'MANUAL'
) -> Optional[str]:
    """
    Create or update price row for a product.
    - If purchase_rate unchanged → updates MRP/selling_price in place
    - If purchase_rate changed → archives old, creates new version
    Returns new row id (or None if updated in place).
    """
    try:
        rows = run_query("""
            SELECT upsert_product_price(
                %s::uuid, %s, %s, %s,
                %s::date,
                %s
            ) AS new_id
        """, (
            str(product_id),
            round(float(mrp or 0), 2),
            round(float(selling_price or 0), 2),
            round(float(purchase_rate or 0), 2),
            effective_from or 'today',
            price_source,
        ))
        if rows:
            return rows[0].get('new_id')
        return None
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[price_manager] upsert failed: {e}")
        return None


def format_price_label(price_row: Dict) -> str:
    """Format a price row for display in billing dropdown."""
    mrp = float(price_row.get('mrp') or 0)
    sp  = float(price_row.get('selling_price') or 0)
    is_current = price_row.get('is_current') or price_row.get('is_price_current')
    eff = price_row.get('effective_from', '')
    qty = int(price_row.get('batch_qty') or 0)

    if is_current:
        return f"₹{mrp:.0f} MRP · ₹{sp:.0f} Trade  (Current price)"
    else:
        eff_str = str(eff)[:7] if eff else '—'
        qty_str = f" · {qty} pcs left" if qty > 0 else ""
        return f"₹{mrp:.0f} MRP · ₹{sp:.0f} Trade  (Old — {eff_str}{qty_str})"
