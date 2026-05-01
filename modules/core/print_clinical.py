import streamlit as st


def render_printable_clinical(rx_r: dict, rx_l: dict, patient_name: str):
    """Renders a printable clinical prescription table inside an expander."""
    with st.expander("🩺 Printable Clinical Summary", expanded=False):

        def row(rx, eye):
            return f"""
            <tr>
                <td><b>{eye}</b></td>
                <td>{rx.get('sph', '')}</td>
                <td>{rx.get('cyl', '')}</td>
                <td>{rx.get('axis', '')}</td>
                <td>{rx.get('add', '')}</td>
            </tr>
            """

        html = f"""
        <div style="font-family: Arial; width: 600px;">
            <h3>Clinical Prescription</h3>
            <b>Patient:</b> {patient_name}
            <br><br>
            <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse;">
                <tr style="background:#f0f0f0;">
                    <th>Eye</th>
                    <th>SPH</th>
                    <th>CYL</th>
                    <th>AXIS</th>
                    <th>ADD</th>
                </tr>
                {row(rx_r, "R")}
                {row(rx_l, "L")}
            </table>
        </div>
        """

        st.components.v1.html(html, height=350)
