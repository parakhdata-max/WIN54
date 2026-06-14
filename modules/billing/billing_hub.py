"""
modules/billing/billing_hub.py
================================
Dedicated Billing Hub — raise challan / invoice for any bill-ready order.

Flow:
  1. Select party  (only parties with bill-ready, unbilled orders shown)
  2. Select orders (stock-allocated or procured orders)
  3. Preview       (invoice-style, CL batch/expiry auto-filled from PA, editable)
  4. Save          → create_challan() or create_invoice() based on party preference

Alternate route: production_page "Open Billing" remains unchanged.

Entry point: render_billing_hub()
"""
from __future__ import annotations
import datetime as _dt
import decimal  as _dec
import html     as _hesc
import json     as _json
import re       as _re
import streamlit as st


# ── DB helpers ────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}"); return []

def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {}); return True
    except Exception as e:
        st.error(f"Write: {e}"); return False


# ── Rounding helper ────────────────────────────────────────────────────────────

def _r2(v):
    try:
        return float(_dec.Decimal(str(v)).quantize(
            _dec.Decimal("0.01"), rounding=_dec.ROUND_HALF_UP))
    except Exception:
        return round(float(v or 0), 2)


def _service_qty_label(row: dict, fallback_qty: int) -> str:
    eye = str((row or {}).get("eye_side") or "").upper()
    is_service = bool((row or {}).get("is_service_line")) or eye in ("S", "SERVICE")
    if not is_service:
        return str(fallback_qty)
    lp = (row or {}).get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = _json.loads(lp)
        except Exception:
            lp = {}
    try:
        factor = float((lp or {}).get("service_qty_factor") or 0)
    except Exception:
        factor = 0.0
    if factor > 0:
        if abs(factor - 0.5) < 0.001:
            return "0.5 pair"
        if abs(factor - 1.0) < 0.001:
            return "1 pair"
        return f"{factor:g} pair"
    return "1 service"


def _scan_norm(value: str) -> str:
    return _re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _scan_tokens(raw: str) -> list[str]:
    tokens = []
    for part in _re.split(r"[\s,;\n\r\t]+", str(raw or "")):
        tok = part.strip()
        if tok:
            tokens.append(tok)
    return tokens


def _party_scan_match(scan: str, party: dict) -> bool:
    if not scan:
        return False
    n = _scan_norm(scan)
    hay = [
        party.get("party_name"),
        party.get("mobile"),
        party.get("party_key"),
        party.get("party_type"),
    ]
    return any(n and n in _scan_norm(x) for x in hay)


def _order_scan_match(token: str, order: dict) -> bool:
    n = _scan_norm(token)
    if not n:
        return False
    order_no = str(order.get("order_no") or "")
    ono = _scan_norm(order_no)
    if n == ono or n in ono:
        return True
    # Support short counter scans like R009, R-009, 0010-FIT, 0010-COL.
    m = _re.search(r"(\d{1,5})(?:\s*[-_/]?\s*(FIT|FITTING|COL|COLOUR|COLOR|COLOURING|C))?$", str(token or ""), _re.I)
    if not m:
        return False
    num = m.group(1).zfill(4)
    suffix = (m.group(2) or "").upper()
    if not order_no.endswith(num) and f"/{num}" not in order_no and f"{num}" not in order_no:
        return False
    if suffix.startswith("FIT"):
        return order_no.upper().endswith("-F") or "FIT" in order_no.upper()
    if suffix.startswith(("COL", "COLOUR", "COLOR")) or suffix == "C":
        return order_no.upper().endswith("-C") or "COL" in order_no.upper()
    return True


def _billing_totals_for_lines(lines: list[dict]) -> tuple[float, float, float]:
    taxable = gst_total = grand = 0.0
    for ln in lines or []:
        qty = int(ln.get("unbilled_qty") or ln.get("quantity") or 1)
        unit = _r2(ln.get("unit_price") or 0)
        disc = _r2(ln.get("discount_amount") or 0)
        gst_pct = float(ln.get("gst_percent") or 0)
        order_type = str(ln.get("order_type") or "WHOLESALE").upper()
        line_taxable = _r2(unit * qty - disc)
        if order_type == "RETAIL":
            line_gst = _r2(line_taxable * gst_pct / (100 + gst_pct)) if gst_pct else 0.0
            line_total = line_taxable
        else:
            line_gst = _r2(line_taxable * gst_pct / 100)
            line_total = _r2(line_taxable + line_gst)
        taxable = _r2(taxable + line_taxable)
        gst_total = _r2(gst_total + line_gst)
        grand = _r2(grand + line_total)
    return taxable, gst_total, grand


# ── Power string helper ───────────────────────────────────────────────────────

def _pwr(row):
    parts = []
    try:
        if row.get("sph") is not None:
            parts.append(f"SPH {float(row['sph']):+.2f}")
        if row.get("cyl") and abs(float(row["cyl"])) > 0.01:
            parts.append(f"CYL {float(row['cyl']):+.2f}")
        if row.get("axis"):
            parts.append(f"AX {int(row['axis'])}°")
        if row.get("add_power") and float(row.get("add_power") or 0) > 0:
            parts.append(f"ADD +{float(row['add_power']):.2f}")
    except Exception:
        pass
    return "  ".join(parts)


