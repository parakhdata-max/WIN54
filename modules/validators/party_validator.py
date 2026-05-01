"""
Party related validations
"""

from .base import BaseValidator, ValidationResult
from config.validation_config import VALIDATION_CONFIG


class PartyValidator(BaseValidator):

    name = "PARTY"


    def validate(self, order):

        party = order.get("party", "").strip()

        if not party:

            return ValidationResult(
                rule="MISSING_PARTY",
                passed=False,
                severity="CRITICAL",
                message="Party name missing"
            )


        if party in VALIDATION_CONFIG["BLOCKED_PARTIES"]:

            return ValidationResult(
                rule="BLOCKED_PARTY",
                passed=False,
                severity="CRITICAL",
                message=f"Party {party} is blocked"
            )


        return ValidationResult(
            rule="PARTY_OK",
            passed=True,
            severity="INFO",
            message="Party valid"
        )
