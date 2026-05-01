"""
lab_output_layer.py
====================
Lab Output Layer — document generation for orders.

RENAMED from output_layer.py → lab_output_layer.py (Issue minor 4)
Reason: supplier outputs will come later. Name scopes this correctly.

RULE (Issue 1 — ctx everywhere)
---------------------------------
  All public functions take ctx: BackofficeContext as first argument.
  ctx.order replaces the raw `order` parameter.

WHAT THIS LAYER OWNS
--------------------
  - Job Cards (in-house surfacing)
  - Lab Orders (external lab documents)
  - Labels (stock line labels)
  - Smart document routing (auto-select default doc type)

WHAT IT DOES NOT OWN
---------------------
  - Billing (→ transaction_core.py)
  - Fulfillment routing (→ fulfillment/)
  - Status updates (→ status_panel.py, future)

HOW TO USE
----------
In backoffice_shell.py, inside tab2:

    from .lab_output_layer import render_lab_output_panel
    render_lab_output_panel(ctx)
"""

import streamlit as st
import pandas as pd
import datetime
from typing import Dict, List

from .backoffice_helpers import fmt_signed, get_display_order_id
from .kernel import BackofficeContext


# ═══════════════════════════════════════════════════════════════════════
# PUBLIC API — ctx-first entry point
# ═══════════════════════════════════════════════════════════════════════

def render_lab_output_panel(ctx: BackofficeContext) -> None:
    """
    Smart document panel — auto-selects most relevant doc type.

    Routing logic (decision, not UI):
      Stock only    → Labels
      Inhouse only  → Job Cards
      Lab only      → Lab Orders
      Mixed         → All

    RENAMED from render_output_tabs(order, order_id)
    """
    order    = ctx.order
    order_id = ctx.order_id

    has_inhouse = bool(order.get("inhouse_lines"))
    has_lab     = bool(order.get("lab_order_lines"))
    has_stock   = bool(order.get("stock_lines"))

    if has_stock and not has_inhouse and not has_lab:
        default_doc = "Labels"
    elif has_inhouse and not has_lab:
        default_doc = "Job Cards"
    elif has_lab and not has_inhouse:
        default_doc = "Lab Orders"
    else:
        default_doc = "All"

    doc_options = ["Job Cards", "Lab Orders", "Labels", "All"]
    doc_type = st.radio(
        "Document Type",
        doc_options,
        index=doc_options.index(default_doc),
        horizontal=True,
        key=f"doc_type_{order_id}",
    )

    shown_any = False

    if doc_type in ("Job Cards", "All"):
        if has_inhouse:
            render_job_cards_panel(ctx)
            shown_any = True
        elif doc_type == "Job Cards":
            st.info("No in-house manufacturing lines — no job cards to generate.")

    if doc_type in ("Lab Orders", "All"):
        if has_lab:
            render_lab_orders_panel(ctx)
            shown_any = True
        elif doc_type == "Lab Orders":
            st.info("No external lab lines — no lab orders to generate.")

    if doc_type in ("Labels", "All"):
        if has_stock:
            render_labels_panel(ctx)
            shown_any = True
        elif doc_type == "Labels":
            st.info("No stock lines — no labels to generate.")

    if doc_type == "All" and not shown_any:
        st.warning(
            "No documents to generate for this order yet. "
            "Add and allocate line items first."
        )


# ═══════════════════════════════════════════════════════════════════════
# JOB CARDS PANEL
# ═══════════════════════════════════════════════════════════════════════

