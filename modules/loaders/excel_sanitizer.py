"""
modules/loaders/excel_sanitizer.py
====================================
Universal Ingestion Shield — DV ERP

Three-stage pipeline applied inside load_excel():
  1. normalize_headers      → alias resolution + warning tracking
  2. ai_auto_map_headers    → schema-aware mapping via ai_mapping_engine (NOT global fuzzy)
  3. clean_excel_dataframe  → strip cells, drop empty columns + warnings
  4. auto_fix_schema        → inject missing optional columns + warnings

Also provides:
  - IngestionReport         → quality scoring + warning collector
  - apply_ingestion_shield  → convenience wrapper (all 4 stages)

NOTE on AI mapping:
  Global fuzzy matching (difflib against a mixed cross-domain CANONICAL_COLUMNS list)
  has been REMOVED — it caused fields like "gstpercent" → "costprice" corruption.
  ai_auto_map_headers() now delegates to ai_mapping_engine.intelligent_ai_mapping()
  which is table-context-aware and protects all critical financial/medical fields.
"""

import re

# ── Canonical column aliases ──────────────────────────────────────────────────
HEADER_ALIASES = {
    # Product name variants
    "product":              "productname",
    "producttitle":         "productname",
    "productnames":         "productname",
    "itemname":             "productname",

    # SKU / frame
    "itemcode":             "skucode",

    # Party
    "roletype":             "partytype",

    # Colour
    "color":                "colour",

    # Quantity
    "quantity":             "qty",
}

# CANONICAL_COLUMNS — intentionally scoped to product domain only.
# Previously this was a cross-domain mix of all file types — which caused
# the AI mapper to make dangerous cross-table matches (e.g. "gstpercent" → "costprice").
# The full AI mapping now lives in ai_mapping_engine.py with per-table schemas.
# This list is kept ONLY for any legacy callers of ai_auto_map_headers().
CANONICAL_COLUMNS = [
    "productname", "brand", "brandproductgroup", "maingroup",
    "type", "lenscategory", "index", "material", "coating",
    "coatingtype", "colour", "unit", "wearschedule", "gender",
    "boxsize", "allowloose", "isbatchapplicable", "iseyespecific",
    "isactive", "hsncode",
]

# Fields that must NEVER be remapped by fuzzy guessing.
# Any column matching these names is passed through exactly as-is.
CRITICAL_FIELDS = {
    "mrp", "costprice", "sellingprice", "purchaserate",
    "gstpercent", "gst", "tax", "discount", "discountpercent",
    "hsncode", "hsn",
    "sph", "cyl", "axis", "add", "addpower",
    "rightsph", "rightcyl", "rightaxis", "rightaddpower",
    "leftsph", "leftcyl", "leftaxis", "leftaddpower",
    "productname", "partyname", "skucode", "mobilenumber", "recordno",
}

# Optional columns injected when absent — keeps loaders from KeyError-ing
DEFAULT_COLUMNS = {
    "isactive":           True,
    "isbatchapplicable":  False,
    "iseyespecific":      False,
    "allowloose":         True,
}


# ═══════════════════════════════════════════════════════
# INGESTION REPORT — quality scoring + warning collector
# ═══════════════════════════════════════════════════════

class IngestionReport:
    """
    Tracks all auto-fixes, warnings, and produces a quality score.

    Usage:
        report = IngestionReport()
        df = normalize_headers(df, report)
        df = ai_auto_map_headers(df, report)
        df = clean_excel_dataframe(df, report)
        df = auto_fix_schema(df, report)
        report.finalize()
        print(report.score)   # e.g. 84
        print(report.warnings)
    """

    def __init__(self):
        self.warnings: list[str] = []
        self.auto_fixes: int = 0
        self.missing_required: int = 0
        self.empty_rows_dropped: int = 0
        self.empty_cols_dropped: int = 0
        self.score: int = 100

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def penalize(self, points: int):
        self.score = max(0, self.score - points)

    def finalize(self):
        """
        Compute final score based on accumulated penalties.
        Call this after all pipeline stages are complete.
        """
        self.score -= self.auto_fixes * 2
        self.score -= self.missing_required * 10
        self.score -= self.empty_rows_dropped * 1
        self.score -= self.empty_cols_dropped * 3
        self.score = max(0, min(100, self.score))

    def summary(self) -> dict:
        return {
            "score":               self.score,
            "auto_fixes":          self.auto_fixes,
            "missing_required":    self.missing_required,
            "empty_cols_dropped":  self.empty_cols_dropped,
            "empty_rows_dropped":  self.empty_rows_dropped,
            "warning_count":       len(self.warnings),
            "warnings":            self.warnings,
        }


# ═══════════════════════════════════════════════════════
# STAGE 1 — Header alias resolution
# ═══════════════════════════════════════════════════════

