"""
backoffice_shell.py
===================
Backoffice Shell — PURE ROUTER. No logic. No rendering.

ISSUE 2 RESOLVED
-----------------
  Shell now does exactly one thing: route ctx to the right panel.

  render_order_detail()
    → build ctx                          (kernel.py)
    → render_order_header(ctx)           (this file — 15 lines)
    → render_fulfillment_header(ctx)     (fulfillment/)
    → render_supplier_order_panel(ctx)   (fulfillment/)
    → tabs:
        tab1 → render_order_items_panel(ctx)     (order_items_panel.py)
        tab2 → render_lab_output_panel(ctx)      (lab_output_layer.py)
        tab3 → render_status_panel(ctx)          (order_items_panel.py)
        tab4 → render_billing_summary_tab(ctx)   (order_items_panel.py)
        tab5 → plugin.render(ctx)                (plugins/registry.py)
        tab6 → plugin.render(ctx)                (plugins/registry.py)

RULE: If you're adding rendering logic to this file, it belongs in a panel.
RULE: Every call is ctx-first. No raw (order, st) passed anywhere.

MIGRATION
----------
  In app.py, change:
    from modules.backoffice.backoffice_ui import render_order_detail
  To:
    from modules.backoffice.backoffice_shell import render_order_detail
"""

import logging
import streamlit as st
from typing import Optional

from .kernel import build_context, BackofficeContext
from .backoffice_helpers import get_display_order_id

# ── Panel imports — each owns its tab content ────────────────────────
from .order_items_panel import (
    render_order_items_panel,
    render_status_panel,
    render_billing_summary_tab,
    product_change_dialog,
)
from .lab_output_layer import render_lab_output_panel
from .fulfillment import (
    render_fulfillment_header,
    render_supplier_order_panel,
)
from .plugins.registry import get_active_plugins, discover_plugins

# ── Optional sidebar ─────────────────────────────────────────────────
try:
    from .backoffice_sidebar import render_backoffice_sidebar as _sidebar
except ImportError:
    _sidebar = None

# ── Assignment init ──────────────────────────────────────────────────
try:
    from .assignment_panel import init_assignment_state
    _HAS_ASSIGNMENT = True
except ImportError:
    _HAS_ASSIGNMENT = False

log = logging.getLogger(__name__)

# Run plugin auto-discovery once at import time
_plugins_discovered = False


def _ensure_plugins_discovered() -> None:
    global _plugins_discovered
    if not _plugins_discovered:
        discover_plugins()
        _plugins_discovered = True


# ═══════════════════════════════════════════════════════════════════════
# SESSION STATE INIT
# ═══════════════════════════════════════════════════════════════════════

