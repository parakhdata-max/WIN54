"""
modules/loaders/ophthalmic_adapter.py
=======================================
Ophthalmic Lens Stock + RX Hybrid Adapter — DV ERP WIN16

Handles two modes of ophthalmic lenses in inventory_stock:
  STOCK → physical lens on shelf (quantity > 0, deducted on sale)
  RX    → price/specification only, ordered per job (quantity = 0, never deducted)

Single function for punching:  get_ophthalmic_for_punching()
Single function for validation: check_ophthalmic_availability()
Deduct on sale:                 deduct_ophthalmic_stock()
Convert ordered lens to stock:  promote_rx_to_stock()

Does NOT use batch_manager — ophthalmic lenses have no expiry, no FIFO,
and the RX mode is fundamentally different from batch allocation.
"""

import logging
from typing import Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ── Availability state constants ───────────────────────────────────────────────
IN_STOCK   = "IN STOCK"     # qty > 0, sell from shelf, deduct on confirm
RX_ORDER   = "ORDER / RX"   # qty = 0, place supplier order, no deduction
STOCK_ZERO = "STOCK EMPTY"  # STOCK row exists but fully depleted


# ═══════════════════════════════════════════════════════════════════════════════
# HYBRID LOOKUP — primary function for punching screen
# ═══════════════════════════════════════════════════════════════════════════════

def get_ophthalmic_for_punching(
    product_id: str,
    sph:        Optional[float] = None,
    cyl:        Optional[float] = None,
    axis:       Optional[int]   = None,
    add_power:  Optional[float] = None,
    eye_side:   str             = "B",
) -> pd.DataFrame:
    """
    Hybrid lens lookup for the punching / billing screen.

    Queries inventory_stock for both STOCK and RX rows matching the given
    product + power combination, ranked:
      rank 1 → STOCK with qty > 0   → sell from shelf
      rank 2 → RX row               → place order, show price
      rank 3 → STOCK with qty = 0   → depleted, treat as RX

    Falls back to a generic RX price row (no power filter) if no exact match.

    Returns DataFrame with columns:
      id, item_type, quantity, selling_price, purchase_rate, mrp,
      eye_side, lens_design, sph, cyl, axis, add_power,
      availability, sort_rank
    """
    from modules.sql_adapter import execute_query

    sph_v  = _san(sph)
    cyl_v  = _san(cyl)
    add_v  = _san(add_power)
    axis_v = _san(axis) if cyl_v not in (None, 0.0) else None

    sql = """
        SELECT
            s.id,
            s.item_type,
            s.quantity,
            s.selling_price,
            s.purchase_rate,
            s.mrp,
            s.eye_side,
            s.lens_design,
            s.sph,
            s.cyl,
            s.axis,
            s.add_power,
            CASE
                WHEN s.item_type = 'STOCK' AND s.quantity > 0 THEN 'IN STOCK'
                WHEN s.item_type = 'RX'                        THEN 'ORDER / RX'
                WHEN s.item_type = 'STOCK' AND s.quantity = 0 THEN 'STOCK EMPTY'
                ELSE 'UNKNOWN'
            END AS availability,
            CASE
                WHEN s.item_type = 'STOCK' AND s.quantity > 0 THEN 1
                WHEN s.item_type = 'RX'                        THEN 2
                WHEN s.item_type = 'STOCK' AND s.quantity = 0 THEN 3
                ELSE 4
            END AS sort_rank
        FROM inventory_stock s
        WHERE s.product_id  = %s
          AND s.is_active   = true
          AND s.stock_type  = 'POWER'
          AND s.sph         IS NOT DISTINCT FROM %s
          AND s.cyl         IS NOT DISTINCT FROM %s
          AND s.add_power   IS NOT DISTINCT FROM %s
          AND (s.eye_side = %s OR s.eye_side = 'B')
    """
    params = [str(product_id), sph_v, cyl_v, add_v, eye_side.strip().upper()]

    if axis_v is not None:
        sql += " AND s.axis IS NOT DISTINCT FROM %s"
        params.append(axis_v)

    sql += " ORDER BY sort_rank, s.selling_price LIMIT 10"

    df = execute_query(sql, "ophthalmic_punch_lookup", params=tuple(params))

    # Fallback: no RX price row for this exact power → get product-level RX price
    has_rx = not df.empty and RX_ORDER in df.get("availability", pd.Series()).values
    if not has_rx:
        rx_fb = _rx_price_fallback(product_id)
        if not rx_fb.empty:
            df = pd.concat([df, rx_fb], ignore_index=True) if not df.empty else rx_fb

    if df.empty:
        return pd.DataFrame()

    return df.sort_values("sort_rank").reset_index(drop=True)


def _rx_price_fallback(product_id: str) -> pd.DataFrame:
    """
    Returns the cheapest RX price row for a product with no power filter.
    Used when the exact power has no RX row but the product is RX-type.
    Gives punching screen a price to show while the user enters power.
    """
    from modules.sql_adapter import execute_query
    return execute_query("""
        SELECT
            s.id, s.item_type, s.quantity,
            s.selling_price, s.purchase_rate, s.mrp,
            s.eye_side, s.lens_design,
            s.sph, s.cyl, s.axis, s.add_power,
            'ORDER / RX' AS availability,
            2            AS sort_rank
        FROM inventory_stock s
        WHERE s.product_id = %s
          AND s.item_type  = 'RX'
          AND s.is_active  = true
          AND s.stock_type = 'POWER'
        ORDER BY s.selling_price
        LIMIT 1
    """, "rx_price_fallback", params=(str(product_id),))


