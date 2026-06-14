"""
modules/billing/credit_debit_note_ui.py
========================================
Streamlit UI for Credit Note (CN) and Debit Note (DN) module.

Entry point:  render_cdn_module()
Tabs:
  1. ➕ New Credit Note    — issue CN against an invoice
  2. ➕ New Debit Note     — issue DN against an invoice
  3. 📋 Register          — view/search/cancel all CNs and DNs
  4. 📤 Tally Export      — download CSV for Tally import
  5. 📊 GSTR-1 Summary    — Table 9B data preview

Role Guard:
  Requires BILLING, MANAGER, or ADMIN.
  Cancel action requires MANAGER or ADMIN.
"""

import streamlit as st
from datetime import date, timedelta
from typing import Optional, List, Dict

from modules.billing.credit_debit_note_manager import (
    # Lookups
    get_invoice_for_cdn, get_invoice_lines_for_cdn, search_invoices,
    search_parties_for_cdn, list_party_invoices_for_cdn, list_party_open_orders_for_cdn,
    # Creators
    create_credit_note, create_debit_note,
    # Listings
    list_credit_notes, list_debit_notes,
    get_cn_detail, get_dn_detail,
    cancel_cdn,
    # Tally / GSTR
    export_cdn_for_tally, generate_gstr1_cdn_data, get_cdn_summary_stats,
    # Config
    CN_REASONS, DN_REASONS,
    OUR_STATE_NAME,
)
from modules.security.roles import (
    ADMIN, MANAGER, BILLING, has_role, current_user_name
)


def _cdn_q(sql: str, params=None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as exc:
        st.error(f"CN/DN lookup failed: {exc}")
        return []


def _cdn_scope_clause(scope: str, order_alias: str = "o", invoice_alias: str = "i") -> str:
    ot = f"UPPER(COALESCE({order_alias}.order_type,''))"
    if scope == "Retail":
        return f"AND {ot} = 'RETAIL'"
    if scope == "Online":
        return f"AND {ot} = 'ONLINE'"
    if scope == "Wholesale":
        return (
            f"AND (COALESCE({invoice_alias}.party_id::text,'') <> '' "
            f"OR {ot} IN ('WHOLESALE','BULK','BULK_ORDER')) "
            f"AND {ot} NOT IN ('RETAIL','ONLINE')"
        )
    return ""


def render_cdn_module(inline: bool = False) -> None:
    """
    Main entry point.
    inline=True: compact mode for inside invoice detail (no title/summary bar).
    inline=False: full sidebar page.
    """
    if not inline:
        st.title("📝 Credit & Debit Notes")
        st.caption(
            "GST-compliant · Section 34 CGST Act · "
            "GSTR-1 Table 9B · Tally Prime compatible"
        )
        st.markdown(
            "<div style='background:#08111f;border:1px solid #1e3a5f;"
            "border-left:4px solid #38bdf8;border-radius:8px;padding:10px 14px;"
            "margin:8px 0 12px 0'>"
            "<b style='color:#e2e8f0'>Workflow</b>"
            "<span style='color:#94a3b8;font-size:0.82rem;margin-left:8px'>"
            "Search/scan invoice or party → select invoice lines → adjust qty/rate/reason → "
            "preview GST → issue CN/DN → export to Tally/GSTR-1."
            "</span></div>",
            unsafe_allow_html=True,
        )
        _render_summary_bar()
        st.markdown("---")

    tabs = st.tabs([
        "➕ New Credit Note",
        "➕ New Debit Note",
        "📋 Register",
        "📤 Tally Export",
        "📊 GSTR-1 Preview",
    ])

    with tabs[0]:
        _render_new_cn(inline=inline)
    with tabs[1]:
        _render_new_dn()
    with tabs[2]:
        _render_register()
    with tabs[3]:
        _render_tally_export()
    with tabs[4]:
        _render_gstr1_preview()


# ── Summary bar ───────────────────────────────────────────────────────

def _render_summary_bar() -> None:
    this_month_start = date.today().replace(day=1)
    stats = get_cdn_summary_stats(this_month_start, date.today())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Credit Notes (MTD)", stats["cn_count"],
              delta=f"₹{stats['cn_total']:,.0f}", delta_color="inverse")
    c2.metric("Debit Notes (MTD)",  stats["dn_count"],
              delta=f"₹{stats['dn_total']:,.0f}")
    c3.metric("CN Value (MTD)",     f"₹{stats['cn_total']:,.2f}")
    c4.metric("DN Value (MTD)",     f"₹{stats['dn_total']:,.2f}")
    c5.metric("Pending Tally Export", stats["pending_tally_export"],
              delta="🔴 Unsynced" if stats["pending_tally_export"] > 0 else "✅ All synced",
              delta_color="inverse" if stats["pending_tally_export"] > 0 else "normal")


# ── Shared invoice lookup widget ──────────────────────────────────────

def _extract_invoice_candidates_from_upload(uploaded) -> List[str]:
    """Best-effort invoice number extraction from uploaded PDF/text/image name."""
    if uploaded is None:
        return []
    import re

    text = uploaded.name or ""
    data = b""
    try:
        data = uploaded.getvalue()
    except Exception:
        data = b""

    # Text files / CSV / HTML exports
    if uploaded.type and ("text" in uploaded.type or uploaded.name.lower().endswith((".txt", ".csv", ".html"))):
        try:
            text += "\n" + data.decode("utf-8", errors="ignore")
        except Exception:
            pass

    # PDF text extraction if pypdf/PyPDF2 is available. Scanned PDFs/images
    # still need real OCR later, but filename/manual search remains usable.
    if uploaded.name.lower().endswith(".pdf"):
        try:
            import io
            try:
                from pypdf import PdfReader
            except Exception:
                from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pdf_text = "\n".join((p.extract_text() or "") for p in reader.pages[:3])
            text += "\n" + pdf_text
        except Exception:
            pass

    patterns = [
        r"\bINV[/-]?\d{2,4}[/-]?\d{1,8}\b",
        r"\bINV[/-][A-Z0-9/-]{3,30}\b",
        r"\bR/\d{4}/\d{3,8}\b",
        r"\b[A-Z]{1,4}/\d{2,4}/\d{3,8}\b",
    ]
    found = []
    for pat in patterns:
        for hit in re.findall(pat, text.upper()):
            h = hit.strip(" .,:;")
            if h and h not in found:
                found.append(h)
    return found[:8]


