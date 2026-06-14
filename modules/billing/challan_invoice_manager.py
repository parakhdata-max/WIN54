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
import json as _json
from decimal import Decimal, ROUND_HALF_UP
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


def _line_lens_params(line: Dict) -> Dict:
    lp = (line or {}).get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = _json.loads(lp)
        except Exception:
            lp = {}
    return lp if isinstance(lp, dict) else {}


def _service_family_for_line(line: Dict) -> str:
    lp = _line_lens_params(line)
    text = " ".join(str(x or "") for x in (
        line.get("product_name"),
        lp.get("charge_type"),
        lp.get("service_type"),
        lp.get("service_group"),
        lp.get("service_production_type"),
        lp.get("service_code"),
        lp.get("service_description"),
    )).upper()
    if "COLOUR" in text or "COLOR" in text or "TINT" in text:
        return "COLOURING"
    if "FITTING" in text or "FIT_" in text:
        return "FITTING"
    if "COURIER" in text or "DELIVERY" in text or "FREIGHT" in text:
        return "COURIER"
    if "CONSULT" in text:
        return "CONSULTATION"
    if "TEST" in text or "REFRACTION" in text:
        return "EYE_TESTING"
    if bool(line.get("is_service_line")) or str(line.get("eye_side") or "").upper() in ("S", "SERVICE"):
        return "MISC"
    return ""


def _suspected_missing_services(lines: list) -> list:
    present = {_service_family_for_line(l) for l in (lines or []) if _service_family_for_line(l)}
    suspects = []
    for line in (lines or []):
        if bool(line.get("is_service_line")):
            continue
        lp = _line_lens_params(line)
        text = " ".join(
            [str(k or "") for k in lp.keys()]
            + [str(v or "") for v in lp.values()]
            + [
                str(line.get("product_name") or ""),
                str(line.get("manufacturing_route") or ""),
                str(line.get("status") or ""),
                str(line.get("batch_status") or ""),
            ]
        ).upper()
        if ("COLOUR" in text or "COLOR" in text or "TINT" in text) and "COLOURING" not in present:
            suspects.append("COLOURING")
        if (
            bool(lp.get("fitting_required"))
            or "FITTING" in text
            or "FIT HEIGHT" in text
            or "FITTING_HEIGHT" in text
        ) and "FITTING" not in present:
            suspects.append("FITTING")
    order = ["COLOURING", "FITTING", "COURIER", "CONSULTATION", "EYE_TESTING", "MISC"]
    return [x for x in order if x in set(suspects)]


def audit_billing_preflight(order_ids: List[str], line_ids: Optional[List[str]] = None) -> Tuple[bool, List[str]]:
    """Final document gate for unusual/missing collectable service signals.

    Used by Backoffice Billing Summary and Billing Hub before creating challans
    or invoices. If it returns False, the user must correct the order from
    Backoffice Billing Summary recovery before any document number is created.
    """
    resolved_order_ids = _resolve_order_ids(order_ids or [])
    if not resolved_order_ids:
        return False, ["Selected order was not found."]
    params = {"oids": resolved_order_ids}
    line_filter = ""
    if line_ids is not None:
        clean_lids = [str(x or "").strip() for x in (line_ids or []) if str(x or "").strip()]
        if not clean_lids:
            return False, ["No billable lines selected."]
        params["lids"] = clean_lids
        line_filter = "AND ol.id = ANY(%(lids)s::uuid[])"
    rows = _q(f"""
        SELECT
            o.order_no,
            ol.id::text AS line_id,
            COALESCE(p.product_name, '') AS product_name,
            COALESCE(ol.eye_side, '') AS eye_side,
            COALESCE(ol.is_service_line, FALSE) AS is_service_line,
            COALESCE(ol.status, '') AS status,
            COALESCE(ol.batch_status, '') AS batch_status,
            ol.lens_params
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE ol.order_id = ANY(%(oids)s::uuid[])
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          {line_filter}
        ORDER BY o.order_no, ol.id
    """, params) or []
    issues = []
    by_order = {}
    for row in rows:
        by_order.setdefault(str(row.get("order_no") or "Order"), []).append(row)
    if line_ids is not None:
        # Line-level challans are intentional partial billing. The caller has
        # already selected only ready line IDs, so missing/pending collectables
        # must remain visible in Backoffice but must not block this document.
        return True, []
    for order_no, order_lines in by_order.items():
        missing = _suspected_missing_services(order_lines)
        for service in missing:
            issues.append(
                f"{order_no}: possible missing {service.title()} service line. "
                "Open Backoffice → Billing Summary → Add missed service / collectable before billing."
            )
    return (not issues), issues


def _enforce_billing_preflight(order_ids: List[str], line_ids: Optional[List[str]] = None) -> None:
    ok, issues = audit_billing_preflight(order_ids, line_ids)
    if ok:
        return
    raise ValueError(
        "❌ Billing locked — unusual/missing collectable detected.\n"
        + "\n".join(f"  • {msg}" for msg in issues)
    )


# =====================================================
# BILLING GATE — Pipeline Validation
# =====================================================

