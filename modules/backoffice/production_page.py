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

from modules.backoffice.production_shared import (
    _parse_lp_safe,
    _fmt_pwr_line,
    _init_production_state,
    _q,
    _load_production_orders,
    _load_stage_timeline,
    _fetch_order_for_panel,
    _load_pipeline_overview,
    _render_pipeline_cards,
    _go_to_billing,
    _check_purchase_acked,
    _power_str,
    _sync_supplier_orders_id_sequence,
)
import streamlit as st
import datetime
from typing import List, Dict, Optional


def _scan_norm(value: str) -> str:
    s = "".join(ch for ch in str(value or "") if ch.isalnum()).lower()
    if s.startswith("o") and len(s) > 1:
        s = s[1:]
    return s


def _scan_match(needle: str, hay: str) -> bool:
    raw = str(needle or "").strip().lower()
    if not raw:
        return True
    if raw in str(hay or "").lower():
        return True
    n = _scan_norm(raw)
    h = _scan_norm(hay)
    return bool(n and h and (n in h or h in n))


# ==================================================
# SESSION STATE
# ==================================================

# ══════════════════════════════════════════════════════════════════════
# SHARED MODULE-LEVEL HELPERS — used by all pipelines to avoid duplication
# ══════════════════════════════════════════════════════════════════════

import json as _shared_json

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


def _render_production_overview() -> None:
    counts = _load_pipeline_overview()
    with st.container(border=True):
        h1, h2 = st.columns([6, 1])
        with h1:
            st.markdown("### Production Control")
            st.caption("Orders move from supplier / lab / in-house / stock checks into billing once all gates are complete.")
        with h2:
            if st.button("Refresh", key="prod_overview_refresh", use_container_width=True):
                st.session_state.prod_orders_loaded = False
                st.rerun()

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Supplier", counts.get("supplier_orders", 0))
        m2.metric("In-house", counts.get("inhouse_orders", 0))
        m3.metric("External Lab", counts.get("lab_orders", 0))
        m4.metric("Stock Check", counts.get("stock_orders", 0))
        m5.metric("Open Jobs", counts.get("open_jobs", 0))
        # Strict bill-ready count. The "Ready Jobs" label used to include
        # packing + fitting-done + closed which inflated the number and gave
        # staff false confidence that billing was unlocked. Closed/packing
        # are still surfaced as the delta below, but they no longer share
        # the headline number with bill-ready.
        _bill_ready_n = counts.get("bill_ready_jobs", 0)
        _packing_n    = counts.get("completed_or_packing_jobs", 0)
        m6.metric(
            "Bill-Ready Jobs",
            _bill_ready_n,
            delta=(f"+{_packing_n} packing/done" if _packing_n else None),
            delta_color="off",
        )


