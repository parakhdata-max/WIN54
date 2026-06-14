"""
Admin document edit panel for challan/invoice price corrections.

This module backs the "Admin Edit" tab in app.py. It intentionally keeps the
surface narrow: admin/manager users can adjust line unit price and discount on
pending challans and unpaid invoices, then totals are recalculated in one DB
transaction. Paid invoices remain correction-note territory.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from modules.sql_adapter import execute_query, run_transaction
from modules.security.roles import ADMIN, MANAGER, current_user_name, require_role


def _money(value: Any) -> float:
    try:
        return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return round(float(value or 0), 2)


def _rows(sql: str, params: dict | None = None) -> List[Dict[str, Any]]:
    df = execute_query(sql, "invoice_edit_admin", params or {})
    if df is None or df.empty:
        return []
    return df.where(pd.notnull(df), None).to_dict("records")


def _one(sql: str, params: dict | None = None) -> Dict[str, Any] | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def _fmt(value: Any) -> str:
    return f"Rs. {_money(value):,.2f}"


def _table_columns(table_name: str) -> set[str]:
    try:
        df = execute_query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %(table)s
            """,
            "invoice_edit_admin_schema",
            {"table": table_name},
        )
        if df is None or df.empty:
            return set()
        return {str(v) for v in df["column_name"].tolist()}
    except Exception:
        return set()


def _external_posted_flags(doc_type: str, doc_id: str) -> List[str]:
    """Return external posting flags that lock invoice edits."""
    if doc_type != "Invoice":
        return []
    candidates = [
        "portal_posted",
        "portal_synced",
        "gst_portal_posted",
        "gst_portal_synced",
        "einvoice_posted",
        "einvoice_synced",
        "tally_synced",
    ]
    existing = [c for c in candidates if c in _table_columns("invoices")]
    if not existing:
        return []
    select_bits = ", ".join(f"COALESCE({c}, FALSE) AS {c}" for c in existing)
    row = _one(f"SELECT {select_bits} FROM invoices WHERE id = %(id)s::uuid", {"id": doc_id})
    if not row:
        return []
    return [c for c in existing if bool(row.get(c))]


def _document_search() -> tuple[str, Dict[str, Any] | None]:
    st.subheader("Admin Price Edit")
    st.caption("Restricted correction panel for challan/invoice line values before final payment closure.")

    c1, c2, c3 = st.columns([1.1, 2, 1])
    with c1:
        doc_type = st.radio("Document", ["Invoice", "Challan"], horizontal=True, key="iea_doc_type")
    with c2:
        search = st.text_input(
            "Search",
            placeholder="Invoice/challan no, party name, mobile",
            key="iea_search",
        ).strip()
    with c3:
        limit = int(st.number_input("Rows", min_value=5, max_value=50, value=15, step=5, key="iea_limit"))

    if not search:
        st.info("Enter a document number or party detail to load editable documents.")
        return doc_type, None

    like = f"%{search}%"
    if doc_type == "Invoice":
        docs = _rows(
            """
            SELECT
                i.id::text,
                i.challan_id::text AS challan_id,
                i.invoice_no AS doc_no,
                i.invoice_date AS doc_date,
                COALESCE(p.party_name, i.remarks, 'Unknown') AS party_name,
                COALESCE(p.mobile, p.whatsapp, '') AS mobile,
                COALESCE(i.status, '') AS status,
                COALESCE(i.payment_status, '') AS payment_status,
                COALESCE(i.total_amount, 0) AS total_amount,
                COALESCE(i.total_tax, 0) AS total_tax,
                COALESCE(i.grand_total, 0) AS grand_total,
                COALESCE(i.amount_paid, 0) AS amount_paid,
                COALESCE(i.balance_due, 0) AS balance_due
            FROM invoices i
            LEFT JOIN parties p ON p.id = i.party_id
            WHERE COALESCE(i.is_deleted, FALSE) = FALSE
              AND (
                    i.invoice_no ILIKE %(q)s
                 OR COALESCE(p.party_name, '') ILIKE %(q)s
                 OR COALESCE(p.mobile, '') ILIKE %(q)s
                 OR COALESCE(p.whatsapp, '') ILIKE %(q)s
              )
            ORDER BY i.invoice_date DESC, i.created_at DESC
            LIMIT %(limit)s
            """,
            {"q": like, "limit": limit},
        )
    else:
        docs = _rows(
            """
            SELECT
                c.id::text,
                c.challan_no AS doc_no,
                c.challan_date AS doc_date,
                COALESCE(p.party_name, c.remarks, 'Unknown') AS party_name,
                COALESCE(p.mobile, p.whatsapp, '') AS mobile,
                COALESCE(c.status, '') AS status,
                '' AS payment_status,
                COALESCE(c.total_amount, 0) AS total_amount,
                COALESCE(c.total_tax, 0) AS total_tax,
                COALESCE(c.grand_total, 0) AS grand_total,
                COALESCE(c.amount_paid, 0) AS amount_paid,
                COALESCE(c.balance_due, 0) AS balance_due
            FROM challans c
            LEFT JOIN parties p ON p.id = c.party_id
            WHERE COALESCE(c.is_deleted, FALSE) = FALSE
              AND (
                    c.challan_no ILIKE %(q)s
                 OR COALESCE(p.party_name, '') ILIKE %(q)s
                 OR COALESCE(p.mobile, '') ILIKE %(q)s
                 OR COALESCE(p.whatsapp, '') ILIKE %(q)s
              )
            ORDER BY c.challan_date DESC, c.created_at DESC
            LIMIT %(limit)s
            """,
            {"q": like, "limit": limit},
        )

    if not docs:
        st.warning("No matching document found.")
        return doc_type, None

    labels = [
        f"{d['doc_no']} | {d['doc_date']} | {d['party_name']} | {_fmt(d['grand_total'])} | "
        f"{d.get('payment_status') or d.get('status')}"
        for d in docs
    ]
    picked = st.selectbox("Select document", labels, key="iea_selected_doc")
    return doc_type, docs[labels.index(picked)]


