"""
modules/core/price_qty_governor.py
====================================
PRICE & QUANTITY GOVERNOR — DV ERP

Single source of truth for:
  1. Price field resolution   — which DB column is correct for each order_type
  2. Price normalization      — BOX price → PCS price (one implementation, everywhere)
  3. Price sync validation    — unit_price in order_lines matches what DB says it should be
  4. Quantity normalization   — box/pcs/pair → final_pcs (one implementation, everywhere)
  5. Quantity display reverse — final_pcs → box/pair/pcs for UI display
  6. 2pc = Pair rule          — enforced here, not scattered across screens

USAGE:
    from modules.core.price_qty_governor import (
        resolve_price,
        normalize_to_pcs_price,
        validate_line_price,
        normalize_qty,
        reverse_qty,
        check_sync,
        PRICE_FIELD,
    )

DESIGN RULES:
  - This module never raises. All errors are returned as structured dicts.
  - No Streamlit imports. No DB imports. Pure logic only.
  - Import this; don't copy it. retail_punching and wholesale_punching
    both delegate here — they do NOT have their own implementations.
"""

import math
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — PRICE FIELD MAP
# "Which DB column holds the correct price for this order_type?"
# ═══════════════════════════════════════════════════════════════════════════════

# Primary field per order type (the field that MUST be used)
PRICE_FIELD: Dict[str, str] = {
    "RETAIL":    "mrp",            # Counter / sticker price, GST-inclusive
    "WHOLESALE": "selling_price",  # Trade / distributor price, ex-GST
    "PURCHASE":  "purchase_rate",  # Cost / inward price
    "ONLINE":    "online_price",   # E-commerce price
}

# Fallback chain per order type (first non-zero value in the list wins)
_PRICE_FALLBACK: Dict[str, List[str]] = {
    "RETAIL":    ["mrp",          "selling_price", "unit_price", "price"],
    "WHOLESALE": ["selling_price", "mrp",          "unit_price", "price"],
    "PURCHASE":  ["purchase_rate", "cost_price",   "mrp"],
    "ONLINE":    ["online_price",  "mrp",          "selling_price", "unit_price"],
}
_DEFAULT_FALLBACK = ["selling_price", "mrp", "unit_price", "price"]

# GST handling per order type
# RETAIL: MRP is GST-inclusive → extract GST from MRP for tax lines
# WHOLESALE / PURCHASE: price is ex-GST → add GST on top
GST_INCLUSIVE: Dict[str, bool] = {
    "RETAIL":    True,
    "WHOLESALE": False,
    "PURCHASE":  False,
    "ONLINE":    False,
}


def _safe_float(val) -> float:
    """Convert any DB value to float safely. Returns 0.0 for None/NaN/invalid."""
    try:
        f = float(val)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# resolve_price
# ─────────────────────────────────────────────────────────────────────────────

def resolve_price(row: dict, order_type: str) -> float:
    """
    Returns the correct RAW base price from a DB row for this order_type.

    This is the price as stored in DB — it may be a BOX price.
    Always follow with normalize_to_pcs_price() before using in billing.

    IMPORTANT — DV ERP price source:
        Prices (selling_price, mrp, purchase_rate) live in inventory_stock,
        NOT in the products table. Before calling this function, the row dict
        must be hydrated from inventory_stock (done by _hydrate_product_gst
        in retail_punching.py and wholesale_punching.py).

        Priority chain for WHOLESALE: selling_price → mrp → unit_price → price
        If selling_price = 0 in inventory_stock, mrp is used as fallback.
        Fix: ensure inventory_stock.selling_price is set for every batch.

    Args:
        row:        dict from DB — inventory_stock row or hydrated product row.
        order_type: "RETAIL" | "WHOLESALE" | "PURCHASE" | "ONLINE"

    Returns:
        float: First non-zero value from priority chain, or 0.0.
    """
    ot     = str(order_type or "RETAIL").upper().strip()
    fields = _PRICE_FALLBACK.get(ot, _DEFAULT_FALLBACK)

    for field in fields:
        val = _safe_float(row.get(field))
        if val > 0:
            return val

    return 0.0


def get_primary_price_field(order_type: str) -> str:
    """
    Returns the PRIMARY price field name for an order_type.
    Use for logging / display: "Using mrp for RETAIL order".
    """
    ot = str(order_type or "RETAIL").upper().strip()
    return PRICE_FIELD.get(ot, "selling_price")