@st.cache_data(ttl=20, show_spinner=False)
def _load_dashboard_kpis() -> dict:
    """Fast KPI summary for production dashboard. Read-only and cacheable."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            WITH line_scope AS (
                SELECT
                    o.id AS order_id,
                    o.created_at::date AS order_date,
                    UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) AS route,
                    COALESCE(jm.current_stage, ol.lens_params->>'supplier_stage', '') AS stage,
                    COALESCE(jm.is_closed, FALSE) AS is_closed
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                LEFT JOIN job_master jm ON jm.order_line_id = ol.id
                WHERE COALESCE(ol.is_deleted,FALSE)=FALSE
                  AND COALESCE(ol.is_service_line,FALSE)=FALSE
                  AND o.status NOT IN ('CANCELLED','CLOSED')
                  AND o.created_at::date >= CURRENT_DATE - INTERVAL '60 days'
            ), pending_purchase AS (
                SELECT COUNT(*) AS n
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE COALESCE(ol.is_deleted,FALSE)=FALSE
                  AND COALESCE(ol.is_service_line,FALSE)=FALSE
                  AND EXISTS (
                      SELECT 1 FROM challan_lines cl
                      JOIN challans c ON c.id=cl.challan_id
                      WHERE cl.order_line_id=ol.id AND c.status NOT IN ('CANCELLED','VOID')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM purchase_acknowledgements pa
                      WHERE pa.order_line_id=ol.id AND COALESCE(pa.purchase_price,0)>0
                  )
            )
            SELECT
                COUNT(DISTINCT order_id) FILTER (WHERE route='INHOUSE') AS inhouse_orders,
                COUNT(DISTINCT order_id) FILTER (WHERE route IN ('VENDOR','SUPPLIER')) AS supplier_orders,
                COUNT(DISTINCT order_id) FILTER (WHERE route='EXTERNAL_LAB') AS external_lab_orders,
                COUNT(DISTINCT order_id) FILTER (WHERE route='STOCK') AS stock_orders,
                COUNT(*) FILTER (WHERE stage IN ('READY_TO_BILL','READY_FOR_BILLING')) AS ready_lines,
                COUNT(*) FILTER (WHERE stage = 'READY_FOR_PACK') AS packing_lines,
                COUNT(*) FILTER (WHERE stage='REJECTED') AS rejected_lines,
                (SELECT n FROM pending_purchase) AS procurement_pending
            FROM line_scope
        """, {}) or []
        return dict(rows[0]) if rows else {}
    except Exception:
        return {}


def _render_realtime_production_dashboard() -> None:
    """Lightweight live dashboard. Heavy data is cached; refresh clears cache."""
    st.markdown("### 📊 Production Dashboard")
    st.caption("Cached live view. Use Refresh when you need current counts immediately.")
    c1, c2 = st.columns([6, 1])
    with c2:
        if st.button("Refresh", key="dash_refresh", use_container_width=True):
            try:
                _load_dashboard_kpis.clear()
                _load_pipeline_overview.clear()
            except Exception:
                pass
            st.rerun()
    k = _load_dashboard_kpis()
    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("In-house", int(k.get("inhouse_orders") or 0))
    m2.metric("Supplier", int(k.get("supplier_orders") or 0))
    m3.metric("External Lab", int(k.get("external_lab_orders") or 0))
    m4.metric("Stock", int(k.get("stock_orders") or 0))
    # "Ready to Bill" is strict — only stages that open billing. READY_FOR_PACK
    # is the packing step (pre-billing) and shows separately as the delta below.
    _bill_ready = int(k.get("ready_lines") or 0)
    _packing    = int(k.get("packing_lines") or 0)
    m5.metric(
        "Ready to Bill",
        _bill_ready,
        delta=(f"+{_packing} packing" if _packing else None),
        delta_color="off",
    )
    m6.metric("Rejected", int(k.get("rejected_lines") or 0))
    m7.metric("Procurement Pending", int(k.get("procurement_pending") or 0))

    st.info("Speed change applied: only the selected pipeline panel loads now. Other tabs do not run queries until opened.")