def _load_lines(doc_type: str, doc: Dict[str, Any]) -> tuple[List[Dict[str, Any]], bool, str]:
    if doc_type == "Invoice":
        lines = _rows(
            """
            SELECT
                il.id::text AS line_id,
                il.challan_line_id::text AS challan_line_id,
                il.order_line_id::text AS order_line_id,
                COALESCE(il.product_name, p.product_name, 'Line') AS product_name,
                COALESCE(il.eye_side, ol.eye_side, '') AS eye_side,
                COALESCE(il.quantity, 1) AS quantity,
                COALESCE(il.unit_price, 0) AS unit_price,
                GREATEST(0, COALESCE(ol.discount_amount,
                    ROUND(il.unit_price * il.quantity - COALESCE(il.total_price, il.line_total, 0), 2), 0)) AS discount_amount,
                COALESCE(il.total_price, il.line_total, 0) AS total_price,
                COALESCE(il.line_total, 0) AS line_total,
                COALESCE(il.gst_percent, il.tax_rate, ol.gst_percent, 0) AS gst_percent,
                COALESCE(o.order_type, 'WHOLESALE') AS order_type
            FROM invoice_lines il
            LEFT JOIN order_lines ol ON ol.id = il.order_line_id
            LEFT JOIN orders o ON o.id = il.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE il.invoice_id = %(id)s::uuid
              AND COALESCE(il.is_deleted, FALSE) = FALSE
            ORDER BY il.id
            """,
            {"id": doc["id"]},
        )
        if lines:
            return lines, False, str(doc["id"])
        if doc.get("challan_id"):
            lines = _rows(
                """
                SELECT
                    cl.id::text AS line_id,
                    NULL::text AS challan_line_id,
                    cl.order_line_id::text AS order_line_id,
                    COALESCE(cl.product_name, p.product_name, 'Line') AS product_name,
                    COALESCE(cl.eye_side, ol.eye_side, '') AS eye_side,
                    COALESCE(cl.quantity, 1) AS quantity,
                    COALESCE(cl.unit_price, 0) AS unit_price,
                    GREATEST(0, COALESCE(ol.discount_amount,
                        ROUND(cl.unit_price * cl.quantity - COALESCE(cl.total_price, cl.line_total, 0), 2), 0)) AS discount_amount,
                    COALESCE(cl.total_price, cl.line_total, 0) AS total_price,
                    COALESCE(cl.line_total, 0) AS line_total,
                    COALESCE(cl.gst_percent, ol.gst_percent, 0) AS gst_percent,
                    COALESCE(o.order_type, 'WHOLESALE') AS order_type
                FROM challan_lines cl
                LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
                LEFT JOIN orders o ON o.id = cl.order_id
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE cl.challan_id = %(id)s::uuid
                  AND COALESCE(cl.is_deleted, FALSE) = FALSE
                ORDER BY cl.id
                """,
                {"id": doc["challan_id"]},
            )
            return lines, True, str(doc["challan_id"])
        return [], False, str(doc["id"])

    lines = _rows(
        """
        SELECT
            cl.id::text AS line_id,
            NULL::text AS challan_line_id,
            cl.order_line_id::text AS order_line_id,
            COALESCE(cl.product_name, p.product_name, 'Line') AS product_name,
            COALESCE(cl.eye_side, ol.eye_side, '') AS eye_side,
            COALESCE(cl.quantity, 1) AS quantity,
            COALESCE(cl.unit_price, 0) AS unit_price,
            GREATEST(0, COALESCE(ol.discount_amount,
                ROUND(cl.unit_price * cl.quantity - COALESCE(cl.total_price, cl.line_total, 0), 2), 0)) AS discount_amount,
            COALESCE(cl.total_price, cl.line_total, 0) AS total_price,
            COALESCE(cl.line_total, 0) AS line_total,
            COALESCE(cl.gst_percent, ol.gst_percent, 0) AS gst_percent,
            COALESCE(o.order_type, 'WHOLESALE') AS order_type
        FROM challan_lines cl
        LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
        LEFT JOIN orders o ON o.id = cl.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE cl.challan_id = %(id)s::uuid
          AND COALESCE(cl.is_deleted, FALSE) = FALSE
        ORDER BY cl.id
        """,
        {"id": doc["id"]},
    )
    return lines, True, str(doc["id"])


