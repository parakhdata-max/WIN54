"""
modules/wa_engine.py
════════════════════════════════════════════════════════════════════════════
Central WhatsApp engine.

Place at: WIN54/modules/wa_engine.py

TWO JOBS:
  1. resolve_mobile()     — find the right mobile from ANY context
  2. render_wa_trigger()  — standard WhatsApp button/panel used everywhere

RESOLUTION CHAIN (in priority order):
  1. order dict          → patient_mobile / party_mobile / mobile
  2. patient_id (DB)     → patients.mobile
  3. order_id (DB)       → orders.patient_mobile
  4. session state       → retail_patient_mobile / _erp_patient_mob
  5. passed-in fallback  → whatever the caller provides

This means:
  • Consultation close   → resolves from session (patient just selected)
  • Backoffice           → resolves from order dict (already loaded)
  • Wholesale            → resolves from party/order
  • Any new page         → one import, one call, always correct

USAGE
──────
    from modules.wa_engine import resolve_mobile, render_wa_trigger

    # Get mobile — works from any context
    mob = resolve_mobile(order=order)                    # from order dict
    mob = resolve_mobile(patient_id="uuid...")           # from DB
    mob = resolve_mobile(order_id="uuid...")             # from DB
    mob = resolve_mobile()                               # from session state

    # Render WhatsApp button/panel
    render_wa_trigger(
        key     = "consult_wa",
        msg     = wa_consultation_msg(...),
        mobile  = mob,
        label   = "📲 Send WhatsApp",
        panel   = False,        # True = expanded panel with editable message
    )
"""

from __future__ import annotations
import streamlit as st
import urllib.parse


# ─────────────────────────────────────────────────────────────────────────────
# MOBILE RESOLVER
# ─────────────────────────────────────────────────────────────────────────────

def resolve_mobile(
    order:      dict | None = None,
    patient_id: str  | None = None,
    order_id:   str  | None = None,
    fallback:   str         = "",
) -> str:
    """
    Find the best mobile number from any available context.
    Returns a clean 10-digit string (no country code, no spaces).
    Returns "" if nothing found.

    Called at every step — consultation, backoffice, wholesale, post-save.
    """
    raw = ""

    # ── 1. From order dict (fastest — already in memory) ─────────────────
    if order and isinstance(order, dict):
        raw = (
            order.get("patient_mobile") or
            order.get("party_mobile")   or
            order.get("mobile")         or
            ""
        )

    # ── 2. From session state (retail / consultation flow) ────────────────
    if not raw:
        raw = (
            st.session_state.get("_erp_patient_mob","")      or  # edit mode
            st.session_state.get("retail_patient_mobile","") or  # retail punching
            st.session_state.get("_ws_patient_mobile","")    or  # wholesale
            ""
        )

    # ── 3. From patient_id → DB ───────────────────────────────────────────
    if not raw and patient_id and len(str(patient_id)) > 10:
        try:
            from modules.sql_adapter import run_query
            rows = run_query(
                "SELECT COALESCE(mobile,'') AS mobile FROM patients "
                "WHERE id=%s::uuid LIMIT 1",
                (str(patient_id),)
            ) or []
            raw = rows[0].get("mobile","") if rows else ""
        except Exception:
            pass

    # ── 4. From order_id → DB ─────────────────────────────────────────────
    if not raw and order_id and len(str(order_id)) > 10:
        try:
            from modules.sql_adapter import run_query
            rows = run_query(
                "SELECT COALESCE(patient_mobile, party_mobile, '') AS mobile "
                "FROM orders WHERE id=%s::uuid LIMIT 1",
                (str(order_id),)
            ) or []
            raw = rows[0].get("mobile","") if rows else ""
        except Exception:
            pass

    # ── 5. Fallback ───────────────────────────────────────────────────────
    if not raw:
        raw = fallback or ""

    return _clean_mobile(raw)


