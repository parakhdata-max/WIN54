"""
Image invoice OCR support.

This module handles the real-world WhatsApp-photo bills:
  - rotated mobile photos
  - curved paper
  - low contrast
  - table invoices

It does not require OCR at import time. If pytesseract/tesseract is installed,
`parse_invoice_image()` will run OCR. Without OCR installed it still creates
cleaned image variants and returns OCR_REQUIRED with a clear message.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _rotate_for_reading(img: Image.Image) -> Image.Image:
    """
    Most WhatsApp invoice photos arrive sideways. Try EXIF first, then choose a
    landscape orientation because Indian medicine/optical invoices are usually
    wider than tall after correction.
    """
    img = ImageOps.exif_transpose(img)
    if img.height > img.width:
        img = img.rotate(90, expand=True)
    return img


def prepare_invoice_image(
    image_path: str | Path,
    output_dir: str | Path | None = None,
) -> Dict[str, str]:
    """Create OCR-friendly image variants and return their file paths."""
    src = Path(image_path)
    if output_dir is None:
        output_dir = Path("uploads") / "invoice_ocr"
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    img = Image.open(src).convert("RGB")
    rotated = _rotate_for_reading(img)

    # Upscale small WhatsApp images before thresholding.
    scale = 2 if max(rotated.size) < 2200 else 1
    if scale > 1:
        rotated = rotated.resize((rotated.width * scale, rotated.height * scale), Image.Resampling.LANCZOS)

    gray = ImageOps.grayscale(rotated)
    gray = ImageEnhance.Contrast(gray).enhance(1.8)
    gray = ImageEnhance.Sharpness(gray).enhance(1.6)
    gray = gray.filter(ImageFilter.MedianFilter(size=3))

    # Adaptive-ish threshold using autocontrast then point threshold.
    auto = ImageOps.autocontrast(gray)
    bw = auto.point(lambda p: 255 if p > 168 else 0, mode="1")

    base = src.stem.replace(" ", "_")
    rotated_path = out_dir / f"{base}_rotated.png"
    gray_path = out_dir / f"{base}_gray.png"
    bw_path = out_dir / f"{base}_bw.png"
    rotated.save(rotated_path)
    auto.save(gray_path)
    bw.save(bw_path)

    return {
        "rotated": str(rotated_path),
        "gray": str(gray_path),
        "bw": str(bw_path),
    }


def _get_tesseract_path() -> str:
    """
    Resolve Tesseract executable path. Priority:
    1. Environment variable TESSERACT_CMD
    2. config/settings.toml [ocr] tesseract_cmd
    3. Standard Windows install locations
    4. Assume it is on PATH (Linux/Mac default)
    """
    import os
    env_path = os.environ.get("TESSERACT_CMD", "")
    if env_path and Path(env_path).exists():
        return env_path
    # Try reading from config file (optional)
    _config_paths = [
        Path(__file__).parents[2] / "config" / "settings.toml",
        Path(__file__).parents[2] / "config" / "ocr.toml",
    ]
    for _cfg in _config_paths:
        if _cfg.exists():
            try:
                import tomllib  # Python 3.11+
                with open(_cfg, "rb") as _f:
                    _data = tomllib.load(_f)
                    _t = _data.get("ocr", {}).get("tesseract_cmd", "")
                    if _t and Path(_t).exists():
                        return _t
            except Exception:
                try:
                    import tomli  # fallback package
                    with open(_cfg, "rb") as _f:
                        _data = tomli.load(_f)
                        _t = _data.get("ocr", {}).get("tesseract_cmd", "")
                        if _t and Path(_t).exists():
                            return _t
                except Exception:
                    pass
    # Windows standard locations
    for _exe in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ):
        if Path(_exe).exists():
            return _exe
    return "tesseract"  # assume on PATH (Linux/Mac/conda)


def _run_tesseract(image_path: str) -> Dict[str, Any]:
    try:
        import pytesseract  # type: ignore
    except Exception as exc:
        return {
            "ok": False,
            "engine": "tesseract",
            "text": "",
            "error": f"pytesseract not installed: {exc}",
        }
    _tess_cmd = _get_tesseract_path()
    try:
        pytesseract.pytesseract.tesseract_cmd = _tess_cmd
    except Exception:
        pass
    try:
        text = pytesseract.image_to_string(
            Image.open(image_path),
            config="--oem 3 --psm 6",
        )
        return {"ok": True, "engine": "tesseract", "text": text or "", "error": ""}
    except Exception as exc:
        return {"ok": False, "engine": "tesseract", "text": "", "error": str(exc)}


def _parse_dvijay_text(text: str) -> Dict[str, Any]:
    """
    Best-effort parser for D Vijay / Pharmed-style invoice OCR text.
    The final workflow should show these fields for human confirmation.
    """
    clean = "\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())
    upper = clean.upper()
    header: Dict[str, Any] = {
        "supplier": "D VIJAY PHARMA PVT LTD" if "VIJAY" in upper or "PHARMED" in upper else "",
        "invoice_no": "",
        "invoice_date": "",
        "order_no": "",
        "company_order_no": "",
        "parse_status": "OCR_PARSED" if clean else "OCR_REQUIRED",
    }

    m = re.search(r"(?:INV(?:OICE)?\s*NO\.?|INV\s*NO\.?)\s*[:\-]?\s*([A-Z0-9\/\-]+)", upper)
    if m:
        header["invoice_no"] = m.group(1)
    m = re.search(r"(?:INV(?:OICE)?\s*DATE|DATE)\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})", upper)
    if m:
        header["invoice_date"] = m.group(1)
    m = re.search(r"(?:ORDER\s*NO|DISTRIBUTOR\s*ORDER\s*NO|PO\s*NO)\s*[:\-]?\s*([A-Z0-9\/\-]+)", upper)
    if m:
        header["order_no"] = m.group(1)

    totals: Dict[str, Any] = {}
    for label, key in [
        ("TOTAL", "total_amount"),
        ("TAXABLE", "taxable_amount"),
        ("CGST", "cgst"),
        ("SGST", "sgst"),
        ("IGST", "igst"),
        ("TCS", "tcs"),
    ]:
        matches = re.findall(label + r"[^0-9]{0,20}([0-9,]+\.\d{2})", upper)
        if matches:
            totals[key] = _safe_float(matches[-1])

    items: List[Dict[str, Any]] = []
    for line in clean.splitlines():
        u = line.upper()
        if not any(token in u for token in ("RX", "ASTIG", "GP", "LENS", "DURUSH", "DURAS")):
            continue
        qty_match = re.search(r"\b(\d+(?:\.\d+)?)\b", line)
        amount_match = re.findall(r"([0-9,]+\.\d{2})", line)
        items.append({
            "raw_line": line,
            "description": line,
            "qty": _safe_float(qty_match.group(1)) if qty_match else 0,
            "amount": _safe_float(amount_match[-1]) if amount_match else 0,
        })

    return {"header": header, "totals": totals, "items": items, "raw_text": clean}


def parse_invoice_image(image_path: str | Path) -> Dict[str, Any]:
    """Prepare image, run OCR if available, parse known supplier patterns."""
    image_path = Path(image_path)
    variants = prepare_invoice_image(image_path)
    # Tesseract usually reads the grayscale variant better than binary for
    # folded paper because thresholding can erase faint text.
    ocr = _run_tesseract(variants["gray"])
    parsed = _parse_dvijay_text(ocr.get("text", "")) if ocr.get("ok") else {
        "header": {"parse_status": "OCR_REQUIRED"},
        "totals": {},
        "items": [],
        "raw_text": "",
    }
    return {
        "file_name": image_path.name,
        "source_type": "IMAGE",
        "preprocessed": variants,
        "ocr": ocr,
        **parsed,
    }


def parse_invoice_file(path: str | Path) -> Dict[str, Any]:
    """Route image files to OCR parser; PDF files remain in supplier_invoice_parser."""
    p = Path(path)
    if p.suffix.lower() in IMAGE_EXTENSIONS:
        return parse_invoice_image(p)
    from modules.procurement.supplier_invoice_parser import parse_supplier_invoice_pdf
    return parse_supplier_invoice_pdf(p)


def dump_parse_result(path: str | Path) -> str:
    return json.dumps(parse_invoice_file(path), indent=2, ensure_ascii=False, default=str)
