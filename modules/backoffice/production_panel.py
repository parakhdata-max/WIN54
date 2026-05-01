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
from typing import Dict, List, Optional

from .event_logger import log_event, render_event_timeline, EventType


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
               ol.eye_side, p.product_name, p.id AS product_id,
               p.coating_type,
               ol.lens_params
        FROM job_master jm
        JOIN order_lines ol ON ol.id = jm.order_line_id
        JOIN orders o       ON o.id  = ol.order_id
        JOIN products p     ON p.id  = ol.product_id
        WHERE o.order_no = %(ono)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
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
                    "WHERE job_id = %(j)s::uuid AND stage_code = %(s)s "
                    "ORDER BY created_at DESC LIMIT 1",
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
    """Return fitting types from DB."""
    try:
        rows = _q("""
            SELECT code, label FROM fitting_types
            WHERE is_active = TRUE ORDER BY sort_order
        """, {})
        return [{"code": r["code"], "label": r["label"]} for r in rows] if rows else []
    except Exception:
        return []


def _get_fitter_rate(fitter_id: str, fitting_type_code: str) -> float:
    """Lookup rate for fitter+type from rate chart."""
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
            except: lp = {}
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
                except: raw = {}
            lp = raw if isinstance(raw, dict) else {}
        lp["colour_final_photo"] = b64
        _rwp("UPDATE order_lines SET lens_params=%(lp)s::jsonb WHERE id=%(l)s::uuid",
             {"lp": _jsp.dumps(lp), "l": order_line_id})
        return True
    except Exception:
        return False


def render_production_panel(order: Dict) -> None:
    order_id = order.get("order_no") or str(order.get("id", ""))

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
        "order_type":   "RETAIL",
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
            except: lp = {}
        return bool(lp.get("surfacing_data"))

    def _get_surf(line):
        """Return surfacing_data from line or lens_params."""
        if line.get("surfacing_data"):
            return line["surfacing_data"]
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            import json as _j
            try: lp = _j.loads(lp)
            except: lp = {}
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
            from modules.documents.job_card_surfacing import render_surfacing_job_card
            render_surfacing_job_card(_l_line, _jc_order)

    st.markdown("---")

    # ── Save hint if not saved ─────────────────────────────────────────
    _needs_save = (
        (_r_line and not _r_saved) or
        (_l_line and not _l_saved)
    )
    if _needs_save:
        st.info("👆 Fill forms above and click Save inside each eye form")
    
    # ── PRINT BUTTONS AT TOP ─────────────────────────────────────────
    if _any:
        _print_key = f"prod_print_{order.get('order_no','')}"
        _bar_key   = f"prod_bar_{order.get('order_no','')}"
        _cr80_key  = f"prod_cr80_{order.get('order_no','')}"

        _saved_lines = [l for l in [_r_line, _l_line] if l and _has_surf(l)]
        for _sl in _saved_lines:
            _get_surf(_sl)

        st.markdown("### 🖨️ Print Options")
        _bc1, _bc2, _bc3 = st.columns(3)
        with _bc1:
            if st.button("🖨️ Print Job Card",
                         use_container_width=True,
                         type="primary",
                         key="prod_print_jc"):
                st.session_state[_print_key] = True
                st.rerun()
        with _bc2:
            if st.button("🏷️ Print Label(s)",
                         use_container_width=True,
                         key="prod_print_bar"):
                st.session_state[_bar_key] = True
                st.rerun()
        with _bc3:
            if st.button("💳 Print Authenticity Card",
                         use_container_width=True,
                         key="prod_print_cr80"):
                st.session_state[_cr80_key] = True
                st.rerun()

    # ── PRINT BUTTONS — only after at least one saved ──────────────────
    if _any:
        _print_key = f"prod_print_{order.get('order_no','')}"
        _bar_key   = f"prod_bar_{order.get('order_no','')}"
        _cr80_key  = f"prod_cr80_{order.get('order_no','')}"

        # ── Universal print buttons — adapt to what's saved ────────
        _saved_lines = [l for l in [_r_line, _l_line] if l and _has_surf(l)]
        for _sl in _saved_lines:
            _get_surf(_sl)   # ensure surfacing_data loaded into line dict

        _bc1, _bc2, _bc3 = st.columns(3)
        with _bc1:
            if st.button("🖨️ Print Job Card",
                         use_container_width=True,
                         type="primary" if _both else "secondary",
                         key="prod_print_jc"):
                st.session_state[_print_key] = True
                st.rerun()
        with _bc2:
            if st.button("🏷️ Print Label(s)",
                         use_container_width=True,
                         type="primary",
                         key="prod_print_bar",
                         help="75×65mm barcode label — R, L, or both"):
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
        if st.session_state.get(_print_key):
            st.session_state.pop(_print_key, None)
            from modules.documents.job_card_surfacing import _open_jc_print_window
            _open_jc_print_window(
                _r_line if _r_saved else None,
                _l_line if _l_saved else None,
                _jc_order
            )

        if st.session_state.get(_bar_key):
            st.session_state.pop(_bar_key, None)
            _open_print_window(_build_label_page(_saved_lines, _jc_order))

        if st.session_state.get(_cr80_key):
            st.session_state.pop(_cr80_key, None)
            _open_print_window(_build_cr80_page(
                _r_line if _r_saved else None,
                _l_line if _l_saved else None,
                _jc_order
            ))



