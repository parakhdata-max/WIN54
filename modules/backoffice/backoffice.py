"""
Backoffice Main Orchestrator
v3 — Production Train + Smart Dashboard (all self-contained)
"""

import streamlit as st
import html
from modules.core.business_rules import (
    STATUS_TRANSITIONS, TERMINAL_STATUSES, EDITABLE_STATUSES,
    is_ready_for_billing, skip_allocation, skip_production,
    invoice_requires_full_payment,
)
import datetime
from typing import Dict, List, Optional

from .backoffice_helpers import (
    get_display_order_id,
    get_display_label,
    load_orders_from_database,
    resolve_line_route,
)
from .order_loader import load_orders_summary
from .backoffice_ui import render_order_detail
from modules.workflow.status import OrderStatus
from modules.backoffice_clinical_viewer import render_clinical_viewer_page


def _scan_norm(value: str) -> str:
    s = "".join(ch for ch in str(value or "") if ch.isalnum()).lower()
    if s.startswith("o") and len(s) > 1:
        s = s[1:]
    return s


def _scan_match(needle: str, *hay_values) -> bool:
    raw = str(needle or "").strip().lower()
    norm = _scan_norm(raw)
    if not raw:
        return True
    for value in hay_values:
        text = str(value or "").lower()
        if raw in text:
            return True
        hnorm = _scan_norm(value)
        if norm and hnorm and (norm in hnorm or hnorm in norm):
            return True
    return False


def _sync_supplier_orders_id_sequence() -> None:
    try:
        from modules.sql_adapter import run_write
        run_write("""
            SELECT setval(
                pg_get_serial_sequence('supplier_orders','id'),
                GREATEST((SELECT COALESCE(MAX(id), 0) FROM supplier_orders), 1),
                TRUE
            )
        """, {})
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# TRAIN STATIONS  — edit this list to add / reorder stages
# ══════════════════════════════════════════════════════════════
# Each entry:
#   id           — unique key
#   short        — label shown on the train dot
#   icon         — emoji shown inside the dot
#   status       — order.status that maps to this station (None = sub-station)
#   color        — hex accent color
#   whatsapp     — True = auto-message fires when entering this station
#   wa_msg       — message template (use {party} and {order_no} as placeholders)
#   sub_of       — parent station id (makes this a sub-station)

_STATIONS = [
    {
        "id": "ORDER_RECEIVED",   "short": "Received",
        "icon": "📥",             "status": "PENDING",
        "color": "#3b82f6",       "whatsapp": True,
        "wa_msg": "Dear {party}, your order {order_no} has been received. We'll get started soon!",
    },
    {
        "id": "UNDER_REVIEW",     "short": "Review",
        "icon": "🔍",             "status": "UNDER_REVIEW",
        "color": "#f59e0b",       "whatsapp": False,
    },
    {
        "id": "CONFIRMED",        "short": "Confirmed ✓",
        "icon": "✅",             "status": "CONFIRMED",
        "color": "#6366f1",       "whatsapp": True,
        "wa_msg": "Dear {party}, great news! Order {order_no} is confirmed and being processed.",
    },
    {
        "id": "IN_PRODUCTION",    "short": "Production",
        "icon": "⚙️",             "status": "IN_PRODUCTION",
        "color": "#8b5cf6",       "whatsapp": False,
    },
    {
        "id": "SUPPLIER_PO",      "short": "Supplier PO",
        "icon": "🏭",             "status": None,
        "color": "#a855f7",       "whatsapp": False,
        "sub_of": "IN_PRODUCTION",
    },
    {
        "id": "READY",            "short": "Ready",
        "icon": "📦",             "status": "READY",
        "color": "#10b981",       "whatsapp": True,
        "wa_msg": "Dear {party}, order {order_no} is ready! We will contact you for delivery.",
    },
    {
        "id": "BILLED",           "short": "Billed",
        "icon": "🧾",             "status": "BILLED",
        "color": "#059669",       "whatsapp": True,
        "wa_msg": "Dear {party}, invoice for order {order_no} has been generated. Please check WhatsApp.",
    },
    {
        "id": "DISPATCHED",       "short": "Dispatched",
        "icon": "🚚",             "status": "DISPATCHED",
        "color": "#0891b2",       "whatsapp": True,
        "wa_msg": "Dear {party}, order {order_no} is on its way! Tracking details to follow.",
    },
    {
        "id": "COURIER_ENTRY",    "short": "Courier",
        "icon": "📮",             "status": None,
        "color": "#0e7490",       "whatsapp": False,
        "sub_of": "DISPATCHED",
    },
    {
        "id": "DELIVERED",        "short": "Delivered",
        "icon": "✅",             "status": "DELIVERED",
        "color": "#10b981",       "whatsapp": True,
        "wa_msg": "Dear {party}, order {order_no} has been delivered! Thank you 🙏",
    },
    {
        "id": "CLOSED",           "short": "Closed",
        "icon": "🔒",             "status": "CLOSED",
        "color": "#334155",       "whatsapp": False,
    },
]

# Main track = top-level stations only (no sub_of)
_MAIN_TRACK = [s for s in _STATIONS if not s.get("sub_of")]

# status string → index in _MAIN_TRACK
_STATUS_IDX = {s["status"]: i for i, s in enumerate(_MAIN_TRACK) if s.get("status")}

_TERMINAL = {"CLOSED", "DELIVERED", "CANCELLED", "RETURNED"}


# ══════════════════════════════════════════════════════════════
# STATUS / LIFECYCLE CONSTANTS
# ══════════════════════════════════════════════════════════════

_ALL_STATUSES = [
    "PENDING", "UNDER_REVIEW", "HOLD", "CREDIT_HOLD", "PENDING_PAYMENT",
    "CONFIRMED", "IN_PRODUCTION", "READY", "READY_FOR_BILLING",
    "PARTIALLY_BILLED", "CHALLANED", "BILLED", "DISPATCHED",
    "DELIVERED", "CLOSED", "CANCELLED",
]

_TRANSITIONS = {
    # BILLED removed — billing status is live from challan/invoice system
    # DISPATCHED is the only manual action after billing
    "PENDING":           ["HOLD", "UNDER_REVIEW", "CONFIRMED", "CANCELLED"],
    "HOLD":              ["UNDER_REVIEW", "CONFIRMED", "CANCELLED"],
    "CREDIT_HOLD":       ["UNDER_REVIEW", "CANCELLED"],
    "PENDING_PAYMENT":   ["UNDER_REVIEW", "CANCELLED"],
    "CONFIRMED":         ["IN_PRODUCTION", "READY", "CANCELLED"],
    "IN_PRODUCTION":     ["READY", "CANCELLED"],
    "READY":             ["DISPATCHED"],
    "READY_FOR_BILLING": ["DISPATCHED"],
    "BILLED":            ["DISPATCHED"],
    "DISPATCHED":        ["DELIVERED"],
    "DELIVERED":         ["CLOSED"],
    "CLOSED":            [],
    "CANCELLED":         [],
    "PROVISIONAL":       ["HOLD", "UNDER_REVIEW", "CONFIRMED", "CANCELLED"],
    "UNDER_REVIEW":      ["HOLD", "CONFIRMED", "CANCELLED"],
}

try:
    from modules.backoffice.order_status_live import STATUS_META as _OSL_META
    _STATUS_COLOR = {k: v["color"] for k, v in _OSL_META.items()}
    _STATUS_ICON  = {k: v["icon"]  for k, v in _OSL_META.items()}
except Exception:
    _STATUS_COLOR = {
        "PENDING":"#64748b","PROVISIONAL":"#64748b","UNDER_REVIEW":"#f59e0b",
        "HOLD":"#f97316","CREDIT_HOLD":"#dc2626","PENDING_PAYMENT":"#f97316",
        "CONFIRMED":"#3b82f6","IN_PRODUCTION":"#8b5cf6","READY":"#10b981",
        "READY_FOR_BILLING":"#0d9488","PARTIALLY_BILLED":"#f59e0b",
        "CHALLANED":"#3b82f6","BILLED":"#059669","DISPATCHED":"#0891b2","DELIVERED":"#10b981",
        "CLOSED":"#334155","CANCELLED":"#ef4444",
    }
    _STATUS_ICON = {
        "PENDING":"⏳","PROVISIONAL":"📝","UNDER_REVIEW":"🔍","CONFIRMED":"✅",
        "HOLD":"⏸️","CREDIT_HOLD":"🛑","PENDING_PAYMENT":"💳",
        "IN_PRODUCTION":"⚙️","READY":"📦","READY_FOR_BILLING":"🚀",
        "PARTIALLY_BILLED":"⚡","CHALLANED":"📋","BILLED":"🧾",
        "DISPATCHED":"🚚","DELIVERED":"✅","CLOSED":"🔒","CANCELLED":"❌",
    }
_STATUS_COLOR.setdefault("PROVISIONAL", "#64748b")

_STATUS_DISPLAY = {
    "PENDING": "Pending", "CONFIRMED": "Confirmed",
    "IN_PRODUCTION": "In Production", "READY": "Ready",
    "CHALLANED": "Challaned", "BILLED": "Billed",
    "INVOICED": "Invoiced", "DISPATCHED": "Dispatched",
    "DELIVERED": "Delivered", "CLOSED": "Closed",
    "CANCELLED": "Cancelled", "PARTIALLY_BILLED": "Part Billed",
    "PROVISIONAL": "Provisional", "READY_TO_BILL": "Ready to Bill",
}
_ROUTE_COLOR = {
    "STOCK": "#0891b2", "VENDOR": "#8b5cf6",
    "INHOUSE": "#f59e0b", "EXTERNAL_LAB": "#10b981",
}
_ROUTE_LABEL = {
    "STOCK": "📦 Stock", "VENDOR": "🏭 Supplier",
    "INHOUSE": "🔧 In-House", "EXTERNAL_LAB": "🔬 Ext Lab",
}
_TYPE_COLOR = {
    "RETAIL": "#0891b2", "WHOLESALE": "#8b5cf6", "PURCHASE": "#f59e0b",
}


# ══════════════════════════════════════════════════════════════
# SESSION INIT
# ══════════════════════════════════════════════════════════════

def init_backoffice_state():
    defaults = {
        "bo_active_orders": [],
        "bo_selected_order_id": None,
        "bo_filter_status": "All",
        "bo_search_query": "",
        "bo_view_mode": "dashboard",
        "bo_effectivity_mode": {},
        "bo_editing_line": None,
        "bo_show_allocation_window": False,
        "bo_allocation_line_idx": None,
        "bo_orders_loaded": False,
        "bo_product_change_modal": {"active": False},
        "bo_show_clinical_nav": False,
        "bo_type_filter": "All",
        "bo_route_filter": "All",
        "bo_include_closed": True,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ══════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════

def _days_old(dt) -> int:
    if not dt:
        return 0
    try:
        d = dt.date() if hasattr(dt, "date") else datetime.date.fromisoformat(str(dt)[:10])
        return (datetime.date.today() - d).days
    except Exception:
        return 0


def _order_lines(order: dict) -> list:
    seen, out = set(), []
    for src in ("stock_lines", "inhouse_lines", "lab_order_lines", "service_lines", "lines"):
        for l in (order.get(src) or []):
            if not isinstance(l, dict):
                continue
            _is_deleted_line = l.get("is_deleted")
            if isinstance(_is_deleted_line, str):
                _is_deleted_line = _is_deleted_line.strip().lower() in ("1", "true", "yes", "y")
            if _is_deleted_line:
                continue
            if id(l) not in seen:
                seen.add(id(l)); out.append(l)
    return out


def _route_summary(lines: list) -> dict:
    s = {}
    for l in lines:
        r = resolve_line_route(l)
        s[r] = s.get(r, 0) + 1
    return s


def _current_station_idx(order: dict) -> int:
    status = _determine_workflow_status(order, order.get("status") or "PENDING", _order_lines(order))
    return _STATUS_IDX.get(status, 0)


def _has_supplier_po(order: dict) -> bool:
    return any(
        isinstance(l, dict) and l.get("supplier_order_id")
        for l in _order_lines(order)
    )


def _has_courier(order: dict) -> bool:
    return bool(
        order.get("courier_number") or
        order.get("tracking_no") or
        order.get("courier_ref")
    )


def _suggest_next_status(order: dict, lines: list) -> Optional[str]:
    current = order.get("status", "PENDING")
    if current in _TERMINAL:
        return None
    routes = _route_summary(lines)
    # SERVICE lines (consultation fee) are auto-allocated — exclude from counts
    _prod_lines = [l for l in lines if str(l.get("eye_side","")).upper() not in ("SERVICE", "S")]
    alloc  = sum(int(l.get("allocated_qty") or 0) for l in _prod_lines)
    billed = sum(int(l.get("billing_qty") or 0) for l in _prod_lines)
    # UNDER_REVIEW orders always show Confirm button — backoffice must explicitly confirm
    if current == "UNDER_REVIEW":
        return "CONFIRMED"

    if current == "PENDING" and billed > 0 and alloc >= billed:
        return "CONFIRMED"
    if current == "CONFIRMED" and any(r in routes for r in ("VENDOR", "INHOUSE", "EXTERNAL_LAB")):
        return "IN_PRODUCTION"
    if current == "CONFIRMED" and routes and all(r == "STOCK" for r in routes):
        return "READY"
    return None


def _save_status_change(
    order: dict,
    new_status: str,
    scan_source: str = "DASHBOARD",
    scanned_by: str = None,
) -> bool:
    """
    Persist a status change.
    Writes to orders table + order_status_history with full stage timing.

    scan_source: SCANNER | DASHBOARD | SYSTEM | API
    Records stage_entered_at, and updates stage_exited_at + duration_minutes
    on the PREVIOUS history row automatically.
    """
    try:
        from modules.sql_adapter import run_write, run_query
        _prev_status = order.get("status", "PENDING")

        # Resolve operator
        try:
            from modules.security.roles import current_user_name
            _operator = current_user_name()
        except Exception:
            _operator = st.session_state.get("user", "backoffice")
            if not isinstance(_operator, str):
                _operator = _operator.get("name", "backoffice")

        # 0. Re-fetch status from DB — detect stale UI before writing
        try:
            from modules.sql_adapter import get_fresh_order_status
            _db_status_now = get_fresh_order_status(order.get("order_no", ""))
            if _db_status_now and _db_status_now != _prev_status.upper():
                st.warning(
                    f"⚠️ Order status changed by another user — "
                    f"page shows **{_prev_status}** but DB now has **{_db_status_now}**. "
                    "Refresh the page before proceeding."
                )
                return False
        except Exception:
            pass  # non-fatal — proceed with optimistic update

        # 1. Atomic status update — WHERE status=_prev_status prevents race condition
        # If two users try to move the same order simultaneously, only one wins.
        # The loser gets rows_affected=0 and an informative error.
        try:
            from modules.sql_adapter import status_change_atomic
            _won = status_change_atomic(
                order_no=order.get("order_no"),
                expected_status=_prev_status,
                new_status=new_status,
                changed_by=_operator,
                remarks=f"{scan_source}: {_prev_status} → {new_status}",
            )
            if not _won:
                # Status was already changed by another user
                _current = run_query(
                    "SELECT status FROM orders WHERE order_no=%s LIMIT 1",
                    (order.get("order_no"),)
                )
                _actual = _current[0]["status"] if _current else "unknown"
                st.warning(
                    f"⚠️ Could not move to **{new_status}** — "
                    f"order is currently **{_actual}** (changed by another user). "
                    f"Refresh the page to see the latest status."
                )
                return False
        except ImportError:
            # Fallback for older installs without status_change_atomic
            run_write(
                "UPDATE orders SET status=%(s)s, updated_at=NOW() WHERE order_no=%(o)s",
                {"s": new_status, "o": order.get("order_no")},
            )

        # 2. Write to order_status_history — minimal columns only (safe fallback)
        _rmk = f"{scan_source}: {_prev_status} -> {new_status}"
        _by  = scanned_by or _operator

        # Try full insert first; fall back to minimal columns if extras missing
        try:
            run_write("""
                INSERT INTO order_status_history
                    (history_id, order_id, from_status, to_status,
                     changed_at, changed_by_name, remarks,
                     scan_source, stage_entered_at, scanned_by_user)
                SELECT gen_random_uuid()::uuid, id, %(frm)s, %(to)s,
                       NOW(), %(by)s, %(rmk)s,
                       %(src)s, NOW(), %(sbu)s
                FROM orders WHERE order_no = %(ono)s
            """, {
                "frm": _prev_status, "to": new_status,
                "by": _by, "rmk": _rmk,
                "src": scan_source, "sbu": _by,
                "ono": order.get("order_no"),
            })
        except Exception:
            # Columns like scan_source / stage_entered_at / scanned_by_user
            # may not exist on this install — use the minimal safe set
            try:
                run_write("""
                    INSERT INTO order_status_history
                        (order_id, from_status, to_status,
                         changed_at, changed_by_name, remarks)
                    SELECT id, %(frm)s, %(to)s,
                           NOW(), %(by)s, %(rmk)s
                    FROM orders WHERE order_no = %(ono)s
                """, {
                    "frm": _prev_status, "to": new_status,
                    "by": _by, "rmk": _rmk,
                    "ono": order.get("order_no"),
                })
            except Exception:
                pass  # history is non-fatal — order status still updated

        # 3. Close out PREVIOUS stage row — best-effort, non-fatal
        try:
            run_write("""
                UPDATE order_status_history
                SET stage_exited_at  = NOW(),
                    duration_minutes = ROUND(
                        EXTRACT(EPOCH FROM (NOW() - stage_entered_at)) / 60.0, 1
                    )
                WHERE order_id = (SELECT id FROM orders WHERE order_no = %(ono)s LIMIT 1)
                  AND to_status      = %(prev)s
                  AND stage_exited_at IS NULL
            """, {"ono": order.get("order_no"), "prev": _prev_status})
        except Exception:
            pass

        # 3. Fire event_logger audit event
        try:
            from modules.backoffice.event_logger import log_event, EventType
            log_event(
                EventType.STATUS_CHANGED,
                order.get("order_no") or order.get("order_id", ""),
                details={
                    "from_status": _prev_status,
                    "to_status":   new_status,
                    "order_no":    order.get("order_no"),
                    "source":      "dashboard_manual",
                },
                source=_operator,
                remarks=f"Manual: {_prev_status} → {new_status}",
            )
        except Exception:
            pass

        # 4. Update in-memory state
        order["status"] = new_status
        order.setdefault("status_history", []).append({
            "timestamp": datetime.datetime.now().isoformat(),
            "status": new_status,
            "notes": f"Changed via dashboard by {_operator}",
        })
        try:
            from modules.backoffice.backoffice_helpers import load_orders_from_database
            load_orders_from_database.clear()
            st.session_state["bo_orders_loaded"] = False
        except Exception:
            pass
        return True
    except Exception as e:
        st.error(f"DB update failed: {e}")
        return False


# ── CANCEL ORDER ─────────────────────────────────────────────────────────────

# ── Cancellation rules imported from central business_rules ──────────────
from modules.core.business_rules import (
    CANCELLATION_REASONS        as _CANCEL_REASONS,
    CANCELLATION_BLOCKED_STATUSES as _CANCEL_BLOCKED_STATUSES,
    CANCELLATION_ALLOWED_STATUSES,
    STAGE_RELEASE_ALLOWED_FROM,
    STAGE_RELEASE_TARGET_STATUS,
    STAGE_RELEASE_REASONS,
    RETAIL_EDIT_LOCKED_AFTER,
    RETURN_ALLOWED_FROM_STATUSES,
    RETURN_REASONS,
)


def _order_has_active_jobs(order: dict) -> bool:
    """Return True if any open job_master rows exist for this order."""
    oid = str(order.get("id") or "")
    ono = str(order.get("order_no") or "")
    if not oid and not ono:
        return False
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT 1
            FROM job_master jm
            JOIN order_lines ol ON ol.id = jm.order_line_id
            JOIN orders o       ON o.id  = ol.order_id
            WHERE (o.id::text = %(oid)s OR o.order_no = %(ono)s)
              AND NOT jm.is_closed
            LIMIT 1
        """, {"oid": oid, "ono": ono})
        return bool(rows)
    except Exception:
        return False


def _order_has_active_supplier_orders(order: dict) -> bool:
    """Return True if any non-cancelled supplier orders exist for this order."""
    oid = str(order.get("id") or "")
    ono = str(order.get("order_no") or "")
    if not oid and not ono:
        return False
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT 1
            FROM supplier_orders so
            WHERE (so.customer_order_id = %(ono)s
               OR so.customer_order_id = %(oid)s)
              AND COALESCE(so.status, '') NOT IN ('CANCELLED', 'CLOSED')
            LIMIT 1
        """, {"oid": oid, "ono": ono})
        return bool(rows)
    except Exception:
        return False


