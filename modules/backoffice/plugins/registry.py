"""
plugins/registry.py
====================
Backoffice Plugin Registry — auto-loading tab plugin system.

WHAT IS A PLUGIN
-----------------
  A plugin is any module that:
    1. Lives in modules/backoffice/plugins/
    2. Exposes a render(ctx) function
    3. Has a PLUGIN_META dict

PLUGIN_META shape:
    PLUGIN_META = {
        "id":           "supplier_panel",    # unique id
        "label":        "🚚 Supplier Orders", # tab label
        "tab_key":      "tab5",              # which tab slot
        "roles":        [],                  # [] = no restriction
        "enabled":      True,
    }

HOW TO ADD A PLUGIN
--------------------
  Option A — auto-discover: drop a .py file in plugins/ with PLUGIN_META + render(ctx)
  Option B — register manually:

      from modules.backoffice.plugins.registry import register_plugin
      register_plugin(MyPlugin)

HOW THE SHELL USES IT
----------------------
  from modules.backoffice.plugins.registry import get_active_plugins
  for plugin in get_active_plugins(ctx):
      with tab:
          plugin.render(ctx)
"""

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# PLUGIN DATA CLASS
# ═══════════════════════════════════════════════════════════════════════

class BackofficePlugin:
    """
    Represents a registered backoffice tab plugin.

    Attributes:
        id          : unique string identifier
        label       : tab label shown in UI (include emoji)
        render      : callable(ctx) that renders the plugin UI
        tab_key     : slot hint ("tab5", "tab6", etc.) — advisory only
        roles       : list of roles that can see this plugin
                      empty list = no restriction
        enabled     : bool — can be toggled at runtime
        order       : int — sort order in tab list (lower = earlier)
    """

    def __init__(
        self,
        id: str,
        label: str,
        render: Callable,
        tab_key: str = "auto",
        roles: Optional[List[str]] = None,
        enabled: bool = True,
        order: int = 99,
    ):
        self.id      = id
        self.label   = label
        self.render  = render
        self.tab_key = tab_key
        self.roles   = roles or []
        self.enabled = enabled
        self.order   = order

    def is_accessible(self, ctx) -> bool:
        """True if plugin is enabled and user has required role."""
        if not self.enabled:
            return False
        if not self.roles:
            return True
        return ctx.has_role(*self.roles)

    def __repr__(self) -> str:
        return f"<Plugin id={self.id} label={self.label!r} enabled={self.enabled}>"


# ═══════════════════════════════════════════════════════════════════════
# PLUGIN REGISTRY
# ═══════════════════════════════════════════════════════════════════════

# Master list — populated by register_plugin() and auto-discovery
BACKOFFICE_PLUGINS: List[BackofficePlugin] = []


def register_plugin(plugin: BackofficePlugin) -> None:
    """Register a plugin. Replaces any existing plugin with same id."""
    global BACKOFFICE_PLUGINS
    BACKOFFICE_PLUGINS = [p for p in BACKOFFICE_PLUGINS if p.id != plugin.id]
    BACKOFFICE_PLUGINS.append(plugin)
    BACKOFFICE_PLUGINS.sort(key=lambda p: p.order)
    log.debug(f"[PluginRegistry] Registered: {plugin}")


def unregister_plugin(plugin_id: str) -> None:
    """Remove a plugin by id."""
    global BACKOFFICE_PLUGINS
    BACKOFFICE_PLUGINS = [p for p in BACKOFFICE_PLUGINS if p.id != plugin_id]


def get_plugin(plugin_id: str) -> Optional[BackofficePlugin]:
    """Find a plugin by id."""
    return next((p for p in BACKOFFICE_PLUGINS if p.id == plugin_id), None)


def get_active_plugins(ctx) -> List[BackofficePlugin]:
    """
    Return plugins the current user can access.
    ctx is a BackofficeContext — used for role check.
    """
    return [p for p in BACKOFFICE_PLUGINS if p.is_accessible(ctx)]


def enable_plugin(plugin_id: str) -> None:
    p = get_plugin(plugin_id)
    if p:
        p.enabled = True


def disable_plugin(plugin_id: str) -> None:
    p = get_plugin(plugin_id)
    if p:
        p.enabled = False


# ═══════════════════════════════════════════════════════════════════════
# AUTO-DISCOVERY
# ═══════════════════════════════════════════════════════════════════════

def discover_plugins(package_path: Optional[str] = None) -> int:
    """
    Auto-discover plugins in the plugins/ directory.
    Looks for modules with PLUGIN_META dict + render() function.

    Returns count of plugins discovered.
    """
    if package_path is None:
        package_path = str(Path(__file__).parent)

    discovered = 0
    for finder, module_name, is_pkg in pkgutil.iter_modules([package_path]):
        if module_name in ("registry", "__init__"):
            continue
        try:
            full_name = f"modules.backoffice.plugins.{module_name}"
            mod = importlib.import_module(full_name)
            meta = getattr(mod, "PLUGIN_META", None)
            render_fn = getattr(mod, "render", None)

            if meta and callable(render_fn):
                plugin = BackofficePlugin(
                    id      = meta.get("id", module_name),
                    label   = meta.get("label", module_name),
                    render  = render_fn,
                    tab_key = meta.get("tab_key", "auto"),
                    roles   = meta.get("roles", []),
                    enabled = meta.get("enabled", True),
                    order   = meta.get("order", 99),
                )
                register_plugin(plugin)
                discovered += 1
                log.info(f"[PluginRegistry] Discovered: {plugin}")

        except Exception as e:
            log.warning(f"[PluginRegistry] Failed to load plugin {module_name}: {e}")

    return discovered


# ═══════════════════════════════════════════════════════════════════════
# BUILT-IN PLUGIN REGISTRATIONS
# ═══════════════════════════════════════════════════════════════════════

def _render_supplier_panel_plugin(ctx) -> None:
    """Built-in: Supplier Orders tab plugin."""
    import streamlit as st
    try:
        from modules.backoffice.supplier_panel import render_supplier_panel
        render_supplier_panel(ctx.order)
    except ImportError as e:
        st.error(f"❌ Supplier Panel module not found: {e}")
        st.info("📋 Place supplier_panel.py in modules/backoffice/")
    except Exception as e:
        st.error(f"❌ Supplier Panel error: {e}")
        import traceback
        with st.expander("Debug Info"):
            st.code(traceback.format_exc())


def _render_billing_gate_plugin(ctx) -> None:
    """Built-in: Billing Gate tab plugin."""
    import streamlit as st
    try:
        from modules.backoffice.billing_gate import render_billing_gate
        render_billing_gate(ctx.order)
    except ImportError as e:
        st.error(f"❌ Billing Gate module not found: {e}")
        st.info("📋 Place billing_gate.py in modules/backoffice/")
    except Exception as e:
        st.error(f"❌ Billing Gate error: {e}")
        import traceback
        with st.expander("Debug Info"):
            st.code(traceback.format_exc())


# Register built-ins immediately
register_plugin(BackofficePlugin(
    id      = "supplier_panel",
    label   = "🚚 Supplier Orders",
    render  = _render_supplier_panel_plugin,
    tab_key = "tab5",
    roles   = [],
    enabled = True,
    order   = 10,
))

register_plugin(BackofficePlugin(
    id      = "billing_gate",
    label   = "💳 Billing Gate",
    render  = _render_billing_gate_plugin,
    tab_key = "tab6",
    roles   = [],
    enabled = True,
    order   = 20,
))
