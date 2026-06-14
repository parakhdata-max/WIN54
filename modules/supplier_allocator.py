"""
supplier_allocator.py
=====================
Supplier-side payable allocator.
Symmetric to customer-side payment_allocator / financial_allocator.

Truth model:
    purchase_invoices.amount_paid   — total cash/debit-note applied
    purchase_invoices.balance_due   — invoice_total - amount_paid - debit_note_amount
    purchase_invoices.payment_status — UNPAID | PARTIAL | PAID | OVERPAID | VOIDED

All partial payments, debit notes, and advance adjustments go through this module.
Reports read balance_due directly — they never recalculate.

Tables required (run 0015_supplier_payable_allocator.sql first):
    supplier_payments
    supplier_advances
    purchase_invoices (with amount_paid, balance_due, debit_note_amount, due_date columns)
"""

from __future__ import annotations
import uuid as _uuid
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─── DB helpers ───────────────────────────────────────────────────────────────

def _q(sql: str, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        logger.error(f"[SupplierAllocator] query failed: {e}")
        return []


def _w(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        logger.error(f"[SupplierAllocator] write failed: {e}")
        return False


def _get_conn():
    from modules.sql_adapter import get_transaction_connection
    return get_transaction_connection()


# ─── Read helpers ─────────────────────────────────────────────────────────────

def get_invoice(invoice_no: str) -> Optional[dict]:
    """Return a single purchase_invoice row with all allocator fields."""
    rows = _q("""
        SELECT
            invoice_no,
            supplier_id,
            supplier_name,
            invoice_date,
            due_date,
            COALESCE(invoice_total, 0)          AS invoice_total,
            COALESCE(amount_paid, 0)            AS amount_paid,
            COALESCE(balance_due, invoice_total, 0) AS balance_due,
            COALESCE(debit_note_amount, 0)      AS debit_note_amount,
            COALESCE(payment_status, 'UNPAID')  AS payment_status,
            payment_terms
        FROM purchase_invoices
        WHERE invoice_no = %(inv)s
          AND COALESCE(is_deleted, FALSE) = FALSE
        LIMIT 1
    """, {"inv": invoice_no})
    return rows[0] if rows else None


def get_payments_for_invoice(invoice_no: str) -> list:
    """All payment + debit note entries for an invoice."""
    return _q("""
        SELECT id::text, payment_date, amount, payment_mode,
               reference_no, debit_note_no, notes, created_by, created_at
        FROM supplier_payments
        WHERE invoice_no = %(inv)s
          AND COALESCE(is_deleted, FALSE) = FALSE
        ORDER BY payment_date ASC, created_at ASC
    """, {"inv": invoice_no})


def get_supplier_advances(supplier_id: str, unallocated_only: bool = True) -> list:
    """Advances paid to a supplier, optionally filtering unallocated ones."""
    rows = _q("""
        SELECT id::text, amount, advance_date, reference_no, notes,
               is_allocated, allocated_invoice
        FROM supplier_advances
        WHERE supplier_id = %(sid)s
        ORDER BY advance_date ASC
    """, {"sid": supplier_id})
    if unallocated_only:
        return [r for r in rows if not r.get("is_allocated")]
    return rows


def get_outstanding_invoices(
    supplier_id: Optional[str] = None,
    supplier_name_like: Optional[str] = None,
    min_balance: float = 0.01,
) -> list:
    """
    All purchase invoices with outstanding balance.
    Consumed by creditor aging report in registers.py.
    """
    where = ["COALESCE(pi.balance_due, pi.invoice_total, 0) >= %(mb)s",
             "COALESCE(pi.is_deleted, FALSE) = FALSE",
             "COALESCE(pi.payment_status,'UNPAID') != 'VOIDED'"]
    params: dict = {"mb": min_balance}
    if supplier_id:
        where.append("pi.supplier_id::text = %(sid)s")
        params["sid"] = supplier_id
    if supplier_name_like:
        where.append("LOWER(COALESCE(pi.supplier_name,'')) LIKE %(sn)s")
        params["sn"] = f"%{supplier_name_like.lower()}%"

    return _q(f"""
        SELECT
            pi.invoice_no,
            pi.supplier_name,
            pi.supplier_id::text                                     AS supplier_id,
            pi.invoice_date,
            pi.due_date,
            COALESCE(pi.invoice_total, 0)                           AS invoice_total,
            COALESCE(pi.amount_paid, 0)                             AS amount_paid,
            COALESCE(pi.debit_note_amount, 0)                       AS debit_note_amount,
            COALESCE(pi.balance_due, pi.invoice_total, 0)           AS balance_due,
            COALESCE(pi.payment_status, 'UNPAID')                   AS payment_status,
            COALESCE(pi.payment_terms, 'NET30')                     AS payment_terms,
            CASE
                WHEN pi.due_date IS NULL OR pi.due_date >= CURRENT_DATE THEN 'Current'
                WHEN (CURRENT_DATE - pi.due_date) BETWEEN 1  AND 30  THEN '1-30 days'
                WHEN (CURRENT_DATE - pi.due_date) BETWEEN 31 AND 60  THEN '31-60 days'
                WHEN (CURRENT_DATE - pi.due_date) BETWEEN 61 AND 90  THEN '61-90 days'
                ELSE '90+ days'
            END AS aging_bucket,
            CASE
                WHEN pi.due_date IS NULL OR pi.due_date >= CURRENT_DATE THEN 0
                WHEN (CURRENT_DATE - pi.due_date) BETWEEN 1  AND 30  THEN 1
                WHEN (CURRENT_DATE - pi.due_date) BETWEEN 31 AND 60  THEN 2
                WHEN (CURRENT_DATE - pi.due_date) BETWEEN 61 AND 90  THEN 3
                ELSE 4
            END AS aging_rank
        FROM purchase_invoices pi
        WHERE {" AND ".join(where)}
        ORDER BY aging_rank DESC, balance_due DESC
    """, params)


# ─── Write helpers — the allocator core ───────────────────────────────────────

def _recompute_and_update(cur, invoice_no: str) -> None:
    """
    After any change to supplier_payments, recompute amount_paid + balance_due
    and update payment_status on purchase_invoices.
    Called atomically within the same transaction.
    """
    cur.execute("""
        SELECT
            COALESCE(pi.invoice_total, 0)       AS invoice_total,
            COALESCE(pi.debit_note_amount, 0)   AS dn_amount,
            COALESCE(SUM(CASE WHEN sp.payment_mode != 'DEBIT_NOTE'
                              THEN sp.amount ELSE 0 END), 0)  AS cash_paid,
            COALESCE(SUM(CASE WHEN sp.payment_mode = 'DEBIT_NOTE'
                              THEN sp.amount ELSE 0 END), 0)  AS dn_paid
        FROM purchase_invoices pi
        LEFT JOIN supplier_payments sp
               ON sp.invoice_no = pi.invoice_no
              AND COALESCE(sp.is_deleted, FALSE) = FALSE
        WHERE pi.invoice_no = %(inv)s
          AND COALESCE(pi.is_deleted, FALSE) = FALSE
        GROUP BY pi.invoice_total, pi.debit_note_amount
    """, {"inv": invoice_no})
    row = cur.fetchone()
    if not row:
        return

    invoice_total = float(row[0] or 0)
    dn_pi         = float(row[1] or 0)   # debit notes recorded on invoice directly
    cash_paid     = float(row[2] or 0)   # cash / bank / cheque payments
    dn_paid       = float(row[3] or 0)   # debit note payment entries

    total_deductions = cash_paid + dn_paid + dn_pi
    amount_paid  = cash_paid + dn_paid
    balance_due  = max(0.0, round(invoice_total - total_deductions, 2))

    if balance_due <= 0:
        status = "PAID"
    elif amount_paid > 0 or dn_pi > 0:
        status = "PARTIAL"
    else:
        status = "UNPAID"

    cur.execute("""
        UPDATE purchase_invoices
        SET amount_paid    = %(ap)s,
            balance_due    = %(bd)s,
            payment_status = %(ps)s,
            updated_at     = NOW()
        WHERE invoice_no = %(inv)s
    """, {
        "ap":  round(amount_paid, 2),
        "bd":  balance_due,
        "ps":  status,
        "inv": invoice_no,
    })


def _post_supplier_ledger(
    cur,
    supplier_id: str,
    invoice_no: str,
    entry_type: str,
    amount: float,
    entry_date: str,
    notes: str = "",
    created_by: str = "system",
) -> None:
    """
    Fix 4: Write a supplier ledger entry so creditor reports have a proper audit trail.
    entry_type: INVOICE | PAYMENT | DEBIT_NOTE | ADVANCE_ADJ | ADVANCE
    Sign convention: INVOICE = +ve (increases payable), PAYMENT/DEBIT_NOTE = -ve (reduces payable).
    Silently skips if supplier_ledger table does not exist yet.
    """
    if not supplier_id:
        return
    try:
        _signed = amount if entry_type == "INVOICE" else -abs(amount)
        cur.execute("""
            INSERT INTO supplier_ledger
                (id, supplier_id, invoice_no, entry_type, amount,
                 entry_date, notes, created_by, created_at)
            VALUES
                (gen_random_uuid(), %(sid)s::text, %(inv)s, %(et)s, %(amt)s,
                 %(dt)s::date, %(note)s, %(by)s, NOW())
            ON CONFLICT DO NOTHING
        """, {
            "sid":  supplier_id,
            "inv":  invoice_no or "",
            "et":   entry_type,
            "amt":  round(_signed, 2),
            "dt":   entry_date,
            "note": notes or "",
            "by":   created_by,
        })
    except Exception as _le:
        # Non-fatal — ledger table may not exist yet; log and continue
        logger.warning(f"[SupplierAllocator] ledger post skipped ({entry_type} {invoice_no}): {_le}")


def record_payment(
    invoice_no: str,
    amount: float,
    payment_mode: str = "BANK_TRANSFER",
    reference_no: str = "",
    notes: str = "",
    payment_date: Optional[str] = None,
    created_by: str = "system",
) -> tuple[bool, str]:
    """
    Record a partial or full payment against a purchase invoice.
    Returns (success: bool, message: str).
    """
    if amount <= 0:
        return False, "Amount must be greater than zero."

    inv = get_invoice(invoice_no)
    if not inv:
        return False, f"Invoice {invoice_no} not found."
    if inv["payment_status"] == "VOIDED":
        return False, "Cannot record payment on a voided invoice."
    if round(float(inv["balance_due"]), 2) <= 0:
        return False, f"Invoice {invoice_no} is already fully paid (balance = 0)."

    import datetime as _dt_pay
    _pd = payment_date or str(_dt_pay.date.today())   # Fix 2: real date, never string "CURRENT_DATE"
    try:
        from modules.core.date_guard import validate_payment_date
        _ok_dt, _msg_dt = validate_payment_date(
            _pd,
            payment_type="SUPPLIER_PAYMENT",
            payment_mode=payment_mode,
            method=payment_mode,
            remarks=notes,
            reference_no=reference_no,
        )
        if not _ok_dt:
            return False, _msg_dt
    except Exception as _dg_e:
        return False, f"Payment date validation failed: {_dg_e}"

    conn = _get_conn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO supplier_payments
                    (id, invoice_no, payment_date, amount, payment_mode,
                     reference_no, notes, created_by)
                VALUES
                    (%(id)s::uuid, %(inv)s, %(pd)s::date, %(amt)s, %(mode)s,
                     %(ref)s, %(note)s, %(by)s)
            """, {
                "id":   str(_uuid.uuid4()),
                "inv":  invoice_no,
                "pd":   _pd,
                "amt":  round(float(amount), 2),
                "mode": payment_mode.upper(),
                "ref":  reference_no or "",
                "note": notes or "",
                "by":   created_by,
            })
            _recompute_and_update(cur, invoice_no)

            # Fix 4: post to supplier ledger for proper creditor tracking
            _post_supplier_ledger(
                cur,
                supplier_id=str(inv.get("supplier_id") or ""),
                invoice_no=invoice_no,
                entry_type="PAYMENT",
                amount=round(float(amount), 2),
                entry_date=_pd,
                notes=f"{payment_mode} {reference_no or ''}".strip(),
                created_by=created_by,
            )
        conn.commit()
        bal = get_invoice(invoice_no)
        bal_msg = f"Balance remaining: ₹{float(bal['balance_due']):,.2f}" if bal else ""
        logger.info(f"[SupplierAllocator] payment ₹{amount} on {invoice_no} by {created_by}. {bal_msg}")
        return True, f"✅ Payment of ₹{amount:,.2f} recorded. {bal_msg}"
    except Exception as e:
        conn.rollback()
        logger.error(f"[SupplierAllocator] record_payment failed: {e}")
        return False, f"Payment failed: {e}"
    finally:
        conn.close()


def record_debit_note(
    invoice_no: str,
    amount: float,
    debit_note_no: str = "",
    notes: str = "",
    created_by: str = "system",
) -> tuple[bool, str]:
    """
    Record a debit note (supplier credit to us) against a purchase invoice.
    Reduces balance_due without inflating cash paid.
    """
    if amount <= 0:
        return False, "Debit note amount must be greater than zero."

    inv = get_invoice(invoice_no)
    if not inv:
        return False, f"Invoice {invoice_no} not found."

    conn = _get_conn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # Record as DEBIT_NOTE payment mode — kept separate in _recompute
            cur.execute("""
                INSERT INTO supplier_payments
                    (id, invoice_no, payment_date, amount, payment_mode,
                     debit_note_no, notes, created_by)
                VALUES
                    (%(id)s::uuid, %(inv)s, CURRENT_DATE, %(amt)s, 'DEBIT_NOTE',
                     %(dn)s, %(note)s, %(by)s)
            """, {
                "id":   str(_uuid.uuid4()),
                "inv":  invoice_no,
                "amt":  round(float(amount), 2),
                "dn":   debit_note_no or "",
                "note": notes or "",
                "by":   created_by,
            })
            _recompute_and_update(cur, invoice_no)
            import datetime as _dt_dn
            _post_supplier_ledger(
                cur,
                supplier_id=str(inv.get("supplier_id") or ""),
                invoice_no=invoice_no,
                entry_type="DEBIT_NOTE",
                amount=round(float(amount), 2),
                entry_date=str(_dt_dn.date.today()),
                notes=f"DN {debit_note_no}" if debit_note_no else notes or "",
                created_by=created_by,
            )
        conn.commit()
        return True, f"✅ Debit note ₹{amount:,.2f} recorded against {invoice_no}."
    except Exception as e:
        conn.rollback()
        logger.error(f"[SupplierAllocator] record_debit_note failed: {e}")
        return False, f"Debit note failed: {e}"
    finally:
        conn.close()


def apply_advance(
    invoice_no: str,
    advance_id: str,
    amount: float,
    created_by: str = "system",
) -> tuple[bool, str]:
    """
    Fix 3: Apply a supplier advance (partial or full) to a specific invoice.
    Tracks remaining advance balance — only marks fully_allocated when advance is exhausted.
    """
    inv = get_invoice(invoice_no)
    if not inv:
        return False, f"Invoice {invoice_no} not found."

    adv_rows = _q("""
        SELECT id::text, amount, COALESCE(amount_used, 0) AS amount_used,
               supplier_id, reference_no
        FROM supplier_advances
        WHERE id = %(aid)s::uuid
        LIMIT 1
    """, {"aid": advance_id})
    if not adv_rows:
        return False, "Advance not found."
    adv = adv_rows[0]
    adv_total    = float(adv["amount"] or 0)
    adv_used     = float(adv["amount_used"] or 0)
    adv_remaining = round(adv_total - adv_used, 2)
    if adv_remaining <= 0:
        return False, "This advance is already fully allocated."
    if amount > adv_remaining:
        return False, f"Cannot apply ₹{amount:,.2f} — only ₹{adv_remaining:,.2f} remaining in this advance."

    import datetime as _dt_adv
    _pd = str(_dt_adv.date.today())
    conn = _get_conn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO supplier_payments
                    (id, invoice_no, payment_date, amount, payment_mode, notes, created_by)
                VALUES
                    (%(id)s::uuid, %(inv)s, %(pd)s::date, %(amt)s, 'ADVANCE_ADJ',
                     %(note)s, %(by)s)
            """, {
                "id":   str(_uuid.uuid4()),
                "inv":  invoice_no,
                "pd":   _pd,
                "amt":  round(float(amount), 2),
                "note": f"Advance {advance_id[:8]} applied",
                "by":   created_by,
            })

            # Update advance: increment amount_used, mark fully allocated only when exhausted
            _new_used      = round(adv_used + amount, 2)
            _fully_alloc   = _new_used >= adv_total - 0.01  # tolerance for float rounding
            cur.execute("""
                UPDATE supplier_advances
                SET amount_used       = %(used)s,
                    is_allocated      = %(alloc)s,
                    allocated_invoice = CASE
                        WHEN %(alloc)s THEN %(inv)s
                        ELSE COALESCE(allocated_invoice, %(inv)s)
                    END
                WHERE id = %(aid)s::uuid
            """, {
                "used":  _new_used,
                "alloc": _fully_alloc,
                "inv":   invoice_no,
                "aid":   advance_id,
            })

            _recompute_and_update(cur, invoice_no)
            _post_supplier_ledger(
                cur,
                supplier_id=str(inv.get("supplier_id") or ""),
                invoice_no=invoice_no,
                entry_type="ADVANCE_ADJ",
                amount=round(float(amount), 2),
                entry_date=_pd,
                notes=f"Advance {advance_id[:8]} — ₹{amount:,.2f} of ₹{adv_total:,.2f} applied",
                created_by=created_by,
            )
        conn.commit()
        _remaining_after = round(adv_remaining - amount, 2)
        _tail = f" (₹{_remaining_after:,.2f} advance remaining)" if _remaining_after > 0 else " (advance fully used)"
        return True, f"✅ Advance ₹{amount:,.2f} applied to {invoice_no}.{_tail}"
    except Exception as e:
        conn.rollback()
        logger.error(f"[SupplierAllocator] apply_advance failed: {e}")
        return False, f"Advance application failed: {e}"
    finally:
        conn.close()


def void_payment(
    payment_id: str,
    created_by: str = "system",
) -> tuple[bool, str]:
    """Soft-delete a payment and recompute invoice balance."""
    rows = _q("SELECT invoice_no FROM supplier_payments WHERE id = %(pid)s::uuid LIMIT 1",
              {"pid": payment_id})
    if not rows:
        return False, "Payment not found."
    invoice_no = rows[0]["invoice_no"]

    conn = _get_conn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE supplier_payments
                SET is_deleted = TRUE, updated_at = NOW()
                WHERE id = %(pid)s::uuid
            """, {"pid": payment_id})
            _recompute_and_update(cur, invoice_no)
        conn.commit()
        return True, f"✅ Payment voided. Invoice {invoice_no} balance updated."
    except Exception as e:
        conn.rollback()
        return False, f"Void failed: {e}"
    finally:
        conn.close()


# ─── Reconciliation utility ────────────────────────────────────────────────────

def reconcile_all_invoices() -> dict:
    """
    Recompute amount_paid / balance_due / payment_status for ALL purchase invoices.
    Run this once after the migration to backfill existing data.
    Returns summary dict.
    """
    invoices = _q("""
        SELECT invoice_no FROM purchase_invoices
        WHERE COALESCE(is_deleted, FALSE) = FALSE
        ORDER BY invoice_date
    """)
    updated = 0
    failed  = 0
    for inv in invoices:
        conn = _get_conn()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                _recompute_and_update(cur, inv["invoice_no"])
            conn.commit()
            updated += 1
        except Exception as e:
            conn.rollback()
            logger.error(f"[Reconcile] {inv['invoice_no']}: {e}")
            failed += 1
        finally:
            conn.close()
    logger.info(f"[SupplierAllocator] reconcile_all: {updated} updated, {failed} failed")
    return {"updated": updated, "failed": failed}


# ─── Streamlit UI widget ───────────────────────────────────────────────────────

def render_payment_widget(invoice_no: str, key_prefix: str = "") -> None:
    """
    Drop-in UI widget to record supplier payment, debit note, or advance adjustment.
    Call from purchase_invoice.py or purchase_register.py:

        from modules.supplier_allocator import render_payment_widget
        render_payment_widget(invoice_no, key_prefix=f"pi_{invoice_no}")
    """
    import streamlit as st

    inv = get_invoice(invoice_no)
    if not inv:
        st.error(f"Invoice {invoice_no} not found.")
        return

    total    = float(inv["invoice_total"])
    paid     = float(inv["amount_paid"])
    dn       = float(inv["debit_note_amount"])
    balance  = float(inv["balance_due"])
    status   = inv["payment_status"]

    # ── Status bar ───────────────────────────────────────────────────────
    _status_color = {"PAID": "#22c55e", "PARTIAL": "#f59e0b",
                     "UNPAID": "#ef4444", "OVERPAID": "#3b82f6"}.get(status, "#6b7280")
    st.markdown(
        f"<div style='background:#0f172a;border:1px solid {_status_color};"
        f"border-radius:6px;padding:8px 14px;margin-bottom:8px'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<div>"
        f"<span style='color:{_status_color};font-weight:700'>{status}</span>"
        f"&nbsp;<span style='color:#94a3b8;font-size:0.78rem'>{invoice_no}</span>"
        f"</div>"
        f"<div style='text-align:right;color:#e2e8f0;font-size:0.82rem'>"
        f"Invoice: ₹{total:,.2f} &nbsp;·&nbsp; "
        f"Paid: ₹{paid:,.2f} &nbsp;·&nbsp; "
        f"<b>Balance: ₹{balance:,.2f}</b>"
        f"</div></div></div>",
        unsafe_allow_html=True,
    )

    if status == "PAID":
        st.success("✅ Fully paid. Record a debit note if adjustment needed.")

    # ── Payment history ───────────────────────────────────────────────────
    _hist = get_payments_for_invoice(invoice_no)
    if _hist:
        with st.expander(f"📋 Payment history ({len(_hist)} entries)", expanded=False):
            for p in _hist:
                _mode  = p.get("payment_mode", "")
                _amt   = float(p.get("amount") or 0)
                _ref   = p.get("reference_no") or ""
                _dn    = p.get("debit_note_no") or ""
                _dt    = str(p.get("payment_date") or "")[:10]
                _by    = p.get("created_by") or "?"
                _color = "#a855f7" if _mode == "DEBIT_NOTE" else "#22c55e"
                _icon  = "📝" if _mode == "DEBIT_NOTE" else "💳"
                _ref_lbl = _dn or _ref
                st.markdown(
                    f"<div style='border-left:3px solid {_color};"
                    f"padding:4px 10px;margin-bottom:3px;"
                    f"display:flex;justify-content:space-between'>"
                    f"<span style='color:{_color}'>{_icon} {_mode.replace('_',' ')}"
                    f"{f' · {_ref_lbl}' if _ref_lbl else ''}</span>"
                    f"<span style='color:#e2e8f0;font-weight:700'>₹{_amt:,.2f}</span>"
                    f"<span style='color:#64748b;font-size:0.75rem'>{_dt} by {_by}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    if balance <= 0 and status == "PAID":
        return  # nothing more to record

    # ── Record payment / debit note ───────────────────────────────────────
    _kp = key_prefix or invoice_no.replace("/", "_")
    _by = ""
    try:
        _by = st.session_state.get("user_name", "backoffice")
    except Exception:
        pass

    with st.expander("💳 Record Payment / Debit Note", expanded=False):
        _ptype = st.radio(
            "Type",
            ["Cash Payment", "Bank / NEFT / RTGS", "Cheque", "UPI",
             "Advance Adjustment", "Debit Note"],
            horizontal=True,
            key=f"{_kp}_ptype",
        )
        _mode_map = {
            "Cash Payment":         "CASH",
            "Bank / NEFT / RTGS":   "BANK_TRANSFER",
            "Cheque":               "CHEQUE",
            "UPI":                  "UPI",
            "Advance Adjustment":   "ADVANCE_ADJ",
            "Debit Note":           "DEBIT_NOTE",
        }
        _mode = _mode_map[_ptype]

        _pa1, _pa2 = st.columns(2)
        _amt_val = _pa1.number_input(
            "Amount (₹)",
            min_value=0.01,
            max_value=float(max(total, balance + 1)),
            value=round(balance, 2),
            step=10.0,
            key=f"{_kp}_amt",
        )
        _pdate = _pa2.date_input(
            "Payment Date",
            key=f"{_kp}_pdate",
            format="DD/MM/YYYY",
        )

        _ref_no = ""
        _dn_no  = ""
        if _mode in ("CHEQUE", "BANK_TRANSFER", "UPI"):
            _ref_no = st.text_input("Reference / Cheque / UTR No.",
                                     key=f"{_kp}_ref", placeholder="optional")
        if _mode == "DEBIT_NOTE":
            _dn_no = st.text_input("Debit Note No.",
                                    key=f"{_kp}_dnno", placeholder="DN-2024-001")
            st.caption("Debit note reduces your payable without counting as cash outflow.")
        if _mode == "ADVANCE_ADJ":
            # Show unallocated advances for this supplier
            _advs = get_supplier_advances(str(inv.get("supplier_id") or ""), unallocated_only=True)
            if not _advs:
                st.info("No unallocated advances for this supplier.")
                _adv_id = None
            else:
                _adv_opts = {
                    (f"₹{float(a['amount']):,.2f} — "
                     f"used ₹{float(a.get('amount_used',0)):,.2f} — "
                     f"remaining ₹{float(a['amount'])-float(a.get('amount_used',0)):,.2f} "
                     f"({str(a['advance_date'])[:10]})"): a["id"]
                    for a in _advs
                }
                _adv_sel  = st.selectbox("Select Advance", list(_adv_opts.keys()),
                                          key=f"{_kp}_adv_sel")
                _adv_id   = _adv_opts[_adv_sel]
                _adv_row  = next((a for a in _advs if a["id"] == _adv_id), {})
                _adv_remaining = round(float(_adv_row.get("amount", 0)) - float(_adv_row.get("amount_used", 0)), 2)
                st.caption(f"Remaining in this advance: ₹{_adv_remaining:,.2f}")
                # Default amount = min(advance remaining, invoice balance) — partial allowed
                _amt_val = min(_adv_remaining, round(balance, 2))

        _notes = st.text_input("Notes (optional)", key=f"{_kp}_notes",
                                placeholder="e.g. cheque cleared 15 May")

        _pdate_str = str(_pdate)

        if st.button("💾 Record", key=f"{_kp}_save", type="primary",
                     use_container_width=True):
            if _mode == "DEBIT_NOTE":
                ok, msg = record_debit_note(
                    invoice_no,
                    amount=_amt_val,
                    debit_note_no=_dn_no,
                    notes=_notes,
                    created_by=_by,
                )
            elif _mode == "ADVANCE_ADJ" and _adv_id:
                ok, msg = apply_advance(
                    invoice_no,
                    advance_id=_adv_id,
                    amount=_amt_val,
                    created_by=_by,
                )
            else:
                ok, msg = record_payment(
                    invoice_no,
                    amount=_amt_val,
                    payment_mode=_mode,
                    reference_no=_ref_no,
                    notes=_notes,
                    payment_date=_pdate_str,
                    created_by=_by,
                )
            if ok:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)


__all__ = [
    "get_invoice",
    "get_payments_for_invoice",
    "get_outstanding_invoices",
    "get_supplier_advances",
    "record_payment",
    "record_debit_note",
    "apply_advance",
    "void_payment",
    "reconcile_all_invoices",
    "render_payment_widget",
]
