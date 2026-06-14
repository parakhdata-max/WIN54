# modules/clinical_print.py
# Clinical Examination HTML Print — scannable barcode on all printouts

import streamlit as st
from datetime import datetime
from typing import Optional


def _real_barcode_svg(val, width=160, height=38):
    """Generate scannable Code128 SVG."""
    val = str(val or "").strip()
    if not val:
        return ""
    try:
        import barcode as _bc, io, re
        from barcode.writer import SVGWriter
        bc = _bc.get("code128", val, writer=SVGWriter())
        buf = io.BytesIO()
        bc.write(buf, options={"write_text": True, "module_height": 10.0,
                               "module_width": 0.25, "quiet_zone": 2.0,
                               "font_size": 7, "text_distance": 1.5})
        svg = buf.getvalue().decode("utf-8")
        svg = svg[svg.find("<svg"):]
        svg = re.sub(r'width="[^"]*"',  f'width="{width}px"',  svg, count=1)
        svg = re.sub(r'height="[^"]*"', f'height="{height}px"', svg, count=1)
        return svg
    except Exception:
        return f"<div style='font-family:monospace;font-size:7pt;border:1px solid #000;padding:2px'>{val}</div>"


def generate_clinical_pdf(patient_id: str, visit_id: Optional[str] = None):
    """
    Generate and open clinical examination printout with scannable barcodes.
    Opens in browser tab for Canon laser printing.
    """
    try:
        from modules.sql_adapter import run_query
        sql = """
            SELECT pc.*, p.master_name, p.mobile, p.dob,
                   COALESCE(p.record_no, p.barcode, p.id::text) AS patient_code
            FROM patient_clinicals pc
            JOIN patients p ON pc.patient_id = p.id
            WHERE pc.patient_id = %(pid)s
        """
        params = {"pid": patient_id}
        if visit_id:
            sql += " AND pc.visit_id = %(vid)s"
            params["vid"] = visit_id
        sql += " ORDER BY pc.created_at DESC LIMIT 1"
        rows = run_query(sql, params) or []
        if not rows:
            st.warning("No clinical data found for this patient.")
            return None
        d = rows[0]
    except Exception as e:
        st.error(f"DB error: {e}")
        return None

    def _v(key, default="N/A"):
        v = d.get(key)
        return str(v).strip() if v and str(v).strip() not in ("", "None", "nan") else default

    patient_name = _v("master_name", "—")
    patient_code = _v("patient_code", patient_id[:8].upper() if patient_id else "")
    mobile       = _v("mobile", "")
    exam_date    = str(d.get("created_at", ""))[:10]
    examiner     = _v("created_by", "—")

    bc_svg = _real_barcode_svg(patient_code)

    def _row(label, r_val, l_val):
        return f"""<tr>
            <td class="lbl">{label}</td>
            <td class="val">{r_val}</td>
            <td class="val">{l_val}</td>
        </tr>"""

    def _sec(title):
        return f"<tr><td colspan='3' class='sec'>{title}</td></tr>"

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Clinical Report — {patient_name}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; font-size: 9pt; color: #000; }}
  .header {{ display: flex; justify-content: space-between; align-items: flex-start;
             border-bottom: 2px solid #000; padding-bottom: 6px; margin-bottom: 8px; }}
  .clinic-name {{ font-size: 14pt; font-weight: 900; }}
  .report-title {{ font-size: 11pt; font-weight: 700; color: #333; margin-top: 2px; }}
  .patient-info {{ font-size: 8.5pt; margin-top: 4px; }}
  .bc-corner {{ text-align: right; }}
  .bc-label {{ font-size: 7pt; color: #555; margin-bottom: 2px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; }}
  th {{ background: #e8e8e8; font-size: 8pt; font-weight: 700;
        border: 1px solid #bbb; padding: 3px 5px; text-align: center; }}
  td {{ border: 1px solid #ccc; padding: 3px 5px; font-size: 8.5pt; vertical-align: top; }}
  td.lbl {{ font-weight: 700; background: #f5f5f5; width: 30%; }}
  td.val {{ width: 35%; }}
  td.sec {{ background: #333; color: #fff; font-weight: 700; font-size: 8.5pt;
            padding: 3px 6px; letter-spacing: 0.05em; }}
  .footer {{ margin-top: 12px; border-top: 1px solid #ccc; padding-top: 6px;
             display: flex; justify-content: space-between; align-items: flex-end;
             font-size: 7.5pt; color: #555; }}
  .sign-line {{ border-top: 1px solid #000; width: 140px; text-align: center;
                padding-top: 2px; font-size: 7.5pt; }}
  @media print {{ button {{ display: none; }} }}
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="clinic-name">Parakh Eye Care</div>
    <div class="report-title">Clinical Examination Report</div>
    <div class="patient-info">
      <b>{patient_name}</b>
      {"&nbsp; | &nbsp;Mob: " + mobile if mobile != "N/A" else ""}
      &nbsp; | &nbsp;Date: {exam_date}
      &nbsp; | &nbsp;Examiner: {examiner}
    </div>
  </div>
  <div class="bc-corner">
    <div class="bc-label">Patient ID: {patient_code}</div>
    {bc_svg}
  </div>
</div>

<!-- VISUAL ACUITY -->
<table>
  <tr><th colspan="3">VISUAL ACUITY</th></tr>
  <tr><th>Measurement</th><th>Right Eye</th><th>Left Eye</th></tr>
  {_row("Distance (Unaided)", _v("va_distance_unaided_r"), _v("va_distance_unaided_l"))}
  {_row("Distance (Aided)",   _v("va_distance_aided_r"),   _v("va_distance_aided_l"))}
  {_row("Near",               _v("va_near_r"),              _v("va_near_l"))}
  {_row("PH",                 _v("va_ph_r"),                _v("va_ph_l"))}
</table>

<!-- REFRACTION -->
<table>
  <tr><th colspan="3">REFRACTION / SUBJECTIVE</th></tr>
  <tr><th>Power</th><th>Right Eye</th><th>Left Eye</th></tr>
  {_row("SPH",  _v("subj_sph_r"),  _v("subj_sph_l"))}
  {_row("CYL",  _v("subj_cyl_r"),  _v("subj_cyl_l"))}
  {_row("AXIS", _v("subj_axis_r"), _v("subj_axis_l"))}
  {_row("ADD",  _v("subj_add_r"),  _v("subj_add_l"))}
  {_row("VA with Rx", _v("subj_va_r"), _v("subj_va_l"))}
</table>

<!-- IOP -->
<table>
  <tr><th colspan="3">INTRAOCULAR PRESSURE (IOP)</th></tr>
  <tr><th></th><th>Right Eye</th><th>Left Eye</th></tr>
  {_row("IOP", _v("iop_r"), _v("iop_l"))}
  {_row("Method", _v("iop_method_r", "—"), _v("iop_method_l", "—"))}
</table>

<!-- SLIT LAMP -->
<table>
  <tr><th colspan="3">SLIT LAMP EXAMINATION</th></tr>
  <tr><th>Structure</th><th>Right Eye</th><th>Left Eye</th></tr>
  {_row("Lids",        _v("sle_lids_r",        _v("sle_lids")),        _v("sle_lids_l",   "—"))}
  {_row("Conjunctiva", _v("sle_conjunctiva_r",  _v("sle_conjunctiva")), _v("sle_conjunctiva_l", "—"))}
  {_row("Cornea",      _v("sle_cornea_r",       _v("sle_cornea")),      _v("sle_cornea_l",  "—"))}
  {_row("A/C",         _v("sle_ac_r",           _v("sle_ac")),          _v("sle_ac_l",      "—"))}
  {_row("Iris",        _v("sle_iris_r",         _v("sle_iris")),        _v("sle_iris_l",    "—"))}
  {_row("Lens",        _v("sle_lens_r",         _v("sle_lens")),        _v("sle_lens_l",    "—"))}
  {_row("Vitreous",    _v("sle_vitreous_r",     _v("sle_vitreous")),    _v("sle_vitreous_l","—"))}
  {_row("Fundus",      _v("sle_fundus_r",       _v("sle_fundus")),      _v("sle_fundus_l",  "—"))}
</table>

<!-- ORTHOPTIC -->
<table>
  <tr><th colspan="3">ORTHOPTIC ASSESSMENT</th></tr>
  {_row("Cover Test (Dist)", _v("ortho_cover_test_distance"), "—")}
  {_row("Cover Test (Near)", _v("ortho_cover_test_near"),     "—")}
  {_row("Nystagmus",         _v("ortho_nystagmus"),           "—")}
  {_row("Ocular Motility",   _v("ortho_ocular_motility"),     "—")}
  {_row("Convergence",       _v("ortho_convergence"),         "—")}
</table>

<!-- DIAGNOSIS / ADVICE -->
{"<table><tr><th colspan=3>DIAGNOSIS</th></tr><tr><td colspan=3>" + _v("diagnosis") + "</td></tr></table>" if d.get("diagnosis") else ""}
{"<table><tr><th colspan=3>ADVICE / PRESCRIPTION</th></tr><tr><td colspan=3>" + _v("advice") + "</td></tr></table>" if d.get("advice") else ""}
{"<table><tr><th colspan=3>REMARKS</th></tr><tr><td colspan=3>" + _v("remarks") + "</td></tr></table>" if d.get("remarks") else ""}

<div class="footer">
  <div>
    <div>Printed: {datetime.now().strftime("%d-%m-%Y %H:%M")}</div>
    <div style="font-size:7pt;color:#999">Patient ID: {patient_code}</div>
  </div>
  <div class="sign-line">Doctor / Optometrist</div>
</div>

<script>window.onload = function(){{ window.print(); }}</script>
</body>
</html>"""

    # Open in browser tab for printing
    try:
        import streamlit.components.v1 as _comp
        import base64 as _b64
        _b64_html = _b64.b64encode(html.encode("utf-8")).decode()
        _js = f"""
            <script>
            var w = window.open('', '_blank');
            var html = atob('{_b64_html}');
            w.document.open();
            w.document.write(html);
            w.document.close();
            </script>
        """
        _comp.html(_js, height=0)
        st.success("✅ Clinical report opened for printing.")
    except Exception as e:
        # Download fallback
        import base64 as _b64
        b64 = _b64.b64encode(html.encode()).decode()
        st.download_button("📄 Download Clinical Report (HTML)",
                           data=html.encode(), file_name=f"clinical_{patient_code}.html",
                           mime="text/html")
    return html


# Legacy helpers kept for compatibility
def format_visual_acuity(clinical_data: dict) -> str:
    va = "VISUAL ACUITY\n"
    va += f"Distance (Unaided): R: {clinical_data.get('va_distance_unaided_r','N/A')} | L: {clinical_data.get('va_distance_unaided_l','N/A')}\n"
    va += f"Distance (Aided):   R: {clinical_data.get('va_distance_aided_r','N/A')} | L: {clinical_data.get('va_distance_aided_l','N/A')}\n"
    va += f"Near:               R: {clinical_data.get('va_near_r','N/A')} | L: {clinical_data.get('va_near_l','N/A')}\n"
    return va

def format_slit_lamp(clinical_data: dict) -> str:
    s = "SLIT LAMP\n"
    for k in ["lids","conjunctiva","cornea","ac","iris","lens","vitreous","fundus"]:
        if clinical_data.get(f"sle_{k}"):
            s += f"{k.capitalize()}: {clinical_data[f'sle_{k}']}\n"
    return s

def format_orthoptic(clinical_data: dict) -> str:
    s = "ORTHOPTIC\n"
    for k in ["cover_test_distance","cover_test_near","nystagmus","ocular_motility","convergence"]:
        s += f"{k.replace('_',' ').title()}: {clinical_data.get(f'ortho_{k}','N/A')}\n"
    return s
