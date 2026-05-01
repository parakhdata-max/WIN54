"""
modules/loaders/schema_guard.py
=================================
Schema Evolution Engine — WIN16 DV ERP
Stop → Diff → Preview → Approve → Go

Responsibilities:
  - Detect new / missing / renamed / filled columns vs known schema
  - Fuzzy-match unknown columns to DB column names
  - Track schema snapshots per file_type (in-memory per session)
  - Generate AI advisor commentary for each import
  - Produce change reports

SQL audit table (optional — enables cross-session history):

    CREATE TABLE IF NOT EXISTS loader_schema_history (
        id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        file_type    TEXT,
        file_name    TEXT,
        change_summary JSONB,
        approved_by  TEXT DEFAULT 'user',
        approved_at  TIMESTAMP DEFAULT NOW()
    );
"""

import logging
import json
from datetime import datetime
from difflib import get_close_matches, SequenceMatcher
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# KNOWN SCHEMA CONTRACT
# These are the NORMALIZED (lowercased, no spaces) column names
# the loader expects per file type — after header normalization.
# ═══════════════════════════════════════════════════════

KNOWN_SCHEMA: Dict[str, Set[str]] = {
    "products": {
        "productname", "maingroup", "type", "lenscategory", "brand", "brandproductgroup",
        "material", "index", "coating", "coatingtype", "colour", "color", "gender",
        "wearschedule", "unit", "isbatchapplicable", "iseyespecific",
        "isactive", "hsncode", "boxsize", "allowloose", "gstpercent",
    },
    "FRAME": {
        "skucode", "sku", "product", "productname", "model", "brand",
        "asize", "sizea", "dbl", "templelength", "basematerial", "finish",
        "colour", "color", "shape", "qty", "quantity", "costprice", "mrp",
        "imagepath", "isactive",
    },
    "PARTY": {
        "partyname", "roletype", "mobile", "address", "city", "area", "isactive",
        "dupbymobile", "needsreview",
    },
    "PATIENT": {
        "clientname", "mobilenumber", "recordno", "date",
        "rightsph", "rightcyl", "rightaxis", "rightaddpower",
        "leftsph", "leftcyl", "leftaxis", "leftaddpower", "clientname1",
    },
    "OPHLENS": {
        "product", "productname", "sph", "cyl", "axis", "add", "addpower",
        "eyeside", "batchno", "expirydate", "qty", "quantity",
        "purchaserate", "costprice", "sellingprice", "mrp",
        "isactive", "lenscategory", "wearschedule", "lensdesign", "location",
    },
    "CLENS": {
        "product", "productname", "sph", "cyl", "axis", "add", "addpower",
        "eyeside", "batchno", "expirydate", "qty", "quantity",
        "purchaserate", "costprice", "sellingprice", "mrp",
        "isactive", "lenscategory", "wearschedule", "lensdesign",
    },
    "SOL": {
        "product", "productname", "batchno", "expirydate", "qty", "quantity",
        "costprice", "sellingprice", "mrp", "isactive",
    },
    "BLANK": {
        "add", "qtyright", "qtyleft", "qtyindependent",
        "recomendedbase", "recommendedbase",
        "base1p", "base2p", "base3p",
        "category", "material", "colour", "color", "brand",
    },
}

# All DB column names across all tables — for fuzzy mapping
ALL_DB_COLUMNS = {
    "product_name", "brand", "brand_group", "main_group", "category",
    "lens_category", "material", "index_value", "coating", "coating_type",
    "colour", "gender", "wear_schedule", "unit", "is_batch_applicable",
    "is_eye_specific", "is_active", "hsn_code", "box_size", "allow_loose",
    "sku_code", "model", "size_a", "dbl", "temple_length", "base_material",
    "finish", "shape", "qty", "cost_price", "mrp", "image_path",
    "party_name", "party_type", "mobile", "address", "city", "area",
    "master_name", "record_no", "visit_date", "right_sph", "right_cyl",
    "right_axis", "right_add", "left_sph", "left_cyl", "left_axis", "left_add",
    "sph", "cyl", "axis", "add_power", "eye_side", "batch_no", "expiry_date",
    "quantity", "purchase_rate", "selling_price", "stock_type", "lens_design",
    "location", "qty_right", "qty_left", "qty_independent",
    "base_recommended", "base_1", "base_2", "base_3",
}

