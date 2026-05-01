"""
modules/hr/hr_ui.py
====================
HR UI — Attendance, Employees, Monthly Sheet, Leave, Payroll
"""
import streamlit as st
import pandas as pd
import streamlit.components.v1 as _stc
from datetime import date, timedelta
import calendar


def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# GEOLOCATION JS — captures browser GPS and posts back via query param
# ══════════════════════════════════════════════════════════════════════════════

GEO_JS = """
<script>
function getLocation(action) {
    var btn = document.getElementById('geo_btn_' + action);
    if (btn) btn.disabled = true;
    document.getElementById('geo_status').innerText = '📡 Getting GPS location...';

    if (!navigator.geolocation) {
        document.getElementById('geo_status').innerText = '❌ Geolocation not supported';
        return;
    }

    navigator.geolocation.getCurrentPosition(
        function(pos) {
            var lat = pos.coords.latitude.toFixed(7);
            var lng = pos.coords.longitude.toFixed(7);
            var acc = pos.coords.accuracy.toFixed(1);
            document.getElementById('geo_status').innerText =
                '✅ Got location: ' + lat + ', ' + lng + ' (±' + acc + 'm)';
            // Send to Streamlit via URL param + reload
            var url = new URL(window.parent.location.href);
            url.searchParams.set('geo_action', action);
            url.searchParams.set('geo_lat', lat);
            url.searchParams.set('geo_lng', lng);
            url.searchParams.set('geo_acc', acc);
            window.parent.location.href = url.toString();
        },
        function(err) {
            var msgs = {1:'Permission denied',2:'Position unavailable',3:'Timeout'};
            document.getElementById('geo_status').innerText =
                '❌ Error: ' + (msgs[err.code] || err.message);
            if (btn) btn.disabled = false;
        },
        {enableHighAccuracy: true, timeout: 15000, maximumAge: 0}
    );
}
</script>

<div style="font-family:sans-serif;padding:16px;background:#0a1628;
            border-radius:12px;border:1px solid #1e3a5f;text-align:center">
  <div id="geo_status" style="color:#94a3b8;margin-bottom:12px;font-size:.85rem">
    Press button to capture your GPS location
  </div>
  <div style="display:flex;gap:12px;justify-content:center">
    <button id="geo_btn_checkin"
      onclick="getLocation('checkin')"
      style="background:#10b981;color:#fff;border:none;border-radius:8px;
             padding:14px 28px;font-size:1rem;font-weight:700;cursor:pointer;
             box-shadow:0 0 12px #10b98144">
      📍 CHECK IN
    </button>
    <button id="geo_btn_checkout"
      onclick="getLocation('checkout')"
      style="background:#ef4444;color:#fff;border:none;border-radius:8px;
             padding:14px 28px;font-size:1rem;font-weight:700;cursor:pointer;
             box-shadow:0 0 12px #ef444444">
      🏁 CHECK OUT
    </button>
  </div>
</div>
"""


def _render_geo_buttons(emp_id: str) -> None:
    """Render geo capture buttons + handle URL param return."""
    # Read geo result from URL params (set by JS after GPS capture)
    params  = st.query_params
    action  = params.get("geo_action", "")
    lat_str = params.get("geo_lat",    "")
    lng_str = params.get("geo_lng",    "")
    acc_str = params.get("geo_acc",    "")

    if action and lat_str and emp_id:
        try:
            from modules.hr.hr_engine import check_in, check_out
            lat = float(lat_str)
            lng = float(lng_str)
            acc = float(acc_str or "0")

            if action == "checkin":
                ok, msg, _ = check_in(emp_id, lat, lng, acc)
            else:
                ok, msg, _ = check_out(emp_id, lat, lng, acc)

            if ok:
                st.success(msg)
            else:
                st.warning(msg)

            # Clear URL params
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Attendance error: {e}")
            st.query_params.clear()

    _stc.html(GEO_JS, height=140)


