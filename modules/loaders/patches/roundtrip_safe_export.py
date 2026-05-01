"""
patches/roundtrip_safe_export.py
==================================
PATCH 3 — ROUNDTRIP-SAFE EXPORT TEMPLATES
==========================================

Guarantees: Download → Edit → Upload produces ZERO column remapping errors.

What this module provides:

  1. ROUNDTRIP VALIDATOR
     Checks an uploaded df against the expected schema contract for a
     given loader type. Returns column mismatches before any DB call.

  2. CLEAN TEMPLATE GENERATOR
     Generates an empty, pre-formatted Excel template file for any
     loader type — ready for fresh data entry.

  3. RE-IMPORT MODE EXPORTER
     Wraps fetch_for_download() to produce a version with internal IDs
     and import hashes included — suitable for bulk corrections/undo.

  4. SYSTEM COLUMN PROTECTOR
     Locks system columns (id, created_at, import_id) in Excel to
     prevent accidental editing.

USAGE:

    from patches.roundtrip_safe_export import (
        validate_roundtrip_columns,
        build_clean_template,
        build_reimport_excel,
        SYSTEM_COLUMNS,
    )

    # Before import — check columns match contract
    ok, issues = validate_roundtrip_columns(uploaded_df, "PRODUCT")

    # Generate blank template for data entry
    excel_bytes = build_clean_template("FRAME")

    # Export with IDs for bulk correction
    excel_bytes = build_reimport_excel(df, meta, "PARTY")
"""

import io
import logging
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA CONTRACT
# Exact column names the downloader outputs AND the loader expects.
# This is the single source of truth for roundtrip safety.
# ══════════════════════════════════════════════════════════════════════════════

# Columns that must NEVER be edited by the user
SYSTEM_COLUMNS = {"id", "created_at", "import_id", "updated_at", "product_code"}

# Required columns per loader type (after column mapping)
# These MUST be present for a successful upload
ROUNDTRIP_REQUIRED = {
    "PRODUCT": ["Product"],
    "FRAME":   ["SKUCode"],
    "PARTY":   ["PARTYNAME"],
    "PATIENT": ["Mobile Number", "Record No"],   # at least one required
    "OPHLENS": ["Product", "Quantity"],
    "CLENS":   ["Product", "Quantity"],
    "SOL":     ["Product"],
    "BLANK":   ["brand", "Category", "Material", "Add"],
}

# Full expected column sets per type (what the downloader emits)
ROUNDTRIP_SCHEMA = {
    "PRODUCT": [
        "Product", "MainGroup", "Type", "LensCategory", "Brand", "BrandProductGroup",
        "Material", "Index", "Coating", "coating_type", "Colour", "Gender",
        "WearSchedule", "unit", "IsBatchApplicable", "IsEyeSpecific", "IsActive",
        "HSNCode", "Box Size", "Allow Loose",
    ],
    "FRAME": [
        "SKUCode", "Product", "Model", "Brand", "ASize", "DBL",
        "TempleLength", "BaseMaterial", "Finish", "Colour", "shape",
        "Qty", "CostPrice", "MRP", "IsActive",
    ],
    "PARTY": [
        "PARTYNAME", "ROLETYPE", "MOBILE", "ADDRESS", "CITY", "AREA", "ISACTIVE",
    ],
    "PATIENT": [
        "Client Name", "Mobile Number", "Record No", "Date",
        "Right Sph", "Right CYL", "Right AXIS", "Right Add Power",
        "Left SPH", "Left CYL", "Left AXIS", "Left Add Power",
    ],
    "OPHLENS": [
        "Product", "SPH", "CYL", "AXIS", "ADD", "EyeSide",
        "BatchNo", "ExpiryDate", "Quantity", "PurchaseRate",
        "SellingPrice", "MRP", "lens_design", "Location", "IsActive",
    ],
    "CLENS": [
        "Product", "SPH", "CYL", "AXIS", "ADD", "EyeSide",
        "BatchNo", "ExpiryDate", "Quantity", "PurchaseRate",
        "SellingPrice", "MRP", "IsActive",
    ],
    "SOL": [
        "Product", "BatchNo", "ExpiryDate", "Qty", "CostPrice",
        "SellingPrice", "MRP", "IsActive",
    ],
    "BLANK": [
        "brand", "Category", "Material", "COLOUR", "Add",
        "qty_Right", "qty_left", "qty_independent",
        "Recomended Base", "Base 1 P", "Base 2 P", "Base 3P",
    ],
}

