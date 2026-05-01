"""
modules/loaders
================
Loader engine public surface.

Import from here, not from submodules directly:

    from modules.loaders import run_loader_safe, build_enhanced_excel
"""

from modules.loaders.patches.loader_transaction_wrapper import run_loader_safe
from modules.loaders.patches.excel_export_enhancer import build_enhanced_excel, make_versioned_filename
from modules.loaders.patches.roundtrip_safe_export import (
    validate_roundtrip_columns,
    build_clean_template,
    build_reimport_excel,
    format_dry_run_summary,
)
from modules.loaders.universal_loader_core import run_loader     # original — kept for backwards compat
from modules.loaders.data_downloader import fetch_for_download, compute_diff
from modules.loaders.schema_guard import analyze_schema, generate_ai_advice

__all__ = [
    # Patched (preferred)
    "run_loader_safe",
    "build_enhanced_excel",
    "make_versioned_filename",
    "validate_roundtrip_columns",
    "build_clean_template",
    "build_reimport_excel",
    "format_dry_run_summary",
    # Original (backwards compat)
    "run_loader",
    "fetch_for_download",
    "compute_diff",
    "analyze_schema",
    "generate_ai_advice",
]
