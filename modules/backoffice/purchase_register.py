"""
modules/backoffice/purchase_register.py
========================================
Purchase Register — single unified search across all purchase documents.

Sources:
  purchase_acknowledgements  → 📋 Challan / 🧾 Invoice  (order-linked)
  supplier_orders            → 📤 PO
  purchase_invoices          → 🏪 GRN  (stock replenishment)

Call from anywhere:  from modules.backoffice.purchase_register import render_purchase_register
"""

import streamlit as st
import datetime
import logging


# ── DB helpers ──────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)


def _clear_pr_cache() -> None:
    """Clear cached purchase-register loaders after transaction-based writes."""
    try:
        st.cache_data.clear()
    except Exception as e:
        log.debug("Purchase register cache clear skipped: %s", e)


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}")
        return []


def _rw(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        _clear_pr_cache()
        return True
    except Exception as e:
        st.error(f"Write: {e}")
        return False


def _fmt_pwr(row):
    parts = []
    try:
        if row.get("sph") is not None:
            parts.append(f"SPH {float(row['sph']):+.2f}")
        if row.get("cyl") and abs(float(row["cyl"])) > 0.01:
            parts.append(f"CYL {float(row['cyl']):+.2f}")
        if row.get("axis"):
            parts.append(f"AX {int(row['axis'])}")
        if row.get("add_power") and float(row.get("add_power") or 0) > 0:
            parts.append(f"ADD +{float(row['add_power']):.2f}")
    except Exception:
        pass
    return "  ".join(parts)


def _purchase_doc_ref(row):
    """Best visible purchase document ref for challan/invoice/direct import rows."""
    for key in ("challan_no", "invoice_no", "supplier_order_ref"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    path = str(row.get("invoice_file_path") or "").replace("\\", "/").strip()
    if path:
        name = path.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0]
        if stem:
            # Uploaded files are stored as date_token_invoiceNo.ext; show the
            # business document number, not the storage-safe prefix.
            parts = [p.strip() for p in stem.split("_") if p.strip()]
            if len(parts) >= 3 and any(ch.isdigit() for ch in parts[-1]):
                return parts[-1]
            return stem
    return "NO_DOC"


def _purchase_doc_label(row):
    """Human label matching the best visible purchase document ref."""
    if str(row.get("challan_no") or "").strip():
        return "Challan"
    if str(row.get("invoice_no") or "").strip():
        return "Invoice"
    return "Document"


@st.cache_data(ttl=120, show_spinner=False)
def _load_suppliers_for_purchase_register():
    """Suppliers/creditors available for purchase correction and posting."""
    return _q(
        "SELECT id::text AS id, party_name "
        "FROM parties "
        "WHERE UPPER(COALESCE(party_type,'')) IN "
        "('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB','CONTACT_LENS_SUPPLIER') "
        "AND COALESCE(is_active,TRUE)=TRUE "
        "ORDER BY party_name"
    )


def _purchase_picker_date_bounds():
    """Oldest/latest line dates that should be visible in the picker."""
    rows = _q(
        """
        SELECT
            MIN(COALESCE(document_date, acknowledged_at::date))::text AS min_date,
            MAX(COALESCE(document_date, acknowledged_at::date))::text AS max_date
        FROM purchase_acknowledgements
        WHERE (
            COALESCE(billing_status,'') IN ('PURCHASE_ACKED','PROCURED','READY','LOCKED')
            OR COALESCE(audit_status,'') = 'PENDING_INVENTORY_AUDIT'
          )
    """
    )
    if not rows:
        return None, None

    def _parse(raw):
        if not raw:
            return None
        try:
            return datetime.date.fromisoformat(str(raw)[:10])
        except Exception:
            return None

    return _parse(rows[0].get("min_date")), _parse(rows[0].get("max_date"))


def _oldest_purchase_picker_date():
    return _purchase_picker_date_bounds()[0]


def _keep_purchase_picker_date_visible(doc_date) -> None:
    """After rollback/release, widen the visible date range on next rerun."""
    if not doc_date:
        return
    try:
        if isinstance(doc_date, datetime.datetime):
            dt = doc_date.date()
        elif isinstance(doc_date, datetime.date):
            dt = doc_date
        else:
            dt = datetime.date.fromisoformat(str(doc_date)[:10])
    except Exception:
        return
    cur = st.session_state.get("pr_f_from")
    if not cur or dt < cur:
        st.session_state["pr_f_from"] = dt
    cur_to = st.session_state.get("pr_f_to")
    if not cur_to or dt > cur_to:
        st.session_state["pr_f_to"] = dt


# ── Data loaders ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30, show_spinner=False)
def _load_pa(sup, ref, prod, dfrom, dto):
    w = []
    p = {"df": str(dfrom), "dt": str(dto)}
    # If an exact doc/order search is typed, do not let the date window hide it.
    if not ref.strip():
        w.extend([
            "DATE(COALESCE(pa.document_date, pa.acknowledged_at::date)) >= %(df)s",
            "DATE(COALESCE(pa.document_date, pa.acknowledged_at::date)) <= %(dt)s",
        ])
    if sup.strip():
        w.append("LOWER(COALESCE(pa.supplier_name,'')) LIKE %(sup)s")
        p["sup"] = f"%{sup.strip().lower()}%"
    if ref.strip():
        w.append("(LOWER(COALESCE(pa.order_no,'')) LIKE %(ref)s"
                 " OR LOWER(COALESCE(pa.challan_no,'')) LIKE %(ref)s"
                 " OR LOWER(COALESCE(pa.invoice_no,'')) LIKE %(ref)s)")
        p["ref"] = f"%{ref.strip().lower()}%"
    if prod.strip():
        w.append("(LOWER(COALESCE(p.product_name,'')) LIKE %(prod)s"
                 " OR LOWER(COALESCE(pa.product_name,'')) LIKE %(prod)s)")
        p["prod"] = f"%{prod.strip().lower()}%"

    return _q(f"""
        SELECT
            pa.id::text                              AS pa_id,
            pa.order_line_id::text                   AS order_line_id,
            pa.product_id::text                      AS product_id,
            pa.order_no,
            pa.challan_no,
            pa.invoice_no,
            pa.supplier_name,
            pa.supplier_id::text                     AS supplier_id,
            COALESCE(o.patient_name,o.party_name,'—') AS patient_name,
            COALESCE(p.product_name, pa.our_product_name, pa.product_name,'—') AS product_name,
            COALESCE(p.unit,'PCS')                   AS unit,
            COALESCE(p.box_size,1)                   AS box_size,
            ol.eye_side,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            COALESCE(pa.received_qty, 1)             AS qty,
            COALESCE(pa.purchase_price,0)            AS purchase_price,
            COALESCE(pa.total_value,0)               AS total_value,
            pa.document_date::text                   AS doc_date,
            COALESCE(pa.invoice_file_path,'')        AS invoice_file_path,
            pa.is_price_locked,
            COALESCE(pa.transport,'')                AS transport,
            COALESCE(pa.lr_no,'')                    AS lr_no,
            -- Bug 3 fix: load billing_status so the challan picker gate works
            COALESCE(pa.billing_status,'')           AS billing_status,
            -- Bug 5 fix: load gst_percent so multi-challan invoices compute GST correctly
            COALESCE(ol.gst_percent, p.gst_percent, 18) AS gst_percent,
            -- Register visibility: is this line's invoice in purchase_invoices?
            CASE WHEN pi.invoice_no IS NOT NULL THEN TRUE ELSE FALSE END AS in_register,
            COALESCE(pi.payment_status, '')          AS register_payment_status,
            -- supplier / our product identity (migration 0009) for reporting
            COALESCE(pa.supplier_product_name,'')        AS supplier_product_name,
            COALESCE(pa.supplier_product_code,'')        AS supplier_product_code,
            COALESCE(pa.supplier_product_description,'')  AS supplier_product_description,
            COALESCE(pa.supplier_order_ref,'')           AS supplier_order_ref,
            COALESCE(pa.our_product_name,'')             AS our_product_name,
            COALESCE(pa.mapping_source,'')               AS mapping_source,
            COALESCE(pa.audit_status,'')                 AS audit_status,
            COALESCE(pa.audit_remarks,'')                AS audit_remarks,
            pa.inventory_posted_at::text                 AS inventory_posted_at
        FROM purchase_acknowledgements pa
        LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
        LEFT JOIN orders o       ON o.id  = ol.order_id
        LEFT JOIN products p     ON p.id  = ol.product_id
        LEFT JOIN purchase_invoices pi
               ON pa.invoice_no IS NOT NULL
              AND LOWER(pi.invoice_no) = LOWER(pa.invoice_no)
              AND COALESCE(pi.payment_status,'') != 'VOIDED'
        WHERE {" AND ".join(w)}
        ORDER BY pa.document_date DESC NULLS LAST, pa.acknowledged_at DESC
        LIMIT 400
    """, p)


@st.cache_data(ttl=30, show_spinner=False)
def _load_pa_audit(dfrom, dto, status_filter="PENDING"):
    status_filter = str(status_filter or "PENDING").upper()
    w = [
        "DATE(COALESCE(pa.document_date, pa.acknowledged_at::date)) >= %(df)s",
        "DATE(COALESCE(pa.document_date, pa.acknowledged_at::date)) <= %(dt)s",
        "pa.order_line_id IS NULL",
    ]
    p = {"df": str(dfrom), "dt": str(dto)}
    if status_filter == "PENDING":
        w.append("COALESCE(pa.audit_status,'PENDING_INVENTORY_AUDIT') = 'PENDING_INVENTORY_AUDIT'")
    elif status_filter != "ALL":
        w.append("COALESCE(pa.audit_status,'') = %(st)s")
        p["st"] = status_filter
    return _q(f"""
        SELECT
            pa.id::text AS pa_id,
            COALESCE(pa.audit_status,'PENDING_INVENTORY_AUDIT') AS audit_status,
            COALESCE(pa.audit_remarks,'') AS audit_remarks,
            pa.audited_by,
            pa.audited_at::text AS audited_at,
            pa.inventory_posted_at::text AS inventory_posted_at,
            pa.invoice_no,
            pa.challan_no,
            pa.document_date::text AS doc_date,
            COALESCE(pa.invoice_file_path,'') AS invoice_file_path,
            pa.supplier_name,
            pa.supplier_id::text AS supplier_id,
            COALESCE(pa.supplier_order_ref,'') AS supplier_order_ref,
            COALESCE(pa.our_product_name, pa.product_name, pa.supplier_product_name, '—') AS product_name,
            COALESCE(pa.our_product_id, pa.product_id)::text AS product_id,
            COALESCE(pa.supplier_product_name,'') AS supplier_product_name,
            COALESCE(pa.supplier_product_description, pa.notes, '') AS supplier_product_description,
            COALESCE(pa.supplier_order_ref,'') AS supplier_order_ref,
            COALESCE(pa.received_qty, pa.qty, 1) AS qty,
            COALESCE(pa.purchase_price, 0) AS purchase_price,
            COALESCE(pa.total_value, 0) AS total_value,
            COALESCE(pa.courier_gst_rate, 0) AS gst_percent,
            COALESCE(pa.batch_no, '') AS batch_no,
            pa.expiry_date::text AS expiry_date,
            COALESCE(pa.eye_side, '') AS eye_side,
            pa.acknowledged_at::text AS acknowledged_at
        FROM purchase_acknowledgements pa
        WHERE {" AND ".join(w)}
        ORDER BY pa.acknowledged_at DESC NULLS LAST, pa.document_date DESC NULLS LAST
        LIMIT 300
    """, p)


def _pa_audit_transition(pa_id, to_status, action, remarks=""):
    actor = "System"
    try:
        user = st.session_state.get("user") or {}
        actor = str(user.get("name") or user.get("username") or st.session_state.get("user_name") or "System")
    except Exception:
        actor = "System"
    return _rw("""
        WITH old AS (
            SELECT id, audit_status, order_line_id, invoice_no, supplier_name
            FROM purchase_acknowledgements
            WHERE id = %(pa)s::uuid
        ), upd AS (
            UPDATE purchase_acknowledgements pa
               SET audit_status = %(to_status)s,
                   audit_remarks = NULLIF(%(remarks)s,''),
                   audited_by = %(actor)s,
                   audited_at = NOW()
              FROM old
             WHERE pa.id = old.id
             RETURNING pa.id, old.audit_status AS from_status,
                       pa.audit_status AS to_status, old.order_line_id,
                       old.invoice_no, old.supplier_name
        )
        INSERT INTO procurement_pa_audit_log (
            pa_id, action, from_status, to_status, order_line_id,
            invoice_no, supplier_name, remarks, performed_by, performed_at
        )
        SELECT id, %(action)s, from_status, to_status, order_line_id,
               invoice_no, supplier_name, NULLIF(%(remarks)s,''), %(actor)s, NOW()
        FROM upd
    """, {
        "pa": pa_id,
        "to_status": to_status,
        "action": action,
        "remarks": remarks or "",
        "actor": actor,
    })


def _pa_post_inventory_from_register(pa_id):
    """Post a PA into inventory once, then mark it closed for register audit.

    Linked order PA:
      - create a stock row
      - allot it to the order line until dispatch
      - if already dispatched, immediately net it out with a DISPATCH ledger row

    Orphan PA:
      - create a generic inventory row for owner audit/stock holding
    """
    linked_ok = _rw("""
        WITH pa AS (
            SELECT
                pa.*,
                ol.id AS linked_order_line_id,
                ol.product_id AS ol_product_id,
                ol.lens_params,
                COALESCE(ol.dispatched_qty, 0)::numeric AS already_dispatched_qty
            FROM purchase_acknowledgements pa
            JOIN order_lines ol ON ol.id = pa.order_line_id
            WHERE pa.id = %(pa)s::uuid
              AND pa.order_line_id IS NOT NULL
              AND pa.inventory_posted_at IS NULL
              AND COALESCE(pa.our_product_id, pa.product_id, ol.product_id) IS NOT NULL
        ), ins AS (
            INSERT INTO inventory_stock (
                product_id,
                batch_no, expiry_date, quantity, allocated_qty,
                purchase_rate, purchase_price, selling_price,
                stock_type, item_type, is_active,
                supplier_id, supplier_name,
                created_at, updated_at
            )
            SELECT
                COALESCE(our_product_id, product_id, ol_product_id),
                COALESCE(NULLIF(batch_no,''), COALESCE(NULLIF(invoice_no,''), 'PA') || '-' || LEFT(id::text, 8)),
                expiry_date,
                GREATEST(0, COALESCE(received_qty, qty, 1)::numeric - already_dispatched_qty),
                GREATEST(0, COALESCE(received_qty, qty, 1)::numeric - already_dispatched_qty),
                COALESCE(purchase_price, 0),
                COALESCE(purchase_price, 0),
                COALESCE(purchase_price, 0),
                'BATCH', 'STOCK',
                GREATEST(0, COALESCE(received_qty, qty, 1)::numeric - already_dispatched_qty) > 0,
                supplier_id, supplier_name,
                NOW(), NOW()
            FROM pa
            RETURNING id, product_id, batch_no
        ), purchase_ledger AS (
            INSERT INTO inventory_stock_ledger (
                inventory_stock_id, product_id, batch_no, qty_change,
                ref_type, ref_id, ref_no, remarks, created_at, created_by
            )
            SELECT
                ins.id, ins.product_id, ins.batch_no,
                COALESCE(pa.received_qty, pa.qty, 1)::numeric,
                'PURCHASE', pa.id, pa.invoice_no,
                'Purchase register posting for linked procurement',
                NOW(), 'purchase_register'
            FROM ins, pa
            WHERE COALESCE(pa.received_qty, pa.qty, 1)::numeric > 0
            RETURNING 1
        ), dispatch_ledger AS (
            INSERT INTO inventory_stock_ledger (
                inventory_stock_id, product_id, batch_no, qty_change,
                ref_type, ref_id, ref_no, remarks, created_at, created_by
            )
            SELECT
                ins.id, ins.product_id, ins.batch_no,
                -pa.already_dispatched_qty,
                'DISPATCH', pa.id, pa.invoice_no,
                'Auto net-out: order was dispatched before purchase register posting',
                NOW(), 'purchase_register'
            FROM ins, pa
            WHERE pa.already_dispatched_qty > 0
            RETURNING 1
        ), mark AS (
            UPDATE purchase_acknowledgements
               SET inventory_posted_at = NOW()
             WHERE id = %(pa)s::uuid
               AND EXISTS (SELECT 1 FROM ins)
            RETURNING id
        )
        UPDATE order_lines ol
           SET lens_params = jsonb_set(
                   jsonb_set(
                       COALESCE(ol.lens_params, '{}'::jsonb),
                       '{stock_id}', to_jsonb(ins.id::text), TRUE
                   ),
                   '{batch_allocation}',
                   jsonb_build_array(jsonb_build_object(
                       'stock_id', ins.id::text,
                       'batch_id', ins.id::text,
                       'batch_no', ins.batch_no,
                       'allocated_qty', GREATEST(0, COALESCE(pa.received_qty, pa.qty, 1)::numeric - pa.already_dispatched_qty)
                   )),
                   TRUE
               )
        FROM pa, ins
        WHERE ol.id = pa.linked_order_line_id
          AND EXISTS (SELECT 1 FROM mark)
    """, {"pa": pa_id})
    if linked_ok:
        return True

    return _rw("""
        WITH pa AS (
            SELECT *
            FROM purchase_acknowledgements
            WHERE id = %(pa)s::uuid
              AND order_line_id IS NULL
              AND inventory_posted_at IS NULL
              AND COALESCE(our_product_id, product_id) IS NOT NULL
        ), ins AS (
            INSERT INTO inventory_stock (
                product_id,
                batch_no, expiry_date, quantity,
                purchase_rate, purchase_price, selling_price,
                stock_type, item_type, is_active,
                supplier_id, supplier_name,
                created_at, updated_at
            )
            SELECT
                COALESCE(our_product_id, product_id),
                COALESCE(NULLIF(batch_no,''), 'PA-' || LEFT(id::text, 8)),
                expiry_date,
                GREATEST(1, COALESCE(received_qty, qty, 1)::int),
                COALESCE(purchase_price, 0),
                COALESCE(purchase_price, 0),
                COALESCE(purchase_price, 0),
                'BATCH', 'STOCK', TRUE,
                supplier_id, supplier_name,
                NOW(), NOW()
            FROM pa
            RETURNING 1
        )
        UPDATE purchase_acknowledgements
           SET inventory_posted_at = NOW()
         WHERE id = %(pa)s::uuid
           AND EXISTS (SELECT 1 FROM ins)
    """, {"pa": pa_id})


def _save_audit_pa_details(pa_id, supplier_ref, remarks=""):
    """Save editable fields for an invoice-match line before inventory posting."""
    kb = str(pa_id).replace("-", "")[-10:]
    qty = float(st.session_state.get(f"pa_audit_qty_{kb}", 1) or 1)
    price = float(st.session_state.get(f"pa_audit_price_{kb}", 0) or 0)
    gst_pct = float(st.session_state.get(f"pa_audit_gst_{kb}", 0) or 0)
    doc_no = (st.session_state.get(f"pa_audit_doc_{kb}", "") or "").strip() or None
    doc_date = st.session_state.get(f"pa_audit_date_{kb}", datetime.date.today())
    batch_no = (st.session_state.get(f"pa_audit_batch_{kb}", "") or "").strip() or None
    expiry = st.session_state.get(f"pa_audit_exp_{kb}", None)
    try:
        from modules.core.date_guard import validate_not_future
        ok_dt, msg_dt = validate_not_future(doc_date, "Purchase document date")
        if not ok_dt:
            st.error(msg_dt)
            return False
    except Exception:
        pass
    total = round(qty * price, 2)
    gst_amount = round(total * gst_pct / 100, 2)
    ok = _rw(
        """
        UPDATE purchase_acknowledgements
           SET received_qty = %(qty)s,
               qty = %(qty)s,
               purchase_price = %(price)s,
               total_value = %(total)s,
               document_date = %(doc_date)s::date,
               invoice_no = CASE
                   WHEN COALESCE(invoice_no,'') = '' THEN %(doc)s
                   ELSE invoice_no
               END,
               challan_no = CASE
                   WHEN COALESCE(invoice_no,'') = '' AND COALESCE(challan_no,'') = '' THEN %(doc)s
                   ELSE challan_no
               END,
               batch_no = COALESCE(NULLIF(%(batch)s,''), batch_no),
               expiry_date = %(expiry)s::date,
               courier_gst_rate = %(gst_pct)s,
               courier_gst_amount = %(gst_amt)s,
               supplier_order_ref = COALESCE(NULLIF(%(sref)s,''), supplier_order_ref),
               audit_remarks = NULLIF(%(remarks)s,''),
               acknowledged_at = NOW()
         WHERE id = %(pa)s::uuid
        """,
        {
            "pa": pa_id,
            "qty": qty,
            "price": price,
            "total": total,
            "doc_date": str(doc_date),
            "doc": doc_no,
            "batch": batch_no or "",
            "expiry": str(expiry) if expiry else None,
            "gst_pct": gst_pct,
            "gst_amt": gst_amount,
            "sref": supplier_ref or "",
            "remarks": remarks or "",
        },
    )
    if ok:
        _rw(
            """
            UPDATE inventory_stock ist
               SET quantity = %(qty)s,
                   purchase_rate = %(price)s,
                   purchase_price = %(price)s,
                   selling_price = %(price)s,
                   expiry_date = %(expiry)s::date,
                   updated_at = NOW()
              FROM purchase_acknowledgements pa
             WHERE pa.id = %(pa)s::uuid
               AND pa.inventory_posted_at IS NOT NULL
               AND ist.product_id = COALESCE(pa.our_product_id, pa.product_id)
               AND ist.batch_no = COALESCE(NULLIF(%(batch)s,''), pa.batch_no)
            """,
            {
                "pa": pa_id,
                "qty": int(qty),
                "price": price,
                "batch": batch_no or "",
                "expiry": str(expiry) if expiry else None,
            },
        )
    return ok


def _candidate_order_lines_for_pa(pa):
    """Owner-audit helper: find unprocured order lines matching PA product + power text."""
    try:
        from modules.procurement.supplier_invoice_rules import parse_bonzer_description
    except Exception:
        parse_bonzer_description = None
    desc = str(pa.get("supplier_product_description") or pa.get("supplier_product_name") or "")
    parsed = parse_bonzer_description(desc) if parse_bonzer_description and desc else {}
    powers = []
    for eye_key, eye in (("right", "R"), ("left", "L")):
        pwr = parsed.get(eye_key) or {}
        if isinstance(pwr, dict) and pwr:
            powers.append((eye, pwr))
    product_name = str(pa.get("product_name") or "")
    supplier_name = str(pa.get("supplier_name") or "")
    params = {
        "pname": product_name,
        "supplier": supplier_name,
    }
    base = [
        "COALESCE(ol.is_deleted,FALSE)=FALSE",
        "COALESCE(ol.is_service_line,FALSE)=FALSE",
        "NOT EXISTS (SELECT 1 FROM purchase_acknowledgements pa2 "
        "WHERE pa2.order_line_id=ol.id AND pa2.billing_status NOT IN ('VOID','CANCELLED'))",
    ]
    if product_name:
        base.append(
            "(LOWER(p.product_name)=LOWER(%(pname)s) "
            "OR LOWER(COALESCE(ol.lens_params->>'supplier_product_name','')) "
            "LIKE LOWER(CONCAT('%%', %(pname)s, '%%')))"
        )
    if supplier_name:
        base.append(
            "(LOWER(COALESCE(ol.lens_params->>'supplier_name','')) "
            "LIKE LOWER(CONCAT('%%', %(supplier)s, '%%')) OR TRUE)"
        )
    candidates = []
    if powers:
        for idx, (eye, pwr) in enumerate(powers):
            conds = list(base)
            conds.append(f"UPPER(COALESCE(ol.eye_side,'')) = %(eye_{idx})s")
            p = dict(params)
            p[f"eye_{idx}"] = eye
            for col, key in [("sph","sph"),("cyl","cyl"),("axis","axis"),("add_power","add")]:
                val = pwr.get(key)
                if val is None:
                    continue
                if col == "axis":
                    if int(float(val)) > 0:
                        conds.append(f"COALESCE(ol.{col},0) = %(axis_{idx})s")
                        p[f"axis_{idx}"] = int(float(val))
                else:
                    conds.append(f"ABS(COALESCE(ol.{col},0)-%(v_{idx}_{col})s)<0.03")
                    p[f"v_{idx}_{col}"] = float(val)
            candidates.extend(_q(f"""
                SELECT ol.id::text AS line_id, o.order_no, ol.eye_side,
                       p.product_name, ol.sph, ol.cyl, ol.axis, ol.add_power
                FROM order_lines ol
                JOIN orders o ON o.id=ol.order_id
                LEFT JOIN products p ON p.id=ol.product_id
                WHERE {" AND ".join(conds)}
                ORDER BY o.created_at DESC
                LIMIT 5
            """, p))
    if not candidates and product_name:
        candidates = _q(f"""
            SELECT ol.id::text AS line_id, o.order_no, ol.eye_side,
                   p.product_name, ol.sph, ol.cyl, ol.axis, ol.add_power
            FROM order_lines ol
            JOIN orders o ON o.id=ol.order_id
            LEFT JOIN products p ON p.id=ol.product_id
            WHERE {" AND ".join(base)}
            ORDER BY o.created_at DESC
            LIMIT 5
        """, params)
    seen = set()
    unique = []
    for c in candidates:
        if c.get("line_id") in seen:
            continue
        seen.add(c.get("line_id"))
        unique.append(c)
    return unique


def _link_pa_to_order_line(pa_id, line_id, supplier_order_ref="", remarks=""):
    actor = "System"
    try:
        user = st.session_state.get("user") or {}
        actor = str(user.get("name") or user.get("username") or st.session_state.get("user_name") or "System")
    except Exception:
        actor = "System"
    return _rw("""
        WITH target AS (
            SELECT ol.id AS line_id, o.order_no, ol.eye_side
            FROM order_lines ol
            JOIN orders o ON o.id=ol.order_id
            WHERE ol.id=%(lid)s::uuid
        ), old AS (
            SELECT id, audit_status, invoice_no, supplier_name
            FROM purchase_acknowledgements
            WHERE id=%(pa)s::uuid
        ), upd AS (
            UPDATE purchase_acknowledgements pa
               SET order_line_id = target.line_id,
                   order_no = target.order_no,
                   eye_side = COALESCE(NULLIF(pa.eye_side,''), target.eye_side),
                   supplier_order_ref = COALESCE(NULLIF(%(sref)s,''), pa.supplier_order_ref),
                   audit_status = 'LINKED_PROCUREMENT',
                   audit_remarks = NULLIF(%(remarks)s,''),
                   audited_by = %(actor)s,
                   audited_at = NOW()
              FROM target, old
             WHERE pa.id = old.id
             RETURNING pa.id, old.audit_status AS from_status, pa.audit_status AS to_status,
                       target.line_id, pa.invoice_no, pa.supplier_name, target.order_no
        ), hist AS (
            INSERT INTO procurement_pa_audit_log (
                pa_id, action, from_status, to_status, order_line_id,
                invoice_no, supplier_name, remarks, performed_by, performed_at
            )
            SELECT id, 'LINKED_TO_ORDER', from_status, to_status, line_id,
                   invoice_no, supplier_name,
                   'Linked to ' || order_no || COALESCE(' · ' || NULLIF(%(remarks)s,''), ''),
                   %(actor)s, NOW()
            FROM upd
        )
        UPDATE order_lines ol
           SET lens_params = jsonb_set(
               jsonb_set(
                 jsonb_set(COALESCE(ol.lens_params,'{}'::jsonb),
                           '{replenishment_status}', '"PROCURED"'),
                 '{supplier_stage}', '"READY_FOR_BILLING"'),
               '{procurement_status}', '"PROCURED"')
          FROM target
         WHERE ol.id = target.line_id
    """, {
        "pa": pa_id,
        "lid": line_id,
        "sref": supplier_order_ref or "",
        "remarks": remarks or "",
        "actor": actor,
    })


def _load_pos(sup, ref, dfrom, dto):
    w = []
    p = {"df": str(dfrom), "dt": str(dto)}
    if not ref.strip():
        w.extend(["DATE(so.created_at) >= %(df)s", "DATE(so.created_at) <= %(dt)s"])
    if sup.strip():
        w.append("LOWER(COALESCE(so.supplier_name,'')) LIKE %(sup)s")
        p["sup"] = f"%{sup.strip().lower()}%"
    if ref.strip():
        w.append("(LOWER(COALESCE(so.supplier_order_id,'')) LIKE %(ref)s"
                 " OR LOWER(COALESCE(so.customer_order_id,'')) LIKE %(ref)s)")
        p["ref"] = f"%{ref.strip().lower()}%"

    rows = _q(f"""
        SELECT
            so.id                AS po_id,
            COALESCE(so.supplier_order_id, 'PO-' || so.id::text) AS po_no,
            so.supplier_name,
            so.supplier_id       AS supplier_id,
            so.customer_order_id AS order_ref,
            so.order_date::text      AS doc_date,
            so.expected_delivery_date::text AS exp_date,
            so.status,
            COALESCE(so.total_value,0)   AS total_value,
            COALESCE(so.total_items,0)   AS total_items,
            COALESCE(so.total_qty,0)     AS total_qty,
            COALESCE(so.special_instructions,'') AS notes,
            so.created_at::text      AS created_at
        FROM supplier_orders so
        WHERE {" AND ".join(w)}
        ORDER BY so.created_at DESC
        LIMIT 200
    """, p)

    # Attach items to each PO
    for r in rows:
        r["_items"] = _q("""
            SELECT product_name, eye_side, sph, cyl, axis, add_power,
                   COALESCE(ordered_qty,0) AS ordered_qty,
                   COALESCE(received_qty,0) AS received_qty,
                   COALESCE(unit_price,0) AS unit_price,
                   item_status
            FROM supplier_order_items
            WHERE CAST(supplier_order_id AS TEXT) = %(id_str)s
            ORDER BY item_no
        """, {"id_str": str(r["po_id"])})
    return rows


def _load_grns(sup, ref, dfrom, dto):
    # Check if table exists first
    exists = _q("SELECT 1 FROM information_schema.tables WHERE table_name='purchase_invoices' LIMIT 1")
    if not exists:
        return []
    w = []
    p = {"df": str(dfrom), "dt": str(dto)}
    if not ref.strip():
        w.append(
            "(DATE(pi.invoice_date) BETWEEN %(df)s AND %(dt)s "
            "OR DATE(pi.created_at) BETWEEN %(df)s AND %(dt)s)"
        )
    if sup.strip():
        w.append("LOWER(COALESCE(pi.supplier_name,'')) LIKE %(sup)s")
        p["sup"] = f"%{sup.strip().lower()}%"
    if ref.strip():
        w.append("(LOWER(COALESCE(pi.invoice_no,'')) LIKE %(ref)s"
                 " OR LOWER(COALESCE(pi.supplier_invoice_no,'')) LIKE %(ref)s)")
        p["ref"] = f"%{ref.strip().lower()}%"
    return _q(f"""
        SELECT
            pi.invoice_no,
            pi.supplier_name,
            pi.supplier_id::text          AS supplier_id,
            COALESCE(pi.supplier_order_id,'') AS po_ref,
            COALESCE(pi.supplier_invoice_no,'') AS supplier_inv,
            pi.invoice_date::text         AS doc_date,
            pi.created_at::date::text     AS posted_date,
            COALESCE(pi.total_items,0)    AS total_items,
            COALESCE(pi.total_qty_received,0) AS qty,
            COALESCE(pi.subtotal,0)       AS subtotal,
            COALESCE(pi.gst_amount,0)     AS gst_amount,
            COALESCE(pi.courier_amount,0) AS courier_amount,
            COALESCE(pi.courier_gst,0)    AS courier_gst,
            COALESCE(pi.gst_adjustment_amount,0) AS gst_adjustment_amount,
            COALESCE(pi.round_off_amount,0) AS round_off_amount,
            COALESCE(pi.adjustment_notes,'') AS adjustment_notes,
            COALESCE(pi.invoice_total,0)  AS total_value,
            COALESCE(pi.payment_status,'UNPAID') AS payment_status
        FROM purchase_invoices pi
        WHERE {" AND ".join(w)}
        ORDER BY COALESCE(pi.created_at::date, pi.invoice_date) DESC, pi.invoice_date DESC
        LIMIT 200
    """, p)


# ── PA editable card ────────────────────────────────────────────────────────────

def _render_pa_card(r):
    _pid    = r["pa_id"]
    _kb     = f"pa_{str(_pid).replace('-','')[-10:]}"
    _locked = bool(r.get("is_price_locked"))
    _price  = float(r.get("purchase_price") or 0)
    _qty    = int(r.get("qty") or 1)
    _pname  = (r.get("product_name") or "—")[:35]
    _eye    = str(r.get("eye_side","")).upper()
    _pw     = _fmt_pwr(r)
    _ono    = r.get("order_no","—")
    _chal   = r.get("challan_no","") or ""
    _inv    = r.get("invoice_no","") or ""
    _sup    = r.get("supplier_name","—")
    _trans  = r.get("transport","") or ""
    _lr     = r.get("lr_no","") or ""
    _unit   = str(r.get("unit","PCS")).upper()
    _bsize  = int(r.get("box_size") or 1)
    _total  = float(r.get("total_value") or 0)
    # supplier / our product identity (migration 0009)
    _sup_pn   = (r.get("supplier_product_name") or "").strip()
    _our_pn   = (r.get("our_product_name") or "").strip()
    _map_src  = (r.get("mapping_source") or "").strip()
    _in_reg   = bool(r.get("in_register"))
    _reg_pstat = str(r.get("register_payment_status","")).upper()

    # Qty label
    if _unit == "BOX" and _bsize > 1:
        _nb = _qty // _bsize
        _np = _qty % _bsize
        _qty_lbl = f"{_nb} Box ({_qty} pcs)" + (f" +{_np}" if _np else "")
    else:
        _qty_lbl = f"{_qty} pcs"

    # Status — three layers: doc ref · invoice · register
    if _inv:
        _sc, _st = "#22c55e", f"🧾 {_inv}"
        if _in_reg:
            _reg_badge = (
                f" &nbsp;<span style='background:#14532d;color:#86efac;"
                f"font-size:0.65rem;padding:1px 7px;border-radius:8px;"
                f"font-weight:700'>✅ Purchase Register Done"
                + (f" · {_reg_pstat}" if _reg_pstat else "")
                + "</span>"
            )
        else:
            _reg_badge = (
                " &nbsp;<span style='background:#451a03;color:#fcd34d;"
                "font-size:0.65rem;padding:1px 7px;border-radius:8px;"
                "font-weight:700'>⚠️ Not in Register</span>"
            )
    elif _chal:
        _sc, _st = "#3b82f6", f"📋 {_chal}"
        _reg_badge = (
            " &nbsp;<span style='background:#1e3a5f;color:#93c5fd;"
            "font-size:0.65rem;padding:1px 7px;border-radius:8px;"
            "font-weight:700'>📋 Challan only</span>"
        )
    else:
        _sc, _st = "#ef4444", "⚠️ No doc"
        _reg_badge = (
            " &nbsp;<span style='background:#450a0a;color:#fca5a5;"
            "font-size:0.65rem;padding:1px 7px;border-radius:8px;"
            "font-weight:700'>⚠️ Pending</span>"
        )

    # Header
    st.markdown(
        f"<div style='background:#080f1a;border:1px solid #1e293b;"
        f"border-left:4px solid {_sc};border-radius:6px;padding:6px 12px;margin:2px 0'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:4px'>"
        f"<span style='color:#f1f5f9;font-weight:700'>{_ono} &nbsp; {_eye} &nbsp; {_pname}</span>"
        f"<span style='display:flex;align-items:center;gap:4px'>"
        f"<span style='color:{_sc};font-size:0.72rem;font-weight:700'>{_st}</span>"
        f"{_reg_badge}"
        f"</span>"
        f"</div>"
        f"<div style='color:#475569;font-size:0.7rem;margin-top:2px'>"
        f"{_pw}  ·  {_qty_lbl}  ·  &#8377;{_price:,.2f}/pc  ·  Total &#8377;{_total:,.0f}"
        + (f"  ·  {_sup}" if _sup != "—" else "")
        + (f"  ·  🚚 {_trans}" if _trans else "")
        + (f"  ·  LR: {_lr}" if _lr else "")
        + "</div>"
        + (
            f"<div style='color:#a5b4fc;font-size:0.72rem;margin-top:3px'>"
            f"🏭 Supplier item: <b>{_sup_pn}</b>"
            + (f" &nbsp;·&nbsp; alias (ours): {_our_pn}" if _our_pn else "")
            + (f" &nbsp;·&nbsp; <span style='color:#475569'>{_map_src}</span>" if _map_src else "")
            + "</div>"
            if (_sup_pn or _our_pn) else
            "<div style='color:#64748b;font-size:0.7rem;margin-top:3px'>"
            "🏭 No supplier product mapped on this purchase</div>"
        )
        + "</div>",
        unsafe_allow_html=True
    )

    with st.expander("✏️ Edit", expanded=False):
        # Initialise session state defaults ONCE (avoids value= conflict)
        if f"pr_p_{_kb}"  not in st.session_state: st.session_state[f"pr_p_{_kb}"]  = _price
        if f"pr_c_{_kb}"  not in st.session_state: st.session_state[f"pr_c_{_kb}"]  = _chal
        if f"pr_i_{_kb}"  not in st.session_state: st.session_state[f"pr_i_{_kb}"]  = _inv
        if f"pr_t_{_kb}"  not in st.session_state: st.session_state[f"pr_t_{_kb}"]  = _trans
        if f"pr_lr_{_kb}" not in st.session_state: st.session_state[f"pr_lr_{_kb}"] = _lr
        if f"pr_spi_{_kb}" not in st.session_state: st.session_state[f"pr_spi_{_kb}"] = _sup_pn
        if f"pr_our_{_kb}" not in st.session_state: st.session_state[f"pr_our_{_kb}"] = _our_pn or _pname
        _suppliers = _load_suppliers_for_purchase_register()
        _sup_ids = [""] + [str(s.get("id") or "") for s in _suppliers]
        _sup_names = {"": "— Select Supplier / Creditor —"}
        _sup_names.update({str(s.get("id") or ""): str(s.get("party_name") or "") for s in _suppliers})
        _current_sid = str(r.get("supplier_id") or "")
        if not _current_sid and _sup and _sup != "—":
            _current_sid = next(
                (
                    str(s.get("id") or "")
                    for s in _suppliers
                    if str(s.get("party_name") or "").strip().lower() == str(_sup).strip().lower()
                ),
                "",
            )
        if f"pr_sid_{_kb}" not in st.session_state:
            st.session_state[f"pr_sid_{_kb}"] = _current_sid if _current_sid in _sup_ids else ""
        _doc_dt = r.get("doc_date","") or ""
        if f"pr_d_{_kb}" not in st.session_state:
            try:
                st.session_state[f"pr_d_{_kb}"] = datetime.date.fromisoformat(_doc_dt[:10]) if _doc_dt else datetime.date.today()
            except Exception:
                st.session_state[f"pr_d_{_kb}"] = datetime.date.today()

        st.markdown(
            "<div style='color:#a5b4fc;font-size:0.72rem;font-weight:700;"
            "margin:2px 0 4px'>Supplier Invoice Product Mapping</div>",
            unsafe_allow_html=True,
        )
        _map_c1, _map_c2 = st.columns(2)
        _sel_supplier_id = st.selectbox(
            "Supplier / Creditor",
            _sup_ids,
            key=f"pr_sid_{_kb}",
            format_func=lambda x: _sup_names.get(x, x),
            help="Can be corrected here before posting or while editing an un-paid purchase invoice.",
        )
        _sel_supplier_name = _sup_names.get(_sel_supplier_id, _sup)
        _map_c1.text_input(
            "Supplier item on invoice",
            key=f"pr_spi_{_kb}",
            placeholder="Name printed by supplier",
        )
        _map_c2.text_input(
            "Our alias/product",
            key=f"pr_our_{_kb}",
            placeholder="Our product name",
        )
        if _eye in ("R", "L"):
            _sib_eye = "L" if _eye == "R" else "R"
            if st.button(
                f"↔ Use these details for {_sib_eye}",
                key=f"pr_copy_sib_{_kb}",
                help="Copy supplier item, alias, challan/invoice/date/transport/LR and price to the opposite eye on the same order/challan.",
                use_container_width=True,
            ):
                _copy_spi = (st.session_state.get(f"pr_spi_{_kb}", "") or "").strip() or None
                _copy_our = (st.session_state.get(f"pr_our_{_kb}", "") or "").strip() or None
                _copy_p   = float(st.session_state.get(f"pr_p_{_kb}", _price) or 0)
                _copy_c   = (st.session_state.get(f"pr_c_{_kb}", "") or "").strip() or None
                _copy_i   = (st.session_state.get(f"pr_i_{_kb}", "") or "").strip() or None
                _copy_d   = st.session_state.get(f"pr_d_{_kb}", datetime.date.today())
                _copy_t   = (st.session_state.get(f"pr_t_{_kb}", "") or "").strip() or None
                _copy_lr  = (st.session_state.get(f"pr_lr_{_kb}", "") or "").strip() or None
                _copy_sid = (st.session_state.get(f"pr_sid_{_kb}", "") or "").strip() or None
                _copy_snm = _sup_names.get(_copy_sid, _sup) if _copy_sid else _sup
                _copy_bs  = "INVOICED" if _copy_i else "PURCHASE_ACKED"
                from modules.core.date_guard import validate_not_future
                _ok_dt, _msg_dt = validate_not_future(_copy_d, "Purchase document date")
                if not _ok_dt:
                    st.error(_msg_dt)
                    st.stop()
                _ok_copy = _rw("""
                    UPDATE purchase_acknowledgements
                    SET purchase_price = CASE WHEN COALESCE(is_price_locked,FALSE) THEN purchase_price ELSE %(p)s END,
                        total_value = CASE WHEN COALESCE(is_price_locked,FALSE) THEN total_value ELSE %(p)s * COALESCE(qty, received_qty, 1) END,
                        supplier_id = COALESCE(NULLIF(%(sid)s,'')::uuid, supplier_id),
                        supplier_name = COALESCE(NULLIF(%(snm)s,''), supplier_name),
                        challan_no = %(cn)s,
                        invoice_no = %(iv)s,
                        document_date = %(dd)s::date,
                        transport = %(tr)s,
                        lr_no = %(lr)s,
                        supplier_product_name = COALESCE(%(spi)s, supplier_product_name),
                        supplier_product_description = COALESCE(%(spi)s, supplier_product_description),
                        our_product_name = COALESCE(%(our)s, our_product_name),
                        mapping_source = CASE WHEN %(spi)s IS NOT NULL THEN 'copy_from_sibling' ELSE mapping_source END,
                        billing_status = %(bs)s,
                        acknowledged_at = NOW()
                    WHERE order_no = %(ono)s
                      AND UPPER(COALESCE(eye_side,'')) = %(eye)s
                      AND id <> %(id)s::uuid
                      AND COALESCE(billing_status,'') NOT IN ('VOID','CANCELLED')
                """, {
                    "p": _copy_p, "cn": _copy_c, "iv": _copy_i, "dd": str(_copy_d),
                    "tr": _copy_t, "lr": _copy_lr, "spi": _copy_spi, "our": _copy_our,
                    "sid": _copy_sid or "", "snm": _copy_snm or "",
                    "bs": _copy_bs, "ono": _ono, "eye": _sib_eye, "id": _pid,
                })
                if _ok_copy:
                    st.success(f"Copied details to {_sib_eye} eye.")
                    st.rerun()
                else:
                    st.warning(f"No {_sib_eye} sibling PA found to update.")

        e1, e2, e3, e4 = st.columns(4)
        with e1:
            st.number_input("Purchase Price ₹/pc", min_value=0.0,
                            step=0.5, format="%.2f",
                            key=f"pr_p_{_kb}", disabled=_locked)
            if _locked: st.caption("🔒 Price locked")
        with e2:
            st.text_input("Challan No.", key=f"pr_c_{_kb}", placeholder="CH-001")
            st.text_input("Invoice No.", key=f"pr_i_{_kb}", placeholder="INV/001")
        with e3:
            st.date_input("Document Date", key=f"pr_d_{_kb}", format="DD/MM/YYYY")
            st.text_input("Transport", key=f"pr_t_{_kb}", placeholder="DTDC")
        with e4:
            st.text_input("LR / AWB", key=f"pr_lr_{_kb}", placeholder="LR-12345")
            st.markdown("&nbsp;")
            if st.button("💾 Save Changes", key=f"pr_sv_{_kb}",
                         type="primary", use_container_width=True):
                _new_p  = st.session_state.get(f"pr_p_{_kb}", _price)
                _new_c  = (st.session_state.get(f"pr_c_{_kb}","") or "").strip() or None
                _new_i  = (st.session_state.get(f"pr_i_{_kb}","") or "").strip() or None
                _new_d  = st.session_state.get(f"pr_d_{_kb}", datetime.date.today())
                _new_t  = (st.session_state.get(f"pr_t_{_kb}","") or "").strip() or None
                _new_lr = (st.session_state.get(f"pr_lr_{_kb}","") or "").strip() or None
                _new_spi = (st.session_state.get(f"pr_spi_{_kb}","") or "").strip() or None
                _new_our = (st.session_state.get(f"pr_our_{_kb}","") or "").strip() or None
                _new_sid = (st.session_state.get(f"pr_sid_{_kb}","") or "").strip() or None
                _new_sup = _sup_names.get(_new_sid or "", _sup) if _new_sid else _sup

                # Derive billing_status from whether an invoice number is now set
                _new_bstat = "INVOICED" if _new_i else (
                    "PURCHASE_ACKED" if not _new_c else "PURCHASE_ACKED"
                )

                from modules.core.date_guard import validate_not_future
                _ok_dt, _msg_dt = validate_not_future(_new_d, "Purchase document date")
                if not _ok_dt:
                    st.error(_msg_dt)
                    return

                try:
                    from modules.sql_adapter import get_transaction_connection
                    _sv_conn = get_transaction_connection()
                    try:
                        with _sv_conn.cursor() as _sc:

                            # 1. Update the PA row (including billing_status)
                            _sc.execute("""
                                UPDATE purchase_acknowledgements SET
                                    purchase_price  = CASE WHEN %(lk)s THEN purchase_price ELSE %(p)s END,
                                    total_value     = CASE WHEN %(lk)s THEN total_value    ELSE %(tv)s END,
                                    supplier_id     = COALESCE(NULLIF(%(sid)s,'')::uuid, supplier_id),
                                    supplier_name   = COALESCE(NULLIF(%(snm)s,''), supplier_name),
                                    challan_no      = %(cn)s,
                                    invoice_no      = %(iv)s,
                                    document_date   = %(dd)s::date,
                                    transport       = %(tr)s,
                                    lr_no           = %(lr)s,
                                    supplier_product_name = COALESCE(%(spi)s, supplier_product_name),
                                    supplier_product_description = COALESCE(%(spi)s, supplier_product_description),
                                    our_product_name = COALESCE(%(our)s, our_product_name),
                                    mapping_source = CASE
                                        WHEN %(spi)s IS NOT NULL THEN 'manual_register_edit'
                                        ELSE mapping_source
                                    END,
                                    billing_status  = %(bs)s,
                                    acknowledged_at = NOW()
                                WHERE id = %(id)s::uuid
                            """, {
                                "lk": _locked, "p": _new_p,
                                "tv": round(_new_p * _qty, 2),
                                "cn": _new_c, "iv": _new_i,
                                "dd": str(_new_d),
                                "tr": _new_t, "lr": _new_lr,
                                "sid": _new_sid or "", "snm": _new_sup or "",
                                "spi": _new_spi,
                                "our": _new_our,
                                "bs": _new_bstat,
                                "id": _pid,
                            })

                            # 2. If an invoice number was entered, upsert a
                            #    purchase_invoices header so it appears in
                            #    Registers → Purchase Register with the correct date.
                            if _new_i:
                                _gst_pct = float(r.get("gst_percent") or 0)
                                _line_sub = round(_new_p * _qty, 2)
                                _line_gst = round(_line_sub * _gst_pct / 100, 2)
                                _line_tot = round(_line_sub + _line_gst, 2)
                                _sc.execute("""
                                    INSERT INTO purchase_invoices (
                                        invoice_no, supplier_order_id,
                                        supplier_id, supplier_name,
                                        supplier_invoice_no,
                                        invoice_date,
                                        total_items, total_qty_received,
                                        subtotal, gst_amount, invoice_total,
                                        payment_terms, payment_status,
                                        notes, created_by, created_at, updated_at
                                    ) VALUES (
                                        %(inv)s, %(soid)s,
                                        %(sid)s, %(sname)s,
                                        %(inv)s,
                                        %(idate)s,
                                        1, %(qty)s,
                                        %(sub)s, %(gst)s, %(tot)s,
                                        'NET30', 'UNPAID',
                                        %(notes)s, 'manual_edit', NOW(), NOW()
                                    )
                                    ON CONFLICT (invoice_no) DO UPDATE SET
                                        -- Accumulate totals if other lines already exist
                                        total_items        = (
                                            SELECT COUNT(*) FROM purchase_invoice_lines
                                            WHERE invoice_no = %(inv)s
                                        ) + 1,
                                        total_qty_received = purchase_invoices.total_qty_received + %(qty)s,
                                        subtotal           = purchase_invoices.subtotal + %(sub)s,
                                        gst_amount         = purchase_invoices.gst_amount + %(gst)s,
                                        invoice_total      = purchase_invoices.invoice_total + %(tot)s,
                                        invoice_date       = CASE
                                            WHEN purchase_invoices.created_by = 'manual_edit'
                                            THEN %(idate)s
                                            ELSE purchase_invoices.invoice_date
                                        END,
                                        updated_at         = NOW()
                                """, {
                                    "inv":   _new_i,
                                    "soid":  _new_c or f"ACK-{_ono}",
                                    "sid":   _new_sid or r.get("supplier_id") or None,
                                    "sname": _new_sup or _sup,
                                    "idate": str(_new_d),
                                    "qty":   _qty,
                                    "sub":   _line_sub,
                                    "gst":   _line_gst,
                                    "tot":   _line_tot,
                                    "notes": f"Manual entry from PA — order {_ono}",
                                })

                            # 3. If the invoice number was CLEARED (had one before,
                            #    now empty) and the old value was a manually-created
                            #    header, subtract this line's contribution.
                            _old_inv = _inv.strip() if _inv else None
                            if _old_inv and not _new_i:
                                _sc.execute("""
                                    UPDATE purchase_invoices SET
                                        total_items        = GREATEST(0, total_items - 1),
                                        total_qty_received = GREATEST(0, total_qty_received - %(qty)s),
                                        subtotal           = GREATEST(0, subtotal - %(sub)s),
                                        gst_amount         = GREATEST(0, gst_amount - %(gst)s),
                                        invoice_total      = GREATEST(0, invoice_total - %(tot)s),
                                        updated_at         = NOW()
                                    WHERE invoice_no = %(inv)s
                                      AND created_by = 'manual_edit'
                                """, {
                                    "inv": _old_inv,
                                    "qty": _qty,
                                    "sub": round(_price * _qty, 2),
                                    "gst": round(_price * _qty * float(r.get("gst_percent") or 0) / 100, 2),
                                    "tot": round(_price * _qty * (1 + float(r.get("gst_percent") or 0) / 100), 2),
                                })

                        _sv_conn.commit()
                        if _new_i:
                            _pa_post_inventory_from_register(_pid)
                        for _k in [f"pr_p_{_kb}", f"pr_c_{_kb}", f"pr_i_{_kb}",
                                    f"pr_d_{_kb}", f"pr_t_{_kb}", f"pr_lr_{_kb}",
                                    f"pr_spi_{_kb}", f"pr_our_{_kb}", f"pr_sid_{_kb}"]:
                            st.session_state.pop(_k, None)
                        st.success("✓ Saved" + (" — Purchase Register Done" if _new_i else ""))
                        st.rerun()

                    except Exception as _sve:
                        _sv_conn.rollback()
                        st.error(f"Save failed (rolled back): {_sve}")
                    finally:
                        _sv_conn.close()

                except Exception as _ce:
                    st.error(f"DB connection failed: {_ce}")

        # ── Re-link to a different order ──────────────────────────────────
        # Used when the wrong order was sent to supplier. After voiding/removing
        # from invoice, change which order this PA row belongs to, then re-post.
        st.markdown(
            "<div style='border-top:1px solid #1e293b;margin-top:10px;padding-top:8px;"
            "color:#f59e0b;font-size:0.72rem;font-weight:700'>🔄 Re-link to a Different Order</div>"
            "<div style='color:#64748b;font-size:0.68rem;margin-bottom:6px'>"
            "Use when the wrong order was sent to the supplier. Enter the correct order number "
            "to re-link this procurement line. Roll back any invoice first.</div>",
            unsafe_allow_html=True,
        )
        _relink_col1, _relink_col2 = st.columns([4, 1])
        _new_ono = _relink_col1.text_input(
            "New Order No.",
            placeholder=f"Current: {_ono}  →  Enter correct order no. e.g. R/2627/0125",
            key=f"pr_relink_{_kb}",
            label_visibility="collapsed",
        )
        if _relink_col2.button("🔄 Re-link", key=f"pr_relink_btn_{_kb}",
                               use_container_width=True) and _new_ono.strip():
            _new_ono_clean = _new_ono.strip()
            if _new_ono_clean == _ono:
                st.warning("That is already the current order number.")
            else:
                # Find the correct order_line for the new order + same eye/sph
                _new_ol = _q("""
                    SELECT ol.id::text AS ol_id
                    FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    WHERE LOWER(o.order_no) = LOWER(%(ono)s)
                      AND COALESCE(LOWER(ol.eye_side),'') = LOWER(%(eye)s)
                      AND ROUND(COALESCE(ol.sph,0)::numeric,2)
                          = ROUND(COALESCE(%(sph)s,0)::numeric,2)
                    LIMIT 1
                """, {"ono": _new_ono_clean, "eye": _eye or "", "sph": r.get("sph")})

                if not _new_ol:
                    # Fallback — just match on order_no if eye+sph doesn't match
                    _new_ol = _q("""
                        SELECT ol.id::text AS ol_id
                        FROM order_lines ol
                        JOIN orders o ON o.id = ol.order_id
                        WHERE LOWER(o.order_no) = LOWER(%(ono)s)
                        LIMIT 1
                    """, {"ono": _new_ono_clean})

                if _new_ol:
                    _new_ol_id = _new_ol[0]["ol_id"]
                    _ok = _rw("""
                        UPDATE purchase_acknowledgements SET
                            order_no      = %(ono)s,
                            order_line_id = %(olid)s::uuid,
                            acknowledged_at = NOW()
                        WHERE id = %(pid)s::uuid
                    """, {"ono": _new_ono_clean, "olid": _new_ol_id, "pid": _pid})
                    if _ok:
                        for _k in [f"pr_p_{_kb}", f"pr_c_{_kb}", f"pr_i_{_kb}",
                                   f"pr_d_{_kb}", f"pr_t_{_kb}", f"pr_lr_{_kb}",
                                   f"pr_relink_{_kb}"]:
                            st.session_state.pop(_k, None)
                        st.success(
                            f"✅ Re-linked to order {_new_ono_clean}. "
                            f"This line now belongs to that order — re-post via the challan picker."
                        )
                        st.rerun()
                else:
                    st.error(
                        f"No order line found for order {_new_ono_clean} "
                        f"matching {_eye} / SPH {r.get('sph','?')}. "
                        f"Check the order number and try again."
                    )

    # ── Rollback button — shown when this PA line is linked to an invoice ────
    # This is the primary rollback entry point. The GRN audit section also has
    # one, but only for formally-posted invoice_lines records. This covers both
    # manually-entered and formally-posted PA rows.
    _is_invoiced = (
        str(r.get("billing_status","")).upper() == "INVOICED"
        or bool(_inv)
    )
    if _is_invoiced:
        _rb_confirm_key = f"pa_rb_confirm_{_kb}"
        if st.session_state.get(_rb_confirm_key):
            st.warning(
                f"Remove **{_pname} ({_eye})** from invoice **{_inv or '?'}**? "
                f"Choose whether it should return to challan/register posting or full procurement queue rework."
            )
            _rb_dest = st.radio(
                "Rollback destination",
                [
                    "Purchase Register picker (keep purchase/challan, change invoice)",
                    "Procurement Queue (full rework, re-enter purchase)",
                ],
                key=f"pa_rb_dest_{_kb}",
            )
            _to_queue = _rb_dest.startswith("Procurement Queue")
            _rbc1, _rbc2 = st.columns(2)
            if _rbc1.button("✅ Confirm Remove", key=f"pa_rb_yes_{_kb}",
                            type="primary", use_container_width=True):
                try:
                    from modules.sql_adapter import get_transaction_connection
                    _rb_conn = get_transaction_connection()
                    try:
                        with _rb_conn.cursor() as _rc:
                            # 1. Clear billing_status and invoice_no on PA row
                            if _to_queue:
                                _rc.execute("""
                                    UPDATE purchase_acknowledgements
                                    SET billing_status = 'CANCELLED',
                                        invoice_no      = NULL,
                                        purchase_price  = 0,
                                        total_value     = 0,
                                        received_qty    = 0,
                                        notes = TRIM(BOTH ' |' FROM (
                                            REGEXP_REPLACE(
                                                COALESCE(notes,''),
                                                'invoice:[^|]+\\|?\\s*', '', 'g'
                                            ) || ' | rollback_to_procurement_queue'
                                        ))
                                    WHERE id = %(pid)s::uuid
                                """, {"pid": _pid})
                                if r.get("order_line_id"):
                                    _rc.execute("""
                                        UPDATE order_lines
                                        SET lens_params =
                                            COALESCE(lens_params, '{}'::jsonb)
                                            || jsonb_build_object(
                                                'replenishment_status', 'ORDERED',
                                                'procurement_status', 'ORDERED',
                                                'purchase_register_rollback_at', NOW()::text
                                            )
                                        WHERE id = %(olid)s::uuid
                                    """, {"olid": r.get("order_line_id")})
                            else:
                                _rc.execute("""
                                    UPDATE purchase_acknowledgements
                                    SET billing_status = 'PURCHASE_ACKED',
                                        invoice_no     = NULL,
                                        document_date  = CASE
                                            WHEN document_date > CURRENT_DATE THEN CURRENT_DATE
                                            ELSE document_date
                                        END,
                                        notes = REGEXP_REPLACE(
                                            COALESCE(notes,''),
                                            'invoice:[^|]+\\|?\\s*', '', 'g'
                                        )
                                    WHERE id = %(pid)s::uuid
                                """, {"pid": _pid})

                            # 2. Delete from purchase_invoice_lines if a formal
                            #    line exists (matches on challan_no or order_no)
                            if _inv:
                                _rc.execute("""
                                    DELETE FROM purchase_invoice_lines
                                    WHERE invoice_no = %(inv)s
                                      AND (
                                          LOWER(COALESCE(supplier_order_id,'')) = LOWER(COALESCE(%(ch)s,''))
                                          OR LOWER(COALESCE(supplier_order_id,'')) LIKE %(ono_pat)s
                                      )
                                      AND COALESCE(LOWER(eye_side),'') = LOWER(%(eye)s)
                                      AND ROUND(COALESCE(sph,0)::numeric,2)
                                          = ROUND(COALESCE(%(sph)s,0)::numeric,2)
                                """, {
                                    "inv":     _inv,
                                    "ch":      _chal or "",
                                    "ono_pat": f"%{_ono}%",
                                    "eye":     _eye or "",
                                    "sph":     r.get("sph"),
                                })

                                # 3. Recalculate the invoice header
                                _rc.execute("""
                                    UPDATE purchase_invoices SET
                                        total_items        = (SELECT COUNT(*) FROM purchase_invoice_lines WHERE invoice_no=%(inv)s),
                                        total_qty_received = (SELECT COALESCE(SUM(received_qty),0) FROM purchase_invoice_lines WHERE invoice_no=%(inv)s),
                                        subtotal           = (SELECT COALESCE(SUM(actual_price*received_qty),0) FROM purchase_invoice_lines WHERE invoice_no=%(inv)s),
                                        gst_amount         = (SELECT COALESCE(SUM(actual_price*received_qty*gst_percent/100),0) FROM purchase_invoice_lines WHERE invoice_no=%(inv)s),
                                        invoice_total      = (
                                            SELECT COALESCE(SUM(line_total),0)
                                            FROM purchase_invoice_lines WHERE invoice_no=%(inv)s
                                        ) + COALESCE(courier_amount,0)
                                          + COALESCE(courier_gst,0)
                                          + COALESCE(gst_adjustment_amount,0)
                                          + COALESCE(round_off_amount,0),
                                        updated_at         = NOW()
                                    WHERE invoice_no = %(inv)s
                                """, {"inv": _inv})

                        _rb_conn.commit()
                        _clear_pr_cache()
                        if _to_queue:
                            st.success(f"✅ Removed from invoice {_inv} — returned to Procurement Queue for rework.")
                        else:
                            st.success(f"✅ Removed from invoice {_inv} — returned to Purchase Register picker.")
                            _keep_purchase_picker_date_visible(r.get("doc_date") or r.get("document_date"))
                        st.session_state.pop(_rb_confirm_key, None)
                        st.session_state.pop(f"pa_rb_dest_{_kb}", None)
                        st.rerun()

                    except Exception as _rbe:
                        _rb_conn.rollback()
                        st.error(f"Rollback failed (rolled back): {_rbe}")
                    finally:
                        _rb_conn.close()

                except Exception as _ce2:
                    st.error(f"DB connection failed: {_ce2}")

            if _rbc2.button("✕ Cancel", key=f"pa_rb_no_{_kb}",
                            use_container_width=True):
                st.session_state.pop(_rb_confirm_key, None)
                st.session_state.pop(f"pa_rb_dest_{_kb}", None)
                st.rerun()

        else:
            if st.button(
                f"↩ Rollback from invoice {_inv}",
                key=f"pa_rb_{_kb}",
                help=f"Unlink {_pname} ({_eye}) from invoice {_inv}",
            ):
                st.session_state[_rb_confirm_key] = True
                st.rerun()
    else:
        _qrb_key = f"pa_qrb_confirm_{_kb}"
        if st.session_state.get(_qrb_key):
            st.warning(
                f"Send **{_pname} ({_eye})** back to Procurement Queue? "
                "This cancels this challan/purchase acknowledgement and allows re-entry."
            )
            _qc1, _qc2 = st.columns(2)
            if _qc1.button("✅ Confirm Queue Rollback", key=f"pa_qrb_yes_{_kb}",
                           type="primary", use_container_width=True):
                try:
                    from modules.sql_adapter import get_transaction_connection
                    _qrb_conn = get_transaction_connection()
                    try:
                        with _qrb_conn.cursor() as _qc:
                            _qc.execute("""
                                UPDATE purchase_acknowledgements
                                SET billing_status = 'CANCELLED',
                                    invoice_no      = NULL,
                                    challan_no      = NULL,
                                    purchase_price  = 0,
                                    total_value     = 0,
                                    received_qty    = 0,
                                    notes = TRIM(BOTH ' |' FROM (
                                        REGEXP_REPLACE(
                                            COALESCE(notes,''),
                                            'invoice:[^|]+\\|?\\s*', '', 'g'
                                        ) || ' | rollback_to_procurement_queue'
                                    ))
                                WHERE id = %(pid)s::uuid
                            """, {"pid": _pid})
                            if r.get("order_line_id"):
                                _qc.execute("""
                                    UPDATE order_lines
                                    SET lens_params =
                                        COALESCE(lens_params, '{}'::jsonb)
                                        || jsonb_build_object(
                                            'replenishment_status', 'ORDERED',
                                            'procurement_status', 'ORDERED',
                                            'purchase_register_rollback_at', NOW()::text
                                        )
                                    WHERE id = %(olid)s::uuid
                                """, {"olid": r.get("order_line_id")})
                        _qrb_conn.commit()
                        _clear_pr_cache()
                        st.success("Returned to Procurement Queue for re-entry.")
                        st.session_state.pop(_qrb_key, None)
                        st.rerun()
                    except Exception as _qre:
                        _qrb_conn.rollback()
                        st.error(f"Queue rollback failed: {_qre}")
                    finally:
                        _qrb_conn.close()
                except Exception as _qce:
                    st.error(f"DB connection failed: {_qce}")
            if _qc2.button("✕ Cancel", key=f"pa_qrb_no_{_kb}",
                           use_container_width=True):
                st.session_state.pop(_qrb_key, None)
                st.rerun()
        elif _chal:
            if st.button(
                "↩ Rollback to Procurement Queue",
                key=f"pa_qrb_{_kb}",
                help="Use when challan/purchase entry was wrong and must be re-entered from Procurement Queue.",
            ):
                st.session_state[_qrb_key] = True
                st.rerun()


# ── PO card ─────────────────────────────────────────────────────────────────────

def _render_po_card(r):
    _po_id  = r["po_id"]
    _po_no  = r.get("po_no","—")
    _sup    = r.get("supplier_name","—")
    _status = str(r.get("status","DRAFT")).upper()
    _total  = float(r.get("total_value") or 0)
    _date   = str(r.get("doc_date",""))[:10]
    _items  = r.get("_items",[])
    _exp    = str(r.get("exp_date","") or "")[:10]
    _notes  = r.get("notes","") or ""
    _kb     = f"po_{str(_po_id).replace('-','')[-10:]}"

    _st_color = {
        "DRAFT":"#64748b","SENT":"#3b82f6","CONFIRMED":"#8b5cf6",
        "RECEIVED":"#22c55e","PARTIAL":"#f59e0b","CANCELLED":"#ef4444"
    }.get(_status,"#475569")

    st.markdown(
        f"<div style='background:#080f1a;border:1px solid #1e293b;"
        f"border-left:4px solid {_st_color};border-radius:6px;padding:6px 12px;margin:2px 0'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span style='color:#f1f5f9;font-weight:700;font-family:monospace'>{_po_no}</span>"
        f"<span style='background:{_st_color}22;color:{_st_color};"
        f"font-size:0.68rem;font-weight:700;padding:2px 8px;border-radius:6px'>{_status}</span>"
        f"</div>"
        f"<div style='color:#475569;font-size:0.7rem;margin-top:2px'>"
        f"{_sup}  ·  {len(_items)} item(s)  ·  &#8377;{_total:,.0f}  ·  {_date}"
        + (f"  ·  Expected: {_exp}" if _exp else "")
        + "</div></div>",
        unsafe_allow_html=True
    )

    with st.expander("📋 Items / Actions", expanded=False):
        # Items
        if _items:
            for it in _items:
                _pw = _fmt_pwr(it)
                st.markdown(
                    f"<div style='padding:3px 8px;border-left:2px solid #1e293b;"
                    f"margin:2px 0;font-size:0.78rem;color:#94a3b8'>"
                    f"<b style='color:#e2e8f0'>{str(it.get('eye_side','')).upper()}</b>"
                    f" · {it.get('product_name','')} {_pw}"
                    f" · Ordered: {it.get('ordered_qty',0)}"
                    f" · Received: {it.get('received_qty',0)}"
                    f" · &#8377;{float(it.get('unit_price',0)):,.2f}"
                    f" · {it.get('item_status','PENDING')}"
                    f"</div>",
                    unsafe_allow_html=True
                )
        else:
            st.caption("No line items found.")

        if _notes:
            st.caption(f"Notes: {_notes}")
        if _exp:
            st.caption(f"Expected: {_exp}")

        # Status actions
        if _status not in ("RECEIVED","CANCELLED"):
            ac1, ac2, ac3 = st.columns(3)
            if _status == "DRAFT":
                if ac1.button("📤 Mark Sent", key=f"pr_send_{_kb}",
                              type="primary", use_container_width=True):
                    if _rw("UPDATE supplier_orders SET status='SENT' WHERE id=%(id)s",
                           {"id": _po_id}):
                        st.success("Marked Sent"); st.rerun()
            if _status in ("SENT","CONFIRMED"):
                if ac2.button("✅ Mark Received", key=f"pr_recv_{_kb}",
                              type="primary", use_container_width=True):
                    if _rw("UPDATE supplier_orders SET status='RECEIVED' WHERE id=%(id)s",
                           {"id": _po_id}):
                        st.success("Marked Received"); st.rerun()
            if ac3.button("❌ Cancel", key=f"pr_cancel_{_kb}", use_container_width=True):
                if _rw("UPDATE supplier_orders SET status='CANCELLED' WHERE id=%(id)s",
                       {"id": _po_id}):
                    st.warning("Cancelled"); st.rerun()

        # WhatsApp
        if _items and _status in ("DRAFT","SENT"):
            _mob_rows = _q("""SELECT COALESCE(mobile,'') AS mob
                              FROM parties WHERE id=%(sid)s::uuid LIMIT 1""",
                           {"sid": r.get("supplier_id","")})
            _mob = (_mob_rows[0]["mob"] if _mob_rows else "").replace(" ","")
            _wa_d = "".join(c for c in _mob if c.isdigit())
            if _wa_d.startswith("91") and len(_wa_d)==12: _wa_d = _wa_d[2:]
            _wa_num = f"91{_wa_d}" if len(_wa_d)==10 else ""
            if _wa_num:
                import urllib.parse as _up
                _msg = "\n".join(
                    [f"*PO: {_po_no}*", f"Date: {_date}", f"Supplier: {_sup}", ""]
                    + [f"• {it.get('product_name','')} ({str(it.get('eye_side','')).upper()}) "
                       f"{_fmt_pwr(it)} — Qty {it.get('ordered_qty',0)}" for it in _items]
                    + (["", f"Note: {_notes}"] if _notes else [])
                    + ["", "Please confirm receipt. 🙏"]
                )
                st.link_button("📲 Send via WhatsApp",
                               f"https://wa.me/{_wa_num}?text={_up.quote(_msg)}",
                               use_container_width=True)


# ── GRN card ────────────────────────────────────────────────────────────────────

def _render_grn_card(r):
    _ino    = r.get("invoice_no","—")
    _sup    = r.get("supplier_name","—")
    _date   = str(r.get("doc_date",""))[:10]
    _total  = float(r.get("total_value") or 0)
    _sub    = float(r.get("subtotal") or 0)
    _gst    = float(r.get("gst_amount") or 0)
    _pstat  = str(r.get("payment_status","UNPAID")).upper()
    _sinv   = r.get("supplier_inv","") or ""
    _poref  = r.get("po_ref","") or ""
    _sc     = "#22c55e" if _pstat == "PAID" else "#f59e0b"
    _kb     = f"grn_{str(_ino).replace('/','_').replace('-','_')[-12:]}"

    st.markdown(
        f"<div style='background:#080f1a;border:1px solid #1e293b;"
        f"border-left:4px solid #06b6d4;border-radius:6px;padding:6px 12px;margin:2px 0'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span style='color:#f1f5f9;font-weight:700;font-family:monospace'>{_ino}</span>"
        f"<span style='background:{_sc}22;color:{_sc};"
        f"font-size:0.68rem;font-weight:700;padding:2px 8px;border-radius:6px'>{_pstat}</span>"
        f"</div>"
        f"<div style='color:#475569;font-size:0.7rem;margin-top:2px'>"
        f"{_sup}  ·  {r.get('total_items',0)} item(s)  ·  &#8377;{_total:,.0f}  ·  {_date}"
        + (f"  ·  PO: {_poref}" if _poref else "")
        + "</div></div>",
        unsafe_allow_html=True
    )

    with st.expander("📋 Detail / Audit", expanded=False):
        m1, m2, m3 = st.columns(3)
        m1.metric("Subtotal", f"₹{_sub:,.2f}")
        m2.metric("GST",      f"₹{_gst:,.2f}")
        m3.metric("Total",    f"₹{_total:,.2f}")
        if _sinv:
            st.caption(f"Supplier invoice: {_sinv}")

        with st.expander("🧾 Invoice No. / Date Correction", expanded=False):
            _idc1, _idc2, _idc3 = st.columns([2.5, 1.5, 1])
            _new_inv_no = _idc1.text_input(
                "Supplier Invoice No.",
                value=str(_ino or ""),
                key=f"grn_edit_inv_no_{_kb}",
            )
            try:
                _cur_inv_date = datetime.date.fromisoformat(str(_date)[:10]) if _date else datetime.date.today()
            except Exception:
                _cur_inv_date = datetime.date.today()
            _new_inv_date = _idc2.date_input(
                "Invoice Date",
                value=_cur_inv_date,
                key=f"grn_edit_inv_date_{_kb}",
                format="DD/MM/YYYY",
            )
            if _idc3.button("💾 Save", key=f"grn_edit_inv_save_{_kb}", use_container_width=True):
                _new_inv_no = _new_inv_no.strip()
                if not _new_inv_no:
                    st.error("Invoice number cannot be blank.")
                else:
                    from modules.core.date_guard import validate_not_future
                    _ok_dt, _msg_dt = validate_not_future(_new_inv_date, "Purchase invoice date")
                    if not _ok_dt:
                        st.error(_msg_dt)
                        return
                    try:
                        from modules.sql_adapter import get_transaction_connection
                        _edit_conn = get_transaction_connection()
                        try:
                            with _edit_conn.cursor() as _ec:
                                if _new_inv_no != _ino:
                                    _ec.execute(
                                        "SELECT 1 FROM purchase_invoices WHERE invoice_no=%(n)s LIMIT 1",
                                        {"n": _new_inv_no},
                                    )
                                    if _ec.fetchone():
                                        raise ValueError(f"Invoice {_new_inv_no} already exists.")
                                _ec.execute("""
                                    UPDATE purchase_invoices
                                    SET invoice_no = %(new)s,
                                        supplier_invoice_no = %(new)s,
                                        invoice_date = %(dt)s::date,
                                        updated_at = NOW()
                                    WHERE invoice_no = %(old)s
                                """, {"new": _new_inv_no, "old": _ino, "dt": str(_new_inv_date)})
                                if _new_inv_no != _ino:
                                    _ec.execute("""
                                        UPDATE purchase_invoice_lines
                                        SET invoice_no = %(new)s
                                        WHERE invoice_no = %(old)s
                                    """, {"new": _new_inv_no, "old": _ino})
                                    _ec.execute("""
                                        UPDATE purchase_acknowledgements
                                        SET invoice_no = %(new)s,
                                            notes = TRIM(BOTH ' |' FROM (
                                                REGEXP_REPLACE(
                                                    COALESCE(notes,''),
                                                    'invoice:[^|]+\\|?\\s*', '', 'g'
                                                ) || ' | invoice:' || %(new)s
                                            ))
                                        WHERE invoice_no = %(old)s
                                           OR COALESCE(notes,'') LIKE %(old_ref)s
                                    """, {
                                        "new": _new_inv_no,
                                        "old": _ino,
                                        "old_ref": f"%invoice:{_ino}%",
                                    })
                            _edit_conn.commit()
                            st.success("Invoice number/date updated.")
                            st.rerun()
                        except Exception as _ie:
                            _edit_conn.rollback()
                            st.error(f"Invoice correction failed: {_ie}")
                        finally:
                            _edit_conn.close()
                    except Exception as _ce:
                        st.error(f"DB connection failed: {_ce}")

        with st.expander("🚚 Courier / Round-off Adjustments", expanded=False):
            _adj_key = f"grn_adj_{_kb}"
            _ad1, _ad2, _ad3, _ad4, _ad5 = st.columns([2, 2, 2, 2, 3])
            _courier_amt = _ad1.number_input(
                "Courier / Freight",
                value=float(r.get("courier_amount") or 0),
                min_value=0.0,
                step=1.0,
                format="%.2f",
                key=f"{_adj_key}_courier",
            )
            _courier_gst = _ad2.number_input(
                "Courier GST",
                value=float(r.get("courier_gst") or 0),
                min_value=0.0,
                step=1.0,
                format="%.2f",
                key=f"{_adj_key}_cgst",
            )
            _gst_adj = _ad3.number_input(
                "GST adj. (+/-)",
                value=float(r.get("gst_adjustment_amount") or 0),
                step=0.01,
                format="%.2f",
                key=f"{_adj_key}_gstadj",
                help="Use for paise-level GST difference between supplier software and WIN54.",
            )
            _round_off = _ad4.number_input(
                "Round-off (+/-)",
                value=float(r.get("round_off_amount") or 0),
                step=0.01,
                format="%.2f",
                key=f"{_adj_key}_round",
            )
            _adj_notes = _ad5.text_input(
                "Adjustment note",
                value=str(r.get("adjustment_notes") or ""),
                key=f"{_adj_key}_notes",
                placeholder="Courier bill / round-off reason",
            )
            _new_total = round(_sub + _gst + _gst_adj + _courier_amt + _courier_gst + _round_off, 2)
            st.caption(
                f"Recomputed total: ₹{_sub:,.2f} + GST ₹{_gst:,.2f} + GST adj ₹{_gst_adj:,.2f} "
                f"+ courier ₹{_courier_amt:,.2f} + courier GST ₹{_courier_gst:,.2f} "
                f"+ round-off ₹{_round_off:,.2f} = ₹{_new_total:,.2f}"
            )
            if st.button("💾 Save GST / courier / round-off", key=f"{_adj_key}_save"):
                if _rw("""
                    UPDATE purchase_invoices
                    SET courier_amount = %(ca)s,
                        courier_gst = %(cg)s,
                        gst_adjustment_amount = %(ga)s,
                        round_off_amount = %(ro)s,
                        adjustment_notes = NULLIF(%(notes)s, ''),
                        invoice_total = ROUND(
                            COALESCE(subtotal,0)
                            + COALESCE(gst_amount,0)
                            + %(ga)s
                            + %(ca)s
                            + %(cg)s
                            + %(ro)s,
                            2
                        ),
                        updated_at = NOW()
                    WHERE invoice_no = %(ino)s
                """, {
                    "ca": _courier_amt,
                    "cg": _courier_gst,
                    "ga": _gst_adj,
                    "ro": _round_off,
                    "notes": _adj_notes.strip(),
                    "ino": _ino,
                }):
                    st.success("GST / courier / round-off saved.")
                    st.rerun()

        # ── Load invoice lines ────────────────────────────────────────────
        _inv_lines = _q("""
            SELECT
                il.id::text         AS line_id,
                il.item_no,
                il.product_name,
                il.eye_side,
                il.sph, il.cyl, il.axis, il.add_power,
                il.received_qty,
                il.actual_price,
                il.gst_percent,
                il.line_total,
                -- Bug 4 fix: join PA on challan_no OR order_no so both single-order
                -- and multi-challan invoice lines resolve to the correct PA row.
                -- supplier_order_id stores challan_no for multi-challan lines and
                -- order_no (or ACK-<order>) for single-order lines.
                pa.id::text         AS pa_id,
                pa.order_no         AS order_no,
                pa.billing_status   AS pa_status
            FROM purchase_invoice_lines il
            LEFT JOIN purchase_acknowledgements pa
                ON (
                    -- multi-challan path: supplier_order_id = challan_no
                    LOWER(COALESCE(pa.challan_no,'')) = LOWER(il.supplier_order_id)
                    OR
                    -- single-order path: supplier_order_id contains order_no reference
                    LOWER(pa.order_no) = LOWER(SPLIT_PART(il.supplier_order_id, ' | ', 1))
                )
                AND COALESCE(LOWER(pa.eye_side),'') = COALESCE(LOWER(il.eye_side),'')
                AND (
                    pa.product_id IS NULL
                    OR il.product_id IS NULL
                    OR pa.product_id::text = il.product_id::text
                )
            WHERE il.invoice_no = %(ino)s
            ORDER BY il.item_no
        """, {"ino": _ino})

        if not _inv_lines:
            st.caption("No lines found on this invoice.")
        else:
            st.markdown(
                "<div style='color:#f59e0b;font-size:0.72rem;font-weight:700;"
                "margin-bottom:6px'>🔍 Audit — remove a line to roll it back to "
                "challan-only state, then re-post via the challan picker above.</div>",
                unsafe_allow_html=True
            )

            # Column headers
            _h = st.columns([0.4, 2.2, 0.5, 1.2, 1, 1, 1])
            for _col, _lbl in zip(_h, ["#","Product","Eye","Power","Qty","Price","Total"]):
                _col.markdown(
                    f"<div style='font-size:0.62rem;font-weight:700;color:#475569;"
                    f"border-bottom:1px solid #1e3a5f;padding-bottom:2px'>{_lbl}</div>",
                    unsafe_allow_html=True
                )

            for _il in _inv_lines:
                _lid  = _il.get("line_id","")
                _lkb  = f"il_{str(_lid).replace('-','')[-10:]}"
                _pn   = str(_il.get("product_name","—"))[:28]
                _eye  = str(_il.get("eye_side","")).upper()
                _qty  = int(_il.get("received_qty") or 1)
                _prc  = float(_il.get("actual_price") or 0)
                _lt   = float(_il.get("line_total") or 0)
                _ono  = _il.get("order_no","—")

                # Power string
                def _fp(v, is_axis=False):
                    if v is None: return "—"
                    return str(int(float(v))) if is_axis else f"{float(v):+.2f}"
                _pw = f"S{_fp(_il.get('sph'))} C{_fp(_il.get('cyl'))} A{_fp(_il.get('axis'),True)}"
                if _il.get("add_power"):
                    _pw += f" +{float(_il['add_power']):.2f}"

                _lc = st.columns([0.4, 2.2, 0.5, 1.2, 1, 1, 1])
                _lc[0].markdown(
                    f"<div style='color:#475569;font-size:0.72rem;"
                    f"padding-top:5px;text-align:center'>{_il.get('item_no','')}</div>",
                    unsafe_allow_html=True)
                _lc[1].markdown(
                    f"<div style='color:#e2e8f0;font-size:0.78rem;font-weight:600;"
                    f"padding-top:5px'>{_pn}</div>"
                    f"<div style='color:#475569;font-size:0.65rem'>{_ono}</div>",
                    unsafe_allow_html=True)
                _lc[2].markdown(
                    f"<div style='color:#94a3b8;font-size:0.78rem;"
                    f"padding-top:5px;text-align:center'>{_eye}</div>",
                    unsafe_allow_html=True)
                _lc[3].markdown(
                    f"<div style='color:#a5b4fc;font-size:0.68rem;"
                    f"font-family:monospace;padding-top:5px'>{_pw}</div>",
                    unsafe_allow_html=True)
                _lc[4].markdown(
                    f"<div style='color:#94a3b8;font-size:0.78rem;"
                    f"padding-top:5px;text-align:center'>{_qty}</div>",
                    unsafe_allow_html=True)
                _lc[5].markdown(
                    f"<div style='color:#94a3b8;font-size:0.78rem;"
                    f"padding-top:5px'>₹{_prc:,.2f}</div>",
                    unsafe_allow_html=True)
                _lc[6].markdown(
                    f"<div style='color:#10b981;font-size:0.78rem;font-weight:600;"
                    f"padding-top:5px'>₹{_lt:,.2f}</div>",
                    unsafe_allow_html=True)

                # ── Rollback button per line ──────────────────────────────
                _confirm_key = f"grn_rb_confirm_{_lkb}"
                if st.session_state.get(_confirm_key):
                    st.warning(
                        f"Remove **{_pn} ({_eye})** from invoice {_ino}? "
                        f"It will return to challan-only state and can be re-posted."
                    )
                    _rc1, _rc2, _rc3 = st.columns(3)
                    _rb_line_dest = st.radio(
                        "Rollback destination",
                        [
                            "Purchase Register picker (keep purchase/challan, change invoice)",
                            "Procurement Queue (full rework, re-enter purchase)",
                        ],
                        key=f"grn_rb_dest_{_lkb}",
                    )
                    _line_to_queue = _rb_line_dest.startswith("Procurement Queue")

                    # Optional re-assign target
                    _existing_invs = _q("""
                        SELECT invoice_no FROM purchase_invoices
                        WHERE supplier_name = %(sup)s
                          AND invoice_no != %(cur)s
                          AND COALESCE(is_deleted,FALSE) = FALSE
                        ORDER BY invoice_date DESC LIMIT 20
                    """, {"sup": _sup, "cur": _ino})
                    _inv_opts = ["— Leave as challan only —"] + [
                        i["invoice_no"] for i in (_existing_invs or [])
                    ]
                    _reassign = _rc3.selectbox(
                        "Re-assign to",
                        _inv_opts,
                        key=f"grn_rb_reassign_{_lkb}",
                        label_visibility="collapsed",
                    )

                    if _rc1.button("✅ Confirm Remove", key=f"grn_rb_yes_{_lkb}",
                                   type="primary", use_container_width=True):
                        # Bugs 1 + 2 fix: wrap all steps in ONE transaction so any
                        # failure leaves the DB untouched, and copy the line into the
                        # target invoice BEFORE deleting it from the source.
                        try:
                            from modules.sql_adapter import get_transaction_connection
                            _rb_conn = get_transaction_connection()
                            try:
                                with _rb_conn.cursor() as _rc:
                                    _pa_id = _il.get("pa_id")

                                    # Step 1 — if reassigning, copy the line to the
                                    # target invoice FIRST (row still exists at this point)
                                    if _reassign and _reassign != "— Leave as challan only —":
                                        _rc.execute("""
                                            INSERT INTO purchase_invoice_lines (
                                                invoice_no, item_no,
                                                supplier_order_id, supplier_order_item_no,
                                                product_name, eye_side,
                                                sph, cyl, axis, add_power,
                                                ordered_qty, received_qty,
                                                actual_price, gst_percent, line_total,
                                                created_at
                                            )
                                            SELECT %(new_inv)s,
                                                   COALESCE(
                                                     (SELECT MAX(item_no)+1
                                                      FROM purchase_invoice_lines
                                                      WHERE invoice_no=%(new_inv)s), 1),
                                                   supplier_order_id, supplier_order_item_no,
                                                   product_name, eye_side,
                                                   sph, cyl, axis, add_power,
                                                   ordered_qty, received_qty,
                                                   actual_price, gst_percent, line_total,
                                                   NOW()
                                            FROM purchase_invoice_lines
                                            WHERE id = %(orig_lid)s::uuid
                                        """, {"new_inv": _reassign, "orig_lid": _lid})

                                    # Step 2 — now delete from source invoice
                                    _rc.execute(
                                        "DELETE FROM purchase_invoice_lines WHERE id=%(id)s::uuid",
                                        {"id": _lid},
                                    )

                                    # Step 3 — restore PA billing_status
                                    if _pa_id:
                                        if _reassign and _reassign != "— Leave as challan only —":
                                            # PA moves to the new invoice
                                            _rc.execute("""
                                                UPDATE purchase_acknowledgements
                                                SET billing_status = 'INVOICED',
                                                    invoice_no = %(new_inv)s,
                                                    notes = REGEXP_REPLACE(
                                                        COALESCE(notes,''),
                                                        'invoice:[^|]+\\|?\\s*', '', 'g'
                                                    ) || ' invoice:' || %(new_inv)s
                                                WHERE id = %(pa_id)s::uuid
                                            """, {"pa_id": _pa_id, "new_inv": _reassign})
                                        elif _line_to_queue:
                                            _rc.execute("""
                                                UPDATE purchase_acknowledgements
                                                SET billing_status = 'CANCELLED',
                                                    invoice_no      = NULL,
                                                    purchase_price  = 0,
                                                    total_value     = 0,
                                                    received_qty    = 0,
                                                    notes = TRIM(BOTH ' |' FROM (
                                                        REGEXP_REPLACE(
                                                            COALESCE(notes,''),
                                                            'invoice:[^|]+\\|?\\s*', '', 'g'
                                                        ) || ' | rollback_to_procurement_queue'
                                                    ))
                                                WHERE id = %(pa_id)s::uuid
                                            """, {"pa_id": _pa_id})
                                            _rc.execute("""
                                                UPDATE order_lines ol
                                                SET lens_params =
                                                    COALESCE(ol.lens_params, '{}'::jsonb)
                                                    || jsonb_build_object(
                                                        'replenishment_status', 'ORDERED',
                                                        'procurement_status', 'ORDERED',
                                                        'purchase_register_rollback_at', NOW()::text
                                                    )
                                                FROM purchase_acknowledgements pa
                                                WHERE pa.id = %(pa_id)s::uuid
                                                  AND ol.id = pa.order_line_id
                                            """, {"pa_id": _pa_id})
                                        else:
                                            # PA returns to challan-only
                                            _rc.execute("""
                                                UPDATE purchase_acknowledgements
                                                SET billing_status = 'PURCHASE_ACKED',
                                                    invoice_no     = NULL,
                                                    document_date  = CASE
                                                        WHEN document_date > CURRENT_DATE THEN CURRENT_DATE
                                                        ELSE document_date
                                                    END,
                                                    notes = REGEXP_REPLACE(
                                                        COALESCE(notes,''),
                                                        'invoice:[^|]+\\|?\\s*', '', 'g'
                                                    )
                                                WHERE id = %(pa_id)s::uuid
                                            """, {"pa_id": _pa_id})

                                    # Step 4 — recalculate source invoice header
                                    _rc.execute("""
                                        UPDATE purchase_invoices SET
                                            total_items        = (
                                                SELECT COUNT(*) FROM purchase_invoice_lines
                                                WHERE invoice_no = %(ino)s),
                                            total_qty_received = (
                                                SELECT COALESCE(SUM(received_qty),0)
                                                FROM purchase_invoice_lines
                                                WHERE invoice_no = %(ino)s),
                                            subtotal           = (
                                                SELECT COALESCE(SUM(actual_price * received_qty),0)
                                                FROM purchase_invoice_lines
                                                WHERE invoice_no = %(ino)s),
                                            gst_amount         = (
                                                SELECT COALESCE(SUM(
                                                    actual_price * received_qty * gst_percent/100),0)
                                                FROM purchase_invoice_lines
                                                WHERE invoice_no = %(ino)s),
                                            invoice_total      = (
                                                SELECT COALESCE(SUM(line_total),0)
                                                FROM purchase_invoice_lines
                                                WHERE invoice_no = %(ino)s)
                                                + COALESCE(courier_amount,0)
                                                + COALESCE(courier_gst,0)
                                                + COALESCE(gst_adjustment_amount,0)
                                                + COALESCE(round_off_amount,0),
                                            updated_at = NOW()
                                        WHERE invoice_no = %(ino)s
                                    """, {"ino": _ino})

                                    # Step 5 — recalculate target invoice header (if reassigning)
                                    if _reassign and _reassign != "— Leave as challan only —":
                                        _rc.execute("""
                                            UPDATE purchase_invoices SET
                                                total_items        = (
                                                    SELECT COUNT(*) FROM purchase_invoice_lines
                                                    WHERE invoice_no = %(ino)s),
                                                total_qty_received = (
                                                    SELECT COALESCE(SUM(received_qty),0)
                                                    FROM purchase_invoice_lines
                                                    WHERE invoice_no = %(ino)s),
                                                subtotal           = (
                                                    SELECT COALESCE(SUM(actual_price * received_qty),0)
                                                    FROM purchase_invoice_lines
                                                    WHERE invoice_no = %(ino)s),
                                                gst_amount         = (
                                                    SELECT COALESCE(SUM(
                                                        actual_price * received_qty * gst_percent/100),0)
                                                    FROM purchase_invoice_lines
                                                    WHERE invoice_no = %(ino)s),
                                                invoice_total      = (
                                                    SELECT COALESCE(SUM(line_total),0)
                                                    FROM purchase_invoice_lines
                                                    WHERE invoice_no = %(ino)s)
                                                    + COALESCE(courier_amount,0)
                                                    + COALESCE(courier_gst,0)
                                                    + COALESCE(gst_adjustment_amount,0)
                                                    + COALESCE(round_off_amount,0),
                                                updated_at = NOW()
                                            WHERE invoice_no = %(ino)s
                                        """, {"ino": _reassign})

                                _rb_conn.commit()
                                _clear_pr_cache()

                                if _reassign and _reassign != "— Leave as challan only —":
                                    st.success(
                                        f"✅ Line moved from {_ino} → {_reassign}. "
                                        f"Both invoices recalculated."
                                    )
                                elif _line_to_queue:
                                    st.success(
                                        f"✅ Line removed from {_ino}. "
                                        f"Returned to Procurement Queue for rework."
                                    )
                                else:
                                    st.success(
                                        f"✅ Line removed from {_ino}. "
                                        f"Returned to Purchase Register picker — use the picker above to re-post."
                                    )
                                    _keep_purchase_picker_date_visible(_oldest_purchase_picker_date())

                            except Exception as _rbe:
                                _rb_conn.rollback()
                                st.error(f"Rollback failed (transaction rolled back): {_rbe}")
                            finally:
                                _rb_conn.close()

                        except Exception as _conn_err:
                            st.error(f"Could not open transaction: {_conn_err}")

                        st.session_state.pop(_confirm_key, None)
                        st.session_state.pop(f"grn_rb_dest_{_lkb}", None)
                        st.rerun()

                    if _rc2.button("✕ Cancel", key=f"grn_rb_no_{_lkb}",
                                   use_container_width=True):
                        st.session_state.pop(_confirm_key, None)
                        st.session_state.pop(f"grn_rb_dest_{_lkb}", None)
                        st.rerun()
                else:
                    if st.button(
                        f"↩ Remove from invoice",
                        key=f"grn_rb_{_lkb}",
                        help=f"Remove {_pn} ({_eye}) from {_ino} — rolls back to challan only",
                    ):
                        st.session_state[_confirm_key] = True
                        st.rerun()

                st.markdown(
                    "<div style='border-bottom:1px solid #0f172a;margin:2px 0'></div>",
                    unsafe_allow_html=True)

        st.markdown("---")
        if _pstat == "UNPAID":
            if st.button("✅ Mark as Paid", key=f"pr_pay_{_kb}",
                         type="primary", use_container_width=True):
                if _rw("UPDATE purchase_invoices SET payment_status='PAID' WHERE invoice_no=%(n)s",
                       {"n": _ino}):
                    st.success("Marked as Paid"); st.rerun()

        # ── Void entire invoice ───────────────────────────────────────────
        # Rolls back ALL lines at once — resets every PA row to PURCHASE_ACKED,
        # deletes all purchase_invoice_lines, marks invoice as VOIDED.
        # The challan picker immediately re-offers all lines for re-posting.
        _void_key = f"grn_void_confirm_{_kb}"
        if _pstat not in ("PAID",):   # can't void a paid invoice
            if st.session_state.get(_void_key):
                st.error(
                    f"⚠️ **Void entire invoice {_ino}?** "
                    f"Choose whether all {len(_inv_lines)} line(s) return to register posting "
                    f"or full procurement rework."
                )
                _void_dest = st.radio(
                    "Rollback destination",
                    [
                        "Purchase Register picker (keep purchase/challan, change invoice)",
                        "Procurement Queue (full rework, re-enter purchase)",
                    ],
                    key=f"grn_void_dest_{_kb}",
                )
                _void_to_queue = _void_dest.startswith("Procurement Queue")
                _vc1, _vc2 = st.columns(2)
                if _vc1.button("🗑️ Yes, Void Invoice", key=f"grn_void_yes_{_kb}",
                               type="primary", use_container_width=True):
                    try:
                        from modules.sql_adapter import get_transaction_connection
                        _vc = get_transaction_connection()
                        try:
                            with _vc.cursor() as _cur:
                                # 1. Reset ALL PA rows linked to this invoice
                                if _void_to_queue:
                                    _cur.execute("""
                                        UPDATE order_lines ol
                                        SET lens_params =
                                            COALESCE(ol.lens_params, '{}'::jsonb)
                                            || jsonb_build_object(
                                                'replenishment_status', 'ORDERED',
                                                'procurement_status', 'ORDERED',
                                                'purchase_register_rollback_at', NOW()::text
                                            )
                                        FROM purchase_acknowledgements pa
                                        WHERE ol.id = pa.order_line_id
                                          AND pa.billing_status = 'INVOICED'
                                          AND (
                                            COALESCE(pa.notes,'') LIKE %(ref)s
                                            OR pa.invoice_no = %(ino)s
                                          )
                                    """, {"ino": _ino, "ref": f"%invoice:{_ino}%"})
                                    _cur.execute("""
                                        UPDATE purchase_acknowledgements
                                        SET billing_status = 'CANCELLED',
                                            invoice_no      = NULL,
                                            purchase_price  = 0,
                                            total_value     = 0,
                                            received_qty    = 0,
                                            notes = TRIM(BOTH ' |' FROM (
                                                REGEXP_REPLACE(
                                                    COALESCE(notes,''),
                                                    'invoice:[^|]+\\|?\\s*', '', 'g'
                                                ) || ' | rollback_to_procurement_queue'
                                            ))
                                        WHERE billing_status = 'INVOICED'
                                          AND (
                                            COALESCE(notes,'') LIKE %(ref)s
                                            OR invoice_no = %(ino)s
                                          )
                                    """, {"ino": _ino, "ref": f"%invoice:{_ino}%"})
                                else:
                                    _cur.execute("""
                                        UPDATE purchase_acknowledgements
                                        SET billing_status = 'PURCHASE_ACKED',
                                            invoice_no     = NULL,
                                            document_date  = CASE
                                                WHEN document_date > CURRENT_DATE THEN CURRENT_DATE
                                                ELSE document_date
                                            END,
                                            notes = REGEXP_REPLACE(
                                                COALESCE(notes,''),
                                                'invoice:[^|]+\\|?\\s*', '', 'g'
                                            )
                                        WHERE billing_status = 'INVOICED'
                                          AND (
                                            COALESCE(notes,'') LIKE %(ref)s
                                            OR invoice_no = %(ino)s
                                          )
                                    """, {"ino": _ino, "ref": f"%invoice:{_ino}%"})

                                # 2. Delete all lines
                                _cur.execute(
                                    "DELETE FROM purchase_invoice_lines WHERE invoice_no=%(ino)s",
                                    {"ino": _ino}
                                )

                                # 3. Mark invoice header as VOIDED (not deleted —
                                #    keeps audit trail in registers)
                                _cur.execute("""
                                    UPDATE purchase_invoices SET
                                        payment_status     = 'VOIDED',
                                        total_items        = 0,
                                        total_qty_received = 0,
                                        subtotal           = 0,
                                        gst_amount         = 0,
                                        invoice_total      = 0,
                                        notes = COALESCE(notes,'') || ' [VOIDED]',
                                        updated_at = NOW()
                                    WHERE invoice_no = %(ino)s
                                """, {"ino": _ino})

                            _vc.commit()
                            _clear_pr_cache()
                            if _void_to_queue:
                                st.success(
                                    f"✅ Invoice {_ino} voided. All lines returned to Procurement Queue for rework."
                                )
                            else:
                                st.success(
                                    f"✅ Invoice {_ino} voided. All lines returned to Purchase Register picker."
                                )
                                _keep_purchase_picker_date_visible(_oldest_purchase_picker_date())
                            st.session_state.pop(_void_key, None)
                            st.session_state.pop(f"grn_void_dest_{_kb}", None)
                            st.rerun()
                        except Exception as _ve:
                            _vc.rollback()
                            st.error(f"Void failed (rolled back): {_ve}")
                        finally:
                            _vc.close()
                    except Exception as _vce:
                        st.error(f"DB connection failed: {_vce}")

                if _vc2.button("✕ Cancel", key=f"grn_void_no_{_kb}",
                               use_container_width=True):
                    st.session_state.pop(_void_key, None)
                    st.session_state.pop(f"grn_void_dest_{_kb}", None)
                    st.rerun()
            else:
                if st.button("🗑️ Void Entire Invoice", key=f"grn_void_{_kb}",
                             use_container_width=True,
                             help="Cancel this invoice and return all lines to un-posted state"):
                    st.session_state[_void_key] = True
                    st.rerun()
        else:
            st.caption("ℹ️ Paid invoices cannot be voided. Contact admin to reverse payment first.")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def render_purchase_register():
    st.markdown("### 🔍 Purchase Register")
    st.caption(
        "Primary workflow: pending supplier challans/products → edit price, GST, courier and invoice date → post to accounts. "
        "PO and GRN records are kept below as collapsed audit history."
    )

    # ── Filters ───────────────────────────────────────────────────────────────
    with st.container(border=True):
        _oldest_picker_date, _latest_picker_date = _purchase_picker_date_bounds()
        _default_from = datetime.date.today() - datetime.timedelta(days=90)
        _default_to = datetime.date.today()
        if "pr_f_from" not in st.session_state:
            st.session_state["pr_f_from"] = (
                _oldest_picker_date
                if _oldest_picker_date and _oldest_picker_date < _default_from
                else _default_from
            )
        elif _oldest_picker_date and _oldest_picker_date < st.session_state.get("pr_f_from"):
            st.session_state["pr_f_from"] = _oldest_picker_date
        if "pr_f_to" not in st.session_state:
            st.session_state["pr_f_to"] = (
                _latest_picker_date
                if _latest_picker_date and _latest_picker_date > _default_to
                else _default_to
            )
        elif _latest_picker_date and _latest_picker_date > st.session_state.get("pr_f_to"):
            st.session_state["pr_f_to"] = _latest_picker_date
        f1, f2, f3 = st.columns([3, 3, 3])
        _sup  = f1.text_input("Supplier",   placeholder="🔍 Supplier",
                               key="pr_f_sup",  label_visibility="collapsed")
        _ref  = f2.text_input("Doc / Order", placeholder="🔍 Invoice / Challan / PO / Order no",
                               key="pr_f_ref",  label_visibility="collapsed")
        _prod = f3.text_input("Product",    placeholder="🔍 Product name",
                               key="pr_f_prod", label_visibility="collapsed")

        f4, f5, f6, f7 = st.columns([2, 2, 3, 1])
        _dfrom = f4.date_input("From",
                                value=datetime.date.today() - datetime.timedelta(days=90),
                                key="pr_f_from", label_visibility="collapsed",
                                format="DD/MM/YYYY")
        _dto   = f5.date_input("To",
                                value=datetime.date.today(),
                                key="pr_f_to", label_visibility="collapsed",
                                format="DD/MM/YYYY")
        _dtype = f6.selectbox("Type",
                               ["ALL","CHALLAN","INVOICE","PO","GRN"],
                               format_func=lambda x: {
                                   "ALL":     "All document types",
                                   "CHALLAN": "📋 Challans only",
                                   "INVOICE": "🧾 Invoices only",
                                   "PO":      "📤 POs only",
                                   "GRN":     "🏪 GRNs only",
                               }.get(x, x),
                               key="pr_f_type", label_visibility="collapsed")
        if f7.button("🔄", key="pr_refresh", use_container_width=True):
            st.rerun()

    # ── Load ──────────────────────────────────────────────────────────────────
    pa_rows  = _load_pa(_sup, _ref, _prod, _dfrom, _dto) if _dtype in ("ALL","CHALLAN","INVOICE") else []
    po_rows  = _load_pos(_sup, _ref, _dfrom, _dto)       if _dtype in ("ALL","PO")               else []
    grn_rows = _load_grns(_sup, _ref, _dfrom, _dto)      if _dtype in ("ALL","GRN")              else []
    visible_grn_rows = [
        r for r in grn_rows
        if not (
            str(r.get("payment_status", "")).upper() == "VOIDED"
            and int(float(r.get("total_items") or 0)) == 0
            and abs(float(r.get("total_value") or 0)) < 0.01
        )
    ]

    # Filter PA by type
    if _dtype == "CHALLAN":
        pa_rows = [r for r in pa_rows if r.get("challan_no") and not r.get("invoice_no")]
    elif _dtype == "INVOICE":
        pa_rows = [r for r in pa_rows if r.get("invoice_no")]

    # ── Tab structure: Register view + Creditor Aging ─────────────────────────
    _pr_tab_reg, _pr_tab_aging = st.tabs(["📋 Purchase Register", "📊 Creditor Aging"])

    with _pr_tab_aging:
        st.caption("Outstanding payables from purchase_invoices.balance_due — allocator-maintained truth.")
        _ag1, _ag2, _ag3 = st.columns(3)
        _ag_sup   = _ag1.text_input("Filter supplier", placeholder="Type supplier name",
                                     key="pr_ag_sup", label_visibility="collapsed")
        _ag_minbal = _ag2.number_input("Min outstanding (₹)", min_value=0.0, value=0.01,
                                        step=100.0, key="pr_ag_minbal")
        _ag_status = _ag3.selectbox("Status", ["All", "UNPAID", "PARTIAL"],
                                     key="pr_ag_status")

        try:
            from modules.supplier_allocator import get_outstanding_invoices
            _ag_rows = get_outstanding_invoices(
                supplier_name_like=_ag_sup or None,
                min_balance=float(_ag_minbal),
            )
        except ImportError:
            # Fallback if allocator not deployed — read directly from purchase_invoices
            from modules.sql_adapter import run_query as _rq_ag
            _where_ag = ["COALESCE(pi.invoice_total,0)>0",
                         "COALESCE(pi.is_deleted,FALSE)=FALSE",
                         "COALESCE(pi.payment_status,'UNPAID')!='VOIDED'"]
            _params_ag = {}
            if _ag_sup:
                _where_ag.append("LOWER(COALESCE(pi.supplier_name,'')) LIKE %(sn)s")
                _params_ag["sn"] = f"%{_ag_sup.lower()}%"
            _ag_rows = _rq_ag(f"""
                SELECT invoice_no, supplier_name,
                       invoice_date, due_date,
                       COALESCE(invoice_total,0)     AS invoice_total,
                       COALESCE(amount_paid,0)        AS amount_paid,
                       COALESCE(balance_due, invoice_total, 0) AS balance_due,
                       COALESCE(payment_status,'UNPAID')        AS payment_status,
                       CASE WHEN due_date IS NULL OR due_date>=CURRENT_DATE THEN 'Current'
                            WHEN (CURRENT_DATE-due_date) BETWEEN 1 AND 30 THEN '1-30 days'
                            WHEN (CURRENT_DATE-due_date) BETWEEN 31 AND 60 THEN '31-60 days'
                            WHEN (CURRENT_DATE-due_date) BETWEEN 61 AND 90 THEN '61-90 days'
                            ELSE '90+ days' END AS aging_bucket,
                       CASE WHEN due_date IS NULL OR due_date>=CURRENT_DATE THEN 0
                            WHEN (CURRENT_DATE-due_date) BETWEEN 1 AND 30 THEN 1
                            WHEN (CURRENT_DATE-due_date) BETWEEN 31 AND 60 THEN 2
                            WHEN (CURRENT_DATE-due_date) BETWEEN 61 AND 90 THEN 3
                            ELSE 4 END AS aging_rank
                FROM purchase_invoices pi
                WHERE {" AND ".join(_where_ag)}
                ORDER BY aging_rank DESC, balance_due DESC
            """, _params_ag) or []
            _ag_rows = [r for r in _ag_rows if float(r.get("balance_due",0)) >= float(_ag_minbal)]

        if _ag_status != "All":
            _ag_rows = [r for r in _ag_rows if r.get("payment_status") == _ag_status]

        if not _ag_rows:
            st.success("✅ No outstanding payables for the selected filters.")
        else:
            # Aging bucket metrics
            _bucket_order = ["Current","1-30 days","31-60 days","61-90 days","90+ days"]
            _bucket_totals: dict = {b: 0.0 for b in _bucket_order}
            for r in _ag_rows:
                _bk = r.get("aging_bucket","Current")
                _bucket_totals[_bk] = _bucket_totals.get(_bk, 0) + float(r.get("balance_due",0))
            _bk_cols = st.columns(5)
            for _i, _bk in enumerate(_bucket_order):
                _bk_cols[_i].metric(_bk, f"₹{_bucket_totals[_bk]:,.0f}")

            st.markdown("---")
            _ag_total_bal  = sum(float(r.get("balance_due",0)) for r in _ag_rows)
            _ag_total_inv  = sum(float(r.get("invoice_total",0)) for r in _ag_rows)
            _ag_m1, _ag_m2, _ag_m3 = st.columns(3)
            _ag_m1.metric("Invoices outstanding", len(_ag_rows))
            _ag_m2.metric("Total payable",        f"₹{_ag_total_bal:,.2f}")
            _ag_m3.metric("Unique suppliers",
                           len(set(r.get("supplier_name","") for r in _ag_rows)))
            st.markdown("---")

            # Grouped by supplier
            _by_sup: dict = {}
            for r in _ag_rows:
                _sname = r.get("supplier_name") or "Unknown"
                _by_sup.setdefault(_sname, []).append(r)

            for _sname in sorted(_by_sup.keys()):
                _sinvs = _by_sup[_sname]
                _stotal = sum(float(r.get("balance_due",0)) for r in _sinvs)
                with st.expander(
                    f"🏭 **{_sname}** &nbsp; {len(_sinvs)} invoice(s) &nbsp; "
                    f"Outstanding: **₹{_stotal:,.2f}**",
                    expanded=False,
                ):
                    for r in _sinvs:
                        _bd  = float(r.get("balance_due",0))
                        _it  = float(r.get("invoice_total",0))
                        _ap  = float(r.get("amount_paid",0))
                        _ps  = r.get("payment_status","UNPAID")
                        _bk  = r.get("aging_bucket","Current")
                        _ino = r.get("invoice_no","")
                        _dt  = str(r.get("invoice_date",""))[:10]
                        _ps_color = {"PAID":"#22c55e","PARTIAL":"#f59e0b","UNPAID":"#ef4444"}.get(_ps,"#6b7280")
                        _bk_color = {"Current":"#22c55e","1-30 days":"#f59e0b",
                                     "31-60 days":"#f97316","61-90 days":"#ef4444","90+ days":"#dc2626"}.get(_bk,"#6b7280")
                        st.markdown(
                            f"<div style='background:#0a1628;border-left:3px solid {_bk_color};"
                            f"border-radius:4px;padding:7px 12px;margin-bottom:5px'>"
                            f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                            f"<div>"
                            f"<span style='color:#e2e8f0;font-weight:700'>{_ino}</span>"
                            f"&nbsp;<span style='color:{_bk_color};font-size:0.72rem'>📅 {_bk}</span>"
                            f"&nbsp;<span style='color:{_ps_color};font-size:0.72rem'>{_ps}</span>"
                            f"</div>"
                            f"<div style='text-align:right;color:#e2e8f0;font-size:0.82rem'>"
                            f"Invoice: ₹{_it:,.2f} &nbsp; Paid: ₹{_ap:,.2f} &nbsp; "
                            f"<b>Balance: ₹{_bd:,.2f}</b>"
                            f"</div></div>"
                            f"<div style='color:#64748b;font-size:0.72rem;margin-top:2px'>{_dt}</div>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                        # Payment widget inline
                        if _ps in ("UNPAID", "PARTIAL"):
                            try:
                                from modules.supplier_allocator import render_payment_widget
                                render_payment_widget(_ino, key_prefix=f"ag_{_ino.replace('/','_')}")
                            except ImportError:
                                pass

    with _pr_tab_reg:
        if not pa_rows and not po_rows and not grn_rows:
            st.info(
                f"No purchase records found for the selected period "
                f"({_dfrom.strftime('%d %b %Y')} – {_dto.strftime('%d %b %Y')}). "
                f"Try expanding the date range or clearing filters."
            )
            return
        # ── Metrics ───────────────────────────────────────────────────────────────
        _total_val = (sum(float(r.get("total_value",0)) for r in pa_rows)
                    + sum(float(r.get("total_value",0)) for r in po_rows)
                    + sum(float(r.get("total_value",0)) for r in visible_grn_rows))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("PA Lines",  len(pa_rows))
        m2.metric("POs",       len(po_rows))
        m3.metric("GRNs",      len(visible_grn_rows))
        m4.metric("Total Value", f"₹{_total_val:,.0f}")
        st.markdown("---")

        # ── Inventory posting repair for linked procurements ─────────────────────
        # These are legitimate PA rows tied to customer orders. They must either
        # stay allotted in inventory until dispatch, or net to zero if already sent.
        _unposted_registered = [
            r for r in pa_rows
            if r.get("pa_id")
            and r.get("invoice_no")
            and str(r.get("billing_status") or "").upper() == "INVOICED"
            and not r.get("inventory_posted_at")
        ]
        if _unposted_registered:
            _unposted_total = sum(float(r.get("total_value") or 0) for r in _unposted_registered)
            st.warning(
                f"{len(_unposted_registered)} purchase line(s) are registered but not inventory-posted. "
                f"Total ₹{_unposted_total:,.2f}. Complete posting so stock is either allotted or netted out."
            )
            if st.button(
                f"✅ Complete Inventory Posting for {len(_unposted_registered)} line(s)",
                key="pr_complete_inventory_posting",
                type="primary",
                use_container_width=True,
            ):
                _posted = 0
                _failed = []
                for _upr in _unposted_registered:
                    _pa_id = str(_upr.get("pa_id") or "")
                    if _pa_id and _pa_post_inventory_from_register(_pa_id):
                        _posted += 1
                    else:
                        _failed.append(_upr.get("order_no") or _upr.get("invoice_no") or _pa_id)
                if _failed:
                    st.error(f"Posted {_posted}; failed {len(_failed)}: {', '.join(map(str, _failed[:5]))}")
                else:
                    st.success(f"Inventory posting completed for {_posted} line(s).")
                st.cache_data.clear()
                st.rerun()

        # ── Owner audit: unlinked invoice-match procurements ────────────────────
        _audit_rows = _load_pa_audit(_dfrom, _dto, "PENDING")
        if _audit_rows:
            _audit_total = sum(float(r.get("total_value") or 0) for r in _audit_rows)
            with st.expander(
                f"🧭 Procurement Audit Pending — {len(_audit_rows)} unlinked line(s) · ₹{_audit_total:,.2f}",
                expanded=True,
            ):
                st.caption(
                    "These purchase lines posted from supplier invoices but did not link to a procurement queue/order line. "
                    "They have been kept visible for owner audit and inventory review."
                )
                import csv as _audit_csv, io as _audit_io
                _audit_buf = _audit_io.StringIO()
                _audit_wr = _audit_csv.writer(_audit_buf)
                _audit_wr.writerow([
                    "Supplier", "Invoice", "Date", "Our Product", "Supplier Product",
                    "Supplier Ref", "Description", "Qty", "Rate", "Total", "Audit Status",
                    "Inventory Posted At",
                ])
                for _ar in _audit_rows:
                    _audit_wr.writerow([
                        _ar.get("supplier_name",""), _ar.get("invoice_no",""),
                        _ar.get("doc_date",""), _ar.get("product_name",""),
                        _ar.get("supplier_product_name",""),
                        _ar.get("supplier_order_ref",""),
                        _ar.get("supplier_product_description",""),
                        _ar.get("qty",""), _ar.get("purchase_price",""),
                        _ar.get("total_value",""), _ar.get("audit_status",""),
                        _ar.get("inventory_posted_at",""),
                    ])
                st.download_button(
                    "⬇️ Download Pending Procurement Audit CSV",
                    data=_audit_buf.getvalue().encode("utf-8"),
                    file_name=f"pending_procurement_audit_{_dfrom}_{_dto}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
                for _ar in _audit_rows:
                    _pa = _ar["pa_id"]
                    _audit_doc = _purchase_doc_ref(_ar)
                    _audit_doc_label = _purchase_doc_label(_ar)
                    _audit_doc_display = _audit_doc if _audit_doc != "NO_DOC" else "—"
                    _title = (
                        f"{_ar.get('supplier_name','—')} · "
                        f"{_audit_doc_label} {_audit_doc_display} · "
                        f"{_ar.get('product_name','—')}"
                    )
                    with st.container(border=True):
                        st.markdown(f"**{_title}**")
                        st.caption(
                            f"Supplier item: {_ar.get('supplier_product_name') or '—'} · "
                            f"Qty {_ar.get('qty')} · Rate ₹{float(_ar.get('purchase_price') or 0):,.2f} · "
                            f"Total ₹{float(_ar.get('total_value') or 0):,.2f}"
                        )
                        if _ar.get("supplier_product_description"):
                            st.code(_ar.get("supplier_product_description"), language=None)
                        _akb = str(_pa).replace("-", "")[-10:]
                        _doc_raw = "" if _audit_doc == "NO_DOC" else _audit_doc
                        _date_raw = _ar.get("doc_date") or _ar.get("acknowledged_at") or ""
                        if f"pa_audit_doc_{_akb}" not in st.session_state:
                            st.session_state[f"pa_audit_doc_{_akb}"] = _doc_raw
                        if f"pa_audit_qty_{_akb}" not in st.session_state:
                            st.session_state[f"pa_audit_qty_{_akb}"] = float(_ar.get("qty") or 1)
                        if f"pa_audit_price_{_akb}" not in st.session_state:
                            st.session_state[f"pa_audit_price_{_akb}"] = float(_ar.get("purchase_price") or 0)
                        if f"pa_audit_gst_{_akb}" not in st.session_state:
                            st.session_state[f"pa_audit_gst_{_akb}"] = float(_ar.get("gst_percent") or 0)
                        if f"pa_audit_batch_{_akb}" not in st.session_state:
                            st.session_state[f"pa_audit_batch_{_akb}"] = str(_ar.get("batch_no") or "")
                        if f"pa_audit_date_{_akb}" not in st.session_state:
                            try:
                                st.session_state[f"pa_audit_date_{_akb}"] = datetime.date.fromisoformat(str(_date_raw)[:10])
                            except Exception:
                                st.session_state[f"pa_audit_date_{_akb}"] = datetime.date.today()
                        if f"pa_audit_exp_{_akb}" not in st.session_state:
                            try:
                                _exp_raw = _ar.get("expiry_date") or ""
                                st.session_state[f"pa_audit_exp_{_akb}"] = datetime.date.fromisoformat(str(_exp_raw)[:10]) if _exp_raw else None
                            except Exception:
                                st.session_state[f"pa_audit_exp_{_akb}"] = None
                        with st.expander("✏️ Edit purchase values before accepting", expanded=False):
                            e0, e1, e2, e3 = st.columns([2, 1, 1, 1])
                            e0.text_input("Invoice / Challan No", key=f"pa_audit_doc_{_akb}")
                            e1.date_input("Date", key=f"pa_audit_date_{_akb}", format="DD/MM/YYYY")
                            e2.number_input("Qty", min_value=0.0, step=1.0, key=f"pa_audit_qty_{_akb}")
                            e3.number_input("Rate ₹/pc", min_value=0.0, step=0.5, format="%.2f", key=f"pa_audit_price_{_akb}")
                            e4, e5, e6 = st.columns([1, 2, 2])
                            e4.number_input("GST %", min_value=0.0, step=0.5, format="%.2f", key=f"pa_audit_gst_{_akb}")
                            e5.text_input("Batch", key=f"pa_audit_batch_{_akb}")
                            e6.date_input("Expiry", value=st.session_state.get(f"pa_audit_exp_{_akb}"), key=f"pa_audit_exp_{_akb}", format="DD/MM/YYYY")
                            _preview_qty = float(st.session_state.get(f"pa_audit_qty_{_akb}", 0) or 0)
                            _preview_rate = float(st.session_state.get(f"pa_audit_price_{_akb}", 0) or 0)
                            _preview_gst = float(st.session_state.get(f"pa_audit_gst_{_akb}", 0) or 0)
                            _preview_taxable = round(_preview_qty * _preview_rate, 2)
                            _preview_gst_amt = round(_preview_taxable * _preview_gst / 100, 2)
                            st.caption(
                                f"Taxable ₹{_preview_taxable:,.2f} · "
                                f"GST ₹{_preview_gst_amt:,.2f} · "
                                f"Total ₹{_preview_taxable + _preview_gst_amt:,.2f}"
                            )
                        _cands = _candidate_order_lines_for_pa(_ar)
                        if _cands:
                            st.caption("Possible order link by product + power:")
                            _cand_opts = [""] + [c["line_id"] for c in _cands]
                            _cand_labels = {"": "— Select order line —"}
                            for c in _cands:
                                _pwr = _fmt_pwr(c)
                                _cand_labels[c["line_id"]] = (
                                    f"{c.get('order_no')} · {str(c.get('eye_side') or '').upper()} · "
                                    f"{c.get('product_name')} · {_pwr}"
                                )
                            _sel_cand = st.selectbox(
                                "Link candidate",
                                _cand_opts,
                                key=f"pa_link_candidate_{_pa}",
                                format_func=lambda x: _cand_labels.get(x, x),
                                label_visibility="collapsed",
                            )
                        else:
                            _sel_cand = ""
                            st.caption("No exact order-line candidate found by product + power.")
                        c_a1, c_a2, c_a3, c_a4 = st.columns([2, 2, 2, 4])
                        _supplier_ref = c_a4.text_input(
                            "Supplier order / job ref",
                            value=_ar.get("supplier_order_ref") or "",
                            key=f"pa_supplier_ref_{_pa}",
                            placeholder="Supplier order no from invoice",
                        )
                        _note = c_a4.text_input(
                            "Audit note",
                            key=f"pa_audit_note_{_pa}",
                            placeholder="Reason / owner note",
                            label_visibility="collapsed",
                        )
                        c_a4.caption("Edits above are saved automatically when you Accept, Link, or Flag.")
                        if c_a1.button("✅ Accept as Inventory", key=f"pa_audit_accept_{_pa}", use_container_width=True):
                            _save_audit_pa_details(_pa, _supplier_ref, _note)
                            _inv_ok = True
                            if not _ar.get("inventory_posted_at"):
                                _inv_ok = _pa_post_inventory_from_register(_pa)
                            if _inv_ok and _pa_audit_transition(_pa, "AUDITED_INVENTORY", "AUDITED_INVENTORY", _note):
                                st.success("Marked as audited inventory.")
                                st.cache_data.clear()
                                st.rerun()
                        if c_a2.button("🔗 Link Order", key=f"pa_audit_link_{_pa}", use_container_width=True, disabled=not bool(_sel_cand)):
                            if _link_pa_to_order_line(_pa, _sel_cand, _supplier_ref, _note):
                                st.success("Linked to order line and procurement cleared.")
                                st.cache_data.clear()
                                st.rerun()
                        if c_a3.button("🚩 Flag", key=f"pa_audit_flag_{_pa}", use_container_width=True):
                            if _pa_audit_transition(_pa, "FLAGGED_REVIEW", "FLAGGED_REVIEW", _note):
                                st.warning("Flagged for owner review.")
                                st.cache_data.clear()
                                st.rerun()
                st.markdown("---")

        # ── Supplier-Product Procurement Report (CSV) ────────────────────────────
        # What was procured, as the SUPPLIER's product, with our product aliased —
        # for supplier reconciliation / OCR matching.
        if pa_rows:
            import csv as _csv, io as _io
            _buf = _io.StringIO()
            _wr = _csv.writer(_buf)
            _wr.writerow([
                "Order No", "Eye", "Supplier", "Supplier Product",
                "Supplier Code", "Supplier Order Ref", "Supplier Description",
                "Our Product (alias)", "Mapping Source",
                "Challan No", "Invoice No", "Doc Date",
                "Qty", "Price/pc", "Total",
            ])
            for r in pa_rows:
                _wr.writerow([
                    r.get("order_no", ""), str(r.get("eye_side", "")).upper(),
                    r.get("supplier_name", ""),
                    r.get("supplier_product_name", ""),
                    r.get("supplier_product_code", ""),
                    r.get("supplier_order_ref", ""),
                    r.get("supplier_product_description", ""),
                    r.get("our_product_name", "") or r.get("product_name", ""),
                    r.get("mapping_source", ""),
                    r.get("challan_no", ""), r.get("invoice_no", ""),
                    r.get("doc_date", ""),
                    int(r.get("qty", 1) or 1),
                    f"{float(r.get('purchase_price', 0) or 0):.2f}",
                    f"{float(r.get('total_value', 0) or 0):.2f}",
                ])
            st.download_button(
                "📊 Download Supplier-Product Procurement Report (CSV)",
                data=_buf.getvalue().encode("utf-8"),
                file_name=f"procurement_supplier_report_{_dfrom}_{_dto}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            st.caption(
                "Report lists each procured line by the **supplier's** product "
                "name (with our product as the alias) — for supplier invoice "
                "reconciliation."
            )
            st.markdown("---")

        # ── PA section — grouped by supplier → challan ───────────────────────────
        if pa_rows:
            st.markdown("#### 📋 Challans & 🧾 Invoices")

            # ── Detect PA rows with invoice_no set but no purchase_invoices record ─
            # This happens when invoice numbers were typed manually in the Edit form
            # before the auto-upsert logic existed. The register is empty but the
            # picker won't offer them. Detect and offer a one-click sync.
            _unsynced = _q("""
                SELECT
                    pa.invoice_no,
                    pa.supplier_name,
                    pa.supplier_id::text        AS supplier_id,
                    MAX(pa.document_date)       AS doc_date,
                    COUNT(*)                    AS line_count,
                    SUM(pa.received_qty)        AS total_qty,
                    ROUND(SUM(
                        COALESCE(pa.purchase_price,0)
                        * COALESCE(pa.received_qty,1)
                    ), 2)                       AS subtotal,
                    ROUND(SUM(
                        COALESCE(
                            NULLIF(pa.courier_gst_amount,0),
                            COALESCE(pa.purchase_price,0)
                            * COALESCE(pa.received_qty,1)
                            * COALESCE(ol.gst_percent, p.gst_percent, 18) / 100.0
                        )
                    ), 2)                       AS gst_amount
                FROM purchase_acknowledgements pa
                LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
                LEFT JOIN products p     ON p.id  = COALESCE(pa.our_product_id, pa.product_id, ol.product_id)
                LEFT JOIN purchase_invoices pi ON LOWER(pi.invoice_no) = LOWER(pa.invoice_no)
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS line_count
                    FROM purchase_invoice_lines pil
                    WHERE LOWER(pil.invoice_no) = LOWER(pa.invoice_no)
                ) pilx ON TRUE
                WHERE (pa.invoice_no IS NOT NULL AND pa.invoice_no != '')
                  AND (
                      pi.invoice_no IS NULL
                      OR COALESCE(pilx.line_count, 0) = 0
                  )
                GROUP BY pa.invoice_no, pa.supplier_name, pa.supplier_id
                ORDER BY pa.invoice_no
            """)

            if _unsynced:
                _us_total = sum(
                    float(u.get("subtotal",0)) + float(u.get("gst_amount",0))
                    for u in _unsynced
                )
                st.markdown(
                    f"<div style='background:#1a0a00;border:1px solid #92400e;"
                    f"border-left:4px solid #f59e0b;border-radius:8px;"
                    f"padding:10px 14px;margin-bottom:10px'>"
                    f"<b style='color:#fbbf24'>⚠️ {len(_unsynced)} invoice(s) not yet in Register</b>"
                    f"<div style='color:#94a3b8;font-size:0.78rem;margin-top:4px'>"
                    f"These invoice numbers were entered manually but never posted to the "
                    f"accounts register. Total: ₹{_us_total:,.2f}. "
                    f"Click <b>Sync All to Register</b> to post them now.</div>"
                    f"<div style='margin-top:6px;font-size:0.75rem;color:#64748b'>"
                    + "  ·  ".join(
                        f"<b style='color:#e2e8f0'>{u['invoice_no']}</b> "
                        f"({u['supplier_name']} · {u['line_count']} line(s) · "
                        f"₹{float(u['subtotal'])+float(u['gst_amount']):,.2f})"
                        for u in _unsynced
                    )
                    + "</div></div>",
                    unsafe_allow_html=True,
                )
                if st.button(
                    f"📤 Sync {len(_unsynced)} Invoice(s) to Register  ·  ₹{_us_total:,.2f}",
                    key="pr_sync_unsynced", type="primary", use_container_width=True,
                ):
                    _sync_ok = 0
                    _sync_fail = []
                    try:
                        from modules.sql_adapter import get_transaction_connection
                        import datetime as _dt2
                        _sc = get_transaction_connection()
                        try:
                            with _sc.cursor() as _cur:
                                for _u in _unsynced:
                                    _inv   = _u["invoice_no"]
                                    _sub   = float(_u.get("subtotal") or 0)
                                    _gst   = float(_u.get("gst_amount") or 0)
                                    _tot   = round(_sub + _gst, 2)
                                    _idate = _u.get("doc_date") or _dt2.date.today()
                                    _cur.execute("""
                                        INSERT INTO purchase_invoices (
                                            invoice_no, supplier_order_id,
                                            supplier_id, supplier_name,
                                            supplier_invoice_no, invoice_date,
                                            total_items, total_qty_received,
                                            subtotal, gst_amount, invoice_total,
                                            payment_terms, payment_status,
                                            notes, created_by, created_at, updated_at
                                        ) VALUES (
                                            %(inv)s, %(inv)s,
                                            %(sid)s, %(sname)s,
                                            %(inv)s, %(idate)s,
                                            %(items)s, %(qty)s,
                                            %(sub)s, %(gst)s, %(tot)s,
                                            'NET30', 'UNPAID',
                                            'Synced from manually-entered PA invoice_no',
                                            'manual_sync', NOW(), NOW()
                                        )
                                        ON CONFLICT (invoice_no) DO UPDATE SET
                                            supplier_order_id   = EXCLUDED.supplier_order_id,
                                            supplier_id         = EXCLUDED.supplier_id,
                                            supplier_name       = EXCLUDED.supplier_name,
                                            supplier_invoice_no = EXCLUDED.supplier_invoice_no,
                                            invoice_date        = EXCLUDED.invoice_date,
                                            total_items         = EXCLUDED.total_items,
                                            total_qty_received  = EXCLUDED.total_qty_received,
                                            subtotal            = EXCLUDED.subtotal,
                                            gst_amount          = EXCLUDED.gst_amount,
                                            invoice_total       = EXCLUDED.invoice_total,
                                            payment_status      = CASE
                                                WHEN purchase_invoices.payment_status = 'PAID'
                                                THEN purchase_invoices.payment_status
                                                ELSE EXCLUDED.payment_status
                                            END,
                                            notes               = EXCLUDED.notes,
                                            updated_at          = NOW()
                                    """, {
                                        "inv":   _inv,
                                        "sid":   _u.get("supplier_id"),
                                        "sname": _u.get("supplier_name",""),
                                        "idate": str(_idate)[:10],
                                        "items": int(_u.get("line_count") or 0),
                                        "qty":   int(_u.get("total_qty") or 0),
                                        "sub":   _sub,
                                        "gst":   _gst,
                                        "tot":   _tot,
                                    })
                                    _cur.execute("""
                                        DELETE FROM purchase_invoice_lines
                                        WHERE LOWER(invoice_no) = LOWER(%(inv)s)
                                    """, {"inv": _inv})
                                    _cur.execute("""
                                        INSERT INTO purchase_invoice_lines (
                                            invoice_no, item_no,
                                            supplier_order_id, supplier_order_item_no,
                                            product_id, product_name, brand,
                                            eye_side, sph, cyl, axis, add_power,
                                            batch_no, expiry_date,
                                            ordered_qty, received_qty,
                                            actual_price, gst_percent, line_total,
                                            created_at
                                        )
                                        SELECT
                                            pa.invoice_no,
                                            ROW_NUMBER() OVER (
                                                ORDER BY pa.acknowledged_at, pa.id
                                            )::int AS item_no,
                                            COALESCE(NULLIF(pa.challan_no,''), pa.invoice_no) AS supplier_order_id,
                                            ROW_NUMBER() OVER (
                                                ORDER BY pa.acknowledged_at, pa.id
                                            )::int AS supplier_order_item_no,
                                            COALESCE(pa.our_product_id, pa.product_id, ol.product_id) AS product_id,
                                            COALESCE(pa.our_product_name, pa.product_name, p.product_name,
                                                     pa.supplier_product_name, 'Unknown Product') AS product_name,
                                            COALESCE(p.brand, '') AS brand,
                                            COALESCE(pa.eye_side, ol.eye_side, '') AS eye_side,
                                            ol.sph, ol.cyl, ol.axis, ol.add_power,
                                            pa.batch_no,
                                            pa.expiry_date,
                                            COALESCE(pa.received_qty, pa.qty, 1) AS ordered_qty,
                                            COALESCE(pa.received_qty, pa.qty, 1) AS received_qty,
                                            COALESCE(pa.purchase_price, 0) AS actual_price,
                                            COALESCE(NULLIF(pa.courier_gst_rate,0),
                                                     ol.gst_percent, p.gst_percent, 18) AS gst_percent,
                                            ROUND(
                                                COALESCE(pa.total_value,
                                                    COALESCE(pa.purchase_price,0)
                                                    * COALESCE(pa.received_qty, pa.qty, 1)
                                                )
                                                + COALESCE(
                                                    NULLIF(pa.courier_gst_amount,0),
                                                    COALESCE(pa.purchase_price,0)
                                                    * COALESCE(pa.received_qty, pa.qty, 1)
                                                    * COALESCE(NULLIF(pa.courier_gst_rate,0),
                                                               ol.gst_percent, p.gst_percent, 18) / 100.0
                                                ),
                                                2
                                            ) AS line_total,
                                            NOW()
                                        FROM purchase_acknowledgements pa
                                        LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
                                        LEFT JOIN products p ON p.id = COALESCE(pa.our_product_id, pa.product_id, ol.product_id)
                                        WHERE LOWER(pa.invoice_no) = LOWER(%(inv)s)
                                    """, {"inv": _inv})
                                    # Also stamp billing_status on all linked PA rows
                                    _cur.execute("""
                                        UPDATE purchase_acknowledgements
                                        SET billing_status = 'INVOICED'
                                        WHERE LOWER(invoice_no) = LOWER(%(inv)s)
                                          AND COALESCE(billing_status,'') != 'INVOICED'
                                    """, {"inv": _inv})
                                    _sync_ok += 1
                            _sc.commit()
                            _clear_pr_cache()
                            st.success(
                                f"✅ Purchase Register Done for {_sync_ok} invoice(s). "
                                f"Open the invoice details below or verify in Registers → Purchase Register."
                            )
                            st.rerun()
                        except Exception as _se:
                            _sc.rollback()
                            st.error(f"Sync failed (rolled back): {_se}")
                        finally:
                            _sc.close()
                    except Exception as _sce:
                        st.error(f"DB connection failed: {_sce}")

                st.markdown("---")

            # Group: supplier → purchase document no → list of PA rows.
            # Invoice Match lines may have invoice_no but no challan_no, so do
            # not display them as "(no challan no.)".
            from collections import OrderedDict as _od
            _by_sup = _od()
            for r in pa_rows:
                _s  = r.get("supplier_name", "—")
                _doc = _purchase_doc_ref(r)
                if _s not in _by_sup:
                    _by_sup[_s] = _od()
                if _doc not in _by_sup[_s]:
                    _by_sup[_s][_doc] = []
                _by_sup[_s][_doc].append(r)

            # ── Multi-challan → one invoice picker ───────────────────────────────
            # Build groups first so we only show the picker banner when there is
            # actually something un-invoiced to tick.
            _challan_groups = []
            for _sn, _challan_dict in _by_sup.items():
                for _ch, _ch_rows in _challan_dict.items():
                    _ch_display = _ch if _ch != "NO_DOC" else "(document no. pending)"
                    _doc_label = _purchase_doc_label(_ch_rows[0]) if _ch_rows else "Document"
                    _ch_val = sum(float(r.get("total_value",0)) for r in _ch_rows)
                    # Fully invoiced only if ALL rows have billing_status=INVOICED
                    # or a non-empty invoice_no. Earlier this used ANY, which hid
                    # remaining products when only part of a challan was invoiced.
                    _ch_invoiced = bool(_ch_rows) and all(
                        r.get("billing_status","").upper() == "INVOICED"
                        or bool((r.get("invoice_no") or "").strip())
                        for r in _ch_rows
                    )
                    _order_nos = list({r.get("order_no","") for r in _ch_rows if r.get("order_no")})
                    _challan_groups.append({
                        "supplier": _sn,
                        "challan":  _ch,
                        "display":  _ch_display,
                        "doc_label": _doc_label,
                        "rows":     _ch_rows,
                        "total":    _ch_val,
                        "invoiced": _ch_invoiced,
                        "orders":   _order_nos,
                    })

            _postable_groups = [g for g in _challan_groups if not g["invoiced"]]

            def _challan_pick_key(_cg, _idx):
                return f"pr_pick_{_cg['supplier'][:8]}_{_cg['challan'][:8]}_{_idx}"

            def _line_pick_key(_row):
                return f"pr_pick_line_{_row.get('pa_id')}"

            # Only show the picker banner + checkboxes if there is something to post
            if _postable_groups:
                st.markdown(
                    "<div style='background:#0a1628;border:1px solid #1e3a5f;"
                    "border-left:4px solid #f59e0b;border-radius:8px;"
                    "padding:8px 14px;margin-bottom:10px'>"
                    "<b style='color:#fbbf24'>📤 Post Challans to Invoice</b>"
                    "<span style='color:#64748b;font-size:0.75rem;margin-left:8px'>"
                    "Choose supplier → select challans or individual products → post as one purchase invoice</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )
                _postable_suppliers = []
                for _g in _postable_groups:
                    if _g["supplier"] not in _postable_suppliers:
                        _postable_suppliers.append(_g["supplier"])
                _sel_sup_post = st.selectbox(
                    "Supplier with pending challans",
                    ["ALL"] + _postable_suppliers,
                    key="pr_post_supplier_filter",
                    format_func=lambda x: "All suppliers" if x == "ALL" else x,
                )
                _visible_groups = [
                    g for g in _postable_groups
                    if _sel_sup_post == "ALL" or g["supplier"] == _sel_sup_post
                ]
                _visible_total = sum(float(g.get("total", 0) or 0) for g in _visible_groups)
                st.caption(
                    f"Showing {len(_visible_groups)} pending challan(s)"
                    + (f" for {_sel_sup_post}" if _sel_sup_post != "ALL" else "")
                    + f" · ₹{_visible_total:,.2f}"
                )
                st.caption(
                    "Tick the challan checkbox to post the full challan, or open Products and tick only the lines to invoice."
                )
                _pick_actions = st.columns([1.4, 1.4, 1.4, 3])
                if _pick_actions[0].button("☑ Select Visible Challans", key="pr_pick_all_postable"):
                    for _i_all, _cg_all in enumerate(_challan_groups):
                        if (not _cg_all["invoiced"]) and _cg_all in _visible_groups:
                            st.session_state[_challan_pick_key(_cg_all, _i_all)] = True
                    st.rerun()
                if _pick_actions[1].button("☑ Select Visible Products", key="pr_pick_all_lines"):
                    for _cg_all in _visible_groups:
                        for _row_all in _cg_all.get("rows", []):
                            st.session_state[_line_pick_key(_row_all)] = True
                    st.rerun()
                if _pick_actions[2].button("☐ Clear Visible", key="pr_clear_all_postable"):
                    for _i_all, _cg_all in enumerate(_challan_groups):
                        if (not _cg_all["invoiced"]) and _cg_all in _visible_groups:
                            st.session_state.pop(_challan_pick_key(_cg_all, _i_all), None)
                            for _row_all in _cg_all.get("rows", []):
                                st.session_state.pop(_line_pick_key(_row_all), None)
                    st.rerun()

            # Checkboxes for each un-invoiced challan
            _selected_groups = []
            import html as _pr_html
            _display_groups = locals().get("_visible_groups", _postable_groups)
            for _i, _cg in enumerate(_challan_groups):
                if _cg["invoiced"]:
                    continue  # already posted — skip in picker
                if _cg not in _display_groups:
                    continue
                _ck_key = _challan_pick_key(_cg, _i)
                _pick_col, _info_col = st.columns([0.4, 6])
                _checked = _pick_col.checkbox("", key=_ck_key, label_visibility="collapsed")
                _info_col.markdown(
                    f"<div style='background:#f8fafc;border:1px solid #cbd5e1;"
                    f"border-left:4px solid #2563eb;border-radius:8px;"
                    f"font-size:0.8rem;padding:8px 10px;margin:6px 0 2px'>"
                    f"<b style='color:#111827'>{_pr_html.escape(str(_cg['supplier']))}</b>"
                    f" · {_pr_html.escape(str(_cg.get('doc_label') or 'Document'))} "
                    f"<b style='color:#1d4ed8'>{_pr_html.escape(str(_cg['display']))}</b>"
                    f" · {len(_cg['rows'])} line(s)"
                    f" · <b style='color:#047857'>₹{_cg['total']:,.2f}</b>"
                    + (f" · Orders: {', '.join(_cg['orders'])}" if _cg['orders'] else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )

                _picked_rows = []
                with st.expander(
                    f"Products in {_cg.get('doc_label') or 'Document'} {_cg['display']} ({len(_cg['rows'])} line(s))",
                    expanded=(len(_cg.get("rows", [])) <= 4),
                ):
                    st.caption("Use line ticks when only selected products from this challan should move to the invoice.")
                    for _rline in _cg.get("rows", []):
                        _line_key = _line_pick_key(_rline)
                        _lc_a, _lc_b = st.columns([0.45, 6])
                        _line_checked = _lc_a.checkbox("", key=_line_key, label_visibility="collapsed")
                        _line_label = (
                            f"{_rline.get('order_no','')} · {str(_rline.get('eye_side','')).upper()} · "
                            f"{_rline.get('product_name','')} · {_fmt_pwr(_rline)} · "
                            f"₹{float(_rline.get('total_value') or 0):,.2f}"
                        )
                        _lc_b.markdown(
                            f"<div style='font-size:0.8rem;color:#111827;font-weight:700;padding-top:4px'>"
                            f"{_pr_html.escape(_line_label)}</div>",
                            unsafe_allow_html=True,
                        )
                        if _line_checked:
                            _picked_rows.append(_rline)

                if _checked:
                    _selected_groups.append(_cg)
                elif _picked_rows:
                    _selected_groups.append({
                        **_cg,
                        "rows": _picked_rows,
                        "total": sum(float(x.get("total_value", 0) or 0) for x in _picked_rows),
                        "orders": list({x.get("order_no","") for x in _picked_rows if x.get("order_no")}),
                    })

            if _selected_groups:
                _post_total = sum(g["total"] for g in _selected_groups)
                _post_lines = sum(len(g["rows"]) for g in _selected_groups)
                _all_orders = list({o for g in _selected_groups for o in g["orders"]})

                # Invoice number/date fields
                _inv_sup_col, _inv_col1, _inv_col_date, _inv_col2 = st.columns([2.6, 3, 1.4, 1])
                _suppliers_post = _load_suppliers_for_purchase_register()
                _post_sup_ids = [""] + [str(s.get("id") or "") for s in _suppliers_post]
                _post_sup_names = {"": "Use selected challan supplier"}
                _post_sup_names.update({str(s.get("id") or ""): str(s.get("party_name") or "") for s in _suppliers_post})
                _selected_supplier_names = [g.get("supplier","") for g in _selected_groups if g.get("supplier")]
                _default_supplier_name = _selected_supplier_names[0] if _selected_supplier_names else ""
                _default_supplier_id = next(
                    (
                        str(s.get("id") or "")
                        for s in _suppliers_post
                        if str(s.get("party_name") or "").strip().lower()
                        == str(_default_supplier_name).strip().lower()
                    ),
                    "",
                )
                if "pr_post_supplier_override" not in st.session_state:
                    st.session_state["pr_post_supplier_override"] = (
                        _default_supplier_id if _default_supplier_id in _post_sup_ids else ""
                    )
                _post_supplier_id = _inv_sup_col.selectbox(
                    "Supplier / Creditor on invoice",
                    _post_sup_ids,
                    key="pr_post_supplier_override",
                    format_func=lambda x: _post_sup_names.get(x, x),
                    help="Correct the creditor before moving selected challans/products to Registers.",
                )
                _post_supplier_name = (
                    _post_sup_names.get(_post_supplier_id, "")
                    if _post_supplier_id else _default_supplier_name
                )
                _custom_inv_no = _inv_col1.text_input(
                    "Supplier Invoice No.",
                    placeholder="Enter supplier invoice no. printed on bill",
                    key="pr_custom_inv_no",
                )
                _default_inv_date = max(
                    (
                        r.get("doc_date")
                        for g in _selected_groups
                        for r in g.get("rows", [])
                        if r.get("doc_date")
                    ),
                    default=None,
                )
                try:
                    _default_inv_date = datetime.date.fromisoformat(str(_default_inv_date)[:10]) if _default_inv_date else datetime.date.today()
                except Exception:
                    _default_inv_date = datetime.date.today()
                _custom_inv_date = _inv_col_date.date_input(
                    "Invoice Date",
                    value=_default_inv_date,
                    key="pr_custom_inv_date",
                    format="DD/MM/YYYY",
                )
                _inv_ready = bool(_custom_inv_no.strip())
                if not _inv_ready:
                    _inv_col1.caption("Required. Challan will not move to Register without supplier invoice number.")
                else:
                    _inv_col1.caption("If this unpaid invoice already exists, selected challans will be added to it.")

                _post_label = (
                    f"📤 Post {_post_lines} line(s) · ₹{_post_total:,.2f} "
                    f"from {len(_selected_groups)} challan(s) → 1 Invoice"
                )
                if _inv_col2.button(_post_label, key="pr_post_multi",
                                     type="primary", use_container_width=True,
                                     disabled=not _inv_ready):
                    from modules.core.date_guard import validate_not_future
                    _ok_dt, _msg_dt = validate_not_future(_custom_inv_date, "Purchase invoice date")
                    if not _ok_dt:
                        st.error(_msg_dt)
                        return
                    try:
                        from modules.procurement.purchase_invoice import (
                            convert_acknowledgements_to_invoice_multi
                        )
                        _result = convert_acknowledgements_to_invoice_multi(
                            order_nos     = _all_orders,
                            challan_groups = _selected_groups,
                            custom_inv_no  = _custom_inv_no.strip() or None,
                            invoice_date   = _custom_inv_date,
                            supplier_id    = _post_supplier_id or None,
                            supplier_name  = _post_supplier_name or None,
                        )
                        if _result["ok"]:
                            st.success(
                                f"{_result['message']} · ✅ Purchase Register Done"
                            )
                            _clear_pr_cache()
                            # Clear picker state
                            for _i2, _cg2 in enumerate(_challan_groups):
                                _k2 = f"pr_pick_{_cg2['supplier'][:8]}_{_cg2['challan'][:8]}_{_i2}"
                                st.session_state.pop(_k2, None)
                                for _r2 in _cg2.get("rows", []):
                                    st.session_state.pop(f"pr_pick_line_{_r2.get('pa_id')}", None)
                            st.session_state.pop("pr_custom_inv_no", None)
                            st.session_state.pop("pr_custom_inv_date", None)
                            st.session_state.pop("pr_post_supplier_override", None)
                            st.rerun()
                        else:
                            st.error(_result["message"])
                    except Exception as _pe:
                        st.error(f"Post failed: {_pe}")

            st.markdown("---")

            # ── Pendency summary banner ───────────────────────────────────────────
            _total_groups   = len(_challan_groups)
            _pending_post   = len(_postable_groups)                          # challan, not yet invoiced
            _pending_sync   = len([g for g in _challan_groups              # invoiced but not in register
                                   if g["invoiced"]
                                   and not all(bool(r.get("in_register")) for r in g["rows"]
                                               if bool((r.get("invoice_no") or "").strip()))])
            _fully_done     = _total_groups - _pending_post - _pending_sync

            if _pending_post == 0 and _pending_sync == 0:
                st.markdown(
                    "<div style='background:#052e16;border:1px solid #166534;"
                    "border-left:4px solid #22c55e;border-radius:8px;"
                    "padding:10px 14px;margin-bottom:12px'>"
                    "<b style='color:#86efac'>✅ No Pendency — All invoices posted to Register</b>"
                    "<div style='color:#4ade80;font-size:0.75rem;margin-top:3px'>"
                    f"All {_total_groups} challan group(s) are invoiced and visible in "
                    "Registers → Purchase Register. Nothing pending.</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )
            else:
                _parts = []
                if _pending_post:
                    _parts.append(f"<b style='color:#f87171'>{_pending_post} challan(s) not yet invoiced</b>")
                if _pending_sync:
                    _parts.append(f"<b style='color:#fbbf24'>{_pending_sync} invoice(s) not yet in Register</b>")
                if _fully_done:
                    _parts.append(f"<span style='color:#86efac'>{_fully_done} fully done</span>")
                st.markdown(
                    "<div style='background:#0a1628;border:1px solid #1e3a5f;"
                    "border-left:4px solid #f59e0b;border-radius:8px;"
                    "padding:8px 14px;margin-bottom:12px'>"
                    "<b style='color:#fbbf24'>📊 Pendency Summary</b>"
                    f"<div style='margin-top:4px;font-size:0.78rem'>"
                    + "  ·  ".join(_parts)
                    + "</div></div>",
                    unsafe_allow_html=True,
                )

            # ── Display: grouped by supplier → document card ─────────────────────
            for _sn, _challan_dict in _by_sup.items():
                _sup_total = sum(
                    float(r.get("total_value",0))
                    for _ch_rows in _challan_dict.values()
                    for r in _ch_rows
                )
                _sup_challan_count = len(_challan_dict)
                with st.expander(
                    f"🏭 {_sn}  ·  {_sup_challan_count} document(s)  ·  ₹{_sup_total:,.0f}",
                    expanded=len(_by_sup) <= 2
                ):
                    for _ch, _ch_rows in _challan_dict.items():
                        _ch_val = sum(float(r.get("total_value",0)) for r in _ch_rows)
                        _ch_display = _ch if _ch != "NO_DOC" else "(document no. pending)"
                        _doc_label = _purchase_doc_label(_ch_rows[0]) if _ch_rows else "Document"
                        _ch_invoiced = bool(_ch_rows) and all(
                            r.get("billing_status","").upper() == "INVOICED"
                            or bool((r.get("invoice_no") or "").strip())
                            for r in _ch_rows
                        )
                        _ch_in_reg = _ch_invoiced and all(
                            bool(r.get("in_register")) for r in _ch_rows
                            if bool((r.get("invoice_no") or "").strip())
                        )
                        if _ch_in_reg:
                            _status_icon = "✅ Purchase Register Done"
                            _status_col  = "#22c55e"
                        elif _ch_invoiced:
                            _status_icon = "⚠️ Invoice not in Register"
                            _status_col  = "#f59e0b"
                        else:
                            _status_icon = f"📋 {_doc_label}"
                            _status_col  = "#3b82f6"

                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid #1e293b;"
                            f"border-left:3px solid {_status_col};border-radius:5px;"
                            f"padding:6px 12px;margin:4px 0'>"
                            f"<span style='color:{_status_col};font-weight:700;font-size:0.82rem'>"
                            f"{_status_icon}</span>"
                            f" <b style='color:#e2e8f0'>{_ch_display}</b>"
                            f" · {len(_ch_rows)} line(s) · "
                            f"<b style='color:#10b981'>₹{_ch_val:,.2f}</b>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                        for r in _ch_rows:
                            _render_pa_card(r)

            st.markdown("---")

        # ── PO audit/history section ──────────────────────────────────────────────
        if po_rows:
            from collections import OrderedDict as _od2
            _by_sup2 = _od2()
            for r in po_rows:
                s = r.get("supplier_name","—")
                if s not in _by_sup2: _by_sup2[s] = []
                _by_sup2[s].append(r)

            _po_total = sum(float(r.get("total_value", 0) or 0) for r in po_rows)
            with st.expander(
                f"📤 Supplier PO History / Audit · {len(po_rows)} PO(s) · ₹{_po_total:,.0f}",
                expanded=(_dtype == "PO"),
            ):
                st.caption(
                    "Purchase orders are audit/history records. Daily purchase-register posting should be done from the Pendency Summary above."
                )
                for sn, srows in _by_sup2.items():
                    _sv = sum(float(r.get("total_value",0)) for r in srows)
                    with st.expander(
                        f"🏭 {sn}  ·  {len(srows)} PO(s)  ·  ₹{_sv:,.0f}",
                        expanded=False,
                    ):
                        for r in srows:
                            _render_po_card(r)
            st.markdown("---")

        # ── GRN/register audit section ───────────────────────────────────────────
        if grn_rows:
            from collections import OrderedDict as _od3
            _hidden_void_grns = len(grn_rows) - len(visible_grn_rows)
            _by_sup3 = _od3()
            for r in visible_grn_rows:
                s = r.get("supplier_name","—")
                if s not in _by_sup3: _by_sup3[s] = []
                _by_sup3[s].append(r)

            _grn_total = sum(float(r.get("total_value", 0) or 0) for r in visible_grn_rows)
            with st.expander(
                f"🏪 GRN / Register Audit · {len(visible_grn_rows)} active record(s) · ₹{_grn_total:,.0f}",
                expanded=(_dtype == "GRN"),
            ):
                if _hidden_void_grns:
                    st.caption(f"Hidden from main view: {_hidden_void_grns} voided zero-value GRN record(s).")
                if not visible_grn_rows:
                    st.info("No active GRN/register audit records for the selected filters.")
                for sn, srows in _by_sup3.items():
                    _sv = sum(float(r.get("total_value",0)) for r in srows)
                    with st.expander(
                        f"🏭 {sn}  ·  {len(srows)} GRN(s)  ·  ₹{_sv:,.0f}",
                        expanded=False,
                    ):
                        for r in srows:
                            _render_grn_card(r)

        # ── Blank Replenishment POs / Invoices ───────────────────────────────────
        try:
            _blank_pos = _q("""
                SELECT
                    bro.id::text                        AS po_id,
                    COALESCE(p.party_name,'Unknown')    AS supplier,
                    bro.order_date::date                AS order_date,
                    bro.status,
                    COALESCE(bro.remarks,'')            AS remarks,
                    COALESCE(bri_agg.inv_count, 0)      AS invoice_count,
                    COALESCE(bri_agg.total_invoiced, 0) AS total_invoiced,
                    COALESCE(bri_agg.total_gst, 0)      AS total_gst,
                    bri_agg.last_invoice_no,
                    bri_agg.last_invoice_date
                FROM blank_replenishment_orders bro
                LEFT JOIN parties p ON p.id = bro.supplier_id
                LEFT JOIN (
                    SELECT
                        po_id,
                        COUNT(*)                         AS inv_count,
                        SUM(invoice_amount)              AS total_invoiced,
                        SUM(gst_amount)                  AS total_gst,
                        MAX(invoice_no)                  AS last_invoice_no,
                        MAX(invoice_date)                AS last_invoice_date
                    FROM blank_replenishment_invoices
                    GROUP BY po_id
                ) bri_agg ON bri_agg.po_id = bro.id
                WHERE bro.order_date::date BETWEEN %(df)s AND %(dt)s
                  AND (
                      %(sup)s = '' OR LOWER(COALESCE(p.party_name,'')) LIKE %(sup_like)s
                  )
                ORDER BY bro.order_date DESC
                LIMIT 100
            """, {
                "df":       str(_dfrom),
                "dt":       str(_dto),
                "sup":      (_sup or "").strip(),
                "sup_like": f"%{(_sup or '').lower().strip()}%",
            })
        except Exception:
            _blank_pos = []

        if _blank_pos:
            _bp_total = sum(float(r["total_invoiced"] or 0) for r in _blank_pos)
            _bp_open  = sum(1 for r in _blank_pos if r["status"] in ("SENT","PARTIALLY_RECEIVED"))
            with st.expander(
                f"🧫 Blank Replenishment POs · {len(_blank_pos)} PO(s) · "
                f"{_bp_open} open · ₹{_bp_total:,.0f} invoiced",
                expanded=False,
            ):
                st.caption("Blank lens POs raised from 🧫 Blank Repl. tab. "
                           "For full detail go to Production → Blank Repl.")
                for _bp in _blank_pos:
                    _sts = _bp["status"]
                    _sts_color = {
                        "SENT":                "#f59e0b",
                        "PARTIALLY_RECEIVED":  "#3b82f6",
                        "RECEIVED":            "#22c55e",
                    }.get(_sts, "#64748b")
                    _inv_str = ""
                    if _bp["invoice_count"]:
                        _inv_str = (
                            f" · Inv: {_bp['last_invoice_no']} "
                            f"({_bp['last_invoice_date']}) "
                            f"· ₹{float(_bp['total_invoiced'] or 0):,.0f}"
                            + (f" + GST ₹{float(_bp['total_gst'] or 0):,.0f}" if float(_bp['total_gst'] or 0) > 0 else "")
                        )
                    st.markdown(
                        f"<div style='background:#0a1628;border-left:3px solid {_sts_color};"
                        f"border-radius:4px;padding:6px 12px;margin-bottom:4px;"
                        f"display:flex;justify-content:space-between;align-items:center'>"
                        f"<div>"
                        f"<span style='color:#e2e8f0;font-weight:700'>{_bp['supplier']}</span>"
                        f"<span style='color:#475569;font-size:0.75rem'>"
                        f" · {_bp['order_date']}"
                        f"{(' · ' + _bp['remarks'][:35]) if _bp['remarks'] else ''}"
                        f"{_inv_str}</span>"
                        f"</div>"
                        f"<span style='color:{_sts_color};font-size:0.7rem;font-weight:700'>{_sts}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
