"""
modules/ui/smart_loader_ui.py
==============================
Smart Loader UI — Replaces the current upload tab.

Two clear flows:
  ✏️  EDIT  — Download current data → Edit → Preview changes → AI advice → Approve → Apply
  ➕  ADD   — Download blank template → Fill → Upload → Straight to DB

No wrong files accepted. No accidental overwrites. Full audit trail.

Usage in your main app:
    from modules.ui.smart_loader_ui import render_smart_loader
    render_smart_loader()
"""

import io
import streamlit as st
import pandas as pd
from datetime import datetime


# ── Supported file types ──────────────────────────────────────────────────────
FILE_TYPES = {
    "OPHLENS":      {"label": "Ophthalmic Lens",      "icon": "🔍"},
    "CLENS":        {"label": "Contact Lens",          "icon": "👁️"},
    "PRODUCT":      {"label": "Product Master",        "icon": "📦"},
    "PRICE":        {"label": "Price Master",          "icon": "💰"},
    # FRAME intentionally removed — use 🕶️ Frame Loader in sidebar
    # Frame Loader handles your BatchData Excel format (multi-sheet, StartCode, etc.)
    # and writes to the correct tables (products + inventory_stock)
    "PARTY":        {"label": "Party / Supplier",      "icon": "🏢"},
    "PATIENT":      {"label": "Patient Records",       "icon": "🏥"},
    "SOL":          {"label": "Solution / Batch",      "icon": "💊"},
    "BLANK":        {"label": "Blank Inventory",       "icon": "⬜"},
    "MAIN_GROUPS":  {"label": "Main Groups (GST/HSN)", "icon": "🏷️"},
    "OPH_SPEC":     {"label": "Ophthalmic Specs",       "icon": "🔬"},
    "OPH_ADDON":    {"label": "Ophthalmic Add-ons",     "icon": "➕"},
}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════

def render_smart_loader():
    st.title("📥 Smart Data Loader")
    st.caption("Safe, audited imports — no accidental overwrites, full change preview before applying.")

    # ── Module selector ───────────────────────────────────────────────────────
    col1, col2 = st.columns([2, 1])
    with col1:
        type_options = [f"{v['icon']} {v['label']}" for v in FILE_TYPES.values()]
        type_keys    = list(FILE_TYPES.keys())
        selected_idx = st.selectbox(
            "Select Module",
            range(len(type_keys)),
            format_func=lambda i: type_options[i],
            key="smart_loader_type",
        )
        file_type = type_keys[selected_idx]
        cfg       = FILE_TYPES[file_type]

    with col2:
        st.write("")
        st.write("")
        st.info(f"**{cfg['icon']} {cfg['label']}**")

    st.divider()

    # ── Main Groups: special management UI (no import/export flow) ────────────
    if file_type == "MAIN_GROUPS":
        _render_main_groups()
        return

    # ── Two flow tabs ─────────────────────────────────────────────────────────
    tab_edit, tab_add = st.tabs(["✏️  Edit Existing Records", "➕  Add New Records"])

    with tab_edit:
        _render_edit_flow(file_type, cfg)

    with tab_add:
        _render_add_flow(file_type, cfg)


# ══════════════════════════════════════════════════════════════════════════════
# EDIT FLOW
# ══════════════════════════════════════════════════════════════════════════════

def _render_edit_flow(file_type: str, cfg: dict):
    st.markdown("#### ✏️ Edit Existing Records")
    st.caption(
        "Download the current data, make changes in Excel, upload back. "
        "Only system-downloaded files accepted. Full change preview before applying."
    )

    # ── Step 1: Download ──────────────────────────────────────────────────────
    with st.expander("📥 Step 1 — Download Current Data", expanded=True):
        st.write(f"Download current **{cfg['label']}** records. Filter by brand to get focused lists.")
        st.write("Edit the white/blue columns only. Grey (🔒) columns are locked.")

        # ── Brand / Sub-brand filters ─────────────────────────────────────────
        _dl_filters = {}
        if file_type in ("OPH_SPEC", "OPH_ADDON"):
            _desc = {
                "OPH_SPEC": (
                    "**🔬 Ophthalmic Specs** — index × coating × treatment price matrix.\n\n"
                    "Required: **Product**, **Index**, **Coating**, **WLP_per_pair**.\n"
                    "⚠️ Run `migration_ophthalmic_lenses.sql` and upload "
                    "`PRODUCT_OPHTHALMIC.xlsx` first."
                ),
                "OPH_ADDON": (
                    "**➕ Ophthalmic Add-ons** — brand/category/product-level upgrades.\n\n"
                    "Required: **Brand**, **AddonName**. "
                    "Upserts on Brand + AddonName + AppliesTo."
                ),
            }
            st.info(_desc[file_type])

        if file_type in ("PRODUCT", "CLENS", "OPHLENS", "PRICE", "OPH_SPEC", "OPH_ADDON"):
            try:
                from modules.sql_adapter import run_query as _rq
                _brands = _rq("SELECT DISTINCT brand FROM products WHERE brand IS NOT NULL ORDER BY brand") or []
                _brand_list = ["All Brands"] + [r['brand'] for r in _brands if r.get('brand')]
                _sub_brands = _rq("SELECT DISTINCT brand_group FROM products WHERE brand_group IS NOT NULL ORDER BY brand_group") or []
                _sub_list = ["All"] + [r['brand_group'] for r in _sub_brands if r.get('brand_group')]

                fc1, fc2 = st.columns([2, 2])
                with fc1:
                    _sel_brand = st.selectbox("🏷️ Brand", _brand_list,
                                              key=f"dl_brand_{file_type}")
                    if _sel_brand != "All Brands":
                        _dl_filters["brand"] = _sel_brand
                with fc2:
                    _sel_sub = st.selectbox("📦 Sub-brand / Group", _sub_list,
                                            key=f"dl_sub_{file_type}")
                    if _sel_sub != "All":
                        _dl_filters["brand_group"] = _sel_sub
            except Exception:
                pass

        st.markdown("")
        col1, col2 = st.columns([1, 2])
        with col1:
            if st.button("⬇️ Download for Editing", key=f"dl_edit_{file_type}", type="primary"):
                _do_edit_download(file_type, cfg, filters=_dl_filters)

        with col2:
            st.caption(
                "ℹ️ The downloaded file has a 72-hour expiry. "
                "Download a fresh copy if yours is older."
            )

    # ── Step 2: Upload & Preview ───────────────────────────────────────────────
    with st.expander("📤 Step 2 — Upload Your Edited File", expanded=True):
        uploaded = st.file_uploader(
            "Upload your edited file here",
            type=["xlsx"],
            key=f"ul_edit_{file_type}",
            help="Only files downloaded from this system are accepted.",
        )

        if uploaded:
            _handle_edit_upload(uploaded, file_type, cfg)


def _do_edit_download(file_type: str, cfg: dict, filters: dict = None):
    """Generate and serve the fingerprinted edit download with health check."""
    try:
        from modules.loaders.smart.download_manager import (
            build_edit_download, make_edit_filename,
            check_download_health, _fetch_data,
        )
        user = _get_user()

        # ── Pre-download health check ─────────────────────────────────────────
        with st.spinner("Checking data health..."):
            try:
                _df_preview = _fetch_data(file_type, filters or {})
                health      = check_download_health(file_type, _df_preview)
            except Exception:
                health = {"warnings": [], "alerts": [], "healthy": True,
                          "stats": {"total_rows": "?"}}

        # Show alerts (serious issues)
        for alert in health.get("alerts", []):
            st.warning(alert)

        # Show non-blocking warnings
        for warn in health.get("warnings", []):
            st.info(warn)

        # Row count info
        _row_cnt = health.get("stats", {}).get("total_rows", "?")
        if isinstance(_row_cnt, int) and _row_cnt > 0:
            st.caption(f"📊 {_row_cnt:,} record(s) will be included in this download.")

        # ── Build & serve ─────────────────────────────────────────────────────
        with st.spinner("Preparing download..."):
            excel_bytes, file_id = build_edit_download(file_type, user=user,
                                                        filters=filters or {})

        filename = make_edit_filename(file_type)
        st.download_button(
            label     = f"💾 Save {cfg['icon']} {cfg['label']} Edit File",
            data      = excel_bytes,
            file_name = filename,
            mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key       = f"save_edit_{file_type}_{file_id[:8]}",
        )
        st.success(f"✅ File ready. File ID: `{file_id[:8]}...` | Expires in 72 hours.")

    except Exception as e:
        st.error(f"Download failed: {e}")


