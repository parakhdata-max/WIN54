"""
reporting/margin_leakage.py
============================
Margin Leakage Report — Priority 5, Report 3.

WHAT IT SHOWS
-------------
  • Manual price overrides (where unit_price deviates from catalogue > 10%)
  • Discount hotspots (orders with discount > threshold)
  • Margin by product group (gross margin %)
  • Top leaking SKUs (highest absolute discount given away)

HOW TO USE
----------
  from modules.backoffice.reporting.margin_leakage import (
      render_margin_leakage_report
  )
  render_margin_leakage_report(ctx)
"""

import streamlit as st
import pandas as pd
from typing import Optional, Tuple

# Thresholds (configurable here)
OVERRIDE_DEVIATION_PCT = 10.0   # flag if price deviates > 10% from catalogue
HIGH_DISCOUNT_PCT      = 15.0   # flag if discount > 15%


def render_margin_leakage_report(ctx) -> None:
    """Full Margin Leakage Report page."""
    st.title("🔍 Margin Leakage Report")
    st.caption("Identifies overrides, discounts, and margin erosion")

    col_p, _ = st.columns([2, 4])
    with col_p:
        period_days = st.selectbox(
            "Period",
            [30, 60, 90],
            index=1,
            format_func=lambda x: f"Last {x} days",
            key="leakage_period",
        )

    overrides, discounts, margin_by_group = _load_leakage_data(period_days)

    # ── KPI summary ───────────────────────────────────────────────────
    k1, k2, k3 = st.columns(3)
    k1.metric("Price Overrides",
              len(overrides) if overrides is not None else "–")
    k2.metric("High-Discount Orders",
              len(discounts) if discounts is not None else "–")
    k3.metric("Groups Analysed",
              len(margin_by_group) if margin_by_group is not None else "–")

    st.markdown("---")

    # ── Manual overrides ─────────────────────────────────────────────
    st.subheader(f"⚠️ Price Overrides (>{OVERRIDE_DEVIATION_PCT:.0f}% from catalogue)")
    if overrides is not None and not overrides.empty:
        st.dataframe(
            overrides.rename(columns={
                "order_no":       "Order #",
                "patient_name":   "Patient",
                "product_name":   "Product",
                "catalogue_price":"Catalogue (₹)",
                "actual_price":   "Actual (₹)",
                "deviation_pct":  "Deviation %",
                "changed_by":     "Changed By",
                "created_at":     "Date",
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("✅ No significant price overrides found.")

    st.markdown("---")

    # ── Discount hotspots ─────────────────────────────────────────────
    st.subheader(f"💸 Discount Hotspots (>{HIGH_DISCOUNT_PCT:.0f}%)")
    if discounts is not None and not discounts.empty:
        st.dataframe(
            discounts.rename(columns={
                "order_no":        "Order #",
                "patient_name":    "Patient",
                "product_name":    "Product",
                "discount_percent":"Discount %",
                "discount_amount": "Discount (₹)",
                "billing_total":   "Billed (₹)",
                "approved_by":     "Approved By",
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("✅ No high-discount orders found.")

    st.markdown("---")

    # ── Margin by group ───────────────────────────────────────────────
    st.subheader("📊 Gross Margin by Product Group")
    if margin_by_group is not None and not margin_by_group.empty:
        margin_chart = margin_by_group.set_index("main_group")[["gross_margin_pct"]]
        st.bar_chart(margin_chart)
        st.dataframe(
            margin_by_group.rename(columns={
                "main_group":        "Product Group",
                "revenue":           "Revenue (₹)",
                "cost":              "Cost (₹)",
                "gross_margin_pct":  "Margin %",
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No margin data available.")


def _load_leakage_data(period_days: int) -> Tuple:
    try:
        from modules.sql_adapter import run_query

        # Price overrides
        override_rows = run_query(f"""
            SELECT
                o.order_no,
                o.patient_name,
                p.product_name,
                COALESCE(NULLIF(oi.original_price, 0), oi.unit_price) AS catalogue_price,
                oi.unit_price                                      AS actual_price,
                ROUND(
                    ABS(oi.unit_price - COALESCE(NULLIF(oi.original_price, 0), oi.unit_price))
                    / NULLIF(COALESCE(NULLIF(oi.original_price, 0), oi.unit_price), 0) * 100, 1
                )                                                  AS deviation_pct,
                o.created_by                                       AS changed_by,
                o.created_at::date                                 AS created_at
            FROM order_lines oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.created_at >= NOW() - INTERVAL '{period_days} days'
              AND o.status NOT IN ('CANCELLED')
              AND COALESCE(NULLIF(oi.original_price, 0), oi.unit_price) > 0
              AND ABS(oi.unit_price - COALESCE(NULLIF(oi.original_price, 0), oi.unit_price))
                  / COALESCE(NULLIF(oi.original_price, 0), oi.unit_price) * 100 > {OVERRIDE_DEVIATION_PCT}
            ORDER BY deviation_pct DESC
            LIMIT 100
        """)
        overrides = pd.DataFrame(override_rows) if override_rows else pd.DataFrame()

        # Discount hotspots
        discount_rows = run_query(f"""
            SELECT
                o.order_no,
                o.patient_name,
                p.product_name,
                oi.discount_percent,
                oi.discount_amount,
                oi.billing_total,
                COALESCE(oi.discount_by, o.updated_by, o.created_by) AS approved_by
            FROM order_lines oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.created_at >= NOW() - INTERVAL '{period_days} days'
              AND o.status NOT IN ('CANCELLED')
              AND COALESCE(oi.discount_percent, 0) > {HIGH_DISCOUNT_PCT}
            ORDER BY oi.discount_percent DESC
            LIMIT 100
        """)
        discounts = pd.DataFrame(discount_rows) if discount_rows else pd.DataFrame()

        # Margin by group
        margin_rows = run_query(f"""
            SELECT
                p.main_group,
                SUM(oi.billing_total)::float                       AS revenue,
                SUM(oi.quantity * COALESCE(oi.cost_price, 0))::float AS cost,
                ROUND(
                    (1 - SUM(oi.quantity * COALESCE(oi.cost_price, 0))
                           / NULLIF(SUM(oi.billing_total), 0)) * 100, 1
                )::float                                           AS gross_margin_pct
            FROM order_lines oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.created_at >= NOW() - INTERVAL '{period_days} days'
              AND o.status NOT IN ('CANCELLED')
            GROUP BY p.main_group
            ORDER BY revenue DESC
        """)
        margin_by_group = pd.DataFrame(margin_rows) if margin_rows else pd.DataFrame()

        return overrides, discounts, margin_by_group

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[MarginLeakage] Load failed: {e}")
        return None, None, None
