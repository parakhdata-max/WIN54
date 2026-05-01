"""
Fitting Panel
=============
Manages frame fitting service for orders.
Two modes:
  INHOUSE   — fitted at shop: PENDING → IN_PROGRESS → DONE
  EXTERNAL  — sent to fitter: PENDING → SENT → RECEIVED → DONE → DISPATCHED

Standalone panel (orders with frame fitting only) AND
embedded in production panel (after READY_FOR_PACK).
"""

import streamlit as st
from datetime import date, timedelta
from typing import Dict, List, Optional

# ── DB helpers ────────────────────────────────────────────────────────────

def _q(sql: str, params: dict = None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB error: {e}")
        return []

def _w(sql: str, params: dict = None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        st.error(f"DB write error: {e}")
        return False

def _fmt(v) -> str:
    try: return f"₹{float(v):,.2f}"
    except: return "₹0.00"

def _fd(v) -> str:
    if not v: return "—"
    try:
        from datetime import datetime
        if isinstance(v, date): return v.strftime("%d %b %Y")
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except: return str(v)[:10]

# ── Status config ─────────────────────────────────────────────────────────

_STATUS_COLOR = {
    "PENDING":   "#f59e0b",
    "IN_PROGRESS": "#3b82f6",
    "SENT":      "#8b5cf6",
    "RECEIVED":  "#0ea5e9",
    "DONE":      "#10b981",
    "DELIVERED": "#6b7280",
    "CANCELLED": "#ef4444",
}

_STATUS_FLOW = {
    "INHOUSE":  ["PENDING", "IN_PROGRESS", "DONE"],
    "EXTERNAL": ["PENDING", "SENT", "RECEIVED", "DONE"],
}

def _next_status(current: str, fitting_type: str) -> Optional[str]:
    flow = _STATUS_FLOW.get(fitting_type, _STATUS_FLOW["INHOUSE"])
    try:
        idx = flow.index(current)
        return flow[idx + 1] if idx + 1 < len(flow) else None
    except ValueError:
        return None

# ── Data fetchers ─────────────────────────────────────────────────────────

def _fetch_fitters(fitting_type: str = None) -> List[Dict]:
    sql = "SELECT id, fitter_name, fitter_type, contact FROM fitters WHERE is_active=TRUE"
    params = {}
    if fitting_type:
        sql += " AND fitter_type = %(ft)s"
        params["ft"] = fitting_type
    sql += " ORDER BY fitter_name"
    return _q(sql, params)

def _fetch_fitting_jobs(status_filter: str = "ALL", order_id: str = None) -> List[Dict]:
    where = ["1=1"]
    params = {}
    if status_filter != "ALL":
        where.append("fj.status = %(st)s")
        params["st"] = status_filter
    if order_id:
        where.append("fj.order_id = %(oid)s::uuid")
        params["oid"] = order_id
    return _q(f"""
        SELECT fj.id, fj.fitting_job_no, fj.fitting_type, fj.status,
               fj.fitter_name, fj.frame_brand, fj.frame_model,
               fj.frame_color, fj.frame_size, fj.frame_notes,
               fj.sent_date, fj.due_date, fj.received_date, fj.done_date,
               fj.fitting_cost, fj.cost_paid, fj.remarks,
               fj.created_at, fj.order_id::text, fj.order_line_id::text,
               o.order_no, o.party_name,
               o.patient_mobile, o.party_mobile,
               f.contact AS fitter_contact
        FROM fitting_jobs fj
        LEFT JOIN orders o  ON o.id  = fj.order_id
        LEFT JOIN fitters f ON f.id  = fj.fitter_id
        WHERE {' AND '.join(where)}
        ORDER BY fj.created_at DESC
        LIMIT 200
    """, params)

def _fetch_orders_needing_fitting() -> List[Dict]:
    """Orders at READY_FOR_PACK with no fitting job yet, or fitting-only orders."""
    return _q("""
        SELECT DISTINCT o.id::text AS order_id, o.order_no, o.party_name,
               o.status, o.created_at
        FROM orders o
        WHERE o.status IN ('READY','READY_FOR_BILLING','CONFIRMED','IN_PRODUCTION')
          AND (
            -- has job_master lines at READY_FOR_PACK
            EXISTS (
                SELECT 1 FROM job_master jm
                JOIN order_lines ol ON ol.id = jm.order_line_id
                WHERE ol.order_id = o.id
                  AND jm.current_stage = 'READY_FOR_PACK'
                  AND jm.is_closed = TRUE
            )
            OR
            -- order has no job_master at all (frame-only / service order)
            NOT EXISTS (
                SELECT 1 FROM job_master jm
                JOIN order_lines ol ON ol.id = jm.order_line_id
                WHERE ol.order_id = o.id
            )
          )
          -- no active fitting job exists yet
          AND NOT EXISTS (
              SELECT 1 FROM fitting_jobs fj
              WHERE fj.order_id = o.id
                AND fj.status NOT IN ('DONE','DELIVERED','CANCELLED')
          )
        ORDER BY o.created_at DESC
    """)

# ── Create fitting job ─────────────────────────────────────────────────────

def _create_fitting_job(
    order_id: str, fitting_type: str, fitter_id: Optional[str],
    fitter_name: str, frame_brand: str, frame_model: str,
    frame_color: str, frame_size: str, frame_notes: str,
    fitting_cost: float, due_date, remarks: str, created_by: str
) -> Optional[str]:
    job_no_rows = _q("SELECT generate_fitting_job_no() AS no", {})
    if not job_no_rows:
        st.error("Could not generate fitting job number")
        return None
    job_no = job_no_rows[0]["no"]
    ok = _w("""
        INSERT INTO fitting_jobs (
            id, fitting_job_no, order_id, fitter_id, fitter_name,
            fitting_type, frame_brand, frame_model, frame_color,
            frame_size, frame_notes, status, due_date,
            fitting_cost, remarks, created_by
        ) VALUES (
            gen_random_uuid(), %(no)s, %(oid)s::uuid,
            %(fid)s::uuid, %(fn)s, %(ft)s,
            %(fbr)s, %(fmo)s, %(fco)s, %(fsz)s, %(fno)s,
            'PENDING', %(dd)s, %(cost)s, %(rmk)s, %(by)s
        )
    """, {
        "no": job_no, "oid": order_id,
        "fid": fitter_id if fitter_id else None,
        "fn": fitter_name, "ft": fitting_type,
        "fbr": frame_brand, "fmo": frame_model,
        "fco": frame_color, "fsz": frame_size, "fno": frame_notes,
        "dd": due_date, "cost": fitting_cost,
        "rmk": remarks, "by": created_by
    })
    if ok:
        # Log stage event
        _w("""
            INSERT INTO fitting_stage_events
                (id, fitting_job_id, stage, remarks, performed_by)
            SELECT gen_random_uuid(), id, 'PENDING', 'Job created', %(by)s
            FROM fitting_jobs WHERE fitting_job_no = %(no)s
        """, {"no": job_no, "by": created_by})
    return job_no if ok else None

# ── Advance fitting status ─────────────────────────────────────────────────

def _advance_fitting(fitting_job_id: str, fitting_type: str,
                     current_status: str, remarks: str = "") -> bool:
    next_st = _next_status(current_status, fitting_type)
    if not next_st:
        st.error("No next stage available")
        return False

    date_field = {
        "SENT":      "sent_date",
        "RECEIVED":  "received_date",
        "DONE":      "done_date",
    }.get(next_st)

    date_sql = f", {date_field} = CURRENT_DATE" if date_field else ""
    ok = _w(f"""
        UPDATE fitting_jobs
        SET status = %(st)s, updated_at = NOW() {date_sql}
        WHERE id = %(id)s::uuid
    """, {"st": next_st, "id": fitting_job_id})

    if ok:
        _w("""
            INSERT INTO fitting_stage_events
                (id, fitting_job_id, stage, remarks, performed_by)
            VALUES (gen_random_uuid(), %(id)s::uuid, %(st)s, %(rmk)s,
                    %(by)s)
        """, {
            "id": fitting_job_id, "st": next_st,
            "rmk": remarks,
            "by": st.session_state.get("user_name", "System")
        })
    return ok

# ── UI Components ─────────────────────────────────────────────────────────

def _render_status_pill(status: str) -> str:
    c = _STATUS_COLOR.get(status, "#64748b")
    return (f"<span style='background:{c}22;color:{c};padding:3px 10px;"
            f"border-radius:10px;font-size:0.7rem;font-weight:700'>{status}</span>")

def _render_create_form(order_id: str = None, order_no: str = ""):
    """Form to create a new fitting job."""
    st.markdown("#### ➕ New Fitting Job" + (f" — {order_no}" if order_no else ""))

    fitters_all = _fetch_fitters()

    c1, c2 = st.columns(2)
    with c1:
        fitting_type = st.selectbox("Fitting Type", ["INHOUSE", "EXTERNAL"],
                                    key=f"ft_type_{order_id}")
    with c2:
        fitter_opts = {str(f["id"]): f"{f['fitter_name']} ({f['fitter_type']})"
                       for f in fitters_all
                       if f["fitter_type"] == fitting_type or fitting_type == "INHOUSE"}
        fitter_opts["__manual__"] = "Enter manually…"
        sel_fitter = st.selectbox("Fitter", options=list(fitter_opts.keys()),
                                  format_func=lambda x: fitter_opts.get(x, x),
                                  key=f"ft_fitter_{order_id}")

    if sel_fitter == "__manual__":
        fitter_name = st.text_input("Fitter Name", key=f"ft_fn_{order_id}")
        fitter_id   = None
    else:
        fitter_name = fitter_opts.get(sel_fitter, "").split(" (")[0]
        fitter_id   = sel_fitter if sel_fitter != "__manual__" else None

    st.markdown("**Frame Details**")
    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1: frame_brand = st.text_input("Brand",  key=f"ft_fbr_{order_id}", placeholder="Ray-Ban")
    with fc2: frame_model = st.text_input("Model",  key=f"ft_fmo_{order_id}", placeholder="RB3025")
    with fc3: frame_color = st.text_input("Colour", key=f"ft_fco_{order_id}", placeholder="Gold")
    with fc4: frame_size  = st.text_input("Size",   key=f"ft_fsz_{order_id}", placeholder="52□18")
    frame_notes = st.text_input("Frame Notes", key=f"ft_fno_{order_id}",
                                placeholder="Rimless, nylor, special drill…")

    cc1, cc2 = st.columns(2)
    with cc1:
        fitting_cost = st.number_input("Fitting Cost (₹)", min_value=0.0,
                                       step=10.0, key=f"ft_cost_{order_id}")
    with cc2:
        due_date = st.date_input("Due Date",
                                 value=date.today() + timedelta(days=2),
                                 key=f"ft_due_{order_id}")

    remarks = st.text_input("Remarks", key=f"ft_rmk_{order_id}", placeholder="Optional…")

    if st.button("📐 Create Fitting Job", type="primary",
                 use_container_width=True, key=f"ft_create_{order_id}"):
        if not fitter_name:
            st.error("Please select or enter a fitter name")
            return
        job_no = _create_fitting_job(
            order_id=order_id, fitting_type=fitting_type,
            fitter_id=fitter_id, fitter_name=fitter_name,
            frame_brand=frame_brand, frame_model=frame_model,
            frame_color=frame_color, frame_size=frame_size,
            frame_notes=frame_notes, fitting_cost=fitting_cost,
            due_date=due_date, remarks=remarks,
            created_by=st.session_state.get("user_name", "System")
        )
        if job_no:
            st.success(f"✅ Fitting job **{job_no}** created!")
            st.rerun()


def _render_job_card(fj: Dict, compact: bool = False):
    """Render a single fitting job card with stage advance."""
    fj_id       = str(fj["id"])
    status      = fj.get("status", "PENDING")
    fit_type    = fj.get("fitting_type", "INHOUSE")
    next_st     = _next_status(status, fit_type)
    sc          = _STATUS_COLOR.get(status, "#64748b")
    job_no      = fj.get("fitting_job_no", "")
    fitter_nm   = fj.get("fitter_name") or "—"
    order_no    = fj.get("order_no") or "—"
    party_nm    = fj.get("party_name") or "—"

    with st.container():
        st.markdown(
            f"<div style='background:#0f172a;border:2px solid {sc}44;"
            f"border-radius:10px;padding:12px 16px;margin-bottom:8px'>",
            unsafe_allow_html=True
        )

        h1, h2, h3 = st.columns([1.5, 2, 1])
        with h1:
            st.markdown(
                f"<div style='color:#60a5fa;font-weight:700'>{job_no}</div>"
                f"<div style='color:#94a3b8;font-size:0.72rem'>{order_no} · {party_nm}</div>",
                unsafe_allow_html=True)
        with h2:
            icon = "🏪" if fit_type == "INHOUSE" else "📤"
            st.markdown(
                f"<div style='color:#e2e8f0;font-size:0.8rem'>"
                f"{icon} {fitter_nm}</div>"
                f"<div style='color:#64748b;font-size:0.7rem'>"
                f"Due: {_fd(fj.get('due_date'))}</div>",
                unsafe_allow_html=True)
        with h3:
            st.markdown(_render_status_pill(status), unsafe_allow_html=True)
            if fj.get("fitting_cost"):
                st.markdown(
                    f"<div style='color:#10b981;font-weight:700;"
                    f"font-size:0.82rem'>{_fmt(fj['fitting_cost'])}</div>",
                    unsafe_allow_html=True)

        if not compact:
            # Frame details
            frame_parts = [fj.get("frame_brand"), fj.get("frame_model"),
                           fj.get("frame_color"), fj.get("frame_size")]
            frame_str = " · ".join(p for p in frame_parts if p)
            if frame_str:
                st.markdown(
                    f"<div style='color:#94a3b8;font-size:0.75rem;"
                    f"margin:6px 0'>🖼 {frame_str}</div>",
                    unsafe_allow_html=True)
            if fj.get("frame_notes"):
                st.markdown(
                    f"<div style='color:#64748b;font-size:0.7rem'>"
                    f"📝 {fj['frame_notes']}</div>",
                    unsafe_allow_html=True)

            # Stage progress bar
            flow = _STATUS_FLOW.get(fit_type, _STATUS_FLOW["INHOUSE"])
            prog_cols = st.columns(len(flow))
            for i, (pc, st_code) in enumerate(zip(prog_cols, flow)):
                try:   cur_idx = flow.index(status)
                except: cur_idx = 0
                _done    = i < cur_idx
                _current = i == cur_idx
                _bg  = "#0d2818" if _done else ("#0f1e38" if _current else "#0f172a")
                _clr = "#10b981" if _done else ("#f59e0b" if _current else "#374151")
                with pc:
                    st.markdown(
                        f"<div style='background:{_bg};border:1px solid {_clr}44;"
                        f"border-radius:6px;padding:4px;text-align:center'>"
                        f"<div style='color:{_clr};font-size:0.6rem;font-weight:700'>"
                        f"{'✓' if _done else ('▶' if _current else '○')} {st_code.replace('_',' ')}"
                        f"</div></div>",
                        unsafe_allow_html=True)

            # Dates row
            dates = []
            if fj.get("sent_date"):     dates.append(f"Sent: {_fd(fj['sent_date'])}")
            if fj.get("received_date"): dates.append(f"Rcvd: {_fd(fj['received_date'])}")
            if fj.get("done_date"):     dates.append(f"Done: {_fd(fj['done_date'])}")
            if dates:
                st.markdown(
                    f"<div style='color:#64748b;font-size:0.68rem;margin-top:4px'>"
                    f"{'  ·  '.join(dates)}</div>",
                    unsafe_allow_html=True)

        # Advance button
        if next_st and status not in ("DONE", "DELIVERED", "CANCELLED"):
            st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
            adv_c1, adv_c2 = st.columns([2, 1])
            with adv_c1:
                rmk = st.text_input("", placeholder="Remarks…",
                                    key=f"ft_adv_rmk_{fj_id}",
                                    label_visibility="collapsed")
            with adv_c2:
                _nc = _STATUS_COLOR.get(next_st, "#3b82f6")
                if st.button(f"▶ {next_st.replace('_',' ')}",
                             key=f"ft_adv_{fj_id}",
                             use_container_width=True, type="primary"):
                    if _advance_fitting(fj_id, fit_type, status, rmk):
                        st.rerun()

        # ── WhatsApp button when fitting is DONE ──────────────────────────
        if status == "DONE":
            _wa_mob = str(fj.get("patient_mobile") or fj.get("party_mobile") or "")
            if _wa_mob:
                import urllib.parse as _up
                _wa_c = "".join(x for x in _wa_mob if x.isdigit())
                if len(_wa_c) == 10: _wa_c = "91" + _wa_c
                if _wa_c:
                    _wa_party = fj.get("party_name") or "Customer"
                    _wa_jno   = fj.get("fitting_job_no") or ""
                    _wa_ono   = fj.get("order_no") or ""
                    _wa_text  = (
                        f"Hello {_wa_party} 👋\n\n"
                        f"✅ *Fitting Completed!*\n"
                        f"🔧 Job: *{_wa_jno}* | Order: *{_wa_ono}*\n\n"
                        f"Your frames are ready for collection.\n"
                        f"Thank you! 🙏"
                    )
                    _wa_link = "https://wa.me/{}?text={}".format(
                        _wa_c, _up.quote(_wa_text))
                    st.markdown(
                        f"<a href='{_wa_link}' target='_blank' style='"
                        f"display:inline-block;background:#25d366;color:#fff;"
                        f"padding:5px 14px;border-radius:6px;font-weight:700;"
                        f"font-size:.78rem;text-decoration:none;margin:4px 0'>"
                        f"📲 Send WA — Fitting Done</a>",
                        unsafe_allow_html=True)

        # Edit cost inline
        if not compact:
            with st.expander("✏️ Edit / Cost", expanded=False):
                ec1, ec2 = st.columns(2)
                with ec1:
                    new_cost = st.number_input("Fitting Cost ₹",
                                               value=float(fj.get("fitting_cost") or 0),
                                               key=f"ft_ec_{fj_id}", step=10.0)
                with ec2:
                    cost_paid = st.checkbox("Cost Paid",
                                            value=bool(fj.get("cost_paid")),
                                            key=f"ft_cp_{fj_id}")
                new_rmk = st.text_input("Remarks",
                                        value=fj.get("remarks") or "",
                                        key=f"ft_ermk_{fj_id}")
                if st.button("💾 Save", key=f"ft_save_{fj_id}",
                             use_container_width=True):
                    if _w("""UPDATE fitting_jobs
                              SET fitting_cost=%(c)s, cost_paid=%(p)s,
                                  remarks=%(r)s, updated_at=NOW()
                              WHERE id=%(id)s::uuid""",
                           {"c": new_cost, "p": cost_paid,
                            "r": new_rmk, "id": fj_id}):
                        st.success("Saved"); st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# MAIN PANELS
# ══════════════════════════════════════════════════════════════════════════

def render_fitting_panel_for_order(order: Dict):
    """
    Embedded in backoffice order detail — shows fitting jobs for this order
    and allows creating new ones.
    """
    order_id = str(order.get("id") or "")
    order_no = order.get("order_no") or ""
    if not order_id:
        return

    st.markdown("### 📐 Fitting")

    jobs = _fetch_fitting_jobs(order_id=order_id)

    if jobs:
        for fj in jobs:
            _render_job_card(fj)

    # Create button
    _create_key = f"ft_show_create_{order_id}"
    if _create_key not in st.session_state:
        st.session_state[_create_key] = False

    if not st.session_state[_create_key]:
        if st.button("➕ New Fitting Job", key=f"ft_new_{order_id}",
                     use_container_width=True):
            st.session_state[_create_key] = True
            st.rerun()
    else:
        _render_create_form(order_id=order_id, order_no=order_no)
        if st.button("✕ Cancel", key=f"ft_cancel_{order_id}"):
            st.session_state[_create_key] = False
            st.rerun()


def render_fitting_dashboard():
    """
    Standalone fitting dashboard — full management view.
    """
    st.markdown("### 📐 Fitting Dashboard")

    # ── KPIs ──────────────────────────────────────────────────────────────
    kpi = _q("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'PENDING')     AS pending,
            COUNT(*) FILTER (WHERE status IN ('SENT','IN_PROGRESS')) AS active,
            COUNT(*) FILTER (WHERE status = 'RECEIVED')    AS received,
            COUNT(*) FILTER (WHERE status = 'DONE')        AS done,
            COALESCE(SUM(fitting_cost) FILTER
                (WHERE status NOT IN ('CANCELLED')), 0)     AS total_cost,
            COUNT(*) FILTER (WHERE due_date < CURRENT_DATE
                AND status NOT IN ('DONE','DELIVERED','CANCELLED')) AS overdue
        FROM fitting_jobs
    """)
    k = kpi[0] if kpi else {}

    _kc = st.columns(6)
    for col, lbl, val, clr in [
        (_kc[0], "Pending",  k.get("pending",0),  "#f59e0b"),
        (_kc[1], "Active",   k.get("active",0),   "#3b82f6"),
        (_kc[2], "Received", k.get("received",0), "#0ea5e9"),
        (_kc[3], "Done",     k.get("done",0),     "#10b981"),
        (_kc[4], "Overdue",  k.get("overdue",0),  "#ef4444"),
        (_kc[5], "Total Cost", _fmt(k.get("total_cost",0)), "#8b5cf6"),
    ]:
        with col:
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid {clr}44;"
                f"border-radius:8px;padding:10px;text-align:center'>"
                f"<div style='color:#94a3b8;font-size:0.68rem'>{lbl}</div>"
                f"<div style='color:{clr};font-weight:700;font-size:1.2rem'>{val}</div>"
                f"</div>", unsafe_allow_html=True)

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    tab_active, tab_pending, tab_new, tab_fitters = st.tabs([
        "⚡ Active Jobs", "🕐 Pending", "➕ New Job", "👷 Fitters"
    ])

    # ── Active Jobs ────────────────────────────────────────────────────────
    with tab_active:
        active_jobs = _fetch_fitting_jobs(status_filter="ALL")
        active_jobs = [j for j in active_jobs
                       if j.get("status") not in ("DONE","DELIVERED","CANCELLED")]
        if not active_jobs:
            st.info("No active fitting jobs.")
        else:
            # Filter bar
            _fc1, _fc2, _fc3 = st.columns([2, 1.5, 1.5])
            with _fc1:
                _search = st.text_input("🔍", placeholder="Party or job no…",
                                        label_visibility="collapsed",
                                        key="ft_dash_search")
            with _fc2:
                _type_f = st.selectbox("Type", ["ALL","INHOUSE","EXTERNAL"],
                                       label_visibility="collapsed",
                                       key="ft_dash_type")
            with _fc3:
                _st_f = st.selectbox("Status",
                                     ["ALL","PENDING","IN_PROGRESS","SENT","RECEIVED"],
                                     label_visibility="collapsed",
                                     key="ft_dash_status")

            filtered = active_jobs
            if _search:
                s = _search.lower()
                filtered = [j for j in filtered if
                            s in (j.get("order_no") or "").lower() or
                            s in (j.get("party_name") or "").lower() or
                            s in (j.get("fitting_job_no") or "").lower() or
                            s in (j.get("fitter_name") or "").lower()]
            if _type_f != "ALL":
                filtered = [j for j in filtered if j.get("fitting_type") == _type_f]
            if _st_f != "ALL":
                filtered = [j for j in filtered if j.get("status") == _st_f]

            st.caption(f"{len(filtered)} job(s)")
            for fj in filtered:
                _render_job_card(fj)

    # ── Pending (needs fitting) ────────────────────────────────────────────
    with tab_pending:
        st.markdown("#### Orders ready for fitting assignment")
        needs_fitting = _fetch_orders_needing_fitting()
        if not needs_fitting:
            st.info("✅ No orders waiting for fitting.")
        else:
            st.caption(f"{len(needs_fitting)} order(s) at READY_FOR_PACK with no fitting job")
            for o in needs_fitting:
                _nc1, _nc2, _nc3 = st.columns([2, 2, 1])
                with _nc1:
                    st.markdown(
                        f"<div style='color:#60a5fa;font-weight:700'>{o['order_no']}</div>"
                        f"<div style='color:#94a3b8;font-size:0.75rem'>{o.get('party_name','')}</div>",
                        unsafe_allow_html=True)
                with _nc2:
                    st.markdown(
                        f"<div style='color:#64748b;font-size:0.75rem'>"
                        f"Status: {o.get('status','')}</div>",
                        unsafe_allow_html=True)
                with _nc3:
                    if st.button("➕ Assign", key=f"ft_assign_{o['order_id']}",
                                 use_container_width=True):
                        st.session_state["ft_create_for"] = o["order_id"]
                        st.session_state["ft_create_no"]  = o["order_no"]
                        st.rerun()

            # Inline create form if triggered
            if st.session_state.get("ft_create_for"):
                st.markdown("---")
                _render_create_form(
                    order_id=st.session_state["ft_create_for"],
                    order_no=st.session_state.get("ft_create_no","")
                )
                if st.button("✕ Cancel assignment"):
                    st.session_state["ft_create_for"] = None
                    st.rerun()

    # ── New standalone job ─────────────────────────────────────────────────
    with tab_new:
        # Pick order first
        all_orders = _q("""
            SELECT o.id::text AS order_id, o.order_no, o.party_name
            FROM orders o
            WHERE o.status NOT IN ('CANCELLED','DELIVERED','CLOSED')
            ORDER BY o.created_at DESC LIMIT 100
        """)
        if not all_orders:
            st.info("No orders available.")
        else:
            o_opts = {o["order_id"]: f"{o['order_no']} — {o['party_name']}"
                      for o in all_orders}
            sel_o = st.selectbox("Select Order", options=list(o_opts.keys()),
                                 format_func=lambda x: o_opts.get(x,""),
                                 key="ft_new_order")
            if sel_o:
                _render_create_form(
                    order_id=sel_o,
                    order_no=o_opts.get(sel_o,"").split(" — ")[0]
                )

    # ── Fitters management ─────────────────────────────────────────────────
    with tab_fitters:
        st.markdown("#### 👷 Fitter Directory")
        fitters = _q("""
            SELECT id, fitter_name, fitter_type, contact, address, is_active
            FROM fitters ORDER BY fitter_name
        """)

        if fitters:
            for f in fitters:
                _fid  = str(f["id"])
                _fc   = "#10b981" if f.get("is_active") else "#64748b"
                fc1, fc2, fc3 = st.columns([2, 2, 1])
                with fc1:
                    st.markdown(
                        f"<div style='color:#e2e8f0;font-weight:600'>{f['fitter_name']}</div>"
                        f"<div style='color:#64748b;font-size:0.72rem'>{f.get('fitter_type','')}</div>",
                        unsafe_allow_html=True)
                with fc2:
                    st.markdown(
                        f"<div style='color:#94a3b8;font-size:0.78rem'>"
                        f"📱 {f.get('contact','—')}</div>",
                        unsafe_allow_html=True)
                with fc3:
                    _active = f.get("is_active", True)
                    if st.button("Deactivate" if _active else "Activate",
                                 key=f"ft_tog_{_fid}"):
                        _w("UPDATE fitters SET is_active=%(a)s WHERE id=%(id)s::uuid",
                           {"a": not _active, "id": _fid})
                        st.rerun()
                st.markdown("<hr style='margin:4px 0;border-color:#1e293b'>",
                            unsafe_allow_html=True)

        st.markdown("#### ➕ Add Fitter")
        _af1, _af2 = st.columns(2)
        with _af1:
            new_fn   = st.text_input("Fitter Name", key="ft_new_fn")
            new_ft   = st.selectbox("Type", ["EXTERNAL","INHOUSE"], key="ft_new_type")
        with _af2:
            new_cont = st.text_input("Contact / Mobile", key="ft_new_cont")
            new_addr = st.text_input("Address", key="ft_new_addr")

        if st.button("➕ Add Fitter", type="primary",
                     use_container_width=True, key="ft_add_btn"):
            if not new_fn:
                st.error("Fitter name required")
            else:
                if _w("""INSERT INTO fitters
                           (id, fitter_name, fitter_type, contact, address)
                           VALUES (gen_random_uuid(),%(n)s,%(t)s,%(c)s,%(a)s)""",
                       {"n": new_fn, "t": new_ft,
                        "c": new_cont, "a": new_addr}):
                    st.success(f"✅ Fitter **{new_fn}** added")
                    st.rerun()
