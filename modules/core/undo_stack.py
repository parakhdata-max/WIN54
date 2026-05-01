import streamlit as st
import copy

MAX_UNDO = 10

def push_undo():
    stack = st.session_state.setdefault("_undo_stack", [])
    stack.append(copy.deepcopy(st.session_state.get("retail_order_lines", [])))
    if len(stack) > MAX_UNDO:
        stack.pop(0)

def undo_last():
    stack = st.session_state.get("_undo_stack", [])
    if stack:
        st.session_state.retail_order_lines = stack.pop()
        return True
    return False
