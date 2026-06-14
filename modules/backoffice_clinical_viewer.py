# modules/backoffice_clinical_viewer.py
# ==========================================================
# Backoffice Clinical Examination Viewer
# Admin panel for viewing/managing clinical records
# ==========================================================

import streamlit as st
import pandas as pd
import logging
from datetime import datetime, timedelta
from modules.sql_adapter import run_query, execute_query
from modules.clinical_print import generate_clinical_pdf

logger = logging.getLogger(__name__)


# ==========================================================
# MAIN BACKOFFICE VIEWER
# ==========================================================

def render_clinical_viewer_page():
    """
    Main backoffice page for clinical examination viewing
    
    Usage in backoffice navigation:
        if menu_selection == "Clinical Records":
            from modules.backoffice_clinical_viewer import render_clinical_viewer_page
            render_clinical_viewer_page()
    """
    
    st.title("🩺 Clinical Examination Records")
    st.markdown("---")
    
    # Tabs for different views
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Dashboard",
        "🔍 Search Records", 
        "📈 Analytics",
        "⚙️ Settings"
    ])
    
    with tab1:
        render_dashboard()
    
    with tab2:
        render_search_records()
    
    with tab3:
        render_analytics()
    
    with tab4:
        render_settings()


# ==========================================================
# DASHBOARD TAB
# ==========================================================

def render_dashboard():
    """Clinical examination dashboard with key metrics"""
    
    st.subheader("Clinical Records Dashboard")
    
    # Date range selector
    col_date1, col_date2, col_date3 = st.columns([2, 2, 1])
    
    with col_date1:
        start_date = st.date_input(
            "From Date",
            value=datetime.now() - timedelta(days=30),
            key="clinical_dash_start"
        )
    
    with col_date2:
        end_date = st.date_input(
            "To Date",
            value=datetime.now(),
            key="clinical_dash_end"
        )
    
    with col_date3:
        if st.button("🔄 Refresh", key="clinical_dash_refresh"):
            st.rerun()
    
    st.markdown("---")
    
    # Key Metrics
    metrics = get_clinical_metrics(start_date, end_date)
    
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Total Examinations", metrics.get('total_exams',0),
                  f"+{metrics.get('exams_this_week',0)} this week")
    with col2:
        st.metric("Unique Patients", metrics.get('unique_patients',0))
    with col3:
        st.metric("Avg Exams/Day", f"{metrics.get('avg_per_day',0):.1f}")
    with col4:
        st.metric("Consultations", metrics.get('consult_count',0))
    with col5:
        st.metric("Consult Revenue", f"₹{float(metrics.get('consult_revenue',0)):.0f}")
    
    st.markdown("---")
    
    # Recent Examinations
    st.subheader("Recent Examinations")
    
    # Recent — combine clinical + consultations
    try:
        from modules.sql_adapter import run_query as _rq_dash
        _recent = _rq_dash("""
            SELECT created_at::date::text AS date,
                   patient_name, COALESCE(mobile,\'\') AS mobile,
                   'Full Clinical' AS type, \'\' AS action_id
            FROM (SELECT pc.created_at, p.master_name AS patient_name, p.mobile
                  FROM patient_clinicals pc
                  LEFT JOIN patients p ON p.id=pc.patient_id
                  ORDER BY pc.created_at DESC LIMIT 10) q
            UNION ALL
            SELECT created_at::date::text AS date,
                   patient_name, COALESCE(patient_mobile,\'\') AS mobile,
                   'Consultation ₹'||COALESCE(total_value::text,\'0\') AS type,
                   id::text AS action_id
            FROM orders
            WHERE order_type='CONSULTATION'
            ORDER BY date DESC LIMIT 20
        """) or []
        if _recent:
            import pandas as _pd
            _df = _pd.DataFrame(_recent)
            for _, _row in _df.iterrows():
                _dc1,_dc2,_dc3,_dc4,_dc5 = st.columns([1.5,2,1.5,1.5,1])
                _dc1.caption(str(_row.get('date','')))
                _dc2.caption(str(_row.get('patient_name','')))
                _dc3.caption(str(_row.get('mobile','')))
                _dc4.caption(str(_row.get('type','')))
                # Convert to sales button for consultations
                _aid = str(_row.get('action_id',''))
                if _aid and str(_row.get('type','')).startswith('Consultation'):
                    if _dc5.button('➕ Bill',
                                   key=f'dash_bill_{_aid[:8]}',
                                   help='Convert to full billing order'):
                        # Convert consultation → pre-fill Retail Punching cart
                        try:
                            from modules.consultation import convert_consultation_to_billing
                            import uuid, datetime as _dt
                            _r = convert_consultation_to_billing(_aid)
                            if "error" in _r:
                                if _r.get("already_billed"):
                                    st.session_state["_consult_already_billed"] = _r.get("billed_order_no","see Retail Orders")
                                    st.rerun()
                                else:
                                    st.error(f"Convert error: {_r['error']}")
                            else:
                                _rxd = _r.get("rx",{})
                                # ✅ FIX: Use _consult_prefill (not retail_* directly) so
                                # handle_page_switch doesn't wipe the data before it lands.
                                st.session_state["_consult_prefill"] = {
                                    "patient_name":    _r["patient_name"],
                                    "patient_mobile":  _r.get("patient_mobile",""),
                                    "patient_id":      _r.get("patient_id",""),
                                    "consult_order_id": _aid,   # ← UUID for re-billing check
                                    "rx_r": {"sph":_rxd.get("sph_r",0),"cyl":_rxd.get("cyl_r",0),"axis":_rxd.get("ax_r",0),"add":_rxd.get("add_r",0)},
                                    "rx_l": {"sph":_rxd.get("sph_l",0),"cyl":_rxd.get("cyl_l",0),"axis":_rxd.get("ax_l",0),"add":_rxd.get("add_l",0)},
                                    "order_lines": [],
                                    "include_consult_fee": False,
                                }
                                st.session_state["_sidebar_page"] = "🛍️  Retail Order"
                                st.toast(f"Opening Retail Order for {_r['patient_name']}...")
                                st.rerun()
                        except Exception as _be:
                            st.error(f"Error: {_be}")
        else:
            st.info("No recent examinations")
    except Exception as _de:
        st.error(f"Dashboard error: {_de}")


# ==========================================================
# SEARCH RECORDS TAB
# ==========================================================