# In-memory snapshot: last seen columns per file_type in this session
# Structure: { file_type: { "cols": set, "empty_cols": set } }
# empty_cols = columns that were all-null in the previous upload
_SCHEMA_SNAPSHOTS: Dict[str, dict] = {}


# ═══════════════════════════════════════════════════════
# CORE ANALYSIS
# ═══════════════════════════════════════════════════════

class SchemaDiff:
    """Result of comparing uploaded file columns vs known schema."""

    def __init__(self, file_type: str, actual_cols: Set[str]):
        self.file_type = file_type
        self.actual_cols = actual_cols
        self.expected_cols = KNOWN_SCHEMA.get(file_type, set())

        self.new_columns: List[str] = []         # in file, not in schema
        self.missing_columns: List[str] = []     # in schema, not in file
        self.newly_filled: List[str] = []        # was empty last time, now has data
        self.suggestions: Dict[str, str] = {}    # new_col → suggested_db_col
        self.confidence: Dict[str, float] = {}   # suggestion confidence 0–1
        self.ignored_cols: List[str] = []        # cols loader explicitly ignores
        self.safe_to_proceed: bool = True

        # Cols that loader always ignores regardless
        self._ignored_always = {"dupbymobile", "needsreview", "clientname1"}

    def has_changes(self) -> bool:
        return bool(self.new_columns or self.missing_columns or self.newly_filled)

    def has_blocking_changes(self, strict_mode: bool = False) -> bool:
        if strict_mode:
            return bool(self.new_columns)
        return False  # In normal mode, changes are warnings not blockers

    def to_dict(self) -> dict:
        return {
            "file_type":       self.file_type,
            "new_columns":     self.new_columns,
            "missing_columns": self.missing_columns,
            "newly_filled":    self.newly_filled,
            "suggestions":     self.suggestions,
            "confidence":      self.confidence,
            "safe_to_proceed": self.safe_to_proceed,
        }


def _normalize(col: str) -> str:
    return col.strip().lower().replace(" ", "").replace("_", "")


def _fuzzy_score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def suggest_mapping(unknown_col: str, known_cols: Set[str], threshold: float = 0.6) -> Tuple[Optional[str], float]:
    """
    Suggest best DB column match for an unknown column name.
    Returns (best_match, confidence_score) or (None, 0.0)
    """
    # Normalize for comparison
    norm = _normalize(unknown_col)
    norm_known = {_normalize(k): k for k in known_cols}

    # Direct match after normalization
    if norm in norm_known:
        return norm_known[norm], 1.0

    # difflib close match on normalized names
    matches = get_close_matches(norm, list(norm_known.keys()), n=1, cutoff=threshold)
    if matches:
        best = matches[0]
        score = _fuzzy_score(norm, best)
        return norm_known[best], round(score, 2)

    # Try all-DB-columns fuzzy
    norm_db = {_normalize(k): k for k in ALL_DB_COLUMNS}
    matches2 = get_close_matches(norm, list(norm_db.keys()), n=1, cutoff=threshold)
    if matches2:
        best2 = matches2[0]
        score2 = _fuzzy_score(norm, best2)
        return norm_db[best2], round(score2, 2)

    return None, 0.0


