"""
modules/loaders/schema_evolution_ui.py
========================================
Schema Evolution Viewer — DV ERP

Visualizes schema drift over time using loader_schema_history.

Shows:
  - Timeline of schema changes per file type
  - Diff details: new columns, missing columns, suggestions
  - Schema snapshot (full column list at time of import)
  - Approved-by operator trail

This is rare even in large ERPs — full column evolution memory.
"""

import json
import streamlit as st


# ── Helpers ───────────────────────────────────────────────────────────────────

def _q(sql: str, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        st.caption(f"⚠️ Query unavailable: {e}")
        return []


def _parse_summary(raw) -> dict:
    """Parse change_summary — handles dict or JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


# ── Diff renderer ─────────────────────────────────────────────────────────────

def _render_diff(summary: dict):
    """Render a structured schema diff from change_summary."""

    new_cols     = summary.get("new_columns",     [])
    missing_cols = summary.get("missing_columns", [])
    filled_cols  = summary.get("newly_filled",    [])
    suggestions  = summary.get("suggestions",     {})
    confidence   = summary.get("confidence",      {})
    snapshot     = summary.get("schema_snapshot", [])

    col1, col2, col3 = st.columns(3)

    with col1:
        if new_cols:
            st.markdown("**🆕 New Columns (unknown)**")
            for c in new_cols:
                sug  = suggestions.get(c)
                conf = confidence.get(c, 0)
                if sug:
                    st.markdown(f"- `{c}` → suggested: `{sug}` ({int(conf*100)}%)")
                else:
                    st.markdown(f"- `{c}` _(no match)_")
        else:
            st.markdown("**🆕 New Columns**")
            st.caption("None")

    with col2:
        if missing_cols:
            st.markdown("**⚠️ Missing Columns**")
            for c in missing_cols:
                st.markdown(f"- `{c}`")
        else:
            st.markdown("**⚠️ Missing Columns**")
            st.caption("None")

    with col3:
        if filled_cols:
            st.markdown("**🟡 Newly Filled**")
            for c in filled_cols:
                st.markdown(f"- `{c}`")
        else:
            st.markdown("**🟡 Newly Filled**")
            st.caption("None")

    if snapshot:
        with st.expander(f"📋 Full Schema Snapshot ({len(snapshot)} columns)"):
            st.code("  |  ".join(sorted(snapshot)), language="text")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════

def render_schema_evolution():
    st.title("🧬 Schema Evolution")
    st.caption(
        "Full column evolution memory — tracks every schema change detected across imports. "
        "Powered by loader_schema_history."
    )

    # ── Filters ───────────────────────────────────────────────────────────────
    col1, col2 = st.columns([2, 1])
    with col1:
        ft_filter = st.selectbox(
            "Filter by File Type",
            ["ALL", "PRODUCT", "FRAME", "PARTY", "PATIENT",
             "OPHLENS", "CLENS", "SOL", "BLANK"],
        )
    with col2:
        limit = st.slider("Show last N records", 10, 100, 25, step=5)

    where = f"WHERE file_type = '{ft_filter}'" if ft_filter != "ALL" else ""

    # ── Summary stats ─────────────────────────────────────────────────────────
    st.divider()
    stats = _q(f"""
        SELECT
            COUNT(*)                AS total_events,
            COUNT(DISTINCT file_type) AS unique_types,
            MIN(approved_at)        AS first_change,
            MAX(approved_at)        AS latest_change
        FROM loader_schema_history
        {where}
    """)

    if stats and stats[0].get("total_events"):
        s = stats[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Schema Events",   s.get("total_events", 0))
        c2.metric("File Types",      s.get("unique_types", 0))
        c3.metric("First Change",    str(s.get("first_change",  ""))[:10])
        c4.metric("Latest Change",   str(s.get("latest_change", ""))[:10])
    else:
        st.info(
            "No schema history recorded yet. "
            "Schema changes are captured automatically when you import files "
            "with new or missing columns."
        )
        return

    # ── Per-type change count ─────────────────────────────────────────────────
    st.divider()
    st.subheader("Schema Change Frequency")
    freq = _q("""
        SELECT
            file_type,
            COUNT(*)          AS change_events,
            MAX(approved_at)  AS last_change,
            COUNT(DISTINCT approved_by) AS unique_operators
        FROM loader_schema_history
        GROUP BY file_type
        ORDER BY change_events DESC
    """)

    if freq:
        import pandas as pd
        st.dataframe(
            pd.DataFrame([{
                "File Type":       r.get("file_type"),
                "Schema Events":   r.get("change_events", 0),
                "Unique Operators":r.get("unique_operators", 0),
                "Last Change":     str(r.get("last_change", ""))[:16],
            } for r in freq]),
            use_container_width=True,
            hide_index=True,
        )

    # ── Evolution timeline ────────────────────────────────────────────────────
    st.divider()
    st.subheader("Evolution Timeline")

    history = _q(f"""
        SELECT
            id,
            file_type,
            file_name,
            approved_by,
            approved_at,
            change_summary
        FROM loader_schema_history
        {where}
        ORDER BY approved_at DESC
        LIMIT %s
    """, (limit,))

    if not history:
        st.info("No records match this filter.")
        return

    for row in history:
        summary  = _parse_summary(row.get("change_summary", {}))
        at       = str(row.get("approved_at", ""))[:16]
        ft       = row.get("file_type",  "")
        fn       = row.get("file_name",  "")
        by       = row.get("approved_by", "user")

        new_count  = len(summary.get("new_columns",     []))
        miss_count = len(summary.get("missing_columns", []))
        fill_count = len(summary.get("newly_filled",    []))

        # Build expander label with change summary
        change_tags = []
        if new_count:  change_tags.append(f"🆕 {new_count} new")
        if miss_count: change_tags.append(f"⚠️ {miss_count} missing")
        if fill_count: change_tags.append(f"🟡 {fill_count} filled")
        change_str = "  ·  ".join(change_tags) if change_tags else "Minor / No column changes"

        label = f"**{ft}** — {at}  ·  `{fn}`  ·  by `{by}`  ·  {change_str}"

        with st.expander(label):
            _render_diff(summary)

            # Pretty-printed JSON diff (readable inline)
            st.markdown("**🔬 Structured Diff (JSON)**")
            st.json(summary)

            # Compact raw toggle for copy-paste
            with st.expander("📋 Raw JSON (copy-paste friendly)"):
                raw_str = json.dumps(summary, indent=2, default=str)
                st.code(raw_str, language="json")

    # ── Schema fingerprint comparison ─────────────────────────────────────────
    st.divider()
    with st.expander("🔬 Compare Schema Snapshots"):
        st.caption(
            "Select two records to compare their schema snapshots "
            "and see what columns were added or removed between imports."
        )

        if len(history) < 2:
            st.info("Need at least 2 records to compare.")
        else:
            def _label(r):
                return f"{r.get('file_type')} | {str(r.get('approved_at',''))[:16]} | {r.get('file_name','')}"

            col_a, col_b = st.columns(2)
            with col_a:
                rec_a = st.selectbox("Record A (newer)", history,      format_func=_label, key="rec_a")
            with col_b:
                rec_b = st.selectbox("Record B (older)", history[1:],  format_func=_label, key="rec_b")

            if rec_a and rec_b:
                sum_a = _parse_summary(rec_a.get("change_summary", {}))
                sum_b = _parse_summary(rec_b.get("change_summary", {}))
                snap_a = set(sum_a.get("schema_snapshot", []))
                snap_b = set(sum_b.get("schema_snapshot", []))

                if snap_a and snap_b:
                    added   = snap_a - snap_b
                    removed = snap_b - snap_a
                    same    = snap_a & snap_b

                    c1, c2, c3 = st.columns(3)
                    with c1:
                        st.markdown(f"**➕ Added in A ({len(added)})**")
                        for c in sorted(added):
                            st.markdown(f"- `{c}`")
                    with c2:
                        st.markdown(f"**➖ Removed in A ({len(removed)})**")
                        for c in sorted(removed):
                            st.markdown(f"- `{c}`")
                    with c3:
                        st.markdown(f"**✅ Same ({len(same)})**")
                        st.caption(f"{len(same)} columns in common")
                else:
                    st.info(
                        "Schema snapshots not available for these records. "
                        "Snapshots are stored starting from the audit wiring patch. "
                        "Re-import files to generate snapshots."
                    )
