"""
Billing Module
==============

Handles challan and invoice generation for orders based on party billing preferences.
"""

from .challan_invoice_manager import (
    # Dashboard
    render_billing_dashboard,

    # Creation UI
    render_challan_creation,
    render_invoice_creation,

    # Engine functions
    create_challan,
    create_invoice,

    # Party helpers
    get_party_billing_preference,
    get_pending_orders_for_party,

    # Partial billing helpers (new)
    get_unbilled_lines_for_order,

    # Document detail fetchers
    get_challan_details,
    get_invoice_details,

    # Status updates
    update_challan_status,
    update_invoice_status,
)

__all__ = [
    'render_billing_dashboard',
    'render_challan_creation',
    'render_invoice_creation',
    'create_challan',
    'create_invoice',
    'get_party_billing_preference',
    'get_pending_orders_for_party',
    'get_unbilled_lines_for_order',
    'get_challan_details',
    'get_invoice_details',
    'update_challan_status',
    'update_invoice_status',
]
