"""
Payment Manager — DV ERP v1.0
==============================
Complete payment engine for Wholesale + Retail billing.

WHOLESALE MODES:
  PRE_PAYMENT    — Customer pays upfront → order executes immediately
  ON_COMPLETION  — Standard: Challan on dispatch → Invoice → pay within credit days
  ON_ACCOUNT     — Credit ledger: Invoice → periodic settlement → party statement

RETAIL MODE:
  ADVANCE_BALANCE — Advance at booking + balance on delivery
                    Challan stays open until fully paid
                    Invoice only after full payment

All modes share one append-only payments table.
DB triggers auto-update challan.amount_paid / invoice.payment_status.
"""

import streamlit as st
import uuid as _uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

# ── DB helpers ────────────────────────────────────────────────────────────

def _q(sql: str, params: dict = None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB error: {e}")
        return []

def _tx(steps: list) -> Tuple[bool, str]:
    """Run list of (sql, params) atomically."""
    try:
        from modules.sql_adapter import run_transaction
        run_transaction(steps)
        return True, ""
    except Exception as e:
        return False, str(e)

def _fc(v) -> str:
    try:    return f"₹{float(v):,.2f}"
    except: return "₹0.00"

def _fd(v) -> str:
    if not v: return "—"
    try:
        if isinstance(v, (date, datetime)): return v.strftime("%d %b %Y")
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except: return str(v)[:10]

def _round_to_rupee(v) -> float:
    return float(Decimal(str(v or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def _pill(status: str, color: str = None) -> str:
    s = str(status or "").upper()
    COLOR = {
        "PAID":"#10b981","PARTIAL":"#f59e0b","UNPAID":"#ef4444",
        "PENDING":"#f59e0b","ADVANCE":"#3b82f6","CASH":"#10b981",
        "UPI":"#8b5cf6","NEFT":"#0ea5e9","RTGS":"#0ea5e9",
        "CHEQUE":"#64748b","CARD":"#a855f7","PRE_PAYMENT":"#3b82f6",
        "ON_COMPLETION":"#10b981","ON_ACCOUNT":"#f59e0b",
        "ADVANCE_BALANCE":"#8b5cf6",
    }
    c = color or COLOR.get(s, "#64748b")
    return f"<span style='background:{c}22;color:{c};padding:2px 9px;border-radius:12px;font-size:0.62rem;font-weight:700;border:1px solid {c}44'>{s}</span>"

_CARD = "background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:16px 20px;margin-bottom:12px"
_HDR  = "color:#475569;font-size:0.62rem;letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px"
_VAL  = "color:#f1f5f9;font-size:1.4rem;font-weight:700;font-family:'IBM Plex Mono',monospace"
_SUB  = "color:#64748b;font-size:0.65rem;margin-top:2px"

# ── Payment modes ─────────────────────────────────────────────────────────

MODES_WHOLESALE = {
    "PRE_PAYMENT":   "💳 Pre-payment (Execute on receipt)",
    "ON_COMPLETION": "📦 On Completion (Invoice on dispatch)",
    "ON_ACCOUNT":    "📒 On Account (Credit ledger)",
}
MODES_RETAIL = {
    "ADVANCE_BALANCE": "🛍️ Advance + Balance",
}
PAYMENT_METHODS = ["CASH","UPI","NEFT","RTGS","CHEQUE","CARD"]

# ═══════════════════════════════════════════════════════════════════════════
# PARTY PAYMENT SETTINGS (embedded in party master)
# ═══════════════════════════════════════════════════════════════════════════

def render_party_payment_settings(party_id: str):
    """Inline payment mode editor — used from party master screen."""
    row = _q("SELECT * FROM parties WHERE id=%(id)s", {"id": party_id})
    if not row: return
    p = row[0]

    st.markdown(f"<div style='{_CARD}'>", unsafe_allow_html=True)
    st.markdown("#### ⚙️ Payment Settings")
    c1, c2, c3 = st.columns(3)
    with c1:
        all_modes = {**MODES_WHOLESALE, **MODES_RETAIL}
        cur_mode  = p.get("payment_mode") or "ON_COMPLETION"
        new_mode  = st.selectbox("Payment Mode", list(all_modes.keys()),
                                  index=list(all_modes.keys()).index(cur_mode) if cur_mode in all_modes else 0,
                                  format_func=lambda x: all_modes[x], key=f"pm_mode_{party_id}")
    with c2:
        new_days  = st.number_input("Credit Days", min_value=0, max_value=365,
                                     value=int(p.get("credit_days") or 30), key=f"pm_days_{party_id}")
    with c3:
        new_limit = st.number_input("Credit Limit ₹", min_value=0.0,
                                     value=float(p.get("credit_limit") or 0), key=f"pm_limit_{party_id}")
    if st.button("💾 Save Settings", key=f"pm_save_{party_id}"):
        ok, err = _tx([(
            "UPDATE parties SET payment_mode=%(m)s, credit_days=%(d)s, credit_limit=%(l)s WHERE id=%(id)s",
            {"m": new_mode, "d": new_days, "l": new_limit, "id": party_id}
        )])
        if ok: st.success("✅ Saved"); st.rerun()
        else:  st.error(f"Failed: {err}")
    st.markdown("</div>", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# RECORD PAYMENT — universal panel
# ═══════════════════════════════════════════════════════════════════════════

def render_record_payment(
    *,
    challan_id:  Optional[str] = None,
    invoice_id:  Optional[str] = None,
    order_id:    Optional[str] = None,
    party_id:    Optional[str] = None,
    party_name:  Optional[str] = None,
    grand_total: float = 0.0,
    amount_paid: float = 0.0,
    payment_type: str = "RECEIPT",   # RECEIPT | ADVANCE
    label: str = "💰 Record Payment",
    key_suffix: str = "",
    context: str = "",
):
    """
    Compact payment entry form.
    Works for: challan payment, invoice payment, advance against order.
    """
    balance = round(grand_total - amount_paid, 2)
    if grand_total > 0 and balance <= 0.50:
        st.success("✅ Fully paid")
        return

    with st.expander(label, expanded=False):
        c1, c2, c3 = st.columns([1.5, 1, 1])
        _uk = f"{context}_{key_suffix}" if context else key_suffix
        with c1:
            method = st.selectbox("Mode", PAYMENT_METHODS, key=f"pm_meth_{_uk}")
        with c2:
            _rounded_due = _round_to_rupee(float(max(balance, 0.0)))
            if grand_total > 0 and abs(_rounded_due - balance) <= 0.50:
                balance = _rounded_due
            _cap = float(max(balance, 0.0)) if grand_total > 0 else 9999999.0
            _amt_default = float(max(balance, 0.0)) if grand_total > 0 else 0.0
            amt = st.number_input("Amount ₹", min_value=0.0,
                                  max_value=_cap,
                                  value=_amt_default,
                                  step=0.01, key=f"pm_amt_{_uk}")
        with c3:
            pay_date = st.date_input("Date", value=date.today(), key=f"pm_dt_{_uk}")

        ref_no = ""
        if method in ("UPI","NEFT","RTGS","CHEQUE"):
            ref_no = st.text_input(
                "Reference / TXN No" if method != "CHEQUE" else "Cheque No",
                key=f"pm_ref_{_uk}")
        provisional_cheque = False
        if method == "CHEQUE" and str(payment_type or "").upper() == "ADVANCE" and pay_date > date.today():
            provisional_cheque = st.checkbox(
                "Provisional advance cheque (post-dated)",
                key=f"pm_pdc_{_uk}",
                help="Future dates are blocked except for provisional advance cheques.",
            )

        remarks = st.text_input("Remarks (optional)", key=f"pm_rmk_{_uk}")

        if st.button("✅ Record Payment", type="primary",
                     width='stretch', key=f"pm_btn_{_uk}"):
            if amt <= 0:
                st.error("Amount must be greater than zero")
            else:
             _submit_payment(
                challan_id=challan_id, invoice_id=invoice_id,
                order_id=order_id, party_id=party_id, party_name=party_name,
                amount=amt, method=method, pay_date=pay_date,
                ref_no=ref_no, remarks=remarks, payment_type=payment_type,
                allow_provisional_advance_cheque=provisional_cheque,
            )


def _submit_payment(
    *, challan_id, invoice_id, order_id, party_id, party_name,
    amount, method, pay_date, ref_no="", remarks="", payment_type="RECEIPT",
    allow_provisional_advance_cheque: bool = False,
    rerun_on_success: bool = True,
):
    try:
        from modules.core.date_guard import validate_payment_date
        _ok_dt, _msg_dt = validate_payment_date(
            pay_date,
            payment_type=payment_type,
            payment_mode=method,
            method=method,
            remarks=remarks,
            reference_no=ref_no,
            allow_provisional_advance_cheque=allow_provisional_advance_cheque,
        )
        if not _ok_dt:
            st.error(_msg_dt)
            return
    except Exception as _date_guard_error:
        st.error(f"Payment date validation failed: {_date_guard_error}")
        return

    # ── Allocate payment number and INSERT in ONE transaction ────────────────
    # alloc_doc_number without cursor = own transaction (legacy).
    # Payment INSERT goes into _tx(steps) = second transaction.
    # Acceptable for payments (low concurrency, staff-initiated) but number
    # and insert are not atomic. Full fix: pass cursor into alloc_doc_number.
    try:
        from modules.db.order_number_registry import alloc_doc_number
        pno = alloc_doc_number("PAYMENT")
    except Exception:
        import uuid as _u2, datetime as _dt2
        pno = f"PAY/{_dt2.date.today().strftime('%Y%m%d')}/{_u2.uuid4().hex[:6].upper()}"
    pid = str(_uuid.uuid4())

    steps = [("""
        INSERT INTO payments
            (id, payment_no, party_id, party_name,
             challan_id, invoice_id, order_id,
             payment_date, payment_mode, amount,
             reference_no, remarks, payment_type,
             is_advance, advance_for_order_id,
             created_by)
        VALUES
            (%(id)s, %(pno)s, %(pid)s, %(pname)s,
             %(cid)s, %(iid)s, %(oid)s,
             %(dt)s, %(meth)s, %(amt)s,
             %(ref)s, %(rmk)s, %(ptype)s,
             %(is_adv)s, %(adv_oid)s,
             %(by)s)
    """, {
        "id":      pid,
        "pno":     pno,
        "pid":     party_id or None,
        "pname":   party_name or None,
        "cid":     challan_id or None,
        "iid":     invoice_id or None,
        "oid":     order_id or None,
        "dt":      pay_date,
        "meth":    method,
        "amt":     amount,
        "ref":     ref_no or None,
        "rmk":     remarks or None,
        "ptype":   payment_type,
        "is_adv":  (payment_type == "ADVANCE"),
        "adv_oid": order_id if payment_type == "ADVANCE" else None,
        "by":      st.session_state.get("user_name", "System"),
    })]

    if payment_type == "ADVANCE" and order_id:
        steps.append((""" 
            UPDATE orders
            SET advance_amount = COALESCE(advance_amount, 0) + %(amt)s,
                advance_received = TRUE,
                payment_status = CASE
                    WHEN COALESCE(total_value, 0) > 0
                     AND COALESCE(advance_amount, 0) + %(amt)s >= COALESCE(total_value, 0) - 0.50
                    THEN 'PAID'
                    ELSE 'PARTIAL'
                END,
                updated_at = NOW()
            WHERE id = %(oid)s::uuid
        """, {
            "amt": amount,
            "oid": order_id,
        }))

    if challan_id:
        steps.append((""" 
            WITH ch AS (
                SELECT id, order_ids, COALESCE(grand_total,total_amount,0) AS gt
                FROM challans
                WHERE id = %(cid)s::uuid
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
            UPDATE challans c
            SET amount_paid = paid.amt,
                advance_applied = COALESCE((
                    SELECT SUM(p.amount)
                    FROM payments p, ch
                    WHERE p.advance_for_order_id::text = ANY(ch.order_ids::text[])
                      AND (COALESCE(p.is_advance,FALSE) OR UPPER(COALESCE(p.payment_type,''))='ADVANCE')
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0),
                balance_due = GREATEST(COALESCE(c.grand_total,c.total_amount,0) - paid.amt, 0),
                payment_complete = CASE
                    WHEN COALESCE(c.grand_total,c.total_amount,0) - paid.amt <= 0.50 THEN TRUE
                    ELSE FALSE
                END,
                updated_at = NOW()
            FROM paid
            WHERE c.id = paid.id
        """, {"cid": challan_id}))
        steps.append((""" 
            WITH ch AS (
                SELECT order_ids FROM challans WHERE id = %(cid)s::uuid
            ),
            tgt AS (
                SELECT o.id, COALESCE(o.total_value,0) AS total_value
                FROM orders o, ch
                WHERE o.id::text = ANY(ch.order_ids::text[])
            ),
            paid AS (
                SELECT tgt.id,
                       COALESCE((SELECT SUM(p.amount) FROM payments p
                                 WHERE p.advance_for_order_id = tgt.id
                                   AND NOT COALESCE(p.is_deleted,FALSE)), 0)
                       +
                       COALESCE((SELECT SUM(p.amount) FROM payments p
                                 JOIN challans c ON p.challan_id = c.id
                                 WHERE tgt.id::text = ANY(c.order_ids::text[])
                                   AND p.invoice_id IS NULL
                                   AND NOT COALESCE(p.is_deleted,FALSE)), 0)
                       +
                       COALESCE((SELECT SUM(p.amount) FROM payments p
                                 JOIN invoices i ON p.invoice_id = i.id
                                 WHERE tgt.id::text = ANY(i.order_ids::text[])
                                   AND NOT COALESCE(p.is_deleted,FALSE)), 0) AS amt
                FROM tgt
            )
            UPDATE orders o
            SET payment_status = CASE
                    WHEN paid.amt - COALESCE(o.total_value,0) > 0.50 THEN 'EXCESS'
                    WHEN COALESCE(o.total_value,0) - paid.amt <= 0.50 THEN 'PAID'
                    WHEN paid.amt > 0 THEN 'PARTIAL'
                    ELSE 'UNPAID'
                END,
                advance_received = paid.amt > 0,
                updated_at = NOW()
            FROM paid
            WHERE o.id = paid.id
        """, {"cid": challan_id}))

    if invoice_id:
        steps.append((""" 
            WITH inv AS (
                SELECT id, order_ids, COALESCE(grand_total,0) AS gt
                FROM invoices
                WHERE id = %(iid)s::uuid
            ),
            paid AS (
                SELECT
                    inv.id,
                    COALESCE((
                        SELECT SUM(p.amount)
                        FROM payments p
                        WHERE p.invoice_id = inv.id
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
            UPDATE invoices i
            SET amount_paid = paid.amt,
                balance_due = GREATEST(COALESCE(i.grand_total,0) - paid.amt, 0),
                status = CASE
                    WHEN COALESCE(i.status,'') IN ('CANCELLED','VOID') THEN i.status
                    WHEN COALESCE(i.grand_total,0) - paid.amt <= 0.50 THEN 'PAID'
                    ELSE 'ACTIVE'
                END,
                payment_status = CASE
                    WHEN paid.amt - COALESCE(i.grand_total,0) > 0.50 THEN 'EXCESS'
                    WHEN COALESCE(i.grand_total,0) - paid.amt <= 0.50 THEN 'PAID'
                    WHEN paid.amt > 0 THEN 'PARTIAL'
                    ELSE 'UNPAID'
                END,
                updated_at = NOW()
            FROM paid
            WHERE i.id = paid.id
        """, {"iid": invoice_id}))
        steps.append((""" 
            WITH inv AS (
                SELECT order_ids FROM invoices WHERE id = %(iid)s::uuid
            ),
            tgt AS (
                SELECT o.id, COALESCE(o.total_value,0) AS total_value
                FROM orders o, inv
                WHERE o.id::text = ANY(inv.order_ids::text[])
            ),
            paid AS (
                SELECT tgt.id,
                       COALESCE((SELECT SUM(p.amount) FROM payments p
                                 WHERE p.advance_for_order_id = tgt.id
                                   AND NOT COALESCE(p.is_deleted,FALSE)), 0)
                       +
                       COALESCE((SELECT SUM(p.amount) FROM payments p
                                 JOIN challans c ON p.challan_id = c.id
                                 WHERE tgt.id::text = ANY(c.order_ids::text[])
                                   AND p.invoice_id IS NULL
                                   AND NOT COALESCE(p.is_deleted,FALSE)), 0)
                       +
                       COALESCE((SELECT SUM(p.amount) FROM payments p
                                 JOIN invoices i ON p.invoice_id = i.id
                                 WHERE tgt.id::text = ANY(i.order_ids::text[])
                                   AND NOT COALESCE(p.is_deleted,FALSE)), 0) AS amt
                FROM tgt
            )
            UPDATE orders o
            SET payment_status = CASE
                    WHEN paid.amt - COALESCE(o.total_value,0) > 0.50 THEN 'EXCESS'
                    WHEN COALESCE(o.total_value,0) - paid.amt <= 0.50 THEN 'PAID'
                    WHEN paid.amt > 0 THEN 'PARTIAL'
                    ELSE 'UNPAID'
                END,
                advance_received = paid.amt > 0,
                updated_at = NOW()
            FROM paid
            WHERE o.id = paid.id
        """, {"iid": invoice_id}))

    # Party ledger entry
    steps.append(("""
        INSERT INTO party_ledger
            (party_id, party_name, entry_date, entry_type,
             ref_id, ref_no, credit, narration)
        VALUES
            (%(pid)s, %(pname)s, %(dt)s, %(etype)s,
             %(rid)s, %(rno)s, %(amt)s, %(nar)s)
    """, {
        "pid":   party_id or None,
        "pname": party_name or None,
        "dt":    pay_date,
        "etype": "ADVANCE" if payment_type == "ADVANCE" else "PAYMENT",
        "rid":   pid,
        "rno":   pno,
        "amt":   amount,
        "nar":   f"{method} — {ref_no or remarks or 'Payment received'}",
    }))

    ok, err = _tx(steps)
    if ok:
        st.success(f"✅ {pno} — {_fc(amount)} recorded via {method}")
        if rerun_on_success:
            st.rerun()
    else:
        st.error(f"❌ Failed: {err}")


# ═══════════════════════════════════════════════════════════════════════════
# RETAIL — ADVANCE + BALANCE PANEL
# Used inside challan preview for retail challans
# ═══════════════════════════════════════════════════════════════════════════

def render_retail_payment_panel(challan_id: str, challan_no: str,
                                 party_id: Optional[str], party_name: str):
    """
    Full retail payment tracker:
    - Shows advance already collected (from order)
    - Balance due
    - Record new payment
    - Auto-enables invoice when fully paid
    """
    ch = _q("""
        SELECT c.id, c.grand_total, c.amount_paid, c.balance_due,
               c.payment_complete, c.advance_applied,
               c.status, c.order_ids
        FROM challans c WHERE c.id = %(id)s
    """, {"id": challan_id})
    if not ch: return
    c = ch[0]

    grand     = float(c.get("grand_total") or 0)
    order_ids = [str(x) for x in (c.get("order_ids") or [])]

    # ── Advance collected at order punch time (advance_for_order_id) ──────
    # Deduct advance already consumed by PREVIOUS invoices on the same order.
    # e.g. Order advance = ₹5,000. First invoice used ₹2,899. Remaining = ₹2,101.
    if order_ids:
        _adv = _q("""
            SELECT COALESCE(SUM(amount), 0) AS tot
            FROM payments
            WHERE advance_for_order_id::text = ANY(%(oids)s)
              AND payment_type = 'ADVANCE'
              AND COALESCE(is_deleted, FALSE) = FALSE
        """, {"oids": order_ids})
        total_advance = float((_adv[0]["tot"] if _adv else 0) or 0)

        # Advance already absorbed by OTHER invoices for this order
        _used = _q("""
            SELECT COALESCE(SUM(i.grand_total), 0) AS used
            FROM invoices i
            WHERE i.challan_id != %(cid)s::uuid
              AND i.status NOT IN ('CANCELLED','VOID')
              AND (
                  i.order_ids && %(oids)s::text[]
                  OR EXISTS (
                      SELECT 1 FROM invoice_lines il
                      JOIN order_lines ol ON ol.id = il.order_line_id
                      WHERE il.invoice_id = i.id
                        AND ol.order_id::text = ANY(%(oids)s)
                  )
              )
        """, {"cid": challan_id, "oids": order_ids})
        advance_used_elsewhere = float((_used[0]["used"] if _used else 0) or 0)
        advance = max(0.0, round(total_advance - advance_used_elsewhere, 2))
    else:
        total_advance          = float(c.get("advance_applied") or 0)
        advance_used_elsewhere = 0.0
        advance                = total_advance

    # ── Payments recorded directly against this challan ───────────────────
    _ch_paid = _q("""
        SELECT COALESCE(SUM(amount), 0) AS tot
        FROM payments
        WHERE challan_id = %(cid)s
          AND COALESCE(is_deleted, FALSE) = FALSE
    """, {"cid": challan_id})
    challan_direct = float((_ch_paid[0]["tot"] if _ch_paid else 0) or 0)

    paid    = round(advance + challan_direct, 2)
    balance = round(max(grand - paid, 0), 2)
    excess  = round(max(paid - grand, 0), 2)
    done    = balance <= 0.50

    # Sync challan row if stale (so invoice gate works correctly)
    if abs(float(c.get("amount_paid") or 0) - paid) > 0.005:
        try:
            from modules.sql_adapter import run_write as _rw_pm
            _rw_pm("""UPDATE challans
                  SET amount_paid=%(p)s, advance_applied=%(a)s,
                      balance_due=%(b)s, payment_complete=%(d)s,
                      updated_at=NOW()
                  WHERE id=%(id)s""",
               {"p": paid, "a": advance, "b": balance,
                "d": done, "id": challan_id})
        except Exception:
            pass  # non-critical sync — challan still functions

    # ── Payment history — all sources combined ────────────────────────────
    payments = _q("""
        SELECT COALESCE(payment_no, id::text) AS payment_no,
               payment_date, payment_mode, amount,
               reference_no, payment_type, remarks
        FROM payments
        WHERE (
              challan_id = %(cid)s
              OR (payment_type = 'ADVANCE'
                  AND advance_for_order_id::text = ANY(%(oids)s))
        )
          AND COALESCE(is_deleted, FALSE) = FALSE
        ORDER BY payment_date ASC, created_at ASC
    """, {"cid": challan_id, "oids": order_ids or [""]})

    # ── KPI strip ────────────────────────────────────────────────────────
    # Advance utilisation breakdown
    _adv_disp = ""
    if total_advance > 0:
        if advance_used_elsewhere > 0:
            _adv_disp = (
                f"<div style='{_SUB};color:#f59e0b'>"
                f"Advance: ₹{total_advance:,.2f} − ₹{advance_used_elsewhere:,.2f} used = "
                f"<b>₹{advance:,.2f} remaining</b></div>"
            )
        else:
            _adv_disp = f"<div style='{_SUB}'>incl. ₹{advance:,.2f} advance</div>"

    st.markdown(f"""
    <div style='display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0'>
      <div style='{_CARD};border-top:3px solid #10b981'>
        <div style='{_HDR}'>Challan Total</div>
        <div style='{_VAL}'>{_fc(grand)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid #3b82f6'>
        <div style='{_HDR}'>Advance Available</div>
        <div style='{_VAL};color:#3b82f6'>{_fc(advance)}</div>
        {_adv_disp}
      </div>
      <div style='{_CARD};border-top:3px solid {"#10b981" if done else "#ef4444"}'>
        <div style='{_HDR}'>{"Excess Received" if excess > 0 else "Balance Due"}</div>
        <div style='{_VAL};color:{"#f59e0b" if excess > 0 else ("#10b981" if done else "#ef4444")}'>{_fc(excess if excess > 0 else balance)}</div>
        {"<div style='" + _SUB + "'>customer credit/refund due</div>" if excess > 0 else ("<div style='" + _SUB + "'>after advance</div>" if advance > 0 else "")}
      </div>
      <div style='{_CARD};border-top:3px solid {"#10b981" if done else "#f59e0b"}'>
        <div style='{_HDR}'>Status</div>
        <div style='margin-top:6px'>
          {"<span style='color:#10b981;font-size:1rem;font-weight:700'>✅ FULLY PAID</span>" if done
           else "<span style='color:#f59e0b;font-size:1rem;font-weight:700'>⏳ PENDING</span>"}
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Payment history ───────────────────────────────────────────────────
    if payments:
        st.markdown("<div style='font-size:0.72rem;color:#64748b;font-weight:600;letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px'>Payment History</div>", unsafe_allow_html=True)
        rows = ""
        for p in payments:
            mcolor = {"CASH":"#10b981","UPI":"#8b5cf6","NEFT":"#0ea5e9",
                      "RTGS":"#0ea5e9","CHEQUE":"#64748b","CARD":"#a855f7"}.get(
                      str(p.get("payment_mode") or "CASH").upper(), "#64748b")
            ptype_badge = ""
            if str(p.get("payment_type") or "").upper() == "ADVANCE":
                ptype_badge = "<span style='background:#3b82f622;color:#3b82f6;padding:1px 6px;border-radius:6px;font-size:0.58rem;font-weight:700;margin-left:4px'>ADVANCE</span>"
            rows += f"""
            <tr>
              <td style='color:#94a3b8;font-family:monospace'>{p['payment_no']}</td>
              <td>{_fd(p.get('payment_date'))}</td>
              <td><span style='background:{mcolor}22;color:{mcolor};padding:2px 8px;border-radius:8px;font-size:0.62rem;font-weight:700'>{p.get('payment_mode','')}</span>{ptype_badge}</td>
              <td style='text-align:right;color:#10b981;font-weight:700;font-family:monospace'>{_fc(p.get('amount',0))}</td>
              <td style='color:#64748b;font-size:0.68rem'>{p.get('reference_no') or p.get('remarks') or '—'}</td>
            </tr>"""
        st.markdown(f"""
        <table style='width:100%;border-collapse:collapse;font-size:0.75rem;margin-bottom:14px'>
          <thead><tr>
            <th style='color:#475569;text-align:left;padding:4px 8px;border-bottom:1px solid #1e293b'>PMT No</th>
            <th style='color:#475569;text-align:left;padding:4px 8px;border-bottom:1px solid #1e293b'>Date</th>
            <th style='color:#475569;text-align:left;padding:4px 8px;border-bottom:1px solid #1e293b'>Mode</th>
            <th style='color:#475569;text-align:right;padding:4px 8px;border-bottom:1px solid #1e293b'>Amount</th>
            <th style='color:#475569;text-align:left;padding:4px 8px;border-bottom:1px solid #1e293b'>Ref</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        """, unsafe_allow_html=True)

    # ── Record payment ────────────────────────────────────────────────────
    if not done:
        render_record_payment(
            challan_id=challan_id, party_id=party_id, party_name=party_name,
            grand_total=grand, amount_paid=paid,
            label=f"💰 Record Balance Payment (Due: {_fc(balance)})",
            key_suffix=f"retail_{challan_id[:8]}",
            context="ch_prev",
        )
    else:
        # ── Invoice gate ──────────────────────────────────────────────────
        existing_inv = _q("""
            SELECT invoice_no FROM invoices
            WHERE challan_id = %(cid)s
              AND COALESCE(is_deleted, FALSE) = FALSE
        """, {"cid": challan_id})

        if existing_inv:
            st.success(f"🧾 Invoice raised: **{existing_inv[0]['invoice_no']}**")
        else:
            st.markdown("""
            <div style='background:#10b98112;border:1px solid #10b98144;border-radius:8px;
                        padding:12px 16px;margin:8px 0;color:#10b981;font-size:0.82rem'>
              ✅ Full payment received — ready to generate invoice
            </div>""", unsafe_allow_html=True)
            if st.button("🧾 Generate Invoice", type="primary",
                         width='stretch', key=f"gen_inv_{challan_id[:8]}"):
                _inv_result = _generate_invoice_from_challan(challan_id, party_id, party_name)
                # Trigger WA for invoice made
                try:
                    from modules.wa_hub import wa_document_attachment, wa_panel, wa_invoice_made
                    from modules.settings.shop_master import get_unit_info as _gui
                    _s  = _gui("retail") or {}
                    _ch = _q("SELECT grand_total, order_ids FROM challans WHERE id=%(i)s", {"i": challan_id})
                    _gt = float(_ch[0]["grand_total"] if _ch else 0)
                    _inv_row = _q("SELECT invoice_no FROM invoices WHERE challan_id=%(i)s AND COALESCE(is_deleted,FALSE)=FALSE ORDER BY created_at DESC LIMIT 1", {"i": challan_id})
                    _inv_no  = _inv_row[0]["invoice_no"] if _inv_row else "—"
                    _mob_inv = str(_q("SELECT COALESCE(p.mobile,'') AS m FROM challans c LEFT JOIN parties p ON p.id=c.party_id WHERE c.id=%(i)s LIMIT 1", {"i": challan_id})[0].get("m","") if challan_id else "")
                    _msg_inv = wa_invoice_made(
                        party=party_name, invoice_no=_inv_no,
                        grand_total=_gt, balance=0,
                        shop_name=_s.get("shop_name","DV Optical"),
                        phone=_s.get("shop_phone",""),
                        upi_id=_s.get("shop_upi_id",""),
                    )
                    wa_panel(_mob_inv, _msg_inv, key="inv_wa_" + _inv_no.replace("/","_"),
                             title="📲 Send Invoice WhatsApp", expanded=True,
                             party_name=party_name,
                             attachments=[
                                 wa_document_attachment("invoice", _inv_no)
                             ])
                except Exception:
                    pass


def _generate_invoice_from_challan(challan_id: str,
                                    party_id: Optional[str], party_name: str):
    ch = _q("SELECT * FROM challans WHERE id=%(id)s", {"id": challan_id})
    if not ch: st.error("Challan not found"); return
    c = ch[0]

    try:
        from modules.db.order_number_registry import alloc_doc_number as _adn_pm
        inv_no = _adn_pm("INVOICE")
    except Exception:
        import uuid as _u_pm, datetime as _dt_pm
        inv_no = f"INV/{_dt_pm.date.today().strftime('%Y%m%d')}/{_u_pm.uuid4().hex[:6].upper()}"
    if not inv_no: st.error("Could not generate invoice number"); return
    inv_id = str(_uuid.uuid4())

    credit_days = 0  # retail: paid in full, due = today

    steps = [("""
        INSERT INTO invoices
            (id, invoice_no, challan_id, party_id, order_ids,
             invoice_date, due_date,
             total_amount, total_tax, grand_total,
             amount_paid, balance_due,
             status, payment_status,
             payment_mode, created_by)
        VALUES
            (%(id)s, %(no)s, %(cid)s, %(pid)s, %(oids)s,
             %(idate)s, %(ddate)s,
             %(sub)s, %(tax)s, %(gnd)s,
             %(gnd)s, 0,
             'PAID', 'PAID',
             'ADVANCE_BALANCE', %(by)s)
    """, {
        "id":    inv_id, "no": inv_no,
        "cid":   challan_id,
        "pid":   party_id or None,
        "oids":  c.get("order_ids") or [],
        "idate": date.today(),
        "ddate": date.today(),
        "sub":   float(c.get("total_amount") or 0),
        "tax":   float(c.get("total_tax")    or 0),
        "gnd":   float(c.get("grand_total")  or 0),
        "by":    st.session_state.get("user_name", "System"),
    }), (
        "UPDATE challans SET status='INVOICED', updated_at=NOW() WHERE id=%(id)s",
        {"id": challan_id}
    )]

    # Party ledger — debit entry (invoice raised)
    steps.append(("""
        INSERT INTO party_ledger
            (party_id, party_name, entry_date, entry_type,
             ref_id, ref_no, debit, narration)
        VALUES (%(pid)s, %(pname)s, %(dt)s, 'INVOICE',
                %(rid)s, %(rno)s, %(amt)s, %(nar)s)
    """, {
        "pid":   party_id or None,
        "pname": party_name,
        "dt":    date.today(),
        "rid":   inv_id,
        "rno":   inv_no,
        "amt":   float(c.get("grand_total") or 0),
        "nar":   f"Invoice for challan {c.get('challan_no','')}",
    }))

    ok, err = _tx(steps)
    if ok:
        st.success(f"✅ Invoice **{inv_no}** generated — payment complete")
        st.rerun()
    else:
        st.error(f"❌ {err}")


# ═══════════════════════════════════════════════════════════════════════════
# WHOLESALE — PAYMENT PANEL
# Used inside invoice preview / invoice list
# ═══════════════════════════════════════════════════════════════════════════

def render_wholesale_payment_panel(invoice_id: str, invoice_no: str,
                                    party_id: Optional[str], party_name: str,
                                    payment_mode: str = "ON_COMPLETION",
                                    context: str = ""):
    """
    ON_COMPLETION / ON_ACCOUNT invoice payment tracker.
    Shows outstanding, history, record payment.
    ON_ACCOUNT shows full party statement below.
    """
    inv = _q("""
        SELECT id, grand_total, amount_paid, balance_due, payment_status,
               due_date, payment_mode, order_ids
        FROM invoices WHERE id = %(id)s
    """, {"id": invoice_id})
    if not inv: return
    i = inv[0]

    grand   = float(i.get("grand_total") or 0)

    _inv_order_ids = [str(x) for x in (i.get("order_ids") or [])]
    if _inv_order_ids:
        try:
            from modules.db.advance_allocator import allocate_order_advance
            for _oid in _inv_order_ids:
                if _oid:
                    allocate_order_advance(_oid)
            _fresh = _q("""
                SELECT amount_paid, balance_due, payment_status
                FROM invoices WHERE id = %(id)s::uuid
            """, {"id": invoice_id}) or []
            if _fresh:
                i.update(_fresh[0])
        except Exception:
            pass

    # The allocator-maintained invoice fields are the source of truth. Do not
    # add the full order advance here, or partial invoices double-consume it.
    paid    = round(float(i.get("amount_paid") or 0), 2)
    balance = round(float(i.get("balance_due") if i.get("balance_due") is not None else max(grand - paid, 0)), 2)
    excess  = round(max(paid - grand, 0), 2)
    pstatus = str(i.get("payment_status") or "UNPAID").upper()
    due     = i.get("due_date")
    is_overdue = (due and (due if isinstance(due, date)
                   else datetime.strptime(str(due)[:10], "%Y-%m-%d").date()) < date.today()
                   and pstatus != "PAID")

    st.markdown(f"""
    <div style='display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0'>
      <div style='{_CARD};border-top:3px solid #38bdf8'>
        <div style='{_HDR}'>Invoice Total</div>
        <div style='{_VAL};color:#38bdf8'>{_fc(grand)}</div>
        <div style='{_SUB}'>{_pill(payment_mode)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid #10b981'>
        <div style='{_HDR}'>Received</div>
        <div style='{_VAL};color:#10b981'>{_fc(paid)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid {"#f59e0b" if excess > 0 else ("#ef4444" if is_overdue else "#f59e0b" if balance > 0 else "#10b981")}'>
        <div style='{_HDR}'>{"Excess Received" if excess > 0 else ("⚠️ OVERDUE" if is_overdue else "Balance Due")}</div>
        <div style='{_VAL};color:{"#f59e0b" if excess > 0 else ("#ef4444" if is_overdue else "#f59e0b" if balance > 0 else "#10b981")}'>{_fc(excess if excess > 0 else balance)}</div>
        <div style='{_SUB}'>{"customer credit/refund due" if excess > 0 else "Due: " + _fd(due)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid {"#10b981" if pstatus=="PAID" else "#f59e0b" if pstatus=="PARTIAL" else "#ef4444"}'>
        <div style='{_HDR}'>Payment Status</div>
        <div style='margin-top:8px'>{_pill(pstatus)}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Payment history
    payments = _q("""
        SELECT COALESCE(payment_no, id::text) AS payment_no,
               payment_date, payment_mode, amount, reference_no, remarks
        FROM payments
        WHERE invoice_id = %(id)s AND COALESCE(is_deleted, FALSE) = FALSE
        ORDER BY payment_date DESC
    """, {"id": invoice_id})

    if payments:
        st.markdown("<div style='font-size:0.72rem;color:#64748b;font-weight:600;letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px'>Payment History</div>", unsafe_allow_html=True)
        rows = ""
        for p in payments:
            mcolor = {"CASH":"#10b981","UPI":"#8b5cf6","NEFT":"#0ea5e9",
                      "RTGS":"#0ea5e9","CHEQUE":"#64748b","CARD":"#a855f7"}.get(
                      str(p.get("payment_mode") or "CASH").upper(), "#64748b")
            rows += f"""<tr>
              <td style='color:#94a3b8;font-family:monospace;padding:5px 8px'>{p['payment_no']}</td>
              <td style='padding:5px 8px'>{_fd(p.get('payment_date'))}</td>
              <td style='padding:5px 8px'><span style='background:{mcolor}22;color:{mcolor};padding:2px 8px;border-radius:8px;font-size:0.62rem;font-weight:700'>{p.get('payment_mode','')}</span></td>
              <td style='text-align:right;color:#10b981;font-weight:700;font-family:monospace;padding:5px 8px'>{_fc(p.get('amount',0))}</td>
              <td style='color:#64748b;font-size:0.68rem;padding:5px 8px'>{p.get('reference_no') or p.get('remarks') or '—'}</td>
            </tr>"""
        st.markdown(f"""<table style='width:100%;border-collapse:collapse;font-size:0.75rem;margin-bottom:14px;background:#0a0f1a;border-radius:8px;overflow:hidden'>
          <thead><tr style='background:#0f172a'>
            <th style='color:#475569;text-align:left;padding:6px 8px'>PMT No</th>
            <th style='color:#475569;text-align:left;padding:6px 8px'>Date</th>
            <th style='color:#475569;text-align:left;padding:6px 8px'>Mode</th>
            <th style='color:#475569;text-align:right;padding:6px 8px'>Amount</th>
            <th style='color:#475569;text-align:left;padding:6px 8px'>Reference</th>
          </tr></thead><tbody>{rows}</tbody></table>""", unsafe_allow_html=True)

    if pstatus != "PAID":
        render_record_payment(
            invoice_id=invoice_id, party_id=party_id, party_name=party_name,
            grand_total=grand, amount_paid=paid,
            label=f"💰 Record Payment (Balance: {_fc(balance)})",
            key_suffix=f"ws_{invoice_id[:8]}",
            context=context or f"inv_{invoice_id[-8:]}",
        )

    # ON_ACCOUNT: show party statement below
    if payment_mode == "ON_ACCOUNT":
        with st.expander("📒 Party Account Statement", expanded=False):
            render_party_statement(party_id=party_id, party_name=party_name)



# ═══════════════════════════════════════════════════════════════════════════
# ORDER-LEVEL ADVANCE PANEL
# Shown on backoffice order detail for:
#   RETAIL        → advance at punch time, balance on delivery
#   PRE_PAYMENT   → full payment required before order executes
# ═══════════════════════════════════════════════════════════════════════════

def render_order_advance_panel(order: dict, all_lines: list):
    """
    Smart payment panel that adapts to order type.
    Call this from backoffice order detail, after billing_status_panel.
    """
    order_id   = str(order.get("id") or "")
    order_no   = str(order.get("order_no") or "")
    order_type = str(order.get("order_type") or "RETAIL").upper()
    party_id   = str(order.get("party_id") or "") or None
    party_name = str(order.get("party_name") or order.get("patient_name") or "")

    # Debug: show order_id so we can verify UUID is present
    if not order_id or len(order_id) < 10:
        st.warning(f"⚠️ Payment panel: order UUID not resolved (got: `{order_id!r}`). "
                   f"Payments may not load correctly.")

    # Resolve payment mode: order-level → party-level → default by type
    pmode = str(order.get("payment_mode") or "").upper()
    if not pmode and party_id:
        prow = _q("SELECT payment_mode FROM parties WHERE id=%(id)s", {"id": party_id})
        pmode = str((prow[0].get("payment_mode") if prow else None) or "").upper()
    if not pmode:
        pmode = "ADVANCE_BALANCE" if order_type == "RETAIL" else "ON_COMPLETION"

    # Only show this panel for modes that need upfront payment
    if pmode not in ("ADVANCE_BALANCE", "PRE_PAYMENT"):
        return

    # Compute order total including service charges
    order_total = _compute_order_total(all_lines, order_type, order_id)

    # Advances already collected against this order
    adv_rows = _q("""
        SELECT COALESCE(SUM(amount), 0) AS total,
               COUNT(*) AS count
        FROM payments
        WHERE advance_for_order_id::text = %(oid)s
          AND COALESCE(is_deleted, FALSE) = FALSE
    """, {"oid": order_id})
    adv_total = float((adv_rows[0]["total"] if adv_rows else 0) or 0)
    adv_count = int((adv_rows[0]["count"]  if adv_rows else 0) or 0)
    balance   = round(order_total - adv_total, 2)
    paid_up   = balance <= 0.01

    if pmode == "PRE_PAYMENT":
        _render_prepayment_order_panel(
            order_id=order_id, order_no=order_no,
            party_id=party_id, party_name=party_name,
            order_total=order_total, adv_total=adv_total,
            adv_count=adv_count, balance=balance, paid_up=paid_up,
        )
    else:  # ADVANCE_BALANCE (retail)
        _render_retail_advance_order_panel(
            order_id=order_id, order_no=order_no,
            party_id=party_id, party_name=party_name,
            order_total=order_total, adv_total=adv_total,
            adv_count=adv_count, balance=balance, paid_up=paid_up,
        )


def _render_prepayment_order_panel(
    *, order_id, order_no, party_id, party_name,
    order_total, adv_total, adv_count, balance, paid_up,
):
    """PRE_PAYMENT: full amount required before order executes."""
    pct = min(int(adv_total / order_total * 100), 100) if order_total else 0
    bar_color = "#10b981" if paid_up else "#3b82f6"

    st.markdown(f"""
    <div style='{_CARD};border-left:4px solid {bar_color}'>
      <div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px'>
        <div>
          <div style='{_HDR}'>💳 PRE-PAYMENT REQUIRED</div>
          <div style='color:#f1f5f9;font-size:0.95rem;font-weight:700;margin-top:3px'>{party_name}</div>
          <div style='color:#64748b;font-size:0.7rem'>Order executes only after full payment</div>
        </div>
        <div style='text-align:right'>
          {"<span style='background:#10b98122;color:#10b981;padding:4px 14px;border-radius:12px;font-size:0.72rem;font-weight:700;border:1px solid #10b98144'>✅ FULLY PAID — ORDER CONFIRMED</span>" if paid_up
           else f"<span style='background:#ef444422;color:#ef4444;padding:4px 14px;border-radius:12px;font-size:0.72rem;font-weight:700;border:1px solid #ef444444'>🔴 AWAITING ₹{balance:,.2f}</span>"}
        </div>
      </div>
      <div style='display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px'>
        <div><div style='{_HDR}'>Order Value</div><div style='{_VAL};font-size:1.1rem'>{_fc(order_total)}</div></div>
        <div><div style='{_HDR}'>Received ({adv_count} payment{"s" if adv_count!=1 else ""})</div><div style='{_VAL};font-size:1.1rem;color:#10b981'>{_fc(adv_total)}</div></div>
        <div><div style='{_HDR}'>{"✅ Fully Paid" if paid_up else "Still Needed"}</div><div style='{_VAL};font-size:1.1rem;color:{"#10b981" if paid_up else "#ef4444"}'>{_fc(max(balance,0))}</div></div>
      </div>
      <div style='background:#0a0f1a;border-radius:4px;height:6px;margin-bottom:4px'>
        <div style='background:{bar_color};width:{pct}%;height:6px;border-radius:4px;transition:width .3s'></div>
      </div>
      <div style='color:#475569;font-size:0.65rem;text-align:right'>{pct}% collected</div>
    </div>
    """, unsafe_allow_html=True)

    if paid_up:
        # Auto-confirm if still PENDING
        if str(order.get("status","")).upper() == "PENDING":
            _q("UPDATE orders SET status='CONFIRMED' WHERE id=%(id)s::uuid", {"id": order_id})
        return

    # Payment history
    _show_payment_history(order_id=order_id, key_prefix=f"pre_{order_id[:8]}")

    # Record advance
    render_record_payment(
        order_id=order_id,
        party_id=party_id, party_name=party_name,
        grand_total=order_total, amount_paid=adv_total,
        payment_type="ADVANCE",
        label=f"💳 Record Payment (₹{max(balance,0):,.2f} remaining)",
        key_suffix=f"pre_{order_id[:8]}",
        context="bo_order",
    )


def _render_retail_advance_order_panel(
    *, order_id, order_no, party_id, party_name,
    order_total, adv_total, adv_count, balance, paid_up,
):
    """RETAIL: Advance at punch time, balance collected on delivery via challan."""
    pct = min(int(adv_total / order_total * 100), 100) if order_total else 0

    st.markdown(f"""
    <div style='{_CARD};border-left:4px solid #8b5cf6'>
      <div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px'>
        <div>
          <div style='{_HDR}'>🛍️ RETAIL — ADVANCE + BALANCE</div>
          <div style='color:#f1f5f9;font-size:0.95rem;font-weight:700;margin-top:3px'>{party_name}</div>
          <div style='color:#64748b;font-size:0.7rem'>Advance collected now · Balance on delivery (via challan)</div>
        </div>
        <div style='text-align:right'>
          {"<span style='background:#10b98122;color:#10b981;padding:4px 14px;border-radius:12px;font-size:0.72rem;font-weight:700;border:1px solid #10b98144'>✅ ADVANCE COLLECTED</span>" if adv_total > 0
           else "<span style='background:#f59e0b22;color:#f59e0b;padding:4px 14px;border-radius:12px;font-size:0.72rem;font-weight:700;border:1px solid #f59e0b44'>⏳ NO ADVANCE YET</span>"}
        </div>
      </div>
      <div style='display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px'>
        <div>
          <div style='{_HDR}'>MRP Total</div>
          <div style='{_VAL};font-size:1.1rem'>{_fc(order_total)}</div>
        </div>
        <div>
          <div style='{_HDR}'>Advance Collected</div>
          <div style='{_VAL};font-size:1.1rem;color:#8b5cf6'>{_fc(adv_total)}</div>
          <div style='{_SUB}'>{adv_count} payment{"s" if adv_count!=1 else ""}</div>
        </div>
        <div>
          <div style='{_HDR}'>Balance on Delivery</div>
          <div style='{_VAL};font-size:1.1rem;color:#f59e0b'>{_fc(max(balance,0))}</div>
          <div style='{_SUB}'>collected via challan</div>
        </div>
      </div>
      <div style='background:#0a0f1a;border-radius:4px;height:6px;margin-bottom:4px'>
        <div style='background:#8b5cf6;width:{pct}%;height:6px;border-radius:4px'></div>
      </div>
      <div style='color:#475569;font-size:0.65rem;text-align:right'>{pct}% advance of total</div>
    </div>
    """, unsafe_allow_html=True)

    # Payment history
    _show_payment_history(order_id=order_id, key_prefix=f"ret_{order_id[:8]}")

    # Record advance (no max cap — customer might pay full amount upfront)
    render_record_payment(
        order_id=order_id,
        party_id=party_id, party_name=party_name,
        grand_total=order_total, amount_paid=adv_total,
        payment_type="ADVANCE",
        label="💰 Record Advance Payment",
        key_suffix=f"adv_{order_id[:8]}",
        context="bo_order",
    )


def _show_payment_history(*, order_id: str, key_prefix: str):
    """Compact advance payment history for order detail."""
    rows = _q("""
        SELECT COALESCE(payment_no, id::text) AS payment_no,
               payment_date, payment_mode, amount, reference_no, remarks
        FROM payments
        WHERE advance_for_order_id::text = %(oid)s
          AND COALESCE(is_deleted, FALSE) = FALSE
        ORDER BY payment_date DESC, created_at DESC
    """, {"oid": order_id})

    if not rows:
        return

    st.markdown("<div style='font-size:0.7rem;color:#64748b;font-weight:600;letter-spacing:.07em;text-transform:uppercase;margin:8px 0 4px'>Payments Received</div>", unsafe_allow_html=True)
    trows = ""
    for p in rows:
        mc = {"CASH":"#10b981","UPI":"#8b5cf6","NEFT":"#0ea5e9",
              "RTGS":"#0ea5e9","CHEQUE":"#64748b","CARD":"#a855f7"}.get(
              str(p.get("payment_mode","")).upper(), "#64748b")
        trows += f"""<tr>
          <td style='padding:4px 8px;color:#94a3b8;font-family:monospace;font-size:0.7rem'>{p['payment_no']}</td>
          <td style='padding:4px 8px;color:#94a3b8;font-size:0.72rem'>{_fd(p.get('payment_date'))}</td>
          <td style='padding:4px 8px'><span style='background:{mc}22;color:{mc};padding:1px 7px;border-radius:8px;font-size:0.6rem;font-weight:700'>{p.get('payment_mode','')}</span></td>
          <td style='padding:4px 8px;text-align:right;color:#10b981;font-weight:700;font-family:monospace;font-size:0.8rem'>{_fc(p.get('amount',0))}</td>
          <td style='padding:4px 8px;color:#64748b;font-size:0.67rem'>{p.get('reference_no') or p.get('remarks') or '—'}</td>
        </tr>"""
    st.markdown(f"""
    <table style='width:100%;border-collapse:collapse;background:#0a0f1a;border-radius:6px;overflow:hidden;margin-bottom:10px'>
      <thead><tr style='background:#0f172a'>
        <th style='color:#475569;padding:5px 8px;text-align:left;font-size:0.6rem'>PMT NO</th>
        <th style='color:#475569;padding:5px 8px;text-align:left;font-size:0.6rem'>DATE</th>
        <th style='color:#475569;padding:5px 8px;text-align:left;font-size:0.6rem'>MODE</th>
        <th style='color:#475569;padding:5px 8px;text-align:right;font-size:0.6rem'>AMOUNT</th>
        <th style='color:#475569;padding:5px 8px;text-align:left;font-size:0.6rem'>REF</th>
      </tr></thead>
      <tbody>{trows}</tbody>
    </table>
    """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# PRE-PAYMENT — advance gate for order execution
# ═══════════════════════════════════════════════════════════════════════════

def render_prepayment_panel(order_id: str, order_no: str,
                             party_id: Optional[str], party_name: str,
                             order_total: float):
    """
    Shown for PRE_PAYMENT mode orders.
    Order stays in PENDING until advance >= order total.
    Once paid → auto-advance to CONFIRMED → normal workflow.
    """
    # Check existing advance
    adv = _q("""
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM payments
        WHERE advance_for_order_id::text = %(oid)s
          AND is_deleted = FALSE
    """, {"oid": order_id})
    adv_total = float((adv[0]["total"] if adv else 0) or 0)
    balance   = round(order_total - adv_total, 2)
    paid_up   = balance <= 0.01

    st.markdown(f"""
    <div style='{_CARD};border-left:4px solid {"#10b981" if paid_up else "#3b82f6"}'>
      <div style='display:flex;justify-content:space-between;align-items:center'>
        <div>
          <div style='{_HDR}'>💳 PRE-PAYMENT ORDER — {order_no}</div>
          <div style='color:#f1f5f9;font-size:1rem;font-weight:600;margin-top:4px'>{party_name}</div>
        </div>
        <div style='text-align:right'>
          <div style='{_HDR}'>Order Value</div>
          <div style='{_VAL};color:#38bdf8'>{_fc(order_total)}</div>
        </div>
        <div style='text-align:right'>
          <div style='{_HDR}'>Advance Received</div>
          <div style='{_VAL};color:#10b981'>{_fc(adv_total)}</div>
        </div>
        <div style='text-align:right'>
          <div style='{_HDR}'>{"✅ Fully Paid" if paid_up else "Balance to Receive"}</div>
          <div style='{_VAL};color:{"#10b981" if paid_up else "#f59e0b"}'>{_fc(max(balance,0))}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    if not paid_up:
        render_record_payment(
            order_id=order_id, party_id=party_id, party_name=party_name,
            grand_total=order_total, amount_paid=adv_total,
            payment_type="ADVANCE",
            label=f"💳 Record Advance (Required: {_fc(balance)})",
            key_suffix=f"pre_{order_id[:8]}",
        )
    else:
        # Auto-confirm order
        _q("UPDATE orders SET status='CONFIRMED' WHERE id=%(id)s::uuid AND status='PENDING'",
           {"id": order_id})
        st.success("✅ Payment complete — order confirmed and queued for processing")


# ═══════════════════════════════════════════════════════════════════════════
# PARTY ACCOUNT STATEMENT (ON_ACCOUNT mode)
# ═══════════════════════════════════════════════════════════════════════════

def render_party_statement(party_id: Optional[str] = None,
                            party_name: Optional[str] = None):
    """Full debit/credit ledger for a party."""
    if not party_id and not party_name:
        st.info("Select a party to view statement"); return

    where = "party_id = %(pid)s::uuid" if party_id else "party_name = %(pname)s"
    params = {"pid": party_id} if party_id else {"pname": party_name}

    rows = _q(f"""
        SELECT entry_date, entry_type, ref_no,
               debit, credit,
               debit - credit AS net,
               narration
        FROM party_ledger
        WHERE {where}
        ORDER BY entry_date ASC, created_at ASC
    """, params)

    if not rows:
        st.info("No ledger entries found"); return

    # Running balance
    running = 0.0
    table_rows = ""
    for r in rows:
        dr = float(r.get("debit") or 0)
        cr = float(r.get("credit") or 0)
        running += dr - cr
        bal_color = "#ef4444" if running > 0 else "#10b981"
        etype = str(r.get("entry_type") or "")
        etype_color = {"INVOICE":"#f59e0b","PAYMENT":"#10b981",
                       "ADVANCE":"#3b82f6","CREDIT_NOTE":"#8b5cf6"}.get(etype, "#64748b")
        table_rows += f"""<tr>
          <td style='padding:5px 8px;color:#94a3b8'>{_fd(r.get('entry_date'))}</td>
          <td style='padding:5px 8px'><span style='color:{etype_color};font-size:0.65rem;font-weight:700'>{etype}</span></td>
          <td style='padding:5px 8px;color:#94a3b8;font-family:monospace;font-size:0.72rem'>{r.get('ref_no','—')}</td>
          <td style='padding:5px 8px;text-align:right;color:#f59e0b;font-family:monospace'>{_fc(dr) if dr else "—"}</td>
          <td style='padding:5px 8px;text-align:right;color:#10b981;font-family:monospace'>{_fc(cr) if cr else "—"}</td>
          <td style='padding:5px 8px;text-align:right;color:{bal_color};font-family:monospace;font-weight:700'>{_fc(abs(running))} {"Dr" if running > 0 else "Cr"}</td>
          <td style='padding:5px 8px;color:#64748b;font-size:0.68rem'>{r.get('narration','')}</td>
        </tr>"""

    bal_color = "#ef4444" if running > 0 else "#10b981"
    bal_label = f"{_fc(abs(running))} {'Dr (Party owes you)' if running > 0 else 'Cr (You owe party)'}"

    st.markdown(f"""
    <div style='background:#0a0f1a;border-radius:8px;overflow:hidden;margin-bottom:12px'>
    <table style='width:100%;border-collapse:collapse;font-size:0.75rem'>
      <thead><tr style='background:#0f172a;border-bottom:1px solid #1e293b'>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Date</th>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Type</th>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Ref</th>
        <th style='color:#475569;padding:7px 8px;text-align:right'>Debit</th>
        <th style='color:#475569;padding:7px 8px;text-align:right'>Credit</th>
        <th style='color:#475569;padding:7px 8px;text-align:right'>Balance</th>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Narration</th>
      </tr></thead>
      <tbody>{table_rows}</tbody>
      <tfoot><tr style='background:#0f172a;border-top:2px solid #1e293b'>
        <td colspan='5' style='padding:7px 8px;color:#94a3b8;font-weight:700'>Closing Balance</td>
        <td style='padding:7px 8px;text-align:right;color:{bal_color};font-weight:700;font-family:monospace'>{bal_label}</td>
        <td></td>
      </tr></tfoot>
    </table></div>
    """, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# PAYMENTS DASHBOARD — main tab
# ═══════════════════════════════════════════════════════════════════════════

def render_payments_dashboard():
    """Full payments tab: pending collections + advance tracking + party statements."""
    st.markdown("### 💰 Payments & Collections")

    sub_pending, sub_advance, sub_statement, sub_history = st.tabs([
        "⏳ Pending Collections",
        "💳 Advances (Pre-payment)",
        "📒 Party Statement",
        "📜 Payment History",
    ])

    with sub_pending:  _render_pending_collections()
    with sub_advance:  _render_advances()
    with sub_statement: _render_party_statement_tab()
    with sub_history:  _render_payment_history()


def _render_pending_collections():
    """All unpaid/partial invoices grouped by mode with action buttons."""
    rows = _q("""
        SELECT i.id, i.invoice_no, i.invoice_date, i.due_date,
               i.payment_mode, i.payment_status,
               i.grand_total, i.amount_paid,
               COALESCE(i.balance_due, i.grand_total - COALESCE(i.amount_paid,0)) AS balance_due,
               COALESCE(p.party_name,
                   (SELECT o2.party_name FROM orders o2
                    WHERE o2.id::text = ANY(i.order_ids) LIMIT 1), 'Unknown') AS party_name,
               i.party_id,
               c.challan_no
        FROM invoices i
        LEFT JOIN parties p  ON p.id = i.party_id
        LEFT JOIN challans c ON c.id = i.challan_id
        WHERE i.payment_status IN ('UNPAID','PARTIAL')
          AND COALESCE(i.is_deleted, FALSE) = FALSE
        ORDER BY i.due_date ASC NULLS LAST, i.invoice_date ASC
    """)

    if not rows:
        st.success("✅ No pending collections — all invoices paid!"); return

    total_outstanding = sum(float(r.get("balance_due") or 0) for r in rows)
    overdue = [r for r in rows if r.get("due_date") and
               (r["due_date"] if isinstance(r["due_date"], date)
                else datetime.strptime(str(r["due_date"])[:10],"%Y-%m-%d").date()) < date.today()]

    st.markdown(f"""
    <div style='display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px'>
      <div style='{_CARD};border-top:3px solid #ef4444'>
        <div style='{_HDR}'>Total Outstanding</div>
        <div style='{_VAL};color:#ef4444'>{_fc(total_outstanding)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid #f59e0b'>
        <div style='{_HDR}'>Pending Invoices</div>
        <div style='{_VAL}'>{len(rows)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid #ef4444'>
        <div style='{_HDR}'>Overdue</div>
        <div style='{_VAL};color:#ef4444'>{len(overdue)}</div>
        <div style='{_SUB}'>{_fc(sum(float(r.get("balance_due",0)) for r in overdue))}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    for r in rows:
        due_d = r.get("due_date")
        is_od = False
        if due_d:
            dd = due_d if isinstance(due_d, date) else datetime.strptime(str(due_d)[:10],"%Y-%m-%d").date()
            is_od = dd < date.today()
        border = "#ef4444" if is_od else "#f59e0b" if str(r.get("payment_status")) == "PARTIAL" else "#1e293b"
        pmode  = str(r.get("payment_mode") or "ON_COMPLETION")
        inv_id = str(r["id"])

        with st.expander(
            f"{'⚠️ ' if is_od else ''}**{r['invoice_no']}** · {r['party_name']} · "
            f"{_fc(r.get('balance_due',0))} due · {r.get('payment_status','')}", expanded=False
        ):
            render_wholesale_payment_panel(
                invoice_id=inv_id,
                invoice_no=r["invoice_no"],
                party_id=str(r["party_id"]) if r.get("party_id") else None,
                party_name=r["party_name"],
                payment_mode=pmode,
                context=f"pend_{inv_id[-8:]}",
            )


def _render_advances():
    """PRE_PAYMENT orders: shows advance received vs required."""
    orders = _q("""
        SELECT o.id, o.order_no, o.party_name, o.party_id,
               o.status, o.created_at,
               COALESCE(SUM(
                   ROUND(ol.unit_price * ol.quantity, 2) *
                   CASE UPPER(COALESCE(o.order_type,'WHOLESALE'))
                   WHEN 'RETAIL' THEN 1
                   ELSE (1 + COALESCE(ol.gst_percent,0)/100) END
               ), 0) AS order_total
        FROM orders o
        JOIN order_lines ol ON ol.order_id = o.id
            AND COALESCE(ol.is_deleted, FALSE) = FALSE
        LEFT JOIN parties p ON p.id = o.party_id
        WHERE (p.payment_mode = 'PRE_PAYMENT'
               OR o.payment_mode = 'PRE_PAYMENT')
          AND o.status IN ('PENDING','CONFIRMED','READY_FOR_BILLING')
        GROUP BY o.id, o.order_no, o.party_name, o.party_id, o.status, o.created_at
        ORDER BY o.created_at DESC
    """)

    if not orders:
        st.info("No pre-payment orders pending."); return

    for o in orders:
        render_prepayment_panel(
            order_id=str(o["id"]),
            order_no=o["order_no"],
            party_id=str(o["party_id"]) if o.get("party_id") else None,
            party_name=o.get("party_name") or "Unknown",
            order_total=float(o.get("order_total") or 0),
        )


def _render_party_statement_tab():
    """Select party → show full account statement."""
    parties = _q("""
        SELECT DISTINCT COALESCE(p.id::text,'') AS id,
               COALESCE(p.party_name, pl.party_name) AS party_name,
               COALESCE(p.payment_mode,'ON_COMPLETION') AS payment_mode
        FROM party_ledger pl
        LEFT JOIN parties p ON p.id = pl.party_id
        ORDER BY party_name
    """)
    if not parties:
        st.info("No party ledger entries yet."); return

    opts = {r["id"]: f"{r['party_name']}  ({r['payment_mode']})" for r in parties}
    sel  = st.selectbox("Select Party", list(opts.keys()),
                        format_func=lambda x: opts[x], key="stmt_party_sel")
    if sel:
        pr = next(r for r in parties if r["id"] == sel)
        render_party_statement(party_id=sel or None, party_name=pr["party_name"])


def _render_payment_history():
    """All payments with filters."""
    c1, c2, c3 = st.columns([2, 1.5, 1.5])
    with c1: search  = st.text_input("Search party / PMT no", key="ph_search",
                                      placeholder="Party name or PMT/…", label_visibility="collapsed")
    with c2: from_d  = st.date_input("From", value=date.today().replace(day=1), key="ph_from",
                                      label_visibility="collapsed")
    with c3: to_d    = st.date_input("To",   value=date.today(),               key="ph_to",
                                      label_visibility="collapsed")

    where  = ["p.payment_date BETWEEN %(f)s AND %(t)s", "p.is_deleted = FALSE"]
    params = {"f": from_d, "t": to_d}
    if search:
        where.append("(COALESCE(p.party_name, pt.party_name) ILIKE %(s)s OR p.payment_no ILIKE %(s)s)")
        params["s"] = f"%{search}%"

    rows = _q(f"""
        SELECT COALESCE(p.payment_no, p.id::text) AS payment_no,
               p.payment_date, p.payment_mode,
               p.amount, COALESCE(p.payment_type,'RECEIPT') AS payment_type,
               p.reference_no, p.remarks,
               COALESCE(p.party_name, pt.party_name, 'Unknown') AS party_name,
               c.challan_no, i.invoice_no
        FROM payments p
        LEFT JOIN parties  pt ON pt.id = p.party_id
        LEFT JOIN challans c  ON c.id  = p.challan_id
        LEFT JOIN invoices i  ON i.id  = p.invoice_id
        WHERE {' AND '.join(where)}
        ORDER BY p.payment_date DESC, p.created_at DESC
        LIMIT 200
    """, params)

    if not rows:
        st.info("No payments found."); return

    total = sum(float(r.get("amount") or 0) for r in rows)
    st.markdown(f"<div style='color:#10b981;font-size:0.85rem;font-weight:700;margin-bottom:8px'>Total collected: {_fc(total)} across {len(rows)} payments</div>", unsafe_allow_html=True)

    trows = ""
    for r in rows:
        mcolor = {"CASH":"#10b981","UPI":"#8b5cf6","NEFT":"#0ea5e9",
                  "RTGS":"#0ea5e9","CHEQUE":"#64748b","CARD":"#a855f7"}.get(
                  str(r.get("payment_mode") or "CASH").upper(), "#64748b")
        ref_doc = r.get("challan_no") or r.get("invoice_no") or "—"
        trows += f"""<tr>
          <td style='padding:5px 8px;color:#38bdf8;font-family:monospace;font-size:0.72rem'>{r['payment_no']}</td>
          <td style='padding:5px 8px;color:#94a3b8'>{_fd(r.get('payment_date'))}</td>
          <td style='padding:5px 8px;color:#e2e8f0;font-weight:600'>{r['party_name']}</td>
          <td style='padding:5px 8px'><span style='background:{mcolor}22;color:{mcolor};padding:2px 8px;border-radius:8px;font-size:0.62rem;font-weight:700'>{r.get('payment_mode','')}</span></td>
          <td style='padding:5px 8px;text-align:right;color:#10b981;font-weight:700;font-family:monospace'>{_fc(r.get('amount',0))}</td>
          <td style='padding:5px 8px;color:#64748b;font-family:monospace;font-size:0.7rem'>{ref_doc}</td>
          <td style='padding:5px 8px;color:#475569;font-size:0.68rem'>{r.get('reference_no') or r.get('remarks') or '—'}</td>
        </tr>"""

    st.markdown(f"""
    <div style='background:#0a0f1a;border-radius:8px;overflow:hidden'>
    <table style='width:100%;border-collapse:collapse;font-size:0.75rem'>
      <thead><tr style='background:#0f172a;border-bottom:1px solid #1e293b'>
        <th style='color:#475569;padding:7px 8px;text-align:left'>PMT No</th>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Date</th>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Party</th>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Mode</th>
        <th style='color:#475569;padding:7px 8px;text-align:right'>Amount</th>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Against</th>
        <th style='color:#475569;padding:7px 8px;text-align:left'>Reference</th>
      </tr></thead>
      <tbody>{trows}</tbody>
    </table></div>
    """, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# BACKOFFICE PAYMENT PROVISIONING
# Full payment management for an order — view, edit, void, re-record
# Handles: advance, challan payments, invoice payments, order corrections
# ═══════════════════════════════════════════════════════════════════════════

def render_payment_provisioning(order: dict, all_lines: list):
    """
    Complete payment management panel for backoffice order detail.
    Replaces the simple advance panel — shows everything and allows full editing.
    """
    # backoffice loader uses "order_id" key; retail uses "id"
    order_id   = str(order.get("id") or order.get("order_id") or "")
    order_no   = str(order.get("order_no") or "")
    order_type = str(order.get("order_type") or "RETAIL").upper()
    party_id   = str(order.get("party_id") or "") or None
    party_name = str(order.get("party_name") or order.get("patient_name") or "")

    # Safety: if order_id looks like an order_no (PO-xxx) not a UUID,
    # resolve via DB to get the real UUID
    if order_id and (not '-' in order_id[8:] or len(order_id) != 36):
        try:
            from modules.sql_adapter import run_query as _rq_oid
            _rows = _rq_oid("SELECT id::text FROM orders WHERE order_no=%(n)s LIMIT 1",
                            {"n": order_id})
            if _rows:
                order_id = str(_rows[0]["id"])
        except Exception:
            pass  # keep original - will fail gracefully in queries

    # Resolve payment mode
    pmode = str(order.get("payment_mode") or "").upper()
    if not pmode and party_id:
        _pr = _q("SELECT payment_mode, credit_days FROM parties WHERE id=%(id)s", {"id": party_id})
        pmode = str((_pr[0].get("payment_mode") if _pr else None) or "").upper()
    if not pmode:
        pmode = "ADVANCE_BALANCE" if order_type == "RETAIL" else "ON_COMPLETION"

    # Compute order total from lines
    order_total = _compute_order_total(all_lines, order_type, order_id)

    # Load all payments for this order (advances + challan + invoice)
    all_payments = _load_all_order_payments(order_id, order_no)

    # Load linked challans + invoices
    linked_docs = _load_linked_docs(order_id, order_no)

    st.markdown("---")
    st.markdown(
        "<div style='color:#94a3b8;font-size:0.72rem;font-weight:700;"
        "letter-spacing:.09em;text-transform:uppercase;margin-bottom:10px'>"
        "💰 Payment Management</div>",
        unsafe_allow_html=True
    )

    # ── Payment status summary strip ─────────────────────────────────────
    adv_total  = sum(float(p["amount"]) for p in all_payments if str(p.get("payment_type","")).upper() == "ADVANCE")
    recv_total = sum(float(p["amount"]) for p in all_payments if str(p.get("payment_type","")).upper() != "ADVANCE")
    total_paid = adv_total + recv_total
    balance    = round(order_total - total_paid, 2)
    excess     = round(max(total_paid - order_total, 0), 2)
    fully_paid = balance <= 0.50

    _MODE_COLOR = {
        "ADVANCE_BALANCE": "#8b5cf6",
        "PRE_PAYMENT":     "#3b82f6",
        "ON_COMPLETION":   "#10b981",
        "ON_ACCOUNT":      "#f59e0b",
    }
    _mc = _MODE_COLOR.get(pmode, "#64748b")

    st.markdown(f"""
    <div style='display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:14px'>
      <div style='{_CARD};border-top:3px solid {_mc}'>
        <div style='{_HDR}'>Mode</div>
        <div style='color:{_mc};font-size:0.82rem;font-weight:700;margin-top:4px'>{pmode.replace("_"," ")}</div>
      </div>
      <div style='{_CARD};border-top:3px solid #38bdf8'>
        <div style='{_HDR}'>Order Total</div>
        <div style='{_VAL};font-size:1rem'>{_fc(order_total)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid #8b5cf6'>
        <div style='{_HDR}'>Advance</div>
        <div style='{_VAL};font-size:1rem;color:#8b5cf6'>{_fc(adv_total)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid #10b981'>
        <div style='{_HDR}'>Received</div>
        <div style='{_VAL};font-size:1rem;color:#10b981'>{_fc(recv_total)}</div>
      </div>
      <div style='{_CARD};border-top:3px solid {"#f59e0b" if excess > 0 else ("#10b981" if fully_paid else "#ef4444")}'>
        <div style='{_HDR}'>{"Excess Received" if excess > 0 else ("✅ Settled" if fully_paid else "Balance Due")}</div>
        <div style='{_VAL};font-size:1rem;color:{"#f59e0b" if excess > 0 else ("#10b981" if fully_paid else "#ef4444")}'>{_fc(excess if excess > 0 else max(balance,0))}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)
    if excess > 0:
        st.info(
            f"Customer credit / excess received: {_fc(excess)}. "
            "Keep it on account or refund/transfer it from Payment Correction."
        )

    # ── Sub-tabs: Payments | Challans/Invoices | Edit/Correct ────────────
    _pt1, _pt2, _pt3 = st.tabs(["📜 Payment Ledger", "🧾 Linked Documents", "✏️ Edit / Correct"])

    with _pt1:
        _render_payment_ledger(all_payments, order_id, party_id, party_name,
                                order_total, total_paid, pmode)

    with _pt2:
        _render_linked_documents(linked_docs, party_id, party_name, pmode)

    with _pt3:
        _render_payment_correction(
            order=order, all_lines=all_lines, all_payments=all_payments,
            order_id=order_id, order_no=order_no,
            party_id=party_id, party_name=party_name,
            order_total=order_total, pmode=pmode,
        )


def _fetch_service_charges_total(order_id: str) -> float:
    """Return sum of all service charges (fitting/colouring/courier) for this order."""
    if not order_id:
        return 0.0
    try:
        from modules.backoffice.order_charges_panel import fetch_charges
        charges = fetch_charges(order_id) or []
        return round(sum(float(c.get("total_amount") or 0) for c in charges), 2)
    except Exception:
        return 0.0


def _compute_order_total(all_lines: list, order_type: str,
                         order_id: str = "") -> float:
    """
    Compute true order total = lens lines + service charges.

    Uses compute_line_gst from price_qty_governor as single source of truth.
    Handles retail (GST inclusive) and wholesale (GST exclusive) correctly.

    Prefer stored billing_total (set by backoffice_logic.update_line_billing)
    which is already GST-correct. Fall back to recomputing via governor.
    """
    if not all_lines:
        return 0.0

    try:
        from modules.core.price_qty_governor import compute_line_gst
    except ImportError:
        compute_line_gst = None

    total = 0.0
    for l in all_lines:
        if l.get("is_deleted"):
            continue
        up  = float(l.get("unit_price") or 0)
        qty = int(l.get("billing_qty") or l.get("quantity") or 0)
        gst = float(l.get("gst_percent") or 0)
        ot  = str(l.get("order_type") or order_type or "RETAIL").upper()
        if up > 0 and qty > 0 and compute_line_gst:
            # Always recompute — billing_total is a cache field that may be
            # base-only (without GST). compute_line_gst is the single source of truth.
            total += compute_line_gst(up, qty, gst, ot)["grand_total"]
        elif up > 0 and qty > 0:
            # Fallback without governor
            total += up * qty if ot == "RETAIL" else up * qty * (1 + gst / 100)

    # Add service charges
    if order_id:
        total += _fetch_service_charges_total(order_id)
    return round(total, 2)


def _load_all_order_payments(order_id: str, order_no: str) -> list:
    # Guard: empty id would cause ::uuid cast error → silent empty result
    if not order_id or not order_id.strip():
        return []

    # Try UUID match first; fallback to order_no text search for safety
    rows = _q("""
        SELECT p.id, COALESCE(p.payment_no, p.id::text) AS payment_no,
               p.payment_date, p.payment_mode, p.amount,
               COALESCE(p.payment_type,'RECEIPT') AS payment_type,
               p.reference_no, p.remarks,
               p.challan_id, p.invoice_id,
               COALESCE(p.is_deleted, FALSE) AS is_deleted,
               c.challan_no, i.invoice_no
        FROM payments p
        LEFT JOIN challans c ON c.id = p.challan_id
        LEFT JOIN invoices i ON i.id = p.invoice_id
        WHERE (
            -- advance recorded at punch time against the order UUID
            (p.advance_for_order_id IS NOT NULL
             AND p.advance_for_order_id::text = %(oid)s)
            -- direct payment against order
            OR (p.order_id IS NOT NULL
                AND p.order_id::text = %(oid)s)
            -- payment against a challan that contains this order
            OR p.challan_id IN (
                SELECT id FROM challans
                WHERE %(oid)s = ANY(order_ids)
                   OR %(ono)s = ANY(order_ids)
            )
            -- payment against an invoice that contains this order
            OR p.invoice_id IN (
                SELECT id FROM invoices
                WHERE %(oid)s = ANY(order_ids)
                   OR %(ono)s = ANY(order_ids)
            )
        )
        ORDER BY p.payment_date ASC, p.created_at ASC
    """, {"oid": order_id, "ono": order_no or ""})
    return rows


def _load_linked_docs(order_id: str, order_no: str) -> dict:
    challans = _q("""
        SELECT c.id, c.challan_no, c.challan_date, c.status,
               c.grand_total, c.amount_paid, c.balance_due,
               c.payment_complete
        FROM challans c
        WHERE %(oid)s::text = ANY(c.order_ids)
          AND COALESCE(c.is_deleted, FALSE) = FALSE
        ORDER BY c.created_at DESC
    """, {"oid": order_id})

    invoices = _q("""
        SELECT i.id, i.invoice_no, i.invoice_date, i.status,
               i.payment_status, i.grand_total, i.amount_paid, i.balance_due
        FROM invoices i
        WHERE %(oid)s::text = ANY(i.order_ids)
          AND COALESCE(i.is_deleted, FALSE) = FALSE
        ORDER BY i.created_at DESC
    """, {"oid": order_id})

    return {"challans": challans, "invoices": invoices}


def _render_payment_ledger(all_payments, order_id, party_id, party_name,
                             order_total, total_paid, pmode):
    active = [p for p in all_payments if not p.get("is_deleted")]

    if not active:
        st.info("No payments recorded yet for this order.")
    else:
        rows = ""
        for p in active:
            mc = {"CASH":"#10b981","UPI":"#8b5cf6","NEFT":"#0ea5e9",
                  "RTGS":"#0ea5e9","CHEQUE":"#64748b","CARD":"#a855f7"}.get(
                  str(p.get("payment_mode","")).upper(), "#64748b")
            pt = str(p.get("payment_type","RECEIPT")).upper()
            pt_color = "#8b5cf6" if pt == "ADVANCE" else "#10b981"
            _against_parts = [x for x in (p.get("challan_no"), p.get("invoice_no")) if x]
            against = " / ".join(_against_parts) if _against_parts else "Direct"
            rows += f"""<tr>
              <td style='padding:5px 8px;color:#38bdf8;font-family:monospace;font-size:0.7rem'>{p['payment_no']}</td>
              <td style='padding:5px 8px;color:#94a3b8'>{_fd(p.get('payment_date'))}</td>
              <td style='padding:5px 8px'><span style='background:{mc}22;color:{mc};padding:1px 8px;border-radius:8px;font-size:0.62rem;font-weight:700'>{p.get('payment_mode','')}</span></td>
              <td style='padding:5px 8px'><span style='background:{pt_color}22;color:{pt_color};padding:1px 8px;border-radius:8px;font-size:0.62rem;font-weight:700'>{pt}</span></td>
              <td style='padding:5px 8px;text-align:right;color:#10b981;font-weight:700;font-family:monospace'>{_fc(p.get('amount',0))}</td>
              <td style='padding:5px 8px;color:#64748b;font-size:0.68rem'>{p.get('reference_no') or p.get('remarks') or '—'}</td>
              <td style='padding:5px 8px;color:#475569;font-size:0.68rem'>{against}</td>
            </tr>"""
        st.markdown(f"""
        <div style='background:#0a0f1a;border-radius:8px;overflow:hidden'>
        <table style='width:100%;border-collapse:collapse;font-size:0.75rem'>
          <thead><tr style='background:#0f172a;border-bottom:1px solid #1e293b'>
            <th style='color:#475569;padding:6px 8px;text-align:left'>PMT No</th>
            <th style='color:#475569;padding:6px 8px;text-align:left'>Date</th>
            <th style='color:#475569;padding:6px 8px;text-align:left'>Mode</th>
            <th style='color:#475569;padding:6px 8px;text-align:left'>Type</th>
            <th style='color:#475569;padding:6px 8px;text-align:right'>Amount</th>
            <th style='color:#475569;padding:6px 8px;text-align:left'>Reference</th>
            <th style='color:#475569;padding:6px 8px;text-align:left'>Against</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table></div>
        """, unsafe_allow_html=True)

    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)

    # Quick add payment
    _label = "💰 Add Advance Payment" if pmode in ("ADVANCE_BALANCE","PRE_PAYMENT") else "💰 Record Payment"
    _ptype = "ADVANCE" if pmode in ("ADVANCE_BALANCE","PRE_PAYMENT") else "RECEIPT"
    if order_total > 0 and total_paid >= order_total - 0.50:
        st.success("✅ Fully paid — further receipt is blocked here. Use Payment Correction for refund/transfer.")
    else:
        render_record_payment(
            order_id=order_id, party_id=party_id, party_name=party_name,
            grand_total=order_total, amount_paid=total_paid,
            payment_type=_ptype,
            label=_label,
            key_suffix=f"bo_ledger_{order_id[:8]}",
            context="bo_ledger",
        )


def _render_linked_documents(linked_docs, party_id, party_name, pmode):
    challans = linked_docs.get("challans", [])
    invoices = linked_docs.get("invoices", [])

    if not challans and not invoices:
        st.info("No challans or invoices linked to this order yet.")
        return

    if challans:
        st.markdown("<div style='font-size:0.72rem;color:#64748b;font-weight:700;letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px'>Challans</div>", unsafe_allow_html=True)
        for c in challans:
            _done  = bool(c.get("payment_complete"))
            _bdue  = float(c.get("balance_due") or 0)
            _paid  = float(c.get("amount_paid") or 0)
            _grand = float(c.get("grand_total") or 0)
            _excess = round(max(_paid - _grand, 0), 2)
            _color = "#f59e0b" if _excess > 0 else ("#10b981" if _done else "#f59e0b")
            _status_txt = (
                f"Excess: {_fc(_excess)}"
                if _excess > 0 else
                ("✅ PAID" if _done else f"Balance: {_fc(_bdue)}")
            )
            st.markdown(f"""
            <div style='{_CARD};border-left:3px solid {_color};padding:10px 14px;margin-bottom:6px'>
              <div style='display:flex;justify-content:space-between;align-items:center'>
                <div>
                  <span style='color:#38bdf8;font-family:monospace;font-weight:700'>{c['challan_no']}</span>
                  <span style='color:#64748b;font-size:0.68rem;margin-left:8px'>{_fd(c.get('challan_date'))}</span>
                </div>
                <div style='text-align:right'>
                  <div style='color:#10b981;font-weight:700;font-family:monospace'>{_fc(c.get('grand_total',0))}</div>
                  <div style='color:{_color};font-size:0.68rem'>{_status_txt}</div>
                </div>
              </div>
            </div>""", unsafe_allow_html=True)

            # If retail challan not fully paid — show balance payment here too
            if not _done and pmode == "ADVANCE_BALANCE":
                render_retail_payment_panel(
                    challan_id  = str(c["id"]),
                    challan_no  = c["challan_no"],
                    party_id    = party_id,
                    party_name  = party_name,
                )

    if invoices:
        st.markdown("<div style='font-size:0.72rem;color:#64748b;font-weight:700;letter-spacing:.07em;text-transform:uppercase;margin:10px 0 6px'>Invoices</div>", unsafe_allow_html=True)
        for iv in invoices:
            _pst  = str(iv.get("payment_status","UNPAID")).upper()
            _icolor = {"PAID":"#10b981","PARTIAL":"#f59e0b","UNPAID":"#ef4444"}.get(_pst,"#ef4444")
            st.markdown(f"""
            <div style='{_CARD};border-left:3px solid {_icolor};padding:10px 14px;margin-bottom:6px'>
              <div style='display:flex;justify-content:space-between;align-items:center'>
                <div>
                  <span style='color:#38bdf8;font-family:monospace;font-weight:700'>{iv['invoice_no']}</span>
                  <span style='color:#64748b;font-size:0.68rem;margin-left:8px'>{_fd(iv.get('invoice_date'))}</span>
                </div>
                <div style='text-align:right'>
                  <div style='color:#10b981;font-weight:700;font-family:monospace'>{_fc(iv.get('grand_total',0))}</div>
                  <div><span style='background:{_icolor}22;color:{_icolor};padding:1px 8px;border-radius:8px;font-size:0.62rem;font-weight:700'>{_pst}</span></div>
                </div>
              </div>
            </div>""", unsafe_allow_html=True)

            if _pst != "PAID":
                render_wholesale_payment_panel(
                    invoice_id   = str(iv["id"]),
                    invoice_no   = iv["invoice_no"],
                    party_id     = party_id,
                    party_name   = party_name,
                    payment_mode = pmode,
                    context      = f"bo_docs_{str(iv['id'])[:8]}",
                )


def _render_payment_correction(*, order, all_lines, all_payments,
                                 order_id, order_no, party_id, party_name,
                                 order_total, pmode):
    """
    Full correction panel:
    - Void a wrong payment
    - Edit amount/mode/date on existing payment
    - Add a missing payment
    - Change payment mode on the order
    """
    st.markdown(
        "<div style='background:#1e293b;border:1px solid #ef444433;border-radius:8px;"
        "padding:10px 14px;margin-bottom:12px;color:#f87171;font-size:0.75rem'>"
        "⚠️ <b>Provisioning area</b> — changes here directly update the ledger. "
        "Voided payments are soft-deleted (kept for audit)."
        "</div>",
        unsafe_allow_html=True
    )

    active_pmts = [p for p in all_payments if not p.get("is_deleted")]

    # ── Section 1: Void a payment ────────────────────────────────────────
    if active_pmts:
        with st.expander("🗑️ Void / Remove a Payment", expanded=False):
            opts = {str(p["id"]): f"{p['payment_no']}  ·  {_fc(p.get('amount',0))}  ·  {_fd(p.get('payment_date'))}  [{p.get('payment_type','')}]"
                   for p in active_pmts}
            void_sel = st.selectbox("Select payment to void", list(opts.keys()),
                                    format_func=lambda x: opts[x],
                                    key=f"void_sel_{order_id[:8]}")
            void_reason = st.text_input("Reason for voiding", key=f"void_rsn_{order_id[:8]}",
                                         placeholder="e.g. Wrong amount entered, customer changed mind…")
            if st.button("🗑️ Void This Payment", type="primary",
                         key=f"void_btn_{order_id[:8]}",
                         width='stretch'):
                if not void_reason.strip():
                    st.error("Please enter a reason before voiding.")
                else:
                    ok, err = _tx([(
                        "UPDATE payments SET is_deleted=TRUE, remarks=COALESCE(remarks,'')||%(r)s WHERE id=%(id)s::uuid",
                        {"id": void_sel, "r": f" [VOIDED: {void_reason}]"}
                    )])
                    if ok:
                        st.success("✅ Payment voided — ledger updated")
                        st.rerun()
                    else:
                        st.error(f"Failed: {err}")

    # ── Section 2: Edit a payment ────────────────────────────────────────
    if active_pmts:
        with st.expander("✏️ Edit Payment Details", expanded=False):
            edit_opts = {str(p["id"]): f"{p['payment_no']}  ·  {_fc(p.get('amount',0))}  ·  {p.get('payment_mode','')}"
                        for p in active_pmts}
            edit_sel = st.selectbox("Select payment to edit", list(edit_opts.keys()),
                                     format_func=lambda x: edit_opts[x],
                                     key=f"edit_sel_{order_id[:8]}")
            _ep = next((p for p in active_pmts if str(p["id"]) == edit_sel), {})

            ec1, ec2, ec3 = st.columns(3)
            with ec1:
                new_amt = st.number_input("Amount ₹", min_value=0.01,
                                           value=float(_ep.get("amount") or 0),
                                           key=f"edit_amt_{order_id[:8]}")
            with ec2:
                _methods = PAYMENT_METHODS
                _cur_m   = str(_ep.get("payment_mode") or "CASH").upper()
                new_mode = st.selectbox("Mode", _methods,
                                         index=_methods.index(_cur_m) if _cur_m in _methods else 0,
                                         key=f"edit_mode_{order_id[:8]}")
            with ec3:
                _cur_dt = _ep.get("payment_date")
                if isinstance(_cur_dt, str): _cur_dt = date.fromisoformat(_cur_dt[:10])
                new_date = st.date_input("Date", value=_cur_dt or date.today(),
                                          key=f"edit_date_{order_id[:8]}")

            new_ref = st.text_input("Reference No",
                                     value=str(_ep.get("reference_no") or ""),
                                     key=f"edit_ref_{order_id[:8]}")
            new_rmk = st.text_input("Remarks",
                                     value=str(_ep.get("remarks") or ""),
                                     key=f"edit_rmk_{order_id[:8]}")

            if st.button("💾 Save Changes", type="primary",
                         key=f"edit_save_{order_id[:8]}",
                         width='stretch'):
                from modules.core.date_guard import validate_payment_date
                _ok_dt, _msg_dt = validate_payment_date(
                    new_date,
                    payment_type=str(_ep.get("payment_type") or ""),
                    payment_mode=new_mode,
                    method=new_mode,
                    remarks=new_rmk,
                    reference_no=new_ref,
                )
                if not _ok_dt:
                    st.error(_msg_dt)
                    return
                ok, err = _tx([("""
                    UPDATE payments
                    SET amount=%(amt)s, payment_mode=%(mode)s,
                        payment_date=%(dt)s, reference_no=%(ref)s, remarks=%(rmk)s
                    WHERE id=%(id)s::uuid
                """, {
                    "amt":  new_amt, "mode": new_mode,
                    "dt":   new_date, "ref": new_ref or None,
                    "rmk":  new_rmk or None, "id": edit_sel,
                })])
                if ok:
                    st.success("✅ Payment updated")
                    st.rerun()
                else:
                    st.error(f"Failed: {err}")

    # ── Section 3: Add missing payment ───────────────────────────────────
    with st.expander("➕ Add Missed / Backdated Payment", expanded=False):
        st.caption("Use this if a payment was received but not recorded at the time.")
        _ptype_opts = ["ADVANCE", "RECEIPT"]
        _def_pt     = "ADVANCE" if pmode in ("ADVANCE_BALANCE","PRE_PAYMENT") else "RECEIPT"
        am1, am2, am3, am4 = st.columns([1.5, 1.2, 1.2, 1.2])
        with am1:
            add_amt  = st.number_input("Amount ₹", min_value=0.0, value=0.0, key=f"add_amt_{order_id[:8]}")
        with am2:
            add_mode = st.selectbox("Mode", PAYMENT_METHODS, key=f"add_mode_{order_id[:8]}")
        with am3:
            add_date = st.date_input("Payment Date", value=date.today(), key=f"add_date_{order_id[:8]}")
        with am4:
            add_type = st.selectbox("Type", _ptype_opts,
                                     index=_ptype_opts.index(_def_pt),
                                     key=f"add_type_{order_id[:8]}")
        add_ref = st.text_input("Reference No", key=f"add_ref_{order_id[:8]}")
        add_rmk = st.text_input("Remarks", key=f"add_rmk_{order_id[:8]}",
                                 placeholder="Why this was missed / backdate reason")

        if st.button("➕ Add Payment to Ledger", type="primary",
                     key=f"add_pay_btn_{order_id[:8]}",
                     width='stretch'):
            if add_amt <= 0:
                st.error("Amount must be > 0")
            elif not add_rmk.strip():
                st.error("Add a remark explaining the backdated entry")
            else:
                _submit_payment(
                    order_id    = order_id,
                    party_id    = party_id,
                    party_name  = party_name,
                    amount      = add_amt,
                    method      = add_mode,
                    pay_date    = add_date,
                    ref_no      = add_ref,
                    remarks     = add_rmk,
                    payment_type= add_type,
                    challan_id  = None,
                    invoice_id  = None,
                )

    # ── Section 4: Change payment mode on order ──────────────────────────
    with st.expander("⚙️ Change Payment Mode", expanded=False):
        st.caption("Override the payment mode for this specific order.")
        _all_modes = ["ADVANCE_BALANCE","PRE_PAYMENT","ON_COMPLETION","ON_ACCOUNT"]
        _cur_pmode = pmode if pmode in _all_modes else "ON_COMPLETION"
        _MODE_LABELS = {
            "ADVANCE_BALANCE": "🛍️ Advance + Balance (Retail)",
            "PRE_PAYMENT":     "💳 Pre-Payment (Execute on receipt)",
            "ON_COMPLETION":   "📦 On Completion (Invoice on dispatch)",
            "ON_ACCOUNT":      "📒 On Account (Credit ledger)",
        }
        new_pmode = st.selectbox(
            "Payment Mode", _all_modes,
            index=_all_modes.index(_cur_pmode),
            format_func=lambda x: _MODE_LABELS.get(x, x),
            key=f"pmode_sel_{order_id[:8]}"
        )
        if st.button("💾 Update Payment Mode", key=f"pmode_save_{order_id[:8]}", width='stretch'):
            ok, err = _tx([(
                "UPDATE orders SET payment_mode=%(m)s WHERE id=%(id)s::uuid",
                {"m": new_pmode, "id": order_id}
            )])
            if ok:
                st.success(f"✅ Payment mode updated to {new_pmode}")
                st.rerun()
            else:
                st.error(f"Failed: {err}")

    # ── Audit: show voided payments ──────────────────────────────────────
    voided = [p for p in all_payments if p.get("is_deleted")]
    if voided:
        with st.expander(f"🗃️ Voided Payments ({len(voided)})", expanded=False):
            for p in voided:
                st.markdown(
                    f"<span style='color:#475569;font-size:0.72rem;font-family:monospace'>"
                    f"~~{p['payment_no']}~~ · {_fc(p.get('amount',0))} · {_fd(p.get('payment_date'))} "
                    f"· {p.get('remarks','')}</span>",
                    unsafe_allow_html=True
                )
