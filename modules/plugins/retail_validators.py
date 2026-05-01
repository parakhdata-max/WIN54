"""
modules/plugins/retail_validators.py

Retail-Specific Validators — Severity-Aware
============================================
Runs ONLY when mode == "RETAIL".

Severity guide for retail:
    patient_required    → ERROR    (can't submit without patient)
    optical_power_guard → ERROR    (lens line needs SPH)
    paired_eye_advisory → WARNING  (single-eye orders are valid but unusual)
    lens_params_advisory→ ADVISORY (informational — job card may be incomplete)
"""

from modules.core.validators_builtin import register_for_mode
from modules.core.validation_result import (
    ValidationIssue,
    error, warning, advisory,
)
from typing import List

MODE = "RETAIL"


# ============================================================================
# PATIENT REQUIRED — ERROR
# ============================================================================

@register_for_mode(MODE)
def patient_required(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Retail orders must have a patient name in context."""
    if not ctx.get("party"):
        return [error(
            "NO_PATIENT",
            "Patient name is missing — select patient before submitting",
            line,
        )]
    return []


# ============================================================================
# OPTICAL POWER GUARD — ERROR
# Lens lines (R/L) must have SPH explicitly set.
# 0.0 (plano) is valid — None means it was never entered.
# ============================================================================

@register_for_mode(MODE)
def optical_power_guard(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Lens lines on R/L eye must have SPH set (0.0 plano is valid, None is not)."""
    eye = line.get("eye_side", "OTHER")
    sph = line.get("sph")
    if eye in ("R", "L") and sph is None:
        return [error(
            "MISSING_SPH",
            f"SPH is not entered — use 0.00 for plano lenses",
            line,
        )]
    return []


# ============================================================================
# PAIRED EYE ADVISORY — WARNING
# Only fires once (on first line) using ctx["_first_line"].
# Single-eye orders are valid — this is a soft reminder, not a block.
# ============================================================================

@register_for_mode(MODE)
def paired_eye_advisory(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Warn (not block) when cart contains lens for only one eye."""
    # Only fire once per cart — on the first line
    if line is not ctx.get("_first_line"):
        return []

    has_r = ctx.get("has_R", False)
    has_l = ctx.get("has_L", False)

    if (has_r or has_l) and not (has_r and has_l):
        missing = "LEFT" if has_r else "RIGHT"
        return [warning(
            "SINGLE_EYE",
            f"No lens for {missing} eye — confirm this is intentional",
            line,
        )]
    return []


# ============================================================================
# LENS PARAMS ADVISORY — ADVISORY
# Informational only. Job card may be legitimately empty for accessories.
# ============================================================================

@register_for_mode(MODE)
def lens_params_advisory(line: dict, ctx: dict) -> List[ValidationIssue]:
    """Advisory: lens lines with no parameters may have an incomplete job card."""
    eye         = line.get("eye_side", "OTHER")
    lens_params = line.get("lens_params") or {}

    if eye not in ("R", "L"):
        return []

    filled = {k: v for k, v in lens_params.items() if v}
    if not filled:
        return [advisory(
            "EMPTY_LENS_PARAMS",
            "Lens parameters are empty — frame type / thickness should be filled "
            "for the job card",
            line,
        )]
    return []