def normalize_headers(df, report: IngestionReport = None):
    """
    Resolve column aliases to canonical names.

    Runs AFTER universal_loader_core._normalize_header() has already:
      - stripped whitespace
      - lowercased
      - removed spaces and underscores

    This layer handles semantic aliases (e.g. "product" → "productname").
    Unknown columns pass through unchanged.
    """
    cleaned = []

    for col in df.columns:
        canonical = HEADER_ALIASES.get(col, col)

        if report and canonical != col:
            report.auto_fixes += 1
            report.add_warning(f"Header normalized: '{col}' → '{canonical}'")

        cleaned.append(canonical)

    df.columns = cleaned
    return df


# ═══════════════════════════════════════════════════════
# STAGE 2 — Schema-aware AI header mapping
# ═══════════════════════════════════════════════════════

def ai_auto_map_headers(df, report: IngestionReport = None):
    """
    Schema-aware AI header mapping.

    REPLACED: old global fuzzy matching (cutoff=0.75, cross-domain CANONICAL_COLUMNS)
    which caused dangerous cross-table corruption like "gstpercent" → "costprice".

    NOW delegates to ai_mapping_engine.intelligent_ai_mapping() which:
      - Detects table context (PRODUCT/FRAME/PARTY/etc.) before mapping
      - Only fuzzy-matches within that table's valid column list
      - Never maps critical financial/medical fields (mrp, sph, cyl, gst, etc.)
      - Uses 0.85 confidence threshold
      - Silent no-op if table context is unknown — never corrupts

    Falls back to a no-op if ai_mapping_engine is unavailable.
    """
    try:
        from modules.loaders.ai_mapping_engine import intelligent_ai_mapping
        return intelligent_ai_mapping(df, report)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"[AI-MAP] ai_mapping_engine unavailable — skipping AI mapping: {e}"
        )
        return df


# ═══════════════════════════════════════════════════════
# STAGE 3 — Cell cleaning
# ═══════════════════════════════════════════════════════

def clean_excel_dataframe(df, report: IngestionReport = None):
    """
    Silent fix layer:
      - Strip and collapse whitespace in all string cell values
      - Remove non-breaking spaces (common Excel copy-paste artefact)
      - Convert stringified 'nan' / 'None' back to actual None
      - Drop fully-empty columns
      - Drop columns with null/empty headers (unnamed Excel artefacts)
    """
    import pandas as pd

    # Clean string columns
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(r"\s+", " ", regex=True)    # collapse internal whitespace
                .str.replace("\u00a0", " ", regex=False)  # non-breaking space → space
                .str.strip()
                .replace({"nan": None, "None": None, "NaN": None})
            )

    # Drop columns that are entirely empty
    empty_cols = [c for c in df.columns if df[c].isna().all()]
    if empty_cols:
        if report:
            report.empty_cols_dropped += len(empty_cols)
            report.add_warning(f"Dropped {len(empty_cols)} empty column(s): {empty_cols}")
        df = df.drop(columns=empty_cols)

    # Drop columns with null or blank headers
    df = df.loc[:, df.columns.notna()]
    df = df.loc[:, df.columns.astype(str).str.strip() != ""]

    # Track empty rows (don't drop them — just warn)
    empty_rows = int(df.isnull().all(axis=1).sum())
    if empty_rows and report:
        report.empty_rows_dropped += empty_rows
        report.add_warning(f"Detected {empty_rows} fully empty row(s) in file")

    return df


# ═══════════════════════════════════════════════════════
# STAGE 4 — Schema auto-fix
# ═══════════════════════════════════════════════════════

def auto_fix_schema(df, report: IngestionReport = None):
    """
    Smart fill layer:
    Inject missing optional columns with safe defaults so loaders never
    KeyError on commonly absent-but-expected fields.
    Only adds columns genuinely absent — never overwrites existing data.
    """
    for col, default in DEFAULT_COLUMNS.items():
        if col not in df.columns:
            df[col] = default
            if report:
                report.auto_fixes += 1
                report.add_warning(
                    f"Auto-added missing optional column '{col}' "
                    f"(default: {default})"
                )

    return df


# ═══════════════════════════════════════════════════════
# CONVENIENCE WRAPPER
# ═══════════════════════════════════════════════════════

def apply_ingestion_shield(df, report: IngestionReport = None):
    """
    Full four-stage pipeline in one call.

    Used inside load_excel() in universal_loader_core.py:

        report = IngestionReport()
        df = apply_ingestion_shield(df, report)
        report.finalize()

    The report object is then available for UI display.
    """
    df = normalize_headers(df, report)
    df = ai_auto_map_headers(df, report)
    df = clean_excel_dataframe(df, report)
    df = auto_fix_schema(df, report)
    return df
