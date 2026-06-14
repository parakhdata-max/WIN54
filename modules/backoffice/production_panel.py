"""
Production Panel
================
Stage tracking UI driven by job_master + job_stage_events + job_stage_master.

GOVERNANCE:
- Stage transitions execute via advance_job_stage() DB function
  which validates allowed transitions from job_stage_transitions table.
  No transition logic in Python — DB is the source of truth.
- current_stage on job_master is always DB-derived
- Every advance is logged via event_logger

SCHEMA (from DB backup):
  job_master       : id(uuid), order_line_id(uuid), total_qty, blank_required_qty,
                     blank_allocated_qty, current_stage, reprocess_count, is_closed,
                     created_at, updated_at
  job_stage_events : id(uuid), job_id(uuid), stage_id(uuid), stage_code(varchar),
                     performed_by(uuid), department(varchar), remarks(text), created_at
  job_stage_master : id(uuid), stage_code, stage_name, sequence_order(int),
                     department, is_external(bool), sla_minutes(int)
  job_stage_transitions: id(uuid), from_stage_code, to_stage_code, allowed(bool)
  DB FUNCTION: advance_job_stage(p_job_id uuid, p_next_stage varchar, p_user_id uuid) -> text
"""

import streamlit as st
import logging
log = logging.getLogger(__name__)
from typing import Dict, List, Optional

from .event_logger import log_event, render_event_timeline, EventType
from modules.printing.internal_print_config import (
    CANON_DEFAULT_PAPER,
    CR80_H_MM,
    CR80_W_MM,
    TSC_LABEL_H_MM,
    TSC_LABEL_W_MM,
    css_size,
)


def _q(sql, params):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _fetch_stages_from_db():
    return _q(
        "SELECT id, stage_code, stage_name, sequence_order, department, sla_minutes "
        "FROM job_stage_master ORDER BY sequence_order ASC", {}
    )


def _fetch_allowed_next(current_stage, coating_path: str = ""):
    """
    Returns allowed next stages from DB transitions table.
    Uses coating engine only to filter when multiple branches exist
    (e.g. INSPECTION can branch to HARDCOAT_PICKED, COLOURING_PICKED, ARC_SENT, READY_FOR_PACK).
    DB is always the source of truth — coating engine only narrows the choice.
    """
    rows = _q(
        "SELECT to_stage_code FROM job_stage_transitions "
        "WHERE from_stage_code=%(s)s AND allowed=TRUE", {"s": current_stage}
    )
    all_next = [r["to_stage_code"] for r in rows]

    if not coating_path or len(all_next) <= 1:
        return all_next

    # Multiple options — filter by coating path
    try:
        from modules.backoffice.coating_engine import get_allowed_next_stages
        filtered = get_allowed_next_stages(current_stage, coating_path)
        valid = [s for s in filtered if s in all_next]
        if valid:
            return valid
    except Exception:
        pass

    return all_next


def _fetch_job_cards(order_id):
    """order_id = orders.order_no (text)"""
    rows = _q("""
        SELECT jm.id AS job_id, jm.order_line_id, jm.total_qty,
               jm.blank_required_qty, jm.blank_allocated_qty,
               jm.current_stage, jm.reprocess_count, jm.is_closed,
               jm.created_at, jm.updated_at,
               COALESCE(jm.coating_path, '') AS coating_path,
               ol.id AS id, ol.id AS line_id, ol.order_id,
               ol.eye_side, ol.sph, ol.cyl, ol.axis, ol.add_power,
               ol.qty, ol.price, ol.line_total, ol.category, ol.type,
               p.product_name, p.id AS product_id,
               p.index_value, p.coating, p.material,
               p.coating_type,
               ol.lens_params,
               ol.production_ref
        FROM job_master jm
        JOIN order_lines ol ON ol.id = jm.order_line_id
        JOIN orders o       ON o.id  = ol.order_id
        JOIN products p     ON p.id  = ol.product_id
        WHERE o.order_no = %(ono)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND ol.production_ref IS NOT NULL
        ORDER BY ol.eye_side, jm.created_at
    """, {"ono": order_id})

    # Detect + cache coating_path if not yet saved
    import json as _json
    # First, get services from order lines (most reliable signal)
    _order_services = _detect_order_services(order_id)
    _services_cp    = _order_services.get("coating_path", "UNCOATED")

    for row in rows:
        cp = row.get("coating_path") or ""
        if not cp:
            try:
                from modules.backoffice.coating_engine import (
                    detect_coating_path, save_coating_path_to_job
                )
                lp = row.get("lens_params") or {}
                if isinstance(lp, str):
                    try: lp = _json.loads(lp)
                    except Exception: lp = {}
                surf    = lp.get("surfacing_data") or {}
                blank_m = surf.get("blank_material") or ""
                cp = detect_coating_path(
                    row.get("product_name") or "",
                    blank_m,
                    row.get("coating_type") or ""
                )
                # If product-name detection gives UNCOATED but order has services,
                # prefer service-based detection
                if cp == "UNCOATED" and _services_cp != "UNCOATED":
                    cp = _services_cp
                row["coating_path"] = cp
                # Persist so next load is instant
                save_coating_path_to_job(str(row.get("order_line_id") or ""), cp)
            except Exception:
                row["coating_path"] = _services_cp or "UNCOATED"

        # Also attach order services so _render_eye_job can use them
        row["_order_services"] = _order_services

    return rows



def _fetch_stage_history(job_id):
    return _q("""
        SELECT jse.stage_code, jse.remarks, jse.created_at,
               jse.department, jsm.stage_name
        FROM job_stage_events jse
        LEFT JOIN job_stage_master jsm ON jsm.stage_code = jse.stage_code
        WHERE jse.job_id = %(j)s
        ORDER BY jse.created_at ASC
    """, {"j": job_id})


def _advance_stage(job_id, order_id, next_stage, remarks=""):
    try:
        from modules.sql_adapter import run_scalar, run_write as _rw_adv, run_query as _rq_adv

        # ── Reopen job if advancing from READY_FOR_PACK → fitting stages ──
        # DB function closes job at READY_FOR_PACK; fitting needs it reopened
        if next_stage == "FITTING_PENDING":
            _rw_adv(
                "UPDATE job_master SET is_closed = FALSE "
                "WHERE id = %(j)s::uuid AND current_stage = 'READY_FOR_PACK'",
                {"j": job_id}
            )

        result = run_scalar(
            "SELECT public.advance_job_stage(%(j)s::uuid, %(s)s, NULL::uuid)",
            {"j": job_id, "s": next_stage}
        )
        if result and str(result).startswith("ERROR"):
            st.error(f"Transition blocked: {result}")
            return False

        # Persist remarks to job_stage_events (DB function doesn't write remarks)
        if remarks and remarks.strip():
            try:
                _rw_adv(
                    "UPDATE job_stage_events SET remarks = %(r)s "
                    "WHERE ctid IN ("
                    "  SELECT ctid FROM job_stage_events "
                    "  WHERE job_id = %(j)s::uuid AND stage_code = %(s)s "
                    "  ORDER BY created_at DESC LIMIT 1"
                    ")",
                    {"j": job_id, "s": next_stage, "r": remarks.strip()}
                )
            except Exception:
                pass

        log_event(EventType.STAGE_ADVANCED, order_id=order_id,
                  details={"job_id": job_id, "stage": next_stage, "remarks": remarks}, source="user")

        # ── Auto-ready check: if all jobs closed → mark order READY ──────
        # Only fire at READY_FOR_PACK (no fitting) or FITTING_DONE
        if next_stage in ("READY_FOR_PACK", "FITTING_DONE"):
            try:
                from modules.sql_adapter import run_query as _rq_ar
                from modules.backoffice.coating_engine import check_and_auto_ready_order
                _oid_rows = _rq_ar(
                    "SELECT ol.order_id FROM job_master jm "
                    "JOIN order_lines ol ON ol.id = jm.order_line_id "
                    "WHERE jm.id = %(j)s::uuid LIMIT 1",
                    {"j": job_id}
                )
                if _oid_rows:
                    _oid = str(_oid_rows[0]["order_id"])
                    # Skip auto-ready at READY_FOR_PACK if fitting service exists
                    _skip = False
                    if next_stage == "READY_FOR_PACK":
                        _svc = _detect_order_services(order_id)
                        _skip = _svc.get("has_fitting", False)
                    if not _skip and check_and_auto_ready_order(_oid):
                        st.success("🎉 All jobs complete — Order automatically marked **READY for Billing**!")
            except Exception:
                pass

        return True
    except Exception as e:
        st.error(f"Stage advance error: {e}")
        return False


# ============================================================================
# SET BACK TO BACKOFFICE — shared module (production_rollback.py)
# ============================================================================
# _rollback_order_to_backoffice() and _render_set_back_panel() have been
# extracted to production_rollback.py so Supplier and External Lab pipelines
# can share the same logic without duplication.
from modules.backoffice.production_rollback import (
    rollback_order_to_backoffice as _rollback_order_to_backoffice,
    render_set_back_panel       as _render_set_back_panel_base,
)


def _render_set_back_panel(order: Dict) -> None:
    """In-house pipeline wrapper — calls shared render with route_label='In-house'."""
    _render_set_back_panel_base(order, route_label="In-house")




def _render_progress(stages, current_stage):
    codes = [s["stage_code"] for s in stages]
    cur   = codes.index(current_stage) if current_stage in codes else 0
    if not stages:
        return
    cols = st.columns(len(stages))
    for i, (col, stage) in enumerate(zip(cols, stages)):
        name = stage.get("stage_name", stage["stage_code"])
        with col:
            if i < cur:
                st.markdown(f"<div style='text-align:center;color:#10b981;font-size:1rem'>✓</div>"
                            f"<div style='text-align:center;font-size:0.6rem;color:#10b981'>{name}</div>",
                            unsafe_allow_html=True)
            elif i == cur:
                st.markdown(f"<div style='text-align:center;color:#f59e0b;font-size:1rem'>▶</div>"
                            f"<div style='text-align:center;font-size:0.6rem;color:#f59e0b;"
                            f"font-weight:700;background:#fef3c7;border-radius:3px;padding:1px 3px'>{name}</div>",
                            unsafe_allow_html=True)
            else:
                st.markdown(f"<div style='text-align:center;color:#d1d5db;font-size:1rem'>○</div>"
                            f"<div style='text-align:center;font-size:0.6rem;color:#9ca3af'>{name}</div>",
                            unsafe_allow_html=True)



# ── Order service detection ───────────────────────────────────────────────────

def _detect_order_services(order_no: str) -> dict:
    """
    Single query — reads all product/service lines on the order.
    Returns which coating/fitting services are present and derives coating_path.

    Returns:
      { has_colouring, has_hardcoat, has_arc, has_fitting, coating_path }
    """
    try:
        rows = _q("""
            SELECT LOWER(COALESCE(p.main_group,'')) AS mg,
                   LOWER(COALESCE(p.product_name,'')) AS pn,
                   LOWER(COALESCE(p.coating_type,'')) AS ct
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            JOIN products p ON p.id = ol.product_id
            WHERE o.order_no = %(ono)s
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """, {"ono": order_no})
    except Exception:
        rows = []

    has_col = has_hc = has_arc = has_fit = False
    for r in rows:
        combined = f"{r.get('mg','')} {r.get('pn','')} {r.get('ct','')}"
        if any(k in combined for k in ("colour","color","tint","photo","photosun","gradient")):
            has_col = True
        if any(k in combined for k in ("hardcoat","hard coat"," hc "," h/c","ultra hc","ultraHC")):
            has_hc = True
        if any(k in combined for k in ("arc","ar coat","anti refle","antiref")):
            has_arc = True
        if any(k in combined for k in ("fitting","fitter","frame fit")):
            has_fit = True

    if has_arc and has_hc:
        cp = "HARDCOAT_ARC"
    elif has_arc:
        cp = "ARC"
    elif has_col and has_hc:
        cp = "COLOURING_HC"
    elif has_col:
        cp = "COLOURING"
    elif has_hc:
        cp = "HARDCOAT"
    else:
        cp = "UNCOATED"

    return {"has_colouring": has_col, "has_hardcoat": has_hc,
            "has_arc": has_arc, "has_fitting": has_fit, "coating_path": cp}


def _has_fitting_service(order_no: str) -> bool:
    return _detect_order_services(order_no).get("has_fitting", False)


def _get_fitters() -> list[dict]:
    """Return list of {id, name} from fitters table. Empty list if table missing."""
    try:
        rows = _q("""
            SELECT id::text, fitter_name AS name FROM fitters
            WHERE is_active = TRUE ORDER BY name
        """, {})
        return [{"id": r["id"], "name": r["name"]} for r in rows] if rows else []
    except Exception:
        return []


def _get_fitting_types() -> list[dict]:
    """Return fitting/service types from the generic service master, with legacy fallback."""
    try:
        rows = _q("""
            SELECT service_code AS code, service_name AS label
            FROM service_types
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND UPPER(service_group) = 'FITTING'
            ORDER BY sort_order, service_name
        """, {})
        if rows:
            return [{"code": r["code"], "label": r["label"]} for r in rows]
    except Exception:
        pass
    try:
        rows = _q("""
            SELECT code, label FROM fitting_types
            WHERE is_active = TRUE ORDER BY sort_order
        """, {})
        return [{"code": r["code"], "label": r["label"]} for r in rows] if rows else []
    except Exception:
        return []


def _get_fitter_rate(fitter_id: str, fitting_type_code: str) -> float:
    """Lookup provider/fitter purchase rate from service master, then legacy chart."""
    try:
        rows = _q("""
            SELECT purchase_rate AS rate
            FROM service_provider_rates
            WHERE provider_id = %(fid)s::uuid
              AND service_code = %(ftc)s
              AND COALESCE(is_active, TRUE) = TRUE
              AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
            ORDER BY effective_from DESC LIMIT 1
        """, {"fid": fitter_id, "ftc": fitting_type_code})
        if rows:
            return float(rows[0]["rate"] or 0)
    except Exception:
        pass
    try:
        rows = _q("""
            SELECT rate FROM fitter_rate_chart
            WHERE fitter_id = %(fid)s::uuid
              AND fitting_type_code = %(ftc)s
              AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
            ORDER BY effective_from DESC LIMIT 1
        """, {"fid": fitter_id, "ftc": fitting_type_code})
        return float(rows[0]["rate"]) if rows else 0.0
    except Exception:
        return 0.0


def _create_fitting_assignment(order_no, order_line_id, job_id, eye_side,
                                fitter_id, fitting_type_code, rate, remarks) -> bool:
    """Insert or update fitting_assignment for this job."""
    return bool(_q("""
        INSERT INTO fitting_assignments
            (order_no, order_line_id, job_master_id, eye_side,
             fitter_id, fitting_type_code, rate_applied, remarks,
             status, payment_status)
        VALUES
            (%(ono)s, %(lid)s::uuid, %(jid)s::uuid, %(eye)s,
             %(fid)s::uuid, %(ftc)s, %(rate)s, %(rmk)s,
             'PENDING', 'UNPAID')
        ON CONFLICT DO NOTHING
        RETURNING id
    """, {
        "ono": order_no, "lid": order_line_id, "jid": job_id, "eye": eye_side,
        "fid": fitter_id, "ftc": fitting_type_code, "rate": rate,
        "rmk": remarks or None
    }))


def _get_colouring_photo(job: dict):
    try:
        import json as _jcp
        lp = job.get("lens_params") or {}
        if isinstance(lp, str):
            try: lp = _jcp.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}
        return (lp.get("surfacing_data") or {}).get("colour_final_photo") or                lp.get("colour_final_photo")
    except Exception:
        return None


def _save_colouring_photo(order_line_id: str, b64: str) -> bool:
    try:
        import json as _jsp
        from modules.sql_adapter import run_query as _rqp, run_write as _rwp
        rows = _rqp(
            "SELECT lens_params FROM order_lines WHERE id=%(l)s::uuid LIMIT 1",
            {"l": order_line_id}
        )
        lp = {}
        if rows:
            raw = rows[0].get("lens_params") or {}
            if isinstance(raw, str):
                try: raw = _jsp.loads(raw)
                except Exception as e:
                    log.debug("Could not parse raw JSON: %s", e)
                    raw = {}
            lp = raw if isinstance(raw, dict) else {}
        lp["colour_final_photo"] = b64
        _rwp("UPDATE order_lines SET lens_params=%(lp)s::jsonb WHERE id=%(l)s::uuid",
             {"lp": _jsp.dumps(lp), "l": order_line_id})
        return True
    except Exception:
        return False


def _get_service_context(order_no: str, service_group: str) -> dict:
    """Return latest production-service line details for an order."""
    try:
        import json as _json
        rows = _q("""
            SELECT COALESCE(p.product_name, ol.lens_params->>'service_description', 'Service') AS product_name,
                   ol.lens_params,
                   ol.lens_params->>'service_code' AS service_code,
                   ol.lens_params->>'service_description' AS service_description,
                   ol.lens_params->>'service_instruction' AS service_instruction,
                   ol.lens_params->>'colour_sample_photo' AS colour_sample_photo,
                   ol.lens_params->>'colour_sample_filename' AS colour_sample_filename,
                   ol.lens_params->>'suggested_provider_name' AS suggested_provider_name,
                   ol.lens_params->>'suggested_provider_phone' AS suggested_provider_phone
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE o.order_no = %(ono)s
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = TRUE
              AND UPPER(COALESCE(ol.lens_params->>'charge_type', ol.lens_params->>'service_group', '')) = %(grp)s
            ORDER BY ol.id DESC
            LIMIT 1
        """, {"ono": order_no, "grp": str(service_group or "").upper()})
        if not rows:
            return {}
        out = dict(rows[0])
        lp = out.get("lens_params") or {}
        if isinstance(lp, str):
            try:
                lp = _json.loads(lp)
            except Exception:
                lp = {}
        out["lens_params"] = lp if isinstance(lp, dict) else {}
        if not out.get("suggested_provider_phone") and out.get("service_code"):
            try:
                from modules.backoffice.service_master import suggested_provider_for_service
                sp = suggested_provider_for_service(out.get("service_code")) or {}
                out["suggested_provider_name"] = out.get("suggested_provider_name") or sp.get("provider_name")
                out["suggested_provider_phone"] = out.get("suggested_provider_phone") or sp.get("contact")
            except Exception:
                pass
        return out
    except Exception:
        return {}


