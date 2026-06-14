"""
power_intelligence.py
─────────────────────
Module: Cross-brand power availability intelligence.

Two queries:
1. find_instock_products(sph, cyl, axis)
   → Searches inventory_stock for exact power match (BATCH rows, qty > 0)
   → Returns list of products with available qty, batch, expiry

2. find_orderable_products(sph, cyl, axis)
   → Searches product_power_ranges for products whose range covers this power
   → Returns list of orderable products (not in stock, but available to order)

Used by wholesale_punching.py Power Intelligence Panel.
"""

from typing import Optional
import json
from modules.sql_adapter import run_query


def _sph_in_range(sph: float, sph_min: float, sph_max: float,
                   step_low: float, step_high: float, threshold: float) -> bool:
    """Check if SPH falls on a valid step within range."""
    import math
    if sph < min(sph_min, sph_max) - 0.001 or sph > max(sph_min, sph_max) + 0.001:
        return False
    step = step_low if abs(sph) <= threshold + 0.001 else step_high
    # Check if sph is a multiple of step (within float tolerance)
    if step <= 0: return True
    remainder = abs(round(sph / step, 6)) % 1
    return remainder < 0.01 or remainder > 0.99


def find_instock_products(sph: float, cyl: float = 0.0, axis: int = 0,
                           add_power: float = 0.0) -> list:
    """
    Find all products with this exact power in physical stock.
    Returns list of dicts with product details + batch info.
    """
    try:
        is_toric = cyl not in (None, 0.0, 0)
        is_multifocal = add_power not in (None, 0.0, 0)

        if is_toric:
            rows = run_query("""
                SELECT
                    p.product_name, p.brand, p.wear_schedule, p.category AS type,
                    s.batch_no, s.expiry_date, s.quantity, s.mrp,
                    s.id as stock_id
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE s.stock_type IN ('BATCH','PURCHASE')
                  AND s.is_active = TRUE
                  AND s.quantity > 0
                  AND ABS(s.sph - %(sph)s) < 0.01
                  AND ABS(s.cyl - %(cyl)s) < 0.01
                  AND s.axis = %(axis)s
                ORDER BY p.brand, p.product_name, s.expiry_date
            """, {'sph': sph, 'cyl': cyl, 'axis': axis})
        elif is_multifocal:
            rows = run_query("""
                SELECT
                    p.product_name, p.brand, p.wear_schedule, p.category AS type,
                    s.batch_no, s.expiry_date, s.quantity, s.mrp,
                    s.id as stock_id
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE s.stock_type IN ('BATCH','PURCHASE')
                  AND s.is_active = TRUE
                  AND s.quantity > 0
                  AND ABS(s.sph - %(sph)s) < 0.01
                  AND ABS(COALESCE(s.add_power, 0) - %(add)s) < 0.01
                ORDER BY p.brand, p.product_name, s.expiry_date
            """, {'sph': sph, 'add': add_power})
        else:
            rows = run_query("""
                SELECT
                    p.product_name, p.brand, p.wear_schedule, p.category AS type,
                    s.batch_no, s.expiry_date, s.quantity, s.mrp,
                    s.id as stock_id
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE s.stock_type IN ('BATCH','PURCHASE')
                  AND s.is_active = TRUE
                  AND s.quantity > 0
                  AND ABS(s.sph - %(sph)s) < 0.01
                  AND (s.cyl IS NULL OR ABS(s.cyl) < 0.01)
                  AND (s.axis IS NULL OR s.axis = 0)
                ORDER BY p.brand, p.product_name, s.expiry_date
            """, {'sph': sph})

        return rows or []
    except Exception as e:
        import logging
        logging.warning(f"[power_intelligence] find_orderable_products error: {e}")
        return []


