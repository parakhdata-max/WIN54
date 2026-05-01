"""
modules/security/user_management_ui.py
========================================
Admin-only User Management panel.

Shows:
  - All users with role badges and status
  - Create new user form
  - Edit role / reset password / enable-disable
  - Role change history
  - Change own password (all users)

Add to Admin tab in loader_ui.py or as a dedicated page:
    from modules.security.user_management_ui import render_user_management
    render_user_management()
"""

import streamlit as st
from modules.security.roles import (
    ADMIN, MANAGER, BILLING, LAB, INVENTORY, VIEWER,
    ALL_ROLES, require_role, has_role, current_user_name, current_user_id,
    current_role,
)

# ── Role display config ────────────────────────────────────────────────────
ROLE_COLORS = {
    ADMIN:     ("#7c3aed", "👑"),   # purple
    MANAGER:   ("#0284c7", "🔑"),   # blue
    BILLING:   ("#059669", "💳"),   # green
    LAB:       ("#d97706", "🔬"),   # amber
    INVENTORY: ("#dc2626", "📦"),   # red
    VIEWER:    ("#6b7280", "👁️"),   # grey
}

ROLE_DESCRIPTIONS = {
    ADMIN:     "Full control — users, settings, pricing, all modules",
    MANAGER:   "Business control — all modules, price overrides, reports",
    BILLING:   "Sales — retail & wholesale punching, billing gate",
    LAB:       "Production — backoffice, lab orders, allocation",
    INVENTORY: "Stock — inventory management, stock adjustments",
    VIEWER:    "Read-only — reports and dashboards only",
}

def _role_badge(role: str) -> str:
    color, icon = ROLE_COLORS.get(role, ("#6b7280", "?"))
    return (
        f"<span style='background:{color};color:white;padding:2px 8px;"
        f"border-radius:10px;font-size:0.75rem;font-weight:600'>"
        f"{icon} {role.upper()}</span>"
    )


def _status_badge(active: bool) -> str:
    if active:
        return "<span style='color:#059669;font-weight:600'>● Active</span>"
    return "<span style='color:#dc2626;font-weight:600'>● Inactive</span>"


# ── Main render ────────────────────────────────────────────────────────────

def render_user_management():
    """
    Full user management panel.
    Page-level guard: admin only for mutations, change-own-password for all.
    """
    from modules.security.auth import (
        get_all_users, create_user, update_user_role,
        reset_user_password, toggle_user_active, change_own_password,
        get_session_user,
    )

    me = get_session_user()
    my_id   = me.get("id")
    my_name = current_user_name()
    my_role = current_role()

    st.markdown("## 👤 User Management")

    # ── Tabs ──────────────────────────────────────────────────────────────
    if my_role == ADMIN:
        tabs = st.tabs([
            "👥 Users",
            "➕ Add User",
            "🔑 Role History",
            "🔒 My Password",
            "🔐 Permissions",      # ← NEW: role + module + action designer
            "⚡ Acting Roles",     # ← NEW: temporary elevated roles
            "🎭 Test as Role",     # ← NEW: preview + test action as any role
        ])
    else:
        tabs = st.tabs(["🔒 Change My Password"])
        _render_change_password_tab(tabs[0], my_id, my_name)
        return

    # ── Tab 1: Users list ─────────────────────────────────────────────────
    with tabs[0]:
        _render_users_list(get_all_users(), my_id, my_name)

    # ── Tab 2: Add user ───────────────────────────────────────────────────
    with tabs[1]:
        _render_add_user(my_name)

    # ── Tab 3: Role history ───────────────────────────────────────────────
    with tabs[2]:
        _render_role_history()

    # ── Tab 4: Change own password ────────────────────────────────────────
    with tabs[3]:
        _render_change_password_tab(tabs[3], my_id, my_name)

    # ── Tab 5: Permission Designer ────────────────────────────────────────
    # Admin configures what each role sees (sidebar) and can do (actions).
    # Two sub-sections inside this tab: Role Permissions + Per-User Overrides.
    with tabs[4]:
        try:
            from modules.security.permission_designer_ui import render_permission_designer
            render_permission_designer()
        except ImportError:
            st.error(
                "Permission Designer not found. "
                "Place permission_designer_ui.py in modules/security/"
            )
        except Exception as e:
            st.error(f"Permission Designer error: {e}")

    # ── Tab 6: Acting Roles ───────────────────────────────────────────────
    # Grant a colleague a temporary elevated role when someone is absent.
    with tabs[5]:
        try:
            from modules.security.permission_designer_ui import _render_acting_roles_tab
            _render_acting_roles_tab(_ctx="usermgmt")
        except ImportError:
            st.error("Acting Roles module not found.")
        except Exception as e:
            st.error(f"Acting Roles error: {e}")

    # ── Tab 7: Test as Role ───────────────────────────────────────────────
    # Admin simulates what any role sees + whether actions are blocked/guarded/allowed.
    with tabs[6]:
        try:
            from modules.security.deep_fixes import render_test_action_as_role
            # Preview sidebar first
            st.markdown("### 👁️ Sidebar Preview + Action Test")
            from modules.security.audit_fixes import render_preview_as_role
            render_preview_as_role()
            st.markdown("---")
            # Then action test
            render_test_action_as_role()
        except ImportError:
            st.error("Test-as-Role module not found. Place deep_fixes.py in modules/security/")
        except Exception as e:
            st.error(f"Test as Role error: {e}")


