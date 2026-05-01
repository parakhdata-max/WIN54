"""
Order Engine (Core Business Logic)
=================================

• No UI
• No Streamlit
• No imports from UI
• Only pure order processing

This is the heart of the system.
"""

import uuid
import datetime
import copy
from typing import Dict, List, Optional



def convert_cart_to_order(
    cart_lines: List[Dict],
    order_info: Dict,
    forced_order_id: Optional[str] = None
) -> Dict:

    order_id = forced_order_id or f"PO-{uuid.uuid4().hex[:8].upper()}"

    now = datetime.datetime.now().isoformat()

    total_items = len(cart_lines)

    total_value = sum(
        float(line.get("total_price", 0) or 0)
        for line in cart_lines
    )

    # ===============================
    # 🔑 CREATE PERMANENT PAIR ID
    # ===============================
    cart_copy = copy.deepcopy(cart_lines)

    pair_base = str(uuid.uuid4())[:8]

    for i, line in enumerate(cart_copy):
        line["pair_id"] = f"{pair_base}-{i//2}"


    order = {
        "order_id": order_id,

        "order_type": order_info.get("order_type"),
        "party": order_info.get("party"),

        "patient_name": order_info.get("patient_name"),
        "patient_mobile": order_info.get("patient_mobile"),

        "customer_order_no": order_info.get("customer_order_no", ""),
        "order_status": "PENDING",

        "lines": cart_copy,

        "stock_lines": [],
        "lab_order_lines": [],
        "inhouse_lines": [],

        "total_items": total_items,
        "total_value": round(total_value, 2),

        "created_at": now,
        "updated_at": now,

        "notes": order_info.get("notes", ""),
        "documents": order_info.get("documents", []),

        "is_confirmed": False,
    }

    return order


