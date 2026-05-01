"""
modules/core/finalize_engine.py

Central Finalize Engine
=======================
The ONLY place where validation and pricing run.
Plugins collect data → engine orchestrates → finalize_engine validates + prices.

GOLDEN RULE:
    If it affects money or correctness → lives here.
    If it affects workflow / UI → lives in plugin.

PIPELINE (in order):
    1. Schema normalization + migration  — order_schema.normalize_cart()
    2. Line validators (functional)      — validators_builtin.run_line_validators()
    3. Order validators (class-based)    — validators.engine.ValidationEngine
    4. Pricing pipeline                  — pricing_pipeline.run_pricing()
    5. Schema version stamp              — order_schema.attach_schema_version()
    6. Audit log                         — audit_log.AuditLog.record()
    7. Return CONFIRMED | REJECTED

SEVERITY MODEL:
    ERROR    → order REJECTED  (from any validator)
    WARNING  → order CONFIRMED (caller displays warnings)
    ADVISORY → order CONFIRMED (logged only, not shown in UI unless debug)
"""

import logging
from typing import List

logger = logging.getLogger(__name__)

# Load mode-specific validators at import time (registration side-effect)
try:
    import modules.core.validators_builtin           # noqa: F401 — registers global rules
    import modules.plugins.wholesale_validators      # noqa: F401 — registers WHOLESALE rules
    import modules.plugins.retail_validators         # noqa: F401 — registers RETAIL rules
except ImportError as _e:
    logger.warning("Some validator modules could not be loaded: %s", _e)


# ============================================================================
# LAZY IMPORT CACHE
# ============================================================================

_VALIDATION_ENGINE = None
_AUDIT_LOG         = None


def _get_validation_engine():
    global _VALIDATION_ENGINE
    if _VALIDATION_ENGINE is None:
        from modules.validators.engine import ValidationEngine
        _VALIDATION_ENGINE = ValidationEngine()
    return _VALIDATION_ENGINE


def _get_audit_log():
    global _AUDIT_LOG
    if _AUDIT_LOG is None:
        from modules.core.audit_log import AuditLog
        _AUDIT_LOG = AuditLog()
    return _AUDIT_LOG


# ============================================================================
# MAIN ENTRY
# ============================================================================

