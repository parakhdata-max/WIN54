"""
modules/printing/print_templates.py
======================================
HTML print templates for all DV Optical documents.
All return HTML strings that trigger window.print() via Streamlit components.

Templates:
  job_card_label()       — 75×65mm TSPL lens envelope label (R/L)
  retail_invoice()       — A4 tax invoice with RX + items
  challan()              — A4 delivery challan
  clinical_slip()        — A5 prescription slip
  credit_debit_note()    — A4 credit/debit note
  authenticity_card()    — Credit-card-size end-customer RX card (wholesale)
  order_job_card()       — Full A5 job card for surfacing/lab
"""

from typing import Optional


# ── Shop config helper ─────────────────────────────────────────────────────────
def _shop(key: str, default: str = "") -> str:
    try:
        from modules.sql_adapter import run_query
        row = run_query(f"SELECT value FROM system_flags WHERE key='{key}' LIMIT 1") or []
        return str(row[0].get("value","")) if row else default
    except:
        return default


def _shop_for_unit(unit: str = "retail") -> dict:
    """Get shop info for specific business unit.
    unit: 'retail' | 'wholesale' | 'online'
    Returns dict with shop_name, shop_tagline, address, GSTIN etc.
    """
    try:
        from modules.settings.shop_master import get_unit_info
        return get_unit_info(unit)
    except:
        return {
            "shop_name":    _shop("shop_name", "DV Optical"),
            "shop_tagline": _shop("shop_tagline", ""),
            "shop_address": _shop("shop_address", ""),
            "shop_city":    _shop("shop_city", ""),
            "shop_state":   _shop("shop_state", ""),
            "shop_pincode": _shop("shop_pincode", ""),
            "shop_phone":   _shop("shop_phone", ""),
            "shop_gstin":   _shop("shop_gstin", ""),
            "print_footer": _shop("print_footer", ""),
        }


def _base_css() -> str:
    return """
    <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:'Arial',sans-serif;font-size:11px;color:#111;background:#fff}
    .page{background:#fff;padding:12mm 14mm;max-width:210mm;margin:0 auto}
    table{border-collapse:collapse;width:100%}
    th,td{padding:4px 6px;font-size:10px}
    .hdr{background:#1e3a5f;color:#fff;padding:10px 14px;display:flex;justify-content:space-between;align-items:flex-start}
    .hdr-shop{font-size:16px;font-weight:900;letter-spacing:.05em}
    .hdr-sub{font-size:9px;opacity:.85;margin-top:2px}
    .hdr-doc{text-align:right}
    .hdr-doc b{font-size:14px}
    .divider{border-top:0.5px solid #cbd5e1;margin:8px 0}
    .row{display:flex;justify-content:space-between;padding:2px 0;font-size:10px}
    .row-lbl{color:#64748b}
    .total-row{display:flex;justify-content:space-between;padding:5px 0;font-weight:900;font-size:13px;border-top:1.5px solid #111;margin-top:4px}
    .eye-r{background:#eff6ff;font-weight:700;color:#1e40af}
    .eye-l{background:#f0fdf4;font-weight:700;color:#166534}
    .rx-th{background:#f1f5f9;font-weight:700;text-align:center;border:0.5px solid #cbd5e1}
    .rx-td{text-align:center;border:0.5px solid #cbd5e1}
    .barcode-zone{text-align:center;margin:8px 0;font-family:'Libre Barcode 128',monospace;font-size:36px;letter-spacing:-2px}
    .barcode-num{font-size:9px;color:#475569;font-family:monospace;letter-spacing:.1em}
    @media print{
        @page{margin:8mm}
        body{print-color-adjust:exact;-webkit-print-color-adjust:exact}
    }
    </style>"""


def _barcode_html(value: str, size: int = 36) -> str:
    """Render barcode using Google Fonts Libre Barcode 128."""
    return (
        f'<link href="https://fonts.googleapis.com/css2?family=Libre+Barcode+128&display=swap" rel="stylesheet">'
        f'<div style="font-family:\'Libre Barcode 128\',monospace;font-size:{size}px;'
        f'letter-spacing:-2px;line-height:1;color:#111">{value}</div>'
        f'<div style="font-size:9px;color:#475569;font-family:monospace;letter-spacing:.1em;margin-top:2px">{value}</div>'
    )


