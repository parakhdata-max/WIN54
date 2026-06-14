"""
modules/loaders/live_schema_bridge.py
=======================================
Live DB-to-Loader Schema Bridge  — DV ERP

This is the RUNTIME source of truth for both the downloader and uploader.
The static db_schema_registry.py provides METADATA (human headers,
descriptions, required flags).  This module provides LIVE TRUTH:

  • What columns actually exist in the DB right now
  • Their real Postgres types (boolean, text[], time, etc.)
  • Which table/alias they belong to in JOIN queries
  • How to SELECT them (type casts, array handling, etc.)
  • How to map uploaded Excel headers → DB columns (for upload)

Any column added to the DB appears automatically in downloads
and is accepted by uploads — no registry edit required.
Any column in the registry but not in the DB is silently skipped.

Public API:
    get_live_schema(file_type)           → List[LiveCol]
    build_download_sql(file_type)        → str  (full SELECT SQL)
    get_upload_col_map(file_type)        → dict (normalised_header → db_column)
    coerce_for_write(val, col)           → Python value ready for psycopg2
    refresh(file_type=None)              → clear cache
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────
_CACHE: Dict[str, dict] = {}
CACHE_TTL = 300  # seconds — refresh every 5 minutes or on explicit refresh()

# ── System columns — never downloaded or uploaded ─────────────────────────────
_GLOBAL_SYSTEM_COLS = {
    "id", "created_at", "updated_at", "created_by",
    "product_code", "status",               # legacy mirrors
}

# ── Postgres type → normalised type ──────────────────────────────────────────
_PG_TYPE_MAP = {
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
    "time without time zone":      "time",
    "time with time zone":         "time",
    "uuid":              "uuid",
    "ARRAY":             "array",
}


def _norm_pg_type(pg_type: str) -> str:
    pt = (pg_type or "").strip()
    if pt.endswith("[]"):
        return "array"
    return _PG_TYPE_MAP.get(pt, "text")


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE REGISTRY — one entry per file_type
# ═══════════════════════════════════════════════════════════════════════════════

#  tables   : list of {name, alias}.  First entry = primary table.
#  join      : LEFT/INNER JOIN clause (for multi-table types)
#  base_where: mandatory WHERE fragment (e.g. stock_type filter)
#  order_by  : ORDER BY clause
#  skip_cols : per-type system cols to hide (in addition to _GLOBAL_SYSTEM_COLS)
#  prefer_table: when a column name exists in both tables, which alias wins
#  readonly_tables: tables whose columns are download-only (not written on upload)

TABLE_REGISTRY: Dict[str, dict] = {
    "PARTY": {
        "tables":       [{"name": "parties",  "alias": ""}],
        "order_by":     "party_name",
        "skip_cols":    set(),
    },
    "PATIENT": {
        "tables":       [
            {"name": "patients",       "alias": "pt"},
            {"name": "patient_visits", "alias": "pv"},
        ],
        "join":         "LEFT JOIN patient_visits pv ON pv.patient_id = pt.id",
        "order_by":     "pt.master_name, pv.visit_date DESC NULLS LAST",
        "skip_cols":    {"patient_id", "is_temporary", "visit_name",
                         "va_distance_aided_r", "va_distance_aided_l"},
        "prefer_table": {"record_no": "pt"},   # exists in both — use patients version
        "readonly_tables": set(),              # both tables are writable
    },
    "PRODUCT": {
        "tables":       [{"name": "products", "alias": ""}],
        "base_where":   "COALESCE(is_active, TRUE) = TRUE",
        "order_by":     "product_name",
        "skip_cols":    {"product_code"},
    },
    "SOL": {
        "tables":       [
            {"name": "batches",  "alias": "b"},
            {"name": "products", "alias": "p"},
        ],
        "join":         "JOIN products p ON p.id = b.product_id",
        "order_by":     "p.product_name",
        "skip_cols":    {"product_id"},
        "readonly_tables": {"products"},
    },
    "OPHLENS": {
        "tables":       [
            {"name": "inventory_stock", "alias": "s"},
            {"name": "products",        "alias": "p"},
        ],
        "join":         "LEFT JOIN products p ON p.id = s.product_id",
        "base_where":   "s.stock_type = 'POWER' AND UPPER(p.main_group) = 'OPHTHALMIC LENSES' AND COALESCE(s.is_active,TRUE) = TRUE AND s.quantity > 0",
        "order_by":     "p.product_name, s.sph, s.cyl",
        "skip_cols":    {"product_id"},
        "readonly_tables": {"products"},
    },
    "CLENS": {
        "tables":       [
            {"name": "inventory_stock", "alias": "s"},
            {"name": "products",        "alias": "p"},
        ],
        "join":         "LEFT JOIN products p ON p.id = s.product_id",
        "base_where":   ("s.stock_type = 'BATCH' "  
                         "AND UPPER(COALESCE(p.main_group,'')) NOT IN ('OPHTHALMIC LENSES','OPHTHALMIC LENS') "  
                         "AND COALESCE(s.is_active,TRUE) = TRUE"),
        "order_by":     "p.product_name, s.batch_no",
        "skip_cols":    {"product_id"},
        "readonly_tables": {"products"},
    },
    "FRAME": {
        # FRAME writes to the legacy 'frames' table (not inventory_stock)
        "tables":       [{"name": "frames", "alias": ""}],
        "order_by":     "product_name, sku_code",
        "skip_cols":    {"image_path"},
    },
    "BLANK": {
        "tables":       [{"name": "blank_inventory", "alias": ""}],
        # Only export active blank rows. Legacy no-base rows are kept inactive
        # for ledger/audit history and must not appear in EDIT/ADD downloads.
        "base_where":   "COALESCE(is_active, TRUE) = TRUE",
        "order_by":     "brand, category, material, add_power, base_recommended",
        "skip_cols":    set(),
    },
    # PRICE is excluded from the bridge — it needs a UNION of inventory_stock
    # (CLENS, stock_type=BATCH) + batches (SOL) which the bridge cannot do.
    # download_manager._fetch_data has a dedicated PRICE block that handles it.
    # "PRICE": intentionally omitted from TABLE_REGISTRY

}

# Columns that should always be UPPER-cased when written to DB
_UPPERCASE_COLS = {"gstin", "pan_no", "tan_no", "cin_no"}


# ═══════════════════════════════════════════════════════════════════════════════
# LiveCol — runtime column descriptor
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class LiveCol:
    db_column:     str
    excel_header:  str          # display name used in Excel (AS alias)
    db_type:       str          # normalised: text/numeric/integer/boolean/date/time/array/uuid/timestamp
    pg_type:       str          # raw postgres type (for precise casting)
    table_name:    str          # which DB table this column belongs to
    table_alias:   str          # alias used in SELECT (e.g. "pt", "pv", "s", "")
    required:      bool  = False
    writable:      bool  = True   # False = download-only (never written on upload)
    source:        str   = "auto" # "registry" | "auto"
    description:   str   = ""
    allowed_values: list = field(default_factory=list)
    default:       Any   = None

    @property
    def qualified(self) -> str:
        """Column name with alias prefix: 'pt.master_name' or 'party_name'."""
        return f"{self.table_alias}.{self.db_column}" if self.table_alias else self.db_column

    @property
    def select_expr(self) -> str:
        """Full SELECT expression with type cast and AS alias."""
        q = self.qualified
        hdr = self.excel_header.replace('"', '')
        if self.db_type == "boolean":
            return f"CASE WHEN {q} THEN 'YES' ELSE 'NO' END AS \"{hdr}\""
        if self.db_type == "date":
            return f"TO_CHAR({q}, 'YYYY-MM-DD') AS \"{hdr}\""
        if self.db_type == "time":
            return f"CAST({q} AS text) AS \"{hdr}\""
        if self.db_type == "array":
            return f"array_to_string({q}, ', ') AS \"{hdr}\""
        if self.db_type == "timestamp":
            return f"TO_CHAR({q}, 'YYYY-MM-DD HH24:MI') AS \"{hdr}\""
        return f'{q} AS "{hdr}"'


# ═══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _query(sql: str, params=None) -> list:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        logger.warning(f"[live_schema_bridge] query failed: {e}")
        return []


def _fetch_table_cols(table_name: str) -> List[dict]:
    """
    Fetch column metadata from information_schema for one table.
    Returns list of {column_name, data_type, is_nullable, column_default}.
    """
    rows = _query(
        """
        SELECT column_name, data_type, is_nullable, column_default,
               ordinal_position
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    return rows


