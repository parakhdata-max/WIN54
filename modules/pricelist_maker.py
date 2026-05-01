"""
pricelist_maker.py
──────────────────
Streamlit — Price List Generator with branded cover page.
  • Shop logo upload (your logo)
  • Brand logos (auto from assets/brand_logos/)
  • Cover page: shop logo (left) + brand logo (right)
  • Works for: Contact Lenses, Ophthalmic Lenses, Solutions
  • PDF download → WhatsApp
"""

import io, os, datetime
import streamlit as st

BRAND_THEMES = {
    "Cooper Vision":     {"hex": "#0B5FA5", "logo": "cooper_vision.png"},
    "Bausch & Lomb":     {"hex": "#C0392B", "logo": "bausch_lomb.png"},
    "Johnson & Johnson": {"hex": "#E67E22", "logo": "jnj.png"},
    "Alcon":             {"hex": "#00539F", "logo": "alcon.png"},
    "Silklens":          {"hex": "#8E44AD", "logo": "silklens.png"},
    "CEPL":              {"hex": "#27AE60", "logo": "cepl.png"},
    "All Brands":        {"hex": "#2C3E50", "logo": None},
}

LOGO_DIR = os.path.join(os.path.dirname(__file__), "assets", "brand_logos")


def _get_brand_logo_path(brand: str) -> str | None:
    logo_file = BRAND_THEMES.get(brand, {}).get("logo")
    if not logo_file:
        return None
    path = os.path.join(LOGO_DIR, logo_file)
    return path if os.path.exists(path) else None


def fetch_prices(brand: str, product_type: str = "All") -> list:
    try:
        from modules.sql_adapter import run_query
        brand_filter = "" if brand == "All Brands" else "AND p.brand = %(brand)s"
        type_filter  = "" if product_type == "All" else "AND UPPER(p.main_group) LIKE %(ptype)s"
        params = {}
        if brand != "All Brands": params["brand"] = brand
        if product_type != "All": params["ptype"] = f"%{product_type.upper()}%"

        rows = run_query(f"""
            SELECT p.brand, p.product_name, p.main_group,
                   p.wear_schedule, p.type,
                   s.mrp, s.selling_price, s.purchase_rate,
                   s.effective_from
            FROM inventory_stock s
            JOIN products p ON p.id = s.product_id
            WHERE s.stock_type = 'PRICE'
              AND s.is_price_current = TRUE
              AND COALESCE(s.is_active, TRUE) = TRUE
              {brand_filter} {type_filter}
            ORDER BY p.brand, p.main_group, p.product_name
        """, params) or []
        return rows
    except Exception as e:
        st.error(f"DB error: {e}")
        return []