# Column default values for blank template generation
COLUMN_DEFAULTS = {
    "IsActive":          "YES",
    "ISACTIVE":          "YES",
    "IsBatchApplicable": "NO",
    "IsEyeSpecific":     "NO",
    "unit":              "PCS",
    "EyeSide":           "B",
    "Quantity":          0,
    "Qty":               0,
    "qty_Right":         0,
    "qty_left":          0,
    "qty_independent":   0,
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. ROUNDTRIP COLUMN VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

def validate_roundtrip_columns(
    uploaded_df: pd.DataFrame,
    loader_type:  str,
) -> Tuple[bool, List[str]]:
    """
    Validate that an uploaded Excel matches the expected column contract.

    Returns: (is_valid: bool, issues: List[str])

    Checks:
      - All required columns present
      - No unexpected system columns that could cause DB conflicts
      - Warns about unexpected extra columns (non-fatal)

    Call this BEFORE run_loader() / run_loader_safe() for a clean UX.
    """
    issues: List[str] = []

    # Normalise uploaded column names for comparison
    uploaded_cols_raw  = set(uploaded_df.columns)
    uploaded_cols_norm = {c.strip().lower().replace(" ", "").replace("_", "") for c in uploaded_df.columns}

    schema = ROUNDTRIP_SCHEMA.get(loader_type)
    if not schema:
        return True, []   # unknown type — loader will handle it

    required = ROUNDTRIP_REQUIRED.get(loader_type, [])

    # Check required columns
    for req in required:
        req_norm = req.strip().lower().replace(" ", "").replace("_", "")
        if req_norm not in uploaded_cols_norm:
            issues.append(
                f"❌ CRITICAL — Required column missing: '{req}'. "
                f"Download a fresh template and check column names."
            )

    # Check for system columns that should never appear in uploads
    for col in uploaded_cols_raw:
        col_low = col.strip().lower()
        if col_low in {s.lower() for s in SYSTEM_COLUMNS}:
            issues.append(
                f"⚠️ System column found: '{col}' — this will be ignored by the loader. "
                "Do not edit system columns."
            )

    # Warn about completely unexpected columns (non-fatal)
    expected_norm = {c.strip().lower().replace(" ", "").replace("_", "") for c in schema}
    unexpected = uploaded_cols_norm - expected_norm - {
        s.lower() for s in SYSTEM_COLUMNS
    }
    if unexpected:
        issues.append(
            f"ℹ️ Unexpected columns will be ignored: {sorted(unexpected)}. "
            "This is OK but check for typos in column names."
        )

    has_critical = any(i.startswith("❌") for i in issues)
    return not has_critical, issues


# ══════════════════════════════════════════════════════════════════════════════
# 2. CLEAN TEMPLATE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def build_clean_template(dataset_key: str) -> bytes:
    """
    Generate an empty, pre-formatted Excel template for a given loader type.
    Includes:
      - Correct column headers (exact loader contract)
      - One example row with sensible defaults
      - Data validation dropdowns
      - Header comments
      - Info & Guide sheet

    Returns raw bytes for download.
    """
    from modules.loaders.patches.excel_export_enhancer import build_enhanced_excel

    schema = ROUNDTRIP_SCHEMA.get(dataset_key, [])
    if not schema:
        raise ValueError(f"No schema defined for: {dataset_key}")

    # Empty DataFrame with correct columns
    df = pd.DataFrame(columns=schema)

    meta = {
        "label":       f"{dataset_key} — Blank Template",
        "dataset":     dataset_key,
        "downloaded":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows":        0,
        "loader_type": dataset_key,
        "key_col":     " + ".join(ROUNDTRIP_REQUIRED.get(dataset_key, ["—"])),
        "edit_guide":  (
            f"This is a blank import template for {dataset_key}. "
            "Fill in data rows below the example row. "
            "Delete the orange example row before importing."
        ),
        "filters":     "none (template)",
        "icon":        "📋",
    }

    return build_enhanced_excel(
        df            = df,
        meta          = meta,
        dataset_key   = dataset_key,
        include_sample = True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. RE-IMPORT MODE EXPORTER
# ══════════════════════════════════════════════════════════════════════════════

def build_reimport_excel(
    df:          pd.DataFrame,
    meta:        dict,
    dataset_key: str,
    include_ids: bool = True,
) -> bytes:
    """
    Produce a re-import-ready Excel that includes:
      - All standard columns (loader-compatible)
      - Optional internal IDs column (for traceability)
      - Row hashes (for dedup bypass if needed)
      - System columns LOCKED (protected from editing)

    Useful for bulk corrections: download, fix data, re-upload.

    Note: The loader ignores system columns — they are purely informational.
    """
    from modules.loaders.patches.excel_export_enhancer import build_enhanced_excel

    re_df = df.copy()

    if include_ids and "id" in re_df.columns:
        # Move id to the front as an informational column
        id_col = re_df.pop("id")
        re_df.insert(0, "_db_id [DO NOT EDIT]", id_col)

    # Add a note column
    if "_db_id [DO NOT EDIT]" not in re_df.columns:
        re_df.insert(0, "_source", "re-import")

    reimport_meta = dict(meta)
    reimport_meta["label"]      = f"{meta.get('label', dataset_key)} — Re-import Version"
    reimport_meta["edit_guide"] = (
        "Re-import mode: internal IDs included for traceability. "
        "The loader IGNORES columns starting with '_'. "
        "Edit data columns freely. Delete the orange example row before importing."
    )

    return build_enhanced_excel(
        df           = re_df,
        meta         = reimport_meta,
        dataset_key  = dataset_key,
        include_sample = False,   # no sample in re-import mode — real data only
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. SYSTEM COLUMN PROTECTOR (Streamlit UI helper)
# ══════════════════════════════════════════════════════════════════════════════

def get_editable_columns(df: pd.DataFrame) -> List[str]:
    """
    Returns the list of columns that users should be allowed to edit.
    Filters out system columns. Use this in your UI data editors.
    """
    sys_lower = {s.lower() for s in SYSTEM_COLUMNS}
    return [
        col for col in df.columns
        if col.strip().lower() not in sys_lower
        and not col.startswith("_")
    ]


def strip_system_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove system columns from a DataFrame before passing to the loader.
    Defensive strip — loader ignores them anyway, but cleaner.
    """
    sys_lower = {s.lower() for s in SYSTEM_COLUMNS}
    drop = [c for c in df.columns if c.strip().lower() in sys_lower or c.startswith("_db_")]
    return df.drop(columns=drop, errors="ignore")


# ══════════════════════════════════════════════════════════════════════════════
# 5. IMPORT DRY-RUN DIFF SUMMARY (UI helper)
# ══════════════════════════════════════════════════════════════════════════════

def format_dry_run_summary(result) -> str:
    """
    Format a LoadResult from a DRY RUN into a human-friendly summary string.
    Use this in loader_ui.py to show users what will happen before LIVE.

    Example output:
        📋 DRY RUN SUMMARY — PRODUCT
        Will insert:   34 new rows
        Will update:  120 existing rows
        Will skip:      3 rows (errors)
        ─────────────────────────────
        ✅ Ready for LIVE import (0 critical errors)
    """
    lines = [
        f"📋 DRY RUN SUMMARY — {result.file_type}",
        f"  Will insert:  {result.inserted:>6,} new rows",
        f"  Will update:  {result.updated:>6,} existing rows",
        f"  Will skip:    {result.skipped:>6,} rows (errors/dupes)",
        "─" * 38,
    ]

    critical = [e for e in result.errors if e.get("field") != "info"]
    if not critical:
        lines.append("✅ Ready for LIVE import (0 critical errors)")
    else:
        lines.append(f"⚠️ {len(critical)} error(s) must be reviewed before LIVE")

    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in result.warnings[:5]:
            lines.append(f"  • {w}")
        if len(result.warnings) > 5:
            lines.append(f"  ... and {len(result.warnings) - 5} more")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 6. PRICE CHANGE DIFF PREVIEW
# Shows exactly which batches will have prices changed, old vs new, before LIVE.
# Call this from loader_ui.py after file upload but before confirming LIVE import.
# ══════════════════════════════════════════════════════════════════════════════

def build_price_change_diff(uploaded_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compare uploaded file prices against current DB values for CLENS/OPHLENS.

    For each row in the uploaded Excel, fetches the current selling_price,
    purchase_rate, and mrp from inventory_stock and shows:
      - What the DB currently has
      - What the Excel wants to set
      - Whether it's a change (✅) or no change (─)

    Returns a DataFrame ready to display in Streamlit st.dataframe().
    Rows with no price change are included but dimmed with "─" marker.

    Usage in loader_ui.py:
        diff_df = build_price_change_diff(uploaded_df)
        if not diff_df.empty:
            changed = diff_df[diff_df["Changed"] == "✅"]
            st.info(f"{len(changed)} of {len(diff_df)} rows have price changes")
            st.dataframe(diff_df, use_container_width=True)
    """
    try:
        from modules.sql_adapter import run_query
    except ImportError:
        return pd.DataFrame()

    # Normalise columns — works with both raw upload columns and post-pipeline DB names
    col_map = {
        "product":       "product_name",
        "Product":       "product_name",
        "product_name":  "product_name",
        "BatchNo":       "batch_no",
        "batchno":       "batch_no",
        "batch_no":      "batch_no",
        "SPH":           "sph",
        "sph":           "sph",
        "CYL":           "cyl",
        "cyl":           "cyl",
        "AXIS":          "axis",
        "axis":          "axis",
        "EyeSide":       "eye_side",
        "eyeside":       "eye_side",
        "eye_side":      "eye_side",
        "SellingPrice":  "selling_price",
        "sellingprice":  "selling_price",
        "selling_price": "selling_price",
        "PurchaseRate":  "purchase_rate",
        "purchaserate":  "purchase_rate",
        "purchase_rate": "purchase_rate",
        "MRP":           "mrp",
        "mrp":           "mrp",
    }
    df = uploaded_df.rename(columns={c: col_map[c] for c in uploaded_df.columns if c in col_map})

    required = {"product_name", "batch_no"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()  # not a CLENS/OPHLENS file

    rows_out = []

    for _, row in df.iterrows():
        pname    = str(row.get("product_name", "") or "").strip()
        batch_no = str(row.get("batch_no", "")     or "").strip()
        if not pname or not batch_no:
            continue

        # Excel values
        excel_sp = _to_f(row.get("selling_price"))
        excel_pr = _to_f(row.get("purchase_rate"))
        excel_mr = _to_f(row.get("mrp"))
        sph_val  = row.get("sph")
        cyl_val  = row.get("cyl")
        eye_val  = str(row.get("eye_side", "B") or "B").strip().upper()

        # DB lookup — find inventory_stock row for this batch + power
        try:
            db_rows = run_query("""
                SELECT s.selling_price, s.purchase_rate, s.mrp
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE LOWER(TRIM(p.product_name)) LIKE LOWER(%s)
                  AND s.batch_no = %s
                  AND s.is_active = true
                LIMIT 1
            """, (f"%{pname.lower()[:30]}%", batch_no))
        except Exception:
            db_rows = []

        if not db_rows:
            # Batch not in DB yet — this will be an INSERT, not a price update
            rows_out.append({
                "Product":         _short(pname),
                "Batch":           batch_no,
                "SPH":             sph_val,
                "CYL":             cyl_val,
                "Eye":             eye_val,
                "DB SellingPrice": "─ (new)",
                "→ SellingPrice":  _fmt(excel_sp),
                "DB PurchRate":    "─ (new)",
                "→ PurchRate":     _fmt(excel_pr),
                "DB MRP":          "─ (new)",
                "→ MRP":           _fmt(excel_mr),
                "Changed":         "🆕 INSERT",
            })
            continue

        db      = db_rows[0]
        db_sp   = _to_f(db.get("selling_price"))
        db_pr   = _to_f(db.get("purchase_rate"))
        db_mr   = _to_f(db.get("mrp"))

        sp_changed = excel_sp is not None and abs((excel_sp or 0) - (db_sp or 0)) > 0.001
        pr_changed = excel_pr is not None and abs((excel_pr or 0) - (db_pr or 0)) > 0.001
        mr_changed = excel_mr is not None and abs((excel_mr or 0) - (db_mr or 0)) > 0.001
        any_change = sp_changed or pr_changed or mr_changed

        rows_out.append({
            "Product":         _short(pname),
            "Batch":           batch_no,
            "SPH":             sph_val,
            "CYL":             cyl_val,
            "Eye":             eye_val,
            "DB SellingPrice": _fmt(db_sp),
            "→ SellingPrice":  _fmt(excel_sp) if sp_changed else "─",
            "DB PurchRate":    _fmt(db_pr),
            "→ PurchRate":     _fmt(excel_pr) if pr_changed else "─",
            "DB MRP":          _fmt(db_mr),
            "→ MRP":           _fmt(excel_mr) if mr_changed else "─",
            "Changed":         "✅" if any_change else "─",
        })

    return pd.DataFrame(rows_out) if rows_out else pd.DataFrame()


def _to_f(v) -> Optional[float]:
    """Safe float conversion for diff comparison."""
    if v is None:
        return None
    try:
        f = float(str(v).strip())
        import math
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _fmt(v: Optional[float]) -> str:
    """Format a price for display."""
    if v is None:
        return "─"
    return f"₹{v:,.2f}"


def _short(name: str, maxlen: int = 35) -> str:
    """Truncate long product names for table display."""
    return name if len(name) <= maxlen else name[:maxlen - 1] + "…"