def render_cancel_order_panel(order: dict) -> None:
    """
    Cancellation panel with role-based gating, stage rules, and refund recording.

    Stage rules:
      PENDING / UNDER_REVIEW / CONFIRMED  → BILLING + MANAGER + ADMIN
      IN_PRODUCTION / READY               → MANAGER + ADMIN only (job cards must be closed)
      BILLED / DISPATCHED / DELIVERED     → ADMIN only + Credit Note required
      CANCELLED (already)                 → Grey read-only history panel

    Cancelled orders remain visible but all editing is disabled.
    cancel_reason column is added lazily to orders table on first use.
    """
    from modules.security.roles import has_role, current_user
    from modules.sql_adapter import run_write as _rw_cx, run_query as _rq_cx
    import datetime as _dt_cx, uuid as _uuid_cx

    raw_status = str(order.get("status") or "PENDING").upper()
    order_no   = str(order.get("order_no") or "")
    party      = order.get("patient_name") or order.get("party_name") or "—"
    advance    = float(order.get("advance_amount") or order.get("advance") or 0)
    total_val  = float(order.get("total_value") or 0)

    _CANCEL_REASONS_FULL = [
        "— Select reason —",
        "Cancelled due to non-availability of stock",
        "Cancelled by Client / Party",
        "Cancelled — wrong prescription / entry error",
        "Cancelled — duplicate order",
        "Cancelled — customer changed mind",
        "Cancelled — price dispute",
        "Cancelled — delivery / delay issue",
        "Cancelled — product discontinued",
        "Other (specify below)",
    ]
    _BILLED_REASONS = [
        "— Select reason —",
        "Return & Cancel — wrong product delivered",
        "Return & Cancel — defective / damaged",
        "Return & Cancel — power mismatch",
        "Return & Cancel — customer rejected on delivery",
        "Return & Cancel — non-availability (no replacement)",
        "Cancelled by Client / Party after billing",
        "Return & Cancel — delivered too late",
        "Other (specify below)",
    ]
    _REFUND_MODES = ["Cash", "UPI / GPay / PhonePe", "NEFT / RTGS", "Card Reversal", "Store Credit / Wallet"]

    # ── Already cancelled — grey read-only history ────────────────────────
    if raw_status == "CANCELLED":
        with st.expander("🚫 Order Cancelled — History", expanded=False):
            st.markdown(
                "<div style='background:#1a1a1a;border:1px solid #33415533;"
                "border-radius:8px;padding:10px 14px;color:#475569;font-size:0.78rem'>"
                "This order is <b>cancelled</b>. No edits are possible. "
                "Use the sections below to view cancellation and refund history.</div>",
                unsafe_allow_html=True,
            )
            # Cancel reason
            try:
                _cx_rows = _rq_cx(
                    "SELECT cancel_reason, updated_at FROM orders WHERE order_no=%s LIMIT 1",
                    (order_no,)
                ) or []
                if _cx_rows and _cx_rows[0].get("cancel_reason"):
                    st.caption(f"Reason: {_cx_rows[0]['cancel_reason']}")
            except Exception:
                pass
            # Refund history
            try:
                _rfx = _rq_cx(
                    "SELECT refund_amount, refund_mode, refund_ref, refunded_by, refunded_at "
                    "FROM order_refunds WHERE order_no=%s ORDER BY refunded_at DESC",
                    (order_no,)
                ) or []
                if _rfx:
                    for r in _rfx:
                        st.markdown(
                            f"<div style='color:#10b981;font-size:0.78rem'>"
                            f"💰 Refund ₹{float(r['refund_amount']):,.2f} via {r['refund_mode']}"
                            + (f" · Ref: {r['refund_ref']}" if r.get("refund_ref") else "")
                            + f" · By {r.get('refunded_by','—')}</div>",
                            unsafe_allow_html=True
                        )
            except Exception:
                pass
            # Credit notes
            try:
                _cnx = _rq_cx(
                    """
                    SELECT cn_number AS cn_no, grand_total AS cn_amount, status
                    FROM credit_notes
                    WHERE order_id = (SELECT id FROM orders WHERE order_no=%s LIMIT 1)
                      AND COALESCE(is_deleted,FALSE)=FALSE
                    """,
                    (order_no,)
                ) or []
                if _cnx:
                    for cn in _cnx:
                        st.caption(
                            f"📄 Credit Note {cn['cn_no']} · "
                            f"₹{float(cn['cn_amount']):,.2f} · {cn['status']}"
                        )
            except Exception:
                pass
        return

    try:
        from modules.settings.shop_master import get_order_action_statuses
        if raw_status not in get_order_action_statuses("cancel"):
            return
    except Exception:
        if raw_status not in {"PENDING", "PROVISIONAL", "UNDER_REVIEW"}:
            return

    # ── Determine who can cancel at this stage ────────────────────────────
    _is_pre_prod   = raw_status in {"PENDING","PROVISIONAL","UNDER_REVIEW","HOLD","CREDIT_HOLD","PENDING_PAYMENT","CONFIRMED"}
    _is_in_prod    = raw_status in {"IN_PRODUCTION","READY"}
    _is_post_bill  = raw_status in {"BILLED","DISPATCHED","DELIVERED","CLOSED"}

    # Role gates
    _billing_ok = has_role("admin","manager","billing")
    _manager_ok = has_role("admin","manager")
    _admin_ok   = has_role("admin")

    if _is_pre_prod   and not _billing_ok:  return
    if _is_in_prod    and not _manager_ok:  return
    if _is_post_bill  and not _admin_ok:    return
    if not (_is_pre_prod or _is_in_prod or _is_post_bill): return

    # ── Expander label ────────────────────────────────────────────────────
    _exp_label = (
        "🚫 Cancel Order" if _is_pre_prod else
        "🚫 Cancel Order (Manager / Admin)" if _is_in_prod else
        "🚫 Return & Cancel + Credit Note (Admin only)"
    )

    with st.expander(_exp_label, expanded=False):

        # ── Stage warning ─────────────────────────────────────────────────
        if _is_in_prod:
            st.markdown(
                "<div style='background:#1a0f00;border-left:3px solid #f59e0b;"
                "padding:8px 12px;border-radius:4px;font-size:0.78rem;color:#94a3b8'>"
                "⚠️ Order is <b>in production</b>. Manager / Admin only. "
                "All job cards must be closed before cancellation.</div>",
                unsafe_allow_html=True
            )
        elif _is_post_bill:
            st.markdown(
                "<div style='background:#1a0a0a;border-left:3px solid #ef4444;"
                "padding:8px 12px;border-radius:4px;font-size:0.78rem;color:#94a3b8'>"
                "🔴 Order has been billed. Admin only. A <b>Credit Note</b> is required "
                "to reverse the invoice before cancellation is complete.</div>",
                unsafe_allow_html=True
            )

        # ── Live guard: job cards ─────────────────────────────────────────
        if _is_in_prod or _is_pre_prod:
            if _order_has_active_jobs(order):
                st.error(
                    "❌ Open job cards exist — close them in Production first. "
                    "Then return here to cancel."
                )
                return
            if _order_has_active_supplier_orders(order):
                st.error(
                    "❌ Active supplier orders exist — cancel them in Procurement first."
                )
                return

        # ── Reason ───────────────────────────────────────────────────────
        _reasons_list = _BILLED_REASONS if _is_post_bill else _CANCEL_REASONS_FULL
        _reason = st.selectbox(
            "Reason for cancellation",
            _reasons_list,
            key=f"bo_cancel_reason_{order_no}"
        )
        _other = ""
        if _reason == "Other (specify below)":
            _other = st.text_input(
                "Specify reason",
                key=f"bo_cancel_other_{order_no}",
                placeholder="Enter reason..."
            )
        _final_reason = _other.strip() if _reason == "Other (specify below)" else _reason

        # ── Credit Note flow for post-billed ─────────────────────────────
        _cn_session_key = f"_bo_cn_{order_no}"
        _existing_cn    = st.session_state.get(_cn_session_key, {})

        if _is_post_bill and not _existing_cn:
            st.markdown("**Step 1 — Raise Credit Note**")
            _c1, _c2 = st.columns(2)
            _cn_amt  = _c1.number_input(
                "Credit Note amount ₹",
                min_value=0.01, max_value=max(total_val, 0.01),
                value=total_val, step=1.0,
                key=f"bo_cn_amt_{order_no}"
            )
            _cn_type = _c2.radio(
                "Type", ["Full cancellation", "Partial return"],
                key=f"bo_cn_type_{order_no}", horizontal=True
            )
            if st.button(
                "📄 Raise Credit Note",
                key=f"bo_raise_cn_{order_no}",
                type="primary", use_container_width=True,
                disabled=(_final_reason in ("", "— Select reason —"))
            ):
                _cn_no = f"CN-{_dt_cx.date.today().strftime('%Y%m%d')}-{str(_uuid_cx.uuid4())[:6].upper()}"
                try:
                    _rw_cx("""
                        INSERT INTO credit_notes
                            (cn_number, order_id, party_name, grand_total,
                             reason, reason_detail, status, remarks, created_by,
                             created_at, updated_at)
                        VALUES (
                            %(cn)s,
                            (SELECT id FROM orders WHERE order_no=%(ono)s LIMIT 1),
                            %(party)s, %(amt)s,
                            LEFT(%(r)s, 30), %(r)s,
                            'DRAFT', %(remarks)s, %(user)s,
                            NOW(), NOW()
                        )
                        ON CONFLICT (cn_number) DO NOTHING
                    """, {
                        "cn": _cn_no, "ono": order_no, "party": party,
                        "amt": _cn_amt,
                        "r": _final_reason,
                        "remarks": "FULL" if "Full" in _cn_type else "PARTIAL",
                        "user": current_user() or "System",
                    })
                    st.session_state[_cn_session_key] = {"cn_no": _cn_no, "amount": _cn_amt}
                    st.success(f"📄 Credit Note {_cn_no} raised for ₹{_cn_amt:,.2f}")
                    st.rerun()
                except Exception as _cne:
                    st.error(f"Credit Note failed: {_cne}")
            return  # Don't show confirm until CN is raised

        # ── Refund section ────────────────────────────────────────────────
        _show_refund   = advance > 0 or _is_post_bill
        _refund_amount = 0.0
        _refund_mode   = ""
        _refund_ref    = ""

        if _existing_cn:
            _cn = _existing_cn
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #10b98133;"
                f"border-radius:6px;padding:8px 12px;margin-bottom:8px'>"
                f"<b style='color:#10b981'>📄 {_cn['cn_no']}</b> · ₹{_cn['amount']:,.2f}"
                f"</div>",
                unsafe_allow_html=True
            )
            st.markdown("**Step 2 — Process Refund & Confirm**")
            _show_refund = True

        if _show_refund:
            _max_refund = _existing_cn["amount"] if _existing_cn else advance
            _ra1, _ra2, _ra3 = st.columns(3)
            _refund_amount = _ra1.number_input(
                "Refund ₹", min_value=0.0,
                max_value=max(_max_refund, 0.0),
                value=_max_refund, step=1.0,
                key=f"bo_refund_amt_{order_no}"
            )
            _refund_mode = _ra2.selectbox(
                "Refund mode", _REFUND_MODES,
                key=f"bo_refund_mode_{order_no}"
            )
            _refund_ref = _ra3.text_input(
                "Ref / UTR",
                key=f"bo_refund_ref_{order_no}",
                placeholder="UTR / txn ID"
            )

        # ── Two-step confirm ──────────────────────────────────────────────
        _step2 = f"_bo_cancel_step2_{order_no}"
        _disabled_btn = (_final_reason in ("", "— Select reason —"))

        if not st.session_state.get(_step2):
            _btn_label = (
                "🚫 Cancel Order" if _is_pre_prod else
                "🚫 Cancel Order (Manager Override)" if _is_in_prod else
                "✅ Approve Credit Note & Cancel"
            )
            if st.button(
                _btn_label,
                key=f"bo_cancel_btn1_{order_no}",
                type="primary", use_container_width=True,
                disabled=_disabled_btn
            ):
                st.session_state[_step2] = True
                st.rerun()
        else:
            _confirm_txt = (
                f"Confirm cancellation of {order_no} for {party}. "
                f"Reason: {_final_reason}."
            )
            if _refund_amount > 0:
                _confirm_txt += f" Refund Rs.{_refund_amount:,.2f} via {_refund_mode}."
            if _existing_cn:
                _confirm_txt += f" CN {_existing_cn['cn_no']} will be approved."
            _confirm_txt += " This cannot be undone."
            st.warning(_confirm_txt)

            _yc, _nc = st.columns(2)
            with _yc:
                if st.button(
                    "✅ Yes, Confirm Cancellation",
                    key=f"bo_cancel_yes_{order_no}",
                    type="primary", use_container_width=True
                ):
                    # Ensure cancel_reason column
                    try:
                        _rw_cx("ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancel_reason TEXT")
                    except Exception:
                        pass

                    _audit = (
                        f"[{_dt_cx.datetime.now().strftime('%d-%b-%Y %H:%M')}] "
                        f"CANCELLED by {(current_user() or {}).get('name','backoffice')}: "
                        f"{_final_reason}"
                        + (f" | CN: {_existing_cn['cn_no']}" if _existing_cn else "")
                        + (f" | Refund Rs.{_refund_amount:,.2f} via {_refund_mode}"
                           if _refund_amount > 0 else "")
                    )
                    _ok = _save_status_change(order, "CANCELLED")
                    if _ok:
                        try:
                            _rw_cx(
                                "UPDATE orders SET cancel_reason=%(r)s WHERE order_no=%(o)s",
                                {"r": _audit, "o": order_no}
                            )
                        except Exception:
                            pass

                        # Record refund
                        if _refund_amount > 0:
                            try:
                                _rw_cx("""
                                    CREATE TABLE IF NOT EXISTS order_refunds (
                                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                        order_no TEXT, credit_note_no TEXT,
                                        refund_amount NUMERIC(12,2), refund_mode TEXT,
                                        refund_ref TEXT, refunded_by TEXT,
                                        refunded_at TIMESTAMPTZ DEFAULT NOW(), remarks TEXT
                                    )
                                """)
                                _rw_cx("""
                                    INSERT INTO order_refunds
                                        (order_no, credit_note_no, refund_amount,
                                         refund_mode, refund_ref, refunded_by, remarks)
                                    VALUES (%(ono)s, %(cn)s, %(amt)s,
                                            %(mode)s, %(ref)s, %(by)s, %(rmk)s)
                                """, {
                                    "ono": order_no,
                                    "cn": _existing_cn.get("cn_no","") if _existing_cn else "",
                                    "amt": _refund_amount, "mode": _refund_mode,
                                    "ref": _refund_ref or "",
                                    "by": (current_user() or {}).get("name","backoffice"),
                                    "rmk": _final_reason,
                                })
                            except Exception:
                                pass

                        # Approve CN
                        if _existing_cn:
                            try:
                                _rw_cx(
                                    "UPDATE credit_notes SET status='APPROVED', "
                                    "remarks=COALESCE(remarks,'') || %(note)s, "
                                    "updated_at=NOW() "
                                    "WHERE cn_number=%(cn)s",
                                    {
                                        "note": (
                                            f" | Approved during cancellation {order_no}"
                                            + (f" | Refund: {_refund_mode} {_refund_amount} {_refund_ref or ''}" if _refund_amount else "")
                                        ),
                                        "cn": _existing_cn["cn_no"]
                                    }
                                )
                            except Exception:
                                pass
                            st.session_state.pop(_cn_session_key, None)

                        # ── Stock reversal on cancel ─────────────────────────
                        try:
                            from modules.sql_adapter import run_query as _rq_stk, run_write as _rw_stk
                            _oid_cancel = str(order.get("id") or order.get("order_id") or "")
                            if _oid_cancel:
                                # Atomic: mark stock_reversed=TRUE only if not already done.
                                # RETURNING ensures idempotency — double-cancel = no duplicate restore.
                                _lines_to_reverse = _rq_stk("""
                                    UPDATE order_lines
                                    SET stock_reversed = TRUE
                                    WHERE order_id = %s::uuid
                                      AND COALESCE(is_deleted, FALSE) = FALSE
                                      AND COALESCE(billed_qty, 0) = 0
                                      AND COALESCE(stock_reversed, FALSE) = FALSE
                                    RETURNING
                                        id::text,
                                        product_id::text,
                                        COALESCE(allocated_qty, quantity, 0) AS qty,
                                        lens_params
                                """, (_oid_cancel,)) or []

                                for _lr in _lines_to_reverse:
                                    _lr_pid = str(_lr.get("product_id") or "")
                                    _lr_qty = int(_lr.get("qty") or 0)
                                    _lr_lp  = _lr.get("lens_params") or {}
                                    if isinstance(_lr_lp, str):
                                        import json as _jlr
                                        try: _lr_lp = _jlr.loads(_lr_lp)
                                        except: _lr_lp = {}
                                    _lr_bno = str(_lr_lp.get("batch_no") or "")
                                    if _lr_pid and _lr_qty > 0:
                                        try:
                                            # Per stock flow doc: cancel = release SOFT reservation only
                                            # allocated_qty ↓ — NOT quantity (only dispatch touches quantity)
                                            _rw_stk("""
                                                UPDATE inventory_stock
                                                SET allocated_qty = GREATEST(0, COALESCE(allocated_qty, 0) - %(qty)s)
                                                WHERE product_id = %(pid)s::uuid
                                                  AND (%(bno)s = '' OR batch_no = %(bno)s)
                                                LIMIT 1
                                            """, {"pid": _lr_pid, "bno": _lr_bno, "qty": _lr_qty})
                                        except Exception:
                                            pass  # best-effort
                        except Exception as _stk_err:
                            import logging
                            logging.warning(f"[Cancel] Stock reversal error: {_stk_err}")

                        # ── WhatsApp cancel notification ──────────────────────
                        import urllib.parse as _uparse_cx
                        _cx_party  = order.get("patient_name") or order.get("party_name") or "Customer"
                        _cx_mobile = str(order.get("patient_mobile") or order.get("party_mobile") or "")
                        _cx_mob_clean = "".join(x for x in _cx_mobile if x.isdigit())
                        if _cx_mob_clean.startswith("91") and len(_cx_mob_clean) == 12:
                            _cx_mob_clean = _cx_mob_clean[2:]
                        _cx_wa_mob = ("91" + _cx_mob_clean) if (len(_cx_mob_clean)==10 and _cx_mob_clean[0] in "6789") else ""
                        if _cx_wa_mob:
                            try:
                                from modules.settings.shop_master import get_unit_info as _gui_cx
                                _shop_cx = _gui_cx("retail") or {}
                                _shop_name_cx = _shop_cx.get("shop_name","DV Optical")
                            except Exception:
                                _shop_name_cx = "DV Optical"
                            _cx_reason_part = (f"Reason: {_final_reason}\n\n" if _final_reason and _final_reason != "\u2014 Select reason \u2014" else "")
                            _cx_refund_part  = (f"Refund of \u20b9{_refund_amount:,.0f} via {_refund_mode}.\n\n" if _refund_amount > 0 else "")
                            _cx_msg = (
                                f"Dear {_cx_party},\n\n"
                                f"Your order *{order_no}* has been *cancelled*.\n\n"
                                + _cx_reason_part
                                + _cx_refund_part
                                + f"For help, please contact us.\n\nThank you,\n{_shop_name_cx}"
                            )
                            _cx_wa_url = f"https://wa.me/{_cx_wa_mob}?text={_uparse_cx.quote(_cx_msg)}"
                            st.link_button(
                                "📲 Notify Customer via WhatsApp",
                                _cx_wa_url,
                                use_container_width=True
                            )

                        # Audit log the cancellation
                        try:
                            from modules.backoffice.audit_logger import audit, AuditAction
                            from modules.security.roles import current_user as _cu_cx
                            _can_user = (_cu_cx() or {}).get("name","backoffice")
                            audit(
                                AuditAction.STATUS_CHANGED,
                                entity    = "orders",
                                entity_id = str(order.get("id","")),
                                order_id  = str(order.get("id","")),
                                user_id   = _can_user,
                                payload   = {
                                    "action":        "order_cancelled",
                                    "order_no":      order_no,
                                    "reason":        _final_reason,
                                    "refund_amount": _refund_amount if _refund_amount > 0 else 0,
                                    "refund_mode":   _refund_mode if _refund_amount > 0 else "",
                                }
                            )
                        except Exception:
                            pass

                        st.session_state.pop(_step2, None)
                        st.success(
                            f"✅ Order {order_no} cancelled."
                            + (f" Refund ₹{_refund_amount:,.2f} recorded." if _refund_amount > 0 else "")
                            + " Stock reversed."
                        )
                        st.rerun()

            with _nc:
                if st.button("← Go Back", key=f"bo_cancel_no_{order_no}",
                             use_container_width=True):
                    st.session_state.pop(_step2, None)
                    st.rerun()


