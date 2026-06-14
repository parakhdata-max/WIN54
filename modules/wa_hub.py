"""
modules/wa_hub.py
==================
WhatsApp Hub — single function called from anywhere in the system.
Shows a compact WA send button that opens wa.me with a pre-filled message.
No external API needed. Works on any page, any panel.

Usage:
    from modules.wa_hub import wa_button, wa_link, wa_order_msg

    wa_button(mobile="9876543210", msg="Hello...", key="unique_key")
    # OR
    wa_button(mobile=mobile, msg=wa_order_msg(ctx), key="po_wa")
"""

import urllib.parse
import streamlit as st
from typing import Dict, List, Optional


# ── Link builder ──────────────────────────────────────────────────────────────

def wa_link(mobile: str, msg: str) -> str:
    c = "".join(x for x in (mobile or "") if x.isdigit())
    if c.startswith("91") and len(c) == 12:
        pass
    elif c.startswith("0") and len(c) == 11:
        c = "91" + c[1:]
    elif len(c) >= 10:
        c = "91" + c[-10:]
    if len(c) != 12 or not c.startswith("91"):
        return ""
    return "https://wa.me/{}?text={}".format(c, urllib.parse.quote(msg))


# ── Button ────────────────────────────────────────────────────────────────────

def wa_button(mobile: str, msg: str, key: str,
              label: str = "📲 WhatsApp",
              compact: bool = False,
              use_container_width: bool = False,
              party_name: str = "",
              order_id: str = "",
              patient_id: str = "") -> None:
    """Render a WhatsApp send button.  No mobile → shows disabled button."""
    if party_name or order_id or patient_id:
        try:
            from modules.wa_contact_tools import lookup_mobile, render_mobile_field
            mobile = lookup_mobile(party_name, order_id=order_id, patient_id=patient_id, fallback=mobile)
        except Exception:
            pass
    url = wa_link(mobile, msg)
    if not url:
        try:
            from modules.wa_contact_tools import render_mobile_field
            mobile = render_mobile_field(
                key, name=party_name, mobile=mobile,
                order_id=order_id, patient_id=patient_id,
            )
            url = wa_link(mobile, msg)
        except Exception:
            url = ""
    if not url:
        st.button(label, disabled=True, key=key + "_dis",
                  use_container_width=use_container_width)
        return

    if compact:
        st.markdown(
            "<a href='{url}' target='_blank' style='display:inline-block;"
            "background:#25d366;color:#fff;padding:5px 12px;border-radius:6px;"
            "font-size:.72rem;font-weight:700;text-decoration:none'>{lbl}</a>"
            .format(url=url, lbl=label),
            unsafe_allow_html=True,
        )
    else:
        st.link_button(label, url, use_container_width=use_container_width)


# ── Shop info helper ───────────────────────────────────────────────────────────

def _shop(order_type: str = "retail"):
    try:
        from modules.settings.shop_master import get_unit_info
        _ot = str(order_type or "retail").lower()
        _key = "wholesale" if _ot in ("wholesale", "b2b", "w") else "retail"
        return get_unit_info(_key) or {}
    except Exception:
        return {}


def _fc(v):
    try: return "₹{:,.2f}".format(float(v or 0))
    except Exception: return "₹0.00"


def _safe_text(v) -> str:
    return str(v or "").strip()


def _line_product_name(ln: Dict) -> str:
    parts = []
    oph_specs = ln.get("oph_specs") if isinstance(ln.get("oph_specs"), dict) else {}
    brand = _safe_text(ln.get("brand"))
    product = _safe_text(ln.get("product_name"))
    index = _safe_text(
        ln.get("lens_index")
        or ln.get("index")
        or oph_specs.get("lens_index")
        or oph_specs.get("index")
    )
    coating = _safe_text(
        ln.get("coating")
        or ln.get("lens_coating")
        or oph_specs.get("coating")
    )
    treatment = _safe_text(
        ln.get("treatment")
        or ln.get("colour")
        or ln.get("color")
        or oph_specs.get("treatment")
        or oph_specs.get("colour")
    )
    if brand:
        parts.append(brand)
    if product:
        parts.append(product)
    spec = " | ".join(p for p in [index, coating, treatment] if p)
    if spec and spec.lower() not in " ".join(parts).lower():
        parts.append("(" + spec + ")")
    return " ".join(parts).strip()


