"""
price_governor.py
─────────────────
Central price control module for DV ERP.

Responsibilities:
  1. Get current active price for a product                    → get_current_price()
  2. Get all price versions that still have live BATCH stock   → get_orderable_prices()
  3. Validate no under-invoicing                               → validate_price()
  4. Detect price mismatch at invoice time                     → detect_price_mismatch()
  5. Create new price version when batch arrives at new cost   → create_price_version()
  6. Retire old price when no more old-cost stock exists       → retire_stale_prices()

Rules (non-negotiable):
  - selling_price >= purchase_rate * MIN_MARGIN_FACTOR  (no under-invoice)
  - mrp >= selling_price always (no selling above MRP)
  - Only 1 PRICE row with is_price_current=TRUE per product
  - Old price dropdown shown ONLY if old-cost BATCH stock still exists

Price priority for billing:
  BATCH row price → current PRICE row → products.mrp (last resort)
"""

import uuid
from datetime import date
from typing import Optional
from modules.sql_adapter import run_query, execute_query

# Minimum factor: selling_price must be >= purchase_rate * this
# 0.85 = allows up to 15% below cost (clearance / loss leader — rare)
# Set to 1.0 to never allow below cost
MIN_MARGIN_FACTOR = 0.85

# ── Internal helper ───────────────────────────────────────────────────────────

def _pid(product_id) -> str:
    return str(product_id)


# ══════════════════════════════════════════════════════════════════════════════
# 1. GET CURRENT PRICE
# ══════════════════════════════════════════════════════════════════════════════

def get_current_price(product_id) -> Optional[dict]:
    """
    Returns the current active PRICE row for this product.
    None if no PRICE row exists.
    """
    try:
        rows = run_query("""
            SELECT
                id, product_id,
                mrp, selling_price, purchase_rate,
                effective_from, price_source, is_price_current
            FROM inventory_stock
            WHERE product_id::text = %(pid)s
              AND stock_type = 'PRICE'
              AND is_price_current = TRUE
              AND COALESCE(is_active, TRUE) = TRUE
            LIMIT 1
        """, {'pid': _pid(product_id)})
        return rows[0] if rows else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 2. GET ALL ORDERABLE PRICES (for billing dropdown)
# ══════════════════════════════════════════════════════════════════════════════

def get_orderable_prices(product_id, sph=None, cyl=None, axis=None) -> list:
    """
    Returns all price versions that are valid to bill at.
    A price version is valid if:
      a) It is the current price (is_price_current = TRUE), OR
      b) There is BATCH stock still available at that purchase_rate

    Returns list of dicts sorted by effective_from DESC (newest first).
    Each dict has: mrp, selling_price, purchase_rate, effective_from,
                   is_price_current, has_old_stock, stock_qty
    """
    try:
        # All PRICE rows for this product
        price_rows = run_query("""
            SELECT
                id, mrp, selling_price, purchase_rate,
                effective_from, price_source, is_price_current
            FROM inventory_stock
            WHERE product_id::text = %(pid)s
              AND stock_type = 'PRICE'
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY is_price_current DESC, effective_from DESC NULLS LAST
        """, {'pid': _pid(product_id)}) or []

        if not price_rows:
            return []

        # For each price row, check if BATCH stock exists at that purchase_rate
        result = []
        for pr in price_rows:
            pr_val = float(pr.get('purchase_rate') or 0)

            # Count BATCH stock at this purchase_rate
            stock_qty = 0
            if pr_val > 0:
                params = {
                    'pid': _pid(product_id),
                    'pr':  pr_val,
                    'tol': pr_val * 0.01,  # 1% tolerance for float comparison
                }
                # Power-specific if sph provided
                if sph is not None:
                    params.update({'sph': float(sph), 'cyl': float(cyl or 0), 'axis': int(axis or 0)})
                    qty_rows = run_query("""
                        SELECT COALESCE(SUM(quantity), 0) as qty
                        FROM inventory_stock
                        WHERE product_id::text = %(pid)s
                          AND stock_type IN ('BATCH','PURCHASE')
                          AND quantity > 0
                          AND COALESCE(is_active, TRUE) = TRUE
                          AND ABS(COALESCE(purchase_rate, 0) - %(pr)s) <= %(tol)s
                          AND ABS(COALESCE(sph, 0) - %(sph)s) < 0.01
                          AND ABS(COALESCE(cyl, 0) - %(cyl)s) < 0.01
                          AND COALESCE(axis, 0) = %(axis)s
                    """, params) or []
                else:
                    qty_rows = run_query("""
                        SELECT COALESCE(SUM(quantity), 0) as qty
                        FROM inventory_stock
                        WHERE product_id::text = %(pid)s
                          AND stock_type IN ('BATCH','PURCHASE')
                          AND quantity > 0
                          AND COALESCE(is_active, TRUE) = TRUE
                          AND ABS(COALESCE(purchase_rate, 0) - %(pr)s) <= %(tol)s
                    """, params) or []
                stock_qty = int(qty_rows[0].get('qty', 0)) if qty_rows else 0

            is_current = bool(pr.get('is_price_current'))
            has_old_stock = stock_qty > 0 and not is_current

            # Include if: current price OR has old-cost batch stock
            if is_current or has_old_stock:
                eff = pr.get('effective_from')
                result.append({
                    'id':               str(pr['id']),
                    'mrp':              float(pr.get('mrp') or 0),
                    'selling_price':    float(pr.get('selling_price') or 0),
                    'purchase_rate':    pr_val,
                    'effective_from':   eff.isoformat() if eff else None,
                    'price_source':     pr.get('price_source', 'MANUAL'),
                    'is_price_current': is_current,
                    'has_old_stock':    has_old_stock,
                    'stock_qty':        stock_qty,
                })

        return result
    except Exception as e:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 3. VALIDATE PRICE — NO UNDER-INVOICING
