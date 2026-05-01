"""
Production Page
===============
Standalone Streamlit page for production floor staff.

PURPOSE:
  - Production staff only see their own view — no pricing, no billing
  - Lists all open orders that have active job cards
  - Staff can advance stages directly from here
  - Date range filter + per-order stage timeline dropdown
  - Connected to backoffice — status badge shows BOTH order status + production stage

NAVIGATION:
  Add to app.py sidebar as "Production" page.
  Call render_production_page() from app.py router.

DEPENDS ON:
  modules/backoffice/production_panel.py  — renders per-order job tracking
  modules/sql_adapter                     — run_query
"""

import streamlit as st
import datetime
from typing import List, Dict, Optional


# ==================================================
# SESSION STATE
# ==================================================

def _init_production_state():
    defaults = {
        "prod_view_mode":       "list",
        "prod_selected_order":  None,
        "prod_orders":          [],
        "prod_orders_loaded":   False,
        "prod_date_from":       datetime.date.today() - datetime.timedelta(days=30),
        "prod_date_to":         datetime.date.today(),
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
        SELECT id, order_no, patient_name, party_name, status,
               mobile, party_mobile
        FROM orders WHERE order_no = %(ono)s LIMIT 1
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
            except: lp = {}
        bp = lr.get("boxing_params") or {}
        if isinstance(bp, str):
            try: bp = _json.loads(bp)
            except: bp = {}
        sa = lr.get("suggested_allocation")
        if isinstance(sa, str):
            try: sa = _json.loads(sa)
            except: sa = []
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
    "READY_TO_BILL":     "#16a34a",
    "CHALLANED":         "#0284c7",
    "INVOICED":          "#22c55e",
}

try:
    from modules.backoffice.order_status_live import STATUS_META as _OSL_META
    _ORDER_STATUS_COLORS = {k: v["color"] for k, v in _OSL_META.items()}
except Exception:
    _ORDER_STATUS_COLORS = {
        "PENDING": "#64748b", "CONFIRMED": "#3b82f6",
        "IN_PRODUCTION": "#8b5cf6", "READY": "#10b981",
        "BILLED": "#059669", "DISPATCHED": "#0891b2", "DELIVERED": "#10b981",
    }


# ==================================================
# PRODUCTION ORDER LIST
# ==================================================



# ==================================================
# SUPPLIER PIPELINE
# ==================================================


