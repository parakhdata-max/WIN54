"""
Event Logger
============
Audit trail for backoffice + production events.
Logs to order_status_history (already in your DB schema).
Used by production_panel.py for stage advance logging and timeline display.

SCHEMA (from DB backup):
  order_status_history:
    history_id, order_id(uuid), from_status, to_status,
    changed_at, changed_by, changed_by_name, remarks, metadata(jsonb)
"""

import streamlit as st
from enum import Enum
from typing import Optional, Dict, Any
import re
import uuid as _uuid

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE
)


# ==================================================
# EVENT TYPES
# ==================================================

class EventType(str, Enum):
    STAGE_ADVANCED    = "STAGE_ADVANCED"
    STATUS_CHANGED    = "STATUS_CHANGED"
    ORDER_SAVED       = "ORDER_SAVED"
    ALLOCATION_SAVED  = "ALLOCATION_SAVED"
    PRODUCT_CHANGED   = "PRODUCT_CHANGED"
    NOTE_ADDED        = "NOTE_ADDED"
    BILLING_WRITTEN = "BILLING_WRITTEN"
    BILLING_LOCKED   = "BILLING_LOCKED"
    ORDER_STATUS_CHANGED = "ORDER_STATUS_CHANGED"


# ==================================================
# DB HELPERS
# ==================================================

def _q(sql: str, params: dict):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _exec(sql: str, params: dict) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params)
    except Exception:
        return False


def _resolve_order_uuid(order_id: str) -> Optional[str]:
    """Accept UUID or order_no text. Returns UUID string for orders.id."""
    if UUID_RE.match(str(order_id)):
        return str(order_id)
    rows = _q(
        "SELECT id FROM orders WHERE order_no = %(ono)s LIMIT 1",
        {"ono": order_id}
    )
    return str(rows[0]["id"]) if rows else None


# ==================================================
# LOG EVENT
# ==================================================

def log_event(
    event_type: EventType,
    order_id: str,
    details: Optional[Dict[str, Any]] = None,
    source: str = "system",
    remarks: str = "",
):
    """
    Write an audit event to order_status_history.
    Silently swallows errors — logging must never crash the UI.
    """
    import json
    order_uuid = _resolve_order_uuid(order_id)
    if not order_uuid:
        return

    metadata = {
        "event_type": str(event_type),
        "source":     source,
        **(details or {}),
    }

    _exec("""
        INSERT INTO order_status_history (
            history_id, order_id, from_status, to_status,
            changed_at, changed_by_name, remarks
        )
        VALUES (
            %(hid)s::uuid, %(oid)s::uuid, NULL, %(etype)s,
            NOW(), %(src)s, %(rmk)s
        )
    """, {
        "hid":   str(_uuid.uuid4()),
        "oid":   order_uuid,
        "etype": str(event_type),
        "src":   source,
        "rmk":   remarks or "",
    })


# ==================================================
# RENDER TIMELINE
# ==================================================

def render_event_timeline(order_id: str):
    """
    Render chronological audit trail for an order.
    Called from production_panel inside the Audit Trail expander.
    """
    order_uuid = _resolve_order_uuid(order_id)
    if not order_uuid:
        st.caption("No audit trail — order ID could not be resolved.")
        return

    rows = _q("""
        SELECT to_status, changed_at, changed_by_name, remarks, metadata
        FROM order_status_history
        WHERE order_id = %(oid)s::uuid
        ORDER BY changed_at ASC
    """, {"oid": order_uuid})

    if not rows:
        st.caption("No events recorded yet.")
        return

    colors = {
        "STAGE_ADVANCED":   "#3b82f6",
        "STATUS_CHANGED":   "#8b5cf6",
        "ORDER_SAVED":      "#10b981",
        "ALLOCATION_SAVED": "#f59e0b",
        "PRODUCT_CHANGED":  "#ef4444",
        "NOTE_ADDED":       "#6b7280",
    }

    for row in rows:
        ts    = str(row.get("changed_at", ""))[:19]
        etype = row.get("to_status", "EVENT")
        by    = row.get("changed_by_name") or "system"
        note  = row.get("remarks") or ""
        color = colors.get(etype, "#6b7280")

        meta  = row.get("metadata") or {}
        extra = ""
        if isinstance(meta, dict):
            stage = meta.get("stage") or meta.get("next_stage")
            job   = meta.get("job_id")
            if stage:
                extra += f" → **{stage}**"
            if job:
                extra += f" `{str(job)[:8]}…`"

        st.markdown(
            f"<div style='border-left:3px solid {color};"
            f"padding:4px 10px;margin-bottom:6px;'>"
            f"<span style='color:{color};font-weight:600;font-size:0.8rem'>{etype}</span>"
            f"{extra} "
            f"<span style='color:#6b7280;font-size:0.75rem'>— {ts} by {by}</span>"
            + (f"<br><span style='font-size:0.75rem;color:#374151'>{note}</span>" if note else "")
            + "</div>",
            unsafe_allow_html=True
        )


# ==================================================
# EXPORTS
# ==================================================

__all__ = ["EventType", "log_event", "render_event_timeline"]
