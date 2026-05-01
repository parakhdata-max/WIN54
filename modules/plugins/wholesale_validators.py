"""
modules/plugins/wholesale_validators.py

Wholesale-Specific Validators — Severity-Aware
===============================================
Runs ONLY when mode == "WHOLESALE".

Severity guide for wholesale:
    margin_guard      → ERROR   (can't sell below cost)
    credit_limit      → WARNING (soft block, backoffice can approve)
    min_order_qty     → WARNING (MOQ advisory, not a hard block)
"""

from modules.core.validators_builtin import register_for_mode
from modules.core.validation_result import (
    ValidationIssue,
    error, warning, advisory,
)
from typing import List

MODE = "WHOLESALE"


# ============================================================================
# MARGIN GUARD — ERROR
# Cannot sell below cost price. Hard block.
# ctx["cost_map"] = {product_id: cost_price} — inject from OrderPipeline.
# ============================================================================

@register_for_mode(MODE)
def margin_guard(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Block sale at below-cost pricing. Skips gracefully if cost_map absent."""
    cost_map = ctx.get("cost_map") or {}
    if not cost_map:
        return []

    product_id = str(line.get("product_id", ""))
    cost       = cost_map.get(product_id)
    if cost is None:
        return []

    unit_price = line.get("unit_price", 0)
    if unit_price < cost:
        return [error(
            "BELOW_COST",
            f"unit price ₹{unit_price:.2f} is below cost ₹{cost:.2f}",
            line,
        )]
    return []


# ============================================================================
# CREDIT LIMIT GUARD — WARNING
# Soft-blocks when projected total exceeds credit limit.
# Hard credit check also runs in class-based FinancialValidator.
# ============================================================================

@register_for_mode(MODE)
def credit_limit_guard(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Warn when order total + outstanding exceeds party credit limit."""
    credit_limit = ctx.get("credit_limit") or 0
    outstanding  = ctx.get("outstanding")  or 0
    order_total  = ctx.get("order_total")  or 0

    if credit_limit <= 0:
        return []   # No limit set — handled separately by FinancialValidator

    projected = outstanding + order_total
    if projected > credit_limit:
        party = ctx.get("party", "Party")
        return [warning(
            "CREDIT_EXCEEDED",
            f"Projected total ₹{projected:,.2f} exceeds credit limit "
            f"₹{credit_limit:,.2f} for '{party}'",
            line,
        )]
    return []


# ============================================================================
# MINIMUM ORDER QTY — WARNING
# ctx["moq_map"] = {product_id: min_qty} — inject from product master.
# ============================================================================

@register_for_mode(MODE)
def minimum_order_qty(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Warn when qty is below minimum order quantity for wholesale."""
    moq_map = ctx.get("moq_map") or {}
    if not moq_map:
        return []

    product_id = str(line.get("product_id", ""))
    moq        = moq_map.get(product_id)
    if moq is None:
        return []

    qty = line.get("billing_qty", 0)
    if qty < moq:
        return [warning(
            "BELOW_MOQ",
            f"qty {qty} is below minimum order qty {moq}",
            line,
        )]
    return []
