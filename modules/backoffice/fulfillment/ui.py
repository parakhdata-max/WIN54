"""
fulfillment/ui.py
=================
Fulfillment UI Panel — Streamlit rendering only.

ALL decision logic lives in decision_engine.py.
This file only renders what the engine tells it.

FUNCTIONS
---------
  render_fulfillment_panel(ctx)         → header + supplier section + assignment
  render_fulfillment_header(ctx)        → mode banner with route counts
  render_supplier_order_panel(ctx)      → supplier creation section
  render_assignment_panel_block(ctx)    → delegates to assignment_panel.py
  check_assignment_guard(ctx)           → shows warning, returns bool

RULE (Issue 1 — ctx everywhere)
---------------------------------
  Every public function takes ctx: BackofficeContext as its FIRST argument.
  Extract order = ctx.order, session = ctx.session inside.
  Never accept raw (order, st) parameters.
"""

import streamlit as st
from typing import Dict, List

from ..kernel import BackofficeContext
from .decision_engine import (
    compute_fulfillment_mode,
    compute_route_counts,
    get_vendor_lines,
    get_external_lab_lines,
    get_pending_qty,
    build_route_diagnostics,
    is_assignment_confirmed,
    ROUTE_VENDOR, ROUTE_EXTERNAL_LAB,
)

# ── Assignment panel — delegate rendering only ───────────────────────
try:
    from ..assignment_panel import render_assignment_panel, init_assignment_state
    _ASSIGNMENT_PANEL_AVAILABLE = True
except ImportError:
    _ASSIGNMENT_PANEL_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════
# TOP-LEVEL ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

def render_fulfillment_panel(ctx: BackofficeContext) -> None:
    """
    Master fulfillment block — single call per order detail render.

    Renders:
      1. Fulfillment mode header (banner)
      2. Supplier order section
      3. Assignment panel (gate before save)
    """
    render_fulfillment_header(ctx)
    render_supplier_order_panel(ctx)
    render_assignment_panel_block(ctx)


# ═══════════════════════════════════════════════════════════════════════
# 1. FULFILLMENT HEADER
# ═══════════════════════════════════════════════════════════════════════

def render_fulfillment_header(ctx: BackofficeContext) -> None:
    """
    Fulfillment Mode banner — calls decision_engine, then renders result.

    Examples:
      📦 Fulfillment Mode: STOCK ONLY
      🏭 Fulfillment Mode: SUPPLIER
      🔀 Fulfillment Mode: MIXED — STOCK + VENDOR
    """
    all_lines = ctx.all_lines
    if not all_lines:
        return

    icon, label, color = compute_fulfillment_mode(all_lines)
    counts = compute_route_counts(all_lines)

    if not counts:
        return

    col_mode, col_counts = st.columns([3, 2])

    with col_mode:
        summary = f"{icon} **Fulfillment Mode:** {label}"
        if color == "warning":
            st.warning(summary)
        else:
            st.info(summary)

    with col_counts:
        _render_route_count_chips(counts)


def _render_route_count_chips(counts: Dict[str, int]) -> None:
    """Small inline count badges derived from decision_engine output."""
    icons = {
        "STOCK":        "📦",
        "VENDOR":       "🏭",
        "INHOUSE":      "🔧",
        "EXTERNAL_LAB": "🔬",
    }
    labels = {
        "STOCK":        "stock",
        "VENDOR":       "vendor",
        "INHOUSE":      "in-house",
        "EXTERNAL_LAB": "ext. lab",
    }
    parts = [
        f"{icons.get(r, '?')} {c} {labels.get(r, r)}"
        for r, c in counts.items()
        if c > 0
    ]
    if parts:
        st.caption("  ·  ".join(parts))


# ═══════════════════════════════════════════════════════════════════════
# 2. SUPPLIER ORDER PANEL
# ═══════════════════════════════════════════════════════════════════════

