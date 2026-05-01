"""
modules/consultation.py
=========================
Consultation-only flow — examination without product sale.

Flow:
  1. Patient selected → power entry → clinical exam
  2. This module: consultation charge + print report + close
  
Prints:
  - Clinical prescription slip (Rx + VA + findings)
  - Referral letter (to specialist)
"""

import streamlit as st
import streamlit.components.v1 as components
from datetime import date


def _open_print_tab(html: str, filename: str = "clinical_report.html"):
    """Save HTML to temp file and open in default browser."""
    import os, tempfile
    tmp = os.path.join(tempfile.gettempdir(), filename)
    with open(tmp, "w", encoding="utf-8") as _f:
        _f.write(html)
    _url = "file:///" + tmp.replace(os.sep, "/")
    try:
        import win32api
        win32api.ShellExecute(0, "open", tmp, None, ".", 1)
        st.success("Opened in browser — press Ctrl+P to print")
    except Exception:
        try:
            import webbrowser
            webbrowser.open(_url)
            st.success("Opened in browser — press Ctrl+P to print")
        except Exception as _ex:
            st.warning(f"Could not open: {_ex}")
            st.download_button("Download & Print", html.encode("utf-8"),
                file_name=filename, mime="text/html",
                key=f"dl_{abs(hash(html))%99999}")

def _shop(key="shop_name", default="DV Optical"):
    try:
        from modules.sql_adapter import run_query
        r = run_query(f"SELECT value FROM system_flags WHERE key='{key}' LIMIT 1") or []
        return r[0].get("value", default) if r else default
    except: return default


def _fmt(val, d=2):
    if val is None or str(val).strip() in ('', 'None', 'nan', '0.0', '0'):
        return '—'
    try:
        f = float(val)
        return f"+{f:.{d}f}" if f > 0 else f"{f:.{d}f}"
    except: return str(val)

def _fax(val):
    if val is None or str(val).strip() in ('', 'None', 'nan', '0'):
        return '—'
    try: return str(int(float(val)))
    except: return str(val)


