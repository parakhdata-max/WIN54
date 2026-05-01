"""
DV ERP - Main Streamlit Application
Unified Retail + Wholesale + Backoffice
With RBAC, Login, Performance Logging + State Isolation
"""

import streamlit as st
import traceback
import sys
import os
import logging
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

# ==================================================
# LOGGING SETUP
# ==================================================

_LOG_DIR  = os.path.join(BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "app.log")

_log_formatter   = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
_file_handler    = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(_log_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])

APP_START = time.time()

def log_time(msg: str):
    logging.info(f"[PERF] {msg} | {round(time.time()-APP_START,3)}s")

log_time("Application starting")

# ==================================================
# PATH SETUP
# ==================================================

MODULES_DIR = os.path.join(BASE_DIR, "modules")
if BASE_DIR    not in sys.path: sys.path.insert(0, BASE_DIR)
if MODULES_DIR not in sys.path: sys.path.insert(0, MODULES_DIR)

log_time("Paths configured")

# ==================================================
# FEATURE FLAGS — sync DB → SYSTEM_FLAGS dict
# Must happen BEFORE any module that reads SYSTEM_FLAGS
# ==================================================

try:
    from modules.flags.feature_flags import sync_system_flags
    sync_system_flags()
    log_time("Feature flags synced")
except Exception as _flag_err:
    logging.warning(f"[FLAGS] sync skipped: {_flag_err}")

# ==================================================
# PAYMENT PAGE ROUTE — no auth required
# Customers open ?pay=TOKEN from WhatsApp link.
# Must intercept BEFORE set_page_config so the
# payment page can set its own page config.
# ==================================================

_pay_token = st.query_params.get("pay", "")
if _pay_token:
    try:
        from modules.billing.payment_link_manager import render_payment_page
        render_payment_page(str(_pay_token))
    except Exception as _pge:
        st.error(f"Payment page error: {_pge}")
        import traceback; st.code(traceback.format_exc())
    st.stop()

# ==================================================
# PAGE CONFIG
# ==================================================

st.set_page_config(
    page_title="DV ERP",
    page_icon="👓",
    layout="wide",
    initial_sidebar_state="expanded"
)

log_time("Page config loaded")

# ==================================================
# ENSURE USERS TABLE + DEFAULT ADMIN EXIST
# (runs once at startup — silent on failure)
# ==================================================

_first_run = False
try:
    from modules.security.auth import ensure_default_admin
    _first_run = ensure_default_admin()
except Exception:
    pass

# ==================================================
# LOGIN GATE — blocks everything until authenticated
# ==================================================

try:
    from modules.security.auth import is_logged_in, render_login_page
    if not is_logged_in():
        render_login_page()
        st.stop()
except Exception as _auth_err:
    st.error(f"AUTH ERROR: {_auth_err}")
    raise

# ── From here: user is authenticated ──────────────────────────────────────

from modules.security.roles import (
    ADMIN, MANAGER, BILLING, LAB, INVENTORY, VIEWER,
    current_role, current_user_name, has_role
)

_role     = current_role()
_username = current_user_name()

log_time("Auth OK")

# ==================================================
# HEADER
# ==================================================

_hcol1, _hcol2 = st.columns([5, 1])
with _hcol1:
    st.title("DV ERP 👓")
    st.caption("Optical Business Management System")
with _hcol2:
    _ROLE_COLORS = {
        ADMIN: "#7c3aed", MANAGER: "#0284c7", BILLING: "#059669",
        LAB: "#d97706",   INVENTORY: "#dc2626", VIEWER: "#6b7280",
    }
    _role_color = _ROLE_COLORS.get(_role, "#6b7280")
    st.markdown(
        f"<div style='text-align:right;padding-top:0.8rem'>"
        f"<span style='color:#64748b;font-size:0.85rem'>👤 {_username}</span><br>"
        f"<span style='background:{_role_color};color:white;padding:2px 8px;"
        f"border-radius:10px;font-size:0.75rem;font-weight:600'>{_role.upper()}</span>"
        f"</div>",
        unsafe_allow_html=True
    )

log_time("Header rendered")

# ==================================================
# FIRST-RUN WARNING
# ==================================================