def render_production_panel(order: Dict) -> None:
    order_id = order.get("order_no") or str(order.get("id", ""))

    # ── Quick rollback banner (shown before tabs, always visible) ─────────
    # Gives production supervisors one-click access without hunting through tabs.
    _render_set_back_panel(order)

    _t1, _t2, _t3, _t4, _t5 = st.tabs([
        "🔧 Job Card & Blanks",
        "🏷️ Print Labels",
        "💳 Authenticity Card",
        "🏭 Job Tracking",
        "📬 Envelope Labels",
    ])

    with _t1:
        _render_job_card_tab(order)

    with _t2:
        _render_labels_tab(order)

    with _t3:
        _render_cr80_card_tab(order)

    with _t4:
        _render_job_tracking_tab(order, order_id)

    with _t5:
        _render_label_print_tab(order)


def _render_job_card_tab(order: Dict) -> None:
    """
    Job Card & Blank Selection — clean flow:
      1. RIGHT EYE  — blank selection form (no internal buttons)
      2. LEFT EYE   — blank selection form (no internal buttons)
      3. ONE save button + print buttons after save
    """
    try:
        from modules.documents.job_card_surfacing import (
            render_surfacing_job_card,
            save_job_card_line,
            render_job_card_print_pair,
            render_job_card_print,
        )
    except Exception as e:
        st.error(f"Failed to load job card module: {e}")
        return

    _jc_order = {
        "id":           order.get("id", ""),
        "order_no":     order.get("order_no", ""),
        "patient_name": order.get("patient_name", ""),
        "party_name":   order.get("party_name", ""),
        "order_type":   order.get("order_type") or "RETAIL",
        "lines":        order.get("lines", []),
    }

    # ── Resolve R and L lines ──────────────────────────────────────────
    all_lines = order.get("lines") or []

    def _has_surf(line):
        """Check surfacing_data in line dict OR inside lens_params in DB."""
        if line.get("surfacing_data"):
            return True
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            import json as _j
            try: lp = _j.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}
        return bool(lp.get("surfacing_data"))

    def _get_surf(line):
        """Return surfacing_data from line or lens_params."""
        if line.get("surfacing_data"):
            return line["surfacing_data"]
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            import json as _j
            try: lp = _j.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}
        sd = lp.get("surfacing_data") or {}
        if sd:
            line["surfacing_data"] = sd   # cache on line for print functions
        return sd

    _r_line = next((l for l in all_lines if str(l.get("eye_side","")).upper()[:1] == "R"), None)
    _l_line = next((l for l in all_lines if str(l.get("eye_side","")).upper()[:1] == "L"), None)

    if not _r_line and not _l_line:
        st.warning("No lens lines found for this order.")
        return

    # Pre-load surfacing_data into line dicts from DB
    for _ln in [_r_line, _l_line]:
        if _ln:
            _get_surf(_ln)

    _r_saved = _has_surf(_r_line) if _r_line else False
    _l_saved = _has_surf(_l_line) if _l_line else False
    _both    = _r_saved and _l_saved
    _any     = _r_saved or _l_saved

    # ── Status bar ────────────────────────────────────────────────────
    _sb_parts = []
    if _r_line:
        _sb_parts.append(f"<span style='color:{'#4ade80' if _r_saved else '#f59e0b'};font-weight:700'>"
                         f"{'✅' if _r_saved else '⏳'} RE</span>")
    if _l_line:
        _sb_parts.append(f"<span style='color:{'#60a5fa' if _l_saved else '#f59e0b'};font-weight:700'>"
                         f"{'✅' if _l_saved else '⏳'} LE</span>")
    st.markdown(
        "<div style='display:flex;gap:16px;padding:6px 0 10px'>"
        + "  ·  ".join(_sb_parts) + "</div>",
        unsafe_allow_html=True
    )

    # ── RIGHT EYE ────────────────────────────────────────────────────
    if _r_line:
        st.markdown(
            "<div style='background:#0d2818;border-left:4px solid #4ade80;"
            "border-radius:0 8px 8px 0;padding:6px 14px;margin-bottom:8px'>"
            "<span style='color:#4ade80;font-weight:700'>👁 RIGHT EYE</span></div>",
            unsafe_allow_html=True)
        if _r_saved:
            surf = _get_surf(_r_line)
            st.success(
                f"✅ Saved — {surf.get('blank_brand','')} {surf.get('blank_material','')} "
                f"| Base {float(surf.get('base_curve') or 0):.2f}D "
                f"| SPH {float(surf.get('sph_surf') or 0):+.2f}"
            )
        else:
            from modules.documents.job_card_surfacing import render_surfacing_job_card
            render_surfacing_job_card(_r_line, _jc_order)
    else:
        # Single-eye order — R not required
        st.markdown(
            "<div style='background:#0d2818;border-left:4px solid #374151;"
            "border-radius:0 8px 8px 0;padding:6px 14px;margin-bottom:8px;opacity:0.45'>"
            "<span style='color:#6b7280;font-weight:700'>👁 RIGHT EYE — Not required for this order</span>"
            "</div>",
            unsafe_allow_html=True)

    st.markdown("---")

    # ── LEFT EYE ─────────────────────────────────────────────────────
    if _l_line:
        st.markdown(
            "<div style='background:#0d1f2e;border-left:4px solid #60a5fa;"
            "border-radius:0 8px 8px 0;padding:6px 14px;margin-bottom:8px'>"
            "<span style='color:#60a5fa;font-weight:700'>👁 LEFT EYE</span></div>",
            unsafe_allow_html=True)
        if _l_saved:
            surf = _get_surf(_l_line)
            st.success(
                f"✅ Saved — {surf.get('blank_brand','')} {surf.get('blank_material','')} "
                f"| Base {float(surf.get('base_curve') or 0):.2f}D "
                f"| SPH {float(surf.get('sph_surf') or 0):+.2f}"
            )
        else:
            from modules.documents.job_card_surfacing import render_surfacing_job_card, _line_key

            # ── Same-as-R checkbox (only when R exists and is not yet saved) ──
            if _r_line:
                _same_r_key = f"jc_same_as_r_{order.get('order_no','')}"
                _same_as_r  = st.checkbox(
                    "✅ Use same blank as Right Eye",
                    value=st.session_state.get(_same_r_key, False),
                    key=_same_r_key,
                    help="Copies Brand, Material, Add, Base from Right Eye. "
                         "Surfacing calculations still use Left Eye prescription.",
                )
                if _same_as_r and not _r_saved:
                    _rk = _line_key(_r_line)
                    _lk = _line_key(_l_line)
                    for _rkey in [
                        f"material_{_rk}", f"add_{_rk}", f"brand_{_rk}",
                        f"colour_{_rk}",   f"jc_base_pre_{_rk}",
                        f"jc_base_pre_sel_{_rk}",
                    ]:
                        _lkey = _rkey.replace(_rk, _lk, 1)
                        if _rkey in st.session_state:
                            st.session_state[_lkey] = st.session_state[_rkey]
                    st.caption(
                        "↳ Left Eye pre-filled from Right Eye. "
                        "All fields still editable — surfacing uses L prescription."
                    )
                elif _same_as_r and _r_saved:
                    _r_surf = _get_surf(_r_line)
                    _lk     = _line_key(_l_line)
                    if _r_surf.get("blank_material"):
                        st.session_state[f"material_{_lk}"] = _r_surf["blank_material"]
                    if _r_surf.get("blank_brand"):
                        st.session_state[f"brand_{_lk}"]   = _r_surf["blank_brand"]
                    _base_val = _r_surf.get("base_curve")
                    if _base_val:
                        try:
                            _bv = round(float(_base_val), 2)
                            st.session_state[f"jc_base_pre_{_lk}"]     = _bv
                            st.session_state[f"jc_base_pre_sel_{_lk}"] = _bv
                        except (TypeError, ValueError):
                            pass
                    st.caption(
                        f"↳ Copying from saved Right Eye: "
                        f"{_r_surf.get('blank_brand','')} {_r_surf.get('blank_material','')} "
                        f"Base {float(_r_surf.get('base_curve') or 0):.1f}D"
                    )
            render_surfacing_job_card(_l_line, _jc_order)
    else:
        # Single-eye order — L not required
        st.markdown(
            "<div style='background:#0d1f2e;border-left:4px solid #374151;"
            "border-radius:0 8px 8px 0;padding:6px 14px;margin-bottom:8px;opacity:0.45'>"
            "<span style='color:#6b7280;font-weight:700'>👁 LEFT EYE — Not required for this order</span>"
            "</div>",
            unsafe_allow_html=True)

    st.markdown("---")

    # ── SAVE BOTH button — convenience shortcut ───────────────────────
    # Shows when both eyes are present and neither is saved yet.
    # Clicking triggers the individual save buttons via session state flags.
    _both_pending = (
        _r_line and not _r_saved and
        _l_line and not _l_saved
    )
    if _both_pending:
        _save_both_key = f"jc_save_both_{order.get('order_no','')}"
        if st.button(
            "💾 Save R & L",
            key=_save_both_key,
            type="primary",
            use_container_width=True,
            help="Saves both Right and Left eye job cards in one click.",
        ):
            from modules.documents.job_card_surfacing import save_job_card_line
            _msgs = []
            _ok_count = 0
            for _line, _label in ((_r_line, "R"), (_l_line, "L")):
                if _line and not _has_surf(_line):
                    _ok, _msg = save_job_card_line(_line, _jc_order)
                    if _ok:
                        _ok_count += 1
                        _msgs.append(f"✅ {_label}: {_msg}")
                    else:
                        _msgs.append(f"❌ {_label}: {_msg}")
            for _m in _msgs:
                if _m.startswith("✅"):
                    st.success(_m)
                else:
                    st.error(_m)
            if _ok_count:
                st.rerun()
    elif _r_line and not _r_saved and not _l_line:
        st.caption("↳ Single-eye order — use Save button inside the Right Eye form above.")
    elif _l_line and not _l_saved and not _r_line:
        st.caption("↳ Single-eye order — use Save button inside the Left Eye form above.")

    # ── Save hint ──────────────────────────────────────────────────────
    _needs_save = (
        (_r_line and not _r_saved) or
        (_l_line and not _l_saved)
    )
    if _needs_save and not _both_pending:
        st.info("👆 Fill the form above and click 💾 Save inside the eye form.")
    
    # ── PRINT BUTTONS — only after at least one saved ──────────────────
    if _any:
        _print_key = f"prod_print_{order.get('order_no','')}"
        _bar_key   = f"prod_bar_{order.get('order_no','')}"
        _cr80_key  = f"prod_cr80_{order.get('order_no','')}"

        # ── Universal print buttons — adapt to what's saved ────────
        _saved_lines = [l for l in [_r_line, _l_line] if l and _has_surf(l)]
        for _sl in _saved_lines:
            _get_surf(_sl)   # ensure surfacing_data loaded into line dict
        _missing_alloc = set(_missing_blank_assignments_for_print({
            **order,
            "lines": _saved_lines,
        }))
        _printable_lines = [
            l for l in _saved_lines
            if str(l.get("eye_side") or "").upper()[:1] not in _missing_alloc
        ]
        _can_print_production = bool(_printable_lines) and not _missing_alloc
        if _missing_alloc:
            st.error(
                "🔴 Assignment not done — assign blank first for "
                f"{'/'.join(sorted(_missing_alloc))} eye before printing job card or labels."
            )

        _combo_key = f"print_combo_{order.get('order_no','')}"
        _bc0, _bc1, _bc2, _bc3 = st.columns(4)
        with _bc0:
            if st.button("🖨️ Job + Labels",
                         use_container_width=True,
                         type="primary",
                         key="prod_print_combo",
                         help="Single command: Canon job card + TSC R/L barcode labels",
                         disabled=not _can_print_production):
                st.session_state[_combo_key] = True
                st.rerun()
        with _bc1:
            if st.button("🖨️ Print Job Card",
                         use_container_width=True,
                         type="secondary",
                         key="prod_print_jc",
                         disabled=not _can_print_production):
                st.session_state[_print_key] = True
                st.rerun()
        with _bc2:
            if st.button("🏷️ Print Label(s)",
                         use_container_width=True,
                         type="primary",
                         key="prod_print_bar",
                         help="75×50mm barcode label — R, L, or both",
                         disabled=not _can_print_production):
                st.session_state[_bar_key] = True
                st.rerun()
        with _bc3:
            if st.button("💳 Customer Card",
                         use_container_width=True,
                         key="prod_print_cr80",
                         help="85×54mm CR80 prescription card"):
                st.session_state[_cr80_key] = True
                st.rerun()

        # Print previews
        if st.session_state.get(_combo_key):
            st.session_state.pop(_combo_key, None)
            if _missing_alloc:
                st.error("🔴 Assignment not done — save blank assignment before printing job card/labels.")
                return
            ok, msg = _print_tsc_production_labels(_saved_lines, _jc_order)
            if ok:
                st.success("Sent label(s) to TSC")
            else:
                st.warning(f"TSC direct print failed: {msg}. Opening HTML standby.")
                _open_print_window(_build_label_page(_saved_lines, _jc_order))
            from modules.documents.job_card_surfacing import _open_jc_print_window
            _open_jc_print_window(
                _r_line if _r_saved else None,
                _l_line if _l_saved else None,
                _jc_order
            )

        if st.session_state.get(_print_key):
            st.session_state.pop(_print_key, None)
            from modules.documents.job_card_surfacing import _open_jc_print_window
            if _missing_alloc:
                st.error("🔴 Assignment not done — save blank assignment before printing job card.")
                return
            _open_jc_print_window(
                _r_line if _r_saved else None,
                _l_line if _l_saved else None,
                _jc_order
            )

        if st.session_state.get(_bar_key):
            st.session_state.pop(_bar_key, None)
            if _missing_alloc:
                st.error("🔴 Assignment not done — save blank assignment before printing labels.")
                return
            ok, msg = _print_tsc_production_labels(_saved_lines, _jc_order)
            if ok:
                st.success("Sent label(s) to TSC")
            else:
                st.warning(f"TSC direct print failed: {msg}. Opening HTML standby.")
                _open_print_window(_build_label_page(_saved_lines, _jc_order))

        if st.session_state.get(_cr80_key):
            st.session_state.pop(_cr80_key, None)
            _open_print_window(_build_cr80_page(
                _r_line if _r_saved else None,
                _l_line if _l_saved else None,
                _jc_order
            ))



# ══════════════════════════════════════════════════════════════
# PRINT FUNCTIONS — Label (75×50mm) + CR80 Customer Card
# ══════════════════════════════════════════════════════════════

def _fp(v, default="—"):
    """Format power value with sign."""
    if v is None: return default
    try:
        n = float(v)
        if n == 0.0: return "0.00"
        return f"+{n:.2f}" if n > 0 else f"{n:.2f}"
    except Exception as e:
        log.debug("Display formatting fallback: %s", e)
        return str(v)


def _make_barcode_html(value: str, height: int = 40, width: int = 260) -> str:
    """Generate a real Code128 barcode with quiet zone for scanner readability."""
    value = "".join(c for c in str(value or "") if c.isalnum()) or "UNKNOWN"
    try:
        from modules.printing.patient_card_printer import barcode_svg as _bsvg
        svg = _bsvg(value, width=width, height=height)
        return (
            "<div style='display:inline-block;background:#fff;padding:1mm 2mm;"
            "line-height:0;overflow:visible;max-width:100%;box-sizing:border-box'>"
            f"{svg}</div>"
        )
    except Exception:
        return (
            f"<div style='font-family:Courier New,monospace;font-size:12pt;"
            f"font-weight:900;color:#000;background:#fff;text-align:center'>{value}</div>"
        )


def _resolve_label_line(line: dict, order: dict, eye: str) -> dict:
    """Fetch the real order_line for print when UI passed a grouped placeholder row."""
    import json as _json

    out = dict(line or {})
    eye1 = str(eye or out.get("eye_side") or "").upper()[:1]
    order_no = str(order.get("order_no") or out.get("order_no") or "").strip()
    parent_order_no = order_no.rsplit("-", 1)[0] if order_no.upper().endswith(("-F", "-C")) else ""
    line_id = str(out.get("order_line_id") or out.get("line_id") or out.get("id") or "").strip()

    is_placeholder = (
        "job(s)" in str(out.get("product_name") or "").lower()
        or not out.get("product_name")
        or out.get("sph") is None
    )
    if not is_placeholder:
        lp = out.get("lens_params") or {}
        if isinstance(lp, str):
            try:
                lp = _json.loads(lp)
            except Exception:
                lp = {}
        out["lens_params"] = lp if isinstance(lp, dict) else {}
        out["surfacing_data"] = out.get("surfacing_data") or out["lens_params"].get("surfacing_data") or {}
        return out

    try:
        from modules.sql_adapter import run_query
        params = {"eye": eye1}
        where = [
            "COALESCE(ol.is_deleted,FALSE)=FALSE",
            "LEFT(UPPER(COALESCE(ol.eye_side,'')), 1) = %(eye)s",
        ]
        if line_id and "-" in line_id:
            where.append("ol.id=%(lid)s::uuid")
            params["lid"] = line_id
        elif order_no:
            if parent_order_no:
                where.append("(o.order_no=%(ono)s OR ol.production_ref=%(ono)s OR o.order_no=%(pono)s)")
                params["pono"] = parent_order_no
            else:
                where.append("(o.order_no=%(ono)s OR ol.production_ref=%(ono)s)")
            params["ono"] = order_no
        elif order.get("id"):
            where.append("o.id=%(oid)s::uuid")
            params["oid"] = str(order.get("id"))
        else:
            return out

        rows = run_query(
            f"""
            SELECT ol.id::text AS id, ol.id::text AS line_id, ol.order_id::text AS order_id,
                   o.order_no, o.patient_name, o.party_name, o.order_type,
                   ol.eye_side, ol.sph, ol.cyl, ol.axis, ol.add_power,
                   COALESCE(p.category, '') AS category,
                   '' AS type,
                   ol.lens_params, ol.production_ref,
                   COALESCE(p.product_name, ol.lens_params->>'product_name', '') AS product_name,
                   COALESCE(p.brand, '') AS brand,
                   p.index_value, p.coating, p.coating_type, p.material
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE WHEN ol.production_ref IS NOT NULL THEN 0 ELSE 1 END, ol.id
            LIMIT 1
            """,
            params,
        ) or []
        if rows:
            out.update(dict(rows[0]))
    except Exception as e:
        log.debug("Label print line resolver fallback: %s", e)

    lp = out.get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = _json.loads(lp)
        except Exception:
            lp = {}
    out["lens_params"] = lp if isinstance(lp, dict) else {}
    out["surfacing_data"] = out.get("surfacing_data") or out["lens_params"].get("surfacing_data") or {}
    return out


