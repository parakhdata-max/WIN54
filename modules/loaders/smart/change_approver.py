"""
modules/loaders/smart/change_approver.py
==========================================
Change Approver — Applies approved changes to DB.

After user confirms (Yes / typed CONFIRM):
  1. Takes a backup snapshot of all affected records
  2. Applies ONLY the changed fields (field-level, not row-level)
  3. Writes full audit log per field change
  4. Returns ApplyResult with counts and backup_id

SQL tables needed (run once):
-------------------------------
CREATE TABLE IF NOT EXISTS field_change_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id     UUID,
    file_type     TEXT,
    entity_id     UUID,
    entity_key    TEXT,
    field_name    TEXT,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    TEXT,
    changed_at    TIMESTAMPTZ DEFAULT NOW(),
    risk_level    TEXT,
    backup_id     UUID
);

CREATE TABLE IF NOT EXISTS field_change_backup (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    backup_id     UUID,
    file_type     TEXT,
    entity_id     UUID,
    entity_key    TEXT,
    snapshot      JSONB,
    backed_up_at  TIMESTAMPTZ DEFAULT NOW(),
    backed_up_by  TEXT
);
-------------------------------
"""

import json
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict

from modules.loaders.smart.change_detector import (
    ChangeReport, FieldChange,
    RISK_WARNING, RISK_CAUTION, RISK_SAFE,
)

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    success:      bool
    import_id:    str
    backup_id:    Optional[str]
    applied:      int = 0
    skipped:      int = 0
    errors:       List[str] = field(default_factory=list)
    applied_at:   str = ""


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def apply_changes(
    report:    ChangeReport,
    user:      str = "system",
    dry_run:   bool = False,
) -> ApplyResult:
    """
    Apply all approved changes from a ChangeReport to the DB.

    Steps:
      1. Take backup snapshot of affected records
      2. Apply each FieldChange to DB (field-level UPDATE)
      3. Write audit log per change

    Set dry_run=True to simulate without writing to DB.
    """
    import_id = str(uuid.uuid4())
    backup_id = str(uuid.uuid4()) if getattr(report, "backup_required", False) or _has_warning_changes(report) else None
    result    = ApplyResult(
        success   = False,
        import_id = import_id,
        backup_id = backup_id,
        applied_at = datetime.now().isoformat(),
    )

    if not report.has_changes:
        result.success = True
        result.skipped = 0
        return result

    try:
        from modules.sql_adapter import run_write, run_query
    except ImportError:
        result.errors.append("DB adapter not available")
        return result

    # ── Step 1: Backup affected records ───────────────────────────────────────
    if backup_id and not dry_run:
        _take_backup(report, backup_id, user, run_query, run_write)

    # ── Step 2: Group changes by entity_id for efficient UPDATEs ─────────────
    by_entity: Dict[str, List[FieldChange]] = {}
    for change in report.changes:
        eid = change.entity_id or change.entity_key
        by_entity.setdefault(eid, []).append(change)

    # ── Step 3: Apply changes ─────────────────────────────────────────────────
    for entity_key, changes in by_entity.items():
        entity_id  = changes[0].entity_id
        table      = _get_table(report.file_type)
        id_col     = "id"

        if not entity_id or not table:
            result.skipped += len(changes)
            continue

        # Build SET clause for all changed fields in one UPDATE
        set_parts  = []
        set_values = []

        for c in changes:
            col = c.field_name
            if not _is_safe_column_name(col):
                result.skipped += 1
                continue

            val = _coerce_value(c.field_name, c.new_value, file_type=report.file_type)

            # ── Name → UUID resolution for FK fields ─────────────────────
            # The smart loader stores the human-readable name from Excel.
            # These FK columns require a UUID — resolve before writing.
            if col == "preferred_supplier_id" and val and len(str(val)) < 36:
                # Value is a name, not a UUID — resolve it
                try:
                    from modules.sql_adapter import run_query as _rq_fk
                    sup = _rq_fk(
                        "SELECT id::text FROM parties "
                        "WHERE LOWER(TRIM(party_name)) = LOWER(TRIM(%s)) "
                        "  AND UPPER(party_type) IN ('SUPPLIER','VENDOR') "
                        "LIMIT 1",
                        (str(val),)
                    )
                    if sup:
                        val = sup[0]["id"]
                    else:
                        result.errors.append(
                            f"Supplier '{val}' not found in party master — "
                            f"skipped for product {entity_key}"
                        )
                        result.skipped += 1
                        continue
                except Exception as _fe:
                    result.errors.append(f"Supplier lookup failed: {_fe}")
                    result.skipped += 1
                    continue

            set_parts.append(f'"{col}" = %s')
            set_values.append(val)

        if not set_parts:
            continue

        sql = f'UPDATE {table} SET {", ".join(set_parts)} WHERE {id_col} = %s'
        set_values.append(entity_id)

        if not dry_run:
            try:
                run_write(sql, tuple(set_values))
                result.applied += len(changes)
            except Exception as e:
                err = f"Failed to update {entity_key}: {e}"
                result.errors.append(err)
                logger.error(err)
                result.skipped += len(changes)
                continue
        else:
            result.applied += len(changes)

        # ── Step 4: Write audit log ───────────────────────────────────────────
        if not dry_run:
            for c in changes:
                _log_change(c, import_id, backup_id, user, report.file_type, run_write)

    result.success = len(result.errors) == 0
    return result