def _invoice_upload_scan_panel(key_prefix: str) -> None:
    with st.expander("📎 Upload / Scan Previous Invoice", expanded=False):
        up = st.file_uploader(
            "Upload invoice PDF / text / image",
            type=["pdf", "txt", "csv", "html", "png", "jpg", "jpeg", "webp"],
            key=f"{key_prefix}_invoice_upload",
            help="Text PDFs can be read directly. Scanned images are kept as a helper now; full OCR can be added later.",
        )
        if not up:
            st.caption("Upload an old invoice if staff does not know the invoice number.")
            return
        candidates = _extract_invoice_candidates_from_upload(up)
        st.session_state[f"{key_prefix}_uploaded_invoice_file"] = up.name
        if candidates:
            chosen = st.selectbox(
                "Detected invoice/order reference",
                candidates,
                key=f"{key_prefix}_ocr_candidate",
            )
            if st.button("Use this reference", key=f"{key_prefix}_ocr_use", use_container_width=True):
                st.session_state[f"{key_prefix}_inv_search"] = chosen
                st.session_state[f"{key_prefix}_prefilled_inv"] = chosen
                st.rerun()
        else:
            st.warning("No invoice number detected. Use manual search below.")


def _party_invoice_picker(key_prefix: str) -> Optional[Dict]:
    with st.expander("🏪 Party / Customer → Invoice → Product / Power", expanded=True):
        c1, c2 = st.columns([2, 2])
        scope = c1.radio(
            "Sale source",
            ["All", "Wholesale", "Retail", "Online"],
            horizontal=True,
            key=f"{key_prefix}_acct_scope",
        )
        active_mode = c2.radio(
            "Account list",
            ["With invoices", "All accounts"],
            horizontal=True,
            key=f"{key_prefix}_acct_active",
            help="With invoices keeps only accounts where a CN/DN can actually be raised.",
        )
        q = st.text_input(
            "Search party / customer / mobile / GSTIN",
            key=f"{key_prefix}_party_search",
            placeholder="Type name, mobile or GSTIN",
        ).strip()

        if len(q) < 2:
            st.caption("Type at least 2 characters to search party/customer.")
            return None

        scope_clause = _cdn_scope_clause(scope, "o", "i")
        if active_mode == "With invoices":
            parties = _cdn_q(f"""
                SELECT
                    COALESCE(i.party_id::text, 'NAME:' || COALESCE(pt.party_name, o.party_name, o.patient_name, '')) AS key,
                    COALESCE(pt.party_name, o.party_name, o.patient_name, 'Customer') AS party_name,
                    COALESCE(pt.mobile, o.patient_mobile, '') AS mobile,
                    COALESCE(pt.gstin, '') AS gstin,
                    COUNT(DISTINCT i.id) AS invoice_count,
                    MAX(i.invoice_date)::text AS last_invoice_date
                FROM invoices i
                LEFT JOIN parties pt ON pt.id = i.party_id
                LEFT JOIN LATERAL (
                    SELECT o2.party_name, o2.patient_name, o2.patient_mobile, o2.order_type
                    FROM orders o2
                    WHERE o2.id::text = ANY(i.order_ids)
                    LIMIT 1
                ) o ON TRUE
                WHERE COALESCE(i.is_deleted, FALSE) = FALSE
                  AND i.status NOT IN ('CANCELLED','VOID')
                  AND (
                        UPPER(COALESCE(pt.party_name, o.party_name, o.patient_name, '')) LIKE %(q)s
                     OR UPPER(COALESCE(pt.mobile, o.patient_mobile, '')) LIKE %(q)s
                     OR UPPER(COALESCE(pt.gstin, '')) LIKE %(q)s
                  )
                  {scope_clause}
                GROUP BY i.party_id, pt.party_name, o.party_name, o.patient_name,
                         pt.mobile, o.patient_mobile, pt.gstin
                ORDER BY MAX(i.invoice_date) DESC, party_name
                LIMIT 40
            """, {"q": f"%{q.upper()}%"})
        else:
            order_scope = ""
            if scope == "Retail":
                order_scope = "AND UPPER(COALESCE(order_type,'')) = 'RETAIL'"
            elif scope == "Online":
                order_scope = "AND UPPER(COALESCE(order_type,'')) = 'ONLINE'"
            elif scope == "Wholesale":
                order_scope = "AND UPPER(COALESCE(order_type,'')) NOT IN ('RETAIL','ONLINE')"
            parties = _cdn_q("""
                SELECT key, party_name, mobile, gstin, 0 AS invoice_count, '' AS last_invoice_date
                FROM (
                    SELECT id::text AS key, party_name, COALESCE(mobile,'') AS mobile, COALESCE(gstin,'') AS gstin
                    FROM parties
                    WHERE COALESCE(is_active, TRUE) = TRUE
                    UNION
                    SELECT 'NAME:' || COALESCE(patient_name, party_name, '') AS key,
                           COALESCE(patient_name, party_name, '') AS party_name,
                           COALESCE(patient_mobile, '') AS mobile,
                           '' AS gstin
                    FROM orders
                    WHERE COALESCE(patient_name, party_name, '') <> ''
                      """ + order_scope + """
                ) x
                WHERE UPPER(COALESCE(party_name,'')) LIKE %(q)s
                   OR UPPER(COALESCE(mobile,'')) LIKE %(q)s
                   OR UPPER(COALESCE(gstin,'')) LIKE %(q)s
                ORDER BY party_name
                LIMIT 40
            """, {"q": f"%{q.upper()}%"})

        if not parties:
            st.warning("No matching party/customer found.")
            return None

        labels = {
            p["key"]: (
                f"{p['party_name']} · {p.get('mobile','') or '—'}"
                + (f" · {int(p.get('invoice_count') or 0)} invoice(s)" if active_mode == "With invoices" else "")
            )
            for p in parties
        }
        pkey = st.selectbox(
            "Party / Customer",
            list(labels.keys()),
            format_func=lambda x: labels.get(x, x),
            key=f"{key_prefix}_party_select",
        )
        selected_party = next((p for p in parties if p["key"] == pkey), {})

        f1, f2, f3, f4, f5 = st.columns([2, 1, 1, 1, 1])
        prod_filter = f1.text_input("Product filter", key=f"{key_prefix}_inv_product_filter", placeholder="Product / brand")
        sph_filter = f2.text_input("SPH", key=f"{key_prefix}_inv_sph_filter", placeholder="+/-")
        cyl_filter = f3.text_input("CYL", key=f"{key_prefix}_inv_cyl_filter", placeholder="+/-")
        ax_filter = f4.text_input("AX", key=f"{key_prefix}_inv_axis_filter", placeholder="Axis")
        add_filter = f5.text_input("ADD", key=f"{key_prefix}_inv_add_filter", placeholder="+")

        party_where = ""
        params = {
            "name": selected_party.get("party_name", ""),
            "prod": f"%{prod_filter.strip().upper()}%",
        }
        if str(pkey).startswith("NAME:"):
            party_where = "AND COALESCE(pt.party_name, o.party_name, o.patient_name, '') = %(name)s"
        else:
            party_where = "AND i.party_id = %(pid)s::uuid"
            params["pid"] = pkey

        line_filters = ""
        if prod_filter.strip():
            line_filters += """
                AND EXISTS (
                    SELECT 1 FROM invoice_lines ilx
                    LEFT JOIN order_lines olx ON olx.id = ilx.order_line_id
                    LEFT JOIN products px ON px.id = olx.product_id
                    WHERE ilx.invoice_id = i.id
                      AND (
                            UPPER(COALESCE(ilx.product_name,'')) LIKE %(prod)s
                         OR UPPER(COALESCE(px.product_name,'')) LIKE %(prod)s
                         OR UPPER(COALESCE(px.brand,'')) LIKE %(prod)s
                      )
                )
            """
        for key, col, val in [
            ("sph", "sph", sph_filter), ("cyl", "cyl", cyl_filter),
            ("axis", "axis", ax_filter), ("add", "add_power", add_filter),
        ]:
            if str(val).strip():
                try:
                    params[key] = float(val)
                    line_filters += f"""
                        AND EXISTS (
                            SELECT 1 FROM invoice_lines ilp
                            JOIN order_lines olp ON olp.id = ilp.order_line_id
                            WHERE ilp.invoice_id = i.id
                              AND ROUND(COALESCE(olp.{col}, 0)::numeric, 2)
                                = ROUND(%({key})s::numeric, 2)
                        )
                    """
                except Exception:
                    st.caption(f"Ignoring invalid {key.upper()} filter.")

        invs = _cdn_q(f"""
            SELECT i.id::text AS id,
                   i.invoice_no,
                   i.invoice_date,
                   i.grand_total,
                   i.payment_status,
                   COALESCE(STRING_AGG(DISTINCT COALESCE(il.product_name,''), ', '), '') AS products
            FROM invoices i
            LEFT JOIN parties pt ON pt.id = i.party_id
            LEFT JOIN LATERAL (
                SELECT o2.party_name, o2.patient_name, o2.order_type
                FROM orders o2
                WHERE o2.id::text = ANY(i.order_ids)
                LIMIT 1
            ) o ON TRUE
            LEFT JOIN invoice_lines il ON il.invoice_id = i.id AND COALESCE(il.is_deleted,FALSE)=FALSE
            WHERE COALESCE(i.is_deleted, FALSE) = FALSE
              AND i.status NOT IN ('CANCELLED','VOID')
              {party_where}
              {scope_clause}
              {line_filters}
            GROUP BY i.id, i.invoice_no, i.invoice_date, i.grand_total, i.payment_status
            ORDER BY i.invoice_date DESC, i.created_at DESC
            LIMIT 50
        """, params)

        if not invs:
            st.warning("No invoices found for this party/customer with the selected filters.")
            return None

        inv_labels = {
            i["invoice_no"]: (
                f"{i['invoice_no']} · {str(i.get('invoice_date'))[:10]} · "
                f"₹{float(i.get('grand_total') or 0):,.2f} · {i.get('products') or 'No product lines'}"
            )
            for i in invs
        }
        ino = st.selectbox(
            "Invoice with products",
            list(inv_labels.keys()),
            format_func=lambda x: inv_labels.get(x, x),
            key=f"{key_prefix}_party_invoice_select",
        )
        if st.button("Use selected invoice", key=f"{key_prefix}_party_invoice_use", use_container_width=True):
            st.session_state[f"{key_prefix}_prefilled_inv"] = ino
            st.rerun()
        return None

