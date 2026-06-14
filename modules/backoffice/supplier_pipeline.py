"""
modules/backoffice/supplier_pipeline.py
=============================================
Supplier Pipeline (🏭) — VENDOR/EXTERNAL_LAB orders, PO creation, supplier invoices.

Extracted from production_page.py.
Entry points called from production_page.py:
  render_supplier_pipeline
"""
from __future__ import annotations
from modules.backoffice.production_shared import (
    _PIPELINE_THEME,
    _q,
    _render_pipeline_cards,
    _go_to_billing,
    _check_purchase_acked,
    _power_str
)

import streamlit as st

try:
    from modules.backoffice.inhouse_pipeline import (
        build_optical_stage_flow,
        build_service_only_stage_flow,
        detect_coating_path,
        normalize_stage_alias,
    )
except Exception:
    build_optical_stage_flow = None
    build_service_only_stage_flow = None
    normalize_stage_alias = lambda stage: str(stage or "").upper().strip()

    def detect_coating_path(coating: str, has_colouring: bool) -> str:
        txt = str(coating or "").upper()
        compact = "".join(ch for ch in txt if ch.isalnum())
        has_hc = (
            "HARDCOAT" in txt
            or "HARD COAT" in txt
            or "HARDCOTE" in txt
            or "ULTRAHC" in compact
            or "OPTIFRESHHC" in compact
            or compact.endswith("HC")
            or " HC" in f" {txt} "
        )
        if has_hc and has_colouring:
            return "COLOURING_HC"
        if has_hc:
            return "HARDCOAT"
        if has_colouring:
            return "COLOURING"
        return "UNCOATED"

def _scan_norm(value: str) -> str:
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
        import logging as _lg; _lg.getLogger(__name__).warning(f"[prod_page] silent err: {_e}")


