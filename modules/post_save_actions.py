"""
Post-Save Actions — shown after every confirmed order (retail + wholesale)
─────────────────────────────────────────────────────────────────────────
1. WhatsApp  — message preview (editable) + Manual wa.me + Automated stub
2. Razorpay  — button placeholder, API wired when keys ready
3. UPI / QR  — shows stored QR image from Shop Master + account details
"""
import streamlit as st
import streamlit.components.v1
import urllib.parse


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _si():
    """Get shop info safely."""
    try:
        from modules.settings.shop_master import get_unit_info
        return get_unit_info("retail") or {}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

_TEMPLATES = {}

def _reg(tid, name, fn):
    _TEMPLATES[tid] = {"name": name, "build": fn}



def _fmt_power(sph, cyl, axis, add):
    """Format lens power for WhatsApp - compact single line."""
    def _f(v, decimals=2):
        if v is None:
            return None
        try:
            n = float(v)
            sign = "+" if n >= 0 else ""
            return f"{sign}{n:.{decimals}f}"
        except Exception:
            return None
    parts = []
    s = _f(sph)
    c = _f(cyl)
    a = str(int(float(axis))) if axis is not None else None
    ad = _f(add)
    if s:  parts.append(f"Sph:{s}")
    if c:  parts.append(f"Cyl:{c}")
    if a:  parts.append(f"Ax:{a}")
    if ad: parts.append(f"Add:{ad}")
    return "  ".join(parts) if parts else ""


def _build_product_lines(lines: list) -> str:
    """
    Build compact product + power summary for WhatsApp.
    Works for new orders, edits, and backoffice saves.
    Skips lines with no product name (service charges, deleted lines).
    """
    if not lines:
        return ""
    parts = []
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        pname = str(ln.get("product_name") or "")
        if not pname:               # skip unnamed / service lines
            continue
        if ln.get("is_deleted"):    # skip soft-deleted lines
            continue

        eye   = str(ln.get("eye_side") or "").upper()
        brand = str(ln.get("brand") or "")

        # Try every possible qty field — billing_qty may be 0 on edit reloads
        qty = (
            ln.get("billing_qty")
            or ln.get("requested_qty")
            or ln.get("quantity")
            or ln.get("billing_qty_pcs")
            or 0
        )
        try:
            qty = int(float(qty))
        except Exception:
            qty = 0

        total = float(
            ln.get("total_price") or ln.get("billing_total")
            or ln.get("line_total") or 0
        )

        eye_label = {"R": "👁 Right", "L": "👁 Left", "B": "👁👁 Both"}.get(eye, "")
        prod_txt  = f"{brand} {pname}".strip() if brand else pname

        pw = _fmt_power(
            ln.get("sph"), ln.get("cyl"), ln.get("axis"), ln.get("add_power")
        )

        line_parts = []
        if eye_label:
            line_parts.append(f"*{eye_label}*")
        line_parts.append(prod_txt)
        if qty > 0:
            line_parts.append(f"Qty:{qty}")
        if pw:
            line_parts.append(f"[{pw}]")
        if total > 0:
            line_parts.append(f"₹{total:,.0f}")

        parts.append("  ".join(line_parts))

    return "\n".join(parts)

def _tpl_retail(ctx):
    s = _si()
    shop    = s.get("shop_name","DV Optical")
    phone   = s.get("shop_phone","")
    total   = float(ctx.get("total",0))
    advance = float(ctx.get("advance",0))
    balance = max(round(total - advance, 2), 0)
    delivery= ctx.get("delivery_date","")
    prod_lines = _build_product_lines(ctx.get("lines") or [])
    return (
        f"Hello {ctx.get('party_name','')} 👋\n\n"
        f"✅ *Order Confirmed!*\n"
        f"📋 Order No: *{ctx.get('order_no','')}*\n"
        f"🏪 {shop}\n"
        + (f"\n📦 *Order Details:*\n{prod_lines}\n" if prod_lines else "\n")
        + f"\n💰 Order Total: ₹{total:,.2f}\n"
        + (f"✅ Previously Paid: ₹{advance:,.2f}\n" if advance > 0 else "")
        + (f"⏳ Balance Due: ₹{balance:,.2f}\n" if balance > 0 else "")
        + (f"📅 Expected Supply: {delivery}\n" if delivery else "")
        + f"\nQueries: {phone or 'contact the store'}\n"
        f"Thank you for choosing {shop}! 🙏"
    )
