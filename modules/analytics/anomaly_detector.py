"""
modules/analytics/anomaly_detector.py
=======================================
Auto Anomaly Detection — DV ERP Ingestion Platform

Analyses recent import history from loader_import_log and
flags unusual patterns:
  - Sudden drop in import volume
  - Error rate spike
  - Unusually short or long durations
  - Zero-row imports

Designed to be called from health_dashboard.py.
Silent on DB failure — never crashes the UI.
"""

import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


def detect_anomalies(lookback: int = 10) -> List[Dict]:
    """
    Analyse the last `lookback` import runs and return a list of anomaly dicts.

    Each anomaly dict has:
        severity  : "warning" | "critical"
        message   : human-readable description
        detail    : optional extra context

    Returns empty list if no anomalies or if DB is unavailable.
    """
    try:
        from modules.sql_adapter import run_query

        rows = run_query(
            """
            SELECT
                id AS import_id,
                file_type,
                import_mode AS mode,
                status,
                COALESCE(rows_total, 0)  AS total,
                COALESCE(error_count, 0) AS errors,
                COALESCE(duration_s, 0)  AS duration,
                imported_at
            FROM loader_import_log
            WHERE COALESCE(import_mode, '') != 'DRY'
            ORDER BY imported_at DESC
            LIMIT %(lookback)s
            """,
            {"lookback": lookback},
        )
    except Exception as e:
        logger.warning(f"[ANOMALY] Could not query import log: {e}")
        return []

    if not rows or len(rows) < 3:
        return []   # Not enough history to detect patterns

    anomalies = []
    latest    = rows[0]
    history   = rows[1:]   # everything except the most recent

    # ── Helpers ──────────────────────────────────────────────────────────────
    def avg(field):
        vals = [r[field] for r in history if r[field] is not None]
        return sum(vals) / len(vals) if vals else 0

    avg_total    = avg("total")
    avg_errors   = avg("errors")
    avg_duration = avg("duration")

    # ── Check 1: Volume drop ─────────────────────────────────────────────────
    if avg_total > 10 and latest["total"] < avg_total * 0.4:
        anomalies.append({
            "severity": "warning",
            "message":  "📉 Import volume dropped significantly",
            "detail":   (
                f"Latest: {latest['total']} rows  |  "
                f"Recent avg: {avg_total:.0f} rows"
            ),
        })

    # ── Check 2: Error spike ─────────────────────────────────────────────────
    if avg_errors > 0 and latest["errors"] > avg_errors * 3:
        anomalies.append({
            "severity": "critical",
            "message":  "🚨 Error rate spiked",
            "detail":   (
                f"Latest errors: {latest['errors']}  |  "
                f"Recent avg: {avg_errors:.1f}"
            ),
        })
    elif avg_errors == 0 and latest["errors"] > 5:
        anomalies.append({
            "severity": "warning",
            "message":  "🚨 Errors appeared in an otherwise clean import history",
            "detail":   f"Latest errors: {latest['errors']}",
        })

    # ── Check 3: Zero-row import (non-DRY) ───────────────────────────────────
    if latest["total"] == 0 and latest["status"] not in ("DRY", "FAILED"):
        anomalies.append({
            "severity": "warning",
            "message":  "⚠️ Last import processed zero rows",
            "detail":   f"File type: {latest['file_type']} | Status: {latest['status']}",
        })

    # ── Check 4: Duration spike (slow imports) ───────────────────────────────
    if avg_duration > 1 and latest["duration"] > avg_duration * 5:
        anomalies.append({
            "severity": "warning",
            "message":  "🐢 Import took unusually long",
            "detail":   (
                f"Latest: {latest['duration']:.1f}s  |  "
                f"Recent avg: {avg_duration:.1f}s"
            ),
        })

    # ── Check 5: Consecutive failures ────────────────────────────────────────
    recent_statuses = [r["status"] for r in rows[:5]]
    failed_count = recent_statuses.count("FAILED")
    if failed_count >= 3:
        anomalies.append({
            "severity": "critical",
            "message":  f"🔴 {failed_count} of last 5 imports FAILED",
            "detail":   "Check file format and column mapping.",
        })

    return anomalies


def get_import_health_score(lookback: int = 20) -> int:
    """
    Returns an overall ingestion health score (0–100).

    Based on:
      - Success rate of recent imports
      - Average error rate per import
      - Absence of anomalies

    Returns 100 if no history available (benefit of the doubt).
    """
    try:
        from modules.sql_adapter import run_query

        rows = run_query(
            """
            SELECT
                status,
                COALESCE(rows_total, 0)  AS total,
                COALESCE(error_count, 0) AS errors
            FROM loader_import_log
            WHERE COALESCE(import_mode, '') != 'DRY'
            ORDER BY imported_at DESC
            LIMIT %(lookback)s
            """,
            {"lookback": lookback},
        )
    except Exception as e:
        logger.warning(f"[HEALTH] Could not compute health score: {e}")
        return 100

    if not rows:
        return 100

    total_imports = len(rows)
    failed        = sum(1 for r in rows if r["status"] == "FAILED")
    partial       = sum(1 for r in rows if r["status"] == "PARTIAL")
    total_errors  = sum(r["errors"] for r in rows)
    total_rows    = sum(r["total"] for r in rows) or 1

    # Penalty breakdown
    failure_penalty  = (failed / total_imports) * 40
    partial_penalty  = (partial / total_imports) * 15
    error_rate       = total_errors / total_rows
    error_penalty    = min(error_rate * 100, 30)

    score = 100 - failure_penalty - partial_penalty - error_penalty
    return max(0, min(100, int(score)))
