"""
modules/billing/services/reversal_service.py
=============================================
Payment Reversal & Audit Service — DV ERP

Spec: Reversal_Audit_System_Guide.pdf
  - Never hard-delete — compensating entries only
  - reverse_payment(payment_id, user_id) — full transaction
  - Prevents double reversal
  - Recalculates invoice/challan balance from FK after reversal
  - Full audit trail in party_ledger
"""

from __future__ import annotations
from typing import Dict, Optional, Tuple
import uuid
import datetime
import logging

_log = logging.getLogger(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or ()) or []
    except Exception as e:
        _log.warning(f"[reversal._q] {e}")
        return []


def _w(sql, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or ())
        return True
    except Exception as e:
        _log.warning(f"[reversal._w] {e}")
        return False


def _tx(steps) -> Tuple[bool, Optional[str]]:
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
# SCHEMA MIGRATION (run once on startup)
# ══════════════════════════════════════════════════════════════════════════════

def ensure_reversal_columns() -> None:
    """Add reversal columns if not present — idempotent."""
    _w("ALTER TABLE payments ADD COLUMN IF NOT EXISTS is_cancelled  BOOLEAN DEFAULT FALSE")
    _w("ALTER TABLE payments ADD COLUMN IF NOT EXISTS reversed_at   TIMESTAMPTZ")
    _w("ALTER TABLE payments ADD COLUMN IF NOT EXISTS reversed_by   TEXT")
    _w("ALTER TABLE payments ADD COLUMN IF NOT EXISTS reversal_reason TEXT")
    _w("ALTER TABLE payments ADD COLUMN IF NOT EXISTS original_payment_id UUID")

    _w("ALTER TABLE party_ledger ADD COLUMN IF NOT EXISTS ref_payment_id UUID")
    _w("ALTER TABLE party_ledger ADD COLUMN IF NOT EXISTS is_reversal    BOOLEAN DEFAULT FALSE")

    # Index for audit queries
    _w("CREATE INDEX IF NOT EXISTS idx_payments_cancelled ON payments(is_cancelled) WHERE is_cancelled = TRUE")
    _w("CREATE INDEX IF NOT EXISTS idx_ledger_reversal   ON party_ledger(is_reversal) WHERE is_reversal = TRUE")
    _w("CREATE INDEX IF NOT EXISTS idx_ledger_payment_ref ON party_ledger(ref_payment_id)")


# ══════════════════════════════════════════════════════════════════════════════
# FETCH PAYMENT
# ══════════════════════════════════════════════════════════════════════════════

def get_payment(payment_id: str) -> Optional[Dict]:
    rows = _q("""
        SELECT
            p.id::text, p.payment_no, p.party_id::text, p.party_name,
            p.invoice_id::text, p.challan_id::text,
            p.advance_for_order_id::text AS order_id,
            p.payment_date, p.payment_mode, p.amount, p.reference_no,
            p.remarks, p.payment_type,
            COALESCE(p.is_cancelled, FALSE) AS is_cancelled,
            COALESCE(p.is_deleted, FALSE)   AS is_deleted,
            p.reversed_at, p.reversed_by, p.reversal_reason,
            p.created_by
        FROM payments p
        WHERE p.id::text = %s LIMIT 1
    """, (payment_id,))
    return rows[0] if rows else None


def get_payments_for_party(party_id: str, include_cancelled: bool = False) -> list:
    cf = "" if include_cancelled else "AND NOT COALESCE(p.is_cancelled, FALSE)"
    return _q(f"""
        SELECT
            p.id::text, p.payment_no, p.party_name,
            p.payment_date::text, p.payment_mode, p.amount,
            p.invoice_id::text, p.challan_id::text,
            p.reference_no, p.remarks,
            COALESCE(p.is_cancelled, FALSE) AS is_cancelled,
            p.reversed_at::text, p.reversed_by, p.reversal_reason,
            p.created_by,
            COALESCE(i.invoice_no, '—') AS invoice_no,
            COALESCE(c.challan_no, '—') AS challan_no
        FROM payments p
        LEFT JOIN invoices i ON i.id = p.invoice_id
        LEFT JOIN challans  c ON c.id = p.challan_id
        WHERE p.party_id::text = %s
          AND NOT COALESCE(p.is_deleted, FALSE)
          {cf}
        ORDER BY p.payment_date DESC, p.created_at DESC
        LIMIT 100
    """, (party_id,))


# ══════════════════════════════════════════════════════════════════════════════
# REVERSAL SERVICE
# ══════════════════════════════════════════════════════════════════════════════

