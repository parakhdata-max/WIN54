"""
order_status_window.py
──────────────────────────────────────────────────────────────────────────────
Renders the "Status" tab (tab3) and the inline train strip shown at the top of
every order detail card.

Public surfaces:
  render_order_status_window(order)    — full tab3 view, READ-ONLY status display
                                         + detected services strip
                                         (stage advancement happens on the
                                         Production page, not here)
  _render_train_inline(order)          — compact strip used above the line items
  render_production_stage_panel(order) — DEPRECATED: read-only shim only,
                                         emits DeprecationWarning. Use
                                         render_order_status_window() instead.

Stage transitions (for reference only — the backoffice no longer triggers them):
  The DB function advance_job_stage(job_id, next_stage, user_id) validates
  transitions via job_stage_transitions table. The Production page is the
  single owner of stage advancement. Per spec:
    READY_FOR_PACK → packing step, NOT billable, does NOT close the job
    READY_TO_BILL  → the only stage that closes the job and opens billing
──────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import datetime
import html
import json
import streamlit as st
from typing import Dict, List, Optional, Tuple


# ── sql helpers ───────────────────────────────────────────────────────────────

def _q(sql: str, params: dict) -> list:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _w(sql: str, params: dict) -> None:
    from modules.sql_adapter import run_write
    run_write(sql, params)


def _live_status(order: dict) -> str:
    try:
        from modules.backoffice.order_status_live import get_live_status
        return get_live_status(order)
    except Exception:
        return str(order.get("status") or "PENDING").upper()


def _meta(status: str) -> dict:
    try:
        from modules.backoffice.order_status_live import get_status_meta
        return get_status_meta(status)
    except Exception:
        return {"label": status, "icon": "•", "color": "#64748b"}


def _operator() -> str:
    try:
        from modules.security.roles import current_user_name
        u = current_user_name()
        return u if isinstance(u, str) else getattr(u, "name", "backoffice")
    except Exception:
        return st.session_state.get("user_name", "backoffice")


# ── ORDER train stations ──────────────────────────────────────────────────────

_TRAIN: List[Dict] = [
    {"key": "PENDING",           "label": "Received",      "icon": "📥", "color": "#3b82f6"},
    {"key": "UNDER_REVIEW",      "label": "Under Review",  "icon": "🔍", "color": "#f59e0b"},
    {"key": "CONFIRMED",         "label": "Confirmed",     "icon": "✅", "color": "#6366f1"},
    {"key": "IN_PRODUCTION",     "label": "Production",    "icon": "⚙️", "color": "#8b5cf6"},
    {"key": "READY",             "label": "Ready",         "icon": "📦", "color": "#10b981"},
    {"key": "READY_FOR_BILLING", "label": "To Bill",       "icon": "🚀", "color": "#0d9488"},
    {"key": "PARTIALLY_BILLED",  "label": "Part Billed",   "icon": "⚡", "color": "#f59e0b"},
    {"key": "CHALLANED",         "label": "Challaned",     "icon": "📋", "color": "#3b82f6"},
    {"key": "BILLED",            "label": "Invoiced",      "icon": "🧾", "color": "#059669"},
    {"key": "DISPATCHED",        "label": "Dispatched",    "icon": "🚚", "color": "#0891b2"},
    {"key": "DELIVERED",         "label": "Delivered",     "icon": "✅", "color": "#10b981"},
    {"key": "CLOSED",            "label": "Closed",        "icon": "🔒", "color": "#334155"},
]
_TRAIN_IDX = {s["key"]: i for i, s in enumerate(_TRAIN)}

# ── JOB stage definitions ─────────────────────────────────────────────────────

_JOB_STAGES: List[Dict] = [
    {"code": "JOB_CREATED",          "label": "Job Created",         "icon": "📋", "color": "#64748b"},
    {"code": "JOB_PRINTED",          "label": "Job Printed",         "icon": "🖨",  "color": "#3b82f6"},
    {"code": "BLANK_ALLOCATED",      "label": "Blank Allocated",     "icon": "🎯", "color": "#8b5cf6"},
    {"code": "PRODUCTION_PICKED",    "label": "In Production",       "icon": "⚙️", "color": "#f59e0b"},
    {"code": "SURFACING_DONE",       "label": "Surfacing Done",      "icon": "✨", "color": "#a855f7"},
    {"code": "HARDCOAT_COMPLETED",   "label": "Hardcoat Done",       "icon": "🛡",  "color": "#06b6d4"},
    {"code": "ARC_RECEIVED",         "label": "ARC Done",            "icon": "🔬", "color": "#0891b2"},
    {"code": "COLOURING_COMPLETED",  "label": "Colouring Done",      "icon": "🎨", "color": "#ec4899"},
    {"code": "PRODUCTION_COMPLETED", "label": "Production Complete", "icon": "✅", "color": "#10b981"},
    {"code": "FINAL_QC",             "label": "Final QC",            "icon": "🔍", "color": "#0d9488"},
    {"code": "READY_FOR_PACK",       "label": "Ready for Pack ✓",    "icon": "📦", "color": "#10b981"},
]
_STAGE_IDX  = {s["code"]: i for i, s in enumerate(_JOB_STAGES)}
_STAGE_META = {s["code"]: s for s in _JOB_STAGES}

# Hardcoded transition map (mirrors job_stage_transitions table as fallback)
_STD_NEXT: Dict[str, List[str]] = {
    "JOB_CREATED":          ["JOB_PRINTED"],
    "JOB_PRINTED":          ["BLANK_ALLOCATED", "PRODUCTION_PICKED"],
    "BLANK_ALLOCATED":      ["PRODUCTION_PICKED"],
    "PRODUCTION_PICKED":    ["SURFACING_DONE", "HARDCOAT_COMPLETED"],
    "SURFACING_DONE":       ["HARDCOAT_COMPLETED"],
    "HARDCOAT_COMPLETED":   ["ARC_RECEIVED", "PRODUCTION_COMPLETED"],
    "ARC_RECEIVED":         ["COLOURING_COMPLETED", "PRODUCTION_COMPLETED", "FINAL_QC"],
    "COLOURING_COMPLETED":  ["PRODUCTION_COMPLETED", "FINAL_QC"],
    "PRODUCTION_COMPLETED": ["FINAL_QC", "READY_FOR_PACK", "READY_TO_BILL"],
    "FINAL_QC":             ["READY_FOR_PACK", "READY_TO_BILL"],
    "READY_FOR_PACK":       ["READY_TO_BILL"],
    "READY_TO_BILL":        [],
}


# ── stage shifting ────────────────────────────────────────────────────────────

def _get_allowed_next(current_stage: str, job_id: str) -> List[str]:
    rows = _q("""
        SELECT to_stage_code FROM job_stage_transitions
        WHERE from_stage_code = %(s)s AND allowed = TRUE
        ORDER BY to_stage_code
    """, {"s": current_stage})
    if rows:
        return [r["to_stage_code"] for r in rows if r.get("to_stage_code")]
    return _STD_NEXT.get(current_stage, [])


def _advance_stage(job_id: str, next_stage: str, order_id: str) -> Tuple[bool, str]:
    """Disabled — stage advancement must happen via the Production page.
    The old fallback logic (closing job at READY_FOR_PACK) conflicts with the
    READY_TO_BILL final gate and is no longer safe to run from Backoffice.
    """
    return False, "Stage advancement is disabled in Backoffice. Use Production page."


# ── production stage panel ────────────────────────────────────────────────────

def render_production_stage_panel(order: dict) -> None:
    """
    READ-ONLY production stage panel.

    Earlier versions of this function rendered stage-advance buttons and could
    close jobs at READY_FOR_PACK. That conflicts with the rule that only
    READY_TO_BILL closes/bills. The function is kept as a deprecated shim
    rather than deleted so any caller that still imports it gets a safe
    read-only render and a one-time deprecation warning instead of an
    AttributeError.

    Stage advancement now happens exclusively from the Production page.
    Backoffice surfaces stage *visibility* through render_order_status_window().
    """
    try:
        import warnings
        warnings.warn(
            "render_production_stage_panel() is deprecated. Stage advancement "
            "lives in production_page.py. Use render_order_status_window() for "
            "read-only visibility.",
            DeprecationWarning,
            stacklevel=2,
        )
    except Exception:
        pass

    order_id = str(order.get("id") or "")
    order_no = str(order.get("order_no") or "")
    _oid     = order_id if len(order_id) == 36 else None

    try:
        jobs = _q("""
            SELECT
                jm.id::text          AS job_id,
                jm.current_stage,
                jm.is_closed,
                jm.total_qty,
                jm.blank_allocated_qty,
                jm.coating_path,
                jm.updated_at,
                ol.eye_side,
                ol.id::text          AS line_id,
                p.product_name
            FROM job_master jm
            JOIN order_lines ol ON ol.id = jm.order_line_id
            JOIN orders o       ON o.id  = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE (%(oid)s IS NOT NULL AND o.id = %(oid)s::uuid
                OR o.order_no = %(ono)s)
            ORDER BY jm.is_closed, ol.eye_side, p.product_name
        """, {"oid": _oid, "ono": order_no})
    except Exception:
        jobs = []

    if not jobs:
        st.info("No job cards yet. Job cards are created on the Production page.")
        return

    st.caption(
        "Stage advancement happens on the Production page. "
        "This view is read-only."
    )

    # Read-only badge per job, alias-normalised.
    try:
        from modules.backoffice.production_page import normalize_stage_alias
    except Exception:
        def normalize_stage_alias(s):  # graceful fallback
            return str(s or "").upper()

    for job in jobs:
        stage_raw = job.get("current_stage") or "JOB_CREATED"
        stage     = normalize_stage_alias(stage_raw)
        eye_l     = (job.get("eye_side") or "").strip().upper()
        prod      = str(job.get("product_name") or "")[:30]
        closed    = bool(job.get("is_closed"))
        if closed:
            color = "#10b981"; status_label = "✅ closed"
        elif stage == "READY_TO_BILL":
            color = "#f59e0b"; status_label = "⏳ ready to bill"
        elif stage == "READY_FOR_PACK":
            color = "#0d9488"; status_label = "📦 packing"
        else:
            color = "#3b82f6"; status_label = stage.replace("_", " ").lower()
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:10px;"
            f"padding:6px 10px;background:{color}11;border:1px solid {color}33;"
            f"border-radius:8px;margin-bottom:4px'>"
            f"<span style='font-weight:700;color:{color}'>{eye_l or '·'}</span>"
            f"<span style='flex:1;color:#475569;font-size:0.85rem'>{prod}</span>"
            f"<span style='color:{color};font-size:0.75rem;font-weight:600'>"
            f"{status_label}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )



# ── helpers ───────────────────────────────────────────────────────────────────

def _get_ts_map(order_no: str) -> dict:
    try:
        rows = _q("""
            SELECT h.to_status,
                   MIN(h.changed_at) AS changed_at,
                   (array_agg(h.changed_by_name ORDER BY h.changed_at))[1] AS by
            FROM order_status_history h
            JOIN orders o ON o.id = h.order_id
            WHERE o.order_no = %(ono)s
            GROUP BY h.to_status
        """, {"ono": order_no})
        _ALIAS = {"PENDING_VALIDATION": "PENDING", "PROVISIONAL": "PENDING",
                  "ORDER_SAVED": "PENDING"}
        result = {}
        for r in rows:
            raw = (r.get("to_status") or "").upper()
            sts = _ALIAS.get(raw, raw)
            ts  = str(r.get("changed_at") or "")[:16].replace("T", " ")
            by  = r.get("by") or "system"
            if sts and sts not in result:
                result[sts] = {"ts": ts, "by": by}
        return result
    except Exception:
        return {}


def _get_job_rows_inline(order_id: str, order_no: str) -> list:
    _oid = order_id if (order_id and len(order_id) == 36) else None
    return _q("""
        SELECT jm.id::text AS job_id, jm.current_stage, jm.is_closed,
               ol.eye_side, p.product_name
        FROM job_master jm
        JOIN order_lines ol ON ol.id = jm.order_line_id
        JOIN orders o       ON o.id  = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE (%(oid)s IS NOT NULL AND o.id = %(oid)s::uuid
            OR o.order_no = %(ono)s)
          AND NOT COALESCE(jm.is_closed, FALSE)
        ORDER BY ol.eye_side, p.product_name
    """, {"oid": _oid, "ono": order_no})


def _render_structured_audit_log(order_id: str) -> None:
    if not order_id:
        return
    try:
        from modules.backoffice.audit_logger import get_audit_trail
        rows = get_audit_trail(order_id, limit=50) or []
    except Exception:
        rows = []

    with st.expander("Audit Log", expanded=False):
        st.caption("Structured backoffice actions recorded for this order.")
        if not rows:
            st.info("No structured audit_log entries recorded for this order yet.")
            return

        for row in rows:
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {"detail": payload}
            if not isinstance(payload, dict):
                payload = {"detail": str(payload)}

            event = html.escape(str(row.get("event") or "audit_event"))
            entity = html.escape(str(row.get("entity") or ""))
            user = html.escape(str(row.get("user_id") or "system"))
            at = html.escape(str(row.get("created_at") or "")[:16].replace("T", " "))

            bits = []
            for key in (
                "action", "order_no", "status", "from_status", "to_status",
                "product", "eye_side", "old_value", "new_value", "amount",
                "ref_no", "reason", "refund_amount", "refund_mode",
            ):
                val = payload.get(key)
                if val not in (None, ""):
                    bits.append(f"{key.replace('_', ' ').title()}: {html.escape(str(val))}")
            detail = " | ".join(bits) if bits else html.escape(json.dumps(payload, default=str))

            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #334155;"
                f"border-radius:8px;padding:8px 12px;margin:4px 0'>"
                f"<div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>"
                f"<span style='color:#93c5fd;font-weight:800;font-size:0.78rem'>{event}</span>"
                f"<span style='color:#64748b;font-size:0.68rem'>{entity}</span>"
                f"<span style='margin-left:auto;color:#94a3b8;font-size:0.68rem'>{at}</span>"
                f"</div>"
                f"<div style='color:#cbd5e1;font-size:0.74rem;margin-top:4px'>{detail}</div>"
                f"<div style='color:#64748b;font-size:0.65rem;margin-top:3px'>by {user}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Public: compact inline train strip
# ─────────────────────────────────────────────────────────────────────────────

def _render_train_inline(order: dict) -> None:
    status   = _live_status(order)
    cur_idx  = _TRAIN_IDX.get(status, 0)
    order_no = str(order.get("order_no") or "")
    order_id = str(order.get("id") or "")
    visible  = [s for s in _TRAIN if s["key"] not in ("CLOSED", "CANCELLED")]

    dots = []
    for s in visible:
        idx  = _TRAIN_IDX[s["key"]]
        done = idx < cur_idx
        curr = idx == cur_idx
        if done:
            style = (f"background:{s['color']};color:#fff;border:2px solid {s['color']};"
                     "border-radius:50%;width:28px;height:28px;display:inline-flex;"
                     "align-items:center;justify-content:center;font-size:0.7rem;"
                     "font-weight:700;flex-shrink:0")
            dots.append(f"<div title='{s['label']}' style='{style}'>{s['icon']}</div>")
        elif curr:
            style = (f"background:{s['color']}22;color:{s['color']};"
                     f"border:2.5px solid {s['color']};border-radius:50%;"
                     "width:32px;height:32px;display:inline-flex;"
                     "align-items:center;justify-content:center;font-size:0.85rem;"
                     f"font-weight:700;flex-shrink:0;box-shadow:0 0 0 4px {s['color']}33")
            label_html = (f"<span style='color:{s['color']};font-size:0.65rem;"
                          f"font-weight:700;white-space:nowrap'>{s['label']}</span>")
            dots.append(
                f"<div style='display:flex;flex-direction:column;align-items:center;gap:2px'>"
                f"<div title='{s['label']}' style='{style}'>{s['icon']}</div>"
                f"{label_html}</div>"
            )
        else:
            style = ("background:#1e293b;color:#475569;border:1.5px solid #334155;"
                     "border-radius:50%;width:24px;height:24px;display:inline-flex;"
                     "align-items:center;justify-content:center;font-size:0.65rem;flex-shrink:0")
            dots.append(f"<div title='{s['label']}' style='{style}'>{s['icon']}</div>")

        if s != visible[-1]:
            conn_col = s["color"] if done else "#1e293b"
            dots.append(
                f"<div style='flex:1;height:2px;background:{conn_col};"
                "min-width:8px;max-width:32px;margin:auto 2px'></div>"
            )

    st.markdown(
        "<div style='display:flex;align-items:center;gap:2px;"
        "overflow-x:auto;padding:6px 2px 10px;scrollbar-width:none'>"
        + "".join(dots) + "</div>",
        unsafe_allow_html=True,
    )

    # ── Partial billing sub-summary ─────────────────────────────────────────
    if status == "PARTIALLY_BILLED" and (order_id or order_no):
        try:
            from modules.sql_adapter import run_query as _rq_ps
            _ps_rows = _rq_ps("""
                SELECT
                    ol.eye_side,
                    p.product_name,
                    EXISTS (
                        SELECT 1 FROM challan_lines cl
                        JOIN challans c ON c.id = cl.challan_id
                        WHERE cl.order_line_id = ol.id
                          AND c.status NOT IN ('CANCELLED','VOID','DELETED')
                    ) AS has_challan,
                    (SELECT c2.challan_no FROM challan_lines cl2
                     JOIN challans c2 ON c2.id = cl2.challan_id
                     WHERE cl2.order_line_id = ol.id
                       AND c2.status NOT IN ('CANCELLED','VOID','DELETED')
                     LIMIT 1) AS challan_no,
                    EXISTS (
                        SELECT 1 FROM challan_lines cl3
                        JOIN challans c3 ON c3.id = cl3.challan_id
                        JOIN invoices i ON i.challan_id = c3.id
                        WHERE cl3.order_line_id = ol.id
                          AND i.status NOT IN ('CANCELLED','VOID')
                    ) AS has_invoice
                FROM order_lines ol
                JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
                  AND COALESCE(ol.is_service_line, FALSE) = FALSE
                ORDER BY CASE WHEN ol.eye_side='R' THEN 0 WHEN ol.eye_side='L' THEN 1 ELSE 2 END
            """, {"oid": order_id}) or []

            if _ps_rows:
                _parts = []
                for _pr in _ps_rows:
                    _eye = str(_pr.get("eye_side","")).upper()
                    _eye_s = "R" if _eye in ("R","RIGHT") else "L" if _eye in ("L","LEFT") else _eye[:1]
                    _eye_c = "#ef4444" if _eye_s == "R" else "#60a5fa"
                    _pn  = str(_pr.get("product_name","")).split(" | ")[0][:18]
                    _cno = _pr.get("challan_no","")
                    if _pr.get("has_invoice"):
                        _doc = f"<span style='color:#22c55e;font-size:0.65rem'>🧾 {_cno}</span>"
                    elif _pr.get("has_challan"):
                        _doc = f"<span style='color:#3b82f6;font-size:0.65rem'>📋 {_cno}</span>"
                    else:
                        _doc = "<span style='color:#f59e0b;font-size:0.65rem'>⏳ Pending</span>"
                    _parts.append(
                        f"<span style='color:{_eye_c};font-weight:700;font-size:0.7rem'>{_eye_s}</span>"
                        f"<span style='color:#94a3b8;font-size:0.68rem'> {_pn}</span> {_doc}"
                    )
                _sep = "<span style='color:#1e293b;margin:0 4px'>·</span>"
                st.markdown(
                    "<div style='display:flex;flex-wrap:wrap;gap:8px;align-items:center;"
                    "padding:4px 8px;background:#0f172a;border-radius:6px;"
                    "border:1px solid #1e293b;margin-bottom:4px'>"
                    + _sep.join(_parts)
                    + "</div>",
                    unsafe_allow_html=True
                )
        except Exception:
            pass

    # Job pipeline badges (read-only — go to tab3 for buttons)
    jobs = _get_job_rows_inline(order_id, order_no)
    if jobs:
        job_parts = []
        for j in jobs:
            eye   = (j.get("eye_side") or "").strip().upper()
            eye_l = {"R": "RE", "L": "LE"}.get(eye, eye or "—")
            stage = j.get("current_stage") or "JOB_CREATED"
            sm    = _STAGE_META.get(stage, {"label": stage, "icon": "•", "color": "#64748b"})
            job_parts.append(
                f"<span style='background:{sm['color']}18;color:{sm['color']};"
                f"border:1px solid {sm['color']}44;border-radius:20px;"
                f"padding:2px 10px;font-size:0.68rem;font-weight:700;white-space:nowrap'>"
                f"{eye_l} {sm['icon']} {sm['label']}</span>"
            )
        st.markdown(
            "<div style='display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 6px;"
            "align-items:center'>"
            "<span style='color:#475569;font-size:0.62rem'>🔬 Lab:</span>"
            + "".join(job_parts)
            + "</div>",
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public: full status tab
# ─────────────────────────────────────────────────────────────────────────────

def render_order_status_window(order: dict) -> None:
    order_no = str(order.get("order_no") or "")
    order_id = str(order.get("id") or "")
    status   = _live_status(order)
    meta     = _meta(status)
    cur_idx  = _TRAIN_IDX.get(status, 0)

    # ── Header ───────────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='background:#0f172a;border:1px solid {meta['color']}44;"
        f"border-radius:12px;padding:14px 20px;margin-bottom:16px;"
        f"display:flex;align-items:center;gap:14px'>"
        f"<span style='font-size:2rem'>{meta['icon']}</span>"
        f"<div>"
        f"<div style='color:{meta['color']};font-size:1.15rem;font-weight:800'>"
        f"{meta['label']}</div>"
        f"<div style='color:#475569;font-size:0.75rem;margin-top:2px'>"
        f"Order {order_no} · Live status</div>"
        f"</div></div>",
        unsafe_allow_html=True,
    )

    # ── Detected Services Strip (read-only — production handles advancement) ──
    # Shows at-a-glance whether colouring/fitting/frame are part of this order
    # so backoffice staff can answer customer questions without bouncing to
    # the production page.
    try:
        from modules.backoffice.production_page import detect_production_services
        _svc = detect_production_services(order_id)
    except Exception:
        _svc = {"colouring": False, "fitting": False, "frame_name": "",
                "frame_source": "NO_FRAME", "fitting_vendor": "", "fitting_note": ""}

    def _svc_pill(label, on, color_on="#10b981", color_off="#475569"):
        bg = color_on if on else color_off
        ic = "✓" if on else "—"
        return (f"<span style='background:{bg}22;border:1px solid {bg}66;"
                f"color:{bg};padding:4px 10px;border-radius:14px;font-size:0.7rem;"
                f"font-weight:700;white-space:nowrap'>{ic} {label}</span>")

    _frame_label_map = {
        "SOLD_WITH_ORDER": "Frame: sold with order",
        "CUSTOMER_FRAME":  "Frame: customer's own",
        "NO_FRAME":        "Frame: none",
    }
    _frame_pill_label = _frame_label_map.get(_svc.get("frame_source", "NO_FRAME"),
                                             "Frame: —")
    if _svc.get("frame_name"):
        _frame_pill_label += f" · {_svc['frame_name']}"

    # Next stage hint (best-effort) — only meaningful when in production
    _next_hint = ""
    try:
        from modules.backoffice.production_page import (
            build_optical_stage_flow, normalize_stage_alias
        )
        # Derive coating from any line if available; otherwise leave blank.
        _coat_for_hint = ""
        for _l in (order.get("inhouse_lines") or []) + (order.get("lines") or []):
            _lp_h = _l.get("lens_params") or {}
            if isinstance(_lp_h, dict):
                _coat_for_hint = (str(_l.get("coating") or _lp_h.get("coating") or "")).strip()
                if _coat_for_hint:
                    break
        if _coat_for_hint:
            _flow = build_optical_stage_flow(
                _coat_for_hint, _svc.get("colouring", False), _svc.get("fitting", False)
            )
            _cur_stage = ""
            for _l in (order.get("inhouse_lines") or []):
                _cur_stage = normalize_stage_alias(_l.get("current_stage") or _l.get("lab_stage") or "")
                if _cur_stage in _flow:
                    break
            if _cur_stage and _cur_stage in _flow:
                _idx = _flow.index(_cur_stage)
                if _idx + 1 < len(_flow):
                    _next_hint = _flow[_idx + 1]
    except Exception:
        _next_hint = ""

    _pills = [
        _svc_pill("Colouring", _svc.get("colouring", False)),
        _svc_pill("Fitting",   _svc.get("fitting", False)),
        _svc_pill(_frame_pill_label,
                  _svc.get("frame_source") in ("SOLD_WITH_ORDER","CUSTOMER_FRAME"),
                  color_on="#3b82f6"),
    ]
    if _svc.get("fitting_vendor"):
        _pills.append(_svc_pill(f"Vendor: {_svc['fitting_vendor']}", True, color_on="#8b5cf6"))
    if _next_hint:
        _pills.append(_svc_pill(f"Next: {_next_hint}", True, color_on="#f59e0b"))

    st.markdown(
        "<div style='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px'>"
        + "".join(_pills) +
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Two-column layout ─────────────────────────────────────────────────────
    col_tl, col_prod = st.columns([1, 1])

    with col_tl:
        st.markdown("#### 📅 Order Timeline")
        ts_map = _get_ts_map(order_no)
        if "PENDING" not in ts_map:
            _ca = order.get("created_at") or order.get("order_date") or ""
            if _ca:
                ts_map["PENDING"] = {"ts": str(_ca)[:16].replace("T", " "), "by": "system"}

        # Strip stale BILLED if no billing docs
        if "BILLED" in ts_map:
            _oid_t = order_id if len(order_id) == 36 else "__none__"
            docs = _q("""
                SELECT 1 FROM challans
                WHERE (order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR order_ids::text[] @> ARRAY[%(ono)s::text])
                  AND status NOT IN ('CANCELLED','VOID')
                UNION ALL SELECT 1 FROM invoices
                WHERE (order_ids::text[] @> ARRAY[%(oid)s::text]
                    OR order_ids::text[] @> ARRAY[%(ono)s::text])
                  AND status NOT IN ('CANCELLED','VOID')
                LIMIT 1
            """, {"oid": _oid_t, "ono": order_no})
            if not docs:
                ts_map.pop("BILLED", None)

        for s in _TRAIN:
            k   = s["key"]
            idx = _TRAIN_IDX[k]
            if idx > cur_idx and k not in ts_map:
                continue
            rec  = ts_map.get(k)
            done = idx <= cur_idx or bool(rec)
            curr = k == status
            bg   = s["color"] + "18" if done else "#0f172a"
            brd  = s["color"]        if done else "#1e293b"
            tcol = s["color"]        if done else "#334155"
            ts_str = rec["ts"] if rec else "—"
            by_str = f" · by {rec['by']}" if (rec and rec.get("by") and rec["by"] != "system") else ""
            curr_badge = (
                f" <span style='background:{s['color']};color:#fff;font-size:0.58rem;"
                f"font-weight:700;padding:1px 7px;border-radius:10px'>NOW</span>"
                if curr else ""
            )
            st.markdown(
                f"<div style='background:{bg};border:1px solid {brd};border-radius:8px;"
                f"padding:8px 14px;margin:3px 0;display:flex;align-items:center;gap:10px'>"
                f"<span style='font-size:1.1rem'>{s['icon']}</span>"
                f"<div style='flex:1'>"
                f"<div style='color:{tcol};font-weight:700;font-size:0.8rem'>"
                f"{s['label']}{curr_badge}</div>"
                f"<div style='color:#475569;font-size:0.65rem;margin-top:1px'>"
                f"{ts_str}{by_str}</div>"
                f"</div></div>",
                unsafe_allow_html=True,
            )

    with col_prod:
        st.markdown("#### ⚙️ Production Stage  —  Current Status")
        st.caption("View-only. Advance stages from the Production page.")
        # Show job/supplier stage badges via production train
        try:
            from modules.backoffice.production_train import render_train_sidebar
            render_train_sidebar(str(order.get("order_no") or ""))
        except Exception:
            pass
        # Show detailed job stage per line
        try:
            from modules.sql_adapter import run_query as _rq_sw
            _STAGE_META_SW = {
                "JOB_CREATED":       ("🖨 Created",      "#64748b"),
                "PRINTED":           ("🖨 Printed",       "#3b82f6"),
                "JOB_PRINTED":       ("🖨 Printed",       "#3b82f6"),
                "BLANK_ALLOCATED":   ("🎯 Blank",         "#8b5cf6"),
                "PRODUCTION_PICKED": ("⚙ In Prod",       "#f59e0b"),
                "PRODUCTION_DONE":   ("✨ Prod Done",     "#a855f7"),
                "HARDCOAT_PICKED":   ("🛡 Hardcoat",      "#f59e0b"),
                "HARDCOAT_DONE":     ("🛡 HC Done",       "#eab308"),
                "COLOURING_PICKED":  ("🎨 Colouring",     "#ec4899"),
                "COLOURING_DONE":    ("🎨 Colour Done",   "#db2777"),
                "ARC_SENT":          ("🔬 ARC Sent",      "#06b6d4"),
                "ARC_RECEIVED":      ("🔬 ARC Rcvd",      "#10b981"),
                "INSPECTION":        ("🔍 Inspect",       "#ef4444"),
                "FINAL_QC":          ("🔍 Final QC",      "#0d9488"),
                "READY_FOR_PACK":    ("📦 Pack Ready",    "#0d9488"),
                "READY_TO_BILL":     ("💰 Ready→Bill",    "#059669"),
                "FITTING_DONE":      ("✅ Fit Done",      "#4c1d95"),
                "REJECTED":          ("🚫 Rejected",      "#dc2626"),
            }
            _jm_rows = _rq_sw("""
                SELECT jm.current_stage, jm.is_closed,
                       ol.eye_side,
                       COALESCE(ol.lens_params->>'manufacturing_route','') AS route,
                       COALESCE(ol.lens_params->>'supplier_stage','') AS supplier_stage
                FROM job_master jm
                JOIN order_lines ol ON ol.id = jm.order_line_id
                JOIN orders o ON o.id = ol.order_id
                WHERE o.order_no = %(ono)s
                ORDER BY ol.eye_side
            """, {"ono": order.get("order_no","")})

            if _jm_rows:
                for _jrow in _jm_rows:
                    _stg   = str(_jrow.get("current_stage") or "JOB_CREATED").upper()
                    _eye   = str(_jrow.get("eye_side") or "").upper()
                    _eye_l = {"R":"RE","L":"LE"}.get(_eye, _eye or "—")
                    _lbl, _clr = _STAGE_META_SW.get(_stg, (f"⚙ {_stg}", "#64748b"))
                    _closed = _jrow.get("is_closed", False)
                    _bdr   = f"border:2px solid {_clr}" if _closed else f"border:1px solid {_clr}44"
                    st.markdown(
                        f"<div style='background:{_clr}12;{_bdr};border-radius:6px;"
                        f"padding:6px 12px;margin:4px 0;display:flex;align-items:center;gap:10px'>"
                        f"<span style='color:{_clr};font-weight:700;font-size:0.75rem'>{_eye_l}</span>"
                        f"<span style='color:#e2e8f0;font-size:0.8rem'>{_lbl}</span>"
                        + (f"<span style='color:#22c55e;font-size:0.65rem;margin-left:auto'>"
                           f"✅ Billing Ready</span>" if _closed else "")
                        + "</div>",
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No job cards created yet for this order.")
        except Exception as _sw_e:
            st.caption(f"Stage load: {_sw_e}")

    # ── Billing documents ─────────────────────────────────────────────────────
    _oid_q = order_id if len(order_id) == 36 else "__none__"
    challans = _q("""
        SELECT c.id::text AS challan_id, c.challan_no, c.status,
               c.grand_total, c.created_at, c.is_partial_billing,
               (SELECT COUNT(*) FROM challan_lines cl
                WHERE cl.challan_id=c.id
                  AND NOT COALESCE(cl.is_deleted,FALSE)) AS line_count,
               (SELECT i.invoice_no FROM invoices i
                WHERE i.challan_id=c.id
                  AND NOT COALESCE(i.is_deleted,FALSE)
                  AND i.status NOT IN ('CANCELLED','VOID')
                LIMIT 1) AS invoice_no
        FROM challans c
        WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text]
            OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
          AND NOT COALESCE(c.is_deleted, FALSE)
          AND c.status NOT IN ('CANCELLED','VOID')
        ORDER BY c.created_at DESC LIMIT 10
    """, {"oid": _oid_q, "ono": order_no})

    invoices = _q("""
        SELECT i.invoice_no, i.status, i.grand_total,
               i.payment_status, i.created_at, i.is_partial_billing
        FROM invoices i
        WHERE (i.order_ids::text[] @> ARRAY[%(oid)s::text]
            OR i.order_ids::text[] @> ARRAY[%(ono)s::text])
          AND NOT COALESCE(i.is_deleted, FALSE)
          AND i.status NOT IN ('CANCELLED','VOID')
        ORDER BY i.created_at DESC LIMIT 10
    """, {"oid": _oid_q, "ono": order_no})

    if challans or invoices:
        st.markdown("---")
        st.markdown("#### 🧾 Billing Documents")
        for c in challans:
            partial_badge = (
                " <span style='font-size:0.6rem;background:#f59e0b22;color:#fbbf24;"
                "border:1px solid #f59e0b55;border-radius:10px;padding:1px 7px'>"
                "PARTIAL</span>"
            ) if c.get("is_partial_billing") else ""
            inv_badge = (
                f" <span style='background:#05966922;color:#34d399;"
                f"border:1px solid #05966955;border-radius:10px;padding:1px 7px;"
                f"font-size:0.6rem'>INV {c['invoice_no']}</span>"
            ) if c.get("invoice_no") else ""
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #0d948866;"
                f"border-radius:8px;padding:9px 14px;margin:3px 0;"
                f"display:flex;align-items:center;gap:10px'>"
                f"<span>📋</span>"
                f"<div style='flex:1'>"
                f"<div style='color:#5eead4;font-weight:700;font-size:0.82rem'>"
                f"{c.get('challan_no','—')}{partial_badge}{inv_badge}</div>"
                f"<div style='color:#475569;font-size:0.65rem'>"
                f"{int(c.get('line_count') or 0)} line(s) · "
                f"{str(c.get('created_at',''))[:10]}</div></div>"
                f"<span style='color:#10b981;font-weight:700'>"
                f"₹{float(c.get('grand_total') or 0):,.2f}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        for inv in invoices:
            pstat = (inv.get("payment_status") or "UNPAID").upper()
            pclr  = "#10b981" if pstat == "PAID" else "#f59e0b" if pstat == "PARTIAL" else "#ef4444"
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #05966966;"
                f"border-radius:8px;padding:9px 14px;margin:3px 0;"
                f"display:flex;align-items:center;gap:10px'>"
                f"<span>🧾</span>"
                f"<div style='flex:1'>"
                f"<div style='color:#34d399;font-weight:700;font-size:0.82rem'>"
                f"{inv.get('invoice_no','—')}</div>"
                f"<div style='color:#475569;font-size:0.65rem'>"
                f"{str(inv.get('created_at',''))[:10]}</div></div>"
                f"<div style='text-align:right'>"
                f"<div style='color:#10b981;font-weight:700'>"
                f"₹{float(inv.get('grand_total') or 0):,.2f}</div>"
                f"<div style='color:{pclr};font-size:0.65rem;font-weight:700'>"
                f"{pstat}</div></div></div>",
                unsafe_allow_html=True,
            )

    st.markdown("---")
    _render_structured_audit_log(_oid_q if _oid_q != "__none__" else order_id)
