"""
backoffice/fulfillment/__init__.py
====================================
Fulfillment package — public API.

Usage:
    from modules.backoffice.fulfillment import (
        render_fulfillment_panel,
        render_fulfillment_header,
        render_supplier_order_panel,
        render_assignment_panel_block,
        check_assignment_guard,
    )
    from modules.backoffice.fulfillment import decision_engine
"""

from .ui import (
    render_fulfillment_panel,
    render_fulfillment_header,
    render_supplier_order_panel,
    render_assignment_panel_block,
    check_assignment_guard,
)
from . import decision_engine

__all__ = [
    "render_fulfillment_panel",
    "render_fulfillment_header",
    "render_supplier_order_panel",
    "render_assignment_panel_block",
    "check_assignment_guard",
    "decision_engine",
]