def find_orderable_products(sph: float, cyl: float = 0.0, axis: int = 0,
                             add_power: float = 0.0) -> list:
    """
    Find all products whose power RANGE covers this prescription,
    but which are NOT in stock (order-based brands).
    Returns list of dicts with product + brand info.
    """
    try:
        # First get products already in stock for this power (to exclude)
        instock = find_instock_products(sph, cyl, axis, add_power)
        instock_names = {r['product_name'] for r in instock}

        # Fetch all active ranges
        all_ranges = run_query("""
            SELECT
                pr.id, pr.product_name, pr.brand,
                pr.sph_min, pr.sph_max,
                pr.sph_step_low, pr.sph_step_high, pr.sph_high_threshold,
                pr.cyl_values, pr.axis_values, pr.add_values,
                pr.notes,
                p.wear_schedule, p.box_size
            FROM product_power_ranges pr
            LEFT JOIN products p ON p.product_name = pr.product_name
            WHERE pr.is_active = TRUE
            ORDER BY pr.brand, pr.product_name
        """) or []

        is_toric = cyl not in (None, 0.0, 0)
        is_multifocal = add_power not in (None, 0.0, 0)
        orderable = []

        for r in all_ranges:
            # Skip if in stock
            if r['product_name'] in instock_names:
                continue

            # Check SPH in range
            if not _sph_in_range(
                sph,
                float(r['sph_min']), float(r['sph_max']),
                float(r['sph_step_low']), float(r['sph_step_high']),
                float(r['sph_high_threshold'])
            ):
                continue

            def _parse_json(v):
                if v is None: return []
                if isinstance(v, list): return v   # already parsed by psycopg2
                try: return json.loads(v)
                except: return []
            cyls  = _parse_json(r.get('cyl_values'))
            axes  = _parse_json(r.get('axis_values'))
            adds  = _parse_json(r.get('add_values'))

            # Toric check
            if is_toric:
                if not cyls or not axes:
                    continue
                cyl_ok  = any(abs(float(c) - cyl) < 0.01 for c in cyls)
                axis_ok = any(int(a) == axis for a in axes)
                if not cyl_ok or not axis_ok:
                    continue
            else:
                # SPH-only product — must not be toric
                if cyls:
                    continue

            # Multifocal check
            if is_multifocal:
                if not adds:
                    continue
                if not any(abs(float(a) - add_power) < 0.01 for a in adds):
                    continue

            orderable.append({
                'product_name': r['product_name'],
                'brand':        r['brand'],
                'wear_schedule':r.get('wear_schedule', ''),
                'type':         r.get('type', ''),
                'box_size':     r.get('box_size', 1),
                'mrp':          r.get('mrp'),
                'notes':        r.get('notes', ''),
            })

        return orderable
    except Exception as e:
        import logging
        logging.warning(f"[power_intelligence] find_orderable_products error: {e}")
        return []


