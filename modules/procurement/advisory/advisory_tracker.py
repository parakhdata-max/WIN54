"""
procurement/advisory/advisory_tracker.py
=========================================
Advisory PO Tracker — query, filter, snooze.

WHAT THIS OWNS
--------------
  - Load open/closed advisory POs with optional filters
  - Status transitions for advisory POs
  - Snooze a product alert (sets advisory_snoozed_until)
  - Summary counts for dashboard header

WHAT IT DOES NOT OWN
---------------------
  - PO creation  → po_engine.py
  - Alert computation → advisory_service.py
  - Any st.* calls → advisory_panel.py
"""

import logging
from datetime import date, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


def get_advisory_pos(
    supplier: Optional[str] = None,
    status:   Optional[str] = None,
    start:    Optional[date] = None,
    end:      Optional[date] = None,
    limit:    int = 200,
) -> List[Dict]:
    """
    Fetch advisory POs with optional filters.

    Args:
        supplier : party_name partial match (case-insensitive)
        status   : exact status ("Draft", "Sent", "Confirmed", "Received")
        start    : created_at >= start
        end      : created_at <= end
        limit    : max rows

    Returns:
        List of PO dicts ordered by created_at DESC.
    """
    try:
        from modules.sql_adapter import run_query

        query = """
            SELECT
                so.id::text          AS po_id,
                so.po_number,
                so.created_at,
                so.status,
                so.expected_delivery,
                so.notes,
                p.party_name         AS supplier,
                COUNT(soi.id)        AS item_count,
                SUM(soi.quantity)    AS total_qty
            FROM supplier_orders so
            JOIN  parties p ON p.id = so.supplier_id
            LEFT JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
            WHERE so.source = 'ADVISORY'
        """
        params: Dict = {}

        if supplier:
            query += " AND LOWER(p.party_name) LIKE %(supplier)s"
            params["supplier"] = f"%{supplier.lower()}%"

        if status:
            query += " AND so.status = %(status)s"
            params["status"] = status

        if start:
            query += " AND so.created_at >= %(start)s"
            params["start"] = start

        if end:
            query += " AND so.created_at <= %(end)s"
            params["end"] = end

        query += """
            GROUP BY so.id, so.po_number, so.created_at,
                     so.status, so.expected_delivery, so.notes, p.party_name
            ORDER BY so.created_at DESC
            LIMIT %(limit)s
        """
        params["limit"] = limit

        return run_query(query, params) or []

    except Exception as e:
        log.warning(f"[AdvisoryTracker] get_advisory_pos failed: {e}")
        return []


def get_po_line_items(po_id: str) -> List[Dict]:
    """Fetch line items for a specific advisory PO."""
    try:
        from modules.sql_adapter import run_query
        return run_query("""
            SELECT soi.id::text, soi.product_id::text,
                   soi.product_name, soi.quantity, soi.received_qty,
                   p.brand, p.main_group
            FROM supplier_order_items soi
            LEFT JOIN products p ON p.product_id = soi.product_id
            WHERE soi.supplier_order_id = %(po_id)s
            ORDER BY soi.product_name
        """, {"po_id": po_id}) or []
    except Exception as e:
        log.warning(f"[AdvisoryTracker] get_po_line_items failed: {e}")
        return []


def update_po_status(po_id: str, new_status: str, notes: str = "") -> Dict:
    """
    Update status of an advisory PO.

    Valid: Draft → Sent → Confirmed → Received  |  Any → Cancelled
    """
    VALID = {"Draft", "Sent", "Confirmed", "Received", "Cancelled"}
    if new_status not in VALID:
        return {"success": False, "error": f"Invalid status: {new_status}"}
    try:
        from modules.sql_adapter import run_query
        run_query("""
            UPDATE supplier_orders
            SET status     = %(status)s,
                notes      = CASE WHEN %(notes)s != '' THEN %(notes)s ELSE notes END,
                updated_at = NOW()
            WHERE id::text = %(po_id)s AND source = 'ADVISORY'
        """, {"status": new_status, "notes": notes, "po_id": po_id})
        return {"success": True, "message": f"PO {po_id} → {new_status}"}
    except Exception as e:
        log.error(f"[AdvisoryTracker] update_po_status failed: {e}")
        return {"success": False, "error": str(e)}


def snooze_product_alert(product_id: str, days: int = 7) -> Dict:
    """
    Snooze the advisory alert for a product for N days.
    Sets products.advisory_snoozed_until — build_smart_alerts_v2 respects this.
    """
    snooze_until = date.today() + timedelta(days=days)
    try:
        from modules.sql_adapter import run_query
        run_query("""
            UPDATE products
            SET advisory_snoozed_until = %(until)s
            WHERE product_id::text = %(pid)s
        """, {"until": snooze_until, "pid": str(product_id)})
        return {
            "success": True,
            "message": f"Alert snoozed until {snooze_until.strftime('%d %b %Y')}",
        }
    except Exception as e:
        log.error(f"[AdvisoryTracker] snooze_product_alert failed: {e}")
        return {"success": False, "error": str(e)}


def get_advisory_summary() -> Dict:
    """
    Quick summary counts for the advisory dashboard header.
    Returns: open_pos, draft_pos, snoozed_products
    """
    try:
        from modules.sql_adapter import run_query
        today = date.today()

        pos = run_query("""
            SELECT
                COUNT(*) FILTER (WHERE status NOT IN ('Received','Cancelled')) AS open_pos,
                COUNT(*) FILTER (WHERE status = 'Draft')                       AS draft_pos
            FROM supplier_orders WHERE source = 'ADVISORY'
        """) or [{}]

        snoozed = run_query("""
            SELECT COUNT(*) AS cnt FROM products
            WHERE advisory_snoozed_until >= %(today)s
        """, {"today": today}) or [{}]

        return {
            "open_pos":         int((pos[0] or {}).get("open_pos", 0)),
            "draft_pos":        int((pos[0] or {}).get("draft_pos", 0)),
            "snoozed_products": int((snoozed[0] or {}).get("cnt", 0)),
        }
    except Exception as e:
        log.warning(f"[AdvisoryTracker] summary failed: {e}")
        return {"open_pos": 0, "draft_pos": 0, "snoozed_products": 0}
