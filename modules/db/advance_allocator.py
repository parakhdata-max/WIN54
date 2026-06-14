"""
modules/db/advance_allocator.py
================================
Single source of truth for advance allocation across partial billing.
Runs entirely inside ONE database transaction for atomicity.

The allocator:
1. Acquires an advisory lock (prevents concurrent allocation for same order)
2. Collects all advance + receipt payments for an order
3. Allocates payments sequentially — document-by-document in creation order
4. Updates invoices, non-invoiced challans, and writes audit trail
5. All in a single BEGIN/COMMIT — either everything succeeds or nothing does

Call allocate_order_advance(order_id) after:
- Any invoice creation
- Any payment recording
- Any challan-to-invoice conversion
"""
from __future__ import annotations
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def allocate_order_advance(order_id: str) -> Dict[str, Any]:
    """
    Allocate all payments for an order across its invoices in creation order.
    Runs inside a single DB transaction — atomic, lock-protected.

    Returns summary dict:
    {
        "order_id": "...",
        "total_advance": 5000.00,
        "total_invoiced": 9529.00,
        "invoices_updated": 2,
        "balance_outstanding": 2499.00,
    }
    """
    if not order_id:
        return {}

    from modules.sql_adapter import run_transaction_fn

    def _allocate(conn, cur):
        def q(sql, params=None):
            cur.execute(sql, params or {})
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, row)) for row in cur.fetchall()]

        def w(sql, params=None):
            cur.execute(sql, params or {})

        # ── Step 0: Advisory lock (same session = same tx = auto-released on commit) ──
        lock_key = int(hashlib.md5(order_id.encode()).hexdigest()[:8], 16) % (2**31)
        w(f"SELECT pg_advisory_xact_lock({lock_key})")

        # ── Step 1: Get all invoices ordered by creation ──────────────────────
        invoices = q("""
            SELECT i.id::text, i.invoice_no,
                   COALESCE(i.grand_total, 0)::numeric AS total,
                   i.created_at,
                   i.challan_id::text AS challan_id
            FROM invoices i
            WHERE i.order_ids::text[] @> ARRAY[%(oid)s::text]
              AND COALESCE(i.is_deleted, FALSE) = FALSE
              AND i.status NOT IN ('VOID','CANCELLED')
            ORDER BY i.created_at ASC
        """, {"oid": order_id})

        if not invoices:
            return {}

        # ── Step 2: Direct receipts per invoice (non-advance) ────────────────
        direct: Dict[str, float] = {}
        for inv in invoices:
            cur.execute("""
                SELECT COALESCE(SUM(p.amount), 0)::numeric AS total
                FROM payments p
                WHERE (
                        p.invoice_id = %(iid)s::uuid
                     OR (
                            %(cid)s <> ''
                        AND p.challan_id = NULLIF(%(cid)s,'')::uuid
                        AND p.invoice_id IS NULL
                     )
                )
                  AND NOT COALESCE(p.is_advance, FALSE)
                  AND UPPER(COALESCE(p.payment_type,'')) NOT IN ('ADVANCE')
                  AND COALESCE(p.is_deleted, FALSE) = FALSE
            """, {"iid": inv["id"], "cid": str(inv.get("challan_id") or "")})
            row = cur.fetchone()
            direct[inv["id"]] = float(row[0] if row else 0)

        # ── Step 3: Total advance pool ────────────────────────────────────────
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0)::numeric AS total
            FROM payments
            WHERE advance_for_order_id = %(oid)s::uuid
              AND payment_type = 'ADVANCE'
              AND COALESCE(is_deleted, FALSE) = FALSE
        """, {"oid": order_id})
        row = cur.fetchone()
        advance_pool = float(row[0] if row else 0)

        # ── Step 4: Allocate sequentially across invoices ─────────────────────
        remaining    = advance_pool
        total_inv    = sum(float(inv["total"]) for inv in invoices)
        n_updated    = 0
        audit_rows   = []

        for inv in invoices:
            inv_id    = inv["id"]
            inv_total = float(inv["total"])
            d_recv    = direct.get(inv_id, 0.0)

            needed     = max(0.0, inv_total - d_recv)
            adv_taken  = min(remaining, needed)
            remaining  = max(0.0, remaining - adv_taken)

            total_paid = round(d_recv + adv_taken, 2)
            balance    = max(0.0, round(inv_total - total_paid, 2))
            pay_status = (
                "PAID"    if balance <= 0.50 else
                "PARTIAL" if total_paid > 0  else
                "UNPAID"
            )

            w("""
                UPDATE invoices
                SET amount_paid    = %(paid)s,
                    balance_due    = %(bal)s,
                    payment_status = %(ps)s,
                    status         = CASE
                                       WHEN COALESCE(status,'') IN ('VOID','CANCELLED') THEN status
                                       WHEN %(ps)s = 'PAID' THEN 'PAID'
                                       WHEN %(ps)s = 'EXCESS' THEN 'PAID'
                                       WHEN %(ps)s = 'PARTIAL' THEN 'ACTIVE'
                                       ELSE COALESCE(NULLIF(status,''), 'PENDING')
                                     END,
                    updated_at     = NOW()
                WHERE id = %(iid)s::uuid
                  AND (amount_paid IS DISTINCT FROM %(paid)s
                       OR balance_due IS DISTINCT FROM %(bal)s
                       OR payment_status IS DISTINCT FROM %(ps)s
                       OR status IS DISTINCT FROM CASE
                            WHEN COALESCE(status,'') IN ('VOID','CANCELLED') THEN status
                            WHEN %(ps)s = 'PAID' THEN 'PAID'
                            WHEN %(ps)s = 'EXCESS' THEN 'PAID'
                            WHEN %(ps)s = 'PARTIAL' THEN 'ACTIVE'
                            ELSE COALESCE(NULLIF(status,''), 'PENDING')
                          END)
            """, {
                "paid": total_paid, "bal": balance,
                "ps": pay_status, "iid": inv_id,
            })
            if cur.rowcount:
                n_updated += 1
                log.info("[allocator] %s paid=%.2f bal=%.2f %s",
                         inv.get("invoice_no"), total_paid, balance, pay_status)

            audit_rows.append({
                "invoice_id":      inv_id,
                "invoice_no":      inv.get("invoice_no",""),
                "invoice_total":   inv_total,
                "advance_applied": round(adv_taken, 2),
                "direct_receipts": round(d_recv, 2),
                "remaining_pool":  round(remaining, 2),
            })

        # ── Step 5: Reconcile non-invoiced challans ───────────────────────────
        challans = q("""
            SELECT c.id::text, c.challan_no,
                   COALESCE(c.grand_total, c.total_amount, 0)::numeric AS total,
                   EXISTS(
                       SELECT 1 FROM invoices i
                       WHERE i.challan_id = c.id
                         AND COALESCE(i.is_deleted,FALSE) = FALSE
                   ) AS has_invoice
            FROM challans c
            WHERE c.order_ids::text[] @> ARRAY[%(oid)s::text]
              AND COALESCE(c.is_deleted, FALSE) = FALSE
              AND COALESCE(c.status,'') NOT IN ('VOID','CANCELLED')
            ORDER BY c.created_at ASC
        """, {"oid": order_id})

        chal_adv_rem = advance_pool
        for ch in challans:
            if ch["has_invoice"]:
                continue  # invoice is the source of truth; challan inherits

            ch_id    = ch["id"]
            ch_total = float(ch["total"])

            cur.execute("""
                SELECT COALESCE(SUM(p.amount),0)::numeric AS total
                FROM payments p
                WHERE p.challan_id = %(cid)s::uuid
                  AND NOT COALESCE(p.is_advance, FALSE)
                  AND UPPER(COALESCE(p.payment_type,'')) NOT IN ('ADVANCE')
                  AND COALESCE(p.is_deleted, FALSE) = FALSE
            """, {"cid": ch_id})
            row = cur.fetchone()
            ch_direct = float(row[0] if row else 0)

            ch_adv   = min(chal_adv_rem, max(0.0, ch_total - ch_direct))
            chal_adv_rem = max(0.0, chal_adv_rem - ch_adv)
            ch_paid  = round(ch_direct + ch_adv, 2)
            ch_bal   = max(0.0, round(ch_total - ch_paid, 2))

            w("""
                UPDATE challans
                SET amount_paid      = %(paid)s,
                    balance_due      = %(bal)s,
                    payment_complete = %(done)s,
                    updated_at       = NOW()
                WHERE id = %(cid)s::uuid
                  AND (amount_paid IS DISTINCT FROM %(paid)s
                       OR balance_due IS DISTINCT FROM %(bal)s)
            """, {
                "paid": ch_paid, "bal": ch_bal,
                "done": ch_bal <= 0.50, "cid": ch_id,
            })

        # ── Step 6: Audit trail (same transaction) ────────────────────────────
        try:
            w("""
                INSERT INTO advance_allocation_log
                    (id, order_id, total_advance, allocation_detail,
                     invoices_updated, allocation_run_at)
                VALUES (
                    %(id)s::uuid, %(oid)s::uuid,
                    %(adv)s, %(detail)s::jsonb,
                    %(n)s, NOW()
                )
            """, {
                "id":     str(uuid.uuid4()),
                "oid":    order_id,
                "adv":    advance_pool,
                "detail": json.dumps(audit_rows),
                "n":      n_updated,
            })
        except Exception as _ae:
            log.warning("[allocator] audit log failed (non-fatal): %s", _ae)

        # ── Step 7: Balance calculation (deductions-aware) ────────────────────
        # Outstanding = invoiced - advance - direct receipts - credit notes
        total_direct = sum(direct.values())
        try:
            cur.execute("""
                SELECT COALESCE(SUM(COALESCE(cn.grand_total, cn.total_tax_amount, 0)),0)::numeric AS total
                FROM credit_notes cn
                WHERE cn.order_id = %(oid)s::uuid
                  AND COALESCE(cn.is_deleted,FALSE) = FALSE
                  AND cn.status NOT IN ('VOID','CANCELLED')
            """, {"oid": order_id})
            cn_row = cur.fetchone()
            credit_notes = float(cn_row[0] if cn_row else 0)
        except Exception:
            credit_notes = 0.0

        outstanding = round(
            total_inv - advance_pool - total_direct - credit_notes, 2
        )
        total_received = round(advance_pool + total_direct, 2)
        order_status = (
            "EXCESS" if total_received + credit_notes - total_inv > 0.50 else
            "PAID" if outstanding <= 0.50 and total_inv > 0 else
            "PARTIAL" if total_received > 0 else
            "PENDING"
        )
        try:
            w("""
                UPDATE orders o
                   SET payment_status = %(ps)s,
                       advance_received = %(paid_any)s,
                       billing_complete = NOT EXISTS (
                           SELECT 1
                           FROM order_lines ol
                           WHERE ol.order_id = o.id
                             AND COALESCE(ol.is_deleted, FALSE) = FALSE
                             AND COALESCE(ol.billed_qty, 0)
                                 < COALESCE(ol.billing_qty, ol.quantity, 0)
                       ),
                       updated_at = NOW()
                 WHERE o.id = %(oid)s::uuid
            """, {
                "ps": order_status,
                "paid_any": total_received > 0,
                "oid": order_id,
            })
        except Exception as _oe:
            log.warning("[allocator] order cache update failed: %s", _oe)

        return {
            "order_id":            order_id,
            "total_advance":       advance_pool,
            "total_invoiced":      total_inv,
            "invoices_updated":    n_updated,
            "credit_notes_applied": credit_notes,
            "balance_outstanding": max(0.0, outstanding),
        }

    try:
        result = run_transaction_fn(_allocate)
        return result or {}
    except Exception as e:
        log.error("[allocator] failed for order %s: %s", order_id, e)
        return {}


def allocate_on_invoice_created(invoice_id: str) -> None:
    """Call immediately after a new invoice is created."""
    from modules.sql_adapter import run_query
    rows = run_query("""
        SELECT order_ids[1]::text AS order_id
        FROM invoices
        WHERE id = %(iid)s::uuid
          AND array_length(order_ids, 1) > 0
        LIMIT 1
    """, {"iid": invoice_id}) or []
    if rows and rows[0].get("order_id"):
        allocate_order_advance(rows[0]["order_id"])


def allocate_on_payment_recorded(payment_id: str) -> None:
    """Call immediately after any payment is recorded."""
    from modules.sql_adapter import run_query
    rows = run_query("""
        SELECT COALESCE(order_id::text, advance_for_order_id::text) AS order_id
        FROM payments
        WHERE id = %(pid)s::uuid
        LIMIT 1
    """, {"pid": payment_id}) or []
    if rows and rows[0].get("order_id"):
        allocate_order_advance(rows[0]["order_id"])
