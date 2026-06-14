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
import logging

logger = logging.getLogger(__name__)


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



def _is_nonzero(v) -> bool:
    try:
        return v is not None and abs(float(v)) > 0.0001
    except Exception:
        return False


def _whole_rupee(v) -> int:
    try:
        return int(round(float(v or 0)))
    except Exception:
        return 0


def _fmt_power(sph, cyl, axis, add):
    """Format lens power for WhatsApp, hiding unused power fields."""
    def _signed(v):
        try:
            n = float(v)
            sign = "+" if n >= 0 else ""
            return f"{sign}{n:.2f}"
        except Exception:
            return None

    def _plain(v):
        try:
            return f"{float(v):.2f}"
        except Exception:
            return None

    parts = []
    s = _signed(sph) if _is_nonzero(sph) else None
    c = _signed(cyl) if _is_nonzero(cyl) else None
    a = str(int(round(float(axis)))) if _is_nonzero(axis) else None
    ad = _plain(add) if _is_nonzero(add) else None
    if s:
        parts.append(f"Sph : {s}")
    if c:
        parts.append(f"Cyl : {c}")
    if c and a:
        parts.append(f"Axis : {a}")
    if ad:
        parts.append(f"Add : {ad}")
    return "  ".join(parts) if parts else ""


def _lens_spec_text(line: dict) -> str:
    """Return ophthalmic spec text to identify product + index + coating."""
    lp = line.get("lens_params") or {}
    if not isinstance(lp, dict):
        return ""

    idx = (
        lp.get("lens_index")
        or lp.get("index")
        or lp.get("index_value")
        or ""
    )
    coating = (
        lp.get("coating")
        or lp.get("coating_type")
        or ""
    )
    treatment = lp.get("treatment") or ""
    suffix = str(lp.get("display_suffix") or "").strip()

    pieces = []
    if idx:
        pieces.append(f"Index {idx}")
    if coating:
        pieces.append(str(coating))
    if treatment and str(treatment).strip().lower() != "clear":
        pieces.append(str(treatment))

    spec = " | ".join(pieces)
    if suffix and not spec:
        spec = suffix.lstrip("+ ").strip()
    return spec