def _wa_message(order: dict, new_status: str) -> Optional[str]:
    """Return rich WhatsApp message with products + powers for WA-enabled stations."""
    import urllib.parse as _uparse
    station = next(
        (s for s in _STATIONS if s.get("status") == new_status and s.get("whatsapp")),
        None,
    )
    if not station:
        return None

    party    = order.get("patient_name") or order.get("party_name") or "Customer"
    order_no = order.get("order_no") or "your order"
    mobile   = str(order.get("patient_mobile") or order.get("party_mobile") or "")

    nl = "\n"
    try:
        from modules.settings.shop_master import get_unit_info as _gui
        _shop = _gui("retail") or {}
        shop_name  = _shop.get("shop_name","DV Optical")
        shop_phone = _shop.get("shop_phone","")
    except Exception:
        shop_name, shop_phone = "DV Optical", ""

    try:
        from modules.wa_hub import _line_product_name as _wa_prod_name
        from modules.wa_hub import _power_parts as _wa_power_parts
    except Exception:
        _wa_prod_name = None
        _wa_power_parts = None

    lines = order.get("lines") or []
    prod_block = ""
    for ln in lines:
        if not isinstance(ln, dict): continue
        eye   = str(ln.get("eye_side") or "").upper()
        pname = _wa_prod_name(ln) if _wa_prod_name else str(ln.get("product_name") or "")
        qty   = ln.get("billing_qty") or ln.get("quantity") or 0
        total = float(ln.get("billing_total") or ln.get("total_price") or 0)
        elbl  = {"R":"👁 Right","L":"👁 Left","B":"👁👁 Both"}.get(eye, "")
        prod  = pname
        pw_parts = _wa_power_parts(ln) if _wa_power_parts else []
        pw    = "  ".join(pw_parts)
        row   = []
        if elbl: row.append("*" + elbl + "*")
        if prod: row.append(prod)
        if qty and int(qty) > 0: row.append("Qty:" + str(qty))
        if pw:   row.append("[" + pw + "]")
        if total > 0: row.append("Rs." + "{:,.0f}".format(total))
        if row:  prod_block += "  ".join(row) + nl

    # Expected supply date/window. CS override wins over planned schedule.
    esd = order.get("cs_expected_supply_date") or order.get("expected_supply_date")
    esw = (
        str(order.get("cs_expected_supply_window") or "").strip()
        if order.get("cs_expected_supply_date") else ""
    ) or str(order.get("expected_supply_window") or "").strip()
    esd_str = ""
    if esd:
        try:
            from datetime import date, datetime
            if hasattr(esd, "strftime"):
                esd_str = esd.strftime("%d %b %Y")
            else:
                esd_str = str(esd)[:10]
            if esw and esw != "To be confirmed":
                esd_str += " · " + esw
        except Exception:
            esd_str = str(esd)

    # Compose rich message per status
    if new_status == "CONFIRMED":
        m  = "Hello " + party + " 👋" + nl + nl
        m += "🏪 *" + shop_name + "*" + nl
        m += "Thanks for your order." + nl
        m += "Your order is *Confirmed*." + nl
        m += "📋 Order Number: *" + order_no + "*" + nl
        if prod_block:
            m += nl + "📦 *Details of Order:*" + nl + prod_block
        if esd_str:
            m += nl + "📅 Expected Date of Supply: *" + esd_str + "*" + nl
        if shop_phone: m += "Queries: " + shop_phone + nl
        m += nl + "Thanks for Choosing Parakh Opticals for your Supplies"

    elif new_status == "READY":
        m  = "Hello " + party + " 👋" + nl + nl
        m += "🎉 *Your order is Ready!*" + nl
        m += "📋 Order: *" + order_no + "*" + nl
        m += "🏪 " + shop_name + nl
        if prod_block:
            m += nl + "📦 *Items Ready:*" + nl + prod_block
        m += nl + "Please collect at your convenience." + nl
        if shop_phone: m += "Queries: " + shop_phone + nl
        m += nl + "Thank you! 🙏 " + shop_name

    elif new_status == "DISPATCHED":
        m  = "Hello " + party + " 👋" + nl + nl
        m += "🚚 *Your order is on its way!*" + nl
        m += "📋 Order: *" + order_no + "*" + nl
        m += "🏪 " + shop_name + nl
        if prod_block:
            m += nl + "📦 *Items Dispatched:*" + nl + prod_block
        m += nl + "We will contact you for delivery." + nl
        if shop_phone: m += "Queries: " + shop_phone + nl
        m += nl + "Thank you! 🙏 " + shop_name

    else:
        # Fallback for other statuses
        tmpl = station.get("wa_msg", "Order {order_no} status: " + new_status)
        m    = tmpl.format(party=party, order_no=order_no)

    return m


def _wa_url(mobile: str, msg: str) -> str:
    import urllib.parse as _up
    c = "".join(x for x in (mobile or "") if x.isdigit())
    if len(c) == 10: c = "91" + c
    return "https://wa.me/{}?text={}".format(c, _up.quote(msg)) if c else ""


# ══════════════════════════════════════════════════════════════
# PRODUCTION TRAIN RENDERERS  (self-contained, no external import)
# ══════════════════════════════════════════════════════════════

def _train_mini(order: dict) -> None:
    """
    Single-line dot train shown inside each dashboard order card.

    📥 ─── ✅ ─── ⚙️ ─── 📦 ─── 🧾 ─── 🚚 ─── ✅ ─── 🔒
         Confirmed  (active label shown only for current)
    """
    status = _determine_workflow_status(order, order.get("status") or "PENDING", _order_lines(order))

    if status == "CANCELLED":
        st.markdown(
            "<span style='color:#ef4444;font-size:0.72rem;font-weight:700'>❌ CANCELLED</span>",
            unsafe_allow_html=True,
        )
        return

    cur = _current_station_idx(order)

    dots = []
    for i, s in enumerate(_MAIN_TRACK):
        done   = i < cur
        active = i == cur
        future = i > cur

        bg    = s["color"] if (done or active) else "#1e293b"
        fg    = "#fff"    if (done or active) else "#334155"
        icon  = "✓"       if done else s["icon"]
        ring  = f"box-shadow:0 0 0 2px {s['color']};" if active else ""
        size  = "0.9rem"  if active else "0.72rem"

        # Current station gets a name tag below the dot
        name_tag = (
            f"<div style='font-size:0.55rem;color:{s['color']};"
            f"text-align:center;margin-top:2px;white-space:nowrap;"
            f"font-weight:700'>{s['short']}</div>"
            if active else ""
        )

        # WhatsApp badge on active station if it sends a message
        wa_tag = (
            "<div style='font-size:0.5rem;color:#25d366;"
            "text-align:center;white-space:nowrap'>📱 WA</div>"
            if (active and s.get("whatsapp")) else ""
        )

        dots.append(
            f"<div style='display:inline-flex;flex-direction:column;"
            f"align-items:center;min-width:32px'>"
            f"<div style='background:{bg};color:{fg};width:22px;height:22px;"
            f"border-radius:50%;display:inline-flex;align-items:center;"
            f"justify-content:center;font-size:{size};{ring}'>{icon}</div>"
            f"{name_tag}{wa_tag}</div>"
        )

        # Connector line to next station
        if i < len(_MAIN_TRACK) - 1:
            lc = s["color"] if done else ("#334155" if future else s["color"])
            # Pulsing dot on active connector
            connector = (
                f"<div style='display:inline-flex;align-items:center;"
                f"margin-bottom:11px;gap:1px'>"
                f"<div style='width:10px;height:2px;background:{lc}'></div>"
                f"<div style='width:4px;height:4px;border-radius:50%;"
                f"background:{lc if done else '#334155'}'></div>"
                f"<div style='width:10px;height:2px;background:{lc}'></div>"
                f"</div>"
            )
            dots.append(connector)

    st.markdown(
        "<div style='display:flex;align-items:flex-start;flex-wrap:nowrap;"
        "overflow-x:auto;padding:6px 0 2px 0;gap:0'>"
        + "".join(dots) +
        "</div>",
        unsafe_allow_html=True,
    )

    # ── For IN_PRODUCTION orders: show actual job/supplier stage below the train ──
    if status in ("IN_PRODUCTION", "READY"):
        try:
            from modules.backoffice.production_train import render_train_sidebar
            render_train_sidebar(str(order.get("order_no") or ""))
        except Exception:
            pass


def _train_full(order: dict) -> None:
    """
    Full-width station-by-station train, shown inside the order detail
    (order_status_window) and the inline expanded card view.
    Each station gets a column: circle on top, label below, WA badge if applicable.
    Sub-stations (Supplier PO, Courier) appear as inset pills under their parent.
    """
    status = order.get("status") or "PENDING"

    if status == "CANCELLED":
        st.markdown(
            "<div style='background:#ef444420;border:1px solid #ef4444;"
            "border-radius:8px;padding:10px 16px;color:#ef4444;"
            "font-weight:700;text-align:center'>❌ ORDER CANCELLED</div>",
            unsafe_allow_html=True,
        )
        return

    cur          = _current_station_idx(order)
    show_po      = _has_supplier_po(order)
    show_courier = _has_courier(order)

    # Build display list — inject sub-stations where needed
    display = []
    for i, station in enumerate(_MAIN_TRACK):
        display.append({"s": station, "i": i, "sub": False})
        if station["id"] == "IN_PRODUCTION" and show_po:
            sub = next((x for x in _STATIONS if x.get("id") == "SUPPLIER_PO"), None)
            if sub:
                display.append({"s": sub, "i": i, "sub": True})
        if station["id"] == "DISPATCHED" and show_courier:
            sub = next((x for x in _STATIONS if x.get("id") == "COURIER_ENTRY"), None)
            if sub:
                display.append({"s": sub, "i": i, "sub": True})

    cols = st.columns(len(display))

    for col, item in zip(cols, display):
        station  = item["s"]
        main_idx = item["i"]
        is_sub   = item["sub"]
        done     = main_idx < cur
        active   = main_idx == cur
        scale    = "22px" if is_sub else "30px"
        bg       = station["color"] if (done or active) else "#1e293b"
        fg       = "#fff"          if (done or active) else "#475569"
        ring     = f"box-shadow:0 0 0 3px {station['color']}55;" if active else ""
        icon     = "✓" if done else station["icon"]
        lbl_col  = "#e2e8f0" if (done or active) else "#475569"
        lbl_size = "0.6rem"  if is_sub else "0.7rem"

        wa_badge = (
            f"<div style='font-size:0.55rem;color:#25d366;margin-top:2px;"
            f"text-align:center'>📱 WhatsApp</div>"
            if (station.get("whatsapp") and active) else ""
        )

        with col:
            st.markdown(
                f"<div style='text-align:center;padding:4px 2px'>"
                f"<div style='background:{bg};color:{fg};width:{scale};height:{scale};"
                f"border-radius:50%;display:flex;align-items:center;"
                f"justify-content:center;margin:0 auto;"
                f"font-size:{'0.7rem' if is_sub else '1rem'};{ring}'>{icon}</div>"
                f"<div style='font-size:{lbl_size};color:{lbl_col};margin-top:4px;"
                f"line-height:1.3;word-break:break-word'>{station['short']}</div>"
                f"{wa_badge}"
                f"</div>",
                unsafe_allow_html=True,
            )


# ══════════════════════════════════════════════════════════════
# ORDER CARD
# ══════════════════════════════════════════════════════════════

def _live_billing_badge(order_no: str) -> str:
    """
    Query challans + invoices for this order_no.
    Returns HTML showing live billing status:
      - Not yet billed
      - Challan created (challan_no, status, amount)
      - Invoiced (invoice_no, status, amount)
      - Partial (some lines challan, some invoice)
    """
    try:
        from modules.sql_adapter import run_query as _rqb
        _rows = _rqb("""
            SELECT 'CHALLAN'  AS doc_type,
                   c.challan_no  AS doc_no,
                   c.status,
                   c.grand_total,
                   c.challan_date::date AS doc_date
            FROM challans c
            WHERE %(ono)s = ANY(c.order_ids)
              AND c.status NOT IN ('CANCELLED','VOID')
              AND COALESCE(c.grand_total,0) > 0
              AND EXISTS (
                  SELECT 1 FROM challan_lines cl
                  WHERE cl.challan_id = c.id
                    AND NOT COALESCE(cl.is_deleted, FALSE)
              )
            UNION ALL
            SELECT 'INVOICE'  AS doc_type,
                   i.invoice_no AS doc_no,
                   i.status,
                   i.grand_total,
                   i.invoice_date::date AS doc_date
            FROM invoices i
            WHERE %(ono)s = ANY(i.order_ids)
              AND i.status NOT IN ('CANCELLED','VOID')
              AND COALESCE(i.grand_total,0) > 0
              AND EXISTS (
                  SELECT 1 FROM challan_lines cl
                  WHERE cl.challan_id = i.challan_id
                    AND NOT COALESCE(cl.is_deleted, FALSE)
              )
            ORDER BY doc_date DESC
        """, {"ono": order_no}) or []
    except Exception:
        _rows = []

    if not _rows:
        return (
            "<div style='padding:4px 8px;color:#475569;font-size:0.7rem'>"
            "💳 Not yet billed</div>"
        )

    parts = []
    for r in _rows:
        doc   = r.get("doc_type", "")
        no    = r.get("doc_no", "—")
        st_v  = (r.get("status") or "").upper()
        amt   = float(r.get("grand_total") or 0)

        # Color + icon per doc type and status
        if doc == "INVOICE":
            color = "#059669"; icon = "🧾"
        else:
            color = "#8b5cf6"; icon = "📋"

        if st_v in ("PAID", "CLOSED"):
            color = "#10b981"
        elif st_v in ("PENDING", "DRAFT"):
            color = "#f59e0b"

        parts.append(
            f"<span style='background:{color}22;border:1px solid {color}55;"
            f"color:{color};padding:2px 8px;border-radius:10px;"
            f"font-size:0.68rem;font-weight:700;white-space:nowrap'>"
            f"{icon} {no}"
            f"<span style='font-weight:400;opacity:.8'> · {st_v} · ₹{amt:,.0f}</span>"
            f"</span>"
        )

    # Partial flag: >1 doc = partial across docs
    prefix = ""
    if len(_rows) > 1:
        prefix = "<span style='color:#f59e0b;font-size:0.65rem;margin-right:6px'>⚡ Partial</span>"

    return (
        f"<div style='display:flex;flex-wrap:wrap;align-items:center;"
        f"gap:4px;padding:3px 0'>"
        f"{prefix}"
        + "".join(parts)
        + "</div>"
    )


