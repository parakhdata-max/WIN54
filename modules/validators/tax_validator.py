"""
Tax Pipeline Integrity Validator
=================================
Ensures every line entering the pricing pipeline has the
fields the tax engine needs. Catches data-starvation bugs
(like the backoffice gst_percent=0 issue) at validation time
instead of silently producing wrong tax amounts.

SEVERITY MODEL (matches existing validators):
    ERROR   → order REJECTED  — tax data so broken it cannot proceed
    WARNING → order CONFIRMED — missing non-critical fields, UI shows warning
    INFO    → always passes   — logged only

REGISTERED AS: "TAX" in registry.py
ENABLED IN:    config/validation_rules.json → "TAX": true
"""

from .base import BaseValidator, ValidationResult


class TaxDataIntegrityError(Exception):
    """Raised when a line is missing tax-critical fields."""
    pass


class TaxValidator(BaseValidator):

    name = "TAX"

    # Required on every line before tax engine runs.
    # NOTE: cart lines store 'total_price'; 'billing_total' is the post-normalizer
    # alias. We accept either via the _line_total() helper below.
    LINE_REQUIRED = ("product_id", "billing_qty")

    # Must be > 0 for tax to be meaningful
    LINE_NUMERIC  = ("billing_qty", "unit_price")

    def __init__(self, config):
        rules = config.get("tax", {})
        # If True → missing gst_percent is ERROR (blocks save)
        # If False → WARNING only (saves but flags)
        self.strict_gst = rules.get("strict_gst_required", True)
        # If True → a 0% line that is not marked GST-exempt is an ERROR.
        # Keep configurable so live rollout can start in warning mode while
        # product GST master cleanup is completed.
        self.block_zero_gst_unless_exempt = rules.get("block_zero_gst_unless_exempt", False)
        # Allowed GST slabs — empty list = accept any value
        self.valid_slabs = rules.get("valid_gst_slabs", [0, 5, 18])
        self._exempt_cache: dict = {}

    def _is_line_exempt(self, line: dict) -> bool:
        """Return True when a 0% line is explicitly GST-exempt.

        Prefer a line flag. If absent, look up products.is_gst_exempt by UUID.
        Any lookup problem fails closed.
        """
        v = line.get("is_gst_exempt")
        if v is not None:
            return bool(v)
        pid = line.get("product_id")
        if not pid:
            return False
        key = str(pid)
        if key in self._exempt_cache:
            return self._exempt_cache[key]
        exempt = False
        try:
            from modules.sql_adapter import as_uuid_or_none, run_query
            pid_u = as_uuid_or_none(pid)
            if pid_u:
                rows = run_query(
                    "SELECT COALESCE(is_gst_exempt, FALSE) AS ex FROM products WHERE id = %s::uuid",
                    (pid_u,),
                ) or []
                exempt = bool(rows[0].get("ex")) if rows else False
        except Exception:
            exempt = False
        self._exempt_cache[key] = exempt
        return exempt

    @staticmethod
    def _line_total(line: dict) -> float:
        """
        Cart lines use 'total_price'; order_normalizer renames it to
        'billing_total'. Accept either so validation works at both stages.
        """
        v = line.get("billing_total") or line.get("total_price") or 0
        return float(v)

    def validate(self, order) -> ValidationResult:

        order_type = (
            order.get("order_info", {}).get("order_type")
            or order.get("order_type", "RETAIL")
        ).upper()

        lines = order.get("lines", [])

        if not lines:
            return ValidationResult(
                rule="TAX_NO_LINES",
                passed=False,
                severity="ERROR",
                message="Order has no lines — cannot validate tax data"
            )

        missing_gst      = []   # lines where gst_percent is None / missing
        zero_gst         = []   # lines where gst_percent is 0 (may be legitimate)
        invalid_slab     = []   # lines where gst_percent not in valid_slabs
        missing_required = []   # lines missing product_id / billing_qty / billing_total
        zero_price_lines = []   # lines with qty > 0 but unit_price = 0

        for idx, line in enumerate(lines, 1):
            name = line.get("product_name") or f"Line {idx}"

            # ── Required fields present ──────────────────────────────────────
            for field in self.LINE_REQUIRED:
                if line.get(field) is None:
                    missing_required.append(f"{name}: missing '{field}'")

            # Check total price exists under either accepted key
            if self._line_total(line) == 0 and (line.get("billing_qty") or 0) > 0:
                # Warn but don't block — Partial/To-Order lines have total=0 legitimately
                pass  # caught by zero_price_lines check below

            # ── Numeric sanity ───────────────────────────────────────────────
            qty   = line.get("billing_qty", 0) or 0
            price = line.get("unit_price", 0) or 0
            total = self._line_total(line)   # handles both total_price & billing_total

            if qty > 0 and price == 0:
                zero_price_lines.append(f"{name}: qty={qty} but unit_price=0")

            # ── GST field check ──────────────────────────────────────────────
            gst = line.get("gst_percent")

            if gst is None:
                missing_gst.append(name)
            elif float(gst) == 0.0:
                if not self._is_line_exempt(line):
                    zero_gst.append(name)
            elif self.valid_slabs and float(gst) not in [float(s) for s in self.valid_slabs]:
                invalid_slab.append(f"{name}: {gst}%")

        # ── CRITICAL: required fields missing ────────────────────────────────
        if missing_required:
            return ValidationResult(
                rule="TAX_MISSING_REQUIRED_FIELDS",
                passed=False,
                severity="ERROR",
                message=f"Lines missing required fields: {'; '.join(missing_required)}",
                details={"missing": missing_required}
            )

        # ── CRITICAL: unit price zero ─────────────────────────────────────────
        if zero_price_lines:
            return ValidationResult(
                rule="TAX_ZERO_PRICE",
                passed=False,
                severity="ERROR",
                message=f"Lines with qty but no price: {'; '.join(zero_price_lines)}",
                details={"zero_price": zero_price_lines}
            )

        # ── gst_percent completely absent from line dict ──────────────────────
        if missing_gst:
            severity = "ERROR" if self.strict_gst else "WARNING"
            return ValidationResult(
                rule="TAX_GST_FIELD_MISSING",
                passed=False,
                severity=severity,
                message=(
                    f"gst_percent field absent on {len(missing_gst)} line(s): "
                    f"{', '.join(missing_gst)}. "
                    f"Data loader did not populate GST. "
                    f"Check fetch_orders_with_lines SQL."
                ),
                details={"missing_gst_field": missing_gst}
            )

        # ── gst_percent = 0 and NOT flagged exempt ───────────────────────────
        if zero_gst and self.strict_gst:
            severity = "ERROR" if self.block_zero_gst_unless_exempt else "WARNING"
            return ValidationResult(
                rule="TAX_GST_ZERO",
                passed=False,
                severity=severity,
                message=(
                    f"{len(zero_gst)} line(s) have 0% GST but are not marked GST-exempt: "
                    f"{', '.join(zero_gst)}. "
                    f"Set a GST slab on the product, or mark it GST-exempt if it genuinely has no GST."
                ),
                details={"zero_gst_lines": zero_gst}
            )

        # ── invalid slab ──────────────────────────────────────────────────────
        if invalid_slab:
            return ValidationResult(
                rule="TAX_INVALID_SLAB",
                passed=False,
                severity="WARNING",
                message=(
                    f"Non-standard GST slab on lines: {', '.join(invalid_slab)}. "
                    f"Valid slabs: {self.valid_slabs}"
                ),
                details={"invalid_slabs": invalid_slab}
            )

        # ── All good ──────────────────────────────────────────────────────────
        gst_values = [float(l.get("gst_percent", 0)) for l in lines]
        return ValidationResult(
            rule="TAX_OK",
            passed=True,
            severity="INFO",
            message=(
                f"Tax data integrity OK — {len(lines)} lines, "
                f"GST rates: {sorted(set(gst_values))}%, "
                f"order_type: {order_type}"
            ),
            details={"gst_rates_found": sorted(set(gst_values))}
        )
