"""
modules/billing/db/billing_queries.py
======================================
Billing DB layer — pure SQL access only.
NO streamlit, NO business logic, NO session state.

All functions:
  - Accept typed parameters
  - Return plain dicts / lists
  - Use positional %s params throughout
  - Never raise — return empty / None on error

Import pattern:
    from modules.billing.db.billing_queries import (
        get_open_invoices_for_party,
        get_open_challans_for_party,
        get_party_payments,
        ...
    )
"""

from __future__ import annotations
from typing import List, Dict, Optional
import logging

_log = logging.getLogger(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _q(sql: str, params=None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or ()) or []
    except Exception as e:
        _log.warning(f"[billing_queries._q] {e}")
        return []


def _qp(sql: str, params: tuple = ()) -> List[Dict]:
    """Positional-only params — for ANY(%s) array queries."""
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        _log.warning(f"[billing_queries._qp] {e}")
        return []


def _w(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or ())
        return True
    except Exception as e:
        _log.warning(f"[billing_queries._w] {e}")
        return False


def _tx(steps: list) -> tuple[bool, Optional[str]]:
    try:
        from modules.sql_adapter import run_transaction
        run_transaction(steps)
        return True, None
    except Exception:
        ok, err = True, None
        for sql, params in steps:
            try:
                from modules.sql_adapter import run_write
                run_write(sql, params)
            except Exception as se:
                ok, err = False, str(se)
        return ok, err


# ══════════════════════════════════════════════════════════════════════════════
# PARTY QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_party_by_id(party_id: str) -> Optional[Dict]:
    rows = _q("""
        SELECT id::text, party_name, COALESCE(mobile,'') AS mobile,
               COALESCE(city,'') AS city,
               COALESCE(party_type,'') AS party_type,
               COALESCE(gstin,'') AS gstin,
               COALESCE(credit_limit,0) AS credit_limit,
               COALESCE(billing_category,'ON_COMPLETION') AS billing_category
        FROM parties WHERE id::text = %s LIMIT 1
    """, (party_id,))
    return rows[0] if rows else None


def get_party_name(party_id: str) -> str:
    rows = _q("SELECT party_name FROM parties WHERE id::text = %s LIMIT 1", (party_id,))
    return rows[0]["party_name"] if rows else ""


def resolve_name_for_party_or_patient(party_id: str) -> str:
    """Check patients table first, then parties."""
    nr = _q("SELECT master_name AS n FROM patients WHERE id::text = %s LIMIT 1", (party_id,))
    if nr: return nr[0].get("n", "")
    nr = _q("SELECT party_name AS n FROM parties WHERE id::text = %s LIMIT 1", (party_id,))
    return nr[0].get("n", "") if nr else ""


# ══════════════════════════════════════════════════════════════════════════════
# INVOICE QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_open_invoices_for_party(party_id: str) -> List[Dict]:
    """
    Invoices with balance > 0, derived from payments FK (not stored status).
    """
    return _q("""
        SELECT
            i.id::text              AS id,
            i.invoice_no            AS doc_no,
            'INVOICE'               AS doc_type,
            i.invoice_date          AS doc_date,
            COALESCE(i.grand_total,0) AS grand_total,
            i.challan_id::text      AS challan_id,
            COALESCE((
                SELECT SUM(p.amount) FROM payments p
                WHERE p.invoice_id = i.id AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0) AS amount_paid,
            GREATEST(COALESCE(i.grand_total,0) - COALESCE((
                SELECT SUM(p.amount) FROM payments p
                WHERE p.invoice_id = i.id AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0), 0) AS balance_due,
            CASE
                WHEN GREATEST(COALESCE(i.grand_total,0) - COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.invoice_id = i.id AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0), 0) <= 0.01 THEN 'PAID'
                WHEN COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.invoice_id = i.id AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0) > 0 THEN 'PARTIAL'
                ELSE 'UNPAID'
            END AS payment_status
        FROM invoices i
        WHERE i.party_id::text = %s
          AND COALESCE(i.is_deleted,FALSE) = FALSE
    """, (party_id,))


def get_invoices_by_order_refs(order_refs: List[str]) -> List[Dict]:
    """Find invoices via order_ids array — for patient invoices with NULL party_id."""
    if not order_refs:
        return []
    return _qp("""
        SELECT i.id::text, i.invoice_no AS doc_no, 'INVOICE' AS doc_type,
               i.invoice_date AS doc_date,
               COALESCE(i.grand_total,0) AS grand_total,
               i.challan_id::text AS challan_id,
               COALESCE((
                   SELECT SUM(p.amount) FROM payments p
                   WHERE p.invoice_id = i.id AND NOT COALESCE(p.is_deleted,FALSE)
               ), 0) AS amount_paid,
               GREATEST(COALESCE(i.grand_total,0) - COALESCE((
                   SELECT SUM(p.amount) FROM payments p
                   WHERE p.invoice_id = i.id AND NOT COALESCE(p.is_deleted,FALSE)
               ), 0), 0) AS balance_due,
               COALESCE(i.payment_status,'UNPAID') AS payment_status
        FROM invoices i
        WHERE EXISTS (
            SELECT 1 FROM unnest(i.order_ids) AS oid WHERE oid = ANY(%s)
        )
          AND COALESCE(i.is_deleted,FALSE) = FALSE
    """, (order_refs,))


def update_invoice_balance(invoice_id: str) -> bool:
    """Recalculate invoice balance from direct payments + order advances."""
    return _w("""
        WITH inv AS (
            SELECT id, order_ids, COALESCE(grand_total,0) AS gt
            FROM invoices
            WHERE id = %(id)s::uuid
        ),
        paid AS (
            SELECT
                inv.id,
                COALESCE((
                    SELECT SUM(p.amount)
                    FROM payments p
                    WHERE p.invoice_id = inv.id
                      AND NOT COALESCE(p.is_deleted,FALSE)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM invoices i2
                          JOIN payments cp ON cp.challan_id = i2.challan_id
                          WHERE i2.id = inv.id
                            AND NOT COALESCE(cp.is_deleted,FALSE)
                            AND ABS(COALESCE(cp.amount,0) - COALESCE(p.amount,0)) <= 0.01
                            AND cp.payment_date <= p.payment_date
                      )
                ), 0)
                +
                COALESCE((
                    SELECT SUM(p.amount)
                    FROM payments p
                    JOIN invoices i2 ON i2.id = inv.id
                    WHERE p.challan_id = i2.challan_id
                      AND i2.challan_id IS NOT NULL
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0)
                +
                COALESCE((
                    SELECT SUM(p.amount)
                    FROM payments p
                    WHERE p.advance_for_order_id::text = ANY(inv.order_ids::text[])
                      AND (COALESCE(p.is_advance,FALSE) OR UPPER(COALESCE(p.payment_type,''))='ADVANCE')
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0) AS amt
            FROM inv
        )
        UPDATE invoices SET
            amount_paid = paid.amt,
            balance_due = GREATEST(COALESCE(grand_total,0) - paid.amt, 0),
            status = CASE
                WHEN COALESCE(status,'') IN ('CANCELLED','VOID') THEN status
                WHEN COALESCE(grand_total,0) - paid.amt <= 0.50 THEN 'PAID'
                ELSE 'ACTIVE'
            END,
            payment_status = CASE
                WHEN paid.amt - COALESCE(grand_total,0) > 0.50 THEN 'EXCESS'
                WHEN COALESCE(grand_total,0) - paid.amt <= 0.50 THEN 'PAID'
                WHEN paid.amt > 0 THEN 'PARTIAL'
                ELSE 'UNPAID'
            END,
            updated_at = NOW()
        FROM paid
        WHERE invoices.id = paid.id
    """, {"id": invoice_id})


# ══════════════════════════════════════════════════════════════════════════════
# CHALLAN QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_open_challans_for_party(party_id: str) -> List[Dict]:
    """
    Challans with balance > 0. INVOICED status derived from invoices.challan_id FK.
    """
    return _q("""
        SELECT
            c.id::text                              AS id,
            c.challan_no                            AS doc_no,
            'CHALLAN'                               AS doc_type,
            c.challan_date                          AS doc_date,
            COALESCE(c.grand_total,c.total_amount,0) AS grand_total,
            NULL::text                              AS challan_id,
            COALESCE((
                SELECT SUM(p.amount) FROM payments p
                WHERE p.challan_id = c.id AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0) AS amount_paid,
            GREATEST(COALESCE(c.grand_total,c.total_amount,0) - COALESCE((
                SELECT SUM(p.amount) FROM payments p
                WHERE p.challan_id = c.id AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0), 0) AS balance_due,
            -- INVOICED derived from FK, not stored status
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM invoices inv
                    WHERE inv.challan_id = c.id
                      AND COALESCE(inv.is_deleted,FALSE) = FALSE
                ) THEN 'INVOICED'
                ELSE COALESCE(c.status,'PENDING')
            END AS payment_status
        FROM challans c
        WHERE c.party_id::text = %s
          AND COALESCE(c.is_deleted,FALSE) = FALSE
          AND UPPER(COALESCE(c.status,'PENDING')) NOT IN ('PAID','CANCELLED')
    """, (party_id,))


def update_challan_balance(challan_id: str) -> bool:
    """Recalculate challan balance from direct payments + order advances.

    Challan retail payments can be recorded in two places:
    - payments.challan_id for balance receipts
    - payments.advance_for_order_id for punched advances

    Keep amount_paid as actual received. balance_due is clamped to zero,
    so overpayments remain visible as amount_paid - grand_total instead of
    being hidden as a negative balance.
    """
    return _w("""
        WITH ch AS (
            SELECT id, order_ids, COALESCE(grand_total,total_amount,0) AS gt
            FROM challans
            WHERE id = %(id)s::uuid
        ),
        paid AS (
            SELECT
                ch.id,
                COALESCE((
                    SELECT SUM(p.amount)
                    FROM payments p
                    WHERE p.challan_id = ch.id
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0)
                +
                COALESCE((
                    SELECT SUM(p.amount)
                    FROM payments p
                    WHERE p.advance_for_order_id::text = ANY(ch.order_ids::text[])
                      AND (COALESCE(p.is_advance,FALSE) OR UPPER(COALESCE(p.payment_type,''))='ADVANCE')
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0) AS amt
            FROM ch
        )
        UPDATE challans SET
            amount_paid = paid.amt,
            advance_applied = COALESCE((
                SELECT SUM(p.amount)
                FROM payments p, ch
                WHERE p.advance_for_order_id::text = ANY(ch.order_ids::text[])
                  AND (COALESCE(p.is_advance,FALSE) OR UPPER(COALESCE(p.payment_type,''))='ADVANCE')
                  AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0),
            balance_due = GREATEST(COALESCE(grand_total,total_amount,0) - paid.amt, 0),
            payment_complete = CASE
                WHEN COALESCE(grand_total,total_amount,0) - paid.amt <= 0.50 THEN TRUE
                ELSE FALSE
            END,
            status = CASE
                WHEN EXISTS (
                    SELECT 1 FROM invoices inv
                    WHERE inv.challan_id = %(id)s::uuid
                      AND COALESCE(inv.is_deleted,FALSE) = FALSE
                ) THEN 'INVOICED'
                ELSE status
            END,
            updated_at = NOW()
        FROM paid
        WHERE challans.id = paid.id
    """, {"id": challan_id})


# ══════════════════════════════════════════════════════════════════════════════
# ORDER QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_order_refs_for_party(party_id: str, party_name: str = "") -> tuple[List[str], List[str]]:
    """Return (uuid_list, order_no_list) for all orders of this party/patient."""
    if party_name:
        rows = _q("""
            SELECT id::text, order_no::text FROM orders
            WHERE (party_id::text = %s OR party_name ILIKE %s OR patient_name ILIKE %s)
              AND COALESCE(is_deleted,FALSE) = FALSE LIMIT 200
        """, (party_id, f"%{party_name}%", f"%{party_name}%"))
    else:
        rows = _q("""
            SELECT id::text, order_no::text FROM orders
            WHERE party_id::text = %s AND COALESCE(is_deleted,FALSE) = FALSE LIMIT 200
        """, (party_id,))

    uuids = [r["id"]       for r in rows if r.get("id")]
    nos   = [r["order_no"] for r in rows if r.get("order_no")]
    return uuids, nos


def get_orders_with_balance(party_id: str, party_name: str = "") -> List[Dict]:
    """Open orders with unpaid balance — for On Account display."""
    pn = f"%{party_name}%" if party_name else "__none__"
    return _q("""
        SELECT
            o.id::text              AS id,
            o.order_no              AS doc_no,
            'ON_ACCOUNT'            AS doc_type,
            o.created_at::date      AS doc_date,
            COALESCE(o.total_value,0) AS grand_total,
            NULL::text              AS challan_id,
            COALESCE(SUM(p.amount),0) AS amount_paid,
            GREATEST(COALESCE(o.total_value,0)-COALESCE(SUM(p.amount),0),0) AS balance_due,
            o.status                AS payment_status
        FROM orders o
        LEFT JOIN payments p ON p.advance_for_order_id = o.id
                             AND NOT COALESCE(p.is_deleted,FALSE)
        WHERE (o.party_id::text = %s OR o.party_name ILIKE %s OR o.patient_name ILIKE %s)
          AND COALESCE(o.is_deleted,FALSE) = FALSE
          AND o.order_type IN ('RETAIL','WHOLESALE')
          AND o.status NOT IN ('PAID','CANCELLED','BILLED','CLOSED')
        GROUP BY o.id, o.order_no, o.total_value, o.created_at, o.status
        HAVING COALESCE(o.total_value,0) - COALESCE(SUM(p.amount),0) > 0.50
        ORDER BY o.created_at DESC LIMIT 20
    """, (party_id, pn, pn))


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENT QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_party_payments(party_id: str, limit: int = 50) -> List[Dict]:
    return _q("""
        SELECT id::text, payment_no, payment_date, payment_mode,
               amount, reference_no, remarks, payment_type,
               invoice_id::text, challan_id::text,
               advance_for_order_id::text AS order_id
        FROM payments
        WHERE party_id::text = %s
          AND NOT COALESCE(is_deleted,FALSE)
        ORDER BY payment_date DESC, id DESC
        LIMIT %s
    """, (party_id, limit))


def insert_payment(params: Dict) -> bool:
    from modules.core.date_guard import validate_payment_date
    _ok_dt, _msg_dt = validate_payment_date(
        params.get("dt"),
        payment_type=params.get("payment_type") or "PAYMENT",
        payment_mode=params.get("mode") or params.get("payment_mode") or "",
        method=params.get("method") or params.get("mode") or "",
        remarks=params.get("nar") or params.get("remarks") or "",
        reference_no=params.get("ref") or params.get("reference_no") or "",
        allow_provisional_advance_cheque=bool(params.get("allow_provisional_advance_cheque")),
    )
    if not _ok_dt:
        raise ValueError(_msg_dt)
    return _w("""
        INSERT INTO payments
            (id, payment_no, party_id, party_name,
             invoice_id, challan_id, order_id,
             payment_date, payment_mode, amount,
             reference_no, remarks, payment_type,
             is_advance, advance_for_order_id, created_by)
        VALUES
            (%(id)s, %(pno)s, %(pid)s, %(pn)s,
             %(iid)s, %(cid)s, %(oid)s,
             %(dt)s, %(mode)s, %(amt)s,
             %(ref)s, %(nar)s, 'PAYMENT',
             FALSE, %(oid)s, %(by)s)
    """, params)


# ══════════════════════════════════════════════════════════════════════════════
# PARTY LEDGER
# ══════════════════════════════════════════════════════════════════════════════

def ensure_ledger_table() -> None:
    _w("""CREATE TABLE IF NOT EXISTS party_ledger (
        id          BIGSERIAL PRIMARY KEY,
        party_id    UUID,
        party_name  TEXT,
        entry_date  DATE DEFAULT CURRENT_DATE,
        entry_type  TEXT,
        ref_id      TEXT,
        ref_no      TEXT,
        debit       NUMERIC(14,2) DEFAULT 0,
        credit      NUMERIC(14,2) DEFAULT 0,
        narration   TEXT,
        created_by  TEXT,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    )""")
    _w("ALTER TABLE party_ledger ADD COLUMN IF NOT EXISTS created_by TEXT")
    _w("CREATE INDEX IF NOT EXISTS idx_pldgr_party ON party_ledger(party_id) WHERE party_id IS NOT NULL")
    _w("CREATE INDEX IF NOT EXISTS idx_pldgr_name  ON party_ledger(party_name)")
    _w("CREATE INDEX IF NOT EXISTS idx_pldgr_date  ON party_ledger(entry_date DESC)")


def insert_ledger_credit(params: Dict) -> bool:
    return _w("""
        INSERT INTO party_ledger
            (party_id, party_name, entry_date, entry_type,
             ref_id, ref_no, credit, narration, created_by)
        VALUES
            (%(pid)s, %(pn)s, %(dt)s, %(et)s,
             %(rid)s, %(rno)s, %(amt)s, %(nar)s, %(by)s)
    """, params)


def insert_ledger_debit(
    party_id: str, party_name: str,
    doc_type: str, doc_id: str, doc_no: str,
    amount: float, doc_date, narration: str, created_by: str = "System",
) -> None:
    _w("""
        INSERT INTO party_ledger
            (party_id, party_name, entry_date, entry_type,
             ref_id, ref_no, debit, credit, narration, created_by)
        VALUES
            (%(pid)s, %(pn)s, %(dt)s, %(et)s,
             %(rid)s, %(rno)s, %(deb)s, 0, %(nar)s, %(by)s)
        ON CONFLICT DO NOTHING
    """, {
        "pid": party_id or None, "pn": party_name or "",
        "dt": doc_date, "et": doc_type,
        "rid": doc_id, "rno": doc_no,
        "deb": round(float(amount or 0), 2),
        "nar": narration or f"{doc_type} — {doc_no}",
        "by": created_by,
    })


def get_party_ledger(party_id: str, party_name: str = "", limit: int = 120) -> List[Dict]:
    return _q("""
        SELECT entry_date::text AS entry_date, entry_type, ref_no,
               COALESCE(debit,0) AS debit, COALESCE(credit,0) AS credit,
               COALESCE(narration,'') AS narration,
               COALESCE(created_by,'') AS created_by
        FROM party_ledger
        WHERE party_id::text = %s
           OR (party_id IS NULL AND party_name ILIKE %s)
        ORDER BY entry_date ASC, id ASC LIMIT %s
    """, (party_id, f"%{party_name}%" if party_name else "__none__", limit))
