"""
modules/printing/label_printer.py
====================================
TSPL label printer for TSC TTP-244 Pro (and compatible TSC printers).
Sends raw TSPL commands directly — no image, no DPI mismatch.

Layout  :  80mm × 14mm
Left    :  0–26.5mm → Code128 barcode + code text
Right   :  26.5–80mm → Shop name + MRP price
"""

# ── CONFIG — one place to change for entire ERP ──────────────────────────────
PRINTER_NAME  = "TSC TTP-244 Pro"
LABEL_W_MM    = 80
LABEL_H_MM    = 14
GAP_MM        = 2
FOLD_MM       = 26.5
H_SHIFT_MM    = 3
BARCODE_H     = 43
RIGHT_PAD_MM  = 4.0
ROW_GAP_DOTS  = 8
V_OFFSET_MM   = 1.0
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
RETAIL_FONT_PREF  = "3"
PRICE_FONT_PREF   = "4"
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
    right_w     = mm(LABEL_W_MM) - right_start
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
        f"BARCODE {left_start},{bar_y},\"128\",{bar_h},1,0,{module},{BAR_RATIO},\"{code}\"\n"
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
        h = win32print.OpenPrinter(PRINTER_NAME)
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