def _render_users_list(users: list, my_id: str, my_name: str):
    from modules.security.auth import (
        update_user_role, reset_user_password, toggle_user_active
    )

    if not users:
        st.info("No users found. Add one below.")
        return

    # Summary metrics
    active_count = sum(1 for u in users if u.get("is_active"))
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Users", len(users))
    c2.metric("Active", active_count)
    c3.metric("Inactive", len(users) - active_count)

    st.markdown("---")

    # Group by role
    from collections import defaultdict
    by_role = defaultdict(list)
    for u in users:
        by_role[u.get("role", "viewer")].append(u)

    role_order = [ADMIN, MANAGER, BILLING, LAB, INVENTORY, VIEWER]

    for role in role_order:
        role_users = by_role.get(role, [])
        if not role_users:
            continue

        color, icon = ROLE_COLORS.get(role, ("#6b7280", "?"))
        st.markdown(
            f"<h4 style='color:{color};margin-bottom:0.5rem'>"
            f"{icon} {role.upper()} <span style='font-weight:400;font-size:0.85rem;color:#64748b'>"
            f"— {ROLE_DESCRIPTIONS.get(role,'')}</span></h4>",
            unsafe_allow_html=True
        )

        for u in role_users:
            uid         = str(u.get("id", ""))
            username    = u.get("username", "")
            display     = u.get("display_name", username)
            is_active   = u.get("is_active", True)
            last_login  = str(u.get("last_login_at") or "Never")[:16]
            is_me       = (uid == str(my_id))

            with st.expander(
                f"{'🟢' if is_active else '🔴'} {display} "
                f"(@{username}){' ← you' if is_me else ''}"
                f"  |  Last login: {last_login}"
            ):
                col_info, col_actions = st.columns([2, 3])

                with col_info:
                    st.markdown(
                        f"**Username:** `{username}`  \n"
                        f"**Role:** {_role_badge(role)}",
                        unsafe_allow_html=True
                    )
                    st.markdown(
                        f"**Status:** {_status_badge(is_active)}",
                        unsafe_allow_html=True
                    )
                    notes = u.get("notes")
                    if notes:
                        st.caption(f"📝 {notes}")

                with col_actions:
                    # ── Change role ─────────────────────────────────
                    if not is_me:   # can't change own role
                        st.markdown("**Change Role**")
                        new_role = st.selectbox(
                            "New role",
                            sorted(ALL_ROLES),
                            index=sorted(ALL_ROLES).index(role) if role in ALL_ROLES else 0,
                            key=f"role_{uid}",
                            label_visibility="collapsed"
                        )
                        reason = st.text_input(
                            "Reason (optional)",
                            key=f"reason_{uid}",
                            placeholder="e.g. promotion, position change"
                        )
                        if st.button("✅ Update Role", key=f"upd_role_{uid}"):
                            if new_role == role:
                                st.info("No change — same role selected")
                            else:
                                ok, msg = update_user_role(uid, new_role, my_name, reason)
                                if ok:
                                    st.success(f"✅ {msg}")
                                    st.rerun()
                                else:
                                    st.error(f"❌ {msg}")

                    st.markdown("---")

                    # ── Reset password ──────────────────────────────
                    st.markdown("**Reset Password**")
                    new_pw = st.text_input(
                        "New password (min 6 chars)",
                        type="password",
                        key=f"pw_{uid}",
                        placeholder="New password"
                    )
                    if st.button("🔑 Reset Password", key=f"rst_pw_{uid}"):
                        if not new_pw:
                            st.warning("Enter a new password")
                        else:
                            ok, msg = reset_user_password(uid, new_pw, my_name)
                            st.success(f"✅ {msg}") if ok else st.error(f"❌ {msg}")

                    st.markdown("---")

                    # ── Enable/Disable ──────────────────────────────
                    if not is_me:
                        if is_active:
                            if st.button(
                                "⛔ Disable Account", key=f"dis_{uid}",
                                help="Prevents this user from logging in"
                            ):
                                ok, msg = toggle_user_active(uid, False, my_name)
                                st.success(f"✅ {msg}") if ok else st.error(f"❌ {msg}")
                                if ok: st.rerun()
                        else:
                            if st.button("✅ Enable Account", key=f"en_{uid}"):
                                ok, msg = toggle_user_active(uid, True, my_name)
                                st.success(f"✅ {msg}") if ok else st.error(f"❌ {msg}")
                                if ok: st.rerun()

        st.markdown("&nbsp;", unsafe_allow_html=True)


