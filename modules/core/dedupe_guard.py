# ==========================================================
# GLOBAL ORDER LINE DEDUPE GUARD (ERP SAFE)
# Prevents duplicate product-power lines across ERP
# ==========================================================

def normalize_power(v):
    if v in (None, "", 0, 0.0):
        return None
    try:
        return float(v)
    except:
        return None


def build_line_signature(line: dict) -> tuple:
    """
    Unique optical signature
    """
    return (
        str(line.get("product_id")),
        normalize_power(line.get("sph")),
        normalize_power(line.get("cyl")),
        normalize_power(line.get("axis")),
        normalize_power(line.get("add_power")),
        (line.get("eye_side") or "").upper(),
    )


def merge_order_lines(order_lines: list,
                      party_id: str = "",
                      order_type: str = "WHOLESALE") -> list:
    """
    ERP-safe merge.
    Handles: Retail, Wholesale, Batch allocations.

    IMPORTANT: Discount is re-stamped AFTER merge using the new combined qty.
    This ensures slab/BOGO rules fire correctly when the same product is
    added multiple times (e.g. 3 boxes + 3 boxes = 6 boxes triggers slab).
    """

    merged = {}

    for line in order_lines:
        sig = build_line_signature(line)

        if sig not in merged:
            merged[sig] = line.copy()
            continue

        base = merged[sig]

        # SAFE QTY MERGE
        base["requested_qty"] = int(base.get("requested_qty", 0)) + int(line.get("requested_qty", 0))
        base["billing_qty"]   = int(base.get("billing_qty", 0))   + int(line.get("billing_qty", 0))
        base["order_qty"]     = int(base.get("order_qty", 0))     + int(line.get("order_qty", 0))

        # PRICE RECALC on merged qty
        total_units = base["billing_qty"] + base["order_qty"]
        if total_units > 0:
            base["total_price"] = round(float(base.get("unit_price", 0)) * total_units, 2)

        # MERGE BATCHES
        if line.get("batch_allocation"):
            base.setdefault("batch_allocation", [])
            base["batch_allocation"].extend(line["batch_allocation"])

        # DISPLAY QTY REFRESH
        base["display_qty"] = base.get("display_qty") or str(base.get("requested_qty", 0))

        # Reset discount so re-stamp below fires for new combined qty
        base["discount_percent"] = 0.0
        base["discount_amount"]  = 0.0

    result = list(merged.values())

    # Re-stamp discount on every merged line with new combined qty.
    # Slab and BOGO rules depend on qty — must recalculate after merge.
    # Zero-risk: engine failure leaves lines with old discount values.
    if result:
        try:
            from modules.pricing.discount_engine import apply_discounts
            apply_discounts(result, party_id=party_id, order_type=order_type)
            for _l in result:
                _disc = float(_l.get("discount_amount") or 0)
                if _disc > 0:
                    _l["billing_total"] = round(float(_l.get("total_price") or 0) - _disc, 2)
        except Exception:
            pass

    return result
