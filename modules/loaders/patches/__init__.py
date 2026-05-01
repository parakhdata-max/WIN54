"""
patches/__init__.py
=====================
DV ERP — Production Patches Package
=====================================

4 production patches for the loader + Excel export stack.

QUICK INTEGRATION GUIDE
========================

PATCH 1 — Transaction Wrapper (MOST CRITICAL)
----------------------------------------------
Replace run_loader() with run_loader_safe() everywhere:

    # OLD (loader_ui.py line ~XX):
    from modules.loaders.universal_loader_core import run_loader
    result = run_loader(path, mode=mode, stock_mode=stock_mode)

    # NEW:
    from patches.loader_transaction_wrapper import run_loader_safe
    result = run_loader_safe(path, mode=mode, stock_mode=stock_mode, user=st.session_state.get("user","system"))
    st.caption(f"Import ID: {result.import_id}")

    Run this SQL once to create audit tables:
        CREATE TABLE IF NOT EXISTS loader_import_log ( ... )  -- see patch docstring
        CREATE TABLE IF NOT EXISTS loader_row_history ( ... ) -- see patch docstring


PATCH 2 — Excel Export Enhancer
---------------------------------
Replace build_download_excel() with build_enhanced_excel():

    # OLD (loader_ui.py download section):
    from modules.loaders.data_downloader import build_download_excel
    excel_bytes = build_download_excel(df, meta, diff_df)
    filename = f"{dataset_key}_export.xlsx"

    # NEW:
    from patches.excel_export_enhancer import build_enhanced_excel, make_versioned_filename
    excel_bytes = build_enhanced_excel(df, meta, dataset_key, diff_df=diff_df, exported_by=user)
    filename = make_versioned_filename(dataset_key, import_count=import_count)

    # Get import_count from DB:
    import_count = run_query(
        "SELECT COUNT(*) AS n FROM loader_import_log WHERE file_type=%s", (dataset_key,)
    )[0]["n"] + 1


PATCH 3 — Roundtrip Safety
----------------------------
Add validation before every import, and a clean template download:

    from patches.roundtrip_safe_export import (
        validate_roundtrip_columns,
        build_clean_template,
        format_dry_run_summary,
    )

    # In upload handler — before run_loader_safe():
    ok, issues = validate_roundtrip_columns(uploaded_df, file_type)
    if issues:
        for issue in issues:
            st.warning(issue) if "⚠️" in issue else st.error(issue)
    if not ok:
        st.stop()

    # In download section — add blank template button:
    if st.button("📋 Download Blank Template"):
        template_bytes = build_clean_template(dataset_key)
        st.download_button("Save Template", template_bytes,
                           file_name=f"{dataset_key}_template.xlsx")

    # After DRY RUN — show friendly summary:
    if result:
        st.code(format_dry_run_summary(result))


PATCH 4 — Schema Version Tagging
----------------------------------
Embed schema version in every export and track it in DB:

    from patches.schema_version import CURRENT_SCHEMA_VERSION, check_schema_compatibility

    # In build_enhanced_excel() call:
    excel_bytes = build_enhanced_excel(
        ...,
        schema_version = CURRENT_SCHEMA_VERSION,
    )

    # In upload handler:
    compat, msg = check_schema_compatibility(uploaded_df)
    if not compat:
        st.warning(msg)


PATCH DEPENDENCY ORDER
======================
Install order matters for the first run:

1. Run SQL DDL from loader_transaction_wrapper.py docstring (create tables)
2. Deploy all 3 .py files into modules/loaders/patches/ or a top-level patches/
3. Update imports in loader_ui.py (Patch 1 is the highest priority)
4. Test with a DRY RUN before any LIVE imports

All patches are backwards-compatible — they do NOT change existing DB schema.
"""

from modules.loaders.patches.loader_transaction_wrapper import run_loader_safe
from modules.loaders.patches.excel_export_enhancer import build_enhanced_excel, make_versioned_filename
from modules.loaders.patches.roundtrip_safe_export import (
    validate_roundtrip_columns,
    build_clean_template,
    build_reimport_excel,
    format_dry_run_summary,
    strip_system_columns,
)

__all__ = [
    "run_loader_safe",
    "build_enhanced_excel",
    "make_versioned_filename",
    "validate_roundtrip_columns",
    "build_clean_template",
    "build_reimport_excel",
    "format_dry_run_summary",
    "strip_system_columns",
]