def check_power_in_product_range(product_id, product_name: str,
                                   sph: float, cyl: float = 0.0,
                                   axis: int = 0) -> dict:
    """
    Check if a specific power is within a product's available range.
    
    For Alcon (has CATALOGUE rows): checks exact power in inventory_stock
    For other brands: checks product_power_ranges
    
    Returns:
      {'in_range': bool, 'reason': str, 'sph_min': float, 'sph_max': float}
    """
    try:
        is_toric = cyl not in (None, 0.0, 0)

        # ── Check CATALOGUE rows (Alcon) ──────────────────────────────────
        catalogue_check = run_query("""
            SELECT COUNT(*) as cnt,
                   MIN(sph) as sph_min, MAX(sph) as sph_max
            FROM inventory_stock s
            JOIN products p ON p.id = s.product_id
            WHERE p.id::text = %(pid)s
              AND s.stock_type = 'CATALOGUE'
              AND COALESCE(s.is_active, TRUE) = TRUE
        """, {'pid': str(product_id)}) or []

        catalogue_count = int((catalogue_check[0] or {}).get('cnt', 0)) if catalogue_check else 0

        if catalogue_count > 0:
            # Alcon product — check exact power in catalogue
            if is_toric:
                exact = run_query("""
                    SELECT COUNT(*) as cnt
                    FROM inventory_stock s
                    JOIN products p ON p.id = s.product_id
                    WHERE p.id::text = %(pid)s
                      AND s.stock_type = 'CATALOGUE'
                      AND ABS(s.sph - %(sph)s) < 0.01
                      AND ABS(COALESCE(s.cyl, 0) - %(cyl)s) < 0.01
                      AND COALESCE(s.axis, 0) = %(axis)s
                      AND COALESCE(s.is_active, TRUE) = TRUE
                """, {'pid': str(product_id), 'sph': sph,
                      'cyl': cyl, 'axis': axis}) or []
            else:
                exact = run_query("""
                    SELECT COUNT(*) as cnt
                    FROM inventory_stock s
                    JOIN products p ON p.id = s.product_id
                    WHERE p.id::text = %(pid)s
                      AND s.stock_type = 'CATALOGUE'
                      AND ABS(s.sph - %(sph)s) < 0.01
                      AND COALESCE(s.is_active, TRUE) = TRUE
                """, {'pid': str(product_id), 'sph': sph}) or []

            found = int((exact[0] or {}).get('cnt', 0)) if exact else 0
            sph_min = float((catalogue_check[0] or {}).get('sph_min') or 0)
            sph_max = float((catalogue_check[0] or {}).get('sph_max') or 0)

            if found == 0:
                return {
                    'in_range': False,
                    'reason':   f"SPH {sph:+.2f} not available in {product_name} "
                                f"(range: {sph_min:+.2f} to {sph_max:+.2f})",
                    'sph_min':  sph_min,
                    'sph_max':  sph_max,
                }
            return {'in_range': True, 'reason': '', 'sph_min': sph_min, 'sph_max': sph_max}

        # ── Check product_power_ranges (CV/BL/JJ/Silklens/CEPL) ──────────
        ranges = run_query("""
            SELECT sph_min, sph_max, sph_step_low, sph_step_high,
                   sph_high_threshold, cyl_values, axis_values
            FROM product_power_ranges
            WHERE product_name = %(pname)s
              AND is_active = TRUE
            LIMIT 1
        """, {'pname': product_name}) or []

        if ranges:
            r = ranges[0]
            sph_min = float(r['sph_min']); sph_max = float(r['sph_max'])
            in_range = _sph_in_range(
                sph, sph_min, sph_max,
                float(r['sph_step_low']), float(r['sph_step_high']),
                float(r['sph_high_threshold'])
            )
            if not in_range:
                return {
                    'in_range': False,
                    'reason':   f"SPH {sph:+.2f} not available in {product_name} "
                                f"(range: {sph_min:+.2f} to {sph_max:+.2f})",
                    'sph_min':  sph_min,
                    'sph_max':  sph_max,
                }
            # Check CYL range for toric
            if is_toric:
                def _pj(v):
                    if v is None: return []
                    if isinstance(v, list): return v
                    try: return json.loads(v)
                    except: return []
                cyls = _pj(r.get('cyl_values'))
                if cyls and not any(abs(float(c) - cyl) < 0.01 for c in cyls):
                    return {
                        'in_range': False,
                        'reason':   f"CYL {cyl:+.2f} not available in {product_name} "
                                    f"(available: {', '.join(str(c) for c in cyls)})",
                        'sph_min':  sph_min,
                        'sph_max':  sph_max,
                    }
            return {'in_range': True, 'reason': '', 'sph_min': sph_min, 'sph_max': sph_max}

    except Exception:
        pass

    # No range data — assume in range
    return {'in_range': True, 'reason': '', 'sph_min': 0.0, 'sph_max': 0.0}



# ── Colour detection ──────────────────────────────────────────────────────────
_COLOUR_KEYWORDS = {
    'blue','green','grey','gray','brown','hazel','honey','purple','turquoise',
    'rainbow','circle','diamond','salsa','hip hop','trueblends','day2day',
    'breeze','peppy','groovy','jazzy','spicy','icy','mystery','naughty','envy',
    'pretty','trendy','cool','magic','soft','aqua','bold','sporty','true',
    'dream','glory','lively','cute','posh','color','colour','lacelle',
}

def is_colour_product(product_name: str) -> bool:
    """Returns True if product name suggests a colour/cosmetic lens."""
    name_lower = (product_name or '').lower()
    return any(kw in name_lower for kw in _COLOUR_KEYWORDS)


# ── Nearest power finder ──────────────────────────────────────────────────────