def render_search_records():
    """Search and view clinical records"""
    
    st.subheader("Search Clinical Records")
    
    # Search filters
    col_search1, col_search2 = st.columns([3, 1])
    
    with col_search1:
        search_term = st.text_input(
            "Search by Patient Name or Mobile",
            placeholder="Type to search...",
            key="clinical_search_term"
        )
    
    with col_search2:
        search_button = st.button("🔍 Search", key="clinical_search_btn", use_container_width=True)
    
    # Additional filters
    col_filter1, col_filter2, col_filter3 = st.columns(3)
    
    with col_filter1:
        filter_date_from = st.date_input(
            "From Date",
            value=datetime.now() - timedelta(days=90),
            key="clinical_filter_from"
        )
    
    with col_filter2:
        filter_date_to = st.date_input(
            "To Date",
            value=datetime.now(),
            key="clinical_filter_to"
        )
    
    with col_filter3:
        examiner_filter = st.selectbox(
            "Examiner",
            ["All"] + get_examiner_list(),
            key="clinical_examiner_filter"
        )
    
    st.markdown("---")
    
    # Auto-search on load (show today by default)
    if not search_term and not search_button:
        search_term = ""  # show all in date range

    results = search_clinical_records(
        search_term=search_term,
        date_from=filter_date_from,
        date_to=filter_date_to,
        examiner=examiner_filter if examiner_filter != "All" else None
    )

    if not results.empty:
        st.caption(f"{len(results)} record(s) found")

        # ── Record list (compact, clickable) ─────────────────────────
        _sel_key = "clinical_selected_idx"
        for idx, row in results.iterrows():
            _src  = str(row.get("record_source","CLINICAL"))
            _icon = "🩺" if _src == "CONSULTATION" else "📋"
            _name = str(row.get("patient_name","—"))
            _date = str(row.get("exam_date",""))[:10]
            _va   = str(row.get("va_summary",""))
            _fee  = row.get("consult_fee",0)
            _converted = bool(row.get("is_converted", False))
            if _src == "CONSULTATION":
                _status_tag = " ✅ BILLED" if _converted else ""
                _lbl = f"{_icon} {_name} — {_date} — CONSULTATION ₹{int(_fee or 0)}{_status_tag}"
            else:
                _lbl = f"{_icon} {_name} — {_date} — VA {_va}"
            _is_sel = st.session_state.get(_sel_key) == idx
            if st.button(_lbl,
                         key=f"sel_rec_{idx}",
                         use_container_width=True,
                         type="primary" if _is_sel else "secondary"):
                st.session_state[_sel_key] = idx if not _is_sel else None
                st.rerun()

        # ── Selected record detail + actions ─────────────────────────
        _sel = st.session_state.get(_sel_key)
        if _sel is not None and _sel in results.index:
            record = results.loc[_sel]
            st.markdown("---")
            _render_record_detail(record)

    else:
            st.warning("No records found matching your search criteria")