def render_consultation_close():
    """
    Consultation-only closing panel.
    Shows after clinical exam is filled.
    Handles: consultation charges + print clinical report + print referral + close visit.
    """
    pid  = st.session_state.get("retail_patient_id")
    if not pid:
        return

    # ── Ensure patient has a unique barcode ID ─────────────────────────────
    try:
        from modules.printing.patient_card_printer import (
            ensure_patient_id, render_patient_card_buttons,
            render_patient_id_badge, barcode_svg
        )
        patient_barcode = ensure_patient_id(pid)
    except Exception:
        patient_barcode = pid[:8].upper()

    name = st.session_state.get("retail_patient_name", "")
    # In edit mode _erp_patient_mob is set from the order record — prefer that
    mob  = (
        st.session_state.get("_erp_patient_mob","") or
        st.session_state.get("retail_patient_mobile", "")
    )

    # Edit mode flags — defined early, used throughout the function
    _in_edit_mode  = bool(
        st.session_state.get("_editing_consult_order_id") or
        st.session_state.get("_erp_order_id")
    )
    def _is_valid_uuid(v):
        v = str(v or "").strip()
        return len(v) == 36 and v.count("-") == 4 and not v.upper().startswith("CONS-")

    _raw_edit_oid = (
        st.session_state.get("_editing_consult_order_id","") or
        st.session_state.get("_erp_order_id","")
    )
    # Validate — never use order_no (CONS-*) as a UUID
    _edit_order_id = _raw_edit_oid if _is_valid_uuid(_raw_edit_oid) else ""

    # ── ISSUE 2: Block editing if consultation already converted to billing ──
    if _in_edit_mode and _edit_order_id and len(_edit_order_id) > 10:
        try:
            from modules.sql_adapter import run_query as _rq_conv
            _conv_row = _rq_conv("""
                SELECT COALESCE(is_converted, FALSE) AS converted,
                       order_no
                FROM orders WHERE id=%s::uuid LIMIT 1
            """, (_edit_order_id,)) or []
            if _conv_row and _conv_row[0].get("converted"):
                # Find linked retail order for display
                _linked = _rq_conv("""
                    SELECT order_no FROM orders
                    WHERE customer_order_no=%s
                      AND order_type IN ('RETAIL','WHOLESALE')
                      AND COALESCE(is_deleted,FALSE)=FALSE
                    LIMIT 1
                """, (_edit_order_id,)) or []
                _linked_no = _linked[0]["order_no"] if _linked else "billing"
                st.markdown(
                    "<div style='background:#052e16;border:1px solid #22c55e;"
                    "border-radius:8px;padding:14px 18px;margin:10px 0'>"
                    "<span style='color:#22c55e;font-weight:700;font-size:1rem'>"
                    "🔒 Consultation Converted</span><br>"
                    f"<span style='color:#4ade80;font-size:0.85rem'>"
                    f"This consultation has been billed as order "
                    f"<b>{_linked_no}</b>. No further edits allowed.</span>"
                    "</div>",
                    unsafe_allow_html=True
                )
                return  # Hard stop — no edit UI rendered
        except Exception:
            pass

    # if name/mobile blank in edit mode, fetch from DB
    if _in_edit_mode and (not name or not mob):
        try:
            from modules.sql_adapter import run_query as _rq_nm
            # Try order first
            if _edit_order_id and len(_edit_order_id) > 10:
                _nm_row = _rq_nm(
                    "SELECT patient_name, patient_mobile FROM orders WHERE id=%s::uuid LIMIT 1",
                    (_edit_order_id,)
                ) or []
                if _nm_row:
                    if not name: name = str(_nm_row[0].get("patient_name","") or "")
                    if not mob:  mob  = str(_nm_row[0].get("patient_mobile","") or "")
            # Try patient table if still blank
            if (not name or not mob) and pid and len(str(pid)) > 10:
                _pt_row = _rq_nm(
                    "SELECT master_name, mobile FROM patients WHERE id=%s::uuid LIMIT 1",
                    (str(pid),)
                ) or []
                if _pt_row:
                    if not name: name = str(_pt_row[0].get("master_name","") or "")
                    if not mob:  mob  = str(_pt_row[0].get("mobile","") or "")
            # Update session so subsequent reruns don't re-fetch
            if name: st.session_state["retail_patient_name"]   = name
            if mob:  st.session_state["retail_patient_mobile"] = mob
        except Exception:
            pass
    try:
        from modules.settings.shop_master import get_unit_info as _gui
        _si = _gui("retail")   # Retail = Parakh Eye Care
    except:
        _si = {}
    shop  = _si.get("shop_name","DV Optical")
    addr  = ", ".join(filter(None,[
        _si.get("shop_address",""), _si.get("shop_address2",""),
        _si.get("shop_city",""), _si.get("shop_state",""), _si.get("shop_pincode","")
    ]))
    phone = _si.get("shop_phone","")
    gstin = _si.get("shop_gstin","")
    footer= _si.get("print_footer","This prescription is valid for one year.")

    st.markdown("---")
    st.markdown(
        "<div style='background:#0f172a;border-left:4px solid #10b981;"
        "padding:10px 16px;border-radius:6px'>"
        "<b style='color:#10b981;font-size:1rem'>🩺 Consultation Close</b>"
        "<span style='color:#94a3b8;font-size:0.78rem;margin-left:10px'>"
        "Examination only — no product sale</span>"
        "</div>", unsafe_allow_html=True
    )
    st.markdown("")

    # ── Consultation charge ────────────────────────────────────────────────
    _default_fee = float(_si.get("consult_fee_default","200") or "200")

    # In edit mode — pre-fill fee from existing order in DB
    _fee_widget_key = "consult_fee"
    if _in_edit_mode and _fee_widget_key not in st.session_state:
        try:
            from modules.sql_adapter import run_query as _rq_fee_pre
            _fee_pre_row = _rq_fee_pre(
                "SELECT COALESCE(total_value,0) AS fee FROM orders WHERE id=%s::uuid LIMIT 1",
                (_edit_order_id,)
            ) or []
            if _fee_pre_row:
                _default_fee = float(_fee_pre_row[0].get("fee", _default_fee) or _default_fee)
        except Exception:
            pass

    cc1, cc2, cc3 = st.columns([1, 1, 2])
    with cc1:
        consult_fee = st.number_input(
            "Consultation fee ₹", min_value=0.0, value=_default_fee, step=10.0,
            key="consult_fee"
        )
    with cc2:
        pay_mode = st.selectbox(
            "Payment mode",
            ["Cash", "UPI", "Card", "Free", "Insurance"],
            key="consult_pay_mode"
        )
    with cc3:
        referral_name = st.text_input(
            "Refer to (optional)",
            placeholder="Dr. Sharma, LV Prasad Eye Institute",
            key="consult_referral"
        )

    # ── Referral reason — only shown when a doctor is entered ────────────
    referral_reason = ""
    if referral_name.strip():
        referral_reason = st.text_area(
            "📋 Reason for Referral",
            placeholder=(
                "e.g. High myopia for fundus evaluation · "
                "Suspected glaucoma — IOP elevated · "
                "Cataract for surgical opinion · "
                "Amblyopia management · "
                "Diabetic retinopathy screening"
            ),
            height=68,
            key="consult_referral_reason",
            help="State clearly why you are referring — appears on the referral letter"
        )

    # ── Get current RX from session (stored in retail_new_rx_r/l dicts) ──
    # In edit mode — if session RX is empty, load directly from patient_visits
    _rx_r = st.session_state.get("retail_new_rx_r") or {}
    _rx_l = st.session_state.get("retail_new_rx_l") or {}

    if _in_edit_mode and not _rx_r:   # only reload if truly empty, not if plano (all zeros)
        try:
            from modules.sql_adapter import run_query as _rq_rxed
            _vid_ed = st.session_state.get("_erp_visit_id","") or \
                      str(st.session_state.get("_edit_order_id_for_rx","") or "")
            _rx_rows_ed = []
            if _vid_ed and len(_vid_ed) > 10:
                _rx_rows_ed = _rq_rxed("""
                    SELECT right_sph, right_cyl, right_axis, right_add,
                           left_sph,  left_cyl,  left_axis,  left_add
                    FROM patient_visits WHERE id=%s::uuid LIMIT 1
                """, (_vid_ed,)) or []
            if not _rx_rows_ed and pid and len(str(pid)) > 10:
                _rx_rows_ed = _rq_rxed("""
                    SELECT right_sph, right_cyl, right_axis, right_add,
                           left_sph,  left_cyl,  left_axis,  left_add
                    FROM patient_visits WHERE patient_id=%s::uuid
                    ORDER BY visit_date DESC, created_at DESC LIMIT 1
                """, (str(pid),)) or []
            if _rx_rows_ed:
                _rxed = _rx_rows_ed[0]
                _rx_r = {"sph": _rxed.get("right_sph"), "cyl": _rxed.get("right_cyl"),
                         "axis": _rxed.get("right_axis"), "add": _rxed.get("right_add")}
                _rx_l = {"sph": _rxed.get("left_sph"),  "cyl": _rxed.get("left_cyl"),
                         "axis": _rxed.get("left_axis"),  "add": _rxed.get("left_add")}
                st.session_state["retail_new_rx_r"] = _rx_r
                st.session_state["retail_new_rx_l"] = _rx_l
        except Exception:
            pass

    def _rx_val(d, key):
        """Extract RX value preserving 0 (plano). None/missing → empty string."""
        v = d.get(key)
        return v if v is not None else ""

    rx_r_sph  = _rx_val(_rx_r, "sph")
    rx_r_cyl  = _rx_val(_rx_r, "cyl")
    rx_r_axis = _rx_val(_rx_r, "axis")
    rx_r_add  = _rx_val(_rx_r, "add")
    rx_l_sph  = _rx_val(_rx_l, "sph")
    rx_l_cyl  = _rx_val(_rx_l, "cyl")
    rx_l_axis = _rx_val(_rx_l, "axis")
    rx_l_add  = _rx_val(_rx_l, "add")

    # ── Clinical findings from session ─────────────────────────────────────
    cx = st.session_state.get("retail_clinical_exam", {})
    va_ur = cx.get("va_distance_unaided_r","—")
    va_ul = cx.get("va_distance_unaided_l","—")
    va_ar = cx.get("va_distance_aided_r","—")
    va_al = cx.get("va_distance_aided_l","—")
    va_nr = cx.get("va_near_r","—")
    va_nl = cx.get("va_near_l","—")
    lids  = cx.get("sle_lids","")
    cornea= cx.get("sle_cornea","")
    lens  = cx.get("sle_lens","")
    fundus= cx.get("sle_fundus","")
    iop_r = cx.get("iop_right","")
    iop_l = cx.get("iop_left","")
    remarks = cx.get("ortho_remarks","")

    today = date.today().strftime("%d %b %Y")

    # ── FIX 3: Add to Billing checkbox ────────────────────────────────────
    # Replaces the old caption hint with a real checkbox.
    # When ticked → billing product lines panel appears below consultation close.
    _add_to_billing = st.checkbox(
        "🛍️  Add product lines to this visit (spectacles / lenses / frames)",
        value=st.session_state.get("consult_add_billing", False),
        key="consult_add_billing",
        help="Tick to punch product lines in the same visit. "
             "Consultation fee is already collected — only product billing added.",
    )
    if _add_to_billing:
        st.info(
            "📋 Billing lines will appear below after saving consultation. "
            "Save the consultation first, then punch product lines.",
            icon="ℹ️"
        )

    # ── Patient ID card print section ────────────────────────────────────
    st.markdown("**Patient ID Card**")
    try:
        render_patient_card_buttons(
            patient_id=pid, patient_name=name, mobile=mob,
            rx_r={"sph":rx_r_sph,"cyl":rx_r_cyl,"axis":rx_r_axis,"add":rx_r_add},
            rx_l={"sph":rx_l_sph,"cyl":rx_l_cyl,"axis":rx_l_axis,"add":rx_l_add},
            visit_date=today
        )
    except Exception as _pce:
        st.caption(f"Patient card: {_pce}")
    st.markdown("---")

    # ── helper ────────────────────────────────────────────────────────────
    def _do_print():
        # ALWAYS fetch name+mobile from DB before print — never trust session state
        # This fixes first-print blank name issue (session not yet committed)
        _p_name = name or ""
        _p_mob  = mob or ""
        _p_rx_r = (rx_r_sph, rx_r_cyl, rx_r_axis, rx_r_add)
        _p_rx_l = (rx_l_sph, rx_l_cyl, rx_l_axis, rx_l_add)

        _lookup_id = (
            _edit_order_id or
            st.session_state.get("_erp_order_id","") or
            st.session_state.get("_editing_consult_order_id","")
        )
        if _lookup_id and len(str(_lookup_id)) > 10:
            try:
                from modules.sql_adapter import run_query as _rq_pr
                _pr_row = _rq_pr(
                    "SELECT patient_name, patient_mobile FROM orders WHERE id=%s::uuid LIMIT 1",
                    (str(_lookup_id),)
                ) or []
                if _pr_row:
                    _db_name = str(_pr_row[0].get("patient_name","") or "")
                    _db_mob  = str(_pr_row[0].get("patient_mobile","") or "")
                    if _db_name: _p_name = _db_name
                    if _db_mob:  _p_mob  = _db_mob
            except Exception: pass
        # Fallback to patient table if still blank
        if not _p_name and pid and len(str(pid)) > 10:
            try:
                from modules.sql_adapter import run_query as _rq_pt
                _pt_row = _rq_pt(
                    "SELECT master_name, mobile FROM patients WHERE id=%s::uuid LIMIT 1",
                    (str(pid),)
                ) or []
                if _pt_row:
                    if not _p_name: _p_name = str(_pt_row[0].get("master_name","") or "")
                    if not _p_mob:  _p_mob  = str(_pt_row[0].get("mobile","") or "")
            except Exception: pass

        # If RX is all zero/empty, fetch from patient_visits
        _rx_empty = all(not x for x in _p_rx_r)
        if _rx_empty and pid and len(str(pid)) > 10:
            try:
                from modules.sql_adapter import run_query as _rq_rx
                _vid_pr = st.session_state.get("_erp_visit_id","")
                _rxpr = []
                if _vid_pr and len(_vid_pr) > 10:
                    _rxpr = _rq_rx(
                        "SELECT right_sph,right_cyl,right_axis,right_add,"
                        "left_sph,left_cyl,left_axis,left_add "
                        "FROM patient_visits WHERE id=%s::uuid LIMIT 1",
                        (_vid_pr,)
                    ) or []
                if not _rxpr:
                    _rxpr = _rq_rx(
                        "SELECT right_sph,right_cyl,right_axis,right_add,"
                        "left_sph,left_cyl,left_axis,left_add "
                        "FROM patient_visits WHERE patient_id=%s::uuid "
                        "ORDER BY visit_date DESC LIMIT 1",
                        (str(pid),)
                    ) or []
                if _rxpr:
                    _rx = _rxpr[0]
                    _p_rx_r = (_rx.get("right_sph"), _rx.get("right_cyl"), _rx.get("right_axis"), _rx.get("right_add"))
                    _p_rx_l = (_rx.get("left_sph"),  _rx.get("left_cyl"),  _rx.get("left_axis"),  _rx.get("left_add"))
            except Exception: pass

        _print_clinical_report(
            name=_p_name, mobile=_p_mob, date=today,
            shop=shop, addr=addr, phone=phone,
            rx_r=_p_rx_r,
            rx_l=_p_rx_l,
            va_unaided=(va_ur, va_ul), va_aided=(va_ar, va_al), va_near=(va_nr, va_nl),
            lids=lids, cornea=cornea, lens=lens, fundus=fundus,
            iop_r=iop_r, iop_l=iop_l, remarks=remarks,
            fee=consult_fee, pay_mode=pay_mode,
            patient_barcode=patient_barcode,
        )

    def _do_save():
        # ── Edit mode: UPDATE existing visit — never create new ──────────
        _raw_consult_oid = (
            st.session_state.get("_editing_consult_order_id","") or
            st.session_state.get("_erp_order_id","")
        )
        # Guard: never use order_no (CONS-*) as UUID — causes DB cast error
        _edit_consult_oid = _raw_consult_oid if _is_valid_uuid(_raw_consult_oid) else ""
        _edit_visit_id = st.session_state.get("_erp_visit_id","")

        if _edit_consult_oid and len(_edit_consult_oid) > 10:
            try:
                from modules.sql_adapter import run_write as _rw_e, run_query as _rq_e

                # Fetch order details — need order_no to return and party_id for fallback
                _orow = _rq_e(
                    "SELECT order_no, party_id::text AS pid, "
                    "customer_order_no AS stored_visit_id, "
                    "created_at::date::text AS odate "
                    "FROM orders WHERE id=%s::uuid LIMIT 1",
                    (_edit_consult_oid,)
                ) or []
                _od      = _orow[0] if _orow else {}
                _order_no_e  = _od.get("order_no","")
                _pid_e       = _od.get("pid","")
                _odate_e     = _od.get("odate","")

                # Prefer visit_id from DB (customer_order_no) over session state
                # Session state may be empty for old records
                _db_visit_id = str(_od.get("stored_visit_id","") or "")
                _vid_to_use  = (
                    _db_visit_id if _db_visit_id and len(_db_visit_id) > 10
                    else _edit_visit_id if _edit_visit_id and len(_edit_visit_id) > 10
                    else ""
                )

                _rx_params = (
                    float(rx_r_sph or 0), float(rx_r_cyl or 0),
                    int(float(rx_r_axis or 0)), float(rx_r_add or 0),
                    float(rx_l_sph or 0), float(rx_l_cyl or 0),
                    int(float(rx_l_axis or 0)), float(rx_l_add or 0),
                )

                if _vid_to_use:
                    # Verify the visit actually exists before trying to update it
                    _visit_exists = _rq_e(
                        "SELECT id::text FROM patient_visits WHERE id=%s::uuid LIMIT 1",
                        (_vid_to_use,)
                    ) or []
                    if not _visit_exists:
                        # Linked visit was deleted/never created — fall back to latest
                        _latest = _rq_e("""
                            SELECT id::text AS vid FROM patient_visits
                            WHERE patient_id=%s::uuid
                            ORDER BY visit_date DESC, created_at DESC LIMIT 1
                        """, (_pid_e,)) or []
                        if _latest:
                            _vid_to_use = _latest[0]["vid"]
                            # Repair the link so future edits go to correct visit
                            _rw_e(
                                "UPDATE orders SET customer_order_no=%s WHERE id=%s::uuid",
                                (_vid_to_use, _edit_consult_oid)
                            )
                        else:
                            _vid_to_use = ""  # no visits at all — fall through to create

                if _vid_to_use:
                    st.caption(f"🔧 Saving: R={float(rx_r_sph or 0):+.2f} / L={float(rx_l_sph or 0):+.2f} → visit {_vid_to_use[:8]}...")
                    _rw_e("""
                        UPDATE patient_visits
                           SET right_sph=%s, right_cyl=%s, right_axis=%s, right_add=%s,
                               left_sph=%s,  left_cyl=%s,  left_axis=%s,  left_add=%s
                         WHERE id=%s::uuid
                    """, (*_rx_params, _vid_to_use))

                elif _pid_e and _odate_e:
                    # Fallback: most recent visit on the order's date for this patient
                    # Uses ORDER BY created_at DESC so it hits the latest (most relevant)
                    _rw_e("""
                        UPDATE patient_visits
                           SET right_sph=%s, right_cyl=%s, right_axis=%s, right_add=%s,
                               left_sph=%s,  left_cyl=%s,  left_axis=%s,  left_add=%s
                         WHERE id = (
                             SELECT id FROM patient_visits
                              WHERE patient_id=%s::uuid
                                AND visit_date=%s::date
                              ORDER BY created_at DESC LIMIT 1
                         )
                    """, (*_rx_params, _pid_e, _odate_e))

                # Also update order fee if it changed
                _rw_e("""
                    UPDATE orders
                       SET total_value=%s, payment_mode=%s
                     WHERE id=%s::uuid
                """, (float(consult_fee), pay_mode, _edit_consult_oid))

                # Only clear the visit_id lock — keep editing flags
                # so UI stays in edit mode and shows updated values after rerun
                st.session_state.pop("_erp_visit_id", None)
                # Keep _editing_consult_order_id so post-save panel stays visible

                # Return order_no (CONS-...) not UUID
                return _order_no_e or _edit_consult_oid

            except Exception as _ee:
                st.error(f"Update failed: {_ee}")
                return None

        # ── New consultation: INSERT as normal ───────────────────────────
        return _save_consultation(
            pid=pid, name=name, mob=mob,
            fee=consult_fee, pay_mode=pay_mode,
            rx_r=(rx_r_sph, rx_r_cyl, rx_r_axis, rx_r_add),
            rx_l=(rx_l_sph, rx_l_cyl, rx_l_axis, rx_l_add),
            referral=referral_name.strip()
        )

    from modules.utils.submit_guard import guarded_submit, is_locked

    # ── Saved-order marker: once saved for this session, block re-save ────
    # Key uses patient_id + today's date so it auto-resets for a new patient
    # or a new calendar day, but blocks double-click / button-spam same visit.
    _save_key      = f"consult_saved_{pid}_{date.today().isoformat()}"
    _already_saved = bool(st.session_state.get(_save_key))
    _saved_ono     = st.session_state.get(_save_key, "")

    # _in_edit_mode and _edit_order_id defined at top of function
    if _in_edit_mode:
        _already_saved = False  # always allow save in edit mode
        # Pre-fill saved_ono from existing order so post-save panel shows immediately
        if not _saved_ono and _edit_order_id:
            try:
                from modules.sql_adapter import run_query as _rq_eno
                _eno_row = _rq_eno(
                    "SELECT order_no FROM orders WHERE id=%s::uuid LIMIT 1",
                    (_edit_order_id,)
                ) or []
                if _eno_row:
                    _saved_ono = str(_eno_row[0].get("order_no",""))
                    # Also set _save_key so post-save panel renders
                    if _saved_ono:
                        st.session_state[_save_key] = _saved_ono
            except Exception:
                pass

    # ── Helper: build WhatsApp message (FIX 2) ────────────────────────────
    def _wa_consultation_msg(consultation_id: str, fee_amount: float) -> str:
        _store_name  = shop or "Parakh Eye Care"
        _store_phone = phone or ""
        return (
            f"Thanks for Visiting {_store_name} a state of art Optometry Clinic.\n\n"
            f"Your Consultation ID is *{consultation_id}*.\n\n"
            f"We have received your consultation of *Rs {fee_amount:.0f}*.\n\n"
            f"We will be happy if you see our wide range of frames and lenses "
            f"at our Optical Store and avail great offers and discount.\n\n"
            f"Store this number in your mobile to get regular updates"
            + (f": {_store_phone}" if _store_phone else ".")
        )

    b1, b2, b3, b4, b5 = st.columns(5)

    with b1:
        if st.button("💾 Save", key="consult_save",
                     width='stretch',
                     disabled=_already_saved or is_locked("consult_save_action"),
                     help="Save — stay on screen"):
            with guarded_submit("consult_save_action") as _ok:
                if _ok:
                    ono = _do_save()
                    if ono:
                        st.session_state[_save_key] = ono
                        try:
                            from modules.utils.submit_guard import clear_lock
                            clear_lock("consult_save_action")
                        except Exception:
                            pass
                        # Issue 3: set fee lines so retail treats ₹200 as advance
                        _set_consult_billing_state(ono, consult_fee, pay_mode, pid, name, mob)
                        # Invalidate visit cache so history tab shows new visit immediately
                        try:
                            from modules.wa_engine import invalidate_visit_cache
                            invalidate_visit_cache(pid)
                        except Exception:
                            pass
                        # Issue 4: auto-redirect if checkbox ticked
                        if st.session_state.get("consult_add_billing"):
                            st.session_state["_open_retail_after_consult"] = True
                        st.rerun()

    with b2:
        if st.button("💾🖨️ Save & Print", type="primary",
                     key="consult_save_print",
                     width='stretch',
                     disabled=_already_saved or is_locked("consult_save_action"),
                     help="Save then print clinical report"):
            with guarded_submit("consult_save_action") as _ok:
                if _ok:
                    ono = _do_save()
                    if ono:
                        st.session_state[_save_key] = ono
                        try:
                            from modules.utils.submit_guard import clear_lock
                            clear_lock("consult_save_action")
                        except Exception:
                            pass
                        _set_consult_billing_state(ono, consult_fee, pay_mode, pid, name, mob)
                        if st.session_state.get("consult_add_billing"):
                            st.session_state["_open_retail_after_consult"] = True
                        _do_print()
                        st.rerun()

    with b3:
        if _already_saved:
            st.success(f"✅ {_saved_ono}")
            if st.button("🖨️ Print Again", key="consult_print_again",
                         width='stretch'):
                _do_print()
        else:
            if st.button("🖨️ Print Only", key="consult_print_rx",
                         width='stretch',
                         help="Print without saving"):
                _do_print()

    with b4:
        if st.button("📄 Referral", key="consult_referral_btn",
                     width='stretch',
                     help="Print referral letter"):
            if not referral_name.strip():
                st.warning("Enter referral doctor name first")
            else:
                _print_referral_letter(
                    name=name, mobile=mob, date=today,
                    shop=shop, addr=addr, phone=phone,
                    rx_r=(rx_r_sph, rx_r_cyl, rx_r_axis, rx_r_add),
                    rx_l=(rx_l_sph, rx_r_cyl, rx_l_axis, rx_l_add),
                    va_unaided=(va_ur, va_ul), va_aided=(va_ar, va_al),
                    lids=lids, cornea=cornea, lens=lens, fundus=fundus,
                    iop_r=iop_r, iop_l=iop_l,
                    remarks=remarks, referral=referral_name.strip(),
                    referral_reason=referral_reason.strip(),
                )

    with b5:
        if st.button("✅ Close Visit", key="consult_new_patient",
                     width='stretch',
                     help="Clear and start next patient"):
            _reset_consultation()
            st.rerun()

    # ══════════════════════════════════════════════════════════════════════
    # POST-SAVE PANEL — shown only after consultation is saved
    # FIX 1: Payment collection window
    # FIX 2: WhatsApp message
    # FIX 3: Add to billing lines
    # ══════════════════════════════════════════════════════════════════════
    # Show post-save panel in edit mode (pre-existing order) OR after fresh save
    _show_postsave = (_saved_ono and
        (bool(st.session_state.get(_save_key)) or _in_edit_mode))

    if _show_postsave:
        st.markdown("---")

        # ── FIX 1: Payment Collection Window ─────────────────────────────
        # In edit mode — check if fee changed vs what's stored in DB
        _stored_fee = 0.0
        if _in_edit_mode and _edit_order_id:
            try:
                from modules.sql_adapter import run_query as _rq_fee
                _fee_row = _rq_fee(
                    "SELECT COALESCE(total_value,0) AS fee FROM orders WHERE id=%s::uuid LIMIT 1",
                    (_edit_order_id,)
                ) or []
                _stored_fee = float(_fee_row[0].get("fee",0) if _fee_row else 0)
            except Exception:
                pass
            _fee_changed = abs(float(consult_fee) - _stored_fee) > 0.01
            if _fee_changed:
                st.warning(
                    f"⚠️ Fee changed from ₹{_stored_fee:.0f} → ₹{consult_fee:.0f}. "
                    f"Save to update the order.",
                    icon="⚠️"
                )

        st.markdown(
            "<div style='background:#0d2818;border:1px solid #10b981;"
            "border-radius:8px;padding:14px 16px;margin-bottom:12px'>"
            "<div style='color:#10b981;font-weight:700;font-size:0.95rem;margin-bottom:4px'>"
            "💰 Payment Collection</div>"
            "<div style='color:#6ee7b7;font-size:0.8rem'>"
            f"Consultation {_saved_ono} · Collect and close this visit</div>"
            "</div>",
            unsafe_allow_html=True,
        )

        _coll_done_key = f"consult_coll_done_{_saved_ono}"
        _coll_done     = bool(st.session_state.get(_coll_done_key))

        if _coll_done:
            # Payment done — show only the success line, no editable fields
            st.success(
                f"✅ ₹{st.session_state.get('consult_collect_amount', consult_fee):.0f} "
                f"collected via {st.session_state.get('consult_collect_mode', pay_mode)}"
            )
        else:
            # Not yet collected — show input fields
            _pc1, _pc2, _pc3 = st.columns([1, 1, 1.5])
            with _pc1:
                _coll_amount = st.number_input(
                    "Amount received ₹",
                    min_value=0.0,
                    value=float(consult_fee),
                    step=10.0,
                    key="consult_collect_amount",
                )
            with _pc2:
                _coll_mode = st.selectbox(
                    "Payment mode",
                    ["Cash", "UPI", "Card", "Free", "Insurance"],
                    index=["Cash","UPI","Card","Free","Insurance"].index(pay_mode)
                           if pay_mode in ["Cash","UPI","Card","Free","Insurance"] else 0,
                    key="consult_collect_mode",
                )
            with _pc3:
                _coll_ref = st.text_input(
                    "Reference / UPI ID (optional)",
                    placeholder="UPI txn ID or cheque no",
                    key="consult_collect_ref",
                )

            _cp1, _cp2 = st.columns(2)
            with _cp1:
                if st.button("✅ Mark Payment Collected",
                             key="consult_collect_btn",
                             type="primary",
                             use_container_width=True):
                    try:
                        from modules.sql_adapter import run_write as _rw_pay, run_query as _rq_pay
                        import uuid as _uuid_pay
                        _pay_id  = str(_uuid_pay.uuid4())
                        _pay_no  = f"CPR-{_saved_ono}"
                        _ord_row = _rq_pay(
                            "SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
                            (_saved_ono,)
                        ) or []
                        _ord_id  = _ord_row[0]["id"] if _ord_row else None
                        if _ord_id:
                            _rw_pay("""
                                INSERT INTO payments
                                    (id, payment_no, payment_date, payment_mode,
                                     amount, reference_no, remarks,
                                     order_id, advance_for_order_id,
                                     payment_type, created_at)
                                VALUES
                                    (%s::uuid, %s, CURRENT_DATE, %s,
                                     %s, %s, %s,
                                     %s::uuid, %s::uuid,
                                     'ADVANCE', NOW())
                                ON CONFLICT DO NOTHING
                            """, (
                                _pay_id, _pay_no, _coll_mode,
                                float(_coll_amount),
                                _coll_ref or None,
                                f"Consultation fee — {name}",
                                _ord_id, _ord_id,
                            ))
                        st.session_state[_coll_done_key] = True
                        st.rerun()
                    except Exception as _pe:
                        st.error(f"Payment record failed: {_pe}. Mark manually.")
                        st.session_state[_coll_done_key] = True
                        st.rerun()

        # ── FIX 2: WhatsApp Message — always shown, own row ──────────────
        st.markdown("")
        _mob_key      = "consult_wa_mobile_display"
        _mob_edit_key = f"consult_wa_mob_edited_{_saved_ono}"

        # Seed ONLY if: key missing, OR empty, OR not yet edited by user
        if not st.session_state.get(_mob_edit_key):
            _best_mob = mob or ""
            try:
                from modules.wa_engine import resolve_mobile
                _best_mob = resolve_mobile(patient_id=pid, fallback=mob) or mob or ""
            except Exception:
                pass
            if _mob_key not in st.session_state or not st.session_state.get(_mob_key):
                st.session_state[_mob_key] = _best_mob

        _wa_c1, _wa_c2 = st.columns([1.5, 1])
        with _wa_c1:
            _prev_mob = st.session_state.get(_mob_key, "")
            _wa_mob_display = st.text_input(
                "Mobile",
                key=_mob_key,
                help="Edit if incorrect before sending WhatsApp",
            )
            if _wa_mob_display != _prev_mob:
                st.session_state[_mob_edit_key] = True

        with _wa_c2:
            # Fix 5: robust Indian mobile sanitization
            _wa_mob = "".join(x for x in (_wa_mob_display or "") if x.isdigit())
            if _wa_mob.startswith("91") and len(_wa_mob) == 12:
                _wa_mob = _wa_mob[2:]
            elif _wa_mob.startswith("0") and len(_wa_mob) == 11:
                _wa_mob = _wa_mob[1:]
            elif _wa_mob.startswith("091") and len(_wa_mob) == 13:
                _wa_mob = _wa_mob[3:]
            # Only valid 10-digit Indian mobile (starts 6/7/8/9)
            _wa_mob = ("91" + _wa_mob) if (len(_wa_mob) == 10 and _wa_mob[0] in "6789") else ""

            if _wa_mob:
                import urllib.parse as _uparse
                _wa_msg = _wa_consultation_msg(_saved_ono, consult_fee)
                _wa_url = f"https://wa.me/{_wa_mob}?text={_uparse.quote(_wa_msg)}"
                st.markdown("")  # vertical alignment spacer
                st.link_button("📲 Send WhatsApp", _wa_url, use_container_width=True)
            else:
                st.caption("No valid mobile — WhatsApp unavailable")

        # ── FIX 4: Add to Billing — auto-redirect, no extra button ───────
        if _add_to_billing:
            st.markdown("---")
            st.info(
                "🛍️ **Add to Billing** is ticked. "
                "Consultation fee ₹{:.0f} will be treated as advance. "
                "Opening Retail Order now...".format(consult_fee)
            )
            # Auto-redirect: set all state and navigate immediately
            _conv_key = f"consult_converted_{_saved_ono}"
            if not st.session_state.get(_conv_key):
                st.session_state[_conv_key] = True
                _redir_key = f"consult_redir_{_saved_ono}"
                # Fix 4: guard double-redirect on rerun
                if not st.session_state.get(_redir_key):
                    st.session_state[_redir_key] = True
                    try:
                        from modules.consultation import convert_consultation_to_billing
                        _result = convert_consultation_to_billing(_saved_ono)
                        if _result and "error" not in _result:
                                st.session_state["_order_edit_prefill"]          = _result
                                st.session_state["_erp_mode"]                    = "CONSULT_BILLING"
                                # Store UUID in _erp_order_id (not order_no) to avoid
                                # "invalid input syntax for type uuid" errors in edit flow
                                _erp_uuid_lookup = ""
                                try:
                                    from modules.sql_adapter import run_query as _rq_eid
                                    _eid_rows = _rq_eid(
                                        "SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
                                        (_saved_ono,)
                                    ) or []
                                    if _eid_rows: _erp_uuid_lookup = _eid_rows[0]["id"]
                                except Exception: pass
                                # Never store order_no as _erp_order_id — it breaks UUID casts
                                st.session_state["_erp_order_id"] = _erp_uuid_lookup or ""
                                st.session_state["_open_retail_after_consult"]   = False
                                st.session_state["_consult_fee_lines_consumed"]  = True
                                # Force full billing mode — clear all consultation flags
                                st.session_state["_visit_mode_default"]          = 0
                                st.session_state.pop("retail_visit_mode", None)
                                st.session_state.pop("_editing_consult_order_id", None)
                                st.session_state.pop("_force_consultation_tab", None)
                                st.session_state["_sidebar_page"]                = "🛍️  Retail Order"
                                st.rerun()
                        elif _result and "error" in _result:
                            st.error(_result["error"])
                    except Exception as _be:
                        st.error(f"Billing open failed: {_be}")
            else:
                st.success("✅ Billing opened — switch to Retail Order tab")