def is_gst_inclusive(order_type: str) -> bool:
    """Returns True if the price for this order_type includes GST (e.g. RETAIL MRP)."""
    ot = str(order_type or "RETAIL").upper().strip()
    return GST_INCLUSIVE.get(ot, False)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — PRICE NORMALIZATION  (BOX → PCS)
# ═══════════════════════════════════════════════════════════════════════════════

def is_box_product(product: dict) -> bool:
    """
    A product is a box product if:
      - unit == 'BOX'  AND  box_size > 1

    Rule: unit alone is unreliable (can be 'NOS', 'PCS' even for box products
    entered inconsistently). box_size > 1 is the definitive signal.
    This function is the single definition — not duplicated in retail/wholesale.
    """
    unit     = str(product.get("unit") or "").upper().strip()
    box_size = int(product.get("box_size") or 0)
    return unit == "BOX" and box_size > 1


def normalize_to_pcs_price(raw_price, product: dict) -> float:
    """
    Convert a DB price (may be BOX price) to per-PCS price.

    THIS is the single implementation. retail_punching and wholesale_punching
    both import this — they do NOT have their own copies.

    Logic:
      - If box_size > 1: divide raw_price by box_size to get per-PCS
      - Otherwise: return as-is

    Args:
        raw_price: Price from DB (might be BOX or PCS)
        product:   Product dict with at minimum 'unit' and 'box_size'

    Returns:
        float: Price per single PCS, rounded to 2 decimals.
    """
    price = _safe_float(raw_price)
    if not price:
        return 0.0

    box_size = int(product.get("box_size") or 0)
    if box_size > 1:
        return round(price / box_size, 2)

    return round(price, 2)


def normalize_box_total(box_price: float, qty_pcs: int, product: dict) -> float:
    """
    Calculate billing total for BOX products WITHOUT per-PCS rounding error.

    PROBLEM: box_price/box_size x qty_pcs introduces rounding error.
    e.g. 500/6pcs = 83.33/pc x 12pcs = 999.96 (not 1000.00)

    CORRECT: (qty_pcs / box_size) x box_price
    e.g. (12/6) x 500 = 2 x 500 = 1000.00

    Rules:
      - BOX products (unit=BOX, box_size>1): use box math
      - All others: unit_price x qty_pcs (no box_size involved)

    Args:
        box_price: Price per BOX as stored in inventory_stock
        qty_pcs:   Quantity in PCS (final_pcs stored internally)
        product:   Product dict with unit, box_size

    Returns:
        float: Exact billing total, no rounding error.
    """
    price    = _safe_float(box_price)
    qty      = int(qty_pcs or 0)
    box_size = int(product.get("box_size") or 0)

    if box_size > 1 and qty > 0 and price > 0:
        full_boxes = qty // box_size
        loose_pcs  = qty %  box_size
        total      = round(full_boxes * price, 2)
        if loose_pcs > 0:
            # Partial box: charge per-PCS for remainder
            pcs_price = round(price / box_size, 2)
            total     = round(total + loose_pcs * pcs_price, 2)
        return total

    # Non-box product: pcs_price x qty
    pcs_price = normalize_to_pcs_price(price, product)
    return round(pcs_price * qty, 2)


def pcs_price_to_box_price(pcs_price: float, product: dict) -> float:
    """
    Reverse of normalize_to_pcs_price.
    Returns the price for a full box (for display in box-billing UI).
    """
    price    = _safe_float(pcs_price)
    box_size = max(1, int(product.get("box_size") or 1))
    return round(price * box_size, 2)


