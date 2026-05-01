"""
modules/security/permission_engine.py
══════════════════════════════════════════════════════════════════════════════
Core permission engine — DB-driven, no Streamlit.

DESIGN DECISIONS (explained so future developers understand):
─────────────────────────────────────────────────────────────
Discount threshold : 20%
  Optical retail industry standard. Above 20% is unusual enough to warrant
  a manager's eye. Stored in DB so admin can change it per installation.

Price override reasons : Dropdown + optional free-text note
  Dropdown prevents vague entries ("boss said so") and makes audit reports
  useful. Free-text catches edge cases without polluting the dropdown list.

Permission resolution order (most specific wins):
  1. User-level override (user_module_grants / user_action_grants)
  2. Acting role grants (temporary elevated role)
  3. Role-level grants (role_module_grants / role_action_grants)
  4. Hardcoded fallback defaults

Acting Role:
  Any user can be given a "temporary elevated role" that expires at a
  set time. This solves "staff absent, need cover" without permanently
  changing someone's role. Checked on every permission call.

PLACEMENT: modules/core/permission_engine.py
  Pure logic — imported by backoffice, retail, billing, API.
  Streamlit UI lives in modules/security/permission_designer_ui.py
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
import datetime

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

DISCOUNT_APPROVAL_THRESHOLD = 20.0   # % — above this requires manager

PRICE_OVERRIDE_REASONS = [
    "— Select reason —",
    "Bulk order discount",
    "Loyalty / regular customer",
    "Damaged / defective item",
    "Promotional / festive offer",
    "Management instruction",
    "Rate revision / price mismatch",
    "Other (specify below)",
]

ROLES_ORDERED = ["viewer", "staff", "lab", "inventory", "billing", "manager", "admin"]
_ROLE_RANK = {r: i for i, r in enumerate(ROLES_ORDERED)}

# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR MODULE CATALOGUE  (single source of truth)
# ─────────────────────────────────────────────────────────────────────────────
# Each entry: (module_key, label, section, default_roles)
# default_roles = roles that see this module in suggested defaults
# This drives both the DB seeding AND the permission designer UI

SIDEBAR_CATALOGUE: List[Dict] = [
    # ── BILLING section ──────────────────────────────────────────────────────
    {"key": "retail_order",     "label": "🛍️  Retail Order",       "section": "BILLING",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "wholesale_order",  "label": "📦  Wholesale Order",     "section": "BILLING",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "orders",           "label": "📋  Orders",              "section": "BILLING",
     "default_roles": ["billing", "lab", "manager", "admin"]},
    {"key": "cl_advisor",       "label": "👁️  CL Advisor",          "section": "BILLING",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "collect_payment",  "label": "💳  Collect Payment",     "section": "BILLING",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "challan_invoice",  "label": "🧾  Challan & Invoice",   "section": "BILLING",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "credit_debit",     "label": "📄  Credit & Debit Notes","section": "BILLING",
     "default_roles": ["manager", "admin"]},
    {"key": "reports",          "label": "📊  Reports",             "section": "BILLING",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "registers",        "label": "📚  Registers",           "section": "BILLING",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "accounts",         "label": "📒  Accounts",            "section": "BILLING",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "hr_attendance",    "label": "👥  HR & Attendance",     "section": "BILLING",
     "default_roles": ["staff", "lab", "billing", "inventory", "manager", "admin"]},

    # ── PRODUCTION section ───────────────────────────────────────────────────
    {"key": "backoffice",       "label": "⚙️  Backoffice",          "section": "PRODUCTION",
     "default_roles": ["lab", "manager", "admin"]},
    {"key": "production",       "label": "🔬  Production",          "section": "PRODUCTION",
     "default_roles": ["lab", "manager", "admin"]},
    {"key": "rejection_report", "label": "📋  Rejection Report",    "section": "PRODUCTION",
     "default_roles": ["manager", "admin"]},

    # ── MASTERS & STOCK section ──────────────────────────────────────────────
    {"key": "quick_add",        "label": "➕  Quick Add",           "section": "STOCK",
     "default_roles": ["billing", "inventory", "manager", "admin"]},
    {"key": "scan_add_frame",   "label": "🕶️  Scan & Add Frame",    "section": "STOCK",
     "default_roles": ["inventory", "manager", "admin"]},
    {"key": "data_loader",      "label": "📥  Data Loader",         "section": "STOCK",
     "default_roles": ["manager", "admin"]},
    {"key": "frame_stock",      "label": "🕶️  Frame Stock",         "section": "STOCK",
     "default_roles": ["billing", "inventory", "manager", "admin"]},
    {"key": "inventory_search", "label": "🔍  Inventory Search",    "section": "STOCK",
     "default_roles": ["billing", "inventory", "lab", "manager", "admin"]},
    {"key": "label_preview",    "label": "🏷️  Label Preview",       "section": "STOCK",
     "default_roles": ["inventory", "manager", "admin"]},
    {"key": "scanner",          "label": "📷  Scanner",             "section": "STOCK",
     "default_roles": ["billing", "inventory", "lab", "manager", "admin"]},
    {"key": "crm",              "label": "🤝  CRM / Parties",       "section": "STOCK",
     "default_roles": ["billing", "manager", "admin"]},
    {"key": "procurement",      "label": "🛒  Procurement",         "section": "STOCK",
     "default_roles": ["inventory", "manager", "admin"]},
    {"key": "product_inventory","label": "🔬  Product & Inventory", "section": "STOCK",
     "default_roles": ["inventory", "manager", "admin"]},
    {"key": "bulk_order",       "label": "⚡  Bulk Order",          "section": "STOCK",
     "default_roles": ["inventory", "manager", "admin"]},

    # ── ADMIN section ────────────────────────────────────────────────────────
    {"key": "shop_master",      "label": "🏪  Shop Master",         "section": "ADMIN",
     "default_roles": ["manager", "admin"]},
    {"key": "pricing_admin",    "label": "💲  Pricing Admin",       "section": "ADMIN",
     "default_roles": ["manager", "admin"]},
    {"key": "edit_log",         "label": "📝  Edit Log",            "section": "ADMIN",
     "default_roles": ["manager", "admin"]},
    {"key": "user_management",  "label": "👥  User Management",     "section": "ADMIN",
     "default_roles": ["admin"]},
    {"key": "system_health",    "label": "❤️  System Health",       "section": "ADMIN",
     "default_roles": ["admin"]},
    {"key": "order_numbers",    "label": "🔢  Order Numbers",       "section": "ADMIN",
     "default_roles": ["admin"]},
    {"key": "import_analytics", "label": "📊  Import Analytics",    "section": "ADMIN",
     "default_roles": ["admin"]},
    {"key": "schema_evolution", "label": "🔧  Schema Evolution",    "section": "ADMIN",
     "default_roles": ["admin"]},
]

# ─────────────────────────────────────────────────────────────────────────────
# ACTION CATALOGUE  (what can happen inside each module)
# ─────────────────────────────────────────────────────────────────────────────

ACTION_CATALOGUE: List[Dict] = [
    # Retail Order
    {"module": "retail_order", "key": "punch_order",       "label": "Punch new order",
     "desc": "Create a new retail order",                   "default_roles": ["billing","manager","admin"]},
    {"module": "retail_order", "key": "edit_pending",      "label": "Edit PENDING order",
     "desc": "Modify a saved but not-confirmed order",      "default_roles": ["billing","manager","admin"]},
    {"module": "retail_order", "key": "apply_discount",    "label": "Apply discount (≤20%)",
     "desc": "Standard discount on order lines",           "default_roles": ["billing","manager","admin"]},
    {"module": "retail_order", "key": "discount_over_threshold", "label": "Discount >20% (needs manager)",
     "desc": "High discount — requires approval reason",   "default_roles": ["manager","admin"]},
    {"module": "retail_order", "key": "override_price",    "label": "Override price (needs reason)",
     "desc": "Change price from catalogue — logged always","default_roles": ["manager","admin"]},
    {"module": "retail_order", "key": "delete_order",      "label": "Delete / void order",
     "desc": "Hard delete — admin + pipeline check",       "default_roles": ["admin"]},
    {"module": "retail_order", "key": "print_receipt",     "label": "Print receipt",
     "desc": "",                                            "default_roles": ["billing","lab","manager","admin"]},

    # Orders page
    {"module": "orders",       "key": "view_all_orders",   "label": "View all orders",
     "desc": "",                                            "default_roles": ["billing","lab","manager","admin"]},
    {"module": "orders",       "key": "edit_confirmed",    "label": "Edit confirmed order (backoffice only)",
     "desc": "Blocked for billing — backoffice only",      "default_roles": ["lab","manager","admin"]},
    {"module": "orders",       "key": "cancel_order",      "label": "Cancel order",
     "desc": "Stage-aware cancellation",                   "default_roles": ["billing","manager","admin"]},

    # Backoffice
    {"module": "backoffice",   "key": "confirm_order",     "label": "Save/confirm order",
     "desc": "Move order to CONFIRMED — locks retail edit","default_roles": ["lab","manager","admin"]},
    {"module": "backoffice",   "key": "allot_blank",       "label": "Allot blank / enter job card",
     "desc": "",                                            "default_roles": ["lab","manager","admin"]},
    {"module": "backoffice",   "key": "backstep_stage",    "label": "Admin backstep (manager+)",
     "desc": "Move job stage backward with reason",        "default_roles": ["manager","admin"]},
    {"module": "backoffice",   "key": "override_price_bo", "label": "Override price in backoffice",
     "desc": "Change price on confirmed order — logged",   "default_roles": ["manager","admin"]},
    {"module": "backoffice",   "key": "cancel_sent_po",    "label": "Cancel SENT supplier PO",
     "desc": "Manager+ required; vendor notified",         "default_roles": ["manager","admin"]},

    # Production
    {"module": "production",   "key": "advance_stage",     "label": "Advance production stage",
     "desc": "",                                            "default_roles": ["lab","manager","admin"]},
    {"module": "production",   "key": "reject_blank",      "label": "Reject & return blank",
     "desc": "",                                            "default_roles": ["lab","manager","admin"]},

    # Collect Payment
    {"module": "collect_payment","key": "collect_payment",  "label": "Collect payment",
     "desc": "",                                            "default_roles": ["billing","manager","admin"]},
    {"module": "collect_payment","key": "reverse_payment",  "label": "Reverse payment",
     "desc": "Requires manager",                           "default_roles": ["manager","admin"]},

    # Reports
    {"module": "reports",      "key": "view_reports",      "label": "View reports",
     "desc": "",                                            "default_roles": ["billing","manager","admin"]},
    {"module": "reports",      "key": "export_data",       "label": "Export / download data",
     "desc": "",                                            "default_roles": ["manager","admin"]},

    # Accounts
    {"module": "accounts",     "key": "view_accounts",     "label": "View accounts",
     "desc": "",                                            "default_roles": ["billing","manager","admin"]},
    {"module": "accounts",     "key": "post_journal",      "label": "Post journal entry",
     "desc": "",                                            "default_roles": ["manager","admin"]},

    # Procurement
    {"module": "procurement",  "key": "create_po",         "label": "Create purchase order",
     "desc": "",                                            "default_roles": ["inventory","manager","admin"]},
    {"module": "procurement",  "key": "receive_goods",     "label": "Receive goods (mark PO received)",
     "desc": "",                                            "default_roles": ["inventory","lab","manager","admin"]},

    # Admin
    {"module": "user_management","key": "manage_users",    "label": "Create / edit users",
     "desc": "",                                            "default_roles": ["admin"]},
    {"module": "user_management","key": "grant_acting_role","label": "Grant temporary acting role",
     "desc": "Assign elevated role to cover absent staff",  "default_roles": ["manager","admin"]},
    {"module": "user_management","key": "manage_permissions","label": "Manage role permissions",
     "desc": "Change what each role can see and do",        "default_roles": ["admin"]},
]


# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _q(sql: str, params=None) -> list:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []

def _w(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# DB SCHEMA BOOTSTRAP  (auto-creates tables on first run)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_permission_tables() -> None:
    """Create permission tables if they don't exist. Call once at app startup."""
    _w("""
    CREATE TABLE IF NOT EXISTS role_module_grants (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        role        TEXT NOT NULL,
        module_key  TEXT NOT NULL,
        can_view    BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(role, module_key)
    )""")
    _w("""
    CREATE TABLE IF NOT EXISTS role_action_grants (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        role        TEXT NOT NULL,
        module_key  TEXT NOT NULL,
        action_key  TEXT NOT NULL,
        granted     BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(role, module_key, action_key)
    )""")
    _w("""
    CREATE TABLE IF NOT EXISTS user_module_grants (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id     UUID NOT NULL,
        module_key  TEXT NOT NULL,
        can_view    BOOLEAN,          -- NULL = inherit from role
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(user_id, module_key)
    )""")
    _w("""
    CREATE TABLE IF NOT EXISTS user_action_grants (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id     UUID NOT NULL,
        module_key  TEXT NOT NULL,
        action_key  TEXT NOT NULL,
        granted     BOOLEAN,          -- NULL = inherit from role
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(user_id, module_key, action_key)
    )""")
    _w("""
    CREATE TABLE IF NOT EXISTS user_acting_roles (
        id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id        UUID NOT NULL,
        acting_role    TEXT NOT NULL,
        granted_by     UUID,
        granted_reason TEXT,
        expires_at     TIMESTAMPTZ NOT NULL,
        is_active      BOOLEAN NOT NULL DEFAULT TRUE,
        created_at     TIMESTAMPTZ DEFAULT NOW()
    )""")
    _w("""
    CREATE TABLE IF NOT EXISTS permission_override_log (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id       UUID,
        username      TEXT,
        action_type   TEXT NOT NULL,   -- 'price_override' | 'discount_approval' | 'po_cancel' etc
        order_no      TEXT,
        original_val  TEXT,
        new_val       TEXT,
        reason        TEXT NOT NULL,
        reason_note   TEXT,
        approved_by   TEXT,
        created_at    TIMESTAMPTZ DEFAULT NOW()
    )""")
    _w("""
    CREATE TABLE IF NOT EXISTS permission_settings (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_by  TEXT,
        updated_at  TIMESTAMPTZ DEFAULT NOW()
    )""")
    # Seed default discount threshold if not set
    _w("""
    INSERT INTO permission_settings (key, value, updated_by)
    VALUES ('discount_approval_threshold', '20', 'system')
    ON CONFLICT (key) DO NOTHING
    """)


# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

def get_discount_threshold() -> float:
    """Returns the discount % above which manager approval is required."""
    rows = _q("SELECT value FROM permission_settings WHERE key='discount_approval_threshold'")
    try:
        return float(rows[0]["value"]) if rows else DISCOUNT_APPROVAL_THRESHOLD
    except Exception:
        return DISCOUNT_APPROVAL_THRESHOLD


def set_discount_threshold(pct: float, by: str = "admin") -> None:
    _w("""
    INSERT INTO permission_settings (key, value, updated_by, updated_at)
    VALUES ('discount_approval_threshold', %(v)s, %(by)s, NOW())
    ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by,
                                    updated_at=NOW()
    """, {"v": str(pct), "by": by})


# ─────────────────────────────────────────────────────────────────────────────
# ACTING ROLE  (temporary elevation)
# ─────────────────────────────────────────────────────────────────────────────

def get_acting_role(user_id: str) -> Optional[str]:
    """
    Returns the active acting role for a user if one is valid right now,
    else None. Checks both is_active and expires_at.
    """
    if not user_id:
        return None
    rows = _q("""
        SELECT acting_role FROM user_acting_roles
        WHERE user_id = %(u)s::uuid
          AND is_active = TRUE
          AND expires_at > NOW()
        ORDER BY expires_at DESC LIMIT 1
    """, {"u": user_id})
    return rows[0]["acting_role"].lower() if rows else None


