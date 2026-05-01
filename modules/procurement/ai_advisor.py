"""
procurement/ai_advisor.py
==========================
AI Advisory Layer — Priority 8 (Optional, Now Architecture-Safe).

WHAT THIS DOES
--------------
  Provides three AI-powered signals that overlay on top of the
  existing advisory and fulfillment layers:

  1. Supplier Recommendation AI
     Given a product + order context, recommends the best supplier
     using a weighted score from supplier_intelligence + LLM reasoning.

  2. Smart Reorder Prediction
     Uses 90-day velocity + seasonality to predict when each advisory
     product will need restocking BEFORE it hits the alert threshold.

  3. Seasonal Stocking Suggestions
     Generates a plain-language summary of what to stock up on before
     known demand peaks (e.g., school season, wedding season in India).

ARCHITECTURE
------------
  This layer is READ-ONLY. It never writes to the DB.
  It only produces suggestions → human operator confirms → system acts.

  advisory_panel.py  →  ai_advisor.get_supplier_recommendation(...)
  advisory_panel.py  →  ai_advisor.get_reorder_predictions(ctx)
  advisory_panel.py  →  ai_advisor.get_seasonal_suggestions(ctx)

WHEN TO ENABLE
--------------
  kernel.set_flag("enable_ai_advisor", True)

  Only enable after advisory is stable and has 60+ days of data.

PUBLIC API
----------
  get_supplier_recommendation(product_id, qty, ctx) → AIRecommendation
  get_reorder_predictions(ctx)                       → list[ReorderPrediction]
  get_seasonal_suggestions(ctx)                      → list[str]
  render_ai_advisor_panel(ctx)                       → Streamlit panel
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_AI_FLAG = "enable_ai_advisor"


# ═══════════════════════════════════════════════════════════════════════
# DATA OBJECTS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AIRecommendation:
    supplier_id:   str
    supplier_name: str
    score:         int
    confidence:    str          # "high" | "medium" | "low"
    reasoning:     str          # plain-language explanation
    alternatives:  List[Dict] = field(default_factory=list)


@dataclass
class ReorderPrediction:
    product_id:           str
    product_name:         str
    days_until_stockout:  int
    predicted_reorder_date: str   # ISO date string
    confidence:           str
    basis:                str     # explanation of prediction


# ═══════════════════════════════════════════════════════════════════════
# STREAMLIT PANEL
# ═══════════════════════════════════════════════════════════════════════

def render_ai_advisor_panel(ctx) -> None:
    """
    AI Advisor tab — renders only when enable_ai_advisor flag is True.
    Add as a plugin or advisory sub-tab.
    """
    import streamlit as st

    if not ctx.flags.get(_AI_FLAG, False):
        st.info(
            "🤖 AI Advisor is disabled. "
            "Enable it in config: `set_flag('enable_ai_advisor', True)` "
            "once you have 60+ days of advisory data."
        )
        return

    st.subheader("🤖 AI Advisor")
    st.caption("Powered by velocity data + supplier intelligence")

    tabs = st.tabs([
        "📦 Reorder Predictions",
        "🏭 Supplier Recommendations",
        "🌦️ Seasonal Stocking",
    ])

    with tabs[0]:
        _render_reorder_predictions(ctx)

    with tabs[1]:
        _render_supplier_recommendation_ui(ctx)

    with tabs[2]:
        _render_seasonal_suggestions(ctx)


# ═══════════════════════════════════════════════════════════════════════
# 1. REORDER PREDICTIONS
# ═══════════════════════════════════════════════════════════════════════

def get_reorder_predictions(ctx) -> List[ReorderPrediction]:
    """
    Predict reorder dates for advisory products before they hit alert threshold.
    Uses current velocity to project stockout date + adds lead time buffer.
    """
    from modules.procurement.advisory.advisory_service import (
        load_advisory_inventory,
        ADVISORY_GROUPS,
    )
    import datetime

    inventory = load_advisory_inventory(list(ADVISORY_GROUPS.keys()))
    if inventory is None or inventory.empty:
        return []

    predictions = []
    for _, row in inventory.iterrows():
        curr_stock = float(row.get("current_stock", 0))
        velocity   = max(float(row.get("velocity_per_day", 1.0)), 0.01)
        min_stock  = float(row.get("min_stock", 10))

        # Stockout = when current stock hits min_stock
        days_until_min = max(0, (curr_stock - min_stock) / velocity)
        # Reorder date = stockout date minus 7-day lead time buffer
        reorder_in_days = max(0, int(days_until_min) - 7)
        reorder_date    = (
            datetime.date.today() + datetime.timedelta(days=reorder_in_days)
        ).isoformat()

        confidence = (
            "high"   if float(row.get("velocity_per_day", 0)) > 0.5 else
            "medium" if float(row.get("velocity_per_day", 0)) > 0.1 else
            "low"
        )

        predictions.append(ReorderPrediction(
            product_id           = str(row.get("product_id", "")),
            product_name         = str(row.get("product_name", "N/A")),
            days_until_stockout  = int(days_until_min),
            predicted_reorder_date = reorder_date,
            confidence           = confidence,
            basis                = (
                f"Velocity {velocity:.2f} units/day · "
                f"Current stock {int(curr_stock)} · "
                f"Min stock {int(min_stock)}"
            ),
        ))

    predictions.sort(key=lambda p: p.days_until_stockout)
    return predictions


def _render_reorder_predictions(ctx) -> None:
    import streamlit as st
    import pandas as pd

    st.markdown("#### 🔮 Predicted Reorder Dates")
    st.caption("Based on current velocity + 7-day lead time buffer")

    predictions = get_reorder_predictions(ctx)

    if not predictions:
        st.info("No advisory inventory data available for predictions.")
        return

    rows = [
        {
            "Product":         p.product_name,
            "Days to Reorder": p.days_until_stockout,
            "Reorder By":      p.predicted_reorder_date,
            "Confidence":      p.confidence.title(),
            "Basis":           p.basis,
        }
        for p in predictions
    ]
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════════
# 2. SUPPLIER RECOMMENDATION
# ═══════════════════════════════════════════════════════════════════════

def get_supplier_recommendation(
    product_id:    str,
    qty:           int,
    ctx,
) -> Optional[AIRecommendation]:
    """
    Recommend the best supplier for a product + qty combo.
    Uses supplier_intelligence scores as primary signal.
    """
    from modules.procurement.supplier_intelligence import (
        get_ranked_suppliers_for_assignment,
    )

    ranked = get_ranked_suppliers_for_assignment(product_id)
    if not ranked:
        return None

    best        = ranked[0]
    alternatives = ranked[1:4]

    # Build confidence from score
    score = int(best.get("score", 0))
    confidence = "high" if score >= 85 else "medium" if score >= 70 else "low"

    # Build plain-language reasoning
    delivery_d = best.get("delivery_days_avg", "N/A")
    rejection  = best.get("rejection_pct", 0)
    past_pos   = best.get("past_orders_for_product", 0)

    reasoning_parts = [
        f"Highest reliability score ({score}/100)",
        f"Avg delivery: {delivery_d:.1f} days" if isinstance(delivery_d, float) else "",
        f"Rejection rate: {rejection:.1f}%" if rejection else "No rejections on record",
        f"{past_pos} previous orders for this product" if past_pos else "",
    ]
    reasoning = " · ".join(p for p in reasoning_parts if p)

    return AIRecommendation(
        supplier_id   = str(best.get("id", "")),
        supplier_name = str(best.get("name", "N/A")),
        score         = score,
        confidence    = confidence,
        reasoning     = reasoning,
        alternatives  = [{"name": a["name"], "score": a["score"]} for a in alternatives],
    )


def _render_supplier_recommendation_ui(ctx) -> None:
    import streamlit as st

    st.markdown("#### 🏭 Supplier Recommendation")
    st.caption("Enter a product to get AI-ranked supplier suggestions")

    product_id = st.text_input("Product ID", key="ai_sup_pid",
                               placeholder="e.g. 1042")
    qty        = st.number_input("Quantity", min_value=1, value=10,
                                 key="ai_sup_qty")

    if st.button("🤖 Get Recommendation", key="ai_sup_btn") and product_id:
        rec = get_supplier_recommendation(product_id, qty, ctx)
        if rec:
            st.success(
                f"**Recommended: {rec.supplier_name}** "
                f"(Score: {rec.score}/100 · Confidence: {rec.confidence})"
            )
            st.info(f"📋 Reasoning: {rec.reasoning}")
            if rec.alternatives:
                st.caption("Alternatives: " + ", ".join(
                    f"{a['name']} ({a['score']})" for a in rec.alternatives
                ))
        else:
            st.warning("No supplier data found for this product.")


# ═══════════════════════════════════════════════════════════════════════
# 3. SEASONAL STOCKING SUGGESTIONS
# ═══════════════════════════════════════════════════════════════════════

def get_seasonal_suggestions(ctx) -> List[str]:
    """
    Generate plain-language stocking suggestions based on historical
    seasonal demand patterns.

    Returns a list of suggestion strings.
    """
    import datetime

    month = datetime.date.today().month
    suggestions = _SEASONAL_SUGGESTIONS.get(month, [])
    return suggestions


# Indian optical market seasonal patterns
_SEASONAL_SUGGESTIONS = {
    1:  ["📚 School season approaching — stock up on children's frames",
         "❄️ Winter — contact lens solution demand typically rises"],
    2:  ["💑 Wedding season — premium frame brands, sunglasses demand high",
         "🎓 Board exams — student frame repairs spike"],
    3:  ["🌸 March–April: stock sunglasses ahead of summer",
         "📦 Review blanks inventory before Q1 close"],
    4:  ["☀️ Summer peak — anti-reflective and UV-protection lenses",
         "🕶️ Sunglass frames: reorder premium brands early"],
    5:  ["🏫 Pre-school season: children's frames + impact lenses",
         "🧴 Contact lens solutions peak — check stock levels"],
    6:  ["📚 School admissions: children's frames, budget range"],
    7:  ["🌧️ Monsoon: photochromic lens demand rises",
         "📦 Mid-year advisory restock recommended"],
    8:  ["🏫 School fully open — children's frame accessories",
         "💊 Contact lens hygiene products — solution peak"],
    9:  ["🎃 Festival season approaching — premium gifting",
         "💍 Wedding season starts — designer frames"],
    10: ["🪔 Diwali — gifting sunglasses + premium frames high ROI",
         "📦 Pre-festival inventory build: 2–3 weeks lead time needed"],
    11: ["❄️ Winter: anti-fog coatings, photochromic lenses",
         "🎓 Year-end school check-ups drive prescription lens demand"],
    12: ["🎁 Year-end gifting — premium sunglasses, designer frames",
         "📊 Annual stock review — identify dead stock before FY close"],
}


def _render_seasonal_suggestions(ctx) -> None:
    import streamlit as st
    import datetime

    st.markdown("#### 🌦️ Seasonal Stocking Suggestions")

    month_name = datetime.date.today().strftime("%B")
    st.caption(f"Suggestions for {month_name} based on Indian optical market patterns")

    suggestions = get_seasonal_suggestions(ctx)

    if suggestions:
        for s in suggestions:
            st.info(s)
    else:
        st.success("✅ No specific seasonal actions this month.")
