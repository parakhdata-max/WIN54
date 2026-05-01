"""
modules/procurement/__init__.py
Procurement domain public API.
"""
from .purchase_ui import render_purchase_ui

# Zone 1 — Advisory
from .advisory import (
    build_smart_alerts_v2,
    load_advisory_inventory,
    compute_alerts,
    create_quick_refill_po,
    bundle_alerts_into_pos,
    get_advisory_pos,
    snooze_product_alert,
    get_advisory_summary,
    format_adv_whatsapp_message,
    create_advisory_po,
)

# Zone 3 — PO Engine
from .po_engine import (
    create_po,
    send_po,
    get_po,
    update_po_status,
    format_po_message,
    POItem,
    POResult,
    SOURCE_DAILY,
    SOURCE_ADVISORY,
    SOURCE_AUTO,
)

__all__ = [
    "render_purchase_ui",
    # advisory
    "build_smart_alerts_v2",
    "load_advisory_inventory",
    "compute_alerts",
    "create_quick_refill_po",
    "bundle_alerts_into_pos",
    "get_advisory_pos",
    "snooze_product_alert",
    "get_advisory_summary",
    "format_adv_whatsapp_message",
    "create_advisory_po",
    # po_engine
    "create_po",
    "send_po",
    "get_po",
    "update_po_status",
    "format_po_message",
    "POItem",
    "POResult",
    "SOURCE_DAILY",
    "SOURCE_ADVISORY",
    "SOURCE_AUTO",
]
