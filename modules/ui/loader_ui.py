"""
modules/loaders/loader_ui.py
=============================
Data Loader Module — WIN16 DV ERP  (Production Grade v2)
Stop → Schema Diff → AI Advice → Preview → Accept → Import → Audit

Tab structure:
  1. Upload & Import  (Stop→Preview→Accept→Go)
  2. DB Export
  3. Audit & Integrity
  4. Schema History
  5. Admin (Flags + Health)
  6. Schema Reference
"""

import io
import os
import tempfile
import traceback
import json
import logging
from datetime import datetime

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)


# ─── Lazy imports ─────────────────────────────────────────────────────────────
def _get_core():
    try:
        from modules.loaders.universal_loader_core import (
            run_loader, detect_file_type, load_excel, load_excel_df,
            export_all_tables, export_master_report,
            is_opening_mode_allowed, STOCK_MODE_SUPPORTED,
        )
        from modules.loaders.patches.loader_transaction_wrapper import run_loader_safe
        return run_loader_safe, detect_file_type, load_excel_df, export_all_tables, export_master_report, is_opening_mode_allowed, STOCK_MODE_SUPPORTED
    except Exception:
        return None, None, None, None, None, None, None

def _get_guard():
    try:
        from modules.loaders.schema_guard import (
            analyze_schema, generate_ai_advice, analyze_column_quality,
            save_schema_history,
        )
        return analyze_schema, generate_ai_advice, analyze_column_quality, save_schema_history
    except Exception:
        return None, None, None, None


def _get_flags():
    try:
        from modules.loaders.feature_flags import get_flag, get_all_flags, set_flag, ensure_flags_table
        return get_flag, get_all_flags, set_flag, ensure_flags_table
    except Exception:
        return (lambda k, d=True: d), (lambda: {}), (lambda k, v: False), (lambda: False)


def _get_downloader():
    try:
        from modules.loaders.data_downloader import (
            DOWNLOAD_CONFIGS, fetch_for_download,
            build_download_excel, compute_diff, get_key_cols_for_type,
        )
        return DOWNLOAD_CONFIGS, fetch_for_download, build_download_excel, compute_diff, get_key_cols_for_type
    except Exception:
        return None, None, None, None, None


# ─── CSS ──────────────────────────────────────────────────────────────────────
_CSS = """
<style>
.loader-card {
    background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 10px; padding: 16px 20px; margin-bottom: 12px;
}
.stat-box {
    background: #1e3a5f; color: white;
    border-radius: 8px; padding: 12px 8px; text-align: center;
}
.stat-num  { font-size: 26px; font-weight: 700; }
.stat-lbl  { font-size: 11px; opacity: 0.8; margin-top: 2px; }
.ok-badge  { background:#dcfce7; color:#16a34a; border-radius:4px; padding:2px 8px; font-size:12px; font-weight:600; }
.warn-badge{ background:#fff3cd; color:#92400e; border-radius:4px; padding:2px 8px; font-size:12px; font-weight:600; }
.err-badge { background:#fee2e2; color:#dc2626; border-radius:4px; padding:2px 8px; font-size:12px; font-weight:600; }
.mode-shadow{ background:#dbeafe; color:#1d4ed8; border-radius:6px; padding:4px 12px; font-weight:700; }
.mode-live  { background:#dcfce7; color:#15803d; border-radius:6px; padding:4px 12px; font-weight:700; }
.mode-dry   { background:#f3f4f6; color:#374151; border-radius:6px; padding:4px 12px; font-weight:700; }
.stock-add     { background:#f0fdf4; color:#166534; border-radius:6px; padding:4px 12px; font-weight:700; }
.stock-opening { background:#fef9c3; color:#854d0e; border-radius:6px; padding:4px 12px; font-weight:700; }
.stock-price   { background:#dbeafe; color:#1d4ed8; border-radius:6px; padding:4px 12px; font-weight:700; }
.stock-locked  { background:#f3f4f6; color:#9ca3af; border-radius:6px; padding:4px 12px; font-weight:600; font-style:italic; }
.section-head  { font-size:15px; font-weight:700; color:#1e3a5f; margin-bottom:8px; }
.file-type-pill{ display:inline-block; background:#1e3a5f; color:white;
                 border-radius:20px; padding:3px 14px; font-size:13px; font-weight:600; margin-bottom:8px; }
.opening-warning{ background:#fef3c7; border:1px solid #f59e0b; border-radius:8px;
                  padding:12px 16px; margin:8px 0; color:#78350f; font-weight:500; }
.schema-stop  { background:#fee2e2; border:1px solid #fca5a5; border-radius:10px;
                padding:14px 18px; margin:10px 0; }
.schema-warn  { background:#fff7ed; border:1px solid #fdba74; border-radius:10px;
                padding:14px 18px; margin:10px 0; }
.schema-ok    { background:#f0fdf4; border:1px solid #86efac; border-radius:10px;
                padding:14px 18px; margin:10px 0; }
.ai-panel     { background:linear-gradient(135deg,#1e3a5f 0%,#2d5a9e 100%);
                color:white; border-radius:12px; padding:18px 22px; margin:12px 0; }
.ai-title     { font-size:16px; font-weight:700; margin-bottom:10px; }
.ai-item      { padding:5px 0; font-size:13px; border-bottom:1px solid rgba(255,255,255,0.1); }
.ai-item:last-child { border-bottom:none; }
.col-ok       { background:#dcfce7; border-radius:4px; padding:1px 6px; font-size:11px; }
.col-warn     { background:#fef9c3; border-radius:4px; padding:1px 6px; font-size:11px; }
.col-err      { background:#fee2e2; border-radius:4px; padding:1px 6px; font-size:11px; }
.col-unknown  { background:#f3e8ff; border-radius:4px; padding:1px 6px; font-size:11px; }
.col-empty    { background:#f1f5f9; border-radius:4px; padding:1px 6px; font-size:11px; }
.step-active  { background:#1e3a5f; color:white; border-radius:50%; width:28px; height:28px;
                display:inline-flex; align-items:center; justify-content:center; font-weight:700; }
.step-done    { background:#16a34a; color:white; border-radius:50%; width:28px; height:28px;
                display:inline-flex; align-items:center; justify-content:center; font-weight:700; }
.step-pending { background:#e2e8f0; color:#6b7280; border-radius:50%; width:28px; height:28px;
                display:inline-flex; align-items:center; justify-content:center; }
.flag-on      { background:#dcfce7; color:#15803d; border-radius:4px; padding:2px 8px; font-size:12px; font-weight:600; }
.flag-off     { background:#fee2e2; color:#dc2626; border-radius:4px; padding:2px 8px; font-size:12px; font-weight:600; }
.dl-card      { background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:14px 16px; margin-bottom:10px; cursor:pointer; transition:border 0.15s; }
.dl-card-sel  { background:#EFF6FF; border:2px solid #1e3a5f; border-radius:10px; padding:14px 16px; margin-bottom:10px; }
.dl-card-icon { font-size:24px; margin-bottom:4px; }
.dl-card-title{ font-weight:700; font-size:13px; color:#1e293b; }
.dl-card-desc { font-size:11px; color:#6b7280; margin-top:3px; line-height:1.4; }
.dl-card-key  { font-size:10px; color:#9ca3af; margin-top:5px; }
.dl-workflow  { background:linear-gradient(135deg,#1e3a5f,#2d5a9e); color:white; border-radius:12px; padding:16px 20px; margin:12px 0; }
.dl-wf-title  { font-weight:700; font-size:15px; margin-bottom:8px; }
.dl-wf-step   { padding:3px 0; font-size:13px; opacity:0.92; }
.diff-new     { background:#dcfce7; color:#166534; border-radius:4px; padding:2px 10px; font-weight:700; font-size:13px; }
.diff-updated { background:#fef9c3; color:#92400e; border-radius:4px; padding:2px 10px; font-weight:700; font-size:13px; }
.diff-deleted { background:#fee2e2; color:#dc2626; border-radius:4px; padding:2px 10px; font-weight:700; font-size:13px; }
.diff-unch    { background:#f1f5f9; color:#64748b; border-radius:4px; padding:2px 10px; font-weight:700; font-size:13px; }
.edit-guide   { background:#f0fdf4; border:1px solid #86efac; border-radius:8px; padding:12px 16px; margin:8px 0; color:#14532d; font-size:13px; }
.reimport-box { background:#dbeafe; border:1px solid #93c5fd; border-radius:8px; padding:12px 16px; margin:10px 0; color:#1e3a8a; font-size:13px; }
</style>
"""

# ─── Metadata ────────────────────────────────────────────────────────────────
FILE_TYPE_META = {
    "PRODUCT": {"label": "Product Master",        "icon": "📦",  "desc": "Lenses, frames, solutions — master catalogue",      "key_col": "Product",             "table": "products",        "sample": "product_master.xlsx"},
    "FRAME":   {"label": "Frame Batch",           "icon": "🖼️",  "desc": "Frame SKUs with pricing and dimensions",            "key_col": "SKUCode",             "table": "frames",          "sample": "Frame_batch.xlsx"},
    "PARTY":   {"label": "Party Master",          "icon": "🏢",  "desc": "Customers, doctors, suppliers — with GST, PAN, credit terms", "key_col": "MOBILE or PARTYNAME", "table": "parties", "sample": "party_master.xlsx"},
    "PATIENT": {"label": "Patient Data",          "icon": "👤",  "desc": "Patient records with Rx history",                  "key_col": "Mobile / Record No",  "table": "patients",        "sample": "Patient Data.xlsx"},
    "OPHLENS": {"label": "Ophthalmic Lens Stock", "icon": "👁️",  "desc": "Single vision / progressive / toric with powers",  "key_col": "Product + SPH/CYL",   "table": "inventory_stock", "sample": "ophlens_batch.xlsx"},
    "CLENS":   {"label": "Contact Lens Batch",    "icon": "🔵",  "desc": "Contact lenses with powers and expiry",            "key_col": "Product + SPH + Batch","table": "inventory_stock", "sample": "clens_batch.xlsx"},
    "SOL":     {"label": "Solution / Batch",      "icon": "🧴",  "desc": "Solutions, accessories — batch import",            "key_col": "Product + BatchNo",   "table": "batches",         "sample": "Sol_batch.xlsx"},
    "BLANK":   {"label": "Blank Inventory",       "icon": "🔲",  "desc": "Optical blank lenses with base curves",            "key_col": "Brand+Category+Add",  "table": "blank_inventory", "sample": "Blankupdate.xlsx"},
}

STOCK_MODE_IRRELEVANT = {"PRODUCT", "PARTY", "PATIENT", "SOL", "UNKNOWN"}

# ── DB column registry — REGISTRY-DRIVEN (auto-syncs with DB schema) ─────────
# All column definitions come from db_schema_registry.py.
# Adding a column to DB_SCHEMA automatically updates the column mapping panel.
# DO NOT add columns here manually — edit db_schema_registry.py instead.

def _build_db_table_columns():
    """Build DB_TABLE_COLUMNS from registry at startup."""
    try:
        from modules.loaders.db_schema_registry import get_writable_cols
        result = {}
        for ft in ["PRODUCT", "FRAME", "PARTY", "PATIENT", "OPHLENS", "CLENS", "SOL", "BLANK"]:
            result[ft] = [c.db_column for c in get_writable_cols(ft) if c.db_column]
        return result
    except Exception:
        # Fallback to hardcoded if registry unavailable
        return {
            "PRODUCT":  ["product_name","brand","main_group","category","material",
                          "index_value","coating","coating_type","colour","gender",
                          "wear_schedule","unit","is_batch_applicable","is_eye_specific",
                          "is_active","hsn_code","box_size","allow_loose","gst_percent",
                          "lens_category","brand_group"],
            "FRAME":    ["product_name","model","brand","sku_code","size_a","dbl",
                          "temple_length","base_material","finish","colour","shape",
                          "qty","cost_price","mrp","gst_percent","is_active"],
            "PARTY":    [
                "party_name","party_type","mobile","alt_mobile","email","contact_person",
                "address","city","area","pincode","state_name","state_code",
                "gstin","pan_no","tan_no","cin_no","gst_rate",
                "credit_limit","credit_days","opening_balance","balance_type",
                "tally_group","notes","is_active",
            ],
            "PATIENT":  ["master_name","mobile","record_no","visit_date","right_sph",
                          "right_cyl","right_axis","right_add","left_sph","left_cyl",
                          "left_axis","left_add"],
            "OPHLENS":  ["product_name","sph","cyl","axis","add_power","eye_side",
                          "item_type","stock_type","quantity","purchase_rate","selling_price",
                          "mrp","lens_design","location","is_active"],
            "CLENS":    ["product_name","sph","cyl","axis","add_power","eye_side",
                          "batch_no","expiry_date","quantity","purchase_rate","selling_price",
                          "mrp","item_type","lens_design","location","is_active"],
            "SOL":      ["product_name","batch_no","expiry_date","qty_available",
                          "cost_price","selling_price","mrp","is_active"],
            "BLANK":    ["brand","category","material","colour","add_power","qty_right",
                          "qty_left","qty_independent","min_stock","cost_price","batch_no",
                          "location","base_recommended","base_1","base_2","base_3","is_active"],
        }

DB_TABLE_COLUMNS = _build_db_table_columns()

# Note next to product_name for inventory tables — shown in the panel
_PRODUCT_NAME_NOTE = {
    "OPHLENS": "product_name  (loader resolves → product_id automatically)",
    "CLENS":   "product_name  (loader resolves → product_id automatically)",
    "SOL":     "product_name  (loader resolves → product_id automatically)",
}


IMPORT_ORDER = [
    ("1", "product_master.xlsx",  "PRODUCT", "Required first — all stock files need products in DB"),
    ("2", "party_master.xlsx",    "PARTY",   "Required before orders reference parties"),
    ("3", "Patient Data.xlsx",    "PATIENT", "Independent — no dependencies"),
    ("4", "Frame_batch.xlsx",     "FRAME",   "Independent of products table"),
    ("5", "ophlens_batch.xlsx",   "OPHLENS", "Needs products imported first"),
    ("6", "clens_batch.xlsx",     "CLENS",   "Needs products imported first"),
    ("7", "Sol_batch.xlsx",       "SOL",     "Needs products imported first"),
    ("8", "Blankupdate.xlsx",     "BLANK",   "Standalone — no dependencies"),
]

