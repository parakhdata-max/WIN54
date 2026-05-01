# plugins package — plug-in tab modules for backoffice
from .registry import (
    BackofficePlugin,
    BACKOFFICE_PLUGINS,
    register_plugin,
    unregister_plugin,
    get_plugin,
    get_active_plugins,
    enable_plugin,
    disable_plugin,
    discover_plugins,
)
