"""
output_layer.py
===============
Output Layer — all document generation for an order.

Extracted from backoffice_ui.py as part of Issue 1 refactor (PASS 3).

WHAT THIS LAYER OWNS
--------------------
  - Job Cards (in-house surfacing)
  - Lab Orders (external lab documents)
  - Labels (stock line labels)
  - Smart document routing (which doc type to show by default)

WHAT IT DOES NOT OWN
---------------------
  - Billing (→ transaction_core.py)
  - Fulfillment routing (→ fulfillment_layer.py)
  - Status updates

NAMING NOTE
-----------
  "Documents" tab in UI is now "Lab Orders" per blueprint rename.
  This layer supports that rename — the tab label change is in backoffice_shell.py.

HOW TO USE
----------
In backoffice_shell.py, inside tab2 (Documents / Lab Orders):

    from .output_layer import render_output_tabs
    render_output_tabs(order, order_id)
"""

import streamlit as st
import pandas as pd
import datetime
from typing import Dict, List

from .backoffice_helpers import fmt_signed, get_display_order_id


# ═══════════════════════════════════════════════════════════════════════
# PUBLIC API — main entry point
# ═══════════════════════════════════════════════════════════════════════

def render_output_tabs(order: Dict, order_id: str) -> None:
    """
    Smart document tab — auto-selects the most relevant doc type.

    Logic:
      - Stock only   → Labels
      - Inhouse only → Job Cards
      - Lab only     → Lab Orders
      - Mixed        → All

    MOVED FROM: backoffice_ui.py → tab2 block
    RENAME:     Documents → Lab Orders (blueprint alignment)
    """
    has_inhouse = bool(order.get("inhouse_lines"))
    has_lab     = bool(order.get("lab_order_lines"))
    has_stock   = bool(order.get("stock_lines"))

    # Smart default
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
            generate_job_cards(order)
            shown_any = True
        elif doc_type == "Job Cards":
            st.info("No in-house manufacturing lines on this order — no job cards to generate.")

    if doc_type in ("Lab Orders", "All"):
        if has_lab:
            generate_lab_orders(order)
            shown_any = True
        elif doc_type == "Lab Orders":
            st.info("No external lab lines on this order — no lab orders to generate.")

    if doc_type in ("Labels", "All"):
        if has_stock:
            generate_labels(order)
            shown_any = True
        elif doc_type == "Labels":
            st.info("No stock lines on this order — no labels to generate.")

    if doc_type == "All" and not shown_any:
        st.warning(
            "No documents to generate for this order yet. "
            "Add and allocate line items first."
        )


# ═══════════════════════════════════════════════════════════════════════
# JOB CARDS
# ═══════════════════════════════════════════════════════════════════════

def generate_job_cards(order: Dict) -> None:
    """
    Generate job cards for in-house surfacing lines.
    MOVED FROM: backoffice_ui.py → generate_job_cards()
    """
    from modules.documents.job_card_surfacing import (
        render_surfacing_job_card,
        render_job_card_print,
    )

    st.markdown("---")
    st.markdown("### 🔧 In-House Job Cards")

    inhouse_lines = order.get("inhouse_lines", [])

    if not inhouse_lines:
        st.warning("No in-house items requiring job cards")
        return

    for idx, line in enumerate(inhouse_lines, 1):
        with st.expander(
            f"Job Card #{idx} — {line.get('product_name', 'N/A')} — "
            f"{line.get('eye_side', 'N/A')} Eye",
            expanded=False,
        ):
            render_surfacing_job_card(line, order)
            st.markdown("---")

            if line.get("surfacing_data"):
                with st.expander("🖨️ Print Preview", expanded=False):
                    render_job_card_print(line, order)

                    if st.button("🖨️ Print Job Card", key=f"print_job_{idx}"):
                        from modules.documents.job_engine import format_job_card_for_print
                        job_data = {
                            "order_no": order.get("order_no"),
                            "patient":  order.get("patient_name"),
                            "eye":      line.get("eye_side"),
                            "product":  line.get("product_name"),
                            "brand":    line.get("brand"),
                            "category": line.get("category"),
                            "sph":      line.get("sph"),
                            "cyl":      line.get("cyl"),
                            "axis":     line.get("axis"),
                            "add":      line.get("add_power"),
                            "surfacing": line.get("surfacing_data"),
                        }
                        print_text = format_job_card_for_print(job_data)
                        st.code(print_text, language=None)


# ═══════════════════════════════════════════════════════════════════════
# LAB ORDERS
# ═══════════════════════════════════════════════════════════════════════

def generate_lab_orders(order: Dict) -> None:
    """
    Delegate to the proper lab_output_layer which has the full external lab order UI.
    """
    try:
        from modules.backoffice.lab_output_layer import generate_lab_orders as _real_lab
        _real_lab(order)
    except ImportError:
        # Fallback: minimal view
        st.markdown("### 🔬 Lab Orders")
        lab_lines = order.get("lab_order_lines", [])
        if not lab_lines:
            st.info("No external lab lines on this order.")
            return
        for line in lab_lines:
            st.markdown(
                f"**{line.get('product_name','—')}** · {line.get('eye_side','—')} · "
                f"SPH {fmt_signed(line.get('sph'))} CYL {fmt_signed(line.get('cyl'))} "
                f"AXIS {line.get('axis','—')}"
            )


# ═══════════════════════════════════════════════════════════════════════
# LABELS
# ═══════════════════════════════════════════════════════════════════════

def generate_labels(order: Dict) -> None:
    """
    Generate product labels for ALL order lines (stock + inhouse + lab).
    """
    st.markdown("---")
    st.markdown("### 🏷️ Product Labels")

    # Combine all lines — labels needed for every product
    stock_lines   = order.get("stock_lines", [])
    inhouse_lines = order.get("inhouse_lines", [])
    lab_lines     = order.get("lab_order_lines", [])
    all_label_lines = stock_lines + inhouse_lines + lab_lines

    if not all_label_lines:
        st.warning("No items to generate labels for.")
        return

    # Show which types are present
    type_summary = []
    if stock_lines:   type_summary.append(f"{len(stock_lines)} stock")
    if inhouse_lines: type_summary.append(f"{len(inhouse_lines)} inhouse")
    if lab_lines:     type_summary.append(f"{len(lab_lines)} lab")
    st.caption(f"Generating labels for: {', '.join(type_summary)}")

    stock_lines = all_label_lines  # reuse below

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