def _invoice_lookup_widget(key_prefix: str) -> Optional[Dict]:
    """
    Search for an invoice by number or party name.
    Returns the selected invoice dict or None.
    Auto-fills if launched from invoice detail page via cdn_prefill_invoice_no.
    """
    # Auto-fill if redirected from invoice detail
    # Use a stable key so the value persists across the render cycle
    _prefill = st.session_state.get("cdn_prefill_invoice_no")
    if _prefill:
        # Don't pop yet — let it persist so text_input can read it
        st.session_state[f"{key_prefix}_prefilled_inv"] = _prefill
        st.session_state["cdn_prefill_invoice_no"] = None  # clear for next render

    # Use prefilled value if available
    _stable_prefill = st.session_state.get(f"{key_prefix}_prefilled_inv", "")

    st.markdown("#### 🔍 Find Invoice")
    _invoice_upload_scan_panel(key_prefix)
    _party_invoice_picker(key_prefix)

    # If we have a prefill, skip the search widget and go direct to lookup
    if _stable_prefill:
        st.info(f"Looking up invoice: **{_stable_prefill}**")
        inv = get_invoice_for_cdn(_stable_prefill)
        if inv:
            # Check DB for existing CNs against this invoice
            try:
                from modules.billing.credit_debit_note_manager import get_credit_notes_for_invoice
                _inv_id = str(inv.get("id") or "")
                _existing_cns = get_credit_notes_for_invoice(_inv_id) if _inv_id else []
            except Exception:
                _existing_cns = []

            # Also check session state for just-issued CN
            _issued_cn = st.session_state.get(f"cn_issued_{_stable_prefill}")
            # Show existing CNs warning (only CONFIRMED ones from DB)
            _all_cn_nos = []
            if _existing_cns:
                _all_cn_nos = [c.get("cn_number") for c in _existing_cns if c.get("cn_number")]
            # If session state has a CN number but DB no longer has it as CONFIRMED
            # (e.g. it was cancelled), clear the stale session key
            if _issued_cn:
                if _issued_cn in _all_cn_nos:
                    pass  # still active
                else:
                    # CN was cancelled or doesn't exist — clear stale key
                    st.session_state.pop(f"cn_issued_{_stable_prefill}", None)
                    _issued_cn = None

            _show_invoice_card(inv)

            # If all CNs were cancelled, _all_cn_nos is empty — show form directly
            if not _all_cn_nos and not _issued_cn:
                if st.button("🔄 Search different invoice",
                             key=f"{key_prefix}_clear_prefill"):
                    st.session_state.pop(f"{key_prefix}_prefilled_inv", None)
                    st.rerun()
                return inv  # No active CNs — show form

            if _all_cn_nos:
                st.warning(
                    f"⚠️ CN(s) already issued for **{_stable_prefill}**: "
                    + ", ".join(f"**{c}**" for c in _all_cn_nos)
                )
                _btn1, _btn2 = st.columns(2)
                with _btn1:
                    if st.button("📄 Issue another CN (partial credit)",
                                 key=f"{key_prefix}_issue_another"):
                        st.session_state[f"{key_prefix}_force_new"] = True
                        st.session_state.pop(f"cn_issued_{_stable_prefill}", None)
                        st.rerun()
                with _btn2:
                    if st.button("🔄 Different invoice",
                                 key=f"{key_prefix}_clear_prefill"):
                        st.session_state.pop(f"{key_prefix}_prefilled_inv", None)
                        st.session_state.pop(f"cn_issued_{_stable_prefill}", None)
                        st.session_state.pop(f"{key_prefix}_force_new", None)
                        st.rerun()
                if not st.session_state.get(f"{key_prefix}_force_new"):
                    return None  # Don't show form unless user clicked "Issue another"
            else:
                if st.button("🔄 Search different invoice",
                             key=f"{key_prefix}_clear_prefill"):
                    st.session_state.pop(f"{key_prefix}_prefilled_inv", None)
                    st.rerun()
            return inv
        else:
            st.warning(f"Invoice **{_stable_prefill}** not found.")
            st.session_state.pop(f"{key_prefix}_prefilled_inv", None)

    col1, col2 = st.columns([3, 1])

    with col1:
        search_term = st.text_input(
            "Invoice number or party name",
            placeholder="e.g. INV/2026/0011 or Raj Optical",
            key=f"{key_prefix}_inv_search",
        )
    with col2:
        manual_mode = st.checkbox("Enter manually", key=f"{key_prefix}_manual")

    if manual_mode:
        inv_no = st.text_input("Invoice Number", key=f"{key_prefix}_manual_no",
                               placeholder="INV/2026/0042").strip().upper()
        if inv_no:
            inv = get_invoice_for_cdn(inv_no)
            if inv:
                _show_invoice_card(inv)
                return inv
            else:
                st.warning(f"Invoice **{inv_no}** not found.")
        return None

    if not search_term or len(search_term) < 2:
        st.caption("Type at least 2 characters to search.")
        return None

    results = search_invoices(search_term)
    if not results:
        st.info("No invoices found matching your search.")
        return None

    options = {
        r["id"]: f"{r['invoice_no']}  ·  {r['party_name']}  ·  ₹{float(r['grand_total'] or 0):,.2f}  ·  {str(r['invoice_date'])[:10]}"
        for r in results
    }
    selected_id = st.selectbox(
        "Select Invoice",
        options=list(options.keys()),
        format_func=lambda x: options.get(x, x),
        key=f"{key_prefix}_inv_select",
    )
    inv = next((r for r in results if r["id"] == selected_id), None)
    if inv:
        _show_invoice_card(inv)
        # Fetch full details
        full = get_invoice_for_cdn(inv["invoice_no"])
        return full
    return None


