"""
billing_delete_engine.py
========================
Complete void/cancel cascade for challans and invoices.

RULES:
  Invoice voided  → Invoice status='VOID', challan status='PENDING' (released)
  Challan voided  → Challan status='VOID', invoice (if any) also voided,
                    order_lines.billed_qty reset, order status recalculated
  
IMPORTANT:
  - Numbers are NEVER reused. Voided CH/2026/0001 stays in DB as VOID.
  - The void row is visible in lists (collapsed) so GST sequence has no gaps.
  - original_order_info snapshot is saved before voiding so history is preserved.
"""

from __future__ import annotations
import json
import datetime
from decimal import Decimal

class _SafeEncoder(json.JSONEncoder):
    """Handles Decimal, date, datetime, UUID from DB rows."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return str(obj)
        try:
            return str(obj)
        except Exception:
            return None
from typing import Tuple, Optional
import streamlit as st


def _q(sql: str, params: dict = None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        return []


def _w(sql: str, params: dict = None) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception as e:
        st.error(f"DB write error: {e}")
        return False


def _operator() -> str:
    try:
        from modules.security.roles import current_user_name
        u = current_user_name()
        return u if isinstance(u, str) else getattr(u, "name", "backoffice")
    except Exception:
        return st.session_state.get("user_name", "backoffice")


# ─────────────────────────────────────────────────────────────────────────────
# INVOICE VOID
# ─────────────────────────────────────────────────────────────────────────────

def void_invoice(invoice_id: str, reason: str = "") -> Tuple[bool, str]:
    try:
        from modules.security.module_permissions import can as _c
        if not _c("void_invoice"): return False, "⛔ MANAGER/ADMIN required"
    except ImportError: pass
    """
    Void an invoice:
      1. Save snapshot of what it covered
      2. Mark invoice VOID (soft delete)
      3. Restore challan to PENDING so it can be re-invoiced
      4. Order status is NOT changed (challan still covers the order)
    """
    try:
        op = _operator()
        now = datetime.datetime.now().isoformat()

        # ── Fetch invoice details ─────────────────────────────────────
        inv = _q("""
            SELECT i.id::text, i.invoice_no, i.challan_id::text,
                   i.party_id::text, i.order_ids,
                   i.grand_total, i.status
            FROM invoices i
            WHERE i.id = %(id)s::uuid
        """, {"id": invoice_id})
        if not inv:
            return False, "Invoice not found"
        inv = inv[0]

        if inv.get("status") == "VOID":
            return False, f"Invoice {inv['invoice_no']} is already voided"

        # ── Fetch invoice lines for snapshot ─────────────────────────
        lines = _q("""
            SELECT il.order_line_id::text, il.product_name,
                   il.eye_side, il.quantity, il.unit_price, il.total_price
            FROM invoice_lines il
            WHERE il.invoice_id = %(id)s::uuid
              AND NOT COALESCE(il.is_deleted, FALSE)
        """, {"id": invoice_id})

        snapshot = {
            "invoice_no":   inv["invoice_no"],
            "voided_at":    now,
            "voided_by":    op,
            "reason":       reason,
            "grand_total":  str(inv.get("grand_total") or 0),
            "order_ids":    inv.get("order_ids") or [],
            "lines":        lines,
        }

        # ── Mark invoice VOID ─────────────────────────────────────────
        _w("""
            UPDATE invoices
            SET status = 'VOID',
                is_deleted = TRUE,
                deleted_at = NOW(),
                deleted_by = %(op)s,
                void_reason = %(reason)s,
                original_order_info = %(snap)s::jsonb,
                updated_at = NOW()
            WHERE id = %(id)s::uuid
        """, {
            "id":     invoice_id,
            "op":     op,
            "reason": reason or "Voided by user",
            "snap":   json.dumps(snapshot, cls=_SafeEncoder),
        })

        # ── Mark invoice lines deleted ────────────────────────────────
        _w("""
            UPDATE invoice_lines
            SET is_deleted = TRUE, deleted_at = NOW(), deleted_by = %(op)s
            WHERE invoice_id = %(id)s::uuid
              AND NOT COALESCE(is_deleted, FALSE)
        """, {"id": invoice_id, "op": op})

        # ── Restore challan to PENDING ────────────────────────────────
        challan_id = inv.get("challan_id")
        if challan_id:
            _w("""
                UPDATE challans
                SET status = 'PENDING', updated_at = NOW()
                WHERE id = %(cid)s::uuid
                  AND status = 'INVOICED'
            """, {"cid": challan_id})

        # ── Transfer payments from voided invoice → challan ───────────
        # Money already collected stays valid — moves to challan so it
        # applies automatically when next invoice is raised.
        pmt_rows = _q("""
            SELECT id::text, amount
            FROM payments
            WHERE invoice_id = %(iid)s::uuid
              AND COALESCE(is_deleted, FALSE) = FALSE
        """, {"iid": invoice_id})

        total_transferred = 0.0
        if pmt_rows and challan_id:
            for pmt in pmt_rows:
                _w("""
                    UPDATE payments
                    SET invoice_id = NULL,
                        challan_id = %(cid)s::uuid,
                        remarks    = COALESCE(remarks, '') || %(note)s
                    WHERE id = %(pid)s::uuid
                """, {
                    "cid":  challan_id,
                    "pid":  pmt["id"],
                    "note": f" [Re-linked from voided {inv.get('invoice_no','')}]",
                })
            total_transferred = sum(
                float(p.get("amount") or 0) for p in pmt_rows
            )
            # Recalculate challan amount_paid from actual payments (safe, no double-count)
            _w("""
                UPDATE challans
                SET amount_paid = (
                    SELECT COALESCE(SUM(p.amount), 0)
                    FROM payments p
                    WHERE p.challan_id = challans.id
                      AND COALESCE(p.is_deleted, FALSE) = FALSE
                ),
                balance_due = GREATEST(0, grand_total - (
                    SELECT COALESCE(SUM(p.amount), 0)
                    FROM payments p
                    WHERE p.challan_id = challans.id
                      AND COALESCE(p.is_deleted, FALSE) = FALSE
                )),
                updated_at = NOW()
                WHERE id = %(cid)s::uuid
            """, {"cid": challan_id})

        inv_no = inv["invoice_no"]
        pmt_msg = (
            f" · ₹{total_transferred:,.2f} payment transferred to challan"
            if total_transferred else ""
        )
        return True, f"Invoice {inv_no} voided — challan released to PENDING{pmt_msg}"

    except Exception as e:
        return False, f"Invoice void failed: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# CHALLAN VOID
# ─────────────────────────────────────────────────────────────────────────────

def void_challan(challan_id: str, reason: str = "") -> Tuple[bool, str]:
    """
    Void a challan (full cascade):
      1. If challan has an active invoice → void invoice first
      2. Save snapshot of what it covered
      3. Mark challan VOID
      4. Reset billed_qty on all covered order_lines
      5. Recalculate order status
    """
    try:
        op = _operator()
        now = datetime.datetime.now().isoformat()

        # ── Fetch challan ─────────────────────────────────────────────
        ch = _q("""
            SELECT c.id::text, c.challan_no, c.order_ids,
                   c.party_id::text, c.grand_total, c.status
            FROM challans c
            WHERE c.id = %(id)s::uuid
        """, {"id": challan_id})
        if not ch:
            return False, "Challan not found"
        ch = ch[0]

        if ch.get("status") == "VOID":
            return False, f"Challan {ch['challan_no']} is already voided"

        # ── Check for active invoice ──────────────────────────────────
        active_inv = _q("""
            SELECT id::text, invoice_no
            FROM invoices
            WHERE challan_id = %(cid)s::uuid
              AND status NOT IN ('VOID','CANCELLED')
              AND NOT COALESCE(is_deleted, FALSE)
            LIMIT 1
        """, {"cid": challan_id})

        if active_inv:
            # Void invoice first
            ok, msg = void_invoice(active_inv[0]["id"], reason=f"Cascade from challan void: {reason}")
            if not ok:
                return False, f"Could not void linked invoice: {msg}"

        # ── Fetch challan lines for rollback + snapshot ───────────────
        cl_rows = _q("""
            SELECT cl.id::text AS cl_id,
                   cl.order_line_id::text,
                   cl.order_id::text,
                   cl.product_name, cl.eye_side,
                   cl.quantity, cl.unit_price, cl.total_price
            FROM challan_lines cl
            WHERE cl.challan_id = %(cid)s::uuid
              AND NOT COALESCE(cl.is_deleted, FALSE)
        """, {"cid": challan_id})

        order_line_ids = [r["order_line_id"] for r in cl_rows if r.get("order_line_id")]
        # order_id in challan_lines may be UUID or order_no text — collect both
        # Use ch.order_ids (order_no TEXT[]) as primary source for recalculate
        _ch_order_ids = ch.get("order_ids") or []
        order_ids = list({r["order_id"] for r in cl_rows if r.get("order_id")})

        snapshot = {
            "challan_no":  ch["challan_no"],
            "voided_at":   now,
            "voided_by":   op,
            "reason":      reason,
            "grand_total": str(ch.get("grand_total") or 0),
            "order_ids":   ch.get("order_ids") or [],
            "lines":       cl_rows,
        }

        # ── Mark challan VOID ─────────────────────────────────────────
        _w("""
            UPDATE challans
            SET status = 'VOID',
                is_deleted = TRUE,
                deleted_at = NOW(),
                deleted_by = %(op)s,
                void_reason = %(reason)s,
                original_order_info = %(snap)s::jsonb,
                updated_at = NOW()
            WHERE id = %(id)s::uuid
        """, {
            "id":     challan_id,
            "op":     op,
            "reason": reason or "Voided by user",
            "snap":   json.dumps(snapshot, cls=_SafeEncoder),
        })

        # ── Mark challan lines deleted ────────────────────────────────
        _w("""
            UPDATE challan_lines
            SET is_deleted = TRUE, deleted_at = NOW(), deleted_by = %(op)s
            WHERE challan_id = %(cid)s::uuid
              AND NOT COALESCE(is_deleted, FALSE)
        """, {"cid": challan_id, "op": op})

        # ── Reset billed_qty on order_lines ───────────────────────────
        for line_id in order_line_ids:
            _w("""
                UPDATE order_lines
                SET billed_qty = GREATEST(0,
                    COALESCE(billed_qty, 0) - (
                        SELECT COALESCE(quantity, 0)
                        FROM challan_lines
                        WHERE order_line_id = %(lid)s::uuid
                          AND challan_id = %(cid)s::uuid
                        LIMIT 1
                    ))
                WHERE id = %(lid)s::uuid
            """, {"lid": line_id, "cid": challan_id})

        # ── Recalculate order status ──────────────────────────────────
        # Use order_no list from challan header (ch.order_ids = TEXT[] of order_nos)
        # Resolve each order_no → UUID for recalculate_order_status
        _recalc_order_nos = _ch_order_ids if _ch_order_ids else []
        # Also include any UUIDs from challan_lines.order_id
        _recalc_uuids = [o for o in order_ids if o and len(o) == 36 and '-' in o]

        for ono in _recalc_order_nos:
            try:
                from modules.sql_adapter import run_scalar, run_query
                # Resolve order_no → UUID
                _rows = run_query(
                    "SELECT id::text FROM orders WHERE order_no = %(ono)s LIMIT 1",
                    {"ono": ono}
                )
                if _rows:
                    _uuid = _rows[0]["id"]
                    run_scalar(
                        "SELECT public.recalculate_order_status(%(id)s::uuid)",
                        {"id": _uuid}
                    )
            except Exception:
                # Fallback: reset to CONFIRMED by order_no
                _w("""
                    UPDATE orders
                    SET status = 'CONFIRMED', updated_at = NOW()
                    WHERE order_no = %(ono)s
                      AND NOT EXISTS (
                          SELECT 1 FROM order_lines ol
                          WHERE ol.order_id = orders.id
                            AND COALESCE(ol.billed_qty,0) > 0
                      )
                """, {"ono": ono})

        # Also recalculate for any UUID-based order_ids
        for oid in _recalc_uuids:
            try:
                from modules.sql_adapter import run_scalar
                run_scalar(
                    "SELECT public.recalculate_order_status(%(id)s::uuid)",
                    {"id": oid}
                )
            except Exception:
                pass

        ch_no = ch["challan_no"]
        return True, f"Challan {ch_no} voided — {len(order_line_ids)} line(s) released, order status reset"

    except Exception as e:
        return False, f"Challan void failed: {e}"