def analyze_schema(
    df: pd.DataFrame,
    file_type: str,
    check_fill: bool = True,
) -> SchemaDiff:
    """
    Main entry point — analyze uploaded file against known schema.
    Returns SchemaDiff with full analysis.
    """
    actual = set(df.columns)
    diff = SchemaDiff(file_type, actual)

    expected = KNOWN_SCHEMA.get(file_type, set())

    # New columns (in file, not in known schema)
    raw_new = actual - expected
    for col in raw_new:
        if col in diff._ignored_always:
            diff.ignored_cols.append(col)
        else:
            diff.new_columns.append(col)
            # Suggest mapping
            suggestion, confidence = suggest_mapping(col, ALL_DB_COLUMNS)
            if suggestion:
                diff.suggestions[col] = suggestion
                diff.confidence[col] = confidence

    # Missing columns (in schema, not in file) — only required ones
    from modules.loaders.universal_loader_core import REQUIRED_COLUMNS
    required = set(REQUIRED_COLUMNS.get(file_type, []))
    # Normalize required cols for comparison
    norm_req = {_normalize(r) for r in required}
    norm_actual = {_normalize(c) for c in actual}
    for req in norm_req:
        if req not in norm_actual:
            diff.missing_columns.append(req)

    # Optional missing (informational only)
    optional_missing = expected - actual - diff._ignored_always
    # Already captured new_columns; missing = expected but absent
    # Re-derive: columns in expected but not in actual = genuinely missing
    # (new_columns are in actual but not expected — opposite)
    all_missing = expected - actual
    optional_only_missing = all_missing - {_normalize(r) for r in required}

    # Newly filled columns — ONLY flag if column was all-null in previous upload
    # prev snapshot stores which columns were empty last time
    if check_fill and file_type in _SCHEMA_SNAPSHOTS:
        prev_snap     = _SCHEMA_SNAPSHOTS[file_type]
        prev_cols     = prev_snap.get("cols",       set())
        prev_empty    = prev_snap.get("empty_cols", set())   # cols that were all-null last time
        for col in actual:
            if col not in prev_cols:
                continue   # truly new column, handled above as new_column
            if col not in prev_empty:
                continue   # was already filled last time — not newly filled
            # Was empty last upload — check if now has data
            try:
                null_pct = df[col].isna().mean()
                if null_pct < 0.99:   # at least 1% filled = genuinely new data
                    diff.newly_filled.append(col)
            except Exception:
                pass

    # Update snapshot — record which columns are empty NOW (for next upload comparison)
    empty_now = set()
    for col in actual:
        try:
            if df[col].isna().all():
                empty_now.add(col)
        except Exception:
            pass
    _SCHEMA_SNAPSHOTS[file_type] = {"cols": actual, "empty_cols": empty_now}

    # Strict mode: block if unknown columns
    diff.safe_to_proceed = not bool(diff.missing_columns)

    return diff


def save_schema_history(
    file_type: str,
    file_name: str,
    diff: SchemaDiff,
    approved_by: str = "user",
    schema_snapshot: Optional[set] = None,
) -> bool:
    """
    Save schema change record to DB audit table.

    Args:
        file_type:       Loader type (PRODUCT, FRAME, etc.)
        file_name:       Original uploaded filename
        diff:            SchemaDiff result from analyze_schema()
        approved_by:     Operator who approved — defaults to 'user', pass session user
        schema_snapshot: Raw column set seen in this file — stored for permanent reference
    """
    try:
        from modules.sql_adapter import run_write

        # Build snapshot — use diff.actual_cols if not explicitly passed
        snapshot = list(schema_snapshot or diff.actual_cols)

        run_write("""
            INSERT INTO loader_schema_history
            (file_type, file_name, change_summary, approved_by, approved_at)
            VALUES (%s, %s, %s, %s, NOW())
        """, (
            file_type,
            file_name,
            json.dumps({
                **diff.to_dict(),
                "schema_snapshot": snapshot,     # full column list embedded in change_summary
            }),
            approved_by,
        ))
        logger.info(f"[SCHEMA] History saved: {file_type} | {file_name} | by={approved_by}")
        return True
    except Exception as e:
        logger.debug(f"Could not save schema history: {e}")
        return False


def get_schema_history(file_type: str = None, limit: int = 20) -> List[dict]:
    """Retrieve recent schema change history from DB."""
    try:
        from modules.sql_adapter import run_query
        # Use COALESCE for file_name in case column doesn't exist yet in older DBs
        _sel = "SELECT file_type, change_summary, approved_at"
        if file_type:
            rows = run_query(f"""
                {_sel}
                FROM loader_schema_history
                WHERE file_type=%s
                ORDER BY approved_at DESC LIMIT %s
            """, (file_type, limit))
        else:
            rows = run_query(f"""
                {_sel}
                FROM loader_schema_history
                ORDER BY approved_at DESC LIMIT %s
            """, (limit,))
        return [dict(r) for r in rows]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════
# AI ADVISOR ENGINE
# ═══════════════════════════════════════════════════════

