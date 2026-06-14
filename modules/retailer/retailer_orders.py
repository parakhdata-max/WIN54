"""
modules/retailer/retailer_orders.py  v4
========================================
Universal stock-led order engine.
- All categories handled via category_type routing
- CL: OR logic for R/L eye powers
- Transaction-safe stock locking (SELECT FOR UPDATE)
- Universal stock row + cart key = stock_id always
- Cached filter options (5 min TTL)
- Pagination support
"""
from __future__ import annotations
from typing import List, Dict, Optional
import uuid

RAZORPAY_KEY_ID     = ""
RAZORPAY_KEY_SECRET = ""


def _razorpay_keys() -> tuple[str, str]:
    """Read Razorpay keys from system_settings, falling back to legacy constants."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT key, value FROM system_settings WHERE key IN (%s, %s)",
            ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"),
        ) or []
        cfg = {str(r.get("key")): str(r.get("value") or "") for r in rows}
        return (
            cfg.get("RAZORPAY_KEY_ID") or RAZORPAY_KEY_ID,
            cfg.get("RAZORPAY_KEY_SECRET") or RAZORPAY_KEY_SECRET,
        )
    except Exception:
        return RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET


# ══════════════════════════════════════════════════════════════
# CATEGORY ROUTING
# ══════════════════════════════════════════════════════════════

def get_category_type(category: str) -> str:
    c = (category or "").lower()
    if any(x in c for x in ["contact","cl lens","soft lens"]): return "CONTACT_LENS"
    if any(x in c for x in ["frame","sunglass"]):              return "FRAME"
    if "solution" in c:                                         return "SOLUTION"
    if any(x in c for x in ["accessor","misc"]):               return "ACCESSORY"
    return "GENERAL"


# ══════════════════════════════════════════════════════════════
# PARTY / HOME — single call on page load
# ══════════════════════════════════════════════════════════════

def get_portal_home(party_id: str) -> dict:
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                COALESCE(pd.cash_disc_pct, cs.discount_pct, 20)  AS cash_disc_pct,
                COALESCE(pd.trade_disc_pct, 0)                    AS trade_disc_pct,
                COALESCE(ppt.credit_limit, 0)                     AS credit_limit,
                COALESCE(ppt.payment_mode, 'BOTH')                AS payment_mode,
                COALESCE(ppt.min_order_amt, 0)                    AS min_order_amt,
                COALESCE(ppt.is_blocked, FALSE)                   AS is_blocked,
                COALESCE(ppt.block_reason, '')                    AS block_reason,
                COALESCE((
                    SELECT SUM(net_amount) FROM retailer_orders
                    WHERE party_id = %(pid)s::uuid
                      AND payment_method = 'CREDIT' AND payment_status = 'PENDING'
                ), 0) AS outstanding
            FROM parties p
            LEFT JOIN party_discounts pd ON pd.party_id = p.id
            LEFT JOIN party_payment_terms ppt ON ppt.party_id = p.id
            LEFT JOIN LATERAL (
                SELECT discount_pct FROM cash_schemes
                WHERE is_active = TRUE
                  AND (valid_to IS NULL OR valid_to >= CURRENT_DATE)
                  AND (party_id IS NULL OR party_id = p.id)
                ORDER BY party_id DESC NULLS LAST LIMIT 1
            ) cs ON TRUE
            WHERE p.id = %(pid)s::uuid
        """, {"pid": party_id}) or [{}]
        r = rows[0]
        cl  = float(r.get("credit_limit") or 0)
        out = float(r.get("outstanding")  or 0)
        return {
            "cash_disc_pct":  float(r.get("cash_disc_pct")  or 20),
            "trade_disc_pct": float(r.get("trade_disc_pct") or 0),
            "credit_limit":   cl,
            "outstanding":    out,
            "avail_credit":   max(0, cl - out),
            "payment_mode":   str(r.get("payment_mode")  or "BOTH"),
            "min_order_amt":  float(r.get("min_order_amt") or 0),
            "is_blocked":     bool(r.get("is_blocked")),
            "block_reason":   str(r.get("block_reason") or ""),
        }
    except Exception:
        return {"cash_disc_pct":20,"trade_disc_pct":0,"credit_limit":0,
                "outstanding":0,"avail_credit":0,"payment_mode":"BOTH",
                "min_order_amt":0,"is_blocked":False,"block_reason":""}


def get_payment_methods(party_home: dict) -> List[dict]:
    mode     = party_home.get("payment_mode","BOTH")
    cash_d   = party_home.get("cash_disc_pct", 20)
    trade_d  = party_home.get("trade_disc_pct", 0)
    avail_cr = party_home.get("avail_credit", 0)
    methods  = []
    if mode in ("BOTH","CASH_ONLY"):
        methods.append({"method":"CASH","label":"💵 Cash/UPI",
                         "extra":f"{cash_d:.0f}% OFF","disc":cash_d})
    if mode in ("BOTH","CREDIT_ONLY"):
        methods.append({"method":"CREDIT","label":"📒 Credit",
                         "extra":f"₹{avail_cr:,.0f} available",
                         "disc":trade_d})
    return methods or [{"method":"CASH","label":"💵 Cash/UPI",
                         "extra":f"{cash_d:.0f}% OFF","disc":cash_d}]


# ══════════════════════════════════════════════════════════════
# CATEGORIES
# ══════════════════════════════════════════════════════════════

