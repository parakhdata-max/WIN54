"""
modules/backoffice/inhouse_pipeline.py
============================================
In-house Pipeline (🔬) — job cards, surfacing, blank allocation, stage tracking.

Extracted from production_page.py.
Entry points called from production_page.py:
  render_inhouse_pipeline
  render_assignment_workspace
"""
from __future__ import annotations
import logging
from modules.backoffice.production_shared import (
    _PIPELINE_THEME,
    _q,
    _fetch_order_for_panel,
    _load_pipeline_overview,
    _render_pipeline_cards,
    _production_card_key_suffix,
    _go_to_billing,
    _power_str
)

import streamlit as st

log = logging.getLogger(__name__)

def _scan_norm(value: str) -> str:
    """Normalize scanned order barcodes like OR26270010 to stored R/2627/0010 shape."""
    s = "".join(ch for ch in str(value or "") if ch.isalnum()).lower()
    if s.startswith("o") and len(s) > 1:
        s = s[1:]
    return s

def _scan_match(needle: str, *hay_values) -> bool:
    raw = str(needle or "").strip().lower()
    norm = _scan_norm(needle)
    if not raw and not norm:
        return True
    for val in hay_values:
        text = str(val or "").lower()
        if raw and raw in text:
            return True
        if norm and norm in _scan_norm(text):
            return True
    return False

def _bootstrap_service_production_jobs() -> None:
    """
    Create production-visible jobs for service-only work before staff tests:
    - FITTING charges/service lines create fitting_jobs.
    - COLOURING service lines create lightweight job_master rows.
    - COLOURING charges without a service order_line create fitting_jobs(type=COLOURING).
    Idempotent: existing active rows are skipped.

    Performance: full-table scan of orders/order_lines/order_charges runs only
    once per Streamlit session and is restricted to the last 90 days. New
    orders punched within the session won't trigger re-bootstrap until the
    page is reloaded — that's acceptable because punching itself can create
    the rows directly in future, and the manual refresh button below the
    pipeline view forces a re-run.
    """
    try:
        import streamlit as _st_boot
        if _st_boot.session_state.get("_svc_bootstrap_done", False):
            return
        # Allow a manual force-rerun if the user clicks Refresh
        if _st_boot.session_state.pop("_svc_bootstrap_force", False):
            pass  # fall through and re-scan
        else:
            _st_boot.session_state["_svc_bootstrap_done"] = True
    except Exception as _e:
        pass

    try:
        from modules.sql_adapter import run_query as _rq_svc, run_write as _rw_svc
    except Exception as _e:
        return

    try:
        _rows = _rq_svc("""
            WITH svc AS (
                SELECT
                    o.id::text AS order_id,
                    o.order_no,
                    COALESCE(o.patient_name, o.party_name, '') AS customer_name,
                    ol.id::text AS line_id,
                    TRUE AS has_line,
                    UPPER(COALESCE(ol.lens_params->>'charge_type',
                                   ol.lens_params->>'service_type',
                                   p.product_name,
                                   p.category,
                                   p.main_group,
                                   '')) AS service_text,
                    COALESCE(p.product_name,
                             ol.lens_params->>'service_display_name',
                             ol.lens_params->>'display_product_name',
                             ol.lens_params->>'service_description',
                             ol.lens_params->>'description',
                             '') AS description,
                    COALESCE(ol.total_price, ol.unit_price, 0) AS amount
                FROM orders o
                JOIN order_lines ol ON ol.order_id = o.id
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE o.status NOT IN ('CANCELLED','CLOSED')
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND o.created_at >= NOW() - INTERVAL '90 days'
                  AND (
                    COALESCE(ol.is_service_line, FALSE) = TRUE
                    OR UPPER(COALESCE(ol.eye_side,'')) IN ('S','SERVICE')
                  )
                UNION ALL
                SELECT
                    o.id::text AS order_id,
                    o.order_no,
                    COALESCE(o.patient_name, o.party_name, '') AS customer_name,
                    NULL::text AS line_id,
                    FALSE AS has_line,
                    UPPER(COALESCE(oc.charge_type,'') || ' ' || COALESCE(oc.description,'')) AS service_text,
                    COALESCE(oc.description, oc.charge_type, '') AS description,
                    COALESCE(oc.total_amount, oc.amount, 0) AS amount
                FROM orders o
                JOIN order_charges oc ON oc.order_id = o.id
                WHERE o.status NOT IN ('CANCELLED','CLOSED')
                  AND COALESCE(oc.is_confirmed, TRUE) = TRUE
                  AND o.created_at >= NOW() - INTERVAL '90 days'
            )
            SELECT * FROM svc
            WHERE service_text LIKE '%%FITT%%'
               OR service_text LIKE '%%COLOUR%%'
               OR service_text LIKE '%%COLOR%%'
               OR service_text LIKE '%%TINT%%'
            ORDER BY order_no
        """, {}) or []
    except Exception as _e:
        return

    for r in _rows:
        order_id = str(r.get("order_id") or "")
        line_id = str(r.get("line_id") or "")
        service_text = str(r.get("service_text") or "").upper()
        desc = str(r.get("description") or "")
        amount = float(r.get("amount") or 0)
        is_fitting = "FITT" in service_text
        is_colouring = ("COLOUR" in service_text or "COLOR" in service_text or "TINT" in service_text)

        if is_fitting:
            try:
                # Idempotency: if a service line_id is present, dedupe per line
                # so two fitting lines on one order get two fitting jobs.
                # If no line_id (charge-only), dedupe per order_id.
                if line_id:
                    existing = _rq_svc("""
                        SELECT 1 FROM fitting_jobs
                        WHERE order_line_id = %(lid)s::uuid
                          AND fitting_type IN ('INHOUSE','EXTERNAL')
                          AND status NOT IN ('DONE','DELIVERED','CANCELLED')
                        LIMIT 1
                    """, {"lid": line_id}) or []
                else:
                    existing = _rq_svc("""
                        SELECT 1 FROM fitting_jobs
                        WHERE order_id = %(oid)s::uuid
                          AND order_line_id IS NULL
                          AND fitting_type IN ('INHOUSE','EXTERNAL')
                          AND status NOT IN ('DONE','DELIVERED','CANCELLED')
                        LIMIT 1
                    """, {"oid": order_id}) or []
                if not existing:
                    job_no_rows = _rq_svc("SELECT generate_fitting_job_no() AS no", {}) or []
                    job_no = job_no_rows[0]["no"] if job_no_rows else f"FIT-{str(order_id)[:8]}"
                    _rw_svc("""
                        INSERT INTO fitting_jobs (
                            id, fitting_job_no, order_id, order_line_id,
                            fitting_type, frame_notes, status, fitting_cost,
                            remarks, created_by
                        ) VALUES (
                            gen_random_uuid(), %(no)s, %(oid)s::uuid,
                            NULLIF(%(lid)s,'')::uuid, 'INHOUSE',
                            %(notes)s, 'PENDING', %(cost)s,
                            'Auto-created from fitting service', 'production_auto'
                        )
                    """, {"no": job_no, "oid": order_id, "lid": line_id, "notes": desc, "cost": amount})
                    _rw_svc("""
                        INSERT INTO fitting_stage_events (id, fitting_job_id, stage, remarks, performed_by)
                        SELECT gen_random_uuid(), id, 'PENDING', 'Auto-created from fitting service', 'production_auto'
                        FROM fitting_jobs WHERE fitting_job_no = %(no)s
                    """, {"no": job_no})
            except Exception as _e:
                log.warning("[prod_page] silent err: %s", _e)

        if is_colouring:
            if line_id:
                try:
                    _rw_svc("""
                        UPDATE order_lines
                        SET lens_params = COALESCE(lens_params,'{}'::jsonb)
                            || jsonb_build_object(
                                'manufacturing_route','INHOUSE',
                                'service_production_type','COLOURING',
                                'service_description', %(desc)s
                            )
                        WHERE id=%(lid)s::uuid
                    """, {"lid": line_id, "desc": desc})
                    exists = _rq_svc(
                        "SELECT 1 FROM job_master WHERE order_line_id=%(lid)s::uuid LIMIT 1",
                        {"lid": line_id}
                    ) or []
                    if not exists:
                        _rw_svc("""
                            INSERT INTO job_master (
                                id, order_line_id, total_qty, blank_required_qty,
                                blank_allocated_qty, current_stage, reprocess_count,
                                is_closed, created_at, updated_at
                            ) VALUES (
                                gen_random_uuid(), %(lid)s::uuid, 1, 0, 0,
                                'JOB_CREATED', 0, FALSE, NOW(), NOW()
                            )
                        """, {"lid": line_id})
                except Exception as _e:
                    import logging as _lg; _lg.getLogger(__name__).warning(f"[prod_page] silent err: {_e}")
            else:
                try:
                    existing = _rq_svc("""
                        SELECT 1 FROM fitting_jobs
                        WHERE order_id = %(oid)s::uuid
                          AND fitting_type = 'COLOURING'
                          AND status NOT IN ('DONE','DELIVERED','CANCELLED')
                        LIMIT 1
                    """, {"oid": order_id}) or []
                    if not existing:
                        job_no_rows = _rq_svc("SELECT generate_fitting_job_no() AS no", {}) or []
                        job_no = job_no_rows[0]["no"] if job_no_rows else f"COL-{str(order_id)[:8]}"
                        _rw_svc("""
                            INSERT INTO fitting_jobs (
                                id, fitting_job_no, order_id, fitting_type,
                                frame_notes, status, fitting_cost, remarks, created_by
                            ) VALUES (
                                gen_random_uuid(), %(no)s, %(oid)s::uuid, 'COLOURING',
                                %(notes)s, 'PENDING', %(cost)s,
                                'Auto-created from colouring charge', 'production_auto'
                            )
                        """, {"no": job_no, "oid": order_id, "notes": desc, "cost": amount})
                except Exception as _e:
                    import logging as _lg; _lg.getLogger(__name__).warning(f"[prod_page] silent err: {_e}")


def _ih_print_line_for_panel(line: dict | None) -> dict | None:
    """Normalize an in-house row into the print-line shape used by production prints."""
    if not line:
        return None
    import json as _json

    out = dict(line)
    lp = out.get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = _json.loads(lp) if lp else {}
        except Exception:
            lp = {}
    out["lens_params"] = lp if isinstance(lp, dict) else {}
    out["surfacing_data"] = (
        out.get("surfacing_data")
        or out["lens_params"].get("surfacing_data")
        or {}
    )
    return out