def _show_invoice_card(inv: Dict) -> None:
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Invoice No",  inv.get("invoice_no", "—"))
        c2.metric("Party",       inv.get("party_name", "—"))
        c3.metric("Grand Total", f"₹{float(inv.get('grand_total') or 0):,.2f}",
                  help="Invoice grand total — includes lens lines + service charges if any")
        c4.metric("Date",        str(inv.get("invoice_date", ""))[:10])
        if inv.get("party_gstin"):
            st.caption(f"GSTIN: `{inv['party_gstin']}`")

        # Show service charges from challan_service_charges snapshot
        # so CN/DN creator can see the full original order breakdown
        order_ids = inv.get("order_ids") or []
        if order_ids:
            try:
                from modules.sql_adapter import run_query
                from modules.billing.challan_invoice_manager import _q as cim_q
                svc_rows = cim_q("""
                    SELECT charge_type, description,
                           base_amount, gst_amount, total_amount
                    FROM challan_service_charges
                    WHERE challan_id = (
                        SELECT challan_id FROM invoices
                        WHERE invoice_no = %(ino)s LIMIT 1
                    )
                    ORDER BY charge_type
                """, {"ino": inv.get("invoice_no", "")})
                if svc_rows:
                    svc_total = sum(float(r.get("total_amount") or 0) for r in svc_rows)
                    st.caption(
                        f"⚙️ Service charges on original invoice: "
                        + "  |  ".join(
                            f"{r['charge_type']} ₹{float(r['total_amount'] or 0):,.2f}"
                            for r in svc_rows
                        )
                        + f"  →  Total ₹{svc_total:,.2f}"
                    )
                    st.info(
                        "ℹ️ When raising a Credit/Debit Note for service charges, "
                        "add the service charge lines manually in the line items below.",
                        icon="ℹ️"
                    )
            except Exception:
                pass


# ── Line item builder ─────────────────────────────────────────────────

