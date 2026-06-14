"""
modules/settings/consultation_settings.py
==========================================
Admin UI: define consultation types and their default fees.

Stored in shop_master / system_flags as:
    key  = "consultation_types"
    value = JSON list of {"name": str, "fee": float}

Usage (from settings page or admin panel):
    from modules.settings.consultation_settings import render_consultation_settings
    render_consultation_settings()
"""

import streamlit as st
import json
import logging

logger = logging.getLogger(__name__)

_DEFAULT_TYPES = [
    {"name": "Consultation",              "fee": 200},
    {"name": "Special Consultation",      "fee": 400},
    {"name": "Low Vision Consultation",   "fee": 500},
    {"name": "Contact Lens Consultation", "fee": 300},
]


def _load_consult_types() -> list:
    """Load consultation types from shop_master / system_flags."""
    try:
        from modules.settings.shop_master import get_unit_info
        _si = get_unit_info("retail") or {}
        _raw = _si.get("consultation_types", "")
        if _raw:
            _parsed = json.loads(_raw) if isinstance(_raw, str) else _raw
            if isinstance(_parsed, list) and _parsed:
                return _parsed
    except Exception:
        pass
    # Fallback: check system_flags
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT value FROM system_flags WHERE key='consultation_types' LIMIT 1"
        ) or []
        if rows and rows[0].get("value"):
            _parsed = json.loads(rows[0]["value"])
            if isinstance(_parsed, list):
                return _parsed
    except Exception:
        pass
    return list(_DEFAULT_TYPES)


def _save_consult_types(types: list) -> bool:
    """Persist consultation types to system_flags and shop_master extra field."""
    _json_val = json.dumps(types)
    try:
        from modules.sql_adapter import run_write
        # system_flags upsert
        run_write("""
            INSERT INTO system_flags (key, value, updated_at)
            VALUES ('consultation_types', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (_json_val,))

        # Also update shop_master if the column / key exists there
        try:
            run_write("""
                UPDATE shop_master SET consultation_types = %s
                WHERE unit_type = 'retail'
                  OR id = (SELECT id FROM shop_master ORDER BY id LIMIT 1)
            """, (_json_val,))
        except Exception:
            pass  # column may not exist; system_flags is the source of truth

        return True
    except Exception as e:
        logger.warning(f"[ConsultSettings] save failed: {e}")
        return False


def render_consultation_settings():
    """
    Render the consultation types admin panel.
    Call from your settings / admin page.
    """
    st.markdown(
        "<div style='background:#0a1628;border-left:4px solid #10b981;"
        "padding:10px 16px;border-radius:6px;margin-bottom:12px'>"
        "<b style='color:#10b981;font-size:1rem'>🩺 Consultation Types & Fees</b>"
        "<div style='color:#94a3b8;font-size:0.78rem;margin-top:3px'>"
        "Define the consultation types available on the consultation screen. "
        "Staff selects the type; the fee auto-fills but can be overridden per patient."
        "</div></div>",
        unsafe_allow_html=True,
    )

    _types = _load_consult_types()

    # Edit grid
    st.markdown("**Current types:**")

    _updated = []
    _delete_idx = None

    for i, t in enumerate(_types):
        _tc1, _tc2, _tc3 = st.columns([3, 1.2, 0.5])
        with _tc1:
            _new_name = st.text_input(
                "Type name",
                value=t.get("name", ""),
                key=f"ct_name_{i}",
                label_visibility="collapsed",
                placeholder="e.g. Consultation, Low Vision, Contact Lens...",
            )
        with _tc2:
            _new_fee = st.number_input(
                "Fee ₹",
                value=float(t.get("fee", 0)),
                min_value=0.0,
                step=50.0,
                key=f"ct_fee_{i}",
                label_visibility="collapsed",
            )
        with _tc3:
            if st.button("🗑️", key=f"ct_del_{i}", help="Remove this type"):
                _delete_idx = i

        if _delete_idx != i:  # keep all except the one being deleted
            _updated.append({"name": _new_name.strip(), "fee": float(_new_fee)})

    st.markdown("")

    # Add new type
    with st.expander("➕ Add new consultation type", expanded=False):
        _an1, _an2 = st.columns([3, 1.2])
        with _an1:
            _new_type_name = st.text_input(
                "New type name",
                key="ct_new_name",
                placeholder="e.g. Paediatric Eye Exam",
            )
        with _an2:
            _new_type_fee = st.number_input(
                "Fee ₹", value=200.0, min_value=0.0, step=50.0,
                key="ct_new_fee",
            )
        if st.button("Add type", key="ct_add_btn", type="primary"):
            if _new_type_name.strip():
                _updated.append({
                    "name": _new_type_name.strip(),
                    "fee": float(_new_type_fee),
                })
                st.session_state.pop("ct_new_name", None)
                st.session_state.pop("ct_new_fee", None)
                if _save_consult_types(_updated):
                    st.success(f"✅ Added '{_new_type_name.strip()}'")
                    st.rerun()
                else:
                    st.error("Save failed — check DB connection")
            else:
                st.warning("Type name cannot be blank")

    # Save button
    st.markdown("")
    _sv1, _sv2 = st.columns([1, 3])
    with _sv1:
        if st.button("💾 Save consultation types", key="ct_save_btn", type="primary",
                     use_container_width=True):
            # Filter blank names
            _to_save = [t for t in _updated if t.get("name","").strip()]
            if not _to_save:
                st.warning("At least one consultation type is required")
            elif _save_consult_types(_to_save):
                st.success(f"✅ Saved {len(_to_save)} consultation type(s)")
                st.rerun()
            else:
                st.error("Save failed — check DB connection")
    with _sv2:
        if st.button("↩️ Reset to defaults", key="ct_reset_btn"):
            if _save_consult_types(list(_DEFAULT_TYPES)):
                st.success("Reset to defaults")
                st.rerun()

    # Preview
    st.markdown("---")
    st.caption("**Preview** — how this appears in the dropdown:")
    for t in _updated:
        if t.get("name","").strip():
            st.caption(f"  · {t['name']}  —  ₹{t.get('fee',0):.0f}")