def _render_supplier_pipeline(route_filter: str = "VENDOR"):
    """
    Full supplier/lab order pipeline:
    ORDER_PLACED → SUPPLIER_CONFIRMED → AWAITING_SUPPLY → RECEIVED → INSPECTION → READY_FOR_BILLING
    """
    def _power_str(line: dict) -> str:
        """Format power string — is-not-None checks so SPH 0.0 (plano) renders correctly.
        Falls back to lens_params for stock lenses that store power there."""
        parts = []
        try:
            # ── Pull from columns first (is-not-None so 0.0 / 0 are kept) ──
            sph  = line["sph"]       if "sph"       in line and line["sph"]  is not None else None
            cyl  = line["cyl"]       if "cyl"       in line and line["cyl"]  is not None else None
            axis = line["axis"]      if "axis"      in line and line["axis"] is not None else None
            add  = line["add_power"] if "add_power" in line and line["add_power"] is not None else None

            if sph  is None: sph  = line.get("sph_val")
            if cyl  is None: cyl  = line.get("cyl_val")
            if axis is None: axis = line.get("axis_val")
            if add  is None: add  = line.get("add")

            if any(v is None for v in (sph, cyl, axis)):
                import json as _pj
                _lp = line.get("_lp") or line.get("lens_params") or {}
                if isinstance(_lp, str):
                    try: _lp = _pj.loads(_lp)
                    except: _lp = {}
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


    import json as _jsp
    import urllib.parse as _uparse

    _is_lab = (route_filter == "EXTERNAL_LAB")
    if _is_lab:
        st.markdown("### 🧪 External Lab Pipeline")
        st.caption("Manage lenses sent to external labs — from order placement to return.")
    else:
        st.markdown("### 🏭 Supplier Pipeline")
        st.caption("Manage direct supplier orders — from placement to delivery and billing.")

    # ── Stage definitions ─────────────────────────────────────────────
    STAGES = [
        ("NEEDS_ORDERING",      "🆕 Needs Ordering"),
        ("ORDER_PLACED",        "📤 Order Placed"),
        # SUPPLIER_CONFIRMED is metadata-only — ref number saves to lens_params
        # and advances directly to AWAITING_SUPPLY. Not a pipeline step.
        ("AWAITING_SUPPLY",     "⏳ Awaiting Supply"),
        ("RECEIVED",            "📦 Received"),
        ("INSPECTION",          "🔍 Inspection"),
        ("READY_FOR_BILLING",   "💰 Ready for Billing"),
    ]
    STAGE_IDX   = {s[0]: i for i, s in enumerate(STAGES)}
    STAGE_IDX["SUPPLIER_CONFIRMED"] = STAGE_IDX["ORDER_PLACED"]  # legacy
    STAGE_LABEL = {s[0]: s[1] for s in STAGES}
    STAGE_LABEL["SUPPLIER_CONFIRMED"] = "📤 Order Placed"

    def _lp_dict_ih(line: dict) -> dict:
        """Parse lens_params into dict."""
        import json as _lpji
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            try:
                lp = _lpji.loads(lp)
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

    def _order_has_service_ih(order_id: str, service_type: str) -> bool:
        try:
            from modules.sql_adapter import run_query as _rq_svc_ih
            rows = _rq_svc_ih(
                """
                SELECT 1
                FROM order_lines ol
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
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
                {"oid": order_id, "needle": f"%{service_type.lower()}%"},
            ) or []
            return bool(rows)
        except Exception as _e:
            return False

    def _coating_path_ih(line: dict, order_id: str = "") -> str:
        # Delegates to the module-level helper so behaviour stays in one place.
        # Both the inhouse panel and the supplier panel use the same logic now.
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
        has_colouring = _order_has_service_ih(order_id, "colour") if order_id else False
        return detect_coating_path(combined, has_colouring)

    def _stage_sequence_ih(line: dict, order_id: str = "") -> list:
        lp = _lp_dict_ih(line)
        # Service-only jobs (no lens product) — detect all services on this order
        service_type = str(lp.get("service_production_type") or "").upper()
        if service_type in ("COLOURING", "FITTING", "FITTING_ONLY"):
            # Check if the order also has the OTHER service type
            _has_col = service_type == "COLOURING" or _order_has_service_ih(order_id, "colour")
            _has_fit = service_type in ("FITTING", "FITTING_ONLY") or _order_has_service_ih(order_id, "fitting")
            if _has_col and _has_fit:
                return build_service_only_stage_flow("COLOURING+FITTING")
            if _has_col:
                return build_service_only_stage_flow("COLOURING")
            if _has_fit:
                return build_service_only_stage_flow("FITTING")

        # Normal lens flow — coating + colouring + fitting decided dynamically
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
        has_colouring = _order_has_service_ih(order_id, "colour") if order_id else False
        has_fitting   = _order_has_service_ih(order_id, "fitting") if order_id else False
        return build_optical_stage_flow(combined, has_colouring, has_fitting)

    def _next_stage_ih(line: dict, current_stage: str, events: list, order_id: str = ""):
        seq = _stage_sequence_ih(line, order_id)
        cur_norm = normalize_stage_alias(current_stage)
        positions = [i for i, s in enumerate(seq) if s == cur_norm]
        if not positions:
            return None
        stage_hits = sum(
            1 for ev in events
            if normalize_stage_alias(str(ev.get("stage_code") or "")) == cur_norm
        )
        pos_idx = min(max(stage_hits - 1, 0), len(positions) - 1)
        idx = positions[pos_idx]
        if idx + 1 < len(seq):
            code = seq[idx + 1]
            return code, STAGE_LABEL.get(code, code)
        return None

    def _prev_stages_ih(line: dict, current_stage: str, events: list, order_id: str = "") -> list:
        seq = _stage_sequence_ih(line, order_id)
        cur_norm = normalize_stage_alias(current_stage)
        positions = [i for i, s in enumerate(seq) if s == cur_norm]
        if not positions:
            return []
        stage_hits = sum(
            1 for ev in events
            if normalize_stage_alias(str(ev.get("stage_code") or "")) == cur_norm
        )
        pos_idx = min(max(stage_hits - 1, 0), len(positions) - 1)
        idx = positions[pos_idx]
        seen = []
        for code in seq[:idx]:
            if code not in seen:
                seen.append(code)
        return [(code, STAGE_LABEL.get(code, code)) for code in seen]

    def _stage_color(stage):
        return {"NEEDS_ORDERING":"#f59e0b","ORDER_PLACED":"#64748b","SUPPLIER_CONFIRMED":"#3b82f6",
                "AWAITING_SUPPLY":"#f59e0b","RECEIVED":"#8b5cf6",
                "INSPECTION":"#ef4444","READY_FOR_BILLING":"#22c55e"}.get(stage,"#475569")

    def _line_hardcoat_path(line: dict) -> str:
        """Return coating path for supplier/lab lines using the in-house rules."""
        lp = _lp_dict_ih(line)
        text = " ".join(
            str(x or "")
            for x in (
                line.get("product_name"),
                line.get("coating"),
                line.get("coating_type"),
                line.get("treatment"),
                lp.get("coating"),
                lp.get("coating_type"),
                lp.get("coating_name"),
                lp.get("treatment"),
                lp.get("material"),
            )
        )
        return detect_coating_path(text, _order_has_service_ih(str(line.get("order_id") or ""), "colour"))

    def _line_requires_internal_hardcoat(line: dict) -> bool:
        path = _line_hardcoat_path(line)
        return path in ("HARDCOAT", "COLOURING_HC", "HARDCOAT_ARC", "COLOURING_HC_ARC")

    def _line_internal_hardcoat_pending(line_or_lp: dict) -> bool:
        lp = line_or_lp.get("_lp") if isinstance(line_or_lp, dict) else {}
        if not isinstance(lp, dict):
            lp = line_or_lp if isinstance(line_or_lp, dict) else {}
        return str(lp.get("post_supplier_process") or lp.get("internal_process") or "").upper() == "HARDCOAT" \
            and str(lp.get("internal_process_stage") or "").upper() not in ("READY_TO_BILL", "BILLED", "CANCELLED", "VOID")

    def _send_supplier_line_to_internal_hardcoat(line: dict, lp_dict: dict) -> None:
        """Create/park a job_master row at INSPECTION so Hardcoat In is the next valid scan."""
        from modules.sql_adapter import run_write as _rw_hc

        line_id = str(line.get("line_id") or "").strip()
        if not line_id:
            raise ValueError("Order line is missing; cannot route to hardcoat.")
        qty = max(int(float(line.get("quantity") or line.get("billing_qty") or 1)), 1)
        order_no = str(line.get("order_no") or "").strip()
        production_ref = str(line.get("production_ref") or "").strip() or order_no

        _rw_hc(
            """
            UPDATE order_lines
               SET ready_qty = GREATEST(COALESCE(ready_qty,0), %(qty)s),
                   production_ref = COALESCE(NULLIF(production_ref,''), %(pref)s)
             WHERE id = %(lid)s::uuid
            """,
            {"lid": line_id, "qty": qty, "pref": production_ref},
        )
        _rw_hc(
            """
            INSERT INTO job_master (
                id, order_line_id, total_qty, blank_required_qty,
                blank_allocated_qty, current_stage, reprocess_count,
                is_closed, created_at, updated_at, coating_path
            )
            VALUES (
                gen_random_uuid(), %(lid)s::uuid, %(qty)s, 0,
                0, 'INSPECTION', 0, FALSE, NOW(), NOW(), 'HARDCOAT'
            )
            ON CONFLICT (order_line_id) DO UPDATE
               SET total_qty = GREATEST(job_master.total_qty, EXCLUDED.total_qty),
                   blank_required_qty = 0,
                   blank_allocated_qty = 0,
                   current_stage = CASE
                       WHEN job_master.current_stage IN ('READY_TO_BILL','BILLED','DISPATCHED','DELIVERED','CANCELLED','VOID')
                       THEN job_master.current_stage
                       ELSE 'INSPECTION'
                   END,
                   is_closed = CASE
                       WHEN job_master.current_stage IN ('READY_TO_BILL','BILLED','DISPATCHED','DELIVERED','CANCELLED','VOID')
                       THEN job_master.is_closed
                       ELSE FALSE
                   END,
                   coating_path = 'HARDCOAT',
                   updated_at = NOW()
            """,
            {"lid": line_id, "qty": qty},
        )
        _rw_hc(
            """
            INSERT INTO job_stage_events (id, job_id, stage_id, stage_code, remarks, created_at)
            SELECT gen_random_uuid(), jm.id, jsm.id, 'INSPECTION',
                   'External supply received; routed to internal hardcoat', NOW()
              FROM job_master jm
              JOIN job_stage_master jsm ON jsm.stage_code = 'INSPECTION'
             WHERE jm.order_line_id = %(lid)s::uuid
               AND NOT EXISTS (
                   SELECT 1
                     FROM job_stage_events e
                    WHERE e.job_id = jm.id
                      AND e.stage_code = 'INSPECTION'
                      AND COALESCE(e.remarks,'') = 'External supply received; routed to internal hardcoat'
               )
            """,
            {"lid": line_id},
        )

        lp_dict["supplier_stage"] = "INTERNAL_HARDCOAT"
        lp_dict["inspection_result"] = "PASS"
        lp_dict["post_supplier_process"] = "HARDCOAT"
        lp_dict["internal_process"] = "HARDCOAT"
        lp_dict["internal_process_stage"] = "INSPECTION"
        lp_dict["internal_hardcoat_from_supplier"] = True
        lp_dict["internal_hardcoat_routed_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        _save_lp(line_id, lp_dict)

    # ── Fetch lines ───────────────────────────────────────────────────
    try:
        rows = _q("""
            SELECT
                o.id::text          AS order_id,
                o.order_no,
                COALESCE(o.patient_name, o.party_name, '—') AS patient_name,
                o.status,
                ol.id::text         AS line_id,
                ol.eye_side,
                ol.quantity,
                COALESCE(ol.ready_qty, 0)    AS ready_qty,
                COALESCE(ol.allocated_qty,0) AS allocated_qty,
                COALESCE(ol.sph, 0)       AS sph,
                COALESCE(ol.cyl, 0)       AS cyl,
                COALESCE(ol.axis, 0)      AS axis,
                COALESCE(ol.add_power, 0) AS add_power,
                ol.lens_params,
                ol.boxing_params,
                ol.product_id::text       AS product_id,
                p.product_name,
                p.main_group
            FROM order_lines ol
            JOIN orders o   ON o.id = ol.order_id
            JOIN products p ON p.id = ol.product_id
            WHERE (
                      ol.lens_params->>'manufacturing_route' = %(route)s
                   OR ol.lens_params->>'job_type'             = %(route)s
                  )
              AND o.status NOT IN ('CANCELLED','CLOSED','PENDING_PAYMENT','PENDING_VALIDATION','PROVISIONAL','CREDIT_HOLD')
              -- UNDER_REVIEW/PENDING are kept: lines already assigned to a supplier/lab
              -- must show in the pipeline even if order-level status is not finally confirmed.
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
              AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) != 'STOCK'
              AND UPPER(COALESCE(ol.lens_params->>'batch_status','')) != 'STOCK_ALLOCATED'
              AND UPPER(COALESCE(ol.lens_params->>'supplier_stage','')) NOT IN
                  ('READY_FOR_PACK','INTERNAL_HARDCOAT')
            ORDER BY o.created_at DESC, o.order_no,
                     CASE WHEN ol.eye_side='R' THEN 0 WHEN ol.eye_side='L' THEN 1 ELSE 2 END
        """, {"route": route_filter}) or []

        # Enrich lens_params
        _sup_cache = {}
        for _row in rows:
            _lp = _row.get("lens_params") or {}
            if isinstance(_lp, str):
                try: _lp = _jsp.loads(_lp)
                except: _lp = {}
            _bp = _row.get("boxing_params") or {}
            if isinstance(_bp, str):
                try: _bp = _jsp.loads(_bp)
                except: _bp = {}
            _row["_lp"]           = _lp
            _row["boxing_params"] = _bp
            _row["supplier_id"]   = str(_lp.get("supplier_id") or "")
            _row["supplier_name"] = str(_lp.get("supplier_name") or "")
            _raw_sup = str(_lp.get("supplier_stage") or "NEEDS_ORDERING").upper()
            _row["sup_stage"] = "ORDER_PLACED" if _raw_sup == "SUPPLIER_CONFIRMED" else _raw_sup
            _row["sup_order_no"]  = str(_lp.get("supplier_order_no") or "")
            _sid = _row["supplier_id"]
            if _sid and _sid not in _sup_cache:
                try:
                    _sr = _q("SELECT party_name, mobile FROM parties WHERE id=%(sid)s::uuid LIMIT 1", {"sid": _sid}) or []
                    _sup_cache[_sid] = {"name": _sr[0]["party_name"] if _sr else "—",
                                        "mobile": _sr[0].get("mobile","") if _sr else ""}
                except Exception as _e:
                    _sup_cache[_sid] = {"name":"—","mobile":""}
            if _sid and not _row["supplier_name"]:
                _row["supplier_name"] = _sup_cache.get(_sid,{}).get("name","—")
            _row["supplier_mobile"] = _sup_cache.get(_sid,{}).get("mobile","") if _sid else ""

        # ── Batch-fetch live PO status per line from supplier_orders ─────────
        _line_ids_all = [r["line_id"] for r in rows if r.get("line_id")]
        _po_by_line   = {}
        _doc_by_line  = {}
        if _line_ids_all:
            try:
                _po_status_rows = _q("""
                    SELECT soi.customer_line_id::text AS line_id,
                           COALESCE(so.supplier_order_id,'PO-'||so.id::text) AS po_no,
                           so.status                  AS po_status,
                           so.created_at              AS po_created
                    FROM supplier_order_items soi
                    JOIN supplier_orders so ON so.id = soi.supplier_order_id
                    WHERE soi.customer_line_id = ANY(%(lids)s::text[])
                      AND so.status NOT IN ('CANCELLED','VOID')
                    ORDER BY so.created_at DESC
                """, {"lids": _line_ids_all})
                for _psr in _po_status_rows:
                    _k = _psr["line_id"]
                    if _k not in _po_by_line:          # keep most recent PO per line
                        _po_by_line[_k] = _psr
            except Exception as _e:
                pass
            try:
                _doc_rows = _q("""
                    SELECT cl.order_line_id::text AS line_id,
                           c.challan_no,
                           c.status AS challan_status,
                           i.invoice_no,
                           i.status AS invoice_status
                    FROM challan_lines cl
                    JOIN challans c ON c.id = cl.challan_id
                    LEFT JOIN invoices i
                           ON i.challan_id = c.id
                          AND i.status NOT IN ('CANCELLED','VOID')
                    WHERE cl.order_line_id = ANY(%(lids)s::uuid[])
                      AND c.status NOT IN ('CANCELLED','VOID')
                    ORDER BY c.created_at DESC
                """, {"lids": _line_ids_all})
                for _dr in _doc_rows or []:
                    _dk = str(_dr.get("line_id") or "")
                    if _dk and _dk not in _doc_by_line:
                        _doc_by_line[_dk] = _dr
            except Exception as _e:
                pass
        for _row in rows:
            _po_hit = _po_by_line.get(_row["line_id"], {})
            _row["live_po_no"]     = _po_hit.get("po_no", "")
            _row["live_po_status"] = _po_hit.get("po_status", "")
            _doc_hit = _doc_by_line.get(_row["line_id"], {})
            _row["challan_no"]     = _doc_hit.get("challan_no", "")
            _row["challan_status"] = _doc_hit.get("challan_status", "")
            _row["invoice_no"]     = _doc_hit.get("invoice_no", "")
            _row["invoice_status"] = _doc_hit.get("invoice_status", "")
            _row["_is_challaned"]  = bool(_row["challan_no"])
            _row["_is_invoiced"]   = bool(_row["invoice_no"]) or str(_row["challan_status"] or "").upper() == "INVOICED"

    except Exception as _se:
        st.error(f"Could not load lines: {_se}")
        return

    if not rows:
        # Show what routes/statuses exist so we can diagnose
        _debug = _q("""
            SELECT o.status,
                   ol.lens_params->>'manufacturing_route' AS route,
                   COALESCE(ol.billed_qty,0) AS bq,
                   COUNT(*) AS n
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
              AND ol.lens_params->>'manufacturing_route' = %(route)s
            GROUP BY o.status, ol.lens_params->>'manufacturing_route', COALESCE(ol.billed_qty,0)
            ORDER BY n DESC LIMIT 15
        """, {"route": route_filter})
        if _debug:
            with st.expander("🔍 No orders shown — debug info", expanded=True):
                st.caption("Orders exist with this route but are filtered out:")
                for r in _debug:
                    st.caption(
                        f"Status: {r.get('status')!r}  "
                        f"Route: {r.get('route')!r}  "
                        f"billed_qty: {r.get('bq')}  "
                        f"Count: {r.get('n')}"
                    )
        else:
            st.info("✅ No orders with this route.")
        return

    # ── Search / Filter bar ──────────────────────────────────────────────────
    import datetime as _dts_sp
    _today_sp = _dts_sp.date.today()
    _show_all_sp = st.session_state.get(f"spf_all_{route_filter}", False)
    with st.container(border=True):
        _sfa, _sfb, _sf1, _sf2, _sf3, _sf4, _sf5 = st.columns([1, 1, 2, 2, 2, 2, 1])
        _show_all_sp = _sfa.toggle("All", value=_show_all_sp,
                                    key=f"spf_all_{route_filter}",
                                    help="Show all including billed/completed")
        _sfb.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        _default_from = None if _show_all_sp else (_today_sp - _dts_sp.timedelta(days=30))
        _default_to   = None if _show_all_sp else _today_sp
        _flt_from  = _sf1.date_input("From", value=_default_from,
                                      key=f"spf_from_{route_filter}",
                                      label_visibility="collapsed",
                                      help="Order date from", format="DD/MM/YYYY")
        _flt_to    = _sf2.date_input("To",   value=_default_to,
                                      key=f"spf_to_{route_filter}",
                                      label_visibility="collapsed",
                                      help="Order date to",   format="DD/MM/YYYY")
        _flt_sup   = _sf3.text_input("Supplier", key=f"spf_sup_{route_filter}",
                                      placeholder="🔍 Supplier name",
                                      label_visibility="collapsed")
        _flt_ord   = _sf4.text_input("Order No", key=f"spf_ord_{route_filter}",
                                      placeholder="🔍 Order no / patient",
                                      label_visibility="collapsed")
        _all_stage_labels = ["All Stages"] + [s[1] for s in STAGES]
        _all_stage_codes  = ["ALL"] + [s[0] for s in STAGES]
        _flt_stg_lbl = _sf5.selectbox("Stage", _all_stage_labels,
                                       key=f"spf_stg_{route_filter}",
                                       label_visibility="collapsed")
        _flt_stg = _all_stage_codes[_all_stage_labels.index(_flt_stg_lbl)]

    # Apply filters to rows in Python (no round-trip to DB needed)
    import datetime as _dts
    def _matches(row):
        # Order date filter
        if _flt_from or _flt_to:
            _odate = None
            try:
                _odate = row.get("created_at")
                if isinstance(_odate, str):
                    _odate = _dts.date.fromisoformat(_odate[:10])
                elif hasattr(_odate, "date"):
                    _odate = _odate.date()
            except Exception as _e:
                pass
            if _odate:
                if _flt_from and _odate < _flt_from: return False
                if _flt_to   and _odate > _flt_to:   return False
        # Supplier name filter
        if _flt_sup and _flt_sup.strip():
            if _flt_sup.strip().lower() not in str(row.get("supplier_name","")).lower():
                return False
        if _flt_ord and _flt_ord.strip():
            if not _scan_match(_flt_ord, row.get("order_no", ""), row.get("patient_name", ""), row.get("party_name", "")):
                return False
        if _flt_stg != "ALL":
            if str(row.get("sup_stage","NEEDS_ORDERING")) != _flt_stg:
                return False
        return True

    rows = [r for r in rows if _matches(r)]

    if not rows:
        st.info("No lines match the current filters.")
        return

    _n_orders_sp = len(set(r['order_id'] for r in rows))
    _tbl_col, _cap_col = st.columns([1, 8])
    with _tbl_col:
        _has_unassigned_supplier_rows = any(not str(r.get("supplier_id") or "").strip() for r in rows)
        if _has_unassigned_supplier_rows:
            st.session_state[f"sp_tbl_{route_filter}"] = False
        _sup_table_view = st.toggle("⊞", value=st.session_state.get(f"sp_tbl_{route_filter}", not _has_unassigned_supplier_rows),
                                     key=f"sp_tbl_{route_filter}", help="Compact table view")
    with _cap_col:
        st.caption(f"Showing {_n_orders_sp} order(s) · {len(rows)} line(s)")

    if _sup_table_view:
        from collections import defaultdict as _spdd
        _sp_groups = _spdd(lambda: {"order_no":"","patient":"","lines":[],"order_id":"","created_at":""})
        for _r in rows:
            _gk = _r["order_id"]
            _sp_groups[_gk]["order_no"]   = _r["order_no"]
            _sp_groups[_gk]["patient"]    = _r["patient_name"]
            _sp_groups[_gk]["order_id"]   = _r["order_id"]
            _sp_groups[_gk]["created_at"] = str(_r.get("created_at",""))[:10]
            _sp_groups[_gk]["lines"].append(_r)
        _render_pipeline_cards(
            groups=_sp_groups,
            route_key=route_filter,
            stage_label_fn=lambda l: STAGE_LABEL.get(l.get("sup_stage","NEEDS_ORDERING"),"NEEDS_ORDERING").split(" ",1)[-1],
            stage_code_fn=lambda l: l.get("sup_stage","NEEDS_ORDERING"),
            open_billing_fn=_go_to_billing,
        )
        return

    # _sup_by_id: used only for stage-advance button labels (supplier name display).
    # Assignment dropdowns removed — assignment happens in the ✏️ workspace.
    _sup_by_id = {}
    try:
        _sup_rows = _q(
            "SELECT id::text, party_name, COALESCE(mobile,'') AS mobile "
            "FROM parties WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB') "
            "AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name", {}
        ) or []
        _sup_by_id = {r["id"]: {"name": r["party_name"], "mobile": r.get("mobile","")} for r in _sup_rows}
    except Exception as _e:
        pass

    def _save_lp(line_id, lp_dict):
        from modules.sql_adapter import run_write as _rw
        try:
            _old = _q(
                "SELECT lens_params FROM order_lines WHERE id=%(lid)s::uuid LIMIT 1",
                {"lid": line_id},
            ) or []
            _old_lp = _old[0].get("lens_params") if _old else {}
            if isinstance(_old_lp, str):
                try: _old_lp = _jsp.loads(_old_lp)
                except Exception: _old_lp = {}
            _old_stage = str((_old_lp or {}).get("supplier_stage") or "")
            _new_stage = str((lp_dict or {}).get("supplier_stage") or "")
            if _new_stage and _new_stage != _old_stage:
                import datetime as _dt_sup_tl
                _tl = list((lp_dict or {}).get("supplier_timeline") or [])
                _tl.append({
                    "stage": _new_stage,
                    "at": _dt_sup_tl.datetime.now().isoformat(timespec="seconds"),
                    "source": "production_supplier_panel",
                })
                lp_dict["supplier_timeline"] = _tl[-25:]
        except Exception as _e:
            pass
        _rw("UPDATE order_lines SET lens_params = %(lp)s::jsonb WHERE id = %(lid)s::uuid",
            {"lp": _jsp.dumps(lp_dict), "lid": line_id})

    def _ensure_pipeline_po(lines_for_po, supplier_id, supplier_name, order_no):
        """
        Auto-create a supplier_orders + supplier_order_items record the moment
        a pipeline order is placed (stage → SUPPLIER_CONFIRMED or ref entered).

        Idempotent: if a PO already exists for ANY of these lines via
        supplier_order_items.customer_line_id, returns that existing po_number.

        Returns po_number string (e.g. "PO/2526/0001") or "" on failure.
        """
        if not lines_for_po or not supplier_id:
            return ""
        try:
            from modules.sql_adapter import run_query as _rq_po, run_write as _rw_po

            # ── Check if PO already exists for any of these lines ─────────────
            _lids = [str(l["line_id"]) for l in lines_for_po if l.get("line_id")]
            if not _lids:
                return ""
            _existing = _rq_po("""
                SELECT so.supplier_order_id AS po_number
                FROM supplier_order_items soi
                JOIN supplier_orders so ON so.id = soi.supplier_order_id
                WHERE soi.customer_line_id = ANY(%(lids)s::text[])
                  AND so.status NOT IN ('CANCELLED','VOID')
                LIMIT 1
            """, {"lids": _lids})
            if _existing:
                return (_existing[0].get("po_number")
                        or _existing[0].get("supplier_order_id")
                        or "")

            # ── Allocate proper PO number ──────────────────────────────────────
            try:
                from modules.db.order_number_registry import alloc_doc_number
                _po_num = alloc_doc_number("PURCHASE_ORDER")
            except Exception as _e:
                import datetime as _dt2
                _po_num = f"PO/{_dt2.date.today().strftime('%y%m%d%H%M%S')}"

            # ── Totals ────────────────────────────────────────────────────────
            _tqty = sum(int(l.get("quantity") or 1) for l in lines_for_po)
            _tval = sum(
                float(l.get("unit_price") or 0) * int(l.get("quantity") or 1)
                for l in lines_for_po
            )

            # ── Insert PO header ──────────────────────────────────────────────
            _sync_supplier_orders_id_sequence()
            _hdr = _rq_po("""
                INSERT INTO supplier_orders (
                    supplier_order_id,
                    supplier_id, supplier_name,
                    order_date, status,
                    total_items, total_qty, total_value,
                    created_by, created_at
                ) VALUES (
                    %(pon)s,
                    %(sid)s::uuid, %(sname)s,
                    CURRENT_DATE, 'SENT',
                    %(items)s, %(qty)s, %(val)s,
                    'pipeline', NOW()
                )
                RETURNING id AS po_id
            """, {
                "pon":   _po_num,
                "sid":   supplier_id,
                "sname": supplier_name,
                "items": len(lines_for_po),
                "qty":   _tqty,
                "val":   _tval,
            })
            if not _hdr:
                return ""
            _po_id = int(_hdr[0]["po_id"])

            # ── Insert one item per line ──────────────────────────────────────
            for _idx, _ln in enumerate(lines_for_po):
                _null_uuid = "00000000-0000-0000-0000-000000000000"
                _pid  = _ln.get("product_id") or _null_uuid
                _qty  = int(_ln.get("quantity") or 1)
                _up   = float(_ln.get("unit_price") or 0)
                _rw_po("""
                    INSERT INTO supplier_order_items (
                        supplier_order_id,
                        item_no, product_id, product_name,
                        eye_side, sph, cyl, axis, add_power,
                        ordered_qty, unit_price, total_price,
                        customer_line_id, item_status
                    ) VALUES (
                        %(soid)s,
                        %(itno)s, %(pid)s::uuid, %(pname)s,
                        %(eye)s, %(sph)s, %(cyl)s, %(axis)s, %(add)s,
                        %(qty)s, %(up)s, %(tot)s,
                        NULLIF(%(clid)s,''), 'PENDING'
                    )
                    ON CONFLICT DO NOTHING
                """, {
                    "soid":  _po_id,
                    "itno":  _idx + 1,
                    "pid":   _pid,
                    "pname": (_ln.get("product_name") or "")[:120],
                    "eye":   str(_ln.get("eye_side") or ""),
                    "sph":   _ln.get("sph"),
                    "cyl":   _ln.get("cyl"),
                    "axis":  _ln.get("axis"),
                    "add":   _ln.get("add_power"),
                    "qty":   _qty,
                    "up":    _up,
                    "tot":   round(_up * _qty, 2),
                    "clid":  str(_ln["line_id"]),
                })

            return _po_num

        except Exception as _pe:
            import traceback
            st.warning(f"PO auto-create note: {_pe}")
            return ""


        parts = []
        try:
            if line.get("sph") is not None: parts.append(f"SPH {float(line['sph']):+.2f}")
            if line.get("cyl") and abs(float(line["cyl"])) > 0.01: parts.append(f"CYL {float(line['cyl']):+.2f}")
            if line.get("axis"): parts.append(f"AX {int(line['axis'])}")
            if line.get("add_power") and float(line["add_power"]) > 0: parts.append(f"ADD +{float(line['add_power']):.2f}")
        except Exception: pass
        return "  ".join(parts)

    # ── Group by (order_id, supplier_id) ─────────────────────────────
    # Single order can have R eye → Supplier A, L eye → Supplier B.
    # Each (order, supplier) pair = one card with its own WA message.
    from collections import defaultdict as _dd, OrderedDict as _od
    _groups = _od()  # key: (order_id, supplier_id)
    for row in rows:
        _gsid = row.get("supplier_id") or "__UNASSIGNED__"
        _gkey = (row["order_id"], _gsid)
        if _gkey not in _groups:
            _groups[_gkey] = {
                "order_id":      row["order_id"],
                "order_no":      row["order_no"],
                "patient_name":  row["patient_name"],
                "supplier_id":   _gsid if _gsid != "__UNASSIGNED__" else "",
                "supplier_name": row.get("supplier_name","—"),
                "lines":         [],
            }
        _groups[_gkey]["lines"].append(row)

    # ── Render each (order, supplier) card ────────────────────────────
    for _gkey, odata in _groups.items():
        _goid, _gsid = _gkey
        info  = odata
        lines = odata["lines"]
        _supp_hdr = odata["supplier_name"] or "Unassigned"

        _total = len(lines)
        _billing_ready = sum(1 for l in lines if l.get("sup_stage") == "READY_FOR_BILLING")

        # Check actual billing state from challans table — single source of truth
        _billed_line_count = 0
        try:
            from modules.sql_adapter import run_query as _rq_bls
            _bls = _rq_bls("""
                SELECT COUNT(DISTINCT cl.order_line_id) AS n
                FROM challan_lines cl
                JOIN challans c ON c.id = cl.challan_id
                WHERE cl.order_id = %(oid)s::uuid
                  AND c.status NOT IN ('CANCELLED','VOID')
                  AND COALESCE(c.is_deleted, FALSE) = FALSE
            """, {"oid": odata["order_id"]})
            _billed_line_count = int((_bls[0].get("n") or 0) if _bls else 0)
        except Exception as _e:
            pass
        _all_billed = (_billed_line_count >= _total and _total > 0)

        if _all_billed:
            _hdr_icon = "🧾"
        elif _billing_ready == _total and _total > 0:
            _hdr_icon = "💰"
        elif all(l.get("sup_stage") in ("RECEIVED","INSPECTION","READY_FOR_BILLING") for l in lines):
            _hdr_icon = "📦"
        else:
            _hdr_icon = "⏳"

        # Per-eye stage summary for collapsed header
        _rl_sorted_hdr = sorted(
            [l for l in lines if str(l.get("eye_side","")).upper() in ("R","L","RIGHT","LEFT")],
            key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1
        )
        _eye_stage_parts = []
        for _hl in _rl_sorted_hdr:
            _he = str(_hl.get("eye_side","")).upper()
            _hs = STAGE_LABEL.get(_hl.get("sup_stage") or "NEEDS_ORDERING",
                                   _hl.get("sup_stage") or "NEEDS_ORDERING")
            _hs_short = _hs.split(" ", 1)[-1] if _hs and _hs[0] in "📤✅⏳📦🔍💰" else _hs
            _eye_stage_parts.append(f"{_he}: {_hs_short}")
        _eye_stage_str = "  |  ".join(_eye_stage_parts)

        # ── Pre-compute advance state for top-level buttons ─────────
        _rl_assigned_top = sorted(
            [l for l in lines if str(l.get("eye_side","")).upper() in ("R","L","RIGHT","LEFT")
             and l.get("supplier_id")],
            key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1
        )
        _top_sup_ids = list(dict.fromkeys(l["supplier_id"] for l in _rl_assigned_top))
        _is_split_top = len(_top_sup_ids) > 1

        # ── Order separator — clear visual break between orders ────────
        _hdr_st = str(info.get("status","")).upper()
        _hdr_badge = (
            " <span style='background:#f97316;color:#fff;font-size:0.6rem;"
            "font-weight:700;padding:1px 6px;border-radius:8px'>⏸ HOLD</span>"
            if _hdr_st == "HOLD" else
            " <span style='background:#f59e0b;color:#000;font-size:0.6rem;"
            "font-weight:700;padding:1px 6px;border-radius:8px'>⏳ PENDING</span>"
            if _hdr_st == "PENDING" else ""
        )
        st.markdown(
            "<div style='border-top:2px solid #1e3a5f;margin:12px 0 4px 0;"
            "display:flex;align-items:center;gap:8px'>"
            f"<span style='background:#0a1628;color:#334155;font-size:0.62rem;"
            f"font-weight:700;padding:0 8px;letter-spacing:.06em;white-space:nowrap;"
            f"border:1px solid #1e3a5f;border-radius:3px'>"
            f"📋 {info['order_no']} — {info['patient_name']}{_hdr_badge}</span>"
            "<div style='flex:1;border-top:1px solid #1e293b'></div></div>",
            unsafe_allow_html=True,
        )

        # ── Order card — native container keeps everything visually grouped ──
        with st.container(border=True):

            # ── Top header row: order info + advance button(s) ────────
            _th_left, _th_right = st.columns([3, 2])
            with _th_left:
                st.markdown(
                    f"<div style='padding:4px 0'>"
                    f"<span style='font-weight:800;color:#e2e8f0;font-size:1rem'>"
                    f"{_hdr_icon} {info['order_no']}</span>"
                    f"<span style='color:#64748b;font-size:0.82rem'> — {info['patient_name']}</span><br>"
                    + ("" if _all_billed else f"<span style='color:#475569;font-size:0.72rem'>🏭 {_supp_hdr} · {_billing_ready}/{_total} ready"
                    + (f" · {_eye_stage_str}" if _eye_stage_str else "")
                    + "</span>")
                    + ("" if not _all_billed else
                       f"<br><span style='background:#052e16;color:#22c55e;font-size:0.78rem;"
                       f"font-weight:700;padding:2px 10px;border-radius:4px;"
                       f"border:1px solid #22c55e'>✅ BILLED — LOCKED</span>")
                    + "</div>",
                    unsafe_allow_html=True
                )
            with _th_right:
                if not _rl_assigned_top:
                    st.caption("Assign supplier in details below.")
                elif _rl_assigned_top:
                    st.caption("Use the R+L advancement controls below.")
                elif _is_split_top:
                    # One compact advance button per supplier
                    for _tsid in _top_sup_ids:
                        _tlines   = [l for l in _rl_assigned_top if l.get("supplier_id") == _tsid]
                        _teyes    = "+".join(str(l.get("eye_side","")).upper() for l in _tlines)
                        _tname    = _sup_by_id.get(_tsid, {}).get("name","") or                                     next((l.get("supplier_name","") for l in _tlines), "—")
                        _tstages  = [l.get("sup_stage") or "NEEDS_ORDERING" for l in _tlines]
                        _tmax_idx = max(STAGE_IDX.get(s, 0) for s in _tstages)
                        _tnext    = STAGES[_tmax_idx + 1] if _tmax_idx < len(STAGES) - 1 else None
                        if _tnext:
                            if st.button(
                                f"▶ {_teyes} → {_tnext[1]}",
                                key=f"top_adv_{route_filter}_{_goid}_{_tsid[:8]}",
                                use_container_width=True, type="primary"
                            ):
                                _miss_ref = _missing_supplier_refs(_tlines, _tnext[0])
                                if _miss_ref:
                                    _show_missing_ref_stop(_miss_ref)
                                if _tnext[0] == "READY_FOR_BILLING":
                                    st.error(
                                        "Inspection route decision is compulsory. Open the Inspection Result "
                                        "section and choose 'Ready for Billing' or 'Process to Hardcoat'."
                                    )
                                    st.stop()
                                try:
                                    import datetime as _dt_tadv
                                    from modules.sql_adapter import run_write as _rw_t
                                    for _tl in _tlines:
                                        _tlp = dict(_tl.get("_lp") or {})
                                        _tlp["supplier_stage"] = _tnext[0]
                                        if _tnext[0] == "SUPPLIER_CONFIRMED":
                                            _tlp["supplier_confirmed_at"] = _dt_tadv.datetime.now().astimezone().isoformat()
                                        if _tnext[0] == "RECEIVED":
                                            _tq = int(_tl.get("quantity") or 1)
                                            _tlp["ready_qty"] = _tq
                                            _rw_t("UPDATE order_lines SET ready_qty=%(rq)s WHERE id=%(lid)s::uuid",
                                                  {"rq": _tq, "lid": str(_tl["line_id"])})
                                        _save_lp(str(_tl["line_id"]), _tlp)
                                    # ── Auto-create PO when first confirming with supplier ──
                                    if _tnext[0] == "SUPPLIER_CONFIRMED":
                                        _po_num_t = _ensure_pipeline_po(
                                            _tlines, _tsid,
                                            _sup_by_id.get(_tsid, {}).get("name", _tname),
                                            info.get("order_no", "")
                                        )
                                        if _po_num_t:
                                            for _tl in _tlines:
                                                _tlp2 = dict(_tl.get("_lp") or {})
                                                _tlp2["supplier_order_no"] = _po_num_t
                                                _tlp2["supplier_confirmation_no"] = _po_num_t
                                                _save_lp(str(_tl["line_id"]), _tlp2)
                                            st.toast(f"📤 PO {_po_num_t} created", icon="✅")
                                    try:
                                        from modules.backoffice.order_status_live import compute_order_status as _cos_t
                                        _cos_t({"id": odata["order_id"], "order_no": odata["order_no"],
                                                "status": odata.get("status","")}, write=True)
                                    except Exception: pass
                                    st.rerun()
                                except Exception as _te: st.error(str(_te))
                        else:
                            if _billing_ready == _total and _total > 0:
                                if st.button("💰 Open Billing",
                                             key=f"sp_bill_{_goid}_{_tsid[:6]}",
                                             type="primary", use_container_width=True):
                                    _go_to_billing(info["order_id"], info["order_no"])
                            else:
                                st.caption(f"✅ {_teyes} done")
                else:
                    # Same supplier — one combined advance button
                    _tsid2    = _top_sup_ids[0] if _top_sup_ids else ""
                    _teyes2   = "+".join(str(l.get("eye_side","")).upper() for l in _rl_assigned_top)
                    _tstages2 = [l.get("sup_stage") or "NEEDS_ORDERING" for l in _rl_assigned_top]
                    _tmax2    = max(STAGE_IDX.get(s, 0) for s in _tstages2)
                    _tnext2   = STAGES[_tmax2 + 1] if _tmax2 < len(STAGES) - 1 else None
                    if _tnext2:
                        if st.button(
                            f"▶ Advance {_teyes2} → {_tnext2[1]}",
                            key=f"top_adv_{route_filter}_{_goid}_{_gsid[:8]}",
                            use_container_width=True, type="primary"
                        ):
                            _miss_ref = _missing_supplier_refs(_rl_assigned_top, _tnext2[0])
                            if _miss_ref:
                                _show_missing_ref_stop(_miss_ref)
                            if _tnext2[0] == "READY_FOR_BILLING":
                                st.error(
                                    "Inspection route decision is compulsory. Open the Inspection Result "
                                    "section and choose 'Ready for Billing' or 'Process to Hardcoat'."
                                )
                                st.stop()
                            try:
                                import datetime as _dt_tadv2
                                from modules.sql_adapter import run_write as _rw_t2
                                for _tl2 in _rl_assigned_top:
                                    _tlp2 = dict(_tl2.get("_lp") or {})
                                    _tlp2["supplier_stage"] = _tnext2[0]
                                    if _tnext2[0] == "SUPPLIER_CONFIRMED":
                                        _tlp2["supplier_confirmed_at"] = _dt_tadv2.datetime.now().astimezone().isoformat()
                                    if _tnext2[0] == "RECEIVED":
                                        _tq2 = int(_tl2.get("quantity") or 1)
                                        _tlp2["ready_qty"] = _tq2
                                        _rw_t2("UPDATE order_lines SET ready_qty=%(rq)s WHERE id=%(lid)s::uuid",
                                               {"rq": _tq2, "lid": str(_tl2["line_id"])})
                                    _save_lp(str(_tl2["line_id"]), _tlp2)
                                # ── Auto-create PO when first confirming with supplier ──
                                if _tnext2[0] == "SUPPLIER_CONFIRMED":
                                    _sup_name_t2 = (_sup_by_id.get(_tsid2, {}).get("name", "")
                                                    or next((l.get("supplier_name","")
                                                             for l in _rl_assigned_top), ""))
                                    _po_num_t2 = _ensure_pipeline_po(
                                        _rl_assigned_top, _tsid2,
                                        _sup_name_t2,
                                        info.get("order_no", "")
                                    )
                                    if _po_num_t2:
                                        for _tl2b in _rl_assigned_top:
                                            _tlp2b = dict(_tl2b.get("_lp") or {})
                                            _tlp2b["supplier_order_no"] = _po_num_t2
                                            _tlp2b["supplier_confirmation_no"] = _po_num_t2
                                            _save_lp(str(_tl2b["line_id"]), _tlp2b)
                                        st.toast(f"📤 PO {_po_num_t2} created", icon="✅")
                                try:
                                    from modules.backoffice.order_status_live import compute_order_status as _cos_t2
                                    _cos_t2({"id": odata["order_id"], "order_no": odata["order_no"],
                                             "status": odata.get("status","")}, write=True)
                                except Exception: pass
                                st.rerun()
                            except Exception as _te2: st.error(str(_te2))
                    else:
                        if _billing_ready == _total and _total > 0:
                            _bill_key = f"hdr_bill_{_goid}_{_gsid[:8] if _gsid else 'none'}"
                            if st.button("💰 Open Billing",
                                         key=_bill_key,
                                         type="primary", use_container_width=True):
                                _go_to_billing(info["order_id"], info["order_no"])
                        else:
                            st.success("✅ All at final stage")

            # Always-visible rollback shortcut for Supplier and External Lab.
            # The detailed panel also has rollback controls, but this keeps
            # External Lab orders recoverable without hunting inside expanders.
            if _rl_assigned_top:
                try:
                    _top_stage_idxs = [
                        STAGE_IDX.get(str(l.get("sup_stage") or "NEEDS_ORDERING"), 0)
                        for l in _rl_assigned_top
                    ]
                    _top_min_idx = min(_top_stage_idxs) if _top_stage_idxs else 0
                    _top_rec_opts = STAGES[:_top_min_idx]
                    if _top_rec_opts:
                        with st.expander("◀ Set Back Stage", expanded=False):
                            _top_rec_lbls = ["Select previous stage..."] + [s[1] for s in _top_rec_opts]
                            _top_rec_codes = [None] + [s[0] for s in _top_rec_opts]
                            _top_rec_sel = st.selectbox(
                                "Previous stage",
                                _top_rec_lbls,
                                key=f"top_recede_sel_{route_filter}_{_goid}",
                                label_visibility="collapsed",
                            )
                            _top_rec_code = _top_rec_codes[_top_rec_lbls.index(_top_rec_sel)]
                            if _top_rec_code:
                                if st.button(
                                    "◀ Apply Set Back",
                                    key=f"top_recede_btn_{route_filter}_{_goid}",
                                    use_container_width=True,
                                ):
                                    for _rlb in _rl_assigned_top:
                                        _rlb_lp = dict(_rlb.get("_lp") or {})
                                        _rlb_lp["supplier_stage"] = _top_rec_code
                                        _save_lp(str(_rlb["line_id"]), _rlb_lp)
                                    st.rerun()
                except Exception as _top_rec_err:
                    st.caption(f"Set back unavailable: {_top_rec_err}")

            # ── ↩ Set Back to Backoffice (full rollback) ──────────────────────
            # Different from "◀ Set Back Stage" above (which only moves between
            # supplier stages). This cancels all job records and returns the order
            # to CONFIRMED so backoffice staff can re-edit power, product, or route.
            # Shared with in-house pipeline via production_rollback.py.
            try:
                from modules.backoffice.production_rollback import render_set_back_panel as _rsbp
                _route_label_sb = "External Lab" if _is_lab else "Supplier"
                _order_dict_sb = {
                    "id":       odata.get("order_id") or "",
                    "order_no": odata.get("order_no") or "",
                    "status":   info.get("status") or odata.get("status") or "",
                }
                _rsbp(_order_dict_sb, route_label=_route_label_sb)
            except Exception as _sb_err:
                import logging as _sb_log
                _sb_log.getLogger(__name__).debug(
                    "[supplier_pipeline] rollback panel unavailable: %s", _sb_err
                )

            try:
                import html as _html_card
                _detail_rows = []
                for _dl in sorted(
                    lines,
                    key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1
                ):
                    _eye_d = str(_dl.get("eye_side") or "").upper() or "B"
                    _pwr_d = _power_str(_dl) or "Power not entered"
                    _qty_d = int(_dl.get("quantity") or 1)
                    _stage_d = STAGE_LABEL.get(_dl.get("sup_stage") or "NEEDS_ORDERING",
                                               _dl.get("sup_stage") or "NEEDS_ORDERING")
                    _detail_rows.append(
                        f"<div style='padding:3px 0;border-top:1px solid #1e293b'>"
                        f"<b style='color:#e2e8f0'>{_html_card.escape(_eye_d)}</b> "
                        f"<span style='color:#cbd5e1'>{_html_card.escape(str(_dl.get('product_name') or '').split(' | ')[0])}</span> "
                        f"<span style='color:#94a3b8'>· {_html_card.escape(_pwr_d)} · Qty {_qty_d} · {_html_card.escape(_stage_d)}</span>"
                        f"</div>"
                    )
                if _detail_rows:
                    st.markdown(
                        "<div style='background:#0f172a;border:1px solid #1e293b;"
                        "border-radius:6px;padding:7px 10px;margin:4px 0 8px 0;"
                        "font-size:0.78rem'>"
                        + "".join(_detail_rows) + "</div>",
                        unsafe_allow_html=True,
                    )
            except Exception as _e:
                pass

            with st.expander("🔍 Details / WhatsApp / Settings", expanded=not bool(_rl_assigned_top)):
                def _eye_sort(x):
                    _e = str(x.get('eye_side', '')).upper()
                    if _e in ('R', 'RIGHT'): return 0
                    if _e in ('L', 'LEFT'):  return 1
                    return 2

                # ── Smart R+L grouping ─────────────────────────────────────────
                # If R and L have the same supplier → combine into one block
                # If different suppliers → show per-line (existing loop below)
                _rl_detail = sorted(
                    [l for l in lines if str(l.get("eye_side","")).upper() in ("R","L","RIGHT","LEFT")],
                    key=_eye_sort
                )
                _other_detail = [l for l in lines if str(l.get("eye_side","")).upper() not in
                                  ("R","L","RIGHT","LEFT")]

                _d_r = next((l for l in _rl_detail if str(l.get("eye_side","")).upper() in ("R","RIGHT")), None)
                _d_l = next((l for l in _rl_detail if str(l.get("eye_side","")).upper() in ("L","LEFT")), None)
                _d_r_sup = str((_d_r or {}).get("supplier_id",""))
                _d_l_sup = str((_d_l or {}).get("supplier_id",""))
                _d_same_sup = bool(_d_r_sup and _d_l_sup and _d_r_sup == _d_l_sup)

                # Toggle: allow staff to split if needed
                _split_key = f"sup_det_split_{_goid[:8]}"
                if _d_r and _d_l and _d_same_sup:
                    _force_split_det = st.checkbox(
                        "Show R and L separately",
                        value=st.session_state.get(_split_key, False),
                        key=_split_key,
                        help="Check only if R and L need different actions"
                    )
                else:
                    _force_split_det = True  # different suppliers → always split

                if _d_r and _d_l and _d_same_sup and not _force_split_det:
                    # ── COMBINED R+L block ─────────────────────────────────────
                    _cb_sup   = _d_r.get("supplier_name","—")
                    _cb_mob   = _d_r.get("supplier_mobile","")
                    _cb_stage = _d_r.get("sup_stage") or "NEEDS_ORDERING"
                    _cb_stg_c = _stage_color(_cb_stage)
                    _cb_stg_l = STAGE_LABEL.get(_cb_stage, _cb_stage)
                    _cb_lp_r  = dict(_d_r.get("_lp") or {})
                    _cb_lp_l  = dict(_d_l.get("_lp") or {})
                    _cb_pwr_r = _power_str(_d_r)
                    _cb_pwr_l = _power_str(_d_l)
                    _cb_prod  = str(_d_r.get("product_name","")).split(" | ")[0]

                    # Header
                    st.markdown(
                        f"<div style='background:#0f172a;border:1px solid #1e293b;"
                        f"border-left:3px solid {_cb_stg_c};border-radius:6px;"
                        f"padding:8px 12px;margin-bottom:6px'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                        f"<span style='color:#e2e8f0;font-weight:700'>R+L &nbsp; {_cb_prod}</span>"
                        f"<span style='background:{_cb_stg_c}22;color:{_cb_stg_c};"
                        f"font-size:0.72rem;font-weight:700;padding:2px 9px;border-radius:4px'>"
                        f"{_cb_stg_l}</span></div>"
                        f"<div style='color:#64748b;font-size:0.75rem;margin-top:4px'>"
                        f"RE: {_cb_pwr_r} &nbsp;·&nbsp; LE: {_cb_pwr_l}</div>"
                        f"<div style='color:#94a3b8;font-size:0.75rem'>🏭 {_cb_sup}"
                        + (f" &nbsp;·&nbsp; {_cb_mob}" if _cb_mob else "")
                        + "</div></div>",
                        unsafe_allow_html=True,
                    )

                    # Supplier ref / PO number (applies to both)
                    _cb_ref = str(_cb_lp_r.get("supplier_order_no","") or _d_r.get("live_po_no","") or "")
                    _cb_ref_new = st.text_input(
                        "Supplier Ref / PO",
                        value=_cb_ref,
                        key=f"cb_ref_{_goid[:8]}",
                        placeholder="Supplier order / ref number",
                    )
                    if _cb_ref_new != _cb_ref and _cb_ref_new.strip():
                        if st.button("💾 Save Ref", key=f"cb_save_ref_{_goid[:8]}",
                                     use_container_width=True):
                            import datetime as _dt_cb
                            from modules.sql_adapter import run_write as _rw_cb
                            for _cbl in [_d_r, _d_l]:
                                _cb_lp_tmp = dict(_cbl.get("_lp") or {})
                                _cb_lp_tmp["supplier_order_no"] = _cb_ref_new.strip()
                                _cb_lp_tmp["supplier_confirmation_no"] = _cb_ref_new.strip()
                                # Promote stage so the line enters Procurement Queue.
                                # Without this, lines sat at ORDER_PLACED forever and
                                # never appeared in the queue — the supplier ref was
                                # saved but the gate didn't open. Only flip if still
                                # at an early stage (don't downgrade later stages).
                                _cur_cb_stage = str(_cb_lp_tmp.get("supplier_stage","") or "").upper()
                                if _cur_cb_stage in ("", "NEEDS_ORDERING", "ORDER_PLACED"):
                                    _cb_lp_tmp["supplier_stage"] = "SUPPLIER_CONFIRMED"
                                    _cb_lp_tmp["supplier_confirmed_at"] = _dt_cb.datetime.now().astimezone().isoformat()
                                _save_lp(str(_cbl["line_id"]), _cb_lp_tmp)
                            # Preserve current tab — staff just saved a ref, they
                            # don't want to be bounced to Dashboard.
                            try:
                                _cur_panel = st.session_state.get("prod_lazy_panel", "")
                                if _cur_panel:
                                    st.session_state["_prod_lazy_panel_next"] = _cur_panel
                            except Exception:
                                pass
                            st.success("✅ Ref saved · R+L moved to Procurement Queue (SUPPLIER_CONFIRMED)")
                            st.rerun()

                    # Purchase entry is centralized in Procurement Queue. The old
                    # combined R+L purchase form is disabled to avoid duplicate
                    # purchase records from supplier advancement.
                    _pa_db_r = _check_purchase_acked(str(_d_r.get("line_id") or ""))
                    _pa_db_l = _check_purchase_acked(str(_d_l.get("line_id") or ""))
                    if _pa_db_r or _pa_db_l:
                        st.success("✅ Purchase already recorded in Procured")
                    else:
                        st.info("📋 Purchase entry is handled in **📥 Procurement Queue** after receiving.")
                        if st.button("Open Procurement Queue", key=f"cb_open_prx_{_goid[:8]}", use_container_width=True):
                            st.session_state["_prod_lazy_panel_next"] = "📥 Procurement Queue"
                            st.rerun()

                    if False:
                        _rpa1, _rpa2, _rpa3 = st.columns([3, 2, 2])
                        _pa_chal_new = _rpa1.text_input("Challan / Invoice No",
                                                         value=_pa_challan,
                                                         key=f"cb_pa_chal_{_goid[:8]}")
                        _pa_price_new = _rpa2.number_input("Unit Price ₹",
                                                             value=_pa_price, min_value=0.0, step=0.5,
                                                             key=f"cb_pa_price_{_goid[:8]}")
                        _pa_qty_total = int(_d_r.get("quantity",1)) + int(_d_l.get("quantity",1))
                        _rpa3.metric("Total Qty", _pa_qty_total)
                        if st.button("✅ Confirm Purchase R+L", key=f"cb_pa_save_{_goid[:8]}",
                                     type="primary", use_container_width=True):
                            from modules.sql_adapter import run_write as _rw_pa
                            for _cbl2 in [_d_r, _d_l]:
                                _pa_lp = dict(_cbl2.get("_lp") or {})
                                _pa_lp["pa_acked"]      = True
                                _pa_lp["purchase_acked"]= True
                                _pa_lp["pa_challan_no"] = _pa_chal_new.strip()
                                _pa_lp["pa_price"]      = _pa_price_new
                                _pa_lp["purchase_price"]= _pa_price_new
                                _pa_lp["supplier_stage"]= "READY_FOR_BILLING"
                                _save_lp(str(_cbl2["line_id"]), _pa_lp)
                            st.success("✅ Purchase confirmed for R+L → Ready for Billing")
                            st.rerun()

                    # Advancement is handled by the single R+L control block
                    # below. Keep this detail section read-only to avoid three
                    # competing advance buttons on the same order card.
                    _cb_cur_idx = STAGE_IDX.get(_cb_stage, 0)
                    _cb_next    = STAGES[_cb_cur_idx + 1] if _cb_cur_idx < len(STAGES) - 1 else None
                    if _cb_next:
                        st.caption(f"Next: {_cb_next[1]} · use the R+L advancement controls below.")
                    if False and _cb_next:
                        if st.button(
                            f"▶ Advance R+L → {_cb_next[1]}",
                            key=f"cb_adv_{_goid[:8]}",
                            type="primary", use_container_width=True
                        ):
                            from modules.sql_adapter import run_write as _rw_adv
                            for _cbl3 in [_d_r, _d_l]:
                                _adv_lp = dict(_cbl3.get("_lp") or {})
                                _adv_lp["supplier_stage"] = _cb_next[0]
                                if _cb_next[0] == "RECEIVED":
                                    _adv_qty = int(_cbl3.get("quantity",1))
                                    _adv_lp["ready_qty"] = _adv_qty
                                    _rw_adv("UPDATE order_lines SET ready_qty=%(rq)s WHERE id=%(lid)s::uuid",
                                            {"rq": _adv_qty, "lid": str(_cbl3["line_id"])})
                                _save_lp(str(_cbl3["line_id"]), _adv_lp)
                            if _cb_next[0] == "SUPPLIER_CONFIRMED":
                                _po_cb = _ensure_pipeline_po(
                                    [_d_r, _d_l], _d_r_sup,
                                    _cb_sup, info.get("order_no","")
                                )
                                if _po_cb:
                                    st.toast(f"📤 PO {_po_cb} created", icon="✅")
                            st.rerun()

                    # WhatsApp is also available in the single R+L control
                    # block below; suppress the old duplicate link here.
                    if False and _cb_mob:
                        import urllib.parse as _cb_up
                        _wa_d_cb = "".join(d for d in _cb_mob if d.isdigit())
                        if _wa_d_cb.startswith("91") and len(_wa_d_cb)==12: _wa_d_cb=_wa_d_cb[2:]
                        if len(_wa_d_cb)==10:
                            _nl = "\n"
                            _wa_msg_cb = (
                                "Dear " + _cb_sup + "," + _nl
                                + "Order: " + info["order_no"] + _nl
                                + "RE: " + _cb_pwr_r + _nl
                                + "LE: " + _cb_pwr_l + _nl
                                + "Product: " + _cb_prod + _nl
                                + "Qty: " + str(_pa_qty_total) + _nl + _nl
                                + "Parakh Optical"
                            )
                            st.link_button(
                                "📲 WhatsApp Supplier",
                                f"https://wa.me/91{_wa_d_cb}?text={_cb_up.quote(_wa_msg_cb)}",
                                use_container_width=True,
                            )

                    # Other lines (frames etc.) go through the main loop below
                else:
                    # Different suppliers or forced split — all lines go through main loop
                    _other_detail = sorted(lines, key=_eye_sort)
                for line in _other_detail:
                    _lid      = str(line["line_id"])
                    _eye      = str(line.get("eye_side") or "").upper()
                    _pname    = str(line.get("product_name") or "").split(" | ")[0]
                    _needed   = int(line.get("quantity") or 1)
                    _ready    = int(line.get("ready_qty") or 0)
                    _supp     = str(line.get("supplier_name") or "—")
                    _sup_mob  = str(line.get("supplier_mobile") or "")
                    _stage    = line.get("sup_stage") or "NEEDS_ORDERING"
                    _sup_ono  = line.get("sup_order_no") or ""
                    _lp       = dict(line.get("_lp") or {})
                    _pwr      = _power_str(line)
                    _eye_lbl  = (f"👁 {_eye}" if _eye and _eye not in ("O","OTHER","") else "🖼")
                    _stg_clr  = _stage_color(_stage)
                    _stg_lbl  = STAGE_LABEL.get(_stage, _stage)
                    # Live PO status from supplier_orders (batch-fetched above)
                    _live_po_no = line.get("live_po_no", "")
                    _live_po_st = line.get("live_po_status", "")
                    _po_clr     = {"DRAFT":"#64748b","SENT":"#3b82f6","CONFIRMED":"#10b981",
                                   "RECEIVED":"#22c55e","PARTIAL":"#f59e0b"}.get(_live_po_st,"#475569")

                    # ── Skip line if already billed — use order-level result if available ──
                    _line_billed = _all_billed  # fast path: if whole order is billed, all lines are
                    if not _all_billed:
                        # Partial billing — check this specific line
                        try:
                            from modules.sql_adapter import run_query as _rq_lb
                            _lb = _rq_lb("""
                                SELECT 1 FROM challan_lines cl
                                JOIN challans c ON c.id = cl.challan_id
                                WHERE cl.order_line_id = %(lid)s::uuid
                                  AND c.status NOT IN ('CANCELLED','VOID')
                                LIMIT 1
                            """, {"lid": _lid})
                            _line_billed = bool(_lb)
                        except Exception as _e:
                            pass

                    if _line_billed:
                        st.markdown(
                            f"<div style='padding:4px 10px;font-size:0.78rem;"
                            f"color:#22c55e;border-left:3px solid #22c55e;"
                            f"margin:2px 0'>🧾 {_eye_lbl} {_pname} — Billed 🔒</div>",
                            unsafe_allow_html=True
                        )
                        continue

                    # ── Eye colour theme: RE = red, LE = dark slate ───────
                    _is_re = _eye in ("R", "RIGHT")
                    _is_le = _eye in ("L", "LEFT")
                    if _is_re:
                        _eye_accent   = "#ef4444"
                        _eye_bg       = "#ef444412"
                        _eye_border   = "#ef4444"
                        _eye_txt      = "#fca5a5"
                        _eye_badge_bg = "#7f1d1d"
                        _eye_label    = "R"
                    elif _is_le:
                        _eye_accent   = "#94a3b8"
                        _eye_bg       = "#1e293b"
                        _eye_border   = "#475569"
                        _eye_txt      = "#cbd5e1"
                        _eye_badge_bg = "#0f172a"
                        _eye_label    = "L"
                    else:
                        _eye_accent   = "#64748b"
                        _eye_bg       = "#1e293b"
                        _eye_border   = "#334155"
                        _eye_txt      = "#94a3b8"
                        _eye_badge_bg = "#1e293b"
                        _eye_label    = _eye or "—"

                    # For External Lab: pre-compute supplier product name
                    _sup_pname_mapped = ""
                    if _is_lab:
                        try:
                            from modules.backoffice.supplier_product_map_ui import get_supplier_product_name as _gspn
                            _spm_r = _gspn(
                                str(line.get("product_id") or ""),
                                str(line.get("supplier_id") or "")
                            )
                            _sup_pname_mapped = _spm_r.get("supplier_product_name","")
                        except Exception as _e:
                            pass

                    # ── Line header ───────────────────────────────────────
                    st.markdown(
                        f"<div style='border:1px solid {_eye_border};"
                        f"border-left:5px solid {_eye_accent};"
                        f"border-radius:6px;padding:8px 12px;margin-bottom:6px;"
                        f"background:{_eye_bg}'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                        f"<span style='display:flex;align-items:center;gap:8px'>"
                        f"<span style='background:{_eye_badge_bg};border:1px solid {_eye_accent};"
                        f"color:{_eye_accent};font-size:0.7rem;font-weight:800;padding:1px 8px;"
                        f"border-radius:4px;letter-spacing:.06em'>{_eye_label}E</span>"
                        f"<span style='color:{_eye_txt};font-weight:700'>{_pname}"
                        + (f" <code style='font-size:0.72rem;color:{_eye_accent}'>{_pwr}</code>" if _pwr else "")
                        + (f" <span style='color:#a78bfa;font-size:0.72rem'>→ {_sup_pname_mapped}</span>"
                           if _sup_pname_mapped else "")
                        + f"</span></span>"
                        f"<span style='background:{_stg_clr}22;color:{_stg_clr};"
                        f"font-size:0.7rem;font-weight:700;padding:2px 8px;"
                        f"border-radius:10px'>{_stg_lbl}</span>"
                        f"</div>"
                        f"<div style='color:#64748b;font-size:0.75rem;margin-top:3px'>"
                        f"Supplier: <b style='color:{_eye_txt}'>{_supp}</b>"
                        + (f" · Ref: <b style='color:{_eye_txt}'>{_sup_ono}</b>" if _sup_ono else "")
                        + (f" · 📤 PO: <b style='color:#a78bfa'>{_live_po_no}</b>"
                           f" <span style='background:{_po_clr}22;color:{_po_clr};"
                           f"font-size:0.65rem;font-weight:700;padding:1px 6px;"
                           f"border-radius:8px'>{_live_po_st}</span>"
                           if _live_po_no else "")
                        + f" · {_ready}/{_needed} pcs</div></div>",
                        unsafe_allow_html=True
                    )

                    # ── Supplier / Lab assignment — READ ONLY ─────────────────────
                    # Assignment is done via ✏️ Assignment Workspace (not here).
                    _supp_disp = str(line.get("supplier_name") or "").strip()
                    _lbl_party = "Lab" if _is_lab else "Supplier"
                    if _supp_disp and _supp_disp not in ("—", ""):
                        st.markdown(
                            f"<div style='background:#0f1a0f;border:1px solid #22c55e33;"
                            f"border-radius:6px;padding:5px 12px;margin:4px 0;"
                            f"font-size:0.78rem;color:#86efac'>"
                            f"✅ {_lbl_party}: <b>{_supp_disp}</b></div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        _wa_col, _btn_col = st.columns([3, 2])
                        with _wa_col:
                            st.markdown(
                                f"<div style='background:#1a0a00;border:1px solid #f9731633;"
                                f"border-radius:6px;padding:5px 12px;margin:4px 0;"
                                f"font-size:0.78rem;color:#fb923c'>"
                                f"⚠️ {_lbl_party} not assigned — open Assignment Workspace</div>",
                                unsafe_allow_html=True,
                            )
                        with _btn_col:
                            if st.button(
                                "✏️ Assign",
                                key=f"ws_open_line_{_lid}",
                                use_container_width=True,
                                help="Open Assignment Workspace for this order",
                            ):
                                for _sk in list(st.session_state.keys()):
                                    if _sk.startswith("aw_r_") or _sk.startswith("aw_l_") or _sk.startswith("radio_aw_"):
                                        del st.session_state[_sk]
                                st.session_state["prod_assign_order_no"] = info["order_no"]
                                st.session_state["prod_view_mode"] = "assign"
                                # Sidecar key — direct write to prod_lazy_panel
                                # is rejected after that widget is instantiated
                                # in production_page.py. Reader consumes this
                                # key on next rerun and seeds the widget.
                                st.session_state["_prod_lazy_panel_next"] = "🧪 External Supplier" if _is_lab else "🏭 Supplier"
                                st.rerun()

                    # ACTION: Supplier order number input
                    if _stage in ("NEEDS_ORDERING","ORDER_PLACED","SUPPLIER_CONFIRMED","AWAITING_SUPPLY"):
                        _new_ono = st.text_input(
                            "Supplier Ref No.",
                            value=_sup_ono,
                            placeholder="Enter supplier's ref / order no.",
                            key=f"sup_ono_{_lid}",
                            label_visibility="collapsed"
                        )
                        if _new_ono != _sup_ono:
                            try:
                                import datetime as _dt_sp
                                _lp["supplier_order_no"] = _new_ono
                                _lp["supplier_confirmation_no"] = _new_ono
                                if _new_ono and _stage in ("NEEDS_ORDERING", "ORDER_PLACED", "SUPPLIER_CONFIRMED"):
                                    _lp["supplier_stage"] = "AWAITING_SUPPLY"
                                    _lp["supplier_confirmed_at"] = _dt_sp.datetime.now().astimezone().isoformat()
                                _save_lp(_lid, _lp)
                                # ── Auto-create PO when ref entered (order confirmed) ─
                                if _new_ono and _stage in ("NEEDS_ORDERING", "ORDER_PLACED"):
                                    _po_num_ref = _ensure_pipeline_po(
                                        [line],
                                        str(line.get("supplier_id") or _lp.get("supplier_id") or ""),
                                        _supp,
                                        info.get("order_no", "")
                                    )
                                    if _po_num_ref:
                                        # Only stamp if supplier_order_no was blank (don't overwrite PO no)
                                        if not _sup_ono:
                                            _lp["supplier_order_no"] = _po_num_ref
                                            _save_lp(_lid, _lp)
                                        st.toast(f"📤 PO {_po_num_ref} raised", icon="✅")
                                st.rerun()
                            except Exception as _re: st.error(str(_re))

                    # Received qty (for RECEIVED stage)
                    if _stage in ("AWAITING_SUPPLY","RECEIVED"):
                        _recv = st.number_input(
                            "Qty received",
                            min_value=0, max_value=_needed, value=_ready, step=1,
                            key=f"recv_{_lid}", label_visibility="collapsed"
                        )
                        if _recv != _ready:
                            if st.button("✅ Update Received", key=f"recv_btn_{_lid}"):
                                try:
                                    from modules.sql_adapter import run_write as _rw3
                                    _rw3("UPDATE order_lines SET ready_qty=%(rq)s WHERE id=%(lid)s::uuid",
                                         {"rq": _recv, "lid": _lid})
                                    _lp["supplier_stage"] = "RECEIVED" if _recv >= _needed else "AWAITING_SUPPLY"
                                    _save_lp(_lid, _lp)
                                    # Sync order status
                                    try:
                                        from modules.backoffice.order_status_live import compute_order_status as _cos2
                                        _cos2({"id": odata["order_id"], "order_no": odata["order_no"],
                                               "status": odata.get("status","")}, write=True)
                                    except Exception: pass
                                    st.rerun()
                                except Exception as _re: st.error(str(_re))

                    # ── Purchase handoff ─────────────────────────────────────
                    # Purchase recording is centralized in Procurement Queue /
                    # Procured. Keep supplier advancement focused on
                    # supplier status only, so staff do not enter purchase data
                    # in two different places.
                    if _stage in ("RECEIVED", "INSPECTION", "READY_FOR_BILLING"):
                        _pa_db = _check_purchase_acked(_lid)
                        if _pa_db:
                            st.success(
                                "✅ Purchase already recorded in Procurement"
                                + (f" · {_pa_db.get('challan_no') or _pa_db.get('invoice_no') or ''}")
                            )
                        else:
                            st.info(
                                "📋 Purchase entry is handled in **📥 Procurement Queue**. "
                                "Use this supplier panel only for order placement, follow-up, receiving, and inspection."
                            )
                            if st.button("Open Procurement Queue", key=f"open_prx_{_lid}", use_container_width=True):
                                st.session_state["_prod_lazy_panel_next"] = "📥 Procurement Queue"
                                st.rerun()

                    # Legacy inline purchase form retired. Left disabled for
                    # rollback safety; Procurement RX is now the source of truth.
                    if False and _stage in ("RECEIVED", "INSPECTION", "READY_FOR_BILLING"):
                        # DB is source of truth — ignore lens_params JSON
                        _pa_db = _check_purchase_acked(_lid)
                        _is_locked = bool(_pa_db.get("is_price_locked"))
                        _rp_key = f"rp_open_{_lid}"
                        if _pa_db:
                            _rp_lbl = (
                                f"{'🔒' if _is_locked else '✅'} Purchase Acked"
                                + (f" — {_pa_db.get('challan_no') or _pa_db.get('invoice_no','')}"
                                   if (_pa_db.get('challan_no') or _pa_db.get('invoice_no')) else "")
                                + (f" · ₹{float(_pa_db.get('purchase_price') or 0):,.2f}/pc"
                                   if _pa_db.get('purchase_price') else "")
                                + (" 🔒 LOCKED" if _is_locked else "")
                            )
                        else:
                            _rp_lbl = "📋 Record Purchase (required before billing)"
                        with st.expander(_rp_lbl, expanded=False):
                            st.markdown(
                                "<div style='background:#0f172a;border-left:3px solid #f59e0b;"
                                "border-radius:0 6px 6px 0;padding:8px 14px;margin-bottom:8px'>"
                                "<span style='color:#f59e0b;font-weight:700;font-size:0.8rem'>"
                                "📋 Purchase Reference</span>"
                                "<div style='color:#64748b;font-size:0.72rem;margin-top:2px'>"
                                "Records supplier challan/invoice. Not a full purchase entry — "
                                "used as procurement reference. Fills price from stock master.</div>"
                                "</div>",
                                unsafe_allow_html=True
                            )

                            # Auto-fill purchase price from DB (pa_db is source of truth)
                            _rp_auto_price = float(_pa_db.get("purchase_price") or 0)
                            if _rp_auto_price <= 0:
                                try:
                                    from modules.sql_adapter import run_query as _rq_rp
                                    _pp_row = _rq_rp("""
                                        SELECT
                                            COALESCE(purchase_price, 0)::numeric AS purchase_price
                                        FROM inventory_stock
                                        WHERE product_id = (
                                            SELECT product_id FROM order_lines
                                            WHERE id = %(lid)s::uuid LIMIT 1
                                        )
                                          AND COALESCE(is_active, TRUE) = TRUE
                                        ORDER BY created_at DESC
                                        LIMIT 1
                                    """, {"lid": _lid}) or []
                                    if _pp_row:
                                        _rp_auto_price = float(_pp_row[0].get("purchase_price") or 0)
                                except Exception as _e:
                                    pass

                            import datetime as _dt_rp
                            _rpc1, _rpc2 = st.columns(2)
                            with _rpc1:
                                _rp_challan = st.text_input(
                                    "Challan No.",
                                    value=_pa_db.get("challan_no",""),
                                    placeholder="e.g. CH-2526/001",
                                    key=f"rp_challan_{_lid}",
                                    label_visibility="collapsed"
                                )
                                _rp_invoice = st.text_input(
                                    "Invoice No.",
                                    value=_pa_db.get("invoice_no",""),
                                    placeholder="e.g. INV/2025-26/001",
                                    key=f"rp_invoice_{_lid}",
                                    label_visibility="collapsed"
                                )
                            with _rpc2:
                                _rp_date = st.date_input(
                                    "Document Date",
                                    value=_dt_rp.date.today(),
                                    key=f"rp_date_{_lid}",
                                    format="DD/MM/YYYY",
                                    label_visibility="collapsed"
                                )
                                _rp_price = st.number_input(
                                    "Purchase Price ₹ (per pc)",
                                    min_value=0.0,
                                    value=float(_pa_db.get("purchase_price") or _rp_auto_price),
                                    step=1.0,
                                    format="%.2f",
                                    key=f"rp_price_{_lid}",
                                    disabled=_is_locked,
                                    help="🔒 Locked after purchase invoice" if _is_locked else "Auto-filled from stock master. Edit if needed."
                                )
                                _rp_recv_qty = st.number_input(
                                    "Qty Received",
                                    min_value=0,
                                    max_value=_needed,
                                    value=int(_pa_db.get("received_qty") or _needed),
                                    step=1,
                                    key=f"rp_recv_{_lid}",
                                    disabled=_is_locked,
                                    help="Actual qty received from supplier (cannot exceed ordered qty)"
                                )

                            # Supplier is auto-filled from line
                            _rp_supp_name = str(_supp or "—")
                            st.markdown(
                                f"<div style='background:#0f172a;border:1px solid #1e293b;"
                                f"border-radius:6px;padding:6px 12px;margin:4px 0;"
                                f"font-size:0.75rem;color:#64748b'>"
                                f"Supplier: <b style='color:#e2e8f0'>{_rp_supp_name}</b>"
                                f"{'  ·  ' if _rp_auto_price > 0 else ''}"
                                + (f"Stock price: <b style='color:#f59e0b'>₹{_rp_auto_price:,.2f}</b>" if _rp_auto_price > 0 else "")
                                + "</div>",
                                unsafe_allow_html=True
                            )

                            _rp_notes = st.text_input(
                                "Notes (optional)",
                                value=_pa_db.get("notes",""),
                                placeholder="e.g. Partial supply, balance pending...",
                                key=f"rp_notes_{_lid}",
                                label_visibility="collapsed"
                            )

                            _rpb1, _rpb2 = st.columns([3, 2])
                            with _rpb1:
                                _rp_has_price = float(_rp_price or 0) > 0
                                _rp_can_save = bool(_rp_challan.strip() or _rp_invoice.strip()) and _rp_has_price
                                if not _rp_has_price:
                                    st.error("❌ Purchase price is required before saving this purchase record.")
                                if st.button(
                                    "💾 Save Purchase Record",
                                    key=f"rp_save_{_lid}",
                                    type="primary",
                                    use_container_width=True,
                                    disabled=not _rp_can_save,
                                    help="Enter challan/invoice number and purchase price to save"
                                ):
                                    try:
                                        # DB is the only source of truth — no JSON write
                                        # Write to purchase_acknowledgements table
                                        # Table created by migration — not at runtime
                                        # Run procurement_migration_v3.sql if table missing

                                        try:
                                            from modules.sql_adapter import run_write as _rw_pa2, run_query as _rq_pa2
                                            # Get product_id for this line
                                            _line_meta = _rq_pa2(
                                                "SELECT product_id::text, order_id::text FROM order_lines "
                                                "WHERE id=%(lid)s::uuid LIMIT 1", {"lid": _lid}
                                            )
                                            _prod_id = (_line_meta[0].get("product_id") if _line_meta else None)
                                            _ord_id  = (_line_meta[0].get("order_id") if _line_meta else None)
                                            # Supplier ID: prefer DB parties table lookup over JSON
                                            # JSON supplier_id may be stale if supplier was reassigned
                                            _sup_id_raw = ""
                                            _sup_lp_id = str(_lp.get("supplier_id") or "")
                                            if _sup_lp_id and len(_sup_lp_id) == 36:
                                                # Validate it actually exists in parties
                                                try:
                                                    _sup_valid = _rq_pa2(
                                                        "SELECT id::text FROM parties "
                                                        "WHERE id=%(sid)s::uuid LIMIT 1",
                                                        {"sid": _sup_lp_id}
                                                    )
                                                    if _sup_valid:
                                                        _sup_id_raw = _sup_lp_id
                                                except Exception as _e:
                                                    pass
                                            if not _sup_id_raw and _rp_supp_name and _rp_supp_name != "—":
                                                # Fall back to name lookup in parties
                                                try:
                                                    _sup_by_name = _rq_pa2(
                                                        "SELECT id::text FROM parties "
                                                        "WHERE UPPER(party_name)=UPPER(%(n)s) "
                                                        "LIMIT 1",
                                                        {"n": _rp_supp_name}
                                                    )
                                                    if _sup_by_name:
                                                        _sup_id_raw = _sup_by_name[0]["id"]
                                                except Exception as _e:
                                                    pass
                                            _rw_pa2("""
                                                INSERT INTO purchase_acknowledgements (
                                                    order_line_id, order_id, order_no,
                                                    product_id, product_name, eye_side,
                                                    supplier_id, supplier_name,
                                                    challan_no, invoice_no, document_date,
                                                    qty, received_qty,
                                                    purchase_price, total_value, notes,
                                                    supplier_product_name, supplier_product_code,
                                                    supplier_product_description,
                                                    our_product_name, our_product_id, mapping_source,
                                                    billing_status, acknowledged_at
                                                ) VALUES (
                                                    %(lid)s::uuid, %(oid)s::uuid, %(ono)s,
                                                    %(pid)s::uuid, %(pname)s, %(eye)s,
                                                    %(sid)s::uuid, %(sname)s,
                                                    %(chal)s, %(inv)s, %(ddate)s::date,
                                                    %(qty)s, %(rqty)s,
                                                    %(price)s, %(total)s, %(notes)s,
                                                    NULLIF(%(sp_name)s,''), NULLIF(%(sp_code)s,''),
                                                    NULLIF(%(sp_desc)s,''),
                                                    NULLIF(%(our_pname)s,''),
                                                    NULLIF(%(our_pid)s,'')::uuid, NULLIF(%(map_src)s,''),
                                                    'NOT_READY', NOW()
                                                )
                                                ON CONFLICT (order_line_id) DO UPDATE SET
                                                    challan_no      = EXCLUDED.challan_no,
                                                    invoice_no      = EXCLUDED.invoice_no,
                                                    document_date   = EXCLUDED.document_date,
                                                    received_qty    = EXCLUDED.received_qty,
                                                    supplier_product_name        = COALESCE(NULLIF(EXCLUDED.supplier_product_name,''),        purchase_acknowledgements.supplier_product_name),
                                                    supplier_product_code        = COALESCE(NULLIF(EXCLUDED.supplier_product_code,''),        purchase_acknowledgements.supplier_product_code),
                                                    supplier_product_description = COALESCE(NULLIF(EXCLUDED.supplier_product_description,''), purchase_acknowledgements.supplier_product_description),
                                                    our_product_name             = COALESCE(NULLIF(EXCLUDED.our_product_name,''),             purchase_acknowledgements.our_product_name),
                                                    our_product_id               = COALESCE(EXCLUDED.our_product_id,                          purchase_acknowledgements.our_product_id),
                                                    mapping_source               = COALESCE(NULLIF(EXCLUDED.mapping_source,''),               purchase_acknowledgements.mapping_source),
                                                    purchase_price  = CASE
                                                        WHEN purchase_acknowledgements.is_price_locked
                                                        THEN purchase_acknowledgements.purchase_price
                                                        ELSE EXCLUDED.purchase_price
                                                    END,
                                                    total_value     = CASE
                                                        WHEN purchase_acknowledgements.is_price_locked
                                                        THEN purchase_acknowledgements.total_value
                                                        ELSE EXCLUDED.total_value
                                                    END,
                                                    notes           = EXCLUDED.notes,
                                                    billing_status  = CASE
                                                        WHEN purchase_acknowledgements.is_price_locked
                                                        THEN purchase_acknowledgements.billing_status
                                                        ELSE 'NOT_READY'
                                                    END,
                                                    acknowledged_at = NOW()
                                            """, {
                                                "lid":   _lid,
                                                "oid":   _ord_id or "00000000-0000-0000-0000-000000000000",
                                                "ono":   odata.get("order_no",""),
                                                "pid":   _prod_id or "00000000-0000-0000-0000-000000000000",
                                                "pname": _pname,
                                                "eye":   _eye,
                                                "sid":   _sup_id_raw if len(_sup_id_raw)==36 else "00000000-0000-0000-0000-000000000000",
                                                "sname": _rp_supp_name,
                                                "chal":  _rp_challan.strip(),
                                                "inv":   _rp_invoice.strip(),
                                                "ddate": str(_rp_date),
                                                "qty":   _needed,
                                                "rqty":  int(_rp_recv_qty),
                                                "price": float(_rp_price),
                                                "total": round(float(_rp_price) * int(_rp_recv_qty), 2),
                                                "notes": _rp_notes.strip(),
                                                # ── product identity (migration 0009) ──
                                                # _sup_pname_mapped already fetched above
                                                # from supplier_product_map for _is_lab.
                                                "sp_name":  (_sup_pname_mapped or "").strip(),
                                                "sp_code":  "",
                                                "sp_desc":  " · ".join(filter(None, [
                                                    (_sup_pname_mapped or "").strip(),
                                                    f"{_pname}" if _pname else "",
                                                    f"{_eye}" if _eye else "",
                                                ])) or "",
                                                "our_pname": (_pname or "").strip(),
                                                "our_pid":  str(_prod_id or "").strip(),
                                                "map_src":  "supplier_product_map" if (_sup_pname_mapped or "").strip() else "",
                                            })
                                        except Exception as _pa_e:
                                            st.error(f"❌ Purchase save failed: {_pa_e}")
                                            return

                                        st.success(
                                            f"✅ Purchase acknowledged — "
                                            + (f"Challan {_rp_challan} " if _rp_challan else "")
                                            + (f"Invoice {_rp_invoice}" if _rp_invoice else "")
                                        )
                                        st.rerun()
                                    except Exception as _rpe: st.error(f"Save failed: {_rpe}")
                            with _rpb2:
                                # Clear from DB only (JSON no longer source of truth)
                                _pa_exists = bool(_pa_db)
                                if _pa_exists and not _is_locked:
                                    if st.button("🗑 Clear", key=f"rp_clear_{_lid}",
                                                 use_container_width=True,
                                                 help="Remove purchase acknowledgement (only if not invoiced)"):
                                        try:
                                            from modules.sql_adapter import run_write as _rw_clr
                                            _rw_clr(
                                                "DELETE FROM purchase_acknowledgements "
                                                "WHERE order_line_id = %(lid)s::uuid "
                                                "AND COALESCE(is_price_locked, FALSE) = FALSE",
                                                {"lid": _lid}
                                            )
                                            st.rerun()
                                        except Exception as _rpe2: st.error(str(_rpe2))
                                elif _is_locked:
                                    st.caption("🔒 Locked")

                    st.markdown("") # spacing

                    # ── Status communication templates (stages 1-3) ──────────────
                    if _stage in ("NEEDS_ORDERING", "ORDER_PLACED", "AWAITING_SUPPLY", "SUPPLIER_CONFIRMED"):
                        _wa_mob_clean = "".join(x for x in _sup_mob if x.isdigit())
                        if _wa_mob_clean.startswith("91") and len(_wa_mob_clean)==12:
                            _wa_mob_clean = _wa_mob_clean[2:]
                        _wa_tmpl_mob = ("91"+_wa_mob_clean) if len(_wa_mob_clean)==10 else ""
                        if _wa_tmpl_mob:
                            with st.expander("📨 Send Status Message", expanded=False):
                                _TMPL_OPTIONS = [
                                    "Follow-up — please confirm status",
                                    "Urgent — order is delayed, need ETA",
                                    "Happy — order received on time ✅",
                                    "Custom message",
                                ]
                                _sel_tmpl = st.radio("Template", _TMPL_OPTIONS,
                                                      key=f"tmpl_{_lid}", label_visibility="collapsed")
                                _base = f"*Re: Order {info['order_no']}* — {info['patient_name']}\n"
                                _TMPL_BODY = {
                                    "Follow-up — please confirm status":
                                        _base + "Kindly share current status. Please confirm.",
                                    "Urgent — order is delayed, need ETA":
                                        _base + "⚠️ This order is running late. Please share revised delivery date.",
                                    "Happy — order received on time ✅":
                                        _base + "✅ Order received. Quality is good. Thank you!",
                                }
                                if _sel_tmpl == "Custom message":
                                    _tmpl_body = st.text_area("Message", key=f"tmpl_body_{_lid}",
                                                               height=80, label_visibility="collapsed")
                                else:
                                    _tmpl_body = _TMPL_BODY.get(_sel_tmpl, "")
                                    st.caption(_tmpl_body)
                                if _tmpl_body:
                                    st.link_button("📲 Send via WhatsApp",
                                        f"https://wa.me/{_wa_tmpl_mob}?text={_uparse.quote(_tmpl_body)}",
                                        use_container_width=True)

                    # ── Stage 5: Inspection ──────────────────────────────────────
                    if _stage == "INSPECTION":
                        with st.container(border=True):
                            st.markdown("**🔍 Inspection Result**")
                            _INSP_ISSUES = [
                                "✅ No issues — approve",
                                "Power mismatch — wrong SPH/CYL",
                                "Scratch / surface defect",
                                "Coating failure (peeling/bubbles)",
                                "Wrong tint / colour",
                                "Prism / axis error",
                                "Chipped / cracked blank",
                                "Wrong product supplied",
                                "Other (specify)",
                            ]
                            _insp_result = st.selectbox("Issue", _INSP_ISSUES,
                                                         key=f"insp_{_lid}", label_visibility="collapsed")
                            if _insp_result == "✅ No issues — approve":
                                _needs_hc = _line_requires_internal_hardcoat(line)
                                if _needs_hc:
                                    st.warning(
                                        "HC coating detected. Choose the next route. "
                                        "Billing is blocked until this decision is saved."
                                    )
                                _route_opts = ["Process to Hardcoat", "Ready for Billing"]
                                _route_default = 0 if _needs_hc else 1
                                _route_choice = st.radio(
                                    "After inspection",
                                    _route_opts,
                                    index=_route_default,
                                    key=f"insp_route_{_lid}",
                                    horizontal=True,
                                )
                                if _route_choice == "Process to Hardcoat":
                                    if st.button("✅ Send to Internal Hardcoat",
                                                 key=f"insp_hc_{_lid}", type="primary", use_container_width=True):
                                        try:
                                            _send_supplier_line_to_internal_hardcoat(line, _lp)
                                            st.success("Sent to internal Hardcoat. Next scan: Hardcoat In.")
                                            st.rerun()
                                        except Exception as _hc_e:
                                            st.error(f"Could not route to internal hardcoat: {_hc_e}")
                                else:
                                    _confirm_direct = True
                                    if _needs_hc:
                                        _confirm_direct = st.checkbox(
                                            "Confirm: this HC lens is already fully coated and can go to billing",
                                            key=f"insp_bill_confirm_{_lid}",
                                        )
                                    if st.button("✅ Approve → Ready for Billing",
                                                 key=f"insp_ok_{_lid}", type="primary",
                                                 use_container_width=True, disabled=not _confirm_direct):
                                        _lp["supplier_stage"] = "READY_FOR_BILLING"
                                        _lp["inspection_result"] = "PASS"
                                        _lp["post_supplier_process"] = "BILLING"
                                        _lp.pop("internal_process", None)
                                        _lp.pop("internal_process_stage", None)
                                        try:
                                            from modules.sql_adapter import run_write as _rw4
                                            _rw4("UPDATE order_lines SET ready_qty=%(q)s, allocated_qty=%(q)s "
                                                 "WHERE id=%(lid)s::uuid", {"q": _needed, "lid": _lid})
                                        except Exception: pass
                                        _save_lp(_lid, _lp)
                                        st.rerun()
                            else:
                                _issue_note = st.text_input("Describe issue", key=f"insp_note_{_lid}",
                                                             label_visibility="collapsed") if _insp_result == "Other (specify)" else ""
                                _issue_text = _issue_note or _insp_result
                                _revised_eta = st.date_input("Revised delivery date", value=None,
                                                              key=f"insp_eta_{_lid}",
                                                              label_visibility="collapsed", format="DD/MM/YYYY")
                                # Customer WA
                                _cust_msg = (f"Dear {info['patient_name']},\nYour order "
                                             f"{info['order_no']} has an issue:\n⚠️ {_issue_text}\n")
                                if _revised_eta:
                                    _cust_msg += f"Revised date: {_revised_eta.strftime('%d %b %Y')}\n"
                                _cust_msg += "We apologise for the inconvenience."
                                _ic1, _ic2 = st.columns(2)
                                with _ic1:
                                    # Try to get customer mobile from order
                                    _cust_mob_raw = _q("""
                                                        SELECT COALESCE(o.patient_mobile, pt.mobile, '') AS mob
                                                        FROM orders o
                                                        LEFT JOIN parties pt ON pt.id = o.party_id
                                                        WHERE o.id=%(oid)s::uuid LIMIT 1""",
                                                        {"oid": odata["order_id"]})
                                    _cmob = "".join(x for x in ((_cust_mob_raw[0].get("mob","") if _cust_mob_raw else "")) if x.isdigit())
                                    if _cmob.startswith("91") and len(_cmob)==12: _cmob = _cmob[2:]
                                    _cwa = ("91"+_cmob) if len(_cmob)==10 else ""
                                    if _cwa:
                                        st.link_button("📲 Notify Customer",
                                            f"https://wa.me/{_cwa}?text={_uparse.quote(_cust_msg)}",
                                            use_container_width=True)
                                    else:
                                        st.caption("No customer mobile")
                                with _ic2:
                                    if st.button("🔄 Return / Re-order", key=f"insp_ret_{_lid}",
                                                 use_container_width=True):
                                        _lp["supplier_stage"] = "ORDER_PLACED"
                                        _lp["inspection_result"] = f"FAIL: {_issue_text}"
                                        _lp["reprocess_count"] = int(_lp.get("reprocess_count",0)) + 1
                                        _save_lp(_lid, _lp)
                                        st.rerun()

                    # ── Billing readiness — DB truth, not stage ───────────────────
                    # Show billing CTA whenever purchase is locked + received_qty > 0
                    # Stage is irrelevant — user may be at any stage with a valid ack
                    _rfb_acked = _check_purchase_acked(_lid)
                    _rfb_locked   = bool(_rfb_acked.get("is_price_locked"))
                    _rfb_recv_qty = float(_rfb_acked.get("received_qty") or 0)
                    _rfb_price    = float(_rfb_acked.get("purchase_price") or 0)
                    _rfb_hc_pending = _line_internal_hardcoat_pending(_lp)
                    _rfb_ready    = _rfb_locked and _rfb_recv_qty > 0 and _rfb_price > 0 and not _rfb_hc_pending

                    if _rfb_ready:
                        _rfb1, _rfb2 = st.columns([2, 1])
                        with _rfb1:
                            st.success(
                                f"✅ Purchase locked · Received {int(_rfb_recv_qty)} pc · "
                                f"₹{_rfb_price:,.2f}/pc"
                            )
                        with _rfb2:
                            if st.button("💰 Open Billing", key=f"go_bill_{_lid}",
                                         type="primary", use_container_width=True):
                                _go_to_billing(odata["order_id"], odata["order_no"])
                    elif _rfb_hc_pending:
                        st.warning("Billing blocked: this external supply was routed to internal hardcoat. Complete Hardcoat In → Hardcoat Done → Inspection after Hardcoat first.")
                    elif _stage in ("RECEIVED", "INSPECTION", "READY_FOR_BILLING") and not _rfb_acked:
                        st.info("💡 Record purchase acknowledgement (optional — for procurement records)")

                    if _stage == "READY_FOR_BILLING":
                        # Keep customer notification WA regardless of ack status
                        _cust_ready_rows = _q("""SELECT COALESCE(o.patient_mobile, pt.mobile, '') AS mob,
                                               COALESCE(o.patient_name, o.party_name, '') AS name
                                               FROM orders o
                                               LEFT JOIN parties pt ON pt.id = o.party_id
                                               WHERE o.id=%(oid)s::uuid LIMIT 1""",
                                               {"oid": odata["order_id"]})
                        if _cust_ready_rows:
                            _cr = _cust_ready_rows[0]
                            _cr_mob = "".join(x for x in _cr.get("mob","") if x.isdigit())
                            if _cr_mob.startswith("91") and len(_cr_mob)==12: _cr_mob = _cr_mob[2:]
                            _cr_wa = ("91"+_cr_mob) if len(_cr_mob)==10 else ""
                            _cr_msg = (f"Dear {_cr.get('name', info['patient_name'])},\n"
                                       f"Your order {info['order_no']} is ready for collection! "
                                       f"Please visit us at your convenience.\nThank you.")
                            if _cr_wa:
                                st.link_button("📲 Notify Customer — Order Ready",
                                    f"https://wa.me/{_cr_wa}?text={_uparse.quote(_cr_msg)}",
                                    use_container_width=True)


                # ── Helper: build WA message for a subset of lines ───────
                def _line_detail_grp(wl, show_ref=False, use_supplier_name=False):
                    """Build full parameter block for one eye line.
                    use_supplier_name=True: swap product name to supplier catalogue name
                    (used for External Lab WA messages — order by their name, sell by ours)
                    """
                    _we  = str(wl.get("eye_side","")).upper()
                    _lbl = "👁 R Eye" if _we in ("R","RIGHT") else "👁 L Eye"
                    _pn  = str(wl.get("product_name","")).split(" | ")[0]
                    _supplier_desc = ""
                    _lpp = wl.get("_lp") or wl.get("lens_params") or {}
                    if isinstance(_lpp, str):
                        try: import json as _jj; _lpp = _jj.loads(_lpp)
                        except: _lpp = {}
                    # Supplier/Lab: swap to supplier product name if mapping exists
                    if use_supplier_name:
                        _wl_pid = str(wl.get("product_id") or "")
                        _wl_sid = str(
                            wl.get("supplier_id")
                            or _lpp.get("supplier_id")
                            or _lpp.get("replenishment_supplier_id")
                            or ""
                        )
                        if _wl_pid and _wl_sid:
                            try:
                                from modules.backoffice.supplier_product_map_ui import get_supplier_product_name
                                _spm = get_supplier_product_name(_wl_pid, _wl_sid)
                                if _spm.get("supplier_product_name"):
                                    _pn = _spm["supplier_product_name"]
                                _desc_parts = []
                                if _spm.get("supplier_brand"):
                                    _desc_parts.append(str(_spm.get("supplier_brand")))
                                if _spm.get("supplier_index"):
                                    _desc_parts.append(f"Index {_spm.get('supplier_index')}")
                                if _spm.get("supplier_coating"):
                                    _desc_parts.append(str(_spm.get("supplier_coating")))
                                if _spm.get("supplier_treatment") and str(_spm.get("supplier_treatment")) != "Clear":
                                    _desc_parts.append(str(_spm.get("supplier_treatment")))
                                _supplier_desc = " | ".join(_desc_parts)
                            except Exception as _e:
                                pass  # fall back to our product name
                    parts = [f"*{_lbl}: {_pn}*"]
                    if _supplier_desc:
                        parts.append(f"  Product Specs: {_supplier_desc}")
                    _rx = _power_str(wl)
                    if _rx: parts.append(f"  Rx: {_rx}")
                    _extras = []
                    for _fk, _fl in [
                        ("thickness",     "Thickness"),
                        ("tinted",        "Tinted"),
                        ("corridor",      "Corridor"),
                        ("diameter",      "Diameter"),
                        ("frame_type",    "Frame"),
                        ("fitting_height","Fitting Ht"),
                        ("instructions",  "Note"),
                    ]:
                        _fv = str(_lpp.get(_fk,"")).strip()
                        if _fv and _fv not in ("", "None", "null"):
                            _extras.append(f"{_fl}: {_fv}")
                    if _extras: parts.append("  " + " | ".join(_extras))
                    if show_ref:
                        _sref = str(_lpp.get("supplier_order_no","")).strip()
                        if _sref: parts.append(f"  Supplier Ref: {_sref}")
                    parts.append(f"  Qty: {int(wl.get('quantity') or 1)}")
                    return "\n".join(parts)

                def _build_wa_msg(wa_lines, sup_name, show_ref):
                    _po_refs = []
                    for _pln in wa_lines:
                        _plp = _pln.get("_lp") or _pln.get("lens_params") or {}
                        if isinstance(_plp, str):
                            try: import json as _pj_po; _plp = _pj_po.loads(_plp)
                            except Exception: _plp = {}
                        _pref = str(
                            _plp.get("supplier_order_no")
                            or _plp.get("replenishment_po_no")
                            or _pln.get("live_po_no")
                            or ""
                        ).strip()
                        if _pref and _pref not in _po_refs:
                            _po_refs.append(_pref)
                    _msg_parts = []
                    if _po_refs:
                        _msg_parts.append(f"*📦 PO No: {', '.join(_po_refs)}*")
                        _msg_parts.append(f"Customer Order: {info['order_no']}")
                    else:
                        _msg_parts.append(f"*📋 Order: {info['order_no']}*")
                    _msg_parts.append(f"Patient: {info['patient_name']}")
                    if sup_name and sup_name != "—":
                        _msg_parts.append(f"To: {sup_name}")
                    _msg_parts.append("")
                    for _wl in wa_lines:
                        # Supplier/Lab: use supplier's product name in WA message
                        _msg_parts.append(_line_detail_grp(
                            _wl, show_ref=show_ref,
                            use_supplier_name=True
                        ))
                        _msg_parts.append("")
                    if _is_lab:
                        _msg_parts.append("Please confirm receipt & send your order reference. 🙏")
                    else:
                        _msg_parts.append("Please confirm order & share your reference number. 🙏")
                    return "\n".join(_msg_parts)

                def _mob_for_supplier(sup_id):
                    """Get clean WA-ready mobile number for a supplier."""
                    _mob_raw = _sup_by_id.get(sup_id, {}).get("mobile", "") if sup_id else ""
                    if not _mob_raw:
                        _mob_raw = next((l.get("supplier_mobile","") for l in lines
                                         if l.get("supplier_id") == sup_id and l.get("supplier_mobile")), "")
                    _mc = "".join(x for x in _mob_raw if x.isdigit())
                    if _mc.startswith("91") and len(_mc) == 12: _mc = _mc[2:]
                    return ("91" + _mc) if len(_mc) == 10 else ""

                def _line_supplier_ref(line):
                    """Supplier's own confirmation/order number saved after sending."""
                    _lp_ref = line.get("_lp") or line.get("lens_params") or {}
                    if isinstance(_lp_ref, str):
                        try:
                            import json as _json_ref
                            _lp_ref = _json_ref.loads(_lp_ref)
                        except Exception:
                            _lp_ref = {}
                    if not isinstance(_lp_ref, dict):
                        _lp_ref = {}
                    return str(
                        _lp_ref.get("supplier_order_no")
                        or _lp_ref.get("supplier_confirmation_no")
                        or line.get("sup_order_no")
                        or ""
                    ).strip()

                def _missing_supplier_refs(stage_lines, next_code):
                    """
                    Let staff mark the order as placed/sent first, then require
                    supplier ref before moving into Awaiting Supply or beyond.
                    """
                    if str(next_code or "").upper() in ("", "ORDER_PLACED"):
                        return []
                    missing = []
                    for _ml in stage_lines or []:
                        _cur_stage = str(_ml.get("sup_stage") or "NEEDS_ORDERING").upper()
                        if _cur_stage in ("ORDER_PLACED", "SUPPLIER_CONFIRMED") and not _line_supplier_ref(_ml):
                            _eye = str(_ml.get("eye_side") or "").upper()
                            _name = str(_ml.get("product_name") or "").split(" | ")[0]
                            missing.append(" ".join(x for x in [_eye, _name] if x).strip())
                    return missing

                def _show_missing_ref_stop(missing):
                    st.error(
                        "Save the supplier order/reference number before moving to the next stage. "
                        + ("Missing: " + ", ".join(missing[:4]) if missing else "")
                    )
                    st.stop()

                # ── Separate: unassigned lines vs assigned lines ──────────
                _rl_lines = sorted(
                    [l for l in lines if str(l.get("eye_side","")).upper() in ("R","L","RIGHT","LEFT")],
                    key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1
                )
                _assigned_rl   = [l for l in _rl_lines if l.get("supplier_id")]
                _unassigned_rl = [l for l in _rl_lines if not l.get("supplier_id")]

                # Smart R+L grouping: if same supplier, show as one combined block
                _r_sup = next((l.get("supplier_id","") for l in _rl_lines
                               if str(l.get("eye_side","")).upper() in ("R","RIGHT")), "")
                _l_sup = next((l.get("supplier_id","") for l in _rl_lines
                               if str(l.get("eye_side","")).upper() in ("L","LEFT")), "")
                _same_supplier = bool(_r_sup and _l_sup and _r_sup == _l_sup)
                _split_sup_key = f"sup_split_{_goid[:8]}"
                _force_split   = st.session_state.get(_split_sup_key, False)
                _show_split    = not _same_supplier or _force_split

                # Unique supplier IDs among assigned R/L lines in this card
                _sup_ids_in_grp = list(dict.fromkeys(
                    l["supplier_id"] for l in _assigned_rl if l.get("supplier_id")
                ))
                _is_split = len(_sup_ids_in_grp) > 1  # R → Sup A, L → Sup B

                st.markdown("---")

                # ── Supplier/lab assignment lives here (procurement team owned) ────
                if _unassigned_rl:
                    with st.container(border=True):
                        _assign_title = "External supplier/lab" if _is_lab else "Supplier"
                        st.markdown(f"**Assign {_assign_title}**")
                        st.caption("Backoffice only sets the route. Select the actual supplier here, then WhatsApp/advance controls appear.")
                        _sup_ids = [""] + list(_sup_by_id.keys())
                        _sel_sid = st.selectbox(
                            _assign_title,
                            _sup_ids,
                            key=f"sup_assign_{route_filter}_{_goid[:8]}",
                            format_func=lambda x: "— Select —" if not x else _sup_by_id.get(x, {}).get("name", x),
                        )
                        _as1, _as2 = st.columns([1, 1])
                        with _as1:
                            if st.button(
                                f"✅ Assign to all pending {len(_unassigned_rl)} line(s)",
                                key=f"sup_assign_save_{route_filter}_{_goid[:8]}",
                                type="primary",
                                use_container_width=True,
                                disabled=not bool(_sel_sid),
                            ):
                                _sname = _sup_by_id.get(_sel_sid, {}).get("name", "")
                                try:
                                    for _ul in _unassigned_rl:
                                        _ulp = dict(_ul.get("_lp") or {})
                                        _ulp["supplier_id"] = _sel_sid
                                        _ulp["supplier_name"] = _sname
                                        _ulp.setdefault("supplier_stage", "NEEDS_ORDERING")
                                        _save_lp(str(_ul["line_id"]), _ulp)
                                    # Sidecar key — see comment above on
                                    # _prod_lazy_panel_next.
                                    st.session_state["_prod_lazy_panel_next"] = "🧪 External Supplier" if _is_lab else "🏭 Supplier"
                                    st.success(f"Assigned to {_sname}.")
                                    st.rerun()
                                except Exception as _ase:
                                    st.error(f"Supplier assignment failed: {_ase}")
                        with _as2:
                            st.caption("Need R/L different suppliers? Assign one, then use split/recede controls after reload.")

                # ── Advance + WA buttons — only for assigned lines ────────
                if not _assigned_rl:
                    st.info("⚠️ Supplier/Lab not assigned. Select supplier above to enable WhatsApp and advancement.")
                elif _is_split:
                    # ── SPLIT MODE: one advance + one WA button per supplier ──
                    st.markdown(
                        "<div style='background:#1e293b;border-radius:6px;padding:6px 12px;"
                        "margin-bottom:6px;font-size:0.72rem;color:#94a3b8'>"
                        "🔀 <b>Split routing</b> — R & L going to different suppliers. "
                        "Controls shown per supplier below.</div>",
                        unsafe_allow_html=True
                    )
                    for _split_sid in _sup_ids_in_grp:
                        _split_lines = [l for l in _assigned_rl if l.get("supplier_id") == _split_sid]
                        _split_name  = _sup_by_id.get(_split_sid, {}).get("name","") or \
                                       next((l.get("supplier_name","") for l in _split_lines if l.get("supplier_name")), "—")
                        _split_eyes  = "+".join(str(l.get("eye_side","")).upper() for l in sorted(
                            _split_lines, key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1))
                        _split_stages = [l.get("sup_stage") or "NEEDS_ORDERING" for l in _split_lines]
                        _split_max_idx = max(STAGE_IDX.get(s, 0) for s in _split_stages)
                        _split_next    = STAGES[_split_max_idx + 1] if _split_max_idx < len(STAGES) - 1 else None
                        _split_mob     = _mob_for_supplier(_split_sid)
                        _split_show_ref = (STAGES[_split_max_idx][0] not in ("NEEDS_ORDERING", "ORDER_PLACED"))

                        with st.container(border=True):
                            st.markdown(
                                f"<div style='font-size:0.78rem;font-weight:700;color:#e2e8f0;"
                                f"margin-bottom:4px'>👁 {_split_eyes} → 🏭 {_split_name}</div>",
                                unsafe_allow_html=True
                            )
                            _sp1, _sp2 = st.columns(2)
                            with _sp1:
                                if _split_next:
                                    if st.button(
                                        f"▶ Advance {_split_eyes} → {_split_next[1]}",
                                        key=f"gadv_{route_filter}_{_goid}_{_split_sid[:8]}",
                                        type="primary", use_container_width=True
                                    ):
                                        _miss_ref = _missing_supplier_refs(_split_lines, _split_next[0])
                                        if _miss_ref:
                                            _show_missing_ref_stop(_miss_ref)
                                        if _split_next[0] == "READY_FOR_BILLING":
                                            st.error(
                                                "Inspection route decision is compulsory. Open the Inspection Result "
                                                "section and choose 'Ready for Billing' or 'Process to Hardcoat'."
                                            )
                                            st.stop()
                                        try:
                                            from modules.sql_adapter import run_write as _rw_sp
                                            for _al in _split_lines:
                                                _alp = dict(_al.get("_lp") or {})
                                                _alp["supplier_stage"] = _split_next[0]
                                                if _split_next[0] == "RECEIVED":
                                                    _aq = int(_al.get("quantity") or 1)
                                                    _alp["ready_qty"] = _aq
                                                    _rw_sp("UPDATE order_lines SET ready_qty=%(rq)s "
                                                           "WHERE id=%(lid)s::uuid",
                                                           {"rq": _aq, "lid": str(_al["line_id"])})
                                                _save_lp(str(_al["line_id"]), _alp)
                                            try:
                                                from modules.backoffice.order_status_live import compute_order_status as _cos_sp
                                                _cos_sp({"id": odata["order_id"],
                                                         "order_no": odata["order_no"],
                                                         "status": odata.get("status","")}, write=True)
                                            except Exception: pass
                                            st.rerun()
                                        except Exception as _spe: st.error(str(_spe))
                                else:
                                    st.success(f"✅ {_split_eyes} at final stage")

                            with _sp2:
                                _split_order_sent = (STAGES[_split_max_idx][0] not in ("NEEDS_ORDERING", "ORDER_PLACED"))
                                if _split_order_sent:
                                    st.markdown(
                                        "<div style='background:#1e293b;border:1px solid #334155;"
                                        "border-radius:6px;padding:7px 12px;text-align:center;"
                                        "color:#64748b;font-size:0.78rem'>✅ Order sent</div>",
                                        unsafe_allow_html=True
                                    )
                                else:
                                    # External Lab: check mapping before WA
                                    _sp_all_mapped = True
                                    if _is_lab:
                                        for _sp_ck in _split_lines:
                                            _sp_ck_pid = str(_sp_ck.get("product_id") or "")
                                            _sp_ck_sid = str(_sp_ck.get("supplier_id") or "")
                                            if _sp_ck_pid and _sp_ck_sid:
                                                try:
                                                    from modules.backoffice.supplier_product_map_ui import get_supplier_product_name as _gspn_sp
                                                    _sp_ck_m = _gspn_sp(_sp_ck_pid, _sp_ck_sid)
                                                    if not _sp_ck_m.get("supplier_product_name","").strip():
                                                        _sp_all_mapped = False
                                                except Exception:
                                                    _sp_all_mapped = False

                                    _sp_map_key = f"_spm_sp_{_goid}_{_split_sid[:8]}"
                                    if _is_lab and not _sp_all_mapped:
                                        st.warning("⚠️ Map supplier products first")
                                        if st.button("🔗 Map Products",
                                                     key=f"spm_sp_{_goid}_{_split_sid[:8]}",
                                                     use_container_width=True,
                                                     type="primary"):
                                            st.session_state[_sp_map_key] = True
                                            st.rerun()
                                    elif _split_mob:
                                        _sp_wa_msg = _build_wa_msg(_split_lines, _split_name, _split_show_ref)
                                        _sp_wa_url = f"https://wa.me/{_split_mob}?text={_uparse.quote(_sp_wa_msg, safe='')}"
                                        st.link_button(
                                            f"📲 WhatsApp {_split_eyes} to {_split_name}",
                                            _sp_wa_url, use_container_width=True, type="primary"
                                        )
                                    else:
                                        st.caption(f"⚠️ No mobile for {_split_name}")

                                    if _is_lab and st.session_state.get(_sp_map_key):
                                        st.markdown("---")
                                        try:
                                            from modules.backoffice.supplier_product_map_ui import render_supplier_product_map_admin
                                            _sp_lines_for_map = [dict(l) for l in _split_lines]
                                            for _sml in _sp_lines_for_map:
                                                _sml["supplier_id"]   = _split_sid
                                                _sml["supplier_name"] = _split_name
                                            render_supplier_product_map_admin(
                                                supplier_id=_split_sid,
                                                lines=_sp_lines_for_map,
                                            )
                                        except ImportError:
                                            st.error("supplier_product_map_ui.py not installed")
                                        if st.button("✓ Done", key=f"spm_sp_done_{_goid}_{_split_sid[:8]}",
                                                     use_container_width=True):
                                            st.session_state.pop(_sp_map_key, None)
                                            st.rerun()

                            # Recede for this sub-group
                            _split_min_idx = min(STAGE_IDX.get(s, 0) for s in _split_stages)
                            _sp_recede_opts = STAGES[:_split_min_idx]
                            if _sp_recede_opts:
                                _sp_rec_lbls  = ["◀ Set back to..."] + [s[1] for s in _sp_recede_opts]
                                _sp_rec_codes = [None] + [s[0] for s in _sp_recede_opts]
                                _sp_rec_sel   = st.selectbox(
                                    "Recede", _sp_rec_lbls,
                                    key=f"recede_sel_{route_filter}_{_goid}_{_split_sid[:8]}",
                                    label_visibility="collapsed"
                                )
                                _sp_rec_code = _sp_rec_codes[_sp_rec_lbls.index(_sp_rec_sel)]
                                if _sp_rec_code:
                                    if st.button("◀ Apply",
                                                 key=f"recede_btn_{route_filter}_{_goid}_{_split_sid[:8]}",
                                                 use_container_width=True):
                                        try:
                                            for _al in _split_lines:
                                                _alp = dict(_al.get("_lp") or {})
                                                _alp["supplier_stage"] = _sp_rec_code
                                                _save_lp(str(_al["line_id"]), _alp)
                                            st.rerun()
                                        except Exception as _re2: st.error(str(_re2))

                            # Email/format hooks per supplier
                            _eh1s, _eh2s = st.columns(2)
                            with _eh1s:
                                st.button("📧 Email", key=f"email_hook_{route_filter}_{_goid[:8]}_{_split_sid[:8]}",
                                          use_container_width=True, disabled=True,
                                          help="Configure SMTP in Settings — coming soon")
                            with _eh2s:
                                st.button("📊 Co. Format", key=f"fmt_hook_{route_filter}_{_goid[:8]}_{_split_sid[:8]}",
                                          use_container_width=True, disabled=True,
                                          help="Company-prescribed Rx format — coming soon")
                            with st.expander("📤 Send order options — WhatsApp / Mail / Excel / Phone", expanded=True):
                                try:
                                    from modules.backoffice.replenishment_panel import render_replenishment_panel
                                    _split_sup = dict(_sup_by_id.get(_split_sid, {}) or {})
                                    _split_sup.setdefault("id", _split_sid)
                                    _split_sup.setdefault("supplier_id", _split_sid)
                                    _split_sup.setdefault("name", _split_name)
                                    _split_sup.setdefault("mobile", _split_mob)
                                    _send_lines = []
                                    for _sl in _split_lines:
                                        _tmp = dict(_sl)
                                        _tmp.setdefault("order_no", info.get("order_no", ""))
                                        _send_lines.append(_tmp)
                                    render_replenishment_panel(
                                        _send_lines,
                                        _split_sup,
                                        order_no=info.get("order_no", ""),
                                        route=route_filter,
                                        key_prefix=f"sup_send_{route_filter}_{_goid[:8]}_{_split_sid[:8]}",
                                        allow_save_ref=False,
                                    )
                                except Exception as _send_e:
                                    st.error(f"Send panel error: {_send_e}")
                                # ── Bonzer portal send (display-only) ──
                                try:
                                    from modules.backoffice.bonzer_portal import render_bonzer_send
                                    st.markdown("---")
                                    render_bonzer_send(
                                        _split_lines,
                                        order_no=info.get("order_no", ""),
                                        patient_name=info.get("patient_name", ""),
                                        patient_mobile=info.get("patient_mobile", ""),
                                        supplier_name=_split_name,
                                        key_prefix=f"bonzer_{route_filter}_{_goid[:8]}_{_split_sid[:8]}",
                                    )
                                except Exception as _bz_e:
                                    st.caption(f"Bonzer panel unavailable: {_bz_e}")

                else:
                    # ── SAME SUPPLIER MODE: combined advance + WA for all assigned lines ──
                    _same_sid      = _sup_ids_in_grp[0] if _sup_ids_in_grp else ""
                    _same_name     = _sup_by_id.get(_same_sid, {}).get("name","") or \
                                     next((l.get("supplier_name","") for l in _assigned_rl if l.get("supplier_name")), "—")
                    _same_mob      = _mob_for_supplier(_same_sid)
                    _same_stages   = [l.get("sup_stage") or "NEEDS_ORDERING" for l in _assigned_rl]
                    _same_min_idx  = min(STAGE_IDX.get(s, 0) for s in _same_stages)
                    _same_max_idx  = max(STAGE_IDX.get(s, 0) for s in _same_stages)
                    _same_next     = STAGES[_same_max_idx + 1] if _same_max_idx < len(STAGES) - 1 else None
                    _same_cur_lbl  = STAGE_LABEL.get(STAGES[_same_min_idx][0], STAGES[_same_min_idx][0])
                    _same_show_ref = (STAGES[_same_max_idx][0] not in ("NEEDS_ORDERING", "ORDER_PLACED"))
                    _same_eyes     = "+".join(str(l.get("eye_side","")).upper() for l in sorted(
                        _assigned_rl, key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1))

                    st.markdown(
                        f"<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
                        f"padding:8px 14px;margin:4px 0;display:flex;align-items:center;gap:8px'>"
                        f"<span style='font-size:0.68rem;color:#64748b;text-transform:uppercase;"
                        f"letter-spacing:.06em'>{_same_eyes} stage</span>"
                        f"<span style='background:{_stage_color(STAGES[_same_min_idx][0])}22;"
                        f"color:{_stage_color(STAGES[_same_min_idx][0])};"
                        f"font-size:0.72rem;font-weight:700;padding:2px 10px;border-radius:10px'>"
                        f"{_same_cur_lbl}</span></div>",
                        unsafe_allow_html=True
                    )

                    _gsc1, _gsc2 = st.columns([3, 3])

                    with _gsc1:
                        if _same_next:
                            _adv_lbl = (f"▶ Advance Both → {_same_next[1]}"
                                        if len(_assigned_rl) > 1 else f"▶ Advance → {_same_next[1]}")
                            if st.button(_adv_lbl,
                                         key=f"gadv_{route_filter}_{_goid}_{_gsid[:8]}", type="primary",
                                         use_container_width=True):
                                _miss_ref = _missing_supplier_refs(_assigned_rl, _same_next[0])
                                if _miss_ref:
                                    _show_missing_ref_stop(_miss_ref)
                                if _same_next[0] == "READY_FOR_BILLING":
                                    st.error(
                                        "Inspection route decision is compulsory. Open the Inspection Result "
                                        "section and choose 'Ready for Billing' or 'Process to Hardcoat'."
                                    )
                                    st.stop()
                                try:
                                    from modules.sql_adapter import run_write as _rw_g
                                    for _al in _assigned_rl:
                                        _alp = dict(_al.get("_lp") or {})
                                        _alp["supplier_stage"] = _same_next[0]
                                        if _same_next[0] == "RECEIVED":
                                            _aq = int(_al.get("quantity") or 1)
                                            _alp["ready_qty"] = _aq
                                            _rw_g("UPDATE order_lines SET ready_qty=%(rq)s "
                                                  "WHERE id=%(lid)s::uuid",
                                                  {"rq": _aq, "lid": str(_al["line_id"])})
                                        _save_lp(str(_al["line_id"]), _alp)
                                    try:
                                        from modules.backoffice.order_status_live import compute_order_status as _cos_g
                                        _cos_g({"id": odata["order_id"],
                                                "order_no": odata["order_no"],
                                                "status": odata.get("status","")}, write=True)
                                    except Exception: pass
                                    st.rerun()
                                except Exception as _ge: st.error(str(_ge))
                        else:
                            st.success("✅ All assigned lines at final stage")

                    with _gsc2:
                        _same_order_sent = (STAGES[_same_max_idx][0] not in ("NEEDS_ORDERING", "ORDER_PLACED"))
                        if _same_order_sent:
                            st.markdown(
                                "<div style='background:#1e293b;border:1px solid #334155;"
                                "border-radius:6px;padding:7px 12px;text-align:center;"
                                "color:#64748b;font-size:0.78rem'>✅ Order sent</div>",
                                unsafe_allow_html=True
                            )
                        else:
                            # ── External Lab: check product mapping before WA ──
                            _all_mapped = True
                            _unmapped_lines = []
                            if _is_lab:
                                for _ck_l in _assigned_rl:
                                    _ck_pid = str(_ck_l.get("product_id") or "")
                                    _ck_sid = str(_ck_l.get("supplier_id") or "")
                                    if _ck_pid and _ck_sid:
                                        try:
                                            from modules.backoffice.supplier_product_map_ui import get_supplier_product_name as _gspn_ck
                                            _ck_map = _gspn_ck(_ck_pid, _ck_sid)
                                            if not _ck_map.get("supplier_product_name","").strip():
                                                _all_mapped = False
                                                _unmapped_lines.append(_ck_l)
                                        except Exception:
                                            _all_mapped = False
                                            _unmapped_lines.append(_ck_l)

                            # Show mapping UI inline if not mapped
                            _map_key = f"_spm_open_{_goid}_{_gsid[:8]}"
                            if _is_lab and not _all_mapped:
                                st.warning("⚠️ Map supplier products first")
                                for _ul in _unmapped_lines:
                                    st.caption(f"Not mapped: {_ul.get('product_name','?')} → {_same_name}")
                                if st.button(
                                    "🔗 Map Products (required)",
                                    key=f"spm_open_{_goid}_{_gsid[:8]}",
                                    use_container_width=True,
                                    type="primary",
                                    help="Map your product to the supplier's catalogue name before sending order",
                                ):
                                    st.session_state[_map_key] = True
                                    st.rerun()
                            else:
                                if _same_mob:
                                    _wa_msg_same = _build_wa_msg(_assigned_rl, _same_name, _same_show_ref)
                                    _wa_url_same = f"https://wa.me/{_same_mob}?text={_uparse.quote(_wa_msg_same, safe='')}"
                                    _wa_lbl = (f"📲 WhatsApp {_same_eyes} to {_same_name}"
                                               if _same_name and _same_name != "—" else "📲 Send via WhatsApp")
                                    st.link_button(_wa_lbl, _wa_url_same, use_container_width=True, type="primary")
                                else:
                                    _wa_msg_same = _build_wa_msg(_assigned_rl, _same_name, _same_show_ref)
                                    _wa_url_same = f"https://wa.me/?text={_uparse.quote(_wa_msg_same, safe='')}"
                                    st.link_button(
                                        "📲 WhatsApp (enter/pick number)",
                                        _wa_url_same,
                                        use_container_width=True,
                                        type="primary",
                                        help="No mobile saved for this supplier.",
                                    )

                            # Inline mapping panel (opens when button clicked)
                            if _is_lab and st.session_state.get(_map_key):
                                st.markdown("---")
                                try:
                                    from modules.backoffice.supplier_product_map_ui import render_supplier_product_map_admin
                                    # Pass all assigned lines so R and L are mapped separately
                                    _lines_for_map = [dict(l) for l in _assigned_rl]
                                    for _ml in _lines_for_map:
                                        _ml["supplier_id"]   = _same_sid
                                        _ml["supplier_name"] = _same_name
                                    render_supplier_product_map_admin(
                                        supplier_id=_same_sid,
                                        lines=_lines_for_map,
                                    )
                                except ImportError:
                                    st.error("supplier_product_map_ui.py not installed")
                                if st.button("✓ Done mapping", key=f"spm_done_{_goid}_{_gsid[:8]}",
                                             use_container_width=True):
                                    st.session_state.pop(_map_key, None)
                                    st.rerun()

                    # Recede
                    _rec_opts = STAGES[:_same_min_idx]
                    if _rec_opts:
                        _rec_lbls  = ["◀ Set back to..."] + [s[1] for s in _rec_opts]
                        _rec_codes = [None] + [s[0] for s in _rec_opts]
                        _sel_rec   = st.selectbox(
                            "Recede", _rec_lbls,
                            key=f"recede_sel_{route_filter}_{_goid}_{_gsid[:8]}",
                            label_visibility="collapsed"
                        )
                        _rec_code = _rec_codes[_rec_lbls.index(_sel_rec)]
                        if _rec_code:
                            if st.button("◀ Apply to All",
                                         key=f"recede_btn_{route_filter}_{_goid}_{_gsid[:8]}",
                                         use_container_width=True):
                                try:
                                    for _al in _assigned_rl:
                                        _alp = dict(_al.get("_lp") or {})
                                        _alp["supplier_stage"] = _rec_code
                                        _save_lp(str(_al["line_id"]), _alp)
                                    st.rerun()
                                except Exception as _re2: st.error(str(_re2))

                    # Email/format hooks
                    _eh1, _eh2 = st.columns(2)
                    with _eh1:
                        st.button("📧 Send via Email", key=f"email_hook_{route_filter}_{_goid[:8]}_{_gsid[:8]}",
                                  use_container_width=True, disabled=True,
                                  help="Configure SMTP in Settings — coming soon")
                    with _eh2:
                        st.button("📊 Company Format", key=f"fmt_hook_{route_filter}_{_goid[:8]}_{_gsid[:8]}",
                                  use_container_width=True, disabled=True,
                                  help="Company-prescribed Rx format — coming soon")
                    with st.expander("📤 Send order options — WhatsApp / Mail / Excel / Phone", expanded=True):
                        try:
                            from modules.backoffice.replenishment_panel import render_replenishment_panel
                            _same_sup = dict(_sup_by_id.get(_same_sid, {}) or {})
                            _same_sup.setdefault("id", _same_sid)
                            _same_sup.setdefault("supplier_id", _same_sid)
                            _same_sup.setdefault("name", _same_name)
                            _same_sup.setdefault("mobile", _same_mob)
                            _send_lines = []
                            for _sl in _assigned_rl:
                                _tmp = dict(_sl)
                                _tmp.setdefault("order_no", info.get("order_no", ""))
                                _send_lines.append(_tmp)
                            render_replenishment_panel(
                                _send_lines,
                                _same_sup,
                                order_no=info.get("order_no", ""),
                                route=route_filter,
                                key_prefix=f"sup_send_{route_filter}_{_goid[:8]}_{_gsid[:8]}",
                                allow_save_ref=False,
                            )
                        except Exception as _send_e:
                            st.error(f"Send panel error: {_send_e}")
                        # ── Bonzer portal send (display-only) ──
                        try:
                            from modules.backoffice.bonzer_portal import render_bonzer_send
                            st.markdown("---")
                            render_bonzer_send(
                                _assigned_rl,
                                order_no=info.get("order_no", ""),
                                patient_name=info.get("patient_name", ""),
                                patient_mobile=info.get("patient_mobile", ""),
                                supplier_name=_same_name,
                                key_prefix=f"bonzer_{route_filter}_{_goid[:8]}_{_gsid[:8]}",
                            )
                        except Exception as _bz_e:
                            st.caption(f"Bonzer panel unavailable: {_bz_e}")

            # end container

