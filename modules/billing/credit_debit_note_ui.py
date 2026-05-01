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
            for i, il in enumerate(inv_lines):
                key = f"{key_prefix}_line_{i}"
                with st.container(border=True):
                    col1, col2, col3, col4, col5 = st.columns([3, 1, 1, 1, 1])
                    with col1:
                        _line_id = str(il.get("id") or "")
                        _already_cn = _credited.get(_line_id)
                        if _already_cn:
                            st.markdown(
                                f"<div style='color:#94a3b8;font-size:0.82rem'>"
                                f"✅ {il.get('product_name','Item')}</div>"
                                f"<div style='color:#10b981;font-size:0.7rem'>"
                                f"Credited: {_already_cn}</div>",
                                unsafe_allow_html=True
                            )
                            include = False
                        else:
                            include = st.checkbox(
                                f"{il.get('product_name','Item')}",
                                value=True, key=f"{key}_include"
                            )
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

                    if include and qty > 0:
                        lines.append({
                            "product_name":    il.get("product_name", ""),
                            "hsn_sac_code":    il.get("hsn_sac_code", ""),
                            "invoice_line_id": il.get("id"),
                            "quantity":        qty,
                            "unit_price":      unit_price,
                            "taxable_amount":  taxable,
                            "gst_percent":     gst_pct,
                        })

    # Manual line entry
    with st.expander("➕ Add Manual Line Item", expanded=False):
        mc1, mc2, mc3, mc4 = st.columns([3, 1, 1, 1])
        with mc1:
            m_name = st.text_input("Product / Service", key=f"{key_prefix}_m_name")
        with mc2:
            m_qty  = st.number_input("Qty", min_value=0.0, step=0.5, key=f"{key_prefix}_m_qty")
        with mc3:
            m_rate = st.number_input("Rate ₹", min_value=0.0, step=0.5, key=f"{key_prefix}_m_rate")
        with mc4:
            m_gst  = st.number_input("GST %", min_value=0.0, max_value=28.0,
                                     value=12.0, step=0.5, key=f"{key_prefix}_m_gst")
        m_hsn = st.text_input("HSN/SAC Code (optional)", key=f"{key_prefix}_m_hsn")

        if st.button("Add Line", key=f"{key_prefix}_m_add") and m_name and m_qty > 0:
            lines.append({
                "product_name":   m_name,
                "hsn_sac_code":   m_hsn,
                "invoice_line_id": None,
                "quantity":       m_qty,
                "unit_price":     m_rate,
                "taxable_amount": round(m_qty * m_rate, 2),
                "gst_percent":    m_gst,
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
