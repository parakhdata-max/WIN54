"""
modules/procurement/direct_purchase_ui.py
==========================================
Direct Purchase Entry — purchase invoice without a linked sales order.

Use cases:
  • Buy stock speculatively (frames, CL, accessories) before a customer order
  • Receive a supplier invoice for items already in stock
  • Record any supplier payment that doesn't map to an order line

Creates:
  • procurement_orders + procurement_order_items  (ledger)
  • procurement_receipts                          (receipt record)
  • journal_entries / journal_lines               (accounting JV: Dr Purchase / Cr Creditor)

Entry point: render_direct_purchase_ui()
"""
from __future__ import annotations
import datetime as _dt
import decimal as _dec
import json
import streamlit as st


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


def _r2(v):
    try:
        return float(_dec.Decimal(str(v)).quantize(_dec.Decimal("0.01"), rounding=_dec.ROUND_HALF_UP))
    except Exception:
        return round(float(v or 0), 2)


def render_direct_purchase_ui():
    """
    Standalone procurement screen — no sales order required.
    Staff enter a supplier invoice line by line and save.
    """

    st.markdown(
        "<div style='background:#0a1628;border:1px solid #1e3a5f;"
        "border-left:4px solid #f59e0b;border-radius:8px;"
        "padding:10px 16px;margin-bottom:14px'>"
        "<span style='color:#fbbf24;font-size:1rem;font-weight:800'>"
        "🧾 Direct Purchase Entry</span>"
        "<span style='color:#475569;font-size:0.78rem;margin-left:10px'>"
        "Record a supplier invoice without a sales order — "
        "stock purchases, speculative buys, accessories.</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Supplier & document header ────────────────────────────────────────
    _sups = _q(
        "SELECT id::text AS id, party_name, COALESCE(mobile,'') AS mobile "
        "FROM parties WHERE UPPER(COALESCE(party_type,'')) IN "
        "('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB','CONTACT_LENS_SUPPLIER') "
        "AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name"
    )
    _sup_ids   = [""] + [s["id"] for s in _sups]
    _sup_names = {"": "— Select Supplier —"}
    _sup_names.update({s["id"]: s["party_name"] for s in _sups})

    with st.container(border=True):
        st.markdown(
            "<div style='color:#a5b4fc;font-size:0.8rem;font-weight:700;"
            "margin-bottom:8px'>📄 Invoice Header</div>",
            unsafe_allow_html=True,
        )
        _h1, _h2, _h3, _h4 = st.columns([3, 2, 2, 2])
        _sup_id   = _h1.selectbox("Supplier", _sup_ids,
                                   format_func=lambda x: _sup_names.get(x, x),
                                   key="dp_sup", label_visibility="collapsed")
        _doc_type = _h2.selectbox("Type", ["INVOICE","CHALLAN","BOTH"],
                                   key="dp_dtype", label_visibility="collapsed")
        _doc_no   = _h3.text_input("Invoice / Challan No", key="dp_docno",
                                    placeholder="Ref no", label_visibility="collapsed")
        _doc_date = _h4.date_input("Date", value=_dt.date.today(),
                                    key="dp_docdate", format="DD/MM/YYYY",
                                    label_visibility="collapsed")

    _sup_name = _sup_names.get(_sup_id, "")

    # ── Line items ────────────────────────────────────────────────────────
    # Session state: list of line dicts
    if "dp_lines" not in st.session_state:
        st.session_state["dp_lines"] = [_empty_line()]

    st.markdown(
        "<div style='color:#a5b4fc;font-size:0.8rem;font-weight:700;"
        "margin:10px 0 6px 0'>🗒️ Line Items</div>",
        unsafe_allow_html=True,
    )

    # Header row
    _lh = st.columns([3, 1, 1.2, 1.2, 1.2, 0.5])
    for _col, _lbl in zip(_lh, ["Product / Description","Qty","Unit Price ₹","GST %","Total ₹",""]):
        _col.markdown(
            f"<div style='font-size:0.67rem;font-weight:700;color:#475569;"
            f"text-transform:uppercase;border-bottom:1px solid #1e3a5f;"
            f"padding-bottom:3px'>{_lbl}</div>",
            unsafe_allow_html=True,
        )

    _lines = st.session_state["dp_lines"]
    _grand_taxable = _grand_gst = _grand_total = 0.0

    for _i, _ln in enumerate(_lines):
        _c1, _c2, _c3, _c4, _c5, _c6 = st.columns([3, 1, 1.2, 1.2, 1.2, 0.5])
        with _c1:
            _ln["product_name"] = st.text_input(
                "Product", value=_ln.get("product_name",""),
                key=f"dp_pname_{_i}", label_visibility="collapsed",
                placeholder="Product / description",
            )
            _cat_opts = ["LENSES","FRAMES","CONTACT LENS","ACCESSORIES","OTHER"]
            _ln["category"] = st.selectbox(
                "Category", _cat_opts,
                index=_cat_opts.index(_ln.get("category","LENSES")) if _ln.get("category","LENSES") in _cat_opts else 0,
                key=f"dp_cat_{_i}", label_visibility="collapsed",
            )
        with _c2:
            _ln["qty"] = int(st.number_input(
                "Qty", value=float(_ln.get("qty",1)), min_value=0.0, step=1.0,
                key=f"dp_qty_{_i}", label_visibility="collapsed", format="%.0f",
            ))
        with _c3:
            _ln["price"] = _r2(st.number_input(
                "Price", value=_ln.get("price",0.0), min_value=0.0, step=0.01,
                key=f"dp_price_{_i}", label_visibility="collapsed", format="%.2f",
            ))
        with _c4:
            _ln["gst_pct"] = float(st.selectbox(
                "GST", [0,5,12,18,28],
                index=[0,5,12,18,28].index(int(_ln.get("gst_pct",5))) if int(_ln.get("gst_pct",5)) in [0,5,12,18,28] else 1,
                key=f"dp_gst_{_i}", label_visibility="collapsed",
            ))

        _taxable  = _r2(_ln["price"] * _ln["qty"])
        _gst_amt  = _r2(_taxable * _ln["gst_pct"] / 100)
        _line_tot = _r2(_taxable + _gst_amt)
        _grand_taxable = _r2(_grand_taxable + _taxable)
        _grand_gst     = _r2(_grand_gst + _gst_amt)
        _grand_total   = _r2(_grand_total + _line_tot)

        with _c5:
            st.markdown(
                f"<div style='font-size:0.85rem;color:#10b981;font-weight:700;"
                f"padding-top:28px'>₹{_line_tot:,.2f}</div>",
                unsafe_allow_html=True,
            )
        with _c6:
            if st.button("✕", key=f"dp_del_{_i}", use_container_width=True):
                st.session_state["dp_lines"].pop(_i)
                st.rerun()

    if st.button("＋ Add Line", key="dp_addline"):
        st.session_state["dp_lines"].append(_empty_line())
        st.rerun()

    if not _lines:
        st.info("Add at least one line item.")
        return

    # ── Totals ────────────────────────────────────────────────────────────
    st.markdown("<div style='border-top:2px solid #1e3a5f;margin:6px 0 4px 0'></div>",
                unsafe_allow_html=True)
    _t1, _t2, _t3, _t4 = st.columns([4, 1.5, 1.5, 1.5])
    _t1.markdown(f"<div style='font-size:0.8rem;font-weight:700;color:#a5b4fc;padding-top:4px'>"
                 f"{len(_lines)} line(s)</div>", unsafe_allow_html=True)
    _t2.markdown(f"<div style='font-size:0.8rem;color:#94a3b8;padding-top:4px'>"
                 f"Taxable ₹{_grand_taxable:,.2f}</div>", unsafe_allow_html=True)
    _t3.markdown(f"<div style='font-size:0.8rem;color:#f59e0b;padding-top:4px'>"
                 f"GST ₹{_grand_gst:,.2f}</div>", unsafe_allow_html=True)
    _t4.markdown(f"<div style='font-size:0.95rem;font-weight:800;color:#10b981;padding-top:4px'>"
                 f"Total ₹{_grand_total:,.2f}</div>", unsafe_allow_html=True)

    if _grand_gst > 0:
        _cgst = _r2(_grand_gst / 2)
        _sgst = _r2(_grand_gst - _cgst)
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:5px;"
            f"padding:6px 14px;margin:4px 0;font-size:0.75rem;color:#94a3b8'>"
            f"CGST: <b style='color:#f59e0b'>₹{_cgst:,.2f}</b>&nbsp;·&nbsp;"
            f"SGST: <b style='color:#f59e0b'>₹{_sgst:,.2f}</b>&nbsp;·&nbsp;"
            f"Total GST: <b style='color:#f59e0b'>₹{_grand_gst:,.2f}</b>&nbsp;·&nbsp;"
            f"Grand Total: <b style='color:#10b981'>₹{_grand_total:,.2f}</b>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Validation ────────────────────────────────────────────────────────
    _can_save = bool(_sup_id and _doc_no.strip() and _grand_total > 0)
    if not _sup_id:
        st.warning("Select a supplier.")
    elif not _doc_no.strip():
        st.warning("Enter invoice / challan number.")

    st.markdown("---")
    _sv1, _sv2 = st.columns([3, 1])
    _do_save = _sv1.button(
        f"💾 Save Direct Purchase ({len(_lines)} lines · ₹{_grand_total:,.2f})",
        type="primary", use_container_width=True, key="dp_save",
        disabled=not _can_save,
    )
    _sv2.button("🗑 Clear", key="dp_clear", use_container_width=True,
                on_click=lambda: st.session_state.update({"dp_lines": [_empty_line()]}))

    if _do_save and _can_save:
        try:
            from modules.core.date_guard import validate_not_future
            _ok_dt, _msg_dt = validate_not_future(_doc_date, "Direct purchase date")
        except Exception as _dg_e:
            _ok_dt, _msg_dt = False, f"Date validation failed: {_dg_e}"
        if not _ok_dt:
            st.error(_msg_dt)
            return
        _save_direct_purchase(
            sup_id       = _sup_id,
            sup_name     = _sup_name,
            doc_no       = _doc_no.strip(),
            doc_type     = _doc_type,
            doc_date     = _doc_date,
            lines        = _lines,
            grand_taxable= _grand_taxable,
            grand_gst    = _grand_gst,
            grand_total  = _grand_total,
        )


def _empty_line():
    return {"product_name":"","category":"LENSES","qty":1,"price":0.0,"gst_pct":5}


def _save_direct_purchase(
    sup_id, sup_name, doc_no, doc_type, doc_date,
    lines, grand_taxable, grand_gst, grand_total,
):
    """Save to procurement_orders + procurement_receipts + accounting JV."""
    from modules.core.date_guard import validate_not_future
    _ok_dt, _msg_dt = validate_not_future(doc_date, "Direct purchase date")
    if not _ok_dt:
        st.error(_msg_dt)
        return

    saved_ledger = False
    vno = ""

    # ── 1. Procurement ledger ─────────────────────────────────────────────
    try:
        from modules.procurement.procurement_ledger import record_scanned_invoice_items
        record_scanned_invoice_items(
            items=[
                {
                    "product_name": ln.get("product_name", ""),
                    "description": ln.get("product_name", ""),
                    "route": ln.get("category", "STOCK"),
                    "qty": ln["qty"],
                    "unit_price": ln["price"],
                    "gst_percent": ln.get("gst_pct", 0),
                    "batch_no": "",
                    "expiry_date": "",
                }
                for ln in lines
            ],
            supplier_id   = sup_id,
            supplier_name = sup_name,
            document_no   = doc_no,
            document_type = doc_type,
            document_date = str(doc_date),
            source        = "DIRECT_PURCHASE",
        )
        saved_ledger = True
    except Exception as _le:
        st.warning(f"Ledger save pending: {_le}")

    # ── 2. Accounting journal ─────────────────────────────────────────────
    cats = [ln.get("category","LENSES").upper() for ln in lines]
    dominant_cat = max(set(cats), key=cats.count) if cats else "LENSES"
    try:
        from modules.accounting.accounts_engine import post_purchase_invoice_jv
        _acc_ok, vno = post_purchase_invoice_jv(
            invoice_no        = doc_no,
            invoice_id        = "",
            supplier_name     = sup_name,
            grand_total       = grand_total,
            taxable           = grand_taxable,
            tax_amount        = grand_gst,
            purchase_category = dominant_cat,
            voucher_date      = doc_date,
            created_by        = st.session_state.get("user_name","Staff"),
        )
        if not _acc_ok:
            st.warning(f"Accounting post pending: {vno}")
            vno = ""
    except Exception as _ae:
        st.warning(f"Accounting wiring pending: {_ae}")
        vno = ""

    # ── 3. Result ─────────────────────────────────────────────────────────
    if saved_ledger or vno:
        st.success(
            f"✅ Direct Purchase saved — {sup_name} · Ref: {doc_no} · ₹{grand_total:,.2f}"
            + (f"  ·  Journal: {vno}" if vno else "")
        )
        st.session_state["dp_lines"] = [_empty_line()]
        import time; time.sleep(0.3)
        st.rerun()
    else:
        st.error("❌ Save failed — check supplier and line data.")