def price_for_display(pcs_price: float, product: dict, mode: str = "PCS") -> Dict:
    """
    Returns price display dict for UI (billing screens, challan, prints).

    Returns:
        {
          "pcs_price":   float,   # per PCS (always)
          "box_price":   float,   # per BOX (if box product, else same as pcs_price)
          "pair_price":  float,   # per PAIR = pcs_price × 2 (for lens display)
          "display_str": str,     # formatted for UI e.g. "₹150.00/pc  (₹300.00/pair)"
          "mode":        str,     # "PCS" | "BOX" | "PAIR"
        }
    """
    pcs   = _safe_float(pcs_price)
    box_s = max(1, int(product.get("box_size") or 1))

    box  = round(pcs * box_s, 2)
    pair = round(pcs * 2, 2)

    if mode == "BOX" and box_s > 1:
        disp = f"₹{box:,.2f}/box  (₹{pcs:,.2f}/pc)"
    elif mode == "PAIR":
        disp = f"₹{pcs:,.2f}/pc  (₹{pair:,.2f}/pair)"
    else:
        disp = f"₹{pcs:,.2f}/pc"

    return {
        "pcs_price":   pcs,
        "box_price":   box,
        "pair_price":  pair,
        "display_str": disp,
        "mode":        mode,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — PRICE SYNC VALIDATION
# "Does the unit_price stored in order_lines match DB truth for this order_type?"
# ═══════════════════════════════════════════════════════════════════════════════

# Tolerance: prices are considered in sync if they differ by less than this
_PRICE_SYNC_TOLERANCE = 0.05   # ₹0.05 per PCS

class PriceSyncResult:
    """Result of a price sync check."""
    __slots__ = (
        "order_line_id", "product_name", "order_type",
        "stored_price", "expected_price", "primary_field",
        "in_sync", "diff", "severity", "message"
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    def __repr__(self):
        status = "OK" if self.in_sync else f"DRIFT ₹{self.diff:.2f}"
        return f"<PriceSyncResult [{status}] {self.product_name} ({self.order_type})>"


def validate_line_price(
    line: dict,
    inventory_row: dict,
    product: dict,
    order_type: str,
    tolerance: float = _PRICE_SYNC_TOLERANCE,
) -> PriceSyncResult:
    """
    Check that unit_price in an order_line matches the expected price
    from inventory_stock for this order_type.

    Args:
        line:          order_lines row — must have 'unit_price'
        inventory_row: inventory_stock row — has selling_price, mrp, purchase_rate
        product:       products row — has box_size, unit
        order_type:    "RETAIL" | "WHOLESALE" | "PURCHASE"
        tolerance:     max allowed ₹ diff per PCS (default ₹0.05)

    Returns:
        PriceSyncResult with in_sync=True/False, diff, severity, message
    """
    stored_raw    = _safe_float(line.get("unit_price"))
    # Normalize stored price to PCS so we compare apples-to-apples.
    # order_lines.unit_price should always be per-PCS (governor rule),
    # but older saves may have stored BOX price. Normalize before compare.
    stored_pcs    = normalize_to_pcs_price(stored_raw, product)
    raw_db_price  = resolve_price(inventory_row or {}, order_type)
    expected_pcs  = normalize_to_pcs_price(raw_db_price, product)
    primary_field = get_primary_price_field(order_type)

    # Compare normalized PCS prices
    diff = abs(stored_pcs - expected_pcs) if expected_pcs > 0 else 0.0
    in_sync = (expected_pcs == 0) or (diff <= tolerance)
    stored_raw = stored_pcs  # report the normalized value for clarity

    # Severity
    if in_sync:
        severity = "ok"
        msg = "Price in sync"
    elif diff <= 1.0:
        severity = "warn"
        msg = (
            f"Minor price drift ₹{diff:.2f}: "
            f"stored={stored_raw:.2f}, expected {primary_field}={expected_pcs:.2f}"
        )
    else:
        severity = "error"
        msg = (
            f"Price mismatch ₹{diff:.2f}: "
            f"stored={stored_raw:.2f} but {order_type} should use "
            f"{primary_field}={expected_pcs:.2f}"
        )

    return PriceSyncResult(
        order_line_id = line.get("id") or line.get("order_line_id"),
        product_name  = line.get("product_name") or product.get("product_name", "?"),
        order_type    = order_type,
        stored_price  = stored_raw,
        expected_price= expected_pcs,
        primary_field = primary_field,
        in_sync       = in_sync,
        diff          = round(diff, 4),
        severity      = severity,
        message       = msg,
    )


def validate_order_prices(
    lines: List[dict],
    inventory_map: Dict[str, dict],
    product_map: Dict[str, dict],
    order_type: str,
) -> Dict:
    """
    Validate all lines in an order.

    Args:
        lines:         list of order_lines dicts
        inventory_map: {product_id: inventory_stock row}  (pre-fetched)
        product_map:   {product_id: products row}          (pre-fetched)
        order_type:    order-level order_type string

    Returns:
        {
            "all_ok":    bool,
            "errors":    [PriceSyncResult.to_dict(), ...],   # severity == error
            "warnings":  [PriceSyncResult.to_dict(), ...],   # severity == warn
            "results":   [PriceSyncResult.to_dict(), ...],   # all results
        }
    """
    results  = []
    errors   = []
    warnings = []

    for line in (lines or []):
        pid      = str(line.get("product_id") or "")
        inv_row  = inventory_map.get(pid) or {}
        prod_row = product_map.get(pid) or {}
        ot       = str(line.get("order_type") or order_type or "RETAIL").upper()

        r = validate_line_price(line, inv_row, prod_row, ot)
        results.append(r.to_dict())
        if r.severity == "error":
            errors.append(r.to_dict())
        elif r.severity == "warn":
            warnings.append(r.to_dict())

    return {
        "all_ok":   len(errors) == 0,
        "errors":   errors,
        "warnings": warnings,
        "results":  results,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — QUANTITY NORMALIZATION  (box/pair/pcs → final_pcs)
# ═══════════════════════════════════════════════════════════════════════════════

# Quantity modes (mirrors QuantityEngine but authoritative here)
QTY_MODE_PCS_ONLY  = "PCS_ONLY"
QTY_MODE_BOX_ONLY  = "BOX_ONLY"
QTY_MODE_FLEX      = "FLEX"        # box + loose pcs
QTY_MODE_PAIR_ONLY = "PAIR_ONLY"
QTY_MODE_PAIR_FLEX = "PAIR_FLEX"   # pair + loose pcs
QTY_MODE_NO_ONLY   = "NO_ONLY"     # NOS / units


def detect_qty_mode(product: dict) -> str:
    """
    Determine the quantity mode for a product.

    DB stores ALL quantities in PCS universally (SAP convention).
    Display layer converts PCS → boxes/pairs for UI.

    Rules:
      box_size > 1            → BOX product (unit field is unreliable)
        + allow_loose         → FLEX  (boxes + loose pcs allowed)
        + not allow_loose     → BOX_ONLY
      unit == 'PAIR'          → PAIR product (1 pair = 2 pcs)
        + allow_loose         → PAIR_FLEX
        + not allow_loose     → PAIR_ONLY
      unit == 'NO'            → NO_ONLY (numbered units)
      everything else         → PCS_ONLY

    Key insight: box_size > 1 takes precedence over unit field because:
    - Products table unit column is inconsistently filled ('PCS', 'BOX', 'NOS')
    - box_size > 1 is the DEFINITIVE signal that this is a box product
    - A contact lens with unit='PCS' and box_size=6 IS a box product
    """
    unit        = str(product.get("unit") or "PCS").upper().strip()
    box_size    = int(product.get("box_size") or 0)
    allow_loose = product.get("allow_loose")
    loose       = allow_loose in (True, "t", "true", "True", 1, "1")

    # box_size > 1 is definitive — regardless of unit field
    if box_size > 1:
        return QTY_MODE_FLEX if loose else QTY_MODE_BOX_ONLY

    # PAIR: 1 pair = 2 pcs (spectacle lenses, ophthalmic)
    if unit == "PAIR":
        return QTY_MODE_PAIR_FLEX if loose else QTY_MODE_PAIR_ONLY

    if unit == "NO":
        return QTY_MODE_NO_ONLY

    return QTY_MODE_PCS_ONLY


# ─────────────────────────────────────────────────────────────────────────────
# 2pc = PAIR RULE
# 1 pair = 2 pcs.  This is enforced here, never assumed elsewhere.
# ─────────────────────────────────────────────────────────────────────────────
PAIR_TO_PCS = 2   # 1 PAIR = 2 PCS — this constant is the law


def normalize_qty(user_input: dict, product: dict) -> Dict:
    """
    Convert user-entered box/pair/pcs values into a canonical final_pcs count.

    THIS is the single implementation for qty normalization.
    retail_punching, wholesale_punching, backoffice all call this.

    Args:
        user_input: {
            "box":  int,    # number of boxes entered (0 if not applicable)
            "pcs":  int,    # loose pcs entered        (0 if not applicable)
            "pair": float,  # pairs entered (0.5 = 1 lens) (0 if not applicable)
        }
        product: product dict with 'unit', 'box_size', 'allow_loose'

    Returns:
        {
            "final_pcs": int,     # canonical unit for stock/billing
            "mode":      str,     # qty mode used
            "box_size":  int,     # from product
            "box":       int,     # as entered
            "pcs":       int,     # as entered (loose)
            "pair":      float,   # as entered
            "is_valid":  bool,
            "errors":    [str],
        }
    """
    mode     = detect_qty_mode(product)
    box_size = max(1, int(product.get("box_size") or 1))

    box  = int(user_input.get("box",  0) or 0)
    pcs  = int(user_input.get("pcs",  0) or 0)
    pair = float(user_input.get("pair", 0) or 0)

    final_pcs = 0

    if mode in (QTY_MODE_BOX_ONLY, QTY_MODE_FLEX):
        final_pcs += box * box_size

    if mode in (QTY_MODE_PAIR_ONLY, QTY_MODE_PAIR_FLEX):
        # 2pc = PAIR rule: 1 pair = PAIR_TO_PCS pcs
        final_pcs += int(pair * PAIR_TO_PCS)

    if mode in (QTY_MODE_PCS_ONLY, QTY_MODE_PAIR_FLEX, QTY_MODE_FLEX, QTY_MODE_NO_ONLY):
        final_pcs += pcs

    # Validation
    errors = []

    if final_pcs <= 0:
        errors.append("Quantity must be greater than zero")

    if mode == QTY_MODE_BOX_ONLY and pcs > 0:
        errors.append("Loose PCS not allowed — this product is BOX only")

    if mode == QTY_MODE_PAIR_ONLY and pcs > 0:
        errors.append("Loose PCS not allowed — this product is PAIR only")

    if mode in (QTY_MODE_PAIR_ONLY, QTY_MODE_PAIR_FLEX):
        # Pairs must be in steps of 0.5
        if (pair * 2) % 1 != 0:
            errors.append("Pair quantity must be in steps of 0.5 (e.g. 0.5, 1.0, 1.5)")

    return {
        "final_pcs": final_pcs,
        "mode":      mode,
        "box_size":  box_size,
        "box":       box,
        "pcs":       pcs,
        "pair":      pair,
        "is_valid":  len(errors) == 0,
        "errors":    errors,
    }


def reverse_qty(final_pcs: int, product: dict) -> Dict:
    """
    Convert a stored final_pcs back into display units for UI.

    Used when loading saved order lines back into editing screens.

    Returns:
        {
            "box":       int,    # full boxes
            "pcs":       int,    # remainder loose pcs (for FLEX mode)
            "pair":      float,  # pairs (for PAIR modes)
            "display":   str,    # human label e.g. "2 Box (20 pcs)" or "3 Pair"
            "mode":      str,
        }
    """
    mode     = detect_qty_mode(product)
    box_size = max(1, int(product.get("box_size") or 1))

    try:
        qty = int(final_pcs or 0)
    except (TypeError, ValueError):
        qty = 0

    if qty <= 0:
        return {"box": 0, "pcs": 0, "pair": 0.0, "display": "0", "mode": mode}

    if mode in (QTY_MODE_BOX_ONLY, QTY_MODE_FLEX):
        full_boxes = qty // box_size
        loose_pcs  = qty %  box_size
        total_pcs  = qty
        # Display: "10 box(es) (60 pcs)" or "10 box(es) (60 pcs) + 2 loose"
        display = f"{full_boxes} box(es) ({total_pcs} pcs)"
        if loose_pcs:
            display = (f"{full_boxes} box(es) ({full_boxes * box_size} pcs)"
                       f" + {loose_pcs} loose pcs")
        return {"box": full_boxes, "pcs": loose_pcs, "pair": 0.0,
                "display": display, "mode": mode, "total_pcs": total_pcs}

    if mode in (QTY_MODE_PAIR_ONLY, QTY_MODE_PAIR_FLEX):
        # 2pc = PAIR rule: pcs / PAIR_TO_PCS = pairs
        pairs = qty / PAIR_TO_PCS
        loose = 0
        if mode == QTY_MODE_PAIR_FLEX:
            full_pairs = int(qty // PAIR_TO_PCS)
            loose      = qty % PAIR_TO_PCS
            pairs = float(full_pairs) + (0.5 if loose else 0)
            display = f"{pairs:.1f} Pair"
            if loose:
                display += f" + {loose} pcs"
        else:
            pairs = float(qty) / PAIR_TO_PCS
            display = f"{pairs:.1f} Pair"
        return {"box": 0, "pcs": loose, "pair": pairs, "display": display, "mode": mode}

    # PCS_ONLY / NO_ONLY
    return {"box": 0, "pcs": qty, "pair": 0.0, "display": f"{qty} pcs", "mode": mode}


def pcs_to_display(pcs: int, product: dict) -> str:
    """
    Convert raw PCS quantity (as stored in DB) to human-readable display string.

    DB stores everything in PCS universally. This converts to:
      BOX products:  60 pcs → "10 box(es) (60 pcs)"  [box_size=6]
                     62 pcs → "10 box(es) (60 pcs) + 2 loose pcs"
      PAIR products: 4 pcs  → "2 pair(s) (4 pcs)"
                     5 pcs  → "2 pair(s) + 1 pcs"
      PCS products:  60 pcs → "60 pcs"

    Args:
        pcs:     raw quantity as stored in inventory_stock or order_lines
        product: product dict with box_size, unit, allow_loose

    Returns:
        str: display string for UI
    """
    r = reverse_qty(pcs, product)
    return r["display"]


def box_qty_label(box: int, pcs_extra: int, box_size: int) -> str:
    """
    Format box quantity for display in billing lines, challan, and prints.

    Examples:
      box=2, pcs_extra=0, box_size=10 → "2 Box (20 pcs)"
      box=2, pcs_extra=3, box_size=10 → "2 Box + 3 pcs (23 pcs)"
      box=1, pcs_extra=0, box_size=6  → "1 Box (6 pcs)"
    """
    total = box * box_size + pcs_extra
    label = f"{box} Box ({total} pcs)"
    if pcs_extra > 0:
        label = f"{box} Box + {pcs_extra} pcs ({total} pcs)"
    return label


def pair_qty_label(pair: float, pcs_extra: int = 0) -> str:
    """
    Format pair quantity for display.

    Examples:
      pair=2.0, pcs_extra=0 → "2 Pair (4 pcs)"
      pair=1.5, pcs_extra=0 → "1.5 Pair (3 pcs)"
      pair=2.0, pcs_extra=1 → "2 Pair + 1 pc (5 pcs)"
    """
    pcs_from_pair = int(pair * PAIR_TO_PCS)
    total = pcs_from_pair + pcs_extra
    pair_str = f"{pair:.1f}" if pair != int(pair) else str(int(pair))
    label = f"{pair_str} Pair ({total} pcs)"
    if pcs_extra:
        label = f"{pair_str} Pair + {pcs_extra} pc ({total} pcs)"
    return label


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — CROSS-SCREEN SYNC CHECKER
# "Are price + qty consistent from punch → billing summary → challan → print?"
# ═══════════════════════════════════════════════════════════════════════════════

def check_sync(
    punch_line: dict,
    inventory_row: dict,
    product: dict,
    order_type: str,
) -> Dict:
    """
    Master sync check for a single order line.

    Runs BOTH price sync and quantity consistency checks.
    Call this from the billing summary, challan preview, and print to
    catch drift before it reaches the customer.

    Args:
        punch_line:    order_lines row as stored (has unit_price, quantity, etc.)
        inventory_row: matching inventory_stock row
        product:       matching products row
        order_type:    "RETAIL" | "WHOLESALE"

    Returns:
        {
            "ok":           bool,     # True if everything is in sync
            "price_check":  {...},    # from validate_line_price
            "qty_check":    {...},    # quantity consistency results
            "warnings":     [str],    # human-readable issues
        }
    """
    warnings = []

    # ── Price check ──────────────────────────────────────────────────────────
    price_result = validate_line_price(
        punch_line, inventory_row, product, order_type
    )

    # ── Quantity consistency check ────────────────────────────────────────────
    stored_qty  = int(punch_line.get("quantity") or 0)
    mode        = detect_qty_mode(product)
    box_size    = max(1, int(product.get("box_size") or 1))
    qty_ok      = True
    qty_issues  = []

    # BOX products: qty must be a multiple of box_size (unless allow_loose)
    allow_loose = product.get("allow_loose") in (True, "t", "true", "True", 1)
    if mode == QTY_MODE_BOX_ONLY and box_size > 1:
        if stored_qty % box_size != 0:
            qty_ok = False
            remainder = stored_qty % box_size
            qty_issues.append(
                f"Qty {stored_qty} is not a multiple of box_size {box_size} "
                f"(remainder {remainder} pcs)"
            )

    # PAIR products: qty must be even (2pc = pair rule)
    if mode == QTY_MODE_PAIR_ONLY:
        if stored_qty % PAIR_TO_PCS != 0:
            qty_ok = False
            qty_issues.append(
                f"Qty {stored_qty} is odd — PAIR products must be in "
                f"multiples of {PAIR_TO_PCS} (2pc = 1 pair)"
            )

    # unit_price × quantity must equal total_price (or billing_total)
    stored_total   = _safe_float(punch_line.get("total_price") or punch_line.get("billing_total"))
    # Use PCS-normalized unit_price for the total check
    _raw_up        = _safe_float(punch_line.get("unit_price"))
    _pcs_up        = normalize_to_pcs_price(_raw_up, product)
    computed_total = round(_pcs_up * stored_qty, 2) if _pcs_up > 0 else round(_raw_up * stored_qty, 2)
    # Allow tolerance scaled to qty to avoid false positives on rounding
    _total_tol = max(0.05, stored_qty * 0.01)
    if stored_total > 0 and computed_total > 0 and abs(stored_total - computed_total) > _total_tol:
        qty_issues.append(
            f"Total price mismatch: stored={stored_total:.2f}, "
            f"computed={computed_total:.2f} (unit_price × qty)"
        )
        qty_ok = False

    # ── Build warnings list ───────────────────────────────────────────────────
    if not price_result.in_sync:
        warnings.append(price_result.message)
    warnings.extend(qty_issues)

    return {
        "ok":          price_result.in_sync and qty_ok,
        "price_check": price_result.to_dict(),
        "qty_check":   {
            "ok":      qty_ok,
            "mode":    mode,
            "issues":  qty_issues,
        },
        "warnings":    warnings,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — CONVENIENCE: RESOLVE + NORMALIZE IN ONE CALL
# ═══════════════════════════════════════════════════════════════════════════════

def get_pcs_price(row: dict, order_type: str, product: Optional[dict] = None) -> float:
    """
    One-shot: resolve correct DB price field for order_type + normalize to PCS.

    This replaces the pattern:
        unit_price = resolve_price_for_order_type(row, "RETAIL")
        pcs_price  = normalize_to_pcs_price(unit_price, product)

    With:
        pcs_price = get_pcs_price(row, "RETAIL", product)

    Args:
        row:        DB row (inventory_stock, products, or batch)
        order_type: "RETAIL" | "WHOLESALE" | "PURCHASE"
        product:    Product dict for box normalization. If None, uses `row` itself.

    Returns:
        float: Per-PCS price, ready for billing.
    """
    raw   = resolve_price(row, order_type)
    prod  = product if product is not None else row
    return normalize_to_pcs_price(raw, prod)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — GST COMPUTATION (governed, consistent across challan/print)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_line_gst(
    unit_price_pcs: float,
    quantity: int,
    gst_percent: float,
    order_type: str,
) -> Dict:
    """
    Compute GST for a single line consistently.

    RETAIL (GST-inclusive):  GST = subtotal × gst% / (100 + gst%)
    WHOLESALE (ex-GST):      GST = subtotal × gst% / 100

    Args:
        unit_price_pcs: per-PCS price (already normalized)
        quantity:       final_pcs (already normalized)
        gst_percent:    GST rate (e.g. 12 for 12%)
        order_type:     "RETAIL" | "WHOLESALE"

    Returns:
        {
            "subtotal":     float,   # price × qty
            "gst_amount":   float,
            "grand_total":  float,
            "gst_base":     float,   # taxable base (ex-GST)
            "inclusive":    bool,
        }
    """
    subtotal   = round(_safe_float(unit_price_pcs) * int(quantity or 0), 2)
    gst_pct    = _safe_float(gst_percent)
    inclusive  = is_gst_inclusive(order_type)

    if gst_pct <= 0:
        return {
            "subtotal":    subtotal,
            "gst_amount":  0.0,
            "grand_total": subtotal,
            "gst_base":    subtotal,
            "inclusive":   inclusive,
        }

    if inclusive:
        # MRP includes GST → extract backward
        gst_base   = round(subtotal * 100 / (100 + gst_pct), 2)
        gst_amount = round(subtotal - gst_base, 2)
        grand      = subtotal   # MRP already is the grand total
    else:
        # ex-GST price → add GST
        gst_base   = subtotal
        gst_amount = round(subtotal * gst_pct / 100, 2)
        grand      = round(subtotal + gst_amount, 2)

    return {
        "subtotal":    subtotal,
        "gst_amount":  gst_amount,
        "grand_total": grand,
        "gst_base":    gst_base,
        "inclusive":   inclusive,
    }
