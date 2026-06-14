"""
modules/backoffice/production_shared.py
========================================
Shared helpers used across all production pipeline sub-modules.

Imported by:
  stock_pipeline.py
  supplier_pipeline.py
  inhouse_pipeline.py
  production_page.py (directly)

IMPORTANT: Add new shared helpers here, not in individual pipeline files.
"""
from __future__ import annotations
import datetime
import hashlib
import json as _shared_json
import logging
import re as _re
import uuid as _uuid
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

log = logging.getLogger(__name__)


def _production_card_key_suffix(group_key: Any) -> str:
    """Stable Streamlit key suffix for production cards.

    A single customer order can now render several production_refs. The old
    first-8-character order id suffix collides for those sibling cards.
    """
    raw = str(group_key or "")
    safe = _re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")[-32:] or "group"
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
    return f"{safe}_{digest}"


def _parse_lp_safe(line: dict) -> dict:
    """Parse lens_params from any line dict. Safe — never raises. Cached on line."""
    if "_lp" in line:
        return line["_lp"] if isinstance(line["_lp"], dict) else {}
    lp = line.get("lens_params") or {}
    if isinstance(lp, str):
        try:  lp = _shared_json.loads(lp)
        except Exception as e:
            log.debug("Could not parse lens_params: %s", e)
            lp = {}
    result = lp if isinstance(lp, dict) else {}
    line["_lp"] = result  # cache for next call
    return result



def _fmt_pwr_line(line: dict) -> str:
    """Format power string from a line dict. Uses columns first, falls back to lens_params."""
    if "_pw" in line:
        return line["_pw"]
    parts = []
    lp = _parse_lp_safe(line)
    for col, lp_key, fmt in [
        ("sph",       "sph",       lambda v: f"SPH {float(v):+.2f}"),
        ("cyl",       "cyl",       lambda v: f"CYL {float(v):+.2f}" if abs(float(v)) > 0.01 else ""),
        ("axis",      "axis",      lambda v: f"AX {int(float(v))}°" if int(float(v)) != 0 else ""),
        ("add_power", "add_power", lambda v: f"ADD {float(v):+.2f}" if float(v) > 0 else ""),
    ]:
        val = line.get(col) if line.get(col) is not None else lp.get(lp_key)
        if val is not None and str(val) not in ("", "None", "0", "0.0"):
            try:
                s = fmt(val)
                if s: parts.append(s)
            except (ValueError, TypeError):
                pass
    result = "  ".join(parts)
    line["_pw"] = result  # cache
    return result



def _init_production_state():
    defaults = {
        "prod_view_mode":        "list",
        "prod_selected_order":   None,
        "prod_assign_order_no":  None,   # order_no for assignment workspace
        "prod_orders":           [],
        "prod_orders_loaded":    False,
        "prod_date_from":        datetime.date.today() - datetime.timedelta(days=30),
        "prod_date_to":          datetime.date.today(),
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ==================================================
# DB HELPERS
# ==================================================


def _q(sql: str, params: dict) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        st.error(f"DB error: {e}")
        return []



def _load_production_orders(date_from: datetime.date, date_to: datetime.date) -> List[Dict]:
    """Load orders with open job cards in date range."""
    rows = _q("""
        SELECT DISTINCT
            o.id,
            o.order_no,
            o.patient_name,
            o.party_name,
            o.status,
            o.created_at,
            COUNT(jm.id)                                    AS total_jobs,
            SUM(CASE WHEN jm.is_closed THEN 1 ELSE 0 END)  AS closed_jobs,
            (
                SELECT jm2.current_stage
                FROM job_master jm2
                JOIN order_lines ol2 ON ol2.id = jm2.order_line_id
                LEFT JOIN job_stage_master jsm2 ON jsm2.stage_code = jm2.current_stage
                WHERE ol2.order_id = o.id
                  AND COALESCE(ol2.is_deleted, FALSE) = FALSE
                ORDER BY COALESCE(jsm2.sequence_order, 0) DESC
                LIMIT 1
            ) AS latest_stage
        FROM orders o
        JOIN order_lines ol  ON ol.order_id  = o.id
        JOIN job_master  jm  ON jm.order_line_id = ol.id
        WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
          AND o.status NOT IN ('CLOSED', 'CANCELLED')
          AND DATE(o.created_at) >= %(df)s
          AND DATE(o.created_at) <= %(dt)s
        GROUP BY o.id, o.order_no, o.patient_name, o.party_name, o.status, o.created_at
        ORDER BY o.created_at DESC
        LIMIT 200
    """, {"df": date_from, "dt": date_to})

    return [
        {
            "order_id":      str(r["id"]),
            "order_no":      r["order_no"],
            "patient_name":  r.get("patient_name") or r.get("party_name") or "—",
            "status":        r.get("status", ""),
            "created_at":    r.get("created_at"),
            "total_jobs":    int(r.get("total_jobs") or 0),
            "closed_jobs":   int(r.get("closed_jobs") or 0),
            "open_jobs":     int(r.get("total_jobs") or 0) - int(r.get("closed_jobs") or 0),
            "current_stage": r.get("latest_stage") or "—",
        }
        for r in rows
    ]



def _load_stage_timeline(order_no: str) -> List[Dict]:
    """Load full stage event history for an order (both eyes)."""
    return _q("""
        SELECT
            jse.stage_code,
            jsm.stage_name,
            jsm.department,
            jsm.sequence_order,
            ol.eye_side,
            jse.created_at,
            jse.remarks,
            jse.performed_by
        FROM job_stage_events jse
        JOIN job_master jm       ON jm.id = jse.job_id
        JOIN order_lines ol      ON ol.id = jm.order_line_id
        JOIN orders o            ON o.id  = ol.order_id
        LEFT JOIN job_stage_master jsm ON jsm.stage_code = jse.stage_code
        WHERE o.order_no = %(ono)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        ORDER BY jse.created_at ASC
    """, {"ono": order_no})



def _fetch_order_for_panel(order_no: str) -> Optional[Dict]:
    """Fetch full order dict needed to render production_panel + job card forms."""
    rows = _q("""
        SELECT o.id, o.order_no, o.patient_name, o.party_name, o.status,
               COALESCE(o.patient_mobile, p.mobile, '') AS mobile,
               COALESCE(p.mobile, '') AS party_mobile
        FROM orders o
        LEFT JOIN parties p ON p.id = o.party_id
        WHERE o.order_no = %(ono)s
        LIMIT 1
    """, {"ono": order_no})
    if not rows:
        return None
    r = rows[0]

    # Fetch ALL fields needed by render_surfacing_job_card
    line_rows = _q("""
        SELECT
            ol.id, ol.eye_side, ol.lens_params, ol.boxing_params,
            ol.product_id,
            COALESCE(ol.sph, 0)       AS sph,
            COALESCE(ol.cyl, 0)       AS cyl,
            COALESCE(ol.axis, 0)      AS axis,
            COALESCE(ol.add_power, 0) AS add_power,
            COALESCE(ol.billing_qty, ol.quantity, 1) AS billing_qty,
            COALESCE(ol.quantity, 1)  AS quantity,
            COALESCE(ol.unit_price, 0) AS unit_price,
            ol.batch_status,
            ol.allocated_qty,
            ol.suggested_allocation,
            p.product_name,
            p.brand,
            COALESCE(p.main_group, '') AS main_group,
            COALESCE(p.category, '')   AS category,
            COALESCE(p.lens_category, '') AS lens_category,
            COALESCE(p.unit, 'PCS')    AS unit,
            COALESCE(p.box_size, 1)    AS box_size,
            COALESCE(p.gst_percent, 0) AS gst_percent
        FROM order_lines ol
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE ol.order_id = %(oid)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND COALESCE(ol.is_service_line, FALSE) = FALSE
        ORDER BY ol.eye_side
    """, {"oid": r["id"]})

    import json as _json
    _lines = []
    for lr in line_rows:
        lp = lr.get("lens_params") or {}
        if isinstance(lp, str):
            try: lp = _json.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}
        bp = lr.get("boxing_params") or {}
        if isinstance(bp, str):
            try: bp = _json.loads(bp)
            except Exception as e:
                log.debug("Could not parse boxing_params: %s", e)
                bp = {}
        sa = lr.get("suggested_allocation")
        if isinstance(sa, str):
            try: sa = _json.loads(sa)
            except Exception as e:
                log.debug("Could not parse suggested_allocation: %s", e)
                sa = []
        _lines.append({
            "id":            str(lr["id"]),
            "line_id":       str(lr["id"]),
            "eye_side":      lr.get("eye_side") or "",
            "product_id":    str(lr.get("product_id")) if lr.get("product_id") else None,
            "product_name":  lr.get("product_name", "Unknown"),
            "brand":         lr.get("brand", ""),
            "main_group":    lr.get("main_group", ""),
            "category":      lr.get("category", ""),
            "lens_category": lr.get("lens_category", ""),
            "unit":          lr.get("unit", "PCS"),
            "box_size":      int(lr.get("box_size") or 1),
            "sph":           float(lr.get("sph") or 0),
            "cyl":           float(lr.get("cyl") or 0),
            "axis":          int(lr.get("axis") or 0),
            "add_power":     float(lr.get("add_power") or 0) if float(lr.get("add_power") or 0) != 0.0 else None,
            "billing_qty":   int(lr.get("billing_qty") or 1),
            "quantity":      int(lr.get("quantity") or 1),
            "unit_price":    float(lr.get("unit_price") or 0),
            "gst_percent":   float(lr.get("gst_percent") or 0),
            "batch_status":  lr.get("batch_status") or "",
            "allocated_qty": int(lr.get("allocated_qty") or 0),
            "suggested_allocation": sa or [],
            "lens_params":   lp,
            "boxing_params": bp,
            "surfacing_data": lp.get("surfacing_data") or None,
        })

    return {
        "id":           str(r["id"]),
        "order_no":     r["order_no"],
        "patient_name": r.get("patient_name") or r.get("party_name") or "—",
        "mobile":       r.get("mobile") or r.get("party_mobile") or "",
        "status":       r.get("status", ""),
        "lines":        _lines,
    }


