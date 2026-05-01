"""
modules/security/permission_designer_ui.py
══════════════════════════════════════════════════════════════════════════════
Visual Permission Designer — admin configures what each role/user sees & does.

UI LAYOUT:
  Tab bar (top)    : one tab per role  +  "👤 Per User" tab
  Left column      : Sidebar modules — checkboxes (what appears in sidebar)
  Right column     : When sidebar item clicked, shows actions inside that
                     module — checkboxes (what they can do inside the page)

  ┌─────────────────────────────────────────────────────────────────────┐
  │  viewer │ staff │ billing │ lab │ inventory │ manager │ admin │ 👤  │
  ├────────────────────┬────────────────────────────────────────────────┤
  │  SIDEBAR MODULES   │  ACTIONS INSIDE: Retail Order                  │
  │  ☑ Retail Order  ← │  ☑ Punch new order                             │
  │  ☑ Orders          │  ☑ Apply discount (≤20%)                       │
  │  ☐ Backoffice      │  ☐ Discount >20% (needs manager)               │
  │  ☐ Reports         │  ☐ Override price (needs reason)               │
  │  ...               │  ☑ Print receipt                               │
  │                    │                                                │
  │  [Reset to Default]│             [💾 Save Permissions]              │
  └────────────────────┴────────────────────────────────────────────────┘

Place at: modules/security/permission_designer_ui.py
Imported by: admin tab in app.py or user_management_ui.py
"""

from __future__ import annotations
import streamlit as st
import datetime
from typing import Dict, Optional

from modules.security.permission_engine import (
    SIDEBAR_CATALOGUE, ACTION_CATALOGUE, ROLES_ORDERED,
    load_role_module_grants, load_role_action_grants,
    save_role_module_grants, save_role_action_grants,
    save_user_module_grants, save_user_action_grants,
    get_active_acting_roles, grant_acting_role, revoke_acting_role,
    seed_defaults_for_role, get_discount_threshold, set_discount_threshold,
    PRICE_OVERRIDE_REASONS,
)
from modules.security.roles import require_role, current_user_name, current_user_id


def _live_sidebar_catalogue() -> list:
    """
    Returns the sidebar module list that EXACTLY matches app.py's sidebar.
    Priority:
      1. st.session_state["_live_sidebar_registry"]  — written by app.py at startup
         This has the same order, labels, icons, and sections as the real sidebar.
      2. SIDEBAR_CATALOGUE from permission_engine     — fallback if app not running
    """
    live = st.session_state.get("_live_sidebar_registry")
    if live:
        # Convert to SIDEBAR_CATALOGUE format, skip entries with no key mapping
        return [
            {
                "key":          m["key"],
                "label":        m["label"],
                "section":      m["section"],
                "default_roles": next(
                    (p.get("default_roles", [])
                     for p in SIDEBAR_CATALOGUE if p["key"] == m["key"]),
                    []
                ),
            }
            for m in live
            if m.get("key")  # skip any labels that didn't map to a key
        ]
    return SIDEBAR_CATALOGUE


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

SECTION_COLORS = {
    "BILLING":    "#0284c7",
    "PRODUCTION": "#7c3aed",
    "STOCK":      "#059669",
    "ADMIN":      "#dc2626",
}
SECTION_ICONS = {
    "BILLING": "💳", "PRODUCTION": "🔬", "STOCK": "📦", "ADMIN": "⚙️",
}

ROLE_COLORS = {
    "viewer":    "#6b7280",
    "staff":     "#78716c",
    "billing":   "#059669",
    "lab":       "#d97706",
    "inventory": "#dc2626",
    "manager":   "#0284c7",
    "admin":     "#7c3aed",
}
ROLE_ICONS = {
    "viewer":    "👁️", "staff":     "👤",
    "billing":   "💳", "lab":       "🔬",
    "inventory": "📦", "manager":   "🔑",
    "admin":     "👑",
}


