"""
modules/online_store/store_orders.py
======================================
Order placement, history, tracking.
Creates online_orders + mirrors to ERP orders table.
"""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional, List


def _rq(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params) or []

def _rw(sql, params=None):
    from modules.sql_adapter import run_write
    return run_write(sql, params)


def _resolve_online_order_contact_item(item: dict) -> dict:
    try:
        from modules.online_store.store_cart import _resolve_online_contact_item
        return _resolve_online_contact_item(dict(item or {}))
    except Exception:
        return item


def _next_online_order_no() -> str:
    rows = _rq("SELECT nextval('online_order_seq') AS n")
    seq = int(rows[0]["n"]) if rows else 1
    year = datetime.now().year % 100
    return f"ONL/{year:02d}/{seq:04d}"


def place_order(
    customer: dict,
    cart: dict,
    address_id: str,
    payment_method: str,  # PREPAID | COD
    promo_code: str = None,
    totals: dict = None,
) -> dict:
    """
    Returns {success, order_id, order_no, total, razorpay_order_id?}
    """
    if not cart:
        return {"success": False, "message": "Cart is empty"}
    if not address_id:
        return {"success": False, "message": "Select a delivery address"}
    cart = {
        str(k): _resolve_online_order_contact_item(v)
        for k, v in (cart or {}).items()
    }

    try:
        from modules.sql_adapter import get_transaction_connection
        conn = get_transaction_connection()
        conn.autocommit = False
        cur  = conn.cursor()
        cur.execute("BEGIN")

        oid      = str(uuid.uuid4())
        order_no = _next_online_order_no()
        tot      = totals or {}
        subtotal = float(tot.get("subtotal", 0))
        disc_amt = float(tot.get("discount", 0))
        gst_amt  = float(tot.get("gst", 0))
        delivery = float(tot.get("delivery", 0))
        total    = float(tot.get("total", subtotal))
        pay_status = "PENDING" if payment_method == "PREPAID" else "COD"

        cur.execute("""
            INSERT INTO online_orders
                (id, order_no, customer_id, address_id,
                 subtotal, discount_amt, gst_amt, delivery_charge, total,
                 promo_code, payment_method, payment_status, status)
            VALUES
                (%(id)s::uuid, %(no)s, %(cid)s::uuid, %(aid)s::uuid,
                 %(sub)s, %(disc)s, %(gst)s, %(del)s, %(tot)s,
                 %(promo)s, %(pm)s, %(ps)s, 'PLACED')
        """, {
            "id": oid, "no": order_no, "cid": customer["id"],
            "aid": address_id, "sub": subtotal, "disc": disc_amt,
            "gst": gst_amt, "del": delivery, "tot": total,
            "promo": promo_code, "pm": payment_method, "ps": pay_status,
        })

        # Insert order lines
        for item in cart.values():
            lid  = str(uuid.uuid4())
            qty  = int(item.get("qty", 1))
            up   = float(item.get("price", 0))
            gp   = float(item.get("gst_percent", 0))
            ga   = round(up * qty * gp / (100 + gp), 2)
            tp   = round(up * qty, 2)
            cur.execute("""
                INSERT INTO online_order_lines
                    (id, order_id, product_id, stock_id, product_name,
                     eye_side, sph, cyl, axis, add_power,
                     qty, unit_price, gst_percent, gst_amount, total_price)
                VALUES
                    (%(id)s::uuid, %(oid)s::uuid,
                     %(pid)s::uuid, %(sid)s::uuid,
                     %(pn)s, %(eye)s, %(sph)s, %(cyl)s, %(ax)s, %(add)s,
                     %(qty)s, %(up)s, %(gp)s, %(ga)s, %(tp)s)
            """, {
                "id":  lid, "oid": oid,
                "pid": item.get("product_id"),
                "sid": item.get("stock_id") or None,
                "pn":  item.get("product_name", ""),
                "eye": item.get("eye_side"),
                "sph": item.get("sph"), "cyl": item.get("cyl"),
                "ax":  item.get("axis"), "add": item.get("add_power"),
                "qty": qty, "up": up, "gp": gp, "ga": ga, "tp": tp,
            })

        # Increment promo usage
        if promo_code:
            cur.execute("""
                UPDATE promo_codes SET uses_count = uses_count + 1
                WHERE UPPER(code) = UPPER(%(c)s)
            """, {"c": promo_code})

        cur.execute("COMMIT")
        conn.close()

        # Mirror to ERP only when the order is actionable for staff.
        # COD can go to backoffice immediately. PREPAID must wait for verified payment.
        if payment_method == "COD":
            try:
                _mirror_to_erp(oid)
            except Exception:
                pass  # Online order remains safe; admin can retry mirror.

        # Create Razorpay order if prepaid
        razorpay_order_id = None
        if payment_method == "PREPAID":
            try:
                razorpay_order_id = _create_razorpay_order(oid, total, order_no)
            except Exception:
                pass

        return {
            "success": True, "order_id": oid, "order_no": order_no,
            "total": total, "razorpay_order_id": razorpay_order_id,
        }

    except Exception as e:
        try: conn.rollback(); conn.close()
        except: pass
        return {"success": False, "message": str(e)}


