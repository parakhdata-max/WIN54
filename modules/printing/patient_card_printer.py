"""
modules/printing/patient_card_printer.py
==========================================
Patient ID card printing for all three formats:

  1. TSC TTP-244 Pro  — 75×65mm TSPL sticker (jewellery roll)
  2. Evolis Primacy   — 85.6×54mm CR80 plastic card (HTML→PDF via browser)
  3. A4 PDF sheet     — 6 cards per page, download and cut

Patient ID format: PAT000001 (auto-incremented, stored in patients.barcode)

Barcode on all formats for universal scan:
  - Scan at billing → auto-fills patient details
  - Scan at future kiosk → pulls history
  - Scan at referral → optometrist sees records
"""

import streamlit as st
import streamlit.components.v1 as components


# ── Patient ID generation ─────────────────────────────────────────────────────

def ensure_patient_id(patient_id: str) -> str:
    """
    Get or generate PAT000001-format ID for a patient.
    Uses record_no if already in PAT format.
    Otherwise adds barcode column via migration and generates one.
    Falls back gracefully if column doesn't exist yet.
    """
    # TEMP IDs are placeholders for unlinked patients — return fallback immediately
    if not patient_id or str(patient_id).upper().startswith("TEMP-"):
        return str(patient_id or "UNKNOWN")[:12].upper()

    try:
        from modules.sql_adapter import run_query, run_write

        # Step 1: Check record_no — may already be PAT format
        rows = run_query("""
            SELECT id::text AS pid,
                   COALESCE(record_no,'') AS record_no
            FROM patients WHERE id=%s::uuid LIMIT 1
        """, (patient_id,)) or []

        if rows:
            rec = rows[0].get("record_no","")
            if rec and str(rec).upper().startswith("PAT"):
                return str(rec).upper()

        # Step 2: Try barcode column
        try:
            brows = run_query(
                "SELECT barcode FROM patients WHERE id=%s::uuid LIMIT 1",
                (patient_id,)
            ) or []
            if brows and brows[0].get("barcode"):
                return str(brows[0]["barcode"])
        except Exception:
            # Column doesn't exist — add it
            try:
                run_write(
                    "ALTER TABLE patients ADD COLUMN IF NOT EXISTS barcode VARCHAR(50)"
                )
            except Exception:
                pass

        # Step 3: Generate new PAT number
        try:
            cnt = run_query(
                "SELECT COUNT(*) AS n FROM patients WHERE barcode LIKE 'PAT%'"
            ) or [{"n": 0}]
        except Exception:
            cnt = [{"n": 0}]

        # Also count record_no PAT entries
        try:
            cnt2 = run_query(
                "SELECT COUNT(*) AS n FROM patients WHERE record_no LIKE 'PAT%'"
            ) or [{"n": 0}]
            total = max(int(cnt[0].get("n",0)), int(cnt2[0].get("n",0)))
        except Exception:
            total = int(cnt[0].get("n",0))

        new_code = f"PAT{total+1:06d}"

        # Step 4: Save — try barcode column first, else update record_no
        try:
            run_write(
                "UPDATE patients SET barcode=%s WHERE id=%s::uuid AND (barcode IS NULL OR barcode='')",
                (new_code, patient_id)
            )
        except Exception:
            try:
                run_write(
                    "UPDATE patients SET record_no=%s WHERE id=%s::uuid AND (record_no IS NULL OR record_no='')",
                    (new_code, patient_id)
                )
            except Exception:
                pass

        return new_code

    except Exception as ex:
        # Final fallback — use short UUID
        short = patient_id.replace("-","")[:8].upper()
        return f"PAT{short}"


def get_patient_barcode(patient_id: str) -> str:
    """Get existing patient barcode or generate one."""
    return ensure_patient_id(patient_id)


# ── TSC TTP-244 Pro — 75×65mm sticker (TSPL) ─────────────────────────────────

