"""
modules/online_store/store_catalog.py
======================================
Product catalog, search, filters.
Reads products + inventory_stock + product_images.
Pricing priority: online_price > inventory_stock.mrp
"""
from __future__ import annotations
from typing import List, Dict, Optional


def _rq(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params) or []


# ── Categories ────────────────────────────────────────────────────────────

def get_online_categories() -> List[dict]:
    """Groups with at least one online-active product."""
    return _rq("""
        SELECT DISTINCT p.main_group AS name,
               COUNT(*) FILTER (WHERE p.online_active=TRUE) AS product_count
        FROM products p
        WHERE p.online_active = TRUE AND p.is_active = TRUE
        GROUP BY p.main_group
        ORDER BY product_count DESC
    """)


def get_featured_products(limit: int = 8) -> List[dict]:
    return _get_products(limit=limit, only_featured=True)


def get_products(
    category: str = None,
    brand: str = None,
    search: str = None,
    min_price: float = None,
    max_price: float = None,
    tags: List[str] = None,
    sort: str = "popular",    # popular | price_asc | price_desc | newest
    offset: int = 0,
    limit: int = 20,
) -> Dict:
    """Returns {items, total, has_more}."""
    return _get_products(
        category=category, brand=brand, search=search,
        min_price=min_price, max_price=max_price, tags=tags,
        sort=sort, offset=offset, limit=limit,
    )


def _get_products(
    category=None, brand=None, search=None,
    min_price=None, max_price=None, tags=None,
    sort="popular", offset=0, limit=20, only_featured=False,
) -> Dict:
    where = ["p.online_active = TRUE", "p.is_active = TRUE"]
    params: dict = {}

    if category:
        where.append("p.main_group = %(cat)s")
        params["cat"] = category
    if brand:
        where.append("p.brand = %(brand)s")
        params["brand"] = brand
    if search:
        where.append("(LOWER(p.product_name) LIKE %(s)s OR LOWER(p.brand) LIKE %(s)s)")
        params["s"] = f"%{search.lower()}%"
    if min_price is not None:
        where.append("COALESCE(p.online_price, i.mrp, 0) >= %(minp)s")
        params["minp"] = min_price
    if max_price is not None:
        where.append("COALESCE(p.online_price, i.mrp, 0) <= %(maxp)s")
        params["maxp"] = max_price
    if tags:
        where.append("p.online_tags && %(tags)s")
        params["tags"] = tags
    if only_featured:
        where.append("'BESTSELLER' = ANY(p.online_tags) OR 'FEATURED' = ANY(p.online_tags)")

    order_map = {
        "popular":    "p.online_sort ASC, p.product_name",
        "price_asc":  "COALESCE(p.online_price, i.mrp, 0) ASC",
        "price_desc": "COALESCE(p.online_price, i.mrp, 0) DESC",
        "newest":     "p.created_at DESC",
    }
    order_clause = order_map.get(sort, order_map["popular"])
    where_sql = " AND ".join(where)
    params.update({"off": offset, "lim": limit + 1})

    sql = f"""
        SELECT
            p.id::text, p.product_name, p.brand, p.main_group, p.category,
            p.material, p.coating_type, p.index_value, p.colour,
            p.wear_schedule, p.gender, p.unit, p.box_size, p.gst_percent,
            p.online_desc, p.online_badge, p.online_tags,
            COALESCE(p.online_price, i.mrp, 0)       AS price,
            COALESCE(i.mrp, p.online_price, 0)        AS mrp,
            COALESCE(i.selling_price, 0)              AS trade_price,
            COALESCE(i.stock_qty, 0)                  AS stock_qty,
            pi_img.image_url
        FROM products p
        LEFT JOIN (
            SELECT product_id,
                   MAX(mrp) AS mrp,
                   MAX(selling_price) AS selling_price,
                   SUM(quantity) AS stock_qty
            FROM inventory_stock
            WHERE is_active=TRUE
            GROUP BY product_id
        ) i ON i.product_id = p.id
        LEFT JOIN LATERAL (
            SELECT image_url
            FROM product_images
            WHERE product_id = p.id AND is_primary = TRUE
            LIMIT 1
        ) pi_img ON TRUE
        WHERE {where_sql}
        ORDER BY {order_clause}
        LIMIT %(lim)s OFFSET %(off)s
    """
    rows = _rq(sql, params)
    has_more = len(rows) > limit
    return {
        "items":    [dict(r) for r in rows[:limit]],
        "has_more": has_more,
        "offset":   offset + limit,
    }