def _render_sales_orders_to_po_tab():
    """Orders → Purchase: browse billed orders, cart, then PO / Invoice / Blank."""
    import datetime as _dt_po
    import urllib.parse as _uparse_po

    def _qpo(sql, params=None):
        try:
            from modules.sql_adapter import run_query
            return run_query(sql, params or {}) or []
        except Exception as e:
            st.error(f"DB: {e}"); return []

    def _rwpo(sql, params=None):
        try:
            from modules.sql_adapter import run_write
            run_write(sql, params or {}); return True
        except Exception as e:
            st.error(f"Write: {e}"); return False

    # ── session state ──────────────────────────────────────────────────────
    if "po_accumulated_items" in st.session_state:
        del st.session_state["po_accumulated_items"]

    if "po_cart"           not in st.session_state: st.session_state.po_cart           = []
    if "po_action"         not in st.session_state: st.session_state.po_action         = None
    if "po_selected_lines" not in st.session_state: st.session_state.po_selected_lines = set()
    if "po_last_clicked"   not in st.session_state: st.session_state.po_last_clicked   = None

    _cart   = st.session_state.po_cart
    _action = st.session_state.po_action

    # ── STEP 2/3: action screen ────────────────────────────────────────────
    if _action and _cart:
        if st.button("← Back to Order Selection", key="po_back_btn"):
            st.session_state.po_action = None
            st.rerun()
        st.markdown("---")
        try:
            if _action == "PO":
                _render_po_creation(_cart, _qpo, _rwpo, _uparse_po)
            elif _action == "INVOICE":
                _render_purchase_invoice(_cart, _qpo, _rwpo)
            elif _action == "BLANK":
                _render_blank_purchase(_cart, _qpo, _rwpo)
        except Exception as _e:
            st.error(f"Error loading action screen: {_e}")
            import traceback
            st.code(traceback.format_exc())
        return

    # ── STEP 1: order browsing ─────────────────────────────────────────────

    # Filters
    with st.container(border=True):
        _pf1, _pf2, _pf3, _pf5 = st.columns([3, 3, 2, 1])
        _po_ord_flt  = _pf1.text_input("Order/Patient", placeholder="🔍 Order no / patient",
                                        key="po_ord_flt", label_visibility="collapsed")
        _po_stk_flt  = _pf2.selectbox("Route", ["All","Stock","Rx (Lab)","In-house"],
                                        key="po_stk_flt", label_visibility="collapsed")
        _po_date_flt = _pf3.date_input(
            "From", value=_dt_po.date.today() - _dt_po.timedelta(days=60),
            key="po_date_flt", label_visibility="collapsed", format="DD/MM/YYYY"
        )
        _po_show_all = False  # always show all — purchased = green, pending = red
        _po_refresh  = _pf5.button("🔄", key="po_refresh_btn",
                                    help="Refresh", use_container_width=True)

    # ── Cache query — only re-runs when filters change or refresh ──────────
    _filter_key = f"{_po_ord_flt}|{_po_stk_flt}|{_po_date_flt}|{_po_show_all}"
    if (_po_refresh
            or "po_rows_cache" not in st.session_state
            or st.session_state.get("po_filter_key") != _filter_key):

        _po_where = [
            "COALESCE(ol.is_deleted,FALSE)=FALSE",
            "COALESCE(ol.is_service_line,FALSE)=FALSE",
            "UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')",
            "DATE(o.created_at) >= %(df)s",
            "EXISTS (SELECT 1 FROM challan_lines cl JOIN challans c ON c.id=cl.challan_id "
            "WHERE cl.order_line_id=ol.id AND c.status NOT IN ('CANCELLED','VOID'))",
        ]
        _po_params = {"df": str(_po_date_flt)}
        if _po_ord_flt.strip():
            _po_where.append(
                "(LOWER(o.order_no) LIKE %(ord)s "
                "OR regexp_replace(LOWER(COALESCE(o.order_no,'')), '[^a-z0-9]', '', 'g') LIKE %(ord_norm)s "
                "OR LOWER(COALESCE(o.patient_name,o.party_name,'')) LIKE %(ord)s)"
            )
            _po_params["ord"] = f"%{_po_ord_flt.strip().lower()}%"
            _po_params["ord_norm"] = f"%{_scan_norm(_po_ord_flt)}%"
        if _po_stk_flt == "Stock":
            _po_where.append(
                "(ol.lens_params->>'manufacturing_route'='STOCK' "
                "OR ol.lens_params->>'batch_status'='ALLOCATED' "
                "OR (ol.lens_params->>'batch_no' IS NOT NULL "
                "AND COALESCE(ol.lens_params->>'manufacturing_route','') "
                "NOT IN ('INHOUSE','VENDOR','EXTERNAL_LAB')))"
            )
        elif _po_stk_flt == "Rx (Lab)":
            _po_where.append(
                "ol.lens_params->>'manufacturing_route' IN ('VENDOR','EXTERNAL_LAB')"
            )
        elif _po_stk_flt == "In-house":
            _po_where.append("ol.lens_params->>'manufacturing_route'='INHOUSE'")
        
        # Only show lines that have NO active PO and NO purchase recorded
        # Lines with PO are managed in Procurement → PO Management
        _po_where.append(
            "NOT EXISTS (SELECT 1 FROM supplier_order_items soi "
            "JOIN supplier_orders so ON so.id = soi.supplier_order_id "
            "WHERE soi.customer_line_id::text = ol.id::text AND so.status NOT IN ('CANCELLED','VOID'))"
        )
        _po_where.append(
            "NOT EXISTS (SELECT 1 FROM purchase_acknowledgements pa "
            "WHERE pa.order_line_id = ol.id AND COALESCE(pa.purchase_price, 0) > 0)"
        )

        # Show all lines — purchased shown green, pending shown red
        # Lines with an active PO shown with PO badge (not hidden)

        # Always fetch ALL billed lines — purchased shown in green, pending in red
        # No hide toggle — game is to clear ALL to green
        _fetched = _qpo("""
            SELECT o.id::text AS order_id, o.order_no,
                   COALESCE(o.patient_name,o.party_name,'—') AS patient_name,
                   o.status AS order_status, o.created_at,
                   ol.id::text AS line_id, ol.eye_side, ol.quantity,
                   COALESCE(ol.unit_price,0) AS unit_price,
                    ol.sph, ol.cyl, ol.axis, ol.add_power,
                    ol.product_id::text AS product_id,
                    p.product_name,
                    COALESCE(p.category,'') AS category,
                    COALESCE(p.unit,'PCS') AS unit,
                    COALESCE(p.box_size,1) AS box_size,
                    COALESCE(ol.lens_params->>'manufacturing_route','STOCK') AS route,
                    pa.challan_no        AS pa_challan,
                    pa.invoice_no        AS pa_invoice,
                    pa.purchase_price    AS pa_price,
                    pa.is_price_locked   AS pa_locked,
                    COALESCE(pt.party_name, ol.lens_params->>'supplier_name','') AS pa_supplier,
                    -- PO info: if an active (non-cancelled) PO exists for this line
                    so.id                AS po_id,
                    so.supplier_order_id AS po_no,
                    so.status            AS po_status,
                    so.created_at       AS po_date,
                    sp.party_name        AS po_supplier
             FROM order_lines ol
             JOIN orders o ON o.id=ol.order_id
             JOIN products p ON p.id=ol.product_id
             LEFT JOIN purchase_acknowledgements pa ON pa.order_line_id=ol.id
             LEFT JOIN parties pt ON pt.id=pa.supplier_id
             -- Join active PO if one exists for this line
             LEFT JOIN supplier_order_items soi
                    ON ol.id::text = NULLIF(soi.customer_line_id::text, '')
             LEFT JOIN supplier_orders so ON so.id=soi.supplier_order_id
                 AND so.status NOT IN ('CANCELLED','VOID')
             LEFT JOIN parties sp ON sp.id::text = so.supplier_id
             WHERE """ + " AND ".join(_po_where) + """
             ORDER BY
                 CASE WHEN pa.purchase_price IS NULL OR pa.purchase_price = 0 THEN 0 ELSE 1 END,
                 CASE WHEN so.id IS NOT NULL THEN 1 ELSE 0 END,
                 o.created_at DESC, o.order_no,
                CASE WHEN ol.eye_side='R' THEN 0 WHEN ol.eye_side='L' THEN 1 ELSE 2 END
        """, _po_params)

        st.session_state.po_rows_cache    = _fetched
        st.session_state.po_filter_key    = _filter_key
        st.session_state.po_selected_lines = set()
        # Clear stale checkbox state
        for _k in [k for k in st.session_state if k.startswith("po_chk_")]:
            del st.session_state[_k]

    _po_rows = st.session_state.po_rows_cache
    if not _po_rows:
        st.info("No billed orders found. Adjust filters or enable 'All' to show purchased lines too.")
        return

    # Group by order
    from collections import OrderedDict as _od_po
    _po_groups = _od_po()
    for _r in _po_rows:
        _ono = _r["order_no"]
        if _ono not in _po_groups:
            _po_groups[_ono] = {
                "order_no":   _ono,
                "patient":    _r["patient_name"],
                "status":     _r["order_status"],
                "created_at": str(_r.get("created_at",""))[:10],
                "lines":      [],
            }
        _po_groups[_ono]["lines"].append(_r)

    _cart_ids    = {c["line_id"] for c in st.session_state.po_cart}
    # Three states: purchased (invoice/challan recorded), po_raised (PO exists, no invoice yet), pending (nothing)
    _purchased_ids = {r["line_id"] for r in _po_rows
                      if r.get("pa_price") and float(r.get("pa_price") or 0) > 0}
    _po_raised_ids = {r["line_id"] for r in _po_rows
                      if r.get("po_no") and r["line_id"] not in _purchased_ids}
    _pending_ids   = {r["line_id"] for r in _po_rows
                      if r["line_id"] not in _purchased_ids and r["line_id"] not in _po_raised_ids}
    # Only truly pending lines (no purchase, no PO) can be checked and added to cart
    _all_free    = [lid for lid in _pending_ids if lid not in _cart_ids]

    # Stable checkbox key
    def _chk_key(lid):
        return f"po_chk_{str(lid).replace('-','')}"

    def _ordsel_key(ono):
        return f"po_ordsel_{abs(hash(ono)) % 99999999}"

    # Single source of truth
    _sel = set()
    for lid in _all_free:
        if st.session_state.get(_chk_key(lid), False):
            _sel.add(lid)
    st.session_state.po_selected_lines = _sel
    _n_sel = len(_sel)

    _n_pending   = len(_pending_ids)
    _n_po        = len(_po_raised_ids)
    _n_purchased = len(_purchased_ids)

    # Sticky top bar — shows progress toward clearing all
    _cart_val = sum(float(i.get("unit_price",0))*int(i.get("quantity",1)) for i in _cart)
    _pct_done = int(_n_purchased / len(_po_rows) * 100) if _po_rows else 0
    st.markdown(
        f"<div style='position:sticky;top:0;z-index:100;background:#0f172a;"
        f"border-bottom:1px solid #1e293b;padding:8px 12px;margin-bottom:6px;"
        f"display:flex;gap:16px;align-items:center;flex-wrap:wrap'>"
        f"<span style='color:#ef4444;font-weight:700'>&#9888; {_n_pending} pending</span>"
        + (f"<span style='color:#8b5cf6;font-weight:700'>&#128228; {_n_po} PO raised</span>"
           if _n_po else "")
        + f"<span style='color:#22c55e;font-weight:700'>&#10003; {_n_purchased} purchased</span>"
        f"<span style='color:#475569;font-size:0.72rem'>{_pct_done}% cleared</span>"
        + (f"<span style='color:#475569'>|</span>"
           f"<span style='color:#60a5fa;font-weight:700'>&#9989; {_n_sel} selected</span>"
           if _n_sel else "")
        + (f"<span style='color:#475569'>|</span>"
           f"<span style='color:#10b981;font-weight:700'>&#128722; {len(_cart)} in cart"
           f" &middot; &#8377;{_cart_val:,.0f}</span>" if _cart else "")
        + "</div>",
        unsafe_allow_html=True
    )

    # ── Action bar ─────────────────────────────────────────────────────────
    _aa3, _aa4 = st.columns([2, 2])
    with _aa3:
        if st.button(
            f"✅ Add {_n_sel} to Cart" if _n_sel else "✅ Add to Cart",
            key="po_add_sel", use_container_width=True,
            type="primary" if _n_sel else "secondary",
            disabled=_n_sel == 0
        ):
            _already = {c["line_id"] for c in st.session_state.po_cart}
            for _r in _po_rows:
                if _r["line_id"] in _sel and _r["line_id"] not in _already:
                    st.session_state.po_cart.append(_r)
            st.session_state.po_selected_lines = set()
            st.rerun()

    if _cart:
        with _aa4:
            _act_sel = st.selectbox(
                "", ["— Action —", "🧾 Invoice", "📦 Blank Purchase"],
                key="po_act_sel", label_visibility="collapsed"
            )
            if _act_sel != "— Action —":
                st.session_state.po_action = {
                    "🧾 Invoice":   "INVOICE",
                    "📦 Blank Purchase": "BLANK"
                }.get(_act_sel)
                st.rerun()
    
    if st.button("🗑 Clear Cart", key="po_clear_cart"):
        st.session_state.po_cart = []
        st.session_state.pop("po_rows_cache", None)
        st.rerun()

    # Build index map for shift-select (order matters — same as render order)
    _idx_to_lid = [r["line_id"] for r in _po_rows if r["line_id"] not in _cart_ids]
    _lid_to_idx = {lid: i for i, lid in enumerate(_idx_to_lid)}

    # Cart expander
    if _cart:
        with st.expander(f"📦 Cart: {len(_cart)} line(s) · ₹{_cart_val:,.0f}", expanded=False):
            for _ci, _cl in enumerate(_cart):
                _ce   = str(_cl.get("eye_side","")).upper()
                _cpwr = _fmt_power_po(_cl)
                _cc1, _cc2 = st.columns([5, 1])
                _cc1.markdown(
                    f"<span style='font-size:0.8rem;color:#e2e8f0'>"
                    f"**{_cl.get('order_no','')}** &middot; {_ce} &middot; "
                    f"{_cl.get('product_name','')} {_cpwr} &middot; "
                    f"Qty {_cl.get('quantity',1)} &middot; "
                    f"&#8377;{float(_cl.get('unit_price',0)):,.0f}</span>",
                    unsafe_allow_html=True
                )
                _cart_rm_key = f"po_rem_{str(_cl.get('line_id',str(_ci))).replace('-','')}"
                if _cc2.button("✕", key=_cart_rm_key, use_container_width=True):
                    st.session_state.po_cart = [
                        x for x in _cart if x.get("line_id") != _cl.get("line_id")
                    ]
                    st.rerun()
        st.markdown("---")

    # ── Order cards ────────────────────────────────────────────────────────
    for _ono, _og in _po_groups.items():
        _olines      = _og["lines"]
        _all_in_cart = all(l["line_id"] in _cart_ids for l in _olines)
        _ord_free    = [l["line_id"] for l in _olines if l["line_id"] not in _cart_ids]

        # Order purchase status
        _ord_line_ids  = [l["line_id"] for l in _olines]
        _ord_purchased = sum(1 for lid in _ord_line_ids if lid in _purchased_ids)
        _ord_po        = sum(1 for lid in _ord_line_ids if lid in _po_raised_ids)
        _ord_pending   = sum(1 for lid in _ord_line_ids if lid in _pending_ids)
        _ord_total     = len(_ord_line_ids)

        _ord_all_done  = _ord_pending == 0 and _ord_po == 0
        _ord_all_pend  = _ord_purchased == 0 and _ord_po == 0
        _ord_border    = "#22c55e" if _ord_all_done else "#ef4444" if _ord_all_pend else "#f59e0b"
        _ord_badge     = ("✅ All purchased" if _ord_all_done
                          else f"⚠️ {_ord_pending} pending" if _ord_all_pend
                          else (f"🔶 {_ord_purchased}✓ {_ord_po}📤 {_ord_pending}⚠️"))
        _ord_badge_col = "#22c55e" if _ord_all_done else "#ef4444" if _ord_all_pend else "#f59e0b"

        # Order header row
        _oh1, _oh2 = st.columns([7, 3])
        with _oh1:
            _ri = {"VENDOR":"🏭","EXTERNAL_LAB":"🧪","INHOUSE":"🔬"}.get(
                _olines[0].get("route","STOCK"), "📦")
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e293b;"
                f"border-left:4px solid {_ord_border};border-radius:6px;"
                f"padding:6px 12px;margin:2px 0'>"
                f"<span style='color:#f1f5f9;font-weight:800'>{_ri} {_ono}</span>"
                f"<span style='color:#64748b;font-size:0.8rem'>"
                f" — {_og['patient']} &middot; {_og['created_at']}</span>"
                f"<span style='color:{_ord_badge_col};font-size:0.72rem;margin-left:10px'>"
                f"{_ord_badge}</span>"
                + (f"<span style='color:#60a5fa;font-size:0.72rem;margin-left:8px'>"
                   f"&#128722; In cart</span>" if _all_in_cart else "")
                + "</div>", unsafe_allow_html=True
            )

        # Line rows
        with st.container():
            for _ln in _olines:
                _lid     = _ln["line_id"]
                _eye     = str(_ln.get("eye_side","")).upper()
                _pwr     = _fmt_power_po(_ln)
                _pn      = (_ln.get("product_name") or "")[:30]
                _qty     = int(_ln.get("quantity") or 1)
                _price   = float(_ln.get("unit_price") or 0)
                _unit    = str(_ln.get("unit","PCS")).upper()
                _bsize   = int(_ln.get("box_size") or 1)
                _in_cart = _lid in _cart_ids

                # Status — purchase recorded, PO raised, or pending
                _pa_price = float(_ln.get("pa_price") or 0)
                _po_no    = _ln.get("po_no","")
                _po_stat  = str(_ln.get("po_status","")).upper()
                _po_sup   = _ln.get("po_supplier","") or ""

                if _ln.get("pa_invoice"):
                    _ps_txt, _ps_color = f"🧾 {_ln['pa_invoice']}", "#22c55e"
                elif _ln.get("pa_challan"):
                    _ps_txt, _ps_color = f"📋 {_ln['pa_challan']}", "#3b82f6"
                elif _po_no:
                    # PO raised but no invoice yet — show PO status
                    _po_badge = {"DRAFT":"📝","SENT":"📤","CONFIRMED":"✅","RECEIVED":"📦"}.get(_po_stat,"📤")
                    _ps_txt   = f"{_po_badge} {_po_no} ({_po_stat})"
                    _ps_color = "#8b5cf6"
                else:
                    _ps_txt, _ps_color = "⚠️ No purchase", "#ef4444"

                # Qty string — use governor for correct box label
                if _unit == "BOX" and _bsize > 1:
                    _nb = _qty // (_bsize or 1)
                    _np = _qty % (_bsize or 1)
                    try:
                        from modules.core.price_qty_governor import box_qty_label as _bql2
                        _qty_str = _bql2(_nb, _np, _bsize)
                    except ImportError:
                        _qty_str = f"{_nb} Box ({_qty} pcs)"
                        if _np: _qty_str = f"{_nb} Box + {_np} pcs ({_qty} pcs)"
                else:
                    _qty_str = f"{_qty} pcs"

                _is_purchased = _lid in _purchased_ids
                _is_po_raised = _lid in _po_raised_ids
                _is_sel       = _lid in _sel
                _row_bg = "#0d2110" if _is_sel else (
                    "#050e07" if _is_purchased else (
                    "#0d0a1a" if _is_po_raised else "transparent"))

                _lc1, _lc2 = st.columns([8, 1])
                with _lc1:
                    if _in_cart:
                        st.markdown(
                            f"<div style='padding:2px 4px;font-size:0.75rem'>"
                            f"&#128722; <b style='color:#60a5fa'>{_eye}</b> · {_pn}"
                            f"<br><span style='color:#475569;font-size:0.68rem'>"
                            f"{_pwr} · {_qty_str} · &#8377;{_price:,.0f} · "
                            f"<span style='color:{_ps_color}'>{_ps_txt}</span>"
                            f"</span></div>",
                            unsafe_allow_html=True
                        )
                    elif _is_purchased:
                        # ✅ Purchase recorded — green, muted, no checkbox
                        st.markdown(
                            f"<div style='background:{_row_bg};border-radius:4px;"
                            f"padding:2px 4px;opacity:0.75'>"
                            f"<span style='font-size:0.75rem;color:#4ade80'>"
                            f"&#10003; <b>{_eye}</b> · {_pn}"
                            f"</span>"
                            f"<br><span style='color:#166534;font-size:0.68rem'>"
                            f"{_pwr} · {_qty_str} · &#8377;{_price:,.0f} · "
                            f"<span style='color:#22c55e'>{_ps_txt}</span>"
                            + (f" · {_ln.get('pa_supplier','')}" if _ln.get("pa_supplier") else "")
                            + f"</span></div>",
                            unsafe_allow_html=True
                        )
                    elif _is_po_raised:
                        # 📤 PO raised — purple, no checkbox (waiting for invoice)
                        _po_sup_txt = _ln.get("po_supplier","") or _ln.get("pa_supplier","")
                        st.markdown(
                            f"<div style='background:{_row_bg};border-radius:4px;"
                            f"padding:2px 4px;opacity:0.85'>"
                            f"<span style='font-size:0.75rem;color:#a78bfa'>"
                            f"&#128228; <b>{_eye}</b> · {_pn}"
                            f"</span>"
                            f"<br><span style='color:#4c1d95;font-size:0.68rem'>"
                            f"{_pwr} · {_qty_str} · &#8377;{_price:,.0f} · "
                            f"<span style='color:#8b5cf6'>{_ps_txt}</span>"
                            + (f" · {_po_sup_txt}" if _po_sup_txt else "")
                            + f"</span></div>",
                            unsafe_allow_html=True
                        )
                    else:
                        # ⚠️ Pending — red, checkbox enabled
                        st.markdown(
                            f"<div style='background:{_row_bg};border-radius:4px;padding:1px 4px'>",
                            unsafe_allow_html=True
                        )
                        st.checkbox(
                            f"{_eye}  {_pn}  · ₹{_price:,.0f}",
                            key=_chk_key(_lid)
                        )
                        st.caption(
                            f"{_pwr}  ·  {_qty_str}"
                            + (f"  ·  {_ln.get('pa_supplier','')}" if _ln.get("pa_supplier") else "")
                            + f"  ·  {_ps_txt}"
                        )
                        st.markdown("</div>", unsafe_allow_html=True)

                with _lc2:
                    if _in_cart:
                        if st.button("✕",
                                     key=f"po_rm_{str(_lid).replace('-','')}",
                                     use_container_width=True):
                            st.session_state.po_cart = [
                                x for x in st.session_state.po_cart
                                if x.get("line_id") != _lid
                            ]
                            st.rerun()

        st.markdown(
            "<div style='height:1px;background:#1e293b;margin:3px 0'></div>",
            unsafe_allow_html=True
        )

    # ── Floating "Add to Cart" button — visible without scrolling ──────────
    if _n_sel > 0:
        st.markdown(
            f"<div style='position:fixed;bottom:24px;right:24px;z-index:999'>"
            f"<div style='background:#22c55e;color:#fff;font-weight:700;"
            f"padding:12px 20px;border-radius:10px;"
            f"box-shadow:0 4px 16px rgba(0,0,0,0.4);font-size:0.9rem'>"
            f"&#128722; {_n_sel} selected — use Add to Cart ↑"
            f"</div></div>",
            unsafe_allow_html=True
        )


