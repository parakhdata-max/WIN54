"""
modules/system_health.py
==========================
SHIM — Re-exports from modules.ui.system_health

loader_ui.py imports from modules.system_health (no 'ui' segment).
The actual implementation lives in modules/ui/system_health.py.
This shim bridges the two so no import in loader_ui.py needs to change.
"""

from modules.ui.system_health import get_module_health, get_health_summary

__all__ = ["get_module_health", "get_health_summary"]
