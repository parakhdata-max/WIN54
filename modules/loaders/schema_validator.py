"""
modules/loaders/smart/schema_validator.py
==========================================
Schema Validator — Self-diagnosing layer for the smart loader.

Runs on every upload BEFORE change detection. Compares:
  Excel columns  vs  system config (FIELD_CONFIG)  vs  live DB schema

Returns:
  suggestions  : human-readable list of issues (shown in UI)
  auto_fixes   : {excel_col → config_col} safe renames ready to apply
  preview_diff : structured list for the UI diff table (colour-coded)

Safety rules (hardcoded, never bypassed):
  ✅ Only renames columns — never deletes or adds
  ❌ Never auto-deletes DB/config columns
  ❌ Never applies fixes without explicit user confirmation
"""

from typing import Dict, List, Optional


# ── Public API ────────────────────────────────────────────────────────────────

def validate_schema(df, file_type: str, cfg: dict, db_columns: List[str]) -> Dict:
    """
    Compare Excel columns vs system config vs live DB schema.

    Parameters
    ----------
    df          : uploaded DataFrame (post-guard, pre-detect)
    file_type   : e.g. "PRODUCT", "CLENS", "PARTY"
    cfg         : FIELD_CONFIG entry for this file_type
    db_columns  : column names fetched from information_schema (can be [])

    Returns
    -------
    dict with keys:
      "suggestions"  → List[str]       human-readable messages
      "auto_fixes"   → Dict[str, str]  {current_col: target_col} safe renames
      "preview_diff" → List[Dict]      rows for the UI diff table
    """
    # Strip the 🔒 lock prefix download_manager adds to locked headers
    df_cols  = {c.replace("🔒 ", "").strip() for c in df.columns}
    cfg_cols = set(cfg.get("columns", []))
    db_cols  = set(db_columns)

    suggestions:  List[str]       = []
    auto_fixes:   Dict[str, str]  = {}
    preview_diff: List[Dict]      = []

    # ── 1. Excel column → config match ───────────────────────────────────────
    for col in sorted(df_cols):
        if col in cfg_cols:
            continue  # exact match — no action needed

        match = _suggest_match(col, cfg_cols)

        if match:
            # Fuzzy match found (e.g. "IsActive" → "is_active")
            suggestions.append(f"🔁 '{col}' looks like '{match}' → rename recommended")
            auto_fixes[col] = match
            preview_diff.append({
                "Current Column":   col,
                "Suggested Column": match,
                "Action":           "Rename",
            })
        else:
            # Column not tracked at all
            suggestions.append(f"➕ '{col}' not tracked by system config")
            preview_diff.append({
                "Current Column":   col,
                "Suggested Column": "—",
                "Action":           "Ignored",
            })

    # ── 2. Config columns missing in the uploaded file ────────────────────────
    locked = set(cfg.get("locked_cols", []))
    for col in sorted(cfg_cols - df_cols):
        if col in locked:
            continue  # locked cols are identity fields — expected to be there via alias
        if col not in auto_fixes.values():  # don't double-warn if already being fixed
            suggestions.append(f"⚠️ Expected column '{col}' is missing in your file")

    # ── 3. Config columns that don't exist in live DB ─────────────────────────
    if db_cols:
        for col in sorted(cfg_cols):
            if col not in db_cols:
                suggestions.append(
                    f"❌ '{col}' is in system config but NOT found in live DB — "
                    "may need a schema migration"
                )

    return {
        "suggestions":  suggestions,
        "auto_fixes":   auto_fixes,
        "preview_diff": preview_diff,
    }


def apply_auto_fixes(df, fixes: Dict[str, str]):
    """
    Apply column renames to df.

    Safe by design:
      - Only renames — never adds, drops, or reorders columns
      - Skips keys that aren't in df.columns (idempotent)
      - Returns a new df (original is not mutated)
    """
    if not fixes:
        return df
    valid_fixes = {k: v for k, v in fixes.items() if k in df.columns}
    return df.rename(columns=valid_fixes)


def get_db_columns(table_name: str) -> List[str]:
    """
    Fetch live column names for a DB table via information_schema.

    Returns [] on failure (no DB connection, unknown table, etc.)
    so the rest of the pipeline degrades gracefully.
    """
    if not table_name:
        return []
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table_name,),
        ) or []
        return [r["column_name"] for r in rows]
    except Exception:
        return []


# ── Internal helpers ──────────────────────────────────────────────────────────

def _suggest_match(col: str, candidates) -> Optional[str]:
    """
    Fuzzy-match `col` against `candidates` by normalising to lowercase alphanum.
    "IsActive" and "is_active" both normalise to "isactive" → match.
    Returns the best candidate string or None.
    """
    col_norm = _norm(col)
    for c in candidates:
        if _norm(c) == col_norm:
            return c
    return None


def _norm(s: str) -> str:
    """Lowercase + strip spaces, underscores, and hyphens."""
    return s.lower().replace(" ", "").replace("_", "").replace("-", "")