def _set_consult_billing_state(
    order_no: str, fee: float, pay_mode: str,
    pid: str, name: str, mob: str
) -> None:
    """
    FIX 3 + FIX 5: After consultation save, bridge the billing system.

    Sets two session_state keys retail_punching reads:
      _consult_fee_lines        — fee line so retail shows ₹200 as advance
      _retail_consult_source_id — links the consultation order

    When patient converts to full billing:
      - ₹200 consultation fee is pre-loaded as an advance
      - If receipt already generated → treated as advance against new order
      - If not → added to billing lines as SERVICE item
    """
    import streamlit as _st
    _st.session_state["_consult_fee_lines"] = [{
        "product_name":    "Consultation Fee",
        "quantity":        1,
        "unit_price":      float(fee),
        "total_price":     float(fee),
        "eye_side":        "SERVICE",
        "is_service_line": True,
        "payment_mode":    pay_mode,
    }]
    _st.session_state["_retail_consult_source_id"] = order_no
    # Also set the prefill so retail knows patient context
    _st.session_state["_consult_prefill"] = {
        "patient_id":       pid or "",
        "patient_name":     name or "",
        "patient_mobile":   mob or "",
        "mobile":           mob or "",
        "consult_order_id": order_no,
        "consult_fee":      float(fee),
        "order_lines":      [],
    }