def _get_registry_meta(file_type: str) -> Dict[str, dict]:
    """
    Pull metadata from db_schema_registry for this file_type.
    Returns dict keyed by db_column.
    """
    try:
        from modules.loaders.db_schema_registry import DB_SCHEMA
        meta = {}
        for col in DB_SCHEMA.get(file_type, []):
            if col.db_column:
                meta[col.db_column] = {
                    "excel_header":  col.excel_header,
                    "required":      col.required,
                    "writable":      col.writable,
                    "description":   col.description,
                    "allowed_values":col.allowed_values or [],
                    "default":       col.default,
                    "download":      getattr(col, "download", True),
                }
        return meta
    except Exception as e:
        logger.warning(f"[live_schema_bridge] registry import failed: {e}")
        return {}


def _auto_excel_header(db_column: str) -> str:
    """Convert snake_case DB column name to Title Case header."""
    return db_column.replace("_", " ").title()


# ═══════════════════════════════════════════════════════════════════════════════
# CORE: get_live_schema
# ═══════════════════════════════════════════════════════════════════════════════

def _build_live_schema(file_type: str) -> List[LiveCol]:
    cfg = TABLE_REGISTRY.get(file_type)
    if not cfg:
        logger.warning(f"[live_schema_bridge] Unknown file_type: {file_type}")
        return []

    registry_meta = _get_registry_meta(file_type)
    skip_cols      = _GLOBAL_SYSTEM_COLS | cfg.get("skip_cols", set())
    prefer_table   = cfg.get("prefer_table", {})
    readonly_tbls  = cfg.get("readonly_tables", set())

    # Fetch live columns for every table in this config
    # table_name → {col_name → {data_type, ...}}
    live_tables: Dict[str, Dict[str, dict]] = {}
    for tbl_def in cfg["tables"]:
        tbl = tbl_def["name"]
        if tbl not in live_tables:
            rows = _fetch_table_cols(tbl)
            live_tables[tbl] = {r["column_name"]: r for r in rows}

    live_cols: List[LiveCol] = []
    seen_db_cols: set = set()   # avoid duplicates when col exists in multiple tables

    for tbl_def in cfg["tables"]:
        tbl_name  = tbl_def["name"]
        tbl_alias = tbl_def["alias"]
        writable_table = tbl_name not in readonly_tbls

        for col_name, col_info in live_tables.get(tbl_name, {}).items():
            if col_name in skip_cols:
                continue
            if col_name in seen_db_cols:
                # Col exists in multiple tables — honour prefer_table or skip
                preferred = prefer_table.get(col_name)
                if preferred and preferred != tbl_alias:
                    continue   # already added from preferred table
                # If already added and not preferred, skip duplicate
                if not preferred:
                    continue

            pg_type  = col_info.get("data_type", "text")
            norm_type = _norm_pg_type(pg_type)

            # Skip UUID columns that are cross-table FK references (not user-visible)
            if norm_type == "uuid" and col_name not in ("id",):
                continue
            # Skip timestamp/system UUID entirely
            if norm_type in ("uuid",):
                continue

            # Overlay with registry metadata if available
            meta = registry_meta.get(col_name, {})

            # If registry says download=False, skip entirely
            if not meta.get("download", True):
                continue

            excel_hdr = meta.get("excel_header") or _auto_excel_header(col_name)
            if not excel_hdr:   # empty string in registry (system col) → skip
                continue

            live_cols.append(LiveCol(
                db_column     = col_name,
                excel_header  = excel_hdr,
                db_type       = norm_type,
                pg_type       = pg_type,
                table_name    = tbl_name,
                table_alias   = tbl_alias,
                required      = meta.get("required", False),
                writable      = meta.get("writable", True) and writable_table,
                source        = "registry" if meta else "auto",
                description   = meta.get("description", ""),
                allowed_values= meta.get("allowed_values", []),
                default       = meta.get("default"),
            ))
            seen_db_cols.add(col_name)

    return live_cols