def update_payment(
    online_order_id: str,
    razorpay_payment_id: str,
    status: str,
    razorpay_order_id: str = None,
    razorpay_signature: str = None,
) -> bool:
    """
    Payment callback/update.
    - Never trust client-side "PAID" unless Razorpay signature verifies.
    - On verified PAID, mirror to ERP/backoffice exactly once.
    """
    final_status = status
    if str(status).upper() == "PAID":
        if not _verify_razorpay_payment(online_order_id, razorpay_order_id, razorpay_payment_id, razorpay_signature):
            final_status = "FAILED"

    _rw("""
        UPDATE online_orders
        SET payment_status=%(ps)s,
            razorpay_payment_id=%(rpid)s,
            status = CASE WHEN %(ps)s='PAID' THEN 'CONFIRMED' ELSE status END,
            updated_at=NOW()
        WHERE id=%(id)s::uuid
    """, {"id": online_order_id, "ps": final_status, "rpid": razorpay_payment_id})

    if final_status == "PAID":
        try:
            _mirror_to_erp(online_order_id)
        except Exception:
            # Keep online order paid/confirmed; admin can retry mirror from Orders tab.
            pass
    return final_status == "PAID"


def get_orders(customer_id: str) -> List[dict]:
    return _rq("""
        SELECT o.*, COUNT(l.id) AS line_count
        FROM online_orders o
        LEFT JOIN online_order_lines l ON l.order_id = o.id
        WHERE o.customer_id=%(cid)s::uuid
        GROUP BY o.id
        ORDER BY o.created_at DESC
        LIMIT 50
    """, {"cid": customer_id})


def get_order_lines(order_id: str, customer_id: str) -> List[dict]:
    return _rq("""
        SELECT l.*, p.product_name AS p_name
        FROM online_order_lines l
        JOIN online_orders o ON o.id = l.order_id
        LEFT JOIN products p ON p.id = l.product_id
        WHERE l.order_id=%(oid)s::uuid AND o.customer_id=%(cid)s::uuid
    """, {"oid": order_id, "cid": customer_id})