def _determine_workflow_status(order, actual_status, lines):
    """Delegate to canonical get_live_status for all status resolution."""
    try:
        from modules.backoffice.order_status_live import get_live_status
        return get_live_status(order)
    except Exception:
        pass
    # Fallback inline logic
    """
    Determine the correct display status based on workflow logic.

    RULES (train order):
      1. ALWAYS trust the DB status first — it is the source of truth.
      2. Override to BILLED only if actual billing documents (challan/invoice) exist.
      3. Map legacy/transient states to their canonical train station.
      4. Never fabricate status from line data alone (was causing false CONFIRMED/BILLED).
    """
    # ── Canonical alias map (legacy / transient → train station) ──────────
    _ALIAS = {
        "PENDING_VALIDATION": "PENDING",
        "PROVISIONAL":        "PENDING",
        "UNDER_REVIEW":       "UNDER_REVIEW",
        "ORDER_SAVED":        "PENDING",
        "READY_FOR_BILLING":  "READY",
    }
    canonical = _ALIAS.get(actual_status, actual_status)

    # ── Terminal states that don't need doc verification ─────────────────
    if canonical in ("DISPATCHED", "DELIVERED", "CLOSED", "CANCELLED"):
        return canonical

    # ── BILLED must ALWAYS be verified against actual billing documents ───
    # DB status="BILLED" can be stale (set manually or by old bug).
    # Source of truth is challan/invoice tables — same logic as billing panel.
    # challan_preview stores order_ids as order_no strings (e.g. "PO-52F622B1")
    # while challan_invoice_manager stores UUIDs — check both to cover all paths.
    try:
        from modules.sql_adapter import run_query as _rq
        _oid = str(order.get("id") or "")
        _ono = str(order.get("order_no") or "")
        if _oid or _ono:
            _rows = _rq("""
                SELECT 1 FROM challans
                WHERE (
                    order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR order_ids::text[] @> ARRAY[%(ono)s::text]
                )
                AND status NOT IN ('CANCELLED','VOID')
                AND COALESCE(grand_total,0) > 0
                AND EXISTS (
                    SELECT 1 FROM challan_lines cl
                    WHERE cl.challan_id = challans.id
                      AND NOT COALESCE(cl.is_deleted, FALSE)
                )
                UNION ALL
                SELECT 1 FROM invoices
                WHERE (
                    order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR order_ids::text[] @> ARRAY[%(ono)s::text]
                )
                AND status NOT IN ('CANCELLED','VOID')
                AND COALESCE(grand_total,0) > 0
                AND EXISTS (
                    SELECT 1 FROM challan_lines cl
                    WHERE cl.challan_id = invoices.challan_id
                      AND NOT COALESCE(cl.is_deleted, FALSE)
                )
                LIMIT 1
            """, {"oid": _oid, "ono": _ono}) or []
            if _rows:
                return "BILLED"
    except Exception:
        pass
    # No billing documents found — if DB says BILLED, downgrade to READY
    if canonical == "BILLED":
        return "READY"

    # ── CONFIRMED without backoffice save → UNDER_REVIEW ────────────────
    # If DB says CONFIRMED but there is no order_status_history entry
    # showing a manual CONFIRMED transition (from backoffice), it was
    # auto-confirmed by old persistence code — treat as UNDER_REVIEW.
    if canonical == "CONFIRMED":
        try:
            from modules.sql_adapter import run_query as _rq2
            _oid2 = str(order.get("id") or "")
            _ono2 = str(order.get("order_no") or "")
            _hist = _rq2("""
                SELECT 1 FROM order_status_history h
                JOIN orders o ON o.id = h.order_id
                WHERE (o.id = %(oid)s::uuid OR o.order_no = %(ono)s)
                  AND h.to_status = 'CONFIRMED'
                LIMIT 1
            """, {"oid": _oid2, "ono": _ono2}) or []
            if not _hist:
                return "UNDER_REVIEW"
        except Exception:
            pass

    # ── Return the canonical DB status — no fabrication ───────────────────
    return canonical if canonical else "PENDING"


def _is_order_ready_for_billing(lines):
    """Check if order is ready for billing"""
    if not lines:
        return False
    
    # SERVICE lines (consultation fee) are auto-allocated — skip them
    _chk_lines = [l for l in lines if str(l.get("eye_side","")).upper() not in ("SERVICE", "S")]

    def _line_is_fulfilled(l):
        """
        True when a line's stock/allocation is satisfied.
        batch_no = SKU picked from inventory (primary signal for frames).
        allocated_qty = fallback for lenses recorded in DB.
        """
        _lp = l.get("lens_params") or {}
        _lp = _lp if isinstance(_lp, dict) else {}
        _bn = str(l.get("batch_no") or _lp.get("batch_no") or "").strip()
        if _bn:
            return True
        return int(l.get("allocated_qty") or 0) >= int(l.get("billing_qty") or l.get("quantity") or 0)

    # Check if all product lines are allocated
    _is_fully_alloc = all(_line_is_fulfilled(l) for l in _chk_lines)

    # Check if all lines have pricing
    _is_priced = all(float(l.get("unit_price") or 0) > 0 for l in _chk_lines)
    
    return _is_fully_alloc and _is_priced




