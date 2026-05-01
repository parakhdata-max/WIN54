"""
modules/backoffice/procurement_consolidation.py
================================================
Purchase Consolidation — 4 sub-tabs:

  Tab 1 — Purchase Challans  : view / search / edit challan records
  Tab 2 — Purchase Invoices  : view / search / edit invoice records
  Tab 3 — Challans → Invoice : convert open challans to one invoice
  Tab 4 — Pending Entry      : lines billed to customer but no purchase doc
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


def _pwr(row):
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


def _load_pa(extra_where, params, limit=500):
    where_sql = " AND ".join(extra_where) if extra_where else "1=1"
    return _q(f"""
        SELECT
            pa.id::text                          AS pa_id,
            pa.order_no,
            pa.order_line_id::text               AS line_id,
            pa.challan_no,
            pa.invoice_no,
            pa.document_date::text               AS doc_date,
            pa.acknowledged_at::text             AS acked_at,
            pa.supplier_name,
            pa.supplier_id::text                 AS supplier_id,
            pa.purchase_price,
            pa.total_value,
            pa.qty,
            pa.eye_side,
            pa.is_price_locked,
            pa.transport,
            pa.lr_no,
            COALESCE(o.patient_name,o.party_name,'—') AS patient_name,
            COALESCE(p.product_name, pa.product_name, '—') AS product_name,
            COALESCE(p.category,'')              AS category,
            COALESCE(p.unit,'PCS')               AS unit,
            COALESCE(p.box_size,1)               AS box_size,
            ol.sph, ol.cyl, ol.axis, ol.add_power
        FROM purchase_acknowledgements pa
        LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
        LEFT JOIN orders o       ON o.id  = ol.order_id
        LEFT JOIN products p     ON p.id  = ol.product_id
        WHERE {where_sql}
        ORDER BY pa.document_date DESC NULLS LAST,
                 pa.acknowledged_at DESC,
                 pa.supplier_name, pa.order_no
        LIMIT {limit}
    """, params)


def _filter_bar(prefix, default_days=60):
    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 1])
        _sup  = c1.text_input("Supplier", placeholder="🔍 Supplier",
                               key=f"{prefix}_sup", label_visibility="collapsed")
        _ord  = c2.text_input("Order / Ref", placeholder="🔍 Order / challan / invoice no",
                               key=f"{prefix}_ord", label_visibility="collapsed")
        _prod = c3.text_input("Product", placeholder="🔍 Product name",
                               key=f"{prefix}_prod", label_visibility="collapsed")
        _from = c4.date_input("From",
                               value=datetime.date.today() - datetime.timedelta(days=default_days),
                               key=f"{prefix}_from", label_visibility="collapsed",
                               format="DD/MM/YYYY")
        if c5.button("🔄", key=f"{prefix}_refresh", use_container_width=True):
            st.rerun()

    where = ["DATE(COALESCE(pa.document_date, pa.acknowledged_at::date)) >= %(df)s"]
    params = {"df": str(_from)}
    if _sup.strip():
        where.append("LOWER(COALESCE(pa.supplier_name,'')) LIKE %(sup)s")
        params["sup"] = f"%{_sup.strip().lower()}%"
    if _ord.strip():
        where.append("(LOWER(COALESCE(pa.order_no,'')) LIKE %(ord)s "
                     " OR LOWER(COALESCE(pa.challan_no,'')) LIKE %(ord)s "
                     " OR LOWER(COALESCE(pa.invoice_no,'')) LIKE %(ord)s)")
        params["ord"] = f"%{_ord.strip().lower()}%"
    if _prod.strip():
        where.append("(LOWER(COALESCE(p.product_name,'')) LIKE %(prod)s "
                     " OR LOWER(COALESCE(pa.product_name,'')) LIKE %(prod)s)")
        params["prod"] = f"%{_prod.strip().lower()}%"
    return where, params


def _render_pa_editable(row, prefix):
    """One editable row card for a purchase_acknowledgements entry."""
    _pa_id   = row["pa_id"]
    _pname   = (row.get("product_name") or "—")[:35]
    _eye     = str(row.get("eye_side","")).upper()
    _pw      = _pwr(row)
    _qty     = int(row.get("qty") or 1)
    _price   = float(row.get("purchase_price") or 0)
    _total   = float(row.get("total_value") or 0)
    _locked  = bool(row.get("is_price_locked"))
    _chal    = row.get("challan_no","") or ""
    _inv     = row.get("invoice_no","") or ""
    _doc_dt  = row.get("doc_date","") or ""
    _patient = row.get("patient_name","—")
    _ono     = row.get("order_no","—")
    _unit    = str(row.get("unit","PCS")).upper()
    _bsize   = int(row.get("box_size") or 1)
    _trans   = row.get("transport","") or ""
    _lr      = row.get("lr_no","") or ""

    if _unit == "BOX" and _bsize > 1:
        _nb = _qty // _bsize
        _np = _qty % _bsize
        _qty_lbl = f"{_nb} Box ({_qty} pcs)" + (f" + {_np}" if _np else "")
    else:
        _qty_lbl = f"{_qty} pcs"

    if _inv:
        _sc, _st = "#22c55e", f"🧾 {_inv}"
    elif _chal:
        _sc, _st = "#3b82f6", f"📋 {_chal}"
    else:
        _sc, _st = "#ef4444", "⚠️ No doc"

    _kb = f"{prefix}_{str(_pa_id).replace('-','')[-8:]}"

    st.markdown(
        f"<div style='background:#080f1a;border:1px solid #1e293b;"
        f"border-left:4px solid {_sc};border-radius:6px;padding:6px 12px;margin:3px 0'>"
        f"<div style='display:flex;justify-content:space-between'>"
        f"<span style='color:#f1f5f9;font-weight:700;font-size:0.85rem'>"
        f"{_ono} &nbsp; {_eye} &nbsp; {_pname}</span>"
        f"<span style='color:{_sc};font-size:0.72rem;font-weight:700'>{_st}</span>"
        f"</div>"
        f"<div style='color:#64748b;font-size:0.7rem;margin-top:2px'>"
        f"{_pw}  ·  {_qty_lbl}  ·  &#8377;{_price:,.2f}/pc  ·  Total &#8377;{_total:,.0f}"
        + (f"  ·  {_patient}" if _patient != "—" else "")
        + (f"  ·  🚚 {_trans}" if _trans else "")
        + (f"  ·  LR: {_lr}" if _lr else "")
        + "</div></div>",
        unsafe_allow_html=True
    )

    with st.expander("✏️ Edit", expanded=False):
        _e1, _e2, _e3, _e4 = st.columns(4)
        with _e1:
            _np = st.number_input("Purchase Price ₹/pc", min_value=0.0,
                                   value=_price, step=0.5, format="%.2f",
                                   key=f"ep_{_kb}", disabled=_locked)
            if _locked:
                st.caption("🔒 Price locked")
        with _e2:
            _nc = st.text_input("Challan No.", value=_chal, key=f"ec_{_kb}",
                                 placeholder="CH-001")
            _ni = st.text_input("Invoice No.", value=_inv, key=f"ei_{_kb}",
                                 placeholder="INV/2025-26/001")
        with _e3:
            try:
                _dd = datetime.date.fromisoformat(str(_doc_dt)[:10]) if _doc_dt else datetime.date.today()
            except Exception:
                _dd = datetime.date.today()
            _nd = st.date_input("Document Date", value=_dd, key=f"ed_{_kb}",
                                 format="DD/MM/YYYY")
            _nt = st.text_input("Transport", value=_trans, key=f"et_{_kb}",
                                 placeholder="e.g. DTDC")
        with _e4:
            _nlr = st.text_input("LR / AWB No.", value=_lr, key=f"elr_{_kb}",
                                  placeholder="LR-12345")
            st.markdown("")
            if st.button("💾 Save", key=f"es_{_kb}", type="primary",
                         use_container_width=True):
                st.session_state[f"dosave_{_kb}"] = True

        if st.session_state.pop(f"dosave_{_kb}", False):
            _ok = _rw("""
                UPDATE purchase_acknowledgements SET
                    purchase_price = CASE WHEN %(lk)s THEN purchase_price ELSE %(p)s END,
                    total_value    = CASE WHEN %(lk)s THEN total_value    ELSE %(tv)s END,
                    challan_no     = %(cn)s,
                    invoice_no     = %(iv)s,
                    document_date  = %(dd)s::date,
                    transport      = %(tr)s,
                    lr_no          = %(lr)s,
                    acknowledged_at = NOW()
                WHERE id = %(id)s::uuid
            """, {
                "lk": _locked,
                "p":  _np,
                "tv": round(_np * _qty, 2),
                "cn": _nc.strip() or None,
                "iv": _ni.strip() or None,
                "dd": str(_nd),
                "tr": _nt.strip() or None,
                "lr": _nlr.strip() or None,
                "id": _pa_id,
            })
            if _ok:
                st.success("&#10003; Saved")
                st.rerun()


# ── Tab 1: Purchase Challans ────────────────────────────────────────────────────

def _render_tab_challans():
    st.markdown("### 📋 Purchase Challans")
    st.caption(
        "All challan-recorded purchases. Search by supplier, order, product or date. "
        "Click ✏️ Edit on any row to update price or reference. "
        "Select rows to convert to an invoice."
    )
    _where, _params = _filter_bar("ch")
    _where.append("pa.challan_no IS NOT NULL")
    if not st.toggle("Show invoiced challans", value=False, key="ch_show_inv"):
        _where.append("pa.invoice_no IS NULL")

    rows = _load_pa(_where, _params)
    if not rows:
        st.info("No challan records found.")
        return

    from collections import OrderedDict as _od
    _by_sup = _od()
    for r in rows:
        s = r.get("supplier_name","—")
        c = r.get("challan_no","—")
        if s not in _by_sup: _by_sup[s] = _od()
        if c not in _by_sup[s]: _by_sup[s][c] = []
        _by_sup[s][c].append(r)

    m1, m2, m3 = st.columns(3)
    m1.metric("Lines",    len(rows))
    m2.metric("Challans", sum(len(v) for v in _by_sup.values()))
    m3.metric("Value",    f"₹{sum(float(r.get('total_value') or 0) for r in rows):,.0f}")
    st.markdown("---")

    # Quick convert bar
    _free_ids = [r["pa_id"] for r in rows if not r.get("invoice_no")]
    _n_sel = sum(1 for lid in _free_ids if st.session_state.get(f"chk2_{str(lid)[-8:]}", False))
    _sel_val = sum(float(r.get("total_value") or 0) for r in rows
                   if st.session_state.get(f"chk2_{str(r['pa_id'])[-8:]}", False))
    if _n_sel:
        v1, v2, v3 = st.columns([2, 2, 3])
        _cinv  = v1.text_input("Invoice No. *", placeholder="INV/2025-26/042", key="ch_cinv")
        _cdate = v2.date_input("Invoice Date", key="ch_cdate", format="DD/MM/YYYY")
        if v3.button(f"🧾 Convert {_n_sel} lines → Invoice",
                     key="ch_conv_btn", type="primary",
                     use_container_width=True, disabled=not _cinv.strip()):
            _ids = [lid for lid in _free_ids
                    if st.session_state.get(f"chk2_{str(lid)[-8:]}", False)]
            if all(_rw("UPDATE purchase_acknowledgements SET invoice_no=%(i)s WHERE id=%(id)s::uuid",
                       {"i": _cinv.strip(), "id": x}) for x in _ids):
                st.success(f"&#10003; Invoice {_cinv} registered for {len(_ids)} line(s)")
                for lid in _ids:
                    st.session_state.pop(f"chk2_{str(lid)[-8:]}", None)
                st.rerun()
        st.markdown("---")

    for sn, challans in _by_sup.items():
        st_val = sum(float(r.get("total_value") or 0) for cl in challans.values() for r in cl)
        st.markdown(
            f"<div style='color:#f59e0b;font-weight:700;font-size:0.85rem;margin:8px 0 3px'>"
            f"🏭 {sn} &nbsp;<span style='color:#475569;font-weight:400;font-size:0.78rem'>"
            f"&#8377;{st_val:,.0f}</span></div>",
            unsafe_allow_html=True
        )
        for cno, clines in challans.items():
            ct = sum(float(r.get("total_value") or 0) for r in clines)
            cd = str(clines[0].get("doc_date",""))[:10]
            ai = all(r.get("invoice_no") for r in clines)
            with st.expander(
                f"{'🧾' if ai else '📋'} {cno}  ·  {cd}  ·  "
                f"{len(clines)} line(s)  ·  ₹{ct:,.0f}"
                + (f"  → {clines[0]['invoice_no']}" if ai else ""),
                expanded=False
            ):
                for r in clines:
                    _lid8 = str(r["pa_id"])[-8:]
                    if not r.get("invoice_no"):
                        ca, cb = st.columns([1, 10])
                        ca.checkbox("", key=f"chk2_{_lid8}")
                        with cb:
                            _render_pa_editable(r, "ch2")
                    else:
                        _render_pa_editable(r, "ch2")


# ── Tab 2: Purchase Invoices ────────────────────────────────────────────────────

def _render_tab_invoices():
    st.markdown("### 🧾 Purchase Invoices")
    st.caption(
        "All invoice-recorded purchases. Search by supplier, order, product or date. "
        "Click ✏️ Edit to correct price or details."
    )
    _where, _params = _filter_bar("inv")
    _where.append("pa.invoice_no IS NOT NULL")

    rows = _load_pa(_where, _params)
    if not rows:
        st.info("No invoice records found.")
        return

    from collections import OrderedDict as _od2
    _by_sup = _od2()
    for r in rows:
        s = r.get("supplier_name","—")
        i = r.get("invoice_no","—")
        if s not in _by_sup: _by_sup[s] = _od2()
        if i not in _by_sup[s]: _by_sup[s][i] = []
        _by_sup[s][i].append(r)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Lines",     len(rows))
    m2.metric("Invoices",  sum(len(v) for v in _by_sup.values()))
    m3.metric("Suppliers", len(_by_sup))
    m4.metric("Value",     f"₹{sum(float(r.get('total_value') or 0) for r in rows):,.0f}")
    st.markdown("---")

    for sn, invoices in _by_sup.items():
        st_val = sum(float(r.get("total_value") or 0) for il in invoices.values() for r in il)
        st.markdown(
            f"<div style='color:#22c55e;font-weight:700;font-size:0.85rem;margin:8px 0 3px'>"
            f"🏭 {sn} &nbsp;<span style='color:#475569;font-weight:400;font-size:0.78rem'>"
            f"&#8377;{st_val:,.0f}</span></div>",
            unsafe_allow_html=True
        )
        for ino, ilines in invoices.items():
            it = sum(float(r.get("total_value") or 0) for r in ilines)
            id_ = str(ilines[0].get("doc_date",""))[:10]
            itr = ilines[0].get("transport","") or ""
            ilr = ilines[0].get("lr_no","") or ""
            with st.expander(
                f"🧾 {ino}  ·  {id_}  ·  {len(ilines)} line(s)  ·  ₹{it:,.0f}"
                + (f"  ·  🚚 {itr}" if itr else ""),
                expanded=False
            ):
                if ilr:
                    st.caption(f"LR / AWB: {ilr}")
                for r in ilines:
                    _render_pa_editable(r, "inv2")


# ── Tab 3: Challans → Invoice ───────────────────────────────────────────────────

def _render_tab_challan_to_invoice():
    st.markdown("### 🔄 Convert Challans → Purchase Invoice")
    st.caption(
        "Select open challan lines (no invoice yet) and register as one invoice. "
        "Use for suppliers who send a monthly or fortnightly consolidated invoice."
    )

    rows = _load_pa(["pa.challan_no IS NOT NULL", "pa.invoice_no IS NULL"], {})
    if not rows:
        st.info("No open challans — all challan lines already have an invoice.")
        return

    def _ckey(pa_id):
        return f"cti_{str(pa_id).replace('-','')[-10:]}"

    _all_ids = [r["pa_id"] for r in rows]
    _n = sum(1 for lid in _all_ids if st.session_state.get(_ckey(lid), False))
    _sv = sum(float(r.get("total_value") or 0) for r in rows
              if st.session_state.get(_ckey(r["pa_id"]), False))
    _ssup = list(dict.fromkeys(r.get("supplier_name","—") for r in rows
                               if st.session_state.get(_ckey(r["pa_id"]), False)))

    if _n:
        st.markdown(
            f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
            f"border-radius:8px;padding:10px 14px;margin-bottom:8px'>"
            f"<b style='color:#60a5fa'>{_n} line(s) selected</b>"
            f" · Supplier: <b style='color:#e2e8f0'>{', '.join(_ssup[:2])}</b>"
            f" · Total: <b style='color:#10b981'>&#8377;{_sv:,.0f}</b></div>",
            unsafe_allow_html=True
        )
        ci1, ci2, ci3 = st.columns([2, 2, 2])
        cinv  = ci1.text_input("Invoice No. *", placeholder="INV/2025-26/042", key="cti_inv")
        cdate = ci2.date_input("Invoice Date", key="cti_date", format="DD/MM/YYYY")
        ci3.text_input("Notes", placeholder="e.g. April consolidated", key="cti_notes")
        if st.button(f"🧾 Register Invoice for {_n} Lines",
                     key="cti_reg", type="primary",
                     use_container_width=True, disabled=not cinv.strip()):
            ids = [lid for lid in _all_ids if st.session_state.get(_ckey(lid), False)]
            if all(_rw("UPDATE purchase_acknowledgements SET invoice_no=%(i)s WHERE id=%(id)s::uuid",
                       {"i": cinv.strip(), "id": x}) for x in ids):
                st.success(f"&#10003; Invoice {cinv} for {len(ids)} line(s) · &#8377;{_sv:,.0f}")
                for lid in ids:
                    st.session_state.pop(_ckey(lid), None)
                st.rerun()
        st.markdown("---")

    from collections import OrderedDict as _od3
    _by_sup = _od3()
    for r in rows:
        s = r.get("supplier_name","—")
        if s not in _by_sup: _by_sup[s] = []
        _by_sup[s].append(r)

    for sn, srows in _by_sup.items():
        sup_ids = [r["pa_id"] for r in srows]
        sup_all = all(st.session_state.get(_ckey(lid), False) for lid in sup_ids)
        st.markdown(
            f"<div style='color:#f59e0b;font-weight:700;font-size:0.85rem;margin:8px 0 3px'>"
            f"🏭 {sn} &nbsp;<span style='color:#475569;font-weight:400;font-size:0.78rem'>"
            f"{len(srows)} lines</span></div>",
            unsafe_allow_html=True
        )
        if st.checkbox(f"Select all from {sn}", value=sup_all,
                       key=f"cti_sa_{abs(hash(sn)) % 99999}"):
            for lid in sup_ids: st.session_state[_ckey(lid)] = True
        else:
            for lid in sup_ids: st.session_state[_ckey(lid)] = False

        for r in srows:
            _eye = str(r.get("eye_side","")).upper()
            _pn  = (r.get("product_name",""))[:28]
            _pw  = _pwr(r)
            la, lb = st.columns([1, 10])
            la.checkbox("", key=_ckey(r["pa_id"]))
            lb.markdown(
                f"<span style='font-size:0.8rem;color:#e2e8f0'>"
                f"<b>{r.get('order_no','—')}</b> · {_eye} · {_pn}"
                f"</span><br>"
                f"<span style='font-size:0.7rem;color:#64748b'>"
                f"{_pw} · {int(r.get('qty') or 1)} pcs · "
                f"&#8377;{float(r.get('purchase_price') or 0):,.2f}/pc · "
                f"Challan: {r.get('challan_no','—')}"
                f"</span>",
                unsafe_allow_html=True
            )
        st.markdown("<div style='height:1px;background:#1e293b;margin:4px 0'></div>",
                    unsafe_allow_html=True)


# ── Tab 4: Pending Entry ────────────────────────────────────────────────────────

def _render_tab_pending():
    st.markdown("### ⚠️ Pending Purchase Entry")
    st.caption(
        "Lines billed to customers but with no purchase record at all. "
        "Go to **📋 Orders → Purchase** tab to record them."
    )
    with st.container(border=True):
        pf1, pf2 = st.columns([3, 2])
        p_flt = pf1.text_input("Order / Supplier", placeholder="🔍 Filter",
                                key="pend_flt", label_visibility="collapsed")
        p_from = pf2.date_input(
            "From",
            value=datetime.date.today() - datetime.timedelta(days=60),
            key="pend_from", label_visibility="collapsed", format="DD/MM/YYYY"
        )

    pw = [
        "DATE(o.created_at) >= %(df)s",
        "COALESCE(ol.is_service_line,FALSE)=FALSE",
        "COALESCE(ol.is_deleted,FALSE)=FALSE",
        "UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')",
        "NOT EXISTS (SELECT 1 FROM purchase_acknowledgements pa WHERE pa.order_line_id=ol.id)",
        "EXISTS (SELECT 1 FROM challan_lines cl JOIN challans c ON c.id=cl.challan_id "
        "WHERE cl.order_line_id=ol.id AND c.status NOT IN ('CANCELLED','VOID'))",
    ]
    pp = {"df": str(p_from)}
    if p_flt.strip():
        pw.append("(LOWER(o.order_no) LIKE %(flt)s "
                  " OR LOWER(COALESCE(o.patient_name,o.party_name,'')) LIKE %(flt)s "
                  " OR LOWER(COALESCE(ol.lens_params->>'supplier_name','')) LIKE %(flt)s)")
        pp["flt"] = f"%{p_flt.strip().lower()}%"

    rows = _q(f"""
        SELECT ol.id::text AS line_id, o.order_no,
               COALESCE(o.patient_name,o.party_name,'—') AS patient_name,
               o.created_at::text AS order_date,
               ol.eye_side, ol.quantity, ol.unit_price,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               p.product_name,
               COALESCE(ol.lens_params->>'supplier_name','') AS supplier_hint
        FROM order_lines ol
        JOIN orders o   ON o.id = ol.order_id
        JOIN products p ON p.id = ol.product_id
        WHERE {" AND ".join(pw)}
        ORDER BY o.created_at DESC, o.order_no, ol.eye_side
        LIMIT 200
    """, pp)

    if not rows:
        st.success("&#10003; No pending entries — all billed lines have purchase records.")
        return

    st.warning(f"&#9888; **{len(rows)} line(s)** have no purchase record.")

    from collections import OrderedDict as _od4
    by_ord = _od4()
    for r in rows:
        ono = r["order_no"]
        if ono not in by_ord: by_ord[ono] = []
        by_ord[ono].append(r)

    for ono, olines in by_ord.items():
        patient = olines[0].get("patient_name","—")
        odate   = str(olines[0].get("order_date",""))[:10]
        st.markdown(
            f"<div style='background:#1a0000;border:1px solid #ef444433;"
            f"border-left:4px solid #ef4444;border-radius:6px;"
            f"padding:6px 12px;margin:3px 0'>"
            f"<b style='color:#f87171'>{ono}</b>"
            f"<span style='color:#64748b;font-size:0.78rem'> — {patient} · {odate}</span><br>"
            f"<span style='color:#94a3b8;font-size:0.7rem'>"
            + " | ".join(
                f"{str(r.get('eye_side','')).upper()} {r.get('product_name','')[:20]} {_pwr(r)}"
                + (f" [{r['supplier_hint']}]" if r.get("supplier_hint") else "")
                for r in olines
            )
            + "</span></div>",
            unsafe_allow_html=True
        )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def render_procurement_consolidation():
    """Main entry — called from production_page procurement tab."""
    st.markdown("### 🛒 Purchase Consolidation")

    t1, t2, t3, t4 = st.tabs([
        "📋 Purchase Challans",
        "🧾 Purchase Invoices",
        "🔄 Challans → Invoice",
        "⚠️ Pending Entry",
    ])
    with t1: _render_tab_challans()
    with t2: _render_tab_invoices()
    with t3: _render_tab_challan_to_invoice()
    with t4: _render_tab_pending()