def _fmt_power_po(line: dict) -> str:
    """Format power string for order line."""
    parts = []
    try:
        if line.get("sph") is not None:
            n = float(line["sph"])
            parts.append(f"SPH {n:+.2f}")
        if line.get("cyl") and abs(float(line["cyl"])) > 0.01:
            parts.append(f"CYL {float(line['cyl']):+.2f}")
        if line.get("axis"):
            parts.append(f"AX {int(line['axis'])}")
        if line.get("add_power") and float(line.get("add_power") or 0) > 0:
            parts.append(f"ADD +{float(line['add_power']):.2f}")
    except Exception as _e:
        pass
    return "  ".join(parts)


def _render_po_creation(cart: list, _qpo, _rwpo, _uparse_po):
    """Step 3a — Create PO and send to supplier via WhatsApp."""
    import datetime as _dt_po

    st.markdown("### 📤 Create Purchase Order")

    _sups = _qpo("""
        SELECT id::text AS id, party_name,
               COALESCE(mobile,'') AS mobile,
               COALESCE(whatsapp, mobile, '') AS whatsapp
        FROM parties
        WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB')
          AND COALESCE(is_active,TRUE)=TRUE
        ORDER BY party_name
    """)
    if not _sups:
        _sups = _qpo("SELECT id::text AS id, party_name, '' AS mobile, '' AS whatsapp FROM parties WHERE COALESCE(is_active,TRUE)=TRUE ORDER BY party_name")

    if not _sups:
        st.warning("No suppliers found in party master.")
        return

    _sup_ids  = [s["id"] for s in _sups]
    _sup_map  = {s["id"]: s["party_name"] for s in _sups}
    _sup_mob  = {s["id"]: s.get("whatsapp") or s.get("mobile","") for s in _sups}

    _pc1, _pc2 = st.columns(2)
    with _pc1:
        _sel_sup = st.selectbox("Supplier *", _sup_ids,
                                format_func=lambda x: _sup_map.get(x, x),
                                key="po_cr_sup")
        _po_date = st.date_input("Order Date", value=_dt_po.date.today(),
                                 key="po_cr_date", format="DD/MM/YYYY")
    with _pc2:
        _exp_del = st.date_input("Expected Delivery",
                                  value=_dt_po.date.today() + _dt_po.timedelta(days=7),
                                  key="po_cr_exp", format="DD/MM/YYYY")
        _po_notes = st.text_input("Notes / Instructions",
                                   placeholder="e.g. Urgent, handle with care",
                                   key="po_cr_notes")

    st.markdown("#### Order Lines")
    _po_total = 0.0
    for _ln in cart:
        _pwr = _fmt_power_po(_ln)
        _lv  = float(_ln.get("unit_price",0)) * int(_ln.get("quantity",1))
        _po_total += _lv
        st.markdown(
            f"<div style='padding:4px 12px;border-left:3px solid #334155;margin:2px 0;"
            f"font-size:0.82rem;color:#94a3b8'>"
            f"<b style='color:#e2e8f0'>{_ln.get('order_no','')} · "
            f"{str(_ln.get('eye_side','')).upper()}</b> · "
            f"{_ln.get('product_name','')} {_pwr} · "
            f"Qty {_ln.get('quantity',1)} · &#8377;{float(_ln.get('unit_price',0)):,.0f}"
            f"</div>",
            unsafe_allow_html=True
        )

    st.metric("Total Order Value", f"₹{_po_total:,.2f}")

    _key_po = "po_do_create_po"
    if st.button("📤 Create PO", key="po_create_btn",
                 type="primary", use_container_width=True):
        st.session_state[_key_po] = True

    if st.session_state.pop(_key_po, False):
        try:
            from modules.sql_adapter import run_query as _rq2, run_write as _rw2
            import datetime as _dtp2
            _now = _dtp2.datetime.now()
            _po_ref_tmp = f"PO-{_now.strftime('%Y%m%d%H%M%S')}"

            _sync_supplier_orders_id_sequence()
            _rw2("""
                INSERT INTO supplier_orders (
                    supplier_order_id, supplier_id, supplier_name,
                    order_date, expected_delivery_date, status, po_type,
                    total_value, total_items, total_qty,
                    special_instructions, created_by, created_at, updated_at
                ) VALUES (
                    %(ref)s, %(sid)s::uuid, %(sname)s,
                    %(odate)s, %(edate)s, 'DRAFT', 'CONVERSION',
                    %(total)s, %(items)s, %(qty)s,
                    %(notes)s, 'orders_to_purchase', NOW(), NOW()
                )
            """, {
                "ref":   _po_ref_tmp,
                "sid":   _sel_sup,
                "sname": _sup_map.get(_sel_sup,""),
                "odate": str(_po_date),
                "edate": str(_exp_del),
                "total": round(_po_total, 2),
                "items": len(cart),
                "qty":   sum(int(i.get("quantity",1)) for i in cart),
                "notes": _po_notes,
            })

            _res = _rq2("""
                SELECT id FROM supplier_orders
                WHERE supplier_order_id=%(ref)s AND created_by='orders_to_purchase'
                ORDER BY created_at DESC LIMIT 1
            """, {"ref": _po_ref_tmp})
            _po_int_id = int(_res[0]["id"]) if _res else 0

            if _po_int_id:
                _rw2("UPDATE supplier_orders SET supplier_order_id=%(ref)s WHERE id=%(id)s",
                     {"ref": f"PO-{_po_int_id}", "id": _po_int_id})
                for _idx, _ln in enumerate(cart, 1):
                    _rw2("""
                        INSERT INTO supplier_order_items (
                            supplier_order_id, item_no, product_id, product_name,
                            eye_side, sph, cyl, axis, add_power,
                            ordered_qty, unit_price, total_price, item_status,
                            customer_line_id
                        ) VALUES (
                            %(soid)s, %(ino)s, %(pid)s::uuid, %(pname)s,
                            %(eye)s, %(sph)s, %(cyl)s, %(axis)s, %(add)s,
                            %(qty)s, %(price)s, %(total)s, 'PENDING',
                            NULLIF(%(clid)s,'')
                        )
                    """, {
                        "soid":  _po_int_id, "ino": _idx,
                        "pid":   _ln.get("product_id"),
                        "pname": _ln.get("product_name",""),
                        "eye":   _ln.get("eye_side",""),
                        "sph":   _ln.get("sph"),   "cyl": _ln.get("cyl"),
                        "axis":  _ln.get("axis"),  "add": _ln.get("add_power"),
                        "qty":   int(_ln.get("quantity",1)),
                        "price": float(_ln.get("unit_price",0)),
                        "total": float(_ln.get("unit_price",0)) * int(_ln.get("quantity",1)),
                        "clid":  _ln.get("line_id"),
                    })

                # WhatsApp PO message
                _mob = _sup_mob.get(_sel_sup,"")
                _wa_d = "".join(d for d in _mob if d.isdigit())
                if _wa_d.startswith("91") and len(_wa_d)==12: _wa_d = _wa_d[2:]
                _wa_num = f"91{_wa_d}" if len(_wa_d)==10 else ""

                _wa_msg_lines = [
                    f"*Purchase Order PO-{_po_int_id}*",
                    f"Date: {_po_date}  |  Expected: {_exp_del}", ""
                ]
                for _ln in cart:
                    _pwr2 = _fmt_power_po(_ln)
                    _wa_msg_lines.append(
                        f"• {_ln.get('product_name','')} "
                        f"({str(_ln.get('eye_side','')).upper()}) "
                        f"{_pwr2} — Qty {_ln.get('quantity',1)}"
                    )
                if _po_notes:
                    _wa_msg_lines += ["", f"Note: {_po_notes}"]
                _wa_msg_lines += ["", "Please confirm receipt. 🙏"]
                _wa_msg = "\n".join(_wa_msg_lines)

                st.success(f"&#10003; PO-{_po_int_id} created — {len(cart)} line(s) · &#8377;{_po_total:,.2f}")
                if _wa_num:
                    st.link_button(
                        "📲 Send PO to Supplier via WhatsApp",
                        f"https://wa.me/{_wa_num}?text={_uparse_po.quote(_wa_msg)}",
                        use_container_width=True
                    )
                st.session_state.po_cart   = []
                st.session_state.po_action = None
                st.session_state.pop("po_rows_cache", None)
                st.rerun()
        except Exception as _poe:
            import traceback
            st.error(f"PO error: {_poe}")
            st.code(traceback.format_exc())


