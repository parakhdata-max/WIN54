"""
advisory_panel.py
=================
Track 2 — Advisory Procurement Panel UI.

ARCHITECTURE (Issue 4 fixed)
-----------------------------
  UI (this file) → advisory_service.py → sql_adapter
  Never: UI → sql_adapter directly

ROLE GATE (Issue minor 2)
--------------------------
  Panel is restricted to roles in SYSTEM_FLAGS["advisory_roles"].
  When list is empty (default) → no restriction.
  Wire in app.py: kernel.set_flag("advisory_roles", ["manager", "inventory"])

FUNCTIONS
---------
  render_advisory_panel(ctx)          → main entry point (ctx-first)
  render_smart_alert_cards(ctx)       → Section A
  render_quick_refill_panel(ctx)      → Section B
  render_advisory_po_tracker(ctx)     → Section C
  render_threshold_management(ctx)    → Settings

NAMING (Issue minor 1)
-----------------------
  This file is a panel (pure UI), so suffix is correct: advisory_panel.py
"""

import streamlit as st
import datetime
from typing import Dict, List, Optional

from .kernel import BackofficeContext, SYSTEM_FLAGS

# ── Service layer (Issue 4 — no direct sql_adapter calls from UI) ────
from modules.procurement.advisory.advisory_service import (
    ADVISORY_GROUPS,
    load_advisory_inventory,
    compute_alerts,
    get_ranked_suppliers_for_product,
    load_advisory_pos,
    create_quick_refill_po,
    bundle_alerts_into_pos,
    load_products_for_group,
    save_threshold,
    get_group_config,
)


# ═══════════════════════════════════════════════════════════════════════
# STANDALONE ENTRY POINT (for app.py page wiring)
# ═══════════════════════════════════════════════════════════════════════

def render_advisory_panel_page() -> None:
    """
    Standalone page entry point — builds a minimal ctx from session.

    Use this in app.py:
        from modules.backoffice.advisory_panel import render_advisory_panel_page
        render_advisory_panel_page()
    """
    import streamlit as st
    from .kernel import build_context

    # Build minimal ctx (no live order needed for advisory)
    ctx = build_context(order={}, st_module=st)
    render_advisory_panel(ctx)


# ═══════════════════════════════════════════════════════════════════════
# MAIN PANEL — ctx-first (Issue 1)
# ═══════════════════════════════════════════════════════════════════════

def render_advisory_panel(ctx: BackofficeContext) -> None:
    """
    Track 2 Advisory Procurement Panel — main entry point.

    Role gate: checks ctx.can_access_advisory() before rendering.
    """
    # ── Role gate (Issue minor 2) ─────────────────────────────────────
    if not ctx.can_access_advisory():
        st.warning(
            "⚠️ Advisory Procurement is restricted to "
            f"inventory / manager roles. Contact your administrator."
        )
        return

    st.title("🧭 Advisory Procurement")
    st.caption(
        "Track 2 — Frames, Solutions, Accessories, Blanks. "
        "Smart reorder alerts + Quick Refill for specific models."
    )

    # Group filter
    selected_groups = st.multiselect(
        "Product Groups",
        list(ADVISORY_GROUPS.keys()),
        default=list(ADVISORY_GROUPS.keys()),
        key="advisory_group_filter",
    )

    if not selected_groups:
        st.info("Select at least one product group above.")
        return

    st.markdown("---")

    # Load via service (never directly from sql_adapter)
    inventory = load_advisory_inventory(selected_groups)

    if inventory is None:
        st.warning("⚠️ Could not load inventory data. Check database connection.")
        return

    sec_a, sec_b, sec_c, sec_settings = st.tabs([
        "🚨 Smart Alerts",
        "⚡ Quick Refill",
        "📋 PO Tracker",
        "⚙️ Thresholds",
    ])

    with sec_a:
        render_smart_alert_cards(ctx, inventory, selected_groups)

    with sec_b:
        render_quick_refill_panel(ctx, inventory, selected_groups)

    with sec_c:
        render_advisory_po_tracker(ctx)

    with sec_settings:
        render_threshold_management(ctx, selected_groups)