def _handle_edit_upload(uploaded, file_type: str, cfg: dict):
    """Full edit upload flow: guard → detect → advise → confirm → apply."""
    from modules.loaders.smart.upload_guard import check_upload
    from modules.loaders.smart.schema_validator import smart_process, apply_auto_fixes
    from modules.loaders.smart.ai_change_advisor import advise, answer_question
    from modules.loaders.smart.change_approver import apply_changes

    file_bytes = uploaded.read()
    user       = _get_user()

    # ── Guard check ───────────────────────────────────────────────────────────
    with st.spinner("Verifying file..."):
        guard = check_upload(file_bytes, expected_type=file_type, user=user)

    if not guard.allowed:
        for issue in guard.issues:
            st.error(issue)
        st.stop()
        return

    if guard.flow != "EDIT":
        st.error("⛔ This looks like an ADD template, not an EDIT file. Use the 'Add New Records' tab instead.")
        st.stop()
        return

    for w in guard.warnings:
        st.warning(w)

    st.success("✅ File verified — this is a valid system-downloaded edit file.")

    # ── Detect changes (with schema validation) ───────────────────────────────
    # If the user already applied column fixes this session, use the fixed df.
    _fix_key     = f"schema_fix_{file_type}_{uploaded.name}"
    _applied_key = f"schema_applied_{file_type}_{uploaded.name}"
    _active_df   = st.session_state.get(_fix_key, guard.df)

    # ── Learning Memory: auto-apply known fixes silently ──────────────────────
    # If we've seen these column names before for this file type, fix them
    # automatically without asking the user. Show a quiet notice only.
    try:
        from modules.loaders.smart.learning_memory import auto_apply_memory
        _active_df, _auto_applied, _ = auto_apply_memory(
            file_type, _active_df,
            type("_R", (), {"auto_fixes": {}})(),
            user=user,
        )
        if _auto_applied:
            st.caption(
                f"🧠 Memory: {len(_auto_applied)} column name(s) auto-corrected from past sessions "
                f"({', '.join(_auto_applied.keys())})"
            )
    except Exception:
        pass  # memory is non-blocking — never fail an upload due to this

    with st.spinner("Scanning for changes..."):
        _active_df, report = smart_process(_active_df, file_type)

    # ── Unified Control Panel ─────────────────────────────────────────────────
    render_control_panel(report)

    # ── HARD BLOCK — critical missing columns ─────────────────────────────────
    if getattr(report, "critical_errors", []):
        st.error("⛔ **Critical Issues — Import Cannot Proceed**")
        for ce in report.critical_errors:
            st.write(f"🔴 {ce}")
        _offer_error_download(report)
        st.stop()
        return

    # ── Schema Fix Panel — shown when columns need renaming ──────────────────
    _fixes_applied = st.session_state.get(_applied_key, False)

    if _fixes_applied:
        st.success(
            "✅ Column fixes applied — detection re-ran on cleaned data. "
            "Review changes below."
        )
    elif hasattr(report, "auto_fixes") and report.auto_fixes:
        with st.expander("⚙️ Column Name Issues Detected — Action Needed", expanded=True):
            st.warning(
                "**Some columns in your file don't exactly match the system's expected names.** "
                "This can cause changes to go undetected. "
                "Review the table below and click **Apply Suggested Fixes** to auto-correct."
            )

            if hasattr(report, "preview_diff") and report.preview_diff:
                import pandas as _pd

                _diff_df = _pd.DataFrame(report.preview_diff)

                def _colour_diff_row(row):
                    if row["Action"] == "Rename":
                        return ["background-color: #fff3cd; color: #856404"] * len(row)
                    elif row["Action"] == "Ignored":
                        return ["background-color: #f8d7da; color: #721c24"] * len(row)
                    return [""] * len(row)

                st.dataframe(
                    _diff_df.style.apply(_colour_diff_row, axis=1),
                    use_container_width=True,
                    hide_index=True,
                )
                st.caption(
                    "🟡 Yellow = will be renamed to match system config  "
                    "| 🔴 Red = not tracked, will stay ignored"
                )

            if st.button(
                "🔧 Apply Suggested Fixes",
                key=f"apply_schema_fix_{file_type}_{uploaded.name}",
                type="primary",
            ):
                from modules.loaders.smart.schema_validator import apply_auto_fixes as _apply_fixes
                _fixed_df = _apply_fixes(guard.df.copy(), report.auto_fixes)
                st.session_state[_fix_key]     = _fixed_df
                st.session_state[_applied_key] = True
                # ── Save to learning memory so next time these fixes are automatic ──
                try:
                    from modules.loaders.smart.learning_memory import record_mappings
                    saved = record_mappings(file_type, report.auto_fixes, user=user)
                    if saved:
                        st.toast(f"🧠 {saved} fix(es) saved to memory — auto-applied next time")
                except Exception:
                    pass
                st.rerun()

    elif hasattr(report, "schema_suggestions") and report.schema_suggestions:
        # Has suggestions but no auto-fixable renames → show info only
        with st.expander("ℹ️ Schema Notes", expanded=False):
            for _s in report.schema_suggestions:
                st.write(_s)

    # ── Show untracked column warnings ──────────────────────────────────────
    if hasattr(report, "untracked_cols") and report.untracked_cols:
        st.warning(
            f"⚠️ **{len(report.untracked_cols)} column(s) in your file are NOT tracked by the system** "
            f"and will be ignored:\n\n"
            f"🔴 `{'`, `'.join(report.untracked_cols)}`\n\n"
            f"These columns are either not in the system config or require a schema update. "
            f"Contact support if you believe these should be editable."
        )
        # Show highlighted preview of uploaded file
        with st.expander("👁️ Preview — Red columns are ignored by system", expanded=True):
            try:
                def _highlight_untracked(col):
                    if col.name in report.untracked_cols:
                        return ["background-color: #ffcccc; color: #cc0000; font-weight: bold"] * len(col)
                    return [""] * len(col)
                preview_df = guard.df.copy()
                preview_df.columns = [c.replace("🔒 ", "").strip() for c in preview_df.columns]
                styled = preview_df.head(10).style.apply(_highlight_untracked, axis=0)
                st.dataframe(styled, use_container_width=True, hide_index=True)
                st.caption("🔴 Red columns = ignored by system | White columns = tracked and compared")
            except Exception:
                st.dataframe(guard.df.head(10), use_container_width=True, hide_index=True)

    if not report.has_changes and not report.has_blocked:
        if report.rows_not_found:
            st.warning(
                f"⚠️ No changes detected — but **{len(report.rows_not_found)} row(s) could not be matched** "
                f"to any record in the database. This is usually a key-matching bug.\n\n"
                f"Rows not found: `{'`, `'.join(report.rows_not_found[:10])}`\n\n"
                f"Please report this to support with your file attached."
            )
        elif hasattr(report, "untracked_cols") and report.untracked_cols:
            st.info(
                "ℹ️ No changes detected in tracked columns. "
                "If you were trying to edit the red columns above, "
                "those are not currently tracked by the system."
            )
        else:
            st.info("ℹ️ No changes detected. The uploaded file matches the current database. Nothing to update.")
        return

    # ── Remind about untracked cols if changes exist ──────────────────────────
    if hasattr(report, "untracked_cols") and report.untracked_cols and report.has_changes:
        st.info(
            f"ℹ️ Note: {len(report.untracked_cols)} column(s) were ignored "
            f"(`{'`, `'.join(report.untracked_cols)}`). "
            "Only tracked columns are shown in changes below."
        )

    # ── Show blocked changes ──────────────────────────────────────────────────
    if report.has_blocked:
        st.warning(
            f"⛔ {len(report.blocked)} change(s) to locked fields will be **ignored** "
            "(identity fields cannot be changed)."
        )
        with st.expander("See ignored locked-field changes"):
            blocked_df = pd.DataFrame([{
                "Row": b.row_index, "Field": b.field_name,
                "Old": b.old_value, "Attempted New": b.new_value
            } for b in report.blocked])
            st.dataframe(blocked_df, use_container_width=True, hide_index=True)

    # ── AI Advisor ────────────────────────────────────────────────────────────
    advice = advise(report)

    # Propagate backup decision from advisor → report (change_approver reads report.backup_required)
    report.backup_required = advice.backup_required

    # Warn if any rows could not be matched to DB records
    if report.rows_not_found:
        st.warning(
            f"⚠️ {len(report.rows_not_found)} row(s) not found in DB — "
            "they will be skipped. This can happen if a record was deleted since download. "
            f"First few: {', '.join(report.rows_not_found[:5])}"
        )

    st.markdown("---")
    st.markdown("### 🔍 Change Preview")

    # Summary metrics
    rc = report.risk_counts
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Changes",   len(report.changes))
    c2.metric("🟢 Safe",          rc.get("SAFE", 0))
    c3.metric("🟡 Caution",       rc.get("CAUTION", 0))
    c4.metric("🔴 Warning",       rc.get("WARNING", 0))

    # Summary text
    _render_advice_box(advice)

    # Detailed change table
    with st.expander(f"📋 See all {len(report.changes)} field changes", expanded=report.risk_counts.get("WARNING", 0) > 0):
        change_df = report.to_dataframe()
        if not change_df.empty:
            st.dataframe(
                change_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Risk": st.column_config.TextColumn("Risk", width="small"),
                    "Old Value": st.column_config.TextColumn("Old Value"),
                    "New Value": st.column_config.TextColumn("New Value"),
                }
            )

    # ── Ask AI section ────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 💬 Ask About These Changes")
    question = st.text_input(
        "Type your question (e.g. 'what will change?', 'is it safe?', 'explain box_size')",
        key=f"ai_question_{file_type}",
        placeholder="what will change? | is it safe? | will old records change? | explain box_size",
    )
    if question:
        answer = answer_question(question, report)
        st.info(f"💡 {answer}")

    # ── Decision Guidance ─────────────────────────────────────────────────────
    st.markdown("---")
    _decision_state = render_decision_guidance(report)

    # ── Inline Edit + Approve Grid (enforced: approval filters report.changes) ─
    _key_pfx   = f"{file_type}_{uploaded.name}"
    _edited_df = render_inline_edit_grid(report, file_type=file_type,
                                         key_prefix=_key_pfx)

    # ENFORCEMENT: updates report.changes with edited values + removes rejected rows
    # Stamps approved_by + manually_edited on every surviving FieldChange
    if _edited_df is not None:
        report = apply_grid_edits_to_report(report, _edited_df, user=user)

    # ── Pre-Apply Validation Shield ────────────────────────────────────────────
    # Runs the same checks process_upload does — catches bad values BEFORE commit.
    # This closes the gap: inline edits now go through full validation, not just
    # the change_approver's direct UPDATE path.
    _validation_errors = _validate_approved_changes(report, file_type)
    if _validation_errors:
        st.error("⛔ **Validation failed — fix these before committing:**")
        for ve in _validation_errors:
            st.error(f"• {ve}")
        st.stop()
        return

    # ── AI value suggestions (if any flagged) ─────────────────────────────────
    _ai_hints = [s for s in getattr(report, "schema_suggestions", []) if s.startswith("💡")]
    if _ai_hints:
        with st.expander("🧠 AI Value Suggestions", expanded=False):
            for hint in _ai_hints:
                st.write(hint)

    # ── Download error / diff report ──────────────────────────────────────────
    _offer_error_download(report)

    # ── GUIDED APPROVAL FLOW ──────────────────────────────────────────────────
    st.markdown("---")
    if _decision_state != "BLOCK":
        _render_guided_approval(report, advice, file_type, user, guard, uploaded,
                                apply_changes)


