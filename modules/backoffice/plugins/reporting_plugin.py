"""
plugins/reporting_plugin.py
============================
Reporting Dashboard — Plugin for backoffice shell.

Auto-discovered by registry.discover_plugins().
Adds a "📈 Reports" tab to the backoffice order view.

PLUGIN_META is required for auto-discovery.
"""

import streamlit as st

PLUGIN_META = {
    "id":      "reporting_dashboard",
    "label":   "📈 Reports",
    "tab_key": "tab7",
    "roles":   ["manager", "admin"],   # restrict to managers; [] = no restriction
    "enabled": True,
    "order":   30,
}


def render(ctx) -> None:
    """Reporting tab plugin entry point."""
    st.subheader("📈 Business Reports")
    st.caption("Available reports for this ERP instance")

    report = st.selectbox(
        "Select Report",
        [
            "Supplier Performance",
            "Product Velocity",
            "Margin Leakage",
        ],
        key="reporting_plugin_select",
    )

    st.markdown("---")

    if report == "Supplier Performance":
        from modules.backoffice.reporting.supplier_performance import (
            render_supplier_performance_dashboard,
        )
        render_supplier_performance_dashboard(ctx)

    elif report == "Product Velocity":
        from modules.backoffice.reporting.product_velocity import (
            render_product_velocity_dashboard,
        )
        render_product_velocity_dashboard(ctx)

    elif report == "Margin Leakage":
        from modules.backoffice.reporting.margin_leakage import (
            render_margin_leakage_report,
        )
        render_margin_leakage_report(ctx)