# ==================================================
# STAGE COLOUR MAP
# ==================================================

_STAGE_COLORS = {
    # ── Must match job_stage_master.stage_code exactly ──
    "JOB_CREATED":       "#6b7280",
    "PRINTED":           "#3b82f6",   # was JOB_PRINTED
    "PRODUCTION_PICKED": "#8b5cf6",
    "PRODUCTION_DONE":   "#a855f7",   # was PRODUCTION_COMPLETED
    "INSPECTION":        "#ef4444",
    "HARDCOAT_PICKED":   "#f59e0b",
    "HARDCOAT_DONE":     "#eab308",   # was HARDCOAT_COMPLETED
    "COLOURING_PICKED":  "#ec4899",
    "COLOURING_DONE":    "#db2777",   # was COLOURING_COMPLETED
    "ARC_SENT":          "#06b6d4",   # was SENT_TO_ARC
    "ARC_RECEIVED":      "#0891b2",
    "FINAL_QC":          "#f97316",
    "READY_FOR_PACK":    "#10b981",
    "FITTING_PENDING":   "#7c3aed",
    "FITTING_SENT":      "#6d28d9",
    "FITTING_RECEIVED":  "#5b21b6",
    "FITTING_DONE":      "#4c1d95",
    "DISPATCHED":        "#059669",
    "DELIVERED":         "#10b981",
    "READY_FOR_PACK":    "#0d9488",
    "READY_TO_BILL":     "#16a34a",
    "CHALLANED":         "#0284c7",
    "INVOICED":          "#22c55e",
}

try:
    from modules.backoffice.order_status_live import STATUS_META as _OSL_META
    _ORDER_STATUS_COLORS = {k: v["color"] for k, v in _OSL_META.items()}
except Exception as _e:
    _ORDER_STATUS_COLORS = {
        "PENDING": "#64748b", "CONFIRMED": "#3b82f6",
        "IN_PRODUCTION": "#8b5cf6", "READY": "#10b981",
        "BILLED": "#059669", "DISPATCHED": "#0891b2", "DELIVERED": "#10b981",
    }


# ==================================================
# PRODUCTION ORDER LIST
# ==================================================

@st.cache_data(ttl=20, show_spinner=False)
def _load_pipeline_overview() -> Dict[str, int]:
    """Small live counts for the production landing panel."""
    rows = _q("""
        WITH line_scope AS (
            SELECT
                o.id AS order_id,
                UPPER(COALESCE(ol.lens_params->>'manufacturing_route', '')) AS route,
                COALESCE(ol.billed_qty, 0) AS billed_qty,
                COALESCE(ol.quantity, 0) AS quantity,
                jm.current_stage,
                jm.is_closed
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN job_master jm ON jm.order_line_id = ol.id
            WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol.eye_side, '')) NOT IN ('S','SERVICE')
              AND o.status NOT IN ('CANCELLED','CLOSED')
        )
        SELECT
            COUNT(DISTINCT order_id) FILTER (WHERE route = 'VENDOR') AS supplier_orders,
            COUNT(DISTINCT order_id) FILTER (WHERE route = 'EXTERNAL_LAB') AS lab_orders,
            COUNT(DISTINCT order_id) FILTER (WHERE route = 'INHOUSE') AS inhouse_orders,
            COUNT(DISTINCT order_id) FILTER (
                WHERE route = 'STOCK' OR (route = '' AND billed_qty > 0)
            ) AS stock_orders,
            COUNT(*) FILTER (
                WHERE current_stage IS NOT NULL AND COALESCE(is_closed, FALSE) = FALSE
            ) AS open_jobs,
            COUNT(*) FILTER (
                -- Strict bill-ready: only stages that open billing.
                WHERE current_stage IN ('READY_TO_BILL','READY_FOR_BILLING')
            ) AS bill_ready_jobs,
            COUNT(*) FILTER (
                -- Packing or already closed (terminal). Earlier this was
                -- aliased "ready_jobs" which conflated billing-ready with
                -- packing/closed and showed inflated numbers in the overview.
                WHERE current_stage IN ('READY_FOR_PACK','FITTING_DONE')
                   OR COALESCE(is_closed, FALSE) = TRUE
            ) AS completed_or_packing_jobs,
            -- Backward-compat alias kept so legacy callers don't NameError.
            -- New consumers should use bill_ready_jobs and completed_or_packing_jobs.
            COUNT(*) FILTER (
                WHERE current_stage IN ('READY_TO_BILL','READY_FOR_BILLING')
            ) AS ready_jobs
        FROM line_scope
    """, {})
    if not rows:
        return {}
    return {k: int(rows[0].get(k) or 0) for k in rows[0].keys()}



_PIPELINE_THEME = {
    "VENDOR":       {"accent": "#f59e0b", "bg": "#1a1200", "icon": "🏭"},
    "EXTERNAL_LAB": {"accent": "#a855f7", "bg": "#130b1e", "icon": "🧪"},
    "INHOUSE":      {"accent": "#3b82f6", "bg": "#0a1628", "icon": "🔬"},
    "STOCK":        {"accent": "#22c55e", "bg": "#041a0e", "icon": "📦"},
}