def _section_badge(section: str) -> str:
    color = SECTION_COLORS.get(section, "#374151")
    icon  = SECTION_ICONS.get(section, "")
    return (
        f"<span style='background:{color}22;color:{color};border:1px solid {color}44;"
        f"padding:1px 8px;border-radius:20px;font-size:0.65rem;font-weight:700'>"
        f"{icon} {section}</span>"
    )


def _role_pill(role: str) -> str:
    color = ROLE_COLORS.get(role, "#374151")
    icon  = ROLE_ICONS.get(role, "")
    return (
        f"<span style='background:{color};color:#fff;"
        f"padding:3px 12px;border-radius:20px;font-size:0.8rem;font-weight:700'>"
        f"{icon} {role.upper()}</span>"
    )


def _get_actions_for_module(module_key: str) -> list:
    """
    Returns actions + sub-tabs for a module.

    Sub-tab source priority:
      1. st.session_state["_live_tab_registry"][module_key]  — live from page render
      2. page_registry.SUB_TABS                              — static fallback
      3. ACTION_CATALOGUE view_* entries                     — legacy fallback
    """
    # Module-level actions (not sub-tabs)
    actions = [a for a in ACTION_CATALOGUE
               if a["module"] == module_key and not a["key"].startswith("view_")]

    # Sub-tabs: live session state first
    live_tabs = []
    try:
        from modules.security.section_guard import get_live_tabs_for_module
        live_tabs = get_live_tabs_for_module(module_key)
    except Exception:
        pass

    if live_tabs:
        if actions:
            actions.append({"_separator": True, "label": "── Sub-tabs (live from page) ──"})
        for tab in live_tabs:
            actions.append({
                "module":        module_key,
                "key":           tab["key"],
                "label":         tab["label"],
                "default_roles": tab.get("default_roles", ["manager", "admin"]),
                "_is_tab":       True,
            })
    else:
        tab_actions = [a for a in ACTION_CATALOGUE
                       if a["module"] == module_key and a["key"].startswith("view_")]
        if tab_actions:
            if actions:
                actions.append({"_separator": True, "label": "── Sub-tabs ──"})
            actions.extend(tab_actions)

    return actions


def render_permission_designer() -> None:
    """
    Full permission designer page.
    Admin-only. Call from admin settings tab.
    """
    require_role("admin")

    st.markdown("## 🔐 Permission Designer")
    st.markdown(
        "<div style='color:#94a3b8;font-size:0.82rem;margin-bottom:12px'>"
        "Configure exactly what each role sees in the sidebar and what actions "
        "they can perform inside each module. Changes take effect immediately on next login."
        "</div>", unsafe_allow_html=True
    )

    # ── Main tabs ─────────────────────────────────────────────────────────
    role_labels    = [f"{ROLE_ICONS.get(r,'')} {r.upper()}" for r in ROLES_ORDERED]
    all_tabs_labels = role_labels + ["👤 Per User", "⚙️ Settings"]

    tabs = st.tabs(all_tabs_labels)

    for i, role in enumerate(ROLES_ORDERED):
        with tabs[i]:
            _render_role_tab(role)

    with tabs[len(ROLES_ORDERED)]:       # Per User tab
        _render_per_user_tab()

    with tabs[len(ROLES_ORDERED) + 1]:   # Settings tab
        _render_settings_tab()


# ─────────────────────────────────────────────────────────────────────────────
# ROLE TAB
# ─────────────────────────────────────────────────────────────────────────────

