"""
Supplier invoice parser.

Current production-ready template:
  - Alcon digital PDF invoices generated with embedded compressed text streams.

The parser intentionally avoids a hard dependency on OCR libraries. It first
extracts positioned text from PDF content streams. If a future supplier sends
image-only scans, the caller can mark parse_status="OCR_REQUIRED" and route it
to an OCR dependency later.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path
from typing import Any, Dict, List, Tuple


_BT_TEXT_RE = re.compile(
    rb"BT\s+([\d\.\-]+)\s+([\d\.\-]+)\s+Td\s+/[^\s]+\s+[\d\.]+\s+Tf\s+\((.*?)\)\s*Tj\s+ET",
    re.S,
)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_BONZER_HEX_BLOCK_RE = re.compile(
    rb"BT\s*/[^\s]+\s+[\d\.]+\s+Tf\s+1\s+0\s+0\s+-1\s+([\d\.\-]+)\s+([\d\.\-]+)\s+Tm\s+((?:<[^>]+>\s*Tj\s*(?:[\d\.\-]+\s+0\s+Td\s*)?)*)ET",
    re.S,
)
_HEX_TJ_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*Tj")


def _clean_pdf_text(raw: bytes) -> str:
    return (
        raw.replace(rb"\\(", b"(")
        .replace(rb"\\)", b")")
        .replace(rb"\\n", b" ")
        .decode("latin1", "ignore")
        .strip()
    )


def _decode_bonzer_hex(hex_text: bytes) -> str:
    """Decode Bonzer's custom font hex. Low byte is shifted by +29."""
    try:
        bs = bytes.fromhex(hex_text.decode("ascii", "ignore"))
    except Exception:
        return ""
    chars: List[str] = []
    for i in range(0, len(bs) - 1, 2):
        code = (bs[i] << 8) + bs[i + 1]
        if not code:
            continue
        mapped = code + 29
        if 0 <= mapped <= 0x10FFFF:
            chars.append(chr(mapped))
    return "".join(chars)


def _money(value: str) -> float:
    try:
        return float(str(value or "0").replace(",", "").strip())
    except Exception:
        return 0.0


def _extract_positioned_text(pdf_path: str | Path) -> List[List[Tuple[float, float, str]]]:
    """Return one list of (y, x, text) per compressed text stream/page."""
    data = Path(pdf_path).read_bytes()
    pages: List[List[Tuple[float, float, str]]] = []
    for stream in _STREAM_RE.findall(data):
        try:
            decoded = zlib.decompress(stream.strip(b"\r\n"))
        except Exception:
            continue
        page_items: List[Tuple[float, float, str]] = []
        for m in _BT_TEXT_RE.finditer(decoded):
            text = _clean_pdf_text(m.group(3))
            if text:
                page_items.append((float(m.group(2)), float(m.group(1)), text))
        if page_items:
            pages.append(page_items)
    return pages


def _extract_bonzer_positioned_text(pdf_path: str | Path) -> List[List[Tuple[float, float, str]]]:
    """Extract positioned text from Bonzer PDFs using the custom hex font map."""
    data = Path(pdf_path).read_bytes()
    pages: List[List[Tuple[float, float, str]]] = []
    for stream in _STREAM_RE.findall(data):
        try:
            decoded = zlib.decompress(stream.strip(b"\r\n"))
        except Exception:
            continue
        page_items: List[Tuple[float, float, str]] = []
        for m in _BONZER_HEX_BLOCK_RE.finditer(decoded):
            x = float(m.group(1))
            y = float(m.group(2))
            text = "".join(_decode_bonzer_hex(h) for h in _HEX_TJ_RE.findall(m.group(3))).strip()
            if text:
                page_items.append((y, x, text))
        if page_items:
            pages.append(page_items)
    return pages


