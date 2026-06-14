"""
modules/printing/label_printer.py
====================================
TSPL label printer for TSC TTP-244 Pro (and compatible TSC printers).
Sends raw TSPL commands directly — no image, no DPI mismatch.

Layout  :  80mm × 12mm frame / jewellery sticker
Left    :  0–26.5mm → Code128 barcode + code text
Right   :  26.5–80mm → retail name + MRP
"""

# ── CONFIG — one place to change for entire ERP ──────────────────────────────
from modules.printing.internal_print_config import (
    FRAME_STICKER_H_MM,
    FRAME_STICKER_W_MM,
    TSC_LABEL_H_MM,
    TSC_LABEL_PRINTER,
    TSC_LABEL_W_MM,
)
from modules.printing.printer_config import get_printer, printer_status

PRINTER_NAME  = TSC_LABEL_PRINTER
LABEL_W_MM    = FRAME_STICKER_W_MM
LABEL_H_MM    = FRAME_STICKER_H_MM
GAP_MM        = 2
FOLD_MM       = 26.5
H_SHIFT_MM    = 1.0
BARCODE_H     = 34
RIGHT_PAD_MM  = 2.5
ROW_GAP_DOTS  = 8
V_OFFSET_MM   = 5.5
BAR_MODULE_MAX = 2
BAR_MODULE_MIN = 1
BAR_RATIO      = 2
DENSITY        = 10
SPEED          = 3

FONT_METRICS = {
    "0": (4,  8),  "1": (6, 12),  "2": (8, 16),
    "3": (10, 20), "4": (12, 24), "5": (14, 28),
}
BARCODE_FONT_PREF = "2"
RETAIL_FONT_PREF  = "2"
PRICE_FONT_PREF   = "3"
FONT_ORDER = ["5", "4", "3", "2", "1", "0"]


def mm(v):  return int(v / 25.4 * 203)
def text_w(text, font):  return len(text) * FONT_METRICS[font][0]

def fit_font(text, max_dots, pref):
    start = FONT_ORDER.index(pref) if pref in FONT_ORDER else 0
    for f in FONT_ORDER[start:]:
        if text_w(text, f) <= max_dots:
            return f
    return "0"

