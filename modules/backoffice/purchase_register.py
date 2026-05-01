"""
modules/backoffice/purchase_register.py
========================================
Purchase Register — single unified search across all purchase documents.

Sources:
  purchase_acknowledgements  → 📋 Challan / 🧾 Invoice  (order-linked)
  supplier_orders            → 📤 PO
  purchase_invoices          → 🏪 GRN  (stock replenishment)

Call from anywhere:  from modules.backoffice.purchase_register import render_purchase_register
"""

import streamlit as st
import datetime


# ── DB helpers ──────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}")
        return []


def _rw(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        st.error(f"Write: {e}")
        return False


def _fmt_pwr(row):
    parts = []
    try:
        if row.get("sph") is not None:
            parts.append(f"SPH {float(row['sph']):+.2f}")
        if row.get("cyl") and abs(float(row["cyl"])) > 0.01:
            parts.append(f"CYL {float(row['cyl']):+.2f}")
        if row.get("axis"):
            parts.append(f"AX {int(row['axis'])}")
        if row.get("add_power") and float(row.get("add_power") or 0) > 0:
            parts.append(f"ADD +{float(row['add_power']):.2f}")
    except Exception:
        pass
    return "  ".join(parts)


# ── Data loaders ────────────────────────────────────────────────────────────────

def _load_pa(sup, ref, prod, dfrom, dto):
    w = [
        "DATE(COALESCE(pa.document_date, pa.acknowledged_at::date)) >= %(df)s",
        "DATE(COALESCE(pa.document_date, pa.acknowledged_at::date)) <= %(dt)s",
    ]
    p = {"df": str(dfrom), "dt": str(dto)}
    if sup.strip():
        w.append("LOWER(COALESCE(pa.supplier_name,'')) LIKE %(sup)s")
        p["sup"] = f"%{sup.strip().lower()}%"
    if ref.strip():
        w.append("(LOWER(COALESCE(pa.order_no,'')) LIKE %(ref)s"
                 " OR LOWER(COALESCE(pa.challan_no,'')) LIKE %(ref)s"
                 " OR LOWER(COALESCE(pa.invoice_no,'')) LIKE %(ref)s)")
        p["ref"] = f"%{ref.strip().lower()}%"
    if prod.strip():
        w.append("(LOWER(COALESCE(p.product_name,'')) LIKE %(prod)s"
                 " OR LOWER(COALESCE(pa.product_name,'')) LIKE %(prod)s)")
        p["prod"] = f"%{prod.strip().lower()}%"

    return _q(f"""
        SELECT
            pa.id::text                              AS pa_id,
            pa.order_no,
            pa.challan_no,
            pa.invoice_no,
            pa.supplier_name,
            pa.supplier_id::text                     AS supplier_id,
            COALESCE(o.patient_name,o.party_name,'—') AS patient_name,
            COALESCE(p.product_name, pa.product_name,'—') AS product_name,
            COALESCE(p.unit,'PCS')                   AS unit,
            COALESCE(p.box_size,1)                   AS box_size,
            pa.eye_side,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            pa.qty,
            COALESCE(pa.purchase_price,0)            AS purchase_price,
            COALESCE(pa.total_value,0)               AS total_value,
            pa.document_date::text                   AS doc_date,
            pa.is_price_locked,
            COALESCE(pa.transport,'')                AS transport,
            COALESCE(pa.lr_no,'')                    AS lr_no
        FROM purchase_acknowledgements pa
        LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
        LEFT JOIN orders o       ON o.id  = ol.order_id
        LEFT JOIN products p     ON p.id  = ol.product_id
        WHERE {" AND ".join(w)}
        ORDER BY pa.document_date DESC NULLS LAST, pa.acknowledged_at DESC
        LIMIT 400
    """, p)


def _load_pos(sup, ref, dfrom, dto):
    w = ["DATE(so.created_at) >= %(df)s", "DATE(so.created_at) <= %(dt)s"]
    p = {"df": str(dfrom), "dt": str(dto)}
    if sup.strip():
        w.append("LOWER(COALESCE(so.supplier_name,'')) LIKE %(sup)s")
        p["sup"] = f"%{sup.strip().lower()}%"
    if ref.strip():
        w.append("(LOWER(COALESCE(so.supplier_order_id,'')) LIKE %(ref)s"
                 " OR LOWER(COALESCE(so.customer_order_id,'')) LIKE %(ref)s)")
        p["ref"] = f"%{ref.strip().lower()}%"

    rows = _q(f"""
        SELECT
            so.id                AS po_id,
            COALESCE(so.supplier_order_id, 'PO-' || so.id::text) AS po_no,
            so.supplier_name,
            so.supplier_id       AS supplier_id,
            so.customer_order_id AS order_ref,
            so.order_date::text      AS doc_date,
            so.expected_delivery_date::text AS exp_date,
            so.status,
            COALESCE(so.total_value,0)   AS total_value,
            COALESCE(so.total_items,0)   AS total_items,
            COALESCE(so.total_qty,0)     AS total_qty,
            COALESCE(so.special_instructions,'') AS notes,
            so.created_at::text      AS created_at
        FROM supplier_orders so
        WHERE {" AND ".join(w)}
        ORDER BY so.created_at DESC
        LIMIT 200
    """, p)

    # Attach items to each PO
    for r in rows:
        r["_items"] = _q("""
            SELECT product_name, eye_side, sph, cyl, axis, add_power,
                   COALESCE(ordered_qty,0) AS ordered_qty,
                   COALESCE(received_qty,0) AS received_qty,
                   COALESCE(unit_price,0) AS unit_price,
                   item_status
            FROM supplier_order_items
            WHERE CAST(supplier_order_id AS TEXT) = %(id_str)s
            ORDER BY item_no
        """, {"id_str": str(r["po_id"])})
    return rows


def _load_grns(sup, ref, dfrom, dto):
    # Check if table exists first
    exists = _q("SELECT 1 FROM information_schema.tables WHERE table_name='purchase_invoices' LIMIT 1")
    if not exists:
        return []
    w = ["DATE(pi.invoice_date) >= %(df)s", "DATE(pi.invoice_date) <= %(dt)s"]
    p = {"df": str(dfrom), "dt": str(dto)}
    if sup.strip():
        w.append("LOWER(COALESCE(pi.supplier_name,'')) LIKE %(sup)s")
        p["sup"] = f"%{sup.strip().lower()}%"
    if ref.strip():
        w.append("(LOWER(COALESCE(pi.invoice_no,'')) LIKE %(ref)s"
                 " OR LOWER(COALESCE(pi.supplier_invoice_no,'')) LIKE %(ref)s)")
        p["ref"] = f"%{ref.strip().lower()}%"
    return _q(f"""
        SELECT
            pi.invoice_no,
            pi.supplier_name,
            pi.supplier_id::text          AS supplier_id,
            COALESCE(pi.supplier_order_id,'') AS po_ref,
            COALESCE(pi.supplier_invoice_no,'') AS supplier_inv,
            pi.invoice_date::text         AS doc_date,
            COALESCE(pi.total_items,0)    AS total_items,
            COALESCE(pi.total_qty_received,0) AS qty,
            COALESCE(pi.subtotal,0)       AS subtotal,
            COALESCE(pi.gst_amount,0)     AS gst_amount,
            COALESCE(pi.invoice_total,0)  AS total_value,
            COALESCE(pi.payment_status,'UNPAID') AS payment_status
        FROM purchase_invoices pi
        WHERE {" AND ".join(w)}
        ORDER BY pi.invoice_date DESC
        LIMIT 200
    """, p)


# ── PA editable card ────────────────────────────────────────────────────────────

def _render_pa_card(r):
    _pid    = r["pa_id"]
    _kb     = f"pa_{str(_pid).replace('-','')[-10:]}"
    _locked = bool(r.get("is_price_locked"))
    _price  = float(r.get("purchase_price") or 0)
    _qty    = int(r.get("qty") or 1)
    _pname  = (r.get("product_name") or "—")[:35]
    _eye    = str(r.get("eye_side","")).upper()
    _pw     = _fmt_pwr(r)
    _ono    = r.get("order_no","—")
    _chal   = r.get("challan_no","") or ""
    _inv    = r.get("invoice_no","") or ""
    _sup    = r.get("supplier_name","—")
    _trans  = r.get("transport","") or ""
    _lr     = r.get("lr_no","") or ""
    _unit   = str(r.get("unit","PCS")).upper()
    _bsize  = int(r.get("box_size") or 1)
    _total  = float(r.get("total_value") or 0)

    # Qty label
    if _unit == "BOX" and _bsize > 1:
        _nb = _qty // _bsize
        _np = _qty % _bsize
        _qty_lbl = f"{_nb} Box ({_qty} pcs)" + (f" +{_np}" if _np else "")
    else:
        _qty_lbl = f"{_qty} pcs"

    # Status
    if _inv:
        _sc, _st = "#22c55e", f"🧾 {_inv}"
    elif _chal:
        _sc, _st = "#3b82f6", f"📋 {_chal}"
    else:
        _sc, _st = "#ef4444", "⚠️ No doc"

    # Header
    st.markdown(
        f"<div style='background:#080f1a;border:1px solid #1e293b;"
        f"border-left:4px solid {_sc};border-radius:6px;padding:6px 12px;margin:2px 0'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span style='color:#f1f5f9;font-weight:700'>{_ono} &nbsp; {_eye} &nbsp; {_pname}</span>"
        f"<span style='color:{_sc};font-size:0.72rem;font-weight:700'>{_st}</span>"
        f"</div>"
        f"<div style='color:#475569;font-size:0.7rem;margin-top:2px'>"
        f"{_pw}  ·  {_qty_lbl}  ·  &#8377;{_price:,.2f}/pc  ·  Total &#8377;{_total:,.0f}"
        + (f"  ·  {_sup}" if _sup != "—" else "")
        + (f"  ·  🚚 {_trans}" if _trans else "")
        + (f"  ·  LR: {_lr}" if _lr else "")
        + "</div></div>",
        unsafe_allow_html=True
    )

    with st.expander("✏️ Edit", expanded=False):
        # Initialise session state defaults ONCE (avoids value= conflict)
        if f"pr_p_{_kb}"  not in st.session_state: st.session_state[f"pr_p_{_kb}"]  = _price
        if f"pr_c_{_kb}"  not in st.session_state: st.session_state[f"pr_c_{_kb}"]  = _chal
        if f"pr_i_{_kb}"  not in st.session_state: st.session_state[f"pr_i_{_kb}"]  = _inv
        if f"pr_t_{_kb}"  not in st.session_state: st.session_state[f"pr_t_{_kb}"]  = _trans
        if f"pr_lr_{_kb}" not in st.session_state: st.session_state[f"pr_lr_{_kb}"] = _lr
        _doc_dt = r.get("doc_date","") or ""
        if f"pr_d_{_kb}" not in st.session_state:
            try:
                st.session_state[f"pr_d_{_kb}"] = datetime.date.fromisoformat(_doc_dt[:10]) if _doc_dt else datetime.date.today()
            except Exception:
                st.session_state[f"pr_d_{_kb}"] = datetime.date.today()

        e1, e2, e3, e4 = st.columns(4)
        with e1:
            st.number_input("Purchase Price ₹/pc", min_value=0.0,
                            step=0.5, format="%.2f",
                            key=f"pr_p_{_kb}", disabled=_locked)
            if _locked: st.caption("🔒 Price locked")
        with e2:
            st.text_input("Challan No.", key=f"pr_c_{_kb}", placeholder="CH-001")
            st.text_input("Invoice No.", key=f"pr_i_{_kb}", placeholder="INV/001")
        with e3:
            st.date_input("Document Date", key=f"pr_d_{_kb}", format="DD/MM/YYYY")
            st.text_input("Transport", key=f"pr_t_{_kb}", placeholder="DTDC")
        with e4:
            st.text_input("LR / AWB", key=f"pr_lr_{_kb}", placeholder="LR-12345")
            st.markdown("&nbsp;")
            if st.button("💾 Save Changes", key=f"pr_sv_{_kb}",
                         type="primary", use_container_width=True):
                _new_p  = st.session_state.get(f"pr_p_{_kb}", _price)
                _new_c  = (st.session_state.get(f"pr_c_{_kb}","") or "").strip() or None
                _new_i  = (st.session_state.get(f"pr_i_{_kb}","") or "").strip() or None
                _new_d  = st.session_state.get(f"pr_d_{_kb}", datetime.date.today())
                _new_t  = (st.session_state.get(f"pr_t_{_kb}","") or "").strip() or None
                _new_lr = (st.session_state.get(f"pr_lr_{_kb}","") or "").strip() or None
                _ok = _rw("""
                    UPDATE purchase_acknowledgements SET
                        purchase_price  = CASE WHEN %(lk)s THEN purchase_price ELSE %(p)s END,
                        total_value     = CASE WHEN %(lk)s THEN total_value    ELSE %(tv)s END,
                        challan_no      = %(cn)s,
                        invoice_no      = %(iv)s,
                        document_date   = %(dd)s::date,
                        transport       = %(tr)s,
                        lr_no           = %(lr)s,
                        acknowledged_at = NOW()
                    WHERE id = %(id)s::uuid
                """, {
                    "lk": _locked, "p": _new_p,
                    "tv": round(_new_p * _qty, 2),
                    "cn": _new_c, "iv": _new_i,
                    "dd": str(_new_d),
                    "tr": _new_t, "lr": _new_lr,
                    "id": _pid,
                })
                if _ok:
                    # Clear cached defaults so they reload fresh
                    for _k in [f"pr_p_{_kb}", f"pr_c_{_kb}", f"pr_i_{_kb}",
                                f"pr_d_{_kb}", f"pr_t_{_kb}", f"pr_lr_{_kb}"]:
                        st.session_state.pop(_k, None)
                    st.success("✓ Saved")
                    st.rerun()


# ── PO card ─────────────────────────────────────────────────────────────────────

def _render_po_card(r):
    _po_id  = r["po_id"]
    _po_no  = r.get("po_no","—")
    _sup    = r.get("supplier_name","—")
    _status = str(r.get("status","DRAFT")).upper()
    _total  = float(r.get("total_value") or 0)
    _date   = str(r.get("doc_date",""))[:10]
    _items  = r.get("_items",[])
    _exp    = str(r.get("exp_date","") or "")[:10]
    _notes  = r.get("notes","") or ""
    _kb     = f"po_{str(_po_id).replace('-','')[-10:]}"

    _st_color = {
        "DRAFT":"#64748b","SENT":"#3b82f6","CONFIRMED":"#8b5cf6",
        "RECEIVED":"#22c55e","PARTIAL":"#f59e0b","CANCELLED":"#ef4444"
    }.get(_status,"#475569")

    st.markdown(
        f"<div style='background:#080f1a;border:1px solid #1e293b;"
        f"border-left:4px solid {_st_color};border-radius:6px;padding:6px 12px;margin:2px 0'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span style='color:#f1f5f9;font-weight:700;font-family:monospace'>{_po_no}</span>"
        f"<span style='background:{_st_color}22;color:{_st_color};"
        f"font-size:0.68rem;font-weight:700;padding:2px 8px;border-radius:6px'>{_status}</span>"
        f"</div>"
        f"<div style='color:#475569;font-size:0.7rem;margin-top:2px'>"
        f"{_sup}  ·  {len(_items)} item(s)  ·  &#8377;{_total:,.0f}  ·  {_date}"
        + (f"  ·  Expected: {_exp}" if _exp else "")
        + "</div></div>",
        unsafe_allow_html=True
    )

    with st.expander("📋 Items / Actions", expanded=False):
        # Items
        if _items:
            for it in _items:
                _pw = _fmt_pwr(it)
                st.markdown(
                    f"<div style='padding:3px 8px;border-left:2px solid #1e293b;"
                    f"margin:2px 0;font-size:0.78rem;color:#94a3b8'>"
                    f"<b style='color:#e2e8f0'>{str(it.get('eye_side','')).upper()}</b>"
                    f" · {it.get('product_name','')} {_pw}"
                    f" · Ordered: {it.get('ordered_qty',0)}"
                    f" · Received: {it.get('received_qty',0)}"
                    f" · &#8377;{float(it.get('unit_price',0)):,.2f}"
                    f" · {it.get('item_status','PENDING')}"
                    f"</div>",
                    unsafe_allow_html=True
                )
        else:
            st.caption("No line items found.")

        if _notes:
            st.caption(f"Notes: {_notes}")
        if _exp:
            st.caption(f"Expected: {_exp}")

        # Status actions
        if _status not in ("RECEIVED","CANCELLED"):
            ac1, ac2, ac3 = st.columns(3)
            if _status == "DRAFT":
                if ac1.button("📤 Mark Sent", key=f"pr_send_{_kb}",
                              type="primary", use_container_width=True):
                    if _rw("UPDATE supplier_orders SET status='SENT' WHERE id=%(id)s",
                           {"id": _po_id}):
                        st.success("Marked Sent"); st.rerun()
            if _status in ("SENT","CONFIRMED"):
                if ac2.button("✅ Mark Received", key=f"pr_recv_{_kb}",
                              type="primary", use_container_width=True):
                    if _rw("UPDATE supplier_orders SET status='RECEIVED' WHERE id=%(id)s",
                           {"id": _po_id}):
                        st.success("Marked Received"); st.rerun()
            if ac3.button("❌ Cancel", key=f"pr_cancel_{_kb}", use_container_width=True):
                if _rw("UPDATE supplier_orders SET status='CANCELLED' WHERE id=%(id)s",
                       {"id": _po_id}):
                    st.warning("Cancelled"); st.rerun()

        # WhatsApp
        if _items and _status in ("DRAFT","SENT"):
            _mob_rows = _q("""SELECT COALESCE(whatsapp,mobile,'') AS mob
                              FROM parties WHERE id=%(sid)s::uuid LIMIT 1""",
                           {"sid": r.get("supplier_id","")})
            _mob = (_mob_rows[0]["mob"] if _mob_rows else "").replace(" ","")
            _wa_d = "".join(c for c in _mob if c.isdigit())
            if _wa_d.startswith("91") and len(_wa_d)==12: _wa_d = _wa_d[2:]
            _wa_num = f"91{_wa_d}" if len(_wa_d)==10 else ""
            if _wa_num:
                import urllib.parse as _up
                _msg = "\n".join(
                    [f"*PO: {_po_no}*", f"Date: {_date}", f"Supplier: {_sup}", ""]
                    + [f"• {it.get('product_name','')} ({str(it.get('eye_side','')).upper()}) "
                       f"{_fmt_pwr(it)} — Qty {it.get('ordered_qty',0)}" for it in _items]
                    + (["", f"Note: {_notes}"] if _notes else [])
                    + ["", "Please confirm receipt. 🙏"]
                )
                st.link_button("📲 Send via WhatsApp",
                               f"https://wa.me/{_wa_num}?text={_up.quote(_msg)}",
                               use_container_width=True)


# ── GRN card ────────────────────────────────────────────────────────────────────

def _render_grn_card(r):
    _ino    = r.get("invoice_no","—")
    _sup    = r.get("supplier_name","—")
    _date   = str(r.get("doc_date",""))[:10]
    _total  = float(r.get("total_value") or 0)
    _sub    = float(r.get("subtotal") or 0)
    _gst    = float(r.get("gst_amount") or 0)
    _pstat  = str(r.get("payment_status","UNPAID")).upper()
    _sinv   = r.get("supplier_inv","") or ""
    _poref  = r.get("po_ref","") or ""
    _sc     = "#22c55e" if _pstat == "PAID" else "#f59e0b"
    _kb     = f"grn_{str(_ino).replace('/','_')[-10:]}"

    st.markdown(
        f"<div style='background:#080f1a;border:1px solid #1e293b;"
        f"border-left:4px solid #06b6d4;border-radius:6px;padding:6px 12px;margin:2px 0'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span style='color:#f1f5f9;font-weight:700;font-family:monospace'>{_ino}</span>"
        f"<span style='background:{_sc}22;color:{_sc};"
        f"font-size:0.68rem;font-weight:700;padding:2px 8px;border-radius:6px'>{_pstat}</span>"
        f"</div>"
        f"<div style='color:#475569;font-size:0.7rem;margin-top:2px'>"
        f"{_sup}  ·  {r.get('total_items',0)} item(s)  ·  &#8377;{_total:,.0f}  ·  {_date}"
        + (f"  ·  PO: {_poref}" if _poref else "")
        + "</div></div>",
        unsafe_allow_html=True
    )

    with st.expander("📋 Detail", expanded=False):
        m1, m2, m3 = st.columns(3)
        m1.metric("Subtotal", f"₹{_sub:,.2f}")
        m2.metric("GST",      f"₹{_gst:,.2f}")
        m3.metric("Total",    f"₹{_total:,.2f}")
        if _sinv:
            st.caption(f"Supplier invoice: {_sinv}")
        if _pstat == "UNPAID":
            if st.button("✅ Mark as Paid", key=f"pr_pay_{_kb}",
                         type="primary", use_container_width=True):
                if _rw("UPDATE purchase_invoices SET payment_status='PAID' WHERE invoice_no=%(n)s",
                       {"n": _ino}):
                    st.success("Marked as Paid"); st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def render_purchase_register():
    st.markdown("### 🔍 Purchase Register")
    st.caption("All purchase documents — challans, invoices, POs, GRNs — in one place. "
               "Search, view detail, edit price, update status.")

    # ── Filters ───────────────────────────────────────────────────────────────
    with st.container(border=True):
        f1, f2, f3 = st.columns([3, 3, 3])
        _sup  = f1.text_input("Supplier",   placeholder="🔍 Supplier",
                               key="pr_f_sup",  label_visibility="collapsed")
        _ref  = f2.text_input("Doc / Order", placeholder="🔍 Invoice / Challan / PO / Order no",
                               key="pr_f_ref",  label_visibility="collapsed")
        _prod = f3.text_input("Product",    placeholder="🔍 Product name",
                               key="pr_f_prod", label_visibility="collapsed")

        f4, f5, f6, f7 = st.columns([2, 2, 3, 1])
        _dfrom = f4.date_input("From",
                                value=datetime.date.today() - datetime.timedelta(days=90),
                                key="pr_f_from", label_visibility="collapsed",
                                format="DD/MM/YYYY")
        _dto   = f5.date_input("To",
                                value=datetime.date.today(),
                                key="pr_f_to", label_visibility="collapsed",
                                format="DD/MM/YYYY")
        _dtype = f6.selectbox("Type",
                               ["ALL","CHALLAN","INVOICE","PO","GRN"],
                               format_func=lambda x: {
                                   "ALL":     "All document types",
                                   "CHALLAN": "📋 Challans only",
                                   "INVOICE": "🧾 Invoices only",
                                   "PO":      "📤 POs only",
                                   "GRN":     "🏪 GRNs only",
                               }.get(x, x),
                               key="pr_f_type", label_visibility="collapsed")
        if f7.button("🔄", key="pr_refresh", use_container_width=True):
            st.rerun()

    # ── Load ──────────────────────────────────────────────────────────────────
    pa_rows  = _load_pa(_sup, _ref, _prod, _dfrom, _dto) if _dtype in ("ALL","CHALLAN","INVOICE") else []
    po_rows  = _load_pos(_sup, _ref, _dfrom, _dto)       if _dtype in ("ALL","PO")               else []
    grn_rows = _load_grns(_sup, _ref, _dfrom, _dto)      if _dtype in ("ALL","GRN")              else []

    # Filter PA by type
    if _dtype == "CHALLAN":
        pa_rows = [r for r in pa_rows if r.get("challan_no") and not r.get("invoice_no")]
    elif _dtype == "INVOICE":
        pa_rows = [r for r in pa_rows if r.get("invoice_no")]

    if not pa_rows and not po_rows and not grn_rows:
        st.info("No records found. Adjust filters.")
        return

    # ── Metrics ───────────────────────────────────────────────────────────────
    _total_val = (sum(float(r.get("total_value",0)) for r in pa_rows)
                + sum(float(r.get("total_value",0)) for r in po_rows)
                + sum(float(r.get("total_value",0)) for r in grn_rows))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("PA Lines",  len(pa_rows))
    m2.metric("POs",       len(po_rows))
    m3.metric("GRNs",      len(grn_rows))
    m4.metric("Total Value", f"₹{_total_val:,.0f}")
    st.markdown("---")

    # ── PA section — grouped by supplier ─────────────────────────────────────
    if pa_rows:
        st.markdown("#### 📋 Challans & 🧾 Invoices")
        from collections import OrderedDict as _od
        _by_sup = _od()
        for r in pa_rows:
            s = r.get("supplier_name","—")
            if s not in _by_sup: _by_sup[s] = []
            _by_sup[s].append(r)

        for sn, srows in _by_sup.items():
            _sv = sum(float(r.get("total_value",0)) for r in srows)
            with st.expander(
                f"🏭 {sn}  ·  {len(srows)} line(s)  ·  ₹{_sv:,.0f}",
                expanded=len(_by_sup) <= 2
            ):
                for r in srows:
                    _render_pa_card(r)
        st.markdown("---")

    # ── PO section ────────────────────────────────────────────────────────────
    if po_rows:
        st.markdown("#### 📤 Purchase Orders")
        from collections import OrderedDict as _od2
        _by_sup2 = _od2()
        for r in po_rows:
            s = r.get("supplier_name","—")
            if s not in _by_sup2: _by_sup2[s] = []
            _by_sup2[s].append(r)

        for sn, srows in _by_sup2.items():
            _sv = sum(float(r.get("total_value",0)) for r in srows)
            with st.expander(
                f"🏭 {sn}  ·  {len(srows)} PO(s)  ·  ₹{_sv:,.0f}",
                expanded=len(_by_sup2) <= 2
            ):
                for r in srows:
                    _render_po_card(r)
        st.markdown("---")

    # ── GRN section ───────────────────────────────────────────────────────────
    if grn_rows:
        st.markdown("#### 🏪 GRNs (Stock Receipts)")
        from collections import OrderedDict as _od3
        _by_sup3 = _od3()
        for r in grn_rows:
            s = r.get("supplier_name","—")
            if s not in _by_sup3: _by_sup3[s] = []
            _by_sup3[s].append(r)

        for sn, srows in _by_sup3.items():
            _sv = sum(float(r.get("total_value",0)) for r in srows)
            with st.expander(
                f"🏭 {sn}  ·  {len(srows)} GRN(s)  ·  ₹{_sv:,.0f}",
                expanded=len(_by_sup3) <= 2
            ):
                for r in srows:
                    _render_grn_card(r)
