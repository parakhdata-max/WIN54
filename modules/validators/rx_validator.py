"""
RX Validator (Config Driven, Hardened)
====================================
"""

from .base import BaseValidator, ValidationResult
from config.validation_config import VALIDATION_CONFIG


class RxValidator(BaseValidator):

    name = "RX"

    def validate(self, order):

        limits = VALIDATION_CONFIG["RX_LIMITS"]
        errors = []

        lines = order.get("lines")

        if not isinstance(lines, list):
            return ValidationResult(
                rule="INVALID_STRUCTURE",
                passed=False,
                severity="CRITICAL",
                message="Order lines must be a list"
            )

        for idx, line in enumerate(lines, 1):

            # Only RX-validate optical lens lines (eye_side R or L).
            # SERVICE lines (consultation fee, fitting), OTHER (frames,
            # accessories), and any line with no eye_side should be skipped —
            # they carry no SPH/CYL and will fail float() conversion.
            eye_side = str(line.get("eye_side", "")).upper()
            if eye_side not in ("R", "L"):
                continue

            product = line.get("product_name", f"Line {idx}")

            def _safe_float(v, default=0.0):
                """Convert power field safely — empty string / None / 'None' / 'nan' → default."""
                if v is None or str(v).strip() in ("", "None", "none", "nan", "NaN"):
                    return default
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return None  # signals unparseable non-empty value

            def _safe_int(v, default=0):
                if v is None or str(v).strip() in ("", "None", "none", "nan", "NaN"):
                    return default
                try:
                    return int(float(v))
                except (ValueError, TypeError):
                    return None

            # Contact lenses may store powers in lens_params instead of top-level fields
            lp = line.get("lens_params") or {}
            sph       = _safe_float(line.get("sph")       or lp.get("sph"))
            cyl       = _safe_float(line.get("cyl")       or lp.get("cyl"))
            axis      = _safe_int(line.get("axis")        or lp.get("axis"))
            add_power = _safe_float(line.get("add_power") or lp.get("add_power"))

            if None in (sph, cyl, axis, add_power):
                errors.append(f"{product}: Invalid RX format (unparseable power value)")
                continue

            if not limits["SPH_MIN"] <= sph <= limits["SPH_MAX"]:
                errors.append(f"{product}: SPH {sph:+.2f} out of range")

            if not limits["CYL_MIN"] <= cyl <= limits["CYL_MAX"]:
                errors.append(f"{product}: CYL {cyl:+.2f} out of range")

            if cyl != 0:
                if not limits["AXIS_MIN"] <= axis <= limits["AXIS_MAX"]:
                    errors.append(f"{product}: AXIS {axis} invalid")

            if not limits["ADD_MIN"] <= add_power <= limits["ADD_MAX"]:
                errors.append(f"{product}: ADD {add_power:+.2f} out of range")

        if errors:
            return ValidationResult(
                rule="INVALID_RX",
                passed=False,
                severity="CRITICAL",
                message=f"RX validation failed: {'; '.join(errors)}",
                details={"errors": errors}
            )

        return ValidationResult(
            rule="RX_OK",
            passed=True,
            severity="INFO",
            message="RX valid"
        )