_reg("retail_confirmation", "Order Confirmation (Retail)", _tpl_retail)


def _tpl_wholesale(ctx):
    s = _si()
    shop       = s.get("shop_name","DV Optical")
    total      = float(ctx.get("total",0))
    advance    = float(ctx.get("advance",0))
    balance    = max(round(total - advance, 2), 0)
    on_account = ctx.get("on_account", True)
    bal_label  = "📒 Balance on Account" if on_account else "⏳ Balance on Delivery"
    prod_lines = _build_product_lines(ctx.get("lines") or [])
    return (
        f"Hello {ctx.get('party_name','')} 👋\n\n"
        f"✅ *Wholesale Order Confirmed*\n"
        f"📋 Order No: *{ctx.get('order_no','')}*\n"
        f"🏪 {shop}\n"
        + (f"\n📦 *Order Details:*\n{prod_lines}\n" if prod_lines else "\n")
        + f"\n💰 Order Value (incl. GST): ₹{total:,.2f}\n"
        + (f"✅ Previously Paid: ₹{advance:,.2f}\n" if advance > 0 else "")
        + (f"{bal_label}: ₹{balance:,.2f}\n" if balance > 0 else "✅ Fully Paid\n")
        + f"\nThank you! 🙏"
    )
_reg("wholesale_confirmation", "Order Confirmation (Wholesale)", _tpl_wholesale)


def _tpl_balance(ctx):
    s = _si()
    shop    = s.get("shop_name","DV Optical")
    phone   = s.get("shop_phone","")
    total   = float(ctx.get("total",0))
    advance = float(ctx.get("advance",0))
    balance = max(round(total - advance, 2), 0)
    return (
        f"Hello {ctx.get('party_name','')} 👋\n\n"
        f"Reminder from *{shop}* 🏪\n\n"
        f"📋 Order: {ctx.get('order_no','')}\n"
        f"💰 Balance Due: *₹{balance:,.2f}*\n\n"
        f"Please clear at your earliest.\n"
        f"Queries: {phone or 'contact us'}\n\nThank you! 🙏"
    )
_reg("balance_reminder", "Balance Reminder", _tpl_balance)


def _tpl_ready(ctx):
    s = _si()
    shop  = s.get("shop_name","DV Optical")
    phone = s.get("shop_phone","")
    prod_lines = _build_product_lines(ctx.get("lines") or [])
    expected   = ctx.get("expected_date") or ctx.get("delivery_date") or ""
    return (
        f"Hello {ctx.get('party_name','')} 👋\n\n"
        f"🎉 *Your order is Ready!*\n"
        f"📋 Order: *{ctx.get('order_no','')}*\n"
        f"🏪 {shop}\n"
        + (f"\n📦 *Items:*\n{prod_lines}\n" if prod_lines else "\n")
        + (f"📅 Expected Supply: {expected}\n" if expected else "")
        + f"\nPlease collect at your convenience.\n"
        f"Queries: {phone or 'contact us'}\n\nThank you! 🙏"
    )
_reg("order_ready", "Order Ready for Collection", _tpl_ready)


