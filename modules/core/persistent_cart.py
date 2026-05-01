import streamlit as st
import json

KEY = "_persistent_cart"

def persist_cart():
    try:
        lines = st.session_state.get("retail_order_lines", [])
        if lines:
            st.session_state[KEY] = json.dumps(lines)
        else:
            # Explicitly clear persisted cart so restore_cart doesn't resurrect it
            st.session_state.pop(KEY, None)
    except Exception:
        pass

def restore_cart():
    raw = st.session_state.get(KEY)
    if raw and not st.session_state.get("retail_order_lines"):
        try:
            st.session_state.retail_order_lines = json.loads(raw)
        except Exception:
            pass
