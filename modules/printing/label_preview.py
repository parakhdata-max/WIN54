"""
modules/printing/label_preview.py
====================================
SVG/HTML preview of the 80×12mm jewellery label.
Shows exact proportional layout — barcode bars, text, zones.
No printer needed. Use in Streamlit to verify before printing.
"""

import streamlit as st
import streamlit.components.v1 as components


def _bar_svg(code: str, x: int, y: int, w: int, h: int) -> str:
    """Draw a Code128-style barcode as SVG bars (visual approximation)."""
    import hashlib
    # Generate pseudo-random bar pattern from code for visual preview
    seed = int(hashlib.md5(code.encode()).hexdigest(), 16)
    bars = []
    cx = x
    bar_count = 60
    bar_unit  = w / (bar_count * 1.5)
    for i in range(bar_count):
        bit = (seed >> (i % 64)) & 1
        bw  = bar_unit * (2 if (seed >> ((i+3) % 64)) & 1 else 1)
        if bit:
            bars.append(f'<rect x="{cx:.1f}" y="{y}" width="{bw:.1f}" height="{h}" fill="#111"/>')
        cx += bw + bar_unit * 0.3
        if cx > x + w - bar_unit:
            break
    # Always end with stop pattern
    bars.append(f'<rect x="{x + w - 6:.1f}" y="{y}" width="1.5" height="{h}" fill="#111"/>')
    bars.append(f'<rect x="{x + w - 3:.1f}" y="{y}" width="3"   height="{h}" fill="#111"/>')
    return "".join(bars)


def render_label_preview(
    code: str  = "D10007",
    shop: str  = "DV OPTICAL",
    price: str = "Rs.890",
    copies: int = 1,
    show_zones: bool = False,
):
    """
    Render an accurate proportional SVG preview of the 80×12mm frame jewellery label.
    Matches the actual TSPL layout zones exactly.
    """
    import sys
    sys.path.insert(0, ".")
    try:
        from modules.printing.label_printer import (
            mm, fit_font, cap_text, fit_module, FONT_METRICS,
            FOLD_MM, H_SHIFT_MM, RIGHT_PAD_MM, LABEL_W_MM, LABEL_H_MM,
            V_OFFSET_MM, BARCODE_H, ROW_GAP_DOTS,
            BARCODE_FONT_PREF, RETAIL_FONT_PREF, PRICE_FONT_PREF,
        )
    except Exception as ex:
        st.error(f"Preview error: {ex}")
        return

    # ── Layout math (same as build_tspl) ─────────────────────────────────────
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

    _, code_fh  = FONT_METRICS[code_font]
    _, shop_fh  = FONT_METRICS[shop_font]
    _, price_fh = FONT_METRICS[price_font]

    bar_h   = max(min(BARCODE_H, label_h - voff - 2 - code_fh - 2), 8)
    bar_y   = voff + 2
    code_y  = bar_y + bar_h + 2
    shop_y  = voff + 2
    price_y = shop_y + shop_fh + ROW_GAP_DOTS

    # ── Scale to SVG (label is 639×111 dots → display at 1px = 0.9 dots) ────
    SCALE   = 0.85
    SVG_W   = int(mm(LABEL_W_MM) * SCALE)
    SVG_H   = int(label_h * SCALE)
    PADDING = 24   # outer padding for the card

    def s(v): return round(v * SCALE, 1)   # scale dots → svg px

    # ── Font sizes for SVG (approximate TSPL fonts) ───────────────────────────
    FONT_PX = {"0":7, "1":9, "2":11, "3":13, "4":15, "5":17}

    barcode_svg = _bar_svg(code, s(left_start), s(bar_y), s(left_w - 4), s(bar_h))

    zone_overlays = ""
    if show_zones:
        zone_overlays = (
            f'<rect x="{s(left_start)}" y="0" width="{s(left_w)}" height="{SVG_H}" '
            f'fill="#3b82f6" fill-opacity="0.08" stroke="#3b82f6" stroke-width="0.5" stroke-dasharray="3,2"/>'
            f'<rect x="{s(right_start)}" y="0" width="{s(right_w)}" height="{SVG_H}" '
            f'fill="#10b981" fill-opacity="0.08" stroke="#10b981" stroke-width="0.5" stroke-dasharray="3,2"/>'
            f'<line x1="{s(fold)}" y1="0" x2="{s(fold)}" y2="{SVG_H}" '
            f'stroke="#f59e0b" stroke-width="1" stroke-dasharray="4,2"/>'
        )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: Arial, sans-serif; background: #f1f5f9; padding: 20px; }}
.wrap {{ background: #fff; border-radius: 12px; padding: 20px 24px;
         box-shadow: 0 2px 12px rgba(0,0,0,0.08); max-width: 680px; }}
.title {{ font-size: 13px; font-weight: 700; color: #374151;
          letter-spacing: .04em; margin-bottom: 14px; }}
