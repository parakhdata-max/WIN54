"""
patches/loader_transaction_wrapper.py
=======================================
PATCH 1 — IMPORT TRANSACTION WRAPPER + IMPORT ID TRACKING + ROW DEDUP
======================================================================

Drop-in replacement for run_loader() in universal_loader_core.py.

What this adds vs the original run_loader():

  1. TRANSACTION WRAPPING
     Every import is wrapped in a single DB transaction.
     Any crash → full rollback. No half-loaded files ever.

  2. IMPORT ID TRACKING
     Every import gets a UUID (import_id) written to loader_import_log.
     Attach this ID to audit tables when you extend loader internals.

  3. ROW HASHING (duplicate protection)
     Each Excel row is hashed (MD5 of sorted key+value pairs).
     Hashes stored in loader_row_history.
     Duplicate uploads → skipped rows with a warning.
     Prevents double-loading the same file in ADD mode.

  4. HARD CAP (safety rail)
     MAX_ROWS_PER_IMPORT = 50_000
     Reject files that exceed this before any DB work.

USAGE — replace the single run_loader() call in your UI / loader_ui.py:

    from patches.loader_transaction_wrapper import run_loader_safe

    result = run_loader_safe(
        file_path   = path,
        mode        = "LIVE",        # DRY | SHADOW | LIVE
        stock_mode  = "ADD",         # ADD | OPENING
        force_type  = None,
        user        = "admin",       # any identifier string
    )
    print(result.import_id)          # UUID for audit trail

SQL to run ONCE in your DB before using this patch:
------------------------------------------------------
    CREATE TABLE IF NOT EXISTS loader_import_log (
        import_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        file_name    TEXT,
        file_type    TEXT,
        mode         TEXT,
        stock_mode   TEXT,
        "user"       TEXT,
        imported_at  TIMESTAMPTZ DEFAULT NOW(),
        rows_total   INT,
        rows_ok      INT,
        rows_skipped INT,
        error_count  INT,
        duration_s   NUMERIC(10,3),
        status       TEXT          -- 'OK' | 'PARTIAL' | 'FAILED' | 'DRY'
    );

    CREATE TABLE IF NOT EXISTS loader_row_history (
        row_hash     CHAR(32) PRIMARY KEY,
        import_id    UUID REFERENCES loader_import_log(import_id),
        file_type    TEXT,
        imported_at  TIMESTAMPTZ DEFAULT NOW()
    );
------------------------------------------------------
"""

import hashlib
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

from modules.sql_adapter import get_connection, run_write

# Import the original loader machinery
from modules.loaders.universal_loader_core import (
    LoadResult,
    LOADER_MAP,
    STOCK_MODE_ADD,
    STOCK_MODE_PRICE_ONLY,   # FIX: needed for dedup bypass logic
    _resolve_stock_mode,
    detect_file_type,
    load_excel,
)

logger = logging.getLogger(__name__)

# ── Safety cap ────────────────────────────────────────────────────────────────
MAX_ROWS_PER_IMPORT = 50_000


# ══════════════════════════════════════════════════════════════════════════════
# ROW HASHING
# ══════════════════════════════════════════════════════════════════════════════

def _row_hash(row_dict: dict) -> str:
    """
    MD5 of the sorted key=value pairs of a row.
    Stable across runs — same data = same hash.
    """
    payload = str(sorted(row_dict.items())).encode("utf-8")
    return hashlib.md5(payload).hexdigest()


