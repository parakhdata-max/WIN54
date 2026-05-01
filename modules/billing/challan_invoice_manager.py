import re
"""
Challan & Invoice Manager
========================

Handles challan and invoice generation for orders based on party billing preferences.
- CHALLAN customers: Orders grouped into challans, then invoiced
- DIRECT_INVOICE customers: Orders invoiced directly

Location: modules/billing/challan_invoice_manager.py
"""

import streamlit as st
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime, date, timedelta
import uuid as _uuid
from modules.pricing.billing_engine import compute_line_totals


def _q(sql: str, params: dict = None) -> List[Dict]:
    """Database query helper"""
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"Database error: {e}")
        return []


def _write(sql: str, params: dict = None) -> bool:
    """Database write helper"""
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        st.error(f"Database write error: {e}")
        return False


def _run_transaction(steps: list) -> bool:
    """
    Execute multiple (sql, params) pairs inside a single DB transaction.

    If any step raises, the whole transaction rolls back and no partial data
    is written. This is critical for create_challan / create_invoice where
    we must atomically insert the header, lines, update billed_qty, and
    write the ledger row.

    Usage:
        ok = _run_transaction([
            (insert_challan_sql, challan_params),
            (insert_line_sql,    line_params),
            (update_billed_sql,  billed_params),
        ])

    Requires sql_adapter.run_transaction(steps) → raises on failure.
    Falls back to sequential _write() calls if run_transaction is not
    available in the adapter (graceful degradation for older adapters).
    """
    try:
        from modules.sql_adapter import run_transaction
        run_transaction(steps)
        return True
    except ImportError:
        # Adapter does not yet support run_transaction — degrade gracefully
        # by running steps individually. Atomicity is lost but functionality
        # is preserved. Upgrade the adapter to gain full transaction safety.
        try:
            for sql, params in steps:
                from modules.sql_adapter import run_write
                run_write(sql, params or {})
            return True
        except Exception as e:
            st.error(f"Transaction step failed: {e}")
            return False
    except Exception as e:
        st.error(f"Transaction failed — all changes rolled back: {e}")
        return False


# =====================================================
# BILLING GATE — Pipeline Validation
# =====================================================

def is_line_billing_ready(order_line_id: str) -> Tuple[bool, str]:
    """
    Gate check per order line.
    Returns (ready: bool, reason: str).
    STOCK   → allocated_qty >= quantity
    VENDOR  → supplier received >= ordered
    INHOUSE → job stage in READY/DISPATCHED/CLOSED
    """
    rows = _q("""
        SELECT quantity, COALESCE(allocated_qty,0) AS allocated_qty,
               COALESCE(ready_qty,0) AS ready_qty,
               COALESCE(lens_params->>'manufacturing_route','STOCK') AS route
        FROM order_lines
        WHERE id = %(id)s::uuid
    """, {"id": order_line_id})
    if not rows:
        return False, "Order line not found"

    r     = rows[0]
    qty   = int(r.get("quantity") or 0)
    alloc = int(r.get("allocated_qty") or 0)
    route = str(r.get("route") or "STOCK").upper()

    if route == "STOCK":
        if alloc >= qty:
            return True, "Stock allocated"
        return False, f"Stock not fully allocated ({alloc}/{qty})"

    if route in ("VENDOR", "EXTERNAL_LAB"):
        rcv = _q("""
            SELECT COALESCE(SUM(soi.received_qty),0) AS rcv,
                   COALESCE(SUM(soi.ordered_qty), 0) AS ord
            FROM supplier_order_items soi
            JOIN supplier_orders so ON so.id = soi.supplier_order_id
            WHERE soi.customer_line_id = %(id)s::uuid
        """, {"id": order_line_id})
        if rcv:
            r_qty, o_qty = int(rcv[0].get("rcv") or 0), int(rcv[0].get("ord") or 0)
            if o_qty > 0 and r_qty >= o_qty:
                return True, "Supplier delivered"
            return False, f"Supplier delivery pending ({r_qty}/{o_qty})"
        return False, "No supplier order found"

    if route == "INHOUSE":
        jobs = _q("""
            SELECT current_stage, is_closed FROM job_master
            WHERE order_line_id = %(id)s::uuid
        """, {"id": order_line_id})
        if not jobs:
            return False, "Job card not created yet"
        for j in jobs:
            stage = str(j.get("current_stage") or "").upper()
            if not j.get("is_closed") and stage not in ("READY", "DISPATCHED", "CLOSED"):
                return False, f"Production not complete (stage: {j.get('current_stage')})"
        return True, "Production complete"

    return True, "Route unknown — billing allowed"


def suggest_manufacturing_route(product_id: str, sph, cyl, axis) -> str:
    """
    Smart Lens Allocation — suggests STOCK / INHOUSE / VENDOR.
    Priority: 1) available stock  2) inhouse capability  3) vendor
    """
    stock = _q("""
        SELECT quantity FROM inventory_stock
        WHERE product_id = %(p)s::uuid
          AND (%(s)s IS NULL OR sph = %(s)s)
          AND (%(c)s IS NULL OR cyl = %(c)s)
          AND (%(a)s IS NULL OR axis = %(a)s)
          AND quantity > 0
        LIMIT 1
    """, {"p": product_id, "s": sph, "c": cyl, "a": axis})
    if stock:
        return "STOCK"

    capability = _q("""
        SELECT 1 FROM lens_capability
        WHERE (%(s)s IS NULL OR max_sph >= %(s)s)
          AND (%(c)s IS NULL OR max_cyl >= %(c)s)
        LIMIT 1
    """, {"s": abs(float(sph or 0)), "c": abs(float(cyl or 0))})
    if capability:
        return "INHOUSE"

    return "VENDOR"


def _write_document_ledger(
    doc_type: str, doc_id: str, doc_no: str,
    order_id: str, order_line_id: str,
    party_id: str, product_id: str,
    quantity: int, base_amount: float,
    tax_amount: float, total_amount: float,
) -> None:
    """
    Append one row to the document_ledger reporting table.
    Never call UPDATE on this table — it is append-only.
    """
    _write("""
        INSERT INTO document_ledger
            (doc_type, doc_id, doc_no, order_id, order_line_id,
             party_id, product_id,
             quantity, base_amount, tax_amount, total_amount)
        VALUES
            (%(dt)s, %(did)s::uuid, %(dno)s, %(oid)s::uuid, %(olid)s::uuid,
             %(pid)s, %(prid)s::uuid,
             %(qty)s, %(base)s, %(tax)s, %(total)s)
    """, {
        "dt":    doc_type,
        "did":   doc_id,
        "dno":   doc_no,
        "oid":   order_id,
        "olid":  order_line_id,
        "pid":   party_id,
        "prid":  product_id,
        "qty":   quantity,
        "base":  base_amount,
        "tax":   tax_amount,
        "total": total_amount,
    })


def _write_party_ledger_debit(
    party_id: str,
    party_name: str,
    doc_type: str,
    doc_id: str,
    doc_no: str,
    amount: float,
    doc_date,
    narration: str = "",
    created_by: str = "System",
) -> None:
    """
    Write a DEBIT entry to party_ledger when an invoice/challan is raised.
    This creates the audit trail:
      - Invoice raised → DEBIT (party owes us)
      - Payment received → CREDIT (in payment_collection._record_allocation)
    Running balance = cumulative debit - cumulative credit = outstanding
    """
    try:
        _write("""
            INSERT INTO party_ledger
                (party_id, party_name, entry_date, entry_type,
                 ref_id, ref_no, debit, credit, narration, created_by)
            VALUES
                (%(pid)s, %(pn)s, %(dt)s, %(et)s,
                 %(rid)s, %(rno)s, %(deb)s, 0, %(nar)s, %(by)s)
            ON CONFLICT DO NOTHING
        """, {
            "pid":  party_id or None,
            "pn":   party_name or "",
            "dt":   doc_date,
            "et":   doc_type,           # 'INVOICE' or 'CHALLAN'
            "rid":  doc_id,
            "rno":  doc_no,
            "deb":  round(float(amount or 0), 2),
            "nar":  narration or f"{doc_type} raised — {doc_no}",
            "by":   created_by,
        })
    except Exception as _le:
        import logging
        logging.getLogger(__name__).warning(f"[party_ledger debit] {_le}")


def soft_delete(table: str, record_id: str, deleted_by: str) -> bool:
    """
    Soft-delete a record instead of physically removing it.
    All financial tables use is_deleted instead of DELETE.
    """
    allowed = {"orders", "order_lines", "challans", "challan_lines",
               "invoices", "invoice_lines"}
    if table not in allowed:
        return False
    return _write(f"""
        UPDATE {table}
        SET is_deleted = TRUE,
            deleted_at = NOW(),
            deleted_by = %(by)s
        WHERE id = %(id)s::uuid
    """, {"by": deleted_by, "id": record_id})


def _auto_advance_order_status(order_ids: List[str], line_ids: Optional[List[str]] = None) -> None:
    """
    DEPRECATED — no longer called.

    Order status advancement to BILLED is now handled entirely by the DB trigger
    auto_close_order_on_billing() which fires on UPDATE of order_lines.billed_qty.

    Keeping the function signature so any external callers get a no-op rather
    than a NameError, but the body does nothing.  Remove callers over time.
    """
    pass  # DB trigger handles this — no Python logic needed


def get_unbilled_lines_for_order(order_id: str) -> List[Dict]:
    """
    Return order lines that have NOT yet been fully billed and are not soft-deleted.
    Used for partial billing UI.

    Fix 5: product_name and brand come from the products table only.
    order_lines does not have these columns — falling back to ol.product_name
    would cause "column does not exist" errors.
    """
    # Option 1: read prices exactly as backoffice computed and saved them.
    # unit_price, total_price, gst_percent, gst_amount are written by
    # order_persistence.py — no LATERAL join or engine needed.
    return _q("""
        SELECT ol.id,
               ol.product_id,
               o.order_type,
               COALESCE(p.product_name, 'Lens')       AS product_name,
               COALESCE(p.brand, '')                   AS brand,
               COALESCE(p.main_group, '')              AS main_group,
               ol.eye_side,
               ol.quantity                       AS billing_qty,
               ol.quantity,
               ol.unit_price,
               ROUND(ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0), 2) AS billing_total,
               ol.gst_percent,
               -- Use stored gst_amount (set by finalize_engine — correct for RETAIL inclusive GST).
               -- Never recalculate as unit_price*qty*gst%/100 — that is the wholesale formula only.
               COALESCE(ol.gst_amount, 0)                           AS gst_amount,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               COALESCE(ol.billed_qty, 0)        AS billed_qty
        FROM order_lines ol
        JOIN  orders o    ON o.id  = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE ol.order_id = %(oid)s::uuid
          AND COALESCE(ol.billed_qty, 0) < ol.quantity
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        ORDER BY ol.eye_side, ol.id
    """, {"oid": order_id})


