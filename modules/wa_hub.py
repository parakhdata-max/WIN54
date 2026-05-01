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
from typing import Optional


# ── Link builder ──────────────────────────────────────────────────────────────

def wa_link(mobile: str, msg: str) -> str:
    c = "".join(x for x in (mobile or "") if x.isdigit())
    if len(c) == 10:
        c = "91" + c
    if not c:
        return ""
    return "https://wa.me/{}?text={}".format(c, urllib.parse.quote(msg))


# ── Button ────────────────────────────────────────────────────────────────────

def wa_button(mobile: str, msg: str, key: str,
              label: str = "📲 WhatsApp",
              compact: bool = False,
              use_container_width: bool = False) -> None:
    """Render a WhatsApp send button.  No mobile → shows disabled button."""
    url = wa_link(mobile, msg)
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

def _shop():
    try:
        from modules.settings.shop_master import get_unit_info
        return get_unit_info("retail") or {}
    except Exception:
        return {}


def _fc(v):
    try: return "₹{:,.2f}".format(float(v or 0))
    except Exception: return "₹0.00"


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
    m += "✅ *Order Confirmed!*" + nl
    m += "📋 Order: *{}*".format(order_no) + nl
    if lines:
        m += nl + "📦 *Items:*" + nl
        for ln in lines:
            if not isinstance(ln, dict): continue
            pname = str(ln.get("product_name") or "")
            if not pname: continue
            eye = str(ln.get("eye_side") or "").upper()
            brand = str(ln.get("brand") or "")
            eye_lbl = {"R":"👁R","L":"👁L","B":"👁👁"}.get(eye,"")
            pw_parts = []
            for k, lbl in [("sph","Sph"),("cyl","Cyl"),("add_power","Add")]:
                v = ln.get(k)
                if v is not None:
                    try: pw_parts.append("{}{:+.2f}".format(lbl,float(v)))
                    except: pass
            axis = ln.get("axis")
            if axis is not None:
                try: pw_parts.append("Ax{}".format(int(float(axis))))
                except: pass
            prod = ("{} {}".format(brand, pname).strip() if brand else pname)
            row = "  ".join(filter(None,[eye_lbl, prod,
                                         "[{}]".format(" ".join(pw_parts)) if pw_parts else ""]))
            m += "  " + row + nl
    if expected_date:
        m += nl + "📅 *Expected Supply: {}*".format(expected_date) + nl
    if advance > 0:
        balance = max(float(total) - float(advance), 0)
        m += nl + "✅ Advance: {}".format(_fc(advance)) + nl
        if balance > 0:
            m += "⏳ Balance: {}".format(_fc(balance)) + nl
    if phone: m += nl + "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
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
    m += nl + "Your delivery challan is ready." + nl
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
    if phone: m += nl + "Queries: " + phone + nl
    m += nl + "Thank you! 🙏 " + shop_name
    return m


def wa_dispatched(party, order_no, courier="", tracking="",
                  shop_name="DV Optical", phone=""):
    nl = _nl()
    m  = _header(party, shop_name)
    m += "🚚 *Order Dispatched!*" + nl
    m += "📋 Order: *{}*".format(order_no) + nl
    if courier:
        m += "📦 Courier: {}".format(courier) + nl
    if tracking:
        m += "🔖 Tracking: {}".format(tracking) + nl
    m += nl + "Your order is on its way!" + nl
    if phone: m += "Queries: " + phone + nl
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
             expanded: bool = False) -> None:
    """
    Compact WhatsApp panel — shows pre-built message, allows edit, one-click send.
    Use this anywhere you want a WA option without a full post-save panel.
    """
    with st.expander(title, expanded=expanded):
        mob_key = key + "_mob"
        msg_key = key + "_msg"
        if mob_key not in st.session_state:
            st.session_state[mob_key] = mobile or ""
        if msg_key not in st.session_state:
            st.session_state[msg_key] = msg

        c1, c2 = st.columns([2, 3])
        mob_in  = c1.text_input("Mobile", key=mob_key,
                                placeholder="10-digit number")
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
