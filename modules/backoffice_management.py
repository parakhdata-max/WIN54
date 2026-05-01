"""
Backoffice Management - Enhanced Compatibility Wrapper v2.0

Features:
- Backward compatibility
- Performance logging
- Lazy imports with structured error handling
- Kernel integration
- Feature flag support
- Type hints
"""

import logging
import time
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


# ==============================================================================
# VERSION & KERNEL
# ==============================================================================

BACKOFFICE_MANAGEMENT_VERSION = "2.0.0"

# Lazy kernel import
_KERNEL = None


def _get_kernel():
    """Lazy load kernel to avoid circular imports"""
    global _KERNEL
    if _KERNEL is None:
        try:
            from .backoffice_kernel import (
                MODULES, FEATURES, initialize_kernel, 
                get_execution_mode, is_development
            )
            _KERNEL = {
                "modules": MODULES,
                "features": FEATURES,
                "init": initialize_kernel,
                "get_mode": get_execution_mode,
                "is_dev": is_development
            }
        except ImportError:
            logger.warning("[WRAPPER] Kernel not available - running in compatibility mode")
            _KERNEL = {}
    return _KERNEL


# ==============================================================================
# PERFORMANCE LOGGING
# ==============================================================================

_BO_START = time.time()


def _log(msg: str, level: str = "info"):
    """Enhanced performance logger with mode awareness"""
    elapsed = round(time.time() - _BO_START, 3)
    
    kernel = _get_kernel()
    if kernel and kernel.get("is_dev") and kernel["is_dev"]():
        # Verbose logging in development
        log_msg = f"[PERF][BACKOFFICE] {msg} | {elapsed}s"
    else:
        # Minimal logging in production
        log_msg = f"[BO] {msg}"
    
    if level == "info":
        logger.info(log_msg)
    elif level == "warning":
        logger.warning(log_msg)
    elif level == "error":
        logger.error(log_msg)


_log("Backoffice wrapper v2.0 loading")


# ==============================================================================
# SCHEMA SAFETY ADAPTER
# ==============================================================================

def safe_load_lines(raw_lines: list) -> list:
    """
    Normalize order lines loaded from the database through the schema contract.

    Call this anywhere the backoffice reads lines from DB or session state.
    Heals old-schema rows (pre-v2) transparently — no migrations needed.

    Usage:
        lines = safe_load_lines(db_order.get("lines", []))
    """
    try:
        from modules.core.order_schema import safe_load_lines as _schema_safe
        return _schema_safe(raw_lines)
    except Exception as exc:
        logger.warning("[WRAPPER] Schema normalization unavailable: %s — returning raw lines", exc)
        return raw_lines or []


# ==============================================================================
# LAZY IMPORT CACHE (with typing)
# ==============================================================================

_BACKOFFICE_CACHE: Dict[str, Any] = {}