if _first_run:
    st.warning(
        "⚠️ **First run detected** — default admin created. "
        "**Username:** `admin` | **Password:** `admin123`  \n"
        "Go to **Admin → User Management** and change this password immediately."
    )

# ==================================================
# DATABASE CHECK — once per session only (cached)
# Prevents 3 DB queries on every single rerun.
# ==================================================

if "db_info_cache" not in st.session_state:
    try:
        from modules.sql_adapter import get_database_info
        st.session_state.db_info_cache = get_database_info()
    except Exception as _dbe:
        st.session_state.db_info_cache = {"connected": False, "error": str(_dbe)}

_db = st.session_state.db_info_cache
if not _db.get("connected"):
    st.warning(f"⚠️ Database: {_db.get('error', 'Unknown')}")
# ✅ Connected — silent. No green banner on every rerun.

st.divider()

# ==================================================
# AUTO SCHEMA SYNC — once per session only
# Was running diff_schema() on every rerun (expensive).
# ==================================================

if "schema_synced" not in st.session_state:
    st.session_state.schema_synced = False
    try:
        from modules.loaders.schema_sync import diff_schema, apply_sync, reload_registry
        _sync_diff = diff_schema()
        _new_cols  = _sync_diff.get("new_columns", [])
        if _new_cols:
            _ok, _msg, _added = apply_sync(_new_cols)
            if _ok and _added:
                reload_registry()
                st.toast(f"🔄 Schema synced — {len(_added)} new column(s)", icon="✅")
        st.session_state.schema_synced = True
    except Exception as _sync_err:
        logging.warning(f"[SCHEMA-SYNC] Skipped: {_sync_err}")

# ==================================================
# SAFE IMPORT HELPER
# ==================================================

def safe_import(name, path, func):
    try:
        module = __import__(path, fromlist=[func])
        fn = getattr(module, func)
        log_time(f"{name} loaded")
        return fn, True
    except Exception as e:
        st.sidebar.error(f"{name}: {e}")
        logging.error(f"[IMPORT] {name} failed: {e}")
        log_time(f"{name} failed")
        return None, False

# ==================================================
# MODULE IMPORTS
# ==================================================

log_time("Importing modules")

render_retail_punching,       retail_ok       = safe_import("Retail",           "modules.retail_punching",              "render_retail_punching")
render_wholesale_punching,    wholesale_ok    = safe_import("Wholesale",         "modules.wholesale_punching",           "render_wholesale_punching")
render_backoffice_management, backoffice_ok   = safe_import("Backoffice",        "modules.backoffice.backoffice",        "render_backoffice_management")
render_production_page,       production_ok   = safe_import("Production",        "modules.backoffice.production_page",   "render_production_page")
render_purchase_ui,           procurement_ok  = safe_import("Procurement",       "modules.procurement.purchase_ui",      "render_purchase_ui")
render_pricing_admin,         pricing_ok      = safe_import("Pricing Admin",     "modules.ui.pricing_admin.admin_ui",    "render_pricing_admin")
render_import_dashboard,      analytics_ok    = safe_import("Import Analytics",  "modules.analytics.import_dashboard",   "render_import_dashboard")
render_schema_evolution,      schema_evo_ok   = safe_import("Schema Evolution",  "modules.loaders.schema_evolution_ui",  "render_schema_evolution")
render_rollback_ui,           rollback_ok     = safe_import("Import Rollback",   "modules.loaders.rollback_engine",      "render_rollback_ui")
render_import_health,         health_ok       = safe_import("Import Health",     "modules.analytics.health_dashboard",   "render_import_health")

# ── New modules (Zone 1-4) ────────────────────────────────────────────────
render_control_dashboard,     founder_ok      = safe_import("Control Tower",     "modules.founder.control_dashboard",    "render_control_dashboard")
render_crm_module,            crm_ok          = safe_import("CRM",               "modules.crm.crm",                      "render_crm_module")
render_system_health,         system_health_ok = safe_import("System Health",    "modules.admin.system_health",          "render_system_health")

log_time("All modules imported")

# ==================================================
# CORE SESSION STATE
# ==================================================