def get_party_billing_preference(party_id: str) -> str:
    """
    Return billing doc preference for a party.

    Returns:
        'C'  — Challan first, invoice later from Challan Dashboard (default)
        'I'  — Direct invoice (wholesale only — challan auto-created internally)

    Reads doc_preference (CHAR 'C'/'I') first; falls back to the legacy
    billing_preference text column during the migration window.
    RETAIL orders must always be treated as 'C' by the caller — this
    function returns the party setting, not the order-type override.
    """
    if not party_id or str(party_id).startswith("name:"):
        return "C"
    result = _q("""
        SELECT
            COALESCE(doc_preference, 'C')            AS doc_pref,
            COALESCE(billing_preference, 'CHALLAN')  AS legacy_pref
        FROM parties
        WHERE id = %(pid)s::uuid
        LIMIT 1
    """, {"pid": str(party_id)})
    if not result:
        return "C"
    row = result[0]
    # Prefer new column; fall back to legacy mapping if new column not yet populated
    dp = str(row.get("doc_pref") or "").strip().upper()
    if dp in ("C", "I"):
        return dp
    # Legacy fallback
    lp = str(row.get("legacy_pref") or "").strip().upper()
    return "I" if lp == "DIRECT_INVOICE" else "C"


def get_pending_orders_for_party(party_key: str) -> List[Dict]:
    """Get orders ready for billing for a party.

    party_key can be:
      - A UUID string  → match by party_id OR by party_name lookup
      - "name:Vijay wadekar" prefix → match by party_name directly
        (used for parties that exist in orders but have no parties table row)

    unit_price is always per-PCS; never read from inventory_stock.
    """
    if party_key.startswith("name:"):
        # Unregistered party — match only by party_name
        pname = party_key[5:]
        return _q("""
            SELECT o.id, o.order_no, o.created_at,
                   o.patient_name, o.party_name, o.status,
                   o.order_type,
                   COUNT(ol.id) AS line_count,
                   COALESCE(SUM(
                       CASE UPPER(COALESCE(o.order_type,'WHOLESALE'))
                       WHEN 'RETAIL' THEN
                           ROUND(ol.unit_price * ol.quantity, 2)
                       ELSE
                           ROUND(ol.unit_price * ol.quantity, 2)
                           * (1 + COALESCE(ol.gst_percent, 0) / 100)
                       END
                   ), 0) AS total_value
            FROM orders o
            LEFT JOIN order_lines ol
                   ON ol.order_id = o.id
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
            WHERE o.party_name = %(pname)s
              AND o.status IN ('PENDING', 'CONFIRMED', 'READY', 'READY_FOR_BILLING', 'IN_PRODUCTION', 'BILLED', 'PARTIALLY_BILLED')
              AND NOT EXISTS (
                  -- Exclude fully-billed orders (all lines covered, no unbilled non-service lines)
                  SELECT 1 FROM order_lines ol2
                  WHERE ol2.order_id = o.id
                    AND COALESCE(ol2.is_deleted, FALSE) = FALSE
                    AND COALESCE(ol2.is_service_line, FALSE) = FALSE
                    AND UPPER(COALESCE(ol2.eye_side, '')) NOT IN ('S', 'SERVICE')
                  HAVING COUNT(*) > 0
                     AND COUNT(*) = SUM(CASE WHEN COALESCE(ol2.billed_qty,0) >= ol2.quantity THEN 1 ELSE 0 END)
              )
              AND (
                  -- Has unbilled lines OR status lets it through for initial billing
                  EXISTS (
                      SELECT 1 FROM order_lines ol3
                      WHERE ol3.order_id = o.id
                        AND COALESCE(ol3.is_deleted, FALSE) = FALSE
                        AND COALESCE(ol3.is_service_line, FALSE) = FALSE
                        AND UPPER(COALESCE(ol3.eye_side, '')) NOT IN ('S', 'SERVICE')
                        AND COALESCE(ol3.billed_qty, 0) < ol3.quantity
                  )
              )
            GROUP BY o.id, o.order_no, o.created_at,
                     o.patient_name, o.party_name, o.status, o.order_type
            ORDER BY o.created_at DESC
        """, {"pname": pname})

    # Registered party — match by UUID or name lookup
    return _q("""
        SELECT o.id, o.order_no, o.created_at,
               o.patient_name, o.party_name, o.status,
               o.order_type,
               COUNT(ol.id) AS line_count,
               COALESCE(SUM(
                   CASE UPPER(COALESCE(o.order_type,'WHOLESALE'))
                   WHEN 'RETAIL' THEN
                       ROUND(ol.unit_price * ol.quantity, 2)
                   ELSE
                       ROUND(ol.unit_price * ol.quantity, 2)
                       * (1 + COALESCE(ol.gst_percent, 0) / 100)
                   END
               ), 0) AS total_value
        FROM orders o
        LEFT JOIN order_lines ol
               ON ol.order_id = o.id
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        WHERE (
            o.party_id = %(party_id)s::uuid
            OR o.party_name = (SELECT party_name FROM parties WHERE id = %(party_id)s::uuid LIMIT 1)
        )
        AND o.status IN ('PENDING', 'CONFIRMED', 'READY', 'READY_FOR_BILLING', 'IN_PRODUCTION', 'BILLED', 'PARTIALLY_BILLED')
        AND NOT EXISTS (
            -- Exclude fully-billed orders (all non-service lines covered)
            SELECT 1 FROM order_lines ol2
            WHERE ol2.order_id = o.id
              AND COALESCE(ol2.is_deleted, FALSE) = FALSE
              AND COALESCE(ol2.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol2.eye_side, '')) NOT IN ('S', 'SERVICE')
            HAVING COUNT(*) > 0
               AND COUNT(*) = SUM(CASE WHEN COALESCE(ol2.billed_qty,0) >= ol2.quantity THEN 1 ELSE 0 END)
        )
        AND (
            -- Must have at least one unbilled non-service line remaining
            EXISTS (
                SELECT 1 FROM order_lines ol3
                WHERE ol3.order_id = o.id
                  AND COALESCE(ol3.is_deleted, FALSE) = FALSE
                  AND COALESCE(ol3.is_service_line, FALSE) = FALSE
                  AND UPPER(COALESCE(ol3.eye_side, '')) NOT IN ('S', 'SERVICE')
                  AND COALESCE(ol3.billed_qty, 0) < ol3.quantity
            )
        )
        GROUP BY o.id, o.order_no, o.created_at,
                 o.patient_name, o.party_name, o.status, o.order_type
        ORDER BY o.created_at DESC
    """, {"party_id": party_key})


