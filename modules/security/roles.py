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
STAFF     = "staff"          # Stage 3: now a first-class role (previously
                             # silently degraded to VIEWER). No existing user
                             # has role 'staff' (UI never offered it), so this
                             # is zero-regression — it only enables correct
                             # resolution + matrix grants going forward.
VIEWER    = "viewer"

ALL_ROLES = {ADMIN, MANAGER, BILLING, LAB, INVENTORY, STAFF, VIEWER}

# Legacy / variant spellings → canonical role. Keeps the vocabulary single
# even if older rows or external callers use different casing/wording.
_ROLE_ALIASES = {
    "administrator": ADMIN,
    "mgr":           MANAGER,
    "store_staff":   STAFF,
    "counter":       STAFF,
}


def normalize_role(raw) -> str:
    """Map any incoming role value to a canonical role, else VIEWER.

    Single normalization point for a *user's own role*. Unknown → VIEWER is
    the SAFE default here (least privilege for an unrecognised principal).
    """
    if not raw:
        return VIEWER
    r = str(raw).strip().lower()
    r = _ROLE_ALIASES.get(r, r)
    return r if r in ALL_ROLES else VIEWER


def canonical_role_or_none(raw):
    """Strict resolver for values about to be PERSISTED (DB role writes).

    Returns the canonical role string, or None if `raw` is not a recognised
    role. Unlike normalize_role(), this does NOT fall back to VIEWER — the
    caller must reject unknown input rather than silently downgrade it.
    """
    if raw is None:
        return None
    r = str(raw).strip().lower()
    r = _ROLE_ALIASES.get(r, r)
    return r if r in ALL_ROLES else None


def _normalize_allowed(raw) -> str:
    """Normalise a role token in an ALLOWED list (require_role/has_role).

    Applies case + alias folding so ADMIN / STAFF / store_staff all match,
    but does NOT collapse unknown tokens to VIEWER — a mistyped constant
    must match nothing (fail-safe: deny), never accidentally admit viewers.
    """
    r = str(raw).strip().lower()
    return _ROLE_ALIASES.get(r, r)


# ── Current user ──────────────────────────────────────────────────────────────

def current_user() -> dict:
    """Returns current user dict. Always use this — never read session directly."""
    u = st.session_state.get("user", {})
    if isinstance(u, str):
        return {"name": u, "role": VIEWER, "id": None}
    return u or {}


def current_role() -> str:
    """Normalised role string. Defaults to VIEWER if missing or unknown."""
    return normalize_role(current_user().get("role"))


def current_user_name() -> str:
    """Display name for audit logging."""
    u = current_user()
    return u.get("name") or u.get("username") or "system"


def current_user_id():
    """User UUID for DB writes, or None."""
    return current_user().get("id")


def actor() -> dict:
    """Canonical principal for audit stamping AND permission checks.

    Single source: the dict written to session_state['user'] at login.
    Every audit/created_by/permission helper should derive identity from
    THIS — never read session_state directly with ad-hoc keys.

        a = actor()
        created_by = a["name"]      # real user, never "System"
        if a["role"] == ADMIN: ...
    """
    u = current_user()
    return {
        "id":   u.get("id"),
        "name": u.get("name") or u.get("username") or "system",
        "role": current_role(),
    }


# ── Access control ────────────────────────────────────────────────────────────

def require_role(*allowed_roles: str):
    """
    Block page/section if user's role is not in allowed_roles.
    Shows error and calls st.stop().

        require_role(ADMIN, MANAGER)
    """
    role    = current_role()
    allowed = {_normalize_allowed(r) for r in allowed_roles}
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
    return current_role() in {_normalize_allowed(r) for r in allowed_roles}


def is_admin()             -> bool: return current_role() == ADMIN
def is_manager_or_above()  -> bool: return current_role() in {ADMIN, MANAGER}
