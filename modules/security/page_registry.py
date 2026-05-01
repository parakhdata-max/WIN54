"""
modules/security/page_registry.py
══════════════════════════════════════════════════════════════════════════════
Single source of truth for every page in the sidebar AND every sub-tab
inside each page.

Place at: WIN54/modules/security/page_registry.py

HOW IT WORKS
─────────────
1. This file defines PAGES (sidebar) and SUB_TABS (sections within pages).
2. permission_engine.py imports PAGES → builds SIDEBAR_CATALOGUE dynamically.
3. permission_engine.py imports SUB_TABS → adds section actions dynamically.
4. app.py imports get_sidebar_pages() → builds the sidebar.

ADDING A NEW PAGE — only edit this file:
─────────────────────────────────────────
    # Step 1: Add to PAGES
    {"key": "my_new_page", "label": "🆕  My New Page",
     "section": "BILLING", "default_roles": ["billing","manager","admin"]},

    # Step 2: Add sub-tabs if the page has them
    "my_new_page": [
        ("view_tab_one",  "📋 Tab One",  ["billing","manager","admin"]),
        ("view_tab_two",  "📊 Tab Two",  ["manager","admin"]),
    ],

That's it. Permission Designer picks it up on next restart.
No changes needed in app.py, permission_engine.py, or anywhere else.
"""

from typing import List, Dict

# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR PAGES — complete list, in display order
# ─────────────────────────────────────────────────────────────────────────────
# key          : used as module_key throughout the permission system
# label        : shown in sidebar exactly as written
# section      : group heading (BILLING / PRODUCTION / STOCK / ADMIN)
# default_roles: roles that see this page by default
# feature_flag : optional — page only appears if this module loads successfully
# ─────────────────────────────────────────────────────────────────────────────

