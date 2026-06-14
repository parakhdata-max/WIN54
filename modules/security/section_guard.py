"""
modules/security/section_guard.py
Place at: WIN54/modules/security/section_guard.py

LIVE SYNC: Every render_guarded_tabs() call writes tab definitions into
st.session_state["_live_tab_registry"][module_key].
Permission Designer reads from there — always in sync with the real page.
Rename a tab in the page file → Designer shows new name on next render.
"""

from __future__ import annotations
import streamlit as st
from typing import Callable


def _get_user_state() -> tuple[str, str]:
    try:
        from modules.security.roles import current_role, current_user_id
        return str(current_user_id() or ""), current_role()
    except Exception:
        return "", "viewer"


def _can_view_section(module_key: str, action_key: str,
                      user_id: str, role: str) -> bool:
    if role == "admin":
        return True
    try:
        from modules.security.permission_engine import can_do
        return can_do(action_key, user_id, role, module_key)
    except Exception as e:
        # Stage 2: FAIL CLOSED. Previously returned True (fail-open) — any
        # import/engine error silently granted everyone everything. Now we
        # deny and log loudly so the failure is caught in testing, never
        # silently bypassed in production.
        try:
            import logging
            logging.error(
                "[section_guard] permission check failed for "
                "module=%s action=%s role=%s — DENYING. %s",
                module_key, action_key, role, e,
            )
        except Exception:
            pass
        return False


def _register_tabs(module_key: str,
                   tab_definitions: list) -> None:
    """
    Write tab definitions into session_state for Permission Designer.
    Called automatically by render_guarded_tabs() before any filtering.

    Written structure:
        st.session_state["_live_tab_registry"]["reports"] = [
            {"key": "view_ledger", "label": "📒 Ledger", "default_roles": [...]},
            ...
        ]
    """
    if "_live_tab_registry" not in st.session_state:
        st.session_state["_live_tab_registry"] = {}

    try:
        from modules.security.page_registry import SUB_TABS
        _defaults = {ak: dr for ak, _, dr in SUB_TABS.get(module_key, [])}
    except Exception:
        _defaults = {}

    st.session_state["_live_tab_registry"][module_key] = [
        {
            "key":           action_key,
            "label":         label,
            "default_roles": _defaults.get(action_key, ["manager", "admin"]),
        }
        for action_key, label, _ in tab_definitions
    ]


def render_guarded_tabs(
    module_key: str,
    tab_definitions: list,
) -> None:
    """
    Renders only the sub-tabs this user is allowed to see.
    Registers tab list for live sync with Permission Designer.

    Args:
        module_key      : e.g. "reports"
        tab_definitions : list of (action_key, label, render_fn)
    """
    _register_tabs(module_key, tab_definitions)  # always register first

    user_id, role = _get_user_state()

    visible = [
        (action_key, label, fn)
        for action_key, label, fn in tab_definitions
        if _can_view_section(module_key, action_key, user_id, role)
    ]

    if not visible:
        st.markdown(
            "<div style='background:#1a0a0a;border:1px solid #374151;"
            "border-radius:8px;padding:20px;text-align:center;margin:20px 0'>"
            "<div style='color:#6b7280;font-size:1.1rem;margin-bottom:6px'>🔒</div>"
            "<div style='color:#4b5563;font-size:0.9rem'>"
            "No sections available for your role.</div>"
            "<div style='color:#374151;font-size:0.78rem;margin-top:4px'>"
            "Contact your administrator to request access.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        return

    hidden_count = len(tab_definitions) - len(visible)
    if hidden_count > 0 and role not in ("admin", "manager"):
        st.caption(
            f"🔒 {hidden_count} section{'s' if hidden_count > 1 else ''} "
            f"not available for your role."
        )

    labels    = [label for _, label, _ in visible]
    functions = [fn    for _, _,  fn in visible]

    tabs = st.tabs(labels)
    for tab, fn in zip(tabs, functions):
        with tab:
            fn()


def check_section_access(module_key: str, action_key: str) -> bool:
    user_id, role = _get_user_state()
    return _can_view_section(module_key, action_key, user_id, role)


def get_live_tabs_for_module(module_key: str) -> list:
    """
    Returns live tab list for a module from session_state.
    Used by Permission Designer.
    Falls back to page_registry if page hasn't been visited this session.

    Returns: [{"key": ..., "label": ..., "default_roles": [...]}, ...]
    """
    live = st.session_state.get("_live_tab_registry", {}).get(module_key)
    if live:
        return live

    # Fallback: static definition from page_registry
    try:
        from modules.security.page_registry import SUB_TABS
        return [
            {"key": ak, "label": lbl, "default_roles": dr}
            for ak, lbl, dr in SUB_TABS.get(module_key, [])
        ]
    except Exception:
        return []