def _render_production_list():
    st.markdown("## 🏭 Production Pipeline")
    _render_production_overview()

    # Service-only punching/backoffice charges do not always have a normal
    # lens product line. Bootstrap lightweight production/fitting jobs so they
    # appear in the correct panels before staff starts advancing work.
    try:
        _bootstrap_service_production_jobs()
    except Exception:
        pass

    # Ultra-fast loading: Streamlit tabs render all bodies eagerly.
    # This navigator renders only the selected panel, so heavy queries run only on demand.
    _pending_panel = st.session_state.pop("_prod_lazy_panel_next", None)
    if _pending_panel:
        st.session_state["prod_lazy_panel"] = _pending_panel
    _panel_alias = {
        "🛒 Stock Procurement": "📊 Inventory Movement",
        "🛍 Procurement RX": "📥 Procurement Queue",
        "📦 Stock": "📦 Stock Repl.",
    }
    if st.session_state.get("prod_lazy_panel") in _panel_alias:
        st.session_state["prod_lazy_panel"] = _panel_alias[st.session_state["prod_lazy_panel"]]
    _panel_options = [
        "📊 Dashboard",
        "🏭 Supplier",
        "🔬 In-house Lab",
        "🧪 External Supplier",
        "📦 Stock Repl.",
        "📥 Procurement Queue",
        "📊 Inventory Movement",
        "📑 PO Reports",
        "💳 Ready Billing",
        "🧫 Blank Repl.",
        "📦 Procurement Analytics",
        "📄 Invoice Match",
        "🏷️ Authenticity Cards",
        "🗑️ Rejection Bin",
    ]
    if st.session_state.get("prod_lazy_panel") not in _panel_options:
        st.session_state["prod_lazy_panel"] = "📊 Dashboard"
    _view = st.radio(
        "Pipeline View",
        _panel_options,
        horizontal=True,
        key="prod_lazy_panel",
        label_visibility="collapsed",
    )

    if _view == "📊 Dashboard":
        _render_realtime_production_dashboard()

    elif _view == "🏭 Supplier":
        from modules.backoffice.supplier_pipeline import render_supplier_pipeline
        render_supplier_pipeline(route_filter="VENDOR")

    elif _view == "🧪 External Supplier":
        from modules.backoffice.supplier_pipeline import render_supplier_pipeline
        render_supplier_pipeline(route_filter="EXTERNAL_LAB")

    elif _view == "📦 Stock Repl.":
        from modules.backoffice.stock_pipeline import render_stock_pipeline
        render_stock_pipeline()

    elif _view == "🧫 Blank Repl.":
        try:
            try:
                from modules.backoffice.replenishment_panel import render_blank_replenishment_summary
            except ImportError:
                try:
                    from replenishment_panel import render_blank_replenishment_summary
                except ImportError as _imp_e:
                    st.error(
                        f"replenishment_panel.py not found. "
                        f"Place it at: modules/backoffice/replenishment_panel.py\n{_imp_e}"
                    )
                    st.stop()
            render_blank_replenishment_summary()
        except Exception as _blank_repl_err:
            st.error(f"Blank replenishment error: {_blank_repl_err}")

    elif _view == "📥 Procurement Queue":
        try:
            from modules.backoffice.procurement_queue import render_procurement_queue
            render_procurement_queue()
        except Exception as _prq_e:
            st.error(f"Procurement Queue failed to load: {_prq_e}")

    elif _view == "🛒 Stock Procurement":
        from modules.backoffice.stock_pipeline import render_stock_procurement
        render_stock_procurement()

    elif _view in ("📊 Inventory Movement", "✅ Procured"):
        from modules.backoffice.stock_pipeline import render_stock_procurement
        render_stock_procurement(procured_view=True)

    elif _view == "📑 PO Reports":
        try:
            from modules.backoffice.po_reports import render_po_reports
            render_po_reports()
        except Exception as _por_e:
            st.error(f"PO Reports failed: {_por_e}")

    elif _view == "💳 Ready Billing":
        try:
            from modules.backoffice.ready_billing_panel import render_ready_billing_panel
            render_ready_billing_panel()
        except Exception as _rb_e:
            st.error(f"Ready Billing failed: {_rb_e}")

    elif _view == "📦 Procurement Analytics":
        try:
            from modules.backoffice.procurement_queue import render_procurement_analytics
            render_procurement_analytics()
        except Exception as _pra_e:
            st.error(f"Procurement Analytics failed to load: {_pra_e}")

    elif _view == "🔬 In-house Lab":
        from modules.backoffice.inhouse_pipeline import render_inhouse_pipeline
        render_inhouse_pipeline()

    elif _view == "📄 Invoice Match":
        try:
            from modules.procurement.invoice_match_ui import render_invoice_match_ui
            render_invoice_match_ui()
        except Exception as _im_e:
            st.error(f"Invoice Match failed to load: {_im_e}")
            import traceback
            st.code(traceback.format_exc())

    elif _view == "🏷️ Authenticity Cards":
        _render_authenticity_cards_tab()

    elif _view == "🗑️ Rejection Bin":
        _render_rejection_bin_tab()


