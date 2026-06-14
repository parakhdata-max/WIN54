"""
Compatibility wrapper for procurement invoice matching.

The canonical supplier-product mapping UI lives in
``modules.backoffice.supplier_product_map_ui``.  Invoice Match historically
imports ``modules.procurement.supplier_product_map_ui.upsert_supplier_map``,
so keep this small adapter to avoid breaking that workflow.
"""
from __future__ import annotations


def upsert_supplier_map(
    product_id: str,
    supplier_id: str,
    supplier_product_name: str,
    supplier_brand: str = "",
    supplier_index: str = "",
    supplier_coating: str = "",
    supplier_treatment: str = "",
    route_type: str = "VENDOR",
    notes: str = "",
) -> bool:
    """Save/update supplier mapping for Invoice Match training."""
    try:
        from modules.backoffice.supplier_product_map_ui import _upsert_supplier_mapping

        return bool(
            _upsert_supplier_mapping(
                product_id=product_id,
                supplier_id=supplier_id,
                supplier_product_name=supplier_product_name,
                supplier_brand=supplier_brand,
                supplier_index=supplier_index,
                supplier_coating=supplier_coating,
                supplier_treatment=supplier_treatment,
                route_type=route_type,
                notes=notes,
            )
        )
    except Exception:
        return False


def get_supplier_product_name(product_id: str, supplier_id: str) -> dict:
    """Delegate lookup to the canonical backoffice mapping module."""
    try:
        from modules.backoffice.supplier_product_map_ui import get_supplier_product_name as _lookup

        return _lookup(product_id, supplier_id) or {}
    except Exception:
        return {}
