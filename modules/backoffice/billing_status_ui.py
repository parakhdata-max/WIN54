"""
Billing status panel with delete functionality.
"""

from __future__ import annotations
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

def _q(sql: str, params: dict = None):
    """Run a read query."""
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {})
    except Exception as e:
        st.error(f"❌ Query error: {e}")
        return []

def _convert_challan_to_invoice(challan_id: str, order: dict) -> Tuple[bool, str]:
    """Convert a challan to invoice."""
    try:
        from modules.sql_adapter import run_write, run_query

        # ── Fetch challan header ───────────────────────────────────
        ch_rows = run_query("""
            SELECT c.id, c.challan_no, c.party_id, c.order_ids,
                   c.total_amount, c.total_tax, c.grand_total,
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
                   cl.total_price, cl.eye_side, cl.brand,
                   ol.gst_percent
            FROM challan_lines cl
            LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
            WHERE cl.challan_id = %(cid)s::uuid
              AND NOT COALESCE(cl.is_deleted, FALSE)
        """, {"cid": challan_id})

        # ── Create invoice header ───────────────────────────────────
        inv_no = _next_invoice_no()
        run_write("""
            INSERT INTO invoices
                (invoice_no, challan_id, party_id, order_ids,
                 invoice_date, total_amount, total_tax, grand_total,
                 status, created_by, gst_included)
            VALUES
                (%(inv)s, %(cid)s::uuid, %(pid)s::uuid, ARRAY[%(oid)s::text],
                 CURRENT_DATE, %(amt)s, %(tax)s, %(gt)s, 'PENDING',
                 %(by)s, TRUE)
        """, {
            "inv":     inv_no,
            "cid":     challan_id,
            "pid":     ch.get("party_id"),
            "oid":     ch.get("order_ids"),
            "amt":     ch.get("total_amount"),
            "tax":     ch.get("total_tax"),
            "gt":      ch.get("grand_total"),
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
                     quantity, unit_price, total_price, eye_side, brand,
                     gst_percent)
                VALUES
                    (%(inv_uuid)s::uuid, %(oid)s::uuid, %(olid)s::uuid, %(pn)s,
                     %(qty)s, %(up)s, %(tp)s, %(eye)s, %(br)s,
                     %(gst)s)
            """, {
                "inv_uuid": inv_uuid,
                "oid":      _o_id,
                "olid":     _ol_id,
                "pn":       cl.get("product_name") or "",
                "qty":      cl.get("quantity") or 0,
                "up":       cl.get("unit_price") or 0,
                "tp":       cl.get("total_price") or 0,
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
    Returns the correct grand total for a line using compute_line_gst.
    Single source of truth — delegates to price_qty_governor.
    RETAIL:    unit_price × qty  (MRP inclusive, no GST added)
    WHOLESALE: unit_price × qty × (1 + gst%)  (ex-GST, GST added)
    """
    try:
        from modules.core.price_qty_governor import compute_line_gst
        up  = float(line.get("unit_price") or 0)
        qty = int(line.get("billing_qty") or line.get("quantity") or 0)
        gst = float(line.get("gst_percent") or 0)
        ot  = str(order_type or "RETAIL").upper()
        if up > 0 and qty > 0:
            return compute_line_gst(up, qty, gst, ot)["grand_total"]
    except Exception:
        pass
    return float(line.get("total_price") or line.get("billing_total") or 0)


def render_billing_status_panel(order, all_lines):
    """Main billing status panel rendering function."""
    try:
        from modules.sql_adapter import run_query, run_write
        import streamlit as st

        # ── Consultation orders: fee-only billing, no product lines ──────
        if str(order.get("order_type","")).upper() == "CONSULTATION":
            _render_consultation_billing(order, run_query, run_write)
            return

        order_id  = str(order.get("id") or "")
        order_no  = str(order.get("order_no") or "")
        party_id  = order.get("party_id") or None
        otype     = (order.get("order_type") or "RETAIL").upper()
        operator  = _operator()

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
            _lbg = "#0c1a3a" if _ls == "CHALLANED" else "#052e16"
            _lbd = "#3b82f6" if _ls == "CHALLANED" else "#22c55e"
            _lic = "📋"      if _ls == "CHALLANED" else "🔒"
            _lmsg = ("Challaned — awaiting invoice conversion."
                     if _ls == "CHALLANED" else
                     "Fully invoiced — no further edits allowed.")
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

        # ── Ready lines analysis ─────────────────────────────────────
        # Only lines in ACTIVE (non-void) challans count as "billed"
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
        _bs_order_uuid = str(order.get("id") or "")
        _job_done_stages = {
            "READY_FOR_PACK", "AWAITING_FITTING", "SENT_TO_FITTER",
            "RECEIVED_FROM_FITTER", "FITTING_DONE",
            "DISPATCHED", "DELIVERED", "READY_TO_BILL"
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
                if _jstg in _job_done_stages:
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
                        if not _l.get("manufacturing_route") and _lp_bs.get("manufacturing_route"):
                            _l["manufacturing_route"] = _lp_bs["manufacturing_route"]
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
                # SERVICE lines always ready
                if not line.get("allocated_qty"):
                    line["allocated_qty"] = line.get("quantity") or 1
                ready_lines.append(line)
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

                # INHOUSE: surfacing_data present = job was completed = READY
                # (ready_qty may be 0 if old pipeline didn't set it — surfacing_data is ground truth)
                if _is_inhouse:
                    if _has_surfacing or ready_q >= needed:
                        ready_lines.append(line)
                    else:
                        pending_lines.append(line)
                elif _is_vendor:
                    # VENDOR + EXTERNAL_LAB: must go through supplier pipeline.
                    # supplier_stage=null or ORDER_PLACED = not ready
                    # Only READY_FOR_BILLING allows billing.
                    _lp_vnd   = line.get("lens_params") or {}
                    _lp_vnd   = _lp_vnd if isinstance(_lp_vnd, dict) else {}
                    _sup_stg  = str(_lp_vnd.get("supplier_stage") or "ORDER_PLACED").upper()
                    if _sup_stg == "READY_FOR_BILLING":
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
        _billing_pref = "CHALLAN"  # default
        try:
            from modules.billing.challan_invoice_manager import get_party_billing_preference
            if party_id:
                _billing_pref = (get_party_billing_preference(str(party_id)) or "CHALLAN").upper()
        except Exception:
            pass
        # ── Document routing ────────────────────────────────────────────────
        # doc_preference 'C' = Challan first, invoice from Challan Dashboard
        # doc_preference 'I' = Direct invoice (wholesale only)
        #
        # RETAIL always 'C' — no direct invoice, no exceptions.
        # WHOLESALE follows party.doc_preference set in Party Master.
        # get_party_billing_preference() now returns 'C' or 'I'.
        if otype == "RETAIL":
            _direct_invoice = False           # retail → challan always
        else:
            _direct_invoice = (_billing_pref == "I")   # wholesale → party setting

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
            _fpname = str(_fl.get("product_name") or "").split(" | ")[0]
            _fqty  = int(_fl.get("quantity") or _fl.get("billing_qty") or 1)
            _fup   = float(_fl.get("unit_price") or 0)
            _ftotal = _correct_line_total(_fl, otype)
            _feye_lbl = "👁R" if _feye in ("R","RIGHT") else "👁L" if _feye in ("L","LEFT") else "🔧" if _feye in ("S","SERVICE") else "🖼"
            _fpwr = ""
            try:
                if _fl.get("sph") is not None:
                    _fpwr = f"{float(_fl['sph']):+.2f}"
                    if _fl.get("cyl") and abs(float(_fl["cyl"])) > 0.01:
                        _fpwr += f"/{float(_fl['cyl']):+.2f}"
                    if _fl.get("axis"): _fpwr += f"×{int(_fl['axis'])}"
            except Exception: pass

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
                if _is_ready_fl and not _is_billed_fl:
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
        if checked_lines or (not ready_lines and not pending_lines and not billed_lines):
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
                _up_r  = float(_l.get("unit_price") or 0)
                _qty_r = int(_l.get("quantity") or _l.get("billing_qty") or 1)
                _gst_p = float(_l.get("gst_percent") or 0)
                # Use per-pcs unit_price — always ex-GST for wholesale, MRP for retail
                _gst_c = compute_line_gst(_up_r, _qty_r, _gst_p, otype)
                _total_base += _gst_c["gst_base"]
                _total_tax  += _gst_c["gst_amount"]

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
                _inv_payment_ok = not _blocked_inv
                if _blocked_inv:
                    st.warning(f"⚠️ **Invoice blocked** — {_inv_block_reason}")
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
                    _challan_btn_label = (
                        "📋 Make Challan & Invoice"
                        if _direct_invoice
                        else "📋 Make Challan"
                    )
                    _challan_btn_help = (
                        "Wholesale direct invoice party — challan + invoice created together"
                        if _direct_invoice
                        else "Creates a challan. Convert to invoice later from Challan Dashboard."
                    )
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
                                                st.success(f"✅ Challan {challan_no} → Invoice {_inv_no} created")
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
                                st.rerun()
                            else:
                                st.error("❌ Challan creation failed — check logs")
                        except Exception as _ce:
                            st.error(f"❌ {_ce}")

                with _b2:
                    st.caption(f"{_n_sel} line(s) · ₹{_sel_total:,.2f}")

        elif not checked_lines and not ready_lines and not pending_lines and not billed_lines:
            st.info("No lines ready for billing yet. Allocate stock and complete production first.")
        elif ready_lines and not checked_lines:
            st.caption("☐ Tick lines above to select for billing")

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
                            from modules.billing.smart_print import (
                                build_invoice_html, _get_shop, _fetch_lines, _fetch_advance
                            )
                            from modules.consultation import _open_print_tab
                            # Load challan as invoice-style document
                            _ch_inv_row = run_query("""
                                SELECT c.*, c.id::text AS id,
                                       COALESCE(p.party_name, o.party_name, '—') AS party_name,
                                       COALESCE(p.mobile,'') AS mobile,
                                       COALESCE(p.address,'') AS address,
                                       COALESCE(p.gstin,'') AS gstin,
                                       COALESCE(p.state_code,'') AS state_code,
                                       ARRAY[o.id::text] AS order_ids
                                FROM challans c
                                LEFT JOIN parties p ON p.id = c.party_id
                                LEFT JOIN orders o ON o.id::text = ANY(c.order_ids)
                                WHERE c.id = %(cid)s::uuid LIMIT 1
                            """, {"cid": cid}) or []
                            if _ch_inv_row:
                                _chi = _ch_inv_row[0]
                                _chi["id"] = _chi.get("id") or cid
                                _chl = _fetch_lines(cid, None)
                                _cha = _fetch_advance(_chi.get("order_ids") or [])
                                _ch_html = build_invoice_html(
                                    inv=_chi, lines=_chl, shop=_get_shop(),
                                    doc_type="CHALLAN", advance_paid=_cha
                                )
                                _open_print_tab(_ch_html, f"challan_{cno}.html")
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
                                from modules.billing.smart_print import (
                                    build_invoice_html, _get_shop, _fetch_lines, _fetch_advance
                                )
                                from modules.consultation import _open_print_tab
                                _inv_rows = run_query("""
                                    SELECT i.*, i.id::text AS id,
                                           COALESCE(p.party_name,'—') AS party_name,
                                           COALESCE(p.mobile,'') AS mobile,
                                           COALESCE(p.address,'') AS address,
                                           COALESCE(p.gstin,'') AS gstin,
                                           COALESCE(p.state_code,'') AS state_code,
                                           COALESCE(p.print_with_powers,TRUE) AS print_with_powers,
                                           c.challan_no
                                    FROM invoices i
                                    LEFT JOIN parties p ON p.id = i.party_id
                                    LEFT JOIN challans c ON c.id = i.challan_id
                                    WHERE i.invoice_no = %(n)s LIMIT 1
                                """, {"n": inv_no})
                                if _inv_rows:
                                    _inv = _inv_rows[0]
                                    _inv_lines = _fetch_lines(str(_inv["id"]), _inv.get("challan_id"))
                                    _inv_adv   = _fetch_advance(_inv.get("order_ids") or [])
                                    _inv_html  = build_invoice_html(
                                        inv=_inv, lines=_inv_lines, shop=_get_shop(),
                                        show_powers=bool(_inv.get("print_with_powers", True)),
                                        doc_type="TAX INVOICE", advance_paid=_inv_adv
                                    )
                                    _open_print_tab(_inv_html, f"invoice_{inv_no}.html")
                                else:
                                    st.error("Invoice not found")
                            except Exception as _pie:
                                st.error(f"Print error: {_pie}")
                    else:
                        st.caption(f"Status: {cstatus}")

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
                                        st.rerun()
                                    except Exception as _rce:
                                        st.error(f"Recall failed: {_rce}")
                            with _rcb2:
                                if st.button("← Cancel", key=f"recall_ch_no_{cid}",
                                             use_container_width=True):
                                    st.session_state.pop(_recall_ch_key, None)
                                    st.rerun()

                with _rch3:
                    if cstatus == "PENDING" and not inv_no:
                        inv_btn_key  = f"make_invoice_{cid}"
                        inv_conf_key = f"confirm_invoice_{cid}"

                        # Live payment check against this challan's value
                        _ch_order_ids_inv = [str(x) for x in (ch.get("order_ids") or [])]
                        _ch_adv = 0.0
                        _ch_dir = 0.0
                        try:
                            if _ch_order_ids_inv:
                                _a = run_query("""
                                    SELECT COALESCE(SUM(amount),0) AS tot FROM payments
                                    WHERE advance_for_order_id::text = ANY(%(oids)s)
                                      AND payment_type='ADVANCE'
                                      AND COALESCE(is_deleted,FALSE)=FALSE
                                """, {"oids": _ch_order_ids_inv})
                                _ch_adv = float((_a[0]["tot"] if _a else 0) or 0)
                            _d = run_query("""
                                SELECT COALESCE(SUM(amount),0) AS tot FROM payments
                                WHERE challan_id=%(cid)s
                                  AND COALESCE(is_deleted,FALSE)=FALSE
                            """, {"cid": cid})
                            _ch_dir = float((_d[0]["tot"] if _d else 0) or 0)
                        except Exception:
                            pass
                        _ch_live_paid    = _ch_adv + _ch_dir
                        _ch_live_balance = round(max(cgt - _ch_live_paid, 0), 2)
                        _ch_inv_ok = (
                            otype != "RETAIL"
                            or _ch_live_balance <= 0.01
                            or cgt == 0
                        )

                        if _ch_inv_ok:
                            if not st.session_state.get(inv_conf_key):
                                if st.button("🧾 Convert to Invoice",
                                             key=inv_btn_key, type="primary",
                                             use_container_width=True):
                                    st.session_state[inv_conf_key] = True
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
                                            st.rerun()
                                        else:
                                            st.error(msg)
                                with _n:
                                    if st.button("❌ Cancel", key=f"cancel_inv_{cid}",
                                                 use_container_width=True):
                                        st.session_state.pop(inv_conf_key, None)
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
                    st.rerun()
                except Exception as _se:
                    st.error(str(_se))

        # Collect active (non-void, non-cancelled) challans & invoices
        try:
            from modules.sql_adapter import run_query as _rq_bs
            _active_challans = _rq_bs("""
                SELECT c.id::text AS challan_id, c.challan_no,
                       c.status, c.grand_total, c.total_amount,
                       c.created_at, c.is_partial_billing,
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

                # Per-challan lines
                try:
                    _ch_lines = _rq_bs("""
                        SELECT cl.eye_side, cl.product_name, cl.quantity, cl.total_price
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
                        "#fbbf24" if _pstat == "PARTIAL" else "#f87171"
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
                                 value=float(balance), step=1.0,
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
                pno = "PMT-" + _dt.datetime.now().strftime("%Y%m%d%H%M%S")
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
                st.success(f"✅ {pno} — ₹{amt:,.2f} recorded")
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