def cap_text(text, font, max_dots):
    cw = FONT_METRICS[font][0]
    if text_w(text, font) <= max_dots:
        return text
    return text[:max(max_dots // cw - 1, 0)] + "."

def fit_module(code, max_dots):
    for mod in range(BAR_MODULE_MAX, BAR_MODULE_MIN - 1, -1):
        if (11 * len(code) + 35) * mod <= max_dots:
            return mod
    return BAR_MODULE_MIN

def build_tspl(code: str, shop: str, price: str, copies: int = 1) -> str:
    fold        = mm(FOLD_MM)
    left_start  = mm(H_SHIFT_MM)
    left_w      = fold - left_start
    right_start = fold + mm(RIGHT_PAD_MM)
    right_w     = (fold * 2) - right_start
    label_h     = mm(LABEL_H_MM)
    voff        = mm(V_OFFSET_MM)

    module     = fit_module(code,  left_w)
    code_font  = fit_font(code,   left_w,  BARCODE_FONT_PREF)
    code_cap   = cap_text(code,   code_font, left_w)
    shop_font  = fit_font(shop,   right_w, RETAIL_FONT_PREF)
    shop_cap   = cap_text(shop,   shop_font, right_w)
    price_font = fit_font(price,  right_w, PRICE_FONT_PREF)
    price_cap  = cap_text(price,  price_font, right_w)

    bar_h  = max(min(BARCODE_H, label_h - voff - 2 - FONT_METRICS[code_font][1] - 2), 8)
    bar_y  = voff + 2
    code_y = bar_y + bar_h + 2
    shop_y = voff + 2
    price_y = shop_y + FONT_METRICS[shop_font][1] + ROW_GAP_DOTS

    return (
        f"SIZE {LABEL_W_MM} mm,{LABEL_H_MM} mm\n"
        f"GAP {GAP_MM} mm,0\n"
        f"DIRECTION 1\n"
        f"DENSITY {DENSITY}\n"
        f"SPEED {SPEED}\n"
        f"CLS\n"
        f"BARCODE {left_start},{bar_y},\"128\",{bar_h},0,0,{module},{BAR_RATIO},\"{code}\"\n"
        f"TEXT {left_start},{code_y},\"{code_font}\",0,1,1,\"{code_cap}\"\n"
        f"TEXT {right_start},{shop_y},\"{shop_font}\",0,1,1,\"{shop_cap}\"\n"
        f"TEXT {right_start},{price_y},\"{price_font}\",0,1,1,\"{price_cap}\"\n"
        f"PRINT {copies}\n"
    )

def _price_str(price):
    s = str(price).strip().encode("ascii", errors="ignore").decode("ascii").strip()
    return f"Rs.{s}" if not s.lower().startswith("rs") else s

def _send_tspl(tspl: str):
    try:
        import win32print
        printer_name = get_printer("tsc") or PRINTER_NAME
        status = printer_status(printer_name)
        if not (status.get("exists") and status.get("ready")):
            return False, str(status.get("message") or status.get("status") or "Printer not ready")
        h = win32print.OpenPrinter(printer_name)
        try:
            win32print.StartDocPrinter(h, 1, ("TSPL Label", None, "RAW"))
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, tspl.encode("ascii", errors="replace"))
            win32print.EndPagePrinter(h)
            win32print.EndDocPrinter(h)
            return True, "OK"
        finally:
            win32print.ClosePrinter(h)
    except ImportError:
        return False, "win32print not available (not running on Windows)"
    except Exception as e:
        return False, str(e)

def print_label(code: str, shop: str, price, copies: int = 1):
    """Print one label. Returns (success, message)."""
    return _send_tspl(build_tspl(str(code).strip(), str(shop).strip(), _price_str(price), copies))


def _clean(v, max_len=60):
    s = str(v or "").encode("ascii", errors="ignore").decode("ascii")
    return s.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()[:max_len]


def _power(v, blank="-"):
    if v is None or str(v).strip() in ("", "None", "nan", "---", "--", "—"):
        return blank
    try:
        n = float(v)
        if n == 0:
            return "0.00"
        return f"+{n:.2f}" if n > 0 else f"{n:.2f}"
    except Exception:
        return _clean(v, 10) or blank


def _axis(v, blank="-"):
    if v is None or str(v).strip() in ("", "None", "nan", "0", "---", "--", "—"):
        return blank
    try:
        return str(int(float(v)))
    except Exception:
        return _clean(v, 8) or blank


def _customer_name(v, max_len=32):
    s = _clean(v, max_len)
    return "" if s.lower() in ("end customer", "unknown", "none", "null", "-", "—") else s


def build_tsc_production_label(
    order_no: str,
    eye: str,
    customer: str,
    optician: str,
    product: str,
    sph,
    cyl,
    axis,
    add,
    date_text: str = "",
    frame: str = "",
    category: str = "",
    shop: str = "Parakh Eye Care",
    copies: int = 1,
) -> str:
    """75x50mm production/envelope label using TSC native Code128 barcode."""
    W = mm(TSC_LABEL_W_MM)
    eye = _clean(eye, 1).upper() or "R"
    order_code = "".join(c for c in str(order_no or "") if c.isalnum()) + eye
    customer = _customer_name(customer, 32)
    optician = _clean(optician, 36)
    product = _clean(product, 54)
    date_text = _clean(date_text, 12)
    frame = _clean(frame, 14)
    category = _clean(category, 8)
    shop = _clean(shop, 28)

    return (
        f"SIZE {TSC_LABEL_W_MM} mm,{TSC_LABEL_H_MM} mm\n"
        "GAP 2 mm,0\n"
        "DIRECTION 0\n"
        "REFERENCE 0,0\n"
        "SPEED 3\n"
        "DENSITY 12\n"
        "SET TEAR ON\n"
        "CLS\n"
        + (f'TEXT 14,8,"3",0,1,1,"{optician}"\n' if optician else f'TEXT 14,8,"3",0,1,1,"{customer}"\n')
        + (f'TEXT 14,44,"2",0,1,1,"End Customer: {customer}"\n' if optician and customer else "")
        + f'TEXT 440,8,"2",0,1,1,"{date_text}"\n'
        + f'TEXT 14,66,"2",0,1,1,"{_clean(order_no, 22)} {eye}"\n'
        + f"BAR 14,78,{W-28},2\n"
        + f'TEXT 14,88,"2",0,1,1,"{product}"\n'
        + f"BOX 14,136,148,208,2\nBOX 160,136,294,208,2\nBOX 306,136,440,208,2\nBOX 452,136,586,208,2\n"
        + 'TEXT 69,144,"2",0,1,1,"SPH"\n'
        + 'TEXT 215,144,"2",0,1,1,"CYL"\n'
        + 'TEXT 357,144,"2",0,1,1,"AXIS"\n'
        + 'TEXT 507,144,"2",0,1,1,"ADD"\n'
        + f'TEXT 56,176,"3",0,1,1,"{_power(sph)}"\n'
        + f'TEXT 202,176,"3",0,1,1,"{_power(cyl)}"\n'
        + f'TEXT 358,176,"3",0,1,1,"{_axis(axis)}"\n'
        + f'TEXT 494,176,"3",0,1,1,"{_power(add)}"\n'
        + f"BAR 14,222,{W-28},2\n"
        + f'BARCODE 14,240,"128",80,1,0,2,2,"{order_code}"\n'
        + f'TEXT 220,326,"3",0,1,1,"{order_code}"\n'
        + f'TEXT 14,354,"2",0,1,1,"{shop}"\n'
        + f'TEXT 260,354,"1",0,1,1,"{frame}"\n'
        + f"PRINT {copies},1\n"
    )


def build_tsc_customer_label(
    order_no: str,
    customer: str,
    optician: str,
    product: str,
    rx_r: dict | None = None,
    rx_l: dict | None = None,
    mobile: str = "",
    tagline: str = "See Clearly, Check Regularly",
    date_text: str = "",
    copies: int = 1,
) -> str:
    """75x50mm customer label/card using TSC native Code128 barcode."""
    rx_r = rx_r or {}
    rx_l = rx_l or {}
    order_code = "".join(c for c in str(order_no or "") if c.isalnum()) or "ORDER"
    customer = _customer_name(customer, 32)
    optician = _clean(optician, 38)
    product = _clean(product, 56)
    mobile = _clean(mobile, 18)
    tagline = _clean(tagline, 36)
    date_text = _clean(date_text, 12)

    return (
        f"SIZE {TSC_LABEL_W_MM} mm,{TSC_LABEL_H_MM} mm\n"
        "GAP 2 mm,0\n"
        "DIRECTION 0\n"
        "REFERENCE 0,0\n"
        "SPEED 3\n"
        "DENSITY 12\n"
        "SET TEAR ON\n"
        "CLS\n"
        + f'TEXT 14,8,"2",0,1,1,"AUTHENTICITY CARD"\n'
        + f'TEXT 440,8,"2",0,1,1,"{date_text}"\n'
        + f'TEXT 14,34,"3",0,1,1,"{customer}"\n'
        + (f'TEXT 14,66,"2",0,1,1,"Optician: {optician}"\n' if optician else "")
        + (f'TEXT 408,68,"1",0,1,1,"{mobile}"\n' if mobile else "")
        + f"BAR 14,88,{mm(TSC_LABEL_W_MM)-28},2\n"
        + f'TEXT 14,100,"2",0,1,1,"{product}"\n'
        + f"BAR 14,126,{mm(TSC_LABEL_W_MM)-28},2\n"
        + 'TEXT 70,136,"2",0,1,1,"SPH"\nTEXT 188,136,"2",0,1,1,"CYL"\nTEXT 310,136,"2",0,1,1,"AX"\nTEXT 430,136,"2",0,1,1,"ADD"\n'
        + f'TEXT 18,164,"3",0,1,1,"R"\nTEXT 62,164,"3",0,1,1,"{_power(rx_r.get("sph"))}"\nTEXT 178,164,"3",0,1,1,"{_power(rx_r.get("cyl"))}"\nTEXT 312,164,"3",0,1,1,"{_axis(rx_r.get("axis"))}"\nTEXT 420,164,"3",0,1,1,"{_power(rx_r.get("add"))}"\n'
        + f'TEXT 18,198,"3",0,1,1,"L"\nTEXT 62,198,"3",0,1,1,"{_power(rx_l.get("sph"))}"\nTEXT 178,198,"3",0,1,1,"{_power(rx_l.get("cyl"))}"\nTEXT 312,198,"3",0,1,1,"{_axis(rx_l.get("axis"))}"\nTEXT 420,198,"3",0,1,1,"{_power(rx_l.get("add"))}"\n'
        + f"BAR 14,232,{mm(TSC_LABEL_W_MM)-28},2\n"
        + f'BARCODE 14,246,"128",60,1,0,2,2,"{order_code}"\n'
        + f'TEXT 220,306,"2",0,1,1,"{order_code}"\n'
        + f'TEXT 108,350,"2",0,1,1,"{tagline}"\n'
        + "REVERSE 14,342,571,32\n"
        + f"PRINT {copies},1\n"
    )


def print_tspl_production_label(**kwargs):
    return _send_tspl(build_tsc_production_label(**kwargs))


def print_tspl_customer_label(**kwargs):
    return _send_tspl(build_tsc_customer_label(**kwargs))

def print_batch(items: list, shop: str = "DV Optical"):
    """
    Print multiple labels.
    items = list of dicts {code, price, qty, shop}
         or (code, price) tuples
         or (code, shop, price) tuples
         or (code, shop, price, qty) tuples
    Returns {printed, failed, errors}.
    """
    printed = failed = 0
    errors  = []
    for item in items:
        if isinstance(item, dict):
            code   = str(item.get("code") or item.get("batch_no") or "").strip()
            price  = item.get("price") or item.get("mrp") or ""
            copies = int(item.get("qty") or item.get("copies") or 1)
            sh     = str(item.get("shop") or shop).strip()
        elif isinstance(item, (list, tuple)):
            if len(item) == 2:   code, price = item; sh = shop; copies = 1
            elif len(item) == 3: code, sh, price = item; copies = 1
            else:                code, sh, price, copies = item[0],item[1],item[2],int(item[3])
        else:
            continue
        ok, msg = print_label(code, sh, price, copies)
        if ok: printed += copies
        else:  failed  += copies; errors.append(f"{code}: {msg}")
    return {"printed": printed, "failed": failed, "errors": errors}

def list_printers():
    try:
        import win32print
        return [n for _,_,n,_ in win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
    except: return []

def get_tspl_preview(code: str, shop: str, price) -> str:
    return build_tspl(str(code).strip(), str(shop).strip(), _price_str(price), 1)