if "active_module" not in st.session_state:
    st.session_state.active_module = None

# ==================================================
# SIDEBAR — RBAC-FILTERED
# ==================================================

log_time("Rendering sidebar")

# ── Sidebar: clean professional light theme ──────────────────────────────
st.markdown("""
<style>
/* Sidebar background */
section[data-testid="stSidebar"] {
    background: #ffffff !important;
    border-right: 1px solid #e5e7eb !important;
    min-width: 220px !important;
}
/* Kill any dark overrides from page modules */
section[data-testid="stSidebar"] > div {
    background: #ffffff !important;
}
/* All sidebar text dark */
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] div {
    color: #111827 !important;
}
/* Radio items */
section[data-testid="stSidebar"] .stRadio label {
    font-size: 0.83rem !important;
    font-weight: 500 !important;
    padding: 6px 12px 6px 10px !important;
    border-radius: 7px !important;
    margin: 1px 0 !important;
    display: block !important;
    cursor: pointer !important;
    text-transform: none !important;
    letter-spacing: normal !important;
    background: transparent !important;
    border: none !important;
    color: #374151 !important;
    transition: all 0.15s !important;
}
section[data-testid="stSidebar"] .stRadio label:hover {
    background: #e0f2fe !important;
    color: #0c4a6e !important;
}
section[data-testid="stSidebar"] .stRadio label:has(input:checked) {
    background: #7dd3fc !important;
    color: #0c4a6e !important;
    font-weight: 700 !important;
    border-left: 3px solid #0369a1 !important;
}
/* Logout button */
section[data-testid="stSidebar"] .stButton > button {
    background: #f9fafb !important;
    color: #6b7280 !important;
    border: 1px solid #e5e7eb !important;
    font-size: 0.78rem !important;
    border-radius: 7px !important;
}
section[data-testid="stSidebar"] .stButton > button:hover {
    background: #fee2e2 !important;
    color: #dc2626 !important;
    border-color: #fca5a5 !important;
}
/* Hide radio button circles */
section[data-testid="stSidebar"] .stRadio input[type="radio"] {
    display: none !important;
}
</style>
""", unsafe_allow_html=True)

# ── User pill ─────────────────────────────────────────────────────────────
_role_color = _ROLE_COLORS.get(_role, "#6b7280")
st.sidebar.markdown(
    f"""<div style='padding:10px 12px 12px;border-bottom:1px solid #f3f4f6;margin-bottom:8px'>
    <div style='font-size:0.68rem;color:#9ca3af;font-weight:600;text-transform:uppercase;
                letter-spacing:.07em;margin-bottom:3px'>Logged in as</div>
    <div style='font-size:0.92rem;font-weight:700;color:#111827'>{_username}</div>
    <span style='background:{_role_color};color:#fff;font-size:0.65rem;font-weight:700;
                 padding:2px 8px;border-radius:20px;letter-spacing:.04em'>{_role.upper()}</span>
    </div>""",
    unsafe_allow_html=True
)

# ── Build page list (flat, no section header markdown calls) ──────────────
pages = []

if has_role(BILLING, MANAGER, ADMIN):
    if retail_ok:    pages.append("🛍️  Retail Order")
    if wholesale_ok: pages.append("📦  Wholesale Order")
    pages.append("📋  Orders")
    pages.append("🔍  Inventory Search")

if has_role(LAB, MANAGER, ADMIN):
    if backoffice_ok:             pages.append("⚙️  Backoffice")
    if production_ok:             pages.append("🔬  Production")
    if has_role(MANAGER, ADMIN):  pages.append("📝  Edit Log")

if has_role(BILLING, INVENTORY, MANAGER, ADMIN):
    if crm_ok: pages.append("🤝  CRM")

if has_role(INVENTORY, MANAGER, ADMIN):
    if procurement_ok: pages.append("🛒  Procurement")

if has_role(BILLING, MANAGER, ADMIN):
    pages.append("🧾  Challan & Invoice")
    pages.append("📄  Credit & Debit Notes")