def build_tspl_patient_sticker(
    barcode: str,
    name: str,
    mobile: str = "",
    rx_r: dict = None,
    rx_l: dict = None,
    shop: str = "DV Optical",
    copies: int = 1,
) -> str:
    """
    TSPL commands for 75×65mm patient sticker on TSC TTP-244 Pro.
    Resolution: 8 dots/mm (203 dpi) → 600W × 520H dots.

    Layout:
    ┌──────────────────────────────┐
    │ DV OPTICAL        PAT000001 │  ← shop + barcode ID   (bold, large)
    ├──────────────────────────────┤
    │ RAMESH KUMAR                 │  ← patient name        (extra bold)
    │ Mob: 9876543210              │  ← mobile
    ├──────────────────────────────┤
    │  R: -2.00 / -0.50 x 90      │  ← RX lines
    │  L: -1.75 / -0.25 x 85      │
    ├──────────────────────────────┤
    │ ████████ CODE128 ████████    │  ← barcode + text
    └──────────────────────────────┘

    Font guide (TSC built-in):
      "1"=8×12  "2"=12×20  "3"=16×24  "4"=24×32  "5"=32×48
    """
    rx_r = rx_r or {}
    rx_l = rx_l or {}

    def _fv(v):
        if not v or str(v).strip() in ("", "None", "nan", "0.0", "0"):
            return "---"
        try:
            f = float(v)
            return f"+{f:.2f}" if f > 0 else f"{f:.2f}"
        except:
            return str(v)

    def _fax(v):
        if not v or str(v).strip() in ("", "None", "nan", "0"):
            return "---"
        try:
            return str(int(float(v)))
        except:
            return str(v)

    W = 600   # 75mm × 8dpmm
    H = 520   # 65mm × 8dpmm

    rs = _fv(rx_r.get("sph")); rc = _fv(rx_r.get("cyl")); ra = _fax(rx_r.get("axis"))
    ls = _fv(rx_l.get("sph")); lc = _fv(rx_l.get("cyl")); la = _fax(rx_l.get("axis"))
    r_add = _fv(rx_r.get("add")); l_add = _fv(rx_l.get("add"))

    rx_r_str = f"R: {rs} / {rc} x {ra}" + (f" ADD {r_add}" if r_add != "---" else "")
    rx_l_str = f"L: {ls} / {lc} x {la}" + (f" ADD {l_add}" if l_add != "---" else "")

    # Truncate to fit label width
    name_upper = name.upper()[:26]
    shop_upper = shop.upper()[:18]
    mob_str    = f"Mob: {mobile}" if mobile else ""

    tspl = f"""SIZE 75 mm, 65 mm
GAP 2 mm, 0 mm
DIRECTION 0
REFERENCE 0,0
CLS
SET PEEL OFF
SET CUTTER OFF

; ── Row 1: Shop name (L) + Patient ID (R) ──
TEXT 8,6,"3",0,1,1,"{shop_upper}"
TEXT {W-8},6,"3",0,1,1,"{barcode}"
BAR 0,46,{W},3

; ── Row 2: Patient name (large bold) ──
TEXT 8,56,"4",0,1,1,"{name_upper}"
TEXT 8,98,"2",0,1,1,"{mob_str}"
BAR 0,126,{W},2

; ── Row 3: RX ──
TEXT 8,136,"3",0,1,1,"{rx_r_str}"
TEXT 8,170,"3",0,1,1,"{rx_l_str}"
BAR 0,202,{W},2

; ── Row 4: Barcode (centred) ──
BARCODE {W//2},{H-108},"128",88,1,0,3,3,"{barcode}"
TEXT {W//2},{H-14},"2",0,1,1,"{barcode}"

PRINT {copies}
"""
    return tspl




# ── Evolis Primacy — CR80 plastic card (HTML) ─────────────────────────────────

