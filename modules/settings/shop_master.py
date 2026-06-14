"""
modules/settings/shop_master.py
================================
Shop / Company Master — like "Create Company" in Tally.
Stores in system_flags table. Used by all print templates.
"""
import streamlit as st

_FIELDS = [
    ("shop_name",           "Shop / Clinic Name",      "DV Optical",              "Basic"),
    ("shop_tagline",        "Tagline",                 "Your vision, our care",   "Basic"),
    ("shop_address",        "Address Line 1",          "123, Main Road",          "Basic"),
    ("shop_address2",       "Address Line 2",          "Near Bus Stand",          "Basic"),
    ("shop_city",           "City",                    "Nagpur",                  "Basic"),
    ("shop_state",          "State",                   "Maharashtra",             "Basic"),
    ("shop_pincode",        "PIN Code",                "440001",                  "Basic"),
    ("shop_phone",          "Phone / Mobile",          "9876543210",              "Contact"),
    ("shop_mobile2",        "Alternate Mobile",        "",                        "Contact"),
    ("shop_email",          "Email",                   "dvoptical@gmail.com",     "Contact"),
    ("shop_website",        "Website",                 "www.dvoptical.com",       "Contact"),
    ("shop_gstin",          "GSTIN",                   "27XXXXX0000X1ZX",         "GST & Legal"),
    ("shop_pan",            "PAN Number",              "XXXXX0000X",              "GST & Legal"),
    ("shop_drug_lic",       "Drug Licence No.",        "(if applicable)",         "GST & Legal"),
    ("shop_dl_exp",         "Drug Licence Expiry",     "DD/MM/YYYY",              "GST & Legal"),
    ("bank_name",           "Bank Name",               "SBI",                     "Bank"),
    ("bank_account",        "Account Number",          "00000000000",             "Bank"),
    ("bank_ifsc",           "IFSC Code",               "SBIN0000000",             "Bank"),
    ("bank_branch",         "Branch",                  "Nagpur Main",             "Bank"),
    ("shop_upi_id",         "UPI ID",                  "Q29827914@ybl",           "Bank"),
    ("upi_qr_image",        "UPI QR Code Image",       "",                        "Bank"),
    ("print_footer",        "Print Footer Line",       "", "Prints"),
    ("frame_barcode_print_name", "Frame Barcode Print Name", "Parakh",            "Prints"),
    ("document_print_mode", "Document Print Mode",     "DIRECT_THEN_HTML",        "Prints"),
    ("consult_fee_default", "Default Consult Fee",     "200",                     "Prints"),
    ("invoice_prefix",      "Invoice Prefix",          "INV",                     "Prints"),
    ("challan_prefix",      "Challan Prefix",          "CHL",                     "Prints"),
    # Business Units — different brand names for different channels
    ("unit_retail_name",    "Retail Brand Name",       "Parakh Eye Care",         "Business Units"),
    ("unit_retail_tagline", "Retail Tagline",          "Clinics by Parakh Opticals", "Business Units"),
    ("unit_wholesale_name", "Wholesale Brand Name",    "Parakh Opticals",         "Business Units"),
    ("unit_wholesale_tagline","Wholesale Tagline",     "Wholesale Division",       "Business Units"),
    ("unit_online_name",    "Online Brand Name",       "Ultrasight",              "Business Units"),
    ("unit_online_tagline", "Online Tagline",          "Shop Online",             "Business Units"),
    # Consultation — stored as JSON, not rendered by the generic _FIELDS loop
    # (rendered separately in render_shop_master via render_consultation_settings)
    ("consultation_types",  "",                        "",                        "_internal"),
]

ORDER_PIPELINE_STATUSES = [
    "PENDING", "PROVISIONAL", "UNDER_REVIEW", "CONFIRMED", "IN_PRODUCTION",
    "READY", "READY_FOR_BILLING", "PARTIALLY_BILLED", "BILLED", "DISPATCHED",
    "DELIVERED", "CLOSED", "CANCELLED",
]

DEFAULT_EDIT_STATUSES = ["PENDING", "PROVISIONAL", "UNDER_REVIEW"]
DEFAULT_CANCEL_STATUSES = ["PENDING", "PROVISIONAL", "UNDER_REVIEW", "HOLD", "CREDIT_HOLD", "PENDING_PAYMENT"]


import streamlit as st


@st.cache_data(ttl=60, show_spinner=False)
def _load_all_flags() -> dict:
    """Load all system_flags in ONE query, cache 60s. Replaces 30 individual DB hits."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("SELECT key, value FROM system_flags") or []
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


def _get(key, default=""):
    flags = _load_all_flags()
    return flags.get(key, default) or default


def _invalidate_shop_cache():
    """Call after saving settings so next read picks up new values."""
    _load_all_flags.clear()


def _set(key, value):
    conn = None
    cursor = None
    try:
        from modules.sql_adapter import get_transaction_connection
        conn = get_transaction_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO system_flags (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """, (str(key), "" if value is None else str(value)))
        conn.commit()
        return True
    except Exception as ex:
        if conn:
            conn.rollback()
        st.error(f"Save failed ({key}): {ex}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _csv_to_list(value: str) -> list[str]:
    return [
        x.strip()
        for x in str(value or "").replace(";", ",").split(",")
        if x and x.strip()
    ]


@st.cache_data(ttl=300, show_spinner=False)
def _load_product_brands() -> list[str]:
    """Distinct product brands for production-routing setup."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT DISTINCT TRIM(brand) AS brand
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND COALESCE(TRIM(brand), '') <> ''
            ORDER BY TRIM(brand)
        """, {}) or []
        return [str(r.get("brand") or "").strip() for r in rows if r.get("brand")]
    except Exception:
        return []


