"""
modules/printing/print_ui.py
==============================
Streamlit print trigger functions — one per document type.
Each opens the HTML in a new browser tab / triggers window.print().
"""
import streamlit as st
import streamlit.components.v1 as components


def _open_print(html: str, height: int = 0):
    """Render HTML in Streamlit — triggers window.print() on load."""
    components.html(html, height=height, scrolling=False)


def render_job_card_label_button(line: dict, order: dict, key: str = ""):
    """Print R and L job card labels for a lens order line."""
    eye  = str(line.get("eye_side","") or "").upper()
    eyes = ["R","L"] if eye == "B" else [eye[0:1]] if eye else ["R","L"]

    for e in eyes:
        lbl = "Right eye" if e == "R" else "Left eye"
        if st.button(f"🖨️ Print {lbl} label", key=f"jc_lbl_{e}_{key}", use_container_width=True):
            from modules.printing.print_templates import job_card_label_html
            html = job_card_label_html(
                order_no     = order.get("order_no",""),
                patient      = order.get("patient_name",""),
                eye          = e,
                sph          = line.get("sph"),
                cyl          = line.get("cyl"),
                axis         = line.get("axis"),
                add          = line.get("add_power"),
                product_name = line.get("product_name",""),
                brand        = line.get("brand",""),
                batch_no     = line.get("batch_no",""),
                shop         = None,
                date         = str(order.get("created_at",""))[:10],
                party        = order.get("party_name",""),
                location     = line.get("location",""),
            )
            _open_print(html, height=320)


def render_invoice_print_button(order: dict, lines: list, invoice_no: str = "", key: str = ""):
    """Print retail invoice."""
    if st.button("🖨️ Print Invoice", key=f"inv_print_{key}", type="primary",
                 use_container_width=True):
        from modules.printing.print_templates import retail_invoice
        rx = _extract_rx(lines)
        html = retail_invoice(
            invoice_no = invoice_no or order.get("order_no",""),
            order_no   = order.get("order_no",""),
            patient    = order.get("patient_name",""),
            mobile     = order.get("patient_mobile",""),
            lines      = lines,
            date       = str(order.get("created_at",""))[:10],
            rx         = rx,
        )
        _open_print(html, height=800)


def render_challan_print_button(challan: dict, lines: list, key: str = ""):
    """Print delivery challan."""
    if st.button("🖨️ Print Challan", key=f"chal_print_{key}", type="primary",
                 use_container_width=True):
        from modules.printing.print_templates import challan as challan_tmpl
        html = challan_tmpl(
            challan_no   = challan.get("challan_no",""),
            party_name   = challan.get("party_name",""),
            customer_no  = challan.get("customer_no",""),
            lines        = lines,
            date         = str(challan.get("challan_date",""))[:10],
            ref_order    = challan.get("order_no",""),
        )
        _open_print(html, height=800)


def render_clinical_print_button(patient: dict, rx_r: dict, rx_l: dict,
                                  optometrist: str = "", date: str = "",
                                  notes: str = "", key: str = ""):
    """Print clinical prescription slip."""
    if st.button("🖨️ Print Prescription", key=f"rx_print_{key}", use_container_width=True):
        from modules.printing.print_templates import clinical_slip
        html = clinical_slip(
            patient     = patient.get("patient_name",""),
            mobile      = patient.get("mobile",""),
            age         = patient.get("age",""),
            gender      = patient.get("gender",""),
            rx_r        = rx_r,
            rx_l        = rx_l,
            optometrist = optometrist,
            date        = date,
            notes       = notes,
        )
        _open_print(html, height=500)


def render_credit_note_button(note: dict, lines: list, key: str = ""):
    """Print credit or debit note."""
    ntype = note.get("note_type","CREDIT")
    if st.button(f"🖨️ Print {ntype} Note", key=f"cn_print_{key}", use_container_width=True):
        from modules.printing.print_templates import credit_debit_note
        html = credit_debit_note(
            note_no    = note.get("note_no",""),
            note_type  = ntype,
            party_name = note.get("party_name",""),
            ref_invoice= note.get("ref_invoice",""),
            lines      = lines,
            date       = str(note.get("date",""))[:10],
            reason     = note.get("reason",""),
        )
        _open_print(html, height=700)


def render_authenticity_card_button(
    order: dict, lines: list,
    customer_name: str = "", from_party: str = "", key: str = ""
):
    """
    Wholesale end-customer authenticity card.
    Shows a text input for end customer name, then prints the card.
    """
    cname = st.text_input(
        "End customer name (for card)",
        value=customer_name or order.get("patient_name",""),
        key=f"auth_cname_{key}",
        placeholder="Name printed on the authenticity card"
    )
    if st.button("🖨️ Print Authenticity Card", key=f"auth_print_{key}",
                 type="primary", use_container_width=True):
        from modules.printing.print_templates import authenticity_card
        rx = _extract_rx(lines)
        # Find frame and lens product names
        frame_line = next((l for l in lines if "frame" in str(l.get("main_group","")).lower()), {})
        lens_line  = next((l for l in lines if "lens" in str(l.get("main_group","")).lower() or
                           "ophthalmic" in str(l.get("main_group","")).lower()), {})
        html = authenticity_card(
            customer_name = cname,
            order_no      = order.get("order_no",""),
            date          = str(order.get("created_at",""))[:10],
            rx_r          = rx.get("R",{}),
            rx_l          = rx.get("L",{}),
            product       = lens_line.get("product_name",""),
            frame         = frame_line.get("product_name",""),
            batch_no      = frame_line.get("batch_no",""),
            from_party    = from_party or order.get("party_name",""),
        )
        _open_print(html, height=500)


def _extract_rx(lines: list) -> dict:
    """Extract R/L prescription from order lines."""
    rx = {"R": {}, "L": {}}
    for l in lines:
        eye = str(l.get("eye_side","")).upper()
        if eye in ("R","RIGHT"):
            rx["R"] = {"sph":l.get("sph"), "cyl":l.get("cyl"),
                       "axis":l.get("axis"), "add":l.get("add_power")}
        elif eye in ("L","LEFT"):
            rx["L"] = {"sph":l.get("sph"), "cyl":l.get("cyl"),
                       "axis":l.get("axis"), "add":l.get("add_power")}
    return rx