def _build_evolis_html(
    barcode: str,
    name: str,
    mobile: str = "",
    rx_r: dict = None,
    rx_l: dict = None,
    shop: str = "DV Optical",
    tagline: str = "",
    visit_date: str = "",
) -> str:
    """
    HTML for Evolis Primacy CR80 plastic card (85.6mm × 54mm).

    Evolis Primacy is a dye-sublimation printer:
    ● ALWAYS use white background — gradients print as muddy gray
    ● Use solid dark navy (#0f172a) or black (#000) for all text
    ● Minimum font size 7pt for legibility after lamination
    ● Barcode must be jet black on white — no color bars
    ● Print via browser → Ctrl+P → select Evolis → set CR80 paper size
    """
    rx_r = rx_r or {}
    rx_l = rx_l or {}

    def _fv(v):
        if not v or str(v).strip() in ("", "None", "nan", "0.0", "0", "---"):
            return "—"
        try:
            f = float(v)
            return f"+{f:.2f}" if f > 0 else f"{f:.2f}"
        except:
            return str(v)

    def _fax(v):
        if not v or str(v).strip() in ("", "None", "nan", "0", "---"):
            return "—"
        try:
            return str(int(float(v)))
        except:
            return str(v)

    rs = _fv(rx_r.get("sph")); rc = _fv(rx_r.get("cyl")); ra = _fax(rx_r.get("axis"))
    ls = _fv(rx_l.get("sph")); lc = _fv(rx_l.get("cyl")); la = _fax(rx_l.get("axis"))
    r_add = _fv(rx_r.get("add")); l_add = _fv(rx_l.get("add"))

    # Barcode SVG embedded — real Code128, black on white
    try:
        bc_svg = barcode_svg(barcode, width=180, height=36)
    except Exception:
        bc_svg = f"<div style=\'font-family:monospace;font-size:7pt;font-weight:bold\'>{barcode}</div>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: 85.6mm 54mm; margin: 0; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: Arial, Helvetica, sans-serif; background: white; }}