def _line_builder(invoice_id: Optional[str], key_prefix: str) -> List[Dict]:
    """
    Build line items for a CDN.
    If invoice_id is given: show invoice lines as pre-filled checkboxes.
    Also supports manual item entry.
    """
    st.markdown("#### 📦 Line Items")

    lines: List[Dict] = []

    _line_filter = ""
    if invoice_id:
        inv_lines = get_invoice_lines_for_cdn(str(invoice_id))
        # Get already-credited line IDs to disable those checkboxes
        try:
            from modules.billing.credit_debit_note_manager import get_credited_line_ids
            _credited = get_credited_line_ids(str(invoice_id))
        except Exception:
            _credited = {}
        if inv_lines:
            st.caption("Select which invoice lines to include in this note:")
            _line_filter = st.text_input(
                "Search within invoice lines",
                key=f"{key_prefix}_line_filter",
                placeholder="Product / power / eye / brand",
                label_visibility="collapsed",
            )
            if _line_filter.strip():
                needle = _line_filter.strip().lower()
                def _line_blob(row: Dict) -> str:
                    lp = row.get("lens_params") or {}
                    return " ".join(str(row.get(k) or "") for k in (
                        "product_name", "brand", "category", "eye_side",
                        "sph", "cyl", "axis", "add_power"
                    )) + " " + str(lp)
                inv_lines = [r for r in inv_lines if needle in _line_blob(r).lower()]
                if not inv_lines:
                    st.warning("No invoice lines match this search.")

            for i, il in enumerate(inv_lines):
                key = f"{key_prefix}_line_{i}"
                with st.container(border=True):
                    col1, col2, col3, col4, col5 = st.columns([3.4, 1, 1, 1, 1])
                    with col1:
                        _line_id = str(il.get("id") or "")
                        _already_cn = _credited.get(_line_id)
                        _pwr_bits = []
                        for _pk, _lbl in [("sph","SPH"),("cyl","CYL"),("axis","AX"),("add_power","ADD")]:
                            _pv = il.get(_pk)
                            if _pv not in (None, "", "None", 0, 0.0, "0", "0.0"):
                                try:
                                    _pf = float(_pv)
                                    if _pk == "axis":
                                        _pwr_bits.append(f"AX {int(_pf)}")
                                    elif _pk == "add_power":
                                        _pwr_bits.append(f"ADD {_pf:+.2f}")
                                    else:
                                        _pwr_bits.append(f"{_lbl} {_pf:+.2f}")
                                except Exception:
                                    pass
                        _pwr_str = "  ".join(_pwr_bits)
                        _line_meta = " · ".join(
                            x for x in [
                                str(il.get("eye_side") or "").upper(),
                                str(il.get("brand") or ""),
                                str(il.get("category") or ""),
                                _pwr_str,
                            ] if x
                        )
                        if _already_cn:
                            st.markdown(
                                f"<div style='color:#94a3b8;font-size:0.82rem'>"
                                f"✅ {il.get('product_name','Item')}</div>"
                                + (f"<div style='color:#38bdf8;font-size:0.7rem'>{_line_meta}</div>" if _line_meta else "")
                                + f"<div style='color:#10b981;font-size:0.7rem'>"
                                f"Credited: {_already_cn}</div>",
                                unsafe_allow_html=True
                            )
                            include = False
                        else:
                            include = st.checkbox(
                                f"{il.get('product_name','Item')}",
                                value=True, key=f"{key}_include"
                            )
                            if _line_meta:
                                st.caption(_line_meta)
                    with col2:
                        qty = st.number_input(
                            "Qty", min_value=0.0,
                            value=float(il.get("quantity") or 0),
                            step=0.5, key=f"{key}_qty"
                        )
                    with col3:
                        unit_price = st.number_input(
                            "Rate ₹", min_value=0.0,
                            value=float(il.get("unit_price") or 0),
                            step=0.5, key=f"{key}_rate"
                        )
                    with col4:
                        # Use pre-calculated taxable_amount (base excl GST) from query
                        # This handles both GST-inclusive and GST-exclusive pricing
                        _pre_taxable = float(il.get("taxable_amount") or 0)
                        if _pre_taxable > 0:
                            # Scale by qty ratio in case user changed qty
                            _orig_qty = float(il.get("quantity") or 1)
                            taxable = round(_pre_taxable * qty / _orig_qty, 2) if _orig_qty else round(qty * unit_price, 2)
                        else:
                            taxable = round(qty * unit_price, 2)
                        st.metric("Taxable", f"₹{taxable:,.2f}")
                    with col5:
                        # GST% is fixed — read from invoice line, not user-editable
                        _gst_val = float(il.get("gst_percent") or il.get("tax_rate") or 0)
                        if _gst_val == 0 and float(il.get("tax_amount") or 0) > 0:
                            _tp = float(il.get("taxable_amount") or il.get("total_price") or 1)
                            _gst_val = round(float(il["tax_amount"]) / _tp * 100, 2) if _tp else 0
                        if _gst_val == 0 and float(il.get("line_total") or 0) > float(il.get("total_price") or 0) + 0.01:
                            # Back-calc from line_total vs total_price
                            _tp2 = float(il.get("total_price") or 1)
                            _lt2 = float(il.get("line_total") or 0)
                            _gst_val = round((_lt2 - _tp2) / _tp2 * 100, 2) if _tp2 else 0
                        gst_pct = _gst_val
                        # Show as read-only metric
                        st.metric("GST %", f"{_gst_val:.1f}%")

                    _move_inv = False
                    if key_prefix == "dn":
                        _move_inv = st.checkbox(
                            "Physical goods (reduce stock)",
                            value=False,
                            key=f"{key}_move_inv",
                            help=(
                                "Tick only when this DN line covers extra goods "
                                "actually shipped. Leave off for value-only "
                                "adjustments such as rate, freight, or interest."
                            ),
                        )

                    if include and qty > 0:
                        lines.append({
                            "product_name":    il.get("product_name", ""),
                            "hsn_sac_code":    il.get("hsn_sac_code", ""),
                            "invoice_line_id": il.get("id"),
                            "order_line_id":   il.get("order_line_id"),
                            "product_id":      il.get("product_id"),
                            "quantity":        qty,
                            "unit_price":      unit_price,
                            "taxable_amount":  taxable,
                            "gst_percent":     gst_pct,
                            "tax_amount":      round(taxable * gst_pct / 100, 2),
                            "move_inventory":  _move_inv,
                        })

    # Manual line entry
    with st.expander("➕ Add Manual Line Item / Scan Product", expanded=False):
        _prod_search = st.text_input(
            "Search / scan product",
            key=f"{key_prefix}_m_product_search",
            placeholder="Barcode, SKU or product name",
        )
        _prod_choice = None
        if len(_prod_search.strip()) >= 2:
            try:
                from modules.ui_product_selector import lookup_sku
                _sku_hit = lookup_sku(_prod_search.strip())
                if _sku_hit:
                    _prod_choice = {
                        "id": _sku_hit.get("product_id"),
                        "product_name": _sku_hit.get("product_name"),
                        "brand": _sku_hit.get("brand", ""),
                        "category": _sku_hit.get("category", ""),
                        "gst_percent": _sku_hit.get("gst_percent", 0),
                        "hsn_code": _sku_hit.get("hsn_code", ""),
                        "rate": _sku_hit.get("selling_price") or _sku_hit.get("mrp") or 0,
                    }
                    st.caption(
                        "Matched via product selector: "
                        f"{_prod_choice.get('product_name')} · "
                        f"{_sku_hit.get('batch_no') or _sku_hit.get('item_code') or ''}"
                    )
            except Exception:
                _prod_choice = None
        if len(_prod_search.strip()) >= 2 and not _prod_choice:
            try:
                from modules.sql_adapter import run_query
                _prod_rows = run_query("""
                    SELECT id::text AS id,
                           product_name,
                           COALESCE(brand,'') AS brand,
                           COALESCE(category, main_group, '') AS category,
                           COALESCE(gst_percent, 0) AS gst_percent,
                           COALESCE(hsn_code, '') AS hsn_code,
                           COALESCE(mrp, selling_price, 0) AS rate
                    FROM products
                    WHERE COALESCE(is_active, TRUE) = TRUE
                      AND (
                            LOWER(product_name) LIKE %(q)s
                         OR LOWER(COALESCE(barcode,'')) LIKE %(q)s
                         OR LOWER(COALESCE(sku_code,'')) LIKE %(q)s
                      )
                    ORDER BY product_name
                    LIMIT 25
                """, {"q": f"%{_prod_search.strip().lower()}%"}) or []
            except Exception:
                _prod_rows = []
            if _prod_rows:
                _prod_map = {p["id"]: p for p in _prod_rows}
                _prod_id = st.selectbox(
                    "Matched product",
                    list(_prod_map.keys()),
                    format_func=lambda x: (
                        f"{_prod_map[x]['product_name']} · {_prod_map[x].get('brand','')} · "
                        f"₹{float(_prod_map[x].get('rate') or 0):,.2f}"
                    ),
                    key=f"{key_prefix}_m_product_match",
                )
                _prod_choice = _prod_map.get(_prod_id)
            else:
                st.caption("No product match; enter manual line below.")
        mc1, mc2, mc3, mc4 = st.columns([3, 1, 1, 1])
        with mc1:
            m_name = st.text_input(
                "Product / Service",
                value=str((_prod_choice or {}).get("product_name") or ""),
                key=f"{key_prefix}_m_name",
            )
        with mc2:
            m_qty  = st.number_input("Qty", min_value=0.0, step=0.5, key=f"{key_prefix}_m_qty")
        with mc3:
            m_rate = st.number_input(
                "Rate ₹",
                min_value=0.0,
                value=float((_prod_choice or {}).get("rate") or 0),
                step=0.5,
                key=f"{key_prefix}_m_rate",
            )
        with mc4:
            m_gst  = st.number_input("GST %", min_value=0.0, max_value=28.0,
                                     value=float((_prod_choice or {}).get("gst_percent") or 5.0),
                                     step=0.5, key=f"{key_prefix}_m_gst")
        m_hsn = st.text_input(
            "HSN/SAC Code (optional)",
            value=str((_prod_choice or {}).get("hsn_code") or ""),
            key=f"{key_prefix}_m_hsn",
        )
        _m_move_inv = False
        if key_prefix == "dn":
            _m_move_inv = st.checkbox(
                "Physical goods (reduce stock)",
                value=False,
                key=f"{key_prefix}_m_move_inv",
                help="Tick only when this DN line covers extra goods actually shipped.",
            )

        if st.button("Add Line", key=f"{key_prefix}_m_add") and m_name and m_qty > 0:
            lines.append({
                "product_name":   m_name,
                "hsn_sac_code":   m_hsn,
                "invoice_line_id": None,
                "product_id":      (_prod_choice or {}).get("id"),
                "quantity":       m_qty,
                "unit_price":     m_rate,
                "taxable_amount": round(m_qty * m_rate, 2),
                "gst_percent":    m_gst,
                "tax_amount":      round(m_qty * m_rate * m_gst / 100, 2),
                "move_inventory":  _m_move_inv,
            })
            st.success(f"Added: {m_name}")

    return lines


