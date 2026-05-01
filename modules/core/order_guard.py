"""
modules/core/order_guard.py
============================
Order Guard — Central Permission Controller

Single source of truth for EVERY order action across the ERP:
  - Edit (open in punching screen)
  - Cancel
  - Status change
  - Line edit
  - Allocation change
  - Price edit

Rules are data-driven — change PERMISSION_MAP to change behaviour
everywhere in the system simultaneously.

Usage:
    from modules.core.order_guard import OrderGuard, render_cancelled_banner

    guard = OrderGuard(order)
    if not guard.can_edit:
        guard.render_block("edit")
        return

    if guard.can_cancel:
        # show cancel button
"""

from __future__ import annotations
from typing import Optional
import streamlit as st


# ─────────────────────────────────────────────────────────────────────────────
# PERMISSION MAP — edit here to change rules everywhere
# ─────────────────────────────────────────────────────────────────────────────

# Status → {action: [roles_allowed]}
# Empty list [] = nobody can do it
# None = everyone can do it (no role restriction)

PERMISSION_MAP: dict[str, dict[str, list[str] | None]] = {
    "PENDING": {
        "edit":           ["admin", "manager", "billing"],
        "cancel":         ["admin", "manager", "billing"],
        "status_change":  ["admin", "manager", "billing"],
        "line_edit":      ["admin", "manager", "billing"],
        "alloc_change":   ["admin", "manager", "billing"],
        "price_edit":     ["admin", "manager"],
    },
    "PROVISIONAL": {
        "edit":           ["admin", "manager", "billing"],
        "cancel":         ["admin", "manager", "billing"],
        "status_change":  ["admin", "manager", "billing"],
        "line_edit":      ["admin", "manager", "billing"],
        "alloc_change":   ["admin", "manager", "billing"],
        "price_edit":     ["admin", "manager"],
    },
    "UNDER_REVIEW": {
        "edit":           ["admin", "manager", "billing"],
        "cancel":         ["admin", "manager", "billing"],
        "status_change":  ["admin", "manager"],
        "line_edit":      ["admin", "manager", "billing"],
        "alloc_change":   ["admin", "manager"],
        "price_edit":     ["admin", "manager"],
    },
    "CONFIRMED": {
        "edit":           ["admin", "manager"],         # manager/admin only
        "cancel":         ["admin", "manager"],         # manager/admin only
        "status_change":  ["admin", "manager", "lab"],
        "line_edit":      ["admin", "manager"],
        "alloc_change":   ["admin", "manager", "lab"],
        "price_edit":     ["admin"],
    },
    "IN_PRODUCTION": {
        "edit":           [],                           # nobody — must release first
        "cancel":         ["admin", "manager"],         # manager/admin, job cards must close
        "status_change":  ["admin", "manager", "lab"],
        "line_edit":      [],
        "alloc_change":   ["admin", "manager", "lab"],
        "price_edit":     ["admin"],
    },
    "READY": {
        "edit":           [],                           # nobody — must release first
        "cancel":         ["admin", "manager"],
        "status_change":  ["admin", "manager", "lab"],
        "line_edit":      [],
        "alloc_change":   ["admin", "manager", "lab"],
        "price_edit":     ["admin"],
    },
    # ── Billing pipeline statuses ─────────────────────────────────────────────
    # Sent to billing dashboard — fully frozen until invoice raised
    "READY_FOR_BILLING": {
        "edit":           [],
        "cancel":         ["admin", "manager"],
        "status_change":  ["admin", "manager"],
        "line_edit":      [],                           # FROZEN — billing in progress
        "alloc_change":   [],
        "recall":         ["admin", "manager"],         # pull back to CONFIRMED
        "price_edit":     [],
    },
    "PARTIALLY_BILLED": {
        "edit":           [],
        "cancel":         ["admin", "manager"],
        "status_change":  ["admin", "manager"],
        "line_edit":      [],                           # FROZEN — partial billing in progress
        "alloc_change":   [],
        "recall":         ["admin", "manager"],
        "price_edit":     [],
    },
    "BILLED": {
        "edit":           [],
        "cancel":         ["admin"],                    # admin only + credit note
        "status_change":  ["admin", "manager"],
        "line_edit":      [],
        "alloc_change":   [],
        "price_edit":     [],
    },
    "DISPATCHED": {
        "edit":           [],
        "cancel":         ["admin"],
        "status_change":  ["admin", "manager"],
        "line_edit":      [],
        "alloc_change":   [],
        "price_edit":     [],
    },
    "DELIVERED": {
        "edit":           [],
        "cancel":         ["admin"],
        "status_change":  ["admin"],
        "line_edit":      [],
        "alloc_change":   [],
        "price_edit":     [],
    },
    "CLOSED": {
        "edit":           [],
        "cancel":         ["admin"],
        "status_change":  ["admin"],
        "line_edit":      [],
        "alloc_change":   [],
        "price_edit":     [],
    },
    # ── TERMINAL — nothing allowed ────────────────────────────────────────────
    "CANCELLED": {
        "edit":           [],
        "cancel":         [],
        "status_change":  [],
        "line_edit":      [],
        "alloc_change":   [],
        "price_edit":     [],
    },
    "RETURNED": {
        "edit":           [],
        "cancel":         [],
        "status_change":  ["admin"],
        "line_edit":      [],
        "alloc_change":   [],
        "price_edit":     [],
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# MESSAGES shown when blocked
# ─────────────────────────────────────────────────────────────────────────────

BLOCK_MESSAGES: dict[str, str] = {
    "edit":          "This order cannot be edited at this stage.",
    "cancel":        "Cancellation is not allowed at this stage.",
    "status_change": "Status cannot be changed at this stage.",
    "line_edit":     "Line items cannot be edited at this stage.",
    "alloc_change":  "Allocations cannot be changed at this stage.",
    "price_edit":    "Prices cannot be edited at this stage.",
}

CANCEL_BLOCKED_MSG: dict[str, str] = {
    "CANCELLED":    "This order is already cancelled.",
    "RETURNED":     "This order has been returned. Raise a credit note if needed.",
    "IN_PRODUCTION":"Order is in production. Close job cards first, then cancel.",
    "READY":             "Order is ready. Close job cards first, then cancel.",
    "READY_FOR_BILLING": "Order is in the billing queue. Admin/Manager can recall it to CONFIRMED.",
    "PARTIALLY_BILLED":  "Order is partially billed. Admin/Manager can recall unbilled lines.",
    "BILLED":            "Order is billed. Admin must raise a Credit Note to cancel.",
    "DISPATCHED":   "Order is dispatched. Admin must raise a Credit Note to cancel.",
    "DELIVERED":    "Order is delivered. Admin must raise a Credit Note to cancel.",
    "CLOSED":       "Order is closed. Admin must raise a Credit Note to cancel.",
}


# ─────────────────────────────────────────────────────────────────────────────
# GUARD CLASS
# ─────────────────────────────────────────────────────────────────────────────

class OrderGuard:
    """
    Evaluates all permissions for a given order and current user.

    Usage:
        guard = OrderGuard(order)
        if guard.can("edit"):
            # show edit button
        if not guard.can("cancel"):
            guard.render_block("cancel")
    """

    def __init__(self, order: dict):
        self.order   = order
        self.status  = str(order.get("status") or "PENDING").upper()
        self._perms  = PERMISSION_MAP.get(self.status, PERMISSION_MAP.get("PENDING", {}))
        self._role   = self._get_role()
        self._cancelled = (self.status == "CANCELLED")

    def _get_role(self) -> str:
        try:
            from modules.security.roles import current_role
            return (current_role() or "viewer").lower()
        except Exception:
            return "viewer"

    def can(self, action: str) -> bool:
        """Returns True if current user can perform action on this order."""
        if self._cancelled and action != "view":
            return False
        roles = self._perms.get(action)
        if roles is None:
            return True          # no restriction
        if not roles:
            return False         # nobody
        return self._role in roles

    # ── Convenience properties ────────────────────────────────────────────────
    @property
    def can_edit(self)         -> bool: return self.can("edit")
    @property
    def can_cancel(self)       -> bool: return self.can("cancel")
    @property
    def can_recall(self)       -> bool: return self.can("recall")
    @property
    def is_billing_frozen(self) -> bool:
        return self.status.upper() in (
            "READY_FOR_BILLING", "PARTIALLY_BILLED",
            "BILLED", "DISPATCHED", "DELIVERED", "CLOSED"
        )
    @property
    def can_change_status(self)-> bool: return self.can("status_change")
    @property
    def can_edit_lines(self)   -> bool: return self.can("line_edit")
    @property
    def can_change_alloc(self) -> bool: return self.can("alloc_change")
    @property
    def can_edit_price(self)   -> bool: return self.can("price_edit")
    @property
    def is_cancelled(self)     -> bool: return self._cancelled
    @property
    def is_terminal(self)      -> bool:
        return self.status in {"CANCELLED","RETURNED","DELIVERED","CLOSED"}

    # ── UI helpers ────────────────────────────────────────────────────────────

    def render_block(self, action: str, compact: bool = False) -> None:
        """Show a locked message explaining why the action is blocked."""
        msg = BLOCK_MESSAGES.get(action, "Action not allowed at this stage.")
        if self._cancelled:
            msg = f"Order is CANCELLED — {msg}"

        if compact:
            st.caption(f"🔒 {msg}")
        else:
            st.markdown(
                f"<div style='background:#1a0a0a;border:1px solid #ef444433;"
                f"border-radius:8px;padding:10px 14px;color:#94a3b8;font-size:0.82rem'>"
                f"🔒 {msg}</div>",
                unsafe_allow_html=True,
            )

    def require(self, action: str) -> bool:
        """
        Returns True if allowed, renders block message and returns False if not.
        Use at the top of a render function to gate early.

        Example:
            guard = OrderGuard(order)
            if not guard.require("edit"):
                return
        """
        if not self.can(action):
            self.render_block(action)
            return False
        return True

    def cancel_block_reason(self) -> str:
        """Human-readable reason why cancel is blocked for this status."""
        return CANCEL_BLOCKED_MSG.get(self.status, f"Cannot cancel at status {self.status}.")

    def allowed_actions(self) -> list[str]:
        """List of all actions the current user can perform."""
        return [a for a in self._perms if self.can(a)]

    def status_badge(self) -> str:
        """HTML badge for current status — grey if cancelled."""
        colours = {
            "PENDING":       "#3b82f6", "PROVISIONAL":   "#3b82f6",
            "UNDER_REVIEW":  "#f59e0b", "CONFIRMED":     "#6366f1",
            "IN_PRODUCTION": "#8b5cf6", "READY":         "#10b981",
            "BILLED":        "#059669", "DISPATCHED":    "#0891b2",
            "DELIVERED":     "#10b981", "CLOSED":        "#334155",
            "CANCELLED":     "#334155", "RETURNED":      "#ef4444",
        }
        col = colours.get(self.status, "#64748b")
        icon = "🔒" if self._cancelled else ("❌" if self.status == "RETURNED" else "")
        return (
            f"<span style='background:{col};color:#fff;padding:2px 10px;"
            f"border-radius:12px;font-size:0.72rem;font-weight:700'>"
            f"{icon} {self.status}</span>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE UI COMPONENTS
# ─────────────────────────────────────────────────────────────────────────────

def render_cancelled_banner(order: dict) -> None:
    """
    Full-width grey banner for cancelled orders.
    Shows at the top of any order detail view.
    Displays: cancel reason, who cancelled, when, refund + CN if any.
    """
    status = str(order.get("status") or "").upper()
    if status != "CANCELLED":
        return

    order_no = str(order.get("order_no") or "")

    st.markdown(
        "<div style='background:#1a1a1a;border:2px solid #334155;"
        "border-radius:10px;padding:14px 18px;margin:8px 0'>"
        "<div style='color:#475569;font-size:1rem;font-weight:700'>🔒 Order Cancelled</div>"
        "<div style='color:#334155;font-size:0.78rem;margin-top:4px'>"
        "This order is cancelled. All editing, allocation, and status changes are permanently disabled."
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # Pull cancel reason, refund, CN from DB
    try:
        from modules.sql_adapter import run_query as _rq
        _row = (_rq(
            "SELECT cancel_reason, updated_at FROM orders WHERE order_no=%s LIMIT 1",
            (order_no,)
        ) or [{}])[0]
        if _row.get("cancel_reason"):
            st.caption(f"📋 {_row['cancel_reason']}")
    except Exception:
        pass

    try:
        from modules.sql_adapter import run_query as _rq2
        _refunds = _rq2(
            "SELECT refund_amount, refund_mode, refund_ref, refunded_by "
            "FROM order_refunds WHERE order_no=%s ORDER BY refunded_at DESC",
            (order_no,)
        ) or []
        for r in _refunds:
            st.markdown(
                f"<span style='color:#10b981;font-size:0.78rem'>"
                f"💰 Refund ₹{float(r['refund_amount']):,.2f} via {r['refund_mode']}"
                + (f" · Ref: {r['refund_ref']}" if r.get("refund_ref") else "")
                + f" · By {r.get('refunded_by','—')}</span>",
                unsafe_allow_html=True,
            )
    except Exception:
        pass

    try:
        from modules.sql_adapter import run_query as _rq3
        _cns = _rq3(
            "SELECT cn_no, cn_amount, status FROM credit_notes WHERE order_no=%s",
            (order_no,)
        ) or []
        for cn in _cns:
            _c = "#10b981" if cn["status"] == "APPROVED" else "#f59e0b"
            st.markdown(
                f"<span style='color:{_c};font-size:0.78rem'>"
                f"📄 Credit Note {cn['cn_no']} · "
                f"₹{float(cn['cn_amount']):,.2f} · {cn['status']}</span>",
                unsafe_allow_html=True,
            )
    except Exception:
        pass


def guard_page(order: dict, action: str = "edit") -> Optional[OrderGuard]:
    """
    Convenience function for top-of-page guarding.
    Shows cancelled banner if cancelled, then checks permission.
    Returns guard if allowed, None if blocked (caller should return).

    Usage:
        guard = guard_page(order, "edit")
        if guard is None:
            return
    """
    render_cancelled_banner(order)

    guard = OrderGuard(order)
    if not guard.can(action):
        guard.render_block(action)
        return None
    return guard
