"""
modules/core/pricing_pipeline.py

Pricing Pipeline — Full Cart Orchestrator
==========================================
Chains all pricing stages in the correct order and returns a PricingTrace
for the audit log.

PIPELINE ORDER (per line):
    1. apply_pricing_line()  — weighted batch price → unit_price, total_price
    2. apply_discounts()     — party/product discounts (stub, ready to wire)
    3. apply_schemes()       — BOGOs, slabs, promotions (stub, ready to wire)

PIPELINE ORDER (full cart):
    4. apply_taxes()         — GST on order total (stub, ready to wire)

IDEMPOTENCY:
    apply_pricing_line() is already idempotent (checks pricing_applied_at).
    The pipeline respects this — lines already priced are passed through.

AUDIT TRACE:
    run_pricing() returns PricingTrace containing:
        - per-line pricing result
        - discounts applied
        - schemes applied
        - tax calculation
        - any errors/warnings
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# TRACE STRUCTURES
# ============================================================================

@dataclass
class LinePricingTrace:
    """Pricing trace for a single order line."""
    line_id:          str
    product_name:     str
    billing_qty:      int
    unit_price:       float
    total_price:      float
    pricing_source:   str            # "batch_weighted" | "manual" | "skipped"
    discount_applied: Optional[str]  # e.g. "WHOLESALE_15%"
    scheme_applied:   Optional[str]  # e.g. "BOGO"
    gst_percent:      float
    error:            Optional[str]  # non-None if pricing failed for this line

    def to_dict(self) -> dict:
        return {
            "line_id":          self.line_id,
            "product_name":     self.product_name,
            "billing_qty":      self.billing_qty,
            "unit_price":       self.unit_price,
            "total_price":      self.total_price,
            "pricing_source":   self.pricing_source,
            "discount_applied": self.discount_applied,
            "scheme_applied":   self.scheme_applied,
            "gst_percent":      self.gst_percent,
            "error":            self.error,
        }


@dataclass
class PricingTrace:
    """Full cart pricing trace — written to audit log."""
    order_id:       str
    mode:           str
    lines:          List[LinePricingTrace]  = field(default_factory=list)
    subtotal:       float                   = 0.0
    discount_total: float                   = 0.0
    net_value:      float                   = 0.0
    tax_amount:     float                   = 0.0
    final_value:    float                   = 0.0
    errors:         List[str]               = field(default_factory=list)
    warnings:       List[str]               = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def to_dict(self) -> dict:
        return {
            "order_id":       self.order_id,
            "mode":           self.mode,
            "lines":          [l.to_dict() for l in self.lines],
            "subtotal":       self.subtotal,
            "discount_total": self.discount_total,
            "net_value":      self.net_value,
            "tax_amount":     self.tax_amount,
            "final_value":    self.final_value,
            "errors":         self.errors,
            "warnings":       self.warnings,
        }


# ============================================================================
# LAZY IMPORTS
# ============================================================================

def _apply_pricing_line():
    from modules.pricing.pricing_engine import apply_pricing_line
    return apply_pricing_line

def _apply_discounts():
    from modules.pricing.discount_engine import apply_discounts
    return apply_discounts

def _apply_schemes():
    from modules.pricing.scheme_engine import apply_schemes
    return apply_schemes

def _apply_taxes():
    from modules.pricing.tax_engine import apply_taxes
    return apply_taxes


# ============================================================================
# MAIN ENTRY
# ============================================================================

def run_pricing(
    cart_lines: list,
    order_info: dict,
    skip_pricing: bool = False,
) -> tuple[list, PricingTrace]:
    """
    Run the full pricing pipeline on a cart.

    Args:
        cart_lines:   Schema-normalized lines (from normalize_cart).
        order_info:   Order header — mode, party, etc.
        skip_pricing: If True, skip line pricing (used in tests or re-submissions).

    Returns:
        (priced_lines, PricingTrace)
        priced_lines: cart_lines with unit_price, total_price, pricing metadata set.
        trace:        Full audit record of what happened.
    """
    order_id = order_info.get("provisional_order_id") or "PENDING"
    mode     = order_info.get("order_type", "RETAIL")

    trace = PricingTrace(order_id=order_id, mode=mode)

    # ── Step 1: Per-line pricing ───────────────────────────────────────────────
    if not skip_pricing:
        apply_line = _apply_pricing_line()

        for i, line in enumerate(cart_lines):
            line_trace = _price_single_line(line, i, apply_line)
            trace.lines.append(line_trace)

            if line_trace.error:
                # Non-fatal: provisional lines may not have batches yet
                trace.warnings.append(line_trace.error)
            else:
                cart_lines[i]["unit_price"]  = line_trace.unit_price
                cart_lines[i]["total_price"] = line_trace.total_price

    # ── Step 2: Subtotal ──────────────────────────────────────────────────────
    trace.subtotal = round(sum(l.get("total_price", 0) for l in cart_lines), 2)

    # ── Step 3: Discounts ─────────────────────────────────────────────────────
    order_for_discount = {
        "total_value": trace.subtotal,
        "lines": cart_lines,
        "party_type": mode,
        "order_info": order_info,
    }
    try:
        apply_disc  = _apply_discounts()
        discounted  = apply_disc(order_for_discount)
        trace.discount_total = discounted.get("discount_amount", 0.0)
        trace.net_value      = discounted.get("net_value", trace.subtotal)
    except Exception as exc:
        logger.warning("Discount engine failed: %s — skipping discounts", exc)
        trace.net_value = trace.subtotal
        trace.warnings.append(f"Discount engine unavailable: {exc}")

    # ── Step 4: Schemes ───────────────────────────────────────────────────────
    order_for_scheme = {
        "net_value": trace.net_value,
        "lines": cart_lines,
        "order_info": order_info,
    }
    try:
        apply_sch  = _apply_schemes()
        schemed    = apply_sch(order_for_scheme)
        schemes    = schemed.get("scheme_applied", [])
        if schemes:
            for lt in trace.lines:
                lt.scheme_applied = ", ".join(str(s) for s in schemes) or None
    except Exception as exc:
        logger.warning("Scheme engine failed: %s — skipping schemes", exc)
        trace.warnings.append(f"Scheme engine unavailable: {exc}")

    # ── Step 5: Tax ───────────────────────────────────────────────────────────
    order_for_tax = {
        "net_value": trace.net_value,
        "total_value": trace.subtotal,
        "lines": cart_lines,
        "order_info": order_info,
    }
    try:
        apply_tax       = _apply_taxes()
        taxed           = apply_tax(order_for_tax)
        trace.tax_amount  = taxed.get("tax_amount", 0.0)
        trace.final_value = taxed.get("final_value", trace.net_value)
    except Exception as exc:
        logger.warning("Tax engine failed: %s — skipping tax", exc)
        trace.final_value = trace.net_value
        trace.warnings.append(f"Tax engine unavailable: {exc}")

    # Stamp final value onto order_info for downstream use
    order_info["subtotal"]    = trace.subtotal
    order_info["discount"]    = trace.discount_total
    order_info["net_value"]   = trace.net_value
    order_info["tax_amount"]  = trace.tax_amount
    order_info["final_value"] = trace.final_value

    return cart_lines, trace


# ============================================================================
# HELPERS
# ============================================================================

def _price_single_line(
    line: dict,
    idx:  int,
    apply_fn,
) -> LinePricingTrace:
    """Apply pricing to one line and return a trace record."""
    line_id  = line.get("line_id", f"line_{idx}")
    name     = line.get("product_name", f"Line {idx + 1}")

    # Already priced — pass through (idempotent)
    if line.get("pricing_applied_at"):
        return LinePricingTrace(
            line_id          = line_id,
            product_name     = name,
            billing_qty      = line.get("billing_qty", 0),
            unit_price       = line.get("unit_price", 0.0),
            total_price      = line.get("total_price", 0.0),
            pricing_source   = line.get("pricing_source", "pre-applied"),
            discount_applied = None,
            scheme_applied   = None,
            gst_percent      = line.get("gst_percent", 18.0),
            error            = None,
        )

    try:
        priced = apply_fn(line)
        return LinePricingTrace(
            line_id          = line_id,
            product_name     = name,
            billing_qty      = priced.get("billing_qty", 0),
            unit_price       = priced.get("unit_price", 0.0),
            total_price      = priced.get("total_price", 0.0),
            pricing_source   = priced.get("pricing_source", "batch_weighted"),
            discount_applied = None,
            scheme_applied   = None,
            gst_percent      = priced.get("gst_percent", 18.0),
            error            = None,
        )
    except ValueError as exc:
        # Missing batch allocation — expected for provisional lines
        return LinePricingTrace(
            line_id          = line_id,
            product_name     = name,
            billing_qty      = line.get("billing_qty", 0),
            unit_price       = line.get("unit_price", 0.0),
            total_price      = line.get("total_price", 0.0),
            pricing_source   = "skipped",
            discount_applied = None,
            scheme_applied   = None,
            gst_percent      = line.get("gst_percent", 18.0),
            error            = f"Pricing skipped: {exc}",
        )
    except Exception as exc:
        logger.exception("Pricing failed for line %s", line_id)
        return LinePricingTrace(
            line_id          = line_id,
            product_name     = name,
            billing_qty      = line.get("billing_qty", 0),
            unit_price       = 0.0,
            total_price      = 0.0,
            pricing_source   = "error",
            discount_applied = None,
            scheme_applied   = None,
            gst_percent      = line.get("gst_percent", 18.0),
            error            = f"Pricing error: {exc}",
        )
