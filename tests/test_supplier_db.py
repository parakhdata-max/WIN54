from modules.sql_adapter import (
    save_supplier_order,
    fetch_supplier_orders,
    update_supplier_order_status
)

import datetime


def test_supplier():

    order = {
        "supplier_order_id": "SO-TEST-001",
        "supplier_id": "SUP01",
        "supplier_name": "Test Supplier",

        "customer_order_id": None,

        "order_date": datetime.datetime.now(),
        "expected_delivery_date": None,

        "priority": "NORMAL",
        "payment_terms": "NET 30",

        "special_instructions": "Test order",

        "status": "SENT",

        "total_items": 1,
        "total_qty": 2,
        "total_value": 500,

        "created_by": "tester",
        "created_at": datetime.datetime.now(),
        "updated_at": datetime.datetime.now(),

        "items": [
            {
                "item_no": 1,
                "product_id": "P01",
                "product_name": "Test Lens",
                "brand": "ABC",
                "eye_side": "R",

                "sph": -1.0,
                "cyl": -0.5,
                "axis": 90,

                "add_power": None,

                "ordered_qty": 2,
                "received_qty": 0,
                "pending_qty": 2,

                "unit_price": 250,
                "total_price": 500,

                "customer_line_id": "LINE01",
                "item_status": "PENDING"
            }
        ],

        "status_history": [
            {
                "status": "SENT",
                "timestamp": datetime.datetime.now(),
                "notes": "Initial test",
                "changed_by": "tester"
            }
        ]
    }

    print("Saving...")
    save_supplier_order(order)

    print("Fetching...")
    data = fetch_supplier_orders()
    print(data)

    print("Updating status...")
    update_supplier_order_status(
        "SO-TEST-001",
        "ACKNOWLEDGED",
        "Supplier confirmed",
        "tester"
    )

    print("Done")


test_supplier()