def get_categories() -> List[str]:
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT DISTINCT p.main_group
            FROM products p JOIN inventory_stock s ON s.product_id = p.id
            LEFT JOIN product_visibility pv ON pv.product_id = p.id
            WHERE COALESCE(p.is_active,TRUE)=TRUE AND COALESCE(s.is_active,TRUE)=TRUE
              AND COALESCE(s.quantity,0)>0 AND COALESCE(p.main_group,'')!=''
              AND LOWER(COALESCE(p.main_group,'')) NOT IN ('ophthalmic lenses','ophthalmic lens')
              AND COALESCE(pv.show_online, TRUE)=TRUE
            ORDER BY p.main_group
        """) or []
        return [r["main_group"] for r in rows]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════
# DYNAMIC FILTERS — cached per category
# ══════════════════════════════════════════════════════════════

def get_dynamic_filters(category: str) -> dict:
    """
    Returns dict of available filter values for the category.
    Cached 5 min via st.cache_data at call site.
    """
    if not category:
        return {}
    try:
        from modules.sql_adapter import run_query
        cat_type = get_category_type(category)
        mg = f"%{category.lower()}%"

        _base = """
            FROM inventory_stock s
            JOIN products p ON p.id = s.product_id
            LEFT JOIN product_visibility pv ON pv.product_id = p.id
            WHERE LOWER(COALESCE(p.main_group,'')) LIKE %(mg)s
              AND COALESCE(p.is_active,TRUE)=TRUE
              AND COALESCE(s.is_active,TRUE)=TRUE
              AND COALESCE(s.quantity,0)>0
              AND COALESCE(pv.show_online, TRUE)=TRUE
        """

        def _pq(col):
            rows = run_query(
                f"SELECT DISTINCT p.{col} AS v {_base} "
                f"AND p.{col} IS NOT NULL AND p.{col}::text!='' ORDER BY p.{col}",
                {"mg": mg}) or []
            return [r["v"] for r in rows]

        def _sq(col):
            rows = run_query(
                f"SELECT DISTINCT s.{col} AS v {_base} "
                f"AND s.{col} IS NOT NULL ORDER BY s.{col}",
                {"mg": mg}) or []
            return [r["v"] for r in rows]

        result = {"brands": _pq("brand")}

        if cat_type == "CONTACT_LENS":
            result.update({
                "products":  _pq("product_name"),
                "sph":       _sq("sph"),
                "cyl":       _sq("cyl"),
                "axis":      _sq("axis"),
                "add_power": _sq("add_power"),
            })
        elif cat_type == "FRAME":
            result.update({
                "colours":   _sq("colour"),
                "shapes":    _sq("shape"),
                "materials": _sq("base_material"),
                "finishes":  _sq("finish"),
                "sizes_a":   _sq("size_a"),
            })
        elif cat_type in ("SOLUTION","ACCESSORY","GENERAL"):
            result.update({"products": _pq("product_name")})

        return result
    except Exception as e:
        import logging; logging.warning(f"[get_dynamic_filters] {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# STOCK SEARCH — universal dispatcher
# ══════════════════════════════════════════════════════════════

_STOCK_SELECT = """
    SELECT
        s.id                                        AS stock_id,
        p.id                                        AS product_id,
        s.batch_no                                  AS sku_code,
        p.product_name,
        COALESCE(p.brand,'')                        AS brand,
        COALESCE(p.main_group,'')                   AS main_group,
        COALESCE(p.category,'')                     AS category,
        COALESCE(p.unit,'PCS')                      AS unit,
        COALESCE(p.gst_percent,0)                   AS gst_percent,
        COALESCE(p.water_content::text,'')          AS water_content,
        COALESCE(p.base_curve::text,'')             AS base_curve,
        COALESCE(p.diameter::text,'')               AS diameter,
        COALESCE(p.replacement_schedule,'')         AS replacement_schedule,
        COALESCE(p.uv_blocking,FALSE)               AS uv_blocking,
        COALESCE(p.coating,'')                      AS coating,
        s.sph, s.cyl, s.axis, s.add_power,
        COALESCE(s.eye_side,'')                     AS eye_side,
        COALESCE(s.colour,'')                       AS colour,
        COALESCE(s.colour_mix,'')                   AS colour_mix,
        COALESCE(s.shape,'')                        AS shape,
        COALESCE(s.base_material,'')                AS frame_material,
        COALESCE(s.finish,'')                       AS finish,
        s.size_a, s.size_b, s.dbl,
        COALESCE(s.location,'')                     AS location,
        COALESCE(s.expiry_date::text,'')            AS expiry_date,
        COALESCE(pv.online_price,
                 s.selling_price, s.mrp, 0)         AS trade_price,
        COALESCE(s.mrp, s.selling_price, 0)         AS mrp,
        COALESCE(s.quantity, 0)                     AS stock_qty,
        COALESCE(pv.min_order_qty, 1)               AS min_order_qty
    FROM inventory_stock s
    JOIN products p ON p.id = s.product_id
    LEFT JOIN product_visibility pv ON pv.product_id = p.id