def is_line_billing_ready(order_line_id: str) -> Tuple[bool, str]:
    """
    Gate check per order line.
    Returns (ready: bool, reason: str).
    STOCK   → allocated_qty >= quantity
    VENDOR  → supplier received >= ordered
    INHOUSE → job stage in READY_TO_BILL/READY_FOR_BILLING/CLOSED
    """
    rows = _q("""
        SELECT quantity, COALESCE(allocated_qty,0) AS allocated_qty,
               COALESCE(ready_qty,0) AS ready_qty,
               COALESCE(
                   NULLIF(lens_params->>'manufacturing_route',''),
                   NULLIF(lens_params->>'production_route',''),
                   NULLIF(lens_params->>'fulfillment_route',''),
                   NULLIF(lens_params->>'stock_source',''),
                   NULLIF(lens_params->>'route',''),
                   'STOCK'
               ) AS route
        FROM order_lines
        WHERE id = %(id)s::uuid
    """, {"id": order_line_id})
    if not rows:
        return False, "Order line not found"

    r     = rows[0]
    qty   = int(r.get("quantity") or 0)
    alloc = int(r.get("allocated_qty") or 0)
    route = str(r.get("route") or "STOCK").upper()

    # SERVICE lines never need blank/job-card checks — they bill when stages are done
    # Also handle old lines where lens_params.manufacturing_route was incorrectly "INHOUSE"
    # Detect via is_service_line column or route="SERVICE"
    if route == "SERVICE":
        return True, "Service line — direct billing"

    # Also check DB is_service_line flag (covers old records with route="INHOUSE")
    svc_check = _q("""
        SELECT COALESCE(is_service_line, FALSE) AS is_svc,
               eye_side,
               lens_params->>'service_production_type' AS svc_prod_type
        FROM order_lines WHERE id = %(id)s::uuid
    """, {"id": order_line_id})
    if svc_check:
        _is_svc = bool(svc_check[0].get("is_svc"))
        _eye    = str(svc_check[0].get("eye_side") or "").upper()
        _spt    = str(svc_check[0].get("svc_prod_type") or "").upper()
        if _is_svc or _eye in ("S", "SERVICE"):
            if _spt in ("COLOURING", "FITTING"):
                # Check if job card reached a terminal stage
                svc_jobs = _q("""
                    SELECT current_stage, is_closed FROM job_master
                    WHERE order_line_id = %(id)s::uuid
                """, {"id": order_line_id})
                if not svc_jobs:
                    return True, "Service line — no job card needed for billing"
                allowed = {"READY_TO_BILL", "READY_FOR_BILLING", "CLOSED",
                           "COLOURING_DONE", "FITTING_DONE", "FITTING_RECEIVED"}
                for j in svc_jobs:
                    stage = str(j.get("current_stage") or "").upper()
                    if not j.get("is_closed") and stage not in allowed:
                        return False, f"Service not complete — current stage: {stage}. Advance to {'/'.join(sorted(allowed)[:2])} first."
                return True, "Service complete"
            return True, "Service line — direct billing"

    if route == "STOCK":
        if alloc < qty:
            return False, f"Stock not fully allocated ({alloc}/{qty})"
        po_rows = _q("""
            SELECT status, qty_received, batch_no, expiry_date
            FROM procurement_order_items
            WHERE order_line_id = %(id)s::uuid
            LIMIT 1
        """, {"id": order_line_id})
        if po_rows:
            po = po_rows[0]
            pst = str(po.get("status") or "").upper()
            if pst not in {"PROCURED", "RECEIVED", "INVOICED", "PURCHASE_ACKED", "READY", "LOCKED", "DISCARDED", "NO_REPLENISH"}:
                return False, f"Stock procurement pending ({pst or 'QUEUED'})"
            cl_check = _q("""
                SELECT p.main_group, COALESCE(p.is_batch_applicable, FALSE) AS batch_req,
                       COALESCE(NULLIF(poi.batch_no,''), pa.batch_no)   AS batch_no,
                       COALESCE(poi.expiry_date, pa.expiry_date)        AS expiry_date
                FROM order_lines ol
                JOIN products p ON p.id = ol.product_id
                LEFT JOIN procurement_order_items poi ON poi.order_line_id = ol.id
                LEFT JOIN purchase_acknowledgements pa  ON pa.order_line_id = ol.id
                WHERE ol.id = %(id)s::uuid
            """, {"id": order_line_id})
            if cl_check:
                mg = str(cl_check[0].get("main_group") or "").lower()
                is_cl = "contact" in mg or bool(cl_check[0].get("batch_req"))
                if is_cl and not cl_check[0].get("batch_no"):
                    return False, "CL batch no. required — record in Procurement before billing"
                if is_cl and not cl_check[0].get("expiry_date"):
                    return False, "CL expiry date required — record in Procurement before billing"
        return True, f"Stock allocated ({alloc}/{qty})"

    if route in ("VENDOR", "SUPPLIER", "EXTERNAL_LAB", "EXTERNAL"):
        def _procurement_receipt_ready() -> Tuple[bool, str]:
            """Supplier/external billing must have a real procurement receipt.

            A supplier stage in lens_params is not enough: it is only workflow
            state. Billing needs purchase acknowledgement/receipt with price so
            accounts, purchase register and inventory audit stay aligned.
            """
            pa_rows = _q("""
                SELECT billing_status, purchase_price,
                       batch_no, expiry_date
                FROM purchase_acknowledgements
                WHERE order_line_id = %(id)s::uuid
                  AND COALESCE(purchase_price, 0) > 0
                  AND COALESCE(qty, received_qty, 0) > 0
                ORDER BY acknowledged_at DESC
                LIMIT 1
            """, {"id": order_line_id})
            if pa_rows:
                pa = pa_rows[0]
                pa_stat = str(pa.get("billing_status") or "").upper()
                if pa_stat in {"PURCHASE_ACKED", "PROCURED", "READY_FOR_BILLING", "LOCKED", "INVOICED"}:
                    cl_check = _q("""
                        SELECT p.main_group, COALESCE(p.is_batch_applicable, FALSE) AS batch_req
                        FROM order_lines ol
                        JOIN products p ON p.id = ol.product_id
                        WHERE ol.id = %(id)s::uuid
                    """, {"id": order_line_id})
                    if cl_check:
                        mg = str(cl_check[0].get("main_group") or "").lower()
                        is_cl = "contact" in mg or bool(cl_check[0].get("batch_req"))
                        if is_cl and not pa.get("batch_no"):
                            return False, "CL batch no. required — record in Procurement before billing"
                        if is_cl and not pa.get("expiry_date"):
                            return False, "CL expiry date required — record in Procurement before billing"
                    return True, f"Procured via purchase acknowledgement ({pa_stat})"

            po_rows = _q("""
                SELECT
                    poi.status,
                    COALESCE(poi.qty_received, 0) AS qty_received,
                    COALESCE(poi.qty_ordered, poi.qty_requested, 0) AS qty_ordered,
                    COALESCE(poi.unit_price, 0) AS unit_price,
                    poi.batch_no, poi.expiry_date,
                    pr.id AS receipt_id
                FROM procurement_order_items poi
                LEFT JOIN procurement_receipts pr
                       ON pr.procurement_order_id = poi.procurement_order_id
                WHERE poi.order_line_id = %(id)s::uuid
                ORDER BY COALESCE(poi.received_at, poi.updated_at, poi.created_at) DESC
                LIMIT 1
            """, {"id": order_line_id})
            if not po_rows:
                return False, "No procurement receipt found — open Production → Procurement/Supplier and record purchase first"

            po = po_rows[0]
            pr = int(po.get("qty_received") or 0)
            poq = int(po.get("qty_ordered") or qty or 0)
            pst = str(po.get("status") or "").upper()
            if pst not in {"PROCURED", "RECEIVED", "INVOICED", "PURCHASE_ACKED", "READY", "LOCKED"} or pr < max(poq, qty):
                return False, f"Procurement pending ({pr}/{max(poq, qty)}) — receive purchase before billing"
            if float(po.get("unit_price") or 0) <= 0:
                return False, "Purchase price missing — enter supplier purchase price before billing"
            if not po.get("receipt_id"):
                return False, "Procurement receipt missing — record purchase document before billing"

            cl_check = _q("""
                SELECT p.main_group, COALESCE(p.is_batch_applicable, FALSE) AS batch_req
                FROM order_lines ol
                JOIN products p ON p.id = ol.product_id
                WHERE ol.id = %(id)s::uuid
            """, {"id": order_line_id})
            if cl_check:
                mg = str(cl_check[0].get("main_group") or "").lower()
                is_cl = "contact" in mg or bool(cl_check[0].get("batch_req"))
                if is_cl and not po.get("batch_no"):
                    return False, "CL batch no. required — record in Procurement before billing"
                if is_cl and not po.get("expiry_date"):
                    return False, "CL expiry date required — record in Procurement before billing"
            return True, "Procurement received"

        rcv = _q("""
            SELECT COALESCE(SUM(soi.received_qty),0) AS rcv,
                   COALESCE(SUM(soi.ordered_qty), 0) AS ord
            FROM supplier_order_items soi
            JOIN supplier_orders so ON so.id = soi.supplier_order_id
            WHERE soi.customer_line_id::text = %(id)s
        """, {"id": order_line_id})
        if rcv:
            r_qty, o_qty = int(rcv[0].get("rcv") or 0), int(rcv[0].get("ord") or 0)
            if o_qty > 0 and r_qty < o_qty:
                return False, f"Supplier delivery pending ({r_qty}/{o_qty})"
            if o_qty > 0 and r_qty >= o_qty:
                return _procurement_receipt_ready()
        return _procurement_receipt_ready()

    # ── Universal PA fallback (all routes) ───────────────────────────────────
    # If procurement_queue.py saved a purchase_acknowledgements row with a
    # terminal billing_status and a price > 0, the line is billing-ready
    # regardless of whether supplier_order_items / procurement_order_items exist.
    pa_rows = _q("""
        SELECT billing_status, purchase_price,
               batch_no, expiry_date
        FROM purchase_acknowledgements
        WHERE order_line_id = %(id)s::uuid
          AND COALESCE(purchase_price, 0) > 0
        ORDER BY acknowledged_at DESC
        LIMIT 1
    """, {"id": order_line_id})
    if pa_rows:
        pa      = pa_rows[0]
        pa_stat = str(pa.get("billing_status") or "").upper()
        if pa_stat in {"PURCHASE_ACKED", "PROCURED", "READY_FOR_BILLING", "LOCKED", "INVOICED"}:
            # CL batch/expiry check against PA for all routes
            cl_check2 = _q("""
                SELECT p.main_group, COALESCE(p.is_batch_applicable, FALSE) AS batch_req
                FROM order_lines ol
                JOIN products p ON p.id = ol.product_id
                WHERE ol.id = %(id)s::uuid
            """, {"id": order_line_id})
            if cl_check2:
                mg2   = str(cl_check2[0].get("main_group") or "").lower()
                is_cl2 = "contact" in mg2 or bool(cl_check2[0].get("batch_req"))
                if is_cl2 and not pa.get("batch_no"):
                    return False, "CL batch no. required — enter in Procurement receipt before billing"
                if is_cl2 and not pa.get("expiry_date"):
                    return False, "CL expiry date required — enter in Procurement receipt before billing"
            return True, f"Procured via purchase queue ({pa_stat})"

    if route == "INHOUSE":
        jobs = _q("""
            SELECT current_stage, is_closed FROM job_master
            WHERE order_line_id = %(id)s::uuid
        """, {"id": order_line_id})
        if not jobs:
            return False, "Job card not created yet"
        allowed = {"READY_TO_BILL", "READY_FOR_BILLING", "CLOSED"}
        for j in jobs:
            stage = str(j.get("current_stage") or "").upper()
            if not j.get("is_closed") and stage not in allowed:
                return False, f"Production not complete (stage: {j.get('current_stage')})"
        return True, "Production complete"

    if route == "FITTING":
        jobs = _q("""
            SELECT status FROM fitting_jobs
            WHERE order_line_id = %(id)s::uuid
              AND status NOT IN ('CANCELLED')
            ORDER BY created_at DESC
            LIMIT 1
        """, {"id": order_line_id})
        if not jobs:
            return False, "Fitting job not created yet"
        stage = str(jobs[0].get("status") or "").upper()
        if stage in {"DONE", "DELIVERED", "RECEIVED"}:
            return True, "Fitting complete"
        return False, f"Fitting not complete (stage: {stage})"

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
             %(pid)s, NULLIF(%(prid)s, '')::uuid,
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


def _ensure_roundoff_columns() -> None:
    """Add document-level round-off columns if this DB has not received them yet."""
    _write("ALTER TABLE challans ADD COLUMN IF NOT EXISTS round_off_amount NUMERIC(12,2) DEFAULT 0", {})
    _write("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS round_off_amount NUMERIC(12,2) DEFAULT 0", {})