def _build_schema_contract():
    """Build schema contract dynamically from registry."""
    try:
        from modules.loaders.db_schema_registry import get_writable_cols, get_allowed_values
        result = {}
        for ft in ["PRODUCT", "FRAME", "PARTY", "PATIENT", "OPHLENS/CLENS", "SOL", "BLANK"]:
            actual_ft = ft.split("/")[0].strip()
            cols = get_writable_cols(actual_ft)
            result[ft] = {
                "required": [c.excel_header for c in cols if c.required],
                "optional": [c.excel_header for c in cols if not c.required],
                "enums":    {c.excel_header: c.allowed_values for c in cols if c.allowed_values},
                "notes":    "; ".join(c.notes for c in cols if c.notes),
            }
        return result
    except Exception:
        # Hardcoded fallback
        return {
            "PRODUCT": {
                "required": ["Product"],
                "optional": ["MainGroup", "Type", "LensCategory", "Brand", "BrandProductGroup",
                             "Material", "Index", "Coating", "coating_type", "Colour", "Gender",
                             "WearSchedule", "unit", "IsBatchApplicable", "IsEyeSpecific",
                             "IsActive", "HSNCode", "Box Size", "Allow Loose", "GSTPercent"],
                "enums": {}, "notes": "ProductCode auto-generated.",
            },
            "FRAME": {
                "required": ["SKUCode"],
                "optional": ["Product", "Model", "Brand", "ASize", "DBL", "TempleLength",
                             "BaseMaterial", "Finish", "Colour", "shape", "Qty", "CostPrice", "MRP",
                             "GSTPercent", "IsActive"],
                "enums": {"GSTPercent": ["0","5","12","18","28"]},
                "notes": "Conflict key: SKUCode.",
            },
            "PARTY": {
                "required": ["PARTYNAME"],
                "optional": [
                    "ROLETYPE","MOBILE","ALTMOBILE","EMAIL","CONTACTPERSON",
                    "ADDRESS","CITY","AREA","PINCODE","STATE","STATECODE",
                    "GSTIN","PAN","TAN","CIN","GSTRATE",
                    "CREDITLIMIT","CREDITDAYS","OPENINGBALANCE","BALANCETYPE",
                    "TALLYGROUP","NOTES","ISACTIVE",
                ],
                "enums": {
                    "ROLETYPE":    ["Retail","Doctor","Optician","Supplier","Fitter","Wholesale"],
                    "ISACTIVE":    ["YES","NO"],
                    "BALANCETYPE": ["Dr","Cr"],
                    "GSTRATE":     ["0","5","12","18","28"],
                },
                "notes": (
                    "Conflict key: MOBILE if filled, else PARTYNAME. "
                    "GSTIN = 15 chars (e.g. 27AABCU9603R1ZX). "
                    "PAN = 10 chars. ISACTIVE: YES/NO. BALANCETYPE: Dr/Cr."
                ),
            },
            "PATIENT": {
                "required": ["Client Name", "Mobile Number OR Record No"],
                "optional": ["Date", "Right Sph", "Right CYL", "Right AXIS", "Right Add Power",
                             "Left SPH", "Left CYL", "Left AXIS", "Left Add Power"],
                "enums": {}, "notes": "Each row = one patient_visits record.",
            },
            "OPHLENS / CLENS": {
                "required": ["Product", "Quantity"],
                "optional": ["SPH", "CYL", "AXIS", "ADD", "BatchNo", "ExpiryDate", "EyeSide",
                             "MRP", "PurchaseRate", "SellingPrice", "IsActive", "lens_design",
                             "GSTPercent"],
                "enums": {"EyeSide": ["R", "L", "B"], "lens_design": ["SPHERICAL", "TORIC", "MULTIFOCAL"]},
                "notes": "TORIC: CYL present → AXIS mandatory. GSTPercent is read-only (from product master).",
            },
            "SOL": {
                "required": ["Product"],
                "optional": ["BatchNo", "ExpiryDate", "Qty", "CostPrice", "SellingPrice", "MRP",
                             "GSTPercent", "IsActive"],
                "enums": {}, "notes": "Always inserts new batch row. GSTPercent is read-only (from product master).",
            },
            "BLANK": {
                "required": ["brand", "Category", "Material", "Add"],
                "optional": ["COLOUR", "qty_Right", "qty_left", "qty_independent",
                             "Recomended Base", "Base 1 P", "Base 2 P", "Base 3P",
                             "min_stock", "cost_price", "batch_no", "location", "IsActive"],
                "enums": {}, "notes": "Conflict: brand+category+material+colour+add_power.",
            },
        }

SCHEMA_CONTRACT = _build_schema_contract()


# ════════════════════════════════════════════════════════════
# RENDER HELPERS
# ════════════════════════════════════════════════════════════

def _stat(val, label, col):
    col.markdown(f'<div class="stat-box"><div class="stat-num">{val}</div><div class="stat-lbl">{label}</div></div>', unsafe_allow_html=True)


def _render_steps(current_step: int):
    steps = ["📁 Upload", "🔍 Analyse", "👁 Preview", "✅ Accept", "🚀 Import"]
    cols = st.columns(len(steps))
    for i, (col, label) in enumerate(zip(cols, steps)):
        n = i + 1
        with col:
            if n < current_step:
                st.markdown(f'<div style="text-align:center"><span class="step-done">✓</span><br><small style="color:#16a34a">{label}</small></div>', unsafe_allow_html=True)
            elif n == current_step:
                st.markdown(f'<div style="text-align:center"><span class="step-active">{n}</span><br><small style="color:#1e3a5f;font-weight:700">{label}</small></div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div style="text-align:center"><span class="step-pending">{n}</span><br><small style="color:#9ca3af">{label}</small></div>', unsafe_allow_html=True)
    st.markdown("---")


def _render_ai_panel(advice: list):
    if not advice:
        return
    level_order = {"error": 0, "warn": 1, "info": 2, "tip": 3}
    advice_sorted = sorted(advice, key=lambda x: level_order.get(x["level"], 9))
    items_html = ""
    for a in advice_sorted:
        prefix = {"error": "🚨 <strong>ERROR:</strong>", "warn": "⚠️ <strong>WARN:</strong>",
                  "info": "ℹ️", "tip": "💡 <em>"}.get(a["level"], "")
        suffix = "</em>" if a["level"] == "tip" else ""
        items_html += f'<div class="ai-item">{prefix} {a["message"]}{suffix}</div>'
    st.markdown(f'<div class="ai-panel"><div class="ai-title">🤖 AI Import Advisor</div>{items_html}</div>', unsafe_allow_html=True)


def _render_schema_diff(diff, strict_mode: bool) -> bool:
    """Returns True if blocking."""
    if not diff.has_changes():
        st.markdown('<div class="schema-ok">✅ <strong>Schema OK</strong> — columns match known schema.</div>', unsafe_allow_html=True)
        return False

    blocking = False

    if diff.missing_columns:
        blocking = True
        st.markdown(f'<div class="schema-stop">🚫 <strong>Missing Required Columns</strong><br>'
                    f'<code>{"</code>, <code>".join(diff.missing_columns)}</code><br>'
                    f'<em>Add these to your Excel file and re-upload.</em></div>', unsafe_allow_html=True)

    if diff.new_columns:
        items = ""
        for col in diff.new_columns:
            items += f"<li><code>{col}</code> — not part of known schema, will be ignored safely</li>"
        cls = "schema-stop" if strict_mode else "schema-warn"
        title = "🚫 Unknown Columns (STRICT — Blocked)" if strict_mode else "🔍 Unknown Columns Detected (will be ignored)"
        st.markdown(f'<div class="{cls}">{title}<ul>{items}</ul></div>', unsafe_allow_html=True)
        if strict_mode:
            blocking = True

    if diff.newly_filled:
        st.markdown(f'<div class="schema-warn">🟡 <strong>Newly Filled Fields</strong><br>'
                    f'Previously empty, now have data: <code>{"</code>, <code>".join(diff.newly_filled)}</code></div>', unsafe_allow_html=True)

    return blocking