def _render_record_detail(record):
    """Detail panel for selected record — outside any expander so buttons work."""
    import os, tempfile
    _rid = str(record.get("id",""))[:8]
    _pid = str(record.get("patient_id","")) if record.get("patient_id") else ""
    _src = str(record.get("record_source","CLINICAL"))
    _tmp = tempfile.gettempdir()

    # ── Shop info ─────────────────────────────────────────────────────
    try:
        from modules.settings.shop_master import get_unit_info
        _si = get_unit_info("retail")
        _addr = ", ".join(filter(None,[_si.get("shop_address",""),
                                       _si.get("shop_city",""),
                                       _si.get("shop_state","")]))
    except Exception:
        _si = {}; _addr = ""

    # ── Patient header ────────────────────────────────────────────────
    _c1,_c2,_c3,_c4 = st.columns(4)
    _c1.metric("Patient",  str(record.get("patient_name","—")))
    _c2.metric("Date",     str(record.get("exam_date",""))[:10])
    _c3.metric("Mobile",   str(record.get("mobile","—") or "—"))
    if _src == "CONSULTATION":
        _c4.metric("Fee", f"\u20b9{float(record.get('consult_fee',0) or 0):.0f}")
    else:
        _c4.metric("VA R/L", str(record.get("va_summary","—")))

    # ── RX — always fresh lookup ──────────────────────────────────────
    def _fv(v):
        if not v or str(v).strip() in ("","None","nan","0.0","0","NaN"): return "\u2014"
        try:
            f=float(v); return f"+{f:.2f}" if f>0 else f"{f:.2f}"
        except Exception as _e:
            logger.warning("Suppressed error: %s", _e)
            return str(v)
    def _fa(v):
        if not v or str(v).strip() in ("","None","nan","0","NaN"): return "\u2014"
        try: return str(int(float(v)))
        except Exception as _e:
            logger.warning("Suppressed error: %s", _e)
            return str(v)

    rs=_fv(record.get("sph_r")); rc=_fv(record.get("cyl_r"))
    ra=_fa(record.get("axis_r")); rad=_fv(record.get("add_r"))
    ls=_fv(record.get("sph_l")); lc=_fv(record.get("cyl_l"))
    la=_fa(record.get("axis_l")); lad=_fv(record.get("add_l"))

    # For CONSULTATION: RX in patient_visits, not orders
    if all(v == "\u2014" for v in [rs,rc,ls,lc]):
        try:
            from modules.sql_adapter import run_query as _rqv
            _visit_id = str(record.get("visit_id") or "")
            _pid_uuid = str(record.get("patient_id") or "")
            _pname    = str(record.get("patient_name",""))
            _cdate    = str(record.get("exam_date",""))[:10] or "2099-12-31"
            _vrx = []

            # PRIMARY: exact visit_id stored at save time
            if _visit_id and len(_visit_id) > 10:
                _vrx = _rqv("""
                    SELECT
                        COALESCE(right_sph::text,'') AS sr,
                        COALESCE(right_cyl::text,'') AS cr,
                        COALESCE(right_axis::text,'') AS ar,
                        COALESCE(right_add::text,'') AS addr,
                        COALESCE(left_sph::text,'') AS sl,
                        COALESCE(left_cyl::text,'') AS cl,
                        COALESCE(left_axis::text,'') AS al,
                        COALESCE(left_add::text,'') AS addl
                    FROM patient_visits
                    WHERE id = %s::uuid LIMIT 1
                """, (_visit_id,)) or []

            # SECONDARY: patient UUID + exact date
            _pid_is_real_uuid = (
                _pid_uuid and len(_pid_uuid) > 10
                and not _pid_uuid.upper().startswith("TEMP-")
                and "-" in _pid_uuid
            )
            if not _vrx and _pid_is_real_uuid:
                _vrx = _rqv("""
                    SELECT
                        COALESCE(right_sph::text,'') AS sr,
                        COALESCE(right_cyl::text,'') AS cr,
                        COALESCE(right_axis::text,'') AS ar,
                        COALESCE(right_add::text,'') AS addr,
                        COALESCE(left_sph::text,'') AS sl,
                        COALESCE(left_cyl::text,'') AS cl,
                        COALESCE(left_axis::text,'') AS al,
                        COALESCE(left_add::text,'') AS addl
                    FROM patient_visits
                    WHERE patient_id = %s::uuid
                      AND visit_date = %s::date
                    ORDER BY created_at DESC LIMIT 1
                """, (_pid_uuid, _cdate)) or []

            # LEGACY FALLBACK: name ILIKE (old orders without party_id/visit_id)
            if not _vrx:
                _vrx = _rqv("""
                    SELECT
                        COALESCE(right_sph::text,'') AS sr,
                        COALESCE(right_cyl::text,'') AS cr,
                        COALESCE(right_axis::text,'') AS ar,
                        COALESCE(right_add::text,'') AS addr,
                        COALESCE(left_sph::text,'') AS sl,
                        COALESCE(left_cyl::text,'') AS cl,
                        COALESCE(left_axis::text,'') AS al,
                        COALESCE(left_add::text,'') AS addl
                    FROM patient_visits pv
                    JOIN patients p ON p.id = pv.patient_id
                    WHERE p.master_name ILIKE %s
                      AND pv.visit_date::date <= %s::date
                    ORDER BY pv.visit_date DESC LIMIT 1
                """, (_pname, _cdate)) or []

            if _vrx:
                _v = _vrx[0]
                rs=_fv(_v.get("sr")); rc=_fv(_v.get("cr"))
                ra=_fa(_v.get("ar")); rad=_fv(_v.get("addr"))
                ls=_fv(_v.get("sl")); lc=_fv(_v.get("cl"))
                la=_fa(_v.get("al")); lad=_fv(_v.get("addl"))
        except Exception:
            pass

    # RX table
    st.markdown(
        f"<table style=\'width:100%;border-collapse:collapse;font-size:12px;margin:8px 0\'>"
        f"<tr style=\'background:#1e3a5f;color:#fff\'>"
        f"<th style=\'padding:4px 8px\'>Eye</th><th style=\'padding:4px 8px\'>SPH</th>"
        f"<th style=\'padding:4px 8px\'>CYL</th><th style=\'padding:4px 8px\'>AXIS</th>"
        f"<th style=\'padding:4px 8px\'>ADD</th></tr>"
        f"<tr style=\'background:#eff6ff\'>"
        f"<td style=\'padding:4px 8px;font-weight:700\'>R</td>"
        f"<td style=\'text-align:center;padding:4px\'>{rs}</td>"
        f"<td style=\'text-align:center;padding:4px\'>{rc}</td>"
        f"<td style=\'text-align:center;padding:4px\'>{ra}</td>"
        f"<td style=\'text-align:center;padding:4px\'>{rad}</td></tr>"
        f"<tr style=\'background:#f0fdf4\'>"
        f"<td style=\'padding:4px 8px;font-weight:700\'>L</td>"
        f"<td style=\'text-align:center;padding:4px\'>{ls}</td>"
        f"<td style=\'text-align:center;padding:4px\'>{lc}</td>"
        f"<td style=\'text-align:center;padding:4px\'>{la}</td>"
        f"<td style=\'text-align:center;padding:4px\'>{lad}</td></tr>"
        f"</table>", unsafe_allow_html=True
    )

    if _src != "CONSULTATION":
        _cf = []
        _lids = str(record.get("sle_lids","") or "")
        _corn = str(record.get("sle_cornea","") or "")
        _lens2 = str(record.get("sle_lens","") or "")
        _rmk  = str(record.get("ortho_remarks","") or "")
        if _lids not in ("","Normal","—"): _cf.append("Lids: "+_lids)
        if _corn not in ("","WNL","—"):    _cf.append("Cornea: "+_corn)
        if _lens2 not in ("","WNL","—"):   _cf.append("Lens: "+_lens2)
        if _rmk: _cf.append(_rmk)
        if _cf: st.caption("  ·  ".join(_cf))
        if _cf: st.caption("  \u00b7  ".join(_cf))

    # ── Print buttons ─────────────────────────────────────────────────
    st.markdown("**Print / Cards**")
    _pa1,_pa2,_pa3,_pa4 = st.columns(4)

    # Helper to build rx tuples
    def _rxr(): return (
        "" if rs=="\u2014" else rs,
        "" if rc=="\u2014" else rc,
        "" if ra=="\u2014" else ra,
        "" if rad=="\u2014" else rad,
    )
    def _rxl(): return (
        "" if ls=="\u2014" else ls,
        "" if lc=="\u2014" else lc,
        "" if la=="\u2014" else la,
        "" if lad=="\u2014" else lad,
    )

    with _pa1:
        if st.button("\U0001f5a8\ufe0f Clinical Report", key=f"prt_{_rid}",
                     use_container_width=True):
            try:
                from modules.consultation import _print_clinical_report
                from modules.printing.patient_card_printer import ensure_patient_id
                _bc = ensure_patient_id(_pid) if _pid and _pid not in ("","None") else ""
                _hp = os.path.join(_tmp, f"rx_{_rid}_{id(record)}.html")
                _print_clinical_report(
                    name=str(record.get("patient_name","")),
                    mobile=str(record.get("mobile","") or ""),
                    date=str(record.get("exam_date",""))[:10],
                    shop=_si.get("shop_name","DV Optical"),
                    addr=_addr, phone=_si.get("shop_phone",""),
                    rx_r=_rxr(), rx_l=_rxl(),
                    va_unaided=(str(record.get("va_distance_unaided_r","") or ""),
                                str(record.get("va_distance_unaided_l","") or "")),
                    va_aided=(str(record.get("va_distance_aided_r","") or ""),
                              str(record.get("va_distance_aided_l","") or "")),
                    va_near=(str(record.get("va_near_r","") or ""),
                             str(record.get("va_near_l","") or "")),
                    lids=str(record.get("sle_lids","") or ""),
                    cornea=str(record.get("sle_cornea","") or ""),
                    lens=str(record.get("sle_lens","") or ""),
                    fundus=str(record.get("sle_fundus","") or ""),
                    iop_r="", iop_l="",
                    remarks=str(record.get("ortho_remarks","") or ""),
                    fee=float(record.get("consult_fee",0) or 0),
                    pay_mode=str(record.get("pay_mode","") or ""),
                    patient_barcode=_bc,
                    footer=_si.get("print_footer",""),
                    _save_path=_hp,
                )
                try:
                    import win32api
                    win32api.ShellExecute(0,"open",_hp,None,".",1)
                except Exception:
                    import webbrowser
                    webbrowser.open("file:///"+_hp.replace(os.sep,"/"))
                st.success("\u2705 Print dialog opened")
            except Exception as _pe:
                st.error(f"Print: {_pe}")

    with _pa2:
        _ref = st.text_input("Referral doctor",
                             placeholder="Dr. Sharma / LV Prasad",
                             key=f"ref_doc_{_rid}",
                             label_visibility="collapsed")
        if st.button("\U0001f4c4 Referral Letter", key=f"ref_{_rid}",
                     use_container_width=True):
            if not _ref.strip():
                st.warning("Enter doctor name above")
            else:
                try:
                    from modules.consultation import _print_referral_letter
                    _rp = os.path.join(_tmp, f"ref_{_rid}_{id(record)}.html")
                    _print_referral_letter(
                        name=str(record.get("patient_name","")),
                        mobile=str(record.get("mobile","") or ""),
                        date=str(record.get("exam_date",""))[:10],
                        shop=_si.get("shop_name","DV Optical"),
                        addr=_addr, phone=_si.get("shop_phone",""),
                        rx_r=_rxr(), rx_l=_rxl(),
                        va_unaided=(str(record.get("va_distance_unaided_r","") or ""),
                                    str(record.get("va_distance_unaided_l","") or "")),
                        va_aided=(str(record.get("va_distance_aided_r","") or ""),
                                  str(record.get("va_distance_aided_l","") or "")),
                        lids=str(record.get("sle_lids","") or ""),
                        cornea=str(record.get("sle_cornea","") or ""),
                        lens=str(record.get("sle_lens","") or ""),
                        fundus=str(record.get("sle_fundus","") or ""),
                        remarks=str(record.get("ortho_remarks","") or ""),
                        referral=_ref.strip(),
                        _save_path=_rp,
                    )
                    try:
                        import win32api
                        win32api.ShellExecute(0,"open",_rp,None,".",1)
                    except Exception:
                        import webbrowser
                        webbrowser.open("file:///"+_rp.replace(os.sep,"/"))
                    st.success("\u2705 Referral opened")
                except Exception as _re:
                    st.error(f"Referral: {_re}")

    with _pa3:
        if st.button("\U0001f5a8\ufe0f TSC Sticker", key=f"tsc_{_rid}",
                     use_container_width=True):
            try:
                from modules.printing.patient_card_printer import (
                    ensure_patient_id, build_tspl_patient_sticker)
                from modules.printing.label_printer import _send_tspl
                _bc = ensure_patient_id(_pid) if _pid and _pid not in ("","None") else "PAT000000"
                _fv3 = lambda v: "" if v in ("\u2014","—","") else str(v)
                tspl = build_tspl_patient_sticker(
                    barcode=_bc,
                    name=str(record.get("patient_name","")),
                    mobile=str(record.get("mobile","") or ""),
                    rx_r={"sph":_fv3(rs),"cyl":_fv3(rc),"axis":_fv3(ra),"add":_fv3(rad)},
                    rx_l={"sph":_fv3(ls),"cyl":_fv3(lc),"axis":_fv3(la),"add":_fv3(lad)},
                    shop=_si.get("shop_name","DV Optical")
                )
                ok, msg = _send_tspl(tspl)
                st.success("\u2705 Sent to TSC") if ok else st.warning(f"TSC: {msg}")
            except Exception as _te:
                st.error(f"TSC: {_te}")

    with _pa4:
        if st.button("\U0001f4b3 Evolis Card", key=f"ev_{_rid}",
                     use_container_width=True):
            try:
                from modules.printing.patient_card_printer import (
                    ensure_patient_id, _build_evolis_html)
                _bc = ensure_patient_id(_pid) if _pid and _pid not in ("","None") else "PAT000000"
                _fv3 = lambda v: "" if v in ("\u2014","—","") else str(v)
                _ehtml = _build_evolis_html(
                    barcode=_bc,
                    name=str(record.get("patient_name","")),
                    mobile=str(record.get("mobile","") or ""),
                    rx_r={"sph":_fv3(rs),"cyl":_fv3(rc),"axis":_fv3(ra),"add":_fv3(rad)},
                    rx_l={"sph":_fv3(ls),"cyl":_fv3(lc),"axis":_fv3(la),"add":_fv3(lad)},
                    shop=_si.get("shop_name","DV Optical"),
                    tagline=_si.get("shop_tagline",""),
                    visit_date=str(record.get("exam_date",""))[:10]
                )
                _etmp = os.path.join(_tmp, f"card_{_bc}.html")
                with open(_etmp,"w",encoding="utf-8") as _ef: _ef.write(_ehtml)
                try:
                    import win32api
                    win32api.ShellExecute(0,"open",_etmp,None,".",1)
                    st.success("\u2705 Opened → Ctrl+P → Evolis → CR80")
                except Exception:
                    import webbrowser
                    webbrowser.open("file:///"+_etmp.replace(os.sep,"/"))
            except Exception as _ee:
                st.error(f"Evolis: {_ee}")

    # ── Bill button — open this consultation order directly ──────────
    if _src == "CONSULTATION":
        st.markdown("---")
        _full_oid = str(record.get("id",""))

        # ── Check if already billed (show badge instead of button) ──────

        try:
            from modules.sql_adapter import run_query as _rq_bill
            _billed_rows = _rq_bill("""
                SELECT o2.order_no FROM orders o2
                JOIN orders o1 ON o1.id = %s::uuid
                WHERE o2.order_type IN ('RETAIL','WHOLESALE')
                  AND COALESCE(o2.is_deleted, false) = false
                  AND (
                      o2.customer_order_no = o1.id::text
                      OR (
                          o2.party_id IS NOT NULL
                          AND o2.party_id = o1.party_id
                          AND o2.created_at::date >= o1.created_at::date
                      )
                  )
                ORDER BY o2.created_at ASC LIMIT 1
            """, (_full_oid,)) or []
        except Exception:
            _billed_rows = []


        if _billed_rows:
            st.markdown(
                f"<div style='background:#f0fdf4;border:1px solid #22c55e;"
                f"border-radius:6px;padding:6px 12px;font-size:11px'>"
                f"✅ <b>Already Billed</b> → Order "
                f"<b>{_billed_rows[0]['order_no']}</b></div>",
                unsafe_allow_html=True
            )
        else:
            if st.button("➕ Add Products / Bill",
                         key=f"bill_{_rid}",
                         type="primary",
                         use_container_width=True,
                         help="Opens this consultation order — add product lines there"):
                # Convert consultation → pre-fill Retail Punching cart
                try:
                    from modules.consultation import convert_consultation_to_billing
                    import uuid, datetime as _dt2
                    _r2 = convert_consultation_to_billing(_full_oid)
                    if "error" in _r2:
                        if _r2.get("already_billed"):
                            st.warning(f"⚠️ Already billed as {_r2.get('billed_order_no','')}")
                        else:
                            st.error(f"Convert error: {_r2['error']}")
                    else:
                        _rxd2 = _r2.get("rx",{})
                        _prefill_data = {
                            "patient_name":    _r2["patient_name"],
                            "patient_mobile":  _r2.get("patient_mobile",""),
                            "patient_id":      _r2.get("patient_id",""),
                            "consult_order_id": _full_oid,   # ← UUID used for re-billing check
                            "rx_r": {"sph":_rxd2.get("sph_r",0),"cyl":_rxd2.get("cyl_r",0),"axis":_rxd2.get("ax_r",0),"add":_rxd2.get("add_r",0)},
                            "rx_l": {"sph":_rxd2.get("sph_l",0),"cyl":_rxd2.get("cyl_l",0),"axis":_rxd2.get("ax_l",0),"add":_rxd2.get("add_l",0)},
                            "order_lines": [],
                            "include_consult_fee": False,
                        }
                        st.session_state["_consult_prefill"] = _prefill_data
                        st.session_state["_sidebar_page"] = "🛍️  Retail Order"
                        st.toast(f"Opening Retail Order for {_r2['patient_name']}...")
                        st.rerun()
                except Exception as _be2:
                    st.error(f"Error: {_be2}")


