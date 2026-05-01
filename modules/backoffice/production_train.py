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
    "BLANK_ALLOCATED":      ("🎯 Blank",      "#8b5cf6"),
    "PRODUCTION_PICKED":    ("⚙ In Prod",    "#f59e0b"),
    "SURFACING_DONE":       ("✨ Surfacing",  "#a855f7"),
    "HARDCOAT_COMPLETED":   ("🛡 Hardcoat",   "#06b6d4"),
    "ARC_RECEIVED":         ("🔬 ARC",        "#10b981"),
    "COLOURING_COMPLETED":  ("🎨 Colour",     "#ec4899"),
    "PRODUCTION_COMPLETED": ("✅ Done",       "#10b981"),
    "FINAL_QC":             ("🔍 QC",         "#0d9488"),
    "READY_FOR_PACK":       ("📦 Pack",       "#10b981"),
    "READY_FOR_BILLING":    ("🚀 To Bill",    "#059669"),
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


def render_train_sidebar(order_no: str) -> None:
    """
    Renders a compact job pipeline for the sidebar order card.
    Shows one badge per open job_master row.
    """
    rows = _q("""
        SELECT jm.current_stage, jm.is_closed, ol.eye_side
        FROM   job_master jm
        JOIN   order_lines ol ON ol.id = jm.order_line_id
        JOIN   orders o        ON o.id  = ol.order_id
        WHERE  o.order_no = %(ono)s
        ORDER  BY ol.eye_side
    """, {"ono": order_no})

    if not rows:
        return

    parts = []
    for r in rows:
        if r.get("is_closed"):
            continue
        eye   = (r.get("eye_side") or "").strip().upper()
        eye_l = {"R": "RE", "L": "LE"}.get(eye, eye or "—")
        stage = r.get("current_stage") or "JOB_CREATED"
        lbl, clr = _STAGE_META.get(stage, (f"⚙ {stage}", "#64748b"))
        parts.append(
            f"<span style='background:{clr}18;color:{clr};border:1px solid {clr}44;"
            f"border-radius:14px;padding:2px 8px;font-size:0.62rem;font-weight:700;"
            f"white-space:nowrap'>{eye_l} {lbl}</span>"
        )

    if parts:
        st.markdown(
            "<div style='display:flex;flex-wrap:wrap;gap:4px;margin:3px 0'>"
            + "".join(parts) + "</div>",
            unsafe_allow_html=True,
        )