def _lazy_import() -> Dict[str, Any]:
    """
    Lazy import with structured error handling.
    
    Returns:
        Dictionary of imported modules and functions
        
    Raises:
        ImportError: If critical modules cannot be loaded
    """
    
    if _BACKOFFICE_CACHE:
        return _BACKOFFICE_CACHE
    
    _log("Loading backoffice package")
    
    kernel = _get_kernel()
    
    # ==================================================
    # CORE MODULES (Critical - must load)
    # ==================================================
    
    try:
        from .backoffice import backoffice
        _log("✓ Core backoffice loaded")
        if kernel and "modules" in kernel:
            kernel["modules"].mark_loaded("backoffice", success=True)
    except Exception as e:
        _log(f"✗ Core backoffice failed: {e}", "error")
        if kernel and "modules" in kernel:
            kernel["modules"].mark_loaded("backoffice", success=False, error=str(e))
        logging.exception("Critical: Backoffice core load failed")
        raise
    
    # ==================================================
    # HELPERS MODULE (Critical)
    # ==================================================
    
    try:
        from .backoffice.backoffice_helpers import (
            fmt_num,
            fmt_signed,
            get_display_order_id,
            power_key,
            compute_jobcard_power,
            sync_power_to_ui,
            force_power_refresh,
            resolve_stock_batch,
            get_max_historical_price,
            categorize_order_lines,
            load_orders_from_database,
            run_system_health_check
        )
        _log("✓ Helpers loaded")
    except Exception as e:
        _log(f"✗ Helpers failed: {e}", "error")
        logging.exception("Critical: Helpers load failed")
        raise
    
    # ==================================================
    # LOGIC MODULE (Critical)
    # ==================================================
    
    try:
        from .backoffice.backoffice_logic import (
            update_manufacturing_power,
            update_batch_allocation,
            update_line_billing,
            recalculate_order_totals,
            refresh_line_state
        )
        _log("✓ Logic loaded")
    except Exception as e:
        _log(f"✗ Logic failed: {e}", "error")
        logging.exception("Critical: Logic load failed")
        raise
    
    # ==================================================
    # UI MODULE (Critical)
    # ==================================================
    
    try:
        from .backoffice.backoffice_ui import (
            render_product_info_display,
            render_product_sync_option,
            product_change_dialog,
            render_power_edit_ui,
            render_lens_params_edit_ui,
            render_boxing_params_edit_ui,
            render_qty_finalization_ui,
            render_allocation_window,
            show_supplier_order_section,
            generate_job_cards,
            generate_lab_orders,
            generate_labels,
            show_status_update_modal
        )
        _log("✓ UI loaded")
        if kernel and "modules" in kernel:
            kernel["modules"].mark_loaded("backoffice_ui", success=True)
    except Exception as e:
        _log(f"✗ UI failed: {e}", "error")
        if kernel and "modules" in kernel:
            kernel["modules"].mark_loaded("backoffice_ui", success=False, error=str(e))
        logging.exception("Critical: UI load failed")
        raise
    
    # ==================================================
    # PRODUCTION MODULE (Optional)
    # ==================================================
    
    production_page = None
    if kernel and "features" in kernel:
        if kernel["features"].is_enabled("production_module"):
            try:
                from .backoffice.production_page import render_production_page
                production_page = render_production_page
                _log("✓ Production loaded")
                kernel["modules"].mark_loaded("production_page", success=True)
            except Exception as e:
                _log(f"⚠ Production unavailable: {e}", "warning")
                kernel["modules"].mark_loaded("production_page", success=False, error=str(e))
    else:
        # No kernel - load anyway
        try:
            from .backoffice.production_page import render_production_page
            production_page = render_production_page
            _log("✓ Production loaded")
        except Exception as e:
            _log(f"⚠ Production unavailable: {e}", "warning")
    
    # ==================================================
    # SUPPLIER PANEL (Optional)
    # ==================================================
    
    supplier_panel = None
    if kernel and "features" in kernel:
        if kernel["features"].is_enabled("supplier_panel"):
            try:
                from .backoffice.supplier_panel import render_supplier_panel
                supplier_panel = render_supplier_panel
                _log("✓ Supplier panel loaded")
                kernel["modules"].mark_loaded("supplier_panel", success=True)
            except Exception as e:
                _log(f"⚠ Supplier panel unavailable: {e}", "warning")
                kernel["modules"].mark_loaded("supplier_panel", success=False, error=str(e))
    else:
        try:
            from .backoffice.supplier_panel import render_supplier_panel
            supplier_panel = render_supplier_panel
            _log("✓ Supplier panel loaded")
        except Exception as e:
            _log(f"⚠ Supplier panel unavailable: {e}", "warning")
    
    # ==================================================
    # BILLING GATE (Optional)
    # ==================================================
    
    billing_gate = None
    if kernel and "features" in kernel:
        if kernel["features"].is_enabled("billing_gate"):
            try:
                from .backoffice.billing_gate import render_billing_gate
                billing_gate = render_billing_gate
                _log("✓ Billing gate loaded")
                kernel["modules"].mark_loaded("billing_gate", success=True)
            except Exception as e:
                _log(f"⚠ Billing gate unavailable: {e}", "warning")
                kernel["modules"].mark_loaded("billing_gate", success=False, error=str(e))
    else:
        try:
            from .backoffice.billing_gate import render_billing_gate
            billing_gate = render_billing_gate
            _log("✓ Billing gate loaded")
        except Exception as e:
            _log(f"⚠ Billing gate unavailable: {e}", "warning")
    
    # ==================================================
    # SIDEBAR (Optional)
    # ==================================================
    
    sidebar = None
    if kernel and "features" in kernel:
        if kernel["features"].is_enabled("sidebar_dashboard"):
            try:
                from .backoffice.backoffice_sidebar import render_backoffice_sidebar
                sidebar = render_backoffice_sidebar
                _log("✓ Sidebar loaded")
                kernel["modules"].mark_loaded("backoffice_sidebar", success=True)
            except Exception as e:
                _log(f"⚠ Sidebar unavailable: {e}", "warning")
                kernel["modules"].mark_loaded("backoffice_sidebar", success=False, error=str(e))
    else:
        try:
            from .backoffice.backoffice_sidebar import render_backoffice_sidebar
            sidebar = render_backoffice_sidebar
            _log("✓ Sidebar loaded")
        except Exception as e:
            _log(f"⚠ Sidebar unavailable: {e}", "warning")
    
    # ==================================================
    # WORKFLOW STATUS (Legacy compatibility)
    # ==================================================
    
    try:
        from modules.workflow.status import OrderStatus
    except Exception:
        OrderStatus = None
        _log("⚠ OrderStatus enum unavailable", "warning")
    
    # ==================================================
    # POPULATE CACHE
    # ==================================================
    
    _BACKOFFICE_CACHE.update({
        # Main entry points
        "render_backoffice_management": backoffice.render_backoffice_management,
        "render_backoffice_dashboard": backoffice.render_backoffice_dashboard,
        "render_order_detail": backoffice.render_order_detail,
        "init_backoffice_state": backoffice.init_backoffice_state,
        
        # Schema safety (prevents field-change crashes)
        "safe_load_lines": safe_load_lines,
        "fmt_num": fmt_num,
        "fmt_signed": fmt_signed,
        "get_display_order_id": get_display_order_id,
        "power_key": power_key,
        "compute_jobcard_power": compute_jobcard_power,
        "sync_power_to_ui": sync_power_to_ui,
        "force_power_refresh": force_power_refresh,
        "resolve_stock_batch": resolve_stock_batch,
        "get_max_historical_price": get_max_historical_price,
        "categorize_order_lines": categorize_order_lines,
        "load_orders_from_database": load_orders_from_database,
        "run_system_health_check": run_system_health_check,
        
        # Logic
        "update_manufacturing_power": update_manufacturing_power,
        "update_batch_allocation": update_batch_allocation,
        "update_line_billing": update_line_billing,
        "recalculate_order_totals": recalculate_order_totals,
        "refresh_line_state": refresh_line_state,
        
        # Enums
        "OrderStatus": OrderStatus,
        
        # UI
        "render_product_info_display": render_product_info_display,
        "render_product_sync_option": render_product_sync_option,
        "product_change_dialog": product_change_dialog,
        "render_power_edit_ui": render_power_edit_ui,
        "render_lens_params_edit_ui": render_lens_params_edit_ui,
        "render_boxing_params_edit_ui": render_boxing_params_edit_ui,
        "render_qty_finalization_ui": render_qty_finalization_ui,
        "render_allocation_window": render_allocation_window,
        "show_supplier_order_section": show_supplier_order_section,
        "generate_job_cards": generate_job_cards,
        "generate_lab_orders": generate_lab_orders,
        "generate_labels": generate_labels,
        "show_status_update_modal": show_status_update_modal,
        
        # Optional modules (may be None)
        "render_production_page": production_page,
        "render_supplier_panel": supplier_panel,
        "render_billing_gate": billing_gate,
        "render_backoffice_sidebar": sidebar,
    })
    
    _log("Backoffice package loaded successfully")
    
    return _BACKOFFICE_CACHE


