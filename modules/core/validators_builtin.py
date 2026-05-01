"""
modules/core/validators_builtin.py

Built-in Global Validators — Severity-Aware
============================================
Validators now return list[ValidationIssue] instead of list[str].

THREE SEVERITY LEVELS:
    error()    → ERROR    — hard block, order REJECTED
    warning()  → WARNING  — soft alert, order proceeds with notification
    advisory() → ADVISORY — informational, always passes, logged only

REGISTRY:
    @register_global               → runs on every order, every mode
    @register_for_mode("RETAIL")   → runs only for RETAIL mode
    @register_for_mode("WHOLESALE")→ runs only for WHOLESALE mode

ADDING A RULE:
    @register_global
    def my_rule(line: dict, ctx: dict) -> list[ValidationIssue]:
        if something_wrong:
            return [error("MY_RULE", "description", line)]
        return []
"""

from typing import Callable, Dict, List
from modules.core.validation_result import (
    ValidationIssue,
    error, warning, advisory,
)

# ============================================================================
# REGISTRY TYPES
# ============================================================================

Validator = Callable[[dict, dict], List[ValidationIssue]]

_GLOBAL: List[Validator]            = []
_MODE:   Dict[str, List[Validator]] = {}


# ============================================================================
# REGISTRATION
# ============================================================================

def register_global(fn: Validator) -> Validator:
    """Register a validator that runs on every line of every order."""
    _GLOBAL.append(fn)
    return fn


def register_for_mode(mode: str, fn: Validator = None):
    """
    Register a validator for a specific mode only.

    Decorator with argument:
        @register_for_mode("WHOLESALE")
        def my_rule(line, ctx): ...

    Or direct call:
        register_for_mode("WHOLESALE", my_rule)
    """
    def decorator(func: Validator) -> Validator:
        _MODE.setdefault(mode.upper(), []).append(func)
        return func

    if fn is not None:
        return decorator(fn)
    return decorator


# ============================================================================
# RUNNER — returns structured issues, not flat strings
# ============================================================================

def run_line_validators(cart_lines: list, context: dict) -> List[ValidationIssue]:
    """
    Run all registered global + mode-specific validators.

    Called by finalize_engine AFTER schema normalization, BEFORE pricing.

    Args:
        cart_lines: Schema-normalized lines.
        context: {
            mode:         "RETAIL" | "WHOLESALE" | "LAB",
            party:        str,
            user:         str,
            order_total:  float,
            cost_map:     dict,    # {product_id: cost_price}  optional
            credit_limit: float,   # optional
            outstanding:  float,   # optional
            moq_map:      dict,    # {product_id: min_qty}  optional
            has_R:        bool,    # cart has right-eye line
            has_L:        bool,    # cart has left-eye line
            _first_line:  dict,    # first cart line (for cart-level checks)
        }

    Returns:
        list[ValidationIssue] — caller separates by .is_error / .is_warning / .is_advisory
    """
    issues: List[ValidationIssue] = []
    mode   = context.get("mode", "").upper()
    fns    = list(_GLOBAL) + list(_MODE.get(mode, []))

    for line in cart_lines:
        for fn in fns:
            try:
                result = fn(line, context)
                if result:
                    issues.extend(result)
            except Exception as exc:
                issues.append(error(
                    "VALIDATOR_CRASH",
                    f"Validator {fn.__name__} crashed: {exc}",
                    line,
                ))

    return issues


# ============================================================================
# BUILT-IN GLOBAL RULES
# ============================================================================

@register_global
def no_zero_qty(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Every billed line must have billing_qty > 0."""
    if line.get("billing_qty", 0) <= 0:
        return [error("NO_QTY", "billing_qty is zero or missing", line)]
    return []


@register_global
def no_negative_price(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Unit price must be non-negative (0 is allowed for samples / FOC lines)."""
    if line.get("unit_price", 0) < 0:
        return [error(
            "NEGATIVE_PRICE",
            f"unit_price is negative ({line.get('unit_price')})",
            line,
        )]
    return []


@register_global
def gst_range_check(line: dict, ctx: dict) -> List[ValidationIssue]:
    """GST percent must be in valid Indian slab range 0–28."""
    gst = line.get("gst_percent", 0)
    if not (0 <= gst <= 28):
        return [error(
            "INVALID_GST",
            f"GST {gst}% is outside valid range 0–28",
            line,
        )]
    return []


@register_global
def eye_side_valid(line: dict, ctx: dict) -> List[ValidationIssue]:
    """eye_side must be R, L, B, OTHER, or SERVICE (consultation/service lines)."""
    # eye_side may come as DB char(1) compact form or full form
    _eye_expand = {"O":"OTHER","S":"SERVICE"}
    side  = str(line.get("eye_side","OTHER") or "OTHER").upper().strip()
    side  = _eye_expand.get(side, side)  # S→SERVICE, O→OTHER
    valid = {"R", "L", "B", "OTHER", "SERVICE"}
    if side not in valid:
        return [error(
            "INVALID_EYE_SIDE",
            f"eye_side '{side}' is not valid (must be R/L/B/OTHER/SERVICE)",
            line,
        )]
    return []


@register_global
def batch_expiry_check(line: dict, ctx: dict) -> List[ValidationIssue]:
    """
    WARNING (not hard-block) if any allocated batch has already expired.
    Backoffice can override warnings — they don't REJECT the order.
    """
    import datetime
    issues = []
    today  = datetime.date.today()

    for batch in line.get("batch_allocation") or []:
        raw_exp = batch.get("expiry_date")
        if not raw_exp:
            continue
        try:
            exp = (
                raw_exp if isinstance(raw_exp, datetime.date)
                else datetime.date.fromisoformat(str(raw_exp)[:10])
            )
            if exp < today:
                issues.append(warning(
                    "EXPIRED_BATCH",
                    f"Batch {batch.get('batch_no', '?')} expired on {exp}",
                    line,
                ))
        except (ValueError, TypeError):
            pass  # Unparseable date — skip, don't crash

    return issues