def create_challan(party_id: str, order_ids: List[str],
                  total_amount: float, total_tax: float,
                  remarks: str = "",
                  line_ids: Optional[List[str]] = None,
                  svc_charges: Optional[List[Dict]] = None) -> Optional[str]:
    """
    Create a new challan.

    If line_ids is provided, only those order_lines are included (partial billing).
    If line_ids is None, all lines from the selected orders are included.
    svc_charges: list of dicts from fetch_charges() — written as immutable snapshot
                 in challan_service_charges and locked against deletion.
    """
    try:
        # ── Duplicate challan guard ───────────────────────────────────────
        # Prevent two users creating challan for the same order simultaneously.
        # PARTIAL BILLING ALLOWED: a second challan is permitted when the
        # existing challan(s) cover DIFFERENT order_line_ids (e.g. first challan
        # was for service/consultation, second challan covers product lines).
        try:
            from modules.sql_adapter import run_query as _rq_dup
            for _dup_oid in order_ids:
                _existing = _rq_dup("""
                    SELECT c.challan_no, c.id::text AS challan_id
                    FROM challans c
                    WHERE %(oid)s = ANY(c.order_ids)
                      AND c.status NOT IN ('CANCELLED','VOID')
                """, {"oid": _dup_oid})
                if _existing:
                    # Check if the existing challan covers the SAME lines we're
                    # trying to bill now. If line_ids provided, check overlap.
                    if line_ids:
                        _already_billed_line_ids = set()
                        for _ex in _existing:
                            _cl_rows = _rq_dup("""
                                SELECT order_line_id::text
                                FROM challan_lines
                                WHERE challan_id = %(cid)s::uuid
                                  AND NOT COALESCE(is_deleted, FALSE)
                            """, {"cid": _ex["challan_id"]})
                            _already_billed_line_ids.update(
                                r["order_line_id"] for r in (_cl_rows or [])
                            )
                        _overlap = set(str(l) for l in line_ids) & _already_billed_line_ids
                        if _overlap:
                            raise ValueError(
                                f"Lines already in challan {_existing[0]['challan_no']}. "
                                f"Refresh the page to see the latest billing status."
                            )
                        # No overlap — partial billing of different lines, allow it
                    else:
                        # No specific line_ids — billing all lines, block duplicate
                        raise ValueError(
                            f"Order already has an active challan: "
                            f"{_existing[0]['challan_no']}. "
                            f"Refresh the page to see the latest billing status."
                        )
        except ValueError:
            raise
        except Exception as _guard_e:
            import logging as _lg
            _lg.getLogger(__name__).warning(f"[challan_guard] query failed: {_guard_e}")
            pass  # guard is best-effort — DB unique index is the hard stop

        # ── INHOUSE job readiness gate ────────────────────────────────────
        # For every INHOUSE order line being billed, validate:
        #   - blank is allocated
        #   - surfacing_data is saved
        #   - job is at a billable stage (READY_FOR_PACK / READY_TO_BILL / FITTING_DONE)
        # This runs BEFORE the transaction so a bad line blocks the entire challan.
        if line_ids:
            try:
                from modules.sql_adapter import run_query as _rq_vbr
                _inhouse_lids = _rq_vbr("""
                    SELECT ol.id::text AS line_id
                    FROM order_lines ol
                    WHERE ol.id = ANY(%(lids)s::uuid[])
                      AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                """, {"lids": line_ids})
                _inhouse_ids = [r["line_id"] for r in (_inhouse_lids or [])]

                _block_msgs = []
                for _ih_lid in _inhouse_ids:
                    _vbr = _rq_vbr(
                        "SELECT * FROM public.validate_billing_readiness(%(lid)s::uuid)",
                        {"lid": _ih_lid}
                    )
                    if _vbr and not _vbr[0].get("is_ready"):
                        _reason = _vbr[0].get("block_reason") or "Not ready"
                        _block_msgs.append(f"Line {_ih_lid[:8]}…: {_reason}")

                if _block_msgs:
                    raise ValueError(
                        "❌ Cannot create challan — INHOUSE job(s) not ready:\n"
                        + "\n".join(f"  • {m}" for m in _block_msgs)
                    )
            except ValueError:
                raise
            except Exception as _vbr_e:
                import logging as _lg_vbr
                _lg_vbr.getLogger(__name__).warning(
                    f"[billing_readiness] check failed (non-blocking): {_vbr_e}"
                )
                # Gate failure is logged but not blocking — DB constraints are the hard stop

        # ── Challan number + all writes in ONE transaction ────────────────
        # run_transaction_fn passes (conn, cursor) so the doc-number FOR UPDATE
        # lock and the INSERT share the same transaction.
        # If any step fails → rollback → number never consumed. Zero gap.
        challan_id = str(_uuid.uuid4())

        # Allocate challan number — will be merged into tx_steps below
        # using a SQL-level approach: the registry UPDATE is prepended to tx_steps
        # so number alloc and INSERT happen in ONE transaction (run_transaction).
        try:
            from modules.db.order_number_registry import alloc_doc_number
            challan_no = alloc_doc_number("CHALLAN")
        except Exception as _cn_e:
            import uuid as _u_ch, datetime as _dt_ch
            challan_no = f"CH/{_dt_ch.date.today().strftime('%Y%m%d')}/{_u_ch.uuid4().hex[:6].upper()}"
        # NOTE: alloc_doc_number opens its own short transaction for the number only.
        # The challan INSERT below goes into run_transaction (a separate transaction).
        # This is a known two-commit pattern — acceptable here because:
        #   - challans are created by backoffice staff, not high-concurrency automated saves
        #   - the number commit is atomic; if the INSERT fails, the gap is logged
        # Full single-transaction refactor is a future improvement (CHALLAN-TX-001).

        # ── All writes (header + lines + billed_qty + ledger) go into one
        # transaction list so the entire challan is atomic.  If lines fail,
        # the header is never committed — no empty orphan challans.
        grand_total = total_amount + total_tax

        # Resolve party_id: "name:Vijay wadekar" keys have no UUID
        real_party_id = None if (not party_id or party_id.startswith("name:")) else party_id

        # Step 0: challan header is the first item in the transaction
        tx_steps: list = [("""
            INSERT INTO challans
            (id, challan_no, party_id, order_ids, challan_date,
             total_amount, total_tax, grand_total, status, created_by, remarks)
            VALUES (%(id)s, %(challan_no)s, %(party_id)s, %(order_ids)s,
                    %(challan_date)s, %(total_amount)s, %(total_tax)s,
                    %(grand_total)s, %(status)s, %(created_by)s, %(remarks)s)
        """, {
            "id":           challan_id,
            "challan_no":   challan_no,
            "party_id":     real_party_id,
            "order_ids":    order_ids,
            "challan_date": date.today(),
            "total_amount": total_amount,
            "total_tax":    total_tax,
            "grand_total":  grand_total,
            "status":       "PENDING",
            "created_by":   st.session_state.get("user_name", "System"),
            "remarks":      remarks,
        })]

        # Resolve order_ids: accept UUIDs or order_no strings (e.g. "R/2526/0001")
        _UUID_RE = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I
        )
        _resolved_order_ids = []
        for _raw_oid in order_ids:
            _raw_oid = str(_raw_oid or "").strip()
            if _UUID_RE.match(_raw_oid):
                _resolved_order_ids.append(_raw_oid)
            elif _raw_oid:
                # order_no string — resolve to UUID
                try:
                    _uuid_rows = _q(
                        "SELECT id::text FROM orders WHERE order_no = %(n)s LIMIT 1",
                        {"n": _raw_oid}
                    )
                    if _uuid_rows:
                        _resolved_order_ids.append(str(_uuid_rows[0]["id"]))
                except Exception:
                    pass  # skip unresolvable

        # Deduplicate while preserving order
        _seen = set()
        _deduped = []
        for _oid in _resolved_order_ids:
            if _oid not in _seen:
                _seen.add(_oid); _deduped.append(_oid)

        for order_id in _deduped:
            if line_ids:
                line_filter = "AND ol.id = ANY(%(lids)s::uuid[])"
                line_params = {"order_id": order_id, "lids": line_ids}
            else:
                line_filter = ""
                line_params = {"order_id": order_id}

            order_lines = _q(f"""
                SELECT ol.id,
                       ol.product_id,
                       o.order_type,
                       COALESCE(p.product_name, 'Service')     AS product_name,
                       COALESCE(p.brand, '')                  AS brand,
                       COALESCE(ol.billing_qty, ol.quantity)  AS quantity,
                       ol.unit_price,
                       0                                      AS product_selling_price,
                       0                                      AS product_mrp,
                       ROUND(ol.unit_price * COALESCE(ol.billing_qty, ol.quantity) - COALESCE(ol.discount_amount, 0), 2) AS total_price,
                       ol.gst_percent,
                       COALESCE(ol.gst_amount, ROUND(ol.unit_price * COALESCE(ol.billing_qty, ol.quantity) * COALESCE(ol.gst_percent, 0) / 100, 2)) AS gst_amount,
                       ol.eye_side,
                       ol.sph, ol.cyl, ol.axis, ol.add_power,
                       COALESCE(ol.is_service_line, FALSE)    AS is_service_line
                FROM order_lines ol
                JOIN  orders o    ON o.id  = ol.order_id
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id = %(order_id)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND (
                      -- Regular lines: not yet fully billed
                      COALESCE(ol.billed_qty, 0) < ol.quantity
                      OR
                      -- Service lines: always include (no stock gate)
                      COALESCE(ol.is_service_line, FALSE) = TRUE
                      OR UPPER(COALESCE(ol.eye_side, '')) IN ('S', 'SERVICE')
                  )
                  -- But never include already-fully-billed service lines
                  AND NOT (
                      (COALESCE(ol.is_service_line, FALSE) = TRUE
                       OR UPPER(COALESCE(ol.eye_side,'')) IN ('S','SERVICE'))
                      AND COALESCE(ol.billed_qty, 0) >= ol.quantity
                  )
                {line_filter}
            """, line_params)

            for line in order_lines:
                line_qty   = int(line.get("quantity") or 0)
                base_price = float(line.get("total_price") or 0)
                gst_amt    = float(line.get("gst_amount") or 0)
                order_type = str(line.get("order_type") or "WHOLESALE").upper()

                # Price integrity check for wholesale orders
                if order_type == "WHOLESALE":
                    _sp  = float(line.get("product_selling_price") or 0)
                    _mrp = float(line.get("product_mrp") or 0)
                    _up  = float(line.get("unit_price") or 0)
                    if _sp <= 0 and _mrp > 0 and abs(_up - _mrp) < 0.01:
                        import streamlit as _st
                        _st.warning(
                            f"⚠️ **{line.get('product_name','')}** — "
                            f"trade price (selling_price) not set; "
                            f"challan is using MRP ₹{_mrp:,.2f}. "
                            f"Update the product master to set correct wholesale price."
                        )
                if order_type == "RETAIL":
                    line_total = round(base_price, 2)
                else:
                    line_total = round(base_price + gst_amt, 2)

                # 0) Ensure unique index exists on challan_lines (idempotent DDL)
                # ON CONFLICT needs this — CREATE IF NOT EXISTS is safe
                tx_steps.insert(0, ("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_indexes
                            WHERE tablename='challan_lines'
                              AND indexname='challan_lines_challan_order_line_idx'
                        ) THEN
                            CREATE UNIQUE INDEX challan_lines_challan_order_line_idx
                            ON challan_lines(challan_id, order_line_id)
                            WHERE COALESCE(is_deleted, FALSE) = FALSE;
                        END IF;
                    END
                    $$
                """, None))

                # 1) Immutable snapshot in challan_lines
                # challan_lines actual cols: challan_id, order_id, order_line_id,
                # product_name, brand, eye_side, quantity, unit_price, total_price, line_total
                tx_steps.append(("""
                        INSERT INTO challan_lines
                        (challan_id, order_id, order_line_id,
                         product_name, brand, eye_side,
                         quantity, unit_price, total_price, line_total)
                        SELECT %(challan_id)s, %(order_id)s::uuid,
                               %(order_line_id)s::uuid,
                               %(product_name)s, %(brand)s, %(eye_side)s,
                               %(quantity)s, %(unit_price)s, %(total_price)s, %(line_total)s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM challan_lines
                            WHERE challan_id    = %(challan_id)s
                              AND order_line_id = %(order_line_id)s::uuid
                              AND COALESCE(is_deleted, FALSE) = FALSE
                        )
                    """, {
                        "challan_id":    challan_id,
                        "order_id":      order_id,
                        "order_line_id": line.get("id"),
                        "product_name":  line.get("product_name") or "",
                        "brand":         line.get("brand") or "",
                        "eye_side":      line.get("eye_side") or "",
                        "quantity":      line_qty,
                        "unit_price":    float(line.get("unit_price") or 0),
                        "total_price":   base_price,
                        "line_total":    line_total,
                    }))

                # 2) Over-billing guard
                _is_svc = bool(line.get("is_service_line")) or                            str(line.get("eye_side","")).upper() in ("S","SERVICE")
                _ceil_col = "COALESCE(allocated_qty, quantity)" if _is_svc else "quantity"
                guard = _q(f"""
                    SELECT COALESCE(billed_qty, 0) + %(qty)s <= {_ceil_col} AS allowed
                    FROM order_lines
                    WHERE id = %(line_id)s::uuid
                """, {"qty": line_qty, "line_id": line.get("id")})
                if not guard or not guard[0].get("allowed"):
                    if not _is_svc:
                        raise Exception(
                            f"Over-billing prevented: line {line.get('id')} "
                            f"— billed qty would exceed ordered quantity."
                        )
                    # Service line: allow anyway (auto-allocated)

                tx_steps.append((f"""
                    UPDATE order_lines
                    SET billed_qty = COALESCE(billed_qty, 0) + %(qty)s
                    WHERE id = %(line_id)s::uuid
                      AND COALESCE(billed_qty, 0) + %(qty)s <= {_ceil_col}
                """, {
                    "qty":     line_qty,
                    "line_id": line.get("id"),
                }))

                # 3) Document ledger row
                tx_steps.append(("""
                    INSERT INTO document_ledger
                        (doc_type, doc_id, doc_no, order_id, order_line_id,
                         party_id, product_id,
                         quantity, base_amount, tax_amount, total_amount)
                    VALUES
                        (%(dt)s, %(did)s::uuid, %(dno)s,
                         %(oid)s::uuid, %(olid)s::uuid,
                         %(pid)s, %(prid)s::uuid,
                         %(qty)s, %(base)s, %(tax)s, %(total)s)
                """, {
                    "dt":    "CHALLAN",
                    "did":   challan_id,
                    "dno":   challan_no or "",
                    "oid":   order_id,
                    "olid":  str(line.get("id") or ""),
                    "pid":   real_party_id,
                    "prid":  str(line.get("product_id") or ""),
                    "qty":   line_qty,
                    "base":  base_price,
                    "tax":   gst_amt,
                    "total": line_total,
                }))

        # Final step: recompute challan totals from actual written lines.
        # This corrects any mismatch between the UI-computed total (which may
        # include lines that the DB re-query filtered out as already-billed)
        # and the lines actually committed to challan_lines.
        # total_amount = sum of taxable base (total_price per line)
        # total_tax    = grand_total - total_amount
        # grand_total  = sum of line_total (tax-inclusive for retail, ex-tax for wholesale)
        tx_steps.append(("""
            UPDATE challans SET
                total_amount = COALESCE((
                    SELECT SUM(COALESCE(total_price, line_total, 0))
                    FROM challan_lines
                    WHERE challan_id = %(cid)s::uuid
                      AND NOT COALESCE(is_deleted, FALSE)
                ), 0),
                grand_total = COALESCE((
                    SELECT SUM(COALESCE(line_total, total_price, 0))
                    FROM challan_lines
                    WHERE challan_id = %(cid)s::uuid
                      AND NOT COALESCE(is_deleted, FALSE)
                ), 0),
                total_tax = COALESCE((
                    SELECT SUM(COALESCE(line_total, 0)) -
                           SUM(COALESCE(total_price, COALESCE(line_total, 0)))
                    FROM challan_lines
                    WHERE challan_id = %(cid)s::uuid
                      AND NOT COALESCE(is_deleted, FALSE)
                ), 0)
            WHERE id = %(cid)s::uuid
        """, {"cid": challan_id}))

        # Execute the entire challan atomically — header + all lines together
        ok = _run_transaction(tx_steps)
        if not ok:
            raise Exception("Transaction failed — challan not committed.")

        # ── Write service charges snapshot + lock charges ─────────────────
        # Done AFTER main transaction (challan now exists) so FK is valid.
        # Each charge is snapshotted in challan_service_charges (immutable record)
        # and the source row is locked so it cannot be deleted post-challan.
        if svc_charges:
            for sc in svc_charges:
                _write("""
                    INSERT INTO challan_service_charges
                        (challan_id, order_id, charge_type, description,
                         base_amount, gst_percent, gst_amount, total_amount)
                    VALUES
                        (%(cid)s::uuid, %(oid)s::uuid, %(ct)s, %(desc)s,
                         %(base)s, %(gst_pct)s, %(gst_amt)s, %(total)s)
                """, {
                    "cid":     challan_id,
                    "oid":     str(sc.get("order_id") or ""),
                    "ct":      sc.get("charge_type", "MISC"),
                    "desc":    sc.get("description") or "",
                    "base":    float(sc.get("amount")      or 0),
                    "gst_pct": float(sc.get("gst_percent") or 0),
                    "gst_amt": float(sc.get("gst_amount")  or 0),
                    "total":   float(sc.get("total_amount") or 0),
                })
                # Lock source row — delete_charge() checks is_locked before allowing delete
                _write("""
                    UPDATE order_charges
                    SET is_locked = TRUE, challan_id = %(cid)s::uuid
                    WHERE id = %(scid)s::uuid
                """, {"cid": challan_id, "scid": str(sc.get("id") or "")})

        # ── Reconcile grand_total to include service charges ─────────────
        # Service charges are written after the main transaction.
        # Update challan totals to include them so the stored grand_total
        # matches what's actually on the challan.
        if svc_charges:
            _write("""
                UPDATE challans SET
                    grand_total = COALESCE((
                        SELECT SUM(COALESCE(line_total, total_price, 0))
                        FROM challan_lines
                        WHERE challan_id = %(cid)s::uuid
                          AND NOT COALESCE(is_deleted, FALSE)
                    ), 0) + COALESCE((
                        SELECT SUM(COALESCE(total_amount, 0))
                        FROM challan_service_charges
                        WHERE challan_id = %(cid)s::uuid
                    ), 0),
                    total_amount = COALESCE((
                        SELECT SUM(COALESCE(total_price, line_total, 0))
                        FROM challan_lines
                        WHERE challan_id = %(cid)s::uuid
                          AND NOT COALESCE(is_deleted, FALSE)
                    ), 0) + COALESCE((
                        SELECT SUM(COALESCE(base_amount, 0))
                        FROM challan_service_charges
                        WHERE challan_id = %(cid)s::uuid
                    ), 0),
                    total_tax = COALESCE((
                        SELECT SUM(COALESCE(gst_amount, 0))
                        FROM challan_service_charges
                        WHERE challan_id = %(cid)s::uuid
                    ), 0) + COALESCE((
                        SELECT SUM(COALESCE(line_total, 0)) -
                               SUM(COALESCE(total_price, COALESCE(line_total, 0)))
                        FROM challan_lines
                        WHERE challan_id = %(cid)s::uuid
                          AND NOT COALESCE(is_deleted, FALSE)
                    ), 0)
                WHERE id = %(cid)s::uuid
            """, {"cid": challan_id})

        # Order status advancement to BILLED is handled by the DB trigger
        # auto_close_order_on_billing() — no Python call needed here.
        return challan_no

    except Exception as e:
        st.error(f"Failed to create challan: {e}")
        return None


def create_invoice(challan_id: Optional[str], party_id: str, 
                 order_ids: List[str], total_amount: float, 
                 total_tax: float, due_days: int = 30,
                 remarks: str = "") -> Optional[str]:
    """Create a new invoice (from challan or direct)
    
    RULE: RETAIL orders MUST have a challan_id — direct invoice not allowed.
    WHOLESALE orders may use direct invoice if billing_preference=DIRECT_INVOICE.
    """
    try:
        # ── RETAIL GATE: must have challan ────────────────────────────────
        if order_ids and not challan_id:
            retail_orders = _q("""
                SELECT id FROM orders
                WHERE id = ANY(%s::uuid[])
                  AND order_type = 'RETAIL'
                LIMIT 1
            """, (order_ids,))
            if retail_orders:
                import logging as _log_inv
                _log_inv.getLogger(__name__).warning(
                    f"[Invoice] Blocked direct invoice for RETAIL orders: {order_ids}"
                )
                return None  # Caller shows error
        # ─────────────────────────────────────────────────────────────────
        # Generate invoice number
        try:
            from modules.db.order_number_registry import alloc_doc_number
            invoice_no = alloc_doc_number("INVOICE")
        except Exception as _in_e:
            import uuid as _u_inv, datetime as _dt_inv
            invoice_no = f"INV/{_dt_inv.date.today().strftime('%Y%m%d')}/{_u_inv.uuid4().hex[:6].upper()}"
        
        # Create invoice record
        invoice_id = str(_uuid.uuid4())
        grand_total = total_amount + total_tax
        due_date = date.today() + timedelta(days=due_days)

        # ── Pre-fill amount_paid from advances already collected at order punch time ──
        # Advances are stored in payments with advance_for_order_id = order.id.
        # The invoice must reflect these so balance_due is correct from day 1.
        _adv_rows = _q("""
            SELECT COALESCE(SUM(amount), 0) AS tot
            FROM payments
            WHERE advance_for_order_id::text = ANY(%(oids)s)
              AND payment_type = 'ADVANCE'
              AND COALESCE(is_deleted, FALSE) = FALSE
        """, {"oids": [str(o) for o in order_ids]}) if order_ids else []
        _advance_paid    = round(float((_adv_rows[0]["tot"] if _adv_rows else 0) or 0), 2)
        _balance_due     = round(max(grand_total - _advance_paid, 0), 2)
        _payment_status  = "PAID" if _balance_due <= 0.01 else ("PARTIAL" if _advance_paid > 0 else "UNPAID")

        success = _write("""
            INSERT INTO invoices 
            (id, invoice_no, challan_id, party_id, order_ids, 
             invoice_date, due_date, total_amount, total_tax, grand_total,
             amount_paid, balance_due,
             status, payment_status, created_by, remarks,
             tally_synced)
            VALUES (%(id)s, %(invoice_no)s, %(challan_id)s, %(party_id)s, 
                    %(order_ids)s, %(invoice_date)s, %(due_date)s, 
                    %(total_amount)s, %(total_tax)s, %(grand_total)s,
                    %(amount_paid)s, %(balance_due)s,
                    %(status)s, %(payment_status)s, %(created_by)s, %(remarks)s,
                    FALSE)
        """, {
            "id":             invoice_id,
            "invoice_no":     invoice_no,
            "challan_id":     challan_id,
            "party_id":       party_id,
            "order_ids":      order_ids,
            "invoice_date":   date.today(),
            "due_date":       due_date,
            "total_amount":   total_amount,
            "total_tax":      total_tax,
            "grand_total":    grand_total,
            "amount_paid":    _advance_paid,
            "balance_due":    _balance_due,
            "status":         "PENDING",
            "payment_status": _payment_status,
            "created_by":     st.session_state.get("user_name", "System"),
            "remarks":        remarks,
        })
        
        if success:
            # Create invoice line items
            if challan_id:
                # From challan lines
                # Read directly from challan_lines snapshot — no joins needed
                # This preserves immutability: product/price changes don't affect old invoices
                challan_lines = _q("""
                    SELECT cl.id, cl.order_id, cl.order_line_id,
                           cl.product_name, cl.brand,
                           COALESCE(cl.eye_side, ol.eye_side, '') AS eye_side,
                           cl.quantity, cl.unit_price, cl.total_price,
                           cl.line_total,
                           COALESCE(ol.gst_percent, cl.gst_percent, 0) AS gst_percent,
                           ROUND(cl.line_total * COALESCE(ol.gst_percent, cl.gst_percent, 0)
                                 / (100 + COALESCE(ol.gst_percent, cl.gst_percent, 0)), 2)
                                 AS gst_amount,
                           ol.product_id,
                           ol.sph, ol.cyl, ol.axis, ol.add_power
                    FROM challan_lines cl
                    LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
                    WHERE cl.challan_id = %(challan_id)s
                      AND COALESCE(cl.is_deleted, FALSE) = FALSE
                """, {"challan_id": challan_id})

                for line in challan_lines:
                    base  = float(line.get("total_price") or 0)
                    _price = compute_line_totals(
                        quantity      = int(line.get("quantity") or 0),
                        box_size      = 1,   # challan_lines already stores per-pcs price
                        selling_price = line.get("unit_price") or 0,
                        mrp           = line.get("unit_price") or 0,
                        purchase_rate = line.get("unit_price") or 0,
                        order_type    = "WHOLESALE",
                        gst_percent   = line.get("gst_percent") or 0,
                    )
                    tax   = _price["tax"]
                    total = float(line.get("line_total") or 0) or _price["total"]
                    _write("""
                        INSERT INTO invoice_lines
                        (invoice_id, challan_line_id, order_id, order_line_id,
                         product_name, brand,
                         quantity, unit_price, total_price,
                         tax_amount, tax_rate, line_total)
                        VALUES (%(invoice_id)s, %(challan_line_id)s, %(order_id)s,
                                %(order_line_id)s,
                                %(product_name)s, %(brand)s,
                                %(quantity)s, %(unit_price)s, %(total_price)s,
                                %(tax_amount)s, %(tax_rate)s, %(line_total)s)
                        ON CONFLICT DO NOTHING
                    """, {
                        "invoice_id":       invoice_id,
                        "challan_line_id":  line.get("id"),
                        "order_id":         line.get("order_id"),
                        "order_line_id":    line.get("order_line_id"),
                        "product_name":     line.get("product_name") or "",
                        "brand":            line.get("brand") or "",
                        "quantity":         line.get("quantity") or 0,
                        "unit_price":       float(line.get("unit_price") or 0),
                        "total_price":      base,
                        "tax_amount":       tax,
                        "tax_rate":         float(line.get("gst_percent") or 0),
                        "line_total":       total,
                    })
                    # Append to document ledger — invoice event
                    _write_document_ledger(
                        doc_type     = "INVOICE",
                        doc_id       = invoice_id,
                        doc_no       = invoice_no or "",
                        order_id     = str(line.get("order_id") or ""),
                        order_line_id= str(line.get("order_line_id") or ""),
                        party_id     = party_id,
                        product_id   = str(line.get("product_id") or ""),
                        quantity     = int(line.get("quantity") or 0),
                        base_amount  = base,
                        tax_amount   = tax,
                        total_amount = total,
                    )
            else:
                # Direct invoice from orders — snapshot all financial fields
                for order_id in order_ids:
                    order_lines = _q("""
                        SELECT ol.id,
                               ol.product_id,
                               o.order_type,
                               COALESCE(p.product_name, 'Lens')       AS product_name,
                               COALESCE(p.brand, '')                  AS brand,
                               ol.quantity,
                               ol.unit_price,
                               ROUND(ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0), 2) AS total_price,
                               ol.gst_percent,
                               COALESCE(ol.gst_amount, ROUND(ol.unit_price * ol.quantity * COALESCE(ol.gst_percent, 0) / 100, 2)) AS gst_amount,
                               ol.eye_side,
                               ol.sph, ol.cyl, ol.axis, ol.add_power
                        FROM order_lines ol
                        JOIN  orders o    ON o.id  = ol.order_id
                        LEFT JOIN products p ON p.id = ol.product_id
                        WHERE ol.order_id = %(order_id)s::uuid
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                    """, {"order_id": order_id})

                    for line in order_lines:
                        base  = float(line.get("total_price") or 0)
                        tax   = float(line.get("gst_amount") or 0)
                        ot    = str(line.get("order_type") or "WHOLESALE").upper()
                        total = round(base, 2) if ot == "RETAIL" else round(base + tax, 2)
                        _write("""
                            INSERT INTO invoice_lines
                            (invoice_id, order_id, order_line_id,
                             product_name, brand,
                             quantity, unit_price, total_price,
                             tax_amount, tax_rate, line_total)
                            VALUES (%(invoice_id)s, %(order_id)s::uuid, %(order_line_id)s::uuid,
                                    %(product_name)s, %(brand)s,
                                    %(quantity)s, %(unit_price)s, %(total_price)s,
                                    %(tax_amount)s, %(tax_rate)s, %(line_total)s)
                            ON CONFLICT DO NOTHING
                        """, {
                            "invoice_id":    invoice_id,
                            "order_id":      order_id,
                            "order_line_id": line.get("id"),
                            "product_name":  line.get("product_name") or "",
                            "brand":         line.get("brand") or "",
                            "quantity":      line.get("quantity") or 0,
                            "unit_price":    float(line.get("unit_price") or 0),
                            "total_price":   base,
                            "tax_amount":    tax,
                            "tax_rate":      float(line.get("gst_percent") or 0),
                            "line_total":    total,
                        })
                        # Append to document ledger — direct invoice event
                        _write_document_ledger(
                            doc_type     = "INVOICE",
                            doc_id       = invoice_id,
                            doc_no       = invoice_no or "",
                            order_id     = order_id,
                            order_line_id= str(line.get("id") or ""),
                            party_id     = party_id,
                            product_id   = str(line.get("product_id") or ""),
                            quantity     = int(line.get("quantity") or 0),
                            base_amount  = base,
                            tax_amount   = tax,
                            total_amount = total,
                        )

            # ── Party ledger DEBIT: invoice raised ──────────────────────
            try:
                _by_ldr = st.session_state.get("user_name", "System")
                _pn_rows = _q("SELECT party_name FROM parties WHERE id=%s::uuid LIMIT 1",
                              (party_id,)) if party_id else []
                _pn_ldr = (_pn_rows[0]["party_name"] if _pn_rows else "") or ""
                _write_party_ledger_debit(
                    party_id   = party_id,
                    party_name = _pn_ldr,
                    doc_type   = "INVOICE",
                    doc_id     = invoice_id,
                    doc_no     = invoice_no or "",
                    amount     = grand_total,
                    doc_date   = date.today(),
                    narration  = f"Invoice raised — {invoice_no}",
                    created_by = _by_ldr,
                )
            except Exception as _lde:
                import logging; logging.getLogger(__name__).warning(f"[ledger debit] {_lde}")

            # ── Lock job stages after invoice ────────────────────────────
            # Calls set_job_billed_lock() on all INHOUSE job_master rows
            # linked to lines on this invoice — prevents stage rollback after billing.
            try:
                from modules.sql_adapter import run_query as _rq_lock_jb, run_scalar as _rs_lock_jb
                _lock_lids = []
                if challan_id:
                    _cl_lock = _rq_lock_jb(
                        "SELECT order_line_id::text FROM challan_lines "
                        "WHERE challan_id = %(cid)s AND NOT COALESCE(is_deleted, FALSE)",
                        {"cid": challan_id}
                    )
                    _lock_lids = [r["order_line_id"] for r in (_cl_lock or [])]
                elif order_ids:
                    _ol_lock = _rq_lock_jb(
                        "SELECT ol.id::text FROM order_lines ol "
                        "WHERE ol.order_id = ANY(%(oids)s::uuid[]) "
                        "AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE' "
                        "AND NOT COALESCE(ol.is_deleted, FALSE)",
                        {"oids": order_ids}
                    )
                    _lock_lids = [r["id"] for r in (_ol_lock or [])]

                for _lock_lid in _lock_lids:
                    _jm_rows = _rq_lock_jb(
                        "SELECT id::text FROM job_master WHERE order_line_id = %(lid)s::uuid LIMIT 1",
                        {"lid": _lock_lid}
                    )
                    if _jm_rows:
                        try:
                            _rs_lock_jb(
                                "SELECT public.set_job_billed_lock(%(jid)s::uuid)",
                                {"jid": _jm_rows[0]["id"]}
                            )
                        except Exception: pass
            except Exception as _jbl_e:
                import logging; logging.getLogger(__name__).warning(
                    f"[set_job_billed_lock] {_jbl_e}"
                )

            # ── Auto-post accounting JV ───────────────────────────
            try:
                from modules.accounting.accounts_engine import post_invoice_jv
                import datetime as _dt
                _taxable = float(grand_total) - sum(
                    float(l.get("tax_amount", 0) or 0) for l in
                    (_q("SELECT tax_amount FROM invoice_lines WHERE invoice_id=%s::uuid",
                        (invoice_id,)) or [])
                )
                _tax = float(grand_total) - _taxable
                post_invoice_jv(
                    invoice_no   = invoice_no or "",
                    invoice_id   = invoice_id,
                    party_name   = _pn_ldr or "",
                    grand_total  = float(grand_total or 0),
                    taxable      = _taxable,
                    tax_amount   = _tax,
                    order_type   = "WHOLESALE",
                    voucher_date = _dt.date.today(),
                    created_by   = _by_ldr or "System",
                )
            except Exception as _jve:
                import logging; logging.getLogger(__name__).warning(f"[JV] invoice: {_jve}")

            return invoice_no

        return None

    except Exception as e:
        st.error(f"Failed to create invoice: {e}")
        return None


def get_pending_challans() -> List[Dict]:
    """Get challans that can be invoiced"""
    return _q("""
        SELECT c.*, COALESCE(p.party_name, c.party_id::text) as party_name,
               p.mobile, p.billing_preference,
               array_length(c.order_ids, 1) as order_count
        FROM challans c
        LEFT JOIN parties p ON p.id = c.party_id
        WHERE c.status = 'PENDING'
        ORDER BY c.challan_date DESC
    """)


def get_challan_details(challan_id: str) -> Dict:
    """Get detailed challan information with line items"""
    # LEFT JOIN parties — for patient orders party_id is in patients table
    # so we fall back to order party_name if parties JOIN returns nothing
    challan = _q("""
        SELECT c.*,
               COALESCE(p.party_name, c.remarks, '') AS party_name,
               COALESCE(p.mobile,  '') AS mobile,
               COALESCE(p.address, '') AS address,
               COALESCE(p.gstin,   '') AS gstin
        FROM challans c
        LEFT JOIN parties p ON p.id = c.party_id
        WHERE c.id = %(challan_id)s
    """, {"challan_id": challan_id})

    if not challan:
        return {}

    challan_data = challan[0]

    # Enrich party_name from orders if still blank (patient orders)
    if not challan_data.get("party_name"):
        ords = _q("""
            SELECT party_name FROM orders
            WHERE id = ANY(%(oids)s::uuid[])
            LIMIT 1
        """, {"oids": challan_data.get("order_ids") or []})
        if ords:
            challan_data["party_name"] = ords[0].get("party_name", "")

    # Read lines — include rows even if order_line_id is null (direct inserts)
    challan_data["lines"] = _q("""
        SELECT cl.*, o.order_no, o.created_at AS order_date
        FROM challan_lines cl
        LEFT JOIN orders o ON o.id = cl.order_id
        WHERE cl.challan_id = %(challan_id)s
          AND COALESCE(cl.is_deleted, FALSE) = FALSE
        ORDER BY cl.id
    """, {"challan_id": challan_id})

    return challan_data


def get_invoice_details(invoice_id: str) -> Dict:
    """Get detailed invoice information with line items"""
    invoice = _q("""
        SELECT i.*,
               COALESCE(p.party_name, '') AS party_name,
               COALESCE(p.mobile,  '')   AS mobile,
               COALESCE(p.address, '')   AS address,
               COALESCE(p.gstin,   '')   AS gstin,
               c.challan_no
        FROM invoices i
        LEFT JOIN parties p ON p.id = i.party_id
        LEFT JOIN challans c ON c.id = i.challan_id
        WHERE i.id = %(invoice_id)s
    """, {"invoice_id": invoice_id})

    if not invoice:
        return {}

    invoice_data = invoice[0]

    # Enrich party_name from orders if blank (patient orders)
    if not invoice_data.get("party_name"):
        ords = _q("""
            SELECT party_name FROM orders
            WHERE id = ANY(%(oids)s::uuid[])
            LIMIT 1
        """, {"oids": invoice_data.get("order_ids") or []})
        if ords:
            invoice_data["party_name"] = ords[0].get("party_name", "")

    # Lines — include all non-deleted lines
    invoice_data["lines"] = _q("""
        SELECT il.*, o.order_no, o.created_at AS order_date
        FROM invoice_lines il
        LEFT JOIN orders o ON o.id = il.order_id
        WHERE il.invoice_id = %(invoice_id)s
          AND COALESCE(il.is_deleted, FALSE) = FALSE
        ORDER BY il.id
    """, {"invoice_id": invoice_id})

    return invoice_data


def update_challan_status(challan_id: str, status: str) -> bool:
    """Update challan status"""
    return _write("""
        UPDATE challans SET status = %(status)s 
        WHERE id = %(challan_id)s
    """, {"challan_id": challan_id, "status": status})


def update_invoice_status(invoice_id: str, status: str,
                       payment_status: str = None) -> bool:
    """
    Update invoice status (e.g. PENDING→ACTIVE).
    payment_status is recalculated from payments.invoice_id FK — never
    accepted as a parameter to avoid stale data drift.
    """
    return _write("""
        UPDATE invoices SET
            status = %(status)s,
            -- Recalculate payment_status from FK (relational truth)
            amount_paid = COALESCE((
                SELECT SUM(p.amount) FROM payments p
                WHERE p.invoice_id = %(id)s::uuid
                  AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0),
            balance_due = GREATEST(COALESCE(grand_total,0) - COALESCE((
                SELECT SUM(p.amount) FROM payments p
                WHERE p.invoice_id = %(id)s::uuid
                  AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0), 0),
            payment_status = CASE
                WHEN GREATEST(COALESCE(grand_total,0) - COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.invoice_id = %(id)s::uuid
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0), 0) <= 0.01 THEN 'PAID'
                WHEN COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.invoice_id = %(id)s::uuid
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0) > 0 THEN 'PARTIAL'
                ELSE 'UNPAID'
            END,
            updated_at = NOW()
        WHERE id = %(id)s::uuid
    """, {"id": invoice_id, "status": status})


# =====================================================
# UI RENDERING FUNCTIONS
# =====================================================

def render_challan_creation():
    """Two-panel challan creation: type+party left, multi-order+lines right."""
    import streamlit as st

    st.markdown("### 📋 Create Challan")

    col_left, col_right = st.columns([1, 2], gap="large")

    # ── LEFT: order type + party ──────────────────────────────────────────
    with col_left:
        st.markdown("#### 🔍 Filter")

        _ot_key = "ch_order_type"
        if _ot_key not in st.session_state:
            st.session_state[_ot_key] = "WHOLESALE"

        for _ot, _icon in [("WHOLESALE","🏪"), ("RETAIL","🛍️")]:
            _active = st.session_state[_ot_key] == _ot
            _bg  = "#0f1e38" if _active else "#0f172a"
            _brd = "#3b82f6" if _active else "#1e293b"
            st.markdown(
                f"<div style='background:{_bg};border:2px solid {_brd};border-radius:8px;"
                f"padding:8px 12px;margin-bottom:4px'>"
                f"<span style='color:#e2e8f0;font-weight:{"700" if _active else "400"}'>"
                f"{_icon} {_ot.capitalize()}</span></div>",
                unsafe_allow_html=True,
            )
            if st.button("✓ Active" if _active else "Select", key=f"ch_ot_{_ot}",
                         use_container_width=True, type="primary" if _active else "secondary"):
                st.session_state[_ot_key]       = _ot
                st.session_state["ch_sel_party"] = None
                st.session_state["ch_ord_sel"]   = {}
                st.session_state["ch_line_sel"]  = {}
                st.rerun()

        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
        st.markdown("#### 🏢 Party")

        _active_ot = st.session_state[_ot_key]

        registered = _q("""
            SELECT DISTINCT p.id::text AS party_key, p.party_name,
                   COALESCE(p.mobile,'') AS mobile
            FROM parties p
            JOIN orders o ON (o.party_id = p.id OR o.party_name = p.party_name)
            WHERE o.status IN ('CONFIRMED','READY','READY_FOR_BILLING','IN_PRODUCTION')
              AND UPPER(COALESCE(o.order_type,'WHOLESALE')) = %(ot)s
              AND NOT EXISTS (SELECT 1 FROM challans cx
                WHERE cx.status NOT IN ('CANCELLED','VOID')
                AND cx.order_ids IS NOT NULL AND o.id::text = ANY(cx.order_ids))
              AND NOT EXISTS (SELECT 1 FROM invoices ix
                WHERE ix.status NOT IN ('CANCELLED','VOID')
                AND ix.order_ids IS NOT NULL AND o.id::text = ANY(ix.order_ids))
            ORDER BY p.party_name
        """, {"ot": _active_ot})

        unregistered = _q("""
            SELECT DISTINCT 'name:' || o.party_name AS party_key,
                   o.party_name, '' AS mobile
            FROM orders o
            WHERE o.party_name IS NOT NULL AND o.party_name <> ''
              AND o.status IN ('CONFIRMED','READY','READY_FOR_BILLING','IN_PRODUCTION')
              AND UPPER(COALESCE(o.order_type,'WHOLESALE')) = %(ot)s
              AND NOT EXISTS (SELECT 1 FROM parties p WHERE p.party_name = o.party_name)
              AND NOT EXISTS (SELECT 1 FROM challans cx
                WHERE cx.status NOT IN ('CANCELLED','VOID')
                AND cx.order_ids IS NOT NULL AND o.id::text = ANY(cx.order_ids))
              AND NOT EXISTS (SELECT 1 FROM invoices ix
                WHERE ix.status NOT IN ('CANCELLED','VOID')
                AND ix.order_ids IS NOT NULL AND o.id::text = ANY(ix.order_ids))
            ORDER BY o.party_name
        """, {"ot": _active_ot})

        all_parties = registered + unregistered
        if "ch_sel_party" not in st.session_state:
            st.session_state["ch_sel_party"] = None

        if not all_parties:
            st.info(f"No {_active_ot.lower()} orders pending.")
        else:
            for p in all_parties:
                _pk   = p["party_key"]
                _pn   = p["party_name"]
                _mob  = p.get("mobile") or ""
                _issel = st.session_state["ch_sel_party"] == _pk
                _bg   = "#0f1e38" if _issel else "#0f172a"
                _brd  = "#10b981" if _issel else "#1e293b"
                st.markdown(
                    f"<div style='background:{_bg};border:2px solid {_brd};border-radius:8px;"
                    f"padding:8px 12px;margin-bottom:4px'>"
                    f"<div style='color:#e2e8f0;font-weight:600;font-size:0.83rem'>{_pn}</div>"
                    + (f"<div style='color:#64748b;font-size:0.7rem'>📱 {_mob}</div>" if _mob else "")
                    + "</div>", unsafe_allow_html=True)
                if st.button("✓ Selected" if _issel else "Select", key=f"ch_party_{_pk}",
                             use_container_width=True, type="primary" if _issel else "secondary"):
                    st.session_state["ch_sel_party"] = _pk
                    st.session_state["ch_ord_sel"]   = {}
                    st.session_state["ch_line_sel"]  = {}
                    st.rerun()

    # ── RIGHT: orders + lines ─────────────────────────────────────────────
    with col_right:
        _active_party = st.session_state.get("ch_sel_party")
        _active_ot    = st.session_state.get(_ot_key, "WHOLESALE")

        if not _active_party:
            st.markdown(
                "<div style='background:#0f172a;border:2px dashed #1e293b;border-radius:12px;"
                "padding:48px;text-align:center;color:#475569;margin-top:24px'>"
                "<div style='font-size:2rem'>👈</div>"
                "<div style='margin-top:8px'>Select order type and party</div></div>",
                unsafe_allow_html=True)
        else:
            pending_orders = get_pending_orders_for_party(_active_party)
            pending_orders = [o for o in pending_orders
                              if str(o.get("order_type") or "WHOLESALE").upper() == _active_ot]

            if not pending_orders:
                st.info(f"No pending {_active_ot.lower()} orders for this party.")
            else:
                _party_name = pending_orders[0].get("party_name","")
                st.markdown(f"#### 📦 Orders — {_party_name}")
                st.caption(f"{len(pending_orders)} order(s) · tick orders to include in challan · expand to review lines")

                if "ch_ord_sel" not in st.session_state:
                    st.session_state["ch_ord_sel"] = {}
                if "ch_line_sel" not in st.session_state:
                    st.session_state["ch_line_sel"] = {}

                # Select All Orders / Deselect All
                _oa, _ob = st.columns(2)
                with _oa:
                    if st.button("☑️ Select All Orders", key="ch_ord_all", use_container_width=True):
                        for o in pending_orders:
                            st.session_state["ch_ord_sel"][str(o["id"])] = True
                        st.rerun()
                with _ob:
                    if st.button("⬜ Deselect All", key="ch_ord_none", use_container_width=True):
                        st.session_state["ch_ord_sel"] = {}
                        st.session_state["ch_line_sel"] = {}
                        st.rerun()

                st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

                # Per-order collapsible rows
                _all_sel_lines  = {}   # lid → price_cache entry
                _all_order_ids  = []

                for o in pending_orders:
                    _oid    = str(o["id"])
                    _ono    = o.get("order_no","")
                    _amt    = f"₹{float(o.get('total_value') or 0):,.2f}"
                    _date   = str(o.get("created_at",""))[:10]
                    _is_ord_sel = st.session_state["ch_ord_sel"].get(_oid, False)

                    # Order row: checkbox + summary + expander toggle
                    _hc1, _hc2 = st.columns([0.07, 0.93])
                    with _hc1:
                        _ord_ticked = st.checkbox("", value=_is_ord_sel,
                                                  key=f"ch_ord_chk_{_oid}",
                                                  label_visibility="collapsed")
                    with _hc2:
                        _bg  = "#0d2035" if _ord_ticked else "#0f172a"
                        _brd = "#3b82f6" if _ord_ticked else "#1e293b"
                        st.markdown(
                            f"<div style='background:{_bg};border:2px solid {_brd};"
                            f"border-radius:8px;padding:8px 14px;margin-bottom:2px'>"
                            f"<span style='color:#60a5fa;font-weight:700;font-size:0.88rem'>{_ono}</span>"
                            f"<span style='color:#94a3b8;font-size:0.72rem;margin-left:12px'>📅 {_date}</span>"
                            f"<span style='color:#10b981;font-weight:700;float:right'>{_amt}</span>"
                            f"</div>", unsafe_allow_html=True)

                    if _ord_ticked != _is_ord_sel:
                        st.session_state["ch_ord_sel"][_oid] = _ord_ticked
                        if not _ord_ticked:
                            st.session_state["ch_line_sel"].pop(_oid, None)
                        st.rerun()

                    # Collapsible lines for this order
                    with st.expander(f"📋 Lines — {_ono}", expanded=_ord_ticked):
                        unbilled = get_unbilled_lines_for_order(_oid)
                        if not unbilled:
                            st.caption("✅ All lines already billed.")
                        else:
                            # Init line selection for this order
                            if _oid not in st.session_state["ch_line_sel"]:
                                st.session_state["ch_line_sel"][_oid] = [str(l["id"]) for l in unbilled]

                            _first_ot  = str((unbilled[0].get("order_type") or "WHOLESALE")).upper()
                            _is_retail = (_first_ot == "RETAIL")
                            _cur_sel   = set(st.session_state["ch_line_sel"].get(_oid, []))
                            _new_line_sel = []
                            _price_cache  = {}

                            for line in unbilled:
                                lid   = str(line["id"])
                                pname = line.get("product_name") or "Lens"
                                brand = line.get("brand") or ""
                                eye   = str(line.get("eye_side") or "")
                                sph_v = line.get("sph")
                                bq    = int(line.get("quantity") or 0)
                                bt    = float(line.get("billing_total") or line.get("total_price") or 0)
                                gst_p = float(line.get("gst_percent") or 0)

                                if _is_retail:
                                    base_e = round(bt/(1+gst_p/100),2) if gst_p else bt
                                    gst_a  = round(bt - base_e, 2)
                                    grand  = round(bt, 2)
                                else:
                                    gst_a  = float(line.get("gst_amount") or 0)
                                    grand  = round(bt + gst_a, 2)
                                    base_e = bt
                                _price_cache[lid] = {"base": base_e, "tax": gst_a, "total": grand}

                                eye_icon = {"R":"👁R","RIGHT":"👁R","L":"👁L","LEFT":"👁L"}.get(eye.upper(), f"👁{eye}" if eye else "👁")
                                sph_str  = f" SPH {float(sph_v):+.2f}" if sph_v else ""

                                _checked = lid in _cur_sel
                                _lc1, _lc2 = st.columns([0.08, 0.92])
                                with _lc1:
                                    _ticked = st.checkbox("", value=_checked,
                                                          key=f"ch_l_{_oid}_{lid}",
                                                          label_visibility="collapsed")
                                with _lc2:
                                    _rbg  = "#0d2035" if _ticked else "#0f172a"
                                    _rbrd = "#3b82f6" if _ticked else "#1e293b"
                                    st.markdown(
                                        f"<div style='background:{_rbg};border:1px solid {_rbrd};"
                                        f"border-radius:6px;padding:6px 10px;margin-bottom:2px'>"
                                        f"<span style='color:#a78bfa;font-size:0.78rem'>{eye_icon}</span>"
                                        f" <span style='color:#e2e8f0;font-size:0.78rem'>{pname}"
                                        + (f" <span style='color:#64748b'>{brand}</span>" if brand else "")
                                        + (f"<span style='color:#94a3b8'>{sph_str}</span>" if sph_str else "")
                                        + f"</span>"
                                        f"<span style='color:#64748b;font-size:0.7rem;margin-left:8px'>{bq} pcs</span>"
                                        f"<span style='color:#10b981;font-weight:600;float:right;font-size:0.78rem'>₹{grand:,.2f}</span>"
                                        f"</div>", unsafe_allow_html=True)
                                if _ticked:
                                    _new_line_sel.append(lid)

                            if set(_new_line_sel) != _cur_sel:
                                st.session_state["ch_line_sel"][_oid] = _new_line_sel
                                st.rerun()

                            # Mini subtotal for this order
                            _ord_total = sum(_price_cache[l]["total"] for l in _new_line_sel if l in _price_cache)
                            st.caption(f"Selected {len(_new_line_sel)}/{len(unbilled)} lines · ₹{_ord_total:,.2f}")

                    # Accumulate into challan totals if order is selected
                    if _ord_ticked:
                        unbilled_for_acc = get_unbilled_lines_for_order(_oid)
                        _sel_lids = set(st.session_state["ch_line_sel"].get(_oid, [str(l["id"]) for l in unbilled_for_acc]))
                        _first_ot  = str((unbilled_for_acc[0].get("order_type") or "WHOLESALE")).upper() if unbilled_for_acc else "WHOLESALE"
                        _is_retail = (_first_ot == "RETAIL")
                        for line in unbilled_for_acc:
                            lid  = str(line["id"])
                            if lid not in _sel_lids:
                                continue
                            bt   = float(line.get("billing_total") or line.get("total_price") or 0)
                            gst_p = float(line.get("gst_percent") or 0)
                            gst_a = float(line.get("gst_amount") or 0)
                            if _is_retail:
                                base_e = round(bt/(1+gst_p/100),2) if gst_p else bt
                                gst_a  = round(bt - base_e, 2)
                                grand  = round(bt, 2)
                            else:
                                grand  = round(bt + gst_a, 2)
                                base_e = bt
                            _all_sel_lines[lid] = {"base": base_e, "tax": gst_a, "total": grand}
                        _all_order_ids.append(_oid)

                # ── Challan summary + create button ───────────────────────
                _sel_ord_count = sum(1 for v in st.session_state["ch_ord_sel"].values() if v)
                if _sel_ord_count > 0 and _all_sel_lines:
                    st.markdown("---")
                    _ch_grand = round(sum(v["total"] for v in _all_sel_lines.values()), 2)
                    _ch_tax   = round(sum(v["tax"]   for v in _all_sel_lines.values()), 2)
                    _ch_base  = round(sum(v["base"]  for v in _all_sel_lines.values()), 2)
                    _tot_lines = len(_all_sel_lines)

                    _cs1, _cs2, _cs3, _cs4 = st.columns(4)
                    for _col, _lbl, _val, _clr in [
                        (_cs1, "Orders",     str(_sel_ord_count),          "#3b82f6"),
                        (_cs2, "Lines",      str(_tot_lines),               "#8b5cf6"),
                        (_cs3, "Taxable",    f"₹{_ch_base:,.2f}",           "#f59e0b"),
                        (_cs4, "Grand Total",f"₹{_ch_grand:,.2f}",          "#10b981"),
                    ]:
                        with _col:
                            st.markdown(
                                f"<div style='background:#0f172a;border:1px solid #1e293b;"
                                f"border-radius:8px;padding:8px;text-align:center'>"
                                f"<div style='color:#94a3b8;font-size:0.68rem'>{_lbl}</div>"
                                f"<div style='color:{_clr};font-weight:700;font-size:1.1rem'>{_val}</div>"
                                f"</div>", unsafe_allow_html=True)

                    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
                    remarks = st.text_input("Remarks", placeholder="Optional…", key="ch_remarks")

                    # ── Add service charges to challan total ──────────────
                    # Service charges split into two buckets by when the service
                    # was actually rendered:
                    #
                    #   CLINICAL (CONSULTATION, EYE_TESTING)
                    #       → already delivered at order time → goes on the FIRST
                    #         challan regardless of how many lines are selected.
                    #
                    #   LAB / LOGISTICS (FITTING, COLOURING, COURIER, MISC)
                    #       → delivered only when the full order is complete → goes
                    #         on the FINAL challan (when all unbilled lines are
                    #         cleared).  Partial challans get ₹0 for these.
                    #
                    # "Final" test:  selected_lines == total_unbilled_lines
                    #
                    # Non-lens product lines (frames, sunglasses, solutions,
                    # cleaners, accessories — anything whose main_group does NOT
                    # contain "lens" or "spectacle") are physical stock handed over
                    # at order time → treated the same as CLINICAL: first challan.
                    # Lens lines require lab processing → final challan only.
                    _CLINICAL_TYPES = {"CONSULTATION", "EYE_TESTING"}

                    def _is_lens_line(line: Dict) -> bool:
                        mg = str(line.get("main_group") or "").lower()
                        es = str(line.get("eye_side")   or "").upper()
                        # eye_side R/L/RIGHT/LEFT → optical lens line
                        # main_group contains "lens" or "spectacle" → lens line
                        return (
                            es in ("R", "L", "RIGHT", "LEFT")
                            or "lens" in mg
                            or "spectacle" in mg
                        )

                    _svc_base_ch = 0.0; _svc_tax_ch = 0.0; _svc_tot_ch = 0.0
                    _all_svc_rows: List[Dict] = []   # full rows for snapshot
                    try:
                        from modules.backoffice.order_charges_panel import fetch_charges
                        for _svc_oid in _all_order_ids:
                            # ── Is this the first / final challan for this order? ─
                            _unbilled_all  = get_unbilled_lines_for_order(_svc_oid)
                            _sel_lids_svc  = set(
                                st.session_state["ch_line_sel"].get(
                                    _svc_oid,
                                    [str(l["id"]) for l in _unbilled_all]
                                )
                            )

                            # "Final" is determined by lens lines only — non-lens
                            # products (frames, solutions, etc.) are on first challan
                            # regardless, so they don't gate the final-challan test.
                            _lens_lines        = [l for l in _unbilled_all if _is_lens_line(l)]
                            _total_lens        = len(_lens_lines)
                            _sel_lens_cnt      = sum(
                                1 for l in _lens_lines if str(l["id"]) in _sel_lids_svc
                            )
                            _is_first_challan  = not _q(
                                "SELECT 1 FROM challans WHERE %(oid)s = ANY(order_ids) LIMIT 1",
                                {"oid": _svc_oid}
                            )
                            _is_final_challan  = (
                                _total_lens > 0 and _sel_lens_cnt >= _total_lens
                            )

                            for _sc in (fetch_charges(_svc_oid) or []):
                                _ctype    = str(_sc.get("charge_type") or "").upper()
                                _clinical = _ctype in _CLINICAL_TYPES

                                # Clinical → include only on first challan for this order
                                # Lab/Logistics → include only on final challan
                                if _clinical and not _is_first_challan:
                                    continue
                                if not _clinical and not _is_final_challan:
                                    continue

                                _sc_snap = dict(_sc)   # shallow copy — don't mutate DB row
                                _sc_snap["order_id"] = _svc_oid
                                _all_svc_rows.append(_sc_snap)
                                _svc_base_ch += float(_sc.get("amount")       or 0)
                                _svc_tax_ch  += float(_sc.get("gst_amount")   or 0)
                                _svc_tot_ch  += float(_sc.get("total_amount") or 0)
                    except Exception:
                        pass
                    _svc_base_ch = round(_svc_base_ch, 2)
                    _svc_tax_ch  = round(_svc_tax_ch, 2)
                    _svc_tot_ch  = round(_svc_tot_ch, 2)
                    _ch_grand_incl = round(_ch_grand + _svc_tot_ch, 2)

                    # ── Deferred service charges warning ──────────────────
                    # For any selected order where lines are only partially selected
                    # AND that order has service charges, show a visible warning so
                    # the user knows those charges will appear on the final challan.
                    try:
                        from modules.backoffice.order_charges_panel import fetch_charges as _fc2
                        _deferred_warnings: List[str] = []
                        for _svc_oid in _all_order_ids:
                            _ub2        = get_unbilled_lines_for_order(_svc_oid)
                            _sel2       = set(st.session_state["ch_line_sel"].get(
                                              _svc_oid, [str(l["id"]) for l in _ub2]))
                            # Partial test: based on lens lines only (same gate as
                            # LAB/LOGISTICS service charges)
                            _lens2      = [l for l in _ub2 if _is_lens_line(l)]
                            _tot2       = len(_lens2)
                            _cnt2       = sum(1 for l in _lens2 if str(l["id"]) in _sel2)
                            _is_partial = _tot2 > 0 and _cnt2 < _tot2
                            if _is_partial:
                                _ord_svc = _fc2(_svc_oid) or []
                                # Only warn about LAB/LOGISTICS charges — CLINICAL
                                # charges are already included on the first challan.
                                _ord_svc = [
                                    s for s in _ord_svc
                                    if str(s.get("charge_type") or "").upper()
                                    not in _CLINICAL_TYPES
                                ]
                                if _ord_svc:
                                    _deferred_tot = sum(
                                        float(s.get("total_amount") or 0) for s in _ord_svc
                                    )
                                    # Get order number for display
                                    _ono2 = next(
                                        (o.get("order_no","") for o in pending_orders
                                         if str(o["id"]) == _svc_oid), _svc_oid[:8]
                                    )
                                    _deferred_warnings.append(
                                        f"<b>{_ono2}</b>: ₹{_deferred_tot:,.2f} service charges "
                                        f"deferred ({_cnt2}/{_tot2} lines selected)"
                                    )
                        if _deferred_warnings:
                            st.markdown(
                                f"<div style='background:#1c1000;border:1px solid #f59e0b;"
                                f"border-radius:6px;padding:8px 14px;margin-bottom:6px'>"
                                f"<div style='color:#fbbf24;font-size:0.75rem;font-weight:700;"
                                f"margin-bottom:4px'>⚠️ Service charges deferred to final challan</div>"
                                + "".join(
                                    f"<div style='color:#fcd34d;font-size:0.72rem'>• {w}</div>"
                                    for w in _deferred_warnings
                                )
                                + "</div>",
                                unsafe_allow_html=True
                            )
                    except Exception:
                        pass

                    if _svc_tot_ch > 0:
                        st.markdown(
                            f"<div style='background:#1a0a2e;border:1px solid #7c3aed;"
                            f"border-radius:6px;padding:6px 14px;margin-bottom:6px;"
                            f"display:flex;justify-content:space-between'>"
                            f"<span style='color:#c4b5fd;font-size:0.8rem'>"
                            f"+ Service Charges (fitting/colouring/courier)</span>"
                            f"<span style='color:#a78bfa;font-weight:700'>₹{_svc_tot_ch:,.2f}</span>"
                            f"</div>", unsafe_allow_html=True)
                        st.markdown(
                            f"<div style='background:#0d2818;border:1px solid #10b981;"
                            f"border-radius:6px;padding:6px 14px;margin-bottom:6px;"
                            f"display:flex;justify-content:space-between'>"
                            f"<span style='color:#6ee7b7;font-weight:700'>Grand Total incl. Services</span>"
                            f"<span style='color:#10b981;font-weight:900;font-size:1.1rem'>"
                            f"₹{_ch_grand_incl:,.2f}</span>"
                            f"</div>", unsafe_allow_html=True)

                    if st.button(
                        f"📋 Create Challan — {_sel_ord_count} order(s) · ₹{_ch_grand_incl:,.2f}",
                        type="primary", use_container_width=True, key="ch_create_btn"
                    ):
                        # Collect all selected line IDs
                        _all_line_ids = list(_all_sel_lines.keys())
                        challan_no = create_challan(
                            party_id     = _active_party,
                            order_ids    = _all_order_ids,
                            total_amount = round(_ch_base + _svc_base_ch, 2),
                            total_tax    = round(_ch_tax  + _svc_tax_ch,  2),
                            remarks      = remarks,
                            line_ids     = _all_line_ids,
                            svc_charges  = _all_svc_rows or None,
                        )
                        if challan_no:
                            st.session_state["ch_ord_sel"]  = {}
                            st.session_state["ch_line_sel"] = {}
                            st.success(f"✅ Challan **{challan_no}** created — {_sel_ord_count} order(s), {_tot_lines} lines, ₹{_ch_grand_incl:,.2f}")
                            # ── WhatsApp notification ─────────────────
                            try:
                                from modules.wa_hub import wa_panel, wa_challan_made
                                from modules.settings.shop_master import get_unit_info
                                _ap  = st.session_state.get("ch_sel_party", "")
                                _pr  = _q("SELECT party_name, COALESCE(mobile,'') AS mobile FROM parties WHERE id=%s::uuid LIMIT 1", (_ap,)) if _ap else []
                                _pn  = _pr[0]["party_name"] if _pr else ""
                                _mob = _pr[0]["mobile"]     if _pr else ""
                                _sh  = get_unit_info("wholesale")
                                _msg = wa_challan_made(
                                    party      = _pn,
                                    order_no   = "",
                                    challan_no = challan_no,
                                    grand_total= _ch_grand_incl,
                                    shop_name  = _sh.get("shop_name","DV Optical"),
                                    phone      = _sh.get("shop_phone",""),
                                )
                                wa_panel(_mob, _msg,
                                         key=f"wa_chal_{challan_no}",
                                         title="📲 WhatsApp — Challan Ready",
                                         expanded=True)
                            except Exception:
                                pass
                            st.rerun()
                else:
                    st.info("☝️ Tick orders above to include them in the challan.")


def render_invoice_creation():
    """Render invoice creation interface"""
    st.markdown("### 🧾 Create Invoice")
    
    # Tab for challan-based vs direct invoice
    tab1, tab2 = st.tabs(["📋 From Challan", "🎯 Direct Invoice"])
    
    with tab1:
        render_invoice_from_challan()
    
    with tab2:
        render_direct_invoice()


def render_invoice_from_challan():
    """Create invoice from existing challan"""
    pending_challans = get_pending_challans()
    
    if not pending_challans:
        st.info("No pending challans available for invoicing.")
        return
    
    # Challan selection
    challan_options = {c["id"]: f"{c['challan_no']} - {c['party_name']} - ₹{c['grand_total']:,.2f}" 
                       for c in pending_challans}
    selected_challan_id = st.selectbox("Select Challan", 
                                       options=list(challan_options.keys()),
                                       format_func=lambda x: challan_options.get(x, ""))
    
    if selected_challan_id:
        challan_details = get_challan_details(selected_challan_id)
        
        st.markdown("#### 📋 Challan Details")
        col1, col2, col3 = st.columns(3)
        col1.metric("Challan No", challan_details.get("challan_no"))
        col2.metric("Party", challan_details.get("party_name"))
        col3.metric("Amount", f"₹{challan_details.get('grand_total', 0):,.2f}")
        
        due_days = st.number_input("Due Days", min_value=0, value=30, help="Payment due period in days")
        remarks = st.text_area("Remarks", placeholder="Optional remarks...")
        
        if st.button("🧾 Create Invoice from Challan", type="primary", use_container_width=True):
            invoice_no = create_invoice(
                selected_challan_id, 
                challan_details["party_id"],
                challan_details["order_ids"],
                challan_details["total_amount"],
                challan_details["total_tax"],
                due_days,
                remarks
            )
            
            if invoice_no:
                # Update challan status
                # Sync challans.status for legacy reads.
                # Relational truth: INVOICED derived via invoices.challan_id FK in _open_docs.
                update_challan_status(selected_challan_id, "INVOICED")
                st.success(f"✅ Invoice {invoice_no} created successfully!")
                # ── WhatsApp notification ─────────────────────────────
                try:
                    from modules.wa_hub import wa_panel, wa_invoice_made
                    from modules.settings.shop_master import get_unit_info
                    _pr2 = _q("SELECT p.party_name, COALESCE(p.mobile,'') AS mobile, "
                              "COALESCE(i.grand_total,0) AS gt, COALESCE(i.balance_due,i.grand_total,0) AS bal "
                              "FROM invoices i LEFT JOIN parties p ON p.id=i.party_id "
                              "WHERE i.invoice_no=%s LIMIT 1", (invoice_no,))
                    if _pr2:
                        _sh2 = get_unit_info("wholesale")
                        wa_panel(
                            mobile = _pr2[0]["mobile"],
                            msg    = wa_invoice_made(
                                party      = _pr2[0]["party_name"],
                                invoice_no = invoice_no,
                                grand_total= float(_pr2[0]["gt"]),
                                balance    = float(_pr2[0]["bal"]),
                                shop_name  = _sh2.get("shop_name","DV Optical"),
                                phone      = _sh2.get("shop_phone",""),
                                upi_id     = _sh2.get("shop_upi_id",""),
                            ),
                            key      = f"wa_inv_{invoice_no}",
                            title    = "📲 WhatsApp — Invoice Generated",
                            expanded = True,
                        )
                except Exception:
                    pass
                st.rerun()


def render_direct_invoice():
    """Create direct invoice for party — WHOLESALE only.
    RETAIL orders must go through Challan first (business rule).
    """
    st.info(
        "💡 **Retail orders** must be invoiced via Challan → Invoice flow. "
        "Direct invoice is for wholesale parties with DIRECT_INVOICE billing preference only."
    )
    # Get parties with DIRECT_INVOICE preference
    direct_parties = _q("""
        SELECT id, party_name, mobile, billing_preference
        FROM parties 
        WHERE COALESCE(doc_preference, 'C') = 'I'
          AND UPPER(COALESCE(party_type,'')) NOT IN ('RETAIL','PATIENT')
        ORDER BY party_name
    """)
    
    if not direct_parties:
        st.info("No wholesale parties with DIRECT_INVOICE billing preference found.")
        return
    
    # Party selection
    party_options = {p["id"]: f"{p['party_name']} ({p['mobile']})" 
                   for p in direct_parties}
    selected_party_id = st.selectbox("Select Party", 
                                 options=list(party_options.keys()),
                                 format_func=lambda x: party_options.get(x, ""),
                                 key="direct_invoice_party")
    
    if selected_party_id:
        # Get pending orders for selected party
        # Only WHOLESALE orders — RETAIL must go via challan
        pending_orders = [
            o for o in get_pending_orders_for_party(selected_party_id)
            if str(o.get("order_type","")).upper() != "RETAIL"
        ]
        
        if not pending_orders:
            st.info("No pending orders for this party.")
            return
        
        st.markdown("#### 📦 Pending Orders")
        
        # Order selection
        order_options = {
            o["id"]: f"{o['order_no']} - {float(o.get('total_value', 0)):,.2f} ({str(o.get('created_at', ''))[:10]})"
            for o in pending_orders
        }
        selected_orders = st.multiselect("Select Orders to Invoice",
                                    options=list(order_options.keys()),
                                    format_func=lambda x: order_options.get(x, ""),
                                    key="direct_invoice_orders")
        
        if selected_orders:
            direct_lines = _q("""
                SELECT ROUND(ol.unit_price * ol.quantity, 2)  AS total_price,
                       ROUND(ol.unit_price * ol.quantity * COALESCE(ol.gst_percent, 0) / 100, 2) AS gst_amount,
                       o.order_type
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE ol.order_id = ANY(%(ids)s::uuid[])
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
            """, {"ids": selected_orders})
            total_amount = round(sum(float(r.get("total_price") or 0)
                                     for r in direct_lines), 2)
            tax_amount   = round(sum(float(r.get("gst_amount") or 0)
                                     for r in direct_lines), 2)
            grand_total  = round(sum(
                float(r.get("total_price") or 0) + (
                    0 if str(r.get("order_type") or "").upper() == "RETAIL"
                    else float(r.get("gst_amount") or 0)
                ) for r in direct_lines
            ), 2)
            
            st.markdown("#### 💰 Summary")
            col1, col2, col3 = st.columns(3)
            col1.metric("Total Amount", f"₹{total_amount:,.2f}")
            col2.metric("Tax", f"₹{tax_amount:,.2f}")
            col3.metric("Grand Total", f"₹{grand_total:,.2f}")
            
            due_days = st.number_input("Due Days", min_value=0, value=30, 
                                    help="Payment due period in days",
                                    key="direct_due_days")
            remarks = st.text_area("Remarks", placeholder="Optional remarks...",
                                key="direct_remarks")
            
            if st.button("🧾 Create Direct Invoice", type="primary", use_container_width=True):
                invoice_no = create_invoice(None, selected_party_id, selected_orders,
                                         total_amount, tax_amount, due_days, remarks)
                if invoice_no:
                    st.success(f"✅ Invoice {invoice_no} created successfully!")
                    st.rerun()


def render_billing_dashboard():
    """Main billing dashboard"""
    st.markdown("# 🧾 Billing & Invoicing")
    
    # Summary metrics
    pending_challans = len(get_pending_challans())
    
    st.markdown("### 📊 Summary")
    col1, col2, col3 = st.columns(3)
    col1.metric("Pending Challans", pending_challans)
    col2.metric("Pending Invoices", 0)  # TODO: Implement
    col3.metric("Overdue Invoices", 0)  # TODO: Implement
    
    st.markdown("---")
    
    # Tabs for different operations
    tab1, tab2, tab3 = st.tabs(["📋 Create Challan", "🧾 Create Invoice", "📊 Reports"])
    
    with tab1:
        render_challan_creation()
    
    with tab2:
        render_invoice_creation()
    
    with tab3:
        st.info("📊 Reports section coming soon...")


# =====================================================
# MAIN ENTRY POINT
# =====================================================

if __name__ == "__main__":
    render_billing_dashboard()