# ── Load procured order lines with PA data ────────────────────────────────────

def _load_order_lines(order_ids: list[str]) -> list[dict]:
    """
    Load all unbilled lines for the selected orders, enriched with
    batch/expiry from purchase_acknowledgements and procurement_order_items.
    """
    if not order_ids:
        return []
    return _q("""
        SELECT
            ol.id::text         AS line_id,
            ol.order_id::text   AS order_id,
            o.order_no,
            o.order_type,
            COALESCE(o.patient_name, o.party_name, '—') AS patient_name,
            ol.eye_side,
            COALESCE(p.product_name,
                     ol.lens_params->>'service_display_name',
                     ol.lens_params->>'display_product_name',
                     ol.lens_params->>'service_description',
                     'Service')              AS product_name,
            COALESCE(p.brand,'')             AS brand,
            COALESCE(p.main_group,
                     CASE WHEN COALESCE(ol.is_service_line,FALSE) THEN 'Services' ELSE '' END) AS main_group,
            COALESCE(ol.is_service_line, FALSE) AS is_service_line,
            ol.lens_params,
            COALESCE(p.is_batch_applicable, FALSE) AS batch_req,
            ol.quantity,
            COALESCE(ol.billed_qty, 0)       AS billed_qty,
            ol.quantity - COALESCE(ol.billed_qty,0) AS unbilled_qty,
            ol.unit_price,
            COALESCE(ol.billing_total, ol.total_price,
                ROUND(ol.unit_price * ol.quantity
                      - COALESCE(ol.discount_amount,0), 2)) AS billing_total,
            COALESCE(ol.gst_percent, 0)      AS gst_percent,
            COALESCE(ol.gst_amount, 0)       AS gst_amount,
            COALESCE(ol.discount_amount, 0)  AS discount_amount,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            -- PA data
            pa.id::text                      AS pa_id,
            pa.supplier_name                 AS pa_supplier,
            COALESCE(NULLIF(pa.challan_no,''), NULLIF(pa.invoice_no,'')) AS pa_ref,
            pa.billing_status                AS pa_status,
            COALESCE(pa.purchase_price, 0)   AS purchase_price,
            -- Batch/expiry: procurement_order_items first, PA as fallback
            COALESCE(NULLIF(poi.batch_no,''), NULLIF(pa.batch_no,''))    AS batch_no,
            COALESCE(poi.expiry_date, pa.expiry_date)                    AS expiry_date
        FROM order_lines ol
        JOIN orders o   ON o.id  = ol.order_id
        LEFT JOIN products p ON p.id  = ol.product_id
        LEFT JOIN purchase_acknowledgements pa  ON pa.order_line_id = ol.id
        LEFT JOIN procurement_order_items   poi ON poi.order_line_id = ol.id
        WHERE ol.order_id = ANY(%(ids)s::uuid[])
          AND COALESCE(ol.is_deleted, FALSE)    = FALSE
          AND COALESCE(ol.billed_qty, 0) < ol.quantity
        ORDER BY o.order_no,
                 CASE WHEN ol.eye_side='R' THEN 0
                      WHEN ol.eye_side='L' THEN 1
                      ELSE 2 END
    """, {"ids": order_ids})


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════════

