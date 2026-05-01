"""
logistics_manager.py
====================
Logistics layer for the order workflow train.

RESPONSIBILITY:
  - Carrier / route master (stub — UI to be designed later)
  - Dispatch event ledger (order_dispatches table)
  - Partial-dispatch tracking: which lines shipped, what qty remains
  - Delivery confirmation
  - Fetch dispatch history for an order

RULES:
  - No dispatch record can be created without a billing document
    (challan OR invoice) already existing for the order.
  - Partial dispatch keeps order in DISPATCHED state; remaining
    lines stay billed-but-pending until next dispatch event.
  - Full dispatch → order progresses to DELIVERED when confirmed.

DB TABLES USED:
  order_dispatches        — one row per dispatch event
  order_dispatch_lines    — per-line qty breakdown per event
  (challans / invoices    — read-only gate check here)

USAGE:
  from modules.backoffice.logistics_manager import (
      billing_gate_check,
      get_dispatch_history,
      create_dispatch_event,
      LogisticsRoute,
  )
"""

import uuid
import datetime
from typing import Dict, List, Optional, Tuple
import streamlit as st


# ──────────────────────────────────────────────────────────────
# CARRIER / ROUTE DEFINITIONS  (stub — design UI later)
# ──────────────────────────────────────────────────────────────

class LogisticsRoute:
    """
    Stub registry for logistics routes.
    Each route has a code, display name, and carrier type.
    Full UI configuration to be added in a future window.
    """

    _ROUTES: List[Dict] = [
        # code            label                  type          track_url_tpl
        {"code": "COURIER_LOCAL",   "label": "Local Courier",        "type": "COURIER",  "track_url": ""},
        {"code": "COURIER_DTDC",    "label": "DTDC",                 "type": "COURIER",  "track_url": "https://www.dtdc.in/trace.asp?Txnno={tracking_no}"},
        {"code": "COURIER_DELHIVERY", "label": "Delhivery",          "type": "COURIER",  "track_url": "https://www.delhivery.com/track/package/{tracking_no}"},
        {"code": "COURIER_BLUEDART","label": "Bluedart",             "type": "COURIER",  "track_url": "https://www.bluedart.com/tracking/{tracking_no}"},
        {"code": "COURIER_SPEEDPOST","label": "India Speed Post",    "type": "POSTAL",   "track_url": "https://www.indiapost.gov.in/"},
        {"code": "HAND_DELIVERY",   "label": "Hand Delivery",        "type": "MANUAL",   "track_url": ""},
        {"code": "SALES_REP",       "label": "Sales Representative", "type": "MANUAL",   "track_url": ""},
        {"code": "BUS_PARCEL",      "label": "Bus Parcel / ST",      "type": "TRANSPORT","track_url": ""},
        {"code": "SELF_COLLECT",    "label": "Self Collection",      "type": "MANUAL",   "track_url": ""},
        # ROUTE_DESIGN_PLACEHOLDER — more routes will be added via UI
    ]

    @classmethod
    def all_routes(cls) -> List[Dict]:
        return cls._ROUTES

    @classmethod
    def get(cls, code: str) -> Optional[Dict]:
        return next((r for r in cls._ROUTES if r["code"] == code), None)

    @classmethod
    def labels(cls) -> List[str]:
        return [r["label"] for r in cls._ROUTES]

    @classmethod
    def code_for_label(cls, label: str) -> str:
        r = next((r for r in cls._ROUTES if r["label"] == label), None)
        return r["code"] if r else "COURIER_LOCAL"

    @classmethod
    def tracking_url(cls, code: str, tracking_no: str) -> str:
        r = cls.get(code)
        if r and r.get("track_url") and tracking_no:
            return r["track_url"].replace("{tracking_no}", tracking_no)
        return ""


# ──────────────────────────────────────────────────────────────
# DB HELPERS
# ──────────────────────────────────────────────────────────────