def _render_column_quality(col_report: list):
    if not col_report:
        return
    rows = []
    for r in col_report:
        status = r["status"]
        badge = {"critical": "⛔ REQUIRED EMPTY", "warning": "⚠️ REQUIRED LOW",
                 "unknown": "❓ UNKNOWN", "empty": "○ EMPTY", "ok": "✓ OK"}.get(status, "ERR")
        rows.append({
            "Req": "🔴" if r["required"] else "⚪",
            "Column": r["column"],
            "Filled %": f"{r['fill_pct']}%",
            "Count": f"{r['filled']}/{r['total']}",
            "Status": badge,
            "Sample": " | ".join(r["samples"][:3]) if r["samples"] else "—",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("🔴 = Required | CRITICAL/LOW = must fix before import")


def _render_result(result: dict):
    mode  = result["mode"]
    smode = result.get("stock_mode", "ADD")
    mode_cls  = {"DRY": "mode-dry", "SHADOW": "mode-shadow", "LIVE": "mode-live"}.get(mode, "mode-dry")
    smode_cls = "stock-opening" if smode == "OPENING" else ("stock-price" if smode == "PRICE_ONLY" else "stock-add")
    st.markdown(f'<span class="{mode_cls}">MODE: {mode}</span>&nbsp;&nbsp;<span class="{smode_cls}">STOCK: {smode}</span>', unsafe_allow_html=True)
    st.markdown("")
    c1, c2, c3, c4, c5 = st.columns(5)
    _stat(f"{result['total_rows']:,}", "Total Rows",   c1)
    _stat(f"{result['inserted']:,}",   "Inserted",     c2)
    _stat(f"{result['updated']:,}",    "Updated",      c3)
    _stat(f"{result['skipped']:,}",    "Skipped",      c4)
    _stat(f"{result['success_rate']}%","Success Rate", c5)
    st.caption(f"⏱ Completed in {result['duration_s']:.1f}s")
    if result["warnings"]:
        with st.expander(f"⚠️ Warnings ({len(result['warnings'])})"):
            for w in result["warnings"]:
                st.warning(w)
    if result["errors"]:
        with st.expander(f"❌ Errors ({result['error_count']})", expanded=result['error_count'] < 20):
            df_err = pd.DataFrame(result["errors"])
            st.dataframe(df_err, use_container_width=True, hide_index=True)
            buf = io.BytesIO()
            df_err.to_excel(buf, index=False)
            buf.seek(0)
            st.download_button("📥 Download Error Report", data=buf,
                               file_name=f"loader_errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.success("✅ No errors — clean import!")


# ════════════════════════════════════════════════════════════
# TAB 1: UPLOAD & IMPORT
# ════════════════════════════════════════════════════════════


def _render_col_mapping_panel(df_cols: list, file_type: str, key: str) -> dict:
    """
    Manual Column Mapping panel.
    Shows every Excel column with a dropdown of all real DB target columns.
    Pre-fills with AI fuzzy suggestion. User can override or SKIP.
    Returns {excel_col: db_col_or_None}
    """
    import difflib

    db_cols = DB_TABLE_COLUMNS.get(file_type, [])
    if not db_cols:
        st.info("No column schema registered for this file type.")
        return {}

    # Load registry info for descriptions / examples / allowed values
    try:
        from modules.loaders.db_schema_registry import get_schema_for_ui
        schema_info = {c["db_column"]: c for c in get_schema_for_ui(file_type)}
    except Exception:
        schema_info = {}

    SKIP = "— skip (ignore this column) —"
    options = [SKIP] + db_cols

    def _suggest(col: str) -> str:
        norm = col.lower().replace(" ", "").replace("_", "").replace("-", "")
        for d in db_cols:
            if d.replace("_", "") == norm:
                return d
        aliases = {
            "product": "product_name", "productname": "product_name",
            "qty": "quantity", "costprice": "purchase_rate",
            "purchaserate": "purchase_rate", "sellingprice": "selling_price",
            "eyeside": "eye_side", "batchno": "batch_no",
            "expirydate": "expiry_date", "addpower": "add_power",
            "lensdesign": "lens_design", "isactive": "is_active",
            "itemtype": "item_type", "qtyright": "qty_right",
            "qtyleft": "qty_left", "qtyindependent": "qty_independent",
            "qtyavailable":   "qty_available",
            "gstpercent":     "gst_percent",
            # PARTY extended fields
            "altmobile":      "alt_mobile",
            "alternatemobile":"alt_mobile",
            "contactperson":  "contact_person",
            "contact":        "contact_person",
            "pincode":        "pincode",
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
            "creditlimit":    "credit_limit",
            "creditdays":     "credit_days",
            "openingbalance": "opening_balance",
            "openingbal":     "opening_balance",
            "balancetype":    "balance_type",
            "tallygroup":     "tally_group",
            "tallyname":      "tally_group",
            "notes":          "notes",
            "remarks":        "notes",
        }
        if norm in aliases and aliases[norm] in db_cols:
            return aliases[norm]
        db_norms = [d.replace("_", "") for d in db_cols]
        hits = difflib.get_close_matches(norm, db_norms, n=1, cutoff=0.72)
        if hits:
            return db_cols[db_norms.index(hits[0])]
        return SKIP

    sess_key = f"colmap_{file_type}_{key}"
    if sess_key not in st.session_state:
        st.session_state[sess_key] = {}

    st.markdown(
        '<div style="background:#1e3a5f;color:white;padding:9px 14px;border-radius:6px 6px 0 0;">'
        '<b>🗂️ Column Mapping — Excel → Database</b>'
        '<span style="font-size:11px;color:#93c5fd;margin-left:12px;">'
        'Pre-filled by AI · Override any row · SKIP to ignore · Hover DB column for description</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    note = _PRODUCT_NAME_NOTE.get(file_type)
    if note:
        st.info(
            f"**{file_type} loads into `inventory_stock`.**  "
            f"Map your product column to `product_name` — the loader looks up the "
            f"product in the database and stores the `product_id` automatically."
        )

    h = st.columns([3, 3, 2, 3])
    h[0].markdown("**Excel column (from your file)**")
    h[1].markdown("**DB column (target)**")
    h[2].markdown("**Match status**")
    h[3].markdown("**Description / Example**")
    st.markdown('<hr style="margin:2px 0 6px 0;border-color:#e2e8f0"/>', unsafe_allow_html=True)

    mapping = {}
    for col in df_cols:
        prev      = st.session_state[sess_key].get(col)
        suggested = _suggest(col)
        default   = prev if prev else suggested
        try:
            idx = options.index(default)
        except ValueError:
            idx = 0

        row = st.columns([3, 3, 2, 3])
        with row[0]:
            st.markdown(
                f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:4px;'
                f'padding:5px 9px;font-size:12px;font-family:monospace;margin-top:5px;">{col}</div>',
                unsafe_allow_html=True,
            )
        with row[1]:
            chosen = st.selectbox(
                f"__map_{col}", options, index=idx,
                key=f"cm_{file_type}_{key}_{col}",
                label_visibility="collapsed",
            )
            st.session_state[sess_key][col] = chosen
        with row[2]:
            if chosen == SKIP:
                st.markdown('<span style="color:#ef4444;font-size:11px;">⊘ SKIP</span>', unsafe_allow_html=True)
            elif chosen == suggested and suggested != SKIP:
                st.markdown('<span style="color:#16a34a;font-size:11px;">✓ AI matched</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span style="color:#2563eb;font-size:11px;">✎ Manual</span>', unsafe_allow_html=True)
        with row[3]:
            if chosen and chosen != SKIP and chosen in schema_info:
                info = schema_info[chosen]
                desc = info.get("description", "")
                ex   = info.get("example", "")
                allowed = info.get("allowed_values")
                tip = f'<span style="font-size:10px;color:#374151">{desc}</span>'
                if ex:
                    tip += f'<span style="font-size:10px;color:#6b7280"> e.g. <b>{ex}</b></span>'
                if allowed:
                    tip += f'<span style="font-size:9px;color:#2563eb"> [{", ".join(allowed)}]</span>'
                st.markdown(tip, unsafe_allow_html=True)
        mapping[col] = None if chosen == SKIP else chosen

    mapped  = sum(1 for v in mapping.values() if v)
    skipped = sum(1 for v in mapping.values() if not v)
    st.markdown(
        f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:0 0 6px 6px;'
        f'padding:7px 14px;font-size:12px;color:#166534;">'
        f'✅ <b>{mapped}</b> mapped &nbsp;|&nbsp; ⊘ <b>{skipped}</b> skipped'
        f'</div>',
        unsafe_allow_html=True,
    )
    return mapping


def _tab_upload():
    run_loader, detect_file_type, load_excel, _, _, is_opening_allowed_fn, STOCK_MODE_SUPPORTED = _get_core()
    analyze_schema, generate_ai_advice, analyze_col_quality, save_schema_hist = _get_guard()
    get_flag, _, _, ensure_flags_table = _get_flags()

    if run_loader is None:
        st.error("❌ Loader core unavailable — check modules/loaders/universal_loader_core.py")
        return

    try:
        ensure_flags_table()
    except Exception:
        pass

    schema_guard_on  = get_flag("loader.schema_guard",     True)
    ai_advisor_on    = get_flag("loader.ai_advisor",       True)
    preview_required = get_flag("loader.preview_required", True)
    strict_mode      = get_flag("loader.strict_mode",      False)
    opening_allowed  = get_flag("loader.opening_enabled",  True)
    try:
        opening_allowed = opening_allowed and is_opening_allowed_fn()
    except Exception:
        pass

    # ── STEP 1: UPLOAD ──────────────────────────────────────────────────────
    _render_steps(1)

    uploaded = st.file_uploader(
        "Drop your Excel or CSV file here",
        type=["xlsx", "xls", "csv"],
        help="Supports: product_master, party_master, Patient Data, Frame_batch, clens_batch, ophlens_batch, Sol_batch, Blankupdate"
    )

    if not uploaded:
        st.markdown('<div class="section-head">Supported File Types</div>', unsafe_allow_html=True)
        cols = st.columns(4)
        for i, (ftype, meta) in enumerate(FILE_TYPE_META.items()):
            with cols[i % 4]:
                st.markdown(f"""<div class="loader-card">
                  <div style="font-size:22px">{meta['icon']}</div>
                  <div style="font-weight:700;font-size:13px">{meta['label']}</div>
                  <div style="font-size:11px;color:#6b7280;margin-top:4px">{meta['desc']}</div>
                  <div style="font-size:10px;color:#9ca3af;margin-top:6px">🔑 {meta['key_col']}</div>
                </div>""", unsafe_allow_html=True)
        st.markdown("---")
        st.markdown('<div class="section-head">📋 Recommended Import Order</div>', unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(IMPORT_ORDER, columns=["Order", "File", "Type", "Reason"]),
                     use_container_width=True, hide_index=True)
        return

    # Save to temp
    suffix = ".xlsx" if uploaded.name.lower().endswith(("xlsx", "xls")) else ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded.read())
        tmp_path = tmp.name

    try:
        df = load_excel(tmp_path)          # load_excel_df — returns plain DataFrame
        file_type = detect_file_type(df)
    except Exception as e:
        st.error(f"❌ Cannot read file: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return

    # ── STEP 2: ANALYSE ─────────────────────────────────────────────────────
    _render_steps(2)

    meta = FILE_TYPE_META.get(file_type, {})
    st.markdown(f'<div class="file-type-pill">{meta.get("icon","📄")} Detected: {meta.get("label", file_type)}</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Rows",    f"{len(df):,}")
    c2.metric("Columns", len(df.columns))
    c3.metric("File",    uploaded.name[:28])

    if file_type == "UNKNOWN":
        st.error("❌ Could not detect file type. Use Force Type override below or check Schema Reference tab.")

    st.markdown("---")
    st.markdown('<div class="section-head">⚙️ Import Settings</div>', unsafe_allow_html=True)

    col_type, col_mode, col_stock = st.columns(3)

    with col_type:
        force_type = st.selectbox("File Type", ["AUTO"] + list(FILE_TYPE_META.keys()),
                                   index=0 if file_type != "UNKNOWN" else 1,
                                   help="Leave AUTO unless detection failed")

    with col_mode:
        mode = st.radio("Import Mode", ["DRY RUN", "SHADOW", "LIVE"], horizontal=True,
                        help="DRY: validate only | SHADOW: test write | LIVE: production")

    display_type    = (None if force_type == "AUTO" else force_type) or file_type
    actual_mode     = mode.replace(" ", "_").replace("_RUN", "")

    with col_stock:
        if display_type in STOCK_MODE_IRRELEVANT:
            st.markdown('<span class="stock-locked">Stock: N/A (master)</span>', unsafe_allow_html=True)
            actual_stock_mode = "ADD"
        elif not opening_allowed:
            st.markdown('<span class="stock-locked">🔒 Opening Mode Locked by Admin</span>', unsafe_allow_html=True)
            actual_stock_mode = "ADD"
        else:
            sc = st.radio("Stock Behaviour", ["Incremental (ADD)", "Price Update (PRICE_ONLY)", "Opening Reset (OPENING)"], horizontal=True,
                          help="ADD: existing+excel qty | PRICE_ONLY: update prices only, qty unchanged | OPENING: set qty to excel value")
            if "PRICE_ONLY" in sc:
                actual_stock_mode = "PRICE_ONLY"
            elif "OPENING" in sc:
                actual_stock_mode = "OPENING"
            else:
                actual_stock_mode = "ADD"

    # Schema analysis
    diff    = None
    blocking = False
    if schema_guard_on and analyze_schema and file_type not in ("UNKNOWN",):
        try:
            diff = analyze_schema(df, file_type)
        except Exception:
            pass

    # AI advice
    advice    = []
    has_errors = False
    if ai_advisor_on and generate_ai_advice and file_type not in ("UNKNOWN",):
        try:
            from modules.loaders.schema_guard import SchemaDiff
            empty_diff = SchemaDiff(file_type, set()) if diff is None else diff
            advice = generate_ai_advice(empty_diff, df, file_type, len(df), actual_mode, actual_stock_mode)
            has_errors = any(a["level"] == "error" for a in advice)
        except Exception:
            pass

    # ── STEP 3: PREVIEW ─────────────────────────────────────────────────────
    _render_steps(3)

    if ai_advisor_on and advice:
        _render_ai_panel(advice)

    if schema_guard_on and diff:
        st.markdown('<div class="section-head">🔍 Schema Analysis</div>', unsafe_allow_html=True)
        blocking = _render_schema_diff(diff, strict_mode)

    with st.expander("📋 Data Preview — first 10 rows", expanded=True):
        st.dataframe(df.head(10), use_container_width=True)

    if analyze_col_quality and file_type not in ("UNKNOWN",):
        with st.expander("📊 Column Quality Report", expanded=False):
            try:
                col_rpt = analyze_col_quality(df, file_type)
                _render_column_quality(col_rpt)
            except Exception as e:
                st.caption(f"Column quality check unavailable: {e}")

    with st.expander("🔍 All Detected Columns"):
        try:
            from modules.loaders.schema_guard import KNOWN_SCHEMA
            ks = KNOWN_SCHEMA.get(file_type, set())
        except Exception:
            ks = set()
        st.dataframe(pd.DataFrame({
            "Column (normalized)": list(df.columns),
            "In Schema": ["✅" if c in ks else "❓" for c in df.columns],
        }), use_container_width=True, hide_index=True)

    # ── COLUMN MAPPING PANEL ─────────────────────────────────────────────────
    col_mapping = {}
    if file_type not in ("UNKNOWN",):
        with st.expander("🗂️ Column Mapping — Review & Override", expanded=True):
            st.caption(
                "Every column from your Excel is shown below. "
                "The loader has pre-matched each column to the closest DB column. "
                "Override any mapping if incorrect. SKIP columns you don't want imported."
            )
            col_mapping = _render_col_mapping_panel(
                list(df.columns),
                display_type,
                key=uploaded.name,
            )

    contract_ok = True
    if display_type not in ("UNKNOWN", None):
        try:
            from modules.loaders.loader_contract import (
                build_loader_contract_report,
                render_loader_contract_panel,
            )
            contract_report = build_loader_contract_report(display_type, df)
            contract_ok = render_loader_contract_panel(
                st,
                contract_report,
                require_ack=(actual_mode != "DRY"),
                key=f"loader_contract_{display_type}_{uploaded.name}_{actual_mode}",
            )
        except Exception as _contract_ex:
            st.warning(f"Loader contract check unavailable: {_contract_ex}")

    # ── STEP 4: ACCEPT ──────────────────────────────────────────────────────
    _render_steps(4)
    st.markdown('<div class="section-head">✅ Confirm & Proceed</div>', unsafe_allow_html=True)

    if blocking:
        st.error("🚫 Import blocked — fix schema errors above, then re-upload your file.")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return

    if has_errors and actual_mode == "LIVE":
        st.warning("⚠️ AI Advisor found issues above. Review carefully.")

    ready = True
    if preview_required and actual_mode != "DRY":
        ready = st.checkbox("✅ I have reviewed the preview — data looks correct") and ready
    if actual_mode in ("SHADOW", "LIVE"):
        ready = st.checkbox(f"✅ I confirm **{actual_mode}** mode") and ready
    if actual_stock_mode == "OPENING" and actual_mode != "DRY":
        ready = st.checkbox("✅ I understand OPENING will **OVERWRITE** existing quantities") and ready
    if actual_stock_mode == "PRICE_ONLY" and actual_mode != "DRY":
        ready = st.checkbox("✅ I confirm PRICE UPDATE only — stock quantities will **NOT** change") and ready
    if actual_mode == "LIVE" and diff and diff.new_columns:
        ready = st.checkbox("✅ I acknowledge schema changes — unknown columns will be ignored") and ready
    ready = ready and contract_ok

    if actual_mode == "LIVE":
        st.markdown('<div class="opening-warning">🔴 <strong>LIVE MODE</strong> — Production DB write. Do NOT re-import same file after success.</div>', unsafe_allow_html=True)
    if actual_stock_mode == "OPENING":
        st.markdown('<div class="opening-warning">⚠️ <strong>OPENING STOCK RESET</strong> — Quantities will be overwritten. Irreversible.</div>', unsafe_allow_html=True)
    if actual_stock_mode == "PRICE_ONLY":
        st.markdown('<div style="background:#dbeafe;border:1px solid #93c5fd;border-radius:8px;padding:10px 14px;margin:6px 0;color:#1e3a8a;font-weight:500;">💰 <strong>PRICE UPDATE MODE</strong> — Only PurchaseRate, SellingPrice and MRP will be updated. Stock quantities are untouched.</div>', unsafe_allow_html=True)

    actual_type_arg = None if force_type == "AUTO" else force_type
    btn_lbl = ("▶ Run DRY RUN (Validate Only)" if actual_mode == "DRY"
               else f"▶ Run {actual_mode} Import [{actual_stock_mode}]")

    run_btn = st.button(btn_lbl,
                        type="primary" if actual_mode == "LIVE" else "secondary",
                        disabled=not ready,
                        use_container_width=True)

    # ── STEP 5: IMPORT ──────────────────────────────────────────────────────
    if run_btn:
        _render_steps(5)
        with st.spinner(f"Running {actual_mode} [{actual_stock_mode}]..."):
            try:
                logger.debug("Calling loader: %s.%s", run_loader.__module__, run_loader.__name__)
                result = run_loader(tmp_path, mode=actual_mode,
                                    stock_mode=actual_stock_mode, force_type=actual_type_arg,
                                    user=(st.session_state.get("user") or {}).get("username", "admin") if isinstance(st.session_state.get("user"), dict) else str(st.session_state.get("user", "admin")),
                                    skip_dedup=True)

                st.markdown("---")
                st.markdown("### 🎯 Import Complete")
                _render_result(result.to_dict())

                # ── Ingestion Quality Score ──────────────────────────────
                rpt = getattr(result, "ingestion_report", None)
                if rpt:
                    score = rpt.score
                    score_colour = (
                        "🟢" if score >= 85 else
                        "🟡" if score >= 60 else
                        "🔴"
                    )
                    st.metric(
                        label=f"{score_colour} Excel Quality Score",
                        value=f"{score} / 100",
                        help="Score based on headers auto-fixed, empty columns, missing optional fields, etc."
                    )
                    shield_warnings = [
                        w for w in rpt.warnings
                        if any(kw in w for kw in ["normalized", "AI mapped", "Auto-added", "Dropped", "Channel", "empty"])
                    ]
                    if shield_warnings:
                        with st.expander(f"🛡️ Ingestion Shield — {len(shield_warnings)} auto-fix(es) applied"):
                            for w in shield_warnings:
                                st.write(f"• {w}")
                    else:
                        st.success("✅ Excel file was clean — no auto-fixes needed")

                # Show import ID for traceability
                if hasattr(result, "import_id") and result.import_id:
                    st.caption(f"🔑 Import ID: `{result.import_id}`")

                # Save schema history
                if diff and diff.has_changes() and save_schema_hist:
                    try:
                        save_schema_hist(
                            file_type,
                            uploaded.name,
                            diff,
                            approved_by=st.session_state.get("user", "admin"),
                        )
                    except Exception:
                        pass

                # Session history
                if "loader_history" not in st.session_state:
                    st.session_state.loader_history = []
                st.session_state.loader_history.insert(0, {
                    "file": uploaded.name, "type": result.file_type,
                    "mode": result.mode, "stock_mode": result.stock_mode,
                    "rows": result.total_rows, "inserted": result.inserted,
                    "updated": result.updated, "errors": len(result.errors),
                    "rate": f"{result.success_rate}%",
                    "schema_changes": len(diff.new_columns) if diff else 0,
                    "at": result.started_at.strftime("%d-%b %H:%M"),
                })

                # Post-import advisor
                rate = result.success_rate
                if rate >= 95:
                    if actual_mode == "LIVE":
                        st.success("✅ Excellent import! Go to Audit & Integrity → Run All Checks now.")
                    elif actual_mode == "SHADOW":
                        st.info("🔵 Shadow import complete. Verify in Audit tab, then switch to LIVE.")
                    else:
                        st.info("🧪 DRY RUN passed cleanly. Switch to SHADOW → then LIVE.")
                elif rate >= 70:
                    st.warning(f"⚠️ {rate}% success. Download error report, fix issues, re-import.")
                else:
                    st.error(f"🚨 Only {rate}% success. Something is fundamentally wrong. Check errors carefully.")

            except Exception as e:
                st.error(f"❌ Loader crashed: {e}")
                st.code(traceback.format_exc())
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


# ════════════════════════════════════════════════════════════
# TAB 2: DB EXPORT
# ════════════════════════════════════════════════════════════

def _tab_export():
    _, _, _, export_all_tables, export_master_report, _, _ = _get_core()

    st.markdown('<div class="section-head">Export DB to Excel</div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)

    with c1:
        st.markdown("""<div class="loader-card"><div style="font-size:20px">📊</div>
          <div style="font-weight:700">Master Report</div>
          <div style="font-size:12px;color:#6b7280">Products, Parties, Frames, Patients, Batches, Inventory</div>
        </div>""", unsafe_allow_html=True)
        if st.button("Export Master Report", use_container_width=True):
            with st.spinner("Exporting..."):
                out = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx").name
                ok, msg = export_master_report(out)
                if ok:
                    with open(out, "rb") as f:
                        st.download_button("📥 Download Master Report", f.read(),
                                           f"master_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                else:
                    st.error(f"Export failed: {msg}")

    with c2:
        st.markdown("""<div class="loader-card"><div style="font-size:20px">🗄️</div>
          <div style="font-weight:700">Full DB Export</div>
          <div style="font-size:12px;color:#6b7280">Every table — for backup or external analysis</div>
        </div>""", unsafe_allow_html=True)
        if st.button("Export Full DB", use_container_width=True):
            with st.spinner("Exporting all tables..."):
                out = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx").name
                ok, msg = export_all_tables(out)
                if ok:
                    with open(out, "rb") as f:
                        st.download_button("📥 Download Full DB", f.read(),
                                           f"dv_optical_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                else:
                    st.error(f"Export failed: {msg}")

    st.markdown("---")
    st.markdown('<div class="section-head">Database Row Counts</div>', unsafe_allow_html=True)
    try:
        from modules.sql_adapter import run_query
        tables_q = {"products": "SELECT COUNT(*) FROM products",
                    "frames": "SELECT COUNT(*) FROM frames",
                    "parties": "SELECT COUNT(*) FROM parties",
                    "patients": "SELECT COUNT(*) FROM patients",
                    "patient_visits": "SELECT COUNT(*) FROM patient_visits",
                    "inventory_stock": "SELECT COUNT(*) FROM inventory_stock",
                    "batches": "SELECT COUNT(*) FROM batches",
                    "blank_inventory": "SELECT COUNT(*) FROM blank_inventory",
                    "orders": "SELECT COUNT(*) FROM orders"}
        counts = {}
        for tbl, q in tables_q.items():
            try:
                r = run_query(q)
                counts[tbl] = r[0]["count"] if r else "?"
            except Exception:
                counts[tbl] = "N/A"
        cols = st.columns(3)
        for i, (tbl, cnt) in enumerate(counts.items()):
            with cols[i % 3]:
                st.metric(tbl.replace("_", " ").title(), f"{cnt:,}" if isinstance(cnt, int) else cnt)
    except Exception as e:
        st.warning(f"Could not load DB summary: {e}")


# ════════════════════════════════════════════════════════════
# TAB 3: AUDIT & INTEGRITY
# ════════════════════════════════════════════════════════════

def _tab_audit():
    st.markdown('<div class="section-head">Session Import History</div>', unsafe_allow_html=True)
    history = st.session_state.get("loader_history", [])
    if history:
        df_hist = pd.DataFrame(history)
        def _hl(row):
            if row.get("stock_mode") == "OPENING":
                return ["background-color:#fef9c3"] * len(row)
            if row.get("stock_mode") == "PRICE_ONLY":
                return ["background-color:#dbeafe"] * len(row)
            if row.get("mode") == "LIVE":
                return ["background-color:#f0fdf4"] * len(row)
            return [""] * len(row)
        st.dataframe(df_hist.style.apply(_hl, axis=1), use_container_width=True, hide_index=True)
        st.caption("🟡 = Opening reset | 🔵 = Price update | 🟢 = Live import")
    else:
        st.info("No imports in this session yet.")

    st.markdown("---")
    st.markdown('<div class="section-head">DB Integrity Checks</div>', unsafe_allow_html=True)
    if st.button("🔍 Run All Integrity Checks", type="secondary"):
        try:
            from modules.sql_adapter import run_query
            checks = []
            def _chk(label, q, good_fn=lambda v: v == 0, cat="Data"):
                try:
                    r = run_query(q)
                    v = list(r[0].values())[0] if r else 0
                    status = "✅ OK" if good_fn(v) else "⚠️ Issues"
                    checks.append({"Category": cat, "Check": label, "Value": v, "Status": status})
                except Exception as e:
                    checks.append({"Category": cat, "Check": label, "Value": "ERR", "Status": str(e)[:50]})
            _chk("Products no brand",    "SELECT COUNT(*) FROM products WHERE brand IS NULL",                                                              cat="Products")
            _chk("Products no HSN",      "SELECT COUNT(*) FROM products WHERE hsn_code IS NULL",                                                          cat="Products")
            _chk("Parties no mobile",    "SELECT COUNT(*) FROM parties WHERE mobile IS NULL",        lambda v: True,                                      cat="Parties")
            _chk("Batches zero qty",     "SELECT COUNT(*) FROM batches WHERE qty_available=0 AND is_active=true",                                          cat="Stock")
            _chk("Inventory qty=0",      "SELECT COUNT(*) FROM inventory_stock WHERE quantity=0 AND is_active=true",                                      cat="Stock")
            _chk("TORIC missing axis",   "SELECT COUNT(*) FROM inventory_stock WHERE cyl IS NOT NULL AND cyl!=0 AND (axis IS NULL OR axis=0)",            cat="Stock")
            _chk("Frames no MRP",        "SELECT COUNT(*) FROM frames WHERE mrp IS NULL OR mrp=0",                                                        cat="Frames")
            _chk("Orders zero tax",      "SELECT COUNT(*) FROM orders WHERE tax_amount=0 AND final_value>0",                                              cat="Orders")
            _chk("Patient visits no date","SELECT COUNT(*) FROM patient_visits WHERE visit_date IS NULL", lambda v: True,                                 cat="Patients")
            _chk("Discount rules active","SELECT COUNT(*) FROM discount_rules WHERE active=true",     lambda v: v > 0,                                   cat="Pricing")
            _chk("Shadow orders",        "SELECT COUNT(*) FROM orders WHERE environment_tag='SHADOW'", lambda v: True,                                    cat="Shadow")
            df_chk = pd.DataFrame(checks)
            st.dataframe(df_chk, use_container_width=True, hide_index=True)
            issues = [c for c in checks if "⚠️" in c["Status"]]
            if issues:
                st.warning(f"⚠️ {len(issues)} issue(s) found")
                for iss in issues:
                    st.markdown(f"- **{iss['Check']}**: {iss['Value']} rows")
            else:
                st.success("✅ All integrity checks passed")
        except Exception as e:
            st.error(f"Integrity check failed: {e}")

    st.markdown("---")
    st.markdown('<div class="section-head">Null Audit — by Table</div>', unsafe_allow_html=True)
    if st.button("📊 Run Null Audit", type="secondary"):
        try:
            from modules.sql_adapter import run_query
            tables = run_query("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
            tabs = [r["table_name"] for r in tables]
            if tabs:
                sel = st.selectbox("Select table", tabs)
                cols_r = run_query("SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position", (sel,))
                total_r = run_query(f"SELECT COUNT(*) AS t FROM {sel}")
                total = total_r[0]["t"] if total_r else 1
                null_data = []
                for c in cols_r:
                    col = c["column_name"]
                    r = run_query(f"SELECT COUNT(*) AS n FROM {sel} WHERE {col} IS NULL")
                    n = r[0]["n"] if r else 0
                    if n > 0:
                        null_data.append({"Column": col, "Null Count": n, "Null %": f"{round(n/total*100,1)}%"})
                if null_data:
                    st.dataframe(pd.DataFrame(null_data).sort_values("Null Count", ascending=False), use_container_width=True, hide_index=True)
                else:
                    st.success(f"✅ No nulls in {sel}")
        except Exception as e:
            st.error(f"Null audit failed: {e}")


# ════════════════════════════════════════════════════════════
# TAB 4: SCHEMA HISTORY
# ════════════════════════════════════════════════════════════

def _tab_schema_history():
    st.markdown('<div class="section-head">Schema Change History</div>', unsafe_allow_html=True)
    st.caption("Records every schema change detected during import (requires loader_schema_history table).")
    try:
        from modules.loaders.schema_guard import get_schema_history
        col_ft, col_lim = st.columns([2, 1])
        with col_ft:
            ft_filter = st.selectbox("Filter by File Type", ["ALL"] + list(FILE_TYPE_META.keys()))
        with col_lim:
            limit = st.number_input("Max records", 10, 100, 20)
        history = get_schema_history(None if ft_filter == "ALL" else ft_filter, limit)
        if not history:
            st.info("No schema history found. History records schema changes detected during import.")
            return
        rows = []
        for h in history:
            summary = h.get("change_summary", {})
            if isinstance(summary, str):
                try:
                    summary = json.loads(summary)
                except Exception:
                    summary = {}
            rows.append({
                "File Type": h.get("file_type",""), "File Name": h.get("file_name",""),
                "New Cols": len(summary.get("new_columns",[])),
                "Missing Cols": len(summary.get("missing_columns",[])),
                "Newly Filled": len(summary.get("newly_filled",[])),
                "At": str(h.get("approved_at",""))[:16],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        if history:
            idx = st.selectbox("View detail", range(len(history)),
                               format_func=lambda i: f"{history[i].get('file_name','')} @ {str(history[i].get('approved_at',''))[:16]}")
            s = history[idx].get("change_summary", {})
            if isinstance(s, str):
                try:
                    s = json.loads(s)
                except Exception:
                    pass
            st.json(s)
    except Exception as e:
        st.warning(f"Schema history unavailable: {e}")
        st.caption("Create the table via Admin tab → 'Create loader_schema_history table'")


# ════════════════════════════════════════════════════════════
# TAB 5: ADMIN (Flags + Health)
# ════════════════════════════════════════════════════════════

def _suggest_excel_header_ui(db_col: str) -> str:
    """Convert db_column_name → ExcelHeaderSuggestion for display."""
    return db_col.replace("_", " ").title().replace(" ", "")


def _tab_admin():
    get_flag, get_all_flags, set_flag, ensure_flags_table = _get_flags()

    # ════════════════════════════════════════════════════════════════════════
    # 🔄 SCHEMA SYNC FROM DB
    # ════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-head">🔄 Schema Registry — Sync from DB</div>', unsafe_allow_html=True)
    st.caption(
        "Queries live DB (`information_schema.columns`), diffs against `db_schema_registry.py`, "
        "and lets you add any new DB columns into the registry so they appear in the downloader, "
        "upload column mapping panel, and schema reference automatically."
    )

    col_scan, col_apply = st.columns([1, 2])

    with col_scan:
        scan_btn = st.button("🔍 Scan DB for New Columns", type="secondary", use_container_width=True)

    if scan_btn or st.session_state.get("_sync_diff"):
        try:
            from modules.loaders.schema_sync import diff_schema, run_full_sync
        except ImportError:
            st.error("❌ schema_sync.py not found. Place it at: `modules/loaders/schema_sync.py`")
            st.stop()

        with st.spinner("Scanning DB schema..."):
            try:
                diff = diff_schema()
                st.session_state["_sync_diff"] = diff
            except Exception as e:
                st.error(f"Scan failed: {e}")
                diff = st.session_state.get("_sync_diff")

        diff = st.session_state.get("_sync_diff", {})
        if not diff:
            st.info("Run scan first.")
        else:
            new_cols     = diff.get("new_columns", [])
            removed_cols = diff.get("removed_columns", [])
            type_changes = diff.get("type_changed", [])
            specials     = diff.get("special", [])
            in_sync      = diff.get("in_sync", [])

            # Summary row
            sc1, sc2, sc3, sc4, sc5 = st.columns(5)
            sc1.metric("✅ In Sync",      len(in_sync))
            sc2.metric("🆕 New in DB",    len(new_cols),     delta=len(new_cols) or None,     delta_color="off")
            sc3.metric("🗑️ Removed",      len(removed_cols), delta=-len(removed_cols) or None, delta_color="off")
            sc4.metric("🔀 Type Changed", len(type_changes))
            sc5.metric("⚠️ Manual Review", len(specials))

            # ── NEW COLUMNS ───────────────────────────────────────────────
            if new_cols:
                st.markdown("---")
                st.markdown("#### 🆕 New columns found in DB — not yet in registry")
                st.caption("Select which ones to add. Uncheck any you want to manage manually.")

                # Build selection checkboxes
                selected = []
                for nc in new_cols:
                    col1, col2, col3, col4, col5 = st.columns([1, 2, 2, 2, 3])
                    checked = col1.checkbox("", value=True, key=f"sync_chk_{nc['file_type']}_{nc['db_column']}")
                    col2.markdown(f'<span style="font-family:monospace;font-size:12px;color:#1e3a5f"><b>{nc["db_column"]}</b></span>', unsafe_allow_html=True)
                    col3.markdown(f'`{nc["db_type"]}`')
                    col4.markdown(f'→ `{nc["file_type"]}` / `{nc["table"]}`')
                    # Editable excel header suggestion
                    suggested = nc.get("suggested_excel_header", nc["db_column"])
                    override  = col5.text_input(
                        "Excel header",
                        value=suggested,
                        key=f"sync_hdr_{nc['file_type']}_{nc['db_column']}",
                        label_visibility="collapsed",
                    )
                    if checked:
                        nc_copy = dict(nc)
                        nc_copy["suggested_excel_header"] = override
                        selected.append(nc_copy)

                st.markdown("")
                btn_col1, btn_col2, btn_col3 = st.columns([2, 2, 3])

                with btn_col1:
                    dry_btn = st.button("🧪 Preview Changes (Dry Run)", use_container_width=True, type="secondary")
                with btn_col2:
                    apply_btn = st.button(
                        f"✅ Add {len(selected)} Column(s) to Registry",
                        use_container_width=True,
                        type="primary",
                        disabled=(len(selected) == 0),
                    )

                if dry_btn and selected:
                    result = run_full_sync(selected, dry_run=True)
                    st.info(f"**Dry Run:** {result['message']}")
                    if result["added_lines"]:
                        st.code("\n".join(result["added_lines"]), language="python")

                if apply_btn and selected:
                    with st.spinner(f"Writing {len(selected)} column(s) to registry..."):
                        result = run_full_sync(selected, dry_run=False)

                    if result["success"]:
                        st.success(result["message"])
                        if result["added_lines"]:
                            with st.expander("📋 Lines added to db_schema_registry.py"):
                                st.code("\n".join(result["added_lines"]), language="python")
                        if result["reloaded"]:
                            st.success(f"♻️ {result['reload_msg']}")
                            st.info("💡 New columns are now live in the downloader, upload panel, and schema reference. No restart needed.")
                        else:
                            st.warning(f"Registry written but reload failed: {result['reload_msg']} — restart the app to pick up changes.")
                        # Clear cached diff so next scan is fresh
                        st.session_state.pop("_sync_diff", None)
                        st.rerun()
                    else:
                        st.error(f"Sync failed: {result['message']}")
                        st.warning("You can add the column manually to `db_schema_registry.py`. See dry run output above for the exact line to add.")

            elif not any([removed_cols, type_changes, specials]):
                st.success("✅ Registry is fully in sync with the DB — no new columns found.")

            # ── REMOVED COLUMNS ───────────────────────────────────────────
            if removed_cols:
                st.markdown("---")
                with st.expander(f"🗑️ {len(removed_cols)} column(s) in registry but NOT in DB (read only)", expanded=False):
                    st.caption("These were likely removed from the DB. Remove them manually from db_schema_registry.py if no longer needed.")
                    for rc in removed_cols:
                        st.markdown(f"- `{rc['db_column']}` in **{rc['file_type']}** / `{rc['table']}`")

            # ── TYPE CHANGES ──────────────────────────────────────────────
            if type_changes:
                st.markdown("---")
                with st.expander(f"🔀 {len(type_changes)} type mismatch(es) — review manually", expanded=False):
                    st.caption("DB type differs from registry. Update the db_type in db_schema_registry.py manually.")
                    for tc in type_changes:
                        st.markdown(
                            f"- `{tc['db_column']}` ({tc['file_type']}) — "
                            f"registry: `{tc['registry_type']}` → DB: `{tc['db_type']}`"
                        )

            # ── SPECIAL / MANUAL ──────────────────────────────────────────
            if specials:
                st.markdown("---")
                with st.expander(f"⚠️ {len(specials)} column(s) need manual review (inventory_stock split)", expanded=False):
                    st.caption("inventory_stock is shared between OPHLENS and CLENS. Add new columns to one or both manually.")
                    for sp in specials:
                        st.markdown(
                            f"- `{sp['db_column']}` (`{sp['db_type']}`) in `{sp['table']}` — {sp['note']}"
                        )
                        st.code(
                            f'Col("{_suggest_excel_header_ui(sp["db_column"])}", '
                            f'"{sp["db_column"]}", "{sp["db_type"]}", '
                            f'description="", example="", notes="Add to OPHLENS and/or CLENS block"),',
                            language="python"
                        )

    st.markdown("---")
    st.markdown('<div class="section-head">🏥 System Health</div>', unsafe_allow_html=True)
    if st.button("🔄 Run Health Check", type="secondary"):
        try:
            from modules.system_health import get_module_health, get_health_summary
            with st.spinner("Checking all systems..."):
                checks = get_module_health()
                summary = get_health_summary(checks)
            icon = {"healthy": "✅", "degraded": "⚠️", "critical": "🚨"}.get(summary["status"], "❓")
            st.markdown(f"### {icon} {summary['status'].upper()} — {summary['ok']}/{summary['total']} systems OK")
            cols = st.columns(3)
            for i, (name, chk) in enumerate(checks.items()):
                with cols[i % 3]:
                    ind = "🟢" if chk["ok"] else "🔴"
                    st.markdown(f"""<div class="loader-card">
                      <div>{ind} <strong>{chk['label']}</strong></div>
                      <div style="font-size:11px;color:#6b7280">{chk['detail']}</div>
                      <div style="font-size:10px;color:#9ca3af">{chk['ms']}ms</div>
                    </div>""", unsafe_allow_html=True)
        except ImportError:
            st.warning("system_health.py not found — place at modules/system_health.py")
        except Exception as e:
            st.error(f"Health check failed: {e}")

    st.markdown("---")
    st.markdown('<div class="section-head">🔒 Submit Lock Monitor</div>', unsafe_allow_html=True)
    st.caption("Shows active double-click locks across punching and backoffice. Force-unlock if a button is stuck.")
    try:
        from modules.utils.submit_guard import debug_locks_panel
        debug_locks_panel()
    except ImportError:
        st.warning("submit_guard not found — place at modules/utils/submit_guard.py")

    st.markdown("---")
    # ── Observability ─────────────────────────────────────────────────────────
    st.markdown("---")
    with st.expander("📡 Observability — Errors & Performance", expanded=False):
        try:
            from modules.ui.system_health import render_observability_panel
            render_observability_panel()
        except ImportError:
            st.warning("system_health not updated — replace modules/ui/system_health.py")

    # ── Submit Lock Monitor ──────────────────────────────────────────────────
    st.markdown("---")
    with st.expander("🔒 Submit Lock Monitor", expanded=False):
        st.caption("Active double-click locks across punching and backoffice. Force-unlock if a button is stuck.")
        try:
            from modules.utils.submit_guard import debug_locks_panel
            debug_locks_panel()
        except ImportError:
            st.warning("submit_guard not found — place at modules/utils/submit_guard.py")

    # ── Deleted Orders Recovery ───────────────────────────────────────────────
    st.markdown("---")
    with st.expander("🗑️ Deleted Orders (Soft Delete Recovery)", expanded=False):
        st.caption("Recover orders that were soft-deleted. Admin/Manager only.")
        try:
            from modules.core.erp_stability import render_deleted_orders_panel
            render_deleted_orders_panel()
        except ImportError:
            st.warning("erp_stability not found — place at modules/core/erp_stability.py")

    # ── Audit Trail Viewer ────────────────────────────────────────────────────
    st.markdown("---")
    with st.expander("📋 Audit Trail Viewer", expanded=False):
        st.caption("Complete log of all order, billing, price and stock events.")
        try:
            from modules.core.erp_stability import render_audit_trail_panel
            render_audit_trail_panel(limit=200)
        except ImportError:
            st.warning("erp_stability not found — run SQL migration first.")

    st.markdown("---")
    st.markdown('<div class="section-head">🔒 Data Integrity Checks</div>', unsafe_allow_html=True)
    st.caption("Run these weekly to catch data quality issues before they cause problems.")

    if st.button("🔍 Run Integrity Checks", type="secondary"):
        try:
            from modules.sql_adapter import run_query

            ic1, ic2, ic3 = st.columns(3)

            # 1 — Duplicate products (spacing variants)
            with ic1:
                try:
                    rows = run_query("""
                        SELECT COUNT(*) AS n FROM (
                            SELECT LOWER(REGEXP_REPLACE(product_name, '\\s+', ' ', 'g'))
                            FROM products
                            GROUP BY 1 HAVING COUNT(*) > 1
                        ) t
                    """)
                    n = rows[0]["n"] if rows else 0
                    if n == 0:
                        st.success("✅ No duplicate products")
                    else:
                        st.error(f"❌ {n} duplicate product name(s)")
                        dups = run_query("""
                            SELECT LOWER(REGEXP_REPLACE(product_name,'\\s+', ' ','g')) AS name, COUNT(*) AS cnt
                            FROM products
                            GROUP BY 1 HAVING COUNT(*) > 1 LIMIT 10
                        """)
                        for d in (dups or []):
                            st.write(f"  `{d['name']}` × {d['cnt']}")
                except Exception as e:
                    st.error(f"Duplicate check failed: {e}")

            # 2 — Orphan stock rows (product_id not in products)
            with ic2:
                try:
                    rows = run_query("""
                        SELECT COUNT(*) AS n FROM inventory_stock s
                        WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.id = s.product_id)
                    """)
                    n = rows[0]["n"] if rows else 0
                    if n == 0:
                        st.success("✅ No orphan stock rows")
                    else:
                        st.error(f"❌ {n} orphan stock row(s) — product deleted but stock remains")
                except Exception as e:
                    st.error(f"Orphan check failed: {e}")

            # 3 — Products added in last 7 days (audit)
            with ic3:
                try:
                    rows = run_query("""
                        SELECT product_name, created_at::date AS added
                        FROM products
                        WHERE created_at > NOW() - INTERVAL '7 days'
                        ORDER BY created_at DESC LIMIT 10
                    """)
                    if not rows:
                        st.info("ℹ️ No new products this week")
                    else:
                        st.warning(f"⚠️ {len(rows)} product(s) added this week — verify intentional")
                        for r in rows:
                            st.write(f"  `{r['product_name']}` ({r['added']})")
                except Exception as e:
                    st.error(f"New product audit failed: {e}")

            # FK check
            st.markdown("**Foreign Key Status**")
            try:
                fk_rows = run_query("""
                    SELECT conname, conrelid::regclass AS child_table
                    FROM pg_constraint
                    WHERE confrelid = 'products'::regclass AND contype = 'f'
                    ORDER BY conrelid::regclass::text
                """)
                expected = {"inventory_stock", "order_lines", "batches"}
                found    = {str(r["child_table"]) for r in (fk_rows or [])}
                for tbl in sorted(expected):
                    if tbl in found:
                        st.write(f"  ✅ `{tbl}` → `products` FK exists")
                    else:
                        st.write(f"  ⚠️ `{tbl}` → `products` FK **missing** — add in pgAdmin")
            except Exception as e:
                st.error(f"FK check failed: {e}")

        except Exception as e:
            st.error(f"Integrity check failed: {e}")

    st.markdown("---")
    st.markdown('<div class="section-head">🚩 Feature Flags</div>', unsafe_allow_html=True)
    try:
        ensure_flags_table()
    except Exception:
        pass

    FLAG_META = {
        "loader.opening_enabled":   ("Opening Stock Reset Mode",      "Allow OPENING mode. Disable after go-live."),
        "loader.schema_guard":      ("Schema Guard Engine",           "Run schema diff before every import. Keep ON."),
        "loader.preview_required":  ("Preview Confirmation Required", "Force preview checkbox before LIVE import."),
        "loader.strict_mode":       ("Strict Schema Mode",            "Block import if any unknown column found."),
        "loader.auto_schema":       ("Auto Schema Suggestions",       "Fuzzy-match suggestions for unknown columns."),
        "loader.ai_advisor":        ("AI Import Advisor",             "Show AI advice panel during import."),
        "loader.lazy_load":         ("Lazy Load Core",                "Load loader only when page opened (faster boot)."),
    }

    for key, (name, desc) in FLAG_META.items():
        current = get_flag(key, True)
        col_n, col_t, col_d = st.columns([2, 1, 4])
        with col_n:
            badge = f'<span class="flag-on">ON</span>' if current else f'<span class="flag-off">OFF</span>'
            st.markdown(f"**{name}** {badge}", unsafe_allow_html=True)
        with col_t:
            new_val = st.toggle(name or key, value=current, label_visibility="collapsed", key=f"flag_{key}")
            if new_val != current:
                if set_flag(key, new_val):
                    st.rerun()
                else:
                    st.caption("⚠️ Memory only — DB write failed")
        with col_d:
            st.caption(desc)

    st.markdown("---")
    st.markdown('<div class="section-head">🗄️ DB Table Setup</div>', unsafe_allow_html=True)
    st.caption("Run once on initial setup to create required tables.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Create system_flags table", use_container_width=True):
            ok = ensure_flags_table()
            st.success("✅ system_flags ready") if ok else st.warning("Check DB permissions")
    with c2:
        if st.button("Create schema_history table", use_container_width=True):
            try:
                from modules.sql_adapter import run_write
                run_write("""CREATE TABLE IF NOT EXISTS loader_schema_history (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    file_type TEXT, file_name TEXT,
                    change_summary JSONB,
                    approved_by TEXT DEFAULT 'user',
                    approved_at TIMESTAMP DEFAULT NOW()
                )""")
                # Add file_name column if table exists but column was missing
                run_write("""
                    ALTER TABLE loader_schema_history
                    ADD COLUMN IF NOT EXISTS file_name TEXT
                """)
                st.success("✅ loader_schema_history ready")
            except Exception as e:
                st.error(f"Failed: {e}")


# ════════════════════════════════════════════════════════════
# TAB 6: SCHEMA REFERENCE
# ════════════════════════════════════════════════════════════

def _tab_schema_ref():
    st.markdown('<div class="section-head">Excel Schema Contract</div>', unsafe_allow_html=True)

    with st.expander("📖 Stock Mode Reference", expanded=False):
        st.markdown("""
**ADD** *(default — always safe)*: `new_qty = existing + excel`  
**OPENING** *(admin-controlled)*: `new_qty = excel` (overwrites)  
Applies to: OPHLENS, CLENS, BLANK, FRAME. No effect on masters.  
Lock via: `loader.opening_enabled = false` in Admin tab.
        """)

    with st.expander("📚 Common Error Dictionary", expanded=False):
        errors = [
            ("Product not found in DB",        "OPHLENS/CLENS/SOL", "Import product_master.xlsx first, then stock files."),
            ("TORIC CYL but AXIS missing",      "OPHLENS/CLENS",     "Fill AXIS for every row where CYL is non-zero. Range: 1–180."),
            ("Both mobile and record_no empty", "PATIENT",           "Add at least one identity field — mobile or record_no."),
            ("Duplicate SKU in Excel",          "FRAME",             "Same sku_code appears twice. Fix source Excel."),
            ("Missing required column",         "ALL",               "Add missing column to Excel. Check Schema Reference."),
            ("Invalid ADD value >99.99",        "BLANK",             "Data entry error. Max realistic ADD power is ~4.00."),
            ("Cannot detect file type",         "ALL",               "Use Force Type dropdown. Check column names."),
            ("Zero or missing quantity",        "OPHLENS/CLENS",     "Quantity is 0 or blank. Fill or remove row."),
            ("OPENING mode disabled",           "All stock types",   "Enable via Admin tab → Feature Flags → Opening Stock Reset Mode."),
        ]
        st.dataframe(pd.DataFrame(errors, columns=["Error", "Affects", "Fix"]), use_container_width=True, hide_index=True)

    for ftype, contract in SCHEMA_CONTRACT.items():
        meta = FILE_TYPE_META.get(ftype.split("/")[0].strip(), {})
        with st.expander(f"{meta.get('icon','📄')}  {ftype}", expanded=False):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Required 🔴**")
                for col in contract["required"]:
                    st.markdown(f"- `{col}`")
            with c2:
                st.markdown("**Optional**")
                for col in contract["optional"]:
                    st.markdown(f"- `{col}`")
            if contract["enums"]:
                st.markdown("**Allowed Values**")
                for field, vals in contract["enums"].items():
                    st.markdown(f"- `{field}`: {', '.join(vals)}")
            st.info(f"📝 {contract['notes']}")


# ════════════════════════════════════════════════════════════
# TAB 0: DOWNLOAD & EDIT
# DB → Download Excel → Edit in Excel → Upload & Import → DB updated
# ════════════════════════════════════════════════════════════

def _tab_download():
    DOWNLOAD_CONFIGS, fetch_for_download, build_download_excel, compute_diff, get_key_cols = _get_downloader()

    if DOWNLOAD_CONFIGS is None:
        st.error("❌ data_downloader.py not found.")
        st.caption("Place it at: `modules/loaders/data_downloader.py`")
        return

    # ── Workflow banner ──────────────────────────────────────────────────────
    st.markdown("""<div class="dl-workflow">
      <div class="dl-wf-title">📥 Download → Edit → Re-import Workflow</div>
      <div class="dl-wf-step">1️⃣  Choose a dataset and apply filters</div>
      <div class="dl-wf-step">2️⃣  Download the Excel — columns are pre-formatted for the loader</div>
      <div class="dl-wf-step">3️⃣  Open in Excel — edit values, add rows, set IsActive=NO to deactivate</div>
      <div class="dl-wf-step">4️⃣  <strong>Do NOT rename column headers</strong></div>
      <div class="dl-wf-step">5️⃣  Save and go to <strong>Upload &amp; Import</strong> tab → DRY RUN → LIVE</div>
      <div class="dl-wf-step">6️⃣  Optionally: upload edited file below to see exactly what changed before importing</div>
    </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # ── Dataset selector cards ───────────────────────────────────────────────
    st.markdown('<div class="section-head">1️⃣  Choose Dataset to Download</div>', unsafe_allow_html=True)

    current_key = st.session_state.get("dl_key")
    card_cols   = st.columns(4)

    for i, (key, cfg) in enumerate(DOWNLOAD_CONFIGS.items()):
        with card_cols[i % 4]:
            is_sel = current_key == key
            card_cls = "dl-card-sel" if is_sel else "dl-card"
            st.markdown(f"""<div class="{card_cls}">
              <div class="dl-card-icon">{cfg['icon']}</div>
              <div class="dl-card-title">{cfg['label']}</div>
              <div class="dl-card-desc">{cfg['desc'][:60]}...</div>
              <div class="dl-card-key">🔑 {cfg['key_col'][:40]}</div>
            </div>""", unsafe_allow_html=True)
            if st.button(f"Select {cfg['label']}", key=f"dl_pick_{key}", use_container_width=True,
                         type="primary" if is_sel else "secondary"):
                st.session_state["dl_key"] = key
                # Clear stale data
                for sk in ["dl_df", "dl_meta", "dl_bytes"]:
                    st.session_state.pop(sk, None)
                st.rerun()

    if not current_key:
        st.info("👆 Select a dataset above to begin")
        return

    cfg = DOWNLOAD_CONFIGS[current_key]
    st.markdown(f"**Selected:** {cfg['icon']} {cfg['label']}")
    st.markdown("---")

    # ── Filters ──────────────────────────────────────────────────────────────
    st.markdown('<div class="section-head">2️⃣  Apply Filters</div>', unsafe_allow_html=True)

    filters_def = cfg.get("filters", [])
    filter_vals = {}

    if filters_def:
        fcols = st.columns(min(len(filters_def), 4))
        for i, fdef in enumerate(filters_def):
            with fcols[i % len(fcols)]:
                fname = fdef["name"]
                ftype = fdef["type"]
                flab  = fdef["label"]
                fkey  = f"dl_flt_{current_key}_{fname}"

                if ftype == "checkbox":
                    filter_vals[fname] = st.checkbox(flab, value=bool(fdef.get("default", False)), key=fkey)
                elif ftype == "text":
                    filter_vals[fname] = st.text_input(flab, value=str(fdef.get("default", "")), key=fkey)
                elif ftype == "select":
                    opts = fdef.get("options", ["ALL"])
                    filter_vals[fname] = st.selectbox(flab, opts, key=fkey)
                elif ftype == "date":
                    filter_vals[fname] = st.text_input(flab + " (YYYY-MM-DD)", value="", key=fkey)

    lc1, lc2 = st.columns([1, 3])
    with lc1:
        row_limit = st.number_input("Max rows", 100, 100000, 10000, step=1000,
                                    help="Increase for full export")
    with lc2:
        st.markdown("<br>", unsafe_allow_html=True)
        fetch_btn = st.button("🔍  Fetch Preview", use_container_width=True, type="secondary")

    if fetch_btn:
        with st.spinner(f"Fetching {cfg['label']} from DB..."):
            try:
                df, meta = fetch_for_download(current_key, filter_vals, limit=row_limit)
                st.session_state["dl_df"]   = df
                st.session_state["dl_meta"] = meta
                st.session_state.pop("dl_bytes", None)
            except Exception as e:
                st.error(f"❌ Fetch failed: {e}")
                st.code(traceback.format_exc())
                return

    df   = st.session_state.get("dl_df")
    meta = st.session_state.get("dl_meta")

    if df is None:
        st.info("Set your filters and click **Fetch Preview**")
        return

    if df.empty:
        st.warning("⚠️ No rows matched your filters. Try relaxing the conditions.")

        # ── Debug panel — registry-driven, shows actual SQL built by build_query ──
        with st.expander("🔬 Debug — Why no rows?", expanded=True):
            try:
                from modules.sql_adapter import run_query
                from modules.loaders.data_downloader import DOWNLOAD_CONFIGS, build_query, _build_where

                _cfg = DOWNLOAD_CONFIGS.get(current_key, {})

                # Build the ACTUAL sql that was used (same call as fetch_for_download)
                _sql = build_query(current_key, _cfg, filter_vals, limit=50)
                st.markdown("**SQL that ran:**")
                st.code(_sql.strip(), language="sql")

                # ── Raw table counts (no filters) ────────────────────────────
                _tbl_map = {
                    "PARTY":   "parties",
                    "PRODUCT": "products",
                    "OPHLENS": "inventory_stock",
                    "CLENS":   "inventory_stock",
                    "FRAME":   "frames",
                    "PATIENT": "patients",
                    "SOL":     "batches",
                    "BLANK":   "blank_inventory",
                }
                tbl = _tbl_map.get(current_key)
                if tbl:
                    try:
                        cnt = run_query(f"SELECT COUNT(*) AS n FROM {tbl}")
                        st.metric(f"Total rows in `{tbl}` (no filters)", cnt[0]["n"] if cnt else "error")
                    except Exception as ce:
                        st.error(f"Count query failed: {ce}")

                # ── Dataset-specific diagnostics ─────────────────────────────
                if current_key == "OPHLENS":
                    try:
                        dist = run_query(
                            "SELECT s.stock_type, p.main_group, COUNT(*) AS n "
                            "FROM inventory_stock s JOIN products p ON p.id = s.product_id "
                            "GROUP BY s.stock_type, p.main_group ORDER BY n DESC"
                        )
                        if dist:
                            st.markdown("**`stock_type` × `main_group` distribution:**")
                            for row in dist:
                                match = "✅" if "OPHTHALMIC" in str(row["main_group"]).upper() else "⚠️"
                                st.write(f"  {match} stock_type=`{row['stock_type']}` main_group=`{row['main_group']}` → {row['n']} rows")
                            st.info("The downloader filters `UPPER(main_group) = 'OPHTHALMIC LENSES'`. "
                                    "If your data shows a different main_group above, import ophthalmic lens stock first.")
                        else:
                            st.info("inventory_stock is empty — import ophlens_batch.xlsx via Upload & Import tab first.")
                    except Exception as de:
                        st.error(f"Distribution query failed: {de}")

                elif current_key == "CLENS":
                    try:
                        dist = run_query(
                            "SELECT s.stock_type, p.main_group, COUNT(*) AS n "
                            "FROM inventory_stock s JOIN products p ON p.id = s.product_id "
                            "GROUP BY s.stock_type, p.main_group ORDER BY n DESC"
                        )
                        if dist:
                            st.markdown("**`stock_type` × `main_group` distribution:**")
                            for row in dist:
                                match = "✅" if "CONTACT" in str(row["main_group"]).upper() else "⚠️"
                                st.write(f"  {match} stock_type=`{row['stock_type']}` main_group=`{row['main_group']}` → {row['n']} rows")
                        else:
                            st.info("inventory_stock is empty.")
                    except Exception as de:
                        st.error(f"Distribution query failed: {de}")

                elif current_key == "PARTY":
                    try:
                        total_r = run_query("SELECT COUNT(*) AS n FROM parties")
                        total   = total_r[0]["n"] if total_r else 0
                        if total == 0:
                            st.warning("⚠️ parties table is empty — import a party_master.xlsx first.")
                        else:
                            st.metric("Total parties", total)
                            # is_active distribution
                            dist = run_query(
                                "SELECT is_active, COUNT(*) AS n FROM parties GROUP BY is_active ORDER BY is_active DESC"
                            )
                            if dist:
                                st.markdown("**`is_active` breakdown:**")
                                for row in dist:
                                    icon = "🟢" if row["is_active"] else "🔴"
                                    st.write(f"  {icon} is_active=`{row['is_active']}` → {row['n']} rows")
                            # Party type distribution
                            types = run_query(
                                "SELECT party_type, COUNT(*) AS n FROM parties GROUP BY party_type ORDER BY n DESC"
                            )
                            if types:
                                st.markdown("**Party type breakdown:**")
                                for row in types:
                                    st.write(f"  • {row['party_type'] or '(blank)'}: {row['n']}")
                            # GST / compliance coverage
                            cov = run_query("""
                                SELECT
                                    COUNT(*) FILTER (WHERE gstin IS NOT NULL AND gstin != '') AS with_gstin,
                                    COUNT(*) FILTER (WHERE pan_no IS NOT NULL AND pan_no != '') AS with_pan,
                                    COUNT(*) FILTER (WHERE mobile IS NOT NULL AND mobile != '') AS with_mobile,
                                    COUNT(*) FILTER (WHERE credit_limit > 0) AS with_credit
                                FROM parties
                            """)
                            if cov:
                                r = cov[0]
                                st.markdown("**Field coverage:**")
                                st.write(f"  📱 Mobile: {r.get('with_mobile',0)} / {total}")
                                st.write(f"  🔖 GSTIN: {r.get('with_gstin',0)} / {total}")
                                st.write(f"  📋 PAN: {r.get('with_pan',0)} / {total}")
                                st.write(f"  💳 Credit Limit set: {r.get('with_credit',0)} / {total}")
                    except Exception as de:
                        st.error(f"Distribution query failed: {de}")

                elif current_key == "SOL":
                    try:
                        cnt = run_query("SELECT COUNT(*) AS n FROM batches")
                        n = cnt[0]["n"] if cnt else 0
                        if n == 0:
                            st.info("ℹ️ **batches table is empty** — import Sol_batch.xlsx via Upload & Import tab first.")
                        else:
                            st.metric("Rows in batches table", n)
                    except Exception:
                        pass

                elif current_key == "BLANK":
                    try:
                        cnt = run_query("SELECT COUNT(*) AS n FROM blank_inventory")
                        n = cnt[0]["n"] if cnt else 0
                        if n == 0:
                            st.info("ℹ️ **blank_inventory table is empty** — import Blank_Inventory_Template.xlsx first.")
                        else:
                            st.metric("Rows in blank_inventory", n)
                    except Exception:
                        pass

                # ── Filter values actually used ───────────────────────────────
                active_filters = {k: v for k, v in filter_vals.items()
                                  if v not in (False, "", "ALL", None)}
                if active_filters:
                    st.markdown("**Active filters that restricted results:**")
                    for k, v in active_filters.items():
                        st.write(f"  `{k}` = `{v}`")
                    st.caption("Try unchecking 'Active only' or clearing text filters above.")
                else:
                    st.caption("No filters active — the table itself is likely empty or the join returned no matches.")

            except Exception as dbg_e:
                st.error(f"Debug panel error: {dbg_e}")
                import traceback as _tb
                st.code(_tb.format_exc())
        return

    # ── Preview ──────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="section-head">3️⃣  Preview & Download</div>', unsafe_allow_html=True)

    pc1, pc2, pc3 = st.columns(3)
    pc1.metric("Rows fetched",   f"{meta['rows']:,}")
    pc2.metric("Columns",        len(df.columns))
    pc3.metric("Loader type",    meta["loader_type"])

    # Edit guide
    guide = cfg.get("edit_guide", "")
    if guide:
        st.markdown(f'<div class="edit-guide">✏️ {guide}</div>', unsafe_allow_html=True)

    # Key column callout
    st.markdown(f"🔑 **Key column(s):** `{cfg['key_col']}` — do **NOT** rename or change these values when editing")

    # Data preview
    with st.expander(f"📋 Data preview — first 20 rows of {meta['rows']:,}", expanded=True):
        st.dataframe(df.head(20), use_container_width=True)

    # Column list
    with st.expander("🔍 Column names in this download", expanded=False):
        col_df = pd.DataFrame({
            "Excel Column Name": df.columns.tolist(),
            "Note": (["🔑 KEY — do not change" if c == df.columns[0] else "✏️ Editable"
                      for c in df.columns]),
        })
        st.dataframe(col_df, use_container_width=True, hide_index=True)
        st.caption("These column names are pre-matched to the loader — upload this file directly after editing.")

    # Build Excel
    if "dl_bytes" not in st.session_state:
        with st.spinner("Building formatted Excel..."):
            try:
                st.session_state["dl_bytes"] = build_download_excel(df, meta)
            except Exception as e:
                st.error(f"Excel build failed: {e}")
                return

    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{current_key.lower()}_{ts}.xlsx"

    st.download_button(
        label=f"📥  Download {cfg['label']} ({meta['rows']:,} rows)",
        data=st.session_state["dl_bytes"],
        file_name=fname,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
    st.caption(
        f"File: `{fname}` · {meta['rows']:,} rows · {len(df.columns)} columns · "
        f"Downloaded: {meta['downloaded']}"
    )

    st.markdown("""<div class="reimport-box">
      📤 <strong>After editing:</strong> Go to <strong>Upload &amp; Import</strong> tab →
      drop your edited file → run <strong>DRY RUN</strong> first → then SHADOW → LIVE.<br>
      The file is pre-formatted with exact column names — no remapping needed.
    </div>""", unsafe_allow_html=True)

    # ── Compare Changes ───────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="section-head">4️⃣  Compare Changes (optional — before importing)</div>', unsafe_allow_html=True)
    st.caption(
        "Upload your **edited** file here to see a row-level diff — "
        "🟢 new rows, 🟡 updated rows, 🔴 deleted rows — before a single byte hits the DB."
    )

    compare_file = st.file_uploader(
        "Upload edited Excel to compare against current DB data",
        type=["xlsx", "xls", "csv"],
        key=f"dl_cmp_{current_key}",
        help="Read-only diff — nothing is imported here"
    )

    if not compare_file:
        return

    # Load edited file
    with st.spinner("Computing diff..."):
        try:
            suf = ".csv" if compare_file.name.lower().endswith(".csv") else ".xlsx"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suf) as tmp:
                tmp.write(compare_file.read())
                tmp_path = tmp.name

            if suf == ".csv":
                upd_df = pd.read_csv(tmp_path, dtype=str)
            else:
                upd_df = pd.read_excel(tmp_path, sheet_name="Data", dtype=str)

            os.unlink(tmp_path)

            # Pass RAW dataframes — compute_diff applies _norm_cell internally.
            # Pre-converting to str here causes false positives (e.g. 100.0 != "100").
            key_cols = get_key_cols(cfg["loader_type"])
            diff_df, summary = compute_diff(df, upd_df, key_cols)

        except Exception as e:
            st.error(f"Diff failed: {e}")
            st.caption("Make sure you uploaded the file from the **Data** sheet (not a renamed sheet).")
            return

    # ── Diff Summary ──────────────────────────────────────────────────────────
    st.markdown("#### Change Summary")

    # ✅ Show exactly what columns changed — critical for price-update workflow
    if summary.get("updated", 0) > 0 and not diff_df.empty and "_changed_cols" in diff_df.columns:
        changed_col_counts = {}
        for v in diff_df.loc[diff_df["_status"] == "UPDATED", "_changed_cols"]:
            for c in str(v).split(", "):
                c = c.strip()
                if c:
                    changed_col_counts[c] = changed_col_counts.get(c, 0) + 1
        if changed_col_counts:
            pills = " &nbsp; ".join(
                f'<span style="background:#fef9c3;border:1px solid #f59e0b;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;">{col} ({n} rows)</span>'
                for col, n in sorted(changed_col_counts.items(), key=lambda x: -x[1])
            )
            st.markdown(f'<div style="margin-bottom:8px;">✏️ <strong>Columns with changes:</strong> &nbsp; {pills}</div>', unsafe_allow_html=True)

    ds1, ds2, ds3, ds4 = st.columns(4)
    with ds1:
        st.markdown(f'<div class="diff-new">🟢 {summary.get("new",0)} New rows</div>', unsafe_allow_html=True)
    with ds2:
        st.markdown(f'<div class="diff-updated">🟡 {summary.get("updated",0)} Updated</div>', unsafe_allow_html=True)
    with ds3:
        st.markdown(f'<div class="diff-deleted">🔴 {summary.get("deleted",0)} Missing from file (NOT deleted from DB)</div>', unsafe_allow_html=True)
    with ds4:
        st.markdown(f'<div class="diff-unch">⚪ {summary.get("unchanged",0)} Unchanged</div>', unsafe_allow_html=True)

    st.markdown("")

    total_changes = summary.get("new", 0) + summary.get("updated", 0) + summary.get("deleted", 0)

    if "error" in summary:
        st.error(f"Diff error: {summary['error']}")
        return

    if "warning" in summary:
        st.warning(summary["warning"])

    if total_changes == 0:
        st.success("✅ No changes detected — your edited file is identical to the downloaded version.")
        return

    # ── AI commentary ─────────────────────────────────────────────────────────
    ai_msgs = []
    if summary.get("new", 0):
        ai_msgs.append(f"✅ **{summary['new']} new rows** will be **INSERTED** when you import this file.")
    if summary.get("updated", 0):
        ai_msgs.append(f"✏️ **{summary['updated']} rows changed** — these will be **UPDATED** in DB (matched by key column).")
    if summary.get("deleted", 0):
        st.error(
            f"🚫 **{summary['deleted']} rows are missing from your edited file** — "
            f"but they will **NOT be deleted from the database**. "
            f"The loader only INSERTs and UPDATEs. It never deletes. \n\n"
            f"👉 To deactivate a party/product: **keep the row** in your Excel and set `IsActive = NO`. "
            f"Then re-import. That will mark it inactive in the DB."
        )
    if summary.get("updated", 0) > 1000:
        ai_msgs.append("⏳ Large update batch. Run in SHADOW mode first to verify before going LIVE.")
    if cfg["loader_type"] in ("OPHLENS", "CLENS", "BLANK", "FRAME") and summary.get("updated", 0) > 0:
        ai_msgs.append(
            "📦 **Stock changes detected.** In Upload & Import tab, choose your mode carefully:\n"
            "- **Price Update (PRICE_ONLY)** → updates PurchaseRate / SellingPrice / MRP only — qty unchanged ✅\n"
            "- **Incremental (ADD)** → adds your qty on top of existing stock\n"
            "- **Opening Reset (OPENING)** → overwrites existing qty with your value"
        )

    for msg in ai_msgs:
        st.markdown(msg)

    # ── Change detail table ───────────────────────────────────────────────────
    if not diff_df.empty:

        # Filter view options
        view_filter = st.radio(
            "Show rows:",
            ["All changes", "New only", "Updated only", "Deleted/Missing only"],
            horizontal=True
        )
        status_filter = {
            "All changes":           ["NEW", "UPDATED", "MISSING FROM FILE"],
            "New only":              ["NEW"],
            "Updated only":          ["UPDATED"],
            "Missing from file only": ["MISSING FROM FILE"],
        }.get(view_filter, ["NEW", "UPDATED", "DELETED"])

        disp_df = diff_df[diff_df["_status"].isin(status_filter)].copy() if "_status" in diff_df.columns else diff_df

        def _colour_status(val):
            return {
                "NEW":       "background-color:#dcfce7; font-weight:bold",
                "UPDATED":   "background-color:#fef9c3; font-weight:bold",
                "MISSING FROM FILE": "background-color:#fee2e2; font-weight:bold",
                "UNCHANGED": "background-color:#f1f5f9",
            }.get(val, "")

        with st.expander(f"🔍 Detailed Changes ({len(disp_df)} rows)", expanded=len(disp_df) < 300):
            # Move _changed_cols next to _status so it's easy to read
            display_cols = ["_status"]
            if "_changed_cols" in disp_df.columns:
                display_cols.append("_changed_cols")
            display_cols += [c for c in disp_df.columns if c not in ("_status", "_changed_cols")]
            disp_show = disp_df[[c for c in display_cols if c in disp_df.columns]]

            if "_status" in disp_show.columns:
                style_cols = ["_status"]
                if "_changed_cols" in disp_show.columns:
                    style_cols.append("_changed_cols")
                styled = disp_show.style.applymap(_colour_status, subset=["_status"])
                st.dataframe(styled, use_container_width=True, hide_index=True)
            else:
                st.dataframe(disp_show, use_container_width=True, hide_index=True)

        # Download colour-coded change report
        with st.spinner("Building colour-coded change report..."):
            try:
                change_bytes = build_download_excel(df, meta, diff_df=diff_df)
                change_fname = f"changes_{current_key.lower()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                st.download_button(
                    "📥  Download Colour-Coded Change Report",
                    data=change_bytes,
                    file_name=change_fname,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                st.caption(
                    "Report has 3 sheets: **Data** (original), **Changes** (colour-coded diff), **Info & Guide**"
                )
            except Exception:
                pass

        # Final CTA
        if total_changes > 0:
            st.markdown("""<div class="reimport-box">
              ✅ <strong>Ready to import?</strong>
              Go to <strong>Upload &amp; Import</strong> tab → drop your edited file →
              DRY RUN first → verify AI Advisor output → then LIVE.
            </div>""", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════



# ════════════════════════════════════════════════════════════
# TAB: MAIN GROUPS — GST% + HSN master
# ════════════════════════════════════════════════════════════

def _tab_cl_barcode_update():
    """Update missing contact lens scan/barcode values on inventory_stock rows."""
    from modules.sql_adapter import run_query, run_write

    st.markdown("### 🧿 Contact Lens Scan Code Update")
    st.caption(
        "Find contact lens stock by brand/product/power and save the scanner barcode "
        "into `inventory_stock.barcode`. Existing duplicate barcodes are blocked."
    )

    try:
        brands = run_query("""
            SELECT DISTINCT COALESCE(p.brand, '') AS brand
            FROM inventory_stock s
            JOIN products p ON p.id = s.product_id
            WHERE UPPER(COALESCE(p.main_group,'')) LIKE '%%CONTACT%%'
              AND COALESCE(s.is_active, TRUE) = TRUE
            ORDER BY brand
        """) or []
    except Exception as exc:
        st.error(f"Could not load contact lens stock: {exc}")
        return

    brand_opts = [r["brand"] for r in brands if str(r.get("brand") or "").strip()]
    f1, f2, f3 = st.columns([2, 3, 2])
    brand = f1.selectbox("Brand", ["All"] + brand_opts, key="clbc_brand")
    product_params = {}
    product_where = [
        "UPPER(COALESCE(p.main_group,'')) LIKE '%%CONTACT%%'",
        "COALESCE(s.is_active, TRUE) = TRUE",
    ]
    if brand != "All":
        product_where.append("COALESCE(p.brand,'') = %(brand)s")
        product_params["brand"] = brand
    product_rows = run_query(f"""
        SELECT DISTINCT COALESCE(p.product_name, '') AS product_name
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE {' AND '.join(product_where)}
        ORDER BY product_name
    """, product_params) or []
    product_opts = [
        r["product_name"] for r in product_rows
        if str(r.get("product_name") or "").strip()
    ]
    product_name = f2.selectbox(
        "Product",
        ["All"] + product_opts,
        key="clbc_product",
    )
    only_missing = f3.checkbox("Only missing barcode", value=True, key="clbc_missing")
    product_refine = st.text_input(
        "Refine product contains",
        key="clbc_product_refine",
        placeholder="Optional: type to narrow long product dropdown/result",
    )

    single_power = st.checkbox(
        "Single power exact search",
        value=False,
        key="clbc_single_power",
        help="Use exact SPH/CYL/Axis, e.g. -0.25 / -0.50 / 180. Range filters are ignored.",
    )
    if single_power:
        sp1, sp2, sp3 = st.columns(3)
        exact_sph = sp1.number_input("SPH", value=-0.25, step=0.25, format="%.2f", key="clbc_exact_sph")
        exact_cyl = sp2.number_input("CYL", value=-0.50, step=0.25, format="%.2f", key="clbc_exact_cyl")
        exact_axis = int(sp3.number_input("Axis", value=180, min_value=0, max_value=180, step=1, key="clbc_exact_axis"))

    r1, r2, r3, r4 = st.columns(4)
    sph_min = r1.number_input("SPH min", value=-20.0, step=0.25, format="%.2f", key="clbc_sph_min")
    sph_max = r2.number_input("SPH max", value=20.0, step=0.25, format="%.2f", key="clbc_sph_max")
    cyl_min = r3.number_input("CYL min", value=-10.0, step=0.25, format="%.2f", key="clbc_cyl_min")
    cyl_max = r4.number_input("CYL max", value=10.0, step=0.25, format="%.2f", key="clbc_cyl_max")

    a1, a2 = st.columns(2)
    axis_min = int(a1.number_input("Axis min", value=0, min_value=0, max_value=180, step=1, key="clbc_axis_min"))
    axis_max = int(a2.number_input("Axis max", value=180, min_value=0, max_value=180, step=1, key="clbc_axis_max"))
    sph_low, sph_high = sorted([float(sph_min), float(sph_max)])
    cyl_low, cyl_high = sorted([float(cyl_min), float(cyl_max)])
    axis_low, axis_high = sorted([int(axis_min), int(axis_max)])
    if (sph_low, sph_high) != (float(sph_min), float(sph_max)) or (cyl_low, cyl_high) != (float(cyl_min), float(cyl_max)):
        st.caption(
            f"Range auto-corrected for search: SPH {sph_low:+.2f} to {sph_high:+.2f}, "
            f"CYL {cyl_low:+.2f} to {cyl_high:+.2f}."
        )

    where = [
        "UPPER(COALESCE(p.main_group,'')) LIKE '%%CONTACT%%'",
        "COALESCE(s.is_active, TRUE) = TRUE",
    ]
    params = {
        "sph_min": sph_low, "sph_max": sph_high,
        "cyl_min": cyl_low, "cyl_max": cyl_high,
        "axis_min": axis_low, "axis_max": axis_high,
    }
    if single_power:
        where += [
            "ROUND(COALESCE(s.sph,0)::numeric, 2) = ROUND(%(exact_sph)s::numeric, 2)",
            "ROUND(COALESCE(s.cyl,0)::numeric, 2) = ROUND(%(exact_cyl)s::numeric, 2)",
            "COALESCE(s.axis,0) = %(exact_axis)s",
        ]
        params.update({
            "exact_sph": float(exact_sph),
            "exact_cyl": float(exact_cyl),
            "exact_axis": exact_axis,
        })
    else:
        where += [
            "COALESCE(s.sph,0) BETWEEN %(sph_min)s AND %(sph_max)s",
            "COALESCE(s.cyl,0) BETWEEN %(cyl_min)s AND %(cyl_max)s",
            "COALESCE(s.axis,0) BETWEEN %(axis_min)s AND %(axis_max)s",
        ]
    if brand != "All":
        where.append("COALESCE(p.brand,'') = %(brand)s")
        params["brand"] = brand
    if product_name != "All":
        where.append("COALESCE(p.product_name,'') = %(product_name)s")
        params["product_name"] = product_name
    if product_refine.strip():
        where.append("COALESCE(p.product_name,'') ILIKE %(prod)s")
        params["prod"] = f"%{product_refine.strip()}%"
    if only_missing:
        where.append("NULLIF(TRIM(COALESCE(s.barcode,'')), '') IS NULL")

    rows = run_query(f"""
        SELECT
            s.id::text AS stock_id,
            p.brand,
            p.product_name,
            COALESCE(p.company_product_name, '') AS company_product_name,
            COALESCE(p.sku_code, '') AS product_sku,
            COALESCE(p.barcode, '') AS product_barcode,
            s.batch_no,
            s.expiry_date::text AS expiry_date,
            s.sph, s.cyl, s.axis, s.add_power,
            COALESCE(s.quantity,0) AS quantity,
            COALESCE(s.mrp,0) AS mrp,
            COALESCE(s.selling_price,0) AS selling_price,
            COALESCE(s.purchase_rate, s.purchase_price, 0) AS purchase_rate,
            COALESCE(s.location,'') AS location,
            COALESCE(s.barcode,'') AS barcode
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE {' AND '.join(where)}
        ORDER BY p.brand, p.product_name, s.sph, s.cyl, s.axis, s.add_power, s.expiry_date
        LIMIT 300
    """, params) or []

    if not rows:
        if single_power:
            st.warning(
                "No exact contact lens row found for "
                f"SPH {float(exact_sph):+.2f} / CYL {float(exact_cyl):+.2f} / AX {int(exact_axis)}."
            )
            alt_where = [
                "UPPER(COALESCE(p.main_group,'')) LIKE '%%CONTACT%%'",
                "COALESCE(s.is_active, TRUE) = TRUE",
            ]
            alt_params = {
                "exact_sph": float(exact_sph),
                "exact_cyl": float(exact_cyl),
                "exact_axis": int(exact_axis),
            }
            if brand != "All":
                alt_where.append("COALESCE(p.brand,'') = %(brand)s")
                alt_params["brand"] = brand
            if product_name != "All":
                alt_where.append("COALESCE(p.product_name,'') = %(product_name)s")
                alt_params["product_name"] = product_name
            if product_refine.strip():
                alt_where.append("COALESCE(p.product_name,'') ILIKE %(prod)s")
                alt_params["prod"] = f"%{product_refine.strip()}%"
            if only_missing:
                alt_where.append("NULLIF(TRIM(COALESCE(s.barcode,'')), '') IS NULL")

            same_sph = run_query(f"""
                SELECT
                    p.brand,
                    p.product_name,
                    s.sph,
                    s.cyl,
                    s.axis,
                    COALESCE(SUM(s.quantity),0) AS total_qty,
                    COUNT(*) AS row_count,
                    COUNT(*) FILTER (
                        WHERE NULLIF(TRIM(COALESCE(s.barcode,'')), '') IS NULL
                    ) AS missing_barcode_rows
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE {' AND '.join(alt_where)}
                  AND ROUND(COALESCE(s.sph,0)::numeric, 2) = ROUND(%(exact_sph)s::numeric, 2)
                GROUP BY p.brand, p.product_name, s.sph, s.cyl, s.axis
                ORDER BY
                    ABS(COALESCE(s.cyl,0)::numeric - %(exact_cyl)s::numeric),
                    ABS(COALESCE(s.axis,0)::numeric - %(exact_axis)s::numeric)
                LIMIT 30
            """, alt_params) or []

            if same_sph:
                st.caption("Same SPH is available with these CYL / Axis combinations:")
                st.dataframe(pd.DataFrame(same_sph), use_container_width=True, hide_index=True)
            else:
                nearest = run_query(f"""
                    SELECT
                        p.brand,
                        p.product_name,
                        s.sph,
                        s.cyl,
                        s.axis,
                        COALESCE(SUM(s.quantity),0) AS total_qty,
                        COUNT(*) AS row_count,
                        COUNT(*) FILTER (
                            WHERE NULLIF(TRIM(COALESCE(s.barcode,'')), '') IS NULL
                        ) AS missing_barcode_rows
                    FROM inventory_stock s
                    JOIN products p ON p.id = s.product_id
                    WHERE {' AND '.join(alt_where)}
                    GROUP BY p.brand, p.product_name, s.sph, s.cyl, s.axis
                    ORDER BY
                        ABS(COALESCE(s.sph,0)::numeric - %(exact_sph)s::numeric),
                        ABS(COALESCE(s.cyl,0)::numeric - %(exact_cyl)s::numeric),
                        ABS(COALESCE(s.axis,0)::numeric - %(exact_axis)s::numeric)
                    LIMIT 30
                """, alt_params) or []
                if nearest:
                    st.caption("Nearest available powers for the selected brand/product:")
                    st.dataframe(pd.DataFrame(nearest), use_container_width=True, hide_index=True)
            st.info("Adjust the exact power above, or untick exact search to use the range search.")
        else:
            st.info("No contact lens rows match this filter.")
        return

    st.markdown(f"#### Matching Stock Rows — {len(rows)}")
    df_rows = pd.DataFrame(rows)
    st.dataframe(df_rows, use_container_width=True, hide_index=True)

    st.markdown("#### Multi Update")
    edit_df = df_rows.copy()
    edit_df.insert(0, "Update", False)
    edit_df["new_barcode"] = edit_df["barcode"].fillna("").astype(str)
    edit_df["new_batch_no"] = edit_df["batch_no"].fillna("").astype(str)
    edit_df["new_expiry_date"] = edit_df["expiry_date"].fillna("").astype(str)
    edit_df["new_mrp"] = pd.to_numeric(edit_df.get("mrp", 0), errors="coerce").fillna(0.0)
    edit_df["new_wlp"] = pd.to_numeric(edit_df.get("selling_price", 0), errors="coerce").fillna(0.0)
    edit_df["new_purchase_rate"] = pd.to_numeric(edit_df.get("purchase_rate", 0), errors="coerce").fillna(0.0)
    edit_cols = [
        "Update", "brand", "product_name", "sph", "cyl", "axis", "add_power",
        "batch_no", "new_batch_no", "expiry_date", "new_expiry_date", "quantity",
        "mrp", "new_mrp", "selling_price", "new_wlp", "purchase_rate", "new_purchase_rate",
        "barcode", "new_barcode", "stock_id"
    ]
    edit_df = edit_df[[c for c in edit_cols if c in edit_df.columns]]
    edited = st.data_editor(
        edit_df,
        use_container_width=True,
        hide_index=True,
        key="clbc_multi_editor",
        disabled=[
            c for c in edit_df.columns
            if c not in (
                "Update", "new_barcode", "new_batch_no", "new_expiry_date",
                "new_mrp", "new_wlp", "new_purchase_rate"
            )
        ],
        column_config={
            "Update": st.column_config.CheckboxColumn("Update"),
            "new_barcode": st.column_config.TextColumn(
                "New SKU / Barcode",
                help="Leave blank to clear a wrongly tagged barcode.",
            ),
            "new_batch_no": st.column_config.TextColumn("New Batch"),
            "new_expiry_date": st.column_config.TextColumn(
                "New Expiry",
                help="Use YYYY-MM-DD. Leave blank if expiry is unknown.",
            ),
            "new_mrp": st.column_config.NumberColumn("New MRP", min_value=0.0, step=1.0, format="%.2f"),
            "new_wlp": st.column_config.NumberColumn("New WLP", min_value=0.0, step=1.0, format="%.2f"),
            "new_purchase_rate": st.column_config.NumberColumn("New Purchase", min_value=0.0, step=1.0, format="%.2f"),
            "stock_id": st.column_config.TextColumn("Stock ID", disabled=True),
        },
    )
    if st.button("💾 Save Selected Stock Updates", type="primary", key="clbc_multi_save", use_container_width=True):
        selected_updates = []
        for _, er in edited.iterrows():
            if not bool(er.get("Update")):
                continue
            code = str(er.get("new_barcode") or "").strip()
            sid = str(er.get("stock_id") or "").strip()
            old = str(er.get("barcode") or "").strip()
            if not sid:
                continue
            new_batch = str(er.get("new_batch_no") or "").strip()
            new_expiry = str(er.get("new_expiry_date") or "").strip()
            try:
                new_mrp = max(float(er.get("new_mrp") or 0), 0.0)
                new_wlp = max(float(er.get("new_wlp") or 0), 0.0)
                new_purchase_rate = max(float(er.get("new_purchase_rate") or 0), 0.0)
            except Exception:
                st.error(f"Price fields must be numeric for stock row {sid[:8]}.")
                return
            if code == old and new_batch == str(er.get("batch_no") or "").strip() \
                    and new_expiry == str(er.get("expiry_date") or "").strip() \
                    and round(new_mrp, 2) == round(float(er.get("mrp") or 0), 2) \
                    and round(new_wlp, 2) == round(float(er.get("selling_price") or 0), 2) \
                    and round(new_purchase_rate, 2) == round(float(er.get("purchase_rate") or 0), 2):
                continue
            selected_updates.append({
                "sid": sid,
                "code": code,
                "batch_no": new_batch,
                "expiry_date": new_expiry,
                "mrp": new_mrp,
                "wlp": new_wlp,
                "purchase_rate": new_purchase_rate,
            })

        if not selected_updates:
            st.info("No changed selected stock rows to save.")
        else:
            codes = [u["code"].upper() for u in selected_updates if u["code"]]
            if len(codes) != len(set(codes)):
                st.error("Duplicate barcode inside selected update rows. Each stock row needs a unique scan code.")
                return
            dup = []
            if codes:
                dup = run_query("""
                    SELECT s.id::text AS stock_id, s.barcode, p.brand, p.product_name, s.batch_no
                    FROM inventory_stock s
                    LEFT JOIN products p ON p.id = s.product_id
                    WHERE UPPER(TRIM(COALESCE(s.barcode,''))) = ANY(%(codes)s)
                      AND NOT (s.id::text = ANY(%(ids)s))
                    LIMIT 5
                """, {
                    "codes": codes,
                    "ids": [u["sid"] for u in selected_updates],
                }) or []
            if dup:
                d = dup[0]
                st.error(
                    "Duplicate barcode blocked: "
                    f"{d.get('barcode')} already used by {d.get('brand','')} "
                    f"{d.get('product_name','')} batch {d.get('batch_no','-')}."
                )
                return
            saved = 0
            for upd in selected_updates:
                run_write(
                    """
                    UPDATE inventory_stock
                    SET
                        barcode=%s,
                        batch_no=NULLIF(%s, ''),
                        expiry_date=NULLIF(%s, '')::date,
                        mrp=%s,
                        selling_price=%s,
                        purchase_rate=%s,
                        updated_at=NOW()
                    WHERE id=%s::uuid
                    """,
                    (
                        upd["code"] or None,
                        upd["batch_no"],
                        upd["expiry_date"],
                        upd["mrp"],
                        upd["wlp"],
                        upd["purchase_rate"],
                        upd["sid"],
                    ),
                )
                saved += 1
            st.success(f"✅ Saved {saved} contact lens stock update(s).")
            st.rerun()

    labels = {}
    for r in rows:
        sid = str(r.get("stock_id") or "")
        power = " ".join([
            f"SPH {float(r.get('sph') or 0):+.2f}",
            f"CYL {float(r.get('cyl') or 0):+.2f}" if float(r.get("cyl") or 0) else "",
            f"AX {int(float(r.get('axis') or 0))}" if int(float(r.get("axis") or 0)) else "",
            f"ADD {float(r.get('add_power') or 0):+.2f}" if float(r.get("add_power") or 0) else "",
        ]).strip()
        labels[sid] = (
            f"{r.get('brand') or ''} | {r.get('product_name') or ''} | {power or 'No power'} "
            f"| Batch {r.get('batch_no') or '-'} | Qty {r.get('quantity') or 0} "
            f"| Current {r.get('barcode') or 'MISSING'}"
        )

    selected = st.selectbox(
        "Select exact box / stock row",
        list(labels.keys()),
        format_func=lambda sid: labels.get(sid, sid),
        key="clbc_selected_stock",
    )
    current = next((r for r in rows if str(r.get("stock_id")) == selected), {})

    c1, c2 = st.columns([3, 1])
    new_code = c1.text_input(
        "Scan / enter SKU barcode",
        value=str(current.get("barcode") or ""),
        key=f"clbc_new_code_{selected}",
        placeholder="Scan box barcode here",
    )
    c2.metric("Qty", int(float(current.get("quantity") or 0)))

    if st.button("💾 Save Scan Code", type="primary", key=f"clbc_save_{selected}", use_container_width=True):
        code = str(new_code or "").strip()
        if not code:
            st.warning("Scan code cannot be blank.")
            return
        dup = run_query("""
            SELECT s.id::text AS stock_id, p.brand, p.product_name, s.batch_no
            FROM inventory_stock s
            LEFT JOIN products p ON p.id = s.product_id
            WHERE UPPER(TRIM(COALESCE(s.barcode,''))) = UPPER(TRIM(%(bc)s))
              AND s.id::text <> %(sid)s
            LIMIT 1
        """, {"bc": code, "sid": selected}) or []
        if dup:
            d = dup[0]
            st.error(
                "Duplicate barcode blocked: already used by "
                f"{d.get('brand','')} {d.get('product_name','')} batch {d.get('batch_no','-')}."
            )
            return
        try:
            run_write(
                "UPDATE inventory_stock SET barcode=%s, updated_at=NOW() WHERE id=%s::uuid",
                (code, selected),
            )
            st.success("✅ Contact lens scan code saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Save failed: {exc}")


def _tab_main_groups():
    """Manage main_groups master — canonical GST% and HSN code per product group."""
    from modules.sql_adapter import run_query, run_write

    st.markdown(
        "<style>.mg-head{font-size:1.1rem;font-weight:700;color:#1a3c5e}"
        ".mg-note{font-size:.82rem;color:#555;margin-bottom:1rem}</style>",
        unsafe_allow_html=True
    )
    st.markdown('<div class="mg-head">🏷️ Main Groups — GST % & HSN Master</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="mg-note">'
        'Set the canonical GST rate and HSN code for each product group. '
        'When you upload a PRODUCT Excel with a blank GST% or HSN, these values are auto-filled. '
        'Products still store their own overrides — changing here only affects future uploads.'
        '</div>',
        unsafe_allow_html=True
    )

    # ── Ensure table exists ────────────────────────────────────────────────────
    try:
        run_write("""
            CREATE TABLE IF NOT EXISTS main_groups (
                id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                name        TEXT         NOT NULL UNIQUE,
                gst_percent NUMERIC(5,2) NOT NULL DEFAULT 12,
                hsn_code    TEXT         NOT NULL DEFAULT '',
                description TEXT,
                created_at  TIMESTAMP    DEFAULT NOW(),
                updated_at  TIMESTAMP    DEFAULT NOW()
            )
        """)
    except Exception as e:
        st.error(f"Cannot create main_groups table: {e}")
        return

    # ── Load current groups ────────────────────────────────────────────────────
    rows = run_query(
        "SELECT id, name, gst_percent, hsn_code, description FROM main_groups ORDER BY name"
    ) or []

    GST_OPTIONS = [0, 5, 18]

    st.markdown("#### Existing Groups")
    if not rows:
        st.info("No groups yet. Add your groups below — confirm GST rates and HSN codes with your CA before saving.")
    else:
        for row in rows:
            gst_val = int(row["gst_percent"])
            hsn_val = row["hsn_code"] or "—"
            label   = f"**{row['name']}** — {gst_val}% GST | HSN: {hsn_val}"
            with st.expander(label, expanded=False):
                c1, c2, c3, c4 = st.columns([3, 1, 2, 2])
                rid      = str(row["id"])
                new_name = c1.text_input("Group Name", value=row["name"],         key="mg_name_" + rid)
                gst_idx  = GST_OPTIONS.index(gst_val) if gst_val in GST_OPTIONS else 2
                new_gst  = c2.selectbox("GST %", GST_OPTIONS, index=gst_idx,      key="mg_gst_"  + rid)
                new_hsn  = c3.text_input("HSN Code", value=row["hsn_code"] or "", key="mg_hsn_"  + rid)
                new_desc = c4.text_input("Note",     value=row["description"] or "",key="mg_desc_" + rid)
                sc1, sc2 = st.columns([1, 5])
                if sc1.button("💾 Save", key="mg_save_" + rid):
                    try:
                        run_write(
                            "UPDATE main_groups SET name=%s, gst_percent=%s, hsn_code=%s, "
                            "description=%s, updated_at=NOW() WHERE id=%s",
                            (new_name.strip(), float(new_gst), new_hsn.strip(), new_desc.strip(), rid)
                        )
                        st.success("✅ Saved.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")
                if sc2.button("🗑️ Delete", key="mg_del_" + rid):
                    try:
                        run_write("DELETE FROM main_groups WHERE id=%s", (rid,))
                        st.success("Deleted.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Delete failed: {e}")

    # ── Add new group ──────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### ➕ Add New Group")
    a1, a2, a3, a4 = st.columns([3, 1, 2, 2])
    add_name = a1.text_input("Group Name", placeholder="e.g. Ophthalmic Lenses", key="mg_new_name")
    add_gst  = a2.selectbox("GST %", GST_OPTIONS, index=2, key="mg_new_gst")
    add_hsn  = a3.text_input("HSN Code", placeholder="e.g. 9001509000",          key="mg_new_hsn")
    add_desc = a4.text_input("Note", placeholder="optional",                      key="mg_new_desc")
    if st.button("➕ Add Group", key="mg_add_btn"):
        if not add_name.strip():
            st.warning("Group name is required.")
        else:
            try:
                run_write(
                    "INSERT INTO main_groups (name, gst_percent, hsn_code, description) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                    (add_name.strip(), float(add_gst), add_hsn.strip(), add_desc.strip())
                )
                st.success(f"✅ '{add_name}' added.")
                st.rerun()
            except Exception as e:
                st.error(f"Add failed: {e}")




def render_loader_page():
    st.markdown(_CSS, unsafe_allow_html=True)
    st.title("🗂️ Data Loader")
    st.caption("Smart Import (fingerprinted, audited) | Legacy Import (DRY/SHADOW/LIVE) | Export | Audit | Admin")

    tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
        "🧠 Smart Import",
        "📤 Legacy Import",
        "📊 DB Export",
        "🔍 Audit & Integrity",
        "📜 Schema History",
        "⚙️ Admin",
        "📋 Schema Reference",
        "🏷️ Main Groups",
        "🧿 CL Barcode Update",
    ])
    with tab0:
        try:
            from modules.ui.smart_loader_ui import render_smart_loader
            render_smart_loader()
        except Exception as e:
            st.error(f"❌ Smart loader unavailable: {e}")
            import traceback
            st.code(traceback.format_exc())
    with tab1: _tab_upload()
    with tab2: _tab_export()
    with tab3: _tab_audit()
    with tab4: _tab_schema_history()
    with tab5: _tab_admin()
    with tab6: _tab_schema_ref()
    with tab7: _tab_main_groups()
    with tab8: _tab_cl_barcode_update()