.label-outer {{
    display: inline-block;
    background: #fff;
    border: 1.5px solid #374151;
    border-radius: 3px;
    padding: {PADDING}px;
    box-shadow: 2px 2px 0 #d1d5db, 4px 4px 0 #e5e7eb;
    position: relative;
    margin-bottom: 16px;
}}
.label-outer::before {{
    content: '';
    position: absolute;
    top: -6px; left: 50%;
    transform: translateX(-50%);
    width: 40px; height: 4px;
    background: #374151;
    border-radius: 2px;
}}
.cut-mark {{
    position: absolute;
    width: 6px; height: 6px;
    border: 1px solid #94a3b8;
    background: transparent;
}}
.cut-tl {{ top: 8px;  left: 8px;  border-right: none; border-bottom: none; }}
.cut-tr {{ top: 8px;  right: 8px; border-left: none;  border-bottom: none; }}
.cut-bl {{ bottom: 8px; left: 8px;  border-right: none; border-top: none; }}
.cut-br {{ bottom: 8px; right: 8px; border-left: none;  border-top: none; }}
.info-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr;
              gap: 10px; margin-top: 12px; font-size: 11px; color: #6b7280; }}
.info-item {{ background: #f8fafc; border-radius: 6px; padding: 8px 10px; }}
.info-label {{ font-size: 9px; text-transform: uppercase;
               letter-spacing: .06em; color: #94a3b8; margin-bottom: 2px; }}
.info-val {{ font-size: 12px; font-weight: 700; color: #374151; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="title">FRAME STICKER PREVIEW — 80mm × 12mm · two 26.5mm panels + tail</div>

  <div class="label-outer">
    <div class="cut-mark cut-tl"></div>
    <div class="cut-mark cut-tr"></div>
    <div class="cut-mark cut-bl"></div>
    <div class="cut-mark cut-br"></div>

    <svg width="{SVG_W}" height="{SVG_H}"
         viewBox="0 0 {SVG_W} {SVG_H}"
         xmlns="http://www.w3.org/2000/svg"
         style="display:block;background:#fff;border:0.5px solid #e5e7eb">

      {zone_overlays}

      <!-- Barcode bars -->
      {barcode_svg}

      <!-- SKU code text below barcode -->
      <text x="{s(left_start)}" y="{s(code_y + code_fh - 2)}"
            font-family="Courier New, monospace"
            font-size="{FONT_PX[code_font]}px"
            fill="#111">{code_cap}</text>

      <!-- Shop name -->
      <text x="{s(right_start)}" y="{s(shop_y + shop_fh - 2)}"
            font-family="Arial Black, Arial, sans-serif"
            font-size="{FONT_PX[shop_font]}px"
            font-weight="bold"
            fill="#111">{shop_cap}</text>

      <!-- Price -->
      <text x="{s(right_start)}" y="{s(price_y + price_fh - 2)}"
            font-family="Arial Black, Arial, sans-serif"
            font-size="{FONT_PX[price_font] + 2}px"
            font-weight="900"
            fill="#1e3a5f">{price_cap}</text>

      <!-- Fold line (invisible on print) -->
      <line x1="{s(fold)}" y1="0" x2="{s(fold)}" y2="{SVG_H}"
            stroke="#e5e7eb" stroke-width="0.5" stroke-dasharray="2,2"/>
    </svg>
  </div>

  <div class="info-grid">
    <div class="info-item">
      <div class="info-label">Label size</div>
      <div class="info-val">80 × 12 mm</div>
    </div>
    <div class="info-item">
      <div class="info-label">Barcode</div>
      <div class="info-val">Code128 · mod {module}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Copies</div>
      <div class="info-val">{copies}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Left zone</div>
      <div class="info-val">0–{FOLD_MM:g}mm · SKU + barcode</div>
    </div>
    <div class="info-item">
      <div class="info-label">Right zone</div>
      <div class="info-val">{FOLD_MM:g}–{FOLD_MM * 2:g}mm · shop + MRP</div>
    </div>
    <div class="info-item">
      <div class="info-label">Font sizes</div>
      <div class="info-val">SKU:{code_font} · Shop:{shop_font} · Price:{price_font}</div>
    </div>
  </div>

  {'<p style="font-size:10px;color:#94a3b8;margin-top:8px">Zone overlay: blue=barcode zone · green=text zone · amber=fold line</p>' if show_zones else ''}
</div>
</body>
</html>"""

    components.html(html, height=SVG_H + PADDING * 2 + 180, scrolling=False)


def render_label_preview_widget():
    """
    Full interactive Streamlit widget — edit code/shop/price and see preview update live.
    """
    st.markdown("#### 🏷️ Label Preview")
    st.caption("Live preview of the 80×12mm frame jewellery sticker. Edit values to see changes.")

    c1, c2, c3, c4 = st.columns(4)
    code   = c1.text_input("SKU / Barcode", value="D10007",   key="prev_code")
    shop   = c2.text_input("Shop Name",     value="DV OPTICAL", key="prev_shop")
    price  = c3.text_input("MRP",           value="Rs.890",   key="prev_price")
    copies = c4.number_input("Copies",      min_value=1, value=1, key="prev_copies")

    show_zones = st.checkbox("Show zone overlay", value=False, key="prev_zones")

    render_label_preview(
        code=code, shop=shop, price=price,
        copies=copies, show_zones=show_zones
    )