# ══════════════════════════════════════════════════════════════════════════════
# BACKUP
# ══════════════════════════════════════════════════════════════════════════════

def _take_backup(report: ChangeReport, backup_id: str, user: str, run_query, run_write):
    """Snapshot all affected records before applying changes."""
    entity_ids = list({c.entity_id for c in report.changes if c.entity_id})
    if not entity_ids:
        return

    table = _get_table(report.file_type)
    if not table:
        return

    try:
        placeholders = ", ".join(["%s"] * len(entity_ids))
        rows = run_query(
            f"SELECT * FROM {table} WHERE id IN ({placeholders})",
            tuple(entity_ids)
        ) or []

        for row in rows:
            entity_id  = str(row.get("id", ""))
            entity_key = str(row.get(_get_key_col(report.file_type), entity_id))
            snapshot   = {k: str(v) for k, v in row.items()}

            run_write("""
                INSERT INTO field_change_backup
                    (backup_id, file_type, entity_id, entity_key, snapshot, backed_up_by)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (backup_id, report.file_type, entity_id, entity_key,
                  json.dumps(snapshot), user))

    except Exception as e:
        logger.warning(f"Backup failed (non-fatal): {e}")


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ══════════════════════════════════════════════════════════════════════════════

def _log_change(change: FieldChange, import_id: str, backup_id: Optional[str],
                user: str, file_type: str, run_write):
    """
    Write one audit row to field_change_log per field changed.

    Records full approval context:
      - approved_by:     who approved this specific change (from inline grid)
      - manually_edited: True if user changed the value in the edit grid
      - approved:        always True here (rejected rows never reach this function)
    """
    _approved_by     = getattr(change, "approved_by",     None) or user
    _manually_edited = getattr(change, "manually_edited", False)

    try:
        run_write("""
            INSERT INTO field_change_log
                (import_id, file_type, entity_id, entity_key, field_name,
                 old_value, new_value, changed_by, risk_level, backup_id,
                 approved, approved_by, manually_edited)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            import_id,
            file_type,
            change.entity_id,
            change.entity_key,
            change.field_name,
            str(change.old_value) if change.old_value is not None else None,
            str(change.new_value) if change.new_value is not None else None,
            user,
            change.risk_level,
            backup_id,
            True,             # approved — rejected rows never reach here
            _approved_by,     # who approved this change
            _manually_edited, # True if value was edited in the inline grid
        ))
    except Exception as e:
        # Fallback: try without new columns (old DB schema before migration)
        try:
            run_write("""
                INSERT INTO field_change_log
                    (import_id, file_type, entity_id, entity_key, field_name,
                     old_value, new_value, changed_by, risk_level, backup_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                import_id, file_type, change.entity_id, change.entity_key,
                change.field_name,
                str(change.old_value) if change.old_value is not None else None,
                str(change.new_value) if change.new_value is not None else None,
                user, change.risk_level, backup_id,
            ))
        except Exception as e2:
            logger.warning(f"Audit log failed (non-fatal): {e2}")



# ══════════════════════════════════════════════════════════════════════════════
# ROLLBACK
# ══════════════════════════════════════════════════════════════════════════════

def rollback_by_backup_id(backup_id: str, user: str = "system") -> ApplyResult:
    """
    Restore all records from a backup snapshot.
    Called from Import Rollback tab.
    """
    result = ApplyResult(success=False, import_id=str(uuid.uuid4()), backup_id=backup_id)

    try:
        from modules.sql_adapter import run_query, run_write
    except ImportError:
        result.errors.append("DB adapter not available")
        return result

    snapshots = run_query(
        "SELECT * FROM field_change_backup WHERE backup_id = %s",
        (backup_id,)
    ) or []

    if not snapshots:
        result.errors.append(f"No backup found for backup_id: {backup_id}")
        return result

    for snap in snapshots:
        file_type = snap.get("file_type")
        entity_id = snap.get("entity_id")
        snapshot  = snap.get("snapshot", {})
        table     = _get_table(file_type)

        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except Exception:
                continue

        if not table or not entity_id:
            continue

        # Build UPDATE from snapshot
        set_parts  = []
        set_values = []
        skip_cols  = {"id", "_id", "created_at", "updated_at"}

        for col, val in snapshot.items():
            if col in skip_cols or not _is_safe_column_name(col):
                continue
            set_parts.append(f'"{col}" = %s')
            set_values.append(None if val in ("None", "nan", "NaT", "") else val)

        if not set_parts:
            continue

        sql = f'UPDATE {table} SET {", ".join(set_parts)} WHERE id = %s'
        set_values.append(entity_id)

        try:
            run_write(sql, tuple(set_values))
            result.applied += 1
        except Exception as e:
            result.errors.append(f"Rollback failed for {entity_id}: {e}")
            result.skipped += 1

    result.success = len(result.errors) == 0
    return result


def get_backup_list(file_type: Optional[str] = None, limit: int = 50) -> list:
    """Fetch recent backups for display in rollback UI."""
    try:
        from modules.sql_adapter import run_query
        where = f"WHERE file_type = '{file_type}'" if file_type else ""
        return run_query(f"""
            SELECT DISTINCT backup_id, file_type, backed_up_by,
                   MIN(backed_up_at) AS backed_up_at,
                   COUNT(*) AS record_count
            FROM field_change_backup
            {where}
            GROUP BY backup_id, file_type, backed_up_by
            ORDER BY MIN(backed_up_at) DESC
            LIMIT %s
        """, (limit,)) or []
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_table(file_type: str) -> Optional[str]:
    """
    Registry-driven table lookup — single source of truth.
    Falls back to hardcoded map only if registry unavailable.
    """
    try:
        from modules.loaders.smart.download_manager import _get_cfg
        cfg = _get_cfg(file_type)
        if cfg and cfg.get("table"):
            return cfg["table"]
    except Exception:
        pass
    # Fallback
    return {
        "CLENS":   "inventory_stock",
        "OPHLENS": "inventory_stock",
        "PRODUCT": "products",
        "FRAME":   "inventory_stock",   # ← was wrong ("frames") — fixed
        "PARTY":   "parties",
        "SOL":     "batches",
        "BLANK":   "blank_inventory",
        "PATIENT": "patients",
        "PRICE":   "inventory_stock",
    }.get(file_type)


def _get_key_col(file_type: str) -> str:
    try:
        from modules.loaders.smart.download_manager import _get_cfg
        cfg = _get_cfg(file_type)
        if cfg and cfg.get("key_col"):
            return cfg["key_col"]
    except Exception:
        pass
    return {
        "CLENS":   "batch_no",
        "OPHLENS": "product_name",
        "PRODUCT": "product_name",
        "FRAME":   "batch_no",
        "PARTY":   "party_name",
        "SOL":     "batch_no",
        "BLANK":   "brand",
        "PATIENT": "mobile",
    }.get(file_type, "id")


def _is_safe_column_name(col: str) -> bool:
    """Prevent SQL injection via column names."""
    import re
    return bool(re.match(r'^[a-z_][a-z0-9_]*$', col))


# ── Registry-driven type cache (built once per process per file_type) ─────────
_type_cache: dict = {}

def _get_field_type_info(file_type: str) -> dict:
    """
    Returns {db_column: {db_type, allowed_values}} from DB_SCHEMA registry.
    Cached per file_type — registry is the single source of truth for types.
    Same data the loader engine uses for validation and coercion.
    """
    if file_type in _type_cache:
        return _type_cache[file_type]
    result: dict = {}
    try:
        from modules.loaders.db_schema_registry import DB_SCHEMA
        for col in DB_SCHEMA.get(file_type, []):
            if col.db_column:
                result[col.db_column] = {
                    "db_type":       col.db_type,
                    "allowed_values": [v.lower() for v in (col.allowed_values or [])],
                }
    except Exception:
        pass
    _type_cache[file_type] = result
    return result


def _coerce_value(field_name: str, value, file_type: str = ""):
    """
    Registry-driven value coercion — same logic the loader engine uses.

    Reads db_type from DB_SCHEMA (the single source of truth) so this
    function and process_upload() are always in sync.

    Fallback: hardcoded type sets for backward compatibility when
    registry is unavailable (e.g. during testing).
    """
    if value is None or str(value).strip() in ("", "None", "nan", "NaT"):
        return None

    cleaned = str(value).strip()

    # ── Try registry first ────────────────────────────────────────────────────
    if file_type:
        _type_info = _get_field_type_info(file_type)
        _finfo     = _type_info.get(field_name)
        if _finfo:
            db_type = _finfo["db_type"]

            if db_type == "boolean":
                return cleaned.upper() in ("YES", "TRUE", "1", "Y")

            if db_type in ("numeric", "decimal", "float", "integer", "int"):
                try:
                    return float(cleaned)
                except (ValueError, TypeError):
                    return None

            if db_type == "integer":
                try:
                    return int(float(cleaned))
                except (ValueError, TypeError):
                    return None

            # main_group: normalise casing to prevent duplicate group entries
            if field_name == "main_group":
                try:
                    from modules.loaders.universal_loader_core import _canonical_main_group
                    return _canonical_main_group(cleaned)
                except Exception:
                    pass

            return cleaned   # text / date / other — pass as-is

    # ── Fallback: hardcoded sets (when registry unavailable) ─────────────────
    _bool_fields = {
        "is_active", "is_batch_applicable", "is_eye_specific",
        "allow_loose", "auto_fulfillment", "reorder_enabled",
        "uv_blocking", "is_percentage", "print_with_powers",
    }
    _numeric_fields = {
        "selling_price", "purchase_rate", "mrp", "cost_price",
        "quantity", "qty", "box_size", "credit_limit", "credit_days",
        "gst_rate", "gst_percent", "index_value", "sph", "cyl", "axis",
        "add_power", "qty_right", "qty_left", "qty_independent",
        "min_stock_qty", "supplier_tat_days", "wlp_per_pair",
        "srp_per_pair", "wlp_addon", "srp_addon", "sort_order",
        "water_content", "base_curve", "diameter",
    }

    if field_name in _bool_fields:
        return cleaned.upper() in ("YES", "TRUE", "1", "Y")

    if field_name == "main_group":
        try:
            from modules.loaders.universal_loader_core import _canonical_main_group
            return _canonical_main_group(cleaned)
        except Exception:
            pass

    if field_name in _numeric_fields:
        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return None

    return cleaned


def _has_warning_changes(report: ChangeReport) -> bool:
    from modules.loaders.smart.change_detector import RISK_WARNING
    return any(c.risk_level == RISK_WARNING for c in report.changes)