def get_live_schema(file_type: str) -> List[LiveCol]:
    """
    Return the live merged column list for the given file_type.
    DB is authoritative for existence and types.
    Registry provides metadata overlay.
    Results are cached for CACHE_TTL seconds.
    """
    cached = _CACHE.get(file_type)
    if cached and (time.time() - cached["ts"]) < CACHE_TTL:
        return cached["cols"]

    cols = _build_live_schema(file_type)
    _CACHE[file_type] = {"cols": cols, "ts": time.time()}
    return cols


def refresh(file_type: str = None):
    """Clear the schema cache.  Pass file_type to clear one type, or None for all."""
    if file_type:
        _CACHE.pop(file_type, None)
    else:
        _CACHE.clear()
    logger.info(f"[live_schema_bridge] Cache cleared: {file_type or 'ALL'}")


# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD SQL BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_download_sql(file_type: str, filter_where: str = "") -> str:
    """
    Build a complete SELECT SQL for downloading data for this file_type.
    Handles JOINs, type casts, array conversions, and boolean YES/NO.
    filter_where: optional WHERE clause string (caller-supplied, already safe).
    """
    cfg  = TABLE_REGISTRY.get(file_type)
    if not cfg:
        raise ValueError(f"[live_schema_bridge] Unknown file_type: {file_type}")

    cols = get_live_schema(file_type)
    if not cols:
        raise ValueError(f"[live_schema_bridge] No columns resolved for {file_type}")

    select_parts = [c.select_expr for c in cols]

    # FROM clause
    primary_table = cfg["tables"][0]
    if primary_table["alias"]:
        from_clause = f'{primary_table["name"]} {primary_table["alias"]}'
    else:
        from_clause = primary_table["name"]

    join_clause  = cfg.get("join", "")
    base_where   = cfg.get("base_where", "")
    order_by     = cfg.get("order_by", "1")

    # Assemble WHERE
    where_parts = []
    if base_where:
        where_parts.append(base_where)
    if filter_where:
        where_parts.append(filter_where)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = f"""
        SELECT
            {',\n            '.join(select_parts)}
        FROM {from_clause}
        {join_clause}
        {where_clause}
        ORDER BY {order_by}
    """.strip()

    return sql


