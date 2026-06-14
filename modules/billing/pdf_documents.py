"""
Local PDF generation for WhatsApp document sharing.

The PDF is generated from live invoice/challan DB data and stored locally under
generated_docs/whatsapp. WhatsApp deep links cannot attach files, but this gives
staff a ready PDF to open/download and attach manually.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Dict, List, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


RETENTION_DAYS = 30


def _q(sql: str, params=None) -> List[Dict]:
    from modules.sql_adapter import run_query

    return run_query(sql, params or {}) or []


def _shop() -> Dict:
    try:
        from modules.settings.shop_master import get_unit_info

        return get_unit_info("retail") or {}
    except Exception:
        return {}


def _root_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "generated_docs" / "whatsapp"


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return safe.strip("_") or "document"


def cleanup_old_pdfs(days: int = RETENTION_DAYS) -> None:
    root = _root_dir()
    if not root.exists():
        return
    cutoff = time.time() - max(days, 1) * 24 * 60 * 60
    for path in root.rglob("*.pdf"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except Exception:
            pass


def ensure_document_pdf(document_type: str, document_no: str, force: bool = False) -> Tuple[str, bytes]:
    doc_type = str(document_type or "").strip().lower()
    if doc_type not in ("invoice", "challan"):
        raise ValueError("document_type must be invoice or challan")
    doc_no = str(document_no or "").strip()
    if not doc_no:
        raise ValueError("document_no is required")

    out_dir = _root_dir() / (doc_type + "s")
    out_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_pdfs()
    path = out_dir / f"{doc_type}_{_safe_filename(doc_no)}.pdf"
    if path.exists() and not force:
        return str(path), path.read_bytes()

    if doc_type == "invoice":
        doc, lines = _invoice_payload(doc_no)
    else:
        doc, lines = _challan_payload(doc_no)
    if not doc:
        raise LookupError(f"{doc_type.title()} {doc_no} not found")

    _build_pdf(path, doc_type, doc, lines)
    return str(path), path.read_bytes()


def _invoice_payload(invoice_no: str) -> Tuple[Dict, List[Dict]]:
    rows = _q(
        """
        SELECT i.*,
               COALESCE(p.party_name, 'Walk-in') AS party_name,
               COALESCE(p.mobile, '') AS mobile,
               COALESCE(p.address, '') AS address,
               COALESCE(p.city, '') AS city,
               COALESCE(p.gstin, '') AS gstin,
               c.challan_no
        FROM invoices i
        LEFT JOIN parties p ON p.id = i.party_id
        LEFT JOIN challans c ON c.id = i.challan_id
        WHERE i.invoice_no = %(no)s
        LIMIT 1
        """,
        {"no": invoice_no},
    )
    if not rows:
        return {}, []
    doc = rows[0]
    lines = _q(
        """
        SELECT il.quantity, il.unit_price,
               COALESCE(il.total_price, il.line_total, 0) AS total_price,
               COALESCE(il.tax_amount, 0) AS tax_amount,
               COALESCE(ol.gst_percent, 0) AS gst_percent,
               COALESCE(il.product_name, pr.product_name, 'Lens') AS product_name,
               COALESCE(il.brand, pr.brand, '') AS brand,
               COALESCE(il.eye_side, ol.eye_side, '') AS eye_side,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               '' AS lens_index,
               COALESCE(pr.coating::text, '') AS coating,
               COALESCE(pr.colour::text, '') AS colour
        FROM invoice_lines il
        LEFT JOIN order_lines ol ON ol.id = il.order_line_id
        LEFT JOIN products pr ON pr.id = ol.product_id
        WHERE il.invoice_id = %(id)s
          AND NOT COALESCE(il.is_deleted, FALSE)
        ORDER BY COALESCE(il.eye_side, ''), il.id
        """,
        {"id": str(doc["id"])},
    )
    return doc, lines


def _challan_payload(challan_no: str) -> Tuple[Dict, List[Dict]]:
    rows = _q(
        """
        SELECT c.*,
               COALESCE(p.party_name,
                   (SELECT o2.party_name FROM orders o2
                    WHERE o2.id::text = ANY(c.order_ids) LIMIT 1), 'Walk-in') AS party_name,
               COALESCE(p.mobile, '') AS mobile,
               COALESCE(p.address, '') AS address,
               COALESCE(p.city, '') AS city,
               COALESCE(p.gstin, '') AS gstin
        FROM challans c
        LEFT JOIN parties p ON p.id = c.party_id
        WHERE c.challan_no = %(no)s
        LIMIT 1
        """,
        {"no": challan_no},
    )
    if not rows:
        return {}, []
    doc = rows[0]
    lines = _q(
        """
        SELECT cl.quantity, cl.unit_price,
               COALESCE(cl.line_total, cl.total_price, 0) AS total_price,
               COALESCE(ol.gst_percent, 0) AS gst_percent,
               COALESCE(cl.product_name, pr.product_name, 'Lens') AS product_name,
               COALESCE(cl.brand, pr.brand, '') AS brand,
               COALESCE(cl.eye_side, ol.eye_side, '') AS eye_side,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               COALESCE(o.order_no, '') AS order_no,
               '' AS lens_index,
               COALESCE(pr.coating::text, '') AS coating,
               COALESCE(pr.colour::text, '') AS colour
        FROM challan_lines cl
        LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
        LEFT JOIN orders o ON o.id = cl.order_id
        LEFT JOIN products pr ON pr.id = ol.product_id
        WHERE cl.challan_id = %(id)s
          AND NOT COALESCE(cl.is_deleted, FALSE)
        ORDER BY COALESCE(cl.eye_side, ''), cl.id
        """,
        {"id": str(doc["id"])},
    )
    return doc, lines


def _money(value) -> str:
    try:
        return "Rs.{:,.2f}".format(float(value or 0))
    except Exception:
        return "Rs.0.00"


def _date(value) -> str:
    if not value:
        return ""
    try:
        if hasattr(value, "strftime"):
            return value.strftime("%d %b %Y")
        return str(value)[:10]
    except Exception:
        return str(value)[:10]


def _line_name(line: Dict) -> str:
    brand = str(line.get("brand") or "").strip()
    product = str(line.get("product_name") or "").strip()
    spec = " | ".join(
        str(line.get(k) or "").strip()
        for k in ("lens_index", "coating", "colour")
        if str(line.get(k) or "").strip()
    )
    name = " ".join(p for p in [brand, product] if p)
    if spec and spec.lower() not in name.lower():
        name = f"{name} ({spec})"
    return name or "Item"


def _power(line: Dict) -> str:
    def num(v, signed=True):
        if v in (None, "", "—", "-"):
            return ""
        try:
            n = float(v)
            if abs(n) < 0.0001:
                return ""
            return ("{:+.2f}" if signed else "{:.2f}").format(n)
        except Exception:
            return str(v)

    parts = []
    sph = num(line.get("sph"))
    cyl = num(line.get("cyl"))
    add = num(line.get("add_power"), signed=False)
    if sph:
        parts.append("Sph " + sph)
    if cyl:
        parts.append("Cyl " + cyl)
        axis = line.get("axis")
        if axis not in (None, "", "—", "-"):
            try:
                ax = int(float(axis))
                if ax:
                    parts.append("Axis " + str(ax))
            except Exception:
                parts.append("Axis " + str(axis))
    if add:
        parts.append("Add " + add)
    return " ".join(parts)


def _build_pdf(path: Path, doc_type: str, doc: Dict, lines: List[Dict]) -> None:
    shop = _shop()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", parent=styles["Normal"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="Tiny", parent=styles["Normal"], fontSize=7, leading=9))
    story = []

    title = "TAX INVOICE" if doc_type == "invoice" else "DELIVERY CHALLAN"
    doc_no = doc.get("invoice_no") if doc_type == "invoice" else doc.get("challan_no")
    doc_date = doc.get("invoice_date") if doc_type == "invoice" else doc.get("challan_date")

    shop_name = str(shop.get("shop_name") or "DV Optical")
    shop_lines = [
        str(shop.get("shop_address") or ""),
        " ".join(p for p in [str(shop.get("shop_city") or ""), str(shop.get("shop_pincode") or "")] if p),
        "Phone: " + str(shop.get("shop_phone") or "") if shop.get("shop_phone") else "",
        "GSTIN: " + str(shop.get("shop_gstin") or "") if shop.get("shop_gstin") else "",
    ]
    shop_html = "<br/>".join(x for x in shop_lines if x)
    header = Table(
        [
            [
                Paragraph(f"<b>{shop_name}</b><br/>{shop_html}", styles["Small"]),
                Paragraph(f"<b>{title}</b><br/>{doc_no}<br/>{_date(doc_date)}", styles["Small"]),
            ]
        ],
        colWidths=[120 * mm, 60 * mm],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story.extend([header, Spacer(1, 6)])

    party_lines = [
        str(doc.get("party_name") or "Walk-in"),
        str(doc.get("address") or ""),
        str(doc.get("city") or ""),
        "Mobile: " + str(doc.get("mobile") or "") if doc.get("mobile") else "",
        "GSTIN: " + str(doc.get("gstin") or "") if doc.get("gstin") else "",
    ]
    story.append(Paragraph("<b>Bill To:</b><br/>" + "<br/>".join(x for x in party_lines if x), styles["Small"]))
    story.append(Spacer(1, 8))

    data = [["#", "Description", "Eye", "Qty", "Rate", "GST%", "Total"]]
    for idx, line in enumerate(lines or [], start=1):
        desc = _line_name(line)
        pwr = _power(line)
        if pwr:
            desc += "<br/><font size='7'>" + pwr + "</font>"
        data.append(
            [
                str(idx),
                Paragraph(desc, styles["Tiny"]),
                str(line.get("eye_side") or ""),
                str(int(float(line.get("quantity") or 0))),
                _money(line.get("unit_price")),
                str(line.get("gst_percent") or 0),
                _money(line.get("total_price")),
            ]
        )

    table = Table(data, colWidths=[8 * mm, 78 * mm, 12 * mm, 13 * mm, 24 * mm, 16 * mm, 29 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    story.extend([table, Spacer(1, 8)])

    totals = Table(
        [
            ["Taxable / Base", _money(doc.get("total_amount"))],
            ["Tax", _money(doc.get("total_tax"))],
            ["Grand Total", _money(doc.get("grand_total"))],
        ],
        colWidths=[40 * mm, 35 * mm],
        hAlign="RIGHT",
    )
    totals.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]
        )
    )
    story.append(totals)
    if doc.get("remarks"):
        story.extend([Spacer(1, 8), Paragraph("<b>Remarks:</b> " + str(doc.get("remarks")), styles["Small"])])
    story.extend([Spacer(1, 18), Paragraph("Authorised Signatory", styles["Small"])])

    pdf = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    pdf.build(story)