# ═══════════════════════════════════════════════════════════════════════
# SECTION A — SMART ALERT CARDS
# ═══════════════════════════════════════════════════════════════════════

def render_smart_alert_cards(ctx: BackofficeContext, inventory, selected_groups: List[str]) -> None:
    """
    Smart Alert Cards — urgency-sorted product alerts.
    Data computed by advisory_service.compute_alerts().
    """
    import pandas as pd

    st.markdown("### 🚨 Smart Alert Cards")
    st.caption("Products below reorder threshold, sorted by urgency.")

    # Service call — no SQL here
    alerts = compute_alerts(inventory)

    if alerts.empty:
        st.success("✅ All advisory products are well-stocked.")
        return

    urgency_filter = st.radio(
        "Show",
        ["All Alerts", "🔴 Urgent (≤ 3 days)", "🟡 Low Stock (4–7 days)"],
        horizontal=True,
        key="alert_urgency_filter",
    )

    if urgency_filter == "🔴 Urgent (≤ 3 days)":
        alerts = alerts[alerts["urgency_tier"] == "urgent"]
    elif urgency_filter == "🟡 Low Stock (4–7 days)":
        alerts = alerts[alerts["urgency_tier"] == "low"]

    if alerts.empty:
        st.info("No alerts matching this filter.")
        return

    for _, row in alerts.iterrows():
        _render_alert_card(row)

    st.markdown("---")

    if st.button(
        f"📦 Bundle All Alerts into POs ({len(alerts)} products)",
        type="primary",
        use_container_width=True,
        key="bundle_all_alerts",
    ):
        ctx.record("advisory_bundle_triggered", {"count": len(alerts)})
        results = bundle_alerts_into_pos(alerts)
        if results:
            for supplier, po_id in results.items():
                st.success(f"✅ PO #{po_id} created for {supplier}")
        else:
            st.warning("⚠️ Bundle PO failed — check DB connection or supplier setup.")


def _render_alert_card(row) -> None:
    """Render a single alert card row."""
    group_config = get_group_config(row.get("product_group", ""))
    icon         = group_config.get("icon", "📦")
    tier         = row.get("urgency_tier", "watch")
    days         = row.get("days_of_stock", 99)
    stock        = row.get("current_stock", 0)
    reorder_qty  = row.get("suggested_reorder_qty", 0)

    tier_display = {
        "urgent": ("error",   "🔴 URGENT"),
        "low":    ("warning", "🟡 LOW STOCK"),
        "watch":  ("info",    "🔵 WATCH"),
    }
    level, badge = tier_display.get(tier, ("info", "🔵 WATCH"))

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 1, 1, 2])
        with c1:
            st.markdown(f"**{icon} {row.get('product_name', 'N/A')}**")
            st.caption(f"Brand: {row.get('brand', 'N/A')} | SKU: {row.get('product_id', 'N/A')}")
        with c2:
            st.metric("Stock", f"{int(stock)}")
        with c3:
            st.metric("Days Left", f"~{days}d")
        with c4:
            getattr(st, level)(f"{badge} — Reorder: {reorder_qty} units")


# ═══════════════════════════════════════════════════════════════════════
# SECTION B — QUICK REFILL
# ═══════════════════════════════════════════════════════════════════════