if has_role(ADMIN, MANAGER):
    if pricing_ok:       pages.append("💲  Pricing Admin")
    pages.append("📥  Data Loader")
    pages.append("🕶️  Frame Stock")
    pages.append("👥  User Management")
    if system_health_ok: pages.append("❤️  System Health")

if has_role(ADMIN):
    if analytics_ok:  pages.append("📊  Import Analytics")
    if health_ok:     pages.append("🩺  Import Health")
    if schema_evo_ok: pages.append("🔧  Schema Evolution")
    if rollback_ok:   pages.append("↩️  Import Rollback")

try:
    from modules.flags.feature_flags import SYSTEM_FLAGS
    _founder_flag = SYSTEM_FLAGS.get("founder_dashboard_enabled", False)
except Exception:
    _founder_flag = False

if has_role(ADMIN) and _founder_flag and founder_ok:
    pages.append("🏰  Control Tower")

if has_role(VIEWER) and not has_role(BILLING, LAB, INVENTORY, MANAGER, ADMIN):
    if analytics_ok: pages.append("📊  Import Analytics")

if not pages:
    st.sidebar.warning("No modules available for your role.")
    st.error("⛔ No modules available. Contact admin.")
    st.stop()

selected_page = st.sidebar.radio("", pages, label_visibility="collapsed")

# ── Strip emoji prefix to get router key ─────────────────────────────────
_PAGE_MAP = {
    "🛍️  Retail Order":        "Retail Order",
    "📦  Wholesale Order":      "Wholesale Order",
    "📋  Orders":               "📋 Orders",
    "⚙️  Backoffice":           "Backoffice",
    "🔬  Production":           "Production",
    "📝  Edit Log":             "Edit Log",
    "🤝  CRM":                  "CRM",
    "🛒  Procurement":          "Procurement",
    "🧾  Challan & Invoice":    "Challan & Invoice Dashboard",
    "📄  Credit & Debit Notes": "Credit & Debit Notes",
    "💲  Pricing Admin":        "Pricing Admin",
    "📥  Data Loader":          "Data Loader",
    "👥  User Management":      "User Management",
    "❤️  System Health":        "System Health",
    "📊  Import Analytics":     "Import Analytics",
    "🩺  Import Health":        "Import Health",
    "🔧  Schema Evolution":     "Schema Evolution",
    "↩️  Import Rollback":      "Import Rollback",
    "🏰  Control Tower":        "Control Tower",
    "🔍  Inventory Search":     "Inventory Search",
    "🕶️  Frame Stock":          "Frame Loader",
}
selected_page = _PAGE_MAP.get(selected_page, selected_page)

# ── Logout ────────────────────────────────────────────────────────────────
st.sidebar.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
if st.sidebar.button("🚪  Logout", use_container_width=True):
    from modules.security.auth import logout
    logout()

# ==================================================
# DEBUG PRICING TOGGLE (dev only — silent if missing)
# ==================================================
try:
    from modules.backoffice.devtools.debug_pricing_overlay import add_debug_toggle_to_sidebar
    add_debug_toggle_to_sidebar()
except Exception:
    pass

# ==================================================
# SYSTEM INFO (collapsed by default)
# ==================================================
with st.sidebar.expander("ℹ️ System Info"):
    st.write("**Loaded Modules**")
    for name, ok in [
        ("Retail",           retail_ok),
        ("Wholesale",        wholesale_ok),
        ("Backoffice",       backoffice_ok),
        ("Production",       production_ok),
        ("Procurement",      procurement_ok),
        ("CRM",              crm_ok),
        ("Pricing Admin",    pricing_ok),
        ("Import Analytics", analytics_ok),
        ("Import Health",    health_ok),
        ("Control Tower",    founder_ok),
        ("System Health",    system_health_ok),
    ]:
        st.write(f"- {name}: {'✅' if ok else '❌'}")
    try:
        from modules.pricing.pricing_engine import money
        st.write("- Pricing Engine: ✅")
    except Exception:
        st.write("- Pricing Engine: ❌")
    try:
        from modules.loaders.db_schema_registry import DB_SCHEMA, ALL_FILE_TYPES
        total_cols = sum(len(v) for v in DB_SCHEMA.values())
        st.write(f"- Schema Registry: 🟢 {len(ALL_FILE_TYPES)} types · {total_cols} cols")
    except Exception:
        st.write("- Schema Registry: 🔴")
    try:
        from modules.flags.feature_flags import SYSTEM_FLAGS
        advisory_on = SYSTEM_FLAGS.get("advisory_enabled", False)
        st.write(f"- Advisory: {'✅' if advisory_on else '⛔'}")
    except Exception:
        st.write("- Advisory: ⚠️")
    st.caption("Full health → Import Health dashboard")