def init_backoffice_state() -> None:
    """Initialise all bo_ session state keys. Safe to call on every render."""
    defaults = {
        "bo_view_mode":              "dashboard",
        "bo_selected_order_id":      None,
        "bo_active_orders":          [],
        "bo_orders_loaded":          False,
        "bo_editing_line":           None,
        "bo_show_allocation_window": False,
        "bo_allocation_line_idx":    None,
        "bo_product_change_modal":   {"active": False},
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

    if _HAS_ASSIGNMENT:
        init_assignment_state()


# ═══════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

def render_order_detail() -> None:
    """
    Pure router — resolves order, builds ctx, delegates to panels.
    Zero rendering logic in this function.
    """
    _ensure_plugins_discovered()

    # ── Resolve order_id ─────────────────────────────────────────────
    order_id = st.session_state.get("bo_selected_order_id")
    if not order_id:
        st.warning("No order selected.")
        if st.button("⬅️ Back to Dashboard", key="back_no_order"):
            st.session_state.bo_view_mode = "dashboard"
            st.rerun()
        return

    # ── Reset assignment state on order switch ────────────────────────
    _reset_assignment_state_if_switched(order_id)

    # ── Find order in session ─────────────────────────────────────────
    order = _find_order(order_id)
    if order is None:
        st.error("Order not found.")
        if st.button("⬅️ Back to Dashboard", key="back_not_found"):
            st.session_state.bo_view_mode = "dashboard"
            st.rerun()
        return

    # ── Build context (kernel) ────────────────────────────────────────
    ctx = build_context(order, st)

    # ── Lazy workflow refresh ─────────────────────────────────────────
    _run_lazy_refresh(ctx)

    # ── Sidebar (optional) ────────────────────────────────────────────
    _render_sidebar(ctx)

    # ── Order header ──────────────────────────────────────────────────
    _render_order_header(ctx)

    # ── Supplier section (above tabs) ─────────────────────────────────
    render_supplier_order_panel(ctx)

    # ── Product change dialog (if active) ─────────────────────────────
    if ctx.session.get("bo_product_change_modal", {}).get("active", False):
        product_change_dialog(ctx)

    # ── Fulfillment mode header ───────────────────────────────────────
    render_fulfillment_header(ctx)

    # ── Tabs ─────────────────────────────────────────────────────────
    _render_tabs(ctx)


# ═══════════════════════════════════════════════════════════════════════
# TAB ROUTER — still the shell's job, but pure delegation
# ═══════════════════════════════════════════════════════════════════════

def _render_tabs(ctx: BackofficeContext) -> None:
    """
    Build tab list from fixed tabs + active plugins, then delegate.

    COUNTER_SALE orders get an extra "⚡ Counter Billing" tab injected
    between Billing Summary and plugins — it embeds billing_gate directly
    so the operator never has to navigate to a separate billing screen.

    Tabs 5+ are plugin-driven — zero hardcoded try/except import blocks.
    """
    active_plugins = get_active_plugins(ctx)

    # Detect COUNTER_SALE — check order dict then DB
    _order_src = str(ctx.order.get("order_source") or "").upper()
    _is_counter_sale = (_order_src == "COUNTER_SALE")

    # Streamlit opens the first tab after every rerun. Payment/challan actions
    # set bo_show_billing_tab, so render Billing first for that one rerun.
    _jump_billing = (
        st.session_state.pop("bo_jump_to_billing", False)
        or st.session_state.pop("bo_show_billing_tab", False)
    )
    if _jump_billing:
        fixed_labels = [
            "💰 Billing Summary ◀",
            "📦 Order Items",
            "🔬 Lab Orders",
            "📊 Status",
        ]
    else:
        fixed_labels = [
            "📦 Order Items",
            "🔬 Lab Orders",
            "📊 Status",
            "💰 Billing Summary",
        ]
    if _is_counter_sale:
        fixed_labels.append("⚡ Counter Billing")

    plugin_labels = [p.label for p in active_plugins]
    all_tabs = st.tabs(fixed_labels + plugin_labels)

    if _jump_billing:
        tab4, tab1, tab2, tab3 = all_tabs[0], all_tabs[1], all_tabs[2], all_tabs[3]
    else:
        tab1, tab2, tab3, tab4 = all_tabs[0], all_tabs[1], all_tabs[2], all_tabs[3]
    tab_counter = all_tabs[4] if _is_counter_sale else None
    plugin_tabs = all_tabs[5:] if _is_counter_sale else all_tabs[4:]

    with tab1:
        render_order_items_panel(ctx)

    with tab2:
        render_lab_output_panel(ctx)

    with tab3:
        render_status_panel(ctx)

    with tab4:
        render_billing_summary_tab(ctx)

    if _is_counter_sale and tab_counter is not None:
        with tab_counter:
            _render_counter_billing_tab(ctx)

    for plugin, tab in zip(active_plugins, plugin_tabs):
        with tab:
            try:
                plugin.render(ctx)
            except Exception as _e:
                import traceback
                st.error(f"❌ Plugin '{plugin.label}' error: {_e}")
                with st.expander("Debug"):
                    st.code(traceback.format_exc())
                log.error(f"[Shell] Plugin {plugin.id} failed: {_e}", exc_info=True)


def _render_counter_billing_tab(ctx: BackofficeContext) -> None:
    """
    Dedicated billing tab for COUNTER_SALE orders.

    Embeds billing_gate (billed_qty tracking + challan/invoice creation)
    directly inside the backoffice order view so the operator does not
    have to navigate to a separate billing screen.

    Flow for a counter-sale order:
      1. Order arrives here with status=READY_FOR_BILLING
         and allocated_qty=quantity on every line (set at cart build).
      2. Billing gate auto-fills billed_qty — operator sees read-only badges.
      3. "Save Billing" commits billed_qty; trigger sets status=BILLED.
      4. Operator clicks "Add to Challan" or "Create Direct Invoice" inline.
    """
    order = ctx.order
    st.markdown("### ⚡ Counter Sale Billing")
    st.caption(
        "Stock was verified and FIFO-allocated at cart build. "
        "Billing qty is auto-filled — click **Save Billing** then create your document."
    )

    # Live status badge
    try:
        from modules.backoffice.order_status_live import get_live_status, status_badge_html
        _live = get_live_status(order)
        st.markdown(status_badge_html(_live, size="0.85rem"), unsafe_allow_html=True)
    except Exception:
        pass

    st.markdown("---")

    # Embed the full billing gate
    try:
        from modules.backoffice.billing_gate import render_billing_gate
        render_billing_gate(order)
    except Exception as _e:
        st.error(f"Billing gate error: {_e}")
        import traceback
        with st.expander("Debug"):
            st.code(traceback.format_exc())


# ═══════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS — shell housekeeping only, no UI rendering
# ═══════════════════════════════════════════════════════════════════════

def _reset_assignment_state_if_switched(order_id: str) -> None:
    key = "_bo_assignment_last_order_id"
    if st.session_state.get(key) != order_id:
        st.session_state[key]                         = order_id
        st.session_state["bo_assignments"]            = {}
        st.session_state["bo_assignments_locked"]     = False
        st.session_state["bo_shift_target"]           = None


def _find_order(order_id: str) -> Optional[dict]:
    for o in st.session_state.get("bo_active_orders", []):
        if get_display_order_id(o) == order_id:
            return o
    return None


def _run_lazy_refresh(ctx: BackofficeContext) -> None:
    from .backoffice_logic import refresh_line_state, recalculate_order_totals
    order      = ctx.order
    needs_rerun = False
    for line in order.get("lines", []):
        if line.get("_needs_refresh"):
            try:
                refresh_line_state(line)
            except Exception as e:
                log.warning(f"[Shell] refresh_line_state failed for {line.get('product_name')}: {e}")
            finally:
                line["_needs_refresh"] = False
            needs_rerun = True
    if needs_rerun:
        recalculate_order_totals(order)
        try:
            from .backoffice_helpers import categorize_order_lines
            categorize_order_lines(order)
        except ImportError:
            pass


def _render_sidebar(ctx: BackofficeContext) -> None:
    if _sidebar is not None:
        try:
            _sidebar(ctx.order)
        except Exception as e:
            log.warning(f"[Shell] Sidebar render failed: {e}")


def _render_order_header(ctx: BackofficeContext) -> None:
    """Order title + back button + metrics — 15 lines, no logic."""
    order = ctx.order

    col1, col2 = st.columns([3, 1])
    with col1:
        st.title(f"📋 Order: {ctx.order_id}")
    with col2:
        if st.button("⬅️ Back", use_container_width=True, key="back_btn"):
            ctx.session["bo_view_mode"]              = "dashboard"
            ctx.session["bo_editing_line"]           = None
            ctx.session["bo_show_allocation_window"] = False
            st.rerun()

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Patient", order.get("patient_name", "N/A"))
    col_b.metric("Status",  order.get("status", "PENDING"))
    date_str = str(order.get("created_at", ""))[:10] or "N/A"
    col_c.metric("Date", date_str)


# ═══════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY
# ═══════════════════════════════════════════════════════════════════════

def generate_job_cards(order: dict) -> None:
    from .lab_output_layer import generate_job_cards as _g; _g(order)

def generate_lab_orders(order: dict) -> None:
    from .lab_output_layer import generate_lab_orders as _g; _g(order)

def generate_labels(order: dict) -> None:
    from .lab_output_layer import generate_labels as _g; _g(order)

def show_supplier_order_section(order: dict) -> None:
    ctx = build_context(order, st)
    render_supplier_order_panel(ctx)