def generate_pricelist_pdf(
    rows, brand, product_type,
    shop_name, shop_address, shop_phone,
    show_selling, show_mrp,
    footer_note, theme_hex,
    shop_logo_bytes=None,
) -> bytes:
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib.utils import ImageReader
    import colorsys

    def hex_to_rgbf(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16)/255 for i in (0,2,4))

    def mix_white(hex_c, pct):
        r,g,b = [int(hex_c.lstrip("#")[i:i+2], 16) for i in (0,2,4)]
        return colors.Color(
            (r+(255-r)*pct/100)/255, (g+(255-g)*pct/100)/255, (b+(255-b)*pct/100)/255)

    def darken(hex_c, pct):
        r,g,b = hex_to_rgbf(hex_c)
        h,s,v = colorsys.rgb_to_hsv(r,g,b)
        v2 = max(0, v*(1-pct/100))
        r2,g2,b2 = colorsys.hsv_to_rgb(h,s,v2)
        return colors.Color(r2,g2,b2)

    buf = io.BytesIO()
    W, H = A4
    c = rl_canvas.Canvas(buf, pagesize=A4)
    THEME   = colors.HexColor(theme_hex)
    LIGHT   = mix_white(theme_hex, 90)
    DARK    = darken(theme_hex, 20)
    TODAY   = datetime.date.today().strftime("%d %b %Y")

    # ── COVER PAGE ────────────────────────────────────────────────────────
    # Background gradient band
    c.setFillColor(THEME)
    c.rect(0, 0, W, H, fill=True, stroke=0)

    # White content card
    c.setFillColor(colors.white)
    c.roundRect(18*mm, 30*mm, W-36*mm, H-60*mm, 8, fill=True, stroke=0)

    # Top accent bar
    c.setFillColor(DARK)
    c.rect(18*mm, H-50*mm, W-36*mm, 20*mm, fill=True, stroke=0)

    # Brand name in accent bar
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    title_text = f"{brand} — {product_type}" if product_type != "All" else brand
    c.drawCentredString(W/2, H-40*mm, title_text)

    # Subtitle
    c.setFillColor(THEME)
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(W/2, H-62*mm, "PRODUCT PRICE LIST")
    c.setFont("Helvetica", 10)
    c.drawCentredString(W/2, H-70*mm, f"Effective Date: {TODAY}")

    # ── Shop logo (left) ──────────────────────────────────────────────────
    logo_y = H/2 - 5*mm
    if shop_logo_bytes:
        try:
            shop_img = ImageReader(io.BytesIO(shop_logo_bytes))
            c.drawImage(shop_img, 30*mm, logo_y - 20*mm, width=60*mm, height=20*mm,
                        preserveAspectRatio=True, anchor='c', mask='auto')
        except Exception:
            pass

    # ── Brand logo (right) ────────────────────────────────────────────────
    brand_logo_path = _get_brand_logo_path(brand)
    if brand_logo_path and os.path.exists(brand_logo_path):
        try:
            brand_img = ImageReader(brand_logo_path)
            c.drawImage(brand_img, W-90*mm, logo_y - 20*mm, width=60*mm, height=20*mm,
                        preserveAspectRatio=True, anchor='c', mask='auto')
        except Exception:
            pass

    # Divider line between logos
    c.setStrokeColor(LIGHT)
    c.setLineWidth(1)
    c.line(W/2, logo_y-22*mm, W/2, logo_y+2*mm)

    # Centre label
    c.setFillColor(colors.HexColor("#888888"))
    c.setFont("Helvetica", 8)
    c.drawCentredString(W/2, logo_y - 25*mm, "SUPPLIER")

    # ── Shop details box ──────────────────────────────────────────────────
    box_y = H/2 - 45*mm
    c.setFillColor(LIGHT)
    c.roundRect(28*mm, box_y, W-56*mm, 28*mm, 4, fill=True, stroke=0)
    c.setFillColor(THEME)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(W/2, box_y+20*mm, shop_name.upper())
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#444444"))
    lines = []
    if shop_address: lines.append(shop_address)
    if shop_phone:   lines.append(f"📞 {shop_phone}")
    for i, line in enumerate(lines):
        c.drawCentredString(W/2, box_y+13*mm - i*5*mm, line)

    # Product count
    c.setFillColor(THEME)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(W/2, box_y - 6*mm, f"{len(rows)} Products Listed")

    # Footer on cover
    c.setFillColor(colors.white)
    c.setFont("Helvetica", 8)
    c.drawCentredString(W/2, 20*mm, footer_note or "All prices in Rs. incl. GST. Subject to availability.")
    c.drawCentredString(W/2, 15*mm, f"Generated: {TODAY}")
    c.showPage()

    # ── DATA PAGES ────────────────────────────────────────────────────────
    from collections import defaultdict
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("brand", brand)].append(row)

    hdr_cells = ["#", "Product", "Schedule"]
    if show_mrp:     hdr_cells.append("MRP (Rs.)")
    if show_selling: hdr_cells.append("Price (Rs.)")
    n_cols = len(hdr_cells)
    col_w = {3:[10*mm,105*mm,35*mm], 4:[10*mm,90*mm,32*mm,32*mm],
              5:[10*mm,78*mm,28*mm,28*mm,28*mm]}
    col_widths = col_w.get(n_cols, col_w[5])

    rows_per_page = 28
    all_pages = [(b, chunk)
        for b_name, b_rows in grouped.items()
        for chunk in [b_rows[i:i+rows_per_page] for i in range(0,max(1,len(b_rows)),rows_per_page)]
        for b in [b_name]]
    total_pages = len(all_pages)

    for page_idx, (b_name, chunk) in enumerate(all_pages):
        page_num = page_idx + 1

        # Header bar
        c.setFillColor(THEME)
        c.rect(0, H-22*mm, W, 22*mm, fill=True, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(12*mm, H-10*mm, shop_name.upper())
        c.setFont("Helvetica", 8)
        c.drawString(12*mm, H-17*mm, f"{b_name} — Price List")
        c.drawRightString(W-12*mm, H-17*mm, f"Effective: {TODAY}")

        # Brand logo small in header
        if brand_logo_path and os.path.exists(brand_logo_path):
            try:
                brand_img = ImageReader(brand_logo_path)
                c.drawImage(brand_img, W-55*mm, H-20*mm, width=40*mm, height=12*mm,
                            preserveAspectRatio=True, anchor='c', mask='auto')
            except Exception:
                pass

        # Table
        data = [hdr_cells]
        for i, row in enumerate(chunk, 1):
            mrp_v = row.get("mrp") or 0
            sp_v  = row.get("selling_price") or 0
            cells = [str(i), row.get("product_name",""),
                     (row.get("wear_schedule") or row.get("type") or "").title()]
            if show_mrp:     cells.append(f"{float(mrp_v):,.0f}" if mrp_v else "-")
            if show_selling: cells.append(f"{float(sp_v):,.0f}" if sp_v else "-")
            data.append(cells)

        row_h = 7*mm
        tbl = Table(data, colWidths=col_widths, rowHeights=row_h)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),THEME), ("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"), ("FONTSIZE",(0,0),(-1,0),8),
            ("FONTNAME",(0,1),(-1,-1),"Helvetica"), ("FONTSIZE",(0,1),(-1,-1),8),
            ("ALIGN",(0,0),(-1,0),"CENTER"), ("ALIGN",(0,1),(0,-1),"CENTER"),
            ("ALIGN",(2,1),(-1,-1),"CENTER"), ("ALIGN",(1,1),(1,-1),"LEFT"),
            ("TOPPADDING",(0,0),(-1,-1),2), ("BOTTOMPADDING",(0,0),(-1,-1),2),
            ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,LIGHT]),
            ("FONTNAME",(-1,1),(-1,-1),"Helvetica-Bold"),
        ]))
        y_start = H - 28*mm
        tbl.wrapOn(c, W-24*mm, H)
        tbl.drawOn(c, 12*mm, y_start - row_h*len(data))

        # Footer
        c.setStrokeColor(THEME); c.setLineWidth(0.5)
        c.line(12*mm, 14*mm, W-12*mm, 14*mm)
        c.setFillColor(colors.grey); c.setFont("Helvetica", 7)
        c.drawString(12*mm, 10*mm, footer_note or "All prices in Rs. incl. 5% GST.")
        c.drawRightString(W-12*mm, 10*mm, f"Page {page_num} of {total_pages}")
        c.showPage()

    c.save()
    return buf.getvalue()


