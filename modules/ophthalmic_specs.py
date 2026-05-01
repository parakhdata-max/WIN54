"""
ophthalmic_specs.py
───────────────────
Data layer for ophthalmic lens specs and pricing.
Pure DB functions — no Streamlit imports.

Deploy to: modules/ophthalmic_specs.py
"""
from __future__ import annotations
from typing import Optional


def _q(sql: str, params: dict = None) -> list:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        import logging; logging.warning(f"[ophthalmic_specs] {e}")
        return []


def get_all_specs_for_product(product_id: str) -> list[dict]:
    """Return ALL spec rows for a product (all index × coating × treatment)."""
    return _q("""
        SELECT
            s.id, s.index_value::text AS index_value,
            s.brand, s.coating, COALESCE(s.treatment,'Clear') AS treatment,
            s.lens_category, s.wlp_per_pair, s.srp_per_pair, s.purchase_rate
        FROM ophthalmic_lens_specs s
        WHERE s.product_id=%(pid)s::uuid
          AND s.is_active  = TRUE
          AND s.wlp_per_pair IS NOT NULL
        ORDER BY s.index_value, s.coating, s.treatment
    """, {"pid": product_id})


def get_index_options(product_id: str) -> list[str]:
    rows = _q("""
        SELECT DISTINCT index_value::text AS idx
        FROM ophthalmic_lens_specs
        WHERE product_id=%(pid)s::uuid AND is_active=TRUE AND wlp_per_pair IS NOT NULL
        ORDER BY index_value::text
    """, {"pid": product_id})
    return [r["idx"] for r in rows if r.get("idx")]


def get_coating_options(product_id: str, index_value: str) -> list[str]:
    rows = _q("""
        SELECT DISTINCT coating
        FROM ophthalmic_lens_specs
        WHERE product_id=%(pid)s::uuid AND index_value=%(idx)s::numeric
          AND is_active=TRUE AND wlp_per_pair IS NOT NULL
        ORDER BY coating
    """, {"pid": product_id, "idx": index_value})
    return [r["coating"] for r in rows if r.get("coating")]


def get_treatment_options(product_id: str, index_value: str, coating: str) -> list[str]:
    rows = _q("""
        SELECT DISTINCT COALESCE(treatment,'Clear') AS treatment
        FROM ophthalmic_lens_specs
        WHERE product_id=%(pid)s::uuid AND index_value=%(idx)s::numeric
          AND coating=%(coat)s AND is_active=TRUE
        ORDER BY 1
    """, {"pid": product_id, "idx": index_value, "coat": coating})
    return [r["treatment"] for r in rows] or ["Clear"]


def get_spec_price(product_id: str, index_value: str, coating: str,
                   treatment: str = "Clear", order_type: str = "RETAIL") -> dict:
    """
    Returns {wlp, srp, purchase, selling(per pair), found}.
    order_type RETAIL → selling=srp | WHOLESALE → selling=wlp
    """
    rows = _q("""
        SELECT wlp_per_pair, srp_per_pair, purchase_rate
        FROM ophthalmic_lens_specs
        WHERE product_id=%(pid)s::uuid AND index_value=%(idx)s::numeric
          AND coating=%(coat)s AND COALESCE(treatment,'Clear')=%(treat)s
          AND is_active=TRUE
        LIMIT 1
    """, {"pid": product_id, "idx": index_value,
           "coat": coating, "treat": treatment or "Clear"})
    if not rows:
        return {"wlp": 0, "srp": 0, "purchase": 0, "selling": 0, "found": False}
    r = rows[0]
    wlp = float(r.get("wlp_per_pair") or 0)
    srp = float(r.get("srp_per_pair")  or 0)
    cst = float(r.get("purchase_rate") or 0)
    return {"wlp": wlp, "srp": srp, "purchase": cst,
            "selling": srp if order_type == "RETAIL" else wlp, "found": True}


def check_stock(product_id: str, sph: float, cyl: float, axis: int,
                add_power: float, index_value: str, coating: str,
                eye_side: str = "PAIR") -> dict:
    """
    Check physical stock for power + index + coating.
    Returns {status: STOCK|RX_ORDER, qty_r, qty_l, batch_no}
    """
    rows = _q("""
        SELECT eye_side, SUM(quantity) AS qty, MIN(batch_no) AS batch_no
        FROM inventory_stock
        WHERE product_id=%(pid)s::uuid
          AND stock_type IN ('BATCH','POWER')
          AND ABS(COALESCE(sph,0)           - %(sph)s)  < 0.01
          AND ABS(COALESCE(cyl,0)           - %(cyl)s)  < 0.01
          AND (index_value IS NULL OR ABS(index_value - %(idx)s::numeric) < 0.01)
          AND LOWER(COALESCE(coating,''))   = LOWER(%(coat)s)
          AND COALESCE(is_active,TRUE) = TRUE
          AND quantity > 0
        GROUP BY eye_side
    """, {"pid": product_id, "sph": sph or 0, "cyl": cyl or 0,
           "idx": index_value or "0", "coat": coating or ""})

    qty_r = qty_l = 0; batch = ""
    for row in rows:
        side = str(row.get("eye_side") or "").upper()
        q    = int(row.get("qty") or 0)
        b    = row.get("batch_no") or ""
        if side in ("R","RIGHT"):       qty_r = q; batch = b
        elif side in ("L","LEFT"):      qty_l = q; batch = b
        elif side in ("PAIR","","MAIN"):qty_r = q; qty_l = q; batch = b

    if qty_r > 0 or qty_l > 0:
        return {"status": "STOCK", "qty_r": qty_r, "qty_l": qty_l,
                "batch_no": batch, "message": ""}
    return {"status": "RX_ORDER", "qty_r": 0, "qty_l": 0,
            "batch_no": "", "message": "📋 RX order basis"}

def get_addons_for_product(
    brand: str,
    lens_category: str = "ALL",
    product_id: str = None,
) -> list[dict]:
    """
    Return add-ons using 3-tier precedence (product > category > brand).
    Deduplication done in Python to avoid DISTINCT ON complexity.
    """
    rows = _q("""
        SELECT
            id::text, addon_name, addon_category, applies_to,
            wlp_addon, srp_addon, is_percentage, sort_order, notes,
            product_id::text AS product_id,
            CASE
                WHEN product_id IS NOT NULL THEN 1
                WHEN LOWER(applies_to) = LOWER(%(cat)s) THEN 2
                ELSE 3
            END AS prec
        FROM ophthalmic_addons
        WHERE brand = %(brand)s
          AND is_active = TRUE
          AND (
              product_id = %(pid)s::uuid
              OR (product_id IS NULL AND (
                  applies_to = 'ALL'
                  OR LOWER(applies_to) = LOWER(%(cat)s)
              ))
          )
        ORDER BY sort_order, addon_name
    """, {
        "brand": brand or "",
        "cat":   lens_category or "ALL",
        "pid":   product_id if product_id else "00000000-0000-0000-0000-000000000000",
    }) or []

    # Deduplicate by addon_name — keep highest precedence row
    seen: dict = {}
    for r in rows:
        name = r.get("addon_name", "")
        if not name: continue
        existing = seen.get(name)
        if existing is None or int(r.get("prec",9)) < int(existing.get("prec",9)):
            seen[name] = r
    return sorted(seen.values(), key=lambda x: (int(x.get("sort_order") or 99), x.get("addon_name","")))