def _render_stock_pipeline():
    """
    Stock pipeline — shows billed stock lines for purchase acknowledgement.

    Flow:
      Stock line sold (billed) → appears here
      → Record purchase (supplier challan + price)
      → Flows to Procurement tab for combined purchase invoice
    """
    import json as _jss
    import urllib.parse as _uparse
    import datetime as _dts

    st.markdown("### 📦 Stock Pipeline")
    st.caption(
        "Stock lines already billed to customers. "
        "Record the purchase (supplier challan + price) to complete procurement flow."
    )

    # ── Filters ──────────────────────────────────────────────────────────────
    with st.container(border=True):
        sf1, sf2, sf3 = st.columns([3, 2, 2])
        with sf1:
            _flt_ord = sf1.text_input("Order/Patient", placeholder="🔍 Order no / patient",
                                      key="stk_flt_ord", label_visibility="collapsed")
        with sf2:
            _flt_from = sf2.date_input("From", value=None, key="stk_flt_from",
                                       label_visibility="collapsed", format="DD/MM/YYYY",
                                       help="Billed from date")
        with sf3:
            _show_acked = sf3.toggle("Show acknowledged", value=False, key="stk_show_acked")

    # ── Query: billed stock lines ─────────────────────────────────────────────
    _where_extra = ""
    _params = {}
    if not _show_acked:
        _where_extra += """
            AND NOT EXISTS (
                SELECT 1 FROM purchase_acknowledgements pa
                WHERE pa.order_line_id = ol.id
                  AND COALESCE(pa.purchase_price, 0) > 0
            )"""
    if _flt_from:
        _where_extra += " AND DATE(o.created_at) >= %(df)s"
        _params["df"] = str(_flt_from)
    if _flt_ord.strip():
        _where_extra += (
            " AND (LOWER(o.order_no) LIKE %(ord)s "
            " OR LOWER(COALESCE(o.patient_name,o.party_name,'')) LIKE %(ord)s)"
        )
        _params["ord"] = f"%{_flt_ord.strip().lower()}%"

    try:
        rows = _q(f"""
            SELECT
                o.id::text                                     AS order_id,
                o.order_no,
                COALESCE(o.patient_name, o.party_name, '—')   AS patient_name,
                o.status,
                o.created_at,
                ol.id::text                                    AS line_id,
                ol.eye_side,
                ol.quantity,
                COALESCE(ol.billed_qty, ol.quantity, 1)        AS billed_qty,
                ol.product_id::text                            AS product_id,
                ol.lens_params,
                p.product_name,
                p.category,
                -- Supplier from purchase_acknowledgements (DB truth) or lens_params
                pa.supplier_id::text                           AS pa_supplier_id,
                COALESCE(pt.party_name,
                         ol.lens_params->>'supplier_name',
                         '')                                   AS supplier_name,
                pa.challan_no                                  AS pa_challan,
                pa.purchase_price                              AS pa_price,
                pa.is_price_locked                             AS pa_locked,
                pa.billing_status                              AS pa_status,
                pa.received_qty                                AS pa_recv_qty,
                -- Inventory purchase price for auto-fill
                -- Try purchase_price first, fallback to purchase_rate
                COALESCE((
                    SELECT COALESCE(NULLIF(s.purchase_price,0), s.purchase_rate, 0)
                    FROM inventory_stock s
                    WHERE s.product_id = ol.product_id
                      AND COALESCE(s.is_active,TRUE) = TRUE
                    ORDER BY s.created_at DESC LIMIT 1
                ), 0) AS inv_purchase_price
            FROM order_lines ol
            JOIN orders  o  ON o.id  = ol.order_id
            JOIN products p ON p.id  = ol.product_id
            -- Only STOCK routed lines
            LEFT JOIN purchase_acknowledgements pa ON pa.order_line_id = ol.id
            LEFT JOIN parties pt ON pt.id = pa.supplier_id
            WHERE (
                      ol.lens_params->>'manufacturing_route' = 'STOCK'
                   OR ol.lens_params->>'batch_status' = 'ALLOCATED'
                   OR (ol.lens_params->>'batch_no' IS NOT NULL
                       AND COALESCE(ol.lens_params->>'manufacturing_route','') != 'INHOUSE')
                  )
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              -- Show billed lines (purchase acknowledgement needed)
              AND EXISTS (
                  SELECT 1 FROM challan_lines cl
                  JOIN challans c ON c.id = cl.challan_id
                  WHERE cl.order_line_id = ol.id
                    AND c.status NOT IN ('CANCELLED','VOID')
              )
            {_where_extra}
            ORDER BY o.created_at DESC, o.order_no, ol.eye_side
        """, _params) or []
    except Exception as _se:
        st.error(f"Could not load stock lines: {_se}")
        return

    if not rows:
        st.info("✅ No stock lines pending purchase acknowledgement.")
        return

    # ── Group by order ────────────────────────────────────────────────────────
    from collections import defaultdict as _dds
    _orders = _dds(lambda: {"info": {}, "lines": []})
    for row in rows:
        _lp = row.get("lens_params") or {}
        if isinstance(_lp, str):
            try: _lp = _jss.loads(_lp)
            except: _lp = {}
        row["_lp"] = _lp
        _orders[row["order_id"]]["info"] = {
            "order_no":     row["order_no"],
            "patient_name": row["patient_name"],
            "status":       row["status"],
            "order_id":     row["order_id"],
        }
        _orders[row["order_id"]]["lines"].append(row)

    _stk_tbl_col, _stk_cap_col = st.columns([1, 8])
    with _stk_tbl_col:
        _stk_table_view = st.toggle("⊞", value=st.session_state.get("stk_tbl_view", True),
                                     key="stk_tbl_view", help="Compact card view")
    with _stk_cap_col:
        st.caption(f"{len(rows)} line(s) · {len(_orders)} order(s)")

    if _stk_table_view:
        # Build groups for shared card renderer
        _stk_groups = {}
        for _sk, _sd2 in _orders.items():
            _sl2 = _sd2["lines"]
            _stk_groups[_sk] = {
                "order_no":   _sd2["info"]["order_no"],
                "patient":    _sd2["info"]["patient_name"],
                "order_id":   _sk,
                "created_at": str(_sl2[0].get("created_at",""))[:10] if _sl2 else "",
                "lines":      _sl2,
            }
            # Attach power from lens_params if available
            for _l2 in _sl2:
                _lp2 = _l2.get("_lp") or {}
                if isinstance(_lp2, dict):
                    for _pk in ("sph","cyl","axis","add_power"):
                        if _lp2.get(_pk) is not None and not _l2.get(_pk):
                            _l2[_pk] = _lp2.get(_pk)
        _render_pipeline_cards(
            groups=_stk_groups,
            route_key="STOCK",
            stage_label_fn=lambda l: "✅ Acked" if float(l.get("pa_price") or 0) > 0 else "⚠️ Pending GRN",
            stage_code_fn=lambda l: "ACKED" if float(l.get("pa_price") or 0) > 0 else "PENDING",
            open_billing_fn=_go_to_billing,
        )
        st.divider()
        st.caption("📋 Purchase Acknowledgement Details:")

    for oid, odata in _orders.items():
        info  = odata["info"]
        lines = odata["lines"]
        _acked = sum(1 for l in lines if l.get("pa_price") and float(l.get("pa_price") or 0) > 0)
        _total = len(lines)
        _all_acked = _acked == _total

        with st.container(border=True):
            # ── Order header ──────────────────────────────────────────────
            _h1, _h2 = st.columns([4, 2])
            with _h1:
                st.markdown(
                    f"<div style='padding:2px 0'>"
                    f"<span style='font-weight:800;color:#e2e8f0'>"
                    f"{'✅' if _all_acked else '📦'} {info['order_no']}</span>"
                    f" <span style='color:#64748b;font-size:0.82rem'>"
                    f"— {info['patient_name']}</span><br>"
                    f"<span style='color:#475569;font-size:0.72rem'>"
                    f"{_acked}/{_total} acknowledged</span></div>",
                    unsafe_allow_html=True
                )
            with _h2:
                _ord_date = str(odata["lines"][0].get("created_at",""))[:10]
                st.caption(f"📅 {_ord_date}")

            # ── Per-line purchase acknowledgement ─────────────────────────
            for line in lines:
                _lid      = str(line["line_id"])
                _eye      = str(line.get("eye_side") or "").upper()
                _pname    = str(line.get("product_name") or "").split(" | ")[0]
                _qty      = int(line.get("billed_qty") or line.get("quantity") or 1)
                _pa_price = float(line.get("pa_price") or 0)
                _pa_locked = bool(line.get("pa_locked"))
                _pa_challan = str(line.get("pa_challan") or "")
                _pa_acked  = _pa_price > 0
                _auto_price = float(line.get("inv_purchase_price") or 0)
                _sup_name  = str(line.get("supplier_name") or "")
                _lp        = line.get("_lp") or {}

                _eye_lbl = f"👁 {_eye}" if _eye in ("R","L") else "🖼"
                _status_color = "#22c55e" if _pa_locked else "#f59e0b" if _pa_acked else "#ef4444"
                _status_label = "🔒 Locked" if _pa_locked else "✅ Acked" if _pa_acked else "⚠️ Pending"

                st.markdown(
                    f"<div style='border:1px solid #1e293b;"
                    f"border-left:4px solid {_status_color};"
                    f"border-radius:6px;padding:8px 12px;margin:4px 0'>"
                    f"<div style='display:flex;justify-content:space-between'>"
                    f"<span style='color:#e2e8f0;font-weight:700'>"
                    f"{_eye_lbl} {_pname}</span>"
                    f"<span style='color:{_status_color};font-size:0.72rem;"
                    f"font-weight:700'>{_status_label}</span></div>"
                    + (f"<div style='color:#64748b;font-size:0.72rem'>"
                       f"Supplier: {_sup_name}  ·  "
                       f"{'Challan: ' + _pa_challan if _pa_challan else 'No challan yet'}"
                       f"{'  ·  ₹' + f'{_pa_price:,.2f}' if _pa_price > 0 else ''}"
                       f"</div>" if _sup_name or _pa_challan else "")
                    + "</div>",
                    unsafe_allow_html=True
                )

                # Purchase record expander
                if not _pa_locked:
                    with st.expander(
                        "📋 Record Purchase" if not _pa_acked
                        else f"📋 Update Purchase — ₹{_pa_price:,.2f}/pc",
                        expanded=not _pa_acked
                    ):
                        # Supplier selector
                        _sup_list = _q("""
                            SELECT id::text AS id, party_name
                            FROM parties
                            WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR')
                              AND COALESCE(is_active,TRUE) = TRUE
                            ORDER BY party_name
                        """, {})
                        if _sup_list:
                            _sup_ids   = [""] + [s["id"] for s in _sup_list]
                            _sup_names = {"": "— Select Supplier —"}
                            _sup_names.update({s["id"]: s["party_name"] for s in _sup_list})
                            # Pre-select if already set
                            _cur_sid = str(line.get("pa_supplier_id") or
                                          _lp.get("supplier_id") or "")
                            _def_si  = _sup_ids.index(_cur_sid) if _cur_sid in _sup_ids else 0
                            _sel_sid = st.selectbox(
                                "Supplier", _sup_ids, index=_def_si,
                                format_func=lambda x: _sup_names.get(x, x),
                                key=f"stk_sup_{_lid}",
                                label_visibility="collapsed"
                            )
                        else:
                            _sel_sid = ""

                        rc1, rc2 = st.columns(2)
                        with rc1:
                            _chal = st.text_input(
                                "Challan / Invoice No.",
                                value=_pa_challan,
                                placeholder="e.g. CH-001 or INV/2025-26/001",
                                key=f"stk_chal_{_lid}",
                                label_visibility="collapsed"
                            )
                            _doc_date = st.date_input(
                                "Date", value=_dts.date.today(),
                                key=f"stk_date_{_lid}",
                                format="DD/MM/YYYY",
                                label_visibility="collapsed"
                            )
                        with rc2:
                            _price = st.number_input(
                                "Purchase Price ₹/pc",
                                value=float(_pa_price or _auto_price),
                                min_value=0.0, step=1.0, format="%.2f",
                                key=f"stk_price_{_lid}",
                                help="Auto-filled from stock master. Edit if different."
                            )
                            _recv_qty = st.number_input(
                                "Qty",
                                value=_qty, min_value=0, max_value=_qty,
                                key=f"stk_recv_{_lid}",
                                label_visibility="collapsed"
                            )

                        if _auto_price > 0 and _price != _auto_price:
                            st.caption(
                                f"ℹ️ Stock master price: ₹{_auto_price:,.2f} · "
                                f"Variance: ₹{_price - _auto_price:+,.2f}"
                            )

                        if st.button("💾 Save Purchase Record",
                                     key=f"stk_save_{_lid}",
                                     type="primary",
                                     use_container_width=True,
                                     disabled=not (_chal.strip())):
                            try:
                                from modules.sql_adapter import run_write as _rw_stk, run_query as _rq_stk
                                # Get product_id and order_id
                                _meta = _rq_stk(
                                    "SELECT product_id::text, order_id::text "
                                    "FROM order_lines WHERE id=%(lid)s::uuid LIMIT 1",
                                    {"lid": _lid}
                                )
                                _prod_id = _meta[0].get("product_id") if _meta else None
                                _ord_id  = _meta[0].get("order_id") if _meta else None
                                _sup_name_save = _sup_names.get(_sel_sid, "") if _sup_list else ""

                                _rw_stk("""
                                    INSERT INTO purchase_acknowledgements (
                                        order_line_id, order_id, order_no,
                                        product_id, product_name, eye_side,
                                        supplier_id, supplier_name,
                                        challan_no, document_date,
                                        qty, received_qty,
                                        purchase_price, total_value,
                                        billing_status, acknowledged_at
                                    ) VALUES (
                                        %(lid)s::uuid, %(oid)s::uuid, %(ono)s,
                                        %(pid)s::uuid, %(pname)s, %(eye)s,
                                        %(sid)s, %(sname)s,
                                        %(chal)s, %(ddate)s::date,
                                        %(qty)s, %(rqty)s,
                                        %(price)s, %(total)s,
                                        'NOT_READY', NOW()
                                    )
                                    ON CONFLICT (order_line_id) DO UPDATE SET
                                        challan_no      = EXCLUDED.challan_no,
                                        document_date   = EXCLUDED.document_date,
                                        supplier_id     = CASE
                                            WHEN EXCLUDED.supplier_id = '00000000-0000-0000-0000-000000000000'::uuid
                                            THEN purchase_acknowledgements.supplier_id
                                            ELSE EXCLUDED.supplier_id END,
                                        supplier_name   = CASE
                                            WHEN EXCLUDED.supplier_name = ''
                                            THEN purchase_acknowledgements.supplier_name
                                            ELSE EXCLUDED.supplier_name END,
                                        received_qty    = EXCLUDED.received_qty,
                                        purchase_price  = CASE
                                            WHEN purchase_acknowledgements.is_price_locked
                                            THEN purchase_acknowledgements.purchase_price
                                            ELSE EXCLUDED.purchase_price END,
                                        total_value     = CASE
                                            WHEN purchase_acknowledgements.is_price_locked
                                            THEN purchase_acknowledgements.total_value
                                            ELSE EXCLUDED.total_value END,
                                        acknowledged_at = NOW()
                                """, {
                                    "lid":   _lid,
                                    "oid":   _ord_id or "00000000-0000-0000-0000-000000000000",
                                    "ono":   info["order_no"],
                                    "pid":   _prod_id or "00000000-0000-0000-0000-000000000000",
                                    "pname": _pname,
                                    "eye":   _eye,
                                    "sid":   _sel_sid if (_sel_sid and len(_sel_sid)==36) else "00000000-0000-0000-0000-000000000000",
                                    "sname": _sup_name_save,
                                    "chal":  _chal.strip(),
                                    "ddate": str(_doc_date),
                                    "qty":   _qty,
                                    "rqty":  _recv_qty,
                                    "price": _price,
                                    "total": round(_price * _recv_qty, 2),
                                })
                                st.success(f"✅ Purchase recorded — {_chal}")
                                st.rerun()
                            except Exception as _se2:
                                st.error(f"Save failed: {_se2}")


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
        except Exception:
            pass
        return "  ".join(parts) if parts else ""


    for _gk, _gd in groups.items():
        _lines    = _gd.get("lines", [])
        _order_no = _gd.get("order_no", "")
        _patient  = _gd.get("patient", "—")
        _oid      = _gd.get("order_id", _gk)
        _date     = str(_gd.get("created_at","") or _lines[0].get("created_at","") if _lines else "")[:10]

        # Stage summary
        _stages   = [stage_code_fn(l) for l in _lines]
        _stg_lbl  = " | ".join(dict.fromkeys(stage_label_fn(l) for l in _lines))

        # Billed check — per this group's lines
        _all_billed_here = False
        _has_invoice_here = False
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
        except Exception:
            pass

        # Billed badge
        if _all_billed_here:
            _bill_label = "🧾 INVOICED" if _has_invoice_here else "📋 CHALLANED"
            _bill_color = "#22c55e" if _has_invoice_here else "#3b82f6"
            _status_html = (
                f"<span style='background:{_bill_color}22;color:{_bill_color};"
                f"font-size:0.68rem;font-weight:700;padding:2px 9px;"
                f"border-radius:8px'>{_bill_label}</span>"
            )
        else:
            _status_html = (
                f"<span style='background:{_accent}22;color:{_accent};"
                f"font-size:0.68rem;font-weight:700;padding:2px 9px;"
                f"border-radius:8px'>{_stg_lbl}</span>"
            )

        # Product+power lines HTML — includes diameter, fitting height, and all lens_params detail
        _prod_html = ""
        for _l in sorted(_lines, key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1):
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
                except: _lp_d = {}
            if isinstance(_bp_d, str):
                try: import json as _jbp; _bp_d = _jbp.loads(_bp_d)
                except: _bp_d = {}

            _detail_bits = []
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

        # Main card
        _detail_key = f"pp_detail_{route_key}_{_gk[:8]}"
        _cc1, _cc2 = st.columns([6, 4])

        with _cc1:
            st.markdown(
                f"<div style='background:{_bg};border:1px solid {_accent}33;"
                f"border-left:4px solid {_accent};border-radius:6px;"
                f"padding:8px 14px;margin-bottom:3px'>"
                f"<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>"
                f"<span style='color:#475569;font-size:0.7rem'>{_date}</span>"
                f"<span style='color:#f1f5f9;font-weight:800;font-size:0.88rem;"
                f"font-family:monospace'>{_order_no}</span>"
                f"<span style='color:#cbd5e1;font-size:0.82rem;font-weight:600'>{_patient}</span>"
                f"{_status_html}"
                f"</div>"
                f"<div style='margin-top:4px;color:#475569;font-size:0.7rem'>"
                f"{_icon} {route_key.replace('_',' ')}</div>"
                f"</div>",
                unsafe_allow_html=True
            )

        with _cc2:
            _ab1, _ab2, _ab3, _ab4 = st.columns([1, 1, 1, 1])
            with _ab1:
                if st.button("👁", key=f"pp_det_{route_key}_{_gk[:8]}",
                             help="Show products & power",
                             use_container_width=True):
                    st.session_state[_detail_key] = not st.session_state.get(_detail_key, False)
                    st.rerun()
            with _ab2:
                if _all_billed_here:
                    st.button("🔒", key=f"pp_lock_{route_key}_{_gk[:8]}",
                              disabled=True, use_container_width=True)
                elif open_billing_fn:
                    if st.button("💰", key=f"pp_bill_{route_key}_{_gk[:8]}",
                                 help="Open billing", type="primary",
                                 use_container_width=True):
                        st.session_state.pop("_billing_blocked_msg", None)
                        open_billing_fn(_oid, _order_no)
            with _ab3:
                if st.button("📋", key=f"pp_jc_{route_key}_{_gk[:8]}",
                             help="Print Job Card (R+L)",
                             use_container_width=True):
                    st.session_state[f"pp_do_jc_{_gk[:8]}"] = True
            with _ab4:
                if st.button("🏷️", key=f"pp_lbl_{route_key}_{_gk[:8]}",
                             help="Print Barcode Labels",
                             use_container_width=True):
                    st.session_state[f"pp_do_lbl_{_gk[:8]}"] = True

        # Show billing blocked message inline (below this card only)
        _bill_msg_key = "_billing_blocked_msg"
        if st.session_state.get(_bill_msg_key):
            st.warning(st.session_state.pop(_bill_msg_key))

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

        # ── Helper: load surfacing_data from lens_params for a line ──
        def _load_surf_lp(ln):
            if not ln: return None
            import json as _lslpj
            _ld = dict(ln)
            if not _ld.get("surfacing_data"):
                _lp2 = _ld.get("lens_params") or {}
                if isinstance(_lp2, str):
                    try: _lp2 = _lslpj.loads(_lp2)
                    except: _lp2 = {}
                _ld["surfacing_data"] = _lp2.get("surfacing_data") or {}
            return _ld

        # ── Trigger: Print Job Card (R+L combined) ──
        if st.session_state.pop(f"pp_do_jc_{_gk[:8]}", False):
            try:
                from modules.backoffice.production_panel import (
                    _build_combined_job_card_html, _open_print_window
                )
                _r_ln = next((l for l in _lines if str(l.get("eye_side","")).upper() in ("R","RIGHT")), None)
                _l_ln = next((l for l in _lines if str(l.get("eye_side","")).upper() in ("L","LEFT")), None)
                _jc_order2 = {"id": _oid, "order_no": _order_no, "patient_name": _patient}
                _open_print_window(
                    _build_combined_job_card_html(
                        _load_surf_lp(_r_ln), _load_surf_lp(_l_ln), _jc_order2
                    )
                )
            except Exception as _jpe2:
                st.error(f"Job card error: {_jpe2}")

        # ── Trigger: Print Barcode Labels ──
        if st.session_state.pop(f"pp_do_lbl_{_gk[:8]}", False):
            try:
                from modules.backoffice.production_panel import (
                    _build_label_page, _open_print_window
                )
                _lbl_lines = []
                for _ll in _lines:
                    _lld = _load_surf_lp(_ll)
                    if _lld: _lbl_lines.append(_lld)
                _lb_ord = {"id": _oid, "order_no": _order_no, "patient_name": _patient,
                           "party_name": _gd.get("lines",[{}])[0].get("party_name","") if _lines else "",
                           "order_type": _lines[0].get("order_type","RETAIL") if _lines else "RETAIL"}
                _open_print_window(_build_label_page(_lbl_lines, _lb_ord))
            except Exception as _lpe2:
                st.error(f"Label error: {_lpe2}")

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
    Gates:
      1. Inhouse jobs must be closed before billing.
      2. Contact lenses and solutions must have purchase recorded before billing.
    """
    try:
        from modules.sql_adapter import run_query as _rq_gate

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
                    AND COALESCE(jm.is_closed, FALSE) = TRUE
              )
        """, {"oid": order_id})
        _n_inhouse = int((_inhouse_open[0].get("n") or 0) if _inhouse_open else 0)
        if _n_inhouse > 0:
            st.session_state["_billing_blocked_msg"] = (
                f"Billing blocked — {_n_inhouse} inhouse job(s) not yet closed. "
                f"Advance production to READY FOR PACK first."
            )
            return

        # Gate 2: contact lenses / solutions must have purchase recorded
        # These are stock items that are consumed per order —
        # purchase MUST be recorded before revenue is booked
        _cl_no_purchase = _rq_gate("""
            SELECT COUNT(*) AS n
            FROM order_lines ol
            JOIN products p ON p.id = ol.product_id
            WHERE ol.order_id = %(oid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
              -- Contact lenses and solutions identified by main_group
              AND UPPER(COALESCE(p.main_group,'')) IN (
                  'CONTACT LENSES', 'CONTACT LENS', 'SOLUTION'
              )
              -- Route must be STOCK (not RX custom)
              AND COALESCE(ol.lens_params->>'manufacturing_route','STOCK') IN ('STOCK','')
              -- No purchase acknowledgement with price recorded
              AND NOT EXISTS (
                  SELECT 1 FROM purchase_acknowledgements pa
                  WHERE pa.order_line_id = ol.id
                    AND COALESCE(pa.purchase_price, 0) > 0
              )
        """, {"oid": order_id})
        _n_cl = int((_cl_no_purchase[0].get("n") or 0) if _cl_no_purchase else 0)
        if _n_cl > 0:
            st.session_state["_billing_blocked_msg"] = (
                f"⚠️ Billing blocked — {_n_cl} contact lens / solution line(s) "
                f"have no purchase recorded. "
                f"Go to Procurement → 📋 Orders → Purchase to record the purchase first."
            )
            return

    except Exception:
        pass

    st.session_state["bo_selected_order_id"] = order_no
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
    except Exception:
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
    except Exception:
        pass
    return "  ".join(parts) if parts else ""


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
        except Exception:
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
        ("ORDER_PLACED",        "📤 Order Placed"),
        ("SUPPLIER_CONFIRMED",  "✅ Supplier Confirmed"),
        ("AWAITING_SUPPLY",     "⏳ Awaiting Supply"),
        ("RECEIVED",            "📦 Received"),
        ("INSPECTION",          "🔍 Inspection"),
        ("READY_FOR_BILLING",   "💰 Ready for Billing"),
    ]
    STAGE_IDX   = {s[0]: i for i, s in enumerate(STAGES)}
    STAGE_LABEL = {s[0]: s[1] for s in STAGES}

    def _stage_color(stage):
        return {"ORDER_PLACED":"#64748b","SUPPLIER_CONFIRMED":"#3b82f6",
                "AWAITING_SUPPLY":"#f59e0b","RECEIVED":"#8b5cf6",
                "INSPECTION":"#ef4444","READY_FOR_BILLING":"#22c55e"}.get(stage,"#475569")

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
              AND o.status NOT IN ('CANCELLED','CLOSED')
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
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
            _row["sup_stage"]     = str(_lp.get("supplier_stage") or "ORDER_PLACED")
            _row["sup_order_no"]  = str(_lp.get("supplier_order_no") or "")
            _sid = _row["supplier_id"]
            if _sid and _sid not in _sup_cache:
                try:
                    _sr = _q("SELECT party_name, mobile FROM parties WHERE id=%(sid)s::uuid LIMIT 1", {"sid": _sid}) or []
                    _sup_cache[_sid] = {"name": _sr[0]["party_name"] if _sr else "—",
                                        "mobile": _sr[0].get("mobile","") if _sr else ""}
                except Exception:
                    _sup_cache[_sid] = {"name":"—","mobile":""}
            if _sid and not _row["supplier_name"]:
                _row["supplier_name"] = _sup_cache.get(_sid,{}).get("name","—")
            _row["supplier_mobile"] = _sup_cache.get(_sid,{}).get("mobile","") if _sid else ""

        # ── Batch-fetch live PO status per line from supplier_orders ─────────
        _line_ids_all = [r["line_id"] for r in rows if r.get("line_id")]
        _po_by_line   = {}
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
            except Exception:
                pass
        for _row in rows:
            _po_hit = _po_by_line.get(_row["line_id"], {})
            _row["live_po_no"]     = _po_hit.get("po_no", "")
            _row["live_po_status"] = _po_hit.get("po_status", "")

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
            except Exception:
                pass
            if _odate:
                if _flt_from and _odate < _flt_from: return False
                if _flt_to   and _odate > _flt_to:   return False
        # Supplier name filter
        if _flt_sup and _flt_sup.strip():
            if _flt_sup.strip().lower() not in str(row.get("supplier_name","")).lower():
                return False
        # Order no / patient filter
        # Stage filter only — date/supplier/order handled in SQL above
        if _flt_stg != "ALL":
            if str(row.get("sup_stage","ORDER_PLACED")) != _flt_stg:
                return False
        return True

    rows = [r for r in rows if _matches(r)]

    if not rows:
        st.info("No lines match the current filters.")
        return

    _n_orders_sp = len(set(r['order_id'] for r in rows))
    _tbl_col, _cap_col = st.columns([1, 8])
    with _tbl_col:
        _sup_table_view = st.toggle("⊞", value=st.session_state.get(f"sp_tbl_{route_filter}", True),
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
            stage_label_fn=lambda l: STAGE_LABEL.get(l.get("sup_stage","ORDER_PLACED"),"ORDER_PLACED").split(" ",1)[-1],
            stage_code_fn=lambda l: l.get("sup_stage","ORDER_PLACED"),
            open_billing_fn=_go_to_billing,
        )
        return

    # Load suppliers for assignment dropdown
    _suppliers = []
    try:
        _sup_rows = _q("""
            SELECT id::text, party_name, COALESCE(mobile,'') AS mobile
            FROM parties
            WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR')
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY party_name
        """, {}) or []
        _suppliers = [{"id": r["id"], "name": r["party_name"], "mobile": r.get("mobile","")} for r in _sup_rows]
    except Exception:
        _suppliers = []
    _sup_by_id = {s["id"]: s for s in _suppliers}

    def _save_lp(line_id, lp_dict):
        from modules.sql_adapter import run_write as _rw
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
            except Exception:
                import datetime as _dt2
                _po_num = f"PO/{_dt2.date.today().strftime('%y%m%d%H%M%S')}"

            # ── Totals ────────────────────────────────────────────────────────
            _tqty = sum(int(l.get("quantity") or 1) for l in lines_for_po)
            _tval = sum(
                float(l.get("unit_price") or 0) * int(l.get("quantity") or 1)
                for l in lines_for_po
            )

            # ── Insert PO header ──────────────────────────────────────────────
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
                        NULLIF(%(clid)s,'')::uuid, 'PENDING'
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
        except Exception:
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
            _hs = STAGE_LABEL.get(_hl.get("sup_stage") or "ORDER_PLACED",
                                   _hl.get("sup_stage") or "ORDER_PLACED")
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
                    st.caption("Assign supplier to advance")
                elif _is_split_top:
                    # One compact advance button per supplier
                    for _tsid in _top_sup_ids:
                        _tlines   = [l for l in _rl_assigned_top if l.get("supplier_id") == _tsid]
                        _teyes    = "+".join(str(l.get("eye_side","")).upper() for l in _tlines)
                        _tname    = _sup_by_id.get(_tsid, {}).get("name","") or                                     next((l.get("supplier_name","") for l in _tlines), "—")
                        _tstages  = [l.get("sup_stage") or "ORDER_PLACED" for l in _tlines]
                        _tmax_idx = max(STAGE_IDX.get(s, 0) for s in _tstages)
                        _tnext    = STAGES[_tmax_idx + 1] if _tmax_idx < len(STAGES) - 1 else None
                        if _tnext:
                            if st.button(
                                f"▶ {_teyes} → {_tnext[1]}",
                                key=f"top_adv_{route_filter}_{_goid}_{_tsid[:8]}",
                                use_container_width=True, type="primary"
                            ):
                                try:
                                    from modules.sql_adapter import run_write as _rw_t
                                    for _tl in _tlines:
                                        _tlp = dict(_tl.get("_lp") or {})
                                        _tlp["supplier_stage"] = _tnext[0]
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
                    _tstages2 = [l.get("sup_stage") or "ORDER_PLACED" for l in _rl_assigned_top]
                    _tmax2    = max(STAGE_IDX.get(s, 0) for s in _tstages2)
                    _tnext2   = STAGES[_tmax2 + 1] if _tmax2 < len(STAGES) - 1 else None
                    if _tnext2:
                        if st.button(
                            f"▶ Advance {_teyes2} → {_tnext2[1]}",
                            key=f"top_adv_{route_filter}_{_goid}_{_gsid[:8]}",
                            use_container_width=True, type="primary"
                        ):
                            try:
                                from modules.sql_adapter import run_write as _rw_t2
                                for _tl2 in _rl_assigned_top:
                                    _tlp2 = dict(_tl2.get("_lp") or {})
                                    _tlp2["supplier_stage"] = _tnext2[0]
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

            with st.expander("🔍 Details / WhatsApp / Settings", expanded=False):
                def _eye_sort(x):
                    _e = str(x.get('eye_side', '')).upper()
                    if _e in ('R', 'RIGHT'): return 0
                    if _e in ('L', 'LEFT'):  return 1
                    return 2
                for line in sorted(lines, key=_eye_sort):
                    _lid      = str(line["line_id"])
                    _eye      = str(line.get("eye_side") or "").upper()
                    _pname    = str(line.get("product_name") or "").split(" | ")[0]
                    _needed   = int(line.get("quantity") or 1)
                    _ready    = int(line.get("ready_qty") or 0)
                    _supp     = str(line.get("supplier_name") or "—")
                    _sup_mob  = str(line.get("supplier_mobile") or "")
                    _stage    = line.get("sup_stage") or "ORDER_PLACED"
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
                        except Exception:
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
                        except Exception:
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

                    # ── Per-line: supplier assign + ref no ────────────────────
                    if _suppliers:
                        _sup_ids_l  = [s["id"] for s in _suppliers]
                        _sup_lbls_l = {s["id"]: s["name"] for s in _suppliers}
                        _cur_l      = line.get("supplier_id","")
                        # prepend blank option for unassigned
                        _opts_l     = [""] + _sup_ids_l
                        _opts_fmt_l = {"": "— Assign supplier —", **_sup_lbls_l}
                        _def_l      = _opts_l.index(_cur_l) if _cur_l in _opts_l else 0
                        _pa1, _pa2 = st.columns([4, 3])
                        with _pa1:
                            _new_sup_l = st.selectbox(
                                f"Supplier {_eye_lbl}",
                                _opts_l,
                                index=_def_l,
                                format_func=lambda x: _opts_fmt_l.get(x, x),
                                key=f"sup_sel_line_{_lid}",
                                label_visibility="collapsed"
                            )
                        with _pa2:
                            if _new_sup_l != _cur_l and _new_sup_l != "":
                                if st.button(f"✅ Assign to {_eye_lbl}",
                                             key=f"sup_save_line_{_lid}",
                                             use_container_width=True, type="primary"):
                                    try:
                                        _lp["supplier_id"]   = _new_sup_l
                                        _lp["supplier_name"] = _sup_lbls_l.get(_new_sup_l,"")
                                        _save_lp(_lid, _lp)
                                        st.rerun()
                                    except Exception as _sle: st.error(str(_sle))
                            elif _new_sup_l == "" and _cur_l:
                                if st.button("✖ Unassign", key=f"sup_clear_line_{_lid}",
                                             use_container_width=True):
                                    try:
                                        _lp.pop("supplier_id", None)
                                        _lp.pop("supplier_name", None)
                                        _save_lp(_lid, _lp)
                                        st.rerun()
                                    except Exception as _ule: st.error(str(_ule))
                            else:
                                st.caption("✔ Assigned" if _cur_l else "No supplier")

                    # ACTION: Supplier order number input
                    if _stage in ("ORDER_PLACED","SUPPLIER_CONFIRMED","AWAITING_SUPPLY"):
                        _new_ono = st.text_input(
                            "Supplier Ref No.",
                            value=_sup_ono,
                            placeholder="Enter supplier's ref / order no.",
                            key=f"sup_ono_{_lid}",
                            label_visibility="collapsed"
                        )
                        if _new_ono != _sup_ono:
                            try:
                                _lp["supplier_order_no"] = _new_ono
                                if _new_ono and _stage == "ORDER_PLACED":
                                    _lp["supplier_stage"] = "SUPPLIER_CONFIRMED"
                                _save_lp(_lid, _lp)
                                # ── Auto-create PO when ref entered (order confirmed) ─
                                if _new_ono and _stage == "ORDER_PLACED":
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

                    # ── Record Purchase (challan/invoice reference) ──────────
                    # Available once goods are RECEIVED.
                    # Records supplier challan/invoice number as a procurement reference.
                    # Temporarily fills purchase price from inventory_stock.purchase_price.
                    if _stage in ("RECEIVED", "INSPECTION", "READY_FOR_BILLING"):
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
                                except Exception:
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
                                _rp_can_save = bool(_rp_challan.strip() or _rp_invoice.strip())
                                if st.button(
                                    "💾 Save Purchase Record",
                                    key=f"rp_save_{_lid}",
                                    type="primary",
                                    use_container_width=True,
                                    disabled=not _rp_can_save,
                                    help="Enter at least challan no. or invoice no. to save"
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
                                                except Exception:
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
                                                except Exception:
                                                    pass
                                            _rw_pa2("""
                                                INSERT INTO purchase_acknowledgements (
                                                    order_line_id, order_id, order_no,
                                                    product_id, product_name, eye_side,
                                                    supplier_id, supplier_name,
                                                    challan_no, invoice_no, document_date,
                                                    qty, received_qty,
                                                    purchase_price, total_value, notes,
                                                    billing_status, acknowledged_at
                                                ) VALUES (
                                                    %(lid)s::uuid, %(oid)s::uuid, %(ono)s,
                                                    %(pid)s::uuid, %(pname)s, %(eye)s,
                                                    %(sid)s::uuid, %(sname)s,
                                                    %(chal)s, %(inv)s, %(ddate)s::date,
                                                    %(qty)s, %(rqty)s,
                                                    %(price)s, %(total)s, %(notes)s,
                                                    'NOT_READY', NOW()
                                                )
                                                ON CONFLICT (order_line_id) DO UPDATE SET
                                                    challan_no      = EXCLUDED.challan_no,
                                                    invoice_no      = EXCLUDED.invoice_no,
                                                    document_date   = EXCLUDED.document_date,
                                                    received_qty    = EXCLUDED.received_qty,
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
                    if _stage in ("ORDER_PLACED", "AWAITING_SUPPLY", "SUPPLIER_CONFIRMED"):
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
                                if st.button("✅ Approve → Ready for Billing",
                                             key=f"insp_ok_{_lid}", type="primary", use_container_width=True):
                                    _lp["supplier_stage"] = "READY_FOR_BILLING"
                                    _lp["inspection_result"] = "PASS"
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
                    _rfb_ready    = _rfb_locked and _rfb_recv_qty > 0 and _rfb_price > 0

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
                    # External Lab: swap to supplier product name if mapping exists
                    if use_supplier_name:
                        _wl_pid = str(wl.get("product_id") or "")
                        _wl_sid = str(wl.get("supplier_id") or "")
                        if _wl_pid and _wl_sid:
                            try:
                                from modules.backoffice.supplier_product_map_ui import get_supplier_product_name
                                _spm = get_supplier_product_name(_wl_pid, _wl_sid)
                                if _spm.get("supplier_product_name"):
                                    _pn = _spm["supplier_product_name"]
                            except Exception:
                                pass  # fall back to our product name
                    _lpp = wl.get("_lp") or wl.get("lens_params") or {}
                    if isinstance(_lpp, str):
                        try: import json as _jj; _lpp = _jj.loads(_lpp)
                        except: _lpp = {}
                    parts = [f"*{_lbl}: {_pn}*"]
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
                    _msg_parts = [f"*📋 Order: {info['order_no']}*"]
                    _msg_parts.append(f"Patient: {info['patient_name']}")
                    if sup_name and sup_name != "—":
                        _msg_parts.append(f"To: {sup_name}")
                    _msg_parts.append("")
                    for _wl in wa_lines:
                        # External Lab: use supplier's product name in WA message
                        _msg_parts.append(_line_detail_grp(
                            _wl, show_ref=show_ref,
                            use_supplier_name=_is_lab
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

                # ── Separate: unassigned lines vs assigned lines ──────────
                _rl_lines = sorted(
                    [l for l in lines if str(l.get("eye_side","")).upper() in ("R","L","RIGHT","LEFT")],
                    key=lambda x: 0 if str(x.get("eye_side","")).upper() in ("R","RIGHT") else 1
                )
                _assigned_rl   = [l for l in _rl_lines if l.get("supplier_id")]
                _unassigned_rl = [l for l in _rl_lines if not l.get("supplier_id")]

                # Unique supplier IDs among assigned R/L lines in this card
                _sup_ids_in_grp = list(dict.fromkeys(
                    l["supplier_id"] for l in _assigned_rl if l.get("supplier_id")
                ))
                _is_split = len(_sup_ids_in_grp) > 1  # R → Sup A, L → Sup B

                st.markdown("---")

                # ── Quick-assign unassigned lines (only when some are unassigned) ─
                if _unassigned_rl and _suppliers:
                    _sup_ids_qa  = [s["id"] for s in _suppliers]
                    _sup_lbls_qa = {s["id"]: s["name"] for s in _suppliers}
                    _qa1, _qa2 = st.columns([4, 3])
                    with _qa1:
                        _sel_sup_qa = st.selectbox(
                            "Quick-assign unassigned eyes",
                            _sup_ids_qa,
                            format_func=lambda x: _sup_lbls_qa.get(x, x),
                            key=f"sup_sel_{route_filter}_{_goid[:8]}_{_gsid[:8]}",
                            label_visibility="collapsed"
                        )
                    with _qa2:
                        _unassigned_eye_labels = "+".join(
                            str(l.get("eye_side","")).upper() for l in _unassigned_rl
                        )
                        if st.button(f"✅ Assign {_unassigned_eye_labels} → {_sup_lbls_qa.get(_sel_sup_qa,'')}",
                                     key=f"sup_asgn_{route_filter}_{_goid[:8]}_{_gsid[:8]}",
                                     use_container_width=True):
                            try:
                                from modules.sql_adapter import run_write as _rwa
                                for _ul in _unassigned_rl:
                                    _ulp = dict(_ul.get("_lp") or {})
                                    _ulp["supplier_id"]   = _sel_sup_qa
                                    _ulp["supplier_name"] = _sup_lbls_qa.get(_sel_sup_qa,"")
                                    _rwa("UPDATE order_lines SET lens_params=%(lp)s::jsonb "
                                         "WHERE id=%(lid)s::uuid",
                                         {"lp": _jsp.dumps(_ulp), "lid": _ul["line_id"]})
                                st.success(f"✅ Assigned to {_sup_lbls_qa.get(_sel_sup_qa)}")
                                st.rerun()
                            except Exception as _ae: st.error(str(_ae))

                # ── Advance + WA buttons — only for assigned lines ────────
                if not _assigned_rl:
                    st.info("⚠️ Assign a supplier to R/L lines above to enable advancement and WhatsApp.")
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
                        _split_stages = [l.get("sup_stage") or "ORDER_PLACED" for l in _split_lines]
                        _split_max_idx = max(STAGE_IDX.get(s, 0) for s in _split_stages)
                        _split_next    = STAGES[_split_max_idx + 1] if _split_max_idx < len(STAGES) - 1 else None
                        _split_mob     = _mob_for_supplier(_split_sid)
                        _split_show_ref = (STAGES[_split_max_idx][0] != "ORDER_PLACED")

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
                                _split_order_sent = (STAGES[_split_max_idx][0] != "ORDER_PLACED")
                                if _split_order_sent:
                                    st.markdown(
                                        "<div style='background:#1e293b;border:1px solid #334155;"
                                        "border-radius:6px;padding:7px 12px;text-align:center;"
                                        "color:#64748b;font-size:0.78rem'>✅ Order sent</div>",
                                        unsafe_allow_html=True
                                    )
                                elif _split_mob:
                                    _sp_wa_msg = _build_wa_msg(_split_lines, _split_name, _split_show_ref)
                                    _sp_wa_url = f"https://wa.me/{_split_mob}?text={_uparse.quote(_sp_wa_msg, safe='')}"
                                    st.link_button(
                                        f"📲 WhatsApp {_split_eyes} to {_split_name}",
                                        _sp_wa_url, use_container_width=True, type="primary"
                                    )
                                else:
                                    st.caption(f"⚠️ No mobile for {_split_name}")

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

                else:
                    # ── SAME SUPPLIER MODE: combined advance + WA for all assigned lines ──
                    _same_sid      = _sup_ids_in_grp[0] if _sup_ids_in_grp else ""
                    _same_name     = _sup_by_id.get(_same_sid, {}).get("name","") or \
                                     next((l.get("supplier_name","") for l in _assigned_rl if l.get("supplier_name")), "—")
                    _same_mob      = _mob_for_supplier(_same_sid)
                    _same_stages   = [l.get("sup_stage") or "ORDER_PLACED" for l in _assigned_rl]
                    _same_min_idx  = min(STAGE_IDX.get(s, 0) for s in _same_stages)
                    _same_max_idx  = max(STAGE_IDX.get(s, 0) for s in _same_stages)
                    _same_next     = STAGES[_same_max_idx + 1] if _same_max_idx < len(STAGES) - 1 else None
                    _same_cur_lbl  = STAGE_LABEL.get(STAGES[_same_min_idx][0], STAGES[_same_min_idx][0])
                    _same_show_ref = (STAGES[_same_max_idx][0] != "ORDER_PLACED")
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
                        _same_order_sent = (STAGES[_same_max_idx][0] != "ORDER_PLACED")
                        if _same_order_sent:
                            st.markdown(
                                "<div style='background:#1e293b;border:1px solid #334155;"
                                "border-radius:6px;padding:7px 12px;text-align:center;"
                                "color:#64748b;font-size:0.78rem'>✅ Order sent</div>",
                                unsafe_allow_html=True
                            )
                        elif _same_mob:
                            _wa_msg_same = _build_wa_msg(_assigned_rl, _same_name, _same_show_ref)
                            _wa_url_same = f"https://wa.me/{_same_mob}?text={_uparse.quote(_wa_msg_same, safe='')}"
                            _wa_lbl = (f"📲 WhatsApp {_same_eyes} to {_same_name}"
                                       if _same_name and _same_name != "—" else "📲 Send via WhatsApp")
                            st.link_button(_wa_lbl, _wa_url_same, use_container_width=True, type="primary")
                        else:
                            st.warning("⚠️ Assign supplier with mobile to enable WhatsApp")

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

            # end container

def _render_inhouse_pipeline():
    """
    In-house lab pipeline — same UI as supplier pipeline.
    Identifies orders by presence of job cards in job_master.
    Stages driven by job_master.current_stage.
    Shows BILLED orders too so production staff can see completed jobs.
    """
    import json as _ji

    st.markdown("### 🔬 In-house Lab Pipeline")
    st.caption("Track lenses through internal production stages.")

    STAGES = [
        ("JOB_CREATED",      "📋 Job Created"),
        ("PRINTED",          "🖨️ Job Printed"),         # was JOB_PRINTED
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
            "READY_FOR_PACK":    "#10b981",
            "READY_TO_BILL":     "#16a34a",
            "DISPATCHED":        "#059669",
            "DELIVERED":         "#22c55e",
            "CHALLANED":         "#0284c7",
            "INVOICED":          "#22c55e",
            "REJECTED":          "#dc2626",
        }.get(stage, "#475569")

    # ── Fetch lines via job_master ────────────────────────────────────
    try:
        rows = _q("""
            SELECT
                o.id::text              AS order_id,
                o.order_no,
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
            JOIN products p     ON p.id  = ol.product_id
            JOIN job_master jm  ON jm.order_line_id = ol.id
            WHERE o.status NOT IN ('CANCELLED','CLOSED')
              AND COALESCE(ol.is_deleted, FALSE)  = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
              -- INHOUSE identification: the INNER JOIN with job_master above already
              -- guarantees these are inhouse lines (job cards only exist for inhouse).
              -- We do NOT filter by lens_params->>'manufacturing_route' because
              -- that field may only be set on one eye (e.g. L) and not the other (R),
              -- causing R eye to disappear from the list.
              -- Extra safety: exclude orders that have ZERO job_master rows
              -- (they'd be supplier/stock orders, not inhouse).
              AND o.id IN (
                SELECT DISTINCT ol2.order_id FROM order_lines ol2
                JOIN job_master jm2 ON jm2.order_line_id = ol2.id
                WHERE COALESCE(ol2.is_deleted, FALSE) = FALSE
              )
            ORDER BY o.created_at DESC, o.order_no,
                     CASE WHEN ol.eye_side='R' THEN 0 WHEN ol.eye_side='L' THEN 1 ELSE 2 END
        """, {}) or []
    except Exception as _se:
        st.error(f"Could not load in-house lines: {_se}")
        return

    if not rows:
        st.info("✅ No in-house jobs found. Create job cards from the Backoffice → Documents tab.")
        return

    # ── lab_stage already set by SQL from job_master.current_stage ───
    for _row in rows:
        if not _row.get("lab_stage"):
            _row["lab_stage"] = "JOB_CREATED"

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
        except Exception:
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
    _active_stages = {s[0] for s in STAGES if s[0] not in ("READY_TO_BILL","REJECTED")}

    def _ih_matches(r):
        if _flt_ord and _flt_ord.strip():
            _s = _flt_ord.strip().lower()
            if not (_s in str(r.get("order_no","")).lower() or
                    _s in str(r.get("patient_name","")).lower()):
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
            except Exception:
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
                JOIN products p     ON p.id  = ol.product_id
                JOIN job_master jm  ON jm.order_line_id = ol.id
                WHERE o.id = ANY(%(oids)s::uuid[])
                  AND COALESCE(ol.is_deleted, FALSE)  = FALSE
                  AND COALESCE(ol.is_service_line, FALSE) = FALSE
                  AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')
                ORDER BY o.created_at DESC, o.order_no,
                         CASE WHEN ol.eye_side='R' THEN 0
                              WHEN ol.eye_side='L' THEN 1 ELSE 2 END
            """, {"oids": _matched_order_ids}) or rows  # fallback to filtered rows
            rows = _all_rows_for_matched
        except Exception:
            pass  # keep original filtered rows on error

    _n_orders_ih = len(set(r['order_id'] for r in rows))
    _ihtbl_col, _ihcap_col = st.columns([1, 8])
    with _ihtbl_col:
        _ih_table_view = st.toggle("⊞", value=st.session_state.get("ih_tbl_view", True),
                                    key="ih_tbl_view", help="Compact table view")
    with _ihcap_col:
        st.caption(f"Showing {_n_orders_ih} order(s) · {len(rows)} line(s)")

    if _ih_table_view:
        from collections import defaultdict as _ihdd
        _ih_grps = _ihdd(lambda: {"order_no":"","patient":"","lines":[],"order_id":"","created_at":""})
        for _r in rows:
            _gk = _r["order_id"]
            _ih_grps[_gk]["order_no"]   = _r["order_no"]
            _ih_grps[_gk]["patient"]    = _r["patient_name"]
            _ih_grps[_gk]["order_id"]   = _r["order_id"]
            _ih_grps[_gk]["created_at"] = str(_r.get("created_at",""))[:10]
            _ih_grps[_gk]["lines"].append(_r)
        _render_pipeline_cards(
            groups=_ih_grps,
            route_key="INHOUSE",
            stage_label_fn=lambda l: STAGE_LABEL.get(l.get("lab_stage","JOB_CREATED"),"JOB_CREATED").split(" ",1)[-1],
            stage_code_fn=lambda l: l.get("lab_stage","JOB_CREATED"),
            open_billing_fn=_go_to_billing,
        )
        return

    def _save_stage_ih(job_id, new_stage):
        from modules.sql_adapter import run_write as _rw
        # Terminal stages close the job — enables billing gate
        _terminal = new_stage in ("READY_TO_BILL", "READY_FOR_PACK")
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
            except Exception:
                pass
        # Log event for stage timeline
        try:
            _rw("""
                INSERT INTO job_stage_events (job_id, stage_code, created_at)
                VALUES (%(jid)s::uuid, %(stage)s, NOW())
            """, {"jid": job_id, "stage": new_stage})
        except Exception:
            pass  # best-effort — never crash the advance

    def _power_str_ih(line):
        parts = []
        try:
            if line.get("sph") is not None: parts.append(f"SPH {float(line['sph']):+.2f}")
            if line.get("cyl") and abs(float(line["cyl"])) > 0.01: parts.append(f"CYL {float(line['cyl']):+.2f}")
            if line.get("axis"): parts.append(f"AX {int(line['axis'])}")
            if line.get("add_power") and float(line["add_power"]) > 0: parts.append(f"ADD +{float(line['add_power']):.2f}")
        except Exception: pass
        return "  ".join(parts)

    # ── Group by order ────────────────────────────────────────────────
    from collections import OrderedDict as _od
    _groups = _od()
    for _row in rows:
        _oid = _row["order_id"]
        if _oid not in _groups:
            _groups[_oid] = {
                "order_id":     _oid,
                "order_no":     _row["order_no"],
                "patient_name": _row["patient_name"],
                "status":       _row.get("status",""),
                "lines":        [],
            }
        _groups[_oid]["lines"].append(_row)

    # ── Render each order card ────────────────────────────────────────
    for _oid, odata in _groups.items():
        lines   = odata["lines"]
        _total  = len(lines)
        _ready  = sum(1 for l in lines if l.get("lab_stage") in (
            "READY_FOR_PACK","READY_TO_BILL","FITTING_DONE"))

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
        except Exception:
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
                    except Exception:
                        pass
                    _bill_label = "🧾 INVOICED — LOCKED" if _has_invoice_ih else "📋 CHALLANED — LOCKED"
                    _bill_color = "#22c55e" if _has_invoice_ih else "#3b82f6"
                    st.markdown(
                        f"<div style='padding:4px 0'>"
                        f"<span style='font-weight:800;color:#e2e8f0;font-size:1rem'>"
                        f"{'🧾' if _has_invoice_ih else '📋'} {odata['order_no']}</span>"
                        f"<span style='color:#64748b;font-size:0.82rem'> — {odata['patient_name']}</span><br>"
                        f"<span style='background:#052e16;color:{_bill_color};font-size:0.78rem;"
                        f"font-weight:700;padding:2px 10px;border-radius:4px;"
                        f"border:1px solid {_bill_color}'>{_bill_label}</span>"
                        f"</div>",
                        unsafe_allow_html=True
                    )
                else:
                    st.markdown(
                        f"<div style='padding:4px 0'>"
                        f"<span style='font-weight:800;color:#e2e8f0;font-size:1rem'>"
                        f"{_hdr_icon} {odata['order_no']}</span>"
                        f"<span style='color:#64748b;font-size:0.82rem'> — {odata['patient_name']}</span><br>"
                        f"<span style='color:#475569;font-size:0.72rem'>"
                        f"🔬 In-house · {_ready}/{_total} ready"
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
                                    in ("JOB_CREATED", "PRINTED")]
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
                    except Exception:
                        pass

                _rl_stages = [l.get("lab_stage") or "JOB_CREATED" for l in _rl_lines]
                _min_idx   = min(STAGE_IDX.get(s,0) for s in _rl_stages) if _rl_stages else 0
                _max_idx   = max(STAGE_IDX.get(s,0) for s in _rl_stages) if _rl_stages else 0
                _same_stage = (_min_idx == _max_idx)

                if not _rl_lines:
                    st.caption("No R/L lines")
                elif _top_locked:
                    st.caption("🔒 Locked after billing")
                elif _same_stage:
                    _tnext = STAGES[_max_idx + 1] if _max_idx < len(STAGES) - 1 else None
                    _teyes = "+".join(str(l.get("eye_side","")).upper() for l in _rl_lines)
                    if _tnext:
                        if st.button(f"▶ Advance {_teyes} → {_tnext[1]}",
                                     key=f"ih_top_adv_{_oid}",
                                     use_container_width=True, type="primary"):
                            try:
                                for _tl in _rl_lines:
                                    _save_stage_ih(str(_tl["job_id"]), _tnext[0])
                                st.rerun()
                            except Exception as _te: st.error(str(_te))
                    else:
                        st.success("✅ All at final stage")
                else:
                    # Different stages — one button per eye
                    for _tl in _rl_lines:
                        _te   = str(_tl.get("eye_side","")).upper()
                        _ts   = _tl.get("lab_stage") or "JOB_CREATED"
                        _tidx = STAGE_IDX.get(_ts, 0)
                        _tnxt = STAGES[_tidx + 1] if _tidx < len(STAGES) - 1 else None
                        if _tnxt:
                            if st.button(f"▶ {_te} → {_tnxt[1]}",
                                         key=f"ih_top_adv_{_oid}_{_te}",
                                         use_container_width=True, type="primary"):
                                try:
                                    _save_stage_ih(str(_tl["job_id"]), _tnxt[0])
                                    st.rerun()
                                except Exception as _te2: st.error(str(_te2))

            # ── Expander: per-line detail + controls ──────────────────
            with st.expander("🔍 Details / Settings", expanded=False):
                def _eye_sort_ih(x):
                    _e = str(x.get("eye_side","")).upper()
                    if _e in ("R","RIGHT"): return 0
                    if _e in ("L","LEFT"):  return 1
                    return 2

                _jc_rendered_pair_oids = set()  # track orders where paired JC already rendered

                for line in sorted(lines, key=_eye_sort_ih):
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
                        f"{_rdyq}/{_needed} pcs</div></div>",
                        unsafe_allow_html=True
                    )

                    # ── Stage timeline (time tags) ────────────────────
                    _job_evs = _stage_events_by_job.get(_jid, [])
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
                    except Exception:
                        pass

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

                    _next    = None if _is_rejected else (
                        STAGES[_cur_idx + 1] if _cur_idx < len(STAGES) - 2 else None
                    )  # -2 to exclude REJECTED from normal advance chain
                    _prev    = STAGES[:_cur_idx] if not _is_rejected else []

                    _adv_col, _rec_col, _rej_col = st.columns([3, 3, 2])
                    with _adv_col:
                        if _is_rejected:
                            st.error("🚫 Rejected")
                        elif _is_challaned:
                            st.caption("🔒 Locked after billing")
                        elif _next:
                            if st.button(f"▶ → {_next[1]}",
                                         key=f"ih_adv_{_lid}", type="primary",
                                         use_container_width=True):
                                try:
                                    _save_stage_ih(_jid, _next[0])
                                    st.rerun()
                                except Exception as _ae: st.error(str(_ae))
                        else:
                            st.success("✅ Final stage")

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
                                except Exception:
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
                                        from modules.sql_adapter import run_scalar as _rs_rej, run_write as _rwrej
                                        import json as _rjson

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
                                                    except: _lp_rej = {}
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
                                                        (job_id, stage_code, remarks, created_at)
                                                    VALUES (%(jid)s::uuid, 'REJECTED', %(rmk)s, NOW())
                                                """, {"jid": _jid, "rmk": _rej_reason})
                                            except Exception: pass

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
                    _show_jc = (_stage in ("JOB_CREATED", "PRINTED") and not _ih_all_billed)
                    # Skip if this eye's order was already rendered as a pair
                    if _show_jc and odata["order_id"] in _jc_rendered_pair_oids:
                        _show_jc = False
                    if _show_jc:
                        # Pre-load surfacing data from DB for prefill
                        _jc_lp = line.get("lens_params") or {}
                        if isinstance(_jc_lp, str):
                            try:
                                import json as _jcj
                                _jc_lp = _jcj.loads(_jc_lp)
                            except Exception:
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
                                    "id":           odata["order_id"],
                                    "order_no":     odata["order_no"],
                                    "patient_name": odata["patient_name"],
                                    "order_type":   "RETAIL",
                                }
                                # Build line dict — full fields for job card form
                                import json as _jcjson
                                _jc_lp_raw = line.get("lens_params") or {}
                                if isinstance(_jc_lp_raw, str):
                                    try: _jc_lp_raw = _jcjson.loads(_jc_lp_raw)
                                    except: _jc_lp_raw = {}
                                _jc_bp_raw = line.get("boxing_params") or {}
                                if isinstance(_jc_bp_raw, str):
                                    try: _jc_bp_raw = _jcjson.loads(_jc_bp_raw)
                                    except: _jc_bp_raw = {}
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
                                        except Exception:
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
                                            except: _lp2 = {}
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
                                            from modules.backoffice.production_panel import (
                                                _build_combined_job_card_html, _open_print_window
                                            )
                                            _open_print_window(_build_combined_job_card_html(
                                                _pb_r_line, _pb_l_line, _jc_order
                                            ))
                                        except Exception as _pbe:
                                            st.error(f"Print error: {_pbe}")

                                    if st.session_state.pop(_key_lbl, False):
                                        try:
                                            from modules.backoffice.production_panel import (
                                                _build_label_page, _open_print_window
                                            )
                                            _lb_lines = [l for l in [_pb_r_line, _pb_l_line] if l]
                                            _lb_order = {
                                                "id":           odata["order_id"],
                                                "order_no":     odata["order_no"],
                                                "patient_name": odata["patient_name"],
                                                "party_name":   lines[0].get("party_name","") if lines else "",
                                                "order_type":   lines[0].get("order_type","RETAIL") if lines else "RETAIL",
                                            }
                                            _open_print_window(_build_label_page(_lb_lines, _lb_order))
                                        except Exception as _lbe:
                                            st.error(f"Label error: {_lbe}")

                                else:
                                    # ── NOT YET ALLOCATED: show blank selection form + save ──
                                    render_surfacing_job_card(_jc_line, _jc_order, show_buttons=False)
                                    _jc1, _jc2 = st.columns(2)
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
                                        if st.button("🖨 Print Job Card",
                                                     key=f"jc_print_{_lid}",
                                                     use_container_width=True):
                                            try:
                                                from modules.documents.job_card_surfacing import render_job_card_print
                                                render_job_card_print(_jc_line, _jc_order)
                                            except Exception as _pe:
                                                st.error(f"Print error: {_pe}")
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
                                except: _lp_summ = {}
                            _surf = _lp_summ.get("surfacing_data") or {}
                        except Exception:
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
                                except: _lp_bl = {}
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
                        except Exception:
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
                                                    (job_id, stage_code, remarks, created_at)
                                                VALUES (%(jid)s::uuid, 'JOB_CREATED',
                                                        %(rmk)s, NOW())
                                            """, {
                                                "jid": _jid,
                                                "rmk": (
                                                    f"Pipeline restarted after rejection. "
                                                    f"New blank: {_sel_blank_row.get('blank_brand','')} "
                                                    f"{_sel_blank_row.get('blank_material','')} "
                                                    f"BC:{_sel_blank_row.get('base_curve','')}"
                                                )
                                            })
                                        except Exception: pass
                                        # 3. Write new blank selection into lens_params
                                        try:
                                            import json as _jrestart
                                            _lp_restart = line.get("lens_params") or {}
                                            if isinstance(_lp_restart, str):
                                                try: _lp_restart = _jrestart.loads(_lp_restart)
                                                except: _lp_restart = {}
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
                                                   SET lens_params = %(lp)s::jsonb,
                                                       updated_at  = NOW()
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
                # Only include lines that can still advance (exclude REJECTED and DELIVERED)
                _rl_advanceable = [l for l in _rl_lines
                                   if l.get("lab_stage") not in ("REJECTED", "READY_TO_BILL")]
                _grp_stages  = [l.get("lab_stage") or "JOB_CREATED" for l in _rl_advanceable] if _rl_advanceable else []
                _grp_all_stages = [l.get("lab_stage") or "JOB_CREATED" for l in _rl_lines]
                _grp_min_idx = min(STAGE_IDX.get(s,0) for s in _grp_stages) if _grp_stages else 0
                _grp_max_idx = max(STAGE_IDX.get(s,0) for s in _grp_stages) if _grp_stages else 0
                _grp_display_idx = min(STAGE_IDX.get(s,0) for s in _grp_all_stages) if _grp_all_stages else 0
                _grp_next    = STAGES[_grp_max_idx + 1] if (_grp_stages and _grp_max_idx < len(STAGES) - 2) else None
                _grp_lbl     = STAGE_LABEL.get(STAGES[_grp_display_idx][0], STAGES[_grp_display_idx][0])
                _grp_eyes    = "+".join(str(l.get("eye_side","")).upper() for l in _rl_lines)

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
                except Exception:
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
                    if not _rl_advanceable:
                        st.info("🚫 All lines rejected or delivered")
                    elif _grp_locked:
                        st.caption(f"🔒 Locked — {_grp_lock_label}")
                    elif _grp_next:
                        if st.button(
                            f"▶ Advance Both → {_grp_next[1]}"
                            if len(_rl_advanceable) > 1 else f"▶ Advance → {_grp_next[1]}",
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
                    _grp_prev = STAGES[:_grp_min_idx]
                    if _grp_prev and _rl_advanceable and not _grp_locked:
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


def _render_production_list():
    st.markdown("## 🏭 Production Pipeline")

    _tab_supplier, _tab_inhouse, _tab_extlab, _tab_stock, _tab_proc, _tab_auth = st.tabs([
        "🏭 Supplier (Direct)",
        "🔬 In-house Lab",
        "🧪 External Lab",
        "📦 From Stock",
        "🛒 Procurement",
        "🏷️ Authenticity Cards",
    ])

    with _tab_supplier:
        _render_supplier_pipeline(route_filter="VENDOR")

    with _tab_extlab:
        _render_supplier_pipeline(route_filter="EXTERNAL_LAB")

    with _tab_stock:
        _render_stock_pipeline()

    with _tab_proc:
        _proc_tab1, _proc_tab2, _proc_tab3 = st.tabs([
            "📋 Direct Purchase",
            "📤 PO Management",
            "🔍 Purchase Register",
        ])
        with _proc_tab1:
            _render_sales_orders_to_po_tab()
        with _proc_tab2:
            try:
                from modules.procurement.po_management import render_po_management
                render_po_management()
            except Exception as _po_mgmt:
                import traceback
                st.error(f"PO Management error: {_po_mgmt}")
                st.code(traceback.format_exc())
        with _proc_tab3:
            try:
                from modules.backoffice.purchase_register import render_purchase_register
                render_purchase_register()
            except Exception as _pre:
                import traceback
                st.error(f"Purchase Register error: {_pre}")
                st.code(traceback.format_exc())

    with _tab_inhouse:
        _render_inhouse_pipeline()

    with _tab_auth:
        _render_authenticity_cards_tab()

    # ==================================================
    # SINGLE ORDER PRODUCTION VIEW
    # ==================================================

def _render_production_order():
    order = st.session_state.prod_selected_order
    if not order:
        st.session_state.prod_view_mode = "list"
        st.rerun()
        return

    col_back, col_title = st.columns([1, 5])
    with col_back:
        if st.button("← Back", use_container_width=True):
            st.session_state.prod_view_mode = "list"
            st.session_state.prod_selected_order = None
            st.session_state.prod_orders_loaded = False  # force refresh on return
            st.rerun()
    with col_title:
        st.markdown(
            f"## 🏭 {order['order_no']} "
            f"<span style='font-size:1rem;color:#6b7280'>— {order['patient_name']}</span>",
            unsafe_allow_html=True
        )

    # ── Dual status bar ───────────────────────────────────────────────
    prod_stage   = order.get("current_stage", "—")
    order_status = order.get("status", "—")
    sc_prod   = _STAGE_COLORS.get(prod_stage, "#6b7280")
    sc_status = _ORDER_STATUS_COLORS.get(order_status, "#64748b")

    st.markdown(
        f"<div style='display:flex;gap:12px;margin-bottom:10px;flex-wrap:wrap'>"
        f"<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
        f"padding:8px 16px'>"
        f"<div style='font-size:0.6rem;color:#64748b;text-transform:uppercase;"
        f"letter-spacing:.07em;margin-bottom:3px'>Order Status (Backoffice)</div>"
        f"<span style='background:{sc_status};color:#fff;padding:3px 12px;"
        f"border-radius:6px;font-size:0.82rem;font-weight:700'>{order_status}</span>"
        f"</div>"
        f"<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
        f"padding:8px 16px'>"
        f"<div style='font-size:0.6rem;color:#64748b;text-transform:uppercase;"
        f"letter-spacing:.07em;margin-bottom:3px'>Production Stage (Engine)</div>"
        f"<span style='background:{sc_prod};color:#fff;padding:3px 12px;"
        f"border-radius:6px;font-size:0.82rem;font-weight:700'>▶ {prod_stage}</span>"
        f"</div>"
        f"<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
        f"padding:8px 16px'>"
        f"<div style='font-size:0.6rem;color:#64748b;text-transform:uppercase;"
        f"letter-spacing:.07em;margin-bottom:3px'>Jobs</div>"
        f"<span style='color:#f1f5f9;font-weight:700'>"
        f"{order.get('open_jobs',0)} open / {order.get('total_jobs',0)} total</span>"
        f"</div>"
        f"</div>",
        unsafe_allow_html=True
    )
    st.markdown("---")

    # ── Fetch full order with all line fields (sph/cyl/category etc) ──
    # prod_selected_order only has list-view fields — need full lines for job card
    _full_order = _fetch_order_for_panel(order["order_no"])
    if _full_order:
        # Merge: keep production metadata from session order, add full lines
        _full_order["current_stage"] = order.get("current_stage", "")
        _full_order["status"]        = order.get("status", "")
        _full_order["open_jobs"]     = order.get("open_jobs", 0)
        _full_order["total_jobs"]    = order.get("total_jobs", 0)
        order = _full_order

    try:
        from modules.backoffice.production_panel import render_production_panel
        render_production_panel(order)
    except Exception as e:
        import traceback
        st.error(f"❌ Production panel error: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())


# ==================================================
# BACKOFFICE DUAL BADGE HELPER
# (imported by backoffice.py to replace manual status buttons)
# ==================================================

def render_production_status_badge(order_id: str, order_status: str) -> None:
    """
    Renders two stacked badges for an order card in backoffice:
      1. Order Status  (from orders.status — backoffice engine)
      2. Production Stage (from job_master — production engine)
    Auto-updates on each rerun — no manual button needed.
    Called from _render_order_card() in backoffice.py.
    """
    prod_stage = None
    try:
        rows = _q("""
            SELECT jm.current_stage
            FROM job_master jm
            JOIN order_lines ol ON ol.id = jm.order_line_id
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN job_stage_master jsm ON jsm.stage_code = jm.current_stage
            WHERE (o.id::text = %(oid)s OR o.order_no = %(oid)s)
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND jm.is_closed = FALSE
            ORDER BY COALESCE(jsm.sequence_order, 0) DESC
            LIMIT 1
        """, {"oid": order_id})
        if rows:
            prod_stage = rows[0].get("current_stage")
    except Exception:
        pass

    sc_status = _ORDER_STATUS_COLORS.get(order_status, "#64748b")
    sc_prod   = _STAGE_COLORS.get(prod_stage or "", "#334155")

    if prod_stage:
        st.markdown(
            f"<div style='display:flex;flex-direction:column;gap:3px'>"
            f"<div><span style='font-size:0.58rem;color:#64748b'>ORDER &nbsp;</span>"
            f"<span style='background:{sc_status};color:#fff;padding:2px 8px;"
            f"border-radius:4px;font-size:0.7rem;font-weight:700'>{order_status}</span></div>"
            f"<div><span style='font-size:0.58rem;color:#64748b'>PROD &nbsp;&nbsp;</span>"
            f"<span style='background:{sc_prod};color:#fff;padding:2px 8px;"
            f"border-radius:4px;font-size:0.7rem;font-weight:700'>▶ {prod_stage}</span></div>"
            f"</div>",
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            f"<span style='background:{sc_status};color:#fff;padding:2px 8px;"
            f"border-radius:4px;font-size:0.75rem;font-weight:700'>{order_status}</span>",
            unsafe_allow_html=True
        )


# ==================================================
# MAIN ENTRY POINT
# ==================================================


# ══════════════════════════════════════════════════════════════════════
# AUTHENTICITY CARD / BARCODE STICKER TAB
# ══════════════════════════════════════════════════════════════════════

def _render_authenticity_cards_tab():
    st.markdown(
        "<div style='color:#a78bfa;font-size:0.72rem;font-weight:700;"
        "letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px'>"
        "🏷️ AUTHENTICITY CARDS &amp; BARCODE STICKERS</div>",
        unsafe_allow_html=True
    )
    try:
        from modules.sql_adapter import run_query as _rq_auth
        _auth_rows = _rq_auth("""
            SELECT o.order_no, o.created_at, o.party_name, o.patient_name,
                   o.patient_mobile, o.order_type,
                   COALESCE(o.extra_data::text, '{}') AS extra_data_txt,
                   COALESCE(
                       (SELECT string_agg(CONCAT(l.eye_side,': SPH ',l.sph::text), ' | ')
                        FROM order_lines l
                        WHERE l.order_id = o.id AND l.sph IS NOT NULL
                        LIMIT 4), ''
                   ) AS power_summary
            FROM orders o
            WHERE o.order_type IN ('WHOLESALE', 'RETAIL')
              AND COALESCE(o.is_deleted, FALSE) = FALSE
              AND o.status NOT IN ('CANCELLED', 'RETURNED')
            ORDER BY o.created_at DESC
            LIMIT 50
        """) or []
    except Exception as _ae:
        st.error(f"Could not load orders: {_ae}")
        return

    if not _auth_rows:
        st.info("No recent orders found.")
        return

    _af1, _af2, _af3 = st.columns([3, 2, 2])
    with _af1:
        _auth_search = st.text_input("🔍 Search", placeholder="Order / customer / party",
                                      key="auth_search", label_visibility="collapsed")
    with _af2:
        _auth_type = st.selectbox("Type", ["All", "WHOLESALE", "RETAIL"],
                                   key="auth_type_filter", label_visibility="collapsed")
    with _af3:
        _sticker_fmt = st.selectbox(
            "Format",
            ["85×54 mm (Card)", "75×65 mm (Label Sticker)"],
            key="auth_sticker_fmt"
        )

    import json as _json
    _filtered = []
    for _r in _auth_rows:
        if _auth_type != "All" and _r.get("order_type") != _auth_type:
            continue
        try:
            _ec = _json.loads(_r.get("extra_data_txt") or "{}").get("end_customer") or {}
        except Exception:
            _ec = {}
        _q = (_auth_search or "").lower()
        _hay = " ".join([str(_r.get(k,"")) for k in ("order_no","party_name","patient_name")
                         ] + [str(_ec.get("name","")), str(_ec.get("mobile",""))]).lower()
        if _q and _q not in _hay:
            continue
        _filtered.append((_r, _ec))

    st.caption(f"{len(_filtered)} order(s)")

    _sel_key = "auth_selected_orders"
    if _sel_key not in st.session_state:
        st.session_state[_sel_key] = set()

    # ── Editable names (shown when at least one order selected) ──────
    _sel_onos = st.session_state.get(_sel_key, set())
    _selected_rows = [(r, ec) for r, ec in _filtered if r["order_no"] in _sel_onos]

    if _selected_rows:
        with st.expander(f"✏️ Edit customer names before printing ({len(_selected_rows)} selected)", expanded=True):
            st.caption("Correct any spelling mistakes. Changes apply to this print only — not saved to the order.")
            _name_overrides = {}
            for _r2, _ec2 in _selected_rows:
                _ono2 = _r2["order_no"]
                _default_name = _ec2.get("name") or _r2.get("patient_name") or ""
                _name_overrides[_ono2] = st.text_input(
                    f"{_ono2} — {_r2.get('party_name','')}",
                    value=_default_name,
                    key=f"auth_name_edit_{_ono2}",
                    placeholder="Customer name on card"
                )
            st.session_state["auth_name_overrides"] = _name_overrides

    _sb1, _sb2, _sb3 = st.columns([1, 1, 2])
    with _sb1:
        if st.button("☑️ All", key="auth_selall", use_container_width=True):
            st.session_state[_sel_key] = {r[0]["order_no"] for r in _filtered}; st.rerun()
    with _sb2:
        if st.button("☐ Clear", key="auth_clrsel", use_container_width=True,
                     disabled=not st.session_state[_sel_key]):
            st.session_state[_sel_key] = set(); st.rerun()
    with _sb3:
        if st.button(f"🖨️ Print {len(st.session_state[_sel_key])} Card(s)",
                     key="auth_print_btn", type="primary", use_container_width=True,
                     disabled=not st.session_state[_sel_key]):
            st.session_state["auth_do_print"] = True

    # List rows
    for _r, _ec in _filtered:
        _ono = _r["order_no"]
        _cols = st.columns([0.4, 1.8, 2, 2, 2.5, 1])
        with _cols[0]:
            _sel = _ono in st.session_state[_sel_key]
            _new = st.checkbox(" ", value=_sel, key=f"auth_chk_{_ono}", label_visibility="collapsed")
            if _new: st.session_state[_sel_key].add(_ono)
            else:    st.session_state[_sel_key].discard(_ono)
        _cols[1].caption(_ono)
        _cols[2].caption(_r.get("party_name",""))
        # Show end_customer name from extra_data (preferred) or patient_name
        _disp_name = _ec.get("name") or _r.get("patient_name") or "—"
        _cols[3].markdown(f"<span style='color:#a78bfa;font-size:0.78rem'>{_disp_name}</span>", unsafe_allow_html=True)
        _cols[4].caption(_r.get("power_summary") or "—")
        _cols[5].caption(_ec.get("mobile") or _r.get("patient_mobile") or "—")

    if st.session_state.pop("auth_do_print", False):
        _to_print = [(r, ec) for r, ec in _filtered if r["order_no"] in st.session_state[_sel_key]]
        # Apply name overrides
        _overrides = st.session_state.get("auth_name_overrides", {})
        _to_print_with_names = []
        for _r3, _ec3 in _to_print:
            _ono3 = _r3["order_no"]
            _ec3_copy = dict(_ec3)
            if _ono3 in _overrides and _overrides[_ono3].strip():
                _ec3_copy["name"] = _overrides[_ono3].strip()
            elif not _ec3_copy.get("name"):
                _ec3_copy["name"] = _r3.get("patient_name") or ""
            _to_print_with_names.append((_r3, _ec3_copy))
        _render_auth_card_print(_to_print_with_names, _sticker_fmt)


def _render_auth_card_print(orders_ec: list, fmt: str):
    import streamlit.components.v1 as _comp
    import base64 as _b64

    _is_card = "85" in fmt   # 85×54 card vs 75×65 sticker

    # ── Fetch full R+L RX for each order ────────────────────────────
    def _get_full_rx(order_no):
        try:
            from modules.sql_adapter import run_query as _rq_rx
            _rows = _rq_rx("""
                SELECT eye_side, sph, cyl, axis, add_power
                FROM order_lines
                WHERE order_id = (SELECT id FROM orders WHERE order_no=%(ono)s LIMIT 1)
                  AND COALESCE(is_deleted,FALSE)=FALSE AND eye_side IS NOT NULL
                ORDER BY eye_side
            """, {"ono": order_no}) or []
            _rx = {}
            for _rr in _rows:
                _e = str(_rr.get("eye_side","")).upper()[:1]
                if _e in ("R","L"):
                    _rx[_e] = {"sph": _rr.get("sph"), "cyl": _rr.get("cyl"),
                                "axis": _rr.get("axis"), "add": _rr.get("add_power")}
            return _rx
        except Exception:
            return {}

    def _fp(v):
        if v is None: return "&mdash;"
        try:
            n = float(v)
            return f"+{n:.2f}" if n >= 0 else f"{n:.2f}"
        except: return str(v)

    def _ax(v):
        if v is None: return "&mdash;"
        try: return str(int(float(v)))
        except: return str(v)

    # Build simple barcode representation
    def _bc_html(val, ht=16):
        _clean = "".join(c for c in val if c.isalnum())
        bars = ""
        for c in _clean:
            w1 = 2 if ord(c) % 2 == 0 else 1
            w2 = 1 if ord(c) % 3 == 0 else 2
            bars += (f"<span style='display:inline-block;width:{w1}px;height:{ht}px;"
                     f"background:#fff;margin:0;vertical-align:top'></span>"
                     f"<span style='display:inline-block;width:{w2}px;height:{ht}px;"
                     f"background:transparent;margin:0;vertical-align:top'></span>")
        return (f"<div style='display:inline-block;background:#fff;"
                f"padding:1px 3px;border-radius:2px'>"
                f"<div style='white-space:nowrap;line-height:0'>{bars}</div>"
                f"<div style='font-family:monospace;font-size:5pt;text-align:center;"
                f"color:#fff;margin-top:1px;letter-spacing:.05em'>{_clean}</div>"
                f"</div>")

    _cards_html = ""

    for _r, _ec in orders_ec:
        _name   = _ec.get("name") or _r.get("patient_name") or ""
        _mobile = _ec.get("mobile") or _r.get("patient_mobile") or ""
        _ono    = _r.get("order_no","")
        _party  = _r.get("party_name","")
        _ono_c  = "".join(c for c in _ono if c.isalnum())

        if _is_card:
            # ── 85×54mm dark gradient card ──────────────────────────
            _rx = _get_full_rx(_ono)
            _rx_r, _rx_l = _rx.get("R",{}), _rx.get("L",{})

            _r_row = (f"<tr><td class='ey'>R</td>"
                      f"<td>{_fp(_rx_r.get('sph'))}</td><td>{_fp(_rx_r.get('cyl'))}</td>"
                      f"<td class='ax'>{_ax(_rx_r.get('axis'))}</td>"
                      f"<td>{_fp(_rx_r.get('add'))}</td></tr>") if _rx_r else ""
            _l_row = (f"<tr><td class='ey'>L</td>"
                      f"<td>{_fp(_rx_l.get('sph'))}</td><td>{_fp(_rx_l.get('cyl'))}</td>"
                      f"<td class='ax'>{_ax(_rx_l.get('axis'))}</td>"
                      f"<td>{_fp(_rx_l.get('add'))}</td></tr>") if _rx_l else ""

            _cards_html += (
                f"<div class='card'>"
                f"<div class='top-row'>"
                f"  <span class='badge'>AUTHENTICITY CARD</span>"
                f"  <span class='logo'>&#9673;</span>"
                f"</div>"
                f"<div class='cname'>{_name or '&mdash;'}</div>"
                f"{'<div class=mobile>' + _mobile + '</div>' if _mobile else ''}"
                f"<table><tr class='hdr'><th></th><th>SPH</th><th>CYL</th>"
                f"<th class='ax'>AXIS</th><th>ADD</th></tr>"
                f"{_r_row}{_l_row}</table>"
                f"<div class='det'>"
                f"<span class='lbl'>Order</span> {_ono} &nbsp;&nbsp;"
                f"<span class='lbl'>Retailer</span> {_party}</div>"
                f"<div class='bc-row'>{_bc_html(_ono_c, ht=18)}</div>"
                f"</div>"
                f"<div style='page-break-after:always'></div>"
            )
        else:
            # ── 75×65mm white sticker ───────────────────────────────
            _rx = _get_full_rx(_ono)
            _rx_r, _rx_l = _rx.get("R",{}), _rx.get("L",{})
            _r_row = (f"<tr><td class='ey'>R</td>"
                      f"<td>{_fp(_rx_r.get('sph'))}</td><td>{_fp(_rx_r.get('cyl'))}</td>"
                      f"<td class='ax'>{_ax(_rx_r.get('axis'))}</td>"
                      f"<td>{_fp(_rx_r.get('add'))}</td></tr>") if _rx_r else ""
            _l_row = (f"<tr><td class='ey'>L</td>"
                      f"<td>{_fp(_rx_l.get('sph'))}</td><td>{_fp(_rx_l.get('cyl'))}</td>"
                      f"<td class='ax'>{_ax(_rx_l.get('axis'))}</td>"
                      f"<td>{_fp(_rx_l.get('add'))}</td></tr>") if _rx_l else ""
            _cards_html += (
                f"<div class='sticker'>"
                f"<div class='st-name'>{_name or _ono}</div>"
                f"<div class='st-ref'>{_ono} &bull; {_party}</div>"
                f"<table class='st-tbl'><tr class='hdr'><th></th><th>SPH</th><th>CYL</th>"
                f"<th class='ax'>AXIS</th><th>ADD</th></tr>{_r_row}{_l_row}</table>"
                f"<div class='st-bc' style='margin-top:1mm'>{_bc_html(_ono_c, ht=14)}</div>"
                f"</div>"
                f"<div style='page-break-after:always'></div>"
            )

    if _is_card:
        _pw, _ph = "85mm", "54mm"
        _card_css = """
    .card{box-sizing:border-box;width:85mm;height:54mm;padding:3.5mm 5mm 2.5mm;
          background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 60%,#0f172a 100%);
          color:#fff;display:flex;flex-direction:column;position:relative}
    .top-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.5mm}
    .badge{font-size:5.5pt;color:#a78bfa;font-weight:700;letter-spacing:.1em;text-transform:uppercase}
    .logo{font-size:14pt;opacity:.4;color:#a78bfa}
    .cname{font-size:11pt;font-weight:700;color:#f1f5f9;margin-bottom:0.5mm;line-height:1.1}
    .mobile{font-size:7pt;color:#94a3b8;margin-bottom:1.5mm}
    table{border-collapse:collapse;width:100%;font-size:7pt;margin-bottom:1.5mm}
    tr.hdr{background:rgba(255,255,255,.1)}
    th{padding:.8mm 1.5mm;text-align:center;color:#94a3b8;font-weight:600;font-size:6pt}
    td{color:#e2e8f0;padding:.8mm 1.5mm;text-align:center;border-bottom:.3mm solid rgba(255,255,255,.08)}
    td.ey{color:#64748b;text-align:left;font-weight:700}
    td.ax,th.ax{color:#fde68a;font-weight:900}
    .det{font-size:6pt;color:#94a3b8;margin-bottom:1mm}
    .lbl{color:#64748b}
    .bc-row{margin-top:auto;border-top:.3mm solid rgba(255,255,255,.15);padding-top:1mm}"""
    else:
        _pw, _ph = "75mm", "65mm"
        _card_css = """
    .sticker{box-sizing:border-box;width:75mm;height:65mm;padding:3mm 4mm;
             background:#fff;border:1.5px solid #000;display:flex;flex-direction:column}
    .st-name{font-size:9pt;font-weight:900;color:#0f172a;margin-bottom:.5mm}
    .st-ref{font-size:6pt;color:#475569;margin-bottom:1.5mm;font-family:monospace}
    table.st-tbl{border-collapse:collapse;width:100%;font-size:8pt;margin-bottom:1.5mm}
    tr.hdr{background:#0f172a}
    th{padding:1mm 1.5mm;text-align:center;color:#fff;font-weight:600;font-size:6.5pt}
    td{color:#0f172a;padding:1mm 1.5mm;text-align:center;border-bottom:.3mm solid #e2e8f0}
    td.ey{color:#475569;text-align:left;font-weight:700}
    td.ax,th.ax{color:#b45309;font-weight:900}
    .st-bc{text-align:center}"""

    _html = (f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
             f"@page{{size:{_pw} {_ph};margin:0}}"
             f"body{{margin:0;padding:0;font-family:Arial,Helvetica,sans-serif}}"
             f"{_card_css}"
             f".no-print{{display:none}}@media print{{.no-print{{display:none!important}}}}"
             f"</style></head><body>{_cards_html}"
             f"<div class='no-print' style='text-align:center;padding:20px'>"
             f"<button onclick='window.print()'"
             f" style='background:#6366f1;color:#fff;border:none;padding:10px 32px;"
             f"border-radius:8px;font-size:.95rem;font-weight:700;cursor:pointer'>"
             f"Print / Save PDF</button></div></body></html>")

    _b64_html = _b64.b64encode(_html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}'),],{{type:'text/html'}});"
        f"window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )
    st.success(f"&#10003; {len(orders_ec)} card(s) sent to print")


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
                "OR LOWER(COALESCE(o.patient_name,o.party_name,'')) LIKE %(ord)s)"
            )
            _po_params["ord"] = f"%{_po_ord_flt.strip().lower()}%"
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
            "WHERE soi.customer_line_id::uuid = ol.id AND so.status NOT IN ('CANCELLED','VOID'))"
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
             LEFT JOIN supplier_order_items soi ON soi.customer_line_id = ol.id::text
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
    except Exception:
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
                            %(clid)s::uuid
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
            except Exception:
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
        _svc_gst_rate = st.selectbox("GST Rate", [0, 5, 12, 18],
                                      format_func=lambda x: "No GST" if x==0 else f"{x}%",
                                      index=2, key="inv_svc_gst_rate",
                                      help="5% for courier, 12% for goods, 18% for services")
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
    _goods_gst = round(_goods_subtotal * 0.12, 2)
    _t3.metric(
        f"Goods GST 12% ({'IGST' if _is_igst else 'CGST+SGST'}) — input credit",
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
        SELECT id, supplier_order_id, supplier_name, customer_order_id,
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
                       ordered_qty, received_qty, unit_price, item_status
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
                        except Exception:
                            pass
                    st.caption(
                        f"#{_it.get('item_no')} · {_it.get('product_name','')} "
                        f"{str(_it.get('eye_side','')).upper()} {_pwr} · "
                        f"Ordered: {_it.get('ordered_qty',0)} · "
                        f"Received: {_it.get('received_qty',0)} · "
                        f"₹{float(_it.get('unit_price',0)):,.0f} · "
                        f"{_it.get('item_status','PENDING')}"
                    )

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
            " OR LOWER(COALESCE(pa.order_no,'')) LIKE %(flt)s)"
        )
        _params["flt"] = f"%{_pa_sup_flt.strip().lower()}%"
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
                except Exception:
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


def render_production_page():
    _init_production_state()

    if st.session_state.prod_view_mode == "order":
        _render_production_order()
    else:
        _render_production_list()