def render_billing_hub():
    # ── Page header ─────────────────────────────────────────────────────────
    st.markdown(
        "<div style='background:#0a1628;border:1px solid #1e3a5f;"
        "border-left:4px solid #22c55e;border-radius:8px;"
        "padding:10px 16px;margin-bottom:14px'>"
        "<span style='color:#4ade80;font-size:1rem;font-weight:800'>"
        "🧾 Billing Hub</span>"
        "<span style='color:#475569;font-size:0.78rem;margin-left:10px'>"
        "Club bill-ready orders into one challan/invoice across Retail, Wholesale, and Bulk.</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    scan_tab, manual_tab = st.tabs(["⚡ Scan Billing", "📋 Manual Billing Hub"])

    with scan_tab:
        _render_scan_billing_tab()

    with manual_tab:
        _render_manual_billing_hub()


def _render_scan_billing_tab():
    st.caption(
        "Scan/select party, then scan order numbers. Only bill-ready orders for that party are accepted."
    )
    try:
        from modules.billing.challan_invoice_manager import (
            get_parties_with_procured_orders,
            get_procured_orders_for_party,
            get_party_billing_preference,
        )
    except Exception as exc:
        st.error(f"Billing scan setup failed: {exc}")
        return

    parties = get_parties_with_procured_orders()
    if not parties:
        st.info("No bill-ready parties found.")
        return

    p1, p2 = st.columns([2, 3])
    party_scan = p1.text_input(
        "Scan / type party",
        key="bh_scan_party_text",
        placeholder="Party barcode / name / mobile",
    )
    matched_party = next((p for p in parties if _party_scan_match(party_scan, p)), None)
    party_keys = [p["party_key"] for p in parties]
    party_labels = {
        p["party_key"]: (
            f"{p['party_name']}"
            + (f" · {p['mobile']}" if p.get("mobile") else "")
            + f" · {p.get('party_type','')}"
            + f" ({p['order_count']} ready)"
        )
        for p in parties
    }
    default_idx = 0
    if matched_party and matched_party.get("party_key") in party_keys:
        default_idx = party_keys.index(matched_party["party_key"]) + 1
    selected_party = p2.selectbox(
        "Party",
        options=[""] + party_keys,
        index=default_idx,
        format_func=lambda x: "— Select scanned party —" if not x else party_labels.get(x, x),
        key="bh_scan_party",
    )
    if not selected_party:
        st.info("Scan/select a party first.")
        return

    orders = get_procured_orders_for_party(selected_party)
    if not orders:
        st.warning("Selected party has no bill-ready pending orders.")
        return

    pref = (get_party_billing_preference(selected_party) or "C").upper()
    doc_type = "Invoice" if pref in ("I", "DIRECT_INVOICE", "INVOICE") else "Challan"
    if selected_party.startswith("name:"):
        doc_type = "Challan"

    st.markdown(
        f"**Document mode:** `{doc_type}` · **Ready orders:** `{len(orders)}`"
    )

    order_tokens = st.text_area(
        "Scan order numbers",
        key="bh_scan_orders_text",
        height=110,
        placeholder="Example: R-002-Fit  R002-Col  R-009  R/2627/0010",
        help="Scan one order then press Enter. Use Generate after all scans are listed.",
    )

    tokens = _scan_tokens(order_tokens)
    selected_orders = []
    missing = []
    seen = set()
    for tok in tokens:
        match = next((o for o in orders if _order_scan_match(tok, o)), None)
        if not match:
            missing.append(tok)
            continue
        oid = str(match.get("order_id") or "")
        if oid and oid not in seen:
            seen.add(oid)
            selected_orders.append(match)

    if tokens:
        if missing:
            st.error("Not found / not bill-ready for this party: " + ", ".join(missing))
        if selected_orders:
            st.success(f"{len(selected_orders)} order(s) selected by scan.")
            st.dataframe(
                [
                    {
                        "Order": o.get("order_no"),
                        "Patient": o.get("patient_name"),
                        "Type": o.get("order_type"),
                        "Lines": o.get("line_count"),
                        "Value": float(o.get("total_value") or 0),
                    }
                    for o in selected_orders
                ],
                use_container_width=True,
                hide_index=True,
            )

    order_ids = [str(o.get("order_id")) for o in selected_orders if o.get("order_id")]
    lines = _load_order_lines(order_ids)
    taxable, gst_total, grand_total = _billing_totals_for_lines(lines)

    if selected_orders:
        c1, c2, c3 = st.columns(3)
        c1.metric("Taxable", f"₹{taxable:,.2f}")
        c2.metric("GST", f"₹{gst_total:,.2f}")
        c3.metric("Total", f"₹{grand_total:,.2f}")

    remarks = st.text_input(
        "Remarks",
        key="bh_scan_remarks",
        placeholder="Optional scan-billing note",
    )

    go = st.button(
        f"Generate {doc_type} + Print",
        type="primary",
        use_container_width=True,
        key="bh_scan_generate",
        disabled=not order_ids or bool(missing) or grand_total <= 0,
    )
    if not go:
        return

    try:
        from modules.billing.challan_invoice_manager import create_challan, create_invoice
        from modules.printing.print_opener import open_html_print
        doc_no = ""
        if doc_type == "Invoice":
            doc_no = create_invoice(
                challan_id=None,
                party_id=selected_party,
                order_ids=order_ids,
                total_amount=taxable,
                total_tax=gst_total,
                remarks=remarks.strip() or "Fast scan billing",
            )
            if doc_no:
                from modules.billing.smart_print import render_smart_invoice
                html = render_smart_invoice(doc_no, return_html=True)
                open_html_print(html, f"invoice_{doc_no}.html")
        else:
            doc_no = create_challan(
                party_id=selected_party if not selected_party.startswith("name:") else "",
                order_ids=order_ids,
                total_amount=taxable,
                total_tax=gst_total,
                remarks=remarks.strip() or "Fast scan billing",
            )
            if doc_no:
                from modules.billing.smart_print import render_smart_challan
                html = render_smart_challan(doc_no, return_html=True)
                open_html_print(html, f"challan_{doc_no}.html")
        if not doc_no:
            st.error("Document was not created. Check party preference and billing gate messages above.")
            return
        st.success(f"{doc_type} created and opened for print: {doc_no}")
        st.session_state["bh_scan_last_doc"] = doc_no
    except Exception as exc:
        st.error(f"Fast scan billing failed: {exc}")


def _render_manual_billing_hub():
    # ── Step 1: Party selector ───────────────────────────────────────────────
    with st.container(border=True):
        _ph1, _ph2 = st.columns([5, 1])
        _ph1.markdown(
            "<div style='color:#a5b4fc;font-size:0.8rem;font-weight:700;"
            "margin-bottom:6px'>Step 1 — Select Party</div>",
            unsafe_allow_html=True,
        )
        if _ph2.button("🔄 Refresh", key="bh_refresh", use_container_width=True):
            for _k in list(st.session_state.keys()):
                if _k.startswith("bh_"):
                    del st.session_state[_k]
            st.rerun()

        try:
            from modules.billing.challan_invoice_manager import get_parties_with_procured_orders
            _parties = get_parties_with_procured_orders()
        except Exception as _pe:
            st.error(f"Could not load parties: {_pe}")
            return

        if not _parties:
            st.info(
                "No parties with fully-procured orders found. "
                "Complete procurement for an order first, then return here to bill."
            )
            return

        _party_keys  = [p["party_key"] for p in _parties]
        _party_labels = {
            p["party_key"]: (
                f"{p['party_name']}"
                + (f" · {p['mobile']}" if p.get("mobile") else "")
                + f"  ({p['order_count']} order(s) ready)"
            )
            for p in _parties
        }

        _sel_party = st.selectbox(
            "Party",
            options=[""] + _party_keys,
            format_func=lambda x: "— Select party —" if not x else _party_labels.get(x, x),
            key="bh_party",
            label_visibility="collapsed",
        )

    if not _sel_party:
        return

    # ── Step 2: Order picker ─────────────────────────────────────────────────
    try:
        from modules.billing.challan_invoice_manager import get_procured_orders_for_party
        _orders = get_procured_orders_for_party(_sel_party)
    except Exception as _oe:
        st.error(f"Could not load orders: {_oe}")
        return

    if not _orders:
        st.warning("No procured orders found for this party.")
        return

    with st.container(border=True):
        st.markdown(
            "<div style='color:#a5b4fc;font-size:0.8rem;font-weight:700;"
            "margin-bottom:8px'>Step 2 — Select Order(s)</div>",
            unsafe_allow_html=True,
        )

        # Order cards with checkboxes
        _sel_order_ids = []
        for _ord in _orders:
            _oid   = _ord["order_id"]
            _ono   = _ord.get("order_no","—")
            _pat   = _hesc.escape(str(_ord.get("patient_name","—")))
            _otype = str(_ord.get("order_type","WHOLESALE")).upper()
            _val   = _r2(_ord.get("total_value",0))
            _sup   = _hesc.escape(str(_ord.get("suppliers") or "—"))
            _ref   = _hesc.escape(str(_ord.get("ref_nos") or "—"))
            _date  = str(_ord.get("last_doc_date",""))[:10]
            _lc    = int(_ord.get("line_count",0))
            _otype_color = {"RETAIL":"#f59e0b","WHOLESALE":"#3b82f6","BULK":"#8b5cf6"}.get(_otype,"#64748b")

            _ck1, _ck2 = st.columns([0.3, 7])
            _checked = _ck1.checkbox("", key=f"bh_ord_{_oid}", value=False,
                                      label_visibility="collapsed")
            with _ck2:
                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                    f"border-radius:6px;padding:8px 12px;margin-bottom:2px'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                    f"<span style='color:#e2e8f0;font-weight:700;font-family:monospace'>{_ono}</span>"
                    f"<span style='background:{_otype_color}22;color:{_otype_color};"
                    f"font-size:0.68rem;font-weight:700;padding:2px 8px;border-radius:4px'>{_otype}</span>"
                    f"</div>"
                    f"<div style='color:#94a3b8;font-size:0.73rem;margin-top:3px'>"
                    f"👤 {_pat} · {_lc} line(s) · "
                    f"<b style='color:#10b981'>₹{_val:,.2f}</b>"
                    f"</div>"
                    f"<div style='color:#475569;font-size:0.68rem;margin-top:2px'>"
                    f"🏭 {_sup} · Ref: {_ref}"
                    + (f" · Procured: {_date}" if _date else "")
                    + f"</div></div>",
                    unsafe_allow_html=True,
                )
            if _checked:
                _sel_order_ids.append(_oid)

    if not _sel_order_ids:
        st.caption("Tick one or more orders above to preview the bill.")
        return

    # ── Step 3: Load lines + Preview ─────────────────────────────────────────
    _lines = _load_order_lines(_sel_order_ids)
    if not _lines:
        st.warning("No unbilled lines found for selected orders.")
        return

    # Party doc preference (C=Challan first, I=Direct invoice)
    try:
        from modules.billing.challan_invoice_manager import get_party_billing_preference
        _doc_pref = get_party_billing_preference(_sel_party)
    except Exception:
        _doc_pref = "C"

    with st.container(border=True):
        st.markdown(
            "<div style='color:#a5b4fc;font-size:0.8rem;font-weight:700;"
            "margin-bottom:8px'>Step 3 — Preview & Confirm</div>",
            unsafe_allow_html=True,
        )

        # Doc type: locked to Challan for Retail, selectable for Wholesale/Bulk
        _all_retail = all(
            str(_ln.get("order_type","WHOLESALE")).upper() == "RETAIL"
            for _ln in _lines
        )
        _has_retail = any(
            str(_ln.get("order_type","WHOLESALE")).upper() == "RETAIL"
            for _ln in _lines
        )

        _bh1, _bh2, _bh3 = st.columns([2, 2, 3])
        if _all_retail or _has_retail:
            # Retail → always Challan, no choice
            _doc_type = "Challan"
            _bh1.markdown(
                "<div style='background:#0f172a;border:1px solid #f59e0b;"
                "border-radius:6px;padding:7px 12px;font-size:0.8rem;"
                "color:#fbbf24;font-weight:700'>📋 Challan (Retail — fixed)</div>",
                unsafe_allow_html=True,
            )
        else:
            _doc_options = ["Challan", "Invoice"] if _doc_pref == "C" else ["Invoice", "Challan"]
            _doc_type = _bh1.selectbox(
                "Document Type", _doc_options, key="bh_doctype",
                label_visibility="collapsed",
            )
        _bill_date = _bh2.date_input(
            "Billing Date", value=_dt.date.today(), key="bh_date",
            format="DD/MM/YYYY", label_visibility="collapsed",
        )
        _remarks = _bh3.text_input(
            "Remarks (optional)", key="bh_remarks",
            placeholder="e.g. Against PO / Ref / Special instruction",
            label_visibility="collapsed",
        )

        st.markdown("<div style='margin:6px 0'></div>", unsafe_allow_html=True)

        # Column headers
        _hcols = st.columns([3, 0.6, 1.2, 1, 1.1, 1.1, 1.2])
        for _col, _lbl in zip(_hcols, ["Product / Order","Qty","Unit ₹","Disc ₹","Taxable ₹","GST ₹","Total ₹"]):
            _col.markdown(
                f"<div style='font-size:0.65rem;font-weight:700;color:#475569;"
                f"text-transform:uppercase;border-bottom:1px solid #1e3a5f;"
                f"padding-bottom:3px'>{_lbl}</div>",
                unsafe_allow_html=True,
            )

        _grand_taxable = _grand_gst = _grand_total = 0.0
        _preview_lines = []

        for _idx, _ln in enumerate(_lines):
            _lid      = _ln["line_id"]
            _ono      = _ln.get("order_no","—")
            _eye      = str(_ln.get("eye_side","")).upper()
            _pname    = _hesc.escape(str(_ln.get("product_name","—")))
            _brand    = _hesc.escape(str(_ln.get("brand","") or ""))
            _pwr_str  = _pwr(_ln)
            _otype    = str(_ln.get("order_type","WHOLESALE")).upper()
            _qty      = int(_ln.get("unbilled_qty") or _ln.get("quantity") or 1)
            _qty_label = _service_qty_label(_ln, _qty)
            _gst_pct  = float(_ln.get("gst_percent") or 0)
            _pa_ref   = _hesc.escape(str(_ln.get("pa_ref") or ""))
            _pa_sup   = _hesc.escape(str(_ln.get("pa_supplier") or ""))
            _is_cl    = "contact" in str(_ln.get("main_group","")).lower() or bool(_ln.get("batch_req"))

            _lc1, _lc2, _lc3, _lc4, _lc5, _lc6, _lc7 = st.columns([3, 0.6, 1.2, 1, 1.1, 1.1, 1.2])

            with _lc1:
                _meta = []
                if _pwr_str: _meta.append(_pwr_str)
                if _brand:   _meta.append(_brand)
                st.markdown(
                    f"<div style='font-size:0.8rem;color:#e2e8f0;font-weight:600'>"
                    f"{_eye} — {_pname}</div>"
                    f"<div style='font-size:0.67rem;color:#64748b'>"
                    f"{_ono}"
                    + (f" · {'  '.join(_meta)}" if _meta else "")
                    + (f"<br><span style='color:#38bdf8'>🏭 {_pa_sup}"
                       + (f" · {_pa_ref}" if _pa_ref else "")
                       + "</span>" if _pa_sup else "")
                    + f"</div>",
                    unsafe_allow_html=True,
                )
                # CL batch/expiry — auto-filled from PA, editable
                if _is_cl:
                    _batch_val  = str(_ln.get("batch_no") or "")
                    _expiry_val = _ln.get("expiry_date")
                    _batch_in   = st.text_input(
                        "Batch", value=_batch_val, key=f"bh_batch_{_idx}_{_lid}",
                        placeholder="Batch no.", label_visibility="collapsed",
                    )
                    _expiry_in  = st.text_input(
                        "Expiry", value=str(_expiry_val)[:7] if _expiry_val else "",
                        key=f"bh_expiry_{_idx}_{_lid}",
                        placeholder="YYYY-MM",
                        label_visibility="collapsed",
                        help="Format: YYYY-MM or YYYY-MM-DD",
                    )
                else:
                    _batch_in  = ""
                    _expiry_in = ""

            with _lc2:
                st.markdown(
                    f"<div style='font-size:0.82rem;color:#94a3b8;"
                    f"padding-top:6px'>{_qty_label}</div>",
                    unsafe_allow_html=True,
                )

            with _lc3:
                _unit_price = _r2(st.number_input(
                    "Unit ₹", value=float(_ln.get("unit_price") or 0),
                    min_value=0.0, step=0.01, format="%.2f",
                    key=f"bh_price_{_idx}_{_lid}",
                    label_visibility="collapsed",
                ))

            with _lc4:
                _disc = _r2(st.number_input(
                    "Disc ₹", value=float(_ln.get("discount_amount") or 0),
                    min_value=0.0, step=0.5, format="%.2f",
                    key=f"bh_disc_{_idx}_{_lid}",
                    label_visibility="collapsed",
                ))

            # Compute totals per billing_engine rules
            _base_before_disc = _r2(_unit_price * _qty)
            _taxable = _r2(_base_before_disc - _disc)

            if _otype == "RETAIL":
                # MRP-inclusive: extract GST
                _gst_amt  = _r2(_taxable * _gst_pct / (100 + _gst_pct)) if _gst_pct else 0.0
                _line_tot = _taxable
            else:
                _gst_amt  = _r2(_taxable * _gst_pct / 100)
                _line_tot = _r2(_taxable + _gst_amt)

            _grand_taxable = _r2(_grand_taxable + _taxable)
            _grand_gst     = _r2(_grand_gst + _gst_amt)
            _grand_total   = _r2(_grand_total + _line_tot)

            with _lc5:
                st.markdown(
                    f"<div style='font-size:0.82rem;color:#94a3b8;"
                    f"padding-top:6px'>₹{_taxable:,.2f}</div>",
                    unsafe_allow_html=True,
                )
            with _lc6:
                _gst_lbl = f"{_gst_pct:.0f}%" if _gst_pct else "—"
                st.markdown(
                    f"<div style='font-size:0.82rem;color:#f59e0b;"
                    f"padding-top:6px'>₹{_gst_amt:,.2f}"
                    f"<span style='font-size:0.63rem;color:#475569'> ({_gst_lbl})</span></div>",
                    unsafe_allow_html=True,
                )
            with _lc7:
                st.markdown(
                    f"<div style='font-size:0.88rem;color:#10b981;"
                    f"font-weight:700;padding-top:6px'>₹{_line_tot:,.2f}</div>",
                    unsafe_allow_html=True,
                )

            _preview_lines.append({
                "line_id":          _lid,
                "order_id":         _ln["order_id"],
                "unit_price":       _unit_price,
                "qty":              _qty,
                "discount":         _disc,
                "taxable":          _taxable,
                "gst_amt":          _gst_amt,
                "total":            _line_tot,
                "batch_no":         _batch_in,
                "expiry":           _expiry_in,
                "is_cl":            _is_cl,
                "product_name":     _ln.get("product_name",""),
                "eye_side":         _ln.get("eye_side",""),
                # Originals — used in _do_billing to detect user-changed values only
                "_orig_unit_price": float(_ln.get("unit_price") or 0),
                "_orig_discount":   float(_ln.get("discount_amount") or 0),
            })

        # Totals row
        st.markdown("<div style='border-top:2px solid #1e3a5f;margin:4px 0 2px 0'></div>",
                    unsafe_allow_html=True)
        _t1, _t2, _t3, _t4, _t5, _t6, _t7 = st.columns([3, 0.6, 1.2, 1, 1.1, 1.1, 1.2])
        _t1.markdown(
            f"<div style='font-size:0.8rem;font-weight:700;color:#a5b4fc;"
            f"padding-top:4px'>{len(_preview_lines)} line(s) · "
            f"{len(_sel_order_ids)} order(s)</div>",
            unsafe_allow_html=True,
        )
        _t5.markdown(
            f"<div style='font-size:0.8rem;font-weight:700;color:#94a3b8;"
            f"padding-top:4px'>₹{_grand_taxable:,.2f}</div>",
            unsafe_allow_html=True,
        )
        _t6.markdown(
            f"<div style='font-size:0.8rem;font-weight:700;color:#f59e0b;"
            f"padding-top:4px'>₹{_grand_gst:,.2f}</div>",
            unsafe_allow_html=True,
        )
        _t7.markdown(
            f"<div style='font-size:0.95rem;font-weight:800;color:#10b981;"
            f"padding-top:4px'>₹{_grand_total:,.2f}</div>",
            unsafe_allow_html=True,
        )

        # GST split
        if _grand_gst > 0:
            _cgst = _r2(_grand_gst / 2)
            _sgst = _r2(_grand_gst - _cgst)
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                f"border-radius:5px;padding:6px 14px;margin:4px 0;"
                f"font-size:0.75rem;color:#94a3b8'>"
                f"Taxable: <b style='color:#e2e8f0'>₹{_grand_taxable:,.2f}</b>"
                f"&nbsp;·&nbsp;CGST: <b style='color:#f59e0b'>₹{_cgst:,.2f}</b>"
                f"&nbsp;·&nbsp;SGST: <b style='color:#f59e0b'>₹{_sgst:,.2f}</b>"
                f"&nbsp;·&nbsp;Grand Total: <b style='color:#10b981'>₹{_grand_total:,.2f}</b>"
                f"</div>",
                unsafe_allow_html=True,
            )

        # CL validation
        _cl_issues = []
        for _pl in _preview_lines:
            if _pl["is_cl"]:
                if not _pl["batch_no"].strip():
                    _cl_issues.append(f"Batch no. missing for {_pl['eye_side']} {_pl['product_name']}")
                if not _pl["expiry"].strip():
                    _cl_issues.append(f"Expiry missing for {_pl['eye_side']} {_pl['product_name']}")
        if _cl_issues:
            for _ci in _cl_issues:
                st.warning(f"⚠️ {_ci}")

        # ── Step 4: Discount gate + Save ─────────────────────────────────────
        st.markdown("---")

        # Discount gate — check before showing save button
        try:
            from modules.pricing.discount_audit import render_billing_gate
            _gate_ok = render_billing_gate(_sel_order_ids)
        except Exception:
            _gate_ok = True  # If audit module missing, don't block billing

        if not _gate_ok:
            return  # Gate rendered its own UI with fix/confirm options
        _preflight_ok = True
        _preflight_issues = []
        try:
            from modules.billing.challan_invoice_manager import audit_billing_preflight
            _preflight_ok, _preflight_issues = audit_billing_preflight(_sel_order_ids, None)
        except Exception as _pf_err:
            _preflight_ok = False
            _preflight_issues = [f"Billing preflight audit could not run: {_pf_err}"]
        if not _preflight_ok:
            st.error(
                "Guided billing lock: this order has possible missing/unusual collectables. "
                "Open Backoffice Billing Summary for the order and use "
                "'Add missed service / collectable before billing' first."
            )
            for _issue in _preflight_issues:
                st.write("•", _issue)
        _sv1, _sv2 = st.columns([5, 1])
        _can_save = not _cl_issues and _grand_total > 0 and _preflight_ok
        _do_save = _sv1.button(
            f"{'📋 Create Challan' if _doc_type == 'Challan' else '🧾 Create Invoice'}"
            f"  ({len(_sel_order_ids)} order(s) · ₹{_grand_total:,.2f})",
            type="primary", use_container_width=True, key="bh_save",
            disabled=not _can_save,
        )
        _sv2.button("↩ Reset", key="bh_reset", use_container_width=True,
                    on_click=lambda: [st.session_state.pop(_k, None)
                                      for _k in list(st.session_state.keys())
                                      if _k.startswith("bh_")])

        if _do_save and _can_save:
            _do_billing(
                party_key    = _sel_party,
                order_ids    = _sel_order_ids,
                doc_type     = _doc_type,
                bill_date    = _bill_date,
                remarks      = _remarks.strip(),
                preview_lines= _preview_lines,
                grand_taxable= _grand_taxable,
                grand_gst    = _grand_gst,
                grand_total  = _grand_total,
            )


