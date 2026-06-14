"""
modules/retail_punching.py
===========================
Public shell for retail punching. Keeps existing imports working while
the implementation lives in retail_punching_data/rx/ui.
"""

from importlib import import_module as _import_module

_retail_data_mod = _import_module("modules.retail_punching_data")
_retail_rx_mod = _import_module("modules.retail_punching_rx")
_retail_ui_mod = _import_module("modules.retail_punching_ui")

_retail_modules = (_retail_data_mod, _retail_rx_mod, _retail_ui_mod)
_retail_merged = {}
for _retail_mod in _retail_modules:
    _retail_merged.update({k: v for k, v in vars(_retail_mod).items() if not k.startswith("__")})

for _retail_mod in _retail_modules:
    vars(_retail_mod).update(_retail_merged)

globals().update(_retail_merged)
__all__ = [k for k in _retail_merged if not k.startswith("__")]

del _import_module, _retail_mod, _retail_modules, _retail_merged
