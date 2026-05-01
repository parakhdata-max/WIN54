"""
modules/documents/card_generator.py
=====================================
Print barcode cards for patients and parties.

Patient card layout (credit-card size 85mm x 54mm):
  ┌─────────────────────────────────┐
  │  DV OPTICAL              PAT000001  │
  │  Ramesh Gadhvi                  │
  │  Son of Suresh · M · DOB 1985   │
  │  📞 9876543210                  │
  │  ║║║║║║║║║║║║║║║║║║║║║║║║║║║║  │
  │         PAT000001               │
  └─────────────────────────────────┘

Page layout: A4, 2 columns × 5 rows = 10 cards per page
Cards have cut marks at corners.

Party card: same layout with party_type instead of relation.

Usage:
    from modules.documents.card_generator import generate_patient_cards
    pdf_bytes = generate_patient_cards([patient_row, ...], shop_name="DV Optical")
    # Returns bytes — stream to st.download_button
"""

import io
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128
from reportlab.lib import colors


# Card dimensions (credit card standard)
CARD_W  = 85 * mm
CARD_H  = 54 * mm
MARGIN  = 10 * mm   # page margin
GAP     = 6  * mm   # gap between cards
COLS    = 2
ROWS    = 5


def _draw_card(c, x, y, code, name, line2, line3, shop_name, card_type="PATIENT"):
    """
    Draw one card at (x, y) — bottom-left corner.
    x, y are in points (reportlab units).
    """
    from reportlab.lib.units import mm

    # Card background
    c.setFillColor(colors.white)
    c.setStrokeColor(colors.HexColor("#334155"))
    c.setLineWidth(0.5)
    c.roundRect(x, y, CARD_W, CARD_H, 3*mm, fill=1, stroke=1)

    # Cut marks (tiny crosses at corners)
    _cut_mark(c, x, y + CARD_H)       # top-left
    _cut_mark(c, x + CARD_W, y + CARD_H) # top-right
    _cut_mark(c, x, y)                 # bottom-left
    _cut_mark(c, x + CARD_W, y)        # bottom-right

    # Header bar
    c.setFillColor(colors.HexColor("#1e3a5f"))
    c.rect(x, y + CARD_H - 9*mm, CARD_W, 9*mm, fill=1, stroke=0)

    # Shop name (header)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(x + 3*mm, y + CARD_H - 6*mm, shop_name.upper())

    # Card type badge (top right)
    c.setFont("Helvetica", 6)
    c.drawRightString(x + CARD_W - 3*mm, y + CARD_H - 6*mm, card_type)

    # Code below header (right-aligned, grey)
    c.setFillColor(colors.HexColor("#94a3b8"))
    c.setFont("Helvetica", 6.5)
    c.drawRightString(x + CARD_W - 3*mm, y + CARD_H - 12*mm, code)

    # Patient/party name
    c.setFillColor(colors.HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 9)
    # Truncate if too long
    name_display = name[:32] if len(name) > 32 else name
    c.drawString(x + 3*mm, y + CARD_H - 16*mm, name_display)

    # Line 2 (relation/type + gender + DOB)
    if line2:
        c.setFont("Helvetica", 7)
        c.setFillColor(colors.HexColor("#475569"))
        c.drawString(x + 3*mm, y + CARD_H - 21.5*mm, line2[:48])

    # Line 3 (mobile)
    if line3:
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#1e40af"))
        c.drawString(x + 3*mm, y + CARD_H - 27*mm, line3)

    # Barcode (Code128)
    try:
        bc_x = x + 3*mm
        bc_y = y + 3*mm
        bc_w = CARD_W - 6*mm
        bc_h = 12*mm
        bc = code128.Code128(
            code,
            barHeight=bc_h,
            barWidth=bc_w / (len(code) * 11 + 35),  # auto-scale width
            humanReadable=False,
        )
        bc.drawOn(c, bc_x, bc_y)

        # Code below barcode
        c.setFont("Helvetica", 6.5)
        c.setFillColor(colors.HexColor("#334155"))
        c.drawCentredString(x + CARD_W/2, y + 1.5*mm, code)
    except Exception:
        # Fallback: just print the code as text
        c.setFont("Courier-Bold", 8)
        c.setFillColor(colors.HexColor("#0f172a"))
        c.drawCentredString(x + CARD_W/2, y + 8*mm, code)