def grant_acting_role(user_id: str, acting_role: str, granted_by_id: str,
                      reason: str, expires_at: datetime.datetime) -> bool:
    """Grant a temporary elevated role to a user."""
    # Deactivate any existing acting role first
    _w("""
    UPDATE user_acting_roles SET is_active = FALSE
    WHERE user_id = %(u)s::uuid AND is_active = TRUE
    """, {"u": user_id})
    return _w("""
    INSERT INTO user_acting_roles
        (user_id, acting_role, granted_by, granted_reason, expires_at, is_active)
    VALUES
        (%(u)s::uuid, %(r)s, %(g)s::uuid, %(reason)s, %(exp)s, TRUE)
    """, {"u": user_id, "r": acting_role,
          "g": granted_by_id, "reason": reason, "exp": expires_at})


def revoke_acting_role(user_id: str) -> bool:
    return _w("""
    UPDATE user_acting_roles SET is_active = FALSE
    WHERE user_id = %(u)s::uuid AND is_active = TRUE
    """, {"u": user_id})


def _effective_role(user_id: str, base_role: str) -> str:
    """
    Returns the higher of base_role and any active acting_role.
    This is what permission checks should use.
    """
    acting = get_acting_role(user_id)
    if not acting:
        return base_role
    # Return whichever role has higher rank
    base_rank   = _ROLE_RANK.get(base_role.lower(),  0)
    acting_rank = _ROLE_RANK.get(acting.lower(), 0)
    return acting if acting_rank > base_rank else base_role


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR VISIBILITY
# ─────────────────────────────────────────────────────────────────────────────

