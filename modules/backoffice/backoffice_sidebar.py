"""
backoffice_sidebar.py — live order status sidebar
"""
import streamlit as st
import datetime
from typing import Dict, List
from modules.flags.feature_flags import SYSTEM_FLAGS

try:
    from modules.backoffice.order_status_live import STATUS_META as _OSL_META
    STATUS_COLOR = {k: v["color"] for k, v in _OSL_META.items()}
    STATUS_ICON  = {k: v["icon"]  for k, v in _OSL_META.items()}
except Exception:
    STATUS_COLOR = {}
    STATUS_ICON  = {}
# Extended with non-order statuses used in sidebar
STATUS_COLOR.update({
    "DRAFT":"#64748b","SENT":"#3b82f6","ACKNOWLEDGED":"#8b5cf6",
    "PARTIAL":"#f59e0b","RECEIVED":"#10b981","JOB_CREATED":"#64748b",
    "IN_PROCESS":"#8b5cf6","FINISHING":"#f59e0b","QC":"#f59e0b",
})
STATUS_ICON.update({
    "DRAFT":"📝","SENT":"📤","ACKNOWLEDGED":"👍","PARTIAL":"⚡",
    "RECEIVED":"📬","JOB_CREATED":"🔧","IN_PROCESS":"⚙️","FINISHING":"✨","QC":"🔍",
})

from modules.backoffice.production_train import render_train_sidebar

def _q(sql, params):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []

def _days_old(dt) -> int:
    if not dt:
        return 0
    try:
        d = dt.date() if hasattr(dt,"date") else datetime.date.fromisoformat(str(dt)[:10])
        return (datetime.date.today() - d).days
    except Exception:
        return 0

def _fetch_supplier_summary(order_no: str) -> Dict:
    rows = _q("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='RECEIVED' THEN 1 ELSE 0 END) AS received,
               SUM(CASE WHEN status='PARTIAL'  THEN 1 ELSE 0 END) AS partial,
               SUM(CASE WHEN status IN ('DRAFT','SENT','ACKNOWLEDGED') THEN 1 ELSE 0 END) AS pending,
               COALESCE(SUM(total_qty),0) AS total_ordered,
               COALESCE(SUM((SELECT SUM(received_qty) FROM supplier_order_items
                             WHERE supplier_order_id=supplier_orders.id)),0) AS total_received
        FROM supplier_orders WHERE customer_order_id=%(ono)s
    """, {"ono": order_no})
    return dict(rows[0]) if rows else {}

def _fetch_production_summary(order_no: str) -> Dict:
    rows = _q("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN is_closed=TRUE THEN 1 ELSE 0 END) AS done,
               MIN(current_stage) AS stage
        FROM job_master jm
        JOIN order_lines ol ON ol.id=jm.order_line_id
        JOIN orders o       ON o.id=ol.order_id
        WHERE o.order_no=%(ono)s
    """, {"ono": order_no})
    return dict(rows[0]) if rows else {}