def generate_ai_advice(
    diff: SchemaDiff,
    df: pd.DataFrame,
    file_type: str,
    row_count: int,
    mode: str,
    stock_mode: str,
) -> List[dict]:
    """
    Generate context-aware advisory messages.
    Each advice item: {level: 'info'|'warn'|'error'|'tip', message: str, icon: str}
    """
    advice = []

    def _add(level, icon, msg):
        advice.append({"level": level, "icon": icon, "message": msg})

    # ── Row count advisories ──────────────────────────────────────────────────
    if row_count == 0:
        _add("error", "🚫", "File has zero data rows. Nothing to import.")
    elif row_count > 5000:
        _add("warn", "⏳", f"Large file: {row_count:,} rows. Import may take 1–3 minutes. Don't close the tab.")
    elif row_count > 1000:
        _add("info", "📊", f"{row_count:,} rows detected. Medium-sized import — should complete in under 30 seconds.")
    else:
        _add("info", "✅", f"{row_count:,} rows ready. Small file — fast import expected.")

    # ── Schema change advisories ──────────────────────────────────────────────
    if diff.new_columns:
        for col in diff.new_columns:
            if col in diff.suggestions:
                conf = diff.confidence.get(col, 0)
                conf_pct = int(conf * 100)
                _add("warn", "🔍",
                     f"Unknown column '{col}' detected. "
                     f"Best match: '{diff.suggestions[col]}' ({conf_pct}% confidence). "
                     f"Loader will ignore this column unless you manually remap it.")
            else:
                _add("warn", "❓",
                     f"Column '{col}' not recognized and has no close match. "
                     f"It will be ignored during import. Check the Schema Reference tab.")

    if diff.missing_columns:
        for col in diff.missing_columns:
            _add("error", "🚨",
                 f"Required column '{col}' is MISSING from your file. "
                 f"Import will fail without it. Add this column to your Excel.")

    if diff.newly_filled:
        _add("info", "🟡",
             f"Previously empty columns now have data: {', '.join(diff.newly_filled[:5])}. "
             f"These will be imported for the first time.")

    # ── File-type specific tips ───────────────────────────────────────────────
    if file_type in ("OPHLENS", "CLENS"):
        # Check TORIC rows
        try:
            cyl_col = next((c for c in df.columns if "cyl" in c), None)
            axis_col = next((c for c in df.columns if "axis" in c), None)
            if cyl_col and axis_col:
                toric = df[df[cyl_col].notna() & (df[cyl_col].astype(str) != "0")]
                missing_axis = toric[df[axis_col].isna() | (df[axis_col].astype(str) == "0")]
                if len(missing_axis) > 0:
                    _add("error", "🌀",
                         f"{len(missing_axis)} TORIC rows have CYL but missing AXIS. "
                         f"These rows will be skipped. Fix AXIS values before importing.")
                else:
                    toric_count = len(toric)
                    if toric_count > 0:
                        _add("tip", "🌀", f"{toric_count} TORIC rows detected — all have AXIS values. Good.")
        except Exception:
            pass

    if file_type == "PRODUCT":
        # Check for trailing spaces in product names
        try:
            pname_col = next((c for c in df.columns if "product" in c), None)
            if pname_col:
                with_spaces = df[pname_col].astype(str).str.startswith(" ").sum()
                if with_spaces > 0:
                    _add("warn", "⚠️", f"{with_spaces} product names start with a space. Loader will strip them automatically.")
        except Exception:
            pass

    if file_type == "PARTY":
        # Check mobile null rate
        try:
            mob_col = next((c for c in df.columns if "mobile" in c), None)
            if mob_col:
                null_pct = int(df[mob_col].isna().mean() * 100)
                if null_pct > 50:
                    _add("info", "📱", f"{null_pct}% of party rows have no mobile. Conflict key will fall back to party name.")
        except Exception:
            pass

    if file_type == "PATIENT":
        # Identity completeness check
        try:
            mob_col = next((c for c in df.columns if "mobile" in c.lower()), None)
            rec_col = next((c for c in df.columns if "record" in c.lower()), None)
            if mob_col and rec_col:
                both_null = df[df[mob_col].isna() & df[rec_col].isna()]
                if len(both_null) > 0:
                    _add("error", "🆔",
                         f"{len(both_null)} patient rows have BOTH mobile and record_no empty. "
                         f"These rows will be SKIPPED — they have no identity key.")
        except Exception:
            pass

    if file_type == "BLANK":
        # Check ADD power range
        try:
            add_col = next((c for c in df.columns if c in ("add", "addpower")), None)
            if add_col:
                invalid = df[pd.to_numeric(df[add_col], errors="coerce").abs() > 99.99]
                if len(invalid) > 0:
                    _add("error", "🔢", f"{len(invalid)} rows have invalid ADD power (>99.99). These will be skipped.")
        except Exception:
            pass

    # ── Mode-specific advisories ──────────────────────────────────────────────
    if mode == "DRY":
        _add("tip", "🧪", "DRY RUN mode — no data will be written. Safe to run as many times as needed.")

    if mode == "SHADOW":
        _add("tip", "🔵", "SHADOW mode — writes to DB with environment_tag='SHADOW'. Will not affect live orders or billing.")

    if mode == "LIVE" and row_count > 100:
        _add("warn", "🔴",
             "LIVE mode with a large file. Once complete, DO NOT re-import — it will duplicate data. "
             "Run Audit → Integrity Checks immediately after.")

    if stock_mode == "OPENING":
        _add("warn", "🔄",
             "OPENING mode ACTIVE — existing stock quantities will be OVERWRITTEN. "
             "This is irreversible. Confirm your Excel quantities are correct before proceeding.")

    # ── Data quality checks ───────────────────────────────────────────────────
    try:
        total_cells = df.size
        null_cells = df.isna().sum().sum()
        null_pct = round(null_cells / total_cells * 100, 1) if total_cells > 0 else 0
        if null_pct > 60:
            _add("warn", "⬜", f"File is {null_pct}% empty cells overall. Check if your Excel exported correctly.")
        elif null_pct > 30:
            _add("info", "⬜", f"{null_pct}% of cells are empty — review optional fields if needed.")
    except Exception:
        pass

    # ── Duplicate check ───────────────────────────────────────────────────────
    key_col_map = {
        "PRODUCT": "product",
        "FRAME":   "skucode",
        "PARTY":   "partyname",
    }
    key_col = key_col_map.get(file_type)
    if key_col and key_col in df.columns:
        try:
            dup_count = df[key_col].duplicated().sum()
            if dup_count > 0:
                _add("error", "🔁",
                     f"{dup_count} duplicate {key_col} values in your Excel. "
                     f"Both rows will be skipped. Fix duplicates at source.")
        except Exception:
            pass

    # ── Import order reminder ─────────────────────────────────────────────────
    if file_type in ("OPHLENS", "CLENS", "SOL", "FRAME"):
        _add("tip", "📋",
             "Reminder: Products must be imported BEFORE stock files. "
             "If you see 'Product not found' errors, run product_master.xlsx first.")

    return advice


