"""
modules/analytics/price_suggestions.py
=========================================
Phase 3D — AI Price Suggestion Engine (10/10 Production Grade).

Multi-factor analysis per product:
  margin       = (billing_total - cost_price) / billing_total × 100
  velocity     = qty_sold / period_days
  disc_ratio   = discount_amount / billing_total × 100

Decision engine (in priority order):
  1. margin < 5  AND cost > 0   → 🚨 Loss risk — reduce discount immediately
  2. disc_ratio > 25            → ⚠️  Over-discounting
  3. margin > 40 AND vel < 5    → 📉  Boost sales — increase discount
  4. vel > 20   AND margin > 20 → 🔥  High performer — maintain pricing
  5. else                       → ✅  Healthy pricing

UI features:
  - Colour-coded rows (red/amber/green)
  - Filter dropdown by suggestion type
  - Sidebar alert badge for 🚨 signals
  - Period selector (30 / 60 / 90 days)
"""
import streamlit as st


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        st.error(f"DB error: {e}")
        return []


def _analyze_row(r: dict, period_days: int) -> dict:
    """
    Return suggestion dict with icon, label, colour, and detail.
    """
    billing      = float(r.get("billing_total")    or 0)
    discount     = float(r.get("discount_amount")  or 0)
    cost         = float(r.get("cost_price")        or 0)
    qty          = float(r.get("qty")               or 0)

    if billing <= 0:
        return {"icon": "⚠️", "label": "Invalid data",
                "colour": "orange", "priority": 9}

    # ── Metrics ──────────────────────────────────────────────────────────────
    # cost_price from SQL is MAX(purchase_rate) — one unit cost
    # billing_total and qty are aggregated sums over the period
    # Correct formula: total_cost = cost_per_unit * qty_sold
    # margin % = (total_revenue - total_cost) / total_revenue * 100
    total_cost   = cost * qty                          # cost × qty = total COGS
    margin       = ((billing - total_cost) / billing * 100) if (cost > 0 and billing > 0) else None
    disc_ratio   = (discount / billing * 100)          if billing > 0 else 0.0
    velocity     = qty / max(period_days, 1)           # units per day

    # ── Decision engine ───────────────────────────────────────────────────────
    if cost > 0 and margin is not None and margin < 5:
        return {
            "icon":     "🚨",
            "label":    "Loss risk — reduce discount immediately",
            "colour":   "red",
            "priority": 1,
            "detail":   f"Margin {margin:.1f}% (cost ₹{cost:,.0f}, "
                        f"revenue ₹{billing:,.0f}). "
                        f"Reduce or remove discount rule for this product.",
        }
    if disc_ratio > 25:
        return {
            "icon":     "⚠️",
            "label":    "Over-discounting",
            "colour":   "orange",
            "priority": 2,
            "detail":   f"Discount is {disc_ratio:.1f}% of revenue. "
                        f"Review rule — typical safe threshold is 15–20%.",
        }
    if margin is not None and margin > 40 and velocity < 5:
        return {
            "icon":     "📉",
            "label":    "Boost sales — consider increasing discount",
            "colour":   "blue",
            "priority": 3,
            "detail":   f"High margin ({margin:.1f}%) but low velocity "
                        f"({velocity:.2f} units/day). "
                        f"A targeted discount could push volume.",
        }
    if velocity > 20 and (margin is None or margin > 20):
        return {
            "icon":     "🔥",
            "label":    "High performer — maintain pricing",
            "colour":   "green",
            "priority": 4,
            "detail":   f"Selling {velocity:.1f} units/day with "
                        f"{'margin '+str(round(margin,1))+'%' if margin else 'no cost data'}. "
                        f"No action needed.",
        }
    return {
        "icon":     "✅",
        "label":    "Healthy pricing",
        "colour":   "green",
        "priority": 5,
        "detail":   f"Disc ratio {disc_ratio:.1f}%, "
                    f"velocity {velocity:.2f}/day. All good.",
    }


