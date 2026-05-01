"""
modules/ui/party_dedup_ui.py
==============================
Party dedup review UI — load → analyse → review each group → confirm → write.

Access: Data Loader → Party → "🔍 Dedup & Customer Numbers" tab
"""

import streamlit as st


_CONFLICT_COLOURS = {
    'EXACT_NAME':    ('#ef4444', '🔴', 'Exact duplicate name'),
    'SAME_GSTIN':    ('#f97316', '🟠', 'Same GSTIN — same legal entity'),
    'SAME_MOBILE':   ('#f59e0b', '🟡', 'Same mobile — branch or rename?'),
    'SPELL_SIMILAR': ('#3b82f6', '🔵', 'Possible spelling variant'),
    'SAME_BARCODE':  ('#8b5cf6', '🟣', 'Duplicate barcode — must fix'),
}

_RES_OPTIONS = {
    'MERGE':      '🔀 Merge — keep one, move history, deactivate others',
    'KEEP_BOTH':  '✅ Keep both — legitimate separate entities',
    'RENAME':     '✏️ Rename — distinguish with suffix (city, branch)',
    'ASSIGN_NO':  '🔢 Assign number only — no other change',
}


def render_party_dedup():
    st.markdown(
        "<div style='background:#0f172a;border-left:4px solid #f59e0b;"
        "padding:10px 16px;border-radius:6px;margin-bottom:12px'>"
        "<b style='color:#f59e0b;font-size:1rem'>🔍 Party Dedup & Customer Numbers</b>"
        "<span style='color:#94a3b8;font-size:0.78rem;margin-left:10px'>"
        "Find conflicts → review each group → confirm → write</span>"
        "</div>", unsafe_allow_html=True
    )

    # ── Step 1: Run Analysis ──────────────────────────────────────────────────
    col_btn, col_info = st.columns([2, 3])
    with col_btn:
        if st.button("🔍 Analyse All Parties", type="primary",
                     use_container_width=True, key="pdd_analyse"):
            with st.spinner("Scanning all parties for conflicts..."):
                from modules.loaders.party_dedup import analyse_parties
                result = analyse_parties()
                st.session_state["_pdd_result"] = result
                # Reset all resolutions
                st.session_state["_pdd_resolutions"] = {}

    result = st.session_state.get("_pdd_result")
    if not result:
        st.info("Click **Analyse All Parties** to scan for duplicates and conflicts.")
        return

    if 'error' in result:
        st.error(f"Analysis failed: {result['error']}")
        return

    # ── Stats summary ─────────────────────────────────────────────────────────
    s = result['stats']
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total parties", s['total'])
    c2.metric("Clean (no conflict)", s['clean'], delta=None)
    c3.metric("In conflict groups", s['conflicts'],
              delta=f"-{s['conflicts']}" if s['conflicts'] else None,
              delta_color="inverse")
    c4.metric("Groups to resolve", s['groups'])

    if s['groups'] == 0:
        st.success("✅ No conflicts found! All parties are clean.")
        _show_assign_clean(result['clean'])
        return

    # Conflict type breakdown
    from collections import Counter
    ctype_counts = Counter(g['conflict_type'] for g in result['groups'])
    pills = []
    for ct, count in ctype_counts.items():
        icon = _CONFLICT_COLOURS.get(ct, ('','❓',''))[1]
        label = _CONFLICT_COLOURS.get(ct, ('','','Unknown'))[2]
        pills.append(f"{icon} {label}: **{count}**")
    st.caption("  ·  ".join(pills))

    st.markdown("---")

    # ── Step 2: Review each conflict group ───────────────────────────────────
    resolutions = st.session_state.get("_pdd_resolutions", {})

    st.markdown(f"### Review {s['groups']} conflict group(s)")
    st.caption("Decide what to do with each group. Changes only apply after you click **Confirm & Apply**.")

    for gi, group in enumerate(result['groups']):
        ct = group['conflict_type']
        colour, icon, label = _CONFLICT_COLOURS.get(ct, ('#94a3b8', '⚪', ct))
        parties = group['parties']

        with st.expander(
            f"{icon} Group {gi+1} — {label} | {len(parties)} parties",
            expanded=(gi == 0 or ct in ('EXACT_NAME', 'SAME_GSTIN'))
        ):
            # Show party cards
            for p in parties:
                _show_party_card(p)

            st.markdown("**Resolution:**")
            res_key = f"_pdd_res_{gi}"
            merge_key = f"_pdd_merge_{gi}"
            rename_key = f"_pdd_rename_{gi}"

            current_res = resolutions.get(gi, {}).get('action', group['suggested_resolution'])

            action = st.radio(
                "Action",
                list(_RES_OPTIONS.keys()),
                format_func=lambda x: _RES_OPTIONS[x],
                index=list(_RES_OPTIONS.keys()).index(current_res),
                horizontal=False,
                key=f"pdd_action_{gi}",
                label_visibility="collapsed"
            )

            extra = {}

            if action == 'MERGE':
                names = [p['party_name'] for p in parties]
                keep = st.selectbox(
                    "Keep which record?",
                    names,
                    key=f"pdd_keep_{gi}",
                    help="All orders and history will be moved to this record"
                )
                extra['keep_name'] = keep
                extra['keep_id']   = next(p['id'] for p in parties if p['party_name'] == keep)
                extra['delete_ids']= [p['id'] for p in parties if p['party_name'] != keep]

            elif action == 'RENAME':
                st.caption("Enter new names to distinguish the parties:")
                new_names = {}
                for p in parties:
                    nn = st.text_input(
                        f"New name for '{p['party_name']}'",
                        value=p['party_name'],
                        key=f"pdd_rename_{gi}_{p['id']}"
                    )
                    if nn.strip() != p['party_name']:
                        new_names[p['id']] = nn.strip()
                extra['new_names'] = new_names

            elif action == 'KEEP_BOTH':
                st.info("Both records will be kept. Customer numbers will be assigned to both.")

            elif action == 'ASSIGN_NO':
                st.info("Customer numbers will be assigned. No other changes.")

            # Store resolution
            resolutions[gi] = {
                'action':     action,
                'group_idx':  gi,
                'parties':    parties,
                'conflict':   ct,
                **extra
            }

        # Show resolved badge
        if gi in resolutions:
            a = resolutions[gi]['action']
            badge = {'MERGE':'🔀 Merge','KEEP_BOTH':'✅ Keep both',
                     'RENAME':'✏️ Rename','ASSIGN_NO':'🔢 Number only'}
            st.caption(f"Decision: {badge.get(a, a)}")

    st.session_state["_pdd_resolutions"] = resolutions

    st.markdown("---")

    # ── Step 3: Summary before confirm ───────────────────────────────────────
    all_resolved = len(resolutions) == len(result['groups'])
    if not all_resolved:
        st.warning(f"⚠️ {len(result['groups']) - len(resolutions)} group(s) still unresolved.")

    _show_assign_clean(result['clean'], show_button=False)

    # Confirm button
    confirmed = st.session_state.get("_pdd_confirmed", False)
    c_btn, c_warn = st.columns([2, 3])
    with c_btn:
        if st.button("⚠️ Confirm & Apply All Changes",
                     type="primary", key="pdd_confirm_btn",
                     use_container_width=True, disabled=not all_resolved):
            st.session_state["_pdd_confirmed"] = True
            st.rerun()

    if st.session_state.get("_pdd_confirmed"):
        st.session_state["_pdd_confirmed"] = False
        _apply_resolutions(resolutions, result['clean'])


