import re
"""
modules/loaders/universal_loader_core.py
=========================================
MASTER LOADER ENGINE — DV ERP
Owns: Excel → Validate → Clean → DB → Report pipeline

Supports:
  - Dry Run mode (no DB writes)
  - Shadow mode  (writes with environment_tag = 'SHADOW')
  - Live mode    (writes with environment_tag = 'LIVE')

STOCK MODE (new in WIN16 r2):
  - stock_mode="ADD"     → existing_qty + excel_qty  (default, safe)
  - stock_mode="OPENING" → overwrite with excel_qty  (opening reset, admin-controlled)

Applies to:  OPHLENS, CLENS, BLANK, FRAME
Does NOT apply to: PRODUCT, PARTY, PATIENT, SOL (masters — always upsert)

FIXES APPLIED vs original scripts:
  - All DB_CONFIG removed — uses WIN16 sql_adapter
  - Sol_batch leading-space column names → stripped
  - product_master trailing-space column names → stripped
  - import_power bug: undefined `product` variable → fixed to `pname`
  - blankimport input() confirm removed (UI handles it)
  - Party ROLETYPE normalized to DB enum
  - ophupdater fuzzy match now logs correctly
  - All files: NaN → None using _sanitize_value from sql_adapter
  - Batch-level try/except so one bad row never kills the whole file
  - Pre-load column validation before any DB call
  - _CANONICAL_COLUMNS: added all snake_case DB column names so AI mapper
    never warns "Unknown column product_name / item_type / is_active etc."
  - _canonical_to_db_columns bridge: completed with ALL loaders' columns
    (was only 7 entries, now covers every table the loaders write to)

INGESTION SHIELD (added — replaces need for separate excel_sanitizer.py):
  - Alias resolution  : "Product Name", "product_name", "Product" → "productname"
  - AI fuzzy mapping  : unknown headers auto-mapped via difflib similarity
  - Cell cleaning     : strips whitespace, collapses internal spaces, removes \u00a0
  - Schema defaults   : injects missing optional columns (isactive, allowloose, etc.)
  - Empty col removal : drops fully-empty columns from poorly formatted exports
  - IngestionReport   : quality score + warning collector surfaced to UI
  - Channel detection : detects online/channel pricing fields in uploads
"""

import re
import uuid
import hashlib
import logging
import math
from datetime import datetime
from difflib import get_close_matches
from typing import Dict, List, Tuple, Optional

import pandas as pd

# Use WIN16's hardened DB layer — no raw psycopg2 here
from modules.sql_adapter import run_query, run_write, get_connection, get_transaction_connection, close_connection

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# AUDIT HELPERS
# ═══════════════════════════════════════════════════════

def generate_import_id() -> str:
    """Generate a unique UUID for each import run."""
    return str(uuid.uuid4())


def row_hash(row: dict) -> str:
    """
    MD5 fingerprint of a data row.
    Same data always produces the same hash — used for dedup protection.
    Prevents the same Excel from being double-imported in ADD mode.
    """
    raw = str(sorted(row.items()))
    return hashlib.md5(raw.encode()).hexdigest()


def _log_import_to_db(
    import_id: str,
    file_name: str,
    file_type: str,
    mode: str,
    stock_mode: str,
    result: "LoadResult",
    user: str = "system",
    status: str = "OK",
) -> None:
    """
    Write one audit row to loader_import_log.
    Silent on failure — audit must never crash an import.
    """
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
                file_type,
                mode,
                stock_mode,
                user,
                result.total_rows,
                result.inserted + result.updated,
                result.skipped,
                len(result.errors),
                result.duration_seconds,
                status,
            ),
        )
        logger.info(f"[AUDIT] Import logged: {import_id} | {file_type} | {status}")
    except Exception as e:
        logger.warning(f"[AUDIT] Could not write to loader_import_log: {e}")


def _log_row_hashes(import_id: str, file_type: str, hashes: list) -> None:
    """
    Persist new row hashes to loader_row_history for dedup.
    Silent on failure — audit must never crash an import.
    """
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
                (h, import_id, file_type),
            )
    except Exception as e:
        logger.warning(f"[AUDIT] Could not write row hashes: {e}")


def _load_seen_hashes(file_type: str) -> set:
    """
    Fetch all previously-imported row hashes for this file_type.
    Used to skip duplicate rows on re-upload.
    """
    try:
        rows = run_query(
            "SELECT row_hash FROM loader_row_history WHERE file_type = %s",
            (file_type,),
        )
        return {r["row_hash"] for r in (rows or [])}
    except Exception as e:
        logger.warning(f"[AUDIT] Could not load seen hashes (dedup disabled): {e}")
        return set()


# ═══════════════════════════════════════════════════════
# ENUMS & CONTRACTS
# ═══════════════════════════════════════════════════════

# What DB actually accepts for party_type
VALID_PARTY_TYPES = {
    "Retail":               "Retail",
    "Doctor":               "Doctor",
    "Optician":             "Optician",
    "Supplier":             "Supplier",
    "Fitter":               "Fitter",
    "WholeSeller":          "Wholesale",
    "Wholesale":            "Wholesale",
    "Cosmetics":            "Retail",          # map to nearest
    "Company Sales Person": "Supplier",
    "Optometerist":         "Doctor",          # typo in source data
    "Optometrist":          "Doctor",
}

VALID_EYE_SIDES = {"R", "L", "B"}

VALID_LENS_DESIGNS = {"SPHERICAL", "TORIC", "MULTIFOCAL"}

VALID_STOCK_TYPES = {"POWER", "BATCH", "SIMPLE"}

# ── Stock mode constants ─────────────────────────────
STOCK_MODE_ADD        = "ADD"         # accumulate: existing + excel qty
STOCK_MODE_OPENING    = "OPENING"     # overwrite:  set to excel qty
STOCK_MODE_PRICE_ONLY = "PRICE_ONLY"  # update prices only — qty untouched

# File types that support dual stock_mode
STOCK_MODE_SUPPORTED = {"OPHLENS", "CLENS", "BLANK", "FRAME"}

# Admin flag: set False to lock out OPENING mode system-wide
# Can be overridden from DB settings table later
ALLOW_OPENING_MODE = True

# Excel → DB column maps per loader type (after header normalization)
PRODUCT_COLUMN_MAP = {
    "product":            "product_name",
    "productname":        "product_name",
    "brandproductgroup":  "brand_group",
    "maingroup":          "main_group",
    "type":               "category",
    "lenscategory":       "lens_category",
    "index":              "index_value",
    "isbatchapplicable":  "is_batch_applicable",
    "iseyespecific":      "is_eye_specific",
    "isactive":           "is_active",
    "hsncode":            "hsn_code",
    "boxsize":            "box_size",
    "allowloose":         "allow_loose",
    "coatingtype":        "coating_type",
    "brand":              "brand",
    "material":           "material",
    "coating":            "coating",
    "colour":             "colour",
    "color":              "colour",
    "unit":               "unit",
    "wearschedule":       "wear_schedule",
    "gender":             "gender",
    "gstpercent":         "gst_percent",
    "gst":                "gst_percent",
    "gst%":               "gst_percent",
    "suppliertransitdays":"supplier_tat_days",
    "preferredsupplier":  "preferred_supplier_id",
    "suppliertatdays":    "supplier_tat_days",
    "basecurve":          "base_curve",
    "diameter":           "diameter",
    "dkt":                "dk_t_value",
    "dkvalue":            "dk_value",
    "watercontent":       "water_content",
    "ct":                 "ct_value",
    "modulus":            "modulus_mpa",
    "uvblocking":         "uv_blocking",
}

FRAME_COLUMN_MAP = {
    "skucode":       "sku_code",
    "sku":           "sku_code",
    "barcode":       "sku_code",    # Barcode column in Excel = sku_code/batch_no
    "asize":         "size_a",
    "sizea":         "size_a",
    "bsize":         "size_b",      # ← NEW (from migration)
    "sizeb":         "size_b",      # ← NEW
    "templelength":  "temple_length",
    "basematerial":  "base_material",
    "material":      "base_material",
    "colourmix":     "colour_mix",  # ← NEW (from migration)
    "colormix":      "colour_mix",  # ← NEW
    "templecolour":  "temple_colour",  # ← NEW (from migration)
    "templecolor":   "temple_colour",  # ← NEW
    "startcode":     "location",    # ← NEW (from migration) StartCode = rack/bin location
    "framegroup":    "frame_group", # ← NEW (from migration)
    "frametype":     "frame_type",  # ← NEW (from migration)
    "frameseq":      "frame_seq",   # ← NEW display/sort sequence
    "sellingprice":  "selling_price",  # ← NEW (from migration)
    "gender":        "gender",      # ← NEW (from migration)
    "model":         "model",
    "costprice":     "cost_price",
    "purchaseprice": "cost_price",  # "Purchase price" column in Excel
    "purchaserate":  "cost_price",
    "imagepath":     "image_path",
    "isactive":      "is_active",
    "product":       "product_name",
    "productname":   "product_name",
    "qty":           "qty",
    "quantity":      "qty",
    "colour":        "colour",
    "color":         "colour",
    "shape":         "shape",
    "finish":        "finish",
    "brand":         "brand",
    "dbl":           "dbl",
    "mrp":           "mrp",
}

OPHLENS_COLUMN_MAP = {
    "productname":      "product_name",
    "product":          "product_name",
    "add":              "add_power",
    "addpower":         "add_power",
    "eyeside":          "eye_side",
    "lenssideeyeside":  "eye_side",
    "batchno":          "batch_no",
    "expirydate":       "expiry_date",
    "qty":              "quantity",
    "quantity":         "quantity",
    # Bonzer / stock template: qty_right + qty_left → qty + eye_side rows
    "qtyright":         "qty_right",
    "qtyleft":          "qty_left",
    "qtyindependent":   "qty_independent",
    "purchaserate":     "purchase_rate",
    "costprice":        "purchase_rate",
    "sellingprice":     "selling_price",
    "isactive":         "is_active",
    "lenscategory":     "lens_category",
    "category":         "lens_category",
    "wearschedule":     "wear_schedule",
    "lensdesign":       "lens_design",
    "itemtype":         "item_type",
    "rxorstock":        "item_type",
    "type":             "item_type",
    "coating":          "coating",
    "coatingtype":      "coating",
    "barcode":          "barcode",
    "productbarcode":   "barcode",
    "indexvalue":       "index_value",
    "index":            "index_value",
    "colour":           "colour",
    "color":            "colour",
    "brand":            "brand",
    "material":         "material",
    "minstockright":    "min_stock_right",
    "minstockleft":     "min_stock_left",
    "minstock":         "min_stock",
    "location":         "location",
}

# Separate map for CLENS — identical but explicit
CLENS_COLUMN_MAP = {
    "product":            "product_name",   # template uses db column names, normaliser strips _ so productname must map
    "productname":        "product_name",
    "add":                "add_power",
    "addpower":           "add_power",
    "eyeside":            "eye_side",
    "batchno":            "batch_no",
    "expirydate":         "expiry_date",
    "qty":                "quantity",
    "quantity":           "quantity",
    "qtyboxes":           "qty_boxes",
    "boxes":              "qty_boxes",
    "qtyinboxes":         "qty_boxes",
    "loosepcs":           "loose_pcs",
    "loose":              "loose_pcs",
    "extraloosepcs":      "loose_pcs",
    # ── PCS / PAIR download columns ─────────────────────────────────────────
    # Download sends qty_display (pair-aware) + qty_unit ('PAIRS'/'PCS')
    # Loader reads these and converts back to PCS for DB storage
    "qtydisplay":         "qty_display",    # unit-aware qty from download
    "qtyunit":            "qty_unit",       # 'PAIRS' or 'PCS'
    "unit":               "unit",           # product unit from products table
    # ────────────────────────────────────────────────────────────────────────
    "companyproductname": "company_product_name",
    "supplierproductname":"company_product_name",
    "invoiceproductname": "company_product_name",
    "qtyinboxes":    "qty_boxes",
    "packsize":           "pack_size",
    "uomentry":           "uom_entry",
    "uomdb":              "uom_db",
    "purchaserate":  "purchase_rate",
    "costprice":     "purchase_rate",
    "sellingprice":  "selling_price",
    "isactive":      "is_active",
    "lensdesign":    "lens_design",
    "itemtype":      "item_type",
    "rxorstock":     "item_type",
    "type":          "item_type",
}

SOL_COLUMN_MAP = {
    "product":       "product_name",
    "productname":   "product_name",
    "batchno":       "batch_no",
    "expirydate":    "expiry_date",
    "qty":           "quantity",
    "costprice":     "cost_price",
    "sellingprice":  "selling_price",
    "isactive":      "is_active",
    "mrp":           "mrp",
}

PARTY_COLUMN_MAP = {
    "partyname":      "party_name",
    "roletype":       "party_type",
    "partytype":      "party_type",
    "altmobile":      "alt_mobile",
    "alternatemobile":"alt_mobile",
    "contactperson":  "contact_person",
    "contact":        "contact_person",
    "email":          "email",
    "pincode":        "pincode",
    "pin":            "pincode",
    "state":          "state_name",
    "statename":      "state_name",
    "statecode":      "state_code",
    "gstin":          "gstin",
    "gstno":          "gstin",
    "gstnumber":      "gstin",
    "pan":            "pan_no",
    "panno":          "pan_no",
    "tan":            "tan_no",
    "tanno":          "tan_no",
    "cin":            "cin_no",
    "cinno":          "cin_no",
    "gstrate":        "gst_rate",
    "gstpercent":     "gst_rate",
    "creditlimit":    "credit_limit",
    "creditdays":     "credit_days",
    "openingbalance": "opening_balance",
    "openingbal":     "opening_balance",
    "balancetype":    "balance_type",
    "tallygroup":     "tally_group",
    "tallyname":      "tally_group",
    "notes":          "notes",
    "remarks":        "notes",
    "mobile":     "mobile",
    "address":    "address",
    "city":       "city",
    "area":       "area",
    "isactive":   "is_active",
}

PATIENT_COLUMN_MAP = {
    "recordno":    "record_no",
    "clientname":  "master_name",
    "mobilenumber":"mobile",
    "date":        "visit_date",
    "rightsph":    "right_sph",
    "rightcyl":    "right_cyl",
    "rightaxis":   "right_axis",
    "rightaddpower":"right_add",
    "leftsph":     "left_sph",
    "leftcyl":     "left_cyl",
    "leftaxis":    "left_axis",
    "leftaddpower":"left_add",
}

BLANK_COLUMN_MAP = {
    "add":                      "add_power",
    "qtyright":                 "qty_right",
    "qtyleft":                  "qty_left",
    "qtyindependent":           "qty_independent",
    "recomendedbase":           "base_recommended",
    "recommendedbase":          "base_recommended",
    "base1p":                   "base_1",
    "base2p":                   "base_2",
    "base3p":                   "base_3",
    "category":                 "category",
    "material":                 "material",
    "colour":                   "colour",
    "color":                    "colour",
    "brand":                    "brand",
    "costprice":                "cost_price",
    "cost":                     "cost_price",
    "purchaseprice":            "cost_price",
    "price":                    "cost_price",
    "minstock":                 "min_stock",
    "minimumstock":             "min_stock",
    "barcode":                  "barcode",
    "itemcode":                 "item_code",
    "isactive":                 "is_active",
    "companybillingname":       "company_billing_name",
    "supplierbillingname":      "company_billing_name",
    "billingname":              "company_billing_name",
    "supplierproductname":      "company_billing_name",
    "invoicename":              "company_billing_name",
}

# Required columns per file type — derived from registry, with fallback hardcoded values
def _get_required_columns(file_type: str):
    """Get required DB columns for a file type from registry, with static fallback."""
    try:
        from modules.loaders.db_schema_registry import get_required_cols
        reg_required = get_required_cols(file_type)
        if reg_required:
            return reg_required
    except Exception:
        pass
    # Static fallback — used if registry unavailable
    return {
        "PRODUCT":  ["product_name"],
        "OPH_SPEC":  ["product", "index_value", "coating"],
        "OPH_ADDON": ["brand", "addon_name"],
        "FRAME":    ["batch_no"],
        "PARTY":    ["party_name"],
        "PATIENT":  ["master_name"],
        "OPHLENS":  ["product_name"],
        "CLENS":    ["product_name", "quantity"],
        "SOL":      ["product_name", "quantity"],
        "BLANK":    ["brand", "category", "material", "add_power"],
    }.get(file_type, [])

# Keep REQUIRED_COLUMNS dict for backward compatibility — reads from registry
REQUIRED_COLUMNS = {
    "PRODUCT":  ["product_name"],
    "FRAME":    ["batch_no", "product_name", "quantity", "mrp"],
    "PARTY":    ["party_name"],
    "PATIENT":  ["master_name"],
    "OPHLENS":  ["product_name", "quantity"],
    "CLENS":    ["product_name", "quantity"],
    "SOL":      ["product_name", "quantity"],
    "BLANK":    ["brand", "category", "material", "add_power"],
}


# ═══════════════════════════════════════════════════════
# VALUE HELPERS
# ═══════════════════════════════════════════════════════

def _clean(v):
    """NaN / empty → None. Strip strings."""
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
        if pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return v


def _safe_str(v, default=None) -> Optional[str]:
    v = _clean(v)
    return str(v).strip() if v is not None else default


def _safe_num(v, default=None):
    """Return float or None — used for optional numeric spec columns."""
    if v is None: return default
    try:
        s = str(v).strip()
        if s in ('', 'None', '-', 'nan', 'N/A'): return default
        return float(s)
    except (ValueError, TypeError):
        return default

def _safe_float(v, default=None) -> Optional[float]:
    """Raw float — use for optical powers (SPH, CYL, ADD) where full precision needed."""
    v = _clean(v)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _safe_money(v, default=None):
    """ERP-grade decimal money handling — no float drift.
    Uses Decimal(str(v)) to avoid IEEE-754 representation errors.
    Banker rounding replaced by ROUND_HALF_UP (standard for invoices/GST).
    Returns Python Decimal — psycopg2 maps this to NUMERIC(x,2) exactly.

    e.g. round(float(10.235), 2) = 10.23  ❌ (float drift)
         _safe_money(10.235)     = 10.24  ✅ (ROUND_HALF_UP)
    """
    from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
    v = _clean(v)
    if v is None:
        return default
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return default


def _safe_int(v, default=None) -> Optional[int]:
    v = _clean(v)
    if v is None:
        return default
    try:
        return int(float(v))
    except Exception:
        return default


def _safe_bool(v, default=False) -> bool:
    if v is None:
        return default
    return str(v).strip().upper() in ("1", "TRUE", "YES", "Y", "T")


# ── Canonical main_group map — ensures consistent casing on every DB write ───
_MAIN_GROUP_CANONICAL = {
    "ophthalmic lenses":  "Ophthalmic Lenses",
    "contact lenses":     "Contact Lenses",
    "frames":             "Frames",
    "sunglasses":         "Sunglasses",
    "solution":           "Solution",
    "solutions":          "Solution",
    "service":            "Service",
    "services":           "Service",
    "accessories":        "Accessories",
    "cloth":              "Accessories",
    "blank":              "Blank",
}

def _canonical_main_group(val) -> str:
    """
    Normalise main_group to canonical title-case form on every write.
    Prevents 'OPHTHALMIC LENSES' vs 'Ophthalmic Lenses' split forever.
    """
    if not val:
        return val
    key = str(val).strip().lower()
    return _MAIN_GROUP_CANONICAL.get(key, str(val).strip().title())


def _safe_date(v):
    if _clean(v) is None:
        return None
    try:
        d = pd.to_datetime(v, errors="coerce")
        return d.date() if not pd.isna(d) else None
    except Exception:
        return None


def _safe_axis(v) -> Optional[int]:
    v = _safe_int(v)
    if v is None:
        return None
    return v if 0 <= v <= 180 else None


def _safe_mobile(v) -> Optional[str]:
    v = _clean(v)
    if v is None:
        return None
    try:
        return str(int(float(v)))
    except Exception:
        return str(v).strip() if v else None


def _norm_party_type(v: str) -> str:
    v = _safe_str(v, "Retail")
    return VALID_PARTY_TYPES.get(v, "Retail")


def _norm_eye(v) -> str:
    v = _safe_str(v, "B")
    return v.upper() if v.upper() in VALID_EYE_SIDES else "B"


def _detect_lens_design(sph, cyl, axis, add) -> str:
    if add not in (None, 0.0, 0):
        return "MULTIFOCAL"
    if cyl not in (None, 0.0, 0) and axis not in (None, 0, 0.0):
        return "TORIC"
    return "SPHERICAL"


def _normalize_header(col: str) -> str:
    """strip, lower, remove spaces and underscores — universal normalization"""
    return col.strip().lower().replace(" ", "").replace("_", "")


def _get_registry_col_map(file_type: str) -> dict:
    """
    Build normalised excel_header → db_column map.
    Sources (merged in priority order):
      1. live_schema_bridge — DB-authoritative, includes new columns automatically
      2. db_schema_registry — static metadata fallback
    Any new column added to the DB is immediately accepted on upload.
    """
    result = {}
    # Fallback: static registry
    try:
        from modules.loaders.db_schema_registry import get_column_map
        result.update(get_column_map(file_type))
    except Exception:
        pass
    # Primary: live bridge (overwrites registry — DB is authoritative)
    try:
        from modules.loaders.live_schema_bridge import get_upload_col_map
        result.update(get_upload_col_map(file_type))
    except Exception:
        pass
    return result


# ═══════════════════════════════════════════════════════
# STOCK MODE GUARD
# ═══════════════════════════════════════════════════════

def _resolve_stock_mode(stock_mode: str, file_type: str) -> str:
    """
    Validates and resolves the effective stock_mode.
    Returns ADD, OPENING, or PRICE_ONLY.
    Falls back to ADD if OPENING is globally disabled.
    Falls back to ADD for file types that don't support OPENING.
    """
    requested = (stock_mode or STOCK_MODE_ADD).upper()

    if requested == STOCK_MODE_OPENING:
        if not ALLOW_OPENING_MODE:
            logger.warning("OPENING mode requested but disabled by admin — falling back to ADD")
            return STOCK_MODE_ADD
        if file_type not in STOCK_MODE_SUPPORTED:
            logger.info(f"stock_mode=OPENING has no effect on {file_type} — using ADD logic")
            return STOCK_MODE_ADD

    return requested if requested in (STOCK_MODE_ADD, STOCK_MODE_OPENING, STOCK_MODE_PRICE_ONLY) else STOCK_MODE_ADD


def _compute_new_qty(existing_qty: int, excel_qty: int, stock_mode: str) -> int:
    """
    Core quantity resolution.
    ADD:        accumulate  (trial phase corrections, ongoing stock loads)
    OPENING:    overwrite   (set opening stock, migration resets)
    PRICE_ONLY: no change   (return existing qty unchanged)
    """
    if stock_mode == STOCK_MODE_OPENING:
        return excel_qty
    if stock_mode == STOCK_MODE_PRICE_ONLY:
        return existing_qty or 0   # qty unchanged — prices updated by caller
    return (existing_qty or 0) + excel_qty


# ═══════════════════════════════════════════════════════
# INGESTION SHIELD
# Four-stage pipeline applied inside load_excel():
#   1. _resolve_header_aliases  → semantic alias resolution
#   2. _ai_map_headers          → fuzzy match unknown columns
#   3. _clean_excel_df          → strip cells, drop empty columns
#   4. _auto_fix_schema         → inject missing optional columns
#
# IngestionReport tracks all auto-fixes and produces a quality score
# that is surfaced in the UI after each upload.
# ═══════════════════════════════════════════════════════

# Semantic aliases resolved AFTER _normalize_header() has already stripped/lowercased.
_HEADER_ALIASES = {
    # ── Product name variants ─────────────────────────────────────────────────
    "product":           "productname",
    "producttitle":      "productname",
    "productnames":      "productname",
    "itemname":          "productname",
    # ── DB column name aliases (so db_column names work same as excel_header) ─
    # These allow files with db_column headers (product_name, batch_no etc.)
    # to be uploaded just like files with excel_header names (Product, BatchNo)
    "productname":       "productname",   # product_name → productname
    "maingroup":         "maingroup",     # main_group
    "lenscat":           "lenscategory",  # lens_category → lenscategory  
    "lenscategory":      "lenscategory",
    "brandgroup":        "brandproductgroup",  # brand_group
    "brandproductgroup": "brandproductgroup",
    "indexvalue":        "index",         # index_value → index
    "coatingtype":       "coatingtype",
    "wearsched":         "wearsched",
    "wearschedule":      "wearsched",
    "isbatchapplicable": "isbatchapplicable",
    "iseyespecific":     "iseyespecific",
    "hsncode":           "hsncode",
    "boxsize":           "boxsize",
    "allowloose":        "allowloose",
    "gstpercent":        "gstpercent",
    "isactive":          "isactive",
    "preferredsupplier": "preferredsupplier",  # preferred_supplier_id
    "preferredsupplierid": "preferredsupplier",
    "suppliertatdays":   "suppliertatdays",
    "createsource":      "createdsource",  # created_source
    "createdsource":     "createdsource",
    "skucode":           "skucode",
    "autofulfillment":   "autofulfillment",
    "minstockqty":       "minstockqty",
    "reorderenabled":    "reorderenabled",
    # ── CLENS / OPHLENS ──────────────────────────────────────────────────────
    "batchno":           "batch_no",
    "batchnumber":       "batch_no",
    "batch_no":          "batch_no",
    "itemcode":          "item_code",
    "item_code":         "item_code",
    "expirydate":        "expiry_date",
    "expiry":            "expiry_date",
    "expiry_date":       "expiry_date",
    "addpower":          "add",           # add_power → add
    "eyeside":           "eyeside",
    "purchaserate":      "purchaserate",
    "sellingprice":      "sellingprice",
    "itemtype":          "itemtype",
    "lensdesign":        "lensdesign",
    "location":          "location",
    "alconitemname":     "alconitemname",
    "materialcode":      "materialcode",
    # ── Party ────────────────────────────────────────────────────────────────
    "roletype":          "partytype",
    "partytype":         "partytype",
    # ── General ──────────────────────────────────────────────────────────────
    "color":             "colour",
    "colour":            "colour",
    # NOTE: quantity and qty are both valid — handled by per-loader COLUMN_MAP
    # Do NOT alias quantity→qty here as it causes REQUIRED_COLUMNS check to fail
}

# Full canonical column list — used by AI fuzzy mapper.
# Contains BOTH normalised (no-underscore) AND snake_case DB forms so the
# AI mapper never flags post-bridge column names as "unknown".
_CANONICAL_COLUMNS = [
    # ── Normalised (no-underscore) forms ──────────────────────────────────
    "productname", "maingroup", "brandproductgroup", "brand", "type",
    "lenscategory", "index", "isbatchapplicable", "iseyespecific",
    "isactive", "hsncode", "boxsize", "allowloose", "coatingtype",
    "material", "coating", "colour", "unit", "wearschedule", "gender",
    "skucode", "sku", "asize", "sizea", "templelength", "basematerial",
    "costprice", "imagepath", "qty",
    "add", "addpower", "eyeside", "batchno", "expirydate", "purchaserate",
    "sellingprice", "lensdesign", "itemtype",
    "partyname", "partytype", "mobile", "address", "city", "area",
    "recordno", "clientname", "mobilenumber", "date",
    "rightsph", "rightcyl", "rightaxis", "rightaddpower",
    "leftsph", "leftcyl", "leftaxis", "leftaddpower",
    "sph", "cyl", "axis", "mrp",
    "qtyright", "qtyleft", "qtyindependent",
    "recommendedbase", "base1p", "base2p", "base3p", "category",
    "onlineprice", "amazonprice", "shopifyprice", "channelsku",
    # ── snake_case DB column names (post-bridge) ──────────────────────────
    # Without these the AI mapper fires "Unknown column product_name" on every
    # file that comes through the bridge with DB-native column names.
    "product_name", "purchase_rate", "selling_price", "eye_side",
    "add_power", "lens_design", "is_active", "item_type",
    "is_batch_applicable", "is_eye_specific", "allow_loose",
    "stock_type", "batch_no", "expiry_date", "main_group",
    "brand_group", "lens_category", "hsn_code", "box_size",
    "coating_type", "wear_schedule", "index_value",
    "qty_right", "qty_left", "qty_independent",
    "base_recommended", "cost_price", "min_stock",
    "party_name", "party_type", "record_no", "visit_date",
    "right_sph", "right_cyl", "right_axis", "right_add",
    "left_sph", "left_cyl", "left_axis", "left_add",
    "sku_code", "temple_length", "base_material", "size_a",
    "qty_available", "master_name",
]

# Optional columns injected when absent — keeps loaders from KeyError-ing.
_SCHEMA_DEFAULTS = {
    "isactive":          True,
    "isbatchapplicable": False,
    "iseyespecific":     False,
    "allowloose":        True,
}


# ── IngestionReport ──────────────────────────────────────────────────────────

class IngestionReport:
    """
    Tracks all auto-fixes and warnings during the ingestion pipeline,
    then computes a quality score (0–100) for display in the UI.

    Usage:
        report = IngestionReport()
        df = _resolve_header_aliases(df, report)
        df = _ai_map_headers(df, report)
        df = _clean_excel_df(df, report)
        df = _auto_fix_schema(df, report)
        report.finalize()
        # report.score, report.warnings now available
    """
    def __init__(self):
        self.warnings: List[str] = []
        self.auto_fixes: int = 0
        self.missing_required: int = 0
        self.empty_rows: int = 0
        self.empty_cols: int = 0
        self.score: int = 100

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def penalize(self, points: int):
        self.score = max(0, self.score - points)

    def finalize(self):
        """Compute final score. Call after all pipeline stages complete."""
        self.score -= self.auto_fixes * 2
        self.score -= self.missing_required * 10
        self.score -= self.empty_cols * 3
        self.score -= self.empty_rows * 1
        self.score = max(0, min(100, self.score))

    def summary(self) -> dict:
        return {
            "score":            self.score,
            "auto_fixes":       self.auto_fixes,
            "missing_required": self.missing_required,
            "empty_cols":       self.empty_cols,
            "empty_rows":       self.empty_rows,
            "warning_count":    len(self.warnings),
            "warnings":         self.warnings,
        }


# ── Stage 1 — Alias resolution ───────────────────────────────────────────────

def _resolve_header_aliases(df: pd.DataFrame, report: IngestionReport = None) -> pd.DataFrame:
    """Resolve known semantic aliases to canonical column names."""
    new_cols = []
    for col in df.columns:
        canonical = _HEADER_ALIASES.get(col, col)
        if report and canonical != col:
            report.auto_fixes += 1
            report.add_warning(f"Header normalized: '{col}' → '{canonical}'")
        new_cols.append(canonical)
    df.columns = new_cols
    return df


# ── Stage 2 — Schema-aware AI header mapping ─────────────────────────────────
# Old blind global fuzzy matching removed.
# Replaced with intelligent_ai_mapping() from ai_mapping_engine.py which:
#   - Detects table context (PRODUCT/FRAME/PARTY/etc.) before mapping
#   - Only fuzzy-matches within that table's valid column list
#   - Never maps critical financial/medical fields via fuzzy guess
#   - Uses 0.85 confidence threshold (was 0.75 — caused cross-domain corruption)
#   - Returns df unchanged if table context cannot be determined (safe fallback)

def _ai_map_headers(df: pd.DataFrame, report: IngestionReport = None) -> pd.DataFrame:
    """
    Schema-aware AI header mapping — delegates to ai_mapping_engine.intelligent_ai_mapping().
    The old global fuzzy matcher has been replaced with a table-context-aware engine.
    Kept as a named function so the load_excel() pipeline call is unchanged.
    Falls back to a no-op if ai_mapping_engine is unavailable.
    """
    try:
        from modules.loaders.ai_mapping_engine import intelligent_ai_mapping
        return intelligent_ai_mapping(df, report)
    except Exception as e:
        logger.warning(f"[AI-MAP] ai_mapping_engine unavailable — skipping AI mapping: {e}")
        return df


# ── Stage 3 — Cell cleaning ──────────────────────────────────────────────────

def _clean_excel_df(df: pd.DataFrame, report: IngestionReport = None) -> pd.DataFrame:
    """
    Silent fix layer:
      - Strip and collapse whitespace in all string cell values
      - Remove non-breaking spaces (common Excel copy-paste artefact)
      - Convert stringified 'nan' / 'None' back to actual None
      - Drop fully-empty columns
      - Drop columns with null/empty headers (unnamed Excel artefacts)
    """
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(r"\s+", " ", regex=True)
                .str.replace("\u00a0", " ", regex=False)
                .str.strip()
                .replace({"nan": None, "None": None, "NaN": None})
            )

    # Drop fully-empty columns
    empty_cols = [c for c in df.columns if df[c].isna().all()]
    if empty_cols:
        if report:
            report.empty_cols += len(empty_cols)
            report.add_warning(f"Dropped {len(empty_cols)} empty column(s): {empty_cols}")
        df = df.drop(columns=empty_cols)

    # Drop columns with null or blank headers
    df = df.loc[:, df.columns.notna()]
    df = df.loc[:, df.columns.astype(str).str.strip() != ""]

    # Warn about fully-empty rows (don't drop — let loaders handle them)
    empty_rows = int(df.isnull().all(axis=1).sum())
    if empty_rows and report:
        report.empty_rows += empty_rows
        report.add_warning(f"Detected {empty_rows} fully empty row(s) in file")

    return df


# ── Stage 4 — Schema defaults ────────────────────────────────────────────────

def _auto_fix_schema(df: pd.DataFrame, report: IngestionReport = None) -> pd.DataFrame:
    """
    Inject missing optional columns with safe defaults so loaders never
    KeyError on commonly absent-but-expected fields.
    """
    for col, default in _SCHEMA_DEFAULTS.items():
        if col not in df.columns:
            df[col] = default
            if report:
                report.auto_fixes += 1
                report.add_warning(
                    f"Auto-added missing column '{col}' (default: {default})"
                )
    return df


# ═══════════════════════════════════════════════════════
# EXCEL READER & AUTO-DETECTOR
# ═══════════════════════════════════════════════════════

def load_excel(path: str) -> Tuple[pd.DataFrame, IngestionReport]:
    """
    Load Excel/CSV with full header normalization + ingestion shield.
    """

    report = IngestionReport()

    # --- read file ---
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path, dtype=str)
    else:
        # Always read 'Data' or 'New Records' sheet if present — never the Guide sheet
        _wb_sheets = None
        try:
            import openpyxl as _opx
            _wb_tmp = _opx.load_workbook(path, read_only=True, data_only=True)
            _wb_sheets = _wb_tmp.sheetnames
            _wb_tmp.close()
        except Exception:
            pass
        _preferred = ['Data', 'New Records']
        _sheet = next((s for s in _preferred if _wb_sheets and s in _wb_sheets), None)
        df = pd.read_excel(path, engine="openpyxl", dtype=str,
                           sheet_name=_sheet if _sheet else 0)

    # --- normalize headers (BOM + trim only) ---
    df.columns = (
        df.columns
        .astype(str)
        .str.replace("\ufeff", "", regex=False)
        .str.strip()
    )

    # Stage 1 — original normalization
    df.columns = [_normalize_header(c) for c in df.columns]

    # Stage 2–5 — ingestion shield with reporting
    df = _resolve_header_aliases(df, report)
    df = _ai_map_headers(df, report)
    df = _clean_excel_df(df, report)
    df = _auto_fix_schema(df, report)

    # ✅ NEW FIX — REMOVE EMPTY ROWS
    before = len(df)
    df = df.dropna(how="all").reset_index(drop=True)
    removed = before - len(df)
    if removed and report.empty_rows == 0:
        report.add_warning(f"Removed {removed} fully empty row(s)")

    # Stage 6 — channel detection (informational only)
    try:
        from modules.loaders.channel_detector import detect_channel_columns
        channel_cols = detect_channel_columns(df)
        if channel_cols:
            report.add_warning(f"📡 Channel fields detected: {channel_cols}")
    except Exception:
        pass  # optional module

    df = _canonical_to_db_columns(df)
    report.finalize()
    return df, report

def load_excel_df(path: str) -> pd.DataFrame:
    """
    Convenience wrapper around load_excel() for callers that only need the DataFrame.
    Discards the IngestionReport.

    Use this in loader_ui.py for file preview:
        df = load_excel_df(path)
    """
    df, _ = load_excel(path)
    return df


def detect_file_type(df: pd.DataFrame) -> str:
    """
    Detect what kind of file this is from its normalized columns.
    Returns: PRODUCT | FRAME | PARTY | PATIENT | OPHLENS | CLENS | SOL | BLANK | UNKNOWN
    """
    cols = set(df.columns)

    # Patient: has mobilenumber + rightsph
    if "mobilenumber" in cols and "rightsph" in cols:
        return "PATIENT"

    # Party
    if "partyname" in cols:
        return "PARTY"

    # Frame: has skucode
    if "skucode" in cols or "sku" in cols:
        return "FRAME"

    # Blank inventory: has qtyright or qtyleft
    if "qtyright" in cols or "qtyleft" in cols:
        return "BLANK"

    # Product master: has lenscategory + brand + product (no sph/cyl)
    if "lenscategory" in cols and "brand" in cols and "sph" not in cols and "qty" not in cols:
        return "PRODUCT"

    # Contact lens: has lenscategory column (clens_batch.xlsx always has it)
    if "sph" in cols and "cyl" in cols and "lenscategory" in cols:
        return "CLENS"

    # Contact lens: detect by wear schedule / BC / DIA columns (contact-lens-only fields)
    _clens_signals = {"wearschedule", "basecurve", "bc", "dia", "diameter",
                      "disposalperiod", "replacementschedule", "wearduration"}
    if "sph" in cols and _clens_signals & cols:
        return "CLENS"

    _has_batch     = "batchno"    in cols or "batch_no"    in cols
    _has_expiry    = "expirydate" in cols or "expiry_date"  in cols
    _has_item_type = "itemtype"   in cols or "item_type"    in cols

    # Check if batch column actually has data (not just present as empty column)
    # OPHLENS files downloaded from system include batch_no/expiry_date columns
    # but they are always empty — CLENS files have actual batch values
    def _col_has_data(col_names):
        for c in col_names:
            if c in df.columns:
                return df[c].notna().any() and (df[c].astype(str).str.strip() != "").any()
        return False

    _batch_has_data  = _col_has_data(["batchno", "batch_no"])
    _expiry_has_data = _col_has_data(["expirydate", "expiry_date"])

    # OPHLENS: has item_type + sph — strongest OPHLENS signal
    # Even if batch/expiry columns exist, if they have NO DATA → OPHLENS
    # EXCEPTION: if item_type column contains 'CATALOGUE' → this is a CLENS catalogue file
    _item_type_col = next((c for c in ["item_type","itemtype"] if c in df.columns), None)
    _has_catalogue_rows = False
    if _item_type_col:
        _has_catalogue_rows = df[_item_type_col].astype(str).str.upper().str.strip().eq("CATALOGUE").any()
    if "sph" in cols and _has_item_type and not (_batch_has_data and _expiry_has_data):
        if _has_catalogue_rows:
            return "CLENS"   # CATALOGUE rows with SPH = contact lens catalogue file
        return "OPHLENS"

    # CLENS: batch + expiry columns present AND have actual data
    if "sph" in cols and _batch_has_data and _expiry_has_data:
        return "CLENS"

    # CLENS: batch + expiry structure (columns present, trust structure)
    if "sph" in cols and _has_batch and _has_expiry and not _has_item_type:
        return "CLENS"

    # OPHLENS: sph + product name, no expiry data
    if "sph" in cols and (
        "product" in cols
        or "productname" in cols
        or "product_name" in cols
    ) and not _expiry_has_data:
        return "OPHLENS"

    # CLENS: batch alone with sph and actual batch data
    if "sph" in cols and _batch_has_data:
        return "CLENS"

    # Price master: has MRP/SellingPrice but no sph/batch — order-only product prices
    _has_price_cols = "mrp" in cols and ("selling_price" in cols or "sellingprice" in cols)
    if _has_price_cols and "sph" not in cols and "batchno" not in cols and "batch_no" not in cols:
        return "PRICE"

    # Solution/simple batch: has batchno + costprice + no sph
    if ("batchno" in cols or "costprice" in cols) and "sph" not in cols:
        return "SOL"

    return "UNKNOWN"


def apply_column_map(df: pd.DataFrame, col_map: dict, file_type: str = None) -> pd.DataFrame:
    """
    Rename Excel columns to DB column names.
    Step 1: normalize df column names (lowercase, strip spaces/underscores).
    Step 2: merge registry map + static map.
    Step 3: rename.
    """
    if file_type:
        registry_map = _get_registry_col_map(file_type)
        merged = {**col_map, **registry_map}
        col_map = merged

    # Normalize df column names so 'Product' matches key 'product',
    # 'WLP_per_pair' matches 'wlpperpair', etc.
    norm_map = {}
    for col in df.columns:
        norm = str(col).lower().replace(' ', '').replace('_', '').replace('/', '')
        norm_map[col] = norm
    df = df.rename(columns=norm_map)

        # Also handle columns already in db format (e.g. product_name -> productname after strip)
    augmented = dict(col_map)
    for v in list(col_map.values()):
        norm_v = v.lower().replace(' ', '').replace('_', '').replace('/', '')
        if norm_v not in augmented:
            augmented[norm_v] = v
    return df.rename(columns={k: v for k, v in augmented.items() if k in df.columns})


# ═══════════════════════════════════════════════════════
# PRE-LOAD VALIDATION
# ═══════════════════════════════════════════════════════

class LoadResult:
    """Holds the result of one import operation."""

    def __init__(self, file_type: str, mode: str, stock_mode: str = STOCK_MODE_ADD):
        self.file_type = file_type
        self.mode = mode                  # DRY | SHADOW | LIVE
        self.stock_mode = stock_mode      # ADD | OPENING
        self.total_rows = 0
        self.inserted = 0
        self.updated = 0
        self.skipped = 0
        self.errors: List[Dict] = []
        self.warnings: List[str] = []
        self.import_id: str = ""          # set by run_loader() — UUID for audit trail
        self.ingestion_report: IngestionReport = None  # quality score + shield warnings
        self.started_at = datetime.now()
        self.finished_at = None

    def add_error(self, row: int, field: str, msg: str, value=None):
        self.errors.append({
            "row": row,
            "field": field,
            "message": msg,
            "value": str(value) if value is not None else "",
        })

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def finish(self):
        self.finished_at = datetime.now()

    @property
    def success_rate(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return round((self.inserted + self.updated) / self.total_rows * 100, 1)

    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    def to_dict(self) -> dict:
        d = {
            "file_type":    self.file_type,
            "mode":         self.mode,
            "stock_mode":   self.stock_mode,
            "total_rows":   self.total_rows,
            "inserted":     self.inserted,
            "updated":      self.updated,
            "skipped":      self.skipped,
            "error_count":  len(self.errors),
            "success_rate": self.success_rate,
            "duration_s":   self.duration_seconds,
            "errors":       self.errors,
            "warnings":     self.warnings,
        }
        if self.ingestion_report:
            d["ingestion_report"] = self.ingestion_report.summary()
        return d


def validate_columns(df: pd.DataFrame, file_type: str, result: LoadResult) -> bool:
    """
    Check required columns exist after mapping.
    Uses registry first, falls back to static REQUIRED_COLUMNS.
    """
    required = _get_required_columns(file_type) or REQUIRED_COLUMNS.get(file_type, [])

    # OPHLENS special case: quantity OR (qty_right / qty_left / qty_independent) satisfies qty check
    if file_type == "OPHLENS":
        _qty_cols = {"quantity","qty_right","qty_left","qty_independent"}
        if not _qty_cols.intersection(set(df.columns)):
            result.add_error(0, "quantity",
                "Required column missing from Excel: 'quantity' (or qty_right / qty_left / qty_independent)", "")
            return False

    # CLENS special case: batch_no and expiry_date are not required for CATALOGUE-only files.
    # CATALOGUE rows have no batch/expiry by design — they are the full power range for ordering.
    if file_type == "CLENS" and required:
        _item_col = next((c for c in ["item_type", "itemtype"] if c in df.columns), None)
        if _item_col:
            _all_catalogue = df[_item_col].astype(str).str.upper().str.strip().isin(["CATALOGUE", "NAN", ""]).all()
            if _all_catalogue:
                required = [c for c in required if c not in ("batch_no", "expiry_date")]

    missing = [c for c in required if c not in df.columns]
    if missing:
        for c in missing:
            result.add_error(0, c, f"Required column missing from Excel: '{c}'")
        return False
    return True


def pre_validate_row(row: pd.Series, file_type: str, row_num: int, result: LoadResult) -> bool:
    """
    Basic sanity checks per row before DB call.
    Returns False if row should be skipped.
    """
    ok = True

    if file_type == "PRODUCT":
        if not _safe_str(row.get("product_name")):
            result.add_error(row_num, "product_name", "Empty product name — row skipped")
            ok = False

    elif file_type == "FRAME":
        if not _safe_str(row.get("sku_code")):
            result.add_error(row_num, "sku_code", "Empty SKU — row skipped")
            ok = False
        if _safe_float(row.get("mrp"), 0) < 0:
            result.add_error(row_num, "mrp", "Negative MRP", row.get("mrp"))
            ok = False

    elif file_type == "PARTY":
        if not _safe_str(row.get("party_name")):
            result.add_error(row_num, "party_name", "Empty party name — row skipped")
            ok = False

    elif file_type == "PATIENT":
        mobile = _safe_mobile(row.get("mobile"))
        rec = _safe_str(row.get("record_no"))
        if not mobile and not rec:
            result.add_error(row_num, "mobile/record_no", "Both mobile and record_no empty — row skipped")
            ok = False

    elif file_type in ("OPHLENS", "CLENS"):
        qty = _safe_int(row.get("quantity"), 0)
        if qty is None or qty <= 0:
            result.add_error(row_num, "quantity", f"Zero or missing quantity ({qty}) — row skipped")
            ok = False
        cyl = _safe_float(row.get("cyl"))
        axis = _safe_int(row.get("axis"))
        if cyl and cyl != 0 and (axis is None or axis == 0):
            result.add_error(row_num, "axis", "TORIC has CYL but missing AXIS — row skipped", cyl)
            ok = False

    elif file_type == "SOL":
        if not _safe_str(row.get("product_name")):
            result.add_error(row_num, "product_name", "Empty product — row skipped")
            ok = False

    elif file_type == "OPH_ADDON":
        if not _safe_str(row.get("brand")):
            result.add_error(row_num, "brand", "Empty brand — row skipped"); ok=False
        if not _safe_str(row.get("addon_name")):
            result.add_error(row_num, "addon_name", "Empty add-on name — row skipped"); ok=False

    elif file_type == "OPH_SPEC":
        if not _safe_str(row.get("product")):
            result.add_error(row_num, "product", "Empty product name — row skipped")
            ok = False
        if not row.get("index_value"):
            result.add_error(row_num, "index_value", "Missing index — row skipped")
            ok = False
        if not row.get("coating"):
            result.add_error(row_num, "coating", "Missing coating — row skipped")
            ok = False

    return ok


# ═══════════════════════════════════════════════════════
# PRODUCT LOADER
# ═══════════════════════════════════════════════════════

def _import_products(df: pd.DataFrame, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    df = apply_column_map(df, PRODUCT_COLUMN_MAP)
    if not validate_columns(df, "PRODUCT", result):
        return

    # Excel-level duplicate check on product_name
    dup_names = set(df[df.duplicated("product_name", keep=False)]["product_name"].dropna())
    if dup_names:
        result.add_warning(f"Duplicate product names in Excel (will skip): {list(dup_names)[:5]}")

    _prod_inserts: list = []
    _prod_updates: list = []
    for i, row in df.iterrows():
        row_num = i + 2
        result.total_rows += 1

        name = _normalize_name(_safe_str(row.get("product_name")) or "")
        if not name:
            result.add_error(row_num, "product_name", "Empty product name")
            result.skipped += 1
            continue

        if name in dup_names:
            result.add_error(row_num, "product_name", f"Duplicate in Excel: {name}")
            result.skipped += 1
            continue

        try:
            existing = run_query(
                "SELECT product_code FROM products WHERE LOWER(product_name)=LOWER(%s)",
                (name,)
            )

            if dry_run:
                result.inserted += 1 if not existing else 0
                result.updated  += 1 if existing else 0
                continue

            # ── Resolve preferred_supplier_id from name ─────────────────────
            # Excel has supplier name (e.g. "Alcon India") — resolve to UUID
            raw_supplier_name = _safe_str(row.get("preferred_supplier_id") or
                                          row.get("preferredsupplier") or
                                          row.get("preferred_supplier") or "")
            resolved_supplier_id = None
            if raw_supplier_name:
                try:
                    sup_rows = run_query(
                        "SELECT id::text FROM parties "
                        "WHERE LOWER(TRIM(party_name)) = LOWER(TRIM(%s)) "
                        "  AND UPPER(party_type) IN ('SUPPLIER','VENDOR') "
                        "LIMIT 1",
                        (raw_supplier_name,)
                    )
                    if sup_rows:
                        resolved_supplier_id = sup_rows[0]["id"]
                    else:
                        result.add_warning(
                            f"Row {row_num}: PreferredSupplier '{raw_supplier_name}' "
                            f"not found in party master — supplier not linked for {name}"
                        )
                except Exception as _sup_ex:
                    logger.warning(
                        f"[product_loader] Row {row_num}: supplier lookup failed "
                        f"for '{raw_supplier_name}': {_sup_ex}"
                    )
                    result.add_warning(f"Row {row_num}: supplier link skipped ({_sup_ex})")

            tat_days = _safe_int(row.get("supplier_tat_days") or
                                 row.get("suppliertatdays") or
                                 row.get("SupplierTATDays"))
            # NOTE: auto_fulfillment, min_stock_qty, reorder_enabled are
            # managed per-power in product_stock_minimum — not imported here.

            # ── GST% and HSN — main_groups master ALWAYS overrides ──────────
            # Rule: main_groups.gst_percent is the single source of truth.
            # Product-level gst_percent is only used as fallback if main_groups
            # has no entry for this group.
            # This ensures a wrong 12% stored in the past is corrected
            # automatically when the correct rate (e.g. 5% for Frames) is set
            # in Main Groups master.
            main_group_name = _canonical_main_group(_safe_str(row.get("main_group")) or "")
            row_gst  = _safe_money(row.get("gst_percent"))
            row_hsn  = _safe_str(row.get("hsn_code")) or ""
            # Fallback: if gst_percent still None after Excel + main_groups, default by group
            if row_gst is None:
                _mg_lower = (main_group_name or "").lower()
                if "contact" in _mg_lower: row_gst = 5.0
                elif "ophthal" in _mg_lower or "lens" in _mg_lower: row_gst = 12.0
                elif "frame" in _mg_lower: row_gst = 12.0
                elif "solution" in _mg_lower: row_gst = 12.0
                else: row_gst = 12.0
            if main_group_name:
                try:
                    grp = run_query(
                        "SELECT gst_percent, hsn_code FROM main_groups "
                        "WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s)) LIMIT 1",
                        (main_group_name,)
                    )
                    if grp:
                        # ALWAYS take GST from main_groups if it has a value
                        if grp[0].get("gst_percent") is not None:
                            row_gst = round(float(grp[0]["gst_percent"]), 2)
                        if not row_hsn and grp[0].get("hsn_code"):
                            row_hsn = str(grp[0]["hsn_code"])
                except Exception as _mg_ex:
                    logger.warning(
                        f"[product_loader] Row {row_num}: main_groups GST lookup failed "
                        f"for '{main_group_name}': {_mg_ex} — using Excel value"
                    )
            # ────────────────────────────────────────────────────────────────

            params = (
                _safe_str(row.get("brand")),
                _safe_str(row.get("brand_group")),
                main_group_name or None,
                _safe_str(row.get("category")),
                _safe_str(row.get("lens_category")),
                _safe_str(row.get("material")),
                _safe_float(row.get("index_value")),
                _safe_str(row.get("coating")),
                _safe_str(row.get("coating_type")),
                _safe_str(row.get("colour")),
                _safe_str(row.get("unit"), "PCS"),
                _safe_str(row.get("wear_schedule")),
                _safe_str(row.get("gender"), "Unisex"),
                _safe_int(row.get("box_size"), 1),
                _safe_bool(row.get("allow_loose")),
                _safe_bool(row.get("is_batch_applicable")),
                _safe_bool(row.get("is_eye_specific")),
                row_hsn or None,
                _safe_bool(row.get("is_active"), True),
            )

            if existing:
                _prod_updates.append(params + (
                        row_gst, resolved_supplier_id, tat_days,
                        _safe_num(row.get("base_curve") or row.get("BaseCurve")),
                        _safe_num(row.get("diameter") or row.get("Diameter")),
                        _safe_num(row.get("dk_t_value") or row.get("DkT")),
                        _safe_num(row.get("water_content") or row.get("WaterContent")),
                        _safe_num(row.get("ct_value") or row.get("CT")),
                        _safe_num(row.get("modulus_mpa") or row.get("Modulus")),
                        name,
                    ))
                result.updated += 1
            else:
                _prod_inserts.append(
                    (str(uuid.uuid4()), str(uuid.uuid4()), name) + params + (
                        row_gst, resolved_supplier_id, tat_days,
                        _safe_num(row.get("base_curve") or row.get("BaseCurve")),
                        _safe_num(row.get("diameter") or row.get("Diameter")),
                        _safe_num(row.get("dk_t_value") or row.get("DkT")),
                        _safe_num(row.get("water_content") or row.get("WaterContent")),
                        _safe_num(row.get("ct_value") or row.get("CT")),
                        _safe_num(row.get("modulus_mpa") or row.get("Modulus")),
                    )
                )
                result.inserted += 1

        except Exception as e:
            result.add_error(row_num, "DB", str(e), name)
            result.skipped += 1

    if not dry_run and (_prod_inserts or _prod_updates):
        _conn = None
        try:
            from psycopg2.extras import execute_batch as _eb
            _conn = get_transaction_connection()
            _cur  = _conn.cursor()
            if _prod_inserts:
                _eb(_cur, """
                    INSERT INTO products
                    (id, product_code, product_name, brand, brand_group, main_group,
                     category, lens_category, material, index_value, coating,
                     coating_type, colour, unit, wear_schedule, gender, box_size,
                     allow_loose, is_batch_applicable, is_eye_specific,
                     hsn_code, is_active, gst_percent,
                     preferred_supplier_id, supplier_tat_days,
                     base_curve, diameter, dk_t_value, water_content,
                     ct_value, modulus_mpa, created_at)
                    VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                     %s,%s,
                     %s,%s,%s,%s,%s,%s, NOW())
                """, _prod_inserts, page_size=500)
            if _prod_updates:
                _eb(_cur, """
                    UPDATE products SET
                        brand=%s, brand_group=%s, main_group=%s, category=%s,
                        lens_category=%s, material=%s, index_value=%s,
                        coating=%s, coating_type=%s, colour=%s, unit=%s,
                        wear_schedule=%s, gender=%s, box_size=%s, allow_loose=%s,
                        is_batch_applicable=%s, is_eye_specific=%s,
                        hsn_code=%s, is_active=%s,
                        gst_percent=%s, preferred_supplier_id=%s, supplier_tat_days=%s,
                        base_curve=%s, diameter=%s, dk_t_value=%s,
                        water_content=%s, ct_value=%s, modulus_mpa=%s,
                        updated_at=NOW()
                    WHERE LOWER(product_name)=LOWER(%s)
                """, _prod_updates, page_size=500)
            _conn.commit()
        except Exception as _tx_ex:
            if _conn:
                try: _conn.rollback()
                except: pass
            result.add_error(0,"DB-BATCH",f"PRODUCT batch write failed — all rows rolled back: {_tx_ex}")
            result.inserted = 0; result.updated = 0
        finally:
            if _conn:
                try: close_connection(_conn)
                except: pass


    # ── Extension columns (new DB columns not in hardcoded SQL) ─────────────
    try:
        from modules.loaders.live_schema_bridge import write_extension_cols
        _PRODUCT_CORE = {
            "id","product_code","product_name","brand","brand_group","main_group",
            "category","lens_category","material","index_value","coating","coating_type",
            "colour","unit","wear_schedule","gender","box_size","allow_loose",
            "is_batch_applicable","is_eye_specific","hsn_code","is_active","gst_percent",
            "preferred_supplier_id","supplier_tat_days","created_at","updated_at","status",
        }
        write_extension_cols("products", df, ["product_name"], _PRODUCT_CORE, dry_run)
    except Exception as _ex:
        logger.warning(f"[products] extension cols skipped: {_ex}")


# ═══════════════════════════════════════════════════════
# FRAME LOADER
# ═══════════════════════════════════════════════════════

def _import_frames(df: pd.DataFrame, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    """
    FRAME stock_mode behaviour:
      ADD:     On existing SKU → UPDATE qty = existing_qty + excel_qty
      OPENING: On existing SKU → UPDATE qty = excel_qty  (reset opening stock)
    Metadata fields (prices, dimensions) always updated on existing rows.

    Columns handled (after migration_frames_new_cols.sql):
      Core:    product_name, model, brand, sku_code, qty, mrp, cost_price
      Sizes:   size_a, size_b (BSize), dbl, temple_length
      Look:    colour, colour_mix, temple_colour, shape, base_material, finish
      Business: location (StartCode), frame_group, gender, frame_type, frame_seq
      Pricing:  selling_price
      System:  gst_percent, image_path, is_active
    """
    df = apply_column_map(df, FRAME_COLUMN_MAP)
    if not validate_columns(df, "FRAME", result):
        return

    dup_skus = set(df[df.duplicated("sku_code", keep=False)]["sku_code"].dropna())
    if dup_skus:
        result.add_warning(f"Duplicate SKUs in Excel (will skip): {list(dup_skus)[:5]}")

    if stock_mode == STOCK_MODE_OPENING:
        result.add_warning("⚠️ OPENING mode: Frame qty will be SET (not added) to excel quantity.")

    _frame_inserts: list = []
    _frame_updates: list = []
    for i, row in df.iterrows():
        row_num = i + 2
        result.total_rows += 1

        sku = _safe_str(row.get("sku_code"))
        if not sku:
            result.add_error(row_num, "sku_code", "Missing SKU / Barcode")
            result.skipped += 1
            continue

        if sku in dup_skus:
            result.add_error(row_num, "sku_code", f"Duplicate SKU in Excel: {sku}")
            result.skipped += 1
            continue

        excel_qty = _safe_int(row.get("qty"), 0)

        try:
            existing = run_query("SELECT id, qty FROM frames WHERE sku_code=%s", (sku,))

            if dry_run:
                result.inserted += 1 if not existing else 0
                result.updated  += 1 if existing else 0
                continue

            params = (
                _safe_str(row.get("product_name")),
                _safe_str(row.get("model")),
                _safe_str(row.get("brand")),
                # Dimensions
                _safe_int(row.get("size_a")),
                _safe_int(row.get("size_b")),           # ← NEW BSize
                _safe_int(row.get("dbl")),
                _safe_int(row.get("temple_length")),
                # Look
                _safe_str(row.get("base_material")),
                _safe_str(row.get("finish")),
                _safe_str(row.get("colour")),
                _safe_str(row.get("colour_mix")),       # ← NEW ColourMix
                _safe_str(row.get("temple_colour")),    # ← NEW TempleColour
                _safe_str(row.get("shape")),
                # Business
                _safe_str(row.get("location")),         # ← NEW StartCode → location
                _safe_str(row.get("frame_group")),      # ← NEW FrameGroup
                _safe_str(row.get("gender")),           # ← NEW Gender
                _safe_str(row.get("frame_type")),       # ← NEW FrameType
                _safe_int(row.get("frame_seq")),        # ← NEW FrameSeq (sort order)
                # Pricing
                _safe_money(row.get("cost_price")),
                _safe_money(row.get("selling_price")),  # ← NEW selling_price
                _safe_money(row.get("mrp")),
                _safe_money(row.get("gst_percent")),
                # System
                _safe_str(row.get("image_path")),
                _safe_bool(row.get("is_active"), True),
            )

            if existing:
                old_qty = _safe_int(existing[0].get("qty"), 0)
                new_qty = _compute_new_qty(old_qty, excel_qty, stock_mode)
                _frame_updates.append(params + (new_qty, sku))
                result.updated += 1
            else:
                # INSERT: (id, product_name, model, brand, sku_code, ...rest of params..., qty at position)
                _frame_inserts.append(
                    (str(uuid.uuid4()),) + params[:3] + (sku,) + params[3:18] + (excel_qty,) + params[18:]
                )
                result.inserted += 1

        except Exception as e:
            result.add_error(row_num, "DB", str(e), sku)
            result.skipped += 1

    if not dry_run and (_frame_inserts or _frame_updates):
        _conn = None
        try:
            from psycopg2.extras import execute_batch as _eb
            _conn = get_transaction_connection()
            _cur  = _conn.cursor()
            if _frame_inserts:
                _eb(_cur, """
                    INSERT INTO frames
                    (id, product_name, model, brand, sku_code,
                     size_a, size_b, dbl, temple_length,
                     base_material, finish, colour, colour_mix, temple_colour,
                     shape, location, frame_group, gender, frame_type, frame_seq,
                     qty,
                     cost_price, selling_price, mrp, gst_percent,
                     image_path, is_active, created_at)
                    VALUES(%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s, %s,%s,%s,%s, %s,%s,NOW())
                """, _frame_inserts, page_size=500)
            if _frame_updates:
                _eb(_cur, """
                    UPDATE frames SET
                        product_name=%s, model=%s, brand=%s,
                        size_a=%s, size_b=%s, dbl=%s, temple_length=%s,
                        base_material=%s, finish=%s, colour=%s, colour_mix=%s, temple_colour=%s,
                        shape=%s, location=%s, frame_group=%s, gender=%s, frame_type=%s, frame_seq=%s,
                        cost_price=%s, selling_price=%s, mrp=%s, gst_percent=%s,
                        image_path=%s, is_active=%s,
                        qty=%s, updated_at=NOW()
                    WHERE sku_code=%s
                """, _frame_updates, page_size=500)
            _conn.commit()
        except Exception as _tx_ex:
            if _conn:
                try: _conn.rollback()
                except: pass
            result.add_error(0, "DB-BATCH", f"FRAME batch write failed — all rows rolled back: {_tx_ex}")
            result.inserted = 0; result.updated = 0
        finally:
            if _conn:
                try: close_connection(_conn)
                except: pass

    # ── Extension columns (any extra cols not in core set) ────────────────────
    try:
        from modules.loaders.live_schema_bridge import write_extension_cols
        _FRAME_CORE = {
            "id", "product_name", "model", "brand", "sku_code",
            "size_a", "size_b", "dbl", "temple_length",
            "base_material", "finish", "colour", "colour_mix", "temple_colour",
            "shape", "location", "frame_group", "gender", "frame_type", "frame_seq",
            "qty", "cost_price", "selling_price", "mrp", "gst_percent",
            "image_path", "is_active", "created_at", "updated_at",
        }
        write_extension_cols("frames", df, ["sku_code"], _FRAME_CORE, dry_run)
    except Exception as _ex:
        logger.warning(f"[frames] extension cols skipped: {_ex}")


# ═══════════════════════════════════════════════════════
# PARTY LOADER
# ═══════════════════════════════════════════════════════

def _import_parties(df: pd.DataFrame, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    df = apply_column_map(df, PARTY_COLUMN_MAP)
    if not validate_columns(df, "PARTY", result):
        return

    # ── Excel-level duplicate check ───────────────────────────────────────────
    _seen_mobiles = {}
    _seen_names   = {}
    for _i, _row in df.iterrows():
        _rn  = _i + 2
        _mob = _safe_mobile(_row.get("mobile"))
        _nm  = (_safe_str(_row.get("party_name")) or "").lower().strip()
        if _mob:
            if _mob in _seen_mobiles:
                result.add_warning(
                    f"Row {_rn}: duplicate mobile {_mob} — already in row {_seen_mobiles[_mob]}. "
                    f"Later row will UPDATE the earlier one."
                )
            else:
                _seen_mobiles[_mob] = _rn
        elif _nm:
            if _nm in _seen_names:
                result.add_warning(
                    f"Row {_rn}: duplicate name '{_row.get('party_name')}' (no mobile) — "
                    f"already in row {_seen_names[_nm]}. Later row will UPDATE the earlier one."
                )
            else:
                _seen_names[_nm] = _rn

    # ── BULK LOAD: fetch ALL existing parties in ONE query ────────────────────
    # Replaces N individual SELECT queries with a single fetch.
    # For 4000+ rows this reduces DB round-trips from 8000+ → 3 total.
    try:
        existing_rows = run_query(
            "SELECT id, mobile, LOWER(party_name) AS name_lower FROM parties"
        )
    except Exception as e:
        result.add_error(0, "DB", f"Could not load existing parties: {e}")
        return

    # Build lookup maps in Python — O(1) per row
    mobile_to_id = {}   # mobile_str → party id
    name_to_id   = {}   # lower(name) → party id (only for rows with no mobile)
    for r in existing_rows:
        mob = str(r.get("mobile") or "").strip()
        if mob:
            mobile_to_id[mob] = r["id"]
        else:
            nm = (r.get("name_lower") or "").strip()
            if nm:
                name_to_id[nm] = r["id"]

    # ── Build INSERT and UPDATE batches ───────────────────────────────────────
    inserts = []   # list of param tuples for INSERT
    updates = []   # list of param tuples for UPDATE

    for i, row in df.iterrows():
        row_num = i + 2
        result.total_rows += 1

        name = _safe_str(row.get("party_name"))
        if not name:
            result.add_error(row_num, "party_name", "Empty party name")
            result.skipped += 1
            continue

        mobile     = _safe_mobile(row.get("mobile"))
        party_type = _norm_party_type(row.get("party_type", "Retail"))

        # Match existing record — same logic as before, but against in-memory maps
        existing_id = None
        if mobile:
            existing_id = mobile_to_id.get(str(mobile).strip())
        if existing_id is None:
            existing_id = name_to_id.get(name.lower().strip())

        # ── Similarity check — warn about similar names in dry_run ────────────
        if existing_id is None:  # new party — check for similar names
            try:
                from modules.loaders.party_dedup import find_similar_parties
                similar = find_similar_parties(name, threshold=0.40, limit=3)
                if similar:
                    exact   = [r for r in similar if r['conflict_type'] == 'EXACT']
                    fuzzy   = [r for r in similar if r['conflict_type'] == 'SIMILAR']
                    if exact:
                        result.add_error(row_num, "duplicate_name",
                            f"EXACT MATCH: '{name}' already exists as '{exact[0]['party_name']}' "
                            f"(Customer#: {exact[0].get('customer_no','—')}, "
                            f"Mobile: {exact[0].get('mobile','—')}). "
                            "Row will be skipped unless you resolve this first.")
                    elif fuzzy:
                        names_list = ', '.join(f"'{r['party_name']}'" for r in fuzzy)
                        result.add_warning(row_num,
                            f"SIMILAR NAME: '{name}' resembles {names_list}. "
                            "If this is a different party, add a distinguishing suffix "
                            "(city, owner name, or -2). If same party, remove from import.")
            except Exception:
                pass  # never block import due to similarity check

        if dry_run:
            result.inserted += 1 if existing_id is None else 0
            result.updated  += 1 if existing_id else 0
            continue

        params = (
            name,
            party_type,
            mobile,
            _safe_str(row.get("alt_mobile")),
            _safe_str(row.get("email")),
            _safe_str(row.get("contact_person")),
            _safe_str(row.get("address")),
            _safe_str(row.get("city")),
            _safe_str(row.get("area")),
            _safe_str(row.get("pincode")),
            _safe_str(row.get("state_name")),
            _safe_str(row.get("state_code")),
            _safe_str(row.get("gstin", "")).upper() if row.get("gstin") else None,
            _safe_str(row.get("pan_no", "")).upper() if row.get("pan_no") else None,
            _safe_str(row.get("tan_no", "")).upper() if row.get("tan_no") else None,
            _safe_str(row.get("cin_no", "")).upper() if row.get("cin_no") else None,
            round(float(row.get("gst_rate") or 0), 2),
            round(float(row.get("credit_limit") or 0), 2),
            int(row.get("credit_days") or 0),
            round(float(row.get("opening_balance") or 0), 2),
            _safe_str(row.get("balance_type")) or "Dr",
            _safe_str(row.get("tally_group")),
            _safe_str(row.get("notes")),
            _safe_bool(row.get("is_active"), True),   # BOOLEAN — True/False only
        )

        if existing_id:
            updates.append(params + (existing_id,))
        else:
            inserts.append((str(uuid.uuid4()),) + params)

    if dry_run:
        return

    # ── Execute batches — one transaction each ────────────────────────────────
    # INSERT batch
    if inserts:
        conn = None
        cur  = None
        try:
            conn = get_transaction_connection()   # autocommit=False — needed for execute_batch
            cur  = conn.cursor()
            from psycopg2.extras import execute_batch
            execute_batch(cur, """
                INSERT INTO parties (
                    id, party_name, party_type, mobile, alt_mobile, email,
                    contact_person, address, city, area, pincode,
                    state_name, state_code, gstin, pan_no, tan_no, cin_no, gst_rate,
                    credit_limit, credit_days, opening_balance, balance_type,
                    tally_group, notes, is_active, created_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s, NOW()
                )
                ON CONFLICT (mobile) DO UPDATE SET
                    party_name=EXCLUDED.party_name, party_type=EXCLUDED.party_type,
                    alt_mobile=EXCLUDED.alt_mobile,
                    email=EXCLUDED.email, contact_person=EXCLUDED.contact_person,
                    address=EXCLUDED.address, city=EXCLUDED.city, area=EXCLUDED.area,
                    pincode=EXCLUDED.pincode, state_name=EXCLUDED.state_name,
                    state_code=EXCLUDED.state_code, gstin=EXCLUDED.gstin,
                    pan_no=EXCLUDED.pan_no, tan_no=EXCLUDED.tan_no,
                    cin_no=EXCLUDED.cin_no, gst_rate=EXCLUDED.gst_rate,
                    credit_limit=EXCLUDED.credit_limit, credit_days=EXCLUDED.credit_days,
                    opening_balance=EXCLUDED.opening_balance, balance_type=EXCLUDED.balance_type,
                    tally_group=EXCLUDED.tally_group, notes=EXCLUDED.notes,
                    is_active=EXCLUDED.is_active
            """, inserts, page_size=500)
            conn.commit()
            result.inserted += len(inserts)
        except Exception as e:
            if conn:
                try: conn.rollback()
                except: pass
            result.add_error(0, "DB-INSERT", f"Party batch insert failed: {e}")
        finally:
            try: cur.close()
            except: pass
            try: close_connection(conn)
            except: pass

    # UPDATE batch
    if updates:
        conn = None
        cur  = None
        try:
            conn = get_transaction_connection()   # autocommit=False
            cur  = conn.cursor()
            from psycopg2.extras import execute_batch
            execute_batch(cur, """
                UPDATE parties SET
                    party_name=%s, party_type=%s, mobile=%s,
                    alt_mobile=%s, email=%s, contact_person=%s,
                    address=%s, city=%s, area=%s, pincode=%s,
                    state_name=%s, state_code=%s,
                    gstin=%s, pan_no=%s, tan_no=%s, cin_no=%s, gst_rate=%s,
                    credit_limit=%s, credit_days=%s,
                    opening_balance=%s, balance_type=%s,
                    tally_group=%s, notes=%s, is_active=%s
                WHERE id=%s
            """, updates, page_size=500)
            conn.commit()
            result.updated += len(updates)
        except Exception as e:
            if conn:
                try: conn.rollback()
                except: pass
            result.add_error(0, "DB-UPDATE", f"Party batch update failed: {e}")
        finally:
            try: cur.close()
            except: pass
            try: close_connection(conn)
            except: pass

    # ── Extension columns: write any new DB columns not in the hardcoded SQL ──
    # e.g. billing_preference, payment_mode, print_with_powers, order_cutoff_time
    # These are detected live from the DB schema so no code change needed when
    # a new column is added.
    _PARTY_CORE_COLS = {
        "id", "party_name", "party_type", "mobile", "alt_mobile", "email",
        "contact_person", "address", "city", "area", "pincode",
        "state_name", "state_code", "gstin", "pan_no", "tan_no", "cin_no",
        "gst_rate", "credit_limit", "credit_days", "opening_balance",
        "balance_type", "tally_group", "notes", "is_active", "created_at",
        "updated_at", "status", "barcode", "customer_no",
    }
    try:
        from modules.loaders.live_schema_bridge import (
            get_writable_cols_for_table, coerce_for_write
        )
        ext_cols = get_writable_cols_for_table(
            "PARTY", "parties", list(df.columns), exclude=_PARTY_CORE_COLS
        )
        if ext_cols and not dry_run:
            # Build: UPDATE parties SET col1=%s, col2=%s WHERE mobile=%s OR party_name=%s
            set_clause  = ", ".join(f'"{c.db_column}"=%s' for c in ext_cols)
            ext_updates = []
            for i, row in df.iterrows():
                mob  = _safe_mobile(row.get("mobile"))
                name = _safe_str(row.get("party_name", ""))
                if not mob and not name:
                    continue
                vals = [coerce_for_write(row.get(c.db_column), c) for c in ext_cols]
                if mob:
                    ext_updates.append(vals + [mob, None])
                else:
                    ext_updates.append(vals + [None, name])
            if ext_updates:
                conn = None
                try:
                    conn = get_transaction_connection()
                    cur  = conn.cursor()
                    from psycopg2.extras import execute_batch
                    ext_sql = (
                        f"UPDATE parties SET {set_clause} "
                        "WHERE (mobile=%s AND mobile IS NOT NULL) "
                        "   OR (LOWER(party_name)=LOWER(%s) AND (%s) IS NULL)"
                    )
                    # Flatten: [col_vals..., mob, name, mob_again_for_null_check]
                    ext_rows = [tuple(r[:-2]) + (r[-2], r[-1], r[-2]) for r in ext_updates]
                    execute_batch(cur, ext_sql, ext_rows, page_size=500)
                    conn.commit()
                except Exception as ex:
                    if conn:
                        try: conn.rollback()
                        except: pass
                    logger.warning(f"[party_loader] extension cols update failed: {ex}")
                finally:
                    if conn:
                        try: close_connection(conn)
                        except: pass
    except Exception as ex:
        logger.warning(f"[party_loader] extension cols skipped: {ex}")


# ═══════════════════════════════════════════════════════
# PATIENT LOADER
# ═══════════════════════════════════════════════════════

def _import_patients(df: pd.DataFrame, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    df = apply_column_map(df, PATIENT_COLUMN_MAP)
    if not validate_columns(df, "PATIENT", result):
        return

    _patient_inserts: list = []

    for i, row in df.iterrows():
        row_num = i + 2
        result.total_rows += 1

        mobile   = _safe_mobile(row.get("mobile"))
        record_no = _safe_str(row.get("record_no"))
        name     = _safe_str(row.get("master_name"))

        if not mobile and not record_no:
            result.add_error(row_num, "identity", "Both mobile and record_no are empty — skipped")
            result.skipped += 1
            continue

        try:
            # ── Patient identity resolution — all scenes handled ────────────
            # Uses patient_dedup.resolve_patient() which handles:
            #   - Exact match (name+mobile) → return visit
            #   - Spelling variant (same mobile, similar name) → flag for review
            #   - Family member (same mobile, different name) → new patient + relation
            #   - No-mobile duplicate → auto-suffix (Ramesh Gadhvi-2)
            #   - Truly new → insert normally
            from modules.loaders.patient_dedup import save_patient, resolve_patient

            if dry_run:
                resolution = resolve_patient(name, mobile,
                                             relation=row.get("relation","Self"))
                if resolution["action"] == "found":
                    result.updated += 1
                elif resolution["action"] == "spell":
                    result.add_error(row_num, "identity",
                        f"Possible spelling variant of existing patient — "
                        f"'{resolution['candidates'][0]['master_name']}' on same mobile. "
                        f"Will be created as new patient if confirmed.")
                    result.inserted += 1
                else:
                    result.inserted += 1
                continue

            pid, err = save_patient(
                name    = name,
                mobile  = mobile,
                relation= row.get("relation") or "Self",
                gender  = row.get("gender") or None,
                dob     = row.get("dob") or None,
                ref_mobile = row.get("ref_mobile") or None,
                record_no  = record_no,
            )

            if err == "SPELL_CONFIRM_REQUIRED":
                # Loader auto-proceeds: treat as new patient with mobile as differentiator
                pid, err = save_patient(name, mobile,
                                        relation=row.get("relation","Self"),
                                        record_no=record_no)

            if err:
                result.add_error(row_num, "patient_save", err)
                result.skipped += 1
                continue

            # Check if it was an insert or update
            # (save_patient returns existing id for found patients)
            result.inserted += 1

            # Insert visit record.
            # Multiple visits per patient are intentional (medical history).
            # Guard: skip if exact same (patient_id + record_no + visit_date) already exists
            # — this prevents duplicate rows from re-importing the same Excel file,
            # while allowing genuine new visits on different dates to be added normally.
            _visit_date = _safe_date(row.get("visit_date"))
            _visit_exists = False
            if pid:
                if record_no and _visit_date:
                    # Most precise guard: same record + same date = same visit
                    _vcheck = run_query(
                        "SELECT 1 FROM patient_visits "
                        "WHERE patient_id=%s AND record_no=%s AND visit_date=%s LIMIT 1",
                        (pid, record_no, _visit_date)
                    )
                    _visit_exists = bool(_vcheck)
                elif record_no and not _visit_date:
                    # No date in Excel — guard on record_no alone to avoid blind duplicates
                    _vcheck = run_query(
                        "SELECT 1 FROM patient_visits "
                        "WHERE patient_id=%s AND record_no=%s LIMIT 1",
                        (pid, record_no)
                    )
                    _visit_exists = bool(_vcheck)
                # If no record_no — always insert (can't identify duplicate safely)

            if not _visit_exists:
                _patient_inserts.append((
                    str(uuid.uuid4()), pid, record_no,
                    _visit_date, name,
                    _safe_float(row.get("right_sph")),
                    _safe_float(row.get("right_cyl")),
                    _safe_axis(row.get("right_axis")),
                    _safe_float(row.get("right_add")),
                    _safe_float(row.get("left_sph")),
                    _safe_float(row.get("left_cyl")),
                    _safe_axis(row.get("left_axis")),
                    _safe_float(row.get("left_add")),
                ))

        except Exception as e:
            result.add_error(row_num, "DB", str(e), name)
            result.skipped += 1


    if not dry_run and _patient_inserts:
        _conn = None
        try:
            from psycopg2.extras import execute_batch as _eb
            _conn = get_transaction_connection()
            _cur  = _conn.cursor()
            _eb(_cur, """
                INSERT INTO patient_visits
                (id,patient_id,record_no,visit_date,visit_name,
                 right_sph,right_cyl,right_axis,right_add,
                 left_sph,left_cyl,left_axis,left_add)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, _patient_inserts, page_size=500)
            _conn.commit()
            result.inserted += len(_patient_inserts)
        except Exception as _tx_ex:
            if _conn:
                try: _conn.rollback()
                except: pass
            result.add_error(0,"DB-BATCH",f"PATIENT batch write failed — all rows rolled back: {_tx_ex}")
            result.inserted = 0
        finally:
            if _conn:
                try: close_connection(_conn)
                except: pass

    # ── Extension columns (va_distance_aided_r/l, notes, etc.) ──────────────
    try:
        from modules.loaders.live_schema_bridge import write_extension_cols
        _VISIT_CORE = {
            "id","patient_id","record_no","visit_date","visit_name",
            "right_sph","right_cyl","right_axis","right_add",
            "left_sph","left_cyl","left_axis","left_add","created_at","updated_at",
        }
        write_extension_cols(
            "patient_visits", df,
            ["patient_id","record_no","visit_date"],
            _VISIT_CORE, dry_run
        )
    except Exception as _ex:
        logger.warning(f"[patient_visits] extension cols skipped: {_ex}")


# ═══════════════════════════════════════════════════════
# OPHTHALMIC / CONTACT LENS STOCK LOADER
# (ophlens_batch.xlsx and clens_batch.xlsx — same logic)
# ═══════════════════════════════════════════════════════

def _normalize_name(name: str) -> str:
    """
    Collapse all internal whitespace to single space and strip ends.
    Applied before every product lookup and DB write to prevent
    space-variant duplicates ("Cobalt  ARC" vs "Cobalt ARC").
    Used by all loaders — single definition, used everywhere.
    """
    if not name:
        return ""
    return " ".join(name.split())


def _find_product(product_name: str) -> Optional[Tuple[str, str, str, str, str]]:
    """
    Returns (product_id, lens_category, main_group, supplier_id, supplier_name) or None.
    supplier_id / supplier_name come from products.preferred_supplier_id → parties.
    These are empty strings when not set — never None so callers can safely use [3]/[4].

    Match strategy (in order):
      1. Exact match     — collapse internal spaces both sides
      2. Full-name ILIKE — DB LIKE '%full_name%'
      3. Token ALL-match — ALL tokens (>=2 chars) must match.
                           Min length=2 so short-but-critical tokens like
                           '3p', '6p', '2p' are included — prevents
                           "AirOptix Aqua 3P" matching "AirOptix Aqua 6P".
    """
    if not product_name:
        return None

    clean = _normalize_name(product_name)
    if not clean:
        return None

    # 1 — Exact match, collapse internal spaces on both sides
    rows = run_query(
        "SELECT p.id, p.lens_category, p.main_group, "
        "       COALESCE(p.preferred_supplier_id::text, '') AS supplier_id, "
        "       COALESCE(par.party_name, '') AS supplier_name "
        "FROM products p "
        "LEFT JOIN parties par ON par.id = p.preferred_supplier_id "
        "WHERE LOWER(regexp_replace(p.product_name, '\\s+', ' ', 'g')) "
        "    = LOWER(%s) LIMIT 1",
        (clean,)
    )
    if rows:
        return (rows[0]["id"], rows[0].get("lens_category", ""), rows[0].get("main_group", ""),
                rows[0].get("supplier_id", ""), rows[0].get("supplier_name", ""))

    # 2 — Full name ILIKE
    rows = run_query(
        "SELECT p.id, p.lens_category, p.main_group, p.product_name, "
        "       COALESCE(p.preferred_supplier_id::text, '') AS supplier_id, "
        "       COALESCE(par.party_name, '') AS supplier_name "
        "FROM products p "
        "LEFT JOIN parties par ON par.id = p.preferred_supplier_id "
        "WHERE LOWER(p.product_name) LIKE %s LIMIT 1",
        (f"%{clean.lower()}%",)
    )
    if rows:
        logger.info(f"[PRODUCT-MATCH] Full ILIKE: '{clean}' -> '{rows[0]['product_name']}'")
        return (rows[0]["id"], rows[0].get("lens_category", ""), rows[0].get("main_group", ""),
                rows[0].get("supplier_id", ""), rows[0].get("supplier_name", ""))

    # 3 — Token ALL-match: every token must appear in the product name.
    # Min len=2 keeps short-but-critical differentiators like "3p" vs "6p".
    # Strip brackets so "(three)" becomes "three" for matching.
    tokens = [t for t in re.split(r"[\s()\[\]]+", clean.lower()) if len(t) >= 2]
    if len(tokens) >= 2:
        conditions = " AND ".join(["LOWER(product_name) LIKE %s"] * len(tokens))
        params = tuple(f"%{t}%" for t in tokens)
        rows = run_query(
            f"SELECT p.id, p.lens_category, p.main_group, p.product_name, "
            f"       COALESCE(p.preferred_supplier_id::text, '') AS supplier_id, "
            f"       COALESCE(par.party_name, '') AS supplier_name "
            f"FROM products p "
            f"LEFT JOIN parties par ON par.id = p.preferred_supplier_id "
            f"WHERE {conditions} LIMIT 1",
            params
        )
        if rows:
            logger.info(f"[PRODUCT-MATCH] Token-ALL({len(tokens)}): '{clean}' -> '{rows[0]['product_name']}'")
            return (rows[0]["id"], rows[0].get("lens_category", ""), rows[0].get("main_group", ""),
                    rows[0].get("supplier_id", ""), rows[0].get("supplier_name", ""))

        # Fallback: drop 2-char tokens, require all >=3 char tokens to match
        # Handles minor name differences while staying specific
        tokens_strict = [t for t in tokens if len(t) >= 3]
        if len(tokens_strict) >= 3 and len(tokens_strict) < len(tokens):
            conditions = " AND ".join(["LOWER(product_name) LIKE %s"] * len(tokens_strict))
            params = tuple(f"%{t}%" for t in tokens_strict)
            rows = run_query(
                f"SELECT id, lens_category, main_group, product_name FROM products "
                f"WHERE {conditions} LIMIT 1",
                params
            )
            if rows:
                logger.info(f"[PRODUCT-MATCH] Token-STRICT({len(tokens_strict)}): '{clean}' -> '{rows[0]['product_name']}'")
                return (rows[0]["id"], rows[0].get("lens_category", ""), rows[0].get("main_group", ""),
                        rows[0].get("supplier_id", ""), rows[0].get("supplier_name", ""))

    logger.warning(f"[PRODUCT-MATCH] No match found for: '{clean}'")
    return None
    return None


def _import_ophlens(df: pd.DataFrame, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    """
    OPHTHALMIC LENS loader.
    - Tracked by SPH/CYL/AXIS powers — stock_type always 'POWER'
    - batch_no: optional (present for certain stock lots — stored if provided)
    - expiry_date: NOT used — ophthalmic lenses do not expire
    - item_type: 'STOCK' (physical stock) or 'RX' (made to order) — from Excel or default STOCK
    - Dedup key: product + sph + cyl + axis + add_power + eye_side + item_type + batch_no
    - ADD mode:     duplicate → quantity ADDED
    - OPENING mode: duplicate → quantity SET to excel value
    """
    df = apply_column_map(df, OPHLENS_COLUMN_MAP)
    if not validate_columns(df, "OPHLENS", result):
        return

    # ── Pre-expand qty_right/qty_left/qty_independent into separate rows ──────
    # When the upload uses split-qty style (Bonzer/stock template), each source row
    # becomes up to 3 rows: one for R, one for L, one for B (independent)
    _has_split_cols = any(c in df.columns for c in ["qty_right","qty_left","qty_independent"])
    if _has_split_cols:
        expanded = []
        for _, r in df.iterrows():
            qty_r   = int(r.get("qty_right",   0) or 0)
            qty_l   = int(r.get("qty_left",    0) or 0)
            qty_ind = int(r.get("qty_independent", 0) or 0)
            if qty_r == 0 and qty_l == 0 and qty_ind == 0:
                continue  # skip empty rows
            base = r.to_dict()
            if qty_r   > 0:
                row_r = dict(base); row_r["quantity"] = qty_r;   row_r["eye_side"] = "R"; expanded.append(row_r)
            if qty_l   > 0:
                row_l = dict(base); row_l["quantity"] = qty_l;   row_l["eye_side"] = "L"; expanded.append(row_l)
            if qty_ind > 0:
                row_b = dict(base); row_b["quantity"] = qty_ind; row_b["eye_side"] = "B"; expanded.append(row_b)
        if expanded:
            import pandas as _pd
            df = _pd.DataFrame(expanded).reset_index(drop=True)
    # ─────────────────────────────────────────────────────────────────────────

    if stock_mode == STOCK_MODE_OPENING:
        result.add_warning("OPENING mode: Existing OPHLENS power rows will be OVERWRITTEN with excel quantity.")

    match_cache   = {}
    _oph_inserts: list = []
    _oph_updates: list = []   # (params_tuple, update_sql_key)  key: "qty"|"price_only"

    for i, row in df.iterrows():
        row_num = i + 2
        # Silently skip fully-empty rows — don't count in total_rows (blank trailing lines)
        if row.isna().all():
            continue

        result.total_rows += 1

        pname = _safe_str(row.get("product_name"))
        if not pname:
            result.add_error(row_num, "product_name", "Missing product name")
            result.skipped += 1
            continue

        # Detect item_type early — RX rows don't need qty (price reference only)
        raw_item_type = _safe_str(row.get("item_type", "STOCK")) or "STOCK"
        item_type     = "RX" if raw_item_type.upper() in ("RX", "PRESCRIPTION", "R/X") else "STOCK"

        # Support both single-qty style (quantity col) and split style (qty_right/qty_left)
        _qty_r   = _safe_int(row.get("qty_right"),     0) or 0
        _qty_l   = _safe_int(row.get("qty_left"),      0) or 0
        _qty_ind = _safe_int(row.get("qty_independent"), 0) or 0
        qty      = _safe_int(row.get("quantity"),      0) or 0

        # If split qty columns are used, determine eye_side and quantity from them
        _eye_raw = _safe_str(row.get("eye_side", "B")) or "B"
        _eye     = _eye_raw.upper() if _eye_raw.upper() in VALID_EYE_SIDES else "B"

        _has_split = (_qty_r > 0 or _qty_l > 0 or _qty_ind > 0)
        if _has_split and qty == 0:
            # Convert split rows to (eye_side, quantity) pairs for processing
            _split_rows = []
            if _qty_r   > 0: _split_rows.append(("R", _qty_r))
            if _qty_l   > 0: _split_rows.append(("L", _qty_l))
            if _qty_ind > 0: _split_rows.append(("B", _qty_ind))
        elif qty > 0:
            _split_rows = [(_eye, qty)]
        else:
            if item_type == "STOCK":
                result.skipped += 1
                continue
            _split_rows = [(_eye, 0)]

        # Process each split_row as a separate inventory entry
        _original_row = row.copy()
        for _proc_eye, _proc_qty in _split_rows:
            row = _original_row.copy()
            qty = _proc_qty
            _eye = _proc_eye
        # RX rows: qty=0 is valid — they are made-to-order price references, not stock

        sph  = _safe_float(row.get("sph"))
        cyl  = _safe_float(row.get("cyl"))
        axis = _safe_int(row.get("axis"))
        addp = _safe_float(row.get("add_power"))

        # TORIC validation — CYL present → AXIS required for RX, optional for STOCK
        # Stock lenses (e.g. Bonzer Omega) are dispensed to any axis — AXIS can be blank
        if cyl and cyl != 0 and (axis is None or axis == 0):
            if item_type == "RX":
                result.add_error(row_num, "axis", "TORIC: CYL present but AXIS is missing", cyl)
                result.skipped += 1
                continue
            else:
                # STOCK toric: axis=None is acceptable — will match any axis at dispensing
                axis = None

        # Product lookup (cached)
        if pname not in match_cache:
            match_cache[pname] = _find_product(pname)
        pid_cat = match_cache[pname]
        if not pid_cat:
            result.add_error(row_num, "product_name", f"Product not found: '{pname}' — import product_master.xlsx first, then re-run this file")
            result.skipped += 1
            continue

        pid          = pid_cat[0]
        _sup_id      = pid_cat[3] if pid_cat[3] else None   # preferred supplier from product
        _sup_name    = pid_cat[4] if pid_cat[4] else None
        eye        = _norm_eye(row.get("eye_side"))
        stock_type = "POWER"   # OPHLENS is always POWER — no batch tracking
        # item_type already resolved above (needed for qty bypass logic)

        # lens_design: map plain-English values → DB enum
        # Excel may have: "Single Vision", "Progressive", "KT", "Bifocal"
        # DB only accepts: SPHERICAL, TORIC, MULTIFOCAL
        LENS_MAP = {
            "SINGLE VISION": "SPHERICAL",
            "SV":            "SPHERICAL",
            "KT":            "SPHERICAL",
            "PROGRESSIVE":   "MULTIFOCAL",
            "PAP":           "MULTIFOCAL",
            "BIFOCAL":       "MULTIFOCAL",
        }
        raw_design  = _safe_str(row.get("lens_design"))
        lens_design = raw_design or _detect_lens_design(sph, cyl, axis, addp)
        lens_design = (lens_design or "SPHERICAL").upper()
        lens_design = LENS_MAP.get(lens_design, lens_design)
        if lens_design not in {"SPHERICAL", "TORIC", "MULTIFOCAL"}:
            lens_design = _detect_lens_design(sph, cyl, axis, addp)  # fall back to auto-detect

        try:
            if dry_run:
                result.inserted += 1
                continue

            batch_no_val = _safe_str(row.get("batch_no"))
            existing = run_query("""
                SELECT id, quantity FROM inventory_stock
                WHERE product_id=%s
                AND sph IS NOT DISTINCT FROM %s
                AND cyl IS NOT DISTINCT FROM %s
                AND axis IS NOT DISTINCT FROM %s
                AND add_power IS NOT DISTINCT FROM %s
                AND eye_side=%s
                AND batch_no IS NOT DISTINCT FROM %s
                AND stock_type='POWER'
                AND item_type=%s
                AND is_active=true
            """, (pid, sph, cyl, axis, addp, eye, batch_no_val, item_type))

            if existing:
                old_qty = existing[0]["quantity"] or 0
                new_qty = _compute_new_qty(old_qty, qty, stock_mode)
                if stock_mode == STOCK_MODE_PRICE_ONLY:
                    _oph_updates.append(("price_only", (
                        _safe_money(row.get("mrp")),
                        _safe_money(row.get("purchase_rate")),
                        _safe_money(row.get("selling_price")),
                        existing[0]["id"],
                    )))
                else:
                    _oph_updates.append(("qty", (
                        new_qty,
                        _safe_money(row.get("mrp")),
                        _safe_money(row.get("purchase_rate")),
                        _safe_money(row.get("selling_price")),
                        lens_design, existing[0]["id"],
                    )))
                result.updated += 1
            else:
                _oph_inserts.append((
                    str(uuid.uuid4()), pid,
                    sph, cyl, axis, addp, eye,
                    _safe_str(row.get("batch_no")),
                    _safe_str(row.get("location")),
                    qty,
                    _safe_money(row.get("mrp")),
                    _safe_money(row.get("purchase_rate")),
                    _safe_money(row.get("selling_price")),
                    item_type, lens_design,
                    _sup_id, _sup_name,
                    _safe_str(row.get("barcode"))  or None,
                    _safe_str(row.get("coating"))  or _safe_str(row.get("coating_type")) or None,
                ))
                result.inserted += 1

        except Exception as e:
            result.add_error(row_num, "DB", str(e), pname)
            result.skipped += 1

    if not dry_run and (_oph_inserts or _oph_updates):
        _conn = None
        try:
            from psycopg2.extras import execute_batch as _eb
            _conn = get_transaction_connection()
            _cur  = _conn.cursor()
            if _oph_inserts:
                _eb(_cur, """
                    INSERT INTO inventory_stock
                    (id,product_id,sph,cyl,axis,add_power,eye_side,
                     batch_no,location,quantity,mrp,purchase_rate,selling_price,
                     stock_type,item_type,lens_design,is_active,created_at,updated_at,
                     supplier_id,supplier_name,barcode,coating)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'POWER',%s,%s,true,NOW(),NOW(),%s,%s,%s,%s)
                """, _oph_inserts, page_size=500)
            qty_upd   = [p for k,p in _oph_updates if k == "qty"]
            price_upd = [p for k,p in _oph_updates if k == "price_only"]
            if qty_upd:
                _eb(_cur, """
                    UPDATE inventory_stock SET quantity=%s,
                        mrp=COALESCE(%s,mrp),purchase_rate=COALESCE(%s,purchase_rate),
                        selling_price=COALESCE(%s,selling_price),
                        lens_design=COALESCE(%s,lens_design),updated_at=NOW()
                    WHERE id=%s
                """, qty_upd, page_size=500)
            if price_upd:
                _eb(_cur, """
                    UPDATE inventory_stock SET
                        mrp=COALESCE(%s,mrp),purchase_rate=COALESCE(%s,purchase_rate),
                        selling_price=COALESCE(%s,selling_price),updated_at=NOW()
                    WHERE id=%s
                """, price_upd, page_size=500)
            _conn.commit()
        except Exception as _tx_ex:
            if _conn:
                try: _conn.rollback()
                except: pass
            result.add_error(0,"DB-BATCH",f"OPHLENS batch write failed — all rows rolled back: {_tx_ex}")
            result.inserted = 0; result.updated = 0
        finally:
            if _conn:
                try: close_connection(_conn)
                except: pass


    # ── Extension columns (barcode, item_code, coating, etc.) ───────────────
    try:
        from modules.loaders.live_schema_bridge import write_extension_cols
        _OPHLENS_CORE = {
            "id","product_id","sph","cyl","axis","add_power","eye_side",
            "batch_no","location","quantity","mrp","purchase_rate","selling_price",
            "stock_type","item_type","lens_design","is_active","created_at","updated_at",
            "barcode","coating_type","supplier_id","supplier_name",
        }
        write_extension_cols(
            "inventory_stock", df,
            ["product_id","sph","cyl","axis","eye_side"],
            _OPHLENS_CORE, dry_run
        )
    except Exception as _ex:
        logger.warning(f"[ophlens] extension cols skipped: {_ex}")

def _import_clens(df: pd.DataFrame, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    """
    CONTACT LENS loader.
    - Tracked by batch_no + expiry_date — stock_type always 'BATCH'
    - MUST have batch_no and expiry_date
    - item_type: always 'STOCK' (contact lenses are always physical batch stock)
    - ADD mode:     duplicate batch → quantity ADDED
    - OPENING mode: duplicate batch → quantity SET to excel value
    ⚠️  QUANTITY MUST BE IN PIECES (PCS), NOT BOXES.
         If your product has box_size=3, enter 3 for 1 box, 6 for 2 boxes etc.
    """
    df = apply_column_map(df, CLENS_COLUMN_MAP)
    if not validate_columns(df, "CLENS", result):
        return

    if stock_mode == STOCK_MODE_OPENING:
        result.add_warning("OPENING mode: Existing CLENS batch rows will be OVERWRITTEN with excel quantity.")

    # ── Box-size warning: detect if quantities look like BOX counts ───────────
    # If a product has box_size > 1 and all quantities are small integers (≤20),
    # it is likely the user entered BOX qty instead of PCS qty.
    # Warn clearly so they can fix before confirming.
    try:
        from modules.sql_adapter import run_query as _rq_bs
        _qty_col = next((c for c in ["quantity","qty"] if c in df.columns), None)
        _prod_col = next((c for c in ["product_name","product","productname"] if c in df.columns), None)
        if _qty_col and _prod_col:
            _products_in_file = df[_prod_col].dropna().astype(str).str.strip().unique().tolist()
            if _products_in_file:
                _box_info = _rq_bs(
                    "SELECT product_name, box_size FROM products "
                    "WHERE product_name = ANY(%s) AND box_size > 1",
                    (_products_in_file,)
                ) or []
                for _bi in _box_info:
                    _pname    = _bi["product_name"]
                    _box_size = int(_bi["box_size"] or 1)
                    _prod_rows = df[df[_prod_col].astype(str).str.strip() == _pname]
                    _stock_rows = _prod_rows[_prod_rows.get(_qty_col, 0) > 0] if _qty_col in _prod_rows else _prod_rows
                    if len(_stock_rows) > 0:
                        try:
                            _qtys = _stock_rows[_qty_col].dropna().astype(float)
                            _max_qty = _qtys.max()
                            # If max qty entered is ≤ 20 and box_size > 1, likely entered as BOXES
                            if _max_qty <= 20 and _box_size > 1:
                                result.add_warning(
                                    f"⚠️  QTY CHECK — '{_pname}' has box_size={_box_size}. "
                                    f"Max qty in file = {int(_max_qty)}. "
                                    f"If you entered BOXES, multiply by {_box_size} to get PCS. "
                                    f"DB always stores PCS. Example: 2 boxes = {2*_box_size} PCS."
                                )
                        except Exception:
                            pass
    except Exception:
        pass  # warning is non-blocking — never fail import due to this check

    match_cache    = {}
    _clens_inserts: list           = []
    _clens_catalogue_inserts: list = []
    _clens_updates: list           = []

    for i, row in df.iterrows():
        row_num = i + 2
        # Silently skip fully-empty rows — don't count in total_rows (blank trailing lines)
        if row.isna().all():
            continue

        result.total_rows += 1

        pname = _safe_str(row.get("product_name"))
        if not pname:
            result.add_error(row_num, "product_name", "Missing product name")
            result.skipped += 1
            continue

        # Determine if this is a CATALOGUE row (no physical stock — just power registry)
        # CATALOGUE rows: item_type='CATALOGUE' OR qty=0 with no batch_no
        _item_type_raw = _safe_str(row.get("item_type") or row.get("ItemType") or "")
        _is_catalogue = (_item_type_raw or "").upper() == "CATALOGUE"
        _is_price_row = (_item_type_raw or "").upper() == "PRICE"

        batch_no = _safe_str(row.get("batch_no"))
        if not batch_no and not _is_catalogue and not _is_price_row:
            result.add_error(row_num, "batch_no", "Contact lens import requires BatchNo")
            result.skipped += 1
            continue

        expiry = _safe_date(row.get("expiry_date"))
        if not expiry and not _is_catalogue and not _is_price_row:
            result.add_error(row_num, "expiry_date", "Contact lens import requires ExpiryDate")
            result.skipped += 1
            continue

        # ── Box→PCS conversion (pack_size aware) ──────────────────────────
        # DB stores PCS always. User enters BOXES. Convert using product.pack_size.
        # If qty_boxes column present → use that. Otherwise treat quantity as boxes
        # and convert if pack_size > 1.
        try:
            from modules.cl_pack_utils import boxes_to_pcs, get_box_size
            _pack = get_box_size(str(pid) if pid else "", row.get("product_name",""))
        except Exception:
            _pack = 1

        _qty_boxes_raw = _safe_float(row.get("qty_boxes")) or 0
        _qty_raw       = _safe_float(row.get("quantity")) or 0

        # Also accept qty_display (download sends this for pair-aware display)
        _qty_display_raw = _safe_float(row.get("qty_display")) or 0
        if _qty_display_raw > 0 and _qty_raw == 0 and _qty_boxes_raw == 0:
            _qty_raw = _qty_display_raw

        if _qty_boxes_raw > 0:
            # Explicit boxes column — convert to pcs
            qty = boxes_to_pcs(_qty_boxes_raw, _pack)
        elif _qty_raw > 0 and _pack > 1:
            # Only quantity column — treat as boxes if pack_size > 1
            qty = boxes_to_pcs(_qty_raw, _pack)
        else:
            qty = int(_qty_raw or 0)

        # ── PAIR → PCS conversion ──────────────────────────────────────────
        # DB always stores quantity in PCS. If the product's unit is PAIR,
        # the download shows qty in pairs (qty÷2). On upload we must double it
        # back so the DB stays consistent.
        # unit column comes from download (qty_unit='PAIRS') or from product row.
        _qty_unit = str(row.get("qty_unit") or row.get("unit") or "PCS").strip().upper()
        if _qty_unit in ("PAIR", "PAIRS") and qty > 0:
            qty = qty * 2   # pairs → pcs for DB storage


        # CATALOGUE rows allowed with qty=0 — they are the full power range for ordering
        # PRICE_ONLY mode: qty=0 is valid — only prices need updating, not stock counts
        if stock_mode != STOCK_MODE_PRICE_ONLY and not _is_catalogue and not _is_price_row and (qty is None or qty <= 0):
            result.skipped += 1
            continue
        if stock_mode == STOCK_MODE_PRICE_ONLY:
            qty = qty or 0  # safe default for price-only rows

        sph  = _safe_float(row.get("sph"))
        cyl  = _safe_float(row.get("cyl"))
        axis = _safe_int(row.get("axis"))
        addp = _safe_float(row.get("add_power"))

        # TORIC validation
        if cyl and cyl != 0 and (axis is None or axis == 0):
            result.add_error(row_num, "axis", "TORIC: CYL present but AXIS is missing", cyl)
            result.skipped += 1
            continue

        if pname not in match_cache:
            match_cache[pname] = _find_product(pname)
        pid_cat = match_cache[pname]
        if not pid_cat:
            result.add_error(row_num, "product_name", f"Product not found: '{pname}' — import product_master.xlsx first, then re-run this file")
            result.skipped += 1
            continue

        pid       = pid_cat[0]
        _sup_id   = pid_cat[3] if pid_cat[3] else None
        _sup_name = pid_cat[4] if pid_cat[4] else None
        eye         = _norm_eye(row.get("eye_side"))
        lens_design = _safe_str(row.get("lens_design")) or _detect_lens_design(sph, cyl, axis, addp)
        lens_design = (lens_design or "SPHERICAL").upper()

        # NORMALIZE TO DB ENUM
        LENS_MAP = {
            "SINGLE VISION": "SPHERICAL",
            "SV": "SPHERICAL",
            "PROGRESSIVE": "MULTIFOCAL",
            "BIFOCAL": "MULTIFOCAL",
            "KT": "SPHERICAL",
        }

        lens_design = LENS_MAP.get(lens_design, lens_design)

        # fallback safety
        if lens_design not in {"SPHERICAL", "TORIC", "MULTIFOCAL"}:
            lens_design = "SPHERICAL"

        try:
            if dry_run:
                result.inserted += 1
                continue

            # ✅ DEDUP KEY: product_id + batch_no + sph + cyl + axis + add_power + eye_side
            #
            # WHY all power columns are included:
            #   Contact lens batches can contain multiple powers in the same physical
            #   shipment (e.g. Batch AO2024A01 contains -1.00, -1.50, -2.00 etc.)
            #   Each power is a SEPARATE stock slot — different item, different qty.
            #
            #   Old code matched on batch_no only → if two rows in the same import
            #   had the same batch_no but different SPH, the second row's qty was
            #   added to the first row and the second power was LOST.
            #
            #   Two different products can also share a batch_no (manufacturer reuse) —
            #   adding product_id to the key prevents cross-product contamination.
            # CATALOGUE rows: dedup on power+product only (no batch_no)
            if _is_price_row:
                # PRICE row — retire existing current price first, then insert
                _stock_type_val = 'PRICE'
                _is_price_current = True
                batch_no = None
                expiry   = None
                qty      = 0
            elif _is_catalogue:
                existing = run_query("""
                    SELECT id, quantity FROM inventory_stock
                    WHERE product_id = %s
                    AND   stock_type = 'CATALOGUE'
                    AND   sph         IS NOT DISTINCT FROM %s
                    AND   cyl         IS NOT DISTINCT FROM %s
                    AND   axis        IS NOT DISTINCT FROM %s
                    AND   add_power   IS NOT DISTINCT FROM %s
                    AND   eye_side    = %s
                    AND   is_active   = true
                    ORDER BY created_at ASC
                    LIMIT 1
                """, (pid, sph, cyl, axis, addp, eye))
            else:
                existing = run_query("""
                    SELECT id, quantity FROM inventory_stock
                    WHERE product_id = %s
                    AND   batch_no   = %s
                    AND   stock_type = 'BATCH'
                    AND   sph         IS NOT DISTINCT FROM %s
                    AND   cyl         IS NOT DISTINCT FROM %s
                    AND   axis        IS NOT DISTINCT FROM %s
                    AND   add_power   IS NOT DISTINCT FROM %s
                    AND   eye_side    = %s
                    AND   is_active   = true
                    ORDER BY created_at ASC
                    LIMIT 1
                """, (pid, batch_no, sph, cyl, axis, addp, eye))

            if existing:
                old_qty = existing[0]["quantity"] or 0
                new_qty = _compute_new_qty(old_qty, qty, stock_mode)

                if stock_mode == STOCK_MODE_PRICE_ONLY:
                    _clens_updates.append(("price_only", (
                        _safe_money(row.get("mrp")),
                        _safe_money(row.get("purchase_rate")),
                        _safe_money(row.get("selling_price")),
                        existing[0]["id"],
                    )))
                else:
                    _clens_updates.append(("qty", (
                        new_qty,
                        _safe_money(row.get("mrp")),
                        _safe_money(row.get("purchase_rate")),
                        _safe_money(row.get("selling_price")),
                        expiry, lens_design, existing[0]["id"],
                    )))
                result.updated += 1
            else:
                if _is_catalogue:
                    _clens_catalogue_inserts.append((
                        str(uuid.uuid4()), pid,
                        sph, cyl, axis, addp, eye,
                        None, None,          # batch_no, expiry_date = NULL for catalogue
                        _safe_str(row.get("location")),
                        0,                   # qty = 0 for catalogue
                        _safe_money(row.get("mrp")),
                        _safe_money(row.get("purchase_rate")),
                        _safe_money(row.get("selling_price")),
                        lens_design,
                        _sup_id, _sup_name,
                    ))
                else:
                    _clens_inserts.append((
                        str(uuid.uuid4()), pid,
                        sph, cyl, axis, addp, eye,
                        batch_no, expiry,
                        _safe_str(row.get("location")),
                        qty,
                        _safe_money(row.get("mrp")),
                        _safe_money(row.get("purchase_rate")),
                        _safe_money(row.get("selling_price")),
                        lens_design,
                        _sup_id, _sup_name,
                        _safe_str(row.get("company_product_name") or row.get("CompanyProductName") or None),
                        _safe_str(row.get("alcon_item_name") or row.get("AlconItemName")),
                        _safe_str(row.get("material_code") or row.get("MaterialCode")),
                    ))
                result.inserted += 1

        except Exception as e:
            result.add_error(row_num, "DB", str(e), pname)
            result.skipped += 1

    if not dry_run and (_clens_inserts or _clens_updates):
        _conn = None
        try:
            from psycopg2.extras import execute_batch as _eb
            _conn = get_transaction_connection()
            _cur  = _conn.cursor()
            if _clens_inserts:
                _eb(_cur, """
                    INSERT INTO inventory_stock
                    (id,product_id,sph,cyl,axis,add_power,eye_side,
                     batch_no,expiry_date,location,quantity,mrp,
                     purchase_rate,selling_price,stock_type,item_type,
                     lens_design,is_active,created_at,updated_at,
                     supplier_id,supplier_name,company_product_name,alcon_item_name,material_code)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'BATCH','STOCK',%s,true,NOW(),NOW(),%s,%s,%s,%s,%s)
                """, _clens_inserts, page_size=500)
            if _clens_catalogue_inserts:
                _eb(_cur, """
                    INSERT INTO inventory_stock
                    (id,product_id,sph,cyl,axis,add_power,eye_side,
                     batch_no,expiry_date,location,quantity,mrp,
                     purchase_rate,selling_price,stock_type,item_type,
                     lens_design,is_active,created_at,updated_at,
                     supplier_id,supplier_name)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CATALOGUE','STOCK',%s,true,NOW(),NOW(),%s,%s)
                """, _clens_catalogue_inserts, page_size=500)
            qty_upd   = [p for k,p in _clens_updates if k == "qty"]
            price_upd = [p for k,p in _clens_updates if k == "price_only"]
            if qty_upd:
                _eb(_cur, """
                    UPDATE inventory_stock SET quantity=%s,
                        mrp=COALESCE(%s,mrp),purchase_rate=COALESCE(%s,purchase_rate),
                        selling_price=COALESCE(%s,selling_price),
                        expiry_date=COALESCE(%s,expiry_date),
                        lens_design=COALESCE(%s,lens_design),updated_at=NOW()
                    WHERE id=%s
                """, qty_upd, page_size=500)
            if price_upd:
                _eb(_cur, """
                    UPDATE inventory_stock SET
                        mrp=COALESCE(%s,mrp),purchase_rate=COALESCE(%s,purchase_rate),
                        selling_price=COALESCE(%s,selling_price),updated_at=NOW()
                    WHERE id=%s
                """, price_upd, page_size=500)
            _conn.commit()
        except Exception as _tx_ex:
            if _conn:
                try: _conn.rollback()
                except: pass
            result.add_error(0,"DB-BATCH",f"CLENS batch write failed — all rows rolled back: {_tx_ex}")
            result.inserted = 0; result.updated = 0
        finally:
            if _conn:
                try: close_connection(_conn)
                except: pass


    # ── Extension columns (barcode, item_code, coating, etc.) ───────────────
    try:
        from modules.loaders.live_schema_bridge import write_extension_cols
        _CLENS_CORE = {
            "id","product_id","sph","cyl","axis","add_power","eye_side",
            "batch_no","expiry_date","location","quantity","mrp","purchase_rate",
            "selling_price","stock_type","item_type","lens_design","is_active",
            "created_at","updated_at",
        }
        write_extension_cols(
            "inventory_stock", df,
            ["product_id","batch_no","sph","cyl","axis"],
            _CLENS_CORE, dry_run
        )
    except Exception as _ex:
        logger.warning(f"[clens] extension cols skipped: {_ex}")


# ═══════════════════════════════════════════════════════
# SOLUTION / SIMPLE BATCH LOADER
# ═══════════════════════════════════════════════════════

def _import_sol(df: pd.DataFrame, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    df = apply_column_map(df, SOL_COLUMN_MAP)
    if not validate_columns(df, "SOL", result):
        return

    for i, row in df.iterrows():
        row_num = i + 2
        # Silently skip fully-empty rows — don't count in total_rows (blank trailing lines)
        if row.isna().all():
            continue

        result.total_rows += 1

        pname = _safe_str(row.get("product_name"))
        if not pname:
            result.add_error(row_num, "product_name", "Empty product name")
            result.skipped += 1
            continue

        qty = _safe_int(row.get("quantity"), 0)

        try:
            pid_rows = run_query(
                "SELECT p.id, "
                "       COALESCE(p.preferred_supplier_id::text, '') AS supplier_id, "
                "       COALESCE(par.party_name, '') AS supplier_name "
                "FROM products p "
                "LEFT JOIN parties par ON par.id = p.preferred_supplier_id "
                "WHERE LOWER(TRIM(p.product_name))=LOWER(TRIM(%s))", (pname,)
            )
            if not pid_rows:
                result.add_error(row_num, "product_name", f"Product not found: '{pname}' — import product_master.xlsx first, then re-run this file")
                result.skipped += 1
                continue

            pid       = pid_rows[0]["id"]
            _sup_id   = pid_rows[0].get("supplier_id") or None
            _sup_name = pid_rows[0].get("supplier_name") or None

            if dry_run:
                result.inserted += 1
                continue

            _batch_no  = _safe_str(row.get("batch_no")) or "DEFAULT"
            _exp_date  = _safe_date(row.get("expiry_date"))
            _cost      = _safe_money(row.get("cost_price"))
            _sell      = _safe_money(row.get("selling_price"))
            _mrp       = _safe_money(row.get("mrp"))

            # Check if batch already exists (no unique constraint on product_id+batch_no)
            _existing_batch = run_query(
                "SELECT id FROM batches WHERE product_id=%s AND batch_no=%s LIMIT 1",
                (pid, _batch_no)
            )
            if _existing_batch:
                _sol_updates.append((_exp_date, qty, _cost, _sell, _mrp, pid, _batch_no))
                result.updated += 1
            else:
                _sol_inserts.append((
                    str(uuid.uuid4()), pid, _batch_no,
                    _exp_date, qty, _cost, _sell, _mrp,
                    _sup_id, _sup_name,
                ))
                result.inserted += 1

        except Exception as e:
            result.add_error(row_num, "DB", str(e), pname)
            result.skipped += 1

    if not dry_run and (_sol_inserts or _sol_updates):
        _conn = None
        try:
            from psycopg2.extras import execute_batch as _eb
            _conn = get_transaction_connection()
            _cur  = _conn.cursor()
            if _sol_inserts:
                _eb(_cur, """
                    INSERT INTO batches
                    (id, product_id, batch_no, expiry_date, qty_available,
                     cost_price, selling_price, mrp, is_active, created_at,
                     supplier_id, supplier_name)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true,NOW(),%s,%s)
                """, _sol_inserts, page_size=500)
            if _sol_updates:
                _eb(_cur, """
                    UPDATE batches SET
                        expiry_date=%s, qty_available=%s, cost_price=%s,
                        selling_price=%s, mrp=%s, is_active=true
                    WHERE product_id=%s AND batch_no=%s
                """, _sol_updates, page_size=500)
            _conn.commit()
        except Exception as _tx_ex:
            if _conn:
                try: _conn.rollback()
                except: pass
            result.add_error(0, "DB-BATCH", f"SOL batch write failed — all rows rolled back: {_tx_ex}")
            result.inserted = 0; result.updated = 0
        finally:
            if _conn:
                try: close_connection(_conn)
                except: pass


    # ── Extension columns ──────────────────────────────────────────────────────
    try:
        from modules.loaders.live_schema_bridge import write_extension_cols
        _SOL_CORE = {
            "id","product_id","batch_no","expiry_date","qty_available",
            "cost_price","selling_price","mrp","is_active","created_at",
        }
        write_extension_cols("batches", df, ["product_id","batch_no"], _SOL_CORE, dry_run)
    except Exception as _ex:
        logger.warning(f"[sol] extension cols skipped: {_ex}")


# ═══════════════════════════════════════════════════════
# BLANK INVENTORY LOADER
# ═══════════════════════════════════════════════════════

def _import_blanks(df: pd.DataFrame, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    """
    BLANK stock_mode behaviour:
      ADD:     On conflict → quantities ADDED (original behaviour)
      OPENING: On conflict → quantities SET to excel values
    Non-quantity fields (base curves etc.) always updated.
    """
    df = apply_column_map(df, BLANK_COLUMN_MAP)
    if not validate_columns(df, "BLANK", result):
        return

    if stock_mode == STOCK_MODE_OPENING:
        result.add_warning("⚠️ OPENING mode: Blank inventory quantities will be SET (not added).")

    _blank_add: list = []
    _blank_opening: list = []
    for i, row in df.iterrows():
        row_num = i + 2
        # Silently skip fully-empty rows — don't count in total_rows (blank trailing lines)
        if row.isna().all():
            continue

        result.total_rows += 1

        brand    = _safe_str(row.get("brand"))
        category = _safe_str(row.get("category"))
        material = _safe_str(row.get("material"))
        add_p    = _safe_float(row.get("add_power"))

        if not all([brand, category, material]):
            result.add_error(row_num, "required", "Missing brand/category/material")
            result.skipped += 1
            continue

        if add_p and abs(add_p) > 99.99:
            result.add_error(row_num, "add_power", f"Invalid ADD value: {add_p}")
            result.skipped += 1
            continue

        excel_qty_right = max(0, _safe_int(row.get("qty_right"), 0) or 0)
        excel_qty_left  = max(0, _safe_int(row.get("qty_left"),  0) or 0)
        excel_qty_ind   = max(0, _safe_int(row.get("qty_independent"), 0) or 0)
        colour          = _safe_str(row.get("colour"))

        try:
            if dry_run:
                result.inserted += 1
                continue
            row_params = (
                brand, category, material, colour, add_p,
                excel_qty_right, excel_qty_left, excel_qty_ind,
                _safe_float(row.get("base_recommended")),
                _safe_float(row.get("base_1")),
                _safe_float(row.get("base_2")),
                _safe_float(row.get("base_3")),
                _safe_float(row.get("cost_price"))         or None,
                _safe_int(row.get("min_stock"), None)      or None,
                _safe_str(row.get("company_billing_name")) or None,
            )
            if stock_mode == STOCK_MODE_OPENING:
                _blank_opening.append(row_params)
            else:
                _blank_add.append(row_params)
            result.inserted += 1

        except Exception as e:
            result.add_error(row_num, "DB", str(e), brand)
            result.skipped += 1

    if not dry_run and (_blank_add or _blank_opening):
        _conn = None
        try:
            from psycopg2.extras import execute_batch as _eb
            _conn = get_transaction_connection()
            _cur  = _conn.cursor()
            if _blank_opening:
                _eb(_cur, """
                    INSERT INTO blank_inventory
                    (brand,category,material,colour,add_power,
                     qty_right,qty_left,qty_independent,
                     base_recommended,base_1,base_2,base_3,
                     cost_price,min_stock,company_billing_name,created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'LOADER')
                    ON CONFLICT(brand,category,material,colour,add_power)
                    DO UPDATE SET
                        qty_right=EXCLUDED.qty_right,
                        qty_left=EXCLUDED.qty_left,
                        qty_independent=EXCLUDED.qty_independent,
                        base_recommended=EXCLUDED.base_recommended,
                        base_1=EXCLUDED.base_1, base_2=EXCLUDED.base_2,
                        base_3=EXCLUDED.base_3,
                        cost_price=COALESCE(EXCLUDED.cost_price, blank_inventory.cost_price),
                        min_stock=COALESCE(EXCLUDED.min_stock,  blank_inventory.min_stock),
                        company_billing_name=COALESCE(EXCLUDED.company_billing_name,
                                                      blank_inventory.company_billing_name),
                        updated_at=NOW()
                """, _blank_opening, page_size=500)
            if _blank_add:
                _eb(_cur, """
                    INSERT INTO blank_inventory
                    (brand,category,material,colour,add_power,
                     qty_right,qty_left,qty_independent,
                     base_recommended,base_1,base_2,base_3,
                     cost_price,min_stock,company_billing_name,created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'LOADER')
                    ON CONFLICT(brand,category,material,colour,add_power)
                    DO UPDATE SET
                        qty_right=blank_inventory.qty_right+EXCLUDED.qty_right,
                        qty_left=blank_inventory.qty_left+EXCLUDED.qty_left,
                        qty_independent=blank_inventory.qty_independent+EXCLUDED.qty_independent,
                        base_recommended=EXCLUDED.base_recommended,
                        base_1=EXCLUDED.base_1, base_2=EXCLUDED.base_2,
                        base_3=EXCLUDED.base_3,
                        cost_price=COALESCE(EXCLUDED.cost_price, blank_inventory.cost_price),
                        min_stock=COALESCE(EXCLUDED.min_stock,  blank_inventory.min_stock),
                        company_billing_name=COALESCE(EXCLUDED.company_billing_name,
                                                      blank_inventory.company_billing_name),
                        updated_at=NOW()
                """, _blank_add, page_size=500)
            _conn.commit()

            # ── Alias insert: store company_billing_name → blank_supplier_alias ──────
            # Runs in a separate transaction so alias failures don't roll back stock
            _alias_rows = [
                (r[0],  # brand
                 _safe_str(df.iloc[i].get("company_billing_name"))
                 )
                for i, r in enumerate(_blank_add + _blank_opening)
                if _safe_str(df.iloc[i].get("company_billing_name") if i < len(df) else None)
            ]
            if _alias_rows:
                try:
                    _alias_conn = get_transaction_connection()
                    _alias_cur  = _alias_conn.cursor()
                    _eb(_alias_cur, """
                        INSERT INTO blank_supplier_alias
                            (brand, supplier_name, our_name, blank_inventory_id)
                        SELECT
                            %s AS brand,
                            %s AS supplier_name,
                            CONCAT_WS(' ',b.brand,b.category,b.material,
                                      b.colour,b.add_power::text) AS our_name,
                            b.id AS blank_inventory_id
                        FROM blank_inventory b
                        WHERE b.brand=%s
                        LIMIT 1
                        ON CONFLICT (brand, supplier_name)
                        DO UPDATE SET
                            supplier_name = EXCLUDED.supplier_name,
                            updated_at    = NOW()
                    """, [(a[0], a[1], a[0]) for a in _alias_rows], page_size=200)
                    _alias_conn.commit()
                    result.add_warning(f"ℹ️ {len(_alias_rows)} supplier alias(es) saved for OCR matching.")
                except Exception as _alias_ex:
                    result.add_warning(f"⚠️ Alias save failed (non-critical): {_alias_ex}")
                finally:
                    try: close_connection(_alias_conn)
                    except: pass

        except Exception as _tx_ex:
            if _conn:
                try: _conn.rollback()
                except: pass
            result.add_error(0,"DB-BATCH",f"BLANK batch write failed — all rows rolled back: {_tx_ex}")
            result.inserted = 0
        finally:
            if _conn:
                try: close_connection(_conn)
                except: pass


    # ── Extension columns (barcode, item_code, min_stock, cost_price, etc.) ──
    try:
        from modules.loaders.live_schema_bridge import write_extension_cols
        _BLANK_CORE = {
            "brand","category","material","colour","add_power",
            "qty_right","qty_left","qty_independent",
            "base_recommended","base_1","base_2","base_3",
            "created_by","created_at","updated_at","id",
        }
        write_extension_cols(
            "blank_inventory", df,
            ["brand","category","material","colour","add_power"],
            _BLANK_CORE, dry_run
        )
    except Exception as _ex:
        logger.warning(f"[blanks] extension cols skipped: {_ex}")


# ═══════════════════════════════════════════════════════
# MASTER DISPATCH
# ═══════════════════════════════════════════════════════

def _resolve_product_id(product_name: str) -> Optional[str]:
    """Resolve product name to UUID. Returns product_id str or None."""
    result = _find_product(product_name)
    if result:
        return result[0]  # product_id is index 0
    return None

def _import_price(df: pd.DataFrame, result: LoadResult, dry_run: bool,
                   env_tag: str, stock_mode: str):
    """
    PRICE loader — creates/updates stock_type=PRICE rows in inventory_stock.
    One price row per product. Auto-versions when purchase_rate changes.
    Columns: Product, MRP, SellingPrice, PurchaseRate, EffectiveFrom, PriceSource, Notes
    """
    df = apply_column_map(df, {
        "product":       "product_name",
        "productname":   "product_name",
        "mrp":           "mrp",
        "sellingprice":  "selling_price",
        "selling_price": "selling_price",
        "purchaserate":  "purchase_rate",
        "purchase_rate": "purchase_rate",
        "effectivefrom": "effective_from",
        "pricesource":   "price_source",
        "notes":         "notes",
    })

    if "product_name" not in df.columns:
        result.add_error(0, "product_name", "Required column missing: Product")
        return

    match_cache = {}

    for i, row in df.iterrows():
        row_num = i + 2
        result.total_rows += 1
        name = _safe_str(row.get("product_name"))
        if not name:
            result.skipped += 1; continue

        # Resolve product_id
        if name.lower() not in match_cache:
            match_cache[name.lower()] = _resolve_product_id(name)
        pid = match_cache[name.lower()]
        if not pid:
            result.add_error(row_num, "product_name",
                f"Product not found: '{name}'"); result.skipped += 1; continue

        mrp   = round(float(row.get("mrp") or 0), 2)
        sp    = round(float(row.get("selling_price") or 0), 2)
        pr    = round(float(row.get("purchase_rate") or 0), 2)
        from datetime import date as _date
        eff   = _safe_str(row.get("effective_from")) or str(_date.today())
        src   = _safe_str(row.get("price_source")) or "PRICE_LIST"

        if mrp <= 0:
            result.add_error(row_num, "mrp", f"MRP must be > 0 for '{name}'")
            result.skipped += 1; continue

        if dry_run:
            result.inserted += 1; continue

        try:
            conn = get_transaction_connection()
            cur  = conn.cursor()
            # Archive old current price if purchase_rate changed
            cur.execute("""
                UPDATE inventory_stock
                SET is_price_current = FALSE, updated_at = NOW()
                WHERE product_id = %s::uuid
                  AND stock_type = 'PRICE'
                  AND is_price_current = TRUE
                  AND ABS(COALESCE(purchase_rate,0) - %s) > 0.01
            """, (pid, pr))
            archived = cur.rowcount

            # Upsert current price row
            cur.execute("""
                INSERT INTO inventory_stock (
                    id, product_id, stock_type, item_type,
                    mrp, selling_price, purchase_rate,
                    effective_from, price_source, is_price_current,
                    quantity, is_active, created_at, updated_at
                ) VALUES (
                    gen_random_uuid(), %s::uuid, 'PRICE', 'STOCK',
                    %s, %s, %s, %s::date, %s, TRUE, 0, TRUE, NOW(), NOW()
                )
                ON CONFLICT DO NOTHING
            """, (pid, mrp, sp, pr, eff, src))

            if cur.rowcount == 0:
                # Row exists (same purchase_rate) — update prices
                cur.execute("""
                    UPDATE inventory_stock
                    SET mrp=%(mrp)s, selling_price=%(sp)s,
                        effective_from=%(eff)s::date,
                        price_source=%(src)s, updated_at=NOW()
                    WHERE product_id=%(pid)s::uuid
                      AND stock_type='PRICE' AND is_price_current=TRUE
                """, {"mrp":mrp,"sp":sp,"eff":eff,"src":src,"pid":pid})
                result.updated += 1
            else:
                result.inserted += 1

            conn.commit()
        except Exception as e:
            if conn:
                try: conn.rollback()
                except: pass
            result.add_error(row_num, "DB", f"Price insert failed: {e}")
        finally:
            if conn:
                try: close_connection(conn)
                except: pass



# ═══════════════════════════════════════════════════════
# OPHTHALMIC SPEC LOADER
# Reads: Brand | Product | LensCategory | Index | Coating
#        | Treatment | WLP_per_pair | SRP_per_pair | PurchaseRate
# Writes: ophthalmic_lens_specs table
# ═══════════════════════════════════════════════════════

OPH_SPEC_COLUMN_MAP = {
    "brand":          "brand",
    "product":        "product_name",
    "productname":    "product_name",
    "lenscategory":   "lens_category",
    "category":       "lens_category",
    "index":          "index_value",
    "indexvalue":     "index_value",
    "lensindex":      "index_value",
    "coating":        "coating",
    "coatingname":    "coating",
    "treatment":      "treatment",
    "wlp":            "wlp_per_pair",
    "wlpperpair":     "wlp_per_pair",
    "wlppair":        "wlp_per_pair",
    "wholesaleprice": "wlp_per_pair",
    "dp":             "wlp_per_pair",
    "dealerprice":    "wlp_per_pair",
    "wsp":            "wlp_per_pair",
    "srp":            "srp_per_pair",
    "srpperpair":     "srp_per_pair",
    "mrp":            "srp_per_pair",
    "retailprice":    "srp_per_pair",
    "purchaserate":   "purchase_rate",
    "purchase":       "purchase_rate",
    "cost":           "purchase_rate",
    "cp":             "purchase_rate",
}


def _normalise_index(raw: str) -> list[float]:
    """
    Parse raw index string into list of float values.

    Handles:
      "1.60"          → [1.60]
      "1.59/1.60"     → [1.59, 1.60]   # combined — create both rows
      "1.5"           → [1.50]          # short notation
      "1.6*"          → [1.60]          # asterisk variant
      "1.50 SV"       → [1.50]          # strip suffix
      "1.50 (FSV)"    → [1.50]
      "1.5 (Photo Grey)" → [1.50]       # treatment handled separately
    
    Auto-mirror rule:
      1.59 present → also add 1.60  (same lens, two notations)
      1.60 present → also add 1.59
    """
    raw = str(raw or '').strip()
    # Remove suffixes: *, SV, (FSV), (Photo), (Extra Grey), etc.
    raw_clean = re.sub(r'[*]|\s*\(.*?\)|\s+(SV|RX|FSV|XR.*)', '', raw, flags=re.IGNORECASE).strip()

    # Split on "/" or "," for combined entries like "1.59/1.60"
    parts = re.split(r'[/,]', raw_clean)
    indices = []
    for p in parts:
        p = p.strip()
        m = re.search(r'(\d+[.]\d+)', p)
        if m:
            try:
                val = round(float(m.group(1)), 2)
                indices.append(val)
            except ValueError:
                pass

    # Auto-mirror ONLY when explicitly written as "1.59/1.60" in same cell
    # (both appear in split parts above — no implicit mirroring)
    # If a brand sells them as separate products (like Zeiss), keep them separate.

    return list(dict.fromkeys(indices))  # deduplicate, preserve order


def _import_ophthalmic_specs(df, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    """
    Load ophthalmic lens specs into ophthalmic_lens_specs table.
    Looks up product_id from products table by (product_name, brand).
    Upserts on (product_id, index_value, coating, treatment).
    Handles combined index like "1.59/1.60" → creates 2 rows automatically.
    """
    df = apply_column_map(df, OPH_SPEC_COLUMN_MAP)

    required = ["product", "index_value", "coating"]
    for col in required:
        if col not in df.columns:
            result.add_error(0, col, f"Required column missing: {col}")
            result.finish()
            return

    inserts = []; updates = []; skipped = 0

    for row_num, row in enumerate(df.to_dict("records"), start=2):
        product_name = _safe_str(row.get("product"))
        brand        = _safe_str(row.get("brand"))
        lens_cat     = _safe_str(row.get("lens_category")) or "SV RX"
        index_val    = _safe_str(row.get("index_value"))
        coating      = _safe_str(row.get("coating"))
        treatment    = _safe_str(row.get("treatment")) or "Clear"

        # Skip rows that are add-ons (Transitions, Sensity etc.) — not spec rows
        # These belong in ophthalmic_addons, not ophthalmic_lens_specs
        _ADDON_KEYWORDS = ["transitions", "sensity", "photochromic", "xtractive",
                           "gen s", "gen x", "varia", "photofusion"]
        _coat_lower = (coating or "").lower()
        _notes_lower = str(row.get("notes") or "").lower()
        _is_addon_row = (
            any(kw in _coat_lower for kw in _ADDON_KEYWORDS)
            or "add-on" in _notes_lower
            or "add on" in _notes_lower
        )
        if _is_addon_row:
            skipped += 1
            continue  # skip — these are add-ons, not lens+coating specs
        wlp          = _safe_money(row.get("wlp_per_pair"))
        srp          = _safe_money(row.get("srp_per_pair"))
        purchase     = _safe_money(row.get("purchase_rate"))

        if not product_name or not index_val or not coating:
            result.add_warning(f"Row {row_num}: missing product/index/coating — skipped")
            skipped += 1
            continue

        if wlp is None and srp is None:
            skipped += 1
            continue

        # Parse index — handles "1.59/1.60", "1.6*", "1.5 (FSV)" etc.
        index_floats = _normalise_index(index_val)
        if not index_floats:
            result.add_warning(f"Row {row_num}: cannot parse index '{index_val}' — skipped")
            skipped += 1
            continue

        # One row in Excel may create 2 spec rows (e.g. 1.59/1.60)
        for index_float in index_floats:
            inserts.append((
                product_name, brand, lens_cat, index_float,
                coating, treatment, wlp, srp, purchase, row_num
            ))

    if not inserts:
        result.add_warning("No valid rows to insert")
        result.finish()
        return

    result.total_rows = len(inserts) + skipped

    if dry_run:
        result.add_warning(f"DRY RUN: {len(inserts)} spec rows would be loaded")
        return

    # ── Batch upsert ──────────────────────────────────────────────────────────
    ok = 0; err = 0
    _conn = None
    try:
        from psycopg2.extras import execute_batch as _eb
        _conn = get_transaction_connection()
        cur   = _conn.cursor()
        for product_name, brand, lens_cat, index_float, coating, treatment, wlp, srp, purchase, row_num in inserts:
            try:
                # Resolve product_id — exact match first, then ILIKE fuzzy
                cur.execute("""
                    SELECT id FROM products
                    WHERE LOWER(product_name) = LOWER(%s)
                      AND (%s IS NULL OR LOWER(brand) = LOWER(%s))
                      AND main_group = 'Ophthalmic Lenses'
                      AND is_active = TRUE
                    LIMIT 1
                """, (product_name, brand, brand))
                pid_row = cur.fetchone()

                # Fuzzy fallback: ILIKE with wildcard
                if not pid_row:
                    cur.execute("""
                        SELECT id FROM products
                        WHERE LOWER(product_name) LIKE LOWER(%s)
                          AND (%s IS NULL OR LOWER(brand) = LOWER(%s))
                          AND main_group = 'Ophthalmic Lenses'
                          AND is_active = TRUE
                        LIMIT 1
                    """, (f"%{product_name}%", brand, brand))
                    pid_row = cur.fetchone()

                if not pid_row:
                    result.add_error(row_num, "product",
                        f"Product '{product_name}' (brand: {brand}) not found in Ophthalmic Lenses. "
                        f"Upload PRODUCT_OPHTHALMIC.xlsx first, or check spelling.",
                        product_name)
                    err += 1
                    continue

                product_id = pid_row[0]

                cur.execute("""
                    INSERT INTO ophthalmic_lens_specs
                        (product_id, brand, lens_category, index_value,
                         coating, treatment, wlp_per_pair, srp_per_pair,
                         purchase_rate, is_active, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,NOW())
                    ON CONFLICT (product_id, index_value, coating, treatment)
                    DO UPDATE SET
                        brand         = EXCLUDED.brand,
                        lens_category = EXCLUDED.lens_category,
                        wlp_per_pair  = COALESCE(EXCLUDED.wlp_per_pair, ophthalmic_lens_specs.wlp_per_pair),
                        srp_per_pair  = COALESCE(EXCLUDED.srp_per_pair, ophthalmic_lens_specs.srp_per_pair),
                        purchase_rate = COALESCE(EXCLUDED.purchase_rate, ophthalmic_lens_specs.purchase_rate),
                        is_active     = TRUE,
                        updated_at    = NOW()
                """, (
                    str(product_id), brand, lens_cat, index_float,
                    coating, treatment, wlp, srp, purchase
                ))
                # rowcount=1 means INSERT; rowcount=-1 means UPDATE (ON CONFLICT)
                if cur.rowcount == 1:
                    ok += 1
                else:
                    result.updated += 1

            except Exception as row_err:
                result.add_error(row_num, "DB", f"Spec write failed: {row_err}", product_name)
                err += 1

        _conn.commit()
        result.inserted = ok
        result.updated  = 0
        if skipped: result.add_warning(f"{skipped} rows skipped (no price / invalid index)")
        if err:     result.add_warning(f"{err} rows failed — check errors above")

    except Exception as e:
        if _conn:
            try: _conn.rollback()
            except: pass
        result.add_error(0, "DB-BATCH", f"Ophthalmic spec batch failed: {e}")
    finally:
        try: cur.close()
        except: pass
        close_connection(_conn)


# ═══════════════════════════════════════════════════════
# OPHTHALMIC ADD-ON LOADER
# Reads OPHTHALMIC_ADDONS.xlsx → ophthalmic_addons table
# ═══════════════════════════════════════════════════════

OPH_ADDON_COLUMN_MAP = {
    "brand":         "brand",
    "product":       "product_name",
    "productname":   "product_name",
    "addonname":     "addon_name",
    "addon_name":    "addon_name",
    "name":          "addon_name",
    "addoncategory": "addon_category",
    "category":      "addon_category",
    "appliesto":     "applies_to",
    "applies_to":    "applies_to",
    "wlp_addon":     "wlp_addon",
    "wlpaddon":      "wlp_addon",
    "wlp":           "wlp_addon",
    "srp_addon":     "srp_addon",
    "srpaddon":      "srp_addon",
    "srp":           "srp_addon",
    "mrpaddon":      "srp_addon",
    "ispercentage":  "is_percentage",
    "percentage":    "is_percentage",
    "sortorder":     "sort_order",
    "sort":          "sort_order",
    "notes":         "notes",
    "note":          "notes",
    "description":   "notes",
}


def _import_ophthalmic_addons(df, result: LoadResult, dry_run: bool, env_tag: str, stock_mode: str):
    """
    Load ophthalmic add-ons into ophthalmic_addons table.
    Upserts on (brand, addon_name, applies_to).
    """
    df = apply_column_map(df, OPH_ADDON_COLUMN_MAP)

    if "addon_name" not in df.columns or "brand" not in df.columns:
        result.add_error(0, "columns", "Required columns: Brand and AddonName")
        result.finish(); return

    upserts = []; skipped = 0

    for row_num, row in enumerate(df.to_dict("records"), start=2):
        brand      = _safe_str(row.get("brand"))
        addon_name = _safe_str(row.get("addon_name"))
        if not brand or not addon_name:
            skipped += 1; continue

        addon_cat  = _safe_str(row.get("addon_category")) or "General"
        applies_to = _safe_str(row.get("applies_to")) or "ALL"
        wlp_addon  = _safe_money(row.get("wlp_addon"))
        srp_addon  = _safe_money(row.get("srp_addon"))
        is_pct_raw = str(row.get("is_percentage") or "").strip().upper()
        is_pct     = is_pct_raw in ("YES","TRUE","1","Y")
        sort_order = int(row.get("sort_order") or 99)
        notes      = _safe_str(row.get("notes")) or ""

        product_name = _safe_str(row.get("product_name"))  # optional
        upserts.append((brand, addon_name, addon_cat, applies_to,
                        wlp_addon, srp_addon, is_pct, sort_order, notes,
                        product_name, row_num))

    result.total_rows = len(upserts) + skipped
    if not upserts:
        result.add_warning("No valid add-on rows found"); result.finish(); return
    if dry_run:
        result.add_warning(f"DRY RUN: {len(upserts)} add-ons would be loaded"); return

    ok = 0; err = 0
    _conn = None
    try:
        _conn = get_transaction_connection()
        cur   = _conn.cursor()
        for brand, addon_name, cat, applies_to, wlp, srp, is_pct, sort, notes, pname, rn in upserts:
            try:
                # Resolve optional product_id
                resolved_pid = None
                if pname:
                    cur.execute("""
                        SELECT id FROM products
                        WHERE LOWER(product_name)=LOWER(%s) AND is_active=TRUE LIMIT 1
                    """, (pname,))
                    pid_row = cur.fetchone()
                    if pid_row:
                        resolved_pid = str(pid_row[0])
                    else:
                        result.add_warning(f"Row {rn}: product '{pname}' not found — saved as brand-level")

                cur.execute("""
                    INSERT INTO ophthalmic_addons
                        (brand, addon_name, addon_category, applies_to,
                         wlp_addon, srp_addon, is_percentage, sort_order,
                         notes, product_id, is_active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid,TRUE)
                    ON CONFLICT (brand, addon_name, applies_to,
                                 COALESCE(product_id::text,'ALL')) DO UPDATE SET
                        addon_category = EXCLUDED.addon_category,
                        wlp_addon      = COALESCE(EXCLUDED.wlp_addon, ophthalmic_addons.wlp_addon),
                        srp_addon      = COALESCE(EXCLUDED.srp_addon, ophthalmic_addons.srp_addon),
                        is_percentage  = EXCLUDED.is_percentage,
                        sort_order     = EXCLUDED.sort_order,
                        notes          = EXCLUDED.notes,
                        product_id     = EXCLUDED.product_id,
                        is_active      = TRUE
                """, (brand, addon_name, cat, applies_to,
                      wlp, srp, is_pct, sort, notes, resolved_pid))
                ok += 1
            except Exception as e:
                result.add_error(rn, "DB", f"Add-on write failed: {e}", addon_name); err += 1
        _conn.commit()
        result.inserted = ok
        if skipped: result.add_warning(f"{skipped} rows skipped (missing brand/name)")
        if err:     result.add_warning(f"{err} rows failed")
    except Exception as e:
        if _conn:
            try: _conn.rollback()
            except: pass
        result.add_error(0, "DB-BATCH", f"Add-on batch failed: {e}")
    finally:
        try: cur.close()
        except: pass
        close_connection(_conn)


LOADER_MAP = {
    "PRODUCT": _import_products,
    "FRAME":   _import_frames,
    "PARTY":   _import_parties,
    "PATIENT": _import_patients,
    "OPHLENS": _import_ophlens,
    "CLENS":   _import_clens,
    "PRICE":   _import_price,
    "SOL":     _import_sol,
    "BLANK":   _import_blanks,
    "OPH_SPEC":  _import_ophthalmic_specs,
    "OPH_ADDON": _import_ophthalmic_addons,
}


def run_loader(
    file_path: str,
    mode: str = "DRY",           # "DRY" | "SHADOW" | "LIVE"
    stock_mode: str = "ADD",     # "ADD" | "OPENING"
    force_type: str = None,      # override auto-detection
    progress_callback=None,      # optional callable(current, total)
    user: str = "system",        # operator name for audit log
) -> LoadResult:
    """
    Main entry point. Called by UI and CLI.

    mode="DRY"          → validate only, no DB writes
    mode="SHADOW"       → writes with environment_tag='SHADOW'
    mode="LIVE"         → writes with environment_tag='LIVE'

    stock_mode="ADD"     → accumulate qty (safe default — use for ongoing imports)
    stock_mode="OPENING" → overwrite qty  (use for opening stock / audit corrections)
                           Only applies to: OPHLENS, CLENS, BLANK, FRAME
                           No effect on: PRODUCT, PARTY, PATIENT, SOL (masters — always upsert)

    user                → operator identifier written to loader_import_log
    """
    import os

    # ── Audit setup ───────────────────────────────────────────────────────────
    import_id  = generate_import_id()
    file_name  = os.path.basename(file_path)
    dry_run    = (mode == "DRY")
    env_tag    = "SHADOW" if mode == "SHADOW" else "LIVE"

    # ── Load Excel ────────────────────────────────────────────────────────────
    try:
        df, ingestion_report = load_excel(file_path)
    except Exception as e:
        result = LoadResult("UNKNOWN", mode)
        result.add_error(0, "file", f"Cannot read Excel file: {e}")
        result.finish()
        _log_import_to_db(import_id, file_name, "UNKNOWN", mode, stock_mode, result, user, "FAILED")
        return result

    # ── Detect type ───────────────────────────────────────────────────────────
    file_type = force_type or detect_file_type(df)
    effective_stock_mode = _resolve_stock_mode(stock_mode, file_type)
    result = LoadResult(file_type, mode, stock_mode=effective_stock_mode)
    result.import_id = import_id            # expose for UI display
    result.ingestion_report = ingestion_report  # expose quality score + warnings to UI

    # Surface ingestion shield warnings as import warnings so UI shows them
    for w in ingestion_report.warnings:
        result.add_warning(w)

    if file_type == "UNKNOWN":
        result.add_error(0, "file_type",
            "Cannot detect file type. Check that required columns exist. "
            "See excel_schema_contract for required headers.")
        result.finish()
        _log_import_to_db(import_id, file_name, file_type, mode, stock_mode, result, user, "FAILED")
        return result

    # ── Dispatch ──────────────────────────────────────────────────────────────
    loader_fn = LOADER_MAP.get(file_type)
    if not loader_fn:
        result.add_error(0, "file_type", f"No loader registered for type: {file_type}")
        result.finish()
        _log_import_to_db(import_id, file_name, file_type, mode, stock_mode, result, user, "FAILED")
        return result

    # ── Row dedup — load previously seen hashes (LIVE/SHADOW only) ────────────
    seen_hashes = set()
    new_hashes  = []
    # Skip dedup for PRICE_ONLY — price corrections are intentional re-uploads
    if not dry_run and effective_stock_mode != STOCK_MODE_PRICE_ONLY:
        seen_hashes = _load_seen_hashes(file_type)

    # ── Inject row hash check before loader processes each row ────────────────
    # We snapshot the df rows, filter duplicates, pass cleaned df to loader
    if not dry_run and len(df) > 0:
        keep_indices = []
        dup_count    = 0
        for idx, (_, row) in enumerate(df.iterrows()):
            h = row_hash(row.to_dict())
            if h in seen_hashes:
                dup_count += 1
            else:
                keep_indices.append(idx)
                new_hashes.append(h)
                seen_hashes.add(h)  # prevent within-batch duplication
        if dup_count:
            result.add_warning(
                f"⚠️ {dup_count} duplicate row(s) detected (already imported) — skipped. "
                "Use OPENING mode or clear row history to force re-import."
            )
            df = df.iloc[keep_indices].reset_index(drop=True)

    # ── Run the loader ────────────────────────────────────────────────────────
    try:
        loader_fn(df, result, dry_run, env_tag, effective_stock_mode)
    except Exception as e:
        result.add_error(0, "loader", f"Loader crashed: {e}")

    result.finish()

    # ── Write audit records ───────────────────────────────────────────────────
    if dry_run:
        _log_import_to_db(import_id, file_name, file_type, mode, stock_mode, result, user, "DRY")
    else:
        import_status = "FAILED" if not result.errors and result.total_rows == 0 else (
            "PARTIAL" if result.errors else "OK"
        )
        _log_import_to_db(import_id, file_name, file_type, mode, stock_mode, result, user, import_status)
        _log_row_hashes(import_id, file_type, new_hashes)

    logger.info(
        f"[LOADER] {import_id[:8]}… | {file_type} | {mode} | "
        f"ins={result.inserted} upd={result.updated} skip={result.skipped} err={len(result.errors)}"
    )

    return result


# ═══════════════════════════════════════════════════════
# ADMIN HELPER — check if OPENING mode is currently allowed
# ═══════════════════════════════════════════════════════

def is_opening_mode_allowed() -> bool:
    """
    Returns whether OPENING mode is currently enabled.
    In future: can read from DB settings table.
    Currently: reads ALLOW_OPENING_MODE module constant.
    """
    return ALLOW_OPENING_MODE


def set_opening_mode_allowed(enabled: bool):
    """
    Runtime toggle for OPENING mode. For admin use.
    Note: module-level constant — resets on restart.
    For persistence, use DB settings table.
    """
    global ALLOW_OPENING_MODE
    ALLOW_OPENING_MODE = enabled
    logger.info(f"OPENING mode {'enabled' if enabled else 'DISABLED'} by admin")


# ═══════════════════════════════════════════════════════
# DB EXPORT (for loader.py / complete_audit_export.py)
# ═══════════════════════════════════════════════════════

def export_all_tables(output_path: str) -> Tuple[bool, str]:
    """Replaces loader.py — exports all DB tables to Excel."""
    try:
        import openpyxl
        tables = run_query("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' ORDER BY table_name
        """)
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            for row in tables:
                tbl = row["table_name"]
                try:
                    df = pd.read_sql(f"SELECT * FROM {tbl}", _get_raw_conn())
                    for col in df.select_dtypes(include=["datetimetz"]).columns:
                        df[col] = df[col].dt.tz_localize(None)
                    df.to_excel(writer, sheet_name=tbl[:31], index=False)
                except Exception:
                    pass
        return True, output_path
    except Exception as e:
        return False, str(e)


def export_master_report(output_path: str) -> Tuple[bool, str]:
    """Replaces complete_audit_export.py."""
    QUERIES = {
        "Product_Master": "SELECT id, product_name, brand, main_group, category, material, index_value, coating, colour, is_active, created_at FROM products ORDER BY product_name",
        "Party_Master":   "SELECT id, party_name, party_type, status, mobile, address, city FROM parties ORDER BY party_name",
        "Batch_Master":   "SELECT id, product_id, batch_no, expiry_date, COALESCE(qty_available,0) AS quantity, COALESCE(cost_price,0), COALESCE(mrp,0), is_active FROM batches ORDER BY id",
        "Frame_Master":   "SELECT id, product_name, model, brand, sku_code, COALESCE(qty,0), COALESCE(cost_price,0), COALESCE(mrp,0), is_active FROM frames ORDER BY sku_code",
        "Patient_Master": "SELECT id, mobile, master_name, record_no, created_at FROM patients ORDER BY id DESC",
        "Inventory_Stock":"SELECT id, product_id, sph, cyl, axis, add_power, eye_side, batch_no, COALESCE(quantity,0), COALESCE(mrp,0), is_active FROM inventory_stock ORDER BY id",
    }
    try:
        conn = _get_raw_conn()
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            for sheet, q in QUERIES.items():
                try:
                    pd.read_sql(q, conn).to_excel(writer, sheet_name=sheet, index=False)
                except Exception:
                    pass
        conn.close()
        return True, output_path
    except Exception as e:
        return False, str(e)


def _get_raw_conn():
    """Raw connection for pandas read_sql only."""
    from modules.sql_adapter import _get_db_config
    import psycopg2
    return psycopg2.connect(**_get_db_config())

def _canonical_to_db_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Bridge: normalised canonical column names → snake_case DB column names.
    Applied as the LAST step in load_excel() so all loaders receive DB-native names.
    snake_case columns that arrive already (e.g. from registry-driven downloads)
    pass through unchanged — they are already in the target form.
    """
    BRIDGE_MAP = {
        # Core identity
        "productname":       "product_name",
        "maingroup":         "main_group",
        "brandproductgroup": "brand_group",
        "lenscategory":      "lens_category",
        "coatingtype":       "coating_type",
        "wearschedule":      "wear_schedule",
        "hsncode":           "hsn_code",
        "boxsize":           "box_size",
        "allowloose":        "allow_loose",
        "isbatchapplicable": "is_batch_applicable",
        "iseyespecific":     "is_eye_specific",
        "isactive":          "is_active",
        # Stock / inventory
        "purchaserate":      "purchase_rate",
        "sellingprice":      "selling_price",
        "eyeside":           "eye_side",
        "addpower":          "add_power",
        "lensdesign":        "lens_design",
        "itemtype":          "item_type",
        "stocktype":         "stock_type",
        "batchno":           "batch_no",
        "expirydate":        "expiry_date",
        "indexvalue":        "index_value",
        # Blank inventory
        "qtyright":          "qty_right",
        "qtyleft":           "qty_left",
        "qtyindependent":    "qty_independent",
        "recommendedbase":   "base_recommended",
        "recomendedbase":    "base_recommended",
        "base1p":            "base_1",
        "base2p":            "base_2",
        "base3p":            "base_3",
        "costprice":         "cost_price",
        "minstock":          "min_stock",
        # Frames
        "skucode":           "sku_code",
        "templelength":      "temple_length",
        "basematerial":      "base_material",
        "sizea":             "size_a",
        "asize":             "size_a",
        # Party
        "partyname":         "party_name",
        "partytype":         "party_type",
        # Patient / visits
        "clientname":        "master_name",
        "mobilenumber":      "mobile",
        "recordno":          "record_no",
        "rightsph":          "right_sph",
        "rightcyl":          "right_cyl",
        "rightaxis":         "right_axis",
        "rightaddpower":     "right_add",
        "leftsph":           "left_sph",
        "leftcyl":           "left_cyl",
        "leftaxis":          "left_axis",
        "leftaddpower":      "left_add",
        # SOL
        "qtyavailable":      "qty_available",
    }
    return df.rename(columns={k: v for k, v in BRIDGE_MAP.items() if k in df.columns})