def _save_consultation(pid, name, mob, fee, pay_mode, rx_r, rx_l, referral=""):
    """
    Save consultation as order AND create a patient_visit record with Rx.

    Links the order to the visit via:
      orders.party_id          = patient UUID
      orders.customer_order_no = visit UUID  ← used by convert_consultation_to_billing
                                               and clinical viewer for exact Rx lookup
    Returns order_no or None.
    """
    try:
        from modules.sql_adapter import run_write, run_query
        import uuid

        order_id = str(uuid.uuid4())
        order_no = f"CONS-{date.today().strftime('%Y%m%d')}-{order_id[:6].upper()}"
        visit_id = str(uuid.uuid4())

        # ── Unpack Rx tuples ─────────────────────────────────────────────
        def _safe_float(v):
            try: return float(v) if v not in (None, "", "None") else None
            except: return None
        def _safe_int(v):
            try: return int(float(v)) if v not in (None, "", "None", "0") else None
            except: return None

        r_sph, r_cyl, r_axis, r_add = rx_r
        l_sph, l_cyl, l_axis, l_add = rx_l
        r_sph_f  = _safe_float(r_sph);  r_cyl_f  = _safe_float(r_cyl)
        r_axis_i = _safe_int(r_axis);   r_add_f  = _safe_float(r_add)
        l_sph_f  = _safe_float(l_sph);  l_cyl_f  = _safe_float(l_cyl)
        l_axis_i = _safe_int(l_axis);   l_add_f  = _safe_float(l_add)

        # ── Normalise pid to plain Python string ──────────────────────────
        # retail_patient_id may arrive as: str UUID, uuid.UUID object,
        # pandas UUID, or numpy type — force to str and strip.
        try:
            _pid_str = str(pid).strip() if pid is not None else ""
        except Exception:
            _pid_str = ""

        # Reject blank, "None", "nan", temp IDs, and short values
        _pid_is_valid = (
            bool(_pid_str)
            and _pid_str.lower() not in ("none", "nan", "nat", "")
            and not _pid_str.upper().startswith("TEMP-")
            and len(_pid_str) >= 10   # UUID = 36 chars; be lenient for safety
        )

        # Use the normalised string for all DB calls
        pid = _pid_str if _pid_is_valid else None

        if not _pid_is_valid:
            st.warning(f"⚠️ Patient ID invalid (value: `{_pid_str}`) — visit not linked. Order will still save.")

        # ── 0a. If name is blank but pid is valid — fetch from DB ─────────
        # Happens when industrial_reset wipes retail_patient_name before save
        if _pid_is_valid and not name:
            try:
                _pname_row = run_query(
                    "SELECT master_name FROM patients WHERE id = %s::uuid LIMIT 1",
                    (pid,)
                ) or []
                if _pname_row:
                    name = str(_pname_row[0].get("master_name","") or "")
            except Exception:
                pass

        # ── 0. Fetch patient's real Case ID (record_no) from patients table ──
        # record_no in patient_visits MUST match patients.record_no — it's the
        # Case ID that Case ID search, visit history, and backoffice all use.
        patient_record_no = None
        if _pid_is_valid:
            try:
                _pt = run_query(
                    "SELECT record_no FROM patients WHERE id = %s::uuid LIMIT 1",
                    (pid,)
                ) or []
                patient_record_no = (_pt[0]["record_no"] or None) if _pt else None
            except Exception:
                pass

        # ── 1. Create OR UPDATE patient_visit with today's Rx ───────────────
        # Uses UPSERT pattern: if a visit already exists for this patient today,
        # UPDATE it. Never creates duplicate visits on re-save.
        visit_saved = False
        if _pid_is_valid:
            try:
                # Check if a visit already exists for this patient today
                _existing_visit = run_query("""
                    SELECT id::text AS vid FROM patient_visits
                    WHERE patient_id = %s::uuid
                      AND visit_date = CURRENT_DATE
                      AND visit_name = 'Consultation'
                    ORDER BY created_at DESC LIMIT 1
                """, (pid,)) or []

                if _existing_visit:
                    # UPDATE existing visit instead of inserting new
                    visit_id = _existing_visit[0]["vid"]
                    run_write("""
                        UPDATE patient_visits
                           SET right_sph=%s, right_cyl=%s, right_axis=%s, right_add=%s,
                               left_sph=%s,  left_cyl=%s,  left_axis=%s,  left_add=%s
                         WHERE id=%s::uuid
                    """, (
                        r_sph_f, r_cyl_f, r_axis_i, r_add_f,
                        l_sph_f, l_cyl_f, l_axis_i, l_add_f,
                        visit_id,
                    ))
                else:
                    # INSERT new visit
                    run_write("""
                        INSERT INTO patient_visits (
                            id, patient_id, record_no, visit_date, visit_name,
                            right_sph, right_cyl, right_axis, right_add,
                            left_sph,  left_cyl,  left_axis,  left_add,
                            created_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s, CURRENT_DATE, 'Consultation',
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            NOW()
                        )
                    """, (
                        visit_id, pid,
                        patient_record_no,
                        r_sph_f, r_cyl_f, r_axis_i, r_add_f,
                        l_sph_f, l_cyl_f, l_axis_i, l_add_f,
                    ))
                visit_saved = True
            except Exception as _ve:
                import logging as _log
                _log.getLogger(__name__).warning(
                    f"[CONSULTATION] patient_visits insert failed (pid={pid}): {_ve}"
                )
                st.warning(f"⚠️ Visit record not saved: {_ve}")
                visit_id = ""   # don't link a failed visit
        else:
            visit_id = ""       # temp/anonymous patient — no visit record

        # ── 2. Get next display order number ─────────────────────────────
        try:
            _seq = run_query("SELECT nextval('orders_display_seq') AS n") or []
            _disp_no = int(_seq[0]["n"]) if _seq else None
        except Exception:
            _disp_no = None

        _disp_col = ", display_order_no" if _disp_no else ""
        _disp_val = f", {_disp_no}" if _disp_no else ""

        # ── 3. Save order ─────────────────────────────────────────────────
        # Use NULL for party_id / customer_order_no if values are missing/invalid
        # to avoid ::uuid cast failure on empty strings.
        _party_col  = ", party_id"          if _pid_is_valid else ""
        _party_val  = ", %s::uuid"           if _pid_is_valid else ""
        _visit_col  = ", customer_order_no"  if visit_saved   else ""
        _visit_val  = ", %s"                 if visit_saved   else ""  # TEXT column — no ::uuid cast

        _extra_params = []
        if _pid_is_valid:
            _extra_params.append(pid)
        if visit_saved:
            _extra_params.append(visit_id)

        # ── Guard: check if consultation already saved for this patient today ──
        # Prevents duplicates on double-click or rerun race condition
        if _pid_is_valid:
            _existing = run_query("""
                SELECT order_no FROM orders
                WHERE party_id = %s::uuid
                  AND order_type = 'CONSULTATION'
                  AND DATE(created_at) = CURRENT_DATE
                ORDER BY created_at DESC LIMIT 1
            """, (pid,)) or []
            if _existing:
                _existing_ono = _existing[0]["order_no"]
                # If name or fee was blank on the existing record — patch it now
                if name or float(fee) > 0:
                    run_write("""
                        UPDATE orders
                           SET party_name   = COALESCE(NULLIF(party_name,''),   %s),
                               patient_name = COALESCE(NULLIF(patient_name,''), %s),
                               patient_mobile = COALESCE(NULLIF(patient_mobile,''), %s),
                               total_value  = CASE WHEN total_value = 0 THEN %s ELSE total_value END,
                               payment_mode = COALESCE(NULLIF(payment_mode,''), %s)
                         WHERE order_no = %s
                           AND order_type = 'CONSULTATION'
                    """, (name, name, mob, float(fee), pay_mode, _existing_ono))
                return _existing_ono  # already saved — return existing

        run_write(f"""
            INSERT INTO orders (
                id, order_no, order_type, order_source, status,
                party_name, patient_name, patient_mobile,
                total_items, total_value, payment_mode,
                created_at{_party_col}{_visit_col}{_disp_col}
            ) VALUES (
                %s::uuid, %s, 'CONSULTATION', 'RETAIL', 'CLOSED',
                %s, %s, %s,
                0, %s, %s,
                NOW(){_party_val}{_visit_val}{_disp_val}
            )
            ON CONFLICT (id) DO NOTHING
        """, (
            order_id, order_no,
            name, name, mob,
            float(fee), pay_mode,
            *_extra_params,
        ))

        # ── 4. Save consultation fee as a SERVICE order_line ──────────────
        # This makes the fee visible in billing/challan/invoice exactly like
        # a product line — eye_side=SERVICE, gst_percent=0, no allocation needed.
        if float(fee) > 0:
            try:
                # Find or create the "Consultation Fee" service product
                _fp = run_query("""
                    SELECT id::text FROM products
                    WHERE LOWER(product_name) LIKE '%consultation%'
                      AND COALESCE(is_active,true)=true
                    ORDER BY created_at LIMIT 1
                """) or []
                if _fp:
                    _fee_prod_id = _fp[0]["id"]
                else:
                    _fee_prod_id = str(uuid.uuid4())
                    run_write("""
                        INSERT INTO products
                            (id, product_name, main_group, category,
                             unit, gst_percent, is_active, created_at)
                        VALUES (%s::uuid, 'Consultation Fee', 'Services', 'Services',
                                'SERVICE', 0, true, NOW())
                        ON CONFLICT DO NOTHING
                    """, (_fee_prod_id,))

                _line_id = str(uuid.uuid4())
                run_write("""
                    INSERT INTO order_lines (
                        id, order_id, product_id,
                        eye_side, quantity,
                        unit_price, total_price,
                        billing_qty, billing_total, allocated_qty,
                        gst_percent, gst_amount,
                        status, is_service_line,
                        created_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid,
                        'SERVICE', 1,
                        %s, %s,
                        1, %s, 1,
                        0, 0,
                        'READY', TRUE,
                        NOW()
                    )
                    ON CONFLICT (id) DO NOTHING
                """, (
                    _line_id, order_id, _fee_prod_id,
                    float(fee), float(fee), float(fee),   # unit_price, total_price, billing_total
                ))
            except Exception as _le:
                import logging as _log
                _log.getLogger(__name__).warning(
                    f"[CONSULTATION] fee order_line insert failed: {_le}"
                )
                # Non-critical — order still saves, fee visible in total_value

        # ── Auto-insert advance payment for consultation fee ────────────
        # Creates payment record immediately on save — staff doesn't need
        # to click a separate button. Idempotent: skips if already exists.
        if float(fee) > 0:
            try:
                _pay_id  = str(uuid.uuid4())
                _pay_no  = f"CPR-{order_no}"
                run_write("""
                    INSERT INTO payments (
                        id, payment_no, payment_date, payment_mode,
                        amount, reference_no, remarks,
                        order_id, advance_for_order_id,
                        payment_type, created_at
                    ) VALUES (
                        %s::uuid, %s, CURRENT_DATE, %s,
                        %s, %s, %s,
                        %s::uuid, %s::uuid,
                        'ADVANCE', NOW()
                    )
                    ON CONFLICT DO NOTHING
                """, (
                    _pay_id, _pay_no, pay_mode,
                    float(fee), f"CONS-{order_id[:8].upper()}",
                    f"Consultation fee — {name}",
                    order_id, order_id,
                ))
            except Exception as _pe:
                import logging as _log
                _log.getLogger(__name__).warning(
                    f"[CONSULTATION] auto-advance payment failed: {_pe}"
                )
                # Non-critical — order saved, payment can be recorded manually

        return order_no

    except Exception as ex:
        st.error(f"Save failed: {ex}")
        return None


