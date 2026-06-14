"""
modules/analytics/import_dashboard.py
=======================================
Import Analytics Dashboard — DV ERP

Visualizes ingestion health using:
  - v_import_summary        (per file_type totals)
  - v_user_import_activity  (per operator activity)
  - loader_import_log       (recent import timeline)
  - loader_row_history      (dedup / row coverage)

Read-only. No DB writes. Safe to open at any time.
"""

import streamlit as st
from datetime import datetime, timedelta


# ── Data helpers ──────────────────────────────────────────────────────────────

def _q(sql: str, params=None):
    """Safe query wrapper — returns [] on any error."""
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        st.caption(f"⚠️ Query unavailable: {e}")
        return []


# ── Status color helpers ──────────────────────────────────────────────────────

_STATUS_ICON = {
    "OK":      "🟢",
    "PARTIAL": "🟡",
    "FAILED":  "🔴",
    "DRY":     "🔵",
}

def _status_badge(status: str) -> str:
    return f"{_STATUS_ICON.get(status, '⚪')} {status}"


# ── Metric card helper ────────────────────────────────────────────────────────

def _metric_row(metrics: list):
    """Render a row of st.metric cards. metrics = list of (label, value, delta?)."""
    cols = st.columns(len(metrics))
    for col, m in zip(cols, metrics):
        if len(m) == 3:
            col.metric(m[0], m[1], m[2])
        else:
            col.metric(m[0], m[1])


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════