def _build_product_lines(lines: list, ctx: dict = None) -> str:
    """
    Build compact product + power summary for WhatsApp.
    Works for new orders, edits, and backoffice saves.
    Skips lines with no product name (service charges, deleted lines).
    ctx: optional context dict (may contain end_customer_name etc.)
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

        # ── Product display name ──────────────────────────────────────────
        _display_name = pname
        if brand and pname.lower().startswith(brand.lower()):
            _display_name = pname[len(brand):].lstrip(" -_|·")
        if not _display_name:
            _display_name = pname

        # ── Spec text — deduplicate tokens already in product name ────────
        spec_txt = _lens_spec_text(ln)
        if spec_txt:
            _name_lower = _display_name.lower()
            _spec_tokens = [t.strip() for t in spec_txt.split("|")]
            _new_tokens  = []
            for _tok in _spec_tokens:
                _tok_core = _tok.replace("Index ", "").strip()
                if _tok_core and _tok_core.lower() not in _name_lower:
                    _new_tokens.append(_tok.strip())
                elif "Index" in _tok and _tok_core not in _name_lower:
                    _new_tokens.append(_tok.strip())
            spec_txt = " | ".join(_new_tokens)

        prod_txt = f"{_display_name} | {spec_txt}".strip(" |") if spec_txt else _display_name

        # ── Lens parameters (fitting, diameter, fitting height) ───────────
        lp = ln.get("lens_params") or {}
        if isinstance(lp, str):
            try: lp = __import__("json").loads(lp)
            except Exception as _e:
                logger.warning("Suppressed error: %s", _e)
                lp = {}
        _lp_parts = []
        _frame_type    = str(lp.get("frame_type") or "").strip()
        _fitting_type  = str(lp.get("fitting_type") or "").strip()
        _thickness     = str(lp.get("thickness") or "").strip()
        _diameter      = str(lp.get("diameter") or "").strip()
        _fitting_ht    = str(lp.get("fitting_height") or "").strip()
        _corridor      = str(lp.get("corridor") or "").strip()
        _instructions  = str(lp.get("instructions") or "").strip()
        if _frame_type:    _lp_parts.append(f"Frame: {_frame_type}")
        if _fitting_type:  _lp_parts.append(f"Fitting: {_fitting_type}")
        if _thickness and _thickness.lower() not in ("regular",):
            _lp_parts.append(f"Thick: {_thickness}")
        if _diameter:      _lp_parts.append(f"Dia: {_diameter}")
        if _fitting_ht:    _lp_parts.append(f"FH: {_fitting_ht}")
        if _corridor:      _lp_parts.append(f"Corridor: {_corridor}")
        _lp_str = "  |  ".join(_lp_parts)

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
        if _lp_str:
            line_parts.append(f"({_lp_str})")
        if _instructions:
            line_parts.append(f"📝 {_instructions}")
        if total > 0:
            line_parts.append(f"₹{total:,.0f}")

        parts.append("  ".join(line_parts))

    # ── End customer name (appended once at end if present) ───────────────
    _ec_name = ""
    if ctx:
        _ec_name = str(ctx.get("end_customer_name") or "").strip()
    if _ec_name:
        parts.append(f"👤 End Customer: *{_ec_name}*")

    return "\n".join(parts)

def _tpl_retail(ctx):
    s = _si()
    shop    = s.get("shop_name","DV Optical")
    phone   = s.get("shop_phone","")
    total   = _whole_rupee(ctx.get("total",0))
    advance = _whole_rupee(ctx.get("advance",0))
    balance = max(total - advance, 0)
    delivery= ctx.get("delivery_date","")
    prod_lines = _build_product_lines(ctx.get("lines") or [], ctx=ctx)
    return (
        f"Hello {ctx.get('party_name','')} 👋\n\n"
        f"✅ *Order Confirmed!*\n"
        f"📋 Order No: *{ctx.get('order_no','')}*\n"
        f"🏪 {shop}\n"
        + (f"\n📦 *Order Details:*\n{prod_lines}\n" if prod_lines else "\n")
        + f"\n💰 Order Total: ₹{total:,.0f}\n"
        + (f"✅ Previously Paid: ₹{advance:,.0f}\n" if advance > 0 else "")
        + (f"⏳ Balance Due: ₹{balance:,.0f}\n" if balance > 0 else "")
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
    prod_lines = _build_product_lines(ctx.get("lines") or [], ctx=ctx)
    status_label = str(ctx.get("status_label") or "RECEIVED").upper()
    if status_label == "CONFIRMED":
        status_line = "✅ *Wholesale Order Confirmed*"
    else:
        status_line = "📥 *Order Received — Under Review*"
    return (
        f"Hello {ctx.get('party_name','')} 👋\n\n"
        f"{status_line}\n"
        f"📋 Order No: *{ctx.get('order_no','')}*\n"
        f"🏪 {shop}\n"
        + (f"\n📦 *Order Details:*\n{prod_lines}\n" if prod_lines else "\n")
        + f"\n💰 Order Value (incl. GST): ₹{total:,.2f}\n"
        + (f"✅ Previously Paid: ₹{advance:,.2f}\n" if advance > 0 else "")
        + (f"{bal_label}: ₹{balance:,.2f}\n" if balance > 0 else "✅ Fully Paid\n")
        + (
            "\nWe will review and confirm your order shortly. 🙏"
            if status_label != "CONFIRMED"
            else "\nThank you! 🙏"
        )
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
    prod_lines = _build_product_lines(ctx.get("lines") or [], ctx=ctx)
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
    status_label: str = "RECEIVED",
    end_customer_name: str = "",
):
    if str(order_type).upper() == "RETAIL":
        total = _whole_rupee(total)
        advance = _whole_rupee(advance)
    balance = max(round(total - advance, 2), 0)
    s = _si()
    _money_fmt = ",.0f" if str(order_type).upper() == "RETAIL" else ",.2f"

    st.markdown(
        "<div style='background:#0f172a;border:1px solid #334155;"
        "border-radius:10px;padding:12px 16px;margin:8px 0'>"
        "<div style='color:#f59e0b;font-size:0.72rem;font-weight:700;"
        "text-transform:uppercase;letter-spacing:.08em'>🚀 Post-Save Actions</div>"
        "<div style='color:#94a3b8;font-size:0.78rem;margin-top:2px'>"
        f"Order: <b style='color:#e2e8f0'>{order_no}</b> &nbsp;·&nbsp; "
        f"Party: <b style='color:#e2e8f0'>{party_name}</b> &nbsp;·&nbsp; "
        f"Total: <b style='color:#e2e8f0'>₹{total:{_money_fmt}}</b>"
        + (f" &nbsp;·&nbsp; Balance: <b style='color:#f59e0b'>₹{balance:{_money_fmt}}</b>" if balance > 0 else "")
        + "</div></div>",
        unsafe_allow_html=True
    )

    # Use expanders instead of tabs for compatibility
    # ── WhatsApp ──────────────────────────────────────────────────────────────
    with st.expander("💬 WhatsApp — Send order details", expanded=True):
        _render_whatsapp(order_no, party_name, mobile, total, advance, order_type, delivery_date, s, on_account=on_account, lines=lines or [], status_label=status_label, end_customer_name=end_customer_name)

    # ── Print Receipt ─────────────────────────────────────────────────────────
    with st.expander("🖨️ Print Confirmation Receipt", expanded=False):
        _render_print_receipt(order_no, party_name, mobile, total, advance, order_type, delivery_date, s, on_account=on_account, lines=lines or [])

    # ── Patient ID Card (Evolis CR80 card + TSC 75×50 sticker) ──────────────────
    with st.expander("💳 Patient ID Card — Evolis card / TSC 75×50 sticker", expanded=False):
        try:
            from modules.printing.patient_card_printer import render_patient_card_for_order
            render_patient_card_for_order(key_prefix="ps")
        except Exception as _pc_ex:
            st.caption(f"Patient card unavailable: {_pc_ex}")

    # ── Razorpay ──────────────────────────────────────────────────────────────
    with st.expander("💳 Razorpay — Payment Link", expanded=False):
        _render_razorpay(order_no, party_name, balance, s)

    # ── UPI / QR ──────────────────────────────────────────────────────────────
    with st.expander("🏦 UPI / Bank Details & QR", expanded=False):
        _render_upi(order_no, balance, s)


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP
# ══════════════════════════════════════════════════════════════════════════════

def _render_whatsapp(order_no, party_name, mobile, total, advance, order_type, delivery, si, on_account=True, lines=None, status_label="RECEIVED", end_customer_name=""):
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
        try:
            from modules.wa_contact_tools import render_mobile_field
            _mob_in = render_mobile_field(
                f"post_save_{order_no}",
                name=party_name,
                mobile=mobile or "",
                label="Mobile",
            )
        except Exception:
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
        "status_label": status_label,
        "end_customer_name": end_customer_name or "",
    }
    _msg_default = _TEMPLATES[_sel_tpl]["build"](ctx)

    # Editable preview — use session state key so edits persist
    _msg_key = f"wa_msg_{order_no}"
    _sig_key = f"wa_sig_{order_no}"
    _msg_sig = (
        _sel_tpl,
        str(total),
        str(advance),
        str(delivery),
        len(lines or []),
        "|".join(
            str((ln or {}).get("product_name") or "")
            for ln in (lines or [])
            if isinstance(ln, dict)
        ),
    )
    # Refresh message when template changes
    _tpl_key = f"wa_last_tpl_{order_no}"
    _cached_msg = str(st.session_state.get(_msg_key) or "").strip()
    if (
        st.session_state.get(_tpl_key) != _sel_tpl
        or st.session_state.get(_sig_key) != _msg_sig
        or not _cached_msg
    ):
        st.session_state[_msg_key] = _msg_default
        st.session_state[_tpl_key] = _sel_tpl
        st.session_state[_sig_key] = _msg_sig

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
            _eye   = str(_ln.get("eye_side") or "").upper()
            _pname = str(_ln.get("product_name") or "Item")
            _brand = str(_ln.get("brand") or "")

            # Strip brand prefix if it's already at the start of product name
            _display = _pname
            if _brand and _pname.lower().startswith(_brand.lower()):
                _display = _pname[len(_brand):].lstrip(" -_|·")
            if not _display:
                _display = _pname

            # Deduplicate spec tokens already present in display name
            _spec = _lens_spec_text(_ln)
            if _spec:
                _name_lo = _display.lower()
                _kept = []
                for _tok in [t.strip() for t in _spec.split("|")]:
                    _core = _tok.replace("Index ", "").strip()
                    if _core and _core.lower() not in _name_lo:
                        _kept.append(_tok)
                _spec = " | ".join(_kept)
            _pn = f"{_display} | {_spec}".strip(" |") if _spec else _display
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
