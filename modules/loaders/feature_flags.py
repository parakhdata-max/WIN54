"""
modules/loaders/feature_flags.py
==================================
SHIM — Re-exports from modules.flags.feature_flags

loader_ui.py imports from modules.loaders.feature_flags.
The actual implementation lives in modules/flags/feature_flags.py.
This shim bridges the two so no import in loader_ui.py needs to change.
"""

from modules.flags.feature_flags import (
    get_flag,
    set_flag,
    get_all_flags,
    clear_cache,
    ensure_flags_table,
    _FLAG_CACHE,
    _DEFAULTS,
)

__all__ = [
    "get_flag",
    "set_flag",
    "get_all_flags",
    "clear_cache",
    "ensure_flags_table",
    "_FLAG_CACHE",
    "_DEFAULTS",
]