def _tax_preview(lines: List[Dict], place_of_supply: str) -> None:
    """Show GST computation preview before saving."""
    if not lines:
        return

    from modules.billing.credit_debit_note_manager import split_gst, _aggregate_lines
    agg = _aggregate_lines(lines, place_of_supply)
    is_inter = agg["igst_amount"] > 0

    st.markdown("#### 💰 Tax Summary")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Taxable Amount", f"₹{agg['taxable_amount']:,.2f}")
    if is_inter:
        c2.metric("IGST", f"₹{agg['igst_amount']:,.2f}")
        c3.metric("CGST", "₹0.00")
        c4.metric("SGST", "₹0.00")
    else:
        c2.metric("CGST", f"₹{agg['cgst_amount']:,.2f}")
        c3.metric("SGST", f"₹{agg['sgst_amount']:,.2f}")
        c4.metric("IGST", "₹0.00")
    c5.metric("Grand Total", f"₹{agg['grand_total']:,.2f}")

    supply_label = "Inter-state → IGST" if is_inter else f"Intra-state ({OUR_STATE_NAME}) → CGST+SGST"
    st.caption(f"Supply type: {supply_label}")


# ── NEW CREDIT NOTE ───────────────────────────────────────────────────

def _render_new_cn(inline: bool = False) -> None:
    if not inline:
        st.subheader("➕ Issue Credit Note")
        st.caption("Credit Notes reduce your GST output liability.")

    # Invoice lookup
    inv = _invoice_lookup_widget("cn")
    if not inv:
        return

    # Auto-fill party details — never editable fields, just display
    _party_name  = str(inv.get("party_name") or "")
    _party_gstin = str(inv.get("party_gstin") or "")
    _inv_date    = inv.get("invoice_date")

    # For retail invoices, party_name may be empty — show customer from order
    if not _party_name:
        _party_name = "Retail Customer"

    # Compact header strip
    st.markdown(
        f"<div style='background:#0a1628;border:1px solid #1e3a5f;border-radius:8px;"
        f"padding:10px 14px;margin:8px 0;display:flex;gap:20px;flex-wrap:wrap'>"
        f"<span style='color:#94a3b8;font-size:0.8rem'>Party: "
        f"<b style='color:#e2e8f0'>{_party_name}</b></span>"
        f"<span style='color:#94a3b8;font-size:0.8rem'>GSTIN: "
        f"<b style='color:#e2e8f0'>{_party_gstin or '—'}</b></span>"
        f"<span style='color:#94a3b8;font-size:0.8rem'>Invoice Date: "
        f"<b style='color:#e2e8f0'>{str(_inv_date)[:10] if _inv_date else '—'}</b></span>"
        f"<span style='color:#94a3b8;font-size:0.8rem'>Total: "
        f"<b style='color:#10b981'>₹{float(inv.get('grand_total') or 0):,.2f}</b></span>"
        f"</div>",
        unsafe_allow_html=True
    )

    # Compact form row
    _c1, _c2, _c3 = st.columns([3, 2, 2])
    with _c1:
        reason = st.selectbox(
            "Reason",
            options=list(CN_REASONS.keys()),
            format_func=lambda x: CN_REASONS.get(x, x),
            key="cn_reason",
            label_visibility="collapsed"
        )
    with _c2:
        pos = st.text_input(
            "Place of Supply", value=OUR_STATE_NAME,
            key="cn_pos", label_visibility="collapsed",
            placeholder="Place of Supply"
        )
    with _c3:
        supply_type = st.selectbox(
            "Supply Type", ["B2B", "B2C", "EXPORT"],
            key="cn_supply_type", label_visibility="collapsed",
            help="B2B=registered, B2C=unregistered, EXPORT=zero-rated"
        )

    orig_date    = _inv_date if _inv_date else date.today()
    reason_detail = st.text_input(
        "Additional details (optional)",
        placeholder="e.g. 5 pieces returned, price correction",
        key="cn_reason_detail"
    )

    # Lines — closed by default
    lines = _line_builder(inv.get("id") if inv else None, "cn")

    if lines:
        _tax_preview(lines, pos)

    # Step 5 — Remarks + submit
    remarks = st.text_area("Remarks", key="cn_remarks",
                           placeholder="Optional internal note")

    invoice_no = inv.get("invoice_no", "") if inv else st.text_input(
        "Invoice Reference No", key="cn_inv_no_manual",
        placeholder="INV/2526/0042"
    )

    st.markdown("---")

    if st.button("📝 Issue Credit Note", type="primary",
                 use_container_width=True, key="cn_submit"):
        if not invoice_no:
            st.error("Please provide an invoice reference number.")
            return
        if not _party_name:
            st.error("Party name is required.")
            return
        if not lines:
            st.error("Add at least one line item.")
            return

        ok, result = create_credit_note(
            invoice_no    = invoice_no,
            invoice_id    = inv.get("id") if inv else None,
            order_id      = None,
            party_id      = inv.get("party_id") if inv else None,
            party_name    = _party_name,
            party_gstin   = _party_gstin,
            place_of_supply = pos,
            supply_type   = supply_type,
            reason        = reason,
            reason_detail = reason_detail,
            lines         = lines,
            original_invoice_date = orig_date,
            remarks       = remarks,
            created_by    = current_user_name(),
        )

        if ok:
            st.success(f"✅ Credit Note **{result}** issued successfully!")
            st.balloons()
            # Clear prefill state
            st.session_state.pop("cn_prefilled_inv", None)
            st.session_state.pop("cdn_prefill_invoice_no", None)
            st.session_state.pop("cn_force_new", None)
            st.session_state[f"cn_issued_{invoice_no}"] = result
            # Signal invoice panel to close CDN and show success
            _inv_id = str(inv.get("id") or "") if inv else ""
            if _inv_id:
                st.session_state[f"cdn_issued_cn_{_inv_id}"] = result
                st.session_state.pop(f"show_cdn_{_inv_id}", None)
            st.rerun()
        else:
            st.error(f"❌ Failed: {result}")


