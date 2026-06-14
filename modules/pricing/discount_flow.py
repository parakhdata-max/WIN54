"""
modules/pricing/discount_flow.py
================================
Shared discount lifecycle helpers.

Purpose:
  - Apply party/rule discounts and immediately restamp line totals + GST.
  - Persist a discount cancellation flag in lens_params.
  - Let Backoffice cancel/reinstate discount before challan/invoice.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List


def _as_dict(value) -> Dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def discount_cancelled(line: Dict) -> bool:
    lp = _as_dict(line.get("lens_params"))
    return str(lp.get("discount_status") or "").upper() == "CANCELLED"


def gross_amount(line: Dict) -> float:
    qty = int(line.get("billing_qty") or line.get("quantity") or 1)
    return round(float(line.get("unit_price") or 0) * qty, 2)


def _is_service_line(line: Dict) -> bool:
    eye = str(line.get("eye_side") or "").upper()
    lp = _as_dict(line.get("lens_params"))
    route = str(lp.get("manufacturing_route") or lp.get("service_production_type") or "").upper()
    return (
        bool(line.get("is_service_line"))
        or eye in ("S", "SERVICE")
        or bool(lp.get("service_type") or lp.get("charge_type"))
        or route in ("FITTING", "COLOURING", "SERVICE")
    )


def restamp_line_totals(line: Dict, order_type: str = "WHOLESALE") -> Dict:
    """Recompute net total and GST from current discount fields."""
    ot = str(order_type or line.get("order_type") or "WHOLESALE").upper()
    gross = gross_amount(line)

    if discount_cancelled(line):
        line["discount_percent"] = 0.0
        line["discount_amount"] = 0.0
        line["discount_rule"] = ""
        line["applied_rule_ids"] = ""

    disc = min(max(float(line.get("discount_amount") or 0), 0.0), gross)
    net = round(gross - disc, 2)
    gst_pct = float(line.get("gst_percent") or 0)

    if gst_pct <= 0 or net <= 0:
        gst_amt = 0.0
    elif ot == "RETAIL":
        gst_amt = round(net - (net / (1 + gst_pct / 100)), 2)
    else:
        gst_amt = round(net * gst_pct / 100, 2)

    # Product rows store taxable/net in both fields. Wholesale service rows
    # are different: billing checks treat billing_total as customer payable
    # amount, so keep total_price taxable and billing_total GST-inclusive.
    line["total_price"] = net
    if _is_service_line(line) and ot != "RETAIL" and gst_pct > 0:
        line["billing_total"] = round(net + gst_amt, 2)
    else:
        line["billing_total"] = net
    line["gst_amount"] = gst_amt
    return line


def apply_order_discounts(
    lines: List[Dict],
    party_id: str = "",
    order_type: str = "WHOLESALE",
) -> List[Dict]:
    """Apply discount rules to active lines and restamp all totals."""
    active = [line for line in lines if not discount_cancelled(line)]
    if active:
        try:
            from modules.pricing.discount_engine import apply_discounts
            apply_discounts(active, party_id=party_id, order_type=order_type)
        except Exception:
            pass

    for line in lines:
        lp = _as_dict(line.get("lens_params"))
        if discount_cancelled(line):
            lp["discount_status"] = "CANCELLED"
        elif float(line.get("discount_amount") or 0) > 0:
            lp["discount_status"] = "APPLIED"
        line["lens_params"] = lp
        restamp_line_totals(line, order_type)
    return lines


def _line_update_sql(has_billing_total: bool) -> str:
    bt_clause = ", billing_total = %(total_price)s" if has_billing_total else ""
    return f"""
        UPDATE order_lines
        SET discount_percent = %(discount_percent)s,
            discount_amount  = %(discount_amount)s,
            total_price      = %(total_price)s,
            gst_amount       = %(gst_amount)s,
            applied_rule_ids = %(applied_rule_ids)s,
            lens_params      = %(lens_params)s::jsonb
            {bt_clause}
        WHERE id = %(line_id)s::uuid
    """


def _has_order_line_column(column_name: str) -> bool:
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'order_lines'
              AND column_name = %(col)s
            LIMIT 1
        """, {"col": column_name}) or []
        return bool(rows)
    except Exception:
        return False