def get_visible_modules(user_id: str, base_role: str) -> Set[str]:
    """
    Returns set of module_keys visible to this user.
    Resolution order: user override → acting role → base role → fallback defaults.
    """
    role = _effective_role(user_id, base_role)

    # Role-level grants from DB
    role_rows = _q("""
        SELECT module_key, can_view FROM role_module_grants WHERE role = %(r)s
    """, {"r": role})
    role_grants = {r["module_key"]: bool(r["can_view"]) for r in role_rows}

    # User-level overrides
    user_rows = _q("""
        SELECT module_key, can_view FROM user_module_grants WHERE user_id = %(u)s::uuid
    """, {"u": user_id}) if user_id else []
    user_grants = {r["module_key"]: r["can_view"] for r in user_rows if r["can_view"] is not None}

    visible = set()
    for mod in SIDEBAR_CATALOGUE:
        key = mod["key"]
        # User override takes precedence
        if key in user_grants:
            if user_grants[key]:
                visible.add(key)
            # False = explicitly hidden for this user
        elif key in role_grants:
            if role_grants[key]:
                visible.add(key)
        else:
            # Fallback: use catalogue defaults
            if role in mod.get("default_roles", []):
                visible.add(key)

    return visible


# ─────────────────────────────────────────────────────────────────────────────
# ACTION PERMISSIONS
# ─────────────────────────────────────────────────────────────────────────────