def _cut_mark(c, x, y, size=2*mm):
    """Tiny cross cut mark."""
    c.setStrokeColor(colors.HexColor("#94a3b8"))
    c.setLineWidth(0.3)
    c.line(x - size, y, x + size, y)
    c.line(x, y - size, x, y + size)


def _page_positions():
    """Yield (col, row) → (x, y) bottom-left for each card slot on A4."""
    page_w, page_h = A4
    total_w = COLS * CARD_W + (COLS - 1) * GAP
    total_h = ROWS * CARD_H + (ROWS - 1) * GAP
    start_x = (page_w - total_w) / 2
    start_y = (page_h - total_h) / 2

    positions = []
    for row in range(ROWS):
        for col in range(COLS):
            x = start_x + col * (CARD_W + GAP)
            y = start_y + (ROWS - 1 - row) * (CARD_H + GAP)
            positions.append((x, y))
    return positions


# ── Public API ────────────────────────────────────────────────────────────────

def generate_patient_cards(patients: list, shop_name: str = "DV Optical") -> bytes:
    """
    Generate printable PDF of patient barcode cards.

    patients: list of dicts with keys:
        barcode, master_name, mobile, relation, gender, dob (all optional except barcode+name)

    Returns PDF bytes.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    positions = _page_positions()
    slot = 0

    for patient in patients:
        if slot > 0 and slot % (COLS * ROWS) == 0:
            c.showPage()

        x, y = positions[slot % (COLS * ROWS)]

        code    = str(patient.get('barcode') or patient.get('id','')[:8]).upper()
        name    = str(patient.get('master_name') or patient.get('patient_name','—'))
        mobile  = str(patient.get('mobile') or '').strip()
        relation= str(patient.get('relation') or 'Self').strip()
        gender  = str(patient.get('gender') or '').strip()
        dob     = str(patient.get('dob') or '').strip()

        # Line 2: relation · gender · DOB
        parts2 = []
        if relation and relation.lower() != 'self': parts2.append(relation)
        if gender:   parts2.append(gender[0].upper())  # M / F / O
        if dob:      parts2.append(f"DOB {dob[:10]}")
        line2 = '  ·  '.join(parts2) if parts2 else ''

        line3 = f"Mob: {mobile}" if mobile else ''

        _draw_card(c, x, y, code, name, line2, line3, shop_name, "PATIENT")
        slot += 1

    c.save()
    return buf.getvalue()


def generate_party_cards(parties: list, shop_name: str = "DV Optical") -> bytes:
    """
    Generate printable PDF of party barcode cards.

    parties: list of dicts with keys:
        barcode, party_name, mobile, party_type, gstin, city
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    positions = _page_positions()
    slot = 0

    for party in parties:
        if slot > 0 and slot % (COLS * ROWS) == 0:
            c.showPage()

        x, y = positions[slot % (COLS * ROWS)]

        code   = str(party.get('barcode') or party.get('id','')[:8]).upper()
        name   = str(party.get('party_name','—'))
        mobile = str(party.get('mobile') or '').strip()
        ptype  = str(party.get('party_type') or '').strip()
        city   = str(party.get('city') or '').strip()
        gstin  = str(party.get('gstin') or '').strip()

        parts2 = []
        if ptype: parts2.append(ptype)
        if city:  parts2.append(city)
        line2 = '  ·  '.join(parts2)

        parts3 = []
        if mobile: parts3.append(f"Mob: {mobile}")
        if gstin:  parts3.append(f"GST: {gstin[:15]}")
        line3 = '  ·  '.join(parts3)

        _draw_card(c, x, y, code, name, line2, line3 or '', shop_name, ptype.upper() or "PARTY")
        slot += 1

    c.save()
    return buf.getvalue()


def generate_blank_cards(
    barcode: str,
    label: str,
    subtitle: str = '',
    count: int = 1,
    shop_name: str = "DV Optical",
    card_type: str = "STAGE"
) -> bytes:
    """
    Generate cards for stage cards, blank labels, or any custom barcode.
    Use for: production stage cards, blank rack labels, service cards.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    positions = _page_positions()

    for slot in range(min(count, COLS * ROWS)):
        if slot > 0 and slot % (COLS * ROWS) == 0:
            c.showPage()
        x, y = positions[slot % (COLS * ROWS)]
        _draw_card(c, x, y, barcode, label, subtitle, '', shop_name, card_type)

    c.save()
    return buf.getvalue()