def _power_parts(ln: Dict) -> List[str]:
    def _num(v, signed=True, decimals=2):
        if v in (None, "", "—", "-"):
            return ""
        try:
            n = float(v)
            if abs(n) < 0.0001:
                return ""
            return ("{:+.{d}f}" if signed else "{:.{d}f}").format(n, d=decimals)
        except Exception:
            return _safe_text(v)

    parts = []
    sph = _num(ln.get("sph"))
    cyl = _num(ln.get("cyl"))
    add = _num(ln.get("add_power") if ln.get("add_power") is not None else ln.get("add"), signed=False)
    if sph:
        parts.append("Sph : " + sph)
    if cyl:
        parts.append("Cyl : " + cyl)
        axis = ln.get("axis")
        if axis not in (None, "", "—", "-"):
            try:
                ax = int(float(axis))
                if ax:
                    parts.append("Axis : " + str(ax))
            except Exception:
                axis_txt = _safe_text(axis)
                if axis_txt:
                    parts.append("Axis : " + axis_txt)
    if add:
        parts.append("Add : " + add)
    return parts


def wa_document_attachment(document_type: str, document_no: str, label: str = "") -> Dict:
    doc_type = _safe_text(document_type).lower()
    doc_no = _safe_text(document_no)
    nice = label or ("Invoice" if doc_type == "invoice" else "Challan")
    return {
        "type": doc_type,
        "no": doc_no,
        "label": nice,
        "filename": f"{doc_type}_{doc_no.replace('/', '_')}.html",
    }