# ═══════════════════════════════════════════════════════════════════════════════
# UPLOAD COL MAP
# ═══════════════════════════════════════════════════════════════════════════════

def _normalise(s: str) -> str:
    """Lower, strip, remove spaces and underscores — universal normalisation."""
    return re.sub(r"[\s_]", "", str(s).strip().lower())


def get_upload_col_map(file_type: str) -> Dict[str, str]:
    """
    Return a dict of  normalised_header → db_column  for the given file_type.

    Covers:
      1. Registry excel_header → db_column  (e.g. "PARTYNAME" → "party_name")
      2. db_column direct match             (e.g. "party_name" → "party_name")
      3. Auto-generated header              (e.g. "Relation" → "relation")
      4. Common legacy aliases              (handled by static maps in universal_loader_core)

    The upload system merges this with its own static maps, so both old and new
    column names are accepted.
    """
    cols = get_live_schema(file_type)
    col_map: Dict[str, str] = {}

    for c in cols:
        if not c.writable:
            continue
        db = c.db_column
        # excel_header → db_column
        if c.excel_header:
            col_map[_normalise(c.excel_header)] = db
        # db_column direct (snake_case) → db_column
        col_map[_normalise(db)] = db
        # auto_header → db_column (in case user types "Relation" or "relation")
        auto = _auto_excel_header(db)
        col_map[_normalise(auto)] = db

    return col_map