def render_clinical_record_card(record):
    """Render individual clinical record as expandable card"""
    
    with st.expander(
        f"{'🩺' if record.get('record_source')=='CONSULTATION' else '📋'} "
        f"{record.get('patient_name','—')} — {str(record.get('exam_date',''))[:10]}"
        f"{' — CONSULTATION ₹'+str(int(record.get('consult_fee',0))) if record.get('record_source')=='CONSULTATION' else ' — VA: '+str(record.get('va_summary','—'))}",
        expanded=False
    ):
        col_info1, col_info2, col_info3 = st.columns(3)
        
        with col_info1:
            st.markdown("**Patient Details**")
            st.caption(f"Name: {record['patient_name']}")
            st.caption(f"Mobile: {record['mobile'] or 'N/A'}")
            st.caption(f"Patient ID: {record['patient_id']}")
        
        with col_info2:
            st.markdown("**Examination Details**")
            st.caption(f"Date: {str(record.get('exam_date',''))[:16]}")
            st.caption(f"Examiner: {record.get('examiner', record.get('created_by','—') or '—')}")
            st.caption(f"Record No: {record['record_no'] or 'N/A'}")
        
        with col_info3:
            st.markdown("**Actions**")
            _rid = str(record.get("id",""))[:8]
            _pid = str(record.get("patient_id",""))
            _src = str(record.get("record_source","CLINICAL"))
            _oid = str(record.get("id",""))  # order id for consultation

            # Pre-generate print file paths so links work without rerun
            import os, tempfile
            _tmp = tempfile.gettempdir()

            # Generate clinical report HTML file upfront
            try:
                from modules.consultation import _print_clinical_report as _pcr
                from modules.settings.shop_master import get_unit_info
                from modules.printing.patient_card_printer import ensure_patient_id
                _si = get_unit_info("retail")
                _addr = ", ".join(filter(None,[_si.get("shop_address",""),
                                               _si.get("shop_city",""),
                                               _si.get("shop_state","")]))
                _bc = ensure_patient_id(_pid) if _pid and _pid != "None" else ""
                def _fv(v): return str(v) if v and str(v) not in ("None","nan","") else ""
                # Build HTML and save to temp
                import io
                _html_path = os.path.join(_tmp, f"rx_{_rid}.html")
                if not os.path.exists(_html_path):
                    # Generate once and cache as file
                    _pcr(
                        name=str(record.get("patient_name","")),
                        mobile=str(record.get("mobile","") or ""),
                        date=str(record.get("exam_date",""))[:10],
                        shop=_si.get("shop_name","DV Optical"),
                        addr=_addr, phone=_si.get("shop_phone",""),
                        rx_r=(_fv(record.get("sph_r")),_fv(record.get("cyl_r")),
                              _fv(record.get("axis_r")),_fv(record.get("add_r"))),
                        rx_l=(_fv(record.get("sph_l")),_fv(record.get("cyl_l")),
                              _fv(record.get("axis_l")),_fv(record.get("add_l"))),
                        va_unaided=(record.get("va_distance_unaided_r",""),
                                    record.get("va_distance_unaided_l","")),
                        va_aided=(record.get("va_distance_aided_r",""),
                                  record.get("va_distance_aided_l","")),
                        va_near=(record.get("va_near_r",""),record.get("va_near_l","")),
                        lids=record.get("sle_lids",""),
                        cornea=record.get("sle_cornea",""),
                        lens=record.get("sle_lens",""),
                        fundus=record.get("sle_fundus",""),
                        iop_r="", iop_l="",
                        remarks=str(record.get("ortho_remarks","") or ""),
                        fee=float(record.get("consult_fee",0) or 0),
                        pay_mode=str(record.get("pay_mode","") or ""),
                        patient_barcode=_bc, footer=_si.get("print_footer",""),
                        _save_path=_html_path,
                    )
                _html_url = "file:///" + _html_path.replace(os.sep, "/")
            except Exception as _pe:
                _html_url = None
                st.caption(f"Print setup: {_pe}")

            if st.button("📄 Referral Letter",
                         key=f"ref_{_rid}",
                         use_container_width=True):
                st.session_state[f"do_ref_{_rid}"] = True

            _ref = st.session_state.get(f"ref_to_{_rid}","").strip()
            if st.session_state.pop(f"do_ref_{_rid}", False):
                if not _ref:
                    st.warning("Enter referral doctor below first")
                else:
                    try:
                        from modules.consultation import _print_referral_letter
                        from modules.settings.shop_master import get_unit_info
                        _si = get_unit_info("retail")
                        _addr = ", ".join(filter(None,[
                            _si.get("shop_address",""),_si.get("shop_city",""),
                            _si.get("shop_state","")]))
                        def _fv(v): return str(v) if v and str(v) not in ("None","nan","") else ""
                        _print_referral_letter(
                            name=str(record.get("patient_name","")),
                            mobile=str(record.get("mobile","") or ""),
                            date=str(record.get("exam_date",""))[:10],
                            shop=_si.get("shop_name","DV Optical"),
                            addr=_addr, phone=_si.get("shop_phone",""),
                            rx_r=(_fv(record.get("sph_r")),_fv(record.get("cyl_r")),
                                  _fv(record.get("axis_r")),_fv(record.get("add_r"))),
                            rx_l=(_fv(record.get("sph_l")),_fv(record.get("cyl_l")),
                                  _fv(record.get("axis_l")),_fv(record.get("add_l"))),
                            va_unaided=(record.get("va_distance_unaided_r",""),
                                        record.get("va_distance_unaided_l","")),
                            va_aided=(record.get("va_distance_aided_r",""),
                                      record.get("va_distance_aided_l","")),
                            lids=record.get("sle_lids",""),
                            cornea=record.get("sle_cornea",""),
                            lens=record.get("sle_lens",""),
                            fundus=record.get("sle_fundus",""),
                            remarks=str(record.get("ortho_remarks","") or ""),
                            referral=_ref,
                        )
                    except Exception as ex:
                        st.error(f"Referral error: {ex}")
            st.text_input("Refer to", placeholder="Dr. Sharma",
                          key=f"ref_to_{_rid}", label_visibility="collapsed")

            if st.button("🖨️ TSC Card",
                         key=f"tsc_{_rid}",
                         use_container_width=True):
                st.session_state[f"do_tsc_{_rid}"] = True

            if st.session_state.pop(f"do_tsc_{_rid}", False):
                try:
                    from modules.printing.patient_card_printer import (
                        ensure_patient_id, build_tspl_patient_sticker)
                    from modules.printing.label_printer import _send_tspl
                    from modules.settings.shop_master import get_unit_info
                    _bc = ensure_patient_id(_pid)
                    _si = get_unit_info("retail")
                    def _fv(v): return str(v) if v and str(v) not in ("None","nan","") else ""
                    tspl = build_tspl_patient_sticker(
                        barcode=_bc,
                        name=str(record.get("patient_name","")),
                        mobile=str(record.get("mobile","") or ""),
                        rx_r={"sph":_fv(record.get("sph_r")),"cyl":_fv(record.get("cyl_r")),
                              "axis":_fv(record.get("axis_r")),"add":_fv(record.get("add_r"))},
                        rx_l={"sph":_fv(record.get("sph_l")),"cyl":_fv(record.get("cyl_l")),
                              "axis":_fv(record.get("axis_l")),"add":_fv(record.get("add_l"))},
                        shop=_si.get("shop_name","DV Optical")
                    )
                    ok, msg = _send_tspl(tspl)
                    if ok: st.success("✅ Sent to TSC")
                    else: st.warning(f"TSC: {msg}")
                except Exception as ex:
                    st.error(f"TSC error: {ex}")

            if st.button("💳 Evolis Card",
                         key=f"evolis_{_rid}",
                         use_container_width=True):
                st.session_state[f"do_ev_{_rid}"] = True

            if st.session_state.pop(f"do_ev_{_rid}", False):
                try:
                    from modules.printing.patient_card_printer import (
                        ensure_patient_id, _build_evolis_html)
                    from modules.settings.shop_master import get_unit_info
                    import os, tempfile
                    _bc = ensure_patient_id(_pid)
                    _si = get_unit_info("retail")
                    def _fv(v): return str(v) if v and str(v) not in ("None","nan","") else ""
                    html = _build_evolis_html(
                        barcode=_bc,
                        name=str(record.get("patient_name","")),
                        mobile=str(record.get("mobile","") or ""),
                        rx_r={"sph":_fv(record.get("sph_r")),"cyl":_fv(record.get("cyl_r")),
                              "axis":_fv(record.get("axis_r")),"add":_fv(record.get("add_r"))},
                        rx_l={"sph":_fv(record.get("sph_l")),"cyl":_fv(record.get("cyl_l")),
                              "axis":_fv(record.get("axis_l")),"add":_fv(record.get("add_l"))},
                        shop=_si.get("shop_name","DV Optical"),
                        tagline=_si.get("shop_tagline",""),
                        visit_date=str(record.get("exam_date",""))[:10]
                    )
                    tmp = os.path.join(tempfile.gettempdir(), f"card_{_bc}.html")
                    with open(tmp,"w",encoding="utf-8") as _f: _f.write(html)
                    try:
                        import win32api
                        win32api.ShellExecute(0,"open",tmp,None,".",1)
                        st.success("✅ Opened → Ctrl+P → Evolis Primacy → CR80")
                    except Exception:
                        import webbrowser
                        webbrowser.open("file:///"+tmp.replace(os.sep,"/"))
                        st.success("✅ Opened in browser")
                except Exception as ex:
                    st.error(f"Evolis error: {ex}")
        
        st.markdown("---")
        
        # Visual Acuity Summary
        col_va1, col_va2 = st.columns(2)
        
        with col_va1:
            st.markdown("**Visual Acuity - Right Eye**")
            st.caption(f"Unaided: {record['va_distance_unaided_r']}")
            st.caption(f"Aided: {record['va_distance_aided_r']}")
            st.caption(f"Near: {record['va_near_r']}")
        
        with col_va2:
            st.markdown("**Visual Acuity - Left Eye**")
            st.caption(f"Unaided: {record['va_distance_unaided_l']}")
            st.caption(f"Aided: {record['va_distance_aided_l']}")
            st.caption(f"Near: {record['va_near_l']}")
        
        # Slit Lamp Findings (if abnormal)
        abnormal_findings = []
        if record['sle_lids'] != 'Normal':
            abnormal_findings.append(f"Lids: {record['sle_lids']}")
        if record['sle_cornea'] != 'WNL':
            abnormal_findings.append(f"Cornea: {record['sle_cornea']}")
        if record['sle_lens'] != 'WNL':
            abnormal_findings.append(f"Lens: {record['sle_lens']}")
        
        if abnormal_findings:
            st.markdown("**⚠️ Abnormal Findings**")
            for finding in abnormal_findings:
                st.caption(f"• {finding}")
        
        # Clinical Notes / Consultation fee
        _src = str(record.get('record_source','CLINICAL'))
        if _src == 'CONSULTATION':
            _fee = record.get('consult_fee',0)
            _pm  = record.get('pay_mode','')
            if _fee:
                st.caption(f"Consultation fee: ₹{float(_fee):.0f}  ·  Mode: {_pm or '—'}")
        else:
            if record.get('sle_fundus'):
                st.caption(f"Fundus: {record['sle_fundus']}")
            if record.get('ortho_remarks'):
                st.caption(f"Remarks: {record['ortho_remarks']}")