PAGES: List[Dict] = [

    # ── BILLING ──────────────────────────────────────────────────────────────
    {"key": "retail_order",      "label": "🛍️  Retail Order",        "section": "BILLING",
     "default_roles": ["billing","manager","admin"],    "feature_flag": "retail_ok"},

    {"key": "wholesale_order",   "label": "📦  Wholesale Order",      "section": "BILLING",
     "default_roles": ["billing","manager","admin"],    "feature_flag": "wholesale_ok"},

    {"key": "bulk_order",        "label": "⚡  Bulk Order",           "section": "BILLING",
     "default_roles": ["inventory","manager","admin"],  "feature_flag": "bulk_order_ok"},

    {"key": "orders",            "label": "📋  Orders",               "section": "BILLING",
     "default_roles": ["billing","lab","manager","admin"]},

    {"key": "cl_advisor",        "label": "👁️  CL Advisor",           "section": "BILLING",
     "default_roles": ["billing","manager","admin"]},

    {"key": "collect_payment",   "label": "💳  Collect Payment",      "section": "BILLING",
     "default_roles": ["billing","manager","admin"]},

    {"key": "challan_invoice",   "label": "🧾  Challan & Invoice",    "section": "BILLING",
     "default_roles": ["billing","manager","admin"]},

    {"key": "credit_debit",      "label": "📄  Credit & Debit Notes", "section": "BILLING",
     "default_roles": ["manager","admin"]},

    {"key": "reports",           "label": "📊  Reports",              "section": "BILLING",
     "default_roles": ["billing","manager","admin"]},

    {"key": "registers",         "label": "📚  Registers",            "section": "BILLING",
     "default_roles": ["billing","manager","admin"]},

    {"key": "accounts",          "label": "📒  Accounts",             "section": "BILLING",
     "default_roles": ["billing","manager","admin"]},

    {"key": "hr_attendance",     "label": "👥  HR & Attendance",      "section": "BILLING",
     "default_roles": ["staff","lab","billing","inventory","manager","admin"]},

    # ── PRODUCTION ───────────────────────────────────────────────────────────
    {"key": "backoffice",        "label": "⚙️  Backoffice",           "section": "PRODUCTION",
     "default_roles": ["lab","manager","admin"],        "feature_flag": "backoffice_ok"},

    {"key": "production",        "label": "🔬  Production",           "section": "PRODUCTION",
     "default_roles": ["lab","manager","admin"],        "feature_flag": "production_ok"},

    {"key": "rejection_report",  "label": "📋  Rejection Report",     "section": "PRODUCTION",
     "default_roles": ["manager","admin"]},

    # ── MASTERS & STOCK ──────────────────────────────────────────────────────
    {"key": "quick_add",         "label": "➕  Quick Add",            "section": "STOCK",
     "default_roles": ["billing","inventory","manager","admin"]},

    {"key": "scan_add_frame",    "label": "🕶️  Scan & Add Frame",     "section": "STOCK",
     "default_roles": ["inventory","manager","admin"]},

    {"key": "data_loader",       "label": "📥  Data Loader",          "section": "STOCK",
     "default_roles": ["manager","admin"]},

    {"key": "frame_stock",       "label": "🕶️  Frame Stock",          "section": "STOCK",
     "default_roles": ["billing","inventory","manager","admin"]},

    {"key": "inventory_search",  "label": "🔍  Inventory Search",     "section": "STOCK",
     "default_roles": ["billing","inventory","lab","manager","admin"]},

    {"key": "label_preview",     "label": "🏷️  Label Preview",        "section": "STOCK",
     "default_roles": ["inventory","manager","admin"]},

    {"key": "scanner",           "label": "📷  Scanner",              "section": "STOCK",
     "default_roles": ["billing","inventory","lab","manager","admin"]},

    {"key": "crm",               "label": "🤝  CRM / Parties",        "section": "STOCK",
     "default_roles": ["billing","manager","admin"],    "feature_flag": "crm_ok"},

    {"key": "procurement",       "label": "🛒  Procurement",          "section": "STOCK",
     "default_roles": ["inventory","manager","admin"],  "feature_flag": "procurement_ok"},

    {"key": "product_inventory", "label": "🔬  Product & Inventory",  "section": "STOCK",
     "default_roles": ["inventory","manager","admin"],  "feature_flag": "prod_inv_ok"},

    # ── ADMIN ────────────────────────────────────────────────────────────────
    {"key": "shop_master",       "label": "🏪  Shop Master",          "section": "ADMIN",
     "default_roles": ["manager","admin"]},

    {"key": "pricing_admin",     "label": "💲  Pricing Admin",        "section": "ADMIN",
     "default_roles": ["manager","admin"],              "feature_flag": "pricing_ok"},

    {"key": "edit_log",          "label": "📝  Edit Log",             "section": "ADMIN",
     "default_roles": ["manager","admin"]},

    {"key": "user_management",   "label": "👥  User Management",      "section": "ADMIN",
     "default_roles": ["admin"]},

    {"key": "system_health",     "label": "❤️  System Health",        "section": "ADMIN",
     "default_roles": ["admin"],                        "feature_flag": "system_health_ok"},

    {"key": "order_numbers",     "label": "🔢  Order Numbers",        "section": "ADMIN",
     "default_roles": ["admin"]},

    {"key": "import_analytics",  "label": "📊  Import Analytics",     "section": "ADMIN",
     "default_roles": ["admin"],                        "feature_flag": "analytics_ok"},

    {"key": "schema_evolution",  "label": "🔧  Schema Evolution",     "section": "ADMIN",
     "default_roles": ["admin"],                        "feature_flag": "schema_evo_ok"},

    {"key": "import_rollback",   "label": "↩️  Import Rollback",      "section": "ADMIN",
     "default_roles": ["admin"],                        "feature_flag": "rollback_ok"},
]


# ─────────────────────────────────────────────────────────────────────────────
# SUB-TABS — sections within each page
# ─────────────────────────────────────────────────────────────────────────────
# Format: {module_key: [(action_key, tab_label, default_roles), ...]}
# action_key must start with "view_" by convention
# ─────────────────────────────────────────────────────────────────────────────