def render_quick_refill_panel(ctx: BackofficeContext, inventory, selected_groups: List[str]) -> None:
    """
    Quick Refill — the Rayban 3036 scenario.
    All data operations go through advisory_service.
    """
    st.markdown("### ⚡ Quick Refill")
    st.caption(
        "Restock a specific model immediately. "
        "Example: 'I need 5 more Rayban 3036 in black'."
    )

    # Step 1: Search
    st.markdown("**Step 1 — Find Product**")
    col_search, col_group = st.columns([3, 2])
    with col_search:
        search_term = st.text_input(
            "Product name / model",
            placeholder="e.g. Rayban 3036, Titan T-1234…",
            key="quick_refill_search",
        )
    with col_group:
        group_filter = st.selectbox(
            "Product Group",
            ["All"] + selected_groups,
            key="quick_refill_group",
        )

    if inventory is None or inventory.empty:
        st.info("No inventory data available.")
        return

    filtered = inventory.copy()
    if search_term:
        filtered = filtered[filtered["product_name"].str.contains(search_term, case=False, na=False)]
    if group_filter != "All":
        filtered = filtered[filtered["product_group"] == group_filter]

    if filtered.empty:
        if search_term:
            st.warning(f"No products found matching '{search_term}'")
        else:
            st.info("Search above to begin Quick Refill.")
        return

    # Step 2: Select
    st.markdown("**Step 2 — Select**")
    product_options = filtered["product_name"].tolist()
    selected_product = st.selectbox("Product", product_options, key="quick_refill_product")

    if not selected_product:
        return

    product_row = filtered[filtered["product_name"] == selected_product].iloc[0]
    product_id  = str(product_row.get("product_id", ""))

    st.info(
        f"📦 Current stock: **{int(product_row.get('current_stock', 0))}** units  |  "
        f"Min stock: **{int(product_row.get('min_stock', 0))}**  |  "
        f"Preferred supplier: **{product_row.get('preferred_supplier', 'N/A')}**"
    )

    # Step 3: Quantity
    st.markdown("**Step 3 — Quantity**")
    min_stock  = int(product_row.get("min_stock", 10))
    curr_stock = int(product_row.get("current_stock", 0))
    suggested  = max(0, min_stock - curr_stock) + min_stock

    refill_qty = st.number_input(
        "Refill Quantity", min_value=1, value=max(1, suggested), step=1,
        key="quick_refill_qty",
    )

    # Step 4: Supplier (from service)
    st.markdown("**Step 4 — Supplier**")
    suppliers = get_ranked_suppliers_for_product(product_id)

    if not suppliers:
        st.warning("⚠️ No suppliers configured. Add in Party Master.")
        return

    supplier_options = {s["id"]: s["name"] for s in suppliers}
    preferred_id     = str(product_row.get("preferred_supplier_id") or "")
    default_idx      = next(
        (i for i, sid in enumerate(supplier_options) if sid == preferred_id), 0
    )

    chosen_supplier_id = st.selectbox(
        "Supplier",
        list(supplier_options.keys()),
        index=default_idx,
        format_func=lambda x: supplier_options[x],
        key="quick_refill_supplier",
    )

    # Step 5: Notes + Send
    st.markdown("**Step 5 — Confirm**")
    notes = st.text_area(
        "PO Notes (optional)",
        placeholder="e.g. Urgent — needed for patient appointment on 28 Feb",
        key="quick_refill_notes",
        height=80,
    )

    with st.expander("📱 Preview Message to Supplier", expanded=False):
        _render_whatsapp_preview(
            product_name  = selected_product,
            qty           = refill_qty,
            supplier_name = supplier_options.get(chosen_supplier_id, ""),
            notes         = notes,
        )

    col_send, col_cancel = st.columns(2)

    with col_send:
        if st.button("📤 Send Quick Refill PO", type="primary", use_container_width=True, key="qr_send"):
            # Service call — no SQL in UI
            result = create_quick_refill_po(
                product_id    = product_id,
                product_name  = selected_product,
                qty           = refill_qty,
                supplier_id   = chosen_supplier_id,
                supplier_name = supplier_options.get(chosen_supplier_id, ""),
                notes         = notes,
            )
            if result["success"]:
                ctx.record("quick_refill_po_created", {
                    "product": selected_product,
                    "qty":     refill_qty,
                    "supplier": supplier_options.get(chosen_supplier_id),
                })
                st.success(result["message"])
                st.balloons()
            else:
                st.error(f"❌ PO creation failed: {result.get('error')}")

    with col_cancel:
        if st.button("❌ Cancel", use_container_width=True, key="qr_cancel"):
            st.rerun()


def _render_whatsapp_preview(product_name: str, qty: int, supplier_name: str, notes: str) -> None:
    today   = datetime.date.today().strftime("%d %b %Y")
    message = (
        f"*Purchase Order — {today}*\n\n"
        f"Dear {supplier_name},\n\n"
        f"Please supply the following:\n"
        f"• {product_name} × {qty} units\n\n"
        f"{'Notes: ' + notes if notes else ''}\n\n"
        f"Please confirm availability and delivery date.\n\n"
        f"Thank you."
    )
    st.code(message, language=None)