def _tpl_payment(ctx):
    s = _si()
    shop    = s.get("shop_name","DV Optical")
    upi_id  = s.get("shop_upi_id","")
    total   = float(ctx.get("total",0))
    advance = float(ctx.get("advance",0))
    balance = max(round(total - advance, 2), 0)
    return (
        f"Hello {ctx.get('party_name','')} 👋\n\n"
        f"💳 *Payment Request — {shop}*\n"
        f"📋 Order: {ctx.get('order_no','')}\n"
        f"💰 Amount Due: *₹{balance:,.2f}*\n\n"
        + (f"📱 Pay via UPI: *{upi_id}*\n\n" if upi_id else "")
        + f"Please share screenshot after payment.\nThank you! 🙏"
    )
_reg("payment_request", "Payment Request with UPI", _tpl_payment)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

def render_post_save_actions(
    order_no: str,
    party_name: str,
    mobile: str,
    total: float,
    order_type: str,
    advance: float = 0.0,
    delivery_date: str = "",
    on_account: bool = True,
    lines: list = None,
):
    balance = max(round(total - advance, 2), 0)
    s = _si()

    st.markdown(
        "<div style='background:#0f172a;border:1px solid #334155;"
        "border-radius:10px;padding:12px 16px;margin:8px 0'>"
        "<div style='color:#f59e0b;font-size:0.72rem;font-weight:700;"
        "text-transform:uppercase;letter-spacing:.08em'>🚀 Post-Save Actions</div>"
        "<div style='color:#94a3b8;font-size:0.78rem;margin-top:2px'>"
        f"Order: <b style='color:#e2e8f0'>{order_no}</b> &nbsp;·&nbsp; "
        f"Party: <b style='color:#e2e8f0'>{party_name}</b> &nbsp;·&nbsp; "
        f"Total: <b style='color:#e2e8f0'>₹{total:,.2f}</b>"
        + (f" &nbsp;·&nbsp; Balance: <b style='color:#f59e0b'>₹{balance:,.2f}</b>" if balance > 0 else "")
        + "</div></div>",
        unsafe_allow_html=True
    )

    # Use expanders instead of tabs for compatibility
    # ── WhatsApp ──────────────────────────────────────────────────────────────
    with st.expander("💬 WhatsApp — Send order details", expanded=True):
        _render_whatsapp(order_no, party_name, mobile, total, advance, order_type, delivery_date, s, on_account=on_account, lines=lines or [])

    # ── Print Receipt ─────────────────────────────────────────────────────────
    with st.expander("🖨️ Print Confirmation Receipt", expanded=False):
        _render_print_receipt(order_no, party_name, mobile, total, advance, order_type, delivery_date, s, on_account=on_account, lines=lines or [])

    # ── Razorpay ──────────────────────────────────────────────────────────────
    with st.expander("💳 Razorpay — Payment Link", expanded=False):
        _render_razorpay(order_no, party_name, balance, s)

    # ── UPI / QR ──────────────────────────────────────────────────────────────
    with st.expander("🏦 UPI / Bank Details & QR", expanded=False):
        _render_upi(order_no, balance, s)


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP
# ══════════════════════════════════════════════════════════════════════════════

