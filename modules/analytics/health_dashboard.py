"""
modules/analytics/health_dashboard.py
=======================================
Import Health Dashboard — DV ERP Ingestion Command Center

Renders a full Streamlit page showing:
  - Overall ingestion health score
  - Anomaly alerts
  - Recent import history table
  - Per-file-type breakdown
  - Channel field detections
"""

import logging
import streamlit as st

logger = logging.getLogger(__name__)


def render_import_health():
    """
    Main entry point — called from app.py router.
    Renders the full Import Health Dashboard page.
    """
    st.title("🧠 Import Health Dashboard")
    st.caption("Live ingestion observability — anomalies, scores, and history")

    # ── Imports ───────────────────────────────────────────────────────────────
    try:
        from modules.sql_adapter import run_query
    except Exception as e:
        st.error(f"❌ Database unavailable: {e}")
        return

    try:
        from modules.analytics.anomaly_detector import (
            detect_anomalies,
            get_import_health_score,
        )
    except Exception as e:
        st.error(f"❌ Anomaly detector unavailable: {e}")
        return

    # ════════════════════════════════════════════════════════════
    # ROW 1 — Health score + anomaly status
    # ════════════════════════════════════════════════════════════

    score     = get_import_health_score(lookback=20)
    anomalies = detect_anomalies(lookback=10)

    col1, col2, col3 = st.columns(3)

    with col1:
        colour = "normal" if score >= 80 else ("off" if score >= 50 else "inverse")
        st.metric(
            label="🏥 Ingestion Health Score",
            value=f"{score}/100",
            delta="Healthy" if score >= 80 else ("Degraded" if score >= 50 else "Critical"),
            delta_color=colour,
        )

    with col2:
        anomaly_count = len(anomalies)
        critical_count = sum(1 for a in anomalies if a["severity"] == "critical")
        st.metric(
            label="🚨 Active Anomalies",
            value=anomaly_count,
            delta=f"{critical_count} critical" if critical_count else "All clear",
            delta_color="inverse" if critical_count else "off",
        )

    with col3:
        try:
            recent = run_query(
                """
                SELECT COUNT(*) AS cnt
                FROM loader_import_log
                WHERE imported_at >= NOW() - INTERVAL '24 hours'
                  AND COALESCE(import_mode, '') != 'DRY'
                """
            )
            today_count = recent[0]["cnt"] if recent else 0
        except Exception:
            today_count = "—"
        st.metric(label="📥 Imports Today", value=today_count)

    st.divider()

    # ════════════════════════════════════════════════════════════
    # ROW 2 — Anomaly alerts
    # ════════════════════════════════════════════════════════════

    if anomalies:
        st.subheader("⚠️ Detected Anomalies")
        for a in anomalies:
            if a["severity"] == "critical":
                st.error(f"**{a['message']}**  \n{a.get('detail', '')}")
            else:
                st.warning(f"**{a['message']}**  \n{a.get('detail', '')}")
    else:
        st.success("✅ No anomalies detected — ingestion is healthy")

    st.divider()

    # ════════════════════════════════════════════════════════════
    # ROW 3 — Recent import history
    # ════════════════════════════════════════════════════════════

    st.subheader("📋 Recent Import History")

    try:
        history = run_query(
            """
            SELECT
                id AS import_id,
                file_type,
                import_mode AS mode,
                stock_mode,
                status,
                rows_total,
                rows_ok,
                skipped_rows AS rows_skipped,
                error_count,
                ROUND(duration_s::numeric, 2)   AS duration_s,
                user_name AS "user",
                imported_at
            FROM loader_import_log
            ORDER BY imported_at DESC
            LIMIT 50
            """
        )

        if history:
            import pandas as pd

            df = pd.DataFrame(history)

            # Colour-code status column
            def style_status(val):
                colours = {
                    "OK":      "background-color: #d4edda; color: #155724",
                    "PARTIAL": "background-color: #fff3cd; color: #856404",
                    "FAILED":  "background-color: #f8d7da; color: #721c24",
                    "DRY":     "background-color: #d1ecf1; color: #0c5460",
                }
                return colours.get(val, "")

            styled = df.style.applymap(style_status, subset=["status"])
            st.dataframe(styled, use_container_width=True)
        else:
            st.info("No import history found.")

    except Exception as e:
        st.warning(f"Could not load history: {e}")

    st.divider()

    # ════════════════════════════════════════════════════════════
    # ROW 4 — Per file-type breakdown
    # ════════════════════════════════════════════════════════════

    st.subheader("📊 Per File-Type Summary")

    try:
        summary = run_query(
            """
            SELECT
                file_type,
                COUNT(*)                        AS total_imports,
                SUM(rows_total)                 AS total_rows,
                SUM(error_count)                AS total_errors,
                ROUND(AVG(duration_s)::numeric, 2) AS avg_duration_s,
                SUM(CASE WHEN status = 'OK'      THEN 1 ELSE 0 END) AS ok_count,
                SUM(CASE WHEN status = 'FAILED'  THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN status = 'PARTIAL' THEN 1 ELSE 0 END) AS partial_count,
                MAX(imported_at)                AS last_import
            FROM loader_import_log
            WHERE COALESCE(import_mode, '') != 'DRY'
            GROUP BY file_type
            ORDER BY total_imports DESC
            """
        )

        if summary:
            import pandas as pd
            st.dataframe(pd.DataFrame(summary), use_container_width=True)
        else:
            st.info("No summary data available.")

    except Exception as e:
        st.warning(f"Could not load summary: {e}")

    st.divider()

    # ════════════════════════════════════════════════════════════
    # ROW 5 — Channel field detections
    # ════════════════════════════════════════════════════════════

    st.subheader("🌐 Online / Channel Readiness")

    try:
        from modules.loaders.channel_detector import OPTIONAL_CHANNEL_FIELDS

        st.info(
            "The loader is ready to detect these online/channel fields "
            "when present in uploaded Excel files. No extra configuration needed."
        )

        col_a, col_b = st.columns(2)
        half = len(OPTIONAL_CHANNEL_FIELDS) // 2
        with col_a:
            for f in OPTIONAL_CHANNEL_FIELDS[:half]:
                st.write(f"✅ `{f}`")
        with col_b:
            for f in OPTIONAL_CHANNEL_FIELDS[half:]:
                st.write(f"✅ `{f}`")

    except Exception as e:
        st.warning(f"Channel detector not available: {e}")
