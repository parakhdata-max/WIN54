"""
modules/procurement/purchase_engine.py

Purchase Engine — Goods Receipt + Stock Update Logic
=====================================================
STATUS: Stub — implementation pending.

Will contain:
    - receive_purchase_order(po_id, received_lines)  → updates stock batches
    - validate_grn(grn_data)                         → GRN validation rules
    - close_purchase_order(po_id)                    → marks PO as complete

Currently the purchase flow is handled in:
    modules/procurement/purchase_invoice.py  (GRN UI)
    modules/procurement/purchase_ui.py       (Purchase order UI)

This engine file will centralize the business logic
when those modules are refactored.
"""

# TODO: implement receive_purchase_order()
# TODO: implement validate_grn()
# TODO: implement close_purchase_order()
