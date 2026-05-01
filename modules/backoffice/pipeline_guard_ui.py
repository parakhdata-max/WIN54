"""
modules/backoffice/pipeline_guard_ui.py
════════════════════════════════════════════════════════════════════════════
Streamlit UI layer for the pipeline guard.

Place at:  modules/backoffice/pipeline_guard_ui.py

All business logic lives in modules/core/pipeline_guard.py.
This file ONLY contains Streamlit render functions.

Usage:
    from modules.backoffice.pipeline_guard_ui import (
        render_edit_lock_banner,
        render_backstep_ui,
    )
"""

from __future__ import annotations
import streamlit as st
from modules.core.pipeline_guard import (
    EditPermission,
    evaluate_backstep,
    execute_backstep_db,
    BACKSTEP_ALLOWED_FROM,
    BACKSTEP_REQUIRES_ADMIN,
)


def render_edit_lock_banner(perm: EditPermission, context: str = "order"):
    """
    Render the pipeline-aware lock banner + unlock guidance.
    Shows nothing if order is fully open (depth 0).
    """
    if not perm.blocking_message and not perm.guidance:
        return

    depth = perm.max_depth

    # Color by depth
    if depth >= 6:
        bg, border, icon = "#1a0812", "#7c3aed", "🔒"
    elif depth >= 4:
        bg, border, icon = "#1a0a0a", "#ef4444", "🔒"
    elif depth >= 3:
        bg, border, icon = "#1c1107", "#f97316", "⚠️"
    else:
        bg, border, icon = "#0a1628", "#3b82f6", "ℹ️"

    st.markdown(
        f"<div style='background:{bg};border:1px solid {border};"
        f"border-radius:8px;padding:12px 16px;margin-bottom:10px'>"
        f"<div style='color:{border};font-weight:700;margin-bottom:4px'>"
        f"{icon} {perm.blocking_message}</div>"
        + (f"<div style='color:#94a3b8;font-size:0.8rem'>{perm.guidance}</div>"
           if perm.guidance else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    if perm.unlock_steps:
        with st.expander("🔓 How to unlock for editing", expanded=False):
            for i, step in enumerate(perm.unlock_steps, 1):
                st.markdown(
                    f"<div style='padding:3px 0;color:#e2e8f0;font-size:0.82rem'>"
                    f"<span style='color:#60a5fa;font-weight:700'>{i}.</span> {step}</div>",
                    unsafe_allow_html=True,
                )


def render_backstep_ui(job_id: str, current_stage: str, eye_side: str,
                       order_id: str):
    """
    Admin/manager backstep panel — shown inside production_panel per-eye section.

    Shows:
      - Valid backstep target (adjacent stage only)
      - Full cascade consequence analysis
      - Role gate
      - Reason input + two-step confirm
    """
    try:
        from modules.security.roles import has_role
    except Exception:
        def has_role(*args): return True  # fallback if roles module absent

    if not has_role("admin", "manager"):
        return

    target = BACKSTEP_ALLOWED_FROM.get(current_stage)
    if not target:
        return  # no backstep defined for this stage

    result = evaluate_backstep(job_id, current_stage, target, eye_side)

    with st.expander(
        f"🔙 Admin Backstep: {current_stage} → {target}", expanded=False
    ):
        if not result.is_allowed:
            for r in result.blocking_reasons:
                st.error(r)
            return

        role_label = (
            "🔑 Admin only" if result.requires_role == "admin" else "👔 Manager+"
        )
        st.markdown(
            f"<div style='background:#1c1107;border:1px solid #f59e0b;"
            f"border-radius:6px;padding:10px 14px;margin-bottom:8px'>"
            f"<span style='color:#fbbf24;font-weight:700'>⚠️ Backstep Analysis</span>"
            f"<span style='color:#94a3b8;font-size:0.75rem;margin-left:8px'>"
            f"{role_label}</span></div>",
            unsafe_allow_html=True,
        )

        if result.cascade_warnings:
            st.markdown("**Will happen automatically:**")
            for w in result.cascade_warnings:
                color = "#ef4444" if "⚠️" in w else "#94a3b8"
                st.markdown(
                    f"<div style='color:{color};font-size:0.8rem;padding:2px 0'>"
                    f"• {w}</div>",
                    unsafe_allow_html=True,
                )

        if result.manual_steps:
            st.markdown("**You must do manually:**")
            for s in result.manual_steps:
                st.markdown(
                    f"<div style='color:#fbbf24;font-size:0.8rem;padding:2px 0'>"
                    f"📋 {s}</div>",
                    unsafe_allow_html=True,
                )

        # Role gate
        if result.requires_role == "admin" and not has_role("admin"):
            st.error("🔑 Admin role required to execute this backstep.")
            return

        _confirm_key = f"backstep_confirm_{job_id}"
        _reason_key  = f"backstep_reason_{job_id}"

        reason = st.text_input(
            "Reason for backstep (required)",
            key=_reason_key,
            placeholder="e.g. Wrong base curve, customer changed prescription",
        )

        if not st.session_state.get(_confirm_key):
            if st.button(
                f"↩️ Move back to {target}",
                key=f"backstep_btn_{job_id}",
                disabled=not reason.strip(),
                use_container_width=True,
            ):
                st.session_state[_confirm_key] = True
                st.rerun()
        else:
            st.warning(
                f"Confirm: move **{current_stage}** → **{target}** "
                f"for {eye_side} eye? This cannot be automatically undone."
            )
            _c1, _c2 = st.columns(2)
            with _c1:
                if st.button(
                    "✅ Confirm Backstep",
                    key=f"backstep_exec_{job_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    ok, msg = execute_backstep_db(
                        job_id, current_stage, target, reason, order_id
                    )
                    st.session_state.pop(_confirm_key, None)
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
            with _c2:
                if st.button(
                    "✕ Cancel",
                    key=f"backstep_cancel_{job_id}",
                    use_container_width=True,
                ):
                    st.session_state.pop(_confirm_key, None)
                    st.rerun()