# ═══════════════════════════════════════════════════════════════════════════════
# VALUE COERCION FOR WRITE
# ═══════════════════════════════════════════════════════════════════════════════

def coerce_for_write(val: Any, col: LiveCol) -> Any:
    """
    Convert a raw cell value to the correct Python type for psycopg2.

    Rules:
      boolean  → True / False / None
      numeric  → float / None
      integer  → int / None
      date     → str 'YYYY-MM-DD' / None  (psycopg2 accepts ISO strings for date)
      time     → str 'HH:MM:SS' / None
      array    → list of strings / None
      text     → str / None
      uuid     → str / None   (should not normally be written by user)
    """
    if val is None or (isinstance(val, float) and val != val):
        return col.default   # NaN → default

    if col.db_type == "boolean":
        if isinstance(val, bool):
            return val
        s = str(val).strip().lower()
        return True  if s in ("yes", "1", "true", "y")  else \
               False if s in ("no",  "0", "false", "n") else \
               col.default

    if col.db_type == "numeric":
        try:
            return float(str(val).replace(",", ""))
        except (ValueError, TypeError):
            return col.default or 0.0

    if col.db_type == "integer":
        try:
            return int(float(str(val).replace(",", "")))
        except (ValueError, TypeError):
            return col.default or 0

    if col.db_type == "date":
        import pandas as pd
        try:
            dt = pd.to_datetime(val, dayfirst=False, errors="coerce")
            if pd.isna(dt):
                return None
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    if col.db_type == "time":
        s = str(val).strip()
        return s if s else None

    if col.db_type == "array":
        if isinstance(val, list):
            return val
        parts = [p.strip() for p in str(val).split(",") if p.strip()]
        return parts if parts else None

    # text / fallback
    s = str(val).strip() if val is not None else None
    if not s:
        return col.default
    # Uppercase certain compliance columns
    if col.db_column in _UPPERCASE_COLS:
        s = s.upper()
    return s


# ═══════════════════════════════════════════════════════════════════════════════
# DYNAMIC WRITE HELPERS  (used by universal_loader_core dynamic importers)
# ═══════════════════════════════════════════════════════════════════════════════