def _render_purchase_invoice(cart: list, _qpo, _rwpo):
    """
    Record Purchase Invoice.
    - Price per BOX from DB dropdown (box_size applied for total)
    - Contact lenses: batch + expiry required
    - Ophthalmic / frames: no batch/expiry
    - Service charges (courier etc.) with optional GST @18%
    """
    import datetime as _dt_inv

    st.markdown("### 🧾 Record Purchase Invoice")
    st.caption(
        "Price is per box for BOX items, per piece for PCS items. "
        "Contact lenses 🔵: batch + expiry required."
    )

    _sups_inv = _qpo("""
        SELECT id::text AS id, party_name,
               COALESCE(state_code,'') AS state_code
        FROM parties
        WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB')
          AND COALESCE(is_active,TRUE)=TRUE
        ORDER BY party_name
    """)
    if not _sups_inv:
        _sups_inv = _qpo("SELECT id::text AS id, party_name, '' AS state_code "
                         "FROM parties WHERE COALESCE(is_active,TRUE)=TRUE ORDER BY party_name")
    if not _sups_inv:
        st.warning("No suppliers found."); return

    _sup_inv_ids = [s["id"] for s in _sups_inv]
    _sup_inv_map = {s["id"]: s for s in _sups_inv}

    # Invoice header
    _ih1, _ih2, _ih3 = st.columns(3)
    with _ih1:
        _inv_sup = st.selectbox("Supplier *", _sup_inv_ids,
                                format_func=lambda x: _sup_inv_map.get(x,{}).get("party_name",x),
                                key="inv_sup")
        _inv_no  = st.text_input("Invoice / Challan No. *",
                                  placeholder="e.g. INV/2025-26/001",
                                  key="inv_no")
    with _ih2:
        _inv_date      = st.date_input("Invoice Date", value=_dt_inv.date.today(),
                                        key="inv_date", format="DD/MM/YYYY")
        _inv_transport = st.text_input("Transport / Courier",
                                        placeholder="e.g. DTDC, FedEx",
                                        key="inv_transport")
    with _ih3:
        _inv_lr    = st.text_input("LR / AWB No.", key="inv_lr",
                                    placeholder="e.g. LR-12345")
        _inv_notes = st.text_input("Notes", key="inv_notes_f",
                                    placeholder="e.g. Partial supply...")

    # GST type
    _company_state = "27"
    _sup_state = (_sup_inv_map.get(_inv_sup, {}).get("state_code") or "").strip()[:2]
    _is_igst   = bool(_sup_state and _sup_state != _company_state)

    # Line Items header
    st.markdown("#### 📦 Line Items")
    _hc = st.columns([3, 1, 2, 2, 2, 2, 2])
    for _hcol, _hl in zip(_hc, ["Product / Power", "Eye",
                                  "Qty (pcs / boxes)",
                                  "Price ₹/box or ₹/pc",
                                  "Batch No", "Expiry", "Line Total"]):
        _hcol.markdown(
            f"<span style='font-size:0.68rem;color:#64748b;font-weight:700'>{_hl}</span>",
            unsafe_allow_html=True
        )

    _inv_lines      = []
    _goods_subtotal = 0.0

    for _ln in cart:
        _lid   = _ln.get("line_id","")
        _eye   = str(_ln.get("eye_side","")).upper()
        _pname = (_ln.get("product_name") or "")[:35]
        _qty   = int(_ln.get("quantity") or 1)
        _pwr   = _fmt_power_po(_ln)
        _cat   = str(_ln.get("category","")).upper()
        _unit  = str(_ln.get("unit","PCS")).upper()
        _bsize = int(_ln.get("box_size") or 1)
        _is_cl = any(k in _cat for k in ("CONTACT","CL","SOFT","HARD LENS"))

        # Box-to-piece display — use governor for correct label
        if _unit == "BOX" and _bsize > 1:
            _n_boxes   = _qty // _bsize
            _extra_pcs = _qty % _bsize
            try:
                from modules.core.price_qty_governor import box_qty_label as _bql
                _qty_disp = _bql(_n_boxes, _extra_pcs, _bsize)
            except ImportError:
                _qty_disp = f"{_n_boxes} Box ({_qty} pcs)"
                if _extra_pcs:
                    _qty_disp = f"{_n_boxes} Box + {_extra_pcs} pcs ({_qty} pcs)"
            _price_lbl = "₹/box"
        else:
            _qty_disp  = f"{_qty} pcs"
            _price_lbl = "₹/pc"

        # Fetch DB prices AND all batches for this product
        _pdb = _qpo("""
            SELECT DISTINCT
                COALESCE(NULLIF(purchase_price,0), NULLIF(purchase_rate,0), 0)::numeric AS price,
                COALESCE(batch_no,'')   AS batch_no,
                expiry_date::text       AS expiry
            FROM inventory_stock
            WHERE product_id = %(pid)s::uuid
              AND COALESCE(is_active,TRUE)=TRUE
            ORDER BY expiry DESC NULLS LAST
            LIMIT 20
        """, {"pid": _ln.get("product_id","")})

        _price_vals     = list(dict.fromkeys(
            float(r["price"]) for r in _pdb if float(r.get("price") or 0) > 0
        ))
        _price_lbls     = [f"₹{p:,.2f} ({_price_lbl})" for p in _price_vals] + ["Enter manually"]
        _price_vals_ext = _price_vals + [None]

        _lc = st.columns([3, 1, 2, 2, 2, 2, 2])

        _lc[0].markdown(
            f"<span style='font-size:0.8rem;color:#e2e8f0'><b>{_pname}</b>"
            + (f"<br><span style='color:#64748b;font-size:0.7rem'>{_pwr}</span>" if _pwr else "")
            + ("&nbsp;🔵" if _is_cl else "")
            + "</span>", unsafe_allow_html=True
        )
        _lc[1].markdown(
            f"<span style='color:#94a3b8;font-weight:700;font-size:0.82rem'>{_eye}</span>",
            unsafe_allow_html=True
        )
        _lc[2].markdown(
            f"<span style='color:#f1f5f9;font-size:0.78rem'>{_qty_disp}</span>",
            unsafe_allow_html=True
        )

        # Price dropdown — stable key using UUID tail
        _hkey = str(_lid).replace('-','')[-10:]
        if _price_vals:
            _psel = _lc[3].selectbox(
                "", options=range(len(_price_lbls)),
                format_func=lambda i: _price_lbls[i],
                key=f"inv_psel_{_hkey}", label_visibility="collapsed"
            )
            if _price_vals_ext[_psel] is None:
                _final_price = _lc[3].number_input(
                    "", min_value=0.0, value=0.0, step=1.0, format="%.2f",
                    key=f"inv_pman_{_hkey}", label_visibility="collapsed"
                )
            else:
                _final_price = float(_price_vals_ext[_psel])
        else:
            _final_price = _lc[3].number_input(
                "", min_value=0.0, value=0.0, step=1.0, format="%.2f",
                key=f"inv_pent_{_hkey}", label_visibility="collapsed",
                help=f"No price in DB — enter {_price_lbl}"
            )

        # Line total — use price_qty_governor for correct box math (no rounding error)
        try:
            from modules.core.price_qty_governor import normalize_box_total as _nbt
            _line_total = _nbt(_final_price, _qty, {"unit": _unit, "box_size": _bsize})
        except ImportError:
            # Fallback if governor not available: correct box math inline
            if _unit == "BOX" and _bsize > 1:
                _full_boxes = _qty // _bsize
                _loose_pcs  = _qty % _bsize
                _line_total = round(_full_boxes * _final_price, 2)
                if _loose_pcs > 0:
                    _line_total = round(_line_total + _loose_pcs * (_final_price / _bsize), 2)
            else:
                _line_total = round(_final_price * _qty, 2)
        _goods_subtotal += _line_total

        # Batch — contact lens only: selectbox from existing inventory batches
        if _is_cl:
            # Build batch options from DB — all batches for this product
            _batch_opts = [
                {"batch": r.get("batch_no",""), "expiry": r.get("expiry","")}
                for r in _pdb if r.get("batch_no")
            ]
            # Deduplicate by batch_no
            _seen_b = set()
            _batch_opts_clean = []
            for _bo in _batch_opts:
                if _bo["batch"] not in _seen_b:
                    _seen_b.add(_bo["batch"])
                    _batch_opts_clean.append(_bo)

            if _batch_opts_clean:
                _batch_labels = [
                    f"{_bo['batch']}"
                    + (f"  exp:{str(_bo['expiry'])[:7]}" if _bo.get('expiry') else "")
                    for _bo in _batch_opts_clean
                ] + ["+ Enter new batch"]
                _batch_vals = [_bo["batch"] for _bo in _batch_opts_clean] + ["__NEW__"]

                _bsel = _lc[4].selectbox(
                    "Batch", options=range(len(_batch_labels)),
                    format_func=lambda i: _batch_labels[i],
                    key=f"inv_bsel_{_hkey}",
                    label_visibility="collapsed"
                )
                if _batch_vals[_bsel] == "__NEW__":
                    _batch = _lc[4].text_input(
                        "New Batch No", placeholder="e.g. 10024458994",
                        key=f"inv_bnew_{_hkey}",
                        label_visibility="collapsed"
                    )
                    _auto_expiry = None
                else:
                    _batch     = _batch_vals[_bsel]
                    _auto_expiry = _batch_opts_clean[_bsel].get("expiry")
            else:
                # No batches in DB — free text
                _batch = _lc[4].text_input(
                    "Batch No", placeholder="Batch No *",
                    key=f"inv_b_{_hkey}",
                    label_visibility="collapsed"
                )
                _auto_expiry = None
        else:
            _batch       = ""
            _auto_expiry = None
            _lc[4].markdown("<span style='color:#334155;font-size:0.72rem'>—</span>",
                             unsafe_allow_html=True)

        # Expiry — auto-filled from selected batch, editable
        if _is_cl:
            try:
                _exp_def = _dt_inv.date.fromisoformat(str(_auto_expiry)[:10]) if _auto_expiry else None
            except Exception as _e:
                _exp_def = None
            if f"inv_e_{_hkey}" not in st.session_state and _exp_def:
                st.session_state[f"inv_e_{_hkey}"] = _exp_def
            _expiry = _lc[5].date_input("Expiry",
                                         key=f"inv_e_{_hkey}",
                                         label_visibility="collapsed",
                                         format="DD/MM/YYYY")
        else:
            _expiry = None
            _lc[5].markdown("<span style='color:#334155;font-size:0.72rem'>—</span>",
                             unsafe_allow_html=True)

        _lc[6].markdown(
            f"<span style='color:#10b981;font-size:0.82rem;font-weight:700'>"
            f"&#8377;{_line_total:,.2f}</span>", unsafe_allow_html=True
        )
        _inv_lines.append({
            "line_id":      _lid,
            "order_no":     _ln.get("order_no",""),
            "order_id":     _ln.get("order_id",""),
            "product_id":   _ln.get("product_id",""),
            "product_name": _pname,
            "eye_side":     _eye,
            "qty":          _qty,
            "unit":         _unit,
            "box_size":     _bsize,
            "price":        _final_price,
            "total":        _line_total,
            "batch_no":     _batch,
            "expiry":       str(_expiry) if _expiry else None,
            "is_cl":        _is_cl,
            "transport":    _inv_transport,
            "lr_no":        _inv_lr,
        })

    # Service / Courier Charges
    st.markdown("#### 🚚 Service / Courier Charges")
    _svc1, _svc2, _svc3, _svc4, _svc5 = st.columns([3, 2, 2, 2, 2])
    with _svc1:
        _svc_desc = st.text_input("Service Description", value="Courier Charges",
                                   key="inv_svc_desc",
                                   placeholder="e.g. Courier, Packing, Handling")
    with _svc2:
        _svc_amount = st.number_input("Amount ₹", min_value=0.0, value=0.0,
                                       step=10.0, format="%.2f", key="inv_svc_amt")
    with _svc3:
        _svc_gst_rate = st.selectbox("GST Rate", [0, 5, 18],
                                      format_func=lambda x: "No GST" if x==0 else f"{x}%",
                                      index=2, key="inv_svc_gst_rate",
                                      help="5% for courier/goods, 18% for services")
    with _svc4:
        _svc_gst = _svc_gst_rate > 0
        if _svc_gst and _svc_amount > 0:
            _svc_gst_amt = round(_svc_amount * (_svc_gst_rate / 100), 2)
            st.metric(f"IGST {_svc_gst_rate}%" if _is_igst else f"CGST+SGST {_svc_gst_rate//2}%+{_svc_gst_rate//2}%",
                      f"₹{_svc_gst_amt:,.2f}")
        else:
            _svc_gst_amt = 0.0
            st.metric("Service GST", "—")
    with _svc5:
        if _svc_gst and _svc_amount > 0:
            st.metric("Service Total", f"₹{_svc_amount + _svc_gst_amt:,.2f}")
        else:
            st.metric("Service Total", "—")

    _svc_total = _svc_amount + _svc_gst_amt

    # Add service charges as a line item for tracking
    if _svc_amount > 0:
        _inv_lines.append({
            "line_id":      "SERVICE",
            "order_no":     cart[0].get("order_no","") if cart else "",
            "order_id":     cart[0].get("order_id","") if cart else "",
            "product_id":   None,
            "product_name": _svc_desc or "Courier Charges",
            "eye_side":     "",
            "qty":          1,
            "unit":         "PCS",
            "box_size":     1,
            "price":        _svc_amount,
            "total":        _svc_total,
            "batch_no":     None,
            "expiry":       None,
            "is_cl":        False,
            "transport":    _inv_transport,
            "lr_no":        _inv_lr,
            "is_service":   True,
            "courier_gst_rate": _svc_gst_rate,
            "courier_gst_amount": _svc_gst_amt,
        })

    # Totals
    st.markdown("---")
    _t1, _t2, _t3, _t4 = st.columns(4)
    _t1.metric("Goods Subtotal", f"₹{_goods_subtotal:,.2f}")
    _t2.metric(
        f"{_svc_desc or 'Service'}" + (" + GST 18%" if _svc_gst else ""),
        f"₹{_svc_total:,.2f}"
    )
    _goods_gst = round(_goods_subtotal * 0.05, 2)
    _t3.metric(
        f"Goods GST 5% ({'IGST' if _is_igst else 'CGST+SGST'}) — input credit",
        f"₹{_goods_gst:,.2f}",
        help="For GST input credit only"
    )
    _invoice_total = round(_goods_subtotal + _svc_total, 2)
    _t4.metric("Invoice Total (Payable)", f"₹{_invoice_total:,.2f}")

    st.caption(
        f"Supplier state: **{_sup_state or '?'}** · "
        f"{'Inter-state → IGST' if _is_igst else 'Intra-state → CGST+SGST'}"
    )

    # Validation
    _cl_miss  = [d["product_name"] for d in _inv_lines
                 if d["is_cl"] and (not d.get("batch_no") or not d.get("expiry"))]
    _no_price = [d["product_name"] for d in _inv_lines if d["price"] <= 0]
    if _cl_miss:
        st.warning("⚠️ Batch + Expiry required for: " + ", ".join(_cl_miss[:3]))
    if _no_price:
        st.warning("⚠️ Enter purchase price for: " + ", ".join(_no_price[:3]))

    _can_save = bool(_inv_no.strip()) and not _cl_miss and not _no_price

    _key_inv = "po_do_inv_save"
    if st.button("💾 Save Purchase Invoice", key="inv_save_btn",
                 type="primary", use_container_width=True,
                 disabled=not _can_save):
        st.session_state[_key_inv] = True

    if st.session_state.pop(_key_inv, False):
        _ok = True
        for _d in _inv_lines:
            # Skip service lines - they need separate handling
            if _d.get("is_service"):
                continue
                
            _ok = _ok and _rwpo("""
                INSERT INTO purchase_acknowledgements (
                    order_line_id, order_id, order_no,
                    product_id, product_name, eye_side,
                    supplier_id, supplier_name,
                    challan_no, invoice_no, document_date,
                    qty, received_qty, purchase_price, total_value,
                    billing_status, is_price_locked, acknowledged_at,
                    batch_no, expiry_date, transport, lr_no
                ) VALUES (
                    %(lid)s::uuid, %(oid)s::uuid, %(ono)s,
                    %(pid)s::uuid, %(pname)s, %(eye)s,
                    %(sid)s::uuid, %(sname)s,
                    %(chal)s, %(inv)s, %(ddate)s::date,
                    %(qty)s, %(qty)s, %(price)s, %(total)s,
                    'NOT_READY', TRUE, NOW(),
                    %(batch)s, %(expiry)s::date, %(transport)s, %(lr)s
                )
                ON CONFLICT (order_line_id) DO UPDATE SET
                    invoice_no      = EXCLUDED.invoice_no,
                    challan_no      = COALESCE(
                                        purchase_acknowledgements.challan_no,
                                        EXCLUDED.challan_no),
                    document_date   = EXCLUDED.document_date,
                    supplier_id     = EXCLUDED.supplier_id,
                    supplier_name   = EXCLUDED.supplier_name,
                    purchase_price  = CASE
                        WHEN purchase_acknowledgements.is_price_locked
                        THEN purchase_acknowledgements.purchase_price
                        ELSE EXCLUDED.purchase_price END,
                    total_value     = CASE
                        WHEN purchase_acknowledgements.is_price_locked
                        THEN purchase_acknowledgements.total_value
                        ELSE EXCLUDED.total_value END,
                    is_price_locked = TRUE,
                    acknowledged_at = NOW(),
                    batch_no        = COALESCE(purchase_acknowledgements.batch_no, EXCLUDED.batch_no),
                    expiry_date     = COALESCE(purchase_acknowledgements.expiry_date, EXCLUDED.expiry_date),
                    transport       = COALESCE(purchase_acknowledgements.transport, EXCLUDED.transport),
                    lr_no           = COALESCE(purchase_acknowledgements.lr_no, EXCLUDED.lr_no)
            """, {
                "lid":   _d["line_id"],
                "oid":   _d["order_id"] or "00000000-0000-0000-0000-000000000000",
                "ono":   _d["order_no"],
                "pid":   _d["product_id"] or "00000000-0000-0000-0000-000000000000",
                "pname": _d["product_name"],
                "eye":   _d["eye_side"],
                "sid":   _inv_sup,
                "sname": _sup_inv_map.get(_inv_sup,{}).get("party_name",""),
                "chal":  _inv_no.strip(),
                "inv":   _inv_no.strip(),
                "ddate": str(_inv_date),
                "qty":   _d["qty"],
                "price": _d["price"],
                "total": _d["total"],
                "batch": _d.get("batch_no") or None,
                "expiry": _d.get("expiry") or None,
                "transport": _d.get("transport") or None,
                "lr": _d.get("lr_no") or None,
            })
            if _d["is_cl"] and (_d.get("batch_no") or _d.get("expiry")):
                # Target ONLY the specific batch row — adding batch_no to WHERE
                # prevents touching other rows and avoids unique constraint violations
                _rwpo("""
                    UPDATE inventory_stock SET
                        expiry_date    = COALESCE(%(e)s::date, expiry_date),
                        purchase_price = CASE WHEN %(p)s > 0
                                         THEN %(p)s ELSE purchase_price END,
                        purchase_rate  = CASE WHEN %(p)s > 0
                                         THEN %(p)s ELSE purchase_rate END,
                        updated_at     = NOW()
                    WHERE product_id = %(pid)s::uuid
                      AND batch_no    = %(b)s
                      AND COALESCE(is_active,TRUE)=TRUE
                """, {
                    "b":   _d.get("batch_no"),
                    "e":   _d.get("expiry") or None,
                    "p":   _d["price"],
                    "pid": _d["product_id"],
                })

        # Save service charges (courier) as separate record without order_line_id
        _svc_line = next((d for d in _inv_lines if d.get("is_service")), None)
        if _svc_line and _ok:
            _ok = _rwpo("""
                INSERT INTO purchase_acknowledgements (
                    order_no, product_name, qty, purchase_price, total_value,
                    billing_status, is_price_locked, acknowledged_at,
                    supplier_id, supplier_name, challan_no, invoice_no, document_date,
                    transport, lr_no, courier_gst_rate, courier_gst_amount
                ) VALUES (
                    %(ono)s, %(pname)s, 1, %(price)s, %(total)s,
                    'NOT_READY', TRUE, NOW(),
                    %(sid)s::uuid, %(sname)s, %(chal)s, %(inv)s, %(ddate)s::date,
                    %(transport)s, %(lr)s, %(cgst)s, %(cgsta)s
                )
            """, {
                "ono": _svc_line["order_no"],
                "pname": _svc_line["product_name"],
                "price": _svc_line["price"],
                "total": _svc_line["total"],
                "sid":   _inv_sup,
                "sname": _sup_inv_map.get(_inv_sup,{}).get("party_name",""),
                "chal":  _inv_no.strip(),
                "inv":   _inv_no.strip(),
                "ddate": str(_inv_date),
                "transport": _svc_line.get("transport") or None,
                "lr": _svc_line.get("lr_no") or None,
                "cgst": _svc_line.get("courier_gst_rate", 0),
                "cgsta": _svc_line.get("courier_gst_amount", 0),
            })

        if _ok:
            st.success(
                f"&#10003; Invoice **{_inv_no}** saved — "
                f"{len(_inv_lines)} line(s) · &#8377;{_invoice_total:,.2f}"
                + (f" (incl. {_svc_desc} &#8377;{_svc_total:,.2f})"
                   if _svc_amount > 0 else "")
            )
            st.session_state.po_cart   = []
            st.session_state.po_action = None
            st.session_state.pop("po_rows_cache", None)
            st.rerun()