# ==================================================
# AUTO RESET ON PAGE SWITCH — no rerun if same page
# Old code called st.rerun() unconditionally which
# caused an infinite loop: rerun → same page → rerun.
# ==================================================

def handle_page_switch(new_page: str):
    prev = st.session_state.get("active_module")
    if prev != new_page:
        # Clear module-specific state keys
        for key in list(st.session_state.keys()):
            if key.startswith("retail_") or key.startswith("wholesale_"):
                del st.session_state[key]
        st.session_state.active_module = new_page
        log_time(f"Switched to {new_page}")
        st.rerun()   # Only fires when page actually changes

handle_page_switch(selected_page)

# ==================================================
# SAFE RENDER HELPER
# ==================================================

def safe_render(fn, name: str):
    try:
        fn()
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"[{name}] Render failed: {e}\n{tb}")
        st.error(f"❌ {name} error: {e}")
        st.code(tb)

# ==================================================
# PAGE ROUTER — RBAC DOUBLE-GUARD
# Every page checks role independently (defence in depth).
# ==================================================

# ── Sales ──────────────────────────────────────────────────────────────────

if selected_page == "Retail Order":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Retail")
    if retail_ok: safe_render(render_retail_punching, "Retail")
    else: st.error("Retail unavailable")

elif selected_page == "Wholesale Order":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Wholesale")
    if wholesale_ok: safe_render(render_wholesale_punching, "Wholesale")
    else: st.error("Wholesale unavailable")

elif selected_page == "📋 Orders":
    from modules.security.roles import require_role
    require_role(BILLING, LAB, MANAGER, ADMIN)
    log_time("Open Orders")
    try:
        from modules.backoffice.order_edit_view import render_order_edit_view
        safe_render(render_order_edit_view, "Orders")
    except Exception as _oev_e:
        st.error(f"Orders view error: {_oev_e}")
        import traceback; st.code(traceback.format_exc())

# ── Operations ─────────────────────────────────────────────────────────────

elif selected_page == "Backoffice":
    from modules.security.roles import require_role
    require_role(LAB, MANAGER, ADMIN)
    log_time("Open Backoffice")
    if backoffice_ok: safe_render(render_backoffice_management, "Backoffice")
    else: st.error("Backoffice unavailable")

elif selected_page == "Edit Log":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open Edit Log")
    try:
        from modules.backoffice.edit_log_panel import render_edit_log_page
        safe_render(render_edit_log_page, "Edit Log")
    except Exception as e:
        st.error(f"❌ Edit Log failed: {e}")
        import traceback; st.code(traceback.format_exc())

elif selected_page == "Production":
    from modules.security.roles import require_role
    require_role(LAB, MANAGER, ADMIN)
    log_time("Open Production")
    if production_ok: safe_render(render_production_page, "Production")
    else: st.error("Production unavailable")

# ── Procurement ────────────────────────────────────────────────────────────

elif selected_page == "Procurement":
    from modules.security.roles import require_role
    require_role(INVENTORY, MANAGER, ADMIN)
    log_time("Open Procurement")
    if procurement_ok: safe_render(render_purchase_ui, "Procurement")
    else: st.error("Procurement unavailable")

# ── CRM ────────────────────────────────────────────────────────────────────

elif selected_page == "CRM":
    from modules.security.roles import require_role
    require_role(BILLING, INVENTORY, MANAGER, ADMIN)
    log_time("Open CRM")
    if crm_ok: safe_render(render_crm_module, "CRM")
    else: st.error("CRM unavailable")

# ── Billing Dashboard ──────────────────────────────────────────────────────

