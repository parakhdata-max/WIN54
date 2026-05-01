"""
modules/security/roles.py
==========================
Role constants and access control — single source of truth.

USAGE:
    from modules.security.roles import ADMIN, MANAGER, require_role, has_role

    require_role(ADMIN, MANAGER)       # blocks if not admin/manager
    if has_role(BILLING): ...          # conditional UI
    name = current_user_name()         # for audit logging
"""

import streamlit as st

# ── Role constants — always use these, never raw strings ─────────────────────
ADMIN     = "admin"
MANAGER   = "manager"
BILLING   = "billing"
LAB       = "lab"
INVENTORY = "inventory"
VIEWER    = "viewer"

ALL_ROLES = {ADMIN, MANAGER, BILLING, LAB, INVENTORY, VIEWER}


# ── Current user ──────────────────────────────────────────────────────────────

def current_user() -> dict:
    """Returns current user dict. Always use this — never read session directly."""
    u = st.session_state.get("user", {})
    if isinstance(u, str):
        return {"name": u, "role": VIEWER, "id": None}
    return u or {}


def current_role() -> str:
    """Normalised role string. Defaults to VIEWER if missing or unknown."""
    role = current_user().get("role") or VIEWER
    normalised = str(role).lower().strip()
    return normalised if normalised in ALL_ROLES else VIEWER


def current_user_name() -> str:
    """Display name for audit logging."""
    u = current_user()
    return u.get("name") or u.get("username") or "system"


def current_user_id():
    """User UUID for DB writes, or None."""
    return current_user().get("id")


# ── Access control ────────────────────────────────────────────────────────────

def require_role(*allowed_roles: str):
    """
    Block page/section if user's role is not in allowed_roles.
    Shows error and calls st.stop().

        require_role(ADMIN, MANAGER)
    """
    role    = current_role()
    allowed = {r.lower().strip() for r in allowed_roles}
    if role not in allowed:
        st.error(f"⛔ Access denied — requires: {', '.join(sorted(allowed)).upper()}")
        st.stop()


def has_role(*allowed_roles: str) -> bool:
    """
    Returns True if current user has any of the roles.
    Use for show/hide — not for blocking (use require_role for that).

        if has_role(ADMIN, MANAGER):
            st.button("Override Price")
    """
    return current_role() in {r.lower().strip() for r in allowed_roles}


def is_admin()             -> bool: return current_role() == ADMIN
def is_manager_or_above()  -> bool: return current_role() in {ADMIN, MANAGER}