# ── Save handler ──────────────────────────────────────────────────────────────

def _do_billing(
    party_key, order_ids, doc_type, bill_date,
    remarks, preview_lines, grand_taxable, grand_gst, grand_total,
):
    import traceback as _tb

    # 1. Write back any edited batch/expiry to PA before billing
    for _pl in preview_lines:
        if _pl["is_cl"] and (_pl["batch_no"].strip() or _pl["expiry"].strip()):
            _w("""
                UPDATE purchase_acknowledgements
                SET batch_no    = COALESCE(NULLIF(%(bn)s,''), batch_no),
                    expiry_date = COALESCE(NULLIF(%(exp)s,'')::date, expiry_date)
                WHERE order_line_id = %(lid)s::uuid
            """, {
                "bn":  _pl["batch_no"].strip() or None,
                "exp": (_pl["expiry"].strip() + "-01")[:10] if len(_pl["expiry"].strip()) == 7
                       else (_pl["expiry"].strip()[:10] or None),
                "lid": _pl["line_id"],
            })

    # 2. Write back to order_lines ONLY if user explicitly changed a value
    # (never overwrite original discount set by backoffice at order creation)
    for _pl in preview_lines:
        _orig_price = float(_pl.get("_orig_unit_price") or _pl["unit_price"])
        _orig_disc  = float(_pl.get("_orig_discount")   or 0)
        _price_changed = abs(_pl["unit_price"] - _orig_price) > 0.005
        _disc_changed  = abs(_pl["discount"]   - _orig_disc)  > 0.005
        if _price_changed or _disc_changed:
            _w("""
                UPDATE order_lines
                SET unit_price      = %(up)s,
                    discount_amount = %(disc)s,
                    billing_total   = %(bt)s
                WHERE id = %(lid)s::uuid
            """, {
                "up":   _pl["unit_price"],
                "disc": _pl["discount"],
                "bt":   _pl["taxable"],
                "lid":  _pl["line_id"],
            })

    # 3. Create challan or invoice
    try:
        from modules.billing.challan_invoice_manager import (
            create_challan, create_invoice, get_party_billing_preference
        )
        _doc_no = ""
        if doc_type == "Challan":
            _doc_no = create_challan(
                party_id     = party_key if not party_key.startswith("name:") else "",
                order_ids    = order_ids,
                total_amount = grand_taxable,
                total_tax    = grand_gst,
                remarks      = remarks,
            )
        else:
            # Direct invoice — challan auto-created internally
            _doc_no = create_invoice(
                challan_id   = None,
                party_id     = party_key if not party_key.startswith("name:") else "",
                order_ids    = order_ids,
                total_amount = grand_taxable,
                total_tax    = grand_gst,
                remarks      = remarks,
                party_name   = None,
            )
    except Exception as _ce:
        st.error(f"❌ Billing failed: {_ce}")
        with st.expander("🔍 Full error"):
            st.code(_tb.format_exc())
        return

    if _doc_no:
        st.success(
            f"✅ {'Challan' if doc_type == 'Challan' else 'Invoice'} created: "
            f"**{_doc_no}** · ₹{grand_total:,.2f}"
        )

        # ── Check if challan values differ from backoffice order_lines ───────
        # If user changed price/discount in preview, the challan has different
        # values than what backoffice shows. Ask once: sync backoffice to challan?
        _sync_needed = []
        for _pl in preview_lines:
            _orig_up   = float(_pl.get("_orig_unit_price") or 0)
            _orig_disc = float(_pl.get("_orig_discount")   or 0)
            _new_up    = float(_pl["unit_price"])
            _new_disc  = float(_pl["discount"])
            if abs(_new_up - _orig_up) > 0.005 or abs(_new_disc - _orig_disc) > 0.005:
                _sync_needed.append({
                    "line_id":       _pl["line_id"],
                    "product_name":  _pl.get("product_name",""),
                    "eye_side":      _pl.get("eye_side",""),
                    "bo_price":      _orig_up,
                    "ch_price":      _new_up,
                    "bo_disc":       _orig_disc,
                    "ch_disc":       _new_disc,
                    "taxable":       _pl["taxable"],
                    "gst_amt":       _pl["gst_amt"],
                })

        if _sync_needed:
            st.warning(
                f"⚠️ **{len(_sync_needed)} line(s)** on this {doc_type.lower()} "
                f"have a different price/discount than what's in Backoffice."
            )
            for _sn in _sync_needed:
                _eye = str(_sn["eye_side"] or "").upper()
                _pn  = str(_sn["product_name"])
                _lbl = f"{_eye + ' — ' if _eye else ''}{_pn}"
                st.markdown(
                    f"<div style='background:#1a1200;border:1px solid #854d0e;"
                    f"border-radius:5px;padding:6px 12px;margin:3px 0;"
                    f"font-size:0.78rem'>"
                    f"<b style='color:#fcd34d'>{_lbl}</b>"
                    f"<span style='color:#94a3b8;margin-left:8px'>"
                    f"Backoffice: ₹{_sn['bo_price']:,.2f}"
                    + (f" · Disc ₹{_sn['bo_disc']:,.2f}" if _sn["bo_disc"] > 0 else "")
                    + f" &nbsp;→&nbsp; "
                    f"<b style='color:#fbbf24'>{doc_type}: ₹{_sn['ch_price']:,.2f}"
                    + (f" · Disc ₹{_sn['ch_disc']:,.2f}" if _sn["ch_disc"] > 0 else "")
                    + f"</b></span></div>",
                    unsafe_allow_html=True,
                )

            _sy1, _sy2 = st.columns(2)
            if _sy1.button(
                f"✅ Yes — Update Backoffice to match {doc_type}",
                key="bh_sync_yes", type="primary", use_container_width=True,
            ):
                _synced = 0
                for _sn in _sync_needed:
                    _ok = _w("""
                        UPDATE order_lines
                        SET unit_price      = %(up)s,
                            discount_amount = %(disc)s,
                            total_price     = %(tp)s,
                            billing_total   = %(tp)s,
                            gst_amount      = %(ga)s
                        WHERE id = %(lid)s::uuid
                    """, {
                        "up":   _sn["ch_price"],
                        "disc": _sn["ch_disc"],
                        "tp":   _sn["taxable"],
                        "ga":   _sn["gst_amt"],
                        "lid":  _sn["line_id"],
                    })
                    if _ok:
                        _synced += 1
                st.success(
                    f"✅ Backoffice updated for {_synced} line(s). "
                    f"Order now matches {doc_type} {_doc_no}."
                )
                import time; time.sleep(0.3)
                for _k in list(st.session_state.keys()):
                    if _k.startswith("bh_"):
                        del st.session_state[_k]
                st.rerun()

            if _sy2.button(
                "Skip — Keep Backoffice as is",
                key="bh_sync_no", use_container_width=True,
            ):
                for _k in list(st.session_state.keys()):
                    if _k.startswith("bh_"):
                        del st.session_state[_k]
                st.rerun()

            return  # Wait for user choice before clearing

        # No mismatch — clean exit
        st.balloons()
        import time; time.sleep(0.5)
        for _k in list(st.session_state.keys()):
            if _k.startswith("bh_"):
                del st.session_state[_k]
        st.rerun()
    else:
        st.error("❌ Document creation returned no document number — check DB logs.")
