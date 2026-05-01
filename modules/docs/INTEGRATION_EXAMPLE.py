"""
optical_discount_engine/INTEGRATION_EXAMPLE.py

How to plug the Discount Engine into your existing billing pipeline.

This file shows 3 integration patterns:
  1. Minimal — just calculate discount for one line
  2. Full billing pipeline — load rules from DB, calculate invoice
  3. Preview mode — return JSON for UI
"""

from decimal import Decimal

# ─────────────────────────────────────────────
# PATTERN 1 — MINIMAL (no DB, manual rules)
# ─────────────────────────────────────────────

def example_minimal():
    """
    Quickest way to get started.
    Hardcode a few rules and calculate a line.
    """
    from core.engine import DiscountEngine
    from models.discount_rule import DiscountRule, LineItem, RuleType, ValueType, RuleConditions

    # Define your rules
    rules = [
        DiscountRule(
            id="party-12", name="Wholesale 12%",
            type=RuleType.PARTY, value_type=ValueType.PERCENT,
            value=Decimal("12"), gst_rate=Decimal("12"),
            conditions=RuleConditions(party_tags=["wholesale"]),
            priority=2, active=True,
        )
    ]

    engine = DiscountEngine(rules)

    # Your billing line item
    line = LineItem(
        base_price  = Decimal("1200"),
        quantity    = 5,
        product_cat = "frame",
        party_tags  = ["wholesale"],
    )

    result = engine.calculate(line)
    print(result.pretty())
    # Output:
    #   Base Price   : ₹1200 × 5 = ₹6000
    #   Rule Applied : Wholesale 12%
    #   Discount     : 12.00% = −₹720.00
    #   Net Amount   : ₹5280.00
    #   GST (12%)    : +₹633.60
    #   Final Amount : ₹5913.60

    return result


# ─────────────────────────────────────────────
# PATTERN 2 — FULL PIPELINE WITH DB
# ─────────────────────────────────────────────

def example_with_db(db_conn, invoice_id: str, billing_lines: list):
    """
    Full production pipeline.
    Load rules from DB, calculate all lines, log applications.

    Args:
        db_conn:       psycopg2 connection
        invoice_id:    your invoice UUID
        billing_lines: list of dicts from your DB with
                       {base_price, quantity, product_cat, party_tags, line_id}

    Returns:
        Invoice totals dict
    """
    from core.engine import DiscountEngine
    from core.discount_adapter import DiscountAdapter
    from models.discount_rule import LineItem

    adapter = DiscountAdapter(db_conn)
    rules   = adapter.get_active_rules()      # Fetch all active rules from discount_rules table
    engine  = DiscountEngine(rules)

    line_items = []
    for bl in billing_lines:
        line_items.append(LineItem(
            base_price  = Decimal(str(bl["unit_price"])),
            quantity    = bl["quantity"],
            product_id  = bl.get("product_id"),
            product_cat = bl.get("product_category"),
            party_id    = bl.get("party_id"),
            party_tags  = bl.get("party_tags", []),
        ))

    # Calculate with discount + tax
    invoice_result = engine.calculate_invoice(line_items)

    # Log each application to audit trail
    for bl, line_result in zip(billing_lines, invoice_result["lines"]):
        result = engine.calculate(LineItem(
            base_price  = Decimal(str(bl["unit_price"])),
            quantity    = bl["quantity"],
            product_cat = bl.get("product_category"),
            party_tags  = bl.get("party_tags", []),
        ))
        adapter.log_application(
            invoice_id      = invoice_id,
            invoice_line_id = bl.get("line_id"),
            item            = LineItem(
                base_price  = Decimal(str(bl["unit_price"])),
                quantity    = bl["quantity"],
            ),
            result          = result,
            applied_by      = "billing_system",
        )

    return invoice_result["totals"]


# ─────────────────────────────────────────────
# PATTERN 3 — PREVIEW JSON (for UI panel)
# ─────────────────────────────────────────────

def example_preview_json():
    """
    Simulation/preview mode for your UI.
    Returns the full JSON breakdown used by the preview panel.
    """
    from core.engine import DiscountEngine
    from models.discount_rule import DiscountRule, LineItem, RuleType, ValueType, RuleConditions

    rules = [
        DiscountRule(
            id="r1", name="Party 12%",
            type=RuleType.PARTY, value_type=ValueType.PERCENT,
            value=Decimal("12"), gst_rate=Decimal("12"),
            conditions=RuleConditions(party_tags=["wholesale"]),
            priority=2, active=True,
        ),
        DiscountRule(
            id="r2", name="Frame Slab 10%",
            type=RuleType.OFFER_SLAB, value_type=ValueType.PERCENT,
            gst_rate=Decimal("12"),
            conditions=RuleConditions(product_cats=["frame"]),
            slab_config=[
                __import__("models.discount_rule", fromlist=["SlabTier"]).SlabTier(
                    min_qty=10, max_qty=None, discount_pct=Decimal("10")
                )
            ],
            priority=4, active=True,
        ),
    ]

    engine = DiscountEngine(rules)
    item   = LineItem(
        base_price  = Decimal("1200"),
        quantity    = 10,
        product_cat = "frame",
        party_tags  = ["wholesale"],
    )

    preview = engine.simulate(item)
    import json
    print(json.dumps(preview, indent=2, default=str))
    # Returns:
    # {
    #   "input": { "base_price": 1200, "quantity": 10, ... },
    #   "winner": {
    #     "base_price": 1200, "quantity": 10, "gross_amount": 12000,
    #     "rule_applied": "Party 12%", "discount_pct": 12.0,
    #     "discount_amount": 1440.0, "net_amount": 10560.0,
    #     "gst_rate": 12.0, "gst_amount": 1267.2, "final_amount": 11827.2
    #   },
    #   "all_evaluated": [ ... both rules shown, winner flagged ... ]
    # }

    return preview


# ─────────────────────────────────────────────
# HOW TO PLUG INTO YOUR BILLING PIPELINE
# ─────────────────────────────────────────────
"""
In your existing billing code, find where you calculate line total.
It probably looks like:

    BEFORE (your current code):
    ─────────────────────────────────────────
    line_total = unit_price * quantity
    gst_amount = line_total * gst_rate / 100
    final      = line_total + gst_amount

    AFTER (with discount engine):
    ─────────────────────────────────────────
    from core.engine import DiscountEngine
    from core.discount_adapter import DiscountAdapter
    from models.discount_rule import LineItem

    # Load once at billing session start (cache this!)
    adapter = DiscountAdapter(your_db_conn)
    engine  = DiscountEngine(adapter.get_active_rules())

    # Per line calculation
    item = LineItem(
        base_price  = unit_price,
        quantity    = quantity,
        product_cat = product_category,
        party_tags  = party.tags,
    )
    result = engine.calculate(item)

    # Use these values in your invoice
    line_total  = result.net_amount      # after discount
    gst_amount  = result.gst_amount      # on post-discount
    final       = result.final_amount
    rule_used   = result.rule_name       # for display

    # Optional: log it
    adapter.log_application(invoice_id, line_id, item, result)
    ─────────────────────────────────────────

That's it. Everything else in your pipeline stays the same.
"""

if __name__ == "__main__":
    print("=" * 60)
    print("EXAMPLE 1 — Minimal calculation")
    print("=" * 60)
    result = example_minimal()

    print("\n" + "=" * 60)
    print("EXAMPLE 3 — Simulate / Preview JSON")
    print("=" * 60)
    example_preview_json()
