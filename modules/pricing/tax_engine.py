# modules/pricing/tax_engine.py

import hashlib
import json
from datetime import date


def resolve_gst_percent(line, bill_date, gst_lookup=None, order=None):
    """
    ONE SOURCE OF TRUTH for GST resolution.

    Priority (highest → lowest):
        1. line["gst_percent"]        — saved at order creation time (authoritative)
        2. product_gst_history lookup — only if line has no gst_percent (old orders)
        3. 0.0                         — no data available

    WHY this order:
        order_lines.gst_percent is stamped at punching time from the product master.
        It is the contract value for that order. The history table should NEVER
        override an already-saved line value — it is only a fallback for orders
        saved before gst_percent was added as a column (would be NULL/0).
    """
    # ── Priority 1: use what was saved on the line ────────────────────────────
    line_gst = line.get("gst_percent")
    if line_gst is not None and float(line_gst) > 0:
        return float(line_gst)

    # ── Priority 2: history table — ONLY if line has no gst_percent (old data) ─
    if gst_lookup and line.get("product_id"):
        cache = None
        if order is not None:
            cache = order.setdefault("_gst_cache", {})

        key = (line["product_id"], str(bill_date))

        if cache is not None:
            if key not in cache:
                cache[key] = gst_lookup(line["product_id"], bill_date)
            hist_gst = cache[key]
        else:
            hist_gst = gst_lookup(line["product_id"], bill_date)

        if hist_gst is not None:
            return float(hist_gst)

    # ── Priority 3: no data ───────────────────────────────────────────────────
    return 0.0


def apply_taxes(order):
    """
    Apply GST per line using gst_percent from product table.

    RETAIL   → MRP is GST-inclusive. Extract (back-calculate) GST from total_price.
               final_value = total_price (no addition, GST already inside)

    WHOLESALE / PURCHASE → Price is GST-exclusive. Add GST on top.
               final_value = total_price + gst_amount
    """

    order_type = (order.get("order_info", {}).get("order_type")
                  or order.get("order_type", "RETAIL")).upper()

    lines = order.get("lines", [])

    total_tax   = 0.0
    net_value   = order.get("net_value", order.get("total_value", 0))

    for line in lines:
        # Skip lines that have no price yet (provisional / unallocated)
        line_total  = float(line.get("billing_total") or line.get("total_price") or 0)
        bill_date   = order.get("bill_date") or date.today()
        gst_percent = resolve_gst_percent(line, bill_date, order.get("gst_lookup"), order)

        # Stamp resolved GST on line — self-contained tax record for audit / disputes
        line["gst_percent_used"] = gst_percent
        line["tax_inclusive"]    = (order_type == "RETAIL")
        line["gst_resolved_at"]  = str(bill_date)

        if line_total == 0 or gst_percent == 0:
            line["gst_amount"] = 0.0
            continue

        if order_type == "RETAIL":
            # MRP is inclusive — back-calculate GST
            # GST amount = total - (total / (1 + rate/100))
            gst_amount = round(line_total - (line_total / (1 + gst_percent / 100)), 2)
        else:
            # WHOLESALE or PURCHASE — price is exclusive, add GST on top
            gst_amount = round(line_total * (gst_percent / 100), 2)

        line["gst_amount"] = gst_amount
        line["tax_hash"]   = hashlib.sha1(
            json.dumps({
                "product":   line.get("product_id"),
                "base":      round(line_total, 2),
                "gst":       gst_percent,
                "inclusive": line["tax_inclusive"],
                "amount":    gst_amount,
            }, sort_keys=True).encode()
        ).hexdigest()
        total_tax += gst_amount

    total_tax = round(total_tax, 2)

    if order_type == "RETAIL":
        # GST already inside MRP — final value does not change
        final_value = round(net_value, 2)
    else:
        # Add GST on top
        final_value = round(net_value + total_tax, 2)

    order["tax_amount"]  = total_tax
    order["final_value"] = final_value

    return order