def run_finalize(
    cart_lines:   list,
    order_info:   dict,
    user_name:    str  = "System",
    skip_pricing: bool = False,
) -> dict:
    """
    Run the full finalize pipeline on a cart.

    Args:
        cart_lines:   Raw cart from session state (any schema version).
        order_info:   Order header dict (party, order_type, etc.).
        user_name:    Who is submitting — stamped in audit log.
        skip_pricing: Set True in tests or when pricing already applied.

    Returns:
        {
            "status":     "CONFIRMED" | "REJECTED",
            "order_no":   str | None,
            "lines":      list[dict],   # normalized + migrated + priced
            "order_info": dict,         # header with schema_version stamped
            "errors":     list[str],    # empty if CONFIRMED
            "warnings":   list[str],    # non-blocking — always show to user
            "advisories": list[str],    # informational — log / debug only
        }
    """
    from modules.core.validation_result import ValidationIssue
    all_issues: List[ValidationIssue] = []

    # ── Step 1: Schema normalization + migration ───────────────────────────────
    from modules.core.order_schema import normalize_cart, attach_schema_version
    try:
        cart_lines = normalize_cart(cart_lines)   # migrate_line() runs inside
    except Exception as exc:
        logger.exception("Schema normalization failed")
        _write_audit("FINALIZE", "ERR-SCHEMA", order_info, user_name,
                     "REJECTED", [], None, {"error": str(exc)})
        return _rejected([f"Schema normalization error: {exc}"])

    if not cart_lines:
        return _rejected(["Cart is empty — no lines to finalize."])

    # ── Step 2: Line validators (functional, severity-aware) ──────────────────
    from modules.core.validators_builtin import run_line_validators

    # Build eye-side context for retail validators
    has_r = any(l.get("eye_side") == "R" for l in cart_lines)
    has_l = any(l.get("eye_side") == "L" for l in cart_lines)

    line_context = {
        "mode":         order_info.get("order_type", "RETAIL").upper(),
        "party":        order_info.get("party") or order_info.get("patient_name", ""),
        "user":         user_name,
        "order_total":  sum(l.get("total_price", 0) for l in cart_lines),
        "cost_map":     order_info.get("cost_map", {}),
        "credit_limit": order_info.get("credit_limit", 0),
        "outstanding":  order_info.get("outstanding", 0),
        "moq_map":      order_info.get("moq_map", {}),
        "has_R":        has_r,
        "has_L":        has_l,
        "_first_line":  cart_lines[0] if cart_lines else None,
    }

    line_issues = run_line_validators(cart_lines, line_context)
    all_issues.extend(line_issues)

    # ── Step 3: Order validators (class-based ValidationEngine) ───────────────
    order_data = _build_order_data(cart_lines, order_info, user_name)
    try:
        engine  = _get_validation_engine()
        results = engine.run(order_data)
        # engine.run() returns a list of dicts (each validator calls .to_dict()).
        # Use .get() — NOT attribute access — to read fields safely.
        for r in results:
            if isinstance(r, dict):
                _passed   = bool(r.get("passed", True))
                _severity = str(r.get("severity", "")).upper()
                _message  = str(r.get("message", r.get("rule", "Unknown validation error")))
                _rule     = str(r.get("rule", "UNKNOWN_RULE"))
            else:
                # Fallback: object with attributes (should not occur, but safe)
                _passed   = bool(getattr(r, "passed", True))
                _severity = str(getattr(r, "severity", "")).upper()
                _message  = str(getattr(r, "message", "Unknown validation error"))
                _rule     = str(getattr(r, "rule", "UNKNOWN_RULE"))
            if not _passed:
                from modules.core.validation_result import error as mk_error, warning as mk_warn
                if _severity in ("CRITICAL", "ERROR"):
                    all_issues.append(mk_error(_rule, _message))
                elif _severity == "WARNING":
                    all_issues.append(mk_warn(_rule, _message))
    except Exception as exc:
        logger.exception("Class-based ValidationEngine failed")
        from modules.core.validation_result import error as mk_error
        all_issues.append(mk_error("VALIDATOR_ENGINE_CRASH", f"Validation engine error: {exc}"))

    # ── Separate by severity ──────────────────────────────────────────────────
    errors     = [i for i in all_issues if i.is_error]
    warnings   = [i for i in all_issues if i.is_warning]
    advisories = [i for i in all_issues if i.is_advisory]

    if errors:
        _write_audit("FINALIZE", "REJECTED", order_info, user_name,
                     "REJECTED", all_issues, None,
                     {"_line_count": len(cart_lines)})
        return _rejected(
            errors   = [str(e) for e in errors],
            warnings = [str(w) for w in warnings],
            advisories=[str(a) for a in advisories],
        )

    # ── Step 4: Pricing pipeline ──────────────────────────────────────────────
    from modules.core.pricing_pipeline import run_pricing
    pricing_trace = None
    try:
        cart_lines, pricing_trace = run_pricing(
            cart_lines, order_info, skip_pricing=skip_pricing
        )
        # Pricing warnings → surface to caller
        for pw in pricing_trace.warnings:
            from modules.core.validation_result import warning as mk_warn
            warnings.append(mk_warn("PRICING_WARN", pw))

        # ── Shadow mode ───────────────────────────────────────────────────────
        from modules.pricing.shadow_mode import run_shadow
        _channel = order_info.get("order_type", "retail").lower()
        run_shadow(cart_lines, order_info, pricing_trace, channel=_channel)

    except Exception as exc:
        logger.exception("Pricing pipeline failed")
        from modules.core.validation_result import error as mk_error
        errors.append(mk_error("PRICING_CRASH", f"Pricing pipeline error: {exc}"))

    if errors:
        _write_audit("FINALIZE", "REJECTED-PRICING", order_info, user_name,
                     "REJECTED", all_issues, pricing_trace,
                     {"_line_count": len(cart_lines)})
        return _rejected([str(e) for e in errors], [str(w) for w in warnings])

    # ── Step 5: Stamp schema version + metadata ───────────────────────────────
    order_info = attach_schema_version(order_info)
    order_info["submitted_by"]  = user_name
    order_info["_line_count"]   = len(cart_lines)

    # ── Step 6: Audit log ─────────────────────────────────────────────────────
    order_no = _generate_order_no(order_info.get("order_type", "ORD"))
    order_info["order_no"] = order_no

    _write_audit("FINALIZE", order_no, order_info, user_name,
                 "CONFIRMED", all_issues, pricing_trace,
                 {"_line_count": len(cart_lines)})

    # ── Step 7: Safety normalizer — prevent None propagation to DB ───────────
    # Runs AFTER pricing so it never masks pricing errors.
    # Sets hard defaults only — never overwrites values already set by pricing.
    for _l in cart_lines:
        # billing_qty: try every known alias before defaulting to 0
        if not _l.get("billing_qty"):
            _l["billing_qty"] = (
                _l.get("qty") or _l.get("order_qty") or
                _l.get("punch_qty") or _l.get("requested_qty") or 0
            )
        # Ensure these are never None (DB NOT NULL columns)
        _l.setdefault("unit_price",       0.0)
        _l.setdefault("total_price",      0.0)
        _l.setdefault("gst_percent",      0.0)
        _l.setdefault("gst_amount",       0.0)
        _l.setdefault("discount_percent", 0.0)
        _l.setdefault("discount_amount",  0.0)

    # ── Step 8: Return ────────────────────────────────────────────────────────
    return {
        "status":     "UNDER_REVIEW",  # awaits backoffice
        "order_no":   order_no,
        "lines":      cart_lines,
        "order_info": order_info,
        "errors":     [],
        "warnings":   [str(w) for w in warnings],
        "advisories": [str(a) for a in advisories],
    }


