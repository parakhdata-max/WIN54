"""
order_edit_view.py
─────────────────────────────────────────────────────────────────────────────
Standalone page accessible from app.py sidebar as "📋 Orders".

Shows all recent orders with quick filters:
  • Editable (PENDING) — can edit RX, add/remove lines, change lens params
  • Confirmed         — read-only view, can still add missing lines with warning

Used by:
  • Internal staff to fix punching errors before backoffice confirmation
  • (Future) Client-facing self-service portal

Rule: Once backoffice clicks "SAVE TO ORDER" → status = CONFIRMED → locked.
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import uuid
import streamlit as st
from typing import Dict, List, Optional
import datetime
from modules.core.price_qty_governor import (
    normalize_to_pcs_price,
    is_box_product,
    reverse_qty,
    check_sync,
    PAIR_TO_PCS,
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _rq(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        return []


def _write(sql, params):
    """Use run_write for DML (INSERT/UPDATE/DELETE) — run_query is SELECT-only."""
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params)
    except Exception as e:
        st.error(f"DB error: {e}")
        return False


def _resolve_order_id(order_ref: str) -> str:
    try:
        from modules.sql_adapter import resolve_order_uuid
        return resolve_order_uuid(order_ref) or ""
    except Exception:
        return ""


def _refresh_order_total(order_id: str) -> None:
    """Keep order header value in sync after inline line edits/deletes."""
    order_id = _resolve_order_id(order_id)
    if not order_id:
        return
    _write("""
        UPDATE orders
           SET total_value = CASE
                   WHEN UPPER(COALESCE(order_type,'')) = 'RETAIL' THEN
                       ROUND(COALESCE((
                           SELECT SUM(COALESCE(NULLIF(billing_total,0), total_price, 0))
                             FROM order_lines
                            WHERE order_id = %(oid)s::uuid
                              AND COALESCE(is_deleted, FALSE) = FALSE
                       ), 0), 0)
                   ELSE
                       COALESCE((
                           SELECT SUM(COALESCE(NULLIF(billing_total,0), total_price, 0))
                             FROM order_lines
                            WHERE order_id = %(oid)s::uuid
                              AND COALESCE(is_deleted, FALSE) = FALSE
                       ), 0)
               END,
               updated_at = NOW()
         WHERE id = %(oid)s::uuid
    """, {"oid": order_id})


def _sync_pricing_after_edit(order_id: str) -> None:
    """Run final pricing stack after Orders-page edits before refreshing totals."""
    order_id = _resolve_order_id(order_id)
    if not order_id:
        return
    try:
        rows = _rq(
            "SELECT id::text AS id, order_no, order_type, party_id::text AS party_id, "
            "party_name, patient_name FROM orders WHERE id=%(oid)s::uuid LIMIT 1",
            {"oid": order_id},
        ) or []
        if not rows:
            _refresh_order_total(order_id)
            return
        order = dict(rows[0])
        order["lines"] = _load_lines(order_id)
        from modules.backoffice.backoffice_helpers import refresh_order_pricing_rules
        refresh_order_pricing_rules(order, persist=True)
    except Exception:
        _refresh_order_total(order_id)


def _safe_json_text(value) -> str:
    """JSONB text safe for DB writes; removes NaN/Infinity from nested params."""
    try:
        from modules.core.json_sanitizer import sanitize_json
        return json.dumps(sanitize_json(value or {}))
    except Exception:
        return json.dumps(value or {})


def _fmt_date(dt) -> str:
    if not dt:
        return "—"
    try:
        return str(dt)[:10]
    except Exception:
        return str(dt)


try:
    from modules.backoffice.order_status_live import STATUS_META as _OSL_META
    _STATUS_COLOR = {k: v["color"] for k, v in _OSL_META.items()}
except Exception:
    _STATUS_COLOR = {
        "PENDING": "#3b82f6", "UNDER_REVIEW": "#f59e0b", "CONFIRMED": "#6366f1",
        "IN_PRODUCTION": "#8b5cf6", "READY": "#10b981",
        "BILLED": "#059669", "DISPATCHED": "#0891b2",
        "DELIVERED": "#10b981", "CLOSED": "#334155",
        "CANCELLED": "#ef4444",
    }


def _is_editable(status: str, order_type: str = "", is_converted: bool = False,
                 linked_retail_no: str = "") -> bool:
    # FIX: CONSULTATION orders are always saved as CLOSED — that is normal.
    # They are editable unless genuinely converted (is_converted + linked_retail_no).
    if str(order_type).upper() == "CONSULTATION":
        return not (is_converted and bool(linked_retail_no))
    try:
        from modules.settings.shop_master import get_order_action_statuses
        return str(status).upper() in get_order_action_statuses("edit")
    except Exception:
        pass
    # UNDER_REVIEW and PENDING are editable; HOLD/CREDIT_HOLD require release first.
    return str(status).upper() not in (
        "HOLD", "CREDIT_HOLD", "PENDING_PAYMENT",
        "CONFIRMED", "IN_PRODUCTION", "READY", "READY_TO_BILL", "READY_FOR_BILLING",
        "BILLED", "CHALLANED", "INVOICED", "DISPATCHED", "DELIVERED", "CLOSED", "CANCELLED"
    )


def _render_pipeline_lock(order: dict):
    """
    Smart pipeline lock banner for the Orders screen.
    Shown for CONFIRMED+ orders — explains exactly what to do first
    based on actual pipeline depth (job card stage, blank allotment, PO status).
    Replaces the generic 'order is locked' message with actionable guidance.
    """
    import streamlit as st
    try:
        from modules.core.pipeline_guard import (
            get_order_edit_permission,
        )
        from modules.backoffice.pipeline_guard_ui import render_edit_lock_banner
        perm = get_order_edit_permission(order)
        render_edit_lock_banner(perm, context="orders")
    except Exception:
        # Fallback generic message if guard module unavailable
        st.markdown(
            "<div style='background:#1a0a0a;border:1px solid #ef444433;border-radius:8px;"
            "padding:12px 16px;color:#94a3b8;font-size:0.82rem'>"
            "🔒 <b>Order is confirmed</b> — go to Backoffice to make changes. "
            "If a job card has a blank allotted, cancel the job card first.</div>",
            unsafe_allow_html=True,
        )


# ── Load orders ───────────────────────────────────────────────────────────────

def _digits_only(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _load_orders(search: str, status_filter: str,
                 from_date: datetime.date, to_date: datetime.date,
                 order_type: str, patient_filter: str = "",
                 mobile_filter: str = "", doc_filter: str = "") -> List[Dict]:
    # ── Ensure optional columns exist before querying ─────────────────────
    # is_converted and linked_retail_no are added lazily during consultation
    # conversion. If they don't exist yet the SELECT below will throw a
    # "column does not exist" error and return zero rows for ALL orders.
    # Running ADD COLUMN IF NOT EXISTS here is safe and near-instant.
    try:
        from modules.sql_adapter import run_write as _rw_oe
        _rw_oe("ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_converted BOOLEAN DEFAULT FALSE")
        _rw_oe("ALTER TABLE orders ADD COLUMN IF NOT EXISTS linked_retail_no TEXT")
    except Exception:
        pass

    where = ["o.created_at::date BETWEEN %(fd)s AND %(td)s", "COALESCE(o.is_deleted,FALSE)=FALSE"]
    params: Dict = {"fd": from_date, "td": to_date}

    if status_filter != "All":
        where.append("o.status = %(st)s")
        params["st"] = status_filter

    if order_type != "All":
        where.append("UPPER(o.order_type) = %(ot)s")
        params["ot"] = order_type.upper()

    if search:
        # Detect if search looks like a phone number (mostly digits)
        _is_phone = search.replace(" ","").replace("-","").isdigit() and len(search) >= 6
        if _is_phone:
            where.append("""(
                o.order_no      ILIKE %(s)s
                OR o.patient_name ILIKE %(s)s
                OR o.party_name   ILIKE %(s)s
                OR o.patient_mobile ILIKE %(s)s
                OR EXISTS (
                    SELECT 1 FROM patients p
                    WHERE p.id = o.party_id::uuid
                      AND p.mobile ILIKE %(s)s
                )
            )""")
        else:
            where.append("""(
                o.order_no      ILIKE %(s)s
                OR o.patient_name ILIKE %(s)s
                OR o.party_name   ILIKE %(s)s
                OR o.patient_mobile ILIKE %(s)s
            )""")
        params["s"] = f"%{search}%"

    if doc_filter:
        where.append("""(
            o.order_no ILIKE %(doc)s
            OR COALESCE(o.display_order_no::text,'') ILIKE %(doc)s
        )""")
        params["doc"] = f"%{doc_filter}%"

    if patient_filter:
        where.append("""(
            o.patient_name ILIKE %(patient)s
            OR o.party_name ILIKE %(patient)s
        )""")
        params["patient"] = f"%{patient_filter}%"

    _mob_digits = _digits_only(mobile_filter)
    if _mob_digits:
        where.append("""(
            regexp_replace(COALESCE(o.patient_mobile,''), '\\D', '', 'g') ILIKE %(mobile)s
            OR EXISTS (
                SELECT 1 FROM patients p
                WHERE p.id = o.party_id::uuid
                  AND regexp_replace(COALESCE(p.mobile,''), '\\D', '', 'g') ILIKE %(mobile)s
            )
        )""")
        params["mobile"] = f"%{_mob_digits}%"

    rows = _rq(f"""
        SELECT
            o.id, o.order_no, o.display_order_no,
            o.patient_name, o.party_name,
            o.patient_mobile, o.party_id::text AS party_id,
            o.customer_order_no,
            o.order_type, o.status, o.created_at,
            (COALESCE(o.is_converted, false) OR linked_bill.linked_order_no IS NOT NULL) AS is_converted,
            COALESCE(NULLIF(o.linked_retail_no,''), linked_bill.linked_order_no, '') AS linked_retail_no,
            COUNT(ol.id) AS line_count,
            COALESCE(
                NULLIF(SUM(
                    CASE
                        WHEN COALESCE(p2.unit,'PCS')='BOX'
                             AND COALESCE(p2.box_size,1)>1
                             AND ol.unit_price > 0
                             AND COALESCE(inv2.selling_price, 0) > 0
                             AND ABS(ol.unit_price - COALESCE(inv2.selling_price, 0)) < 0.5
                        THEN (ol.unit_price / COALESCE(p2.box_size,1)) * COALESCE(ol.quantity,0)
                        ELSE COALESCE(ol.billing_total, ol.total_price, 0)
                    END
                ), 0),
                o.total_value,
                0
            ) AS total_value
        FROM orders o
        LEFT JOIN order_lines ol
            ON ol.order_id = o.id
            AND COALESCE(ol.is_deleted, FALSE) = FALSE
        LEFT JOIN products p2
            ON p2.id = ol.product_id
        LEFT JOIN LATERAL (
            SELECT COALESCE(selling_price, 0) AS selling_price
            FROM inventory_stock is3
            WHERE is3.product_id = ol.product_id
              AND COALESCE(is3.sph, 0)  = COALESCE(ol.sph, 0)
              AND COALESCE(is3.cyl, 0)  = COALESCE(ol.cyl, 0)
              AND COALESCE(is3.axis, 0) = COALESCE(ol.axis, 0)
              AND is3.selling_price > 0
            ORDER BY is3.updated_at DESC NULLS LAST
            LIMIT 1
        ) inv2 ON true
        LEFT JOIN LATERAL (
            SELECT o2.order_no AS linked_order_no
            FROM orders o2
            WHERE o2.customer_order_no = o.id::text
              AND UPPER(COALESCE(o2.order_type,'')) IN ('RETAIL','WHOLESALE')
              AND COALESCE(o2.is_deleted, FALSE) = FALSE
            ORDER BY o2.created_at DESC NULLS LAST
            LIMIT 1
        ) linked_bill ON TRUE
        WHERE {' AND '.join(where)}
        GROUP BY o.id, o.order_no, o.display_order_no,
                 o.patient_name, o.party_name, o.patient_mobile,
                 o.party_id, o.customer_order_no,
                 o.order_type, o.status, o.created_at,
                 o.is_converted, o.linked_retail_no, linked_bill.linked_order_no
        ORDER BY o.created_at DESC
        LIMIT 100
    """, params)
    return rows or []


# ── Load lines for one order ──────────────────────────────────────────────────

def _load_lines(order_id: str) -> List[Dict]:
    order_id = _resolve_order_id(order_id)
    if not order_id:
        return []
    rows = _rq("""
        SELECT
            ol.id AS line_id, ol.product_id,
            ol.eye_side, ol.sph, ol.cyl, ol.axis, ol.add_power,
            ol.quantity, ol.unit_price, ol.total_price,
            COALESCE(ol.billing_total, ol.total_price, 0) AS billing_total,
            COALESCE(ol.discount_percent, 0) AS discount_percent,
            COALESCE(ol.discount_amount, 0)  AS discount_amount,
            COALESCE(ol.applied_rule_ids, '') AS applied_rule_ids,
            ol.lens_params, ol.boxing_params, ol.status,
            COALESCE(o.order_type, 'RETAIL') AS order_type,
            p.product_name, p.brand, p.main_group,
            COALESCE(p.unit, 'PCS')         AS unit,
            COALESCE(p.box_size, 1)         AS box_size,
            COALESCE(p.allow_loose, false)  AS allow_loose,
            COALESCE(inv.selling_price, 0)  AS selling_price,
            COALESCE(ol.gst_percent, p.gst_percent, 0) AS gst_percent,
            ol.lens_params::jsonb->>'manufacturing_route' AS manufacturing_route,
            COALESCE(ol.allocated_qty, 0)               AS allocated_qty,
            COALESCE(ol.batch_status,
                ol.lens_params::jsonb->>'batch_status') AS batch_status,
            ol.suggested_allocation                     AS suggested_allocation,
            COALESCE(ol.billed_qty, 0)                  AS billed_qty,
            COALESCE(ol.dispatched_qty, 0)              AS dispatched_qty,
            COALESCE(ol.lens_params::jsonb->>'supplier_id', '')  AS supplier_id,
            COALESCE(ol.is_service_line, FALSE)         AS is_service_line
        FROM order_lines ol
        LEFT JOIN orders o ON o.id = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        LEFT JOIN LATERAL (
            SELECT COALESCE(selling_price, 0) AS selling_price
            FROM inventory_stock is2
            WHERE is2.product_id = ol.product_id
              AND COALESCE(is2.sph, 0)  = COALESCE(ol.sph, 0)
              AND COALESCE(is2.cyl, 0)  = COALESCE(ol.cyl, 0)
              AND COALESCE(is2.axis, 0) = COALESCE(ol.axis, 0)
              AND is2.selling_price > 0
            ORDER BY is2.updated_at DESC NULLS LAST
            LIMIT 1
        ) inv ON true
        WHERE ol.order_id = %(oid)s::uuid
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        ORDER BY ol.eye_side, ol.id
    """, {"oid": order_id})
    return rows or []


def _parse_lp(val) -> Dict:
    import math
    if val is None: return {}
    if isinstance(val, float):
        try:
            return {} if math.isnan(val) else {}
        except Exception: return {}
    if isinstance(val, dict): return val
    if isinstance(val, str) and val.strip():
        try: return json.loads(val) or {}
        except Exception: return {}
    return {}


# ── Render main view ──────────────────────────────────────────────────────────



def _fmt_qty_disp(pcs: int, product_row: dict) -> str:
    """Format PCS quantity using product unit/box_size — same logic as retail_punching."""
    try:
        unit     = str(product_row.get("unit","PCS") or "PCS").upper()
        box_size = int(product_row.get("box_size",1) or 1)
        if unit == "BOX" and box_size > 1:
            boxes = pcs // box_size
            loose = pcs % box_size
            if loose == 0:
                return f"{boxes} BOX"
            return f"{boxes} BOX + {loose} PCS"
        elif unit == "PAIR":
            pairs = pcs / 2.0
            return f"{pcs} PCS ({pairs:.1f} pair)"
    except Exception:
        pass
    return f"{pcs} PCS"


def _preload_order_for_edit(order: dict, lines: list) -> dict:
    """
    Load an existing order + lines into session state for editing in
    Retail Punching (RETAIL) or Wholesale Punching (WHOLESALE).

    Returns dict with success/error and routing info.
    """
    import uuid, datetime as _dt
    from modules.sql_adapter import run_query as _rq_edit

    _otype = str(order.get("order_type","RETAIL")).upper()
    _oid   = str(order.get("id",""))
    _ono   = str(order.get("order_no",""))

    # Resolve patient_id from name
    _pname = str(order.get("patient_name","") or order.get("party_name",""))
    _pmob  = str(order.get("patient_mobile","") or "")
    try:
        _pr = _rq_edit(
            "SELECT id::text AS pid FROM patients WHERE master_name ILIKE %s LIMIT 1",
            (_pname,)
        ) or []
        _pid = _pr[0]["pid"] if _pr else None
    except Exception:
        _pid = None

    # Load RX: use visit_id linked to THIS order first (exact match)
    # Fallback to latest visit only if no visit_id stored
    # visit_id is stored as customer_order_no (set at consultation save time)
    _visit_id_on_order = str(order.get("customer_order_no", "") or order.get("visit_id", "") or "")
    try:
        _rxd = {}

        # PRIMARY: visit_id stored on the order at consultation save time
        if _visit_id_on_order and len(_visit_id_on_order) > 10:
            _rx = _rq_edit("""
                SELECT COALESCE(right_sph,0) AS sph_r, COALESCE(right_cyl,0) AS cyl_r,
                       COALESCE(right_axis,0) AS ax_r,  COALESCE(right_add,0) AS add_r,
                       COALESCE(left_sph,0)  AS sph_l,  COALESCE(left_cyl,0)  AS cyl_l,
                       COALESCE(left_axis,0) AS ax_l,   COALESCE(left_add,0)  AS add_l
                FROM patient_visits
                WHERE id = %s::uuid LIMIT 1
            """, (_visit_id_on_order,)) or []
            _rxd = _rx[0] if _rx else {}

        # SECONDARY: match by patient UUID + order creation date
        if not _rxd and _pid and len(_pid) > 10:
            _order_date = str(order.get("created_at", ""))[:10] or "today"
            _rx = _rq_edit("""
                SELECT COALESCE(right_sph,0) AS sph_r, COALESCE(right_cyl,0) AS cyl_r,
                       COALESCE(right_axis,0) AS ax_r,  COALESCE(right_add,0) AS add_r,
                       COALESCE(left_sph,0)  AS sph_l,  COALESCE(left_cyl,0)  AS cyl_l,
                       COALESCE(left_axis,0) AS ax_l,   COALESCE(left_add,0)  AS add_l
                FROM patient_visits
                WHERE patient_id = %s::uuid
                  AND visit_date = %s::date
                ORDER BY created_at DESC LIMIT 1
            """, (_pid, _order_date)) or []
            _rxd = _rx[0] if _rx else {}

        # LAST RESORT: latest visit by name (old orders without visit_id)
        if not _rxd:
            _rx = _rq_edit("""
                SELECT COALESCE(right_sph,0) AS sph_r, COALESCE(right_cyl,0) AS cyl_r,
                       COALESCE(right_axis,0) AS ax_r,  COALESCE(right_add,0) AS add_r,
                       COALESCE(left_sph,0)  AS sph_l,  COALESCE(left_cyl,0)  AS cyl_l,
                       COALESCE(left_axis,0) AS ax_l,   COALESCE(left_add,0)  AS add_l
                FROM patient_visits pv
                JOIN patients p ON p.id = pv.patient_id
                WHERE p.master_name ILIKE %s
                ORDER BY pv.visit_date DESC LIMIT 1
            """, (_pname,)) or []
            _rxd = _rx[0] if _rx else {}

    except Exception:
        _rxd = {}

    # Fallback: derive RX from order lines (wholesale party ≠ patient, no patient_visits row)
    if not _rxd:
        _ln_r = next((l for l in lines if str(l.get("eye_side","")).upper().startswith("R")), None)
        _ln_l = next((l for l in lines if str(l.get("eye_side","")).upper().startswith("L")), None)
        def _fv_rx(v, default=0):
            try: return float(v) if v is not None else default
            except: return default
        if _ln_r or _ln_l:
            _rxd = {
                "sph_r": _fv_rx(_ln_r.get("sph") if _ln_r else None),
                "cyl_r": _fv_rx(_ln_r.get("cyl") if _ln_r else None),
                "ax_r":  _fv_rx(_ln_r.get("axis") if _ln_r else None),
                "add_r": _fv_rx(_ln_r.get("add_power") if _ln_r else None),
                "sph_l": _fv_rx(_ln_l.get("sph") if _ln_l else None),
                "cyl_l": _fv_rx(_ln_l.get("cyl") if _ln_l else None),
                "ax_l":  _fv_rx(_ln_l.get("axis") if _ln_l else None),
                "add_l": _fv_rx(_ln_l.get("add_power") if _ln_l else None),
            }

    # Convert order_lines → retail_order_lines format
    _cart = []
    for ln in lines:
        # DB stores eye_side as char(1): R/L/B/O/S — expand back to full names
        _eye_db  = str(ln.get("eye_side","OTHER") or "OTHER").upper().strip()
        _eye_expand = {"R":"R","L":"L","B":"B","O":"OTHER","S":"SERVICE","OTHER":"OTHER","SERVICE":"SERVICE"}
        _eye     = _eye_expand.get(_eye_db, _eye_db if _eye_db in ("R","L","B","OTHER","SERVICE") else "OTHER")
        _sph    = ln.get("sph")
        _cyl    = ln.get("cyl")
        _axis   = ln.get("axis")
        _add    = ln.get("add_power")
        _qty    = int(ln.get("quantity",1) or 1)
        _box_sz = int(ln.get("box_size",1) or 1)
        _unit   = str(ln.get("unit","PCS") or "PCS").upper()
        _uprice_raw = float(ln.get("unit_price", 0) or 0)
        _total_raw  = float(ln.get("billing_total") or ln.get("total_price", 0) or 0)
        # ── Price normalization guard ────────────────────────────────────────
        # order_lines.unit_price is stored as PCS price (normalized at save time
        # by the finalize engine). normalize_to_pcs_price() must NOT be called
        # blindly here — it would divide 90.06 by 6 → 15.01 (the ÷6 bug).
        #
        # Safe rule: only divide if unit_price × qty is materially larger than
        # total_price, which proves the stored price is a full-BOX price.
        # Otherwise treat unit_price as already-PCS and use as-is.
        _is_stored_as_box = (
            _uprice_raw > 0 and _total_raw > 0 and _qty > 0
            and (_uprice_raw * _qty) > (_total_raw * 1.15)   # >15% over means BOX price
        )
        if _is_stored_as_box:
            _uprice = normalize_to_pcs_price(_uprice_raw, ln)  # only divide when truly a BOX price
        else:
            _uprice = round(_uprice_raw, 4)   # already PCS — use directly
        # Fallback: if stored unit_price is 0, derive from total_price / qty
        if _uprice == 0 and _qty > 0 and _total_raw > 0:
            _uprice = round(_total_raw / _qty, 4)
        _total  = round(_uprice * _qty, 2) if _uprice > 0 else _total_raw
        _lp     = _parse_lp(ln.get("lens_params"))
        _bp     = _parse_lp(ln.get("boxing_params"))
        _suggested = ln.get("suggested_allocation") or []
        if isinstance(_suggested, str) and _suggested.strip():
            try:
                _suggested = json.loads(_suggested) or []
            except Exception:
                _suggested = []
        _cart.append({
            "line_id":            str(ln.get("line_id","")) or str(uuid.uuid4()),
            "provisional_order_id": f"EDIT-{_oid[:8]}",
            "_edit_order_id":     _oid,
            "_edit_order_no":     _ono,
            "_edit_line_id":      str(ln.get("line_id","")),
            "product_id":         str(ln.get("product_id","") or ""),
            "product_name":       str(ln.get("product_name","") or ""),
            "brand":              str(ln.get("brand","") or ""),
            "main_group":         str(ln.get("main_group","") or ""),
            "batch_no":           "",
            "eye_side":           _eye,
            "sph":                float(_sph) if _sph is not None else None,
            "cyl":                float(_cyl) if _cyl is not None else None,
            "axis":               int(_axis) if _axis is not None else None,
            "add_power":          float(_add) if _add is not None else None,
            "lens_params":        _lp,
            "boxing_params":      _bp,
            "requested_qty":      _qty,
            "billing_qty":        _qty,
            "order_qty":          0,          # 0 = fully from stock/already ordered
            "display_qty":        _fmt_qty_disp(_qty, ln),
            "unit":               str(ln.get("unit","PCS") or "PCS"),
            "box_size":           int(ln.get("box_size",1) or 1),
            "allow_loose":        bool(ln.get("allow_loose",False)),
            "batch_allocation":   list(_suggested) if isinstance(_suggested, list) else [],
            "suggested_allocation": list(_suggested) if isinstance(_suggested, list) else [],
            "unit_price":         _uprice,
            "total_price":        round(_uprice * _qty, 2) if _uprice > 0 else _total,
            "billing_total":      round(float(ln.get("billing_total") or _total), 2),
            "gst_percent":        float(ln.get("gst_percent",0) or 0),
            "gst_amount":         float(ln.get("gst_amount",0) or 0),
            "discount_percent":   float(ln.get("discount_percent",0) or 0),
            "discount_amount":    float(ln.get("discount_amount",0) or 0),
            "applied_rule_ids":   str(ln.get("applied_rule_ids") or ""),
            "allocated_qty":      int(ln.get("allocated_qty") or 0),
            "status":             str(ln.get("status","") or ""),
            "created_at":         _dt.datetime.now().isoformat(),
        })

    # Load existing advance payments for this order
    try:
        _adv_rows = _rq_edit("""
            SELECT COALESCE(SUM(amount),0) AS total_adv,
                   MAX(payment_mode) AS last_mode
            FROM payments
            WHERE advance_for_order_id = %s::uuid
              AND payment_type = 'ADVANCE'
              AND COALESCE(is_deleted,false) = false
        """, (_oid,)) or []
        _existing_adv = float((_adv_rows[0]["total_adv"] if _adv_rows else 0) or 0)
        _adv_mode     = str((_adv_rows[0]["last_mode"] if _adv_rows else "") or "CASH")
    except Exception:
        _existing_adv = 0.0
        _adv_mode     = "CASH"

    # Extract powers from cart lines for wholesale eye-power prefill.
    # The wholesale punching power section reads retail_new_rx_r/l from session state.
    # For WHOLESALE edit we set retail_new_rx = {} (to avoid QE trigger) in app.py,
    # but we still need the per-eye rx stored in the prefill dict so app.py can
    # populate wh_sph_R/wh_sph_L etc. via the existing wh_ key injection below.
    # We store them as rx_r / rx_l directly in the prefill dict.
    _rx_r_from_lines = {}
    _rx_l_from_lines = {}
    for _cl in _cart:
        _es = str(_cl.get("eye_side","")).upper()
        if _es == "R" and not _rx_r_from_lines:
            _rx_r_from_lines = {
                "sph":  _cl.get("sph"),
                "cyl":  _cl.get("cyl"),
                "axis": _cl.get("axis"),
                "add":  _cl.get("add_power"),
            }
        elif _es == "L" and not _rx_l_from_lines:
            _rx_l_from_lines = {
                "sph":  _cl.get("sph"),
                "cyl":  _cl.get("cyl"),
                "axis": _cl.get("axis"),
                "add":  _cl.get("add_power"),
            }

    # Existing order lines are the source of truth while editing an order.
    # Patient-visit RX can be stale or blank, especially for orders created
    # directly from punching. Prefer saved order-line powers so edit screens
    # reopen with the same R/L powers that were billed/produced.
    if _rx_r_from_lines or _rx_l_from_lines:
        def _rxv(_rx: dict, _key: str):
            _v = _rx.get(_key) if _rx else None
            return _v if _v is not None else 0

        _rxd = {
            "sph_r": _rxv(_rx_r_from_lines, "sph"),
            "cyl_r": _rxv(_rx_r_from_lines, "cyl"),
            "ax_r":  _rxv(_rx_r_from_lines, "axis"),
            "add_r": _rxv(_rx_r_from_lines, "add"),
            "sph_l": _rxv(_rx_l_from_lines, "sph"),
            "cyl_l": _rxv(_rx_l_from_lines, "cyl"),
            "ax_l":  _rxv(_rx_l_from_lines, "axis"),
            "add_l": _rxv(_rx_l_from_lines, "add"),
        }

    return {
        "success":        True,
        "order_type":     _otype,
        "order_id":       _oid,
        "order_no":       _ono,
        "patient_name":   _pname,
        "patient_mobile": _pmob,
        "patient_id":     _pid,
        "rx":             _rxd,
        "rx_r":           _rx_r_from_lines,
        "rx_l":           _rx_l_from_lines,
        "cart":           _cart,
        "payment_mode":   str(order.get("payment_mode","") or ""),
        "existing_advance": _existing_adv,
        "advance_mode":   _adv_mode,
        "sidebar_page":   "🛍️  Retail Order" if _otype == "RETAIL" else "📦  Wholesale Order",
    }

def render_order_edit_view():
    st.markdown("""
    <style>
    .block-container { padding-top: 0.2rem !important; padding-bottom: 0.8rem !important; }
    h1,h2,h3,h4,h5 { margin-top: 0rem !important; margin-bottom: 0.2rem !important; }
    .element-container { margin-bottom: 4px !important; }
    </style>
    """, unsafe_allow_html=True)
    st.markdown(
        "<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px'>"
        "<span style='background:#0f172a;color:#94a3b8;font-size:0.7rem;font-weight:800;"
        "padding:3px 10px;border-radius:20px;letter-spacing:.06em;border:1px solid #334155'>"
        "📋 Orders</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Two tabs: Rx Orders vs Consultations ─────────────────────────────
    # If a consultation was just opened for edit, default to Consultations tab
    _default_tab = 1 if st.session_state.get("_oev_land_on_consult") else 0
    # Clear the flag after reading
    if st.session_state.get("_oev_land_on_consult"):
        st.session_state.pop("_oev_land_on_consult", None)

    _tab_rx, _tab_cons = st.tabs(["👓 Rx Orders", "🩺 Consultations"])

    for _active_tab, _tab_otype in [(_tab_rx, "RX"), (_tab_cons, "CONSULTATION")]:
        with _active_tab:
            _render_orders_tab(_tab_otype)


def _render_orders_tab(tab_otype: str):
    """Render the orders list for a given tab type (RX or CONSULTATION)."""

    # ── Filters ───────────────────────────────────────────────────────────
    if tab_otype == "RX":
        fc1, fc2, fc3, fc4 = st.columns([1.4, 1.6, 1.3, 1.2])
        with fc1:
            doc_search = st.text_input("Order No", placeholder="Order / bill no",
                                       label_visibility="collapsed", key="oev_doc_rx")
        with fc2:
            patient_search = st.text_input("Patient / Party", placeholder="Patient / party",
                                           label_visibility="collapsed", key="oev_patient_rx")
        with fc3:
            mobile_search = st.text_input("Mobile", placeholder="Mobile",
                                          label_visibility="collapsed", key="oev_mobile_rx")
        with fc4:
            status_f = st.selectbox(
                "Status", ["All", "PENDING", "UNDER_REVIEW", "CONFIRMED", "IN_PRODUCTION",
                           "READY", "READY_FOR_BILLING", "BILLED", "CLOSED"],
                label_visibility="collapsed", key="oev_status_rx")
        fd1, fd2, fd3 = st.columns([1.6, 1.2, 1.2])
        with fd1:
            search = st.text_input("Search", placeholder="Any search",
                                   label_visibility="collapsed", key="oev_search_rx")
        with fd2:
            from_d = st.date_input("From", value=datetime.date.today() - datetime.timedelta(days=30),
                                   label_visibility="collapsed", key="oev_from_rx")
        with fd3:
            to_d = st.date_input("To", value=datetime.date.today(),
                                 label_visibility="collapsed", key="oev_to_rx")
        # RX tab shows RETAIL + WHOLESALE
        orders_r = _load_orders(search, status_f, from_d, to_d, "RETAIL",
                                patient_search, mobile_search, doc_search)
        orders_w = _load_orders(search, status_f, from_d, to_d, "WHOLESALE",
                                patient_search, mobile_search, doc_search)
        orders = orders_r + orders_w
        orders.sort(key=lambda o: str(o.get("created_at","")), reverse=True)
    else:
        fc1, fc2, fc3 = st.columns([1.4, 1.8, 1.3])
        with fc1:
            doc_search = st.text_input("Consult No", placeholder="Consult no",
                                       label_visibility="collapsed", key="oev_doc_cons")
        with fc2:
            patient_search = st.text_input("Patient", placeholder="Patient",
                                           label_visibility="collapsed", key="oev_patient_cons")
        with fc3:
            mobile_search = st.text_input("Mobile", placeholder="Mobile",
                                          label_visibility="collapsed", key="oev_mobile_cons")
        fd1, fd2, fd3 = st.columns([1.6, 1.2, 1.2])
        with fd1:
            search = st.text_input("Search", placeholder="Any search",
                                   label_visibility="collapsed", key="oev_search_cons")
        with fd2:
            from_d = st.date_input("From", value=datetime.date.today() - datetime.timedelta(days=30),
                                   label_visibility="collapsed", key="oev_from_cons")
        with fd3:
            to_d = st.date_input("To", value=datetime.date.today(),
                                 label_visibility="collapsed", key="oev_to_cons")
        status_f = "All"
        orders = _load_orders(search, status_f, from_d, to_d, "CONSULTATION",
                              patient_search, mobile_search, doc_search)

    # For RX tab filter out consultations (consultations show in their own tab)
    if tab_otype == "RX":
        orders = [o for o in orders if str(o.get("order_type","")).upper() != "CONSULTATION"]

    if not orders:
        st.info("No orders found for the selected filters.")
        return

    # ── Summary KPIs ──────────────────────────────────────────────────────
    _editable_n  = sum(1 for o in orders if _is_editable(
        str(o.get("status","")),
        str(o.get("order_type","")),
        bool(o.get("is_converted")),
        str(o.get("linked_retail_no") or ""),
    ))
    # UNDER_REVIEW label
    _under_review_n = sum(1 for o in orders if str(o.get("status","")).upper() == "UNDER_REVIEW")
    _confirmed_n = len(orders) - _editable_n
    k1, k2, k3 = st.columns(3)
    k1.metric("Total Orders", len(orders))
    k2.metric("✏️ Editable", _editable_n, help="Can be modified")
    k3.metric("🔒 Confirmed", _confirmed_n, help="Locked — backoffice saved")

    st.markdown("<hr style='border:none;border-top:1px solid #1e293b;margin:8px 0'>",
                unsafe_allow_html=True)

    # ── Column headers ────────────────────────────────────────────────────
    _hcols = st.columns([0.5, 1.8, 2.5, 1.2, 0.7, 1.0, 0.9])
    for hc, hl in zip(_hcols, ["", "Order No", "Patient / Party",
                                 "Date", "Lines", "Value", "Status"]):
        hc.markdown(
            f"<div style='font-size:0.65rem;font-weight:700;color:#475569;"
            f"text-transform:uppercase;letter-spacing:.06em'>{hl}</div>",
            unsafe_allow_html=True)
    st.markdown("<hr style='border:none;border-top:1px solid #1e293b;margin:2px 0 6px'>",
                unsafe_allow_html=True)

    # ── State: which order is open ────────────────────────────────────────
    _open_key = f"oev_open_order_{tab_otype}"
    if _open_key not in st.session_state:
        st.session_state[_open_key] = None

    for o in orders:
        _oid     = str(o.get("id") or "")
        _is_consult_row = str(o.get("order_type","")).upper() == "CONSULTATION"
        _ono     = str(o.get("order_no") or "—") if _is_consult_row else str(o.get("display_order_no") or o.get("order_no") or "—")
        _name    = o.get("patient_name") or o.get("party_name") or "—"
        _otype   = str(o.get("order_type") or "")
        try:
            from modules.backoffice.order_status_live import get_live_status as _gls_oev
            _status = _gls_oev(o)
        except Exception:
            _status = str(o.get("status") or "PENDING").upper()
        _sc      = _STATUS_COLOR.get(_status, "#64748b")
        _edit    = _is_editable(
            _status,
            str(o.get("order_type","")),
            bool(o.get("is_converted")),
            str(o.get("linked_retail_no") or ""),
        )
        _lc      = int(o.get("line_count") or 0)
        _val     = float(o.get("total_value") or 0)
        _date    = _fmt_date(o.get("created_at"))
        _is_open = st.session_state[_open_key] == _oid
        _row_bg  = "#1e293b22" if _is_open else "transparent"

        st.markdown(f"<div style='background:{_row_bg};border-radius:6px;margin:1px 0'>",
                    unsafe_allow_html=True)
        rcols = st.columns([0.5, 1.8, 2.5, 1.2, 0.7, 1.0, 0.9])

        # Detect converted consultation — button is dead (greyed, no action)
        # FIX: status=CLOSED is the normal state for ALL saved consultations.
        # Only freeze when is_converted=True AND a real linked retail order
        # exists (linked_retail_no is set). Using status alone was causing every
        # consultation to appear frozen/CONVERTED immediately after saving.
        _is_converted_consult = (
            _is_consult_row
            and bool(o.get("is_converted"))
            and bool(o.get("linked_retail_no"))
        )

        with rcols[0]:
            if _is_converted_consult:
                if st.button("🔒", key=f"oev_lock_{_oid}",
                             width='stretch',
                             help="View locked consultation documents"):
                    st.session_state[_open_key] = None if _is_open else _oid
                    st.session_state["_oev_land_on_consult"] = True
                    st.rerun()
            else:
                _arrow = "▼" if _is_open else ("✏️" if _edit else "▶")
                if st.button(_arrow, key=f"oev_arr_{_oid}",
                             width='stretch',
                             help="Edit" if _edit else "View"):
                    st.session_state[_open_key] = None if _is_open else _oid
                    # FIX 4: set flag so Consultations tab is auto-selected
                    if _is_consult_row and not _is_open:
                        st.session_state["_oev_land_on_consult"] = True
                    st.rerun()

        with rcols[1]:
            if _is_converted_consult:
                # Find linked retail order_no from DB
                try:
                    from modules.sql_adapter import run_query as _rq_lnk
                    _lnk = (_rq_lnk(
                        "SELECT order_no FROM orders WHERE customer_order_no=%s"
                        " AND COALESCE(is_deleted,false)=false LIMIT 1",
                        (_oid,)) or [{}])[0].get("order_no","")
                except Exception:
                    _lnk = ""
                _lnk_txt = f" → {_lnk}" if _lnk else ""
                st.markdown(
                    f"<div style='padding:4px 2px;color:#334155;font-size:0.78rem'>"
                    f"🩺 {_ono}"
                    f"<span style='color:#6366f1;font-size:0.7rem'>{_lnk_txt}</span></div>",
                    unsafe_allow_html=True)
            else:
                _btn_type = "primary" if _is_open else "secondary"
                if st.button(_ono, key=f"oev_no_{_oid}",
                             type=_btn_type, width='stretch'):
                    st.session_state[_open_key] = None if _is_open else _oid
                    if _is_consult_row and not _is_open:
                        st.session_state["_oev_land_on_consult"] = True
                    st.rerun()

        with rcols[2]:
            _tc = {"RETAIL": "#0891b2", "WHOLESALE": "#8b5cf6",
                   "CONSULTATION": "#10b981"}.get(_otype, "#64748b")
            st.markdown(
                f"<div style='padding:4px 2px'>"
                f"<div style='color:#e2e8f0;font-size:0.82rem;font-weight:600'>{_name}</div>"
                f"<span style='background:{_tc}33;color:{_tc};padding:1px 6px;"
                f"border-radius:6px;font-size:0.62rem;font-weight:700'>{_otype}</span>"
                f"</div>",
                unsafe_allow_html=True)

        with rcols[3]:
            st.markdown(f"<div style='color:#cbd5e1;font-size:0.75rem;padding:6px 2px'>{_date}</div>",
                        unsafe_allow_html=True)

        with rcols[4]:
            _lc_color = "#ef4444" if _lc == 0 else "#cbd5e1"
            st.markdown(f"<div style='color:{_lc_color};text-align:center;padding:6px 2px;font-size:0.82rem'>{_lc}</div>",
                        unsafe_allow_html=True)

        with rcols[5]:
            st.markdown(f"<div style='color:#10b981;font-weight:700;text-align:right;padding:6px 2px;font-size:0.82rem'>₹{_val:,.0f}</div>",
                        unsafe_allow_html=True)

        with rcols[6]:
            if _is_converted_consult:
                st.markdown(
                    "<span style='background:#312e81;color:#a5b4fc;padding:2px 8px;"
                    "border-radius:8px;font-size:0.65rem;font-weight:700'>"
                    "🔄 ORDERED</span>",
                    unsafe_allow_html=True)
            elif _is_consult_row and not _is_converted_consult:
                st.markdown(
                    "<span style='background:#065f46;color:#34d399;padding:2px 8px;"
                    "border-radius:8px;font-size:0.65rem;font-weight:700'>"
                    "🩺 OPEN</span>",
                    unsafe_allow_html=True)
            else:
                # Bright, readable status badge
                _is_locked_status = _status in ("CONFIRMED","BILLED","DISPATCHED",
                                                 "DELIVERED","CLOSED","CANCELLED","IN_PRODUCTION")
                _badge_bg   = f"{_sc}33"
                _badge_text = _sc
                _edit_icon  = "🔒 " if _is_locked_status else "✏️ "
                st.markdown(
                    f"<span style='background:{_badge_bg};color:{_badge_text};"
                    f"padding:2px 8px;border-radius:8px;"
                    f"font-size:0.65rem;font-weight:700;white-space:nowrap'>"
                    f"{_edit_icon}{_status}</span>",
                    unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

        # ── Inline edit panel — blocked for converted consultations ───
        if _is_open:
            with st.container():
                st.markdown(
                    "<div style='background:#0b1628;border:1px solid #1e3a5f;"
                    "border-radius:10px;padding:16px 20px;margin:6px 0 14px'>",
                    unsafe_allow_html=True)

                _cl, _cr = st.columns([1, 6])
                with _cl:
                    if st.button("✕ Close", key=f"oev_cls_{_oid}",
                                 width='stretch'):
                        st.session_state[_open_key] = None
                        st.rerun()

                _render_order_edit_panel(o, _edit)
                st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<hr style='border:none;border-top:1px solid #1e293b;margin:16px 0'>",
                unsafe_allow_html=True)


# ── Inline order edit panel ───────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# CANCELLATION + REFUND + CREDIT NOTE PANEL
# ─────────────────────────────────────────────────────────────────────────────

_CANCEL_REASONS = [
    "— Select reason —",
    "Cancelled due to non-availability of stock",
    "Cancelled by Client / Party",
    "Cancelled — wrong prescription / entry error",
    "Cancelled — duplicate order",
    "Cancelled — customer changed mind",
    "Cancelled — price dispute",
    "Cancelled — delay / delivery issue",
    "Cancelled — product discontinued",
    "Other (specify below)",
]

_BILLED_CANCEL_REASONS = [
    "— Select reason —",
    "Return & Cancel — product not delivered",
    "Return & Cancel — wrong product",
    "Return & Cancel — defective / damaged",
    "Return & Cancel — power mismatch",
    "Return & Cancel — customer rejected",
    "Return & Cancel — non-availability (replacement not possible)",
    "Cancelled by Client / Party after billing",
    "Other (specify below)",
]

_REFUND_MODES = ["Cash", "UPI / GPay / PhonePe", "NEFT / RTGS", "Card Reversal", "Store Credit / Wallet"]


def _render_cancel_panel(order: dict, oid: str, ono: str, status: str):
    """
    Full cancellation, refund, and Credit Note panel.

    Flow:
      Pre-billed (PENDING / CONFIRMED / IN_PRODUCTION):
        → Cancel → select reason → confirm → status = CANCELLED
        → If advance paid → trigger refund → record refund mode + amount

      Post-billed (BILLED / DISPATCHED / DELIVERED):
        → Cancel requires Credit Note first
        → Raise Credit Note (CN-YYYYMMDD-XXXXXX) → amount = invoice total or partial
        → Select refund mode + amount → confirm → CN status = APPROVED + REFUND_PROCESSED

    All actions logged to order_status_history with reason + user.
    """
    from modules.security.roles import has_role, ADMIN, MANAGER, BILLING, current_user

    # ── Who can cancel ───────────────────────────────────────────────────────
    can_cancel = has_role(ADMIN, MANAGER, BILLING)
    if not can_cancel:
        return

    _upper_status = status.upper()

    # Already cancelled or returned — show history only
    if _upper_status in ("CANCELLED", "RETURNED", "REFUND_PROCESSED"):
        _show_cancel_history(oid, _upper_status)
        return

    # ── Determine flow ───────────────────────────────────────────────────────
    _is_billed = _upper_status in ("BILLED", "DISPATCHED", "DELIVERED", "CLOSED")
    _is_pre_bill = _upper_status in ("PENDING", "PROVISIONAL", "UNDER_REVIEW",
                                      "CONFIRMED", "IN_PRODUCTION", "READY")

    if not _is_billed and not _is_pre_bill:
        return  # Unknown status — don't show cancel

    # ── Section header ────────────────────────────────────────────────────────
    with st.expander(
        "🚫 Cancel Order" if _is_pre_bill else "🚫 Cancel / Return & Refund",
        expanded=st.session_state.get(f"_cancel_open_{oid}", False)
    ):
        st.session_state[f"_cancel_open_{oid}"] = True

        _advance_paid = float(order.get("advance_amount") or order.get("advance") or 0)
        _order_total  = float(order.get("total_value") or 0)
        _party        = order.get("patient_name") or order.get("party_name") or "—"
        _order_type   = str(order.get("order_type") or "RETAIL").upper()

        if _is_billed:
            _render_billed_cancel(oid, ono, _party, _order_total, _advance_paid, _order_type)
        else:
            _render_prebill_cancel(oid, ono, _party, _order_total, _advance_paid, _order_type, _upper_status)


def _render_prebill_cancel(oid, ono, party, order_total, advance_paid, order_type, status):
    """Cancel flow for pre-billed orders (no invoice exists)."""
    st.markdown(
        "<div style='background:#1a0a0a;border-left:3px solid #ef4444;"
        "padding:8px 12px;border-radius:4px;font-size:0.8rem;color:#94a3b8'>"
        "This order has <b>not been billed</b>. Cancellation will mark it as "
        "CANCELLED and restore stock. If an advance was collected, a refund "
        "will be recorded."
        "</div>",
        unsafe_allow_html=True
    )
    st.markdown("")

    # Reason
    reason = st.selectbox(
        "Cancellation reason",
        _CANCEL_REASONS,
        key=f"cancel_reason_{oid}"
    )
    other_reason = ""
    if reason == "Other (specify below)":
        other_reason = st.text_input(
            "Specify reason",
            key=f"cancel_other_{oid}",
            placeholder="Enter reason..."
        )

    final_reason = other_reason.strip() if reason == "Other (specify below)" else reason

    # Advance refund section
    _show_refund = False
    refund_amount = 0.0
    refund_mode   = ""
    refund_ref    = ""

    if advance_paid > 0:
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #f59e0b33;"
            f"border-radius:6px;padding:10px 14px;margin:8px 0'>"
            f"<div style='color:#f59e0b;font-size:0.78rem;font-weight:700'>💰 Advance Paid: ₹{advance_paid:,.2f}</div>"
            f"<div style='color:#64748b;font-size:0.72rem'>Refund must be processed and recorded.</div>"
            f"</div>",
            unsafe_allow_html=True
        )
        _show_refund = True
        ra1, ra2, ra3 = st.columns([2, 2, 2])
        refund_amount = ra1.number_input(
            "Refund amount ₹",
            min_value=0.0, max_value=advance_paid,
            value=advance_paid, step=1.0,
            key=f"cancel_refund_amt_{oid}"
        )
        refund_mode = ra2.selectbox(
            "Refund mode",
            _REFUND_MODES,
            key=f"cancel_refund_mode_{oid}"
        )
        refund_ref = ra3.text_input(
            "Reference / UTR (optional)",
            key=f"cancel_refund_ref_{oid}",
            placeholder="UTR / txn ID"
        )

    # Two-step confirm
    _step2_key = f"_cancel_step2_{oid}"
    if not st.session_state.get(_step2_key):
        if st.button(
            "🚫 Cancel This Order",
            key=f"cancel_btn1_{oid}",
            width='stretch',
            disabled=(final_reason in ("", "— Select reason —"))
        ):
            st.session_state[_step2_key] = True
            st.rerun()
    else:
        _confirm_msg = (
            f"Cancel order {ono} for {party}? "
            f"Reason: {final_reason}"
            + (f" | Refund Rs.{refund_amount:,.2f} via {refund_mode}" if _show_refund and refund_amount > 0 else "")
            + " | This cannot be undone."
        )
        st.warning(_confirm_msg)
        _cc1, _cc2 = st.columns(2)
        with _cc1:
            if st.button("✅ Yes, Cancel Order", key=f"cancel_confirm_{oid}",
                         type="primary", width='stretch'):
                _do_cancel(
                    oid=oid, ono=ono, status=status,
                    reason=final_reason,
                    refund_amount=refund_amount if _show_refund else 0,
                    refund_mode=refund_mode,
                    refund_ref=refund_ref,
                    is_billed=False,
                    credit_note_no="",
                )
                st.session_state.pop(_step2_key, None)
        with _cc2:
            if st.button("← Go Back", key=f"cancel_back_{oid}", width='stretch'):
                st.session_state.pop(_step2_key, None)
                st.rerun()


def _render_billed_cancel(oid, ono, party, order_total, advance_paid, order_type):
    """Cancel flow for billed orders — requires Credit Note."""
    st.markdown(
        "<div style='background:#1a0a0a;border-left:3px solid #f59e0b;"
        "padding:8px 12px;border-radius:4px;font-size:0.8rem;color:#94a3b8'>"
        "This order <b>has been billed</b>. A <b>Credit Note</b> must be raised "
        "to reverse the invoice before cancellation. The CN number will be "
        "auto-generated and linked to this order."
        "</div>",
        unsafe_allow_html=True
    )
    st.markdown("")

    # Check for existing credit note
    _cn_key = f"_cn_raised_{oid}"
    _existing_cn = st.session_state.get(_cn_key, {})

    if not _existing_cn:
        # ── Step 1: Raise Credit Note ─────────────────────────────────────
        st.markdown("**Step 1 — Raise Credit Note**")

        reason = st.selectbox(
            "Reason for return / cancellation",
            _BILLED_CANCEL_REASONS,
            key=f"bcancel_reason_{oid}"
        )
        other_reason = ""
        if reason == "Other (specify below)":
            other_reason = st.text_input(
                "Specify reason",
                key=f"bcancel_other_{oid}",
                placeholder="Enter reason..."
            )
        final_reason = other_reason.strip() if reason == "Other (specify below)" else reason

        ca1, ca2 = st.columns(2)
        cn_amount = ca1.number_input(
            "Credit Note amount ₹",
            min_value=0.01, max_value=max(order_total, 0.01),
            value=order_total, step=1.0,
            key=f"cn_amount_{oid}",
            help="Full order value for full cancellation, or partial for partial return"
        )
        cn_type = ca2.radio(
            "Credit Note type",
            ["Full cancellation", "Partial return"],
            key=f"cn_type_{oid}",
            horizontal=True
        )

        cn_notes = st.text_area(
            "Notes (optional)",
            key=f"cn_notes_{oid}",
            height=60,
            placeholder="e.g. Product returned in original condition, no damage"
        )

        if st.button(
            "📄 Raise Credit Note",
            key=f"raise_cn_{oid}",
            type="primary",
            width='stretch',
            disabled=(final_reason in ("", "— Select reason —"))
        ):
            import uuid, datetime
            cn_no = f"CN-{datetime.date.today().strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"
            try:
                from modules.sql_adapter import run_write as _rw_cn, run_query as _rq_cn
                _rw_cn("""
                    INSERT INTO credit_notes
                        (cn_number, order_id, order_no, party_name,
                         grand_total, reason, reason_detail, remarks, status)
                    VALUES
                        (%(cn_no)s, %(oid)s::uuid, %(ono)s, %(party)s,
                         %(amt)s, %(reason_code)s, %(reason)s, %(notes)s, 'DRAFT')
                    ON CONFLICT (cn_number) DO NOTHING
                """, {
                    "cn_no": cn_no, "oid": oid, "ono": ono, "party": party,
                    "amt": cn_amount,
                    "reason_code": "01",  # GST reason code: return of goods
                    "reason": final_reason, "notes": cn_notes,
                })
                st.session_state[_cn_key] = {
                    "cn_no": cn_no, "amount": cn_amount,
                    "reason": final_reason, "type": cn_type
                }
                st.success(f"✅ Credit Note **{cn_no}** raised for ₹{cn_amount:,.2f}")
                st.rerun()
            except Exception as e:
                st.error(f"Error raising Credit Note: {e}")

    else:
        # ── Step 2: Approve CN + Refund ───────────────────────────────────
        _cn = _existing_cn
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #10b98133;"
            f"border-radius:6px;padding:10px 14px;margin-bottom:12px'>"
            f"<div style='color:#10b981;font-weight:700'>📄 Credit Note Raised</div>"
            f"<div style='color:#e2e8f0;font-size:0.9rem;margin-top:4px'>"
            f"<b>{_cn['cn_no']}</b> · ₹{_cn['amount']:,.2f} · {_cn['type']}</div>"
            f"<div style='color:#64748b;font-size:0.72rem'>Reason: {_cn['reason']}</div>"
            f"</div>",
            unsafe_allow_html=True
        )

        st.markdown("**Step 2 — Process Refund & Confirm Cancellation**")

        rb1, rb2, rb3 = st.columns([2, 2, 2])
        refund_amount = rb1.number_input(
            "Refund amount ₹",
            min_value=0.0, max_value=_cn["amount"],
            value=_cn["amount"], step=1.0,
            key=f"bcancel_refund_amt_{oid}"
        )
        refund_mode = rb2.selectbox(
            "Refund mode",
            _REFUND_MODES,
            key=f"bcancel_refund_mode_{oid}"
        )
        refund_ref = rb3.text_input(
            "Reference / UTR",
            key=f"bcancel_refund_ref_{oid}",
            placeholder="UTR / txn ID / cheque no."
        )

        refund_date = st.date_input(
            "Refund date",
            key=f"bcancel_refund_date_{oid}"
        )

        _step3_key = f"_bcancel_step3_{oid}"
        if not st.session_state.get(_step3_key):
            if st.button(
                "✅ Approve CN & Process Cancellation",
                key=f"bcancel_confirm1_{oid}",
                type="primary", width='stretch'
            ):
                st.session_state[_step3_key] = True
                st.rerun()
        else:
            _final_msg = (
                f"Final confirmation: cancel order {ono} for {party}. "
                f"Credit Note {_cn['cn_no']} will be marked APPROVED. "
                f"Refund Rs.{refund_amount:,.2f} via {refund_mode}"
                + (f" Ref: {refund_ref}" if refund_ref else "") + "."
            )
            st.warning(_final_msg)
            _fc1, _fc2 = st.columns(2)
            with _fc1:
                if st.button("✅ Confirm", key=f"bcancel_final_{oid}",
                             type="primary", width='stretch'):
                    _do_cancel(
                        oid=oid, ono=ono, status="BILLED",
                        reason=_cn["reason"],
                        refund_amount=refund_amount,
                        refund_mode=refund_mode,
                        refund_ref=refund_ref,
                        is_billed=True,
                        credit_note_no=_cn["cn_no"],
                    )
                    st.session_state.pop(_cn_key, None)
                    st.session_state.pop(_step3_key, None)
            with _fc2:
                if st.button("← Back", key=f"bcancel_back_{oid}",
                             width='stretch'):
                    st.session_state.pop(_step3_key, None)
                    st.rerun()


def _do_cancel(oid, ono, status, reason, refund_amount, refund_mode, refund_ref,
               is_billed, credit_note_no):
    """Execute cancellation — update order status, log history, update credit note."""
    from modules.security.roles import current_user
    from modules.sql_adapter import run_write as _rw, run_query as _rq
    import datetime

    user_name = (current_user() or {}).get("name", "backoffice")

    try:
        # Ensure cancel_reason column exists (ADD only if missing — safe to repeat)
        try:
            _rw("ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancel_reason TEXT")
        except Exception:
            pass

        # 1. Update order status
        _audit_note = (
            f"[{datetime.datetime.now().strftime('%d-%b-%Y %H:%M')}] "
            f"CANCELLED by {user_name}: {reason}"
            + (f" | CN: {credit_note_no}" if credit_note_no else "")
            + (f" | Refund Rs.{refund_amount:,.2f} via {refund_mode}"
               if refund_amount > 0 else "")
        )
        _rw("""
            UPDATE orders
            SET status = 'CANCELLED',
                cancel_reason = %(reason)s,
                updated_at = NOW()
            WHERE id = %(oid)s::uuid
        """, {
            "oid": oid,
            "reason": _audit_note,
        })

        # 2. Log to order_status_history
        try:
            _rw("""
                INSERT INTO order_status_history
                    (order_id, from_status, to_status, changed_by_name, remarks, changed_at)
                VALUES
                    (%(oid)s::uuid, %(from_s)s, 'CANCELLED', %(user)s, %(remarks)s, NOW())
            """, {
                "oid": oid, "from_s": status, "user": user_name,
                "remarks": reason
                    + (f" | Credit Note: {credit_note_no}" if credit_note_no else "")
                    + (f" | Refund: ₹{refund_amount:,.2f} via {refund_mode}" if refund_amount > 0 else ""),
            })
        except Exception:
            pass  # history table may not exist on older installs

        # 3. Record refund if applicable
        if refund_amount > 0:
            try:
                _rw("""
                    CREATE TABLE IF NOT EXISTS order_refunds (
                        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        order_id      UUID,
                        order_no      TEXT,
                        credit_note_no TEXT,
                        refund_amount NUMERIC(12,2),
                        refund_mode   TEXT,
                        refund_ref    TEXT,
                        refunded_by   TEXT,
                        refunded_at   TIMESTAMPTZ DEFAULT NOW(),
                        remarks       TEXT
                    )
                """)
                _rw("""
                    INSERT INTO order_refunds
                        (order_id, order_no, credit_note_no, refund_amount,
                         refund_mode, refund_ref, refunded_by, remarks)
                    VALUES
                        (%(oid)s::uuid, %(ono)s, %(cn)s, %(amt)s,
                         %(mode)s, %(ref)s, %(user)s, %(reason)s)
                """, {
                    "oid": oid, "ono": ono, "cn": credit_note_no or "",
                    "amt": refund_amount, "mode": refund_mode,
                    "ref": refund_ref or "", "user": user_name, "reason": reason,
                })
            except Exception:
                pass

        # 4. Update credit note status if applicable
        if credit_note_no:
            try:
                _rw("""
                    UPDATE credit_notes
                    SET status = 'APPROVED',
                        refund_mode   = %(mode)s,
                        refund_amount = %(amt)s,
                        refund_ref    = %(ref)s,
                        updated_at    = NOW()
                    WHERE cn_number = %(cn_no)s
                """, {
                    "mode": refund_mode, "amt": refund_amount,
                    "ref": refund_ref or "", "cn_no": credit_note_no,
                })
            except Exception:
                pass

        # 5. Show success
        _success_msg = f"✅ Order **{ono}** cancelled successfully."
        if credit_note_no:
            _success_msg += f" Credit Note **{credit_note_no}** approved."
        if refund_amount > 0:
            _success_msg += f" Refund ₹{refund_amount:,.2f} via {refund_mode} recorded."
        st.success(_success_msg)
        st.rerun()

    except Exception as e:
        st.error(f"❌ Cancellation failed: {e}")


def _show_cancel_history(oid, status):
    """Show cancellation and refund history for already-cancelled orders."""
    from modules.sql_adapter import run_query as _rq_h
    _color = "#ef4444" if status == "CANCELLED" else "#10b981"
    st.markdown(
        f"<div style='background:#1a0a0a;border-left:3px solid {_color};"
        f"padding:8px 12px;border-radius:4px;font-size:0.8rem;color:#94a3b8'>"
        f"<b style='color:{_color}'>Order status: {status}</b>"
        f"</div>",
        unsafe_allow_html=True
    )

    # Refund records
    try:
        refunds = _rq_h("""
            SELECT refund_amount, refund_mode, refund_ref,
                   refunded_by, refunded_at::text, remarks
            FROM order_refunds WHERE order_id = %(oid)s::uuid
            ORDER BY refunded_at DESC
        """, {"oid": oid}) or []
        if refunds:
            st.markdown("**💰 Refunds Processed**")
            for r in refunds:
                st.markdown(
                    f"<div style='background:#0f172a;border-radius:6px;padding:8px 12px;margin:4px 0'>"
                    f"<span style='color:#10b981;font-weight:700'>₹{float(r['refund_amount']):,.2f}</span>"
                    f" via <b>{r['refund_mode']}</b>"
                    + (f" · Ref: {r['refund_ref']}" if r.get('refund_ref') else "")
                    + f"<br><span style='color:#64748b;font-size:0.72rem'>"
                    f"By {r.get('refunded_by','—')} · {str(r.get('refunded_at',''))[:16]}"
                    f"</span></div>",
                    unsafe_allow_html=True
                )
    except Exception:
        pass

    # Credit notes
    try:
        cns = _rq_h("""
            SELECT cn_number AS cn_no, grand_total AS cn_amount,
                   reason_detail, status,
                   refund_mode, refund_amount,
                   created_at::text
            FROM credit_notes WHERE order_id = %(oid)s::uuid
            ORDER BY created_at DESC
        """, {"oid": oid}) or []
        if cns:
            st.markdown("**📄 Credit Notes**")
            for cn in cns:
                _cn_c = "#10b981" if cn["status"] == "APPROVED" else "#f59e0b"
                st.markdown(
                    f"<div style='background:#0f172a;border-left:3px solid {_cn_c};"
                    f"border-radius:6px;padding:8px 12px;margin:4px 0'>"
                    f"<b style='color:{_cn_c}'>{cn['cn_no']}</b>"
                    f" · ₹{float(cn['cn_amount']):,.2f}"
                    f" · <span style='color:{_cn_c}'>{cn['status']}</span>"
                    f"<br><span style='color:#64748b;font-size:0.72rem'>"
                    f"{cn.get('reason_detail','')}</span></div>",
                    unsafe_allow_html=True
                )
    except Exception:
        pass



def _render_order_edit_panel(order: Dict, editable: bool):
    from modules.core.business_rules import RETAIL_EDIT_LOCKED_AFTER, STAGE_RELEASE_ALLOWED_FROM
    _cur_st = str(order.get("status","")).upper()
    _is_consult_panel = str(order.get("order_type","")).upper() == "CONSULTATION"
    _is_genuinely_converted = (
        bool(order.get("is_converted")) and bool(order.get("linked_retail_no"))
    )

    # FIX: Consultation orders are always saved as status=CLOSED — that is their
    # normal resting state, NOT a backoffice lock. Only block them when they are
    # genuinely converted (is_converted=True + linked_retail_no set).
    # Non-consultation orders follow the normal RETAIL_EDIT_LOCKED_AFTER rules.
    if not _is_consult_panel and _cur_st in RETAIL_EDIT_LOCKED_AFTER:
        # ── Order confirmed in backoffice — fully locked ──────────────────
        _lock_color = {"CONFIRMED":"#6366f1","BILLED":"#10b981",
                       "DISPATCHED":"#3b82f6","DELIVERED":"#22c55e"}.get(_cur_st,"#64748b")
        st.markdown(
            f"<div style='background:#0a0f1a;border:2px solid {_lock_color}44;"
            f"border-left:4px solid {_lock_color};border-radius:8px;"
            f"padding:12px 16px;margin-bottom:10px'>"
            f"<div style='color:{_lock_color};font-weight:800;font-size:0.88rem'>"
            f"🔒 Order {_cur_st} — No further edits</div>"
            f"<div style='color:#94a3b8;font-size:0.78rem;margin-top:4px'>"
            f"This order has been confirmed in backoffice. "
            f"To make changes, raise a Credit/Debit Note or contact the backoffice team."
            f"</div></div>",
            unsafe_allow_html=True,
        )
        return
    _oid      = str(order.get("id") or "")
    _ono      = order.get("display_order_no") or order.get("order_no") or "—"
    _patient  = str(order.get("patient_name") or "").strip()
    _party    = str(order.get("party_name") or "").strip()
    # For wholesale: party is the retailer; for retail: patient is the customer
    _name_main  = _patient or _party or "—"
    _name_sub   = _party if (_patient and _party and _patient != _party) else ""
    _status   = str(order.get("status") or "PENDING")
    _is_consult = str(order.get("order_type","")).upper() == "CONSULTATION"
    if _is_consult:
        _ono = order.get("order_no") or _ono

    if _is_consult and _is_genuinely_converted:
        _linked_no = str(order.get("linked_retail_no") or "").strip()
        _linked_txt = f" Full billing order: {_linked_no}." if _linked_no else ""
        st.markdown(
            "<div style='background:#111827;border:2px solid #4f46e544;"
            "border-left:4px solid #6366f1;border-radius:8px;"
            "padding:12px 16px;margin-bottom:10px'>"
            "<div style='color:#a5b4fc;font-weight:800;font-size:0.9rem'>"
            "🔒 Consultation shifted to Full Billing</div>"
            "<div style='color:#cbd5e1;font-size:0.8rem;margin-top:4px'>"
            f"{_ono} has already been changed to full billing.{_linked_txt} "
            "No edits or RX changes are allowed from Orders now."
            "</div></div>",
            unsafe_allow_html=True,
        )

    _status_colour = "#fcd34d" if editable else "#6366f1"
    _status_label  = "✏️ EDITABLE" if editable else "🔒 CONFIRMED"
    st.markdown(
        f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
        f"border-radius:8px;padding:10px 14px;margin-bottom:10px'>"
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start'>"
        f"<div>"
        f"<span style='color:#60a5fa;font-size:1rem;font-weight:800'>{_ono}</span>"
        f"<br><span style='color:#e2e8f0;font-size:0.9rem;font-weight:700'>{_name_main}</span>"
        + (f"<br><span style='color:#94a3b8;font-size:0.75rem'>🏪 {_name_sub}</span>" if _name_sub else "")
        + f"</div>"
        f"<div style='text-align:right'>"
        f"<span style='color:{_status_colour};font-size:0.75rem;font-weight:700'>"
        f"{_status_label}</span>"
        f"<br><span style='color:#475569;font-size:0.7rem'>{_status}</span>"
        f"</div>"
        f"</div></div>",
        unsafe_allow_html=True)

    # ── Edit Order button ─────────────────────────────────────────────────
    _otype_up = str(order.get("order_type","RETAIL")).upper()

    if _is_consult:
        # ── CONSULTATION: buttons rendered here, outside RETAIL/WHOLESALE guard ──
        _oe_guard_ok = str(order.get("status","")).upper() != "CANCELLED"
        # DO NOT run OrderGuard — CLOSED is normal state for consultations

        if not _oe_guard_ok:
            st.markdown(
                "<div style='background:#1a1a1a;border:1px solid #334155;"
                "border-radius:8px;padding:8px 14px;color:#475569;font-size:0.8rem'>"
                "🔒 This consultation is cancelled.</div>",
                unsafe_allow_html=True
            )
        else:
            # ── CONSULTATION — single clean action panel ──────────────────
            _c_fee  = float(order.get("total_value") or 0)
            _c_mode = order.get("payment_mode") or "—"
            _c_date = str(order.get("created_at",""))[:10]

            st.markdown(
                f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
                f"border-radius:8px;padding:10px 14px;margin-bottom:10px'>"
                f"<div style='color:#60a5fa;font-weight:700;font-size:0.85rem'>"
                f"🩺 Consultation — {_c_date}</div>"
                f"<div style='color:#94a3b8;font-size:0.75rem;margin-top:3px'>"
                f"Fee: <b style='color:#10b981'>₹{_c_fee:.0f}</b>"
                f"&nbsp;·&nbsp;Mode: {_c_mode}"
                f"</div></div>",
                unsafe_allow_html=True,
            )

            # Fetch power before buttons
            _rx_r = {}; _rx_l = {}
            try:
                from modules.sql_adapter import run_query as _rq_pre
                _rx_rows_pre = []
                _visit_id_pre = str(order.get("customer_order_no","") or "").strip()
                _pid_pre      = str(order.get("party_id") or "").strip()
                _pname_pre    = str(order.get("patient_name","") or "")
                if _visit_id_pre and len(_visit_id_pre) >= 32:
                    _rx_rows_pre = _rq_pre("""
                        SELECT right_sph, right_cyl, right_axis, right_add,
                               left_sph,  left_cyl,  left_axis,  left_add
                        FROM patient_visits WHERE id=%s::uuid LIMIT 1
                    """, (_visit_id_pre,)) or []
                if not _rx_rows_pre and _pid_pre and len(_pid_pre) > 10:
                    _rx_rows_pre = _rq_pre("""
                        SELECT right_sph, right_cyl, right_axis, right_add,
                               left_sph,  left_cyl,  left_axis,  left_add
                        FROM patient_visits WHERE patient_id=%s::uuid
                        ORDER BY visit_date DESC LIMIT 1
                    """, (_pid_pre,)) or []
                if not _rx_rows_pre and _pname_pre and _pname_pre != "—":
                    _rx_rows_pre = _rq_pre("""
                        SELECT pv.right_sph, pv.right_cyl, pv.right_axis, pv.right_add,
                               pv.left_sph,  pv.left_cyl,  pv.left_axis,  pv.left_add
                        FROM patient_visits pv
                        JOIN patients pt ON pt.id = pv.patient_id
                        WHERE pt.master_name ILIKE %s
                        ORDER BY pv.visit_date DESC LIMIT 1
                    """, (_pname_pre,)) or []
                if _rx_rows_pre:
                    _rx = _rx_rows_pre[0]
                    _rx_r = {"sph":_rx.get("right_sph"),"cyl":_rx.get("right_cyl"),
                             "axis":_rx.get("right_axis"),"add":_rx.get("right_add")}
                    _rx_l = {"sph":_rx.get("left_sph"), "cyl":_rx.get("left_cyl"),
                             "axis":_rx.get("left_axis"),"add":_rx.get("left_add")}
            except Exception:
                pass

            # ── SINGLE ACTION: Open Consultation ─────────────────────────
            if _is_genuinely_converted:
                st.info("Billing/RX is locked. Clinical notes and print options are still available below.")
            elif st.button(
                "✏️ Open Consultation",
                key=f"edit_consult_{_oid[:8]}",
                type="primary",
                use_container_width=True,
                help="Open to view/edit powers, then convert to a Retail Order when ready",
            ):
                _pid_ec  = str(order.get("party_id") or "")
                _mob_ec  = str(order.get("patient_mobile","") or "")
                _name_ec = str(order.get("patient_name","") or "")
                _vid_ec  = str(order.get("customer_order_no","") or "")
                if not _pid_ec:
                    _mob_ec = ""

                # If mobile blank on order (old consultations saved before mobile-fix),
                # fetch from patients table so header never shows blank.
                if not _mob_ec and _pid_ec and len(_pid_ec) > 10:
                    try:
                        from modules.sql_adapter import run_query as _rq_mob_ec
                        _mob_ec_row = _rq_mob_ec(
                            "SELECT COALESCE(mobile,'') AS m FROM patients WHERE id=%s::uuid LIMIT 1",
                            (_pid_ec,)
                        ) or []
                        if _mob_ec_row:
                            _mob_ec = str(_mob_ec_row[0].get("m","") or "").strip()
                    except Exception:
                        pass

                if _pid_ec and len(_pid_ec) > 10:
                    st.session_state["retail_patient_id"]     = _pid_ec
                st.session_state["retail_patient_name"]       = _name_ec
                st.session_state["retail_patient_mobile"]     = _mob_ec
                if _mob_ec:
                    st.session_state["consult_wa_mobile_display"] = _mob_ec
                else:
                    st.session_state.pop("consult_wa_mobile_display", None)

                if _rx_r:
                    st.session_state["retail_old_rx_r"] = dict(_rx_r)
                    st.session_state["retail_new_rx_r"] = dict(_rx_r)
                if _rx_l:
                    st.session_state["retail_old_rx_l"] = dict(_rx_l)
                    st.session_state["retail_new_rx_l"] = dict(_rx_l)

                st.session_state["_erp_mode"]                  = "CONSULT_EDIT"
                st.session_state["_editing_consult_order_id"]  = _oid
                st.session_state["_erp_visit_id"]              = _vid_ec
                st.session_state["_erp_order_id"]              = _oid
                st.session_state["_erp_patient_id"]            = _pid_ec
                st.session_state["_erp_patient_name"]          = _name_ec
                st.session_state["_erp_patient_mob"]           = _mob_ec
                st.session_state["_erp_rx_r"]                  = dict(_rx_r) if _rx_r else {}
                st.session_state["_erp_rx_l"]                  = dict(_rx_l) if _rx_l else {}

                # Populate flat retail_* RX keys so clinical_exam/consultation
                # pre-fills power fields correctly on load
                if _rx_r:
                    st.session_state["retail_right_sph"]  = _rx_r.get("sph") or 0
                    st.session_state["retail_right_cyl"]  = _rx_r.get("cyl") or 0
                    st.session_state["retail_right_axis"] = _rx_r.get("axis") or 0
                    st.session_state["retail_right_add"]  = _rx_r.get("add") or 0
                if _rx_l:
                    st.session_state["retail_left_sph"]   = _rx_l.get("sph") or 0
                    st.session_state["retail_left_cyl"]   = _rx_l.get("cyl") or 0
                    st.session_state["retail_left_axis"]  = _rx_l.get("axis") or 0
                    st.session_state["retail_left_add"]   = _rx_l.get("add") or 0

                # Force Consultation tab — NEVER full billing
                # Do NOT set _force_full_billing_mode or _erp_mode="CONSULT_BILLING".
                # Those are only set when staff explicitly clicks "Open Full Billing"
                # from inside the consultation screen.
                st.session_state["_visit_mode_default"]         = 1
                st.session_state["_force_consultation_tab"]     = True
                st.session_state.pop("_force_full_billing_mode", None)
                st.session_state.pop("retail_visit_mode", None)   # cleared so _visit_mode_default=1 wins
                st.session_state["_sidebar_page"]               = "🛍️  Retail Order"
                st.rerun()

            # Note: Convert to Billing happens FROM within the consultation screen
            # via the explicit "🛍️ Open Full Billing for this patient" button.
            st.caption(
                "💡 Open → review/edit power → click '🛍️ Open Full Billing' "
                "inside the consultation screen to create a Retail Order."
            )

    elif _otype_up in ("RETAIL","WHOLESALE"):
        # ── RETAIL / WHOLESALE edit ───────────────────────────────────────
        # No OrderGuard — staff can always open and edit orders that aren't
        # CONFIRMED yet. CONFIRMED+ orders are already blocked by _is_editable
        # in the pipeline lock section above, so they never reach here.
        _is_cancelled = str(order.get("status","")).upper() == "CANCELLED"

        if _is_cancelled:
            st.markdown(
                "<div style='background:#1a0000;border:1px solid #7f1d1d;"
                "border-radius:8px;padding:8px 14px;color:#fca5a5;font-size:0.8rem'>"
                "🚫 This order is cancelled — no further edits possible.</div>",
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                "<div style='background:#1e3a5f;border-radius:8px;padding:10px 14px;"
                "margin-bottom:10px;border-left:4px solid #f97316'>"
                "<span style='color:#fbbf24;font-size:0.72rem;font-weight:700;"
                "text-transform:uppercase;letter-spacing:.06em'>✏️ Edit this order</span>"
                "<br><span style='color:#94a3b8;font-size:0.78rem'>"
                "Opens in punching screen — change products, power, qty, advance. "
                "All changes are logged with your user ID and timestamp.</span>"
                "</div>",
                unsafe_allow_html=True
            )
            if st.button("✏️ Open in Punching Screen to Edit",
                         key=f"edit_order_{_oid[:8]}",
                         type="primary",
                         width='stretch',
                         help="Opens in Retail/Wholesale Punching with all lines pre-loaded"):
                _lines_for_edit = _load_lines(_oid)
                _edit_result = _preload_order_for_edit(order, _lines_for_edit)
                if "error" in _edit_result:
                    st.error(_edit_result["error"])
                else:
                    st.session_state["_erp_mode"]     = "BILL_EDIT"
                    st.session_state["_erp_order_id"] = _oid
                    st.session_state["_erp_order_no"] = _ono
                    st.session_state["_order_edit_prefill"] = _edit_result
                    st.session_state["_sidebar_page"]       = _edit_result["sidebar_page"]
                    st.rerun()

        st.markdown("---")
        try:
            _latest_edit = _rq("""
                SELECT changed_by_name, remarks,
                       COALESCE(changed_at::text, '') AS changed_at
                FROM order_status_history
                WHERE order_id = %(oid)s::uuid
                  AND from_status = 'EDIT_SOURCE'
                ORDER BY changed_at DESC NULLS LAST
                LIMIT 1
            """, {"oid": _oid}) or []
            if _latest_edit:
                _eh = _latest_edit[0]
                st.markdown(
                    f"<div style='background:#1a0f00;border:1px solid #f97316;"
                    f"border-radius:8px;padding:10px 14px;margin-bottom:10px'>"
                    f"<div style='color:#f97316;font-size:.72rem;font-weight:800;"
                    f"text-transform:uppercase;letter-spacing:.08em'>Last Edit</div>"
                    f"<div style='color:#e2e8f0;font-size:.86rem;margin-top:3px'>"
                    f"<b>{_eh.get('changed_by_name') or 'Staff'}</b>"
                    f"<span style='color:#94a3b8;margin-left:8px'>{str(_eh.get('changed_at') or '')[:16]}</span>"
                    f"</div><div style='color:#cbd5e1;font-size:.82rem;margin-top:4px'>"
                    f"{_eh.get('remarks') or ''}</div></div>",
                    unsafe_allow_html=True,
                )
        except Exception:
            pass
        _render_cancel_panel(order, _oid, _ono, _status)
        st.markdown("---")

    lines = _load_lines(_oid)

    # ── Payment summary (advance paid + balance due) ──────────────────────
    if not _is_consult:
        try:
            from modules.sql_adapter import run_query as _rq_adv_bo
            _adv_bo = _rq_adv_bo(
                "SELECT COALESCE(SUM(amount),0) AS total_adv "
                "FROM payments "
                "WHERE advance_for_order_id = %s::uuid "
                "  AND payment_type = 'ADVANCE' "
                "  AND COALESCE(is_deleted, FALSE) = FALSE",
                (_oid,)
            ) or []
            _adv_paid  = round(float((_adv_bo[0]["total_adv"] if _adv_bo else 0) or 0), 2)
            _order_val = round(float(order.get("total_value") or 0), 2)
            _bal_due   = round(max(_order_val - _adv_paid, 0), 2)
            if _adv_paid > 0 or _order_val > 0:
                _pm1, _pm2, _pm3 = st.columns(3)
                _pm1.metric("Order Value",      f"₹{_order_val:,.2f}")
                _pm2.metric("Previously Paid",  f"₹{_adv_paid:,.2f}",
                            delta=f"{'Fully Paid' if _bal_due <= 0 else f'Balance ₹{_bal_due:,.2f}'}",
                            delta_color="normal" if _bal_due <= 0 else "inverse")
                _pm3.metric("Balance Due",       f"₹{_bal_due:,.2f}")
        except Exception:
            pass
        st.markdown("---")

    # ── Consultation order — special view ────────────────────────────────
    if _is_consult:
        # Show persistent already-billed error if set by convert button
        _ab2 = st.session_state.pop("_consult_already_billed", None)
        if _ab2:
            st.error(
                f"⛔ Already converted to billing order "
                f"({'Order: ' + _ab2 if _ab2 != 'see Retail Orders' else 'check Retail Orders'}). "
                f"Cannot convert again.",
                icon="⛔"
            )
        # Show consultation details + print option
        st.markdown(
            "<div style='background:#0f172a;border-left:4px solid #10b981;"
            "padding:10px 14px;border-radius:6px;margin-bottom:10px'>"
            "<b style='color:#10b981'>🩺 Consultation Visit</b>"
            "<span style='color:#94a3b8;font-size:0.78rem;margin-left:10px'>"
            "Examination only — no product sale</span></div>",
            unsafe_allow_html=True
        )
        # ── Patient header ─────────────────────────────────────────────
        _pname = str(order.get("patient_name","—") or "—")
        _pmob  = str(order.get("patient_mobile","—") or "—")
        _pdate = _fmt_date(order.get("created_at"))
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
            f"border-radius:6px;padding:8px 12px;margin-bottom:6px'>"
            f"<div style='color:#60a5fa;font-weight:700;font-size:.9rem'>"
            f"👤 {_pname}</div>"
            f"<div style='color:#94a3b8;font-size:.75rem;margin-top:2px'>"
            f"📱 {_pmob}  ·  📅 {_pdate}</div>"
            f"</div>",
            unsafe_allow_html=True
        )

        # ── R / L Power summary ────────────────────────────────────────────
        _rx_r = {}; _rx_l = {}   # always safe — overwritten if DB succeeds
        try:
            from modules.sql_adapter import run_query as _rq_rx
            _rx_rows = []
            _pid_order   = str(order.get("party_id") or order.get("patient_id") or "")
            # PRIORITY 1: exact visit_id stored in customer_order_no at consultation save
            _visit_id_stored = str(order.get("customer_order_no","") or "")
            if _visit_id_stored and len(_visit_id_stored) == 36:
                _rx_rows = _rq_rx("""
                    SELECT right_sph, right_cyl, right_axis, right_add,
                           left_sph,  left_cyl,  left_axis,  left_add
                    FROM   patient_visits
                    WHERE  id = %s::uuid
                    LIMIT  1
                """, (_visit_id_stored,)) or []

            # PRIORITY 2: latest visit for this patient (patient_id on order)
            if not _rx_rows and _pid_order and len(_pid_order) > 10:
                _rx_rows = _rq_rx("""
                    SELECT right_sph, right_cyl, right_axis, right_add,
                           left_sph,  left_cyl,  left_axis,  left_add
                    FROM   patient_visits
                    WHERE  patient_id = %s::uuid
                    ORDER  BY visit_date DESC, created_at DESC LIMIT 1
                """, (_pid_order,)) or []

            # PRIORITY 3: name match fallback (old records without patient_id)
            if not _rx_rows and _pname and _pname != "—":
                _rx_rows = _rq_rx("""
                    SELECT pv.right_sph, pv.right_cyl, pv.right_axis, pv.right_add,
                           pv.left_sph,  pv.left_cyl,  pv.left_axis,  pv.left_add
                    FROM   patient_visits pv
                    JOIN   patients pt ON pt.id = pv.patient_id
                    WHERE  pt.master_name ILIKE %s
                    ORDER  BY pv.visit_date DESC, pv.created_at DESC LIMIT 1
                """, (_pname,)) or []

            if _rx_rows:
                _rx = _rx_rows[0]
                _rx_r = {"sph": _rx.get("right_sph"), "cyl": _rx.get("right_cyl"),
                          "axis": _rx.get("right_axis"), "add": _rx.get("right_add")}
                _rx_l = {"sph": _rx.get("left_sph"),  "cyl": _rx.get("left_cyl"),
                          "axis": _rx.get("left_axis"),  "add": _rx.get("left_add")}
        except Exception:
            pass

        def _pwr(v):
            try:
                f = float(v or 0)
                return f"{f:+.2f}" if f != 0 else "Plano"
            except: return "—"
        def _ax(v):
            try: return f"{int(v or 0)}°"
            except: return "—"

        def _power_card(eye_label, color, rx):
            sph  = _pwr(rx.get("sph"))
            cyl  = _pwr(rx.get("cyl"))
            axis = _ax(rx.get("axis"))
            add  = _pwr(rx.get("add")) if rx.get("add") else ""
            add_txt = f"  ADD {add}" if add and add != "+0.00" else ""
            has_data = any(rx.values())
            return (
                f"<div style='background:#0a1628;border-left:3px solid {color};"
                f"border-radius:4px;padding:6px 10px;margin-bottom:4px'>"
                f"<div style='color:{color};font-weight:700;font-size:.78rem'>"
                f"👁 {eye_label}</div>"
                + (
                    f"<div style='color:#e2e8f0;font-size:.82rem;margin-top:2px'>"
                    f"SPH <b>{sph}</b>  CYL <b>{cyl}</b>  AX <b>{axis}</b>{add_txt}</div>"
                    if has_data else
                    f"<div style='color:#475569;font-size:.78rem'>No power recorded</div>"
                ) +
                f"</div>"
            )

        st.markdown(
            _power_card("RIGHT EYE", "#f97316", _rx_r) +
            _power_card("LEFT EYE",  "#3b82f6", _rx_l),
            unsafe_allow_html=True
        )

        if _is_genuinely_converted:
            with st.expander("🩺 Modify clinical notes only", expanded=False):
                st.caption("RX power and billing are locked. This section saves only clinical findings for the same visit.")
                try:
                    _pid_cl = str(order.get("party_id") or order.get("patient_id") or "")
                    _vid_cl = str(order.get("customer_order_no") or "")
                    if _pid_cl and len(_pid_cl) > 10:
                        st.session_state["retail_patient_id"] = _pid_cl
                        st.session_state["retail_patient_name"] = _pname
                        st.session_state["retail_patient_mobile"] = "" if _pmob == "—" else _pmob
                    if _vid_cl and len(_vid_cl) == 36:
                        st.session_state["retail_selected_visit_id"] = _vid_cl
                    from modules.clinical_exam import (
                        load_clinical_examination as _load_clinical_oev,
                        render_clinical_examination as _render_clinical_oev,
                    )
                    _clinical_ctx = f"{_pid_cl}:{_vid_cl}"
                    if _pid_cl and len(_pid_cl) > 10 and st.session_state.get("_oev_clinical_ctx") != _clinical_ctx:
                        _load_clinical_oev(_pid_cl, _vid_cl if len(_vid_cl) == 36 else None)
                        st.session_state["_oev_clinical_ctx"] = _clinical_ctx
                    _render_clinical_oev()
                except Exception as _ce:
                    st.error(f"Clinical edit unavailable: {_ce}")

        # ── Product lines if any ───────────────────────────────────────────
        try:
            from modules.sql_adapter import run_query as _rq_ol
            _ol = _rq_ol("""
                SELECT COALESCE(p.product_name,
                       (ol.lens_params::jsonb->>'display_product_name'),
                       'Item') AS product_name,
                       ol.eye_side, ol.quantity, ol.total_price
                FROM   order_lines ol
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE  ol.order_id = %s::uuid
                  AND  COALESCE(ol.is_deleted, FALSE) = FALSE
                ORDER  BY ol.eye_side
            """, (_oid,))
            if _ol:
                st.caption("📦 Items:")
                for _li in _ol[:4]:
                    _ep = f" [{_li.get('eye_side','')}]" if _li.get("eye_side") else ""
                    st.caption(
                        f"  • {_li.get('product_name','?')}{_ep}  ×{_li.get('quantity',1)}"
                        f"  ₹{float(_li.get('total_price',0) or 0):,.0f}"
                    )
                if len(_ol) > 4:
                    st.caption(f"  … +{len(_ol)-4} more items")
        except Exception:
            pass

        # ── Print & WhatsApp actions ──────────────────────────────────────
        _act_col1, _act_col2, _act_col3, _act_col4 = st.columns(4)
        with _act_col1:
            if st.button("🖨️ Re-print Clinical Report",
                         key=f"reprint_consult_{_oid[:8]}",
                         use_container_width=True):
                try:
                    from modules.consultation import _print_clinical_report
                    from modules.settings.shop_master import get_unit_info
                    _si   = get_unit_info("retail")
                    _addr = ", ".join(filter(None, [
                        _si.get("shop_address",""), _si.get("shop_address2",""),
                        _si.get("shop_city",""), _si.get("shop_state",""),
                    ]))
                    # Barcode — prefer party_id, fallback name lookup
                    _pb = ""
                    try:
                        from modules.printing.patient_card_printer import ensure_patient_id
                        _pb_pid = str(order.get("party_id") or "")
                        if _pb_pid and len(_pb_pid) > 10:
                            _pb = ensure_patient_id(_pb_pid)
                        elif _pname and _pname != "—":
                            from modules.sql_adapter import run_query as _rq_pb
                            _pb_row = _rq_pb(
                                "SELECT id::text FROM patients WHERE master_name ILIKE %s LIMIT 1",
                                (_pname,)
                            ) or []
                            if _pb_row:
                                _pb = ensure_patient_id(_pb_row[0]["id"])
                    except Exception:
                        pass
                    _clin = {}
                    try:
                        _vid_print = str(order.get("customer_order_no") or "")
                        _pid_print = str(order.get("party_id") or order.get("patient_id") or "")
                        if _pid_print and len(_pid_print) > 10:
                            from modules.sql_adapter import run_query as _rq_clp
                            _params_clp = {"pid": _pid_print}
                            _sql_clp = "SELECT * FROM patient_clinicals WHERE patient_id=%(pid)s::uuid"
                            if _vid_print and len(_vid_print) == 36:
                                _sql_clp += " AND visit_id=%(vid)s::uuid"
                                _params_clp["vid"] = _vid_print
                            _sql_clp += " ORDER BY created_at DESC LIMIT 1"
                            _clin_rows = _rq_clp(_sql_clp, _params_clp) or []
                            _clin = dict(_clin_rows[0]) if _clin_rows else {}
                    except Exception:
                        _clin = {}

                    # Build RX tuples from already-fetched _rx_r / _rx_l dicts
                    def _rv(d, k): return d.get(k) if d.get(k) is not None else ""
                    _print_clinical_report(
                        name=_pname,
                        mobile=_pmob,
                        date=_pdate,
                        shop=_si.get("shop_name", "DV Optical"),
                        addr=_addr,
                        phone=_si.get("shop_phone", ""),
                        rx_r=(_rv(_rx_r,"sph"), _rv(_rx_r,"cyl"), _rv(_rx_r,"axis"), _rv(_rx_r,"add")),
                        rx_l=(_rv(_rx_l,"sph"), _rv(_rx_l,"cyl"), _rv(_rx_l,"axis"), _rv(_rx_l,"add")),
                        va_unaided=(_clin.get("va_distance_unaided_r",""), _clin.get("va_distance_unaided_l","")),
                        va_aided=(_clin.get("va_distance_aided_r",""), _clin.get("va_distance_aided_l","")),
                        va_near=(_clin.get("va_near_r",""), _clin.get("va_near_l","")),
                        lids=_clin.get("sle_lids",""),
                        conjunctiva=_clin.get("sle_conjunctiva",""),
                        cornea=_clin.get("sle_cornea",""),
                        ac=_clin.get("sle_ac",""),
                        iris=_clin.get("sle_iris",""),
                        lens=_clin.get("sle_lens",""),
                        vitreous=_clin.get("sle_vitreous",""),
                        fundus=_clin.get("sle_fundus",""),
                        iop_r=_clin.get("iop_r",""),
                        iop_l=_clin.get("iop_l",""),
                        ortho_dist=_clin.get("ortho_cover_test_distance",""),
                        ortho_near=_clin.get("ortho_cover_test_near",""),
                        nystagmus=_clin.get("ortho_nystagmus",""),
                        motility=_clin.get("ortho_ocular_motility",""),
                        convergence=_clin.get("ortho_convergence",""),
                        remarks=_clin.get("ortho_remarks",""),
                        doctor_notes=_clin.get("doctor_notes",""),
                        treatment_plan=_clin.get("treatment_plan",""),
                        followup_advice=_clin.get("followup_advice",""),
                        fee=float(order.get("total_value", 0)),
                        pay_mode=order.get("payment_mode", "Cash"),
                        patient_barcode=_pb,
                        footer=_si.get("print_footer", ""),
                    )
                except Exception as _pe:
                    st.error(f"Print error: {_pe}")

        with _act_col2:
            if st.button("🧾 Print Receipt",
                         key=f"receipt_consult_{_oid[:8]}",
                         use_container_width=True):
                try:
                    from modules.consultation import _print_consultation_receipt
                    from modules.settings.shop_master import get_unit_info
                    _si = get_unit_info("retail")
                    _addr = ", ".join(filter(None, [
                        _si.get("shop_address",""), _si.get("shop_address2",""),
                        _si.get("shop_city",""), _si.get("shop_state",""),
                    ]))
                    def _rv(d, k): return d.get(k) if d.get(k) is not None else ""
                    _print_consultation_receipt(
                        order_no=order.get("order_no","—"),
                        patient_name=_pname,
                        mobile="" if _pmob == "—" else _pmob,
                        consult_type="Consultation",
                        fee=float(order.get("total_value") or 0),
                        pay_mode=order.get("payment_mode", "Cash"),
                        visit_date=_pdate,
                        shop=_si.get("shop_name", "DV Optical"),
                        addr=_addr,
                        phone=_si.get("shop_phone", ""),
                        rx_r=(_rv(_rx_r,"sph"), _rv(_rx_r,"cyl"), _rv(_rx_r,"axis"), _rv(_rx_r,"add")),
                        rx_l=(_rv(_rx_l,"sph"), _rv(_rx_l,"cyl"), _rv(_rx_l,"axis"), _rv(_rx_l,"add")),
                    )
                except Exception as _re:
                    st.error(f"Receipt print error: {_re}")

        with _act_col3:
            try:
                from modules.printing.patient_card_printer import render_patient_card_buttons
                _pid_card = str(order.get("party_id") or order.get("patient_id") or "")
                if _pid_card and len(_pid_card) > 10:
                    render_patient_card_buttons(
                        patient_id=_pid_card,
                        patient_name=_pname,
                        mobile="" if _pmob == "—" else _pmob,
                        rx_r=_rx_r,
                        rx_l=_rx_l,
                        visit_date=_pdate,
                    )
                else:
                    st.button("🪪 Patient Card", disabled=True, use_container_width=True,
                              key=f"pcard_consult_dis_{_oid[:8]}",
                              help="No linked patient master")
            except Exception as _pce:
                st.caption(f"Patient card unavailable: {_pce}")

        with _act_col4:
            # WhatsApp — build message from DB data, then allow saving missing mobile.
            try:
                from modules.settings.shop_master import get_unit_info as _g
                _ws = _g("retail")
                _wstore = _ws.get("shop_name","Parakh Eye Care")
                _wphone = _ws.get("shop_phone","")
            except Exception:
                _wstore, _wphone = "Parakh Eye Care", ""

            def _wfmt(v):
                try:
                    f = float(v or 0)
                    return f"{f:+.2f}" if f != 0 else "Plano"
                except: return "—"
            def _wax(v):
                try: return f"{int(float(v or 0))}°"
                except: return "—"

            _wr = (f"R: SPH {_wfmt(_rx_r.get('sph'))} CYL {_wfmt(_rx_r.get('cyl'))} "
                   f"AX {_wax(_rx_r.get('axis'))}" if _rx_r else "R: —")
            _wl = (f"L: SPH {_wfmt(_rx_l.get('sph'))} CYL {_wfmt(_rx_l.get('cyl'))} "
                   f"AX {_wax(_rx_l.get('axis'))}" if _rx_l else "L: —")
            _c_ono = order.get("order_no","—")
            _c_fee = float(order.get("total_value") or 0)
            _wa_text = (
                f"Thanks for visiting {_wstore}.\n\n"
                f"Consultation ID: *{_c_ono}*\n"
                f"Patient: *{_pname}*\n\n"
                f"*Your Prescription:*\n{_wr}\n{_wl}\n\n"
                + (f"Consultation fee: *₹{_c_fee:.0f}*\n\n" if _c_fee > 0 else "")
                + "Your prescription is valid for one year from the date of examination."
                + (f"\n\nStore our number: {_wphone}" if _wphone else "")
            )
            try:
                from modules.wa_contact_tools import render_mobile_field
                from modules.wa_hub import wa_link
                _wa_mob = render_mobile_field(
                    f"oev_consult_rx_{_oid[:8]}",
                    name=_pname,
                    mobile="" if _pmob == "—" else _pmob,
                    order_id=_oid,
                    label="WhatsApp mobile",
                )
                _wa_url = wa_link(_wa_mob, _wa_text)
            except Exception:
                import urllib.parse as _uparse
                _wa_mob_raw = "".join(x for x in (_pmob or "") if x.isdigit())
                if _wa_mob_raw.startswith("91") and len(_wa_mob_raw) == 12:
                    _wa_mob_raw = _wa_mob_raw[2:]
                elif _wa_mob_raw.startswith("0") and len(_wa_mob_raw) == 11:
                    _wa_mob_raw = _wa_mob_raw[1:]
                _wa_mob_e164 = ("91" + _wa_mob_raw) if (len(_wa_mob_raw) == 10 and _wa_mob_raw[0] in "6789") else ""
                _wa_url = f"https://wa.me/{_wa_mob_e164}?text={_uparse.quote(_wa_text)}" if _wa_mob_e164 else ""
            if _wa_url:
                st.link_button("📲 WhatsApp RX", _wa_url, use_container_width=True)
            else:
                st.button("📲 WhatsApp RX", disabled=True,
                          key=f"wa_consult_disabled_{_oid[:8]}",
                          use_container_width=True,
                          help="Enter and save a valid mobile number")

        with st.expander("📄 Referral letter", expanded=False):
            _ref_saved = ""
            _ref_reason_saved = ""
            try:
                from modules.sql_adapter import run_query as _rq_ref_load
                _ref_row = _rq_ref_load(
                    "SELECT COALESCE(extra_data::json->>'referral','') AS referral, "
                    "       COALESCE(extra_data::json->>'referral_reason','') AS reason "
                    "FROM orders WHERE id=%(oid)s::uuid LIMIT 1",
                    {"oid": _oid},
                ) or []
                if _ref_row:
                    _ref_saved = str(_ref_row[0].get("referral") or "")
                    _ref_reason_saved = str(_ref_row[0].get("reason") or "")
            except Exception:
                pass

            _ref_to = st.text_input(
                "Refer to",
                value=_ref_saved,
                placeholder="Doctor / hospital / specialist",
                key=f"oev_ref_to_{_oid[:8]}",
            ).strip()
            _ref_reason = st.text_area(
                "Reason for referral",
                value=_ref_reason_saved,
                placeholder="Reason that should appear on the referral letter",
                key=f"oev_ref_reason_{_oid[:8]}",
                height=90,
            ).strip()

            _ref_c1, _ref_c2 = st.columns(2)
            with _ref_c1:
                if st.button("💾 Save Referral", key=f"oev_ref_save_{_oid[:8]}", use_container_width=True):
                    try:
                        from modules.sql_adapter import run_write as _rw_ref_save
                        import json as _json_ref_save
                        _rw_ref_save(
                            "UPDATE orders "
                            "SET extra_data = COALESCE(extra_data,'{}'::jsonb) || %(payload)s::jsonb "
                            "WHERE id=%(oid)s::uuid",
                            {
                                "oid": _oid,
                                "payload": _json_ref_save.dumps({
                                    "referral": _ref_to,
                                    "referral_reason": _ref_reason,
                                }),
                            },
                        )
                        st.success("Referral saved.")
                    except Exception as _rse:
                        st.error(f"Referral save failed: {_rse}")

            with _ref_c2:
                if st.button("📄 Print Referral", key=f"oev_ref_print_{_oid[:8]}", use_container_width=True):
                    if not _ref_to:
                        st.warning("Enter referral doctor / hospital first.")
                    else:
                        try:
                            from modules.consultation import _print_referral_letter
                            from modules.settings.shop_master import get_unit_info
                            _si = get_unit_info("retail")
                            _addr = ", ".join(filter(None, [
                                _si.get("shop_address",""), _si.get("shop_address2",""),
                                _si.get("shop_city",""), _si.get("shop_state",""),
                            ]))
                            _clin = {}
                            try:
                                _vid_ref = str(order.get("customer_order_no") or "")
                                _pid_ref = str(order.get("party_id") or order.get("patient_id") or "")
                                if _pid_ref and len(_pid_ref) > 10:
                                    from modules.sql_adapter import run_query as _rq_ref_clin
                                    _params_ref = {"pid": _pid_ref}
                                    _sql_ref = "SELECT * FROM patient_clinicals WHERE patient_id=%(pid)s::uuid"
                                    if _vid_ref and len(_vid_ref) == 36:
                                        _sql_ref += " AND visit_id=%(vid)s::uuid"
                                        _params_ref["vid"] = _vid_ref
                                    _sql_ref += " ORDER BY created_at DESC LIMIT 1"
                                    _clin_rows_ref = _rq_ref_clin(_sql_ref, _params_ref) or []
                                    _clin = dict(_clin_rows_ref[0]) if _clin_rows_ref else {}
                            except Exception:
                                _clin = {}

                            _pb_ref = ""
                            try:
                                from modules.printing.patient_card_printer import ensure_patient_id
                                _pid_ref_bc = str(order.get("party_id") or order.get("patient_id") or "")
                                if _pid_ref_bc and len(_pid_ref_bc) > 10:
                                    _pb_ref = ensure_patient_id(_pid_ref_bc)
                            except Exception:
                                pass

                            def _rv(d, k): return d.get(k) if d.get(k) is not None else ""
                            _print_referral_letter(
                                name=_pname,
                                mobile="" if _pmob == "—" else _pmob,
                                date=_pdate,
                                shop=_si.get("shop_name", "DV Optical"),
                                addr=_addr,
                                phone=_si.get("shop_phone", ""),
                                rx_r=(_rv(_rx_r,"sph"), _rv(_rx_r,"cyl"), _rv(_rx_r,"axis"), _rv(_rx_r,"add")),
                                rx_l=(_rv(_rx_l,"sph"), _rv(_rx_l,"cyl"), _rv(_rx_l,"axis"), _rv(_rx_l,"add")),
                                va_unaided=(_clin.get("va_distance_unaided_r",""), _clin.get("va_distance_unaided_l","")),
                                va_aided=(_clin.get("va_distance_aided_r",""), _clin.get("va_distance_aided_l","")),
                                lids=_clin.get("sle_lids",""),
                                cornea=_clin.get("sle_cornea",""),
                                lens=_clin.get("sle_lens",""),
                                fundus=_clin.get("sle_fundus",""),
                                iop_r=_clin.get("iop_r",""),
                                iop_l=_clin.get("iop_l",""),
                                remarks=_clin.get("ortho_remarks","") or _clin.get("doctor_notes",""),
                                referral=_ref_to,
                                referral_reason=_ref_reason,
                                patient_barcode=_pb_ref,
                                footer=_si.get("print_footer", ""),
                            )
                        except Exception as _rpe:
                            st.error(f"Referral print error: {_rpe}")

        # ── Linked retail order (if consultation was used to create one) ───

        # Check if a retail order was already raised from this consultation
        try:
            from modules.sql_adapter import run_query as _rq_cb
            # Tier 0: is_converted flag — add column if missing, then check
            try:
                _rq_cb("ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_converted BOOLEAN DEFAULT FALSE")
            except Exception:
                pass
            try:
                _conv_flag = _rq_cb(
                    "SELECT order_no FROM orders WHERE id=%s::uuid "
                    "AND COALESCE(is_converted,false)=true LIMIT 1",
                    (_oid,)
                ) or []
                if _conv_flag:
                    _billed_check = [{"order_no": "see Retail Orders"}]
                else:
                    _billed_check = None  # proceed to full check
            except Exception:
                _conv_flag = []
                _billed_check = None
            if _billed_check is None:
                # Only match via explicit customer_order_no link — never match by party_id+date
                # because any existing retail order for this patient would hide the Convert button.
                _billed_check = _rq_cb("""
                    SELECT o2.order_no FROM orders o2
                    JOIN orders o1 ON o1.id = %(cid)s::uuid
                    WHERE o2.order_type IN ('RETAIL','WHOLESALE')
                      AND COALESCE(o2.is_deleted, false) = false
                      AND o2.customer_order_no = o1.id::text
                    ORDER BY o2.created_at ASC LIMIT 1
                """, {"cid": _oid}) or []
        except Exception:
            _billed_check = []

        st.markdown("---")

        # Show linked retail order if consultation was used to create one
        if _billed_check:
            _linked_ono = _billed_check[0].get("order_no","—")
            st.markdown(
                f"<div style='background:#0d2818;border:2px solid #22c55e;"
                f"border-radius:8px;padding:12px 16px;margin-top:8px'>"
                f"<div style='font-size:0.78rem;font-weight:800;color:#4ade80;"
                f"text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px'>"
                f"🔄 Converted to Full Billing</div>"
                f"<div style='font-size:1rem;font-weight:700;color:#e2e8f0'>"
                f"{_linked_ono}</div>"
                f"<div style='font-size:0.72rem;color:#6ee7b7;margin-top:4px'>"
                f"All further edits must be done through the Retail Order above.</div>"
                f"</div>",
                unsafe_allow_html=True
            )
            # Direct link to the retail order in Order View
            if st.button(
                f"📋 Open {_linked_ono}",
                key=f"open_linked_{_oid[:8]}",
                use_container_width=True,
                type="secondary",
            ):
                # Open the linked retail order in order view
                try:
                    from modules.sql_adapter import run_query as _rq_lnk2
                    _lnk_id_row = _rq_lnk2(
                        "SELECT id::text FROM orders WHERE order_no=%s AND COALESCE(is_deleted,FALSE)=FALSE LIMIT 1",
                        (_linked_ono,)
                    ) or []
                    if _lnk_id_row:
                        _lnk_id = _lnk_id_row[0]["id"]
                        st.session_state[f"oev_open_{_lnk_id}"] = _lnk_id
                        st.session_state["_oev_land_on_consult"] = False
                        st.rerun()
                except Exception:
                    pass

    if not lines and not _is_consult:
        st.warning("No lines found for this order.")
    elif not _is_consult:
        # ── Lines table ────────────────────────────────────────────────
        st.markdown("<div style='color:#60a5fa;font-size:0.7rem;font-weight:700;"
                    "letter-spacing:.08em;margin-bottom:6px'>LINE ITEMS</div>",
                    unsafe_allow_html=True)

        for ln in lines:
            _lid  = str(ln.get("line_id") or "")
            _eye  = str(ln.get("eye_side") or "—")
            _pn   = ln.get("product_name") or "—"
            _br   = ln.get("brand") or ""
            _lp   = _parse_lp(ln.get("lens_params"))

            # Build display name with coating/index suffix from lens_params
            _disp_suffix = str(_lp.get("display_suffix") or "").strip()
            _coating_disp = str(_lp.get("coating") or _lp.get("coating_type") or "").strip()
            _index_disp   = str(_lp.get("lens_index") or _lp.get("index_value") or
                                _lp.get("index") or "").strip()
            if _disp_suffix:
                # Use stored display suffix (e.g. "+ 1.50 UltraHC")
                _pn_display = f"{_pn} {_disp_suffix}" if _disp_suffix not in _pn else _pn
            elif _coating_disp or _index_disp:
                _parts = []
                if _index_disp: _parts.append(f"Idx {_index_disp}")
                if _coating_disp: _parts.append(_coating_disp)
                _pn_display = f"{_pn} ({' · '.join(_parts)})"
            else:
                _pn_display = _pn

            _colour = str(_lp.get("colour") or "")
            _colour = "" if _colour.lower() in ("none","no","") else _colour
            _fit    = bool(_lp.get("fitting_required"))
            _instruct = str(_lp.get("instructions") or "")

            _sph = f"{float(ln['sph']):+.2f}" if ln.get("sph") is not None else "—"
            _cyl = f"{float(ln['cyl']):+.2f}" if ln.get("cyl") is not None else "—"
            _ax  = str(int(ln.get("axis") or 0)) + "°" if ln.get("axis") else "—"
            _add = f"{float(ln['add_power']):+.2f}" if ln.get("add_power") else "—"

            _eye_col = "#4ade80" if _eye == "R" else "#60a5fa"

            with st.container(border=True):
                _lc1, _lc2, _lc3 = st.columns([0.4, 3, 2])
                with _lc1:
                    st.markdown(
                        f"<div style='background:{_eye_col}22;color:{_eye_col};"
                        f"font-weight:900;font-size:1rem;text-align:center;"
                        f"padding:8px 4px;border-radius:6px'>{_eye}</div>",
                        unsafe_allow_html=True)
                with _lc2:
                    st.markdown(
                        f"<div style='color:#e2e8f0;font-weight:700'>{_pn_display}</div>"
                        f"<div style='color:#64748b;font-size:0.7rem'>{_br}</div>",
                        unsafe_allow_html=True)
                    # Badges
                    _badges = []
                    _coating = str(_lp.get("coating") or _lp.get("coating_type") or "")
                    _index   = str(_lp.get("lens_index") or _lp.get("index_value") or
                                   _lp.get("index") or "")
                    _thickness = str(_lp.get("thickness") or "")
                    _frame   = str(_lp.get("frame_type") or "")
                    if _coating:   _badges.append(f"🔬 {_coating}")
                    if _index:     _badges.append(f"Idx {_index}")
                    if _thickness and _thickness.lower() not in ("regular",""):
                        _badges.append(f"Thick: {_thickness}")
                    if _frame:     _badges.append(f"🖼️ {_frame}")
                    if _colour:    _badges.append(f"🎨 {_colour}")
                    if _fit:       _badges.append(f"🔧 {_lp.get('fitting_type','Fitting')}")
                    if _instruct:  _badges.append(f"📝 {_instruct[:30]}")
                    if _badges:
                        st.caption(" · ".join(_badges))
                with _lc3:
                    st.markdown(
                        f"<div style='font-family:monospace;color:#94a3b8;font-size:0.8rem'>"
                        f"SPH {_sph} &nbsp; CYL {_cyl} &nbsp; AX {_ax}"
                        f"{'&nbsp; ADD ' + _add if _add != '—' else ''}"
                        f"</div>",
                        unsafe_allow_html=True)
                    # Pricing + discount
                    _up   = float(ln.get("unit_price") or 0)
                    _dp   = float(ln.get("discount_percent") or 0)
                    _da   = float(ln.get("discount_amount") or 0)
                    _tot  = float(ln.get("total_price") or 0)
                    _gst  = float(ln.get("gst_amount") or 0)
                    _grand= round(_tot + _gst, 2)
                    _disc_str = (
                        f"<span style='color:#f87171'>−₹{_da:.2f} ({_dp:.0f}%)</span>"
                        if _da > 0 else ""
                    )
                    st.markdown(
                        f"<div style='font-size:0.75rem;margin-top:4px'>"
                        f"<span style='color:#94a3b8'>₹{_up:.2f}/pc</span>"
                        + (f" {_disc_str}" if _disc_str else "")
                        + f" → <b style='color:#10b981'>₹{_grand:.2f}</b>"
                        f"</div>",
                        unsafe_allow_html=True)

                if editable:
                    if _otype_up in ("RETAIL", "WHOLESALE"):
                        st.caption("✏️ Use 'Open in Punching Screen to Edit' above for product, power, qty, service, and line changes.")
                    else:
                        with st.expander("✏️ Edit RX / Lens Params", expanded=False):
                            _render_line_edit_form(ln, _oid)
                else:
                    # Order confirmed — line is read-only, no edit path
                    st.caption("🔒 Confirmed — line cannot be edited.")

    # ── Edit History — always visible ────────────────────────────────
    try:
        _hist = _rq("""
            SELECT changed_by_name, remarks,
                   COALESCE(changed_at::text, '') AS changed_at
            FROM order_status_history
            WHERE order_id = %(oid)s::uuid
              AND from_status = 'EDIT_SOURCE'
            ORDER BY changed_at DESC NULLS LAST LIMIT 10
        """, {"oid": _oid}) or []
        if _hist:
            st.markdown(
                "<div style='background:#1a0f00;border:1px solid #f97316;"
                "border-radius:8px;padding:10px 14px;margin-top:10px'>"
                "<div style='color:#f97316;font-size:0.7rem;font-weight:700;"
                "text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px'>"
                "📋 Edit History</div>",
                unsafe_allow_html=True
            )
            for _h in _hist:
                _by  = _h.get("changed_by_name","?")
                _at  = str(_h.get("changed_at",""))[:16]
                _rem = _h.get("remarks","")
                st.markdown(
                    f"<div style='border-left:3px solid #f97316;padding:4px 10px;"
                    f"margin:2px 0;font-size:0.8rem'>"
                    f"<b style='color:#fbbf24'>{_by}</b>"
                    f"<span style='color:#94a3b8;font-size:0.75rem;margin-left:8px'>{_at}</span><br>"
                    f"<span style='color:#cbd5e1'>{_rem}</span></div>",
                    unsafe_allow_html=True
                )
            st.markdown("</div>", unsafe_allow_html=True)
    except Exception as _he:
        pass

    # ── Confirmation block — UNDER_REVIEW / PENDING orders ──────────
    if not _is_consult and editable:
        _render_confirm_order_block(order, lines, _oid, str(_ono))

    # ── Mirror + Add line ─────────────────────────────────────────────
    st.markdown("---")
    try:
        # Build a minimal order dict for the adder
        _order_mock = {
            "id": _oid,
            "order_no": order.get("order_no",""),
            "status": _status,
            "lines": lines,
            "stock_lines": lines,
            "inhouse_lines": [],
            "lab_order_lines": [],
        }
        from modules.backoffice.order_line_adder import (
            render_mirror_panel, render_add_line_panel)
        render_mirror_panel(_order_mock)
        render_add_line_panel(_order_mock)
    except Exception as _ae:
        st.caption(f"Add line: {_ae}")


def _render_confirm_order_block(order: Dict, lines: list, oid: str, ono: str):
    """
    Confirmation block shown in backoffice for UNDER_REVIEW / PENDING orders.
    - Shows order total + advance paid + balance due
    - Blocked from confirming if balance > 0 and advance not received
    - Once confirmed → status = CONFIRMED → order is locked
    """
    from modules.sql_adapter import run_query as _rq_cb, run_write as _rw_cb

    _status = str(order.get("status","")).upper()
    if _status not in ("UNDER_REVIEW", "PENDING"):
        return  # Only show for unconfirmed orders

    st.markdown("---")
    st.markdown(
        "<div style='background:#0a1628;border:2px solid #3b82f6;"
        "border-radius:10px;padding:14px 16px;margin:8px 0'>"
        "<b style='color:#60a5fa;font-size:.95rem'>✅ Order Confirmation</b>"
        "<span style='color:#475569;font-size:.78rem;margin-left:10px'>"
        "Review lines above before confirming — once confirmed, edits require Release</span>"
        "</div>",
        unsafe_allow_html=True
    )

    # ── Calculate totals ──────────────────────────────────────────────
    _order_val = float(order.get("total_value") or 0)
    # Recalculate from live lines (more accurate than order header)
    if lines:
        _order_val = round(sum(
            float(l.get("total_price") or 0) for l in lines
            if not l.get("is_deleted")
        ), 2)

    # Advance paid
    _adv_rows = _rq_cb("""
        SELECT COALESCE(SUM(amount),0) AS adv
        FROM payments
        WHERE (order_id = %s::uuid OR advance_for_order_id = %s::uuid)
          AND payment_type IN ('ADVANCE','PAYMENT','RECEIPT')
          AND COALESCE(is_deleted,FALSE)=FALSE
    """, (oid, oid)) or []
    _adv_paid = round(float((_adv_rows[0].get("adv") if _adv_rows else 0) or 0), 2)
    _bal_due  = round(max(_order_val - _adv_paid, 0), 2)

    # ── Summary metrics ───────────────────────────────────────────────
    _m1, _m2, _m3 = st.columns(3)
    _m1.metric("Order Total",    f"₹{_order_val:,.2f}")
    _m2.metric("Advance Received", f"₹{_adv_paid:,.2f}")
    _m3.metric("Balance Due",    f"₹{_bal_due:,.2f}",
               delta="Pending" if _bal_due > 0 else "Fully Paid",
               delta_color="inverse" if _bal_due > 0 else "normal")

    # ── Advance payment guard ─────────────────────────────────────────
    _force_key = f"_confirm_force_{oid[:8]}"
    _can_confirm = True
    _block_msg   = ""

    if _bal_due > 0 and not st.session_state.get(_force_key):
        _can_confirm = False
        _block_msg = (
            f"⚠️ Balance of ₹{_bal_due:,.2f} not yet received. "
            "Collect advance before confirming, or override below."
        )
        st.warning(_block_msg)
        if st.checkbox(
            "✔ Override — confirm without full advance (e.g. credit party)",
            key=f"_confirm_override_{oid[:8]}"
        ):
            st.session_state[_force_key] = True
            st.rerun()
    elif _bal_due > 0:
        st.info(f"Override active — confirming with ₹{_bal_due:,.2f} balance due.")

    # ── Confirm button ────────────────────────────────────────────────
    if _can_confirm:
        _conf_key = f"_confirming_{oid[:8]}"
        if st.session_state.get(_conf_key):
            st.warning(f"Confirm order **{ono}**? This will lock the order.")
            _cy, _cn = st.columns(2)
            with _cy:
                if st.button("✅ Yes — Confirm", type="primary",
                             key=f"conf_yes_{oid[:8]}", width='stretch'):
                    # Update order status + total_value from live lines
                    _rw_cb("""
                        UPDATE orders
                           SET status      = 'CONFIRMED',
                               total_value = %s,
                               updated_at  = NOW()
                         WHERE id = %s::uuid
                    """, (_order_val, oid))
                    # Log status change
                    try:
                        _rw_cb("""
                            INSERT INTO order_status_history
                                (order_id, from_status, to_status,
                                 changed_by_name, remarks)
                            VALUES (%s::uuid, %s, 'CONFIRMED', %s, %s)
                        """, (oid, _status,
                              "Backoffice",
                              f"Confirmed. Order total: Rs {_order_val:,.2f}. Advance: Rs {_adv_paid:,.2f}"))
                    except Exception:
                        pass
                    st.session_state.pop(_conf_key, None)
                    st.session_state.pop(_force_key, None)
                    st.success(f"✅ Order {ono} confirmed!")
                    st.rerun()
            with _cn:
                if st.button("❌ Cancel", key=f"conf_no_{oid[:8]}",
                             width='stretch'):
                    st.session_state.pop(_conf_key, None)
                    st.rerun()
        else:
            if st.button("✅ Confirm Order",
                         type="primary",
                         key=f"conf_btn_{oid[:8]}",
                         width='stretch'):
                st.session_state[_conf_key] = True
                st.rerun()




def _infer_material_from_product_name(product_name: str, current_material: str = "") -> str:
    """First-select material from product naming, but keep staff override editable."""
    cur = str(current_material or "").strip()
    if cur:
        return cur
    name = str(product_name or "").upper()
    # Common shop naming: UV KT Clear / Photochromatic / Blue Block etc.
    if "PHOTO" in name or "PGX" in name:
        return "PHOTOCHROMATIC"
    if "BLUE" in name or "BB" in name or "BLUECUT" in name or "BLUE CUT" in name:
        return "BLUE BLOCK"
    if "TINT" in name or "COLOUR" in name or "COLOR" in name:
        return "TINTED"
    if "CLEAR" in name or "UV" in name or "KT" in name or "KRYPTOK" in name:
        return "CLEAR"
    return cur

def _render_line_edit_form(ln: Dict, order_id: str):
    _lid = str(ln.get("line_id") or "")
    _lp  = _parse_lp(ln.get("lens_params"))
    _bp  = _parse_lp(ln.get("boxing_params"))
    _inferred_material = _infer_material_from_product_name(
        ln.get("product_name") or "",
        _lp.get("material") or _lp.get("blank_material") or _lp.get("treatment") or "",
    )

    def _txt_val(v):
        if v in (None, "", "None"):
            return ""
        try:
            return f"{float(v):g}"
        except Exception:
            return str(v)

    def _float_or_none(v):
        txt = str(v or "").strip()
        if not txt:
            return None
        try:
            return float(txt)
        except Exception:
            return None

    def _axis_or_none(v):
        txt = str(v or "").strip()
        if not txt:
            return None
        try:
            ax = int(float(txt))
            return min(max(ax, 0), 180)
        except Exception:
            return None

    def _fmt_power_for_log(v):
        try:
            return f"{float(v):+.2f}"
        except Exception:
            return ""

    _ea, _eb, _ec, _ed = st.columns(4)
    with _ea:
        _new_sph_raw = st.text_input("SPH", value=_txt_val(ln.get("sph")),
                                     key=f"oev_sph_{_lid}")
    with _eb:
        _new_cyl_raw = st.text_input("CYL", value=_txt_val(ln.get("cyl")),
                                     key=f"oev_cyl_{_lid}")
    with _ec:
        _new_ax_raw = st.text_input("AXIS", value=_txt_val(ln.get("axis")),
                                    key=f"oev_ax_{_lid}")
    with _ed:
        _new_add_raw = st.text_input("ADD", value=_txt_val(ln.get("add_power")),
                                     key=f"oev_add_{_lid}")
    _new_sph = _float_or_none(_new_sph_raw)
    _new_cyl = _float_or_none(_new_cyl_raw)
    _new_ax = _axis_or_none(_new_ax_raw)
    _new_add = _float_or_none(_new_add_raw)
    _new_sph_num = float(_new_sph or 0)
    _new_cyl_num = float(_new_cyl or 0)
    _new_ax_num = int(_new_ax or 0)
    _new_add_num = float(_new_add or 0)

    # Lens params quick edits
    _COLOURS = ["None","Brown 25%","Brown 50%","Brown 75%","Grey 25%","Grey 50%",
                "Grey 75%","Green 50%","Green 75%","Blue 50%","Pink 50%","Other (Manual)"]
    _cur_col = _lp.get("colour","None")
    if _cur_col not in _COLOURS: _cur_col = "None"
    _new_col = st.selectbox("Colour", _COLOURS, index=_COLOURS.index(_cur_col),
                             key=f"oev_col_{_lid}")

    _new_fit = st.checkbox("Fitting Required",
                            value=bool(_lp.get("fitting_required")),
                            key=f"oev_fit_{_lid}")
    _new_inst = st.text_area("Lab Instructions",
                              value=str(_lp.get("instructions") or ""),
                              height=60, key=f"oev_inst_{_lid}")

    # Full lens + boxing parameters: text inputs allow backspace/blank edits.
    st.markdown("**Lens Parameters**")
    _l1, _l2, _l3, _l4 = st.columns(4)
    with _l1:
        _new_index = st.text_input(
            "Index",
            value=str(_lp.get("lens_index") or _lp.get("index") or ""),
            key=f"oev_lp_index_{_lid}",
        )
    with _l2:
        _new_coating = st.text_input(
            "Coating",
            value=str(_lp.get("coating") or ""),
            key=f"oev_lp_coat_{_lid}",
        )
    with _l3:
        _new_treatment = st.text_input(
            "Material / Treatment",
            value=str(_inferred_material or _lp.get("treatment") or ""),
            key=f"oev_lp_treat_{_lid}",
            help="Auto-selected from product name when blank (e.g. UV KT Clear → CLEAR). Staff can change it.",
        )
    with _l4:
        _new_frame_type = st.text_input(
            "Frame Type",
            value=str(_lp.get("frame_type") or ""),
            key=f"oev_lp_frame_{_lid}",
        )

    _l5, _l6, _l7, _l8 = st.columns(4)
    with _l5:
        _new_thickness = st.text_input(
            "Thickness",
            value=str(_lp.get("thickness") or ""),
            key=f"oev_lp_thick_{_lid}",
        )
    with _l6:
        _new_corridor = st.text_input(
            "Corridor",
            value=str(_lp.get("corridor") or ""),
            key=f"oev_lp_corridor_{_lid}",
        )
    with _l7:
        _new_diameter = st.text_input(
            "Diameter",
            value=str(_lp.get("diameter") or ""),
            key=f"oev_lp_diameter_{_lid}",
        )
    with _l8:
        _new_tinted = st.checkbox(
            "Tinted",
            value=bool(_lp.get("tinted")),
            key=f"oev_lp_tinted_{_lid}",
        )

    st.markdown("**Frame / Boxing Measurements**")
    _bvals = {}
    _boxing_fields = [
        ("a_box", "A"), ("b_box", "B"), ("ed", "ED"), ("ed_axis", "ED Axis"),
        ("dbl", "DBL"), ("r_pd", "R PD"), ("l_pd", "L PD"), ("ipd", "IPD"),
        ("fitting_ht_r", "Fit HT R"), ("fitting_ht_l", "Fit HT L"),
        ("panto", "Panto"), ("tilt", "Tilt"), ("bvd", "BVD"),
    ]
    for _row_i in range(0, len(_boxing_fields), 4):
        _cols = st.columns(4)
        for _col, (_field, _label) in zip(_cols, _boxing_fields[_row_i:_row_i + 4]):
            with _col:
                _bvals[_field] = st.text_input(
                    _label,
                    value=_txt_val(_bp.get(_field)),
                    key=f"oev_bp_{_field}_{_lid}",
                )

    # ── Qty + Price edit ──────────────────────────────────────────────
    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
    _qp1, _qp2, _qp3 = st.columns(3)
    with _qp1:
        _new_qty_input = st.number_input(
            "Qty", min_value=1, max_value=9999,
            value=int(ln.get("quantity") or ln.get("billing_qty") or 1),
            step=1, key=f"oev_qty_{_lid}"
        )
    with _qp2:
        _new_price_input = st.number_input(
            "Unit Price ₹", min_value=0.0,
            value=float(ln.get("unit_price") or 0),
            step=0.50, format="%.2f", key=f"oev_price_{_lid}"
        )
    with _qp3:
        _new_disc_input = st.number_input(
            "Discount %", min_value=0.0, max_value=100.0,
            value=float(ln.get("discount_percent") or 0),
            step=0.5, format="%.1f", key=f"oev_disc_{_lid}"
        )
    # Live total preview
    _prev_total = round(_new_price_input * _new_qty_input * (1 - _new_disc_input/100), 2)
    st.caption(f"Total: ₹{_prev_total:,.2f}")

    _btn_save, _btn_del = st.columns([3, 1])

    with _btn_save:
        if st.button("💾 Save Line", type="primary",
                     width='stretch',
                     key=f"oev_save_{_lid}"):
            _lp_new = dict(_lp)
            _lp_new["colour"] = _new_col
            _lp_new["fitting_required"] = _new_fit
            _lp_new["instructions"] = _new_inst
            for _k, _v in {
                "lens_index": _new_index,
                "index": _new_index,
                "coating": _new_coating,
                "treatment": _new_treatment,
                "material": _new_treatment,
                "frame_type": _new_frame_type,
                "thickness": _new_thickness,
                "corridor": _new_corridor,
                "diameter": _new_diameter,
            }.items():
                if str(_v or "").strip():
                    _lp_new[_k] = str(_v).strip()
                else:
                    _lp_new.pop(_k, None)
            _lp_new["tinted"] = bool(_new_tinted)

            _bp_new = {}
            for _field, _label in _boxing_fields:
                _num = _float_or_none(_bvals.get(_field))
                if _num is not None:
                    _bp_new[_field] = _num

            # Check if power changed — if so fetch fresh price from inventory
            _power_changed = (
                _new_sph_num != float(ln.get("sph") or 0) or
                _new_cyl_num != float(ln.get("cyl") or 0) or
                _new_ax_num  != int(ln.get("axis") or 0) or
                abs(float(_new_add or 0) - float(ln.get("add_power") or 0)) > 0.001
            )
            _new_unit_price = None
            if _power_changed:
                try:
                    from modules.sql_adapter import run_query as _rq_price
                    _pid = str(ln.get("product_id") or "")
                    _otype = str(ln.get("order_type") or "RETAIL").upper()
                    if _pid:
                        _inv = _rq_price("""
                            SELECT selling_price, mrp
                            FROM inventory_stock
                            WHERE product_id=%s::uuid
                              AND COALESCE(is_active,true)=true
                            LIMIT 1
                        """, (_pid,)) or []
                        if _inv:
                            _new_unit_price = float(
                                _inv[0].get("mrp" if _otype == "RETAIL" else "selling_price") or
                                _inv[0].get("selling_price") or
                                _inv[0].get("mrp") or 0
                            )
                except Exception:
                    pass

            _new_qty   = _new_qty_input
            _price_up  = _new_unit_price if _new_unit_price else _new_price_input
            _disc_pct  = _new_disc_input

            # ── Re-run discount flow on any edit (qty/price/product change) ──
            # Slab rules depend on qty; brand/product rules depend on product.
            # Shared helper restamps discount + GST + net totals together.
            _edit_line = {}   # initialised here — populated inside try, read in _update_params
            _edit_otype = str(ln.get("order_type") or "RETAIL").upper()
            try:
                from modules.pricing.discount_flow import apply_order_discounts
                _edit_line = {
                    "product_id":   str(ln.get("product_id") or ""),
                    "brand":        str(ln.get("brand") or ""),
                    "main_group":   str(ln.get("main_group") or ""),
                    "unit_price":   _price_up,
                    "billing_qty":  _new_qty,
                    "quantity":     _new_qty,
                    "eye_side":     str(ln.get("eye_side") or ""),
                    "gst_percent":  float(ln.get("gst_percent") or 0),
                    "lens_params":  dict(_lp_new),
                    "sph": _new_sph_num, "cyl": _new_cyl_num,
                }
                # Resolve party_id from order context
                _edit_party_id = str(order_id)  # order_id is line's order_id here
                try:
                    from modules.sql_adapter import run_query as _rq_edit
                    _o_rows = _rq_edit(
                        "SELECT COALESCE(party_id::text,'') AS pid, "
                        "COALESCE(order_type,'WHOLESALE') AS ot, "
                        "COALESCE(party_name,'') AS pname "
                        "FROM orders WHERE id=%s::uuid LIMIT 1",
                        (order_id,)
                    ) or []
                    _edit_otype = "WHOLESALE"
                    if _o_rows:
                        _edit_party_id = _o_rows[0].get("pid", "") or ""
                        _edit_otype    = _o_rows[0].get("ot", "WHOLESALE")
                        if not _edit_party_id:
                            _pname = _o_rows[0].get("pname","")
                            if _pname:
                                _pr = _rq_edit(
                                    "SELECT id::text AS id FROM parties "
                                    "WHERE party_name=%s AND COALESCE(is_active,TRUE)=TRUE LIMIT 1",
                                    (_pname,)
                                ) or []
                                if _pr: _edit_party_id = _pr[0].get("id","")
                    apply_order_discounts([_edit_line], party_id=_edit_party_id, order_type=_edit_otype)
                    _disc_pct = float(_edit_line.get("discount_percent", _disc_pct))
                except Exception:
                    pass  # keep user-entered discount if engine fails
            except Exception:
                pass

            if not _edit_line:
                _edit_line = {
                    "unit_price": _price_up,
                    "billing_qty": _new_qty,
                    "quantity": _new_qty,
                    "discount_percent": _disc_pct,
                    "discount_amount": round(_price_up * _new_qty * _disc_pct / 100, 2),
                    "gst_percent": float(ln.get("gst_percent") or 0),
                    "lens_params": dict(_lp_new),
                }
                try:
                    from modules.pricing.discount_flow import restamp_line_totals
                    restamp_line_totals(_edit_line, _edit_otype)
                except Exception:
                    pass

            _disc_pct  = float(_edit_line.get("discount_percent") or _disc_pct)
            _disc_amt  = float(_edit_line.get("discount_amount") or 0)
            _net_price = float(_edit_line.get("billing_total") or _edit_line.get("total_price") or 0)
            _new_total = _net_price
            _gst_amt   = float(_edit_line.get("gst_amount") or 0)
            _lp_new    = dict(_edit_line.get("lens_params") or _lp_new)

            _update_fields = """
                sph = %(sph)s, cyl = %(cyl)s,
                axis = %(axis)s, add_power = %(add)s,
                lens_params = %(lp)s::jsonb,
                boxing_params = %(bp)s::jsonb,
                quantity = %(qty)s,
                unit_price = %(up)s,
                gst_amount = %(ga)s,
                discount_percent = %(dp)s,
                discount_amount = %(da)s,
                billing_total = %(bt)s,
                total_price = %(tp)s,
                applied_rule_ids = %(ari)s
            """
            _update_params = {
                "sph":  float(_new_sph) if _new_sph is not None else None,
                "cyl":  float(_new_cyl) if _new_cyl is not None else None,
                "axis": int(_new_ax) if _new_ax is not None else None,
                "add":  float(_new_add) if _new_add is not None else None,
                "lp":   _safe_json_text(_lp_new),
                "bp":   _safe_json_text(_bp_new),
                "qty":  _new_qty,
                "up":   _price_up,
                "ga":   _gst_amt,
                "dp":   _disc_pct,
                "da":   _disc_amt,
                "bt":   _net_price,
                "tp":   _new_total,
                "ari":  str(_edit_line.get("applied_rule_ids") or ""),
                "lid":  _lid,
            }

            ok = _write(
                f"UPDATE order_lines SET {_update_fields} WHERE id = %(lid)s::uuid",
                _update_params
            )
            if ok is not False:   # None = success (PostgreSQL UPDATE returns None)
                _sync_pricing_after_edit(order_id)
                try:
                    _write("""
                        INSERT INTO order_status_history
                            (order_id, from_status, to_status, changed_by_name, remarks)
                        VALUES (%(oid)s::uuid, 'EDIT_SOURCE', 'EDIT_SOURCE', %(by)s, %(remarks)s)
                    """, {
                        "oid": order_id,
                        "by": st.session_state.get("user_name", "Backoffice"),
                        "remarks": (
                            f"Line edited: {_lid[:8]} | SPH {_fmt_power_for_log(_new_sph)}, "
                            f"CYL {_fmt_power_for_log(_new_cyl)}, AX {_new_ax or ''}, ADD {_fmt_power_for_log(_new_add)}, "
                            f"Qty {_new_qty}, Total Rs {_new_total:,.2f}"
                        ),
                    })
                except Exception:
                    pass
                if _power_changed and _new_unit_price:
                    st.success(f"✅ Line updated — price refreshed to ₹{_new_unit_price:,.2f} from inventory")
                else:
                    st.success("✅ Line updated")
                st.rerun()

    with _btn_del:
        _billed_qty = int(ln.get("billed_qty") or 0)
        if _billed_qty > 0:
            # Line already on a challan — cannot delete
            st.markdown(
                "<div style='text-align:center;padding:6px 0'>"
                "<span title='Line already billed — raise a Credit Note to reverse'>🔒</span>"
                "</div>",
                unsafe_allow_html=True)
        else:
            _del_key = f"oev_del_confirm_{_lid}"
            if st.session_state.get(_del_key):
                # Confirmation step
                st.warning("Delete this line?")
                _cy, _cn = st.columns(2)
                with _cy:
                    if st.button("✅ Yes", key=f"oev_del_yes_{_lid}",
                                 width='stretch'):
                        ok = _write("""
                            UPDATE order_lines
                            SET is_deleted = TRUE,
                                deleted_at = NOW(),
                                deleted_by = 'backoffice_edit'
                            WHERE id = %(lid)s::uuid
                              AND COALESCE(billed_qty, 0) = 0
                        """, {"lid": _lid})
                        st.session_state.pop(_del_key, None)
                        if ok is not False:
                            _sync_pricing_after_edit(order_id)
                            st.success("🗑️ Line removed")
                            st.rerun()
                        else:
                            st.error("❌ Delete failed — line may already be billed")
                with _cn:
                    if st.button("❌ No", key=f"oev_del_no_{_lid}",
                                 width='stretch'):
                        st.session_state.pop(_del_key, None)
                        st.rerun()
            else:
                if st.button("🗑️", key=f"oev_del_btn_{_lid}",
                             width='stretch',
                             help="Remove this line"):
                    st.session_state[_del_key] = True
                    st.rerun()