"""

def get_matching_stock(category: str, filters: dict,
                       limit: int = 80, offset: int = 0) -> List[dict]:
    cat_type = get_category_type(category)
    if cat_type == "CONTACT_LENS":  return _cl_stock(category, filters, limit, offset)
    if cat_type == "FRAME":         return _frame_stock(category, filters, limit, offset)
    return _general_stock(category, filters, limit, offset)


def _base_where(category: str) -> tuple:
    """Common WHERE conditions."""
    return (
        [
            "LOWER(COALESCE(p.main_group,'')) LIKE %(mg)s",
            "COALESCE(p.is_active,TRUE)=TRUE",
            "COALESCE(s.is_active,TRUE)=TRUE",
            "COALESCE(s.quantity,0)>0",
            "COALESCE(pv.show_online,TRUE)=TRUE",
        ],
        {"mg": f"%{category.lower()}%"}
    )


def _cl_stock(category: str, filters: dict, limit: int, offset: int) -> List[dict]:
    """
    CL stock search.
    R and L SPH use OR logic — results tagged with matched_eye.
    Run separately for R and L, merge, tag.
    """
    try:
        from modules.sql_adapter import run_query
        try:
            from modules.contact_lens_resolver import (
                line_for_eye as _cl_line_for_eye,
                resolve_contact_lens_order as _cl_resolve_order,
            )
        except Exception:
            _cl_line_for_eye = None
            _cl_resolve_order = None
        results = []

        eyes = []
        if filters.get("r_sph") is not None:
            eyes.append({"label":"R", "sph": filters["r_sph"],
                         "cyl": filters.get("r_cyl"),
                         "axis": filters.get("r_axis"),
                         "add": filters.get("r_add")})
        if filters.get("l_sph") is not None:
            eyes.append({"label":"L", "sph": filters["l_sph"],
                         "cyl": filters.get("l_cyl"),
                         "axis": filters.get("l_axis"),
                         "add": filters.get("l_add")})

        if not eyes:
            # No powers — just filter by brand/product
            _where, _params = _base_where(category)
            if filters.get("brand"):
                _where.append("LOWER(p.brand)=LOWER(%(brand)s)")
                _params["brand"] = filters["brand"]
            if filters.get("product"):
                _where.append("LOWER(p.product_name)=LOWER(%(product)s)")
                _params["product"] = filters["product"]
            rows = run_query(
                f"{_STOCK_SELECT} WHERE {' AND '.join(_where)} "
                f"ORDER BY p.brand,p.product_name,s.sph NULLS LAST "
                f"LIMIT %(lim)s OFFSET %(off)s",
                {**_params,"lim":limit,"off":offset}
            ) or []
            for r in rows: r["matched_eye"] = "—"
            return rows

        # Run per eye — OR logic
        seen = set()
        for eye in eyes:
            _where, _params = _base_where(category)
            _resolved_pid = ""
            if filters.get("product") and _cl_resolve_order and _cl_line_for_eye:
                try:
                    _power = {
                        "sph": eye.get("sph"),
                        "cyl": eye.get("cyl"),
                        "axis": eye.get("axis"),
                        "add": eye.get("add"),
                    }
                    _res = _cl_resolve_order(
                        selected_product=str(filters.get("product") or ""),
                        right_power=_power if eye["label"] == "R" else None,
                        left_power=_power if eye["label"] == "L" else None,
                        brand=filters.get("brand"),
                    )
                    _line = _cl_line_for_eye(_res, eye["label"])
                    _resolved_pid = str(_line.get("product_id") or "")
                except Exception:
                    _resolved_pid = ""

            _params["sph_val"] = float(eye["sph"])
            _where.append("ROUND(s.sph::numeric,2) = ROUND(%(sph_val)s::numeric,2)")

            if eye["cyl"] is not None:
                _params["cyl_val"] = float(eye["cyl"])
                _where.append("ROUND(COALESCE(s.cyl,0)::numeric,2) = ROUND(%(cyl_val)s::numeric,2)")
            else:
                _where.append("ROUND(COALESCE(s.cyl,0)::numeric,2) = 0")

            if eye["axis"] is not None and eye["cyl"] is not None:
                _params.update({"ax_lo": int(eye["axis"])-5, "ax_hi": int(eye["axis"])+5})
                _where.append("COALESCE(s.axis,0) BETWEEN %(ax_lo)s AND %(ax_hi)s")

            if eye["add"] is not None:
                _params["add_val"] = float(eye["add"])
                _where.append("ROUND(COALESCE(s.add_power,0)::numeric,2) = ROUND(%(add_val)s::numeric,2)")

            if filters.get("brand"):
                _where.append("LOWER(p.brand)=LOWER(%(brand)s)")
                _params["brand"] = filters["brand"]
            if _resolved_pid:
                _where.append("p.id = %(resolved_pid)s::uuid")
                _params["resolved_pid"] = _resolved_pid
            elif filters.get("product"):
                _where.append("LOWER(p.product_name)=LOWER(%(product)s)")
                _params["product"] = filters["product"]

            rows = run_query(
                f"{_STOCK_SELECT} WHERE {' AND '.join(_where)} "
                f"ORDER BY p.brand,p.product_name,s.quantity DESC "
                f"LIMIT %(lim)s OFFSET %(off)s",
                {**_params,"lim":limit,"off":offset}
            ) or []

            for r in rows:
                sid = str(r["stock_id"])
                key = f"{sid}_{eye['label']}"
                if key not in seen:
                    seen.add(key)
                    r["matched_eye"] = eye["label"]
                    results.append(r)

        return results
    except Exception as e:
        import logging; logging.warning(f"[_cl_stock] {e}"); return []


def _frame_stock(category: str, filters: dict, limit: int, offset: int) -> List[dict]:
    try:
        from modules.sql_adapter import run_query
        _where, _params = _base_where(category)

        def _fadd(col, key, table="s"):
            val = filters.get(key,"")
            if val:
                _where.append(f"LOWER(COALESCE({table}.{col},''))=LOWER(%({key})s)")
                _params[key] = val

        _fadd("brand",        "brand",    "p")
        _fadd("colour",       "colour")
        _fadd("shape",        "shape")
        _fadd("base_material","material")
        _fadd("finish",       "finish")
        if filters.get("size_a"):
            _where.append("s.size_a = %(size_a)s")
            _params["size_a"] = float(filters["size_a"])

        rows = run_query(
            f"{_STOCK_SELECT} WHERE {' AND '.join(_where)} "
            f"ORDER BY p.brand,p.product_name,s.colour NULLS LAST "
            f"LIMIT %(lim)s OFFSET %(off)s",
            {**_params,"lim":limit,"off":offset}
        ) or []
        for r in rows: r["matched_eye"] = "—"
        return rows
    except Exception as e:
        import logging; logging.warning(f"[_frame_stock] {e}"); return []


def _general_stock(category: str, filters: dict, limit: int, offset: int) -> List[dict]:
    try:
        from modules.sql_adapter import run_query
        _where, _params = _base_where(category)
        if filters.get("brand"):
            _where.append("LOWER(p.brand)=LOWER(%(brand)s)")
            _params["brand"] = filters["brand"]
        if filters.get("product"):
            _where.append("LOWER(p.product_name)=LOWER(%(product)s)")
            _params["product"] = filters["product"]

        rows = run_query(
            f"{_STOCK_SELECT} WHERE {' AND '.join(_where)} "
            f"ORDER BY p.brand,p.product_name "
            f"LIMIT %(lim)s OFFSET %(off)s",
            {**_params,"lim":limit,"off":offset}
        ) or []
        for r in rows: r["matched_eye"] = "—"
        return rows
    except Exception as e:
        import logging; logging.warning(f"[_general_stock] {e}"); return []


# ══════════════════════════════════════════════════════════════
# SKU SCAN — universal, POS-like
# ══════════════════════════════════════════════════════════════

def lookup_by_sku(barcode: str) -> Optional[dict]:
    if not barcode: return None
    try:
        from modules.sql_adapter import run_query
        sku = barcode.strip().upper()
        rows = run_query(
            f"{_STOCK_SELECT} "
            "WHERE (UPPER(TRIM(s.batch_no))=%(sku)s "
            "       OR UPPER(TRIM(COALESCE(s.barcode,'')))=%(sku)s "
            "       OR UPPER(TRIM(COALESCE(p.barcode,'')))=%(sku)s) "
            "AND COALESCE(s.is_active,TRUE)=TRUE "
            "AND COALESCE(s.quantity,0)>0 "
            "ORDER BY s.expiry_date ASC NULLS LAST LIMIT 1",
            {"sku": sku}
        ) or []
        if rows:
            rows[0]["matched_eye"] = "—"
        return rows[0] if rows else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# REORDER / FAVOURITES
# ══════════════════════════════════════════════════════════════

def get_reorder_items(party_id: str, limit: int = 12) -> List[dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query("""
            SELECT l.product_name, l.brand, l.sku_code,
                   l.stock_id::text AS stock_id,
                   l.sph, l.cyl, l.axis, l.add_power, l.eye_side,
                   l.colour, l.shape, l.size_a, l.size_b,
                   l.unit_price AS trade_price,
                   l.qty AS last_qty,
                   l.main_group,
                   COUNT(*) AS times_ordered,
                   MAX(o.punched_at) AS last_ordered
            FROM retailer_order_lines l
            JOIN retailer_orders o ON o.id = l.order_id
            WHERE o.party_id = %s AND o.status != 'CANCELLED'
            GROUP BY l.product_name,l.brand,l.sku_code,l.stock_id,
                     l.sph,l.cyl,l.axis,l.add_power,l.eye_side,
                     l.colour,l.shape,l.size_a,l.size_b,
                     l.unit_price,l.qty,l.main_group
            ORDER BY times_ordered DESC, last_ordered DESC
            LIMIT %s
        """, (party_id, limit)) or []
    except Exception:
        return []


def validate_reorder_item(stock_id: str) -> Optional[dict]:
    """Check if a previously ordered stock_id still exists and has qty."""
    if not stock_id: return None
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            f"{_STOCK_SELECT} WHERE s.id = %(sid)s::uuid "
            "AND COALESCE(s.is_active,TRUE)=TRUE",
            {"sid": stock_id}
        ) or []
        if rows: rows[0]["matched_eye"] = "—"
        return rows[0] if rows else None
    except Exception:
        return None


def get_favourites(party_id: str) -> List[dict]:
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            f"{_STOCK_SELECT} "
            "JOIN party_favourites pf ON pf.stock_id = s.id "
            "WHERE pf.party_id = %(pid)s::uuid "
            "AND COALESCE(s.is_active,TRUE)=TRUE "
            "ORDER BY pf.added_at DESC",
            {"pid": party_id}
        ) or []
        for r in rows: r["matched_eye"] = "—"
        return rows
    except Exception:
        return []


def toggle_favourite(party_id: str, stock_id: str, product_id: str) -> bool:
    try:
        from modules.sql_adapter import run_query, run_write
        ex = run_query("SELECT id FROM party_favourites WHERE party_id=%s AND stock_id=%s::uuid",
                       (party_id, stock_id)) or []
        if ex:
            run_write("DELETE FROM party_favourites WHERE party_id=%s AND stock_id=%s::uuid",
                      (party_id, stock_id)); return False
        run_write("INSERT INTO party_favourites(id,party_id,stock_id,product_id) VALUES(%s,%s,%s::uuid,%s::uuid)",
                  (str(uuid.uuid4()), party_id, stock_id, product_id)); return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# UNIVERSAL HELPERS
# ══════════════════════════════════════════════════════════════

def build_item_detail(item: dict) -> str:
    """Universal detail string for any stock row."""
    parts = []
    if item.get("sph") is not None:
        parts.append(f"SPH {float(item['sph']):+.2f}")
    if item.get("cyl") is not None and float(item.get("cyl") or 0) != 0:
        parts.append(f"CYL {float(item['cyl']):+.2f}")
    if item.get("axis") and item.get("cyl") and float(item.get("cyl",0)) != 0:
        parts.append(f"AX {int(item['axis'])}°")
    if item.get("add_power") and float(item.get("add_power",0)) != 0:
        parts.append(f"ADD {float(item['add_power']):+.2f}")
    if item.get("eye_side"):
        parts.append(f"{'👁R' if 'R' in str(item['eye_side']).upper() else '👁L'}")
    if item.get("matched_eye") and item.get("matched_eye") not in ("—",""):
        eye = item["matched_eye"]
        label = "👁R" if eye == "R" else "👁L"
        if label not in parts: parts.append(label)
    if item.get("colour"):        parts.append(item["colour"])
    if item.get("shape"):         parts.append(item["shape"])
    if item.get("frame_material"): parts.append(item["frame_material"])
    if item.get("size_a"):
        sz = f"A:{item['size_a']}"
        if item.get("size_b"): sz += f" B:{item['size_b']}"
        if item.get("dbl"):    sz += f" DBL:{item['dbl']}"
        parts.append(sz)
    if item.get("expiry_date"):
        parts.append(f"Exp:{str(item['expiry_date'])[:7]}")
    if item.get("location"):
        parts.append(f"📍{item['location']}")
    return "  |  ".join(parts)


# ══════════════════════════════════════════════════════════════
# CART — stock_id key, always
# ══════════════════════════════════════════════════════════════

def cart_add(item: dict, qty: int = 1) -> None:
    import streamlit as st
    item = _resolve_cart_contact_item(dict(item or {}))
    cart = st.session_state.get("retailer_cart", {})
    key  = str(item.get("stock_id") or item.get("product_id",""))
    if key in cart: cart[key]["qty"] += qty
    else:           cart[key] = {**item, "qty": qty, "cart_key": key}
    st.session_state["retailer_cart"] = cart

def cart_update_qty(key: str, qty: int) -> None:
    import streamlit as st
    cart = st.session_state.get("retailer_cart", {})
    if qty <= 0: cart.pop(key, None)
    elif key in cart: cart[key]["qty"] = qty
    st.session_state["retailer_cart"] = cart

def cart_remove(key: str) -> None:
    import streamlit as st
    cart = st.session_state.get("retailer_cart", {})
    cart.pop(key, None)
    st.session_state["retailer_cart"] = cart

def cart_clear() -> None:
    import streamlit as st
    st.session_state["retailer_cart"] = {}

def cart_get() -> dict:
    import streamlit as st
    return st.session_state.get("retailer_cart", {})


def _resolve_cart_contact_item(item: dict) -> dict:
    """Final B2B cart guard: selected CL product is a hint, power decides product."""
    try:
        if get_category_type(item.get("main_group") or item.get("category") or "") != "CONTACT_LENS":
            return item
        from modules.contact_lens_resolver import line_for_eye, resolve_contact_lens_order
        eye = str(item.get("eye_side") or item.get("matched_eye") or "R").upper()[:1]
        power = {
            "sph": item.get("sph"),
            "cyl": item.get("cyl"),
            "axis": item.get("axis"),
            "add": item.get("add_power"),
        }
        res = resolve_contact_lens_order(
            selected_product=str(item.get("product_name") or ""),
            right_power=power if eye != "L" else None,
            left_power=power if eye == "L" else None,
            brand=item.get("brand"),
        )
        line = line_for_eye(res, "L" if eye == "L" else "R")
        row = line.get("product_row") or {}
        if not row.get("product_id"):
            return item
        out = dict(item)
        out["product_id"] = row.get("product_id")
        out["product_name"] = row.get("product_name") or out.get("product_name")
        out["brand"] = row.get("brand") or out.get("brand")
        out["main_group"] = row.get("main_group") or out.get("main_group")
        out["category"] = row.get("category") or out.get("category")
        out["unit"] = row.get("unit") or out.get("unit")
        if line.get("stock_id"):
            out["stock_id"] = line.get("stock_id")
        out["cl_resolver_route"] = line.get("route")
        out["cl_lens_type"] = line.get("lens_type")
        return out
    except Exception:
        return item

def cart_totals(cart: dict, payment_method: str, party_home: dict) -> dict:
    subtotal = sum(float(i.get("trade_price",0))*i["qty"] for i in cart.values())
    if payment_method in ("CASH","UPI"):
        disc_pct    = float(party_home.get("cash_disc_pct", 20))
        scheme_name = f"Cash/UPI {disc_pct:.0f}% discount"
    else:
        disc_pct    = float(party_home.get("trade_disc_pct", 0))
        scheme_name = f"Trade {disc_pct:.0f}%" if disc_pct else "Credit"
    disc_amt = round(subtotal * disc_pct / 100, 2)
    return {"subtotal": subtotal, "disc_pct": disc_pct, "disc_amount": disc_amt,
            "net": round(subtotal - disc_amt, 2), "scheme_name": scheme_name,
            "item_count": sum(i["qty"] for i in cart.values())}


# ══════════════════════════════════════════════════════════════
# ORDER — full transaction, SELECT FOR UPDATE, payment expiry
# ══════════════════════════════════════════════════════════════

def create_order(party: dict, cart: dict, payment_method: str,
                 party_home: dict, notes: str = "") -> dict:
    """
    Atomic order creation:
    1. Pre-checks (blocked / credit / min order)
    2. Open psycopg2 transaction
    3. SELECT FOR UPDATE NOWAIT — locks stock rows, blocks concurrent orders
    4. Validate qty inside lock
    5. Insert order (PAYMENT_PENDING for CASH, SUBMITTED for CREDIT)
    6. Insert lines
    7. Deduct stock (WHERE quantity >= qty — rowcount=0 → abort)
    8. COMMIT or full ROLLBACK on any failure

    CASH payment expiry = NOW() + 30 min.
    Unpaid stock released by release_expired_payment_orders() DB function.
    """
    if not cart:
        return {"success": False, "message": "Cart is empty"}
    cart = {
        str(k): _resolve_cart_contact_item(v)
        for k, v in (cart or {}).items()
    }

    totals   = cart_totals(cart, payment_method, party_home)
    is_cash  = payment_method in ("CASH", "UPI")
    order_id = str(uuid.uuid4())

    # Pre-checks — no DB writes yet
    if party_home.get("is_blocked"):
        return {"success": False,
                "message": f"Account blocked: {party_home.get('block_reason','')}"}
    min_amt = party_home.get("min_order_amt", 0)
    if min_amt and totals["subtotal"] < min_amt:
        return {"success": False, "message": f"Minimum order ₹{min_amt:,.0f}"}
    if payment_method == "CREDIT":
        avail = party_home.get("avail_credit", 0)
        if party_home.get("credit_limit", 0) > 0 and totals["net"] > avail:
            return {"success": False,
                    "message": f"Exceeds available credit ₹{avail:,.0f}"}

    try:
        from modules.sql_adapter import run_query
        order_no_row = run_query("SELECT generate_order_no() AS no") or []
        order_no = (order_no_row[0]["no"] if order_no_row
                    else f"ORD-{uuid.uuid4().hex[:8].upper()}")

        try:
            from modules.sql_adapter import get_connection
            conn = get_connection()
            # Retry up to 2 times on lock contention (NOWAIT failure)
            for attempt in range(3):
                result = _create_order_tx(conn, order_id, order_no, is_cash,
                                          party, cart, payment_method, totals, notes)
                if result.get("success") or result.get("_lock_contention") is not True:
                    break
                import time; time.sleep(0.3 * (attempt + 1))  # 0.3s, 0.6s backoff
                # Fresh connection for retry
                try: conn = get_connection()
                except Exception: break
            result.pop("_lock_contention", None)
            return result
        except Exception:
            import logging
            logging.warning("[retailer_orders] Raw connection unavailable — using fallback (no FOR UPDATE)")
            return _create_order_fallback(order_id, order_no, is_cash,
                                          party, cart, payment_method, totals, notes)
    except Exception as e:
        return {"success": False, "message": f"Order failed: {e}"}


def _mirror_retailer_order_to_backoffice(retailer_order_id: str) -> None:
    """
    Retailer portal keeps its own order history, but Backoffice/Production read
    the main orders tables. Mirror once so portal stock orders enter the normal
    wholesale review and routing flow.
    """
    try:
        from modules.sql_adapter import run_write
        run_write("""
            INSERT INTO orders (
                id, order_no, order_type, order_source, status,
                party_name, patient_name, patient_mobile, customer_order_no,
                total_items, total_value, party_id, payment_mode, created_at
            )
            SELECT gen_random_uuid(), ro.order_no, 'WHOLESALE', 'RETAILER_PORTAL',
                   'UNDER_REVIEW', ro.party_name, ro.party_name, ro.mobile,
                   ro.order_no,
                   (SELECT COUNT(*) FROM retailer_order_lines rl WHERE rl.order_id = ro.id),
                   COALESCE(ro.net_amount, 0), ro.party_id,
                   COALESCE(ro.payment_method, 'CREDIT'), COALESCE(ro.submitted_at, ro.punched_at, NOW())
            FROM retailer_orders ro
            WHERE ro.id = %(rid)s::uuid
              AND NOT EXISTS (
                  SELECT 1 FROM orders o
                  WHERE o.customer_order_no = ro.order_no
                    AND COALESCE(o.order_source,'') = 'RETAILER_PORTAL'
              )
        """, {"rid": retailer_order_id})

        run_write("""
            INSERT INTO order_lines (
                id, order_id, product_id,
                sph, cyl, axis, add_power, eye_side,
                quantity, unit_price, total_price,
                gst_percent, gst_amount,
                discount_percent, discount_amount,
                applied_rule_ids,
                lens_params, boxing_params, suggested_allocation,
                is_service_line, allocated_qty, ready_qty, status
            )
            SELECT gen_random_uuid(), o.id, rl.product_id,
                   rl.sph, rl.cyl, rl.axis, rl.add_power, COALESCE(rl.eye_side, 'OTHER'),
                   COALESCE(rl.qty, 1), COALESCE(rl.unit_price, 0), COALESCE(rl.line_total, 0),
                   0, 0,
                   COALESCE(rl.discount_pct, 0),
                   ROUND((COALESCE(rl.unit_price,0) * COALESCE(rl.qty,1) * COALESCE(rl.discount_pct,0) / 100.0)::numeric, 2),
                   '[]'::jsonb,
                   jsonb_build_object(
                       'order_source', 'RETAILER_PORTAL',
                       'retailer_order_id', ro.id::text,
                       'retailer_line_id', rl.id::text,
                       'stock_id', COALESCE(rl.stock_id::text, ''),
                       'sku_code', COALESCE(rl.sku_code, ''),
                       'batch_no', COALESCE(NULLIF(rl.batch_no,''), NULLIF(rl.sku_code,''), ''),
                       'manufacturing_route', CASE WHEN rl.stock_id IS NOT NULL THEN 'STOCK' ELSE 'VENDOR' END,
                       'batch_allocation', CASE WHEN rl.stock_id IS NOT NULL THEN
                           jsonb_build_array(jsonb_build_object(
                               'stock_id', rl.stock_id::text,
                               'batch_no', COALESCE(NULLIF(rl.batch_no,''), NULLIF(rl.sku_code,''), ''),
                               'allocated_qty', COALESCE(rl.qty, 1),
                               'qty', COALESCE(rl.qty, 1),
                               'unit', COALESCE(rl.unit, 'PCS')
                           ))
                       ELSE '[]'::jsonb END,
                       'colour_mix', COALESCE(rl.colour, ''),
                       'size_info', COALESCE(rl.size_info, '')
                   ),
                   '{}'::jsonb,
                   CASE WHEN rl.stock_id IS NOT NULL THEN
                       jsonb_build_array(jsonb_build_object(
                           'stock_id', rl.stock_id::text,
                           'batch_no', COALESCE(NULLIF(rl.batch_no,''), NULLIF(rl.sku_code,''), ''),
                           'allocated_qty', COALESCE(rl.qty, 1)
                       ))
                   ELSE '[]'::jsonb END,
                   FALSE,
                   CASE WHEN rl.stock_id IS NOT NULL THEN COALESCE(rl.qty, 1) ELSE 0 END,
                   CASE WHEN rl.stock_id IS NOT NULL THEN COALESCE(rl.qty, 1) ELSE 0 END,
                   'PENDING'
            FROM retailer_order_lines rl
            JOIN retailer_orders ro ON ro.id = rl.order_id
            JOIN orders o ON o.customer_order_no = ro.order_no
                         AND COALESCE(o.order_source,'') = 'RETAILER_PORTAL'
            WHERE ro.id = %(rid)s::uuid
              AND NOT EXISTS (
                  SELECT 1 FROM order_lines ol
                  WHERE ol.order_id = o.id
                    AND ol.lens_params->>'retailer_line_id' = rl.id::text
              )
        """, {"rid": retailer_order_id})
    except Exception as e:
        import logging
        logging.warning(f"[retailer_orders] backoffice mirror failed: {e}")


def _create_order_tx(conn, order_id, order_no, is_cash,
                     party, cart, payment_method, totals, notes):
    """Full psycopg2 transaction with SELECT FOR UPDATE."""
    from datetime import datetime, timedelta
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")

        # Lock all stock rows atomically
        stock_ids = [str(i.get("stock_id","")) for i in cart.values() if i.get("stock_id")]
        locked = {}
        if stock_ids:
            ph = ",".join(["%s"]*len(stock_ids))
            cur.execute(
                f"SELECT id, COALESCE(quantity,0) FROM inventory_stock "
                f"WHERE id IN ({ph}) FOR UPDATE NOWAIT",
                stock_ids
            )
            locked = {str(r[0]): int(r[1]) for r in cur.fetchall()}

            errors = []
            for key, item in cart.items():
                sid = str(item.get("stock_id",""))
                if not sid: continue
                avail = locked.get(sid, 0)
                if avail < item["qty"]:
                    errors.append(
                        f"'{item['product_name']}' — need {item['qty']}, only {avail} left")
            if errors:
                cur.execute("ROLLBACK"); conn.close()
                return {"success": False,
                        "message": "Stock issue:\n• " + "\n• ".join(errors)}

        expires_at = (datetime.utcnow() + timedelta(minutes=30)) if is_cash else None
        status     = "PAYMENT_PENDING" if is_cash else "SUBMITTED"

        cur.execute("""
            INSERT INTO retailer_orders
            (id,order_no,party_id,party_name,mobile,status,payment_method,payment_status,
             subtotal,discount_pct,discount_amount,net_amount,scheme_applied,notes,
             payment_expires_at,punched_at,submitted_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),NOW())
        """, (order_id,order_no,str(party.get("id")),party.get("party_name"),
               party.get("mobile"),status,payment_method,
               totals["subtotal"],totals["disc_pct"],totals["disc_amount"],
               totals["net"],totals["scheme_name"],notes,expires_at))

        for key, item in cart.items():
            trade=float(item.get("trade_price",0)); qty=item["qty"]
            line_tot=round(trade*qty*(1-totals["disc_pct"]/100),2)
            sid=str(item.get("stock_id") or "")
            sph=item.get("sph"); cyl=item.get("cyl"); axis=item.get("axis")
            add_p=item.get("add_power")
            eye=str(item.get("eye_side") or item.get("matched_eye") or "")
            expiry=str(item.get("expiry_date",""))[:10] or None

            cur.execute("""
                INSERT INTO retailer_order_lines
                (id,order_id,product_id,stock_id,sku_code,batch_no,
                 product_name,brand,main_group,category,unit,
                 sph,cyl,axis,add_power,eye_side,colour,shape,frame_material,
                 size_a,size_b,dbl,expiry_date,location,
                 qty,unit_price,discount_pct,line_total)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (str(uuid.uuid4()),order_id,str(item.get("product_id","")),sid or None,
                  str(item.get("sku_code","") or ""),str(item.get("sku_code","") or ""),
                  item.get("product_name",""),item.get("brand",""),
                  item.get("main_group",""),item.get("category",""),item.get("unit","PCS"),
                  float(sph) if sph is not None else None,
                  float(cyl) if cyl is not None else None,
                  int(axis)  if axis is not None else None,
                  float(add_p) if add_p is not None else None,
                  eye or None,
                  item.get("colour") or None,item.get("shape") or None,
                  item.get("frame_material") or None,
                  float(item["size_a"]) if item.get("size_a") else None,
                  float(item["size_b"]) if item.get("size_b") else None,
                  float(item["dbl"])    if item.get("dbl")    else None,
                  expiry,item.get("location") or None,
                  qty,trade,totals["disc_pct"],line_tot))

            if sid:
                cur.execute(
                    "UPDATE inventory_stock "
                    "SET quantity=quantity-%s, updated_at=NOW() "
                    "WHERE id=%s AND COALESCE(quantity,0)>=%s",
                    (qty,sid,qty))
                if cur.rowcount == 0:
                    cur.execute("ROLLBACK"); conn.close()
                    return {"success":False,
                            "message":(f"⚠️ '{item['product_name']}' "
                                       "just went out of stock. Retry.")}

        # ── Insert service lines from cart ──────────────────────────────────────
        import uuid as _svc_uuid, json as _svc_json
        for _ck, _ci in cart.items():
            if not _ci.get("is_service"):
                continue
            _svc_gst_a = round(float(_ci.get("gst_amount") or 0), 2)
            _svc_tot   = round(float(_ci.get("total") or float(_ci.get("trade_price",0))), 2)
            _svc_qty   = float(_ci.get("qty") or 1)
            _direct    = str(_ci.get("service_type","")).upper() == "COURIER"
            _svc_lp    = _svc_json.dumps({
                "charge_type":           _ci.get("service_type",""),
                "service_type":          _ci.get("service_type",""),
                "service_description":   _ci.get("service_description",""),
                "service_production_type": "" if _direct else _ci.get("service_type",""),
                "manufacturing_route":   "SERVICE",
                "service_instruction":   _ci.get("service_instruction",""),
                "service_qty_factor":    _svc_qty,
                "order_source":          "RETAILER_PORTAL",
            })
            cur.execute("""
                INSERT INTO order_lines
                  (id, order_id, eye_side,
                   unit_price, total_price, billing_total,
                   gst_percent, gst_amount,
                   quantity, billing_qty, allocated_qty,
                   is_service_line, batch_status, lens_params)
                VALUES
                  (%s::uuid, %s::uuid, 'S',
                   %s, %s, %s,
                   %s, %s,
                   %s, %s, %s,
                   TRUE, %s, %s::jsonb)
            """, (
                str(_svc_uuid.uuid4()), order_id,
                float(_ci.get("trade_price",0)), _svc_tot, _svc_tot,
                float(_ci.get("gst_percent",0)), _svc_gst_a,
                _svc_qty, _svc_qty, 1 if _direct else 0,
                "READY" if _direct else "PENDING",
                _svc_lp,
            ))

        cur.execute("COMMIT"); conn.close()
        if not is_cash:
            _mirror_retailer_order_to_backoffice(order_id)
        return {"success":True,"order_id":order_id,"order_no":order_no,
                "net_amount":totals["net"],"is_cash":is_cash,"message":"Order placed"}

    except Exception as e:
        try: cur.execute("ROLLBACK"); conn.close()
        except Exception: pass
        err = str(e).lower()
        # psycopg2 raises "could not obtain lock" on NOWAIT contention
        if "could not obtain lock" in err or "lock" in err and "nowait" in err:
            return {
                "success": False,
                "_lock_contention": True,   # signals retry in create_order
                "message": "⚠️ Another order is being processed for this item. Retrying...",
            }
        return {"success": False, "message": f"Order failed (rolled back): {e}"}


