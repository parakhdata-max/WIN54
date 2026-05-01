"""
modules/validation_gateway.py
==============================
Central validation entry point.

Usage:
    from modules.validation_gateway import validate_before_submit, validate_lines

    result = validate_before_submit(order_data)
    if not result["is_valid"]:
        st.error(result["errors"])
"""
from modules.validators.engine import ValidationEngine
from modules.core.validators_builtin import run_line_validators


def validate_before_submit(order_data: dict) -> dict:
    """
    Run ORDER, PARTY, PRODUCT, RX, FINANCIAL, TAX validators.

    order_data must contain:
        order_id, party, lines (list with product_id, eye_side,
        billing_qty, unit_price, gst_percent, sph/cyl/axis/add_power)
    """
    engine = ValidationEngine()
    return engine.run_structured(order_data)


def validate_lines(cart_lines: list, context: dict) -> dict:
    """
    Run line-level validators (qty, price, GST range, eye_side, batch expiry).

    context: { mode: RETAIL|WHOLESALE, party: str, order_total: float }
    """
    issues   = run_line_validators(cart_lines, context)
    errors   = [i.message for i in issues if i.is_error]
    warnings = [i.message for i in issues if i.is_warning]
    return {
        "is_valid":     not errors,
        "has_warnings": bool(warnings),
        "errors":       errors,
        "warnings":     warnings,
        "results":      [i.to_dict() for i in issues],
    }