def render_price_suggestions():
    st.title("🤖 AI Price Suggestions")
    st.caption(
        "Multi-factor analysis: margin × velocity × discount ratio. "
        "**Read-only — nothing auto-applies.**"
    )

    col_p, col_f = st.columns([2, 3])
    period = col_p.selectbox(
        "Period",
        [30, 60, 90],
        format_func=lambda x: f"Last {x} days",
        index=1,
        key="ps_period",
    )
    filter_type = col_f.selectbox(
        "Filter by suggestion",
        ["All", "🚨 Loss Risk", "⚠️ Over-discounting",
         "📉 Boost Sales", "🔥 High Performer", "✅ Healthy"],
        key="ps_filter",
    )

    # ── Fetch product-level aggregated data ───────────────────────────────────
    data = _q("""
        SELECT
            COALESCE(p.product_name, 'Unknown')        AS product_name,
            ol.product_id,
            COALESCE(p.brand, '')                      AS brand,
            COALESCE(p.main_group, '')                 AS category,
            COALESCE(SUM(ol.billing_total), 0)         AS billing_total,
            COALESCE(SUM(ol.discount_amount), 0)       AS discount_amount,
            COALESCE(SUM(ol.billing_qty), 0)           AS qty,
            COALESCE(MAX(s.purchase_rate), 0)          AS cost_price
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        LEFT JOIN inventory_stock s
               ON s.product_id = ol.product_id
              AND s.purchase_rate IS NOT NULL
              AND s.purchase_rate > 0
        WHERE o.created_at >= NOW() - (%(period)s * INTERVAL '1 day')
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        GROUP BY p.product_name, ol.product_id, p.brand, p.main_group
        ORDER BY billing_total DESC
        LIMIT 80
    """, {"period": period})

    if not data:
        st.info(
            "No order data found for this period. "
            "Place some orders first, then re-open this tab."
        )
        return

    # ── Build result rows ─────────────────────────────────────────────────────
    results = []
    loss_risk_count = 0

    for r in data:
        s = _analyze_row(r, period)
        disc_ratio = (
            float(r.get("discount_amount") or 0)
            / float(r.get("billing_total") or 1) * 100
        )
        margin_val = None
        cost = float(r.get("cost_price") or 0)
        billing = float(r.get("billing_total") or 0)
        if cost > 0 and billing > 0:
            margin_val = round((billing - cost) / billing * 100, 1)

        results.append({
            "_icon":      s["icon"],
            "_label":     s["label"],
            "_colour":    s["colour"],
            "_priority":  s["priority"],
            "_detail":    s.get("detail", ""),
            "Product":    r.get("product_name", ""),
            "Brand":      r.get("brand", ""),
            "Category":   r.get("category", ""),
            "Revenue":    f"₹{float(r.get('billing_total') or 0):,.0f}",
            "Discount":   f"₹{float(r.get('discount_amount') or 0):,.0f}",
            "Disc %":     f"{disc_ratio:.1f}%",
            "Margin %":   f"{margin_val:.1f}%" if margin_val is not None else "—",
            "Qty Sold":   int(r.get("qty") or 0),
            "Suggestion": f"{s['icon']} {s['label']}",
        })

        if s["icon"] == "🚨":
            loss_risk_count += 1

    # ── Sidebar alert badge for 3F integration ────────────────────────────────
    if loss_risk_count > 0:
        try:
            st.sidebar.error(
                f"🚨 {loss_risk_count} product(s) at loss risk — "
                f"check AI Suggestions"
            )
        except Exception:
            pass

    # ── Apply filter ──────────────────────────────────────────────────────────
    filter_map = {
        "🚨 Loss Risk":       "🚨",
        "⚠️ Over-discounting": "⚠️",
        "📉 Boost Sales":     "📉",
        "🔥 High Performer":  "🔥",
        "✅ Healthy":         "✅",
    }
    if filter_type != "All":
        target_icon = filter_map.get(filter_type, "")
        results = [r for r in results if r["_icon"] == target_icon]

    # Sort by priority (🚨 first)
    results.sort(key=lambda x: x["_priority"])

    if not results:
        st.info(f"No products matching filter: {filter_type}")
        return

    # ── Summary badges ────────────────────────────────────────────────────────
    from collections import Counter
    counts = Counter(r["_icon"] for r in results)
    b_cols = st.columns(5)
    for i, (icon, label) in enumerate([
        ("🚨", "Loss Risk"), ("⚠️", "Over-Disc"),
        ("📉", "Boost"),     ("🔥", "Star"),    ("✅", "Healthy")
    ]):
        b_cols[i].metric(f"{icon} {label}", counts.get(icon, 0))

    st.markdown("---")

    # ── Colour-coded rows ─────────────────────────────────────────────────────
    _COLOUR_CSS = {
        "red":    "background-color:#3d1a1a;border-left:4px solid #ef4444",
        "orange": "background-color:#3d2a0a;border-left:4px solid #f97316",
        "blue":   "background-color:#0a1f3d;border-left:4px solid #3b82f6",
        "green":  "background-color:#0a2a1a;border-left:4px solid #22c55e",
    }

    for r in results:
        css    = _COLOUR_CSS.get(r["_colour"], "")
        detail = r.get("_detail", "")

        with st.container():
            st.markdown(
                f"<div style='{css};padding:10px 14px;border-radius:6px;"
                f"margin-bottom:6px'>"
                f"<b>{r['Suggestion']}</b> — {r['Product']}"
                f"<span style='color:#94a3b8;font-size:0.82rem'> "
                f"({r['Brand']} · {r['Category']})</span>"
                f"<br><span style='font-size:0.82rem;color:#cbd5e1'>"
                f"Revenue: {r['Revenue']} &nbsp;|&nbsp; "
                f"Discount: {r['Discount']} ({r['Disc %']}) &nbsp;|&nbsp; "
                f"Margin: {r['Margin %']} &nbsp;|&nbsp; "
                f"Qty: {r['Qty Sold']}</span>"
                f"<br><span style='font-size:0.78rem;color:#94a3b8'>"
                f"💡 {detail}</span>"
                f"</div>",
                unsafe_allow_html=True
            )

    st.markdown("---")
    st.info(
        "⚠️ All suggestions are informational only. "
        "No changes have been applied. "
        "Use **Pricing Admin → Active Rules** or **Party Discounts** to act."
    )