def render_supplier_order_panel(ctx: BackofficeContext) -> None:
    """
    Supplier procurement section — calls decision_engine, renders result.
    Shows create button when vendor lines exist, diagnostics otherwise.
    """
    from modules.supplier_orders_management import create_supplier_order_from_lines

    order     = ctx.order
    all_lines = ctx.all_lines

    vendor_lines   = get_vendor_lines(all_lines)       # VENDOR route only
    ext_lab_lines  = get_external_lab_lines(all_lines)  # EXTERNAL_LAB route only

    st.markdown("---")
    st.markdown("### 🚚 Supplier Orders")

    if ext_lab_lines:
        st.info(
            f"🔬 **{len(ext_lab_lines)} line(s) routed to External Lab** — "
            "manage them in the **Supplier Orders → External Lab** tab."
        )

    if vendor_lines:
        _render_vendor_lines_ready(ctx, vendor_lines, order)
    elif not ext_lab_lines:
        _render_supplier_diagnostics(ctx, all_lines)

    st.markdown("---")


def _render_vendor_lines_ready(
    ctx: BackofficeContext,
    vendor_lines: List[Dict],
    order: Dict,
) -> None:
    """Render only the Create Supplier Order button — banner/expander removed."""
    from modules.supplier_orders_management import create_supplier_order_from_lines

    if st.button(
        f"📦 Create Supplier Order ({len(vendor_lines)} items)",
        type="primary",
        use_container_width=True,
        key="create_supplier_order_main_btn",
    ):
        ctx.record("supplier_order_created", {"count": len(vendor_lines)})
        create_supplier_order_from_lines(order, vendor_lines)


def _render_supplier_diagnostics(ctx: BackofficeContext, all_lines: List[Dict]) -> None:
    """
    Renders diagnostic info when no vendor lines found.
    Data comes from decision_engine.build_route_diagnostics().
    """
    diag = build_route_diagnostics(all_lines)

    if diag["total_lines"] == 0:
        st.warning("⚠️ No order lines found in this order")
        return

    st.info(f"📋 **Order has {diag['total_lines']} line items:**")

    route_display = {
        "VENDOR":       ("success", "🏭"),
        "EXTERNAL_LAB": ("success", "🔬"),
        "STOCK":        ("info",    "📦"),
        "INHOUSE":      ("info",    "🔧"),
        "LAB_ORDER":    ("info",    "🧪"),
    }

    for route, count in diag["routes"].items():
        level, icon = route_display.get(route, ("warning", "❓"))
        getattr(st, level)(f"{icon} {count} items — route: {route}")

    if diag["already_ordered"]:
        st.success(f"✅ {diag['already_ordered']} items already have supplier orders")

    if diag["no_vendor_routes"]:
        st.info(
            "ℹ️ **No vendor items in this order.**\n\n"
            "Items route to vendors when they can't be fulfilled from stock or in-house. "
            "Check manufacturing route settings."
        )
    elif diag["all_vendor_ordered"]:
        st.success("✅ All vendor items already have supplier orders created")


# ═══════════════════════════════════════════════════════════════════════
# 3. ASSIGNMENT PANEL BLOCK
# ═══════════════════════════════════════════════════════════════════════

def render_assignment_panel_block(ctx: BackofficeContext) -> None:
    """
    Renders the assignment panel — the gate before Save.
    Delegates to assignment_panel.py for actual rendering.
    """
    order     = ctx.order
    all_lines = ctx.all_lines

    if _ASSIGNMENT_PANEL_AVAILABLE:
        render_assignment_panel(order, all_lines)
    else:
        st.warning(
            "⚠️ Assignment panel not available. "
            "Ensure assignment_panel.py is in modules/backoffice/."
        )


# ═══════════════════════════════════════════════════════════════════════
# ASSIGNMENT GUARD  (used by save button in shell)
# ═══════════════════════════════════════════════════════════════════════

def check_assignment_guard(ctx: BackofficeContext) -> bool:
    """
    Returns True if assignments are confirmed.
    Shows st.warning and returns False if not.

    Usage in save button:
        if not check_assignment_guard(ctx):
            return
    """
    if not _ASSIGNMENT_PANEL_AVAILABLE:
        return True

    if not is_assignment_confirmed(ctx.session):
        st.warning(
            "⚠️ Supplier / Job assignments not confirmed. "
            "Scroll up and click **Confirm All Assignments** before saving."
        )
        return False

    return True
