"""
modules/security/permission_engine.py
Pure logic — no Streamlit. DB-driven permission catalogue.
SIDEBAR_CATALOGUE and section actions load dynamically from page_registry.py.
To add a page or sub-tab, edit page_registry.py only.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
import datetime

DISCOUNT_APPROVAL_THRESHOLD = 20.0

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
_ROLE_RANK    = {r: i for i, r in enumerate(ROLES_ORDERED)}

ROLE_COLORS = {
    "viewer":    "#6b7280",
    "staff":     "#78716c",
    "billing":   "#059669",
    "lab":       "#d97706",
    "inventory": "#dc2626",
    "manager":   "#0284c7",
    "admin":     "#7c3aed",
}

# ── Load sidebar catalogue dynamically from page_registry ────────────────
try:
    from modules.security.page_registry import get_sidebar_catalogue, get_all_section_actions
    SIDEBAR_CATALOGUE: List[Dict] = get_sidebar_catalogue()
    _SECTION_ACTIONS: List[Dict]  = get_all_section_actions()
except Exception:
    # Fallback: empty — system still works, permission designer shows no pages
    SIDEBAR_CATALOGUE = []
    _SECTION_ACTIONS  = []

ACTION_CATALOGUE: List[Dict] = [
    {"module": "retail_order",  "key": "punch_order",              "label": "Punch new order",                  "default_roles": ["billing","manager","admin"]},
    {"module": "retail_order",  "key": "edit_pending",             "label": "Edit PENDING order",               "default_roles": ["billing","manager","admin"]},
    {"module": "retail_order",  "key": "apply_discount",           "label": "Apply discount (≤20%)",            "default_roles": ["billing","manager","admin"]},
    {"module": "retail_order",  "key": "discount_over_threshold",  "label": "Discount >20% (manager approval)", "default_roles": ["manager","admin"]},
    {"module": "retail_order",  "key": "override_price",           "label": "Override price (reason required)",  "default_roles": ["manager","admin"]},
    {"module": "retail_order",  "key": "delete_order",             "label": "Delete / void order",              "default_roles": ["admin"]},
    {"module": "retail_order",  "key": "print_receipt",            "label": "Print receipt",                    "default_roles": ["billing","lab","manager","admin"]},
    {"module": "orders",        "key": "view_all_orders",          "label": "View all orders",                  "default_roles": ["billing","lab","manager","admin"]},
    {"module": "orders",        "key": "edit_confirmed",           "label": "Edit confirmed order (BO only)",   "default_roles": ["lab","manager","admin"]},
    {"module": "orders",        "key": "cancel_order",             "label": "Cancel order",                     "default_roles": ["billing","manager","admin"]},
    {"module": "backoffice",    "key": "confirm_order",            "label": "Save / confirm order",             "default_roles": ["lab","manager","admin"]},
    {"module": "backoffice",    "key": "allot_blank",              "label": "Allot blank / enter job card",     "default_roles": ["lab","manager","admin"]},
    {"module": "backoffice",    "key": "backstep_stage",           "label": "Admin backstep production stage",  "default_roles": ["manager","admin"]},
    {"module": "backoffice",    "key": "override_price_bo",        "label": "Override price in backoffice",     "default_roles": ["manager","admin"]},
    {"module": "backoffice",    "key": "cancel_sent_po",           "label": "Cancel SENT supplier PO",          "default_roles": ["manager","admin"]},
    {"module": "production",    "key": "advance_stage",            "label": "Advance production stage",         "default_roles": ["lab","manager","admin"]},
    {"module": "production",    "key": "reject_blank",             "label": "Reject & return blank",            "default_roles": ["lab","manager","admin"]},
    {"module": "collect_payment","key": "collect_payment",         "label": "Collect payment",                  "default_roles": ["billing","manager","admin"]},
    {"module": "collect_payment","key": "reverse_payment",         "label": "Reverse payment",                  "default_roles": ["manager","admin"]},
    {"module": "reports",       "key": "view_reports",             "label": "View reports",                     "default_roles": ["billing","manager","admin"]},
    {"module": "reports",       "key": "export_data",              "label": "Export / download data",           "default_roles": ["manager","admin"]},
    {"module": "accounts",      "key": "view_accounts",            "label": "View accounts",                    "default_roles": ["billing","manager","admin"]},
    {"module": "accounts",      "key": "post_journal",             "label": "Post journal entry",               "default_roles": ["manager","admin"]},
    {"module": "procurement",   "key": "create_po",                "label": "Create purchase order",            "default_roles": ["inventory","manager","admin"]},
    {"module": "procurement",   "key": "receive_goods",            "label": "Receive goods (mark PO received)", "default_roles": ["inventory","lab","manager","admin"]},
    {"module": "user_management","key": "manage_users",            "label": "Create / edit users",              "default_roles": ["admin"]},
    {"module": "user_management","key": "grant_acting_role",       "label": "Grant temporary acting role",      "default_roles": ["manager","admin"]},
    {"module": "user_management","key": "manage_permissions",      "label": "Manage role permissions",          "default_roles": ["admin"]},

    # ── Wholesale Order ───────────────────────────────────────────────────
    {"module": "wholesale_order","key": "punch_wholesale",         "label": "Punch wholesale order",            "default_roles": ["billing","manager","admin"]},
    {"module": "wholesale_order","key": "apply_discount",          "label": "Apply discount",                   "default_roles": ["billing","manager","admin"]},
    {"module": "wholesale_order","key": "override_price",          "label": "Override price (reason required)", "default_roles": ["manager","admin"]},
    {"module": "wholesale_order","key": "view_party_ledger",       "label": "View party ledger / balance",      "default_roles": ["billing","manager","admin"]},

    # ── Challan & Invoice ─────────────────────────────────────────────────
    {"module": "challan_invoice","key": "create_challan",          "label": "Create challan",                   "default_roles": ["billing","manager","admin"]},
    {"module": "challan_invoice","key": "create_invoice",          "label": "Create invoice",                   "default_roles": ["billing","manager","admin"]},
    {"module": "challan_invoice","key": "edit_invoice",            "label": "Edit invoice",                     "default_roles": ["billing","manager","admin"]},
    {"module": "challan_invoice","key": "delete_invoice",          "label": "Delete / void invoice",            "default_roles": ["manager","admin"]},
    {"module": "challan_invoice","key": "print_invoice",           "label": "Print invoice",                    "default_roles": ["billing","lab","manager","admin"]},

    # ── Credit & Debit Notes ──────────────────────────────────────────────
    {"module": "credit_debit",  "key": "create_credit_note",      "label": "Create credit note",               "default_roles": ["manager","admin"]},
    {"module": "credit_debit",  "key": "create_debit_note",       "label": "Create debit note",                "default_roles": ["manager","admin"]},
    {"module": "credit_debit",  "key": "void_note",               "label": "Void credit/debit note",           "default_roles": ["admin"]},

    # ── CL Advisor ────────────────────────────────────────────────────────
    {"module": "cl_advisor",    "key": "view_cl_records",          "label": "View CL patient records",          "default_roles": ["billing","manager","admin"]},
    {"module": "cl_advisor",    "key": "add_cl_record",            "label": "Add / update CL record",           "default_roles": ["billing","manager","admin"]},

    # ── Registers ─────────────────────────────────────────────────────────
    {"module": "registers",     "key": "view_registers",           "label": "View registers",                   "default_roles": ["billing","manager","admin"]},
    {"module": "registers",     "key": "export_register",          "label": "Export register data",             "default_roles": ["manager","admin"]},

    # ── HR & Attendance ───────────────────────────────────────────────────
    {"module": "hr_attendance", "key": "view_own_attendance",      "label": "View own attendance",              "default_roles": ["staff","lab","billing","inventory","manager","admin"]},
    {"module": "hr_attendance", "key": "mark_attendance",          "label": "Mark attendance",                  "default_roles": ["staff","lab","billing","inventory","manager","admin"]},
    {"module": "hr_attendance", "key": "view_all_attendance",      "label": "View all staff attendance",        "default_roles": ["manager","admin"]},
    {"module": "hr_attendance", "key": "approve_leave",            "label": "Approve leave requests",           "default_roles": ["manager","admin"]},
    {"module": "hr_attendance", "key": "manage_payroll",           "label": "View / manage payroll",            "default_roles": ["manager","admin"]},

    # ── Rejection Report ──────────────────────────────────────────────────
    {"module": "rejection_report","key": "view_rejections",        "label": "View rejection reports",           "default_roles": ["lab","manager","admin"]},
    {"module": "rejection_report","key": "export_rejections",      "label": "Export rejection data",            "default_roles": ["manager","admin"]},

    # ── Quick Add ─────────────────────────────────────────────────────────
    {"module": "quick_add",     "key": "add_product",              "label": "Add product to catalogue",         "default_roles": ["inventory","manager","admin"]},
    {"module": "quick_add",     "key": "add_frame",                "label": "Add frame to stock",               "default_roles": ["billing","inventory","manager","admin"]},

    # ── Scan & Add Frame ──────────────────────────────────────────────────
    {"module": "scan_add_frame","key": "scan_barcode",             "label": "Scan barcode to add frame",        "default_roles": ["inventory","manager","admin"]},
    {"module": "scan_add_frame","key": "edit_frame_details",       "label": "Edit frame details after scan",    "default_roles": ["inventory","manager","admin"]},

    # ── Data Loader ───────────────────────────────────────────────────────
    {"module": "data_loader",   "key": "import_data",              "label": "Import / upload data files",       "default_roles": ["manager","admin"]},
    {"module": "data_loader",   "key": "run_migration",            "label": "Run DB migration",                 "default_roles": ["admin"]},

    # ── Frame Stock ───────────────────────────────────────────────────────
    {"module": "frame_stock",   "key": "view_frame_stock",         "label": "View frame stock levels",          "default_roles": ["billing","inventory","manager","admin"]},
    {"module": "frame_stock",   "key": "adjust_frame_stock",       "label": "Adjust / correct stock",           "default_roles": ["inventory","manager","admin"]},
    {"module": "frame_stock",   "key": "transfer_frame",           "label": "Transfer between locations",       "default_roles": ["inventory","manager","admin"]},

    # ── Inventory Search ──────────────────────────────────────────────────
    {"module": "inventory_search","key": "search_stock",           "label": "Search inventory",                 "default_roles": ["billing","lab","inventory","manager","admin"]},
    {"module": "inventory_search","key": "view_stock_value",       "label": "View stock valuation",             "default_roles": ["manager","admin"]},

    # ── Label Preview ─────────────────────────────────────────────────────
    {"module": "label_preview", "key": "preview_label",            "label": "Preview labels",                   "default_roles": ["billing","inventory","manager","admin"]},
    {"module": "label_preview", "key": "print_label",              "label": "Print / download labels",          "default_roles": ["inventory","manager","admin"]},

    # ── Scanner ───────────────────────────────────────────────────────────
    {"module": "scanner",       "key": "scan_item",                "label": "Scan item barcode",                "default_roles": ["billing","lab","inventory","manager","admin"]},

    # ── CRM / Parties ─────────────────────────────────────────────────────
    {"module": "crm",           "key": "view_parties",             "label": "View party / customer list",       "default_roles": ["billing","manager","admin"]},
    {"module": "crm",           "key": "add_party",                "label": "Add new party / customer",         "default_roles": ["billing","manager","admin"]},
    {"module": "crm",           "key": "edit_party",               "label": "Edit party details",               "default_roles": ["billing","manager","admin"]},
    {"module": "crm",           "key": "view_party_history",       "label": "View order & payment history",     "default_roles": ["billing","manager","admin"]},

    # ── Product & Inventory ───────────────────────────────────────────────
    {"module": "product_inventory","key": "add_product_master",    "label": "Add product to master",            "default_roles": ["inventory","manager","admin"]},
    {"module": "product_inventory","key": "edit_product_master",   "label": "Edit product details / pricing",   "default_roles": ["inventory","manager","admin"]},
    {"module": "product_inventory","key": "deactivate_product",    "label": "Deactivate / archive product",     "default_roles": ["manager","admin"]},
    {"module": "product_inventory","key": "adjust_stock",          "label": "Stock adjustment (add/remove)",    "default_roles": ["inventory","manager","admin"]},

    # ── Bulk Order ────────────────────────────────────────────────────────
    {"module": "bulk_order",    "key": "create_bulk_order",        "label": "Create bulk order",                "default_roles": ["inventory","manager","admin"]},
    {"module": "bulk_order",    "key": "approve_bulk_order",       "label": "Approve bulk order",               "default_roles": ["manager","admin"]},

    # ── Shop Master ───────────────────────────────────────────────────────
    {"module": "shop_master",   "key": "edit_shop_details",        "label": "Edit shop name / address / GST",   "default_roles": ["manager","admin"]},
    {"module": "shop_master",   "key": "edit_bank_details",        "label": "Edit bank / UPI details",          "default_roles": ["manager","admin"]},

    # ── Pricing Admin ─────────────────────────────────────────────────────
    {"module": "pricing_admin", "key": "view_pricing",             "label": "View pricing rules",               "default_roles": ["manager","admin"]},
    {"module": "pricing_admin", "key": "edit_pricing",             "label": "Edit / update pricing rules",      "default_roles": ["manager","admin"]},
    {"module": "pricing_admin", "key": "override_mrp",             "label": "Override MRP",                     "default_roles": ["admin"]},

    # ── Edit Log ──────────────────────────────────────────────────────────
    {"module": "edit_log",      "key": "view_edit_log",            "label": "View edit log",                    "default_roles": ["manager","admin"]},
    {"module": "edit_log",      "key": "export_edit_log",          "label": "Export edit log",                  "default_roles": ["admin"]},

    # ── System Health ─────────────────────────────────────────────────────
    {"module": "system_health", "key": "view_health",              "label": "View system health dashboard",     "default_roles": ["admin"]},
    {"module": "system_health", "key": "run_diagnostics",          "label": "Run diagnostics / checks",         "default_roles": ["admin"]},

    # ── Order Numbers ─────────────────────────────────────────────────────
    {"module": "order_numbers", "key": "view_order_numbers",       "label": "View order number series",         "default_roles": ["manager","admin"]},
    {"module": "order_numbers", "key": "reset_order_series",       "label": "Reset / configure order series",   "default_roles": ["admin"]},

    # ── Import Analytics ──────────────────────────────────────────────────
    {"module": "import_analytics","key": "view_analytics",         "label": "View import analytics",            "default_roles": ["admin"]},
    {"module": "import_analytics","key": "run_import",             "label": "Run / trigger import",             "default_roles": ["admin"]},

    # ── Schema Evolution ──────────────────────────────────────────────────
    {"module": "schema_evolution","key": "view_schema",            "label": "View schema evolution",            "default_roles": ["admin"]},
    {"module": "schema_evolution","key": "run_migration_schema",   "label": "Apply schema migration",           "default_roles": ["admin"]},
]

# ── Append section/sub-tab actions from page_registry (dynamic) ──────────
# These are all the "Tab: X" entries. Loaded from page_registry.SUB_TABS.
# Existing entries in ACTION_CATALOGUE take priority — _SECTION_ACTIONS
# only adds keys that aren't already present (no duplicates).
_existing_keys = {(a["module"], a["key"]) for a in ACTION_CATALOGUE}
for _sa in _SECTION_ACTIONS:
    if (_sa["module"], _sa["key"]) not in _existing_keys:
        ACTION_CATALOGUE.append(_sa)
        _existing_keys.add((_sa["module"], _sa["key"]))


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []

def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception:
        return False


def get_discount_threshold() -> float:
    rows = _q("SELECT value FROM permission_settings WHERE key='discount_approval_threshold'")
    try:
        return float(rows[0]["value"]) if rows else DISCOUNT_APPROVAL_THRESHOLD
    except Exception:
        return DISCOUNT_APPROVAL_THRESHOLD

def set_discount_threshold(pct: float, by: str = "admin") -> None:
    _w("""INSERT INTO permission_settings (key,value,updated_by,updated_at)
        VALUES ('discount_approval_threshold',%(v)s,%(b)s,NOW())
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,
        updated_by=EXCLUDED.updated_by,updated_at=NOW()""",
       {"v": str(pct), "b": by})

def get_acting_role(user_id: str) -> Optional[str]:
    if not user_id:
        return None
    rows = _q("""SELECT acting_role FROM user_acting_roles
                 WHERE user_id=%(u)s::uuid AND is_active=TRUE AND expires_at>NOW()
                 ORDER BY expires_at DESC LIMIT 1""", {"u": user_id})
    return rows[0]["acting_role"].lower() if rows else None

def _effective_role(user_id: str, base_role: str) -> str:
    acting = get_acting_role(user_id)
    if not acting:
        return base_role
    return acting if _ROLE_RANK.get(acting.lower(),0) > _ROLE_RANK.get(base_role.lower(),0) else base_role

def grant_acting_role(user_id, acting_role, granted_by_id, reason, expires_at) -> bool:
    _w("UPDATE user_acting_roles SET is_active=FALSE WHERE user_id=%(u)s::uuid AND is_active=TRUE",
       {"u": user_id})
    return _w("""INSERT INTO user_acting_roles
                 (user_id,acting_role,granted_by,granted_reason,expires_at,is_active)
                 VALUES(%(u)s::uuid,%(r)s,%(g)s::uuid,%(reason)s,%(exp)s,TRUE)""",
              {"u": user_id, "r": acting_role, "g": granted_by_id,
               "reason": reason, "exp": expires_at})

def revoke_acting_role(user_id: str) -> bool:
    return _w("UPDATE user_acting_roles SET is_active=FALSE WHERE user_id=%(u)s::uuid AND is_active=TRUE",
              {"u": user_id})

def get_active_acting_roles() -> list:
    return _q("""SELECT uar.id, u.display_name AS user_name, u.username,
                        uar.acting_role, uar.granted_reason, uar.expires_at,
                        g.display_name AS granted_by_name
                 FROM user_acting_roles uar
                 JOIN erp_users u ON u.id=uar.user_id
                 LEFT JOIN erp_users g ON g.id=uar.granted_by
                 WHERE uar.is_active=TRUE AND uar.expires_at>NOW()
                 ORDER BY uar.expires_at""")

def get_visible_modules(user_id: str, base_role: str) -> Set[str]:
    role = _effective_role(user_id, base_role)
    role_rows = _q("SELECT module_key,can_view FROM role_module_grants WHERE role=%(r)s", {"r": role})
    role_grants = {r["module_key"]: bool(r["can_view"]) for r in role_rows}
    user_rows = _q("SELECT module_key,can_view FROM user_module_grants WHERE user_id=%(u)s::uuid",
                   {"u": user_id}) if user_id else []
    user_grants = {r["module_key"]: r["can_view"] for r in user_rows if r["can_view"] is not None}
    visible = set()
    for mod in SIDEBAR_CATALOGUE:
        key = mod["key"]
        if key in user_grants:
            if user_grants[key]: visible.add(key)
        elif key in role_grants:
            if role_grants[key]: visible.add(key)
        elif role in mod.get("default_roles", []) or role == "admin":
            visible.add(key)
    return visible

def can_do(action_key: str, user_id: str, base_role: str, module_key: str = "") -> bool:
    role = _effective_role(user_id, base_role)
    if role == "admin": return True
    if not module_key:
        for a in ACTION_CATALOGUE:
            if a["key"] == action_key:
                module_key = a["module"]; break
    if user_id:
        ur = _q("SELECT granted FROM user_action_grants WHERE user_id=%(u)s::uuid AND module_key=%(m)s AND action_key=%(a)s LIMIT 1",
                {"u": user_id, "m": module_key, "a": action_key})
        if ur and ur[0]["granted"] is not None: return bool(ur[0]["granted"])
    rr = _q("SELECT granted FROM role_action_grants WHERE role=%(r)s AND module_key=%(m)s AND action_key=%(a)s LIMIT 1",
            {"r": role, "m": module_key, "a": action_key})
    if rr: return bool(rr[0]["granted"])
    for a in ACTION_CATALOGUE:
        if a["key"] == action_key: return role in a.get("default_roles", [])
    return False

def load_role_module_grants(role: str) -> Dict[str, bool]:
    rows = _q("SELECT module_key,can_view FROM role_module_grants WHERE role=%(r)s", {"r": role})
    return {r["module_key"]: bool(r["can_view"]) for r in rows}

def load_role_action_grants(role: str) -> Dict[str, Dict[str, bool]]:
    rows = _q("SELECT module_key,action_key,granted FROM role_action_grants WHERE role=%(r)s", {"r": role})
    result: Dict[str, Dict[str, bool]] = {}
    for r in rows:
        result.setdefault(r["module_key"], {})[r["action_key"]] = bool(r["granted"])
    return result

def save_role_module_grants(role: str, grants: Dict[str, bool]) -> None:
    for mk, cv in grants.items():
        _w("""INSERT INTO role_module_grants(role,module_key,can_view,updated_at)
              VALUES(%(r)s,%(m)s,%(v)s,NOW())
              ON CONFLICT(role,module_key) DO UPDATE SET can_view=EXCLUDED.can_view,updated_at=NOW()""",
           {"r": role, "m": mk, "v": cv})

def save_role_action_grants(role: str, grants: Dict[str, Dict[str, bool]]) -> None:
    for mk, actions in grants.items():
        for ak, g in actions.items():
            _w("""INSERT INTO role_action_grants(role,module_key,action_key,granted,updated_at)
                  VALUES(%(r)s,%(m)s,%(a)s,%(g)s,NOW())
                  ON CONFLICT(role,module_key,action_key) DO UPDATE SET granted=EXCLUDED.granted,updated_at=NOW()""",
               {"r": role, "m": mk, "a": ak, "g": g})

def save_user_module_grants(user_id: str, grants: Dict[str, Optional[bool]]) -> None:
    for mk, cv in grants.items():
        _w("""INSERT INTO user_module_grants(user_id,module_key,can_view,updated_at)
              VALUES(%(u)s::uuid,%(m)s,%(v)s,NOW())
              ON CONFLICT(user_id,module_key) DO UPDATE SET can_view=EXCLUDED.can_view,updated_at=NOW()""",
           {"u": user_id, "m": mk, "v": cv})

def save_user_action_grants(user_id: str, grants: Dict[str, Dict[str, Optional[bool]]]) -> None:
    for mk, actions in grants.items():
        for ak, g in actions.items():
            if g is not None:
                _w("""INSERT INTO user_action_grants(user_id,module_key,action_key,granted,updated_at)
                      VALUES(%(u)s::uuid,%(m)s,%(a)s,%(g)s,NOW())
                      ON CONFLICT(user_id,module_key,action_key) DO UPDATE SET granted=EXCLUDED.granted,updated_at=NOW()""",
                   {"u": user_id, "m": mk, "a": ak, "g": g})

def seed_defaults_for_role(role: str) -> None:
    for mod in SIDEBAR_CATALOGUE:
        cv = role in mod.get("default_roles", []) or role == "admin"
        _w("""INSERT INTO role_module_grants(role,module_key,can_view)
              VALUES(%(r)s,%(m)s,%(v)s) ON CONFLICT(role,module_key) DO NOTHING""",
           {"r": role, "m": mod["key"], "v": cv})
    for act in ACTION_CATALOGUE:
        g = role in act.get("default_roles", []) or role == "admin"
        _w("""INSERT INTO role_action_grants(role,module_key,action_key,granted)
              VALUES(%(r)s,%(m)s,%(a)s,%(g)s) ON CONFLICT(role,module_key,action_key) DO NOTHING""",
           {"r": role, "m": act["module"], "a": act["key"], "g": g})

def seed_all_defaults() -> None:
    for role in ROLES_ORDERED:
        seed_defaults_for_role(role)

def log_override(action_type, order_no, original_val, new_val, reason,
                 reason_note="", approved_by="") -> None:
    try:
        from modules.security.roles import current_user_name, current_user_id
        uid = current_user_id(); uname = current_user_name()
    except Exception:
        uid, uname = None, "unknown"
    _w("""INSERT INTO permission_override_log
          (user_id,username,action_type,order_no,original_val,new_val,reason,reason_note,approved_by,created_at)
          VALUES(%(u)s,%(un)s,%(at)s,%(on)s,%(ov)s,%(nv)s,%(r)s,%(rn)s,%(ab)s,NOW())""",
       {"u": str(uid) if uid else None, "un": uname, "at": action_type,
        "on": order_no or "", "ov": original_val or "", "nv": new_val or "",
        "r": reason, "rn": reason_note or "", "ab": approved_by or ""})

__all__ = [
    "SIDEBAR_CATALOGUE","ACTION_CATALOGUE","ROLES_ORDERED","ROLE_COLORS",
    "PRICE_OVERRIDE_REASONS","DISCOUNT_APPROVAL_THRESHOLD",
    "get_discount_threshold","set_discount_threshold",
    "get_acting_role","grant_acting_role","revoke_acting_role","get_active_acting_roles",
    "get_visible_modules","can_do",
    "load_role_module_grants","load_role_action_grants",
    "save_role_module_grants","save_role_action_grants",
    "save_user_module_grants","save_user_action_grants",
    "seed_all_defaults","seed_defaults_for_role","log_override",
]