def _group_rows(items: List[Tuple[float, float, str]], y_tol: float = 2.0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for y, x, text in sorted(items, key=lambda t: (-t[0], t[1])):
        if not rows or abs(rows[-1]["y"] - y) > y_tol:
            rows.append({"y": y, "cells": [(x, text)]})
        else:
            rows[-1]["cells"].append((x, text))
    for row in rows:
        row["cells"] = sorted(row["cells"], key=lambda c: c[0])
        row["line"] = " | ".join(t for _, t in row["cells"])
    return rows


def _cell_near(cells: List[Tuple[float, str]], x_min: float, x_max: float) -> List[str]:
    return [txt for x, txt in cells if x_min <= x <= x_max and txt.strip()]


def _first(cells: List[Tuple[float, str]], x_min: float, x_max: float, default: str = "") -> str:
    vals = _cell_near(cells, x_min, x_max)
    return vals[0] if vals else default


def _parse_invoice_header(all_text: str, file_name: str) -> Dict[str, Any]:
    inv_no = ""
    m = re.search(r"\b(9\d{9})\b", all_text)
    if m:
        inv_no = m.group(1)
    if not inv_no:
        inv_no = Path(file_name).stem

    so_no = ""
    delivery_no = ""
    delivery_date = ""
    m = re.search(r"S\.O\.\s*No\s*\|?\s*(\d+).*?Delivery\s+number\s*\|?\s*/?\s*\|?\s*(\d+).*?/ ?\|?\s*(\d{2}\.\d{2}\.\d{4})", all_text, re.S)
    if m:
        so_no, delivery_no, delivery_date = m.group(1), m.group(2), m.group(3)

    customer_po_no = ""
    m = re.search(r"Cust\.PO\s+NO\s*\|?\s*([^|\n]+)", all_text)
    if m:
        customer_po_no = m.group(1).strip()

    due_date = ""
    m = re.search(r"Due date\s*\|?\s*(\d{2}\.\d{2}\.\d{4})", all_text)
    if m:
        due_date = m.group(1)

    return {
        "supplier": "Alcon Laboratories (India) Pvt Ltd" if "Alcon Laboratories" in all_text else "",
        "invoice_no": inv_no,
        "so_no": so_no,
        "company_order_no": so_no,
        "customer_order_no": customer_po_no,
        "delivery_no": delivery_no,
        "delivery_date": delivery_date,
        "due_date": due_date,
        "parse_status": "PARSED_TEXT" if inv_no else "NEEDS_REVIEW",
    }


def _parse_totals(all_text: str) -> Dict[str, Any]:
    def find_amount(label: str) -> float:
        m = re.search(label + r"\s*\|?\s*INR\s*\|?\s*([0-9,]+\.\d{2})", all_text)
        return _money(m.group(1)) if m else 0.0

    qty = 0.0
    m = re.search(r"Total Invoiced Quantity:\s*\|?\s*([0-9,]+\.\d+|[0-9,]+)", all_text)
    if m:
        qty = _money(m.group(1))
    return {
        "total_invoiced_qty": qty,
        "product_value": find_amount("Product Value"),
        "total_gst": find_amount("Total GST"),
        "total_amount": find_amount("Total Amount"),
    }


def _parse_items_from_page(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    pending_idx: int | None = None
    for row in rows:
        cells = row["cells"]
        code = _first(cells, 40, 70)
        desc = _first(cells, 100, 295)
        qty = _first(cells, 295, 318)
        uom = _first(cells, 320, 350)
        unit = _first(cells, 360, 405)
        ext = _first(cells, 403, 456)
        disc = _first(cells, 456, 493)
        value = _first(cells, 493, 540)
        gst = _first(cells, 540, 590)

        if re.fullmatch(r"\d{8,12}", code or "") and desc and qty and uom:
            item = {
                "item_code": code,
                "description": desc,
                "qty": _money(qty),
                "uom": uom,
                "unit_price": _money(unit),
                "ext_price": _money(ext),
                "discount": _money(disc),
                "value": _money(value),
                "gst": _money(gst),
                "batch_no": "",
                "expiry_date": "",
                "manufacturer": "",
                "old_material_no": "",
                "hsn": "",
            }
            item.update(_parse_alcon_description(desc))
            items.append(item)
            pending_idx = len(items) - 1
            continue

        if pending_idx is None:
            continue

        line = row["line"]
        batch_match = re.search(r"\b([A-Z]?\d{7,8})\s*/\s*(\d{2}\.\d{2}\.\d{4})\s*/", line)
        if batch_match and not items[pending_idx].get("batch_no"):
            items[pending_idx]["batch_no"] = batch_match.group(1)
            items[pending_idx]["expiry_date"] = batch_match.group(2)
            continue
        old_match = re.search(r"Old Material No\.\s*\|?\s*(\d+)", line)
        if old_match:
            items[pending_idx]["old_material_no"] = old_match.group(1)
            continue
        hsn_match = re.search(r"HSN/SAC CODE\s*\|?\s*(\d+)", line)
        if hsn_match:
            items[pending_idx]["hsn"] = hsn_match.group(1)
            continue
        if "CIBA" in line.upper() and not items[pending_idx].get("manufacturer"):
            items[pending_idx]["manufacturer"] = line.replace(" | ", " ").strip()

    return items


def _parse_alcon_description(desc: str) -> Dict[str, Any]:
    d = str(desc or "").upper()
    out: Dict[str, Any] = {"product_family": "", "sph": None, "cyl": None, "axis": None, "bc": None, "dia": None}
    nums = re.findall(r"[-+]\d{2}\.\d{2}|\b\d{3}\b", d)
    if "ASTG" in d or "TORIC" in d:
        out["product_family"] = "Air Optix Toric"
        m = re.search(r"(\d{3})\s+(\d{3})\s+([-+]\d{2}\.\d{2})\s+(\d{3})\s+(\d{3})", d)
        if m:
            out["bc"] = f"{int(m.group(1))/100:.2f}"
            out["dia"] = f"{int(m.group(2))/10:.1f}"
            out["sph"] = float(m.group(3))
            out["cyl"] = -float(m.group(4)) / 100
            out["axis"] = int(m.group(5))
    elif "AIROPTIX" in d or "AIROPTIX AQ HG SPH" in d:
        out["product_family"] = "Air Optix Hydraglyde SPH 6PK"
        m = re.search(r"(\d{3})\s+(\d{3})\s+([-+]\d{2}\.\d{2})", d)
        if m:
            out["bc"] = f"{int(m.group(1))/100:.2f}"
            out["dia"] = f"{int(m.group(2))/10:.1f}"
            out["sph"] = float(m.group(3))
    elif "FRESHLOOK" in d:
        out["product_family"] = "FreshLook 1-Day"
        m = re.search(r"(\d{3})\s+(\d{3})\s+([-+]\d{2}\.\d{2})\s+([A-Z]{2})", d)
        if m:
            out["bc"] = f"{int(m.group(1))/100:.2f}"
            out["dia"] = f"{int(m.group(2))/10:.1f}"
            out["sph"] = float(m.group(3))
            out["colour_code"] = m.group(4)
    return out


def _is_bonzer_pdf(rows_text: str) -> bool:
    return "Bonzer Lenses" in rows_text or "BL" in rows_text and "Tax Invoice" in rows_text


def _rx_from_segment(segment: str) -> Dict[str, Any]:
    seg = str(segment or "").replace("\xa0", " ").replace("°", " ")
    parts = re.findall(r"[+-]?\d+(?:\.\d+)?", seg)
    signed = [p for p in parts if p.startswith(("+", "-"))]
    unsigned = [p for p in parts if not p.startswith(("+", "-"))]
    out: Dict[str, Any] = {"sph": None, "cyl": None, "axis": None, "add": None}
    try:
        if len(signed) == 1 and not unsigned and float(signed[0]) > 0:
            out["add"] = float(signed[0])
            return out
        if signed:
            out["sph"] = float(signed[0])
        if len(signed) >= 3:
            out["cyl"] = float(signed[1])
            out["add"] = float(signed[-1])
        elif len(signed) >= 2:
            out["add"] = float(signed[-1])
        if unsigned:
            out["axis"] = int(float(unsigned[0]))
    except Exception:
        pass
    return out


def _parse_bonzer_product_rx(desc_text: str) -> Dict[str, Any]:
    text = " ".join(str(desc_text or "").replace("\xa0", " ").split())
    m = re.search(r"\[R\]\s*(.*?)\s*\[L\]\s*(.*)$", text, re.I)
    if not m:
        return {"product_name": text, "right": {}, "left": {}}
    product = text[:m.start()].strip(" -")
    return {
        "product_name": product,
        "right": _rx_from_segment(m.group(1)),
        "left": _rx_from_segment(m.group(2)),
    }


def _row_text_at(row: Dict[str, Any], x_min: float, x_max: float) -> str:
    return " ".join(t for x, t in row.get("cells", []) if x_min <= x <= x_max).strip()


def _near_text_at(rows: List[Dict[str, Any]], idx: int, x_min: float, x_max: float, before: int = 2, after: int = 1) -> str:
    """Find a column value on the row or immediate split rows around it."""
    direct = _row_text_at(rows[idx], x_min, x_max)
    if direct:
        return direct
    lo = max(0, idx - before)
    hi = min(len(rows), idx + after + 1)
    for j in range(idx - 1, lo - 1, -1):
        val = _row_text_at(rows[j], x_min, x_max)
        if val:
            return val
    for j in range(idx + 1, hi):
        val = _row_text_at(rows[j], x_min, x_max)
        if val:
            return val
    return ""


def _parse_bonzer_invoice(rows: List[Dict[str, Any]], file_name: str) -> Dict[str, Any]:
    rows = sorted(rows, key=lambda r: float(r.get("y") or 0))
    all_text = "\n".join(r["line"] for r in rows)

    invoice_no = ""
    m = re.search(r"Invoice No\s*\|?\s*([A-Z0-9\-/]+)", all_text)
    if m:
        invoice_no = m.group(1)

    invoice_date = ""
    m = re.search(r"Date\s*\|?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", all_text)
    if m:
        invoice_date = m.group(1)

    totals = {
        "pairs_qty": 0.0,
        "pcs_qty": 0.0,
        "sub_total": 0.0,
        "courier_charges": 0.0,
        "cgst": 0.0,
        "sgst": 0.0,
        "total_gst": 0.0,
        "total_amount": 0.0,
    }
    for row in rows:
        line = row["line"]
        if "Sub Total" in line:
            vals = re.findall(r"[0-9,]+\.\d{2}", line)
            if vals:
                totals["sub_total"] = _money(vals[-1])
        elif "Courier Charges" in line:
            vals = re.findall(r"[0-9,]+\.\d{2}", line)
            if vals:
                totals["courier_charges"] = _money(vals[-1])
        elif "CGST" in line:
            vals = re.findall(r"[0-9,]+\.\d{2}", line)
            if vals:
                totals["cgst"] = _money(vals[-1])
        elif "SGST" in line:
            vals = re.findall(r"[0-9,]+\.\d{2}", line)
            if vals:
                totals["sgst"] = _money(vals[-1])
        elif "Net Total" in line:
            vals = re.findall(r"[0-9,]+\.\d{2}", line)
            if vals:
                totals["total_amount"] = _money(vals[-1])
        elif re.fullmatch(r"\d+(?:\.\d+)?", _row_text_at(row, 470, 500) or ""):
            # The bottom qty total sits under the Qty. In Pairs column.
            try:
                y = float(row.get("y") or 0)
                if y > 740:
                    totals["pairs_qty"] = _money(_row_text_at(row, 470, 500))
            except Exception:
                pass
    totals["total_gst"] = round(totals["cgst"] + totals["sgst"], 2)
    totals["pcs_qty"] = totals["pairs_qty"] * 2

    sr_rows = []
    for idx, row in enumerate(rows):
        sr = _row_text_at(row, 15, 35)
        if re.fullmatch(r"\d+", sr or ""):
            sr_rows.append((idx, int(sr)))

    items: List[Dict[str, Any]] = []
    for pos, (idx, sr_no) in enumerate(sr_rows):
        row = rows[idx]
        next_idx = sr_rows[pos + 1][0] if pos + 1 < len(sr_rows) else len(rows)
        prev_idx = sr_rows[pos - 1][0] if pos > 0 else -1

        # Product lines appear just before/on the SR row; power lines after it.
        sr_y = float(row.get("y") or 0)
        product_parts: List[str] = []
        for pr in rows[prev_idx + 1 : idx + 1]:
            if float(pr.get("y") or 0) < sr_y - 35:
                continue
            txt = _row_text_at(pr, 260, 420)
            if not txt:
                continue
            tnorm = txt.strip()
            if "[L]" in tnorm and "[R]" not in tnorm:
                continue
            if re.match(r"^[+-]?\d", tnorm) and "[R]" not in tnorm:
                continue
            product_parts.append(tnorm)

        power_parts: List[str] = []
        for pr in rows[idx + 1 : next_idx]:
            if float(pr.get("y") or 0) > sr_y + 38:
                break
            txt = _row_text_at(pr, 260, 420)
            if not txt:
                continue
            if re.search(r"\b(ALFA|PROG|ZPAR|EASY|SURE|IRIDIO|COBALT|CLEAR|NONE|KPT|PG)\b", txt, re.I) and "[R]" not in txt:
                break
            power_parts.append(txt.strip())

        desc_text = " ".join(product_parts + power_parts)
        parsed = _parse_bonzer_product_rx(desc_text)

        pairs = _money(_row_text_at(row, 470, 505))
        rate = _money(_row_text_at(row, 515, 565))
        discount = _money(_row_text_at(row, 575, 625))
        taxable = _money(_row_text_at(row, 635, 705))

        item = {
            "sr_no": sr_no,
            "order_date": _row_text_at(row, 45, 115),
            "company_order_no": _near_text_at(rows, idx, 120, 180),
            "distributor_order_no": _near_text_at(rows, idx, 185, 245),
            "customer_order_no": _near_text_at(rows, idx, 185, 245),
            "product_name": parsed["product_name"],
            "description": desc_text,
            "hsn": _row_text_at(row, 425, 455),
            "qty_pairs": pairs,
            "qty_pcs": pairs * 2,
            "uom": "PAIR",
            "rate_per_pair": rate,
            "unit_price_per_pc": round(rate / 2, 2) if rate else 0.0,
            "discount": discount,
            "taxable_value": taxable,
            "right": parsed["right"],
            "left": parsed["left"],
        }
        items.append(item)

    if not totals["pairs_qty"]:
        totals["pairs_qty"] = sum(_money(i.get("qty_pairs")) for i in items)
        totals["pcs_qty"] = totals["pairs_qty"] * 2

    return {
        "file_name": Path(file_name).name,
        "header": {
            "supplier": "Bonzer Lenses",
            "invoice_no": invoice_no or Path(file_name).stem,
            "invoice_date": invoice_date,
            "parse_status": "PARSED_BONZER",
            "order_no_meaning": {
                "company_order_no": "Bonzer/company order no",
                "distributor_order_no": "Our/customer order no",
            },
        },
        "totals": totals,
        "items": items,
        "raw_text_preview": all_text[:4000],
    }


def parse_supplier_invoice_pdf(pdf_path: str | Path) -> Dict[str, Any]:
    """Parse a supplier invoice PDF into header, totals, and line items."""
    pages = _extract_positioned_text(pdf_path)
    if not pages:
        bonzer_pages = _extract_bonzer_positioned_text(pdf_path)
        if bonzer_pages:
            all_rows: List[Dict[str, Any]] = []
            for page in bonzer_pages:
                all_rows.extend(_group_rows(page))
            return _parse_bonzer_invoice(all_rows, str(pdf_path))
    all_lines: List[str] = []
    items: List[Dict[str, Any]] = []
    for page in pages:
        rows = _group_rows(page)
        all_lines.extend(row["line"] for row in rows)
        items.extend(_parse_items_from_page(rows))
    all_text = "\n".join(all_lines)
    header = _parse_invoice_header(all_text, str(pdf_path))
    totals = _parse_totals(all_text)
    item_qty = sum(_money(item.get("qty", 0)) for item in items)
    if item_qty and (not totals.get("total_invoiced_qty") or abs(totals["total_invoiced_qty"] - item_qty) > 0.01):
        totals["total_invoiced_qty_raw"] = totals.get("total_invoiced_qty")
        totals["total_invoiced_qty"] = item_qty
    if not pages:
        header["parse_status"] = "OCR_REQUIRED"
    return {
        "file_name": Path(pdf_path).name,
        "header": header,
        "totals": totals,
        "items": items,
        "raw_text_preview": all_text[:4000],
    }


def parse_many(paths: List[str | Path]) -> List[Dict[str, Any]]:
    return [parse_supplier_invoice_pdf(p) for p in paths]