def _status_pill(status: str) -> str:
    colors = {
        "PRESENT":  ("#10b981", "#0a2a1a"),
        "LATE":     ("#f59e0b", "#2a1a00"),
        "ABSENT":   ("#ef4444", "#2a0a0a"),
        "HALF_DAY": ("#3b82f6", "#0a1628"),
        "LEAVE":    ("#a855f7", "#1a0a2a"),
        "HOLIDAY":  ("#6b7280", "#1a1a1a"),
        "REMOTE":   ("#f97316", "#2a1200"),
    }
    c, bg = colors.get(status, ("#94a3b8", "#1a1a2a"))
    return (f"<span style='background:{bg};color:{c};border:1px solid {c}44;"
            f"border-radius:4px;padding:2px 8px;font-size:.7rem;font-weight:700'>"
            f"{status}</span>")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — MY ATTENDANCE (Employee self-service)
# ══════════════════════════════════════════════════════════════════════════════

def _tab_my_attendance():
    st.caption("Employee self-service — check in / check out with GPS verification")

    from modules.hr.hr_engine import (
        ensure_hr_schema, get_all_employees, get_today_attendance,
    )
    ensure_hr_schema()

    # Employee selector (in real use, this would be their login)
    emps = get_all_employees()
    if not emps:
        st.info("No employees yet. Add employees in the **Employees** tab first.")
        return

    emp_opts = {f"{e['name']} ({e['role']})": e for e in emps}
    chosen   = st.selectbox("Select Employee", list(emp_opts.keys()),
                             key="my_att_emp")
    emp      = emp_opts[chosen]
    emp_id   = emp["id"]

    # Today's record
    today_rec = get_today_attendance(emp_id)

    # Status card
    st.markdown(
        f"<div style='background:#0a1628;border:1px solid #1e3a5f;border-radius:10px;"
        f"padding:14px 18px;margin:10px 0'>"
        f"<div style='color:#60a5fa;font-weight:700;font-size:1.1rem'>{emp['name']}</div>"
        f"<div style='color:#475569;font-size:.8rem'>{emp['role']} · Shift: "
        f"{emp.get('shift_start','?')} – {emp.get('shift_end','?')}</div>"
        f"<div style='margin-top:8px;font-size:.85rem;color:#94a3b8'>"
        f"Today: {date.today().strftime('%A, %d %b %Y')}</div>"
        f"</div>",
        unsafe_allow_html=True
    )

    if today_rec:
        ci = str(today_rec.get("check_in_time",""))[:16] if today_rec.get("check_in_time") else "—"
        co = str(today_rec.get("check_out_time",""))[:16] if today_rec.get("check_out_time") else "Pending"
        wh = today_rec.get("work_hours")
        dist_in  = today_rec.get("check_in_dist")
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Check In",    ci)
        c2.metric("Check Out",   co)
        c3.metric("Hours",       f"{float(wh):.1f}h" if wh else "—")
        c4.markdown(
            f"<div style='margin-top:8px'>"
            + _status_pill(today_rec.get("status","PRESENT"))
            + (f"<br><span style='font-size:.65rem;color:#475569'>📍 {dist_in:.0f}m</span>"
               if dist_in else "")
            + "</div>",
            unsafe_allow_html=True
        )

        if today_rec.get("check_out_time"):
            st.success("✅ Attendance complete for today.")
            return

    st.markdown("---")
    _render_geo_buttons(emp_id)

    # Recent history
    with st.expander("📅 My last 7 days", expanded=False):
        rows = _q("""
            SELECT log_date::text, status, is_late,
                   check_in_time::text, check_out_time::text,
                   ROUND(work_hours,1) AS work_hours,
                   check_in_dist
            FROM attendance_logs
            WHERE employee_id=%s::uuid
            ORDER BY log_date DESC LIMIT 7
        """, (emp_id,))
        if rows:
            for r in rows:
                ci = str(r.get("check_in_time",""))[:16] if r.get("check_in_time") else "—"
                co = str(r.get("check_out_time",""))[:16] if r.get("check_out_time") else "—"
                wh = r.get("work_hours")
                st.markdown(
                    f"**{r['log_date']}**  "
                    + _status_pill(r.get("status","ABSENT"))
                    + f"  In: {ci}  Out: {co}  "
                    + (f"{float(wh):.1f}h" if wh else ""),
                    unsafe_allow_html=True
                )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — TODAY'S ROSTER (Admin)