# ============================================================================
# HELPERS
# ============================================================================

def _build_order_data(cart_lines: list, order_info: dict, user_name: str) -> dict:
    """Bridge finalize format → ValidationEngine.run() expected shape."""
    return {
        "order_id":    order_info.get("provisional_order_id") or "PENDING",
        "party_name":  order_info.get("party") or order_info.get("patient_name", ""),
        "lines":       cart_lines,
        "party_type":  order_info.get("order_type", "RETAIL"),
        "credit_limit":order_info.get("credit_limit", 0),
        "outstanding": order_info.get("outstanding", 0),
        "party":       order_info.get("party") or order_info.get("patient_name", ""),
        "order_value": sum(l.get("total_price", 0) for l in cart_lines),
        "submitted_by":user_name,
        "order_info":  order_info,
    }


def _write_audit(event, order_id, order_info, user_name,
                 outcome, issues, pricing_trace, extra):
    """Fire-and-forget audit write — never raises."""
    try:
        _get_audit_log().record(
            event=event, order_id=str(order_id),
            order_info=order_info, user_name=user_name,
            outcome=outcome, issues=issues,
            pricing_trace=pricing_trace, extra=extra or {},
        )
    except Exception as exc:
        logger.error("Audit write failed (non-fatal): %s", exc)


def _rejected(errors: list, warnings: list = None, advisories: list = None) -> dict:
    return {
        "status":     "REJECTED",
        "order_no":   None,
        "lines":      [],
        "order_info": {},
        "errors":     errors,
        "warnings":   warnings or [],
        "advisories": advisories or [],
    }


def _generate_order_no(prefix: str) -> str:
    import datetime, uuid
    ts  = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    uid = str(uuid.uuid4())[:6].upper()
    return f"{prefix}-{ts}-{uid}"
