"""
reporting/supplier_performance.py
===================================
Supplier Performance Dashboard — Priority 5, Report 1.

WHAT IT SHOWS
-------------
  • Avg delivery time per supplier (bar chart)
  • Delay % (% POs delivered after expected date)
  • Volume share (pie)
  • Score table from supplier_intelligence

HOW TO USE
----------
  from modules.backoffice.reporting.supplier_performance import (
      render_supplier_performance_dashboard
  )

  # In app.py or as a plugin:
  render_supplier_performance_dashboard(ctx)
"""

import streamlit as st
import pandas as pd
from typing import Optional


def render_supplier_performance_dashboard(ctx) -> None:
    """Full Supplier Performance Dashboard page."""
    st.title("📊 Supplier Performance")
    st.caption("Last 180 days · All active suppliers")

    data = _load_supplier_performance()

    if data is None:
        st.error("⚠️ Could not load supplier data.")
        return

    if data.empty:
        st.info("No supplier order history found.")
        return

    # ── KPI row ───────────────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Suppliers", len(data))
    col2.metric("Avg Delivery Days",
                f"{data['delivery_days_avg'].mean():.1f}d")
    col3.metric("Overall Delay %",
                f"{data['delay_pct'].mean():.1f}%")
    col4.metric("Total POs",
                int(data["po_count"].sum()))

    st.markdown("---")

    # ── Score table ───────────────────────────────────────────────────
    st.subheader("🏆 Supplier Rankings")
    from modules.procurement.supplier_intelligence import get_scored_suppliers
    scored = get_scored_suppliers()

    if scored:
        score_df = pd.DataFrame(scored)[[
            "rank", "name", "score", "grade",
            "delivery_days_avg", "rejection_pct", "po_count"
        ]].rename(columns={
            "rank":               "Rank",
            "name":               "Supplier",
            "score":              "Score",
            "grade":              "Grade",
            "delivery_days_avg":  "Avg Delivery (d)",
            "rejection_pct":      "Rejection %",
            "po_count":           "PO Count",
        })
        score_df["Avg Delivery (d)"] = score_df["Avg Delivery (d)"].round(1)
        score_df["Rejection %"]      = score_df["Rejection %"].round(1)
        st.dataframe(score_df, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Charts ────────────────────────────────────────────────────────
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("⏱️ Avg Delivery Days")
        chart_data = data.set_index("name")[["delivery_days_avg"]].sort_values(
            "delivery_days_avg"
        )
        st.bar_chart(chart_data)

    with col_b:
        st.subheader("📦 Volume Share")
        vol_data = data.set_index("name")[["po_count"]]
        st.bar_chart(vol_data)

    st.markdown("---")

    # ── Delay detail ─────────────────────────────────────────────────
    st.subheader("⏰ Delay Analysis")
    delay_df = data[["name", "delay_pct", "po_count"]].copy()
    delay_df.columns = ["Supplier", "Delay %", "Total POs"]
    delay_df = delay_df.sort_values("Delay %", ascending=False)
    delay_df["Delay %"] = delay_df["Delay %"].round(1)
    st.dataframe(delay_df, use_container_width=True, hide_index=True)


def _load_supplier_performance() -> Optional[pd.DataFrame]:
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                p.party_name                                          AS name,
                COALESCE(COUNT(so.id), 0)::int                        AS po_count,
                COALESCE(
                    AVG(EXTRACT(DAY FROM
                        (COALESCE(so.received_at, so.updated_at) - so.created_at)
                    )), 0
                )::float                                              AS delivery_days_avg,
                COALESCE(
                    100.0 * SUM(
                        CASE
                            WHEN so.expected_delivery IS NOT NULL
                             AND COALESCE(so.received_at, so.updated_at)::date
                                 > so.expected_delivery
                            THEN 1 ELSE 0
                        END
                    ) / NULLIF(COUNT(so.id), 0), 0
                )::float                                              AS delay_pct
            FROM parties p
            LEFT JOIN supplier_orders so ON so.supplier_id = p.id
                AND so.created_at >= NOW() - INTERVAL '180 days'
            WHERE LOWER(COALESCE(p.roletype,'')) IN ('supplier','vendor')
              AND COALESCE(p.isactive, true) = true
            GROUP BY p.id, p.party_name
            ORDER BY po_count DESC
        """)
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[SupplierPerf] Load failed: {e}")
        return None