def _print_trigger() -> str:
    return "<script>window.onload=function(){window.print()}</script>"


# ══════════════════════════════════════════════════════════════════════════════
# 1. JOB CARD LABEL — 75×65mm TSPL (R and L)
# ══════════════════════════════════════════════════════════════════════════════

def job_card_label_tspl(
    order_no: str, patient: str, eye: str,
    sph, cyl, axis, add,
    product_name: str, brand: str, batch_no: str,
    shop: str = None, date: str = "",
    frame_group: str = "", location: str = "",
    party_barcode: str = "",
) -> str:
    """
    Build TSPL command for 75×65mm lens envelope label.
    eye: 'R' or 'L'
    Returns TSPL string ready to send to printer.
    """
    from modules.printing.label_printer import (
        mm, fit_font, cap_text, fit_module, FONT_METRICS,
        LABEL_W_MM, LABEL_H_MM, GAP_MM, DENSITY, SPEED,
        BARCODE_FONT_PREF, RETAIL_FONT_PREF, PRICE_FONT_PREF,
        BAR_RATIO, V_OFFSET_MM, H_SHIFT_MM, BARCODE_H,
        RIGHT_PAD_MM, ROW_GAP_DOTS, FOLD_MM, BAR_MODULE_MIN
    )

    # 75×65mm label config (overrides the 80×14 MRP sticker config)
    W_MM  = 75
    H_MM  = 65
    GAP   = 2

    eye_u   = (eye or "").strip().upper()[0:1] or "R"
    is_r    = (eye_u == "R")
    eye_lbl = "RIGHT EYE" if is_r else "LEFT EYE"
    bc_val  = f"{order_no}-{eye_u}"

    def _fmt(v, decimals=2):
        try:
            f = float(v or 0)
            return f"{f:+.{decimals}f}" if decimals else f"{int(f)}"
        except:
            return str(v or "—")

    # TSPL for 75×65mm
    # Left half: barcode + order info
    # Right half: RX table + product
    # Uses TEXT commands with coordinate layout

    left_x  = mm(3)
    right_x = mm(38)
    dots_w  = mm(W_MM)
    dots_h  = mm(H_MM)

    # Eye header bar (top full width)
    eye_label_bc = "RIGHT EYE" if is_r else "LEFT EYE"

    tspl = (
        f"SIZE {W_MM} mm,{H_MM} mm\n"
        f"GAP {GAP} mm,0\n"
        f"DIRECTION 1\n"
        f"DENSITY {DENSITY}\n"
        f"SPEED {SPEED}\n"
        f"CLS\n"
        # Eye header bar
        f"BAR 0,0,{dots_w},18\n"
        f"TEXT {mm(37)},2,\"2\",0,1,1,\"{eye_label_bc}\"\n"
        # Order + patient
        f"TEXT {left_x},22,\"2\",0,1,1,\"{order_no}\"\n"
        f"TEXT {left_x},36,\"1\",0,1,1,\"{patient[:22]}\"\n"
        f"TEXT {left_x},46,\"1\",0,1,1,\"{date[:10]}\"\n"
        # Barcode
        f"BARCODE {left_x},{mm(23)},\"128\",{mm(12)},1,0,2,2,\"{bc_val}\"\n"
        # RX
        f"TEXT {right_x},22,\"2\",0,1,1,\"SPH  CYL  AX  ADD\"\n"
        f"TEXT {right_x},36,\"2\",0,1,1,\"{_fmt(sph)} {_fmt(cyl)} {_fmt(axis,0)} {_fmt(add)}\"\n"
        # Product
        f"TEXT {left_x},{mm(42)},\"1\",0,1,1,\"{product_name[:28]}\"\n"
        f"TEXT {left_x},{mm(48)},\"1\",0,1,1,\"{brand}  {batch_no}  {location}\"\n"
        f"PRINT 1\n"
    )
    return tspl