def _render_guided_approval(report, advice, file_type, user, guard, uploaded,
                             apply_changes):
    """
    Guided approval — system decides how much friction to apply:

    🟢 ALL SAFE    → pre-commit summary + single "Apply Now"
    🟡 HAS CAUTION → pre-commit summary + simple Yes/No confirm
    🔴 HAS WARNING → pre-commit summary + must type CONFIRM
    ⛔ BLOCKED     → never reached (filtered by caller)
    """
    rc          = report.risk_counts
    highest     = report.highest_risk
    key_prefix  = f"approve_{file_type}_{uploaded.name}"
    n_changes   = len(report.changes)

    if n_changes == 0:
        st.info("ℹ️ No approved changes to commit.")
        return

    # ── Pre-Commit Summary (shown for ALL risk levels) ────────────────────────
    st.markdown("### ✅ Commit Summary")
    _fields   = sorted({c.field_name  for c in report.changes})
    _records  = sorted({c.entity_key  for c in report.changes})
    _safe_n   = rc.get("SAFE",    0)
    _caut_n   = rc.get("CAUTION", 0)
    _warn_n   = rc.get("WARNING", 0)

    _summary_color = "error" if _warn_n > 0 else ("warning" if _caut_n > 0 else "success")
    getattr(st, _summary_color)(
        f"**You are about to apply {n_changes} change(s) to {len(_records)} record(s).**\n\n"
        f"Fields: `{'`, `'.join(_fields) if _fields else '—'}`\n\n"
        f"Risk breakdown: 🟢 Safe: {_safe_n} | 🟡 Caution: {_caut_n} | 🔴 Warning: {_warn_n}"
    )

    if advice.backup_required:
        st.info("💾 A backup snapshot will be taken automatically before applying.")

    _render_advice_box(advice)

    st.markdown("---")

    # ══ GUIDED FLOW BY RISK LEVEL ═════════════════════════════════════════════

    # ── 🟢 ALL SAFE — single button ──────────────────────────────────────────
    if highest == "SAFE":
        col1, col2 = st.columns([1, 2])
        with col1:
            if st.button("⚡ Apply Now", type="primary",
                         key=f"safe_apply_{key_prefix}",
                         use_container_width=True):
                _execute_apply(report, user, guard, file_type, apply_changes)
        with col2:
            if st.button("❌ Cancel", key=f"safe_cancel_{key_prefix}",
                         use_container_width=True):
                st.warning("❌ Import cancelled. No changes made.")
                st.stop()
                return

    # ── 🟡 CAUTION — single confirm checkbox + apply ─────────────────────────
    elif highest == "CAUTION" and rc.get("WARNING", 0) == 0:
        _confirm_caut = st.checkbox(
            "I have reviewed the changes and want to proceed",
            key=f"confirm_caut_{key_prefix}",
        )

        if st.button("🔍 Dry Run First", key=f"dry_caut_{key_prefix}"):
            with st.spinner("Simulating..."):
                dry = apply_changes(report, user=user, dry_run=True)
            st.success(f"🔍 Simulation: {dry.applied} change(s) would be applied.")
            for e in dry.errors:
                st.warning(f"⚠️ {e}")

        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("✅ Apply Changes", type="primary",
                         key=f"caution_apply_{key_prefix}",
                         disabled=not _confirm_caut,
                         use_container_width=True):
                _execute_apply(report, user, guard, file_type, apply_changes)
        with col2:
            if st.button("❌ Cancel", key=f"caution_cancel_{key_prefix}",
                         use_container_width=True):
                st.warning("❌ Import cancelled. No changes made.")
                st.stop()
                return

    # ── 🔴 WARNING — dry run + typed CONFIRM required ────────────────────────
    else:
        if st.button("🔍 Dry Run — Simulate Without Saving",
                     key=f"dry_warn_{key_prefix}"):
            with st.spinner("Simulating..."):
                dry = apply_changes(report, user=user, dry_run=True)
            st.success(f"🔍 Dry run: {dry.applied} change(s) would be applied.")
            for e in dry.errors:
                st.warning(f"⚠️ {e}")

        st.markdown(
            "⚠️ **High-risk changes detected.** "
            "Type **CONFIRM** to unlock the Apply button."
        )
        typed = st.text_input(
            "Type CONFIRM to proceed:",
            key=f"typed_{key_prefix}",
            placeholder="CONFIRM",
        )
        can_proceed = typed.strip().upper() == "CONFIRM"
        if typed and not can_proceed:
            st.error("❌ Type exactly: CONFIRM")

        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("🔴 Apply High-Risk Changes", type="primary",
                         disabled=not can_proceed,
                         key=f"warn_apply_{key_prefix}",
                         use_container_width=True):
                _execute_apply(report, user, guard, file_type, apply_changes)
        with col2:
            if st.button("❌ Cancel", key=f"warn_cancel_{key_prefix}",
                         use_container_width=True):
                st.warning("❌ Import cancelled. No changes made.")
                st.stop()
                return

    # ── Undo last commit ──────────────────────────────────────────────────────
    _last_bid = st.session_state.get("last_backup_id")
    if _last_bid:
        st.markdown("---")
        _label = st.session_state.get("last_backup_label", "last commit")
        if st.button(f"↩️ Undo Last Commit ({_label})", key=f"undo_{file_type}"):
            from modules.analytics.change_analytics import undo_commit
            undo_result = undo_commit(_last_bid, user=user)
            if undo_result["success"]:
                st.success(f"✅ Reverted {undo_result['reverted']} change(s) successfully.")
                st.session_state.pop("last_backup_id",    None)
                st.session_state.pop("last_backup_label", None)
            else:
                st.error(f"❌ Undo failed: {'; '.join(undo_result['errors'])}")


def _execute_apply(report, user, guard, file_type, apply_changes):
    """
    Shared apply + live refresh logic.

    Flow:
    1. apply_changes() → routes through change_approver (audit + backup + undo)
    2. On success: re-fetch updated DB values for committed records
    3. Update inline grid session state (live refresh — no full reload flicker)
    4. Show toast + success inline, no blocking message
    """
    with st.spinner("Applying changes..."):
        result = apply_changes(report, user=user, dry_run=False)

    if result.success:
        guard.consume(user)

        # ── Live grid refresh — re-fetch only committed records ───────────────
        _grid_key        = f"inline_grid_{file_type}_{user}"
        _entity_keys     = {c.entity_key for c in report.changes}
        _refreshed       = {}

        try:
            _refreshed = _fetch_refreshed_values(_entity_keys, file_type)
        except Exception:
            pass

        if _refreshed and _grid_key in st.session_state:
            _grid = st.session_state[_grid_key].copy()
            for i, row in _grid.iterrows():
                _rec = str(row.get("Record",""))
                _fld = str(row.get("Field",""))
                if _rec in _refreshed:
                    _new_db_val = _refreshed[_rec].get(_fld)
                    if _new_db_val is not None:
                        _grid.at[i, "DB Value"]  = _new_db_val
                        _grid.at[i, "New Value"] = _new_db_val   # reset after commit
                        _grid.at[i, "Approve"]   = True
            st.session_state[_grid_key] = _grid
            # Show non-blocking toast
            st.toast(
                f"✅ {result.applied} change(s) applied — grid updated",
                icon="✅",
            )
        else:
            st.toast(f"✅ {result.applied} change(s) applied", icon="✅")

        # Persistent confirmation
        st.success(
            f"✅ Done! {result.applied} field change(s) applied."
            + (f" | Backup ID: `{result.backup_id[:8]}...`" if result.backup_id else "")
        )
        st.info("ℹ️ File consumed. Download a fresh copy to make further changes.")

        if result.errors:
            for e in result.errors:
                st.warning(f"⚠️ {e}")

        if result.backup_id:
            st.session_state["last_backup_id"]    = result.backup_id
            st.session_state["last_backup_label"] = (
                f"{file_type} — {len(report.changes)} change(s)"
            )

        # Rerun once to show refreshed grid — feels instant because data is in session state
        st.rerun()

    else:
        st.error(f"❌ Import failed: {'; '.join(result.errors)}")


# ══════════════════════════════════════════════════════════════════════════════
# ADD FLOW
# ══════════════════════════════════════════════════════════════════════════════

def _render_add_flow(file_type: str, cfg: dict):
    st.markdown("#### ➕ Add New Records")
    st.caption(
        "Download a blank template, fill in new records, upload. "
        "This flow ONLY adds new records — it cannot modify existing data."
    )




    # ── Step 1: Download template ─────────────────────────────────────────────
    with st.expander("📥 Step 1 — Download Blank Template", expanded=True):
        st.write(f"Download a blank **{cfg['label']}** template with all required columns.")
        st.write("Delete the orange example row before filling in your data.")

        if st.button("⬇️ Download Blank Template", key=f"dl_add_{file_type}", type="primary"):
            _do_add_download(file_type, cfg)

    # ── Step 2: Upload ────────────────────────────────────────────────────────
    with st.expander("📤 Step 2 — Upload Filled Template", expanded=True):
        uploaded = st.file_uploader(
            "Upload your filled template",
            type=["xlsx"],
            key=f"ul_add_{file_type}",
            help="Upload the blank template after filling in new records.",
        )

        if uploaded:
            _handle_add_upload(uploaded, file_type, cfg)




# ══════════════════════════════════════════════════════════════════════════════
# FRAME QUICK ADD — manual entry form, no Excel required
# ══════════════════════════════════════════════════════════════════════════════

