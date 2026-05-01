"""
procurement/advisory/advisory_po_engine.py
============================================
Advisory PO Engine — thin wrapper over po_engine.py with
advisory-specific WhatsApp formatting.

STEP 1.3 — format_adv_whatsapp_message()
"""

import logging
from typing import Dict, List

from modules.procurement.po_engine import (
    create_po,
    send_po,
    format_po_message,
    POItem,
    SOURCE_ADVISORY,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# STEP 1.3 — ADVISORY WHATSAPP FORMATTER
# ═══════════════════════════════════════════════════════════════════════

def format_adv_whatsapp_message(po: Dict) -> str:
    """
    Clean supplier message for WhatsApp — advisory-specific format.

    Advisory POs have a friendlier tone:
      - Starts with advisory context
      - Groups items clearly
      - Closes with reorder urgency note

    Args:
        po: PO dict with keys: po_number, supplier (or supplier_name),
            items (list of {name, qty}), notes (optional)

    Returns:
        Formatted WhatsApp message string.
    """
    po_number     = po.get("po_number", "N/A")
    supplier      = po.get("supplier_name") or po.get("supplier", "Supplier")
    items: list   = po.get("items") or []
    notes: str    = po.get("notes", "")

    lines = [
        f"*Advisory Purchase Order — {po_number}*",
        f"Dear {supplier},",
        "",
        "We would like to restock the following items:",
        "",
        "Items:",
    ]

    for item in items:
        if isinstance(item, dict):
            name = item.get("product_name") or item.get("name", "N/A")
            qty  = item.get("qty", item.get("quantity", 1))
            lines.append(f"  • {name}  ×  {qty}")

    lines.append("")

    if notes:
        lines.append(f"Note: {notes}")
        lines.append("")

    lines += [
        "Kindly confirm availability and dispatch timeline.",
        "Thank you.",
    ]

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# ADVISORY PO CREATION (wraps po_engine with advisory defaults)
# ═══════════════════════════════════════════════════════════════════════

def create_advisory_po(
    supplier_id:   str,
    supplier_name: str,
    items:         List[Dict],
    notes:         str = "",
    send_whatsapp: bool = False,
) -> Dict:
    """
    Create an advisory PO and optionally send via WhatsApp.

    Args:
        items: list of {"product_id": ..., "product_name": ..., "qty": ...}

    Returns:
        {"success": bool, "po_id": str, "po_number": str, "message": str}
    """
    po_items = [
        POItem(
            product_id=str(i.get("product_id", "")),
            product_name=str(i.get("product_name", i.get("name", ""))),
            qty=int(i.get("qty", 1)),
        )
        for i in items
    ]

    result = create_po(
        source=SOURCE_ADVISORY,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        items=po_items,
        notes=notes,
    )

    if result.success and send_whatsapp:
        from modules.procurement.po_engine import get_po
        po_dict = get_po(result.po_id) or {}
        msg = format_adv_whatsapp_message(po_dict)
        send_po(result.po_id, channel="whatsapp")
        log.info(f"[AdvisoryPOEngine] WhatsApp sent for {result.po_number}")

    return {
        "success":   result.success,
        "po_id":     result.po_id or "",
        "po_number": result.po_number or "",
        "message":   result.message,
        "error":     result.error,
    }
