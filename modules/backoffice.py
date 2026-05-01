"""
Backoffice Main Orchestrator
v3 — Production Train + Smart Dashboard (all self-contained)
"""

import streamlit as st
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
)
from .backoffice_ui import render_order_detail
from modules.workflow.status import OrderStatus
from modules.backoffice_clinical_viewer import render_clinical_viewer_page


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
    "PENDING", "UNDER_REVIEW", "CONFIRMED", "IN_PRODUCTION", "READY",
    "BILLED", "DISPATCHED", "DELIVERED", "CLOSED", "CANCELLED",
]

_TRANSITIONS = {
    # BILLED removed — billing status is live from challan/invoice system
    # DISPATCHED is the only manual action after billing
    "PENDING":           ["UNDER_REVIEW", "CONFIRMED", "CANCELLED"],
    "CONFIRMED":         ["IN_PRODUCTION", "READY", "CANCELLED"],
    "IN_PRODUCTION":     ["READY", "CANCELLED"],
    "READY":             ["DISPATCHED"],
    "READY_FOR_BILLING": ["DISPATCHED"],
    "BILLED":            ["DISPATCHED"],
    "DISPATCHED":        ["DELIVERED"],
    "DELIVERED":         ["CLOSED"],
    "CLOSED":            [],
    "CANCELLED":         [],
    "PROVISIONAL":       ["UNDER_REVIEW", "CONFIRMED", "CANCELLED"],
    "UNDER_REVIEW":      ["CONFIRMED", "CANCELLED"],
}

try:
    from modules.backoffice.order_status_live import STATUS_META as _OSL_META
    _STATUS_COLOR = {k: v["color"] for k, v in _OSL_META.items()}
    _STATUS_ICON  = {k: v["icon"]  for k, v in _OSL_META.items()}
except Exception:
    _STATUS_COLOR = {
        "PENDING":"#64748b","PROVISIONAL":"#64748b","UNDER_REVIEW":"#f59e0b",
        "CONFIRMED":"#3b82f6","IN_PRODUCTION":"#8b5cf6","READY":"#10b981",
        "BILLED":"#059669","DISPATCHED":"#0891b2","DELIVERED":"#10b981",
        "CLOSED":"#334155","CANCELLED":"#ef4444",
    }
    _STATUS_ICON = {
        "PENDING":"⏳","PROVISIONAL":"📝","UNDER_REVIEW":"🔍","CONFIRMED":"✅",
        "IN_PRODUCTION":"⚙️","READY":"📦","BILLED":"🧾",
        "DISPATCHED":"🚚","DELIVERED":"✅","CLOSED":"🔒","CANCELLED":"❌",
    }
_STATUS_COLOR.setdefault("PROVISIONAL", "#64748b")
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
        "bo_include_closed": False,
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
    for src in ("stock_lines", "inhouse_lines", "lab_order_lines", "lines"):
        for l in (order.get(src) or []):
            if isinstance(l, dict) and id(l) not in seen:
                seen.add(id(l)); out.append(l)
    return out


