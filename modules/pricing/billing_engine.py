# modules/pricing/billing_engine.py
"""
Billing Engine — Single source of truth for line-level price computation.

All billing paths (UI preview, create_challan, create_invoice) call this
function so that GST extraction/addition logic lives in exactly one place.

Rules:
  RETAIL   — price = MRP (tax-inclusive). GST is back-calculated (extracted).
             total = base (MRP total); customer pays MRP, not MRP+GST.
  PURCHASE — price = purchase_rate (ex-GST). GST added on top.
  WHOLESALE (default) — price = selling_price (ex-GST). GST added on top.

box_size normalisation:
  Prices in inventory_stock are stored per-BOX.
  pcs_price = box_price / box_size  (always divide before multiplying by qty).
"""


def compute_line_totals(
    quantity: int,
    box_size: int,
    selling_price: float,
    mrp: float,
    purchase_rate: float,
    order_type: str,
    gst_percent: float,
) -> dict:
    """
    Compute base, tax, and total for one order line.

    Parameters
    ----------
    quantity      : number of pieces to bill
    box_size      : units per box (prices stored per box in inventory_stock)
    selling_price : ex-GST wholesale price per box
    mrp           : MRP (tax-inclusive) per box
    purchase_rate : ex-GST purchase cost per box
    order_type    : 'RETAIL' | 'PURCHASE' | 'WHOLESALE' (anything else → WHOLESALE)
    gst_percent   : GST rate as a percentage (e.g. 12 for 12%)

    Returns
    -------
    dict with keys:
        unit_price : per-piece price used for this line
        base       : taxable base amount  (ex-GST for wholesale/purchase;
                                           MRP total for retail)
        tax        : GST amount
        total      : amount customer pays  (base + tax for wholesale/purchase;
                                           base only for retail — tax already inside)
    """
    order_type    = (order_type or "WHOLESALE").upper()
    quantity      = int(quantity or 0)
    box_size      = max(int(box_size or 1), 1)
    gst_percent   = float(gst_percent or 0)
    selling_price = float(selling_price or 0)
    mrp           = float(mrp or 0)
    purchase_rate = float(purchase_rate or 0)

    # 1. Pick the right box-level price
    if order_type == "RETAIL":
        box_price = mrp
    elif order_type == "PURCHASE":
        box_price = purchase_rate
    else:
        box_price = selling_price

    # 2. Normalise to per-piece price
    pcs_price = round(box_price / box_size, 2)

    # 3. Base amount
    base = round(quantity * pcs_price, 2)

    # 4. Tax and total — retail is tax-inclusive, others are tax-exclusive
    if order_type == "RETAIL":
        # GST already embedded in MRP — extract it back
        tax   = round(base * gst_percent / (100 + gst_percent), 2) if gst_percent else 0.0
        total = base          # customer pays MRP total, not MRP + extra GST
    else:
        tax   = round(base * gst_percent / 100, 2) if gst_percent else 0.0
        total = round(base + tax, 2)

    return {
        "unit_price": pcs_price,
        "base":       base,
        "tax":        tax,
        "total":      total,
    }