def _reset_consultation():
    """Clear patient and clinical state for next patient."""
    keys = [
        "retail_patient_id", "retail_patient_name", "retail_patient_mobile",
        "retail_case_no", "retail_selected_case_record_no", "retail_case_visits",
        "retail_right_sph", "retail_right_cyl", "retail_right_axis", "retail_right_add",
        "retail_left_sph", "retail_left_cyl", "retail_left_axis", "retail_left_add",
        "retail_clinical_exam", "clinical_exam_saved",
        "consult_wa_mobile_display",   # clear so next patient's mobile seeds fresh
    ]
    for k in keys:
        if k in st.session_state:
            del st.session_state[k]
    # Clear all consult_saved_* and consult_wa_mob_edited_* markers
    for k in list(st.session_state.keys()):
        if k.startswith("consult_saved_") or k.startswith("consult_wa_mob_edited_"):
            del st.session_state[k]
    # Release any lingering save lock
    try:
        from modules.utils.submit_guard import clear_lock
        clear_lock("consult_save_action")
    except Exception:
        pass


def _rx_row(eye, sph, cyl, axis, add, color):
    _add = f" ADD {_fmt(add)}" if _fmt(add) != '—' else ''
    return (
        f"<tr style='background:{color}'>"
        f"<td style='padding:4px 8px;font-weight:700'>{eye}</td>"
        f"<td style='padding:4px 8px;text-align:center'>{_fmt(sph)}</td>"
        f"<td style='padding:4px 8px;text-align:center'>{_fmt(cyl)}</td>"
        f"<td style='padding:4px 8px;text-align:center'>{_fax(axis)}</td>"
        f"<td style='padding:4px 8px;text-align:center'>{_fmt(add) if _fmt(add) != '—' else '—'}</td>"
        f"</tr>"
    )