def get_writable_cols_for_table(
    file_type: str,
    table_name: str,
    df_columns: List[str],
    exclude: set = None,
) -> List[LiveCol]:
    """
    Return LiveCols that are:
      - writable
      - belong to table_name
      - present in df_columns (after header mapping)
      - not in exclude set
    """
    exclude = exclude or set()
    live = get_live_schema(file_type)
    return [
        c for c in live
        if c.writable
        and c.table_name == table_name
        and c.db_column in df_columns
        and c.db_column not in exclude
        and c.db_type not in ("uuid", "timestamp")
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# GENERIC EXTENSION COLUMN WRITER
# Called at the end of every importer to write any new DB columns
# that are not in the hardcoded core INSERT/UPDATE SQL.
# ═══════════════════════════════════════════════════════════════════════════════

def write_extension_cols(
    table_name: str,
    df: pd.DataFrame,
    where_cols: List[str],
    exclude_cols: set,
    dry_run: bool = False,
) -> int:
    """
    After the core INSERT/UPDATE has run, update any extra DB columns that are:
      - present in df (after header→db_column mapping)
      - exist in the live DB table
      - NOT in exclude_cols (the core columns already written)
      - writable (not uuid/timestamp/system)

    Args:
        table_name:   DB table to UPDATE (e.g. 'parties', 'blank_inventory')
        df:           DataFrame with db_column names as column headers
        where_cols:   List of db_column names used to identify the row in WHERE clause
        exclude_cols: Set of db_column names already handled by core SQL
        dry_run:      If True, skip DB write and return count only

    Returns count of rows updated.
    """
    if dry_run:
        return 0

    try:
        from modules.sql_adapter import run_query, get_transaction_connection, close_connection
        from psycopg2.extras import execute_batch
    except ImportError:
        return 0

    # Fetch live column metadata for this table
    live_cols_rows = _query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table_name,)
    )
    if not live_cols_rows:
        return 0

    db_types = {r["column_name"]: _norm_pg_type(r["data_type"]) for r in live_cols_rows}
    db_col_set = set(db_types.keys())

    # Columns to write: in df AND in DB AND not excluded AND not system AND not WHERE key
    _sys = {"id", "created_at", "updated_at", "created_by", "status", "product_code"}
    ext_cols = [
        col for col in df.columns
        if col in db_col_set
        and col not in exclude_cols
        and col not in _sys
        and col not in where_cols
        and db_types.get(col) not in ("uuid", "timestamp")
    ]

    if not ext_cols:
        return 0

    # Build: UPDATE table SET col1=%s, col2=%s WHERE key1=%s AND key2=%s
    set_clause   = ", ".join(f'"{c}"=%s' for c in ext_cols)
    where_clause = " AND ".join(f'"{c}" IS NOT DISTINCT FROM %s' for c in where_cols)
    sql = f'UPDATE "{table_name}" SET {set_clause} WHERE {where_clause}'

    def _coerce(val, col):
        dtype = db_types.get(col, "text")
        if val is None or (isinstance(val, float) and val != val):
            return None
        if dtype == "boolean":
            if isinstance(val, bool): return val
            s = str(val).strip().lower()
            return True if s in ("yes","1","true","y") else False if s in ("no","0","false","n") else None
        if dtype == "numeric":
            try: return float(str(val).replace(",",""))
            except: return None
        if dtype == "integer":
            try: return int(float(str(val).replace(",","")))
            except: return None
        if dtype == "date":
            import pandas as pd2
            try:
                dt = pd2.to_datetime(val, errors="coerce")
                return None if pd2.isna(dt) else dt.strftime("%Y-%m-%d")
            except: return None
        if dtype == "array":
            if isinstance(val, list): return val
            parts = [p.strip() for p in str(val).split(",") if p.strip()]
            return parts or None
        if dtype == "time":
            return str(val).strip() or None
        s = str(val).strip() if val is not None else None
        if col in _UPPERCASE_COLS and s:
            s = s.upper()
        return s or None

    rows_to_write = []
    for _, row in df.iterrows():
        ext_vals   = [_coerce(row.get(c), c) for c in ext_cols]
        where_vals = [row.get(c) for c in where_cols]
        # Skip rows where any WHERE key is missing
        if any(v is None or (isinstance(v, float) and v != v) for v in where_vals):
            continue
        rows_to_write.append(tuple(ext_vals) + tuple(where_vals))

    if not rows_to_write:
        return 0

    conn = None
    try:
        conn = get_transaction_connection()
        cur  = conn.cursor()
        execute_batch(cur, sql, rows_to_write, page_size=500)
        conn.commit()
        logger.debug(
            f"[live_schema_bridge] extension update: {table_name} "
            f"cols={ext_cols} rows={len(rows_to_write)}"
        )
        return len(rows_to_write)
    except Exception as ex:
        if conn:
            try: conn.rollback()
            except: pass
        logger.warning(f"[live_schema_bridge] write_extension_cols failed on {table_name}: {ex}")
        return 0
    finally:
        if conn:
            try: close_connection(conn)
            except: pass