# ═══════════════════════════════════════════════════════
# COLUMN FILL ANALYSIS
# ═══════════════════════════════════════════════════════

def analyze_column_quality(df: pd.DataFrame, file_type: str) -> List[dict]:
    """
    Per-column quality report for the preview panel.
    Returns list of {col, filled_pct, sample_values, status}
    """
    report = []
    from modules.loaders.universal_loader_core import REQUIRED_COLUMNS
    required = set(REQUIRED_COLUMNS.get(file_type, []))
    known = KNOWN_SCHEMA.get(file_type, set())

    for col in df.columns:
        try:
            total = len(df)
            filled = df[col].notna().sum()
            fill_pct = round(filled / total * 100, 1) if total > 0 else 0
            samples = df[col].dropna().head(3).astype(str).tolist()

            # Normalize for required check
            from modules.loaders.schema_guard import _normalize
            col_norm = _normalize(col)
            req_norms = {_normalize(r) for r in required}

            is_required = col_norm in req_norms
            is_known = col in known or col_norm in {_normalize(k) for k in known}

            if is_required and fill_pct == 0:
                status = "critical"
            elif is_required and fill_pct < 50:
                status = "warning"
            elif fill_pct == 0:
                status = "empty"
            elif not is_known:
                status = "unknown"
            else:
                status = "ok"

            report.append({
                "column":    col,
                "fill_pct":  fill_pct,
                "filled":    int(filled),
                "total":     total,
                "samples":   samples,
                "status":    status,
                "required":  is_required,
                "known":     is_known,
            })
        except Exception:
            report.append({"column": col, "fill_pct": 0, "filled": 0, "total": 0,
                           "samples": [], "status": "error", "required": False, "known": False})

    return report