def _print_clinical_report(**kw):
    shop = kw['shop']; addr = kw['addr']; phone = kw['phone']
    name = kw['name']; mob  = kw['mobile']; dt = kw['date']
    rs,rc,ra,rad = kw['rx_r']; ls,lc,la,lad = kw['rx_l']
    va_ur,va_ul = kw['va_unaided']; va_ar,va_al = kw['va_aided']
    va_nr,va_nl = kw['va_near']
    fee = kw.get('fee',0); pay = kw.get('pay_mode','Cash')
    patient_barcode = kw.get('patient_barcode','')
    try:
        from modules.printing.patient_card_printer import barcode_svg as _bsvg
        _bc_svg = _bsvg(patient_barcode, 180, 40) if patient_barcode else ''
    except:
        _bc_svg = ''
    _bc_section = (
        f"<div style='margin-top:10px;text-align:center'>{_bc_svg}"
        f"<div style='font-family:monospace;font-size:9px;color:#64748b'>{patient_barcode}</div></div>"
    ) if _bc_svg else ''
    footer_txt = kw.get('footer','This prescription is valid for one year from the date of examination.')

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:Arial,sans-serif;padding:12mm;font-size:11px;color:#111}}
    .hdr{{background:#1e3a5f;color:#fff;padding:10px 14px;margin-bottom:12px;
          display:flex;justify-content:space-between;align-items:flex-start}}
    .shop-name{{font-size:18px;font-weight:900}}
    .shop-sub{{font-size:10px;opacity:.85;margin-top:2px}}
    .doc-type{{font-size:14px;font-weight:700;text-align:right}}
    .patient-row{{display:flex;justify-content:space-between;
                  background:#f8fafc;padding:8px 10px;border-radius:4px;margin-bottom:10px}}
    table{{width:100%;border-collapse:collapse;margin:6px 0}}
    th{{background:#1e3a5f;color:#fff;padding:5px 8px;font-size:10px;text-align:center}}
    th:first-child{{text-align:left}}
    td{{border:0.5px solid #e2e8f0;font-size:11px}}
    .section{{font-size:11px;font-weight:700;color:#1e3a5f;margin:10px 0 4px;
              border-bottom:1px solid #e2e8f0;padding-bottom:2px}}
    .finding-row{{display:flex;gap:20px;flex-wrap:wrap;font-size:10px;margin:4px 0}}
    .finding-item{{color:#64748b}}
    .finding-item b{{color:#111}}
    .fee-box{{background:#f0fdf4;border:1px solid #86efac;border-radius:4px;
              padding:6px 10px;margin-top:10px;font-size:11px}}
    .sig-row{{display:flex;justify-content:space-between;margin-top:16px}}
    .sig-line{{border-top:0.5px solid #111;width:140px;text-align:center;
               padding-top:4px;font-size:9px;color:#64748b}}
    .footer{{border-top:1px dashed #cbd5e1;margin-top:12px;padding-top:6px;
             font-size:9px;color:#94a3b8;text-align:center}}
    @media print{{@page{{size:A5;margin:6mm}}body{{padding:0}}}}
    </style></head><body>

    <div class="hdr">
      <div>
        <div class="shop-name">{shop.upper()}</div>
        <div class="shop-sub">{addr}{' · ' + phone if phone else ''}</div>
      </div>
      <div>
        <div class="doc-type">CLINICAL PRESCRIPTION</div>
        <div style="font-size:10px;opacity:.8;text-align:right">{dt}</div>
      </div>
    </div>

    <div class="patient-row">
      <div><b>{name}</b> &nbsp; <span style="color:#64748b;font-size:10px">{mob}</span></div>
      <div style="text-align:right">
        <div style="font-size:10px;color:#64748b">Date: {dt}</div>
        {_bc_section}
      </div>
    </div>

    <div class="section">Refraction (Spectacle Prescription)</div>
    <table>
      <tr>
        <th style="text-align:left">Eye</th>
        <th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th>
      </tr>
      {_rx_row('Right (OD)', rs, rc, ra, rad, '#eff6ff')}
      {_rx_row('Left (OS)', ls, lc, la, lad, '#f0fdf4')}
    </table>

    <div class="section">Visual Acuity</div>
    <table>
      <tr><th style="text-align:left">Eye</th><th>Unaided</th><th>Best Corrected</th><th>Near</th></tr>
      <tr style="background:#eff6ff">
        <td style="padding:4px 8px;font-weight:700">Right</td>
        <td style="padding:4px 8px;text-align:center">{va_ur or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{va_ar or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{va_nr or '—'}</td>
      </tr>
      <tr style="background:#f0fdf4">
        <td style="padding:4px 8px;font-weight:700">Left</td>
        <td style="padding:4px 8px;text-align:center">{va_ul or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{va_al or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{va_nl or '—'}</td>
      </tr>
    </table>

    {'<div class="section">Clinical Findings</div><div class="finding-row">'
     + (''.join([
         f'<div class="finding-item"><b>Lids:</b> {kw["lids"]}</div>' if kw.get('lids') else '',
         f'<div class="finding-item"><b>Cornea:</b> {kw["cornea"]}</div>' if kw.get('cornea') else '',
         f'<div class="finding-item"><b>Lens:</b> {kw["lens"]}</div>' if kw.get('lens') else '',
         f'<div class="finding-item"><b>IOP R:</b> {kw["iop_r"]} &nbsp; <b>IOP L:</b> {kw["iop_l"]}</div>' if kw.get('iop_r') else '',
     ]))
     + '</div>'
     + (f'<div style="font-size:10px;margin-top:4px"><b>Fundus:</b> {kw["fundus"]}</div>' if kw.get('fundus') else '')
     + (f'<div style="font-size:10px;margin-top:4px"><b>Remarks:</b> {kw["remarks"]}</div>' if kw.get('remarks') else '')
     if any([kw.get('lids'),kw.get('cornea'),kw.get('lens'),kw.get('fundus'),kw.get('remarks')]) else ''}

    <div class="fee-box">
      Consultation fee: <b>₹{fee:.0f}</b> &nbsp; Mode: {pay}
    </div>

    <div class="sig-row">
      <div></div>
      <div class="sig-line">Optometrist / Doctor</div>
    </div>

    <div class="footer">
      {footer_txt}<br>
      {shop} · {addr}
    </div>

    <script>window.onload=function(){{window.print()}}</script>
    </body></html>"""

    _save_path = kw.get('_save_path')
    if _save_path:
        # Just save the file, don't open browser
        import os
        with open(_save_path, "w", encoding="utf-8") as _spf:
            _spf.write(html)
    else:
        _open_print_tab(html)


def _print_referral_letter(**kw):
    shop = kw['shop']; addr = kw['addr']; phone = kw['phone']
    name = kw['name']; mob  = kw['mobile']; dt = kw['date']
    rs,rc,ra,rad = kw['rx_r']; ls,lc,la,lad = kw['rx_l']
    va_ur,va_ul  = kw['va_unaided']; va_ar,va_al = kw['va_aided']
    ref           = kw.get('referral', 'Specialist')
    ref_reason    = kw.get('referral_reason', '').strip()
    iop_r         = kw.get('iop_r', ''); iop_l = kw.get('iop_l', '')
    patient_barcode = kw.get('patient_barcode', '')
    footer_txt    = kw.get('footer', 'Please feel free to contact us for additional information.')

    # ── Build findings summary ────────────────────────────────────────────
    def _fmtv(v):
        if not v or str(v).strip() in ('', 'None', 'nan', '0.0', '0', '---'):
            return None
        try:
            f = float(v)
            return f"+{f:.2f}" if f > 0 else f"{f:.2f}"
        except: return str(v).strip() or None

    def _fmtax(v):
        if not v or str(v).strip() in ('', 'None', 'nan', '0', '---'):
            return None
        try:  return str(int(float(v)))
        except: return str(v).strip() or None

    # ── RX table rows ──────────────────────────────────────────────────────
    def _rxrow(eye, sph, cyl, axis, add, bg):
        s=_fmtv(sph) or '—'; c=_fmtv(cyl) or '—'
        a=_fmtax(axis) or '—'; d=_fmtv(add) or '—'
        return (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:5px 8px;font-weight:700'>{eye}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{s}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{c}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{a}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{d}</td>"
            f"</tr>"
        )

    # ── Structured clinical findings ───────────────────────────────────────
    findings = []
    if kw.get('lids')   and kw['lids']   not in ('Normal','WNL','—',''): findings.append(f"<b>Lids:</b> {kw['lids']}")
    if kw.get('cornea') and kw['cornea'] not in ('Normal','WNL','—',''): findings.append(f"<b>Cornea:</b> {kw['cornea']}")
    if kw.get('lens')   and kw['lens']   not in ('Clear','WNL','—',''):  findings.append(f"<b>Lens:</b> {kw['lens']}")
    if kw.get('fundus') and kw['fundus'] not in ('Normal','WNL','—',''): findings.append(f"<b>Fundus:</b> {kw['fundus']}")
    if iop_r or iop_l:
        findings.append(f"<b>IOP:</b> R {iop_r or '—'} / L {iop_l or '—'} mmHg")
    if kw.get('remarks'): findings.append(f"<b>Remarks:</b> {kw['remarks']}")

    findings_html = ""
    if findings:
        findings_html = (
            "<div style='margin:10px 0;padding:8px 12px;background:#fafafa;"
            "border-left:3px solid #1e3a5f;border-radius:3px;font-size:11px;line-height:1.9'>"
            + "<br>".join(findings) +
            "</div>"
        )

    # ── Reason block ───────────────────────────────────────────────────────
    if ref_reason:
        reason_html = (
            f"<div style='margin:12px 0 8px;padding:8px 12px;"
            f"background:#fff7ed;border:1px solid #fed7aa;"
            f"border-radius:4px;font-size:12px'>"
            f"<b style='color:#c2410c'>Reason for Referral:</b><br>"
            f"<span style='font-size:12px'>{ref_reason}</span>"
            f"</div>"
        )
    else:
        reason_html = ""

    # ── VA row ─────────────────────────────────────────────────────────────
    va_r = va_ar or '—'; va_l = va_al or '—'

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Times New Roman',serif;padding:18mm 20mm;font-size:12px;color:#111;line-height:1.7}}
.hdr{{text-align:center;margin-bottom:16px;padding-bottom:10px;border-bottom:3px double #1e3a5f}}
.shop-name{{font-size:22px;font-weight:900;color:#1e3a5f;letter-spacing:.03em}}
.shop-sub{{font-size:10px;color:#475569;margin-top:2px}}
.date-block{{text-align:right;font-size:11px;margin:14px 0 10px}}
.to-block{{margin-bottom:16px;font-size:12px}}
.ref-name{{font-size:14px;font-weight:700;color:#1e3a5f}}
.subject{{font-size:13px;font-weight:700;text-decoration:underline;
          margin:14px 0 10px;color:#1e3a5f}}
.body-text{{font-size:12px;line-height:1.9;margin-bottom:8px}}
table{{border-collapse:collapse;font-size:11px;margin:6px 0}}
th{{background:#1e3a5f;color:#fff;padding:5px 10px;font-size:11px}}
th:first-child{{text-align:left}}
td{{border:0.5px solid #cbd5e1;padding:5px 10px}}
.sig-block{{margin-top:36px}}
.sig-line{{display:inline-block;border-top:0.5px solid #111;
           width:180px;padding-top:5px;font-size:10px;color:#475569;text-align:center}}
.footer{{border-top:1px dashed #cbd5e1;margin-top:18px;padding-top:6px;
         font-size:9px;color:#94a3b8;text-align:center}}
@media print{{@page{{size:A4;margin:12mm}}body{{padding:0}}}}
</style></head><body>

<div class="hdr">
  <div class="shop-name">{shop.upper()}</div>
  <div class="shop-sub">{addr}{' &nbsp;·&nbsp; ' + phone if phone else ''}</div>
</div>

<div class="date-block">{dt}</div>

<div class="to-block">
  To,<br>
  <span class="ref-name">{ref}</span>
</div>

<div class="subject">Re: Referral — {name}</div>

<div class="body-text">
  Dear Doctor,<br><br>
  I am pleased to refer <b>{name}</b> (Mob: {mob}), who presented to our clinic on <b>{dt}</b>.
  I would request your expert evaluation and advice regarding this patient.
</div>

{reason_html}

<div class="body-text" style="margin-top:10px"><b>Clinical Findings &amp; Refraction:</b></div>

<table style="width:60%">
  <tr><th style="text-align:left">Eye</th><th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th></tr>
  {_rxrow('Right (OD)', rs, rc, ra, rad, '#eff6ff')}
  {_rxrow('Left (OS)',  ls, lc, la, lad, '#f0fdf4')}
</table>

<div style="font-size:11px;margin:6px 0">
  <b>Best corrected VA:</b> &nbsp; Right: {va_r} &nbsp;·&nbsp; Left: {va_l}
</div>

{findings_html}

<div class="body-text" style="margin-top:12px">
  Kindly examine and advise further management.
  A copy of your assessment and treatment plan would be greatly appreciated.
</div>

<div class="sig-block">
  Yours sincerely,<br><br><br>
  <div class="sig-line">Optometrist / Doctor<br>{shop}</div>
</div>

<div class="footer">
  {footer_txt}<br>{shop} · {addr}
</div>

<script>window.onload=function(){{window.print()}}</script>
</body></html>"""

    components.html(html, height=750, scrolling=True)




def convert_consultation_to_billing(consult_order_id: str) -> dict:
    """
    Prepare session state to convert a CONSULTATION into a RETAIL billing order.

    Accepts either:
      - orders.id  (UUID like '3f8a2c1d-...')     — preferred
      - orders.order_no (like 'CONS-20260328-D6049A') — auto-resolved to UUID

    Does NOT create a DB order here.
    Instead pre-fills:
      - retail_patient_name / mobile
      - retail_new_rx_r / retail_new_rx_l  (from patient_visits)
      - retail_order_lines with consultation fee as first line (gst=0)
      - retail_consult_source_no (for reference on invoice)

    Staff goes to Retail Punching, sees patient + power pre-loaded,
    adds spectacles to cart, saves once — single order with all lines.
    Consultation fee appears on challan and invoice as first line (GST exempt).
    """
    try:
        from modules.sql_adapter import run_query, run_write
        import uuid, datetime

        # ── Resolve UUID — accept either id or order_no ───────────────────
        _is_uuid = len(consult_order_id) == 36 and consult_order_id.count("-") == 4
        if not _is_uuid:
            # It's an order_no string like CONS-20260328-D6049A — look up the UUID
            _id_row = run_query(
                "SELECT id::text FROM orders WHERE order_no=%s AND order_type='CONSULTATION' LIMIT 1",
                (consult_order_id,)
            ) or []
            if not _id_row:
                return {"error": f"Consultation order not found: {consult_order_id}"}
            consult_order_id = _id_row[0]["id"]

        # Load consultation order
        cons = run_query("""
            SELECT id::text, order_no, patient_name, patient_mobile,
                   COALESCE(total_value,0) AS fee,
                   COALESCE(payment_mode,'Cash') AS payment_mode,
                   created_at::date::text AS cons_date,
                   party_id::text AS patient_id,
                   customer_order_no::text AS visit_id
            FROM orders
            WHERE id = %s::uuid AND order_type='CONSULTATION'
            LIMIT 1
        """, (consult_order_id,)) or []

        if not cons:
            return {"error": "Consultation order not found"}
        c = cons[0]

        # ── Re-billing guard (3-tier) ─────────────────────────────────────
        # Tier 1: exact UUID match via customer_order_no (new orders post-fix)
        # Tier 2: party_id (patient UUID) + same day or after consultation date
        # Tier 3: patient_name + created_at >= cons_date fallback (legacy orders)
        _patient_uuid = c.get("patient_id","")
        _cons_date    = c.get("cons_date","")

        # Tier 0: is_converted flag — ensure column exists, then check
        try:
            run_write("ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_converted BOOLEAN DEFAULT FALSE")
        except Exception:
            pass
        try:
            _conv_check = run_query("""
                SELECT order_no FROM orders
                WHERE id = %s::uuid AND COALESCE(is_converted, false) = true
                LIMIT 1
            """, (consult_order_id,)) or []
            if _conv_check:
                # is_converted=TRUE found. Verify a real retail order exists.
                # If staff clicked Bill but never saved the retail order,
                # is_converted was set prematurely. Reset it and allow re-entry.
                _linked_retail = run_query("""
                    SELECT order_no FROM orders
                    WHERE customer_order_no = %s
                      AND order_type IN ('RETAIL','WHOLESALE')
                      AND COALESCE(is_deleted, false) = false
                    LIMIT 1
                """, (consult_order_id,)) or []
                if _linked_retail:
                    # Genuine conversion: linked retail order exists
                    return {
                        "error":          "Already converted",
                        "already_billed": True,
                        "billed_order_no": _linked_retail[0]["order_no"],
                    }
                else:
                    # Abandoned conversion: reset flag, allow re-entry.
                    # Restore status to CLOSED — consultation orders are always CLOSED,
                    # PENDING would incorrectly surface them in pending-order queues.
                    try:
                        run_write("""
                            UPDATE orders
                            SET is_converted = false, status = 'CLOSED'
                            WHERE id = %s::uuid
                        """, (consult_order_id,))
                    except Exception:
                        pass
                    # Fall through to normal conversion below
        except Exception:
            pass  # column missing, skip; guarded by RETAIL query below

        # Exact UUID link ONLY — the retail order written by retail_punching after
        # a successful save always sets customer_order_no = consultation_order_id.
        # The old Tier-2 (party_id + date range) was a false-positive trap: any
        # retail order for the same patient on the same day or later would block
        # re-entry, showing CONVERTED even when no conversion ever happened.
        _already_billed = run_query("""
            SELECT order_no FROM orders
            WHERE order_type IN ('RETAIL','WHOLESALE')
              AND COALESCE(is_deleted, false) = false
              AND customer_order_no = %(coid)s
            ORDER BY created_at ASC
            LIMIT 1
        """, {
            "coid": consult_order_id,
        }) or []

        if _already_billed:
            return {
                "error":           f"Already billed as {_already_billed[0]['order_no']}",
                "already_billed":  True,
                "billed_order_no": _already_billed[0]["order_no"],
            }

        # ── Load RX: exact visit first (via visit_id stored at save time) ──
        # Fallback: latest visit on/before consultation date by patient_id (UUID)
        # Last resort: name ILIKE match (legacy orders saved before this fix)
        cons_date = c["cons_date"]
        linked_visit_id = c.get("visit_id") or ""
        patient_uuid    = c.get("patient_id") or ""
        rxd = {}

        # ── ALWAYS fetch the most recent visit for today first ──────────────
        # The linked visit (customer_order_no) was created at original save time
        # but doctor may have updated powers since — always use latest visit today
        if patient_uuid and len(patient_uuid) > 10:
            rx = run_query("""
                SELECT
                    COALESCE(right_sph,0)  AS sph_r, COALESCE(right_cyl,0)  AS cyl_r,
                    COALESCE(right_axis,0) AS ax_r,  COALESCE(right_add,0)  AS add_r,
                    COALESCE(left_sph,0)   AS sph_l, COALESCE(left_cyl,0)   AS cyl_l,
                    COALESCE(left_axis,0)  AS ax_l,  COALESCE(left_add,0)   AS add_l,
                    patient_id::text AS patient_id
                FROM patient_visits
                WHERE patient_id = %s::uuid
                  AND visit_date = %s::date
                ORDER BY created_at DESC LIMIT 1
            """, (patient_uuid, cons_date)) or []
            rxd = rx[0] if rx else {}

        # Fallback: exact linked visit (if no visit found for today by UUID)
        if not rxd and linked_visit_id and len(linked_visit_id) > 10:
            rx = run_query("""
                SELECT
                    COALESCE(right_sph,0)  AS sph_r, COALESCE(right_cyl,0)  AS cyl_r,
                    COALESCE(right_axis,0) AS ax_r,  COALESCE(right_add,0)  AS add_r,
                    COALESCE(left_sph,0)   AS sph_l, COALESCE(left_cyl,0)   AS cyl_l,
                    COALESCE(left_axis,0)  AS ax_l,  COALESCE(left_add,0)   AS add_l,
                    id::text AS patient_id
                FROM patient_visits
                WHERE id = %s::uuid
                LIMIT 1
            """, (linked_visit_id,)) or []
            rxd = rx[0] if rx else {}

        if not rxd:
            # ⚠️ LEGACY FALLBACK: name ILIKE + date (old orders without party_id/visit_id)
            rx = run_query("""
                SELECT
                    COALESCE(right_sph,0)  AS sph_r, COALESCE(right_cyl,0)  AS cyl_r,
                    COALESCE(right_axis,0) AS ax_r,  COALESCE(right_add,0)  AS add_r,
                    COALESCE(left_sph,0)   AS sph_l, COALESCE(left_cyl,0)   AS cyl_l,
                    COALESCE(left_axis,0)  AS ax_l,  COALESCE(left_add,0)   AS add_l,
                    p.id::text AS patient_id
                FROM patient_visits pv
                JOIN patients p ON p.id = pv.patient_id
                WHERE p.master_name ILIKE %s
                  AND pv.visit_date::date <= %s::date
                ORDER BY pv.visit_date DESC LIMIT 1
            """, (c["patient_name"], cons_date)) or []
            rxd = rx[0] if rx else {}

        # Resolve patient_id: prefer orders.party_id, fallback to visit join
        resolved_patient_id = (
            patient_uuid or
            rxd.get("patient_id") or
            ""
        )

        # Find or create Consultation Fee product (used as line item)
        fee = float(c.get("fee",0) or 0)
        prod_id = None
        if fee > 0:
            fee_prod = run_query("""
                SELECT id::text, product_name, gst_percent
                FROM products
                WHERE LOWER(product_name) LIKE '%consultation%'
                  AND COALESCE(is_active,true)=true
                ORDER BY created_at LIMIT 1
            """) or []
            if fee_prod:
                prod_id = fee_prod[0]["id"]
                prod_name = fee_prod[0]["product_name"]
            else:
                prod_id = str(uuid.uuid4())
                prod_name = "Consultation Fee"
                try:
                    run_write("""
                        INSERT INTO products
                            (id, product_name, main_group, gst_percent, is_active, created_at)
                        VALUES (%s::uuid, 'Consultation Fee', 'Services', 0, true, NOW())
                        ON CONFLICT DO NOTHING
                    """, (prod_id,))
                except Exception:
                    pass  # already exists

        # NOTE: is_converted is set in retail_punching.py AFTER retail order save.

        return {
            "success": True,
            "patient_name":   c["patient_name"],
            "patient_mobile": c.get("patient_mobile",""),
            "patient_id":     resolved_patient_id,
            "payment_mode":   c["payment_mode"],
            "cons_order_no":  c["order_no"],
            "consult_order_id": consult_order_id,  # returned so retail_punching can mark CONVERTED after save
            "cons_date":      cons_date,
            "consult_fee":    fee,
            "prod_id":        prod_id,
            "prod_name":      prod_name if fee > 0 else "",
            "rx": rxd,
        }

    except Exception as ex:
        return {"error": str(ex)}