def render_import_dashboard():
    st.title("📊 Import Analytics")
    st.caption("Real-time ingestion health — powered by loader_import_log & audit views.")

    # ── Top-level KPIs ────────────────────────────────────────────────────────
    kpi = _q("""
        SELECT
            COUNT(*)                                               AS total_imports,
            SUM(rows_total)                                        AS total_rows,
            SUM(rows_ok)                                           AS total_ok,
            SUM(error_count)                                       AS total_errors,
            SUM(CASE WHEN status = 'OK'     THEN 1 ELSE 0 END)    AS ok_count,
            SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END)    AS failed_count,
            SUM(CASE WHEN status = 'DRY'    THEN 1 ELSE 0 END)    AS dry_count,
            ROUND(AVG(duration_s)::NUMERIC, 2)                     AS avg_duration_s
        FROM loader_import_log
    """)

    if kpi:
        k = kpi[0]
        total = k.get("total_imports") or 0
        ok    = k.get("ok_count")      or 0
        fail  = k.get("failed_count")  or 0
        rows  = k.get("total_rows")    or 0
        errs  = k.get("total_errors")  or 0
        dur   = k.get("avg_duration_s") or 0.0

        success_rate = round((ok / total * 100), 1) if total > 0 else 0.0

        _metric_row([
            ("Total Imports",    total),
            ("✅ Successful",    ok),
            ("❌ Failed",        fail),
            ("📦 Rows Processed", f"{rows:,}" if rows else "0"),
            ("⚠️ Total Errors",  f"{errs:,}" if errs else "0"),
            ("⚡ Avg Duration",  f"{dur}s"),
        ])

        # Success rate progress bar
        st.markdown(f"**Overall Success Rate: {success_rate}%**")
        color = "🟢" if success_rate >= 95 else ("🟡" if success_rate >= 70 else "🔴")
        st.progress(int(success_rate), text=f"{color} {success_rate}% of imports succeeded")

        # ── Import Health Score ───────────────────────────────────────────
        st.divider()
        st.markdown("### 🏥 Import Health Score")
        st.caption("Composite score: success rate, error volume, avg import speed.")

        raw_score = success_rate
        if fail > 0:
            raw_score -= min(fail * 5, 20)           # up to -20 for failures
        if errs > 0:
            raw_score -= min(int(errs / 1000) * 2, 10)  # up to -10 for high error counts
        if float(dur) > 30:
            raw_score -= 5                            # -5 for slow imports
        health_score = max(0.0, min(100.0, round(raw_score, 1)))

        h_icon  = "🟢" if health_score >= 90 else ("🟡" if health_score >= 70 else "🔴")
        h_label = "Excellent" if health_score >= 90 else ("Needs Attention" if health_score >= 70 else "Critical")

        hcol1, hcol2 = st.columns([1, 3])
        with hcol1:
            st.metric("Health Score", f"{health_score} / 100")
            st.caption(f"{h_icon} {h_label}")
        with hcol2:
            st.progress(
                int(health_score),
                text=f"{h_icon} {h_label} — success rate · error volume · avg duration"
            )

    else:
        st.info("No imports recorded yet. Run your first import to populate analytics.")

    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs([
        "📁 By File Type",
        "👤 User Activity",
        "🕒 Recent Imports",
        "🔍 Row Coverage",
    ])

    # ── TAB 1: By File Type ───────────────────────────────────────────────────
    with tab1:
        st.subheader("Import Summary by File Type")
        summary = _q("SELECT * FROM v_import_summary ORDER BY file_type")

        if summary:
            # Enrich with success rate
            rows_out = []
            for r in summary:
                total_i  = r.get("total_imports") or 0
                ok_c     = r.get("ok_count")      or 0
                rate     = round(ok_c / total_i * 100, 1) if total_i > 0 else 0.0
                last     = str(r.get("last_import_at", ""))[:16]
                rows_out.append({
                    "File Type":      r.get("file_type"),
                    "Total Imports":  total_i,
                    "✅ OK":          ok_c,
                    "🟡 Partial":     r.get("partial_count", 0),
                    "❌ Failed":      r.get("failed_count",  0),
                    "🔵 Dry Runs":    r.get("dry_run_count", 0),
                    "Rows Processed": f"{r.get('total_rows_processed', 0):,}",
                    "Rows OK":        f"{r.get('total_rows_ok', 0):,}",
                    "Total Errors":   r.get("total_errors", 0),
                    "Avg Duration":   f"{r.get('avg_duration_s', 0)}s",
                    "Success Rate":   f"{rate}%",
                    "Last Import":    last,
                })
            import pandas as pd
            df_summary = pd.DataFrame(rows_out)
            st.dataframe(
                df_summary,
                use_container_width=True,
                hide_index=True,
            )

            # ── Charts ────────────────────────────────────────────────────
            st.markdown("#### 📊 Visual Breakdown")
            chart_col1, chart_col2 = st.columns(2)

            # Build a numeric-only df for charting (use raw summary rows)
            chart_data = pd.DataFrame([{
                "file_type":           r.get("file_type", ""),
                "total_imports":       r.get("total_imports", 0),
                "total_rows_processed": r.get("total_rows_processed", 0) or 0,
            } for r in summary]).set_index("file_type")

            with chart_col1:
                st.markdown("**Total Imports by File Type**")
                st.bar_chart(chart_data["total_imports"])

            with chart_col2:
                st.markdown("**Rows Processed by File Type**")
                st.bar_chart(chart_data["total_rows_processed"])

            # ── Timeline trend using recent imports ───────────────────────
            st.markdown("#### 📈 Import Volume Over Time")
            trend = _q("""
                SELECT
                    DATE_TRUNC('day', imported_at)::DATE  AS import_day,
                    file_type,
                    COUNT(*)                              AS imports_that_day,
                    COALESCE(SUM(rows_ok), 0)             AS rows_that_day
                FROM loader_import_log
                WHERE imported_at >= NOW() - INTERVAL '30 days'
                GROUP BY import_day, file_type
                ORDER BY import_day
            """)

            if trend:
                trend_df = pd.DataFrame(trend)
                trend_pivot = (
                    trend_df
                    .pivot_table(
                        index="import_day",
                        columns="file_type",
                        values="rows_that_day",
                        aggfunc="sum",
                        fill_value=0,
                    )
                )
                st.line_chart(trend_pivot)
                st.caption("Rows processed per day (last 30 days), grouped by file type.")
            else:
                st.caption("Not enough data yet for a time trend. Run more imports.")

        else:
            st.info("No data yet — import summary will appear after first import.")

    # ── TAB 2: User Activity ──────────────────────────────────────────────────
    with tab2:
        st.subheader("Operator Activity")
        users = _q("SELECT * FROM v_user_import_activity ORDER BY last_active_at DESC")

        if users:
            import pandas as pd
            rows_out = []
            for r in users:
                rows_out.append({
                    "Operator":       r.get("user", "—"),
                    "Total Imports":  r.get("total_imports", 0),
                    "LIVE Imports":   r.get("live_imports",  0),
                    "Rows OK":        f"{r.get('total_rows_ok', 0):,}",
                    "Last Active":    str(r.get("last_active_at", ""))[:16],
                })
            st.dataframe(
                pd.DataFrame(rows_out),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No user activity yet.")

    # ── TAB 3: Recent Imports Timeline ────────────────────────────────────────
    with tab3:
        st.subheader("Recent Import Log")

        col1, col2 = st.columns([2, 1])
        with col1:
            limit = st.slider("Show last N imports", 10, 100, 20, step=10)
        with col2:
            ft_filter = st.selectbox(
                "Filter by type",
                ["ALL", "PRODUCT", "FRAME", "PARTY", "PATIENT",
                 "OPHLENS", "CLENS", "SOL", "BLANK"],
            )

        where = f"AND file_type = '{ft_filter}'" if ft_filter != "ALL" else ""
        recent = _q(f"""
            SELECT
                id AS import_id,
                file_name,
                file_type,
                import_mode AS mode,
                stock_mode,
                status,
                rows_total,
                rows_ok,
                skipped_rows AS rows_skipped,
                error_count,
                user_name AS "user",
                ROUND(duration_s::NUMERIC, 2)  AS duration_s,
                imported_at
            FROM loader_import_log
            WHERE 1=1 {where}
            ORDER BY imported_at DESC
            LIMIT %(limit)s
        """, {"limit": limit})

        if recent:
            import pandas as pd
            rows_out = []
            for r in recent:
                status = r.get("status", "")
                rows_out.append({
                    "Import ID":   str(r.get("import_id", ""))[:8] + "…",
                    "File":        r.get("file_name", ""),
                    "Type":        r.get("file_type", ""),
                    "Mode":        r.get("mode", ""),
                    "Stock":       r.get("stock_mode", ""),
                    "Status":      _status_badge(status),
                    "Rows Total":  r.get("rows_total", 0),
                    "Rows OK":     r.get("rows_ok", 0),
                    "Skipped":     r.get("rows_skipped", 0),
                    "Errors":      r.get("error_count", 0),
                    "Operator":    r.get("user", ""),
                    "Duration":    f"{r.get('duration_s', 0)}s",
                    "At":          str(r.get("imported_at", ""))[:16],
                })
            df = pd.DataFrame(rows_out)
            st.dataframe(df, use_container_width=True, hide_index=True)

            # Download
            csv = df.to_csv(index=False)
            st.download_button(
                "⬇️ Download as CSV",
                csv,
                file_name=f"import_log_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )
        else:
            st.info("No imports match this filter.")

    # ── TAB 4: Row Coverage ───────────────────────────────────────────────────
    with tab4:
        st.subheader("Row Dedup Coverage")
        st.caption("Total unique row hashes stored in loader_row_history — your duplicate protection layer.")

        coverage = _q("""
            SELECT
                file_type,
                COUNT(*)               AS unique_rows_hashed,
                MIN(created_at)        AS first_seen,
                MAX(created_at)        AS last_seen
            FROM loader_row_history
            GROUP BY file_type
            ORDER BY file_type
        """)

        if coverage:
            import pandas as pd
            rows_out = []
            for r in coverage:
                rows_out.append({
                    "File Type":        r.get("file_type"),
                    "Unique Rows Hashed": f"{r.get('unique_rows_hashed', 0):,}",
                    "First Import":     str(r.get("first_seen", ""))[:16],
                    "Last Import":      str(r.get("last_seen",  ""))[:16],
                })
            st.dataframe(
                pd.DataFrame(rows_out),
                use_container_width=True,
                hide_index=True,
            )

            total_hashes = _q("SELECT COUNT(*) AS n FROM loader_row_history")
            if total_hashes:
                st.metric("Total Hashes Stored", f"{total_hashes[0].get('n', 0):,}")
        else:
            st.info("No row hashes yet — run a LIVE import to populate dedup history.")