def _load_seen_hashes(file_type: str) -> set:
    """
    Fetch already-imported hashes for this file_type from the DB.
    Returns a set of hex strings.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT row_hash FROM loader_row_history WHERE file_type = %s",
            (file_type,)
        )
        return {r["row_hash"] for r in (rows or [])}
    except Exception as e:
        logger.warning(f"Could not load row history (dedup disabled): {e}")
        return set()


def _save_hashes(import_id: str, file_type: str, hashes: list):
    """Persist new row hashes after a successful import."""
    if not hashes:
        return
    try:
        for h in hashes:
            run_write(
                """
                INSERT INTO loader_row_history (row_hash, import_id, file_type)
                VALUES (%s, %s, %s)
                ON CONFLICT (row_hash) DO NOTHING
                """,
                (h, import_id, file_type)
            )
    except Exception as e:
        logger.warning(f"Could not save row hashes: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# IMPORT LOG
# ══════════════════════════════════════════════════════════════════════════════

def _log_import(import_id: str, result: LoadResult, file_name: str, user: str, status: str):
    """Write one row to loader_import_log."""
    try:
        run_write(
            """
            INSERT INTO loader_import_log
                (import_id, file_name, file_type, mode, stock_mode,
                 "user", rows_total, rows_ok, rows_skipped,
                 error_count, duration_s, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (import_id) DO UPDATE SET
                rows_total   = EXCLUDED.rows_total,
                rows_ok      = EXCLUDED.rows_ok,
                rows_skipped = EXCLUDED.rows_skipped,
                error_count  = EXCLUDED.error_count,
                duration_s   = EXCLUDED.duration_s,
                status       = EXCLUDED.status
            """,
            (
                import_id,
                file_name,
                result.file_type,
                result.mode,
                result.stock_mode,
                user,
                result.total_rows,
                result.inserted + result.updated,
                result.skipped,
                len(result.errors),
                result.duration_seconds,
                status,
            )
        )
    except Exception as e:
        logger.warning(f"Could not write import log: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TRANSACTIONAL INNER RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def _run_loader_internal(df, file_type, result, dry_run, env_tag, effective_stock_mode, seen_hashes):
    """
    Wraps the per-type loader with:
      - per-row hash dedup
      - collection of new hashes to persist later

    Returns list of new hashes written during this import.
    """
    new_hashes = []
    dup_count = 0

    # Inject dedup into each row before calling the loader
    # We shadow the DataFrame to add a _skip_dedup column
    row_dicts = [row.to_dict() for _, row in df.iterrows()]
    skip_indices = set()

    for idx, rd in enumerate(row_dicts):
        h = _row_hash(rd)
        if h in seen_hashes:
            skip_indices.add(idx)
            dup_count += 1
        else:
            new_hashes.append(h)
            seen_hashes.add(h)  # prevent within-batch duplication

    if dup_count:
        result.add_warning(
            f"⚠️ {dup_count} duplicate row(s) detected (same data already imported) — skipped. "
            "If this is intentional, use OPENING mode or clear row history."
        )

    # Filter out already-seen rows
    if skip_indices and not dry_run:
        import pandas as pd
        df = df.iloc[[i for i in range(len(df)) if i not in skip_indices]].reset_index(drop=True)

    loader_fn = LOADER_MAP.get(file_type)
    loader_fn(df, result, dry_run, env_tag, effective_stock_mode)

    return new_hashes


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT — replaces run_loader()
# ══════════════════════════════════════════════════════════════════════════════

def run_loader_safe(
    file_path:          str,
    mode:               str = "DRY",
    stock_mode:         str = "ADD",
    force_type:         Optional[str] = None,
    user:               str = "system",
    skip_dedup:         bool = False,    # set True to bypass hash check (e.g. OPENING reset)
    progress_callback   = None,
) -> LoadResult:
    """
    Transactional, dedup-aware, audit-logged replacement for run_loader().

    Differences vs original run_loader():
      - Wraps LIVE/SHADOW imports in a single DB transaction
      - Auto-rollback on any crash → no partial imports
      - Row hash dedup prevents double-importing the same Excel
      - Import ID (UUID) logged to loader_import_log for full traceability
      - Hard row cap (MAX_ROWS_PER_IMPORT) as a safety rail
      - result.import_id available after call
    """
    import_id = str(uuid.uuid4())
    dry_run   = (mode == "DRY")
    env_tag   = "SHADOW" if mode == "SHADOW" else "LIVE"
    file_name = os.path.basename(file_path)

    # ── 1. Load Excel ─────────────────────────────────────────────────────────
    try:
        df, ingestion_report = load_excel(file_path)  # FIX: unpack (df, IngestionReport) tuple
        logger.debug(f"[{import_id}] file_type={detect_file_type(df)} cols={list(df.columns)} rows={len(df)}")
    except Exception as e:
        result = LoadResult("UNKNOWN", mode)
        result.import_id = import_id
        result.add_error(0, "file", f"Cannot read file: {e}")
        result.finish()
        _log_import(import_id, result, file_name, user, "FAILED")
        return result

    # ── 2. Detect type ────────────────────────────────────────────────────────
    file_type = force_type or detect_file_type(df)
    effective_stock_mode = _resolve_stock_mode(stock_mode, file_type)
    result = LoadResult(file_type, mode, stock_mode=effective_stock_mode)
    result.import_id = import_id  # expose for callers
    result.ingestion_report = ingestion_report   # FIX: expose quality report to UI
    for w in ingestion_report.warnings:          # FIX: surface ingestion warnings
        result.add_warning(w)

    if file_type == "UNKNOWN":
        result.add_error(0, "file_type", "Cannot detect file type. Check required columns.")
        result.finish()
        _log_import(import_id, result, file_name, user, "FAILED")
        return result

    loader_fn = LOADER_MAP.get(file_type)
    if not loader_fn:
        result.add_error(0, "file_type", f"No loader registered for: {file_type}")
        result.finish()
        _log_import(import_id, result, file_name, user, "FAILED")
        return result

    # ── 3. Hard cap ───────────────────────────────────────────────────────────
    if len(df) > MAX_ROWS_PER_IMPORT:
        result.add_error(
            0, "row_count",
            f"File has {len(df):,} rows — exceeds hard cap of {MAX_ROWS_PER_IMPORT:,}. "
            "Split into smaller files."
        )
        result.finish()
        _log_import(import_id, result, file_name, user, "FAILED")
        return result

    # ── 4. Load seen hashes for dedup ─────────────────────────────────────────
    seen_hashes = set()
    # Master data loaders (PARTY, PATIENT, SOL) always upsert — never skip on hash.
    # Dedup only applies to stock loaders (OPHLENS, CLENS, BLANK) where re-importing
    # the same batch file would double the inventory.
    DEDUP_EXEMPT = {"PARTY", "PATIENT", "SOL", "PRODUCT", "MAIN_GROUPS"}
    _skip_dedup_for_type = file_type in DEDUP_EXEMPT
    if not skip_dedup and not _skip_dedup_for_type and not dry_run and effective_stock_mode != STOCK_MODE_PRICE_ONLY:
        seen_hashes = _load_seen_hashes(file_type)

    # ── 5. DRY RUN (no transaction needed) ───────────────────────────────────
    if dry_run:
        try:
            new_hashes = _run_loader_internal(
                df, file_type, result, True, env_tag, effective_stock_mode, seen_hashes
            )
        except Exception as e:
            result.add_error(0, "loader", f"Dry-run validation crashed: {e}")

        result.finish()
        _log_import(import_id, result, file_name, user, "DRY")
        return result

    # ── 6. LIVE / SHADOW — wrapped in a transaction ───────────────────────────
    new_hashes = []
    import_status = "OK"
    conn = None

    try:
        from modules.sql_adapter import get_transaction_connection, close_connection
        conn = get_transaction_connection()   # autocommit=False — required for transaction
        try:
            new_hashes = _run_loader_internal(
                df, file_type, result, False, env_tag, effective_stock_mode, seen_hashes
            )
            conn.commit()
            logger.info(
                f"[{import_id}] Import committed: {file_type} "
                f"ins={result.inserted} upd={result.updated} skip={result.skipped}"
            )
        except Exception as e:
            try: conn.rollback()
            except: pass
            result.add_error(0, "transaction", f"Import rolled back due to: {e}")
            import_status = "FAILED"
            logger.error(f"[{import_id}] Import ROLLED BACK: {e}")
        finally:
            try: close_connection(conn)
            except: pass

    except Exception as conn_err:
        result.add_error(0, "connection", f"DB connection failed: {conn_err}")
        import_status = "FAILED"

    # Partial success detection
    if import_status == "OK" and result.errors:
        import_status = "PARTIAL"

    # ── 7. Persist hashes + log ───────────────────────────────────────────────
    if import_status in ("OK", "PARTIAL") and new_hashes:
        _save_hashes(import_id, file_type, new_hashes)

    result.finish()
    _log_import(import_id, result, file_name, user, import_status)

    return result
