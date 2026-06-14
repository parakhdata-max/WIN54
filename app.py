"""
DV ERP - Main Streamlit Application
Unified Retail + Wholesale + Backoffice
With RBAC, Login, Performance Logging + State Isolation
"""

from dotenv import load_dotenv
import os
import streamlit as st
import traceback
import sys
import logging
import time
from logging.handlers import RotatingFileHandler

# Load ENV
load_dotenv()

# ==================================================
# PAGE CONFIG — must be the FIRST Streamlit command
# ==================================================

st.set_page_config(
    page_title="DV ERP",
    page_icon="👓",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ENV SWITCH (environment based)
from modules.core.environment import app_env as _app_env, db_label as _db_label, db_url as _db_url, is_prod as _is_prod_env

_env = _app_env()
DB_URL = _db_url()
st.session_state["DB_URL"] = DB_URL
st.session_state["APP_ENV"] = _env
if _is_prod_env():
    st.sidebar.markdown(
        f"""
        <div style="background:#7f1d1d;color:#fff;border:2px solid #ef4444;
                    padding:10px 12px;border-radius:8px;text-align:center;
                    font-weight:900;font-size:1.05rem;margin-bottom:8px">
            LIVE / PRODUCTION<br>
            <span style="font-size:.78rem;font-weight:700">{_db_label(DB_URL)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    st.sidebar.markdown(
        f"""
        <div style="background:#facc15;color:#111827;border:3px solid #dc2626;
                    padding:12px;border-radius:8px;text-align:center;
                    font-weight:1000;font-size:1.45rem;margin-bottom:8px">
            BIG TEST<br>
            <span style="font-size:.82rem;font-weight:800">Database: {_db_label(DB_URL)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

# Run idempotent schema migrations after the selected DB URL is known.
try:
    if not st.session_state.get("_migrations_done"):
        from modules.db.migrations.runner import run_pending_migrations
        _applied_migrations = run_pending_migrations()
        st.session_state["_migrations_done"] = True
        if _applied_migrations:
            st.sidebar.caption(f"DB migrations: {len(_applied_migrations)} applied")
except Exception as _mig_err:
    st.error(f"Database migration failed: {_mig_err}")
    st.stop()

# Base path setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)
# ==================================================
# LOGGING SETUP
# ==================================================

_LOG_DIR  = os.path.join(BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "app.log")

_log_formatter   = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
_file_handler = RotatingFileHandler(
    _LOG_FILE,
    encoding="utf-8",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
)
_file_handler.setLevel(logging.WARNING)
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

log_time("Feature flag sync deferred until after login")

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
# RETAILER PORTAL ROUTE — no ERP login required
# Retailers open ?portal=retailer from their link.
# Has its own OTP-based login system.
# ==================================================

_portal_param = st.query_params.get("portal", "")
if _portal_param == "retailer":
    try:
        from modules.retailer.retailer_portal import render_retailer_portal
        render_retailer_portal()
    except Exception as _rpe:
        st.error(f"Retailer Portal error: {_rpe}")
        import traceback; st.code(traceback.format_exc())
    st.stop()

if _portal_param == "online":
    try:
        from modules.online_store.store_app import render_online_store
        render_online_store()
    except Exception as _ose:
        st.error(f"Online Store error: {_ose}")
        import traceback; st.code(traceback.format_exc())
    st.stop()

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

# Restore keyboard page scrolling in Streamlit layouts. Some widgets/iframes keep
# focus away from the real page scroller, so Arrow/Page keys can stop moving.
# ── Keyboard scroll + dropdown arrow-key fix ─────────────────────────────────
try:
    from keyboard_fix import install_keyboard_fix
    install_keyboard_fix()
except Exception:
    pass

# ==================================================
# POST-LOGIN STARTUP MAINTENANCE
# Keep this behind the login gate so the login page renders quickly.
# ==================================================

if not st.session_state.get("_feature_flags_synced"):
    try:
        from modules.flags.feature_flags import sync_system_flags
        sync_system_flags()
        log_time("Feature flags synced")
    except Exception as _flag_err:
        logging.warning(f"[FLAGS] sync skipped: {_flag_err}")
    st.session_state["_feature_flags_synced"] = True

# ── Order number registry: sync counters from existing data (once per session) ──
# Runs in its own transaction, isolated from order saves.
# Safe to run on startup — GREATEST() means counters never go backward.
if not st.session_state.get("_registry_synced"):
    try:
        from modules.db.order_number_registry import sync_registry_from_existing_orders
        sync_registry_from_existing_orders()
        st.session_state["_registry_synced"] = True
        log_time("Order registry synced")
    except Exception:
        pass  # non-fatal — registry will self-create on first order save

# ── Stock allocation drift check — once per session after login ────────
# Detect only (fix=False). Any drift is logged; operator sees it in
# System Health Check. Nightly fix=True can be scheduled separately.
if not st.session_state.get("_stock_recon_done"):
    try:
        from modules.backoffice.audit_logger import reconcile_stock_allocations
        _recon_result = reconcile_stock_allocations(fix=False)
        if not _recon_result.get("ok"):
            import logging as _recon_log
            _recon_log.warning(
                "[Startup] Stock drift detected - %d line(s). "
                "Run reconcile_stock_allocations(fix=True) to patch.",
                len(_recon_result.get("drifted", []))
            )
        log_time("Stock allocation reconcile checked")
    except Exception:
        pass  # startup check never blocks the app
    st.session_state["_stock_recon_done"] = True

# Sync order statuses only when explicitly requested from a maintenance page.
# Running this over hundreds of orders before the UI renders can delay startup
# by 40-60 seconds on real data.

# ==================================================
# ROLE COLOURS — used in sidebar logo
# ==================================================

_ROLE_COLORS = {
    ADMIN: "#7c3aed", MANAGER: "#0284c7", BILLING: "#059669",
    LAB: "#d97706",   INVENTORY: "#dc2626", VIEWER: "#6b7280",
}
_role_color = _ROLE_COLORS.get(_role, "#6b7280")

log_time("Header rendered")

# ==================================================
# FIRST-RUN WARNING
# ==================================================

if _first_run:
    st.warning(
        "⚠️ **First run detected** — default admin created. "
        "Use the secured bootstrap credential from your deployment notes, then go to "
        "**Admin → User Management** and change it immediately."
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

    # ── Step 1: Run all pending DB migrations (ADD COLUMN IF NOT EXISTS) ────────
    # Single source of truth: modules/loaders/migrations.py
    try:
        from modules.loaders.migrations import run_all_migrations, MIGRATIONS
        _mig = run_all_migrations(silent=True)
        # Store full result in session_state so sidebar panel can display it
        st.session_state["_migration_result"] = {
            "applied":   _mig.get("applied", []),
            "skipped":   _mig.get("skipped", []),
            "errors":    _mig.get("errors", []),
            "total_defined": len(MIGRATIONS),
            "ran_at": __import__("datetime").datetime.now().strftime("%d %b %Y %H:%M:%S"),
        }
        _mig_applied = len(_mig.get("applied", []))
        _mig_errors  = _mig.get("errors", [])
        if _mig_applied:
            logging.info(f"[MIGRATION] {_mig_applied} column(s) applied")
            st.toast(f"🗄️ DB migration — {_mig_applied} new column(s) added", icon="✅")
        if _mig_errors:
            logging.warning(f"[MIGRATION] Errors: {_mig_errors}")
    except Exception as _mig_err:
        logging.warning(f"[MIGRATION] Skipped: {_mig_err}")
        st.session_state["_migration_result"] = {"errors": [str(_mig_err)], "ran_at": "failed"}

    # ── Step 2: Sync schema registry with live DB columns ─────────────────────
    # Detects any columns in DB not yet in db_schema_registry.py and adds them.
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

# ── Lazy import: only check if module is importable, don't load yet ───────
def _can_import(path, func):
    """Check if module exists without fully importing it (fast)."""
    try:
        import importlib.util
        spec = importlib.util.find_spec(path.replace(".", "/").split("/")[0])
        return spec is not None
    except Exception:
        return False

def lazy_import(name, path, func):
    """Return a lazy loader — module only imported when function is called."""
    try:
        # Just check the module file exists, don't execute it
        import importlib.util, os
        parts = path.split(".")
        mod_path = os.path.join(*parts) + ".py"
        full_path = os.path.join(os.path.dirname(__file__), mod_path)
        exists = os.path.exists(full_path)
        if not exists:
            return None, False

        def _loader(*args, **kwargs):
            module = __import__(path, fromlist=[func])
            return getattr(module, func)(*args, **kwargs)

        return _loader, True
    except Exception as e:
        logging.warning(f"[LAZY] {name} not found: {e}")
        return None, False

render_retail_punching,       retail_ok        = lazy_import("Retail",           "modules.retail_punching",              "render_retail_punching")
render_wholesale_punching,    wholesale_ok     = lazy_import("Wholesale",         "modules.wholesale_punching",           "render_wholesale_punching")
render_backoffice_management, backoffice_ok    = lazy_import("Backoffice",        "modules.backoffice.backoffice",        "render_backoffice_management")
render_production_page,       production_ok    = lazy_import("Production",        "modules.backoffice.production_page",   "render_production_page")
render_fitter_management,     fitter_ok        = lazy_import("Service Management", "modules.backoffice.fitter_manager",    "render_fitter_management")
render_purchase_ui,           procurement_ok   = lazy_import("Procurement",       "modules.procurement.purchase_ui",      "render_purchase_ui")
render_direct_purchase,       direct_pur_ok    = lazy_import("Direct Purchase",    "modules.procurement.direct_purchase_ui","render_direct_purchase_ui")
render_product_inventory_mgr, prod_inv_ok      = lazy_import("Product Inventory", "modules.procurement.product_inventory_manager", "render_product_inventory_manager")
render_bulk_order,            bulk_order_ok    = lazy_import("Bulk Order",        "modules.billing.bulk_order",                    "render_bulk_order")
render_wa_queue,              wa_queue_ok      = lazy_import("WA Queue",         "modules.wa_queue_sender",                       "render_wa_queue")
render_wa_bulk_sender,        wa_bulk_ok       = lazy_import("Bulk WhatsApp",     "modules.wa_bulk_sender",                        "render_wa_bulk_sender")
render_billing_hub,           billing_hub_ok   = lazy_import("Billing Hub",       "modules.billing.billing_hub",                   "render_billing_hub")
render_discount_audit,        disc_audit_ok    = lazy_import("Discount Audit",    "modules.pricing.discount_audit",                "render_discount_audit")
render_invoice_edit_admin,    inv_edit_ok      = lazy_import("Invoice Edit",      "modules.billing.invoice_edit_admin",            "render_invoice_edit_admin")
render_pricing_admin,         pricing_ok       = lazy_import("Pricing Admin",     "modules.ui.pricing_admin.admin_ui",    "render_pricing_admin")
render_import_dashboard,      analytics_ok     = lazy_import("Import Analytics",  "modules.analytics.import_dashboard",   "render_import_dashboard")
render_discount_dashboard,    disc_dash_ok     = lazy_import("Discount Analytics","modules.analytics.discount_dashboard", "render_discount_dashboard")
render_price_suggestions,     price_sug_ok     = lazy_import("AI Suggestions",    "modules.analytics.price_suggestions",  "render_price_suggestions")
render_schema_evolution,      schema_evo_ok    = lazy_import("Schema Evolution",  "modules.loaders.schema_evolution_ui",  "render_schema_evolution")
render_rollback_ui,           rollback_ok      = lazy_import("Import Rollback",   "modules.loaders.rollback_engine",      "render_rollback_ui")
render_import_health,         health_ok        = lazy_import("Import Health",     "modules.analytics.health_dashboard",   "render_import_health")

# ── New modules (Zone 1-4) ────────────────────────────────────────────────
render_control_dashboard,     founder_ok       = lazy_import("Control Tower",     "modules.founder.control_dashboard",    "render_control_dashboard")
render_owner_dashboard,       owner_dash_ok    = lazy_import("Owner Dashboard",   "modules.founder.owner_dashboard",      "render_owner_dashboard")
render_crm_module,            crm_ok           = lazy_import("CRM",               "modules.crm.crm",                      "render_crm_module")
render_system_health,         system_health_ok = lazy_import("System Health",     "modules.admin.system_health",          "render_system_health")
render_backoffice_orders,     ret_orders_ok    = lazy_import("Retailer Orders",   "modules.retailer.backoffice_orders",   "render_backoffice_orders")
render_dispatch_queue,        dispatch_ok      = lazy_import("Dispatch",          "modules.backoffice.dispatch_panel",    "render_dispatch_queue_tab")
render_online_store,          online_store_ok  = lazy_import("Online Store",      "modules.online_store.store_app",       "render_online_store")
render_online_store_admin,    online_admin_ok  = lazy_import("Online Admin",      "modules.online_store.store_admin",     "render_online_store_admin")

log_time("All modules registered (lazy)")

# ==================================================
# CORE SESSION STATE
# ==================================================

if "active_module" not in st.session_state:
    st.session_state.active_module = None

# ==================================================
# SIDEBAR — RBAC-FILTERED
# ==================================================

log_time("Rendering sidebar")

# ── Global layout: kill Streamlit's default top padding so pages start at top ──
st.markdown("""
<style>
/* Main content gets the full working canvas when sidebar is collapsed */
.block-container {
    max-width: 98vw !important;
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
    padding-top: 1.35rem !important;
    padding-bottom: 1rem !important;
}
/* Keep Streamlit's native header/sidebar toggle stable. */
header[data-testid="stHeader"] {
    background: transparent !important;
}
div[data-testid="stDecoration"] { display: none !important; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar: clean professional light theme ──────────────────────────────
st.markdown("""
<style>
/* Sidebar background */
section[data-testid="stSidebar"] {
    background: #ffffff !important;
    border-right: 1px solid #e5e7eb !important;
    min-width: 220px !important;
    max-width: 260px !important;
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

# App-wide readability pass: keep muted text/dividers visible on light panels.
st.markdown("""
<style>
:root {
    --dv-muted-readable: #374151;
    --dv-muted-readable-strong: #1f2937;
    --dv-line-readable: #6b7280;
}

[data-testid="stCaptionContainer"],
[data-testid="stMarkdownContainer"] small,
[data-testid="stMarkdownContainer"] .caption,
[data-testid="stMarkdownContainer"] [style*="color:#94a3b8"],
[data-testid="stMarkdownContainer"] [style*="color: #94a3b8"],
[data-testid="stMarkdownContainer"] [style*="color:#9ca3af"],
[data-testid="stMarkdownContainer"] [style*="color: #9ca3af"],
[data-testid="stMarkdownContainer"] [style*="color:#cbd5e1"],
[data-testid="stMarkdownContainer"] [style*="color: #cbd5e1"],
[data-testid="stMarkdownContainer"] [style*="color:gray"],
[data-testid="stMarkdownContainer"] [style*="color: gray"],
[data-testid="stMarkdownContainer"] [style*="color:grey"],
[data-testid="stMarkdownContainer"] [style*="color: grey"] {
    color: var(--dv-muted-readable) !important;
}

[data-testid="stAppViewContainer"] [style*="color:#94a3b8"],
[data-testid="stAppViewContainer"] [style*="color: #94a3b8"],
[data-testid="stAppViewContainer"] [style*="color:#9ca3af"],
[data-testid="stAppViewContainer"] [style*="color: #9ca3af"],
[data-testid="stAppViewContainer"] [style*="color:#cbd5e1"],
[data-testid="stAppViewContainer"] [style*="color: #cbd5e1"],
[data-testid="stAppViewContainer"] [style*="color:#64748b"],
[data-testid="stAppViewContainer"] [style*="color: #64748b"],
[data-testid="stAppViewContainer"] [style*="color:#6b7280"],
[data-testid="stAppViewContainer"] [style*="color: #6b7280"],
[data-testid="stAppViewContainer"] [style*="color:gray"],
[data-testid="stAppViewContainer"] [style*="color: gray"],
[data-testid="stAppViewContainer"] [style*="color:grey"],
[data-testid="stAppViewContainer"] [style*="color: grey"] {
    color: var(--dv-muted-readable) !important;
}

[data-testid="stMarkdownContainer"] [style*="border-color:#e2e8f0"],
[data-testid="stMarkdownContainer"] [style*="border-color: #e2e8f0"],
[data-testid="stMarkdownContainer"] [style*="border-color:#cbd5e1"],
[data-testid="stMarkdownContainer"] [style*="border-color: #cbd5e1"],
[data-testid="stMarkdownContainer"] [style*="border-color:#e5e7eb"],
[data-testid="stMarkdownContainer"] [style*="border-color: #e5e7eb"],
[data-testid="stMarkdownContainer"] [style*="border:1px solid #e5e7eb"],
[data-testid="stMarkdownContainer"] [style*="border: 1px solid #e5e7eb"],
[data-testid="stMarkdownContainer"] [style*="border-bottom:1px solid #f3f4f6"],
[data-testid="stMarkdownContainer"] [style*="border-bottom: 1px solid #f3f4f6"],
[data-testid="stMarkdownContainer"] [style*="border-top:1px solid #f3f4f6"],
[data-testid="stMarkdownContainer"] [style*="border-top: 1px solid #f3f4f6"],
hr {
    border-color: var(--dv-line-readable) !important;
}

[data-testid="stAppViewContainer"] [style*="border-color:#e2e8f0"],
[data-testid="stAppViewContainer"] [style*="border-color: #e2e8f0"],
[data-testid="stAppViewContainer"] [style*="border-color:#cbd5e1"],
[data-testid="stAppViewContainer"] [style*="border-color: #cbd5e1"],
[data-testid="stAppViewContainer"] [style*="border-color:#e5e7eb"],
[data-testid="stAppViewContainer"] [style*="border-color: #e5e7eb"],
[data-testid="stAppViewContainer"] [style*="border:1px solid #e5e7eb"],
[data-testid="stAppViewContainer"] [style*="border: 1px solid #e5e7eb"],
[data-testid="stAppViewContainer"] [style*="border-bottom:1px solid #f3f4f6"],
[data-testid="stAppViewContainer"] [style*="border-bottom: 1px solid #f3f4f6"],
[data-testid="stAppViewContainer"] [style*="border-top:1px solid #f3f4f6"],
[data-testid="stAppViewContainer"] [style*="border-top: 1px solid #f3f4f6"] {
    border-color: var(--dv-line-readable) !important;
}

div[data-testid="stCheckbox"],
div[data-testid="stCheckbox"] *,
[data-testid="stWidgetLabel"],
[data-testid="stWidgetLabel"] * {
    opacity: 1 !important;
}

div[data-testid="stCheckbox"] label,
div[data-testid="stCheckbox"] label *,
div[data-testid="stCheckbox"] p,
div[data-testid="stCheckbox"] span,
[data-testid="stWidgetLabel"],
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] span {
    color: var(--dv-muted-readable-strong) !important;
}

div[data-testid="stCheckbox"] [aria-disabled="true"],
div[data-testid="stCheckbox"] [disabled],
div[data-testid="stCheckbox"] [data-disabled="true"] {
    opacity: 1 !important;
    color: var(--dv-muted-readable-strong) !important;
}
</style>
""", unsafe_allow_html=True)

# ── Sidebar: DV ERP logo + user pill ──────────────────────────────────────
st.sidebar.markdown(
    f"""<div style='padding:14px 12px 10px;border-bottom:1px solid #e5e7eb;margin-bottom:8px'>
    <div style='font-size:1.15rem;font-weight:800;color:#111827;letter-spacing:-0.01em;line-height:1.2'>
        DV ERP 👓
    </div>
    <div style='font-size:0.68rem;color:#6b7280;font-weight:500;margin-bottom:8px;letter-spacing:.02em'>
        Optical Business Management System
    </div>
    <div style='border-top:1px solid #f3f4f6;padding-top:8px'>
        <div style='font-size:0.65rem;color:#9ca3af;font-weight:600;text-transform:uppercase;
                    letter-spacing:.07em;margin-bottom:2px'>Logged in as</div>
        <div style='font-size:0.88rem;font-weight:700;color:#111827'>👤 {_username}</div>
        <span style='background:{_role_color};color:#fff;font-size:0.62rem;font-weight:700;
                     padding:2px 8px;border-radius:20px;letter-spacing:.04em'>{_role.upper()}</span>
    </div>
    </div>""",
    unsafe_allow_html=True
)

# ── Build page list ───────────────────────────────────────────────────────
pages = []

def _sidebar_pending_count(table: str, status_col: str = "status") -> int:
    """Small live badge for external order queues; never blocks sidebar."""
    try:
        import time as _time
        _cache_key = f"_sidebar_pending_{table}"
        _ts_key = f"{_cache_key}_ts"
        _now = _time.time()
        if (_now - float(st.session_state.get(_ts_key, 0) or 0)) < 60:
            return int(st.session_state.get(_cache_key, 0) or 0)
        from modules.sql_adapter import run_query
        if table not in ("online_orders", "retailer_orders"):
            return 0
        rows = run_query(f"""
            SELECT COUNT(*) AS n
            FROM {table}
            WHERE UPPER(COALESCE({status_col},'')) NOT IN
                  ('DELIVERED','DISPATCHED','CANCELLED','VOID','CLOSED','COMPLETED')
        """) or []
        n = int((rows[0].get("n") if rows else 0) or 0)
        st.session_state[_cache_key] = n
        st.session_state[_ts_key] = _now
        return n
    except Exception:
        return 0

_online_pending = _sidebar_pending_count("online_orders") if online_admin_ok else 0
_retailer_pending = _sidebar_pending_count("retailer_orders") if ret_orders_ok else 0
_online_label = (
    f"🟢  Online Orders ({_online_pending})"
    if _online_pending > 0 else "🌐  Online Orders"
)
_retailer_label = (
    f"🟢  Retailer Orders ({_retailer_pending})"
    if _retailer_pending > 0 else "🛒  Retailer Orders"
)

if has_role(BILLING, MANAGER, ADMIN):
    pages.append("── PUNCHINGS ──")
    if retail_ok:     pages.append("🛍️  Retail Punching")
    if wholesale_ok:  pages.append("📦  Wholesale Punching")
    if bulk_order_ok: pages.append("⚡  Bulk Order")
    if online_admin_ok: pages.append(_online_label)
    if ret_orders_ok: pages.append(_retailer_label)

if has_role(BILLING, LAB, MANAGER, ADMIN):
    pages.append("── ORDER MANAGEMENT ──")
    pages.append("📋  Orders")
    if backoffice_ok: pages.append("⚙️  Back Office Management")

if has_role(LAB, MANAGER, ADMIN):
    pages.append("── PRODUCTION ──")
    if production_ok: pages.append("🔬  Production")
    if fitter_ok:     pages.append("🔧  Service Management")

if has_role(BILLING, MANAGER, ADMIN):
    pages.append("── ACCOUNT MANAGEMENT ──")
    pages.append("🧾  Challan & Invoice")
    pages.append("📄  Credit & Debit Notes")
    pages.append("💳  Collect Payment")
    pages.append("📚  Registers")
    pages.append("📊  Reports")
    pages.append("🧾  GST Portal")
    # Keep Accounts available for legacy accounting utilities/JV screens.
    pages.append("📒  Accounts")

if has_role(LAB, BILLING, INVENTORY, MANAGER, ADMIN):
    pages.append("── UTILITIES ──")
    pages.append("🔍  Inventory Search")
    pages.append("📋  Inventory Audit")
    pages.append("🖨️  Printer Settings")
    pages.append("👁️  Contact Lens Advisor")
    if wa_queue_ok: pages.append("📬  W/A Queue")
    if wa_bulk_ok: pages.append("📲  Bulk WhatsApp")
    pages.append("📷  Scanner")

if has_role(BILLING, INVENTORY, MANAGER, ADMIN):
    pages.append("── LOADERS ──")
    pages.append("➕  Quick Add")
    pages.append("🕶️  Frame Stock")
    pages.append("🕶️  Scan & Add Frame")
    pages.append("📥  Data Loaders")
    if crm_ok: pages.append("🤝  CRM / Parties")
    if prod_inv_ok: pages.append("🔬  Product & Inventory")

if has_role(LAB, BILLING, MANAGER, ADMIN):
    if dispatch_ok:   pages.append("🚚  Dispatch & Logistics")

if has_role(BILLING, INVENTORY, MANAGER, ADMIN):
    if procurement_ok: pages.append("🛒  Procurement")

if has_role(ADMIN, MANAGER):
    pages.append("── ADMIN ──")
    pages.append("🔍  Discount Audit")
    pages.append("👥  HR & Attendance")
    pages.append("📋  Rejection Report")
    pages.append("🏷️  Label Preview")
    pages.append("🏪  Shop Master")
    if pricing_ok:       pages.append("💲  Pricing Admin")
    if disc_dash_ok:     pages.append("📊  Discount Analytics")
    if price_sug_ok:     pages.append("🤖  AI Price Suggestions")
    if has_role(MANAGER, ADMIN): pages.append("📝  Edit Log")
    pages.append("👥  User Management")
    if system_health_ok: pages.append("❤️  System Health")
    pages.append("🔢  Order Numbers")
    pages.append("🔀  Patient Merge")

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

if has_role(ADMIN):
    pages.append("👑  Owner Dashboard")

if has_role(VIEWER) and not has_role(BILLING, LAB, INVENTORY, MANAGER, ADMIN):
    if analytics_ok: pages.append("📊  Import Analytics")

def _sidebar_sort_key(label: str) -> str:
    txt = str(label or "")
    txt = __import__("re").sub(r"^[^\wA-Za-z]+", "", txt).strip()
    txt = __import__("re").sub(r"^\(\d+\)\s*", "", txt).strip()
    return txt.lower()


def _sort_sidebar_sections(raw_pages):
    sorted_pages = []
    section = None
    items = []

    def _flush():
        if section:
            sorted_pages.append(section)
        sorted_pages.extend(sorted(items, key=_sidebar_sort_key))

    for item in raw_pages:
        if str(item).startswith("──"):
            if section is not None or items:
                _flush()
            section = item
            items = []
        else:
            items.append(item)
    if section is not None or items:
        _flush()
    return sorted_pages


pages = _sort_sidebar_sections(pages)

if not pages:
    st.sidebar.warning("No modules available for your role.")
    st.error("⛔ No modules available. Contact admin.")
    st.stop()

# ── Write live sidebar registry for Permission Designer ───────────────────
# Permission Designer reads this so its sidebar preview always matches
# exactly what is shown here — same order, same labels, same sections.
# Adding/renaming/reordering a page here automatically updates the Designer.
try:
    from modules.security.page_registry import LABEL_TO_KEY as _L2K
    _live_registry = []
    _cur_sec = None
    for _p in pages:
        if _p.startswith("──"):
            _cur_sec = _p.strip("── ").strip()
        else:
            _live_registry.append({
                "label":   _p.strip(),
                "section": _cur_sec or "GENERAL",
                "key":     _L2K.get(_p.strip(), ""),
            })
    st.session_state["_live_sidebar_registry"] = _live_registry
except Exception:
    pass  # non-critical — designer falls back to page_registry static list

# ── Render sidebar with section headers as labels, not buttons ────────────
_SECTION_CSS = (
    "<style>"
    ".sidebar-section-hdr{"
    "  font-size:10px;font-weight:700;letter-spacing:.08em;"
    "  color:#94a3b8;padding:10px 4px 4px;text-transform:uppercase;"
    "  border-top:0.5px solid #334155;margin-top:4px"
    "}"
    "</style>"
)
st.sidebar.markdown(_SECTION_CSS, unsafe_allow_html=True)

# Build sections dict preserving order
_sections = []
_cur_section = None
_cur_items   = []
for p in pages:
    if p.startswith("──"):
        if _cur_items or _cur_section:
            _sections.append((_cur_section, _cur_items))
        _cur_section = p.strip("── ").strip()
        _cur_items   = []
    else:
        _cur_items.append(p)
if _cur_items or _cur_section:
    _sections.append((_cur_section, _cur_items))

# Render each section
_all_pages_flat = [p for p in pages if not p.startswith("──")]
for _sec_name, _sec_pages in _sections:
    if _sec_name:
        st.sidebar.markdown(
            f"<div class='sidebar-section-hdr'>{_sec_name}</div>",
            unsafe_allow_html=True
        )
    for _pg in _sec_pages:
        _is_selected = (st.session_state.get("_sidebar_page","") == _pg or
                        (_pg == _all_pages_flat[0] and not st.session_state.get("_sidebar_page","")))
        if st.sidebar.button(
            _pg,
            key=f"nav_{_pg}",
            width='stretch',
            type="primary" if _is_selected else "secondary",
        ):
            st.session_state["_sidebar_page"] = _pg
            # Bump a click-counter for Retail so re-entry always resets
            if _pg in ("Retail Order", "🛍️  Retail Order"):
                _prev = st.session_state.get("_retail_entry_count", 0)
                st.session_state["_retail_entry_count"] = _prev + 1
            st.rerun()

# ── Default landing page ──────────────────────────────────────────
# On first load _sidebar_page is not set.
# Priority: Backoffice → Production → Retail → first real page (never a section header)
if "_sidebar_page" not in st.session_state:
    def _first_matching(keywords):
        for _kw in keywords:
            _match = next((p for p in _all_pages_flat if _kw in p.lower()), None)
            if _match:
                return _match
        return None

    _bo_default = (
        _first_matching(["back office management", "backoffice"])
        or _first_matching(["production", "orders", "retail", "wholesale"])
        or (_all_pages_flat[0] if _all_pages_flat else "")
    )
    st.session_state["_sidebar_page"] = _bo_default

selected_page = st.session_state.get("_sidebar_page", _all_pages_flat[0] if _all_pages_flat else "")

# ── Handle jump navigation from CRM / other modules ──────────────────────────
# Modules like CRM set global_nav_target + party filter keys, then rerun.
# We consume the target here before rendering so the right page opens.
_nav_target = st.session_state.pop("global_nav_target", None)
if _nav_target:
    def _find_page(*keywords):
        for kw in keywords:
            m = next((p for p in _all_pages_flat if kw.lower() in p.lower()), None)
            if m:
                return m
        return None
    if _nav_target == "backoffice":
        _dest = _find_page("back office management", "backoffice", "back office")
    elif _nav_target == "billing":
        _dest = _find_page("challan & invoice", "billing hub", "billing")
    elif _nav_target == "crm":
        _dest = _find_page("crm", "parties")
    elif _nav_target == "production":
        _dest = _find_page("production")
    else:
        _dest = _find_page(_nav_target)
    if _dest:
        st.session_state["_sidebar_page"] = _dest
        selected_page = _dest

# ── Strip emoji prefix to get router key ─────────────────────────────────
_PAGE_MAP = {
    "🛍️  Retail Order":        "Retail Order",
    "🛍️  Retail Punching":     "Retail Order",
    "📦  Wholesale Order":      "Wholesale Order",
    "📦  Wholesale Punching":   "Wholesale Order",
    "📋  Orders":               "📋 Orders",
    "⚙️  Backoffice":           "Backoffice",
    "⚙️  Back Office Management": "Backoffice",
    "🔬  Production":           "Production",
    "🚚  Dispatch & Logistics":  "Dispatch",
    "🔧  Service Management":   "Service Management",
    "📝  Edit Log":             "Edit Log",
    "🤝  CRM / Parties":        "CRM",
    "🛒  Procurement":          "Procurement",
    "🔬  Product & Inventory":  "Product & Inventory",
    "⚡  Bulk Order":            "Bulk Order",
    "📬  WA Queue":             "WhatsApp Queue",
    "📬  W/A Queue":            "WhatsApp Queue",
    "📲  Bulk WhatsApp":         "Bulk WhatsApp",
    "🧾  Challan & Invoice":    "Challan & Invoice Dashboard",
    "📄  Credit & Debit Notes": "Credit & Debit Notes",
    "💲  Pricing Admin":        "Pricing Admin",
    "📊  Discount Analytics":   "Discount Analytics",
    "🤖  AI Price Suggestions": "AI Price Suggestions",
    "📥  Data Loader":          "Data Loader",
    "📥  Data Loaders":         "Data Loader",
    "🏪  Shop Master":          "Shop Master",
    "👥  User Management":      "User Management",
    "🔀  Patient Merge":        "Patient Merge",
    "❤️  System Health":        "System Health",
    "📊  Import Analytics":     "Import Analytics",
    "🩺  Import Health":        "Import Health",
    "🔧  Schema Evolution":     "Schema Evolution",
    "↩️  Import Rollback":      "Import Rollback",
    "🏰  Control Tower":        "Control Tower",
    "👑  Owner Dashboard":      "Owner Dashboard",
    "🔍  Inventory Search":     "Inventory Search",
    "📋  Inventory Audit":      "Inventory Audit",
    "🖨️  Printer Settings":     "Printer Settings",
    "➕  Quick Add":            "Quick Add",
    "🕶️  Scan & Add Frame":     "Scan Frame",
    "📷  Scanner":              "Scanner",
    "🏷️  Label Preview":        "Label Preview",
    "🕶️  Frame Stock":          "Frame Loader",
    "📊  Reports":              "Reports",
    "📚  Registers":            "Registers",
    "🧾  GST Portal":           "GST Portal",
    "🔍  Discount Audit":       "Discount Audit",
    "📒  Accounts":             "Accounts",
    "👥  HR & Attendance":      "HR",
    "💳  Collect Payment":      "Collect Payment",
    "👁️  CL Advisor":           "👁️  CL Advisor",
    "👁️  Contact Lens Advisor": "👁️  CL Advisor",
    "🔢  Order Numbers":         "🔢  Order Numbers",
    "📋  Rejection Report":       "📋  Rejection Report",
    "🌐  Online Orders":         "Online Orders",
    "🛒  Retailer Orders":       "Retailer Orders",
    _online_label:               "Online Orders",
    _retailer_label:             "Retailer Orders",
}
selected_page = _PAGE_MAP.get(selected_page, selected_page)

# ── Logout ────────────────────────────────────────────────────────────────
st.sidebar.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

# ==================================================
# 3F — REAL-TIME PRICING ALERT ENGINE
# Queries order_lines for recent margin/discount issues.
# Runs at most once per 5 min (cached in session_state).
# Never blocks page load — silent on any failure.
# ==================================================
try:
    import time as _alert_time
    _alert_cache_key  = "_pricing_alerts_cache"
    _alert_ts_key     = "_pricing_alerts_ts"
    _ALERT_TTL        = 300  # 5 minutes

    _now_ts  = _alert_time.time()
    _last_ts = st.session_state.get(_alert_ts_key, 0)

    if (_now_ts - _last_ts) > _ALERT_TTL:
        from modules.sql_adapter import run_query as _aq
        _alerts = []

        # Signal 1: margin hard-stops in last 24h
        _hs = _aq("""
            SELECT COUNT(*) AS n
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE ol.margin_status = 'hard_stop'
              AND o.created_at >= NOW() - INTERVAL '24 hours'
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """) or [{}]
        _hs_count = int((_hs[0].get("n") or 0))
        if _hs_count > 0:
            _alerts.append(f"🛑 {_hs_count} margin hard-stop(s) today")

        # Signal 2: discount spike — today vs 7-day average
        _spike = _aq("""
            SELECT
                COALESCE(AVG(CASE WHEN DATE(o.created_at) = CURRENT_DATE
                    THEN ol.discount_percent END), 0)       AS today_avg,
                COALESCE(AVG(ol.discount_percent), 0)       AS week_avg
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE o.created_at >= NOW() - INTERVAL '7 days'
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.discount_percent, 0) > 0
        """) or [{}]
        _today_avg = float(_spike[0].get("today_avg") or 0)
        _week_avg  = float(_spike[0].get("week_avg") or 0)
        if _week_avg > 0 and _today_avg > _week_avg * 1.5:
            _alerts.append(
                f"⚠️ Discount spike: today {_today_avg:.1f}% "
                f"vs 7-day avg {_week_avg:.1f}%"
            )

        # Signal 3: rules with very high discount fired today
        _high = _aq("""
            SELECT COUNT(*) AS n
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE ol.discount_percent > 30
              AND DATE(o.created_at) = CURRENT_DATE
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """) or [{}]
        _high_count = int((_high[0].get("n") or 0))
        if _high_count > 0:
            _alerts.append(f"🔴 {_high_count} line(s) with >30% discount today")

        st.session_state[_alert_cache_key] = _alerts
        st.session_state[_alert_ts_key]    = _now_ts

    _cached_alerts = st.session_state.get(_alert_cache_key, [])
    if _cached_alerts:
        with st.sidebar.expander(
            f"🚨 Pricing Alerts ({len(_cached_alerts)})", expanded=True
        ):
            for _a in _cached_alerts:
                st.sidebar.warning(_a)
            st.sidebar.caption("→ See 📊 Discount Analytics for details")
except Exception:
    pass  # never block sidebar on alert failure

if st.sidebar.button("🚪  Logout", width='stretch'):
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
        ("Service Management", fitter_ok),
        ("Dispatch",          dispatch_ok),
        ("Procurement",      procurement_ok),
        ("Product Inventory",prod_inv_ok),
        ("CRM",              crm_ok),
        ("Pricing Admin",    pricing_ok),
        ("Import Analytics", analytics_ok),
        ("Import Health",    health_ok),
        ("Control Tower",    founder_ok),
        ("System Health",    system_health_ok),
        ("Retailer Orders",  ret_orders_ok),
        ("Online Store",     online_store_ok),
        ("Online Admin",     online_admin_ok),
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
    try:
        from modules.core.system_observer import get_observer_summary
        _obs_sum = get_observer_summary()
        _slow_n = sum((_obs_sum.get("slow_pages") or {}).values())
        _err_n = sum((_obs_sum.get("error_pages") or {}).values())
        st.write(f"- Observer: 🧠 {_slow_n} slow · {_err_n} error")
        if _slow_n or _err_n:
            with st.expander("Observer Notes", expanded=False):
                for _ev in (_obs_sum.get("events") or [])[-8:]:
                    _lbl = _ev.get("event_type", "")
                    _pg = _ev.get("page", "")
                    _sec = _ev.get("elapsed_sec", 0)
                    _msg = _ev.get("relief_action") or _ev.get("message") or ""
                    st.caption(f"{_ev.get('at')} · {_lbl} · {_pg} · {_sec}s {_msg}")
    except Exception:
        st.write("- Observer: ⚠️")
    st.caption("Full health → Import Health dashboard")

# ==================================================
# DB MIGRATION STATUS (sidebar panel)
# ==================================================
with st.sidebar.expander("🗄️ DB Migrations", expanded=False):
    _mr = st.session_state.get("_migration_result")
    if not _mr:
        st.caption("Migrations run once per session on startup.")
        if st.button("▶️ Run Now", key="sb_run_mig", width='stretch'):
            try:
                from modules.loaders.migrations import run_all_migrations, MIGRATIONS
                _mr2 = run_all_migrations(silent=True)
                st.session_state["_migration_result"] = {
                    "applied":       _mr2.get("applied", []),
                    "skipped":       _mr2.get("skipped", []),
                    "errors":        _mr2.get("errors", []),
                    "total_defined": len(MIGRATIONS),
                    "ran_at": __import__("datetime").datetime.now().strftime("%d %b %Y %H:%M:%S"),
                }
                st.rerun()
            except Exception as _me:
                st.error(str(_me))
    else:
        applied = _mr.get("applied", [])
        skipped = _mr.get("skipped", [])
        errors  = _mr.get("errors", [])
        total   = _mr.get("total_defined", 0)
        ran_at  = _mr.get("ran_at", "")

        # Summary row
        _sc1, _sc2, _sc3 = st.columns(3)
        _sc1.metric("Added",    len(applied))
        _sc2.metric("Existed",  len(skipped))
        _sc3.metric("Errors",   len(errors))
        st.caption(f"Last run: {ran_at}  ·  {total} total defined")

        if applied:
            st.markdown(
                "<span style='font-size:0.72rem;font-weight:700;color:#16a34a;"
                "text-transform:uppercase'>✅ Newly Added</span>",
                unsafe_allow_html=True
            )
            for col_id in applied:
                table, col = col_id.split(".", 1) if "." in col_id else ("?", col_id)
                st.markdown(
                    f"<div style='font-size:0.75rem;color:#166534;padding:1px 0'>"
                    f"<b>{table}</b> · {col}</div>",
                    unsafe_allow_html=True
                )

        if skipped:
            st.markdown(
                "<span style='font-size:0.72rem;font-weight:700;color:#64748b;"
                "text-transform:uppercase'>⏭️ Already Existed</span>",
                unsafe_allow_html=True
            )
            for col_id in skipped:
                table, col = col_id.split(".", 1) if "." in col_id else ("?", col_id)
                st.markdown(
                    f"<div style='font-size:0.72rem;color:#94a3b8;padding:1px 0'>"
                    f"{table} · {col}</div>",
                    unsafe_allow_html=True
                )

        if errors:
            st.markdown(
                "<span style='font-size:0.72rem;font-weight:700;color:#dc2626;"
                "text-transform:uppercase'>❌ Errors</span>",
                unsafe_allow_html=True
            )
            for e in errors:
                st.error(e)

        if st.button("🔄 Re-run Migrations", key="sb_rerun_mig", width='stretch'):
            st.session_state.pop("_migration_result", None)
            st.session_state.pop("schema_synced", None)
            st.rerun()

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
        # Clear tracking vars that might block operations
        st.session_state.pop("_retail_finalized_eyes", None)
        st.session_state.pop("retail_pending_eyes", None)
        # Retail restores cart from these private snapshots on render. When
        # switching modules, they must be cleared or old retail lines return
        # without a selected patient.
        st.session_state.pop("_persistent_cart", None)
        st.session_state.pop("_crash_snapshot", None)
        # Also clear post-save / receipt / edit state so new page starts fresh
        for _clr in (
            "_receipt_snapshot", "_last_receipt_key",
            "_post_save_data", "_post_save_ws_data",
            "_editing_order_id", "_editing_order_no",
            "_edit_existing_advance",
            # Backoffice order hold — clear so next visit starts fresh
            "bo_selected_order_id", "bo_view_mode",
            "bo_jump_to_billing", "bo_orders_loaded",
            "bo_assignments", "bo_assignments_locked",
        ):
            st.session_state.pop(_clr, None)
        st.session_state.active_module = new_page
        log_time(f"Switched to {new_page}")
        st.rerun()   # Only fires when page actually changes

handle_page_switch(selected_page)

# ── ERP ROUTING: explicit mode handler (replaces inference logic) ────────────
# Set by order_edit_view.py with _erp_mode = CONSULT_EDIT | BILL_NEW | BILL_EDIT
_erp_mode = st.session_state.get("_erp_mode","")
if _erp_mode and selected_page in ("Retail Order","Wholesale Order"):

    _erp_rx_r    = st.session_state.get("_erp_rx_r", {})
    _erp_rx_l    = st.session_state.get("_erp_rx_l", {})
    _erp_pid     = st.session_state.get("_erp_patient_id","")
    _erp_pname   = st.session_state.get("_erp_patient_name","")
    _erp_pmob    = st.session_state.get("_erp_patient_mob","")
    _erp_fee     = float(st.session_state.get("_erp_consult_fee",0) or 0)
    _erp_cart    = st.session_state.get("_erp_cart_lines",[]) or []
    _erp_vid     = st.session_state.get("_erp_visit_id","")
    _erp_oid     = st.session_state.get("_erp_order_id","")
    _erp_ono     = st.session_state.get("_erp_order_no","")

    # Wipe stale retail state first
    for _k in list(st.session_state.keys()):
        if (_k.startswith("retail_") or _k.startswith("ps_") or
            _k.startswith("new_sph_") or _k.startswith("new_cyl_") or
            _k.startswith("new_axis_") or _k.startswith("new_add_")):
            del st.session_state[_k]
    st.session_state.pop("_persistent_cart", None)
    st.session_state.pop("rx_reset_counter", None)
    st.session_state.pop("retail_visit_mode", None)
    try:
        from modules.utils.submit_guard import clear_all_locks
        clear_all_locks()
    except Exception: pass

    # Set patient
    st.session_state["retail_patient_name"]   = _erp_pname
    st.session_state["retail_patient_mobile"] = _erp_pmob
    st.session_state["retail_patient_id"]     = _erp_pid if _erp_pid else None

    # Set powers into both old+new slots
    import math as _em
    def _clean_rx(v):
        try:
            f = float(v or 0)
            return None if (_em.isnan(f) or _em.isinf(f)) else f
        except Exception as _e:
            logging.getLogger(__name__).warning("Suppressed error: %s", _e)
            return None
    _rx_r_clean = {k: _clean_rx(v) for k,v in _erp_rx_r.items()}
    _rx_l_clean = {k: _clean_rx(v) for k,v in _erp_rx_l.items()}
    st.session_state["retail_old_rx_r"] = _rx_r_clean
    st.session_state["retail_old_rx_l"] = _rx_l_clean
    st.session_state["retail_new_rx_r"] = _rx_r_clean
    st.session_state["retail_new_rx_l"] = _rx_l_clean

    if _erp_mode == "CONSULT_EDIT":
        # Open Consultation tab, UPDATE existing visit on save
        st.session_state["_visit_mode_default"]        = 1   # Consultation Only
        st.session_state["_erp_visit_id"]              = _erp_vid   # kept for _do_save
        st.session_state["retail_order_lines"]         = []
        st.session_state["_force_consultation_tab"]    = True
        st.session_state.pop("_force_full_billing_mode", None)
        st.session_state.pop("retail_visit_mode", None)
        st.session_state.pop("last_confirmed_order", None)
        if not _erp_pid:
            _erp_pmob = ""
            st.session_state["retail_patient_mobile"] = ""
            st.session_state["_erp_patient_mob"] = ""
            st.session_state.pop("consult_wa_mobile_display", None)
        elif _erp_pmob:
            st.session_state["consult_wa_mobile_display"] = _erp_pmob
        else:
            st.session_state.pop("consult_wa_mobile_display", None)
        # Older consultation rows can have no linked party_id. Still open the
        # consultation editor with the saved name/mobile instead of falling into
        # a blank Retail Punching screen.
        if not _erp_pid and _erp_pname:
            import uuid as _erp_temp_uuid
            st.session_state["retail_patient_id"] = f"TEMP-{str(_erp_temp_uuid.uuid4())[:8].upper()}"
        # Mark as edit so _do_save in consultation.py uses UPDATE
        st.session_state["_editing_consult_order_id"]  = _erp_oid

    elif _erp_mode == "BILL_NEW":
        # Open Full Billing tab, new order, consultation fee pre-loaded
        st.session_state["_visit_mode_default"]         = 0   # Full Billing
        st.session_state["_force_full_billing_mode"]    = True
        st.session_state["retail_order_lines"]          = _erp_cart
        _consult_oid_erp = st.session_state.get("_erp_consult_oid","")
        if _consult_oid_erp:
            st.session_state["retail_case_no"]              = _consult_oid_erp
            st.session_state["_retail_consult_source_id"]   = _consult_oid_erp
        if _erp_cart:
            st.session_state["_consult_fee_lines"] = [
                l for l in _erp_cart
                if str(l.get("eye_side","")).upper() in ("SERVICE","S")
                or bool(l.get("is_service_line"))
            ]

    elif _erp_mode == "BILL_EDIT":
        # Open Full Billing tab, edit existing order
        st.session_state["_visit_mode_default"] = 0   # Full Billing
        st.session_state["_force_full_billing_mode"] = True
        # _order_edit_prefill already set by order_edit_view — will be picked up below

    # Clean up all _erp_* keys
    for _k in list(st.session_state.keys()):
        if _k.startswith("_erp_") and _k not in ("_erp_visit_id","_editing_consult_order_id"):
            st.session_state.pop(_k, None)
    st.session_state.pop("_erp_mode", None)
    for _k in list(st.session_state.keys()):
        if _k.startswith("_confirmed_cart_"):
            st.session_state.pop(_k, None)


# ── Apply consultation prefill AFTER page switch (survives retail_* wipe) ──
_cp = st.session_state.get("_consult_prefill")
if _cp and selected_page == "Retail Order":
    # Clear previous patient's receipt — new patient coming in
    st.session_state.pop("_receipt_snapshot", None)
    st.session_state.pop("_last_receipt_key", None)
    for _k in list(st.session_state.keys()):
        if _k.startswith("_confirmed_cart_"):
            st.session_state.pop(_k, None)

# ── Apply order edit prefill (from Edit Full Order button) ──────────────
_ep = st.session_state.get("_order_edit_prefill")
if _ep and selected_page in ("Retail Order", "Wholesale Order"):
    st.session_state.pop("_order_edit_prefill", None)
    st.session_state.pop("_receipt_snapshot", None)
    st.session_state.pop("_last_receipt_key", None)
    for _k in list(st.session_state.keys()):
        if _k.startswith("_confirmed_cart_"):
            st.session_state.pop(_k, None)
    st.session_state.pop("_consult_prefill", None)

    # ── HARD WIPE first — clears any stale retail state ──────────────
    for _k in list(st.session_state.keys()):
        if (
            _k.startswith("retail_")
            or _k.startswith("ps_")
            or _k.startswith("retail_qe_")
            or _k.startswith("new_sph_")
            or _k.startswith("new_cyl_")
            or _k.startswith("new_axis_")
            or _k.startswith("new_add_")
        ):
            del st.session_state[_k]
    st.session_state.pop("_persistent_cart", None)
    st.session_state.pop("_crash_snapshot", None)
    st.session_state.pop("rx_reset_counter", None)
    st.session_state.pop("reset_product_selector", None)
    try:
        from modules.utils.submit_guard import clear_all_locks
        clear_all_locks()
    except Exception:
        pass

    # ── Apply prefill AFTER wipe so values are not wiped ─────────────
    _ep_rx = _ep.get("rx",{})
    _ep_order_type = str(_ep.get("order_type","RETAIL")).upper()
    st.session_state["retail_patient_id"]            = _ep.get("patient_id")
    st.session_state["retail_patient_name"]          = _ep.get("patient_name","")
    st.session_state["retail_patient_mobile"]        = _ep.get("patient_mobile","")
    # For WHOLESALE edit: keep retail_new_rx empty so QE flow is NOT triggered.
    # But DO inject the per-eye powers into wh_ keys so power widgets show saved values.
    # For RETAIL edit: populate from order RX as usual.
    if _ep_order_type == "WHOLESALE":
        # Delete stale wh_ widget keys — Streamlit stores widget keys in session_state
        # permanently after first render. If we also pass value= to those keys, it
        # triggers a conflict warning and an infinite rerun loop. Deleting them first
        # lets the widget render fresh from value= without conflict.
        for _wh_eye in ("R", "L"):
            for _wh_k in (f"wh_sph_{_wh_eye}", f"wh_cyl_{_wh_eye}",
                          f"wh_axis_{_wh_eye}", f"wh_add_{_wh_eye}"):
                st.session_state.pop(_wh_k, None)
        # Set powers into retail_new_rx_r/l — the power widgets read these
        # via value=float(new_rx.get("sph") or 0.0) so they show saved powers.
        _rx_r_ep = _ep.get("rx_r", {})
        _rx_l_ep = _ep.get("rx_l", {})
        st.session_state["retail_new_rx_r"] = {
            "sph":  float(_rx_r_ep.get("sph") or 0.0),
            "cyl":  float(_rx_r_ep.get("cyl") or 0.0),
            "axis": int(_rx_r_ep.get("axis") or 0),
            "add":  float(_rx_r_ep.get("add") or 0.0),
        } if _rx_r_ep else {}
        st.session_state["retail_new_rx_l"] = {
            "sph":  float(_rx_l_ep.get("sph") or 0.0),
            "cyl":  float(_rx_l_ep.get("cyl") or 0.0),
            "axis": int(_rx_l_ep.get("axis") or 0),
            "add":  float(_rx_l_ep.get("add") or 0.0),
        } if _rx_l_ep else {}
    else:
        _rx_r_ep = _ep.get("rx_r", {}) or {}
        _rx_l_ep = _ep.get("rx_l", {}) or {}
        st.session_state["retail_new_rx_r"] = {
            "sph":  _rx_r_ep.get("sph", _ep_rx.get("sph_r", 0)),
            "cyl":  _rx_r_ep.get("cyl", _ep_rx.get("cyl_r", 0)),
            "axis": _rx_r_ep.get("axis", _ep_rx.get("ax_r", 0)),
            "add":  _rx_r_ep.get("add", _ep_rx.get("add_r", 0)),
        }
        st.session_state["retail_new_rx_l"] = {
            "sph":  _rx_l_ep.get("sph", _ep_rx.get("sph_l", 0)),
            "cyl":  _rx_l_ep.get("cyl", _ep_rx.get("cyl_l", 0)),
            "axis": _rx_l_ep.get("axis", _ep_rx.get("ax_l", 0)),
            "add":  _rx_l_ep.get("add", _ep_rx.get("add_l", 0)),
        }
    st.session_state["retail_order_lines"]           = _ep.get("cart",[])
    st.session_state["_visit_mode_default"]          = 0
    st.session_state["_editing_order_id"]            = _ep.get("order_id","")
    st.session_state["_editing_order_no"]            = _ep.get("order_no","")
    # Pre-load existing advance so UI shows it
    _ep_adv = float(_ep.get("existing_advance", 0) or 0)
    st.session_state["_edit_existing_advance"]       = _ep_adv
    st.session_state["retail_advance_mode"]          = _ep.get("advance_mode","CASH")
    # Always start new-advance input at 0 — existing_adv shown separately in UI
    st.session_state["retail_advance_amount"]        = 0.0
    st.session_state["retail_collect_advance"]       = False
    # Resolve patient UUID
    try:
        from modules.sql_adapter import run_query as _rq_ep
        _pid_r = _rq_ep("SELECT id::text AS pid FROM patients WHERE master_name ILIKE %s LIMIT 1",
                        (_ep.get("patient_name",""),)) or []
        if _pid_r:
            st.session_state["retail_patient_id"] = _pid_r[0]["pid"]
    except Exception:
        pass

    # ── Apply patient ─────────────────────────────────────────────────────
if _cp and selected_page == "Retail Order":
    _cp_include_fee = bool(_cp.get("include_consult_fee"))
    # ── Apply patient ─────────────────────────────────────────────────────
    _cp_name = _cp.get("patient_name", "")
    st.session_state["retail_patient_name"]   = _cp_name
    st.session_state["retail_patient_mobile"] = _cp.get("patient_mobile", "")

    # ── Apply Rx to BOTH old and new slots so power section shows it ──────
    # Sanitize values: convert NaN/None/'NaN' to None so number_inputs
    # never receive float('nan') as their default value.
    def _safe_prefill_rx(d: dict) -> dict:
        import math
        out = {}
        for k, v in (d or {}).items():
            if v is None:
                out[k] = None
                continue
            try:
                f = float(v)
                out[k] = None if (math.isnan(f) or math.isinf(f)) else f
            except (TypeError, ValueError):
                out[k] = None
        return out

    _rx_r = _safe_prefill_rx(_cp.get("rx_r", {}))
    _rx_l = _safe_prefill_rx(_cp.get("rx_l", {}))
    st.session_state["retail_old_rx_r"] = _rx_r
    st.session_state["retail_old_rx_l"] = _rx_l
    st.session_state["retail_new_rx_r"] = _rx_r
    st.session_state["retail_new_rx_l"] = _rx_l

    # ── Apply cart lines ───────────────────────────────────────────────────
    if _cp_include_fee and _cp.get("order_lines"):
        st.session_state["retail_order_lines"] = _cp["order_lines"]
    else:
        st.session_state["retail_order_lines"] = []

    st.session_state["_visit_mode_default"] = 0  # Full Billing
    st.session_state["_force_full_billing_mode"] = True
    # Delete widget key so radio re-renders fresh with index=0 (Full Billing).
    # Without this the widget keeps its previous value (Consultation Only).
    st.session_state.pop("retail_visit_mode", None)

    # ── Store consult_order_id as case_no so customer_order_no in saved order
    #    points to the consultation UUID — enables re-billing prevention check ──
    _consult_oid = _cp.get("consult_order_id", "")
    if _consult_oid:
        st.session_state["retail_case_no"] = _consult_oid
        st.session_state["_retail_consult_source_id"] = _consult_oid
        # ── Persist consultation fee separately so it survives ALL cart resets ──
        # Stored as a plain dict — not part of retail_order_lines — so no reset
        # can wipe it. The cart rendering reads this and shows an "Add Fee" button.
        _fee_lines = _cp.get("order_lines") or []
        _fee_amt = float(_cp.get("consult_fee") or 0)
        _consult_paid_amt = float(_cp.get("consult_paid_amount") or 0)
        _consult_is_paid = bool(_cp.get("consult_paid")) or _consult_paid_amt > 0
        if not _cp_include_fee:
            st.session_state.pop("_consult_fee_lines", None)
            st.session_state.pop("_consult_paid_advance_amount", None)
            st.session_state.pop("_consult_paid_advance_mode", None)
            st.session_state.pop("_consult_paid_advance_ref", None)
        elif _consult_is_paid:
            st.session_state["_consult_paid_advance_amount"] = _consult_paid_amt or _fee_amt
            st.session_state["_consult_paid_advance_mode"] = _cp.get("payment_mode", "CASH")
            st.session_state.pop("_consult_fee_lines", None)
        if not _fee_amt and _fee_lines:
            _fee_amt = sum(float(l.get("total_price",0)) for l in _fee_lines
                          if str(l.get("eye_side","")).upper() in ("SERVICE","S"))
        if _cp_include_fee and not _consult_is_paid and (_fee_amt > 0 or _fee_lines):
            import uuid as _uuid_cfe_app, datetime as _dt_cfe_app
            if not _fee_lines:
                # Build fee line from scratch if not in order_lines
                try:
                    from modules.sql_adapter import run_query as _rq_fp
                    _fp_r = (_rq_fp(
                        "SELECT id::text, product_name FROM products "
                        "WHERE LOWER(product_name) LIKE '%consultation%' "
                        "AND COALESCE(is_active,true)=true ORDER BY created_at LIMIT 1"
                    ) or [{}])[0]
                except Exception:
                    _fp_r = {}
                _fee_lines = [{
                    "line_id":            str(_uuid_cfe_app.uuid4()),
                    "provisional_order_id": None,
                    "product_id":         _fp_r.get("id",""),
                    "product_name":       _fp_r.get("product_name","Consultation Fee"),
                    "brand":              "Service", "main_group": "Services",
                    "eye_side":           "SERVICE",
                    "sph": None, "cyl": None, "axis": None, "add_power": None,
                    "lens_params": {}, "boxing_params": {},
                    "requested_qty": 1, "billing_qty": 1, "order_qty": 0,
                    "display_qty": "1 SERVICE", "batch_allocation": [],
                    "unit_price": _fee_amt, "total_price": _fee_amt,
                    "gst_percent": 0.0, "gst_amount": 0.0,
                    "is_gst_exempt": True,
                    "is_service_line": True, "status": "Complete",
                    "created_at": _dt_cfe_app.datetime.now().isoformat(),
                }]
            # Store persistently — survives all retail_* resets
            st.session_state["_consult_fee_lines"] = _fee_lines

    # Clear stale confirmed-cart fingerprints so the new order can be submitted
    # (fingerprints from the previous session's provisional ID must not block this)
    for _k in list(st.session_state.keys()):
        if _k.startswith("_confirmed_cart_"):
            st.session_state.pop(_k, None)

    # ── Resolve patient_id: direct UUID first, DB name lookup as fallback ─
    _pid_direct = _cp.get("patient_id", "")
    if _pid_direct and len(str(_pid_direct)) > 10:
        st.session_state["retail_patient_id"] = str(_pid_direct)
    else:
        try:
            from modules.sql_adapter import run_query as _rq_cp
            _pr = _rq_cp(
                "SELECT id::text AS pid FROM patients WHERE master_name ILIKE %s LIMIT 1",
                (_cp_name,)
            ) or []
            st.session_state["retail_patient_id"] = _pr[0]["pid"] if _pr else None
        except Exception:
            st.session_state["retail_patient_id"] = None

    # ── Store consultation fee lines in a durable protected key ──────────
    # retail_order_lines will be wiped by product selection resets.
    # _consult_fee_lines survives until the order is saved.
    _cp_svc = [l for l in (_cp.get("order_lines") or [])
               if _cp_include_fee
               and (str(l.get("eye_side","")).upper() in ("SERVICE","S")
                    or bool(l.get("is_service_line")))]
    if _cp_svc and not bool(_cp.get("consult_paid")):
        st.session_state["_consult_fee_lines"] = _cp_svc

    # Pop _consult_prefill so it doesn't re-apply on every render
    st.session_state.pop("_consult_prefill", None)


# ==================================================
# SAFE RENDER HELPER
# ==================================================

def safe_render(fn, name: str):
    import time as _safe_time
    _t0 = _safe_time.perf_counter()
    try:
        from modules.core.system_observer import start_perf_trace
        start_perf_trace(name)
    except Exception:
        pass
    try:
        try:
            _db_block = st.session_state.get("_last_db_write_failure")
            if _db_block:
                _age = _safe_time.time() - float(_db_block.get("at") or 0)
                if _age < 120:
                    _healed = _db_block.get("healed") or []
                    st.error(
                        "**Database save blocked / rolled back**\n\n"
                        f"Page: {name}\n\n"
                        "The last button write did not commit. The internal auditor saved the cause."
                    )
                    if _healed:
                        st.info(f"Self-heal applied: {', '.join(_healed)}")
                    if _db_block.get("issue_note"):
                        st.caption(f"Issue note: {_db_block.get('issue_note')}")
                else:
                    st.session_state.pop("_last_db_write_failure", None)
        except Exception:
            pass
        fn()
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"[{name}] Render failed: {e}\n{tb}")
        try:
            from modules.core.system_observer import record_page_error
            _obs_err = record_page_error(name, e)
            if _obs_err.get("relief_action"):
                st.info(f"Observer relief: {_obs_err['relief_action']}")
        except Exception:
            pass
        try:
            from modules.core.error_logger import log_error
            log_error(e, context=f"render:{name}", payload={"page": name})
        except Exception:
            pass
        try:
            from modules.core.operator_alerts import render_operator_alert
            render_operator_alert(e, context=name, show_traceback=True)
        except Exception:
            st.error(f"❌ {name} error: {e}")
            st.code(tb)
    finally:
        try:
            _elapsed = _safe_time.perf_counter() - _t0
            _threshold = float(st.session_state.get("_slow_render_threshold_sec", 8.0))
            from modules.core.system_observer import finish_perf_trace, record_page_render
            _perf = finish_perf_trace(_elapsed)
            _obs = record_page_render(name, _elapsed, _threshold, perf=_perf)
            if _obs.get("slow"):
                _relief = f" {str(_obs.get('relief_action'))}" if _obs.get("relief_action") else ""
                _repeat = " Repeated slowness detected." if _obs.get("repeated") else ""
                _cause = f" Probable cause: {_obs.get('probable_cause')}." if _obs.get("probable_cause") else ""
                st.warning(
                    f"⚠️ {name} was slow to load ({_elapsed:.1f}s)."
                    f"{_cause}{_repeat}{_relief} Diagnostic note saved: {_obs.get('note_path') or 'issue_notes'}"
                )
        except Exception:
            pass

# ==================================================
# PAGE ROUTER — RBAC DOUBLE-GUARD
# Every page checks role independently (defence in depth).
# ==================================================

# ── Sales ──────────────────────────────────────────────────────────────────

if selected_page == "Patient Merge":
    from modules.patient_merge import render_patient_merge
    safe_render(render_patient_merge, "Patient Merge")

elif selected_page == "Retail Order":
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

elif selected_page == "📋  Rejection Report":
    from modules.security.roles import require_role, MANAGER, ADMIN
    require_role(MANAGER, ADMIN)
    try:
        from modules.backoffice.rejection_report import render_rejection_report
        render_rejection_report()
    except Exception as _rr_e:
        import streamlit as st
        st.error(f"Rejection Report error: {_rr_e}")
        import traceback; st.code(traceback.format_exc())

elif selected_page == "🔢  Order Numbers":
    from modules.security.roles import require_role
    require_role("admin","manager")
    try:
        from modules.backoffice.order_number_health import render_order_number_health
        render_order_number_health()
    except Exception as _onh_e:
        import streamlit as st
        st.error(f"Order Numbers health: {_onh_e}")
        import traceback; st.code(traceback.format_exc())

elif selected_page == "👁️  CL Advisor":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    try:
        from modules.cl_lens_advisor import render_cl_lens_advisor
        render_cl_lens_advisor()
    except Exception as _cla_e:
        st.error(f"CL Advisor error: {_cla_e}")
        import traceback; st.code(traceback.format_exc())

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

elif selected_page == "Service Management":
    from modules.security.roles import require_role
    require_role(LAB, MANAGER, ADMIN)
    log_time("Open Service Management")
    if fitter_ok: safe_render(render_fitter_management, "Service Management")
    else: st.error("Service Management unavailable")

elif selected_page == "Dispatch":
    from modules.security.roles import require_role
    require_role(LAB, BILLING, MANAGER, ADMIN)
    log_time("Open Dispatch & Logistics")
    if dispatch_ok:
        safe_render(render_dispatch_queue, "Dispatch & Logistics")
    else:
        st.error("Dispatch module unavailable")

# ── Procurement Hub ─────────────────────────────────────────────────────────

elif selected_page == "Procurement":
    from modules.security.roles import require_role
    require_role(BILLING, INVENTORY, MANAGER, ADMIN)
    log_time("Open Procurement Hub")
    _proc_tab = st.radio(
        "Procurement section",
        ["📥 Queue", "📄 Invoice Match", "🧾 Direct Purchase", "📋 Purchase Register", "📊 Intelligence"],
        horizontal=True, key="proc_hub_tab", label_visibility="collapsed",
    )
    if _proc_tab == "📥 Queue":
        try:
            from modules.backoffice.procurement_queue import render_procurement_queue
            safe_render(render_procurement_queue, "Procurement Queue")
        except Exception as _e:
            st.error(f"Procurement Queue: {_e}")
    elif _proc_tab == "📄 Invoice Match":
        try:
            from modules.procurement.invoice_match_ui import render_invoice_match_ui
            safe_render(render_invoice_match_ui, "Invoice Match")
        except Exception as _e:
            st.error(f"Invoice Match: {_e}")
            import traceback; st.code(traceback.format_exc())
    elif _proc_tab == "🧾 Direct Purchase":
        try:
            from modules.procurement.direct_purchase_ui import render_direct_purchase_ui
            safe_render(render_direct_purchase_ui, "Direct Purchase")
        except Exception as _e:
            st.error(f"Direct Purchase: {_e}")
    elif _proc_tab == "📋 Purchase Register":
        try:
            from modules.backoffice.purchase_register import render_purchase_register
            safe_render(render_purchase_register, "Purchase Register")
        except Exception as _e:
            st.error(f"Purchase Register: {_e}")
    elif _proc_tab == "📊 Intelligence":
        if procurement_ok:
            safe_render(render_purchase_ui, "Procurement Intelligence")
        else:
            st.error("Procurement Intelligence unavailable")

elif selected_page == "Product & Inventory":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open Product & Inventory")
    if prod_inv_ok: safe_render(render_product_inventory_mgr, "Product & Inventory")
    else: st.error("Product & Inventory manager unavailable")

elif selected_page == "Bulk Order":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open Bulk Order")
    if bulk_order_ok: safe_render(render_bulk_order, "Bulk Order")
    else: st.error("Bulk Order unavailable")

elif selected_page == "WhatsApp Queue":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN, BILLING)
    log_time("Open WA Queue")
    if wa_queue_ok: safe_render(render_wa_queue, "WA Queue")
    else: st.error("WA Queue unavailable")

elif selected_page == "Bulk WhatsApp":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN, BILLING)
    log_time("Open Bulk WhatsApp")
    if wa_bulk_ok: safe_render(render_wa_bulk_sender, "Bulk WhatsApp")
    else: st.error("Bulk WhatsApp unavailable")

# ── CRM ────────────────────────────────────────────────────────────────────

elif selected_page == "CRM":
    from modules.security.roles import require_role
    require_role(BILLING, INVENTORY, MANAGER, ADMIN)
    log_time("Open CRM")
    if crm_ok: safe_render(render_crm_module, "CRM")
    else: st.error("CRM unavailable")


elif selected_page == "Retailer Orders":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Retailer Orders")
    if ret_orders_ok: safe_render(render_backoffice_orders, "Retailer Orders")
    else: st.error("Retailer Orders unavailable — deploy modules/retailer/backoffice_orders.py")

elif selected_page == "Online Orders":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Online Orders")
    if online_admin_ok: safe_render(render_online_store_admin, "Online Orders")
    else: st.error("Online Orders unavailable — deploy modules/online_store/store_admin.py")

# ── Payment Collection ──────────────────────────────────────────

elif selected_page == "Collect Payment":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Collect Payment")
    try:
        from modules.billing.payment_collection import render_payment_collection
        safe_render(render_payment_collection, "Collect Payment")
    except Exception as _pce:
        st.error(f"Payment Collection error: {_pce}")
        import traceback; st.code(traceback.format_exc())

# ── Challan & Invoice / Billing Hub ──────────────────────────────────────────

elif selected_page == "Challan & Invoice Dashboard":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Billing Dashboard")
    _bill_tab = st.radio(
        "Billing section",
        ["🧾 Billing Hub", "📋 Challan & Invoice Dashboard", "🔏 Admin Edit"],
        horizontal=True, key="bill_hub_tab", label_visibility="collapsed",
    )
    if _bill_tab == "🧾 Billing Hub":
        if billing_hub_ok:
            safe_render(render_billing_hub, "Billing Hub")
        else:
            try:
                from modules.billing.billing_hub import render_billing_hub as _rbh
                safe_render(_rbh, "Billing Hub")
            except Exception as _bhe:
                st.error(f"Billing Hub: {_bhe}")
    elif _bill_tab == "🔏 Admin Edit":
        try:
            from modules.billing.invoice_edit_admin import render_invoice_edit_admin
            safe_render(render_invoice_edit_admin, "Admin Edit")
        except Exception as _iee:
            st.error(f"Admin Edit: {_iee}")
    else:
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

elif selected_page == "Discount Analytics":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open Discount Analytics")
    if disc_dash_ok:
        safe_render(render_discount_dashboard, "Discount Analytics")
    else:
        st.error("Discount Analytics unavailable — ensure discount_dashboard.py is deployed.")

elif selected_page == "AI Price Suggestions":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open AI Price Suggestions")
    if price_sug_ok:
        safe_render(render_price_suggestions, "AI Price Suggestions")
    else:
        st.error("AI Price Suggestions unavailable — ensure price_suggestions.py is deployed.")

elif selected_page == "Data Loader":
    from modules.security.roles import require_role
    require_role(MANAGER, ADMIN)
    log_time("Open Data Loader")
    try:
        from modules.ui.loader_ui import render_loader_page
        safe_render(render_loader_page, "Data Loader")
    except Exception as e:
        st.error(f"❌ Data Loader failed: {e}")
        st.code(traceback.format_exc())

elif selected_page == "Scan Frame":
    # Opens Quick Add directly on the Frame tab (tab index 1)
    st.session_state["quick_add_tab"] = 1
    try:
        from modules.ui.quick_add import render_quick_add
        render_quick_add(default_tab=1, render_id="sf")
    except Exception as e:
        st.error(f"Scan Frame error: {e}")

elif selected_page == "Quick Add":
    from modules.security.roles import require_role
    require_role(BILLING, INVENTORY, MANAGER, ADMIN)
    log_time("Open Quick Add")
    try:
        from modules.ui.quick_add import render_quick_add
        safe_render(render_quick_add, "Quick Add")
    except Exception as e:
        st.error(f"❌ Quick Add failed: {e}")
        import traceback; st.code(traceback.format_exc())

elif selected_page == "Reports":
    try:
        from modules.reports.reports_ui import render_reports
        render_reports()
    except Exception as e:
        import traceback
        st.error(f"Reports error: {e}")
        st.code(traceback.format_exc())

elif selected_page == "Registers":
    try:
        from modules.reports.registers import render_registers
        render_registers()
    except Exception as e:
        import traceback
        st.error(f"Registers error: {e}")
        st.code(traceback.format_exc())

elif selected_page == "GST Portal":
    try:
        from modules.gst.gst_portal_ui import render_gst_portal
        render_gst_portal()
    except Exception as e:
        import traceback
        st.error(f"GST Portal error: {e}")
        st.code(traceback.format_exc())

elif selected_page == "Discount Audit":
    from modules.security.roles import require_role
    require_role(BILLING, MANAGER, ADMIN)
    log_time("Open Discount Audit")
    try:
        from modules.pricing.discount_audit import render_discount_audit
        safe_render(render_discount_audit, "Discount Audit")
    except Exception as _dae:
        st.error(f"Discount Audit: {_dae}")
        st.code(traceback.format_exc())

elif selected_page == "Accounts":
    try:
        from modules.accounting.accounts_ui import render_accounts
        render_accounts()
    except Exception as e:
        import traceback
        st.error(f"Accounts error: {e}")
        st.code(traceback.format_exc())

elif selected_page == "HR":
    try:
        from modules.hr.hr_ui import render_hr
        render_hr()
    except Exception as e:
        import traceback
        st.error(f"HR error: {e}")
        st.code(traceback.format_exc())

elif selected_page == "Label Preview":
    try:
        from modules.printing.label_preview import render_label_preview_widget
        render_label_preview_widget()
    except Exception as e:
        st.error(f"Label Preview error: {e}")

elif selected_page == "Scanner":
    log_time("Open Scanner")
    try:
        from modules.backoffice.scanner_panel import render_scanner_panel
        safe_render(render_scanner_panel, "Scanner")
    except Exception as e:
        st.error(f"❌ Scanner Panel failed: {e}")
        import traceback; st.code(traceback.format_exc())

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

elif selected_page == "Inventory Audit":
    from modules.security.roles import require_role
    require_role(INVENTORY, MANAGER, ADMIN)
    log_time("Open Inventory Audit")
    try:
        from modules.ui.inventory_audit_manager import render_inventory_audit_manager
        safe_render(render_inventory_audit_manager, "Inventory Audit")
    except Exception as e:
        st.error(f"❌ Inventory Audit failed: {e}")
        import traceback; st.code(traceback.format_exc())

elif selected_page == "Printer Settings":
    from modules.security.roles import require_role
    require_role(INVENTORY, MANAGER, ADMIN, LAB)
    log_time("Open Printer Settings")
    try:
        from modules.printing.printer_settings_ui import render_printer_settings
        safe_render(render_printer_settings, "Printer Settings")
    except Exception as e:
        st.error(f"❌ Printer Settings failed: {e}")
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

elif selected_page == "Shop Master":
    try:
        from modules.settings.shop_master import render_shop_master
        render_shop_master()
    except Exception as e:
        import traceback
        st.error(f"Shop Master error: {e}")
        st.code(traceback.format_exc())

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

elif selected_page == "Owner Dashboard":
    from modules.security.roles import require_role
    require_role(ADMIN)
    log_time("Open Owner Dashboard")
    if owner_dash_ok:
        safe_render(render_owner_dashboard, "Owner Dashboard")
    else:
        st.error("Owner Dashboard unavailable — deploy modules/founder/owner_dashboard.py")

else:
    # ── Fallback: unknown/blank selected_page ─────────────────────────────
    # Happens on first load if default page detection failed, or if
    # session state has a stale page label from an old session.
    if selected_page and not selected_page.startswith("──"):
        st.warning(f"Page **{selected_page}** not found. Select a page from the sidebar.")
    # Auto-redirect to first available real page
    if _all_pages_flat:
        st.session_state["_sidebar_page"] = _all_pages_flat[0]
        st.rerun()

# ==================================================
# FOOTER
# ==================================================

st.divider()
st.caption(f"DV ERP v1.0 | Unified Clean Architecture | {_username} ({_role.upper()})")

log_time("App fully loaded")