def _verify_razorpay_payment(online_order_id: str, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str) -> bool:
    """Verify Razorpay callback signature against stored online order."""
    if not (razorpay_payment_id and razorpay_signature):
        return False
    rows = _rq("""
        SELECT razorpay_order_id
        FROM online_orders
        WHERE id=%(id)s::uuid
        LIMIT 1
    """, {"id": online_order_id})
    stored_order_id = (rows[0].get("razorpay_order_id") if rows else None) or razorpay_order_id
    if not stored_order_id:
        return False
    try:
        import razorpay
        cfg = _rq("SELECT key, value FROM system_settings WHERE key IN ('RAZORPAY_KEY_ID','RAZORPAY_KEY_SECRET')")
        keys = {r["key"]: r["value"] for r in (cfg or [])}
        client = razorpay.Client(auth=(keys.get("RAZORPAY_KEY_ID", ""), keys.get("RAZORPAY_KEY_SECRET", "")))
        client.utility.verify_payment_signature({
            "razorpay_order_id": stored_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature,
        })
        return True
    except Exception:
        return False

def _create_razorpay_order(online_order_id: str, amount: float, receipt: str) -> Optional[str]:
    """Create Razorpay order. Returns razorpay_order_id."""
    try:
        import razorpay
        from modules.sql_adapter import run_query
        cfg = run_query("SELECT key, value FROM system_settings WHERE key IN ('RAZORPAY_KEY_ID','RAZORPAY_KEY_SECRET')")
        keys = {r["key"]: r["value"] for r in (cfg or [])}
        client = razorpay.Client(auth=(keys.get("RAZORPAY_KEY_ID",""), keys.get("RAZORPAY_KEY_SECRET","")))
        rp_order = client.order.create({
            "amount": int(amount * 100),  # paise
            "currency": "INR",
            "receipt": receipt,
        })
        _rw("UPDATE online_orders SET razorpay_order_id=%(rid)s WHERE id=%(id)s::uuid",
            {"rid": rp_order["id"], "id": online_order_id})
        return rp_order["id"]
    except Exception:
        return None