def _render_frame_quick_add():
    """
    Single-frame entry form.  Writes directly to the frames table via
    universal_loader_core so dedup and audit are identical to Excel upload.
    """
    from modules.sql_adapter import run_query, run_write
    import uuid as _uuid

    st.markdown(
        "<div style='background:#f0f9ff;border:1px solid #bae6fd;border-radius:9px;"
        "padding:14px 18px;margin-bottom:10px'>"
        "<b style='color:#0369a1'>⚡ Quick Add — Enter Frame Directly</b>"
        "<span style='color:#64748b;font-size:0.78rem;margin-left:8px'>"
        "No Excel needed for single frames</span></div>",
        unsafe_allow_html=True
    )

    with st.form("frame_quick_add_form", clear_on_submit=True):
        # Row 1 — Identity (product_name links to existing product in products table)
        c1, c2, c3 = st.columns([4, 2, 2])
        product_name = c1.text_input("Product Name *", placeholder="e.g. Butler 8305 Black")
        brand        = c2.text_input("Brand", placeholder="e.g. Butler")
        sku          = c3.text_input("Batch / SKU", placeholder="e.g. FR-BUT-8305 (auto if blank)")

        # Row 2 — Pricing + Stock
        c4, c5, c6, c7, c8 = st.columns([2, 2, 2, 1.5, 1.5])
        cost_price    = c4.number_input("Cost Price ₹", min_value=0.0, step=0.5, format="%.2f")
        selling_price = c5.number_input("Selling Price ₹", min_value=0.0, step=0.5, format="%.2f")
        mrp           = c6.number_input("MRP ₹ *", min_value=0.0, step=0.5, format="%.2f")
        gst_pct       = c7.selectbox("GST %", [0, 5, 12, 18, 28], index=0)
        qty           = c8.number_input("Qty *", min_value=0, step=1, value=1)

        submitted = st.form_submit_button("➕ Add Frame", type="primary", use_container_width=False)

    if submitted:
        errors = []
        if not product_name.strip(): errors.append("Product Name is required")
        if mrp <= 0:                 errors.append("MRP must be > 0")
        if qty < 0:                  errors.append("Qty cannot be negative")

        if errors:
            for e in errors:
                st.error(f"❌ {e}")
            return

        # Frames live in inventory_stock JOIN products (category='frame')
        # Step 1: find or create the product record
        try:
            prod_rows = run_query(
                "SELECT id FROM products WHERE product_name=%s AND LOWER(category)='frame' LIMIT 1",
                (product_name.strip(),)
            )
            if prod_rows:
                prod_id = str(prod_rows[0]["id"])
            else:
                # Create product master entry for this frame
                prod_id = str(_uuid.uuid4())
                run_write("""
                    INSERT INTO products
                    (id, product_name, brand, category, main_group,
                     unit, is_active, gst_percent, created_at)
                    VALUES (%s,%s,%s,'Frame','Frame',%s,true,%s,NOW())
                    ON CONFLICT DO NOTHING
                """, (prod_id, product_name.strip(), brand.strip(), 'PCS', round(float(gst_pct), 2)))
                # re-fetch in case ON CONFLICT hit
                prod_rows2 = run_query(
                    "SELECT id FROM products WHERE product_name=%s AND LOWER(category)='frame' LIMIT 1",
                    (product_name.strip(),)
                )
                if prod_rows2:
                    prod_id = str(prod_rows2[0]["id"])
        except Exception as ex:
            st.error(f"❌ Product lookup failed: {ex}")
            return

        # Step 2: check for existing inventory_stock row for this product
        batch_key = sku.strip().upper() if sku.strip() else f"FRAME-{product_name.strip()[:10].upper().replace(' ','-')}"
        try:
            stock_rows = run_query(
                "SELECT id, quantity FROM inventory_stock WHERE product_id=%s AND batch_no=%s LIMIT 1",
                (prod_id, batch_key)
            )
            if stock_rows:
                old_qty = int(stock_rows[0].get("quantity") or 0)
                new_qty = old_qty + qty
                run_write("""
                    UPDATE inventory_stock SET
                        quantity=%s, purchase_rate=%s,
                        selling_price=%s, mrp=%s,
                        is_active=true, updated_at=NOW()
                    WHERE product_id=%s AND batch_no=%s
                """, (new_qty, round(float(cost_price), 2),
                      round(float(selling_price), 2), round(float(mrp), 2),
                      prod_id, batch_key))
                st.success(f"✅ Stock updated — **{product_name.strip()}** qty: {old_qty} → **{new_qty}** | MRP ₹{mrp:,.2f}")
            else:
                run_write("""
                    INSERT INTO inventory_stock
                    (id, product_id, batch_no, quantity,
                     purchase_rate, selling_price, mrp,
                     stock_type, item_type,
                     is_active, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'SIMPLE','STOCK',true,NOW(),NOW())
                """, (
                    str(_uuid.uuid4()), prod_id, batch_key,
                    qty,
                    round(float(cost_price), 2),
                    round(float(selling_price), 2),
                    round(float(mrp), 2)
                ))
                st.success(f"✅ Frame added — **{product_name.strip()}** | Batch: {batch_key} | MRP ₹{mrp:,.2f} | Qty: {qty}")
        except Exception as ex:
            st.error(f"❌ Stock insert failed: {ex}")

    # ── Live frame stock preview ──────────────────────────────────────────────
    with st.expander("📋 Current Frame Stock", expanded=False):
        try:
            from modules.sql_adapter import run_query as _rq
            rows = _rq("""
                SELECT
                    p.product_name, p.brand,
                    s.batch_no,
                    s.quantity              AS qty,
                    s.purchase_rate         AS cost_price,
                    s.selling_price,
                    s.mrp,
                    p.gst_percent,
                    s.is_active
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE LOWER(p.category) = 'frame'
                  AND COALESCE(s.is_active, true) = true
                ORDER BY p.brand, p.product_name
                LIMIT 200
            """) or []
            if rows:
                import pandas as _pd
                df = _pd.DataFrame(rows)
                st.dataframe(df, use_container_width=True, hide_index=True,
                             column_config={
                                 "mrp":           st.column_config.NumberColumn("MRP ₹",           format="₹%.2f"),
                                 "cost_price":    st.column_config.NumberColumn("Cost ₹",          format="₹%.2f"),
                                 "selling_price": st.column_config.NumberColumn("Selling ₹",       format="₹%.2f"),
                                 "gst_percent":   st.column_config.NumberColumn("GST %",           format="%.0f%%"),
                                 "qty":           st.column_config.NumberColumn("Qty"),
                                 "is_active":     st.column_config.CheckboxColumn("Active"),
                             })
                st.caption(f"{len(rows)} frame(s) in stock")
            else:
                st.info("No frames in stock yet.")
        except Exception as ex:
            st.error(f"Could not load frame stock: {ex}")


def _do_add_download(file_type: str, cfg: dict):
    try:
        from modules.loaders.smart.download_manager import build_add_template, make_add_filename

        with st.spinner("Preparing template..."):
            excel_bytes = build_add_template(file_type)

        filename = make_add_filename(file_type)
        st.download_button(
            label     = f"💾 Save {cfg['icon']} {cfg['label']} Blank Template",
            data      = excel_bytes,
            file_name = filename,
            mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key       = f"save_add_{file_type}_{datetime.now().strftime('%H%M%S')}",
        )
        st.success("✅ Template ready. Fill in your data and upload.")

    except Exception as e:
        st.error(f"Template generation failed: {e}")


def _handle_add_upload(uploaded, file_type: str, cfg: dict):
    """ADD flow upload: guard → preview → confirm → run_loader_safe ADD mode."""
    from modules.loaders.smart.upload_guard import check_upload
    from modules.loaders.patches.loader_transaction_wrapper import run_loader_safe
    import tempfile, os

    file_bytes = uploaded.read()
    user       = _get_user()

    # Guard check
    with st.spinner("Verifying file..."):
        guard = check_upload(file_bytes, expected_type=file_type, user=user)

    if not guard.allowed:
        for issue in guard.issues:
            st.error(issue)
        return

    if guard.flow != "ADD":
        st.error("⛔ This looks like an EDIT file, not a blank template. Use the 'Edit Existing Records' tab instead.")
        return

    df = guard.df
    # Strip example row (orange row with "EXAMPLE ROW" text)
    if not df.empty:
        first_val = str(df.iloc[0, 0]).upper()
        if "EXAMPLE" in first_val or "DELETE" in first_val:
            df = df.iloc[1:].reset_index(drop=True)
            st.info("ℹ️ Example row detected and automatically removed.")

    if df.empty:
        st.warning("⚠️ No data rows found after removing example row.")
        return

    # Preview
    st.markdown("### 📋 Preview — New Records to Add")
    st.info(f"**{len(df)} new record(s)** ready to add. Review below before confirming.")
    st.dataframe(df.head(20), use_container_width=True, hide_index=True)
    if len(df) > 20:
        st.caption(f"Showing first 20 of {len(df)} rows.")

    # Confirm
    st.markdown("---")
    col1, col2 = st.columns(2)
    with col1:
        confirm = st.button(
            f"✅ Add {len(df)} Records to Database",
            type="primary",
            key=f"confirm_add_{file_type}_{uploaded.name}",
            use_container_width=True,
        )
    with col2:
        cancel = st.button(
            "❌ Cancel",
            key=f"cancel_add_{file_type}_{uploaded.name}",
            use_container_width=True,
        )

    if cancel:
        st.warning("❌ Cancelled. No records were added.")
        return

    if confirm:
        with st.spinner("Adding records..."):
            # Save to temp file for loader
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
            tmp.write(file_bytes)
            tmp.close()

            try:
                result = run_loader_safe(
                    file_path   = tmp.name,
                    mode        = "LIVE",
                    stock_mode  = "ADD",
                    force_type  = file_type,
                    user        = user,
                    skip_dedup  = False,
                )
                if result.inserted > 0 or result.updated > 0:
                    st.success(
                        f"✅ Done! {result.inserted} new record(s) added, "
                        f"{result.updated} existing record(s) updated."
                    )
                else:
                    st.warning(
                        f"⚠️ {result.skipped} row(s) skipped. "
                        "Records may already exist. Check errors below."
                    )
                if result.errors:
                    with st.expander("⚠️ Errors"):
                        for e in result.errors:
                            st.write(f"• {e}")
            except Exception as e:
                st.error(f"Import failed: {e}")
            finally:
                os.unlink(tmp.name)


# ══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED CONTROL PANEL
# ══════════════════════════════════════════════════════════════════════════════

def render_control_panel(report) -> None:
    """
    Smart Loader Control Panel — status strip at top of edit upload flow.
    Shows: Schema | Headers | Changes | Risk | Critical
    Call immediately after smart_process() in _handle_edit_upload.
    """
    total_changes    = len(getattr(report, "changes",           []))
    safe_changes     = sum(1 for c in getattr(report, "changes", []) if c.risk_level == "SAFE")
    caution_changes  = sum(1 for c in getattr(report, "changes", []) if c.risk_level == "CAUTION")
    warning_changes  = sum(1 for c in getattr(report, "changes", []) if c.risk_level == "WARNING")
    schema_suggestions = getattr(report, "schema_suggestions",  [])
    auto_fixes       = getattr(report, "auto_fixes",            {})
    critical_errors  = getattr(report, "critical_errors",       [])
    preview_diff     = getattr(report, "preview_diff",          [])
    avg_confidence   = getattr(report, "avg_confidence",        None)

    st.markdown("### 🧠 Smart Loader Control Panel")

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        if schema_suggestions:
            st.warning("⚠ Schema")
        else:
            st.success("✔ Schema OK")

    with col2:
        if auto_fixes:
            st.info(f"🔁 {len(auto_fixes)} Fix{'es' if len(auto_fixes) > 1 else ''}")
        else:
            st.success("✔ Headers OK")

    with col3:
        if total_changes:
            st.info(f"📊 {total_changes} Change{'s' if total_changes != 1 else ''}")
        else:
            st.success("✔ No Changes")

    with col4:
        if warning_changes:
            st.error(f"🔴 {warning_changes} Risky")
        elif caution_changes:
            st.warning(f"🟡 {caution_changes} Caution")
        else:
            st.success("🟢 Safe")

    with col5:
        if critical_errors:
            st.error("⛔ Blocked")
        else:
            st.success("✔ Ready")

    if avg_confidence is not None:
        st.caption(f"🧠 AI Match Confidence: {avg_confidence:.0%}")

    with st.expander("🔍 Control Panel Details", expanded=False):
        if schema_suggestions:
            st.write("#### ⚠ Schema Suggestions")
            for s in schema_suggestions[:15]:
                st.write(f"• {s}")

        if preview_diff:
            st.write("#### 🔁 Column Fix Preview")
            st.dataframe(
                pd.DataFrame(preview_diff),
                use_container_width=True,
                hide_index=True,
            )

        if total_changes:
            st.write("#### 📊 Change Breakdown")
            _cc1, _cc2, _cc3 = st.columns(3)
            _cc1.metric("🟢 Safe",    safe_changes)
            _cc2.metric("🟡 Caution", caution_changes)
            _cc3.metric("🔴 Warning", warning_changes)

        if critical_errors:
            st.write("#### ⛔ Critical Issues")
            for e in critical_errors:
                st.error(f"• {e}")

    st.divider()


