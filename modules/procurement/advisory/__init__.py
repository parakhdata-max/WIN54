"""
procurement/advisory/__init__.py
Clean public API for the advisory procurement domain.
"""
from .advisory_service import (
    ADVISORY_GROUPS,
    build_smart_alerts_v2,
    load_advisory_inventory,
    compute_alerts,
    get_ranked_suppliers_for_product,
    create_quick_refill_po,
    bundle_alerts_into_pos,
    load_products_for_group,
    save_threshold,
)
from .advisory_tracker import (
    get_advisory_pos,
    get_po_line_items,
    update_po_status,
    snooze_product_alert,
    get_advisory_summary,
)
from .advisory_po_engine import (
    format_adv_whatsapp_message,
    create_advisory_po,
)

__all__ = [
    "ADVISORY_GROUPS",
    "build_smart_alerts_v2",
    "load_advisory_inventory",
    "compute_alerts",
    "get_ranked_suppliers_for_product",
    "create_quick_refill_po",
    "bundle_alerts_into_pos",
    "load_products_for_group",
    "save_threshold",
    "get_advisory_pos",
    "get_po_line_items",
    "update_po_status",
    "snooze_product_alert",
    "get_advisory_summary",
    "format_adv_whatsapp_message",
    "create_advisory_po",
]