# ══════════════════════════════════════════════════════════════
# PRINT FUNCTIONS — Label (75×65mm) + CR80 Customer Card
# ══════════════════════════════════════════════════════════════

def _fp(v, default="—"):
    """Format power value with sign."""
    if v is None: return default
    try:
        n = float(v)
        if n == 0.0: return "0.00"
        return f"+{n:.2f}" if n > 0 else f"{n:.2f}"
    except: return str(v)


def _make_barcode_html(value: str, height: int = 40) -> str:
    """Generate barcode as text-based visual — works without external libs."""
    # Use CODE128-style visual representation
    try:
        from modules.printing.patient_card_printer import barcode_svg as _bsvg
        return _bsvg(value, width=200, height=height)
    except Exception:
        # Fallback: styled text barcode representation
        bars = "".join(
            f"<span style='display:inline-block;width:{2 if ord(c)%2==0 else 1}px;"
            f"height:{height}px;background:#000;margin:0'></span>"
            f"<span style='display:inline-block;width:{1 if ord(c)%3==0 else 2}px;"
            f"height:{height}px;margin:0'></span>"
            for c in value
        )
        return (
            f"<div style='display:inline-block;border:1px solid #000;padding:2px 4px'>"
            f"<div style='white-space:nowrap;line-height:0'>{bars}</div>"
            f"<div style='font-family:monospace;font-size:7pt;text-align:center;margin-top:2px'>{value}</div>"
            f"</div>"
        )


