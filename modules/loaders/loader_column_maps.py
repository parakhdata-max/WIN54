"""
modules/loaders/loader_column_maps.py
=======================================
Registry-driven column maps for universal_loader_core.py

This replaces all hardcoded *_COLUMN_MAP dicts and REQUIRED_COLUMNS dict
in universal_loader_core.py with live lookups from db_schema_registry.

HOW TO USE in universal_loader_core.py:
  Replace ALL the hardcoded dicts at the top with:

    from modules.loaders.loader_column_maps import (
        get_loader_column_map,
        get_loader_required_columns,
        PRODUCT_COLUMN_MAP,
        FRAME_COLUMN_MAP,
        OPHLENS_COLUMN_MAP,
        CLENS_COLUMN_MAP,
        SOL_COLUMN_MAP,
        PARTY_COLUMN_MAP,
        PATIENT_COLUMN_MAP,
        BLANK_COLUMN_MAP,
        REQUIRED_COLUMNS,
    )

These are all built live from db_schema_registry at import time.
When a column is added to DB_SCHEMA, it flows through automatically.
"""

from modules.loaders.db_schema_registry import get_column_map, get_required_cols, ALL_FILE_TYPES


def get_loader_column_map(file_type: str) -> dict:
    """
    Build the normalised excel_header → db_column map for a given loader type.
    Identical format to the old hardcoded dicts — drop-in replacement.
    """
    return get_column_map(file_type)


def get_loader_required_columns(file_type: str) -> list:
    """
    Return list of required DB column names for a given loader type.
    """
    return get_required_cols(file_type)


# ── Build all maps at import time ─────────────────────────────────────────────
# These constants maintain backward compatibility with universal_loader_core.py
# which references them by name (e.g. apply_column_map(df, OPHLENS_COLUMN_MAP))

PRODUCT_COLUMN_MAP  = get_loader_column_map("PRODUCT")
FRAME_COLUMN_MAP    = get_loader_column_map("FRAME")
OPHLENS_COLUMN_MAP  = get_loader_column_map("OPHLENS")
CLENS_COLUMN_MAP    = get_loader_column_map("CLENS")
SOL_COLUMN_MAP      = get_loader_column_map("SOL")
PARTY_COLUMN_MAP    = get_loader_column_map("PARTY")
PATIENT_COLUMN_MAP  = get_loader_column_map("PATIENT")
BLANK_COLUMN_MAP    = get_loader_column_map("BLANK")

REQUIRED_COLUMNS = {ft: get_loader_required_columns(ft) for ft in ALL_FILE_TYPES}


# ── Canonical columns list for AI header mapping ──────────────────────────────
# Replaces the _CANONICAL_COLUMNS list in universal_loader_core.py

def build_canonical_columns() -> list:
    """
    Flat list of all normalised excel_header values across all file types.
    Used by the AI fuzzy mapper (_ai_map_headers) to recognise unknown columns.
    """
    seen = set()
    result = []
    for ft in ALL_FILE_TYPES:
        for norm_header in get_column_map(ft).keys():
            if norm_header not in seen:
                seen.add(norm_header)
                result.append(norm_header)
    return result


CANONICAL_COLUMNS = build_canonical_columns()


# ── Schema defaults for _auto_fix_schema ─────────────────────────────────────
# Replaces _SCHEMA_DEFAULTS dict in universal_loader_core.py

from modules.loaders.db_schema_registry import get_defaults

def build_schema_defaults() -> dict:
    """
    Merged defaults across all file types.
    Column default from any file type is included (safe — loader only uses
    defaults for the active file type's columns).
    """
    merged = {}
    for ft in ALL_FILE_TYPES:
        merged.update(get_defaults(ft))
    return merged


SCHEMA_DEFAULTS = build_schema_defaults()
