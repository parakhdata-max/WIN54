"""
Procurement ledger.

This is the stable procurement backbone used by Production Queue / Procured.
The older purchase_acknowledgements table is still written by legacy screens,
but these helpers mirror those actions into procurement_orders and
procurement_order_items so RX, Stock, Supplier, and External Lab all share one
supplier-wise lifecycle.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from psycopg2.extras import Json, RealDictCursor

from modules.sql_adapter import (
    close_connection,
    get_transaction_connection,
    run_query,
)


PROCURED_STATUSES = {"PROCURED", "RECEIVED", "INVOICED", "PURCHASE_ACKED", "READY", "LOCKED"}
ACTIVE_STATUSES = {"QUEUED", "ORDERED", "PARTIAL", "PROCURED", "RECEIVED", "INVOICED"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return float(default)


def _line_snapshot(order_line_id: str) -> Optional[Dict[str, Any]]:
    rows = run_query(
        """
        SELECT
            o.id::text AS order_id,
            o.order_no,
            ol.id::text AS order_line_id,
            ol.product_id::text AS product_id,
            COALESCE(p.product_name, '') AS product_name,
            COALESCE(ol.lens_params->>'manufacturing_route', 'STOCK') AS route,
            COALESCE(ol.eye_side, '') AS eye_side,
            COALESCE(NULLIF(ol.allocated_qty,0), NULLIF(ol.billed_qty,0), ol.quantity, 1) AS qty,
            COALESCE(ol.unit_price, 0) AS unit_price,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            ol.lens_params
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE ol.id = %(lid)s::uuid
        LIMIT 1
        """,
        {"lid": order_line_id},
    )
    return rows[0] if rows else None


def _power_json(line: Dict[str, Any]) -> Dict[str, Any]:
    lp = line.get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = json.loads(lp)
        except Exception:
            lp = {}
    return {
        "sph": line.get("sph"),
        "cyl": line.get("cyl"),
        "axis": line.get("axis"),
        "add": line.get("add_power"),
        "lens_params": lp,
    }


def _next_procurement_no(cursor, prefix: str = "PQ") -> str:
    cursor.execute("SELECT COUNT(*) + 1 AS n FROM procurement_orders")
    row = cursor.fetchone()
    if isinstance(row, dict):
        n = int(row.get("n") or 1)
    else:
        n = int((row or [1])[0] or 1)
    return f"{prefix}-{n:06d}"


def ensure_queue_item(order_line_id: str, source: str = "BACKOFFICE", status: str = "QUEUED") -> Optional[str]:
    """Ensure a procurement_order_items row exists for this order line.

    The helper mirrors stock/vendor/RX lines into the procurement ledger. If
    the line is already queued, an ORDERED update will promote its status.
    """
    line = _line_snapshot(order_line_id)
    if not line:
        return None

    actual_status = str(status or "QUEUED").upper()
    if actual_status not in ("QUEUED", "ORDERED"):
        actual_status = "QUEUED"

    conn = None
    cur = None
    try:
        conn = get_transaction_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id::text FROM procurement_order_items WHERE order_line_id=%(lid)s::uuid LIMIT 1",
            {"lid": order_line_id},
        )
        existing = cur.fetchone()
        if existing:
            qty_existing = _num(line.get("qty"), 1)
            cur.execute(
                """
                UPDATE procurement_order_items
                SET qty_requested = CASE
                        WHEN status IN ('QUEUED','ORDERED') THEN %(qty)s
                        ELSE qty_requested
                    END,
                    qty_ordered = CASE
                        WHEN procurement_order_items.status IN ('QUEUED','ORDERED')
                        THEN %(qty)s
                        ELSE procurement_order_items.qty_ordered
                    END,
                    product_name = %(pname)s,
                    route        = %(route)s,
                    eye_side     = %(eye)s,
                    power_json   = %(power)s,
                    status       = CASE
                        WHEN procurement_order_items.status IN ('QUEUED','ORDERED')
                        THEN %(status)s
                        ELSE procurement_order_items.status
                    END,
                    updated_at   = NOW()
                WHERE order_line_id=%(lid)s::uuid
                """,
                {
                    "qty": qty_existing,
                    "pname": line.get("product_name") or "",
                    "route": str(line.get("route") or "STOCK").upper(),
                    "eye": line.get("eye_side") or "",
                    "power": Json(_power_json(line)),
                    "status": actual_status,
                    "lid": order_line_id,
                },
            )
            conn.commit()
            return str(existing[0])
        cur.execute(
            """
            INSERT INTO procurement_orders
                (procurement_no, status, source_route, source, order_ref, total_qty, total_value)
            VALUES
                (%(pno)s, 'QUEUED', %(route)s, %(source)s, %(oref)s, %(qty)s, %(total)s)
            RETURNING id
            """,
            {
                "pno": _next_procurement_no(cur),
                "route": str(line.get("route") or "STOCK").upper(),
                "source": source,
                "oref": line.get("order_no") or "",
                "qty": _num(line.get("qty"), 1),
                "total": round(_num(line.get("qty"), 1) * _num(line.get("unit_price")), 2),
            },
        )
        po_id = str(cur.fetchone()[0])
        qty = _num(line.get("qty"), 1)
        unit = _num(line.get("unit_price"))
        cur.execute(
            """
            INSERT INTO procurement_order_items
                (procurement_order_id, order_id, order_line_id, product_id,
                 product_name, route, eye_side, power_json,
                 qty_requested, qty_ordered, unit_price, total_value, status, source)
            VALUES
                (%(poid)s::uuid, %(oid)s::uuid, %(lid)s::uuid, %(pid)s::uuid,
                 %(pname)s, %(route)s, %(eye)s, %(power)s,
                 %(qty)s, %(qty)s, %(unit)s, %(total)s, %(status)s, %(source)s)
            ON CONFLICT (order_line_id) DO UPDATE SET
                qty_requested = EXCLUDED.qty_requested,
                qty_ordered   = CASE
                    WHEN procurement_order_items.status IN ('QUEUED','ORDERED')
                    THEN EXCLUDED.qty_ordered
                    ELSE procurement_order_items.qty_ordered
                END,
                product_name  = EXCLUDED.product_name,
                route         = EXCLUDED.route,
                eye_side      = EXCLUDED.eye_side,
                power_json    = EXCLUDED.power_json,
                status        = CASE
                    WHEN procurement_order_items.status IN ('QUEUED','ORDERED')
                    THEN EXCLUDED.status
                    ELSE procurement_order_items.status
                END,
                updated_at    = NOW()
            RETURNING id
            """,
            {
                "poid": po_id,
                "oid": line.get("order_id"),
                "lid": line.get("order_line_id"),
                "pid": line.get("product_id"),
                "pname": line.get("product_name") or "",
                "route": str(line.get("route") or "STOCK").upper(),
                "eye": line.get("eye_side") or "",
                "power": Json(_power_json(line)),
                "qty": qty,
                "unit": unit,
                "total": round(qty * unit, 2),
                "status": actual_status,
                "source": source,
            },
        )
        item_id = str(cur.fetchone()[0])
        conn.commit()
        return item_id
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            close_connection(conn)


def ensure_queue_items(order_line_ids: Iterable[str], source: str = "BACKOFFICE") -> int:
    count = 0
    for lid in order_line_ids:
        if not lid:
            continue
        try:
            if ensure_queue_item(str(lid), source=source):
                count += 1
        except Exception:
            # Queue visibility must not break the production screen.
            continue
    return count


def record_procurement_receipt(
    *,
    line_items: List[Dict[str, Any]],
    supplier_id: str = "",
    supplier_name: str = "",
    document_no: str = "",
    document_type: str = "INVOICE",
    document_date: str = "",
    invoice_file_path: str = "",
    source: str = "PRODUCTION_QUEUE",
) -> Optional[str]:
    """Create/update a supplier-wise procurement order and mark selected lines procured."""
    if not line_items:
        return None

    conn = None
    cur = None
    try:
        conn = get_transaction_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        total_qty = sum(_num(x.get("qty"), 1) for x in line_items)
        total_value = sum(_num(x.get("qty"), 1) * _num(x.get("price")) for x in line_items)

        cur.execute(
            """
            INSERT INTO procurement_orders
                (procurement_no, supplier_id, supplier_name, status, source_route, source,
                 sent_via, order_ref, document_type, document_no, document_date,
                 invoice_file_path, total_qty, total_value, ordered_at, received_at)
            VALUES
                (%(pno)s, NULLIF(%(sid)s,'')::uuid, %(sname)s, 'PROCURED',
                 'MIXED', %(source)s, 'MANUAL', %(oref)s, %(dtype)s, %(dno)s,
                 NULLIF(%(ddate)s,'')::date, %(fpath)s, %(qty)s, %(total)s, NOW(), NOW())
            RETURNING id::text, procurement_no
            """,
            {
                "pno": _next_procurement_no(cur),
                "sid": supplier_id or "",
                "sname": supplier_name or "",
                "source": source,
                "oref": document_no or "",
                "dtype": document_type or "",
                "dno": document_no or "",
                "ddate": document_date or "",
                "fpath": invoice_file_path or "",
                "qty": total_qty,
                "total": round(total_value, 2),
            },
        )
        po = cur.fetchone()
        po_id = po["id"]

        cur.execute(
            """
            INSERT INTO procurement_receipts
                (procurement_order_id, supplier_id, supplier_name, document_type,
                 document_no, document_date, total_qty, total_value, invoice_file_path)
            VALUES
                (%(poid)s::uuid, NULLIF(%(sid)s,'')::uuid, %(sname)s, %(dtype)s,
                 %(dno)s, NULLIF(%(ddate)s,'')::date, %(qty)s, %(total)s, %(fpath)s)
            RETURNING id::text
            """,
            {
                "poid": po_id,
                "sid": supplier_id or "",
                "sname": supplier_name or "",
                "dtype": document_type or "",
                "dno": document_no or "",
                "ddate": document_date or "",
                "qty": total_qty,
                "total": round(total_value, 2),
                "fpath": invoice_file_path or "",
            },
        )
        receipt_id = cur.fetchone()["id"]

        upload_id = None
        if invoice_file_path:
            cur.execute(
                """
                INSERT INTO supplier_invoice_uploads
                    (procurement_order_id, receipt_id, supplier_id, file_name,
                     file_path, document_no, document_date, parse_status)
                VALUES
                    (%(poid)s::uuid, %(rid)s::uuid, NULLIF(%(sid)s,'')::uuid,
                     %(fname)s, %(fpath)s, %(dno)s, NULLIF(%(ddate)s,'')::date, 'UPLOADED')
                RETURNING id::text
                """,
                {
                    "poid": po_id,
                    "rid": receipt_id,
                    "sid": supplier_id or "",
                    "fname": invoice_file_path.split("\\")[-1].split("/")[-1],
                    "fpath": invoice_file_path,
                    "dno": document_no or "",
                    "ddate": document_date or "",
                },
            )
            upload_id = cur.fetchone()["id"]

        for item in line_items:
            lid = str(item.get("line_id") or item.get("order_line_id") or "")
            if not lid:
                continue
            # Ensure line snapshot exists, then lock/update it into this PO.
            ensure_queue_item(lid, source=source)
            qty = _num(item.get("qty"), 1)
            price = _num(item.get("price"))
            cur.execute(
                """
                UPDATE procurement_order_items
                SET procurement_order_id = %(poid)s::uuid,
                    supplier_id          = NULLIF(%(sid)s,'')::uuid,
                    supplier_name        = %(sname)s,
                    qty_ordered          = %(qty)s,
                    qty_received         = %(qty)s,
                    unit_price           = %(price)s,
                    total_value          = %(total)s,
                    batch_no             = COALESCE(NULLIF(%(batch)s,''), batch_no),
                    expiry_date          = COALESCE(NULLIF(%(expiry)s,'')::date, expiry_date),
                    status               = 'PROCURED',
                    ordered_at           = COALESCE(ordered_at, NOW()),
                    received_at          = NOW(),
                    updated_at           = NOW()
                WHERE order_line_id = %(lid)s::uuid
                RETURNING id::text
                """,
                {
                    "poid": po_id,
                    "sid": supplier_id or "",
                    "sname": supplier_name or "",
                    "qty": qty,
                    "price": price,
                    "total": round(qty * price, 2),
                    "batch": str(item.get("batch_no") or ""),
                    "expiry": str(item.get("expiry_date") or ""),
                    "lid": lid,
                },
            )
            updated = cur.fetchone()
            if updated and upload_id:
                cur.execute(
                    """
                    INSERT INTO supplier_invoice_matches
                        (upload_id, procurement_item_id, order_line_id,
                         match_status, match_score, matched_fields)
                    VALUES
                        (%(uid)s::uuid, %(piid)s::uuid, %(lid)s::uuid,
                         'CONFIRMED', 100, %(fields)s)
                    """,
                    {
                        "uid": upload_id,
                        "piid": updated["id"],
                        "lid": lid,
                        "fields": Json({"document_no": document_no, "manual_confirmed": True}),
                    },
                )

        conn.commit()
        return str(po.get("procurement_no") or po_id)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            close_connection(conn)


def record_scanned_invoice_items(
    *,
    supplier_id: str = "",
    supplier_name: str = "",
    document_no: str = "",
    document_type: str = "INVOICE",
    document_date: str = "",
    invoice_file_path: str = "",
    items: List[Dict[str, Any]],
    source: str = "SCANNED_INVOICE",
) -> Optional[str]:
    """Save OCR-confirmed invoice items that may not yet be matched to order_lines."""
    if not items:
        return None

    conn = None
    cur = None
    try:
        conn = get_transaction_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        total_qty = sum(_num(x.get("qty"), 0) for x in items)
        total_value = sum(_num(x.get("qty"), 0) * _num(x.get("unit_price"), 0) for x in items)
        cur.execute(
            """
            INSERT INTO procurement_orders
                (procurement_no, supplier_id, supplier_name, status, source_route, source,
                 sent_via, order_ref, document_type, document_no, document_date,
                 invoice_file_path, total_qty, total_value, ordered_at, received_at, metadata)
            VALUES
                (%(pno)s, NULLIF(%(sid)s,'')::uuid, %(sname)s, 'PROCURED',
                 'SCANNED', %(source)s, 'OCR', %(oref)s, %(dtype)s, %(dno)s,
                 NULLIF(%(ddate)s,'')::date, %(fpath)s, %(qty)s, %(total)s,
                 NOW(), NOW(), %(meta)s)
            RETURNING id::text, procurement_no
            """,
            {
                "pno": _next_procurement_no(cur),
                "sid": supplier_id or "",
                "sname": supplier_name or "",
                "source": source,
                "oref": document_no or "",
                "dtype": document_type or "",
                "dno": document_no or "",
                "ddate": document_date or "",
                "fpath": invoice_file_path or "",
                "qty": total_qty,
                "total": round(total_value, 2),
                "meta": Json({"entry_mode": "scan_review"}),
            },
        )
        po = cur.fetchone()
        po_id = po["id"]

        cur.execute(
            """
            INSERT INTO procurement_receipts
                (procurement_order_id, supplier_id, supplier_name, document_type,
                 document_no, document_date, total_qty, total_value, invoice_file_path)
            VALUES
                (%(poid)s::uuid, NULLIF(%(sid)s,'')::uuid, %(sname)s, %(dtype)s,
                 %(dno)s, NULLIF(%(ddate)s,'')::date, %(qty)s, %(total)s, %(fpath)s)
            RETURNING id::text
            """,
            {
                "poid": po_id,
                "sid": supplier_id or "",
                "sname": supplier_name or "",
                "dtype": document_type or "",
                "dno": document_no or "",
                "ddate": document_date or "",
                "qty": total_qty,
                "total": round(total_value, 2),
                "fpath": invoice_file_path or "",
            },
        )
        receipt_id = cur.fetchone()["id"]

        upload_id = None
        if invoice_file_path:
            cur.execute(
                """
                INSERT INTO supplier_invoice_uploads
                    (procurement_order_id, receipt_id, supplier_id, file_name,
                     file_path, document_no, document_date, parse_status, metadata)
                VALUES
                    (%(poid)s::uuid, %(rid)s::uuid, NULLIF(%(sid)s,'')::uuid,
                     %(fname)s, %(fpath)s, %(dno)s, NULLIF(%(ddate)s,'')::date,
                     'CONFIRMED', %(meta)s)
                RETURNING id::text
                """,
                {
                    "poid": po_id,
                    "rid": receipt_id,
                    "sid": supplier_id or "",
                    "fname": invoice_file_path.split("\\")[-1].split("/")[-1],
                    "fpath": invoice_file_path,
                    "dno": document_no or "",
                    "ddate": document_date or "",
                    "meta": Json({"entry_mode": "scan_review"}),
                },
            )
            upload_id = cur.fetchone()["id"]

        for item in items:
            qty = _num(item.get("qty"), 0)
            price = _num(item.get("unit_price"), 0)
            cur.execute(
                """
                INSERT INTO procurement_order_items
                    (procurement_order_id, order_id, order_line_id, product_id,
                     product_name, route, eye_side, power_json,
                     qty_requested, qty_ordered, qty_received,
                     unit_price, total_value, batch_no, expiry_date,
                     status, supplier_id, supplier_name, source, metadata,
                     ordered_at, received_at)
                VALUES
                    (%(poid)s::uuid, NULL, NULL, NULLIF(%(pid)s,'')::uuid,
                     %(pname)s, %(route)s, %(eye)s, %(power)s,
                     %(qty)s, %(qty)s, %(qty)s,
                     %(price)s, %(total)s, %(batch)s, NULLIF(%(expiry)s,'')::date,
                     'PROCURED', NULLIF(%(sid)s,'')::uuid, %(sname)s, %(source)s, %(meta)s,
                     NOW(), NOW())
                RETURNING id::text
                """,
                {
                    "poid": po_id,
                    "pid": str(item.get("product_id") or ""),
                    "pname": str(item.get("product_name") or item.get("description") or ""),
                    "route": str(item.get("route") or "STOCK"),
                    "eye": str(item.get("eye_side") or ""),
                    "power": Json(item.get("power_json") or {}),
                    "qty": qty,
                    "price": price,
                    "total": round(qty * price, 2),
                    "batch": str(item.get("batch_no") or ""),
                    "expiry": str(item.get("expiry_date") or ""),
                    "sid": supplier_id or "",
                    "sname": supplier_name or "",
                    "source": source,
                    "meta": Json(item),
                },
            )
            pi = cur.fetchone()
            if upload_id and pi:
                cur.execute(
                    """
                    INSERT INTO supplier_invoice_matches
                        (upload_id, procurement_item_id, order_line_id,
                         match_status, match_score, matched_fields)
                    VALUES
                        (%(uid)s::uuid, %(piid)s::uuid, NULL,
                         'UNMATCHED_PRODUCT_CONFIRMED', %(score)s, %(fields)s)
                    """,
                    {
                        "uid": upload_id,
                        "piid": pi["id"],
                        "score": _num(item.get("match_score"), 0),
                        "fields": Json({"product_id": item.get("product_id"), "description": item.get("description")}),
                    },
                )

        conn.commit()
        return str(po.get("procurement_no") or po_id)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            close_connection(conn)


def is_line_procured(order_line_id: str) -> Dict[str, Any]:
    rows = run_query(
        """
        SELECT
            poi.status, poi.qty_received, poi.batch_no, poi.expiry_date,
            po.document_no, po.document_type, po.procurement_no
        FROM procurement_order_items poi
        LEFT JOIN procurement_orders po ON po.id = poi.procurement_order_id
        WHERE poi.order_line_id = %(lid)s::uuid
        LIMIT 1
        """,
        {"lid": order_line_id},
    )
    if rows:
        r = rows[0]
        status = str(r.get("status") or "").upper()
        r["is_procured"] = status in PROCURED_STATUSES or _num(r.get("qty_received")) > 0
        return r
    return {"is_procured": False}