# ══════════════════════════════════════════════════════════════════════════════

def _tab_roster():
    st.caption("Live view — who is in / out today")

    from modules.hr.hr_engine import ensure_hr_schema
    ensure_hr_schema()

    rows = _q("""
        SELECT e.name, e.role,
               COALESCE(a.status,'ABSENT') AS status,
               a.check_in_time::text,
               a.check_out_time::text,
               ROUND(COALESCE(a.work_hours,0),1) AS work_hours,
               a.is_late,
               ROUND(a.check_in_dist,0) AS dist_m,
               a.check_in_valid
        FROM employees e
        LEFT JOIN attendance_logs a
            ON a.employee_id=e.id AND a.log_date=CURRENT_DATE
        WHERE e.is_active=TRUE
        ORDER BY e.name
    """)

    if not rows:
        st.info("No employees found.")
        return

    df = _df(rows)

    # Summary metrics
    present  = sum(1 for r in rows if r["status"] in ("PRESENT","LATE"))
    absent   = sum(1 for r in rows if r["status"] == "ABSENT")
    checked_out = sum(1 for r in rows if r.get("check_out_time"))
    late     = sum(1 for r in rows if r.get("is_late"))

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Present",     present,     delta=f"{absent} absent",  delta_color="inverse")
    m2.metric("Checked Out", checked_out)
    m3.metric("Late",        late,        delta_color="inverse" if late else "off")
    m4.metric("Total Staff", len(rows))

    st.markdown("---")
    for r in rows:
        ci = str(r.get("check_in_time",""))[:16] if r.get("check_in_time") else "—"
        co = str(r.get("check_out_time",""))[:16] if r.get("check_out_time") else "—"
        geo = (f"📍 {r['dist_m']:.0f}m" if r.get("dist_m") else "")
        valid_icon = "✅" if r.get("check_in_valid") else ("⚠️" if r.get("check_in_time") else "")
        st.markdown(
            f"**{r['name']}** &nbsp; <span style='color:#475569'>{r['role']}</span>"
            f"&nbsp;&nbsp;"
            + _status_pill(r["status"])
            + f"&nbsp;&nbsp; In: `{ci}` &nbsp; Out: `{co}` &nbsp;"
            + (f" {float(r['work_hours']):.1f}h" if r.get("work_hours") else "")
            + f" &nbsp; {valid_icon} {geo}",
            unsafe_allow_html=True
        )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — MONTHLY SHEET
# ══════════════════════════════════════════════════════════════════════════════