def _render_whatsapp(order_no, party_name, mobile, total, advance, order_type, delivery, si, on_account=True, lines=None):
    # Template selector
    _default = "retail_confirmation" if order_type == "RETAIL" else "wholesale_confirmation"
    _tpl_keys  = list(_TEMPLATES.keys())
    _tpl_names = [_TEMPLATES[k]["name"] for k in _tpl_keys]
    _def_idx   = _tpl_keys.index(_default) if _default in _tpl_keys else 0

    tc1, tc2 = st.columns([2, 1])
    with tc1:
        _sel_idx = st.selectbox(
            "Template",
            options=range(len(_tpl_keys)),
            format_func=lambda i: _tpl_names[i],
            index=_def_idx,
            key=f"wa_tpl_{order_no}",
            label_visibility="collapsed"
        )
        _sel_tpl = _tpl_keys[_sel_idx]
    with tc2:
        _mob_in = st.text_input(
            "Mobile",
            value=mobile or "",
            key=f"wa_mob_{order_no}",
            placeholder="10-digit number",
            label_visibility="collapsed"
        )

    # Build message
    ctx = {
        "order_no": order_no, "party_name": party_name,
        "total": total, "advance": advance,
        "delivery_date": delivery,
        "on_account": on_account,
        "lines": lines or [],
    }
    _msg_default = _TEMPLATES[_sel_tpl]["build"](ctx)

    # Editable preview — use session state key so edits persist
    _msg_key = f"wa_msg_{order_no}"
    # Refresh message when template changes
    _tpl_key = f"wa_last_tpl_{order_no}"
    if st.session_state.get(_tpl_key) != _sel_tpl:
        st.session_state[_msg_key] = _msg_default
        st.session_state[_tpl_key] = _sel_tpl

    _edited = st.text_area(
        "Edit message before sending",
        key=_msg_key,
        height=160,
        label_visibility="collapsed"
    )
    st.caption("✏️ Edit the message above if needed before sending")

    # Buttons
    _mob_clean = "".join(c for c in (_mob_in or "") if c.isdigit())
    _wa_num    = "91" + _mob_clean[-10:] if len(_mob_clean) >= 10 else ""
    _final_msg = st.session_state.get(_msg_key, _msg_default)

    b1, b2, b3 = st.columns(3)

    with b1:
        # Manual — wa.me opens WhatsApp with pre-filled message
        if _wa_num:
            _wa_url = f"https://wa.me/{_wa_num}?text={urllib.parse.quote(_final_msg)}"
            # Use markdown link as universal fallback (works on all Streamlit versions)
            st.markdown(
                f"<a href='{_wa_url}' target='_blank' style='"
                f"display:block;background:#25d366;color:#fff;text-align:center;"
                f"padding:8px 12px;border-radius:6px;font-weight:700;"
                f"font-size:0.82rem;text-decoration:none;margin-top:4px'>"
                f"📱 Manual (wa.me)</a>",
                unsafe_allow_html=True
            )
            st.caption(f"Opens WhatsApp for {_mob_clean[-10:]}")
        else:
            st.button("📱 Manual (wa.me)", disabled=True,
                      key=f"wa_man_{order_no}", use_container_width=True)
            st.caption("Enter mobile number above")

    with b2:
        if st.button("🤖 Automated (API)",
                     key=f"wa_auto_{order_no}",
                     use_container_width=True):
            st.info(
                "WhatsApp Business API — coming soon.\n"
                "Settings → Integrations → WhatsApp API",
                icon="🤖"
            )

    with b3:
        if st.button("📋 Copy Message",
                     key=f"wa_copy_{order_no}",
                     use_container_width=True):
            st.code(_final_msg, language=None)
            st.caption("↑ Select all and copy")


# ══════════════════════════════════════════════════════════════════════════════
# RAZORPAY
# ══════════════════════════════════════════════════════════════════════════════

def _render_razorpay(order_no, party_name, balance, si):
    st.markdown(
        "<div style='color:#a78bfa;font-size:0.78rem;margin-bottom:8px'>"
        "Generate a Razorpay payment link and share with customer. "
        "Customer pays online — you get notified.</div>",
        unsafe_allow_html=True
    )
    rc1, rc2 = st.columns([2, 1])
    with rc1:
        if st.button(
            f"🔗 Generate Razorpay Link  ₹{balance:,.2f}",
            key=f"rzp_{order_no}",
            use_container_width=True,
            type="primary"
        ):
            # ── WIRE HERE when keys are ready ────────────────────────────
            # rzp_key = si.get("razorpay_key_id","")
            # rzp_sec = si.get("razorpay_key_secret","")
            # import requests, base64
            # auth = base64.b64encode(f"{rzp_key}:{rzp_sec}".encode()).decode()
            # resp = requests.post(
            #     "https://api.razorpay.com/v1/payment_links",
            #     json={"amount": int(balance*100), "currency":"INR",
            #           "description": f"Order {order_no}",
            #           "customer": {"name": party_name}},
            #     headers={"Authorization": f"Basic {auth}"}
            # )
            # short_url = resp.json().get("short_url","")
            # st.success(f"✅ Link generated: {short_url}")
            # ─────────────────────────────────────────────────────────────
            st.info(
                "Configure Razorpay API keys in Settings → Integrations → Razorpay.\n"
                "Once keys are added, this button will generate and show the payment link.",
                icon="⏳"
            )
    with rc2:
        st.metric("Amount", f"₹{balance:,.2f}")
        st.caption(f"Order: {order_no}")