elif selected_page == "Challan & Invoice Dashboard":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Billing Dashboard")
    try:
        from modules.billing.challan_preview import render_challan_invoice_dashboard
        safe_render(render_challan_invoice_dashboard, "Billing Dashboard")
    except Exception as e:
        st.error(f"❌ Billing Dashboard failed: {e}")
        st.code(traceback.format_exc())

# ── Credit & Debit Notes ──────────────────────────────────────────────────

elif selected_page == "Credit & Debit Notes":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Credit & Debit Notes")
    try:
        from modules.billing.credit_debit_note_ui import render_cdn_module
        safe_render(render_cdn_module, "Credit & Debit Notes")
    except Exception as e:
        st.error(f"❌ Credit & Debit Notes failed: {e}")
        st.code(traceback.format_exc())

# ── Admin Tools ────────────────────────────────────────────────────────────

elif selected_page == "Pricing Admin":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open Pricing Admin")
    if pricing_ok: safe_render(render_pricing_admin, "Pricing Admin")
    else: st.error("Pricing Admin unavailable")

elif selected_page == "Data Loader":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open Data Loader")
    try:
        from modules.ui.smart_loader_ui import render_smart_loader
        safe_render(render_smart_loader, "Data Loader")
    except Exception as e:
        st.error(f"❌ Data Loader failed: {e}")
        st.code(traceback.format_exc())

elif selected_page == "Inventory Search":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN, LAB, INVENTORY)
    log_time("Open Inventory Search")
    try:
        from modules.ui.inventory_search import render_inventory_search
        safe_render(render_inventory_search, "Inventory Search")
    except Exception as e:
        st.error(f"❌ Inventory Search failed: {e}")
        import traceback; st.code(traceback.format_exc())

elif selected_page == "Frame Loader":
    from modules.security.roles import require_role
    require_role(INVENTORY, MANAGER, ADMIN)
    log_time("Open Frame Loader")
    try:
        from modules.ui.frame_batch_loader import render_frame_batch_loader
        safe_render(render_frame_batch_loader, "Frame Loader")
    except Exception as e:
        st.error(f"❌ Frame Loader failed: {e}")
        import traceback; st.code(traceback.format_exc())

elif selected_page == "User Management":
    from modules.security.roles import require_role
    require_role(ADMIN)
    log_time("Open User Management")
    from modules.security.user_management_ui import render_user_management
    render_user_management()

elif selected_page == "System Health":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open System Health")
    if system_health_ok: safe_render(render_system_health, "System Health")
    else: st.error("System Health unavailable")

# ── Ingestion ──────────────────────────────────────────────────────────────

elif selected_page == "Import Analytics":
    from modules.security.roles import require_role
    require_role(ADMIN, MANAGER)
    log_time("Open Import Analytics")
    if analytics_ok: safe_render(render_import_dashboard, "Import Analytics")
    else: st.error("Import Analytics unavailable")

elif selected_page == "Import Health":
    from modules.security.roles import require_role
    require_role(ADMIN)
    log_time("Open Import Health")
    if health_ok: safe_render(render_import_health, "Import Health Dashboard")
    else: st.error("Import Health unavailable")

elif selected_page == "Schema Evolution":
    from modules.security.roles import require_role
    require_role(ADMIN)
    log_time("Open Schema Evolution")
    if schema_evo_ok: safe_render(render_schema_evolution, "Schema Evolution")
    else: st.error("Schema Evolution unavailable")

elif selected_page == "Import Rollback":
    from modules.security.roles import require_role
    require_role(ADMIN)
    log_time("Open Import Rollback")
    if rollback_ok: safe_render(render_rollback_ui, "Import Rollback")
    else: st.error("Import Rollback unavailable")

# ── Founder ────────────────────────────────────────────────────────────────

elif selected_page == "Control Tower":
    from modules.security.roles import require_role
    require_role(ADMIN)
    log_time("Open Control Tower")
    if founder_ok: safe_render(render_control_dashboard, "Control Tower")
    else: st.error("Control Tower unavailable")

# ==================================================
# FOOTER
# ==================================================

st.divider()
st.caption(f"DV ERP v1.0 | Unified Clean Architecture | {_username} ({_role.upper()})")

log_time("App fully loaded")