def _tab_monthly():
    st.caption("Full month attendance — P / A / L / H / Late")

    from modules.hr.hr_engine import get_monthly_sheet, ensure_hr_schema
    ensure_hr_schema()

    c1,c2 = st.columns(2)
    month = c1.selectbox("Month", list(range(1,13)),
                          index=date.today().month-1,
                          format_func=lambda m: date(2000,m,1).strftime("%B"),
                          key="ms_month")
    year  = c2.number_input("Year", min_value=2020, max_value=2030,
                             value=date.today().year, step=1, key="ms_year")

    rows = get_monthly_sheet(int(year), int(month))
    if not rows:
        st.info("No data.")
        return

    # Pivot: rows = employees, cols = dates
    df = _df(rows)
    df["day"] = pd.to_datetime(df["log_date"]).dt.day

    status_map = {
        "PRESENT": "P", "LATE": "L", "ABSENT": "A",
        "HALF_DAY": "H", "LEAVE": "LV", "HOLIDAY": "H",
        "REMOTE": "R", None: "A"
    }
    color_map = {
        "P":  "#10b981", "L": "#f59e0b", "A": "#ef4444",
        "H":  "#3b82f6", "LV":"#a855f7", "R": "#f97316",
    }

    # Build pivot table
    employees = df["name"].unique()
    days_in_month = calendar.monthrange(int(year), int(month))[1]

    # Summary per employee
    st.markdown("**Summary**")
    summary_rows = []
    for emp in employees:
        edf = df[df["name"] == emp]
        counts = edf["status"].value_counts().to_dict()
        p  = counts.get("PRESENT",0) + counts.get("LATE",0)
        a  = counts.get("ABSENT",0)
        l  = counts.get("LEAVE",0)
        h  = counts.get("HALF_DAY",0)
        lt = counts.get("LATE",0)
        wh = edf["work_hours"].apply(pd.to_numeric, errors="coerce").sum()
        summary_rows.append({
            "Employee": emp,
            "Present":  p,
            "Absent":   a,
            "Leave":    l,
            "Half Day": h,
            "Late":     lt,
            "Work Hrs": round(float(wh), 1),
        })

    sdf = _df(summary_rows)
    st.dataframe(sdf, width='stretch', hide_index=True,
        column_config={
            "Present":  st.column_config.NumberColumn(),
            "Absent":   st.column_config.NumberColumn(),
        })

    # Daily grid
    st.markdown("**Day-wise Grid**")
    grid_cols = ["Employee"] + [str(d) for d in range(1, days_in_month+1)]
    grid_data = []
    for emp in employees:
        edf = df[df["name"] == emp]
        day_status = {int(r["day"]): status_map.get(r["status"], "A")
                      for _, r in edf.iterrows()}
        row = {"Employee": emp}
        for d in range(1, days_in_month+1):
            row[str(d)] = day_status.get(d, "")
        grid_data.append(row)

    gdf = _df(grid_data)
    st.dataframe(gdf, width='stretch', hide_index=True)

    # Download
    st.download_button("⬇ Export CSV",
        gdf.to_csv(index=False).encode(),
        file_name=f"Attendance_{year}_{month:02d}.csv",
        mime="text/csv", key="ms_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — LEAVE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _tab_leave():
    st.caption("Apply for leave · Admin approval")

    from modules.hr.hr_engine import (
        ensure_hr_schema, get_all_employees, apply_leave,
        approve_leave, get_pending_leaves,
    )
    ensure_hr_schema()

    sub1, sub2 = st.tabs(["Apply Leave", "Pending Approvals"])

    with sub1:
        emps     = get_all_employees()
        emp_opts = {f"{e['name']} ({e['role']})": e for e in emps}
        chosen   = st.selectbox("Employee", list(emp_opts.keys()), key="lv_emp")
        emp      = emp_opts.get(chosen, {})

        c1,c2,c3 = st.columns(3)
        lv_from  = c1.date_input("From", value=date.today(), key="lv_from")
        lv_to    = c2.date_input("To",   value=date.today(), key="lv_to")
        lv_type  = c3.selectbox("Type", ["CL","SL","EL","LWP"], key="lv_type")
        reason   = st.text_area("Reason", key="lv_reason", height=80)

        days = (lv_to - lv_from).days + 1 if lv_to >= lv_from else 0
        st.caption(f"Duration: **{days} day(s)**")

        if st.button("📋 Apply Leave", type="primary", key="lv_apply",
                      disabled=(not emp or days <= 0)):
            lid = apply_leave(emp["id"], lv_from, lv_to, lv_type, reason)
            st.success(f"✅ Leave applied — {days} day(s) {lv_type} from {lv_from} to {lv_to}")

        # My leave history
        if emp:
            with st.expander("My leave history", expanded=False):
                hist = _q("""
                    SELECT leave_from::text, leave_to::text, leave_type,
                           reason, status, applied_at::text
                    FROM leave_requests
                    WHERE employee_id=%s::uuid
                    ORDER BY applied_at DESC LIMIT 10
                """, (emp["id"],))
                if hist:
                    st.dataframe(_df(hist), width='stretch', hide_index=True)
                else:
                    st.caption("No leave history.")

    with sub2:
        pending = get_pending_leaves()
        if not pending:
            st.success("✅ No pending leave requests.")
        else:
            for lv in pending:
                c1,c2,c3 = st.columns([3,1,1])
                c1.markdown(
                    f"**{lv['name']}** — {lv['leave_type']}  "
                    f"`{lv['leave_from']}` to `{lv['leave_to']}`  \n"
                    f"<span style='color:#94a3b8'>{lv.get('reason','')}</span>",
                    unsafe_allow_html=True
                )
                if c2.button("✅ Approve", key=f"lv_ok_{lv['id']}",
                              width='stretch'):
                    approve_leave(lv["id"],
                                  st.session_state.get("user_name","Admin"),
                                  True)
                    st.rerun()
                if c3.button("❌ Reject", key=f"lv_no_{lv['id']}",
                              width='stretch'):
                    approve_leave(lv["id"],
                                  st.session_state.get("user_name","Admin"),
                                  False)
                    st.rerun()
                st.markdown("---")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — EMPLOYEES MASTER
# ══════════════════════════════════════════════════════════════════════════════

def _tab_employees():
    st.caption("Employee master — add / edit staff and delivery boys")

    from modules.hr.hr_engine import ensure_hr_schema, get_all_employees, save_employee
    ensure_hr_schema()

    with st.expander("➕ Add / Edit Employee", expanded=False):
        emps = get_all_employees(active_only=False)
        edit_opts = {"New Employee":{}} | {e["name"]: e for e in emps}
        editing = st.selectbox("Edit existing or add new",
                                list(edit_opts.keys()), key="emp_edit_sel")
        ed = edit_opts.get(editing, {})

        c1,c2 = st.columns(2)
        name    = c1.text_input("Name *",      value=ed.get("name",""),      key="emp_name")
        code    = c2.text_input("Emp Code",    value=ed.get("emp_code",""),  key="emp_code")
        phone   = c1.text_input("Phone",       value=ed.get("phone",""),     key="emp_phone")
        role    = c2.selectbox("Role",
                               ["STAFF","DELIVERY","MANAGER","ADMIN"],
                               index=["STAFF","DELIVERY","MANAGER","ADMIN"].index(
                                   ed.get("role","STAFF")), key="emp_role")
        dept    = c1.text_input("Department",  value=ed.get("department",""), key="emp_dept")
        stype   = c2.selectbox("Salary Type", ["MONTHLY","DAILY"],
                               index=["MONTHLY","DAILY"].index(
                                   ed.get("salary_type","MONTHLY")), key="emp_stype")
        salary  = c1.number_input("Salary ₹", value=float(ed.get("salary_amount") or 0),
                                   step=500.0, key="emp_sal")
        shift_s = c2.text_input("Shift Start", value=str(ed.get("shift_start","10:00"))[:5],
                                 key="emp_shiftstart", placeholder="HH:MM")
        shift_e = c1.text_input("Shift End",   value=str(ed.get("shift_end","19:00"))[:5],
                                 key="emp_shiftend",   placeholder="HH:MM")
        grace   = c2.number_input("Late grace (min)", value=int(ed.get("late_grace_min") or 15),
                                   step=5, key="emp_grace")
        weekly  = c1.selectbox("Weekly Off",
                               ["Sunday","Monday","Saturday","None"],
                               index=["Sunday","Monday","Saturday","None"].index(
                                   ed.get("weekly_off","Sunday")), key="emp_woff")
        j_date  = c2.date_input("Join Date",
                                 value=date.fromisoformat(str(ed.get("join_date",date.today()))[:10]),
                                 key="emp_jdate")
        notes   = st.text_area("Notes", value=ed.get("notes","") or "", height=60, key="emp_notes")

        if st.button("💾 Save Employee", type="primary", key="emp_save",
                      disabled=not name.strip()):
            save_employee({
                "id":           ed.get("id"),
                "emp_code":     code.strip() or None,
                "name":         name.strip(),
                "phone":        phone.strip() or None,
                "role":         role,
                "department":   dept.strip() or None,
                "salary_type":  stype,
                "salary_amount":salary,
                "shift_start":  shift_s,
                "shift_end":    shift_e,
                "late_grace_min": grace,
                "weekly_off":   weekly,
                "join_date":    j_date,
                "notes":        notes.strip() or None,
            })
            st.success(f"✅ {name} saved.")
            st.rerun()

    # Employee list
    emps = get_all_employees()
    if emps:
        df = _df(emps)
        show_cols = ["emp_code","name","role","phone","salary_type",
                     "salary_amount","shift_start","shift_end","weekly_off"]
        st.dataframe(df[[c for c in show_cols if c in df.columns]],
                     width='stretch', hide_index=True,
                     column_config={
                         "salary_amount": st.column_config.NumberColumn("Salary ₹", format="₹%.0f"),
                     })


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — OFFICE LOCATION SETUP
# ══════════════════════════════════════════════════════════════════════════════

def _tab_office_setup():
    st.caption("Register office GPS coordinates — attendance checked against this location")

    from modules.hr.hr_engine import ensure_hr_schema, get_office_location
    ensure_hr_schema()

    office = get_office_location()
    if office:
        st.markdown(
            f"<div style='background:#0a1a0a;border:1px solid #22c55e;border-radius:8px;"
            f"padding:10px 16px;margin-bottom:12px'>"
            f"✅ <b style='color:#86efac'>{office['name']}</b>  "
            f"<span style='color:#94a3b8'>Lat: {office['latitude']}  "
            f"Lng: {office['longitude']}  Radius: {office['radius_m']}m</span></div>",
            unsafe_allow_html=True
        )

    st.markdown("**Set / Update Office Location**")

    c1,c2 = st.columns(2)
    loc_name = c1.text_input("Location Name", value=office.get("name","DV Optical") if office else "DV Optical", key="ol_name")
    radius   = c2.number_input("Allowed Radius (metres)", value=int(office.get("radius_m",150)) if office else 150, step=25, key="ol_radius")

    st.markdown("**Enter coordinates** (or use GPS button below)")

    c3,c4 = st.columns(2)
    lat = c3.number_input("Latitude",  value=float(office.get("latitude",0)) if office else 21.1458,  format="%.7f", step=0.0001, key="ol_lat")
    lng = c4.number_input("Longitude", value=float(office.get("longitude",0)) if office else 79.0882, format="%.7f", step=0.0001, key="ol_lng")

    st.caption("💡 Tip: Open Google Maps, long-press your office → coordinates appear at bottom")

    if st.button("📍 Capture My Current Location as Office", key="ol_geo"):
        st.info("After pressing OK on the location prompt, the coordinates will auto-fill.")
        _stc.html("""
        <script>
        navigator.geolocation.getCurrentPosition(function(pos){
            var url = new URL(window.parent.location.href);
            url.searchParams.set('ol_lat', pos.coords.latitude.toFixed(7));
            url.searchParams.set('ol_lng', pos.coords.longitude.toFixed(7));
            window.parent.location.href = url.toString();
        });
        </script>""", height=0)

    # Handle geo return for office setup
    params = st.query_params
    if params.get("ol_lat"):
        try:
            lat = float(params["ol_lat"])
            lng = float(params["ol_lng"])
            st.success(f"Got GPS: {lat}, {lng}")
            st.query_params.clear()
            st.rerun()
        except Exception:
            pass

    if st.button("💾 Save Office Location", type="primary", key="ol_save"):
        from modules.sql_adapter import run_write
        run_write("""
            INSERT INTO office_locations (name, latitude, longitude, radius_m)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (loc_name, lat, lng, radius))
        # Also update existing
        run_write("""
            UPDATE office_locations SET
                name=%s, latitude=%s, longitude=%s, radius_m=%s
            WHERE is_active=TRUE
        """, (loc_name, lat, lng, radius))
        st.success(f"✅ Office location saved: {lat}, {lng} (radius {radius}m)")
        st.rerun()

    # Show map
    if lat and lng:
        map_html = f"""
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <div id="map" style="height:280px;border-radius:10px"></div>
        <script>
        var map = L.map('map').setView([{lat},{lng}], 17);
        L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
            {{attribution:'© OpenStreetMap'}}).addTo(map);
        L.marker([{lat},{lng}]).addTo(map)
            .bindPopup('<b>{loc_name}</b>').openPopup();
        L.circle([{lat},{lng}], {{radius:{radius},color:'#10b981',
            fillColor:'#10b981',fillOpacity:0.15}}).addTo(map);
        </script>
        """
        _stc.html(map_html, height=300)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — PAYROLL
# ══════════════════════════════════════════════════════════════════════════════

def _tab_payroll():
    st.caption("Monthly payroll — computed from attendance × salary")

    from modules.hr.hr_engine import compute_payroll, ensure_hr_schema
    ensure_hr_schema()

    c1,c2 = st.columns(2)
    month = c1.selectbox("Month", list(range(1,13)),
                          index=date.today().month-1,
                          format_func=lambda m: date(2000,m,1).strftime("%B"),
                          key="pr_month")
    year  = c2.number_input("Year", min_value=2020, max_value=2030,
                             value=date.today().year, step=1, key="pr_year")

    rows = compute_payroll(int(year), int(month))
    if not rows:
        st.info("No payroll data.")
        return

    total_payable = sum(float(r.get("payable") or 0) for r in rows)
    total_deduct  = sum(float(r.get("deduction") or 0) for r in rows)

    m1,m2,m3 = st.columns(3)
    m1.metric("Total Employees",  len(rows))
    m2.metric("Total Payable",    f"₹{total_payable:,.2f}")
    m3.metric("Total Deductions", f"₹{total_deduct:,.2f}")

    st.markdown("---")
    for r in rows:
        with st.container():
            c1,c2,c3,c4,c5 = st.columns([2,1,1,1,1.5])
            c1.markdown(f"**{r['name']}**  \n<span style='color:#475569;font-size:.75rem'>{r['role']} · {r['salary_type']}</span>",
                        unsafe_allow_html=True)
            c2.metric("Present",    int(r.get("present_days") or 0))
            c3.metric("Absent",     int(r.get("absent_days") or 0))
            c4.metric("Paid Days",  f"{float(r.get('paid_days') or 0):.1f}")
            c5.metric("Payable",    f"₹{float(r.get('payable') or 0):,.2f}",
                      delta=f"-₹{float(r.get('deduction') or 0):,.2f}" if r.get("deduction") else None,
                      delta_color="inverse")
            st.markdown("---")

    # Download
    df = _df(rows)
    dl_cols = ["name","role","salary_type","salary_amount","present_days",
               "absent_days","leave_days","half_days","paid_days","payable","deduction"]
    dl_df = df[[c for c in dl_cols if c in df.columns]]
    st.download_button("⬇ Export Payroll CSV",
        dl_df.to_csv(index=False).encode(),
        file_name=f"Payroll_{year}_{month:02d}.csv",
        mime="text/csv", key="pr_dl")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def render_hr():
    st.markdown("## 👥 HR — Attendance & Payroll")

    try:
        from modules.hr.hr_engine import ensure_hr_schema
        ensure_hr_schema()
    except Exception as e:
        st.error(f"HR schema error: {e}")
        return

    tabs = st.tabs([
        "📍 My Attendance",
        "📋 Today's Roster",
        "📅 Monthly Sheet",
        "🏖️ Leave",
        "👤 Employees",
        "🏢 Office Setup",
        "💰 Payroll",
    ])

    with tabs[0]: _tab_my_attendance()
    with tabs[1]: _tab_roster()
    with tabs[2]: _tab_monthly()
    with tabs[3]: _tab_leave()
    with tabs[4]: _tab_employees()
    with tabs[5]: _tab_office_setup()
    with tabs[6]: _tab_payroll()