# ══════════════════════════════════════════════════════════════════════════════
# PRINT RECEIPT
# ══════════════════════════════════════════════════════════════════════════════

def _render_print_receipt(order_no, party_name, mobile, total, advance, order_type, delivery, si, on_account=True, lines=None):
    """Printable HTML receipt — same style for retail and wholesale."""
    balance    = max(round(total - advance, 2), 0)
    shop       = si.get("shop_name","DV Optical")
    shop_addr  = si.get("shop_address","")
    shop_phone = si.get("shop_phone","")
    shop_gstin = si.get("gstin","")
    import datetime as _dt
    date_str = _dt.date.today().strftime("%d/%m/%Y")
    order_tag  = "RETAIL" if order_type == "RETAIL" else "WHOLESALE"
    bal_label  = "Balance on Account" if on_account else "Balance on Delivery"

    # Build line items table for detailed receipt
    lines_html = ""
    if lines:
        _rows = ""
        for _ln in lines:
            _eye  = str(_ln.get("eye_side") or "").upper()
            _pn   = str(_ln.get("product_name") or "Item")
            _qty  = int(_ln.get("billing_qty") or _ln.get("quantity") or 0)
            _up   = float(_ln.get("unit_price") or 0)
            _tp   = float(_ln.get("total_price") or round(_up * _qty, 2))
            _eye_tag = f"[{_eye}] " if _eye in ("R", "L", "B") else ""
            _rows += (
                f"<tr>"
                f"<td style='padding:2px 4px'>{_eye_tag}{_pn}</td>"
                f"<td style='padding:2px 4px;text-align:center'>{_qty}</td>"
                f"<td style='padding:2px 4px;text-align:right'>₹{_tp:,.2f}</td>"
                f"</tr>"
            )
        lines_html = (
            "<div style='border-bottom:1px dashed #000;padding-bottom:6px;margin-bottom:6px'>"
            "<div style='font-weight:700;margin-bottom:3px'>Items:</div>"
            "<table style='width:100%;font-size:0.78rem;border-collapse:collapse'>"
            "<tr style='border-bottom:1px solid #ccc'>"
            "<th style='text-align:left;padding:2px 4px'>Product</th>"
            "<th style='text-align:center;padding:2px 4px'>Qty</th>"
            "<th style='text-align:right;padding:2px 4px'>Amount</th>"
            "</tr>"
            f"{_rows}"
            "</table></div>"
        )

    html = f"""
    <div id="dvprint" style="font-family:monospace;max-width:320px;margin:0 auto;
         padding:16px;background:#fff;color:#000;font-size:0.82rem;line-height:1.6">
      <div style="text-align:center;border-bottom:2px solid #000;padding-bottom:8px;margin-bottom:8px">
        <div style="font-size:1.1rem;font-weight:900">{shop}</div>
        {f"<div>{shop_addr}</div>" if shop_addr else ""}
        {f"<div>📞 {shop_phone}</div>" if shop_phone else ""}
        {f"<div>GSTIN: {shop_gstin}</div>" if shop_gstin else ""}
      </div>
      <div style="text-align:center;margin-bottom:8px">
        <span style="background:#000;color:#fff;padding:2px 10px;font-weight:700">
          ORDER CONFIRMATION — {order_tag}
        </span>
      </div>
      <div style="border-bottom:1px dashed #000;padding-bottom:6px;margin-bottom:6px">
        <div>Order No : <b>{order_no}</b></div>
        <div>Date     : {date_str}</div>
        <div>Party    : <b>{party_name}</b></div>
        {f"<div>Mobile   : {mobile}</div>" if mobile else ""}
        {f"<div>Delivery : {delivery}</div>" if delivery else ""}
      </div>
      {lines_html}
      <div style="border-bottom:1px dashed #000;padding-bottom:6px;margin-bottom:6px">
        <div style="display:flex;justify-content:space-between">
          <span>Order Total (incl. GST)</span><span><b>₹{total:,.2f}</b></span>
        </div>
        {f'<div style="display:flex;justify-content:space-between"><span>Advance Paid</span><span style="color:green"><b>₹{advance:,.2f}</b></span></div>' if advance > 0 else ""}
        {f'<div style="display:flex;justify-content:space-between"><span>{bal_label}</span><span><b>₹{balance:,.2f}</b></span></div>' if balance > 0 else '<div style="text-align:center;color:green"><b>✅ Fully Paid</b></div>'}
      </div>
      <div style="text-align:center;font-size:0.75rem;margin-top:8px">
        Thank you for your business! 🙏
      </div>
    </div>
    <button onclick="
      var w=window.open('','_blank');
      w.document.write(document.getElementById('dvprint').outerHTML);
      w.document.close(); w.print();
    " style="margin-top:10px;padding:8px 20px;background:#1e40af;color:#fff;
             border:none;border-radius:6px;cursor:pointer;font-weight:700;width:100%">
      🖨️ Print Receipt
    </button>
    """
    st.components.v1.html(html, height=480, scrolling=False)