def _render_blank_purchase(cart: list, _qpo, _rwpo):
    """Step 3c — Record Blank Purchase for in-house surfacing."""
    import datetime as _dt_blk

    st.markdown("### 📦 Record Blank Purchase (In-house)")
    st.caption("Updates blank_inventory for in-house surfacing.")

    _sups_b = _qpo("""
        SELECT id::text AS id, party_name FROM parties
        WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR')
          AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name
    """)
    _bsids  = [s["id"] for s in _sups_b]
    _bsmap  = {s["id"]: s["party_name"] for s in _sups_b}

    _bk1, _bk2, _bk3 = st.columns(3)
    _bsup  = _bk1.selectbox("Supplier *", _bsids, format_func=lambda x: _bsmap.get(x,x), key="blk_sup")
    _bchal = _bk2.text_input("Challan / Invoice No.", key="blk_chal", placeholder="CH-001")
    _bdate = _bk3.date_input("Date", value=_dt_blk.date.today(), key="blk_date", format="DD/MM/YYYY")

    st.markdown("#### Items")
    _btotal = 0.0
    for _ln in cart:
        _pdb2 = _qpo("""
            SELECT COALESCE(NULLIF(purchase_price,0), NULLIF(purchase_rate,0), 0)::numeric AS price
            FROM inventory_stock WHERE product_id=%(pid)s::uuid AND COALESCE(is_active,TRUE)=TRUE
            ORDER BY created_at DESC LIMIT 1
        """, {"pid": _ln.get("product_id","")})
        _def_p = float(_pdb2[0]["price"]) if _pdb2 else 0.0
        _pwr3  = _fmt_power_po(_ln)
        _bc1, _bc2 = st.columns([4, 2])
        _bc1.markdown(
            f"<span style='color:#e2e8f0;font-size:0.82rem'>"
            f"<b>{_ln.get('product_name','')}</b> {_pwr3} · Qty {_ln.get('quantity',1)}</span>",
            unsafe_allow_html=True
        )
        _pin = _bc2.number_input("₹/pc", min_value=0.0, value=_def_p, step=1.0, format="%.2f",
                                  key=f"blk_p_{_ln.get('line_id','')[:8]}", label_visibility="collapsed")
        _ln["_blk_price"] = _pin
        _btotal += _pin * int(_ln.get("quantity",1))

    st.metric("Total", f"₹{_btotal:,.2f}")

    _key_blk = "po_do_blank_save"
    if st.button("📦 Record Blank Purchase", key="blk_save",
                 type="primary", use_container_width=True, disabled=not _bsup):
        st.session_state[_key_blk] = True

    if st.session_state.pop(_key_blk, False):
        _ok = True
        for _ln in cart:
            _pid2 = _ln.get("product_id","")
            _qty2 = int(_ln.get("quantity",1))
            _prc2 = float(_ln.get("_blk_price",0))
            _ex   = _qpo("""
                SELECT id FROM blank_inventory WHERE product_id=%(pid)s::uuid LIMIT 1
            """, {"pid": _pid2})
            if _ex:
                _ok = _ok and _rwpo("""
                    UPDATE blank_inventory SET
                        qty_independent = COALESCE(qty_independent,0) + %(qty)s,
                        cost_price = CASE WHEN %(p)s > 0 THEN %(p)s ELSE cost_price END,
                        updated_at = NOW()
                    WHERE id=%(bid)s::uuid
                """, {"qty": _qty2, "p": _prc2, "bid": str(_ex[0]["id"])})
            else:
                _ok = _ok and _rwpo("""
                    INSERT INTO blank_inventory (product_id, qty_independent, cost_price, is_active, created_at, updated_at)
                    VALUES (%(pid)s::uuid, %(qty)s, %(p)s, TRUE, NOW(), NOW())
                """, {"pid": _pid2, "qty": _qty2, "p": _prc2})

            _rwpo("""
                INSERT INTO purchase_acknowledgements (
                    order_line_id, order_id, order_no, product_id, product_name, eye_side,
                    supplier_id, supplier_name, challan_no, document_date,
                    qty, received_qty, purchase_price, total_value,
                    billing_status, is_price_locked, acknowledged_at
                ) VALUES (
                    %(lid)s::uuid, %(oid)s::uuid, %(ono)s, %(pid)s::uuid, %(pname)s, %(eye)s,
                    %(sid)s::uuid, %(sname)s, %(chal)s, %(ddate)s::date,
                    %(qty)s, %(qty)s, %(p)s, %(total)s,
                    'NOT_READY', TRUE, NOW()
                )
                ON CONFLICT (order_line_id) DO UPDATE SET
                    challan_no = COALESCE(purchase_acknowledgements.challan_no, EXCLUDED.challan_no),
                    purchase_price = CASE WHEN purchase_acknowledgements.is_price_locked
                                     THEN purchase_acknowledgements.purchase_price
                                     ELSE EXCLUDED.purchase_price END,
                    is_price_locked = TRUE, acknowledged_at = NOW()
            """, {
                "lid": _ln.get("line_id","00000000-0000-0000-0000-000000000000"),
                "oid": _ln.get("order_id","00000000-0000-0000-0000-000000000000"),
                "ono": _ln.get("order_no",""),
                "pid": _pid2 or "00000000-0000-0000-0000-000000000000",
                "pname": _ln.get("product_name",""),
                "eye": str(_ln.get("eye_side","")).upper(),
                "sid": _bsup, "sname": _bsmap.get(_bsup,""),
                "chal": _bchal.strip() or "",
                "ddate": str(_bdate),
                "qty": _qty2, "p": _prc2,
                "total": round(_prc2 * _qty2, 2),
            })
        if _ok:
            st.success(f"&#10003; Blank purchase recorded — {len(cart)} item(s) · &#8377;{_btotal:,.2f}")
            st.session_state.po_cart   = []
            st.session_state.po_action = None
            st.rerun()


