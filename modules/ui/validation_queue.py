"""
Pending orders queue
"""

_pending_orders = []


def add(order_id, result):

    _pending_orders.append({
        "order_id": order_id,
        "result": result
    })


def show():

    print("\n⏳ Pending Orders")

    for o in _pending_orders:

        print(f"- Order {o['order_id']}")
