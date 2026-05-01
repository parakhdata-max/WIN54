"""
modules/core/error_logger.py
==============================
Central error logger — structured error capture for the entire ERP.

Captures:
  - Unhandled exceptions with full context
  - UI-level errors shown to operators
  - Pipeline failures (order submission, billing, imports)
  - Slow operations above threshold

Writes to:
  - Python logging (goes to stdout / log files per your deployment)
  - error_log DB table (queryable from Admin tab)

USAGE:
    from modules.core.error_logger import log_error, log_slow, capture

    # Log a caught exception with context
    try:
        pipeline.submit(...)
    except Exception as e:
        log_error(e, context={"order_no": "ORD-001", "user": "Rahul"})

    # Auto-capture with context manager
    with capture("retail_confirm", order_no="ORD-001"):
        pipeline.submit(...)

    # Log a slow operation
    with log_slow("db_query", threshold_ms=500) as t:
        run_heavy_query()
"""

import logging
import time
import traceback
import uuid
import json
import datetime
from contextlib import contextmanager
from typing import Any, Dict, Optional

logger = logging.getLogger("erp.errors")


# ── Config ────────────────────────────────────────────────────────────────────
SLOW_QUERY_THRESHOLD_MS = 500    # log warning if operation exceeds this


# ── DB write (best-effort — never raises) ─────────────────────────────────────

def _db_write(error_record: dict):
    """Write to error_log table. Silently swallows failures."""
    try:
        from modules.sql_adapter import run_write
        run_write("""
            INSERT INTO error_log
                (id, context, error_type, error_msg, traceback, payload, created_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
        """, (
            str(uuid.uuid4()),
            error_record.get("context", "unknown"),
            error_record.get("error_type", "Exception"),
            error_record.get("message", "")[:2000],
            error_record.get("traceback", "")[:5000],
            json.dumps(error_record.get("payload", {})),
        ))
    except Exception:
        pass   # error logging must never cause more errors


def _operator():
    try:
        import streamlit as st
        u = st.session_state.get("user", "system")
        return u if isinstance(u, str) else u.get("name", "system")
    except Exception:
        return "system"


# ── Main log_error function ───────────────────────────────────────────────────

def log_error(
    exc: Exception,
    context: str = "unknown",
    payload: Optional[Dict[str, Any]] = None,
    show_ui: bool = False,
):
    """
    Log an error to both Python logging and DB.
    Never raises — safe to call in except blocks.

    Args:
        exc:     The caught exception
        context: Where the error happened e.g. "retail_confirm", "billing_save"
        payload: Extra context dict e.g. {"order_no": "ORD-001"}
        show_ui: If True, also shows st.error() — use sparingly
    """
    try:
        tb       = traceback.format_exc()
        msg      = str(exc)
        err_type = type(exc).__name__
        op       = _operator()

        record = {
            "context":    context,
            "error_type": err_type,
            "message":    msg,
            "traceback":  tb,
            "payload":    {**(payload or {}), "operator": op},
        }

        # Always log to Python logger (shows in terminal / log files)
        logger.error(
            "[%s] %s: %s | payload=%s",
            context, err_type, msg[:200],
            json.dumps(payload or {})[:200],
            exc_info=True
        )

        # Write to DB error_log
        _db_write(record)

        # Optional UI error display
        if show_ui:
            try:
                import streamlit as st
                st.error(f"❌ {err_type}: {msg[:300]}")
            except Exception:
                pass

    except Exception:
        pass   # never let the logger itself raise


# ── Context manager: auto-capture ─────────────────────────────────────────────

@contextmanager
def capture(context: str, payload: Optional[Dict] = None, reraise: bool = True):
    """
    Context manager — automatically captures any exception with full context.
    By default re-raises after logging so normal error handling continues.

        with capture("billing_save", payload={"order_no": "ORD-001"}):
            save_billing(...)
        # if exception: logged to DB + Python logger, then re-raised
    """
    try:
        yield
    except Exception as e:
        log_error(e, context=context, payload=payload)
        if reraise:
            raise


# ── Slow operation tracker ────────────────────────────────────────────────────

@contextmanager
def log_slow(label: str, threshold_ms: int = SLOW_QUERY_THRESHOLD_MS):
    """
    Measure an operation and warn if it exceeds threshold.

        with log_slow("fetch_stock", threshold_ms=300):
            result = run_heavy_query()
    """
    t0 = time.time()
    try:
        yield
    finally:
        elapsed_ms = (time.time() - t0) * 1000
        if elapsed_ms > threshold_ms:
            logger.warning(
                "[SLOW] %s took %.0fms (threshold: %dms)",
                label, elapsed_ms, threshold_ms
            )
            try:
                from modules.sql_adapter import run_write
                run_write("""
                    INSERT INTO error_log
                        (id, context, error_type, error_msg, payload, created_at)
                    VALUES (%s, %s, 'SLOW_QUERY', %s, %s::jsonb, NOW())
                """, (
                    str(uuid.uuid4()),
                    label,
                    f"{elapsed_ms:.0f}ms (threshold {threshold_ms}ms)",
                    json.dumps({"label": label, "elapsed_ms": round(elapsed_ms, 1),
                                "threshold_ms": threshold_ms}),
                ))
            except Exception:
                pass


# ── Query recent errors (for admin panel) ─────────────────────────────────────

def get_recent_errors(limit: int = 50, context_filter: str = None) -> list:
    """Fetch recent errors from DB for the admin panel."""
    try:
        from modules.sql_adapter import run_query
        sql    = "SELECT * FROM error_log WHERE 1=1"
        params = []
        if context_filter:
            sql += " AND context ILIKE %s"
            params.append(f"%{context_filter}%")
        sql += f" ORDER BY created_at DESC LIMIT {limit}"
        return run_query(sql, params) or []
    except Exception:
        return []
