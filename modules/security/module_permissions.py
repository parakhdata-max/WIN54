"""
modules/security/module_permissions.py
========================================
Module-level action permissions — what each role can DO inside a page.

Current system: roles control which PAGES are visible (sidebar).
This layer: roles control which ACTIONS are allowed within a page.

Usage:
    from modules.security.module_permissions import can, require_permission

    # Guard a button
    if can("delete_invoice"):
        if st.button("🗑️ Delete Invoice"):
            ...

    # Guard inline (raises PermissionError shown as st.error)
    require_permission("post_journal")
"""

from __future__ import annotations
import streamlit as st
from typing import Tuple


# ══════════════════════════════════════════════════════════════════════════════
# PERMISSION MATRIX
# module → action → minimum role(s) allowed
# ══════════════════════════════════════════════════════════════════════════════

# Role constants (must match security/auth.py)
ADMIN     = "ADMIN"
MANAGER   = "MANAGER"
BILLING   = "BILLING"
LAB       = "LAB"
INVENTORY = "INVENTORY"
STAFF     = "STAFF"
VIEWER    = "VIEWER"

# Hierarchy: higher index = more privilege
_ROLE_HIERARCHY = [VIEWER, STAFF, LAB, INVENTORY, BILLING, MANAGER, ADMIN]


PERMISSIONS: dict[str, list[str]] = {

    # ── Billing / Invoicing ───────────────────────────────────────────────────
    "create_invoice":       [BILLING, MANAGER, ADMIN],
    "edit_invoice":         [BILLING, MANAGER, ADMIN],
    "delete_invoice":       [MANAGER, ADMIN],
    "post_invoice":         [BILLING, MANAGER, ADMIN],
    "void_invoice":         [MANAGER, ADMIN],
    "print_invoice":        [BILLING, LAB, MANAGER, ADMIN],

    # ── Payments ─────────────────────────────────────────────────────────────
    "collect_payment":      [BILLING, MANAGER, ADMIN],
    "reverse_payment":      [MANAGER, ADMIN],
    "view_payment":         [BILLING, MANAGER, ADMIN],
    "disburse_payment":     [BILLING, MANAGER, ADMIN],

    # ── Challans ─────────────────────────────────────────────────────────────
    "create_challan":       [BILLING, MANAGER, ADMIN],
    "edit_challan":         [BILLING, MANAGER, ADMIN],
    "delete_challan":       [MANAGER, ADMIN],

    # ── Orders ───────────────────────────────────────────────────────────────
    "create_order":         [BILLING, LAB, MANAGER, ADMIN],
    "edit_order":           [BILLING, LAB, MANAGER, ADMIN],
    "cancel_order":         [MANAGER, ADMIN],
    "delete_order":         [ADMIN],
    "override_price":       [MANAGER, ADMIN],
    "apply_discount":       [BILLING, MANAGER, ADMIN],

    # ── Backoffice ───────────────────────────────────────────────────────────
    "confirm_order":        [LAB, MANAGER, ADMIN],
    "dispatch_order":       [LAB, MANAGER, ADMIN],
    "edit_job_card":        [LAB, MANAGER, ADMIN],

    # ── Stock / Inventory ─────────────────────────────────────────────────────
    "view_stock":           [BILLING, LAB, INVENTORY, MANAGER, ADMIN],
    "adjust_stock":         [INVENTORY, MANAGER, ADMIN],
    "allocate_blank":       [LAB, MANAGER, ADMIN],

    # ── Accounting ────────────────────────────────────────────────────────────
    "view_accounts":        [BILLING, MANAGER, ADMIN],
    "post_journal":         [MANAGER, ADMIN],
    "run_backfill":         [MANAGER, ADMIN],
    "view_trial_balance":   [MANAGER, ADMIN],
    "view_pl":              [MANAGER, ADMIN],
    "view_balance_sheet":   [MANAGER, ADMIN],

    # ── Admin ─────────────────────────────────────────────────────────────────
    "manage_users":         [ADMIN],
    "manage_roles":         [ADMIN],
    "view_audit_log":       [MANAGER, ADMIN],
    "delete_audit_entry":   [ADMIN],       # nobody should ever do this
    "edit_shop_settings":   [MANAGER, ADMIN],
    "view_reports":         [BILLING, MANAGER, ADMIN],
    "export_data":          [MANAGER, ADMIN],

    # ── HR ────────────────────────────────────────────────────────────────────
    "view_own_attendance":  [STAFF, LAB, BILLING, INVENTORY, MANAGER, ADMIN],
    "approve_leave":        [MANAGER, ADMIN],
    "view_payroll":         [MANAGER, ADMIN],
    "manage_employees":     [MANAGER, ADMIN],
}


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _current_role() -> str:
    """Role of the logged-in user, as the matrix expects it (UPPER).

    Stage 1 fix: previously read st.session_state['user_role'], a key that
    LOGIN NEVER SET — so this always returned VIEWER and the entire action
    matrix was inert. Now sourced from the canonical principal written at
    login. Falls back to the old behaviour only if roles.py is unavailable.
    """
    try:
        from modules.security.roles import current_role
        return current_role().upper()
    except Exception:
        try:
            return str(st.session_state.get("user_role", VIEWER)).upper()
        except Exception:
            return VIEWER