def _show_party_card(p: dict):
    """Compact party info card."""
    cno = p.get('customer_no','—') or '—'
    mob = p.get('mobile','—') or '—'
    city = p.get('city','') or ''
    gstin = p.get('gstin','') or ''
    active = '✅' if p.get('is_active', True) else '❌'

    st.markdown(
        f"<div style='background:var(--color-background-secondary);"
        f"border:1px solid var(--color-border-tertiary);border-radius:6px;"
        f"padding:8px 12px;margin-bottom:4px;font-size:0.82rem'>"
        f"<b style='color:var(--color-text-primary)'>{p['party_name']}</b> "
        f"<span style='color:var(--color-text-secondary)'>{p.get('party_type','')}</span>"
        f"<span style='float:right;color:var(--color-text-tertiary)'>"
        f"{active} | 📞 {mob} | 🏙️ {city}</span><br>"
        f"<span style='color:var(--color-text-tertiary);font-size:0.75rem'>"
        f"GSTIN: {gstin or '—'} · Customer#: {cno} · "
        f"Barcode: {p.get('barcode','—') or '—'} · "
        f"ID: {str(p['id'])[:8]}..."
        f"</span></div>",
        unsafe_allow_html=True
    )


def _show_assign_clean(clean_parties: list, show_button: bool = True):
    """Show clean parties count and option to assign numbers."""
    no_number = [p for p in clean_parties if not p.get('customer_no')]
    if not no_number:
        st.success(f"✅ All {len(clean_parties)} clean parties already have customer numbers.")
        return

    st.info(
        f"**{len(no_number)}** clean parties (no conflicts) "
        f"need customer numbers assigned."
    )
    if show_button:
        if st.button(f"🔢 Assign Customer Numbers to {len(no_number)} clean parties",
                     key="pdd_assign_clean"):
            from modules.loaders.party_dedup import auto_assign_clean
            n = auto_assign_clean(clean_parties)
            st.success(f"✅ Assigned customer numbers to {n} parties.")
            st.session_state.pop("_pdd_result", None)
            st.rerun()