def _render_open_pos_tab():
    """Render tab showing open purchase orders."""
    import urllib.parse as _upo
    st.markdown("### 📦 Open Purchase Orders")
    st.caption("Purchase orders created from Sales Orders")

    def _q(sql, params=None):
        try:
            from modules.sql_adapter import run_query
            return run_query(sql, params or {}) or []
        except Exception as e:
            st.error(f"Query error: {e}"); return []

    def _w(sql, params=None):
        try:
            from modules.sql_adapter import run_write
            run_write(sql, params or {}); return True
        except Exception as e:
            st.error(f"Write error: {e}"); return False

    pos = _q("""
        SELECT id, supplier_order_id, supplier_name, supplier_id::text AS supplier_id, customer_order_id,
               order_date, expected_delivery_date, status, po_type,
               total_value, total_items, total_qty, created_at
        FROM supplier_orders
        WHERE status NOT IN ('RECEIVED','CLOSED','CANCELLED')
        ORDER BY created_at DESC
    """)

    if not pos:
        st.info("No open purchase orders found.")
        return

    st.caption(f"{len(pos)} open PO(s)")

    _st_color = {"DRAFT":"#64748b","SENT":"#3b82f6","ACKNOWLEDGED":"#10b981","PARTIAL":"#f59e0b"}

    for po in pos:
        _po_id  = po.get("id")
        _po_no  = po.get("supplier_order_id","—")
        _sup    = po.get("supplier_name","—")
        _status = str(po.get("status","DRAFT")).upper()
        _val    = float(po.get("total_value") or 0)
        _its    = int(po.get("total_items") or 0)
        _odate  = str(po.get("order_date",""))[:10]
        _exp    = str(po.get("expected_delivery_date",""))[:10]
        _clr    = _st_color.get(_status,"#475569")

        _kdet = f"po_open_det_{_po_id}"
        _cc1, _cc2 = st.columns([6, 4])
        with _cc1:
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e293b;"
                f"border-left:4px solid {_clr};border-radius:6px;"
                f"padding:8px 14px;margin-bottom:3px'>"
                f"<span style='color:#f1f5f9;font-weight:800;font-family:monospace'>{_po_no}</span>"
                f" <span style='color:#94a3b8;font-size:0.82rem'>{_sup}</span>"
                f" <span style='background:{_clr}22;color:{_clr};font-size:0.68rem;"
                f"font-weight:700;padding:2px 8px;border-radius:8px'>{_status}</span>"
                f"<div style='color:#475569;font-size:0.7rem;margin-top:2px'>"
                f"{_its} items · &#8377;{_val:,.0f}"
                + (f" · {_odate}" if _odate else "")
                + (f" → {_exp}" if _exp else "")
                + "</div></div>",
                unsafe_allow_html=True
            )
        with _cc2:
            _ab1, _ab2, _ab3, _ab4 = st.columns(4)
            with _ab1:
                if st.button("👁", key=f"po_open_v_{_po_id}", use_container_width=True,
                             help="View items"):
                    st.session_state[_kdet] = not st.session_state.get(_kdet, False)
                    st.rerun()
            with _ab2:
                if _status == "DRAFT":
                    if st.button("📤", key=f"po_open_s_{_po_id}", use_container_width=True,
                                 help="Mark as Sent", type="primary"):
                        _w("UPDATE supplier_orders SET status='SENT',updated_at=NOW() WHERE id=%s", (_po_id,))
                        st.rerun()
            with _ab3:
                if _status in ("SENT","ACKNOWLEDGED","PARTIAL"):
                    if st.button("✅", key=f"po_open_r_{_po_id}", use_container_width=True,
                                 help="Mark as Received", type="primary"):
                        _w("UPDATE supplier_orders SET status='RECEIVED',updated_at=NOW() WHERE id=%s", (_po_id,))
                        st.rerun()
            with _ab4:
                if _status not in ("RECEIVED","CANCELLED"):
                    if st.button("🗑", key=f"po_open_c_{_po_id}", use_container_width=True,
                                 help="Cancel PO"):
                        _w("UPDATE supplier_orders SET status='CANCELLED',updated_at=NOW() WHERE id=%s", (_po_id,))
                        st.rerun()

        if st.session_state.get(_kdet):
            items = _q("""
                SELECT item_no, product_name, eye_side, sph, cyl, axis,
                       add_power, ordered_qty, received_qty, unit_price, item_status,
                       customer_line_id
                FROM supplier_order_items WHERE supplier_order_id=%s ORDER BY item_no
            """, (_po_id,))
            if items:
                for _it in items:
                    _pwr = ""
                    if _it.get("sph") is not None:
                        try:
                            _pwr = f"SPH {float(_it['sph']):+.2f}"
                            if _it.get("cyl") and abs(float(_it["cyl"])) > 0.01:
                                _pwr += f" CYL {float(_it['cyl']):+.2f}"
                            if _it.get("axis"):
                                _pwr += f" AX {int(_it['axis'])}"
                        except Exception as _e:
                            pass
                    st.caption(
                        f"#{_it.get('item_no')} · {_it.get('product_name','')} "
                        f"{str(_it.get('eye_side','')).upper()} {_pwr} · "
                        f"Ordered: {_it.get('ordered_qty',0)} · "
                        f"Received: {_it.get('received_qty',0)} · "
                        f"₹{float(_it.get('unit_price',0)):,.0f} · "
                            f"{_it.get('item_status','PENDING')}"
                    )

                if po.get("supplier_id"):
                    _sup_contact = _q(
                        "SELECT COALESCE(mobile,'') AS mobile, COALESCE(email,'') AS email "
                        "FROM parties WHERE id=%s::uuid LIMIT 1",
                        (po.get("supplier_id"),),
                    ) or []
                else:
                    _sup_contact = _q(
                        "SELECT COALESCE(mobile,'') AS mobile, COALESCE(email,'') AS email "
                        "FROM parties WHERE party_name=%s LIMIT 1",
                        (_sup,),
                    ) or []
                _po_mob = _sup_contact[0].get("mobile", "") if _sup_contact else ""
                _po_email = _sup_contact[0].get("email", "") if _sup_contact else ""
                _msg_lines = [
                    f"*📦 PO No: {_po_no}*",
                    f"Date: {_odate or '—'}",
                    f"To: {_sup}",
                    "",
                ]
                if po.get("customer_order_id"):
                    _msg_lines.append(f"Customer Order: {po.get('customer_order_id')}")
                    _msg_lines.append("")
                for _it in items:
                    _pwr = ""
                    try:
                        if _it.get("sph") is not None:
                            _pwr = f"SPH {float(_it['sph']):+.2f}"
                        if _it.get("cyl") is not None and abs(float(_it.get("cyl") or 0)) > 0.01:
                            _pwr += f" CYL {float(_it['cyl']):+.2f}"
                        if _it.get("axis"):
                            _pwr += f" AX {int(float(_it['axis']))}"
                        if _it.get("add_power"):
                            _pwr += f" ADD {float(_it['add_power']):+.2f}"
                    except Exception:
                        _pwr = ""
                    _msg_lines.append(
                        f"{str(_it.get('eye_side') or '').upper() or 'ITEM'} — "
                        f"{_it.get('product_name','')}  {_pwr}  Qty: {_it.get('ordered_qty') or 0}"
                    )
                _msg_lines += ["", "Please confirm receipt and dispatch status.", "Thank you."]
                _po_msg = "\n".join(_msg_lines)
                _po_plain = _po_msg.replace("*", "")
                _po_mob_clean = "".join(c for c in _po_mob if c.isdigit())
                if _po_mob_clean and not _po_mob_clean.startswith("91"):
                    _po_mob_clean = "91" + _po_mob_clean
                _po_wa_url = (
                    f"https://wa.me/{_po_mob_clean}?text={_upo.quote(_po_msg, safe='')}"
                    if _po_mob_clean else
                    f"https://wa.me/?text={_upo.quote(_po_msg, safe='')}"
                )
                _po_mailto = (
                    f"mailto:{_po_email}?subject={_upo.quote('PO ' + str(_po_no))}&body={_upo.quote(_po_plain)}"
                    if _po_email else
                    f"mailto:?subject={_upo.quote('PO ' + str(_po_no))}&body={_upo.quote(_po_plain)}"
                )
                _rs1, _rs2 = st.columns(2)
                _rs1.link_button("📲 Resend WhatsApp", _po_wa_url, use_container_width=True)
                _rs2.link_button("📧 Resend Mail", _po_mailto, use_container_width=True)

        st.markdown("<div style='height:1px;background:#1e293b;margin:2px 0'></div>",
                    unsafe_allow_html=True)