# ═══════════════════════════════════════════════════════════════════════════════
# AVAILABILITY CHECK — for punching pre-validation
# ═══════════════════════════════════════════════════════════════════════════════

def check_ophthalmic_availability(
    product_id:   str,
    sph           = None,
    cyl           = None,
    axis          = None,
    add_power     = None,
    eye_side:     str = "B",
    required_qty: int = 1,
) -> Dict:
    """
    Returns a clean availability dict for the punching screen to consume.

    Return dict:
      status        : 'STOCK' | 'RX' | 'UNAVAILABLE'
      available_qty : int   (0 for RX)
      selling_price : float
      purchase_rate : float
      mrp           : float
      stock_id      : str | None   (inventory_stock.id, None for RX)
      message       : str          (display-ready)
    """
    df = get_ophthalmic_for_punching(product_id, sph, cyl, axis, add_power, eye_side)

    if df.empty:
        return _unavail("No stock or RX price found — check product setup")

    top  = df.iloc[0]
    avail = top.get("availability", "")

    if avail == IN_STOCK:
        qty = int(top.get("quantity", 0))
        return {
            "status":        "STOCK",
            "available_qty": qty,
            "selling_price": _f(top, "selling_price"),
            "purchase_rate": _f(top, "purchase_rate"),
            "mrp":           _f(top, "mrp"),
            "stock_id":      str(top.get("id")),
            "message":       f"✅ In stock — {qty} available",
        }

    elif avail == RX_ORDER:
        return {
            "status":        "RX",
            "available_qty": 0,
            "selling_price": _f(top, "selling_price"),
            "purchase_rate": _f(top, "purchase_rate"),
            "mrp":           _f(top, "mrp"),
            "stock_id":      None,
            "message":       f"📋 RX order — ₹{_f(top, 'mrp'):.0f} (ordered per job)",
        }

    else:
        return _unavail("Stock empty — switch to RX order or check supplier")


def _unavail(msg: str) -> Dict:
    return {
        "status": "UNAVAILABLE", "available_qty": 0,
        "selling_price": 0, "purchase_rate": 0, "mrp": 0,
        "stock_id": None, "message": f"⚠️ {msg}",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# DEDUCT — on confirmed STOCK sale
# ═══════════════════════════════════════════════════════════════════════════════

def deduct_ophthalmic_stock(stock_id: str, qty_sold: int) -> Tuple[bool, str]:
    """
    Deduct quantity from a STOCK row after a confirmed sale.
    Marks row is_active=false if qty reaches 0.
    Never touches RX rows — they carry no quantity.
    """
    from modules.sql_adapter import run_query
    try:
        run_query("""
            UPDATE inventory_stock
            SET
                quantity   = GREATEST(quantity - %s, 0),
                is_active  = CASE WHEN (quantity - %s) <= 0 THEN false ELSE true END,
                updated_at = NOW()
            WHERE id        = %s
              AND item_type  = 'STOCK'
              AND stock_type = 'POWER'
        """, (qty_sold, qty_sold, stock_id))
        return True, f"Deducted {qty_sold} unit(s)"
    except Exception as e:
        logger.error(f"deduct_ophthalmic_stock: {e}")
        return False, f"Deduct failed: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# PROMOTE — RX → STOCK when ordered lens stays back
# ═══════════════════════════════════════════════════════════════════════════════

def promote_rx_to_stock(
    rx_stock_id: str,
    quantity:    int,
    location:    str = "",
) -> Tuple[bool, str]:
    """
    When a received RX lens is kept as shelf stock instead of being dispatched.

    Strategy:
      - INSERT a new STOCK row copying all fields from the RX row
      - The original RX row stays ALIVE as price template for future orders
      - No double-entry, no power re-typing

    Returns: (success, message)
    """
    from modules.sql_adapter import run_query
    try:
        run_query("""
            INSERT INTO inventory_stock (
                product_id, sph, cyl, axis, add_power, eye_side,
                item_type, stock_type, quantity,
                purchase_rate, selling_price, mrp, lens_design,
                location, is_active, created_at, updated_at
            )
            SELECT
                product_id, sph, cyl, axis, add_power, eye_side,
                'STOCK',    stock_type,  %s,
                purchase_rate, selling_price, mrp, lens_design,
                COALESCE(NULLIF(%s, ''), location),
                true, NOW(), NOW()
            FROM inventory_stock
            WHERE id        = %s
              AND item_type  = 'RX'
        """, (quantity, location, rx_stock_id))
        return True, f"✅ {quantity} unit(s) moved to STOCK shelf (RX price row kept)"
    except Exception as e:
        logger.error(f"promote_rx_to_stock: {e}")
        return False, f"Promote failed: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _san(v):
    """Sanitize power values — mirrors sql_adapter._sanitize_value."""
    if v is None:
        return None
    try:
        import math
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 2)
    except (TypeError, ValueError):
        return None


def _f(row, col) -> float:
    try:
        import math
        v = row.get(col)
        if v is None:
            return 0.0
        f = float(v)
        return 0.0 if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return 0.0