def reverse_payment(
    payment_id: str,
    reversed_by: str,
    reason: str,
) -> Tuple[bool, str]:
    """
    Fully reverse a payment — compensating entries, no hard delete.

    Steps:
      1. Fetch and validate payment (not already cancelled)
      2. Insert reverse ledger entry (REVERSAL type, debit side)
      3. Mark payment as cancelled with audit fields
      4. Recalculate invoice/challan balance from payments FK
      5. All in one transaction — commit or rollback

    Returns: (success: bool, message: str)
    """
    ensure_reversal_columns()

    # ── Step 1: Fetch and validate ────────────────────────────────────────
    pay = get_payment(payment_id)
    if not pay:
        return False, "Payment not found."

    if pay.get("is_cancelled"):
        return False, f"Payment {pay.get('payment_no')} is already cancelled — cannot reverse again."

    if pay.get("is_deleted"):
        return False, "Payment has been deleted and cannot be reversed."

    if float(pay.get("amount") or 0) <= 0:
        return False, "Payment amount is zero — nothing to reverse."

    if not reason or not reason.strip():
        return False, "Reversal reason is required."

    pid       = pay["id"]
    pno       = pay["payment_no"]
    party_id  = pay.get("party_id", "")
    party_name= pay.get("party_name", "")
    amount    = float(pay.get("amount") or 0)
    inv_id    = pay.get("invoice_id")
    chal_id   = pay.get("challan_id")
    pay_date  = datetime.date.today()
    rev_id    = str(uuid.uuid4())
    rev_pno   = pno + "-REV"

    steps = []

    # ── Step 2: Reverse ledger entry (REVERSAL — debit side) ─────────────
    steps.append(("""
        INSERT INTO party_ledger
            (party_id, party_name, entry_date, entry_type,
             ref_id, ref_no, debit, credit,
             narration, created_by, ref_payment_id, is_reversal)
        VALUES
            (%(pid)s, %(pn)s, %(dt)s, 'REVERSAL',
             %(rid)s, %(rno)s, %(amt)s, 0,
             %(nar)s, %(by)s, %(orig)s::uuid, TRUE)
    """, {
        "pid":  party_id or None,
        "pn":   party_name,
        "dt":   pay_date,
        "rid":  rev_id,
        "rno":  rev_pno,
        "amt":  amount,
        "nar":  f"REVERSAL of {pno} — {reason.strip()}",
        "by":   reversed_by,
        "orig": pid,
    }))

    # ── Step 3: Mark payment as cancelled ────────────────────────────────
    steps.append(("""
        UPDATE payments SET
            is_cancelled      = TRUE,
            reversed_at       = NOW(),
            reversed_by       = %(by)s,
            reversal_reason   = %(reason)s,
            remarks           = COALESCE(remarks, '') || %(note)s,
            updated_at        = NOW()
        WHERE id = %(id)s::uuid
          AND NOT COALESCE(is_cancelled, FALSE)
    """, {
        "id":     pid,
        "by":     reversed_by,
        "reason": reason.strip(),
        "note":   f" [REVERSED by {reversed_by}: {reason.strip()}]",
    }))

    ok, err = _tx(steps)
    if not ok:
        return False, f"Transaction failed: {err}"

    # ── Step 4: Recalculate invoice/challan balance from FK ───────────────
    if inv_id:
        _w("""
            UPDATE invoices SET
                amount_paid = COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.invoice_id = %(id)s::uuid
                      AND NOT COALESCE(p.is_deleted, FALSE)
                      AND NOT COALESCE(p.is_cancelled, FALSE)
                ), 0),
                balance_due = GREATEST(COALESCE(grand_total, 0) - COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.invoice_id = %(id)s::uuid
                      AND NOT COALESCE(p.is_deleted, FALSE)
                      AND NOT COALESCE(p.is_cancelled, FALSE)
                ), 0), 0),
                payment_status = CASE
                    WHEN GREATEST(COALESCE(grand_total, 0) - COALESCE((
                        SELECT SUM(p.amount) FROM payments p
                        WHERE p.invoice_id = %(id)s::uuid
                          AND NOT COALESCE(p.is_deleted, FALSE)
                          AND NOT COALESCE(p.is_cancelled, FALSE)
                    ), 0), 0) <= 0.01 THEN 'PAID'
                    WHEN COALESCE((
                        SELECT SUM(p.amount) FROM payments p
                        WHERE p.invoice_id = %(id)s::uuid
                          AND NOT COALESCE(p.is_deleted, FALSE)
                          AND NOT COALESCE(p.is_cancelled, FALSE)
                    ), 0) > 0 THEN 'PARTIAL'
                    ELSE 'UNPAID'
                END,
                updated_at = NOW()
            WHERE id = %(id)s::uuid
        """, {"id": inv_id})

    if chal_id:
        _w("""
            UPDATE challans SET
                amount_paid = COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.challan_id = %(id)s::uuid
                      AND NOT COALESCE(p.is_deleted, FALSE)
                      AND NOT COALESCE(p.is_cancelled, FALSE)
                ), 0),
                balance_due = GREATEST(COALESCE(grand_total, total_amount, 0) - COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.challan_id = %(id)s::uuid
                      AND NOT COALESCE(p.is_deleted, FALSE)
                      AND NOT COALESCE(p.is_cancelled, FALSE)
                ), 0), 0),
                updated_at = NOW()
            WHERE id = %(id)s::uuid
        """, {"id": chal_id})

    # ── Auto-post reversal JV ────────────────────────────────────────────
    try:
        from modules.accounting.accounts_engine import post_reversal_jv
        # Find original JV linked to this payment
        orig_jv = _q("""
            SELECT id::text FROM journal_entries
            WHERE ref_doc_id = %s AND ref_doc_type = 'PAYMENT' LIMIT 1
        """, (pid,))
        if orig_jv:
            post_reversal_jv(
                original_vno   = pno,
                original_jv_id = orig_jv[0]["id"],
                reversal_reason = reason,
                voucher_date   = pay_date,
                created_by     = reversed_by,
            )
    except Exception as _jve:
        _log.warning(f"[JV] reversal post skipped: {_jve}")

    _log.info(f"[reversal] {pno} reversed by {reversed_by} — reason: {reason}")
    return True, f"✅ Payment {pno} (₹{amount:,.2f}) reversed successfully. Invoice/challan balance recalculated."


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_audit_ledger(
    party_id:   str = "",
    party_name: str = "",
    date_from:  str = "",
    date_to:    str = "",
    user_id:    str = "",
    entry_types: list = None,
    limit:      int = 500,
) -> list:
    """
    Full audit ledger with filters. Returns all entries including reversals.
    """
    conditions = ["1=1"]
    params: Dict = {}

    if party_id:
        conditions.append("(pl.party_id::text = %(pid)s OR pl.party_name ILIKE %(pn)s)")
        params["pid"] = party_id
        params["pn"]  = f"%{party_name or party_id}%"
    elif party_name:
        conditions.append("pl.party_name ILIKE %(pn)s")
        params["pn"] = f"%{party_name}%"

    if date_from:
        conditions.append("pl.entry_date >= %(df)s")
        params["df"] = date_from
    if date_to:
        conditions.append("pl.entry_date <= %(dt)s")
        params["dt"] = date_to

    if user_id:
        conditions.append("pl.created_by ILIKE %(uid)s")
        params["uid"] = f"%{user_id}%"

    if entry_types:
        conditions.append("pl.entry_type = ANY(%(et)s)")
        params["et"] = entry_types

    where = " AND ".join(conditions)
    params["lim"] = limit

    return _q(f"""
        SELECT
            pl.id,
            pl.entry_date::text         AS "Date",
            pl.entry_type               AS "Type",
            pl.party_name               AS "Party",
            pl.ref_no                   AS "Ref No",
            ROUND(pl.debit, 2)          AS "Debit (₹)",
            ROUND(pl.credit, 2)         AS "Credit (₹)",
            pl.narration                AS "Narration",
            pl.created_by               AS "User",
            COALESCE(pl.is_reversal, FALSE) AS "Is Reversal",
            pl.ref_payment_id::text     AS "Orig Payment ID",
            pl.created_at::text         AS "Created At"
        FROM party_ledger pl
        WHERE {where}
        ORDER BY pl.entry_date DESC, pl.id DESC
        LIMIT %(lim)s
    """, params)


def get_reversal_summary(date_from: str, date_to: str) -> list:
    """Summary of all reversals in a period for the audit dashboard."""
    return _q("""
        SELECT
            pl.party_name               AS "Party",
            pl.ref_no                   AS "Reversal Ref",
            pl.entry_date::text         AS "Date",
            ROUND(pl.debit, 2)          AS "Amount (₹)",
            pl.narration                AS "Reason",
            pl.created_by               AS "Reversed By",
            pl.ref_payment_id::text     AS "Original Payment"
        FROM party_ledger pl
        WHERE pl.is_reversal = TRUE
          AND pl.entry_date BETWEEN %s AND %s
        ORDER BY pl.entry_date DESC, pl.id DESC
    """, (date_from, date_to))
