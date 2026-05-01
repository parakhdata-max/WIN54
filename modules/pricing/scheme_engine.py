# modules/pricing/scheme_engine.py
"""
Scheme Engine — applies BOGO and slab schemes via the original engine.py.
Called by pricing_pipeline.py as the "schemes" stage.

Delegates entirely to engine.py (DiscountEngine) which already handles:
  - BOGO: type=offer_bogo, value_type=bogo, bogo_buy, bogo_get
  - Slab: type=offer_slab, slab_config=[{min_qty, max_qty, discount_pct}]

This is NOT called from punching flows — those call discount_engine.py
directly. This exists for pricing_pipeline.py and any future pipeline callers.
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)


def apply_schemes(order: dict) -> dict:
    """
    Apply BOGO and quantity-slab schemes to order lines.

    Reads from discount_rules where type IN ('offer_bogo', 'offer_slab').
    Stamps each line with discount_percent, discount_amount, discount_rule.
    Returns the order dict (same reference, mutated in place).

    NOTE: This function is called from pricing_pipeline.py only.
    Punching flows (retail/wholesale/bulk) call apply_discounts() directly,
    which routes through engine.calculate() → pick_stackable_rules() and
    handles stacking natively.

    This function handles the pricing_pipeline stacking path:
    apply_discounts() already ran and may have stamped a base discount.
    We must NOT skip lines that already have a discount — instead we check
    if the best rule for this line is a stackable slab/BOGO offer, and if
    so, add it on top of the existing base discount (compounding correctly).
    """
    lines     = order.get("lines") or []
    party_id  = str(order.get("party_id") or "").strip()
    order_type = str(order.get("order_type") or "wholesale")

    if not lines:
        order["scheme_applied"] = []
        return order

    applied = []
    try:
        from modules.pricing.discount_engine import _load_engine, _build_line_item

        channel = "retail" if order_type.upper() == "RETAIL" else "wholesale"
        engine  = _load_engine(channel=channel)

        for line in lines:
            unit_price = float(line.get("unit_price") or 0)
            if unit_price <= 0:
                continue

            item   = _build_line_item(line, party_id, order_type)
            result = engine.calculate(item)

            rule = result.rule_applied
            if not rule:
                continue

            rtype = rule.type.value

            # ── Stacking: add slab/BOGO on top of existing base discount ──────
            # If the engine's best result is a stackable offer (slab/BOGO/promo),
            # apply it cumulatively on the net-after-base-discount price.
            # This mirrors the pick_stackable_rules() logic in engine.py.
            if rule.stackable and rtype in ("offer_bogo", "offer_slab", "promo_code"):
                qty         = int(line.get("billing_qty") or line.get("quantity") or 1)
                existing_da = float(line.get("discount_amount") or 0)
                # Compute offer discount on net-after-existing-discount
                net_after_base = max(0.0, unit_price * qty - existing_da)
                offer_disc_pct = float(result.discount_pct or 0)
                offer_disc_amt = round(net_after_base * offer_disc_pct / 100, 2)

                if offer_disc_amt > 0:
                    new_disc_amt = round(existing_da + offer_disc_amt, 2)
                    new_disc_pct = round(new_disc_amt / (unit_price * qty) * 100, 4) if unit_price * qty > 0 else 0.0

                    # Append to existing rule name
                    existing_rule = str(line.get("discount_rule") or "")
                    new_rule_name = f"{existing_rule} + {rule.name}".lstrip(" + ")

                    line["discount_amount"]  = new_disc_amt
                    line["discount_percent"] = new_disc_pct
                    line["discount_rule"]    = new_rule_name
                    line["billing_total"]    = round(unit_price * qty - new_disc_amt, 2)

                    # Append to applied_rule_ids
                    existing_ids = str(line.get("applied_rule_ids") or "")
                    new_id       = str(rule.id)
                    line["applied_rule_ids"] = f"{existing_ids},{new_id}".strip(",")

                    applied.append({
                        "rule":            rule.name,
                        "type":            rtype,
                        "discount_pct":    offer_disc_pct,
                        "discount_amount": offer_disc_amt,
                        "stacked_on":      existing_rule,
                        "line_product":    line.get("product_name", ""),
                    })

            # ── Non-stackable: only apply if no discount yet (original behaviour) ──
            elif not rule.stackable and rtype in ("offer_bogo", "offer_slab"):
                if float(line.get("discount_percent") or 0) > 0:
                    continue   # base rule already won — don't replace

                disc_pct = float(result.discount_pct or 0)
                disc_amt = float(result.discount_amount or 0)
                qty      = int(line.get("billing_qty") or line.get("quantity") or 1)

                line["discount_percent"] = round(disc_pct, 4)
                line["discount_amount"]  = round(disc_amt, 2)
                line["discount_rule"]    = result.rule_name
                line["billing_total"]    = round(unit_price * qty - disc_amt, 2)

                applied.append({
                    "rule":           result.rule_name,
                    "type":           rtype,
                    "discount_pct":   disc_pct,
                    "discount_amount": disc_amt,
                    "line_product":   line.get("product_name", ""),
                })

    except Exception as e:
        log.warning(f"[scheme_engine] failed: {e}")

    order["scheme_applied"] = applied
    return order