# ──────────────────────────────────────────────────────────────
# REJECTION BIN TAB
# ──────────────────────────────────────────────────────────────

def _render_rejection_bin_tab() -> None:
    """
    Shows all items in the production rejection bin.
    Status lifecycle: IN_BIN → SCRAPPED | REWORKED | RETURNED_TO_STOCK
    """
    import datetime as _rdt
    try:
        from modules.sql_adapter import run_query as _rq, run_write as _rw
    except ImportError:
        st.error("sql_adapter not available")
        return

    st.markdown("### 🗑️ Production Rejection Bin")
    st.caption(
        "Rejected lenses waiting for disposal decision. "
        "Mark each item as Scrapped, Reworked, or — if rejection was before "
        "surfacing — returned to blank stock."
    )

    # ── Filters ───────────────────────────────────────────────────────────────
    with st.expander("🔍 Filters", expanded=True):
        rf1, rf2, rf3 = st.columns(3)
        today   = _rdt.date.today()
        rf_from = rf1.date_input("From", value=today - _rdt.timedelta(days=30),
                                  key="rjb_from")
        rf_to   = rf2.date_input("To",   value=today, key="rjb_to")
        rf_status = rf3.multiselect(
            "Status",
            ["IN_BIN", "SCRAPPED", "REWORKED", "RETURNED_TO_STOCK"],
            default=["IN_BIN"],
            key="rjb_status",
        )

    rows = _rq("""
        SELECT
            rb.id::text,
            rb.job_id::text,
            rb.order_line_id::text,
            rb.order_id::text,
            rb.blank_id::text,
            rb.eye_side,
            rb.qty,
            rb.reason,
            rb.rejected_by,
            rb.rejected_at::text        AS rejected_at,
            rb.status,
            rb.status_remarks,
            rb.status_changed_at::text  AS status_changed_at,
            rb.status_changed_by,
            -- Order info
            COALESCE(o.order_no, '')                           AS order_no,
            COALESCE(o.party_name, o.patient_name, '')        AS party_name,
            o.created_at::date::text                           AS order_date,
            -- Blank info
            COALESCE(bi.brand, '')  AS blank_brand,
            COALESCE(bi.material,'') AS blank_material,
            COALESCE(bi.add_power::text,'') AS add_power,
            -- Product/power from order line
            COALESCE(ol.sph::text,'')  AS sph,
            COALESCE(ol.cyl::text,'')  AS cyl,
            COALESCE(ol.axis::text,'') AS axis,
            COALESCE(ol.lens_params->>'product_name',
                     p.product_name, '') AS product_name,
            COALESCE(ol.lens_params->>'coating','') AS coating,
            COALESCE(ol.lens_params->>'lens_index','') AS lens_index
        FROM production_rejection_bin rb
        LEFT JOIN orders o       ON o.id  = rb.order_id
        LEFT JOIN order_lines ol ON ol.id = rb.order_line_id
        LEFT JOIN products p     ON p.id  = ol.product_id
        LEFT JOIN blank_inventory bi ON bi.id = rb.blank_id
        WHERE rb.rejected_at::date BETWEEN %(df)s AND %(dt)s
          AND rb.status = ANY(%(statuses)s)
        ORDER BY rb.rejected_at DESC
        LIMIT 200
    """, {
        "df": rf_from.isoformat(),
        "dt": rf_to.isoformat(),
        "statuses": rf_status or ["IN_BIN"],
    }) or []

    if not rows:
        st.info("✅ No items in rejection bin for the selected filters.")
        return

    # ── Summary metrics ────────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Items",  len(rows))
    m2.metric("IN BIN",       sum(1 for r in rows if r["status"] == "IN_BIN"))
    m3.metric("Scrapped",     sum(1 for r in rows if r["status"] == "SCRAPPED"))
    m4.metric("Reworked",     sum(1 for r in rows if r["status"] == "REWORKED"))
    st.markdown("---")

    # ── Per-item cards ─────────────────────────────────────────────────────────
    for row in rows:
        rid        = row["id"]
        order_no   = row["order_no"] or "—"
        party      = row["party_name"] or "—"
        eye        = row["eye_side"] or "—"
        reason     = row["reason"] or "—"
        rej_at     = str(row["rejected_at"] or "")[:16]
        status     = row["status"] or "IN_BIN"
        blank_desc = " ".join(filter(None, [
            row.get("blank_brand"), row.get("blank_material"),
            ("Add " + row["add_power"]) if row.get("add_power") else "",
        ])) or "—"
        pwr_parts = []
        if row.get("sph"):  pwr_parts.append(f"SPH {float(row['sph']):+.2f}")
        if row.get("cyl"):  pwr_parts.append(f"CYL {float(row['cyl']):+.2f}")
        if row.get("axis"): pwr_parts.append(f"AX {int(float(row['axis']))}")
        pwr_str    = "  ".join(pwr_parts) or "—"
        spec_parts = [row.get("lens_index",""), row.get("coating","")]
        spec_str   = "  ·  ".join(s for s in spec_parts if s) or ""
        pname      = row.get("product_name") or "—"

        status_clr = {
            "IN_BIN":            "#f59e0b",
            "SCRAPPED":          "#ef4444",
            "REWORKED":          "#10b981",
            "RETURNED_TO_STOCK": "#3b82f6",
        }.get(status, "#64748b")

        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 3, 3])
            with c1:
                eye_icon = {"R":"👁️R","L":"👁️L","S":"⚙️"}.get(eye.upper(), eye)
                st.markdown(
                    f"<div style='font-weight:700;color:#e2e8f0;font-size:0.85rem'>"
                    f"{eye_icon}  {pname}"
                    f"</div>"
                    f"<div style='font-size:0.72rem;color:#a5b4fc;margin-top:2px'>"
                    f"💊 {pwr_str}"
                    f"</div>"
                    + (f"<div style='font-size:0.68rem;color:#67e8f9'>🔬 {spec_str}</div>" if spec_str else "")
                    + f"<div style='font-size:0.68rem;color:#64748b;margin-top:2px'>"
                    f"📦 {order_no}  ·  {party}  ·  {rej_at}</div>"
                    f"<div style='font-size:0.68rem;color:#94a3b8'>Blank: {blank_desc}</div>",
                    unsafe_allow_html=True,
                )
            with c2:
                st.markdown(
                    f"<div style='font-size:0.75rem;color:#f87171'>"
                    f"🚫 Reason: {reason}</div>",
                    unsafe_allow_html=True,
                )
                if row.get("status_remarks"):
                    st.caption(f"Note: {row['status_remarks']}")
            with c3:
                st.markdown(
                    f"<span style='background:{status_clr}22;border:1px solid {status_clr}55;"
                    f"color:{status_clr};padding:3px 10px;border-radius:10px;"
                    f"font-size:0.72rem;font-weight:700'>{status}</span>",
                    unsafe_allow_html=True,
                )

            # ── Status update (only for IN_BIN items) ────────────────────────
            if status == "IN_BIN":
                with st.expander("📝 Update Disposition", expanded=False):
                    da1, da2 = st.columns(2)
                    new_status = da1.selectbox(
                        "Disposition",
                        ["SCRAPPED", "REWORKED", "RETURNED_TO_STOCK"],
                        key=f"rjb_disp_{rid}",
                        help="SCRAPPED = discard. REWORKED = sent for recoating/repair. "
                             "RETURNED_TO_STOCK = pre-surfacing rejection only.",
                    )
                    note = da2.text_input(
                        "Remarks",
                        key=f"rjb_note_{rid}",
                        placeholder="e.g. sent to scrap bin, recoated at ARC lab…",
                    )
                    changed_by = st.session_state.get("user_name", "staff")
                    if st.button(
                        f"✅ Mark as {new_status}",
                        key=f"rjb_save_{rid}",
                        type="primary",
                    ):
                        try:
                            _rw("""
                                UPDATE production_rejection_bin
                                SET status            = %(s)s,
                                    status_remarks    = %(n)s,
                                    status_changed_at = NOW(),
                                    status_changed_by = %(by)s
                                WHERE id = %(id)s::uuid
                            """, {
                                "s":  new_status,
                                "n":  note.strip(),
                                "by": changed_by,
                                "id": rid,
                            })
                            st.success(f"✅ Marked as {new_status}")
                            st.rerun()
                        except Exception as _ue:
                            st.error(f"Update failed: {_ue}")

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
            ["CR80 Plastic Card", "75×50 TSC Customer Label"],
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
        _hay = " ".join(
            [str(_r.get(k, "")) for k in ("order_no", "party_name", "patient_name")]
            + [str(_ec.get("name", "")), str(_ec.get("mobile", ""))]
        )
        if _auth_search and not _scan_match(_auth_search, _hay):
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
            st.caption("Correct spelling or add missing customer details. Use Save to DB if the correction should be remembered.")
            _name_overrides = {}
            _mobile_overrides = {}
            _optician_overrides = {}
            for _r2, _ec2 in _selected_rows:
                _ono2 = _r2["order_no"]
                _default_name = _ec2.get("name") or _r2.get("patient_name") or ""
                _default_mobile = _ec2.get("mobile") or _r2.get("patient_mobile") or ""
                _default_optician = _r2.get("party_name") or ""
                _e1, _e2, _e3 = st.columns([2, 1.2, 2])
                _name_overrides[_ono2] = _e1.text_input(
                    f"{_ono2} — Customer",
                    value=_default_name,
                    key=f"auth_name_edit_{_ono2}",
                    placeholder="Customer name on card"
                )
                _mobile_overrides[_ono2] = _e2.text_input(
                    "Mobile",
                    value=_default_mobile,
                    key=f"auth_mobile_edit_{_ono2}",
                    placeholder="Mobile"
                )
                _optician_overrides[_ono2] = _e3.text_input(
                    "Optician / Party",
                    value=_default_optician,
                    key=f"auth_optician_edit_{_ono2}",
                    placeholder="Optician / Party"
                )
            st.session_state["auth_name_overrides"] = _name_overrides
            st.session_state["auth_mobile_overrides"] = _mobile_overrides
            st.session_state["auth_optician_overrides"] = _optician_overrides

            if st.button("💾 Save Edited Details to DB", key="auth_save_edits", use_container_width=True):
                _save_auth_card_edits(_selected_rows, _name_overrides, _mobile_overrides, _optician_overrides)
                st.rerun()

    _sb1, _sb2, _sb3, _sb4 = st.columns([1, 1, 2, 2])
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
    with _sb4:
        if st.button(f"🏷️ Direct 75×50 {len(st.session_state[_sel_key])}",
                     key="auth_print_75_btn", use_container_width=True,
                     disabled=not st.session_state[_sel_key]):
            st.session_state["auth_force_75"] = True
            st.session_state["auth_do_print_75"] = True

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

    if st.session_state.pop("auth_do_print", False) or st.session_state.pop("auth_do_print_75", False):
        _to_print = [(r, ec) for r, ec in _filtered if r["order_no"] in st.session_state[_sel_key]]
        # Apply name overrides
        _overrides = st.session_state.get("auth_name_overrides", {})
        _mobile_overrides = st.session_state.get("auth_mobile_overrides", {})
        _optician_overrides = st.session_state.get("auth_optician_overrides", {})
        _to_print_with_names = []
        for _r3, _ec3 in _to_print:
            _ono3 = _r3["order_no"]
            _ec3_copy = dict(_ec3)
            if _ono3 in _overrides and _overrides[_ono3].strip():
                _ec3_copy["name"] = _overrides[_ono3].strip()
            elif not _ec3_copy.get("name"):
                _ec3_copy["name"] = _r3.get("patient_name") or ""
            if _ono3 in _mobile_overrides:
                _ec3_copy["mobile"] = _mobile_overrides[_ono3].strip()
            if _ono3 in _optician_overrides:
                _ec3_copy["optician"] = _optician_overrides[_ono3].strip()
            _to_print_with_names.append((_r3, _ec3_copy))
        _render_auth_card_direct_print(_to_print_with_names, "75×50 TSC Customer Label" if st.session_state.get("auth_force_75") else _sticker_fmt)
        st.session_state.pop("auth_force_75", None)