def _clean_mobile(raw: str) -> str:
    """
    Normalise to 10 digits:
      +91XXXXXXXXXX  → XXXXXXXXXX
      91XXXXXXXXXX   → XXXXXXXXXX
      0XXXXXXXXXX    → XXXXXXXXXX
      XXXXXXXXXX     → XXXXXXXXXX (unchanged)
    Returns "" if result is not 10 digits.
    """
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _wa_url(mobile: str, msg: str) -> str:
    """Build WhatsApp URL from clean 10-digit mobile."""
    mob = _clean_mobile(mobile) or mobile  # try cleaning again if passed raw
    if not mob:
        return ""
    return f"https://wa.me/91{mob}?text={urllib.parse.quote(msg)}"


# ─────────────────────────────────────────────────────────────────────────────
# RENDER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def render_wa_trigger(
    key:       str,
    msg:       str,
    mobile:    str  = "",
    order:     dict | None = None,
    patient_id:str  | None = None,
    order_id:  str  | None = None,
    label:     str  = "📲 Send WhatsApp",
    panel:     bool = False,
    show_mobile_field: bool = True,
) -> None:
    """
    Standard WhatsApp trigger used across all modules.

    Resolves mobile from best available source, shows editable mobile field,
    renders send button/panel.

    Args:
        key         : unique Streamlit key
        msg         : pre-built message string
        mobile      : override mobile (optional — resolved automatically if empty)
        order       : order dict (used for resolution)
        patient_id  : patient UUID (used for DB resolution)
        order_id    : order UUID (used for DB resolution)
        label       : button label
        panel       : if True, shows expandable panel with editable message
        show_mobile_field: if True, shows editable mobile input

    Usage:
        from modules.wa_engine import render_wa_trigger
        render_wa_trigger(
            key="bo_order_wa",
            msg=wa_order_confirmed(...),
            order=order,
        )
    """
    # Resolve mobile from best source
    resolved = resolve_mobile(
        order=order, patient_id=patient_id,
        order_id=order_id, fallback=mobile
    )

    if panel:
        _render_wa_panel(key, msg, resolved, label, show_mobile_field)
    else:
        _render_wa_button(key, msg, resolved, label, show_mobile_field)


def _render_wa_button(
    key: str, msg: str, mobile: str, label: str, show_mobile_field: bool
) -> None:
    """Compact: editable mobile + send button on one row."""
    if show_mobile_field:
        c1, c2 = st.columns([1.4, 1])
        with c1:
            mob_key = f"{key}_mob"
            if mob_key not in st.session_state:
                st.session_state[mob_key] = mobile
            _mob = st.text_input(
                "Mobile",
                key=mob_key,
                placeholder="10-digit number",
                label_visibility="collapsed",
            )
        with c2:
            url = _wa_url(_mob, msg)
            if url:
                st.markdown(
                    f"<a href='{url}' target='_blank' style='"
                    "display:block;background:#25D366;color:white;"
                    "text-align:center;padding:7px 10px;border-radius:6px;"
                    "font-weight:700;font-size:0.82rem;text-decoration:none;"
                    "margin-top:4px'>"
                    f"{label}</a>",
                    unsafe_allow_html=True,
                )
            else:
                st.caption("Enter mobile to enable WhatsApp")
    else:
        url = _wa_url(mobile, msg)
        if url:
            st.markdown(
                f"<a href='{url}' target='_blank' style='"
                "display:inline-block;background:#25D366;color:white;"
                "padding:7px 14px;border-radius:6px;font-weight:700;"
                "font-size:0.82rem;text-decoration:none'>"
                f"{label}</a>",
                unsafe_allow_html=True,
            )


