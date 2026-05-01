"""
modules/loaders/feature_flags.py
==================================
Feature Flags System — WIN16 DV ERP
DB-backed runtime toggles with safe in-memory fallback.

SQL to create the table (run once):

    CREATE TABLE IF NOT EXISTS system_flags (
        key         TEXT PRIMARY KEY,
        value       TEXT        NOT NULL DEFAULT 'true',
        description TEXT,
        updated_at  TIMESTAMP   DEFAULT NOW()
    );

    INSERT INTO system_flags (key, value, description) VALUES
        ('loader.opening_enabled',    'true',  'Allow OPENING stock reset mode in loader'),
        ('loader.schema_guard',       'true',  'Enable schema diff engine before import'),
        ('loader.preview_required',   'true',  'Require preview approval before LIVE import'),
        ('loader.strict_mode',        'false', 'Block import if any unknown column detected'),
        ('loader.auto_schema',        'true',  'Auto-suggest column mappings via fuzzy match'),
        ('loader.ai_advisor',         'true',  'Show AI advisor tips in loader UI'),
        ('loader.lazy_load',          'true',  'Lazy-load loader core (faster app boot)')
    ON CONFLICT (key) DO NOTHING;
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ── In-memory cache (cleared on each Streamlit run) ──────────────────────────
_FLAG_CACHE: Dict[str, str] = {}

# ── Hardcoded defaults — used when DB is unavailable ─────────────────────────
_DEFAULTS: Dict[str, bool] = {
    # ── Loader flags (existing) ───────────────────────────────────────
    "loader.opening_enabled":   True,
    "loader.schema_guard":      True,
    "loader.preview_required":  True,
    "loader.strict_mode":       False,
    "loader.auto_schema":       True,
    "loader.ai_advisor":        True,
    "loader.lazy_load":         True,

    # ── Advisory procurement (Zone 1) ─────────────────────────────────
    "advisory_enabled":         True,   # main advisory panel gate
    "advisory_roles":           False,  # True = restrict to manager/inventory roles
    "enable_ai_advisor":        False,  # AI reorder predictions (needs 60d data)

    # ── Backoffice / fulfillment (Zone 2–3) ───────────────────────────
    "billing_gate_enabled":     True,
    "enable_whatsapp_po":       False,  # WhatsApp PO sending
    "enable_audit_log":         True,
    "enable_perf_tracking":     True,
    "debug_mode":               False,  # devtools debug overlay (never True in prod)

    # ── Supplier intelligence (Zone 2) ────────────────────────────────
    "supplier_intelligence_enabled": True,

    # ── Founder control tower (Zone 4) ────────────────────────────────
    "founder_dashboard_enabled": True,
}

# ── Runtime SYSTEM_FLAGS dict (readable from kernel + sidebar) ───────
# Usage:  from modules.flags.feature_flags import SYSTEM_FLAGS
#         if SYSTEM_FLAGS.get("advisory_enabled"): ...
SYSTEM_FLAGS: Dict[str, bool] = dict(_DEFAULTS)


def _db_available() -> bool:
    try:
        from modules.sql_adapter import run_query
        run_query("SELECT 1")
        return True
    except Exception:
        return False


def get_flag(key: str, default: bool = True) -> bool:
    """
    Read a feature flag.
    Priority: in-memory cache → DB → hardcoded default → `default` param
    """
    if key in _FLAG_CACHE:
        return _FLAG_CACHE[key].lower() == "true"

    try:
        from modules.sql_adapter import run_query
        rows = run_query("SELECT value FROM system_flags WHERE key=%s", (key,))
        if rows:
            val = rows[0]["value"]
            _FLAG_CACHE[key] = val
            return val.lower() == "true"
    except Exception as e:
        logger.debug(f"Flag DB read failed for '{key}': {e}")

    return _DEFAULTS.get(key, default)


def set_flag(key: str, value: bool, description: str = "") -> bool:
    """
    Write a feature flag to DB.
    Returns True on success.
    """
    _FLAG_CACHE[key] = "true" if value else "false"
    try:
        from modules.sql_adapter import run_write
        run_write("""
            INSERT INTO system_flags (key, value, description, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value      = EXCLUDED.value,
                updated_at = NOW()
        """, (key, "true" if value else "false", description))
        return True
    except Exception as e:
        logger.warning(f"Flag DB write failed for '{key}': {e}")
        return False


def get_all_flags() -> Dict[str, Any]:
    """Return all flags with current values — for admin display."""
    flags = dict(_DEFAULTS)  # start with defaults
    try:
        from modules.sql_adapter import run_query
        rows = run_query("SELECT key, value, description, updated_at FROM system_flags ORDER BY key")
        for r in rows:
            flags[r["key"]] = {
                "value":       r["value"].lower() == "true",
                "raw":         r["value"],
                "description": r.get("description", ""),
                "updated_at":  str(r.get("updated_at", "")),
            }
    except Exception:
        # Return defaults in same shape
        for k, v in _DEFAULTS.items():
            if k not in flags or not isinstance(flags[k], dict):
                flags[k] = {"value": v, "raw": str(v).lower(), "description": "", "updated_at": "default"}
    return flags


def clear_cache():
    """Force re-read from DB on next access."""
    _FLAG_CACHE.clear()


def sync_system_flags() -> None:
    """
    Sync SYSTEM_FLAGS dict from DB.
    Call once at app startup (in app.py) so kernel + sidebar see current values.

    Usage in app.py:
        from modules.flags.feature_flags import sync_system_flags
        sync_system_flags()
    """
    for key in list(SYSTEM_FLAGS.keys()):
        SYSTEM_FLAGS[key] = get_flag(key, default=_DEFAULTS.get(key, False))


def ensure_flags_table() -> bool:
    """
    Create system_flags table + seed defaults if not present.
    Safe to call on every startup.
    """
    try:
        from modules.sql_adapter import run_write, run_query
        run_write("""
            CREATE TABLE IF NOT EXISTS system_flags (
                key         TEXT PRIMARY KEY,
                value       TEXT        NOT NULL DEFAULT 'true',
                description TEXT,
                updated_at  TIMESTAMP   DEFAULT NOW()
            )
        """)
        for k, v in _DEFAULTS.items():
            run_write("""
                INSERT INTO system_flags (key, value, description)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO NOTHING
            """, (k, "true" if v else "false", ""))
        return True
    except Exception as e:
        logger.warning(f"Could not ensure flags table: {e}")
        return False