def _calc_line(unit_price: float, qty: float, discount: float, gst: float, order_type: str) -> dict:
    gross = _money(unit_price * qty)
    discount = min(_money(discount), gross)
    taxable = _money(max(gross - discount, 0))
    if str(order_type or "").upper() == "RETAIL":
        gst_amount = _money(taxable * gst / (100 + gst)) if gst else 0.0
        line_total = taxable
    else:
        gst_amount = _money(taxable * gst / 100) if gst else 0.0
        line_total = _money(taxable + gst_amount)
    return {
        "discount_amount": discount,
        "total_price": taxable,
        "gst_amount": gst_amount,
        "line_total": line_total,
    }


def _render_line_editor(doc_type: str, doc: Dict[str, Any]) -> None:
    status = str(doc.get("status") or "").upper()
    pstatus = str(doc.get("payment_status") or "").upper()
    if status in {"CANCELLED", "VOID"}:
        st.error("This document is cancelled/void. Price edit is blocked.")
        return
    if doc_type == "Invoice" and pstatus == "PAID":
        st.warning("Paid invoice price changes must be done through Credit/Debit Note, not direct edit.")
        return
    if doc_type == "Invoice":
        external_flags = _external_posted_flags(doc_type, str(doc["id"]))
        if external_flags:
            st.error("This invoice is already posted/synced externally. Direct admin edit is blocked.")
            st.caption(f"Lock flag(s): {', '.join(external_flags)}")
            st.info("Use Credit/Debit Note or a formal reversal/correction workflow.")
            return
    if doc_type == "Challan" and status == "INVOICED":
        st.warning("This challan is already invoiced. Edit the unpaid invoice or use Credit/Debit Note.")
        return

    lines, is_challan_source, source_id = _load_lines(doc_type, doc)
    if not lines:
        st.info("No editable lines found.")
        return

    st.markdown(
        f"**{doc_type}: {doc['doc_no']}** | {doc.get('party_name') or 'Unknown'} | "
        f"Current total: **{_fmt(doc.get('grand_total'))}**"
    )
    st.caption("Edit unit price and discount. GST is shown from the original line and totals are recalculated automatically.")

    mode = st.radio("Discount input", ["Amount", "Percent"], horizontal=True, key=f"iea_disc_mode_{doc_type}_{doc['id']}")
    by_pct = mode == "Percent"

    header = st.columns([3.0, 0.7, 1.1, 1.1, 1.0, 1.2])
    for col, label in zip(header, ["Product", "Qty", "Unit", "Discount", "GST %", "New Total"]):
        col.caption(label)

    edited = []
    for idx, line in enumerate(lines):
        qty = _money(line.get("quantity") or 1)
        gst = _money(line.get("gst_percent") or 0)
        old_unit = _money(line.get("unit_price") or 0)
        old_disc = _money(line.get("discount_amount") or 0)
        old_total = _money(line.get("line_total") or 0)

        c1, c2, c3, c4, c5, c6 = st.columns([3.0, 0.7, 1.1, 1.1, 1.0, 1.2])
        with c1:
            eye = str(line.get("eye_side") or "").upper()
            st.write(f"{eye + ' - ' if eye else ''}{line.get('product_name') or 'Line'}")
        c2.write(qty)
        new_unit = _money(c3.number_input(
            "Unit",
            min_value=0.0,
            value=old_unit,
            step=0.01,
            format="%.2f",
            key=f"iea_unit_{doc_type}_{line['line_id']}",
            label_visibility="collapsed",
        ))
        gross = _money(new_unit * qty)
        if by_pct:
            old_pct = _money(old_disc / gross * 100) if gross else 0.0
            disc_pct = _money(c4.number_input(
                "Discount percent",
                min_value=0.0,
                max_value=100.0,
                value=old_pct,
                step=0.5,
                format="%.2f",
                key=f"iea_discpct_{doc_type}_{line['line_id']}",
                label_visibility="collapsed",
            ))
            new_disc = _money(gross * disc_pct / 100)
        else:
            new_disc = _money(c4.number_input(
                "Discount amount",
                min_value=0.0,
                value=old_disc,
                step=0.5,
                format="%.2f",
                key=f"iea_discamt_{doc_type}_{line['line_id']}",
                label_visibility="collapsed",
            ))
        c5.write(f"{gst:g}")
        calc = _calc_line(new_unit, qty, new_disc, gst, str(line.get("order_type") or "WHOLESALE"))
        changed = abs(new_unit - old_unit) > 0.005 or abs(calc["discount_amount"] - old_disc) > 0.005
        c6.write(_fmt(calc["line_total"]))
        if changed:
            edited.append({
                **line,
                "old_unit_price": old_unit,
                "old_discount_amount": old_disc,
                "old_line_total": old_total,
                "new_unit_price": new_unit,
                **calc,
            })

    if not edited:
        st.info("No line changes detected.")
        return

    new_total = _money(sum(x["line_total"] for x in edited) + sum(
        _money(x.get("line_total") or 0) for x in lines if x["line_id"] not in {e["line_id"] for e in edited}
    ))
    st.warning(f"{len(edited)} line(s) changed. New document total preview: {_fmt(new_total)}")
    reason = st.text_area(
        "Reason for admin edit",
        placeholder="Required for audit log",
        key=f"iea_reason_{doc_type}_{doc['id']}",
    ).strip()

    if st.button("Save Admin Edit", type="primary", use_container_width=True, disabled=not reason):
        _save_edits(doc_type, doc, edited, is_challan_source, source_id, reason)
        st.success("Admin edit saved and totals recalculated.")
        st.rerun()