def _product_display_for_card(line: dict) -> str:
    """Product text used by customer cards: name + index + coating."""
    line = line or {}
    lp = line.get("lens_params") or {}
    if isinstance(lp, str):
        import json as _json
        try:
            lp = _json.loads(lp)
        except Exception:
            lp = {}
    if not isinstance(lp, dict):
        lp = {}

    def _txt(*vals):
        for v in vals:
            s = str(v or "").strip()
            if s and s.lower() not in ("none", "null", "nan", "-", "—"):
                return s
        return ""

    name = _txt(line.get("product_name"), lp.get("product_name"), lp.get("display_product_name"))
    brand = _txt(
        line.get("brand"),
        line.get("brand_name"),
        line.get("selected_brand"),
        lp.get("brand"),
        lp.get("brand_name"),
        lp.get("selected_brand"),
    )
    idx = _txt(
        line.get("index_value"),
        line.get("lens_index"),
        lp.get("index_value"),
        lp.get("lens_index"),
        lp.get("index"),
        lp.get("Lens Index"),
    )
    coating = _txt(
        line.get("coating"),
        line.get("coating_type"),
        lp.get("coating"),
        lp.get("coating_type"),
        lp.get("lens_coating"),
    )
    parts = [name or "—"]
    upper_name = (name or "").upper()
    if idx:
        parts.append(f"Index {idx}")
    if coating and coating.upper() not in upper_name:
        parts.append(coating)
    return " | ".join(parts)


def _order_print_names(order: dict, line: dict | None = None) -> dict:
    """Resolve customer and optician/party names for labels/cards."""
    import json as _json

    line = line or {}
    enriched = dict(order or {})
    order_no = str(enriched.get("order_no") or line.get("order_no") or "").strip()
    parent_order_no = order_no.rsplit("-", 1)[0] if order_no.upper().endswith(("-F", "-C")) else ""
    order_id = str(enriched.get("id") or line.get("order_id") or "").strip()

    if not enriched.get("extra_data") and (order_no or order_id):
        try:
            from modules.sql_adapter import run_query
            params = {}
            where = []
            if order_no:
                if parent_order_no:
                    where.append("(o.order_no=%(ono)s OR ol.production_ref=%(ono)s OR o.order_no=%(pono)s)")
                    params["pono"] = parent_order_no
                else:
                    where.append("(o.order_no=%(ono)s OR ol.production_ref=%(ono)s)")
                params["ono"] = order_no
            elif order_id:
                where.append("o.id=%(oid)s::uuid")
                params["oid"] = order_id
            rows = run_query(
                f"""
                SELECT o.order_no, o.patient_name, o.patient_mobile, o.party_name, o.order_type, o.extra_data
                FROM orders o
                LEFT JOIN order_lines ol ON ol.order_id = o.id
                WHERE {' AND '.join(where)}
                LIMIT 1
                """,
                params,
            ) or []
            if rows:
                for k, v in dict(rows[0]).items():
                    if v not in (None, ""):
                        enriched[k] = v
        except Exception as e:
            log.debug("Order print name resolver failed: %s", e)

    extra = enriched.get("extra_data") or {}
    if isinstance(extra, str):
        try:
            extra = _json.loads(extra) if extra else {}
        except Exception:
            extra = {}
    if not isinstance(extra, dict):
        extra = {}
    end_customer = extra.get("end_customer") or {}
    if not isinstance(end_customer, dict):
        end_customer = {}

    order_type = str(enriched.get("order_type") or line.get("order_type") or "").upper()
    party = str(enriched.get("party_name") or line.get("party_name") or "").strip()
    patient = str(enriched.get("patient_name") or line.get("patient_name") or "").strip()
    end_name = str(end_customer.get("name") or end_customer.get("customer_name") or "").strip()
    end_mobile = str(end_customer.get("mobile") or end_customer.get("phone") or "").strip()

    def _real_customer_name(value: str) -> str:
        s = str(value or "").strip()
        return "" if s.lower() in ("end customer", "unknown", "none", "null", "-", "—") else s

    customer = _real_customer_name(end_name) or _real_customer_name(patient)
    optician = party if party and party != customer else ""

    return {
        "customer": customer,
        "mobile": end_mobile or str(enriched.get("patient_mobile") or line.get("patient_mobile") or "").strip(),
        "optician": optician,
        "party": party,
        "order_type": order_type or "RETAIL",
        "order": enriched,
    }


def _real_label_lines_for_order(lines_to_print: list, order: dict) -> list:
    """Replace compact placeholder rows with real R/L order lines for label printing."""
    lines = [dict(x or {}) for x in (lines_to_print or []) if x]

    def _is_bad_line(ln: dict) -> bool:
        return (
            not ln
            or "job(s)" in str(ln.get("product_name") or "").lower()
            or not ln.get("product_name")
            or ln.get("sph") is None
        )

    if lines and not any(_is_bad_line(ln) for ln in lines):
        return lines

    order_no = str(order.get("order_no") or "").strip()
    parent_order_no = order_no.rsplit("-", 1)[0] if order_no.upper().endswith(("-F", "-C")) else ""
    order_id = str(order.get("id") or "").strip()
    if not (order_no or order_id):
        return lines

    try:
        from modules.sql_adapter import run_query
        params = {}
        where = ["COALESCE(ol.is_deleted,FALSE)=FALSE"]
        if order_no:
            if parent_order_no:
                where.append("(o.order_no=%(ono)s OR ol.production_ref=%(ono)s OR o.order_no=%(pono)s)")
                params["pono"] = parent_order_no
            else:
                where.append("(o.order_no=%(ono)s OR ol.production_ref=%(ono)s)")
            params["ono"] = order_no
        else:
            where.append("o.id=%(oid)s::uuid")
            params["oid"] = order_id

        rows = run_query(
            f"""
            SELECT ol.id::text AS id, ol.id::text AS line_id, ol.order_id::text AS order_id,
                   o.order_no, o.patient_name, o.party_name, o.order_type,
                   ol.eye_side, ol.sph, ol.cyl, ol.axis, ol.add_power,
                   COALESCE(p.category, '') AS category,
                   '' AS type,
                   ol.lens_params, ol.production_ref,
                   COALESCE(p.product_name, ol.lens_params->>'product_name', '') AS product_name,
                   COALESCE(p.brand, '') AS brand,
                   p.index_value, p.coating, p.coating_type, p.material
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE {' AND '.join(where)}
              AND LEFT(UPPER(COALESCE(ol.eye_side,'')), 1) IN ('R','L')
            ORDER BY CASE LEFT(UPPER(COALESCE(ol.eye_side,'')), 1) WHEN 'R' THEN 1 WHEN 'L' THEN 2 ELSE 9 END,
                     CASE WHEN ol.production_ref IS NOT NULL THEN 0 ELSE 1 END,
                     ol.id
            """,
            params,
        ) or []
        out = []
        seen = set()
        for r in rows:
            eye = str(r.get("eye_side") or "").upper()[:1]
            if eye in seen:
                continue
            seen.add(eye)
            out.append(dict(r))
        if out:
            return out
    except Exception as e:
        log.debug("Real label line order resolver failed: %s", e)

    return lines


def _make_label_html(line: dict, order: dict, eye: str) -> str:
    """
    75×50mm barcode label for TSC-244 Pro.
    Layout:
      TOP LEFT: party/patient name + order_no + eye  |  TOP RIGHT: date
      PRODUCT name row
      POWER boxes: SPH | CYL | AXIS | ADD
      BARCODE 1: order number  |  BARCODE 2: party code
      BOTTOM: frame | KT/SV | shop name
    """
    import datetime as _dt

    line = _resolve_label_line(line, order, eye)

    surf = line.get("surfacing_data") or {}
    lp   = line.get("lens_params") or {}
    if isinstance(lp, str):
        import json as _jl
        try: lp = _jl.loads(lp)
        except Exception as e:
            log.debug("Could not parse lens_params: %s", e)
            lp = {}

    names = _order_print_names(order, line)
    order = names.get("order") or order
    order_no   = order.get("order_no", line.get("order_no", "")) or ""
    today      = _dt.date.today().strftime("%d-%m-%Y")
    order_type = names.get("order_type") or (order.get("order_type") or line.get("order_type") or "RETAIL").upper()
    customer_name = (names.get("customer") or "")[:30]
    optician_name = (names.get("optician") or "")[:30]
    party_name = customer_name

    def _fp_lbl(v):
        if v is None: return "&mdash;"
        try:
            n = float(v)
            if n == 0.0: return "0.00"
            return f"+{n:.2f}" if n > 0 else f"{n:.2f}"
        except Exception as e:
            log.debug("Display formatting fallback: %s", e)
            return str(v)

    sph_rx  = _fp_lbl(line.get("sph"))
    cyl_rx  = _fp_lbl(line.get("cyl"))
    _ax_raw = line.get("axis")
    axis_rx = str(int(float(_ax_raw))) if _ax_raw not in (None, "", 0, "0") else "&mdash;"
    add_rx  = _fp_lbl(line.get("add_power"))

    product = _product_display_for_card(line)[:90]
    category   = (line.get("category") or lp.get("manufacturing_route") or "").upper()
    frame_lp   = (lp.get("frame_type") or surf.get("frame_type") or "SUPRA").upper()
    eye_label  = "R" if eye.upper()[:1] == "R" else "L"
    blank_batch = surf.get("blank_batch") or ""

    _ono_clean  = "".join(c for c in order_no if c.isalnum())

    cat_short = "KT"  if ("KRYPTOK" in category or "KT" in category) else \
                "PAL" if "PROGRESSIVE" in category else \
                "SV"  if "SINGLE" in category else \
                (category[:4] if category else "")

    shop_name = "DV Optical"
    try:
        from modules.settings.shop_master import get_unit_info
        _sh = get_unit_info("retail")
        shop_name = _sh.get("shop_name", "DV Optical")
    except Exception:
        pass

    bc1_html = _make_barcode_html(_ono_clean + eye_label, height=32, width=320)
    optician_line = (
        f"<div style='font-size:5.8pt;font-weight:800;color:#334155'>Optician: {optician_name}</div>"
        if optician_name else ""
    )

    _pwr_boxes = "".join(
        f"<div style='border:1.5px solid #000;flex:1;text-align:center;padding:0.5mm 0'>"
        f"<div style='font-size:5.8pt;color:#111;font-weight:900'>{lbl}</div>"
        f"<div style='font-size:10pt;font-weight:900;font-family:monospace;line-height:1.15'>{val}</div>"
        f"</div>"
        for lbl, val in [("SPH", sph_rx), ("CYL", cyl_rx), ("AXIS", axis_rx), ("ADD", add_rx)]
    )

    return (
        f"<div style='width:{TSC_LABEL_W_MM}mm;height:{TSC_LABEL_H_MM}mm;border:1.2px solid #000;box-sizing:border-box;"
        f"font-family:Arial,Helvetica,sans-serif;overflow:hidden;page-break-after:always;"
        f"display:flex;flex-direction:column;padding:0;background:#fff'>"

        # TOP BAR — party/patient name + order + eye  |  date + batch
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start;"
        f"border-bottom:1px solid #000;padding:1mm 2mm .7mm'>"
        f"<div>"
        f"<div style='font-size:7.2pt;font-weight:900;letter-spacing:-.2px'>{customer_name}</div>"
        f"{optician_line}"
        f"<div style='font-size:6.2pt;font-weight:700'>{order_no} {eye_label}</div>"
        f"</div>"
        f"<div style='text-align:right'>"
        f"<div style='font-size:6pt;color:#555'>{today}</div>"
        + (f"<div style='border:1px solid #000;padding:0.5mm 1.5mm;font-size:7.5pt;font-weight:900;"
           f"margin-top:0.5mm;text-align:center'>{blank_batch}</div>" if blank_batch else "")
        + f"</div>"
        f"</div>"

        # PRODUCT NAME
        f"<div style='padding:.7mm 2mm .4mm;border-bottom:1px solid #ccc'>"
        f"<div style='font-size:6.8pt;font-weight:900;line-height:1.08'>- {product}</div>"
        f"</div>"

        # POWER BOXES
        f"<div style='display:flex;gap:.8mm;padding:1.2mm 2mm;border-bottom:1px solid #000'>"
        f"{_pwr_boxes}"
        f"</div>"

        # Single strong order barcode. Party/customer stays as text so scanner has enough width.
        f"<div style='padding:.7mm 2mm;border-bottom:1px solid #ccc;flex:1;"
        f"display:flex;align-items:center;justify-content:center;overflow:hidden'>"
        f"<div style='text-align:center;width:100%'>{bc1_html}</div>"
        f"</div>"

        # BOTTOM: frame | category | shop
        f"<div style='display:flex;justify-content:space-between;padding:.6mm 2mm'>"
        f"<div style='font-size:6pt;font-weight:700'>{frame_lp}</div>"
        f"<div style='font-size:6pt;font-weight:900'>{cat_short}</div>"
        f"<div style='font-size:6pt'>{shop_name}</div>"
        f"</div>"
        f"</div>"
    )


def _make_cr80_html(r_line: dict, l_line: dict, order: dict) -> str:
    """
    85×54mm customer card with R+L prescription.
    Matches reference image format: name table, product, order no, tagline.
    """
    import datetime as _dt

    _resolved_lines = _real_label_lines_for_order([x for x in (r_line, l_line) if x], order)
    if _resolved_lines:
        r_line = next((x for x in _resolved_lines if str(x.get("eye_side") or "").upper()[:1] == "R"), r_line)
        l_line = next((x for x in _resolved_lines if str(x.get("eye_side") or "").upper()[:1] == "L"), l_line)

    if r_line:
        r_line = _resolve_label_line(r_line, order, "R")
    if l_line:
        l_line = _resolve_label_line(l_line, order, "L")

    names = _order_print_names(order, r_line or l_line)
    order = names.get("order") or order
    order_no  = order.get("order_no", "—")
    def _card_customer_name(value) -> str:
        s = str(value or "").strip()
        return "" if s.lower() in ("end customer", "unknown", "none", "null", "-", "—") else s

    patient   = _card_customer_name(names.get("customer")) or _card_customer_name(order.get("patient_name"))
    optician  = names.get("optician") or ""
    today     = _dt.date.today().strftime("%d-%m-%Y")

    product = _product_display_for_card(r_line or l_line)[:78]

    shop_name = "DV Optical"
    tagline   = "See Clearly, Check Regularly"
    try:
        from modules.settings.shop_master import get_unit_info
        _sh = get_unit_info("retail")
        shop_name = _sh.get("shop_name", shop_name)
        tagline   = _sh.get("tagline", tagline) or tagline
    except Exception:
        pass

    order_code = "".join(c for c in str(order_no) if c.isalnum())
    order_bc = _make_barcode_html(order_code, height=22, width=250)

    def _row(line, eye_label):
        line = line or {}
        return (
            f"<tr>"
            f"<td>{eye_label}</td>"
            f"<td>{_fp(line.get('sph'))}</td>"
            f"<td>{_fp(line.get('cyl'))}</td>"
            f"<td>{int(float(line.get('axis') or 0)) if line else '—'}</td>"
            f"<td>{_fp(line.get('add_power'))}</td>"
            f"</tr>"
        )

    r_row = _row(r_line, "R") if r_line else ""
    l_row = _row(l_line, "L")  if l_line else ""

    return f"""
<div style="width:{CR80_W_MM}mm;height:{CR80_H_MM}mm;border:.45mm solid #000;box-sizing:border-box;
     font-family:Arial,Helvetica,sans-serif;page-break-after:always;
     display:flex;flex-direction:column;overflow:hidden;background:#fff;color:#000;padding:2.2mm 3mm">

  <div style="display:flex;justify-content:space-between;gap:2mm;border-bottom:.35mm solid #000;padding-bottom:1mm">
    <div style="font-size:5.8pt;font-weight:900;text-transform:uppercase;letter-spacing:.1em">Authenticity Card</div>
    <div style="font-size:6pt;font-weight:900;text-align:right">{today}</div>
  </div>

  <div style="font-size:11.3pt;font-weight:900;line-height:1.05;padding:1mm 0 .25mm;min-height:6.5mm;
       overflow:hidden">{patient}</div>
  <div style="font-size:6.6pt;font-weight:900;color:#222;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
       margin-bottom:.6mm">{f"Optician: {optician}" if optician else ""}</div>

  <div style="font-size:6.1pt;font-weight:900;line-height:1.1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
       border-bottom:.25mm solid #999;padding-bottom:.7mm">{product}</div>

  <table style="width:100%;border-collapse:collapse;font-size:7pt;margin-top:.8mm">
    <tr>
      <th></th><th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th>
    </tr>
    {r_row}{l_row}
  </table>

  <div style="margin-top:auto;display:flex;align-items:flex-end;justify-content:space-between;gap:2mm;
       border-top:.25mm solid #999;padding-top:.6mm">
    <div style="line-height:0">{order_bc}</div>
    <div style="font-size:5.5pt;font-weight:900;text-align:right;max-width:30mm;line-height:1.15">
      {order_no}
    </div>
  </div>

  <div style="background:#000;color:#fff;text-align:center;margin:.7mm -3mm 0;
       padding:.85mm 0;font-size:7.4pt;font-weight:900;letter-spacing:.03em">{tagline}</div>
</div>
"""


