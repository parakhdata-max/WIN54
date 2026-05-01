"""
modules/core/audit_log.py

Audit Trail — Validation, Pricing, Submission
===============================================
Every finalize call writes a JSON-lines record covering:
    - Who submitted
    - Validation results (all issues, by severity)
    - Pricing trace (per-line, totals)
    - Order outcome (CONFIRMED / REJECTED)
    - Timestamps

FORMAT: JSON Lines (.jsonl) — one record per finalize call.
        Append-only. Never mutates existing records.

STORAGE: Configurable via AUDIT_LOG_PATH env var.
         Defaults to logs/audit.jsonl relative to project root.

THREAD SAFETY: Uses file-level locking (portalocker if available,
               fallback to threading.Lock for single-process use).

USAGE IN finalize_engine.py:
    from modules.core.audit_log import AuditLog
    audit = AuditLog()
    audit.record(
        event        = "FINALIZE",
        order_id     = order_no,
        order_info   = order_info,
        user_name    = user_name,
        issues       = issues,           # list[ValidationIssue]
        pricing_trace= trace,            # PricingTrace | None
        outcome      = "CONFIRMED",
    )
"""

from __future__ import annotations
import json
import os
import datetime
import threading
import logging
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from modules.core.validation_result import ValidationIssue
    from modules.core.pricing_pipeline import PricingTrace

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()


# ============================================================================
# CONFIGURATION
# ============================================================================

def _resolve_log_path() -> str:
    """
    Resolve audit log file path.
    Priority: AUDIT_LOG_PATH env var → logs/audit.jsonl relative to cwd.
    """
    env = os.environ.get("AUDIT_LOG_PATH")
    if env:
        return env
    # Default: logs/ next to project root
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "logs", "audit.jsonl")


# ============================================================================
# AUDIT LOG
# ============================================================================

class AuditLog:
    """
    Thread-safe append-only audit log writer.

    One instance per finalize_engine call is fine — the file is opened,
    written, and closed immediately on each record() call.
    """

    def __init__(self, path: str = None):
        self.path = path or _resolve_log_path()
        self._ensure_dir()

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def record(
        self,
        event:          str,
        order_id:       str,
        order_info:     dict,
        user_name:      str,
        outcome:        str,                          # "CONFIRMED" | "REJECTED"
        issues:         List["ValidationIssue"] = None,
        pricing_trace:  Optional["PricingTrace"] = None,
        extra:          dict = None,
    ) -> None:
        """
        Write one audit record to the log file.

        This is intentionally fire-and-forget: log failures are caught
        and logged to the Python logger but never raise to the caller.
        Audit must never block or crash an order submission.
        """
        try:
            record = self._build_record(
                event, order_id, order_info, user_name,
                outcome, issues or [], pricing_trace, extra or {}
            )
            self._write(record)
        except Exception as exc:
            logger.error("Audit log write failed (non-fatal): %s", exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_record(
        self,
        event:         str,
        order_id:      str,
        order_info:    dict,
        user_name:     str,
        outcome:       str,
        issues:        list,
        pricing_trace,
        extra:         dict,
    ) -> dict:
        now = datetime.datetime.now().isoformat()

        # Validation summary
        errors    = [i for i in issues if i.is_error]
        warnings  = [i for i in issues if i.is_warning]
        advisories= [i for i in issues if i.is_advisory]

        validation_summary = {
            "total":      len(issues),
            "errors":     len(errors),
            "warnings":   len(warnings),
            "advisories": len(advisories),
            "issues":     [i.to_dict() for i in issues],
        }

        # Pricing summary
        pricing_summary = None
        if pricing_trace is not None:
            try:
                pricing_summary = pricing_trace.to_dict()
            except Exception:
                pricing_summary = {"error": "trace serialization failed"}

        return {
            # ── Identity ──────────────────────────────────────────────────────
            "timestamp":   now,
            "event":       event,
            "outcome":     outcome,
            "order_id":    order_id,
            "schema_version": order_info.get("schema_version"),

            # ── Who ───────────────────────────────────────────────────────────
            "user":        user_name,
            "mode":        order_info.get("order_type", "UNKNOWN"),
            "party":       order_info.get("party") or order_info.get("patient_name"),

            # ── What ──────────────────────────────────────────────────────────
            "line_count":  order_info.get("_line_count", 0),
            "final_value": order_info.get("final_value", 0),
            "net_value":   order_info.get("net_value", 0),
            "tax_amount":  order_info.get("tax_amount", 0),

            # ── Validation ────────────────────────────────────────────────────
            "validation":  validation_summary,

            # ── Pricing ───────────────────────────────────────────────────────
            "pricing":     pricing_summary,

            # ── Extra ─────────────────────────────────────────────────────────
            **extra,
        }

    def _write(self, record: dict) -> None:
        """Append one JSON line to the log file. Thread-safe."""
        line = json.dumps(record, default=str) + "\n"
        with _write_lock:
            try:
                import portalocker
                with open(self.path, "a", encoding="utf-8") as f:
                    portalocker.lock(f, portalocker.LOCK_EX)
                    f.write(line)
            except ImportError:
                # portalocker not available — single-process lock only
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)

    # ── Query helpers (for admin panel / health check) ─────────────────────────

    def tail(self, n: int = 50) -> List[dict]:
        """Return the last n audit records. Safe if log doesn't exist yet."""
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return [json.loads(l) for l in lines[-n:] if l.strip()]
        except Exception as exc:
            logger.error("Audit log read failed: %s", exc)
            return []

    def count_by_outcome(self) -> dict:
        """Count CONFIRMED vs REJECTED submissions. Useful for health check."""
        counts = {"CONFIRMED": 0, "REJECTED": 0, "OTHER": 0}
        for record in self.tail(n=10_000):
            outcome = record.get("outcome", "OTHER")
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts
