"""
modules/crm/crm.py  —  DV ERP Professional CRM  (v3)
=====================================================
Tally/ERP-grade party management with:
  - GST, PAN, GSTIN, TAN, CIN, State Code
  - Credit limit & credit days
  - Contact person, email, alternate phone
  - Billing address vs shipping address
  - Party category tags
  - Lead pipeline & follow-up tracker

DB TRUTH (from db_schema_registry.py):
  parties.is_active  = BOOLEAN  (the real active flag)
  parties.status     = BOOLEAN  (legacy mirror — do NOT write varchar here)
  >>> CRM writes is_active only. NEVER writes to status column. <<<
"""

import streamlit as st
import uuid
import logging
import traceback
import json
from datetime import date, timedelta
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

PARTY_TYPES   = ["Supplier","Retail","Wholesale","Doctor","Optician","Fitter"]
LEAD_STAGES   = ["NEW","CONTACTED","QUALIFIED","PROPOSAL","WON","LOST"]
STAGE_COLORS  = {"NEW":"#6b7280","CONTACTED":"#3b82f6","QUALIFIED":"#8b5cf6",
                 "PROPOSAL":"#f59e0b","WON":"#10b981","LOST":"#ef4444"}
FOLLOWUP_TYPES = ["CALL","VISIT","WHATSAPP","EMAIL","DEMO","OTHER"]
LEAD_SOURCES   = ["Walk-in","Referral","Cold Call","WhatsApp","Exhibition",
                  "Online","Existing Customer","Other"]
FU_ICONS = {"CALL":"📞","VISIT":"🚗","WHATSAPP":"💬","EMAIL":"📧","DEMO":"🖥️","OTHER":"📌"}

INDIAN_STATES = [
    "Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh","Goa","Gujarat",
    "Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh",
    "Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha","Punjab","Rajasthan",
    "Sikkim","Tamil Nadu","Telangana","Tripura","Uttar Pradesh","Uttarakhand","West Bengal",
    "Andaman and Nicobar Islands","Chandigarh","Dadra and Nagar Haveli and Daman and Diu",
    "Delhi","Jammu and Kashmir","Ladakh","Lakshadweep","Puducherry","Other",
]

GST_RATES = ["0","5","12","18","28"]


ADMIN_DB_ENTITIES = {
    "Parties / Customers": {
        "table": "parties",
        "pk": "id",
        "search": ["party_name", "mobile", "gstin", "party_type", "billing_preference",
                   "doc_preference", "area", "city", "contact_person"],
        "default_cols": [
            "party_name", "party_type", "mobile", "gstin",
            "billing_preference", "doc_preference", "credit_limit", "credit_days",
            "preferred_courier_name", "is_active",
        ],
        "editable": [
            "party_name", "party_type", "mobile", "alt_mobile", "email",
            "contact_person", "area", "city", "state_name", "pincode",
            "gstin", "pan_no", "state_code",
            "billing_preference", "doc_preference", "credit_limit", "credit_days",
            "opening_balance", "balance_type", "preferred_courier_name",
            "notes", "is_active",
        ],
        "dropdowns": {
            "party_type":         ["", "Supplier","Retail","Wholesale","Doctor","Optician","Fitter"],
            "doc_preference":     ["", "C", "I"],
            "billing_preference": ["", "CHALLAN", "DIRECT_INVOICE"],
            "balance_type":       ["Dr", "Cr"],
            "is_active":          [True, False],
        },
        "identity": ["party_name"],
        "tax": ["gstin", "pan_no", "state_code"],
        "help": {
            "doc_preference":     "C = Challan first, I = Invoice direct",
            "billing_preference": "Default document sent to this party",
            "credit_limit":       "Max outstanding allowed in ₹",
        },
    },
    "Patients": {
        "table": "patients",
        "pk": "id",
        "search": ["master_name", "mobile", "barcode", "email"],
        "default_cols": ["master_name", "mobile", "age", "gender", "barcode", "is_active"],
        "editable": ["master_name", "mobile", "alt_mobile", "email",
                     "age", "gender", "barcode", "address", "city", "pincode", "notes", "is_active"],
        "dropdowns": {
            "gender": ["", "Male", "Female", "Other"],
            "is_active": [True, False],
        },
        "identity": ["master_name"],
        "tax": [],
    },
    "Products (All)": {
        "table": "products",
        "pk": "id",
        "search": ["product_code", "product_name", "brand", "main_group", "category", "barcode", "sku_code", "hsn_code"],
        "default_cols": [
            "product_code", "product_name", "brand", "main_group", "category", "unit",
            "gst_percent", "barcode", "sku_code", "hsn_code", "is_active",
        ],
        "editable": [
            "product_code", "product_name", "brand", "main_group", "category", "material",
            "index_value", "coating", "colour", "unit", "gst_percent", "barcode",
            "sku_code", "hsn_code", "box_size", "allow_loose", "min_stock_qty",
            "base_curve", "diameter", "replacement_schedule", "company_product_name",
            "normal_procurement_discount_pct", "scheme_procurement_discount_pct",
            "discount_percent", "is_gst_exempt", "online_active", "is_active",
        ],
        "dropdowns": {"is_active": [True, False], "gst_percent": [0, 5, 12, 18, 28]},
        "identity": ["product_name", "brand"],
        "tax": ["gst_percent", "hsn_code", "is_gst_exempt"],
        "filter_cols": ["main_group", "brand", "category", "unit", "gst_percent", "is_active"],
    },
    "Frames": {
        "table": "inventory_stock",
        "pk": "id",
        "fixed_filter_sql": (
            "product_id IN ("
            "SELECT id FROM products "
            "WHERE LOWER(COALESCE(main_group::text,'')) LIKE '%frame%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%frame%' "
            "OR LOWER(COALESCE(main_group::text,'')) LIKE '%sunglass%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%sunglass%'"
            ")"
        ),
        "display_exprs": {
            "product_name": "(SELECT p.product_name FROM products p WHERE p.id = inventory_stock.product_id)",
            "brand": "(SELECT p.brand FROM products p WHERE p.id = inventory_stock.product_id)",
            "main_group": "(SELECT p.main_group FROM products p WHERE p.id = inventory_stock.product_id)",
        },
        "search": ["product_name", "brand", "barcode", "product_barcode", "item_code", "batch_no", "location", "bin_no", "model"],
        "default_cols": [
            "product_name", "brand", "item_code", "batch_no", "barcode", "product_barcode",
            "quantity", "mrp", "selling_price", "purchase_rate", "location",
            "bin_no", "model", "colour", "shape", "frame_type", "is_active",
        ],
        "editable": [
            "barcode", "product_barcode", "item_code", "batch_no",
            "quantity", "mrp", "selling_price", "purchase_rate", "purchase_price",
            "allocated_qty", "reserved_qty", "location", "bin_no", "model",
            "size_a", "size_b", "dbl", "temple_length",
            "base_material", "finish", "colour", "colour_mix", "temple_colour",
            "shape", "gender", "frame_type", "frame_seq", "image_path", "is_active",
        ],
        "dropdowns": {"is_active": [True, False]},
        "identity": ["barcode", "product_barcode", "item_code", "batch_no"],
        "tax": [],
        "filter_cols": ["product_name", "brand", "model", "location", "bin_no", "colour", "shape", "frame_type"],
        "labels": {
            "item_code": "Scan Code / Item Code",
            "batch_no": "Batch No",
            "barcode": "Barcode",
            "product_barcode": "Product Barcode",
            "quantity": "Qty",
            "purchase_rate": "Purchase Price",
            "mrp": "MRP",
        },
        "help": {
            "product_barcode": "Product-level barcode if available.",
            "barcode": "Actual stock/barcode scan code.",
            "item_code": "FOR SCANNING: universal code used by Retail/Wholesale/Bulk punching, stock search and future loaders.",
            "batch_no": "NOT FOR SCANNING. Batch/lot number only, mainly for contact lenses/solutions/expiry tracking.",
            "quantity": "Physical frame quantity.",
            "mrp": "Sticker MRP. Changing this affects future sticker prints.",
            "selling_price": "Optional selling price override if used separately from MRP.",
        },
        "scan_note": "Scanning rule: put the printed barcode/SKU in inventory_stock.item_code (shown here as Scan Code / Item Code). Do not put frame scan codes in Batch No.",
    },
    "Old Frame Master": {
        "table": "frames",
        "pk": "id",
        "search": ["product_name", "model", "brand", "sku_code", "colour", "shape", "location", "frame_group"],
        "default_cols": [
            "sku_code", "brand", "product_name", "model", "qty",
            "mrp", "selling_price", "cost_price", "location",
            "size_a", "size_b", "dbl", "temple_length",
            "colour", "shape", "frame_type", "is_active",
        ],
        "editable": [
            "sku_code", "brand", "product_name", "model",
            "qty", "mrp", "selling_price", "cost_price",
            "location", "frame_group", "size_a", "size_b", "dbl", "temple_length",
            "base_material", "finish", "colour", "colour_mix", "temple_colour",
            "shape", "gender", "frame_type", "frame_seq", "image_path", "is_active",
        ],
        "dropdowns": {"is_active": [True, False]},
        "identity": ["sku_code", "model", "brand"],
        "tax": [],
        "filter_cols": ["brand", "model", "location", "frame_group", "colour", "shape", "frame_type"],
    },
    "Lenses": {
        "table": "products",
        "pk": "id",
        "fixed_filter_sql": (
            "(LOWER(COALESCE(main_group::text,'')) LIKE '%lens%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%lens%' "
            "OR LOWER(COALESCE(product_name::text,'')) LIKE '%lens%' "
            "OR LOWER(COALESCE(main_group::text,'')) LIKE '%ophthalmic%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%ophthalmic%')"
        ),
        "search": ["product_code", "product_name", "brand", "main_group", "category", "barcode", "sku_code", "hsn_code"],
        "default_cols": [
            "product_name", "brand", "main_group", "category", "material",
            "index_value", "coating", "colour", "unit", "gst_percent", "sku_code", "barcode", "is_active",
        ],
        "editable": [
            "product_code", "product_name", "brand", "main_group", "category",
            "material", "index_value", "coating", "colour", "unit",
            "gst_percent", "barcode", "sku_code", "hsn_code", "min_stock_qty",
            "base_curve", "diameter", "normal_procurement_discount_pct",
            "scheme_procurement_discount_pct", "discount_percent", "is_gst_exempt", "is_active",
        ],
        "dropdowns": {"is_active": [True, False], "gst_percent": [0, 5, 12, 18, 28]},
        "identity": ["product_name", "brand"],
        "tax": ["gst_percent", "hsn_code", "is_gst_exempt"],
        "filter_cols": ["brand", "category", "material", "index_value", "coating", "colour", "gst_percent", "is_active"],
    },
    "Contact Lenses": {
        "table": "products",
        "pk": "id",
        "fixed_filter_sql": (
            "(LOWER(COALESCE(main_group::text,'')) LIKE '%contact%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%contact%' "
            "OR LOWER(COALESCE(product_name::text,'')) LIKE '%contact%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%toric%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%spherical%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%multifocal%')"
        ),
        "search": ["product_code", "product_name", "brand", "main_group", "category", "barcode", "sku_code", "hsn_code"],
        "default_cols": [
            "product_name", "brand", "main_group", "category", "unit",
            "gst_percent", "barcode", "sku_code", "box_size", "base_curve", "diameter", "is_active",
        ],
        "editable": [
            "product_code", "product_name", "brand", "main_group", "category", "unit",
            "gst_percent", "barcode", "sku_code", "hsn_code", "box_size", "allow_loose",
            "base_curve", "diameter", "dk_value", "dk_t_value", "water_content",
            "ct_value", "modulus", "replacement_schedule", "company_product_name",
            "normal_procurement_discount_pct", "scheme_procurement_discount_pct",
            "discount_percent", "is_gst_exempt", "is_active",
        ],
        "dropdowns": {"is_active": [True, False], "gst_percent": [0, 5, 12, 18, 28]},
        "identity": ["product_name", "brand"],
        "tax": ["gst_percent", "hsn_code", "is_gst_exempt"],
        "filter_cols": ["brand", "category", "box_size", "base_curve", "diameter", "gst_percent", "is_active"],
    },
    "Solutions": {
        "table": "products",
        "pk": "id",
        "fixed_filter_sql": (
            "(LOWER(COALESCE(main_group::text,'')) LIKE '%solution%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%solution%' "
            "OR LOWER(COALESCE(product_name::text,'')) LIKE '%solution%' "
            "OR LOWER(COALESCE(product_name::text,'')) LIKE '%multipurpose%' "
            "OR LOWER(COALESCE(product_name::text,'')) LIKE '%lens care%' "
            "OR LOWER(COALESCE(product_name::text,'')) LIKE '%cleaning%' "
            "OR LOWER(COALESCE(product_name::text,'')) LIKE '%eye drop%')"
        ),
        "search": ["product_code", "product_name", "brand", "main_group", "category", "barcode", "sku_code", "hsn_code"],
        "default_cols": [
            "product_name", "brand", "main_group", "category", "unit",
            "gst_percent", "barcode", "sku_code", "box_size", "is_active",
        ],
        "editable": [
            "product_code", "product_name", "brand", "main_group", "category", "unit",
            "gst_percent", "barcode", "sku_code", "hsn_code", "box_size", "allow_loose",
            "company_product_name", "normal_procurement_discount_pct",
            "scheme_procurement_discount_pct", "discount_percent", "is_gst_exempt", "is_active",
        ],
        "dropdowns": {"is_active": [True, False], "gst_percent": [0, 5, 12, 18, 28]},
        "identity": ["product_name", "brand"],
        "tax": ["gst_percent", "hsn_code", "is_gst_exempt"],
        "filter_cols": ["brand", "category", "unit", "gst_percent", "is_active"],
    },
    "Frame Stock / Barcodes": {
        "table": "inventory_stock",
        "pk": "id",
        "fixed_filter_sql": (
            "product_id IN ("
            "SELECT id FROM products "
            "WHERE LOWER(COALESCE(main_group::text,'')) LIKE '%frame%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%frame%' "
            "OR LOWER(COALESCE(main_group::text,'')) LIKE '%sunglass%' "
            "OR LOWER(COALESCE(category::text,'')) LIKE '%sunglass%'"
            ")"
        ),
        "search": ["barcode", "product_barcode", "item_code", "batch_no", "location", "bin_no", "model"],
        "default_cols": [
            "product_id", "barcode", "product_barcode", "item_code", "batch_no",
            "quantity", "mrp", "selling_price", "location", "bin_no", "model",
            "colour", "shape", "frame_type",
        ],
        "editable": [
            "barcode", "product_barcode", "item_code", "batch_no",
            "mrp", "selling_price", "purchase_rate", "purchase_price",
            "quantity", "allocated_qty", "reserved_qty", "location", "bin_no",
            "model", "colour", "shape", "size_a", "size_b", "dbl", "temple_length",
            "frame_type", "gender", "is_active",
        ],
        "dropdowns": {"is_active": [True, False]},
        "identity": ["barcode", "product_barcode", "item_code", "batch_no"],
        "tax": [],
        "labels": {
            "item_code": "Scan Code / Item Code",
            "batch_no": "Batch No",
            "barcode": "Barcode",
            "product_barcode": "Product Barcode",
        },
        "help": {
            "item_code": "FOR SCANNING: universal code used by Retail/Wholesale/Bulk punching and stock search.",
            "batch_no": "NOT FOR SCANNING. Use only true batch/lot number where applicable.",
        },
        "scan_note": "Scanning rule: Scan Code / Item Code = inventory_stock.item_code. Batch No is not used for frame scanning.",
    },
    "Inventory / Stock": {
        "table": "inventory_stock",
        "pk": "id",
        "search": ["barcode", "product_barcode", "item_code", "batch_no", "location", "bin_no", "model"],
        "default_cols": [
            "product_id", "barcode", "product_barcode", "item_code", "batch_no",
            "quantity", "mrp", "selling_price", "location", "bin_no", "model",
        ],
        "editable": [
            "barcode", "product_barcode", "item_code", "batch_no", "expiry_date",
            "mrp", "selling_price", "purchase_rate", "purchase_price",
            "quantity", "allocated_qty", "reserved_qty", "location", "bin_no",
            "model", "colour", "shape", "size_a", "size_b", "dbl", "temple_length",
            "frame_type", "gender", "is_active",
        ],
        "dropdowns": {"is_active": [True, False]},
        "identity": ["barcode", "product_barcode", "item_code", "batch_no"],
        "tax": [],
        "labels": {
            "item_code": "Scan Code / Item Code",
            "batch_no": "Batch No",
            "barcode": "Barcode",
            "product_barcode": "Product Barcode",
        },
        "help": {
            "item_code": "FOR SCANNING: universal code used by Retail/Wholesale/Bulk punching and stock search.",
            "batch_no": "NOT FOR SCANNING. Use for batch/lot/expiry only, especially contact lenses and solutions.",
        },
        "scan_note": "Scanning rule: enter scanner-readable item codes in inventory_stock.item_code. Keep batch_no for real batch/lot numbers.",
    },
    "Service Master": {
        "table": "service_types",
        "pk": "id",
        "search": ["service_code", "service_group", "service_name"],
        "default_cols": [
            "service_code", "service_group", "service_name",
            "retail_price", "wholesale_price", "gst_percent",
            "production_route", "is_active",
        ],
        "editable": [
            "service_code", "service_group", "service_name",
            "retail_price", "wholesale_price", "gst_percent",
            "production_route", "sort_order", "is_active", "notes",
        ],
        "dropdowns": {
            "service_group":    ["COURIER","FITTING","COLOURING","CONSULTATION","EYE_TESTING","MISC","OTHER"],
            "production_route": ["", "FITTING", "COLOURING"],
            "gst_percent":      [0, 5, 12, 18, 28],
            "is_active":        [True, False],
        },
        "identity": ["service_name"],
        "tax": ["gst_percent"],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# RAW DB
# ─────────────────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        logger.error(f"[CRM:q] {e}")
        return []

def _w(sql, params=None):
    """Returns None on success, error string on failure."""
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params)
        return None
    except Exception as e:
        logger.error(f"[CRM:w] {e}")
        return str(e)


# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP — once per session
# ─────────────────────────────────────────────────────────────────────────────

def _bootstrap():
    if st.session_state.get("_crm_boot3"):
        return

    required_party_cols = {
        "gstin": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS gstin VARCHAR(15)",
        "pan_no": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS pan_no VARCHAR(10)",
        "tan_no": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS tan_no VARCHAR(10)",
        "cin_no": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS cin_no VARCHAR(21)",
        "gst_rate": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS gst_rate NUMERIC(5,2) DEFAULT 0",
        "state_code": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS state_code VARCHAR(2)",
        "state_name": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS state_name VARCHAR(80)",
        "pincode": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS pincode VARCHAR(6)",
        "email": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS email VARCHAR(120)",
        "alt_mobile": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS alt_mobile VARCHAR(15)",
        "contact_person": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS contact_person VARCHAR(100)",
        "credit_limit": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS credit_limit NUMERIC(12,2) DEFAULT 0",
        "credit_days": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS credit_days INTEGER DEFAULT 0",
        "opening_balance": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS opening_balance NUMERIC(12,2) DEFAULT 0",
        "balance_type": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS balance_type VARCHAR(10) DEFAULT 'Dr'",
        "tally_group": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS tally_group VARCHAR(80)",
        "notes": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS notes TEXT",
        "preferred_courier_provider_id": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS preferred_courier_provider_id UUID",
        "preferred_courier_name": "ALTER TABLE parties ADD COLUMN IF NOT EXISTS preferred_courier_name TEXT",
    }
    existing = {
        r.get("column_name")
        for r in _q(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='parties'",
            {},
        )
    }
    ddls = [ddl for col, ddl in required_party_cols.items() if col not in existing]
    ddls += [
        """CREATE TABLE IF NOT EXISTS crm_leads (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            party_id UUID, lead_source VARCHAR(80), stage VARCHAR(40) DEFAULT 'NEW',
            notes TEXT, assigned_to VARCHAR(80), potential_value NUMERIC(12,2) DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS crm_followups (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            party_id UUID, lead_id UUID, followup_type VARCHAR(40) DEFAULT 'CALL',
            due_date DATE, done BOOLEAN DEFAULT FALSE, done_at TIMESTAMP,
            notes TEXT, created_by VARCHAR(80), created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS crm_touchpoints (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            party_id UUID, type VARCHAR(40) DEFAULT 'NOTE',
            summary TEXT, created_by VARCHAR(80), created_at TIMESTAMP DEFAULT NOW())""",
    ]
    for ddl in ddls:
        err = _w(ddl)
        if err:
            logger.warning(f"[CRM:DDL] {err}")
    st.session_state["_crm_boot3"] = True


# ─────────────────────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────────────────────

def _cached(key, fn):
    """Cache result in session_state. Never stores None so failed DB calls retry."""
    k = f"_crm_{key}"
    if k not in st.session_state or st.session_state[k] is None:
        result = fn()
        if result is not None:          # only cache successful results
            st.session_state[k] = result
        return result if result is not None else []
    return st.session_state[k]

def _bust():
    for k in list(st.session_state.keys()):
        if k.startswith("_crm_") and k not in ("_crm_boot3",):
            del st.session_state[k]


# ─────────────────────────────────────────────────────────────────────────────
# FETCH (all cached)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_parties(party_type=None, search=""):
    key = f"pl_{party_type}_{search}"
    def _load():
        s = f"%{(search or '').lower()}%"
        base = ("SELECT id, party_name, party_type, mobile, city, area, "
                "is_active, gstin, credit_limit, credit_days, contact_person, email "
                "FROM parties ")
        if party_type and party_type != "All":
            return _q(base + "WHERE party_type=%(t)s AND "
                      "(LOWER(party_name) LIKE %(s)s OR COALESCE(mobile,'') LIKE %(s)s) "
                      "ORDER BY party_name LIMIT 300",
                      {"t": party_type, "s": s})
        return _q(base + "WHERE (LOWER(party_name) LIKE %(s)s OR COALESCE(mobile,'') LIKE %(s)s) "
                  "ORDER BY party_name LIMIT 300", {"s": s})
    return _cached(key, _load)

def fetch_party(pid):
    rows = _q("SELECT * FROM parties WHERE id=%(id)s", {"id": pid})
    return rows[0] if rows else None

def fetch_suppliers():
    return _cached("sup", lambda: _q(
        "SELECT id, party_name, mobile, city, area, is_active, gstin, "
        "credit_limit, credit_days, contact_person "
        "FROM parties WHERE party_type='Supplier' ORDER BY party_name", {}))

def fetch_metrics():
    def _load():
        # parties count — always safe
        base = _q("SELECT COUNT(*) AS tp FROM parties", {})
        tp   = (base[0].get("tp", 0) if base else 0)
        sup  = _q("SELECT COUNT(*) AS ts FROM parties WHERE party_type='Supplier'", {})
        ts   = (sup[0].get("ts", 0) if sup else 0)
        # CRM tables may not exist yet — query separately so one failure doesn't kill all
        try:
            from modules.sql_adapter import run_query as _rqm
            fup = _rqm("SELECT COUNT(*) AS of FROM crm_followups WHERE done=FALSE AND due_date<=CURRENT_DATE", {})
            of_ = fup[0].get("of", 0) if fup else 0
        except Exception:
            of_ = 0
        try:
            from modules.sql_adapter import run_query as _rqm2
            lds = _rqm2("SELECT COUNT(*) AS ol FROM crm_leads WHERE stage NOT IN ('WON','LOST')", {})
            ol  = lds[0].get("ol", 0) if lds else 0
        except Exception:
            ol  = 0
        return {"tp": tp, "ts": ts, "of": of_, "ol": ol}
    return _cached("metrics", _load)

def fetch_leads(stage=None):
    key = f"leads_{stage}"
    def _load():
        where  = "" if not stage or stage == "All" else "WHERE cl.stage=%(stage)s"
        params = {} if not stage or stage == "All" else {"stage": stage}
        return _q(f"SELECT cl.id, cl.stage, cl.lead_source, cl.notes, cl.assigned_to, "
                  f"cl.potential_value, cl.updated_at, p.party_name, p.mobile "
                  f"FROM crm_leads cl LEFT JOIN parties p ON p.id=cl.party_id "
                  f"{where} ORDER BY cl.updated_at DESC LIMIT 200", params)
    return _cached(key, _load)

def fetch_followups_all():
    return _cached("fups", lambda: _q(
        "SELECT cf.id, cf.followup_type, cf.due_date, cf.notes, "
        "p.party_name, p.mobile FROM crm_followups cf "
        "LEFT JOIN parties p ON p.id=cf.party_id "
        "WHERE cf.done=FALSE ORDER BY cf.due_date ASC LIMIT 200", {}))

def fetch_touchpoints(pid):
    # NOT cached — lazy, called only inside open expander
    return _q("SELECT type, summary, created_by, created_at FROM crm_touchpoints "
              "WHERE party_id=%(pid)s ORDER BY created_at DESC LIMIT 20", {"pid": pid})


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN DB REVIEW / BULK EDIT
# ─────────────────────────────────────────────────────────────────────────────

def _db_columns(table: str) -> List[Dict]:
    return _cached(f"dbcols_{table}", lambda: _q("""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%(t)s
            ORDER BY ordinal_position
        """, {"t": table}))


def _safe_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _coerce_editor_value(value, data_type: str):
    if value is None:
        return None
    if isinstance(value, float) and str(value) == "nan":
        return None
    text = str(value)
    if text.strip() == "":
        return None
    dt = str(data_type or "").lower()
    if "bool" in dt:
        if isinstance(value, bool):
            return value
        return text.strip().lower() in ("true", "1", "yes", "y", "active")
    if any(k in dt for k in ("integer", "numeric", "double", "real")):
        try:
            return float(value)
        except Exception:
            return None
    return value


def _admin_col_expr(cfg: Dict, col: str) -> str:
    display_exprs = cfg.get("display_exprs") or {}
    if col in display_exprs:
        return str(display_exprs[col])
    return _safe_ident(col)


def _admin_db_distinct_values(cfg: Dict, col: str, limit: int = 200) -> List[str]:
    table = cfg["table"]
    expr = _admin_col_expr(cfg, col)
    where = []
    fixed_filter = (cfg.get("fixed_filter_sql") or "").strip()
    if fixed_filter:
        where.append(fixed_filter.replace("%", "%%"))
    sql = f"SELECT DISTINCT {expr}::text AS v FROM {_safe_ident(table)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY 1 NULLS LAST LIMIT {int(limit or 200)}"
    return [str(r.get("v") or "") for r in (_q(sql, {}) or []) if str(r.get("v") or "").strip()]


def _admin_db_fetch_rows(cfg: Dict, columns: List[str], search: str, limit: int, filters: Dict = None) -> List[Dict]:
    table = cfg["table"]
    pk = cfg["pk"]
    display_exprs = cfg.get("display_exprs") or {}
    real_cols = {r["column_name"] for r in _db_columns(table)}
    select_cols = [pk] + [c for c in columns if c != pk]
    select_bits = []
    for col in select_cols:
        if col in display_exprs:
            select_bits.append(f"{display_exprs[col]} AS {_safe_ident(col)}")
        elif col in real_cols:
            select_bits.append(_safe_ident(col))
    sql = "SELECT " + ", ".join(select_bits) + f" FROM {_safe_ident(table)}"
    params = {"lim": int(limit or 50)}
    where = []
    fixed_filter = (cfg.get("fixed_filter_sql") or "").strip()
    if fixed_filter:
        where.append(fixed_filter.replace("%", "%%"))
    if search.strip():
        params["s"] = f"%{search.strip().lower()}%"
        search_cols = [c for c in cfg.get("search", []) if c in real_cols or c in display_exprs]
        if search_cols:
            where.append("(" + " OR ".join(f"LOWER(COALESCE({_admin_col_expr(cfg, c)}::text,'')) LIKE %(s)s" for c in search_cols) + ")")
    for i, (col, spec) in enumerate((filters or {}).items()):
        mode = "exact"
        val = spec
        if isinstance(spec, dict):
            val = spec.get("value")
            mode = spec.get("mode") or "exact"
        if not str(val or "").strip():
            continue
        key = f"f{i}"
        params[key] = f"%{str(val).lower()}%" if mode == "contains" else str(val)
        if mode == "contains":
            where.append(f"LOWER(COALESCE({_admin_col_expr(cfg, col)}::text,'')) LIKE %({key})s")
        else:
            where.append(f"COALESCE({_admin_col_expr(cfg, col)}::text,'') = %({key})s")
    if where:
        sql += " WHERE " + " AND ".join(where)
    order_col = next((c for c in cfg.get("default_cols", []) if c in select_cols and c not in display_exprs), pk)
    sql += f" ORDER BY {_safe_ident(order_col)} NULLS LAST LIMIT %(lim)s"
    return _q(sql, params)


def _ensure_audit_log():
    """Create crm_audit_log table if not exists — idempotent."""
    if st.session_state.get("_crm_audit_log_ready"):
        return
    _w("""
        CREATE TABLE IF NOT EXISTS crm_audit_log (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            changed_at  TIMESTAMPTZ DEFAULT NOW(),
            changed_by  TEXT DEFAULT 'admin',
            action      TEXT NOT NULL,          -- UPDATE / INSERT
            tbl         TEXT NOT NULL,
            row_id      TEXT,
            col_name    TEXT,
            old_value   TEXT,
            new_value   TEXT,
            entity_name TEXT                    -- human-readable label e.g. party_name
        )
    """)
    _w("CREATE INDEX IF NOT EXISTS idx_cal_tbl_row ON crm_audit_log(tbl, row_id, changed_at DESC)")
    _w("CREATE INDEX IF NOT EXISTS idx_cal_changed  ON crm_audit_log(changed_at DESC)")
    st.session_state["_crm_audit_log_ready"] = True


def _write_audit_log(action: str, table: str, row_id: str,
                     changes: Dict, old_values: Dict = None,
                     entity_name: str = "", changed_by: str = "admin") -> None:
    """Write one audit log row per changed column."""
    try:
        _ensure_audit_log()
        for col, new_val in changes.items():
            old_val = (old_values or {}).get(col)
            if str(old_val or "") == str(new_val or ""):
                continue  # skip unchanged
            _w("""
                INSERT INTO crm_audit_log
                    (action, tbl, row_id, col_name, old_value, new_value, entity_name, changed_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (action, table, str(row_id), col,
                  str(old_val) if old_val is not None else None,
                  str(new_val) if new_val is not None else None,
                  entity_name or "", changed_by))
    except Exception as e:
        pass  # audit log failure must never block the actual save


def _fetch_audit_log(table: str = None, row_id: str = None,
                     limit: int = 100) -> List[Dict]:
    where, params = [], []
    if table:
        where.append("tbl = %s"); params.append(table)
    if row_id:
        where.append("row_id = %s"); params.append(row_id)
    sql = "SELECT * FROM crm_audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY changed_at DESC LIMIT %s"
    params.append(limit)
    return _q(sql, params) or []


def _revert_audit_entry(log_id: str) -> Optional[str]:
    """Revert a single audit log entry by restoring old_value."""
    rows = _q("SELECT * FROM crm_audit_log WHERE id = %s::uuid", (log_id,))
    if not rows:
        return "Log entry not found."
    r = rows[0]
    if r.get("action") == "INSERT":
        return "Insert revert not supported — deactivate the row instead."
    if r.get("old_value") is None:
        return "No previous value recorded — cannot revert."
    try:
        col_types = {r["col_name"]: "text"}
        err = _admin_db_update_row(
            str(r["tbl"]), "id", str(r["row_id"]),
            {str(r["col_name"]): r["old_value"]}, col_types
        )
        if err:
            return str(err)
        # Log the revert itself
        _write_audit_log(
            "REVERT", str(r["tbl"]), str(r["row_id"]),
            {str(r["col_name"]): r["old_value"]},
            {str(r["col_name"]): r["new_value"]},
            entity_name=str(r.get("entity_name") or ""),
            changed_by="admin (revert)"
        )
        return None
    except Exception as e:
        return str(e)


def _admin_db_update_row(table: str, pk: str, row_id, changes: Dict, col_types: Dict,
                          old_values: Dict = None, entity_name: str = "") -> Optional[str]:
    if not changes:
        return None
    sets = []
    params = {"id": row_id}
    for i, (col, value) in enumerate(changes.items()):
        key = f"v{i}"
        sets.append(f"{_safe_ident(col)}=%({key})s")
        params[key] = _coerce_editor_value(value, col_types.get(col, "text"))
    sql = f"UPDATE {_safe_ident(table)} SET " + ", ".join(sets)
    if "updated_at" in col_types and "updated_at" not in changes:
        sql += ", updated_at=NOW()"
    sql += f" WHERE {_safe_ident(pk)}=%(id)s::uuid"
    err = _w(sql, params)
    if not err:
        _write_audit_log("UPDATE", table, str(row_id), changes,
                         old_values or {}, entity_name)
    return err


def _admin_db_insert_row(table: str, values: Dict, col_types: Dict,
                          entity_name: str = "") -> Optional[str]:
    clean = {
        k: v for k, v in (values or {}).items()
        if k in col_types and str(v or "").strip() != ""
    }
    if not clean:
        return "No values entered for new row."
    if "id" in col_types and not clean.get("id"):
        clean["id"] = str(uuid.uuid4())
    row_id = clean.get("id", "new")
    cols = list(clean.keys())
    params = {}
    placeholders = []
    for i, col in enumerate(cols):
        key = f"v{i}"
        params[key] = _coerce_editor_value(clean[col], col_types.get(col, "text"))
        placeholders.append(f"%({key})s" + ("::uuid" if col == "id" else ""))
    sql = (
        f"INSERT INTO {_safe_ident(table)} ("
        + ", ".join(_safe_ident(c) for c in cols)
        + ") VALUES ("
        + ", ".join(placeholders)
        + ")"
    )
    err = _w(sql, params)
    if not err:
        _write_audit_log("INSERT", table, str(row_id), clean, {}, entity_name)
    return err


def _table_columns_set(table: str) -> set:
    return {r["column_name"] for r in _db_columns(table)}


def _cascade_party_name(row_id: str, old_name: str, new_name: str) -> Dict:
    """Optional historical rename for party/customer display names."""
    result = {"updated": 0, "errors": []}
    if not row_id or not old_name or old_name == new_name:
        return result
    updates = [
        ("orders", "party_name", "party_id"),
        ("orders", "customer_name", "party_id"),
        ("challans", "party_name", "party_id"),
        ("invoices", "party_name", "party_id"),
        ("payments", "party_name", "party_id"),
        ("party_ledger", "party_name", "party_id"),
    ]
    for table, name_col, id_col in updates:
        try:
            cols = _table_columns_set(table)
            if name_col not in cols or id_col not in cols:
                continue
            _w(
                f"UPDATE {_safe_ident(table)} SET {_safe_ident(name_col)}=%(new)s "
                f"WHERE {_safe_ident(id_col)}=%(id)s::uuid",
                {"new": new_name, "id": row_id},
            )
            result["updated"] += 1
        except Exception:
            result["errors"].append(table)
    return result


def _render_admin_db_editor():
    st.markdown("### 🧠 Admin DB Review / Bulk Edit")
    st.caption("Practical correction desk for masters. Search, tick rows, bulk-set one column, add missing master rows, and protect GST/legal history.")

    entity = st.selectbox("Reference", list(ADMIN_DB_ENTITIES.keys()), key="crm_db_entity")
    cfg = ADMIN_DB_ENTITIES[entity]
    table = cfg["table"]
    pk = cfg["pk"]
    fixed_filter = (cfg.get("fixed_filter_sql") or "").strip()
    db_cols = _db_columns(table)

    # ── Table not found — try fallback names ──────────────────────────────────
    if not db_cols:
        fallbacks = {"inventory_stock": ["product_stock","blank_inventory","stock_items"]}
        for alt in fallbacks.get(table, []):
            db_cols = _db_columns(alt)
            if db_cols:
                table = alt
                break
    if not db_cols:
        st.error(f"Table `{table}` not found in DB. Check that this module's schema has been migrated.")
        st.caption("Tables tried: " + table + " + fallbacks")
        return

    col_types = {r["column_name"]: r["data_type"] for r in db_cols}
    all_cols = [r["column_name"] for r in db_cols]
    display_exprs = cfg.get("display_exprs") or {}
    display_cols = list(display_exprs.keys())
    real_editable_cols = [c for c in cfg.get("editable", []) if c in all_cols and c != pk]
    editable_cols = display_cols + real_editable_cols
    default_cols = [c for c in cfg.get("default_cols", []) if c in editable_cols]
    help_map = cfg.get("help", {})
    label_map = cfg.get("labels", {})

    t1, t2 = st.columns([3, 1])
    search = t1.text_input(
        "🔍 Search / jump",
        key=f"crm_db_search_{table}",
        placeholder="party name, mobile, GSTIN, product, barcode, SKU, doc preference…",
    )
    limit = t2.number_input("Rows", min_value=5, max_value=500, value=50, step=25, key=f"crm_db_limit_{table}")

    if fixed_filter:
        st.caption(f"Filtered view: showing only {entity}")
    if cfg.get("scan_note"):
        st.info(cfg["scan_note"])

    c1, c2 = st.columns([3, 1])
    show_all_cols = c2.checkbox(
        "Show all",
        value=False,
        key=f"crm_db_show_all_{table}_{entity}",
        help="Show all editable columns without opening the column dropdown.",
    )
    chosen_cols = c1.multiselect(
        "Columns to show / edit",
        editable_cols,
        default=editable_cols if show_all_cols else (default_cols or editable_cols[:8]),
        key=f"crm_db_cols_{table}",
        format_func=lambda c: label_map.get(c, c),
        help="Select which columns to display and edit in the grid below.",
    )
    quick_col_text = c2.text_input(
        "Quick-add column",
        value="",
        key=f"crm_db_quick_col_{table}_{entity}",
        placeholder="type column name",
        help="Type a column name or part of it. This avoids long dropdown collapse.",
    )
    quick_col = ""
    if quick_col_text.strip():
        q = quick_col_text.strip().lower()
        quick_col = next((c for c in editable_cols if c.lower() == q or label_map.get(c, "").lower() == q), "")
        if not quick_col:
            matches = [c for c in editable_cols if q in c.lower() or q in label_map.get(c, "").lower()]
            if len(matches) == 1:
                quick_col = matches[0]
            elif matches:
                st.caption("Matches: " + ", ".join(label_map.get(m, m) for m in matches[:12]))
    if quick_col and quick_col not in chosen_cols:
        chosen_cols = chosen_cols + [quick_col]

    filter_cols = [c for c in cfg.get("filter_cols", []) if c in editable_cols]
    active_filters = {}
    if filter_cols:
        with st.expander("🔎 Excel-style filters", expanded=True):
            fcols = st.columns(min(4, max(1, len(filter_cols))))
            for i, fcol in enumerate(filter_cols):
                vals = _admin_db_distinct_values(cfg, fcol)
                if len(vals) <= 80:
                    picked = fcols[i % len(fcols)].selectbox(
                        fcol,
                        [""] + vals,
                        key=f"crm_db_filter_{table}_{entity}_{fcol}",
                    )
                else:
                    picked = fcols[i % len(fcols)].text_input(
                        f"{fcol} contains",
                        key=f"crm_db_filter_{table}_{entity}_{fcol}",
                    )
                if picked:
                    active_filters[fcol] = {"value": picked, "mode": "exact" if len(vals) <= 80 else "contains"}

    # Column help tooltips
    if help_map and chosen_cols:
        tips = {c: v for c, v in help_map.items() if c in chosen_cols}
        if tips:
            with st.expander("💡 Column hints", expanded=False):
                for col, tip in tips.items():
                    st.caption(f"`{col}` — {tip}")

    with st.expander("📋 All DB columns for this table", expanded=False):
        st.dataframe(
            [{"column": r["column_name"], "type": r["data_type"],
              "nullable": r.get("is_nullable",""), "default": r.get("column_default","")}
             for r in db_cols],
            use_container_width=True, hide_index=True,
        )

    with st.expander("➕ Add new master row", expanded=False):
        st.warning("Add only true master data — parties, products, services. Transactions belong in their own screens.")
        add_cols = st.multiselect(
            "Fields for new row",
            real_editable_cols,
            default=[c for c in default_cols if c in real_editable_cols][:8],
            key=f"crm_db_add_cols_{table}_{entity}",
        )
        new_values = {}
        if add_cols:
            add_grid = st.columns(2)
            for i, col in enumerate(add_cols):
                opts = cfg.get("dropdowns", {}).get(col)
                tip  = help_map.get(col, "")
                label = f"{col} — {tip}" if tip else col
                if opts:
                    new_values[col] = add_grid[i % 2].selectbox(label, opts, key=f"crm_db_new_{table}_{entity}_{col}")
                else:
                    new_values[col] = add_grid[i % 2].text_input(label, key=f"crm_db_new_{table}_{entity}_{col}")
        add_confirm = st.checkbox(
            "I confirm this new master row is required",
            key=f"crm_db_add_confirm_{table}_{entity}",
        )
        if st.button("➕ Save New Master Row", key=f"crm_db_add_btn_{table}_{entity}", use_container_width=True):
            if not add_confirm:
                st.error("Tick confirmation before adding a master row.")
            else:
                err = _admin_db_insert_row(table, new_values, col_types)
                if err:
                    st.error(err)
                else:
                    st.success("New master row saved.")
                    _bust()
                    st.rerun()

    if not chosen_cols:
        st.info("Select at least one column.")
        return
    rows = _admin_db_fetch_rows(cfg, chosen_cols, search, int(limit), active_filters)
    if not rows:
        st.info("No matching rows. Try a different search term or increase Rows limit.")
        return

    import pandas as pd
    df = pd.DataFrame(rows)
    display_cols = [pk] + [c for c in chosen_cols if c in df.columns]
    df = df[display_cols]
    df.insert(0, "Apply", False)

    column_config = {
        "Apply": st.column_config.CheckboxColumn("✅", help="Tick rows for bulk-set."),
        pk: st.column_config.TextColumn(pk, disabled=True),
    }
    for col in display_cols:
        if col in df.columns:
            column_config[col] = st.column_config.TextColumn(label_map.get(col, col), disabled=True)
    for col, options in cfg.get("dropdowns", {}).items():
        if col in df.columns:
            column_config[col] = st.column_config.SelectboxColumn(
                label_map.get(col, col), options=options, help=help_map.get(col, ""))
    for col in df.columns:
        if col not in column_config and col != "Apply":
            column_config[col] = st.column_config.TextColumn(label_map.get(col, col), help=help_map.get(col, ""))

    risky_cols    = [c for c in chosen_cols if c in cfg.get("tax", [])]
    identity_cols = [c for c in chosen_cols if c in cfg.get("identity", [])]

    if risky_cols:
        st.error(
            f"🔴 TAX / GST / COMPLIANCE FIELDS SELECTED: {', '.join(risky_cols)}\n\n"
            "Changing these can break GST filing, GSTR reports, portal-matched documents "
            "and old statutory records. Changes apply to the master row only — "
            "old documents are NOT updated automatically."
        )
    if identity_cols:
        st.warning(
            f"⚠️ Identity fields selected: {', '.join(identity_cols)}. "
            "Use cascade option below only for spelling correction of the same entity."
        )

    with st.expander("⚡ Bulk fill — set one column for all ticked rows", expanded=True):
        b1, b2 = st.columns([1, 2])
        bulk_col = b1.selectbox(
            "Column to bulk-set",
            [""] + [c for c in chosen_cols if c in real_editable_cols],
            key=f"crm_db_bulk_col_{table}_{entity}",
            help="Choose a column then set the value. Applies to all ticked rows.",
        )
        bulk_value = ""
        if bulk_col:
            opts = cfg.get("dropdowns", {}).get(bulk_col)
            tip  = help_map.get(bulk_col, "")
            if opts:
                bulk_value = b2.selectbox(
                    f"Value{' — ' + tip if tip else ''}",
                    opts, key=f"crm_db_bulk_val_sel_{table}_{entity}_{bulk_col}")
            else:
                bulk_value = b2.text_input(
                    f"Value{' — ' + tip if tip else ''}",
                    key=f"crm_db_bulk_val_txt_{table}_{entity}_{bulk_col}")
        st.caption("Example: tick wholesale parties → set `doc_preference` = C → bulk-set.")

    edited = st.data_editor(
        df,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        key=f"crm_db_editor_{table}_{'_'.join(chosen_cols)}",
        column_config=column_config,
    )

    # ── Excel export of current view ──────────────────────────────────────────
    _excel_download(
        rows,
        filename=f"{entity.replace(' ','_').replace('/','_')}_{search or 'all'}.xlsx",
        label=f"⬇ Export {len(rows)} rows to Excel",
    )

    cascade_names = False
    if table == "parties" and "party_name" in chosen_cols:
        cascade_names = st.checkbox(
            "Also update old orders/challans/invoices/payments/party ledger with changed party name",
            value=False,
            key="crm_db_cascade_party_name",
            help="Use only for spelling correction of same party. Do not use for GST/legal entity change.",
        )
        st.error(
            "🔴 If GSTIN/legal entity changed, do NOT cascade old documents. Old GST documents should remain exactly as filed."
        )

    confirm = st.checkbox(
        "I understand the impact and want to save these DB changes",
        key=f"crm_db_confirm_{table}_{entity}",
    )

    a1, a2 = st.columns(2)
    if a1.button("⚡ Bulk Set Ticked Rows", use_container_width=True, key=f"crm_db_bulk_save_{table}_{entity}"):
        if not confirm:
            st.error("Tick confirmation before saving.")
            return
        if not bulk_col:
            st.error("Choose a column to bulk-set.")
            return
        if bulk_col in cfg.get("tax", []):
            st.error("🔴 GST/tax/legal fields are blocked from bulk-set. Edit one row at a time after review.")
            return
        selected = [r for r in edited.to_dict("records") if bool(r.get("Apply"))]
        if not selected:
            st.error("Tick at least one row in the Apply column.")
            return
        saved = 0
        errors = []
        old_by_id = {str(r[pk]): r for r in rows}
        for rec in selected:
            row_id = str(rec.get(pk) or "")
            old = old_by_id.get(row_id, {})
            e_name = str(old.get(cfg["identity"][0], row_id)) if cfg.get("identity") else row_id
            err = _admin_db_update_row(table, pk, row_id, {bulk_col: bulk_value},
                                        col_types, old, e_name)
            if err:
                errors.append(f"{row_id}: {err}")
            else:
                saved += 1
        for e in errors[:10]:
            st.error(e)
        if saved:
            st.success(f"Bulk-updated {saved} row(s). Changes logged to Audit Log.")
            _bust()
            st.rerun()

    if a2.button("💾 Save Edited Cells", type="primary", use_container_width=True, key=f"crm_db_save_{table}_{entity}"):
        if not confirm:
            st.error("Tick confirmation before saving.")
            return
        old_by_id = {str(r[pk]): r for r in rows}
        saved = 0
        errors = []
        for rec in edited.to_dict("records"):
            row_id = str(rec.get(pk) or "")
            old = old_by_id.get(row_id) or {}
            e_name = str(old.get(cfg["identity"][0], row_id)) if cfg.get("identity") else row_id
            changes = {}
            for col in chosen_cols:
                if col == "Apply":
                    continue
                if col not in real_editable_cols:
                    continue
                if col not in rec:
                    continue
                old_val = old.get(col)
                new_val = rec.get(col)
                if str(old_val or "") != str(new_val or ""):
                    changes[col] = new_val
            if not changes:
                continue
            err = _admin_db_update_row(table, pk, row_id, changes, col_types, old, e_name)
            if err:
                errors.append(f"{row_id}: {err}")
                continue
            if table == "parties" and cascade_names and "party_name" in changes:
                cascade = _cascade_party_name(row_id, str(old.get("party_name") or ""), str(changes.get("party_name") or ""))
                if cascade.get("errors"):
                    st.warning(f"Party name saved, but cascade skipped/failed in: {', '.join(cascade['errors'])}")
            saved += 1
        if errors:
            for e in errors[:10]:
                st.error(e)
        if saved:
            st.success(f"Saved {saved} row(s). Changes logged to Audit Log.")
            _bust()
            st.rerun()

    st.markdown("---")

    # ── Audit Log ─────────────────────────────────────────────────────────────
    with st.expander("🕐 Audit Log — recent changes to this table", expanded=False):
        _ensure_audit_log()
        log_rows = _fetch_audit_log(table=table, limit=50)
        if not log_rows:
            st.caption("No changes recorded yet for this table.")
        else:
            import pandas as pd
            log_df = pd.DataFrame(log_rows)[[
                "changed_at","action","entity_name","col_name",
                "old_value","new_value","changed_by"
            ]]
            log_df["changed_at"] = log_df["changed_at"].astype(str).str[:16]
            st.dataframe(log_df, use_container_width=True, hide_index=True)
            _excel_download(log_rows, f"audit_{table}.xlsx", "⬇ Export Audit Log")

            st.markdown("**↩ Revert a change**")
            revert_id = st.text_input(
                "Paste log entry ID to revert",
                key=f"revert_id_{table}",
                placeholder="UUID from audit log id column",
                help="Opens the row ID from the audit log and restores old_value"
            )
            if st.button("↩ Revert this change", key=f"revert_btn_{table}",
                         type="secondary", disabled=not revert_id.strip()):
                err = _revert_audit_entry(revert_id.strip())
                if err:
                    st.error(f"Revert failed: {err}")
                else:
                    st.success("✅ Value restored to previous. Check table above.")
                    _bust()
                    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE AUDIT LOG TAB (shown as separate CRM tab)
# ─────────────────────────────────────────────────────────────────────────────

def _tab_audit_log():
    st.markdown("### 🕐 Change Audit Log")
    st.caption("Every add / edit made through Admin DB Editor is recorded here. Revert any single change.")

    _ensure_audit_log()

    f1, f2, f3 = st.columns([2, 2, 1])
    tbl_filter = f1.selectbox(
        "Table", ["All"] + sorted({cfg["table"] for cfg in ADMIN_DB_ENTITIES.values()}),
        key="al_tbl"
    )
    action_filter = f2.selectbox("Action", ["All", "UPDATE", "INSERT", "REVERT"], key="al_action")
    limit = f3.number_input("Rows", 20, 500, 100, 20, key="al_limit")

    tbl = None if tbl_filter == "All" else tbl_filter
    log_rows = _fetch_audit_log(table=tbl, limit=int(limit))

    if action_filter != "All":
        log_rows = [r for r in log_rows if r.get("action") == action_filter]

    if not log_rows:
        st.info("No changes recorded yet.")
        return

    import pandas as pd
    log_df = pd.DataFrame(log_rows)
    show_cols = [c for c in [
        "changed_at","action","tbl","entity_name","col_name",
        "old_value","new_value","changed_by","id"
    ] if c in log_df.columns]
    log_df["changed_at"] = log_df["changed_at"].astype(str).str[:16]
    st.dataframe(log_df[show_cols], use_container_width=True, hide_index=True)

    ec1, ec2 = st.columns([3, 1])
    with ec2:
        _excel_download(log_rows, "crm_audit_log.xlsx", "⬇ Export")

    st.markdown("---")
    st.markdown("**↩ Revert a single change**")
    st.caption("Copy the `id` (UUID) from the log above and paste below.")

    rc1, rc2 = st.columns([3, 1])
    revert_id = rc1.text_input(
        "Log entry ID",
        key="al_revert_id",
        placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    )
    if rc2.button("↩ Revert", type="primary", key="al_revert_btn",
                  disabled=not revert_id.strip()):
        err = _revert_audit_entry(revert_id.strip())
        if err:
            st.error(f"❌ {err}")
        else:
            st.success("✅ Value restored to previous state.")
            st.rerun()

def save_party(data: Dict):
    """
    Upsert party via INSERT ... ON CONFLICT.
    Returns (party_id, None) on success, (None, error_str) on failure.

    CRITICAL: is_active is BOOLEAN in DB. status is also BOOLEAN (legacy).
    This function ONLY writes is_active. status is never touched.
    """
    pid      = data.get("id") or str(uuid.uuid4())
    is_new   = not data.get("id")
    is_active = bool(data.get("is_active", True))

    p = {
        "pid":       pid,
        "name":      (data.get("party_name") or "").strip(),
        "ptype":     data.get("party_type", "Retail"),
        "mobile":    (data.get("mobile") or "").strip() or None,
        "alt_mob":   (data.get("alt_mobile") or "").strip() or None,
        "email":     (data.get("email") or "").strip() or None,
        "contact":   (data.get("contact_person") or "").strip() or None,
        "address":   (data.get("address") or "").strip() or None,
        "city":      (data.get("city") or "").strip() or None,
        "area":      (data.get("area") or "").strip() or None,
        "pincode":   (data.get("pincode") or "").strip() or None,
        "state_name":(data.get("state_name") or "").strip() or None,
        "state_code":(data.get("state_code") or "").strip() or None,
        "gstin":     (data.get("gstin") or "").strip().upper() or None,
        "pan_no":    (data.get("pan_no") or "").strip().upper() or None,
        "tan_no":    (data.get("tan_no") or "").strip().upper() or None,
        "cin_no":    (data.get("cin_no") or "").strip().upper() or None,
        "gst_rate":  float(data.get("gst_rate") or 0),
        "cl":        float(data.get("credit_limit") or 0),
        "cd":        int(data.get("credit_days") or 0),
        "ob":        float(data.get("opening_balance") or 0),
        "bt":        data.get("balance_type", "Dr"),
        "tally_grp": (data.get("tally_group") or "").strip() or None,
        "notes":     (data.get("notes") or "").strip() or None,
        "is_active": is_active,
        "bp":        (data.get("billing_preference") or "CHALLAN").upper(),
        "pcid":      (data.get("preferred_courier_provider_id") or "").strip() or None,
        "pcname":    (data.get("preferred_courier_name") or "").strip() or None,
    }

    sql = """
        INSERT INTO parties (
            id, party_name, party_type, mobile, alt_mobile, email,
            contact_person, address, city, area, pincode,
            state_name, state_code,
            gstin, pan_no, tan_no, cin_no, gst_rate,
            credit_limit, credit_days, opening_balance, balance_type,
            tally_group, notes, is_active, billing_preference,
            preferred_courier_provider_id, preferred_courier_name, created_at
        ) VALUES (
            %(pid)s, %(name)s, %(ptype)s, %(mobile)s, %(alt_mob)s, %(email)s,
            %(contact)s, %(address)s, %(city)s, %(area)s, %(pincode)s,
            %(state_name)s, %(state_code)s,
            %(gstin)s, %(pan_no)s, %(tan_no)s, %(cin_no)s, %(gst_rate)s,
            %(cl)s, %(cd)s, %(ob)s, %(bt)s,
            %(tally_grp)s, %(notes)s, %(is_active)s, %(bp)s,
            NULLIF(%(pcid)s,'')::uuid, %(pcname)s, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            party_name=%(name)s, party_type=%(ptype)s, mobile=%(mobile)s,
            alt_mobile=%(alt_mob)s, email=%(email)s,
            contact_person=%(contact)s, address=%(address)s,
            city=%(city)s, area=%(area)s, pincode=%(pincode)s,
            state_name=%(state_name)s, state_code=%(state_code)s,
            gstin=%(gstin)s, pan_no=%(pan_no)s, tan_no=%(tan_no)s,
            cin_no=%(cin_no)s, gst_rate=%(gst_rate)s,
            credit_limit=%(cl)s, credit_days=%(cd)s,
            opening_balance=%(ob)s, balance_type=%(bt)s,
            tally_group=%(tally_grp)s, notes=%(notes)s, is_active=%(is_active)s,
            billing_preference=%(bp)s,
            preferred_courier_provider_id=NULLIF(%(pcid)s,'')::uuid,
            preferred_courier_name=%(pcname)s
    """
    err = _w(sql, p)
    if err:
        # If new column doesn't exist yet (ALTER TABLE might not have run), retry minimal
        if "column" in err.lower() and "does not exist" in err.lower():
            sql_min = """
                INSERT INTO parties(id, party_name, party_type, mobile, address,
                    city, area, is_active, created_at)
                VALUES(%(pid)s,%(name)s,%(ptype)s,%(mobile)s,%(address)s,
                    %(city)s,%(area)s,%(is_active)s,NOW())
                ON CONFLICT (id) DO UPDATE SET
                    party_name=%(name)s, party_type=%(ptype)s, mobile=%(mobile)s,
                    address=%(address)s, city=%(city)s, area=%(area)s,
                    is_active=%(is_active)s
            """
            err = _w(sql_min, p)
        if err:
            return None, err
    _bust()
    return pid, None


def save_lead(data):
    lid = data.get("id") or str(uuid.uuid4())
    err = _w(
        "INSERT INTO crm_leads(id,party_id,lead_source,stage,notes,assigned_to,"
        "potential_value,created_at,updated_at) "
        "VALUES(%(id)s,%(pid)s,%(src)s,%(stage)s,%(notes)s,%(asgn)s,%(val)s,NOW(),NOW()) "
        "ON CONFLICT (id) DO UPDATE SET stage=%(stage)s,notes=%(notes)s,"
        "lead_source=%(src)s,assigned_to=%(asgn)s,potential_value=%(val)s,updated_at=NOW()",
        {"id": lid, "pid": data.get("party_id"), "src": data.get("lead_source",""),
         "stage": data.get("stage","NEW"), "notes": data.get("notes",""),
         "asgn": data.get("assigned_to",""), "val": float(data.get("potential_value") or 0)})
    if err is None:
        _bust(); return lid, None
    return None, err


def save_followup(data):
    fid = str(uuid.uuid4())
    err = _w(
        "INSERT INTO crm_followups(id,party_id,lead_id,followup_type,due_date,"
        "notes,created_by,created_at) "
        "VALUES(%(id)s,%(pid)s,%(lid)s,%(type)s,%(due)s,%(notes)s,%(by)s,NOW())",
        {"id": fid, "pid": data.get("party_id"), "lid": data.get("lead_id"),
         "type": data.get("followup_type","CALL"), "due": data.get("due_date"),
         "notes": data.get("notes",""), "by": data.get("created_by","system")})
    if err is None:
        _bust(); return fid, None
    return None, err


def mark_followup_done(fid):
    err = _w("UPDATE crm_followups SET done=TRUE,done_at=NOW() WHERE id=%(id)s", {"id": fid})
    if err is None: _bust()
    return err


def log_touchpoint(pid, tp_type, summary, user):
    return _w("INSERT INTO crm_touchpoints(id,party_id,type,summary,created_by,created_at) "
              "VALUES(%(id)s,%(pid)s,%(type)s,%(sum)s,%(by)s,NOW())",
              {"id": str(uuid.uuid4()), "pid": pid, "type": tp_type,
               "sum": summary, "by": user})


def _user():
    try:
        from modules.security.roles import current_user_name
        return current_user_name()
    except Exception:
        return "system"


# ─────────────────────────────────────────────────────────────────────────────
# PARTY FORM — full Tally/ERP grade
# ─────────────────────────────────────────────────────────────────────────────

def _party_form(key: str, existing: Dict = None, compact: bool = False):
    """
    Renders the party form. Returns (submitted:bool, data:dict).
    compact=True for the quick-add supplier widget.
    All internal widget keys are scoped to `key` to prevent
    DuplicateWidgetID when multiple forms render simultaneously
    (Streamlit renders ALL tabs on every run, not just the active one).
    """
    ex  = existing or {}
    _k  = key.replace(" ", "_")   # safe prefix for widget keys

    with st.form(key):
        # ── Basic Info ───────────────────────────────────────────────────────
        st.markdown("**Basic Information**")
        b1, b2, b3 = st.columns(3)
        with b1:
            pname  = st.text_input("Party Name *", value=ex.get("party_name",""))
            mobile = st.text_input("Mobile",        value=ex.get("mobile","") or "")
            email  = st.text_input("Email",         value=ex.get("email","") or "")
        with b2:
            idx    = PARTY_TYPES.index(ex["party_type"]) if ex.get("party_type") in PARTY_TYPES else 0
            ptype  = st.selectbox("Party Type", PARTY_TYPES, index=idx)
            alt_mob = st.text_input("Alt Mobile",  value=ex.get("alt_mobile","") or "")
            contact = st.text_input("Contact Person", value=ex.get("contact_person","") or "")
        with b3:
            is_active = st.checkbox("Active", value=bool(ex.get("is_active", True)))
            tally_grp = st.text_input("Tally Ledger Group",
                                       value=ex.get("tally_group","") or "",
                                       placeholder="e.g. Sundry Debtors")
            if not compact:
                notes = st.text_area("Internal Notes", value=ex.get("notes","") or "", height=68)
            else:
                notes = ""

        if not compact:
            st.markdown("**Address**")
            a1, a2 = st.columns(2)
            with a1:
                address = st.text_area("Billing Address", value=ex.get("address","") or "", height=70)
                city    = st.text_input("City",    value=ex.get("city","") or "", key=f"party_city_{_k}")
                area    = st.text_input("Area",    value=ex.get("area","") or "", key=f"party_area_{_k}")
            with a2:
                pincode = st.text_input("Pincode", value=ex.get("pincode","") or "")
                state_names = [""] + INDIAN_STATES
                cur_state   = ex.get("state_name","") or ""
                sidx = state_names.index(cur_state) if cur_state in state_names else 0
                state_name  = st.selectbox("State", state_names, index=sidx)
                state_code  = st.text_input("State Code (GST)", value=ex.get("state_code","") or "",
                                             placeholder="e.g. 27 for Maharashtra",
                                             max_chars=2)
        else:
            address = st.text_area("Address", value=ex.get("address","") or "", height=60)
            city    = st.text_input("City",    value=ex.get("city","") or "", key=f"party_city_{_k}")
            area    = st.text_input("Area",    value=ex.get("area","") or "", key=f"party_area_{_k}")
            pincode = state_name = state_code = ""

        # ── Tax / Compliance ─────────────────────────────────────────────────
        st.markdown("**GST & Compliance**")
        t1, t2, t3, t4 = st.columns(4)
        with t1:
            gstin   = st.text_input("GSTIN (15 digits)", value=ex.get("gstin","") or "",
                                     max_chars=15, placeholder="27AABCU9603R1ZX")
        with t2:
            pan_no  = st.text_input("PAN (10 chars)",    value=ex.get("pan_no","") or "",
                                     max_chars=10, placeholder="AABCU9603R")
        with t3:
            tan_no  = st.text_input("TAN",               value=ex.get("tan_no","") or "",
                                     max_chars=10)
        with t4:
            gst_idx  = GST_RATES.index(str(int(float(ex.get("gst_rate",0) or 0)))) \
                       if str(int(float(ex.get("gst_rate",0) or 0))) in GST_RATES else 0
            gst_rate = st.selectbox("GST Rate %", GST_RATES, index=gst_idx)

        if not compact:
            cin_no = st.text_input("CIN (Companies)", value=ex.get("cin_no","") or "",
                                    max_chars=21, placeholder="U12345MH2000PTC123456")
        else:
            cin_no = ""

        # ── Credit Terms ─────────────────────────────────────────────────────
        st.markdown("**Credit Terms**")
        cr1, cr2, cr3, cr4 = st.columns(4)
        with cr1:
            credit_limit = st.number_input("Credit Limit (₹)",
                                            value=float(ex.get("credit_limit",0) or 0),
                                            min_value=0.0, step=1000.0)
        with cr2:
            credit_days  = st.number_input("Credit Days",
                                            value=int(ex.get("credit_days",0) or 0),
                                            min_value=0, step=5)
        with cr3:
            opening_bal  = st.number_input("Opening Balance (₹)",
                                            value=float(ex.get("opening_balance",0) or 0),
                                            step=100.0)
        with cr4:
            bt_idx = 0 if ex.get("balance_type","Dr") == "Dr" else 1
            bal_type = st.selectbox("Balance Type", ["Dr","Cr"], index=bt_idx)

        # ── Billing & Document Settings ──────────────────────────────────────
        if not compact:
            st.markdown("**Billing Settings**")
            bp1, bp2 = st.columns(2)
            with bp1:
                _bp_opts   = ["CHALLAN", "DIRECT_INVOICE"]
                _bp_labels = {
                    "CHALLAN":        "📋 Challan first, invoice later (default)",
                    "DIRECT_INVOICE": "🧾 Direct Invoice — wholesale only",
                }
                _bp_cur = (ex.get("billing_preference") or "CHALLAN").upper()
                if _bp_cur not in _bp_opts:
                    _bp_cur = "CHALLAN"
                billing_pref = st.selectbox(
                    "Billing Preference",
                    _bp_opts,
                    index=_bp_opts.index(_bp_cur),
                    format_func=lambda x: _bp_labels.get(x, x),
                    help="CHALLAN: create challan from order, convert to invoice later.\n"
                         "DIRECT_INVOICE: wholesale parties only — challan + invoice created in one step.",
                    key=f"billing_pref_{_k}",
                )
            try:
                from modules.backoffice.service_master import fetch_providers
                _couriers = fetch_providers("COURIER", active_only=True)
            except Exception:
                _couriers = []
            _pc_ids = [""] + [str(c["id"]) for c in _couriers]
            _pc_labels = ["— No preferred courier —"] + [
                f"{c['provider_name']} · {'GST' if c.get('gst_registered') else 'Non-GST'}"
                for c in _couriers
            ]
            _pc_cur = str(ex.get("preferred_courier_provider_id") or "")
            _pc_idx = _pc_ids.index(_pc_cur) if _pc_cur in _pc_ids else 0
            _pc_sel = st.selectbox(
                "Preferred Courier",
                range(len(_pc_labels)),
                index=_pc_idx,
                format_func=lambda i: _pc_labels[i],
                help="Default courier shown in Dispatch. Staff can change it with warning.",
                key=f"preferred_courier_{_k}",
            )
            preferred_courier_provider_id = _pc_ids[int(_pc_sel)]
            preferred_courier_name = ""
            if preferred_courier_provider_id:
                preferred_courier_name = next(
                    (c["provider_name"] for c in _couriers if str(c["id"]) == preferred_courier_provider_id),
                    "",
                )
            with bp2:
                st.markdown(
                    "<div style='background:#0f172a;border:1px solid #1e293b;"
                    "border-radius:6px;padding:8px 12px;margin-top:26px'>"
                    "<div style='color:#94a3b8;font-size:0.72rem'>"
                    "Retail parties always use Challan regardless of this setting. "
                    "Set DIRECT_INVOICE only for wholesale parties who want "
                    "immediate invoicing without a separate challan step."
                    "</div></div>",
                    unsafe_allow_html=True,
                )
        else:
            billing_pref = ex.get("billing_preference") or "CHALLAN"
            preferred_courier_provider_id = ex.get("preferred_courier_provider_id") or ""
            preferred_courier_name = ex.get("preferred_courier_name") or ""

        submitted = st.form_submit_button("💾 Save", type="primary", use_container_width=True)

    if submitted:
        return True, {
            "id":             ex.get("id"),
            "party_name":     pname,
            "party_type":     ptype,
            "mobile":         mobile,
            "alt_mobile":     alt_mob,
            "email":          email,
            "contact_person": contact,
            "address":        address,
            "city":           city,
            "area":           area,
            "pincode":        pincode,
            "state_name":     state_name,
            "state_code":     state_code,
            "gstin":          gstin,
            "pan_no":         pan_no,
            "tan_no":         tan_no,
            "cin_no":         cin_no,
            "gst_rate":       gst_rate,
            "credit_limit":   credit_limit,
            "credit_days":    credit_days,
            "opening_balance":opening_bal,
            "balance_type":   bal_type,
            "tally_group":    tally_grp,
            "notes":          notes,
            "is_active":      is_active,
            "billing_preference": billing_pref,
            "preferred_courier_provider_id": preferred_courier_provider_id,
            "preferred_courier_name": preferred_courier_name,
        }
    return False, {}


def _show_save_error(err):
    st.error(f"❌ {err}")
    if "does not exist" in err:
        st.info("💡 Column not found — ALTER TABLE may still be running. Try again in a moment.")
    elif "duplicate" in err.lower() or "unique" in err.lower():
        st.warning("A party with this name/mobile already exists.")
    elif "boolean" in err.lower():
        st.error("🔴 Boolean type error — please report this to admin. is_active must be True/False.")


# ─────────────────────────────────────────────────────────────────────────────
# INLINE SUPPLIER QUICK-ADD
# ─────────────────────────────────────────────────────────────────────────────

def render_supplier_quick_add(on_success=None):
    """Compact supplier form for embedding in Backoffice. Returns new party_id."""
    st.markdown("#### ➕ Add New Supplier")
    submitted, data = _party_form("crm_sup_quick", compact=True)
    if submitted:
        if not data.get("party_name","").strip():
            st.error("Supplier name is required.")
            return None
        data["party_type"] = "Supplier"
        pid, err = save_party(data)
        if pid:
            st.success(f"✅ **{data['party_name']}** added as Supplier")
            log_touchpoint(pid, "NOTE", "Created via Quick Add", _user())
            if on_success:
                on_success(pid, data["party_name"])
            return pid
        else:
            _show_save_error(err)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# TAB: SUPPLIERS
# ─────────────────────────────────────────────────────────────────────────────

def _tab_suppliers():
    st.markdown("### 🏭 Supplier Manager")
    st.caption("All Supplier-type parties — required for **Order to Supplier** in Backoffice.")

    # ── Inline edit form (shown above list when editing) ─────────────────────
    editing = st.session_state.get("sup_edit")
    if editing:
        ex = {} if editing == "NEW" else (fetch_party(editing) or {})
        label = "➕ New Supplier" if editing == "NEW" else f"✏️ Edit: {ex.get('party_name','')}"
        with st.container(border=True):
            st.markdown(f"**{label}**")
            submitted, data = _party_form(f"sup_form_{editing}", existing=ex)
            if st.button("✖ Cancel", key=f"sup_cancel_{editing}"):
                st.session_state.pop("sup_edit", None)
                st.rerun()
            if submitted:
                if not data.get("party_name","").strip():
                    st.error("Supplier name is required.")
                else:
                    data["party_type"] = "Supplier"
                    pid, err = save_party(data)
                    if pid:
                        st.success(f"✅ Saved: **{data['party_name']}**")
                        st.session_state.pop("sup_edit", None)
                        st.rerun()
                    else:
                        _show_save_error(err)
        st.markdown("---")

    if not editing:
        with st.expander("➕ Add New Supplier",
                         expanded=st.session_state.get("crm_open_sup_add", False)):
            render_supplier_quick_add()
        st.markdown("---")

    suppliers = fetch_suppliers()
    if not suppliers:
        st.warning("No suppliers yet. Add one above.")
        return

    st.success(f"✅ {len(suppliers)} supplier(s) ready for Order to Supplier")
    for s in suppliers:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([4, 2, 2, 1])
            with c1:
                st.markdown(f"**{s.get('party_name','')}**")
                st.caption(f"📍 {s.get('city','') or '—'}  ·  {s.get('area','') or '—'}")
                if s.get("contact_person"):
                    st.caption(f"👤 {s['contact_person']}")
            with c2:
                if s.get("mobile"):   st.caption(f"📱 {s['mobile']}")
                if s.get("gstin"):    st.caption(f"GST: `{s['gstin']}`")
            with c3:
                if s.get("credit_limit"):
                    st.caption(f"💳 Limit: ₹{float(s['credit_limit']):,.0f}")
                if s.get("credit_days"):
                    st.caption(f"📅 {s['credit_days']} days")
            with c4:
                color = "#10b981" if s.get("is_active") else "#ef4444"
                label = "Active" if s.get("is_active") else "Inactive"
                st.markdown(
                    f"<span style='background:{color};color:#fff;padding:2px 8px;"
                    f"border-radius:8px;font-size:0.72rem'>{label}</span>",
                    unsafe_allow_html=True)
                if st.button("✏️ Edit", key=f"supedit_{s['id']}", use_container_width=True):
                    st.session_state["sup_edit"] = str(s["id"])
                    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# TAB: PARTY MASTER
# ─────────────────────────────────────────────────────────────────────────────

def _render_party_scheme_panel(party_id: str, party_name: str):
    """Show and maintain Scheme Center assignments from the party side."""
    rows = _q("""
        SELECT
            s.id::text AS scheme_id,
            s.scheme_name,
            COALESCE(s.scheme_scope,'SUPPLIER') AS scheme_scope,
            COALESCE(s.supplier_name,'') AS supplier_name,
            COALESCE(s.assignment_mode,'ALL_DEALERS') AS assignment_mode,
            s.party_id::text AS direct_party_id,
            s.starts_on::text AS starts_on,
            s.ends_on::text AS ends_on,
            COALESCE(s.active,TRUE) AS scheme_active,
            COALESCE(a.active,FALSE) AS assigned,
            (
                SELECT COUNT(*)
                FROM supplier_party_scheme_assignments ax
                WHERE ax.scheme_id=s.id
                  AND COALESCE(ax.active,TRUE)=TRUE
            ) AS assigned_count
        FROM supplier_party_schemes s
        LEFT JOIN supplier_party_scheme_assignments a
               ON a.scheme_id=s.id
              AND a.party_id=%(pid)s::uuid
        WHERE COALESCE(s.active,TRUE)=TRUE
        ORDER BY s.ends_on DESC, s.scheme_name
        LIMIT 100
    """, {"pid": party_id})
    if not rows:
        st.caption("No schemes configured yet.")
        return

    selected_scheme_ids = []
    st.caption("All-dealer schemes are included automatically. Dealer-specific ticks stay synchronized with Pricing & Discount Admin.")
    for row in rows:
        mode = (row.get("assignment_mode") or "ALL_DEALERS").upper()
        assigned_count = int(row.get("assigned_count") or 0)
        is_all_dealers = mode == "ALL_DEALERS" and not assigned_count and not row.get("direct_party_id")
        direct_match = str(row.get("direct_party_id") or "") == str(party_id)
        checked = bool(is_all_dealers or direct_match or row.get("assigned"))
        disabled = bool(is_all_dealers)
        label = (
            f"{row.get('scheme_name')} · {row.get('scheme_scope')} · "
            f"{row.get('supplier_name') or 'Any supplier'} · "
            f"{row.get('starts_on')} to {row.get('ends_on')}"
        )
        val = st.checkbox(
            label,
            value=checked,
            disabled=disabled,
            key=f"crm_party_scheme_{party_id}_{row['scheme_id']}",
        )
        if val and not disabled:
            selected_scheme_ids.append(row["scheme_id"])

    if st.button("💾 Save scheme ticks", key=f"crm_party_scheme_save_{party_id}"):
        try:
            selected = set(selected_scheme_ids)
            for row in rows:
                sid = row["scheme_id"]
                mode = (row.get("assignment_mode") or "ALL_DEALERS").upper()
                assigned_count = int(row.get("assigned_count") or 0)
                is_all_dealers = mode == "ALL_DEALERS" and not assigned_count and not row.get("direct_party_id")
                if is_all_dealers:
                    continue
                if sid in selected:
                    _w("""
                        INSERT INTO supplier_party_scheme_assignments (
                            scheme_id, party_id, party_name, starts_on, ends_on,
                            active, assigned_source, assigned_by, assigned_at, notes
                        ) VALUES (
                            %(sid)s::uuid, %(pid)s::uuid, %(pname)s,
                            %(st)s::date, %(en)s::date,
                            TRUE, 'PARTY_MASTER', COALESCE(current_user,'system'), NOW(),
                            'Saved from CRM Party Master'
                        )
                        ON CONFLICT (scheme_id, party_id) DO UPDATE
                        SET party_name=EXCLUDED.party_name,
                            starts_on=EXCLUDED.starts_on,
                            ends_on=EXCLUDED.ends_on,
                            active=TRUE,
                            assigned_source='PARTY_MASTER',
                            assigned_by=COALESCE(current_user,'system'),
                            assigned_at=NOW()
                    """, {
                        "sid": sid,
                        "pid": party_id,
                        "pname": party_name,
                        "st": row.get("starts_on"),
                        "en": row.get("ends_on"),
                    })
                    _w("""
                        UPDATE supplier_party_schemes
                        SET assignment_mode='SELECTED_DEALERS',
                            party_id=NULL,
                            party_name='',
                            updated_at=NOW()
                        WHERE id=%(sid)s::uuid
                          AND COALESCE(assignment_mode,'ALL_DEALERS') <> 'ALL_DEALERS'
                    """, {"sid": sid})
                else:
                    _w("""
                        UPDATE supplier_party_scheme_assignments
                        SET active=FALSE,
                            assigned_source='PARTY_MASTER',
                            assigned_by=COALESCE(current_user,'system'),
                            assigned_at=NOW()
                        WHERE scheme_id=%(sid)s::uuid
                          AND party_id=%(pid)s::uuid
                    """, {"sid": sid, "pid": party_id})
                    _w("""
                        UPDATE supplier_party_schemes
                        SET party_id=NULL,
                            party_name='',
                            assignment_mode='SELECTED_DEALERS',
                            updated_at=NOW()
                        WHERE id=%(sid)s::uuid
                          AND party_id=%(pid)s::uuid
                    """, {"sid": sid, "pid": party_id})
            st.success("Scheme ticks saved for this party.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not save scheme ticks: {exc}")

def _party_usage_counts(party_ids: list) -> dict:
    """Return {party_id: {orders, challans, invoices}} for the given party ids."""
    if not party_ids:
        return {}
    try:
        id_list = list({str(x) for x in party_ids if x})
        placeholders = ",".join(["%s::uuid"] * len(id_list))
        rows = _q(f"""
            SELECT p.party_id::text AS party_id,
                   COUNT(DISTINCT o.id)  FILTER (WHERE o.id IS NOT NULL) AS orders,
                   COUNT(DISTINCT ch.id) FILTER (WHERE ch.id IS NOT NULL) AS challans,
                   COUNT(DISTINCT inv.id) FILTER (WHERE inv.id IS NOT NULL) AS invoices
            FROM (SELECT unnest(ARRAY[{placeholders}]::uuid[]) AS party_id) p
            LEFT JOIN orders   o   ON o.party_id   = p.party_id
                                  AND COALESCE(o.is_deleted,FALSE) = FALSE
            LEFT JOIN challans ch  ON ch.party_id  = p.party_id
                                  AND COALESCE(ch.is_deleted,FALSE) = FALSE
            LEFT JOIN invoices inv ON inv.party_id  = p.party_id
                                  AND COALESCE(inv.is_deleted,FALSE) = FALSE
            GROUP BY p.party_id
        """, id_list) or []
        return {r["party_id"]: r for r in rows}
    except Exception:
        return {}


def _excel_download(data: list, filename: str, label: str = "⬇ Download Excel"):
    """Render a Streamlit download button that exports data as Excel."""
    if not data:
        return
    try:
        import io
        import pandas as pd
        buf = io.BytesIO()
        pd.DataFrame(data).to_excel(buf, index=False, engine="openpyxl")
        buf.seek(0)
        st.download_button(
            label=label,
            data=buf.getvalue(),
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key=f"xl_dl_{filename}_{len(data)}",
        )
    except ImportError:
        st.caption("Install openpyxl for Excel export: `pip install openpyxl`")
    except Exception as e:
        st.caption(f"Export error: {e}")


def _tab_party_master():
    st.markdown("### 🗂️ Party Master")

    # Handle redirect from Supplier tab edit button
    if st.session_state.pop("pm_edit_from_sup", False):
        pass  # pm_edit already set

    col1, col2, col3 = st.columns([2, 3, 1])
    with col1:
        type_filter = st.selectbox("Type", ["All"] + PARTY_TYPES, key="pm_type")
    with col2:
        search = st.text_input("Search name / mobile / GSTIN", key="pm_search",
                               placeholder="Type to search…")
    with col3:
        st.markdown("<div style='margin-top:1.75rem'>", unsafe_allow_html=True)
        if st.button("➕ New Party", key="pm_new", use_container_width=True):
            st.session_state["pm_edit"] = "NEW"
        st.markdown("</div>", unsafe_allow_html=True)

    # ── Edit form ────────────────────────────────────────────────────────────
    editing = st.session_state.get("pm_edit")
    if editing:
        ex = {} if editing == "NEW" else (fetch_party(editing) or {})
        label = "➕ New Party" if editing == "NEW" else f"✏️ Edit: {ex.get('party_name','')}"
        with st.container(border=True):
            st.markdown(f"**{label}**")
            submitted, data = _party_form(f"pm_form_{editing}", existing=ex)
            if st.button("✖ Cancel", key=f"pm_cancel_{editing}"):
                st.session_state.pop("pm_edit", None)
                st.rerun()
            if submitted:
                if not data.get("party_name","").strip():
                    st.error("Party name is required.")
                else:
                    pid, err = save_party(data)
                    if pid:
                        st.success(f"✅ Saved: **{data['party_name']}**")
                        st.session_state.pop("pm_edit", None)
                        st.rerun()
                    else:
                        _show_save_error(err)

    # ── List ─────────────────────────────────────────────────────────────────
    parties = fetch_parties(type_filter if type_filter != "All" else None, search)
    if not parties:
        st.info("No parties found.")
        return

    counts = {}
    for p in parties:
        counts[p.get("party_type","?")] = counts.get(p.get("party_type","?"), 0) + 1
    pcols = st.columns(min(len(counts), 6))
    for i, (t, c) in enumerate(sorted(counts.items())):
        pcols[i % len(pcols)].metric(t, c)

    # ── Excel export ─────────────────────────────────────────────────────────
    ec1, ec2 = st.columns([3, 1])
    with ec2:
        _excel_download(
            [{k: v for k, v in p.items() if k != "id"} for p in parties],
            filename=f"parties_{search or type_filter or 'all'}.xlsx",
            label="⬇ Export Excel",
        )

    st.markdown("---")

    # ── Usage counts (batch fetch for all visible parties) ───────────────────
    usage = _party_usage_counts([p["id"] for p in parties])

    for p in parties:
        pid  = str(p["id"])
        u    = usage.get(pid, {})
        n_ord  = int(u.get("orders",   0))
        n_ch   = int(u.get("challans", 0))
        n_inv  = int(u.get("invoices", 0))

        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([4, 2, 2, 2, 1])
            dot = "🟢" if p.get("is_active") else "🔴"
            with c1:
                st.markdown(f"{dot} **{p.get('party_name','')}**  "
                            f"<span style='color:#6b7280;font-size:0.78rem'>"
                            f"{p.get('party_type','')} | {p.get('city','') or '—'}</span>",
                            unsafe_allow_html=True)
                if p.get("contact_person"):
                    st.caption(f"👤 {p['contact_person']}")
                # ── Usage counter ────────────────────────────────────────
                if n_ord or n_ch or n_inv:
                    parts = []
                    if n_ord: parts.append(f"📦 {n_ord} orders")
                    if n_ch:  parts.append(f"📄 {n_ch} challans")
                    if n_inv: parts.append(f"🧾 {n_inv} invoices")
                    st.caption("  ·  ".join(parts))
                else:
                    if not p.get("is_active"):
                        st.caption("⚠️ No transactions — safe to deactivate")
            with c2:
                if p.get("mobile"):  st.caption(f"📱 {p['mobile']}")
                if p.get("email"):   st.caption(f"📧 {p['email']}")
            with c3:
                if p.get("gstin"):
                    st.caption(f"GSTIN: `{p['gstin']}`")
                if p.get("area"):
                    st.caption(f"📍 {p['area']}")
            with c4:
                if p.get("credit_limit"):
                    st.caption(f"💳 ₹{float(p['credit_limit']):,.0f} / {p.get('credit_days',0)}d")
                # ── Jump buttons ──────────────────────────────────────────
                if n_ord:
                    if st.button("📦 Orders", key=f"jump_ord_{pid}",
                                 help="Open this party's orders in Backoffice"):
                        st.session_state["bo_party_filter"]   = p.get("party_name","")
                        st.session_state["global_nav_target"] = "backoffice"
                        st.rerun()
                if n_inv or n_ch:
                    if st.button("🧾 Billing", key=f"jump_inv_{pid}",
                                 help="Open this party's invoices/challans in Billing"):
                        st.session_state["billing_party_filter"] = p.get("party_name","")
                        st.session_state["global_nav_target"]    = "billing"
                        st.rerun()
            with c5:
                if st.button("✏️", key=f"pme_{pid}"):
                    st.session_state["pm_edit"] = pid
                    st.rerun()
            scheme_key = f"pm_show_schemes_{pid}"
            if st.button("🧠 Schemes", key=f"pm_scheme_btn_{pid}", use_container_width=True):
                st.session_state[scheme_key] = not bool(st.session_state.get(scheme_key))
                st.rerun()
            if st.session_state.get(scheme_key):
                with st.container(border=True):
                    st.caption("Scheme ticks are loaded only after opening this panel, keeping CRM fast.")
                    _render_party_scheme_panel(pid, p.get("party_name", ""))


# ─────────────────────────────────────────────────────────────────────────────
# TAB: LEADS
# ─────────────────────────────────────────────────────────────────────────────

def _tab_leads():
    st.markdown("### 🎯 Lead Pipeline")
    stage_filter = st.selectbox("Stage", ["All"] + LEAD_STAGES, key="lead_f")

    with st.expander("➕ New Lead"):
        with st.form("lead_form"):
            lc1, lc2 = st.columns(2)
            with lc1:
                psearch     = st.text_input("Party search (name/mobile)")
                lead_source = st.selectbox("Source", LEAD_SOURCES)
                stage       = st.selectbox("Stage", LEAD_STAGES)
            with lc2:
                assigned = st.text_input("Assigned To")
                pot_val  = st.number_input("Potential Value (₹)", min_value=0.0, step=500.0)
                notes    = st.text_area("Notes", height=68)
            sub = st.form_submit_button("💾 Save Lead", type="primary")
        if sub:
            results = fetch_parties(search=psearch) if psearch else []
            if not results:
                st.error("Party not found — add via Party Master first.")
            else:
                lid, err = save_lead({"party_id": str(results[0]["id"]),
                                       "lead_source": lead_source, "stage": stage,
                                       "notes": notes, "assigned_to": assigned,
                                       "potential_value": pot_val})
                if lid: st.success(f"✅ Lead saved"); st.rerun()
                else:   st.error(f"❌ {err}")

    leads = fetch_leads(stage_filter)
    if not leads:
        st.info("No leads found."); return

    by_stage = {}
    for ld in leads:
        by_stage.setdefault(ld.get("stage","NEW"), []).append(ld)

    show = LEAD_STAGES if stage_filter == "All" else [stage_filter]
    for i in range(0, len(show), 3):
        cols = st.columns(len(show[i:i+3]))
        for col, stage in zip(cols, show[i:i+3]):
            color = STAGE_COLORS.get(stage, "#6b7280")
            sl    = by_stage.get(stage, [])
            with col:
                st.markdown(
                    f"<div style='background:{color};color:#fff;text-align:center;"
                    f"border-radius:6px;padding:4px;font-weight:700;margin-bottom:6px'>"
                    f"{stage} ({len(sl)})</div>", unsafe_allow_html=True)
                for ld in sl:
                    with st.container(border=True):
                        st.markdown(f"**{ld.get('party_name','?')}**")
                        if ld.get("potential_value"):
                            st.caption(f"₹{float(ld['potential_value']):,.0f}")
                        if ld.get("assigned_to"):
                            st.caption(f"👤 {ld['assigned_to']}")
                        ci = LEAD_STAGES.index(stage)
                        for ns in LEAD_STAGES[ci+1:ci+3]:
                            if st.button(f"→ {ns}", key=f"la_{ld['id']}_{ns}",
                                         use_container_width=True):
                                _, err = save_lead({**dict(ld), "id": str(ld["id"]),
                                                    "stage": ns})
                                if err: st.error(err)
                                else:   st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# TAB: FOLLOW-UPS
# ─────────────────────────────────────────────────────────────────────────────

def _tab_followups():
    st.markdown("### 📅 Follow-Up Tracker")
    all_p   = fetch_followups_all()
    overdue  = [f for f in all_p if f.get("due_date") and f["due_date"] <= date.today()]
    upcoming = [f for f in all_p if f not in overdue]

    if overdue:
        st.error(f"🔴 **{len(overdue)} overdue** follow-up(s)")
        for f in overdue:
            c1, c2, c3 = st.columns([5, 2, 1])
            with c1:
                icon = FU_ICONS.get(f.get("followup_type",""), "📌")
                st.markdown(f"{icon} **{f.get('party_name','?')}** — {f.get('followup_type','')}")
                st.caption(f"Due: {f.get('due_date','')}  ·  {(f.get('notes') or '')[:60]}")
            with c2: st.caption(f"📱 {f.get('mobile','') or '—'}")
            with c3:
                if st.button("✅", key=f"fod_{f['id']}"):
                    mark_followup_done(str(f["id"])); st.rerun()
        st.markdown("---")

    with st.expander("➕ Schedule Follow-Up"):
        with st.form("fu_form"):
            fc1, fc2 = st.columns(2)
            with fc1:
                fu_search = st.text_input("Party name / mobile")
                fu_type   = st.selectbox("Type", FOLLOWUP_TYPES)
            with fc2:
                fu_due   = st.date_input("Due Date", value=date.today() + timedelta(days=1))
                fu_notes = st.text_area("Notes", height=68)
            sub = st.form_submit_button("📅 Schedule", type="primary")
        if sub:
            results = fetch_parties(search=fu_search) if fu_search else []
            if not results:
                st.error("Party not found.")
            else:
                fid, err = save_followup({"party_id": str(results[0]["id"]),
                                          "followup_type": fu_type, "due_date": fu_due,
                                          "notes": fu_notes, "created_by": _user()})
                if fid: st.success(f"✅ Scheduled"); st.rerun()
                else:   st.error(f"❌ {err}")

    if not upcoming:
        st.info("🎉 No pending follow-ups."); return

    st.markdown("#### Upcoming")
    for f in upcoming:
        due  = f.get("due_date")
        late = due and due <= date.today()
        bg   = "#fffbeb" if not late else "#fef2f2"
        bdr  = "#f59e0b" if not late else "#dc2626"
        st.markdown(f"<div style='background:{bg};border-left:4px solid {bdr};"
                    f"padding:8px 12px;border-radius:4px;margin:3px 0'>",
                    unsafe_allow_html=True)
        uc1, uc2, uc3 = st.columns([5, 2, 1])
        with uc1:
            icon = FU_ICONS.get(f.get("followup_type",""), "📌")
            st.markdown(f"{icon} **{f.get('party_name','?')}**")
            st.caption(f"{(f.get('notes') or '')[:80]}")
        with uc2:
            st.caption(f"Due: **{due}**")
            st.caption(f"📱 {f.get('mobile','') or '—'}")
        with uc3:
            if st.button("✅", key=f"fup_{f['id']}", help="Mark Done"):
                mark_followup_done(str(f["id"])); st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# TAB: CONTACTS
# ─────────────────────────────────────────────────────────────────────────────

def _tab_contacts():
    st.markdown("### 📋 Contact Book")
    cc1, cc2 = st.columns([3, 1])
    with cc1:
        csearch = st.text_input("Search", key="cb_s", placeholder="Name, mobile or GSTIN…")
    with cc2:
        ctype = st.selectbox("Type", ["All"] + PARTY_TYPES, key="cb_t")

    parties = fetch_parties(ctype if ctype != "All" else None, csearch)
    if not parties:
        st.info("No contacts found."); return
    st.caption(f"{len(parties)} contact(s)")

    def _contact_label(p):
        mob = p.get("mobile") or "—"
        return f"{p.get('party_name','')} | {p.get('party_type','')} | {mob}"

    labels = [_contact_label(p) for p in parties]
    selected_label = st.selectbox(
        "Select contact",
        labels,
        key="cb_contact_sel",
        help="Select one contact to load touchpoints. This keeps CRM fast.",
    )
    p = parties[labels.index(selected_label)] if selected_label in labels else parties[0]

    summary_rows = [
        {
            "Party": x.get("party_name", ""),
            "Type": x.get("party_type", ""),
            "Mobile": x.get("mobile", ""),
            "City": x.get("city", ""),
            "GSTIN": x.get("gstin", ""),
            "Contact": x.get("contact_person", ""),
        }
        for x in parties[:100]
    ]
    st.dataframe(summary_rows, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown(f"#### {p.get('party_name','')}")
    dc1, dc2 = st.columns([2, 3])
    with dc1:
        for field, val in [
            ("City",    p.get("city")), ("Area",  p.get("area")),
            ("Email",   p.get("email")), ("GSTIN", p.get("gstin")),
            ("Credit Limit", f"₹{float(p['credit_limit']):,.0f}" if p.get("credit_limit") else None),
            ("Credit Days",  str(p.get("credit_days","")) if p.get("credit_days") else None),
        ]:
            if val:
                st.markdown(f"**{field}:** {val}")
    with dc2:
        tps = fetch_touchpoints(str(p["id"]))
        st.markdown("**Touchpoints**")
        if tps:
            for tp in tps[:8]:
                icon = {"NOTE":"📝","CALL":"📞","VISIT":"🚗",
                        "EMAIL":"📧","WHATSAPP":"💬"}.get(tp.get("type",""), "•")
                ts   = str(tp.get("created_at",""))[:16]
                st.caption(f"{icon} {ts} — {(tp.get('summary') or '')[:100]}")
        else:
            st.caption("No touchpoints yet.")
        with st.form(f"tp_{p['id']}"):
            t1, t2 = st.columns([1, 3])
            with t1:
                tp_type = st.selectbox(
                    "Touchpoint type",
                    ["NOTE","CALL","VISIT","EMAIL","WHATSAPP"],
                    key=f"tpt_{p['id']}",
                    label_visibility="collapsed",
                )
            with t2:
                tp_text = st.text_input(
                    "Touchpoint note",
                    key=f"tps_{p['id']}",
                    label_visibility="collapsed",
                    placeholder="Enter note…",
                )
            if st.form_submit_button("📝 Log"):
                log_touchpoint(str(p["id"]), tp_type, tp_text, _user())
                st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# METRICS BAR
# ─────────────────────────────────────────────────────────────────────────────

def _render_metrics():
    m = fetch_metrics()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("👥 Parties",           m["tp"])
    c2.metric("🏭 Suppliers",          m["ts"], help="Ready for Order to Supplier")
    c3.metric("🎯 Open Leads",         m["ol"])
    c4.metric("🔴 Overdue Follow-ups", m["of"],
              delta="Action needed" if m["of"] else "All clear",
              delta_color="inverse")


def _safe_crm_tab(label, fn):
    try:
        fn()
    except Exception as e:
        logger.exception("[CRM:%s] tab render failed", label)
        st.error(f"{label} tab blocked: {e}")
        with st.expander("Technical details for support", expanded=False):
            st.code(traceback.format_exc())


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY
# ─────────────────────────────────────────────────────────────────────────────

def render_crm_module():
    _bootstrap()
    st.subheader("🤝 CRM — Customer & Supplier Relationship Manager")

    with st.expander("🔌 Diagnostics", expanded=False):
        if st.button("Run CRM diagnostics", key="crm_run_diagnostics"):
            ca, cb = st.columns(2)
            with ca:
                try:
                    from modules.sql_adapter import get_connection
                    c = get_connection(); c.close()
                    st.success("✅ DB connected")
                except Exception as e:
                    st.error(f"❌ DB: {e}")
            with cb:
                cols = _q("SELECT column_name FROM information_schema.columns "
                          "WHERE table_name='parties' AND table_schema='public'", {})
                col_names = sorted(r.get("column_name","") for r in cols)
                st.caption("parties cols: " + ", ".join(col_names))
        if st.button("🔄 Clear Cache"):
            _bust(); st.rerun()

    _render_metrics()
    st.markdown("---")

    crm_views = {
        "🏭 Suppliers": ("Suppliers", _tab_suppliers),
        "🗂️ Party Master": ("Party Master", _tab_party_master),
        "🎯 Leads": ("Leads", _tab_leads),
        "📅 Follow-Ups": ("Follow-Ups", _tab_followups),
        "📋 Contacts": ("Contacts", _tab_contacts),
        "🧠 Admin DB Editor": ("Admin DB Editor", _render_admin_db_editor),
        "🕐 Audit Log": ("Audit Log", _tab_audit_log),
    }
    active = st.radio(
        "CRM View",
        list(crm_views.keys()),
        horizontal=True,
        label_visibility="collapsed",
        key="crm_active_view",
    )
    label, fn = crm_views[active]
    _safe_crm_tab(label, fn)