def find_nearest_powers(product_id, product_name: str,
                         sph: float, cyl: float = 0.0,
                         axis: int = 0, n: int = 2) -> list:
    """
    For a given product + power combination that is OUT OF STOCK,
    find the nearest available powers (up to n above and n below).

    Returns list of dicts:
      [{'sph': float, 'cyl': float, 'axis': int, 'qty': int,
        'batch_no': str, 'expiry': str, 'direction': 'above'|'below'}]
    Sorted by distance from requested sph.
    """
    try:
        is_toric = cyl not in (None, 0.0, 0)
        params = {'pid': str(product_id)}

        if is_toric:
            rows = run_query("""
                SELECT s.sph, s.cyl, s.axis, SUM(s.quantity) as qty,
                       MIN(s.batch_no) as batch_no,
                       MIN(s.expiry_date::text) as expiry
                FROM inventory_stock s
                WHERE s.product_id::text = %(pid)s
                  AND s.stock_type IN ('BATCH','PURCHASE')
                  AND s.quantity > 0
                  AND COALESCE(s.is_active, TRUE) = TRUE
                  AND ABS(COALESCE(s.cyl, 0) - %(cyl)s) < 0.01
                  AND COALESCE(s.axis, 0) = %(axis)s
                GROUP BY s.sph, s.cyl, s.axis
                ORDER BY ABS(s.sph - %(sph)s)
                LIMIT %(n)s
            """, {**params, 'cyl': cyl, 'axis': axis, 'sph': sph, 'n': n * 2}) or []
        else:
            rows = run_query("""
                SELECT s.sph, COALESCE(s.cyl, 0) as cyl,
                       COALESCE(s.axis, 0) as axis,
                       SUM(s.quantity) as qty,
                       MIN(s.batch_no) as batch_no,
                       MIN(s.expiry_date::text) as expiry
                FROM inventory_stock s
                WHERE s.product_id::text = %(pid)s
                  AND s.stock_type IN ('BATCH','PURCHASE')
                  AND s.quantity > 0
                  AND COALESCE(s.is_active, TRUE) = TRUE
                  AND (s.cyl IS NULL OR ABS(s.cyl) < 0.01)
                GROUP BY s.sph, s.cyl, s.axis
                ORDER BY ABS(s.sph - %(sph)s)
                LIMIT %(n)s
            """, {**params, 'sph': sph, 'n': n * 2}) or []

        result = []
        for r in rows:
            r_sph = float(r.get('sph') or 0)
            if abs(r_sph - sph) < 0.001:
                continue   # skip exact match (it's the one that's out of stock)
            direction = 'above' if r_sph > sph else 'below'
            result.append({
                'sph':       r_sph,
                'cyl':       float(r.get('cyl') or 0),
                'axis':      int(r.get('axis') or 0),
                'qty':       int(r.get('qty') or 0),
                'batch_no':  r.get('batch_no', ''),
                'expiry':    (r.get('expiry') or '')[:7],
                'direction': direction,
                'distance':  abs(r_sph - sph),
            })

        # Return closest n above + n below
        above = sorted([r for r in result if r['direction']=='above'],
                       key=lambda x: x['distance'])[:n]
        below = sorted([r for r in result if r['direction']=='below'],
                       key=lambda x: x['distance'])[:n]
        return sorted(above + below, key=lambda x: x['distance'])

    except Exception as e:
        import logging
        logging.warning(f"[find_nearest_powers] {e}")
        return []


def power_intelligence_summary(sph: float, cyl: float = 0.0,
                                 axis: int = 0, add_power: float = 0.0) -> dict:
    """
    Full intelligence summary for a given power.
    Returns:
      {
        'instock':   [...],  # products with physical stock
        'orderable': [...],  # products available to order
        'total_instock_qty': int,
      }
    """
    instock   = find_instock_products(sph, cyl, axis, add_power)
    orderable = find_orderable_products(sph, cyl, axis, add_power)

    return {
        'instock':            instock,
        'orderable':          orderable,
        'total_instock_qty':  sum(int(r.get('quantity', 0)) for r in instock),
    }