def get_product_detail(product_id: str) -> Optional[dict]:
    """Full product detail + available powers + all images."""
    rows = _rq("""
        SELECT
            p.id::text, p.product_name, p.brand, p.main_group, p.category,
            p.material, p.coating_type, p.index_value, p.colour, p.online_desc,
            p.wear_schedule, p.gender, p.unit, p.box_size, p.gst_percent,
            p.online_badge, p.online_tags,
            COALESCE(p.online_price, 0)  AS online_price,
            COALESCE(MAX(i.mrp), 0)      AS mrp,
            COALESCE(SUM(i.quantity) FILTER (WHERE i.is_active=TRUE), 0) AS total_stock
        FROM products p
        LEFT JOIN inventory_stock i ON i.product_id=p.id AND i.is_active=TRUE
        WHERE p.id=%(pid)s::uuid AND p.online_active=TRUE
        GROUP BY p.id, p.product_name, p.brand, p.main_group, p.category,
                 p.material, p.coating_type, p.index_value, p.colour, p.online_desc,
                 p.wear_schedule, p.gender, p.unit, p.box_size, p.gst_percent,
                 p.online_badge, p.online_tags, p.online_price
    """, {"pid": product_id})
    if not rows:
        return None
    prod = dict(rows[0])

    # Images
    prod["images"] = _rq("""
        SELECT image_url, alt_text, is_primary
        FROM product_images
        WHERE product_id=%(pid)s::uuid
        ORDER BY sort_order, is_primary DESC
    """, {"pid": product_id})

    # Available powers (for contact lenses)
    prod["powers"] = _rq("""
        SELECT MIN(id::text) AS stock_id, sph, cyl, axis, add_power, eye_side,
               SUM(quantity) AS qty
        FROM inventory_stock
        WHERE product_id=%(pid)s::uuid AND is_active=TRUE AND quantity > 0
        GROUP BY sph, cyl, axis, add_power, eye_side
        ORDER BY eye_side, sph, cyl
    """, {"pid": product_id})

    return prod


def get_brands(category: str = None) -> List[str]:
    rows = _rq("""
        SELECT DISTINCT brand FROM products
        WHERE online_active=TRUE AND is_active=TRUE AND brand IS NOT NULL
        %(cat_filter)s
        ORDER BY brand
    """ % {"cat_filter": "AND main_group=%(cat)s" if category else ""},
    {"cat": category} if category else {})
    return [r["brand"] for r in rows if r.get("brand")]


def get_price_range(category: str = None) -> dict:
    params = {"cat": category} if category else {}
    cat_filter = "AND p.main_group=%(cat)s" if category else ""
    rows = _rq(f"""
        SELECT MIN(COALESCE(p.online_price, i.mrp, 0)) AS min_price,
               MAX(COALESCE(p.online_price, i.mrp, 0)) AS max_price
        FROM products p
        LEFT JOIN inventory_stock i ON i.product_id=p.id AND i.is_active=TRUE
        WHERE p.online_active=TRUE AND p.is_active=TRUE {cat_filter}
    """, params)
    if rows:
        return {"min": float(rows[0].get("min_price") or 0),
                "max": float(rows[0].get("max_price") or 9999)}
    return {"min": 0, "max": 9999}