def _save_edits(
    doc_type: str,
    doc: Dict[str, Any],
    edited: List[Dict[str, Any]],
    is_challan_source: bool,
    source_id: str,
    reason: str,
) -> None:
    line_table = "challan_lines" if doc_type == "Challan" or is_challan_source else "invoice_lines"
    line_id_col = "challan_id" if line_table == "challan_lines" else "invoice_id"
    steps = []

    for line in edited:
        if line_table == "invoice_lines":
            steps.append((
                """
                UPDATE invoice_lines SET
                    unit_price = %(unit_price)s,
                    total_price = %(total_price)s,
                    tax_amount = %(gst_amount)s,
                    line_total = %(line_total)s
                WHERE id = %(line_id)s::uuid
                """,
                {
                    "unit_price": line["new_unit_price"],
                    "total_price": line["total_price"],
                    "gst_amount": line["gst_amount"],
                    "line_total": line["line_total"],
                    "line_id": line["line_id"],
                },
            ))
        else:
            steps.append((
                """
                UPDATE challan_lines SET
                    unit_price = %(unit_price)s,
                    total_price = %(total_price)s,
                    line_total = %(line_total)s
                WHERE id = %(line_id)s::uuid
                """,
                {
                    "unit_price": line["new_unit_price"],
                    "total_price": line["total_price"],
                    "line_total": line["line_total"],
                    "line_id": line["line_id"],
                },
            ))

        if line.get("order_line_id"):
            steps.append((
                """
                UPDATE order_lines SET
                    unit_price = %(unit_price)s,
                    discount_amount = %(discount_amount)s,
                    total_price = %(total_price)s,
                    billing_total = %(total_price)s,
                    gst_amount = %(gst_amount)s,
                    price_overridden = TRUE,
                    override_reason = %(reason)s,
                    override_by = %(user)s,
                    override_at = NOW()
                WHERE id = %(order_line_id)s::uuid
                """,
                {
                    "unit_price": line["new_unit_price"],
                    "discount_amount": line["discount_amount"],
                    "total_price": line["total_price"],
                    "gst_amount": line["gst_amount"],
                    "reason": reason,
                    "user": current_user_name(),
                    "order_line_id": line["order_line_id"],
                },
            ))

    if doc_type == "Challan":
        steps.append((
            """
            UPDATE challans SET
                total_amount = (
                    SELECT COALESCE(SUM(total_price), 0)
                    FROM challan_lines
                    WHERE challan_id = %(doc_id)s::uuid AND COALESCE(is_deleted, FALSE) = FALSE
                ),
                grand_total = (
                    SELECT COALESCE(SUM(line_total), 0)
                    FROM challan_lines
                    WHERE challan_id = %(doc_id)s::uuid AND COALESCE(is_deleted, FALSE) = FALSE
                ),
                total_tax = (
                    SELECT COALESCE(SUM(line_total), 0) - COALESCE(SUM(total_price), 0)
                    FROM challan_lines
                    WHERE challan_id = %(doc_id)s::uuid AND COALESCE(is_deleted, FALSE) = FALSE
                ),
                balance_due = GREATEST(0, (
                    SELECT COALESCE(SUM(line_total), 0)
                    FROM challan_lines
                    WHERE challan_id = %(doc_id)s::uuid AND COALESCE(is_deleted, FALSE) = FALSE
                ) - COALESCE(amount_paid, 0)),
                updated_at = NOW(),
                remarks = CONCAT(COALESCE(remarks, ''), %(remark)s)
            WHERE id = %(doc_id)s::uuid
            """,
            {"doc_id": doc["id"], "remark": f"\nAdmin edit by {current_user_name()}: {reason}"},
        ))
    else:
        steps.append((
            f"""
            UPDATE invoices SET
                total_amount = (
                    SELECT COALESCE(SUM(total_price), 0)
                    FROM {line_table}
                    WHERE {line_id_col} = %(source_id)s::uuid AND COALESCE(is_deleted, FALSE) = FALSE
                ),
                grand_total = (
                    SELECT COALESCE(SUM(line_total), 0)
                    FROM {line_table}
                    WHERE {line_id_col} = %(source_id)s::uuid AND COALESCE(is_deleted, FALSE) = FALSE
                ),
                total_tax = (
                    SELECT COALESCE(SUM(line_total), 0) - COALESCE(SUM(total_price), 0)
                    FROM {line_table}
                    WHERE {line_id_col} = %(source_id)s::uuid AND COALESCE(is_deleted, FALSE) = FALSE
                ),
                balance_due = GREATEST(0, (
                    SELECT COALESCE(SUM(line_total), 0)
                    FROM {line_table}
                    WHERE {line_id_col} = %(source_id)s::uuid AND COALESCE(is_deleted, FALSE) = FALSE
                ) - COALESCE(amount_paid, 0)),
                updated_at = NOW(),
                remarks = CONCAT(COALESCE(remarks, ''), %(remark)s)
            WHERE id = %(invoice_id)s::uuid
            """,
            {
                "source_id": source_id,
                "invoice_id": doc["id"],
                "remark": f"\nAdmin edit by {current_user_name()}: {reason}",
            },
        ))

    run_transaction(steps)
    _audit_edit(doc_type, doc, edited, reason)


def _audit_edit(doc_type: str, doc: Dict[str, Any], edited: List[Dict[str, Any]], reason: str) -> None:
    try:
        from modules.backoffice.audit_logger import log_financial

        old_total = _money(doc.get("grand_total") or 0)
        new_total = _money(
            _one(
                "SELECT grand_total FROM invoices WHERE id=%(id)s::uuid"
                if doc_type == "Invoice"
                else "SELECT grand_total FROM challans WHERE id=%(id)s::uuid",
                {"id": doc["id"]},
            ).get("grand_total")
        )
        log_financial(
            action=f"{doc_type.lower()}_admin_price_edit",
            entity="invoices" if doc_type == "Invoice" else "challans",
            entity_id=str(doc["id"]),
            old_value={"grand_total": old_total},
            new_value={"grand_total": new_total, "reason": reason, "line_count": len(edited)},
            user_id=current_user_name(),
            amount=abs(new_total - old_total),
            ref_no=str(doc.get("doc_no") or ""),
        )
    except Exception:
        pass


def render_invoice_edit_admin() -> None:
    require_role(ADMIN, MANAGER)
    doc_type, doc = _document_search()
    if not doc:
        return
    _render_line_editor(doc_type, doc)