def _save_auth_card_edits(selected_rows: list, names: dict, mobiles: dict, opticians: dict) -> None:
    import json as _json
    try:
        from modules.sql_adapter import run_write as _rw_auth_save
        for _r, _ec in selected_rows:
            _ono = _r.get("order_no")
            if not _ono:
                continue
            _name = str(names.get(_ono, "") or "").strip()
            _mobile = str(mobiles.get(_ono, "") or "").strip()
            _optician = str(opticians.get(_ono, "") or "").strip()
            _payload = {
                "name": _name,
                "mobile": _mobile,
                "optician": _optician,
            }
            _rw_auth_save(
                """
                UPDATE orders
                SET extra_data = jsonb_set(
                    jsonb_set(
                        COALESCE(extra_data, '{}'::jsonb),
                        '{end_customer}',
                        COALESCE(extra_data->'end_customer', '{}'::jsonb) || %(ec)s::jsonb,
                        TRUE
                    ),
                    '{auth_card}',
                    COALESCE(extra_data->'auth_card', '{}'::jsonb) || %(ac)s::jsonb,
                    TRUE
                ),
                patient_name = COALESCE(NULLIF(%(name)s,''), patient_name),
                patient_mobile = COALESCE(NULLIF(%(mobile)s,''), patient_mobile)
                WHERE order_no = %(ono)s
                """,
                {
                    "ono": _ono,
                    "name": _name,
                    "mobile": _mobile,
                    "ec": _json.dumps({"name": _name, "mobile": _mobile}),
                    "ac": _json.dumps(_payload),
                },
            )
        st.success("Saved authenticity card details to DB")
    except Exception as _save_e:
        st.error(f"Could not save authenticity details: {_save_e}")


