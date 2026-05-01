"""
modules/loaders/smart/learning_memory.py
==========================================
Learning Memory — The system gets smarter with every upload.

Every time a user confirms a column rename (IsActive → is_active),
that mapping is stored. The next time the same column appears in ANY
file from the same file type, it is auto-applied WITHOUT showing a
suggestion panel.

This is what makes the system feel "intelligent" over time.

DB Table (auto-created on first use):
    loader_column_memory
    ├── file_type      TEXT    (PRODUCT, CLENS, etc.)
    ├── excel_col      TEXT    (exactly what the user's file had)
    ├── db_col         TEXT    (what it maps to in the DB)
    ├── use_count      INT     (how many times confirmed — confidence proxy)
    ├── confidence     FLOAT   (0.0–1.0, updated on each confirmation)
    ├── last_seen      TIMESTAMPTZ
    └── confirmed_by   TEXT    (who last confirmed — traceability)

Public API:
    record_mappings(file_type, fixes, user)  → save confirmed fixes
    get_learned_fixes(file_type, excel_cols) → {excel_col: db_col} for known cols
    get_confidence(file_type, excel_col)     → float 0.0–1.0
    get_all_memory(file_type)                → List[Dict] for display
    delete_mapping(file_type, excel_col)     → admin: forget a learned mapping
"""

from __future__ import annotations
from typing import Dict, List, Optional


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS loader_column_memory (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    file_type     TEXT         NOT NULL,
    excel_col     TEXT         NOT NULL,
    db_col        TEXT         NOT NULL,
    use_count     INT          NOT NULL DEFAULT 1,
    confidence    FLOAT        NOT NULL DEFAULT 0.7,
    last_seen     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    confirmed_by  TEXT         NOT NULL DEFAULT 'system',
    UNIQUE (file_type, excel_col)
);
CREATE INDEX IF NOT EXISTS idx_lcm_lookup ON loader_column_memory (file_type, excel_col);
"""


def _ensure_table() -> bool:
    """Create table if it doesn't exist. Returns True on success."""
    try:
        from modules.sql_adapter import run_write
        run_write(_DDL)
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def record_mappings(
    file_type: str,
    fixes: Dict[str, str],
    user: str = "system",
) -> int:
    """
    Save confirmed column renames to memory.

    Call this AFTER the user clicks 'Apply Fixes' or after auto-fix is applied.

    Parameters
    ----------
    file_type : e.g. "CLENS", "PRODUCT"
    fixes     : {excel_col: db_col}  — exact same dict as report.auto_fixes
    user      : who confirmed

    Returns number of mappings saved.
    """
    if not fixes:
        return 0
    if not _ensure_table():
        return 0

    try:
        from modules.sql_adapter import run_write
        saved = 0
        for excel_col, db_col in fixes.items():
            if not excel_col or not db_col:
                continue
            # Upsert: increment use_count + update confidence on each confirmation
            run_write(
                """
                INSERT INTO loader_column_memory
                    (file_type, excel_col, db_col, use_count, confidence, last_seen, confirmed_by)
                VALUES (%s, %s, %s, 1, 0.70, NOW(), %s)
                ON CONFLICT (file_type, excel_col) DO UPDATE SET
                    db_col       = EXCLUDED.db_col,
                    use_count    = loader_column_memory.use_count + 1,
                    confidence   = LEAST(
                                     0.99,
                                     loader_column_memory.confidence +
                                       (1.0 - loader_column_memory.confidence) * 0.15
                                   ),
                    last_seen    = NOW(),
                    confirmed_by = EXCLUDED.confirmed_by
                """,
                (file_type, excel_col, db_col, user),
            )
            saved += 1
        return saved
    except Exception:
        return 0


def get_learned_fixes(
    file_type: str,
    excel_cols: List[str],
    min_confidence: float = 0.75,
) -> Dict[str, str]:
    """
    Return learned column mappings for the given file type + columns.

    Only returns mappings with confidence ≥ min_confidence so we don't
    auto-apply shaky guesses from a single past use.

    Parameters
    ----------
    file_type      : file type to look up
    excel_cols     : list of column names in the uploaded file
    min_confidence : minimum stored confidence to auto-apply (default 0.75)

    Returns
    -------
    {excel_col: db_col}  — ready to pass to apply_auto_fixes()
    """
    if not excel_cols:
        return {}
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            """
            SELECT excel_col, db_col, confidence
            FROM   loader_column_memory
            WHERE  file_type  = %s
              AND  confidence >= %s
              AND  excel_col  = ANY(%s)
            """,
            (file_type, min_confidence, list(excel_cols)),
        ) or []
        return {r["excel_col"]: r["db_col"] for r in rows}
    except Exception:
        return {}