def render_job_cards_panel(ctx: BackofficeContext) -> None:
    """
    Job cards for in-house surfacing lines.
    RENAMED from generate_job_cards(order) → render_job_cards_panel(ctx)
    """
    from modules.documents.job_card_surfacing import (
        render_surfacing_job_card,
        render_job_card_print,
    )

    order         = ctx.order
    inhouse_lines = order.get("inhouse_lines", [])

    st.markdown("---")
    st.markdown("### 🔧 In-House Job Cards")

    if not inhouse_lines:
        st.warning("No in-house items requiring job cards")
        return

    # Group R and L lines by product so they can be shown side by side
    from .backoffice_helpers import get_display_order_id
    product_groups: dict = {}
    for idx, line in enumerate(inhouse_lines):
        pid = line.get("product_id") or line.get("product_name", f"line_{idx}")
        if pid not in product_groups:
            product_groups[pid] = {"R": None, "R_idx": None, "L": None, "L_idx": None,
                                   "product_name": line.get("product_name", "N/A")}
        eye = (line.get("eye_side") or "").upper().strip()
        if eye in ("R", "RIGHT") and product_groups[pid]["R"] is None:
            product_groups[pid]["R"] = line
            product_groups[pid]["R_idx"] = idx
        elif eye in ("L", "LEFT") and product_groups[pid]["L"] is None:
            product_groups[pid]["L"] = line
            product_groups[pid]["L_idx"] = idx
        else:
            # Odd / unmatched — put in a solo slot
            solo_key = f"{pid}_{idx}"
            product_groups[solo_key] = {"R": line, "R_idx": idx,
                                        "L": None, "L_idx": None,
                                        "product_name": line.get("product_name", "N/A")}

    for grp_key, grp in product_groups.items():
        has_r = grp["R"] is not None
        has_l = grp["L"] is not None

        product_name = grp["product_name"]
        st.markdown(f"#### 👁️ {product_name} — Right & Left Eye")

        def _render_jc_expander(line, idx, eye_label):
            is_done = bool(line.get("surfacing_data"))
            title = ("✅ RIGHT EYE — Saved" if is_done else "👁 RIGHT EYE") if eye_label == "R" \
                else ("✅ LEFT EYE — Saved" if is_done else "👁 LEFT EYE")
            with st.expander(title, expanded=not is_done):
                render_surfacing_job_card(line, order)
                st.markdown("---")
                if line.get("surfacing_data"):
                    with st.expander("🖨️ Print Preview", expanded=False):
                        render_job_card_print(line, order)
                        if st.button("🖨️ Print Job Card", key=f"print_job_{idx}"):
                            from modules.documents.job_engine import format_job_card_for_print
                            job_data = {
                                "order_no":  order.get("order_no"),
                                "patient":   order.get("patient_name"),
                                "eye":       line.get("eye_side"),
                                "product":   line.get("product_name"),
                                "brand":     line.get("brand"),
                                "category":  line.get("category"),
                                "sph":       line.get("sph"),
                                "cyl":       line.get("cyl"),
                                "axis":      line.get("axis"),
                                "add":       line.get("add_power"),
                                "surfacing": line.get("surfacing_data"),
                            }
                            st.code(format_job_card_for_print(job_data), language=None)

        if has_r and has_l:
            col_r, col_divider, col_l = st.columns([10, 1, 10])
            with col_r:
                _render_jc_expander(grp["R"], grp["R_idx"], "R")
            with col_divider:
                st.markdown(
                    "<div style='border-left:2px dashed #334155;height:100%;"
                    "margin:0 auto;width:2px;'></div>",
                    unsafe_allow_html=True,
                )
            with col_l:
                _render_jc_expander(grp["L"], grp["L_idx"], "L")
        elif has_r:
            _render_jc_expander(grp["R"], grp["R_idx"], "R")
        elif has_l:
            _render_jc_expander(grp["L"], grp["L_idx"], "L")

        st.markdown("---")


# ═══════════════════════════════════════════════════════════════════════
# LAB ORDERS PANEL
# ═══════════════════════════════════════════════════════════════════════

def render_lab_orders_panel(ctx: BackofficeContext) -> None:
    """
    Lab orders for external lab lines.
    RENAMED from generate_lab_orders(order) → render_lab_orders_panel(ctx)
    """
    order     = ctx.order
    lab_lines = order.get("lab_order_lines", [])

    st.markdown("---")
    st.markdown("### 🔬 Lab Orders")

    if not lab_lines:
        st.warning("No lab order items")
        return

    # Group R and L by product — show side by side
    product_groups: dict = {}
    for idx, line in enumerate(lab_lines):
        pid = line.get("product_id") or line.get("product_name", f"line_{idx}")
        if pid not in product_groups:
            product_groups[pid] = {"R": None, "L": None,
                                   "product_name": line.get("product_name", "N/A"),
                                   "brand": line.get("brand", "N/A")}
        eye = (line.get("eye_side") or "").upper().strip()
        if eye in ("R", "RIGHT") and product_groups[pid]["R"] is None:
            product_groups[pid]["R"] = line
        elif eye in ("L", "LEFT") and product_groups[pid]["L"] is None:
            product_groups[pid]["L"] = line
        else:
            solo_key = f"{pid}_{idx}"
            product_groups[solo_key] = {"R": line, "L": None,
                                        "product_name": line.get("product_name", "N/A"),
                                        "brand": line.get("brand", "N/A")}

    def _render_lab_eye_card(line, eye_label):
        label = "### 👁 RIGHT EYE" if eye_label == "R" else "### 👁 LEFT EYE"
        st.markdown(label)
        with st.container(border=True):
            billing_qty = int(line.get("billing_qty", 0))
            allocated   = int(line.get("allocated_qty", 0))
            pending     = max(0, billing_qty - allocated)
            st.caption(f"**Product:** {line.get('product_name', 'N/A')} | **Brand:** {line.get('brand', 'N/A')}")
            st.caption(f"**Eye:** {line.get('eye_side', 'N/A')}  |  "
                       f"**SPH:** {fmt_signed(line.get('sph'))}  |  "
                       f"**CYL:** {fmt_signed(line.get('cyl'))}  |  "
                       f"**AXIS:** {line.get('axis', 'N/A')}  |  "
                       f"**Qty to Order:** {pending}")

    for grp_key, grp in product_groups.items():
        st.markdown(f"#### 👁️ {grp['product_name']}")
        if grp['brand'] != 'N/A':
            st.caption(f"Brand: {grp['brand']}")

        has_r = grp["R"] is not None
        has_l = grp["L"] is not None

        if has_r and has_l:
            col_r, col_l = st.columns(2)
        elif has_r:
            col_r = st.container()
            col_l = None
        else:
            col_r = None
            col_l = st.container()

        if has_r:
            with col_r:
                _render_lab_eye_card(grp["R"], "R")
        if has_l:
            with col_l:
                _render_lab_eye_card(grp["L"], "L")

        st.markdown("---")

    lab_name = st.selectbox(
        "Select Lab",
        ["Lab A — Premium Optics", "Lab B — Standard Optics", "Lab C — Express Optics"],
        key="lab_select",
    )
    expected_delivery = st.date_input(
        "Expected Delivery Date",
        value=datetime.date.today() + datetime.timedelta(days=7),
        key="lab_delivery_date",
    )

    from modules.utils.submit_guard import is_locked, guarded_submit
    if st.button(
        "📤 Send Lab Order",
        type="primary",
        use_container_width=True,
        disabled=is_locked("lab_order"),
    ):
        with guarded_submit("lab_order") as _allowed:
            if not _allowed:
                st.stop()
            try:
                from modules.backoffice.audit_logger import audit, AuditAction
                audit(
                    AuditAction.LAB_ORDER_SENT,
                    entity="orders",
                    entity_id=order.get("order_id"),
                    payload={"lab": lab_name, "delivery": str(expected_delivery)},
                )
            except Exception:
                pass
            # Record on ctx audit trail
            ctx.record("lab_order_sent", {"lab": lab_name, "delivery": str(expected_delivery)})
            st.success(f"✅ Lab order sent to {lab_name}")
            st.info(f"Expected delivery: {expected_delivery}")