def _auth_card_lines(order_no: str) -> list[dict]:
    try:
        from modules.sql_adapter import run_query as _rq_auth_lines
        return _rq_auth_lines(
            """
            SELECT ol.id::text AS id, ol.id::text AS line_id, ol.order_id::text AS order_id,
                   o.order_no, o.patient_name, o.patient_mobile, o.party_name, o.order_type,
                   ol.eye_side, ol.sph, ol.cyl, ol.axis, ol.add_power,
                   ol.lens_params, ol.production_ref,
                   COALESCE(p.product_name, ol.lens_params->>'product_name', '') AS product_name,
                   p.brand, p.index_value, p.coating, p.coating_type, p.material
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE o.order_no = %(ono)s
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_service_line, FALSE) = FALSE
              AND UPPER(COALESCE(ol.eye_side,'')) IN ('R','L','RIGHT','LEFT')
            ORDER BY CASE WHEN UPPER(COALESCE(ol.eye_side,'')) IN ('R','RIGHT') THEN 0 ELSE 1 END, ol.id
            """,
            {"ono": order_no},
        ) or []
    except Exception:
        return []


def _render_auth_card_direct_print(orders_ec: list, fmt: str):
    import datetime as _dt
    try:
        from modules.backoffice.production_panel import (
            _build_cr80_page,
            _build_customer_75x50_page,
            _open_print_window,
            _product_display_for_card,
        )
    except Exception as _imp_e:
        st.error(f"Production print functions unavailable: {_imp_e}")
        return

    is_75 = "75" in str(fmt)
    printed = 0
    for _r, _ec in orders_ec:
        _ono = _r.get("order_no", "")
        _lines = _auth_card_lines(_ono)
        _r_line = next((l for l in _lines if str(l.get("eye_side", "")).upper()[:1] == "R"), None)
        _l_line = next((l for l in _lines if str(l.get("eye_side", "")).upper()[:1] == "L"), None)
        _name = str(_ec.get("name") or "").strip()
        _mobile = str(_ec.get("mobile") or "").strip()
        _optician = str(_ec.get("optician") or _r.get("party_name") or "").strip()
        _order = {
            "id": _r.get("id", ""),
            "order_no": _ono,
            "patient_name": _name,
            "patient_mobile": _mobile,
            "party_name": _optician,
            "order_type": _r.get("order_type") or "RETAIL",
            "lines": _lines,
        }
        if is_75:
            try:
                from modules.printing.label_printer import print_tspl_customer_label

                def _rx(_ln):
                    _ln = _ln or {}
                    return {
                        "sph": _ln.get("sph"),
                        "cyl": _ln.get("cyl"),
                        "axis": _ln.get("axis"),
                        "add": _ln.get("add_power"),
                    }

                _product = _product_display_for_card(_r_line or _l_line or {})
                ok, msg = print_tspl_customer_label(
                    order_no=_ono,
                    customer=_name,
                    optician=_optician,
                    product=_product,
                    rx_r=_rx(_r_line),
                    rx_l=_rx(_l_line),
                    mobile=_mobile,
                    date_text=_dt.date.today().strftime("%d-%m-%Y"),
                    copies=1,
                )
                if ok:
                    printed += 1
                    st.success(f"{_ono}: sent 75×50 card to TSC")
                else:
                    st.warning(f"{_ono}: TSC direct failed: {msg}. Opening HTML standby.")
                    _open_print_window(_build_customer_75x50_page(_r_line, _l_line, _order))
            except Exception as _e75:
                st.warning(f"{_ono}: 75×50 direct failed: {_e75}. Opening HTML standby.")
                _open_print_window(_build_customer_75x50_page(_r_line, _l_line, _order))
        else:
            _html = _build_cr80_page(_r_line, _l_line, _order)
            try:
                from modules.printing.direct_print import spool_html_to_printer
                from modules.printing.internal_print_config import EVOLIS_CARD_PRINTER
                ok, msg = spool_html_to_printer(_html, EVOLIS_CARD_PRINTER, job_name=f"CR80_{_ono}")
                if ok:
                    printed += 1
                    st.success(f"{_ono}: {msg}")
                else:
                    st.warning(f"{_ono}: CR80 direct failed: {msg}. Opening HTML standby.")
                    _open_print_window(_html)
            except Exception as _cr_e:
                st.warning(f"{_ono}: CR80 direct failed: {_cr_e}. Opening HTML standby.")
                _open_print_window(_html)
    if printed:
        st.success(f"{printed} direct print job(s) sent")


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


def render_production_page():
    _init_production_state()

    _mode = st.session_state.get("prod_view_mode", "list")

    if _mode == "assign":
        _ono = st.session_state.get("prod_assign_order_no")
        if _ono:
            from modules.backoffice.inhouse_pipeline import render_assignment_workspace
            render_assignment_workspace(_ono)
        else:
            st.session_state["prod_view_mode"] = "list"
            st.rerun()
    elif _mode == "order":
        _render_production_order()
    else:
        _render_production_list()


# ══════════════════════════════════════════════════════════════════════════
# STOCK PROCUREMENT — unified tick-and-purchase screen
# ══════════════════════════════════════════════════════════════════════════