def _mirror_to_erp(online_order_id: str):
    """
    Create/repair matching ERP order + order_lines for backoffice handling.
    Idempotent: safe to call from COD placement, prepaid payment callback, or admin retry.
    """
    from modules.sql_adapter import get_transaction_connection
    conn = get_transaction_connection()
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute("""
            SELECT oo.*, oc.name AS customer_name, oc.mobile, oc.party_id,
                   ca.recipient, ca.line1, ca.line2, ca.city, ca.state, ca.pincode, ca.phone
            FROM online_orders oo
            JOIN online_customers oc ON oc.id = oo.customer_id
            LEFT JOIN customer_addresses ca ON ca.id = oo.address_id
            WHERE oo.id=%(id)s::uuid
            FOR UPDATE
        """, {"id": online_order_id})
        order = cur.fetchone()
        if not order:
            raise ValueError("Online order not found")
        cols = [d[0] for d in cur.description]
        order = dict(zip(cols, order))

        # PREPAID must be paid before staff sees it in ERP. COD is allowed immediately.
        if order.get("payment_method") == "PREPAID" and order.get("payment_status") != "PAID":
            conn.rollback(); return None

        if order.get("erp_order_id"):
            conn.commit(); return str(order["erp_order_id"])

        erp_oid = str(uuid.uuid4())
        party_id = order.get("party_id")
        party_name = order.get("customer_name") or order.get("recipient") or "Online Customer"
        shipping_note = ", ".join(str(x) for x in [order.get("recipient"), order.get("line1"), order.get("line2"), order.get("city"), order.get("state"), order.get("pincode"), order.get("phone")] if x)

        cur.execute("""
            INSERT INTO orders
                (id, order_no, party_id, party_name, patient_name,
                 order_type, status, total_value, source, order_source, online_order_id, remarks)
            VALUES
                (%(id)s::uuid, %(no)s, %(pid)s, %(pname)s, %(patient)s,
                 'RETAIL', 'CONFIRMED', %(tv)s, 'ONLINE', 'ONLINE', %(online_id)s::uuid, %(remarks)s)
            ON CONFLICT (order_no) DO NOTHING
        """, {
            "id": erp_oid,
            "no": order.get("order_no"),
            "pid": party_id,
            "pname": party_name,
            "patient": party_name,
            "tv": float(order.get("total") or 0),
            "online_id": online_order_id,
            "remarks": f"Online order. Ship to: {shipping_note}",
        })

        cur.execute("SELECT id::text FROM orders WHERE order_no=%(no)s LIMIT 1", {"no": order.get("order_no")})
        erp_row = cur.fetchone()
        erp_oid = erp_row[0] if erp_row else erp_oid

        cur.execute("""
            SELECT * FROM online_order_lines
            WHERE order_id=%(oid)s::uuid
            ORDER BY id
        """, {"oid": online_order_id})
        line_cols = [d[0] for d in cur.description]
        online_lines = [dict(zip(line_cols, r)) for r in cur.fetchall()]

        for line in online_lines:
            # Do not duplicate if mirror is retried.
            cur.execute("""
                SELECT 1 FROM order_lines
                WHERE order_id=%(oid)s::uuid
                  AND COALESCE(lens_params->>'online_line_id','') = %(olid)s
                LIMIT 1
            """, {"oid": erp_oid, "olid": str(line.get("id"))})
            if cur.fetchone():
                continue

            qty = int(line.get("qty") or 1)
            stock_id = line.get("stock_id")
            allocated_qty = 0

            # Reserve stock if a stock_id exists. This prevents online oversell.
            if stock_id:
                cur.execute("""
                    SELECT quantity FROM inventory_stock
                    WHERE id=%(sid)s::uuid
                    FOR UPDATE
                """, {"sid": stock_id})
                stock_row = cur.fetchone()
                available = int(stock_row[0] or 0) if stock_row else 0
                if available < qty:
                    raise ValueError(f"Insufficient stock for online line {line.get('product_name')}")
                cur.execute("""
                    UPDATE inventory_stock
                    SET quantity = quantity - %(qty)s
                    WHERE id=%(sid)s::uuid
                """, {"qty": qty, "sid": stock_id})
                allocated_qty = qty

            cur.execute("""
                INSERT INTO order_lines
                    (id, order_id, product_id, eye_side,
                     quantity, billing_qty, allocated_qty, ready_qty,
                     unit_price, total_price, gst_percent, gst_amount,
                     sph, cyl, axis, add_power,
                     lens_params)
                VALUES
                    (%(id)s::uuid, %(oid)s::uuid, NULLIF(%(pid)s::text,'')::uuid, %(eye)s,
                     %(qty)s, %(qty)s, %(alloc)s, %(ready)s,
                     %(up)s, %(tp)s, %(gp)s, %(ga)s,
                     %(sph)s, %(cyl)s, %(axis)s, %(addp)s,
                     %(lens_params)s::jsonb)
            """, {
                "id": str(uuid.uuid4()),
                "oid": erp_oid,
                "pid": line.get("product_id") or None,  # NULLIF-safe: psycopg2 sends None as NULL
                "eye": line.get("eye_side") or "B",
                "qty": qty,
                "alloc": allocated_qty,
                "ready": allocated_qty,
                "up": float(line.get("unit_price") or 0),
                "tp": float(line.get("total_price") or 0),
                "gp": float(line.get("gst_percent") or 0),
                "ga": float(line.get("gst_amount") or 0),
                "sph": line.get("sph"),
                "cyl": line.get("cyl"),
                "axis": line.get("axis"),
                "addp": line.get("add_power"),
                "lens_params": __import__("json").dumps({
                    "manufacturing_route": "STOCK",
                    "source": "ONLINE",
                    "online_order_id": online_order_id,
                    "online_line_id": str(line.get("id")),
                    "stock_id": str(stock_id) if stock_id else None,
                }),
            })

        cur.execute("""
            UPDATE online_orders
            SET erp_order_id=%(eid)s::uuid, status='CONFIRMED', updated_at=NOW()
            WHERE id=%(id)s::uuid
        """, {"eid": erp_oid, "id": online_order_id})
        conn.commit()
        return erp_oid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