def job_card_label_html(
    order_no: str, patient: str, eye: str,
    sph, cyl, axis, add,
    product_name: str, brand: str, batch_no: str,
    shop: str = None, date: str = "",
    party: str = "", location: str = "",
) -> str:
    """HTML preview of job card label (for screen display before printing)."""
    shop = shop or _shop("shop_name", "DV Optical")
    eye_u  = (eye or "").strip().upper()[0:1] or "R"
    is_r   = (eye_u == "R")
    eye_lbl= "RIGHT EYE" if is_r else "LEFT EYE"
    eye_col= "#1e3a5f" if is_r else "#14532d"
    bc_val = f"{order_no}-{eye_u}"

    def _f(v, d=2):
        try: return f"{float(v or 0):+.{d}f}" if d else str(int(float(v or 0)))
        except: return str(v or "—")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <link href="https://fonts.googleapis.com/css2?family=Libre+Barcode+128&display=swap" rel="stylesheet">
    <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:Arial,sans-serif;background:#f8fafc;display:flex;justify-content:center;padding:20px}}
    .card{{width:283px;height:245px;background:#fff;border:2px solid {eye_col};border-radius:8px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,.15)}}
    .eye-hdr{{background:{eye_col};color:#fff;padding:7px 12px;font-size:14px;font-weight:900;letter-spacing:.08em;text-align:center}}
    .body{{padding:10px 12px;height:calc(100% - 40px);display:flex;flex-direction:column;gap:6px}}
    .order-row{{display:flex;justify-content:space-between;align-items:flex-start}}
    .order-no{{font-size:13px;font-weight:700;color:{eye_col}}}
    .patient{{font-size:11px;color:#374151}}
    .date{{font-size:9px;color:#9ca3af}}
    .rx-table{{border-collapse:collapse;width:100%;font-size:10px}}
    .rx-table th{{background:#f1f5f9;padding:3px 4px;text-align:center;border:0.5px solid #d1d5db;font-size:9px}}
    .rx-table td{{padding:3px 4px;text-align:center;border:0.5px solid #d1d5db;font-weight:600;color:{eye_col}}}
    .bc{{font-family:'Libre Barcode 128',monospace;font-size:32px;text-align:center;line-height:1;margin:2px 0}}
    .bc-num{{font-size:8px;text-align:center;color:#6b7280;font-family:monospace}}
    .product{{font-size:9px;color:#374151;border-top:0.5px solid #e5e7eb;padding-top:4px}}
    @media print{{body{{background:#fff;padding:0}}@page{{size:75mm 65mm;margin:0}}}}
    </style></head><body>
    <div class="card">
      <div class="eye-hdr">{eye_lbl}</div>
      <div class="body">
        <div class="order-row">
          <div>
            <div class="order-no">{order_no}</div>
            <div class="patient">{patient}</div>
            <div class="date">{date}</div>
          </div>
          <div class="date" style="text-align:right">{party}</div>
        </div>
        <table class="rx-table">
          <tr><th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th></tr>
          <tr><td>{_f(sph)}</td><td>{_f(cyl)}</td><td>{_f(axis,0)}</td><td>{_f(add)}</td></tr>
        </table>
        <div class="bc">{bc_val}</div>
        <div class="bc-num">{bc_val}</div>
        <div class="product"><b>{product_name[:32]}</b><br>{brand} · {batch_no} · {location}</div>
      </div>
    </div>
    {_print_trigger()}</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# 2. RETAIL INVOICE
# ══════════════════════════════════════════════════════════════════════════════

def retail_invoice(
    invoice_no: str, order_no: str, patient: str, mobile: str,
    lines: list, date: str = "",
    rx: dict = None, shop: str = None,
) -> str:
    """
    lines = [{product_name, qty, unit_price, gst_percent, total_price, brand, sph, cyl, axis}]
    rx    = {R:{sph,cyl,axis,add}, L:{sph,cyl,axis,add}}
    """
    shop      = shop or _shop("shop_name", "DV Optical")
    address   = _shop("shop_address", "Nashik, Maharashtra")
    gstin     = _shop("shop_gstin", "")
    phone     = _shop("shop_phone", "")

    def _f(v, d=2):
        try: return f"{float(v or 0):+.{d}f}" if d else str(int(float(v or 0)))
        except: return str(v or "—")

    subtotal = sum(float(l.get("total_price",0)) for l in lines)
    gst_total= sum(float(l.get("total_price",0)) * float(l.get("gst_percent",0)) / 100 for l in lines)
    grand    = subtotal + gst_total

    items_html = ""
    for i, l in enumerate(lines):
        bg = "#f8fafc" if i % 2 == 0 else "#fff"
        eye = l.get("eye_side","") or ""
        power = ""
        if l.get("sph"):
            power = f"<br><span style='color:#64748b;font-size:9px'>{_f(l.get('sph'))} / {_f(l.get('cyl'))} × {_f(l.get('axis',0),0)}{' ADD '+_f(l.get('add_power')) if l.get('add_power') else ''} {eye}</span>"
        gst_amt = float(l.get("total_price",0)) * float(l.get("gst_percent",0)) / 100
        items_html += f"""
        <tr style='background:{bg}'>
          <td style='padding:4px 6px'><b>{l.get('product_name','')}</b>
            {f"<span style='color:#64748b;font-size:9px'> · {l.get('brand','')}</span>" if l.get('brand') else ''}
            {power}</td>
          <td style='text-align:center;padding:4px 6px'>{l.get('qty',1)}</td>
          <td style='text-align:right;padding:4px 6px'>₹{float(l.get('unit_price',0)):,.0f}</td>
          <td style='text-align:right;padding:4px 6px;color:#64748b;font-size:9px'>@{l.get('gst_percent',0)}%<br>₹{gst_amt:,.0f}</td>
          <td style='text-align:right;padding:4px 6px;font-weight:700'>₹{float(l.get('total_price',0)):,.0f}</td>
        </tr>"""

    rx_html = ""
    if rx:
        rx_html = f"""
        <div style='margin:8px 0;border:0.5px solid #cbd5e1;border-radius:4px;overflow:hidden'>
          <table style='width:100%;border-collapse:collapse;font-size:10px'>
            <tr style='background:#f1f5f9'>
              <th style='padding:3px 6px;text-align:left'>Eye</th>
              <th style='padding:3px'>SPH</th><th style='padding:3px'>CYL</th>
              <th style='padding:3px'>AXIS</th><th style='padding:3px'>ADD</th>
            </tr>
            <tr style='background:#eff6ff'>
              <td style='padding:3px 6px;font-weight:700;color:#1e40af'>Right</td>
              <td style='text-align:center;padding:3px'>{_f(rx.get('R',{}).get('sph'))}</td>
              <td style='text-align:center;padding:3px'>{_f(rx.get('R',{}).get('cyl'))}</td>
              <td style='text-align:center;padding:3px'>{_f(rx.get('R',{}).get('axis',0),0)}</td>
              <td style='text-align:center;padding:3px'>{_f(rx.get('R',{}).get('add'))}</td>
            </tr>
            <tr style='background:#f0fdf4'>
              <td style='padding:3px 6px;font-weight:700;color:#166534'>Left</td>
              <td style='text-align:center;padding:3px'>{_f(rx.get('L',{}).get('sph'))}</td>
              <td style='text-align:center;padding:3px'>{_f(rx.get('L',{}).get('cyl'))}</td>
              <td style='text-align:center;padding:3px'>{_f(rx.get('L',{}).get('axis',0),0)}</td>
              <td style='text-align:center;padding:3px'>{_f(rx.get('L',{}).get('add'))}</td>
            </tr>
          </table>
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <link href="https://fonts.googleapis.com/css2?family=Libre+Barcode+128&display=swap" rel="stylesheet">
    {_base_css()}
    <style>
    .page{{padding:10mm 12mm}}
    @media print{{@page{{size:A4;margin:8mm}}}}
    </style>
    </head><body><div class="page">
    <div class="hdr">
      <div><div class="hdr-shop">{shop.upper()}</div>
        <div class="hdr-sub">{address}{f' · GST: {gstin}' if gstin else ''}{f' · Ph: {phone}' if phone else ''}</div>
      </div>
      <div class="hdr-doc"><b>TAX INVOICE</b><br><span style='font-size:10px'>{invoice_no}</span></div>
    </div>
    <div style='padding:10px 0;display:flex;justify-content:space-between'>
      <div><b>Bill To:</b> {patient}<br><span style='color:#64748b'>{mobile}</span></div>
      <div style='text-align:right;font-size:10px'>Date: {date}<br>Order: {order_no}</div>
    </div>
    <div class="divider"></div>
    {rx_html}
    <table>
      <tr style='background:#1e3a5f;color:#fff'>
        <th style='padding:4px 6px;text-align:left'>Item</th>
        <th style='padding:4px;text-align:center'>Qty</th>
        <th style='padding:4px;text-align:right'>Rate</th>
        <th style='padding:4px;text-align:right'>GST</th>
        <th style='padding:4px;text-align:right'>Amount</th>
      </tr>
      {items_html}
    </table>
    <div style='margin-top:8px;max-width:240px;margin-left:auto'>
      <div class="row"><span class="row-lbl">Subtotal</span><span>₹{subtotal:,.0f}</span></div>
      <div class="row"><span class="row-lbl">GST</span><span>₹{gst_total:,.0f}</span></div>
      <div class="total-row"><span>TOTAL</span><span>₹{grand:,.0f}</span></div>
    </div>
    <div class="divider" style="margin-top:12px"></div>
    <div style='display:flex;justify-content:space-between;margin-top:8px'>
      <div style='font-size:9px;color:#64748b'>Thank you for your visit.<br>Subject to Nashik jurisdiction.</div>
      <div style='text-align:center'>
        <div style='font-size:9px;color:#64748b;margin-bottom:2px'>For {shop}</div>
        <div style='border-top:0.5px solid #111;width:120px;margin-top:24px;font-size:9px;color:#64748b'>Authorised Signatory</div>
      </div>
    </div>
    </div>{_print_trigger()}</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# 3. CHALLAN
# ══════════════════════════════════════════════════════════════════════════════

def challan(
    challan_no: str, party_name: str, customer_no: str,
    lines: list, date: str = "",
    ref_order: str = "", shop: str = None,
    party_barcode: str = "",
) -> str:
    shop    = shop or _shop("shop_name", "DV Optical")
    address = _shop("shop_address", "Nashik, Maharashtra")

    items_html = ""
    for i, l in enumerate(lines):
        bg = "#f8fafc" if i % 2 == 0 else "#fff"
        items_html += f"""<tr style='background:{bg}'>
          <td style='padding:4px 6px'>{l.get('product_name','')}</td>
          <td style='text-align:center;padding:4px'>{l.get('qty',1)}</td>
          <td style='text-align:right;padding:4px 6px'>₹{float(l.get('value',0)):,.0f}</td>
        </tr>"""

    total = sum(float(l.get("value",0)) for l in lines)

    bc_html = f"""<div style='font-family:"Libre Barcode 128",monospace;font-size:40px;
        line-height:1;color:#111'>{challan_no}</div>
        <div style='font-size:9px;color:#475569;font-family:monospace'>{challan_no}</div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <link href="https://fonts.googleapis.com/css2?family=Libre+Barcode+128&display=swap" rel="stylesheet">
    {_base_css()}
    <style>.hdr{{background:#0f4c75}}.page{{padding:10mm 12mm}}
    @media print{{@page{{size:A4;margin:8mm}}}}</style>
    </head><body><div class="page">
    <div class="hdr">
      <div><div class="hdr-shop">{shop.upper()}</div>
        <div class="hdr-sub">{address}</div></div>
      <div class="hdr-doc"><b>DELIVERY CHALLAN</b><br><span style='font-size:10px'>{challan_no}</span></div>
    </div>
    <div style='padding:10px 0;display:flex;justify-content:space-between'>
      <div><b>To:</b> {party_name}<br>
        <span style='color:#64748b;font-size:9px'>{f'Customer#: {customer_no}' if customer_no else ''}</span></div>
      <div style='text-align:right;font-size:10px'>Date: {date}<br>Ref: {ref_order}</div>
    </div>
    <div class="divider"></div>
    <table>
      <tr style='background:#0f4c75;color:#fff'>
        <th style='padding:4px 6px;text-align:left'>Item</th>
        <th style='padding:4px;text-align:center'>Qty</th>
        <th style='padding:4px;text-align:right'>Value</th>
      </tr>
      {items_html}
    </table>
    <div style='max-width:200px;margin-left:auto;margin-top:6px'>
      <div class="total-row"><span>TOTAL VALUE</span><span>₹{total:,.0f}</span></div>
    </div>
    <div style='margin:12px 0;text-align:center'>{bc_html}</div>
    <div class="divider"></div>
    <div style='display:flex;justify-content:space-between;margin-top:16px;font-size:10px'>
      <div>Goods received in good condition<br>
        <div style='border-top:0.5px solid #111;width:140px;margin-top:24px;font-size:9px;color:#64748b'>Receiver signature & stamp</div>
      </div>
      <div style='text-align:center'>
        <div style='font-size:9px;color:#64748b;margin-bottom:2px'>For {shop}</div>
        <div style='border-top:0.5px solid #111;width:120px;margin-top:24px;font-size:9px;color:#64748b'>Authorised Signatory</div>
      </div>
    </div>
    </div>{_print_trigger()}</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# 4. CLINICAL PRESCRIPTION SLIP
# ══════════════════════════════════════════════════════════════════════════════

def clinical_slip(
    patient: str, mobile: str, age: str = "", gender: str = "",
    rx_r: dict = None, rx_l: dict = None,
    optometrist: str = "", date: str = "",
    next_visit: str = "", notes: str = "",
    shop: str = None,
) -> str:
    shop    = shop or _shop("shop_name", "DV Optical")
    address = _shop("shop_address", "Nashik, Maharashtra")
    phone   = _shop("shop_phone", "")

    def _f(v, d=2):
        try: return f"{float(v or 0):+.{d}f}" if d else str(int(float(v or 0)))
        except: return str(v or "—")

    rx_r = rx_r or {}
    rx_l = rx_l or {}

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    {_base_css()}
    <style>.hdr{{background:#1a3a2a}}.page{{padding:8mm 10mm;max-width:148mm}}
    @media print{{@page{{size:A5;margin:6mm}}}}</style>
    </head><body><div class="page">
    <div class="hdr">
      <div><div class="hdr-shop">{shop.upper()}</div>
        <div class="hdr-sub">{address}{f' · {phone}' if phone else ''}</div></div>
      <div class="hdr-doc"><b>PRESCRIPTION</b><br><span style='font-size:10px'>{date}</span></div>
    </div>
    <div style='padding:8px 0'>
      <b style='font-size:13px'>{patient}</b>
      <span style='color:#64748b;font-size:10px'>{f' · {age}Y' if age else ''}{f' {gender[0].upper()}' if gender else ''}</span>
      <span style='color:#64748b;font-size:10px;margin-left:12px'>{mobile}</span>
    </div>
    <div class="divider"></div>
    <table style='font-size:10px'>
      <tr style='background:#f1f5f9'>
        <th style='padding:4px 6px;text-align:left'>Eye</th>
        <th style='padding:4px;text-align:center'>SPH</th>
        <th style='padding:4px;text-align:center'>CYL</th>
        <th style='padding:4px;text-align:center'>AXIS</th>
        <th style='padding:4px;text-align:center'>ADD</th>
        <th style='padding:4px;text-align:center'>VA</th>
      </tr>
      <tr style='background:#eff6ff'>
        <td style='padding:4px 6px;font-weight:700;color:#1e40af'>Right (OD)</td>
        <td style='text-align:center;padding:4px'>{_f(rx_r.get('sph'))}</td>
        <td style='text-align:center;padding:4px'>{_f(rx_r.get('cyl'))}</td>
        <td style='text-align:center;padding:4px'>{_f(rx_r.get('axis',0),0)}</td>
        <td style='text-align:center;padding:4px'>{_f(rx_r.get('add'))}</td>
        <td style='text-align:center;padding:4px'>{rx_r.get('va','6/6')}</td>
      </tr>
      <tr style='background:#f0fdf4'>
        <td style='padding:4px 6px;font-weight:700;color:#166534'>Left (OS)</td>
        <td style='text-align:center;padding:4px'>{_f(rx_l.get('sph'))}</td>
        <td style='text-align:center;padding:4px'>{_f(rx_l.get('cyl'))}</td>
        <td style='text-align:center;padding:4px'>{_f(rx_l.get('axis',0),0)}</td>
        <td style='text-align:center;padding:4px'>{_f(rx_l.get('add'))}</td>
        <td style='text-align:center;padding:4px'>{rx_l.get('va','6/6')}</td>
      </tr>
    </table>
    <div class="divider"></div>
    <div style='display:flex;justify-content:space-between;font-size:10px'>
      <div>{'<b>Notes:</b> '+notes if notes else ''}</div>
      <div style='text-align:right'>
        Optometrist: <b>{optometrist}</b><br>
        {'Next visit: '+next_visit if next_visit else ''}
      </div>
    </div>
    <div style='margin-top:10px;border-top:1px dashed #cbd5e1;padding-top:6px;font-size:8px;color:#64748b'>
      Patient copy — retain this prescription for your next lens purchase
    </div>
    </div>{_print_trigger()}</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# 5. CREDIT / DEBIT NOTE
# ══════════════════════════════════════════════════════════════════════════════

def credit_debit_note(
    note_no: str, note_type: str, party_name: str,
    ref_invoice: str, lines: list, date: str = "",
    reason: str = "", shop: str = None,
) -> str:
    shop    = shop or _shop("shop_name", "DV Optical")
    address = _shop("shop_address", "Nashik, Maharashtra")
    is_credit = note_type.upper() == "CREDIT"
    hdr_col = "#7f1d1d" if is_credit else "#1e3a5f"
    amt_col = "#dc2626" if is_credit else "#1e40af"

    items_html = ""
    total = 0.0
    for i, l in enumerate(lines):
        bg = "#fef2f2" if (is_credit and i%2==0) else ("#eff6ff" if not is_credit and i%2==0 else "#fff")
        v = float(l.get("amount",0))
        total += v
        items_html += f"""<tr style='background:{bg}'>
          <td style='padding:4px 6px'>{l.get('description','')}</td>
          <td style='text-align:right;padding:4px 6px;color:{amt_col};font-weight:700'>
            {'−' if is_credit else '+'}₹{v:,.0f}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    {_base_css()}
    <style>.hdr{{background:{hdr_col}}}.page{{padding:10mm 12mm}}
    @media print{{@page{{size:A4;margin:8mm}}}}</style>
    </head><body><div class="page">
    <div class="hdr">
      <div><div class="hdr-shop">{shop.upper()}</div>
        <div class="hdr-sub">{address}</div></div>
      <div class="hdr-doc"><b>{note_type.upper()} NOTE</b><br><span style='font-size:10px'>{note_no}</span></div>
    </div>
    <div style='padding:10px 0;display:flex;justify-content:space-between'>
      <div><b>Party:</b> {party_name}<br>
        <span style='color:#64748b;font-size:9px'>Ref Invoice: {ref_invoice}</span></div>
      <div style='text-align:right;font-size:10px'>Date: {date}<br>Reason: {reason}</div>
    </div>
    <div class="divider"></div>
    <table>
      <tr style='background:{hdr_col};color:#fff'>
        <th style='padding:4px 6px;text-align:left'>Description</th>
        <th style='padding:4px;text-align:right'>Amount</th>
      </tr>
      {items_html}
    </table>
    <div style='max-width:200px;margin-left:auto;margin-top:6px'>
      <div class="total-row">
        <span>{'CREDIT' if is_credit else 'DEBIT'} AMOUNT</span>
        <span style='color:{amt_col}'>₹{total:,.0f}</span>
      </div>
    </div>
    <div style='margin-top:16px;font-size:9px;color:#64748b'>
      {'Amount will be adjusted against next invoice or refunded.' if is_credit else 'Amount recoverable from party.'}
    </div>
    </div>{_print_trigger()}</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# 6. AUTHENTICITY CARD — wholesale end-customer (credit card size)
# ══════════════════════════════════════════════════════════════════════════════

def authenticity_card(
    customer_name: str, order_no: str, date: str = "",
    rx_r: dict = None, rx_l: dict = None,
    product: str = "", frame: str = "", batch_no: str = "",
    from_party: str = "", shop: str = None,
) -> str:
    shop  = shop or _shop("shop_name", "DV Optical")
    rx_r  = rx_r or {}
    rx_l  = rx_l or {}

    def _f(v, d=2):
        try: return f"{float(v or 0):+.{d}f}" if d else str(int(float(v or 0)))
        except: return str(v or "—")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <link href="https://fonts.googleapis.com/css2?family=Libre+Barcode+128&display=swap" rel="stylesheet">
    {_base_css()}
    <style>
    body{{background:#f1f5f9;display:flex;justify-content:center;padding:20px}}
    .card{{width:85.6mm;background:#fff;border:2px solid #1e3a5f;border-radius:8px;overflow:hidden;
           box-shadow:0 4px 16px rgba(0,0,0,.2)}}
    .gold{{background:#1e3a5f;color:#fff;padding:8px 12px;text-align:center;
           font-size:11px;font-weight:900;letter-spacing:.12em}}
    .body{{padding:8px 10px}}
    .customer{{font-size:13px;font-weight:700;color:#1e3a5f;margin-bottom:2px}}
    .meta{{font-size:9px;color:#64748b;margin-bottom:6px;display:flex;justify-content:space-between}}
    .rx-tbl{{border-collapse:collapse;width:100%;font-size:9px;margin:4px 0}}
    .rx-tbl th{{background:#f1f5f9;padding:2px 4px;text-align:center;border:0.5px solid #d1d5db;font-size:8px}}
    .rx-tbl td{{padding:2px 4px;text-align:center;border:0.5px solid #d1d5db}}
    .divider{{border-top:0.5px solid #e2e8f0;margin:5px 0}}
    .prod{{font-size:9px;color:#374151;line-height:1.5}}
    .bc{{font-family:'Libre Barcode 128',monospace;font-size:30px;text-align:center;line-height:1;margin:4px 0}}
    .bc-num{{font-size:8px;text-align:center;color:#6b7280;font-family:monospace}}
    .footer{{font-size:7.5px;color:#9ca3af;text-align:center;font-style:italic;padding-bottom:6px}}
    @media print{{body{{background:#fff;padding:0}}@page{{size:85.6mm 120mm;margin:0}}}}
    </style></head><body>
    <div class="card">
      <div class="gold">LENS AUTHENTICITY CARD · {shop.upper()}</div>
      <div class="body">
        <div class="customer">{customer_name}</div>
        <div class="meta">
          <span>Order: {order_no}</span>
          <span>{date}</span>
        </div>
        <table class="rx-tbl">
          <tr><th>Eye</th><th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th></tr>
          <tr style='background:#eff6ff'>
            <td style='font-weight:700;color:#1e40af'>R</td>
            <td>{_f(rx_r.get('sph'))}</td><td>{_f(rx_r.get('cyl'))}</td>
            <td>{_f(rx_r.get('axis',0),0)}</td><td>{_f(rx_r.get('add'))}</td>
          </tr>
          <tr style='background:#f0fdf4'>
            <td style='font-weight:700;color:#166534'>L</td>
            <td>{_f(rx_l.get('sph'))}</td><td>{_f(rx_l.get('cyl'))}</td>
            <td>{_f(rx_l.get('axis',0),0)}</td><td>{_f(rx_l.get('add'))}</td>
          </tr>
        </table>
        <div class="divider"></div>
        <div class="prod">
          {'<b>Lens:</b> '+product+'<br>' if product else ''}
          {'<b>Frame:</b> '+frame+(' · '+batch_no if batch_no else '')+'<br>' if frame else ''}
          {'<b>Supplied by:</b> '+from_party if from_party else ''}
        </div>
        <div class="divider"></div>
        <div class="bc">{order_no}</div>
        <div class="bc-num">{order_no} · Scan for service history</div>
        <div class="divider"></div>
        <div class="footer">Present this card for any lens-related service request</div>
      </div>
    </div>
    {_print_trigger()}</body></html>"""
