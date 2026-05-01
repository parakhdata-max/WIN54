"""
Financial Validator (Credit + Pricing)
=====================================

• Credit control for WHOLESALE
• Soft block with admin approval
• Pricing validation
• GST validation
• Retail orders are never blocked
"""

from .base import BaseValidator, ValidationResult


class FinancialValidator(BaseValidator):

    name = "FINANCIAL"


    def __init__(self, config):

        rules = config.get("financial", {})

        self.warning_ratio = rules.get("warning_ratio", 0.9)
        self.block_ratio = rules.get("block_ratio", 1.0)


    def validate(self, order):

        # ============================
        # PARTY TYPE
        # ============================

        party_type = (
            order.get("party_type")
            or order.get("role_type")
            or "RETAIL"
        ).upper()


        # ============================
        # CREDIT CONTROL (WHOLESALE)
        # ============================

        if party_type == "WHOLESALE":

            credit_limit = order.get("credit_limit", 0)
            outstanding = order.get("outstanding", 0)


            # No credit setup → admin review
            if credit_limit <= 0:

                order["requires_admin_approval"] = True

                order["admin_note"] = (
                    "⚠️ No credit limit set. "
                    "Order requires administrator approval."
                )

                for line in order.get("lines", []):
                    line["admin_note"] = order["admin_note"]


                return ValidationResult(
                    rule="NO_CREDIT_LIMIT",
                    passed=False,
                    severity="WARNING",
                    message="No credit limit. Admin approval required"
                )


            ratio = outstanding / credit_limit


            # Credit exceeded → HOLD
            if ratio >= self.block_ratio:

                order["requires_admin_approval"] = True

                order["admin_note"] = (
                    "⚠️ Credit limit exceeded. "
                    "Order accepted but requires administrator approval."
                )

                for line in order.get("lines", []):
                    line["admin_note"] = order["admin_note"]


                return ValidationResult(
                    rule="CREDIT_EXCEEDED",
                    passed=False,
                    severity="WARNING",
                    message="Credit limit exceeded. Admin approval required"
                )


            # Warning level
            if ratio >= self.warning_ratio:

                return ValidationResult(
                    rule="CREDIT_WARNING",
                    passed=False,
                    severity="WARNING",
                    message=f"{int(ratio * 100)}% credit used"
                )


        # ============================
        # PRICING VALIDATION
        # ============================

        lines = order.get("lines", [])


        if not isinstance(lines, list):

            return ValidationResult(
                rule="INVALID_LINES",
                passed=False,
                severity="CRITICAL",
                message="Invalid order lines"
            )


        for idx, line in enumerate(lines, 1):

            qty = line.get("billing_qty", 0)
            unit_price = line.get("unit_price", 0)
            total_price = line.get("total_price", 0)
            gst = line.get("gst_percent", 0)


            # Quantity
            if qty <= 0:

                return ValidationResult(
                    rule="INVALID_QTY",
                    passed=False,
                    severity="CRITICAL",
                    message=f"Line {idx}: Invalid quantity"
                )


            # Unit price
            if unit_price <= 0:

                return ValidationResult(
                    rule="INVALID_PRICE",
                    passed=False,
                    severity="CRITICAL",
                    message=f"Line {idx}: Invalid unit price"
                )


            # Total price — use billing_total if present (BOX-normalised value)
            # unit_price is per-PCS, billing_total already accounts for BOX/PCS
            # conversion. Only check if both values are present and non-zero.
            billing_total = line.get("billing_total") or total_price
            if billing_total and unit_price and qty:
                expected = round(unit_price * qty, 2)
                actual   = round(float(billing_total), 2)
                # Allow 1% tolerance for floating point + BOX rounding
                tolerance = max(0.05, round(expected * 0.01, 2))
                if abs(actual - expected) > tolerance:
                    return ValidationResult(
                        rule="PRICE_MISMATCH",
                        passed=False,
                        severity="WARNING",   # WARNING not CRITICAL — BOX products legitimately differ
                        message=(
                            f"Line {idx}: billing_total {actual} differs from "
                            f"unit_price×qty {expected} by more than tolerance {tolerance}"
                        )
                    )


            # GST
            if gst < 0:

                return ValidationResult(
                    rule="INVALID_GST",
                    passed=False,
                    severity="CRITICAL",
                    message=f"Line {idx}: Invalid GST"
                )


        # ============================
        # PASSED
        # ============================

        return ValidationResult(
            rule="FINANCIAL_OK",
            passed=True,
            severity="INFO",
            message="Financials and pricing valid"
        )
