"""
modules/loaders/schema_sync.py
================================
DB → Registry auto-sync — DV ERP WIN16

Called at startup from app.py (Option A pattern).
Detects new columns in the live DB that are absent from db_schema_registry.py,
writes Col() entries automatically, and reloads the registry in-memory.

Never raises — all failures are logged and silently skipped.
"""

import importlib
import logging
import os
import re
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# ── Tables managed by the registry ────────────────────────────────────────────
_FILE_TYPE_TABLE_MAP = {
    "PRODUCT":  "products",
    "FRAME":    "frames",
    "PARTY":    "parties",
    "OPHLENS":  "inventory_stock",
    "CLENS":    "inventory_stock",
    "SOL":      "batches",
    "BLANK":    "blank_inventory",
    "PATIENT":  "patients",
}

# Columns that are always system-managed — never auto-synced into registry
_SYSTEM_COLS = {
    "id", "created_at", "updated_at", "product_id", "patient_id",
    "created_by", "product_code", "status", "visit_id",
}


# ── Internal DB query helper ───────────────────────────────────────────────────

def _q(sql: str, params=None) -> list:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        logger.warning(f"[schema_sync] Query failed: {e}")
        return []


# ── PostgreSQL type → registry type ───────────────────────────────────────────

def _map_pg_type(pg_type: str) -> str:
    pg_type = (pg_type or "").lower()
    mapping = {
        "character varying": "text",
        "text":              "text",
        "varchar":           "text",
        "integer":           "integer",
        "bigint":            "integer",
        "smallint":          "integer",
        "numeric":           "numeric",
        "decimal":           "numeric",
        "real":              "numeric",
        "double precision":  "numeric",
        "boolean":           "boolean",
        "date":              "date",
        "timestamp without time zone": "timestamp",
        "timestamp with time zone":    "timestamp",
        "uuid":              "uuid",
    }
    return mapping.get(pg_type, "text")


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def diff_schema() -> Dict:
    """
    Compare live DB columns against db_schema_registry.py.

    Returns:
        {
            "new_columns":     [{"file_type", "table", "db_column", "db_type"}, ...],
            "removed_columns": [{"file_type", "db_column"}, ...],
        }
    """
    try:
        from modules.loaders.db_schema_registry import DB_SCHEMA
    except Exception as e:
        logger.warning(f"[schema_sync] Cannot import DB_SCHEMA: {e}")
        return {"new_columns": [], "removed_columns": []}

    new_columns     = []
    removed_columns = []
    seen_tables     = {}  # table → set of db cols already checked (avoid duplicate for OPHLENS/CLENS)

    for file_type, table in _FILE_TYPE_TABLE_MAP.items():
        # Registry columns for this file_type (mapped cols only)
        registry_cols = {
            c.db_column
            for c in DB_SCHEMA.get(file_type, [])
            if c.db_column and c.excel_header
        }

        # Live DB columns for this table
        if table not in seen_tables:
            rows = _q(
                "SELECT column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position",
                (table,),
            )
            seen_tables[table] = {r["column_name"]: r["data_type"] for r in rows}

        db_cols = seen_tables[table]

        if not db_cols:
            # Table doesn't exist yet — skip silently
            continue

        # New in DB, absent from registry
        for col, dtype in db_cols.items():
            if col in _SYSTEM_COLS:
                continue
            if col not in registry_cols:
                new_columns.append({
                    "file_type": file_type,
                    "table":     table,
                    "db_column": col,
                    "db_type":   _map_pg_type(dtype),
                })

    return {"new_columns": new_columns, "removed_columns": removed_columns}


def apply_sync(new_columns: List[Dict]) -> Tuple[bool, str, List[Dict]]:
    """
    Write new Col() entries into db_schema_registry.py source file.

    Inserts each new column just before the '# System' block of the matching
    file_type section, so it appears with the writable columns.

    Returns:
        (success: bool, message: str, added: List[Dict])
    """
    if not new_columns:
        return True, "Nothing to sync", []

    registry_path = os.path.join(os.path.dirname(__file__), "db_schema_registry.py")

    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            source = f.read()

        added = []

        for col_info in new_columns:
            ft       = col_info["file_type"]
            db_col   = col_info["db_column"]
            db_type  = col_info["db_type"]

            # Skip if already present (idempotency guard)
            if f'"{db_col}"' in source:
                continue

            # Generate a sensible Excel header from the db_column name
            excel_hdr = db_col.replace("_", " ").title()

            new_line = (
                f'        Col("{excel_hdr}", "{db_col}", "{db_type}", '
                f'description="Auto-synced from DB", download=True),\n'
                f'        # ^ auto-added by schema_sync on startup\n'
            )

            # Find the file_type block and insert before its first # System comment
            # Pattern: inside the "FILETYPE": [ ... ] block, find "# System"
            # We search for the section header then the System comment
            section_pattern = rf'(\s+"{re.escape(ft)}":\s*\[.*?)(        # System)'
            match = re.search(section_pattern, source, re.DOTALL)

            if match:
                insert_pos = match.start(2)
                source = source[:insert_pos] + new_line + source[insert_pos:]
                added.append(col_info)
                logger.info(f"[schema_sync] Queued: {ft}.{db_col} ({db_type})")
            else:
                logger.warning(f"[schema_sync] Could not find insertion point for {ft}.{db_col}")

        if added:
            with open(registry_path, "w", encoding="utf-8") as f:
                f.write(source)
            logger.info(f"[schema_sync] Wrote {len(added)} new column(s) to registry")
            return True, f"Added {len(added)} column(s)", added
        else:
            return True, "All columns already in registry", []

    except Exception as e:
        logger.error(f"[schema_sync] apply_sync failed: {e}")
        return False, str(e), []


def reload_registry():
    """
    Force-reload db_schema_registry in-memory so new Col() entries are live
    without restarting the process.
    """
    try:
        import modules.loaders.db_schema_registry as reg
        importlib.reload(reg)
        logger.info("[schema_sync] db_schema_registry reloaded in memory")
    except Exception as e:
        logger.warning(f"[schema_sync] Registry reload failed: {e}")
