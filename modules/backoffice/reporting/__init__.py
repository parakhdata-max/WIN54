"""
reporting/__init__.py
Public exports for the reporting layer.
"""
from .supplier_performance import render_supplier_performance_dashboard
from .product_velocity     import render_product_velocity_dashboard
from .margin_leakage       import render_margin_leakage_report

__all__ = [
    "render_supplier_performance_dashboard",
    "render_product_velocity_dashboard",
    "render_margin_leakage_report",
]