def get_inhouse_lab_brands() -> set[str]:
    """Brands that default to in-house production routing."""
    return {b.lower() for b in _csv_to_list(_get("inhouse_lab_brands", ""))}


def get_order_action_statuses(action: str) -> set[str]:
    """Admin-configured status gates for edit/cancel controls."""
    action = str(action or "").lower().strip()
    if action == "cancel":
        raw = _get("order_cancel_allowed_statuses", ",".join(DEFAULT_CANCEL_STATUSES))
        fallback = DEFAULT_CANCEL_STATUSES
    else:
        raw = _get("order_edit_allowed_statuses", ",".join(DEFAULT_EDIT_STATUSES))
        fallback = DEFAULT_EDIT_STATUSES
    values = _csv_to_list(raw) or list(fallback)
    return {v.upper() for v in values}


def get_shop_info() -> dict:
    """Return all shop settings — use in print templates."""
    return {f[0]: _get(f[0], f[2]) for f in _FIELDS}


def get_unit_info(unit: str = "retail") -> dict:
    """
    Return shop info customised for a specific business unit.
    unit: "retail" | "wholesale" | "online"
    
    Returns dict with shop_name, shop_tagline overridden by unit-specific values.
    All other fields (address, GSTIN, phone) remain same — legal entity unchanged.
    """
    base = get_shop_info()
    unit = unit.lower().strip()

    unit_name    = base.get(f"unit_{unit}_name", "").strip()
    unit_tagline = base.get(f"unit_{unit}_tagline", "").strip()

    if unit_name:
        base["shop_name"]    = unit_name
        base["shop_tagline"] = unit_tagline
        base["_unit"]        = unit
    else:
        base["_unit"] = "default"

    return base