SUB_TABS: Dict[str, List] = {

    # ── Challan & Invoice ─────────────────────────────────────────────────
    "challan_invoice": [
        ("view_challans_list",       "📋 Challans",          ["billing","manager","admin"]),
        ("view_invoices_list",       "🧾 Invoices",          ["billing","manager","admin"]),
        ("view_create_challan",      "➕ Create Challan",    ["billing","manager","admin"]),
        ("view_create_invoice",      "➕ Create Invoice",    ["billing","manager","admin"]),
        ("view_challan_payments",    "💰 Payments",          ["billing","manager","admin"]),
        ("view_bulk_actions",        "🚀 Bulk Actions",      ["manager","admin"]),
        ("view_challan_analytics",   "📈 Analytics",         ["manager","admin"]),
    ],

    # ── Credit & Debit Notes ──────────────────────────────────────────────
    "credit_debit": [
        ("view_new_credit_note",     "➕ New Credit Note",   ["manager","admin"]),
        ("view_new_debit_note",      "➕ New Debit Note",    ["manager","admin"]),
        ("view_cn_register",         "📋 Register",          ["billing","manager","admin"]),
        ("view_tally_export",        "📤 Tally Export",      ["manager","admin"]),
        ("view_gstr1_preview",       "📊 GSTR-1 Preview",    ["manager","admin"]),
    ],

    # ── Collect Payment ───────────────────────────────────────────────────
    "collect_payment": [
        ("view_retail_payments",     "🛍️  Retail",           ["billing","manager","admin"]),
        ("view_wholesale_payments",  "📦  Wholesale",        ["billing","manager","admin"]),
        ("view_all_parties_payment", "🌐  All Parties",      ["manager","admin"]),
        ("view_disbursements",       "💸  Disbursement",     ["manager","admin"]),
    ],

    # ── Reports ───────────────────────────────────────────────────────────
    "reports": [
        ("view_ledger",              "📒 Ledger",             ["billing","manager","admin"]),
        ("view_product_sales",       "📦 Product Sales",      ["billing","manager","admin"]),
        ("view_columnar",            "📋 Columnar",           ["billing","manager","admin"]),
        ("view_credit_days",         "⏰ Credit Days",         ["manager","admin"]),
        ("view_challan_register",    "🚚 Challan Register",   ["billing","manager","admin"]),
        ("view_stock_value",         "🏭 Stock Value",        ["inventory","manager","admin"]),
        ("view_outstanding",         "💰 Party Outstanding",  ["billing","manager","admin"]),
        ("view_aging",               "📅 Aging Report",       ["manager","admin"]),
        ("view_cashflow",            "💵 Cash Flow",          ["manager","admin"]),
        ("view_gst",                 "🧾 GST Summary",        ["billing","manager","admin"]),
        ("view_audit_trail",         "🔍 Audit Trail",        ["manager","admin"]),
    ],

    # ── Registers ─────────────────────────────────────────────────────────
    "registers": [
        ("view_sales_register",      "🧾 Sales Register",     ["billing","manager","admin"]),
        ("view_purchase_register",   "🛒 Purchase Register",  ["inventory","manager","admin"]),
        ("view_receipt_book",        "💵 Receipt Book",        ["billing","manager","admin"]),
        ("view_disbursement_book",   "💸 Disbursement Book",  ["manager","admin"]),
        ("view_cash_book",           "💰 Cash Book",           ["manager","admin"]),
        ("view_bank_book",           "🏦 Bank Book",           ["manager","admin"]),
        ("view_party_ledger",        "👤 Party Ledger",        ["billing","manager","admin"]),
        ("view_debtors_register",    "📥 Debtors Register",   ["manager","admin"]),
        ("view_order_register",      "📦 Order Register",      ["billing","lab","manager","admin"]),
        ("view_journal_register",    "📋 Journal Register",   ["manager","admin"]),
    ],

    # ── Accounts ──────────────────────────────────────────────────────────
    "accounts": [
        ("view_chart_of_accounts",   "📋 Chart of Accounts",  ["manager","admin"]),
        ("view_account_ledger",      "📖 Account Ledger",     ["billing","manager","admin"]),
        ("view_journal_entry",       "✏️ Journal Entry",       ["manager","admin"]),
        ("view_bank_book",           "🏦 Bank Book",           ["manager","admin"]),
        ("view_voucher_register",    "📄 Voucher Register",   ["billing","manager","admin"]),
        ("view_trial_balance",       "⚖️ Trial Balance",       ["manager","admin"]),
        ("view_pl",                  "📈 P&L Statement",       ["manager","admin"]),
        ("view_balance_sheet",       "🏛️ Balance Sheet",       ["manager","admin"]),
        ("view_backfill",            "🔄 Backfill",            ["admin"]),
    ],

    # ── HR & Attendance ───────────────────────────────────────────────────
    "hr_attendance": [
        ("view_my_attendance",       "📍 My Attendance",      ["staff","lab","billing","inventory","manager","admin"]),
        ("view_roster",              "📋 Today's Roster",     ["manager","admin"]),
        ("view_monthly_sheet",       "📅 Monthly Sheet",      ["manager","admin"]),
        ("view_leave",               "🏖️ Leave",               ["staff","lab","billing","inventory","manager","admin"]),
        ("view_employees",           "👤 Employees",           ["manager","admin"]),
        ("view_office_setup",        "🏢 Office Setup",        ["admin"]),
        ("view_payroll",             "💰 Payroll",             ["manager","admin"]),
    ],

    # ── Backoffice ────────────────────────────────────────────────────────
    "backoffice": [
        ("view_bo_order_items",      "📦 Order Items",         ["lab","manager","admin"]),
        ("view_bo_documents",        "📄 Documents",           ["lab","manager","admin"]),
        ("view_bo_status",           "📊 Status",              ["lab","billing","manager","admin"]),
        ("view_bo_billing_summary",  "💰 Billing Summary",     ["billing","manager","admin"]),
        ("view_bo_supplier_orders",  "🚚 Supplier Orders",     ["lab","inventory","manager","admin"]),
    ],

    # ── Production ────────────────────────────────────────────────────────
    "production": [
        ("view_prod_queue",          "📋 Queue",               ["lab","manager","admin"]),
        ("view_prod_fitters",        "🧵 Fitter Management",   ["manager","admin"]),
    ],

    # ── Procurement ───────────────────────────────────────────────────────
    "procurement": [
        ("view_smart_conversion",    "🎯 Smart Conversion",    ["inventory","manager","admin"]),
        ("view_purchase_orders",     "📋 Purchase Orders",     ["inventory","manager","admin"]),
        ("view_procurement_analytics","📊 Analytics",          ["manager","admin"]),
        ("view_reorder_monitor",     "🔄 Reorder Monitor",     ["inventory","manager","admin"]),
        ("view_supplier_override",   "🔀 Supplier Override",   ["manager","admin"]),
        ("view_supplier_schedule",   "⏰ Supplier Schedule",   ["manager","admin"]),
        ("view_stock_minimums",      "📐 Stock Minimums",      ["inventory","manager","admin"]),
    ],

    # ── CRM / Parties ─────────────────────────────────────────────────────
    "crm": [
        ("view_suppliers",           "🏭 Suppliers",           ["inventory","manager","admin"]),
        ("view_party_master",        "🗂️ Party Master",        ["billing","manager","admin"]),
        ("view_leads",               "🎯 Leads",               ["billing","manager","admin"]),
        ("view_followups",           "📅 Follow-Ups",          ["billing","manager","admin"]),
        ("view_contacts",            "📋 Contacts",            ["billing","manager","admin"]),
    ],

    # ── Wholesale Order (if has sub-tabs) ────────────────────────────────
    "wholesale_order": [
        ("view_wholesale_punch",     "🛒 New Order",           ["billing","manager","admin"]),
        ("view_wholesale_pending",   "⏳ Pending",             ["billing","manager","admin"]),
        ("view_wholesale_history",   "📋 Order History",       ["billing","manager","admin"]),
    ],

    # ── Orders page ───────────────────────────────────────────────────────
    "orders": [
        ("view_rx_orders",           "👓 Rx Orders",           ["billing","lab","manager","admin"]),
        ("view_consultations",       "🩺 Consultations",       ["billing","manager","admin"]),
    ],

    # ── Product & Inventory ───────────────────────────────────────────────
    "product_inventory": [
        ("view_product_master",      "📦 Product Master",      ["inventory","manager","admin"]),
        ("view_stock_management",    "📊 Stock Management",    ["inventory","manager","admin"]),
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS  (used by permission_engine.py and app.py)
# ─────────────────────────────────────────────────────────────────────────────

def get_sidebar_catalogue() -> List[Dict]:
    """
    Returns SIDEBAR_CATALOGUE compatible with permission_engine.
    Strips feature_flag — that's only used by app.py.
    """
    return [
        {
            "key":          p["key"],
            "label":        p["label"],
            "section":      p["section"],
            "default_roles": p["default_roles"],
        }
        for p in PAGES
    ]


def get_all_section_actions() -> List[Dict]:
    """
    Returns all sub-tab entries as ACTION_CATALOGUE compatible dicts.
    Each entry has: module, key, label, default_roles.
    """
    actions = []
    for module_key, tabs in SUB_TABS.items():
        for action_key, label, default_roles in tabs:
            actions.append({
                "module":        module_key,
                "key":           action_key,
                "label":         f"Tab: {label}",
                "default_roles": default_roles,
            })
    return actions


def get_subtabs_for_module(module_key: str) -> List[tuple]:
    """
    Returns [(action_key, label, default_roles), ...] for a module.
    Used by section_guard and wiring guides.
    """
    return SUB_TABS.get(module_key, [])


def get_section_labels_only(module_key: str) -> List[str]:
    """Returns just the display labels for a module's sub-tabs."""
    return [label for _, label, _ in SUB_TABS.get(module_key, [])]


# ── Label → Key mapping (used by app.py to write live registry) ───────────
# Maps the exact sidebar label string → module_key.
# Includes both trimmed and padded variants (app.py uses "  " padding).
LABEL_TO_KEY: Dict[str, str] = {
    p["label"].strip(): p["key"]
    for p in PAGES
}
# Also add padded variants to handle "🛍️  Retail Order" vs "🛍️ Retail Order"
for _p in PAGES:
    LABEL_TO_KEY[_p["label"]] = _p["key"]
