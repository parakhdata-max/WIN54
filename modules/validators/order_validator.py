"""
Order Validator (Engine Compatible)
==================================

• Validates basic order structure
• Ensures required fields exist
• Validates quantities
• Validates batch allocation
• Prevents malformed orders
"""

from typing import Dict

from .base import BaseValidator, ValidationResult


class OrderValidator(BaseValidator):

    name = "ORDER"


    def __init__(self, config):

        # Keep config for future rules
        self.config = config


    def validate(self, order: Dict) -> ValidationResult:

        # ----------------------------
        # BASIC TYPE CHECK
        # ----------------------------

        if not isinstance(order, dict):

            return ValidationResult(
                rule="INVALID_ORDER",
                passed=False,
                severity="CRITICAL",
                message="Order must be a dictionary"
            )


        # ----------------------------
        # REQUIRED FIELDS
        # ----------------------------

        required_fields = [
            "order_id",
            "party_name",
            "lines",
        ]

        missing = []

        for field in required_fields:

            if field not in order or order.get(field) in ("", None):
                missing.append(field)


        if missing:

            return ValidationResult(
                rule="MISSING_FIELDS",
                passed=False,
                severity="CRITICAL",
                message=f"Missing required fields: {', '.join(missing)}",
                details={"missing": missing}
            )


        # ----------------------------
        # ORDER LINES CHECK
        # ----------------------------

        lines = order.get("lines")


        if not isinstance(lines, list):

            return ValidationResult(
                rule="INVALID_LINES",
                passed=False,
                severity="CRITICAL",
                message="Order lines must be a list"
            )


        if not lines:

            return ValidationResult(
                rule="EMPTY_ORDER",
                passed=False,
                severity="CRITICAL",
                message="Order must contain at least one line"
            )


        # ----------------------------
        # LINE VALIDATION
        # ----------------------------

        for idx, line in enumerate(lines, 1):

            # Structure
            if not isinstance(line, dict):

                return ValidationResult(
                    rule="INVALID_LINE",
                    passed=False,
                    severity="CRITICAL",
                    message=f"Line {idx} must be a dictionary"
                )


            # Product
            if "product_id" not in line:

                return ValidationResult(
                    rule="MISSING_PRODUCT",
                    passed=False,
                    severity="CRITICAL",
                    message=f"Line {idx} missing product_id"
                )


            # Eye side
            if not line.get("eye_side"):

                return ValidationResult(
                    rule="MISSING_EYE",
                    passed=False,
                    severity="CRITICAL",
                    message=f"Line {idx} missing eye side"
                )


            # ----------------------------
            # Quantity Validation
            # ----------------------------

            qty = line.get("billing_qty", 0)

            if not isinstance(qty, (int, float)) or qty <= 0:

                return ValidationResult(
                    rule="INVALID_QTY",
                    passed=False,
                    severity="CRITICAL",
                    message=f"Line {idx} has invalid quantity"
                )


            # ----------------------------
            # Batch Allocation Validation
            # ----------------------------

            batches = line.get("batch_allocation", [])

            # If batches exist → must match qty
            if isinstance(batches, list) and batches:

                allocated = 0

                for b in batches:
                    allocated += int(b.get("allocated_qty", 0))


                if allocated != qty:

                    return ValidationResult(
                        rule="BATCH_MISMATCH",
                        passed=False,
                        severity="CRITICAL",
                        message=f"Line {idx} batch allocation mismatch"
                    )


            # If no batches → allow provisional (do nothing)
            # Flexible ERP logic preserved ✔️


        # ----------------------------
        # PASSED
        # ----------------------------

        return ValidationResult(
            rule="ORDER_OK",
            passed=True,
            severity="INFO",
            message="Order structure and quantities valid"
        )