# ==========================================================
# ANALYTICS TAB
# ==========================================================

def render_analytics():
    """Clinical examination analytics and reports"""
    
    st.subheader("Clinical Analytics")
    
    # Date range
    col_date1, col_date2 = st.columns(2)
    
    with col_date1:
        analytics_start = st.date_input(
            "From Date",
            value=datetime.now() - timedelta(days=90),
            key="analytics_start"
        )
    
    with col_date2:
        analytics_end = st.date_input(
            "To Date",
            value=datetime.now(),
            key="analytics_end"
        )
    
    st.markdown("---")
    
    # Get analytics data
    analytics_data = get_clinical_analytics(analytics_start, analytics_end)
    
    # Visual Acuity Distribution
    st.subheader("📊 Visual Acuity Distribution")
    
    col_va_chart1, col_va_chart2 = st.columns(2)
    
    with col_va_chart1:
        st.markdown("**Right Eye (Aided)**")
        if analytics_data['va_distribution_r']:
            st.bar_chart(analytics_data['va_distribution_r'])
        else:
            st.info("No data available")
    
    with col_va_chart2:
        st.markdown("**Left Eye (Aided)**")
        if analytics_data['va_distribution_l']:
            st.bar_chart(analytics_data['va_distribution_l'])
        else:
            st.info("No data available")
    
    st.markdown("---")
    
    # Common Findings
    st.subheader("🔍 Most Common Findings")
    
    col_findings1, col_findings2, col_findings3 = st.columns(3)
    
    with col_findings1:
        st.markdown("**Lid Findings**")
        for finding, count in analytics_data['common_lid_findings']:
            st.caption(f"{finding}: {count}")
    
    with col_findings2:
        st.markdown("**Lens Findings**")
        for finding, count in analytics_data['common_lens_findings']:
            st.caption(f"{finding}: {count}")
    
    with col_findings3:
        st.markdown("**Orthoptic Findings**")
        for finding, count in analytics_data['common_ortho_findings']:
            st.caption(f"{finding}: {count}")
    
    st.markdown("---")
    
    # Examiner Performance
    st.subheader("👥 Examiner Statistics")
    
    examiner_stats = analytics_data['examiner_stats']
    
    if not examiner_stats.empty:
        st.dataframe(
            examiner_stats,
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No examiner data available")


# ==========================================================
# SETTINGS TAB
# ==========================================================

def render_settings():
    """Clinical module settings"""
    
    st.subheader("Clinical Module Settings")
    
    st.markdown("### 📋 Default Values")
    
    st.info("Default values for new examinations (coming soon)")
    
    st.markdown("### 👥 Examiner Management")
    
    st.info("Manage examiners and permissions (coming soon)")
    
    st.markdown("### 📊 Report Configuration")
    
    st.info("Configure PDF report templates (coming soon)")


# ==========================================================
# DATA FETCHING FUNCTIONS
# ==========================================================

def get_clinical_metrics(start_date, end_date):
    """Get dashboard metrics"""
    
    try:
        sql = """
            SELECT
                (SELECT COUNT(*) FROM patient_clinicals
                 WHERE created_at::date BETWEEN %(start_date)s AND %(end_date)s)
                +
                (SELECT COUNT(*) FROM orders
                 WHERE order_type='CONSULTATION'
                 AND created_at::date BETWEEN %(start_date)s AND %(end_date)s)
                AS total_exams,

                (SELECT COUNT(DISTINCT patient_id) FROM patient_clinicals
                 WHERE created_at::date BETWEEN %(start_date)s AND %(end_date)s)
                AS unique_patients,

                (SELECT COUNT(*) FROM patient_clinicals
                 WHERE created_at >= NOW() - INTERVAL '7 days')
                +
                (SELECT COUNT(*) FROM orders
                 WHERE order_type='CONSULTATION'
                 AND created_at >= NOW() - INTERVAL '7 days')
                AS exams_this_week,

                (SELECT COUNT(*) FROM orders
                 WHERE order_type='CONSULTATION'
                 AND created_at::date BETWEEN %(start_date)s AND %(end_date)s)
                AS consult_count,

                (SELECT COALESCE(SUM(total_value),0) FROM orders
                 WHERE order_type='CONSULTATION'
                 AND created_at::date BETWEEN %(start_date)s AND %(end_date)s)
                AS consult_revenue
        """
        
        result = run_query(sql, {
            "start_date": start_date,
            "end_date": end_date
        })
        
        if result:
            data = result[0]
            days = (end_date - start_date).days + 1
            
            return {
                'total_exams': data.get('total_exams',0),
                'unique_patients': data.get('unique_patients',0),
                'exams_this_week': data.get('exams_this_week',0),
                'avg_per_day': int(data.get('total_exams',0)) / days if days > 0 else 0,
                'completion_rate': 95.0,
                'consult_count': data.get('consult_count',0),
                'consult_revenue': data.get('consult_revenue',0),
            }
        
        return {
            'total_exams': 0,
            'unique_patients': 0,
            'exams_this_week': 0,
            'avg_per_day': 0,
            'completion_rate': 0
        }
        
    except Exception as e:
        st.error(f"Error fetching metrics: {e}")
        return {}


def get_recent_examinations(limit=20):
    """Get recent clinical examinations"""
    
    try:
        sql = """
            SELECT
                pc.id,
                pc.patient_id,
                pc.visit_id,
                pc.created_at as exam_date,
                COALESCE(pc.created_by,'—') as examiner,
                COALESCE(pc.record_no,'') as record_no,
                pc.va_distance_aided_r, pc.va_distance_aided_l,
                pc.va_distance_unaided_r, pc.va_distance_unaided_l,
                pc.va_near_r, pc.va_near_l,
                COALESCE(pc.sle_lids,'—') as sle_lids,
                COALESCE(pc.sle_cornea,'—') as sle_cornea,
                COALESCE(pc.sle_lens,'—') as sle_lens,
                COALESCE(pc.sle_fundus,'') as sle_fundus,
                COALESCE(pc.ortho_remarks,'') as ortho_remarks,
                p.master_name as patient_name,
                COALESCE(p.mobile,'') as mobile,
                COALESCE(pv.right_sph::text,\'\') as sph_r,
                COALESCE(pv.right_cyl::text,\'\') as cyl_r,
                COALESCE(pv.right_axis::text,\'\') as axis_r,
                COALESCE(pv.right_add::text,\'\') as add_r,
                COALESCE(pv.left_sph::text,\'\') as sph_l,
                COALESCE(pv.left_cyl::text,\'\') as cyl_l,
                COALESCE(pv.left_axis::text,\'\') as axis_l,
                COALESCE(pv.left_add::text,\'\') as add_l,
                COALESCE(p.barcode, p.record_no,\'\') as patient_barcode
            FROM patient_clinicals pc
            LEFT JOIN patients p ON pc.patient_id = p.id
            LEFT JOIN patient_visits pv ON pv.id = pc.visit_id
            ORDER BY pc.created_at DESC
            LIMIT %(limit)s
        """
        
        result = run_query(sql, {"limit": limit})
        
        if result:
            df = pd.DataFrame(result)
            df['va_summary'] = df.apply(
                lambda row: f"{row.get('va_distance_aided_r','—') or '—'} / {row.get('va_distance_aided_l','—') or '—'}",
                axis=1
            )
            if 'exam_date' not in df.columns:
                df['exam_date'] = df['created_at']
            return df
        
        return pd.DataFrame()
        
    except Exception as e:
        st.error(f"Error fetching recent exams: {e}")
        return pd.DataFrame()


def search_clinical_records(search_term=None, date_from=None, date_to=None, examiner=None):
    """Search clinical records with filters"""
    
    try:
        sql = """
            SELECT
                pc.id, pc.patient_id, pc.visit_id,
                pc.created_at, pc.created_at as exam_date,
                COALESCE(pc.created_by,'—') as examiner,
                pc.record_no,
                pc.va_distance_aided_r, pc.va_distance_aided_l,
                pc.va_distance_unaided_r, pc.va_distance_unaided_l,
                pc.va_near_r, pc.va_near_l,
                COALESCE(pc.sle_lids,'—') as sle_lids,
                COALESCE(pc.sle_cornea,'—') as sle_cornea,
                COALESCE(pc.sle_lens,'—') as sle_lens,
                COALESCE(pc.sle_fundus,'') as sle_fundus,
                COALESCE(pc.ortho_remarks,'') as ortho_remarks,
                p.master_name as patient_name,
                COALESCE(p.mobile,'') as mobile,
                COALESCE(pv.right_sph::text,'') as sph_r,
                COALESCE(pv.right_cyl::text,'') as cyl_r,
                COALESCE(pv.right_axis::text,'') as axis_r,
                COALESCE(pv.right_add::text,'') as add_r,
                COALESCE(pv.left_sph::text,'') as sph_l,
                COALESCE(pv.left_cyl::text,'') as cyl_l,
                COALESCE(pv.left_axis::text,'') as axis_l,
                COALESCE(pv.left_add::text,'') as add_l,
                COALESCE(p.barcode, p.record_no,'') as patient_barcode
            FROM patient_clinicals pc
            LEFT JOIN patients p ON pc.patient_id = p.id
            LEFT JOIN patient_visits pv ON pv.id = pc.visit_id
            WHERE 1=1
        """
        
        params = {}
        
        if search_term:
            sql += " AND (p.master_name ILIKE %(search)s OR p.mobile ILIKE %(search)s)"
            params['search'] = f"%{search_term}%"
        
        if date_from:
            sql += " AND pc.created_at::date >= %(date_from)s"
            params['date_from'] = date_from
        
        if date_to:
            sql += " AND pc.created_at::date <= %(date_to)s"
            params['date_to'] = date_to
        
        if examiner:
            sql += " AND pc.created_by = %(examiner)s"
            params['examiner'] = examiner
        
        sql += " ORDER BY pc.created_at DESC LIMIT 100"

        result = run_query(sql, params)
        df1 = pd.DataFrame(result) if result else pd.DataFrame()

        # Also pull consultation orders (stored as CONSULTATION order_type)
        cons_params = {}
        cons_where = ["o.order_type='CONSULTATION'"]
        if search_term:
            cons_where.append("(o.patient_name ILIKE %(search)s OR o.patient_mobile ILIKE %(search)s)")
            cons_params['search'] = f"%{search_term}%"
        if date_from:
            cons_where.append("o.created_at::date >= %(date_from)s")
            cons_params['date_from'] = date_from
        if date_to:
            cons_where.append("o.created_at::date <= %(date_to)s")
            cons_params['date_to'] = date_to

        cons_sql = f"""
            SELECT
                o.id, o.party_id::text as patient_id,
                o.customer_order_no::text as visit_id,
                o.created_at, o.created_at as exam_date,
                'Consultation' as examiner,
                o.order_no as record_no,
                '—' as va_distance_aided_r, '—' as va_distance_aided_l,
                '—' as va_distance_unaided_r, '—' as va_distance_unaided_l,
                '—' as va_near_r, '—' as va_near_l,
                '—' as sle_lids, '—' as sle_cornea, '—' as sle_lens,
                '' as sle_fundus, '' as ortho_remarks,
                o.patient_name, COALESCE(o.patient_mobile,'') as mobile,
                '' as sph_r, '' as cyl_r, '' as axis_r, '' as add_r,
                '' as sph_l, '' as cyl_l, '' as axis_l, '' as add_l,
                '' as patient_barcode,
                o.total_value as consult_fee,
                o.payment_mode as pay_mode,
                'CONSULTATION' as record_source
            FROM orders o
            WHERE {' AND '.join(cons_where)}
            ORDER BY o.created_at DESC LIMIT 100
        """
        try:
            # Ensure is_converted column exists before querying
            try:
                run_query("SELECT pg_catalog.format_type(a.atttypid, a.atttypmod) "
                         "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                         "WHERE c.relname='orders' AND a.attname='is_converted' LIMIT 1")
            except Exception:
                pass
            try:
                from modules.sql_adapter import run_write as _rw_cv
                _rw_cv("ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_converted BOOLEAN DEFAULT FALSE")
            except Exception:
                pass
            cons_result = run_query(cons_sql, cons_params)
            df2 = pd.DataFrame(cons_result) if cons_result else pd.DataFrame()
        except Exception:
            # Fallback: query without is_converted column
            try:
                _safe_sql = cons_sql.replace(
                    "COALESCE(o.is_converted, false) as is_converted  -- column added by migration",
                    "false as is_converted"
                )
                cons_result = run_query(_safe_sql, cons_params)
                df2 = pd.DataFrame(cons_result) if cons_result else pd.DataFrame()
            except Exception:
                df2 = pd.DataFrame()

        # Mark source on patient_clinicals
        if not df1.empty:
            df1['record_source'] = 'CLINICAL'
            df1['consult_fee'] = 0
            df1['pay_mode'] = ''
            df1['is_converted'] = False
        if not df2.empty and not df1.empty:
            df = pd.concat([df1, df2], ignore_index=True)
        elif not df2.empty:
            df = df2
        elif not df1.empty:
            df = df1
        else:
            return pd.DataFrame()

        df['va_summary'] = df.apply(
            lambda row: f"{row.get('va_distance_aided_r','—') or '—'} / {row.get('va_distance_aided_l','—') or '—'}",
            axis=1
        )
        if 'exam_date' not in df.columns or df['exam_date'].isna().all():
            df['exam_date'] = df['created_at']
        df = df.sort_values('created_at', ascending=False)
        return df

    except Exception as e:
        st.error(f"Error searching records: {e}")
        return pd.DataFrame()


def get_clinical_analytics(start_date, end_date):
    """Get analytics data"""
    
    # TODO: Implement actual analytics queries
    return {
        'va_distribution_r': {},
        'va_distribution_l': {},
        'common_lid_findings': [('Normal', 85), ('Blepharitis', 10), ('MGD', 5)],
        'common_lens_findings': [('WNL', 70), ('Cataract', 25), ('PCIOL', 5)],
        'common_ortho_findings': [('Ortho', 90), ('Exo', 7), ('Eso', 3)],
        'examiner_stats': pd.DataFrame()
    }


def get_examiner_list():
    """Get list of examiners"""
    
    try:
        sql = """
            SELECT DISTINCT created_by as examiner
            FROM patient_clinicals
            WHERE created_by IS NOT NULL
            ORDER BY created_by
        """
        
        result = run_query(sql, {})
        
        if result:
            return [row['examiner'] for row in result]
        
        return []
        
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return []


def show_full_clinical_record(record_id):
    """Show full clinical record in modal"""
    st.info("Full record view - coming soon")


def print_clinical_via_shell(record: dict):
    """Print clinical report via ShellExecute — opens browser print dialog."""
    import os, tempfile
    try:
        from modules.consultation import _print_clinical_report
        from modules.settings.shop_master import get_unit_info
        from modules.printing.patient_card_printer import ensure_patient_id
        _si   = get_unit_info("retail")
        _addr = ", ".join(filter(None, [
            _si.get("shop_address",""), _si.get("shop_city",""), _si.get("shop_state","")
        ]))
        _bc = ensure_patient_id(str(record.get("patient_id","")))

        # Build RX from record
        def _fv(v):
            return str(v) if v and str(v) not in ("None","nan","") else ""

        _print_clinical_report(
            name=str(record.get("patient_name","")),
            mobile=str(record.get("mobile","") or ""),
            date=str(record.get("exam_date",""))[:10],
            shop=_si.get("shop_name","DV Optical"),
            addr=_addr, phone=_si.get("shop_phone",""),
            rx_r=(_fv(record.get("sph_r")), _fv(record.get("cyl_r")),
                  _fv(record.get("axis_r")), _fv(record.get("add_r"))),
            rx_l=(_fv(record.get("sph_l")), _fv(record.get("cyl_l")),
                  _fv(record.get("axis_l")), _fv(record.get("add_l"))),
            va_unaided=(record.get("va_distance_unaided_r",""),
                        record.get("va_distance_unaided_l","")),
            va_aided=(record.get("va_distance_aided_r",""),
                      record.get("va_distance_aided_l","")),
            va_near=(record.get("va_near_r",""), record.get("va_near_l","")),
            lids=record.get("sle_lids",""),
            cornea=record.get("sle_cornea",""),
            lens=record.get("sle_lens",""),
            fundus=record.get("sle_fundus",""),
            iop_r=str(record.get("iop_r","") or ""),
            iop_l=str(record.get("iop_l","") or ""),
            remarks=str(record.get("ortho_remarks","") or ""),
            fee=0, pay_mode="",
            patient_barcode=_bc,
            footer=_si.get("print_footer",""),
        )
    except Exception as ex:
        st.error(f"Print error: {ex}")


def download_clinical_pdf(patient_id, visit_id):
    """Legacy — kept for compatibility."""
    st.info("Use the print buttons in the record card above.")
