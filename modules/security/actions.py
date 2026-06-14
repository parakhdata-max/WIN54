"""
modules/security/actions.py
══════════════════════════════════════════════════════════════════════════════
CANONICAL ACTION NAMES — single source of truth for every authority check.

WHY THIS FILE EXISTS
────────────────────
The mutation-boundary work (held for reassessment — Stage 4/5) will route
every sensitive operation through one authorize(action, ...) call. If each
file invents its own wording ("hold_release" vs "release_hold" vs
"order.hold.release"), the matrix becomes unauditable. This file fixes the
vocabulary NOW so the later wiring is mechanical and consistent.

THIS FILE ONLY DEFINES NAMES. It does not enforce anything. Nothing here
changes runtime behaviour — it is pure, additive infrastructure and is safe
to ship in the approved scope.

USAGE (later, when authorize() lands — NOT in this pass):
    from modules.security.actions import Action
    authorize(Action.BILLING_CHALLAN_CREATE, order=order)
"""

from __future__ import annotations


class Action:
    """Canonical action identifiers. Use these constants, never raw strings."""

    # ── Order lifecycle ──────────────────────────────────────────────────────
    ORDER_EDIT          = "order.edit"
    ORDER_HOLD_RELEASE  = "order.hold.release"
    ORDER_CANCEL        = "order.cancel"

    # ── Backoffice assignment ────────────────────────────────────────────────
    ASSIGNMENT_SAVE     = "assignment.save"

    # ── Supplier / external lab pipeline ─────────────────────────────────────
    SUPPLIER_ADVANCE    = "supplier.advance"
    SUPPLIER_ROLLBACK   = "supplier.rollback"

    # ── Procurement ──────────────────────────────────────────────────────────
    PROCUREMENT_RECEIVE = "procurement.receive"

    # ── Billing ──────────────────────────────────────────────────────────────
    BILLING_CHALLAN_CREATE = "billing.challan.create"
    BILLING_INVOICE_CREATE = "billing.invoice.create"

    # ── Administration ───────────────────────────────────────────────────────
    USER_MANAGE         = "user.manage"


# Immutable set of every valid action — used by future authorize() to reject
# typos at call time instead of silently failing open.
ALL_ACTIONS = frozenset(
    v for k, v in vars(Action).items()
    if not k.startswith("_") and isinstance(v, str)
)


def is_valid_action(action: str) -> bool:
    """True if `action` is a recognised canonical action name."""
    return action in ALL_ACTIONS