def _ih_panel_job_labels_print_button(odata: dict, lines: list[dict], key_prefix: str) -> None:
    """Outer in-house card: one button for TSC labels + Canon job card."""
    rl_lines = sorted(
        [l for l in lines if str(l.get("eye_side", "")).upper()[:1] in ("R", "L")],
        key=lambda x: 0 if str(x.get("eye_side", "")).upper()[:1] == "R" else 1,
    )
    if not rl_lines:
        st.button("🖨️ PRINT JOB CARD + LABELS", key=f"{key_prefix}_job_labels_none", use_container_width=True, disabled=True)
        return

    if st.button(
        "🖨️ PRINT JOB CARD + LABELS",
        key=f"{key_prefix}_job_labels",
        use_container_width=True,
        help="Single command: TSC R/L labels + Canon job card",
    ):
        try:
            from modules.backoffice.production_panel import (
                _build_label_page,
                _missing_blank_assignments_for_print,
                _open_print_window,
                _open_production_job_card_print,
                _print_tsc_production_labels,
            )

            print_lines = [_ih_print_line_for_panel(l) for l in rl_lines]
            print_lines = [l for l in print_lines if l]
            print_order = {
                "id": odata.get("order_id", ""),
                "order_no": odata.get("order_no", ""),
                "patient_name": odata.get("patient_name", ""),
                "party_name": next((l.get("party_name", "") for l in print_lines if l.get("party_name")), ""),
                "order_type": next((l.get("order_type", "RETAIL") for l in print_lines if l), "RETAIL"),
                "lines": print_lines,
            }
            missing = _missing_blank_assignments_for_print(print_order)
            if missing:
                st.error(
                    "🔴 Assignment not done — assign blank first for "
                    f"{'/'.join(missing)} eye before printing job card and labels."
                )
                return

            ok, msg = _print_tsc_production_labels(print_lines, print_order)
            if ok:
                st.success(msg)
            else:
                st.warning(f"TSC direct print failed: {msg}. Opening label fallback.")
                _open_print_window(_build_label_page(print_lines, print_order))

            r_job = next((l for l in print_lines if str(l.get("eye_side", "")).upper()[:1] == "R"), None)
            l_job = next((l for l in print_lines if str(l.get("eye_side", "")).upper()[:1] == "L"), None)
            _open_production_job_card_print(r_job, l_job, print_order)
        except Exception as exc:
            st.error(f"Job + Labels print error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# SHARED OPTICAL STAGE FLOW (single source of truth)
#
# Replaces the static _COATING_STAGE_SEQUENCES dict that used to live duplicated
# inside two pipeline panels. Both copies of _coating_path_ih / _stage_sequence_ih
# now delegate to these helpers so behaviour cannot diverge.
# ══════════════════════════════════════════════════════════════════════════════

# Shared pre-production stages — appears at the start of every inhouse flow.
# JOB_PRINTED is a legacy alias for PRINTED — kept in STAGE_ALIASES for read-side
# normalisation but NOT in the canonical flow, so _next_stage_ih can never
# suggest "advance from PRINTED to JOB_PRINTED" (which would have been a no-op
# stage that confuses operators).
PRE_PRODUCTION_STAGES = ["JOB_CREATED", "PRINTED", "PRODUCTION_PICKED", "PRODUCTION_DONE"]

# Stage alias normalisation. Some upstream code emits legacy names; we normalise
# them here so stage_index lookups work regardless of which name was written.
STAGE_ALIASES = {
    "JOB_PRINTED":          "PRINTED",
    "PRODUCTION_COMPLETED": "PRODUCTION_DONE",
    "HARDCOAT_COMPLETED":   "HARDCOAT_DONE",
    "COLOURING_COMPLETED":  "COLOURING_DONE",
    "SENT_TO_ARC":          "ARC_SENT",
}


def normalize_stage_alias(stage: str) -> str:
    """Translate legacy stage codes to canonical names."""
    if not stage:
        return stage
    s = str(stage).upper().strip()
    return STAGE_ALIASES.get(s, s)


def detect_coating_path(coating: str, has_colouring: bool) -> str:
    """Determine coating path from coating string + colouring service flag.

    Returns one of:
      UNCOATED, COLOURING, HARDCOAT, COLOURING_HC, HARDCOAT_ARC, COLOURING_HC_ARC

    Suffix rules (checked against compact alphanumeric form):
      UC / UNCOAT / UNCOATED            → UNCOATED
      HC / HARDCOAT / HARDCOTE          → HARDCOAT
      MC / GMC / HMC / UltraGMC / ARC  → HARDCOAT_ARC  (MC always implies HC+ARC)
    """
    coat = str(coating or "").upper()
    coat_compact = "".join(ch for ch in coat if ch.isalnum())
    has_arc = (
        # Suffix-based: any word ending in MC/GMC/HMC triggers ARC path
        coat_compact.endswith("MC")
        or coat_compact.endswith("GMC")
        or coat_compact.endswith("HMC")
        # Substring-based: explicit ARC/GMC/HMC anywhere in the text
        or "ULTRAGMC" in coat_compact
        or "GMC" in coat_compact
        or "HMC" in coat_compact
        or "ARC" in coat
        or "MULTICOAT" in coat
        or "MULTICOATING" in coat
        or "MULTI COAT" in coat
        or "ANTI REF" in coat
        or "BLUE BLOCK" in coat
        or " BB" in f" {coat} "
    )
    has_hardcoat = (
        has_arc  # MC always implies HC
        or "HARDCOAT" in coat
        or "HARDCOTE" in coat
        or "HARD COAT" in coat
        or "HARD COTE" in coat
        or " HC" in f" {coat} "
        or coat_compact.endswith("HC")
        or "ULTRAHC" in coat_compact
    )
    is_uncoat = (
        coat_compact.endswith("UC")
        or "UNCOAT" in coat
        or "UNCOATED" in coat
    ) and not has_hardcoat and not has_arc

    # ARC always implies hardcoat. Priority order honours the spec:
    # ARC + colouring → all three stages; colouring goes BEFORE hardcoat.
    if has_arc and has_colouring:
        return "COLOURING_HC_ARC"
    if has_arc:
        return "HARDCOAT_ARC"
    if has_hardcoat and has_colouring:
        return "COLOURING_HC"
    if has_hardcoat:
        return "HARDCOAT"
    if has_colouring:
        return "COLOURING"
    return "UNCOATED"


def build_optical_stage_flow(coating: str, has_colouring: bool, has_fitting: bool) -> list:
    """Build dynamic optical-lens stage flow per spec.

    Order:
      1. PRE_PRODUCTION_STAGES (job created/printed/picked/done)
      2. INSPECTION
      3. COLOURING_PICKED / DONE   (if colouring)
      4. HARDCOAT_PICKED / DONE    (if hardcoat or ARC)
      5. ARC_SENT / RECEIVED / FINAL_QC  (if ARC) — replaces the post-HC INSPECTION
      6. INSPECTION (post-coating, only if no ARC)
      7. FITTING_PENDING / SENT / RECEIVED / DONE  (if fitting service)
      8. READY_FOR_PACK
      9. READY_TO_BILL

    Consecutive duplicate stages are removed at the end.
    """
    flow = list(PRE_PRODUCTION_STAGES)
    flow.append("INSPECTION")

    if has_colouring:
        flow += ["COLOURING_PICKED", "COLOURING_DONE"]

    coat = str(coating or "").upper()
    coat_compact = "".join(ch for ch in coat if ch.isalnum())
    has_arc = (
        coat_compact.endswith("MC")
        or coat_compact.endswith("GMC")
        or coat_compact.endswith("HMC")
        or "ULTRAGMC" in coat_compact
        or "GMC" in coat_compact
        or "HMC" in coat_compact
        or "ARC" in coat
        or "MULTICOAT" in coat
        or "MULTICOATING" in coat
        or "MULTI COAT" in coat
        or "ANTI REF" in coat
        or "BLUE BLOCK" in coat
        or " BB" in f" {coat} "
    )
    has_hardcoat = (
        has_arc
        or "HARDCOAT" in coat
        or "HARDCOTE" in coat
        or "HARD COAT" in coat
        or "HARD COTE" in coat
        or " HC" in f" {coat} "
        or coat_compact.endswith("HC")
        or "ULTRAHC" in coat_compact
    )

    if has_hardcoat:
        flow += ["HARDCOAT_PICKED", "HARDCOAT_DONE"]

    if has_arc:
        flow += ["ARC_SENT", "ARC_RECEIVED", "FINAL_QC"]
    else:
        # Post-coating inspection only when no ARC. ARC has its own FINAL_QC.
        flow.append("INSPECTION")

    if has_fitting:
        flow += ["FITTING_PENDING", "FITTING_SENT", "FITTING_RECEIVED", "FITTING_DONE"]

    flow += ["READY_FOR_PACK", "READY_TO_BILL"]

    # Remove consecutive duplicate stages (e.g. INSPECTION twice in a row)
    deduped = []
    prev = None
    for s in flow:
        if s != prev:
            deduped.append(s)
        prev = s
    return deduped


def build_service_only_stage_flow(service_type: str) -> list:
    """For service-only orders (no lens product).
    service_type may be a single type or a '+'-joined combo e.g. 'COLOURING+FITTING'.
    """
    st_up = str(service_type or "").upper()
    has_colour  = "COLOUR" in st_up or "COLOR" in st_up or "TINT" in st_up
    has_fitting = "FITT" in st_up

    if has_colour and has_fitting:
        # Combined service job: colour first, then fitting, then billing.
        return [
            "JOB_CREATED",
            "COLOURING_PICKED", "COLOURING_DONE",
            "FITTING_PENDING", "FITTING_SENT", "FITTING_RECEIVED", "FITTING_DONE",
            "READY_TO_BILL",
        ]
    if has_colour:
        return [
            "JOB_CREATED",
            "COLOURING_PICKED", "COLOURING_DONE",
            "READY_TO_BILL",
        ]
    if has_fitting:
        return [
            "JOB_CREATED",
            "FITTING_PENDING", "FITTING_SENT", "FITTING_RECEIVED", "FITTING_DONE",
            "READY_TO_BILL",
        ]
    # Unknown service type — fall back to plain pack/bill
    return ["JOB_CREATED", "READY_FOR_PACK", "READY_TO_BILL"]


def detect_production_services(order_id: str) -> dict:
    """Return service flags + frame info for an order.

    Reads order_lines + order_charges. Returns:
      {
        "colouring": bool,
        "fitting":   bool,
        "frame_name":   str,
        "frame_source": "SOLD_WITH_ORDER" / "CUSTOMER_FRAME" / "NO_FRAME",
        "fitting_vendor": str,
        "fitting_note":   str,
      }
    """
    out = {
        "colouring": False,
        "fitting":   False,
        "frame_name":     "",
        "frame_source":   "NO_FRAME",
        "fitting_vendor": "",
        "fitting_note":   "",
    }
    if not order_id:
        return out
    try:
        from modules.sql_adapter import run_query as _rq_dps
    except Exception as _e:
        return out

    # 1) Service flags from order_lines
    try:
        rows = _rq_dps("""
            SELECT
                UPPER(COALESCE(p.product_name,'') || ' ' ||
                      COALESCE(p.category,'')      || ' ' ||
                      COALESCE(p.main_group,'')    || ' ' ||
                      COALESCE(ol.lens_params->>'service_type','') || ' ' ||
                      COALESCE(ol.lens_params->>'charge_type','')) AS txt,
                COALESCE(p.product_name,'') AS product_name,
                COALESCE(p.main_group,'')   AS main_group,
                COALESCE(ol.lens_params->>'fitting_details','{}') AS fit_details
            FROM order_lines ol
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE ol.order_id = %(oid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """, {"oid": order_id}) or []
    except Exception as _e:
        rows = []

    import json as _json_dps
    for r in rows:
        txt = str(r.get("txt") or "")
        if any(k in txt for k in ("COLOUR", "COLOR", "TINT")):
            out["colouring"] = True
        if "FITT" in txt:
            out["fitting"] = True
        # Frame line detected via main_group
        mg = str(r.get("main_group") or "").lower()
        if not out["frame_name"] and ("frame" in mg or "sunglass" in mg):
            out["frame_name"]   = str(r.get("product_name") or "")
            out["frame_source"] = "SOLD_WITH_ORDER"
        # Pick up customer-frame note + fitting vendor from any line
        try:
            fd = _json_dps.loads(r.get("fit_details") or "{}") or {}
        except Exception as _e:
            fd = {}
        if not out["fitting_vendor"] and fd.get("fitting_vendor"):
            out["fitting_vendor"] = str(fd.get("fitting_vendor") or "")
        if not out["fitting_note"] and fd.get("instructions"):
            out["fitting_note"] = str(fd.get("instructions") or "")
        if out["frame_source"] == "NO_FRAME" and fd.get("frame_source") == "CUSTOMER_FRAME":
            out["frame_source"] = "CUSTOMER_FRAME"
            if not out["frame_name"]:
                out["frame_name"] = str(fd.get("frame_name") or "")

    # 2) Service flags from order_charges (when no service line was punched)
    try:
        crows = _rq_dps("""
            SELECT UPPER(COALESCE(charge_type,'') || ' ' || COALESCE(description,'')) AS txt
            FROM order_charges
            WHERE order_id = %(oid)s::uuid
              AND COALESCE(is_confirmed, TRUE) = TRUE
        """, {"oid": order_id}) or []
    except Exception as _e:
        crows = []
    for r in crows:
        txt = str(r.get("txt") or "")
        if any(k in txt for k in ("COLOUR", "COLOR", "TINT")):
            out["colouring"] = True
        if "FITT" in txt:
            out["fitting"] = True

    return out


def _fitting_work_context_ih(order_id: str) -> dict:
    """Return editable lens/frame context for a fitting service line."""
    out = {"lens_summary": "", "frame_summary": ""}
    if not order_id:
        return out
    try:
        from modules.sql_adapter import run_query as _rq_fit_ctx
        rows = _rq_fit_ctx("""
            SELECT
                ol.id::text AS line_id,
                COALESCE(ol.eye_side, '') AS eye_side,
                COALESCE(p.product_name, ol.lens_params->>'display_product_name', '') AS product_name,
                COALESCE(p.brand, '') AS brand,
                COALESCE(p.main_group, '') AS main_group,
                COALESCE(p.category, '') AS category,
                COALESCE(p.unit, 'PCS') AS unit,
                COALESCE(p.product_code, p.sku_code, p.barcode, '') AS product_code,
                ol.sph, ol.cyl, ol.axis, ol.add_power,
                ol.quantity,
                ol.lens_params
            FROM order_lines ol
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE ol.order_id = %(oid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
            ORDER BY
                CASE UPPER(COALESCE(ol.eye_side,'')) WHEN 'R' THEN 1 WHEN 'RE' THEN 1
                    WHEN 'L' THEN 2 WHEN 'LE' THEN 2 ELSE 3 END,
                ol.id
        """, {"oid": str(order_id)}) or []
    except Exception as _ctx_e:
        log.debug("Fitting context query failed: %s", _ctx_e)
        rows = []

    import json as _fit_ctx_json

    def _lp_ctx(v):
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and v.strip():
            try:
                return _fit_ctx_json.loads(v) or {}
            except Exception:
                return {}
        return {}

    def _fmt_num_ctx(v, signed: bool = True):
        try:
            if v in (None, ""):
                return ""
            f = float(v)
            return f"{f:+.2f}" if signed else f"{f:g}"
        except Exception:
            return ""

    lens_bits = []
    frame_bits = []
    for r in rows:
        lp = _lp_ctx(r.get("lens_params"))
        eye = str(r.get("eye_side") or "").upper()
        mg = str(r.get("main_group") or "").upper()
        cat = str(r.get("category") or "").upper()
        pname = str(r.get("product_name") or "").strip()
        is_service = bool(lp.get("service_group") or lp.get("charge_type") or lp.get("service_type"))
        is_frame = any(k in f"{mg} {cat} {pname}".upper() for k in ("FRAME", "SUNGLASS"))
        is_lens = eye in ("R", "L", "RE", "LE", "RIGHT", "LEFT") and not is_service
        if is_lens:
            pwr_parts = []
            sph = _fmt_num_ctx(r.get("sph"))
            cyl = _fmt_num_ctx(r.get("cyl"))
            ax = _fmt_num_ctx(r.get("axis"), signed=False)
            add = _fmt_num_ctx(r.get("add_power"))
            if sph:
                pwr_parts.append(f"SPH {sph}")
            if cyl:
                pwr_parts.append(f"CYL {cyl}")
            if ax:
                pwr_parts.append(f"AX {ax}")
            if add:
                pwr_parts.append(f"ADD {add}")
            specs = [
                str(lp.get("lens_index") or lp.get("index") or lp.get("Lens Index") or "").strip(),
                str(lp.get("coating") or lp.get("LensCoating") or "").strip(),
                str(lp.get("frame_type") or lp.get("Frame Type") or "").strip(),
            ]
            specs = [x for x in specs if x]
            lens_bits.append(
                f"{eye[:1]}: {pname}"
                + (f" ({' '.join(pwr_parts)})" if pwr_parts else "")
                + (f" · {' · '.join(specs)}" if specs else "")
            )
        if is_frame:
            code = str(r.get("product_code") or "").strip()
            qty = r.get("quantity") or 1
            frame_bits.append(
                f"{pname}"
                + (f" · {r.get('brand')}" if r.get("brand") else "")
                + (f" · Code {code}" if code else "")
                + f" · Qty {qty}"
            )
        fd = lp.get("fitting_details") if isinstance(lp.get("fitting_details"), dict) else {}
        if fd:
            fd_bits = []
            if fd.get("frame_source"):
                fd_bits.append(f"Source: {fd.get('frame_source')}")
            if fd.get("frame_name"):
                fd_bits.append(f"Frame: {fd.get('frame_name')}")
            if fd.get("instructions"):
                fd_bits.append(f"Instructions: {fd.get('instructions')}")
            if fd_bits:
                frame_bits.append(" · ".join(fd_bits))

    out["lens_summary"] = "\n".join(dict.fromkeys(lens_bits))
    out["frame_summary"] = "\n".join(dict.fromkeys(frame_bits))
    return out


# ══════════════════════════════════════════════════════════════════════════════
# FRAME / FITTING DETAILS PANEL  (per spec item 5)
#
# Shown inside the production job card section. Lets staff record:
#   - Frame source (sold-with-order / customer-frame / no-frame)
#   - Frame name / description (auto-filled from frame line if SOLD_WITH_ORDER)
#   - Fitting vendor / lab
#   - Expected return date
#   - Fitting instructions
#
# Saves into lens_params["fitting_details"] on the inhouse lens line. Frame
# product lines themselves are NEVER turned into job cards — frames stay in
# stock_lines per the order_loader frame guard. The fitting_details JSON is
# the bridge between the frame line and the lens job.
# ══════════════════════════════════════════════════════════════════════════════

def render_frame_fitting_details_section(line: dict, order: dict) -> None:
    """Render the Frame/Fitting Details editor inline in a production card.

    `line` must be the in-house lens line dict (has line_id and lens_params).
    `order` is the parent order dict.
    """
    try:
        import streamlit as _st_ff
        import json as _json_ff
    except Exception as _e:
        return

    line_id = str(line.get("line_id") or line.get("id") or "")
    if not line_id:
        return

    # Detect what the order looks like — auto-fills frame source/name
    services = detect_production_services(str(order.get("order_id") or order.get("id") or ""))
    auto_frame_name   = services.get("frame_name", "")
    auto_frame_source = services.get("frame_source", "NO_FRAME")

    # Pull existing fitting_details from lens_params
    lp = line.get("lens_params") or {}
    if isinstance(lp, str):
        try: lp = _json_ff.loads(lp)
        except Exception: lp = {}
    fd_existing = (lp.get("fitting_details") or {}) if isinstance(lp, dict) else {}

    cur_source = fd_existing.get("frame_source") or auto_frame_source or "NO_FRAME"
    cur_name   = fd_existing.get("frame_name")   or auto_frame_name   or ""
    cur_vendor = fd_existing.get("fitting_vendor", "")
    cur_ret    = fd_existing.get("expected_return_date", "")
    cur_inst   = fd_existing.get("instructions", "")

    with _st_ff.expander("🖼 Frame / Fitting Details", expanded=False):
        SOURCES = ["SOLD_WITH_ORDER", "CUSTOMER_FRAME", "NO_FRAME"]
        SOURCE_LABELS = {
            "SOLD_WITH_ORDER": "Sold with order",
            "CUSTOMER_FRAME":  "Customer's own frame",
            "NO_FRAME":        "No frame (lens only)",
        }
        try:
            src_idx = SOURCES.index(cur_source)
        except ValueError:
            src_idx = 2

        col1, col2 = _st_ff.columns(2)
        with col1:
            new_source = _st_ff.selectbox(
                "Frame Source",
                SOURCES,
                format_func=lambda s: SOURCE_LABELS.get(s, s),
                index=src_idx,
                key=f"ff_src_{line_id}",
            )
        with col2:
            new_vendor = _st_ff.text_input(
                "Fitting Vendor / Lab",
                value=cur_vendor,
                key=f"ff_ven_{line_id}",
                help="External fitter or in-house bench",
            )

        # Frame name: auto-fill from frame product if SOLD_WITH_ORDER and field empty
        _placeholder = ""
        if new_source == "SOLD_WITH_ORDER" and not cur_name and auto_frame_name:
            cur_name = auto_frame_name
            _placeholder = f"auto-filled from order: {auto_frame_name}"
        new_name = _st_ff.text_input(
            "Frame Name / Description",
            value=cur_name,
            key=f"ff_name_{line_id}",
            placeholder=_placeholder,
        )

        col3, col4 = _st_ff.columns(2)
        with col3:
            # Date input — accept blank
            try:
                import datetime as _dt_ff
                _ret_default = None
                if cur_ret:
                    try:
                        _ret_default = _dt_ff.date.fromisoformat(str(cur_ret))
                    except Exception as _e:
                        _ret_default = None
                new_ret = _st_ff.date_input(
                    "Expected Return Date",
                    value=_ret_default,
                    key=f"ff_ret_{line_id}",
                )
            except Exception as _e:
                new_ret = None
        with col4:
            _st_ff.write("")
            _st_ff.caption("Optional — leave blank if same-day fitting")

        new_inst = _st_ff.text_area(
            "Fitting Instructions",
            value=cur_inst,
            height=80,
            key=f"ff_inst_{line_id}",
            placeholder="e.g. PD 64, BVD 12, slight pantoscopic tilt…",
        )

        cs1, cs2 = _st_ff.columns([1, 1])
        with cs1:
            if _st_ff.button("💾 Save Fitting Details",
                            key=f"ff_save_{line_id}",
                            type="primary",
                            use_container_width=True):
                _save_fitting_details(line_id, line, {
                    "frame_source":         new_source,
                    "frame_name":           str(new_name or "").strip(),
                    "fitting_vendor":       str(new_vendor or "").strip(),
                    "expected_return_date": str(new_ret) if new_ret else "",
                    "instructions":         str(new_inst or "").strip(),
                })
                _st_ff.success("✅ Fitting details saved")
                _st_ff.rerun()
        with cs2:
            if _st_ff.button("🖨 Fitting Slip (A5)",
                            key=f"ff_slip_{line_id}",
                            use_container_width=True):
                _show_fitting_slip(order, line, {
                    "frame_source":         new_source,
                    "frame_name":           str(new_name or "").strip(),
                    "fitting_vendor":       str(new_vendor or "").strip(),
                    "expected_return_date": str(new_ret) if new_ret else "",
                    "instructions":         str(new_inst or "").strip(),
                })


def _save_fitting_details(line_id: str, line: dict, fd: dict) -> None:
    """Merge fitting_details into lens_params and persist."""
    try:
        import json as _json_sf
        from modules.sql_adapter import run_query as _rq_sf, run_write as _rw_sf
        # Fetch current lens_params so unrelated keys aren't lost
        rows = _rq_sf(
            "SELECT COALESCE(lens_params,'{}')::text AS lp "
            "FROM order_lines WHERE id=%(lid)s::uuid LIMIT 1",
            {"lid": line_id}
        ) or []
        if rows:
            try: lp = _json_sf.loads(rows[0].get("lp") or "{}") or {}
            except Exception: lp = {}
        else:
            lp = {}
        lp["fitting_details"] = fd
        _rw_sf(
            "UPDATE order_lines SET lens_params = %(lp)s::jsonb "
            "WHERE id=%(lid)s::uuid",
            {"lp": _json_sf.dumps(lp), "lid": line_id}
        )
        # In-memory mutation so next render reflects the save
        line["lens_params"] = lp
        # Bust loader caches
        try:
            from modules.backoffice.order_loader import (
                load_single_order, load_orders_from_database
            )
            for _f in (load_single_order, load_orders_from_database):
                try: _f.clear()
                except Exception: pass
        except Exception as _e:
            pass
    except Exception as _e:
        try:
            import streamlit as _st_e
            _st_e.warning(f"Could not save fitting details: {_e}")
        except Exception as _e:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# FITTING SLIP PRINT — A5 + thermal (per spec item 6)
# ══════════════════════════════════════════════════════════════════════════════

def _show_fitting_slip(order: dict, line: dict, fd: dict) -> None:
    """Render an HTML fitting slip viewable + browser-printable.

    A5 layout for full dispatch note; staff can choose narrow thermal-style
    via the toggle inside the modal. Both layouts share the same data.
    """
    try:
        import streamlit as _st_fs
    except Exception as _e:
        return

    order_no   = str(order.get("order_no") or order.get("id") or "")
    patient    = str(order.get("patient_name") or order.get("party_name") or "")
    mobile     = str(order.get("patient_mobile") or order.get("party_mobile") or "")
    order_date = str(order.get("created_at") or "")[:10]
    eye        = str(line.get("eye_side") or "").upper()
    sph        = line.get("sph")
    cyl        = line.get("cyl")
    axis       = line.get("axis")
    add_pwr    = line.get("add_power")
    product    = str(line.get("product_name") or "")

    lp = line.get("lens_params") or {}
    if isinstance(lp, str):
        try:
            import json as _json_lp
            lp = _json_lp.loads(lp)
        except Exception as _e:
            lp = {}
    coating  = str(lp.get("coating") or line.get("coating") or "")
    diameter = str(lp.get("diameter") or "")
    fit_ht   = str(lp.get("fitting_height") or "")
    frame_t  = str(lp.get("frame_type") or "")

    def _fmt(v, w=2, sign=True):
        try:
            n = float(v)
            return f"{n:+.{w}f}" if sign else f"{n:.{w}f}"
        except Exception as _e:
            return "—"

    # Common header
    common_rows = [
        ("Order No",   order_no),
        ("Date",       order_date),
        ("Patient",    patient),
        ("Mobile",     mobile),
        ("Eye",        {"R": "Right", "L": "Left"}.get(eye, eye or "—")),
        ("SPH",        _fmt(sph)),
        ("CYL",        _fmt(cyl)),
        ("AXIS",       f"{axis}°" if axis not in (None, "") else "—"),
        ("ADD",        _fmt(add_pwr) if add_pwr else "—"),
        ("Product",    product or "—"),
        ("Coating",    coating or "—"),
        ("Diameter",   f"{diameter} mm" if diameter else "—"),
        ("Frame Type", frame_t or "—"),
        ("Fit Height", f"{fit_ht} mm" if fit_ht else "—"),
        ("Frame Source", {
            "SOLD_WITH_ORDER": "Sold with order",
            "CUSTOMER_FRAME":  "Customer's own frame",
            "NO_FRAME":        "No frame",
        }.get(fd.get("frame_source", ""), fd.get("frame_source", "—"))),
        ("Frame",      fd.get("frame_name") or "—"),
        ("Vendor",     fd.get("fitting_vendor") or "—"),
        ("Return By",  fd.get("expected_return_date") or "—"),
    ]

    # A5 layout
    a5_rows = "".join(
        f"<tr><td style='padding:4px 10px;background:#f1f5f9;font-weight:600;width:35%'>{k}</td>"
        f"<td style='padding:4px 10px'>{v}</td></tr>"
        for k, v in common_rows
    )
    instr_html = ""
    if fd.get("instructions"):
        instr_html = (
            "<div style='margin-top:14px;padding:10px;background:#fefce8;"
            "border-left:4px solid #eab308;font-size:13px'>"
            f"<b>Instructions</b><br/>{fd['instructions']}</div>"
        )
    a5_html = (
        "<div style='font-family:Helvetica,Arial,sans-serif;max-width:560px;"
        "margin:0 auto;padding:20px;border:1px solid #cbd5e1;background:#fff;color:#0f172a'>"
        "<div style='text-align:center;border-bottom:2px solid #0f172a;padding-bottom:10px;"
        "margin-bottom:14px'>"
        "<div style='font-size:18px;font-weight:700'>FITTING DISPATCH SLIP</div>"
        "<div style='font-size:11px;color:#475569;margin-top:2px'>Parakh Optical</div>"
        "</div>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px'>{a5_rows}</table>"
        f"{instr_html}"
        "<div style='margin-top:24px;display:flex;justify-content:space-between;"
        "font-size:11px;color:#64748b'>"
        "<div>Issued by: ____________</div>"
        "<div>Received by: ____________</div></div>"
        "</div>"
    )

    # Thermal layout (~58mm)
    thermal_rows = "".join(
        f"<div style='display:flex;font-size:10px'>"
        f"<div style='font-weight:700;width:42%'>{k}</div>"
        f"<div style='flex:1'>{v}</div></div>"
        for k, v in common_rows
    )
    thermal_html = (
        "<div style='font-family:monospace;width:240px;margin:0 auto;padding:8px;"
        "border:1px dashed #cbd5e1;background:#fff;color:#0f172a'>"
        "<div style='text-align:center;border-bottom:1px dashed #94a3b8;padding-bottom:4px;"
        "margin-bottom:6px'>"
        "<div style='font-size:11px;font-weight:700'>FITTING SLIP</div>"
        "<div style='font-size:9px'>Parakh Optical</div></div>"
        f"{thermal_rows}"
        + (f"<div style='border-top:1px dashed #94a3b8;margin-top:6px;padding-top:4px;"
           f"font-size:9px'><b>NOTE</b><br/>{fd['instructions']}</div>"
           if fd.get("instructions") else "")
        + "</div>"
    )

    layout = _st_fs.radio(
        "Layout",
        ["A5 Page", "Thermal (58mm)"],
        horizontal=True,
        key=f"slip_layout_{order_no}_{eye}",
    )
    _st_fs.markdown("---")
    _st_fs.markdown(a5_html if layout == "A5 Page" else thermal_html, unsafe_allow_html=True)
    _st_fs.markdown("---")
    _st_fs.caption("Use your browser's Print (Ctrl/Cmd-P) to print this slip.")


# ══════════════════════════════════════════════════════════════════════════════
# SHARED PIPELINE CARD RENDERER
# Used by all 4 pipeline tabs for symmetric, colour-coded compact view
# ══════════════════════════════════════════════════════════════════════════════

_PIPELINE_THEME = {
    "VENDOR":       {"accent": "#f59e0b", "bg": "#1a1200", "icon": "🏭"},
    "EXTERNAL_LAB": {"accent": "#a855f7", "bg": "#130b1e", "icon": "🧪"},
    "INHOUSE":      {"accent": "#3b82f6", "bg": "#0a1628", "icon": "🔬"},
    "STOCK":        {"accent": "#22c55e", "bg": "#041a0e", "icon": "📦"},
}

def _render_inhouse_pipeline():
    """
    In-house lab pipeline — same UI as supplier pipeline.
    Identifies orders by presence of job cards in job_master.
    Stages driven by job_master.current_stage.
    Shows BILLED orders too so production staff can see completed jobs.
    """
    import json as _ji
    import datetime as _dt_ih

    # ── Bootstrap service production jobs (fitting + colouring) ──────────────
    # Reset the session flag daily so orders created today are always picked up.
    # Also reset on manual Refresh button press.
    _today_key = f"_svc_bootstrap_date_{_dt_ih.date.today().isoformat()}"
    if not st.session_state.get(_today_key):
        # New day — clear yesterday's flag so bootstrap re-runs
        st.session_state.pop("_svc_bootstrap_done", None)
        st.session_state[_today_key] = True
    _bootstrap_service_production_jobs()

    st.markdown("### 🔬 In-house Lab Pipeline")
    st.caption("Track lenses through internal production stages.")

    # Manual refresh forces bootstrap re-scan for new fitting/colouring orders
    _rfc1, _rfc2 = st.columns([6, 1])
    with _rfc2:
        if st.button("🔄 Refresh", key="ih_refresh_btn",
                     help="Re-scan for new fitting/colouring jobs"):
            st.session_state["_svc_bootstrap_force"] = True
            st.session_state.pop("_svc_bootstrap_done", None)
            _bootstrap_service_production_jobs()
            st.rerun()

    # Filter control — hide INVOICED/BILLED orders by default
    _show_closed = st.checkbox(
        "Show invoiced/billed orders", value=False,
        key="ih_show_closed", help="Invoiced and Billed orders are hidden by default"
    )

    STAGES = [
        ("JOB_CREATED",      "📋 Job Created"),
        ("PRINTED",          "🖨️ Job Printed"),         # canonical name
        ("JOB_PRINTED",       "🖨️ Job Printed"),         # legacy alias — normalized on load
        ("PRODUCTION_PICKED","⚙️ Production Picked"),
        ("PRODUCTION_DONE",  "✅ Production Done"),      # was PRODUCTION_COMPLETED
        ("INSPECTION",       "🔍 Inspection"),
        ("HARDCOAT_PICKED",  "🧪 Hardcoat Picked"),
        ("HARDCOAT_DONE",    "🧪 Hardcoat Done"),        # was HARDCOAT_COMPLETED
        ("COLOURING_PICKED", "🎨 Colouring Picked"),
        ("COLOURING_DONE",   "🎨 Colouring Done"),       # was COLOURING_COMPLETED
        ("ARC_SENT",         "📤 Sent to ARC"),          # was SENT_TO_ARC
        ("ARC_RECEIVED",     "📥 ARC Received"),
        ("FINAL_QC",         "🔬 Final QC"),
        ("READY_FOR_PACK",   "📦 Ready for Pack"),
        ("READY_TO_BILL",    "💰 Ready to Bill"),
        ("REJECTED",         "🚫 Rejected"),
    ]
    STAGE_IDX   = {s[0]: i for i, s in enumerate(STAGES)}
    STAGE_LABEL = {s[0]: s[1] for s in STAGES}

    def _stg_clr(stage):
        return {
            "JOB_CREATED":       "#64748b",
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
            "READY_FOR_PACK":    "#0d9488",
            "READY_TO_BILL":     "#16a34a",
            "DISPATCHED":        "#059669",
            "DELIVERED":         "#22c55e",
            "CHALLANED":         "#0284c7",
            "INVOICED":          "#22c55e",
            "REJECTED":          "#dc2626",
        }.get(stage, "#475569")

    # JOB_PRINTED dropped — alias-normalised on read via STAGE_ALIASES so any
    # job_master row stored with current_stage='JOB_PRINTED' is treated as PRINTED.
    _PRE_PRODUCTION_STAGES = ["JOB_CREATED", "PRINTED", "PRODUCTION_PICKED", "PRODUCTION_DONE"]
    _COATING_STAGE_SEQUENCES = {
        # READY_FOR_PACK = physical packing step, READY_TO_BILL = billing gate (closes job)
        # Kept for any direct imports — _stage_sequence_ih now uses
        # build_optical_stage_flow() for actual flow generation.
        "UNCOATED":         ["INSPECTION", "READY_FOR_PACK", "READY_TO_BILL"],
        "COLOURING":        ["INSPECTION", "COLOURING_PICKED", "COLOURING_DONE", "INSPECTION", "READY_FOR_PACK", "READY_TO_BILL"],
        "HARDCOAT":         ["INSPECTION", "HARDCOAT_PICKED", "HARDCOAT_DONE", "INSPECTION", "READY_FOR_PACK", "READY_TO_BILL"],
        "COLOURING_HC":     ["INSPECTION", "COLOURING_PICKED", "COLOURING_DONE", "HARDCOAT_PICKED", "HARDCOAT_DONE", "INSPECTION", "READY_FOR_PACK", "READY_TO_BILL"],
        "HARDCOAT_ARC":     ["INSPECTION", "HARDCOAT_PICKED", "HARDCOAT_DONE", "ARC_SENT", "ARC_RECEIVED", "FINAL_QC", "READY_FOR_PACK", "READY_TO_BILL"],
        "COLOURING_HC_ARC": ["INSPECTION", "COLOURING_PICKED", "COLOURING_DONE", "HARDCOAT_PICKED", "HARDCOAT_DONE", "ARC_SENT", "ARC_RECEIVED", "FINAL_QC", "READY_FOR_PACK", "READY_TO_BILL"],
    }

    def _lp_dict_ih(line: dict) -> dict:
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            try:
                lp = _ji.loads(lp)
            except Exception as _e:
                lp = {}
        return lp if isinstance(lp, dict) else {}


    def _is_production_lens_line_ih(line: dict) -> bool:
        """Only true optical lens lines should enter in-house production.
        Frames/accessories/other stock items should go billing + procurement, not job_master.
        """
        eye = str(line.get("eye_side") or "").upper()
        if eye not in ("R", "L", "RE", "LE"):
            return False
        text = " ".join(str(line.get(k) or "") for k in (
            "product_name", "brand", "main_group", "category", "lens_category", "unit"
        )).upper()
        blocked = ("FRAME", "CROSSLINE", "ACCESSORY", "CASE", "CLEANER", "CLOTH")
        if any(b in text for b in blocked):
            return False
        return ("LENS" in text or "UV" in text or "KT" in text or "KRYPTOK" in text or "PROGRESSIVE" in text or "STOCK" in text)

    def _service_kind_ih(line: dict) -> str:
        lp = _lp_dict_ih(line)
        text = " ".join(str(x or "") for x in (
            lp.get("service_production_type"),
            lp.get("service_group"),
            lp.get("charge_type"),
            lp.get("service_type"),
            lp.get("manufacturing_route"),
            lp.get("service_description"),
            lp.get("description"),
            line.get("product_name"),
            line.get("category"),
            line.get("main_group"),
        )).upper()
        is_service = (
            bool(line.get("is_service_line"))
            or str(line.get("eye_side") or "").upper() in ("S", "SERVICE")
            or "SERVICE" in text
        )
        if not is_service:
            return ""
        if "COLOUR" in text or "COLOR" in text or "TINT" in text:
            return "COLOURING"
        if "FITT" in text:
            return "FITTING"
        return "SERVICE"

    def _service_code_from_line_ih(line: dict, kind: str) -> str:
        lp = _lp_dict_ih(line)
        for key in ("service_code", "charge_code", "service_type", "charge_type"):
            val = str(lp.get(key) or "").upper().strip()
            if val:
                if kind == "COLOURING" and ("COLOUR" in val or "COLOR" in val or "TINT" in val):
                    return val
                if kind == "FITTING" and ("FIT" in val or "FRAME" in val):
                    return val
        return "COLOUR_LIGHT" if kind == "COLOURING" else "FIT_STANDARD"

    def _service_master_options_ih(kind: str) -> list[dict]:
        try:
            from modules.backoffice.service_master import fetch_service_types
            rows = fetch_service_types(kind, active_only=True) or []
            if rows:
                return rows
        except Exception as _e:
            log.warning("Could not fetch service types for %s: %s", kind, _e)
        return []

    def _service_provider_options_ih(kind: str) -> list[dict]:
        try:
            from modules.backoffice.service_master import fetch_providers
            rows = fetch_providers(kind, active_only=True) or []
            if rows:
                try:
                    fitter_rows = _q("""
                        SELECT id::text, fitter_name, contact
                        FROM fitters
                        WHERE COALESCE(is_active, TRUE)=TRUE
                    """) or []
                    phone_by_id = {str(r.get("id") or ""): str(r.get("contact") or "") for r in fitter_rows}
                    phone_by_name = {
                        str(r.get("fitter_name") or "").strip().lower(): str(r.get("contact") or "")
                        for r in fitter_rows
                        if str(r.get("fitter_name") or "").strip()
                    }
                    for row in rows:
                        if not str(row.get("contact") or "").strip():
                            row["contact"] = (
                                phone_by_id.get(str(row.get("id") or ""), "")
                                or phone_by_name.get(str(row.get("provider_name") or "").strip().lower(), "")
                            )
                except Exception as _fb_e:
                    log.debug("Provider phone fallback failed: %s", _fb_e)
                return rows
        except Exception as _e:
            log.warning("Could not fetch service providers for %s: %s", kind, _e)
        return []

    def _provider_rate_ih(provider_id: str, service_code: str) -> float:
        if not provider_id or not service_code:
            return 0.0
        try:
            rows = _q("""
                SELECT COALESCE(purchase_rate,0)::numeric AS rate
                FROM service_provider_rates
                WHERE provider_id = %(pid)s::uuid
                  AND service_code = %(code)s
                  AND COALESCE(is_active, TRUE)=TRUE
                  AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
                ORDER BY effective_from DESC
                LIMIT 1
            """, {"pid": provider_id, "code": service_code}) or []
            return float(rows[0].get("rate") or 0) if rows else 0.0
        except Exception as _e:
            log.warning("Provider rate lookup failed: %s", _e)
            return 0.0

    def _service_qty_pair_pcs_ih(line: dict) -> tuple[float, int]:
        lp = _lp_dict_ih(line)
        try:
            pair_qty = float(lp.get("service_qty_factor") or 0)
        except Exception:
            pair_qty = 0.0
        if pair_qty <= 0:
            eye = str(line.get("eye_side") or "").upper()
            if eye in ("R", "L", "RE", "LE"):
                pair_qty = 0.5
            else:
                try:
                    pair_qty = float(line.get("quantity") or line.get("billing_qty") or 1)
                except Exception:
                    pair_qty = 1.0
        pcs_qty = max(1, int(round(pair_qty * 2)))
        return round(pair_qty, 2), pcs_qty

    def _ensure_provider_payout_compat_ih(provider: dict, service_code: str, service_label: str) -> None:
        """Keep Service Management providers usable by the existing payout ledger."""
        try:
            from modules.sql_adapter import run_write as _rw_compat
            _pid = str(provider.get("id") or "").strip()
            if not _pid:
                return
            _ptype = str(provider.get("provider_type") or "FITTING").upper().strip() or "FITTING"
            _rw_compat("""
                INSERT INTO fitters (id, fitter_name, fitter_type, contact, address, is_active, notes)
                VALUES (%(id)s::uuid, %(name)s, %(type)s, %(phone)s, %(addr)s, TRUE, %(notes)s)
                ON CONFLICT (id) DO UPDATE SET
                    fitter_name = EXCLUDED.fitter_name,
                    fitter_type = EXCLUDED.fitter_type,
                    contact     = EXCLUDED.contact,
                    address     = EXCLUDED.address,
                    is_active   = TRUE,
                    notes       = EXCLUDED.notes
            """, {
                "id": _pid,
                "name": provider.get("provider_name") or "Service Provider",
                "type": _ptype,
                "phone": provider.get("contact") or "",
                "addr": provider.get("address") or "",
                "notes": provider.get("notes") or "",
            })
            if service_code:
                _rw_compat("""
                    INSERT INTO fitting_types (code, label, description, is_active, sort_order)
                    VALUES (%(code)s, %(label)s, 'Service provider payout type', TRUE,
                            (SELECT COALESCE(MAX(sort_order),0)+10 FROM fitting_types))
                    ON CONFLICT (code) DO UPDATE SET
                        label = EXCLUDED.label,
                        is_active = TRUE
                """, {"code": service_code, "label": service_label or service_code})
        except Exception as _e:
            log.warning("Provider payout compatibility sync failed: %s", _e)

    def _upsert_provider_assignment_ih(
        *,
        order_no: str,
        line_id: str,
        job_id: str,
        eye_side: str,
        provider: dict,
        service_code: str,
        service_label: str,
        rate: float,
        pair_rate: float,
        pair_qty: float,
        pcs_qty: int,
        remarks: str,
        status: str = "SENT",
    ) -> bool:
        try:
            from modules.sql_adapter import run_write as _rw_assign
            _pid = str(provider.get("id") or "").strip()
            if not (_pid and service_code and line_id and job_id):
                return False
            _ensure_provider_payout_compat_ih(provider, service_code, service_label)
            _rw_assign("""
                INSERT INTO fitting_assignments (
                    order_no, order_line_id, job_master_id, eye_side,
                    fitter_id, fitting_type_code, rate_applied, remarks,
                    sent_date, status, payment_status, paid_amount
                )
                SELECT
                    %(ono)s, %(lid)s::uuid, %(jid)s::uuid, %(eye)s,
                    %(pid)s::uuid, %(code)s, %(rate)s, %(remarks)s,
                    CURRENT_DATE, %(status)s, 'UNPAID', 0
                WHERE NOT EXISTS (
                    SELECT 1 FROM fitting_assignments
                    WHERE order_line_id = %(lid)s::uuid
                      AND job_master_id = %(jid)s::uuid
                      AND status NOT IN ('CANCELLED','VOID')
                )
            """, {
                "ono": order_no,
                "lid": line_id,
                "jid": job_id,
                "eye": eye_side or "S",
                "pid": _pid,
                "code": service_code,
                "rate": float(rate or 0),
                "remarks": (
                    f"Qty: {pcs_qty} pcs ({pair_qty:g} pair) | "
                    f"Pair rate: Rs.{float(pair_rate or 0):.2f}"
                    + (f" | {remarks}" if remarks else "")
                ),
                "status": status,
            })
            _rw_assign("""
                UPDATE fitting_assignments
                SET fitter_id = %(pid)s::uuid,
                    fitting_type_code = %(code)s,
                    rate_applied = %(rate)s,
                    remarks = %(remarks)s,
                    sent_date = COALESCE(sent_date, CURRENT_DATE),
                    status = CASE
                        WHEN status IN ('DONE','RECEIVED') THEN status
                        ELSE %(status)s
                    END,
                    updated_at = NOW()
                WHERE order_line_id = %(lid)s::uuid
                  AND job_master_id = %(jid)s::uuid
            """, {
                "lid": line_id,
                "jid": job_id,
                "pid": _pid,
                "code": service_code,
                "rate": float(rate or 0),
                "remarks": (
                    f"Qty: {pcs_qty} pcs ({pair_qty:g} pair) | "
                    f"Pair rate: Rs.{float(pair_rate or 0):.2f}"
                    + (f" | {remarks}" if remarks else "")
                ),
                "status": status,
            })
            return True
        except Exception as _e:
            log.warning("Provider assignment save failed: %s", _e)
            return False

    def _sync_provider_assignment_stage_ih(line_id: str, job_id: str, next_stage: str) -> None:
        try:
            from modules.sql_adapter import run_write as _rw_sync
            _stage = str(next_stage or "").upper()
            if _stage in ("COLOURING_DONE", "FITTING_DONE", "READY_FOR_PACK", "READY_TO_BILL"):
                _rw_sync("""
                    UPDATE fitting_assignments
                    SET status = CASE WHEN status = 'PAID' THEN status ELSE 'DONE' END,
                        received_date = COALESCE(received_date, CURRENT_DATE),
                        updated_at = NOW()
                    WHERE order_line_id = %(lid)s::uuid
                      AND job_master_id = %(jid)s::uuid
                      AND status NOT IN ('CANCELLED','VOID')
                """, {"lid": line_id, "jid": job_id})
            elif _stage in ("COLOURING_PICKED", "FITTING_SENT", "FITTING_PENDING"):
                _rw_sync("""
                    UPDATE fitting_assignments
                    SET status = CASE WHEN status IN ('DONE','RECEIVED','PAID') THEN status ELSE 'SENT' END,
                        sent_date = COALESCE(sent_date, CURRENT_DATE),
                        updated_at = NOW()
                    WHERE order_line_id = %(lid)s::uuid
                      AND job_master_id = %(jid)s::uuid
                      AND status NOT IN ('CANCELLED','VOID')
                """, {"lid": line_id, "jid": job_id})
        except Exception as _e:
            log.warning("Provider assignment stage sync failed: %s", _e)

    def _order_has_service_ih(order_id: str, service_type: str, production_ref: str = "") -> bool:
        try:
            from modules.sql_adapter import run_query as _rq_svc_ih
            rows = _rq_svc_ih(
                """
                SELECT 1
                FROM order_lines ol
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND (%(pref)s = '' OR COALESCE(ol.production_ref,'') = %(pref)s)
                  AND (
                    UPPER(COALESCE(ol.eye_side,'')) IN ('S','SERVICE')
                    OR COALESCE(ol.is_service_line, FALSE) = TRUE
                    OR LOWER(COALESCE(p.main_group,'')) LIKE '%%service%%'
                    OR LOWER(COALESCE(p.category,'')) LIKE '%%service%%'
                    OR LOWER(COALESCE(p.product_name,'')) LIKE '%%service%%'
                  )
                  AND (
                    LOWER(COALESCE(p.product_name,'')) LIKE %(needle)s
                    OR LOWER(COALESCE(ol.lens_params->>'service_type','')) LIKE %(needle)s
                    OR LOWER(COALESCE(ol.lens_params->>'charge_type','')) LIKE %(needle)s
                    OR LOWER(COALESCE(ol.lens_params->>'description','')) LIKE %(needle)s
                  )
                LIMIT 1
                """,
                {"oid": order_id, "needle": f"%{service_type.lower()}%", "pref": str(production_ref or "")},
            ) or []
            return bool(rows)
        except Exception as _e:
            return False

    def _coating_path_ih(line: dict, order_id: str = "") -> str:
        # Delegates to the module-level helper. Same logic as the first panel.
        lp = _lp_dict_ih(line)
        product_name = str(line.get("product_name") or "")
        coating = str(
            line.get("coating_type")
            or line.get("coating")
            or lp.get("coating")
            or lp.get("coating_type")
            or lp.get("coating_name")
            or ""
        ).strip()
        treatment = str(lp.get("treatment") or line.get("treatment") or "").strip()
        combined = f"{product_name} {coating} {treatment}"
        _pref = str(line.get("production_ref") or "")
        has_colouring = _order_has_service_ih(order_id, "colour", _pref) if order_id else False
        return detect_coating_path(combined, has_colouring)

    def _stage_sequence_ih(line: dict, order_id: str = "") -> list:
        # Service-only colouring/fitting jobs get their flat flow first.
        # Earlier this copy was missing the colouring shortcut that the first
        # copy has — those jobs would have been routed through UNCOATED.
        lp = _lp_dict_ih(line)
        service_type = str(lp.get("service_production_type") or "").upper()
        _pref = str(line.get("production_ref") or "")
        if service_type in ("COLOURING", "FITTING", "FITTING_ONLY"):
            _hc2 = service_type == "COLOURING" or _order_has_service_ih(order_id, "colour", _pref)
            _hf2 = service_type in ("FITTING", "FITTING_ONLY") or _order_has_service_ih(order_id, "fitting", _pref)
            if _hc2 and _hf2: return build_service_only_stage_flow("COLOURING+FITTING")
            if _hc2: return build_service_only_stage_flow("COLOURING")
            if _hf2: return build_service_only_stage_flow("FITTING")
            return build_service_only_stage_flow("FITTING")

        product_name = str(line.get("product_name") or "")
        coating = str(
            line.get("coating_type")
            or line.get("coating")
            or lp.get("coating")
            or lp.get("coating_type")
            or lp.get("coating_name")
            or ""
        ).strip()
        treatment = str(lp.get("treatment") or line.get("treatment") or "").strip()
        combined = f"{product_name} {coating} {treatment}"
        has_colouring = _order_has_service_ih(order_id, "colour", _pref) if order_id else False
        has_fitting   = _order_has_service_ih(order_id, "fitting", _pref) if order_id else False
        return build_optical_stage_flow(combined, has_colouring, has_fitting)

    def _stage_index_from_events_ih(seq: list, current_stage: str, events: list) -> int | None:
        """Resolve current position by walking real events through the dynamic flow."""
        cur_norm = normalize_stage_alias(current_stage)
        positions = [i for i, s in enumerate(seq) if s == cur_norm]
        if not positions:
            return None
        progressed_idx = -1
        for ev in (events or []):
            ev_code = normalize_stage_alias(str(ev.get("stage_code") or ""))
            if not ev_code:
                continue
            next_hits = [i for i, s in enumerate(seq) if i >= progressed_idx and s == ev_code]
            if next_hits:
                progressed_idx = next_hits[0]
        if progressed_idx >= 0 and progressed_idx < len(seq) and seq[progressed_idx] == cur_norm:
            return progressed_idx
        next_current = [i for i in positions if i >= progressed_idx]
        if next_current:
            return next_current[0]
        return positions[-1]

    def _line_stage_events_ih(line: dict, job_events: list | None = None) -> list:
        """Merge DB stage events with lens_params.production_timeline fallback.
        Deduplicates by (stage_code, created_at minute) so a stage that exists
        in both job_stage_events and production_timeline only shows once.
        """
        merged = list(job_events or [])
        def _event_minute_key(value) -> str:
            return str(value or "").replace("T", " ")[:16]

        # Build a set of (stage_code, timestamp-to-minute) already covered by DB events
        _db_keys = set()
        for _ev in merged:
            _sc = str(_ev.get("stage_code") or "")
            _ts = _event_minute_key(_ev.get("created_at"))
            if _sc:
                _db_keys.add((_sc, _ts))

        lp = _lp_dict_ih(line)
        for ev in (lp.get("production_timeline") or []):
            if not isinstance(ev, dict):
                continue
            _sc = str(ev.get("stage") or "")
            _ts = _event_minute_key(ev.get("at"))
            # Only add from timeline if not already covered by a DB event
            if (_sc, _ts) not in _db_keys and _sc:
                merged.append({
                    "stage_code": _sc,
                    "created_at": ev.get("at"),
                    "remarks": ev.get("source") or "production_timeline",
                })
                _db_keys.add((_sc, _ts))

        _seen = set()
        _deduped = []
        for _ev in sorted(merged, key=lambda ev: _event_minute_key(_ev.get("created_at") or _ev.get("at"))):
            _code = normalize_stage_alias(str(_ev.get("stage_code") or _ev.get("stage") or ""))
            _key = (_code, _event_minute_key(_ev.get("created_at") or _ev.get("at")))
            if _key in _seen:
                continue
            _seen.add(_key)
            _deduped.append(_ev)
        return _deduped

    def _next_stage_ih(line: dict, current_stage: str, events: list, order_id: str = ""):
        seq = _stage_sequence_ih(line, order_id)
        idx = _stage_index_from_events_ih(seq, current_stage, events)
        if idx is None:
            return None
        if idx + 1 < len(seq):
            code = seq[idx + 1]
            return code, STAGE_LABEL.get(code, code)
        return None

    def _prev_stages_ih(line: dict, current_stage: str, events: list, order_id: str = "") -> list:
        seq = _stage_sequence_ih(line, order_id)
        idx = _stage_index_from_events_ih(seq, current_stage, events)
        if idx is None or idx <= 0:
            return []

        cur = normalize_stage_alias(current_stage)

        # Recede must be one logical step only. Restarting after surfacing
        # starts is handled by Reject, not by walking a lens back to Job Created.
        if cur in ("COLOURING_PICKED", "COLOURING_DONE", "HARDCOAT_PICKED", "HARDCOAT_DONE"):
            for j in range(idx - 1, -1, -1):
                if seq[j] == "INSPECTION":
                    return [("INSPECTION", STAGE_LABEL.get("INSPECTION", "Inspection"))]

        prev_code = seq[idx - 1]
        return [(prev_code, STAGE_LABEL.get(prev_code, prev_code))]

    # ── Date-range filter ────────────────────────────────────────────
    import datetime as _ih_dt
    if st.session_state.pop("_ih_date_reset_pending", False):
        st.session_state["ih_date_from"] = _ih_dt.date.today() - _ih_dt.timedelta(days=60)
        st.session_state["ih_date_to"]   = _ih_dt.date.today()
    _ih_date_from = st.session_state.get("ih_date_from",
                    _ih_dt.date.today() - _ih_dt.timedelta(days=60))
    _ih_date_to   = st.session_state.get("ih_date_to", _ih_dt.date.today())

    _dfrom_col, _dto_col, _src_col, _reset_col = st.columns([2, 2, 3, 1])
    with _dfrom_col:
        _ih_date_from = st.date_input("From", value=_ih_date_from, key="ih_date_from")
    with _dto_col:
        _ih_date_to   = st.date_input("To",   value=_ih_date_to,   key="ih_date_to")
    with _src_col:
        _ih_search = st.text_input("Search", placeholder="🔍 Order no / patient",
                                    key="ih_search", label_visibility="collapsed")
    with _reset_col:
        if st.button("🔄", key="ih_date_reset", use_container_width=True,
                     help="Reset to last 60 days"):
            st.session_state["_ih_date_reset_pending"] = True
            st.rerun()

    # ── Cached summary fetch (fast — order-level only, no line details) ──
    @st.cache_data(ttl=8, show_spinner=False)
    def _ih_load_summary(dfrom: str, dto: str) -> list:
        """
        Returns one row per order with its worst (earliest) stage and job count.
        Very fast — aggregates in DB, no product/surfacing details.
        """
        try:
            from modules.sql_adapter import run_query as _rqs
            return _rqs("""
                SELECT
                    o.id::text                                       AS order_id,
                    o.order_no,
                    COALESCE(ol.production_ref, o.order_no)          AS production_ref,
                    COALESCE(o.patient_name, o.party_name, '—')     AS patient_name,
                    o.status,
                    o.created_at::date::text                         AS order_date,
                    COUNT(DISTINCT ol.id)                            AS line_count,
                    COUNT(DISTINCT jm.id)                            AS job_count,
                    COUNT(DISTINCT jm.id) FILTER (WHERE jm.is_closed) AS closed_jobs,
                    -- Worst (earliest) stage across all lines
                    MIN(COALESCE(jm.current_stage, 'JOB_CREATED'))  AS min_stage,
                    -- Best (latest) stage
                    MAX(COALESCE(jm.current_stage, 'JOB_CREATED'))  AS max_stage,
                    -- All distinct stages as array
                    ARRAY_AGG(DISTINCT COALESCE(jm.current_stage,'JOB_CREATED'))
                        FILTER (WHERE jm.current_stage IS NOT NULL) AS stages,
                    BOOL_OR(COALESCE(jm.is_closed, FALSE))          AS any_closed,
                    COUNT(DISTINCT cl.id) > 0                          AS is_challaned,
                    COUNT(DISTINCT i.id)  > 0                          AS is_invoiced,
                    COUNT(jm.id) FILTER (WHERE NOT COALESCE(jm.is_closed,FALSE)) AS open_jobs
                FROM orders o
                JOIN order_lines ol ON ol.order_id = o.id
                LEFT JOIN job_master jm ON jm.order_line_id = ol.id
                LEFT JOIN challan_lines cl ON cl.order_line_id = ol.id
                LEFT JOIN challans c ON c.id = cl.challan_id
                    AND c.status NOT IN ('CANCELLED','VOID')
                LEFT JOIN invoices i ON i.challan_id = c.id
                    AND i.status NOT IN ('CANCELLED','VOID')
                WHERE o.status NOT IN ('CANCELLED','CLOSED')
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND ol.production_ref IS NOT NULL
                  AND (
                        (
                          COALESCE(ol.is_service_line, FALSE) = FALSE
                          AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
                          AND (
                              UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                              OR jm.id IS NOT NULL
                          )
                        )
                        OR (
                          ol.lens_params->>'service_production_type' IN ('COLOURING','FITTING')
                          AND UPPER(COALESCE(ol.batch_status,'')) NOT IN ('CANCELLED','DELETED','VOID')
                        )
                      )
                  AND o.created_at::date BETWEEN %(dfrom)s AND %(dto)s
                  AND (
                        UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                        OR jm.id IS NOT NULL
                        OR ol.lens_params->>'service_production_type' IN ('COLOURING','FITTING')
                      )
                GROUP BY o.id, o.order_no, ol.production_ref, o.patient_name, o.party_name,
                         o.status, o.created_at
                ORDER BY o.created_at DESC, production_ref
                LIMIT 300
            """, {"dfrom": dfrom, "dto": dto}) or []
        except Exception as _e:
            return []

    @st.cache_data(ttl=15, show_spinner=False)
    def _ih_load_order_lines(order_id: str) -> list:
        """
        Load full line detail for ONE order — called lazily when user
        opens the order card. Cached per order_id for 15 seconds.
        """
        try:
            from modules.sql_adapter import run_query as _rql
            import json as _ljson
            _raw = _rql("""
                SELECT
                    o.id::text              AS order_id,
                    o.order_no,
                    COALESCE(ol.production_ref, o.order_no) AS production_ref,
                    COALESCE(o.patient_name, o.party_name, '—') AS patient_name,
                    o.status,
                    ol.id::text             AS line_id,
                    ol.eye_side,
                    COALESCE(ol.is_service_line, FALSE) AS is_service_line,
                    ol.quantity,
                    COALESCE(ol.billing_qty, ol.quantity, 1) AS billing_qty,
                    COALESCE(ol.ready_qty, 0)    AS ready_qty,
                    ol.sph, ol.cyl, ol.axis, ol.add_power,
                    ol.lens_params,
                    ol.boxing_params,
                    COALESCE(p.product_name,
                             ol.lens_params->>'service_display_name',
                             ol.lens_params->>'display_product_name',
                             ol.lens_params->>'service_description',
                             'Service') AS product_name,
                    COALESCE(p.coating_type, '')  AS coating_type,
                    COALESCE(p.category, '')      AS category,
                    COALESCE(p.lens_category, '') AS lens_category,
                    COALESCE(p.main_group, '')    AS main_group,
                    COALESCE(p.brand, '')         AS brand,
                    o.created_at,
                    jm.id::text             AS job_id,
                    COALESCE(jm.current_stage, 'JOB_CREATED') AS lab_stage,
                    COALESCE(jm.is_closed, FALSE)              AS job_closed
                FROM order_lines ol
                JOIN orders o       ON o.id  = ol.order_id
                LEFT JOIN products p ON p.id = ol.product_id
                LEFT JOIN job_master jm ON jm.order_line_id = ol.id
                WHERE o.id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND ol.production_ref IS NOT NULL
                  AND (
                        (
                          COALESCE(ol.is_service_line, FALSE) = FALSE
                          AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
                          AND (
                              UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                              OR jm.id IS NOT NULL
                          )
                        )
                        OR (
                          ol.lens_params->>'service_production_type' IN ('COLOURING','FITTING')
                          AND UPPER(COALESCE(ol.batch_status,'')) NOT IN ('CANCELLED','DELETED','VOID')
                        )
                      )
                ORDER BY CASE WHEN ol.eye_side='R' THEN 0
                              WHEN ol.eye_side='L' THEN 1 ELSE 2 END
            """, {"oid": order_id}) or []
            return _raw
        except Exception as _e:
            return []

    # ── Load summary ─────────────────────────────────────────────────────
    _summary_rows = _ih_load_summary(str(_ih_date_from), str(_ih_date_to))

    # If user clicked “Open Full R/L Controls” from compact card, preserve that
    # exact order even if current search/date/stage filters would otherwise hide it.
    _ih_focus_order_id = str(st.session_state.get("_ih_full_order_id") or "")
    _ih_focus_order_no = str(st.session_state.get("_ih_full_order_no") or "")
    if _ih_focus_order_id and not any(str(r.get("order_id")) == _ih_focus_order_id for r in _summary_rows):
        try:
            from modules.sql_adapter import run_query as _rq_focus_ih
            _focus_rows = _rq_focus_ih("""
                SELECT
                    o.id::text                                       AS order_id,
                    o.order_no,
                    COALESCE(ol.production_ref, o.order_no)          AS production_ref,
                    COALESCE(o.patient_name, o.party_name, '—')     AS patient_name,
                    o.status,
                    o.created_at::date::text                         AS order_date,
                    COUNT(DISTINCT ol.id)                            AS line_count,
                    COUNT(DISTINCT jm.id)                            AS job_count,
                    COUNT(DISTINCT jm.id) FILTER (WHERE jm.is_closed) AS closed_jobs,
                    MIN(COALESCE(jm.current_stage, 'JOB_CREATED'))  AS min_stage,
                    MAX(COALESCE(jm.current_stage, 'JOB_CREATED'))  AS max_stage,
                    ARRAY_AGG(DISTINCT COALESCE(jm.current_stage,'JOB_CREATED'))
                        FILTER (WHERE jm.current_stage IS NOT NULL) AS stages,
                    BOOL_OR(COALESCE(jm.is_closed, FALSE))          AS any_closed
                FROM orders o
                JOIN order_lines ol ON ol.order_id = o.id
                LEFT JOIN job_master jm ON jm.order_line_id = ol.id
                WHERE o.id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND (
                        (
                          COALESCE(ol.is_service_line, FALSE) = FALSE
                          AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
                        )
                        OR (
                          ol.lens_params->>'service_production_type' IN ('COLOURING','FITTING')
                          AND UPPER(COALESCE(ol.batch_status,'')) NOT IN ('CANCELLED','DELETED','VOID')
                        )
                      )
                  AND (
                        UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                        OR jm.id IS NOT NULL
                        OR ol.lens_params->>'service_production_type' IN ('COLOURING','FITTING')
                      )
                GROUP BY o.id, o.order_no, ol.production_ref, o.patient_name, o.party_name,
                         o.status, o.created_at
                ORDER BY production_ref
            """, {"oid": _ih_focus_order_id}) or []
            if _focus_rows:
                _summary_rows = list(_focus_rows) + list(_summary_rows)
        except Exception as _e:
            pass

    # Apply search filter in Python (no extra DB round-trip)
    if not st.session_state.get("ih_show_closed", False):
        # Hide orders where ALL jobs are closed (truly complete)
        # Keep CHALLANED/BILLED orders if jobs are still open in production
        _summary_rows = [
            r for r in _summary_rows
            if not (
                str(r.get("status") or "").upper() in
                ("INVOICED", "DISPATCHED", "DELIVERED", "CLOSED", "CANCELLED")
                and int(r.get("open_jobs") or 0) == 0
            )
        ]

    if _ih_search.strip():
        _summary_rows = [
            r for r in _summary_rows
            if _scan_match(_ih_search, r.get("order_no", ""), r.get("patient_name", ""))
        ]

    if not _summary_rows:
        st.info("✅ No in-house jobs found for this period.")
        return

    # ── Build rows list for the rest of the pipeline (lazy per-order) ──
    # The pipeline card renderer expects the old "rows" list format.
    # We now build it on-demand per order when card is expanded.
    # For the TABLE view we need a minimal rows list (summary data only).
    # For the CARD view we load full lines lazily.

    # Build minimal rows for table view from summary
    rows = []
    for _sr in _summary_rows:
        _stages = _sr.get("stages") or [_sr.get("min_stage","JOB_CREATED")]
        for _stg in (_stages if _stages else ["JOB_CREATED"]):
            rows.append({
                "order_id":    _sr["order_id"],
                "order_no":    _sr["order_no"],
                "patient_name":_sr["patient_name"],
                "status":      _sr["status"],
                "created_at":  _sr["order_date"],
                "line_id":     _sr["order_id"],   # placeholder for grouping
                "eye_side":    "",
                "lab_stage":   _stg,
                "job_closed":  _sr.get("any_closed", False),
                "job_id":      "",
                "product_name":"",
                "quantity":    1,
                "sph": None, "cyl": None, "axis": None, "add_power": None,
                "lens_params": {}, "boxing_params": {},
            })
    # Deduplicate to one row per order for table grouping
    _seen_oids = set()
    _table_rows = []
    for _tr in rows:
        if _tr["order_id"] not in _seen_oids:
            _seen_oids.add(_tr["order_id"])
            _table_rows.append(_tr)
    rows = _table_rows  # table view uses summary rows

    # For card view: store summary indexed by order_id
    _ih_summary_by_oid = {_sr["order_id"]: _sr for _sr in _summary_rows}
    # Full line data loaded lazily — stored here when fetched
    _ih_lines_cache: dict = {}

    if not rows:
        st.info("✅ No in-house jobs found. Create job cards from the Backoffice → Documents tab.")
        return

    # ── lab_stage already set by SQL from job_master.current_stage ───
    # Normalize legacy/variant stage codes so the pipeline always uses canonical names
    _STAGE_ALIASES_IH = {
        "JOB_PRINTED":          "PRINTED",        # legacy code stored by advance_job_stage()
        "PRODUCTION_PICK":      "PRODUCTION_PICKED",
        "IN_PROD":              "PRODUCTION_PICKED",
        "SURFACING_DONE":       "PRODUCTION_DONE",
        "HARDCOTE":             "HARDCOAT_PICKED",
        # HARDCOAT_DONE is the correct canonical name — no alias needed
        "COLORING":             "COLOURING_PICKED",
        # COLOURING_DONE is canonical — no alias needed
        # FINAL_QC is canonical for ARC path — no alias
        "QC":                   "INSPECTION",
        "READY_FOR_BILLING":    "READY_TO_BILL",   # old supplier stage name
        # READY_FOR_PACK is a real stage — do NOT alias it away
    }
    for _row in rows:
        if not _row.get("lab_stage"):
            _row["lab_stage"] = "JOB_CREATED"
        else:
            _row["lab_stage"] = _STAGE_ALIASES_IH.get(
                str(_row["lab_stage"]).upper().strip(),
                _row["lab_stage"]
            )

    # ── Fetch stage event timestamps for all visible jobs ─────────────
    _all_job_ids = [r["job_id"] for r in rows if r.get("job_id")]
    _stage_events_by_job: dict = {}
    if _all_job_ids:
        try:
            import json as _jjson
            _ev_rows = _q("""
                SELECT
                    jse.job_id::text  AS job_id,
                    jse.stage_code,
                    jse.created_at,
                    jse.remarks,
                    jse.performed_by
                FROM job_stage_events jse
                WHERE jse.job_id = ANY(%(jids)s::uuid[])
                ORDER BY jse.created_at ASC
            """, {"jids": _all_job_ids}) or []
            for _ev in _ev_rows:
                _jkey = str(_ev["job_id"])
                _stage_events_by_job.setdefault(_jkey, []).append(_ev)
        except Exception as _e:
            pass  # timeline is best-effort — don't crash the page

    # ── Search / filter ───────────────────────────────────────────────
    import datetime as _dts_ih
    _today_ih = _dts_ih.date.today()
    with st.container(border=True):
        _sf1, _sf2, _sf3, _sf4, _sf5 = st.columns([3, 2, 2, 2, 1])
        _flt_ord = _sf1.text_input("Order", key="ihf_ord",
                                    placeholder="🔍 Order no / patient",
                                    label_visibility="collapsed")
        _all_stg_lbls  = ["All Stages"] + [s[1] for s in STAGES if s[0] != "REJECTED"]
        _all_stg_codes = ["ALL"]        + [s[0] for s in STAGES if s[0] != "REJECTED"]
        _flt_stg_lbl   = _sf2.selectbox("Stage", _all_stg_lbls, key="ihf_stg",
                                         label_visibility="collapsed")
        _flt_stg = _all_stg_codes[_all_stg_lbls.index(_flt_stg_lbl)]
        _flt_ih_from = _sf3.date_input("From", value=_today_ih - _dts_ih.timedelta(days=30),
                                        key="ihf_from", label_visibility="collapsed",
                                        format="DD/MM/YYYY")
        _flt_ih_to   = _sf4.date_input("To", value=_today_ih,
                                        key="ihf_to", label_visibility="collapsed",
                                        format="DD/MM/YYYY")
        _show_all_ih = _sf5.toggle("All", value=False, key="ihf_all",
                                    help="Show all orders including completed/billed")

    # Active-only: hide READY_TO_BILL jobs by default (they're done — billing takes over)
    # Show READY_TO_BILL and BILLED in default view
    _active_stages = {s[0] for s in STAGES if s[0] not in ("REJECTED",)}
    _active_stages |= {"READY_TO_BILL", "BILLED"}

    def _ih_matches(r):
        if _flt_ord and _flt_ord.strip():
            if not _scan_match(_flt_ord, r.get("order_no", ""), r.get("patient_name", "")):
                return False
        if _flt_stg != "ALL":
            if r.get("lab_stage","JOB_CREATED") != _flt_stg:
                return False
        # Default: hide completed/billed orders unless "All" toggled
        if not _show_all_ih and _flt_stg == "ALL":
            if r.get("lab_stage","JOB_CREATED") not in _active_stages:
                return False
        # Date range filter
        if not _show_all_ih:
            _odate = None
            try:
                _odate = r.get("created_at")
                if isinstance(_odate, str):
                    _odate = _dts_ih.date.fromisoformat(_odate[:10])
                elif hasattr(_odate, "date"):
                    _odate = _odate.date()
            except Exception as _e:
                pass
            if _odate:
                if _flt_ih_from and _odate < _flt_ih_from: return False
                if _flt_ih_to   and _odate > _flt_ih_to:   return False
        return True

    rows = [r for r in rows if _ih_matches(r)]
    if not rows:
        st.info("No lines match the current filters.")
        return

    # ── Re-group BEFORE rendering so we can apply order-level stage filter ──
    # Build groups first, then filter: if ANY line in the order matches the
    # stage filter, show ALL lines of that order (R and L together).
    from collections import OrderedDict as _od_pre
    _pre_groups = _od_pre()
    for _r in rows:
        _gk = _r["order_id"]
        if _gk not in _pre_groups:
            _pre_groups[_gk] = []
        _pre_groups[_gk].append(_r)

    # Now re-fetch ALL lines for orders that passed the filter,
    # so we never show an order with only one eye
    _matched_order_ids = list(_pre_groups.keys())
    if _matched_order_ids:
        try:
            _all_rows_for_matched = _q("""
                SELECT
                    o.id::text              AS order_id,
                    o.order_no,
                    COALESCE(ol.production_ref, o.order_no) AS production_ref,
                    COALESCE(o.patient_name, o.party_name, '—') AS patient_name,
                    o.status,
                    ol.id::text             AS line_id,
                    ol.eye_side,
                    ol.quantity,
                    COALESCE(ol.billing_qty, ol.quantity, 1) AS billing_qty,
                    COALESCE(ol.ready_qty, 0)    AS ready_qty,
                    ol.sph, ol.cyl, ol.axis, ol.add_power,
                    ol.lens_params,
                    ol.boxing_params,
                    p.product_name,
                    COALESCE(p.coating_type, '')  AS coating_type,
                    ''                            AS lens_index,
                    ''                            AS index_value,
                    COALESCE(p.category, '')      AS category,
                    COALESCE(p.lens_category, '') AS lens_category,
                    COALESCE(p.main_group, '')    AS main_group,
                    COALESCE(p.brand, '')         AS brand,
                    o.created_at,
                    jm.id::text             AS job_id,
                    COALESCE(jm.current_stage, 'JOB_CREATED') AS lab_stage,
                    COALESCE(jm.is_closed, FALSE)              AS job_closed
                FROM order_lines ol
                JOIN orders o       ON o.id  = ol.order_id
                LEFT JOIN products p ON p.id = ol.product_id
                LEFT JOIN job_master jm  ON jm.order_line_id = ol.id
                WHERE o.id = ANY(%(oids)s::uuid[])
                  AND COALESCE(ol.is_deleted, FALSE)  = FALSE
                  AND ol.production_ref IS NOT NULL
                  AND (
                        (
                          COALESCE(ol.is_service_line, FALSE) = FALSE
                          AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
                          AND UPPER(COALESCE(ol.eye_side,'')) IN ('R','L','RE','LE')
                        )
                        OR (
                          ol.lens_params->>'service_production_type' IN ('COLOURING','FITTING')
                          AND UPPER(COALESCE(ol.batch_status,'')) NOT IN ('CANCELLED','DELETED','VOID')
                        )
                      )
                  AND NOT (
                        LOWER(COALESCE(p.main_group,'')) LIKE '%%frame%%'
                     OR LOWER(COALESCE(p.category,'')) LIKE '%%frame%%'
                     OR LOWER(COALESCE(p.lens_category,'')) LIKE '%%frame%%'
                     OR LOWER(COALESCE(p.product_name,'')) LIKE '%%frame%%'
                     OR LOWER(COALESCE(p.product_name,'')) LIKE '%%crossline%%'
                  )
                  AND (
                        UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                        OR jm.id IS NOT NULL
                        OR ol.lens_params->>'service_production_type' IN ('COLOURING','FITTING')
                      )
                ORDER BY o.created_at DESC, o.order_no,
                         CASE WHEN ol.eye_side='R' THEN 0
                              WHEN ol.eye_side='L' THEN 1 ELSE 2 END
            """, {"oids": _matched_order_ids}) or rows  # fallback to filtered rows
            rows = _all_rows_for_matched
        except Exception as _e:
            pass  # keep original filtered rows on error

    _n_orders_ih = len(set(r['order_id'] for r in rows))
    _focused_full_order = bool(st.session_state.get("_ih_full_order_id"))
    _force_full_requested = bool(st.session_state.pop("_ih_force_full_view", False))
    _force_card_view = bool(_force_full_requested or _focused_full_order)
    if _force_full_requested and st.session_state.get("ih_tbl_view"):
        # Opening/refreshing a pinned full-card order must not immediately
        # collapse because the compact-view toggle still holds True.
        st.session_state["ih_tbl_view"] = False
    _ih_default = False if _focused_full_order else st.session_state.get("ih_tbl_view", True)
    _ihtbl_col, _ihcap_col = st.columns([1, 8])
    with _ihtbl_col:
        _ih_table_view_widget = st.toggle("⊞", value=_ih_default,
                                          key="ih_tbl_view", help="Compact table view")
    # Force full controls for the clicked order without writing to the widget key
    # after creation (avoids Streamlit session_state exceptions).
    _ih_table_view = False if _focused_full_order else _ih_table_view_widget
    if not _ih_table_view_widget and not _focused_full_order:
        # Manual/top full-view toggle should show the full current list. A stale
        # card focus from an earlier click must not hide sibling production refs.
        st.session_state.pop("_ih_full_order_id", None)
        st.session_state.pop("_ih_full_order_no", None)
    if _ih_table_view_widget and _focused_full_order:
        # The compact/table toggle is now the explicit close action for a
        # pinned full-card order. Keep full view open across stage saves and
        # prints; close only when the operator presses the toggle again.
        st.session_state.pop("_ih_full_order_id", None)
        st.session_state.pop("_ih_full_order_no", None)
        _focused_full_order = False
        _force_card_view = False
        _ih_table_view = True
    with _ihcap_col:
        st.caption(f"Showing {_n_orders_ih} order(s) · {len(rows)} line(s)")

    if _focused_full_order and st.session_state.get("_ih_full_order_no"):
        _full_no_disp = str(st.session_state.get("_ih_full_order_no") or "")
        if _full_no_disp.upper().endswith("-F"):
            _full_no_disp = _full_no_disp[:-2] + " · Fit"
        elif _full_no_disp.upper().endswith("-C"):
            _full_no_disp = _full_no_disp[:-2] + " · Col"
        st.info(
            "Opened full R/L controls for "
            f"{_full_no_disp}. "
            "Use Compact View toggle to return to cards."
        )

    if _ih_table_view:
        # ── Table/card view: use summary data, load lines lazily ────────
        from collections import defaultdict as _ihdd
        _ih_grps = _ihdd(lambda: {"order_no":"","patient":"","lines":[],"order_id":"","created_at":""})
        for _sr2 in _summary_rows:
            _gk2 = _sr2["order_id"]
            _pref2 = str(_sr2.get("production_ref") or _sr2.get("order_no") or "")
            _card_key2 = f"{_gk2}:{_pref2}"
            # Check if full lines already fetched for this order
            _det_key2 = f"pp_detail_INHOUSE_{_production_card_key_suffix(_card_key2)}"
            _is_open  = st.session_state.get(_det_key2, False)
            if _is_open:
                # Load full lines (cached) only when card is open
                if _gk2 not in _ih_lines_cache:
                    _ih_lines_cache[_gk2] = _ih_load_order_lines(_gk2)
                _full_lines = [
                    _ln for _ln in _ih_lines_cache.get(_gk2, [])
                    if str(_ln.get("production_ref") or _ln.get("order_no") or "") == _pref2
                ]
            else:
                # Use summary row as a placeholder line (just for grouping display)
                _stages_sr = _sr2.get("stages") or [_sr2.get("min_stage","JOB_CREATED")]
                _full_lines = [{
                    "order_id":    _gk2,
                    "order_no":    _sr2["order_no"],
                    "production_ref": _pref2,
                    "patient_name":_sr2["patient_name"],
                    "status":      _sr2["status"],
                    "created_at":  _sr2["order_date"],
                    "line_id":     _gk2,
                    "eye_side":    "R",
                    "lab_stage":   _stages_sr[0] if _stages_sr else "JOB_CREATED",
                    "job_closed":  _sr2.get("any_closed",False),
                    "job_id":      "",
                    "product_name": f"{_sr2.get('job_count',0)} job(s)",
                    "quantity":    1,
                    "sph":None,"cyl":None,"axis":None,"add_power":None,
                    "lens_params":{},"boxing_params":{},
                    "_is_challaned": _sr2.get("is_challaned", False),
                    "_is_invoiced":  _sr2.get("is_invoiced", False),
                    "_open_jobs":    int(_sr2.get("open_jobs") or _sr2.get("job_count",1)),
                }]

            _ih_grps[_card_key2]["order_no"]   = _pref2
            _ih_grps[_card_key2]["parent_order_no"] = _sr2["order_no"]
            _ih_grps[_card_key2]["patient"]    = _sr2["patient_name"]
            _ih_grps[_card_key2]["order_id"]   = _gk2
            _ih_grps[_card_key2]["production_ref"] = _pref2
            _ih_grps[_card_key2]["created_at"] = str(_sr2.get("order_date",""))[:10]
            _ih_grps[_card_key2]["lines"]       = _full_lines

        _render_pipeline_cards(
            groups=_ih_grps,
            route_key="INHOUSE",
            stage_label_fn=lambda l: STAGE_LABEL.get(l.get("lab_stage","JOB_CREATED"),"JOB_CREATED").split(" ",1)[-1],
            stage_code_fn=lambda l: l.get("lab_stage","JOB_CREATED"),
            open_billing_fn=_go_to_billing,
        )
        return

    # ── Full R/L controls view ─────────────────────────────────────────
    # The compact card view uses summary placeholder rows for speed. Full
    # controls require real order_lines/job_master rows. Load them here,
    # and when a specific order was clicked, show only that order so it
    # never vanishes due to filters or summary placeholders.
    _focus_oid_full = str(st.session_state.get("_ih_full_order_id") or "")
    if _focus_oid_full:
        _full_rows = _ih_load_order_lines(_focus_oid_full)
    else:
        _full_rows = []
        for _sr_full in _summary_rows:
            _pref_full = str(_sr_full.get("production_ref") or _sr_full.get("order_no") or "")
            _full_rows.extend([
                _ln for _ln in _ih_load_order_lines(str(_sr_full.get("order_id") or ""))
                if str(_ln.get("production_ref") or _ln.get("order_no") or "") == _pref_full
            ])
    if _full_rows:
        rows = _full_rows
        for _row in rows:
            if not _row.get("lab_stage"):
                _row["lab_stage"] = "JOB_CREATED"
            else:
                _row["lab_stage"] = _STAGE_ALIASES_IH.get(
                    str(_row["lab_stage"]).upper().strip(),
                    _row["lab_stage"],
                )
        # Rebuild event map for real full-control jobs.
        _stage_events_by_job = {}
        _all_job_ids = [r["job_id"] for r in rows if r.get("job_id")]
        if _all_job_ids:
            try:
                _ev_rows = _q("""
                    SELECT
                        jse.job_id::text AS job_id,
                        jse.stage_code,
                        jse.created_at,
                        jse.remarks,
                        jse.performed_by
                    FROM job_stage_events jse
                    WHERE jse.job_id = ANY(%(jids)s::uuid[])
                    ORDER BY jse.created_at ASC
                """, {"jids": _all_job_ids}) or []
                for _ev in _ev_rows:
                    _stage_events_by_job.setdefault(str(_ev["job_id"]), []).append(_ev)
            except Exception as _e:
                pass
    else:
        st.warning("Could not load full R/L controls for this order. Try Refresh or clear filters.")
        return

    # One-shot nav guard — cleared each render so print works normally after nav
    st.session_state.pop("_navigating_to_billing", None)

    def _save_stage_ih(job_id, new_stage):
        from modules.sql_adapter import run_query as _rq_stage, run_write as _rw
        _job_ctx_rows = _rq_stage("""
            SELECT
                jm.current_stage,
                COALESCE(jm.is_closed, FALSE) AS is_closed,
                ol.order_id::text AS order_id,
                ol.id::text AS line_id,
                ol.eye_side,
                ol.quantity,
                COALESCE(ol.billing_qty, ol.quantity, 1) AS billing_qty,
                ol.sph, ol.cyl, ol.axis, ol.add_power,
                ol.lens_params,
                ol.boxing_params,
                ol.production_ref,
                COALESCE(ol.is_service_line, FALSE) AS is_service_line,
                ol.product_id::text AS product_id,
                COALESCE(p.product_name, '') AS product_name,
                COALESCE(p.coating_type, '') AS coating_type,
                COALESCE(p.category, '') AS category,
                COALESCE(p.lens_category, '') AS lens_category,
                COALESCE(p.main_group, '') AS main_group,
                COALESCE(p.brand, '') AS brand
            FROM job_master jm
            JOIN order_lines ol ON ol.id = jm.order_line_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE jm.id = %(jid)s::uuid
            LIMIT 1
        """, {"jid": job_id}) or []
        if not _job_ctx_rows:
            raise ValueError("Job not found. Refresh the production screen.")

        _job_ctx = dict(_job_ctx_rows[0])
        _current_stage = normalize_stage_alias(_job_ctx.get("current_stage") or "JOB_CREATED")
        _target_stage = normalize_stage_alias(new_stage)
        if _target_stage == _current_stage:
            return

        _stage_events = _rq_stage("""
            SELECT stage_code, created_at, remarks, performed_by
            FROM job_stage_events
            WHERE job_id = %(jid)s::uuid
            ORDER BY created_at ASC
        """, {"jid": job_id}) or []
        _next_allowed = _next_stage_ih(_job_ctx, _current_stage, _stage_events, _job_ctx.get("order_id") or "")
        _prev_allowed = _prev_stages_ih(_job_ctx, _current_stage, _stage_events, _job_ctx.get("order_id") or "")
        _allowed_targets = set()
        if _next_allowed:
            _allowed_targets.add(_next_allowed[0])
        _allowed_targets.update(code for code, _label in _prev_allowed)
        if _target_stage not in _allowed_targets:
            _cur_lbl = STAGE_LABEL.get(_current_stage, _current_stage)
            _tgt_lbl = STAGE_LABEL.get(_target_stage, _target_stage)
            raise ValueError(f"Invalid production move: {_cur_lbl} → {_tgt_lbl}. Use Reject to restart after production has started.")
        _svc_kind_ctx = _service_kind_ih(_job_ctx)
        if _svc_kind_ctx in ("COLOURING", "FITTING"):
            _seq_ctx = _stage_sequence_ih(_job_ctx, _job_ctx.get("order_id") or "")
            _cur_pos_ctx = _stage_index_from_events_ih(_seq_ctx, _current_stage, _stage_events)
            _tgt_hits_ctx = [i for i, s in enumerate(_seq_ctx) if s == _target_stage]
            _tgt_pos_ctx = _tgt_hits_ctx[0] if _tgt_hits_ctx else None
            _lp_ctx = _lp_dict_ih(_job_ctx)
            _provider_ok_ctx = bool(_lp_ctx.get("assigned_provider_id") or _lp_ctx.get("assigned_provider_name"))
            if (
                _cur_pos_ctx is not None
                and _tgt_pos_ctx is not None
                and _tgt_pos_ctx > _cur_pos_ctx
                and not _provider_ok_ctx
            ):
                raise ValueError("Select and save service provider before advancing this service job.")

        # Terminal stages close the job — enables billing gate
        # Only READY_TO_BILL closes the job (unlocks billing gate)
        # READY_FOR_PACK keeps job open — packing still in progress
        new_stage = _target_stage
        _terminal = new_stage == "READY_TO_BILL"
        _rw("""UPDATE job_master
               SET current_stage = %(stage)s,
                   is_closed     = %(closed)s,
                   updated_at    = NOW()
             WHERE id = %(jid)s::uuid""",
            {"stage": new_stage, "jid": job_id, "closed": _terminal})
        # Sync ready_qty on order_line when job reaches terminal stage
        if _terminal:
            try:
                _rw("""
                    UPDATE order_lines ol
                    SET ready_qty     = jm.total_qty,
                        allocated_qty = jm.total_qty
                    FROM job_master jm
                    WHERE jm.id = %(jid)s::uuid
                      AND ol.id = jm.order_line_id
                """, {"jid": job_id})
            except Exception as _e:
                import logging as _lg; _lg.getLogger(__name__).warning(f"[prod_page] silent err: {_e}")
        # Log event for stage timeline
        try:
            _rw("""
                INSERT INTO job_stage_events (id, job_id, stage_id, stage_code, created_at)
                SELECT gen_random_uuid(), %(jid)s::uuid, m.id, m.stage_code, NOW()
                FROM job_stage_master m WHERE m.stage_code = %(stage)s LIMIT 1
            """, {"jid": job_id, "stage": new_stage})
        except Exception as _e:
            log.warning("Job stage event insert failed for %s -> %s: %s", job_id, new_stage, _e)
        # job_stage_events is the live production ledger. Earlier we also
        # appended the same stage into lens_params.production_timeline, which
        # made service jobs look duplicated and added an extra write per click.
        # Old production_timeline data is still read above as a fallback.
        # ── Sync backoffice order status after every stage advance ──────────
        try:
            from modules.sql_adapter import run_query as _rq_cos
            _cos_r = _rq_cos(
                "SELECT o.id::text AS oid, o.order_no, o.status "
                "FROM job_master jm "
                "JOIN order_lines ol ON ol.id = jm.order_line_id "
                "JOIN orders o ON o.id = ol.order_id "
                "WHERE jm.id = %(jid)s::uuid LIMIT 1",
                {"jid": job_id}
            )
            if _cos_r and new_stage in ("READY_TO_BILL", "READY_FOR_PACK"):
                from modules.backoffice.order_status_live import compute_order_status
                compute_order_status(
                    {"id": _cos_r[0]["oid"], "order_no": _cos_r[0]["order_no"],
                     "status": _cos_r[0]["status"]}, write=True
                )
        except Exception as _e:
            pass  # never block stage advance

        # ── Clear caches so next rerun reads fresh stage from DB ─────────
        def _clear_prod_stage_caches():
            for _fn in (_load_pipeline_overview, _ih_load_summary, _ih_load_order_lines):
                try:
                    _fn.clear()
                except Exception as _e:
                    pass
            try:
                st.session_state["prod_orders_loaded"] = False
            except Exception as _e:
                pass

        _clear_prod_stage_caches()

    def _power_str_ih(line):
        parts = []
        try:
            if line.get("sph") is not None: parts.append(f"SPH {float(line['sph']):+.2f}")
            if line.get("cyl") and abs(float(line["cyl"])) > 0.01: parts.append(f"CYL {float(line['cyl']):+.2f}")
            if line.get("axis"): parts.append(f"AX {int(line['axis'])}")
            if line.get("add_power") and float(line["add_power"]) > 0: parts.append(f"ADD +{float(line['add_power']):.2f}")
        except Exception: pass
        return "  ".join(parts)

    # ── Group by production_ref ───────────────────────────────────────
    from collections import OrderedDict as _od
    _groups = _od()
    for _row in rows:
        _oid = _row["order_id"]
        _pref = str(_row.get("production_ref") or _row.get("order_no") or "")
        _gkey = f"{_oid}:{_pref}"
        if _gkey not in _groups:
            _groups[_gkey] = {
                "order_id":     _oid,
                "order_no":     _pref,
                "parent_order_no": _row["order_no"],
                "production_ref": _pref,
                "patient_name": _row["patient_name"],
                "status":       _row.get("status",""),
                "lines":        [],
            }
        _groups[_gkey]["lines"].append(_row)

    # ── Render each order card ────────────────────────────────────────
    for _oid, odata in _groups.items():
        _order_uuid = str(odata.get("order_id") or "").split(":", 1)[0].strip()
        lines   = odata["lines"]
        # Count only lines that have an active job (have a job_id from job_master)
        # Service lines and frames without a job should not block billing
        _job_lines = [l for l in lines if l.get("job_id")]
        _total  = len(_job_lines) if _job_lines else len(lines)
        # Strict bill-ready set per spec: only stages that open billing.
        # READY_FOR_PACK is the packing step; FITTING_DONE is mid-flow.
        # Counting them as "ready" was misleading staff into thinking
        # billing was unlocked when it wasn't.
        _ready  = sum(1 for l in lines if l.get("lab_stage") in (
            "READY_TO_BILL","READY_FOR_BILLING"))
        _packing = sum(1 for l in lines if l.get("lab_stage") == "READY_FOR_PACK")

        # Check if order is billed from challans table
        _ih_all_billed = False
        try:
            from modules.sql_adapter import run_query as _rq_ihb
            # Count only lines that belong to THIS pipeline's lines (by line_id)
            _line_ids_ih = [str(l["line_id"]) for l in lines if l.get("line_id")]
            if _line_ids_ih:
                _ihb = _rq_ihb("""
                    SELECT COUNT(DISTINCT cl.order_line_id) AS n
                    FROM challan_lines cl
                    JOIN challans c ON c.id = cl.challan_id
                    WHERE cl.order_line_id = ANY(%(lids)s::uuid[])
                      AND c.status NOT IN ('CANCELLED','VOID')
                      AND COALESCE(c.is_deleted, FALSE) = FALSE
                """, {"lids": _line_ids_ih})
                _ih_billed_n = int((_ihb[0].get("n") or 0) if _ihb else 0)
                _ih_all_billed = (_ih_billed_n >= _total and _total > 0)
        except Exception as _e:
            pass

        if _ih_all_billed:
            _hdr_icon = "🧾"
        elif _ready == _total and _total > 0:
            _hdr_icon = "✅"
        elif all(l.get("lab_stage") in ("QC","READY") for l in lines):
            _hdr_icon = "🔍"
        else:
            _hdr_icon = "🔬"

        # Per-eye stage summary for header
        _rl_hdr = sorted(
            [l for l in lines if str(l.get("eye_side","")).upper() in ("R","L","RIGHT","LEFT")],
            key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1
        )
        _eye_stg_parts = []
        for _hl in _rl_hdr:
            _he = str(_hl.get("eye_side","")).upper()
            _hs = STAGE_LABEL.get(_hl.get("lab_stage") or "JOB_CREATED",
                                   _hl.get("lab_stage") or "JOB_CREATED")
            _hs_s = _hs.split(" ",1)[-1] if _hs and _hs[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" else _hs
            _eye_stg_parts.append(f"{_he}: {_hs_s}")
        _eye_stg_str = "  |  ".join(_eye_stg_parts)

        # Pre-compute advance state for top buttons
        _rl_lines = sorted(
            [l for l in lines if str(l.get("eye_side","")).upper() in ("R","L","RIGHT","LEFT")],
            key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1
        )

        with st.container(border=True):
            _odata_order_no_display = str(odata.get("order_no") or "")
            if _odata_order_no_display.upper().endswith("-F"):
                _odata_order_no_display = _odata_order_no_display[:-2] + " · Fit"
            elif _odata_order_no_display.upper().endswith("-C"):
                _odata_order_no_display = _odata_order_no_display[:-2] + " · Col"
            # Always-visible outer panel print action.
            # Kept above the header/action columns so operators can find it
            # without opening Details or entering the job-card workspace.
            if _ih_all_billed:
                st.button(
                    "🖨️ PRINT JOB CARD + LABELS",
                    key=f"ih_panel_print_locked_{_oid}",
                    use_container_width=True,
                    disabled=True,
                    help="Locked after billing",
                )
            else:
                _ih_panel_job_labels_print_button(odata, lines, f"ih_panel_top_{_oid}")

            # ── Top header row: order info + advance button ───────────
            _th1, _th2 = st.columns([3, 2])
            with _th1:
                if _ih_all_billed:
                    # Check if invoice exists (invoiced) or just challan
                    _has_invoice_ih = False
                    try:
                        from modules.sql_adapter import run_query as _rq_inv
                        _inv_chk = _rq_inv("""
                            SELECT 1 FROM invoices i
                            JOIN challans c ON c.id = i.challan_id
                            JOIN challan_lines cl ON cl.challan_id = c.id
                            WHERE cl.order_line_id = ANY(%(lids)s::uuid[])
                              AND i.status NOT IN ('CANCELLED','VOID')
                            LIMIT 1
                        """, {"lids": [str(l["line_id"]) for l in lines if l.get("line_id")]})
                        _has_invoice_ih = bool(_inv_chk)
                    except Exception as _e:
                        pass
                    _bill_label = "🧾 INVOICED — LOCKED" if _has_invoice_ih else "📋 CHALLANED — LOCKED"
                    _bill_color = "#22c55e" if _has_invoice_ih else "#3b82f6"
                    st.markdown(
                        f"<div style='padding:4px 0'>"
                        f"<span style='font-weight:800;color:#e2e8f0;font-size:1rem'>"
                        f"{'🧾' if _has_invoice_ih else '📋'} {_odata_order_no_display}</span>"
                        f"<span style='color:#64748b;font-size:0.82rem'> — {odata['patient_name']}</span><br>"
                        f"<span style='background:#052e16;color:{_bill_color};font-size:0.78rem;"
                        f"font-weight:700;padding:2px 10px;border-radius:4px;"
                        f"border:1px solid {_bill_color}'>{_bill_label}</span>"
                        f"</div>",
                        unsafe_allow_html=True
                    )
                else:
                    # Show bill-ready + packing separately so the card reflects
                    # the strict billing gate. "X/N ready" used to count packing
                    # too, which made staff click Open Billing prematurely.
                    _badge_parts = [f"{_ready}/{_total} ready"]
                    if _packing > 0:
                        _badge_parts.append(f"{_packing} packing")
                    st.markdown(
                        f"<div style='padding:4px 0'>"
                        f"<span style='font-weight:800;color:#e2e8f0;font-size:1rem'>"
                        f"{_hdr_icon} {_odata_order_no_display}</span>"
                        f"<span style='color:#64748b;font-size:0.82rem'> — {odata['patient_name']}</span><br>"
                        f"<span style='color:#475569;font-size:0.72rem'>"
                        f"🔬 In-house · " + " · ".join(_badge_parts)
                        + (f" · {_eye_stg_str}" if _eye_stg_str else "")
                        + f"</span></div>",
                        unsafe_allow_html=True
                    )
            with _th2:
                # Job card + open billing buttons at top
                _jc_btn_col, _bo_btn_col = st.columns(2)
                with _jc_btn_col:
                    # Show Job Card button for any line at JOB_CREATED or PRINTED (reprocess)
                    _jc_lines_ih = [l for l in lines if (l.get("lab_stage") or "JOB_CREATED")
                                    in ("JOB_CREATED", "PRINTED") and not _service_kind_ih(l)]
                    if _jc_lines_ih and not _ih_all_billed:
                        if st.button("📋 Job Card",
                                     key=f"ih_jc_open_{_oid}",
                                     use_container_width=True,
                                     help="Assign blank and print job card"):
                            # Toggle ALL pending lines open (both RE and LE)
                            for _jcl in _jc_lines_ih:
                                _jc_k = f"jc_open_{_jcl['line_id']}"
                                st.session_state[_jc_k] = not st.session_state.get(_jc_k, False)
                            st.rerun()
                with _bo_btn_col:
                    if _ih_all_billed:
                        st.markdown(
                            "<span style='color:#22c55e;font-size:0.75rem;font-weight:700'>"
                            "🧾 Billed</span>",
                            unsafe_allow_html=True
                        )
                    elif _ready == _total and _total > 0:
                        if st.button("💰 Open Billing",
                                     key=f"ih_bill_{_oid}",
                                     use_container_width=True, type="primary"):
                            _go_to_billing(odata["order_id"], odata["order_no"])
                # One advance button per eye if different stages, else combined
                # ── Billing lock check for top buttons ──
                _top_locked = _ih_all_billed
                if not _top_locked:
                    try:
                        from modules.sql_adapter import run_query as _rq_tl
                        _tl_lids = [str(l["line_id"]) for l in _rl_lines if l.get("line_id")]
                        if _tl_lids:
                            _tl_chk = _rq_tl("""
                                SELECT COUNT(*) AS n FROM challan_lines cl
                                JOIN challans c ON c.id = cl.challan_id
                                WHERE cl.order_line_id = ANY(%(lids)s::uuid[])
                                  AND c.status NOT IN ('CANCELLED','VOID')
                                  AND COALESCE(c.is_deleted, FALSE) = FALSE
                            """, {"lids": _tl_lids})
                            _top_locked = int((_tl_chk[0].get("n") or 0) if _tl_chk else 0) > 0
                    except Exception as _e:
                        pass

                # Normalize aliases before index lookup so "JOB_PRINTED" and "PRINTED"
                # both resolve to the same STAGE_IDX value and are treated as same stage
                _rl_stages = [
                    normalize_stage_alias(l.get("lab_stage") or "JOB_CREATED")
                    for l in _rl_lines
                ]
                _rl_adv_lines = [l for l in _rl_lines if l.get("job_id")]
                _min_idx   = min(STAGE_IDX.get(s,0) for s in _rl_stages) if _rl_stages else 0
                _max_idx   = max(STAGE_IDX.get(s,0) for s in _rl_stages) if _rl_stages else 0
                _same_stage = (_min_idx == _max_idx)

                if not _rl_lines:
                    st.caption("Service production line — use Details / Settings")
                elif not _rl_adv_lines:
                    st.caption("🛠 Create production job in Details first")
                elif _top_locked:
                    st.caption("🔒 Locked after billing")
                elif _same_stage:
                    _tnext = _next_stage_ih(
                        _rl_adv_lines[0],
                        _rl_adv_lines[0].get("lab_stage") or "JOB_CREATED",
                        _line_stage_events_ih(
                            _rl_adv_lines[0],
                            _stage_events_by_job.get(str(_rl_adv_lines[0].get("job_id") or ""), []),
                        ),
                        _oid,
                    ) if _rl_adv_lines else None
                    _teyes = "+".join(str(l.get("eye_side","")).upper() for l in _rl_lines)
                    if _tnext:
                        if st.button(f"▶ Advance {_teyes} → {_tnext[1]}",
                                     key=f"ih_top_adv_{_oid}",
                                     use_container_width=True, type="primary"):
                            try:
                                for _tl in _rl_adv_lines:
                                    _save_stage_ih(str(_tl["job_id"]), _tnext[0])
                                # Keep full-card focus pinned; only refresh caches
                                # so the same order remains open after stage save.
                                st.session_state["_ih_last_advanced_oid"] = _oid
                                try:
                                    _ih_load_summary.clear()
                                    _ih_load_lines.clear()
                                except Exception: pass
                                st.rerun()
                            except Exception as _te: st.error(str(_te))
                    else:
                        st.success("✅ All at final stage")
                else:
                    # Different stages — one button per eye
                    for _tl in _rl_adv_lines:
                        _te   = str(_tl.get("eye_side","")).upper()
                        _ts   = _tl.get("lab_stage") or "JOB_CREATED"
                        _tnxt = _next_stage_ih(
                            _tl, _ts,
                            _line_stage_events_ih(
                                _tl,
                                _stage_events_by_job.get(str(_tl.get("job_id") or ""), []),
                            ),
                            _oid,
                        )
                        if _tnxt:
                            if st.button(f"▶ {_te} → {_tnxt[1]}",
                                         key=f"ih_top_adv_{_oid}_{_te}",
                                         use_container_width=True, type="primary"):
                                try:
                                    _save_stage_ih(str(_tl["job_id"]), _tnxt[0])
                                    # Keep full-card focus pinned; only refresh caches
                                    # so the same order remains open after stage save.
                                    st.session_state["_ih_last_advanced_oid"] = _oid
                                    try:
                                        _ih_load_summary.clear()
                                        _ih_load_lines.clear()
                                    except Exception: pass
                                    st.rerun()
                                except Exception as _te2: st.error(str(_te2))

            # ── ↩ Set Back to Backoffice ──────────────────────────────────
            # Shown on every non-billed card so staff can roll back a stuck
            # order without navigating into a detail view.
            # NOTE: _oid is the group key "{uuid}:{production_ref}" — extract
            # the real UUID from odata["order_id"] which is the raw UUID column.
            _group_has_lens_lines = any(
                str((_ln.get("eye_side") or "")).upper() in ("R", "L", "RE", "LE", "RIGHT", "LEFT")
                and _service_kind_ih(_ln) not in ("COLOURING", "FITTING")
                for _ln in lines
            )
            if not _ih_all_billed and _group_has_lens_lines:
                try:
                    from modules.backoffice.production_rollback import render_set_back_panel as _ih_rsbp
                    _ih_uuid = str(odata.get("order_id") or "").split(":")[0].strip()
                    _ih_rsbp(
                        {
                            "id":       _ih_uuid,
                            "order_no": odata.get("order_no") or "",
                            "status":   odata.get("status") or "",
                        },
                        route_label="In-house",
                    )
                except Exception as _ih_sb_err:
                    import logging as _ih_sb_log
                    _ih_sb_log.getLogger(__name__).debug(
                        "[inhouse_pipeline] rollback panel skipped: %s", _ih_sb_err
                    )

            # ── Expander: per-line detail + controls ──────────────────
            with st.expander("🔍 Details / Settings", expanded=False):
                def _eye_sort_ih(x):
                    _e = str(x.get("eye_side","")).upper()
                    if _e in ("R","RIGHT"): return 0
                    if _e in ("L","LEFT"):  return 1
                    return 2

                _jc_rendered_pair_oids = set()  # track orders where paired JC already rendered

                # ── Frame / Fitting Details (once per order, before per-eye loop) ──
                # Saves into the FIRST inhouse lens line's lens_params so all eyes
                # share one fitting record. If there are no lines yet, skip.
                try:
                    _sorted_for_ff = sorted(lines, key=_eye_sort_ih)
                    _has_rl_for_ff = any(
                        str(_ln_ff.get("eye_side") or "").upper() in ("R", "L", "RIGHT", "LEFT")
                        for _ln_ff in _sorted_for_ff
                    )
                    _has_fitting_for_ff = any(_service_kind_ih(_ln_ff) == "FITTING" for _ln_ff in _sorted_for_ff)
                    if _sorted_for_ff and (_has_rl_for_ff or _has_fitting_for_ff):
                        _ff_anchor_line = _sorted_for_ff[0]
                        _ff_summary = _ih_summary_by_oid.get(_order_uuid, {})
                        _ff_order_dict = {
                            "order_id":       _order_uuid,
                            "id":             _order_uuid,
                            "order_no":       odata.get("order_no"),
                            "patient_name":   odata.get("patient_name") or "",
                            "party_name":     _ff_summary.get("party_name") or "",
                            "patient_mobile": _ff_summary.get("patient_mobile") or "",
                            "created_at":     _ff_summary.get("created_at"),
                        }
                        render_frame_fitting_details_section(_ff_anchor_line, _ff_order_dict)
                except Exception as _e:
                    pass  # never block stage controls if details panel errors

                _rl_lines_for_layout = [
                    l for l in sorted(lines, key=_eye_sort_ih)
                    if str(l.get("eye_side","")).upper() in ("R","RIGHT","L","LEFT")
                ]
                _other_lines_for_layout = [
                    l for l in sorted(lines, key=_eye_sort_ih)
                    if str(l.get("eye_side","")).upper() not in ("R","RIGHT","L","LEFT")
                ]
                if len(_rl_lines_for_layout) >= 2:
                    _eye_cols = st.columns(2, gap="large")
                    _eye_col_by_side = {"R": _eye_cols[0], "RIGHT": _eye_cols[0], "L": _eye_cols[1], "LEFT": _eye_cols[1]}
                else:
                    _single_eye_container = st.container()
                    _eye_col_by_side = {"R": _single_eye_container, "RIGHT": _single_eye_container, "L": _single_eye_container, "LEFT": _single_eye_container}

                for line in _rl_lines_for_layout + _other_lines_for_layout:
                    _eye_probe = str(line.get("eye_side","")).upper()
                    _eye_panel = _eye_col_by_side.get(_eye_probe, st.container())
                    with _eye_panel:
                        _lid    = str(line["line_id"])
                        _eye    = str(line.get("eye_side") or "").upper()
                        _pname  = str(line.get("product_name") or "").split(" | ")[0]
                        _needed = int(line.get("quantity") or 1)
                        _rdyq   = int(line.get("ready_qty") or 0)
                        _stage  = line.get("lab_stage") or "JOB_CREATED"
                        _jid    = str(line.get("job_id") or "")
                        _pwr    = _power_str_ih(line)
                        _sc     = _stg_clr(_stage)
                        _slbl   = STAGE_LABEL.get(_stage, _stage)
                        _lp_line_ih = _lp_dict_ih(line)
                        _idx_ih = str(
                            line.get("lens_index")
                            or _lp_line_ih.get("lens_index")
                            or _lp_line_ih.get("index")
                            or _lp_line_ih.get("index_value")
                            or ""
                        ).strip()
                        _coat_ih = str(
                            line.get("coating_type")
                            or line.get("coating")
                            or _lp_line_ih.get("coating")
                            or _lp_line_ih.get("coating_type")
                            or ""
                        ).strip()
                        _treat_ih = str(_lp_line_ih.get("treatment") or line.get("treatment") or "").strip()
                        _spec_ih = " | ".join(
                            x for x in [
                                f"Index {_idx_ih}" if _idx_ih else "",
                                _coat_ih,
                                _treat_ih if _treat_ih and _treat_ih.lower() != "clear" else "",
                            ] if x
                        )
                        _service_kind_line = _service_kind_ih(line)

                        if not _jid:
                            if _service_kind_line in ("COLOURING", "FITTING"):
                                _svc_label = "Colouring" if _service_kind_line == "COLOURING" else "Fitting"
                                _svc_instruction = str(
                                    _lp_line_ih.get("service_instruction")
                                    or _lp_line_ih.get("instruction")
                                    or _lp_line_ih.get("instructions")
                                    or _lp_line_ih.get("service_description")
                                    or _lp_line_ih.get("description")
                                    or ""
                                ).strip()
                                st.info(f"{_svc_label} production job is pending. Create the job to start stage tracking.")
                                if _svc_instruction:
                                    st.caption(f"Instruction: {_svc_instruction}")
                                if _service_kind_line == "COLOURING":
                                    _photo_name = str(_lp_line_ih.get("colour_sample_filename") or "").strip()
                                    if _photo_name:
                                        st.caption(f"Colour sample: {_photo_name}")
                                if st.button(f"🛠 Create {_svc_label} Job", key=f"ih_create_svc_job_{_lid}", use_container_width=True):
                                    try:
                                        from modules.sql_adapter import run_write as _rw_svc_job
                                        _qty_svc = int(float(line.get("billing_qty") or line.get("quantity") or 1))
                                        _rw_svc_job("""
                                            INSERT INTO job_master (
                                                id, order_line_id, total_qty, blank_required_qty,
                                                blank_allocated_qty, current_stage, reprocess_count,
                                                is_closed, created_at, updated_at
                                            )
                                            SELECT
                                                gen_random_uuid(), %(lid)s::uuid, %(qty)s, 0, 0,
                                                'JOB_CREATED', 0, FALSE, NOW(), NOW()
                                            WHERE NOT EXISTS (
                                                SELECT 1 FROM job_master WHERE order_line_id = %(lid)s::uuid
                                            )
                                        """, {"lid": _lid, "qty": max(_qty_svc, 1)})
                                        st.session_state["_ih_force_full_view"] = True
                                        st.session_state["_ih_full_order_id"] = _order_uuid
                                        st.session_state["_ih_full_order_no"] = odata.get("order_no") or ""
                                        st.success(f"{_svc_label} job created")
                                        st.rerun()
                                    except Exception as _svc_job_e:
                                        st.error(f"Could not create {_svc_label.lower()} job: {_svc_job_e}")
                                st.divider()
                                continue
                            if not _is_production_lens_line_ih(line):
                                st.info(f"{_pname} is a direct-bill service — tracked via billing, no production stage.")
                                continue
                            st.warning(f"{_eye or 'Line'} {_pname} is routed In-house but job is not created yet.")
                            if st.button("🛠 Create Production Job", key=f"ih_create_job_{_lid}", use_container_width=True):
                                try:
                                    from modules.documents.job_card_surfacing import _upsert_job_master
                                    _upsert_job_master(
                                        {
                                            **line,
                                            "id": _lid,
                                            "line_id": _lid,
                                            "billing_qty": line.get("billing_qty") or line.get("quantity") or 1,
                                        },
                                        {
                                            "id": line.get("order_id"),
                                            "order_no": line.get("order_no"),
                                            "patient_name": line.get("patient_name"),
                                        },
                                    )
                                    st.success("Production job created")
                                    st.rerun()
                                except Exception as _cj:
                                    st.error(f"Could not create production job: {_cj}")
                            st.divider()
                            continue

                        # RE/LE colour theme
                        _is_re = _eye in ("R","RIGHT")
                        _is_le = _eye in ("L","LEFT")
                        if _is_re:
                            _ea, _eb, _ebdr, _et, _ebb, _elbl = "#ef4444","#ef444412","#ef4444","#fca5a5","#7f1d1d","R"
                        elif _is_le:
                            _ea, _eb, _ebdr, _et, _ebb, _elbl = "#94a3b8","#1e293b","#475569","#cbd5e1","#0f172a","L"
                        else:
                            _ea, _eb, _ebdr, _et, _ebb, _elbl = "#64748b","#1e293b","#334155","#94a3b8","#1e293b",_eye or "—"

                        # Line header
                        st.markdown(
                            f"<div style='border:1px solid {_ebdr};border-left:5px solid {_ea};"
                            f"border-radius:6px;padding:8px 12px;margin-bottom:4px;background:{_eb}'>"
                            f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                            f"<span style='display:flex;align-items:center;gap:8px'>"
                            f"<span style='background:{_ebb};border:1px solid {_ea};color:{_ea};"
                            f"font-size:0.7rem;font-weight:800;padding:1px 8px;border-radius:4px;"
                            f"letter-spacing:.06em'>{_elbl}E</span>"
                            f"<span style='color:{_et};font-weight:700'>{_pname}"
                            + (f" <code style='font-size:0.72rem;color:{_ea}'>{_pwr}</code>" if _pwr else "")
                            + f"</span></span>"
                            f"<span style='background:{_sc}22;color:{_sc};font-size:0.7rem;"
                            f"font-weight:700;padding:2px 8px;border-radius:10px'>{_slbl}</span>"
                            f"</div>"
                            f"<div style='color:#64748b;font-size:0.75rem;margin-top:3px'>"
                            f"{_rdyq}/{_needed} pcs"
                            + (f" · {_spec_ih}" if _spec_ih else "")
                            + "</div></div>",
                            unsafe_allow_html=True
                        )

                        if _service_kind_line in ("COLOURING", "FITTING"):
                            _svc_title = "🎨 Colouring Order" if _service_kind_line == "COLOURING" else "🔧 Fitting Order"
                            _svc_instruction = str(
                                _lp_line_ih.get("service_instruction")
                                or _lp_line_ih.get("instruction")
                                or _lp_line_ih.get("instructions")
                                or _lp_line_ih.get("service_description")
                                or _lp_line_ih.get("description")
                                or ""
                            ).strip()
                            _svc_bits = []
                            if _service_kind_line == "COLOURING":
                                _shade = str(
                                    _lp_line_ih.get("colour")
                                    or _lp_line_ih.get("color")
                                    or _lp_line_ih.get("shade")
                                    or _lp_line_ih.get("tint_shade")
                                    or ""
                                ).strip()
                                if _shade:
                                    _svc_bits.append(f"Shade: {_shade}")
                                _photo_name = str(_lp_line_ih.get("colour_sample_filename") or "").strip()
                                if _photo_name:
                                    _svc_bits.append(f"Sample: {_photo_name}")
                            else:
                                _frame_note = str(
                                    _lp_line_ih.get("frame_notes")
                                    or _lp_line_ih.get("frame_type")
                                    or _lp_line_ih.get("fitting_note")
                                    or ""
                                ).strip()
                                if _frame_note:
                                    _svc_bits.append(f"Frame: {_frame_note}")
                            st.markdown(
                                "<div style='background:#0f172a;border:1px solid #334155;"
                                "border-radius:6px;padding:8px 12px;margin:4px 0'>"
                                f"<div style='color:#e2e8f0;font-weight:800;font-size:0.82rem'>{_svc_title}</div>"
                                + (f"<div style='color:#94a3b8;font-size:0.75rem;margin-top:3px'>{' · '.join(_svc_bits)}</div>" if _svc_bits else "")
                                + (f"<div style='color:#cbd5e1;font-size:0.76rem;margin-top:4px'>Instruction: {_svc_instruction}</div>" if _svc_instruction else "")
                                + "</div>",
                                unsafe_allow_html=True,
                            )
                            _photo_b64 = str(_lp_line_ih.get("colour_sample_photo") or "").strip()
                            if _service_kind_line == "COLOURING" and _photo_b64:
                                try:
                                    import base64 as _svc_b64
                                    import io as _svc_io
                                    st.image(
                                        _svc_io.BytesIO(_svc_b64.b64decode(_photo_b64)),
                                        caption="Colour sample",
                                        width=180,
                                    )
                                except Exception as _svc_img_e:
                                    log.debug("Could not render colour sample: %s", _svc_img_e)
                            with st.expander(
                                "🎨 Colouring Provider / WhatsApp" if _service_kind_line == "COLOURING"
                                else "🔧 Fitting Provider / WhatsApp",
                                expanded=False,
                            ):
                                _prov_name_cur = str(
                                    _lp_line_ih.get("assigned_provider_name")
                                    or _lp_line_ih.get("suggested_provider_name")
                                    or ""
                                )
                                _prov_phone_cur = str(
                                    _lp_line_ih.get("assigned_provider_phone")
                                    or _lp_line_ih.get("suggested_provider_phone")
                                    or ""
                                )
                                _svc_options = _service_master_options_ih(_service_kind_line)
                                _svc_default = _service_code_from_line_ih(line, _service_kind_line)
                                _svc_codes = [str(s.get("service_code") or "") for s in _svc_options]
                                _svc_labels = {
                                    str(s.get("service_code") or ""):
                                    f"{s.get('service_name') or s.get('service_code')} · {s.get('service_code')}"
                                    for s in _svc_options
                                }
                                if _svc_default not in _svc_codes and _svc_codes:
                                    _svc_default = _svc_codes[0]
                                _svc_code_sel = st.selectbox(
                                    "Service type",
                                    _svc_codes or [_svc_default],
                                    index=(_svc_codes or [_svc_default]).index(_svc_default),
                                    format_func=lambda x: _svc_labels.get(x, x),
                                    key=f"ih_svc_code_{_lid}",
                                )
                                _svc_label_sel = _svc_labels.get(_svc_code_sel, _svc_code_sel)

                                _providers = _service_provider_options_ih(_service_kind_line)
                                _provider_ids = [str(p.get("id") or "") for p in _providers]
                                _provider_by_id = {str(p.get("id") or ""): p for p in _providers}
                                _cur_provider_id = str(_lp_line_ih.get("assigned_provider_id") or _lp_line_ih.get("suggested_provider_id") or "")
                                _prov_opts = [""] + _provider_ids
                                _prov_idx = _prov_opts.index(_cur_provider_id) if _cur_provider_id in _prov_opts else 0
                                _prov_id_sel = st.selectbox(
                                    "Provider",
                                    _prov_opts,
                                    index=_prov_idx,
                                    format_func=lambda x: (
                                        "— Select provider from Service Management —"
                                        if not x else
                                        f"{_provider_by_id.get(x, {}).get('provider_name','')} · {_provider_by_id.get(x, {}).get('contact','') or 'no phone'}"
                                    ),
                                    key=f"ih_svc_provider_id_{_lid}",
                                )
                                _provider_sel = _provider_by_id.get(_prov_id_sel, {})
                                _prov_name_new = str(_provider_sel.get("provider_name") or _prov_name_cur or "").strip()
                                _prov_phone_master = str(_provider_sel.get("contact") or "").strip()
                                _prov_phone_new = str(_prov_phone_master or _prov_phone_cur or "").strip()
                                _rate_auto = _provider_rate_ih(_prov_id_sel, _svc_code_sel)
                                _rate_cur = float(_rate_auto or _lp_line_ih.get("assigned_provider_rate") or 0)
                                _pair_qty, _pcs_qty = _service_qty_pair_pcs_ih(line)
                                _payout_total = round(float(_rate_cur or 0) * float(_pair_qty or 0), 2)
                                st.caption(
                                    f"Production qty: {_pcs_qty} pcs ({_pair_qty:g} pair) · "
                                    f"Provider rates are per pair"
                                )
                                _pc1, _pc2, _pc3 = st.columns([1.2, 1.1, 1])
                                with _pc1:
                                    st.caption(_prov_name_new or "No provider selected")
                                with _pc2:
                                    _prov_phone_new = st.text_input(
                                        "Provider mobile",
                                        value=_prov_phone_new,
                                        key=f"ih_svc_provider_phone_edit_{_lid}_{_prov_id_sel[:8]}",
                                        placeholder="Enter mobile if missing",
                                    )
                                with _pc3:
                                    _rate_pair_new = st.number_input(
                                        "Pair rate",
                                        min_value=0.0,
                                        step=5.0,
                                        value=float(_rate_cur or 0),
                                        format="%.2f",
                                        key=f"ih_svc_provider_rate_{_lid}_{_prov_id_sel[:8]}_{_svc_code_sel}",
                                    )
                                _payout_total = round(float(_rate_pair_new or 0) * float(_pair_qty or 0), 2)
                                st.caption(f"Payout amount: Rs.{_payout_total:.2f}")
                                _fit_lens_details = ""
                                _fit_frame_details = ""
                                if _service_kind_line == "FITTING":
                                    _fit_ctx = _fitting_work_context_ih(
                                        str(odata.get("order_id") or line.get("order_id") or "").split(":")[0]
                                    )
                                    with st.expander("👓 Lens / Frame details for fitter", expanded=True):
                                        _fit_lens_details = st.text_area(
                                            "Lens details",
                                            value=str(
                                                _lp_line_ih.get("assigned_fitter_lens_details")
                                                or _fit_ctx.get("lens_summary")
                                                or ""
                                            ),
                                            key=f"ih_fit_lens_ctx_{_lid}",
                                            height=90,
                                            placeholder="Lens product, powers, coating, index...",
                                        )
                                        _fit_frame_details = st.text_area(
                                            "Frame details",
                                            value=str(
                                                _lp_line_ih.get("assigned_fitter_frame_details")
                                                or _fit_ctx.get("frame_summary")
                                                or ""
                                            ),
                                            key=f"ih_fit_frame_ctx_{_lid}",
                                            height=90,
                                            placeholder="Frame name, barcode, source, fitting instruction...",
                                        )
                                _provider_note = st.text_input(
                                    "Provider note",
                                    value=str(_lp_line_ih.get("assigned_provider_note") or ""),
                                    key=f"ih_svc_provider_note_{_lid}",
                                    placeholder="Frame name, colour shade, sample, urgency...",
                                )
                                if _service_kind_line == "COLOURING":
                                    _photo_name_cur = str(_lp_line_ih.get("colour_sample_filename") or "").strip()
                                    _photo_b64_cur = str(_lp_line_ih.get("colour_sample_photo") or "").strip()
                                    if _photo_name_cur:
                                        st.caption(f"Current colour sample: {_photo_name_cur}")
                                    if _photo_b64_cur:
                                        try:
                                            import base64 as _cur_b64
                                            import io as _cur_io
                                            _sample_bytes_cur = _cur_b64.b64decode(_photo_b64_cur)
                                            st.image(_cur_io.BytesIO(_sample_bytes_cur), caption="Current colour sample", width=160)
                                            st.download_button(
                                                "⬇ Download current sample",
                                                data=_sample_bytes_cur,
                                                file_name=_photo_name_cur or f"{_lid}_colour_sample.jpg",
                                                mime="image/jpeg",
                                                key=f"ih_svc_sample_dl_{_lid}",
                                                use_container_width=True,
                                            )
                                        except Exception as _sample_view_e:
                                            log.debug("Colour sample preview failed: %s", _sample_view_e)
                                    _new_sample = st.file_uploader(
                                        "Replace / upload colour sample",
                                        type=["jpg", "jpeg", "png", "webp"],
                                        key=f"ih_svc_sample_upload_{_lid}",
                                    )
                                else:
                                    _new_sample = None
                                if not _prov_id_sel:
                                    st.warning("Select a provider from Service Management to register payout and enable WhatsApp.")
                                elif not _prov_phone_new:
                                    st.info("Provider mobile is missing. Enter it here; saving will update Service Management also.")
                                if _prov_id_sel and _rate_pair_new <= 0:
                                    st.warning("Provider rate is 0. Add rate in Service Management or enter it here before saving.")

                                if st.button("💾 Save Provider + Register Payout", key=f"ih_svc_provider_save_{_lid}", use_container_width=True, disabled=not _prov_id_sel):
                                    try:
                                        from modules.sql_adapter import run_write as _rw_svc_provider
                                        if _prov_id_sel and str(_prov_phone_new or "").strip() and str(_prov_phone_new or "").strip() != _prov_phone_master:
                                            _rw_svc_provider("""
                                                UPDATE service_providers
                                                SET contact = %(ph)s,
                                                    updated_at = NOW()
                                                WHERE id = %(pid)s::uuid
                                                  AND COALESCE(NULLIF(contact,''), '') = ''
                                            """, {
                                                "pid": _prov_id_sel,
                                                "ph": str(_prov_phone_new or "").strip(),
                                            })
                                        _sample_b64_save = str(_lp_line_ih.get("colour_sample_photo") or "")
                                        _sample_name_save = str(_lp_line_ih.get("colour_sample_filename") or "")
                                        if _new_sample is not None:
                                            import base64 as _new_sample_b64
                                            _sample_b64_save = _new_sample_b64.b64encode(_new_sample.read()).decode("ascii")
                                            _sample_name_save = _new_sample.name or _sample_name_save
                                        _rw_svc_provider("""
                                            UPDATE order_lines
                                            SET lens_params = COALESCE(lens_params,'{}'::jsonb)
                                                || jsonb_build_object(
                                                    'assigned_provider_id', %(pid)s,
                                                    'assigned_provider_name', %(pn)s,
                                                    'assigned_provider_phone', %(ph)s,
                                                    'assigned_provider_rate', %(rate)s,
                                                    'assigned_provider_pair_rate', %(pair_rate)s,
                                                    'assigned_provider_pair_qty', %(pair_qty)s,
                                                    'assigned_provider_pcs_qty', %(pcs_qty)s,
                                                    'assigned_provider_service_code', %(scode)s,
                                                    'assigned_provider_service_label', %(slabel)s,
                                                    'assigned_provider_note', %(note)s,
                                                    'colour_sample_photo', %(sample_photo)s,
                                                    'colour_sample_filename', %(sample_name)s,
                                                    'assigned_fitter_lens_details', %(fit_lens_details)s,
                                                    'assigned_fitter_frame_details', %(fit_frame_details)s,
                                                    'assigned_provider_saved_at', to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SSOF')
                                                )
                                            WHERE id = %(lid)s::uuid
                                        """, {
                                            "lid": _lid,
                                            "pid": _prov_id_sel,
                                            "pn": str(_prov_name_new or "").strip(),
                                            "ph": str(_prov_phone_new or "").strip(),
                                            "rate": float(_payout_total or 0),
                                            "pair_rate": float(_rate_pair_new or 0),
                                            "pair_qty": float(_pair_qty or 0),
                                            "pcs_qty": int(_pcs_qty or 0),
                                            "scode": _svc_code_sel,
                                            "slabel": _svc_label_sel,
                                            "note": _provider_note,
                                            "sample_photo": _sample_b64_save,
                                            "sample_name": _sample_name_save,
                                            "fit_lens_details": _fit_lens_details,
                                            "fit_frame_details": _fit_frame_details,
                                        })
                                        _assignment_remarks = str(_provider_note or "").strip()
                                        if _service_kind_line == "FITTING":
                                            _fit_rem_bits = []
                                            if _fit_lens_details:
                                                _fit_rem_bits.append(f"Lens: {_fit_lens_details}")
                                            if _fit_frame_details:
                                                _fit_rem_bits.append(f"Frame: {_fit_frame_details}")
                                            if _assignment_remarks:
                                                _fit_rem_bits.append(f"Note: {_assignment_remarks}")
                                            _assignment_remarks = "\n".join(_fit_rem_bits)
                                        _assignment_ok = _upsert_provider_assignment_ih(
                                            order_no=str(odata.get("order_no") or ""),
                                            line_id=_lid,
                                            job_id=_jid,
                                            eye_side=_eye or "S",
                                            provider=_provider_sel,
                                            service_code=_svc_code_sel,
                                            service_label=_svc_label_sel,
                                            rate=float(_payout_total or 0),
                                            pair_rate=float(_rate_pair_new or 0),
                                            pair_qty=float(_pair_qty or 0),
                                            pcs_qty=int(_pcs_qty or 0),
                                            remarks=_assignment_remarks,
                                            status="SENT",
                                        )
                                        if _assignment_ok:
                                            st.success("Provider saved and payout registered")
                                        else:
                                            st.warning("Provider saved, but payout registration could not be completed. Check provider/rate setup.")
                                        st.session_state["_ih_force_full_view"] = True
                                        st.session_state["_ih_full_order_id"] = _order_uuid
                                        st.session_state["_ih_full_order_no"] = odata.get("order_no") or ""
                                        st.rerun()
                                    except Exception as _svc_provider_e:
                                        st.error(f"Provider save failed: {_svc_provider_e}")
                                _wa_phone = "".join(ch for ch in str(_prov_phone_new or _prov_phone_cur or "") if ch.isdigit())
                                if len(_wa_phone) >= 10:
                                    _wa_phone = _wa_phone[-10:]
                                    import urllib.parse as _svc_up
                                    _svc_name = "Colouring" if _service_kind_line == "COLOURING" else "Fitting"
                                    _wa_msg = (
                                        f"{_svc_name} work assigned\\n"
                                        f"Order: {odata.get('order_no')}\\n"
                                        f"Production Ref: {line.get('production_ref') or odata.get('order_no')}\\n"
                                        f"Line: {_pname}\\n"
                                        f"Qty: {_pcs_qty} pcs ({_pair_qty:g} pair)\\n"
                                        f"Service: {_svc_label_sel}\\n"
                                        + (f"Frame/Details: {' · '.join(_svc_bits)}\\n" if _svc_bits else "")
                                        + (f"Pair Rate: Rs.{float(_rate_pair_new or 0):.2f}\\n" if float(_rate_pair_new or 0) > 0 else "")
                                        + (f"Payout: Rs.{float(_payout_total or 0):.2f}\\n" if float(_payout_total or 0) > 0 else "")
                                        + (f"Lens Details:\\n{_fit_lens_details}\\n" if _service_kind_line == "FITTING" and _fit_lens_details else "")
                                        + (f"Frame Details:\\n{_fit_frame_details}\\n" if _service_kind_line == "FITTING" and _fit_frame_details else "")
                                        + (f"Provider note: {_provider_note}\\n" if _provider_note else "")
                                        + (f"Sample file: {_lp_line_ih.get('colour_sample_filename')}\\n" if _service_kind_line == "COLOURING" and _lp_line_ih.get("colour_sample_filename") else "")
                                        + (
                                            "Please see colour sample separately and confirm receipt/completion time."
                                            if _service_kind_line == "COLOURING" and _lp_line_ih.get("colour_sample_filename")
                                            else "Please confirm receipt and completion time."
                                        )
                                    )
                                    st.link_button(
                                        "📲 Send WhatsApp",
                                        f"https://wa.me/91{_wa_phone}?text={_svc_up.quote(_wa_msg)}",
                                        use_container_width=True,
                                    )
                                else:
                                    st.caption("Enter provider mobile to enable WhatsApp.")

                        # ── Stage timeline (time tags) ────────────────────
                        _job_evs = _line_stage_events_ih(line, _stage_events_by_job.get(_jid, []))
                        if _job_evs:
                            _tl_html = "<div style='display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 8px 0'>"
                            for _ev in _job_evs:
                                _ec  = str(_ev.get("stage_code") or "")
                                _elb = STAGE_LABEL.get(_ec, _ec).split(" ",1)[-1] if _ec else "—"
                                _ets = str(_ev.get("created_at") or "")[:16].replace("T"," ")
                                _ecc = _stg_clr(_ec)
                                _tl_html += (
                                    f"<span style='background:{_ecc}18;border:1px solid {_ecc}44;"
                                    f"border-radius:4px;padding:1px 7px;font-size:0.68rem;color:{_ecc};"
                                    f"white-space:nowrap' title='{_ets}'>"
                                    f"{_elb} <span style='color:#475569;font-size:0.62rem'>{_ets}</span>"
                                    f"</span>"
                                )
                            _tl_html += "</div>"
                            st.markdown(_tl_html, unsafe_allow_html=True)

                        # Per-line advance
                        _cur_idx = STAGE_IDX.get(_stage, 0)
                        _is_rejected = (_stage == "REJECTED")

                        # ── Billing lock: check if this line is challaned or invoiced ──
                        _is_challaned  = False
                        _is_invoiced   = False
                        try:
                            from modules.sql_adapter import run_query as _rq_lock
                            _lock_chk = _rq_lock("""
                                SELECT
                                    c.status AS challan_status,
                                    (SELECT 1 FROM invoices i
                                     WHERE i.challan_id = c.id
                                       AND i.status NOT IN ('CANCELLED','VOID')
                                     LIMIT 1) AS has_invoice
                                FROM challan_lines cl
                                JOIN challans c ON c.id = cl.challan_id
                                WHERE cl.order_line_id = %(lid)s::uuid
                                  AND c.status NOT IN ('CANCELLED','VOID')
                                  AND COALESCE(c.is_deleted, FALSE) = FALSE
                                LIMIT 1
                            """, {"lid": _lid})
                            if _lock_chk:
                                _is_challaned = True
                                _is_invoiced  = bool(_lock_chk[0].get("has_invoice"))
                        except Exception as _e:
                            pass

                        # ── Blank allocation guard ─────────────────────────────
                        # Stages that require a blank for LENS production.
                        # Service lines (COLOURING/FITTING on customer's own lenses)
                        # never need a blank — they operate on existing lenses.
                        _is_svc_line = (
                            bool(line.get("is_service_line"))
                            or str(line.get("eye_side","")).upper() in ("S","SERVICE")
                            or str((line.get("lens_params") or {}).get("manufacturing_route","")).upper() == "SERVICE"
                            or _service_kind_line in ("COLOURING", "FITTING")
                        )
                        _STAGES_NEEDING_BLANK = set() if _is_svc_line else {
                            "BLANK_ALLOCATED", "INSPECTION", "COLOURING_PICKED",
                            "COLOURING_DONE", "HARDCOAT_PICKED", "HARDCOAT_DONE",
                            "ARC_SENT", "ARC_RECEIVED", "FINAL_QC",
                            "READY_FOR_PACK", "READY_TO_BILL",
                        }
                        _blank_assigned = False
                        try:
                            from modules.sql_adapter import run_query as _rq_ba
                            _ba_rows = _rq_ba(
                                "SELECT 1 FROM blank_allocations "
                                "WHERE order_line_id = %(lid)s::uuid LIMIT 1",
                                {"lid": _lid}
                            )
                            _blank_assigned = bool(_ba_rows)
                            # Also accept if surfacing_data has blank_id (saved in job card
                            # but allocation row may not exist yet on old orders)
                            if not _blank_assigned:
                                _lp_check = (line.get("lens_params") or {})
                                if isinstance(_lp_check, str):
                                    import json as _jba
                                    try:
                                        _lp_check = _jba.loads(_lp_check)
                                    except Exception as _e:
                                        _lp_check = {}
                                _surf = (_lp_check.get("surfacing_data") or {})
                                _blank_assigned = bool(_surf.get("blank_id") or _surf.get("selected_blank_id"))
                        except Exception as _e:
                            log.warning("Blank assignment check failed for %s: %s", _lid, _e)
                            _blank_assigned = False

                        # Show billing status badge
                        if _is_invoiced:
                            st.markdown(
                                "<div style='background:#052e16;border:1px solid #22c55e;"
                                "border-radius:6px;padding:5px 12px;margin:4px 0;display:inline-block'>"
                                "<span style='color:#4ade80;font-weight:700;font-size:0.78rem'>"
                                "🧾 INVOICED — Stage changes locked</span></div>",
                                unsafe_allow_html=True
                            )
                        elif _is_challaned:
                            st.markdown(
                                "<div style='background:#0c1a2e;border:1px solid #0284c7;"
                                "border-radius:6px;padding:5px 12px;margin:4px 0;display:inline-block'>"
                                "<span style='color:#38bdf8;font-weight:700;font-size:0.78rem'>"
                                "📋 CHALLANED — Stage changes locked</span></div>",
                                unsafe_allow_html=True
                            )

                        _next    = None if _is_rejected else _next_stage_ih(
                            line, _stage, _job_evs, odata["order_id"]
                        )
                        _prev    = [] if _is_rejected else _prev_stages_ih(
                            line, _stage, _job_evs, odata["order_id"]
                        )
                        _svc_provider_saved = True
                        if _service_kind_line in ("COLOURING", "FITTING"):
                            _svc_provider_saved = bool(
                                _lp_line_ih.get("assigned_provider_id")
                                or _lp_line_ih.get("assigned_provider_name")
                            )

                        def _ih_print_line(_src):
                            if not _src:
                                return None
                            import json as _ihp_json
                            _out = dict(_src)
                            _lp_print = _out.get("lens_params") or {}
                            if isinstance(_lp_print, str):
                                try:
                                    _lp_print = _ihp_json.loads(_lp_print)
                                except Exception as _e:
                                    _lp_print = {}
                            _out["lens_params"] = _lp_print if isinstance(_lp_print, dict) else {}
                            _out["surfacing_data"] = (
                                _out.get("surfacing_data")
                                or _out["lens_params"].get("surfacing_data")
                                or {}
                            )
                            return _out

                        _partner_eye_for_print = "L" if _eye in ("R","RIGHT") else "R"
                        _partner_for_print = next(
                            (l for l in lines if str(l.get("eye_side","")).upper()[:1] == _partner_eye_for_print),
                            None,
                        )
                        _print_order_ih = {
                            "id": odata["order_id"],
                            "order_no": odata["order_no"],
                            "patient_name": odata["patient_name"],
                            "party_name": line.get("party_name", ""),
                            "order_type": line.get("order_type", "RETAIL"),
                        }

                        _qa1, _qa2, _qa3, _qa4, _qa5 = st.columns([2.1, 1.2, 1.2, 1.3, 1.8])
                        with _qa1:
                            if _is_rejected:
                                st.caption("🚫 Rejected")
                            elif _is_challaned:
                                st.caption("🔒 Locked after billing")
                            elif _next:
                                _adv_needs_blank = _next[0] in _STAGES_NEEDING_BLANK
                                if _service_kind_line in ("COLOURING", "FITTING") and not _svc_provider_saved:
                                    st.warning("Select and save provider first.")
                                elif _adv_needs_blank and not _blank_assigned:
                                    st.error("🔴 Assign blank first — open Documents → Job Cards")
                                elif st.button(
                                    f"▶ Advance to {_next[1]}",
                                    key=f"ih_fast_adv_{_lid}",
                                    type="primary",
                                    use_container_width=True,
                                ):
                                    try:
                                        _save_stage_ih(_jid, _next[0])
                                        if _service_kind_line in ("COLOURING", "FITTING"):
                                            _sync_provider_assignment_stage_ih(_lid, _jid, _next[0])
                                        # Keep full-card focus pinned; only refresh the
                                        # same order after stage save.
                                        st.session_state["_ih_force_full_view"] = True
                                        st.session_state["_ih_full_order_id"] = _order_uuid
                                        st.session_state["_ih_full_order_no"] = odata.get("order_no") or ""
                                        st.rerun()
                                    except Exception as _ae_fast:
                                        st.error(str(_ae_fast))
                            else:
                                st.success("✅ Final stage")
                        with _qa2:
                            if st.button("🏷️ Label", key=f"ih_fast_label_{_lid}", use_container_width=True):
                                try:
                                    from modules.backoffice.production_panel import (
                                        _build_label_page,
                                        _missing_blank_assignments_for_print,
                                        _open_print_window,
                                    )
                                    _print_seed = []
                                    if _eye in ("R", "RIGHT"):
                                        _print_seed = [_ih_print_line(x) for x in (line, _partner_for_print) if x]
                                    elif _eye in ("L", "LEFT"):
                                        _print_seed = [_ih_print_line(x) for x in (_partner_for_print, line) if x]
                                    else:
                                        _print_seed = [_ih_print_line(line)]
                                    _print_order_ih["_print_lines"] = _print_seed
                                    _missing = _missing_blank_assignments_for_print({
                                        **_print_order_ih,
                                        "lines": _print_seed,
                                    })
                                    if _missing:
                                        st.error(
                                            "🔴 Assignment not done — assign blank first for "
                                            f"{'/'.join(_missing)} eye before printing labels."
                                        )
                                    else:
                                        _open_print_window(_build_label_page(_print_seed, _print_order_ih))
                                except Exception as _lbl_fast_err:
                                    st.error(f"Label error: {_lbl_fast_err}")
                        with _qa3:
                            if st.button("💳 Card", key=f"ih_fast_card_{_lid}", use_container_width=True):
                                try:
                                    from modules.backoffice.production_panel import _build_cr80_page, _open_print_window
                                    _r_card = _ih_print_line(line if _eye in ("R","RIGHT") else _partner_for_print)
                                    _l_card = _ih_print_line(line if _eye in ("L","LEFT") else _partner_for_print)
                                    _open_print_window(_build_cr80_page(_r_card, _l_card, _print_order_ih))
                                except Exception as _card_fast_err:
                                    st.error(f"Card error: {_card_fast_err}")
                        with _qa4:
                            if not _blank_assigned:
                                st.button("🖨️ Job", key=f"ih_fast_job_{_lid}",
                                          use_container_width=True, disabled=True,
                                          help="Assign blank in Job Cards first")
                            elif not st.session_state.get("_navigating_to_billing") and st.button("🖨️ Job", key=f"ih_fast_job_{_lid}", use_container_width=True):
                                try:
                                    from modules.backoffice.production_panel import _missing_blank_assignments_for_print
                                    from modules.documents.job_card_surfacing import _open_jc_print_window
                                    _r_job = _ih_print_line(line if _eye in ("R","RIGHT") else _partner_for_print)
                                    _l_job = _ih_print_line(line if _eye in ("L","LEFT") else _partner_for_print)
                                    _missing_job = _missing_blank_assignments_for_print({
                                        **_print_order_ih,
                                        "lines": [x for x in (_r_job, _l_job) if x],
                                    })
                                    if _missing_job:
                                        st.error(
                                            "🔴 Assignment not done — assign blank first for "
                                            f"{'/'.join(_missing_job)} eye before printing job card."
                                        )
                                    else:
                                        _open_jc_print_window(_r_job, _l_job, _print_order_ih)
                                except Exception as _job_fast_err:
                                    st.error(f"Job card error: {_job_fast_err}")
                        with _qa5:
                            if _is_challaned or _is_invoiced:
                                st.caption("🔒 Printed")
                            else:
                                _ih_panel_job_labels_print_button(
                                    odata,
                                    lines,
                                    f"ih_details_{_lid}",
                                )

                        _rec_col, _rej_col = st.columns([1, 1])

                        with _rec_col:
                            if _prev and not _is_challaned:
                                _rec_lbls  = ["◀ Set back to..."] + [s[1] for s in _prev]
                                _rec_codes = [None] + [s[0] for s in _prev]
                                _sel_r = st.selectbox("Recede", _rec_lbls,
                                                       key=f"ih_rec_{_lid}",
                                                       label_visibility="collapsed")
                                _rc = _rec_codes[_rec_lbls.index(_sel_r)]
                                if _rc:
                                    if st.button("◀ Apply", key=f"ih_rec_btn_{_lid}",
                                                 use_container_width=True):
                                        try:
                                            _save_stage_ih(_jid, _rc)
                                            # Keep full-card focus pinned. Collapse only
                                            # when the operator presses the full-card toggle.
                                            st.session_state["_ih_force_full_view"] = True
                                            st.session_state["_ih_full_order_id"] = _order_uuid
                                            st.session_state["_ih_full_order_no"] = odata.get("order_no") or ""
                                            st.rerun()
                                        except Exception as _re: st.error(str(_re))
                            elif _is_challaned:
                                st.caption("🔒 Cannot reverse after billing")

                        with _rej_col:
                            if not _is_rejected:
                                # ── GATE: block reject if order already billed ──
                                _rej_blocked = _ih_all_billed
                                if not _rej_blocked:
                                    # Also check this specific line
                                    try:
                                        from modules.sql_adapter import run_query as _rq_rjg
                                        _rj_chk = _rq_rjg("""
                                            SELECT 1 FROM challan_lines cl
                                            JOIN challans c ON c.id = cl.challan_id
                                            WHERE cl.order_line_id = %(lid)s::uuid
                                              AND c.status NOT IN ('CANCELLED','VOID')
                                            LIMIT 1
                                        """, {"lid": _lid})
                                        _rej_blocked = bool(_rj_chk)
                                    except Exception as _e:
                                        pass
                                if _rej_blocked:
                                    st.caption("🔒 Billed")
                                else:
                                    if st.button("🚫 Reject", key=f"ih_rej_btn_{_lid}",
                                                 use_container_width=True):
                                        st.session_state[f"ih_rej_open_{_lid}"] = True

                        # ── Rejection form (shown on demand) ──────────────
                        if st.session_state.get(f"ih_rej_open_{_lid}"):
                            with st.container(border=True):
                                st.markdown(
                                    "<span style='color:#ef4444;font-weight:700;font-size:0.85rem'>"
                                    "🚫 Confirm Rejection</span>", unsafe_allow_html=True
                                )
                                _REJ_REASONS = [
                                    "— Select reason —",
                                    "Production Issue",
                                    "Power Issue — wrong SPH/CYL ground",
                                    "Lens made very thin",
                                    "Vibrations during surfacing",
                                    "Hardcoat / Coating failure",
                                    "Scratch / surface defect",
                                    "Wrong blank used",
                                    "Other (specify below)",
                                ]
                                _rej_sel = st.selectbox(
                                    "Reason", _REJ_REASONS,
                                    key=f"ih_rej_sel_{_lid}",
                                    label_visibility="collapsed"
                                )
                                _rej_custom = ""
                                if _rej_sel == "Other (specify below)":
                                    _rej_custom = st.text_input(
                                        "Specify", placeholder="Describe the issue...",
                                        key=f"ih_rej_custom_{_lid}",
                                        label_visibility="collapsed"
                                    )
                                _rej_reason = _rej_custom if _rej_sel == "Other (specify below)" else _rej_sel
                                _rej_valid = _rej_sel != "— Select reason —" and (
                                    _rej_sel != "Other (specify below)" or bool(_rej_custom.strip())
                                )
                                _rc1, _rc2 = st.columns(2)
                                with _rc1:
                                    if st.button("✔ Confirm Reject", key=f"ih_rej_confirm_{_lid}",
                                                 type="primary", use_container_width=True,
                                                 disabled=not _rej_valid):
                                        try:
                                            from modules.sql_adapter import run_scalar as _rs_rej, run_write as _rwrej, run_query as _rqrej
                                            import json as _rjson
                                            _rej_bin_pre = {}
                                            try:
                                                _pre_rows = _rqrej("""
                                                    SELECT ol.order_id::text AS oid,
                                                           ol.product_id::text AS pid,
                                                           ba.blank_id::text AS bid,
                                                           UPPER(COALESCE(ba.eye_side, ol.eye_side, '')) AS eye
                                                    FROM order_lines ol
                                                    LEFT JOIN blank_allocations ba
                                                      ON ba.order_line_id = ol.id
                                                    WHERE ol.id = %(lid)s::uuid
                                                    LIMIT 1
                                                """, {"lid": _lid}) or []
                                                _rej_bin_pre = _pre_rows[0] if _pre_rows else {}
                                            except Exception:
                                                _rej_bin_pre = {}

                                            # ── Atomic path: single DB transaction ──────────
                                            _rej_result = None
                                            try:
                                                _rej_result = _rs_rej(
                                                    "SELECT public.reject_and_return_blank("
                                                    "%(jid)s::uuid, %(lid)s::uuid, %(rmk)s)",
                                                    {"jid": _jid, "lid": _lid, "rmk": _rej_reason}
                                                )
                                            except Exception as _fn_e:
                                                _rej_result = None  # fall through to legacy

                                            if _rej_result and str(_rej_result).startswith("OK"):
                                                pass  # atomic succeeded
                                            else:
                                                # ── Legacy fallback (sequential) ────────────
                                                # 1. Reset job_master
                                                _rwrej("""
                                                    UPDATE job_master
                                                       SET current_stage  = 'JOB_CREATED',
                                                           is_closed      = FALSE,
                                                           reprocess_count = COALESCE(reprocess_count,0) + 1,
                                                           blank_allocated_qty = 0,
                                                           updated_at     = NOW()
                                                     WHERE id = %(jid)s::uuid
                                                """, {"jid": _jid})
                                                # 2. Return blank + delete allocation
                                                try:
                                                    from modules.sql_adapter import run_query as _rq_ba
                                                    _ba = _rq_ba("""
                                                        SELECT blank_id, eye_side FROM blank_allocations
                                                        WHERE order_line_id = %(lid)s::uuid LIMIT 1
                                                    """, {"lid": _lid})
                                                    if _ba:
                                                        _bl_id  = str(_ba[0]["blank_id"])
                                                        _bl_eye = str(_ba[0].get("eye_side") or _eye[:1]).upper()
                                                        _qty_col = "qty_left" if _bl_eye == "L" else "qty_right"
                                                        _rwrej(f"""
                                                            UPDATE blank_inventory
                                                            SET {_qty_col} = {_qty_col} + 1, updated_at = NOW()
                                                            WHERE id = %(bid)s::uuid
                                                        """, {"bid": _bl_id})
                                                        _rwrej("""
                                                            DELETE FROM blank_allocations
                                                            WHERE order_line_id = %(lid)s::uuid
                                                        """, {"lid": _lid})
                                                except Exception: pass
                                                # 3. Clear lens_params
                                                try:
                                                    _lp_rej = line.get("lens_params") or {}
                                                    if isinstance(_lp_rej, str):
                                                        try: _lp_rej = _rjson.loads(_lp_rej)
                                                        except Exception as e:
                                                            log.debug("Could not parse rejection lens_params: %s", e)
                                                            _lp_rej = {}
                                                    _lp_rej.pop("surfacing_data", None)
                                                    _lp_rej.pop("job_card_wip", None)
                                                    _rwrej("""
                                                        UPDATE order_lines
                                                        SET lens_params = %(lp)s::jsonb
                                                        WHERE id = %(lid)s::uuid
                                                    """, {"lp": _rjson.dumps(_lp_rej), "lid": _lid})
                                                except Exception: pass
                                                # 4. Log event
                                                try:
                                                    _rwrej("""
                                                        INSERT INTO job_stage_events
                                                            (id, job_id, stage_id, stage_code, remarks, created_at)
                                                        VALUES (gen_random_uuid(), %(jid)s::uuid, gen_random_uuid(),
                                                                'REJECTED', %(rmk)s, NOW())
                                                    """, {"jid": _jid, "rmk": _rej_reason})
                                                except Exception as _e:
                                                    log.warning("Reject stage event insert failed: %s", _e)

                                            # Phase-3: material-side rejection bin.
                                            # job_stage_events counts the event; this row counts
                                            # the rejected lens/item that should be visible in
                                            # the rejection-bin audit.
                                            try:
                                                from modules.sql_adapter import run_write as _rw_bin
                                                _bin_row = _rej_bin_pre or {}
                                                _bin_by = st.session_state.get("user_name", "system") if hasattr(st, "session_state") else "system"
                                                _rw_bin("""
                                                    INSERT INTO production_rejection_bin (
                                                        job_id, order_line_id, order_id, blank_id,
                                                        eye_side, product_id, qty, reason,
                                                        rejected_by, rejected_at, status
                                                    ) VALUES (
                                                        %(jid)s::uuid, %(lid)s::uuid,
                                                        NULLIF(%(oid)s,'')::uuid,
                                                        NULLIF(%(bid)s,'')::uuid,
                                                        NULLIF(%(eye)s,''),
                                                        NULLIF(%(pid)s,'')::uuid,
                                                        1, %(rmk)s, %(by)s, NOW(), 'IN_BIN'
                                                    )
                                                """, {
                                                    "jid": _jid,
                                                    "lid": _lid,
                                                    "oid": _bin_row.get("oid") or "",
                                                    "bid": _bin_row.get("bid") or "",
                                                    "eye": (str(_bin_row.get("eye") or "")[:1]),
                                                    "pid": _bin_row.get("pid") or "",
                                                    "rmk": _rej_reason,
                                                    "by": _bin_by,
                                                })
                                            except Exception as _bin_e:
                                                log.warning("[rejection_bin] insert failed (non-fatal): %s", _bin_e)

                                            # Open THIS eye's job card for re-entry
                                            st.session_state[f"jc_open_{_lid}"] = True
                                            st.session_state.pop(f"ih_rej_open_{_lid}", None)
                                            st.session_state[f"ih_rej_done_{_lid}"] = True
                                            st.success(f"↩ {_eye} eye rejected — select new blank to restart")
                                            st.rerun()
                                        except Exception as _rje: st.error(str(_rje))
                                with _rc2:
                                    if st.button("✕ Cancel", key=f"ih_rej_cancel_{_lid}",
                                                 use_container_width=True):
                                        st.session_state.pop(f"ih_rej_open_{_lid}", None)
                                        st.rerun()

                        # ── Job Card — blank assignment + print ───────────────
                        # Available at JOB_CREATED or PRINTED (after rejection reprocess)
                        # Pre-fills from lens_params.surfacing_data if already saved
                        # Allotment editable only before PRODUCTION_DONE stage
                        # Once in production, changes are blocked (blank physically in use)
                        _LOCK_FROM_STAGES = {
                            "PRODUCTION_DONE","HARDCOAT_PICKED","HARDCOAT_DONE",
                            "COLOURING_PICKED","COLOURING_DONE","ARC_SENT","ARC_RECEIVED",
                            "FINAL_QC","INSPECTION","READY_FOR_PACK","READY_TO_BILL",
                        }
                        _allotment_locked = (not _is_svc_line) and _stage in _LOCK_FROM_STAGES
                        _show_jc = (
                            (not _is_svc_line)
                            and _stage in ("JOB_CREATED", "PRINTED", "PRODUCTION_PICKED", "BLANK_ALLOCATED")
                            and not _ih_all_billed
                        )
                        # Skip if this eye's order was already rendered as a pair
                        if _show_jc and odata["order_id"] in _jc_rendered_pair_oids:
                            _show_jc = False
                        _CAN_REASSIGN_STAGES = {"PRODUCTION_PICKED","BLANK_ALLOCATED","PRINTED"}
                        _can_reassign = (
                            not _is_svc_line
                            and _stage in _CAN_REASSIGN_STAGES
                            and not _ih_all_billed
                        )
                        _NO_BLANK_RETURN_STAGES = {
                            "PRODUCTION_DONE","HARDCOAT_PICKED","HARDCOAT_DONE",
                            "ARC_SENT","ARC_RECEIVED","COLOURING_PICKED","COLOURING_DONE",
                            "INSPECTION","FINAL_QC","READY_FOR_PACK","FITTING_SENT",
                            "FITTING_RECEIVED","FITTING_DONE","FITTING_PENDING",
                            "READY_TO_BILL","BILLED",
                        }
                        _current_job_stage = str(_stage or "").upper()
                        _blank_return_locked = _current_job_stage in _NO_BLANK_RETURN_STAGES

                        if _allotment_locked:
                            if _can_reassign:
                                st.markdown(
                                    f"<div style='background:#0a1a2a;border:1px solid #3b82f644;"
                                    f"border-radius:6px;padding:6px 12px;margin:4px 0;"
                                    f"font-size:0.75rem;color:#93c5fd'>"
                                    f"🔄 Blank assigned — stage: <b>{STAGE_LABEL.get(_stage, _stage)}</b>. "
                                    f"Surfacing not started — you can change blank assignment below.</div>",
                                    unsafe_allow_html=True,
                                )
                                _reassign_key = f"reassign_blank_{_lid}"
                                if st.button("🔄 Change Blank Assignment", key=f"btn_{_reassign_key}",
                                             help="Release current blank and pick a different one."):
                                    st.session_state[_reassign_key] = not st.session_state.get(_reassign_key, False)
                                    st.rerun()
                                if st.session_state.get(_reassign_key):
                                    with st.container(border=True):
                                        st.markdown("**🔄 Reassigning blank** — current blank will be returned to inventory.")
                                        _confirm_reassign = st.checkbox(
                                            "✅ I confirm — return current blank and open new assignment",
                                            key=f"confirm_{_reassign_key}",
                                        )
                                        if _confirm_reassign and st.button("✅ Release & Reassign",
                                                                            key=f"do_{_reassign_key}", type="primary"):
                                            try:
                                                from modules.sql_adapter import run_query as _rq_rs, run_write as _rw_rs
                                                _alloc = _rq_rs(
                                                    "SELECT blank_id, eye_side FROM blank_allocations "
                                                    "WHERE order_line_id=%(lid)s::uuid LIMIT 1", {"lid": _lid}) or []
                                                if _alloc:
                                                    _bl_id  = str(_alloc[0]["blank_id"])
                                                    _bl_eye = str(_alloc[0].get("eye_side","R")).upper()
                                                    _qty_col = "qty_left" if _bl_eye == "L" else "qty_right"
                                                    if _current_job_stage not in _NO_BLANK_RETURN_STAGES:
                                                        _rw_rs(f"UPDATE blank_inventory SET {_qty_col}={_qty_col}+1, updated_at=NOW() WHERE id=%(bid)s::uuid", {"bid": _bl_id})
                                                    _rw_rs("INSERT INTO blank_stock_ledger (id,blank_id,order_line_id,eye_side,qty_change,ref_type,remarks,created_at) VALUES (gen_random_uuid(),%(bid)s::uuid,%(lid)s::uuid,%(eye)s,1,'REASSIGNMENT_RETURN','Blank returned — operator reassignment before surfacing',NOW())", {"bid":_bl_id,"lid":_lid,"eye":_bl_eye})
                                                    _rw_rs("DELETE FROM blank_allocations WHERE order_line_id=%(lid)s::uuid", {"lid": _lid})
                                                    _rw_rs("UPDATE job_master SET current_stage='JOB_CREATED', updated_at=NOW() WHERE order_line_id=%(lid)s::uuid", {"lid": _lid})
                                                    _rw_rs("INSERT INTO job_stage_events (id,job_id,stage_id,stage_code,remarks,created_at) SELECT gen_random_uuid(),jm.id,ms.id,'JOB_CREATED','Blank reassigned by operator',NOW() FROM job_master jm, job_stage_master ms WHERE jm.order_line_id=%(lid)s::uuid AND ms.stage_code='JOB_CREATED'", {"lid": _lid})
                                                    st.success("✅ Blank returned. Assign new blank below.")
                                                    st.session_state.pop(_reassign_key, None)
                                                    st.rerun()
                                            except Exception as _rs_e:
                                                st.error(f"Reassignment failed: {_rs_e}")
                            else:
                                st.markdown(
                                    f"<div style='background:#1a0a00;border:1px solid #f59e0b44;"
                                    f"border-radius:6px;padding:6px 12px;margin:4px 0;"
                                    f"font-size:0.75rem;color:#fbbf24'>"
                                    f"🔒 Blank allotment locked — stage: <b>{STAGE_LABEL.get(_stage, _stage)}</b>. "
                                    f"{'Surfacing complete — use Reject button.' if _blank_return_locked else 'Use Reject button to restart.'}</div>",
                                    unsafe_allow_html=True,
                                )
                        if _show_jc:
                            # Pre-load surfacing data from DB for prefill
                            _jc_lp = line.get("lens_params") or {}
                            if isinstance(_jc_lp, str):
                                try:
                                    import json as _jcj
                                    _jc_lp = _jcj.loads(_jc_lp)
                                except Exception as _e:
                                    _jc_lp = {}
                            _jc_surf_existing = _jc_lp.get("surfacing_data") or {}
                            _jc_allocated = bool(_jc_surf_existing.get("blank_id"))

                            with st.expander(
                                f"{'✅ Allocated — Reconfirm' if _jc_allocated else '📋 Assign Blank'} & Print Job Card",
                                expanded=st.session_state.get(f"jc_open_{_lid}", False)
                            ):
                                # Show allocation summary if already done
                                if _jc_allocated:
                                    st.markdown(
                                        f"<div style='background:#052e16;border:1px solid #16a34a;"
                                        f"border-radius:6px;padding:8px 14px;margin-bottom:8px'>"
                                        f"<span style='color:#4ade80;font-weight:700;font-size:0.82rem'>"
                                        f"✅ Blank Already Allocated</span><br>"
                                        f"<span style='color:#86efac;font-size:0.75rem'>"
                                        f"Brand: <b>{_jc_surf_existing.get('blank_brand','—')}</b> &nbsp;|&nbsp; "
                                        f"Material: <b>{_jc_surf_existing.get('blank_material','—')}</b> &nbsp;|&nbsp; "
                                        f"BC: <b>{_jc_surf_existing.get('base_curve','—')}</b> &nbsp;|&nbsp; "
                                        f"SPH: <b>{_jc_surf_existing.get('sph_surf','—')}</b> &nbsp;|&nbsp; "
                                        f"CYL: <b>{_jc_surf_existing.get('cyl_surf','—')}</b>"
                                        f"</span></div>",
                                        unsafe_allow_html=True
                                    )
                                    st.caption("Production staff: confirm details below and print to proceed.")

                                try:
                                    from modules.documents.job_card_surfacing import (
                                        render_surfacing_job_card,
                                        save_job_card_line,
                                        render_job_card_print_pair,
                                    )
                                    # Build minimal order dict for job card
                                    _jc_order = {
                                        "id":           _order_uuid,
                                        "order_no":     odata["order_no"],
                                        "patient_name": odata["patient_name"],
                                        "order_type":   "RETAIL",
                                    }
                                    # Build line dict — full fields for job card form
                                    import json as _jcjson
                                    _jc_lp_raw = line.get("lens_params") or {}
                                    if isinstance(_jc_lp_raw, str):
                                        try: _jc_lp_raw = _jcjson.loads(_jc_lp_raw)
                                        except Exception as e:
                                            log.debug("Could not parse job-card lens_params: %s", e)
                                            _jc_lp_raw = {}
                                    _jc_bp_raw = line.get("boxing_params") or {}
                                    if isinstance(_jc_bp_raw, str):
                                        try: _jc_bp_raw = _jcjson.loads(_jc_bp_raw)
                                        except Exception as e:
                                            log.debug("Could not parse job-card boxing_params: %s", e)
                                            _jc_bp_raw = {}
                                    _jc_line = dict(line)
                                    _jc_line["line_id"]        = _lid
                                    _jc_line["order_no"]       = odata["order_no"]
                                    _jc_line["surfacing_data"] = _jc_surf_existing
                                    _jc_line["lens_params"]    = _jc_lp_raw
                                    _jc_line["boxing_params"]  = _jc_bp_raw
                                    _jc_line["billing_qty"]    = int(line.get("billing_qty") or line.get("quantity") or 1)
                                    _jc_line["add_power"]      = float(line.get("add_power") or 0) or None
                                    # Pass lines list so _render_job_card_tab can find partner eye
                                    _jc_order["lines"] = lines

                                    # ── Show paired job card (both eyes) if BOTH have surfacing_data ──
                                    _partner_eye  = "L" if _eye in ("R","RIGHT") else "R"
                                    _partner_line = next(
                                        (l for l in lines
                                         if str(l.get("eye_side","")).upper()[:1] == _partner_eye),
                                        None
                                    )

                                    # Check if partner also has surfacing_data (was filled in backoffice)
                                    _partner_has_surf = False
                                    if _partner_line:
                                        _partner_lp = _partner_line.get("lens_params") or {}
                                        if isinstance(_partner_lp, str):
                                            try:
                                                import json as _pj
                                                _partner_lp = _pj.loads(_partner_lp)
                                            except Exception as _e:
                                                _partner_lp = {}
                                        _partner_surf = _partner_lp.get("surfacing_data") or {}
                                        _partner_has_surf = bool(_partner_surf.get("blank_id"))

                                    # ── Single eye form — behaviour depends on allocation state ──
                                    if _jc_allocated:
                                        # ── ALREADY ALLOCATED: no form, no save button ──
                                        # Just show print buttons — user cannot accidentally re-save
                                        st.success("✅ Job card saved — surfacing data recorded.")

                                        # ── Build R/L line dicts for printing ──
                                        import json as _pbjson2
                                        _pb_r_line = _jc_line if _eye in ("R","RIGHT") else _partner_line
                                        _pb_l_line = _jc_line if _eye in ("L","LEFT")  else _partner_line

                                        def _load_surf(ln, jc_ln):
                                            if ln is None: return None
                                            if ln is jc_ln: return ln
                                            _lp2 = ln.get("lens_params") or {}
                                            if isinstance(_lp2, str):
                                                try: _lp2 = _pbjson2.loads(_lp2)
                                                except Exception as e:
                                                    log.debug("Could not parse lens_params: %s", e)
                                                    _lp2 = {}
                                            _ld = dict(ln)
                                            _ld["surfacing_data"] = _ld.get("surfacing_data") or _lp2.get("surfacing_data") or {}
                                            return _ld

                                        _pb_r_line = _load_surf(_pb_r_line, _jc_line)
                                        _pb_l_line = _load_surf(_pb_l_line, _jc_line)

                                        _partner_surf_full = (_pb_l_line or _pb_r_line or {}).get("surfacing_data") or {} \
                                            if _eye in ("R","RIGHT") else \
                                            (_pb_r_line or {}).get("surfacing_data") or {}
                                        _can_print_both = bool(_partner_surf_full.get("blank_id")) or _partner_has_surf

                                        _key_pb  = f"do_print_both_{_lid}"
                                        _key_lbl = f"do_print_lbl_{_lid}"

                                        _pb1, _pb2 = st.columns(2)
                                        with _pb1:
                                            _btn_lbl = "🖨️ Print Both (R+L)" if _can_print_both else f"🖨️ Print {_eye} Card"
                                            if st.button(_btn_lbl, key=f"jc_print_both_{_lid}",
                                                         type="primary", use_container_width=True):
                                                st.session_state[_key_pb] = True
                                        with _pb2:
                                            if st.button("🏷️ Print Labels", key=f"jc_labels_{_lid}",
                                                         use_container_width=True):
                                                st.session_state[_key_lbl] = True

                                        if st.session_state.pop(_key_pb, False):
                                            try:
                                                from modules.backoffice.production_panel import _missing_blank_assignments_for_print
                                                from modules.documents.job_card_surfacing import _open_jc_print_window
                                                _pb_lines = [l for l in [_pb_r_line, _pb_l_line] if l]
                                                _missing = _missing_blank_assignments_for_print({
                                                    **_jc_order,
                                                    "lines": _pb_lines,
                                                })
                                                if _missing:
                                                    st.error(
                                                        "🔴 Assignment not done — assign blank first for "
                                                        f"{'/'.join(_missing)} eye before printing job card."
                                                    )
                                                else:
                                                    _open_jc_print_window(_pb_r_line, _pb_l_line, _jc_order)
                                            except Exception as _pbe:
                                                st.error(f"Print error: {_pbe}")

                                        if st.session_state.pop(_key_lbl, False):
                                            try:
                                                from modules.backoffice.production_panel import (
                                                    _build_label_page,
                                                    _missing_blank_assignments_for_print,
                                                    _open_print_window,
                                                )
                                                _lb_lines = [l for l in [_pb_r_line, _pb_l_line] if l]
                                                _lb_order = {
                                                    "id":           odata["order_id"],
                                                    "order_no":     odata["order_no"],
                                                    "patient_name": odata["patient_name"],
                                                    "party_name":   lines[0].get("party_name","") if lines else "",
                                                    "order_type":   lines[0].get("order_type","RETAIL") if lines else "RETAIL",
                                                }
                                                _missing = _missing_blank_assignments_for_print({
                                                    **_lb_order,
                                                    "lines": _lb_lines,
                                                })
                                                if _missing:
                                                    st.error(
                                                        "🔴 Assignment not done — assign blank first for "
                                                        f"{'/'.join(_missing)} eye before printing labels."
                                                    )
                                                else:
                                                    _open_print_window(_build_label_page(_lb_lines, _lb_order))
                                            except Exception as _lbe:
                                                st.error(f"Label error: {_lbe}")

                                    else:
                                        # ── NOT YET ALLOCATED: show blank selection form + save ──
                                        render_surfacing_job_card(_jc_line, _jc_order, show_buttons=False)
                                        _jc1, _jc2, _jc3 = st.columns(3)
                                        with _jc1:
                                            if st.button("💾 Save Job Card",
                                                         key=f"jc_save_{_lid}",
                                                         type="primary",
                                                         use_container_width=True):
                                                from modules.documents.job_card_surfacing import build_surfacing_data_from_session
                                                _sd = build_surfacing_data_from_session(_jc_line, _jc_order)
                                                if _sd:
                                                    _jc_line["surfacing_data"] = _sd
                                                else:
                                                    st.error("❌ Select a blank and fill the form first")
                                                    st.stop()
                                                _ok, _msg = save_job_card_line(_jc_line, _jc_order)
                                                if _ok:
                                                    st.session_state[f"jc_open_{_lid}"] = False
                                                    st.success("✅ Job card saved")
                                                    st.rerun()
                                                else:
                                                    st.error(f"❌ {_msg}")
                                    with _jc2:
                                        _partner_save_disabled = not bool(_partner_line)
                                        _show_save_both_here = _eye in ("R", "RIGHT")
                                        if not _show_save_both_here and _partner_line:
                                            st.caption("Use Save Both from R panel.")
                                        if _show_save_both_here and st.button("💾 Save Both R+L",
                                                                              key=f"jc_save_both_{_lid}",
                                                                              type="primary",
                                                                              use_container_width=True,
                                                                              disabled=_partner_save_disabled):
                                                from modules.documents.job_card_surfacing import build_surfacing_data_from_session

                                                def _prep_jc_line(_src):
                                                    import json as _pjl_json
                                                    _lp = _src.get("lens_params") or {}
                                                    if isinstance(_lp, str):
                                                        try: _lp = _pjl_json.loads(_lp)
                                                        except Exception: _lp = {}
                                                    _bp = _src.get("boxing_params") or {}
                                                    if isinstance(_bp, str):
                                                        try: _bp = _pjl_json.loads(_bp)
                                                        except Exception: _bp = {}
                                                    _out = dict(_src)
                                                    _out["line_id"] = str(_src.get("line_id") or _src.get("id") or "")
                                                    _out["order_no"] = odata["order_no"]
                                                    _out["lens_params"] = _lp
                                                    _out["boxing_params"] = _bp
                                                    _out["billing_qty"] = int(_src.get("billing_qty") or _src.get("quantity") or 1)
                                                    _out["add_power"] = float(_src.get("add_power") or 0) or None
                                                    return _out

                                                _both_lines = [_jc_line, _prep_jc_line(_partner_line)]
                                                _errors = []
                                                _saved = []
                                                for _bl in sorted(
                                                    _both_lines,
                                                    key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1
                                                ):
                                                    _sd = build_surfacing_data_from_session(_bl, _jc_order)
                                                    if not _sd:
                                                        _errors.append(f"{_bl.get('eye_side','?')} — select blank and fill form")
                                                        continue
                                                    _bl["surfacing_data"] = _sd
                                                    _ok, _msg = save_job_card_line(_bl, _jc_order)
                                                    if _ok:
                                                        _saved.append(str(_bl.get("eye_side","")).upper()[:1] or "?")
                                                    else:
                                                        _errors.append(_msg)
                                                if _errors:
                                                    st.error("❌ " + " | ".join(_errors))
                                                else:
                                                    st.session_state[f"jc_open_{_lid}"] = False
                                                    st.success(f"✅ Saved both eyes ({'+'.join(_saved)})")
                                                    st.rerun()
                                        with _jc3:
                                            st.button(
                                                "🖨 Print Job Card",
                                                key=f"jc_print_{_lid}",
                                                use_container_width=True,
                                                disabled=True,
                                                help="Save blank assignment first. Print opens after allocation is written to DB.",
                                            )
                                            st.caption("Print locked until blank assignment is saved.")
                                except Exception as _jce:
                                    st.error(f"Job card error: {_jce}")
                        elif _stage not in ("JOB_CREATED",) and _jid:
                            # Show compact job card summary for stages past JOB_CREATED
                            # surfacing_data is nested inside lens_params — not top-level
                            try:
                                import json as _jcs
                                _lp_summ = line.get("lens_params") or {}
                                if isinstance(_lp_summ, str):
                                    try: _lp_summ = _jcs.loads(_lp_summ)
                                    except Exception as e:
                                        log.debug("Could not parse summary lens_params: %s", e)
                                        _lp_summ = {}
                                _surf = _lp_summ.get("surfacing_data") or {}
                            except Exception as _e:
                                _surf = {}
                            if isinstance(_surf, dict) and (_surf.get("blank_brand") or _surf.get("blank_id")):
                                _sph_d  = f"{float(_surf.get('sph_surf',0)):+.2f}" if _surf.get("sph_surf") is not None else "—"
                                _cyl_d  = f"{float(_surf.get('cyl_surf',0)):+.2f}" if _surf.get("cyl_surf") is not None else "—"
                                _ax_d   = f"{int(_surf.get('axis_surf',0))}°" if _surf.get("axis_surf") is not None else "—"
                                st.markdown(
                                    f"<div style='font-size:0.75rem;color:#64748b;"
                                    f"padding:4px 10px;border-left:3px solid #334155;margin:4px 0;"
                                    f"background:#0f172a;border-radius:0 4px 4px 0'>"
                                    f"📋 <b style='color:#94a3b8'>{_surf.get('blank_brand','')}"
                                    f" {_surf.get('blank_material','')}</b>"
                                    f" &nbsp;BC:<b>{_surf.get('base_curve','—')}</b>"
                                    f" &nbsp;SPH:<b>{_sph_d}</b>"
                                    f" CYL:<b>{_cyl_d}</b>"
                                    f" AX:<b>{_ax_d}</b>"
                                    f" &nbsp;Tool A:<b>{_surf.get('tool_a','—')}</b>"
                                    f" B:<b>{_surf.get('tool_b','—')}</b>"
                                    f"</div>",
                                    unsafe_allow_html=True
                                )

                        # ── Post-rejection: blank re-select prompt ─────────
                        # Shown when this line is REJECTED (just rejected or was already rejected).
                        # Prompts production staff to select a new blank to restart the pipeline.
                        if _stage == "REJECTED" or st.session_state.get(f"ih_rej_done_{_lid}"):
                            _eye_accent_rej = "#ef4444" if _is_re else "#94a3b8"
                            _eye_lbl_rej    = "RIGHT" if _is_re else ("LEFT" if _is_le else _eye)
                            st.markdown(
                                f"<div style='background:#1a0000;border:1px solid #ef444455;"
                                f"border-left:4px solid #ef4444;"
                                f"border-radius:6px;padding:12px 16px;margin:8px 0'>"
                                f"<div style='color:#ef4444;font-weight:700;font-size:0.85rem;margin-bottom:6px'>"
                                f"🔄 {_eye_lbl_rej} Eye — Select New Blank to Restart</div>"
                                f"<div style='color:#94a3b8;font-size:0.75rem'>"
                                f"This job was rejected. Choose a replacement blank below "
                                f"to reset the pipeline back to <b style='color:#3b82f6'>JOB_CREATED</b> "
                                f"and begin production again.</div>"
                                f"</div>",
                                unsafe_allow_html=True
                            )

                            # Load blank inventory for this eye / material
                            _blank_opts = []
                            try:
                                from modules.sql_adapter import run_query as _rq_bl
                                import json as _jbl
                                _lp_bl = line.get("lens_params") or {}
                                if isinstance(_lp_bl, str):
                                    try: _lp_bl = _jbl.loads(_lp_bl)
                                    except Exception as e:
                                        log.debug("Could not parse blank lens_params: %s", e)
                                        _lp_bl = {}
                                _eye_filter = _eye[:1] if _eye[:1] in ("R","L") else None
                                _blank_rows = _rq_bl("""
                                    SELECT
                                        id::text              AS blank_id,
                                        brand                 AS blank_brand,
                                        material              AS blank_material,
                                        COALESCE(base_recommended::text, base_1::text, '') AS base_curve,
                                        item_code             AS index_value,
                                        batch_no,
                                        COALESCE(qty_right, 0) + COALESCE(qty_left, 0)
                                            + COALESCE(qty_independent, 0) AS qty,
                                        qty_right,
                                        qty_left,
                                        qty_independent
                                    FROM blank_inventory
                                    WHERE COALESCE(is_active, TRUE) = TRUE
                                      AND (
                                        COALESCE(qty_right, 0) + COALESCE(qty_left, 0)
                                        + COALESCE(qty_independent, 0)
                                      ) > 0
                                    ORDER BY brand, material, base_recommended
                                    LIMIT 100
                                """, {}) or []
                                _blank_opts = _blank_rows
                            except Exception as _e:
                                _blank_opts = []

                            if _blank_opts:
                                _bl_ids   = [r["blank_id"] for r in _blank_opts]
                                _eye_key  = "qty_left" if _eye[:1] == "L" else "qty_right"
                                _bl_lbls  = {
                                    r["blank_id"]: (
                                        f"{r.get('blank_brand','')} {r.get('blank_material','')} "
                                        f"BC:{r.get('base_curve','')} "
                                        f"— {r.get(_eye_key, r.get('qty', 0))} pcs ({_eye[:1]} eye)"
                                    ).strip()
                                    for r in _blank_opts
                                }
                                _bl_sel = st.selectbox(
                                    "Select replacement blank",
                                    ["— Choose blank —"] + _bl_ids,
                                    format_func=lambda x: "— Choose blank —" if x == "— Choose blank —" else _bl_lbls.get(x, x),
                                    key=f"ih_rej_blank_{_lid}",
                                    label_visibility="collapsed"
                                )

                                _rej_restart_disabled = (_bl_sel == "— Choose blank —")
                                _rb1, _rb2 = st.columns([3, 2])
                                with _rb1:
                                    if st.button(
                                        "🔄 Restart Pipeline with Selected Blank",
                                        key=f"ih_rej_restart_{_lid}",
                                        type="primary",
                                        use_container_width=True,
                                        disabled=_rej_restart_disabled
                                    ):
                                        try:
                                            from modules.sql_adapter import run_write as _rw_restart
                                            _sel_blank_row = next(
                                                (r for r in _blank_opts if r["blank_id"] == _bl_sel), {}
                                            )
                                            # 1. Reset job_master back to JOB_CREATED
                                            _rw_restart("""
                                                UPDATE job_master
                                                   SET current_stage   = 'JOB_CREATED',
                                                       is_closed       = FALSE,
                                                       reprocess_count = COALESCE(reprocess_count,0) + 1,
                                                       updated_at      = NOW()
                                                 WHERE id = %(jid)s::uuid
                                            """, {"jid": _jid})
                                            # 2. Log the restart event
                                            try:
                                                _rw_restart("""
                                                    INSERT INTO job_stage_events
                                                        (id, job_id, stage_id, stage_code, remarks, created_at)
                                                    VALUES (gen_random_uuid(), %(jid)s::uuid, gen_random_uuid(),
                                                            'JOB_CREATED', %(rmk)s, NOW())
                                                """, {
                                                    "jid": _jid,
                                                    "rmk": (
                                                        f"Pipeline restarted after rejection. "
                                                        f"New blank: {_sel_blank_row.get('blank_brand','')} "
                                                        f"{_sel_blank_row.get('blank_material','')} "
                                                        f"BC:{_sel_blank_row.get('base_curve','')}"
                                                    )
                                                })
                                            except Exception as _e:
                                                log.warning("Restart stage event insert failed: %s", _e)
                                            # 3. Write new blank selection into lens_params
                                            try:
                                                import json as _jrestart
                                                _lp_restart = line.get("lens_params") or {}
                                                if isinstance(_lp_restart, str):
                                                    try: _lp_restart = _jrestart.loads(_lp_restart)
                                                    except Exception as e:
                                                        log.debug("Could not parse restart lens_params: %s", e)
                                                        _lp_restart = {}
                                                _surf_restart = _lp_restart.get("surfacing_data") or {}
                                                _surf_restart.update({
                                                    "blank_id":       _bl_sel,
                                                    "blank_brand":    _sel_blank_row.get("blank_brand",""),
                                                    "blank_material": _sel_blank_row.get("blank_material",""),
                                                    "base_curve":     _sel_blank_row.get("base_curve",""),
                                                    "diameter":       _sel_blank_row.get("diameter",""),
                                                })
                                                _lp_restart["surfacing_data"] = _surf_restart
                                                _rw_restart("""
                                                    UPDATE order_lines
                                                       SET lens_params = %(lp)s::jsonb
                                                     WHERE id = %(lid)s::uuid
                                                """, {
                                                    "lp":  _jrestart.dumps(_lp_restart),
                                                    "lid": _lid
                                                })
                                            except Exception: pass
                                            # Clear session state flags
                                            st.session_state.pop(f"ih_rej_done_{_lid}", None)
                                            st.success(
                                                f"✅ Pipeline reset! {_eye_lbl_rej} eye restarted with "
                                                f"{_sel_blank_row.get('blank_brand','')} "
                                                f"{_sel_blank_row.get('blank_material','')} blank."
                                            )
                                            # ── WhatsApp rejection notice to customer ──────────────
                                            try:
                                                import urllib.parse as _rej_up
                                                _cust_mob = str(info.get("mobile") or "").strip()
                                                _rej_digits = "".join(d for d in _cust_mob if d.isdigit())
                                                if _rej_digits.startswith("91") and len(_rej_digits)==12:
                                                    _rej_digits = _rej_digits[2:]
                                                _rej_wa_num = f"91{_rej_digits}" if len(_rej_digits)==10 else ""
                                                _prod_name_rej = str(line.get("product_name","")).split(" | ")[0]
                                                _coat_rej = str(
                                                    line.get("coating_type") or line.get("coating") or ""
                                                ).strip()
                                                # Estimate reprocess time: +2 working days
                                                import datetime as _rej_dt
                                                _rej_eta = (_rej_dt.date.today() + _rej_dt.timedelta(days=2)).strftime("%d-%b-%Y")
                                                _rej_wa_msg = (
                                                    "Dear " + info.get("patient_name","Customer") + ",\n\n"
                                                    "We regret to inform you that for Order "
                                                    + info["order_no"] + ", "
                                                    + "the " + _eye_lbl_rej + " lens ("
                                                    + _prod_name_rej
                                                    + (" \u00b7 " + _coat_rej if _coat_rej else "") + ") "
                                                    + "was found to be defective during quality check "
                                                    + "and has been taken for reprocessing.\n\n"
                                                    + "Expected completion: " + _rej_eta + "\n\n"
                                                    + "We apologise for the inconvenience.\n"
                                                    + "\u2014 Parakh Optical"
                                                )
                                                if _rej_wa_num:
                                                    st.link_button(
                                                        "📲 Send Rejection WhatsApp to Customer",
                                                        f"https://wa.me/{_rej_wa_num}?text={_rej_up.quote(_rej_wa_msg)}",
                                                        use_container_width=True,
                                                    )
                                                else:
                                                    st.code(_rej_wa_msg, language=None)
                                            except Exception as _e:
                                                pass
                                            st.rerun()
                                        except Exception as _re_restart:
                                            st.error(f"Restart failed: {_re_restart}")
                                with _rb2:
                                    st.markdown(
                                        f"<div style='background:#0f172a;border:1px solid #1e293b;"
                                        f"border-radius:6px;padding:8px 12px;text-align:center'>"
                                        f"<div style='color:#64748b;font-size:0.65rem;margin-bottom:4px'>"
                                        f"SELECTED</div>"
                                        f"<div style='color:#e2e8f0;font-size:0.75rem;font-weight:700'>"
                                        f"{'— choose above —' if _rej_restart_disabled else _bl_lbls.get(_bl_sel,'?')}"
                                        f"</div></div>",
                                        unsafe_allow_html=True
                                    )
                            else:
                                st.warning(
                                    "⚠️ No blanks in inventory. Add stock to blank_inventory "
                                    "before restarting production."
                                )

                        st.markdown("")  # spacing

                # ── Group advance controls (both eyes together) ───────
                st.markdown("---")
                # Only include lines that can still advance. READY_TO_BILL is a valid
                # final stage, not rejected/delivered. Keep separate terminal buckets so
                # the footer message is accurate and the operator knows what to do next.
                # FITTING_DONE is NOT terminal — it must advance through READY_FOR_PACK
                # to READY_TO_BILL before billing opens. Treating it as terminal hides
                # the advance button and strands the order at fitting.
                _TERMINAL_OK_STAGES  = {"READY_TO_BILL", "READY_FOR_BILLING"}
                _TERMINAL_BAD_STAGES = {"REJECTED", "DELIVERED", "CANCELLED"}
                _rl_advanceable = [
                    l for l in _rl_lines
                    if (l.get("lab_stage") or "JOB_CREATED") not in (_TERMINAL_OK_STAGES | _TERMINAL_BAD_STAGES)
                ]
                _rl_ready_to_bill = [
                    l for l in _rl_lines
                    if (l.get("lab_stage") or "").upper() in _TERMINAL_OK_STAGES
                ]
                _rl_bad_terminal = [
                    l for l in _rl_lines
                    if (l.get("lab_stage") or "").upper() in _TERMINAL_BAD_STAGES
                ]
                _grp_stages  = [normalize_stage_alias(l.get("lab_stage") or "JOB_CREATED") for l in _rl_advanceable] if _rl_advanceable else []
                _grp_all_stages = [normalize_stage_alias(l.get("lab_stage") or "JOB_CREATED") for l in _rl_lines]
                _grp_min_idx = min(STAGE_IDX.get(s,0) for s in _grp_stages) if _grp_stages else 0
                _grp_max_idx = max(STAGE_IDX.get(s,0) for s in _grp_stages) if _grp_stages else 0
                _grp_display_idx = min(STAGE_IDX.get(s,0) for s in _grp_all_stages) if _grp_all_stages else 0
                _grp_lbl     = STAGE_LABEL.get(STAGES[_grp_display_idx][0], STAGES[_grp_display_idx][0])
                _grp_eyes    = "+".join(str(l.get("eye_side","")).upper() for l in _rl_lines)
                _rl_eye_codes = [
                    "R" if str(l.get("eye_side","")).upper() in ("R", "RIGHT") else "L"
                    for l in _rl_lines
                ]
                _has_two_eye_pair = len(_rl_lines) == 2 and set(_rl_eye_codes) == {"R", "L"}
                _same_active_stage = bool(_grp_stages) and len(set(_grp_stages)) == 1
                _can_advance_both = (
                    _has_two_eye_pair
                    and len(_rl_advanceable) == 2
                    and _same_active_stage
                )

                # Use _next_stage_ih (product-aware, coating-aware) instead of static STAGES[idx+1].
                # Static indexing gives wrong next stage: HARDCOAT_DONE → COLOURING_PICKED (wrong!)
                # because the STAGES list stores all stages alphabetically/historically, not per-product flow.
                _grp_next = None
                if _can_advance_both:
                    # Pick the line at the latest stage (highest idx) — advance it next
                    _grp_lead_line = max(
                        _rl_advanceable,
                        key=lambda _gl: STAGE_IDX.get(normalize_stage_alias(_gl.get("lab_stage") or "JOB_CREATED"), 0)
                    )
                    _grp_lead_stage = normalize_stage_alias(_grp_lead_line.get("lab_stage") or "JOB_CREATED")
                    _grp_lead_jid   = str(_grp_lead_line.get("job_id") or "")
                    _grp_lead_evs   = _line_stage_events_ih(
                        _grp_lead_line,
                        _stage_events_by_job.get(_grp_lead_jid, []),
                    )
                    _grp_next = _next_stage_ih(_grp_lead_line, _grp_lead_stage, _grp_lead_evs, _oid)

                # Check billing lock at group level
                _grp_locked = False
                _grp_lock_label = ""
                try:
                    from modules.sql_adapter import run_query as _rq_gl
                    _gl_lids = [str(l["line_id"]) for l in _rl_lines if l.get("line_id")]
                    if _gl_lids:
                        _gl_chk = _rq_gl("""
                            SELECT
                                COUNT(DISTINCT cl.order_line_id) AS n,
                                MAX(CASE WHEN i.id IS NOT NULL THEN 1 ELSE 0 END) AS invoiced
                            FROM challan_lines cl
                            JOIN challans c ON c.id = cl.challan_id
                            LEFT JOIN invoices i ON i.challan_id = c.id
                                AND i.status NOT IN ('CANCELLED','VOID')
                            WHERE cl.order_line_id = ANY(%(lids)s::uuid[])
                              AND c.status NOT IN ('CANCELLED','VOID')
                              AND COALESCE(c.is_deleted, FALSE) = FALSE
                        """, {"lids": _gl_lids})
                        if _gl_chk and int(_gl_chk[0].get("n") or 0) > 0:
                            _grp_locked = True
                            _grp_lock_label = "🧾 INVOICED" if int(_gl_chk[0].get("invoiced") or 0) else "📋 CHALLANED"
                except Exception as _e:
                    pass

                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
                    f"padding:8px 14px;margin:4px 0;display:flex;align-items:center;gap:8px'>"
                    f"<span style='font-size:0.68rem;color:#64748b;text-transform:uppercase;"
                    f"letter-spacing:.06em'>{_grp_eyes} stage</span>"
                    f"<span style='background:{_stg_clr(STAGES[_grp_display_idx][0])}22;"
                    f"color:{_stg_clr(STAGES[_grp_display_idx][0])};font-size:0.72rem;font-weight:700;"
                    f"padding:2px 10px;border-radius:10px'>{_grp_lbl}</span>"
                    + (f"<span style='background:#052e16;color:#4ade80;font-size:0.68rem;font-weight:700;"
                       f"padding:2px 8px;border-radius:6px;margin-left:auto'>{_grp_lock_label}</span>"
                       if _grp_locked else "")
                    + f"</div>",
                    unsafe_allow_html=True
                )
                _gc1, _gc2 = st.columns([3, 3])
                with _gc1:
                    if not _rl_lines:
                        st.caption("Service production line — use the single-line advancement controls.")
                    elif len(_rl_lines) == 1:
                        st.caption("Single-eye order — use individual eye advancement.")
                    elif not _has_two_eye_pair:
                        st.caption("Use individual line advancement for this production group.")
                    elif _grp_stages and not _same_active_stage:
                        st.warning("R and L are at different stages. Use individual advancement until both match.")
                    elif not _rl_advanceable:
                        if _grp_locked:
                            st.caption(f"🔒 Locked — {_grp_lock_label}")
                        elif _rl_ready_to_bill and len(_rl_ready_to_bill) == len(_rl_lines):
                            st.success("✅ R+L ready to bill — use 💰 Billing / Backoffice Billing Gate")
                            if st.button(
                                "💰 Open Billing Gate",
                                key=f"ih_gbill_{_oid}",
                                type="primary",
                                use_container_width=True,
                            ):
                                _go_to_billing(
                                    str(odata.get("order_id") or "").split(":", 1)[0],
                                    odata.get("parent_order_no") or odata.get("order_no") or _oid,
                                )
                        elif _rl_bad_terminal and len(_rl_bad_terminal) == len(_rl_lines):
                            st.info("🚫 All lines rejected or delivered")
                        else:
                            st.success("✅ All at final stage")
                    elif _grp_locked:
                        st.caption(f"🔒 Locked — {_grp_lock_label}")
                    elif _rl_advanceable and not _can_advance_both:
                        st.warning("Advance Both is available only when R and L are both active at the same stage.")
                    elif _grp_next:
                        if st.button(
                            f"▶ Advance Both → {_grp_next[1]}",
                            key=f"ih_gadv_{_oid}", type="primary", use_container_width=True
                        ):
                            try:
                                for _al in _rl_advanceable:
                                    _save_stage_ih(str(_al["job_id"]), _grp_next[0])
                                st.rerun()
                            except Exception as _ge: st.error(str(_ge))
                    else:
                        st.success("✅ All at final stage")
                with _gc2:
                    _grp_prev = []
                    if _can_advance_both:
                        _grp_prev = _prev_stages_ih(
                            _grp_lead_line,
                            _grp_lead_stage,
                            _grp_lead_evs,
                            _oid,
                        )
                    if _grp_prev and _can_advance_both and not _grp_locked:
                        _gr_lbls  = ["◀ Set both back to..."] + [s[1] for s in _grp_prev]
                        _gr_codes = [None] + [s[0] for s in _grp_prev]
                        _gr_sel   = st.selectbox("Recede both", _gr_lbls,
                                                  key=f"ih_grec_{_oid}",
                                                  label_visibility="collapsed")
                        _gr_code  = _gr_codes[_gr_lbls.index(_gr_sel)]
                        if _gr_code:
                            if st.button("◀ Apply to Both", key=f"ih_grec_btn_{_oid}",
                                         use_container_width=True):
                                try:
                                    for _al in _rl_advanceable:
                                        _save_stage_ih(str(_al["job_id"]), _gr_code)
                                    st.rerun()
                                except Exception as _re3: st.error(str(_re3))
                    elif _grp_locked:
                        st.caption("🔒 Cannot reverse after billing")


# ══════════════════════════════════════════════════════════════════════
# UNIVERSAL ORDER ASSIGNMENT WORKSPACE
# ══════════════════════════════════════════════════════════════════════

def _render_assignment_workspace(order_no: str) -> None:
    """
    Universal R+L Order Assignment Workspace.

    Connects to:
      • assignment_panel  — route classification, ranked suppliers, DB write
      • job_card_surfacing — blank selection UI (INHOUSE), save_job_card_line
      • production_panel  — CR80 + label print functions

    One screen per order. Single Save button. All lines save together.
    """
    import json as _aw_json
    import urllib.parse as _aw_uparse

    # ═══════════════════════════════════════════════════════════════════
    # LOCAL DB HELPERS
    # ═══════════════════════════════════════════════════════════════════

    def _aw_q(sql, params=None):
        try:
            from modules.sql_adapter import run_query
            return run_query(sql, params or {}) or []
        except Exception as _e:
            st.error(f"DB error: {_e}")
            return []

    def _aw_w(sql, params=None):
        try:
            from modules.sql_adapter import run_write
            run_write(sql, params or {})
            return True
        except Exception as _e:
            st.error(f"Save error: {_e}")
            return False

    def _aw_write_lp(line_id: str, lp_dict: dict) -> bool:
        """
        Fetch current lens_params from DB, merge updates, write back.
        Same pattern as assignment_panel._apply_all_assignments() —
        preserves all existing keys not explicitly overwritten.
        """
        try:
            import json as _j
            from modules.sql_adapter import run_query as _rq, run_write as _rw
            _row = _rq(
                "SELECT COALESCE(lens_params,'{}')::text AS lp "
                "FROM order_lines WHERE id=%(lid)s::uuid LIMIT 1",
                {"lid": line_id}
            )
            _existing = {}
            if _row:
                try: _existing = _j.loads(_row[0]["lp"]) if _row[0]["lp"] else {}
                except Exception as e:
                    log.debug("Could not parse existing payload: %s", e)
                    _existing = {}
            _route_new = str(lp_dict.get("manufacturing_route") or "").upper()
            _released_stock = False
            if _route_new and _route_new != "STOCK":
                _old_bid = str(_existing.get("stock_id") or _existing.get("batch_id") or "").strip()
                _old_qty = int(_existing.get("stock_qty") or _existing.get("allocated_qty") or 0)
                if _old_bid and _old_qty > 0 and str(_existing.get("batch_status","")).upper() == "ALLOCATED":
                    _rw(
                        "UPDATE inventory_stock "
                        "SET allocated_qty = GREATEST(0, COALESCE(allocated_qty,0) - %(qty)s), "
                        "    updated_at = NOW() "
                        "WHERE id = %(bid)s::uuid",
                        {"qty": _old_qty, "bid": _old_bid}
                    )
                    _released_stock = True
            _existing.update(lp_dict)
            if _released_stock:
                for _k in ("stock_id", "batch_id", "stock_qty", "batch_no", "batch_status"):
                    _existing.pop(_k, None)
                _rw(
                    "UPDATE order_lines SET lens_params=%(lp)s::jsonb, "
                    "allocated_qty = 0, ready_qty = 0 "
                    "WHERE id=%(lid)s::uuid",
                    {"lp": _j.dumps(_existing), "lid": line_id}
                )
            else:
                _rw(
                    "UPDATE order_lines SET lens_params=%(lp)s::jsonb "
                    "WHERE id=%(lid)s::uuid",
                    {"lp": _j.dumps(_existing), "lid": line_id}
                )
            return True
        except Exception as _we:
            st.error(f"Write error: {_we}")
            return False

    def _aw_load_lp(line: dict) -> dict:
        if not line:
            return {}
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            try: lp = _aw_json.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}
        return lp if isinstance(lp, dict) else {}

    def _aw_save_stock_assignment_atomic(
        line_id: str,
        patch: dict,
        qty: int,
    ) -> tuple:
        """
        ONE DB TRANSACTION covering:
          1. SELECT ... FOR UPDATE on order_lines row  (row lock, prevents races)
          2. Read old stock_id + old stock_qty from current lens_params
          3. Idempotent: same stock_id + same qty → just re-write lens_params, return
          4. Reserve new batch:
               UPDATE inventory_stock
               SET allocated_qty = allocated_qty + qty
               WHERE id = new_stock_id
                 AND COALESCE(quantity, 0) - COALESCE(allocated_qty, 0) >= qty
               RETURNING id
             If no row returned → ROLLBACK → return (False, error)
          5. Release old batch (only AFTER new deduction confirmed)
          6. UPDATE order_lines SET lens_params = merged_patch   (includes stock_qty)
          7. COMMIT

        patch must already contain: manufacturing_route, stock_id, batch_no, batch_status
        stock_qty = qty is added here before writing.

        Returns (True, "") on success, (False, human_readable_error) on failure.
        """
        import json as _aj

        new_bid     = str(patch.get("stock_id","")).strip()
        new_bno     = str(patch.get("batch_no","")).strip()
        if not new_bid:
            return False, "STOCK line missing stock_id — cannot save."

        try:
            from modules.sql_adapter import get_connection as _gc
        except ImportError:
            return False, "get_connection not available — upgrade sql_adapter."

        try:
            conn = _gc()
            conn.autocommit = False
        except Exception as _ce:
            return False, f"DB connection error: {_ce}"

        try:
            with conn.cursor() as cur:

                # ── 1. Lock the order_line row ───────────────────────
                cur.execute(
                    "SELECT COALESCE(lens_params,'{}')::text AS lp "
                    "FROM order_lines WHERE id = %s::uuid FOR UPDATE",
                    (line_id,)
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return False, f"Order line {line_id} not found."

                # ── 2. Read old stock_id + old stock_qty ─────────────
                try:
                    _existing_lp = _aj.loads(row[0]) if row[0] else {}
                except Exception as _e:
                    _existing_lp = {}

                old_bid = str(_existing_lp.get("stock_id","")).strip()
                old_qty = int(_existing_lp.get("stock_qty", qty) or qty)

                # ── 3. Idempotent — same batch, same qty, already ALLOCATED ──────
                # Only skip deduction if this exact batch is already allocated for
                # this exact qty. If qty changed (re-allotment), fall through to
                # deduct-the-difference path below.
                _already_allocated = (
                    old_bid and old_bid == new_bid
                    and old_qty == qty
                    and str(_existing_lp.get("batch_status","")).upper() == "ALLOCATED"
                )
                if _already_allocated:
                    # Nothing to deduct; just make sure lens_params is current
                    _merged = dict(_existing_lp)
                    _merged.update(patch)
                    _merged["stock_qty"] = qty
                    if str(_merged.get("replenishment_status", "")).upper() not in ("ORDERED", "DISCARDED"):
                        _merged["replenishment_status"] = "PENDING"
                    cur.execute(
                        "UPDATE order_lines SET lens_params = %s::jsonb, "
                        "allocated_qty = %s, ready_qty = %s, status = 'READY' "
                        "WHERE id = %s::uuid",
                        (_aj.dumps(_merged), qty, qty, line_id)
                    )
                    conn.commit()
                    return True, "already_allocated"

                # Same batch, quantity changed: reserve/release only the delta.
                if old_bid and old_bid == new_bid and old_qty != qty:
                    _delta = qty - old_qty
                    if _delta > 0:
                        cur.execute(
                            "UPDATE inventory_stock "
                            "SET allocated_qty = COALESCE(allocated_qty, 0) + %s, "
                            "    updated_at = NOW() "
                            "WHERE id = %s::uuid "
                            "  AND GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) >= %s "
                            "RETURNING id",
                            (_delta, new_bid, _delta)
                        )
                        if not cur.fetchone():
                            conn.rollback()
                            return (
                                False,
                                f"Stock not available — need extra {_delta}, current free stock is lower. "
                                "Assignment not saved."
                            )
                    elif _delta < 0:
                        cur.execute(
                            "UPDATE inventory_stock "
                            "SET allocated_qty = GREATEST(0, COALESCE(allocated_qty, 0) - %s), "
                            "    updated_at = NOW() "
                            "WHERE id = %s::uuid",
                            (abs(_delta), new_bid)
                        )

                    _merged = dict(_existing_lp)
                    _merged.update(patch)
                    _merged["stock_qty"] = qty
                    if str(_merged.get("replenishment_status", "")).upper() not in ("ORDERED", "DISCARDED"):
                        _merged["replenishment_status"] = "PENDING"
                    cur.execute(
                        "UPDATE order_lines SET lens_params = %s::jsonb, "
                        "allocated_qty = %s, ready_qty = %s, status = 'READY' "
                        "WHERE id = %s::uuid",
                        (_aj.dumps(_merged), qty, qty, line_id)
                    )
                    conn.commit()
                    return True, "qty_adjusted"

                # ── 4. Reserve new batch (safe guard — RETURNING) ─────
                # Physical quantity is deducted only at dispatch. At assignment
                # time we block stock by increasing allocated_qty, so punching
                # and backoffice availability read quantity - allocated_qty.
                cur.execute(
                    "UPDATE inventory_stock "
                    "SET allocated_qty = COALESCE(allocated_qty, 0) + %s, "
                    "    updated_at = NOW() "
                    "WHERE id = %s::uuid "
                    "  AND GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) >= %s "
                    "RETURNING id, quantity, allocated_qty",
                    (qty, new_bid, qty)
                )
                reserve_row = cur.fetchone()

                if not reserve_row:
                    conn.rollback()
                    # Fetch current availability for a clear message (outside the tx)
                    cur2_avail = 0
                    try:
                        cur.execute(
                            "SELECT GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) AS av "
                            "FROM inventory_stock WHERE id = %s::uuid",
                            (new_bid,)
                        )
                        _av_row = cur.fetchone()
                        if _av_row:
                            cur2_avail = int(_av_row[0] or 0)
                    except Exception as _e:
                        pass
                    return (
                        False,
                        f"Stock not available — need {qty}, only {cur2_avail} left. "
                        "Assignment not saved."
                    )

                # ── 5. Release old batch — ONLY after new deduction confirmed ──
                if old_bid and old_bid != new_bid and old_qty > 0:
                    # Switching to a different batch: release old reservation
                    cur.execute(
                        "UPDATE inventory_stock "
                        "SET allocated_qty = GREATEST(0, COALESCE(allocated_qty, 0) - %s), "
                        "    updated_at = NOW() "
                        "WHERE id = %s::uuid",
                        (old_qty, old_bid)
                    )
                # ── 6. Update order_lines lens_params ───────────────
                _merged = dict(_existing_lp)
                _merged.update(patch)
                _merged["stock_qty"] = qty   # store for future re-save / qty change
                if str(_merged.get("replenishment_status", "")).upper() not in ("ORDERED", "DISCARDED"):
                    _merged["replenishment_status"] = "PENDING"

                cur.execute(
                    "UPDATE order_lines SET lens_params = %s::jsonb, "
                    "allocated_qty = %s, ready_qty = %s, status = 'READY' "
                    "WHERE id = %s::uuid",
                    (_aj.dumps(_merged), qty, qty, line_id)
                )

            # ── 7. Commit ────────────────────────────────────────────
            conn.commit()
            return True, ""

        except Exception as _tx_err:
            try:
                conn.rollback()
            except Exception as _e:
                pass
            return False, f"Stock assignment transaction failed: {_tx_err}"

        finally:
            try:
                conn.autocommit = True
            except Exception as _e:
                pass
            try:
                conn.close()
            except Exception as _e:
                pass

    # ═══════════════════════════════════════════════════════════════════
    # IMPORT HELPERS FROM SUPPORTING MODULES
    # ═══════════════════════════════════════════════════════════════════

    # assignment_panel helpers
    _ap_get_routes     = None
    _ap_ranked_sups    = None
    _ap_is_ophthalmic  = None
    _ap_is_frame       = None
    _ap_is_inhouse_brand = None
    _ap_has_stock      = None
    _ap_is_stock_alloc = None
    _ap_upsert_jm      = None
    try:
        from modules.backoffice.assignment_panel import (
            _get_job_card_routes_for_line   as _ap_get_routes,
            _get_ranked_suppliers_for_product as _ap_ranked_sups,
            _is_ophthalmic_lens              as _ap_is_ophthalmic,
            _is_frame_line                   as _ap_is_frame,
            _is_inhouse_lab_brand            as _ap_is_inhouse_brand,
            _has_stock_available             as _ap_has_stock,
            _is_stock_allocated              as _ap_is_stock_alloc,
        )
    except Exception as _e:
        pass
    try:
        from modules.documents.job_card_surfacing import _upsert_job_master as _ap_upsert_jm
    except Exception as _e:
        pass

    # job_card_surfacing — surfacing UI + save
    _jcs_render  = None
    _jcs_build   = None
    _jcs_save    = None
    try:
        from modules.documents.job_card_surfacing import (
            render_surfacing_job_card       as _jcs_render,
            build_surfacing_data_from_session as _jcs_build,
            save_job_card_line              as _jcs_save,
        )
    except Exception as _e:
        pass

    # production_panel — print functions
    _pp_cr80    = None
    _pp_label   = None
    _pp_build_cr80 = None
    _pp_build_label = None
    _pp_open_print = None
    try:
        from modules.backoffice.production_panel import (
            _render_cr80_print_html   as _pp_cr80,
            _render_label_print_html  as _pp_label,
            _build_cr80_page          as _pp_build_cr80,
            _build_label_page         as _pp_build_label,
            _open_print_window        as _pp_open_print,
        )
    except Exception as _e:
        pass

    # ═══════════════════════════════════════════════════════════════════
    # BACK BUTTON + ORDER LOAD
    # ═══════════════════════════════════════════════════════════════════

    _bk_col, _hd_col = st.columns([1, 7])
    with _bk_col:
        if st.button("← Back", key="aw_back_btn", use_container_width=True):
            st.session_state["prod_view_mode"] = "list"
            st.session_state["prod_assign_order_no"] = None
            st.rerun()

    order = _fetch_order_for_panel(order_no)
    if not order:
        st.error(f"Order {order_no} not found.")
        return

    lines = order.get("lines", [])

    # Classify lines by eye
    def _ek(e): return str(e or "").upper()
    r_line  = next((l for l in lines if _ek(l.get("eye_side")) in ("R","RIGHT")), None)
    l_line  = next((l for l in lines if _ek(l.get("eye_side")) in ("L","LEFT")), None)
    oth_lines = [
        l for l in lines
        if _ek(l.get("eye_side")) not in ("R","RIGHT","L","LEFT","S","SERVICE")
        and not l.get("is_service_line")
    ]

    r_lp = _aw_load_lp(r_line)
    l_lp = _aw_load_lp(l_line)

    # Expected supply date — check schema first so missing columns do not break the tab.
    _exp_date = ""
    try:
        _cols = _aw_q(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'orders'
              AND column_name IN ('expected_supply_date','expected_delivery_date','expected_date','delivery_date')
            """,
            {}
        ) or []
        _colset = {str(c.get('column_name') or '') for c in _cols}
        _date_col = next(
            (c for c in ('expected_supply_date','expected_delivery_date','expected_date','delivery_date') if c in _colset),
            None
        )
        if _date_col:
            _exp_rows = _aw_q(
                f"SELECT {_date_col}::text AS ed FROM orders WHERE order_no=%(ono)s LIMIT 1",
                {"ono": order_no}
            )
            _exp_date = str(_exp_rows[0].get("ed") or "")[:10] if _exp_rows else ""
    except Exception as _e:
        pass

    # ═══════════════════════════════════════════════════════════════════
    # ORDER HEADER
    # ═══════════════════════════════════════════════════════════════════

    with _hd_col:
        # Route summary chips from existing lens_params
        _route_chips = []
        for _rl in [r_line, l_line]:
            if not _rl: continue
            _rc = str((_aw_load_lp(_rl)).get("manufacturing_route","")).upper()
            _ec = _ek(_rl.get("eye_side",""))[:1]
            _rc_lbl = {"VENDOR":"🏭","INHOUSE":"🔬","EXTERNAL_LAB":"🧪","STOCK":"📦"}.get(_rc,"❓")
            if _rc:
                _route_chips.append(
                    f"<span style='background:#1e293b;color:#94a3b8;"
                    f"font-size:0.68rem;padding:1px 8px;border-radius:4px'>"
                    f"{_ec}: {_rc_lbl} {_rc}</span>"
                )
        st.markdown(
            f"<div style='padding:4px 0'>"
            f"<span style='font-size:1.2rem;font-weight:800;color:#f1f5f9'>"
            f"📋 {order_no}</span> "
            f"<span style='color:#64748b'>{order.get('patient_name','—')}</span>"
            + (f"<span style='color:#475569;font-size:0.78rem'> · Due: {_exp_date}</span>" if _exp_date else "")
            + (" &nbsp; " + " ".join(_route_chips) if _route_chips else "")
            + "</div>",
            unsafe_allow_html=True,
        )

    st.markdown(
        "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
        "padding:5px 14px;margin:4px 0 10px 0;font-size:0.72rem;color:#64748b'>"
        "Set route + supplier/lab/stock for each item. "
        "<b style='color:#94a3b8'>Apply both</b> for same route. "
        "<b style='color:#10b981'>Save Assignment</b> saves all lines together. "
        "In-house blank assignment happens inline below.</div>",
        unsafe_allow_html=True,
    )

    # ═══════════════════════════════════════════════════════════════════
    # ROUTE CONSTANTS
    # ═══════════════════════════════════════════════════════════════════

    ROUTE_OPTS  = ["STOCK", "VENDOR", "INHOUSE", "EXTERNAL_LAB"]
    ROUTE_LABEL = {
        "STOCK":        "📦 Stock",
        "VENDOR":       "🏭 Supplier",
        "INHOUSE":      "🔬 In-house Lab",
        "EXTERNAL_LAB": "🧪 External Lab",
    }

    def _default_route(line: dict, lp: dict) -> str:
        """Pick smart default route using assignment_panel classifiers if available."""
        # Saved route wins
        saved = str(lp.get("manufacturing_route","")).upper()
        if saved in ROUTE_OPTS:
            return saved
        # Use assignment_panel logic
        if _ap_get_routes:
            try:
                _valid, _default = _ap_get_routes(line)
                return _default
            except Exception as _e:
                pass
        # Fallback heuristics
        if _ap_is_stock_alloc and _ap_is_stock_alloc(line):
            return "STOCK"
        if _ap_is_inhouse_brand and _ap_is_inhouse_brand(line):
            return "INHOUSE"
        return "VENDOR"

    def _valid_routes(line: dict) -> list:
        """Valid route options for this line type."""
        if _ap_get_routes:
            try:
                _valid, _ = _ap_get_routes(line)
                return [r for r in _valid if r in ROUTE_OPTS]
            except Exception as _e:
                pass
        return ROUTE_OPTS  # fallback: all routes

    def _ranked_sups(product_id: str, route_type: str = "VENDOR") -> list:
        """Ranked supplier list from assignment_panel, with fallback."""
        if _ap_ranked_sups:
            try:
                return _ap_ranked_sups(product_id, route_type) or []
            except Exception as _e:
                pass
        # Fallback: flat supplier list
        _rows = _aw_q(
            "SELECT id::text AS id, party_name AS name, COALESCE(mobile,'') AS mobile "
            "FROM parties WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB') "
            "AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name"
        )
        return [{"id": r["id"], "name": r["name"], "is_primary": False, "notes":"", "rank":99}
                for r in _rows]

    # ═══════════════════════════════════════════════════════════════════
    # APPLY-TO-BOTH BUTTONS
    # ═══════════════════════════════════════════════════════════════════

    _rl_line_count = (1 if r_line else 0) + (1 if l_line else 0)
    if _rl_line_count == 2:
        _apb1, _apb2, _ = st.columns([2, 2, 4])
        with _apb1:
            if st.button("↕ Copy R → L", key="aw_copy_r_l",
                         help="Apply Right Eye route + supplier to Left Eye",
                         use_container_width=True):
                for _key in ("route","party","batch","party_name"):
                    _rv = st.session_state.get(f"aw_r_{_key}")
                    if _rv is not None:
                        st.session_state[f"aw_l_{_key}"] = _rv
                st.rerun()
        with _apb2:
            if st.button("↕ Copy L → R", key="aw_copy_l_r",
                         help="Apply Left Eye route + supplier to Right Eye",
                         use_container_width=True):
                for _key in ("route","party","batch","party_name"):
                    _lv = st.session_state.get(f"aw_l_{_key}")
                    if _lv is not None:
                        st.session_state[f"aw_r_{_key}"] = _lv
                st.rerun()

    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

    # ═══════════════════════════════════════════════════════════════════
    # EYE SECTION RENDERER
    # ═══════════════════════════════════════════════════════════════════

    def _render_eye_section(eye_label: str, line: dict, lp: dict,
                             pfx: str, accent: str) -> None:
        """
        Render one eye's full assignment panel.
        pfx = "aw_r" or "aw_l"
        Stores selections in st.session_state[pfx_*].
        """
        if not line:
            st.markdown(
                f"<div style='border:1px dashed #1e293b;border-radius:8px;"
                f"padding:20px;text-align:center;color:#334155;font-size:0.82rem'>"
                f"No {eye_label} Eye line in this order</div>",
                unsafe_allow_html=True,
            )
            return

        _lid   = str(line.get("line_id") or line.get("id",""))
        _pname = str(line.get("product_name","")).split(" | ")[0]
        _brand = str(line.get("brand",""))
        _qty   = int(line.get("quantity") or 1)
        _pwr   = _power_str(line)

        # Extra detail bits for header
        _bits = []
        if _brand: _bits.append(f"<span style='color:#60a5fa'>{_brand}</span>")
        _idx_v = str(lp.get("lens_index") or lp.get("index") or line.get("lens_index","")).strip()
        _coat_v = str(lp.get("coating") or lp.get("coating_type") or line.get("coating","")).strip()
        _treat_v = str(lp.get("treatment") or line.get("treatment","")).strip()
        if _idx_v: _bits.append(f"<span style='color:#94a3b8'>Index {_idx_v}</span>")
        if _coat_v: _bits.append(f"<span style='color:#94a3b8'>{_coat_v}</span>")
        if _treat_v and _treat_v.lower() not in ("","clear","none"):
            _bits.append(f"<span style='color:#f59e0b'>{_treat_v}</span>")

        # ── Product header card ──────────────────────────────────────
        st.markdown(
            f"<div style='background:{accent}0d;border:1px solid {accent}33;"
            f"border-left:4px solid {accent};border-radius:8px;"
            f"padding:10px 14px;margin-bottom:8px'>"
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:4px'>"
            f"<span style='background:{accent};color:#fff;font-size:0.7rem;font-weight:800;"
            f"padding:2px 9px;border-radius:5px;letter-spacing:.06em'>{eye_label[:1]}E</span>"
            f"<span style='color:#f1f5f9;font-weight:700;font-size:0.92rem'>{_pname}</span>"
            f"</div>"
            f"<div style='display:flex;gap:8px;flex-wrap:wrap;font-size:0.72rem;margin-bottom:4px'>"
            + (" &nbsp;·&nbsp; ".join(_bits) if _bits else "<span style='color:#475569'>No details</span>")
            + "</div>"
            f"<div style='display:flex;gap:10px;align-items:center'>"
            f"<code style='color:{accent};font-size:0.76rem;background:{accent}18;"
            f"padding:2px 8px;border-radius:4px'>{_pwr or 'Power not entered'}</code>"
            f"<span style='color:#475569;font-size:0.7rem'>Qty: {_qty}</span>"
            f"</div></div>",
            unsafe_allow_html=True,
        )

        # ── Already-billed lock check ───────────────────────────────
        _line_billed = False
        try:
            _bc = _aw_q("""
                SELECT 1 FROM challan_lines cl
                JOIN challans c ON c.id = cl.challan_id
                WHERE cl.order_line_id = %(lid)s::uuid
                  AND c.status NOT IN ('CANCELLED','VOID')
                LIMIT 1
            """, {"lid": _lid})
            _line_billed = bool(_bc)
        except Exception as _e:
            pass
        if _line_billed:
            st.markdown(
                f"<div style='background:#052e16;border:1px solid #22c55e44;"
                f"border-radius:6px;padding:6px 12px;font-size:0.78rem;color:#4ade80'>"
                f"🔒 Billed — route locked</div>",
                unsafe_allow_html=True,
            )
            return

        # ── Route selection ─────────────────────────────────────────
        _valid = _valid_routes(line)
        _def_route = _default_route(line, lp)

        # Pre-populate ss if not set yet
        _sk_route = f"{pfx}_route"
        if _sk_route not in st.session_state:
            st.session_state[_sk_route] = _def_route

        # Constrain to valid options
        _valid_opts  = [r for r in _valid if r in ROUTE_OPTS]
        _valid_labels = [ROUTE_LABEL[r] for r in _valid_opts]
        _cur_route = st.session_state[_sk_route]
        if _cur_route not in _valid_opts:
            _cur_route = _valid_opts[0] if _valid_opts else "VENDOR"
            st.session_state[_sk_route] = _cur_route

        _chosen = st.radio(
            f"Route ({eye_label})",
            options=_valid_opts,
            format_func=lambda r: ROUTE_LABEL.get(r, r),
            index=_valid_opts.index(_cur_route) if _cur_route in _valid_opts else 0,
            key=f"radio_{pfx}_route",
            horizontal=True,
            label_visibility="collapsed",
        )
        st.session_state[_sk_route] = _chosen

        # ── Route sub-form ──────────────────────────────────────────

        if _chosen == "STOCK":
            # ── STOCK: batch selector from inventory_stock ───────────
            _sk_batch = f"{pfx}_batch"
            _prod_id  = str(line.get("product_id",""))

            # Try batch_manager FIFO first (power-matched), fallback to raw SQL
            _stock_items: list = []
            try:
                from modules.batch_manager import get_batches_fifo as _fifo
                _df = _fifo(
                    _prod_id,
                    sph=line.get("sph"), cyl=line.get("cyl"),
                    axis=line.get("axis"), add_power=line.get("add_power"),
                    eye_side=str(line.get("eye_side","")).upper(),
                )
                if _df is not None and not _df.empty:
                    for _, _br in _df.iterrows():
                        _stock_items.append({
                            "id":    str(_br.get("id","")),
                            "batch": str(_br.get("batch_no","—")),
                            "avail": int(_br.get("available_qty", _br.get("quantity",0)) or 0),
                            "rack":  str(_br.get("rack_location", _br.get("location", "")) or ""),
                        })
            except Exception as _e:
                pass

            if not _stock_items:
                _sr = _aw_q("""
                    SELECT id::text, COALESCE(batch_no,'—') AS batch_no,
                           GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) AS avail,
                           COALESCE(location,'') AS rack
                    FROM inventory_stock
                    WHERE product_id=%(pid)s::uuid
                      AND COALESCE(is_active,TRUE)=TRUE
                      AND GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) > 0
                    ORDER BY avail DESC LIMIT 20
                """, {"pid": _prod_id})
                _stock_items = [
                    {"id": r["id"], "batch": r["batch_no"],
                     "avail": int(r["avail"] or 0), "rack": r["rack"]}
                    for r in _sr
                ]

            if _stock_items:
                _bat_labels = [
                    f"{s['batch']}  ·  Avail: {s['avail']}"
                    + (f"  ·  {s['rack']}" if s["rack"] else "")
                    for s in _stock_items
                ]
                _bat_ids = [s["id"] for s in _stock_items]

                # Pre-select saved batch
                _prev = lp.get("stock_id") or lp.get("batch_id","")
                _prev_i = 0
                if _prev and str(_prev) in _bat_ids:
                    _prev_i = _bat_ids.index(str(_prev))

                _bsel = st.selectbox(
                    "Batch / SKU",
                    options=range(len(_bat_labels)),
                    format_func=lambda i: _bat_labels[i],
                    index=_prev_i,
                    key=f"sel_{pfx}_stock",
                )
                st.session_state[_sk_batch] = _bat_ids[_bsel]
                st.caption(
                    f"✅ Will reserve stock on Save. "
                    f"Available: {_stock_items[_bsel]['avail']}"
                )
            else:
                st.warning(
                    "⚠️ No stock available for this product. "
                    "Route to Supplier or change at Backoffice."
                )
                st.session_state[_sk_batch] = None

        elif _chosen in ("VENDOR", "EXTERNAL_LAB"):
            # ── VENDOR / EXTERNAL_LAB: ranked supplier selector ──────
            _sk_party = f"{pfx}_party"
            _sk_pname = f"{pfx}_party_name"
            _is_lab   = (_chosen == "EXTERNAL_LAB")
            _lbl_p    = "External Lab" if _is_lab else "Supplier"

            # Get ranked list
            _sups = _ranked_sups(
                str(line.get("product_id","")),
                "EXTERNAL_LAB" if _is_lab else "VENDOR"
            )

            if _sups:
                _s_ids    = [s["id"]   for s in _sups]
                _s_names  = [s["name"] for s in _sups]
                _s_labels = []
                for _s in _sups:
                    _sl = _s.get("name","")
                    if _s.get("is_primary"): _sl = f"⭐ {_sl}"
                    if _s.get("notes"):      _sl += f"  ({_s['notes'][:30]})"
                    _s_labels.append(_sl)

                # Fetch party mobile
                _pmob = {}
                try:
                    _pmob_rows = _aw_q(
                        "SELECT id::text, COALESCE(mobile,'') AS mobile "
                        "FROM parties WHERE id=ANY(%(ids)s::uuid[])",
                        {"ids": _s_ids}
                    )
                    _pmob = {r["id"]: r["mobile"] for r in _pmob_rows}
                except Exception as _e:
                    pass

                # Pre-select saved supplier
                _prev_pid = (lp.get("supplier_id") or "")
                _prev_si  = 0
                if _prev_pid and str(_prev_pid) in _s_ids:
                    _prev_si = _s_ids.index(str(_prev_pid)) + 1

                _opts_full  = ["— Select —"] + _s_labels
                _vals_full  = [None] + _s_ids
                _psel = st.selectbox(
                    _lbl_p,
                    options=range(len(_opts_full)),
                    format_func=lambda i: _opts_full[i],
                    index=_prev_si,
                    key=f"sel_{pfx}_party",
                )
                _sel_id   = _vals_full[_psel]
                _sel_name = _s_names[_psel - 1] if _psel > 0 else ""
                st.session_state[_sk_party] = _sel_id
                st.session_state[_sk_pname] = _sel_name

                # WhatsApp + Call
                if _sel_id:
                    _mob = _pmob.get(_sel_id,"")
                    _wa_d = "".join(d for d in _mob if d.isdigit())
                    if _wa_d.startswith("91") and len(_wa_d)==12: _wa_d = _wa_d[2:]
                    _wa_num = f"91{_wa_d}" if len(_wa_d)==10 else ""
                    _wa_msg = (
                        f"*Order: {order_no}*\n"
                        f"Customer: {order.get('patient_name','—')}\n"
                        f"{eye_label} Eye: {_pname}\n"
                        f"Power: {_pwr}\nQty: {_qty}\n"
                        + (f"Expected: {_exp_date}\n" if _exp_date else "")
                        + "\nParakh Optical"
                    )
                    _wc1, _wc2 = st.columns([3, 1])
                    if _wa_num:
                        _wc1.link_button(
                            "📲 WhatsApp Order",
                            f"https://wa.me/{_wa_num}?text={_aw_uparse.quote(_wa_msg)}",
                            use_container_width=True,
                        )
                    if _mob:
                        _wc2.link_button("📞", f"tel:{_mob}", use_container_width=True)
                else:
                    st.caption("ℹ️ Supplier blank — select now or assign later in Supplier tab")
            else:
                st.warning(f"No {_lbl_p} found. Add parties in Masters.")
                st.session_state[_sk_party] = None

        elif _chosen == "INHOUSE":
            # ── INHOUSE: route confirmed, show job card form inline ──
            st.markdown(
                "<div style='background:#0f1a0f;border:1px solid #22c55e33;"
                "border-radius:6px;padding:6px 12px;margin:4px 0;"
                "color:#86efac;font-size:0.72rem'>"
                "🔬 In-house Lab — route set. "
                "Complete the <b>Surfacing Job Card</b> below to assign blank.</div>",
                unsafe_allow_html=True,
            )
            st.session_state[f"{pfx}_party"] = None

            # Inline surfacing job card
            if _jcs_render:
                _jc_order = {
                    "id":           order.get("id",""),
                    "order_no":     order_no,
                    "patient_name": order.get("patient_name","—"),
                    "party_name":   order.get("party_name",""),
                    "order_type":   line.get("order_type","RETAIL"),
                }
                # Prep line dict for job card
                import json as _jc_json
                _jc_line = dict(line)
                _jc_line["order_no"]    = order_no
                _jc_line["line_id"]     = _lid
                _lp_for_jc = _aw_load_lp(line)
                _jc_line["lens_params"]   = _lp_for_jc
                _jc_line["surfacing_data"] = (_lp_for_jc.get("surfacing_data") or
                                               line.get("surfacing_data") or {})
                _jc_line["billing_qty"] = int(line.get("billing_qty") or line.get("quantity") or 1)
                _jc_line["add_power"]   = (float(line.get("add_power") or 0) or None)

                with st.expander("🔧 Surfacing Job Card", expanded=not bool(_jc_line["surfacing_data"])):
                    try:
                        _jcs_render(_jc_line, _jc_order, show_buttons=True)
                    except Exception as _jce:
                        st.error(f"Job card error: {_jce}")
            else:
                st.info(
                    "🔬 Route set to In-house Lab. "
                    "Open the **In-house Lab** tab to assign blank and print job card."
                )

    # ═══════════════════════════════════════════════════════════════════
    # R + L PANELS (side by side)
    # ═══════════════════════════════════════════════════════════════════

    _col_r, _col_l = st.columns(2)

    with _col_r:
        with st.container(border=True):
            st.markdown(
                "<div style='color:#ef4444;font-size:0.68rem;font-weight:800;"
                "letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px'>"
                "◉ RIGHT EYE</div>",
                unsafe_allow_html=True,
            )
            _render_eye_section("RIGHT", r_line, r_lp, "aw_r", "#ef4444")

    with _col_l:
        with st.container(border=True):
            st.markdown(
                "<div style='color:#60a5fa;font-size:0.68rem;font-weight:800;"
                "letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px'>"
                "◉ LEFT EYE</div>",
                unsafe_allow_html=True,
            )
            _render_eye_section("LEFT", l_line, l_lp, "aw_l", "#60a5fa")

    # ═══════════════════════════════════════════════════════════════════
    # FRAMES / OTHER ITEMS
    # ═══════════════════════════════════════════════════════════════════

    if oth_lines:
        st.markdown(
            "<div style='color:#a78bfa;font-size:0.68rem;font-weight:800;"
            "letter-spacing:.08em;text-transform:uppercase;margin:12px 0 6px 0'>"
            "🖼 FRAMES / OTHER ITEMS</div>",
            unsafe_allow_html=True,
        )
        for _oi, _ol in enumerate(oth_lines):
            _ol_lid   = str(_ol.get("line_id") or _ol.get("id",""))
            _ol_name  = str(_ol.get("product_name","")).split(" | ")[0]
            _ol_qty   = int(_ol.get("quantity") or 1)
            _ol_lp    = _aw_load_lp(_ol)
            _ol_route = str(_ol_lp.get("manufacturing_route","STOCK")).upper()

            with st.container(border=True):
                _fc1, _fc2 = st.columns([5, 3])
                with _fc1:
                    _ol_sku = str(_ol.get("batch_no") or _ol_lp.get("batch_no") or "")
                    st.markdown(
                        f"<span style='color:#e2e8f0;font-weight:700'>{_ol_name}</span>"
                        + (f" <code style='color:#94a3b8;font-size:0.7rem'>{_ol_sku}</code>" if _ol_sku else "")
                        + f" <span style='color:#475569;font-size:0.7rem'>· Qty {_ol_qty}</span>",
                        unsafe_allow_html=True,
                    )

                    # Route selector (STOCK / VENDOR only for frames)
                    _fr_valid = ["STOCK", "VENDOR"]
                    _sk_fr_rt = f"aw_fr_{_oi}_route"
                    if _sk_fr_rt not in st.session_state:
                        st.session_state[_sk_fr_rt] = _ol_route if _ol_route in _fr_valid else "STOCK"
                    _fr_chosen = st.radio(
                        "Route",
                        options=_fr_valid,
                        format_func=lambda r: {"STOCK": "📦 Stock", "VENDOR": "🏭 Supplier"}.get(r, r),
                        index=_fr_valid.index(st.session_state[_sk_fr_rt]) if st.session_state[_sk_fr_rt] in _fr_valid else 0,
                        key=f"radio_aw_fr_{_oi}_route",
                        horizontal=True,
                        label_visibility="collapsed",
                    )
                    st.session_state[_sk_fr_rt] = _fr_chosen

                    # SKU/batch selector — only when STOCK route
                    if _fr_chosen == "STOCK":
                        _sk_fr_bt = f"aw_fr_{_oi}_batch"
                        _fr_prod_id = str(_ol.get("product_id",""))
                        _fr_stk = _aw_q(
                            "SELECT id::text, COALESCE(batch_no,'—') AS batch_no, "
                            "       GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) AS avail, "
                            "       COALESCE(location,'') AS rack "
                            "FROM inventory_stock "
                            "WHERE product_id=%(pid)s::uuid "
                            "  AND COALESCE(is_active,TRUE)=TRUE "
                            "  AND GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) > 0 "
                            "ORDER BY batch_no, avail DESC LIMIT 30",
                            {"pid": _fr_prod_id}
                        )
                        if _fr_stk:
                            _fr_bt_labels = [
                                f"{r['batch_no']}  ·  Avail: {r['avail']}"
                                + (f"  ·  {r['rack']}" if r["rack"] else "")
                                for r in _fr_stk
                            ]
                            _fr_bt_ids = [r["id"] for r in _fr_stk]
                            # Pre-select saved SKU
                            _prev_fr_bid = str(_ol_lp.get("stock_id","")).strip() or _ol_sku
                            _prev_fr_i = 0
                            for _fi, _fr in enumerate(_fr_stk):
                                if _fr["id"] == _prev_fr_bid or _fr["batch_no"] == _prev_fr_bid:
                                    _prev_fr_i = _fi
                                    break
                            _fr_bsel = st.selectbox(
                                "SKU / Batch",
                                options=range(len(_fr_bt_labels)),
                                format_func=lambda i: _fr_bt_labels[i],
                                index=_prev_fr_i,
                                key=f"sel_aw_fr_{_oi}_batch",
                            )
                            st.session_state[_sk_fr_bt] = _fr_bt_ids[_fr_bsel]
                        else:
                            st.warning("⚠️ No stock for this frame — switch to Supplier route")
                            st.session_state[f"aw_fr_{_oi}_batch"] = None

                with _fc2:
                    # Live stock availability display
                    _fra = _aw_q(
                        "SELECT COALESCE(SUM(GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0))),0) AS av "
                        "FROM inventory_stock WHERE product_id=%(pid)s::uuid "
                        "AND COALESCE(is_active,TRUE)=TRUE",
                        {"pid": str(_ol.get("product_id",""))}
                    )
                    _fr_avail_disp = int((_fra[0].get("av") or 0) if _fra else 0)
                    if _fr_avail_disp >= _ol_qty:
                        st.markdown(
                            f"<span style='color:#22c55e;font-size:0.75rem;font-weight:700'>"
                            f"✅ {_fr_avail_disp} in stock</span>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            f"<span style='color:#ef4444;font-size:0.75rem;font-weight:700'>"
                            f"⚠️ Low stock ({_fr_avail_disp} avail, need {_ol_qty})</span>",
                            unsafe_allow_html=True,
                        )

    # ═══════════════════════════════════════════════════════════════════
    # VALIDATION
    # ═══════════════════════════════════════════════════════════════════

    st.markdown("---")

    def _aw_collect_saves() -> tuple:
        """
        Build (saves_list, errors_list).

        saves_list = [(line_id, lp_patch_dict), ...]
          Each patch contains manufacturing_route plus route-specific keys.
          For STOCK: also stock_id, batch_no, batch_status — so reservation
          block can run for both lens lines and frame lines.

        Validation happens here (before any DB write):
          • STOCK without batch selected → error
          • Batch avail < qty → error (soft check; hard guard is in _aw_save_stock_assignment_atomic)
          • Frame STOCK without SKU → error
        """
        _saves: list = []
        _errs:  list = []

        # ── Lens lines (R and L) ─────────────────────────────────────
        for _pfx, _ln, _lp in [("aw_r", r_line, r_lp), ("aw_l", l_line, l_lp)]:
            if not _ln:
                continue
            _lid2     = str(_ln.get("line_id") or _ln.get("id",""))
            _eye_lbl2 = "R" if _pfx == "aw_r" else "L"
            _rt       = st.session_state.get(f"{_pfx}_route", "VENDOR")
            _patch: dict = {"manufacturing_route": _rt}

            if _rt == "STOCK":
                _bid = st.session_state.get(f"{_pfx}_batch")
                if not _bid:
                    _errs.append(f"{_eye_lbl2} Eye — STOCK route: no batch selected")
                else:
                    _br = _aw_q(
                        "SELECT batch_no, GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0)) AS avail "
                        "FROM inventory_stock WHERE id=%(bid)s::uuid LIMIT 1",
                        {"bid": _bid}
                    )
                    if _br:
                        _needed = int(_ln.get("quantity") or 1)
                        _avail  = int(_br[0].get("avail") or 0)

                        # Skip availability check when re-saving the same already-allocated batch.
                        # The stock was already reserved for this line on the first save, so
                        # available=0 is expected. _aw_save_stock_assignment_atomic() will
                        # detect same stock_id + same qty and return already_allocated safely.
                        _already_this_batch = (
                            str(_lp.get("stock_id","")).strip() == str(_bid).strip()
                            and str(_lp.get("batch_status","")).upper() == "ALLOCATED"
                        )

                        if _avail < _needed and not _already_this_batch:
                            _errs.append(
                                f"{_eye_lbl2} Eye — only {_avail} available, need {_needed}"
                            )
                        else:
                            _patch.update({
                                "stock_id":      _bid,
                                "batch_no":      _br[0].get("batch_no",""),
                                "batch_status":  "ALLOCATED",
                                "supplier_id":   None,
                                "supplier_name": "",
                            })
                    else:
                        _errs.append(f"{_eye_lbl2} Eye — selected batch not found")

            elif _rt in ("VENDOR","EXTERNAL_LAB"):
                _pid = st.session_state.get(f"{_pfx}_party")
                _pnm = st.session_state.get(f"{_pfx}_party_name","")
                _patch.update({
                    "supplier_id":    _pid or None,
                    "supplier_name":  _pnm,
                    "supplier_stage": _lp.get("supplier_stage","ORDER_PLACED"),
                })
                _patch.pop("stock_id", None)
                _patch.pop("batch_no", None)

            elif _rt == "INHOUSE":
                _patch.pop("supplier_id", None)
                _patch.pop("supplier_name", None)
                _patch.pop("stock_id", None)
                _patch.pop("batch_no", None)

            _saves.append((_lid2, _patch))

        # ── Frame / other lines ──────────────────────────────────────
        for _oi, _ol in enumerate(oth_lines):
            _ol_lid  = str(_ol.get("line_id") or _ol.get("id",""))
            _ol_name_c = str(_ol.get("product_name","")).split(" | ")[0]
            _fr_rt   = st.session_state.get(f"aw_fr_{_oi}_route", "STOCK")

            if _fr_rt == "STOCK":
                _fr_bid = st.session_state.get(f"aw_fr_{_oi}_batch")
                if not _fr_bid:
                    _errs.append(
                        f"Frame '{_ol_name_c}' — STOCK route: no SKU/batch selected"
                    )
                else:
                    _fr_br = _aw_q(
                        "SELECT batch_no, GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0)) AS avail "
                        "FROM inventory_stock WHERE id=%(bid)s::uuid LIMIT 1",
                        {"bid": _fr_bid}
                    )
                    if _fr_br:
                        _fr_needed  = int(_ol.get("quantity") or 1)
                        _fr_avail2  = int(_fr_br[0].get("avail") or 0)
                        _fr_cur_lp  = _aw_load_lp(_ol)

                        # Skip availability check when re-saving the same already-allocated batch.
                        _fr_already = (
                            str(_fr_cur_lp.get("stock_id","")).strip() == str(_fr_bid).strip()
                            and str(_fr_cur_lp.get("batch_status","")).upper() == "ALLOCATED"
                        )

                        if _fr_avail2 < _fr_needed and not _fr_already:
                            _errs.append(
                                f"Frame '{_ol_name_c}' — only {_fr_avail2} avail, need {_fr_needed}"
                            )
                        else:
                            _saves.append((_ol_lid, {
                                "manufacturing_route": "STOCK",
                                "stock_id":    _fr_bid,
                                "batch_no":    _fr_br[0].get("batch_no",""),
                                "batch_status": "ALLOCATED",
                            }))
                    else:
                        _errs.append(f"Frame '{_ol_name_c}' — selected batch not found")
            else:
                # VENDOR route for frames — just save route, no stock touch
                _saves.append((_ol_lid, {"manufacturing_route": _fr_rt}))

        return _saves, _errs

    _saves_preview, _errs_preview = _aw_collect_saves()

    if _errs_preview:
        for _em in _errs_preview:
            st.warning(f"⚠️ {_em}")

    # ═══════════════════════════════════════════════════════════════════
    # SAVE + PRINT BUTTONS
    # ═══════════════════════════════════════════════════════════════════

    _sb1, _sb2, _sb3, _sb4 = st.columns([2, 1, 1, 1])

    _do_save       = _sb1.button("💾 Save Assignment",
                                  key="aw_save", type="primary", use_container_width=True)
    _do_cr80       = _sb2.button("🪪 Auth Card",
                                  key="aw_cr80", use_container_width=True,
                                  help="Print authenticity card (R+L)")
    _do_label      = _sb3.button("🏷 Labels",
                                  key="aw_label", use_container_width=True,
                                  help="Print lens labels (R+L)")
    _do_cancel     = _sb4.button("✕ Cancel",
                                  key="aw_cancel", use_container_width=True)

    # ── Cancel ───────────────────────────────────────────────────────
    if _do_cancel:
        st.session_state["prod_view_mode"] = "list"
        st.session_state["prod_assign_order_no"] = None
        st.rerun()

    # ── Print Auth Card (no save needed) ────────────────────────────
    if _do_cr80 and (_pp_cr80 or (_pp_build_cr80 and _pp_open_print)):
        _rx_r = {"sph": r_line.get("sph") if r_line else None,
                 "cyl": r_line.get("cyl") if r_line else None,
                 "axis": r_line.get("axis") if r_line else None,
                 "add": r_line.get("add_power") if r_line else None}
        _rx_l = {"sph": l_line.get("sph") if l_line else None,
                 "cyl": l_line.get("cyl") if l_line else None,
                 "axis": l_line.get("axis") if l_line else None,
                 "add": l_line.get("add_power") if l_line else None}
        try:
            if _pp_build_cr80 and _pp_open_print:
                _pp_open_print(_pp_build_cr80(r_line, l_line, order))
            else:
                _pp_cr80(
                    name=order.get("patient_name","—"),
                    mobile=order.get("mobile",""),
                    ono=order_no,
                    party=order.get("party_name",""),
                    rx_r=_rx_r, rx_l=_rx_l, copies=1
                )
        except Exception as _pce:
            st.error(f"Print error: {_pce}")

    # ── Print Labels (no save needed) ───────────────────────────────
    if _do_label and (_pp_label or (_pp_build_label and _pp_open_print)):
        _rx_r2 = {"sph": r_line.get("sph") if r_line else None,
                  "cyl": r_line.get("cyl") if r_line else None,
                  "axis": r_line.get("axis") if r_line else None,
                  "add": r_line.get("add_power") if r_line else None}
        _rx_l2 = {"sph": l_line.get("sph") if l_line else None,
                  "cyl": l_line.get("cyl") if l_line else None,
                  "axis": l_line.get("axis") if l_line else None,
                  "add": l_line.get("add_power") if l_line else None}
        def _aw_missing_blank_for_print():
            _missing = []
            for _eye, _line, _lp in (("R", r_line, r_lp), ("L", l_line, l_lp)):
                if not _line:
                    continue
                _svc = str((_lp or {}).get("service_production_type") or "").upper()
                if _svc in ("FITTING", "COLOURING"):
                    continue
                _surf = (_lp or {}).get("surfacing_data") or {}
                if isinstance(_surf, dict) and (
                    _surf.get("blank_id") or _surf.get("selected_blank_id") or _surf.get("blank_batch")
                ):
                    continue
                _lid = str(_line.get("line_id") or _line.get("id") or "")
                _has_alloc = False
                if _lid:
                    try:
                        _alloc = _aw_q(
                            "SELECT 1 FROM blank_allocations WHERE order_line_id=%(lid)s::uuid LIMIT 1",
                            {"lid": _lid},
                        ) or []
                        _has_alloc = bool(_alloc)
                    except Exception as _alloc_e:
                        log.debug("Assignment workspace blank check failed: %s", _alloc_e)
                if not _has_alloc:
                    _missing.append(_eye)
            return _missing

        try:
            _missing_blank = _aw_missing_blank_for_print()
            if _missing_blank:
                st.error(f"🔴 Assignment not done — assign blank first for {'/'.join(_missing_blank)} eye before printing labels.")
            elif _pp_build_label and _pp_open_print:
                _pp_open_print(_pp_build_label([x for x in (r_line, l_line) if x], order))
            else:
                _pp_label(
                    patient={
                        "name":   order.get("patient_name","—"),
                        "mobile": order.get("mobile",""),
                        "id":     order_no,
                    },
                    rx_r=_rx_r2, rx_l=_rx_l2, copies=1
                )
        except Exception as _ple:
            st.error(f"Label print error: {_ple}")

    # ── Save Assignment ──────────────────────────────────────────────
    if _do_save:
        if _errs_preview:
            st.error("❌ Fix the issues above before saving.")
        elif not _saves_preview:
            st.warning("Nothing to save.")
        else:
            _all_ok       = True
            _saved_inhouse: list = []

            for _save_lid, _save_patch in _saves_preview:
                _route = _save_patch.get("manufacturing_route","")

                # ── STOCK: one atomic transaction — reserve + write lens_params together
                if _route == "STOCK":
                    _new_bid = _save_patch.get("stock_id")
                    _save_line = next(
                        (l for l in [r_line, l_line] + oth_lines
                         if l and str(l.get("line_id") or l.get("id","")) == _save_lid),
                        None
                    )
                    _deduct_qty = int(_save_line.get("quantity") or 1) if _save_line else 1

                    if not _new_bid:
                        st.error("❌ STOCK line has no stock_id — not saved.")
                        _all_ok = False
                        break

                    _ok_atomic, _atomic_msg = _aw_save_stock_assignment_atomic(
                        _save_lid, _save_patch, _deduct_qty
                    )
                    if not _ok_atomic:
                        st.error(f"❌ {_atomic_msg}")
                        _all_ok = False
                        break
                    # lens_params already written inside the transaction — nothing else needed

                # ── Non-STOCK routes: just write lens_params ─────────
                else:
                    if not _aw_write_lp(_save_lid, _save_patch):
                        _all_ok = False
                        break

                    # INHOUSE: create job_master row after route is written
                    if _route == "INHOUSE" and _ap_upsert_jm:
                        _line_for_jm = next(
                            (l for l in [r_line, l_line] + oth_lines
                             if l and str(l.get("line_id") or l.get("id","")) == _save_lid),
                            None
                        )
                        if _line_for_jm and _is_production_lens_line_ih(_line_for_jm):
                            try:
                                _ap_upsert_jm(_line_for_jm, order)
                                _saved_inhouse.append(
                                    str(_line_for_jm.get("eye_side","?")).upper()[:1]
                                )
                            except Exception as _jme:
                                st.warning(f"Job master warning: {_jme}")
                        elif _line_for_jm:
                            st.info(
                                f"{_line_for_jm.get('product_name','Item')} is not a production lens line; "
                                "kept out of in-house job cards and will flow to billing/procurement."
                            )

            if _all_ok:
                # Recompute order status
                try:
                    from modules.backoffice.order_status_live import compute_order_status as _cos_aw
                    _cos_aw(
                        {"id": order.get("id",""), "order_no": order_no,
                         "status": order.get("status","")},
                        write=True
                    )
                except Exception as _e:
                    pass

                _msg = f"✅ Assignment saved — {len(_saves_preview)} line(s)"
                if _saved_inhouse:
                    _msg += f" · Job card created: {'+'.join(_saved_inhouse)} Eye"
                st.success(_msg)

                import time; time.sleep(0.5)
                st.session_state["prod_view_mode"]       = "list"
                st.session_state["prod_assign_order_no"] = None
                st.session_state["prod_orders_loaded"]   = False
                st.rerun()



# ── Public entry points ──────────────────────────────────────────────────

def render_inhouse_pipeline(*args, **kwargs):
    return _render_inhouse_pipeline(*args, **kwargs)

def render_assignment_workspace(*args, **kwargs):
    return _render_assignment_workspace(*args, **kwargs)
