# modules/session_manager.py

import streamlit as st


# -----------------------------
# CORE RESET
# -----------------------------

def reset_by_prefix(prefix: str):
    """Delete all session keys starting with prefix"""

    keys = [k for k in st.session_state.keys() if k.startswith(prefix)]

    for k in keys:
        del st.session_state[k]


# -----------------------------
# MODULE RESETS
# -----------------------------

def reset_retail():
    reset_by_prefix("retail_")


def reset_wholesale():
    reset_by_prefix("wholesale_")
    reset_by_prefix("wh_")
    # Clear retail_ keys that wholesale punching shares
    _ws_retail_keys = [
        "retail_patient_name", "retail_patient_id", "retail_patient_mobile",
        "retail_case_no", "retail_new_rx_r", "retail_new_rx_l",
        "retail_right_sph", "retail_right_cyl", "retail_right_axis", "retail_right_add",
        "retail_left_sph",  "retail_left_cyl",  "retail_left_axis",  "retail_left_add",
        "_editing_order_id", "_editing_order_no",
    ]
    for _k in _ws_retail_keys:
        st.session_state.pop(_k, None)


# -----------------------------
# FULL RESET
# -----------------------------

def reset_all():
    """Clear everything except system keys"""

    system_keys = ["active_module"]

    for k in list(st.session_state.keys()):
        if k not in system_keys:
            del st.session_state[k]


# -----------------------------
# AFTER SUBMIT
# -----------------------------

def reset_after_submit(module: str):
    """
    Reset after successful order
    module = retail / wholesale
    """

    if module == "retail":
        reset_retail()

    elif module == "wholesale":
        reset_wholesale()

    else:
        reset_all()

    st.success("🧹 New Order Ready")
    st.rerun()