# ══════════════════════════════════════════════════════════════════════════════
# UPI / BANK QR
# ══════════════════════════════════════════════════════════════════════════════

def _render_upi(order_no, balance, si):
    upi_id    = si.get("shop_upi_id","")
    bank_name = si.get("bank_name","")
    acc_no    = si.get("bank_account","")
    ifsc      = si.get("bank_ifsc","")
    bank_branch = si.get("bank_branch","")
    shop_name = si.get("shop_name","DV Optical")
    stored_qr = si.get("upi_qr_image","")

    if not upi_id and not acc_no and not stored_qr:
        st.info("Add UPI ID and bank details in Settings → Shop Master → Bank section.", icon="ℹ️")
        return

    uc1, uc2 = st.columns([1, 1])

    with uc1:
        lines = []
        if shop_name:  lines.append(f"**{shop_name}**")
        if upi_id:     lines.append(f"📱 UPI: `{upi_id}`")
        if bank_name:  lines.append(f"🏦 Bank: {bank_name}")
        if acc_no:     lines.append(f"Account: `{acc_no}`")
        if ifsc:       lines.append(f"IFSC: `{ifsc}`")
        if bank_branch:lines.append(f"Branch: {bank_branch}")
        lines.append(f"\n💰 **Amount Due: ₹{balance:,.2f}**")
        st.markdown("\n\n".join(lines))

    with uc2:
        if stored_qr:
            try:
                import base64 as _b64
                st.image(_b64.b64decode(stored_qr), caption="Scan to Pay", width=180)
            except Exception as e:
                st.caption(f"QR display error: {e}")
        elif upi_id:
            st.markdown(
                f"<div style='background:#1e293b;border-radius:8px;padding:20px;"
                f"text-align:center;color:#94a3b8'>"
                f"<div style='font-size:2rem'>📱</div>"
                f"<div style='font-size:0.8rem;margin-top:6px'>{upi_id}</div>"
                f"<div style='font-size:0.7rem;margin-top:8px;color:#64748b'>"
                f"Upload QR image in<br>Settings → Shop Master</div></div>",
                unsafe_allow_html=True
            )