def render_shop_master():
    st.markdown(
        "<div style='background:#0f172a;border-left:4px solid #f59e0b;"
        "padding:10px 16px;border-radius:6px;margin-bottom:16px'>"
        "<b style='color:#f59e0b;font-size:1rem'>🏪 Shop / Company Master</b>"
        "<span style='color:#94a3b8;font-size:0.78rem;margin-left:10px'>"
        "Used in all prints — invoice, clinical report, labels</span>"
        "</div>", unsafe_allow_html=True
    )

    # Group by section — skip _internal (rendered separately below)
    sections = {}
    for f in _FIELDS:
        if f[3] != "_internal":
            sections.setdefault(f[3], []).append(f)

    # Load current values
    current = {f[0]: _get(f[0], "") for f in _FIELDS}

    edited = {}
    _refresh = int(st.session_state.get("shop_master_refresh", 0))
    for sec, fields in sections.items():
        st.markdown(f"**{sec}**")
        rows = [fields[i:i+2] for i in range(0, len(fields), 2)]
        for row in rows:
            cols = st.columns(len(row))
            for col, (key, label, placeholder, _) in zip(cols, row):
                edited[key] = col.text_input(
                    label,
                    value=current.get(key, ""),
                    placeholder=placeholder,
                    key=f"sm_{key}_{_refresh}"
                )
        st.markdown("---")

    # ── UPI QR Image Upload (Bank section) ──────────────────────────────────
    st.markdown("**UPI QR Code**")
    _qr_c1, _qr_c2 = st.columns([1, 2])
    with _qr_c1:
        _current_qr = _get("upi_qr_image", "")
        if _current_qr:
            try:
                import base64 as _b64
                _qr_bytes = _b64.b64decode(_current_qr)
                st.image(_qr_bytes, caption="Current QR", width=160)
                if st.button("🗑️ Remove QR Image", key="sm_remove_qr"):
                    _set("upi_qr_image", "")
                    st.success("✅ QR removed")
                    st.rerun()
            except Exception:
                st.caption("(stored image invalid — re-upload)")
        else:
            st.caption("No QR image uploaded yet")
    with _qr_c2:
        _uploaded_qr = st.file_uploader(
            "Upload UPI QR code image (PNG/JPG)",
            type=["png","jpg","jpeg"],
        key="sm_upi_qr_upload",
            help="Scan-to-pay QR shown on receipts and post-save panel"
        )
        if _uploaded_qr:
            import base64 as _b64u
            _qr_b64 = _b64u.b64encode(_uploaded_qr.read()).decode()
            if _set("upi_qr_image", _qr_b64):
                st.success("✅ QR image saved")
                st.rerun()
        st.caption("Upload the QR image from your bank/UPI app. "
                   "Shown in post-save panel after order confirmation.")
    st.markdown("---")

    # ── Production Routing ─────────────────────────────────────────────────
    st.markdown("**Production Routing**")
    _brand_options = _load_product_brands()
    _current_inhouse = _csv_to_list(_get("inhouse_lab_brands", ""))
    _current_known = [b for b in _current_inhouse if b in _brand_options]
    _custom_current = [b for b in _current_inhouse if b not in _brand_options]
    edited["inhouse_lab_brands"] = ",".join(st.multiselect(
        "In-house lab brand(s)",
        options=_brand_options,
        default=_current_known,
        key=f"sm_inhouse_lab_brands_{_refresh}",
        help="Ophthalmic products from these brands default to In-house Lab. Staff can switch them only to External Lab.",
    ) + _custom_current)
    if _custom_current:
        st.caption("Existing saved brand(s) not found in active product list: " + ", ".join(_custom_current))
    st.caption("Stock allocation still takes priority. RX orders from all other brands go to Supplier assignment.")
    st.markdown("---")

    # ── Order Edit / Cancellation Governance ────────────────────────────────
    st.markdown("**Order Edit / Cancellation Governance**")
    _cur_edit_statuses = [
        s for s in _csv_to_list(_get("order_edit_allowed_statuses", ",".join(DEFAULT_EDIT_STATUSES)))
        if s in ORDER_PIPELINE_STATUSES
    ]
    _cur_cancel_statuses = [
        s for s in _csv_to_list(_get("order_cancel_allowed_statuses", ",".join(DEFAULT_CANCEL_STATUSES)))
        if s in ORDER_PIPELINE_STATUSES
    ]
    gc1, gc2 = st.columns(2)
    with gc1:
        edited["order_edit_allowed_statuses"] = ",".join(st.multiselect(
            "Allow punching/edit at statuses",
            ORDER_PIPELINE_STATUSES,
            default=_cur_edit_statuses or DEFAULT_EDIT_STATUSES,
            key=f"sm_order_edit_statuses_{_refresh}",
        ))
    with gc2:
        edited["order_cancel_allowed_statuses"] = ",".join(st.multiselect(
            "Allow order cancel at statuses",
            ORDER_PIPELINE_STATUSES,
            default=_cur_cancel_statuses or DEFAULT_CANCEL_STATUSES,
            key=f"sm_order_cancel_statuses_{_refresh}",
        ))
    st.caption("Default: edit/cancel allowed only before Backoffice confirmation.")
    st.markdown("---")

    # ── Consultation Types ─────────────────────────────────────────────────
    try:
        from modules.settings.consultation_settings import render_consultation_settings
        render_consultation_settings()
    except Exception as _cse:
        st.warning(f"Consultation settings panel unavailable: {_cse}")
    st.markdown("---")

    # Preview
    with st.expander("👁️ Preview print header", expanded=True):
        _preview_header(edited)

    # Save
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("💾 Save", type="primary", use_container_width=True):
            saved = 0
            for k, v in edited.items():
                if _set(k, str(v or "").strip()):
                    saved += 1
            _invalidate_shop_cache()
            st.session_state.shop_master_refresh = _refresh + 1
            st.success(f"✅ Saved {saved} settings — will reflect in all prints")
            st.rerun()


def _preview_header(d):
    import streamlit.components.v1 as components
    name    = d.get("shop_name", "DV Optical") or "DV Optical"
    tagline = d.get("shop_tagline", "") or ""
    parts   = [d.get("shop_address",""), d.get("shop_address2",""),
               d.get("shop_city",""), d.get("shop_state",""), d.get("shop_pincode","")]
    addr    = ", ".join(p for p in parts if p and p.strip())
    phone   = d.get("shop_phone","") or ""
    email   = d.get("shop_email","") or ""
    gstin   = d.get("shop_gstin","") or ""
    upi     = d.get("shop_upi_id","") or ""
    footer  = d.get("print_footer","") or ""

    html = f"""
    <div style='font-family:Arial,sans-serif;border:1px solid #e2e8f0;
                border-radius:8px;padding:12px 16px;background:#fff;margin-bottom:8px'>
      <div style='display:flex;justify-content:space-between;align-items:flex-start'>
        <div>
          <div style='font-size:20px;font-weight:900;color:#1e293b;letter-spacing:.02em'>
            {name.upper()}</div>
          {f"<div style='font-size:11px;color:#64748b;margin-top:2px;font-style:italic'>{tagline}</div>" if tagline else ""}
          <div style='font-size:11px;color:#374151;margin-top:4px'>{addr}</div>
        </div>
        <div style='text-align:right;font-size:11px;color:#475569;line-height:1.6'>
          {f"Ph: {phone}<br>" if phone else ""}
          {f"{email}<br>" if email else ""}
          {f"GSTIN: {gstin}<br>" if gstin else ""}
          {f"UPI: {upi}" if upi else ""}
        </div>
      </div>
      {f"<div style='margin-top:8px;border-top:0.5px solid #e2e8f0;padding-top:6px;font-size:10px;color:#94a3b8;text-align:center'>{footer}</div>" if footer else ""}
    </div>
    """
    components.html(html, height=150)