# ── NEW DEBIT NOTE ────────────────────────────────────────────────────

def _render_new_dn() -> None:
    st.subheader("➕ Issue Debit Note")
    st.caption(
        "Debit Notes increase your GST output liability. "
        "Issue when an invoice undercharged the buyer."
    )

    inv = _invoice_lookup_widget("dn")
    st.markdown("---")

    st.markdown("#### 🏢 Party & GST Details")
    col1, col2, col3 = st.columns(3)
    with col1:
        party_name = st.text_input(
            "Party Name", value=inv.get("party_name", "") if inv else "",
            key="dn_party_name"
        )
    with col2:
        party_gstin = st.text_input(
            "Party GSTIN", value=inv.get("party_gstin", "") if inv else "",
            placeholder="22AAAAA0000A1Z5", key="dn_party_gstin"
        )
    with col3:
        pos = st.text_input(
            "Place of Supply",
            value=OUR_STATE_NAME,
            key="dn_pos"
        )

    col4, col5 = st.columns(2)
    with col4:
        supply_type = st.selectbox(
            "Supply Type", ["B2B", "B2C", "EXPORT"],
            key="dn_supply_type"
        )
    with col5:
        orig_date = st.date_input(
            "Original Invoice Date",
            value=inv.get("invoice_date") if inv else date.today(),
            key="dn_orig_date"
        )

    st.markdown("#### 📋 Reason for Debit Note")
    reason = st.selectbox(
        "Reason (as per Section 34, CGST Act)",
        options=list(DN_REASONS.keys()),
        format_func=lambda x: DN_REASONS.get(x, x),
        key="dn_reason"
    )
    reason_detail = st.text_input(
        "Additional details (optional)", key="dn_reason_detail",
        placeholder="e.g. Transport charges ₹500 not included in original invoice"
    )

    lines = _line_builder(inv.get("id") if inv else None, "dn")

    if lines:
        _tax_preview(lines, pos)

    remarks = st.text_area("Remarks", key="dn_remarks")

    invoice_no = inv.get("invoice_no", "") if inv else st.text_input(
        "Invoice Reference No", key="dn_inv_no_manual",
        placeholder="INV/2526/0042"
    )

    st.markdown("---")

    if st.button("📝 Issue Debit Note", type="primary",
                 use_container_width=True, key="dn_submit"):
        if not invoice_no:
            st.error("Please provide an invoice reference number.")
            return
        if not party_name:
            st.error("Party name is required.")
            return
        if not lines:
            st.error("Add at least one line item.")
            return

        ok, result = create_debit_note(
            invoice_no    = invoice_no,
            invoice_id    = inv.get("id") if inv else None,
            order_id      = None,
            party_id      = inv.get("party_id") if inv else None,
            party_name    = party_name,
            party_gstin   = party_gstin,
            place_of_supply = pos,
            supply_type   = supply_type,
            reason        = reason,
            reason_detail = reason_detail,
            lines         = lines,
            original_invoice_date = orig_date,
            remarks       = remarks,
            created_by    = current_user_name(),
        )

        if ok:
            st.success(f"✅ Debit Note **{result}** issued successfully!")
            st.balloons()
        else:
            st.error(f"❌ Failed: {result}")


# ── REGISTER ──────────────────────────────────────────────────────────

def _render_register() -> None:
    st.subheader("📋 Credit & Debit Note Register")

    # Filters
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        doc_type = st.radio("Document Type", ["Both", "Credit", "Debit"],
                             horizontal=True, key="reg_type")
    with col2:
        from_d = st.date_input("From", value=date.today().replace(day=1),
                                key="reg_from")
    with col3:
        to_d   = st.date_input("To", value=date.today(), key="reg_to")
    with col4:
        status_filter = st.selectbox("Status",
                                     ["All", "CONFIRMED", "CANCELLED", "DRAFT"],
                                     key="reg_status")

    status = None if status_filter == "All" else status_filter

    import pandas as pd

    if doc_type in ("Both", "Credit"):
        cns = list_credit_notes(from_date=from_d, to_date=to_d, status=status)
        if cns:
            st.markdown("##### 🟢 Credit Notes")
            _render_cdn_table(cns, "CN")
        elif doc_type == "Credit":
            st.info("No credit notes found for the selected filters.")

    if doc_type in ("Both", "Debit"):
        dns = list_debit_notes(from_date=from_d, to_date=to_d, status=status)
        if dns:
            st.markdown("##### 🔴 Debit Notes")
            _render_cdn_table(dns, "DN")
        elif doc_type == "Debit":
            st.info("No debit notes found for the selected filters.")

    if doc_type == "Both" and not cns and not dns:
        st.info("No credit or debit notes found for the selected filters.")


def _render_cdn_table(docs: List[Dict], doc_type: str) -> None:
    import pandas as pd

    display = []
    for d in docs:
        # Debug: check actual keys
        no    = d.get("cn_number") or d.get("dn_number") or d.get("doc_no") or ""
        dt    = str(d.get("cn_date") or d.get("dn_date") or d.get("doc_date") or "")[:10]
        reason_map = CN_REASONS if doc_type == "CN" else DN_REASONS
        display.append({
            "Doc No":          no,
            "Date":            dt,
            "Party":           d.get("party_name", ""),
            "GSTIN":           d.get("party_gstin", ""),
            "Ref Invoice":     d.get("invoice_no", ""),
            "Reason":          reason_map.get(d.get("reason",""), d.get("reason","")),
            "Taxable ₹":       f"{float(d.get('taxable_amount') or 0):,.2f}",
            "CGST ₹":          f"{float(d.get('cgst_amount') or 0):,.2f}",
            "SGST ₹":          f"{float(d.get('sgst_amount') or 0):,.2f}",
            "IGST ₹":          f"{float(d.get('igst_amount') or 0):,.2f}",
            "Grand Total ₹":   f"{float(d.get('grand_total') or 0):,.2f}",
            "Status":          d.get("status", ""),
            "Tally Exported":  "✅" if d.get("tally_exported_at") else "❌",
        })

    df = pd.DataFrame(display)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Cancel action (managers only)
    if has_role(MANAGER, ADMIN):
        with st.expander("⚠️ Cancel a Document", expanded=False):
            doc_numbers = [d.get("cn_number") or d.get("dn_number") for d in docs
                           if d.get("status") == "CONFIRMED"]
            if not doc_numbers:
                st.info("No CONFIRMED documents available to cancel.")
            else:
                cancel_no = st.selectbox("Select document to cancel", doc_numbers,
                                          key=f"cancel_select_{doc_type}")
                st.warning(
                    "⚠️ Only cancel BEFORE filing GSTR-1 for this period. "
                    "Once GSTR-1 is filed, issue a Debit Note instead of cancelling. "
                    "Cancelled notes are excluded from GSTR-1 and the number is retained in sequence."
                )
                if st.button(f"Cancel {cancel_no}", type="secondary",
                             key=f"cancel_btn_{doc_type}"):
                    doc = next((d for d in docs
                                if (d.get("cn_number") or d.get("dn_number")) == cancel_no), None)
                    if doc:
                        ok = cancel_cdn(doc_type, str(doc["id"]), current_user_name())
                        if ok:
                            st.success(f"✅ {cancel_no} cancelled.")
                            st.rerun()
                        else:
                            st.error("Cancellation failed.")