def _render_attachment_tools(attachments: Optional[List[Dict]], key: str) -> None:
    if not attachments:
        return
    st.markdown("##### Document Copy")
    st.caption("WhatsApp links can fill text only; attach the PDF manually after opening/downloading the document.")
    for idx, att in enumerate(attachments):
        doc_type = _safe_text(att.get("type")).lower()
        doc_no = _safe_text(att.get("no"))
        label = _safe_text(att.get("label")) or doc_type.title()
        filename = _safe_text(att.get("filename")) or f"{doc_type}_{doc_no.replace('/', '_')}.html"
        html = att.get("html")
        if not html and doc_no:
            try:
                if doc_type == "invoice":
                    from modules.billing.smart_print import render_smart_invoice

                    html = render_smart_invoice(doc_no, return_html=True)
                elif doc_type == "challan":
                    from modules.billing.smart_print import render_smart_challan

                    html = render_smart_challan(doc_no, return_html=True)
            except Exception as exc:
                st.caption(f"{label} document unavailable: {exc}")
                html = ""
        if not html:
            continue
        pdf_path = ""
        pdf_bytes = b""
        try:
            from modules.billing.pdf_documents import ensure_document_pdf

            pdf_path, pdf_bytes = ensure_document_pdf(doc_type, doc_no)
        except Exception as exc:
            st.caption(f"{label} PDF unavailable: {exc}")

        c1, c2, c3 = st.columns(3)
        if c1.button(f"Open {label} PDF", key=f"{key}_att_pdf_open_{idx}", use_container_width=True, disabled=not pdf_path):
            try:
                import os

                os.startfile(pdf_path)
                st.success(f"{label} PDF opened: {pdf_path}")
            except Exception as exc:
                st.error(f"Could not open PDF: {exc}")
        if pdf_bytes:
            c2.download_button(
                f"Download {label} PDF",
                data=pdf_bytes,
                file_name=f"{doc_type}_{doc_no.replace('/', '_')}.pdf",
                mime="application/pdf",
                key=f"{key}_att_pdf_dl_{idx}",
                use_container_width=True,
            )
        else:
            c2.button(f"Download {label} PDF", key=f"{key}_att_pdf_dis_{idx}", use_container_width=True, disabled=True)
        if c3.button(f"Open HTML Print", key=f"{key}_att_open_{idx}", use_container_width=True):
            try:
                from modules.printing.print_opener import open_html_print

                path = open_html_print(html, filename)
                st.success(f"{label} opened: {path}")
            except Exception as exc:
                st.error(f"Could not open {label}: {exc}")
        st.download_button(
            f"Download {label} HTML",
            data=html,
            file_name=filename,
            mime="text/html",
            key=f"{key}_att_dl_{idx}",
            use_container_width=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE BUILDERS — one per event
# ══════════════════════════════════════════════════════════════════════════════

def _nl(): return "\n"

def _header(name, shop_name):
    return "Hello {} 👋{nl}{nl}🏪 *{}*{nl}".format(name, shop_name, nl=_nl())


def wa_order_received(party, order_no, total, shop_name="DV Optical", phone=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "✅ *Order Received*" + nl
    m += "📋 Order: *{}*".format(order_no) + nl
    m += "💰 Value: {}".format(_fc(total)) + nl
    m += nl + "We'll review and confirm shortly." + nl
    if phone: m += "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
    return m


def wa_order_confirmed(party, order_no, total, advance=0, lines=None,
                       expected_date="", shop_name="DV Optical", phone=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "Thanks for your order." + nl
    m += "Your order is *Confirmed*." + nl
    m += "📋 Order Number: *{}*".format(order_no) + nl
    if lines:
        m += nl + "📦 *Details of Order:*" + nl
        for ln in lines:
            if not isinstance(ln, dict): continue
            pname = _line_product_name(ln)
            if not pname: continue
            eye = str(ln.get("eye_side") or "").upper()
            eye_lbl = {"R":"*👁 Right*","L":"*👁 Left*","B":"*👁 Both*"}.get(eye,"")
            pw_parts = _power_parts(ln)
            qty = ln.get("billing_qty") or ln.get("quantity") or 0
            try:
                qty_txt = "Qty:{}".format(int(float(qty))) if float(qty or 0) > 0 else ""
            except Exception:
                qty_txt = "Qty:{}".format(qty) if qty else ""
            line_total = (
                ln.get("billing_total")
                if ln.get("billing_total") not in (None, "")
                else ln.get("total_price")
            )
            price_txt = ""
            try:
                if float(line_total or 0) > 0:
                    price_txt = _fc(line_total)
            except Exception:
                price_txt = ""
            row = "  ".join(filter(None, [
                eye_lbl,
                pname,
                qty_txt,
                "[{}]".format("  ".join(pw_parts)) if pw_parts else "",
                price_txt,
            ]))
            m += "  " + row + nl
    if expected_date:
        m += nl + "📅 Expected Date of Supply: *{}*".format(expected_date) + nl
    if advance > 0:
        balance = max(float(total) - float(advance), 0)
        m += nl + "✅ Advance: {}".format(_fc(advance)) + nl
        if balance > 0:
            m += "⏳ Balance: {}".format(_fc(balance)) + nl
    if phone: m += nl + "Queries: " + phone + nl
    m += nl + "Thanks for Choosing Parakh Opticals for your Supplies"
    return m


def wa_job_started(party, order_no, product_name="", shop_name="DV Optical", phone=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "⚙️ *Production Started*" + nl
    m += "📋 Order: *{}*".format(order_no) + nl
    if product_name:
        m += "🔬 Product: {}".format(product_name) + nl
    m += nl + "Your order is in production. We'll update you soon." + nl
    if phone: m += "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
    return m


def wa_job_completed(party, order_no, product_name="", shop_name="DV Optical", phone=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "✅ *Production Completed*" + nl
    m += "📋 Order: *{}*".format(order_no) + nl
    if product_name:
        m += "📦 Product: {}".format(product_name) + nl
    m += nl + "Your order is complete and moving to billing." + nl
    if phone: m += "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
    return m


def wa_ready_for_billing(party, order_no, grand_total, shop_name="DV Optical", phone=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "🧾 *Order Ready for Billing*" + nl
    m += "📋 Order: *{}*".format(order_no) + nl
    m += "💰 Amount: {}".format(_fc(grand_total)) + nl
    m += nl + "Please arrange payment at your earliest." + nl
    if phone: m += "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
    return m


def wa_challan_made(party, order_no, challan_no, grand_total,
                    shop_name="DV Optical", phone=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "📋 *Challan Generated*" + nl
    m += "📦 Challan: *{}*".format(challan_no) + nl
    m += "🔗 Order: {}".format(order_no) + nl
    m += "💰 Amount: {}".format(_fc(grand_total)) + nl
    m += nl + "Your delivery challan is ready. Document copy may be shared separately as PDF." + nl
    if phone: m += "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
    return m


def wa_invoice_made(party, invoice_no, grand_total, balance=0,
                    due_date="", shop_name="DV Optical", phone="", upi_id=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "🧾 *Invoice Generated*" + nl
    m += "📄 Invoice: *{}*".format(invoice_no) + nl
    m += "💰 Amount: {}".format(_fc(grand_total)) + nl
    if balance > 0:
        m += "⏳ Balance Due: *{}*".format(_fc(balance)) + nl
        if due_date:
            m += "📅 Due: {}".format(due_date) + nl
    else:
        m += "✅ Fully Paid" + nl
    if upi_id and balance > 0:
        m += nl + "📱 Pay via UPI: *{}*".format(upi_id) + nl
    m += nl + "Invoice copy may be shared separately as PDF." + nl
    if phone: m += nl + "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
    return m


def wa_dispatched(party, order_no, courier="", tracking="",
                  shop_name="DV Optical", phone="",
                  items=None, order_type="retail",
                  hand_delivery=False):
    """
    Build dispatch WhatsApp message.

    hand_delivery=True or order_type='retail':
        Friendly hand-delivered template with shop contact.
    Otherwise:
        Courier dispatch with product details + tracking.
    """
    nl   = _nl()
    is_hand = (
        hand_delivery
        or str(order_type or "").lower() == "retail"
        or str(courier or "").lower() in ("hand", "hand delivery", "store", "in store")
    )

    # Resolve shop info if not passed
    if not shop_name or shop_name == "DV Optical":
        _sh = _shop(order_type)
        shop_name = _sh.get("shop_name", "DV Optical")
        if not phone:
            phone = _sh.get("shop_phone", "")

    name_parts = str(party or "").split()
    first_name = name_parts[0] if name_parts else str(party)

    if is_hand:
        m  = "Hi " + ("Mr/Ms " + first_name) + "," + nl + nl
        m += "Your order *" + order_no + "* has been delivered by hand"
        m += " from our store. 🙏" + nl + nl
        m += "Hope you are enjoying wearing your Specs / Contact Lenses. 👓" + nl + nl
        m += "We are always here to help. Feel free to call or WhatsApp:" + nl
        m += "📞 *" + phone + "*" + nl + nl
        m += "Thank you for choosing *" + shop_name + "*! 😊"
    else:
        m  = _header(party, shop_name)
        m += "🚚 *Order Dispatched!*" + nl
        m += "📋 Order: *{}*".format(order_no) + nl
        # Product/item details
        if items:
            m += nl + "*Items:*" + nl
            for item in (items or []):
                m += "  • " + str(item) + nl
        m += nl
        if courier:
            m += "📦 Courier: *{}*".format(courier) + nl
        if tracking:
            m += "🔢 Tracking: *{}*".format(tracking) + nl
        m += nl + "Your order is on its way! 🚀" + nl
        if phone:
            m += "Queries: " + phone + nl
        m += nl + "Thank you! 🙏 " + shop_name
    return m


def wa_payment_receipt(party, pno, amount, discount=0, doc_no="",
                       balance=0, mode="CASH", ref="",
                       shop_name="DV Optical", phone=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "✅ *Payment Received*" + nl
    m += "📋 Receipt: *{}*".format(pno) + nl
    m += "💳 Mode: {}".format(mode) + nl
    m += "💰 Amount: *{}*".format(_fc(amount)) + nl
    if discount > 0:
        m += "🎁 Discount: {}".format(_fc(discount)) + nl
    if doc_no:
        m += "🧾 Against: {}".format(doc_no) + nl
    if ref:
        m += "🔖 Ref: {}".format(ref) + nl
    m += nl
    if balance > 0:
        m += "⏳ *Balance: {}*".format(_fc(balance)) + nl
    else:
        m += "✅ *Account Settled!*" + nl
    if phone: m += "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
    return m


# ══════════════════════════════════════════════════════════════════════════════
# INLINE WA PANEL — expandable send box used on any page
# ══════════════════════════════════════════════════════════════════════════════

def wa_panel(mobile: str, msg: str, key: str,
             title: str = "📲 Send WhatsApp",
             expanded: bool = False,
             attachments: Optional[List[Dict]] = None,
             on_sent_callback=None,
             party_name: str = "",
             order_id: str = "",
             patient_id: str = "") -> None:
    """
    Compact WhatsApp panel — shows pre-built message, allows edit, one-click send.
    Use this anywhere you want a WA option without a full post-save panel.
    """
    with st.expander(title, expanded=expanded):
        mob_key = key + "_mob"
        msg_key = key + "_msg"
        try:
            from modules.wa_contact_tools import lookup_mobile, render_mobile_field
            mobile = lookup_mobile(party_name, order_id=order_id, patient_id=patient_id, fallback=mobile)
        except Exception:
            render_mobile_field = None
        if mob_key not in st.session_state:
            st.session_state[mob_key] = mobile or ""
        if msg_key not in st.session_state:
            st.session_state[msg_key] = msg

        c1, c2 = st.columns([2, 3])
        with c1:
            if render_mobile_field:
                mob_in = render_mobile_field(
                    key, name=party_name, mobile=st.session_state.get(mob_key, mobile),
                    order_id=order_id, patient_id=patient_id, label="Mobile",
                )
            else:
                mob_in = st.text_input("Mobile", key=mob_key, placeholder="10-digit number")
        edited  = c2.text_area("Message (edit if needed)", key=msg_key,
                               height=120)
        url = wa_link(mob_in, edited)
        if url:
            st.markdown(
                "<a href='{u}' target='_blank' style='display:block;background:#25d366;"
                "color:#fff;text-align:center;padding:8px;border-radius:6px;"
                "font-weight:700;font-size:.82rem;text-decoration:none'>"
                "📲 Open WhatsApp</a>".format(u=url),
                unsafe_allow_html=True,
            )
        else:
            st.caption("Enter mobile number to enable.")
        # on_sent_callback: fire when user clicks "Mark as Sent"
        if url and on_sent_callback:
            if st.button("✅ Mark as Sent",
                          key=key + "_sent_btn",
                          help="Click after sending WhatsApp"):
                try:
                    on_sent_callback()
                except Exception:
                    pass
        _render_attachment_tools(attachments, key)