def _render_pipeline_cards(
    groups: dict,           # {order_id: {order_no, patient, order_id, lines, created_at}}
    route_key: str,         # VENDOR / EXTERNAL_LAB / INHOUSE / STOCK
    stage_label_fn,         # fn(line) -> str  — returns human stage label
    stage_code_fn,          # fn(line) -> str  — returns stage code
    advance_fn=None,        # fn(order_id, order_no) -> None  — advance button action
    open_billing_fn=None,   # fn(order_id, order_no) -> None  — open billing
    extra_line_fn=None,     # fn(line, order, order_id) -> None — per-line extra UI
):
    """
    Render compact pipeline cards symmetrically for all 4 pipelines.
    Each order = one card. R/L lines grouped inside. Power shown on expand.
    """
    import streamlit as st
    _theme  = _PIPELINE_THEME.get(route_key, _PIPELINE_THEME["VENDOR"])
    _accent = _theme["accent"]
    _bg     = _theme["bg"]
    _icon   = _theme["icon"]

    def _power_str(line: dict) -> str:
        """Format power string — uses is-not-None checks so SPH 0.0 (plano) renders correctly.
        Falls back to lens_params for stock lenses that store power there."""
        parts = []
        try:
            # ── Pull from columns first (is-not-None so 0.0 / 0 are kept) ──
            sph  = line["sph"]       if "sph"       in line and line["sph"]  is not None else None
            cyl  = line["cyl"]       if "cyl"       in line and line["cyl"]  is not None else None
            axis = line["axis"]      if "axis"      in line and line["axis"] is not None else None
            add  = line["add_power"] if "add_power" in line and line["add_power"] is not None else None

            # ── Fallback: sph_val / cyl_val aliases ──
            if sph  is None: sph  = line.get("sph_val")
            if cyl  is None: cyl  = line.get("cyl_val")
            if axis is None: axis = line.get("axis_val")
            if add  is None: add  = line.get("add")

            # ── Fallback: lens_params dict (stock lenses store power here) ──
            if any(v is None for v in (sph, cyl, axis)):
                import json as _pj
                _lp = line.get("_lp") or line.get("lens_params") or {}
                if isinstance(_lp, str):
                    try: _lp = _pj.loads(_lp)
                    except Exception as e:
                        log.debug("Could not parse lens_params: %s", e)
                        _lp = {}
                if sph  is None: sph  = _lp.get("sph")  or _lp.get("sph_val")
                if cyl  is None: cyl  = _lp.get("cyl")  or _lp.get("cyl_val")
                if axis is None: axis = _lp.get("axis") or _lp.get("axis_val")
                if add  is None: add  = _lp.get("add_power") or _lp.get("add")

            if sph is not None and str(sph) not in ("", "None"):
                try: parts.append(f"SPH {float(sph):+.2f}")
                except (ValueError, TypeError): pass

            if cyl is not None and str(cyl) not in ("", "None"):
                try:
                    if abs(float(cyl)) > 0.01:
                        parts.append(f"CYL {float(cyl):+.2f}")
                except (ValueError, TypeError): pass

            if axis is not None and str(axis) not in ("", "None", "0"):
                try:
                    _av = int(float(axis))
                    if _av != 0:
                        parts.append(f"AX {_av}°")
                except (ValueError, TypeError): pass

            if add is not None and str(add) not in ("", "None", "0", "0.0"):
                try:
                    if float(add) > 0:
                        parts.append(f"ADD {float(add):+.2f}")
                except (ValueError, TypeError): pass
        except Exception as _e:
            pass
        return "  ".join(parts) if parts else ""

    if groups:
        st.markdown(
            "<div style='display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 8px 0'>"
            "<span style='color:#94a3b8;font-size:0.68rem'>Actions:</span>"
            "<span style='background:#1e293b;color:#cbd5e1;border-radius:4px;padding:1px 7px;font-size:0.68rem'>👁 Details</span>"
            "<span style='background:#1e293b;color:#cbd5e1;border-radius:4px;padding:1px 7px;font-size:0.68rem'>💰 Billing</span>"
            "<span style='background:#1e293b;color:#cbd5e1;border-radius:4px;padding:1px 7px;font-size:0.68rem'>📋 Job card</span>"
            "<span style='background:#1e293b;color:#cbd5e1;border-radius:4px;padding:1px 7px;font-size:0.68rem'>🏷 Label</span>"
            "</div>",
            unsafe_allow_html=True,
        )

    # ── Pre-process all groups once before rendering ───────────────────────────
    # Parse lens_params JSON and compute power strings OUTSIDE the render loop.
    # Avoids repeated JSON.loads + string formatting on every Streamlit re-render.
    import json as _pp_json
    for _pre_gk, _pre_gd in groups.items():
        for _pre_l in _pre_gd.get("lines", []):
            if "_lp" not in _pre_l:
                _raw_lp = _pre_l.get("lens_params") or {}
                if isinstance(_raw_lp, str):
                    try: _pre_l["_lp"] = _pp_json.loads(_raw_lp)
                    except Exception as e:
                        log.debug("Could not parse cached lens_params: %s", e)
                        _pre_l["_lp"] = {}
                else:
                    _pre_l["_lp"] = _raw_lp if isinstance(_raw_lp, dict) else {}
            if "_pw" not in _pre_l:
                _pre_l["_pw"] = _power_str(_pre_l)
    # ── End pre-processing ──────────────────────────────────────────────────────

    for _gk, _gd in groups.items():
        _lines    = _gd.get("lines", [])
        _order_no = _gd.get("order_no", "")
        _order_no_display = str(_order_no or "")
        if _order_no_display.upper().endswith("-F"):
            _order_no_display = _order_no_display[:-2] + " · Fit"
        elif _order_no_display.upper().endswith("-C"):
            _order_no_display = _order_no_display[:-2] + " · Col"
        _patient  = _gd.get("patient", "—")
        _oid      = _gd.get("order_id", _gk)
        _order_uuid = str(_oid or _gk).split(":", 1)[0].strip()
        _date     = str(_gd.get("created_at","") or _lines[0].get("created_at","") if _lines else "")[:10]

        # Stage summary
        _stages   = [stage_code_fn(l) for l in _lines]
        _stg_lbl  = " | ".join(dict.fromkeys(stage_label_fn(l) for l in _lines))

        # Billed check — use flags from summary SQL if present (placeholder rows)
        # Fallback to DB query for full line rows
        _all_billed_here  = any(l.get("_is_challaned") or l.get("_is_invoiced") for l in _lines)
        _has_invoice_here = any(l.get("_is_invoiced") for l in _lines)
        if not _all_billed_here:
          try:
            from modules.sql_adapter import run_query as _rq_pc
            _lids = [str(l.get("line_id") or l.get("id","")) for l in _lines if l.get("line_id") or l.get("id")]
            if _lids:
                _bc = _rq_pc("""
                    SELECT COUNT(*) AS unbilled FROM (
                        SELECT DISTINCT ol.id FROM order_lines ol
                        WHERE ol.id = ANY(%(lids)s::uuid[])
                          AND NOT EXISTS (
                            SELECT 1 FROM challan_lines cl
                            JOIN challans c ON c.id = cl.challan_id
                            WHERE cl.order_line_id = ol.id
                              AND c.status NOT IN ('CANCELLED','VOID')
                          )
                    ) x
                """, {"lids": _lids})
                _all_billed_here = int((_bc[0].get("unbilled") or 1) if _bc else 1) == 0
                if _all_billed_here:
                    _ic = _rq_pc("""
                        SELECT 1 FROM invoices i
                        JOIN challans c ON c.id = i.challan_id
                        JOIN challan_lines cl ON cl.challan_id = c.id
                        WHERE cl.order_line_id = ANY(%(lids)s::uuid[])
                          AND i.status NOT IN ('CANCELLED','VOID')
                        LIMIT 1
                    """, {"lids": _lids})
                    _has_invoice_here = bool(_ic)
          except Exception as _e:
            pass

        # Billed badge
        if _all_billed_here:
            _bill_label = "🧾 INVOICED" if _has_invoice_here else "📋 CHALLANED"
            _bill_color = "#22c55e" if _has_invoice_here else "#3b82f6"
            _status_html = (
                f"<span style='background:{_bill_color};color:#fff;"
                f"font-size:0.68rem;font-weight:700;padding:2px 9px;"
                f"border-radius:8px'>{_bill_label}</span>"
            )
        else:
            _status_html = (
                f"<span style='background:{_accent}33;color:{_accent};"
                f"font-size:0.75rem;font-weight:700;padding:2px 9px;"
                f"border-radius:8px'>{_stg_lbl}</span>"
            )

        _multi_badge = ""
        _route_count = len(_lines)
        _order_line_count = _route_count
        try:
            from modules.sql_adapter import run_query as _rq_mo
            _oc = _rq_mo("""
                SELECT COUNT(*) AS n,
                       COUNT(DISTINCT UPPER(COALESCE(lens_params->>'manufacturing_route',''))) AS routes
                FROM order_lines
                WHERE order_id = %(oid)s::uuid
                  AND COALESCE(is_deleted, FALSE) = FALSE
            """, {"oid": _order_uuid}) or []
            if _oc:
                _order_line_count = int(_oc[0].get("n") or _route_count)
                _route_kinds = int(_oc[0].get("routes") or 1)
                if _order_line_count > _route_count or _route_kinds > 1:
                    _multi_badge = (
                        f"<span style='background:#312e81;color:#c4b5fd;padding:1px 8px;"
                        f"border-radius:8px;font-size:0.65rem;font-weight:800'>"
                        f"MULTI {_order_line_count} lines</span>"
                    )
        except Exception as _e:
            pass

        # Product+power lines HTML — always fetch fresh from DB so edits in Order Items
        # tab are reflected here without needing to close/reopen the card.
        # Also fixes placeholder rows (product_name="N job(s)") which have empty lens_params.
        _prod_lines = _lines
        try:
            _oid_prod = str(_oid or _gk).strip()
            if _oid_prod:
                from modules.sql_adapter import run_query as _rq_prod
                _db_lines = _rq_prod(
                    """SELECT ol.id, ol.eye_side, pr.product_name,
                              COALESCE(ol.sph,  (ol.lens_params->>'sph')::numeric)       AS sph,
                              COALESCE(ol.cyl,  (ol.lens_params->>'cyl')::numeric)       AS cyl,
                              COALESCE(ol.axis, (ol.lens_params->>'axis')::numeric)      AS axis,
                              COALESCE(ol.add_power,
                                       (ol.lens_params->>'add_power')::numeric,
                                       (ol.lens_params->>'add')::numeric)                AS add_power,
                              ol.quantity, ol.unit_price, ol.billing_total,
                              ol.lens_params, ol.boxing_params,
                              CASE
                                  WHEN UPPER(COALESCE(ol.batch_status,'')) = 'CANCELLED'
                                       THEN 'CANCELLED'
                                  ELSE COALESCE(ol.lens_params->>'replenishment_status','')
                              END AS replenishment_status,
                              COALESCE(ol.lens_params->>'supplier_confirmation_no','') AS supplier_confirmation_no,
                              COALESCE(ol.lens_params->>'replenishment_po_no','') AS replenishment_po_no,
                              pr.coating_type AS pr_coating, pr.index_value AS pr_index
                       FROM order_lines ol
                       LEFT JOIN products pr ON pr.id = ol.product_id
                       WHERE ol.order_id = %(oid)s::uuid
                         AND COALESCE(ol.is_deleted, FALSE) = FALSE
                         AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
                       ORDER BY ol.eye_side""",
                    {"oid": _order_uuid}
                )
                if _db_lines:
                    _prod_lines = _db_lines
        except Exception as _e:
            pass  # fall through to placeholder lines

        _prod_html = ""
        for _l in sorted(_prod_lines, key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1):
            _eye   = str(_l.get("eye_side","")).upper()
            _eye_s = "R" if _eye in ("R","RIGHT") else "L" if _eye in ("L","LEFT") else _eye[:1] or "—"
            _eye_c = "#ef4444" if _eye_s == "R" else "#60a5fa" if _eye_s == "L" else "#94a3b8"
            _pn    = str(_l.get("product_name","")).split(" | ")[0]
            _pw    = _power_str(_l)

            # Extract extra details from lens_params + boxing_params
            _lp_d = _l.get("lens_params") or {}
            _bp_d = _l.get("boxing_params") or {}
            if isinstance(_lp_d, str):
                try: import json as _jlp; _lp_d = _jlp.loads(_lp_d)
                except Exception as e:
                    log.debug("Could not parse lens_params: %s", e)
                    _lp_d = {}
            if isinstance(_bp_d, str):
                try: import json as _jbp; _bp_d = _jbp.loads(_bp_d)
                except Exception as e:
                    log.debug("Could not parse boxing_params: %s", e)
                    _bp_d = {}

            _detail_bits = []
            _idx_det = str(
                _lp_d.get("index_value")
                or _lp_d.get("lens_index")
                or _lp_d.get("index")
                or _l.get("lens_index")
                or _l.get("pr_index")          # from products table JOIN
                or ""
            ).strip()
            _coat_det = str(
                _lp_d.get("coating_type")
                or _lp_d.get("coating")
                or _l.get("coating_type")
                or _l.get("coating")
                or _l.get("pr_coating")        # from products table JOIN
                or ""
            ).strip()
            _treat_det = str(_lp_d.get("treatment") or _l.get("treatment") or "").strip()
            if _idx_det:
                _detail_bits.append(
                    f"<span style='color:#94a3b8;font-size:0.67rem'>Index:</span>"
                    f"<span style='color:#e2e8f0;font-size:0.67rem'>{_idx_det}</span>"
                )
            if _coat_det:
                _detail_bits.append(
                    f"<span style='color:#94a3b8;font-size:0.67rem'>Coating:</span>"
                    f"<span style='color:#e2e8f0;font-size:0.67rem'>{_coat_det}</span>"
                )
            if _treat_det and _treat_det.lower() != "clear":
                _detail_bits.append(
                    f"<span style='color:#94a3b8;font-size:0.67rem'>Treat:</span>"
                    f"<span style='color:#e2e8f0;font-size:0.67rem'>{_treat_det}</span>"
                )
            for _fk, _fl in [
                ("diameter",       "Dia"),
                ("fitting_height",  "FH"),
                ("corridor",       "Corr"),
                ("frame_type",     "Frame"),
                ("tinted",         "Tint"),
                ("prism",          "Prism"),
                ("base_curve",     "BC"),
                ("instructions",   "Note"),
            ]:
                _fv = str(_lp_d.get(_fk, "") or _bp_d.get(_fk, "") or "").strip()
                if _fv and _fv not in ("", "None", "null", "0", "0.0"):
                    _detail_bits.append(
                        f"<span style='color:#94a3b8;font-size:0.67rem'>{_fl}:</span>"
                        f"<span style='color:#e2e8f0;font-size:0.67rem'>{_fv}</span>"
                    )
            _pd = str(_bp_d.get("pd", "") or _lp_d.get("pd", "") or "").strip()
            if _pd and _pd not in ("", "None", "null", "0"):
                _detail_bits.append(
                    f"<span style='color:#94a3b8;font-size:0.67rem'>PD:</span>"
                    f"<span style='color:#e2e8f0;font-size:0.67rem'>{_pd}</span>"
                )

            _repl_status = str(
                _l.get("replenishment_status")
                or _lp_d.get("replenishment_status")
                or ""
            ).upper()
            if _repl_status:
                _repl_label = (
                    "Cancelled / Not needed" if _repl_status == "CANCELLED"
                    else "PO Sent" if _repl_status == "PO_SENT"
                    else "In Procurement" if _repl_status == "ORDERED"
                    else "Procured" if _repl_status == "PROCURED"
                    else "Discarded" if _repl_status == "DISCARDED"
                    else _repl_status.replace("_", " ").title()
                )
                _repl_color = (
                    "#22c55e" if _repl_status in ("ORDERED", "PROCURED")
                    else "#38bdf8" if _repl_status == "PO_SENT"
                    else "#ef4444" if _repl_status in ("CANCELLED", "DISCARDED")
                    else "#94a3b8"
                )
                _po_show = str(_l.get("replenishment_po_no") or _lp_d.get("replenishment_po_no") or "").strip()
                _ref_show = str(_l.get("supplier_confirmation_no") or _lp_d.get("supplier_confirmation_no") or "").strip()
                _detail_bits.append(
                    f"<span style='color:#94a3b8;font-size:0.67rem'>Repl:</span>"
                    f"<span style='color:{_repl_color};font-size:0.67rem;font-weight:700'>"
                    f"{_repl_label}{' · '+_po_show if _po_show else ''}{' · Ref '+_ref_show if _ref_show else ''}</span>"
                )

            _prod_html += (
                f"<div style='padding:4px 0;border-bottom:1px solid #1e293b'>"
                f"<div style='display:flex;gap:8px;align-items:center'>"
                f"<span style='color:{_eye_c};font-weight:800;font-size:0.72rem;min-width:12px'>{_eye_s}</span>"
                f"<span style='color:#e2e8f0;font-size:0.75rem;font-weight:600'>{_pn}</span>"
                + (f"<span style='color:#7dd3fc;font-size:0.7rem;font-family:monospace'>{_pw}</span>" if _pw else "")
                + f"</div>"
                + (
                    f"<div style='display:flex;flex-wrap:wrap;gap:6px;margin-top:2px;padding-left:20px'>"
                    + "".join(
                        f"<span style='background:#1e293b;border:1px solid #334155;border-radius:4px;"
                        f"padding:1px 6px;display:inline-flex;gap:4px'>{b}</span>"
                        for b in _detail_bits
                    )
                    + "</div>"
                    if _detail_bits else ""
                )
                + "</div>"
            )

        # ── Order separator line — clear visual break between orders ────
        st.markdown(
            "<div style='border-top:2px solid #1e293b;margin:10px 0 6px 0;"
            "display:flex;align-items:center;gap:8px'>"
            f"<span style='background:#0f172a;color:#334155;font-size:0.65rem;"
            f"padding:0 8px;white-space:nowrap;letter-spacing:.05em'>"
            f"ORDER {_order_no_display}</span></div>",
            unsafe_allow_html=True,
        )

        # Order type badge (WHOLESALE/RETAIL) for table row
        _ot_raw = str(_lines[0].get("order_type") if _lines else "").upper() if _lines else ""
        _otype_colors = {"WHOLESALE": "#8b5cf6", "RETAIL": "#0891b2", "PURCHASE": "#f59e0b"}
        _otype_col = _otype_colors.get(_ot_raw, "")
        _otype_badge = (
            f"<span style='background:{_otype_col};color:#fff;padding:1px 8px;"
            f"border-radius:8px;font-size:0.65rem;font-weight:800'>{_ot_raw}</span>"
        ) if _otype_col else ""

        # Main card
        _key_suffix = _production_card_key_suffix(_gk)
        _detail_key = f"pp_detail_{route_key}_{_key_suffix}"
        _cc1, _cc2 = st.columns([6, 4])

        with _cc1:
            st.markdown(
                f"<div style='background:{_bg};border:1px solid {_accent}33;"
                f"border-left:4px solid {_accent};border-radius:6px;"
                f"padding:8px 14px;margin-bottom:3px'>"
                f"<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>"
                f"<span style='color:#475569;font-size:0.7rem'>{_date}</span>"
                f"<span style='color:#f1f5f9;font-weight:800;font-size:0.88rem;"
                f"font-family:monospace'>{_order_no_display}</span>"
                f"<span style='color:#cbd5e1;font-size:0.82rem;font-weight:600'>{_patient}</span>"
                f"{_status_html}"
                f"{_otype_badge}"
                f"{_multi_badge}"
                f"</div>"
                f"<div style='margin-top:4px;color:#475569;font-size:0.7rem'>"
                f"{_icon} {route_key.replace('_',' ')}</div>"
                f"</div>",
                unsafe_allow_html=True
            )

        with _cc2:
            _ab1, _ab2, _ab3, _ab4, _ab5, _ab6, _ab7, _ab8, _ab9, _ab10 = st.columns([1, 1, 1, 1, 1, 1, 1, 1, 1, 1])
            with _ab1:
                if st.button("👁", key=f"pp_det_{route_key}_{_key_suffix}",
                             help="Show products & power",
                             use_container_width=True):
                    st.session_state[_detail_key] = not st.session_state.get(_detail_key, False)
                    st.rerun()
            with _ab2:
                if _all_billed_here:
                    st.button("🔒", key=f"pp_lock_{route_key}_{_key_suffix}",
                              disabled=True, use_container_width=True)
                elif open_billing_fn:
                    if st.button("💰", key=f"pp_bill_{route_key}_{_key_suffix}",
                                 help="Open billing", type="primary",
                                 use_container_width=True):
                        st.session_state.pop("_billing_blocked_msg", None)
                        open_billing_fn(_order_uuid, _order_no)
            with _ab3:
                if st.button("📋", key=f"pp_jc_{route_key}_{_key_suffix}",
                             help="Print Job Card (R+L)",
                             use_container_width=True):
                    st.session_state[f"pp_do_jc_{_key_suffix}"] = True
            with _ab4:
                if st.button("🏷️", key=f"pp_lbl_{route_key}_{_key_suffix}",
                             help="Print Barcode Labels",
                             use_container_width=True):
                    st.session_state[f"pp_do_lbl_{_key_suffix}"] = True
            with _ab5:
                if st.button("💳", key=f"pp_card_{route_key}_{_key_suffix}",
                             help="Print Customer Card",
                             use_container_width=True):
                    st.session_state[f"pp_do_card_{_key_suffix}"] = True
            with _ab6:
                if st.button("🪪", key=f"pp_card75_{route_key}_{_key_suffix}",
                             help="Print 75×50 Customer Card",
                             use_container_width=True):
                    st.session_state[f"pp_do_card75_{_key_suffix}"] = True
            with _ab7:
                if route_key == "INHOUSE":
                    st.caption("")
                elif st.button("✏️", key=f"pp_assign_{route_key}_{_key_suffix}",
                               help="Open Assignment Workspace — set route, supplier, lab",
                               use_container_width=True):
                    # Clear old workspace state so selectors reset
                    for _sk in list(st.session_state.keys()):
                        if _sk.startswith("aw_r_") or _sk.startswith("aw_l_") or _sk.startswith("radio_aw_"):
                            del st.session_state[_sk]
                    st.session_state["prod_assign_order_no"] = _order_no
                    st.session_state["prod_view_mode"] = "assign"
                    _panel_by_route = {
                        "VENDOR": "🏭 Supplier",
                        "EXTERNAL_LAB": "🧪 External Supplier",
                        "INHOUSE": "🔬 In-house Lab",
                        "STOCK": "📦 Stock Repl.",
                    }
                    if route_key in _panel_by_route:
                        # Use the _next sidecar key — writing directly to
                        # 'prod_lazy_panel' fails after the widget with that
                        # same key has been instantiated this run. The selector
                        # reads _prod_lazy_panel_next on the next rerun and
                        # uses it as its initial value.
                        st.session_state["_prod_lazy_panel_next"] = _panel_by_route[route_key]
                    st.rerun()
            with _ab8:
                if st.button("🧭", key=f"pp_path_{route_key}_{_key_suffix}",
                             help="Review full order path with timestamps",
                             use_container_width=True):
                    st.session_state[f"pp_show_path_{route_key}_{_key_suffix}"] = not st.session_state.get(f"pp_show_path_{route_key}_{_key_suffix}", False)
                    st.rerun()
            with _ab9:
                # ↕ Shift to full card view for THIS order only
                if st.button("↕️", key=f"pp_shift_{route_key}_{_key_suffix}",
                             help="Switch to full card view for this order",
                             use_container_width=True):
                    if str(st.session_state.get("_ih_full_order_id") or "") == str(_order_uuid):
                        st.session_state.pop("_ih_full_order_id", None)
                        st.session_state.pop("_ih_full_order_no", None)
                    else:
                        st.session_state["_ih_force_full_view"]   = True
                        st.session_state["_ih_full_order_id"]     = _order_uuid
                        st.session_state["_ih_full_order_no"]     = str(_order_no)
                    st.rerun()
            with _ab10:
                if route_key == "INHOUSE":
                    if st.button("🖨️", key=f"pp_jc_lbl_{route_key}_{_key_suffix}",
                                 help="Print Job Card + Barcode Labels together",
                                 use_container_width=True):
                        st.session_state[f"pp_do_jc_lbl_{_key_suffix}"] = True
                else:
                    st.caption("")

        # Show billing blocked message inline (below this card only)
        _bill_msg_key = "_billing_blocked_msg"
        if st.session_state.get(_bill_msg_key):
            st.warning(st.session_state.pop(_bill_msg_key))

        if st.session_state.get(f"pp_show_path_{route_key}_{_key_suffix}"):
            try:
                _path_rows = _q("""
                    SELECT src, line_label, stage, event_time, actor, remarks
                    FROM (
                        SELECT 'Order' AS src,
                               'Order' AS line_label,
                               o.status AS stage,
                               o.created_at AS event_time,
                               '' AS actor,
                               'Order entered system' AS remarks
                        FROM orders o
                        WHERE o.id = %(oid)s::uuid
                        UNION ALL
                        SELECT 'Production' AS src,
                               COALESCE(ol.eye_side,'') || ' ' || COALESCE(p.product_name,'Line') AS line_label,
                               jse.stage_code AS stage,
                               jse.created_at AS event_time,
                               COALESCE(jse.performed_by::text,'') AS actor,
                               COALESCE(jse.remarks,'') AS remarks
                        FROM job_stage_events jse
                        JOIN job_master jm ON jm.id = jse.job_id
                        JOIN order_lines ol ON ol.id = jm.order_line_id
                        LEFT JOIN products p ON p.id = ol.product_id
                        WHERE ol.order_id = %(oid)s::uuid
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                        UNION ALL
                        SELECT 'Supplier' AS src,
                               COALESCE(ol.eye_side,'') || ' ' || COALESCE(p.product_name,'Line') AS line_label,
                               COALESCE(so.status,'PO') AS stage,
                               so.created_at AS event_time,
                               '' AS actor,
                               COALESCE(so.supplier_order_id,'') AS remarks
                        FROM supplier_order_items soi
                        JOIN supplier_orders so ON so.id = soi.supplier_order_id
                        JOIN order_lines ol ON ol.id::text = soi.customer_line_id::text
                        LEFT JOIN products p ON p.id = ol.product_id
                        WHERE ol.order_id = %(oid)s::uuid
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                        UNION ALL
                        SELECT 'Supplier' AS src,
                               COALESCE(ol.eye_side,'') || ' ' || COALESCE(p.product_name,'Line') AS line_label,
                               ev->>'stage' AS stage,
                               NULLIF(ev->>'at','')::timestamptz AS event_time,
                               '' AS actor,
                               COALESCE(ev->>'source','supplier panel') AS remarks
                        FROM order_lines ol
                        LEFT JOIN products p ON p.id = ol.product_id
                        CROSS JOIN LATERAL jsonb_array_elements(
                            COALESCE(ol.lens_params->'supplier_timeline','[]'::jsonb)
                        ) ev
                        WHERE ol.order_id = %(oid)s::uuid
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                        UNION ALL
                        SELECT 'Stock Repl.' AS src,
                               COALESCE(ol.eye_side,'') || ' ' || COALESCE(p.product_name,'Line') AS line_label,
                               ev->>'stage' AS stage,
                               NULLIF(ev->>'at','')::timestamptz AS event_time,
                               '' AS actor,
                               TRIM(BOTH ' · ' FROM CONCAT_WS(' · ',
                                   NULLIF(ev->>'po_no',''),
                                   CASE WHEN NULLIF(ev->>'supplier_ref','') IS NOT NULL
                                        THEN 'Ref ' || (ev->>'supplier_ref') ELSE NULL END,
                                   NULLIF(ev->>'supplier','')
                               )) AS remarks
                        FROM order_lines ol
                        LEFT JOIN products p ON p.id = ol.product_id
                        CROSS JOIN LATERAL jsonb_array_elements(
                            COALESCE(ol.lens_params->'replenishment_timeline','[]'::jsonb)
                        ) ev
                        WHERE ol.order_id = %(oid)s::uuid
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                        UNION ALL
                        SELECT 'Stock Repl.' AS src,
                               COALESCE(ol.eye_side,'') || ' ' || COALESCE(p.product_name,'Line') AS line_label,
                               CASE
                                   WHEN UPPER(COALESCE(ol.batch_status,'')) = 'CANCELLED'
                                        THEN 'CANCELLED'
                                   ELSE COALESCE(ol.lens_params->>'replenishment_status','')
                               END AS stage,
                               COALESCE(o.updated_at, o.created_at) AS event_time,
                               '' AS actor,
                               TRIM(BOTH ' · ' FROM CONCAT_WS(' · ',
                                   NULLIF(ol.lens_params->>'replenishment_po_no',''),
                                   CASE WHEN NULLIF(ol.lens_params->>'supplier_confirmation_no','') IS NOT NULL
                                        THEN 'Ref ' || (ol.lens_params->>'supplier_confirmation_no') ELSE NULL END,
                                   NULLIF(ol.lens_params->>'replenishment_supplier_name','')
                               )) AS remarks
                        FROM order_lines ol
                        JOIN orders o ON o.id = ol.order_id
                        LEFT JOIN products p ON p.id = ol.product_id
                        WHERE ol.order_id = %(oid)s::uuid
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                          AND (
                              UPPER(COALESCE(ol.batch_status,'')) = 'CANCELLED'
                              OR COALESCE(ol.lens_params->>'replenishment_status','') <> ''
                          )
                          AND jsonb_array_length(COALESCE(ol.lens_params->'replenishment_timeline','[]'::jsonb)) = 0
                        UNION ALL
                        SELECT 'Billing' AS src,
                               COALESCE(ol.eye_side,'') || ' ' || COALESCE(p.product_name,'Line') AS line_label,
                               'CHALLAN ' || COALESCE(c.status,'') AS stage,
                               c.created_at AS event_time,
                               '' AS actor,
                               COALESCE(c.challan_no,'') AS remarks
                        FROM challan_lines cl
                        JOIN challans c ON c.id = cl.challan_id
                        JOIN order_lines ol ON ol.id = cl.order_line_id
                        LEFT JOIN products p ON p.id = ol.product_id
                        WHERE ol.order_id = %(oid)s::uuid
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                    ) x
                    ORDER BY event_time ASC NULLS LAST
                """, {"oid": _order_uuid}) or []
                with st.expander("🧭 Review Path", expanded=True):
                    if _path_rows:
                        import pandas as _pd_path
                        st.dataframe(_pd_path.DataFrame(_path_rows), use_container_width=True, hide_index=True)
                    else:
                        st.info("No path events recorded yet.")
            except Exception as _path_e:
                st.warning(f"Review path unavailable: {_path_e}")

        # Expand: product + power detail
        if st.session_state.get(_detail_key):
            st.markdown(
                f"<div style='background:#0a0f1a;border:1px solid {_accent}22;"
                f"border-top:none;border-radius:0 0 6px 6px;"
                f"padding:8px 14px;margin-top:-3px;margin-bottom:4px'>"
                f"{_prod_html}"
                f"</div>",
                unsafe_allow_html=True
            )
            if route_key == "INHOUSE":
                if st.button("↔ Open Full R/L Controls For This Order",
                             key=f"pp_full_{route_key}_{_key_suffix}",
                             use_container_width=True,
                             help="Switch from compact job-stage card to the full R/L production controls"):
                    # Preserve the exact order being opened. Pressing the same
                    # control again is the only collapse action; stage saves
                    # and prints keep the full R/L controls pinned.
                    if str(st.session_state.get("_ih_full_order_id") or "") == str(_order_uuid):
                        st.session_state.pop("_ih_full_order_id", None)
                        st.session_state.pop("_ih_full_order_no", None)
                    else:
                        st.session_state["_ih_force_full_view"] = True
                        st.session_state["_ih_full_order_id"] = _order_uuid
                        st.session_state["_ih_full_order_no"] = str(_order_no or "")
                    st.rerun()

        # ── Helper: load surfacing_data from lens_params for a line ──
        def _load_surf_lp(ln):
            if not ln: return None
            import json as _lslpj
            _ld = dict(ln)
            if not _ld.get("surfacing_data"):
                _lp2 = _ld.get("lens_params") or {}
                if isinstance(_lp2, str):
                    try: _lp2 = _lslpj.loads(_lp2)
                    except Exception as e:
                        log.debug("Could not parse lens_params: %s", e)
                        _lp2 = {}
                _ld["surfacing_data"] = _lp2.get("surfacing_data") or {}
            return _ld

        def _resolved_print_lines_and_missing_blanks(raw_lines, order_for_print):
            """Return real R/L print lines and any eyes still missing blank assignment."""
            try:
                from modules.backoffice.production_panel import _real_label_lines_for_order
                _seed = []
                for _ln in raw_lines or []:
                    _loaded = _load_surf_lp(_ln)
                    if _loaded:
                        _seed.append(_loaded)
                _resolved = [_load_surf_lp(_ln) for _ln in _real_label_lines_for_order(_seed, order_for_print)]
            except Exception as _resolve_e:
                log.debug("Could not resolve real print lines: %s", _resolve_e)
                _resolved = [_load_surf_lp(_ln) for _ln in (raw_lines or []) if _ln]

            _need_check = []
            _missing = []
            for _ln in _resolved:
                _eye = str(_ln.get("eye_side") or "").upper()[:1]
                if _eye not in ("R", "L"):
                    continue
                _lp = _ln.get("lens_params") or {}
                if isinstance(_lp, str):
                    import json as _mbj
                    try:
                        _lp = _mbj.loads(_lp) if _lp else {}
                    except Exception as _lp_e:
                        log.debug("Could not parse lens params for blank gate: %s", _lp_e)
                        _lp = {}
                _svc = str(
                    _ln.get("service_production_type")
                    or _lp.get("service_production_type")
                    or ""
                ).upper()
                if _svc in ("FITTING", "COLOURING"):
                    continue
                _surf = _ln.get("surfacing_data") or _lp.get("surfacing_data") or {}
                if not isinstance(_surf, dict):
                    _surf = {}
                if _surf.get("blank_id") or _surf.get("selected_blank_id") or _surf.get("blank_batch"):
                    continue
                _lid = str(_ln.get("line_id") or _ln.get("id") or "")
                _need_check.append((_eye, _lid))

            _allocated = set()
            _line_ids = [lid for _eye, lid in _need_check if lid]
            if _line_ids:
                try:
                    from modules.sql_adapter import run_query as _rq_blank_gate
                    _alloc_rows = _rq_blank_gate(
                        "SELECT DISTINCT order_line_id::text AS line_id "
                        "FROM blank_allocations "
                        "WHERE order_line_id = ANY(%(lids)s::uuid[])",
                        {"lids": _line_ids},
                    ) or []
                    _allocated = {str(r.get("line_id") or "") for r in _alloc_rows}
                except Exception as _alloc_e:
                    log.debug("Blank allocation check failed: %s", _alloc_e)

            for _eye, _lid in _need_check:
                if not _lid or _lid not in _allocated:
                    _missing.append(_eye)

            return _resolved, sorted(set(_missing))

        # ── Trigger: Print Job Card + Barcode Labels together ──
        if st.session_state.pop(f"pp_do_jc_lbl_{_key_suffix}", False) and not st.session_state.get("_navigating_to_billing"):
            try:
                from modules.backoffice.production_panel import (
                    _build_label_page, _open_print_window, _print_tsc_production_labels
                )
                from modules.documents.job_card_surfacing import _open_jc_print_window

                _combo_order = {
                    "id": _oid,
                    "order_no": _order_no,
                    "patient_name": _patient,
                    "party_name": _gd.get("lines", [{}])[0].get("party_name", "") if _lines else "",
                    "order_type": _lines[0].get("order_type", "RETAIL") if _lines else "RETAIL",
                }
                _combo_seed = []
                for _cl in _lines:
                    _loaded = _load_surf_lp(_cl)
                    if _loaded:
                        _combo_seed.append(_loaded)
                _combo_lines, _combo_missing = _resolved_print_lines_and_missing_blanks(_combo_seed, _combo_order)
                if _combo_missing:
                    st.error(
                        "🔴 Assignment not done — assign blank first for "
                        f"{'/'.join(_combo_missing)} eye before printing job card and labels."
                    )
                else:
                    _ok, _msg = _print_tsc_production_labels(_combo_lines, _combo_order)
                    if _ok:
                        st.success("Sent label(s) to TSC")
                    else:
                        st.warning(f"TSC direct print failed: {_msg}. Opening HTML standby.")
                        _open_print_window(_build_label_page(_combo_lines, _combo_order))

                    _r_ln = next((l for l in _combo_lines if str(l.get("eye_side", "")).upper() in ("R", "RIGHT")), None)
                    _l_ln = next((l for l in _combo_lines if str(l.get("eye_side", "")).upper() in ("L", "LEFT")), None)
                    _open_jc_print_window(_load_surf_lp(_r_ln), _load_surf_lp(_l_ln), _combo_order)
            except Exception as _combo_e:
                st.error(f"Job card + label error: {_combo_e}")

        # ── Trigger: Print Job Card (R+L combined) ──
        if st.session_state.pop(f"pp_do_jc_{_key_suffix}", False) and not st.session_state.get("_navigating_to_billing"):
            try:
                from modules.documents.job_card_surfacing import _open_jc_print_window
                _jc_order2 = {"id": _oid, "order_no": _order_no, "patient_name": _patient}
                _jc_lines, _jc_missing = _resolved_print_lines_and_missing_blanks(_lines, _jc_order2)
                if _jc_missing:
                    st.error(f"🔴 Assignment not done — assign blank first for {'/'.join(_jc_missing)} eye before printing job card.")
                else:
                    _r_ln = next((l for l in _jc_lines if str(l.get("eye_side","")).upper() in ("R","RIGHT")), None)
                    _l_ln = next((l for l in _jc_lines if str(l.get("eye_side","")).upper() in ("L","LEFT")), None)
                    _open_jc_print_window(_load_surf_lp(_r_ln), _load_surf_lp(_l_ln), _jc_order2)
            except Exception as _jpe2:
                st.error(f"Job card error: {_jpe2}")

        # ── Trigger: Print Barcode Labels ──
        if st.session_state.pop(f"pp_do_lbl_{_key_suffix}", False) and not st.session_state.get("_navigating_to_billing"):
            try:
                from modules.backoffice.production_panel import (
                    _build_label_page, _open_print_window, _print_tsc_production_labels
                )
                _lbl_lines = []
                for _ll in _lines:
                    _lld = _load_surf_lp(_ll)
                    if _lld: _lbl_lines.append(_lld)
                _lb_ord = {"id": _oid, "order_no": _order_no, "patient_name": _patient,
                           "party_name": _gd.get("lines",[{}])[0].get("party_name","") if _lines else "",
                           "order_type": _lines[0].get("order_type","RETAIL") if _lines else "RETAIL"}
                _lbl_lines, _lbl_missing = _resolved_print_lines_and_missing_blanks(_lbl_lines, _lb_ord)
                if _lbl_missing:
                    st.error(f"🔴 Assignment not done — assign blank first for {'/'.join(_lbl_missing)} eye before printing labels.")
                else:
                    _ok, _msg = _print_tsc_production_labels(_lbl_lines, _lb_ord)
                    if _ok:
                        st.success("Sent label(s) to TSC")
                    else:
                        st.warning(f"TSC direct print failed: {_msg}. Opening HTML standby.")
                        _open_print_window(_build_label_page(_lbl_lines, _lb_ord))
            except Exception as _lpe2:
                st.error(f"Label error: {_lpe2}")

        # ── Trigger: Print CR80 Customer Card ──
        if st.session_state.pop(f"pp_do_card_{_key_suffix}", False) and not st.session_state.get("_navigating_to_billing"):
            try:
                from modules.backoffice.production_panel import (
                    _build_cr80_page, _open_print_window
                )
                _r_ln = next((l for l in _lines if str(l.get("eye_side","")).upper() in ("R","RIGHT")), None)
                _l_ln = next((l for l in _lines if str(l.get("eye_side","")).upper() in ("L","LEFT")), None)
                _card_order = {"id": _oid, "order_no": _order_no, "patient_name": _patient,
                               "party_name": _gd.get("lines",[{}])[0].get("party_name","") if _lines else "",
                               "order_type": _lines[0].get("order_type","RETAIL") if _lines else "RETAIL"}
                _open_print_window(_build_cr80_page(_load_surf_lp(_r_ln), _load_surf_lp(_l_ln), _card_order))
            except Exception as _cpe2:
                st.error(f"Customer card error: {_cpe2}")

        # ── Trigger: Print 75×50 Customer Card ──
        if st.session_state.pop(f"pp_do_card75_{_key_suffix}", False) and not st.session_state.get("_navigating_to_billing"):
            try:
                from modules.backoffice.production_panel import (
                    _build_customer_75x50_page, _open_print_window, _order_print_names,
                    _product_display_for_card, _real_label_lines_for_order
                )
                from modules.printing.label_printer import print_tspl_customer_label
                _card_order = {"id": _oid, "order_no": _order_no, "patient_name": _patient,
                               "party_name": _gd.get("lines",[{}])[0].get("party_name","") if _lines else "",
                               "order_type": _lines[0].get("order_type","RETAIL") if _lines else "RETAIL"}
                _real_lines = [_load_surf_lp(x) for x in _real_label_lines_for_order(_lines, _card_order)]
                _r_loaded = next((l for l in _real_lines if str((l or {}).get("eye_side","")).upper()[:1] == "R"), None)
                _l_loaded = next((l for l in _real_lines if str((l or {}).get("eye_side","")).upper()[:1] == "L"), None)
                _card75_lines, _card75_missing = _resolved_print_lines_and_missing_blanks(_real_lines, _card_order)
                if _card75_missing:
                    st.error(
                        "🔴 Assignment not done — assign blank first for "
                        f"{'/'.join(_card75_missing)} eye before printing customer label."
                    )
                    return
                _names = _order_print_names(_card_order, _r_loaded or _l_loaded or {})
                import datetime as _dt

                def _rx(_ln):
                    return {
                        "sph": (_ln or {}).get("sph"),
                        "cyl": (_ln or {}).get("cyl"),
                        "axis": (_ln or {}).get("axis"),
                        "add": (_ln or {}).get("add_power"),
                    }

                def _card_customer_name(value):
                    _s = str(value or "").strip()
                    return "" if _s.lower() in ("end customer", "unknown", "none", "null", "-", "—") else _s

                _ok, _msg = print_tspl_customer_label(
                    order_no=_order_no,
                    customer=_card_customer_name(_names.get("customer")),
                    optician=_names.get("optician") or _names.get("party") or _card_order.get("party_name", ""),
                    product=_product_display_for_card(_r_loaded or _l_loaded or {}),
                    rx_r=_rx(_r_loaded),
                    rx_l=_rx(_l_loaded),
                    mobile=_names.get("mobile") or "",
                    date_text=_dt.date.today().strftime("%d-%m-%Y"),
                    copies=1,
                )
                if _ok:
                    st.success("Sent 75×50 customer label to TSC")
                else:
                    st.warning(f"TSC direct print failed: {_msg}. Opening HTML standby.")
                    _open_print_window(_build_customer_75x50_page(_r_loaded, _l_loaded, _card_order))
            except Exception as _c75e2:
                st.error(f"75×50 customer card error: {_c75e2}")

        # Per-line extra UI (stage advance buttons etc) — only when expanded
        if extra_line_fn and st.session_state.get(_detail_key) and not _all_billed_here:
            for _l in _lines:
                extra_line_fn(_l, {"id": _oid, "order_no": _order_no, "patient_name": _patient}, _oid)

        st.markdown(f"<div style='height:1px;background:{_accent}22;margin:0 0 3px 0'></div>",
                    unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════
# BILLING NAVIGATION HELPER
# ══════════════════════════════════════════════════════════════════════


def _go_to_billing(order_id: str, order_no: str) -> None:
    """
    Jump from Production page directly to Billing Summary tab in Backoffice.
    Gate: Inhouse jobs must reach READY_TO_BILL before billing.
    Stock lines are always billable. Procurement runs independently.
    """
    try:
        from modules.sql_adapter import run_query as _rq_gate, resolve_order_uuid as _resolve_order_uuid
        _resolved_order_id = _resolve_order_uuid(order_id or order_no) or ""
        if not _resolved_order_id:
            st.session_state["_billing_blocked_msg"] = "Billing blocked — order could not be resolved."
            return
        order_id = _resolved_order_id

        # Gate 1: inhouse jobs not closed
        _inhouse_open = _rq_gate("""
            SELECT COUNT(*) AS n
            FROM order_lines ol
            WHERE ol.order_id = %(oid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM challan_lines cl
                  JOIN challans c ON c.id = cl.challan_id
                  WHERE cl.order_line_id = ol.id
                    AND c.status NOT IN ('CANCELLED','VOID')
                    AND COALESCE(c.is_deleted, FALSE) = FALSE
              )
              AND NOT EXISTS (
                  SELECT 1 FROM job_master jm
                  WHERE jm.order_line_id = ol.id
                    AND (
                        COALESCE(jm.is_closed, FALSE) = TRUE
                        OR jm.current_stage = 'READY_TO_BILL'
                    )
              )
        """, {"oid": order_id})
        _n_inhouse = int((_inhouse_open[0].get("n") or 0) if _inhouse_open else 0)
        if _n_inhouse > 0:
            st.session_state["_billing_blocked_msg"] = (
                f"Billing blocked — {_n_inhouse} inhouse job(s) not yet closed. "
                f"Advance production to READY TO BILL first."
            )
            return

        # Stock billing is ALWAYS OPEN. Procurement runs independently and
        # does not gate revenue billing. Only inhouse job completion gates billing.

    except Exception as _e:
        pass

    # ── Clear ALL stale print / job-card session state before navigating ─
    # show_print_*, show_label_*, show_cr80_* keys trigger _open_print_window
    # on the NEXT render. If any of these are set from a production-page
    # print action they will open a blob: URL when backoffice loads.
    _stale_keys = [
        k for k in list(st.session_state.keys())
        if k.startswith((
            "show_print_", "show_label_", "show_cr80_",
            "jc_open_", "jc_saving_", "jc_wip_loaded_",
            "pp_do_jc_", "pp_do_lbl_", "pp_do_card_", "pp_do_card75_",
        ))
    ]
    for _sk in _stale_keys:
        st.session_state.pop(_sk, None)

    st.session_state["_navigating_to_billing"] = True
    st.session_state["bo_selected_order_id"] = order_id   # must be UUID, not order_no
    st.session_state["bo_view_mode"]         = "order_detail"
    st.session_state["bo_jump_to_billing"]   = True
    st.session_state["bo_orders_loaded"]     = False
    st.session_state["_sidebar_page"]        = "⚙️  Backoffice"
    st.session_state["active_module"]        = "Backoffice"
    st.rerun()



def _check_purchase_acked(line_id: str) -> dict:
    """
    Returns purchase ack record for a line, or empty dict.
    Used to gate billing button and show lock status.
    """
    try:
        from modules.sql_adapter import run_query as _rq_pa
        rows = _rq_pa("""
            SELECT pa.challan_no, pa.invoice_no, pa.purchase_price,
                   pa.is_price_locked, pa.total_value, pa.document_date::text,
                   pa.received_qty, pa.billing_status
            FROM purchase_acknowledgements pa
            WHERE pa.order_line_id = %(lid)s::uuid
            LIMIT 1
        """, {"lid": line_id})
        return rows[0] if rows else {}
    except Exception as _e:
        return {}


def _power_str(line: dict) -> str:
    """Format power string — uses is-not-None checks so SPH 0.0 (plano) renders correctly.
    Falls back to lens_params for stock lenses that store power there."""
    parts = []
    try:
        # ── Pull from columns first (is-not-None so 0.0 / 0 are kept) ──
        sph  = line["sph"]       if "sph"       in line and line["sph"]  is not None else None
        cyl  = line["cyl"]       if "cyl"       in line and line["cyl"]  is not None else None
        axis = line["axis"]      if "axis"      in line and line["axis"] is not None else None
        add  = line["add_power"] if "add_power" in line and line["add_power"] is not None else None

        # ── Fallback: sph_val / cyl_val aliases ──
        if sph  is None: sph  = line.get("sph_val")
        if cyl  is None: cyl  = line.get("cyl_val")
        if axis is None: axis = line.get("axis_val")
        if add  is None: add  = line.get("add")

        # ── Fallback: lens_params dict (stock lenses store power here) ──
        if any(v is None for v in (sph, cyl, axis)):
            import json as _pj
            _lp = line.get("_lp") or line.get("lens_params") or {}
            if isinstance(_lp, str):
                try: _lp = _pj.loads(_lp)
                except Exception as e:
                    log.debug("Could not parse lens_params: %s", e)
                    _lp = {}
            if sph  is None: sph  = _lp.get("sph")  or _lp.get("sph_val")
            if cyl  is None: cyl  = _lp.get("cyl")  or _lp.get("cyl_val")
            if axis is None: axis = _lp.get("axis") or _lp.get("axis_val")
            if add  is None: add  = _lp.get("add_power") or _lp.get("add")

        if sph is not None and str(sph) not in ("", "None"):
            try: parts.append(f"SPH {float(sph):+.2f}")
            except (ValueError, TypeError): pass

        if cyl is not None and str(cyl) not in ("", "None"):
            try:
                if abs(float(cyl)) > 0.01:
                    parts.append(f"CYL {float(cyl):+.2f}")
            except (ValueError, TypeError): pass

        if axis is not None and str(axis) not in ("", "None", "0"):
            try:
                _av = int(float(axis))
                if _av != 0:
                    parts.append(f"AX {_av}°")
            except (ValueError, TypeError): pass

        if add is not None and str(add) not in ("", "None", "0", "0.0"):
            try:
                if float(add) > 0:
                    parts.append(f"ADD {float(add):+.2f}")
            except (ValueError, TypeError): pass
    except Exception as _e:
        pass
    return "  ".join(parts) if parts else ""



def _sync_supplier_orders_id_sequence() -> None:
    """Keep supplier_orders.id sequence ahead of existing manual/imported IDs."""
    try:
        from modules.sql_adapter import run_write
        run_write("""
            SELECT setval(
                pg_get_serial_sequence('supplier_orders','id'),
                GREATEST((SELECT COALESCE(MAX(id), 0) FROM supplier_orders), 1),
                TRUE
            )
        """, {})
    except Exception as _e:
        log.warning("[prod_page] silent err: %s", _e)