def _render_wa_panel(
    key: str, msg: str, mobile: str, label: str, show_mobile_field: bool
) -> None:
    """Expanded panel: editable mobile + editable message + send."""
    mob_key = f"{key}_mob"
    msg_key = f"{key}_msg"

    if mob_key not in st.session_state:
        st.session_state[mob_key] = mobile
    if msg_key not in st.session_state:
        st.session_state[msg_key] = msg

    with st.expander(label, expanded=False):
        c1, c2 = st.columns([2, 3])
        with c1:
            _mob = st.text_input(
                "Mobile", key=mob_key, placeholder="10-digit number"
            )
        with c2:
            _msg_edited = st.text_area(
                "Message (edit if needed)", key=msg_key, height=120
            )
        url = _wa_url(_mob, _msg_edited)
        if url:
            st.markdown(
                f"<a href='{url}' target='_blank' style='"
                "display:block;background:#25d366;color:#fff;"
                "text-align:center;padding:8px;border-radius:6px;"
                "font-weight:700;font-size:.82rem;text-decoration:none'>"
                "📲 Open WhatsApp</a>",
                unsafe_allow_html=True,
            )
        else:
            st.caption("Enter mobile number to enable.")


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE BUILDERS — consultation specific
# ─────────────────────────────────────────────────────────────────────────────

def wa_consultation_msg(
    consultation_id: str,
    fee: float,
    shop_name: str = "",
    shop_phone: str = "",
) -> str:
    """
    Standard consultation WhatsApp message.
    Used in consultation close panel.
    """
    if not shop_name:
        try:
            from modules.settings.shop_master import get_unit_info
            _si = get_unit_info("retail") or {}
            shop_name  = _si.get("shop_name","Parakh Eye Care")
            shop_phone = shop_phone or _si.get("shop_phone","")
        except Exception:
            shop_name = "Parakh Eye Care"

    return (
        f"Thanks for Visiting {shop_name} a state of art Optometry Clinic.\n\n"
        f"Your Consultation ID is *{consultation_id}*.\n\n"
        f"We have received your consultation of *Rs {fee:.0f}*.\n\n"
        f"We will be happy if you see our wide range of frames and lenses "
        f"at our Optical Store and avail great offers and discount.\n\n"
        f"Store this number in your mobile to get regular updates"
        + (f": {shop_phone}" if shop_phone else ".")
    )


# ─────────────────────────────────────────────────────────────────────────────
# VISIT HISTORY LOADER — loads immediately on patient selection
# ─────────────────────────────────────────────────────────────────────────────

def ensure_visit_history_loaded(patient_id: str | None = None) -> list:
    """
    Load patient visit history immediately when patient is selected.
    Caches in session_state["_visit_history_{patient_id}"].

    Called at patient selection — not after backoffice save.
    Returns list of visit dicts.

    Args:
        patient_id: UUID string. If None, reads from session state.
    """
    pid = patient_id or st.session_state.get("retail_patient_id","")
    if not pid or len(str(pid)) < 10:
        return []

    _cache_key = f"_visit_history_{pid}"

    # Return cached if available
    if _cache_key in st.session_state:
        return st.session_state[_cache_key]

    # Load from DB
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                id::text            AS visit_id,
                visit_date::text    AS visit_date,
                COALESCE(visit_name,'')  AS visit_name,
                COALESCE(record_no,'')   AS case_no,
                COALESCE(right_sph::text,'')  AS right_sph,
                COALESCE(right_cyl::text,'')  AS right_cyl,
                COALESCE(right_axis::text,'') AS right_axis,
                COALESCE(right_add::text,'')  AS right_add_power,
                COALESCE(left_sph::text,'')   AS left_sph,
                COALESCE(left_cyl::text,'')   AS left_cyl,
                COALESCE(left_axis::text,'')  AS left_axis,
                COALESCE(left_add::text,'')   AS left_add_power
            FROM patient_visits
            WHERE patient_id = %s::uuid
            ORDER BY visit_date DESC, created_at DESC
        """, (str(pid),)) or []
        st.session_state[_cache_key] = rows
        return rows
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"ensure_visit_history_loaded failed: {e}")
        return []


def invalidate_visit_cache(patient_id: str | None = None) -> None:
    """
    Clear the visit history cache for a patient.
    Call after saving a new visit so next load is fresh.
    """
    pid = patient_id or st.session_state.get("retail_patient_id","")
    if pid:
        st.session_state.pop(f"_visit_history_{pid}", None)