def _q(sql: str, params: dict = None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        return []


def _w(sql: str, params: dict = None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        st.error(f"DB write error: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# BILLING GATE
# ──────────────────────────────────────────────────────────────

def billing_gate_check(order_id: str) -> Tuple[bool, str, List[Dict]]:
    """
    Returns (is_billed, message, billing_docs)
    Checks challans + invoices. Dispatch is only allowed if at least
    one active billing document exists for this order.
    """
    if not order_id:
        return False, "Order ID missing", []

    docs = []
    try:
        challans = _q("""
            SELECT 'CHALLAN' AS doc_type, challan_no AS doc_no,
                   grand_total AS amount, status, created_at
            FROM challans
            WHERE order_ids::text[] @> ARRAY[%(oid)s::text]
              AND status NOT IN ('CANCELLED','VOID')
            ORDER BY created_at DESC
        """, {"oid": str(order_id)})
        docs.extend(challans or [])

        invoices = _q("""
            SELECT 'INVOICE' AS doc_type, invoice_no AS doc_no,
                   grand_total AS amount, status, created_at
            FROM invoices
            WHERE order_ids::text[] @> ARRAY[%(oid)s::text]
              AND status NOT IN ('CANCELLED','VOID')
            ORDER BY created_at DESC
        """, {"oid": str(order_id)})
        docs.extend(invoices or [])

    except Exception:
        return False, "Cannot verify billing — DB error", []

    if docs:
        doc_labels = ", ".join(f"{d['doc_type']} {d['doc_no']}" for d in docs)
        return True, f"Billing confirmed: {doc_labels}", docs
    else:
        return False, "No challan or invoice found — billing must be completed before dispatch.", []


# ──────────────────────────────────────────────────────────────
# DISPATCH HISTORY
# ──────────────────────────────────────────────────────────────

def get_dispatch_history(order_id: str) -> List[Dict]:
    """
    Fetch all dispatch events for this order, newest first.
    Each event includes its line breakdown.
    """
    events = _q("""
        SELECT d.id, d.dispatch_no, d.dispatch_type, d.route_code,
               d.carrier_name, d.tracking_no, d.dispatched_at,
               d.dispatched_by, d.remarks, d.is_partial,
               d.billing_doc_ref, d.status
        FROM order_dispatches d
        WHERE d.order_id = %(oid)s::uuid
        ORDER BY d.dispatched_at DESC
    """, {"oid": order_id})

    result = []
    for ev in (events or []):
        ev = dict(ev)
        ev["lines"] = _q("""
            SELECT dl.order_line_id, dl.product_name, dl.eye_side,
                   dl.dispatched_qty, dl.remaining_qty, dl.sph, dl.brand
            FROM order_dispatch_lines dl
            WHERE dl.dispatch_id = %(did)s::uuid
            ORDER BY dl.eye_side
        """, {"did": str(ev["id"])}) or []
        result.append(ev)
    return result


def get_billed_lines_for_order(order_id: str) -> List[Dict]:
    """
    Returns order lines with billing quantities and how much has
    already been dispatched — used to build the partial dispatch form.
    """
    lines = _q("""
        SELECT ol.id, ol.product_name, ol.brand, ol.eye_side,
               ol.billing_qty, ol.unit_price, ol.gst_percent,
               ol.sph, ol.cyl, ol.axis,
               COALESCE(
                   (SELECT SUM(dl.dispatched_qty)
                    FROM order_dispatch_lines dl
                    JOIN order_dispatches d ON d.id = dl.dispatch_id
                    WHERE dl.order_line_id = ol.id
                      AND d.status != 'CANCELLED'),
                   0
               ) AS already_dispatched
        FROM order_lines ol
        WHERE ol.order_id = %(oid)s::uuid
        ORDER BY ol.eye_side, ol.id
    """, {"oid": order_id})

    result = []
    for l in (lines or []):
        l = dict(l)
        l["billing_qty"]        = int(l.get("billing_qty") or 0)
        l["already_dispatched"] = int(l.get("already_dispatched") or 0)
        l["remaining_qty"]      = max(0, l["billing_qty"] - l["already_dispatched"])
        result.append(l)
    return result


def get_dispatch_summary(order_id: str) -> Dict:
    """
    Returns {total_billed, total_dispatched, total_remaining, is_fully_dispatched}
    """
    lines = get_billed_lines_for_order(order_id)
    total_billed     = sum(l["billing_qty"] for l in lines)
    total_dispatched = sum(l["already_dispatched"] for l in lines)
    total_remaining  = sum(l["remaining_qty"] for l in lines)
    return {
        "total_billed":        total_billed,
        "total_dispatched":    total_dispatched,
        "total_remaining":     total_remaining,
        "is_fully_dispatched": total_remaining == 0 and total_billed > 0,
        "is_partial":          0 < total_dispatched < total_billed,
        "line_count":          len(lines),
    }


# ──────────────────────────────────────────────────────────────
# CREATE DISPATCH EVENT
# ──────────────────────────────────────────────────────────────

def create_dispatch_event(
    order_id: str,
    order_no: str,
    route_code: str,
    carrier_name: str,
    tracking_no: str,
    dispatched_by: str,
    dispatch_date: datetime.date,
    line_qtys: Dict[str, int],      # {order_line_id: dispatched_qty}
    billing_doc_ref: str = "",
    remarks: str = "",
) -> Tuple[bool, str]:
    """
    Creates one dispatch event with per-line quantities.

    Returns (success, message)
    """
    if not line_qtys:
        return False, "No line quantities provided"

    total_dispatching = sum(line_qtys.values())
    if total_dispatching <= 0:
        return False, "Dispatch quantity must be > 0"

    # Generate dispatch number
    dispatch_id = str(uuid.uuid4())
    try:
        seq_rows = _q("SELECT COALESCE(MAX(dispatch_seq), 0) + 1 AS nxt FROM order_dispatches")
        seq = int((seq_rows[0].get("nxt") or 1)) if seq_rows else 1
    except Exception:
        seq = 1
    dispatch_no = f"DISP/{datetime.date.today().year}/{seq:05d}"

    # Determine if this is a partial event
    billed_lines = get_billed_lines_for_order(order_id)
    billed_map   = {str(l["id"]): l for l in billed_lines}

    total_billed     = sum(l["billing_qty"] for l in billed_lines)
    total_already    = sum(l["already_dispatched"] for l in billed_lines)
    total_after_this = total_already + total_dispatching
    is_partial       = total_after_this < total_billed

    try:
        operator = dispatched_by or st.session_state.get("user_name", "system")
    except Exception:
        operator = "system"

    # Insert dispatch master
    ok = _w("""
        INSERT INTO order_dispatches (
            id, order_id, dispatch_no, dispatch_type, route_code,
            carrier_name, tracking_no, dispatched_at, dispatched_by,
            remarks, is_partial, billing_doc_ref, status,
            dispatch_seq, created_at
        ) VALUES (
            %(id)s::uuid, %(order_id)s::uuid, %(dispatch_no)s,
            %(dispatch_type)s, %(route_code)s,
            %(carrier_name)s, %(tracking_no)s,
            %(dispatched_at)s, %(dispatched_by)s,
            %(remarks)s, %(is_partial)s, %(billing_doc_ref)s,
            'DISPATCHED', %(seq)s, NOW()
        )
    """, {
        "id":             dispatch_id,
        "order_id":       order_id,
        "dispatch_no":    dispatch_no,
        "dispatch_type":  "PARTIAL" if is_partial else "FULL",
        "route_code":     route_code,
        "carrier_name":   carrier_name,
        "tracking_no":    tracking_no,
        "dispatched_at":  dispatch_date.isoformat(),
        "dispatched_by":  operator,
        "remarks":        remarks,
        "is_partial":     is_partial,
        "billing_doc_ref": billing_doc_ref,
        "seq":            seq,
    })
    if not ok:
        return False, "Failed to create dispatch record"

    # Insert per-line quantities
    for line_id, qty in line_qtys.items():
        if qty <= 0:
            continue
        line = billed_map.get(str(line_id), {})
        remaining_after = max(0, line.get("remaining_qty", 0) - qty)
        _w("""
            INSERT INTO order_dispatch_lines (
                id, dispatch_id, order_line_id,
                product_name, brand, eye_side,
                dispatched_qty, remaining_qty, sph
            ) VALUES (
                gen_random_uuid(), %(did)s::uuid, %(lid)s::uuid,
                %(pname)s, %(brand)s, %(eye)s,
                %(dqty)s, %(rqty)s, %(sph)s
            )
        """, {
            "did":   dispatch_id,
            "lid":   line_id,
            "pname": line.get("product_name", ""),
            "brand": line.get("brand", ""),
            "eye":   line.get("eye_side", ""),
            "dqty":  qty,
            "rqty":  remaining_after,
            "sph":   str(line.get("sph") or ""),
        })

    # ── REAL STOCK DEDUCTION (quantity ↓ + allocated_qty ↓) ─────────────
    # Per stock flow doc: Dispatch is the ONLY point where physical stock leaves.
    # quantity       ↓  — real stock deducted
    # allocated_qty  ↓  — soft reservation released (was held since order save)
    # Uses idempotency: dispatched_qty column on order_lines prevents double deduction.
    try:
        from modules.sql_adapter import run_query as _rq_disp, run_write as _rw_disp

        for line_id, qty in line_qtys.items():
            if qty <= 0:
                continue

            # Atomic gate: mark dispatched_qty on the line first (idempotency)
            # Only deduct if the RETURNING row is returned (first dispatch only)
            _claimed = _rq_disp("""
                UPDATE order_lines
                SET dispatched_qty = COALESCE(dispatched_qty, 0) + %(qty)s,
                    updated_at     = NOW()
                WHERE id          = %(lid)s::uuid
                  AND COALESCE(dispatched_qty, 0) + %(qty)s
                      <= COALESCE(billing_qty, quantity, 0)
                RETURNING id, product_id::text, lens_params
            """, {"lid": line_id, "qty": qty}) or []

            if not _claimed:
                continue  # Already dispatched this qty or exceeds billed qty

            _row    = _claimed[0]
            _pid    = str(_row.get("product_id") or "")
            _lp     = _row.get("lens_params") or {}
            if isinstance(_lp, str):
                import json as _jlm
                try: _lp = _jlm.loads(_lp)
                except: _lp = {}
            _bno    = str(_lp.get("batch_no") or "")

            if not _pid:
                continue

            # Deduct real quantity + release soft reservation atomically
            _rw_disp("""
                UPDATE inventory_stock
                SET quantity      = GREATEST(0, COALESCE(quantity, 0)      - %(qty)s),
                    allocated_qty = GREATEST(0, COALESCE(allocated_qty, 0) - %(qty)s),
                    is_active     = CASE
                                        WHEN GREATEST(0, COALESCE(quantity,0) - %(qty)s) <= 0
                                        THEN FALSE ELSE TRUE
                                    END,
                    updated_at    = NOW()
                WHERE product_id = %(pid)s::uuid
                  AND (%(bno)s = '' OR batch_no = %(bno)s)
                LIMIT 1
            """, {"pid": _pid, "bno": _bno, "qty": qty})

    except Exception as _disp_stk_err:
        import logging as _dlog
        _dlog.warning(f"[Dispatch] Stock deduction failed (non-fatal): {_disp_stk_err}")
        # Non-fatal: dispatch is recorded, stock drift caught by reconciliation

    # Write order_status_history entry
    _w("""
        INSERT INTO order_status_history (
            history_id, order_id, from_status, to_status,
            changed_at, changed_by_name, remarks
        )
        SELECT gen_random_uuid()::uuid, id,
               status, 'DISPATCHED',
               NOW(), %(by)s, %(rmk)s
        FROM orders WHERE id = %(oid)s::uuid
    """, {
        "by":  operator,
        "rmk": f"Dispatch {dispatch_no} — {carrier_name or route_code} — {tracking_no or 'no tracking'}",
        "oid": order_id,
    })

    # Update order status
    new_status = "DISPATCHED"  # Stays DISPATCHED for partial too; DELIVERED on confirm
    _w("""
        UPDATE orders SET status = %(st)s, updated_at = NOW()
        WHERE id = %(oid)s::uuid
    """, {"st": new_status, "oid": order_id})

    # Update session state cache
    try:
        for o in st.session_state.get("bo_active_orders", []):
            if str(o.get("id")) == str(order_id):
                o["status"] = new_status
                break
    except Exception:
        pass

    msg = (
        f"✅ {'Partial dispatch' if is_partial else 'Full dispatch'} saved: "
        f"{dispatch_no} · {total_dispatching} unit(s) via {carrier_name or route_code}"
    )
    return True, msg


def confirm_delivery(
    order_id: str,
    dispatch_id: str,
    delivery_date: datetime.date,
    confirmed_by: str,
    notes: str = "",
) -> Tuple[bool, str]:
    """
    Mark a specific dispatch event as delivered.
    If all lines are now fully dispatched+delivered, advance order to DELIVERED.
    """
    ok = _w("""
        UPDATE order_dispatches
        SET status = 'DELIVERED',
            delivered_at = %(dt)s,
            delivered_by = %(by)s,
            delivery_notes = %(notes)s,
            updated_at = NOW()
        WHERE id = %(did)s::uuid AND order_id = %(oid)s::uuid
    """, {
        "dt":    delivery_date.isoformat(),
        "by":    confirmed_by,
        "notes": notes,
        "did":   dispatch_id,
        "oid":   order_id,
    })
    if not ok:
        return False, "Failed to update dispatch"

    summary = get_dispatch_summary(order_id)
    if summary["is_fully_dispatched"]:
        _w("""
            UPDATE orders SET status = 'DELIVERED', updated_at = NOW()
            WHERE id = %(oid)s::uuid
        """, {"oid": order_id})
        _w("""
            INSERT INTO order_status_history (
                history_id, order_id, from_status, to_status,
                changed_at, changed_by_name, remarks
            )
            SELECT gen_random_uuid(), id, 'DISPATCHED', 'DELIVERED',
                   NOW(), %(by)s, %(notes)s
            FROM orders WHERE id = %(oid)s::uuid
        """, {"by": confirmed_by, "notes": notes or "All lines delivered", "oid": order_id})
        try:
            for o in st.session_state.get("bo_active_orders", []):
                if str(o.get("id")) == str(order_id):
                    o["status"] = "DELIVERED"
                    break
        except Exception:
            pass
        return True, "✅ All lines delivered — order marked DELIVERED"

    return True, "✅ Delivery confirmed for this dispatch"


# ──────────────────────────────────────────────────────────────
# SQL DDL  (run once to add tables if not present)
# ──────────────────────────────────────────────────────────────

LOGISTICS_DDL = """
-- Dispatch event master
CREATE TABLE IF NOT EXISTS order_dispatches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id        UUID NOT NULL REFERENCES orders(id),
    dispatch_no     VARCHAR(50) UNIQUE NOT NULL,
    dispatch_seq    INTEGER DEFAULT 1,
    dispatch_type   VARCHAR(20) DEFAULT 'FULL',   -- FULL / PARTIAL
    route_code      VARCHAR(50),
    carrier_name    VARCHAR(100),
    tracking_no     VARCHAR(150),
    dispatched_at   DATE NOT NULL DEFAULT CURRENT_DATE,
    dispatched_by   VARCHAR(100),
    delivered_at    DATE,
    delivered_by    VARCHAR(100),
    delivery_notes  TEXT,
    remarks         TEXT,
    is_partial      BOOLEAN DEFAULT FALSE,
    billing_doc_ref VARCHAR(100),
    status          VARCHAR(20) DEFAULT 'DISPATCHED',  -- DISPATCHED / DELIVERED / CANCELLED
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-line dispatch quantities
CREATE TABLE IF NOT EXISTS order_dispatch_lines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_id     UUID NOT NULL REFERENCES order_dispatches(id) ON DELETE CASCADE,
    order_line_id   UUID NOT NULL REFERENCES order_lines(id),
    product_name    VARCHAR(200),
    brand           VARCHAR(100),
    eye_side        VARCHAR(10),
    sph             VARCHAR(20),
    dispatched_qty  INTEGER NOT NULL DEFAULT 0,
    remaining_qty   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_od_order    ON order_dispatches(order_id);
CREATE INDEX IF NOT EXISTS idx_od_status   ON order_dispatches(status);
CREATE INDEX IF NOT EXISTS idx_odl_dispatch ON order_dispatch_lines(dispatch_id);
CREATE INDEX IF NOT EXISTS idx_odl_line     ON order_dispatch_lines(order_line_id);
"""