def get_confidence(file_type: str, excel_col: str) -> float:
    """Return stored confidence for one mapping. 0.0 if not known."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT confidence FROM loader_column_memory WHERE file_type=%s AND excel_col=%s",
            (file_type, excel_col),
        ) or []
        return float(rows[0]["confidence"]) if rows else 0.0
    except Exception:
        return 0.0


def get_all_memory(file_type: Optional[str] = None) -> List[Dict]:
    """
    Return all learned mappings for display in admin panel.
    If file_type is None, return all file types.
    """
    try:
        from modules.sql_adapter import run_query
        if file_type:
            rows = run_query(
                """
                SELECT file_type, excel_col, db_col,
                       use_count, ROUND(confidence::numeric, 2) AS confidence,
                       TO_CHAR(last_seen, 'DD-MM-YYYY HH24:MI') AS last_seen,
                       confirmed_by
                FROM   loader_column_memory
                WHERE  file_type = %s
                ORDER  BY use_count DESC, confidence DESC
                """,
                (file_type,),
            ) or []
        else:
            rows = run_query(
                """
                SELECT file_type, excel_col, db_col,
                       use_count, ROUND(confidence::numeric, 2) AS confidence,
                       TO_CHAR(last_seen, 'DD-MM-YYYY HH24:MI') AS last_seen,
                       confirmed_by
                FROM   loader_column_memory
                ORDER  BY file_type, use_count DESC
                """,
            ) or []
        return rows
    except Exception:
        return []


def delete_mapping(file_type: str, excel_col: str) -> bool:
    """Admin: forget a learned mapping."""
    try:
        from modules.sql_adapter import run_write
        run_write(
            "DELETE FROM loader_column_memory WHERE file_type=%s AND excel_col=%s",
            (file_type, excel_col),
        )
        return True
    except Exception:
        return False


def get_memory_stats() -> Dict:
    """Return aggregate stats for dashboard display."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            """
            SELECT
                COUNT(*)                                   AS total_mappings,
                COUNT(DISTINCT file_type)                  AS file_types,
                SUM(use_count)                             AS total_applications,
                ROUND(AVG(confidence)::numeric, 2)         AS avg_confidence,
                MAX(last_seen)                             AS last_updated
            FROM loader_column_memory
            """
        ) or [{}]
        return rows[0] if rows else {}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION HELPER
# ══════════════════════════════════════════════════════════════════════════════

def auto_apply_memory(
    file_type: str,
    df,
    report,
    user: str = "system",
):
    """
    One-call helper used in smart_process:

    1. Look up learned mappings for this file type + df columns
    2. Apply them silently (no user action needed)
    3. Log which ones were auto-applied vs still need user review
    4. Return (df, applied_fixes, remaining_fixes)

    Parameters
    ----------
    df      : uploaded DataFrame
    report  : ChangeReport (auto_fixes will be filtered)
    user    : for audit

    Returns
    -------
    (df, auto_applied: dict, needs_review: dict)
    """
    import pandas as pd
    from modules.loaders.smart.schema_validator import apply_auto_fixes

    excel_cols     = list(df.columns)
    learned        = get_learned_fixes(file_type, excel_cols)
    needs_review   = {k: v for k, v in report.auto_fixes.items()
                      if k not in learned}
    auto_applied   = {k: v for k, v in report.auto_fixes.items()
                      if k in learned}

    # Also auto-apply purely learned fixes not in current report.auto_fixes
    for ec, dc in learned.items():
        if ec in df.columns and ec not in auto_applied:
            auto_applied[ec] = dc

    if auto_applied:
        df = apply_auto_fixes(df, auto_applied)
        # Save/reinforce these (they were in df, confirming continued use)
        record_mappings(file_type, auto_applied, user=user)

    return df, auto_applied, needs_review
