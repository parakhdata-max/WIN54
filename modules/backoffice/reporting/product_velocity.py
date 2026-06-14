"""
reporting/product_velocity.py
==============================
Product Velocity Dashboard — Priority 5, Report 2.

WHAT IT SHOWS
-------------
  • Fast-moving SKUs (top 20 by units sold / 30d)
  • Dead stock (no movement in 60+ days)
  • Seasonal trend chart (last 12 months by product group)
  • Low stock warnings

HOW TO USE
----------
  from modules.backoffice.reporting.product_velocity import (
      render_product_velocity_dashboard
  )
  render_product_velocity_dashboard(ctx)
"""

import streamlit as st
import pandas as pd
from typing import Optional


def render_product_velocity_dashboard(ctx) -> None:
    """Full Product Velocity Dashboard page."""
    st.title("⚡ Product Velocity")
    st.caption("Sales movement analysis · Last 90 days")

    col_period, _ = st.columns([2, 4])
    with col_period:
        period_days = st.selectbox(
            "Period",
            [30, 60, 90, 180],
            index=2,
            format_func=lambda x: f"Last {x} days",
            key="velocity_period",
        )

    fast, dead, seasonal = _load_velocity_data(period_days)

    # ── Fast movers ───────────────────────────────────────────────────
    st.subheader("🚀 Fast-Moving SKUs")
    if fast is not None and not fast.empty:
        st.dataframe(
            fast.head(20).rename(columns={
                "product_name": "Product",
                "brand":        "Brand",
                "main_group":   "Group",
                "units_sold":   "Units Sold",
                "velocity":     "Units/Day",
                "revenue":      "Revenue (₹)",
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No movement data available.")

    st.markdown("---")

    # ── Dead stock ────────────────────────────────────────────────────
    st.subheader("💀 Dead Stock (No Movement 60+ Days)")
    if dead is not None and not dead.empty:
        st.warning(f"⚠️ {len(dead)} SKUs with no movement in 60+ days")
        st.dataframe(
            dead.rename(columns={
                "product_name":   "Product",
                "brand":          "Brand",
                "current_stock":  "Stock",
                "days_no_sales":  "Days Silent",
                "stock_value":    "Stock Value (₹)",
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("✅ No dead stock detected.")

    st.markdown("---")

    # ── Seasonal trend ────────────────────────────────────────────────
    st.subheader("📅 Monthly Trend by Group")
    if seasonal is not None and not seasonal.empty:
        pivot = seasonal.pivot_table(
            index="month", columns="main_group",
            values="units_sold", aggfunc="sum", fill_value=0
        )
        st.line_chart(pivot)
    else:
        st.info("No seasonal data available.")


def _load_velocity_data(period_days: int):
    """Returns (fast_movers_df, dead_stock_df, seasonal_df) or (None, None, None)."""
    try:
        from modules.sql_adapter import run_query

        # Fast movers
        fast_rows = run_query("""
            SELECT
                p.product_name,
                p.brand,
                p.main_group,
                SUM(oi.quantity)::int                              AS units_sold,
                (SUM(oi.quantity)::float / %(days)s)::float       AS velocity,
                SUM(oi.quantity * oi.unit_price)::float           AS revenue
            FROM order_lines oi
            JOIN products p ON p.id = oi.product_id
            JOIN orders o ON o.id = oi.order_id
            WHERE o.created_at >= NOW() - INTERVAL '%(days)s days'
              AND o.status NOT IN ('CANCELLED')
            GROUP BY p.id, p.product_name, p.brand, p.main_group
            ORDER BY units_sold DESC
            LIMIT 50
        """.replace("'%(days)s days'", f"'{period_days} days'"), {"days": period_days})

        fast = pd.DataFrame(fast_rows) if fast_rows else pd.DataFrame()
        if not fast.empty:
            fast["velocity"] = fast["velocity"].round(2)
            fast["revenue"]  = fast["revenue"].round(0).astype(int)

        # Dead stock
        dead_rows = run_query("""
            SELECT
                p.product_name,
                p.brand,
                COALESCE(inv.current_stock, 0)::int               AS current_stock,
                EXTRACT(DAY FROM NOW() - COALESCE(
                    (SELECT MAX(o.created_at)
                     FROM order_lines oi2
                     JOIN orders o ON o.id = oi2.order_id
                     WHERE oi2.product_id = p.id
                       AND o.status NOT IN ('CANCELLED')
                    ), NOW() - INTERVAL '999 days'
                ))::int                                            AS days_no_sales,
                COALESCE(inv.stock_value, 0)::float AS stock_value
            FROM products p
            LEFT JOIN (
                SELECT
                    product_id,
                    SUM(quantity)::int AS current_stock,
                    SUM(quantity * COALESCE(purchase_rate, purchase_price, 0)) AS stock_value
                FROM inventory_stock
                WHERE COALESCE(is_active, TRUE) = TRUE
                GROUP BY product_id
            ) inv ON inv.product_id = p.id
            WHERE COALESCE(inv.current_stock, 0) > 0
            GROUP BY p.id, p.product_name, p.brand, inv.current_stock, inv.stock_value
            HAVING EXTRACT(DAY FROM NOW() - COALESCE(
                (SELECT MAX(o.created_at)
                 FROM order_lines oi3
                 JOIN orders o ON o.id = oi3.order_id
                 WHERE oi3.product_id = p.id
                   AND o.status NOT IN ('CANCELLED')
                ), NOW() - INTERVAL '999 days'
            )) >= 60
            ORDER BY days_no_sales DESC
            LIMIT 100
        """)
        dead = pd.DataFrame(dead_rows) if dead_rows else pd.DataFrame()

        # Seasonal
        seasonal_rows = run_query("""
            SELECT
                TO_CHAR(o.created_at, 'YYYY-MM') AS month,
                p.main_group,
                SUM(oi.quantity)::int             AS units_sold
            FROM order_lines oi
            JOIN products p ON p.id = oi.product_id
            JOIN orders o ON o.id = oi.order_id
            WHERE o.created_at >= NOW() - INTERVAL '12 months'
              AND o.status NOT IN ('CANCELLED')
            GROUP BY month, p.main_group
            ORDER BY month
        """)
        seasonal = pd.DataFrame(seasonal_rows) if seasonal_rows else pd.DataFrame()

        return fast, dead, seasonal

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[Velocity] Load failed: {e}")
        return None, None, None