def can_do(action_key: str, user_id: str, base_role: str,
           module_key: str = "") -> bool:
    """
    Returns True if this user can perform action_key.
    Admin can always do everything (except super_admin-only actions).
    """
    role = _effective_role(user_id, base_role)

    if role == "admin":
        return True

    # Resolve module_key from catalogue if not provided
    if not module_key:
        for a in ACTION_CATALOGUE:
            if a["key"] == action_key:
                module_key = a["module"]
                break

    # User-level override
    if user_id:
        user_rows = _q("""
            SELECT granted FROM user_action_grants
            WHERE user_id=%(u)s::uuid AND module_key=%(m)s AND action_key=%(a)s
            LIMIT 1
        """, {"u": user_id, "m": module_key, "a": action_key})
        if user_rows and user_rows[0]["granted"] is not None:
            return bool(user_rows[0]["granted"])

    # Role-level grant
    role_rows = _q("""
        SELECT granted FROM role_action_grants
        WHERE role=%(r)s AND module_key=%(m)s AND action_key=%(a)s
        LIMIT 1
    """, {"r": role, "m": module_key, "a": action_key})
    if role_rows:
        return bool(role_rows[0]["granted"])

    # Fallback: catalogue defaults
    for a in ACTION_CATALOGUE:
        if a["key"] == action_key:
            return role in a.get("default_roles", [])

    return False