def _production_stage_summary_for_card(order_no: str) -> str:
    """Read-only compact production/supplier/stock stage for backoffice order cards."""
    if not order_no:
        return ""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                ol.eye_side,
                COALESCE(ol.lens_params->>'manufacturing_route','') AS route,
                COALESCE(ol.lens_params->>'supplier_stage','') AS supplier_stage,
                COALESCE(ol.lens_params->>'batch_status','') AS batch_status,
                jm.current_stage,
                jm.is_closed
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN job_master jm ON jm.order_line_id = ol.id
            WHERE o.order_no = %(ono)s
              AND COALESCE(ol.is_deleted,FALSE)=FALSE
              AND COALESCE(ol.is_service_line,FALSE)=FALSE
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
            ORDER BY CASE WHEN ol.eye_side='R' THEN 0 WHEN ol.eye_side='L' THEN 1 ELSE 2 END
            LIMIT 12
        """, {"ono": order_no}) or []
    except Exception:
        return ""
    if not rows:
        return ""
    labels = {
        "JOB_CREATED":"Job Created", "JOB_PRINTED":"Printed", "PRINTED":"Printed",
        "PRODUCTION_PICKED":"In Production", "PRODUCTION_DONE":"Production Done",
        "INSPECTION":"Inspection", "HARDCOAT_PICKED":"Hardcoat", "HARDCOAT_DONE":"Hardcoat Done",
        "COLOURING_PICKED":"Colouring", "COLOURING_DONE":"Colour Done",
        "ARC_SENT":"ARC Sent", "ARC_RECEIVED":"ARC Received", "FINAL_QC":"Final QC",
        "FITTING_PENDING":"Fitting Pending", "FITTING_SENT":"Fitting Sent",
        "FITTING_RECEIVED":"Fitting Received", "FITTING_DONE":"Fitting Done",
        "READY_FOR_PACK":"Ready for Pack", "READY_TO_BILL":"Ready to Bill",
        "READY_FOR_BILLING":"Ready for Billing", "REJECTED":"Rejected",
    }
    parts=[]
    for r in rows:
        eye=str(r.get('eye_side') or '').upper()
        eye_l={"R":"RE","L":"LE"}.get(eye, eye or "ITEM")
        route=str(r.get('route') or '').upper()
        stg=str(r.get('current_stage') or '').upper()
        sup=str(r.get('supplier_stage') or '').upper()
        batch=str(r.get('batch_status') or '').upper()
        if stg:
            parts.append(f"{eye_l}: {labels.get(stg, stg)}")
        elif sup:
            parts.append(f"{eye_l}: Supplier {labels.get(sup, sup)}")
        elif route == 'STOCK' or batch == 'ALLOCATED':
            parts.append(f"{eye_l}: Stock Allocated")
        elif route:
            parts.append(f"{eye_l}: {route.title()}")
    if not parts:
        return ""
    return " · ".join(parts[:4])


_PROCUREMENT_READY_STATES = {
    "PROCURED",
    "PURCHASE_ACKED",
    "RECEIVED",
    "READY_FOR_BILLING",
    "READY_TO_BILL",
}


def _line_procurement_required(line: dict) -> bool:
    """True for customer lines that must have supplier procurement before billing."""
    eye = str(line.get("eye_side") or "").upper()
    if eye in ("S", "SERVICE"):
        return False
    if bool(line.get("is_service_line")):
        return False
    route = str(resolve_line_route(line) or "").upper()
    if route in ("VENDOR", "EXTERNAL_LAB"):
        return True
    lp = line.get("lens_params") or {}
    if not isinstance(lp, dict):
        lp = {}
    supplier_markers = (
        lp.get("supplier_id"),
        lp.get("supplier_name"),
        lp.get("supplier_stage"),
        lp.get("supplier_order_id"),
        lp.get("supplier_order_no"),
        lp.get("replenishment_status"),
    )
    return any(bool(x) for x in supplier_markers)


def _line_is_procured(line: dict) -> bool:
    if str(line.get("procurement_pa_id") or "").strip():
        return True
    if str(line.get("procurement_audit_status") or "").upper() == "LINKED_PROCUREMENT":
        return True
    return False


def _order_procurement_summary(order: dict) -> dict:
    required = [l for l in _order_lines(order) if _line_procurement_required(l)]
    procured = [l for l in required if _line_is_procured(l)]
    pending = [l for l in required if not _line_is_procured(l)]
    return {
        "required": len(required),
        "procured": len(procured),
        "pending": len(pending),
        "procured_lines": procured,
        "pending_lines": pending,
    }


def _order_has_unprocured_lines(order: dict) -> bool:
    return _order_procurement_summary(order)["pending"] > 0


def _procurement_badge_html(order: dict) -> str:
    summary = _order_procurement_summary(order)
    req = summary["required"]
    if not req:
        return ""
    pending = summary["pending"]
    procured = summary["procured"]
    if pending:
        color = "#f59e0b"
        text = f"⚠️ Unprocured {pending}/{req}"
        tip_line = summary["pending_lines"][0] if summary["pending_lines"] else {}
        product = html.escape(str(tip_line.get("product_name") or "RX line")[:44])
        detail = f" · {product}"
    else:
        color = "#10b981"
        line = summary["procured_lines"][0] if summary["procured_lines"] else {}
        supplier = html.escape(str(line.get("procurement_supplier_name") or "Supplier")[:34])
        inv = html.escape(str(line.get("procurement_invoice_no") or line.get("procurement_challan_no") or "PA")[:24])
        dt = str(line.get("procurement_document_date") or line.get("procurement_acknowledged_at") or "")[:10]
        text = f"✅ Procured {procured}/{req}"
        detail = f" · {supplier} · {inv}" + (f" · {dt}" if dt else "")
    return (
        f"<div style='margin-top:5px;background:{color}18;border:1px solid {color}66;"
        f"border-radius:6px;padding:3px 8px;display:inline-block'>"
        f"<span style='color:{color};font-size:0.72rem;font-weight:800'>{text}</span>"
        f"<span style='color:#cbd5e1;font-size:0.68rem'>{detail}</span>"
        f"</div>"
    )


def _procurement_line_text(line: dict) -> str:
    if not _line_procurement_required(line):
        return ""
    if _line_is_procured(line):
        supplier = str(line.get("procurement_supplier_name") or "Supplier")
        inv = str(line.get("procurement_invoice_no") or line.get("procurement_challan_no") or "PA")
        dt = str(line.get("procurement_document_date") or line.get("procurement_acknowledged_at") or "")[:10]
        sref = str(line.get("procurement_supplier_order_ref") or "").strip()
        return "Procured: " + " · ".join(x for x in [supplier, inv, dt, f"Supplier ref {sref}" if sref else ""] if x)
    return "Unprocured: supplier invoice / PA not linked yet"

def _render_order_card(order: dict, idx: int) -> None:
    order_id   = get_display_order_id(order)
    status     = order.get("status") or "PENDING"
    party      = order.get("patient_name") or order.get("party_name") or "—"
    order_type = (order.get("order_type") or "RETAIL").upper()
    order_source = str(order.get("order_source") or order_type or "").upper()
    days       = _days_old(order.get("order_date") or order.get("created_at"))
    date_str   = str(order.get("order_date") or order.get("created_at") or "")[:10]
    lines      = _order_lines(order)

    # Determine correct display status based on workflow logic
    display_status = _determine_workflow_status(order, status, lines)
    sc         = _STATUS_COLOR.get(display_status, "#64748b")
    si         = _STATUS_ICON.get(display_status, "•")
    # Use display_status for rendering (not raw status) so label matches reality
    status     = display_status
    tc         = _TYPE_COLOR.get(order_type, "#64748b")
    _src_badges = {
        "RETAIL": ("Retail", "#22c55e"),
        "WHOLESALE": ("Wholesale", "#3b82f6"),
        "BULK": ("Bulk", "#f59e0b"),
        "ONLINE": ("Online", "#ec4899"),
        "RETAILER_PORTAL": ("Retailer", "#8b5cf6"),
    }
    _src_label, _src_color = _src_badges.get(order_source, (order_source.title() if order_source else order_type, "#64748b"))
    source_badge = (
        f"<span style='background:{_src_color}22;color:{_src_color};border:1px solid {_src_color}66;"
        f"padding:2px 10px;border-radius:8px;font-size:0.66rem;font-weight:800;letter-spacing:.03em'>"
        f"{_src_label}</span>"
    )
    routes     = _route_summary(lines)
    prod_stage_summary = _production_stage_summary_for_card(str(order.get("order_no") or get_display_order_id(order) or ""))
    prod_stage_html = (
        f"<div style='margin-top:5px;background:#0c1a3a;border-radius:6px;padding:3px 8px;display:inline-block'>"
        f"<span style='color:#38bdf8;font-size:0.75rem;font-weight:800'>⚙️ {prod_stage_summary}</span></div>"
        if prod_stage_summary else ""
    )
    procurement_html = _procurement_badge_html(order)
    # SERVICE lines (consultation fee) auto-allocated — exclude from alloc display
    _p_lines   = [l for l in lines if str(l.get("eye_side","")).upper() not in ("SERVICE", "S")]
    alloc      = sum(int(l.get("allocated_qty") or 0) for l in _p_lines)
    billed     = sum(int(l.get("billing_qty") or l.get("quantity") or 0) for l in _p_lines)
    alloc_pct  = int(100 * alloc / billed) if billed else (100 if not _p_lines else 0)
    # Live billing total — box logic aware (same engine as order_status_window)
    # total_value is pre-computed in backoffice_helpers at load time — use it directly.
    # This avoids recomputing price logic in the card (single source of truth).
    grand_val = float(order.get("total_value") or 0)
    if not grand_val:
        # Fallback: recompute from unit_price via governor (GST-correct)
        try:
            from modules.core.price_qty_governor import compute_line_gst as _clg
            _ot_card = str(order.get("order_type") or "RETAIL").upper()
            grand_val = sum(
                _clg(
                    float(_l.get("unit_price") or 0),
                    int(_l.get("billing_qty") or _l.get("quantity") or 0),
                    float(_l.get("gst_percent") or 0),
                    _ot_card
                )["grand_total"]
                for _l in lines
                if not _l.get("is_deleted")
            )
        except Exception:
            grand_val = sum(
                float(_l.get("billing_total") or _l.get("total_price") or 0)
                for _l in lines
            )
    is_urgent  = days > 7 and status not in _TERMINAL

    # Power + product summary for blank space in card
    _pw_parts = []
    for _l in sorted(lines, key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1):
        _e = str(_l.get("eye_side","")).upper()
        _es = "R" if _e in ("R","RIGHT") else "L" if _e in ("L","LEFT") else ""
        if not _es: continue
        _ec = "#ef4444" if _es=="R" else "#60a5fa"
        _pn = str(_l.get("product_name","") or "")
        _sw = _l.get("sph"); _cw = _l.get("cyl"); _aw = _l.get("axis"); _dw = _l.get("add_power")
        _pw_str = ""
        try:
            _pw_parts_inner = []
            if _sw is not None: _pw_parts_inner.append(f"SPH {float(_sw):+.2f}")
            if _cw is not None: _pw_parts_inner.append(f"CYL {float(_cw):+.2f}")
            if _aw is not None: _pw_parts_inner.append(f"AX {int(float(_aw))}")
            if _dw is not None and float(_dw): _pw_parts_inner.append(f"ADD {float(_dw):+.2f}")
            _pw_str = "  ".join(_pw_parts_inner)
        except Exception:
            pass
        _pw_parts.append(
            f"<span style='color:{_ec};font-weight:800;font-size:0.7rem'>{_es}</span> "
            f"<span style='color:#e2e8f0;font-size:0.72rem;font-weight:600'>{_pn}</span>"
            + (f" <span style='color:#7dd3fc;font-size:0.68rem;font-family:monospace'>{_pw_str}</span>" if _pw_str else "")
        )
    _power_summary_html = (
        "<div style='margin-top:6px;padding:5px 8px;background:#0f172a;border-radius:5px;"
        "border:1px solid #1e293b;display:flex;flex-direction:column;gap:3px'>"
        + "".join(f"<div>{p}</div>" for p in _pw_parts)
        + "</div>"
    ) if _pw_parts else ""
    is_overdue = days > 3 and not is_urgent and status not in _TERMINAL
    border_col = "#ef4444" if is_urgent else ("#f59e0b" if is_overdue else sc)
    suggestion = _suggest_next_status(order, lines)

    # Route pills
    route_pills = "".join(
        f"<span style='background:{_ROUTE_COLOR.get(r,'#64748b')}20;"
        f"border:1px solid {_ROUTE_COLOR.get(r,'#64748b')}55;"
        f"color:{_ROUTE_COLOR.get(r,'#64748b')};padding:2px 9px;"
        f"border-radius:12px;font-size:0.65rem;font-weight:700;margin-right:4px'>"
        f"{_ROUTE_LABEL.get(r,r)} {cnt}</span>"
        for r, cnt in sorted(routes.items(), key=lambda x: -x[1])
    ) or "<span style='color:#64748b;font-size:0.65rem'>No lines yet</span>"

    urgency_badge = (
        "<span style='background:#ef4444;color:#fff;padding:1px 7px;"
        "border-radius:8px;font-size:0.62rem;font-weight:700;margin-left:6px'>🔴 URGENT</span>"
        if is_urgent else (
        "<span style='background:#f59e0b;color:#fff;padding:1px 7px;"
        "border-radius:8px;font-size:0.62rem;font-weight:700;margin-left:6px'>⚠️ OVERDUE</span>"
        if is_overdue else "")
    )
    alloc_color = "#10b981" if alloc_pct == 100 else ("#f59e0b" if alloc_pct > 0 else "#94a3b8")

    # ── Card header HTML ─────────────────────────────────────────────────────
    st.markdown(
        f"<div style='background:linear-gradient(135deg,#0f172a,#1e293b);"
        f"border-radius:10px;padding:14px 18px;margin-bottom:2px;"
        f"border-left:4px solid {border_col}'>"
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start'>"
        f"<div style='flex:1'>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap'>"
        f"<span style='color:#f1f5f9;font-weight:800;font-family:monospace;font-size:0.95rem'>{get_display_label(order)}</span>"
        f"<span style='background:{tc};color:#fff;padding:2px 10px;border-radius:8px;"
        f"font-size:0.68rem;font-weight:800;letter-spacing:.05em'>{order_type}</span>"
        f"{source_badge}"
        f"{urgency_badge}</div>"
        f"<div style='color:#cbd5e1;font-size:0.85rem;font-weight:600;margin-top:3px'>{party}</div>"
        f"<div style='margin-top:6px'>{route_pills}</div>"
        f"{prod_stage_html}"
        f"{procurement_html}"
        f"{_power_summary_html}"
        f"</div>"
        f"<div style='text-align:right;min-width:130px'>"
        f"<div style='background:{sc};color:#fff;padding:3px 12px;"
        f"border-radius:16px;font-size:0.78rem;font-weight:700;display:inline-block'>"
        f"{si} {_STATUS_DISPLAY.get(status, status.replace(chr(95), chr(32)).title())}</div>"
        f"<div style='color:#64748b;font-size:0.65rem;margin-top:4px'>{date_str} · {days}d ago</div>"
        f"<div style='color:{alloc_color};font-size:0.68rem;margin-top:2px'>"
        f"Alloc {alloc_pct}% · {len(lines)} line{'s' if len(lines)!=1 else ''}"
        f"{'  ·  ₹'+f'{grand_val:,.0f}' if grand_val else ''}</div>"
        f"</div></div></div>",
        unsafe_allow_html=True,
    )

    # ── Production train (mini) ───────────────────────────────────────────────
    _train_mini(order)

    # ── Dual status badge (backoffice status + production engine stage) ──────
    try:
        from modules.backoffice.production_page import render_production_status_badge
        render_production_status_badge(order_id, status)
    except Exception:
        pass

    # ── Inline expand: show product+power when ▼ clicked ─────────────────
    if st.session_state.get(f"_bo_expand_{order_id}", False):
        _exp_lines = [
            l for l in lines
            if str(l.get("eye_side","")).upper()[:1] in ("R","L")
        ]
        _exp_lines = sorted(_exp_lines, key=lambda x: 0 if str(x.get("eye_side","")).upper()[:1]=="R" else 1)
        if _exp_lines:
            with st.container():
                st.markdown(
                    "<div style='background:#0f172a;border:1px solid #1e293b;"
                    "border-radius:8px;padding:10px 14px;margin:4px 0'>",
                    unsafe_allow_html=True
                )
                for _el in _exp_lines:
                    _ee = str(_el.get("eye_side","")).upper()[:1]
                    _ec = "#ef4444" if _ee=="R" else "#60a5fa"
                    _ep = str(_el.get("product_name","") or "")
                    _lpe = _el.get("lens_params") or {}
                    if isinstance(_lpe, str):
                        import json as _jex
                        try: _lpe = _jex.loads(_lpe)
                        except: _lpe = {}
                    _es, _ec2, _ea, _ead = (_el.get(k) for k in ("sph","cyl","axis","add_power"))
                    _pw_bits = []
                    try:
                        if _es is not None: _pw_bits.append(f"SPH {float(_es):+.2f}")
                        if _ec2 is not None: _pw_bits.append(f"CYL {float(_ec2):+.2f}")
                        if _ea is not None: _pw_bits.append(f"AX {int(float(_ea))}")
                        if _ead is not None and float(_ead): _pw_bits.append(f"ADD {float(_ead):+.2f}")
                    except: pass
                    _coat = str(_lpe.get("coating_type") or _lpe.get("coating") or _el.get("coating_type") or "")
                    _idx  = str(_lpe.get("index_value") or _lpe.get("lens_index") or _el.get("lens_index") or "")
                    _dia  = str(_lpe.get("diameter") or "")
                    _frm  = str(_lpe.get("frame_type") or "")
                    _chips = " · ".join(x for x in [_idx, _coat, _dia, _frm] if x)
                    st.markdown(
                        f"<div style='margin-bottom:5px'>"
                        f"<span style='color:{_ec};font-weight:800;font-size:0.75rem'>{_ee}</span>"
                        f"<span style='color:#e2e8f0;font-size:0.78rem;font-weight:600;margin-left:6px'>{_ep}</span>"
                        f"<span style='color:#7dd3fc;font-family:monospace;font-size:0.72rem;margin-left:8px'>"
                        f"{'  '.join(_pw_bits)}</span>"
                        + (f"<br><span style='color:#64748b;font-size:0.68rem;margin-left:20px'>{_chips}</span>" if _chips else "")
                        + "</div>",
                        unsafe_allow_html=True
                    )
                st.markdown("</div>", unsafe_allow_html=True)


    # ── Action row ───────────────────────────────────────────────────────────
    _expand_key = f"_bo_expand_{order_id}"
    _is_expanded = st.session_state.get(_expand_key, False)

    btn_cols = st.columns([2, 1, 4, 3])

    with btn_cols[0]:
        if st.button("🔍 Open", key=f"open_{order_id}_{idx}",
                     use_container_width=True, type="primary"):
            st.session_state["bo_selected_order_id"] = order_id
            st.session_state["bo_view_mode"] = "order_detail"
            st.session_state["bo_orders_loaded"] = False  # force fresh load
            st.rerun()

    with btn_cols[1]:
        # Per-order inline expand toggle — shows power+product without navigating
        _exp_label = "▲" if _is_expanded else "▼"
        if st.button(_exp_label, key=f"bo_exp_{order_id}_{idx}",
                     use_container_width=True,
                     help="Show/hide order details inline"):
            st.session_state[_expand_key] = not _is_expanded
            st.rerun()

    with btn_cols[2]:
        # ── Live billing status from challan/invoice system ───────────────
        _billing_html = _live_billing_badge(order_id)
        st.markdown(_billing_html, unsafe_allow_html=True)

    with btn_cols[2]:
        transitions = _TRANSITIONS.get(status, [])

        # ── CANCELLED — grey lock, no actions ────────────────────────────
        if status == "CANCELLED":
            st.markdown(
                "<div style='padding:5px 0;color:#475569;font-size:0.7rem;"
                "text-align:center;background:#1e293b;border-radius:6px;"
                "border:1px solid #334155'>🔒 Cancelled</div>",
                unsafe_allow_html=True)

        # ── Pre-confirmed: PENDING / UNDER_REVIEW ─────────────────────────
        # Quick HOLD/CANCEL actions. Staff can open detail to confirm/manage.
        elif status in ("PENDING", "PROVISIONAL", "UNDER_REVIEW"):
            from modules.security.roles import has_role as _hr_card
            if _hr_card("admin","manager","billing"):
                _ckey = f"_quick_cancel_{order_id}_{idx}"
                _hkey = f"_quick_hold_{order_id}_{idx}"
                if not st.session_state.get(_ckey):
                    _act1, _act2 = st.columns(2)
                    with _act1:
                        if st.button("⏸ Hold", key=f"qhold_btn_{order_id}_{idx}",
                                     use_container_width=True,
                                     help="Put this order on hold"):
                            if _save_status_change(order, "HOLD"):
                                load_orders_from_database.clear()
                                st.success("Order put on Hold")
                                st.rerun()
                    with _act2:
                        if st.button("🚫 Cancel", key=f"qcancel_btn_{order_id}_{idx}",
                                     use_container_width=True,
                                     help="Cancel this order before it enters production"):
                            st.session_state[_ckey] = True
                            st.rerun()
                else:
                    # Inline mini reason + confirm
                    _qr = st.selectbox(
                        "Reason",
                        ["— select —",
                         "Non-availability of stock",
                         "Cancelled by Client / Party",
                         "Wrong entry / duplicate",
                         "Customer changed mind",
                         "Other"],
                        key=f"qcancel_reason_{order_id}_{idx}",
                        label_visibility="collapsed"
                    )
                    _qc1, _qc2 = st.columns(2)
                    with _qc1:
                        if st.button("✅ Confirm", key=f"qcancel_yes_{order_id}_{idx}",
                                     type="primary", use_container_width=True,
                                     disabled=(_qr == "— select —")):
                            try:
                                from modules.sql_adapter import run_write as _rw_qc
                                _rw_qc("ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancel_reason TEXT")
                            except Exception:
                                pass
                            if _save_status_change(order, "CANCELLED"):
                                try:
                                    from modules.sql_adapter import run_write as _rw_qc2
                                    from modules.security.roles import current_user as _cu_qc
                                    import datetime as _dt_qc
                                    _rw_qc2(
                                        "UPDATE orders SET cancel_reason=%(r)s WHERE order_no=%(o)s",
                                        {"r": f"[{_dt_qc.datetime.now().strftime('%d-%b-%Y %H:%M')}] "
                                             f"CANCELLED: {_qr}", "o": str(order.get('order_no',''))}
                                    )
                                except Exception:
                                    pass
                                st.session_state.pop(_ckey, None)
                                load_orders_from_database.clear()
                                st.success(f"Cancelled — {_qr}")
                                st.rerun()
                    with _qc2:
                        if st.button("← Back", key=f"qcancel_no_{order_id}_{idx}",
                                     use_container_width=True):
                            st.session_state.pop(_ckey, None)
                            st.rerun()

        elif status in ("HOLD", "CREDIT_HOLD", "PENDING_PAYMENT"):
            from modules.security.roles import has_role as _hr_hold
            if _hr_hold("admin","manager","billing"):
                _hc1, _hc2 = st.columns(2)
                with _hc1:
                    if st.button("▶ Resume", key=f"resume_{order_id}_{idx}",
                                 use_container_width=True,
                                 help="Move held order back to Under Review"):
                        if _save_status_change(order, "UNDER_REVIEW"):
                            load_orders_from_database.clear()
                            st.success("Order resumed")
                            st.rerun()
                with _hc2:
                    if st.button("🚫 Cancel", key=f"hold_cancel_{order_id}_{idx}",
                                 use_container_width=True):
                        if _save_status_change(order, "CANCELLED"):
                            load_orders_from_database.clear()
                            st.success("Order cancelled")
                            st.rerun()

        # ── CONFIRMED — no cancel button on card (detail panel only) ─────
        # Show nothing here — staff must open the order detail to cancel.
        elif status == "CONFIRMED":
            st.markdown(
                "<div style='padding:5px 0;color:#3b82f6;font-size:0.7rem;"
                "text-align:center'>✅ Confirmed<br>"
                "<span style='color:#475569;font-size:0.62rem'>Open to manage</span>"
                "</div>",
                unsafe_allow_html=True)

        # ── DISPATCHED (physical dispatch action) ─────────────────────────
        elif "DISPATCHED" in transitions and status not in _TERMINAL:
            if st.button("🚚 Dispatched", key=f"dispatch_{order_id}_{idx}",
                         use_container_width=True):
                if _save_status_change(order, "DISPATCHED"):
                    _mob = str(order.get("patient_mobile") or order.get("party_mobile") or "")
                    wa   = _wa_message(order, "DISPATCHED")
                    if wa and _mob:
                        _wu = _wa_url(_mob, wa)
                        st.markdown(
                            "<a href='{u}' target='_blank' style='display:inline-block;"
                            "background:#25d366;color:#fff;padding:6px 14px;border-radius:6px;"
                            "font-weight:700;font-size:.78rem;text-decoration:none;margin:4px 0'>"
                            "📲 Send WA — Dispatched</a>".format(u=_wu),
                            unsafe_allow_html=True)
                    elif wa:
                        st.info("📱 " + wa)
                    load_orders_from_database.clear()
                    st.rerun()

        # ── DELIVERED ────────────────────────────────────────────────────
        elif "DELIVERED" in transitions:
            if st.button("✅ Delivered", key=f"deliver_{order_id}_{idx}",
                         use_container_width=True):
                if _save_status_change(order, "DELIVERED"):
                    load_orders_from_database.clear()
                    st.rerun()

        # ── Other terminals (CLOSED, DELIVERED, RETURNED) ─────────────────
        elif status in _TERMINAL:
            st.markdown(
                f"<div style='padding:5px 0;color:#334155;font-size:0.7rem;text-align:center'>"
                f"🔒 {status}</div>", unsafe_allow_html=True)

        # ── Suggestion (IN_PRODUCTION, READY, etc.) — not CONFIRMED ───────
        elif suggestion and suggestion not in ("BILLED", "CONFIRMED"):
            if st.button(f"⚡ {suggestion}", key=f"sugg_{order_id}_{idx}",
                         use_container_width=True):
                if _save_status_change(order, suggestion):
                    _mob2 = str(order.get("patient_mobile") or order.get("party_mobile") or "")
                    _wa2  = _wa_message(order, suggestion)
                    if _wa2 and _mob2:
                        _wu2 = _wa_url(_mob2, _wa2)
                        st.markdown(
                            "<a href='{u}' target='_blank' style='display:inline-block;"
                            "background:#25d366;color:#fff;padding:6px 14px;border-radius:6px;"
                            "font-weight:700;font-size:.78rem;text-decoration:none;margin:4px 0'>"
                            "📲 Send WA — {st}</a>".format(u=_wu2, st=suggestion),
                            unsafe_allow_html=True)
                    load_orders_from_database.clear()
                    st.rerun()

    # ── Stage Release button (IN_PRODUCTION / READY → CONFIRMED) ────────────
    if status in STAGE_RELEASE_ALLOWED_FROM:
        _rel_key = f"release_panel_{order_id}_{idx}"
        if not st.session_state.get(_rel_key):
            if st.button("🔓 Release for Edit", key=f"rel_btn_{order_id}_{idx}",
                         use_container_width=True, help="Move back to CONFIRMED so order can be edited"):
                st.session_state[_rel_key] = True
                st.rerun()
        else:
            st.warning("⚠️ Releasing will move order back to **CONFIRMED** for editing. Select reason:")
            _rel_reason = st.selectbox("Release reason", STAGE_RELEASE_REASONS,
                                       key=f"rel_reason_{order_id}_{idx}")
            _rc1, _rc2 = st.columns(2)
            with _rc1:
                if st.button("✅ Confirm Release", type="primary",
                             key=f"rel_yes_{order_id}_{idx}",
                             disabled=_rel_reason == "— Select reason —",
                             use_container_width=True):
                    if _save_status_change(order, STAGE_RELEASE_TARGET_STATUS,
                                           scan_source=f"STAGE_RELEASE:{_rel_reason}"):
                        st.session_state.pop(_rel_key, None)
                        load_orders_from_database.clear()
                        st.success(f"✅ Order released to {STAGE_RELEASE_TARGET_STATUS}")
                        st.rerun()
            with _rc2:
                if st.button("✕ Cancel", key=f"rel_no_{order_id}_{idx}",
                             use_container_width=True):
                    st.session_state.pop(_rel_key, None)
                    st.rerun()

    # ── Return Request button (DISPATCHED / DELIVERED) ────────────────────────
    if status in RETURN_ALLOWED_FROM_STATUSES:
        _ret_key = f"return_panel_{order_id}_{idx}"
        if not st.session_state.get(_ret_key):
            if st.button("↩️ Request Return", key=f"ret_btn_{order_id}_{idx}",
                         use_container_width=True):
                st.session_state[_ret_key] = True
                st.rerun()
        else:
            st.markdown("#### ↩️ Return Request")
            _ret_reason = st.selectbox("Return reason", RETURN_REASONS,
                                       key=f"ret_reason_{order_id}_{idx}")
            _ret_note = st.text_area("Additional notes (optional)",
                                     key=f"ret_note_{order_id}_{idx}",
                                     placeholder="Describe the issue…")
            _rr1, _rr2 = st.columns(2)
            with _rr1:
                if st.button("✅ Submit Return Request", type="primary",
                             key=f"ret_yes_{order_id}_{idx}",
                             disabled=_ret_reason == "— Select reason —",
                             use_container_width=True):
                    _ret_remarks = f"RETURN REQUEST — {_ret_reason}" + (f" | {_ret_note}" if _ret_note else "")
                    if _save_status_change(order, "RETURN_REQUESTED",
                                           scan_source="RETURN_REQUEST"):
                        try:
                            from modules.sql_adapter import run_write as _rw_ret
                            _rw_ret(
                                "UPDATE orders SET remarks=COALESCE(remarks||' | ','') || %(r)s "
                                "WHERE order_no=%(o)s",
                                {"r": _ret_remarks, "o": str(order.get("order_no",""))},
                            )
                        except Exception:
                            pass
                        st.session_state.pop(_ret_key, None)
                        load_orders_from_database.clear()
                        st.success("✅ Return request submitted")
                        st.rerun()
            with _rr2:
                if st.button("✕ Cancel", key=f"ret_no_{order_id}_{idx}",
                             use_container_width=True):
                    st.session_state.pop(_ret_key, None)
                    st.rerun()

    # ── Inline expanded lines ─────────────────────────────────────────────────
    if st.session_state.get(f"expand_{order_id}_{idx}"):
        # Skip production panel for consultation orders — they have no product lines
        if str(order.get("order_type","")).upper() == "CONSULTATION":
            fee = float(order.get("total_value") or 0)
            mob = str(order.get("patient_mobile") or order.get("party_mobile") or "")
            st.markdown(
                f"<div style='background:#1e293b;border-left:4px solid #10b981;"
                f"border-radius:6px;padding:10px 14px;margin:6px 0'>"
                f"<b style='color:#10b981'>🩺 Consultation Visit</b>"
                f"<span style='color:#94a3b8;font-size:0.78rem;margin-left:10px'>"
                f"Examination only — no product lines</span><br>"
                f"<span style='color:#e2e8f0;font-size:0.85rem'>Fee: "
                f"<b>₹{fee:,.2f}</b>"
                + (f"  ·  Mobile: {mob}" if mob else "")
                + f"</span></div>",
                unsafe_allow_html=True,
            )
        else:
            _render_inline_lines(order_id, lines, idx)

    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)


def _render_inline_lines(order_id: str, lines: list, card_idx: int) -> None:
    if not lines:
        st.caption("No lines loaded yet.")
        return

    groups: dict = {}
    for l in lines:
        if not isinstance(l, dict):
            continue
        pid = l.get("product_id") or l.get("product_name", "unknown")
        if pid not in groups:
            groups[pid] = {"name": l.get("product_name", "N/A"),
                           "R": None, "L": None, "other": []}
        eye = (l.get("eye_side") or "").upper()
        if eye in ("R", "RIGHT") and not groups[pid]["R"]:
            groups[pid]["R"] = l
        elif eye in ("L", "LEFT") and not groups[pid]["L"]:
            groups[pid]["L"] = l
        else:
            groups[pid]["other"].append(l)

    for grp in groups.values():
        st.markdown(
            f"<div style='color:#94a3b8;font-size:0.7rem;font-weight:700;"
            f"margin:6px 0 3px;text-transform:uppercase;letter-spacing:.06em'>"
            f"{grp['name']}</div>",
            unsafe_allow_html=True,
        )
        eye_lines = []
        if grp["R"]: eye_lines.append(("👁R", grp["R"]))
        if grp["L"]: eye_lines.append(("👁L", grp["L"]))
        for l in grp["other"]: eye_lines.append(("●", l))

        cols = st.columns(min(len(eye_lines), 3))
        for col, (elabel, l) in zip(cols, eye_lines[:3]):
            with col:
                _render_mini_line_card(elabel, l)

    # ── 4-Route Stage Panel ───────────────────────────────────────────────
    _render_route_stage_panel(order_id, lines)

    # ── Expected Supply Date + WhatsApp Panel ────────────────────────────
    _render_supply_date_wa_panel(order_id, lines)

    st.divider()


def _render_supply_date_wa_panel(order_id: str, lines: list) -> None:
    """
    Shows:
    1. Expected Date of Supply — editable date field, saved to DB
    2. Rich WhatsApp button — sends template with products, powers, expected date
    """
    import urllib.parse, datetime as _dt

    # Load current expected_supply_date from DB
    from modules.sql_adapter import run_query, run_write
    _order_row = (run_query(
        "SELECT o.patient_name, o.party_name, o.patient_mobile, "
        "COALESCE(p.mobile,'') AS party_mobile, o.order_no, "
        "COALESCE(expected_supply_date::text,'') AS esd "
        "FROM orders o "
        "LEFT JOIN parties p ON p.id = o.party_id "
        "WHERE o.id=%s::uuid LIMIT 1", (order_id,)) or [{}])[0]

    _mobile   = str(_order_row.get("patient_mobile") or _order_row.get("party_mobile") or "")
    _name     = str(_order_row.get("patient_name") or _order_row.get("party_name") or "")
    _order_no = str(_order_row.get("order_no") or "")
    _esd_raw  = str(_order_row.get("esd") or "")

    try:
        _esd_val = _dt.date.fromisoformat(_esd_raw) if _esd_raw else (
            _dt.date.today() + _dt.timedelta(days=7))
    except Exception:
        _esd_val = _dt.date.today() + _dt.timedelta(days=7)

    st.markdown(
        "<div style='background:#1e293b;border-radius:6px;padding:10px 14px;margin:6px 0'>",
        unsafe_allow_html=True,
    )
    _dc1, _dc2, _dc3 = st.columns([2, 1.5, 2])

    with _dc1:
        _new_date = st.date_input(
            "📅 Expected Supply Date",
            value=_esd_val,
            key=f"esd_{order_id}",
        )

    with _dc2:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if st.button("💾 Save Date", key=f"esd_save_{order_id}",
                     use_container_width=True):
            try:
                run_write(
                    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                    "expected_supply_date DATE"
                )
            except Exception:
                pass
            try:
                run_write(
                    "UPDATE orders SET expected_supply_date=%s WHERE id=%s::uuid",
                    (_new_date, order_id),
                )
                st.success("✅ Date saved")
            except Exception as _de:
                st.error(f"Save failed: {_de}")

    with _dc3:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        # Build rich WA message with products + powers + expected date
        if _mobile:
            try:
                from modules.post_save_actions import _build_product_lines, _si
                _shop = _si()
                _shop_name = _shop.get("shop_name", "DV Optical")
                _shop_phone = _shop.get("shop_phone", "")

                def _fmt_pwr(sph, cyl, axis, add):
                    def _f(v):
                        if v is None: return None
                        try:
                            n = float(v)
                            return "{}{:.2f}".format("+" if n>=0 else "", n)
                        except Exception: return None
                    parts = [p for p in [
                        ("Sph "+_f(sph)) if _f(sph) else None,
                        ("Cyl "+_f(cyl)) if _f(cyl) else None,
                        ("Ax "+str(int(float(axis)))) if axis is not None else None,
                        ("Add "+_f(add)) if _f(add) else None,
                    ] if p]
                    return "  ".join(parts)

                _prod_lines = []
                for ln in (lines or []):
                    if not isinstance(ln, dict): continue
                    eye   = str(ln.get("eye_side") or "").upper()
                    pname = str(ln.get("product_name") or "")
                    brand = str(ln.get("brand") or "")
                    qty   = ln.get("billing_qty") or ln.get("quantity") or 0
                    total = float(ln.get("billing_total") or ln.get("total_price") or 0)
                    eye_lbl = {"R":"👁 Right","L":"👁 Left","B":"👁👁 Both"}.get(eye,"")
                    prod_txt = "{} {}".format(brand, pname).strip() if brand else pname
                    pw = _fmt_pwr(ln.get("sph"), ln.get("cyl"),
                                  ln.get("axis"), ln.get("add_power"))
                    row_parts = []
                    if eye_lbl: row_parts.append(f"*{eye_lbl}*")
                    row_parts.append(prod_txt)
                    if qty and int(qty)>0: row_parts.append(f"Qty:{qty}")
                    if pw:  row_parts.append(f"[{pw}]")
                    if total>0: row_parts.append(f"₹{total:,.0f}")
                    _prod_lines.append("  ".join(row_parts))

                _prod_block = "\n".join(_prod_lines)
                _date_str = _new_date.strftime("%d %b %Y")

                _nl = "\n"
                _msg = (
                    "Hello " + _name + " 👋" + _nl + _nl +
                    "🏪 *" + _shop_name + "*" + _nl +
                    "📋 Order: *" + _order_no + "*" + _nl
                )
                if _prod_block:
                    _msg += _nl + "📦 *Order Details:*" + _nl + _prod_block + _nl
                _msg += (_nl + "📅 *Expected Supply: " + _date_str + "*" + _nl + _nl +
                         "We will notify you when your order is ready." + _nl)
                if _shop_phone:
                    _msg += "Queries: " + _shop_phone + _nl
                _msg += _nl + "Thank you! 🙏 " + _shop_name

                _clean_mob = "".join(c for c in _mobile if c.isdigit())
                if len(_clean_mob)==10: _clean_mob = "91"+_clean_mob
                _wa_url = "https://wa.me/{}?text={}".format(
                    _clean_mob, urllib.parse.quote(_msg))

                st.link_button("📲 Send WhatsApp", _wa_url,
                               use_container_width=True)
                with st.expander("Preview message", expanded=False):
                    st.code(_msg, language=None)
            except Exception as _we:
                st.caption(f"WA: {_we}")
        else:
            st.caption("No mobile — WA unavailable")

    st.markdown("</div>", unsafe_allow_html=True)


def _render_mini_line_card(eye_label: str, line: dict) -> None:
    import math
    route   = resolve_line_route(line)
    alloc   = int(line.get("allocated_qty") or 0)
    billing = int(line.get("billing_qty") or 1)
    pending = max(0, billing - alloc)
    locked  = bool(line.get("supplier_order_id"))
    rc      = _ROUTE_COLOR.get(route, "#64748b")
    rl      = _ROUTE_LABEL.get(route, route)
    al_pct  = int(100 * alloc / billing) if billing else 0
    al_col  = "#10b981" if pending == 0 else ("#f59e0b" if alloc > 0 else "#ef4444")

    sph = line.get("sph")
    power_str = ""
    if sph is not None:
        try:
            fv = float(sph)
            if not math.isnan(fv):
                power_str = f"SPH {fv:+.2f}"
                cyl = line.get("cyl")
                if cyl is not None:
                    try:
                        cv = float(cyl)
                        if not math.isnan(cv) and cv != 0:
                            power_str += f" CYL {cv:+.2f}"
                    except Exception:
                        pass
        except Exception:
            pass

    total = float(line.get("total_price") or line.get("billing_total") or 0)
    gst   = float(line.get("gst_amount") or 0)
    grand = round(total + gst, 2)

    with st.container(border=True):
        st.markdown(
            f"<div style='font-size:0.72rem;font-weight:700;color:#e2e8f0'>{eye_label}"
            f" &nbsp;<span style='background:{rc};color:#fff;padding:1px 6px;"
            f"border-radius:6px;font-size:0.6rem'>{rl}</span>"
            f"{'&nbsp;🔒' if locked else ''}</div>",
            unsafe_allow_html=True,
        )
        if power_str:
            st.markdown(
                f"<div style='font-family:monospace;font-size:0.7rem;"
                f"color:#93c5fd'>{power_str}</div>",
                unsafe_allow_html=True,
            )
        st.progress(al_pct / 100)
        st.markdown(
            f"<div style='font-size:0.65rem;color:{al_col}'>"
            f"{alloc}/{billing} pcs"
            f"{' · ⏳ '+str(pending)+' to order' if pending else ' · ✅'}"
            f"{'  ₹'+f'{grand:,.0f}' if grand else ''}</div>",
            unsafe_allow_html=True,
        )


# ══════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════

def render_backoffice_dashboard() -> None:

    # ── Daily gap detection (silent, once per calendar day) ───────────────────
    import datetime as _gdt
    _gap_day_key = f"_gap_scan_{_gdt.date.today().isoformat()}"
    if not st.session_state.get(_gap_day_key):
        try:
            from modules.sql_adapter import run_query as _rq_gap
            _has_gap_fn = _rq_gap("""
                SELECT 1 AS ok
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE p.proname = 'detect_all_doc_gaps'
                  AND n.nspname = 'public'
                LIMIT 1
            """)
            if _has_gap_fn:
                _rq_gap("SELECT detect_all_doc_gaps(%(fy)s,'auto_daily')",
                        {"fy": _gdt.date.today().strftime("%y%m")})
            st.session_state[_gap_day_key] = True
        except Exception:
            pass  # non-fatal

    # ── Auto-load recent orders on first open ─────────────────────────────────
    # Keep first paint light; audit users can load closed/history rows with the
    # controls below after the page is already interactive.
    if not st.session_state.get("_bo_default_closed_v2"):
        st.session_state["bo_include_closed"] = False
        st.session_state["_bo_default_closed_v2"] = True
    if not st.session_state.get("bo_orders_loaded"):
        with st.spinner("Loading recent orders…"):
            try:
                load_orders_summary.clear()
                st.session_state.bo_active_orders = load_orders_summary(
                    limit=20,
                    include_closed=st.session_state.get("bo_include_closed", False)
                )
            except Exception:
                st.session_state.bo_active_orders = []
            st.session_state.bo_orders_loaded = True
            st.session_state["bo_load_limit"] = 20
            st.session_state["bo_loaded_include_closed"] = st.session_state.get("bo_include_closed", False)

    # ── Controls row ──────────────────────────────────────────────────────────
    c_search, c_status, c_type, c_route = st.columns([3, 2, 2, 2])

    # Pre-fill from clinical bill button
    _presearch = st.session_state.pop("bo_search_term", None)
    _preconsult = st.session_state.pop("bo_filter_consult", False)
    if _presearch:
        st.session_state["bo_search_query"] = _presearch
    if _preconsult:
        st.session_state["bo_type_filter"] = "All"

    with c_search:
        search = st.text_input(
            "search", key="bo_search_query",
            label_visibility="collapsed",
            placeholder="🔍 Search by order ID or party name…",
        )

    with c_status:
        _status_options = ["All", "UNPROCURED"] + _ALL_STATUSES
        _cur_status_choice = st.session_state.get("bo_status_filter", "All")
        if _cur_status_choice not in _status_options:
            st.session_state["bo_status_filter"] = "All"
        status_filter = st.selectbox(
            "Status", _status_options,
            key="bo_status_filter", label_visibility="collapsed",
            format_func=lambda s: "All Statuses" if s == "All" else (
                "⚠️ Unprocured" if s == "UNPROCURED" else
                f"{_STATUS_ICON.get(s, '')} {s.replace('_', ' ').title()}"
            ),
        )

    with c_type:
        _bo_type_options = ["All", "RETAIL", "WHOLESALE", "PURCHASE"]
        if st.session_state.get("bo_type_filter") not in _bo_type_options:
            st.session_state["bo_type_filter"] = "All"
        type_filter = st.selectbox(
            "Type", _bo_type_options,
            key="bo_type_filter", label_visibility="collapsed",
        )

    with c_route:
        route_filter = st.selectbox(
            "Route", ["All", "📦 Stock", "🏭 Supplier", "🔧 In-House", "🔬 Ext Lab"],
            key="bo_route_filter", label_visibility="collapsed",
        )

    _ROUTE_KEY = {
        "📦 Stock": "STOCK", "🏭 Supplier": "VENDOR",
        "🔧 In-House": "INHOUSE", "🔬 Ext Lab": "EXTERNAL_LAB",
    }

    # ── Load bar: refresh + show-closed + load-more ───────────────────────────
    _lc1, _lc2, _lc3, _lc4, _lc5, _lc6 = st.columns([1, 1, 1, 1, 1, 2])
    _inc_closed = st.session_state.get("bo_include_closed", False)

    def _do_load(limit, offset=0, include_closed=None):
        _closed = st.session_state.get("bo_include_closed", False) if include_closed is None else bool(include_closed)
        load_orders_summary.clear()
        try:
            st.session_state.bo_active_orders = load_orders_summary(
                limit=limit,
                include_closed=_closed,
            )
        except Exception:
            st.session_state.bo_active_orders = []
        st.session_state.bo_orders_loaded = True
        st.session_state["bo_load_limit"] = limit
        st.session_state["bo_loaded_include_closed"] = _closed
        st.rerun()

    with _lc1:
        if st.button("🔄 Refresh", use_container_width=True, help="Reload from DB"):
            _do_load(st.session_state.get("bo_load_limit", 10))

    with _lc2:
        if st.button("📄 20", use_container_width=True, help="Load 20 orders"):
            _do_load(20)

    with _lc3:
        if st.button("📄 50", use_container_width=True, help="Load 50 orders"):
            _do_load(50)

    with _lc4:
        if st.button("📄 100", use_container_width=True, help="Load 100 orders"):
            _do_load(100)

    with _lc5:
        inc_closed = st.toggle(
            "Show closed",
            key="bo_include_closed",
            help="Include CLOSED / DELIVERED / CANCELLED orders"
        )
        # Auto-reload when the toggle differs from the data currently loaded.
        if inc_closed != st.session_state.get("bo_loaded_include_closed", False):
            _do_load(st.session_state.get("bo_load_limit", 20), include_closed=inc_closed)

    with _lc6:
        _cur_limit = st.session_state.get("bo_load_limit", 10)
        _cur_count = len(st.session_state.get("bo_active_orders") or [])
        st.caption(
            f"Showing {_cur_count} order(s) · "
            f"{'🟢' if not inc_closed else '🔵'} "
            f"{'Active only' if not inc_closed else 'All incl. closed'}"
        )

    orders = st.session_state.bo_active_orders
    if not orders:
        load_orders_summary.clear()
        try:
            orders = load_orders_summary(
                limit=st.session_state.get("bo_load_limit", 10),
                include_closed=True,
            )
            st.session_state.bo_active_orders = orders
            st.session_state["bo_loaded_include_closed"] = True
        except Exception:
            orders = []
        if orders:
            st.info("No active orders found. Showing recent delivered/closed orders.")
        else:
            st.info("No orders found.")
            return

    # Deduplicate — same order_no may appear multiple times if loaded with multiple routes
    _seen_nos = set()
    _deduped = []
    for _o in orders:
        _ono = get_display_order_id(_o)
        if _ono not in _seen_nos:
            _seen_nos.add(_ono)
            _deduped.append(_o)
    orders = _deduped
    orders = [
        o for o in orders
        if str(o.get("order_type") or "").upper() != "CONSULTATION"
    ]

    # ── Filters ───────────────────────────────────────────────────────────────
    filtered = orders

    if status_filter == "UNPROCURED":
        filtered = [o for o in filtered if _order_has_unprocured_lines(o)]
    elif status_filter != "All":
        filtered = [
            o for o in filtered
            if _determine_workflow_status(o, o.get("status") or "PENDING", _order_lines(o)) == status_filter
        ]

    if type_filter != "All":
        filtered = [
            o for o in filtered
            if (o.get("order_type") or "RETAIL").upper() == type_filter
        ]

    if route_filter != "All":
        rk = _ROUTE_KEY.get(route_filter, "")
        filtered = [
            o for o in filtered
            if any(resolve_line_route(l) == rk for l in _order_lines(o))
        ]

    if search:
        def _order_matches(o):
            # Order-level fields
            if _scan_match(
                search,
                get_display_order_id(o),
                o.get("order_no"),
                o.get("patient_name"),
                o.get("party_name"),
                o.get("patient_mobile") or o.get("mobile"),
                o.get("case_no") or o.get("customer_order_no"),
                str(o.get("order_date") or o.get("created_at") or "")[:10],
                o.get("status"),
                o.get("order_type"),
            ):
                return True
            # Line-level fields (product name, brand, power)
            for _l in _order_lines(o):
                if _scan_match(search, _l.get("product_name"), _l.get("brand"), _l.get("sph")):
                    return True
            return False
        filtered = [o for o in filtered if _order_matches(o)]

    # ── Summary bar ───────────────────────────────────────────────────────────
    urgent  = sum(1 for o in filtered
                  if _days_old(o.get("order_date") or o.get("created_at")) > 7
                  and o.get("status") not in _TERMINAL)
    pending = sum(1 for o in filtered if o.get("status") == "PENDING")
    unprocured = sum(1 for o in filtered if _order_has_unprocured_lines(o))
    total_v = sum(
        (
            sum(float(l.get("total_price") or 0) + float(l.get("gst_amount") or 0)
                for l in _order_lines(o))
            if _order_lines(o)
            else float(o.get("net_total_value") or o.get("total_value") or 0)
        )
        for o in filtered
    )

    sc1, sc2, sc3, sc4, sc5 = st.columns(5)
    for col, val, lbl, color in [
        (sc1, f"{len(filtered)} / {len(orders)}", "Showing",          "#3b82f6"),
        (sc2, str(pending),                        "Pending action",   "#f59e0b"),
        (sc3, str(urgent),                         "Urgent (7d+)",     "#ef4444"),
        (sc4, str(unprocured),                      "Unprocured",       "#f97316"),
        (sc5, f"₹{total_v:,.0f}",                  "Total (filtered)", "#10b981"),
    ]:
        col.markdown(
            f"<div style='background:#1e293b;border-radius:8px;"
            f"padding:8px 12px;text-align:center;border-top:2px solid {color}'>"
            f"<div style='color:{color};font-size:1.1rem;font-weight:800'>{val}</div>"
            f"<div style='color:#64748b;font-size:0.65rem'>{lbl}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    # ── View toggle: Table (fast) vs Cards (detailed) ─────────────────────────
    _tv1, _tv2 = st.columns([1, 9])
    with _tv1:
        _table_view = st.toggle("⊞ Table", value=st.session_state.get("bo_table_view", True),
                                key="bo_table_view", help="Compact table view (faster)")

    if _table_view:
        # ── COMPACT TABLE VIEW (like old system) ──────────────────────────────
        for i, order in enumerate(filtered):
            _oid    = get_display_order_id(order)
            _party  = order.get("patient_name") or order.get("party_name") or "—"
            _status = _determine_workflow_status(order, order.get("status") or "PENDING", _order_lines(order))
            _sc     = _STATUS_COLOR.get(_status, "#64748b")
            _si     = _STATUS_ICON.get(_status, "•")
            _date   = str(order.get("order_date") or order.get("created_at") or "")[:10]
            _otype  = (order.get("order_type") or "RETAIL").upper()
            _lines  = _order_lines(order)
            _plines = [l for l in _lines if str(l.get("eye_side","")).upper() not in ("S","SERVICE")]
            _proc_html = _procurement_badge_html(order)
            _display_total = float(order.get("net_total_value") or order.get("total_value") or 0)

            # Route pills HTML
            _routes = _route_summary(_lines)
            _route_pills = "".join(
                f"<span style='background:{_ROUTE_COLOR.get(r,'#475569')}22;"
                f"border:1px solid {_ROUTE_COLOR.get(r,'#475569')}55;"
                f"color:{_ROUTE_COLOR.get(r,'#475569')};padding:1px 7px;"
                f"border-radius:8px;font-size:0.62rem;font-weight:700;margin-right:3px'>"
                f"{_ROUTE_LABEL.get(r,r)}</span>"
                for r in _routes
            ) if _routes else "<span style='color:#475569;font-size:0.65rem'>—</span>"

            # Product lines — read directly from original line dicts + lens_params
            _prod_lines_html = ""
            for _l in sorted(_plines, key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1):
                _eye = str(_l.get("eye_side","")).upper()
                _eye_s = "R" if _eye in ("R","RIGHT") else "L" if _eye in ("L","LEFT") else _eye
                _eye_c = "#ef4444" if _eye_s == "R" else "#60a5fa"
                _pn = str(_l.get("product_name","")).split(" | ")[0]
                # Parse lens_params — psycopg2 returns JSONB as dict already
                import json as _jbo
                _lp_e = _l.get("lens_params") or {}
                if isinstance(_lp_e, str):
                    try: _lp_e = _jbo.loads(_lp_e)
                    except: _lp_e = {}
                # Coating and index — use actual lens_params keys from DB audit
                _coat = str(
                    _lp_e.get("coating") or _lp_e.get("coating_type") or
                    _l.get("coating") or _l.get("coating_type") or ""
                ).strip()
                _idx = str(
                    _lp_e.get("lens_index") or _lp_e.get("index_value") or
                    _lp_e.get("refractive_index") or _l.get("lens_index") or ""
                ).strip()
                # Full power string including ADD
                _pw_parts = []
                try:
                    if _l.get("sph") is not None:
                        _pw_parts.append(f"SPH {float(_l['sph']):+.2f}")
                    if _l.get("cyl") is not None and abs(float(_l.get("cyl") or 0)) > 0.01:
                        _pw_parts.append(f"CYL {float(_l['cyl']):+.2f}")
                    if _l.get("axis") and int(float(_l.get("axis") or 0)) != 0:
                        _pw_parts.append(f"AX {int(float(_l['axis']))}°")
                    if _l.get("add_power") and float(_l.get("add_power") or 0) > 0:
                        _pw_parts.append(f"ADD {float(_l['add_power']):+.2f}")
                except: pass
                _pw = "  ".join(_pw_parts)
                _chips = "  ·  ".join(x for x in [_idx, _coat] if x)
                _proc_text = _procurement_line_text(_l)
                _prod_lines_html += (
                    f"<div style='padding:4px 0;border-bottom:1px solid #1e293b'>"
                    f"<div style='display:flex;gap:8px;align-items:center'>"
                    f"<span style='color:{_eye_c};font-weight:800;font-size:0.72rem;min-width:14px'>{_eye_s}</span>"
                    f"<span style='color:#e2e8f0;font-size:0.75rem;font-weight:600'>{_pn}</span>"
                    + (f"<span style='color:#7dd3fc;font-size:0.7rem;font-family:monospace'>{_pw}</span>" if _pw else "")
                    + f"</div>"
                    + (f"<div style='color:#64748b;font-size:0.67rem;padding-left:20px'>{_chips}</div>" if _chips else "")
                    + (
                        f"<div style='color:#a7f3d0;font-size:0.67rem;padding-left:20px'>{html.escape(_proc_text)}</div>"
                        if _proc_text and _proc_text.startswith("Procured:")
                        else (
                            f"<div style='color:#fbbf24;font-size:0.67rem;padding-left:20px'>{html.escape(_proc_text)}</div>"
                            if _proc_text else ""
                        )
                    )
                    + "</div>"
                )

            # Main row card
            _rc1, _rc2 = st.columns([8, 2])
            with _rc1:
                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #1e293b;"
                    f"border-left:4px solid {_sc};border-radius:6px;"
                    f"padding:8px 14px;margin-bottom:4px'>"
                    # Row 1: date | order no | patient | status badge
                    f"<div style='display:flex;align-items:center;gap:12px;flex-wrap:wrap'>"
                    f"<span style='color:#475569;font-size:0.72rem'>{_date}</span>"
                    f"<span style='color:#f1f5f9;font-weight:800;font-size:0.9rem;"
                    f"font-family:monospace'>{_oid}</span>"
                    f"<span style='color:#cbd5e1;font-size:0.82rem;font-weight:600'>{_party}</span>"
                    f"<span style='background:{_sc};color:#fff;font-size:0.68rem;"
                    f"font-weight:700;padding:2px 9px;border-radius:8px'>{_si} {_STATUS_DISPLAY.get(_status, _status.replace(chr(95),' ').title())}</span>"
                    f"<span style='background:{_TYPE_COLOR.get(_otype,'#475569')};color:#fff;"
                    f"font-size:0.65rem;font-weight:800;padding:1px 8px;border-radius:8px'>{_otype}</span>"
                    f"<span style='color:#86efac;font-size:0.78rem;font-weight:800'>₹{_display_total:,.0f}</span>"
                    f"</div>"
                    # Row 2: route pills
                    f"<div style='margin-top:5px;display:flex;align-items:center;gap:4px'>"
                    f"{_route_pills}</div>"
                    f"{_proc_html}"
                    f"</div>",
                    unsafe_allow_html=True
                )
            with _rc2:
                _ba1, _ba2, _ba3, _ba4, _ba5 = st.columns(5)
                # Open detail
                with _ba1:
                    if st.button("📂", key=f"tbl_open_{_oid}_{i}",
                                 help="Open order detail",
                                 use_container_width=True):
                        st.session_state.bo_selected_order_id = _oid
                        st.session_state.bo_view_mode = "order_detail"
                        st.rerun()
                    if st.button("👁", key=f"tbl_det_{_oid}_{i}",
                                 help="Show products",
                                 use_container_width=True):
                        _dk = f"tbl_detail_{_oid}"
                        st.session_state[_dk] = not st.session_state.get(_dk, False)
                        st.rerun()
                # Print job card
                with _ba2:
                    if st.button("📋", key=f"tbl_jc_{_oid}_{i}",
                                 help="Print Job Card",
                                 use_container_width=True):
                        st.session_state[f"tbl_show_jc_{_oid}"] = not st.session_state.get(f"tbl_show_jc_{_oid}", False)
                        st.rerun()
                # Patient card / barcode
                with _ba3:
                    if st.button("🪪", key=f"tbl_card_{_oid}_{i}",
                                 help="Patient Card / Barcode",
                                 use_container_width=True):
                        st.session_state[f"tbl_show_card_{_oid}"] = not st.session_state.get(f"tbl_show_card_{_oid}", False)
                        st.rerun()
                # Print label
                with _ba4:
                    if st.button("🏷️", key=f"tbl_lbl_{_oid}_{i}",
                                 help="Print Label",
                                 use_container_width=True):
                        st.session_state[f"tbl_show_lbl_{_oid}"] = not st.session_state.get(f"tbl_show_lbl_{_oid}", False)
                        st.rerun()
                # WhatsApp
                with _ba5:
                    _wa_mob = str(order.get("patient_mobile") or order.get("party_mobile") or "").strip()
                    if _wa_mob and _wa_mob not in ("nan","None","0"):
                        _wa_url = f"https://wa.me/91{str(_wa_mob or "").strip().lstrip("0").lstrip("+91")[-10:]}?text=Your+order+{_oid}+is+ready"
                        st.link_button("💬", _wa_url, help="WhatsApp",
                                       use_container_width=True)
                    else:
                        st.button("💬", key=f"tbl_wa_{_oid}_{i}",
                                  disabled=True, help="No mobile",
                                  use_container_width=True)

            # ── Expandable print panels (inline, below the row) ──────────────
            # Show product detail collapsible (always below card)
            _detail_key = f"tbl_detail_{_oid}"
            if st.session_state.get(_detail_key):
                st.markdown(
                    f"<div style='background:#0a0f1a;border:1px solid #1e293b;"
                    f"border-radius:0 0 6px 6px;padding:8px 14px;margin-top:-4px;margin-bottom:4px'>"
                    f"{_prod_lines_html}"
                    f"</div>",
                    unsafe_allow_html=True
                )

            if st.session_state.get(f"tbl_show_jc_{_oid}"):
                with st.container(border=True):
                    st.caption(f"📋 Job Card — {_oid}")
                    try:
                        _il = [l for l in _plines if str(l.get("eye_side","")).upper() in ("R","RIGHT")]
                        _ll = [l for l in _plines if str(l.get("eye_side","")).upper() in ("L","LEFT")]
                        if _il and _ll:
                            from modules.documents.job_card_surfacing import render_job_card_print_pair
                            render_job_card_print_pair(_il[0], _ll[0], order)
                        elif _plines:
                            from modules.documents.job_card_surfacing import render_job_card_print
                            render_job_card_print(_plines[0], order)
                        else:
                            st.caption("No product lines — assign routes first.")
                    except Exception as _jce:
                        st.error(f"Job card error: {_jce}")

            if st.session_state.get(f"tbl_show_card_{_oid}"):
                with st.container(border=True):
                    st.caption(f"🪪 Patient Card — {_oid}")
                    try:
                        _pid  = str(order.get("party_id") or "")
                        _pname = order.get("patient_name") or order.get("party_name") or ""
                        _pmob = str(order.get("patient_mobile") or order.get("mobile") or "")
                        if _pid and len(_pid) > 10:
                            # Fetch latest RX for this patient
                            _rx_r2, _rx_l2 = {}, {}
                            try:
                                from modules.sql_adapter import run_query as _rq_rx2
                                _rxr = _rq_rx2("""
                                    SELECT right_sph,right_cyl,right_axis,right_add,
                                           left_sph,left_cyl,left_axis,left_add
                                    FROM patient_visits
                                    WHERE patient_id=%(pid)s::uuid
                                    ORDER BY visit_date DESC LIMIT 1
                                """, {"pid": _pid}) or []
                                if _rxr:
                                    _rx_r2 = {"sph":_rxr[0].get("right_sph"),"cyl":_rxr[0].get("right_cyl"),
                                              "axis":_rxr[0].get("right_axis"),"add":_rxr[0].get("right_add")}
                                    _rx_l2 = {"sph":_rxr[0].get("left_sph"),"cyl":_rxr[0].get("left_cyl"),
                                              "axis":_rxr[0].get("left_axis"),"add":_rxr[0].get("left_add")}
                            except Exception:
                                pass
                            from modules.printing.patient_card_printer import render_patient_card_buttons
                            render_patient_card_buttons(
                                patient_id=_pid, patient_name=_pname,
                                mobile=_pmob, rx_r=_rx_r2, rx_l=_rx_l2
                            )
                        else:
                            st.caption("No patient linked to this order.")
                    except Exception as _pce:
                        st.error(f"Patient card error: {_pce}")

            if st.session_state.get(f"tbl_show_lbl_{_oid}"):
                with st.container(border=True):
                    st.caption(f"🏷️ Label — {_oid}")
                    try:
                        from modules.printing.label_preview import render_label_preview
                        from modules.printing.patient_card_printer import ensure_patient_id
                        _pid2 = str(order.get("party_id") or "")
                        _lcode = ensure_patient_id(_pid2) if _pid2 and len(_pid2) > 10 else _oid[:8].upper()
                        _lval  = str(int(float(order.get("total_value") or 0)))
                        _lname = (order.get("patient_name") or order.get("party_name") or "")[:20]
                        render_label_preview(code=_lcode, shop=_lname, price=f"Rs.{_lval}")
                    except Exception as _lbe:
                        st.error(f"Label error: {_lbe}")

            # Thin divider
            st.markdown(
                "<div style='height:1px;background:#1e293b;margin:0 0 2px 0'></div>",
                unsafe_allow_html=True
            )
    else:
        # ── CARD VIEW (detailed) ──────────────────────────────────────────────
        for i, order in enumerate(filtered):
            _render_order_card(order, i)


# ══════════════════════════════════════════════════════════════
# MAIN ENTRY
# ══════════════════════════════════════════════════════════════

def render_backoffice_management() -> None:
    init_backoffice_state()

    st.title("🏢 Backoffice Management")
    st.markdown(
        "Complete order lifecycle management — "
        "Job Cards, Lab Orders, Labels & Status Tracking"
    )

    nav1, nav2, nav3, _spacer = st.columns([1, 1, 1, 3])
    with nav1:
        if st.button("📦 Orders", use_container_width=True):
            st.session_state.bo_view_mode = "dashboard"
            st.rerun()
    with nav2:
        if st.button("🩺 Clinical Records", use_container_width=True):
            st.session_state.bo_view_mode = "clinical_records"
            st.rerun()
    with nav3:
        if st.button("🔢 Gap Audit", use_container_width=True,
                     help="Document number sequence gap tracker"):
            st.session_state.bo_view_mode = "gap_audit"
            st.rerun()

    st.markdown("---")

    view = st.session_state.bo_view_mode
    if view == "dashboard":
        render_backoffice_dashboard()
    elif view == "order_detail":
        render_order_detail()
    elif view == "clinical_records":
        render_clinical_viewer_page()
    elif view == "gap_audit":
        try:
            from modules.backoffice.doc_gap_audit_ui import render_doc_gap_audit
            render_doc_gap_audit()
        except Exception as _ge:
            st.error(f"Gap audit error: {_ge}")


__all__ = [
    "render_backoffice_management",
    "render_backoffice_dashboard",
    "render_order_detail",
    "init_backoffice_state",
    "_train_mini",
    "_train_full",
]


# ══════════════════════════════════════════════════════════════
# 4-ROUTE STAGE PANEL
# Shows correct stage controls for each manufacturing route:
#   STOCK       — allocate from inventory → mark ready
#   INHOUSE     — in-house lab production stages
#   VENDOR      — supplier PO stages (DRAFT→SENT→ACK→RECEIVED)
#   EXTERNAL_LAB— send to external lab → received → QC → ready
# ══════════════════════════════════════════════════════════════

def _render_route_stage_panel(order_id: str, lines: list) -> None:
    """
    Stage advancement is disabled in Backoffice dashboard.
    Use the Production page for all stage advancement.
    READY_TO_BILL is the only billable terminal stage.
    """
    st.info(
        "⚙️ Production stages are managed via the **Production page**. "
        "Stage advancement from Backoffice is disabled to prevent conflicts."
    )
    return

    # Group lines by route
    route_groups: dict = {}
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        r = resolve_line_route(ln)
        route_groups.setdefault(r, []).append(ln)

    if not route_groups:
        return

    st.markdown("#### ⚙️ Fulfillment & Stage Tracking")

    for route, rlines in route_groups.items():
        if route == "STOCK":
            _stage_stock(order_id, rlines, _rq, _rw)
        elif route == "INHOUSE":
            _stage_inhouse(order_id, rlines, _rq, _rw)
        elif route == "VENDOR":
            _stage_vendor(order_id, rlines, _rq, _rw)
        elif route == "EXTERNAL_LAB":
            _stage_external_lab(order_id, rlines, _rq, _rw)
        else:
            st.caption(f"Route: {route} (no stage panel)")


# ── STOCK ─────────────────────────────────────────────────────────────────────

def _stage_stock(order_id, lines, _rq, _rw):
    with st.expander("📦 STOCK — Inventory Allocation", expanded=True):
        for ln in lines:
            lid  = str(ln.get("line_id") or ln.get("id") or "")
            pn   = str(ln.get("product_name") or "Item")
            eye  = str(ln.get("eye_side") or "")
            qty  = int(ln.get("billing_qty") or ln.get("quantity") or 0)
            alloc= int(ln.get("allocated_qty") or 0)
            rdy  = int(ln.get("ready_qty") or 0)

            eye_badge = {"R": "👁R", "L": "👁L", "B": "👁👁"}.get(eye.upper(), "●")
            done = alloc >= qty or rdy >= qty

            col1, col2, col3 = st.columns([3, 1, 1])
            col1.markdown(
                f"**{eye_badge} {pn}**  "
                f"<span style='color:#94a3b8;font-size:.75rem'>Qty:{qty}  Alloc:{alloc}</span>",
                unsafe_allow_html=True,
            )

            if done:
                col2.success("✅ Allocated")
            else:
                if col2.button("📥 Allocate", key=f"stock_alloc_{lid}",
                               use_container_width=True, type="primary"):
                    try:
                        _rw("""UPDATE order_lines SET allocated_qty = quantity,
                                   ready_qty = quantity
                               WHERE id = %s::uuid""", (lid,))
                        st.success("✅ Stock allocated")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Alloc failed: {e}")

            if alloc >= qty and rdy < qty:
                if col3.button("✅ Mark Ready", key=f"stock_rdy_{lid}",
                               use_container_width=True):
                    try:
                        _rw("""UPDATE order_lines SET ready_qty = quantity
                               WHERE id = %s::uuid""", (lid,))
                        # Advance order status if all lines ready
                        _check_and_advance_order(order_id, "READY", _rq, _rw)
                        st.success("✅ Ready")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Ready failed: {e}")


# ── INHOUSE (Lab) ──────────────────────────────────────────────────────────────

_INHOUSE_STAGES = [
    ("JOB_CREATED",        "📋", "Job Created"),
    ("JOB_PRINTED",        "🖨️",  "Job Card Printed"),
    ("PRODUCTION_PICKED",  "⚙️",  "Picked for Production"),
    ("SURFACING_DONE",     "✨", "Surfacing Done"),
    ("INSPECTION",         "🔍", "Inspection"),
    ("HARDCOAT_DONE",      "🛡️", "Hardcoat Done"),
    ("ARC_SENT",           "📤", "Sent to ARC"),
    ("ARC_RECEIVED",       "📥", "ARC Received"),
    ("FINAL_QC",           "✅", "Final QC"),
    ("READY_FOR_PACK",     "📦", "Ready for Pack"),
]

_INHOUSE_NEXT = {
    "JOB_CREATED":       ["JOB_PRINTED"],
    "JOB_PRINTED":       ["PRODUCTION_PICKED"],
    "PRODUCTION_PICKED": ["SURFACING_DONE"],
    "SURFACING_DONE":    ["INSPECTION", "HARDCOAT_DONE"],
    "INSPECTION":        ["HARDCOAT_DONE", "FINAL_QC"],
    "HARDCOAT_DONE":     ["ARC_SENT", "FINAL_QC"],
    "ARC_SENT":          ["ARC_RECEIVED"],
    "ARC_RECEIVED":      ["FINAL_QC"],
    "FINAL_QC":          ["READY_FOR_PACK"],
    "READY_FOR_PACK":    [],
}

_INHOUSE_COLORS = {
    "JOB_CREATED": "#64748b", "JOB_PRINTED": "#3b82f6",
    "PRODUCTION_PICKED": "#f59e0b", "SURFACING_DONE": "#8b5cf6",
    "INSPECTION": "#0d9488", "HARDCOAT_DONE": "#a855f7",
    "ARC_SENT": "#0891b2", "ARC_RECEIVED": "#06b6d4",
    "FINAL_QC": "#10b981", "READY_FOR_PACK": "#059669",
}


def _stage_inhouse(order_id, lines, _rq, _rw):
    with st.expander("🔬 IN-HOUSE LAB — Production Stages", expanded=True):
        for ln in lines:
            lid = str(ln.get("line_id") or ln.get("id") or "")
            pn  = str(ln.get("product_name") or "Item")
            eye = str(ln.get("eye_side") or "")
            eye_badge = {"R": "👁R", "L": "👁L", "B": "👁👁"}.get(eye.upper(), "●")

            # Fetch job
            jobs = _rq(
                "SELECT id::text AS job_id, current_stage, is_closed FROM job_master "
                "WHERE order_line_id=%s::uuid AND NOT COALESCE(is_closed,FALSE) "
                "ORDER BY created_at DESC LIMIT 1", (lid,)
            ) if lid else []

            current = (jobs[0].get("current_stage") if jobs else None) or "JOB_CREATED"
            job_id  = jobs[0].get("job_id") if jobs else None
            color   = _INHOUSE_COLORS.get(current, "#64748b")
            next_stages = _INHOUSE_NEXT.get(current, [])

            st.markdown(
                f"**{eye_badge} {pn}** — "
                f"<span style='background:{color}22;color:{color};padding:2px 8px;"
                f"border-radius:6px;font-size:.72rem;font-weight:700'>{current}</span>",
                unsafe_allow_html=True,
            )

            if current == "READY_FOR_PACK":
                st.success("✅ Ready for dispatch")
                _check_and_advance_order(order_id, "READY", _rq, _rw)
            elif next_stages:
                btn_cols = st.columns(len(next_stages))
                for col, ns in zip(btn_cols, next_stages):
                    lbl_dict = {s[0]: s[2] for s in _INHOUSE_STAGES}
                    ic_dict  = {s[0]: s[1] for s in _INHOUSE_STAGES}
                    if col.button(
                        f"{ic_dict.get(ns,'')} {lbl_dict.get(ns, ns)}",
                        key=f"inh_{lid}_{ns}", use_container_width=True
                    ):
                        _advance_job_stage(job_id, lid, ns, order_id, _rq, _rw)
                        st.rerun()
            st.markdown("---")


def _advance_job_stage(job_id, line_id, new_stage, order_id, _rq, _rw):
    try:
        if job_id:
            _rw("UPDATE job_master SET current_stage=%s, updated_at=NOW() "
                "WHERE id=%s::uuid", (new_stage, job_id))
        else:
            # Create job row if none exists
            import uuid as _uuid
            _rw("INSERT INTO job_master (id, order_line_id, current_stage, is_closed, created_at) "
                "VALUES (%s::uuid, %s::uuid, %s, FALSE, NOW()) ON CONFLICT DO NOTHING",
                (str(_uuid.uuid4()), line_id, new_stage))
        if new_stage == "READY_FOR_PACK":
            _rw("UPDATE order_lines SET ready_qty = quantity "
                "WHERE id=%s::uuid", (line_id,))
            _check_and_advance_order(order_id, "READY", _rq, _rw)
        st.success(f"✅ Advanced to {new_stage}")
    except Exception as e:
        st.error(f"Stage advance failed: {e}")


# ── VENDOR (Supplier) ─────────────────────────────────────────────────────────

_VENDOR_STAGES  = ["DRAFT", "SENT", "ACKNOWLEDGED", "PARTIAL", "RECEIVED", "CLOSED"]
_VENDOR_ICONS   = {"DRAFT":"📝","SENT":"📤","ACKNOWLEDGED":"👍",
                   "PARTIAL":"⚡","RECEIVED":"📬","CLOSED":"🔒"}
_VENDOR_COLORS  = {"DRAFT":"#64748b","SENT":"#3b82f6","ACKNOWLEDGED":"#8b5cf6",
                   "PARTIAL":"#f59e0b","RECEIVED":"#10b981","CLOSED":"#374151"}
_VENDOR_NEXT    = {
    "DRAFT":       ["SENT"],
    "SENT":        ["ACKNOWLEDGED", "PARTIAL", "RECEIVED"],
    "ACKNOWLEDGED":["PARTIAL", "RECEIVED"],
    "PARTIAL":     ["RECEIVED"],
    "RECEIVED":    ["CLOSED"],
}


def _stage_vendor(order_id, lines, _rq, _rw):
    with st.expander("🏭 SUPPLIER — Purchase Order Stages", expanded=True):
        # Fetch existing PO(s) for this order
        ono = _rq("SELECT order_no FROM orders WHERE id=%s::uuid LIMIT 1",
                  (order_id,))
        order_no = ono[0]["order_no"] if ono else ""

        pos = _rq("""
            SELECT id, supplier_order_id, supplier_name,
                   status, total_qty, total_value,
                   expected_delivery_date
            FROM supplier_orders
            WHERE customer_order_id = %s
              AND COALESCE(status,'') != 'CANCELLED'
            ORDER BY created_at DESC LIMIT 5
        """, (order_no,)) if order_no else []

        if pos:
            for po in pos:
                stage   = str(po.get("status") or "DRAFT").upper()
                color   = _VENDOR_COLORS.get(stage, "#64748b")
                icon    = _VENDOR_ICONS.get(stage, "📋")
                po_no   = po.get("supplier_order_id") or str(po.get("id",""))
                sup_name= po.get("supplier_name","")
                exp_del = po.get("expected_delivery_date","")

                st.markdown(
                    f"**PO: {po_no}** — {sup_name}  "
                    f"<span style='background:{color}22;color:{color};padding:2px 8px;"
                    f"border-radius:6px;font-size:.72rem;font-weight:700'>{icon} {stage}</span>"
                    + (f"  <span style='color:#94a3b8;font-size:.7rem'>Expected: {exp_del}</span>"
                       if exp_del else ""),
                    unsafe_allow_html=True,
                )

                # Progress train
                _render_stage_train(_VENDOR_STAGES, stage, _VENDOR_ICONS, _VENDOR_COLORS)

                next_stages = _VENDOR_NEXT.get(stage, [])
                if next_stages and stage != "CLOSED":
                    po_id = po.get("id")
                    btn_cols = st.columns(len(next_stages))
                    for col, ns in zip(btn_cols, next_stages):
                        if col.button(
                            f"{_VENDOR_ICONS.get(ns,'')} {ns}",
                            key=f"vnd_{po_id}_{ns}", use_container_width=True
                        ):
                            try:
                                _rw("UPDATE supplier_orders SET status=%s, "
                                    "updated_at=NOW() WHERE id=%s",
                                    (ns, po_id))
                                if ns in ("RECEIVED", "CLOSED"):
                                    # Mark order_lines ready
                                    for ln in lines:
                                        lid = str(ln.get("line_id") or ln.get("id") or "")
                                        if lid:
                                            _rw("UPDATE order_lines SET ready_qty=quantity, allocated_qty=quantity WHERE id=%s::uuid", (lid,))
                                    _check_and_advance_order(order_id, "READY", _rq, _rw)
                                st.success(f"✅ PO → {ns}")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")

                if stage == "RECEIVED":
                    # Receive qty per line
                    with st.expander("📥 Record Received Qty", expanded=False):
                        _render_receive_qty(po.get("id"), lines, order_id, _rq, _rw)

                st.markdown("---")
        else:
            # No PO yet — show supplier search + create PO button
            st.info("No supplier PO created yet for this order.")
            _render_create_po_quick(order_id, order_no, lines, _rq, _rw)


def _render_create_po_quick(order_id, order_no, lines, _rq, _rw):
    """Quick PO creation panel."""
    suppliers = _rq(
        "SELECT id::text, party_name FROM parties "
        "WHERE party_type = 'Supplier' AND COALESCE(is_active,TRUE)=TRUE "
        "ORDER BY party_name LIMIT 50", {}
    )
    if not suppliers:
        st.warning("No suppliers found. Add a supplier in CRM first.")
        return

    sup_map = {p["party_name"]: p["id"] for p in suppliers}
    sel_sup = st.selectbox("Select Supplier", list(sup_map.keys()),
                           key=f"po_sup_{order_id}")
    exp_date = st.date_input("Expected Delivery", key=f"po_exp_{order_id}",
                             value=__import__("datetime").date.today()
                             + __import__("datetime").timedelta(days=7))
    total_qty = sum(int(l.get("billing_qty") or l.get("quantity") or 0) for l in lines)
    total_val = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in lines)

    if st.button("📋 Create PO (DRAFT)", key=f"po_create_{order_id}",
                 type="primary", use_container_width=True):
        import uuid as _uuid2, datetime as _dt2
        sup_id = sup_map[sel_sup]
        po_no  = "PO-" + _dt2.datetime.now().strftime("%Y%m%d%H%M%S")
        try:
            _sync_supplier_orders_id_sequence()
            _rw("""
                INSERT INTO supplier_orders
                    (supplier_order_id, supplier_id, supplier_name,
                     customer_order_id, order_date, expected_delivery_date,
                     status, total_qty, total_value, created_by)
                VALUES (%s, %s::uuid, %s, %s, %s, %s, 'DRAFT', %s, %s, %s)
            """, (po_no, sup_id, sel_sup, order_no,
                  _dt2.date.today(), exp_date,
                  total_qty, total_val,
                  st.session_state.get("user_name", "Staff")))
            st.success(f"✅ PO {po_no} created (DRAFT)")
            st.rerun()
        except Exception as e:
            st.error(f"PO creation failed: {e}")


def _render_receive_qty(po_id, lines, order_id, _rq, _rw):
    """Record received qty against each line item."""
    items = _rq("""
        SELECT id, product_name, eye_side, ordered_qty,
               COALESCE(received_qty,0) AS received_qty, item_status
        FROM supplier_order_items WHERE supplier_order_id=%s
    """, (po_id,)) if po_id else []

    if not items:
        st.caption("No line items on this PO.")
        return

    for itm in items:
        iid     = itm["id"]
        pn      = itm.get("product_name","")
        eye     = itm.get("eye_side","")
        ord_q   = int(itm.get("ordered_qty") or 0)
        rcv_q   = int(itm.get("received_qty") or 0)
        pending = max(ord_q - rcv_q, 0)
        c1, c2, c3 = st.columns([3, 1, 1])
        c1.caption(f"{eye} {pn}  Ord:{ord_q}  Rcv:{rcv_q}  Pend:{pending}")
        new_rcv = c2.number_input("Qty", min_value=0, max_value=ord_q,
                                  value=rcv_q, step=1,
                                  key=f"rcv_{iid}")
        if c3.button("Save", key=f"rcv_save_{iid}", use_container_width=True):
            new_status = "RECEIVED" if new_rcv >= ord_q else ("PARTIAL" if new_rcv > 0 else "ORDERED")
            try:
                _rw("""UPDATE supplier_order_items SET received_qty=%s,
                           pending_qty=%s, item_status=%s
                       WHERE id=%s""",
                    (new_rcv, max(ord_q - new_rcv, 0), new_status, iid))
                st.success("✅ Saved")
                st.rerun()
            except Exception as e:
                st.error(f"Save failed: {e}")


# ── EXTERNAL LAB ────────────────────────────────────────────────────────────────

_LAB_STAGES  = ["ORDER_PLACED", "ACKNOWLEDGED", "IN_PROCESSING",
                "DISPATCHED_BY_LAB", "RECEIVED_FROM_LAB", "QC_DONE", "READY"]
_LAB_ICONS   = {"ORDER_PLACED":"📤","ACKNOWLEDGED":"👍","IN_PROCESSING":"⚙️",
                "DISPATCHED_BY_LAB":"🚚","RECEIVED_FROM_LAB":"📥",
                "QC_DONE":"✅","READY":"📦"}
_LAB_COLORS  = {"ORDER_PLACED":"#3b82f6","ACKNOWLEDGED":"#8b5cf6",
                "IN_PROCESSING":"#f59e0b","DISPATCHED_BY_LAB":"#0891b2",
                "RECEIVED_FROM_LAB":"#10b981","QC_DONE":"#059669","READY":"#16a34a"}
_LAB_NEXT    = {
    "ORDER_PLACED":       ["ACKNOWLEDGED"],
    "ACKNOWLEDGED":       ["IN_PROCESSING"],
    "IN_PROCESSING":      ["DISPATCHED_BY_LAB"],
    "DISPATCHED_BY_LAB":  ["RECEIVED_FROM_LAB"],
    "RECEIVED_FROM_LAB":  ["QC_DONE"],
    "QC_DONE":            ["READY"],
}


def _stage_external_lab(order_id, lines, _rq, _rw):
    with st.expander("🔬 EXTERNAL LAB — Lab Order Stages", expanded=True):
        ono = _rq("SELECT order_no FROM orders WHERE id=%s::uuid LIMIT 1", (order_id,))
        order_no = ono[0]["order_no"] if ono else ""

        # Reuse supplier_orders table with lab supplier
        lab_orders = _rq("""
            SELECT id, supplier_order_id, supplier_name, status,
                   expected_delivery_date
            FROM supplier_orders
            WHERE customer_order_id = %s
              AND COALESCE(status,'') != 'CANCELLED'
            ORDER BY created_at DESC LIMIT 5
        """, (order_no,)) if order_no else []

        if lab_orders:
            for lo in lab_orders:
                stage = str(lo.get("status") or "ORDER_PLACED").upper()
                # Normalise old VENDOR stages to LAB stages
                stage_map = {"SENT": "ORDER_PLACED", "DRAFT": "ORDER_PLACED",
                             "RECEIVED": "RECEIVED_FROM_LAB", "CLOSED": "READY"}
                stage = stage_map.get(stage, stage)

                color  = _LAB_COLORS.get(stage, "#64748b")
                icon   = _LAB_ICONS.get(stage, "📋")
                lo_no  = lo.get("supplier_order_id") or str(lo.get("id",""))
                lab    = lo.get("supplier_name","")

                st.markdown(
                    f"**Lab Order: {lo_no}** — {lab}  "
                    f"<span style='background:{color}22;color:{color};padding:2px 8px;"
                    f"border-radius:6px;font-size:.72rem;font-weight:700'>{icon} {stage}</span>",
                    unsafe_allow_html=True,
                )
                _render_stage_train(_LAB_STAGES, stage, _LAB_ICONS, _LAB_COLORS)

                next_stages = _LAB_NEXT.get(stage, [])
                if next_stages and stage != "READY":
                    lo_id    = lo.get("id")
                    btn_cols = st.columns(len(next_stages))
                    for col, ns in zip(btn_cols, next_stages):
                        db_ns = {"RECEIVED_FROM_LAB": "RECEIVED",
                                 "ORDER_PLACED": "SENT"}.get(ns, ns)
                        if col.button(
                            f"{_LAB_ICONS.get(ns,'')} {ns.replace('_',' ').title()}",
                            key=f"lab_{lo_id}_{ns}", use_container_width=True
                        ):
                            try:
                                _rw("UPDATE supplier_orders SET status=%s, "
                                    "updated_at=NOW() WHERE id=%s", (db_ns, lo_id))
                                if ns in ("QC_DONE", "READY"):
                                    for ln in lines:
                                        lid = str(ln.get("line_id") or ln.get("id") or "")
                                        if lid:
                                            _rw("UPDATE order_lines SET "
                                                "ready_qty=quantity, allocated_qty=quantity "
                                                "WHERE id=%s::uuid", (lid,))
                                    _check_and_advance_order(order_id, "READY", _rq, _rw)
                                st.success(f"✅ Lab order → {ns.replace('_',' ').title()}")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")
                st.markdown("---")
        else:
            st.info("No lab order placed yet for this order.")
            _render_create_lab_order(order_id, order_no, lines, _rq, _rw)


def _render_create_lab_order(order_id, order_no, lines, _rq, _rw):
    labs = _rq(
        "SELECT id::text, party_name FROM parties "
        "WHERE UPPER(COALESCE(party_type,'')) IN ('LAB','EXTERNAL_LAB','SUPPLIER') "
        "AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name LIMIT 50", {}
    )
    if not labs:
        # Fallback to all suppliers
        labs = _rq(
            "SELECT id::text, party_name FROM parties "
            "WHERE COALESCE(is_active,TRUE)=TRUE ORDER BY party_name LIMIT 50", {}
        )

    if not labs:
        st.warning("No labs found. Add a lab as a supplier in CRM.")
        return

    lab_map  = {p["party_name"]: p["id"] for p in labs}
    sel_lab  = st.selectbox("Select Lab", list(lab_map.keys()),
                            key=f"lab_sel_{order_id}")
    exp_date = st.date_input("Expected Return Date", key=f"lab_exp_{order_id}",
                             value=__import__("datetime").date.today()
                             + __import__("datetime").timedelta(days=5))
    remarks  = st.text_input("Remarks / Power notes", key=f"lab_rmk_{order_id}")

    if st.button("📤 Place Lab Order", key=f"lab_create_{order_id}",
                 type="primary", use_container_width=True):
        import uuid as _uuid3, datetime as _dt3
        lab_id = lab_map[sel_lab]
        lo_no  = "LAB-" + _dt3.datetime.now().strftime("%Y%m%d%H%M%S")
        total_qty = sum(int(l.get("billing_qty") or l.get("quantity") or 0) for l in lines)
        total_val = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in lines)
        try:
            _sync_supplier_orders_id_sequence()
            _rw("""
                INSERT INTO supplier_orders
                    (supplier_order_id, supplier_id, supplier_name,
                     customer_order_id, order_date, expected_delivery_date,
                     status, total_qty, total_value, created_by)
                VALUES (%s, %s::uuid, %s, %s, %s, %s, 'SENT', %s, %s, %s)
            """, (lo_no, lab_id, sel_lab, order_no,
                  _dt3.date.today(), exp_date,
                  total_qty, total_val,
                  st.session_state.get("user_name","Staff")))
            st.success(f"✅ Lab Order {lo_no} placed")
            st.rerun()
        except Exception as e:
            st.error(f"Lab order failed: {e}")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _render_stage_train(stages, current, icons, colors):
    """Horizontal progress dots showing stage progression."""
    dots = []
    try:
        cur_idx = stages.index(current)
    except ValueError:
        cur_idx = -1

    for i, s in enumerate(stages):
        done   = i < cur_idx
        active = i == cur_idx
        bg     = colors.get(s, "#64748b") if (done or active) else "#1e293b"
        icon   = "✓" if done else icons.get(s, "●")
        ring   = "box-shadow:0 0 0 2px {};".format(colors.get(s,"#64748b")) if active else ""
        dots.append(
            "<div style='display:inline-flex;flex-direction:column;align-items:center;"
            "min-width:36px;margin:0 2px'>"
            "<div style='background:{bg};color:#fff;width:22px;height:22px;"
            "border-radius:50%;display:flex;align-items:center;justify-content:center;"
            "font-size:0.65rem;{ring}'>{icon}</div>"
            "<div style='font-size:0.5rem;color:#64748b;text-align:center;"
            "margin-top:2px;max-width:36px'>{label}</div>"
            "</div>".format(
                bg=bg, ring=ring, icon=icon,
                label=s.replace("_"," ").title()[:8]
            )
        )
    st.markdown(
        "<div style='display:flex;align-items:center;padding:6px 0'>"
        + "".join(dots)
        + "</div>",
        unsafe_allow_html=True,
    )


def _check_and_advance_order(order_id, target_status, _rq, _rw):
    """Advance order to target_status if ALL lines are ready."""
    try:
        rows = _rq("""
            SELECT COUNT(*) FILTER (WHERE COALESCE(ready_qty,0) < quantity) AS pending
            FROM order_lines
            WHERE order_id = %s::uuid
              AND COALESCE(is_deleted, FALSE) = FALSE
        """, (order_id,))
        pending = int(rows[0]["pending"] if rows else 0)
        if pending == 0:
            _rw("""UPDATE orders SET status=%s
                   WHERE id=%s::uuid AND status NOT IN ('BILLED','DISPATCHED',
                   'DELIVERED','CLOSED','CANCELLED')""",
                (target_status, order_id))
    except Exception:
        pass