# ══════════════════════════════════════════════════════════════════════════════

def validate_price(selling_price: float, mrp: float,
                   purchase_rate: float, party_type: str = 'WHOLESALE') -> dict:
    """
    Validates a proposed selling price against business rules.
    Returns: {'valid': bool, 'error': str|None, 'warning': str|None}
    """
    sp  = float(selling_price or 0)
    mrp_val = float(mrp or 0)
    pr  = float(purchase_rate or 0)

    # Rule 1: selling_price can never exceed MRP
    if mrp_val > 0 and sp > mrp_val + 0.01:
        return {
            'valid':   False,
            'error':   f"Selling price ₹{sp:,.2f} cannot exceed MRP ₹{mrp_val:,.2f}",
            'warning': None,
        }

    # Rule 2: selling_price cannot go below purchase_rate * MIN_MARGIN_FACTOR
    if pr > 0 and sp > 0:
        min_allowed = pr * MIN_MARGIN_FACTOR
        if sp < min_allowed - 0.01:
            return {
                'valid':   False,
                'error':   (
                    f"⛔ Under-invoice blocked — selling ₹{sp:,.2f} is below "
                    f"minimum allowed ₹{min_allowed:,.2f} "
                    f"(purchase ₹{pr:,.2f} × {MIN_MARGIN_FACTOR}). "
                    f"Issue a Credit Note if needed."
                ),
                'warning': None,
            }

    # Rule 3: Warn if selling below cost (but above MIN_MARGIN_FACTOR floor)
    if pr > 0 and sp > 0 and sp < pr:
        return {
            'valid':   True,
            'error':   None,
            'warning': f"⚠️ Selling ₹{sp:,.2f} is below purchase cost ₹{pr:,.2f}. Confirm?",
        }

    return {'valid': True, 'error': None, 'warning': None}


# ══════════════════════════════════════════════════════════════════════════════
# 4. DETECT PRICE MISMATCH AT INVOICE
# ══════════════════════════════════════════════════════════════════════════════

def detect_price_mismatch(product_id, order_mrp: float,
                           batch_purchase_rate: float) -> Optional[dict]:
    """
    Detects if an order was punched at new price but batch received at old cost.
    Called at invoice generation time.

    Returns None if no mismatch.
    Returns dict with mismatch details if detected.
    """
    try:
        current = get_current_price(product_id)
        if not current:
            return None

        curr_pr = float(current.get('purchase_rate') or 0)
        batch_pr = float(batch_purchase_rate or 0)

        # Mismatch: batch purchase_rate differs from current price row's purchase_rate
        # AND the order was punched at current price level
        if curr_pr > 0 and batch_pr > 0 and abs(curr_pr - batch_pr) > curr_pr * 0.02:
            # Find the PRICE row matching this batch's purchase_rate
            old_rows = run_query("""
                SELECT mrp, selling_price, purchase_rate, effective_from
                FROM inventory_stock
                WHERE product_id::text = %(pid)s
                  AND stock_type = 'PRICE'
                  AND ABS(COALESCE(purchase_rate, 0) - %(bpr)s) <= %(tol)s
                  AND COALESCE(is_active, TRUE) = TRUE
                LIMIT 1
            """, {'pid': _pid(product_id), 'bpr': batch_pr, 'tol': batch_pr * 0.02}) or []

            old_price = old_rows[0] if old_rows else None
            return {
                'mismatch':          True,
                'current_mrp':       float(current.get('mrp') or 0),
                'current_sell':      float(current.get('selling_price') or 0),
                'current_purchase':  curr_pr,
                'batch_purchase':    batch_pr,
                'old_mrp':           float(old_price.get('mrp') or 0) if old_price else None,
                'old_sell':          float(old_price.get('selling_price') or 0) if old_price else None,
                'message': (
                    f"⚠️ This batch was purchased at ₹{batch_pr:,.2f} (old price). "
                    f"Order was punched at current price (purchase ₹{curr_pr:,.2f}). "
                    f"Select selling price below."
                ),
            }
        return None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 5. CREATE PRICE VERSION