def _route_summary(lines: list) -> dict:
    s = {}
    for l in lines:
        r = l.get("manufacturing_route") or "STOCK"
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
                    "SELECT cn_no, cn_amount, status FROM credit_notes WHERE order_no=%s",
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

    # ── Determine who can cancel at this stage ────────────────────────────
    _is_pre_prod   = raw_status in {"PENDING","PROVISIONAL","UNDER_REVIEW","CONFIRMED"}
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
                        CREATE TABLE IF NOT EXISTS credit_notes (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            cn_no TEXT UNIQUE NOT NULL,
                            order_id UUID,
                            order_no TEXT,
                            party_name TEXT,
                            order_type TEXT,
                            cn_amount NUMERIC(12,2),
                            cn_type TEXT,
                            reason TEXT,
                            notes TEXT,
                            status TEXT DEFAULT 'DRAFT',
                            refund_mode TEXT,
                            refund_amount NUMERIC(12,2) DEFAULT 0,
                            refund_ref TEXT,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        )
                    """)
                    _rw_cx("""
                        INSERT INTO credit_notes
                            (cn_no, order_no, party_name, cn_amount, cn_type, reason, status)
                        VALUES (%(cn)s, %(ono)s, %(party)s, %(amt)s, %(ct)s, %(r)s, 'DRAFT')
                        ON CONFLICT (cn_no) DO NOTHING
                    """, {
                        "cn": _cn_no, "ono": order_no, "party": party,
                        "amt": _cn_amt, "ct": "FULL" if "Full" in _cn_type else "PARTIAL",
                        "r": _final_reason
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
                                    "refund_mode=%(m)s, refund_amount=%(a)s, "
                                    "refund_ref=%(r)s, updated_at=NOW() "
                                    "WHERE cn_no=%(cn)s",
                                    {
                                        "m": _refund_mode, "a": _refund_amount,
                                        "r": _refund_ref or "",
                                        "cn": _existing_cn["cn_no"]
                                    }
                                )
                            except Exception:
                                pass
                            st.session_state.pop(_cn_session_key, None)

                        st.session_state.pop(_step2, None)
                        st.success(
                            f"✅ Order {order_no} cancelled."
                            + (f" Refund ₹{_refund_amount:,.2f} recorded." if _refund_amount > 0 else "")
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

    # Build product+power lines from order lines
    def _fmt_pw(sph, cyl, axis, add):
        def _f(v):
            if v is None: return None
            try:
                n = float(v)
                return ("{:+.2f}".format(n) if n != 0 else "0.00")
            except Exception: return None
        parts = []
        if _f(sph):  parts.append("Sph " + _f(sph))
        if _f(cyl):  parts.append("Cyl " + _f(cyl))
        if axis is not None:
            try: parts.append("Ax " + str(int(float(axis))))
            except Exception: pass
        if _f(add):  parts.append("Add " + _f(add))
        return "  ".join(parts)

    lines = order.get("lines") or []
    prod_block = ""
    for ln in lines:
        if not isinstance(ln, dict): continue
        eye   = str(ln.get("eye_side") or "").upper()
        pname = str(ln.get("product_name") or "")
        brand = str(ln.get("brand") or "")
        qty   = ln.get("billing_qty") or ln.get("quantity") or 0
        total = float(ln.get("billing_total") or ln.get("total_price") or 0)
        elbl  = {"R":"👁 Right","L":"👁 Left","B":"👁👁 Both"}.get(eye, "")
        prod  = (brand + " " + pname).strip() if brand else pname
        pw    = _fmt_pw(ln.get("sph"), ln.get("cyl"), ln.get("axis"), ln.get("add_power"))
        row   = []
        if elbl: row.append("*" + elbl + "*")
        if prod: row.append(prod)
        if qty and int(qty) > 0: row.append("Qty:" + str(qty))
        if pw:   row.append("[" + pw + "]")
        if total > 0: row.append("Rs." + "{:,.0f}".format(total))
        if row:  prod_block += "  ".join(row) + nl

    # Expected supply date
    esd = order.get("expected_supply_date")
    esd_str = ""
    if esd:
        try:
            from datetime import date, datetime
            if hasattr(esd, "strftime"):
                esd_str = esd.strftime("%d %b %Y")
            else:
                esd_str = str(esd)[:10]
        except Exception:
            esd_str = str(esd)

    # Compose rich message per status
    if new_status == "CONFIRMED":
        m  = "Hello " + party + " 👋" + nl + nl
        m += "✅ *Order Confirmed!*" + nl
        m += "📋 Order: *" + order_no + "*" + nl
        m += "🏪 " + shop_name + nl
        if prod_block:
            m += nl + "📦 *Order Details:*" + nl + prod_block
        if esd_str:
            m += nl + "📅 *Expected Supply: " + esd_str + "*" + nl
        m += nl + "We will keep you updated on progress." + nl
        if shop_phone: m += "Queries: " + shop_phone + nl
        m += nl + "Thank you! 🙏 " + shop_name

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
    """Delegates to wa_engine for consistent cleaning across all modules."""
    try:
        from modules.wa_engine import _clean_mobile
        import urllib.parse as _up
        c = _clean_mobile(mobile)
        return "https://wa.me/91{}?text={}".format(c, _up.quote(msg)) if c else ""
    except Exception:
        import urllib.parse as _up
        c = "".join(x for x in (mobile or "") if x.isdigit())
        if c.startswith("91") and len(c) == 12: c = c[2:]
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
            UNION ALL
            SELECT 'INVOICE'  AS doc_type,
                   i.invoice_no AS doc_no,
                   i.status,
                   i.grand_total,
                   i.invoice_date::date AS doc_date
            FROM invoices i
            WHERE %(ono)s = ANY(i.order_ids)
              AND i.status NOT IN ('CANCELLED','VOID')
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
                UNION ALL
                SELECT 1 FROM invoices
                WHERE (
                    order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR order_ids::text[] @> ARRAY[%(ono)s::text]
                )
                AND status NOT IN ('CANCELLED','VOID')
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

    # Check if all product lines are allocated
    _is_fully_alloc = all(
        int(l.get("allocated_qty") or 0) >= int(l.get("billing_qty") or l.get("quantity") or 0)
        for l in _chk_lines
    )

    # Check if all lines have pricing
    _is_priced = all(float(l.get("unit_price") or 0) > 0 for l in _chk_lines)
    
    return _is_fully_alloc and _is_priced


def _render_order_card(order: dict, idx: int) -> None:
    order_id   = get_display_order_id(order)
    status     = order.get("status") or "PENDING"
    party      = order.get("patient_name") or order.get("party_name") or "—"
    order_type = (order.get("order_type") or "RETAIL").upper()
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
    routes     = _route_summary(lines)
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
        f"<span style='background:{tc}22;color:{tc};padding:1px 8px;border-radius:8px;"
        f"font-size:0.6rem;font-weight:700'>{order_type}</span>"
        f"{urgency_badge}</div>"
        f"<div style='color:#cbd5e1;font-size:0.85rem;font-weight:600;margin-top:3px'>{party}</div>"
        f"<div style='margin-top:6px'>{route_pills}</div>"
        f"</div>"
        f"<div style='text-align:right;min-width:130px'>"
        f"<div style='background:{sc};color:#fff;padding:3px 12px;"
        f"border-radius:16px;font-size:0.78rem;font-weight:700;display:inline-block'>"
        f"{si} {status}</div>"
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


    # ── Action row ───────────────────────────────────────────────────────────
    btn_cols = st.columns([2, 5, 3])

    with btn_cols[0]:
        if st.button("🔍 Open", key=f"open_{order_id}_{idx}",
                     use_container_width=True, type="primary"):
            st.session_state["bo_selected_order_id"] = order_id
            st.session_state["bo_view_mode"] = "order_detail"
            st.rerun()

    with btn_cols[1]:
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
        # Show Cancel button HERE (replaces the Confirmed suggestion button).
        # Green forward-progress button is NOT shown — cancel is the only card action.
        # Staff can open the detail to move to Confirmed if needed.
        elif status in ("PENDING", "PROVISIONAL", "UNDER_REVIEW"):
            from modules.security.roles import has_role as _hr_card
            if _hr_card("admin","manager","billing"):
                _ckey = f"_quick_cancel_{order_id}_{idx}"
                if not st.session_state.get(_ckey):
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
            mob = str(order.get("patient_mobile") or "")
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
        "SELECT patient_name, party_name, patient_mobile, order_no, "
        "COALESCE(expected_supply_date::text,'') AS esd "
        "FROM orders WHERE id=%s::uuid LIMIT 1", (order_id,)) or [{}])[0]

    _mobile   = str(_order_row.get("patient_mobile") or "")
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
    route   = line.get("manufacturing_route") or "STOCK"
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

    # ── Controls ──────────────────────────────────────────────────────────────
    c_load, c_search, c_status, c_type, c_route = st.columns([1, 3, 2, 2, 2])

    with c_load:
        if st.button("🔄 Load", use_container_width=True, help="Refresh from DB"):
            with st.spinner("Loading…"):
                load_orders_from_database.clear()
                _inc_closed = st.session_state.get("bo_include_closed", False)
                try:
                    st.session_state.bo_active_orders = load_orders_from_database(
                        include_closed=_inc_closed
                    )
                except TypeError:
                    # older helpers.py without include_closed param — safe fallback
                    st.session_state.bo_active_orders = load_orders_from_database()
                st.session_state.bo_orders_loaded = True
            st.rerun()

    # Pre-fill from clinical bill button
    _presearch = st.session_state.pop("bo_search_term", None)
    _preconsult = st.session_state.pop("bo_filter_consult", False)
    if _presearch:
        st.session_state["bo_search_query"] = _presearch
    if _preconsult:
        st.session_state["bo_type_filter"] = "CONSULTATION"

    with c_search:
        search = st.text_input(
            "search", key="bo_search_query",
            label_visibility="collapsed",
            placeholder="🔍 Search by order ID or party name…",
        )

    with c_status:
        status_filter = st.selectbox(
            "Status", ["All"] + _ALL_STATUSES,
            key="bo_status_filter", label_visibility="collapsed",
        )

    with c_type:
        type_filter = st.selectbox(
            "Type", ["All", "RETAIL", "WHOLESALE", "PURCHASE", "CONSULTATION"],
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

    # Show closed toggle — reloads when toggled
    tog_col1, tog_col2 = st.columns([1, 9])
    with tog_col1:
        inc_closed = st.toggle(
            "Show closed", key="bo_include_closed",
            help="Include CLOSED / DELIVERED / CANCELLED orders"
        )
    if st.session_state.bo_orders_loaded:
        # If toggle changed, force reload on next Load press
        _expected = not inc_closed  # if showing closed, we loaded with include_closed=True
        pass  # reload handled by Load button

    if not st.session_state.bo_orders_loaded:
        st.markdown("")
        st.info("👆 Click **Load** to fetch orders from the database.")
        return

    orders = st.session_state.bo_active_orders
    if not orders:
        st.info("No active orders found.")
        return

    # ── Filters ───────────────────────────────────────────────────────────────
    filtered = orders

    if status_filter != "All":
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
            if any(l.get("manufacturing_route") == rk for l in _order_lines(o))
        ]

    if search:
        q = search.lower().strip()
        def _order_matches(o):
            # Order-level fields
            if q in get_display_order_id(o).lower():
                return True
            if q in (o.get("patient_name") or "").lower():
                return True
            if q in (o.get("party_name") or "").lower():
                return True
            if q in (o.get("patient_mobile") or o.get("mobile") or "").lower():
                return True
            if q in (o.get("case_no") or o.get("customer_order_no") or "").lower():
                return True
            if q in str(o.get("order_date") or o.get("created_at") or "")[:10]:
                return True
            if q in (o.get("status") or "").lower():
                return True
            if q in (o.get("order_type") or "").lower():
                return True
            # Line-level fields (product name, brand, power)
            for _l in _order_lines(o):
                if q in (str(_l.get("product_name") or "")).lower():
                    return True
                if q in (str(_l.get("brand") or "")).lower():
                    return True
                if q in (str(_l.get("sph") or "")).lower():
                    return True
            return False
        filtered = [o for o in filtered if _order_matches(o)]

    # ── Summary bar ───────────────────────────────────────────────────────────
    urgent  = sum(1 for o in filtered
                  if _days_old(o.get("order_date") or o.get("created_at")) > 7
                  and o.get("status") not in _TERMINAL)
    pending = sum(1 for o in filtered if o.get("status") == "PENDING")
    total_v = sum(
        sum(float(l.get("total_price") or 0) + float(l.get("gst_amount") or 0)
            for l in _order_lines(o))
        for o in filtered
    )

    sc1, sc2, sc3, sc4 = st.columns(4)
    for col, val, lbl, color in [
        (sc1, f"{len(filtered)} / {len(orders)}", "Showing",          "#3b82f6"),
        (sc2, str(pending),                        "Pending action",   "#f59e0b"),
        (sc3, str(urgent),                         "Urgent (7d+)",     "#ef4444"),
        (sc4, f"₹{total_v:,.0f}",                  "Total (filtered)", "#10b981"),
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

    # ── Order cards ───────────────────────────────────────────────────────────
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

    nav1, nav2, _spacer = st.columns([1, 1, 4])
    with nav1:
        if st.button("📦 Orders", use_container_width=True):
            st.session_state.bo_view_mode = "dashboard"
            st.rerun()
    with nav2:
        if st.button("🩺 Clinical Records", use_container_width=True):
            st.session_state.bo_view_mode = "clinical_records"
            st.rerun()

    st.markdown("---")

    view = st.session_state.bo_view_mode
    if view == "dashboard":
        render_backoffice_dashboard()
    elif view == "order_detail":
        render_order_detail()
    elif view == "clinical_records":
        render_clinical_viewer_page()


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
    Renders per-line fulfillment stage controls based on manufacturing_route.
    Groups lines by route, shows the appropriate panel for each.
    """
    if not lines:
        return

    from modules.sql_adapter import run_query as _rq, run_write as _rw

    # Group lines by route
    route_groups: dict = {}
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        r = str(ln.get("manufacturing_route") or "STOCK").upper()
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
                                   ready_qty = quantity, updated_at = NOW()
                               WHERE id = %s::uuid""", (lid,))
                        st.success("✅ Stock allocated")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Alloc failed: {e}")

            if alloc >= qty and rdy < qty:
                if col3.button("✅ Mark Ready", key=f"stock_rdy_{lid}",
                               use_container_width=True):
                    try:
                        _rw("""UPDATE order_lines SET ready_qty = quantity,
                                   updated_at = NOW()
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
            _rw("UPDATE order_lines SET ready_qty = quantity, updated_at=NOW() "
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
                                            _rw("UPDATE order_lines SET ready_qty=quantity, "
                                                "allocated_qty=quantity, updated_at=NOW() "
                                                "WHERE id=%s::uuid", (lid,))
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
                           pending_qty=%s, item_status=%s, updated_at=NOW()
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
                                                "ready_qty=quantity, allocated_qty=quantity, "
                                                "updated_at=NOW() WHERE id=%s::uuid", (lid,))
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
            _rw("""UPDATE orders SET status=%s, updated_at=NOW()
                   WHERE id=%s::uuid AND status NOT IN ('BILLED','DISPATCHED',
                   'DELIVERED','CLOSED','CANCELLED')""",
                (target_status, order_id))
    except Exception:
        pass
