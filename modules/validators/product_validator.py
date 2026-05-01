"""
Product validations
"""

from .base import BaseValidator, ValidationResult
from config.validation_config import VALIDATION_CONFIG


class ProductValidator(BaseValidator):

    name = "PRODUCT"


    def validate(self, order):

        discontinued = VALIDATION_CONFIG["DISCONTINUED_PRODUCTS"]

        errors = []


        for i, line in enumerate(order.get("lines", []), 1):

            pid = str(line.get("product_id", ""))

            if pid in discontinued:
                errors.append(f"Line {i}: {pid}")


        if errors:

            return ValidationResult(
                rule="DISCONTINUED_PRODUCT",
                passed=False,
                severity="CRITICAL",
                message="Discontinued products found",
                details={"items": errors}
            )


        return ValidationResult(
            rule="PRODUCT_OK",
            passed=True,
            severity="INFO",
            message="Products valid"
        )