# ══════════════════════════════════════════════════════════════════════════════
# DECISION GUIDANCE
# ══════════════════════════════════════════════════════════════════════════════

def render_decision_guidance(report) -> str:
    """
    Top-of-page guidance strip — tells the user exactly what to do next.

    Returns state string: 'BLOCK' | 'REVIEW' | 'SAFE' | 'NOOP'

    🟢 SAFE   → All changes are safe, proceed
    🟡 REVIEW → Caution/Warning changes, review first
    🔴 BLOCK  → Critical errors, cannot commit
    ✔  NOOP   → No changes detected
    """
    rc              = getattr(report, "risk_counts",    {})
    critical_errors = getattr(report, "critical_errors",[])
    total_changes   = len(getattr(report, "changes",   []))
    warning_count   = rc.get("WARNING", 0)
    caution_count   = rc.get("CAUTION", 0)
    safe_count      = rc.get("SAFE",    0)

    if critical_errors:
        state = "BLOCK"
        st.error(
            "⛔ **Blocked — fix critical issues before proceeding.** "
            f"{len(critical_errors)} issue(s) must be resolved."
        )
    elif warning_count > 0:
        state = "REVIEW"
        st.warning(
            f"🟡 **Review recommended** — {warning_count} high-risk change(s) detected. "
            "These affect master fields. Carefully verify each change below."
        )
    elif caution_count > 0:
        state = "REVIEW"
        st.warning(
            f"🟡 **Check before committing** — {caution_count} financial/sensitive field(s) changing. "
            "Low risk but worth a quick review."
        )
    elif total_changes > 0:
        state = "SAFE"
        st.success(
            f"🟢 **Safe to proceed** — {total_changes} change(s), all low-risk fields."
        )
    else:
        state = "NOOP"
        st.info("✔ No changes detected — uploaded file matches current database.")

    with st.expander("🧭 Why this recommendation?", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Changes", total_changes)
        c2.metric("🟢 Safe",       safe_count)
        c3.metric("🟡 Caution",    caution_count)
        c4.metric("🔴 Warning",    warning_count)
        if critical_errors:
            st.error("Critical issues: " + " | ".join(critical_errors[:3]))

    return state


# ══════════════════════════════════════════════════════════════════════════════
# ROW-LEVEL APPROVE / REJECT DIFF
# ══════════════════════════════════════════════════════════════════════════════

def _validate_approved_changes(report, file_type: str) -> list:
    """
    Pre-apply validation shield — runs the same checks process_upload does
    but applied to the already-approved FieldChange list.

    This is what makes inline edits equivalent to a full engine pass:
    bad values are caught before they reach the DB, not after.

    Checks:
      1. Boolean fields have valid YES/NO values
      2. Numeric fields are actually numeric
      3. Allowed-values fields match their enum
      4. No identity/locked fields slipped through approval
      5. Required fields are not being set to empty
      6. No duplicate (entity_key, field_name) in approved changes (dedup)

    Returns list of error strings (empty = all clear).
    """
    errors = []

    if not report.changes:
        return errors

    # Load allowed values + type info from registry
    _BOOL_FIELDS     = set()
    _NUMERIC_FIELDS  = set()
    _ALLOWED_VALS    = {}
    _IDENTITY_FIELDS = {"product_name", "batch_no", "party_name", "mobile",
                        "record_no", "sku_code"}
    try:
        from modules.loaders.db_schema_registry import DB_SCHEMA
        for col in DB_SCHEMA.get(file_type, []):
            if col.db_type == "boolean":
                _BOOL_FIELDS.add(col.db_column)
            if col.db_type in ("numeric", "integer", "decimal", "float"):
                _NUMERIC_FIELDS.add(col.db_column)
            if col.allowed_values:
                _ALLOWED_VALS[col.db_column] = [v.lower() for v in col.allowed_values]
    except Exception:
        pass  # registry unavailable — skip type checks, still run others

    _seen: set = set()

    for change in report.changes:
        field    = change.field_name
        new_val  = str(change.new_value or "").strip()
        rec      = change.entity_key

        # 1. No identity field should ever reach here (double guard)
        if field in _IDENTITY_FIELDS:
            errors.append(
                f"❌ Identity field '{field}' on '{rec}' cannot be changed "
                f"(should have been blocked earlier)"
            )
            continue

        # 2. Empty value on a change — skip (blank means no-op, caught upstream)
        if not new_val:
            errors.append(
                f"⚠️ '{field}' on '{rec}' has an empty new value — "
                f"remove this row or enter a valid value"
            )
            continue

        # 3. Boolean fields must be YES/NO
        if field in _BOOL_FIELDS:
            if new_val.lower() not in ("yes", "no", "true", "false", "1", "0", "y", "n"):
                errors.append(
                    f"❌ '{field}' on '{rec}': invalid boolean value '{new_val}' "
                    f"— must be YES or NO"
                )

        # 4. Numeric fields must be numeric
        if field in _NUMERIC_FIELDS:
            try:
                float(new_val)
            except ValueError:
                errors.append(
                    f"❌ '{field}' on '{rec}': '{new_val}' is not a number"
                )

        # 5. Allowed-values check
        if field in _ALLOWED_VALS:
            if new_val.lower() not in _ALLOWED_VALS[field]:
                errors.append(
                    f"❌ '{field}' on '{rec}': '{new_val}' is not in allowed values "
                    f"[{', '.join(_ALLOWED_VALS[field])}]"
                )

        # 6. Dedup — same (entity, field) approved twice
        _key = (change.entity_key, field)
        if _key in _seen:
            errors.append(
                f"⚠️ '{field}' on '{rec}' appears more than once in approved changes — "
                f"only one value will be applied"
            )
        _seen.add(_key)

    return errors


def render_inline_edit_grid(
    report,
    file_type: str = "",
    key_prefix: str = "",
) -> "pd.DataFrame | None":
    """
    Inline Edit + Approve Grid — the final evolution of the diff view.

    One unified grid where the user can:
      ✏️  Edit any value inline before committing
      ☑   Approve/reject per row
      📊  See DB current vs new value side-by-side
      🔴  Risk badge per row

    Returns the edited DataFrame (Record, Field, DB Value, New Value, Approve).
    Returns None if nothing to show.

    Enforcement: caller must pass return value to apply_grid_edits_to_report()
    which updates report.changes with edited values and removes rejected rows.
    THEN the normal change_approver.apply_changes path handles audit+backup+undo.
    """
    comparison_rows = getattr(report, "comparison_rows", [])
    if not comparison_rows:
        return None

    df = pd.DataFrame(comparison_rows)
    if df.empty:
        return None

    # Only rows that actually differ
    if "DB Value" in df.columns and "Uploaded Value" in df.columns:
        df = df[
            df.apply(
                lambda r: str(r.get("DB Value","")).lower().strip()
                          != str(r.get("Uploaded Value","")).lower().strip(),
                axis=1,
            )
        ].copy()

    if df.empty:
        st.success("✅ All tracked fields match the database.")
        return None

    # Build the editable frame
    # "New Value" starts as Uploaded Value — user can override inline
    df["New Value"] = df.get("Uploaded Value", df.get("DB Value", ""))
    df["Approve"]   = df.get("Approved", True)   # default: approve all

    _total = len(df)

    st.subheader("✏️ Inline Edit + Approve Grid")
    st.caption(
        "**Edit** any value in 'New Value' column before committing  |  "
        "**Uncheck** Approve to skip that row  |  "
        "Only approved rows go to DB."
    )

    # Summary bar
    _grid_key = f"inline_grid_{file_type}_{key_prefix}"
    _current  = st.session_state.get(_grid_key, df)

    _n_approved = int(_current.get("Approve", pd.Series([True]*len(_current))).sum()) \
                  if isinstance(_current, pd.DataFrame) else _total
    _sa, _sb, _sc = st.columns(3)
    _sa.metric("Total Changes", _total)
    _sb.metric("✅ Will Apply",  _n_approved)
    _sc.metric("☐ Will Skip",   _total - _n_approved)

    # Column config
    _show_cols = ["Record", "Field", "DB Value", "New Value", "Approve"]
    if "Risk" in df.columns:
        _show_cols.insert(2, "Risk")
    _show_cols = [c for c in _show_cols if c in df.columns or c in ["Approve","New Value"]]

    _col_cfg = {
        "Approve": st.column_config.CheckboxColumn(
            "✅ Approve",
            help="Uncheck to skip this change",
            default=True,
            width="small",
        ),
        "New Value": st.column_config.TextColumn(
            "✏️ New Value",
            help="Edit this cell to change the committed value",
            width="medium",
        ),
        "DB Value": st.column_config.TextColumn("📦 DB (current)", width="medium"),
        "Record":   st.column_config.TextColumn("Record",          width="large"),
        "Field":    st.column_config.TextColumn("Field",           width="medium"),
        "Risk":     st.column_config.TextColumn("Risk",            width="small"),
    }

    # Editable grid — Record/Field/DB Value are read-only; New Value + Approve are editable
    _disabled = [c for c in _show_cols if c not in ("New Value", "Approve")]

    # Grouped by Record for clarity
    _records = df["Record"].unique() if "Record" in df.columns else ["All"]
    _all_frames: list = []

    for record in _records:
        _grp = (_current[_current["Record"] == record].copy()
                if isinstance(_current, pd.DataFrame) and "Record" in _current.columns
                else df[df["Record"] == record].copy())

        _risk_vals = _grp.get("Risk", pd.Series([])).tolist() if "Risk" in _grp.columns else []
        _has_warn  = any("Warning" in str(r) for r in _risk_vals)
        _has_caut  = any("Caution" in str(r) for r in _risk_vals)
        _badge     = "🔴" if _has_warn else ("🟡" if _has_caut else "🟢")
        _n_grp_app = int(_grp.get("Approve", pd.Series([True]*len(_grp))).sum())

        with st.expander(
            f"{_badge} {record}  —  {_n_grp_app}/{len(_grp)} approved",
            expanded=_has_warn or _has_caut,
        ):
            _grp_cols = [c for c in _show_cols if c in _grp.columns]
            _edited = st.data_editor(
                _grp[_grp_cols].reset_index(drop=True),
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                disabled=_disabled,
                column_config=_col_cfg,
                key=f"ieg_{file_type}_{record}_{key_prefix}",
            )
            _edited["Record"] = record   # re-attach for merge
            _all_frames.append(_edited)

    if not _all_frames:
        return None

    _combined = pd.concat(_all_frames, ignore_index=True)

    # Detect manual edits (user changed New Value vs original Uploaded Value)
    if "Uploaded Value" in df.columns and "New Value" in _combined.columns:
        _uv = df.set_index(["Record","Field"])["Uploaded Value"].to_dict() \
              if "Field" in df.columns else {}
        _combined["Manually Edited"] = _combined.apply(
            lambda r: str(r.get("New Value","")).strip() !=
                      str(_uv.get((r.get("Record",""), r.get("Field","")), "")).strip(),
            axis=1,
        )
        _n_manual = int(_combined["Manually Edited"].sum())
        if _n_manual:
            st.info(f"✏️ {_n_manual} value(s) manually edited in the grid.")

    # Final approval summary
    _final_app  = int(_combined.get("Approve", pd.Series([True]*len(_combined))).sum())
    _final_skip = len(_combined) - _final_app
    if _final_skip:
        st.warning(f"☐ {_final_skip} row(s) unchecked — will be skipped on commit.")
    else:
        st.success(f"✅ All {_final_app} change(s) approved.")

    # Persist in session state for live refresh
    st.session_state[_grid_key] = _combined

    st.divider()
    return _combined


def apply_grid_edits_to_report(report, edited_df, user: str = "system"):
    """
    THE ENFORCEMENT LAYER — makes approval real, not cosmetic.

    1. Filters report.changes: only (Record, Field) pairs where Approve=True survive
    2. Updates change.new_value if user edited the value in the grid
    3. Sets approved_by + manually_edited on every FieldChange (audit context)
    4. Routes through the existing change_approver (audit + backup + undo preserved)

    This is the critical function that closes the gap between
    "visual approval" and "enforced approval".
    """
    if edited_df is None or edited_df.empty:
        return report

    # Build map: (Record, Field) → {new_value, approved, original_uploaded}
    _grid_map: dict = {}
    for _, row in edited_df.iterrows():
        key = (str(row.get("Record", "")), str(row.get("Field", "")))
        _grid_map[key] = {
            "new_value":  str(row.get("New Value", row.get("Uploaded Value", ""))).strip(),
            "approved":   bool(row.get("Approve", row.get("Approved", True))),
            "orig_upload":str(row.get("Uploaded Value", "")).strip(),
        }

    from modules.loaders.smart.change_detector import FieldChange

    _original  = len(report.changes)
    _accepted  = []
    _rejected  = 0
    _edited_ct = 0

    for change in report.changes:
        key      = (change.entity_key, change.field_name)
        grid_row = _grid_map.get(key)

        if grid_row is None:
            _accepted.append(change)
            continue

        if not grid_row["approved"]:
            _rejected += 1
            continue   # ← ENFORCED: rejected row never reaches DB

        _grid_val     = grid_row["new_value"]
        _orig_upload  = grid_row["orig_upload"]
        _was_edited   = bool(
            _grid_val and
            _grid_val != _orig_upload and
            _grid_val != str(change.new_value or "").strip()
        )

        # Build the final FieldChange — update value if edited, stamp approval context
        _new_val = _grid_val if (_grid_val and _was_edited) else change.new_value
        if _was_edited:
            _edited_ct += 1

        _accepted.append(FieldChange(
            row_index       = change.row_index,
            entity_key      = change.entity_key,
            field_name      = change.field_name,
            old_value       = change.old_value,
            new_value       = _new_val,
            risk_level      = change.risk_level,
            entity_id       = change.entity_id,
            approved_by     = user,          # ← audit: who approved
            manually_edited = _was_edited,   # ← audit: was this value changed in grid
        ))

    report.changes = _accepted

    import logging
    _log = logging.getLogger(__name__)
    if _rejected:
        _log.info(f"[inline_grid] {_rejected}/{_original} change(s) rejected by user")
    if _edited_ct:
        _log.info(f"[inline_grid] {_edited_ct} value(s) overridden by inline edit")

    return report


def _fetch_refreshed_values(
    entity_keys: set,
    file_type: str,
    fields: list = None,
) -> dict:
    """
    Re-fetch current DB values for committed records (targeted, not full table).
    Returns {entity_key: {field: value}} for live grid refresh.
    """
    if not entity_keys:
        return {}

    _TABLE = {
        "PRODUCT": ("products",        "product_name"),
        "CLENS":   ("inventory_stock",  "batch_no"),
        "OPHLENS": ("inventory_stock",  "product_name"),
        "FRAME":   ("inventory_stock",  "batch_no"),
        "SOL":     ("batches",          "product_name"),
        "PARTY":   ("parties",          "party_name"),
        "PATIENT": ("patients",         "master_name"),
        "PRICE":   ("inventory_stock",  "product_name"),
    }

    table_info = _TABLE.get(file_type)
    if not table_info:
        return {}

    table, key_col = table_info
    refreshed = {}

    try:
        from modules.sql_adapter import run_query
        for key in entity_keys:
            rows = run_query(
                f'SELECT * FROM {table} WHERE "{key_col}" = %s LIMIT 1',
                (key,),
            ) or []
            if rows:
                refreshed[key] = rows[0]
    except Exception:
        pass

    return refreshed


# ══════════════════════════════════════════════════════════════════════════════
# SIDE-BY-SIDE DIFF  (fallback / analytics view only — main flow uses inline grid)
# ══════════════════════════════════════════════════════════════════════════════

def render_side_by_side_diff(report) -> None:
    """
    Excel-style side-by-side diff grid.
    Columns: Record | Field | DB Value | Uploaded Value | Change
    - 🔴 Highlighted rows = changed values
    - Filter: show only changes
    - Group: by record


    # Risk colour helper
    _RISK_BG = {
        "🔴 Warning": "#ffe0e0",
        "🟡 Caution": "#fff7d6",
        "🟢 Safe":    "#eafaf1",
    }

    st.subheader("📊 Approve / Reject Changes (Row-Level)")
    st.caption(
        "✅ Checked = **will be committed** | "
        "☐ Unchecked = **will be skipped** | "
        "Uncheck rows you want to exclude from this commit."
    )

    # Approval summary counts
    _n_total    = len(df_changed)
    _n_approved = int(df_changed["Approved"].sum())
    _ca, _cb, _cc = st.columns(3)
    _ca.metric("Total Changes",  _n_total)
    _cb.metric("✅ Will Apply",   _n_approved)
    _cc.metric("☐ Will Skip",    _n_total - _n_approved)

    st.markdown("")

    # Build display cols
    _display_cols = ["Record", "Field", "DB Value", "Uploaded Value", "Change", "Approved"]
    _display_cols = [c for c in _display_cols if c in df_changed.columns]

    # Group by Record — each product/party in its own expander
    _records = df_changed["Record"].unique() if "Record" in df_changed.columns else ["All"]
    _all_edited_frames = []

    for record in _records:
        _grp = df_changed[df_changed["Record"] == record].copy()
        _n_grp = len(_grp)
        _n_grp_appr = int(_grp.get("Approved", pd.Series([True]*_n_grp)).sum())

        _risk_vals = _grp.get("Risk", pd.Series([])).tolist() if "Risk" in _grp.columns else []
        _has_warn  = any("Warning" in str(r) for r in _risk_vals)
        _has_caut  = any("Caution" in str(r) for r in _risk_vals)
        _badge     = "🔴" if _has_warn else ("🟡" if _has_caut else "🟢")

        with st.expander(
            f"{_badge} {record}  —  {_n_grp_appr}/{_n_grp} approved",
            expanded=(_has_warn or _has_caut),
        ):
            _show_cols = [c for c in _display_cols if c in _grp.columns]

            _edited = st.data_editor(
                _grp[_show_cols].reset_index(drop=True),
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in _show_cols if c != "Approved"],
                column_config={
                    "Approved": st.column_config.CheckboxColumn(
                        "✅ Approve",
                        help="Uncheck to skip this change",
                        default=True,
                    ),
                    "Change": st.column_config.TextColumn("Change", width="large"),
                    "DB Value":       st.column_config.TextColumn("DB (current)"),
                    "Uploaded Value": st.column_config.TextColumn("Uploaded (new)"),
                },
                key=f"approval_{file_type}_{record}_{id(_grp)}",
            )
            # Re-attach Record column for filter step
            _edited["Record"] = record
            _all_edited_frames.append(_edited)

    if not _all_edited_frames:
        return None

    _combined = pd.concat(_all_edited_frames, ignore_index=True)

    # Live summary after edits
    _final_approved = int(_combined.get("Approved", pd.Series([True]*len(_combined))).sum())
    _final_skip     = len(_combined) - _final_approved
    if _final_skip > 0:
        st.info(
            f"ℹ️ **{_final_approved} change(s) approved** — "
            f"{_final_skip} will be skipped on commit."
        )
    else:
        st.success(f"✅ All {_final_approved} change(s) approved.")

    st.divider()
    return _combined


    """
    Excel-style side-by-side diff grid.
    Columns: Record | Field | DB Value | Uploaded Value | Change
    - 🔴 Highlighted rows = changed values
    - Filter: show only changes
    - Group: by record
    Call before the Approve section in _handle_edit_upload.
    """
    comparison_rows = getattr(report, "comparison_rows", [])
    if not comparison_rows:
        return

    df = pd.DataFrame(comparison_rows)
    if df.empty:
        return

    # Build Change column (arrow display)
    if "DB Value" in df.columns and "Uploaded Value" in df.columns:
        df["Change"] = df.apply(
            lambda r: (
                f"{r['DB Value']} → {r['Uploaded Value']}"
                if str(r.get("DB Value", "")).lower() != str(r.get("Uploaded Value", "")).lower()
                else ""
            ),
            axis=1,
        )
        if "Changed" in df.columns:
            df = df.drop(columns=["Changed"])

        wanted = ["Record", "Field", "DB Value", "Uploaded Value", "Change"]
        df = df[[c for c in wanted if c in df.columns]]

    st.subheader("📊 Side-by-Side Diff (Live DB vs Uploaded)")

    _diff_col1, _diff_col2 = st.columns([1, 1])
    show_only_changed = _diff_col1.checkbox(
        "Show only changed rows",
        value=True,
        key=f"diff_filter_{report.file_type}_{id(report)}",
    )
    group_by_record = _diff_col2.checkbox(
        "Group by record",
        value=False,
        key=f"diff_group_{report.file_type}_{id(report)}",
    )

    df_view = df[df["Change"] != ""] if (show_only_changed and "Change" in df.columns) else df

    if df_view.empty:
        st.success("✅ All tracked fields match the database.")
        return

    def _highlight_diff_row(row):
        if row.get("Change", ""):
            return [
                ("background-color: #ffe6e6; font-weight: bold"
                 if col == "Change"
                 else "background-color: #ffe6e6")
                for col in row.index
            ]
        return [""] * len(row)

    if group_by_record and "Record" in df_view.columns:
        for record, group in df_view.groupby("Record"):
            n = len(group)
            with st.expander(f"📌 {record}  ({n} diff{'s' if n != 1 else ''})"):
                g = group.drop(columns=["Record"], errors="ignore")
                st.dataframe(
                    g.style.apply(_highlight_diff_row, axis=1),
                    use_container_width=True,
                    hide_index=True,
                )
    else:
        st.dataframe(
            df_view.style.apply(_highlight_diff_row, axis=1),
            use_container_width=True,
            hide_index=True,
        )

    st.caption(
        f"🔴 Highlighted = value differs from DB  |  "
        f"Showing {len(df_view)} of {len(df)} row(s)"
    )
    st.divider()


def _render_advice_box(advice):
    """Render the AI advisor summary with appropriate styling."""
    colour = {
        "PROCEED": "success",
        "REVIEW":  "warning",
        "STOP":    "error",
    }.get(advice.recommendation, "info")

    if colour == "success":
        st.success(advice.explanation)
    elif colour == "warning":
        st.warning(advice.explanation)
    else:
        st.error(advice.explanation)

    for w in advice.warnings:
        if w.startswith("⛔") or w.startswith("🔴"):
            st.error(w)
        elif w.startswith("⚠️") or w.startswith("🟡"):
            st.warning(w)
        else:
            st.info(w)

    if advice.field_advice:
        with st.expander("📖 Field-level guidance"):
            for fname, guidance in advice.field_advice.items():
                st.markdown(f"**{fname}:** {guidance}")


def _get_user() -> str:
    """Get current user from session state."""
    user = st.session_state.get("user", "system")
    if isinstance(user, dict):
        return user.get("username", user.get("name", "system"))
    return str(user) if user else "system"


# ══════════════════════════════════════════════════════════════════════════════
# ERROR REPORT DOWNLOAD HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _offer_error_download(report) -> None:
    """Show a download button for the full diff / error report as Excel."""
    try:
        import io
        from modules.analytics.change_analytics import build_error_report
        df_err = build_error_report(report)
        if df_err.empty:
            return
        buf = io.BytesIO()
        df_err.to_excel(buf, index=False)
        st.download_button(
            label="📥 Download Full Report (Excel)",
            data=buf.getvalue(),
            file_name=f"loader_report_{report.file_type}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_err_{report.file_type}_{id(report)}",
        )
    except Exception:
        pass  # never crash the UI for a download helper


# ══════════════════════════════════════════════════════════════════════════════
# CHANGE ANALYTICS DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

def render_analytics_dashboard() -> None:
    """
    Full Business Intelligence Dashboard.

    Tabs:
      📊 Overview   — KPIs, risk trend chart, time series
      🔥 Volatility — Field volatility, product instability
      💡 Insights   — Business insight layer
      🕒 Activity   — Recent feed + commit history
      ⚠️ Anomalies  — Suspicious pattern detection
      🧠 Memory     — Learned column mappings
    """
    from modules.analytics.change_analytics import (
        get_change_summary, get_top_records, get_risk_distribution,
        get_recent_activity, get_file_type_activity, get_anomalies,
        get_risk_trend, get_time_series, get_field_volatility,
        get_product_instability, get_business_insights,
        get_commit_history, get_user_activity,
    )

    st.title("📊 Smart Loader Intelligence Dashboard")

    days = st.selectbox(
        "Time range",
        [1, 7, 30, 90],
        index=1,
        format_func=lambda d: f"Last {d} day(s)",
        key="analytics_days",
    )

    tab_ov, tab_vol, tab_ins, tab_act, tab_anom, tab_mem = st.tabs([
        "📊 Overview",
        "🔥 Volatility",
        "💡 Business Insights",
        "🕒 Activity",
        "⚠️ Anomalies",
        "🧠 Memory",
    ])

    # ══ TAB 1: OVERVIEW ════════════════════════════════════════════════════════
    with tab_ov:
        st.subheader("📊 Summary")

        _summary = get_change_summary(days=days)
        _risk    = {r["risk_level"]: r["cnt"] for r in get_risk_distribution(days=days)}
        _total   = sum(r.get("change_count", 0) for r in _summary)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Changes",     _total)
        c2.metric("🔴 High Risk",       _risk.get("WARNING", 0))
        c3.metric("🟡 Caution",         _risk.get("CAUTION", 0))
        c4.metric("🟢 Safe",            _risk.get("SAFE",    0))

        st.markdown("---")

        # Risk trend chart
        _trend = get_risk_trend(days=days)
        if _trend:
            st.subheader("📈 Risk Trend Over Time")
            _tdf = pd.DataFrame(_trend).set_index("date")
            _tdf.index = pd.to_datetime(_tdf.index)
            if {"safe", "caution", "warning"}.issubset(_tdf.columns):
                st.area_chart(_tdf[["safe", "caution", "warning"]],
                              color=["#2ecc71", "#f39c12", "#e74c3c"])
                st.caption("🟢 Safe  |  🟡 Caution  |  🔴 Warning — daily stacked area")

        # Time series total
        _ts = get_time_series(days=days)
        if _ts:
            st.subheader("📊 Daily Change Volume")
            _tsdf = pd.DataFrame(_ts).set_index("date")
            _tsdf.index = pd.to_datetime(_tsdf.index)
            st.bar_chart(_tsdf["changes"])

        # Activity by file type
        _ft = get_file_type_activity(days=days)
        if _ft:
            st.subheader("📂 Activity by Module")
            _ftdf = pd.DataFrame(_ft)
            st.bar_chart(_ftdf.set_index("file_type")["changes"])

    # ══ TAB 2: FIELD VOLATILITY ════════════════════════════════════════════════
    with tab_vol:
        st.subheader("🔥 Field Volatility — Which Fields Change Most?")

        _vol = get_field_volatility(days=days, limit=15)
        if _vol:
            _vdf = pd.DataFrame(_vol)

            def _hl_vol(row):
                score = float(row.get("volatility_score", 0) or 0)
                if score >= 2.5:   return ["background-color: #ffe6e6"] * len(row)
                if score >= 1.5:   return ["background-color: #fff3cd"] * len(row)
                return ["background-color: #eafaf1"] * len(row)

            st.dataframe(
                _vdf.style.apply(_hl_vol, axis=1),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "volatility_score": st.column_config.NumberColumn(
                        "Instability", format="%.2f",
                        help="1=safe only, 2=caution mix, 3=all warning"
                    ),
                }
            )
            st.caption(
                "🔴 Score ≥ 2.5 = high instability | "
                "🟡 Score 1.5–2.5 = medium | 🟢 < 1.5 = stable"
            )
            st.bar_chart(_vdf.set_index("field_name")["total_changes"])
        else:
            st.info("No field change data in this time range.")

        st.markdown("---")
        st.subheader("📦 Product/Record Instability")

        _inst = get_product_instability(days=days, limit=10)
        if _inst:
            _idf = pd.DataFrame(_inst)

            def _hl_inst(row):
                score = float(row.get("instability_score", 0) or 0)
                if score >= 5:  return ["background-color: #ffe6e6"] * len(row)
                if score >= 2:  return ["background-color: #fff3cd"] * len(row)
                return [""] * len(row)

            st.dataframe(
                _idf.style.apply(_hl_inst, axis=1),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "Records with highest instability score need audit. "
                "Score = WARNING×3 + CAUTION×2 + SAFE×1"
            )
        else:
            st.info("No instability data for this period.")

    # ══ TAB 3: BUSINESS INSIGHTS ═══════════════════════════════════════════════
    with tab_ins:
        st.subheader("💡 Business Insights")
        insights = get_business_insights(days=days)

        if insights:
            for _key, ins in insights.items():
                col_m, col_t = st.columns([1, 3])
                with col_m:
                    st.metric(ins["label"], ins["value"], ins.get("unit", ""))
                with col_t:
                    if ins["status"] == "alert":
                        st.error(ins["message"])
                    elif ins["status"] == "warn":
                        st.warning(ins["message"])
                    else:
                        st.success(ins["message"])
        else:
            st.info("No insight data available yet.")

        # Top users
        st.markdown("---")
        st.subheader("👤 User Activity")
        _users = get_user_activity(days=days)
        if _users:
            st.dataframe(pd.DataFrame(_users), use_container_width=True, hide_index=True)
        else:
            st.info("No user activity recorded.")

    # ══ TAB 4: ACTIVITY FEED ═══════════════════════════════════════════════════
    with tab_act:
        st.subheader("🕒 Recent Changes")
        _recent = get_recent_activity(limit=200)
        if _recent:
            _rdf = pd.DataFrame(_recent)

            # Filter controls
            _col_ft, _col_risk, _col_search = st.columns([2, 2, 3])
            _ft_opts   = ["All"] + sorted(_rdf["file_type"].dropna().unique().tolist())
            _risk_opts = ["All", "WARNING", "CAUTION", "SAFE"]
            _sel_ft    = _col_ft.selectbox("Module", _ft_opts, key="act_ft")
            _sel_risk  = _col_risk.selectbox("Risk", _risk_opts, key="act_risk")
            _search    = _col_search.text_input("Search record/field", key="act_search")

            _view = _rdf.copy()
            if _sel_ft   != "All":    _view = _view[_view["file_type"]  == _sel_ft]
            if _sel_risk != "All":    _view = _view[_view["risk_level"] == _sel_risk]
            if _search.strip():
                _s = _search.strip().lower()
                _view = _view[
                    _view["entity_key"].str.lower().str.contains(_s, na=False) |
                    _view["field_name"].str.lower().str.contains(_s, na=False)
                ]

            def _hl_act(row):
                lvl = str(row.get("risk_level", "")).upper()
                if lvl == "WARNING": return ["background-color: #ffe6e6"] * len(row)
                if lvl == "CAUTION": return ["background-color: #fff3cd"] * len(row)
                return [""] * len(row)

            st.dataframe(_view.style.apply(_hl_act, axis=1),
                         use_container_width=True, hide_index=True)
            st.caption(f"Showing {len(_view)} of {len(_rdf)} records")
        else:
            st.info("No activity recorded yet.")

        st.markdown("---")
        st.subheader("📦 Commit History")
        _commits = get_commit_history(days=days)
        if _commits:
            _cdf = pd.DataFrame(_commits)
            st.dataframe(_cdf, use_container_width=True, hide_index=True)

            # Undo from commit history
            _sel_commit = st.selectbox(
                "Select commit to undo:",
                options=[""] + [str(r["import_id"]) for r in _commits if r.get("import_id")],
                format_func=lambda x: (
                    next((f"{r['committed_display']} — {r['file_type']} "
                          f"({r['field_changes']} changes)"
                          for r in _commits if str(r.get("import_id","")) == x), x)
                    if x else "— select —"
                ),
                key="undo_select_commit",
            )
            if _sel_commit:
                _undo_user = st.session_state.get("user", "system")
                if st.button("↩️ Undo This Commit", key="undo_from_history",
                             type="secondary"):
                    from modules.analytics.change_analytics import undo_commit
                    res = undo_commit(_sel_commit, user=str(_undo_user))
                    if res["success"]:
                        st.success(f"✅ Reverted {res['reverted']} change(s).")
                    else:
                        st.error(f"❌ {'; '.join(res['errors'])}")
        else:
            st.info("No commit history for this period.")

    # ══ TAB 5: ANOMALIES ═══════════════════════════════════════════════════════
    with tab_anom:
        st.subheader("⚠️ Anomaly Detection")
        anomalies = get_anomalies(days=days)
        if anomalies:
            for a in anomalies:
                sev = a.get("severity", "warn")
                msg = f"**{a['type']}** — {a['subject']}  ({a['count']}×) — {a['note']}"
                if sev == "alert":
                    st.error(msg)
                elif sev == "info":
                    st.info(msg)
                else:
                    st.warning(msg)
        else:
            st.success(f"✅ No anomalies detected in last {days} day(s).")

    # ══ TAB 6: LEARNING MEMORY ═════════════════════════════════════════════════
    with tab_mem:
        st.subheader("🧠 Learned Column Mappings")
        st.caption(
            "Every time you click 'Apply Fixes', the mapping is saved here. "
            "Next upload of the same column name will be auto-corrected silently."
        )
        try:
            from modules.loaders.smart.learning_memory import get_all_memory, delete_mapping, get_memory_stats
            stats = get_memory_stats()
            if stats:
                m1, m2, m3 = st.columns(3)
                m1.metric("Total Learned Mappings", stats.get("total_mappings", 0))
                m2.metric("Total Auto-Applications", stats.get("total_applications", 0))
                m3.metric("Avg Confidence", f"{float(stats.get('avg_confidence') or 0):.0%}")

            _mem_ft = st.selectbox(
                "Filter by module",
                ["All"] + ["PRODUCT","CLENS","OPHLENS","SOL","FRAME","PARTY","PATIENT","PRICE"],
                key="mem_ft_filter",
            )
            rows = get_all_memory(None if _mem_ft == "All" else _mem_ft)
            if rows:
                _mdf = pd.DataFrame(rows)
                st.dataframe(_mdf, use_container_width=True, hide_index=True)

                # Delete a mapping
                _del_col = st.text_input(
                    "Forget a mapping (enter exact excel_col to remove):",
                    key="mem_del_col",
                )
                _del_ft2 = st.selectbox(
                    "For module:",
                    ["PRODUCT","CLENS","OPHLENS","SOL","FRAME","PARTY","PATIENT","PRICE"],
                    key="mem_del_ft",
                )
                if _del_col and st.button("🗑️ Remove Mapping", key="mem_del_btn"):
                    if delete_mapping(_del_ft2, _del_col.strip()):
                        st.success(f"✅ Mapping '{_del_col}' removed.")
                        st.rerun()
                    else:
                        st.error("❌ Could not remove mapping.")
            else:
                st.info("No learned mappings yet. "
                        "Click 'Apply Suggested Fixes' on any upload to start learning.")
        except Exception as _me:
            st.info(f"Learning memory not available: {_me}")



# ══════════════════════════════════════════════════════════════════════════════
# SQL SETUP HELPER
# ══════════════════════════════════════════════════════════════════════════════

SETUP_SQL = """
-- Run this once in your database to enable the Smart Loader audit system

CREATE TABLE IF NOT EXISTS field_change_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id     UUID,
    file_type     TEXT,
    entity_id     UUID,
    entity_key    TEXT,
    field_name    TEXT,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    TEXT,
    changed_at    TIMESTAMPTZ DEFAULT NOW(),
    risk_level    TEXT,
    backup_id     UUID
);