def _current_user() -> str:
    """Display name of the logged-in user, for permission-denied messages."""
    try:
        from modules.security.roles import current_user_name
        return current_user_name()
    except Exception:
        try:
            return str(st.session_state.get("user_name", "unknown"))
        except Exception:
            return "unknown"


def can(action: str, role: str = None) -> bool:
    """
    Return True if the current user (or given role) can perform action.

    Usage:
        if can("delete_invoice"):
            st.button("Delete")

        # With explicit role:
        if can("post_journal", role="MANAGER"):
            ...
    """
    role = (role or _current_role()).upper()

    # ADMIN can always do everything
    if role == ADMIN:
        return True

    allowed_roles = PERMISSIONS.get(action, [])
    return role in [r.upper() for r in allowed_roles]


def require_permission(action: str) -> None:
    """
    Stop execution if current user cannot perform action.
    Shows a clear error in the UI.

    Usage:
        require_permission("reverse_payment")
        # code below only runs if allowed
    """
    if not can(action):
        role = _current_role()
        user = _current_user()
        allowed = PERMISSIONS.get(action, [])
        st.error(
            f"⛔ **Permission denied** — `{action}`  \n"
            f"Your role **{role}** cannot perform this action.  \n"
            f"Required: {', '.join(allowed) or 'ADMIN only'}"
        )
        st.stop()


def permission_gate(
    action: str,
    label:  str = None,
    icon:   str = "🔒",
) -> bool:
    """
    Inline permission check with optional UI feedback.
    Returns True if allowed, shows warning and returns False if not.

    Usage:
        if permission_gate("delete_invoice", "Delete Invoice"):
            # show delete button
    """
    if can(action):
        return True
    if label:
        st.caption(
            f"{icon} {label} — requires "
            f"{', '.join(PERMISSIONS.get(action, ['ADMIN']))}"
        )
    return False


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS UI (Admin manages permissions)
# ══════════════════════════════════════════════════════════════════════════════

def render_permissions_matrix() -> None:
    """
    Admin UI — view the full permission matrix.
    Add to Admin → User Management page.
    """
    import pandas as pd

    st.markdown("### 🔐 Permission Matrix")
    st.caption("What each role can do in each module. ADMIN can always do everything.")

    # Build display table
    roles_display = [VIEWER, STAFF, BILLING, LAB, INVENTORY, MANAGER]
    rows = []
    for action, allowed in sorted(PERMISSIONS.items()):
        row = {"Action": action}
        for r in roles_display:
            row[r] = "✅" if r in [x.upper() for x in allowed] else "—"
        row[ADMIN] = "✅"
        rows.append(row)

    df = pd.DataFrame(rows)

    # Color coding by module
    module_filter = st.selectbox(
        "Filter by module",
        ["All", "Billing/Invoice", "Payments", "Orders", "Backoffice",
         "Accounting", "Stock", "Admin", "HR"],
        key="perm_filter"
    )

    filter_map = {
        "Billing/Invoice": ["invoice", "challan", "print"],
        "Payments":        ["payment"],
        "Orders":          ["order", "price", "discount"],
        "Backoffice":      ["confirm", "dispatch", "job"],
        "Accounting":      ["account", "journal", "backfill", "trial", "balance", "pl"],
        "Stock":           ["stock", "blank"],
        "Admin":           ["user", "role", "audit", "shop", "export"],
        "HR":              ["attendance", "leave", "payroll", "employee"],
    }

    if module_filter != "All":
        keywords = filter_map.get(module_filter, [])
        df = df[df["Action"].apply(
            lambda a: any(k in a.lower() for k in keywords)
        )]

    st.dataframe(df, width='stretch', hide_index=True)
    st.caption(f"✅ = allowed  · — = not allowed  · {len(df)} actions shown")
