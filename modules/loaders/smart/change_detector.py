"""
modules/loaders/smart/change_detector.py
==========================================
Change Detector — Compares uploaded EDIT file vs current DB values.

For every row in the uploaded file:
  - Fetches current DB value for each editable field
  - Identifies what changed (old → new)
  - Classifies risk level per field
  - Builds a structured ChangeReport

FIX: CLENS/OPHLENS use composite key batch_no|sph for row identity.
     Same batch_no can have many sph values — using batch_no alone caused
     the first sph row to win and all other rows compared against wrong DB values.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import pandas as pd


# ── Risk level constants ──────────────────────────────────────────────────────
RISK_SAFE    = "SAFE"      # 🟢 Price changes, status toggles
RISK_CAUTION = "CAUTION"   # 🟡 Purchase rate, margin-sensitive fields
RISK_WARNING = "WARNING"   # 🔴 Master fields — box size, lens design
RISK_BLOCKED = "BLOCKED"   # ⛔ Identity fields — product name, batch no (never allowed)

# ── Field risk classification ─────────────────────────────────────────────────
FIELD_RISK = {
    # Identity — never changeable
    "product_name":   RISK_BLOCKED,
    "batch_no":       RISK_BLOCKED,
    "sku_code":       RISK_BLOCKED,
    "mobile":         RISK_BLOCKED,
    "record_no":      RISK_BLOCKED,
    "party_name":     RISK_BLOCKED,

    # Master fields — change affects historical records
    "box_size":            RISK_WARNING,
    "lens_design":         RISK_WARNING,
    "material":            RISK_WARNING,
    "index_value":         RISK_WARNING,
    "coating":             RISK_WARNING,
    "coating_type":        RISK_WARNING,
    "wear_schedule":       RISK_WARNING,
    "is_batch_applicable": RISK_WARNING,
    "is_eye_specific":     RISK_WARNING,
    "allow_loose":         RISK_WARNING,
    "base_recommended":    RISK_WARNING,
    "gst_rate":            RISK_WARNING,
    "hsn_code":            RISK_WARNING,

    # Financial — caution
    "purchase_rate":  RISK_CAUTION,
    "cost_price":     RISK_CAUTION,
    "credit_limit":   RISK_CAUTION,
    "credit_days":    RISK_CAUTION,

    # Safe changes
    "selling_price":  RISK_SAFE,
    "mrp":            RISK_SAFE,
    "is_active":      RISK_SAFE,
    "quantity":       RISK_SAFE,
    "qty":            RISK_SAFE,
    "expiry_date":    RISK_SAFE,
    "brand":          RISK_SAFE,
    "brand_group":    RISK_SAFE,
    "colour":         RISK_SAFE,
    "gender":         RISK_SAFE,
    "unit":           RISK_SAFE,
    "address":        RISK_SAFE,
    "city":           RISK_SAFE,
    "area":           RISK_SAFE,
    "pincode":        RISK_SAFE,
    "email":          RISK_SAFE,
    "contact_person": RISK_SAFE,
    "alt_mobile":     RISK_SAFE,
    "gstin":          RISK_SAFE,
    "state_name":     RISK_SAFE,
    "category":       RISK_SAFE,
    "main_group":     RISK_SAFE,
    "lens_category":  RISK_SAFE,
}


@dataclass
class FieldChange:
    row_index:        int
    entity_key:       str           # display key shown in UI
    field_name:       str
    old_value:        Any
    new_value:        Any
    risk_level:       str
    entity_id:        Optional[str] = None    # DB primary key id
    # ── Approval metadata (set by inline edit grid) ────────────────────────
    approved_by:      str  = ""     # who approved this specific change
    manually_edited:  bool = False  # True if user changed the value in the grid


@dataclass
class ChangeReport:
    file_type:       str
    total_rows:      int
    changes:         List[FieldChange] = field(default_factory=list)
    blocked:         List[FieldChange] = field(default_factory=list)
    errors:          List[str]         = field(default_factory=list)
    rows_not_found:  List[str]         = field(default_factory=list)
    backup_required:    bool           = False   # set by ai_change_advisor after risk assessment
    untracked_cols:     List[str]      = field(default_factory=list)  # columns in file but not tracked
    # ── Schema validator results (populated before comparison loop) ────────────
    schema_suggestions: List[str]      = field(default_factory=list)  # human-readable issues
    auto_fixes:         Dict[str, str] = field(default_factory=dict)  # {excel_col → config_col}
    preview_diff:       List[Dict]     = field(default_factory=list)  # rows for UI diff table
    critical_errors:    List[str]      = field(default_factory=list)  # hard-block issues
    # ── Row-level Live vs Uploaded diff (populated in comparison loop) ─────────
    comparison_rows:    List[Dict]     = field(default_factory=list)  # {Row,Record,Field,DB,Upload,Risk}

    @property
    def has_changes(self) -> bool:
        return len(self.changes) > 0

    @property
    def has_blocked(self) -> bool:
        return len(self.blocked) > 0

    @property
    def risk_counts(self) -> Dict[str, int]:
        counts = {RISK_SAFE: 0, RISK_CAUTION: 0, RISK_WARNING: 0}
        for c in self.changes:
            if c.risk_level in counts:
                counts[c.risk_level] += 1
        return counts

    @property
    def highest_risk(self) -> str:
        rc = self.risk_counts
        if rc[RISK_WARNING] > 0: return RISK_WARNING
        if rc[RISK_CAUTION] > 0: return RISK_CAUTION
        return RISK_SAFE

    @property
    def changed_fields_summary(self) -> Dict[str, int]:
        summary = {}
        for c in self.changes:
            summary[c.field_name] = summary.get(c.field_name, 0) + 1
        return summary

    def to_dataframe(self) -> pd.DataFrame:
        if not self.changes:
            return pd.DataFrame()
        return pd.DataFrame([{
            "Row":       c.row_index,
            "Record":    c.entity_key,
            "Field":     c.field_name,
            "Old Value": c.old_value,
            "New Value": c.new_value,
            "Risk":      _risk_emoji(c.risk_level),
        } for c in self.changes])


def _risk_emoji(risk: str) -> str:
    return {
        RISK_SAFE:    "🟢 Safe",
        RISK_CAUTION: "🟡 Caution",
        RISK_WARNING: "🔴 Warning",
        RISK_BLOCKED: "⛔ Blocked",
    }.get(risk, risk)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def detect_changes(df: pd.DataFrame, file_type: str) -> ChangeReport:
    """
    Compare uploaded DataFrame against current DB values.
    Returns ChangeReport with all field-level changes classified by risk.
    """
    from modules.loaders.smart.download_manager import FIELD_CONFIG

    cfg = FIELD_CONFIG.get(file_type)
    if not cfg:
        report = ChangeReport(file_type=file_type, total_rows=len(df))
        report.errors.append(f"Unknown file type: {file_type}")
        return report

    report = ChangeReport(file_type=file_type, total_rows=len(df))

    # Strip 🔒 prefix from column names added by download_manager
    df = df.copy()
    df.columns = [c.replace("🔒 ", "").strip() for c in df.columns]

    # ── Schema validation — runs BEFORE comparison, attaches suggestions ──────
    try:
        from modules.loaders.smart.schema_validator import validate_schema, get_db_columns
        _TABLE_MAP = {
            "PRODUCT": "products",
            "FRAME":   "inventory_stock",
            "CLENS":   "inventory_stock",
            "OPHLENS": "inventory_stock",
            "PRICE":   "inventory_stock",
            "SOL":     "batches",
            "BLANK":   "blank_inventory",
            "PARTY":   "parties",
            "PATIENT": "patients",
        }
        _db_live_cols  = get_db_columns(_TABLE_MAP.get(file_type, ""))
        _schema_report = validate_schema(df, file_type, cfg, _db_live_cols)
        report.schema_suggestions = _schema_report["suggestions"]
        report.auto_fixes         = _schema_report["auto_fixes"]
        report.preview_diff       = _schema_report["preview_diff"]
        report.critical_errors    = _schema_report.get("critical_errors", [])
    except Exception as _sv_ex:
        import logging
        logging.getLogger(__name__).warning(
            f"[change_detector] schema_validator skipped: {_sv_ex}"
        )

    # ── Normalize excel_header → db_column names ──────────────────────────────
    # Handles files where headers are excel_header style (Product, BatchNo)
    # instead of db_column style (product_name, batch_no)
    try:
        from modules.loaders.universal_loader_core import apply_column_map, _get_registry_col_map
        col_map = _get_registry_col_map(file_type)
        if col_map:
            df = apply_column_map(df, col_map, file_type)
    except Exception as _cm_ex:
        import logging
        logging.getLogger(__name__).warning(f"[change_detector] column map apply failed: {_cm_ex}")

    locked       = set(cfg["locked_cols"])
    all_cols     = cfg["columns"]
    key_col      = cfg["key_col"]

    # ── Build reverse map: db_column → actual df column name ─────────────────
    # After column map is applied, df may still use excel_header names if
    # apply_column_map could not resolve some columns.
    # Build a translation so all_cols (db names) can find their df equivalent.
    try:
        from modules.loaders.db_schema_registry import DB_SCHEMA
        _db_to_excel = {}
        for col in DB_SCHEMA.get(file_type, []):
            if col.db_column and col.excel_header:
                _db_to_excel[col.db_column] = col.excel_header
    except Exception:
        _db_to_excel = {}

    def _resolve_col(db_col):
        """Return the actual column name in df for a given db_column."""
        if db_col in df.columns:
            return db_col
        excel_hdr = _db_to_excel.get(db_col)
        if excel_hdr and excel_hdr in df.columns:
            return excel_hdr
        return db_col  # fallback — may not exist in df

    # Remap locked and key_col to actual df column names
    locked  = {_resolve_col(c) for c in locked}
    key_col = _resolve_col(key_col)
    is_ophlens   = file_type == "OPHLENS"
    is_clens     = file_type == "CLENS"
    is_lens_type = is_ophlens or is_clens

    # Build DB lookup — composite key for lens types, natural key for others
    db_lookup = _build_db_lookup(file_type, cfg, df, key_col)

    # For OPHLENS: batch_no was included in old FIELD_CONFIG but is always NULL.
    # Exclude it from editable cols so it doesn't generate spurious 'changes'.
    exclude_from_edit = {"batch_no"} if is_ophlens else set()
    # Resolve all_cols to actual df column names
    # editable_cols = list of (df_col_name, db_col_name) tuples
    editable_cols = []
    tracked_df_cols = set()
    for db_col in all_cols:
        actual = _resolve_col(db_col)
        if actual in df.columns and actual not in locked and db_col not in exclude_from_edit:
            editable_cols.append((actual, db_col))
            tracked_df_cols.add(actual)

    # ── Detect untracked columns (present in file but not in system config) ───
    _known_system_cols = {"_id", key_col}
    _known_system_cols.update(locked)
    untracked = set(df.columns) - tracked_df_cols - _known_system_cols
    # Remove meta/system columns that are expected to be in file but not tracked
    untracked -= {"alcon_item_name", "material_code", "tally_item_name",
                  "supplier_name", "product_barcode", "purchase_price"}
    if untracked:
        report.untracked_cols = sorted(untracked)
        report.errors.append(
            f"⚠️ These columns are in your file but NOT tracked by system "
            f"(they will be ignored): {', '.join(sorted(untracked))}"
        )

    for row_idx, row in df.iterrows():
        # ── Build lookup key ──────────────────────────────────────────────────
        if is_ophlens:
            # OPHLENS: power-tracked, no batch_no. Identity = product + full power combo.
            # batch_no is NULL for all OPHLENS rows — cannot use it as key.
            pname    = str(row.get("product_name", "")).strip()
            sph_n    = _normalise(row.get("sph",       "")) or "NULL"
            cyl_n    = _normalise(row.get("cyl",       "")) or "NULL"
            axis_n   = _normalise(row.get("axis",      "")) or "NULL"
            add_n    = _normalise(row.get("add_power", "")) or "NULL"
            eye_n    = str(row.get("eye_side", "")).strip() or "NULL"
            lookup_key  = f"{pname}|{sph_n}|{cyl_n}|{axis_n}|{add_n}|{eye_n}"
            display_key = f"{pname} sph={sph_n} cyl={cyl_n}"
        elif is_clens:
            # CLENS: batch_no + sph composite (batch can have many powers)
            batch       = str(row.get("batch_no", "")).strip()
            sph_n       = _normalise(row.get("sph", "")) or "NULL"
            lookup_key  = f"{batch}|{sph_n}"
            display_key = f"{batch} sph={sph_n}"
        else:
            lookup_key  = str(row.get(key_col, "")).strip()
            display_key = lookup_key

        if not lookup_key or lookup_key in ("|", "||||||"):
            continue

        db_row = db_lookup.get(lookup_key)
        if db_row is None:
            report.rows_not_found.append(display_key)
            continue

        entity_id = db_row.get("_id")

        # ── Check locked columns for tampering ───────────────────────────────
        for lc in locked:
            if lc not in df.columns:
                continue
            # lc is already resolved to df column name; get db name for db_row lookup
            db_lc = _db_to_excel and next((k for k, v in _db_to_excel.items() if v == lc), lc)
            uploaded_val = _normalise(row.get(lc))
            db_val       = _normalise(db_row.get(db_lc))
            if uploaded_val and _values_differ(uploaded_val, db_val):
                report.blocked.append(FieldChange(
                    row_index  = row_idx + 2,
                    entity_key = display_key,
                    field_name = lc,
                    old_value  = db_val,
                    new_value  = uploaded_val,
                    risk_level = RISK_BLOCKED,
                    entity_id  = entity_id,
                ))

        # ── Check editable columns for actual changes ─────────────────────────
        for df_col, db_col in editable_cols:
            uploaded_val = _normalise(row.get(df_col))
            db_val       = _normalise(db_row.get(db_col))
            col          = db_col  # use db_col for risk lookup and field_name

            if uploaded_val is None or uploaded_val == "":
                continue    # blank = no change intended

            if _values_differ(uploaded_val, db_val):
                risk = FIELD_RISK.get(col, RISK_SAFE)
                report.changes.append(FieldChange(
                    row_index  = row_idx + 2,
                    entity_key = display_key,
                    field_name = col,
                    old_value  = db_val,
                    new_value  = uploaded_val,
                    risk_level = risk,
                    entity_id  = entity_id,
                ))
                report.comparison_rows.append({
                    "Row":            row_idx + 2,
                    "Record":         display_key,
                    "Field":          col,
                    "DB Value":       db_val,
                    "Uploaded Value": uploaded_val,
                    "Risk":           _risk_emoji(risk),
                    "Approved":       True,   # default approved; user can uncheck in UI
                })

    return report


# ══════════════════════════════════════════════════════════════════════════════
# DB LOOKUP BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_db_lookup(file_type: str, cfg: dict, df: pd.DataFrame, key_col: str) -> Dict[str, dict]:
    """
    Fetch current DB values for all rows in the uploaded file.

    CLENS/OPHLENS: keyed by "batch_no|sph_normalised"
      — one entry per unique power row, never batch_no alone.
      — old code fell back to batch_no which caused the first sph row to be
        used for ALL rows of that batch → wrong old_value comparisons.

    All other types: keyed by natural key (product_name, party_name, etc).
    """
    try:
        from modules.sql_adapter import run_query
    except ImportError:
        return {}

    all_cols = cfg["columns"]
    lookup   = {}

    if file_type in ("CLENS", "OPHLENS"):
        stock_type = "BATCH" if file_type == "CLENS" else "POWER"

        if file_type == "CLENS":
            # CLENS: batch-tracked — query by batch_no
            keys = df["batch_no"].dropna().astype(str).str.strip()
            keys = [k for k in keys.unique().tolist() if k.lower() not in ("nan", "none", "")]
            if not keys:
                return {}

            placeholders = ", ".join(["%s"] * len(keys))
            rows = run_query(f"""
                SELECT s.id AS _id, p.product_name,
                       s.batch_no, s.sph, s.cyl, s.axis, s.add_power, s.eye_side,
                       s.quantity, s.purchase_rate, s.selling_price, s.mrp,
                       s.lens_design, s.item_type, s.expiry_date, s.is_active
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE s.batch_no IN ({placeholders})
                AND s.stock_type = '{stock_type}'
                AND s.is_active = true
            """, tuple(keys)) or []

            for r in rows:
                sph_norm  = _normalise(r.get("sph")) or "NULL"
                composite = f"{r.get('batch_no')}|{sph_norm}"
                lookup[composite] = r

        else:
            # OPHLENS: power-tracked — batch_no is always NULL.
            # Key = product_name|sph|cyl|axis|add_power|eye_side
            # Query by product_name to fetch all matching rows.
            keys = df["product_name"].dropna().astype(str).str.strip()
            keys = [k for k in keys.unique().tolist() if k.lower() not in ("nan", "none", "")]
            if not keys:
                return {}

            placeholders = ", ".join(["%s"] * len(keys))
            rows = run_query(f"""
                SELECT s.id AS _id, p.product_name,
                       s.sph, s.cyl, s.axis, s.add_power, s.eye_side,
                       s.quantity, s.purchase_rate, s.selling_price, s.mrp,
                       s.lens_design, s.item_type, s.is_active
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE p.product_name IN ({placeholders})
                AND s.stock_type = 'POWER'
                AND s.is_active = true
            """, tuple(keys)) or []

            for r in rows:
                pname  = str(r.get("product_name", "")).strip()
                sph_n  = _normalise(r.get("sph"))       or "NULL"
                cyl_n  = _normalise(r.get("cyl"))       or "NULL"
                axis_n = _normalise(r.get("axis"))      or "NULL"
                add_n  = _normalise(r.get("add_power")) or "NULL"
                eye_n  = str(r.get("eye_side", "")).strip() or "NULL"
                composite = f"{pname}|{sph_n}|{cyl_n}|{axis_n}|{add_n}|{eye_n}"
                lookup[composite] = r

    elif file_type == "PRODUCT":
        # Resolve key_col to actual column name in df (handles excel_header vs db_column mismatch)
        actual_key_col = key_col
        if key_col not in df.columns:
            # Try common aliases
            _aliases = {
                "product_name": ["Product", "product", "ProductName", "productname"],
                "party_name":   ["Party", "PartyName", "party"],
                "batch_no":     ["BatchNo", "batchno", "batch_no"],
                "sku_code":     ["SKUCode", "skucode", "sku_code"],
            }
            for candidate in _aliases.get(key_col, []):
                if candidate in df.columns:
                    actual_key_col = candidate
                    break
        keys = df[actual_key_col].dropna().astype(str).str.strip().unique().tolist()
        if not keys: return {}
        placeholders = ", ".join(["%s"] * len(keys))
        rows = run_query(f"""
            SELECT id AS _id, {', '.join(f'"{c}"' for c in all_cols)}
            FROM products WHERE product_name IN ({placeholders})
        """, tuple(keys)) or []
        for r in rows:
            lookup[str(r.get("product_name", ""))] = r

    elif file_type == "PARTY":
        actual_key_col = key_col if key_col in df.columns else next(
            (c for c in ["Party", "PartyName", "party_name", "party"] if c in df.columns), key_col)
        keys = df[actual_key_col].dropna().astype(str).str.strip().unique().tolist()
        if not keys: return {}
        placeholders = ", ".join(["%s"] * len(keys))
        rows = run_query(f"""
            SELECT id AS _id, {', '.join(f'"{c}"' for c in all_cols)}
            FROM parties WHERE party_name IN ({placeholders})
        """, tuple(keys)) or []
        for r in rows:
            lookup[str(r.get("party_name", ""))] = r

    elif file_type == "FRAME":
        actual_key_col = key_col if key_col in df.columns else next(
            (c for c in ["SKUCode", "skucode", "sku_code", "Sku Code"] if c in df.columns), key_col)
        keys = df[actual_key_col].dropna().astype(str).str.strip().unique().tolist()
        if not keys: return {}
        placeholders = ", ".join(["%s"] * len(keys))
        rows = run_query(f"""
            SELECT id AS _id, {', '.join(f'"{c}"' for c in all_cols)}
            FROM frames WHERE sku_code IN ({placeholders})
        """, tuple(keys)) or []
        for r in rows:
            lookup[str(r.get("sku_code", ""))] = r

    elif file_type in ("SOL", "BLANK"):
        keys = df[key_col].dropna().astype(str).str.strip().unique().tolist()
        if not keys: return {}
        placeholders = ", ".join(["%s"] * len(keys))
        rows = run_query(f"""
            SELECT b.id AS _id, p.product_name, b.batch_no,
                   b.expiry_date, b.qty_available, b.cost_price,
                   b.selling_price, b.mrp, b.is_active
            FROM batches b
            JOIN products p ON p.id = b.product_id
            WHERE b.batch_no IN ({placeholders})
        """, tuple(keys)) or []
        for r in rows:
            lookup[str(r.get("batch_no", ""))] = r

    return lookup


# ══════════════════════════════════════════════════════════════════════════════
# VALUE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _normalise(v) -> Optional[str]:
    """
    Normalise a value for comparison.
    Handles floats, Python booleans, boolean strings, None.

    CRITICAL: Python True/False from DB must map to YES/NO
    so they compare correctly against Excel YES/NO values.
    """
    if v is None:
        return None

    # ── Python bool FIRST — before str(v) which gives "True"/"False" ─────────
    if isinstance(v, bool):
        return "YES" if v else "NO"

    s = str(v).strip()
    if s.lower() in ("nan", "none", "nat", ""):
        return None

    # ── Unify all boolean-like representations → YES / NO ────────────────────
    su = s.upper()
    if su in ("YES", "TRUE", "1", "T", "Y"):
        return "YES"
    if su in ("NO", "FALSE", "0", "F", "N"):
        return "NO"

    # ── Normalise numeric: -2.50 == -2.5 == -2.500 ───────────────────────────
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
        return f"{f:.4f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        pass

    return s


def _values_differ(a: Optional[str], b: Optional[str]) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return a.lower() != b.lower()