def render_pricelist_maker():
    st.markdown("## 📋 Price List Maker")
    st.caption("Branded PDF — download & share via WhatsApp")

    # ── Filters ────────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        brand = st.selectbox("Brand", list(BRAND_THEMES.keys()), key="pl_brand")
    with c2:
        product_type = st.selectbox("Product Type",
            ["All","Contact Lens","Ophthalmic Lenses","Solution","Frame"], key="pl_type")
    with c3:
        theme_hex = st.color_picker("Colour", BRAND_THEMES[brand]["hex"], key="pl_theme")

    # ── Shop details ────────────────────────────────────────────────────────
    st.markdown("**Shop details:**")
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        shop_name    = st.text_input("Shop Name", "Parakh Opticals", key="pl_shop")
        shop_address = st.text_input("Address", "Nagpur", key="pl_addr")
    with c2:
        shop_phone   = st.text_input("Phone", "", key="pl_phone")
        footer_note  = st.text_input("Footer",
            "All prices in Rs. incl. GST. Subject to availability.", key="pl_footer")
    with c3:
        st.markdown("**Shop Logo:**")
        logo_file = st.file_uploader("Upload PNG/JPG", type=["png","jpg","jpeg"],
                                     key="pl_logo", label_visibility="collapsed")
        shop_logo_bytes = logo_file.read() if logo_file else None
        if shop_logo_bytes:
            st.image(logo_file, width=80)

    # Show brand logo preview
    brand_logo_path = _get_brand_logo_path(brand)
    if brand_logo_path:
        st.caption(f"Brand logo: ✅ {brand}")
    else:
        st.caption(f"Brand logo: ⬜ not available (will show text)")

    # ── Price options ───────────────────────────────────────────────────────
    c1, c2 = st.columns(2)
    with c1: show_mrp     = st.checkbox("Show MRP",           True, key="pl_mrp")
    with c2: show_selling = st.checkbox("Show Selling Price", True, key="pl_sell")

    st.markdown("---")

    # ── Preview ─────────────────────────────────────────────────────────────
    with st.spinner("Loading prices..."):
        rows = fetch_prices(brand, product_type)

    if not rows:
        st.warning(f"No prices found for **{brand}** / **{product_type}**. Upload PRICE_MASTER.xlsx first.")
        return

    import pandas as pd
    preview = pd.DataFrame([{
        "Product":  r.get("product_name",""),
        "Schedule": (r.get("wear_schedule") or "").title(),
        "MRP":      f"Rs.{float(r.get('mrp') or 0):,.0f}",
        "Price":    f"Rs.{float(r.get('selling_price') or 0):,.0f}",
    } for r in rows[:8]])
    st.markdown(f"**Preview — {len(rows)} products**")
    st.dataframe(preview, hide_index=True, use_container_width=True)
    if len(rows) > 8:
        st.caption(f"... and {len(rows)-8} more")

    # ── Generate ─────────────────────────────────────────────────────────────
    st.markdown("---")
    if st.button("🖨️ Generate PDF", type="primary", key="pl_gen"):
        with st.spinner("Generating..."):
            try:
                pdf = generate_pricelist_pdf(
                    rows=rows, brand=brand, product_type=product_type,
                    shop_name=shop_name, shop_address=shop_address,
                    shop_phone=shop_phone, show_selling=show_selling,
                    show_mrp=show_mrp, footer_note=footer_note,
                    theme_hex=theme_hex, shop_logo_bytes=shop_logo_bytes,
                )
                fname = (f"PriceList_{brand.replace(' ','_').replace('&','n')}"
                         f"_{datetime.date.today()}.pdf")
                st.success(f"✅ Ready — {len(pdf)//1024} KB")
                st.download_button("📥 Download (WhatsApp ready)",
                    data=pdf, file_name=fname, mime="application/pdf",
                    type="primary", key="pl_dl")
                st.info("💡 Download → WhatsApp → attach → send to customer")
            except Exception as e:
                st.error(f"Failed: {e}")
                import traceback; st.code(traceback.format_exc())