def _fetch_recent_activity(order_no: str, limit: int = 6) -> List[Dict]:
    try:
        # Guard: only query order_events if the table actually exists.
        # The table may not have been created yet — silently return [] rather
        # than emitting ERROR log spam on every sidebar render.
        table_exists = _q("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name   = 'order_events'
            LIMIT 1
        """, {})
        if not table_exists:
            return []

        # order_events.order_id stores the order UUID, not order_no
        # Look up UUID from order_no first
        id_rows = _q(
            "SELECT id::text FROM orders WHERE order_no=%s LIMIT 1",
            (order_no,)
        )
        if not id_rows:
            return []
        order_uuid = id_rows[0]["id"]
        rows = _q("""
            SELECT event_type, details, timestamp
            FROM order_events WHERE order_id=%(oid)s::uuid
            ORDER BY timestamp DESC LIMIT %(lim)s
        """, {"oid": order_uuid, "lim": limit})
        return [dict(r) for r in rows]
    except Exception:
        return []   # order_events table not yet created or order not found

def _dot(color): return f"<span style='display:inline-block;width:8px;height:8px;background:{color};border-radius:50%;margin-right:5px'></span>"

def _bar(label, pct, color):
    st.markdown(f"<div style='display:flex;justify-content:space-between;font-size:0.72rem;color:#94a3b8;margin-bottom:2px'><span>{label}</span><span style='color:{color};font-weight:700'>{pct}%</span></div>", unsafe_allow_html=True)
    st.progress(pct/100)
    st.markdown("")

def _mini(title):
    st.markdown(f"<div style='font-size:0.65rem;font-weight:800;color:#64748b;letter-spacing:.1em;text-transform:uppercase;margin:14px 0 6px 0;border-bottom:1px solid #1e293b;padding-bottom:3px'>{title}</div>", unsafe_allow_html=True)

def render_backoffice_sidebar(order: Dict) -> None:
    order_no = order.get("order_no") or str(order.get("id",""))
    status   = order.get("status","PENDING")
    party    = order.get("patient_name") or order.get("party_name") or "—"
    days     = _days_old(order.get("created_at"))

    all_lines: List[Dict] = []
    all_lines += order.get("stock_lines",[])
    all_lines += order.get("inhouse_lines",[])
    all_lines += order.get("lab_order_lines",[])

    total_qty = sum(int(l.get("billing_qty") or 0) for l in all_lines)
    alloc_qty = sum(int(l.get("allocated_qty") or 0) for l in all_lines)
    alloc_pct = int(100*alloc_qty/total_qty) if total_qty else 0
    locked    = sum(1 for l in all_lines if l.get("supplier_order_id"))

    sup  = _fetch_supplier_summary(order_no)
    prod = _fetch_production_summary(order_no)

    if status in ("CLOSED","DELIVERED","CANCELLED"):
        urg_color, urg_label = "#10b981", ""
    elif days > 7:
        urg_color, urg_label = "#ef4444", "🔴 URGENT"
    elif days > 3:
        urg_color, urg_label = "#f59e0b", "⚠️ OVERDUE"
    else:
        urg_color, urg_label = "#3b82f6", ""

    sc = STATUS_COLOR.get(status,"#64748b")
    si = STATUS_ICON.get(status,"📋")

    with st.sidebar:
        # Order identity card
        st.markdown(
            f"<div style='background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:10px;padding:12px 14px;margin-bottom:8px;border-left:4px solid {urg_color}'>"
            f"<div style='color:#64748b;font-size:0.62rem;letter-spacing:.1em'>ORDER</div>"
            f"<div style='color:#f1f5f9;font-family:monospace;font-size:1rem;font-weight:800'>{order_no}"
            + (f" <span style='background:{urg_color};color:#fff;padding:1px 5px;border-radius:6px;font-size:0.58rem;font-family:sans-serif'>{urg_label}</span>" if urg_label else "")
            + f"</div><div style='color:#64748b;font-size:0.72rem;margin-top:2px'>{party} · {days}d ago</div></div>",
            unsafe_allow_html=True,
        )

        # Status pill
        st.markdown(
            f"<div style='background:{sc};color:#fff;text-align:center;padding:7px;border-radius:8px;font-weight:700;font-size:0.85rem;letter-spacing:.04em'>{si} {status}</div>",
            unsafe_allow_html=True,
        )
        st.markdown("")

        # ── Production Train ────────────────────────────────────────
        _mini("🚂 Production Flow")
        render_train_sidebar(str(order.get("order_no") or ""))
        st.markdown("")

        # ── Progress bars ──────────────────────────────────────────
        _mini("📊 Progress")

        alloc_c = "#10b981" if alloc_pct==100 else ("#f59e0b" if alloc_pct>0 else "#ef4444")
        _bar(f"{_dot(alloc_c)}Allocation", alloc_pct, alloc_c)

        sup_ordered  = int(sup.get("total_ordered") or 0)
        sup_received = int(sup.get("total_received") or 0)
        sup_pct      = int(100*sup_received/sup_ordered) if sup_ordered else 0
        if sup_ordered:
            sup_c = "#10b981" if sup_pct==100 else ("#f59e0b" if sup_pct>0 else "#64748b")
            _bar(f"{_dot(sup_c)}Supplier POs", sup_pct, sup_c)

        total_jobs = int(prod.get("total") or 0)
        done_jobs  = int(prod.get("done")  or 0)
        job_pct    = int(100*done_jobs/total_jobs) if total_jobs else 0
        if total_jobs:
            job_c = "#10b981" if job_pct==100 else ("#8b5cf6" if job_pct>0 else "#64748b")
            _bar(f"{_dot(job_c)}Job Cards", job_pct, job_c)

        # ── Live status cards ──────────────────────────────────────
        _mini("🚦 Live Status")

        total_pos = int(sup.get("total") or 0)
        if total_pos:
            rcvd = int(sup.get("received") or 0)
            part = int(sup.get("partial")  or 0)
            pend = int(sup.get("pending")  or 0)
            rows_html = ""
            if rcvd: rows_html += f"<div style='color:#10b981;font-size:0.7rem'>✅ {rcvd} received</div>"
            if part: rows_html += f"<div style='color:#f59e0b;font-size:0.7rem'>⚡ {part} partial</div>"
            if pend: rows_html += f"<div style='color:#ef4444;font-size:0.7rem'>⏳ {pend} pending</div>"
            if locked: rows_html += f"<div style='color:#f59e0b;font-size:0.7rem'>🔒 {locked} line(s) locked</div>"
            st.markdown(
                f"<div style='background:#1e293b;border-radius:8px;padding:8px 10px;margin-bottom:6px'>"
                f"<div style='color:#64748b;font-size:0.62rem;margin-bottom:3px'>🏭 SUPPLIER · {total_pos} PO(s)</div>"
                f"{rows_html}</div>",
                unsafe_allow_html=True,
            )

        if total_jobs:
            open_j = total_jobs - done_jobs
            jc = "#8b5cf6" if open_j>0 else "#10b981"
            stage_str = prod.get("stage") or ""
            st.markdown(
                f"<div style='background:#1e293b;border-radius:8px;padding:8px 10px;margin-bottom:6px'>"
                f"<div style='color:#64748b;font-size:0.62rem;margin-bottom:3px'>🔧 JOB CARDS · {total_jobs} total</div>"
                f"<div style='color:{jc};font-size:0.7rem'>{'✅ All complete' if open_j==0 else f'⚙️ {open_j} open · {done_jobs} done'}</div>"
                + (f"<div style='color:#475569;font-size:0.62rem'>Stage: {stage_str}</div>" if stage_str else "")
                + "</div>",
                unsafe_allow_html=True,
            )

        # ── Quick Actions ──────────────────────────────────────────
        _mini("⚡ Quick Actions")

        from modules.utils.submit_guard import is_locked as _islock, guarded_submit
        if st.button("💾 Save Order", use_container_width=True, type="primary", disabled=_islock("save_order")):
            with guarded_submit("save_order") as ok:
                if not ok: st.stop()
                try:
                    from modules.persistence.order_persistence import save_order_to_db
                    save_order_to_db(order)
                    st.success("✅ Saved!")
                    st.rerun()
                except ImportError:
                    st.warning("Save not available")
                except Exception as e:
                    st.error(f"Save failed: {e}")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("📄 Docs", use_container_width=True):
                st.info("→ Documents tab")
        with c2:
            if st.button("📊 Status", use_container_width=True):
                st.info("→ Status tab")

        if SYSTEM_FLAGS.get("advisory_enabled", True):
            st.markdown("")
            if st.button("🛒 Advisory", use_container_width=True):
                st.session_state["bo_view_mode"] = "advisory"
                st.rerun()

        if SYSTEM_FLAGS.get("founder_dashboard_enabled", False):
            if st.button("🏰 Control Tower", use_container_width=True):
                st.session_state["bo_view_mode"] = "founder_dashboard"
                st.rerun()

        # ── Collapsibles ────────────────────────────────────────────
        _mini("🔎 Details")

        with st.expander("🔍 System Health"):
            try:
                from modules.backoffice.backoffice_helpers import run_system_health_check
                h = run_system_health_check(order)
                if h.get("all_checks_passed"):
                    st.success("✅ All clear")
                else:
                    for issue in h.get("issues", []):
                        st.warning(f"⚠️ {issue}")
            except Exception as e:
                st.caption(f"Unavailable: {e}")

        with st.expander("📜 Activity"):
            events = _fetch_recent_activity(order_no)
            if events:
                for ev in events:
                    ts = str(ev.get("timestamp",""))[:16].replace("T"," ")
                    et = ev.get("event_type","")
                    st.markdown(
                        f"<div style='font-size:0.68rem;color:#94a3b8;border-left:2px solid #334155;padding-left:6px;margin-bottom:4px'>"
                        f"<b style='color:#e2e8f0'>{et}</b><br>{ts}</div>",
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("No activity logged yet")

        with st.expander("ℹ️ Order Info"):
            st.caption(f"**Patient:** {order.get('patient_name','N/A')}")
            st.caption(f"**Party:** {order.get('party_name','N/A')}")
            st.caption(f"**Created:** {str(order.get('created_at',''))[:10]}")
            val = sum(float(l.get("billing_total") or 0) for l in all_lines)
            st.caption(f"**Value:** ₹{val:,.2f}")

        st.markdown("")
