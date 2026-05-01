"""
modules/core/validation_result.py
==================================
Shared ValidationIssue type used by validators_builtin.py
and any custom validators registered via register_global / register_for_mode.

Severity levels:
  ERROR    — hard block, order REJECTED before save
  WARNING  — soft alert, order proceeds with notification shown
  ADVISORY — informational, always passes, logged only
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ValidationIssue:
    code:        str
    message:     str
    severity:    str            # "ERROR" | "WARNING" | "ADVISORY"
    product_name: Optional[str] = None
    line_idx:    Optional[int]  = None
    details:     dict          = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity == "ERROR"

    @property
    def is_warning(self) -> bool:
        return self.severity == "WARNING"

    @property
    def is_advisory(self) -> bool:
        return self.severity == "ADVISORY"

    def to_dict(self) -> dict:
        return {
            "code":         self.code,
            "message":      self.message,
            "severity":     self.severity,
            "product_name": self.product_name,
            "line_idx":     self.line_idx,
            "details":      self.details,
        }


def error(code: str, message: str, line: dict = None) -> ValidationIssue:
    return ValidationIssue(
        code=code, message=message, severity="ERROR",
        product_name=(line or {}).get("product_name"),
        line_idx=(line or {}).get("_line_idx"),
    )

def warning(code: str, message: str, line: dict = None) -> ValidationIssue:
    return ValidationIssue(
        code=code, message=message, severity="WARNING",
        product_name=(line or {}).get("product_name"),
        line_idx=(line or {}).get("_line_idx"),
    )

def advisory(code: str, message: str, line: dict = None) -> ValidationIssue:
    return ValidationIssue(
        code=code, message=message, severity="ADVISORY",
        product_name=(line or {}).get("product_name"),
        line_idx=(line or {}).get("_line_idx"),
    )