# ══════════════════════════════════════════════════════════════════════════════

def create_price_version(product_id, mrp: float, selling_price: float,
                          purchase_rate: float, source: str = 'MANUAL',
                          effective_from: date = None) -> dict:
    """
    Creates a new PRICE row and retires the previous one.
    Call this when:
      - New batch arrives with different purchase_rate
      - Manual price update from admin
      - Supplier invoice loaded with new price

    Returns: {'success': bool, 'id': str, 'error': str|None}
    """
    try:
        # Validate
        check = validate_price(selling_price, mrp, purchase_rate)
        if not check['valid']:
            return {'success': False, 'id': None, 'error': check['error']}

        from modules.sql_adapter import get_transaction_connection, close_connection
        conn = get_transaction_connection()
        cur  = conn.cursor()

        try:
            # Retire current price row
            cur.execute("""
                UPDATE inventory_stock
                SET is_price_current = FALSE, updated_at = NOW()
                WHERE product_id::text = %s
                  AND stock_type = 'PRICE'
                  AND is_price_current = TRUE
            """, (_pid(product_id),))

            # Insert new price row
            new_id = str(uuid.uuid4())
            eff    = effective_from or date.today()
            cur.execute("""
                INSERT INTO inventory_stock (
                    id, product_id, stock_type,
                    mrp, selling_price, purchase_rate,
                    quantity, is_price_current,
                    effective_from, price_source,
                    is_active, created_at, updated_at
                ) VALUES (
                    %s, %s::uuid, 'PRICE',
                    %s, %s, %s,
                    0, TRUE,
                    %s, %s,
                    TRUE, NOW(), NOW()
                )
            """, (new_id, _pid(product_id), round(mrp, 2),
                  round(selling_price, 2), round(purchase_rate, 2),
                  eff, source))

            conn.commit()
            return {'success': True, 'id': new_id, 'error': None}

        except Exception as e:
            conn.rollback()
            return {'success': False, 'id': None, 'error': str(e)}
        finally:
            close_connection(conn)

    except Exception as e:
        return {'success': False, 'id': None, 'error': str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# 6. RETIRE STALE PRICE ROWS
# ══════════════════════════════════════════════════════════════════════════════

def retire_stale_prices(product_id) -> int:
    """
    Deactivates old PRICE rows where no BATCH stock remains at that purchase_rate.
    Does NOT deactivate the current (is_price_current=TRUE) price row.
    Returns count of rows retired.
    """
    try:
        old_rows = run_query("""
            SELECT id, purchase_rate
            FROM inventory_stock
            WHERE product_id::text = %(pid)s
              AND stock_type = 'PRICE'
              AND is_price_current = FALSE
              AND COALESCE(is_active, TRUE) = TRUE
        """, {'pid': _pid(product_id)}) or []

        retired = 0
        for row in old_rows:
            pr  = float(row.get('purchase_rate') or 0)
            if pr <= 0:
                continue
            # Check if any BATCH stock still exists at this purchase_rate
            qty_rows = run_query("""
                SELECT COALESCE(SUM(quantity), 0) as qty
                FROM inventory_stock
                WHERE product_id::text = %(pid)s
                  AND stock_type IN ('BATCH','PURCHASE')
                  AND quantity > 0
                  AND COALESCE(is_active, TRUE) = TRUE
                  AND ABS(COALESCE(purchase_rate, 0) - %(pr)s) <= %(tol)s
            """, {'pid': _pid(product_id), 'pr': pr, 'tol': pr * 0.02}) or []

            qty = int(qty_rows[0].get('qty', 0)) if qty_rows else 0
            if qty == 0:
                execute_query(
                    "UPDATE inventory_stock SET is_active=FALSE, updated_at=NOW() WHERE id=%s",
                    "retire_price_row", params=(str(row['id']),)
                )
                retired += 1
        return retired
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# 7. GET PRICE FOR BILLING (main entry point for punching screens)
# ══════════════════════════════════════════════════════════════════════════════

def _build_billing_result(product_id, prices_list: list,
                           batch_purchase_rate=None, party_type='WHOLESALE') -> dict:
    """Build billing result dict from price rows list."""
    if not prices_list:
        return {'mrp':0.,'selling_price':0.,'purchase_rate':0.,
                'show_dropdown':False,'price_options':[],'mismatch':None,'source':'PRICE'}

    current = next((p for p in prices_list
                    if p.get('is_price_current') or p.get('is_current')), prices_list[0])
    options = []
    for p in prices_list:
        is_curr = bool(p.get('is_price_current') or p.get('is_current'))
        qty = int(p.get('batch_qty') or 0)
        price_tag = "(current)" if is_curr else f"(old — {qty} pcs in stock)"
        label = f"MRP {float(p.get('mrp') or 0):,.0f} / Trade {float(p.get('selling_price') or 0):,.0f}  {price_tag}"
        options.append({
            'mrp':           float(p.get('mrp') or 0),
            'selling_price': float(p.get('selling_price') or 0),
            'purchase_rate': float(p.get('purchase_rate') or 0),
            'label':         label,
            'is_current':    is_curr,
        })

    old_opts = [o for o in options if not o['is_current']]
    mismatch = None
    if batch_purchase_rate:
        mismatch = detect_price_mismatch(product_id,
                                          float(current.get('mrp') or 0),
                                          batch_purchase_rate)
    return {
        'mrp':           float(current.get('mrp') or 0),
        'selling_price': float(current.get('selling_price') or 0),
        'purchase_rate': float(current.get('purchase_rate') or 0),
        'show_dropdown': len(old_opts) > 0,
        'price_options': options,
        'mismatch':      mismatch,
        'source':        'PRICE',
    }


def get_billing_price(product_id, party_type: str = 'WHOLESALE',
                       batch_purchase_rate: float = None,
                       sph=None, cyl=None, axis=None) -> dict:
    # Delegate to price_manager DB functions when available (preferred path)
    try:
        from modules.price_manager import get_billing_prices as _db_prices
        db_prices = _db_prices(str(product_id))
        if db_prices is not None:  # None means function not available yet
            prices_list = db_prices
            return _build_billing_result(product_id, prices_list,
                                          batch_purchase_rate, party_type)
    except Exception:
        pass  # fall through to Python implementation below

    """
    Main entry point for billing screens.
    Returns: {
        'mrp': float,
        'selling_price': float,
        'purchase_rate': float,
        'show_dropdown': bool,         # True if old-price stock exists
        'price_options': list,         # for dropdown: [{mrp, selling_price, label, is_current}]
        'mismatch': dict|None,         # price mismatch warning if applicable
        'source': 'BATCH'|'PRICE'|'PRODUCT'
    }
    """
    result = {
        'mrp': 0.0, 'selling_price': 0.0, 'purchase_rate': 0.0,
        'show_dropdown': False, 'price_options': [],
        'mismatch': None, 'source': 'PRICE'
    }

    # Get all valid price versions
    prices = get_orderable_prices(product_id, sph, cyl, axis)

    if not prices:
        current = get_current_price(product_id)
        if current:
            result.update({
                'mrp':           float(current.get('mrp') or 0),
                'selling_price': float(current.get('selling_price') or 0),
                'purchase_rate': float(current.get('purchase_rate') or 0),
                'source': 'PRICE',
            })
        return result

    # Current price is always index 0 (sorted newest first)
    current = next((p for p in prices if p['is_price_current']), prices[0])
    result.update({
        'mrp':           current['mrp'],
        'selling_price': current['selling_price'],
        'purchase_rate': current['purchase_rate'],
        'source': 'PRICE',
    })

    # Build dropdown options
    options = []
    for p in prices:
        eff = p.get('effective_from') or 'earlier'
        if p['is_price_current']:
            price_tag = "(current)"
        else:
            price_tag = f"(old — {p['stock_qty']} pcs in stock)"
        label = f"MRP {p['mrp']:,.0f} / Trade {p['selling_price']:,.0f}  {price_tag}"
        options.append({
            'mrp':           p['mrp'],
            'selling_price': p['selling_price'],
            'purchase_rate': p['purchase_rate'],
            'label':         label,
            'is_current':    p['is_price_current'],
        })

    # Show dropdown only if there are old-price options beyond current
    old_options = [o for o in options if not o['is_current']]
    result['show_dropdown'] = len(old_options) > 0
    result['price_options'] = options

    # Detect mismatch if batch purchase_rate provided
    if batch_purchase_rate:
        result['mismatch'] = detect_price_mismatch(product_id, current['mrp'], batch_purchase_rate)

    return result