def _load_order_lines_for_discount(order_id: str) -> tuple[Dict, List[Dict]]:
    from modules.sql_adapter import run_query
    order_rows = run_query("""
        SELECT id::text AS id, order_no, order_type, party_id::text AS party_id
        FROM orders
        WHERE id = %(oid)s::uuid
        LIMIT 1
    """, {"oid": order_id}) or []
    order = order_rows[0] if order_rows else {}

    lines = run_query("""
        SELECT ol.id::text AS line_id,
               ol.product_id::text AS product_id,
               COALESCE(p.product_name, '') AS product_name,
               COALESCE(p.brand, '') AS brand,
               COALESCE(p.main_group, '') AS main_group,
               COALESCE(ol.quantity, 1) AS quantity,
               COALESCE(ol.billing_qty, ol.quantity, 1) AS billing_qty,
               COALESCE(ol.unit_price, 0) AS unit_price,
               COALESCE(ol.total_price, 0) AS total_price,
               COALESCE(ol.gst_percent, p.gst_percent, 0) AS gst_percent,
               COALESCE(ol.discount_percent, 0) AS discount_percent,
               COALESCE(ol.discount_amount, 0) AS discount_amount,
               COALESCE(ol.applied_rule_ids, '') AS applied_rule_ids,
               COALESCE(ol.lens_params, '{{}}')::text AS lens_params
        FROM order_lines ol
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE ol.order_id = %(oid)s::uuid
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
    """, {"oid": order_id}) or []

    for line in lines:
        line["lens_params"] = _as_dict(line.get("lens_params"))
    return order, lines


def save_discount_lines(lines: Iterable[Dict]) -> None:
    from modules.sql_adapter import run_transaction
    has_bt = _has_order_line_column("billing_total")
    sql = _line_update_sql(has_bt)
    steps = []
    for line in lines:
        steps.append((sql, {
            "line_id": line.get("line_id") or line.get("id"),
            "discount_percent": float(line.get("discount_percent") or 0),
            "discount_amount": float(line.get("discount_amount") or 0),
            "total_price": float(line.get("billing_total") or line.get("total_price") or 0),
            "gst_amount": float(line.get("gst_amount") or 0),
            "applied_rule_ids": str(line.get("applied_rule_ids") or ""),
            "lens_params": json.dumps(_as_dict(line.get("lens_params"))),
        }))
    if steps:
        run_transaction(steps)


def cancel_order_discount(order_id: str, user: str = "Backoffice") -> Dict:
    order, lines = _load_order_lines_for_discount(order_id)
    ot = str(order.get("order_type") or "WHOLESALE").upper()
    for line in lines:
        lp = _as_dict(line.get("lens_params"))
        if float(line.get("discount_amount") or 0) > 0:
            lp["discount_previous"] = {
                "discount_percent": float(line.get("discount_percent") or 0),
                "discount_amount": float(line.get("discount_amount") or 0),
                "applied_rule_ids": str(line.get("applied_rule_ids") or ""),
            }
        lp["discount_status"] = "CANCELLED"
        lp["discount_cancelled_by"] = user
        line["lens_params"] = lp
        line["discount_percent"] = 0.0
        line["discount_amount"] = 0.0
        line["applied_rule_ids"] = ""
        restamp_line_totals(line, ot)
    save_discount_lines(lines)
    return discount_summary(lines)


def reinstate_order_discount(order_id: str) -> Dict:
    order, lines = _load_order_lines_for_discount(order_id)
    ot = str(order.get("order_type") or "WHOLESALE").upper()
    for line in lines:
        lp = _as_dict(line.get("lens_params"))
        lp.pop("discount_status", None)
        lp.pop("discount_cancelled_by", None)
        line["lens_params"] = lp
        line["discount_percent"] = 0.0
        line["discount_amount"] = 0.0
        line["applied_rule_ids"] = ""
    apply_order_discounts(lines, party_id=str(order.get("party_id") or ""), order_type=ot)
    save_discount_lines(lines)
    return discount_summary(lines)


def discount_summary(lines: Iterable[Dict]) -> Dict:
    lines = list(lines or [])
    gross = sum(gross_amount(line) for line in lines)
    discount = sum(float(line.get("discount_amount") or 0) for line in lines)
    net = sum(float(line.get("billing_total") or line.get("total_price") or 0) for line in lines)
    cancelled = any(discount_cancelled(line) for line in lines)
    return {
        "gross": round(gross, 2),
        "discount": round(discount, 2),
        "net": round(net, 2),
        "cancelled": cancelled,
    }
