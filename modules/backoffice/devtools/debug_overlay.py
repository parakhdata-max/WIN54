"""
devtools/debug_overlay.py
==========================
Debug Overlay — ctx-first, system flag gated.

FEATURE FLAG (Issue minor 3)
------------------------------
  Debug overlay only renders when BOTH:
    1. SYSTEM_FLAGS["debug_mode"] = True   ← set in app config, NOT in code
    2. session.debug_pricing = True        ← operator toggle in sidebar

  In production: set_flag("debug_mode", False) — overlay is invisible.
  In staging:    set_flag("debug_mode", True)  — overlay can be toggled.

  Usage in app.py or config loader:
      from modules.backoffice.kernel import set_flag
      set_flag("debug_mode", os.getenv("DVERP_DEBUG", "false").lower() == "true")

RULE (Issue 1 — ctx everywhere)
---------------------------------
  All public functions accept ctx: BackofficeContext as first argument.
"""

import streamlit as st
from typing import Dict, List

from ..kernel import BackofficeContext


# ═══════════════════════════════════════════════════════════════════════
# MAIN OVERLAY — ctx-first
# ═══════════════════════════════════════════════════════════════════════

def render_debug_overlay(ctx: BackofficeContext) -> None:
    """
    Full pricing debug panel for all lines.

    Only renders when:
      - ctx.flags["debug_mode"] is True  (system flag)
      - ctx.session.debug_pricing is True (operator toggle)

    RENAMED signature: (order, all_lines) → (ctx)
    """
    order     = ctx.order
    all_lines = ctx.all_lines

    st.markdown("---")
    st.markdown("#### 🔍 Pricing Debug — Line Item Details")
    st.caption("🛠️ Debug mode active — disable in sidebar or set debug_mode=False to hide in prod")

    for idx, line in enumerate(all_lines, 1):
        with st.expander(f"Line {idx}: {line.get('product_name', 'N/A')}", expanded=False):
            billing_qty = int(line.get("billing_qty", 0) or 0)
            allocated   = int(line.get("allocated_qty", 0) or 0)

            debug_info = {
                "Product ID":           line.get("product_id", "N/A"),
                "Price Source":         line.get("price_source", "unknown"),
                "Eye Side":             line.get("eye_side", "N/A"),
                "Billing Qty":          billing_qty,
                "Allocated Qty":        allocated,
                "Pending Qty":          max(0, billing_qty - allocated),
                "Unit Price":           line.get("unit_price", 0),
                "Discount %":           line.get("discount_percent", 0),
                "Billing Total":        line.get("billing_total", 0),
                "Manufacturing Route":  line.get("manufacturing_route", "N/A"),
                "Batch Allocation":     line.get("batch_allocation", []),
                "Pricing Locked":       line.get("pricing_locked", False),
                "GST %":                line.get("gst_percent_used") or line.get("gst_percent", 0),
                "GST Amount":           line.get("gst_amount", 0),
                "Tax Inclusive":        line.get("tax_inclusive", True),
            }
            st.json(debug_info, expanded=False)

    with st.expander("📋 Order-level debug", expanded=False):
        st.json({
            "order_no":     order.get("order_no"),
            "order_type":   order.get("order_type"),
            "order_source": order.get("order_source"),
            "status":       order.get("status"),
            "tax_amount":   order.get("tax_amount"),
            "final_value":  order.get("final_value"),
            "ctx_elapsed":  f"{ctx.elapsed_ms}ms",
            "line_counts": {
                "stock":   len(order.get("stock_lines", [])),
                "inhouse": len(order.get("inhouse_lines", [])),
                "lab":     len(order.get("lab_order_lines", [])),
            }
        }, expanded=False)


def render_debug_overlay_safe(ctx: BackofficeContext) -> None:
    """
    Safe wrapper — only renders when debug_mode flag AND session toggle are active.
    Use this everywhere in the shell. Never render raw debug_overlay without this gate.
    """
    if not ctx.is_debug_pricing:
        return
    try:
        render_debug_overlay(ctx)
    except Exception as _err:
        st.caption(f"Debug overlay error: {_err}")


# ═══════════════════════════════════════════════════════════════════════
# BILLING TAB DEBUG TOGGLE — ctx-first
# ═══════════════════════════════════════════════════════════════════════

def render_billing_debug_toggle(ctx: BackofficeContext) -> None:
    """
    Inline debug toggle for the Billing tab.
    Only shows the checkbox when debug_mode system flag is True.

    RENAMED signature: (order, all_lines, order_id) → (ctx)
    """
    # Only expose toggle in debug-mode environments
    if not ctx.flags.get("debug_mode", False):
        return

    order     = ctx.order
    all_lines = ctx.all_lines
    order_id  = ctx.order_id

    st.markdown("---")
    debug_on = st.checkbox(
        "🔍 Debug Pricing (Advanced)",
        key=f"debug_pricing_{order_id}",
    )
    # Sync back to session so ctx.is_debug_pricing picks it up next render
    ctx.session["debug_pricing"] = debug_on

    if debug_on:
        render_debug_overlay(ctx)
