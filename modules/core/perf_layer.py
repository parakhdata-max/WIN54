import streamlit as st


def freeze_heavy_blocks(flag="__freeze_ui"):
    """Returns True if UI is frozen → caller should return early"""
    return st.session_state.get(flag, False)


def freeze_ui():
    """Freeze UI to skip expensive re-renders"""
    st.session_state["__freeze_ui"] = True


def unfreeze_ui():
    """Unfreeze UI after expensive operation completes"""
    st.session_state["__freeze_ui"] = False
