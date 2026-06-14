"""
Billing status panel with delete functionality.
"""

from __future__ import annotations
import re
from typing import Tuple, List, Dict
import streamlit as st
from modules.core.business_rules import (
    invoice_requires_full_payment, is_service_line, skip_allocation,
    CHALLAN_HARD_DELETE_ALLOWED, CHALLAN_DELETE_MESSAGE,
    INVOICE_HARD_DELETE_ALLOWED, INVOICE_DELETE_MESSAGE,
)
import datetime
from modules.core.price_qty_governor import (
    normalize_to_pcs_price,
    compute_line_gst,
    check_sync,
    PAIR_TO_PCS,
)

try:
    _cache_data = st.cache_data
except Exception:
    def _cache_data(*dargs, **dkwargs):
        def _decorator(fn):
            return fn
        if dargs and callable(dargs[0]) and len(dargs) == 1 and not dkwargs:
            return dargs[0]
        return _decorator

def _q(sql: str, params: dict = None):
    """Run a read query."""
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {})
    except Exception as e:
        st.error(f"❌ Query error: {e}")
        return []

def _text_id_list(raw) -> List[str]:
    """Normalise DB text[]/UUID arrays into plain strings for payment lookups."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(x).strip().strip('"') for x in raw if str(x or "").strip()]
    if isinstance(raw, str):
        return [
            x.strip().strip('"')
            for x in re.sub(r"[{}]", "", raw).split(",")
            if x.strip()
        ]
    return [str(raw)]

@_cache_data(ttl=5, show_spinner=False)
def _challan_payment_truth(challan_id: str, order_ids=None, order_id: str = "", order_no: str = "") -> Dict[str, float]:
    """Return live challan payment truth, including advances collected against its order(s)."""
    if not challan_id:
        return {"paid": 0.0, "advance": 0.0, "direct": 0.0}
    lookup_ids = _text_id_list(order_ids)
    for val in (order_id, order_no):
        sval = str(val or "").strip()
        if sval and sval not in lookup_ids:
            lookup_ids.append(sval)
    try:
        from modules.sql_adapter import run_query

        inv_rows = run_query(
            "SELECT id::text AS iid, order_ids FROM invoices "
            "WHERE challan_id=%(cid)s::uuid AND COALESCE(is_deleted,FALSE)=FALSE "
            "AND status NOT IN ('VOID','CANCELLED') ORDER BY created_at DESC LIMIT 1",
            {"cid": challan_id},
        ) or []
        if inv_rows:
            _oid = lookup_ids[0] if lookup_ids else ""
            _truth = _invoice_payment_truth(inv_rows[0]["iid"], _oid)
            return {
                "paid": float(_truth.get("paid") or 0),
                "advance": float(_truth.get("advance") or 0),
                "direct": float(_truth.get("direct") or 0),
            }

        ch_rows = run_query("""
            SELECT c.id::text AS id, c.challan_no,
                   COALESCE(c.grand_total, c.total_amount, 0) AS total,
                   COALESCE(c.created_at, c.challan_date::timestamp, NOW()) AS sort_at,
                   c.order_ids
            FROM challans c
            WHERE COALESCE(c.is_deleted,FALSE)=FALSE
              AND c.status NOT IN ('VOID','CANCELLED')
              AND (
                    c.id = %(cid)s::uuid
                 OR c.order_ids::text[] && %(oids)s::text[]
              )
            ORDER BY sort_at, challan_no
        """, {"cid": challan_id, "oids": lookup_ids}) or []

        order_pool = set(lookup_ids)
        for ch in ch_rows:
            for oid in _text_id_list(ch.get("order_ids")):
                if oid:
                    order_pool.add(oid)
        adv_rows = run_query("""
            SELECT COALESCE(SUM(amount),0) AS amt
            FROM payments
            WHERE COALESCE(is_deleted,FALSE)=FALSE
              AND advance_for_order_id::text = ANY(%(oids)s::text[])
              AND (COALESCE(is_advance,FALSE) OR UPPER(COALESCE(payment_type,''))='ADVANCE')
        """, {"oids": list(order_pool)}) or []
        adv_pool = float((adv_rows[0].get("amt") if adv_rows else 0) or 0)

        row = {}
        for ch in ch_rows:
            cid = str(ch.get("id") or "")
            direct_rows = run_query("""
                SELECT COALESCE(SUM(amount),0) AS amt
                FROM payments
                WHERE challan_id=%(cid)s::uuid
                  AND COALESCE(is_deleted,FALSE)=FALSE
                  AND NOT (COALESCE(is_advance,FALSE) OR UPPER(COALESCE(payment_type,''))='ADVANCE')
            """, {"cid": cid}) or []
            direct = float((direct_rows[0].get("amt") if direct_rows else 0) or 0)
            total = float(ch.get("total") or 0)
            use_adv = min(adv_pool, max(total - direct, 0))
            adv_pool = max(adv_pool - use_adv, 0)
            if cid == str(challan_id):
                row = {"paid": direct + use_adv, "advance_paid": use_adv, "direct_paid": direct}
                break
        return {
            "paid": round(float(row.get("paid") or 0), 2),
            "advance": round(float(row.get("advance_paid") or 0), 2),
            "direct": round(float(row.get("direct_paid") or 0), 2),
        }
    except Exception:
        return {"paid": 0.0, "advance": 0.0, "direct": 0.0}

@_cache_data(ttl=5, show_spinner=False)
def _invoice_payment_truth(invoice_id: str, order_id: str = "") -> Dict[str, float]:
    """
    Allocate order advances across part invoices in invoice order.

    The old UI added the full advance to every invoice card, so a first
    frame invoice could consume the advance and the later lens invoice still
    showed fake excess. This returns the advance slice for this invoice only.
    """
    if not invoice_id:
        return {"direct": 0.0, "advance": 0.0, "paid": 0.0, "balance": 0.0, "excess": 0.0, "grand_total": 0.0}
    try:
        from modules.sql_adapter import run_query
        _inv_rows = run_query("""
            SELECT id::text AS id, COALESCE(grand_total,0) AS grand_total,
                   COALESCE(created_at, invoice_date::timestamp, NOW()) AS sort_at,
                   order_ids
            FROM invoices
            WHERE COALESCE(is_deleted,FALSE)=FALSE
              AND status NOT IN ('CANCELLED','VOID')
              AND (
                    id = %(iid)s::uuid
                 OR (%(oid)s != '' AND %(oid)s = ANY(order_ids::text[]))
              )
            ORDER BY sort_at, invoice_no
        """, {"iid": invoice_id, "oid": str(order_id or "")}) or []
        if not _inv_rows:
            return {"direct": 0.0, "advance": 0.0, "paid": 0.0, "balance": 0.0, "excess": 0.0, "grand_total": 0.0}

        _order_ids = set()
        if order_id:
            _order_ids.add(str(order_id))
        for _ir in _inv_rows:
            for _oid in _text_id_list(_ir.get("order_ids")):
                if _oid:
                    _order_ids.add(str(_oid))

        _adv_rows = run_query("""
            SELECT COALESCE(SUM(amount),0) AS amt
            FROM payments
            WHERE COALESCE(is_deleted,FALSE)=FALSE
              AND (
                    COALESCE(is_advance,FALSE)
                 OR UPPER(COALESCE(payment_type,''))='ADVANCE'
              )
              AND advance_for_order_id::text = ANY(%(oids)s::text[])
        """, {"oids": list(_order_ids)}) or []
        _advance_pool = float((_adv_rows[0].get("amt") if _adv_rows else 0) or 0)

        _target = None
        _target_adv = 0.0
        for _ir in _inv_rows:
            _iid = str(_ir.get("id") or "")
            _gt = float(_ir.get("grand_total") or 0)
            _direct_rows = run_query("""
                SELECT COALESCE(SUM(amount),0) AS amt
                FROM payments
                WHERE invoice_id=%(iid)s::uuid
                  AND COALESCE(is_deleted,FALSE)=FALSE
                  AND NOT (
                        COALESCE(is_advance,FALSE)
                     OR UPPER(COALESCE(payment_type,''))='ADVANCE'
                  )
            """, {"iid": _iid}) or []
            _direct = float((_direct_rows[0].get("amt") if _direct_rows else 0) or 0)
            _need_after_direct = max(_gt - _direct, 0)
            _use_adv = min(_advance_pool, _need_after_direct)
            _advance_pool = max(_advance_pool - _use_adv, 0)
            if _iid == str(invoice_id):
                _paid = _direct + _use_adv
                _bal = max(_gt - _paid, 0)
                _excess = max(_advance_pool, 0) if _bal <= 0.50 and _ir is _inv_rows[-1] else 0.0
                _target = {
                    "direct": round(_direct, 2),
                    "advance": round(_use_adv, 2),
                    "paid": round(_paid, 2),
                    "balance": round(_bal, 2),
                    "excess": round(_excess, 2),
                    "grand_total": round(_gt, 2),
                }
        return _target or {"direct": 0.0, "advance": 0.0, "paid": 0.0, "balance": 0.0, "excess": 0.0, "grand_total": 0.0}
    except Exception:
        return {"direct": 0.0, "advance": 0.0, "paid": 0.0, "balance": 0.0, "excess": 0.0, "grand_total": 0.0}


def _clear_payment_truth_cache() -> None:
    """Payment writes must immediately refresh Billing Summary balances."""
    for fn in (_challan_payment_truth, _invoice_payment_truth):
        try:
            fn.clear()
        except Exception:
            pass


def _convert_challan_to_invoice(challan_id: str, order: dict) -> Tuple[bool, str]:
    """Convert a challan to invoice."""
    try:
        from modules.sql_adapter import run_write, run_query

        # ── Fetch challan header ───────────────────────────────────
        ch_rows = run_query("""
            SELECT c.id, c.challan_no, c.party_id, c.order_ids,
                   c.total_amount, c.total_tax, c.grand_total,
                   COALESCE(c.round_off_amount, 0) AS round_off_amount,
                   c.is_partial_billing, c.payment_mode
            FROM challans c
            WHERE c.id = %(cid)s::uuid
        """, {"cid": challan_id})
        if not ch_rows:
            return False, "Challan not found"
        ch = ch_rows[0]

        # ── Fetch challan lines ────────────────────────────────────
        cl_rows = run_query("""
            SELECT cl.id AS cl_id, cl.order_id, cl.order_line_id,
                   cl.product_name, cl.quantity, cl.unit_price,
                   cl.total_price,
                   COALESCE(cl.line_total, cl.total_price, 0) AS line_total,
                   ROUND(COALESCE(cl.line_total, cl.total_price, 0) - COALESCE(cl.total_price, 0), 2) AS tax_amount,
                   cl.eye_side, cl.brand,
                   COALESCE(cl.gst_percent, ol.gst_percent, 0) AS gst_percent
            FROM challan_lines cl
            LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
            WHERE cl.challan_id = %(cid)s::uuid
              AND NOT COALESCE(cl.is_deleted, FALSE)
        """, {"cid": challan_id})

        # ── Create invoice header ───────────────────────────────────
        inv_no = _next_invoice_no()
        # Normalise order_ids: psycopg2 must receive a plain Python list of strings.
        # ARRAY[list::text] wraps the whole list as one element — always wrong here.
        def _norm_order_ids(raw):
            if isinstance(raw, list):
                return raw
            if isinstance(raw, str):
                import re as _re
                return [x.strip().strip('"') for x in _re.sub(r'[{}]','',raw).split(',') if x.strip()]
            return [str(raw)] if raw else []

        run_write("""
            INSERT INTO invoices
                (invoice_no, challan_id, party_id, order_ids,
                 invoice_date, total_amount, total_tax, grand_total,
                 round_off_amount,
                 status, created_by, gst_included)
            VALUES
                (%(inv)s, %(cid)s::uuid, %(pid)s::uuid, %(oid)s::text[],
                 CURRENT_DATE, %(amt)s, %(tax)s, %(gt)s, %(ro)s, 'PENDING',
                 %(by)s, TRUE)
        """, {
            "inv":     inv_no,
            "cid":     challan_id,
            "pid":     ch.get("party_id"),
            "oid":     _norm_order_ids(ch.get("order_ids")),
            "amt":     ch.get("total_amount"),
            "tax":     ch.get("total_tax"),
            "gt":      ch.get("grand_total"),
            "ro":      ch.get("round_off_amount") or 0,
            "by":      _operator(),
        })

        # ── Fetch invoice UUID just created ──────────────────────
        inv_rows = run_query(
            "SELECT id::text FROM invoices WHERE invoice_no = %(n)s LIMIT 1",
            {"n": inv_no}
        )
        if not inv_rows:
            return False, f"Invoice {inv_no} not found after creation"
        inv_uuid = inv_rows[0]["id"]

        # ── Create invoice lines ───────────────────────────────────
        for cl in cl_rows:
            _ol_id = str(cl.get("order_line_id") or "")
            _o_id  = str(cl.get("order_id") or "")
            run_write("""
                INSERT INTO invoice_lines
                    (invoice_id, order_id, order_line_id, product_name,
                     quantity, unit_price, total_price, tax_amount,
                     tax_rate, line_total, eye_side, brand, gst_percent)
                VALUES
                    (%(inv_uuid)s::uuid, %(oid)s::uuid, %(olid)s::uuid, %(pn)s,
                     %(qty)s, %(up)s, %(tp)s, %(tax)s,
                     %(gst)s, %(lt)s, %(eye)s, %(br)s, %(gst)s)
            """, {
                "inv_uuid": inv_uuid,
                "oid":      _o_id,
                "olid":     _ol_id,
                "pn":       cl.get("product_name") or "",
                "qty":      cl.get("quantity") or 0,
                "up":       cl.get("unit_price") or 0,
                "tp":       cl.get("total_price") or 0,
                "tax":      cl.get("tax_amount") or 0,
                "lt":       cl.get("line_total") or cl.get("total_price") or 0,
                "eye":      cl.get("eye_side") or "",
                "br":       cl.get("brand") or "",
                "gst":      cl.get("gst_percent") or 0,
            })

        # ── Update challan status ───────────────────────────────────
        run_write("""
            UPDATE challans 
            SET status = 'INVOICED', 
                updated_at = NOW()
            WHERE id = %(cid)s::uuid
        """, {"cid": challan_id})

        try:
            from modules.db.advance_allocator import allocate_order_advance
            for _oid in _norm_order_ids(ch.get("order_ids")):
                if str(_oid or "").strip():
                    allocate_order_advance(str(_oid))
        except Exception:
            pass

        return True, f"✅ Invoice {inv_no} created from challan {ch.get('challan_no')}"

    except Exception as e:
        return False, f"❌ Invoice creation failed: {str(e)}"


def _next_invoice_no() -> str:
    """Generate next sequential invoice number via central registry.

    REPLACED: old MAX(CAST(...)) implementation used a separate format (IN/000001),
    bypassed the registry entirely, and had race conditions under concurrency.
    Now uses alloc_doc_number() — same registry as all other document types.
    """
    try:
        from modules.db.order_number_registry import alloc_doc_number
        return alloc_doc_number("INVOICE")
    except Exception:
        import uuid as _u, datetime as _dt
        return f"INV/{_dt.date.today().strftime('%Y%m%d')}/{_u.uuid4().hex[:6].upper()}"


def _operator() -> str:
    """Get current operator name."""
    try:
        from modules.security.roles import current_user_name
        u = current_user_name()
        return u if isinstance(u, str) else getattr(u, "name", "backoffice")
    except Exception:
        return "backoffice"

def _delete_challan(challan_id: str, challan_no: str, reason: str = "") -> Tuple[bool, str]:
    """Placeholder — challan deletion disabled. Use Credit Notes instead."""
    return False, "Challans cannot be deleted. Issue a Credit Note against the invoice instead."

def _delete_invoice(invoice_id: str, invoice_no: str, reason: str = "") -> Tuple[bool, str]:
    """Placeholder — invoice deletion disabled. Use Credit Notes instead."""
    return False, "Invoices cannot be deleted. Use Credit & Debit Notes module instead."


def _operator():
    try:
        from modules.security.roles import current_user_name
        u = current_user_name()
        return u if isinstance(u, str) else getattr(u, "name", "backoffice")
    except Exception:
        return "backoffice"


def _correct_line_total(line: dict, order_type: str) -> float:
    """
    Returns the correct grand total for a line.
    Wholesale lines store taxable/net in order_lines, while some service
    rows store GST-inclusive totals. Always derive from unit, discount and
    stored GST so Billing Status matches challan_lines.
    """
    try:
        up  = float(line.get("unit_price") or 0)
        qty = int(line.get("billing_qty") or line.get("quantity") or 0)
        gst = float(line.get("gst_percent") or 0)
        disc = float(line.get("discount_amount") or 0)
        stored_gst = float(line.get("gst_amount") or 0)
        ot  = str(order_type or "RETAIL").upper()
        gross = round(up * qty, 2)
        if ot == "RETAIL":
            return round(max(gross - disc, 0), 2)
        taxable = round(max(gross - disc, 0), 2)
        gst_amt = stored_gst if stored_gst > 0 else round(taxable * gst / 100, 2)
        return round(taxable + gst_amt, 2)
    except Exception:
        pass
    return float(line.get("line_total") or line.get("total_price") or line.get("billing_total") or 0)


def render_billing_status_panel(order, all_lines, actions_enabled: bool = True):
    """Main billing status panel rendering function."""
    try:
        from modules.sql_adapter import run_query, run_write, resolve_order_uuid
        import streamlit as st

        # ── Consultation orders: fee-only billing, no product lines ──────
        if str(order.get("order_type","")).upper() == "CONSULTATION":
            _render_consultation_billing(order, run_query, run_write)
            return

        order_id  = resolve_order_uuid(order.get("id") or order.get("order_no")) or ""
        order_no  = str(order.get("order_no") or "")
        party_id  = order.get("party_id") or None
        _party_name_bsp = str(
            order.get("patient_name")
            or order.get("party_name")
            or order.get("customer_name")
            or "Customer"
        ).strip()
        otype     = (order.get("order_type") or "RETAIL").upper()
        operator  = _operator()

        _UUID_PAT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
        if not order_id or not _UUID_PAT.match(order_id):
            st.info("No order selected.")
            return

        # ── Order lock check ────────────────────────────────────────────
        # Once fully billed, order is locked — show banner, no new challan
        try:
            _lock_rows = run_query(
                "SELECT COALESCE(is_locked, FALSE) AS locked FROM orders WHERE id=%(oid)s::uuid LIMIT 1",
                {"oid": order_id}
            )
            _order_is_locked = bool((_lock_rows[0].get("locked") if _lock_rows else False))
        except Exception:
            _order_is_locked = False
        if _order_is_locked:
            _ls  = str(order.get("status","")).upper()
            try:
                _has_invoice_lock = bool(run_query("""
                    SELECT 1
                    FROM invoices i
                    WHERE COALESCE(i.is_deleted, FALSE) = FALSE
                      AND i.status NOT IN ('CANCELLED','VOID')
                      AND (
                            i.order_ids::text[] @> ARRAY[%(oid)s::text]
                         OR i.order_ids::text[] @> ARRAY[%(ono)s::text]
                      )
                    LIMIT 1
                """, {"oid": order_id, "ono": order_no}))
            except Exception:
                _has_invoice_lock = False
            _is_invoice_locked = _has_invoice_lock or _ls in ("INVOICED", "INVOICED_BILLED", "BILLED")
            _lbg = "#052e16" if _is_invoice_locked else "#0c1a3a"
            _lbd = "#22c55e" if _is_invoice_locked else "#3b82f6"
            _lic = "🔒" if _is_invoice_locked else "📋"
            _lmsg = (
                "Fully invoiced — no further edits allowed."
                if _is_invoice_locked else
                "Challaned — awaiting invoice conversion."
            )
            st.markdown(
                f"<div style='background:{_lbg};border:1px solid {_lbd};"
                f"border-radius:8px;padding:10px 16px;margin-bottom:12px'>"
                f"<span style='color:{_lbd};font-weight:700'>{_lic} Order Locked</span>"
                f"<span style='color:{_lbd};font-size:0.82rem;margin-left:8px;opacity:0.8'>"
                f"{_lmsg}</span></div>",
                unsafe_allow_html=True
            )
        
        # ── Fetch existing challans ─────────────────────────────────────
        challans = run_query("""
            SELECT c.id::text AS challan_id, c.challan_no,
                   c.status, c.grand_total, c.created_at,
                   c.order_ids,
                   COALESCE(c.amount_paid, 0) AS amount_paid,
                   COALESCE(c.advance_applied, 0) AS advance_applied,
                   COALESCE(c.balance_due, c.grand_total, 0) AS balance_due,
                   COALESCE(c.payment_complete, FALSE) AS payment_complete,
                   c.is_partial_billing,
                   c.original_order_info,
                   (SELECT COUNT(*) FROM challan_lines cl 
                    WHERE cl.challan_id = c.id 
                      AND NOT COALESCE(cl.is_deleted, FALSE)) AS line_count,
                   (SELECT i.invoice_no FROM invoices i 
                    WHERE i.challan_id = c.id 
                      AND NOT COALESCE(i.is_deleted, FALSE)
                      AND i.status NOT IN ('CANCELLED','VOID')
                      LIMIT 1) AS invoice_no
            FROM challans c
            WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text]
                OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
            ORDER BY c.created_at DESC
            LIMIT 20
        """, {"oid": order_id, "ono": order_no})
        _zero_line_challans = []
        _visible_challans = []
        for _ch0 in (challans or []):
            _ch0_status = str(_ch0.get("status") or "").upper()
            _ch0_lines = int(_ch0.get("line_count") or 0)
            _ch0_total = float(_ch0.get("grand_total") or 0)
            if (
                _ch0_status not in ("VOID", "CANCELLED", "DELETED")
                and _ch0_lines == 0
                and abs(_ch0_total) < 0.01
            ):
                _zero_line_challans.append(_ch0)
                continue
            _visible_challans.append(_ch0)
        if _zero_line_challans:
            st.warning(
                f"Hidden {len(_zero_line_challans)} zero-line billing document(s). "
                "They were created before the safety guard and should be voided from admin repair."
            )
            try:
                from modules.security.roles import has_role as _has_role_zero_docs
                _can_void_zero_docs = _has_role_zero_docs("admin", "manager")
            except Exception:
                _can_void_zero_docs = False
            if _can_void_zero_docs:
                _zero_ids = [str(_z.get("challan_id") or "") for _z in _zero_line_challans if _z.get("challan_id")]
                if _zero_ids and st.button(
                    "🧹 Void hidden zero-line billing docs",
                    key=f"void_zero_challans_{order_id}",
                    help="Marks only zero-line ₹0 challans/invoices as VOID. Real challans are not touched.",
                    use_container_width=True,
                ):
                    _voided = run_write("""
                        UPDATE challans c
                        SET status = 'VOID'
                        WHERE c.id = ANY(%(ids)s::uuid[])
                          AND c.status NOT IN ('VOID','CANCELLED','DELETED')
                          AND COALESCE(c.grand_total,0) = 0
                          AND NOT EXISTS (
                              SELECT 1 FROM challan_lines cl
                              WHERE cl.challan_id = c.id
                                AND NOT COALESCE(cl.is_deleted, FALSE)
                          )
                    """, {"ids": _zero_ids})
                    run_write("""
                        UPDATE invoices i
                        SET status = 'VOID'
                        WHERE i.challan_id = ANY(%(ids)s::uuid[])
                          AND i.status NOT IN ('VOID','CANCELLED','DELETED')
                          AND COALESCE(i.grand_total,0) = 0
                    """, {"ids": _zero_ids})
                    if _voided:
                        st.success(f"Voided {len(_zero_ids)} hidden zero-line document(s).")
                        st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                        st.rerun()
        challans = _visible_challans

        # ── Ready lines analysis ─────────────────────────────────────
        # Only lines in ACTIVE (non-void) challans count as "billed"
        # ── Standalone service / courier invoices ───────────────────────────────
        # These have order_ids = '{}' and no challan_id — linked by party_id + order ref in remarks
        _svc_invoices = []
        if party_id:
            try:
                _svc_invoices = run_query("""
                    SELECT i.id::text, i.invoice_no,
                           COALESCE(i.grand_total,0)::numeric AS grand_total,
                           i.status, i.payment_status,
                           i.created_at::date::text AS inv_date,
                           i.remarks,
                           COALESCE(i.balance_due,0)::numeric AS balance_due
                    FROM invoices i
                    WHERE i.party_id = %(pid)s::uuid
                      AND (i.order_ids = '{}'::text[]
                           OR i.order_ids @> ARRAY[%(oid)s::text])
                      AND i.challan_id IS NULL
                      AND COALESCE(i.is_deleted, FALSE) = FALSE
                      AND i.status NOT IN ('CANCELLED','VOID')
                      AND (i.remarks ILIKE %(pat)s
                           OR i.order_ids @> ARRAY[%(oid)s::text])
                    ORDER BY i.created_at DESC
                    LIMIT 10
                """, {
                    "pid": str(party_id),
                    "oid": str(order_id or ""),
                    "pat": f"%{order_no}%",
                }) or []
            except Exception:
                pass

        if _svc_invoices:
            st.markdown("#### 🧾 Service / Courier Invoices")
            for _si in _svc_invoices:
                _si_no   = _si.get("invoice_no","")
                _si_gt   = float(_si.get("grand_total") or 0)
                _si_bal  = float(_si.get("balance_due") or 0)
                _si_st   = _si.get("payment_status","UNPAID")
                _si_rmk  = (_si.get("remarks") or "")[:80]
                _si_date = _si.get("inv_date","")
                _si_paid_badge = (
                    "<span style='background:#052e16;color:#86efac;padding:1px 7px;"
                    "border-radius:8px;font-size:0.68rem'>✅ PAID</span>"
                    if _si_st == "PAID" else
                    f"<span style='background:#1a0a00;color:#fbbf24;padding:1px 7px;"
                    f"border-radius:8px;font-size:0.68rem'>₹{_si_bal:,.2f} due</span>"
                )
                with st.container(border=True):
                    _sc1, _sc2, _sc3 = st.columns([4, 2, 2])
                    with _sc1:
                        st.markdown(
                            f"<div style='font-size:0.82rem;font-weight:700;color:#e2e8f0'>"
                            f"🧾 {_si_no}</div>"
                            f"<div style='font-size:0.70rem;color:#64748b'>{_si_rmk}</div>",
                            unsafe_allow_html=True,
                        )
                    with _sc2:
                        st.markdown(
                            f"<div style='font-size:0.82rem;color:#e2e8f0'>₹{_si_gt:,.2f}</div>"
                            f"<div style='font-size:0.68rem;color:#64748b'>{_si_date}</div>",
                            unsafe_allow_html=True,
                        )
                    with _sc3:
                        st.markdown(_si_paid_badge, unsafe_allow_html=True)
                    # Payment collection for unpaid courier invoices
                    if _si_st != "PAID" and _si_bal > 0.50:
                        with st.expander("💳 Record Payment"):
                            _sp1, _sp2 = st.columns(2)
                            _si_amt  = _sp1.number_input("Amount ₹", 0.0, _si_bal, _si_bal,
                                                          step=10.0, key=f"si_amt_{_si_no}")
                            _si_mode = _sp2.selectbox("Mode",
                                                       ["CASH","UPI","CARD","BANK"],
                                                       key=f"si_mode_{_si_no}")
                            if st.button(f"✅ Record ₹{_si_amt:,.0f}",
                                         key=f"si_pay_{_si_no}", type="primary"):
                                try:
                                    import uuid as _siuu
                                    try:
                                        from modules.db.order_number_registry import alloc_doc_number as _adn_si
                                        _si_pno = _adn_si("PAYMENT")
                                    except Exception:
                                        import datetime as _sidt
                                        _si_pno = f"PAY/{_sidt.date.today().strftime('%y%m')}/{_siuu.uuid4().hex[:5].upper()}"
                                    run_write("""
                                        INSERT INTO payments
                                            (id, payment_no, invoice_id, party_name,
                                             order_id, payment_type, payment_mode,
                                             amount, created_at)
                                        VALUES (%(id)s::uuid, %(pno)s, %(iid)s::uuid,
                                                %(pn)s,
                                                (SELECT NULLIF(order_ids[1],'')::uuid
                                                 FROM invoices WHERE id=%(iid)s::uuid LIMIT 1),
                                                'RECEIPT', %(mode)s, %(amt)s, NOW())
                                    """, {"id": str(_siuu.uuid4()), "pno": _si_pno,
                                          "iid": _si["id"],
                                          "pn": _si.get("party_name") or _party_name_bsp,
                                          "mode": _si_mode, "amt": _si_amt})
                                    run_write("""
                                        UPDATE invoices
                                        SET amount_paid = COALESCE(amount_paid,0)+%(a)s,
                                            balance_due = GREATEST(0,COALESCE(balance_due,0)-%(a)s),
                                            payment_status = CASE
                                                WHEN GREATEST(0,COALESCE(balance_due,0)-%(a)s)<=0.01
                                                     THEN 'PAID' ELSE 'PARTIAL' END,
                                            updated_at = NOW()
                                        WHERE id = %(iid)s::uuid
                                    """, {"a": _si_amt, "iid": _si["id"]})
                                    try:
                                        from modules.db.advance_allocator import allocate_order_advance
                                        # Get order_id from invoice's order_ids
                                        _si_oid_row = run_query(
                                            "SELECT order_ids[1]::text AS oid FROM invoices "
                                            "WHERE id=%(id)s::uuid LIMIT 1",
                                            {"id": _si["id"]}
                                        ) or []
                                        if _si_oid_row and _si_oid_row[0].get("oid"):
                                            allocate_order_advance(_si_oid_row[0]["oid"])
                                    except Exception:
                                        pass
                                    _clear_payment_truth_cache()
                                    st.success("✅ Payment recorded")
                                    st.session_state["bo_show_billing_tab"] = True
                                    st.rerun()
                                except Exception as _spe:
                                    st.error(f"Payment failed: {_spe}")

        st.markdown("---")
        billed_line_ids = set()
        invoiced_line_ids = set()
        if challans:
            for ch in challans:
                ch_id     = str(ch.get("challan_id"))
                ch_status = (ch.get("status") or "").upper()
                if ch_status in ("VOID", "CANCELLED", "DELETED"):
                    continue  # voided challan — lines are back in play
                cl_rows = run_query("""
                    SELECT order_line_id
                    FROM challan_lines
                    WHERE challan_id = %(cid)s::uuid
                      AND NOT COALESCE(is_deleted, FALSE)
                """, {"cid": ch_id})
                for cl in cl_rows:
                    lid = str(cl["order_line_id"])
                    billed_line_ids.add(lid)
                    if ch.get("invoice_no"):
                        invoiced_line_ids.add(lid)

        # ── Refresh line state from DB + job_master ────────────────────────
        # Single combined query — no nested try/except, no silent failures.
        # Fetches: ready_qty, allocated_qty, lens_params, job stage.
        # Syncs allocated_qty = ready_qty when job is done (minimal allocation system).
        import json as _jbs
        try:
            from modules.sql_adapter import resolve_order_uuid as _resolve_order_uuid
            _bs_order_uuid = _resolve_order_uuid(order.get("id") or order.get("order_no")) or ""
        except Exception:
            _bs_order_uuid = ""
        _job_done_stages = {
            "READY_TO_BILL", "READY_FOR_BILLING", "CLOSED",
            "DISPATCHED", "DELIVERED",
        }
        _job_done_lids: set = set()

        if _bs_order_uuid:
            # 1. Fresh order_lines data
            _fresh_bs = run_query("""
                SELECT id::text            AS line_id,
                       COALESCE(ready_qty, 0)     AS ready_qty,
                       COALESCE(allocated_qty, 0) AS allocated_qty,
                       lens_params
                FROM order_lines
                WHERE order_id = %(oid)s::uuid
                  AND COALESCE(is_deleted, FALSE) = FALSE
            """, {"oid": _bs_order_uuid}) or []
            _fresh_bs_map = {r["line_id"]: r for r in _fresh_bs}

            # 2. Job_master state — single query, no nested try
            _job_rows = run_query("""
                SELECT jm.order_line_id::text AS lid,
                       jm.current_stage,
                       COALESCE(jm.is_closed, FALSE) AS is_closed,
                       COALESCE(jm.total_qty, 0) AS total_qty
                FROM job_master jm
                WHERE jm.order_line_id IN (
                    SELECT id FROM order_lines
                    WHERE order_id = %(oid)s::uuid
                      AND COALESCE(is_deleted, FALSE) = FALSE
                )
            """, {"oid": _bs_order_uuid}) or []

            # 3. For each job in done stage: sync ready_qty and allocated_qty
            _writes_needed = []
            for _jr in _job_rows:
                _jlid = str(_jr.get("lid") or "")
                _jstg = str(_jr.get("current_stage") or "").upper()
                _jqty = int(_jr.get("total_qty") or 0)
                if not _jlid or _jqty <= 0:
                    continue
                if bool(_jr.get("is_closed")) or _jstg in _job_done_stages:
                    _job_done_lids.add(_jlid)
                    if _jlid in _fresh_bs_map:
                        _cur_rq  = int(_fresh_bs_map[_jlid].get("ready_qty") or 0)
                        _cur_alq = int(_fresh_bs_map[_jlid].get("allocated_qty") or 0)
                        # Repair ready_qty if not set
                        if _cur_rq < _jqty:
                            _fresh_bs_map[_jlid]["ready_qty"] = _jqty
                            _cur_rq = _jqty
                        # Sync allocated_qty = ready_qty (minimal allocation system)
                        if _cur_alq < _cur_rq:
                            _fresh_bs_map[_jlid]["allocated_qty"] = _cur_rq
                            _writes_needed.append((_jlid, _cur_rq))

            # 4. Batch-write repairs to DB
            for _wlid, _wqty in _writes_needed:
                try:
                    run_write(
                        "UPDATE order_lines "
                        "SET ready_qty = GREATEST(COALESCE(ready_qty,0), %(q)s), "
                        "    allocated_qty = GREATEST(COALESCE(allocated_qty,0), %(q)s) "
                        "WHERE id = %(lid)s::uuid",
                        {"q": _wqty, "lid": _wlid}
                    )
                except Exception:
                    pass

            # 5. Apply fresh values to session line dicts
            for _l in all_lines:
                _fid = str(_l.get("line_id") or _l.get("id") or "")
                if not _fid:
                    continue
                if _fid in _fresh_bs_map:
                    _fr = _fresh_bs_map[_fid]
                    _l["ready_qty"]    = int(_fr.get("ready_qty") or 0)
                    _l["allocated_qty"] = int(_fr.get("allocated_qty") or 0)
                    _lp_bs = _fr.get("lens_params") or {}
                    if isinstance(_lp_bs, str):
                        try: _lp_bs = _jbs.loads(_lp_bs)
                        except: _lp_bs = {}
                    if isinstance(_lp_bs, dict):
                        _l["lens_params"] = _lp_bs
                        if not _l.get("manufacturing_route") and _lp_bs.get("manufacturing_route"):
                            _l["manufacturing_route"] = _lp_bs["manufacturing_route"]
                        if _lp_bs.get("supplier_stage"):
                            _l["supplier_stage"] = _lp_bs.get("supplier_stage")
                        if _lp_bs.get("external_lab_stage"):
                            _l["external_lab_stage"] = _lp_bs.get("external_lab_stage")
                        if not _l.get("surfacing_data") and _lp_bs.get("surfacing_data"):
                            _l["surfacing_data"] = _lp_bs["surfacing_data"]
                # Mark job-done lines — used as override in readiness check
                if _fid in _job_done_lids:
                    _l["_job_production_done"] = True

        # Filter lines
        ready_lines   = []
        pending_lines = []
        billed_lines  = []

        for line in all_lines:
            line_id   = str(line.get("line_id") or "")
            _is_svc   = str(line.get("eye_side","")).upper() in ("SERVICE", "S")
            if line_id in billed_line_ids:
                billed_lines.append(line)
            elif _is_svc:
                _lp_svc_rt = line.get("lens_params") or {}
                if isinstance(_lp_svc_rt, str):
                    try:
                        _lp_svc_rt = _jbs.loads(_lp_svc_rt) if _lp_svc_rt else {}
                    except Exception:
                        _lp_svc_rt = {}
                _lp_svc_rt = _lp_svc_rt if isinstance(_lp_svc_rt, dict) else {}
                _svc_prod_type = str(_lp_svc_rt.get("service_production_type") or "").upper()
                if not _svc_prod_type:
                    if not line.get("allocated_qty"):
                        line["allocated_qty"] = line.get("quantity") or 1
                    ready_lines.append(line)
                elif line.get("_job_production_done"):
                    ready_lines.append(line)
                else:
                    _svc_job_ready = False
                    try:
                        from modules.sql_adapter import run_query as _rq_svc_job_bs
                        _svc_job_rows = _rq_svc_job_bs("""
                            SELECT current_stage, is_closed
                            FROM job_master
                            WHERE order_line_id = %(lid)s::uuid
                            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                            LIMIT 1
                        """, {"lid": line_id}) if line_id else []
                        if _svc_job_rows:
                            _stg = str(_svc_job_rows[0].get("current_stage") or "").upper()
                            _svc_job_ready = bool(_svc_job_rows[0].get("is_closed")) or _stg in (
                                "READY_TO_BILL", "READY_FOR_BILLING", "CLOSED"
                            )
                    except Exception:
                        _svc_job_ready = False
                    if _svc_job_ready:
                        ready_lines.append(line)
                    else:
                        pending_lines.append(line)
            else:
                alloc   = int(line.get("allocated_qty") or 0)
                needed  = int(line.get("billing_qty") or line.get("quantity") or 0)
                ready_q = int(line.get("ready_qty") or 0)
                _lp_rt  = line.get("lens_params") or {}
                _lp_rt  = _lp_rt if isinstance(_lp_rt, dict) else {}
                route   = str(line.get("manufacturing_route") or
                              _lp_rt.get("manufacturing_route") or
                              "").upper()
                batch_s = str(line.get("batch_status") or "").upper()
                batch_n = str(line.get("batch_no") or _lp_rt.get("batch_no") or "").strip()

                # ── Route resolution ────────────────────────────────────────
                # Priority: explicit route → surfacing_data → ready_qty signal → STOCK
                _has_surfacing = bool(line.get("surfacing_data") or _lp_rt.get("surfacing_data"))
                if not route:
                    if _has_surfacing:
                        route = "INHOUSE"
                    elif ready_q >= needed > 0 and not batch_n:
                        route = "INHOUSE"
                    elif alloc > 0 or batch_s == "ALLOCATED" or bool(batch_n):
                        route = "STOCK"

                # ── Service lines: always ready — no production dependency ──────
                _feye = str(line.get("eye_side") or "").upper()
                if (_feye in ("S","SERVICE","O")
                        or bool(line.get("is_service_line"))
                        or "consultation" in str(line.get("product_name") or "").lower()):
                    ready_lines.append(line)
                    continue

                # ── Ultimate override: job in done stage = READY regardless of route ──
                if line.get("_job_production_done"):
                    ready_lines.append(line)
                    continue

                # ── Readiness rules ──────────────────────────────────────────
                _is_inhouse = route in ("INHOUSE",)
                _is_vendor  = route in ("VENDOR", "EXTERNAL_LAB")
                _is_stock   = route in ("STOCK", "") or (not _is_inhouse and not _is_vendor)

                # INHOUSE: job must reach READY_TO_BILL/closed. A saved blank
                # or surfacing_data is not enough to bill.
                if _is_inhouse:
                    if line.get("_job_production_done") or ready_q >= needed:
                        ready_lines.append(line)
                    else:
                        pending_lines.append(line)
                elif _is_vendor:
                    # VENDOR + EXTERNAL_LAB: must go through supplier pipeline.
                    # supplier_stage=null or ORDER_PLACED = not ready
                    # Only final supplier/lab billing stages allow billing.
                    _lp_vnd   = line.get("lens_params") or {}
                    _lp_vnd   = _lp_vnd if isinstance(_lp_vnd, dict) else {}
                    _sup_stg  = str(
                        line.get("supplier_stage")
                        or line.get("external_lab_stage")
                        or _lp_vnd.get("supplier_stage")
                        or _lp_vnd.get("external_lab_stage")
                        or "ORDER_PLACED"
                    ).upper()
                    if _sup_stg in ("READY_FOR_BILLING", "READY_TO_BILL"):
                        ready_lines.append(line)
                    else:
                        pending_lines.append(line)
                elif _is_stock:
                    if alloc >= needed or batch_s == "ALLOCATED" or bool(batch_n):
                        ready_lines.append(line)
                    else:
                        pending_lines.append(line)
                elif needed == 0:
                    ready_lines.append(line)
                else:
                    pending_lines.append(line)
        
        # ── Fetch party billing preference ─────────────────────────────
        # ── Party-level billing settings ────────────────────────────────────
        # Loaded ONCE here and used throughout the billing section.
        #
        # RETAIL orders:
        #   - Always challan first (no direct invoice, ever)
        #   - Always strict payment gate (payment must be collected before invoicing)
        #
        # WHOLESALE orders — driven by party_master fields:
        #   doc_preference  'C' = Challan only (invoice later from Challan Dashboard)
        #                   'I' = Direct invoice (challan + invoice created together)
        #   requires_payment_before_invoice
        #               TRUE  = hard block — cannot invoice until payment collected
        #               FALSE = warning only — can override and invoice
        # ─────────────────────────────────────────────────────────────────────
        _party_doc_pref     = "C"    # default: challan
        _party_strict_gate  = False  # default: warning only
        if party_id and otype != "RETAIL":
            try:
                _p_settings = run_query("""
                    SELECT
                        UPPER(COALESCE(doc_preference, 'C'))                   AS doc_pref,
                        COALESCE(requires_payment_before_invoice, FALSE)        AS strict_gate
                    FROM parties
                    WHERE id = %(pid)s::uuid LIMIT 1
                """, {"pid": str(party_id)}) or []
                if _p_settings:
                    _party_doc_pref    = str(_p_settings[0].get("doc_pref") or "C").upper()[:1]
                    _party_strict_gate = bool(_p_settings[0].get("strict_gate"))
            except Exception:
                # Fallback: try legacy helper
                try:
                    from modules.billing.challan_invoice_manager import get_party_billing_preference
                    _pref = (get_party_billing_preference(str(party_id)) or "C").upper()
                    _party_doc_pref = "I" if _pref in ("I","INVOICE","DIRECT") else "C"
                except Exception:
                    pass

        if otype == "RETAIL":
            _direct_invoice    = False   # retail: challan always
            _party_strict_gate = True    # retail: strict payment gate always
        else:
            _direct_invoice = (_party_doc_pref == "I")   # wholesale: party preference

        # ── Fetch live advance paid for this order ──────────────────────
        # Also includes advances from the linked consultation order (customer_order_no)
        # because consultation fee (₹200) is recorded against the CONS-* order UUID
        # and carries forward as an advance when the patient converts to retail billing.
        _order_advances = 0.0
        try:
            # Get the consultation order UUID (stored in customer_order_no) if any
            _cons_link_rows = run_query("""
                SELECT COALESCE(customer_order_no,'') AS cons_id
                FROM orders WHERE id = %(oid)s::uuid LIMIT 1
            """, {"oid": order_id}) or []
            _cons_uuid = str((_cons_link_rows[0].get("cons_id") or "") if _cons_link_rows else "")
            _is_valid_cons_uuid = (len(_cons_uuid) == 36 and _cons_uuid.count("-") == 4
                                   and not _cons_uuid.startswith("CONS-"))

            # Build uuid list safely — only include consultation UUID if it's a real UUID
            _adv_uuids = [order_id]
            if _is_valid_cons_uuid:
                _adv_uuids.append(_cons_uuid)

            _adv_rows = run_query("""
                SELECT COALESCE(SUM(amount),0) AS tot
                FROM payments
                WHERE advance_for_order_id = ANY(%(uids)s::uuid[])
                  AND payment_type = 'ADVANCE'
                  AND COALESCE(is_deleted,FALSE) = FALSE
            """, {"uids": _adv_uuids})
            _order_advances = float((_adv_rows[0]["tot"] if _adv_rows else 0) or 0)
        except Exception:
            pass

        # ── Sync order status from billing truth on every render ─────────
        try:
            from modules.backoffice.order_status_live import compute_order_status
            _synced_status = compute_order_status(order, write=True)
            # Update in-memory order dict so downstream checks use synced status
            if _synced_status != str(order.get("status","")).upper():
                order = dict(order)
                order["status"] = _synced_status
        except Exception:
            pass

        # ── Header ─────────────────────────────────────────────────────
        st.markdown("#### 🧾 Billing Status")

        # ── Flat line list — punching order ──────────────────────────────
        # Single flat list: R first, L second, S/services last
        # Checkbox on LEFT, live status on RIGHT
        # No grouping — matches how order was punched
        ck_prefix     = f"ready_line_{order_id}"
        checked_lines = []

        # Build line → challan/invoice map for status display
        _line_doc_map = {}  # line_id → {"challan_no": ..., "invoice_no": ...}
        try:
            from modules.sql_adapter import run_query as _rq_ldm
            _ldm_rows = _rq_ldm("""
                SELECT
                    cl.order_line_id::text AS lid,
                    c.challan_no,
                    c.status AS challan_status,
                    (SELECT i.invoice_no FROM invoices i
                     WHERE i.challan_id = c.id
                       AND i.status NOT IN ('CANCELLED','VOID')
                     LIMIT 1) AS invoice_no
                FROM challan_lines cl
                JOIN challans c ON c.id = cl.challan_id
                WHERE c.order_ids::text[] @> ARRAY[%(oid)s::text]
                  AND c.status NOT IN ('VOID','CANCELLED','DELETED')
            """, {"oid": str(order_id)}) or []
            for _ldm in _ldm_rows:
                _line_doc_map[str(_ldm["lid"])] = {
                    "challan_no":     _ldm.get("challan_no") or "",
                    "challan_status": _ldm.get("challan_status") or "",
                    "invoice_no":     _ldm.get("invoice_no") or "",
                }
        except Exception:
            pass

        # ── Service charges ──────────────────────────────────────────────
        _svc_charges = []
        _svc_total   = 0.0
        try:
            from modules.backoffice.order_charges_panel import fetch_charges
            _svc_charges = fetch_charges(str(order_id)) or []
            _svc_total   = sum(float(c.get("total_amount") or 0) for c in _svc_charges)
        except Exception:
            _svc_charges = []

        # Sort all_lines: R → L → O → S
        def _line_sort_key(l):
            e = str(l.get("eye_side") or "").upper()
            return {"R":0,"RIGHT":0,"L":1,"LEFT":1,"O":2,"S":3,"SERVICE":3}.get(e, 2)

        _sorted_lines = sorted(all_lines, key=_line_sort_key)

        # Column headers
        st.markdown(
            "<div style='display:grid;grid-template-columns:2rem 3fr 2fr 2fr;gap:4px;"
            "font-size:0.68rem;color:#475569;font-weight:700;padding:4px 0;"
            "border-bottom:1px solid #1e293b;margin-bottom:4px;text-transform:uppercase'>"
            "<span></span><span>Product</span><span>Amount</span><span style='text-align:right'>Status</span>"
            "</div>",
            unsafe_allow_html=True
        )

        for _fl in _sorted_lines:
            _fid   = str(_fl.get("line_id") or "")
            _feye  = str(_fl.get("eye_side") or "").upper()
            _lp_fl = _fl.get("lens_params") or {}
            if isinstance(_lp_fl, str):
                try:
                    import json as _json_fl
                    _lp_fl = _json_fl.loads(_lp_fl)
                except Exception:
                    _lp_fl = {}
            _is_service_fl = bool(_fl.get("is_service_line")) or _feye in ("S", "SERVICE")
            _fpname = str(
                _fl.get("product_name")
                or _lp_fl.get("service_display_name")
                or _lp_fl.get("service_description")
                or "Service"
            ).split(" | ")[0]
            _fqty  = int(_fl.get("quantity") or _fl.get("billing_qty") or 1)
            _fup   = float(_fl.get("unit_price") or 0)
            _ftotal = _correct_line_total(_fl, otype)
            _feye_lbl = "👁R" if _feye in ("R","RIGHT") else "👁L" if _feye in ("L","LEFT") else "🔧" if _feye in ("S","SERVICE") else "🖼"
            _fpwr = ""
            if not _is_service_fl:
                try:
                    import math as _math_fl
                    _sph = float(_fl["sph"]) if _fl.get("sph") is not None else None
                    if _sph is not None and not _math_fl.isnan(_sph) and not _math_fl.isinf(_sph):
                        _fpwr = f"{_sph:+.2f}"
                        _cyl = float(_fl["cyl"]) if _fl.get("cyl") is not None else 0.0
                        if not _math_fl.isnan(_cyl) and abs(_cyl) > 0.01:
                            _fpwr += f"/{_cyl:+.2f}"
                        _axis = float(_fl["axis"]) if _fl.get("axis") is not None else 0.0
                        if not _math_fl.isnan(_axis) and abs(_axis) > 0.01:
                            _fpwr += f"×{int(_axis)}"
                except Exception:
                    pass

            _is_billed_fl = _fid in billed_line_ids
            _is_ready_fl  = _fl in ready_lines
            _is_pending_fl = _fl in pending_lines

            # Status badge — show challan/invoice ref when billed
            _doc_info  = _line_doc_map.get(_fid, {})
            _fl_ch_no  = _doc_info.get("challan_no", "")
            _fl_inv_no = _doc_info.get("invoice_no", "")

            if _is_billed_fl:
                if _fl_inv_no:
                    _fstatus = (
                        f"<span style='color:#22c55e;font-size:0.68rem;font-weight:700'>"
                        f"🧾 Invoiced</span>"
                        f"<span style='color:#4ade80;font-size:0.65rem;margin-left:4px'>"
                        f"{_fl_inv_no}</span>"
                    )
                elif _fl_ch_no:
                    _fstatus = (
                        f"<span style='color:#3b82f6;font-size:0.68rem;font-weight:700'>"
                        f"📋 Challaned</span>"
                        f"<span style='color:#60a5fa;font-size:0.65rem;margin-left:4px'>"
                        f"{_fl_ch_no}</span>"
                    )
                else:
                    _fstatus = "<span style='color:#22c55e;font-size:0.7rem'>✅ Billed</span>"
                _fcolor = "#22c55e" if _fl_inv_no else "#3b82f6"
            elif _is_ready_fl:
                _fstatus = "<span style='color:#3b82f6;font-size:0.7rem'>🔵 Ready to Bill</span>"
                _fcolor  = "#3b82f6"
            else:
                _fl_route = str((_fl.get("lens_params") or {}).get("manufacturing_route") or
                                _fl.get("manufacturing_route") or "").upper()
                if _fl_route == "INHOUSE":
                    _fstatus = "<span style='color:#f59e0b;font-size:0.7rem'>⏳ In Production</span>"
                elif _fl_route == "EXTERNAL_LAB":
                    _fstatus = "<span style='color:#a855f7;font-size:0.7rem'>🧪 At Lab</span>"
                elif _fl_route == "VENDOR":
                    _fstatus = "<span style='color:#f59e0b;font-size:0.7rem'>🏭 At Supplier</span>"
                else:
                    _fstatus = "<span style='color:#f59e0b;font-size:0.7rem'>⏳ Pending</span>"
                _fcolor  = "#f59e0b"

            _fc1, _fc2, _fc3, _fc4 = st.columns([0.4, 3.5, 2, 2])
            with _fc1:
                if actions_enabled and _is_ready_fl and not _is_billed_fl:
                    _checked_fl = st.checkbox("Include", value=True, key=f"{ck_prefix}_{_fid}",
                                               label_visibility="collapsed")
                    if _checked_fl:
                        checked_lines.append(_fl)
                else:
                    st.markdown(
                        "<span style='color:#1e293b;font-size:1rem'>○</span>",
                        unsafe_allow_html=True
                    )
            with _fc2:
                st.markdown(
                    f"<div style='border-left:3px solid {_fcolor};"
                    f"padding-left:8px;margin:2px 0'>"
                    f"<span style='color:#e2e8f0;font-size:0.82rem;font-weight:600'>"
                    f"{_feye_lbl} {_fpname}</span>"
                    + (f"<span style='color:#64748b;font-size:0.7rem;margin-left:6px'>{_fpwr}</span>" if _fpwr else "")
                    + "</div>",
                    unsafe_allow_html=True
                )
            with _fc3:
                st.markdown(
                    f"<div style='font-size:0.78rem;color:#94a3b8;padding-top:4px'>"
                    f"₹{_fup:,.2f} × {_fqty} = <b style='color:#e2e8f0'>₹{_ftotal:,.2f}</b></div>",
                    unsafe_allow_html=True
                )
            with _fc4:
                st.markdown(
                    f"<div style='text-align:right;padding-top:4px'>{_fstatus}</div>",
                    unsafe_allow_html=True
                )

        # ── Service charges ──────────────────────────────────────────────
        if _svc_charges:
            for _sc in _svc_charges:
                _ico     = {"FITTING":"🔧","COLOURING":"🎨","COURIER":"📦"}.get(
                    (_sc.get("charge_type") or "").upper(), "➕")
                _sc_desc = _sc.get("description") or _sc.get("charge_type") or "Service"
                _sc_amt  = float(_sc.get("total_amount") or 0)
                _sc1, _sc2, _sc3, _sc4 = st.columns([0.4, 3.5, 2, 2])
                with _sc2:
                    st.markdown(
                        f"<div style='border-left:3px solid #a78bfa;padding-left:8px;margin:2px 0'>"
                        f"<span style='color:#c4b5fd;font-size:0.78rem'>{_ico} {_sc_desc}</span></div>",
                        unsafe_allow_html=True
                    )
                with _sc3:
                    st.markdown(
                        f"<div style='font-size:0.78rem;color:#a78bfa;padding-top:4px'>"
                        f"₹{_sc_amt:,.2f}</div>",
                        unsafe_allow_html=True
                    )
                with _sc4:
                    st.markdown(
                        "<div style='text-align:right;padding-top:4px'>"
                        "<span style='color:#a78bfa;font-size:0.7rem'>⚙️ Service</span></div>",
                        unsafe_allow_html=True
                    )

        # ── Action buttons — Make Challan / Make Invoice ───────────────
        if actions_enabled and (checked_lines or (not ready_lines and not pending_lines and not billed_lines)):
            _sel_total = sum(
                _correct_line_total(l, otype)
                for l in checked_lines
            ) + _svc_total
            _n_sel    = len(checked_lines)
            _n_all    = len(ready_lines) + len(pending_lines)
            _is_part  = (_n_sel < _n_all) or bool(pending_lines)

            if checked_lines:
                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #3b82f6;"
                    f"border-radius:8px;padding:8px 14px;margin:8px 0;"
                    f"display:flex;justify-content:space-between;align-items:center'>"
                    f"<span style='color:#93c5fd;font-size:0.8rem'>"
                    f"{_n_sel} line(s) selected"
                    f"{'  ·  <b style="color:#fbbf24">PARTIAL</b>' if _is_part else ''}"
                    f"</span>"
                    f"<span style='color:#60a5fa;font-weight:700'>₹{_sel_total:,.2f}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

            _line_ids = [str(l.get("line_id") or l.get("id") or "") for l in checked_lines]
            _ch_order_ids = [x for x in [order_id, order_no] if x]

            # Compute totals for challan/invoice creation
            _total_base = 0.0
            _total_tax  = 0.0
            for _l in checked_lines:
                _up_r   = float(_l.get("unit_price") or 0)
                _qty_r  = int(_l.get("quantity") or _l.get("billing_qty") or 1)
                _gst_p  = float(_l.get("gst_percent") or 0)
                _disc_r = float(_l.get("discount_amount") or 0)
                _gross_r   = round(_up_r * _qty_r, 2)
                _taxable_r = round(max(_gross_r - _disc_r, 0), 2)
                if otype == "RETAIL":
                    _line_grand_r = _correct_line_total(_l, otype)
                    _line_tax_r = round(max(_line_grand_r - _taxable_r, 0), 2)
                    _total_base += round(_line_grand_r - _line_tax_r, 2)
                    _total_tax  += _line_tax_r
                else:
                    _line_grand_r = _correct_line_total(_l, otype)
                    _line_tax_r = round(max(_line_grand_r - _taxable_r, 0), 2)
                    if _line_tax_r <= 0 and _gst_p > 0:
                        _line_tax_r = round(_taxable_r * _gst_p / 100, 2)
                    _total_base += _taxable_r
                    _total_tax  += _line_tax_r

            _svc_base = sum(float(c.get("amount") or 0) for c in _svc_charges)
            _svc_tax  = sum(float(c.get("gst_amount") or 0) for c in _svc_charges)
            for _sc in _svc_charges:
                _sc["order_id"] = str(order_id)

            _grand_total = round(_total_base + _svc_base + _total_tax + _svc_tax, 2)

            # Payment gate — use billing_category from business_rules (single source)
            _live_balance_for_inv = round(max(_grand_total - _order_advances, 0), 2)
            try:
                from modules.core.business_rules import billing_blocks_invoice, get_billing_category
                from modules.sql_adapter import run_query as _rq_bc
                # Get billing_category from parties table
                _bc_row = _rq_bc(
                    "SELECT COALESCE(billing_category, payment_mode, "
                    + ("'ADVANCE_BALANCE'" if otype == "RETAIL" else "'ON_COMPLETION'")
                    + ") AS bc FROM parties WHERE id=%(pid)s LIMIT 1",
                    {"pid": party_id}
                ) if party_id else []
                _bc = (_bc_row[0]["bc"] if _bc_row else None) or (
                    "ADVANCE_BALANCE" if otype == "RETAIL" else "ON_COMPLETION"
                )
                # credit limit for ON_ACCOUNT
                _cl_row = _rq_bc("SELECT COALESCE(credit_limit,0) AS cl FROM parties WHERE id=%(pid)s LIMIT 1", {"pid": party_id}) if party_id else []
                _credit_limit = float(_cl_row[0]["cl"] if _cl_row else 0)
                _blocked_inv, _inv_block_reason = billing_blocks_invoice(
                    _bc, _order_advances, _grand_total, 0, _credit_limit
                )
                _advance_covers_total = _order_advances >= (_grand_total - 0.50)
                _inv_payment_ok = not _blocked_inv or _advance_covers_total
                if _blocked_inv and not _advance_covers_total:
                    if _party_strict_gate:
                        # Hard block — payment MUST be collected first
                        # Applies to: all RETAIL orders, and WHOLESALE parties
                        # where requires_payment_before_invoice = TRUE
                        st.error(
                            f"🚫 **Invoice blocked** — {_inv_block_reason}. "
                            + ("Collect payment before invoicing this order."
                               if otype == "RETAIL"
                               else "This party requires payment before invoicing.")
                        )
                    else:
                        # Advisory only — wholesale party without strict flag
                        st.warning(f"⚠️ **Payment pending** — {_inv_block_reason}. "
                                   f"You may proceed, but ensure payment is collected.")
            except Exception as _bc_e:
                # Fallback to simple retail check
                _bc = "ADVANCE_BALANCE" if otype == "RETAIL" else "ON_COMPLETION"
                _inv_payment_ok = (
                    otype != "RETAIL"
                    or _live_balance_for_inv <= 0.01
                    or _grand_total == 0
                )

            if checked_lines:
                # ── MARGIN GUARD — check before allowing billing ──────────────
                _margin_alerts = []
                for _ml in checked_lines:
                    _ml_sell  = float(_ml.get("unit_price") or 0) * int(_ml.get("quantity") or _ml.get("billing_qty") or 1)
                    _ml_cost  = float(_ml.get("cost_price") or 0) * int(_ml.get("quantity") or _ml.get("billing_qty") or 1)
                    if _ml_cost > 0 and _ml_sell > 0:
                        _ml_margin = (_ml_sell - _ml_cost) / _ml_sell * 100
                        _ml_name   = str(_ml.get("product_name","")).split("|")[0].strip()
                        if _ml_margin < 0:
                            _margin_alerts.append(("HARD_STOP", _ml_name, _ml_margin, _ml_sell, _ml_cost))
                        elif _ml_margin < 10:
                            _margin_alerts.append(("SOFT_WARN", _ml_name, _ml_margin, _ml_sell, _ml_cost))

                if _margin_alerts:
                    _hard_stops = [a for a in _margin_alerts if a[0] == "HARD_STOP"]
                    _soft_warns = [a for a in _margin_alerts if a[0] == "SOFT_WARN"]
                    if _hard_stops:
                        st.error(
                            "🚫 **Billing blocked** — selling below cost on "
                            + ", ".join(f"**{a[1]}** (margin {a[2]:.1f}%)" for a in _hard_stops)
                            + ". Update price or get manager override."
                        )
                    if _soft_warns:
                        st.warning(
                            "⚠️ Low margin on "
                            + ", ".join(f"**{a[1]}** ({a[2]:.1f}%)" for a in _soft_warns)
                        )
                    # Log margin alerts to DB
                    try:
                        from modules.sql_adapter import run_write as _rw_ma
                        for _at, _pn, _mp, _sp, _cp in _margin_alerts:
                            _rw_ma("""
                                INSERT INTO billing_margin_alerts
                                    (order_id, order_no, product_name, selling_price,
                                     cost_price, margin_pct, alert_type, created_at)
                                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, NOW())
                                ON CONFLICT DO NOTHING
                            """, (str(order_id) if len(str(order_id))==36 else None,
                                  order_no, _pn, _sp, _cp, round(_mp,2), _at))
                    except Exception:
                        pass  # table may not exist yet
                    if _hard_stops:
                        return  # Hard block — do not render challan button

                _b1, _b2 = st.columns([3, 1])

                # ── Make Challan ─────────────────────────────────────────────────
                # RETAIL:     always challan. Invoice is generated later from Challan dashboard.
                # WHOLESALE:  CHALLAN party   → challan here, invoice from dashboard.
                #             DIRECT_INVOICE  → challan is auto-created and immediately
                #               invoiced in one step (handled below).
                with _b1:
                    if otype == "RETAIL":
                        _challan_btn_label = "📋 Make Challan"
                        _challan_btn_help  = "Retail: challan always. Invoice unlocks after full payment."
                    elif _direct_invoice:
                        _challan_btn_label = "📋 Make Challan & Invoice"
                        _challan_btn_help  = "Wholesale INVOICE party — challan + invoice created together."
                    else:
                        _challan_btn_label = "📋 Make Challan"
                        _challan_btn_help  = ("Wholesale CHALLAN party — challan only. "
                                              "Convert to invoice from Challan Dashboard when ready.")
                    if st.button(
                        _challan_btn_label,
                        type="primary",
                        use_container_width=True,
                        key=f"mk_challan_{order_id}",
                        help=_challan_btn_help,
                    ):
                        try:
                            from modules.billing.challan_invoice_manager import create_challan
                            challan_no = create_challan(
                                party_id     = str(party_id or ""),
                                order_ids    = _ch_order_ids,
                                total_amount = round(_total_base + _svc_base, 2),
                                total_tax    = round(_total_tax + _svc_tax, 2),
                                line_ids     = _line_ids,
                                svc_charges  = _svc_charges or None,
                                remarks      = "Partial billing" if _is_part else "",
                            )
                            if challan_no:
                                # Sync order status from billing truth
                                try:
                                    from modules.backoffice.order_status_live import compute_order_status
                                    compute_order_status(order, write=True)
                                except Exception:
                                    from modules.sql_adapter import run_write as _rw_bs
                                    _new_st = "PARTIALLY_BILLED" if _is_part else "BILLED"
                                    _rw_bs(
                                        "UPDATE orders SET status=%(s)s, updated_at=NOW() WHERE id=%(id)s::uuid",
                                        {"s": _new_st, "id": order_id},
                                    )

                                # Wholesale DIRECT_INVOICE: auto-convert challan → invoice
                                if _direct_invoice and _inv_payment_ok:
                                    try:
                                        from modules.billing.challan_invoice_manager import create_invoice
                                        _cid_rows = run_query(
                                            "SELECT id::text FROM challans WHERE challan_no=%(n)s LIMIT 1",
                                            {"n": challan_no}
                                        )
                                        if _cid_rows:
                                            _inv_no = create_invoice(
                                                challan_id   = _cid_rows[0]["id"],
                                                party_id     = str(party_id or ""),
                                                order_ids    = _ch_order_ids,
                                                total_amount = round(_total_base + _svc_base, 2),
                                                total_tax    = round(_total_tax + _svc_tax, 2),
                                            )
                                            if _inv_no:
                                                run_write(
                                                    "UPDATE orders SET status=%(s)s, updated_at=NOW() WHERE id=%(id)s::uuid",
                                                    {"s": "PARTIALLY_BILLED" if _is_part else "BILLED", "id": order_id},
                                                )
                                                # ── Re-allocate advance now that new invoice exists ──
                                                try:
                                                    from modules.db.advance_allocator import allocate_order_advance
                                                    allocate_order_advance(str(order_id or ""))
                                                except Exception:
                                                    pass
                                                st.success(f"✅ Challan {challan_no} → Invoice {_inv_no} created")
                                                st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                                st.rerun()
                                            else:
                                                st.warning(f"✅ Challan {challan_no} created — invoice creation failed, retry from Challan Dashboard")
                                    except Exception as _auto_inv_e:
                                        st.warning(f"✅ Challan {challan_no} created — auto-invoice failed: {_auto_inv_e}")
                                else:
                                    st.success(f"✅ Challan {challan_no} created — convert to invoice from Challan Dashboard")
                                # Lock order ONLY when fully billed (not partial)
                                # Partial billing allows R eye to be billed after L is done
                                try:
                                    if not _is_part:
                                        run_write("""
                                            UPDATE orders
                                            SET is_locked = TRUE
                                            WHERE id = %(oid)s::uuid
                                              AND COALESCE(is_locked, FALSE) = FALSE
                                        """, {"oid": str(order.get("id") or "")})
                                    # Mark purchase_acknowledgements as BILLED for lines on this challan
                                    if checked_lines:
                                        for _cl in checked_lines:
                                            _cl_lid = str(_cl.get("line_id") or _cl.get("id") or "")
                                            if _cl_lid:
                                                try:
                                                    run_write("""
                                                        UPDATE purchase_acknowledgements
                                                        SET billing_status = 'BILLED'
                                                        WHERE order_line_id = %(lid)s::uuid
                                                          AND COALESCE(is_price_locked, FALSE) = TRUE
                                                    """, {"lid": _cl_lid})
                                                except Exception:
                                                    pass
                                except Exception:
                                    pass  # non-fatal — billing already done
                                st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                st.rerun()
                            else:
                                st.error("❌ Challan creation failed — check logs")
                        except Exception as _ce:
                            st.error(f"❌ {_ce}")

                with _b2:
                    st.caption(f"{_n_sel} line(s) · ₹{_sel_total:,.2f}")

        elif actions_enabled and not checked_lines and not ready_lines and not pending_lines and not billed_lines:
            st.info("No lines ready for billing yet. Allocate stock and complete production first.")
        elif actions_enabled and ready_lines and not checked_lines:
            st.caption("☐ Tick lines above to select for billing")
        elif not actions_enabled and ready_lines:
            st.caption("Billing actions are available in the Bill Now panel above.")

        # ── Existing Challans ─────────────────────────────────────────────
        if challans:
            st.markdown("#### 📋 Existing Challans")

            for ch in challans:
                cid        = str(ch.get("challan_id") or "")
                cno        = ch.get("challan_no") or "—"
                cstatus    = (ch.get("status") or "PENDING").upper()
                cgt        = float(ch.get("grand_total") or 0)
                clc        = int(ch.get("line_count") or 0)
                inv_no     = ch.get("invoice_no")
                is_partial = bool(ch.get("is_partial_billing"))
                cdate      = str(ch.get("created_at") or "")[:10]

                _challan_has_invoice = bool(ch.get("invoice_no"))
                _can_recall_challan  = False
                try:
                    from modules.security.roles import has_role as _hr_ch
                    _can_recall_challan = _hr_ch("admin","manager") and not _challan_has_invoice
                except Exception:
                    pass

                if cstatus == "DELETED":
                    status_badge = ("<span style='background:#dc2626;color:#fff;"
                        "border-radius:10px;padding:1px 8px;font-size:0.62rem;"
                        "font-weight:700;margin-left:6px'>DELETED</span>")
                elif cstatus == "INVOICED":
                    status_badge = ("<span style='background:#10b981;color:#fff;"
                        "border-radius:10px;padding:1px 8px;font-size:0.62rem;"
                        "font-weight:700;margin-left:6px'>INVOICED</span>")
                elif cstatus == "VOID":
                    status_badge = ("<span style='background:#6b7280;color:#fff;"
                        "border-radius:10px;padding:1px 8px;font-size:0.62rem;"
                        "font-weight:700;margin-left:6px'>VOID</span>")
                else:
                    status_badge = (f"<span style='background:#f59e0b22;color:#fbbf24;"
                        f"border:1px solid #f59e0b55;border-radius:10px;"
                        f"padding:1px 8px;font-size:0.62rem;font-weight:700;"
                        f"margin-left:6px'>{cstatus}</span>")

                partial_badge = (
                    "<span style='background:#f59e0b22;color:#fbbf24;"
                    "border:1px solid #f59e0b55;border-radius:10px;"
                    "padding:1px 8px;font-size:0.62rem;font-weight:700;"
                    "margin-left:6px'>PARTIAL</span>" if is_partial else ""
                )
                inv_badge = (
                    f"<span style='background:#05966922;color:#34d399;"
                    f"border:1px solid #05966955;border-radius:10px;"
                    f"padding:1px 8px;font-size:0.62rem;font-weight:700;"
                    f"margin-left:6px'>INV {inv_no}</span>" if inv_no else ""
                )

                if _can_recall_challan and cstatus not in ("DELETED","INVOICED","VOID","CANCELLED"):
                    _rch1, _rch2, _rch3, _rch4 = st.columns([4, 2, 2, 2])
                else:
                    _rch1, _rch2, _rch3 = st.columns([5, 3, 2])
                    _rch4 = None

                with _rch1:
                    st.markdown(
                        f"<div style='background:#0f172a;border:1px solid #0d948866;"
                        f"border-radius:8px;padding:10px 14px'>"
                        f"<div style='color:#5eead4;font-weight:700;font-size:0.85rem'>"
                        f"📋 {cno}{status_badge}{partial_badge}{inv_badge}</div>"
                        f"<div style='color:#475569;font-size:0.68rem;margin-top:3px'>"
                        f"{cdate} · {clc} line(s)</div>"
                        f"<div style='color:#10b981;font-size:0.82rem;font-weight:700;"
                        f"margin-top:4px'>₹{cgt:,.2f}</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    # Print challan — builds HTML and opens in browser
                    if st.button(f"🖨 Print Challan", key=f"print_ch_{cid}",
                                 use_container_width=True, help="Print challan"):
                        try:
                            from modules.billing.smart_print import render_smart_challan
                            from modules.printing.print_opener import open_html_print as _open_print_tab_fn
                            _ch_html = render_smart_challan(cno, return_html=True)
                            if _ch_html:
                                _safe_cno = re.sub(r'[/\\:*?"<>|]', '-', str(cno))
                                _open_print_tab_fn(_ch_html, f"challan_{_safe_cno}.html")
                            else:
                                st.error("Challan data not found")
                        except Exception as _pce:
                            st.error(f"Print error: {_pce}")

                with _rch2:
                    if inv_no:
                        st.markdown(
                            f"<div style='background:#0a1f12;border:1px solid #10b98155;"
                            f"border-radius:8px;padding:10px 14px;text-align:center'>"
                            f"<div style='color:#4ade80;font-size:0.75rem;font-weight:700'>"
                            f"✅ Invoiced</div>"
                            f"<div style='color:#475569;font-size:0.65rem'>{inv_no}</div>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                        # Print invoice button
                        if st.button(f"🖨 Print Invoice", key=f"print_inv_{cid}",
                                     use_container_width=True, help=f"Print invoice {inv_no}"):
                            try:
                                from modules.billing.smart_print import render_smart_invoice
                                from modules.printing.print_opener import open_html_print as _open_print_tab_fn
                                _inv_html = render_smart_invoice(inv_no, return_html=True)
                                if _inv_html:
                                    _safe_ino = re.sub(r'[/\\:*?"<>|]', '-', str(inv_no))
                                    _open_print_tab_fn(_inv_html, f"invoice_{_safe_ino}.html")
                                else:
                                    st.error("Invoice not found")
                            except Exception as _pie:
                                st.error(f"Print error: {_pie}")

                        # ── WhatsApp for invoice ──────────────────────────
                        try:
                            from modules.wa_hub import wa_document_attachment, wa_panel, wa_invoice_made
                            from modules.settings.shop_master import get_unit_info
                            _mob_inv = str(order.get("patient_mobile") or order.get("party_mobile") or "")
                            if not _mob_inv and party_id:
                                _mob_row = run_query("SELECT COALESCE(mobile,'') AS m FROM parties WHERE id=%(pid)s::uuid LIMIT 1", {"pid": str(party_id)})
                                _mob_inv = (_mob_row[0]["m"] if _mob_row else "") or ""
                            _inv_detail = run_query("""
                                SELECT COALESCE(i.grand_total,0) AS gt,
                                       COALESCE(i.balance_due, i.grand_total, 0) AS bal
                                FROM invoices i WHERE i.invoice_no=%(n)s LIMIT 1
                            """, {"n": inv_no}) or []
                            if _inv_detail:
                                _sh_inv = get_unit_info("retail" if otype=="RETAIL" else "wholesale")
                                _pname_inv = str(order.get("patient_name") or order.get("party_name") or "")
                                wa_panel(
                                    mobile = _mob_inv,
                                    msg    = wa_invoice_made(
                                        party       = _pname_inv,
                                        invoice_no  = inv_no,
                                        grand_total = float(_inv_detail[0]["gt"]),
                                        balance     = float(_inv_detail[0]["bal"]),
                                        shop_name   = _sh_inv.get("shop_name","DV Optical"),
                                        phone       = _sh_inv.get("shop_phone",""),
                                        upi_id      = _sh_inv.get("shop_upi_id",""),
                                    ),
                                    key         = f"wa_inv_bsp_{cid}",
                                    title       = "📲 WhatsApp — Invoice",
                                    expanded    = False,
                                    party_name  = _pname_inv,
                                    attachments = [wa_document_attachment("invoice", inv_no)],
                                )
                        except Exception:
                            pass

                        # ── Payment collection for invoice ────────────────
                        _inv_id_bsp = run_query(
                            "SELECT id::text AS iid, COALESCE(balance_due,0) AS bal FROM invoices WHERE invoice_no=%(n)s LIMIT 1",
                            {"n": inv_no}
                        ) or []
                        if _inv_id_bsp:
                            _iid = _inv_id_bsp[0]["iid"]
                            _ibal = float(_inv_id_bsp[0]["bal"])
                            _pay_truth = _invoice_payment_truth(_iid, order_id)
                            _inv_adv_paid = float(_pay_truth.get("advance") or 0)
                            _inv_total_paid = float(_pay_truth.get("direct") or 0)
                            _inv_gt_val = float(_pay_truth.get("grand_total") or 0)
                            _effective_paid = float(_pay_truth.get("paid") or 0)
                            _effective_bal = float(_pay_truth.get("balance") or 0)

                            if _effective_bal <= 0.50:
                                # Fully paid — show clear status, no payment button
                                if _inv_adv_paid > 0 and _inv_total_paid == 0:
                                    _excess = float(_pay_truth.get("excess") or 0)
                                    if _excess > 0.50:
                                        st.success(
                                            f"✅ Fully paid via advance  ·  "
                                            f"Advance ₹{_inv_adv_paid:,.2f}  ·  "
                                            f"Invoice ₹{_inv_gt_val:,.2f}  ·  "
                                            f"**Excess credit ₹{_excess:,.2f}**"
                                        )
                                    else:
                                        st.success(f"✅ Fully paid via advance — ₹{_inv_adv_paid:,.2f} received")
                                else:
                                    st.success(f"✅ Invoice fully paid — ₹{_effective_paid:,.2f} received")
                            elif _ibal > 0.50:
                                _part_paid = _effective_paid
                                if _part_paid > 0:
                                    st.info(f"⚡ Partial payment — ₹{_part_paid:,.2f} received, ₹{_effective_bal:,.2f} due")
                                st.markdown(
                                    f"<div style='background:#052e16;border:1px solid #166534;"
                                    f"border-left:3px solid #22c55e;border-radius:6px;"
                                    f"padding:6px 10px;margin-top:4px'>"
                                    f"<span style='color:#86efac;font-size:0.75rem;font-weight:700'>"
                                    f"💳 Balance Due ₹{_effective_bal:,.2f}</span></div>",
                                    unsafe_allow_html=True,
                                )
                                _ipc1, _ipc2 = st.columns(2)
                                _ip_amt = _ipc1.number_input(
                                    "Collect (₹)", min_value=0.0, max_value=_effective_bal,
                                    value=_effective_bal, step=10.0,
                                    key=f"bsp_inv_amt_{cid}", label_visibility="collapsed",
                                )
                                _ip_mode = _ipc2.selectbox(
                                    "Mode", ["CASH","UPI","CARD","BANK","CHEQUE"],
                                    key=f"bsp_inv_mode_{cid}", label_visibility="collapsed",
                                )
                                if st.button(f"✅ Record ₹{_ip_amt:,.0f}",
                                             key=f"bsp_inv_pay_{cid}",
                                             type="primary", use_container_width=True,
                                             disabled=_ip_amt<=0):
                                    import uuid as _uip
                                    try:
                                        from modules.db.order_number_registry import alloc_doc_number as _adn_ip
                                        _ip_pno2 = _adn_ip("PAYMENT")
                                    except Exception:
                                        import datetime as _ipdt
                                        _ip_pno2 = f"PAY/{_ipdt.date.today().strftime('%y%m')}/{_uip.uuid4().hex[:5].upper()}"
                                    _ok_ip = run_write("""
                                        INSERT INTO payments
                                            (id, payment_no, invoice_id, party_id, party_name,
                                             order_id, amount, payment_mode, method,
                                             payment_date, payment_type, created_by)
                                        VALUES
                                            (%(id)s::uuid, %(pno)s, %(iid)s::uuid,
                                             NULLIF(%(pid)s,'')::uuid, %(pn)s,
                                             %(oid)s::uuid,
                                             %(amt)s, %(mode)s, %(mode)s, NOW(), 'RECEIPT', %(by)s)
                                    """, {
                                        "id":   str(_uip.uuid4()),
                                        "pno":  _ip_pno2,
                                        "iid":  _iid,
                                        "pid":  str(party_id or ""),
                                        "pn":   _party_name_bsp,
                                        "oid":  str(order_id or ""),
                                        "amt":  _ip_amt,
                                        "mode": _ip_mode,
                                        "by":   operator,
                                    })
                                    if _ok_ip:
                                        try:
                                            from modules.db.billing_queries import update_invoice_balance
                                            update_invoice_balance(_iid)
                                        except Exception:
                                            pass
                                        # Allocator LAST — overrides any naive advance sum
                                        try:
                                            from modules.db.advance_allocator import allocate_order_advance
                                            allocate_order_advance(str(order_id or ""))
                                        except Exception:
                                            run_write("""
                                                UPDATE invoices SET
                                                    amount_paid = COALESCE(amount_paid,0)+%(a)s,
                                                    balance_due = GREATEST(COALESCE(balance_due,0)-%(a)s,0),
                                                    payment_status = CASE
                                                        WHEN GREATEST(COALESCE(balance_due,0)-%(a)s,0)<=0.50 THEN 'PAID'
                                                        WHEN COALESCE(amount_paid,0)+%(a)s>0 THEN 'PARTIAL'
                                                        ELSE payment_status END,
                                                    updated_at=NOW()
                                                WHERE id=%(iid)s::uuid
                                            """, {"a": _ip_amt, "iid": _iid})
                                        _clear_payment_truth_cache()
                                        st.success(f"✅ ₹{_ip_amt:,.2f} recorded")
                                        st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                        st.rerun()
                            else:
                                try:
                                    _inv_adv_amt = float(_pay_truth.get("advance") or 0)
                                    _inv_gt = float(_pay_truth.get("grand_total") or 0)
                                    _excess = float(_pay_truth.get("excess") or 0)
                                    if _inv_adv_amt > 0 and _excess > 0.50:
                                        st.success(
                                            f"✅ Fully paid via advance  ·  "
                                            f"Advance: ₹{_inv_adv_amt:,.2f}  ·  "
                                            f"Invoice: ₹{_inv_gt:,.2f}  ·  "
                                            f"**Excess credit: ₹{_excess:,.2f}**"
                                        )
                                    elif _inv_adv_amt > 0:
                                        st.success(f"✅ Fully paid via advance  ·  Advance: ₹{_inv_adv_amt:,.2f}  ·  Invoice: ₹{_inv_gt:,.2f}")
                                    else:
                                        st.success("✅ Fully paid")
                                except Exception:
                                    st.success("✅ Fully paid")

                    else:
                        # Challan not yet invoiced — show WhatsApp + payment + admin price edit
                        st.caption(f"Status: {cstatus}")

                        # ── WhatsApp for challan ──────────────────────────
                        try:
                            from modules.wa_hub import wa_document_attachment, wa_panel, wa_challan_made
                            from modules.settings.shop_master import get_unit_info
                            _mob_ch = str(order.get("patient_mobile") or order.get("party_mobile") or "")
                            if not _mob_ch and party_id:
                                _mob_row_ch = run_query("SELECT COALESCE(mobile,'') AS m FROM parties WHERE id=%(pid)s::uuid LIMIT 1", {"pid": str(party_id)})
                                _mob_ch = (_mob_row_ch[0]["m"] if _mob_row_ch else "") or ""
                            _sh_ch = get_unit_info("retail" if otype=="RETAIL" else "wholesale")
                            _pname_ch = str(order.get("patient_name") or order.get("party_name") or "")
                            _order_nos_ch = str(order.get("order_no") or "")
                            wa_panel(
                                mobile = _mob_ch,
                                msg    = wa_challan_made(
                                    party       = _pname_ch,
                                    order_no    = _order_nos_ch,
                                    challan_no  = cno,
                                    grand_total = cgt,
                                    shop_name   = _sh_ch.get("shop_name","DV Optical"),
                                    phone       = _sh_ch.get("shop_phone",""),
                                ),
                                key         = f"wa_ch_bsp_{cid}",
                                title       = "📲 WhatsApp — Challan",
                                expanded    = False,
                                party_name  = _pname_ch,
                                attachments = [wa_document_attachment("challan", cno)],
                            )
                        except Exception:
                            pass

                        # ── Payment collection for challan ────────────────
                        _ch_pay_bsp = _challan_payment_truth(
                            cid,
                            ch.get("order_ids"),
                            order_id=order_id,
                            order_no=order_no,
                        )
                        _ch_paid_bsp = _ch_pay_bsp["paid"]
                        _ch_bal_bsp = round(max(cgt - _ch_paid_bsp, 0), 2)
                        if _ch_bal_bsp > 0.50:
                            st.markdown(
                                f"<div style='background:#0c1a0c;border:1px solid #166534;"
                                f"border-left:3px solid #22c55e;border-radius:6px;"
                                f"padding:6px 10px;margin-top:4px'>"
                                f"<span style='color:#86efac;font-size:0.75rem;font-weight:700'>"
                                f"💳 Balance ₹{_ch_bal_bsp:,.2f}</span></div>",
                                unsafe_allow_html=True,
                            )
                            _cpc1, _cpc2 = st.columns(2)
                            _cp_amt  = _cpc1.number_input(
                                "Collect (₹)", min_value=0.0, max_value=_ch_bal_bsp,
                                value=_ch_bal_bsp, step=10.0,
                                key=f"bsp_ch_amt_{cid}", label_visibility="collapsed",
                            )
                            _cp_mode = _cpc2.selectbox(
                                "Mode", ["CASH","UPI","CARD","BANK","CHEQUE"],
                                key=f"bsp_ch_mode_{cid}", label_visibility="collapsed",
                            )
                            if st.button(f"✅ Record ₹{_cp_amt:,.0f}",
                                         key=f"bsp_ch_pay_{cid}",
                                         type="primary", use_container_width=True,
                                         disabled=_cp_amt<=0):
                                import uuid as _ucp
                                try:
                                    from modules.db.order_number_registry import alloc_doc_number as _adn_cp
                                    _cp_pno = _adn_cp("PAYMENT")
                                except Exception:
                                    import datetime as _cpdt
                                    _cp_pno = f"PAY/{_cpdt.date.today().strftime('%y%m')}/{_ucp.uuid4().hex[:5].upper()}"
                                run_write("""
                                    INSERT INTO payments
                                        (id, payment_no, challan_id, party_id, party_name,
                                         order_id, amount, payment_mode, method,
                                         payment_date, payment_type, created_by)
                                    VALUES
                                        (%(id)s::uuid, %(pno)s, %(cid)s::uuid,
                                         NULLIF(%(pid)s,'')::uuid, %(pn)s,
                                         %(oid)s::uuid,
                                         %(amt)s, %(mode)s, %(mode)s, NOW(), 'RECEIPT', %(by)s)
                                """, {
                                    "id":   str(_ucp.uuid4()),
                                    "pno":  _cp_pno,
                                    "cid":  cid,
                                    "pid":  str(party_id or ""),
                                    "pn":   _party_name_bsp,
                                    "oid":  str(order_id or ""),
                                    "amt":  _cp_amt,
                                    "mode": _cp_mode,
                                    "by":   operator,
                                })
                                try:
                                    from modules.db.billing_queries import update_challan_balance
                                    update_challan_balance(cid)
                                except Exception:
                                    pass
                                # ── Re-allocate advance across all invoices ──
                                try:
                                    from modules.db.advance_allocator import allocate_order_advance
                                    allocate_order_advance(str(order_id or ""))
                                except Exception as _alloc_e:
                                    pass
                                _clear_payment_truth_cache()
                                st.success(f"✅ ₹{_cp_amt:,.2f} recorded")
                                st.session_state["bo_show_billing_tab"] = True  # keep billing tab active
                                st.rerun()
                        else:
                            st.success("✅ Challan paid")

                        # ── Admin price edit on challan lines ─────────────
                        # Only shown to admin/manager on un-invoiced challans
                        _can_edit_price = False
                        try:
                            from modules.security.roles import has_role as _hr_pe
                            _can_edit_price = _hr_pe("admin","manager")
                        except Exception:
                            pass
                        if _can_edit_price and cstatus not in ("INVOICED","VOID","CANCELLED","DELETED"):
                            with st.expander("✏️ Edit Line Prices (Admin)", expanded=False):
                                _cl_edit = run_query("""
                                    SELECT cl.id::text AS cl_id, cl.product_name,
                                           cl.eye_side, cl.quantity,
                                           cl.unit_price, cl.total_price, cl.line_total,
                                           COALESCE(cl.gst_percent, ol.gst_percent, 0) AS gst_percent
                                    FROM challan_lines cl
                                    LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
                                    WHERE cl.challan_id=%(cid)s::uuid
                                      AND NOT COALESCE(cl.is_deleted,FALSE)
                                    ORDER BY cl.id
                                """, {"cid": cid}) or []

                                _price_changed = False
                                for _ce in _cl_edit:
                                    _ce_id  = _ce["cl_id"]
                                    _eye    = str(_ce.get("eye_side") or "")
                                    _pname  = str(_ce.get("product_name") or "")
                                    _qty    = int(_ce.get("quantity") or 1)
                                    _old_up = float(_ce.get("unit_price") or 0)
                                    _pec1, _pec2, _pec3 = st.columns([3, 2, 2])
                                    _pec1.markdown(
                                        f"<div style='font-size:0.78rem;color:#e2e8f0;padding-top:6px'>"
                                        f"{_eye} · {_pname}</div>",
                                        unsafe_allow_html=True
                                    )
                                    _new_up = _pec2.number_input(
                                        "Unit ₹", value=_old_up, min_value=0.0,
                                        step=0.5, format="%.2f",
                                        key=f"bsp_pe_up_{_ce_id}",
                                        label_visibility="collapsed",
                                    )
                                    # Compute new totals
                                    _gst_p_pe = float(_ce.get("gst_percent") or 0)
                                    _new_base = round(_new_up * _qty, 2)
                                    if otype == "RETAIL" and _gst_p_pe > 0:
                                        _new_lt = _new_base
                                    else:
                                        _new_lt = round(_new_base * (1 + _gst_p_pe / 100), 2)
                                    _pec3.markdown(
                                        f"<div style='font-size:0.75rem;color:#10b981;padding-top:6px'>"
                                        f"₹{_new_lt:,.2f}</div>",
                                        unsafe_allow_html=True
                                    )

                                if st.button("💾 Save Price Changes",
                                             key=f"bsp_pe_save_{cid}",
                                             type="primary",
                                             use_container_width=True):
                                    _saved = 0
                                    for _ce in _cl_edit:
                                        _ce_id  = _ce["cl_id"]
                                        _qty    = int(_ce.get("quantity") or 1)
                                        _new_up = float(st.session_state.get(f"bsp_pe_up_{_ce_id}", _ce.get("unit_price") or 0))
                                        _gst_p_pe = float(_ce.get("gst_percent") or 0)
                                        _gross_or_base = round(_new_up * _qty, 2)
                                        if otype == "RETAIL" and _gst_p_pe > 0:
                                            _taxable = round(_gross_or_base * 100 / (100 + _gst_p_pe), 2)
                                            _line_total = _gross_or_base
                                        else:
                                            _taxable = _gross_or_base
                                            _line_total = round(_taxable * (1 + _gst_p_pe / 100), 2)
                                        _tax_amount = round(_line_total - _taxable, 2)
                                        run_write("""
                                            UPDATE challan_lines
                                            SET unit_price = %(up)s,
                                                total_price = %(tp)s,
                                                line_total  = %(lt)s,
                                                gst_percent = %(gst)s,
                                                updated_at  = NOW()
                                            WHERE id = %(cl_id)s::uuid
                                        """, {
                                            "up":    _new_up,
                                            "tp":    _taxable,
                                            "lt":    _line_total,
                                            "gst":   _gst_p_pe,
                                            "cl_id": _ce_id,
                                        })
                                        # Also update order_lines so backoffice stays in sync
                                        run_write("""
                                            UPDATE order_lines ol
                                            SET unit_price = %(up)s,
                                                total_price = %(tp)s,
                                                gst_percent = %(gst)s,
                                                gst_amount = %(tax)s,
                                                billing_total = %(lt)s
                                            FROM challan_lines cl
                                            WHERE cl.id = %(cl_id)s::uuid
                                              AND ol.id = cl.order_line_id
                                        """, {
                                            "up":    _new_up,
                                            "tp":    _taxable,
                                            "gst":   _gst_p_pe,
                                            "tax":   _tax_amount,
                                            "lt":    _line_total,
                                            "cl_id": _ce_id,
                                        })
                                        _saved += 1
                                    # Recompute challan header totals from the corrected snapshot.
                                    run_write("""
                                        UPDATE challans SET
                                            total_amount = COALESCE((
                                                SELECT SUM(COALESCE(total_price, line_total, 0))
                                                FROM challan_lines
                                                WHERE challan_id=%(cid)s::uuid
                                                  AND NOT COALESCE(is_deleted,FALSE)
                                            ), 0),
                                            total_tax = COALESCE((
                                                SELECT SUM(COALESCE(line_total, total_price, 0) - COALESCE(total_price, 0))
                                                FROM challan_lines
                                                WHERE challan_id=%(cid)s::uuid
                                                  AND NOT COALESCE(is_deleted,FALSE)
                                            ), 0),
                                            grand_total = COALESCE((
                                                SELECT SUM(COALESCE(line_total, total_price, 0))
                                                FROM challan_lines
                                                WHERE challan_id=%(cid)s::uuid
                                                  AND NOT COALESCE(is_deleted,FALSE)
                                            ), 0),
                                            updated_at = NOW()
                                        WHERE id = %(cid)s::uuid
                                    """, {"cid": cid})
                                    st.success(f"✅ {_saved} line(s) updated")
                                    st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                    st.rerun()

                if _rch4 is not None:
                    with _rch4:
                        _recall_ch_key = f"_recall_ch_{cid}"
                        if not st.session_state.get(_recall_ch_key):
                            if st.button(
                                "↩️ Recall", key=f"recall_ch_btn_{cid}",
                                use_container_width=True,
                                help="Void challan and recall order to CONFIRMED",
                            ):
                                st.session_state[_recall_ch_key] = True
                                st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                st.rerun()
                        else:
                            st.warning(f"Void **{cno}** and recall to CONFIRMED?")
                            _rcb1, _rcb2 = st.columns(2)
                            with _rcb1:
                                if st.button("✅ Confirm", key=f"recall_ch_yes_{cid}",
                                             type="primary", use_container_width=True):
                                    try:
                                        from modules.sql_adapter import (
                                            run_write as _rw_ch, run_query as _rq_ch
                                        )
                                        _rw_ch(
                                            "UPDATE challans SET status='VOID' WHERE id=%(id)s",
                                            {"id": cid}
                                        )
                                        _ch_lines = _rq_ch("""
                                            SELECT order_line_id, quantity
                                            FROM challan_lines
                                            WHERE challan_id = %(cid)s::uuid
                                              AND NOT COALESCE(is_deleted,FALSE)
                                        """, {"cid": cid}) or []
                                        for _chl in _ch_lines:
                                            _rw_ch("""
                                                UPDATE order_lines
                                                SET billed_qty = GREATEST(0, COALESCE(billed_qty,0) - %(qty)s)
                                                WHERE id = %(lid)s::uuid
                                            """, {
                                                "qty": int(_chl.get("quantity") or 0),
                                                "lid": str(_chl.get("order_line_id") or ""),
                                            })
                                        _rw_ch(
                                            "UPDATE orders SET status='CONFIRMED' WHERE id=%(id)s::uuid",
                                            {"id": order_id}
                                        )
                                        st.success(f"✅ {cno} voided. Order recalled.")
                                        st.session_state.pop(_recall_ch_key, None)
                                        st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                        st.rerun()
                                    except Exception as _rce:
                                        st.error(f"Recall failed: {_rce}")
                            with _rcb2:
                                if st.button("← Cancel", key=f"recall_ch_no_{cid}",
                                             use_container_width=True):
                                    st.session_state.pop(_recall_ch_key, None)
                                    st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                    st.rerun()

                with _rch3:
                    if cstatus not in ("INVOICED", "VOID", "CANCELLED", "DELETED") and not inv_no:
                        inv_btn_key  = f"make_invoice_{cid}"
                        inv_conf_key = f"confirm_invoice_{cid}"

                        # Live payment check against this challan's value.
                        # Includes payments taken at punching/backoffice as order advances.
                        _ch_pay_truth = _challan_payment_truth(
                            cid,
                            ch.get("order_ids"),
                            order_id=order_id,
                            order_no=order_no,
                        )
                        _ch_live_paid = _ch_pay_truth["paid"]
                        _ch_live_balance = round(max(cgt - _ch_live_paid, 0), 2)
                        _ch_inv_ok = (
                            otype != "RETAIL"
                            or _ch_live_balance <= 0.01
                            or cgt == 0
                        )
                        # ── Partial billing advance breakdown display ─────────────────
                        try:
                            _pb_invoices = run_query("""
                                SELECT i.invoice_no,
                                       COALESCE(i.grand_total,0)::numeric AS total,
                                       COALESCE(i.amount_paid,0)::numeric AS paid,
                                       COALESCE(i.balance_due,0)::numeric AS balance,
                                       i.payment_status
                                FROM invoices i
                                WHERE i.order_ids::text[] @> ARRAY[%(oid)s::text]
                                  AND COALESCE(i.is_deleted,FALSE) = FALSE
                                  AND i.status NOT IN ('VOID','CANCELLED')
                                ORDER BY i.created_at ASC
                            """, {"oid": order_id}) or []
                            _pb_adv = run_query("""
                                SELECT COALESCE(SUM(amount),0)::numeric AS total
                                FROM payments
                                WHERE advance_for_order_id = %(oid)s::uuid
                                  AND payment_type = 'ADVANCE'
                                  AND COALESCE(is_deleted,FALSE) = FALSE
                            """, {"oid": order_id}) or []
                            _pb_adv_total = float(_pb_adv[0]["total"] if _pb_adv else 0)

                            if len(_pb_invoices) >= 1 and _pb_adv_total > 0:
                                _adv_rem = _pb_adv_total
                                _html = (
                                    "<div style='background:#0a1628;border:1px solid #1e3a5f;"
                                    "border-radius:8px;padding:8px 14px;margin:6px 0;font-size:0.76rem'>"
                                    "<b style='color:#93c5fd'>💰 Advance Allocation Across Invoices</b>"
                                    "<table style='width:100%;margin-top:6px;border-collapse:collapse'>"
                                    "<tr>"
                                    "<th style='color:#64748b;text-align:left'>Invoice</th>"
                                    "<th style='color:#64748b;text-align:right'>Value</th>"
                                    "<th style='color:#64748b;text-align:right'>Advance</th>"
                                    "<th style='color:#64748b;text-align:right'>Receipt</th>"
                                    "<th style='color:#64748b;text-align:right'>Balance</th>"
                                    "<th style='color:#64748b;text-align:right'>Status</th>"
                                    "</tr>"
                                )
                                for _pbr in _pb_invoices:
                                    _it  = float(_pbr["total"])
                                    _ip  = float(_pbr["paid"])
                                    _ib  = float(_pbr["balance"])
                                    _ist = _pbr.get("payment_status","")
                                    _ino = _pbr.get("invoice_no","")
                                    _adv = min(_adv_rem, _it)
                                    _adv_rem = max(0, _adv_rem - _adv)
                                    _rec = max(0, _ip - _adv)
                                    _sc  = "#4ade80" if _ist=="PAID" else "#fbbf24" if _ist=="PARTIAL" else "#f87171"
                                    _html += (
                                        f"<tr>"
                                        f"<td style='color:#e2e8f0'>{_ino}</td>"
                                        f"<td style='color:#e2e8f0;text-align:right'>₹{_it:,.2f}</td>"
                                        f"<td style='color:#a5b4fc;text-align:right'>₹{_adv:,.2f}</td>"
                                        f"<td style='color:#86efac;text-align:right'>₹{_rec:,.2f}</td>"
                                        f"<td style='color:#fbbf24;text-align:right'>₹{_ib:,.2f}</td>"
                                        f"<td style='color:{_sc};text-align:right'>{_ist}</td>"
                                        f"</tr>"
                                    )
                                _html += "</table></div>"
                                st.markdown(_html, unsafe_allow_html=True)
                        except Exception:
                            pass

                        # RULE: RETAIL — strict payment gate on Convert to Invoice
                        # RULE: Wholesale CHALLAN party — Convert to Invoice ALLOWED here
                        #       (this is the designated path for challan-only parties)
                        if not _ch_inv_ok and otype == "RETAIL":
                            st.error(
                                f"🚫 **Invoice blocked** — collect ₹{_ch_live_balance:,.2f} "
                                f"before converting to invoice (retail rule)."
                            )
                        elif _ch_inv_ok:
                            if not st.session_state.get(inv_conf_key):
                                if st.button("🧾 Convert to Invoice",
                                             key=inv_btn_key, type="primary",
                                             use_container_width=True):
                                    st.session_state[inv_conf_key] = True
                                    st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                    st.rerun()
                            else:
                                st.warning(f"Create invoice for **{cno}**?")
                                _y, _n = st.columns(2)
                                with _y:
                                    if st.button("✅ Yes, create",
                                                 key=f"yes_inv_{cid}",
                                                 type="primary",
                                                 use_container_width=True):
                                        ok, msg = _convert_challan_to_invoice(cid, order)
                                        st.session_state.pop(inv_conf_key, None)
                                        if ok:
                                            # Sync status after invoice
                                            try:
                                                from modules.backoffice.order_status_live import compute_order_status
                                                compute_order_status(order, write=True)
                                            except Exception:
                                                pass
                                            st.success(msg)
                                            st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                            st.rerun()
                                        else:
                                            st.error(msg)
                                with _n:
                                    if st.button("❌ Cancel", key=f"cancel_inv_{cid}",
                                                 use_container_width=True):
                                        st.session_state.pop(inv_conf_key, None)
                                        st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                                        st.rerun()
                        else:
                            st.button(
                                f"🔒 Invoice (₹{_ch_live_balance:,.0f} due)",
                                disabled=True, key=f"mk_inv_dis_{cid}",
                                use_container_width=True,
                                help=(f"Challan ₹{cgt:,.2f} · "
                                      f"Paid ₹{_ch_live_paid:,.2f} · "
                                      f"Balance ₹{_ch_live_balance:,.2f}")
                            )
                            st.caption(f"⚠️ Collect ₹{_ch_live_balance:,.0f} to unlock")
                    else:
                        st.caption(f"Status: {cstatus}")

        # ─────────────────────────────────────────────────────────────
        # ── BILLING SUMMARY ──────────────────────────────────────────
        st.markdown("---")
        _bsh1, _bsh2 = st.columns([5, 1])
        with _bsh1:
            st.markdown("#### 💰 Billing Summary")
        with _bsh2:
            if st.button("🔄 Sync", key=f"sync_status_{order_id}",
                         help="Recompute order status from billing truth",
                         use_container_width=True):
                try:
                    from modules.backoffice.order_status_live import compute_order_status
                    _s = compute_order_status(order, write=True)
                    st.success(f"Status → {_s}")
                    st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                    st.rerun()
                except Exception as _se:
                    st.error(str(_se))

        # Collect active (non-void, non-cancelled) challans & invoices
        try:
            from modules.sql_adapter import run_query as _rq_bs
            _active_challans = _rq_bs("""
                SELECT c.id::text AS challan_id, c.challan_no,
                       c.status, c.grand_total, c.total_amount,
                       c.order_ids,
                       c.created_at, c.is_partial_billing,
                       (SELECT COUNT(*) FROM challan_lines cl
                        WHERE cl.challan_id = c.id
                          AND NOT COALESCE(cl.is_deleted,FALSE)) AS line_count,
                       (SELECT i.invoice_no FROM invoices i
                        WHERE i.challan_id = c.id
                          AND NOT COALESCE(i.is_deleted,FALSE)
                          AND i.status NOT IN ('CANCELLED','VOID')
                        LIMIT 1) AS invoice_no,
                       (SELECT i.id::text FROM invoices i
                        WHERE i.challan_id = c.id
                          AND NOT COALESCE(i.is_deleted,FALSE)
                          AND i.status NOT IN ('CANCELLED','VOID')
                        LIMIT 1) AS invoice_id,
                       (SELECT i.grand_total FROM invoices i
                        WHERE i.challan_id = c.id
                          AND NOT COALESCE(i.is_deleted,FALSE)
                          AND i.status NOT IN ('CANCELLED','VOID')
                        LIMIT 1) AS invoice_amount,
                       (SELECT i.payment_status FROM invoices i
                        WHERE i.challan_id = c.id
                          AND NOT COALESCE(i.is_deleted,FALSE)
                          AND i.status NOT IN ('CANCELLED','VOID')
                        LIMIT 1) AS payment_status
                FROM challans c
                WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
                  AND c.status NOT IN ('VOID','CANCELLED','DELETED')
                ORDER BY c.created_at ASC
            """, {"oid": order_id, "ono": order_no}) or []
            _hidden_zero_active = []
            _clean_active_challans = []
            for _ach0 in _active_challans:
                if (
                    int(_ach0.get("line_count") or 0) == 0
                    and abs(float(_ach0.get("grand_total") or 0)) < 0.01
                ):
                    _hidden_zero_active.append(_ach0)
                    continue
                _clean_active_challans.append(_ach0)
            if _hidden_zero_active:
                st.warning(
                    f"Hidden {len(_hidden_zero_active)} zero-line billing summary document(s). "
                    "They are legacy corrupt documents and are not counted in billing totals."
                )
            _active_challans = _clean_active_challans
        except Exception:
            _active_challans = []

        # Recalculate order total from lines to avoid stale MRP in total_value
        _otype_pay = str(order.get("order_type") or "RETAIL").upper()
        if all_lines and _otype_pay != "RETAIL":
            _total_order = round(sum(
                _correct_line_total(l, _otype_pay)
                for l in all_lines if not l.get("is_deleted")
            ), 2)
            if _total_order <= 0:
                _total_order = float(order.get("total_value") or 0)
        else:
            _total_order = float(order.get("total_value") or 0)
        _total_invoiced = 0.0
        _total_pending_challan = 0.0

        if _active_challans:
            for _ach in _active_challans:
                _cno   = _ach.get("challan_no") or "—"
                _cstat = (_ach.get("status") or "").upper()
                _camt  = float(_ach.get("grand_total") or 0)
                _cdate = str(_ach.get("created_at") or "")[:10]
                _inv_no   = _ach.get("invoice_no")
                _inv_amt  = float(_ach.get("invoice_amount") or 0)
                _pstat    = (_ach.get("payment_status") or "").upper()
                _is_part  = bool(_ach.get("is_partial_billing"))
                if _inv_no:
                    _doc_paid = _challan_payment_truth(
                        str(_ach.get("challan_id") or ""),
                        _ach.get("order_ids"),
                        order_id=order_id,
                        order_no=order_no,
                    )["paid"]
                    _doc_total_for_status = _inv_amt or _camt
                    if _doc_paid - _doc_total_for_status > 0.50:
                        _pstat = "EXCESS"
                    elif _doc_total_for_status > 0 and (_doc_total_for_status - _doc_paid) <= 0.50:
                        _pstat = "PAID"
                    elif _doc_paid > 0:
                        _pstat = "PARTIAL"
                    elif not _pstat:
                        _pstat = "UNPAID"

                # Per-challan lines
                try:
                    _ch_lines = _rq_bs("""
                        SELECT cl.eye_side, cl.product_name, cl.quantity,
                               COALESCE(cl.line_total, cl.total_price, 0) AS total_price
                        FROM challan_lines cl
                        WHERE cl.challan_id = %(cid)s::uuid
                          AND NOT COALESCE(cl.is_deleted,FALSE)
                    """, {"cid": _ach.get("challan_id")}) or []
                except Exception:
                    _ch_lines = []

                # Colour coding
                if _inv_no:
                    _border   = "#10b981"
                    _bg       = "#0a1a12"
                    _hdr_col  = "#4ade80"
                    _tag      = f"{'PARTIAL ' if _is_part else ''}INVOICED"
                    _tag_bg   = "#10b981"
                    _total_invoiced += _inv_amt or _camt
                else:
                    _border  = "#f59e0b"
                    _bg      = "#1a1200"
                    _hdr_col = "#fbbf24"
                    _tag     = f"{'PARTIAL ' if _is_part else ''}CHALLAN"
                    _tag_bg  = "#f59e0b"
                    _total_pending_challan += _camt

                # Lines HTML
                _lines_html = ""
                for _cl in _ch_lines:
                    _eye = (_cl.get("eye_side") or "").upper()
                    _pn  = _cl.get("product_name") or ""
                    _qty = _cl.get("quantity") or 0
                    _tp  = float(_cl.get("total_price") or 0)
                    _lines_html += (
                        f"<div style='display:flex;justify-content:space-between;"
                        f"padding:3px 0;border-bottom:1px solid #ffffff08'>"
                        f"<span style='color:#94a3b8;font-size:0.74rem'>"
                        f"<b style='color:{_hdr_col}'>{_eye}</b> {_pn}</span>"
                        f"<span style='color:#94a3b8;font-size:0.72rem'>"
                        f"Qty {_qty} · ₹{_tp:,.2f}</span>"
                        f"</div>"
                    )

                _inv_row = ""
                if _inv_no:
                    _ps_color = "#4ade80" if _pstat == "PAID" else (
                        "#fbbf24" if _pstat in ("PARTIAL", "EXCESS") else "#f87171"
                    )
                    _inv_row = (
                        f"<div style='margin-top:6px;padding:4px 8px;"
                        f"background:#0d2818;border-radius:6px;"
                        f"display:flex;justify-content:space-between;align-items:center'>"
                        f"<span style='color:#34d399;font-size:0.75rem;font-weight:700'>"
                        f"🧾 {_inv_no}</span>"
                        f"<span style='color:#34d399;font-size:0.74rem'>₹{_inv_amt:,.2f}</span>"
                        f"<span style='color:{_ps_color};font-size:0.7rem;font-weight:700'>"
                        f"{_pstat or 'PENDING'}</span>"
                        f"</div>"
                    )

                # Challan icon/label
                _doc_icon  = "🧾" if _inv_no else "📋"
                _doc_label = "INVOICED" if _inv_no else "CHALLAN"
                if _is_part:
                    _doc_label = "PARTIAL " + _doc_label

                st.markdown(
                    f"<div style='background:{_bg};border:1px solid {_border}55;"
                    f"border-left:4px solid {_border};border-radius:8px;"
                    f"padding:10px 14px;margin:6px 0'>"
                    # Header row
                    f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap'>"
                    f"<span style='color:{_hdr_col};font-weight:800;font-size:0.88rem'>"
                    f"{_doc_icon} {_cno}</span>"
                    f"<span style='background:{_tag_bg}22;color:{_hdr_col};"
                    f"border:1px solid {_tag_bg}55;border-radius:8px;"
                    f"padding:2px 10px;font-size:0.65rem;font-weight:700;letter-spacing:0.04em'>"
                    f"{_doc_label}</span>"
                    f"<span style='color:#475569;font-size:0.7rem;margin-left:auto'>{_cdate}</span>"
                    f"<span style='color:{_hdr_col};font-weight:800;font-size:0.88rem'>₹{_camt:,.2f}</span>"
                    f"</div>"
                    # Lines
                    f"{_lines_html}"
                    # Invoice row (if invoiced)
                    f"{_inv_row}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
        else:
            # No active challans — show unbilled lines if any
            if billed_lines:
                pass  # all billed via now-void challans (edge case)
            elif not ready_lines:
                st.info("No lines ready for billing yet. Allocate stock and complete production first.")

        # ── Billed Lines section ─────────────────────────────────────
        if billed_lines:
            st.markdown("##### ✅ Billed Lines")
            for line in billed_lines:
                col1, col2, col3, col4 = st.columns([1, 4, 2, 1])
                with col1:
                    st.markdown("✅")
                with col2:
                    eye = (line.get("eye_side") or "").upper()
                    st.markdown(f"**{eye}** — {line.get('product_name','')}")
                with col3:
                    tp = _correct_line_total(line, otype)
                    st.caption(f"₹{tp:,.2f}")
                with col4:
                    st.caption(f"Qty {line.get('quantity') or line.get('billing_qty') or 0}")

        # ── Final Status Banner ───────────────────────────────────────
        _ord_status_now = str(order.get("status") or "").upper()
        _all_billed     = (not ready_lines and not pending_lines and billed_lines)
        _is_partial_now = (_ord_status_now == "PARTIALLY_BILLED" or
                           (billed_lines and (ready_lines or pending_lines)))

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        if _ord_status_now == "BILLED" or (_all_billed and _active_challans):
            # ── FULLY BILLED ─────────────────────────────────────────
            _n_challans = len(_active_challans)
            _n_invoiced = sum(1 for _c in _active_challans if _c.get("invoice_no"))
            _inv_list   = ", ".join(
                _c["invoice_no"] for _c in _active_challans if _c.get("invoice_no")
            )
            st.markdown(
                f"<div style='background:#0a2a1a;border:2px solid #10b981;"
                f"border-radius:10px;padding:14px 18px;text-align:center'>"
                f"<div style='color:#4ade80;font-size:1.1rem;font-weight:800'>"
                f"✅ ORDER FULLY BILLED</div>"
                f"<div style='color:#6ee7b7;font-size:0.78rem;margin-top:4px'>"
                f"{_n_challans} challan(s) · {_n_invoiced} invoice(s)"
                f"{' · ' + _inv_list if _inv_list else ''}</div>"
                f"<div style='color:#34d399;font-size:0.9rem;font-weight:700;margin-top:6px'>"
                f"Total Invoiced: ₹{_total_invoiced:,.2f}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        elif _is_partial_now and _active_challans:
            # ── PARTIALLY BILLED ─────────────────────────────────────
            _n_invoiced = sum(1 for _c in _active_challans if _c.get("invoice_no"))
            _inv_list   = ", ".join(
                _c["invoice_no"] for _c in _active_challans if _c.get("invoice_no")
            )
            _pending_count = len(pending_lines) + len(ready_lines)
            st.markdown(
                f"<div style='background:#1a1200;border:2px solid #f59e0b;"
                f"border-radius:10px;padding:14px 18px'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<span style='color:#fbbf24;font-size:1rem;font-weight:800'>"
                f"⚡ PARTIALLY BILLED</span>"
                f"<span style='color:#f59e0b;font-size:0.75rem'>"
                f"{len(_active_challans)} challan(s) · {_n_invoiced} invoice(s)</span>"
                f"</div>"
                f"{'<div style=\"color:#6ee7b7;font-size:0.74rem;margin-top:2px\">Invoices: ' + _inv_list + '</div>' if _inv_list else ''}"
                f"<div style='margin-top:8px;display:flex;gap:20px'>"
                f"<span style='color:#4ade80;font-size:0.82rem'>"
                f"✅ Invoiced: ₹{_total_invoiced:,.2f}</span>"
                f"<span style='color:#fbbf24;font-size:0.82rem'>"
                f"📋 In Challan: ₹{_total_pending_challan:,.2f}</span>"
                f"<span style='color:#f87171;font-size:0.82rem'>"
                f"⏳ {_pending_count} line(s) still pending</span>"
                f"</div>"
                f"<div style='color:#94a3b8;font-size:0.72rem;margin-top:6px'>"
                f"Order Total: ₹{_total_order:,.2f}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        elif not _active_challans and not billed_lines:
            pass  # already handled above with st.info

    except Exception as e:
        st.error(f"❌ Billing panel error: {str(e)}")



def _render_consultation_billing(order, run_query, run_write):
    """
    Billing panel for consultation orders.
    Consultation = closed order with a fee but no product lines.
    Shows fee, payment mode, and option to mark as paid.
    """
    import streamlit as st

    order_id = str(order.get("id") or "")
    order_no = str(order.get("order_no") or "")
    fee      = float(order.get("total_value") or 0)
    pmode    = str(order.get("payment_mode") or "CASH").upper()
    pname    = str(order.get("patient_name") or order.get("party_name") or "—")
    mob      = str(order.get("patient_mobile") or order.get("party_mobile") or "")
    status   = str(order.get("status") or "CLOSED").upper()

    try:
        from modules.sql_adapter import resolve_order_uuid as _resolve_order_uuid
        order_id = _resolve_order_uuid(order_id or order_no) or ""
    except Exception:
        order_id = ""
    if not order_id:
        return

    # Check if payment already recorded
    paid_rows = run_query(
        "SELECT COALESCE(SUM(amount),0) AS paid FROM payments "
        "WHERE advance_for_order_id=%s::uuid "
        "AND NOT COALESCE(is_deleted,FALSE)", (order_id,)
    ) or []
    already_paid = round(float((paid_rows[0]["paid"] if paid_rows else 0) or 0), 2)
    balance      = round(max(fee - already_paid, 0), 2)

    st.markdown(
        "<div style='background:#0f1e0f;border:1px solid #10b981;border-radius:8px;"
        "padding:10px 14px;margin:6px 0'>"
        "<span style='color:#4ade80;font-weight:700;font-size:0.85rem'>"
        "🩺 Consultation Billing</span></div>",
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Consultation Fee", f"₹{fee:,.2f}")
    c2.metric("Paid",    f"₹{already_paid:,.2f}")
    c3.metric("Balance", f"₹{balance:,.2f}",
              delta="✅ Settled" if balance <= 0 else None,
              delta_color="normal")

    if balance > 0:
        st.markdown("**Record Payment**")
        pc1, pc2, pc3, pc4 = st.columns([2, 1.5, 1.5, 1])
        amt   = pc1.number_input("Amount", min_value=0.0,
                                 max_value=float(max(balance, 0.0)),
                                 value=float(max(balance, 0.0)), step=1.0,
                                 key=f"consult_pmt_amt_{order_id}")
        modes = ["CASH", "UPI", "NEFT", "CARD", "CHEQUE"]
        mode  = pc2.selectbox("Mode", modes, key=f"consult_pmt_mode_{order_id}")
        ref   = pc3.text_input("Ref", key=f"consult_pmt_ref_{order_id}",
                               placeholder="optional")
        pc4.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if pc4.button("✅ Record", key=f"consult_pmt_go_{order_id}",
                      use_container_width=True, type="primary"):
            import uuid as _uuid, datetime as _dt
            try:
                from modules.sql_adapter import run_write as _rw
                try:
                    from modules.db.order_number_registry import alloc_doc_number as _alloc_pay_no
                    pno = _alloc_pay_no("PAYMENT")
                except Exception:
                    pno = "PAY/" + _dt.datetime.now().strftime("%y%m%d%H%M%S")
                _rw("""
                    INSERT INTO payments
                        (id, payment_no, party_name,
                         advance_for_order_id, order_id,
                         payment_date, payment_mode, amount,
                         reference_no, payment_type, is_advance, created_by)
                    VALUES
                        (%s::uuid, %s, %s, %s::uuid, %s::uuid,
                         %s, %s, %s, %s, 'PAYMENT', FALSE, %s)
                """, (str(_uuid.uuid4()), pno, pname,
                      order_id, order_id,
                      _dt.date.today(), mode, amt,
                      ref or None,
                      st.session_state.get("user_name","Staff")))
                _clear_payment_truth_cache()
                st.success(f"✅ {pno} — ₹{amt:,.2f} recorded")
                st.session_state["bo_show_billing_tab"] = True  # keep billing tab active

                st.rerun()
            except Exception as e:
                st.error(f"Payment failed: {e}")
    else:
        st.success("✅ Consultation fee fully paid")

    # Show payment history
    hist = run_query(
        "SELECT payment_no, payment_date, payment_mode, amount "
        "FROM payments WHERE advance_for_order_id=%s::uuid "
        "AND NOT COALESCE(is_deleted,FALSE) ORDER BY payment_date DESC",
        (order_id,)
    ) or []
    if hist:
        with st.expander("Payment History", expanded=False):
            for h in hist:
                st.caption(f"{h['payment_date']} · {h['payment_mode']} · ₹{float(h['amount']):,.2f} · {h['payment_no']}")
