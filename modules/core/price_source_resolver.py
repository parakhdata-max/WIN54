"""
DB-aware price source resolver for WIN54.

This module is the single place that decides where a product price comes from:

1. Physical batch price when stock exists.
2. Current Price Master row (inventory_stock.stock_type = 'PRICE').
3. Ophthalmic spec price when index/coating/treatment is supplied.
4. Any active inventory price row as a last DB fallback.

The pure price_qty_governor module still owns math: field choice by order type,
box-to-pcs normalization, GST mode, and totals. This resolver only fetches the
right DB row and reports the source clearly.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from modules.core.price_qty_governor import normalize_to_pcs_price, resolve_price
from modules.sql_adapter import run_query


def _safe_float(value: Any) -> float:
    try:
        import math

        out = float(value)
        return 0.0 if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return 0.0


def _safe_int(value: Any, default: int = 1) -> int:
    try:
        out = int(float(value))
        return out if out > 0 else default
    except Exception:
        return default


def _product_context(product_id: str, product: Optional[dict] = None) -> dict:
    ctx = dict(product or {})
    ctx.setdefault("product_id", str(product_id or ctx.get("product_id") or ""))
    if ctx.get("unit") and ctx.get("box_size") and ctx.get("gst_percent") is not None:
        ctx["box_size"] = _safe_int(ctx.get("box_size"), 1)
        return ctx

    rows = run_query(
        """
        SELECT
            id::text AS product_id,
            product_name,
            brand,
            main_group,
            COALESCE(unit, 'PCS') AS unit,
            GREATEST(COALESCE(box_size, 1), 1) AS box_size,
            COALESCE(gst_percent, 0) AS gst_percent
        FROM products
        WHERE id = %s::uuid
        LIMIT 1
        """,
        (str(product_id),),
    ) or []
    if rows:
        dbp = dict(rows[0])
        for key, value in dbp.items():
            if value is not None and (key not in ctx or ctx.get(key) in (None, "", 0)):
                ctx[key] = value
    ctx["box_size"] = _safe_int(ctx.get("box_size"), 1)
    ctx["unit"] = str(ctx.get("unit") or "PCS")
    return ctx


def _price_dict(row: dict, order_type: str, product: dict, source: str, source_id: str = "") -> dict:
    raw = resolve_price(row or {}, order_type)
    pcs = normalize_to_pcs_price(raw, product) if raw > 0 else 0.0
    box_size = _safe_int(product.get("box_size"), 1)
    return {
        "found": raw > 0,
        "source": source,
        "source_id": source_id or str(row.get("id") or row.get("price_row_id") or ""),
        "raw_price": round(raw, 2),
        "pcs_price": round(pcs, 2),
        "box_price": round(pcs * box_size, 2) if pcs > 0 else 0.0,
        "mrp": _safe_float(row.get("mrp")),
        "selling_price": _safe_float(row.get("selling_price")),
        "purchase_rate": _safe_float(row.get("purchase_rate")),
        "gst_percent": _safe_float(product.get("gst_percent")),
        "unit": str(product.get("unit") or "PCS"),
        "box_size": box_size,
        "row": dict(row or {}),
    }


def _batch_price(product_id: str, order_type: str, product: dict) -> Optional[dict]:
    rows = run_query(
        """
        SELECT
            id::text AS id,
            batch_no,
            stock_type,
            quantity,
            COALESCE(mrp, 0) AS mrp,
            COALESCE(selling_price, 0) AS selling_price,
            COALESCE(purchase_rate, 0) AS purchase_rate,
            updated_at,
            created_at
        FROM inventory_stock
        WHERE product_id = %s::uuid
          AND COALESCE(is_active, TRUE) = TRUE
          AND COALESCE(stock_type, 'BATCH') <> 'PRICE'
          AND COALESCE(quantity, 0) > 0
          AND COALESCE(NULLIF(TRIM(batch_no), ''), '') <> ''
          AND (COALESCE(mrp,0) > 0 OR COALESCE(selling_price,0) > 0)
        ORDER BY expiry_date ASC NULLS LAST,
                 updated_at DESC NULLS LAST,
                 created_at DESC NULLS LAST
        LIMIT 1
        """,
        (str(product_id),),
    ) or []
    if not rows:
        return None
    return _price_dict(dict(rows[0]), order_type, product, "BATCH", str(rows[0].get("id") or ""))


def _price_master(product_id: str, order_type: str, product: dict) -> Optional[dict]:
    rows = run_query(
        """
        SELECT
            id::text AS id,
            stock_type,
            is_price_current,
            effective_from,
            COALESCE(mrp, 0) AS mrp,
            COALESCE(selling_price, 0) AS selling_price,
            COALESCE(purchase_rate, 0) AS purchase_rate
        FROM inventory_stock
        WHERE product_id = %s::uuid
          AND stock_type = 'PRICE'
          AND COALESCE(is_price_current, TRUE) = TRUE
          AND COALESCE(is_active, TRUE) = TRUE
          AND (COALESCE(mrp,0) > 0 OR COALESCE(selling_price,0) > 0)
        ORDER BY effective_from DESC NULLS LAST,
                 updated_at DESC NULLS LAST,
                 created_at DESC NULLS LAST
        LIMIT 1
        """,
        (str(product_id),),
    ) or []
    if not rows:
        return None
    return _price_dict(dict(rows[0]), order_type, product, "PRICE_MASTER", str(rows[0].get("id") or ""))


def _oph_spec_price(
    product_id: str,
    order_type: str,
    product: dict,
    *,
    index_value: Any = None,
    coating: Any = None,
    treatment: Any = None,
) -> Optional[dict]:
    if not index_value or not coating:
        return None
    rows = run_query(
        """
        SELECT
            id::text AS id,
            COALESCE(srp_per_pair, 0) AS mrp,
            COALESCE(wlp_per_pair, 0) AS selling_price,
            COALESCE(purchase_rate, 0) AS purchase_rate
        FROM ophthalmic_lens_specs
        WHERE product_id = %s::uuid
          AND index_value = %s::numeric
          AND coating = %s
          AND COALESCE(treatment, 'Clear') = COALESCE(%s, 'Clear')
          AND COALESCE(is_active, TRUE) = TRUE
        LIMIT 1
        """,
        (str(product_id), str(index_value), str(coating), str(treatment or "Clear")),
    ) or []
    if not rows:
        return None

    # Specs are per pair. Convert to per-lens for immediate billing use and mark
    # box_size=1 so callers do not divide again.
    row = dict(rows[0])
    if str(order_type or "").upper() == "RETAIL":
        row["mrp"] = round(_safe_float(row.get("mrp")) / 2, 2)
    else:
        row["selling_price"] = round(_safe_float(row.get("selling_price")) / 2, 2)
    spec_product = dict(product)
    spec_product["box_size"] = 1
    spec_product["unit"] = "PCS"
    out = _price_dict(row, order_type, spec_product, "OPH_SPEC", str(row.get("id") or ""))
    out["unit"] = "PCS"
    out["box_size"] = 1
    return out


def _any_inventory_price(product_id: str, order_type: str, product: dict) -> Optional[dict]:
    rows = run_query(
        """
        SELECT
            id::text AS id,
            batch_no,
            stock_type,
            quantity,
            COALESCE(mrp, 0) AS mrp,
            COALESCE(selling_price, 0) AS selling_price,
            COALESCE(purchase_rate, 0) AS purchase_rate
        FROM inventory_stock
        WHERE product_id = %s::uuid
          AND COALESCE(is_active, TRUE) = TRUE
          AND (COALESCE(mrp,0) > 0 OR COALESCE(selling_price,0) > 0)
        ORDER BY
          CASE WHEN stock_type = 'PRICE' THEN 0 ELSE 1 END,
          COALESCE(is_price_current, FALSE) DESC,
          updated_at DESC NULLS LAST,
          created_at DESC NULLS LAST
        LIMIT 1
        """,
        (str(product_id),),
    ) or []
    if not rows:
        return None
    return _price_dict(dict(rows[0]), order_type, product, "INVENTORY_FALLBACK", str(rows[0].get("id") or ""))


def resolve_db_price(
    product_id: str,
    order_type: str = "RETAIL",
    *,
    product: Optional[dict] = None,
    prefer_batch: bool = True,
    index_value: Any = None,
    coating: Any = None,
    treatment: Any = None,
) -> dict:
    """
    Resolve price using the official DB hierarchy.

    For stock billing, keep prefer_batch=True. For RX/to-order lines with no
    selected batch, pass prefer_batch=False to use Price Master before stock.
    """
    product_id = str(product_id or (product or {}).get("product_id") or "")
    if not product_id:
        return {"found": False, "source": "NO_PRODUCT", "raw_price": 0.0, "pcs_price": 0.0}

    ctx = _product_context(product_id, product)
    candidates = []
    if prefer_batch:
        candidates.append(_batch_price(product_id, order_type, ctx))
    # Specific ophthalmic spec beats generic Price Master when staff selected
    # index/coating/treatment. For contact/non-spec items this simply returns
    # None and Price Master is used.
    candidates.extend(
        [
            _oph_spec_price(
                product_id,
                order_type,
                ctx,
                index_value=index_value,
                coating=coating,
                treatment=treatment,
            ),
            _price_master(product_id, order_type, ctx),
            _any_inventory_price(product_id, order_type, ctx),
        ]
    )
    for candidate in candidates:
        if candidate and candidate.get("found"):
            return candidate
    return {
        "found": False,
        "source": "NO_PRICE",
        "raw_price": 0.0,
        "pcs_price": 0.0,
        "box_price": 0.0,
        "mrp": 0.0,
        "selling_price": 0.0,
        "purchase_rate": 0.0,
        "gst_percent": _safe_float(ctx.get("gst_percent")),
        "unit": str(ctx.get("unit") or "PCS"),
        "box_size": _safe_int(ctx.get("box_size"), 1),
        "row": {},
    }