def _apply_resolutions(resolutions: dict, clean_parties: list):
    """Apply all confirmed resolutions."""
    from modules.loaders.party_dedup import (
        merge_parties, rename_party, assign_customer_numbers, auto_assign_clean
    )

    results = []
    errors  = []

    for gi, res in resolutions.items():
        action  = res['action']
        parties = res['parties']

        if action == 'MERGE':
            ok, msg = merge_parties(res['keep_id'], res['delete_ids'])
            if ok:
                assign_customer_numbers([res['keep_id']])
                results.append(f"✅ Group {gi+1}: {msg}")
            else:
                errors.append(f"❌ Group {gi+1} merge failed: {msg}")

        elif action == 'RENAME':
            new_names = res.get('new_names', {})
            for pid, new_name in new_names.items():
                ok, msg = rename_party(pid, new_name)
                if ok:
                    assign_customer_numbers([pid])
                    results.append(f"✅ Group {gi+1}: {msg}")
                else:
                    errors.append(f"❌ Group {gi+1} rename failed: {msg}")
            # Assign to unchanged ones too
            unchanged = [p['id'] for p in parties if p['id'] not in new_names]
            if unchanged:
                assign_customer_numbers(unchanged)

        elif action in ('KEEP_BOTH', 'ASSIGN_NO'):
            ids = [p['id'] for p in parties]
            assigned = assign_customer_numbers(ids)
            results.append(
                f"✅ Group {gi+1}: assigned customer numbers to {len(assigned)} parties"
            )

    # Assign to all clean parties
    n_clean = auto_assign_clean(clean_parties)
    if n_clean:
        results.append(f"✅ Assigned customer numbers to {n_clean} clean parties")

    # Show results
    if results:
        st.success(f"Applied {len(results)} action(s):")
        for r in results:
            st.markdown(r)
    if errors:
        st.error(f"{len(errors)} error(s):")
        for e in errors:
            st.error(e)

    # Clear state and re-run analysis
    st.session_state.pop("_pdd_result", None)
    st.session_state.pop("_pdd_resolutions", None)
    st.info("Re-run analysis to verify all conflicts are resolved.")
