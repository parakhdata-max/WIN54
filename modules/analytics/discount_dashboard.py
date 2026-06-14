"""
modules/analytics/discount_dashboard.py
=========================================
Phase 3A — Standalone Discount Analytics Dashboard.
Plugs into app.py sidebar as a full page.
Can also be called from Admin UI → Analytics tab.

Metrics:
  1. Revenue vs Discount Leakage (ratio + trend)
  2. Rule Effectiveness (usage, discount given, revenue per rule)
  3. Margin Risk Detector (products with repeated hard-stops)
  4. Channel Comparison (wholesale vs retail)
  5. Daily Trend (line chart)
  6. High Discount Alerts (>20%)
  7. Auto-rule Suggestion (3E — qty patterns → slab suggestions)
"""
import streamlit as st


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        st.error(f"DB error: {e}")
        return []


def render_discount_dashboard():
    st.title("📊 Discount Analytics Dashboard")

    # ── 3F: Show alert badge from admin_ui alert engine ──────────────────────
    try:
        from modules.ui.pricing_admin.admin_ui import _render_alert_badge
        _render_alert_badge()
    except Exception:
        pass

    period = st.selectbox(
        "Period", [7, 30, 60, 90, 180],
        format_func=lambda x: f"Last {x} days", index=1, key="dd_period"
    )

    # ── 1. KPI row ─────────────────────────────────────────────────────────
    kpi = _q("""
        SELECT
            COALESCE(SUM(ol.billing_total), 0)          AS revenue,
            COALESCE(SUM(ol.discount_amount), 0)         AS discount,
            COALESCE(SUM(ol.unit_price * ol.quantity),0) AS gross,
            ROUND(
                SUM(ol.discount_amount) * 100.0
                / NULLIF(SUM(ol.billing_total), 0), 2
            )                                            AS discount_ratio,
            COUNT(DISTINCT o.id)                         AS orders,
            COALESCE(AVG(NULLIF(ol.discount_percent,0)),0) AS avg_disc_pct
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
    """, {"period": period})

    if kpi:
        k = kpi[0]
        rev        = float(k.get("revenue") or 0)
        disc       = float(k.get("discount") or 0)
        ratio      = float(k.get("discount_ratio") or 0)
        avg_pct    = float(k.get("avg_disc_pct") or 0)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Orders",          int(k.get("orders") or 0))
        c2.metric("💰 Net Revenue",  f"₹{rev:,.0f}")
        c3.metric("🏷️ Discount Given", f"₹{disc:,.0f}",
                  delta=f"{ratio:.1f}% of revenue", delta_color="inverse")
        c4.metric("📉 Discount Ratio", f"{ratio:.1f}%")
        c5.metric("Avg Disc %",       f"{avg_pct:.1f}%")

    st.markdown("---")

    # ── 2. Rule Effectiveness ───────────────────────────────────────────────
    st.subheader("🔥 Rule Effectiveness (Top 10)")

    # Query 2A: group by discount_rule label (good for stacked combos display)
    rules = _q("""
        SELECT
            COALESCE(ol.discount_rule, '— No Discount') AS rule_name,
            COUNT(*)                                      AS usage_count,
            COALESCE(SUM(ol.discount_amount), 0)          AS discount_given,
            COALESCE(SUM(ol.billing_total), 0)            AS revenue,
            ROUND(
                SUM(ol.discount_amount)*100.0
                / NULLIF(SUM(ol.billing_total),0), 2
            )                                             AS disc_ratio_pct
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        GROUP BY ol.discount_rule
        ORDER BY discount_given DESC
        LIMIT 10
    """, {"period": period})

    if rules:
        import pandas as pd
        df = pd.DataFrame(rules)
        df["discount_given"] = df["discount_given"].apply(lambda x: f"₹{float(x):,.0f}")
        df["revenue"]        = df["revenue"].apply(lambda x: f"₹{float(x):,.0f}")
        df["disc_ratio_pct"] = df["disc_ratio_pct"].apply(lambda x: f"{float(x or 0):.1f}%")
        st.dataframe(df.rename(columns={
            "rule_name": "Rule", "usage_count": "Lines Used",
            "discount_given": "Discount Given", "revenue": "Net Revenue",
            "disc_ratio_pct": "Disc %"
        }), use_container_width=True, hide_index=True)
    else:
        st.info("No rule data for this period.")

    # Query 2B: per individual rule UUID via unnest (accurate for stacked combos)
    # "Party 10% + Slab 5%" stored as "uuid1,uuid2" → split and join to rule names
    with st.expander("🔬 Individual Rule Analytics (unnested per rule ID)"):
        st.caption(
            "Breaks stacked combos apart — each rule counted individually. "
            "Accurate for understanding which single rule drives the most discount."
        )
        rule_ids = _q("""
            SELECT
                dr.name                                        AS rule_name,
                dr.type                                        AS rule_type,
                COUNT(*)                                       AS usage_count,
                COALESCE(
                    SUM(ol.discount_amount / NULLIF(
                        array_length(
                            string_to_array(NULLIF(ol.applied_rule_ids,''), ','), 1
                        ), 0
                    )), 0
                )                                              AS approx_discount
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            JOIN discount_rules dr
              ON dr.id::text = ANY(
                    string_to_array(NULLIF(ol.applied_rule_ids, ''), ',')
                 )
            WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND ol.applied_rule_ids IS NOT NULL
              AND ol.applied_rule_ids != ''
            GROUP BY dr.name, dr.type
            ORDER BY usage_count DESC
            LIMIT 15
        """, {"period": period})

        if rule_ids:
            import pandas as pd
            dfr = pd.DataFrame(rule_ids)
            dfr["approx_discount"] = dfr["approx_discount"].apply(
                lambda x: f"₹{float(x):,.0f}")
            st.dataframe(dfr.rename(columns={
                "rule_name": "Rule", "rule_type": "Type",
                "usage_count": "Times Applied", "approx_discount": "Approx Discount"
            }), use_container_width=True, hide_index=True)
        else:
            st.info(
                "No rule ID data yet. Ensure applied_rule_ids column is populated "
                "(Phase 2A migration required)."
            )

    st.markdown("---")

    # ── 3 + 4. Channel comparison + Margin risk ─────────────────────────────
    col_l, col_r = st.columns(2)

    with col_l:
        st.subheader("📊 Channel Comparison")
        channel = _q("""
            SELECT
                o.order_type                             AS channel,
                COALESCE(SUM(ol.billing_total), 0)       AS revenue,
                COALESCE(SUM(ol.discount_amount), 0)     AS discount,
                ROUND(
                    SUM(ol.discount_amount)*100.0
                    / NULLIF(SUM(ol.billing_total),0), 2
                )                                        AS disc_pct
            FROM orders o
            JOIN order_lines ol ON o.id = ol.order_id
            WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
            GROUP BY o.order_type
            ORDER BY revenue DESC
        """, {"period": period})

        if channel:
            import pandas as pd
            dfc = pd.DataFrame(channel)
            dfc["revenue"]  = dfc["revenue"].apply(lambda x: f"₹{float(x):,.0f}")
            dfc["discount"] = dfc["discount"].apply(lambda x: f"₹{float(x):,.0f}")
            dfc["disc_pct"] = dfc["disc_pct"].apply(lambda x: f"{float(x or 0):.1f}%")
            st.dataframe(dfc.rename(columns={
                "channel": "Channel", "revenue": "Revenue",
                "discount": "Discount", "disc_pct": "Disc %"
            }), use_container_width=True, hide_index=True)

    with col_r:
        st.subheader("⚠️ Margin Risk (Top Loss Makers)")
        risk = _q("""
            SELECT
                COALESCE(p.product_name, 'Unknown') AS product,
                COUNT(*) AS occurrences,
                ROUND(AVG(ol.discount_percent),1)   AS avg_disc_pct,
                COALESCE(SUM(ol.discount_amount),0) AS total_discount
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
              AND ol.margin_status IN ('soft_warning', 'hard_stop')
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
            GROUP BY p.product_name
            ORDER BY total_discount DESC
            LIMIT 8
        """, {"period": period})

        if risk:
            import pandas as pd
            dfr = pd.DataFrame(risk)
            dfr["total_discount"] = dfr["total_discount"].apply(lambda x: f"₹{float(x):,.0f}")
            dfr["avg_disc_pct"]   = dfr["avg_disc_pct"].apply(lambda x: f"{float(x or 0):.1f}%")
            st.dataframe(dfr.rename(columns={
                "product": "Product", "occurrences": "Times",
                "avg_disc_pct": "Avg Disc", "total_discount": "Disc Given"
            }), use_container_width=True, hide_index=True)
        else:
            st.success("✅ No margin risk detected.")

    st.markdown("---")

    # ── 5. High Discount Alerts ─────────────────────────────────────────────
    st.subheader("🚨 High Discount Alerts (>20%)")
    alerts = _q("""
        SELECT
            COALESCE(p.product_name, 'Unknown') AS product,
            o.order_no,
            o.party_name,
            ol.discount_percent,
            ol.discount_amount,
            ol.billing_total
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
          AND COALESCE(ol.discount_percent, 0) > 20
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        ORDER BY ol.discount_percent DESC
        LIMIT 20
    """, {"period": period})

    if alerts:
        import pandas as pd
        dfa = pd.DataFrame(alerts)
        dfa["discount_percent"] = dfa["discount_percent"].apply(lambda x: f"{float(x):.1f}%")
        dfa["discount_amount"]  = dfa["discount_amount"].apply(lambda x: f"₹{float(x):,.0f}")
        dfa["billing_total"]    = dfa["billing_total"].apply(lambda x: f"₹{float(x):,.0f}")
        st.dataframe(dfa.rename(columns={
            "product": "Product", "order_no": "Order",
            "party_name": "Party", "discount_percent": "Disc %",
            "discount_amount": "Disc ₹", "billing_total": "Net"
        }), use_container_width=True, hide_index=True)
    else:
        st.success("✅ No high-discount orders in this period.")

    st.markdown("---")

    # ── 6. Daily Trend ──────────────────────────────────────────────────────
    st.subheader("📈 Daily Discount Trend")
    trend = _q("""
        SELECT
            DATE(o.created_at)                       AS day,
            COALESCE(SUM(ol.discount_amount), 0)     AS discount,
            COALESCE(SUM(ol.billing_total), 0)       AS revenue,
            ROUND(
                SUM(ol.discount_amount)*100.0
                / NULLIF(SUM(ol.billing_total),0), 2
            )                                        AS disc_pct
        FROM orders o
        JOIN order_lines ol ON o.id = ol.order_id
        WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        GROUP BY DATE(o.created_at)
        ORDER BY day ASC
    """, {"period": period})

    if trend:
        import pandas as pd
        dft = pd.DataFrame(trend)
        dft["day"] = pd.to_datetime(dft["day"])
        dft = dft.set_index("day")
        dft["discount"] = dft["discount"].astype(float)
        dft["disc_pct"] = dft["disc_pct"].astype(float)

        t1, t2 = st.columns(2)
        with t1:
            st.caption("₹ Discount given per day")
            st.line_chart(dft[["discount"]], use_container_width=True)
        with t2:
            st.caption("Discount % of revenue per day")
            st.line_chart(dft[["disc_pct"]], use_container_width=True)

    st.markdown("---")

    # ── 7. Phase 3E — Auto Rule Suggestion ─────────────────────────────────
    st.subheader("💡 Auto Rule Suggestions (3E)")
    st.caption("Patterns detected in order data → suggested discount rules")

    slab_hints = _q("""
        SELECT
            COALESCE(p.product_name, 'Unknown')  AS product,
            COALESCE(p.brand, '')                AS brand,
            ol.quantity                          AS qty,
            COUNT(*)                             AS frequency
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
          AND ol.quantity >= 3
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        GROUP BY p.product_name, p.brand, ol.quantity
        HAVING COUNT(*) >= 3
        ORDER BY frequency DESC
        LIMIT 8
    """, {"period": period})

    if slab_hints:
        st.markdown("**📦 Frequent Bulk Quantities — Consider Slab Rules:**")
        for r in slab_hints:
            qty  = int(r.get("qty") or 0)
            freq = int(r.get("frequency") or 0)
            prod = r.get("product", "")
            pct  = min(qty * 2, 20)   # suggested pct = qty × 2, capped at 20%
            st.info(
                f"💡 **{prod}** ordered in qty **{qty}** — {freq}× in this period.  \n"
                f"Suggested: Create slab rule → Buy {qty}+ → **{pct}% off**  \n"
                f"Go to ➕ Add Rule → Type: offer_slab → min_qty: {qty} → value: {pct}"
            )
    else:
        st.info("No bulk-order patterns detected yet.")