def _render_purchase_acknowledgements_tab():
    """Purchase acknowledgements — grouped by supplier with challan/invoice info."""
    st.markdown("### 🧾 Purchase Acknowledgements")
    st.caption("All purchase records from supplier, external lab, and stock routes")

    def _q(sql, params=None):
        try:
            from modules.sql_adapter import run_query
            return run_query(sql, params or {}) or []
        except Exception as e:
            st.error(f"Query: {e}"); return []

    with st.container(border=True):
        _af1, _af2 = st.columns([3, 2])
        _pa_sup_flt = _af1.text_input("Supplier / Order", placeholder="🔍 Filter",
                                       key="pa_tab_flt", label_visibility="collapsed")
        _pa_status  = _af2.selectbox("Status", ["All","NOT_READY","READY","LOCKED"],
                                      key="pa_tab_st", label_visibility="collapsed")

    _where = ["1=1"]
    _params: dict = {}
    if _pa_sup_flt.strip():
        _where.append(
            "(LOWER(COALESCE(pa.supplier_name,'')) LIKE %(flt)s "
            " OR regexp_replace(LOWER(COALESCE(pa.order_no,'')), '[^a-z0-9]', '', 'g') LIKE %(flt_norm)s "
            " OR LOWER(COALESCE(pa.order_no,'')) LIKE %(flt)s)"
        )
        _params["flt"] = f"%{_pa_sup_flt.strip().lower()}%"
        _params["flt_norm"] = f"%{_scan_norm(_pa_sup_flt)}%"
    if _pa_status != "All":
        if _pa_status == "LOCKED":
            _where.append("pa.is_price_locked = TRUE")
        else:
            _where.append("pa.billing_status = %(st)s")
            _params["st"] = _pa_status

    pas = _q(f"""
        SELECT pa.id::text, pa.order_no, pa.supplier_name,
               pa.purchase_price, pa.total_value, pa.billing_status,
               pa.challan_no, pa.invoice_no, pa.document_date::text,
               pa.is_price_locked, pa.acknowledged_at::text,
               COALESCE(p.product_name,'Unknown') AS product_name,
               ol.eye_side, ol.sph, ol.cyl, ol.axis, ol.add_power,
               ol.quantity
        FROM purchase_acknowledgements pa
        LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
        LEFT JOIN products p    ON p.id  = ol.product_id
        WHERE {' AND '.join(_where)}
        ORDER BY pa.acknowledged_at DESC
        LIMIT 200
    """, _params)

    if not pas:
        st.info("No purchase acknowledgement records found.")
        return

    # Group by supplier
    from collections import defaultdict as _dpa
    _by_sup = _dpa(list)
    for r in pas:
        _by_sup[r.get("supplier_name","Unknown")].append(r)

    st.caption(f"{len(pas)} record(s) · {len(_by_sup)} supplier(s)")

    for _sup, _items in _by_sup.items():
        _sup_total = sum(float(i.get("total_value") or 0) for i in _items)
        with st.expander(
            f"🏭 {_sup} — {len(_items)} line(s) — ₹{_sup_total:,.0f}",
            expanded=False
        ):
            for _it in _items:
                _eye  = str(_it.get("eye_side","")).upper()
                _pn   = _it.get("product_name","—")
                _prc  = float(_it.get("purchase_price") or 0)
                _chal = _it.get("challan_no","")
                _inv  = _it.get("invoice_no","")
                _locked = bool(_it.get("is_price_locked"))
                _pwr  = ""
                try:
                    if _it.get("sph") is not None:
                        _pwr = f"SPH {float(_it['sph']):+.2f}"
                        if _it.get("cyl") and abs(float(_it["cyl"])) > 0.01:
                            _pwr += f" CYL {float(_it['cyl']):+.2f}"
                        if _it.get("axis"):
                            _pwr += f" AX {int(_it['axis'])}"
                except Exception as _e:
                    pass

                _badge = ("🔒" if _locked else
                          "🧾" if _inv else
                          "📋" if _chal else "⚠️")
                st.markdown(
                    f"<div style='padding:3px 8px;border-left:2px solid #1e293b;margin:2px 0;"
                    f"font-size:0.78rem;color:#94a3b8'>"
                    f"{_badge} <b style='color:#e2e8f0'>{_it.get('order_no','')}</b> · "
                    f"{_eye} · {_pn} {_pwr} · "
                    f"&#8377;{_prc:,.2f}/pc"
                    + (f" · Challan: <b>{_chal}</b>" if _chal else "")
                    + (f" · Invoice: <b>{_inv}</b>" if _inv else "")
                    + f" · {str(_it.get('document_date',''))[:10]}"
                    + "</div>",
                    unsafe_allow_html=True
                )



# ── Public entry points ──────────────────────────────────────────────────

def render_supplier_pipeline(*args, **kwargs):
    return _render_supplier_pipeline(*args, **kwargs)
