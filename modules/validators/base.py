"""
Base classes & helpers for all validators
"""

from typing import Dict


class ValidationResult:

    def __init__(
        self,
        rule: str,
        passed: bool,
        severity: str,
        message: str,
        details: dict = None
    ):

        self.rule = rule
        self.passed = passed
        self.severity = severity
        self.message = message
        self.details = details or {}

    def to_dict(self):

        return {
            "rule": self.rule,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "details": self.details
        }


class BaseValidator:
    """
    All validators must inherit this
    """

    name = "BASE"

    def validate(self, order: Dict) -> ValidationResult:
        raise NotImplementedError("Validator must implement validate()")
