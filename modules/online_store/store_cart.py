"""
modules/online_store/store_cart.py
====================================
Cart stored in Streamlit session (no DB until order placed).
Supports product lines + power-specific CL lines + service lines.
"""
from __future__ import annotations
import streamlit as st
from typing import List, Dict


CART_KEY = "ow_cart"


def cart_get() -> dict:
    return st.session_state.get(CART_KEY, {})


def cart_add(item: dict) -> None:
    """
    item keys: product_id, product_name, price, gst_percent,
               qty, eye_side (optional), sph/cyl/axis/add_power (optional),
               stock_id (optional), image_b64/image_url (optional)
    """
    item = _resolve_online_contact_item(dict(item or {}))
    cart = cart_get()
    key = _cart_key(item)
    if key in cart:
        cart[key]["qty"] += item.get("qty", 1)
    else:
        cart[key] = {**item, "qty": item.get("qty", 1), "cart_key": key}
    st.session_state[CART_KEY] = cart


def _is_contact_product(row: dict) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("main_group", "category", "lens_category")).lower()
    return "contact" in text or "soft lens" in text


def _resolve_online_contact_item(item: dict) -> dict:
    """
    Online cart guard: for contact lenses, the selected product is only a family
    hint. The entered power decides SPH/Toric/Multifocal product and stock row.
    """
    try:
        pid = str(item.get("product_id") or "")
        if not pid:
            return item

        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT id::text AS product_id,
                   product_name,
                   COALESCE(brand,'') AS brand,
                   COALESCE(company_product_name,'') AS company_product_name,
                   COALESCE(main_group,'') AS main_group,
                   COALESCE(category,'') AS category,
                   COALESCE(lens_category,'') AS lens_category,
                   COALESCE(unit,'PCS') AS unit,
                   COALESCE(gst_percent,0) AS gst_percent
            FROM products
            WHERE id = %(pid)s::uuid
            LIMIT 1
        """, {"pid": pid}) or []
        if not rows:
            return item
        prod = dict(rows[0])
        if not _is_contact_product(prod):
            return item

        from modules.contact_lens_resolver import line_for_eye, resolve_contact_lens_order
        eye = str(item.get("eye_side") or item.get("matched_eye") or "R").upper()[:1]
        if eye not in ("R", "L"):
            eye = "R"
        power = {
            "sph": item.get("sph"),
            "cyl": item.get("cyl"),
            "axis": item.get("axis"),
            "add": item.get("add_power"),
        }
        res = resolve_contact_lens_order(
            selected_product=str(item.get("product_name") or prod.get("product_name") or ""),
            right_power=power if eye == "R" else None,
            left_power=power if eye == "L" else None,
            brand=prod.get("brand"),
            company_product_name=prod.get("company_product_name"),
        )
        line = line_for_eye(res, eye)
        row = line.get("product_row") or {}
        if not row.get("product_id"):
            return item

        out = dict(item)
        out["product_id"] = row.get("product_id")
        out["product_name"] = row.get("product_name") or out.get("product_name")
        out["brand"] = row.get("brand") or prod.get("brand") or out.get("brand")
        out["main_group"] = row.get("main_group") or prod.get("main_group") or out.get("main_group")
        out["category"] = row.get("category") or prod.get("category") or out.get("category")
        out["unit"] = row.get("unit") or prod.get("unit") or out.get("unit")
        out["gst_percent"] = float(row.get("gst_percent") or prod.get("gst_percent") or out.get("gst_percent") or 0)
        if line.get("stock_id"):
            out["stock_id"] = line.get("stock_id")
        out["cl_resolver_route"] = line.get("route")
        out["cl_lens_type"] = line.get("lens_type")
        return out
    except Exception:
        return item


def cart_update_qty(cart_key: str, qty: int) -> None:
    cart = cart_get()
    if cart_key in cart:
        if qty <= 0:
            del cart[cart_key]
        else:
            cart[cart_key]["qty"] = qty
    st.session_state[CART_KEY] = cart


def cart_remove(cart_key: str) -> None:
    cart = cart_get()
    cart.pop(cart_key, None)
    st.session_state[CART_KEY] = cart


def cart_clear() -> None:
    st.session_state[CART_KEY] = {}


def cart_totals(promo: dict = None) -> dict:
    cart = cart_get()
    subtotal = sum(
        float(v.get("price", 0)) * int(v.get("qty", 1))
        for v in cart.values()
    )
    disc_amt = 0.0
    if promo:
        if promo["disc_type"] == "PCT":
            disc_amt = round(subtotal * promo["disc_value"] / 100, 2)
            if promo.get("max_disc"):
                disc_amt = min(disc_amt, float(promo["max_disc"]))
        else:
            disc_amt = min(float(promo["disc_value"]), subtotal)

    taxable    = round(subtotal - disc_amt, 2)
    gst_amt    = round(sum(
        float(v.get("price", 0)) * int(v.get("qty", 1)) *
        float(v.get("gst_percent", 0)) / (100 + float(v.get("gst_percent", 0)))
        for v in cart.values()
    ), 2)
    delivery   = 0.0 if subtotal >= 500 else 50.0
    total      = round(taxable + delivery, 2)

    return {
        "subtotal":  subtotal,
        "discount":  disc_amt,
        "taxable":   taxable,
        "gst":       gst_amt,
        "delivery":  delivery,
        "total":     total,
        "item_count": sum(int(v.get("qty", 1)) for v in cart.values()),
    }


def validate_promo(code: str, subtotal: float) -> dict:
    from modules.sql_adapter import run_query
    rows = run_query("""
        SELECT * FROM promo_codes
        WHERE UPPER(code)=UPPER(%(c)s)
          AND is_active=TRUE
          AND (valid_from IS NULL OR valid_from <= CURRENT_DATE)
          AND (valid_to IS NULL OR valid_to >= CURRENT_DATE)
          AND (uses_limit IS NULL OR uses_count < uses_limit)
          AND min_order <= %(amt)s
        LIMIT 1
    """, {"c": code, "amt": subtotal}) or []
    if not rows:
        return {"valid": False, "message": "Invalid or expired promo code"}
    p = dict(rows[0])
    return {"valid": True, **p}


def _cart_key(item: dict) -> str:
    pid   = str(item.get("product_id", ""))
    eye   = str(item.get("eye_side", "") or "")
    sph   = str(item.get("sph", "") or "")
    cyl   = str(item.get("cyl", "") or "")
    ax    = str(item.get("axis", "") or "")
    sid   = str(item.get("stock_id", "") or "")
    return f"{pid}|{eye}|{sph}|{cyl}|{ax}|{sid}"