# ==============================================================================
# PUBLIC PROXY FUNCTIONS
# ==============================================================================

def __getattr__(name: str):
    """
    Lazy attribute access with enhanced error handling.
    
    This enables:
        from modules.backoffice_management import render_backoffice_management
    
    Without importing everything upfront.
    """
    
    data = _lazy_import()
    
    if name in data:
        attr = data[name]
        if attr is None:
            raise AttributeError(
                f"Module '{name}' is registered but not loaded. "
                f"Check feature flags or module dependencies."
            )
        return attr
    
    raise AttributeError(
        f"module 'backoffice_management' has no attribute '{name}'"
    )


# ==============================================================================
# SELF-TEST HOOK
# ==============================================================================

def self_test() -> Dict[str, Any]:
    """
    Run backoffice self-test.
    
    Can be called from:
    - Startup health check
    - Admin panel
    - CI/CD pipeline
    
    Returns:
        Dict with test results
    """
    
    _log("Running self-test")
    
    try:
        # Force lazy import
        _lazy_import()
        
        # Get kernel if available
        kernel = _get_kernel()
        
        if kernel and "init" in kernel:
            # Run kernel self-test
            return kernel["init"]()
        else:
            # Compatibility mode - basic check
            return {
                "status": "healthy",
                "mode": "compatibility",
                "modules_loaded": len(_BACKOFFICE_CACHE),
                "version": BACKOFFICE_MANAGEMENT_VERSION
            }
    
    except Exception as e:
        _log(f"Self-test failed: {e}", "error")
        logging.exception("Self-test failed")
        return {
            "status": "unhealthy",
            "error": str(e),
            "version": BACKOFFICE_MANAGEMENT_VERSION
        }


# ==============================================================================
# EXPORTS
# ==============================================================================

__all__ = [
    # Version
    "BACKOFFICE_MANAGEMENT_VERSION",
    "self_test",
    
    # Schema safety adapter
    "safe_load_lines",
    
    # Main entry points (lazy-loaded via __getattr__)
    "render_backoffice_management",
    "render_backoffice_dashboard",
    "render_order_detail",
    "init_backoffice_state",
    
    # Helpers
    "fmt_num",
    "fmt_signed",
    "get_display_order_id",
    "power_key",
    "compute_jobcard_power",
    "sync_power_to_ui",
    "force_power_refresh",
    "resolve_stock_batch",
    "get_max_historical_price",
    "categorize_order_lines",
    "load_orders_from_database",
    "run_system_health_check",
    
    # Logic
    "update_manufacturing_power",
    "update_batch_allocation",
    "update_line_billing",
    "recalculate_order_totals",
    "refresh_line_state",
    
    # Enums
    "OrderStatus",
    
    # UI
    "render_product_info_display",
    "render_product_sync_option",
    "product_change_dialog",
    "render_power_edit_ui",
    "render_lens_params_edit_ui",
    "render_boxing_params_edit_ui",
    "render_qty_finalization_ui",
    "render_allocation_window",
    "show_supplier_order_section",
    "generate_job_cards",
    "generate_lab_orders",
    "generate_labels",
    "show_status_update_modal",
    
    # Optional modules
    "render_production_page",
    "render_supplier_panel",
    "render_billing_gate",
    "render_backoffice_sidebar",
]