/* ── CARD: white background — mandatory for dye-sub ── */
.card {{
    width: 85.6mm;
    height: 54mm;
    background: #ffffff;
    border: 0.3mm solid #0f172a;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}}

/* ── TOP HEADER STRIPE — solid navy, no gradient ── */
.header {{
    background: #0f172a;
    padding: 1.6mm 3mm;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
}}
.shop-name {{
    font-size: 8.5pt;
    font-weight: 900;
    color: #ffffff;
    letter-spacing: 0.06em;
    text-transform: uppercase;
}}
.card-label {{
    font-size: 5pt;
    color: #94a3b8;
    letter-spacing: 0.12em;
    text-transform: uppercase;
}}

/* ── PATIENT SECTION ── */
.patient-block {{
    padding: 1.5mm 3mm 0.5mm;
    flex-shrink: 0;
    border-bottom: 0.3mm solid #e2e8f0;
}}
.patient-name {{
    font-size: 10.5pt;
    font-weight: 900;
    color: #0f172a;
    letter-spacing: 0.01em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
.patient-sub {{
    font-size: 6.5pt;
    color: #475569;
    margin-top: 0.3mm;
    font-weight: 600;
}}

/* ── RX TABLE ── */
.rx-wrap {{
    padding: 1mm 3mm 0;
    flex-grow: 1;
}}
.rx-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 6.5pt;
}}
.rx-table th {{
    background: #0f172a;
    color: #ffffff;
    padding: 0.8mm 1.5mm;
    text-align: center;
    font-weight: 700;
    font-size: 6pt;
    letter-spacing: 0.05em;
}}
.rx-table th:first-child {{ text-align: left; }}
.rx-table td {{
    padding: 0.9mm 1.5mm;
    text-align: center;
    font-weight: 700;
    color: #0f172a;
    border-bottom: 0.2mm solid #e2e8f0;
    font-size: 6.5pt;
}}
.rx-table td:first-child {{ text-align: left; font-weight: 900; }}
.row-r {{ background: #eff6ff; }}
.row-l {{ background: #f0fdf4; }}

/* ── BARCODE FOOTER ── */
.bc-footer {{
    background: #ffffff;
    border-top: 0.3mm solid #e2e8f0;
    padding: 0.5mm 3mm;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
}}
.bc-wrap {{ line-height: 0; }}
.visit-info {{
    font-size: 5pt;
    color: #64748b;
    text-align: right;
    font-weight: 600;
}}
</style>
</head><body>
<div class="card">

  <div class="header">
    <span class="shop-name">{shop}</span>
    <span class="card-label">Patient Card</span>
  </div>

  <div class="patient-block">
    <div class="patient-name">{name}</div>
    <div class="patient-sub">
      {f"📞 {mobile}" if mobile else ""}
      &nbsp;·&nbsp;
      <span style="font-family:monospace;color:#0f172a;font-weight:900">{barcode}</span>
    </div>
  </div>

  <div class="rx-wrap">
    <table class="rx-table">
      <tr>
        <th>Eye</th><th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th>
      </tr>
      <tr class="row-r">
        <td>R</td><td>{rs}</td><td>{rc}</td><td>{ra}</td><td>{r_add}</td>
      </tr>
      <tr class="row-l">
        <td>L</td><td>{ls}</td><td>{lc}</td><td>{la}</td><td>{l_add}</td>
      </tr>
    </table>
  </div>

  <div class="bc-footer">
    <div class="bc-wrap">{bc_svg}</div>
    <div class="visit-info">
      {f"Date: {visit_date}" if visit_date else ""}<br>
      {tagline}
    </div>
  </div>

</div>
<script>window.onload = function() {{ window.print(); }}</script>
</body></html>"""



def barcode_svg(code: str, width: int = 220, height: int = 56) -> str:
    """
    Generate a real Code128-B barcode SVG — scannable by any 1D reader.
    Code128-B covers ASCII 32-127 with checksum verification.
    """
    # Code128-B symbol widths (6 elements: bar,space,bar,space,bar,space)
    W = [
        [2,1,2,2,2,2],[2,2,2,1,2,2],[2,2,2,2,2,1],[1,2,1,2,2,3],[1,2,1,3,2,2],
        [1,3,1,2,2,2],[1,2,2,2,1,3],[1,2,2,3,1,2],[1,3,2,2,1,2],[2,2,1,2,1,3],
        [2,2,1,3,1,2],[2,3,1,2,1,2],[1,1,2,2,3,2],[1,2,2,1,3,2],[1,2,2,2,3,1],
        [1,1,3,2,2,2],[1,2,3,1,2,2],[1,2,3,2,2,1],[2,2,3,2,1,1],[2,2,1,1,3,2],
        [2,2,1,2,3,1],[2,1,3,2,1,2],[2,2,3,1,1,2],[3,1,2,1,3,1],[3,1,1,2,2,2],
        [3,2,1,1,2,2],[3,2,1,2,2,1],[3,1,2,2,1,2],[3,2,2,1,1,2],[3,2,2,2,1,1],
        [2,1,2,1,2,3],[2,1,2,3,2,1],[2,3,2,1,2,1],[1,1,1,3,2,3],[1,3,1,1,2,3],
        [1,3,1,3,2,1],[1,1,2,3,1,3],[1,3,2,1,1,3],[1,3,2,3,1,1],[2,1,1,3,1,3],
        [2,3,1,1,1,3],[2,3,1,3,1,1],[1,1,3,1,2,3],[1,1,3,3,2,1],[1,3,3,1,2,1],
        [1,1,2,1,3,3],[1,1,2,3,3,1],[1,3,2,1,3,1],[1,1,3,2,1,3],[1,1,3,3,1,2],
        [1,3,3,2,1,1],[2,1,3,1,1,3],[2,3,1,1,1,3],[2,1,1,1,3,3],[2,1,1,3,3,1],
        [2,1,3,1,3,1],[2,3,3,1,1,1],[2,3,1,3,1,1],[3,3,1,1,2,1],[3,1,1,3,2,1],
        [3,1,3,1,2,1],[3,1,3,3,2,1],[3,3,3,1,2,1],[3,1,2,1,1,3],[3,1,2,3,1,1],
        [3,3,2,1,1,1],[3,3,2,3,1,1],[3,1,4,1,1,1],[2,2,4,1,1,1],[4,3,1,1,1,1],
        [1,1,1,1,4,3],[1,1,1,3,4,1],[1,3,1,1,4,1],[1,1,4,1,1,3],[1,1,4,3,1,1],
        [4,1,1,1,1,3],[4,1,1,3,1,1],[1,1,3,1,4,1],[1,1,4,1,3,1],[3,1,1,1,4,1],
        [4,1,1,1,3,1],[2,1,1,4,1,2],[2,1,1,2,1,4],[2,1,1,4,2,1],[2,4,1,2,1,1],
        [4,1,2,1,1,2],[4,1,2,2,1,1],[4,1,2,1,2,1],[3,1,1,1,1,4],[3,1,1,4,1,1],
        [1,4,1,1,3,1],[1,1,1,4,3,1],[1,4,3,1,1,1],[4,1,1,1,4,1],[2,1,1,1,1,5],
        [2,1,1,5,1,1],[1,5,1,1,2,1],[1,1,5,1,2,1],[5,1,1,1,2,1],[2,1,2,1,4,1],
        [2,1,4,1,2,1],[4,1,2,1,2,1],[1,1,1,2,3,4],
    ]
    START_B = 104
    STOP_W  = [2,3,3,1,1,1,2]

    # Build symbol list with checksum
    symbols = [START_B]
    chk = START_B
    for i, ch in enumerate(code):
        v = ord(ch) - 32
        v = max(0, min(v, 95))
        symbols.append(v)
        chk += v * (i + 1)
    symbols.append(chk % 103)

    # Calculate total modules to set SVG width dynamically
    u = 1.9  # module width in px — wider = more readable
    q = 10   # quiet zone px each side
    total_u = sum(sum(W[s]) for s in symbols if s < len(W)) + sum(STOP_W)
    svg_w = int(total_u * u + q * 2 + 2)
    bar_h = height - 16

    rects = []
    x = float(q)

    def _sym(widths):
        nonlocal x
        for idx2, bw in enumerate(widths):
            if idx2 % 2 == 0:  # black bar
                rects.append(
                    f'<rect x="{x:.1f}" y="2" width="{bw*u:.1f}" '
                    f'height="{bar_h}" fill="#000" shape-rendering="crispEdges"/>')
            x += bw * u

    for s in symbols:
        if s < len(W):
            _sym(W[s])
    _sym(STOP_W)

    cx = svg_w / 2
    return (
        f'<svg width="{svg_w}" height="{height}" xmlns="http://www.w3.org/2000/svg">' +
        f'<rect width="{svg_w}" height="{height}" fill="#fff"/>' +
        "".join(rects) +
        f'<text x="{cx:.0f}" y="{height-2}" text-anchor="middle" ' +
        f'font-family="Courier New,monospace" font-size="11" ' +
        f'font-weight="bold" letter-spacing="1.5" fill="#000">{code}</text>' +
        '</svg>'
    )




# ── Streamlit render functions ────────────────────────────────────────────────

def render_patient_card_buttons(
    patient_id: str,
    patient_name: str,
    mobile: str = "",
    rx_r: dict = None,
    rx_l: dict = None,
    visit_date: str = "",
):
    """
    Render patient card print buttons in consultation/billing screen.
    Shows three options: TSC sticker, Evolis plastic card, PDF sheet.
    """
    rx_r = rx_r or {}
    rx_l = rx_l or {}

    try:
        from modules.settings.shop_master import get_unit_info
        si = get_unit_info("retail")
    except:
        si = {"shop_name": "DV Optical", "shop_tagline": ""}

    shop    = si.get("shop_name", "DV Optical")
    tagline = si.get("shop_tagline", "")

    # Ensure patient has a barcode ID
    barcode = ensure_patient_id(patient_id) if patient_id else "PAT000000"

    st.markdown(
        f"<div style='background:#f0fdf4;border:0.5px solid #22c55e;"
        f"border-radius:8px;padding:8px 12px;margin:4px 0'>"
        f"<span style='font-size:11px;font-weight:700;color:#166534'>PATIENT ID</span>"
        f"<span style='font-family:monospace;font-size:14px;font-weight:900;"
        f"color:#15803d;margin-left:10px'>{barcode}</span>"
        f"</div>",
        unsafe_allow_html=True
    )

    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("🖨️ TSC Sticker (75×65mm)",
                     key=f"tsc_card_{patient_id[:8]}",
                     use_container_width=True,
                     help="Print on TSC TTP-244 Pro"):
            tspl = build_tspl_patient_sticker(
                barcode=barcode, name=patient_name, mobile=mobile,
                rx_r=rx_r, rx_l=rx_l, shop=shop
            )
            try:
                from modules.printing.label_printer import _send_tspl
                _send_tspl(tspl)
                st.success("✅ Sent to TSC printer")
            except Exception as ex:
                st.warning(f"Printer offline or not connected: {ex}")
                st.code(tspl, language="text")
                st.caption("Copy TSPL above → paste in TSC Console to print manually")

    with c2:
        if st.button("💳 Evolis Plastic Card",
                     key=f"evolis_card_{patient_id[:8]}",
                     use_container_width=True,
                     help="Print CR80 card on Evolis Primacy"):
            html = _build_evolis_html(
                barcode=barcode, name=patient_name, mobile=mobile,
                rx_r=rx_r, rx_l=rx_l, shop=shop, tagline=tagline,
                visit_date=visit_date
            )
            import base64
            b64 = base64.b64encode(html.encode()).decode()
            js = f"""
            <script>
            var d = atob("{b64}");
            var w = window.open('','_blank');
            w.document.write(d);
            w.document.close();
            </script>"""
            components.html(js, height=0)
            

    with c3:
        # PDF sheet for cutting
        try:
            from modules.documents.card_generator import generate_patient_cards
            from modules.sql_adapter import run_query
            rows = run_query(
                """SELECT id::text as id, barcode,
                          master_name as patient_name, mobile,
                          '' as relation,
                          '' as gender
                   FROM patients WHERE id=%s::uuid LIMIT 1""",
                (patient_id,)
            ) or []
            if rows:
                pdf_bytes = generate_patient_cards(rows, shop_name=shop)
                st.download_button(
                    "📄 Download PDF Sheet",
                    data=pdf_bytes,
                    file_name=f"patient_card_{barcode}.pdf",
                    mime="application/pdf",
                    key=f"pdf_card_{patient_id[:8]}",
                    use_container_width=True,
                    help="A4 sheet — print and cut"
                )
            else:
                st.button("📄 PDF Sheet", disabled=True, use_container_width=True)
        except Exception as ex:
            st.caption(f"PDF: {ex}")


def render_patient_id_badge(patient_id: str, patient_name: str) -> str:
    """
    Returns patient barcode string and renders a small badge inline.
    Use in consultation, reports, referral letter.
    """
    barcode = get_patient_barcode(patient_id) if patient_id else ""
    if barcode:
        svg = barcode_svg(barcode, width=160, height=36)
        st.markdown(
            f"<div style='display:inline-flex;align-items:center;gap:10px;"
            f"background:#f8fafc;border:0.5px solid #e2e8f0;"
            f"border-radius:6px;padding:4px 10px'>"
            f"{svg}"
            f"<span style='font-family:monospace;font-size:11px;color:#374151'>"
            f"{barcode}</span></div>",
            unsafe_allow_html=True
        )
    return barcode
