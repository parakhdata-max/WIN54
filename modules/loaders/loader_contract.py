"""
Loader contract audit.

One place to compare:
- downloader edit columns
- downloader add-template columns
- upload accepted columns
- live DB writable columns
- uploaded Excel columns that will be ignored/read-only

This is intentionally read-only and UI-safe. Importers still own the actual
write path; this module only makes the contract visible before commit.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import pandas as pd


def _norm(v: Any) -> str:
    text = str(v or "").replace("🔒", "").strip()
    return re.sub(r"[\s_\-₹%()/\.]", "", text.lower())


def _unique(seq) -> List[str]:
    out, seen = [], set()
    for item in seq:
        if item in (None, ""):
            continue
        key = str(item)
        if key not in seen:
            out.append(key)
            seen.add(key)
    return out


def _static_upload_maps(file_type: str) -> Dict[str, str]:
    try:
        import modules.loaders.universal_loader_core as ulc

        maps = {
            "PRODUCT": getattr(ulc, "PRODUCT_COLUMN_MAP", {}),
            "FRAME": getattr(ulc, "FRAME_COLUMN_MAP", {}),
            "OPHLENS": getattr(ulc, "OPHLENS_COLUMN_MAP", {}),
            "CLENS": getattr(ulc, "CLENS_COLUMN_MAP", {}),
            "SOL": getattr(ulc, "SOL_COLUMN_MAP", {}),
            "BLANK": getattr(ulc, "BLANK_COLUMN_MAP", {}),
            "PARTY": getattr(ulc, "PARTY_COLUMN_MAP", {}),
            "PATIENT": getattr(ulc, "PATIENT_COLUMN_MAP", {}),
        }
        return dict(maps.get(file_type, {}) or {})
    except Exception:
        return {}


def _registry_upload_map(file_type: str) -> Dict[str, str]:
    try:
        from modules.loaders.live_schema_bridge import get_upload_col_map

        return dict(get_upload_col_map(file_type) or {})
    except Exception:
        return {}


def _schema_columns(file_type: str):
    try:
        from modules.loaders.live_schema_bridge import get_live_schema

        return list(get_live_schema(file_type) or [])
    except Exception:
        return []


def _db_schema_columns(file_type: str):
    try:
        from modules.loaders.db_schema_registry import DB_SCHEMA

        return list(DB_SCHEMA.get(file_type, []) or [])
    except Exception:
        return []


def _add_template_cols(file_type: str) -> List[str]:
    live = _schema_columns(file_type)
    if live:
        return _unique([c.excel_header for c in live if c.writable and c.excel_header])
    return _unique([c.excel_header for c in _db_schema_columns(file_type) if c.writable and c.excel_header])


def _edit_download_cols(file_type: str) -> List[str]:
    live = _schema_columns(file_type)
    if live:
        return _unique([c.excel_header for c in live if c.excel_header])
    return _unique([c.excel_header for c in _db_schema_columns(file_type) if c.download and c.excel_header])


def _table_contract(file_type: str) -> Dict[str, Any]:
    try:
        from modules.loaders.live_schema_bridge import TABLE_REGISTRY

        bridge_table = ((TABLE_REGISTRY.get(file_type, {}).get("tables") or [{}])[0].get("name") or "")
    except Exception:
        bridge_table = ""

    expected = {
        "PRODUCT": ("products", "product_name"),
        "FRAME": ("frames", "sku_code"),
        "BLANK": ("blank_inventory", "brand+category+material+colour+add_power+base_recommended"),
        "OPHLENS": ("inventory_stock", "product_name+power"),
        "CLENS": ("inventory_stock", "batch_no+power"),
        "SOL": ("batches", "batch_no"),
        "PARTY": ("parties", "party_name"),
        "PATIENT": ("patients", "mobile/record_no"),
        "PRICE": ("inventory_stock/batches", "product_name+mrp"),
    }
    table, key = expected.get(file_type, ("", ""))
    return {
        "expected_table": table,
        "bridge_table": bridge_table,
        "key": key,
        "ok": (not table or not bridge_table or bridge_table in table or table in bridge_table),
    }


def build_loader_contract_report(file_type: str, uploaded_df: pd.DataFrame = None) -> Dict[str, Any]:
    file_type = (file_type or "").upper().strip()
    live = _schema_columns(file_type)
    live_db_cols = _unique([c.db_column for c in live])
    live_writable = {c.db_column for c in live if c.writable}
    readonly_db_cols = {c.db_column for c in live if not c.writable or c.required}
    live_header_map = {}
    for c in live:
        if c.excel_header:
            live_header_map[_norm(c.excel_header)] = c.db_column
        live_header_map[_norm(c.db_column)] = c.db_column

    raw_upload_map = {}
    raw_upload_map.update(_registry_upload_map(file_type))
    raw_upload_map.update(_static_upload_maps(file_type))
    upload_map = {_norm(k): v for k, v in raw_upload_map.items()}
    accepted_db_cols = set(upload_map.values())

    edit_cols = _edit_download_cols(file_type)
    add_cols = _add_template_cols(file_type)
    downloaded_cols = _unique(edit_cols + add_cols)

    download_not_uploadable = []
    readonly_cols = []
    for col in downloaded_cols:
        db_col = upload_map.get(_norm(col)) or live_header_map.get(_norm(col))
        if not db_col:
            download_not_uploadable.append(col)
        elif db_col in readonly_db_cols:
            readonly_cols.append(col)

    upload_not_downloaded = sorted([
        db for db in accepted_db_cols
        if db in live_writable and db not in {upload_map.get(_norm(c)) for c in downloaded_cols}
    ])

    ignored_excel_cols = []
    read_only_excel_cols = []
    fully_respected_cols = []
    missing_required_cols = []
    if uploaded_df is not None:
        actual_cols = [str(c).replace("🔒", "").strip() for c in uploaded_df.columns]
        for col in actual_cols:
            db_col = upload_map.get(_norm(col)) or live_header_map.get(_norm(col))
            if not db_col or (live_db_cols and db_col not in live_db_cols):
                ignored_excel_cols.append(col)
            elif db_col in readonly_db_cols:
                read_only_excel_cols.append(col)
            else:
                fully_respected_cols.append(col)

        required = [c for c in live if c.required and c.writable]
        actual_norms = {_norm(c) for c in actual_cols}
        for c in required:
            if _norm(c.excel_header) not in actual_norms and _norm(c.db_column) not in actual_norms:
                missing_required_cols.append(c.excel_header or c.db_column)

    return {
        "file_type": file_type,
        "download_edit_cols": edit_cols,
        "download_new_cols": add_cols,
        "upload_accepted_cols": sorted(accepted_db_cols),
        "live_db_cols": live_db_cols,
        "ignored_excel_cols": _unique(ignored_excel_cols),
        "read_only_excel_cols": _unique(read_only_excel_cols),
        "fully_respected_cols": _unique(fully_respected_cols),
        "download_not_uploadable": _unique(download_not_uploadable),
        "upload_not_downloaded": upload_not_downloaded,
        "readonly_cols": _unique(readonly_cols),
        "missing_required_cols": _unique(missing_required_cols),
        "table_contract": _table_contract(file_type),
    }


def render_loader_contract_panel(st, report: Dict[str, Any], require_ack: bool = True, key: str = "loader_contract") -> bool:
    """Render a compact contract report. Returns True when safe/acknowledged."""
    ignored = report.get("ignored_excel_cols") or []
    readonly = report.get("read_only_excel_cols") or []
    not_uploadable = report.get("download_not_uploadable") or []
    missing = report.get("missing_required_cols") or []
    table_contract = report.get("table_contract") or {}
    has_risk = bool(ignored or not_uploadable or missing or not table_contract.get("ok", True))

    with st.expander("🧾 Loader Contract Check", expanded=has_risk):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Respected", len(report.get("fully_respected_cols") or []))
        c2.metric("Ignored", len(ignored))
        c3.metric("Read-only", len(readonly))
        c4.metric("Required Missing", len(missing))

        if table_contract:
            if table_contract.get("ok", True):
                st.success(
                    f"Table/key OK: `{table_contract.get('bridge_table') or table_contract.get('expected_table')}` "
                    f"· key `{table_contract.get('key')}`"
                )
            else:
                st.error(
                    f"Table mismatch: expected `{table_contract.get('expected_table')}`, "
                    f"bridge uses `{table_contract.get('bridge_table')}`"
                )

        if ignored:
            st.error("Excel columns present but not respected by uploader:")
            st.code(", ".join(ignored))
        if missing:
            st.error("Required columns missing:")
            st.code(", ".join(missing))
        if not_uploadable:
            st.warning("Downloaded/template columns that cannot round-trip into uploader:")
            st.code(", ".join(not_uploadable))
        if readonly:
            st.info("Read-only / identity columns. They are visible but edits are ignored:")
            st.code(", ".join(readonly[:40]))

        if not has_risk:
            st.success("All uploaded columns are mapped or intentionally read-only. No silent drops detected.")

    if not require_ack or not has_risk:
        return True
    return st.checkbox("✅ I acknowledge the Loader Contract warnings above", key=key)