def _make_label_html(line: dict, order: dict, eye: str) -> str:
    """
    75×65mm barcode label for TSC-244 Pro.
    Layout:
      TOP LEFT: party/patient name + order_no + eye  |  TOP RIGHT: date
      PRODUCT name row
      POWER boxes: SPH | CYL | AXIS | ADD
      BARCODE 1: order number  |  BARCODE 2: party code
      BOTTOM: frame | KT/SV | shop name
    """
    import datetime as _dt

    surf = line.get("surfacing_data") or {}
    lp   = line.get("lens_params") or {}
    if isinstance(lp, str):
        import json as _jl
        try: lp = _jl.loads(lp)
        except: lp = {}

    order_no   = order.get("order_no", line.get("order_no", "")) or ""
    today      = _dt.date.today().strftime("%d-%m-%Y")
    order_type = (order.get("order_type") or line.get("order_type") or "RETAIL").upper()

    # Party name: retail → patient name, wholesale → party name
    if order_type in ("WHOLESALE", "WS"):
        party_name = (order.get("party_name") or order.get("patient_name") or "")[:30]
    else:
        party_name = (order.get("patient_name") or order.get("party_name") or "")[:30]

    def _fp_lbl(v):
        if v is None: return "&mdash;"
        try:
            n = float(v)
            if n == 0.0: return "0.00"
            return f"+{n:.2f}" if n > 0 else f"{n:.2f}"
        except: return str(v)

    sph_rx  = _fp_lbl(line.get("sph"))
    cyl_rx  = _fp_lbl(line.get("cyl"))
    _ax_raw = line.get("axis")
    axis_rx = str(int(float(_ax_raw))) if _ax_raw not in (None, "", 0, "0") else "&mdash;"
    add_rx  = _fp_lbl(line.get("add_power"))

    product    = (line.get("product_name") or "")[:35]
    category   = (line.get("category") or lp.get("manufacturing_route") or "").upper()
    frame_lp   = (lp.get("frame_type") or surf.get("frame_type") or "SUPRA").upper()
    eye_label  = "R" if eye.upper()[:1] == "R" else "L"
    blank_batch = surf.get("blank_batch") or ""

    # Barcode 1: order number (stripped)
    _ono_clean  = "".join(c for c in order_no if c.isalnum())
    # Barcode 2: party code = first alphanum chars of party name + order suffix
    _party_code = "".join(c for c in party_name if c.isalnum())[:8] + _ono_clean[-4:] + eye_label

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

    bc1_html = _make_barcode_html(_ono_clean + eye_label, height=30)
    bc2_html = _make_barcode_html(_party_code, height=30)

    _pwr_boxes = "".join(
        f"<div style='border:1.5px solid #000;flex:1;text-align:center;padding:0.5mm 0'>"
        f"<div style='font-size:5pt;color:#555'>{lbl}</div>"
        f"<div style='font-size:8pt;font-weight:900;font-family:monospace'>{val}</div>"
        f"</div>"
        for lbl, val in [("SPH", sph_rx), ("CYL", cyl_rx), ("AXIS", axis_rx), ("ADD", add_rx)]
    )

    return (
        f"<div style='width:75mm;height:65mm;border:1.5px solid #000;box-sizing:border-box;"
        f"font-family:Arial,Helvetica,sans-serif;overflow:hidden;page-break-after:always;"
        f"display:flex;flex-direction:column;padding:0;background:#fff'>"

        # TOP BAR — party/patient name + order + eye  |  date + batch
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start;"
        f"border-bottom:1px solid #000;padding:1.5mm 2mm 1mm'>"
        f"<div>"
        f"<div style='font-size:7.5pt;font-weight:900;letter-spacing:-.2px'>{party_name}</div>"
        f"<div style='font-size:6.5pt;font-weight:700'>{order_no} {eye_label}</div>"
        f"</div>"
        f"<div style='text-align:right'>"
        f"<div style='font-size:6pt;color:#555'>{today}</div>"
        + (f"<div style='border:1px solid #000;padding:0.5mm 1.5mm;font-size:7.5pt;font-weight:900;"
           f"margin-top:0.5mm;text-align:center'>{blank_batch}</div>" if blank_batch else "")
        + f"</div>"
        f"</div>"

        # PRODUCT NAME
        f"<div style='padding:1mm 2mm 0.5mm;border-bottom:1px solid #ccc'>"
        f"<div style='font-size:6.5pt;font-weight:700;line-height:1.2'>- {product}</div>"
        f"</div>"

        # POWER BOXES
        f"<div style='display:flex;gap:1mm;padding:1.5mm 2mm;border-bottom:1px solid #000'>"
        f"{_pwr_boxes}"
        f"</div>"

        # TWO BARCODES: order no | party code
        f"<div style='display:flex;justify-content:space-between;padding:1mm 2mm;"
        f"border-bottom:1px solid #ccc;gap:2mm;flex:1;align-items:center'>"
        f"<div style='flex:1;text-align:center'>{bc1_html}</div>"
        f"<div style='flex:1;text-align:center'>{bc2_html}</div>"
        f"</div>"

        # BOTTOM: frame | category | shop
        f"<div style='display:flex;justify-content:space-between;padding:1mm 2mm'>"
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

    order_no  = order.get("order_no", "—")
    patient   = order.get("patient_name", "—")
    today     = _dt.date.today().strftime("%d-%m-%Y")

    product   = (r_line.get("product_name") or l_line.get("product_name") or "—")[:40]

    shop_name = "DV Optical"
    tagline   = "See Clearly Check Regularly"
    try:
        from modules.settings.shop_master import get_unit_info
        _sh = get_unit_info("retail")
        shop_name = _sh.get("shop_name", shop_name)
        tagline   = _sh.get("tagline", tagline) or tagline
    except Exception:
        pass

    def _row(line, eye_label):
        return (
            f"<tr>"
            f"<td style='padding:1mm 2mm;font-weight:700'>{eye_label}</td>"
            f"<td style='padding:1mm 2mm;text-align:center'>{_fp(line.get('sph'))}</td>"
            f"<td style='padding:1mm 2mm;text-align:center'>{_fp(line.get('cyl'))}</td>"
            f"<td style='padding:1mm 2mm;text-align:center'>{int(float(line.get('axis') or 0))}</td>"
            f"<td style='padding:1mm 2mm;text-align:center'>{_fp(line.get('add_power'))}</td>"
            f"</tr>"
        )

    r_row = _row(r_line, "Right") if r_line else ""
    l_row = _row(l_line, "Left")  if l_line else ""

    return f"""
<div style="width:85mm;height:54mm;border:2px solid #000;box-sizing:border-box;
     font-family:Arial,Helvetica,sans-serif;page-break-after:always;
     display:flex;flex-direction:column;overflow:hidden">

  <!-- NAME HEADER -->
  <div style="border-bottom:1.5px solid #000;padding:1.5mm 3mm;display:flex;
       justify-content:space-between;align-items:center">
    <div style="font-size:8pt;font-weight:900;text-decoration:underline">Name</div>
    <div style="font-size:7pt;font-weight:700">{patient}</div>
  </div>

  <!-- POWER TABLE -->
  <div style="padding:0 2mm">
    <table style="width:100%;border-collapse:collapse;font-size:7.5pt">
      <tr style="color:#555;font-size:6pt">
        <td style="padding:0.5mm 2mm"></td>
        <td style="padding:0.5mm 2mm;text-align:center;font-weight:700">Sph</td>
        <td style="padding:0.5mm 2mm;text-align:center;font-weight:700">Cyl</td>
        <td style="padding:0.5mm 2mm;text-align:center;font-weight:700">Axis</td>
        <td style="padding:0.5mm 2mm;text-align:center;font-weight:700">Add</td>
      </tr>
      {r_row}{l_row}
    </table>
  </div>

  <!-- PRODUCT + ORDER -->
  <div style="padding:1mm 3mm;border-top:1px solid #ccc;border-bottom:1px solid #ccc">
    <div style="font-size:6.5pt">
      <span style="text-decoration:underline;font-weight:700">Product</span>
      &nbsp; {product}
    </div>
    <div style="font-size:6.5pt;margin-top:0.5mm">
      <span style="text-decoration:underline;font-weight:700">Order No</span>
      &nbsp; {order_no} dated: {today}
    </div>
  </div>

  <!-- TAGLINE FOOTER -->
  <div style="background:#1e293b;color:#fff;text-align:center;
       padding:1.5mm;font-size:7pt;font-weight:700;margin-top:auto;letter-spacing:.03em">
    {tagline}
  </div>
</div>
"""


def _open_print_window(html: str) -> None:
    """Open HTML in new tab for printing."""
    import streamlit.components.v1 as _comp
    import base64 as _b64
    _b64_html = _b64.b64encode(html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{"
        f"var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});"
        f"window.open(URL.createObjectURL(b),'_blank')"
        f"}})();</script>",
        height=0
    )


def _build_label_page(lines_to_print: list, order: dict) -> str:
    """
    Build full printable HTML page — one 75×40mm label per page.
    TSC-244 Pro thermal printer: @page size 75×65mm, one label per page.
    R and L print on separate pages (page-break-after:always on each label div).
    Screen preview shows both labels stacked for review before printing.
    """
    labels_html = "".join(
        _make_label_html(ln, order, str(ln.get("eye_side","R")).upper())
        for ln in lines_to_print
    )
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
  @page {{ size: 75mm 65mm; margin: 0; }}
  body {{ margin: 0; background: #f5f5f5; font-family: Arial,Helvetica,sans-serif; }}
  @media print {{
    body {{ background: #fff; }}
    .no-print {{ display: none !important; }}
  }}
</style></head><body>
{labels_html}
<div class='no-print' style='text-align:center;padding:16px;background:#f5f5f5'>
  <p style='font-size:11px;color:#666;margin:0 0 8px'>
    TSC-244 Pro &mdash; 75&times;40mm labels &mdash; one label per page
  </p>
  <button onclick="document.querySelectorAll('.no-print').forEach(e=>e.style.display='none');window.print();setTimeout(()=>document.querySelectorAll('.no-print').forEach(e=>e.style.display=''),800)"
    style="background:#2563eb;color:#fff;border:none;padding:10px 24px;border-radius:6px;
           font-size:14px;cursor:pointer;font-weight:700">
    Print Label(s)
  </button>
</div>
</body></html>"""


def _build_cr80_page(r_line, l_line, order: dict) -> str:
    """Build full printable HTML page for CR80 customer card."""
    card_html = _make_cr80_html(r_line, l_line, order)
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
  @page {{ size: 85mm 54mm; margin: 0; }}
  body {{ margin: 0; background: #fff; }}
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
        except: return str(v)

    def _int(v, default="—"):
        if v is None: return default
        try: return str(int(float(v)))
        except: return str(v)

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
            except: lp = {}

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
  @page {{ size: A4; margin: 10mm; }}
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
    _open_print_window(_build_label_page([r_line, l_line], r_line))

def _print_barcode_single(line):
    _open_print_window(_build_label_page([line], line))

def _print_cr80_pair(r_line, l_line):
    _open_print_window(_build_cr80_page(r_line, l_line, r_line))

def _print_cr80_single(line):
    eye = str(line.get("eye_side","")).upper()
    r = line if eye[:1] == "R" else None
    l = line if eye[:1] == "L" else None
    _open_print_window(_build_cr80_page(r, l, line))


def _render_labels_tab(order: Dict) -> None:
    """🏷️ Print Labels tab — 75×65mm barcode labels for R and L."""
    st.markdown("#### 🏷️ Barcode Labels (75×65mm)")

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
            except: lp={}
        return bool(lp.get("surfacing_data"))

    def _load(ln):
        if not ln: return ln
        if ln.get("surfacing_data"): return ln
        lp = ln.get("lens_params") or {}
        if isinstance(lp,str):
            import json as _jl
            try: lp=_jl.loads(lp)
            except: lp={}
        sd = lp.get("surfacing_data")
        if sd: ln = dict(ln); ln["surfacing_data"] = sd
        return ln

    r_line = _load(r_line)
    l_line = _load(l_line)
    r_ok = _has(r_line)
    l_ok = _has(l_line)

    _jc_order = {
        "id":           order.get("id",""),
        "order_no":     order.get("order_no",""),
        "patient_name": order.get("patient_name",""),
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
        if r_ok and st.button("🏷️ Print R Label", key="lbl_print_r",
                               use_container_width=True, type="primary"):
            _open_print_window(_build_label_page([r_line], _jc_order))
    with _lb2:
        if l_ok and st.button("🏷️ Print L Label", key="lbl_print_l",
                               use_container_width=True, type="primary"):
            _open_print_window(_build_label_page([l_line], _jc_order))
    with _lb3:
        if r_ok and l_ok and st.button("🏷️ Print Both Labels", key="lbl_print_both",
                                        use_container_width=True, type="primary"):
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
                        # Trigger print for both
                        _print_job_card(r_job, {})
                        _print_job_card(l_job, {})
                with pc2:
                    if st.button("🏷️ Barcode", key=f"pair_bc_{r_job_id[:8]}", use_container_width=True):
                        _print_barcode_75x55(r_job, {})
                        _print_barcode_75x55(l_job, {})
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
                        except:
                            lp = {}

                    surf = lp.get("surfacing_data", {})

                    if ptype == "jobcard":
                        _print_job_card(line, surf)
                    elif ptype == "barcode":
                        _print_barcode_75x55(line, surf)
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
                    st.caption("ℹ️ Add fitters in Settings → Fitter Management")

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
    for _l in (order.get("lines") or []):
        _es = str(_l.get("eye_side","")).upper()
        _rx = {"sph": _l.get("sph"), "cyl": _l.get("cyl"),
               "axis": _l.get("axis"), "add": _l.get("add_power")}
        if _es in ("R","RIGHT"):   rx_r = _rx
        elif _es in ("L","LEFT"):  rx_l = _rx
    return rx_r, rx_l


def _resolve_customer(order: Dict):
    import json as _json
    _ec = {}
    _extra = order.get("extra_data") or {}
    if isinstance(_extra, str):
        try: _extra = _json.loads(_extra)
        except Exception: _extra = {}
    if isinstance(_extra, dict):
        _ec = _extra.get("end_customer") or {}
    return (
        _ec.get("name") or order.get("patient_name") or "",
        _ec.get("mobile") or order.get("patient_mobile") or "",
    )


def _render_job_card_print_tab(order: Dict):
    st.markdown("#### 🖨️ Job Card Print")
    try:
        from modules.documents.job_card_surfacing import (
            render_job_card_print_pair, render_job_card_print, render_surfacing_job_card, save_job_card_line
        )
        _r_line = next((l for l in (order.get("lines") or []) if str(l.get("eye_side","")).upper() in ("R","RIGHT")), None)
        _l_line = next((l for l in (order.get("lines") or []) if str(l.get("eye_side","")).upper() in ("L","LEFT")), None)
        _jc_order = {"id": order.get("id",""), "order_no": order.get("order_no",""),
                     "patient_name": order.get("patient_name",""), "order_type": "RETAIL"}
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
    st.markdown("#### 🏷️ Barcode Label Print (75×55 mm)")
    _rx_r, _rx_l = _resolve_order_rx(order)
    _name, _mobile = _resolve_customer(order)
    _ono = order.get("order_no","")

    _lc1, _lc2 = st.columns(2)
    _disp_name   = _lc1.text_input("Name on label",  value=_name,   key=f"lbl_name_{_ono}")
    _disp_mobile = _lc2.text_input("Mobile on label", value=_mobile, key=f"lbl_mob_{_ono}")

    patient_dict = {"id": _ono, "name": _disp_name, "mobile": _disp_mobile}

    _copies = st.number_input("Copies", min_value=1, max_value=20, value=1,
                               key=f"lbl_copies_{_ono}")

    try:
        from modules.core.barcode_label import render_patient_label
        render_patient_label(patient_dict, _rx_r, _rx_l)
    except Exception as _le:
        st.caption(f"Barcode library unavailable: {_le}")

    if st.button("🖨️ Print Label", key=f"lbl_print_{_ono}", type="primary",
                 use_container_width=True):
        _render_label_print_html(patient_dict, _rx_r, _rx_l, int(_copies))


def _render_cr80_card_tab(order: Dict):
    st.markdown("#### 💳 CR80 Authenticity Card (85×54 mm)")
    _rx_r, _rx_l = _resolve_order_rx(order)
    _name, _mobile = _resolve_customer(order)
    _ono   = order.get("order_no","")
    _party = order.get("party_name","")

    _lc1, _lc2 = st.columns(2)
    _disp_name   = _lc1.text_input("Customer name", value=_name,   key=f"cr80_name_{_ono}")
    _disp_mobile = _lc2.text_input("Mobile",        value=_mobile, key=f"cr80_mob_{_ono}")
    _lc3, _lc4 = st.columns(2)
    _disp_party  = _lc3.text_input("Dealer / Party", value=_party, key=f"cr80_party_{_ono}")
    _copies = _lc4.number_input("Copies", min_value=1, max_value=20, value=1,
                                  key=f"cr80_copies_{_ono}")

    _render_cr80_preview(_disp_name, _disp_mobile, _ono, _disp_party, _rx_r, _rx_l)

    if st.button("🖨️ Print CR80 Card", key=f"cr80_print_{_ono}", type="primary",
                 use_container_width=True):
        _render_cr80_print_html(_disp_name, _disp_mobile, _ono, _disp_party, _rx_r, _rx_l, int(_copies))


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
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )


def _print_job_card(line: dict, surf: dict):
    """Render proper job card print layout."""
    import streamlit.components.v1 as _comp
    import base64 as _b64
    
    def _fp(v):
        if v is None: return "—"
        try:
            n = float(v)
            return f"{'+' if n >= 0 else ''}{n:.2f}"
        except Exception: return str(v)
    
    order_no = str(line.get("order_id", ""))
    eye = str(line.get("eye_side", "")).upper()
    product = line.get("product_name", "Unknown")
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>
@page {{ size: A4; margin: 10mm; }}
body {{ font-family: Arial, sans-serif; font-size: 12px; }}
.jc {{ width: 180mm; border: 2px solid #000; padding: 15mm; margin: 0 auto; }}
.hdr {{ background: #1a1a1a; color: #fff; padding: 10px; text-align: center; margin-bottom: 15px; }}
.hdr h2 {{ margin: 0; }}
table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
th, td {{ border: 1px solid #333; padding: 8px; text-align: left; }}
th {{ background: #f0f0f0; }}
.barcode {{ text-align: center; margin-top: 15px; }}
.print-btn {{ display: block; margin: 20px auto; padding: 12px 24px; background: #2563eb; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }}
@media print {{ .print-btn {{ display: none; }} }}
</style>
</head>
<body>
<div class="jc">
    <div class="hdr">
        <h2>👓 JOB CARD</h2>
        <div>{order_no} | {eye} EYE</div>
    </div>
    <table>
        <tr><th>Product</th><td>{product}</td></tr>
        <tr><th>Blank Brand</th><td>{surf.get('blank_brand', '—')}</td></tr>
        <tr><th>Material</th><td>{surf.get('blank_material', '—')}</td></tr>
        <tr><th>Base Curve</th><td>{_fp(surf.get('base_curve'))}</td></tr>
        <tr><th>Diameter</th><td>{surf.get('diameter', '—')}</td></tr>
        <tr><th>Frame Type</th><td>{surf.get('frame_type', '—')}</td></tr>
        <tr><th>Edge Finish</th><td>{surf.get('edge_finish', '—')}</td></tr>
        <tr><th>Priority</th><td>{surf.get('priority', 'Standard')}</td></tr>
    </table>
    <div class="barcode">
        <svg id="jc_barcode_{eye}" Barcode="{order_no}-{eye[0]}"></svg>
    </div>
    <button class="print-btn" onclick="window.print()">🖨️ Print Job Card</button>
</div>
</body></html>"""
    
    _b64_html = _b64.b64encode(html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )


def _print_barcode_75x55(line: dict, surf: dict):
    """Render barcode label 75×55mm with real SVG barcode."""
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
@page {{ size: 75mm 55mm; margin: 0; }}
body {{ margin: 0; font-family: Arial, sans-serif; }}
.lbl {{ width: 75mm; height: 55mm; border: 1px solid #000; padding: 3mm; box-sizing: border-box; }}
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
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )


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
@page {{ size: 85mm 54mm; margin: 0; }}
body {{ margin: 0; font-family: Arial, sans-serif; }}
.card {{ width: 85mm; height: 54mm; box-sizing: border-box; padding: 4mm;
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
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )


def _render_cr80_preview(name, mobile, ono, party, rx_r, rx_l):
    import streamlit.components.v1 as _comp
    _html = f"""<div style='width:340px;height:215px;box-sizing:border-box;padding:16px 20px;
    background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 60%,#0f172a 100%);color:#fff;
    border-radius:12px;font-family:Arial;position:relative;box-shadow:0 4px 20px rgba(0,0,0,.5)'>
    <div style='position:absolute;top:16px;right:20px;font-size:22px;opacity:.5'>👁️</div>
    <div style='font-size:8px;letter-spacing:.12em;text-transform:uppercase;color:#a78bfa;font-weight:700;margin-bottom:6px'>Authenticity Card</div>
    <div style='font-size:16px;font-weight:700;margin-bottom:4px'>{name or "—"}</div>
    <div style='font-size:10px;color:#94a3b8;margin-bottom:10px'>{mobile}</div>
    <table style='border-collapse:collapse;width:100%;font-size:9px'>
      <tr style='background:rgba(255,255,255,.1)'><th style='padding:3px 5px;text-align:left;color:#94a3b8'></th><th style='padding:3px 5px;color:#94a3b8'>SPH</th><th style='padding:3px 5px;color:#94a3b8'>CYL</th><th style='padding:3px 5px;color:#94a3b8'>AXIS</th><th style='padding:3px 5px;color:#94a3b8'>ADD</th></tr>
      <tr><td style='padding:3px 5px;color:#64748b'>R</td><td style='padding:3px 5px;color:#e2e8f0'>{_fmt_power(rx_r.get("sph"))}</td><td style='padding:3px 5px;color:#e2e8f0'>{_fmt_power(rx_r.get("cyl"))}</td><td style='padding:3px 5px;color:#e2e8f0'>{rx_r.get("axis","—")}</td><td style='padding:3px 5px;color:#e2e8f0'>{_fmt_power(rx_r.get("add"))}</td></tr>
      <tr><td style='padding:3px 5px;color:#64748b'>L</td><td style='padding:3px 5px;color:#e2e8f0'>{_fmt_power(rx_l.get("sph"))}</td><td style='padding:3px 5px;color:#e2e8f0'>{_fmt_power(rx_l.get("cyl"))}</td><td style='padding:3px 5px;color:#e2e8f0'>{rx_l.get("axis","—")}</td><td style='padding:3px 5px;color:#e2e8f0'>{_fmt_power(rx_l.get("add"))}</td></tr>
    </table>
    <div style='position:absolute;bottom:12px;left:20px;right:20px;display:flex;justify-content:space-between'>
      <span style='font-family:monospace;font-size:9px;color:#475569'>{ono}</span>
      <span style='font-size:8px;color:#334155'>{party}</span>
    </div></div>"""
    _comp.html(_html, height=240, scrolling=False)


def _render_cr80_print_html(name, mobile, ono, party, rx_r, rx_l, copies=1):
    import streamlit.components.v1 as _comp
    import base64 as _b64

    def _fp2(v):
        if v is None: return "—"
        try:
            n = float(v)
            return f"+{n:.2f}" if n >= 0 else f"{n:.2f}"
        except: return str(v)

    def _ax2(v):
        if v is None: return "—"
        try: return str(int(float(v)))
        except: return str(v)

    _cards = "".join(
        f"""<div class='card'>
        <div class='logo'>&#9673;</div>
        <div class='badge'>AUTHENTICITY CARD</div>
        <div class='name'>{name or '&mdash;'}</div>
        <div class='mobile'>{mobile}</div>
        <table><tr><th></th><th>SPH</th><th>CYL</th><th class='axis-hdr'>AXIS</th><th>ADD</th></tr>
        <tr><td class='lbl'>R</td><td>{_fp2(rx_r.get("sph"))}</td><td>{_fp2(rx_r.get("cyl"))}</td><td class='axis'>{_ax2(rx_r.get("axis"))}</td><td>{_fp2(rx_r.get("add"))}</td></tr>
        <tr><td class='lbl'>L</td><td>{_fp2(rx_l.get("sph"))}</td><td>{_fp2(rx_l.get("cyl"))}</td><td class='axis'>{_ax2(rx_l.get("axis"))}</td><td>{_fp2(rx_l.get("add"))}</td></tr>
        </table>
        <div class='footer'>
          <div class='ono-bc'><div style='font-family:monospace;font-size:6.5pt;color:#94a3b8;letter-spacing:.05em'>{"".join(c for c in ono if c.isalnum())}</div></div>
          <div class='dealer'>{party}</div>
        </div>
        </div><div style='page-break-after:always'></div>"""
        for _ in range(copies)
    )
    _html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page{{size:85mm 54mm;margin:0}}body{{margin:0;font-family:Arial,Helvetica,sans-serif}}
    .card{{width:85mm;height:54mm;box-sizing:border-box;padding:3.5mm 5mm 2mm;
           background:linear-gradient(135deg,#0f172a,#1e3a5f);color:#fff;position:relative;
           display:flex;flex-direction:column}}
    .logo{{position:absolute;top:3.5mm;right:5mm;font-size:14pt;opacity:.4;color:#a78bfa}}
    .badge{{font-size:5.5pt;letter-spacing:.12em;text-transform:uppercase;color:#a78bfa;font-weight:700;margin-bottom:1.5mm}}
    .name{{font-size:11pt;font-weight:700;margin-bottom:0.5mm;color:#f1f5f9}}
    .mobile{{font-size:7.5pt;color:#94a3b8;margin-bottom:1.5mm}}
    table{{border-collapse:collapse;width:100%;font-size:7pt;margin-bottom:1.5mm}}
    th{{background:rgba(255,255,255,.1);color:#94a3b8;padding:0.8mm 1.5mm;text-align:center;font-weight:600}}
    td{{color:#e2e8f0;padding:0.8mm 1.5mm;text-align:center;border-bottom:.3mm solid rgba(255,255,255,.08)}}
    td.lbl{{color:#64748b;text-align:left;font-weight:700}}
    .axis{{color:#fde68a;font-weight:900}}.axis-hdr{{color:#fde68a}}
    .footer{{margin-top:auto;display:flex;justify-content:space-between;align-items:flex-end;
             border-top:.3mm solid rgba(255,255,255,.15);padding-top:1mm}}
    .ono-bc{{font-family:monospace;font-size:6pt;color:#94a3b8}}
    .dealer{{font-size:6pt;color:#475569}}
    .no-print{{display:none}}@media print{{.no-print{{display:none!important}}}}
    </style></head><body>{_cards}
    <div class='no-print' style='text-align:center;padding:20px'>
    <button onclick='window.print()' style='background:#6366f1;color:#fff;border:none;
    padding:10px 32px;border-radius:8px;font-weight:700;cursor:pointer'>Print / Save PDF</button>
    </div></body></html>"""
    _b64_html = _b64.b64encode(_html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
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
    _cards  = "".join(
        f"""<div class='lbl'>
        <div class='ln'>{_name}</div>
        <div class='lm'>{_mobile}  ·  {_id}</div>
        <table><tr><th></th><th>SPH</th><th>CYL</th><th>AX</th><th>ADD</th></tr>
        <tr><td class='le'>R</td><td>{_fp(rx_r.get("sph"))}</td><td>{_fp(rx_r.get("cyl"))}</td><td>{rx_r.get("axis","—")}</td><td>{_fp(rx_r.get("add"))}</td></tr>
        <tr><td class='le'>L</td><td>{_fp(rx_l.get("sph"))}</td><td>{_fp(rx_l.get("cyl"))}</td><td>{rx_l.get("axis","—")}</td><td>{_fp(rx_l.get("add"))}</td></tr>
        </table></div><div style='page-break-after:always'></div>"""
        for _ in range(copies)
    )
    _html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page{{size:75mm 55mm;margin:0}}body{{margin:0;font-family:Arial}}
    .lbl{{width:75mm;height:55mm;box-sizing:border-box;padding:3mm 4mm;background:#fff;border:.5mm solid #333}}
    .ln{{font-size:11pt;font-weight:700;color:#0f172a;margin-bottom:1mm}}
    .lm{{font-size:7pt;color:#475569;margin-bottom:2mm;font-family:monospace}}
    table{{border-collapse:collapse;width:100%;font-size:8pt}}
    th{{background:#0f172a;color:#fff;padding:1.5mm 2mm;text-align:center;font-size:7pt}}
    td{{padding:1.5mm 2mm;text-align:center;border-bottom:.3mm solid #e2e8f0;color:#0f172a}}
    td.le{{color:#64748b;font-weight:700;text-align:left}}
    .no-print{{display:none}}@media print{{.no-print{{display:none!important}}}}
    </style></head><body>{_cards}
    <div class='no-print' style='text-align:center;padding:20px'>
    <button onclick='window.print()' style='background:#0f172a;color:#fff;border:none;
    padding:10px 32px;border-radius:8px;font-weight:700;cursor:pointer'>🖨️ Print Labels</button>
    </div></body></html>"""
    _b64_html = _b64.b64encode(_html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )
    st.success(f"✅ Label print dialog opened — {copies} copy/copies")