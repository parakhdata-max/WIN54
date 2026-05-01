import streamlit as st
import io
import base64


def generate_barcode_b64(patient_id: str) -> str:
    """
    Generates a Code128 barcode for the given patient_id
    and returns it as a base64-encoded PNG string.
    """
    try:
        from barcode import Code128
        from barcode.writer import ImageWriter

        buffer = io.BytesIO()
        Code128(str(patient_id), writer=ImageWriter()).write(buffer)
        buffer.seek(0)
        return base64.b64encode(buffer.read()).decode("utf-8")
    except ImportError:
        return None


def render_patient_label(patient: dict, rx_r: dict, rx_l: dict):
    """
    Renders a 75×55mm style patient label with prescription table and barcode.

    Usage:
        render_patient_label(
            patient={"id": "P-001", "name": "Ramesh Kumar", "mobile": "9999999999"},
            rx_r={"sph": "-1.00", "cyl": "-0.50", "axis": "180", "add": ""},
            rx_l={"sph": "-1.25", "cyl": "-0.75", "axis": "170", "add": ""}
        )
    """
    with st.expander("🏷️ Patient Label (75×55 mm)", expanded=False):

        barcode_b64 = generate_barcode_b64(patient.get("id", ""))

        barcode_html = (
            f'<img src="data:image/png;base64,{barcode_b64}" '
            f'style="width:100%; margin-top:6px;"/>'
            if barcode_b64
            else "<i style='font-size:10px; color:gray;'>Barcode unavailable (install python-barcode)</i>"
        )

        html = f"""
        <div style="
            width: 280px;
            min-height: 200px;
            border: 1px solid black;
            padding: 8px;
            font-family: Arial;
            font-size: 12px;
        ">
            <b style="font-size:14px;">{patient.get('name', '')}</b><br>
            📞 {patient.get('mobile', '')}<br><br>

            <table style="font-size:11px; border-collapse:collapse; width:100%;">
                <tr style="background:#f0f0f0;">
                    <th style="border:1px solid #ccc; padding:2px;"></th>
                    <th style="border:1px solid #ccc; padding:2px;">SPH</th>
                    <th style="border:1px solid #ccc; padding:2px;">CYL</th>
                    <th style="border:1px solid #ccc; padding:2px;">AXIS</th>
                    <th style="border:1px solid #ccc; padding:2px;">ADD</th>
                </tr>
                <tr>
                    <td style="border:1px solid #ccc; padding:2px;"><b>R</b></td>
                    <td style="border:1px solid #ccc; padding:2px;">{rx_r.get('sph', '')}</td>
                    <td style="border:1px solid #ccc; padding:2px;">{rx_r.get('cyl', '')}</td>
                    <td style="border:1px solid #ccc; padding:2px;">{rx_r.get('axis', '')}</td>
                    <td style="border:1px solid #ccc; padding:2px;">{rx_r.get('add', '')}</td>
                </tr>
                <tr>
                    <td style="border:1px solid #ccc; padding:2px;"><b>L</b></td>
                    <td style="border:1px solid #ccc; padding:2px;">{rx_l.get('sph', '')}</td>
                    <td style="border:1px solid #ccc; padding:2px;">{rx_l.get('cyl', '')}</td>
                    <td style="border:1px solid #ccc; padding:2px;">{rx_l.get('axis', '')}</td>
                    <td style="border:1px solid #ccc; padding:2px;">{rx_l.get('add', '')}</td>
                </tr>
            </table>

            {barcode_html}

            <div style="font-size:10px; text-align:center; margin-top:4px; color:#555;">
                Check Regularly · See Clearly
            </div>
        </div>
        """

        st.components.v1.html(html, height=280)