# ─────────────────────────────────────────────────────────────────────────────
# SEED DEFAULTS  (call once to populate DB from catalogue)
# ─────────────────────────────────────────────────────────────────────────────

def seed_defaults_for_role(role: str) -> None:
    """Populate role_module_grants and role_action_grants from catalogue defaults."""
    for mod in SIDEBAR_CATALOGUE:
        can_view = role in mod.get("default_roles", []) or role == "admin"
        _w("""
        INSERT INTO role_module_grants (role, module_key, can_view)
        VALUES (%(r)s, %(m)s, %(v)s)
        ON CONFLICT (role, module_key) DO NOTHING
        """, {"r": role, "m": mod["key"], "v": can_view})

    for act in ACTION_CATALOGUE:
        granted = role in act.get("default_roles", []) or role == "admin"
        _w("""
        INSERT INTO role_action_grants (role, module_key, action_key, granted)
        VALUES (%(r)s, %(m)s, %(a)s, %(g)s)
        ON CONFLICT (role, module_key, action_key) DO NOTHING
        """, {"r": role, "m": act["module"], "a": act["key"], "g": granted})


def seed_all_defaults() -> None:
    """Seed defaults for all known roles. Safe to run multiple times (ON CONFLICT DO NOTHING)."""
    for role in ROLES_ORDERED:
        seed_defaults_for_role(role)


# ─────────────────────────────────────────────────────────────────────────────
# PERMISSION OVERRIDE LOG
# ─────────────────────────────────────────────────────────────────────────────

def log_override(action_type: str, order_no: str, original_val: str,
                 new_val: str, reason: str, reason_note: str = "",
                 approved_by: str = "") -> None:
    """
    Log any permission-sensitive action: price override, high discount,
    PO cancellation, acting role grant, backstep, etc.
    Call this from the UI layer whenever a guarded action is confirmed.
    """
    try:
        from modules.security.roles import current_user_name, current_user_id
        uid  = current_user_id()
        uname = current_user_name()
    except Exception:
        uid, uname = None, "unknown"

    _w("""
    INSERT INTO permission_override_log
        (user_id, username, action_type, order_no, original_val,
         new_val, reason, reason_note, approved_by, created_at)
    VALUES
        (%(u)s, %(un)s, %(at)s, %(on)s, %(ov)s,
         %(nv)s, %(r)s, %(rn)s, %(ab)s, NOW())
    """, {
        "u":  str(uid)  if uid  else None,
        "un": uname,
        "at": action_type,
        "on": order_no  or "",
        "ov": original_val or "",
        "nv": new_val   or "",
        "r":  reason,
        "rn": reason_note or "",
        "ab": approved_by or "",
    })


# ─────────────────────────────────────────────────────────────────────────────
# SAVE / LOAD ROLE PERMISSIONS  (used by permission designer UI)
# ─────────────────────────────────────────────────────────────────────────────

def save_role_module_grants(role: str, grants: Dict[str, bool]) -> None:
    """
    grants = {module_key: True/False}
    Upsert all module grants for a role in one call.
    """
    for module_key, can_view in grants.items():
        _w("""
        INSERT INTO role_module_grants (role, module_key, can_view, updated_at)
        VALUES (%(r)s, %(m)s, %(v)s, NOW())
        ON CONFLICT (role, module_key) DO UPDATE
            SET can_view = EXCLUDED.can_view, updated_at = NOW()
        """, {"r": role, "m": module_key, "v": can_view})


