import streamlit as st
import copy

def save_runtime_snapshot():
    """Auto-save critical retail state"""
    st.session_state["_crash_snapshot"] = {
        "cart": copy.deepcopy(st.session_state.get("retail_order_lines", [])),
        "patient": st.session_state.get("retail_patient_name"),
        "mobile": st.session_state.get("retail_patient_mobile"),
        "case": st.session_state.get("retail_case_no"),
    }

def restore_after_crash():
    """Restore if Streamlit hot-reloaded"""
    snap = st.session_state.get("_crash_snapshot")
    if not snap:
        return False

    if not st.session_state.get("retail_order_lines"):
        st.session_state.retail_order_lines = snap.get("cart", [])
        st.session_state.retail_patient_name = snap.get("patient")
        st.session_state.retail_patient_mobile = snap.get("mobile")
        st.session_state.retail_case_no = snap.get("case")
        return True
    return False