# ═══════════════════════════════════════════════════════════════════════
# LABELS PANEL
# ═══════════════════════════════════════════════════════════════════════

def render_labels_panel(ctx: BackofficeContext) -> None:
    """
    Labels for stock lines.
    RENAMED from generate_labels(order) → render_labels_panel(ctx)
    """
    order       = ctx.order
    stock_lines = order.get("stock_lines", [])

    st.markdown("---")
    st.markdown("### 🏷️ Product Labels")

    if not stock_lines:
        st.warning("No stock items requiring labels")
        return

    for idx, line in enumerate(stock_lines, 1):
        col1, col2 = st.columns([3, 2])

        with col1:
            st.markdown(f"#### Label #{idx}")
            st.text(f"Product: {line.get('product_name', 'N/A')}")
            st.text(f"Brand:   {line.get('brand', 'N/A')}")
            st.text(f"Patient: {order.get('patient_name', 'N/A')}")
            st.text(f"Eye:     {line.get('eye_side', 'N/A')}")

            if line.get("sph") is not None:
                import math as _math
                _cyl = line.get("cyl")
                _add = line.get("add_power")
                power_str = f"Power: SPH {fmt_signed(line.get('sph'))}"
                if _cyl is not None and not _math.isnan(float(_cyl or 0)) and abs(float(_cyl or 0)) > 0.01:
                    power_str += f" | CYL {fmt_signed(_cyl)}"
                    if line.get("axis") is not None:
                        power_str += f" | AXIS {line.get('axis')}"
                if _add is not None and not _math.isnan(float(_add or 0)) and float(_add or 0) > 0:
                    power_str += f" | ADD {fmt_signed(_add)}"
                st.text(power_str)

            if line.get("batch_allocation"):
                batch_info = ", ".join(
                    b.get("batch_no", "N/A") for b in line.get("batch_allocation", [])
                )
                st.text(f"Batch(es): {batch_info}")

        with col2:
            st.text(f"Order: {get_display_order_id(order)}")
            order_date = order.get("created_at")
            if order_date:
                date_str = order_date.strftime("%Y-%m-%d") if hasattr(order_date, "strftime") else str(order_date)[:10]
            else:
                date_str = "N/A"
            st.text(f"Date: {date_str}")
            st.text(f"Qty:  {line.get('billing_qty', 0)}")

        st.markdown("---")

    st.success(f"✅ {len(stock_lines)} label(s) ready for printing")


# ═══════════════════════════════════════════════════════════════════════
# BACKWARD COMPAT — old function names still work
# ═══════════════════════════════════════════════════════════════════════

def render_output_tabs(order: Dict, order_id: str) -> None:
    """Compat: old (order, order_id) signature → wraps ctx."""
    import streamlit as st
    from .kernel import BackofficeContext
    ctx = BackofficeContext(order=order, session_state=st.session_state)
    render_lab_output_panel(ctx)


def generate_job_cards(order: Dict) -> None:
    """Compat: old generate_job_cards(order)."""
    import streamlit as st
    from .kernel import BackofficeContext
    ctx = BackofficeContext(order=order, session_state=st.session_state)
    render_job_cards_panel(ctx)


def generate_lab_orders(order: Dict) -> None:
    """Compat: old generate_lab_orders(order)."""
    import streamlit as st
    from .kernel import BackofficeContext
    ctx = BackofficeContext(order=order, session_state=st.session_state)
    render_lab_orders_panel(ctx)


def generate_labels(order: Dict) -> None:
    """Compat: old generate_labels(order)."""
    import streamlit as st
    from .kernel import BackofficeContext
    ctx = BackofficeContext(order=order, session_state=st.session_state)
    render_labels_panel(ctx)