def save_role_action_grants(role: str, grants: Dict[str, Dict[str, bool]]) -> None:
    """
    grants = {module_key: {action_key: True/False}}
    """
    for module_key, actions in grants.items():
        for action_key, granted in actions.items():
            _w("""
            INSERT INTO role_action_grants (role, module_key, action_key, granted, updated_at)
            VALUES (%(r)s, %(m)s, %(a)s, %(g)s, NOW())
            ON CONFLICT (role, module_key, action_key) DO UPDATE
                SET granted = EXCLUDED.granted, updated_at = NOW()
            """, {"r": role, "m": module_key, "a": action_key, "g": granted})


def save_user_module_grants(user_id: str, grants: Dict[str, Optional[bool]]) -> None:
    """None = inherit from role, True/False = override."""
    for module_key, can_view in grants.items():
        _w("""
        INSERT INTO user_module_grants (user_id, module_key, can_view, updated_at)
        VALUES (%(u)s::uuid, %(m)s, %(v)s, NOW())
        ON CONFLICT (user_id, module_key) DO UPDATE
            SET can_view = EXCLUDED.can_view, updated_at = NOW()
        """, {"u": user_id, "m": module_key, "v": can_view})


def save_user_action_grants(user_id: str,
                             grants: Dict[str, Dict[str, Optional[bool]]]) -> None:
    for module_key, actions in grants.items():
        for action_key, granted in actions.items():
            _w("""
            INSERT INTO user_action_grants
                (user_id, module_key, action_key, granted, updated_at)
            VALUES (%(u)s::uuid, %(m)s, %(a)s, %(g)s, NOW())
            ON CONFLICT (user_id, module_key, action_key) DO UPDATE
                SET granted = EXCLUDED.granted, updated_at = NOW()
            """, {"u": user_id, "m": module_key, "a": action_key, "g": granted})


def load_role_module_grants(role: str) -> Dict[str, bool]:
    rows = _q("SELECT module_key, can_view FROM role_module_grants WHERE role=%(r)s",
              {"r": role})
    return {r["module_key"]: bool(r["can_view"]) for r in rows}


def load_role_action_grants(role: str) -> Dict[str, Dict[str, bool]]:
    rows = _q("""SELECT module_key, action_key, granted
                 FROM role_action_grants WHERE role=%(r)s""", {"r": role})
    result: Dict[str, Dict[str, bool]] = {}
    for r in rows:
        result.setdefault(r["module_key"], {})[r["action_key"]] = bool(r["granted"])
    return result


def get_active_acting_roles() -> list:
    """Returns list of all currently active acting roles for display."""
    return _q("""
        SELECT uar.id, u.display_name AS user_name, u.username,
               uar.acting_role, uar.granted_reason, uar.expires_at,
               g.display_name AS granted_by_name
        FROM user_acting_roles uar
        JOIN erp_users u ON u.id = uar.user_id
        LEFT JOIN erp_users g ON g.id = uar.granted_by
        WHERE uar.is_active = TRUE AND uar.expires_at > NOW()
        ORDER BY uar.expires_at
    """)


__all__ = [
    "SIDEBAR_CATALOGUE", "ACTION_CATALOGUE", "ROLES_ORDERED",
    "PRICE_OVERRIDE_REASONS", "DISCOUNT_APPROVAL_THRESHOLD",
    "ensure_permission_tables", "seed_all_defaults", "seed_defaults_for_role",
    "get_visible_modules", "can_do",
    "get_acting_role", "grant_acting_role", "revoke_acting_role",
    "get_discount_threshold", "set_discount_threshold",
    "log_override",
    "save_role_module_grants", "save_role_action_grants",
    "save_user_module_grants", "save_user_action_grants",
    "load_role_module_grants", "load_role_action_grants",
    "get_active_acting_roles",
]
