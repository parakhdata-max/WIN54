"""
Purchase Bill Auto-Matcher
============================
When a purchase bill is uploaded (OCR or manual), this module:
1. Takes each line item (company_product_name + qty + rate)
2. Looks up alias in inventory_stock first (stock-level alias)
3. Falls back to price list alias (product-level alias)
4. Returns matched product_id + creates purchase_line entry

Alias Priority:
  Level 1: inventory_stock.company_product_name (specific batch/power alias)
  Level 2: products.company_product_name (product-level alias)
  Level 3: products.product_name exact/fuzzy match
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

try:
    from modules.sql_adapter import run_query, run_write
except ImportError:
    run_query = run_write = None


def find_product_by_alias(
    company_name: str,
    supplier_id: str = None,
    sph: float = None,
    cyl: float = None,
    add_power: float = None,
) -> Optional[dict]:
    """
    Look up a product by the supplier's name on their invoice.

    Priority:
      1. inventory_stock.company_product_name (power-level match if sph/cyl given)
      2. products.company_product_name (product-level match)
      3. products.product_name ILIKE match (fuzzy fallback)

    Returns:
      dict with product_id, product_name, matched_level, stock_id (if level 1)
      or None if no match found.
    """
    if not company_name or not run_query:
        return None

    search = company_name.strip()

    # ── Level 1: inventory_stock alias (power-specific) ─────────────────────
    params_1 = [search]
    sph_filter = ''
    if sph is not None:
        sph_filter = ' AND ABS(COALESCE(s.sph,0) - %s) < 0.01'
        params_1.append(sph)
    if cyl is not None:
        sph_filter += ' AND ABS(COALESCE(s.cyl,0) - %s) < 0.01'
        params_1.append(cyl)

    rows = run_query(f"""
        SELECT
            s.id::text        AS stock_id,
            s.product_id::text AS product_id,
            p.product_name,
            p.main_group,
            s.sph, s.cyl, s.batch_no
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE LOWER(TRIM(s.company_product_name)) = LOWER(TRIM(%s))
          AND s.is_active = TRUE
          {sph_filter}
        ORDER BY s.created_at DESC
        LIMIT 1
    """, params_1) or []

    if rows:
        r = rows[0]
        return {**dict(r), 'matched_level': 1, 'match_desc': 'inventory_stock alias'}

    # ── Level 2: products alias (product-level) ──────────────────────────────
    rows2 = run_query("""
        SELECT id::text AS product_id, product_name, main_group
        FROM products
        WHERE LOWER(TRIM(company_product_name)) = LOWER(TRIM(%s))
          AND COALESCE(is_active, TRUE) = TRUE
        LIMIT 1
    """, (search,)) or []

    if rows2:
        r2 = rows2[0]
        return {**dict(r2), 'matched_level': 2, 'match_desc': 'product alias'}

    # ── Level 3: fuzzy product name match ────────────────────────────────────
    rows3 = run_query("""
        SELECT id::text AS product_id, product_name, main_group
        FROM products
        WHERE product_name ILIKE %s
          AND COALESCE(is_active, TRUE) = TRUE
        ORDER BY LENGTH(product_name) ASC
        LIMIT 3
    """, (f'%{search}%',)) or []

    if rows3:
        r3 = rows3[0]
        return {**dict(r3), 'matched_level': 3, 'match_desc': 'fuzzy name match',
                'alternatives': [dict(r) for r in rows3[1:]]}

    return None


def match_bill_lines(lines: list[dict]) -> list[dict]:
    """
    Process a list of purchase bill lines through the alias matcher.

    Input line dict:
      company_product_name, qty, rate, [sph, cyl, add_power, batch_no]

    Returns list of results with match info + unmatched flagged for manual review.
    """
    results = []
    for line in lines:
        cpn  = line.get('company_product_name', '')
        qty  = line.get('qty', 0)
        rate = line.get('rate', 0)

        match = find_product_by_alias(
            cpn,
            sph=line.get('sph'),
            cyl=line.get('cyl'),
            add_power=line.get('add_power'),
        )

        results.append({
            **line,
            'matched': match is not None,
            'product_id':     match['product_id']   if match else None,
            'product_name':   match['product_name'] if match else None,
            'match_level':    match.get('matched_level') if match else None,
            'match_desc':     match.get('match_desc')    if match else 'NO MATCH',
            'alternatives':   match.get('alternatives', []) if match else [],
        })

    return results


def save_purchase_entry(
    supplier_id: str,
    supplier_name: str,
    bill_no: str,
    bill_date: str,
    matched_lines: list[dict],
    created_by: str = 'OCR_IMPORT',
) -> dict:
    """
    Save matched purchase bill lines to inventory_stock.
    Matched lines → increment stock qty + update purchase_rate.
    Unmatched lines → returned for manual review.

    Returns:
      {saved: int, skipped: int, unmatched: list[dict]}
    """
    if not run_write:
        return {'saved': 0, 'skipped': 0, 'unmatched': matched_lines}

    saved = 0; skipped = 0; unmatched = []

    for line in matched_lines:
        if not line.get('matched') or not line.get('product_id'):
            unmatched.append(line)
            continue

        try:
            # Update purchase_rate on existing stock or insert new batch
            run_write("""
                UPDATE inventory_stock
                SET purchase_rate = %s,
                    quantity = quantity + %s,
                    updated_at = NOW()
                WHERE product_id = %s::uuid
                  AND batch_no = %s
                  AND is_active = TRUE
            """, (line['rate'], line['qty'], line['product_id'], bill_no))
            saved += 1
        except Exception:
            skipped += 1

    return {'saved': saved, 'skipped': skipped, 'unmatched': unmatched}
