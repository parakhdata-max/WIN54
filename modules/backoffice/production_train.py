"""
production_train.py
──────────────────────────────────────────────────────────────────────────────
Compact production pipeline strip for the sidebar. Renders one badge per
active job_master row, showing the current stage and eye side.
──────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import streamlit as st


_STAGE_META = {
    "JOB_CREATED":          ("🖨 Created",    "#64748b"),
    "JOB_PRINTED":          ("🖨 Printed",    "#3b82f6"),
    "PRINTED":              ("🖨 Printed",    "#3b82f6"),
    "BLANK_ALLOCATED":      ("🎯 Blank",      "#8b5cf6"),
    "PRODUCTION_PICKED":    ("⚙ In Prod",    "#f59e0b"),
    "PRODUCTION_DONE":      ("✨ Prod Done",  "#a855f7"),
    "SURFACING_DONE":       ("✨ Surfacing",  "#a855f7"),
    "INSPECTION":           ("🔍 Inspect",    "#ef4444"),
    "HARDCOAT_PICKED":      ("🛡 Hardcoat",   "#f59e0b"),
    "HARDCOAT_DONE":        ("🛡 HC Done",    "#eab308"),
    "HARDCOAT_COMPLETED":   ("🛡 Hardcoat",   "#06b6d4"),
    "COLOURING_PICKED":     ("🎨 Colour",     "#ec4899"),
    "COLOURING_DONE":       ("🎨 Colour Done","#db2777"),
    "ARC_RECEIVED":         ("🔬 ARC",        "#10b981"),
    "ARC_SENT":             ("🔬 ARC Sent",   "#06b6d4"),
    "COLOURING_COMPLETED":  ("🎨 Colour",     "#ec4899"),
    "PRODUCTION_COMPLETED": ("✅ Done",       "#10b981"),
    "FINAL_QC":             ("🔍 QC",         "#0d9488"),
    "READY_FOR_PACK":       ("📦 Pack",       "#10b981"),
    "FITTING_PENDING":      ("⏳ Fitting",    "#7c3aed"),
    "FITTING_SENT":         ("🔧 Fitter",     "#6d28d9"),
    "FITTING_RECEIVED":     ("↩ Fitted",      "#5b21b6"),
    "FITTING_DONE":         ("✅ Fit Done",   "#4c1d95"),
    "READY_FOR_BILLING":    ("🚀 To Bill",    "#059669"),
    "DISPATCHED":           ("🚚 Sent",       "#0891b2"),
    "DELIVERED":            ("✅ Delivered",  "#10b981"),
    # Terminal inhouse stages
    "READY_TO_BILL":        ("💰 To Bill",    "#059669"),
    "REJECTED":             ("🚫 Rejected",   "#dc2626"),
    # Supplier pipeline stages shown in sidebar
    "ORDER_PLACED":         ("📤 Ordered",    "#64748b"),
    "SUPPLIER_CONFIRMED":   ("✅ Confirmed",  "#3b82f6"),
    "AWAITING_SUPPLY":      ("⏳ Awaiting",   "#f59e0b"),
    "RECEIVED":             ("📦 Received",   "#8b5cf6"),
    "INSPECTION":           ("🔍 Inspect",    "#ef4444"),
    "READY_FOR_BILLING":    ("💰 To Bill",    "#059669"),
}


def _q(sql, params):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _get_stage_info(order: dict) -> tuple:
    """Determine current stage and display label. Checks route FIRST."""
    route  = (order.get("route") or order.get("order_type") or "").upper()
    status = (order.get("status") or "").upper()

    # Route check takes priority over status
    if route in ("VENDOR", "SUPPLIER", "PURCHASE"):
        return ("SUPPLIER", "📦 Supplier Order", "blue")

    if status in ("PRODUCTION", "IN_PRODUCTION", "MANUFACTURING"):
        return ("PRODUCTION", "⚙️ In Production", "orange")
    elif status in ("DISPATCHED", "SHIPPED"):
        return ("DISPATCHED", "🚚 Dispatched", "green")
    elif status in ("COMPLETED", "DELIVERED"):
        return ("COMPLETED", "✅ Completed", "green")
    else:
        return ("PENDING", "⏳ Pending", "gray")


# ── Stages visible even when job is_closed (billing-ready) ───────────────────
_CLOSED_SHOW_STAGES = {"READY_TO_BILL", "READY_FOR_PACK"}


def render_train_sidebar(order_no: str) -> None:
    """
    Compact production pipeline strip for the sidebar.
    Shows INHOUSE job stages + supplier stage + stock status in one row.
    Closed jobs are shown when billing-ready (READY_TO_BILL / READY_FOR_PACK).
    """
    import json as _tj

    rows = _q("""
        SELECT
            jm.current_stage,
            jm.is_closed,
            ol.eye_side,
            COALESCE(ol.lens_params, '{}')::text AS lp_raw
        FROM job_master jm
        JOIN order_lines ol ON ol.id = jm.order_line_id
        JOIN orders o ON o.id = ol.order_id
        WHERE o.order_no = %(ono)s
        ORDER BY ol.eye_side
    """, {"ono": order_no})

    # Also fetch supplier/stock lines that have no job_master row
    sup_rows = _q("""
        SELECT
            ol.eye_side,
            COALESCE(ol.lens_params, '{}')::text AS lp_raw
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        WHERE o.order_no = %(ono)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND COALESCE(ol.is_service_line, FALSE) = FALSE
          AND UPPER(COALESCE(ol.eye_side, '')) NOT IN ('S', 'SERVICE')
          AND NOT EXISTS (
              SELECT 1 FROM job_master jm2 WHERE jm2.order_line_id = ol.id
          )
        ORDER BY ol.eye_side
    """, {"ono": order_no})

    if not rows and not sup_rows:
        return

    parts = []

    # ── INHOUSE job stages ────────────────────────────────────────────────────
    for r in rows:
        stage     = (r.get("current_stage") or "JOB_CREATED").upper().strip()
        is_closed = r.get("is_closed")
        eye       = (r.get("eye_side") or "").strip().upper()
        eye_l     = {"R": "RE", "L": "LE"}.get(eye, eye or "—")

        lbl, clr = _STAGE_META.get(stage, (f"⚙ {stage}", "#64748b"))

        if is_closed and stage in _CLOSED_SHOW_STAGES:
            # Billing-ready closed jobs — bold border to signal action needed
            parts.append(
                f"<span style='background:{clr}33;color:{clr};"
                f"border:2px solid {clr};border-radius:6px;"
                f"padding:2px 7px;font-size:0.62rem;font-weight:700;"
                f"white-space:nowrap'>{eye_l} {lbl}</span>"
            )
        elif not is_closed:
            parts.append(
                f"<span style='background:{clr}18;color:{clr};"
                f"border:1px solid {clr}44;border-radius:14px;"
                f"padding:2px 8px;font-size:0.62rem;font-weight:700;"
                f"white-space:nowrap'>{eye_l} {lbl}</span>"
            )

    # ── Supplier / Stock lines (no job_master) ────────────────────────────────
    for r in sup_rows:
        eye   = (r.get("eye_side") or "").strip().upper()
        eye_l = {"R": "RE", "L": "LE"}.get(eye, eye or "—")
        try:
            lp = _tj.loads(r.get("lp_raw") or "{}")
        except Exception:
            lp = {}

        route         = str(lp.get("manufacturing_route") or "").upper()
        supplier_stage = str(lp.get("supplier_stage") or "").upper()
        batch_status  = str(lp.get("batch_status") or "").upper()

        if route in ("VENDOR", "EXTERNAL_LAB") and supplier_stage:
            lbl, clr = _STAGE_META.get(supplier_stage, ("📤 " + supplier_stage, "#64748b"))
            parts.append(
                f"<span style='background:{clr}18;color:{clr};"
                f"border:1px solid {clr}44;border-radius:14px;"
                f"padding:2px 8px;font-size:0.62rem;font-weight:700;"
                f"white-space:nowrap'>{eye_l} {lbl}</span>"
            )
        elif route == "STOCK" and batch_status in ("ALLOCATED", "STOCK_ALLOCATED"):
            parts.append(
                f"<span style='background:#0d948818;color:#0d9488;"
                f"border:1px solid #0d948844;border-radius:14px;"
                f"padding:2px 8px;font-size:0.62rem;font-weight:700;"
                f"white-space:nowrap'>{eye_l} 📦 Stock</span>"
            )

    if parts:
        st.markdown(
            "<div style='display:flex;flex-wrap:wrap;gap:4px;margin:3px 0'>"
            + "".join(parts) + "</div>",
            unsafe_allow_html=True,
        )