CREATE TABLE IF NOT EXISTS field_change_backup (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    backup_id     UUID,
    file_type     TEXT,
    entity_id     UUID,
    entity_key    TEXT,
    snapshot      JSONB,
    backed_up_at  TIMESTAMPTZ DEFAULT NOW(),
    backed_up_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_fcl_import_id  ON field_change_log(import_id);
CREATE INDEX IF NOT EXISTS idx_fcl_entity_id  ON field_change_log(entity_id);
CREATE INDEX IF NOT EXISTS idx_fcl_changed_at ON field_change_log(changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_fcb_backup_id  ON field_change_backup(backup_id);
"""


# ══════════════════════════════════════════════════════════════════════════════
# MAIN GROUPS — GST % + HSN master
# ══════════════════════════════════════════════════════════════════════════════

def _render_main_groups():
    """Manage main_groups master — canonical GST% and HSN code per product group."""
    from modules.sql_adapter import run_query, run_write

    st.markdown(
        "Set the canonical **GST rate** and **HSN code** for each product group. "
        "When you upload a PRODUCT Excel with a blank GST% or HSN, these values are **auto-filled** from here. "
        "Products still store their own values — this only fills blanks on upload."
    )
    st.caption("⚠️ GST rates change by government notification. Confirm current rates with your CA before saving.")

    # ── Ensure table exists ───────────────────────────────────────────────────
    try:
        run_write("""
            CREATE TABLE IF NOT EXISTS main_groups (
                id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                name        TEXT         NOT NULL UNIQUE,
                gst_percent NUMERIC(5,2) NOT NULL DEFAULT 12,
                hsn_code    TEXT         NOT NULL DEFAULT '',
                description TEXT,
                created_at  TIMESTAMP    DEFAULT NOW(),
                updated_at  TIMESTAMP    DEFAULT NOW()
            )
        """)
    except Exception as e:
        st.error(f"Cannot create main_groups table: {e}")
        return

    rows = run_query(
        "SELECT id, name, gst_percent, hsn_code, description FROM main_groups ORDER BY name"
    ) or []

    GST_OPTIONS = [0, 5, 12, 18, 28]

    # ── Existing groups ───────────────────────────────────────────────────────
    if not rows:
        st.info("No groups yet. Add your first group below — confirm rates with your CA.")
    else:
        st.markdown(f"#### {len(rows)} Group{'s' if len(rows) != 1 else ''}")
        for row in rows:
            gst_val = int(row["gst_percent"])
            hsn_val = row["hsn_code"] or "—"
            rid     = str(row["id"])
            with st.expander(f"**{row['name']}** — {gst_val}% GST | HSN: {hsn_val}", expanded=False):
                c1, c2, c3, c4 = st.columns([3, 1, 2, 2])
                new_name = c1.text_input("Group Name", value=row["name"],          key="mg_name_" + rid)
                gst_idx  = GST_OPTIONS.index(gst_val) if gst_val in GST_OPTIONS else 2
                new_gst  = c2.selectbox("GST %", GST_OPTIONS, index=gst_idx,       key="mg_gst_"  + rid)
                new_hsn  = c3.text_input("HSN Code", value=row["hsn_code"] or "",  key="mg_hsn_"  + rid)
                new_desc = c4.text_input("Note",     value=row["description"] or "",key="mg_desc_" + rid)
                sc1, sc2 = st.columns([1, 5])
                if sc1.button("💾 Save", key="mg_save_" + rid):
                    try:
                        run_write(
                            "UPDATE main_groups SET name=%s, gst_percent=%s, hsn_code=%s, "
                            "description=%s, updated_at=NOW() WHERE id=%s",
                            (new_name.strip(), round(float(new_gst), 2), new_hsn.strip(), new_desc.strip(), rid)
                        )
                        st.success("✅ Saved.")
                        st.session_state["_mg_render_n"] = st.session_state.get("_mg_render_n", 0) + 1
                        st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")
                if sc2.button("🗑️ Delete", key="mg_del_" + rid):
                    try:
                        run_write("DELETE FROM main_groups WHERE id=%s", (rid,))
                        st.success("Deleted.")
                        st.session_state["_mg_render_n"] = st.session_state.get("_mg_render_n", 0) + 1
                        st.rerun()
                    except Exception as e:
                        st.error(f"Delete failed: {e}")

    # ── Add new group ─────────────────────────────────────────────────────────
    # _mg_render_n counter: bumped on every rerun so Streamlit sees fresh keys.
    # Without this, static keys like "mg_new_name" register twice on re-render → crash.
    _k = st.session_state.get("_mg_render_n", 0)
    st.markdown("---")
    st.markdown("#### ➕ Add New Group")
    a1, a2, a3, a4 = st.columns([3, 1, 2, 2])
    add_name = a1.text_input("Group Name", placeholder="e.g. Ophthalmic Lenses", key=f"mg_new_name_{_k}")
    add_gst  = a2.selectbox("GST %", GST_OPTIONS, index=2,                        key=f"mg_new_gst_{_k}")
    add_hsn  = a3.text_input("HSN Code", placeholder="e.g. 90015000",             key=f"mg_new_hsn_{_k}")
    add_desc = a4.text_input("Note", placeholder="optional",                      key=f"mg_new_desc_{_k}")

    if st.button("➕ Add Group", key=f"mg_add_btn_{_k}", type="primary"):
        if not add_name.strip():
            st.warning("Group name is required.")
        else:
            try:
                run_write(
                    "INSERT INTO main_groups (name, gst_percent, hsn_code, description) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                    (add_name.strip(), round(float(add_gst), 2), add_hsn.strip(), add_desc.strip())
                )
                st.success(f"✅ '{add_name}' added.")
                st.session_state["_mg_render_n"] = _k + 1   # bump → fresh keys next render
                st.rerun()
            except Exception as e:
                st.error(f"Add failed: {e}")