def _create_order_fallback(order_id, order_no, is_cash,
                            party, cart, payment_method, totals, notes):
    """
    Fallback when raw psycopg2 connection unavailable.
    WARNING: No FOR UPDATE lock — race condition possible under concurrent load.
    Logs loudly so this is visible in production logs.
    """
    import logging
    logging.warning(
        "[retailer_orders] FALLBACK order creation — NO transaction lock. "
        f"order_id={order_id} party={party.get('party_name','?')} "
        "Ensure sql_adapter exposes get_connection() for full safety."
    )
    from modules.sql_adapter import run_write, run_query
    from datetime import datetime, timedelta

    errors=[]
    for key,item in cart.items():
        sid=str(item.get("stock_id") or "")
        if not sid: continue
        rows=run_query("SELECT COALESCE(quantity,0) AS qty FROM inventory_stock WHERE id=%s",(sid,)) or []
        if not rows: errors.append(f"'{item['product_name']}' — SKU not found")
        elif int(rows[0]["qty"])<item["qty"]:
            errors.append(f"'{item['product_name']}' — only {int(rows[0]['qty'])} left")
    if errors:
        return {"success":False,"message":"Stock issue:\n• "+"\n• ".join(errors)}

    expires_at=(datetime.utcnow()+timedelta(minutes=30)) if is_cash else None
    status="PAYMENT_PENDING" if is_cash else "SUBMITTED"

    run_write("""INSERT INTO retailer_orders
        (id,order_no,party_id,party_name,mobile,status,payment_method,payment_status,
         subtotal,discount_pct,discount_amount,net_amount,scheme_applied,notes,
         payment_expires_at,punched_at,submitted_at,updated_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),NOW())""",
        (order_id,order_no,str(party.get("id")),party.get("party_name"),party.get("mobile"),
         status,payment_method,totals["subtotal"],totals["disc_pct"],totals["disc_amount"],
         totals["net"],totals["scheme_name"],notes,expires_at))

    for key,item in cart.items():
        trade=float(item.get("trade_price",0)); qty=item["qty"]
        line_tot=round(trade*qty*(1-totals["disc_pct"]/100),2)
        sid=str(item.get("stock_id") or "")
        sph=item.get("sph"); cyl=item.get("cyl"); axis=item.get("axis")
        add_p=item.get("add_power")
        eye=str(item.get("eye_side") or item.get("matched_eye") or "")
        expiry=str(item.get("expiry_date",""))[:10] or None
        run_write("""INSERT INTO retailer_order_lines
            (id,order_id,product_id,stock_id,sku_code,batch_no,product_name,brand,
             main_group,category,unit,sph,cyl,axis,add_power,eye_side,colour,shape,
             frame_material,size_a,size_b,dbl,expiry_date,location,
             qty,unit_price,discount_pct,line_total)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (str(uuid.uuid4()),order_id,str(item.get("product_id","")),sid or None,
             str(item.get("sku_code","") or ""),str(item.get("sku_code","") or ""),
             item.get("product_name",""),item.get("brand",""),item.get("main_group",""),
             item.get("category",""),item.get("unit","PCS"),
             float(sph) if sph is not None else None,float(cyl) if cyl is not None else None,
             int(axis)  if axis is not None else None,float(add_p) if add_p is not None else None,
             eye or None,item.get("colour") or None,item.get("shape") or None,
             item.get("frame_material") or None,
             float(item["size_a"]) if item.get("size_a") else None,
             float(item["size_b"]) if item.get("size_b") else None,
             float(item["dbl"])    if item.get("dbl")    else None,
             expiry,item.get("location") or None,
             qty,trade,totals["disc_pct"],line_tot))
        if sid:
            affected=run_write(
                "UPDATE inventory_stock SET quantity=quantity-%s,updated_at=NOW() "
                "WHERE id=%s AND COALESCE(quantity,0)>=%s",(qty,sid,qty))
            if affected==0:
                run_write(
                    "UPDATE retailer_orders SET status='CANCELLED',"
                    "notes=COALESCE(notes,'')||' [Auto-cancelled: stock exhausted]',"
                    "updated_at=NOW() WHERE id=%s",(order_id,))
                return {"success":False,"message":
                        f"'{item['product_name']}' just went out of stock. Please retry."}

    if not is_cash:
        _mirror_retailer_order_to_backoffice(order_id)
    return {"success":True,"order_id":order_id,"order_no":order_no,
            "net_amount":totals["net"],"is_cash":is_cash,"message":"Order placed"}


def update_payment_status(order_id:str,rz_oid:str,rz_pid:str)->bool:
    """
    Called after Razorpay payment success callback.
    Sets payment_status = PAID, status = SUBMITTED (ready for backoffice to confirm).
    DEV mode simulates this with dummy IDs.
    """
    try:
        from modules.sql_adapter import run_write
        run_write("""UPDATE retailer_orders
            SET payment_status      = 'PAID',
                razorpay_order_id   = %s,
                razorpay_payment_id = %s,
                status              = 'SUBMITTED',
                submitted_at        = NOW(),
                updated_at          = NOW()
            WHERE id = %s""",
            (rz_oid, rz_pid, order_id))
        _mirror_retailer_order_to_backoffice(order_id)
        return True
    except Exception:
        return False

def get_my_orders(party_id:str,limit:int=20)->list:
    try:
        from modules.sql_adapter import run_query
        return run_query("""SELECT order_no,status,payment_method,payment_status,
            subtotal,discount_amount,net_amount,scheme_applied,notes,
            TO_CHAR(punched_at,'DD-Mon-YYYY HH24:MI') AS punched_display
            FROM retailer_orders WHERE party_id=%s ORDER BY punched_at DESC LIMIT %s""",
            (party_id,limit)) or []
    except Exception: return []

def get_order_lines(order_no:str,party_id:str)->list:
    try:
        from modules.sql_adapter import run_query
        return run_query("""SELECT l.product_name,l.brand,l.sku_code,
            l.sph,l.cyl,l.axis,l.add_power,l.eye_side,l.colour,l.shape,
            l.size_a,l.size_b,l.expiry_date,l.qty,l.unit_price,l.line_total
            FROM retailer_order_lines l JOIN retailer_orders o ON o.id=l.order_id
            WHERE o.order_no=%s AND o.party_id=%s ORDER BY l.product_name""",
            (order_no,party_id)) or []
    except Exception: return []

def create_razorpay_order(amount_rupees:float,order_no:str)->dict:
    try:
        import razorpay
        key_id, key_secret = _razorpay_keys()
        if not key_id or not key_secret:
            return {"success":True,"razorpay_order_id":f"order_{uuid.uuid4().hex[:16]}",
                    "key_id":"rzp_test_demo","amount":int(amount_rupees*100),"dev_mode":True}
        client=razorpay.Client(auth=(key_id,key_secret))
        rz=client.order.create({"amount":int(amount_rupees*100),"currency":"INR","receipt":order_no})
        return {"success":True,"razorpay_order_id":rz["id"],"key_id":key_id,"amount":int(amount_rupees*100)}
    except ImportError:
        return {"success":True,"razorpay_order_id":f"order_{uuid.uuid4().hex[:16]}",
                "key_id":"rzp_test_demo","amount":int(amount_rupees*100),"dev_mode":True}
    except Exception as e: return {"success":False,"message":str(e)}

# backward compat
def get_party_discount(party_id:str)->dict:
    h=get_portal_home(party_id)
    return {"cash_disc_pct":h["cash_disc_pct"],"trade_disc_pct":h["trade_disc_pct"]}

# ══════════════════════════════════════════════════════════════
# PAYMENT EXPIRY BACKGROUND JOB
# ══════════════════════════════════════════════════════════════

def run_payment_expiry_cleanup() -> int:
    """
    Release stock for CASH orders that expired without payment.
    Calls the release_expired_payment_orders() DB function.

    Call this from app.py on startup + periodically:

        from modules.retailer.retailer_orders import start_expiry_scheduler
        start_expiry_scheduler()   # call once in app.py after imports

    Or call manually anytime:
        from modules.retailer.retailer_orders import run_payment_expiry_cleanup
        run_payment_expiry_cleanup()
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("SELECT release_expired_payment_orders() AS released") or []
        released = int(rows[0]["released"]) if rows else 0
        if released > 0:
            import logging
            logging.info(f"[retailer_orders] Released {released} expired payment order(s)")
        return released
    except Exception as e:
        import logging
        logging.warning(f"[retailer_orders] Expiry cleanup failed: {e}")
        return 0


def start_expiry_scheduler(interval_seconds: int = 600) -> None:
    """
    Start a background thread that runs payment expiry cleanup
    every `interval_seconds` (default 10 minutes).

    Add to app.py after imports:

        import streamlit as st
        from modules.retailer.retailer_orders import start_expiry_scheduler

        if "expiry_scheduler_started" not in st.session_state:
            start_expiry_scheduler()
            st.session_state["expiry_scheduler_started"] = True
    """
    import threading, logging

    def _loop():
        import time
        while True:
            try:
                run_payment_expiry_cleanup()
            except Exception as e:
                logging.warning(f"[expiry_scheduler] {e}")
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, daemon=True, name="payment-expiry-scheduler")
    t.start()
    import logging
    logging.info(f"[retailer_orders] Payment expiry scheduler started (every {interval_seconds}s)")