def _render_add_user(my_name: str):
    from modules.security.auth import create_user

    st.markdown("### ➕ Create New User")

    # Role guide
    with st.expander("📋 Role Guide — what each role can access", expanded=False):
        for role in [ADMIN, MANAGER, BILLING, LAB, INVENTORY, VIEWER]:
            color, icon = ROLE_COLORS.get(role, ("#6b7280", "?"))
            st.markdown(
                f"<b style='color:{color}'>{icon} {role.upper()}</b> — "
                f"{ROLE_DESCRIPTIONS.get(role,'')}",
                unsafe_allow_html=True
            )

    st.markdown("---")

    col1, col2 = st.columns(2)
    with col1:
        username     = st.text_input("Username *", placeholder="e.g. ravi.kumar")
        display_name = st.text_input("Full Name *", placeholder="e.g. Ravi Kumar")
    with col2:
        role = st.selectbox(
            "Role *",
            [BILLING, LAB, INVENTORY, MANAGER, VIEWER, ADMIN],
            help="Assign the minimum role needed for this person's job"
        )
        password = st.text_input("Password *", type="password", placeholder="Min 6 characters")

    confirm_pw = st.text_input("Confirm Password *", type="password")
    notes      = st.text_input("Notes (optional)", placeholder="e.g. Counter 2 staff")

    if st.button("➕ Create User", type="primary"):
        if password != confirm_pw:
            st.error("❌ Passwords do not match")
        else:
            ok, msg = create_user(
                username.strip(), display_name.strip(),
                password, role, my_name
            )
            if ok:
                st.success(f"✅ {msg}")
                st.balloons()
                st.rerun()
            else:
                st.error(f"❌ {msg}")


def _render_role_history():
    st.markdown("### 🔑 Role Change History")

    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT target_user, old_role, new_role, changed_by, reason, changed_at
            FROM user_role_changes
            ORDER BY changed_at DESC
            LIMIT 100
        """) or []
    except Exception:
        st.error("Could not load role history — check DB connection")
        return

    if not rows:
        st.info("No role changes recorded yet")
        return

    for r in rows:
        ts      = str(r.get("changed_at", ""))[:19]
        target  = r.get("target_user", "?")
        old_r   = r.get("old_role", "?")
        new_r   = r.get("new_role", "?")
        by      = r.get("changed_by", "?")
        reason  = r.get("reason") or ""

        old_color = ROLE_COLORS.get(old_r, ("#6b7280","?"))[0]
        new_color = ROLE_COLORS.get(new_r, ("#6b7280","?"))[0]

        st.markdown(
            f"`{ts}` **@{target}**: "
            f"<span style='color:{old_color}'>{old_r}</span> → "
            f"<span style='color:{new_color}'><b>{new_r}</b></span> "
            f"by **{by}**"
            + (f" · _{reason}_" if reason else ""),
            unsafe_allow_html=True
        )


def _render_change_password_tab(tab, my_id: str, my_name: str):
    from modules.security.auth import change_own_password

    st.markdown("### 🔒 Change My Password")
    st.info("For security, enter your current password before setting a new one.")

    current_pw = st.text_input("Current Password", type="password", key="_cp_cur")
    new_pw     = st.text_input("New Password (min 6 chars)", type="password", key="_cp_new")
    confirm_pw = st.text_input("Confirm New Password", type="password", key="_cp_conf")

    if st.button("🔑 Change Password", type="primary", key="_cp_submit"):
        if new_pw != confirm_pw:
            st.error("❌ New passwords do not match")
        elif not current_pw:
            st.error("❌ Enter your current password")
        else:
            ok, msg = change_own_password(str(my_id), current_pw, new_pw)
            st.success(f"✅ {msg}") if ok else st.error(f"❌ {msg}")