# ── TALLY EXPORT ──────────────────────────────────────────────────────

def _render_tally_export() -> None:
    st.subheader("📤 Tally Prime / Tally ERP 9 Export")
    st.caption(
        "Downloads a CSV file ready for import via Tally's Data Exchange. "
        "Only CONFIRMED documents are exported. "
        "Export marks documents as synced."
    )

    with st.container(border=True):
        st.markdown("**Export Settings**")
        col1, col2, col3 = st.columns(3)
        with col1:
            exp_from = st.date_input("From Date",
                                     value=date.today().replace(day=1),
                                     key="exp_from")
        with col2:
            exp_to   = st.date_input("To Date", value=date.today(), key="exp_to")
        with col3:
            exp_type = st.selectbox("Document Type",
                                    ["BOTH", "CN", "DN"],
                                    key="exp_type")

        mark_exp = st.checkbox(
            "Mark documents as Tally-exported after download",
            value=True, key="exp_mark"
        )

    if st.button("📥 Generate Tally CSV", type="primary", key="exp_btn"):
        csv_data = export_cdn_for_tally(
            doc_type       = exp_type,
            from_date      = exp_from,
            to_date        = exp_to,
            mark_exported  = mark_exp,
        )
        if not csv_data:
            st.info("No confirmed documents found for the selected period.")
        else:
            filename = f"CDN_Tally_{exp_from}_{exp_to}.csv"
            st.download_button(
                label      = f"⬇️ Download {filename}",
                data       = csv_data.encode("utf-8"),
                file_name  = filename,
                mime       = "text/csv",
                key        = "exp_download",
            )
            st.success(f"✅ CSV ready. {len(csv_data.splitlines())-1} records.")

    # Tally import guide
    with st.expander("📚 How to import into Tally Prime"):
        st.markdown("""
**Steps to import CDN into Tally Prime:**

1. Open Tally Prime → **Gateway of Tally** → **Data** → **Import**
2. Select **Vouchers**
3. Choose the downloaded CSV file
4. Tally will map columns automatically if using the standard format
5. Review the import log — any unmatched ledgers will be flagged
6. Approve and post

**Tally ERP 9 (older):**
1. Go to **Gateway of Tally** → **Import of Data** → **Vouchers**
2. Same file works for both versions

**Ledger mapping:**
- `Party Ledger` must match the ledger name exactly in Tally
- `CGST`, `SGST`, `IGST` must match your Tally GST duty ledgers
- If names differ, edit the CSV before importing

**For Credit Notes in Tally:**
- Voucher type → Credit Note
- Reduces Sales / CGST/SGST payable

**For Debit Notes in Tally:**
- Voucher type → Debit Note
- Increases Sales / CGST/SGST payable
        """)


# ── GSTR-1 PREVIEW ───────────────────────────────────────────────────

def _render_gstr1_preview() -> None:
    st.subheader("📊 GSTR-1 Table 9B — CDN Preview")
    st.caption(
        "This shows what gets reported in GSTR-1 Table 9B "
        "(Debit/Credit Notes — Registered Recipients). "
        "B2C notes appear in Table 10."
    )

    col1, col2 = st.columns(2)
    with col1:
        gst_from = st.date_input("Period From",
                                  value=date.today().replace(day=1),
                                  key="gst_from")
    with col2:
        gst_to   = st.date_input("Period To",
                                  value=date.today(), key="gst_to")

    if st.button("Load GSTR-1 Data", key="gst_load"):
        import json
        data = generate_gstr1_cdn_data(gst_from, gst_to)
        records = data.get("records", [])

        if not records:
            st.info(f"No B2B credit/debit notes in period {gst_from} → {gst_to}.")
            return

        st.success(f"✅ {data['count']} record(s) for GSTR-1 Table {data['table']}")
        st.caption(f"Period: {data['period']}")

        import pandas as pd
        df = pd.DataFrame(records)
        df.columns = [c.replace("_", " ").title() for c in df.columns]
        st.dataframe(df, use_container_width=True, hide_index=True)

        # JSON export for GST portal
        json_str = json.dumps(data, indent=2, default=str)
        st.download_button(
            label     = "⬇️ Download JSON (GST Portal Format)",
            data      = json_str.encode("utf-8"),
            file_name = f"GSTR1_Table9B_{gst_from}_{gst_to}.json",
            mime      = "application/json",
            key       = "gst_json_download",
        )

        # Summary totals
        st.markdown("**Totals**")
        cn_rows = [r for r in records if r["doc_type"] == "CREDIT"]
        dn_rows = [r for r in records if r["doc_type"] == "DEBIT"]
        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("Credit Notes (B2B)", len(cn_rows),
                   delta=f"₹{sum(r['taxable_value'] for r in cn_rows):,.0f} taxable")
        sc2.metric("Debit Notes (B2B)",  len(dn_rows),
                   delta=f"₹{sum(r['taxable_value'] for r in dn_rows):,.0f} taxable")
        sc3.metric("Net Tax Effect",
                   f"₹{sum(r['igst']+r['cgst']+r['sgst'] for r in dn_rows) - sum(r['igst']+r['cgst']+r['sgst'] for r in cn_rows):,.2f}",
                   help="Positive = DN > CN (more output tax). Negative = CN > DN (less output tax).")


# ── Allow standalone testing ──────────────────────────────────────────
if __name__ == "__main__":
    render_cdn_module()

# DEBUG TEMP - remove after testing
def _debug_cdn_lines(invoice_id):
    """Temporary debug function."""
    try:
        from modules.billing.credit_debit_note_manager import get_invoice_lines_for_cdn
        lines = get_invoice_lines_for_cdn(invoice_id)
        import streamlit as st
        st.caption(f"DEBUG: invoice_id={invoice_id!r}, lines={len(lines)}, "
                   f"first={lines[0] if lines else 'EMPTY'}")
    except Exception as e:
        import streamlit as st
        st.caption(f"DEBUG ERROR: {e}")