def _open_print_window(html: str) -> None:
    """Open HTML in new tab for printing."""
    import streamlit.components.v1 as _comp
    import base64 as _b64
    _b64_html = _b64.b64encode(html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{"
        f"var _raw=atob('{_b64_html}');"
        f"var _buf=new Uint8Array(_raw.length);"
        f"for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}"
        f"var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});"
        f"window.open(URL.createObjectURL(b),'_blank')"
        f"}})();</script>",
        height=0
    )


def _build_label_page(lines_to_print: list, order: dict) -> str:
    """
    Build full printable HTML page — one 75×40mm label per page.
    TSC-244 Pro thermal printer: @page size 75×50mm, one label per page.
    R and L print on separate pages (page-break-after:always on each label div).
    Screen preview shows both labels stacked for review before printing.
    """
    lines_to_print = _real_label_lines_for_order(lines_to_print, order)
    labels_html = "".join(
        _make_label_html(ln, order, str(ln.get("eye_side","R")).upper())
        for ln in lines_to_print
    )
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
  @page {{ size: {css_size(TSC_LABEL_W_MM, TSC_LABEL_H_MM)}; margin: 0; }}
  body {{ margin: 0; background: #f5f5f5; font-family: Arial,Helvetica,sans-serif; }}
  @media print {{
    body {{ background: #fff; }}
    .no-print {{ display: none !important; }}
  }}
</style></head><body>
{labels_html}
<div class='no-print' style='text-align:center;padding:16px;background:#f5f5f5'>
  <p style='font-size:11px;color:#666;margin:0 0 8px'>
    TSC-244 Pro &mdash; 75&times;50mm labels &mdash; one label per page
  </p>
  <button onclick="document.querySelectorAll('.no-print').forEach(e=>e.style.display='none');window.print();setTimeout(()=>document.querySelectorAll('.no-print').forEach(e=>e.style.display=''),800)"
    style="background:#2563eb;color:#fff;border:none;padding:10px 24px;border-radius:6px;
           font-size:14px;cursor:pointer;font-weight:700">
    Print Label(s)
  </button>
</div>
</body></html>"""


def _print_tsc_production_labels(lines_to_print: list, order: dict) -> tuple[bool, str]:
    """Print production R/L labels using TSC raw TSPL barcode commands."""
    import datetime as _dt
    try:
        import importlib, modules.printing.label_printer as _lp_mod
        importlib.reload(_lp_mod)
        from modules.printing.label_printer import build_tsc_production_label, _send_tspl

        real_lines = _real_label_lines_for_order(lines_to_print, order)
        if not real_lines:
            return False, "No printable R/L line found"

        chunks = []
        for ln in real_lines:
            eye = str(ln.get("eye_side") or "R").upper()[:1] or "R"
            ln = _resolve_label_line(ln, order, eye)
            names = _order_print_names(order, ln)
            lp = ln.get("lens_params") or {}
            if isinstance(lp, str):
                import json as _json
                try:
                    lp = _json.loads(lp)
                except Exception:
                    lp = {}
            surf = ln.get("surfacing_data") or (lp.get("surfacing_data") if isinstance(lp, dict) else {}) or {}
            category = (ln.get("category") or (lp.get("manufacturing_route") if isinstance(lp, dict) else "") or "").upper()
            cat_short = "KT" if ("KRYPTOK" in category or "KT" in category) else (
                "PAL" if "PROGRESSIVE" in category else ("SV" if "SINGLE" in category else category[:4])
            )
            frame = (lp.get("frame_type") if isinstance(lp, dict) else "") or surf.get("frame_type") or "SUPRA"
            chunks.append(build_tsc_production_label(
                order_no=str((names.get("order") or order).get("order_no") or order.get("order_no") or ln.get("order_no") or ""),
                eye=eye,
                customer=names.get("customer") or "",
                optician=names.get("optician") or "",
                product=_product_display_for_card(ln),
                sph=ln.get("sph"),
                cyl=ln.get("cyl"),
                axis=ln.get("axis"),
                add=ln.get("add_power"),
                date_text=_dt.date.today().strftime("%d-%m-%Y"),
                frame=frame,
                category=cat_short,
                shop="Parakh Eye Care",
                copies=1,
            ))
        tspl = "\n".join(chunks)
        try:
            from pathlib import Path as _Path
            _debug_dir = _Path("generated_docs") / "debug"
            _debug_dir.mkdir(parents=True, exist_ok=True)
            (_debug_dir / "last_tsc_production_label.tspl").write_text(tspl, encoding="utf-8")
        except Exception:
            pass
        return _send_tspl(tspl)
    except Exception as exc:
        return False, str(exc)


def _build_cr80_page(r_line, l_line, order: dict) -> str:
    """Build full printable HTML page for CR80 customer card."""
    card_html = _make_cr80_html(r_line, l_line, order)
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
  @page {{ size: {css_size(CR80_W_MM, CR80_H_MM)}; margin: 0; }}
  body {{ margin: 0; background: #fff; }}
  th {{ background:#000;color:#fff;padding:.75mm 1.2mm;text-align:center;font-size:6pt;font-weight:900; }}
  td {{ border-bottom:.25mm solid #000;padding:.8mm 1.2mm;text-align:center;font-weight:900;color:#000; }}
  td:first-child {{ text-align:left; }}
  @media print {{ .no-print {{ display:none !important; }} }}
</style></head><body>
{card_html}
<div class='no-print' style='text-align:center;padding:16px'>
  <button onclick="document.querySelectorAll('.no-print').forEach(e=>e.style.display='none');window.print();setTimeout(()=>document.querySelectorAll('.no-print').forEach(e=>e.style.display=''),500)"
    style="background:#2563eb;color:#fff;border:none;padding:10px 24px;border-radius:6px;font-size:14px;cursor:pointer;font-weight:700">
    🖨️ Print Customer Card
  </button>
</div>
</body></html>"""


def _make_customer_75x50_html(r_line: dict, l_line: dict, order: dict) -> str:
    """75×50mm customer authenticity card with both R/L powers."""
    import datetime as _dt

    _resolved_lines = _real_label_lines_for_order([x for x in (r_line, l_line) if x], order)
    if _resolved_lines:
        r_line = next((x for x in _resolved_lines if str(x.get("eye_side") or "").upper()[:1] == "R"), r_line)
        l_line = next((x for x in _resolved_lines if str(x.get("eye_side") or "").upper()[:1] == "L"), l_line)

    if r_line:
        r_line = _resolve_label_line(r_line, order, "R")
    if l_line:
        l_line = _resolve_label_line(l_line, order, "L")

    names = _order_print_names(order, r_line or l_line)
    order = names.get("order") or order
    order_no = order.get("order_no", "—")
    def _card_customer_name(value) -> str:
        s = str(value or "").strip()
        return "" if s.lower() in ("end customer", "unknown", "none", "null", "-", "—") else s

    patient = _card_customer_name(names.get("customer")) or _card_customer_name(order.get("patient_name"))
    party = names.get("optician") or order.get("party_name") or ""
    today = _dt.date.today().strftime("%d-%m-%Y")
    product = _product_display_for_card(r_line or l_line)
    barcode_value = "".join(c for c in str(order_no) if c.isalnum()) or str(order.get("id") or "")[:12]
    try:
        bc = _make_barcode_html(barcode_value, height=27)
    except Exception:
        bc = f"<div style='font-family:monospace;font-size:7pt'>{barcode_value}</div>"

    def _row(line, eye):
        line = line or {}
        axis = "—"
        if line.get("axis") not in (None, "", "0", 0):
            try:
                axis = str(int(float(line.get("axis"))))
            except Exception:
                axis = str(line.get("axis") or "—")
        return (
            f"<tr><td class='eye'>{eye}</td>"
            f"<td>{_fp(line.get('sph'))}</td>"
            f"<td>{_fp(line.get('cyl'))}</td>"
            f"<td>{axis}</td>"
            f"<td>{_fp(line.get('add_power'))}</td></tr>"
        )

    return f"""<div class="cust75">
  <div class="top">
    <div>
      <div class="brand">Authenticity Card</div>
      <div class="name">{patient}</div>
      <div class="opt">{f"Optician: {party}" if party else ""}</div>
    </div>
    <div class="ord">{today}</div>
  </div>
  <div class="prod">{product}</div>
  <table>
    <tr><th></th><th>SPH</th><th>CYL</th><th>AX</th><th>ADD</th></tr>
    {_row(r_line, "R")}
    {_row(l_line, "L")}
  </table>
  <div class="foot">
    <div class="bc">{bc}</div>
    <div class="tag">See Clearly, Check Regularly</div>
  </div>
</div>"""


def _build_customer_75x50_page(r_line, l_line, order: dict) -> str:
    """Build printable 75×50mm customer card page."""
    card_html = _make_customer_75x50_html(r_line, l_line, order)
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
  @page {{ size: {css_size(TSC_LABEL_W_MM, TSC_LABEL_H_MM)}; margin: 0; }}
  body {{ margin: 0; background: #fff; font-family: Arial, Helvetica, sans-serif; }}
  .cust75 {{
    width:{TSC_LABEL_W_MM}mm;height:{TSC_LABEL_H_MM}mm;box-sizing:border-box;padding:2.4mm 3mm;
    border:0.5mm solid #0f172a;background:#fff;color:#0f172a;
    display:flex;flex-direction:column;gap:1mm;overflow:hidden;
  }}
  .top {{ display:flex;justify-content:space-between;gap:2mm;border-bottom:.35mm solid #0f172a;padding-bottom:1mm; }}
  .brand {{ font-size:5.5pt;font-weight:900;text-transform:uppercase;letter-spacing:.08em;color:#334155; }}
  .name {{ font-size:11pt;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:50mm;line-height:1.05; }}
  .opt {{ font-size:6.5pt;font-weight:900;color:#334155;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:50mm;margin-top:.3mm; }}
  .ord {{ font-size:6.3pt;font-weight:900;text-align:right;font-family:monospace;line-height:1.25; }}
  .prod {{ font-size:6.3pt;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }}
  table {{ border-collapse:collapse;width:100%;font-size:6.5pt; }}
  th {{ background:#0f172a;color:#fff;padding:.7mm .9mm;text-align:center;font-size:5.8pt; }}
  td {{ border-bottom:.2mm solid #cbd5e1;padding:.65mm .9mm;text-align:center;font-weight:800; }}
  td.eye {{ text-align:left;font-weight:900;color:#0f172a; }}
  .foot {{ margin-top:auto;display:flex;align-items:flex-end;justify-content:space-between;gap:2mm;border-top:.25mm solid #cbd5e1;padding-top:.5mm; }}
  .bc {{ line-height:0;max-width:42mm;overflow:hidden; }}
  .tag {{ font-size:7.2pt;font-weight:900;text-align:center;color:#fff;background:#000;padding:.8mm 1.4mm;max-width:31mm;line-height:1.05; }}
  .no-print {{ display:block; }}
  @media print {{ .no-print {{ display:none!important; }} }}
</style></head><body>
{card_html}
<div class='no-print' style='text-align:center;padding:14px'>
  <button onclick="document.querySelectorAll('.no-print').forEach(e=>e.style.display='none');window.print();setTimeout(()=>document.querySelectorAll('.no-print').forEach(e=>e.style.display=''),500)"
    style="background:#0f172a;color:#fff;border:none;padding:10px 24px;border-radius:6px;font-size:14px;cursor:pointer;font-weight:700">
    Print 75×50 Customer Card
  </button>
</div>
</body></html>"""


def _build_combined_job_card_html(r_line, l_line, order: dict) -> str:
    """
    Combined R + L job card print — A5 landscape (or A4 portrait).
    R card on top, L card below. Each card matches the back-office format:
    header | RX power table | surfacing table | barcodes
    Tool A / Tool B / Axis are HIGHLIGHTED in a coloured box.
    """
    import datetime as _dt

    def _fp(v, default="—"):
        if v is None: return default
        try:
            n = float(v)
            if n == 0.0: return "0.00"
            return f"+{n:.2f}" if n > 0 else f"{n:.2f}"
        except Exception as e:
            log.debug("Display formatting fallback: %s", e)
            return str(v)

    def _int(v, default="—"):
        if v is None: return default
        try: return str(int(float(v)))
        except Exception as e:
            log.debug("Display formatting fallback: %s", e)
            return str(v)

    order_no  = order.get("order_no", "—")
    patient   = order.get("patient_name", "—")
    today     = _dt.date.today().strftime("%d-%m-%Y")

    shop_name = "DV Optical"
    phone_no  = ""
    try:
        from modules.settings.shop_master import get_unit_info
        _sh = get_unit_info("retail")
        shop_name = _sh.get("shop_name", shop_name)
        phone_no  = _sh.get("phone", "")
    except Exception:
        pass

    def _eye_card(line, eye_label):
        if not line:
            return ""
        surf  = line.get("surfacing_data") or {}
        lp    = line.get("lens_params") or {}
        if isinstance(lp, str):
            import json as _jlp
            try: lp = _jlp.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}

        product   = (line.get("product_name") or "—")[:50]
        dia       = surf.get("diameter") or lp.get("diameter") or "75"
        frame_lp  = (lp.get("frame_type") or surf.get("frame_type") or "SUPRA").upper()
        coating   = (line.get("coating_type") or surf.get("coating_type") or "").upper()
        material  = (surf.get("blank_material") or "").upper()

        # RX powers (prescription)
        sph_rx  = _fp(line.get("sph"))
        cyl_rx  = _fp(line.get("cyl"))
        ax_rx   = _int(line.get("axis"))
        add_rx  = _fp(line.get("add_power"))

        # Surfacing powers
        sph_s   = _fp(surf.get("sph_surf"))
        cyl_s   = _fp(surf.get("cyl_surf"))
        ax_s    = _int(surf.get("axis_surf"))
        add_s   = _fp(surf.get("add_power_selected"))
        base    = _fp(surf.get("base_curve"))

        # TOOL A / TOOL B — highlighted
        tool_a  = str(surf.get("tool_a") or surf.get("dia_tool_a") or "—")
        tool_b  = str(surf.get("tool_b") or surf.get("dia_tool_b") or "—")

        blank_brand = surf.get("blank_brand","")
        blank_mat   = surf.get("blank_material","")
        blank_batch = surf.get("blank_batch","")

        # Barcodes
        ono_clean = "".join(c for c in order_no if c.isalnum())
        bc_c  = f"C{ono_clean}{eye_label}"
        bc_o  = f"O{ono_clean}{eye_label}"
        bc_html = _make_barcode_html

        eye_color = "#16a34a" if eye_label == "R" else "#2563eb"

        return f"""
<div style="width:100%;border:2px solid #000;box-sizing:border-box;
     font-family:Arial,Helvetica,sans-serif;margin-bottom:6mm;page-break-inside:avoid">

  <!-- EYE HEADER -->
  <div style="background:#1e293b;color:#fff;padding:3mm 5mm;display:flex;
       justify-content:space-between;align-items:center">
    <div>
      <span style="background:{eye_color};color:#fff;font-size:9pt;font-weight:900;
            padding:1mm 3mm;border-radius:3px;margin-right:3mm">{'RIGHT EYE' if eye_label=='R' else 'LEFT EYE'}</span>
      <span style="font-size:9pt;font-weight:700">{product}</span>
    </div>
    <div style="font-size:7pt;color:#94a3b8">
      Dia: {dia}mm &nbsp;|&nbsp; {frame_lp} &nbsp;|&nbsp; {material}
    </div>
  </div>

  <div style="display:flex;gap:0">

    <!-- LEFT COLUMN: RX + SURFACING powers -->
    <div style="flex:1.5;border-right:1.5px solid #000;padding:3mm 4mm">

      <!-- RX POWER TABLE -->
      <div style="font-size:6pt;font-weight:700;color:#475569;text-transform:uppercase;
           letter-spacing:.06em;margin-bottom:1.5mm">Prescription (RX)</div>
      <table style="border-collapse:collapse;width:100%;font-size:8pt;margin-bottom:3mm">
        <tr style="background:#f1f5f9">
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt"></th>
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt">SPH</th>
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt">CYL</th>
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt">AXIS</th>
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt">ADD</th>
        </tr>
        <tr>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;font-weight:700;font-size:7pt">
            {'RE' if eye_label=='R' else 'LE'}</td>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace">{sph_rx}</td>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace">{cyl_rx}</td>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace">{ax_rx}</td>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace">{add_rx}</td>
        </tr>
      </table>

      <!-- SURFACING TABLE — AXIS highlighted here only -->
      <div style="font-size:6pt;font-weight:700;color:#475569;text-transform:uppercase;
           letter-spacing:.06em;margin-bottom:1.5mm">Surfacing Parameters</div>
      <table style="border-collapse:collapse;width:100%;font-size:8pt">
        <tr style="background:#f1f5f9">
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt">SPH Surf</th>
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt">CYL Surf</th>
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt;
               background:#fef08a;color:#78350f;font-weight:900">AXIS</th>
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt">ADD</th>
          <th style="padding:1.5mm 2mm;text-align:center;border:1px solid #ccc;font-size:6.5pt">Base</th>
        </tr>
        <tr>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace">{sph_s}</td>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace">{cyl_s}</td>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace;
               background:#fef08a;font-weight:900;color:#78350f;font-size:10pt">{ax_s}</td>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace">{add_s}</td>
          <td style="padding:1.5mm 2mm;border:1px solid #ccc;text-align:center;font-family:monospace">{base}</td>
        </tr>
      </table>
    </div>

    <!-- RIGHT COLUMN: Tool A/B + Blank + Barcode -->
    <div style="flex:1;padding:3mm 4mm;display:flex;flex-direction:column;gap:2mm">

      <!-- TOOL A / TOOL B — highlighted boxes -->
      <div style="display:flex;gap:2mm">
        <div style="flex:1;border:2px solid #dc2626;border-radius:4px;padding:2mm 3mm;text-align:center;
             background:#fef2f2">
          <div style="font-size:5.5pt;font-weight:900;color:#dc2626;text-transform:uppercase;
               letter-spacing:.06em">TOOL A</div>
          <div style="font-size:12pt;font-weight:900;color:#1e293b;font-family:monospace">{tool_a}</div>
        </div>
        <div style="flex:1;border:2px solid #7c3aed;border-radius:4px;padding:2mm 3mm;text-align:center;
             background:#faf5ff">
          <div style="font-size:5.5pt;font-weight:900;color:#7c3aed;text-transform:uppercase;
               letter-spacing:.06em">TOOL B</div>
          <div style="font-size:12pt;font-weight:900;color:#1e293b;font-family:monospace">{tool_b}</div>
        </div>
      </div>

      <!-- BLANK INFO -->
      <div style="font-size:7pt;color:#475569;border-top:1px solid #e2e8f0;padding-top:1.5mm">
        <b>Blank:</b> {blank_brand} {blank_mat}
        {f"&nbsp;|&nbsp; Batch: {blank_batch}" if blank_batch else ""}
      </div>

      <!-- BARCODES -->
      <div style="display:flex;flex-direction:column;gap:1mm;margin-top:auto">
        <div style="text-align:center">{_make_barcode_html(bc_c, height=28)}</div>
        <div style="text-align:center">{_make_barcode_html(bc_o, height=28)}</div>
      </div>
    </div>

  </div>
</div>"""

    r_card = _eye_card(r_line, "R") if r_line else ""
    l_card = _eye_card(l_line, "L") if l_line else ""

    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
  @page {{ size: {CANON_DEFAULT_PAPER} landscape; margin: 7mm; }}
  body {{ margin: 0; font-family: Arial, Helvetica, sans-serif; background: #fff; }}
  @media print {{ .no-print {{ display: none !important; }} }}
</style></head><body>

<div style="text-align:center;margin-bottom:5mm;border-bottom:2px solid #000;padding-bottom:3mm">
  <div style="font-size:14pt;font-weight:900">{shop_name}</div>
  {f"<div style='font-size:8pt;color:#475569'>Phone: {phone_no}</div>" if phone_no else ""}
</div>

<div style="display:flex;justify-content:space-between;margin-bottom:4mm;font-size:9pt">
  <div><b>Patient:</b> {patient}</div>
  <div><b>Order No:</b> {order_no}</div>
  <div><b>Date:</b> {today}</div>
</div>

{r_card}
{l_card}

<div class="no-print" style="text-align:center;padding:16px">
  <button onclick="document.querySelectorAll('.no-print').forEach(e=>e.style.display='none');window.print();setTimeout(()=>document.querySelectorAll('.no-print').forEach(e=>e.style.display=''),500)"
    style="background:#1e293b;color:#fff;border:none;padding:10px 28px;border-radius:6px;
           font-size:14px;cursor:pointer;font-weight:700">
    Print Job Card
  </button>
</div>
</body></html>"""


# ── Legacy wrappers (called from old code paths) ──────────────────
def _print_barcode_pair(r_line, l_line):
    _missing = _missing_blank_assignments_for_print({
        "order_no": (r_line or l_line or {}).get("order_no", ""),
        "lines": [x for x in (r_line, l_line) if x],
    })
    if _missing:
        st.error(f"🔴 Assignment not done — assign blank first for {'/'.join(_missing)} eye before printing labels.")
        return
    ok, msg = _print_tsc_production_labels([r_line, l_line], r_line)
    if ok:
        st.success(msg)
    else:
        st.warning(f"TSC direct print failed: {msg}. Opening browser print fallback.")
        _open_print_window(_build_label_page([r_line, l_line], r_line))

def _print_barcode_single(line):
    _missing = _missing_blank_assignments_for_print({
        "order_no": (line or {}).get("order_no", ""),
        "lines": [line] if line else [],
    })
    if _missing:
        st.error(f"🔴 Assignment not done — assign blank first for {'/'.join(_missing)} eye before printing labels.")
        return
    ok, msg = _print_tsc_production_labels([line], line)
    if ok:
        st.success(msg)
    else:
        st.warning(f"TSC direct print failed: {msg}. Opening browser print fallback.")
        _open_print_window(_build_label_page([line], line))

def _print_cr80_pair(r_line, l_line):
    _open_print_window(_build_cr80_page(r_line, l_line, r_line))

def _print_cr80_single(line):
    eye = str(line.get("eye_side","")).upper()
    r = line if eye[:1] == "R" else None
    l = line if eye[:1] == "L" else None
    _open_print_window(_build_cr80_page(r, l, line))


def _render_labels_tab(order: Dict) -> None:
    """🏷️ Print Labels tab — 75×50mm barcode labels for R and L."""
    st.markdown("#### 🏷️ Barcode Labels (75×50mm)")

    all_lines = order.get("lines") or []
    r_line = next((l for l in all_lines if str(l.get("eye_side","")).upper()[:1]=="R"), None)
    l_line = next((l for l in all_lines if str(l.get("eye_side","")).upper()[:1]=="L"), None)

    def _has(ln):
        if not ln: return False
        if ln.get("surfacing_data"): return True
        lp = ln.get("lens_params") or {}
        if isinstance(lp,str):
            import json as _jl
            try: lp=_jl.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}
        return bool(lp.get("surfacing_data"))

    def _load(ln):
        if not ln: return ln
        if ln.get("surfacing_data"): return ln
        lp = ln.get("lens_params") or {}
        if isinstance(lp,str):
            import json as _jl
            try: lp=_jl.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}
        sd = lp.get("surfacing_data")
        if sd: ln = dict(ln); ln["surfacing_data"] = sd
        return ln

    r_line = _load(r_line)
    l_line = _load(l_line)
    r_ok = _has(r_line)
    l_ok = _has(l_line)
    _missing_alloc = set(_missing_blank_assignments_for_print({
        **order,
        "lines": [x for x in (r_line, l_line) if x],
    }))
    r_print_ok = r_ok and "R" not in _missing_alloc
    l_print_ok = l_ok and "L" not in _missing_alloc

    _jc_order = {
        "id":           order.get("id",""),
        "order_no":     order.get("order_no",""),
        "patient_name": order.get("patient_name",""),
        "party_name":   order.get("party_name",""),
        "order_type":   order.get("order_type") or "RETAIL",
    }

    # Status
    st.markdown(
        f"<div style='display:flex;gap:12px;padding:4px 0 10px'>"
        f"<span style='color:{'#4ade80' if r_ok else '#f59e0b'}'>"
        f"{'✅' if r_ok else '⏳'} RIGHT EYE</span>"
        f"<span style='color:{'#60a5fa' if l_ok else '#f59e0b'}'>"
        f"{'✅' if l_ok else '⏳'} LEFT EYE</span>"
        f"</div>",
        unsafe_allow_html=True
    )

    if not r_ok and not l_ok:
        st.info("Save job cards first, then print labels here.")
        return
    if _missing_alloc:
        st.error(
            "🔴 Assignment not done — assign blank first for "
            f"{'/'.join(sorted(_missing_alloc))} eye before printing labels."
        )

    # Preview
    with st.expander("👁 Preview", expanded=True):
        _pc1, _pc2 = st.columns(2)
        with _pc1:
            if r_ok:
                st.markdown("**👁 RIGHT LABEL**")
                st.markdown(
                    _make_label_html(r_line, _jc_order, "R"),
                    unsafe_allow_html=True
                )
        with _pc2:
            if l_ok:
                st.markdown("**👁 LEFT LABEL**")
                st.markdown(
                    _make_label_html(l_line, _jc_order, "L"),
                    unsafe_allow_html=True
                )

    # Print buttons
    _lb1, _lb2, _lb3 = st.columns(3)
    with _lb1:
        if st.button("🏷️ Print R Label", key="lbl_print_r",
                     use_container_width=True, type="primary",
                     disabled=not r_print_ok):
            ok, msg = _print_tsc_production_labels([r_line], _jc_order)
            if ok:
                st.success("Sent to TSC")
            else:
                st.warning(f"TSC direct print failed: {msg}. Opening HTML standby.")
                _open_print_window(_build_label_page([r_line], _jc_order))
    with _lb2:
        if st.button("🏷️ Print L Label", key="lbl_print_l",
                     use_container_width=True, type="primary",
                     disabled=not l_print_ok):
            ok, msg = _print_tsc_production_labels([l_line], _jc_order)
            if ok:
                st.success("Sent to TSC")
            else:
                st.warning(f"TSC direct print failed: {msg}. Opening HTML standby.")
                _open_print_window(_build_label_page([l_line], _jc_order))
    with _lb3:
        if st.button("🏷️ Print Both Labels", key="lbl_print_both",
                     use_container_width=True, type="primary",
                     disabled=not (r_print_ok and l_print_ok)):
            ok, msg = _print_tsc_production_labels([r_line, l_line], _jc_order)
            if ok:
                st.success("Sent both labels to TSC")
            else:
                st.warning(f"TSC direct print failed: {msg}. Opening HTML standby.")
                _open_print_window(_build_label_page([r_line, l_line], _jc_order))


def _render_cr80_tab(order: Dict) -> None:
    """💳 Customer Card tab — 85×54mm CR80 authenticity card."""
    st.markdown("#### 💳 Customer Authenticity Card (85×54mm)")

    all_lines = order.get("lines") or []
    r_line = next((l for l in all_lines if str(l.get("eye_side","")).upper()[:1]=="R"), None)
    l_line = next((l for l in all_lines if str(l.get("eye_side","")).upper()[:1]=="L"), None)

    _jc_order = {
        "id":           order.get("id",""),
        "order_no":     order.get("order_no",""),
        "patient_name": order.get("patient_name",""),
        "party_name":   order.get("party_name",""),
        "order_type":   order.get("order_type") or "RETAIL",
    }

    # Preview
    with st.expander("👁 Preview", expanded=True):
        r_preview = r_line or {}
        l_preview = l_line or {}
        st.markdown(
            _make_cr80_html(r_preview or None, l_preview or None, _jc_order),
            unsafe_allow_html=True
        )

    if st.button("💳 Print Customer Card", key="cr80_print_btn",
                 use_container_width=True, type="primary"):
        _open_print_window(_build_cr80_page(r_line, l_line, _jc_order))
        st.success("✅ Customer card opened in new tab")



def _render_job_tracking_tab(order: Dict, order_id: str) -> None:
    # ── Rollback panel — shown before anything else ───────────────────────
    _render_set_back_panel(order)
    st.markdown("---")

    stages = _fetch_stages_from_db()
    if not stages:
        st.warning("Stage master not configured — populate job_stage_master and job_stage_transitions.")
        return

    jobs = _fetch_job_cards(order_id)
    if not jobs:
        st.info("No job cards found. Generate job cards from Documents tab.")
        return

    total  = len(jobs)
    closed = sum(1 for j in jobs if j.get("is_closed"))

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Jobs", total)
    m2.metric("In Progress", total - closed)
    m3.metric("Completed", closed)
    st.markdown("---")

    # ================================
    # PAIR GROUPING - R+L TOGETHER (MATCH BACKOFFICE)
    # Group by product_id so both eyes show together
    # ================================
    pairs: dict = {}

    for job in jobs:
        # Group by product_id - same as backoffice
        product_id = str(job.get("product_id") or "")
        
        if not product_id:
            # Fallback if no product_id - use unique key
            product_id = f"{job.get('order_line_id', '')[:8]}_{job.get('eye_side', '')}"

        pair_id = product_id
        eye = str(job.get("eye_side", "")).upper().strip()

        if pair_id not in pairs:
            pairs[pair_id] = {
                "R": None,
                "L": None,
                "product_name": job.get("product_name", "Unknown")
            }

        if eye in ("R", "RIGHT"):
            pairs[pair_id]["R"] = job
        elif eye in ("L", "LEFT"):
            pairs[pair_id]["L"] = job


    # ================================
    # BUILD FINAL DISPLAY BLOCKS
    # Show pairs regardless of surfacing_data (like backoffice)
    # ================================
    final_blocks = []

    for pair in pairs.values():
        r = pair.get("R")
        l = pair.get("L")

        # ✅ ALWAYS SHOW AS PAIR if both R and L exist (match backoffice)
        if r and l:
            final_blocks.append({
                "type": "pair",
                "R": r,
                "L": l,
                "product_name": pair["product_name"]
            })
        elif r:
            final_blocks.append({"type": "single", "job": r})
        elif l:
            final_blocks.append({"type": "single", "job": l})


    # ================================
    # BULK PRINT STATE - FIXED CHECKBOX PERSISTENCE
    # ================================
    if "bulk_print_selection" not in st.session_state:
        st.session_state["bulk_print_selection"] = set()

    selected = list(st.session_state["bulk_print_selection"])

    if final_blocks:
        st.markdown("### 🖨️ Bulk Print")

        c1, c2, c3 = st.columns(3)

        if c1.button("🖨️ Job Cards", key="bulk_jc_btn", use_container_width=True):
            st.session_state["bulk_print_payload"] = {
                "type": "jobcard",
                "ids": selected
            }
            st.rerun()

        if c2.button("🏷️ Barcode", key="bulk_barcode_btn", use_container_width=True):
            st.session_state["bulk_print_payload"] = {
                "type": "barcode",
                "ids": selected
            }
            st.rerun()

        if c3.button("💳 CR80", key="bulk_cr80_btn", use_container_width=True):
            st.session_state["bulk_print_payload"] = {
                "type": "cr80",
                "ids": selected
            }
            st.rerun()

    st.markdown("---")

    # ================================
    # RENDER UI
    # ================================
    for block in final_blocks:

        if block["type"] == "pair":
            st.markdown(f"### 👓 {block['product_name']} (R + L)")

            # ── Get job IDs and stages for advance both ──
            r_job = block["R"]
            l_job = block["L"]
            r_job_id = str(r_job.get("job_id") or "")
            l_job_id = str(l_job.get("job_id") or "")
            r_stage = r_job.get("current_stage", "JOB_CREATED")
            l_stage = l_job.get("current_stage", "JOB_CREATED")
            r_line_id = str(r_job.get("order_line_id") or "")
            l_line_id = str(l_job.get("order_line_id") or "")

            # ── Determine common next stage ──
            r_allowed = _fetch_allowed_next(r_stage, r_job.get("coating_path", "UNCOATED"))
            l_allowed = _fetch_allowed_next(l_stage, l_job.get("coating_path", "UNCOATED"))
            common_stages = sorted(set(r_allowed) & set(l_allowed))

            # ── Advance Both button ──
            col_act, col_print = st.columns([2, 3])
            with col_act:
                if common_stages:
                    next_stage = common_stages[0]
                    stage_info = next((s for s in stages if s["stage_code"] == next_stage), {})
                    stage_label = stage_info.get("stage_name", next_stage)
                    if st.button(f"▶ Advance Both → {stage_label}", key=f"adv_both_{r_job_id[:8]}_{l_job_id[:8]}", use_container_width=True):
                        # Advance R
                        _advance_stage(r_job_id, order_id, next_stage, "")
                        # Advance L
                        _advance_stage(l_job_id, order_id, next_stage, "")
                        st.rerun()

            # ── Print buttons for the pair ──
            with col_print:
                pc1, pc2, pc3 = st.columns(3)
                with pc1:
                    if st.button("🖨️ Job Card", key=f"pair_jc_{r_job_id[:8]}", use_container_width=True):
                        _open_production_job_card_print(r_job, l_job, order)
                with pc2:
                    if st.button("🏷️ Barcode", key=f"pair_bc_{r_job_id[:8]}", use_container_width=True):
                        _print_barcode_75x50(r_job, {})
                        _print_barcode_75x50(l_job, {})
                with pc3:
                    if st.button("💳 CR80", key=f"pair_cr80_{r_job_id[:8]}", use_container_width=True):
                        _print_cr80_card(r_job, {})
                        _print_cr80_card(l_job, {})

            st.markdown("---")

            col1, col2 = st.columns(2)

            with col1:
                line_id = str(block["R"].get("order_line_id") or block["R"].get("id"))
                col_chk, col_ui = st.columns([0.5, 5])
                with col_chk:
                    checked = st.checkbox("", key=f"sel_{line_id}", value=line_id in st.session_state.get("bulk_print_selection", set()))
                    if checked:
                        st.session_state["bulk_print_selection"].add(line_id)
                    else:
                        st.session_state["bulk_print_selection"].discard(line_id)
                with col_ui:
                    _render_eye_job(block["R"], "R", True, block["R"].get("coating_path", "UNCOATED"), stages, order_id)

            with col2:
                line_id = str(block["L"].get("order_line_id") or block["L"].get("id"))
                col_chk, col_ui = st.columns([0.5, 5])
                with col_chk:
                    checked = st.checkbox("", key=f"sel_{line_id}", value=line_id in st.session_state.get("bulk_print_selection", set()))
                    if checked:
                        st.session_state["bulk_print_selection"].add(line_id)
                    else:
                        st.session_state["bulk_print_selection"].discard(line_id)
                with col_ui:
                    _render_eye_job(block["L"], "L", True, block["L"].get("coating_path", "UNCOATED"), stages, order_id)

        else:
            job = block["job"]
            eye_side = str(job.get("eye_side", "")).upper()
            line_id = str(job.get("order_line_id") or job.get("id"))
            
            col_chk, col_ui = st.columns([0.5, 5])
            with col_chk:
                checked = st.checkbox("", key=f"sel_{line_id}", value=line_id in st.session_state.get("bulk_print_selection", set()))
                if checked:
                    st.session_state["bulk_print_selection"].add(line_id)
                else:
                    st.session_state["bulk_print_selection"].discard(line_id)
            with col_ui:
                st.markdown(f"### 👁 {eye_side} - {job.get('product_name', 'Unknown')}")
                _render_eye_job(job, eye_side, True, job.get("coating_path", "UNCOATED"), stages, order_id)


    # ================================
    # BULK PRINT HANDLER
    # ================================
    def _handle_bulk_print():
        payload = st.session_state.get("bulk_print_payload")
        if not payload:
            return

        # Guard against duplicate print on rerun
        if st.session_state.get("_printing_in_progress"):
            return
        st.session_state["_printing_in_progress"] = True

        try:
            ids = payload.get("ids", [])
            ptype = payload.get("type")

            st.markdown("---")
            st.markdown(f"## 🖨️ Bulk Print — {ptype.upper()}")

            for lid in ids:
                try:
                    from modules.sql_adapter import run_query
                    rows = run_query(
                        "SELECT * FROM order_lines WHERE id = %(id)s::uuid LIMIT 1",
                        {"id": lid}
                    )

                    if not rows:
                        continue

                    line = rows[0]

                    import json as _jbp
                    lp = line.get("lens_params") or {}
                    if isinstance(lp, str):
                        try:
                            lp = _jbp.loads(lp)
                        except Exception as e:
                            log.debug("Could not parse print lens_params: %s", e)
                            lp = {}

                    surf = lp.get("surfacing_data", {})

                    if ptype == "jobcard":
                        if surf:
                            line = dict(line)
                            line["surfacing_data"] = surf
                        _open_production_job_card_print(
                            line if str(line.get("eye_side", "")).upper()[:1] == "R" else None,
                            line if str(line.get("eye_side", "")).upper()[:1] == "L" else None,
                            order,
                        )
                    elif ptype == "barcode":
                        _print_barcode_75x50(line, surf)
                    elif ptype == "cr80":
                        _print_cr80_card(line, surf)
                except Exception as e:
                    st.error(f"Error printing {lid}: {e}")

            st.session_state["bulk_print_selection"].clear()
            st.session_state.pop("bulk_print_payload", None)
        finally:
            st.session_state["_printing_in_progress"] = False

    _handle_bulk_print()

    st.markdown("---")

    with st.expander("📋 Audit Trail", expanded=False):
        render_event_timeline(order_id)

    _render_supplier_intelligence_panel(order)


def _render_eye_job(job, eye_label, show_advance, job_coating_path, stages, order_id):
    """Render a single eye job card."""
    if job is None:
        return
    try:
        from modules.backoffice.coating_engine import (
            COATING_STAGE_SEQUENCES, FULL_STAGE_SEQUENCE_PREFIX,
            FITTING_STAGE_SEQUENCE, get_allowed_next_stages,
            get_ready_type, save_coating_path_to_job,
        )
    except Exception:
        # coating_engine unavailable — safe fallbacks so panel still renders
        def get_allowed_next_stages(stage, cp, has_fitting=False):
            return _fetch_allowed_next(stage, cp)
        def get_ready_type(cp, has_fitting=False):
            return ("✅", "Ready for Pack", "#166534", "#bbf7d0")
        def save_coating_path_to_job(lid, cp):
            pass
        COATING_STAGE_SEQUENCES    = {}
        FULL_STAGE_SEQUENCE_PREFIX = []
        FITTING_STAGE_SEQUENCE     = []
    
    job_id        = str(job.get("job_id", ""))
    line_id       = str(job.get("order_line_id", ""))
    current_stage = job.get("current_stage", "JOB_CREATED")
    is_closed     = bool(job.get("is_closed"))
    # is_closed may be set by DB at READY_FOR_PACK, but if fitting service
    # exists the job is NOT truly done yet — override so advance shows
    _order_svc_pre = job.get("_order_services") or {}
    if is_closed and current_stage == "READY_FOR_PACK" and _order_svc_pre.get("has_fitting"):
        is_closed = False   # still needs fitting stages
    total_qty     = int(job.get("total_qty") or 0)
    blank_req     = int(job.get("blank_required_qty") or 0)
    blank_alloc   = int(job.get("blank_allocated_qty") or 0)
    reprocess     = int(job.get("reprocess_count") or 0)
    eye_color     = "#1a3a5c" if eye_label == "R" else "#1a3a2a"
    eye_border    = "#3b82f6" if eye_label == "R" else "#10b981"
    eye_title     = "👁 RIGHT EYE" if eye_label == "R" else "👁 LEFT EYE"

    # ── Path labels for manual selector ──────────────────────────────
    PATH_OPTIONS = {
        "UNCOATED":     "🟢 Uncoated (UC)",
        "COLOURING":    "🟡 Colouring only",
        "COLOURING_HC": "🟠 Colouring → HC",
        "HARDCOAT":     "🔵 Hardcoat (HC) only",
        "ARC":          "🔴 ARC only",
        "HARDCOAT_ARC": "🟣 HC → ARC",
    }

    # ── Eye header ───────────────────────────────────────────────────
    st.markdown(
        f"<div style='background:{eye_color};border-left:4px solid {eye_border};"
        f"color:#fff;padding:6px 14px;border-radius:6px;font-weight:700;margin-bottom:4px'>"
        f"{eye_title}</div>",
        unsafe_allow_html=True
    )
    with st.container(border=True):

        # ── Coating path from order services (auto) + confirm gate ────
        _order_svc = job.get("_order_services") or _detect_order_services(order_id)
        _has_fit   = _order_svc.get("has_fitting", False)
        _auto_cp   = _order_svc.get("coating_path", "UNCOATED")

        # If path not yet persisted, pre-fill from services
        if not job_coating_path or job_coating_path == "UNCOATED":
            job_coating_path = _auto_cp

        # ── GATE: INSPECTION requires path confirmation before advance ──
        _path_gate_key      = f"path_confirmed_{job_id}"
        _path_confirmed     = bool(st.session_state.get(_path_gate_key))
        _past_inspection    = current_stage not in {
            "JOB_CREATED","PRINTED","PRODUCTION_PICKED","PRODUCTION_DONE","INSPECTION"
        }

        if current_stage == "INSPECTION" and not _path_confirmed and not _past_inspection:
            st.markdown(
                "<div style='background:#1c1917;border:1px solid #f59e0b;"
                "border-radius:8px;padding:10px 14px;margin-bottom:8px'>"
                "<span style='color:#fde68a;font-weight:700;font-size:0.9rem'>"
                "⚗️ Confirm Coating Path to Continue</span>"
                "<br><span style='color:#a8a29e;font-size:0.78rem'>"
                "Auto-detected from order services. Change if incorrect, then confirm.</span>"
                "</div>",
                unsafe_allow_html=True
            )
            _path_choice = st.selectbox(
                "Coating path",
                options=list(PATH_OPTIONS.keys()),
                format_func=lambda x: PATH_OPTIONS[x],
                index=list(PATH_OPTIONS.keys()).index(job_coating_path)
                      if job_coating_path in PATH_OPTIONS else 0,
                key=f"manual_path_{job_id}",
                label_visibility="collapsed"
            )
            _fit_chk = st.checkbox(
                "🧵 Fitting service on this order",
                value=_has_fit,
                key=f"fit_chk_{job_id}"
            )
            if st.button("✅ Confirm Path & Continue",
                         key=f"confirm_path_{job_id}",
                         type="primary", use_container_width=True):
                save_coating_path_to_job(line_id, _path_choice)
                st.session_state[_path_gate_key]          = True
                st.session_state[f"has_fitting_{job_id}"] = _fit_chk
                job_coating_path = _path_choice
                _has_fit         = _fit_chk
                st.rerun()
            show_advance = False

        elif _path_confirmed or _past_inspection:
            _cp_colors2 = {
                "UNCOATED":"#374151","HARDCOAT":"#1d4ed8","COLOURING":"#92400e",
                "COLOURING_HC":"#b45309","ARC":"#991b1b","HARDCOAT_ARC":"#6b21a8",
            }
            _cp_c2 = _cp_colors2.get(job_coating_path, "#374151")
            st.markdown(
                f"<div style='background:{_cp_c2};border-radius:4px;"
                f"padding:3px 9px;margin-bottom:5px;display:inline-block'>"
                f"<span style='color:#fff;font-size:0.72rem;font-weight:700'>"
                f"⚗️ {PATH_OPTIONS.get(job_coating_path, job_coating_path)}"
                f"{'  🧵' if _has_fit else ''}</span></div>",
                unsafe_allow_html=True
            )
            if current_stage == "INSPECTION":
                with st.expander("🔀 Change path", expanded=False):
                    _pc2 = st.selectbox(
                        "New path",
                        options=list(PATH_OPTIONS.keys()),
                        format_func=lambda x: PATH_OPTIONS[x],
                        index=list(PATH_OPTIONS.keys()).index(job_coating_path)
                              if job_coating_path in PATH_OPTIONS else 0,
                        key=f"override_path_{job_id}",
                        label_visibility="collapsed"
                    )
                    if st.button("✅ Apply Change",
                                 key=f"apply_override_{job_id}",
                                 use_container_width=True):
                        save_coating_path_to_job(line_id, _pc2)
                        job_coating_path = _pc2
                        st.rerun()

        _has_fit = st.session_state.get(f"has_fitting_{job_id}", _has_fit)

        # ── Ready type badge ─────────────────────────────────────────
        if current_stage == "READY_FOR_PACK" and _has_fit:
            st.markdown(
                "<div style='background:#2e1065;border:2px solid #a855f7;"
                "border-radius:8px;padding:8px 12px;margin-bottom:6px;text-align:center'>"
                "<span style='color:#d8b4fe;font-weight:800;font-size:1rem'>"
                "🟣 Lens Ready → Fitting Required</span><br>"
                "<span style='color:#c4b5fd;font-size:0.7rem;opacity:0.9'>"
                "Send to fitter before dispatch</span></div>",
                unsafe_allow_html=True
            )
        elif current_stage == "READY_FOR_PACK":
            _emoji, _rlabel, _rbg, _rtxt = get_ready_type(job_coating_path, has_fitting=False)
            st.markdown(
                f"<div style='background:{_rbg};border:2px solid {_rtxt};"
                f"border-radius:8px;padding:8px 12px;margin-bottom:6px;text-align:center'>"
                f"<span style='color:{_rtxt};font-weight:800;font-size:1rem'>"
                f"{_emoji} {_rlabel}</span><br>"
                f"<span style='color:{_rtxt};font-size:0.7rem;opacity:0.85'>"
                f"Ready for billing &amp; dispatch</span></div>",
                unsafe_allow_html=True
            )
        elif current_stage == "FITTING_DONE":
            st.markdown(
                "<div style='background:#2e1065;border:2px solid #c084fc;"
                "border-radius:8px;padding:8px 12px;margin-bottom:6px;text-align:center'>"
                "<span style='color:#c084fc;font-weight:800;font-size:1rem'>"
                "🟣 Fitting Ready</span><br>"
                "<span style='color:#e9d5ff;font-size:0.7rem;opacity:0.85'>"
                "Ready for billing &amp; dispatch</span></div>",
                unsafe_allow_html=True
            )
        elif is_closed:
            st.success("✅ DISPATCHED / COMPLETE")
        else:
            _cp_colors = {
                "UNCOATED":"#6b7280","HARDCOAT":"#1d4ed8","COLOURING":"#92400e",
                "COLOURING_HC":"#b45309","ARC":"#991b1b","HARDCOAT_ARC":"#6b21a8",
            }
            _cp_col = _cp_colors.get(job_coating_path, "#6b7280")
            st.markdown(
                f"<div style='display:flex;gap:8px;align-items:center;margin-bottom:4px'>"
                f"<span style='background:#1e293b;color:#f59e0b;border:1px solid #f59e0b;"
                f"border-radius:4px;padding:2px 8px;font-size:0.75rem;font-weight:700'>"
                f"{current_stage}</span>"
                f"<span style='background:{_cp_col};color:#fff;"
                f"border-radius:4px;padding:2px 8px;font-size:0.65rem;font-weight:600'>"
                f"{PATH_OPTIONS.get(job_coating_path, job_coating_path)}</span>"
                f"</div>",
                unsafe_allow_html=True
            )

        if blank_req > 0:
            st.caption(
                f"Blanks: {blank_alloc}/{blank_req} · Qty: {total_qty}"
                + (f" · Reprocessed {reprocess}×" if reprocess else "")
            )

        _render_progress(stages, current_stage)

        # ── Stage advance section ────────────────────────────────────
        _force_show_advance = (
            current_stage == "READY_FOR_PACK" and _has_fit
            and current_stage not in {"FITTING_PENDING","FITTING_SENT",
                                      "FITTING_RECEIVED","FITTING_DONE"}
        )
        if show_advance and (not is_closed or _force_show_advance):
            allowed = get_allowed_next_stages(
                current_stage, job_coating_path, has_fitting=_has_fit
            )

            if not allowed:
                allowed = _fetch_allowed_next(current_stage, job_coating_path)

            # ── Provider communication: colour sample / instructions ───
            if "COLOUR" in str(current_stage or "").upper():
                _svc_ctx = _get_service_context(order_id, "COLOURING")
                if _svc_ctx:
                    _sample_b64 = _svc_ctx.get("colour_sample_photo") or ""
                    _instr = _svc_ctx.get("service_instruction") or ""
                    _prov_name = _svc_ctx.get("suggested_provider_name") or "Colouring provider"
                    _prov_phone = "".join(ch for ch in str(_svc_ctx.get("suggested_provider_phone") or "") if ch.isdigit())
                    with st.expander("🎨 Colour Sample / Provider Message", expanded=bool(_sample_b64 or _instr)):
                        if _instr:
                            st.info(_instr)
                        if _sample_b64:
                            try:
                                import base64 as _b64_sample
                                _sample_bytes = _b64_sample.b64decode(_sample_b64)
                                st.image(_sample_bytes, width=180, caption=_svc_ctx.get("colour_sample_filename") or "Colour sample")
                                st.download_button(
                                    "⬇️ Download Sample Photo",
                                    data=_sample_bytes,
                                    file_name=_svc_ctx.get("colour_sample_filename") or f"{order_id}_colour_sample.jpg",
                                    mime="image/jpeg",
                                    key=f"dl_colour_sample_{job_id}",
                                    use_container_width=True,
                                )
                            except Exception:
                                st.caption("Colour sample saved, preview unavailable.")
                        _wa_text = (
                            f"Colouring order {order_id}\\n"
                            f"Product: {job.get('product_name','')}\\n"
                            f"Eye: {job.get('eye_side','')}\\n"
                            f"Instruction: {_instr or '-'}\\n"
                            f"Sample photo is attached separately."
                        )
                        if _prov_phone:
                            import urllib.parse as _urlq
                            st.markdown(
                                f"<a href='https://wa.me/91{_prov_phone[-10:]}?text={_urlq.quote(_wa_text)}' "
                                f"target='_blank' style='display:block;text-align:center;background:#065f46;"
                                f"color:white;padding:8px 12px;border-radius:6px;text-decoration:none'>"
                                f"📲 WhatsApp {_prov_name}</a>",
                                unsafe_allow_html=True,
                            )
                        else:
                            st.caption("Add colouring provider phone in Service Management to enable WhatsApp.")

            # ── GATE 1: Colouring photo required ─────────────────────
            _colour_blocked = False
            if current_stage == "COLOURING_DONE":
                _existing_photo = _get_colouring_photo(job)
                if not _existing_photo:
                    st.markdown(
                        "<div style='background:#1a0a00;border:1px solid #f97316;"
                        "border-radius:6px;padding:8px 12px;margin-bottom:6px'>"
                        "<span style='color:#fb923c;font-weight:700'>📸 Colour photo required</span>"
                        "<br><span style='color:#fed7aa;font-size:0.78rem'>"
                        "Upload final colour photo before advancing to next stage.</span></div>",
                        unsafe_allow_html=True
                    )
                    _photo_up = st.file_uploader(
                        "Upload colour photo",
                        type=["jpg","jpeg","png","webp"],
                        key=f"colour_photo_{job_id}",
                        label_visibility="collapsed"
                    )
                    if _photo_up:
                        import base64 as _b64c
                        _b64 = _b64c.b64encode(_photo_up.read()).decode()
                        st.image(_b64c.b64decode(_b64), width=120, caption="Preview")
                        if st.button("✅ Confirm Colour & Unlock",
                                     key=f"confirm_colour_{job_id}",
                                     type="primary", use_container_width=True):
                                    if _save_colouring_photo(line_id, _b64):
                                        st.success("📸 Photo saved — stage unlocked")
                                        st.rerun()
                    _colour_blocked = True
                else:
                    import base64 as _b64c2
                    try:
                        st.image(_b64c2.b64decode(_existing_photo),
                                 width=80, caption="✅ Colour photo")
                    except Exception:
                        st.caption("✅ Colour photo on file")

            # ── GATE 2: Fitter + fitting type + rate selection ────────
            _fitter_blocked = False
            _fitter_rmk     = ""
            _fitter_id_sel  = None
            _fit_type_sel   = None
            _fit_rate_sel   = 0.0
            _fitter_gate_stages = {"READY_FOR_PACK", "FITTING_PENDING"}
            if current_stage in _fitter_gate_stages and _has_fit and allowed:
                _fit_ctx = _get_service_context(order_id, "FITTING")
                if _fit_ctx:
                    _fit_instr = _fit_ctx.get("service_instruction") or ""
                    _fit_phone = "".join(ch for ch in str(_fit_ctx.get("suggested_provider_phone") or "") if ch.isdigit())
                    _fit_name = _fit_ctx.get("suggested_provider_name") or "Fitting provider"
                    if _fit_instr or _fit_phone:
                        with st.expander("🔧 Fitting Instructions / Provider Message", expanded=bool(_fit_instr)):
                            if _fit_instr:
                                st.info(_fit_instr)
                            if _fit_phone:
                                import urllib.parse as _urlq_fit
                                _fit_text = (
                                    f"Fitting order {order_id}\\n"
                                    f"Product: {job.get('product_name','')}\\n"
                                    f"Eye: {job.get('eye_side','')}\\n"
                                    f"Instruction: {_fit_instr or '-'}"
                                )
                                st.markdown(
                                    f"<a href='https://wa.me/91{_fit_phone[-10:]}?text={_urlq_fit.quote(_fit_text)}' "
                                    f"target='_blank' style='display:block;text-align:center;background:#065f46;"
                                    f"color:white;padding:8px 12px;border-radius:6px;text-decoration:none'>"
                                    f"📲 WhatsApp {_fit_name}</a>",
                                    unsafe_allow_html=True,
                                )
                st.markdown(
                    "<div style='background:#1a0a2e;border:1px solid #a855f7;"
                    "border-radius:6px;padding:10px 14px;margin-bottom:8px'>"
                    "<span style='color:#d8b4fe;font-weight:700;font-size:0.9rem'>"
                    "🧵 Fitting Assignment</span>"
                    "<br><span style='color:#e9d5ff;font-size:0.78rem'>"
                    "Select fitter, fitting type and confirm rate.</span></div>",
                    unsafe_allow_html=True
                )
                _fitters_db    = _get_fitters()
                _fit_types_db  = _get_fitting_types()

                if _fitters_db:
                    _fitter_names  = [f["name"] for f in _fitters_db]
                    _fitter_ids    = {f["name"]: f["id"] for f in _fitters_db}
                    _fitter_opts   = ["— select fitter —"] + _fitter_names + ["Other (enter name)"]
                    _sel_fname     = st.selectbox(
                        "Fitter", _fitter_opts,
                        key=f"fitter_{job_id}", label_visibility="collapsed"
                    )
                    if _sel_fname == "— select fitter —":
                        _fitter_blocked = True
                    elif _sel_fname == "Other (enter name)":
                        _manual = st.text_input(
                            "Fitter name", key=f"fitter_manual_{job_id}",
                            placeholder="Enter name", label_visibility="collapsed"
                        )
                        if not _manual.strip():
                            st.caption("⚠️ Enter fitter name")
                            _fitter_blocked = True
                        else:
                            _fitter_rmk = f"Fitter: {_manual.strip()}"
                    else:
                        _fitter_id_sel = _fitter_ids.get(_sel_fname)
                else:
                    _manual2 = st.text_input(
                        "Fitter name", key=f"fitter_txt_{job_id}",
                        placeholder="Enter fitter name", label_visibility="collapsed"
                    )
                    if not _manual2.strip():
                        _fitter_blocked = True
                    else:
                        _fitter_rmk = f"Fitter: {_manual2.strip()}"
                    st.caption("ℹ️ Add fitting providers in Service Management → Providers & Rates")

                if not _fitter_blocked and _fitter_id_sel:
                    if _fit_types_db:
                        _ft_labels = [f["label"] for f in _fit_types_db]
                        _ft_codes  = {f["label"]: f["code"] for f in _fit_types_db}
                        _sel_ft_label = st.selectbox(
                            "Fitting Type",
                            _ft_labels,
                            key=f"fit_type_{job_id}",
                            label_visibility="collapsed"
                        )
                        _fit_type_sel = _ft_codes.get(_sel_ft_label)

                        if _fit_type_sel:
                            _auto_rate = _get_fitter_rate(_fitter_id_sel, _fit_type_sel)
                            _fit_rate_sel = st.number_input(
                                "Rate (₹)",
                                min_value=0.0, step=5.0,
                                value=_auto_rate,
                                format="%.2f",
                                key=f"fit_rate_{job_id}"
                            )
                            if _fit_rate_sel <= 0:
                                st.markdown(
                                    "<div style='background:#450a0a;border:1px solid #ef4444;"
                                    "border-radius:5px;padding:6px 12px;margin-top:4px'>"
                                    "<span style='color:#fca5a5;font-weight:700'>"
                                    "⛔ Rate not set for this fitter + type</span></div>",
                                    unsafe_allow_html=True
                                )
                                _fitter_blocked = True
                            else:
                                st.markdown(
                                    f"<div style='background:#0f172a;border-radius:4px;"
                                    f"padding:4px 10px;display:inline-block'>"
                                    f"<span style='color:#a3e635;font-weight:700'>"
                                    f"💰 ₹{_fit_rate_sel:.2f} payable to fitter</span>"
                                    f"</div>",
                                    unsafe_allow_html=True
                                )
                            _fitter_rmk = (
                                f"Fitter: {_sel_fname} | "
                                f"Type: {_sel_ft_label} | "
                                f"Rate: ₹{_fit_rate_sel:.2f}"
                            )

            if allowed and not _colour_blocked:
                rmk = st.text_input(
                    "Remarks",
                    key=f"rmk_{job_id}",
                    value=_fitter_rmk,
                    placeholder="optional",
                    label_visibility="collapsed"
                )
                adv_cols = st.columns(len(allowed))
                for i, ns in enumerate(allowed):
                    info  = next((s for s in stages if s["stage_code"] == ns), {})
                    label = info.get("stage_name", ns)
                    _blocked = _fitter_blocked and ns in ("FITTING_SENT", "FITTING_PENDING")
                    with adv_cols[i]:
                        if st.button(
                            f"▶ {label}",
                            key=f"adv_{job_id}_{ns}",
                            use_container_width=True,
                            disabled=_blocked,
                        ):
                            if _advance_stage(job_id, order_id, ns, rmk):
                                if ns == "FITTING_PENDING" and _fitter_id_sel and _fit_type_sel:
                                    _create_fitting_assignment(
                                        order_no=order_id,
                                        order_line_id=line_id,
                                        job_id=job_id,
                                        eye_side=eye_label,
                                        fitter_id=_fitter_id_sel,
                                        fitting_type_code=_fit_type_sel,
                                        rate=_fit_rate_sel,
                                        remarks=rmk
                                    )
                                st.rerun()

        with st.expander("Stage History", expanded=False):
            evs = _fetch_stage_history(job_id)
            if evs:
                for ev in evs:
                    ts   = str(ev.get("created_at", ""))[:19]
                    name = ev.get("stage_name") or ev.get("stage_code", "")
                    dept = f" [{ev['department']}]" if ev.get("department") else ""
                    note = f" · _{ev['remarks']}_" if ev.get("remarks") else ""
                    _is_bs = "BACKSTEP" in str(ev.get("department",""))
                    _pfx = "↩️" if _is_bs else "✓"
                    _col = "#f59e0b" if _is_bs else "#94a3b8"
                    st.markdown(
                        f"<span style='color:{_col};font-size:0.78rem'>"
                        f"{_pfx} **{name}**{dept} — {ts}{note}</span>",
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("No events yet.")

        # ── Admin backstep panel ──────────────────────────────────
        try:
            from modules.backoffice.pipeline_guard_ui import render_backstep_ui
            render_backstep_ui(
                job_id=job_id,
                current_stage=current_stage,
                eye_side=eye_label,
                order_id=order_id,
            )
        except Exception:
            pass


def _render_supplier_intelligence_panel(order: Dict) -> None:
    """Supplier Intelligence — scored supplier list for this order's products."""
    try:
        from modules.flags.feature_flags import SYSTEM_FLAGS
        if not SYSTEM_FLAGS.get("supplier_intelligence_enabled", True):
            return
    except ImportError:
        pass

    st.markdown("---")
    with st.expander("🏭 Supplier Intelligence", expanded=False):
        st.caption("Scored and ranked suppliers based on delivery, price, rejection rate, and reliability")

        try:
            from modules.suppliers.intelligence import get_scored_suppliers

            all_lines = []
            all_lines.extend(order.get("stock_lines", []))
            all_lines.extend(order.get("inhouse_lines", []))
            all_lines.extend(order.get("lab_order_lines", []))

            product_id = None
            for line in all_lines:
                pid = line.get("product_id")
                if pid:
                    product_id = str(pid)
                    break

            if product_id:
                from modules.suppliers.intelligence import get_ranked_suppliers_for_assignment
                scores = get_ranked_suppliers_for_assignment(product_id)
            else:
                scores = get_scored_suppliers()

            if not scores:
                st.caption("No supplier data available yet.")
                return

            st.markdown("### Supplier Scores")
            for s in scores[:8]:
                grade = s.get("grade", "?")
                score = s.get("score", 0)
                name  = s.get("name", "Unknown")
                days  = s.get("delivery_days_avg", 0)
                rej   = s.get("rejection_pct", 0)

                if grade in ("A+", "A"):
                    st.success(
                        f"**{name}** — Score: {score} ({grade})  "
                        f"|  Delivery: {days:.1f}d  |  Rejection: {rej:.1f}%"
                    )
                elif grade == "B":
                    st.info(
                        f"**{name}** — Score: {score} ({grade})  "
                        f"|  Delivery: {days:.1f}d  |  Rejection: {rej:.1f}%"
                    )
                else:
                    st.warning(
                        f"**{name}** — Score: {score} ({grade})  "
                        f"|  Delivery: {days:.1f}d  |  Rejection: {rej:.1f}%"
                    )

        except ImportError:
            st.caption("⚠️ Supplier intelligence module not available.")


# ══════════════════════════════════════════════════════════════════════
# PRODUCTION PANEL TAB HELPERS
# ══════════════════════════════════════════════════════════════════════

def _resolve_order_rx(order: Dict):
    rx_r, rx_l = {}, {}
    lines = _real_label_lines_for_order(order.get("lines") or [], order)

    for _l in lines:
        _es = str(_l.get("eye_side","")).upper()
        _rx = {"sph": _l.get("sph"), "cyl": _l.get("cyl"),
               "axis": _l.get("axis"), "add": _l.get("add_power")}
        if _es in ("R","RIGHT"):
            rx_r = _rx
        elif _es in ("L","LEFT"):
            rx_l = _rx
    return rx_r, rx_l


def _resolve_customer(order: Dict):
    names = _order_print_names(order)
    return (names.get("customer") or "", names.get("mobile") or "")


def _resolve_order_product_for_print(order: Dict) -> str:
    lines = _real_label_lines_for_order(order.get("lines") or [], order)
    return _product_display_for_card(lines[0]) if lines else ""


def _resolved_card_context(order: Dict) -> dict:
    """Shared print context for CR80 and 75x50 customer authenticity cards."""
    import datetime as _dt
    lines = _real_label_lines_for_order(order.get("lines") or [], order)
    r_line = next((l for l in lines if str(l.get("eye_side","")).upper()[:1] == "R"), None)
    l_line = next((l for l in lines if str(l.get("eye_side","")).upper()[:1] == "L"), None)
    names = _order_print_names(order, r_line or l_line or {})

    def _rx(line):
        line = line or {}
        return {
            "sph": line.get("sph"),
            "cyl": line.get("cyl"),
            "axis": line.get("axis"),
            "add": line.get("add_power"),
        }

    def _card_customer_name(value) -> str:
        s = str(value or "").strip()
        return "" if s.lower() in ("end customer", "unknown", "none", "null", "-", "—") else s

    return {
        "lines": lines,
        "r_line": r_line,
        "l_line": l_line,
        "rx_r": _rx(r_line),
        "rx_l": _rx(l_line),
        "customer": _card_customer_name(names.get("customer")),
        "mobile": names.get("mobile") or "",
        "optician": names.get("optician") or names.get("party") or order.get("party_name", ""),
        "product": _product_display_for_card(r_line or l_line or {}),
        "date_text": _dt.date.today().strftime("%d-%m-%Y"),
    }


def _missing_blank_assignments_for_print(order: Dict) -> list[str]:
    """Eyes that still need blank assignment before production labels/job card."""
    lines = _real_label_lines_for_order(order.get("lines") or [], order)
    missing = []
    need_db_check = []

    for line in lines:
        eye = str(line.get("eye_side") or "").upper()[:1]
        if eye not in ("R", "L"):
            continue
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            import json as _json
            try:
                lp = _json.loads(lp) if lp else {}
            except Exception:
                lp = {}
        if not isinstance(lp, dict):
            lp = {}
        svc = str(line.get("service_production_type") or lp.get("service_production_type") or "").upper()
        if svc in ("FITTING", "COLOURING"):
            continue
        surf = line.get("surfacing_data") or lp.get("surfacing_data") or {}
        if isinstance(surf, dict) and (
            surf.get("blank_id") or surf.get("selected_blank_id") or surf.get("blank_batch")
        ):
            continue
        lid = str(line.get("line_id") or line.get("id") or "")
        need_db_check.append((eye, lid))

    allocated = set()
    line_ids = [lid for _eye, lid in need_db_check if lid]
    if line_ids:
        try:
            from modules.sql_adapter import run_query
            rows = run_query(
                "SELECT DISTINCT order_line_id::text AS line_id "
                "FROM blank_allocations "
                "WHERE order_line_id = ANY(%(lids)s::uuid[])",
                {"lids": line_ids},
            ) or []
            allocated = {str(r.get("line_id") or "") for r in rows}
        except Exception as e:
            log.debug("Blank assignment print gate failed: %s", e)

    for eye, lid in need_db_check:
        if not lid or lid not in allocated:
            missing.append(eye)
    return sorted(set(missing))


def _production_job_line_for_print(job: dict) -> dict:
    """Convert a production job row into the order-line shape used by job-card print."""
    import json as _json

    line = dict(job or {})
    line_id = str(line.get("order_line_id") or line.get("line_id") or line.get("id") or "").strip()
    if line_id:
        line["id"] = line_id
        line["line_id"] = line_id

    lp = line.get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = _json.loads(lp)
        except Exception:
            lp = {}
    if not isinstance(lp, dict):
        lp = {}
    line["lens_params"] = lp
    line["surfacing_data"] = line.get("surfacing_data") or lp.get("surfacing_data") or {}
    return line


def _open_production_job_card_print(r_job: dict | None, l_job: dict | None, order: Dict) -> None:
    """Use the canonical A5 landscape R/L surfacing print from all production paths."""
    try:
        from modules.documents.job_card_surfacing import _open_jc_print_window

        r_line = _production_job_line_for_print(r_job) if r_job else None
        l_line = _production_job_line_for_print(l_job) if l_job else None
        first = r_line or l_line or {}
        jc_order = {
            "id": str(order.get("id") or first.get("order_id") or ""),
            "order_no": order.get("order_no") or first.get("order_no") or "",
            "patient_name": order.get("patient_name") or first.get("patient_name") or "",
            "party_name": order.get("party_name") or first.get("party_name") or "",
            "order_type": order.get("order_type") or "RETAIL",
        }
        _open_jc_print_window(r_line, l_line, jc_order)
    except Exception as e:
        st.error(f"Job card print error: {e}")


def _render_job_card_print_tab(order: Dict):
    st.markdown("#### 🖨️ Job Card Print")
    try:
        from modules.documents.job_card_surfacing import (
            render_job_card_print_pair, render_job_card_print, render_surfacing_job_card, save_job_card_line
        )
        _r_line = next((l for l in (order.get("lines") or []) if str(l.get("eye_side","")).upper() in ("R","RIGHT")), None)
        _l_line = next((l for l in (order.get("lines") or []) if str(l.get("eye_side","")).upper() in ("L","LEFT")), None)
        _jc_order = {"id": order.get("id",""), "order_no": order.get("order_no",""),
                     "patient_name": order.get("patient_name",""),
                     "party_name": order.get("party_name",""),
                     "order_type": order.get("order_type") or "RETAIL"}
        _has_surf = any(l.get("surfacing_data") for l in (order.get("lines") or []))
        if _has_surf:
            if _r_line and _l_line:
                render_job_card_print_pair(_r_line, _l_line, _jc_order)
            elif _r_line or _l_line:
                render_job_card_print(_r_line or _l_line, _jc_order)
        else:
            st.info("📋 No job card saved yet — use the **🔧 Job Card & Blanks** tab to assign blanks and save.")
    except Exception as _e:
        import traceback
        st.error(f"Job card error: {_e}")
        with st.expander("Details"): st.code(traceback.format_exc())


def _render_label_print_tab(order: Dict):
    st.markdown("#### 🏷️ TSC Customer Label (75×50 mm)")
    _ctx = _resolved_card_context(order)
    _rx_r, _rx_l = _ctx["rx_r"], _ctx["rx_l"]
    _name, _mobile = _ctx["customer"], _ctx["mobile"]
    _product = _ctx["product"]
    _ono = order.get("order_no","")
    _optician = _ctx["optician"]

    _lc1, _lc2 = st.columns(2)
    _disp_name   = _lc1.text_input("Name on label",  value=_name,   key=f"lbl_name_{_ono}")
    _disp_mobile = _lc2.text_input("Mobile on label", value=_mobile, key=f"lbl_mob_{_ono}")

    patient_dict = {"id": _ono, "name": _disp_name, "mobile": _disp_mobile, "product": _product}

    _copies = st.number_input("Copies", min_value=1, max_value=20, value=1,
                               key=f"lbl_copies_{_ono}")

    try:
        from modules.core.barcode_label import render_patient_label
        render_patient_label(patient_dict, _rx_r, _rx_l)
    except Exception as _le:
        st.caption(f"Barcode library unavailable: {_le}")

    if st.button("🏷️ Print TSC Customer Label", key=f"lbl_print_{_ono}", type="primary",
                 use_container_width=True):
        _missing = _missing_blank_assignments_for_print(order)
        if _missing:
            st.error(f"🔴 Assignment not done — assign blank first for {'/'.join(_missing)} eye before printing labels.")
        else:
            try:
                from modules.printing.label_printer import print_tspl_customer_label
                ok, msg = print_tspl_customer_label(
                    order_no=_ono,
                    customer=_disp_name,
                    optician=_optician,
                    product=_product,
                    rx_r=_rx_r,
                    rx_l=_rx_l,
                    mobile=_disp_mobile,
                    date_text=_ctx["date_text"],
                    copies=int(_copies),
                )
            except Exception as exc:
                ok, msg = False, str(exc)
            if ok:
                st.success("Sent customer label to TSC")
            else:
                st.warning(f"TSC direct print failed: {msg}. Opening HTML standby.")
                _render_label_print_html(patient_dict, _rx_r, _rx_l, int(_copies))


def _render_cr80_card_tab(order: Dict):
    st.markdown("#### 💳 CR80 Plastic Authenticity Card (85.6×54 mm)")
    _ctx = _resolved_card_context(order)
    _rx_r, _rx_l = _ctx["rx_r"], _ctx["rx_l"]
    _name, _mobile = _ctx["customer"], _ctx["mobile"]
    _product = _ctx["product"]
    _ono   = order.get("order_no","")
    _party = _ctx["optician"]

    _lc1, _lc2 = st.columns(2)
    _disp_name   = _lc1.text_input("Customer name", value=_name,   key=f"cr80_name_{_ono}")
    _disp_mobile = _lc2.text_input("Mobile",        value=_mobile, key=f"cr80_mob_{_ono}")
    _lc3, _lc4 = st.columns(2)
    _disp_party  = _lc3.text_input("Dealer / Party", value=_party, key=f"cr80_party_{_ono}")
    _copies = _lc4.number_input("Copies", min_value=1, max_value=20, value=1,
                                  key=f"cr80_copies_{_ono}")

    _render_cr80_preview(_disp_name, _disp_mobile, _ono, _disp_party, _rx_r, _rx_l, _product, _ctx["date_text"])

    if st.button("💳 Print CR80 Plastic Card", key=f"cr80_print_{_ono}", type="primary",
                 use_container_width=True):
        _render_cr80_print_html(
            _disp_name, _disp_mobile, _ono, _disp_party, _rx_r, _rx_l,
            int(_copies), product=_product, date_text=_ctx["date_text"]
        )


def _fmt_power(v):
    if v is None: return "—"
    try:
        n = float(v)
        return f"{'+' if n >= 0 else ''}{n:.2f}"
    except Exception: return str(v)


def _print_html(content: str):
    """Open print dialog with HTML content."""
    import streamlit.components.v1 as _comp
    import base64 as _b64
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>
@media print {{ body {{ margin: 0; }} }}
.no-print {{ display: none; }}
</style>
</head>
<body>
{content}
</body></html>"""
    
    _b64_html = _b64.b64encode(html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var _raw=atob('{_b64_html}');var _buf=new Uint8Array(_raw.length);for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )


def _print_job_card(line: dict, surf: dict):
    """Backward-compatible wrapper for older buttons: use the canonical job-card print."""
    line = _production_job_line_for_print(line or {})
    if surf:
        line["surfacing_data"] = surf
    order = {
        "id": str(line.get("order_id") or ""),
        "order_no": str(line.get("order_no") or ""),
        "patient_name": str(line.get("patient_name") or ""),
        "party_name": str(line.get("party_name") or ""),
        "order_type": str(line.get("order_type") or "RETAIL"),
    }
    try:
        from modules.documents.job_card_surfacing import render_job_card_print
        render_job_card_print(line, order)
    except Exception as e:
        st.error(f"Job card print error: {e}")


def _print_barcode_75x50(line: dict, surf: dict):
    """Render barcode label 75×50mm with real SVG barcode."""
    import streamlit.components.v1 as _comp
    import base64 as _b64
    
    order_no = str(line.get("order_id", ""))
    eye = str(line.get("eye_side", "")).upper()
    product = line.get("product_name", "Unknown")[:20]
    barcode_value = f"{order_no}-{eye[0]}"
    
    # Use existing barcode generator from patient_card_printer
    try:
        from modules.printing.patient_card_printer import barcode_svg as _barcode_svg
        barcode_svg = _barcode_svg(barcode_value, width=180, height=36)
    except Exception:
        # Fallback to simple text if import fails
        barcode_svg = f"<div style='font-family:monospace;font-size:12px'>{barcode_value}</div>"
    
    def _fp(v):
        if v is None: return "—"
        try:
            n = float(v)
            return f"{'+' if n >= 0 else ''}{n:.2f}"
        except Exception: return str(v)
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>
@page {{ size: {css_size(TSC_LABEL_W_MM, TSC_LABEL_H_MM)}; margin: 0; }}
body {{ margin: 0; font-family: Arial, sans-serif; }}
.lbl {{ width: {TSC_LABEL_W_MM}mm; height: {TSC_LABEL_H_MM}mm; border: 1px solid #000; padding: 3mm; box-sizing: border-box; }}
.lbl .name {{ font-size: 10pt; font-weight: bold; text-align: center; margin-bottom: 2mm; }}
.lbl .ref {{ font-size: 7pt; color: #666; text-align: center; margin-bottom: 3mm; }}
.lbl .bc {{ text-align: center; margin: 2mm 0; }}
.lbl .pwr {{ font-size: 7pt; text-align: center; }}
.print-btn {{ display: block; margin: 10px auto; padding: 8px 16px; background: #2563eb; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }}
@media print {{ .print-btn {{ display: none; }} }}
</style>
</head>
<body>
<div class="lbl">
    <div class="name">{product}</div>
    <div class="ref">{order_no} · {eye}</div>
    <div class="bc">{barcode_svg}</div>
    <div class="pwr">SPH: {_fp(surf.get('sph_surf'))} | CYL: {_fp(surf.get('cyl_surf'))}</div>
    <button class="print-btn" onclick="window.print()">🖨️ Print</button>
</div>
</body></html>"""
    
    _b64_html = _b64.b64encode(html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var _raw=atob('{_b64_html}');var _buf=new Uint8Array(_raw.length);for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )


def _print_barcode_75x55(line: dict, surf: dict):
    """Legacy alias retained for older imports/buttons."""
    _print_barcode_75x50(line, surf)


def _print_cr80_card(line: dict, surf: dict):
    """Render CR80 authenticity card 85×54mm."""
    import streamlit.components.v1 as _comp
    import base64 as _b64
    
    order_no = str(line.get("order_id", ""))
    eye = str(line.get("eye_side", "")).upper()
    product = line.get("product_name", "Unknown")
    
    def _fp(v):
        if v is None: return "—"
        try:
            n = float(v)
            return f"{'+' if n >= 0 else ''}{n:.2f}"
        except Exception: return str(v)
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>
@page {{ size: {css_size(CR80_W_MM, CR80_H_MM)}; margin: 0; }}
body {{ margin: 0; font-family: Arial, sans-serif; }}
.card {{ width: {CR80_W_MM}mm; height: {CR80_H_MM}mm; box-sizing: border-box; padding: 4mm;
        background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 60%, #0f172a 100%);
        color: #fff; border-radius: 4px; position: relative; }}
.logo {{ position: absolute; top: 3mm; right: 4mm; font-size: 16px; opacity: 0.5; }}
.badge {{ font-size: 6pt; letter-spacing: 0.1em; text-transform: uppercase; color: #a78bfa; font-weight: bold; margin-bottom: 2mm; }}
.name {{ font-size: 11pt; font-weight: bold; margin-bottom: 1mm; }}
.mobile {{ font-size: 8pt; color: #94a3b8; margin-bottom: 3mm; }}
table {{ border-collapse: collapse; width: 100%; font-size: 7pt; }}
th {{ background: rgba(255,255,255,0.1); color: #94a3b8; padding: 1mm 2mm; text-align: center; }}
td {{ color: #e2e8f0; padding: 1mm 2mm; text-align: center; border-bottom: 0.3mm solid rgba(255,255,255,0.08); }}
.lbl {{ color: #64748b; text-align: left; }}
.footer {{ position: absolute; bottom: 2mm; left: 4mm; right: 4mm; display: flex; justify-content: space-between; }}
.ono {{ font-family: monospace; font-size: 7pt; color: #475569; }}
.dealer {{ font-size: 6pt; color: #334155; }}
.print-btn {{ position: absolute; bottom: 2mm; left: 50%; transform: translateX(-50%); padding: 6px 12px; background: #6366f1; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 10px; }}
@media print {{ .print-btn {{ display: none; }} }}
</style>
</head>
<body>
<div class="card">
    <div class="logo">👁️</div>
    <div class="badge">AUTHENTICITY CARD</div>
    <div class="name">{product}</div>
    <div class="mobile">{order_no} · {eye} EYE</div>
    <table>
        <tr><th></th><th>SPH</th><th>CYL</th><th>AX</th><th>ADD</th></tr>
        <tr><td class="lbl">{eye[0]}</td><td>{_fp(surf.get('sph_surf'))}</td><td>{_fp(surf.get('cyl_surf'))}</td><td>{surf.get('axis_surf','—')}</td><td>{_fp(surf.get('add_power_selected'))}</td></tr>
    </table>
    <div class="footer">
        <span class="ono">{order_no}</span>
        <span class="dealer">✓ Verified Quality</span>
    </div>
    <button class="print-btn" onclick="window.print()">🖨️ Print</button>
</div>
</body></html>"""
    
    _b64_html = _b64.b64encode(html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var _raw=atob('{_b64_html}');var _buf=new Uint8Array(_raw.length);for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )


def _render_cr80_preview(name, mobile, ono, party, rx_r, rx_l, product: str = "", date_text: str = ""):
    import streamlit.components.v1 as _comp
    _html = f"""<div style='width:340px;height:215px;box-sizing:border-box;padding:11px 13px 0;
    background:#fff;color:#000;border:2px solid #000;border-radius:8px;font-family:Arial;position:relative;
    box-shadow:0 3px 12px rgba(0,0,0,.18);overflow:hidden'>
    <div style='display:flex;justify-content:space-between;border-bottom:1px solid #000;padding-bottom:4px'>
      <div style='font-size:8px;letter-spacing:.12em;text-transform:uppercase;font-weight:900'>Authenticity Card</div>
      <div style='font-size:9px;font-weight:900;font-family:monospace'>{date_text}</div>
    </div>
    <div style='font-size:17px;font-weight:900;line-height:1.05;margin-top:5px'>{name or ""}</div>
    <div style='font-size:10px;font-weight:900;margin:2px 0 4px'>{f"Optician: {party}" if party else ""}</div>
    <div style='font-size:8px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid #999;padding-bottom:4px'>{product}</div>
    <table style='border-collapse:collapse;width:100%;font-size:9px;margin-top:5px'>
      <tr style='background:#000;color:#fff'><th style='padding:3px'></th><th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th></tr>
      <tr><td style='padding:4px;font-weight:900'>R</td><td>{_fmt_power(rx_r.get("sph"))}</td><td>{_fmt_power(rx_r.get("cyl"))}</td><td>{rx_r.get("axis","—")}</td><td>{_fmt_power(rx_r.get("add"))}</td></tr>
      <tr><td style='padding:4px;font-weight:900'>L</td><td>{_fmt_power(rx_l.get("sph"))}</td><td>{_fmt_power(rx_l.get("cyl"))}</td><td>{rx_l.get("axis","—")}</td><td>{_fmt_power(rx_l.get("add"))}</td></tr>
    </table>
    <div style='position:absolute;left:0;right:0;bottom:0;background:#000;color:#fff;text-align:center;font-size:10px;font-weight:900;padding:4px 0'>See Clearly, Check Regularly</div>
    <div style='position:absolute;bottom:24px;right:14px;font-size:8px;font-weight:900;font-family:monospace'>{ono}</div>
    </div>"""
    _comp.html(_html, height=240, scrolling=False)


def _render_cr80_print_html(name, mobile, ono, party, rx_r, rx_l, copies=1, product: str = "", date_text: str = ""):
    import streamlit.components.v1 as _comp
    import base64 as _b64

    def _fp2(v):
        if v is None: return "—"
        try:
            n = float(v)
            return f"+{n:.2f}" if n >= 0 else f"{n:.2f}"
        except Exception as e:
            log.debug("Display formatting fallback: %s", e)
            return str(v)

    def _ax2(v):
        if v is None: return "—"
        try: return str(int(float(v)))
        except Exception as e:
            log.debug("Display formatting fallback: %s", e)
            return str(v)

    _ono_clean = "".join(c for c in str(ono) if c.isalnum()) or "ORDER"
    _bc = _make_barcode_html(_ono_clean, height=22, width=250)
    _tagline = "See Clearly, Check Regularly"
    _card = f"""<div class='card'>
        <div class='head'><div class='badge'>AUTHENTICITY CARD</div><div class='ord'>{date_text}</div></div>
        <div class='name'>{name or ''}</div>
        <div class='mobile'>{f"Optician: {party}" if party else mobile}</div>
        <div class='prod'>{product}</div>
        <table><tr><th></th><th>SPH</th><th>CYL</th><th class='axis-hdr'>AXIS</th><th>ADD</th></tr>
        <tr><td class='lbl'>R</td><td>{_fp2(rx_r.get("sph"))}</td><td>{_fp2(rx_r.get("cyl"))}</td><td class='axis'>{_ax2(rx_r.get("axis"))}</td><td>{_fp2(rx_r.get("add"))}</td></tr>
        <tr><td class='lbl'>L</td><td>{_fp2(rx_l.get("sph"))}</td><td>{_fp2(rx_l.get("cyl"))}</td><td class='axis'>{_ax2(rx_l.get("axis"))}</td><td>{_fp2(rx_l.get("add"))}</td></tr>
        </table>
        <div class='footer'><div class='bc'>{_bc}</div><div class='dealer'>{ono}</div></div>
        <div class='tag'>{_tagline}</div>
        </div>"""
    _cards = "".join(
        _card + ("<div class='pg'></div>" if i < int(copies) - 1 else "")
        for i in range(int(copies))
    )
    _html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page{{size:{css_size(CR80_W_MM, CR80_H_MM)};margin:0}}body{{margin:0;font-family:Arial,Helvetica,sans-serif}}
    .card{{width:{CR80_W_MM}mm;height:{CR80_H_MM}mm;box-sizing:border-box;padding:2.2mm 3mm 0;
           background:#fff;color:#000;position:relative;display:flex;flex-direction:column;
           border:.45mm solid #000;overflow:hidden}}
    .pg{{page-break-after:always}}
    .head{{display:flex;justify-content:space-between;gap:2mm;border-bottom:.35mm solid #000;padding-bottom:.8mm}}
    .badge{{font-size:5.5pt;letter-spacing:.1em;text-transform:uppercase;color:#000;font-weight:900}}
    .ord{{font-size:5.5pt;font-family:Courier New,monospace;font-weight:900;text-align:right}}
    .name{{font-size:11.4pt;font-weight:900;line-height:1.05;color:#000;min-height:6.5mm;overflow:hidden;margin-top:.8mm}}
    .mobile{{font-size:6.6pt;color:#000;font-weight:900;margin-bottom:.5mm;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .prod{{font-size:6pt;color:#000;font-weight:900;margin-bottom:.8mm;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:.25mm solid #999;padding-bottom:.6mm}}
    table{{border-collapse:collapse;width:100%;font-size:7pt;margin-bottom:.8mm}}
    th{{background:#000;color:#fff;padding:.75mm 1.2mm;text-align:center;font-weight:900;font-size:6pt}}
    td{{color:#000;padding:.75mm 1.2mm;text-align:center;border-bottom:.25mm solid #000;font-weight:900}}
    td.lbl{{color:#000;text-align:left;font-weight:900}}
    .axis{{font-weight:900}}.axis-hdr{{color:#fff}}
    .footer{{margin-top:auto;display:flex;justify-content:space-between;align-items:flex-end;
             border-top:.25mm solid #999;padding-top:.5mm;gap:2mm}}
    .bc{{line-height:0}}
    .dealer{{font-size:5.3pt;color:#000;font-weight:900;text-align:right;max-width:34mm;line-height:1.15;padding-right:1mm}}
    .tag{{background:#000;color:#fff;text-align:center;margin:.6mm -3mm 0;padding:.85mm 0;
          font-size:7.4pt;font-weight:900;letter-spacing:.03em}}
    .no-print{{display:none}}@media print{{.no-print{{display:none!important}}}}
    </style></head><body>{_cards}
    <div class='no-print' style='text-align:center;padding:20px'>
    <button onclick='window.print()' style='background:#6366f1;color:#fff;border:none;
    padding:10px 32px;border-radius:8px;font-weight:700;cursor:pointer'>Print / Save PDF</button>
    </div></body></html>"""
    _b64_html = _b64.b64encode(_html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var _raw=atob('{_b64_html}');var _buf=new Uint8Array(_raw.length);for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )
    st.success(f"&#10003; Print dialog opened &mdash; {copies} copy/copies")


def _render_label_print_html(patient, rx_r, rx_l, copies=1):
    import streamlit.components.v1 as _comp
    import base64 as _b64

    def _fp(v):
        if v is None: return "—"
        try:
            n = float(v)
            return f"{'+' if n >= 0 else ''}{n:.2f}"
        except Exception: return str(v)

    _name   = patient.get("name","")
    _mobile = patient.get("mobile","")
    _id     = patient.get("id","")
    _product = patient.get("product","")
    _id_clean = "".join(c for c in str(_id) if c.isalnum()) or "ORDER"
    _bc_html = _make_barcode_html(_id_clean, height=24, width=280)
    _card = f"""<div class='lbl'>
        <div class='top'><div class='ln'>{_name}</div><div class='ono'>{_id}</div></div>
        <div class='lm'>{_mobile}</div>
        <div class='prod'>{_product}</div>
        <table><tr><th></th><th>SPH</th><th>CYL</th><th>AX</th><th>ADD</th></tr>
        <tr><td class='le'>R</td><td>{_fp(rx_r.get("sph"))}</td><td>{_fp(rx_r.get("cyl"))}</td><td>{rx_r.get("axis","—")}</td><td>{_fp(rx_r.get("add"))}</td></tr>
        <tr><td class='le'>L</td><td>{_fp(rx_l.get("sph"))}</td><td>{_fp(rx_l.get("cyl"))}</td><td>{rx_l.get("axis","—")}</td><td>{_fp(rx_l.get("add"))}</td></tr>
        </table>
        <div class='bc'>{_bc_html}</div>
        </div>"""
    _cards = "".join(
        _card + ("<div class='pg'></div>" if i < int(copies) - 1 else "")
        for i in range(int(copies))
    )
    _html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page{{size:{css_size(TSC_LABEL_W_MM, TSC_LABEL_H_MM)};margin:0}}body{{margin:0;font-family:Arial;color:#000;background:#fff}}
    .lbl{{width:{TSC_LABEL_W_MM}mm;height:{TSC_LABEL_H_MM}mm;box-sizing:border-box;padding:2mm 3mm;background:#fff;border:.45mm solid #000;overflow:hidden;page-break-inside:avoid}}
    .pg{{page-break-after:always}}
    .top{{display:flex;justify-content:space-between;gap:2mm;border-bottom:.3mm solid #000;padding-bottom:.7mm}}
    .ln{{font-size:9.5pt;font-weight:900;color:#000;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:45mm}}
    .ono{{font-size:5.8pt;font-weight:900;color:#000;font-family:Courier New,monospace;text-align:right}}
    .lm{{font-size:6.2pt;color:#000;margin:.6mm 0;font-family:Courier New,monospace;font-weight:800}}
    .prod{{font-size:6.2pt;color:#000;font-weight:900;margin-bottom:.8mm;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    table{{border-collapse:collapse;width:100%;font-size:7pt}}
    th{{background:#000;color:#fff;padding:.8mm 1.2mm;text-align:center;font-size:6pt}}
    td{{padding:.8mm 1.2mm;text-align:center;border-bottom:.25mm solid #000;color:#000;font-weight:900}}
    td.le{{color:#64748b;font-weight:700;text-align:left}}
    .bc{{text-align:center;margin-top:.7mm;line-height:0}}
    .no-print{{display:none}}@media print{{.no-print{{display:none!important}}}}
    </style></head><body>{_cards}
    <div class='no-print' style='text-align:center;padding:20px'>
    <button onclick='window.print()' style='background:#0f172a;color:#fff;border:none;
    padding:10px 32px;border-radius:8px;font-weight:700;cursor:pointer'>🖨️ Print Labels</button>
    </div></body></html>"""
    _b64_html = _b64.b64encode(_html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var _raw=atob('{_b64_html}');var _buf=new Uint8Array(_raw.length);for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )
    st.success(f"✅ Label print dialog opened — {copies} copy/copies")