def _render_role_tab(role: str) -> None:
    """Two-column layout: sidebar modules | actions inside selected module."""

    # Load current grants from DB (or catalogue defaults if DB empty)
    db_module_grants  = load_role_module_grants(role)
    db_action_grants  = load_role_action_grants(role)

    # Session key for selected module (which module's actions to show on right)
    _sel_key = f"perm_sel_mod_{role}"
    if _sel_key not in st.session_state:
        # Default to first visible module
        for m in _live_sidebar_catalogue():
            if db_module_grants.get(m["key"], role in m.get("default_roles", [])):
                st.session_state[_sel_key] = m["key"]
                break
        else:
            st.session_state[_sel_key] = _live_sidebar_catalogue()[0]["key"] if _live_sidebar_catalogue() else ""

    selected_module = st.session_state.get(_sel_key, "")

    # Header
    st.markdown(
        f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:12px'>"
        f"{_role_pill(role)}"
        f"<span style='color:#94a3b8;font-size:0.78rem'>Configure sidebar visibility "
        f"(left) and in-page actions (right)</span></div>",
        unsafe_allow_html=True,
    )

    if role == "admin":
        st.success("👑 Admin role has access to everything by default. You can restrict specific modules or actions if needed.")

    col_sidebar, col_actions = st.columns([1, 1.6])

    # ── LEFT: Sidebar modules ─────────────────────────────────────────────
    with col_sidebar:
        st.markdown(
            "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
            "padding:10px 12px;margin-bottom:8px'>"
            "<span style='color:#60a5fa;font-weight:700;font-size:0.85rem'>"
            "📋 Sidebar Modules</span><br>"
            "<span style='color:#64748b;font-size:0.7rem'>"
            "Tick = visible in sidebar. Click name to configure actions.</span>"
            "</div>", unsafe_allow_html=True
        )

        new_module_grants: Dict[str, bool] = {}
        current_section = ""

        for mod in _live_sidebar_catalogue():
            section = mod["section"]
            if section != current_section:
                current_section = section
                color = SECTION_COLORS.get(section, "#374151")
                st.markdown(
                    f"<div style='color:{color};font-size:0.65rem;font-weight:800;"
                    f"letter-spacing:.1em;padding:6px 0 2px;border-bottom:1px solid {color}33;'>"
                    f"{SECTION_ICONS.get(section,'')} {section}</div>",
                    unsafe_allow_html=True,
                )

            default = db_module_grants.get(
                mod["key"],
                role in mod.get("default_roles", []) or role == "admin"
            )
            is_selected = (mod["key"] == selected_module)

            mcol1, mcol2 = st.columns([0.12, 0.88])
            with mcol1:
                checked = st.checkbox(
                    "", value=default,
                    key=f"mod_{role}_{mod['key']}",
                    label_visibility="collapsed",
                )
            with mcol2:
                btn_style = (
                    "background:#1e3a5f;border-left:3px solid #3b82f6;"
                    if is_selected else ""
                )
                if st.button(
                    mod["label"],
                    key=f"selmod_{role}_{mod['key']}",
                    use_container_width=True,
                ):
                    st.session_state[_sel_key] = mod["key"]
                    st.rerun()

            new_module_grants[mod["key"]] = checked

    # ── RIGHT: Actions inside selected module ─────────────────────────────
    with col_actions:
        selected_mod_info = next(
            (m for m in _live_sidebar_catalogue() if m["key"] == selected_module), None
        )
        if not selected_mod_info:
            st.info("← Select a module to configure its actions")
        else:
            actions = _get_actions_for_module(selected_module)
            _sec = selected_mod_info.get("section", "")
            _color = SECTION_COLORS.get(_sec, "#374151")

            st.markdown(
                f"<div style='background:#0f172a;border:1px solid {_color}44;border-radius:8px;"
                f"padding:10px 12px;margin-bottom:8px'>"
                f"<span style='color:{_color};font-weight:700;font-size:0.85rem'>"
                f"{selected_mod_info['label']} — Actions</span><br>"
                f"<span style='color:#64748b;font-size:0.7rem'>"
                f"Tick = role can perform this action</span>"
                f"</div>", unsafe_allow_html=True
            )

            if not actions:
                st.caption("No fine-grained actions defined for this module yet.")
            else:
                # ── Legend ───────────────────────────────────────────────
                st.markdown(
                    "<div style='display:flex;gap:12px;margin-bottom:8px;"
                    "padding:5px 8px;background:#0f172a;border-radius:6px;"
                    "font-size:0.68rem;color:#64748b;align-items:center'>"
                    "<span>Legend:</span>"
                    "<span style='background:#15803d22;color:#4ade80;border:1px solid #15803d44;"
                    "padding:1px 7px;border-radius:20px'>✓ granted by default</span>"
                    "<span style='background:#92400e22;color:#fbbf24;border:1px solid #92400e44;"
                    "padding:1px 7px;border-radius:20px'>⚠ manager+ by default</span>"
                    "<span style='background:#1e293b;color:#94a3b8;border:1px solid #334155;"
                    "padding:1px 7px;border-radius:20px'>— not granted by default</span>"
                    "<span style='color:#475569;margin-left:4px'>"
                    "Checkbox overrides the default for this role.</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )

                new_action_grants: Dict[str, bool] = {}
                for act in actions:
                    # ── Separator row ──────────────────────────────────
                    if act.get("_separator"):
                        st.markdown(
                            f"<div style='color:#475569;font-size:0.68rem;font-weight:700;"
                            f"letter-spacing:.08em;padding:8px 0 4px;border-top:1px solid #1e293b;"
                            f"margin-top:4px'>{act['label']}</div>",
                            unsafe_allow_html=True,
                        )
                        continue

                    default = db_action_grants.get(selected_module, {}).get(
                        act["key"],
                        role in act.get("default_roles", []) or role == "admin"
                    )
                    col_chk, col_lbl = st.columns([0.08, 0.92])
                    with col_chk:
                        chk = st.checkbox(
                            "", value=default,
                            key=f"act_{role}_{selected_module}_{act['key']}",
                            label_visibility="collapsed",
                        )
                    with col_lbl:
                        _def_roles = act.get("default_roles", [])
                        _is_tab    = act.get("_is_tab", False)
                        # Badge
                        if role in _def_roles or role == "admin":
                            _badge = ("<span style='background:#15803d22;color:#4ade80;"
                                      "border:1px solid #15803d44;padding:1px 6px;"
                                      "border-radius:10px;font-size:0.62rem;font-weight:600;"
                                      "margin-left:6px'>✓ default</span>")
                        elif any(r in _def_roles for r in ("manager","admin")) and role not in ("manager","admin"):
                            _badge = ("<span style='background:#92400e22;color:#fbbf24;"
                                      "border:1px solid #92400e44;padding:1px 6px;"
                                      "border-radius:10px;font-size:0.62rem;font-weight:600;"
                                      "margin-left:6px'>⚠ manager+</span>")
                        else:
                            _badge = ("<span style='color:#475569;font-size:0.62rem;"
                                      "margin-left:6px'>— not default</span>")

                        # Tab items get a subtle tab icon prefix
                        _prefix = "🗂 " if _is_tab else ""
                        st.markdown(
                            f"{_prefix}**{act['label']}**{_badge}",
                            unsafe_allow_html=True,
                        )
                    new_action_grants[act["key"]] = chk

    # ── Save / Reset buttons ─────────────────────────────────────────────
    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
    btn1, btn2, btn3 = st.columns([2, 1, 1])

    with btn1:
        if st.button(
            f"💾 Save {role.upper()} Permissions",
            key=f"save_role_{role}",
            type="primary",
            use_container_width=True,
        ):
            # Save module grants
            save_role_module_grants(role, new_module_grants)
            # Save action grants for ALL modules (collect from session_state)
            all_action_saves: Dict[str, Dict[str, bool]] = {}
            for mod in _live_sidebar_catalogue():
                acts = _get_actions_for_module(mod["key"])
                if acts:
                    all_action_saves[mod["key"]] = {}
                    for act in acts:
                        if act.get("_separator"):   # skip visual separators
                            continue
                        k = f"act_{role}_{mod['key']}_{act['key']}"
                        val = st.session_state.get(k)
                        if val is not None:
                            all_action_saves[mod["key"]][act["key"]] = val
            save_role_action_grants(role, all_action_saves)
            st.success(f"✅ Permissions saved for {role.upper()}")
            st.rerun()

    with btn2:
        if st.button(
            "↺ Reset to Defaults",
            key=f"reset_role_{role}",
            use_container_width=True,
        ):
            seed_defaults_for_role(role)
            # Clear session state so UI re-reads from DB
            for k in list(st.session_state.keys()):
                if k.startswith(f"mod_{role}_") or k.startswith(f"act_{role}_"):
                    del st.session_state[k]
            st.success(f"↺ Reset to catalogue defaults for {role.upper()}")
            st.rerun()

    with btn3:
        with st.expander("👁 Preview sidebar"):
            st.caption(f"What {role.upper()} sees in the sidebar:")
            current_section2 = ""
            for mod in _live_sidebar_catalogue():
                is_visible = st.session_state.get(
                    f"mod_{role}_{mod['key']}",
                    db_module_grants.get(mod["key"],
                    role in mod.get("default_roles",[]) or role=="admin")
                )
                if is_visible:
                    if mod["section"] != current_section2:
                        current_section2 = mod["section"]
                        st.caption(f"── {current_section2} ──")
                    st.markdown(f"&nbsp;&nbsp;{mod['label']}", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# PER-USER TAB
# ─────────────────────────────────────────────────────────────────────────────

def _render_per_user_tab() -> None:
    """Grant individual users overrides beyond/within their role."""
    st.markdown("### 👤 Per-User Permission Overrides")
    st.markdown(
        "<div style='color:#94a3b8;font-size:0.8rem;margin-bottom:12px'>"
        "Use this to give one person extra access (or restrict them) beyond their role. "
        "<b>Blank = inherit from role.</b> Toggle on = extra access. Toggle off = restricted.</div>",
        unsafe_allow_html=True,
    )

    # Load all users
    try:
        from modules.security.auth import get_all_users
        users = get_all_users() or []
    except Exception:
        st.error("Cannot load users — check auth module")
        return

    if not users:
        st.info("No users found.")
        return

    user_options = {f"{u['display_name']} ({u['username']}) — {u['role'].upper()}": u
                   for u in users}
    selected_label = st.selectbox("Select user", list(user_options.keys()),
                                  key="perm_user_select")
    user = user_options[selected_label]
    user_id  = str(user["id"])
    role     = user.get("role", "viewer")

    # Load role defaults (what their role gives them)
    db_role_mods    = load_role_module_grants(role)
    db_role_actions = load_role_action_grants(role)

    # Load user overrides
    try:
        from modules.sql_adapter import run_query
        user_mod_rows = run_query(
            "SELECT module_key, can_view FROM user_module_grants WHERE user_id=%(u)s::uuid",
            {"u": user_id}
        ) or []
        user_mod_grants = {r["module_key"]: r["can_view"] for r in user_mod_rows}

        user_act_rows = run_query(
            "SELECT module_key, action_key, granted FROM user_action_grants WHERE user_id=%(u)s::uuid",
            {"u": user_id}
        ) or []
        user_act_grants: Dict[str, Dict] = {}
        for r in user_act_rows:
            user_act_grants.setdefault(r["module_key"], {})[r["action_key"]] = r["granted"]
    except Exception:
        user_mod_grants = {}
        user_act_grants = {}

    st.markdown(f"**Configuring:** {user['display_name']}  "
                f"Base role: {role.upper()}")
    st.caption("✅ = has access  ❌ = blocked  — = inherits from role")

    _sel_key2 = f"perm_user_sel_{user_id}"
    if _sel_key2 not in st.session_state:
        st.session_state[_sel_key2] = _live_sidebar_catalogue()[0]["key"]

    col_l, col_r = st.columns([1, 1.6])

    new_user_mod: Dict[str, Optional[bool]] = {}
    with col_l:
        st.markdown("**Sidebar visibility**")
        current_section = ""
        for mod in _live_sidebar_catalogue():
            if mod["section"] != current_section:
                current_section = mod["section"]
                st.caption(f"── {current_section} ──")

            role_default = db_role_mods.get(mod["key"],
                            role in mod.get("default_roles",[]) or role=="admin")
            user_override = user_mod_grants.get(mod["key"])  # None = inherit

            # Three-state selector: inherit / grant / deny
            _opts = ["inherit", "✅ grant", "❌ deny"]
            _cur  = ("✅ grant" if user_override is True
                     else "❌ deny" if user_override is False
                     else "inherit")
            sel = st.radio(
                mod["label"],
                _opts,
                index=_opts.index(_cur),
                key=f"umod_{user_id}_{mod['key']}",
                horizontal=True,
                label_visibility="visible",
            )
            new_user_mod[mod["key"]] = (True if sel == "✅ grant"
                                         else False if sel == "❌ deny"
                                         else None)

            if st.button("↗ Actions", key=f"umod_sel_{user_id}_{mod['key']}",
                         use_container_width=False):
                st.session_state[_sel_key2] = mod["key"]
                st.rerun()

    new_user_act: Dict[str, Dict[str, Optional[bool]]] = {}
    with col_r:
        sel_mod = st.session_state.get(_sel_key2, "")
        acts = _get_actions_for_module(sel_mod)
        mod_info = next((m for m in _live_sidebar_catalogue() if m["key"]==sel_mod), {})
        if acts:
            st.markdown(f"**Actions: {mod_info.get('label','?')}**")
            new_user_act[sel_mod] = {}
            for act in acts:
                role_default = db_role_actions.get(sel_mod, {}).get(
                    act["key"], role in act.get("default_roles",[]) or role=="admin")
                user_override = user_act_grants.get(sel_mod, {}).get(act["key"])
                _opts2 = ["inherit", "✅ grant", "❌ deny"]
                _cur2  = ("✅ grant" if user_override is True
                          else "❌ deny" if user_override is False
                          else "inherit")
                base_txt = "✅ by role" if role_default else "❌ by role"
                sel2 = st.radio(
                    f"{act['label']}  _{base_txt}_",
                    _opts2, index=_opts2.index(_cur2),
                    key=f"uact_{user_id}_{sel_mod}_{act['key']}",
                    horizontal=True,
                )
                new_user_act[sel_mod][act["key"]] = (
                    True if sel2 == "✅ grant"
                    else False if sel2 == "❌ deny"
                    else None
                )

    # Save
    if st.button(f"💾 Save overrides for {user['display_name']}",
                 type="primary", use_container_width=True,
                 key=f"save_user_{user_id}"):
        save_user_module_grants(user_id, {k: v for k, v in new_user_mod.items() if v is not None})
        for mod_key, acts in new_user_act.items():
            save_user_action_grants(user_id, {mod_key: {k: v for k, v in acts.items() if v is not None}})
        st.success(f"✅ Overrides saved for {user['display_name']}")


# ─────────────────────────────────────────────────────────────────────────────
# ACTING ROLES TAB
# ─────────────────────────────────────────────────────────────────────────────

def _render_acting_roles_tab(_ctx: str = "main") -> None:
    """
    Grant and manage temporary elevated roles.
    _ctx: unique string to namespace all widget keys — prevents duplicate key
          errors when this function is called from multiple places on same page.
    """
    st.markdown("### ⚡ Temporary Acting Roles")
    st.markdown(
        "<div style='background:#1c1107;border-left:3px solid #f59e0b;"
        "border-radius:4px;padding:10px 14px;margin-bottom:12px;font-size:0.82rem;color:#fde68a'>"
        "⚡ <b>Use case:</b> Staff member absent today — assign their colleague a "
        "temporary elevated role so work doesn't stop. Role expires automatically at the set time. "
        "No permanent changes to user records.</div>",
        unsafe_allow_html=True,
    )

    # Active acting roles
    active = get_active_acting_roles()
    if active:
        st.markdown("**Currently active acting roles:**")
        for ar in active:
            exp_str = str(ar["expires_at"])[:16] if ar.get("expires_at") else "—"
            col_info, col_revoke = st.columns([4, 1])
            with col_info:
                color = ROLE_COLORS.get(ar["acting_role"].lower(), "#374151")
                st.markdown(
                    f"<div style='background:#0f172a;border-left:3px solid {color};"
                    f"border-radius:4px;padding:6px 12px;margin:3px 0'>"
                    f"<b style='color:#e2e8f0'>{ar['user_name']}</b> "
                    f"<span style='background:{color};color:#fff;padding:1px 8px;"
                    f"border-radius:10px;font-size:0.7rem'>⚡ {ar['acting_role'].upper()}</span>"
                    f"<span style='color:#64748b;font-size:0.72rem;margin-left:8px'>"
                    f"Expires: {exp_str} · Reason: {ar.get('granted_reason','—')}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            with col_revoke:
                if st.button("✕ Revoke", key=f"revoke_{_ctx}_{ar['user_name']}",
                             use_container_width=True):
                    try:
                        from modules.sql_adapter import run_query
                        uid_rows = run_query(
                            "SELECT id FROM erp_users WHERE display_name=%(n)s LIMIT 1",
                            {"n": ar["user_name"]}
                        )
                        if uid_rows:
                            revoke_acting_role(str(uid_rows[0]["id"]))
                            st.success(f"✅ Acting role revoked for {ar['user_name']}")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Revoke failed: {e}")
    else:
        st.caption("No active acting roles right now.")

    st.markdown("---")
    st.markdown("**Grant a temporary acting role:**")

    try:
        from modules.security.auth import get_all_users
        users = get_all_users() or []
    except Exception:
        st.error("Cannot load users")
        return

    user_opts = {f"{u['display_name']} ({u['role'].upper()})": u for u in users}
    _sel = st.selectbox("Select user to elevate", list(user_opts.keys()),
                        key=f"acting_user_select_{_ctx}")
    _user = user_opts[_sel]
    _base_role = _user.get("role", "viewer")
    _base_rank = ["viewer","staff","lab","inventory","billing","manager","admin"].index(
        _base_role) if _base_role in ["viewer","staff","lab","inventory","billing","manager","admin"] else 0

    # Only show roles ABOVE their current role
    _elevatable = [r for r in ["staff","lab","inventory","billing","manager","admin"]
                   if ["viewer","staff","lab","inventory","billing","manager","admin"].index(r)
                   > _base_rank]
    if not _elevatable:
        st.info(f"{_user['display_name']} is already at the highest role (admin).")
        return

    col_role, col_exp = st.columns(2)
    with col_role:
        _acting = st.selectbox("Elevate to role", _elevatable, key=f"acting_role_sel_{_ctx}")
    with col_exp:
        _exp_opts = {
            "End of today":       datetime.datetime.now().replace(hour=23,minute=59),
            "Next 4 hours":       datetime.datetime.now() + datetime.timedelta(hours=4),
            "Next 8 hours":       datetime.datetime.now() + datetime.timedelta(hours=8),
            "Tomorrow end of day":datetime.datetime.now().replace(hour=23,minute=59) + datetime.timedelta(days=1),
            "This week":          datetime.datetime.now() + datetime.timedelta(days=7),
        }
        _exp_label = st.selectbox("Expires", list(_exp_opts.keys()), key=f"acting_exp_sel_{_ctx}")
        _exp_dt = _exp_opts[_exp_label]

    _reason = st.text_input("Reason (required)", key=f"acting_reason_{_ctx}",
                             placeholder="e.g. Rahul is absent today — Priya covering billing")
    if st.button("⚡ Grant Acting Role", type="primary", use_container_width=True,
                 key=f"grant_acting_btn_{_ctx}",
                 disabled=not _reason.strip()):
        me_id = current_user_id()
        ok = grant_acting_role(
            user_id     = str(_user["id"]),
            acting_role = _acting,
            granted_by_id = str(me_id) if me_id else str(_user["id"]),
            reason      = _reason.strip(),
            expires_at  = _exp_dt,
        )
        if ok:
            st.success(
                f"⚡ {_user['display_name']} now has acting role **{_acting.upper()}** "
                f"until {str(_exp_dt)[:16]}"
            )
            st.rerun()
        else:
            st.error("Failed to grant acting role — check DB connection")


# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS TAB
# ─────────────────────────────────────────────────────────────────────────────

def _render_settings_tab() -> None:
    """Global permission settings — thresholds, reasons, flags."""
    st.markdown("### ⚙️ Permission Settings")

    # ── Discount threshold ────────────────────────────────────────────────
    st.markdown("#### 💰 Discount Approval Threshold")
    st.markdown(
        "<span style='color:#94a3b8;font-size:0.8rem'>"
        "Discounts above this percentage require manager approval + logged reason. "
        "Applies across all order types.</span>",
        unsafe_allow_html=True,
    )
    current_thresh = get_discount_threshold()
    new_thresh = st.number_input(
        "Discount threshold (%)",
        min_value=1.0, max_value=100.0,
        value=current_thresh, step=1.0,
        key="discount_threshold_input",
    )
    if st.button("Save Threshold", key="save_threshold"):
        set_discount_threshold(new_thresh, current_user_name())
        st.success(f"✅ Discount threshold set to {new_thresh:.0f}%")

    st.markdown("---")

    # ── Price override reasons ────────────────────────────────────────────
    st.markdown("#### 📝 Price Override Reasons")
    st.markdown(
        "<span style='color:#94a3b8;font-size:0.8rem'>"
        "These reasons appear in the dropdown when manager overrides a price. "
        "Edit the list below (one per line, 'Other' is always added automatically).</span>",
        unsafe_allow_html=True,
    )
    current_reasons = "\n".join(r for r in PRICE_OVERRIDE_REASONS
                                if r not in ("— Select reason —", "Other (specify below)"))
    new_reasons_text = st.text_area(
        "Reasons (one per line)", value=current_reasons, height=160,
        key="override_reasons_input",
    )
    if st.button("Save Reasons", key="save_reasons"):
        # Persist to DB (future: read from DB instead of constant)
        try:
            from modules.security.permission_engine import _w
            reasons_json = [r.strip() for r in new_reasons_text.strip().split("\n") if r.strip()]
            import json
            _w("""
            INSERT INTO permission_settings (key, value, updated_by, updated_at)
            VALUES ('price_override_reasons', %(v)s, %(u)s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                updated_by=EXCLUDED.updated_by, updated_at=NOW()
            """, {"v": json.dumps(reasons_json), "u": current_user_name()})
            st.success(f"✅ {len(reasons_json)} reasons saved")
        except Exception as e:
            st.error(f"Save failed: {e}")

    st.markdown("---")

    # ── Override log viewer ───────────────────────────────────────────────
    st.markdown("#### 📋 Recent Override Log")
    try:
        from modules.sql_adapter import run_query
        log_rows = run_query("""
            SELECT username, action_type, order_no, original_val, new_val,
                   reason, approved_by, created_at
            FROM permission_override_log
            ORDER BY created_at DESC LIMIT 50
        """) or []
        if log_rows:
            import pandas as pd
            df = pd.DataFrame(log_rows)
            df["created_at"] = df["created_at"].astype(str).str[:16]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("No override events logged yet.")
    except Exception:
        st.caption("Override log table not yet created — will appear after first override.")
