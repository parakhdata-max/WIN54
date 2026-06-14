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
import logging

logger = logging.getLogger(__name__)

try:
    from modules.core.name_formatter import format_person_name
except Exception:
    def format_person_name(name):
        return " ".join(str(name or "").strip().split())


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
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return default


def _fmt(val, d=2):
    if val is None or str(val).strip() in ('', 'None', 'nan', '0.0', '0'):
        return '—'
    try:
        f = float(val)
        return f"+{f:.{d}f}" if f > 0 else f"{f:.{d}f}"
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return str(val)

def _fax(val):
    if val is None or str(val).strip() in ('', 'None', 'nan', '0'):
        return '—'
    try: return str(int(float(val)))
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return str(val)


def render_consultation_close():
    """
    Consultation-only closing panel.
    Shows after clinical exam is filled.
    Handles: consultation charges + print clinical report + print referral + close visit.
    """
    pid  = st.session_state.get("retail_patient_id")
    if not pid:
        return

    def _is_valid_uuid(v):
        v = str(v or "").strip()
        return (
            len(v) == 36
            and v.count("-") == 4
            and not v.upper().startswith(("CONS-", "CS/", "TEMP-"))
        )

    _has_real_patient_id = _is_valid_uuid(pid)

    # ── Ensure patient has a unique barcode ID ─────────────────────────────
    if _has_real_patient_id:
        try:
            from modules.printing.patient_card_printer import (
                ensure_patient_id, render_patient_card_buttons,
                render_patient_id_badge, barcode_svg
            )
            patient_barcode = ensure_patient_id(pid)
        except Exception:
            patient_barcode = str(pid)[:8].upper()
    else:
        patient_barcode = ""

    name = format_person_name(st.session_state.get("retail_patient_name", ""))
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
    _raw_edit_oid = (
        st.session_state.get("_editing_consult_order_id","") or
        st.session_state.get("_erp_order_id","")
    )
    # Validate — never use order_no (CS/*, CONS-*) as a UUID
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
            if (not name or not mob) and _has_real_patient_id:
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
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
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

    # ── Load consultation types from shop master ───────────────────────────
    # shop_master stores consultation_types as JSON list of {name, fee} dicts
    # under the key "consultation_types". Falls back to 4 built-in types.
    _DEFAULT_CONSULT_TYPES = [
        {"name": "Consultation",                "fee": 200},
        {"name": "Special Consultation",        "fee": 400},
        {"name": "Low Vision Consultation",     "fee": 500},
        {"name": "Contact Lens Consultation",   "fee": 300},
    ]
    _consult_types = _DEFAULT_CONSULT_TYPES
    try:
        import json as _json_ct
        _ct_raw = _si.get("consultation_types", "")
        if _ct_raw:
            _parsed = _json_ct.loads(_ct_raw) if isinstance(_ct_raw, str) else _ct_raw
            if isinstance(_parsed, list) and _parsed:
                _consult_types = _parsed
    except Exception:
        pass
    _ct_names   = [t["name"] for t in _consult_types]
    _ct_fee_map = {t["name"]: float(t.get("fee", 0)) for t in _consult_types}

    # In edit mode — load saved consult_type from order's extra_data or notes
    _saved_ct = ""
    if _in_edit_mode and _edit_order_id:
        try:
            from modules.sql_adapter import run_query as _rq_ct
            _ct_row = _rq_ct(
                "SELECT COALESCE(extra_data::json->>'consult_type','') AS ct "
                "FROM orders WHERE id=%s::uuid LIMIT 1",
                (_edit_order_id,)
            ) or []
            _saved_ct = str(_ct_row[0].get("ct","") or "") if _ct_row else ""
        except Exception:
            pass

    _ct_default_idx = _ct_names.index(_saved_ct) if _saved_ct in _ct_names else 0

    _ct_col1, _ct_col2 = st.columns([2, 1])
    with _ct_col1:
        _consult_type = st.selectbox(
            "Consultation type",
            _ct_names,
            index=_ct_default_idx,
            key="consult_type_select",
            help="Type determines default fee. Change fee below if needed.",
        )
    with _ct_col2:
        st.markdown("<div style='padding-top:28px'></div>", unsafe_allow_html=True)
        st.caption(f"Default fee: ₹{_ct_fee_map.get(_consult_type, 0):.0f}")

    # Auto-update default fee when type changes (unless edit mode pre-filled)
    _default_fee = _ct_fee_map.get(_consult_type, float(_si.get("consult_fee_default","200") or "200"))

    # In edit mode — pre-fill fee from existing order in DB (overrides type default)
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
    _fee_context = str(_edit_order_id or "NEW")
    if st.session_state.get("_consult_fee_context") != _fee_context:
        st.session_state[_fee_widget_key] = float(_default_fee or 0)
        st.session_state["_consult_fee_context"] = _fee_context

    cc1, cc3 = st.columns([1, 2])
    with cc1:
        consult_fee = st.number_input(
            "Consultation fee ₹", min_value=0.0, value=_default_fee, step=10.0,
            key="consult_fee"
        )
    with cc3:
        referral_name = st.text_input(
            "Refer to (optional)",
            placeholder="Dr. Sharma, LV Prasad Eye Institute",
            key="consult_referral"
        )
    pay_mode = "Cash"

    # Payment mode is chosen in the post-save receipt panel.
    # Before save we only need the fee amount for the consultation order.
    # _charge_to_billing and _record_payment_now default to False here —
    # the post-save panel raises the receipt or billing switch explicitly.
    _charge_to_billing  = False
    _record_payment_now = False

    # ── Same-day second visit detection ───────────────────────────────────
    # Check if a consultation order already exists for this patient today.
    # If so, warn staff and offer an override to create a fresh visit record.
    _same_day_exists = False
    _same_day_ono    = ""
    if pid and len(str(pid)) > 10 and not _in_edit_mode:
        try:
            from modules.sql_adapter import run_query as _rq_sd
            _sd_row = _rq_sd("""
                SELECT order_no FROM orders
                WHERE party_id = %s::uuid
                  AND order_type = 'CONSULTATION'
                  AND DATE(created_at) = CURRENT_DATE
                  AND COALESCE(is_deleted, FALSE) = FALSE
                ORDER BY created_at DESC LIMIT 1
            """, (str(pid),)) or []
            if _sd_row:
                _same_day_exists = True
                _same_day_ono    = _sd_row[0]["order_no"]
        except Exception:
            pass

    _force_new_visit = False
    if _same_day_exists:
        st.warning(
            f"⚠️ A consultation for this patient already exists today: **{_same_day_ono}**. "
            f"Saving again will update the existing record. "
            f"Tick below to create a separate new visit instead."
        )
        _force_new_visit = st.checkbox(
            "➕ Create new visit (re-examination / follow-up today)",
            value=False,
            key="consult_force_new_visit",
            help="Use when the patient is seen a second time today with different findings.",
        )


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
    conjunctiva = cx.get("sle_conjunctiva","")
    cornea= cx.get("sle_cornea","")
    ac    = cx.get("sle_ac","")
    iris  = cx.get("sle_iris","")
    lens  = cx.get("sle_lens","")
    vitreous = cx.get("sle_vitreous","")
    fundus= cx.get("sle_fundus","")
    iop_r = cx.get("iop_r") or cx.get("iop_right","")
    iop_l = cx.get("iop_l") or cx.get("iop_left","")
    ortho_dist = cx.get("ortho_cover_test_distance","")
    ortho_near = cx.get("ortho_cover_test_near","")
    nystagmus = cx.get("ortho_nystagmus","")
    motility = cx.get("ortho_ocular_motility","")
    convergence = cx.get("ortho_convergence","")
    remarks = cx.get("ortho_remarks","")
    doctor_notes = cx.get("doctor_notes","")
    treatment_plan = cx.get("treatment_plan","")
    followup_advice = cx.get("followup_advice","")

    today = date.today().strftime("%d %b %Y")

    from modules.utils.submit_guard import guarded_submit, is_locked

    # ── Saved-order marker: once saved for this session, block re-save ────
    # Key uses patient_id + today's date so it auto-resets for a new patient
    # or a new calendar day, but blocks double-click / button-spam same visit.
    _save_key      = f"consult_saved_{pid}_{date.today().isoformat()}"
    _already_saved = bool(st.session_state.get(_save_key))
    _saved_ono     = st.session_state.get(_save_key, "")

    # force_new_visit=True: staff explicitly wants a fresh visit record today.
    # Clear the existing save key so the Save button is not blocked, and
    # generate a new unique key so this second save gets its own guard slot.
    if _force_new_visit and _already_saved:
        import time as _time
        _save_key      = f"consult_saved_{pid}_{date.today().isoformat()}_{int(_time.time())}"
        _already_saved = bool(st.session_state.get(_save_key))
        _saved_ono     = st.session_state.get(_save_key, "")

    # _in_edit_mode and _edit_order_id defined at top of function
    if _in_edit_mode:
        _already_saved = False  # always allow save in edit mode
        # Load _saved_ono for use in print/WA/billing buttons, but do NOT set _save_key.
        # Setting _save_key here would make _show_postsave=True immediately on page load
        # before staff has actually clicked Save — causing the "saved without storing" bug.
        # Instead we use a separate _consult_edit_saved flag set only after a real save.
        if not _saved_ono and _edit_order_id:
            try:
                from modules.sql_adapter import run_query as _rq_eno
                _eno_row = _rq_eno(
                    "SELECT order_no FROM orders WHERE id=%s::uuid LIMIT 1",
                    (_edit_order_id,)
                ) or []
                if _eno_row:
                    _saved_ono = str(_eno_row[0].get("order_no",""))
                    # Do NOT set _save_key here — that would open the post-save panel
                    # before any save action. _show_postsave uses _consult_edit_saved instead.
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
            lids=lids, conjunctiva=conjunctiva, cornea=cornea, ac=ac,
            iris=iris, lens=lens, vitreous=vitreous, fundus=fundus,
            iop_r=iop_r, iop_l=iop_l,
            ortho_dist=ortho_dist, ortho_near=ortho_near, nystagmus=nystagmus,
            motility=motility, convergence=convergence, remarks=remarks,
            doctor_notes=doctor_notes, treatment_plan=treatment_plan,
            followup_advice=followup_advice,
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
            referral=referral_name.strip(),
            force_new_visit=_force_new_visit,
            consult_type=_consult_type,
            charge_to_billing=_charge_to_billing,
            record_payment_now=bool(_record_payment_now and not _charge_to_billing),
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
                        if _in_edit_mode:
                            st.session_state["_consult_edit_saved"] = True
                        try:
                            from modules.utils.submit_guard import clear_lock
                            clear_lock("consult_save_action")
                        except Exception:
                            pass
                        # Issue 3: set fee lines so retail treats ₹200 as advance
                        _set_consult_billing_state(ono, consult_fee, pay_mode, pid, name, mob,
                                                   record_payment_now=bool(_record_payment_now and not _charge_to_billing))
                        try:
                            from modules.wa_engine import invalidate_visit_cache
                            invalidate_visit_cache(pid)
                        except Exception:
                            pass
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
                        if _in_edit_mode:
                            st.session_state["_consult_edit_saved"] = True
                        try:
                            from modules.utils.submit_guard import clear_lock
                            clear_lock("consult_save_action")
                        except Exception:
                            pass
                        _set_consult_billing_state(ono, consult_fee, pay_mode, pid, name, mob,
                                                   record_payment_now=bool(_record_payment_now and not _charge_to_billing))
                        _do_print()
                        st.rerun()

    with b3:
        if _already_saved:
            st.markdown(
                f"<div style='background:#052e16;border:1px solid #22c55e;border-radius:6px;"
                f"padding:7px 12px;font-size:0.82rem;color:#4ade80'>"
                f"✅ <b>{_saved_ono}</b> &nbsp;·&nbsp; "
                f"<span style='color:#86efac'>Record saved — not billed</span>"
                f"</div>",
                unsafe_allow_html=True
            )
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
                    rx_l=(rx_l_sph, rx_l_cyl, rx_l_axis, rx_l_add),
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

    # ── Second action row: Receipt print ─────────────────────────────────
    _ra1, _ra2, _ra3 = st.columns([1, 1, 2])
    with _ra1:
        if st.button("🧾 Print Receipt", key="consult_print_receipt",
                     width='stretch',
                     help="Print 80mm consultation receipt with fee + RX summary"):
            _print_consultation_receipt(
                order_no=_saved_ono or "—",
                patient_name=name,
                mobile=mob,
                consult_type=st.session_state.get("consult_type_select", "Consultation"),
                fee=consult_fee,
                pay_mode=pay_mode,
                visit_date=today,
                shop=shop, addr=addr, phone=phone,
                rx_r=(rx_r_sph, rx_r_cyl, rx_r_axis, rx_r_add),
                rx_l=(rx_l_sph, rx_l_cyl, rx_l_axis, rx_l_add),
                shop_upi=_si.get("shop_upi_id", ""),
            )

    # ── Patient ID card print section ────────────────────────────────────
    st.markdown("**Patient ID Card**")
    if _has_real_patient_id:
        try:
            render_patient_card_buttons(
                patient_id=pid, patient_name=name, mobile=mob,
                rx_r={"sph":rx_r_sph,"cyl":rx_r_cyl,"axis":rx_r_axis,"add":rx_r_add},
                rx_l={"sph":rx_l_sph,"cyl":rx_l_cyl,"axis":rx_l_axis,"add":rx_l_add},
                visit_date=today
            )
        except Exception as _pce:
            st.caption(f"Patient card: {_pce}")
    else:
        st.caption("Patient card will be available after linking this consultation to a patient master.")
    st.markdown("---")

    # Patient cleanup/merge and medical-history editing now live in
    # Retail Punching → Patient History, keeping consultation close focused.

    # ── Visit History ──────────────────────────────────────────────────────
    with st.expander("📋 Last 5 Visits", expanded=False):
        if _has_real_patient_id:
            try:
                from modules.sql_adapter import run_query as _rq_hist
                _hist = _rq_hist("""
                    SELECT
                        pv.visit_date,
                        pv.id::text              AS visit_id,
                        o.order_no,
                        COALESCE(pv.right_sph,0)  AS rsph,
                        COALESCE(pv.right_cyl,0)  AS rcyl,
                        COALESCE(pv.right_axis,0) AS raxis,
                        COALESCE(pv.left_sph,0)   AS lsph,
                        COALESCE(pv.left_cyl,0)   AS lcyl,
                        COALESCE(pv.left_axis,0)  AS laxis,
                        o.status,
                        COALESCE(o.total_value, 0) AS total_value,
                        o.order_type
                    FROM patient_visits pv
                    LEFT JOIN orders o
                        ON (o.customer_order_no = pv.id::text
                            OR (o.party_id = pv.patient_id
                                AND o.order_type = 'CONSULTATION'
                                AND o.created_at::date = pv.visit_date))
                        AND COALESCE(o.is_deleted, FALSE) = FALSE
                    WHERE pv.patient_id = %s::uuid
                    ORDER BY pv.visit_date DESC, pv.created_at DESC
                    LIMIT 5
                """, (str(pid),)) or []
                if not _hist:
                    st.caption("No visit history found for this patient")
                else:
                    for _h in _hist:
                        _rx_str = (
                            f"R: {float(_h['rsph']):+.2f}/{float(_h['rcyl']):+.2f}"
                            f"×{int(float(_h['raxis']))}  "
                            f"L: {float(_h['lsph']):+.2f}/{float(_h['lcyl']):+.2f}"
                            f"×{int(float(_h['laxis']))}"
                        )
                        _ono  = _h.get("order_no") or "—"
                        _fee  = float(_h.get("total_value") or 0)
                        _stat = _h.get("status") or ""
                        _otype = _h.get("order_type") or ""
                        _badge = (
                            "🧾 Billed" if _otype in ("RETAIL","WHOLESALE")
                            else "🩺 Consult" if _otype == "CONSULTATION"
                            else ""
                        )
                        # Try to fetch clinical notes for this visit
                        _notes_parts = []
                        _vid = _h.get("visit_id","")
                        if _vid and len(_vid) > 10:
                            try:
                                _cn = _rq_hist("""
                                    SELECT
                                        NULLIF(TRIM(COALESCE(doctor_notes,'')), '')     AS dnotes,
                                        NULLIF(TRIM(COALESCE(treatment_plan,'')), '')   AS tx,
                                        NULLIF(TRIM(COALESCE(followup_advice,'')), '')  AS fu,
                                        NULLIF(TRIM(COALESCE(diagnosis,'')), '')        AS dx
                                    FROM patient_clinicals
                                    WHERE visit_id = %s::uuid
                                    LIMIT 1
                                """, (_vid,)) or []
                                if _cn:
                                    _c = _cn[0]
                                    if _c.get("dx"):     _notes_parts.append(f"Dx: {_c['dx']}")
                                    if _c.get("dnotes"): _notes_parts.append(f"Notes: {_c['dnotes']}")
                                    if _c.get("tx"):     _notes_parts.append(f"Tx: {_c['tx']}")
                                    if _c.get("fu"):     _notes_parts.append(f"F/U: {_c['fu']}")
                            except Exception:
                                pass  # patient_clinicals may not exist for old records
                        _notes_line = "  \n".join(_notes_parts) if _notes_parts else ""
                        st.markdown(
                            f"**{_h['visit_date']}** &nbsp; `{_ono}` &nbsp; {_badge} {_stat}"
                            + (f"  ₹{_fee:.0f}" if _fee else "")
                            + f"  \n`{_rx_str}`"
                            + (f"  \n{_notes_line}" if _notes_line else ""),
                            unsafe_allow_html=False
                        )
                        st.markdown("---")
            except Exception as _he:
                st.caption(f"History unavailable: {_he}")
        else:
            st.caption("No patient selected")

    # ── Patient name / mobile correction ──────────────────────────────────
    # Kept out of the consultation close screen. Patient cleanup/merge belongs
    # in Retail Punching -> Patient History so this panel stays focused.
    if False:
      with st.expander("✏️ Patient details / corrections", expanded=False):
        # Load full patient record for pre-filling extra fields
        _pt_full = {}
        if _has_real_patient_id:
            try:
                from modules.sql_adapter import run_query as _rq_ptfull
                _ptf_rows = _rq_ptfull("""
                    SELECT master_name, mobile,
                           COALESCE(alt_mobile,'')          AS alt_mobile,
                           COALESCE(email,'')               AS email,
                           dob, anniversary_date,
                           COALESCE(occupation,'')          AS occupation,
                           COALESCE(diabetes,FALSE)         AS diabetes,
                           COALESCE(hypertension,FALSE)     AS hypertension,
                           COALESCE(thyroid,FALSE)          AS thyroid,
                           COALESCE(cardiac_history,FALSE)  AS cardiac_history,
                           COALESCE(asthma,FALSE)           AS asthma,
                           COALESCE(drug_allergy,'')        AS drug_allergy,
                           COALESCE(current_medication,'')  AS current_medication,
                           COALESCE(surgery_history,'')     AS surgery_history,
                           COALESCE(family_history,'')      AS family_history,
                           COALESCE(systemic_notes,'')      AS systemic_notes
                    FROM patients WHERE id=%s::uuid LIMIT 1
                """, (str(pid),)) or []
                if _ptf_rows:
                    _pt_full = dict(_ptf_rows[0])
            except Exception:
                pass  # columns may not exist yet — safe, we add them below

        _pf1, _pf2 = st.columns(2)
        with _pf1:
            _corr_name = st.text_input(
                "Full name *", value=_pt_full.get("master_name", name) or name,
                key="consult_corr_name",
                help="Corrects patient master record. All old visits remain linked."
            )
            _corr_mob = st.text_input(
                "Primary mobile *", value=_pt_full.get("mobile", mob) or mob,
                key="consult_corr_mob"
            )
            _corr_alt_mob = st.text_input(
                "Alternate mobile",
                value=_pt_full.get("alt_mobile", "") or "",
                key="consult_corr_alt_mob",
                placeholder="Second contact number"
            )
        with _pf2:
            _corr_email = st.text_input(
                "Email",
                value=_pt_full.get("email", "") or "",
                key="consult_corr_email",
                placeholder="patient@example.com"
            )
            _corr_occupation = st.text_input(
                "Occupation",
                value=_pt_full.get("occupation", "") or "",
                key="consult_corr_occupation",
                placeholder="e.g. Software Engineer, Teacher, Retired"
            )

        _pf3, _pf4 = st.columns(2)
        with _pf3:
            # DOB — stored as date, shown as text for easy entry
            _dob_str = ""
            if _pt_full.get("dob"):
                try: _dob_str = str(_pt_full["dob"])[:10]
                except Exception: pass
            _corr_dob = st.text_input(
                "Date of birth (YYYY-MM-DD)",
                value=_dob_str,
                key="consult_corr_dob",
                placeholder="1990-06-15"
            )
        with _pf4:
            _ann_str = ""
            if _pt_full.get("anniversary_date"):
                try: _ann_str = str(_pt_full["anniversary_date"])[:10]
                except Exception: pass
            _corr_ann = st.text_input(
                "Anniversary date (YYYY-MM-DD)",
                value=_ann_str,
                key="consult_corr_ann",
                placeholder="2015-02-20"
            )

        # ── Medical / Systemic History ─────────────────────────────────────
        st.markdown("**Medical / Systemic History**")
        _cm1, _cm2, _cm3, _cm4, _cm5 = st.columns(5)
        with _cm1:
            _corr_dm  = st.checkbox("Diabetes",
                value=bool(_pt_full.get("diabetes", False)), key="consult_corr_dm")
        with _cm2:
            _corr_htn = st.checkbox("Hypertension",
                value=bool(_pt_full.get("hypertension", False)), key="consult_corr_htn")
        with _cm3:
            _corr_thy = st.checkbox("Thyroid",
                value=bool(_pt_full.get("thyroid", False)), key="consult_corr_thy")
        with _cm4:
            _corr_crd = st.checkbox("Cardiac",
                value=bool(_pt_full.get("cardiac_history", False)), key="consult_corr_crd")
        with _cm5:
            _corr_ast = st.checkbox("Asthma",
                value=bool(_pt_full.get("asthma", False)), key="consult_corr_ast")

        _ct1, _ct2 = st.columns(2)
        with _ct1:
            _corr_allergy = st.text_input(
                "Drug allergy",
                value=_pt_full.get("drug_allergy","") or "",
                key="consult_corr_allergy",
                placeholder="e.g. Penicillin, Sulfa drugs"
            )
            _corr_meds = st.text_area(
                "Current medication",
                value=_pt_full.get("current_medication","") or "",
                key="consult_corr_meds",
                placeholder="Metformin 500mg, Amlodipine 5mg...",
                height=75,
            )
        with _ct2:
            _corr_surg = st.text_area(
                "Surgery history",
                value=_pt_full.get("surgery_history","") or "",
                key="consult_corr_surg",
                placeholder="Cataract surgery 2018 RE...",
                height=75,
            )
            _corr_fam = st.text_input(
                "Family ocular history",
                value=_pt_full.get("family_history","") or "",
                key="consult_corr_fam",
                placeholder="e.g. Glaucoma in father"
            )
        _corr_sysnotes = st.text_area(
            "Other systemic notes",
            value=_pt_full.get("systemic_notes","") or "",
            key="consult_corr_sysnotes",
            placeholder="Any other relevant medical information...",
            height=60,
        )

        if st.button("💾 Save patient details", key="consult_corr_save"):
            _corr_name_clean = format_person_name(_corr_name)
            _corr_mob_clean  = _corr_mob.strip()
            if _corr_name_clean and pid and len(str(pid)) > 10:
                try:
                    from modules.sql_adapter import run_write as _rw_corr, run_query as _rq_corr
                    import json as _json_audit

                    # Ensure extra columns exist (idempotent DDL)
                    for _col_def in [
                        "alt_mobile   TEXT",
                        "email        TEXT",
                        "dob          DATE",
                        "anniversary_date DATE",
                        "occupation   TEXT",
                        "diabetes          BOOLEAN DEFAULT FALSE",
                        "hypertension      BOOLEAN DEFAULT FALSE",
                        "thyroid           BOOLEAN DEFAULT FALSE",
                        "cardiac_history   BOOLEAN DEFAULT FALSE",
                        "asthma            BOOLEAN DEFAULT FALSE",
                        "drug_allergy      TEXT",
                        "current_medication TEXT",
                        "surgery_history   TEXT",
                        "family_history    TEXT",
                        "systemic_notes    TEXT",
                    ]:
                        try:
                            _rw_corr(
                                f"ALTER TABLE patients ADD COLUMN IF NOT EXISTS {_col_def}",
                                ()
                            )
                        except Exception:
                            pass

                    # Fetch old values for audit trail
                    _old_row = _rq_corr(
                        "SELECT master_name, mobile FROM patients WHERE id=%s::uuid LIMIT 1",
                        (str(pid),)
                    ) or []
                    _old_name = _old_row[0].get("master_name","") if _old_row else name
                    _old_mob  = _old_row[0].get("mobile","") if _old_row else mob

                    # Parse dates safely
                    def _parse_date(s):
                        if not s or not s.strip(): return None
                        try:
                            import datetime as _dt_p
                            return _dt_p.date.fromisoformat(s.strip())
                        except Exception:
                            return None

                    _dob_val = _parse_date(_corr_dob)
                    _ann_val = _parse_date(_corr_ann)

                    # Apply all corrections in one UPDATE
                    _rw_corr("""
                        UPDATE patients SET
                            master_name        = %s,
                            mobile             = %s,
                            alt_mobile         = NULLIF(%s,''),
                            email              = NULLIF(%s,''),
                            dob                = %s,
                            anniversary_date   = %s,
                            occupation         = NULLIF(%s,''),
                            diabetes           = %s,
                            hypertension       = %s,
                            thyroid            = %s,
                            cardiac_history    = %s,
                            asthma             = %s,
                            drug_allergy       = NULLIF(%s,''),
                            current_medication = NULLIF(%s,''),
                            surgery_history    = NULLIF(%s,''),
                            family_history     = NULLIF(%s,''),
                            systemic_notes     = NULLIF(%s,'')
                        WHERE id = %s::uuid
                    """, (
                        _corr_name_clean,
                        _corr_mob_clean,
                        _corr_alt_mob.strip(),
                        _corr_email.strip(),
                        _dob_val,
                        _ann_val,
                        _corr_occupation.strip(),
                        bool(_corr_dm),
                        bool(_corr_htn),
                        bool(_corr_thy),
                        bool(_corr_crd),
                        bool(_corr_ast),
                        _corr_allergy.strip(),
                        _corr_meds.strip(),
                        _corr_surg.strip(),
                        _corr_fam.strip(),
                        _corr_sysnotes.strip(),
                        str(pid),
                    ))

                    # Audit log
                    try:
                        _rw_corr("""
                            INSERT INTO system_audit_log
                                (table_name, record_id, action, old_values, new_values,
                                 changed_by, changed_at)
                            VALUES ('patients', %s::uuid, 'PATIENT_DETAILS_UPDATE', %s, %s,
                                    current_user, NOW())
                            ON CONFLICT DO NOTHING
                        """, (
                            str(pid),
                            _json_audit.dumps({"master_name": _old_name, "mobile": _old_mob}),
                            _json_audit.dumps({
                                "master_name": _corr_name_clean, "mobile": _corr_mob_clean,
                                "email": _corr_email.strip(), "occupation": _corr_occupation.strip(),
                            }),
                        ))
                    except Exception:
                        pass

                    st.session_state["retail_patient_name"]   = _corr_name_clean
                    st.session_state["retail_patient_mobile"] = _corr_mob_clean

                    _changed = []
                    if _old_name != _corr_name_clean:
                        _changed.append(f"name: **{_old_name}** → **{_corr_name_clean}**")
                    if _old_mob != _corr_mob_clean:
                        _changed.append(f"mobile: {_old_mob} → {_corr_mob_clean}")
                    _chg_str = " · ".join(_changed) if _changed else "details updated"
                    st.success(
                        f"✅ Saved ({_chg_str}) — all existing records remain linked to the same patient ID"
                    )
                    st.rerun()
                except Exception as _corr_err:
                    st.error(f"Save failed: {_corr_err}")
            elif not _corr_name_clean:
                st.warning("Name cannot be blank")
            else:
                st.warning("No patient selected — search for a patient first")

    # ── helper ────────────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════
    # POST-SAVE PANEL — shown only after consultation is saved
    # FIX 1: Payment collection window
    # FIX 2: WhatsApp message
    # FIX 3: Add to billing lines
    # ══════════════════════════════════════════════════════════════════════
    # Show post-save panel only after a real save action:
    # - Fresh save: _save_key is set in session after _do_save() succeeds
    # - Edit mode: an existing consultation may be opened only to collect
    #   receipt or shift to billing, so show the action panel immediately.
    #   Unsaved RX/detail edits still require the Save button before they
    #   affect DB; this panel reads the already-saved consultation row.
    _show_postsave = (_saved_ono and (
        bool(st.session_state.get(_save_key)) or
        _in_edit_mode
    ))

    if _show_postsave:
        st.markdown("---")

        # ── Payment action — two big buttons shown after save ──────────────
        # Check if payment already recorded in DB for this consultation
        _pay_already_recorded = False
        _pay_recorded_amount  = 0.0
        _pay_recorded_mode    = pay_mode
        _pay_recorded_ref     = ""
        _coll_done_key = f"consult_coll_done_{_saved_ono}"
        _coll_done     = bool(st.session_state.get(_coll_done_key))
        _ctb_flag      = False   # charge_to_billing flag from saved order

        if _saved_ono:
            try:
                from modules.sql_adapter import run_query as _rq_paycheck
                # Check charge_to_billing flag
                _ctb_row = _rq_paycheck(
                    "SELECT COALESCE(extra_data::json->>'charge_to_billing','false') AS ctb "
                    "FROM orders WHERE order_no=%s LIMIT 1",
                    (_saved_ono,)
                ) or []
                _ctb_flag = str(_ctb_row[0].get("ctb","false")).lower() in ("true","1") if _ctb_row else False

                # Check for existing advance payment
                _ord_id_check = _rq_paycheck(
                    "SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
                    (_saved_ono,)
                ) or []
                if _ord_id_check and not _ctb_flag:
                    _oid_check = _ord_id_check[0]["id"]
                    _pay_check = _rq_paycheck("""
                        SELECT amount, payment_mode, reference_no
                        FROM payments
                        WHERE advance_for_order_id = %s::uuid
                          AND (
                              payment_type = 'ADVANCE'
                              OR COALESCE(payment_no,'') LIKE 'CPR-%%'
                              OR COALESCE(remarks,'') ILIKE '%%consultation fee%%'
                          )
                          AND COALESCE(is_deleted, FALSE) = FALSE
                        ORDER BY created_at DESC LIMIT 1
                    """, (_oid_check,)) or []
                    if _pay_check:
                        _pay_already_recorded = True
                        _pay_recorded_amount  = float(_pay_check[0].get("amount") or 0)
                        _pay_recorded_mode    = str(_pay_check[0].get("payment_mode") or pay_mode)
                        _pay_recorded_ref     = str(_pay_check[0].get("reference_no") or "")
            except Exception:
                pass

        # ── Case 1: payment already in DB — show receipt line ─────────────
        if _pay_already_recorded or _coll_done:
            _disp_amt  = _pay_recorded_amount if _pay_already_recorded else consult_fee
            _disp_mode = _pay_recorded_mode   if _pay_already_recorded else pay_mode
            _disp_ref  = _pay_recorded_ref if _pay_already_recorded else ""
            _ref_html  = (
                f" &nbsp;·&nbsp; Ref {_disp_ref}"
                if _disp_ref else ""
            )
            _prc1, _prc2, _prc3 = st.columns([5, 1.4, 1])
            with _prc1:
                st.markdown(
                    f"<div style='background:#052e16;border:1px solid #22c55e;"
                    f"border-radius:8px;padding:10px 16px'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                    f"<div>"
                    f"<span style='color:#4ade80;font-weight:700;font-size:0.9rem'>"
                    f"🧾 Consultation Fee Received</span>"
                    f"<div style='color:#86efac;font-size:0.72rem;margin-top:3px'>"
                    f"{_saved_ono} &nbsp;·&nbsp; {_disp_mode}"
                    f"{_ref_html}"
                    f" &nbsp;·&nbsp; <span style='color:#4ade80'>✔ Recorded · not duplicated</span>"
                    f"</div>"
                    f"</div>"
                    f"<div style='color:#4ade80;font-size:1.2rem;font-weight:900'>"
                    f"₹{_disp_amt:,.0f}"
                    f"</div>"
                    f"</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )
            with _prc2:
                if st.button("🛍️ Full Billing",
                             key=f"consult_rx_only_billing_{_saved_ono[:8] if _saved_ono else 'x'}",
                             type="primary",
                             use_container_width=True,
                             help="Open full billing with patient + RX only. The consultation receipt stays separate."):
                    try:
                        from modules.consultation import convert_consultation_to_billing
                        _rx_only = convert_consultation_to_billing(_saved_ono)
                        if _rx_only and "error" not in _rx_only:
                            _rxd_ro = _rx_only.get("rx", {})
                            st.session_state["_consult_prefill"] = {
                                "patient_name":     _rx_only.get("patient_name", name),
                                "patient_mobile":   _rx_only.get("patient_mobile", mob),
                                "patient_id":       _rx_only.get("patient_id", pid),
                                "consult_order_id": _rx_only.get("consult_order_id", _saved_ono),
                                "rx_r": {"sph": _rxd_ro.get("sph_r", 0), "cyl": _rxd_ro.get("cyl_r", 0),
                                         "axis": _rxd_ro.get("ax_r", 0), "add": _rxd_ro.get("add_r", 0)},
                                "rx_l": {"sph": _rxd_ro.get("sph_l", 0), "cyl": _rxd_ro.get("cyl_l", 0),
                                         "axis": _rxd_ro.get("ax_l", 0), "add": _rxd_ro.get("add_l", 0)},
                                "order_lines": [],
                                "include_consult_fee": False,
                            }
                            st.session_state["_erp_mode"] = "CONSULT_BILLING"
                            st.session_state["_visit_mode_default"] = 0
                            st.session_state["_force_full_billing_mode"] = True
                            st.session_state.pop("_consult_fee_lines", None)
                            st.session_state.pop("_consult_paid_advance_amount", None)
                            st.session_state.pop("_consult_paid_advance_mode", None)
                            st.session_state.pop("_consult_paid_advance_ref", None)
                            st.session_state.pop("retail_visit_mode", None)
                            st.session_state.pop("_editing_consult_order_id", None)
                            st.session_state.pop("_force_consultation_tab", None)
                            st.session_state["active_module"] = None
                            st.session_state["_retail_entry_count"] = (
                                int(st.session_state.get("_retail_entry_count", 0) or 0) + 1
                            )
                            st.session_state["_sidebar_page"] = "🛍️  Retail Order"
                            st.rerun()
                        elif _rx_only:
                            st.error(_rx_only.get("error", "Could not open full billing."))
                    except Exception as _rx_only_ex:
                        st.error(f"Open billing failed: {_rx_only_ex}")
            with _prc3:
                if st.button("🗑️ Cancel",
                             key=f"consult_cancel_advance_{_saved_ono[:8] if _saved_ono else 'x'}",
                             use_container_width=True,
                             help="Soft-delete this advance payment row"):
                    _cancel_confirm_key = f"consult_cancel_adv_confirm_{_saved_ono}"
                    st.session_state[_cancel_confirm_key] = True
                    st.rerun()

            # Confirm cancel dialog
            _cancel_confirm_key = f"consult_cancel_adv_confirm_{_saved_ono}"
            if st.session_state.get(_cancel_confirm_key):
                st.warning("⚠️ Cancel this advance payment? The order remains — only the payment receipt is removed.")
                _cca1, _cca2 = st.columns(2)
                with _cca1:
                    if st.button("✅ Yes, cancel",
                                 key=f"consult_cancel_adv_yes_{_saved_ono[:8] if _saved_ono else 'x'}",
                                 type="primary", use_container_width=True):
                        try:
                            from modules.sql_adapter import run_write as _rw_ca, run_query as _rq_ca
                            _ord_ca = _rq_ca(
                                "SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
                                (_saved_ono,)
                            ) or []
                            if _ord_ca:
                                _rw_ca("""
                                    UPDATE payments SET is_deleted = TRUE
                                     WHERE advance_for_order_id = %s::uuid
                                       AND (
                                           payment_type = 'ADVANCE'
                                           OR COALESCE(payment_no,'') LIKE 'CPR-%%'
                                           OR COALESCE(remarks,'') ILIKE '%%consultation fee%%'
                                       )
                                       AND COALESCE(is_deleted, FALSE) = FALSE
                                """, (_ord_ca[0]["id"],))
                                _rw_ca("""
                                    WITH paid AS (
                                        SELECT COALESCE(SUM(amount), 0) AS amt
                                        FROM payments
                                        WHERE (order_id = %s::uuid OR advance_for_order_id = %s::uuid)
                                          AND payment_type IN ('PAYMENT','RECEIPT','ADVANCE')
                                          AND COALESCE(is_deleted, FALSE) = FALSE
                                    )
                                    UPDATE orders o
                                       SET advance_amount = paid.amt,
                                           advance_received = paid.amt > 0,
                                           payment_status = CASE
                                               WHEN paid.amt <= 0 THEN 'PENDING'
                                               WHEN COALESCE(o.total_value, 0) > 0
                                                AND paid.amt >= COALESCE(o.total_value, 0) - 0.50 THEN 'PAID'
                                               ELSE 'PARTIAL'
                                           END
                                      FROM paid
                                     WHERE o.id = %s::uuid
                                """, (_ord_ca[0]["id"], _ord_ca[0]["id"], _ord_ca[0]["id"]))
                            st.session_state.pop(_cancel_confirm_key, None)
                            st.session_state.pop(_coll_done_key, None)
                            st.success("✅ Advance payment cancelled")
                            st.rerun()
                        except Exception as _ca_ex:
                            st.error(f"Cancel failed: {_ca_ex}")
                with _cca2:
                    if st.button("❌ Keep",
                                 key=f"consult_cancel_adv_no_{_saved_ono[:8] if _saved_ono else 'x'}",
                                 use_container_width=True):
                        st.session_state.pop(_cancel_confirm_key, None)
                        st.rerun()

        # ── Case 2: no payment yet — show two big action buttons ──────────
        else:
            st.markdown(
                "<div style='font-size:0.78rem;font-weight:700;color:#94a3b8;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>"
                "What would you like to do with the consultation fee?</div>",
                unsafe_allow_html=True
            )
            _btn_col1, _btn_col2 = st.columns(2)

            # ── Big Button A: Record consultation receipt ──────────────────
            with _btn_col1:
                st.markdown(
                    "<div style='background:#052e16;border:2px solid #22c55e;"
                    "border-radius:10px;padding:14px 16px;margin-bottom:8px'>"
                    "<div style='font-size:1.05rem;font-weight:700;color:#4ade80'>💰 Record Receipt</div>"
                    "<div style='font-size:0.72rem;color:#86efac;margin-top:4px'>"
                    "Collect consultation fee now. Full billing later opens RX-only; "
                    "this receipt stays separate."
                    "</div></div>",
                    unsafe_allow_html=True
                )
                _pay_methods = ["CASH", "UPI", "NEFT", "RTGS", "CHEQUE", "CARD", "FREE", "INSURANCE"]
                _pay_default = str(pay_mode or "CASH").strip().upper()
                _rec_amount_key = "consult_receipt_amount"
                _rec_fee_sync_key = "consult_receipt_amount_source_fee"
                _current_fee_for_receipt = float(consult_fee or 0)
                if st.session_state.get(_rec_fee_sync_key) != _current_fee_for_receipt:
                    st.session_state[_rec_amount_key] = _current_fee_for_receipt
                    st.session_state[_rec_fee_sync_key] = _current_fee_for_receipt
                _rcc1, _rcc2 = st.columns([1, 1])
                with _rcc1:
                    _rec_amount = st.number_input(
                        "Amount received ₹",
                        min_value=0.0,
                        value=_current_fee_for_receipt,
                        step=1.0,
                        key=_rec_amount_key,
                    )
                with _rcc2:
                    _rec_mode = st.selectbox(
                        "Payment mode",
                        _pay_methods,
                        index=_pay_methods.index(_pay_default) if _pay_default in _pay_methods else 0,
                        key="consult_receipt_mode",
                    )
                _rec_ref = ""
                if _rec_mode in ("UPI", "NEFT", "RTGS", "CHEQUE", "CARD"):
                    _rec_ref = st.text_input(
                        "UPI / UTR / cheque / card reference *",
                        key="consult_receipt_ref",
                        placeholder="Required for non-cash payment",
                    ).strip()
                _rec_remarks = st.text_input(
                    "Remarks",
                    key="consult_receipt_remarks",
                    placeholder="Optional note",
                ).strip()
                if st.button(
                    f"💰 Record ₹{_rec_amount:.0f} via {_rec_mode}",
                    key="consult_record_receipt_btn",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        if float(_rec_amount or 0) <= 0:
                            st.error("Enter amount received.")
                            st.stop()
                        if _rec_mode in ("UPI", "NEFT", "RTGS", "CHEQUE", "CARD") and not _rec_ref:
                            st.error("Enter UPI / UTR / cheque / card reference number.")
                            st.stop()
                        from modules.sql_adapter import run_write as _rw_rec, run_query as _rq_rec
                        import uuid as _uuid_rec
                        _rec_method = str(_rec_mode or "Cash").strip().upper()
                        _ord_rec = _rq_rec(
                            "SELECT id::text, COALESCE(patient_name, party_name, '') AS pname "
                            "FROM orders WHERE order_no=%s LIMIT 1",
                            (_saved_ono,)
                        ) or []
                        _receipt_ok = False
                        if not _ord_rec:
                            st.error("Consultation order not found in DB. Save the consultation first, then record payment.")
                            st.stop()
                        if _ord_rec:
                            _oid_rec = _ord_rec[0]["id"]
                            _pname_rec = str(_ord_rec[0].get("pname") or name or "").strip()
                            _pay_exists = _rq_rec("""
                                SELECT id FROM payments
                                WHERE advance_for_order_id = %s::uuid
                                  AND (
                                      payment_type = 'ADVANCE'
                                      OR COALESCE(payment_no,'') LIKE 'CPR-%%'
                                      OR COALESCE(remarks,'') ILIKE '%%consultation fee%%'
                                  )
                                  AND COALESCE(is_deleted,FALSE) = FALSE
                                LIMIT 1
                            """, (_oid_rec,)) or []
                            if _pay_exists:
                                _receipt_ok = True
                            else:
                                _receipt_no = _get_next_payment_number()
                                _deleted_same_no = _rq_rec("""
                                    SELECT id::text
                                    FROM payments
                                    WHERE payment_no = %s
                                      AND COALESCE(is_deleted, FALSE) = TRUE
                                    ORDER BY created_at DESC
                                    LIMIT 1
                                """, (_receipt_no,)) or []
                                if _deleted_same_no:
                                    _rw_rec("""
                                        UPDATE payments
                                           SET party_name = %s,
                                               payment_date = CURRENT_DATE,
                                               payment_mode = %s,
                                               method = %s,
                                               amount = %s,
                                               reference_no = %s,
                                               remarks = %s,
                                               order_id = %s::uuid,
                                               advance_for_order_id = %s::uuid,
                                               payment_type = 'RECEIPT',
                                               is_advance = FALSE,
                                               is_deleted = FALSE
                                         WHERE id = %s::uuid
                                    """, (
                                        _pname_rec,
                                        _rec_method, _rec_method, float(_rec_amount),
                                        _rec_ref or None,
                                        _rec_remarks or f"Consultation fee received — {_saved_ono}",
                                        _oid_rec, _oid_rec,
                                        _deleted_same_no[0]["id"],
                                    ))
                                else:
                                    _rw_rec("""
                                        INSERT INTO payments
                                            (id, payment_no, party_name,
                                             payment_date, payment_mode, method,
                                             amount, reference_no, remarks,
                                             order_id, advance_for_order_id,
                                             payment_type, is_advance, created_by, created_at)
                                        VALUES (%s::uuid, %s, %s,
                                                CURRENT_DATE, %s, %s,
                                                %s, %s, %s,
                                                %s::uuid, %s::uuid,
                                                'RECEIPT', FALSE, %s, NOW())
                                    """, (
                                        str(_uuid_rec.uuid4()), _receipt_no,
                                        _pname_rec,
                                        _rec_method, _rec_method, float(_rec_amount),
                                        _rec_ref or None,
                                        _rec_remarks or f"Consultation fee received — {_saved_ono}",
                                        _oid_rec, _oid_rec,
                                        st.session_state.get("user_name", "Consultation"),
                                    ))
                                _rw_rec("""
                                    UPDATE orders
                                       SET payment_mode = %s,
                                           advance_amount = COALESCE(advance_amount, 0) + %s,
                                           advance_received = TRUE,
                                           payment_status = CASE
                                               WHEN COALESCE(total_value, 0) > 0
                                                AND COALESCE(advance_amount, 0) + %s >= COALESCE(total_value, 0) - 0.50
                                               THEN 'PAID'
                                               ELSE 'PARTIAL'
                                           END
                                     WHERE id = %s::uuid
                                """, (
                                    _rec_method, float(_rec_amount), float(_rec_amount), _oid_rec,
                                ))
                                _verify_insert = _rq_rec("""
                                    SELECT id
                                    FROM payments
                                    WHERE payment_no = %s
                                      AND COALESCE(is_deleted, FALSE) = FALSE
                                    LIMIT 1
                                """, (_receipt_no,)) or []
                                _receipt_ok = bool(_verify_insert)
                        if _receipt_ok:
                            st.session_state[_coll_done_key] = True
                            st.rerun()
                        st.error("Payment was not recorded. Please retry and check the technical alert if it repeats.")
                    except Exception as _re:
                        st.error(f"Record failed: {_re}")

            # ── Big Button B: Add to spectacle order ───────────────────────
            with _btn_col2:
                st.markdown(
                    "<div style='background:#0d0a1e;border:2px solid #6366f1;"
                    "border-radius:10px;padding:14px 16px;margin-bottom:8px'>"
                    "<div style='font-size:1.05rem;font-weight:700;color:#818cf8'>🛍️ Add to Order</div>"
                    "<div style='font-size:0.72rem;color:#a5b4fc;margin-top:4px'>"
                    "Patient is buying spectacles. Fee added as line on retail order. "
                    "One combined bill — no separate consultation receipt."
                    "</div></div>",
                    unsafe_allow_html=True
                )
                st.markdown("<div style='height:42px'></div>", unsafe_allow_html=True)
                if st.button(
                    "🛍️ Add fee to billing",
                    key="consult_add_fee_to_billing_btn",
                    type="primary",
                    use_container_width=True,
                ):
                    _paid_for_billing = 0.0
                    _paid_mode_for_billing = pay_mode
                    _paid_ref_for_billing = ""
                    _consult_uuid_for_billing = ""
                    # Mark order as charge_to_billing so bridge knows fee not paid
                    try:
                        from modules.sql_adapter import run_write as _rw_ctb, run_query as _rq_ctb
                        import json as _json_ctb
                        _ctb_ord = _rq_ctb(
                            "SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
                            (_saved_ono,)
                        ) or []
                        _consult_uuid_for_billing = _ctb_ord[0]["id"] if _ctb_ord else ""
                        _ctb_paid_rows = []
                        if _consult_uuid_for_billing:
                            _ctb_paid_rows = _rq_ctb("""
                                SELECT COALESCE(SUM(amount), 0) AS paid,
                                       MAX(COALESCE(NULLIF(payment_mode,''), method, '')) AS mode,
                                       MAX(COALESCE(reference_no,'')) AS ref_no
                                FROM payments
                                WHERE advance_for_order_id = %s::uuid
                                  AND payment_type = 'ADVANCE'
                                  AND COALESCE(is_deleted, FALSE) = FALSE
                            """, (_consult_uuid_for_billing,)) or []
                            if _ctb_paid_rows:
                                _paid_for_billing = float(_ctb_paid_rows[0].get("paid") or 0)
                                _paid_mode_for_billing = str(_ctb_paid_rows[0].get("mode") or pay_mode)
                                _paid_ref_for_billing = str(_ctb_paid_rows[0].get("ref_no") or "")
                            if _paid_for_billing > 0:
                                _rw_ctb("""
                                    UPDATE payments
                                       SET payment_type = 'ADVANCE',
                                           is_advance = TRUE,
                                           remarks = 'Consultation fee carried to retail billing — ' || %s
                                     WHERE advance_for_order_id = %s::uuid
                                       AND payment_type = 'ADVANCE'
                                       AND COALESCE(is_deleted, FALSE) = FALSE
                                """, (_saved_ono, _consult_uuid_for_billing))
                        _rw_ctb("""
                            UPDATE orders
                               SET extra_data = COALESCE(extra_data,'{}'::jsonb)
                                             || %s::jsonb
                             WHERE order_no = %s
                        """, (_json_ctb.dumps({"charge_to_billing": True}), _saved_ono))
                    except Exception:
                        pass
                    # Trigger billing conversion
                    from modules.consultation import convert_consultation_to_billing
                    import uuid as _uuid_sw, datetime as _dt_sw
                    _result_sw = convert_consultation_to_billing(_saved_ono)
                    if _result_sw and "error" not in _result_sw:
                        _rxd_sw  = _result_sw.get("rx", {})
                        _cfee_sw = float(_result_sw.get("consult_fee") or consult_fee or 0)
                        _cpid_sw = _result_sw.get("prod_id","")
                        _cpnm_sw = _result_sw.get("prod_name","Consultation Fee") or "Consultation Fee"
                        _flines_sw = []
                        if _cfee_sw > 0 and _cpid_sw:
                            _flines_sw = [{
                                "line_id": str(_uuid_sw.uuid4()),
                                "provisional_order_id": None,
                                "product_id": _cpid_sw,
                                "product_name": _cpnm_sw,
                                "brand": "Service", "main_group": "Services",
                                "batch_no": "", "eye_side": "SERVICE",
                                "sph": None, "cyl": None, "axis": None, "add_power": None,
                                "lens_params": {}, "boxing_params": {},
                                "requested_qty": 1, "billing_qty": 1,
                                "order_qty": 0, "display_qty": "1 SERVICE",
                                "batch_allocation": [],
                                "unit_price": _cfee_sw, "total_price": _cfee_sw,
                                "gst_percent": 0.0, "gst_amount": 0.0,
                                "is_gst_exempt": True,
                                "is_service_line": True, "status": "Complete",
                                "created_at": _dt_sw.datetime.now().isoformat(),
                            }]
                        st.session_state["_consult_prefill"] = {
                            "patient_name":    _result_sw.get("patient_name", name),
                            "patient_mobile":  _result_sw.get("patient_mobile", mob),
                            "patient_id":      _result_sw.get("patient_id", pid),
                            "consult_order_id":_result_sw.get("consult_order_id", _saved_ono),
                            "rx_r": {"sph":_rxd_sw.get("sph_r",0),"cyl":_rxd_sw.get("cyl_r",0),
                                     "axis":_rxd_sw.get("ax_r",0),"add":_rxd_sw.get("add_r",0)},
                            "rx_l": {"sph":_rxd_sw.get("sph_l",0),"cyl":_rxd_sw.get("cyl_l",0),
                                     "axis":_rxd_sw.get("ax_l",0),"add":_rxd_sw.get("add_l",0)},
                            "order_lines":    _flines_sw,
                            "include_consult_fee": True,
                            "consult_fee":    _cfee_sw,
                            "consult_paid":   False,
                            "consult_paid_amount": _paid_for_billing,
                            "payment_mode":   _paid_mode_for_billing or pay_mode,
                            "payment_ref":    _paid_ref_for_billing,
                        }
                        st.session_state.pop("_order_edit_prefill", None)
                        st.session_state["_erp_mode"] = "CONSULT_BILLING"
                        _erp_sw = ""
                        try:
                            from modules.sql_adapter import run_query as _rq_sw
                            _sw_row = _rq_sw("SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
                                             (_saved_ono,)) or []
                            if _sw_row: _erp_sw = _sw_row[0]["id"]
                        except Exception: pass
                        st.session_state["_erp_order_id"]              = _erp_sw
                        st.session_state["_open_retail_after_consult"] = False
                        st.session_state.pop("_consult_fee_lines_consumed", None)
                        st.session_state["_visit_mode_default"]        = 0
                        st.session_state["_force_full_billing_mode"]   = True
                        if _paid_for_billing > 0:
                            st.session_state["_consult_paid_advance_amount"] = _paid_for_billing
                            st.session_state["_consult_paid_advance_mode"] = _paid_mode_for_billing or pay_mode
                            st.session_state["_consult_paid_advance_ref"] = _paid_ref_for_billing
                        else:
                            st.session_state.pop("_consult_paid_advance_amount", None)
                            st.session_state.pop("_consult_paid_advance_mode", None)
                            st.session_state.pop("_consult_paid_advance_ref", None)
                        st.session_state.pop("retail_visit_mode", None)
                        st.session_state.pop("_editing_consult_order_id", None)
                        st.session_state.pop("_force_consultation_tab", None)
                        st.session_state["active_module"] = None
                        st.session_state["_retail_entry_count"] = (
                            int(st.session_state.get("_retail_entry_count",0) or 0) + 1
                        )
                        st.session_state["_sidebar_page"] = "🛍️  Retail Order"
                        st.rerun()
                    else:
                        st.error(_result_sw.get("error","Conversion failed"))

        # ── Payment Audit — inline DB check ──────────────────────────────
        with st.expander("🔍 Payment audit — check DB rows", expanded=False):
            if _saved_ono:
                try:
                    from modules.sql_adapter import run_query as _rq_audit
                    # Get order UUID
                    _oa_ord = _rq_audit(
                        "SELECT id::text, order_no, total_value, order_type FROM orders WHERE order_no=%s LIMIT 1",
                        (_saved_ono,)
                    ) or []
                    if not _oa_ord:
                        st.caption(f"Order {_saved_ono} not found in DB")
                    else:
                        _oa_oid = _oa_ord[0]["id"]
                        st.caption(f"Order UUID: `{_oa_oid}`  ·  Type: {_oa_ord[0].get('order_type')}  ·  Value: ₹{float(_oa_ord[0].get('total_value') or 0):.0f}")
                        # All payment rows for this order
                        _oa_pays = _rq_audit("""
                            SELECT
                                payment_no, payment_date, payment_mode,
                                amount, payment_type,
                                COALESCE(is_deleted,FALSE) AS deleted,
                                created_at::text AS created_at
                            FROM payments
                            WHERE advance_for_order_id = %s::uuid
                               OR order_id = %s::uuid
                            ORDER BY created_at
                        """, (_oa_oid, _oa_oid)) or []

                        if not _oa_pays:
                            st.warning("⚠️ No payment rows found for this order in DB")
                        else:
                            _active = [p for p in _oa_pays if not p.get("deleted")]
                            _deleted = [p for p in _oa_pays if p.get("deleted")]
                            st.success(f"✅ {len(_active)} active payment row(s)  ·  {len(_deleted)} soft-deleted")
                            for _p in _oa_pays:
                                _del_mark = " ~~DELETED~~" if _p.get("deleted") else ""
                                st.markdown(
                                    f"- `{_p['payment_no']}` · ₹{float(_p['amount'] or 0):.0f} · "
                                    f"{_p['payment_mode']} · {_p['payment_type']} · {str(_p['created_at'])[:16]}"
                                    + _del_mark
                                )
                except Exception as _ae:
                    st.error(f"Audit query failed: {_ae}")
            else:
                st.caption("Save consultation first to audit payments")

        # ── WhatsApp — reads from DB, no sticky text_input widget ────────
        # The text_input below was causing phone number to "stick" across patients
        # because Streamlit preserves widget state by key.
        # Solution: resolve mobile from DB only, show a direct wa.me link.
        # Staff can correct the number in "Patient Details" expander if needed.
        _wa_db_mob = ""
        if _saved_ono:
            try:
                from modules.sql_adapter import run_query as _rq_wa_db
                _wa_row = _rq_wa_db(
                    "SELECT COALESCE(patient_mobile,'') AS mobile "
                    "FROM orders WHERE order_no=%s LIMIT 1",
                    (_saved_ono,)
                ) or []
                _wa_db_mob = str(_wa_row[0].get("mobile","") or "").strip() if _wa_row else ""
                # Fallback to patient table if blank on order
                if not _wa_db_mob and _has_real_patient_id:
                    _wa_pt = _rq_wa_db(
                        "SELECT COALESCE(mobile,'') AS mobile FROM patients WHERE id=%s::uuid LIMIT 1",
                        (str(pid),)
                    ) or []
                    _wa_db_mob = str(_wa_pt[0].get("mobile","") or "").strip() if _wa_pt else ""
            except Exception:
                _wa_db_mob = str(mob or "").strip()

        _wa_msg = _wa_consultation_msg(_saved_ono, consult_fee)
        try:
            from modules.wa_contact_tools import render_mobile_field
            from modules.wa_hub import wa_link
            _wa_send_mob = render_mobile_field(
                f"consult_rx_{_saved_ono or 'draft'}",
                name=name,
                mobile=_wa_db_mob or mob,
                patient_id=str(pid or "") if _has_real_patient_id else "",
                order_id=str(_edit_order_id or st.session_state.get("_erp_order_id", "") or ""),
                label="WhatsApp mobile",
            )
            _wa_url = wa_link(_wa_send_mob, _wa_msg)
        except Exception:
            import urllib.parse as _uparse
            _wa_clean = "".join(x for x in (_wa_db_mob or mob) if x.isdigit())
            if _wa_clean.startswith("91") and len(_wa_clean) == 12:   _wa_clean = _wa_clean[2:]
            elif _wa_clean.startswith("0") and len(_wa_clean) == 11:  _wa_clean = _wa_clean[1:]
            elif _wa_clean.startswith("091") and len(_wa_clean) == 13: _wa_clean = _wa_clean[3:]
            _wa_e164 = ("91" + _wa_clean) if (len(_wa_clean) == 10 and _wa_clean[0] in "6789") else ""
            _wa_url = f"https://wa.me/{_wa_e164}?text={_uparse.quote(_wa_msg)}" if _wa_e164 else ""

        if _wa_url:
            st.link_button("📲 Send WhatsApp RX", _wa_url, use_container_width=False)
        else:
            st.caption("📲 Enter and save a valid mobile number to enable WhatsApp.")

        # ══════════════════════════════════════════════════════════════════
        # ══════════════════════════════════════════════════════════════════
        # ── Close visit ───────────────────────────────────────────────────
        st.markdown("---")
        _cv1, _cv2 = st.columns(2)
        with _cv1:
            if st.button("🧾 Print Consultation Receipt",
                         key="consult_close_print_receipt",
                         use_container_width=True):
                _print_consultation_receipt(
                    order_no=_saved_ono or "—",
                    patient_name=name, mobile=mob,
                    consult_type=st.session_state.get("consult_type_select","Consultation"),
                    fee=consult_fee, pay_mode=pay_mode, visit_date=today,
                    shop=shop, addr=addr, phone=phone,
                    rx_r=(rx_r_sph, rx_r_cyl, rx_r_axis, rx_r_add),
                    rx_l=(rx_l_sph, rx_l_cyl, rx_l_axis, rx_l_add),
                    shop_upi=_si.get("shop_upi_id", ""),
                )
            if _wa_url:
                st.link_button("📲 WhatsApp RX", _wa_url, use_container_width=True)
        with _cv2:
            if st.button("✅ Close Visit & Next Patient",
                         key="consult_close_visit_only",
                         use_container_width=True,
                         type="primary"):
                _reset_consultation()
                st.rerun()


def _set_consult_billing_state(
    order_no: str, fee: float, pay_mode: str,
    pid: str, name: str, mob: str,
    record_payment_now: bool = False,
) -> None:
    """
    After consultation save, bridge the billing system.

    record_payment_now=True  → fee was collected now → set _consult_paid_advance_amount
                               so if staff later adds billing, retail knows it's pre-paid.
    record_payment_now=False → fee not yet collected → do NOT set advance.
                               Retail billing panel shows no phantom "₹400 already paid".
    """
    import streamlit as _st
    _fee = float(fee or 0)
    if _fee > 0 and record_payment_now:
        # Fee collected at consultation — mark as advance for bridge awareness.
        _st.session_state["_consult_paid_advance_amount"] = _fee
        _st.session_state["_consult_paid_advance_mode"] = pay_mode or "CASH"
        _st.session_state.setdefault("_consult_paid_advance_ref", "")
    else:
        # Fee not yet collected (pending or deferred) — no advance in session.
        _st.session_state.pop("_consult_paid_advance_amount", None)
        _st.session_state.pop("_consult_paid_advance_mode", None)
        _st.session_state.pop("_consult_paid_advance_ref", None)
    # A plain consultation save should never carry a previous patient's service line.
    # The billing conversion path creates fresh _consult_fee_lines from _consult_prefill.
    _st.session_state.pop("_consult_fee_lines", None)
    _st.session_state.pop("_consult_fee_consumed", None)
    _st.session_state.pop("_consult_fee_removed", None)
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


def _get_next_cs_number() -> str:
    """
    Allocate next consultation number via the real registry API.

    Uses alloc_doc_number("CONSULTATION") which:
      - opens its own short transaction (no cursor needed from caller)
      - row-locks order_number_registry FOR UPDATE → gap-free, concurrent-safe
      - formats using SERIES_CONFIG["CONSULTATION"]["prefix"] = "CS"
      - produces CS/2627/0001, CS/2627/0002, ...

    Fallback (if registry import fails):
      MAX()+1 scan of existing CS/* numbers in orders table.

    Ultimate fallback (if DB unavailable):
      Old CONS-YYYYMMDD-XXXXXX format — never crashes.
    """
    try:
        from modules.db.order_number_registry import alloc_doc_number
        return alloc_doc_number("CONSULTATION")
    except Exception as _reg_err:
        import logging as _log
        _log.getLogger(__name__).warning(
            f"[CONSULTATION] alloc_doc_number failed, using MAX+1 fallback: {_reg_err}"
        )
        # MAX+1 fallback using existing CS/* numbers
        try:
            from modules.sql_adapter import run_query as _rq_fb
            import datetime as _dt_fb
            _today  = _dt_fb.date.today()
            _fy_s   = _today.year if _today.month >= 4 else _today.year - 1
            _fy_str = f"{str(_fy_s)[2:]}{str(_fy_s + 1)[2:]}"
            _prefix = f"CS/{_fy_str}/"
            _existing = _rq_fb("""
                SELECT order_no FROM orders
                WHERE order_no LIKE %s
                  AND order_type = 'CONSULTATION'
                  AND COALESCE(is_deleted, FALSE) = FALSE
                ORDER BY order_no DESC LIMIT 1
            """, (f"{_prefix}%",)) or []
            _last_seq = 0
            if _existing:
                try: _last_seq = int(_existing[0]["order_no"].split("/")[-1])
                except Exception: pass
            return f"{_prefix}{_last_seq + 1:04d}"
        except Exception:
            from datetime import date as _d
            import uuid as _u
            return f"CONS-{_d.today().strftime('%Y%m%d')}-{str(_u.uuid4())[:6].upper()}"


def _get_next_payment_number() -> str:
    try:
        from modules.db.order_number_registry import alloc_doc_number
        return alloc_doc_number("PAYMENT")
    except Exception as _reg_err:
        import logging as _log
        _log.getLogger(__name__).warning(
            f"[CONSULTATION] payment number registry failed, using fallback: {_reg_err}"
        )
        from datetime import date as _d
        import uuid as _u
        return f"PAY/{_d.today().strftime('%y%m%d')}/{str(_u.uuid4())[:6].upper()}"



def _ensure_consultation_advance(order_id, order_no, fee, pay_mode, name, run_query, run_write):
    """
    Self-heal: insert an advance payment row for a consultation order if one
    does not already exist. Called both from the same-day guard (so returning
    an existing order always ends up with a payment) and can be called directly
    from the post-save panel.

    Args:
        order_id  : UUID string of the orders row
        order_no  : CONS-* display number (used in payment_no)
        fee       : float — consultation fee
        pay_mode  : str
        name      : patient name for remarks
        run_query : bound run_query function
        run_write : bound run_write function
    """
    if float(fee or 0) <= 0:
        return  # no fee — nothing to do
    try:
        import uuid as _uuid_heal
        _pay_mode_norm = str(pay_mode or "Cash").strip().upper()
        _pay_exists = run_query("""
            SELECT id FROM payments
            WHERE advance_for_order_id = %s::uuid
              AND (
                  payment_type = 'ADVANCE'
                  OR COALESCE(payment_no,'') LIKE 'CPR-%%'
                  OR COALESCE(remarks,'') ILIKE '%%consultation fee%%'
              )
              AND COALESCE(is_deleted, FALSE) = FALSE
            LIMIT 1
        """, (order_id,)) or []
        if not _pay_exists:
            _pay_id = str(_uuid_heal.uuid4())
            _payment_no = _get_next_payment_number()
            run_write("""
                INSERT INTO payments (
                    id, payment_no, party_name,
                    payment_date, payment_mode,
                    amount, remarks,
                    order_id, advance_for_order_id,
                    payment_type, is_advance, created_by, created_at
                ) VALUES (
                    %s::uuid, %s, %s,
                    CURRENT_DATE, %s,
                    %s, %s,
                    %s::uuid, %s::uuid,
                    'RECEIPT', FALSE, %s, NOW()
                )
                ON CONFLICT DO NOTHING
            """, (
                _pay_id, _payment_no, name,
                _pay_mode_norm,
                float(fee), f"Consultation fee received — {order_no}",
                order_id, order_id,
                "Consultation",
            ))
    except Exception as _heal_err:
        import logging as _log
        _log.getLogger(__name__).warning(
            f"[CONSULTATION] _ensure_consultation_advance failed: {_heal_err}"
        )


def _save_consultation(pid, name, mob, fee, pay_mode, rx_r, rx_l, referral="", force_new_visit=False, consult_type="Consultation", charge_to_billing=False, record_payment_now=False):
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
        order_no = _get_next_cs_number()   # e.g. CS/2627/0001
        visit_id = str(uuid.uuid4())

        # ── Unpack Rx tuples ─────────────────────────────────────────────
        def _safe_float(v):
            try: return float(v) if v not in (None, "", "None") else None
            except Exception as _e:
                logger.warning("Suppressed error: %s", _e)
                return None
        def _safe_int(v):
            try: return int(float(v)) if v not in (None, "", "None", "0") else None
            except Exception as _e:
                logger.warning("Suppressed error: %s", _e)
                return None

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
        name = format_person_name(name)

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
        # Normal path: if a visit already exists today, UPDATE it (no duplicates).
        # force_new_visit=True: skip the lookup entirely — always INSERT a fresh row.
        # This is used for re-examination / follow-up on the same calendar day.
        visit_saved = False
        if _pid_is_valid:
            try:
                # Only look for an existing visit when NOT forcing a new one
                _existing_visit = []
                if not force_new_visit:
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
                    # INSERT new visit row (also the only path when force_new_visit=True)
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

        # ── 2. display_order_no intentionally skipped for CONSULTATION ───
        # Consultation orders must NOT consume orders_display_seq — that sequence
        # is for retail/wholesale order numbering. Gaps caused by consultation saves
        # create confusing jumps in the order register.
        # Consultation is identified by its CONS-* order_no and order_type='CONSULTATION'.
        _disp_col = ""
        _disp_val = ""

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
        # Prevents duplicates on double-click or rerun race condition.
        # Bypassed when force_new_visit=True (staff explicitly wants a second visit).
        if _pid_is_valid and not force_new_visit:
            _existing = run_query("""
                SELECT order_no, id::text AS oid FROM orders
                WHERE party_id = %s::uuid
                  AND order_type = 'CONSULTATION'
                  AND DATE(created_at) = CURRENT_DATE
                ORDER BY created_at DESC LIMIT 1
            """, (pid,)) or []
            if _existing:
                _existing_ono = _existing[0]["order_no"]
                _existing_oid = _existing[0]["oid"]
                # Patch blank name/fee if needed
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
                # Only record consultation receipt when explicitly collected.
                if record_payment_now and not charge_to_billing:
                    _ensure_consultation_advance(_existing_oid, _existing_ono, fee, pay_mode, name, run_query, run_write)
                return _existing_ono  # already saved — return existing

        # Build extra_data JSON: stores consult_type and charge_to_billing flag
        import json as _json_save
        _extra_data = _json_save.dumps({
            "consult_type":      consult_type or "Consultation",
            "charge_to_billing": bool(charge_to_billing),
        })

        run_write(f"""
            INSERT INTO orders (
                id, order_no, order_type, order_source, status,
                party_name, patient_name, patient_mobile,
                total_items, total_value, payment_mode,
                extra_data,
                created_at{_party_col}{_visit_col}{_disp_col}
            ) VALUES (
                %s::uuid, %s, 'CONSULTATION', 'RETAIL', 'CLOSED',
                %s, %s, %s,
                0, %s, %s,
                %s::jsonb,
                NOW(){_party_val}{_visit_val}{_disp_val}
            )
            ON CONFLICT DO NOTHING
        """, (
            order_id, order_no,
            name, name, mob,
            float(fee), pay_mode,
            _extra_data,
            *_extra_params,
        ))

        # ── 4. order_lines intentionally omitted for CONSULTATION orders ──
        # Consultation is a clinical record, not a product sale.
        # Fee amount is stored in orders.total_value and payments table only.
        # When staff converts to billing via the explicit button, convert_consultation_to_billing()
        # passes the fee as advance — no order_line duplication.

        # ── Auto-insert advance payment via shared helper ─────────────────
        # Skipped when charge_to_billing=True — fee will appear on the retail
        # order instead. Recording it here AND there would be a double-charge.
        if record_payment_now and not charge_to_billing:
            _ensure_consultation_advance(order_id, order_no, fee, pay_mode, name, run_query, run_write)
        elif not charge_to_billing:
            import logging as _log_pending
            _log_pending.getLogger(__name__).info(
                f"[CONSULTATION] {order_no}: payment not recorded on save; awaiting collection."
            )
        else:
            # Store a note so the post-save panel explains why no receipt yet
            import logging as _log_ctb
            _log_ctb.getLogger(__name__).info(
                f"[CONSULTATION] {order_no}: charge_to_billing=True — "
                f"fee ₹{fee:.0f} deferred to retail order, no advance payment recorded."
            )

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
        # ── Edit-mode keys — these carry sticky data from order_edit_view ──
        # Must be cleared so a new patient load doesn't inherit previous patient's phone
        "_erp_patient_mob", "_erp_patient_name", "_erp_patient_id",
        "_erp_order_id", "_erp_visit_id", "_erp_mode", "_erp_rx_r", "_erp_rx_l",
        "_editing_consult_order_id",
        "_force_consultation_tab",
        # Edit-mode save tracking
        "_consult_edit_saved",
        # Consultation type / payment mode widget state
        "consult_type_select", "consult_charge_to_billing",
        "consult_force_new_visit", "consult_payment_mode",
    ]
    for k in keys:
        if k in st.session_state:
            del st.session_state[k]
    # Clear all consult_saved_* and consult_wa_mob_edited_* markers
    for k in list(st.session_state.keys()):
        if (k.startswith("consult_saved_") or
                k.startswith("consult_wa_mob_edited_") or
                k.startswith("consult_converted_")):
            del st.session_state[k]
    # Release any lingering save lock
    try:
        from modules.utils.submit_guard import clear_lock
        clear_lock("consult_save_action")
    except Exception:
        pass


def _print_consultation_receipt(
    order_no: str, patient_name: str, mobile: str, consult_type: str,
    fee: float, pay_mode: str, visit_date: str,
    shop: str, addr: str, phone: str,
    rx_r: tuple = None, rx_l: tuple = None,
    shop_upi: str = "",
):
    """
    Print a standalone consultation receipt (80mm thermal format).
    Shows: shop header, patient, consultation type, fee, payment mode, RX summary.
    Opens in browser print dialog.
    """
    def _fv(v):
        if v is None or str(v).strip() in ("", "None", "nan", "0.0", "0"):
            return "—"
        try:
            f = float(v)
            return f"{f:+.2f}" if f != 0 else "Plano"
        except Exception:
            return str(v).strip() or "—"

    def _fa(v):
        if not v or str(v).strip() in ("", "None", "nan", "0"):
            return "—"
        try: return str(int(float(v))) + "°"
        except Exception: return str(v)

    _rx_block = ""
    if rx_r or rx_l:
        _rr = rx_r or ("", "", "", "")
        _rl = rx_l or ("", "", "", "")
        _rx_block = f"""
        <div class=sect>Prescription</div>
        <table>
          <tr><th>Eye</th><th>SPH</th><th>CYL</th><th>AX</th><th>ADD</th></tr>
          <tr>
            <td>R</td>
            <td>{_fv(_rr[0])}</td><td>{_fv(_rr[1])}</td>
            <td>{_fa(_rr[2])}</td><td>{_fv(_rr[3]) if len(_rr)>3 else '—'}</td>
          </tr>
          <tr>
            <td>L</td>
            <td>{_fv(_rl[0])}</td><td>{_fv(_rl[1])}</td>
            <td>{_fa(_rl[2])}</td><td>{_fv(_rl[3]) if len(_rl)>3 else '—'}</td>
          </tr>
        </table>"""

    _upi_html = ""
    shop_upi = str(shop_upi or "").strip()
    if shop_upi:
        try:
            import urllib.parse as _url_qr
            import qrcode as _qr_mod, io as _io_qr, base64 as _b64_qr
            _upi_str = "upi://pay?" + _url_qr.urlencode({
                "pa": shop_upi,
                "pn": shop or "DV Optical",
                "am": f"{float(fee or 0):.2f}",
                "tn": order_no,
                "cu": "INR",
            })
            _qr = _qr_mod.QRCode(
                version=None,
                error_correction=_qr_mod.constants.ERROR_CORRECT_M,
                box_size=3,
                border=2,
            )
            _qr.add_data(_upi_str)
            _qr.make(fit=True)
            _img = _qr.make_image(fill_color="black", back_color="white")
            _buf = _io_qr.BytesIO()
            _img.save(_buf, format="PNG")
            _b64 = _b64_qr.b64encode(_buf.getvalue()).decode()
            _upi_html = (
                "<div class='qrbox'>"
                "<div class='qr' style=\"background-image:url(data:image/png;base64,{})\"></div>"
                "<div class='qrt'>Scan to Pay</div>"
                "<div class='upid'>{}</div>"
                "</div>"
            ).format(_b64, shop_upi)
        except Exception:
            _upi_html = f"<div class='qrbox'><b>UPI:</b><br>{shop_upi}</div>"

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Arial,sans-serif;font-size:11px;color:#111;background:#fff;padding:4mm}}
.sn{{font-size:14px;font-weight:900;text-align:center;margin-bottom:2px}}
.sa{{font-size:9px;color:#444;text-align:center;margin-bottom:6px}}
.divider{{border-top:1px dashed #aaa;margin:5px 0}}
.sect{{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
       color:#555;margin:5px 0 3px}}
.pno{{font-family:monospace;background:#0f172a;color:#34d399;padding:2px 8px;
      border-radius:4px;display:block;text-align:center;margin:5px 0;font-size:11px}}
table{{width:100%;border-collapse:collapse;font-size:10px}}
th{{background:#f1f5f9;padding:3px 4px;font-size:9px;text-align:center}}
td{{padding:3px 4px;border-bottom:1px solid #e2e8f0;text-align:center}}
td:first-child{{text-align:left;font-weight:700}}
.row{{display:flex;justify-content:space-between;font-size:11px;padding:2px 0}}
.row b{{font-size:12px}}
.total{{border-top:2px solid #111;margin-top:4px;padding-top:4px;
        font-weight:900;font-size:13px;display:flex;justify-content:space-between}}
.qrbox{{border:1px solid #ddd;margin:6px auto 2px;padding:4px;text-align:center;width:38mm}}
.qr{{width:26mm;height:26mm;margin:0 auto;background-size:contain;background-repeat:no-repeat;
     -webkit-print-color-adjust:exact;print-color-adjust:exact;color-adjust:exact}}
.qrt{{font-size:8px;color:#555;margin-top:2px;font-weight:700}}
.upid{{font-size:8px;font-family:monospace;color:#111;word-break:break-all}}
.ft{{font-size:8px;color:#94a3b8;text-align:center;margin-top:6px;
     border-top:1px dashed #ddd;padding-top:4px}}
@media print{{
  @page{{size:80mm auto;margin:3mm}}
  body{{padding:0}}
}}
</style></head><body>

<div class="sn">{shop}</div>
<div class="sa">{addr}{(' · ' + phone) if phone else ''}</div>
<div class="divider"></div>

<div class="sect">Consultation Receipt</div>
<span class="pno">{order_no}</span>

<div class="row"><span>Date</span><span>{visit_date}</span></div>
<div class="row"><span>Patient</span><b>{patient_name}</b></div>
{'<div class="row"><span>Mobile</span><span>' + mobile + '</span></div>' if mobile else ''}
<div class="divider"></div>

<div class="sect">Charge</div>
<div class="row"><span>{consult_type}</span><span>₹{fee:.0f}</span></div>
<div class="row"><span>Mode</span><span>{pay_mode}</span></div>
<div class="total"><span>Total Received</span><span>₹{fee:.0f}</span></div>
{_upi_html}

{_rx_block}

<div class="divider"></div>
<div class="ft">
  This prescription is valid for one year from date of examination.<br>
  {shop} · {addr}
</div>

<script>window.onload = function() {{ window.print(); }}</script>
</body></html>"""

    _open_print_tab(html, filename="consultation_receipt.html")


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
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        _bc_svg = ''
    _bc_section = (
        f"<div style='margin-top:10px;text-align:center'>{_bc_svg}"
        f"<div style='font-family:monospace;font-size:9px;color:#111'>{patient_barcode}</div></div>"
    ) if _bc_svg else ''
    footer_txt = kw.get('footer','This prescription is valid for one year from the date of examination.')

    def _val(v):
        return str(v).strip() if v not in (None, "", "—") else "—"

    def _finding_line(label, value):
        return f"<div class='finding-line'><b>{label}:</b> <span>{_val(value)}</span></div>"

    def _rx_va(distance, near):
        d = _val(distance)
        n = _val(near)
        if d == "—" and n == "—":
            return "—"
        return f"{d}, {n}"

    slit_lamp_html = "".join([
        _finding_line("Lids", kw.get("lids")),
        _finding_line("Conjunctiva", kw.get("conjunctiva")),
        _finding_line("Cornea", kw.get("cornea")),
        _finding_line("AC", kw.get("ac")),
        _finding_line("Iris", kw.get("iris")),
        _finding_line("Lens", kw.get("lens")),
        _finding_line("Vitreous", kw.get("vitreous")),
    ])
    retina_html = "".join([
        _finding_line("Retina / Fundus", kw.get("fundus")),
        _finding_line("IOP Right", kw.get("iop_r")),
        _finding_line("IOP Left", kw.get("iop_l")),
    ])
    orthoptic_html = "".join([
        _finding_line("Cover Test Distance", kw.get("ortho_dist")),
        _finding_line("Cover Test Near", kw.get("ortho_near")),
        _finding_line("Nystagmus", kw.get("nystagmus")),
        _finding_line("Ocular Motility", kw.get("motility")),
        _finding_line("Convergence", kw.get("convergence")),
        _finding_line("Remarks", kw.get("remarks")),
    ])
    rx_notes_html = "".join([
        _finding_line("Doctor Notes", kw.get("doctor_notes")),
        _finding_line("Treatment", kw.get("treatment_plan")),
        _finding_line("Follow-up", kw.get("followup_advice")),
    ])

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:Arial,sans-serif;padding:10mm;font-size:10.5px;color:#111;background:#fff}}
    .hdr{{background:#fff;color:#111;padding:0 0 8px;margin-bottom:8px;
          border-bottom:1.5px solid #111;display:flex;justify-content:space-between;align-items:flex-start}}
    .shop-name{{font-size:18px;font-weight:900}}
    .shop-sub{{font-size:9.5px;margin-top:2px;color:#111}}
    .doc-type{{font-size:14px;font-weight:700;text-align:right}}
    .patient-row{{display:flex;justify-content:space-between;
                  background:#fff;padding:6px 0;border-bottom:0.5px solid #111;margin-bottom:8px;color:#111}}
    table{{width:100%;border-collapse:collapse;margin:6px 0}}
    th{{background:#fff;color:#111;padding:5px 8px;font-size:10px;text-align:center;border:0.5px solid #111}}
    th:first-child{{text-align:left}}
    td{{border:0.5px solid #111;font-size:10.5px;color:#111}}
    .section{{font-size:11px;font-weight:700;color:#111;margin:9px 0 4px;
              border-bottom:0.5px solid #111;padding-bottom:2px}}
    .findings-grid{{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:5px}}
    .finding-box{{border:0.5px solid #111;min-height:82px;padding:6px 8px;break-inside:avoid}}
    .finding-box.tall{{min-height:118px}}
    .finding-title{{font-size:10.5px;font-weight:700;margin-bottom:4px;text-transform:uppercase}}
    .finding-line{{font-size:9.8px;line-height:1.35;margin-bottom:2px;color:#111}}
    .finding-line span{{color:#111}}
    .finding-item b{{color:#111}}
    .fee-box{{background:#fff;border:0.5px solid #111;border-radius:0;
              padding:6px 10px;margin-top:8px;font-size:10.5px;color:#111}}
    .sig-row{{display:flex;justify-content:space-between;margin-top:14px}}
    .sig-line{{border-top:0.5px solid #111;width:140px;text-align:center;
               padding-top:4px;font-size:9px;color:#111}}
    .footer{{border-top:0.5px dashed #111;margin-top:10px;padding-top:5px;
             font-size:9px;color:#111;text-align:center}}
    @media print{{@page{{size:A4;margin:10mm}}body{{padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
    </style></head><body>

    <div class="hdr">
      <div>
        <div class="shop-name">{shop.upper()}</div>
        <div class="shop-sub">{addr}{' · ' + phone if phone else ''}</div>
      </div>
      <div>
        <div class="doc-type">CLINICAL PRESCRIPTION</div>
        <div style="font-size:10px;text-align:right;color:#111">{dt}</div>
      </div>
    </div>

    <div class="patient-row">
      <div><b>{name}</b> &nbsp; <span style="color:#111;font-size:10px">{mob}</span></div>
      <div style="text-align:right">
        <div style="font-size:10px;color:#111">Date: {dt}</div>
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
      <tr><th style="text-align:left">Eye</th><th>Unaided</th><th>Best Corrected</th><th>Near</th><th>Rx</th></tr>
      <tr style="background:#eff6ff">
        <td style="padding:4px 8px;font-weight:700">Right</td>
        <td style="padding:4px 8px;text-align:center">{va_ur or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{va_ar or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{va_nr or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{_rx_va(va_ar, va_nr)}</td>
      </tr>
      <tr style="background:#f0fdf4">
        <td style="padding:4px 8px;font-weight:700">Left</td>
        <td style="padding:4px 8px;text-align:center">{va_ul or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{va_al or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{va_nl or '—'}</td>
        <td style="padding:4px 8px;text-align:center">{_rx_va(va_al, va_nl)}</td>
      </tr>
    </table>

    <div class="section">Clinical Findings</div>
    <div class="findings-grid">
      <div class="finding-box tall">
        <div class="finding-title">Slit Lamp Examination</div>
        {slit_lamp_html}
      </div>
      <div class="finding-box tall">
        <div class="finding-title">Retina</div>
        {retina_html}
      </div>
      <div class="finding-box">
        <div class="finding-title">Orthoptic</div>
        {orthoptic_html}
      </div>
      <div class="finding-box">
        <div class="finding-title">Rx</div>
        {rx_notes_html}
      </div>
    </div>

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
        except Exception as _e:
            logger.warning("Suppressed error: %s", _e)
            return str(v).strip() or None

    def _fmtax(v):
        if not v or str(v).strip() in ('', 'None', 'nan', '0', '---'):
            return None
        try:  return str(int(float(v)))
        except Exception as _e:
            logger.warning("Suppressed error: %s", _e)
            return str(v).strip() or None

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
            # It's an order_no string — either legacy CONS-YYYYMMDD-XXXXXX or new CS/2627/0001
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
                            (id, product_name, main_group, gst_percent, is_gst_exempt, is_active, created_at)
                        VALUES (%s::uuid, 'Consultation Fee', 'Services', 0, true, true, NOW())
                        ON CONFLICT DO NOTHING
                    """, (prod_id,))
                except Exception:
                    pass  # already exists

        # NOTE: is_converted is set in retail_punching.py AFTER retail order save.
        paid_rows = run_query("""
            SELECT COALESCE(SUM(amount), 0) AS paid,
                   MAX(COALESCE(payment_mode, method, '')) AS mode,
                   MAX(COALESCE(reference_no,'')) AS ref_no
            FROM payments
            WHERE advance_for_order_id = %s::uuid
              AND payment_type = 'ADVANCE'
              AND COALESCE(is_deleted, FALSE) = FALSE
        """, (consult_order_id,)) or []
        paid_amount = float(paid_rows[0].get("paid") or 0) if paid_rows else 0.0
        paid_mode = str(paid_rows[0].get("mode") or c["payment_mode"] or "") if paid_rows else str(c["payment_mode"] or "")
        paid_ref = str(paid_rows[0].get("ref_no") or "") if paid_rows else ""

        return {
            "success": True,
            "patient_name":   c["patient_name"],
            "patient_mobile": c.get("patient_mobile",""),
            "patient_id":     resolved_patient_id,
            "payment_mode":   paid_mode,
            "cons_order_no":  c["order_no"],
            "consult_order_id": consult_order_id,  # returned so retail_punching can mark CONVERTED after save
            "cons_date":      cons_date,
            "consult_fee":    fee,
            "consult_paid":   paid_amount >= max(fee - 0.01, 0),
            "consult_paid_amount": paid_amount,
            "consult_paid_ref": paid_ref,
            "prod_id":        prod_id,
            "prod_name":      prod_name if fee > 0 else "",
            "rx": rxd,
        }

    except Exception as ex:
        return {"error": str(ex)}