# ═══════════════════════════════════════════════════════════════════════
# SECTION C — PO TRACKER
# ═══════════════════════════════════════════════════════════════════════

def render_advisory_po_tracker(ctx: BackofficeContext) -> None:
    """Advisory PO Tracker — delegates data to advisory_service."""
    st.markdown("### 📋 Advisory PO Tracker")
    st.caption("Open purchase orders from alerts or quick refill.")

    # Service call
    pos = load_advisory_pos()

    if pos is None or pos.empty:
        st.info("No open advisory POs. Create one via Smart Alerts or Quick Refill.")
        return

    col_status, col_supplier = st.columns(2)
    with col_status:
        status_filter = st.selectbox(
            "Status",
            ["All", "Draft", "Sent", "Confirmed", "Received", "Cancelled"],
            key="advisory_po_status_filter",
        )
    with col_supplier:
        suppliers  = ["All"] + sorted(pos["supplier_name"].dropna().unique().tolist())
        sup_filter = st.selectbox("Supplier", suppliers, key="advisory_po_supplier_filter")

    filtered = pos.copy()
    if status_filter != "All":
        filtered = filtered[filtered["status"] == status_filter]
    if sup_filter != "All":
        filtered = filtered[filtered["supplier_name"] == sup_filter]

    if filtered.empty:
        st.info("No POs matching this filter.")
        return

    display_cols = {
        "po_number":         "PO #",
        "created_at":        "Date",
        "supplier_name":     "Supplier",
        "product_name":      "Product",
        "qty_ordered":       "Qty",
        "status":            "Status",
        "expected_delivery": "Expected",
    }
    available = [c for c in display_cols if c in filtered.columns]
    st.dataframe(
        filtered[available].rename(columns=display_cols),
        use_container_width=True,
    )
    st.caption(f"Showing {len(filtered)} of {len(pos)} advisory POs")


# ═══════════════════════════════════════════════════════════════════════
# SETTINGS — THRESHOLD MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

def render_threshold_management(ctx: BackofficeContext, selected_groups: List[str]) -> None:
    """Threshold settings — delegates data to advisory_service."""
    st.markdown("### ⚙️ Reorder Threshold Management")
    st.caption("Set min/max reorder levels per product.")

    if not selected_groups:
        st.info("No product groups selected.")
        return

    group_tabs = st.tabs(selected_groups)

    for tab, group_name in zip(group_tabs, selected_groups):
        with tab:
            _render_threshold_tab(ctx, group_name)


def _render_threshold_tab(ctx: BackofficeContext, group_name: str) -> None:
    config = get_group_config(group_name)
    icon   = config.get("icon", "📦")

    st.markdown(f"#### {icon} {group_name} Thresholds")
    st.caption(
        f"Reorder trigger: stock < min × {config.get('reorder_ratio', 1.0):.1f}. "
        f"Urgency if < {config.get('urgency_days', 7)} days left."
    )

    # Service call
    products = load_products_for_group(group_name)

    if products is None or products.empty:
        st.info(f"No {group_name} products found.")
        return

    display_cols = [c for c in ["product_name", "brand", "current_stock", "min_stock", "max_stock"] if c in products.columns]
    st.dataframe(products[display_cols], use_container_width=True)

    st.markdown("**Update threshold:**")
    col_p, col_min, col_max = st.columns(3)
    with col_p:
        selected = st.selectbox("Product", products["product_name"].tolist(), key=f"thresh_p_{group_name}")
    with col_min:
        new_min = st.number_input("Min Stock", min_value=0, value=10, key=f"thresh_min_{group_name}")
    with col_max:
        new_max = st.number_input("Max Stock", min_value=0, value=50, key=f"thresh_max_{group_name}")

    if st.button(f"💾 Save for {selected}", key=f"thresh_save_{group_name}"):
        result = save_threshold(selected, new_min, new_max)
        if result["success"]:
            ctx.record("threshold_updated", {"product": selected, "min": new_min, "max": new_max})
            st.success(result["message"])
        else:
            st.error(f"❌ {result.get('error')}")