def _round_to_rupee(value) -> float:
    """Round positive document totals to nearest rupee using normal cash rounding."""
    return float(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _roundoff_amount(value) -> float:
    raw = float(value or 0)
    return round(_round_to_rupee(raw) - raw, 2)


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
    try:
        from modules.sql_adapter import resolve_order_uuid
        order_id = resolve_order_uuid(order_id) or ""
    except Exception:
        pass
    if not order_id:
        return []
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
               COALESCE(
                   ol.billing_total,
                   ol.total_price,
                   ROUND(ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0), 2)
               ) AS billing_total,
               ol.gst_percent,
               -- Use stored gst_amount (set by finalize_engine — correct for RETAIL inclusive GST).
               -- Never recalculate as unit_price times qty times GST percent over 100.
               -- That is the wholesale formula only.
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
                           COALESCE(
                               ol.billing_total,
                               ol.total_price,
                               ROUND(ol.unit_price * ol.quantity, 2)
                           )
                       ELSE
                           CASE
                           WHEN COALESCE(ol.is_service_line, FALSE) THEN
                               COALESCE(
                                   ol.billing_total,
                                   ol.total_price,
                                   ROUND(ol.unit_price * ol.quantity, 2)
                               )
                           ELSE
                               COALESCE(
                                   ol.billing_total,
                                   ol.total_price,
                                   ROUND(ol.unit_price * ol.quantity, 2)
                               ) * (1 + COALESCE(ol.gst_percent, 0) / 100)
                           END
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
                    -- ALL lines checked: service lines included in fully-billed test
                    -- ol2_eye filter removed: is_service_line check is sufficient
                  HAVING COUNT(*) > 0
                     AND COUNT(*) = SUM(CASE WHEN COALESCE(ol2.billed_qty,0) >= ol2.quantity THEN 1 ELSE 0 END)
              )
              AND (
                  -- Has unbilled lines OR status lets it through for initial billing
                  EXISTS (
                      SELECT 1 FROM order_lines ol3
                      WHERE ol3.order_id = o.id
                        AND COALESCE(ol3.is_deleted, FALSE) = FALSE
                        -- service lines included (service-only orders must appear)
                        AND COALESCE(ol3.billed_qty, 0) < ol3.quantity
                        -- Include service lines: eye_side='S' is valid billable line
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
                       COALESCE(
                           ol.billing_total,
                           ol.total_price,
                           ROUND(ol.unit_price * ol.quantity, 2)
                       )
                   ELSE
                       CASE
                       WHEN COALESCE(ol.is_service_line, FALSE) THEN
                           COALESCE(
                               ol.billing_total,
                               ol.total_price,
                               ROUND(ol.unit_price * ol.quantity, 2)
                           )
                       ELSE
                           COALESCE(
                               ol.billing_total,
                               ol.total_price,
                               ROUND(ol.unit_price * ol.quantity, 2)
                           ) * (1 + COALESCE(ol.gst_percent, 0) / 100)
                       END
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
                    -- ALL lines checked: service lines included in fully-billed test
                    -- ol2_eye filter removed: is_service_line check is sufficient
            HAVING COUNT(*) > 0
               AND COUNT(*) = SUM(CASE WHEN COALESCE(ol2.billed_qty,0) >= ol2.quantity THEN 1 ELSE 0 END)
        )
        AND (
            -- Must have at least one unbilled non-service line remaining
            EXISTS (
                SELECT 1 FROM order_lines ol3
                WHERE ol3.order_id = o.id
                  AND COALESCE(ol3.is_deleted, FALSE) = FALSE
                        -- service lines included (service-only orders must appear)
                         -- service lines included in unbilled check
                  AND COALESCE(ol3.billed_qty, 0) < ol3.quantity
            )
        )
        GROUP BY o.id, o.order_no, o.created_at,
                 o.patient_name, o.party_name, o.status, o.order_type
        ORDER BY o.created_at DESC
    """, {"party_id": party_key})


def get_procured_orders_for_party(party_key: str) -> List[Dict]:
    """
    Return orders where every non-service line is bill-ready:
    either stock/blank allocated, or purchase/procurement acknowledged.
    The order must not be fully billed.

    Used by billing_hub.py to show only procurement-complete orders.
    party_key: UUID string or "name:PartyName"
    """
    if party_key.startswith("name:"):
        party_filter = "o.party_name = %(party_val)s"
        party_val    = party_key[5:]
    else:
        party_filter = "(o.party_id = %(party_val)s::uuid OR o.party_name = (SELECT party_name FROM parties WHERE id = %(party_val)s::uuid LIMIT 1))"
        party_val    = party_key

    return _q(f"""
        SELECT
            o.id::text          AS order_id,
            o.order_no,
            o.created_at,
            COALESCE(o.patient_name, o.party_name, '—') AS patient_name,
            o.party_name,
            o.status,
            o.order_type,
            COUNT(ol.id)        AS line_count,
            COALESCE(SUM(
                CASE UPPER(COALESCE(o.order_type,'WHOLESALE'))
                WHEN 'RETAIL' THEN COALESCE(
                    ol.billing_total,
                    ol.total_price,
                    ROUND(ol.unit_price * ol.quantity, 2)
                )
                ELSE COALESCE(
                    ol.billing_total,
                    ol.total_price,
                    ROUND(ol.unit_price * ol.quantity, 2)
                ) * (1 + COALESCE(ol.gst_percent,0)/100)
                END
            ), 0)               AS total_value,
            STRING_AGG(DISTINCT COALESCE(pa.supplier_name,''), ', ')
                FILTER (WHERE pa.supplier_name IS NOT NULL) AS suppliers,
            MAX(pa.document_date::text)  AS last_doc_date,
            STRING_AGG(DISTINCT COALESCE(NULLIF(pa.challan_no,''), NULLIF(pa.invoice_no,'')), ', ')
                FILTER (WHERE COALESCE(NULLIF(pa.challan_no,''), NULLIF(pa.invoice_no,'')) IS NOT NULL)
                AS ref_nos
        FROM orders o
        JOIN order_lines ol
               ON ol.order_id = o.id
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
        LEFT JOIN purchase_acknowledgements pa ON pa.order_line_id = ol.id
        WHERE {party_filter}
          AND o.status IN ('PENDING','CONFIRMED','READY','READY_FOR_BILLING',
                           'IN_PRODUCTION','BILLED','PARTIALLY_BILLED')
          AND EXISTS (
              SELECT 1 FROM order_lines ol3
              WHERE ol3.order_id = o.id
                AND COALESCE(ol3.is_deleted, FALSE) = FALSE
                AND COALESCE(ol3.billed_qty, 0) < ol3.quantity
          )
          AND NOT EXISTS (
              SELECT 1 FROM order_lines ol2
              LEFT JOIN purchase_acknowledgements pa2 ON pa2.order_line_id = ol2.id
              WHERE ol2.order_id = o.id
                AND COALESCE(ol2.is_deleted, FALSE) = FALSE
                AND COALESCE(ol2.is_service_line, FALSE) = FALSE
                AND UPPER(COALESCE(ol2.eye_side,'')) NOT IN ('S','SERVICE')
                AND (
                    -- Stock / blank orders can bill from allocation alone.
                    COALESCE(ol2.allocated_qty, 0) < COALESCE(ol2.quantity, 0)
                    AND (
                        pa2.id IS NULL
                        OR COALESCE(pa2.purchase_price, 0) = 0
                        OR UPPER(COALESCE(pa2.billing_status,'')) NOT IN
                           ('PURCHASE_ACKED','PROCURED','READY_FOR_BILLING','LOCKED','INVOICED')
                    )
                )
          )
        GROUP BY o.id, o.order_no, o.created_at,
                 o.patient_name, o.party_name, o.status, o.order_type
        ORDER BY o.created_at DESC
    """, {"party_val": party_val})


def get_parties_with_procured_orders() -> List[Dict]:
    """
    Return all parties that have at least one fully-procured, unbilled order.
    Used by billing_hub.py to populate the party selector.
    """
    return _q("""
        SELECT DISTINCT
            COALESCE(pt.id::text, 'name:' || o.party_name) AS party_key,
            COALESCE(pt.party_name, o.party_name, '—')     AS party_name,
            COALESCE(pt.mobile, '')                         AS mobile,
            COALESCE(pt.party_type, '')                     AS party_type,
            COUNT(DISTINCT o.id)                            AS order_count
        FROM orders o
        LEFT JOIN parties pt ON pt.id = o.party_id
           OR pt.party_name = o.party_name
        JOIN order_lines ol
               ON ol.order_id = o.id
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
        LEFT JOIN purchase_acknowledgements pa ON pa.order_line_id = ol.id
           AND COALESCE(pa.purchase_price, 0) > 0
           AND UPPER(COALESCE(pa.billing_status,'')) IN
               ('PURCHASE_ACKED','PROCURED','READY_FOR_BILLING','LOCKED','INVOICED')
        WHERE o.status IN ('PENDING','CONFIRMED','READY','READY_FOR_BILLING',
                           'IN_PRODUCTION','BILLED','PARTIALLY_BILLED')
          -- Billing Hub now supports all bill-ready orders.
          -- Retail still respects the normal challan-first flow.
          AND EXISTS (
              SELECT 1 FROM order_lines ol3
              WHERE ol3.order_id = o.id
                AND COALESCE(ol3.is_deleted, FALSE) = FALSE
                AND COALESCE(ol3.billed_qty, 0) < ol3.quantity
          )
          AND NOT EXISTS (
              SELECT 1 FROM order_lines ol2
              LEFT JOIN purchase_acknowledgements pa2 ON pa2.order_line_id = ol2.id
              WHERE ol2.order_id = o.id
                AND COALESCE(ol2.is_deleted, FALSE) = FALSE
                AND COALESCE(ol2.is_service_line, FALSE) = FALSE
                AND UPPER(COALESCE(ol2.eye_side,'')) NOT IN ('S','SERVICE')
                AND (
                    -- Stock / blank orders can bill from allocation alone.
                    COALESCE(ol2.allocated_qty, 0) < COALESCE(ol2.quantity, 0)
                    AND (
                        pa2.id IS NULL
                        OR COALESCE(pa2.purchase_price, 0) = 0
                        OR UPPER(COALESCE(pa2.billing_status,'')) NOT IN
                           ('PURCHASE_ACKED','PROCURED','READY_FOR_BILLING','LOCKED','INVOICED')
                    )
                )
          )
        GROUP BY COALESCE(pt.id::text, 'name:' || o.party_name),
                 COALESCE(pt.party_name, o.party_name, '—'),
                 COALESCE(pt.mobile, ''),
                 COALESCE(pt.party_type, '')
        ORDER BY party_name
    """)


def _normalise_order_ids(raw: list) -> list:
    """Strip PostgreSQL array-literal curly braces from order IDs.
    e.g. '{f2fc0606-...}' → 'f2fc0606-...'
    Prevents the JOIN bug where challan.order_ids = '{uuid}' doesn't match orders.id
    """
    cleaned = []
    for x in (raw or []):
        s = str(x).strip()
        if s.startswith("{") and s.endswith("}"):
            s = s[1:-1].strip()
        if s:
            cleaned.append(s)
    return cleaned


def _resolve_order_ids(raw: list) -> list:
    """Accept UUIDs or visible order numbers and return only orders.id UUIDs."""
    try:
        from modules.sql_adapter import resolve_order_uuid
    except Exception:
        return []

    resolved = []
    seen = set()
    for value in _normalise_order_ids(raw):
        oid = resolve_order_uuid(value)
        oid = str(oid or "").strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        resolved.append(oid)
    return resolved


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
    order_ids = _resolve_order_ids(order_ids)
    if not order_ids:
        raise ValueError("❌ Cannot create challan — selected order was not found.")
    if line_ids is not None:
        _clean_line_ids = []
        _seen_line_ids = set()
        for _raw_lid in (line_ids or []):
            _lid = str(_raw_lid or "").strip()
            if not _lid or _lid in _seen_line_ids:
                continue
            _seen_line_ids.add(_lid)
            _clean_line_ids.append(_lid)
        line_ids = _clean_line_ids
        if not line_ids:
            raise ValueError("❌ Cannot create challan — no billable lines selected.")

    _enforce_billing_preflight(order_ids, line_ids)

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
        #   - job is at the billable stage (READY_TO_BILL / READY_FOR_BILLING)
        # This runs BEFORE the transaction so a bad line blocks the entire challan.
        if line_ids:
            try:
                from modules.sql_adapter import run_query as _rq_vbr
                _inhouse_lids = _rq_vbr("""
                    SELECT ol.id::text AS line_id
                    FROM order_lines ol
                    WHERE ol.id = ANY(%(lids)s::uuid[])
                      AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                      AND COALESCE(ol.is_service_line, FALSE) = FALSE
                      AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
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
                _fitting_lids = _rq_vbr("""
                    SELECT ol.id::text AS line_id
                    FROM order_lines ol
                    WHERE ol.id = ANY(%(lids)s::uuid[])
                      AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'FITTING'
                """, {"lids": line_ids}) or []
                _fit_blocks = []
                for _fit in _fitting_lids:
                    _ok_fit, _reason_fit = is_line_billing_ready(_fit["line_id"])
                    if not _ok_fit:
                        _fit_blocks.append(f"Line {_fit['line_id'][:8]}…: {_reason_fit}")
                if _fit_blocks:
                    raise ValueError(
                        "❌ Cannot create challan — FITTING service(s) not ready:\n"
                        + "\n".join(f"  • {m}" for m in _fit_blocks)
                    )
            except ValueError:
                raise
            except Exception as _vbr_e:
                import logging as _lg_vbr
                _lg_vbr.getLogger(__name__).warning(
                    f"[billing_readiness] DB function failed; using Python fallback: {_vbr_e}"
                )
                _fallback_blocks = []
                for _ih_lid in _inhouse_ids:
                    _ok_fb, _reason_fb = is_line_billing_ready(_ih_lid)
                    if not _ok_fb:
                        _fallback_blocks.append(f"Line {_ih_lid[:8]}…: {_reason_fb}")
                if _fallback_blocks:
                    raise ValueError(
                        "❌ Cannot create challan — INHOUSE job(s) not ready:\n"
                        + "\n".join(f"  • {m}" for m in _fallback_blocks)
                    )

        # ── STOCK lines receipt gate ────────────────────────────────────────
        # Stock billing is ALWAYS OPEN once allocated_qty >= quantity.
        # Procurement (batch no, expiry, purchase price) runs independently
        # and does not gate revenue billing.

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
        # Step 0: challan header — tx_steps MUST be initialised before any
        # .append() call. Python marks tx_steps as local for the whole function
        # scope the moment it sees the list assignment, so appending before
        # initialisation raises UnboundLocalError: cannot access local variable.
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

        # Schema guards — safe to run repeatedly (IF NOT EXISTS).
        # Inserted at positions 0 and 1 so they run before the challan header INSERT.
        tx_steps.insert(0, (
            "ALTER TABLE challans ADD COLUMN IF NOT EXISTS round_off_amount NUMERIC(12,2) DEFAULT 0",
            None,
        ))
        tx_steps.insert(1, (
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS round_off_amount NUMERIC(12,2) DEFAULT 0",
            None,
        ))

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

        if not _deduped and line_ids:
            try:
                _line_order_rows = _q("""
                    SELECT DISTINCT order_id::text AS order_id
                    FROM order_lines
                    WHERE id = ANY(%(lids)s::uuid[])
                      AND COALESCE(is_deleted, FALSE) = FALSE
                    ORDER BY order_id
                """, {"lids": line_ids})
                for _lor in (_line_order_rows or []):
                    _loid = str(_lor.get("order_id") or "").strip()
                    if _loid and _loid not in _seen:
                        _seen.add(_loid)
                        _deduped.append(_loid)
            except Exception as _line_order_e:
                import logging as _line_order_log
                _line_order_log.getLogger(__name__).warning(
                    "Could not derive challan order_ids from selected lines: %s",
                    _line_order_e,
                )
        if not _deduped:
            raise ValueError(
                "❌ Cannot create challan — selected billing lines could not be linked "
                "to a customer order. Refresh the order and try again."
            )

        _selected_line_count = 0
        _selected_line_ids_written = set()
        _selected_line_total = 0.0

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
                       COALESCE(ol.discount_amount, 0)        AS discount_amount,
                       COALESCE(
                           ol.billing_total,
                           ol.total_price,
                           ROUND(
                               ol.unit_price * COALESCE(ol.billing_qty, ol.quantity)
                               - COALESCE(ol.discount_amount, 0),
                           2)
                       )                                      AS saved_line_total,
                       0                                      AS product_selling_price,
                       0                                      AS product_mrp,
                       -- total_price = taxable base after discount.
                       -- RETAIL lines are GST-inclusive, so split taxable/GST
                       -- here instead of letting a zero gst_amount snapshot
                       -- make the challan look non-GST.
                       CASE UPPER(COALESCE(o.order_type,'WHOLESALE'))
                       WHEN 'RETAIL' THEN
                           ROUND(
                               COALESCE(
                                   ol.billing_total,
                                   ol.total_price,
                                   ROUND(
                                       ol.unit_price * COALESCE(ol.billing_qty, ol.quantity)
                                       - COALESCE(ol.discount_amount, 0),
                                   2)
                               )
                               * 100 / NULLIF(100 + COALESCE(ol.gst_percent,0), 0),
                           2)
                       ELSE
                           COALESCE(
                               ol.billing_total,
                               ol.total_price,
                               ROUND(
                                   ol.unit_price * COALESCE(ol.billing_qty, ol.quantity)
                                   - COALESCE(ol.discount_amount, 0),
                               2)
                           )
                       END AS total_price,
                       ol.gst_percent,
                       -- gst_amount: use stored value first (correctly computed by billing engine);
                       -- fallback recomputes on the POST-discount taxable base, not gross.
                       -- Bug fix: old fallback used unit_price times qty times GST percent
                       -- which ignored discount.
                       CASE
                       WHEN COALESCE(ol.gst_amount,0) > 0 THEN ol.gst_amount
                       WHEN COALESCE(ol.gst_percent,0) > 0 THEN
                           CASE UPPER(COALESCE(o.order_type,'WHOLESALE'))
                           WHEN 'RETAIL' THEN
                               ROUND(
                                   COALESCE(
                                       ol.billing_total,
                                       ol.total_price,
                                       ROUND(
                                           ol.unit_price * COALESCE(ol.billing_qty,ol.quantity)
                                           - COALESCE(ol.discount_amount,0),
                                       2)
                                   )
                                   * COALESCE(ol.gst_percent,0)
                                   / NULLIF(100 + COALESCE(ol.gst_percent,0), 0),
                               2)
                           ELSE
                               ROUND(
                                   COALESCE(
                                       ol.billing_total,
                                       ol.total_price,
                                       ROUND(
                                           ol.unit_price * COALESCE(ol.billing_qty,ol.quantity)
                                           - COALESCE(ol.discount_amount,0),
                                       2)
                                   )
                                   * COALESCE(ol.gst_percent,0) / 100,
                               2)
                           END
                       ELSE 0
                       END AS gst_amount,
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
                saved_total = float(
                    line.get("saved_line_total")
                    or line.get("total_price")
                    or 0
                )
                order_type = str(line.get("order_type") or "WHOLESALE").upper()
                gst_amt    = float(line.get("gst_amount") or 0)
                if order_type == "RETAIL":
                    base_price = round(max(saved_total - gst_amt, 0), 2)
                    line_total = round(saved_total, 2)
                else:
                    base_price = round(saved_total, 2)
                    line_total = round(saved_total + gst_amt, 2)
                _line_id_s = str(line.get("id") or "").strip()
                if _line_id_s:
                    _selected_line_ids_written.add(_line_id_s)
                _selected_line_count += 1

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
                # Retail billing_total is GST-inclusive. Wholesale billing_total
                # is taxable/base value and GST is added at billing time.
                _selected_line_total = round(_selected_line_total + float(line_total or 0), 2)

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
                         quantity, unit_price, total_price, line_total, gst_percent)
                        SELECT %(challan_id)s, %(order_id)s::uuid,
                               %(order_line_id)s::uuid,
                               %(product_name)s, %(brand)s, %(eye_side)s,
                               %(quantity)s, %(unit_price)s, %(total_price)s,
                               %(line_total)s, %(gst_percent)s
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
                        "gst_percent":   float(line.get("gst_percent") or 0),
                    }))

                # 2) Over-billing guard
                _is_svc = bool(line.get("is_service_line")) or                            str(line.get("eye_side","")).upper() in ("S","SERVICE")

                # Ceiling for billed_qty: always quantity.
                # Service lines (COLOURING/FITTING) have allocated_qty=0 which is NOT NULL,
                # so COALESCE(allocated_qty, quantity) would return 0 — silently skipping
                # the UPDATE. Using quantity directly handles all cases correctly.
                guard = _q("""
                    SELECT COALESCE(billed_qty, 0) + %(qty)s <= quantity AS allowed
                    FROM order_lines
                    WHERE id = %(line_id)s::uuid
                """, {"qty": line_qty, "line_id": line.get("id")})
                if not guard or not guard[0].get("allowed"):
                    if not _is_svc:
                        raise Exception(
                            f"Over-billing prevented: line {line.get('id')} "
                            f"— billed qty would exceed ordered quantity."
                        )
                    # Service line: allow anyway — quantity is the true ceiling

                tx_steps.append(("""
                    UPDATE order_lines
                    SET billed_qty = COALESCE(billed_qty, 0) + %(qty)s
                    WHERE id = %(line_id)s::uuid
                      AND COALESCE(billed_qty, 0) + %(qty)s <= quantity
                """, {
                    "qty":     line_qty,
                    "line_id": line.get("id"),
                }))

                # 3a) Mark purchase_acknowledgements as INVOICED
                tx_steps.append(("""
                    UPDATE purchase_acknowledgements
                    SET billing_status = 'INVOICED',
                        acknowledged_at = NOW()
                    WHERE order_line_id = %(line_id)s::uuid
                      AND UPPER(COALESCE(billing_status,'')) IN
                          ('PURCHASE_ACKED','PROCURED','READY_FOR_BILLING','LOCKED')
                """, {"line_id": line.get("id")}))

                # 3) Document ledger row
                tx_steps.append(("""
                    INSERT INTO document_ledger
                        (doc_type, doc_id, doc_no, order_id, order_line_id,
                         party_id, product_id,
                         quantity, base_amount, tax_amount, total_amount)
                    VALUES
                        (%(dt)s, %(did)s::uuid, %(dno)s,
                         %(oid)s::uuid, %(olid)s::uuid,
                         %(pid)s, NULLIF(%(prid)s, '')::uuid,
                         %(qty)s, %(base)s, %(tax)s, %(total)s)
                """, {
                    "dt":    "CHALLAN",
                    "did":   challan_id,
                    "dno":   challan_no or "",
                    "oid":   order_id,
                    "olid":  str(line.get("id") or ""),
                    "pid":   real_party_id,
                    "prid":  str(line.get("product_id") or "") or None,
                    "qty":   line_qty,
                    "base":  base_price,
                    "tax":   gst_amt,
                    "total": line_total,
                }))

        if _selected_line_count <= 0:
            raise ValueError(
                "❌ Cannot create challan — no active unbilled order lines matched "
                "the selected billing lines. No challan was created."
            )
        if _selected_line_total <= 0:
            raise ValueError(
                "❌ Cannot create challan — selected billing lines total ₹0.00. "
                "Fix line pricing before creating challan."
            )
        if line_ids:
            _missing_lids = set(str(x) for x in line_ids) - _selected_line_ids_written
            if _missing_lids:
                _short = ", ".join(sorted(x[:8] for x in _missing_lids))
                raise ValueError(
                    "❌ Cannot create challan — selected line(s) are already billed, "
                    f"deleted, or no longer billable: {_short}. Refresh the order."
                )

        # Final step: recompute challan totals from actual written lines.
        # This corrects any mismatch between the UI-computed total (which may
        # include lines that the DB re-query filtered out as already-billed)
        # and the lines actually committed to challan_lines.
        # total_amount = sum of taxable base (total_price per line)
        # total_tax    = grand_total - total_amount
        # grand_total  = sum of line_total (tax-inclusive for retail, ex-tax for wholesale)
        tx_steps.append(("""
            WITH sums AS (
                SELECT
                    COALESCE(SUM(COALESCE(total_price, line_total, 0)), 0)::numeric AS base_total,
                    COALESCE(SUM(COALESCE(line_total, total_price, 0)), 0)::numeric AS raw_grand,
                    COALESCE(
                        SUM(COALESCE(line_total, 0)) -
                        SUM(COALESCE(total_price, COALESCE(line_total, 0))),
                    0)::numeric AS tax_total
                FROM challan_lines
                WHERE challan_id = %(cid)s::uuid
                  AND NOT COALESCE(is_deleted, FALSE)
            )
            UPDATE challans c SET
                total_amount = ROUND(sums.base_total, 2),
                total_tax = ROUND(sums.tax_total, 2),
                grand_total = ROUND(sums.raw_grand, 0),
                round_off_amount = ROUND(ROUND(sums.raw_grand, 0) - sums.raw_grand, 2)
            FROM sums
            WHERE c.id = %(cid)s::uuid
        """, {"cid": challan_id}))
        tx_steps.append(("""
            INSERT INTO document_ledger
                (doc_type, doc_id, doc_no, order_id, order_line_id,
                 party_id, product_id, quantity, base_amount, tax_amount, total_amount)
            SELECT
                'CHALLAN_ROUND_OFF', c.id, c.challan_no,
                NULLIF(%(oid)s, '')::uuid, NULL,
                NULLIF(%(pid)s, '')::uuid, NULL,
                1, 0, 0, c.round_off_amount
            FROM challans c
            WHERE c.id = %(cid)s::uuid
              AND ABS(COALESCE(c.round_off_amount, 0)) >= 0.005
              AND NOT EXISTS (
                  SELECT 1 FROM document_ledger dl
                  WHERE dl.doc_type = 'CHALLAN_ROUND_OFF'
                    AND dl.doc_id = c.id
              )
        """, {
            "cid": challan_id,
            "oid": _deduped[0] if _deduped else "",
            "pid": real_party_id or "",
        }))

        # Execute the entire challan atomically — header + all lines together
        ok = _run_transaction(tx_steps)
        if not ok:
            raise Exception("Transaction failed — challan not committed.")

        # ── Advance order status → CHALLANED (post-transaction) ──────────
        # Done after _run_transaction so our UPDATE never enters the batch
        # (run_transaction may split multi-line SQL, causing parse errors).
        # A post-commit status write is safe here: challan is already durable.
        for _coid in list(dict.fromkeys(_deduped)):
            if not _coid:
                continue
            try:
                _prev_st = (_q("SELECT status FROM orders WHERE id = %(o)s::uuid LIMIT 1",
                               {"o": _coid}) or [{}])[0].get("status", "")
                if _prev_st not in ("BILLED","CHALLANED","INVOICED","CANCELLED","VOID","CLOSED"):
                    _write("UPDATE orders SET status='CHALLANED', updated_at=NOW() WHERE id=%(o)s::uuid",
                           {"o": _coid})
                    _write(
                        "INSERT INTO order_status_history "
                        "(history_id,order_id,from_status,to_status,changed_at,changed_by_name,remarks) "
                        "VALUES (gen_random_uuid(),%(o)s::uuid,%(f)s,'CHALLANED',NOW(),'billing_system',"
                        "'Challan raised — status advanced to CHALLANED')",
                        {"o": _coid, "f": _prev_st},
                    )
            except Exception as _cst_e:
                import logging
                logging.getLogger(__name__).warning(
                    "[create_challan] status→CHALLANED failed (non-fatal): %s", _cst_e
                )

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
                WITH sums AS (
                    SELECT
                        (
                            SELECT COALESCE(SUM(COALESCE(total_price, line_total, 0)), 0)
                            FROM challan_lines
                            WHERE challan_id = %(cid)s::uuid
                              AND NOT COALESCE(is_deleted, FALSE)
                        ) + (
                            SELECT COALESCE(SUM(COALESCE(base_amount, 0)), 0)
                            FROM challan_service_charges
                            WHERE challan_id = %(cid)s::uuid
                        ) AS base_total,
                        (
                            SELECT COALESCE(SUM(COALESCE(line_total, total_price, 0)), 0)
                            FROM challan_lines
                            WHERE challan_id = %(cid)s::uuid
                              AND NOT COALESCE(is_deleted, FALSE)
                        ) + (
                            SELECT COALESCE(SUM(COALESCE(total_amount, 0)), 0)
                            FROM challan_service_charges
                            WHERE challan_id = %(cid)s::uuid
                        ) AS raw_grand,
                        (
                            SELECT COALESCE(SUM(COALESCE(gst_amount, 0)), 0)
                            FROM challan_service_charges
                            WHERE challan_id = %(cid)s::uuid
                        ) + (
                            SELECT COALESCE(
                                SUM(COALESCE(line_total, 0)) -
                                SUM(COALESCE(total_price, COALESCE(line_total, 0))),
                            0)
                            FROM challan_lines
                            WHERE challan_id = %(cid)s::uuid
                              AND NOT COALESCE(is_deleted, FALSE)
                        ) AS tax_total
                )
                UPDATE challans c SET
                    total_amount = ROUND(sums.base_total, 2),
                    total_tax = ROUND(sums.tax_total, 2),
                    grand_total = ROUND(sums.raw_grand, 0),
                    round_off_amount = ROUND(ROUND(sums.raw_grand, 0) - sums.raw_grand, 2)
                FROM sums
                WHERE c.id = %(cid)s::uuid
            """, {"cid": challan_id})
            _write("""
                INSERT INTO document_ledger
                    (doc_type, doc_id, doc_no, order_id, order_line_id,
                     party_id, product_id, quantity, base_amount, tax_amount, total_amount)
                SELECT
                    'CHALLAN_ROUND_OFF', c.id, c.challan_no,
                    NULL, NULL, c.party_id, NULL, 1, 0, 0, c.round_off_amount
                FROM challans c
                WHERE c.id = %(cid)s::uuid
                  AND ABS(COALESCE(c.round_off_amount, 0)) >= 0.005
                  AND NOT EXISTS (
                      SELECT 1 FROM document_ledger dl
                      WHERE dl.doc_type = 'CHALLAN_ROUND_OFF'
                        AND dl.doc_id = c.id
                  )
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
                 remarks: str = "", party_name: Optional[str] = None) -> Optional[str]:
    """Create a new invoice (from challan or direct)
    
    RULE: RETAIL orders MUST have a challan_id — direct invoice not allowed.
    WHOLESALE orders may use direct invoice if billing_preference=DIRECT_INVOICE.
    """
    order_ids = _resolve_order_ids(order_ids)
    if order_ids and not challan_id:
        _enforce_billing_preflight(order_ids, None)

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
        _ensure_roundoff_columns()
        _round_off_amount = _roundoff_amount(total_amount + total_tax)
        grand_total = _round_to_rupee(total_amount + total_tax)
        if challan_id:
            _ch_round = _q("""
                SELECT total_amount, total_tax, grand_total, round_off_amount
                FROM challans
                WHERE id = %(cid)s::uuid LIMIT 1
            """, {"cid": challan_id})
            if _ch_round:
                total_amount = float(_ch_round[0].get("total_amount") or total_amount or 0)
                total_tax = float(_ch_round[0].get("total_tax") or total_tax or 0)
                grand_total = float(_ch_round[0].get("grand_total") or grand_total or 0)
                _round_off_amount = float(_ch_round[0].get("round_off_amount") or 0)
        due_date = date.today() + timedelta(days=due_days)

        _otype_rows = _q("""
            SELECT UPPER(COALESCE(order_type, '')) AS order_type
            FROM orders
            WHERE id::text = ANY(%(oids)s)
            ORDER BY CASE WHEN UPPER(COALESCE(order_type,'')) = 'RETAIL' THEN 0 ELSE 1 END
            LIMIT 1
        """, {"oids": [str(o) for o in order_ids]}) if order_ids else []
        _invoice_order_type = str((_otype_rows[0].get("order_type") if _otype_rows else "") or "").upper()
        _gst_included_flag = (_invoice_order_type != "WHOLESALE")

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
        _challan_pay_rows = _q("""
            SELECT COALESCE(SUM(amount), 0) AS tot
            FROM payments
            WHERE challan_id = %(cid)s::uuid
              AND COALESCE(is_deleted, FALSE) = FALSE
        """, {"cid": challan_id}) if challan_id else []
        _advance_paid    = round(float((_adv_rows[0]["tot"] if _adv_rows else 0) or 0), 2)
        _challan_paid    = round(float((_challan_pay_rows[0]["tot"] if _challan_pay_rows else 0) or 0), 2)
        _invoice_paid    = round(_advance_paid + _challan_paid, 2)
        _balance_due     = round(max(grand_total - _invoice_paid, 0), 2)
        _payment_status  = (
            "EXCESS" if _invoice_paid - grand_total > 0.50
            else "PAID" if _balance_due <= 0.50
            else "PARTIAL" if _invoice_paid > 0
            else "UNPAID"
        )

        success = _write("""
            INSERT INTO invoices 
            (id, invoice_no, challan_id, party_id, order_ids, 
             invoice_date, due_date, total_amount, total_tax, grand_total,
             round_off_amount,
             amount_paid, balance_due,
             status, payment_status, created_by, remarks,
             tally_synced, gst_included)
            VALUES (%(id)s, %(invoice_no)s, %(challan_id)s, %(party_id)s, 
                    %(order_ids)s, %(invoice_date)s, %(due_date)s, 
                    %(total_amount)s, %(total_tax)s, %(grand_total)s,
                    %(round_off_amount)s,
                    %(amount_paid)s, %(balance_due)s,
                    %(status)s, %(payment_status)s, %(created_by)s, %(remarks)s,
                    FALSE, %(gst_included)s)
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
            "round_off_amount": _round_off_amount,
            "amount_paid":    _invoice_paid,
            "balance_due":    _balance_due,
            "status":         "PAID" if _payment_status in ("PAID", "EXCESS") else "PENDING",
            "payment_status": _payment_status,
            "created_by":     st.session_state.get("user_name", "System"),
            "remarks":        remarks,
            "gst_included":   _gst_included_flag,
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
                    total = float(line.get("line_total") or 0)
                    # Preserve challan snapshot. Recomputing from unit_price would
                    # ignore order-line discount cancellation/continuation.
                    if total > 0 and total >= base:
                        tax = round(total - base, 2)
                    else:
                        tax = float(line.get("gst_amount") or 0)
                        if total <= 0:
                            total = round(base + tax, 2)
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
                        product_id   = str(line.get("product_id") or "") or None,
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
                               COALESCE(
                                   ol.billing_total,
                                   ol.total_price,
                                   ROUND(ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0), 2)
                               ) AS final_total,
                               ol.gst_percent,
                               CASE
                               WHEN COALESCE(ol.gst_amount,0) > 0 THEN ol.gst_amount
                               WHEN UPPER(COALESCE(o.order_type,'WHOLESALE')) = 'RETAIL' THEN
                                   ROUND(
                                       COALESCE(
                                           ol.billing_total,
                                           ol.total_price,
                                           ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0)
                                       )
                                       - (
                                           COALESCE(
                                               ol.billing_total,
                                               ol.total_price,
                                               ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0)
                                           ) / (1 + COALESCE(ol.gst_percent,0)/100)
                                       ),
                                       2
                                   )
                               ELSE
                                   ROUND(
                                       COALESCE(
                                           ol.billing_total,
                                           ol.total_price,
                                           ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0)
                                       ) * COALESCE(ol.gst_percent, 0) / 100,
                                       2
                                   )
                               END AS gst_amount,
                               ol.eye_side,
                               ol.sph, ol.cyl, ol.axis, ol.add_power
                        FROM order_lines ol
                        JOIN  orders o    ON o.id  = ol.order_id
                        LEFT JOIN products p ON p.id = ol.product_id
                        WHERE ol.order_id = %(order_id)s::uuid
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                    """, {"order_id": order_id})

                    for line in order_lines:
                        final_total = float(line.get("final_total") or 0)
                        tax   = float(line.get("gst_amount") or 0)
                        ot    = str(line.get("order_type") or "WHOLESALE").upper()
                        if ot == "RETAIL":
                            base  = round(max(final_total - tax, 0), 2)
                            total = round(final_total, 2)
                        else:
                            base  = round(final_total, 2)
                            total = round(final_total + tax, 2)
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
                            product_id   = str(line.get("product_id") or "") or None,
                            quantity     = int(line.get("quantity") or 0),
                            base_amount  = base,
                            tax_amount   = tax,
                            total_amount = total,
                        )

            if abs(float(_round_off_amount or 0)) >= 0.005:
                _write("""
                    INSERT INTO document_ledger
                        (doc_type, doc_id, doc_no, order_id, order_line_id,
                         party_id, product_id, quantity, base_amount, tax_amount, total_amount)
                    VALUES
                        ('INVOICE_ROUND_OFF', %(did)s::uuid, %(dno)s,
                         NULLIF(%(oid)s, '')::uuid, NULL,
                         NULLIF(%(pid)s, '')::uuid, NULL,
                         1, 0, 0, %(amt)s)
                    ON CONFLICT DO NOTHING
                """, {
                    "did": invoice_id,
                    "dno": invoice_no or "",
                    "oid": str(order_ids[0]) if order_ids else "",
                    "pid": party_id or "",
                    "amt": _round_off_amount,
                })

            if challan_id:
                try:
                    _write("""
                        UPDATE payments
                        SET invoice_id = %(iid)s::uuid
                        WHERE challan_id = %(cid)s::uuid
                          AND COALESCE(is_deleted, FALSE) = FALSE
                          AND invoice_id IS NULL
                    """, {"iid": invoice_id, "cid": challan_id})
                    _write("""
                        UPDATE challans
                        SET status = 'INVOICED',
                            updated_at = NOW()
                        WHERE id = %(cid)s::uuid
                          AND status NOT IN ('CANCELLED', 'VOID')
                    """, {"cid": challan_id})
                    _write("""
                        WITH inv AS (
                            SELECT id, order_ids, COALESCE(grand_total,0) AS gt
                            FROM invoices WHERE id = %(iid)s::uuid
                        ),
                        paid AS (
                            SELECT inv.id,
                                   COALESCE((
                                       SELECT SUM(p.amount)
                                       FROM payments p
                                       WHERE p.invoice_id = inv.id
                                         AND COALESCE(p.is_deleted, FALSE) = FALSE
                                   ), 0)
                                   +
                                   COALESCE((
                                       SELECT SUM(p.amount)
                                       FROM payments p
                                       WHERE p.advance_for_order_id::text = ANY(inv.order_ids::text[])
                                         AND (COALESCE(p.is_advance,FALSE)
                                              OR UPPER(COALESCE(p.payment_type,''))='ADVANCE')
                                         AND COALESCE(p.is_deleted, FALSE) = FALSE
                                   ), 0) AS amt
                            FROM inv
                        )
                        UPDATE invoices
                        SET amount_paid = paid.amt,
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
                          AND invoices.id = %(iid)s::uuid
                    """, {"iid": invoice_id})
                except Exception as _ch_inv_sync_e:
                    import logging
                    logging.getLogger(__name__).warning(
                        "[create_invoice] challan payment/status sync failed: %s",
                        _ch_inv_sync_e,
                    )

            # ── Party ledger DEBIT: invoice raised ──────────────────────
            try:
                _by_ldr = st.session_state.get("user_name", "System")
                _pn_rows = _q("SELECT party_name FROM parties WHERE id=%s::uuid LIMIT 1",
                              (party_id,)) if party_id else []
                _pn_ldr = (_pn_rows[0]["party_name"] if _pn_rows else "") or ""
                if not _pn_ldr:
                    _oid_vals = [str(_o) for _o in (order_ids or []) if str(_o or "").strip()]
                    if _oid_vals:
                        _ord_party_rows = _q("""
                            SELECT COALESCE(NULLIF(o.party_name, ''), NULLIF(o.patient_name, ''), '') AS party_name
                            FROM orders o
                            WHERE o.id::text = ANY(%s)
                               OR o.order_no = ANY(%s)
                            ORDER BY o.created_at DESC
                            LIMIT 1
                        """, (_oid_vals, _oid_vals)) or []
                        _pn_ldr = (_ord_party_rows[0].get("party_name") if _ord_party_rows else "") or ""
                if not _pn_ldr and challan_id:
                    _chal_party_rows = _q("""
                        SELECT COALESCE(NULLIF(o.party_name, ''), NULLIF(o.patient_name, ''), '') AS party_name
                        FROM challan_lines cl
                        JOIN orders o ON o.id = cl.order_id
                        WHERE cl.challan_id = %s::uuid
                          AND NOT COALESCE(cl.is_deleted, FALSE)
                        ORDER BY o.created_at DESC
                        LIMIT 1
                    """, (challan_id,)) or []
                    _pn_ldr = (_chal_party_rows[0].get("party_name") if _chal_party_rows else "") or ""
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

            # ── Advance order status → INVOICED ─────────────────────────
            # The DB trigger auto_close_order_on_billing only sets BILLED when
            # all lines are fully billed. INVOICED must be set explicitly here
            # so the backoffice lock engages and the status badge updates.
            try:
                from modules.sql_adapter import run_write as _rw_inv_st
                for _inv_oid in (order_ids or []):
                    if not _inv_oid:
                        continue
                    _rw_inv_st(
                        """
                        UPDATE orders
                        SET status     = 'INVOICED',
                            updated_at = NOW()
                        WHERE id = %(oid)s::uuid
                          AND status NOT IN ('CANCELLED','VOID','CLOSED')
                        """,
                        {"oid": str(_inv_oid)},
                    )
                    _rw_inv_st(
                        """
                        INSERT INTO order_status_history
                            (history_id, order_id, from_status, to_status,
                             changed_at, changed_by_name, remarks)
                        SELECT gen_random_uuid(), id,
                               status, 'INVOICED',
                               NOW(), 'billing_system',
                               'Invoice raised — status advanced to INVOICED'
                        FROM orders
                        WHERE id = %(oid)s::uuid
                          AND status NOT IN ('INVOICED','CANCELLED','VOID','CLOSED')
                        """,
                        {"oid": str(_inv_oid)},
                    )
            except Exception as _inv_st_e:
                import logging
                logging.getLogger(__name__).warning(
                    "[create_invoice] status update failed (non-fatal): %s", _inv_st_e
                )

            # ── Mark job_master stage → BILLED + write audit event ──────────
            # set_job_billed_lock() closes the job but keeps current_stage=READY_TO_BILL.
            # Explicitly set stage to BILLED so production panel and History tab
            # show the correct final state and the audit trail is complete.
            try:
                from modules.sql_adapter import run_write as _rw_jb, run_query as _rq_jb
                _jb_lids = []
                if challan_id:
                    _cl_jb = _rq_jb(
                        "SELECT order_line_id::text FROM challan_lines "
                        "WHERE challan_id=%(c)s::uuid AND NOT COALESCE(is_deleted,FALSE)",
                        {"c": challan_id}
                    ) or []
                    _jb_lids = [r["order_line_id"] for r in _cl_jb]
                else:
                    for _jb_oid in order_ids:
                        _ol_jb = _rq_jb(
                            "SELECT id::text FROM order_lines "
                            "WHERE order_id=%(o)s::uuid AND NOT COALESCE(is_deleted,FALSE)",
                            {"o": _jb_oid}
                        ) or []
                        _jb_lids += [r["id"] for r in _ol_jb]

                for _jb_lid in _jb_lids:
                    # Each eye is independent — one failure must NOT skip the others.
                    try:
                        _jm_rows = _rq_jb(
                            "SELECT id::text FROM job_master "
                            "WHERE order_line_id=%(l)s::uuid LIMIT 1",
                            {"l": _jb_lid}
                        ) or []
                        if not _jm_rows:
                            continue
                        _jm_id = _jm_rows[0]["id"]
                        # UPDATE first — always attempt this
                        _rw_jb(
                            "UPDATE job_master SET current_stage='BILLED', is_closed=TRUE, "
                            "updated_at=NOW() WHERE id=%(j)s::uuid",
                            {"j": _jm_id}
                        )
                        # Audit event — separate try so UPDATE is never rolled back by this
                        try:
                            _rw_jb(
                                "INSERT INTO job_stage_events "
                                "(id, job_id, stage_id, stage_code, remarks, created_at) "
                                "SELECT gen_random_uuid(), %(j)s::uuid, m.id, m.stage_code, "
                                "       'Invoice raised — job closed and marked BILLED', NOW() "
                                "FROM job_stage_master m "
                                "WHERE m.stage_code = 'BILLED' LIMIT 1",
                                {"j": _jm_id}
                            )
                        except Exception as _jse_err:
                            import logging
                            logging.getLogger(__name__).warning(
                                "[create_invoice] job_stage_events insert failed "
                                "for line %s (job update already committed): %s",
                                _jb_lid, _jse_err
                            )
                    except Exception as _jb_line_err:
                        import logging
                        logging.getLogger(__name__).warning(
                            "[create_invoice] job_master BILLED stamp failed "
                            "for order_line %s (non-fatal, other eyes continue): %s",
                            _jb_lid, _jb_line_err
                        )
            except Exception as _jb_err:
                import logging
                logging.getLogger(__name__).warning(
                    "[create_invoice] job_master BILLED outer block failed: %s", _jb_err
                )

            # ── Payment allocation is the final authority ─────────────
            # create_invoice historically pre-filled every partial invoice with
            # the full order advance. The allocator consumes advances
            # sequentially across all invoices for the order and also includes
            # challan-level receipts when a challan is converted.
            try:
                from modules.db.advance_allocator import allocate_order_advance
                for _alloc_oid in [str(_o) for _o in (order_ids or []) if str(_o or "").strip()]:
                    allocate_order_advance(_alloc_oid)
            except Exception as _alloc_e:
                import logging
                logging.getLogger(__name__).warning(
                    "[create_invoice] advance allocation failed: %s", _alloc_e
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

            # ── Entitlement hook: invoice can earn future rewards ──────────
            # FUTURE_ENTITLEMENT club offers create a redeem-later credit,
            # e.g. buy Platinum now, get Silver at Rs.1 on a later order.
            try:
                import json as _ent_json
                import uuid as _ent_uuid
                from modules.pricing.entitlement_engine import create_entitlement
                from modules.sql_adapter import run_query as _rq_ent

                try:
                    _ent_uuid.UUID(str(party_id or ""))
                    _ent_party_id = str(party_id)
                except Exception:
                    _ent_party_id = ""

                if _ent_party_id:
                    _ent_party_name = _pn_ldr or ""
                    if not _ent_party_name and order_ids:
                        _ent_ord_party = _rq_ent("""
                            SELECT COALESCE(party_name, patient_name, '') AS party_name
                            FROM orders
                            WHERE id = %(oid)s::uuid
                            LIMIT 1
                        """, {"oid": str(order_ids[0])}) or []
                        _ent_party_name = (_ent_ord_party[0].get("party_name") if _ent_ord_party else "") or ""

                    _ent_offers = _rq_ent("""
                        SELECT co.id::text, co.name,
                               co.trigger_product_ids,
                               co.reward_product_ids,
                               COALESCE(co.entitlement_valid_days, 30) AS valid_days,
                               COALESCE(co.nominal_billing_value, 1)::float AS billing_value
                        FROM club_offers co
                        WHERE COALESCE(co.active, TRUE) = TRUE
                          AND COALESCE(co.application_mode, 'SAME_ORDER') = 'FUTURE_ENTITLEMENT'
                          AND (co.valid_from IS NULL OR co.valid_from <= CURRENT_DATE)
                          AND (co.valid_to IS NULL OR co.valid_to >= CURRENT_DATE)
                    """) or []

                    if _ent_offers:
                        _inv_lines = _rq_ent("""
                            SELECT DISTINCT
                                   ol.product_id::text AS product_id,
                                   COALESCE(il.product_name, p.product_name, '') AS product_name,
                                   il.order_id::text AS order_id
                            FROM invoice_lines il
                            LEFT JOIN order_lines ol ON ol.id = il.order_line_id
                            LEFT JOIN products p ON p.id = ol.product_id
                            WHERE il.invoice_id = %(iid)s::uuid
                              AND COALESCE(il.is_deleted, FALSE) = FALSE
                              AND ol.product_id IS NOT NULL
                        """, {"iid": invoice_id}) or []
                        _inv_pids = {str(l.get("product_id")) for l in _inv_lines if l.get("product_id")}

                        for _eo in _ent_offers:
                            try:
                                _tpids_raw = _eo.get("trigger_product_ids") or []
                                _tpids = set(_ent_json.loads(_tpids_raw) if isinstance(_tpids_raw, str) else _tpids_raw)
                            except Exception:
                                _tpids = set()
                            _matches = _inv_pids & {str(x) for x in _tpids if x}
                            if not _matches:
                                continue

                            try:
                                _rpids_raw = _eo.get("reward_product_ids") or []
                                _rpids = _ent_json.loads(_rpids_raw) if isinstance(_rpids_raw, str) else _rpids_raw
                            except Exception:
                                _rpids = []

                            _trigger_pid = next(iter(_matches), "")
                            _trigger_line = next((l for l in _inv_lines if str(l.get("product_id")) == _trigger_pid), {})
                            _trigger_order_id = str(_trigger_line.get("order_id") or (order_ids[0] if order_ids else ""))

                            for _rpid in [str(x) for x in (_rpids or []) if x]:
                                _already = _rq_ent("""
                                    SELECT 1
                                    FROM scheme_entitlements
                                    WHERE scheme_id = %(sid)s::uuid
                                      AND trigger_invoice_id = %(iid)s::uuid
                                      AND reward_product_id = %(rpid)s::uuid
                                      AND COALESCE(status, '') <> 'CANCELLED'
                                    LIMIT 1
                                """, {"sid": _eo.get("id"), "iid": invoice_id, "rpid": _rpid}) or []
                                if _already:
                                    continue

                                _rp = _rq_ent(
                                    "SELECT product_name FROM products WHERE id = %(id)s::uuid LIMIT 1",
                                    {"id": _rpid},
                                ) or []
                                create_entitlement(
                                    scheme_id=str(_eo.get("id") or ""),
                                    party_id=_ent_party_id,
                                    party_name=_ent_party_name,
                                    trigger_invoice_id=str(invoice_id or ""),
                                    trigger_order_id=_trigger_order_id,
                                    trigger_product_id=_trigger_pid,
                                    trigger_product_name=_trigger_line.get("product_name", ""),
                                    reward_product_id=_rpid,
                                    reward_product_name=(_rp[0].get("product_name", "") if _rp else ""),
                                    valid_days=int(_eo.get("valid_days") or 30),
                                    reward_billing_value=float(_eo.get("billing_value") or 1),
                                    notes=f"Auto-created from invoice {invoice_no} via {_eo.get('name','club offer')}",
                                )
            except Exception as _ent_e:
                import logging
                logging.getLogger(__name__).warning("[entitlement] invoice hook skipped: %s", _ent_e)

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
                                from modules.wa_hub import wa_document_attachment, wa_panel, wa_challan_made
                                from modules.settings.shop_master import get_unit_info
                                _ap  = st.session_state.get("ch_sel_party", "")
                                _pr  = _q("SELECT party_name, COALESCE(mobile,'') AS mobile FROM parties WHERE id=%s::uuid LIMIT 1", (_ap,)) if _ap else []
                                _pn  = _pr[0]["party_name"] if _pr else ""
                                _mob = _pr[0]["mobile"]     if _pr else ""
                                _sh  = get_unit_info("wholesale")
                                # Fix: pass actual order numbers instead of blank
                                _order_nos_wa = ", ".join(
                                    o.get("order_no", "") for o in pending_orders
                                    if st.session_state["ch_ord_sel"].get(str(o["id"]))
                                ) or ""
                                _msg = wa_challan_made(
                                    party      = _pn,
                                    order_no   = _order_nos_wa,
                                    challan_no = challan_no,
                                    grand_total= _ch_grand_incl,
                                    shop_name  = _sh.get("shop_name","DV Optical"),
                                    phone      = _sh.get("shop_phone",""),
                                )
                                wa_panel(_mob, _msg,
                                         key=f"wa_chal_{challan_no}",
                                         title="📲 WhatsApp — Challan Ready",
                                         expanded=True,
                                         party_name=_pn,
                                         attachments=[
                                             wa_document_attachment("challan", challan_no)
                                         ])
                            except Exception:
                                pass

                            # ── Payment collection panel ──────────────
                            # Shown immediately after challan creation so
                            # staff can record advance/full payment without
                            # navigating away. Looks up the new challan_id.
                            try:
                                _new_ch_rows = _q(
                                    "SELECT id::text AS challan_id, party_id::text, "
                                    "grand_total, amount_paid, balance_due, "
                                    "COALESCE((SELECT o.party_name FROM orders o "
                                    "WHERE o.id::text = ANY(challans.order_ids::text[]) "
                                    "ORDER BY o.created_at DESC LIMIT 1), '') AS order_party_name "
                                    "FROM challans WHERE challan_no=%s LIMIT 1",
                                    (challan_no,)
                                )
                                if _new_ch_rows:
                                    _new_cid  = _new_ch_rows[0]["challan_id"]
                                    _new_pid  = _new_ch_rows[0].get("party_id") or ""
                                    _new_amt  = float(_new_ch_rows[0].get("grand_total") or 0)
                                    _new_paid = float(_new_ch_rows[0].get("amount_paid") or 0)
                                    _new_pnm  = _new_ch_rows[0].get("order_party_name") or ""
                                    st.markdown("---")
                                    st.markdown(
                                        "<div style='background:#052e16;border:1px solid #166534;"
                                        "border-left:4px solid #22c55e;border-radius:8px;"
                                        "padding:8px 14px;margin-bottom:8px'>"
                                        "<b style='color:#86efac'>💳 Collect Payment</b>"
                                        "<span style='color:#4ade80;font-size:0.75rem;margin-left:8px'>"
                                        f"Challan {challan_no} · ₹{_new_amt:,.2f}</span></div>",
                                        unsafe_allow_html=True,
                                    )
                                    try:
                                        from modules.billing.payment_manager import render_record_payment
                                        render_record_payment(
                                            challan_id  = _new_cid,
                                            party_id    = _new_pid,
                                            party_name  = _new_pnm,
                                            grand_total = _new_amt,
                                            amount_paid = _new_paid,
                                            payment_type= "RECEIPT",
                                            label       = f"💰 Collect balance for {challan_no}",
                                            key_suffix  = f"ch_{challan_no}",
                                            context     = "inline_challan",
                                        )
                                    except ImportError:
                                        # Fallback: simple payment amount + method entry
                                        _pay_c1, _pay_c2, _pay_c3 = st.columns([2, 1, 1])
                                        _pay_amt = _pay_c1.number_input(
                                            "Amount Received (₹)",
                                            min_value=0.0,
                                            max_value=float(_new_amt),
                                            value=float(_new_amt),
                                            step=10.0,
                                            key=f"ch_pay_amt_{challan_no}",
                                        )
                                        _pay_method = _pay_c2.selectbox(
                                            "Method",
                                            ["CASH","UPI","CARD","BANK","CHEQUE","OTHER"],
                                            key=f"ch_pay_method_{challan_no}",
                                        )
                                        _pay_ref = _pay_c3.text_input(
                                            "Ref / UTR",
                                            placeholder="Optional",
                                            key=f"ch_pay_ref_{challan_no}",
                                        )
                                        if st.button(
                                            f"✅ Record ₹{_pay_amt:,.2f} {_pay_method}",
                                            key=f"ch_pay_save_{challan_no}",
                                            type="primary",
                                            use_container_width=True,
                                            disabled=_pay_amt <= 0,
                                        ):
                                            try:
                                                from modules.db.order_number_registry import alloc_doc_number
                                                _pay_no = alloc_doc_number("PAYMENT")
                                            except Exception:
                                                _pay_no = f"PAY/{date.today().strftime('%Y%m%d')}/{_uuid.uuid4().hex[:6].upper()}"
                                            _pay_ok = _write("""
                                                INSERT INTO payments
                                                    (id, payment_no, challan_id, party_id, party_name,
                                                     order_id,
                                                     amount, payment_mode, method, reference_no,
                                                     payment_date, payment_type,
                                                     is_advance, created_by)
                                                VALUES
                                                    (%(id)s::uuid, %(pno)s, %(cid)s::uuid,
                                                     NULLIF(%(pid)s,'')::uuid, %(pn)s,
                                                     (SELECT NULLIF(order_ids[1],'')::uuid FROM challans WHERE id=%(cid)s::uuid LIMIT 1),
                                                     %(amt)s, %(method)s, %(method)s, %(ref)s,
                                                     NOW(), 'RECEIPT', FALSE,
                                                     %(by)s)
                                            """, {
                                                "id":     str(_uuid.uuid4()),
                                                "pno":    _pay_no,
                                                "cid":    _new_cid,
                                                "pid":    _new_pid or "",
                                                "pn":     _new_pnm or None,
                                                "amt":    _pay_amt,
                                                "method": _pay_method,
                                                "ref":    _pay_ref or "",
                                                "by":     st.session_state.get("user_name","System"),
                                            })
                                            if _pay_ok:
                                                try:
                                                    from modules.db.billing_queries import update_challan_balance
                                                    update_challan_balance(_new_cid)
                                                except Exception:
                                                    pass
                                                st.success(f"✅ ₹{_pay_amt:,.2f} recorded via {_pay_method}")
                                            else:
                                                st.error("Payment recording failed")
                            except Exception as _pay_e:
                                st.caption(f"Payment panel error: {_pay_e}")

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
                _inv_id_for_pay = None
                _inv_amt_for_pay = 0.0
                _inv_pid_for_pay = ""
                try:
                    from modules.wa_hub import wa_document_attachment, wa_panel, wa_invoice_made
                    from modules.settings.shop_master import get_unit_info
                    _pr2 = _q("SELECT i.id::text AS inv_id, "
                              "COALESCE(p.party_name, (SELECT o.party_name FROM orders o "
                              "WHERE o.id::text = ANY(i.order_ids::text[]) ORDER BY o.created_at DESC LIMIT 1), '') AS party_name, "
                              "COALESCE(p.mobile,'') AS mobile, "
                              "COALESCE(i.grand_total,0) AS gt, COALESCE(i.balance_due,i.grand_total,0) AS bal, "
                              "COALESCE(i.amount_paid,0) AS paid, COALESCE(i.party_id::text,'') AS party_id "
                              "FROM invoices i LEFT JOIN parties p ON p.id=i.party_id "
                              "WHERE i.invoice_no=%s LIMIT 1", (invoice_no,))
                    if _pr2:
                        _sh2 = get_unit_info("wholesale")
                        _inv_id_for_pay  = _pr2[0].get("inv_id","")
                        _inv_amt_for_pay = float(_pr2[0].get("bal") or _pr2[0].get("gt") or 0)
                        _inv_pid_for_pay = _pr2[0].get("party_id","")
                        _inv_paid_for_pay = float(_pr2[0].get("paid") or 0)
                        _inv_party_for_pay = _pr2[0].get("party_name") or ""
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
                            party_name = _pr2[0]["party_name"],
                            attachments=[
                                wa_document_attachment("invoice", invoice_no)
                            ],
                        )
                except Exception:
                    pass

                # ── Payment collection panel ──────────────────────────
                if _inv_amt_for_pay > 0 and _inv_id_for_pay:
                    st.markdown("---")
                    st.markdown(
                        "<div style='background:#052e16;border:1px solid #166534;"
                        "border-left:4px solid #22c55e;border-radius:8px;"
                        "padding:8px 14px;margin-bottom:8px'>"
                        "<b style='color:#86efac'>💳 Collect Payment</b>"
                        f"<span style='color:#4ade80;font-size:0.75rem;margin-left:8px'>"
                        f"Invoice {invoice_no} · ₹{_inv_amt_for_pay:,.2f} balance due</span></div>",
                        unsafe_allow_html=True,
                    )
                    try:
                        from modules.billing.payment_manager import render_record_payment
                        render_record_payment(
                            invoice_id  = _inv_id_for_pay,
                            party_id    = _inv_pid_for_pay,
                            party_name  = _inv_party_for_pay,
                            grand_total = float(_pr2[0].get("gt") or 0) if '_pr2' in locals() and _pr2 else _inv_amt_for_pay,
                            amount_paid = _inv_paid_for_pay,
                            payment_type= "RECEIPT",
                            label       = f"💰 Collect balance for {invoice_no}",
                            key_suffix  = f"inv_{invoice_no}",
                            context     = "inline_invoice",
                        )
                    except ImportError:
                        _ip1, _ip2, _ip3 = st.columns([2, 1, 1])
                        _ip_amt    = _ip1.number_input("Amount Received (₹)", min_value=0.0,
                                                        max_value=_inv_amt_for_pay,
                                                        value=_inv_amt_for_pay, step=10.0,
                                                        key=f"inv_pay_amt_{invoice_no}")
                        _ip_method = _ip2.selectbox("Method",
                                                    ["CASH","UPI","CARD","BANK","CHEQUE","OTHER"],
                                                    key=f"inv_pay_method_{invoice_no}")
                        _ip_ref    = _ip3.text_input("Ref / UTR", placeholder="Optional",
                                                    key=f"inv_pay_ref_{invoice_no}")
                        if st.button(f"✅ Record ₹{_ip_amt:,.2f} {_ip_method}",
                                     key=f"inv_pay_save_{invoice_no}",
                                     type="primary", use_container_width=True,
                                     disabled=_ip_amt <= 0):
                            try:
                                from modules.db.order_number_registry import alloc_doc_number
                                _ip_no = alloc_doc_number("PAYMENT")
                            except Exception:
                                _ip_no = f"PAY/{date.today().strftime('%Y%m%d')}/{_uuid.uuid4().hex[:6].upper()}"
                            _ip_ok = _write("""
                                INSERT INTO payments
                                    (id, payment_no, invoice_id, party_id, party_name,
                                     order_id, amount,
                                     payment_mode, method, reference_no,
                                     payment_date, payment_type, is_advance, created_by)
                                VALUES
                                    (%(id)s::uuid, %(pno)s, %(iid)s::uuid,
                                     NULLIF(%(pid)s,'')::uuid,
                                     %(pn)s,
                                     (SELECT NULLIF(order_ids[1],'')::uuid FROM invoices WHERE id=%(iid)s::uuid LIMIT 1),
                                     %(amt)s, %(method)s, %(method)s, %(ref)s,
                                     NOW(), 'RECEIPT', FALSE, %(by)s)
                            """, {
                                "id":     str(_uuid.uuid4()),
                                "pno":    _ip_no,
                                "iid":    _inv_id_for_pay,
                                "pid":    _inv_pid_for_pay or "",
                                "pn":     _inv_party_for_pay or None,
                                "amt":    _ip_amt,
                                "method": _ip_method,
                                "ref":    _ip_ref or "",
                                "by":     st.session_state.get("user_name","System"),
                            })
                            if _ip_ok:
                                # Recalculate invoice balance_due
                                _write("""
                                    WITH inv AS (
                                        SELECT id, order_ids, COALESCE(grand_total,0) AS gt
                                        FROM invoices WHERE id = %(iid)s::uuid
                                    ),
                                    paid AS (
                                        SELECT inv.id,
                                               COALESCE((SELECT SUM(p.amount)
                                                         FROM payments p
                                                         WHERE p.invoice_id = inv.id
                                                           AND COALESCE(p.is_deleted,FALSE)=FALSE), 0)
                                               +
                                               COALESCE((SELECT SUM(p.amount)
                                                         FROM payments p
                                                         WHERE p.advance_for_order_id::text = ANY(inv.order_ids::text[])
                                                           AND (COALESCE(p.is_advance,FALSE)
                                                                OR UPPER(COALESCE(p.payment_type,''))='ADVANCE')
                                                           AND COALESCE(p.is_deleted,FALSE)=FALSE), 0)
                                               AS amt
                                        FROM inv
                                    )
                                    UPDATE invoices SET
                                        amount_paid = paid.amt,
                                        balance_due = GREATEST(COALESCE(grand_total,0) - paid.amt, 0),
                                        status = CASE
                                            WHEN COALESCE(status,'') IN ('CANCELLED','VOID') THEN status
                                            WHEN COALESCE(grand_total,0) - paid.amt <= 0.50 THEN 'PAID'
                                            ELSE 'ACTIVE' END,
                                        payment_status = CASE
                                            WHEN paid.amt - COALESCE(grand_total,0) > 0.50 THEN 'EXCESS'
                                            WHEN COALESCE(grand_total,0) - paid.amt <= 0.50 THEN 'PAID'
                                            WHEN paid.amt > 0 THEN 'PARTIAL'
                                            ELSE 'UNPAID' END,
                                        updated_at = NOW()
                                    FROM paid
                                    WHERE invoices.id = paid.id
                                      AND invoices.id = %(iid)s::uuid
                                """, {"iid": _inv_id_for_pay})
                                st.success(f"✅ ₹{_ip_amt:,.2f} recorded via {_ip_method}")
                            else:
                                st.error("Payment recording failed")
                    except Exception as _pe:
                        st.caption(f"Payment panel error: {_pe}")

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
                SELECT COALESCE(
                           ol.billing_total,
                           ol.total_price,
                           ROUND(ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0), 2)
                       ) AS total_price,
                       CASE
                       WHEN COALESCE(ol.gst_amount,0) > 0 THEN ol.gst_amount
                       WHEN UPPER(COALESCE(o.order_type,'WHOLESALE')) = 'RETAIL' THEN
                           ROUND(
                               COALESCE(
                                   ol.billing_total,
                                   ol.total_price,
                                   ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0)
                               )
                               - (
                                   COALESCE(
                                       ol.billing_total,
                                       ol.total_price,
                                       ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0)
                                   ) / (1 + COALESCE(ol.gst_percent,0)/100)
                               ),
                               2
                           )
                       ELSE
                           ROUND(
                               COALESCE(
                                   ol.billing_total,
                                   ol.total_price,
                                   ol.unit_price * ol.quantity - COALESCE(ol.discount_amount, 0)
                               ) * COALESCE(ol.gst_percent, 0) / 100,
                               2
                           )
                       END AS gst_amount,
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
