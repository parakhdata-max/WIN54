"""
modules/hr/hr_engine.py
========================
HR Engine — Employees, Attendance (Geo), Leave, Payroll
"""
from __future__ import annotations
from typing import List, Dict, Optional, Tuple
import uuid, datetime, math, logging

_log = logging.getLogger(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []

def _w(sql, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or ())
        return True
    except Exception as e:
        _log.warning(f"[hr._w] {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

def ensure_hr_schema() -> None:
    """Create all HR tables — idempotent."""

    # Office locations (geo-fence)
    _w("""
        CREATE TABLE IF NOT EXISTS office_locations (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            latitude    NUMERIC(10,7) NOT NULL,
            longitude   NUMERIC(10,7) NOT NULL,
            radius_m    INTEGER DEFAULT 150,
            is_active   BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Employee master
    _w("""
        CREATE TABLE IF NOT EXISTS employees (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            emp_code        TEXT UNIQUE,
            name            TEXT NOT NULL,
            phone           TEXT,
            role            TEXT,           -- STAFF | DELIVERY | MANAGER | ADMIN
            department      TEXT,
            salary_type     TEXT DEFAULT 'MONTHLY',  -- MONTHLY | DAILY
            salary_amount   NUMERIC(10,2) DEFAULT 0,
            shift_start     TIME DEFAULT '10:00',
            shift_end       TIME DEFAULT '19:00',
            late_grace_min  INTEGER DEFAULT 15,  -- minutes grace after shift_start
            weekly_off      TEXT DEFAULT 'Sunday',
            join_date       DATE DEFAULT CURRENT_DATE,
            is_active       BOOLEAN DEFAULT TRUE,
            notes           TEXT,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Attendance logs
    _w("""
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id     UUID NOT NULL REFERENCES employees(id),
            log_date        DATE NOT NULL DEFAULT CURRENT_DATE,
            check_in_time   TIMESTAMPTZ,
            check_in_lat    NUMERIC(10,7),
            check_in_lng    NUMERIC(10,7),
            check_in_acc    NUMERIC(8,1),   -- accuracy in metres
            check_in_dist   NUMERIC(8,1),   -- distance from office in metres
            check_in_valid  BOOLEAN,        -- within geo-fence?
            check_out_time  TIMESTAMPTZ,
            check_out_lat   NUMERIC(10,7),
            check_out_lng   NUMERIC(10,7),
            check_out_dist  NUMERIC(8,1),
            check_out_valid BOOLEAN,
            work_hours      NUMERIC(5,2),   -- computed on checkout
            status          TEXT DEFAULT 'PRESENT',  -- PRESENT|ABSENT|LATE|HALF_DAY|LEAVE|HOLIDAY
            is_late         BOOLEAN DEFAULT FALSE,
            note            TEXT,
            marked_by       TEXT DEFAULT 'SELF',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(employee_id, log_date)
        )
    """)

    # Leave requests
    _w("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id     UUID NOT NULL REFERENCES employees(id),
            leave_from      DATE NOT NULL,
            leave_to        DATE NOT NULL,
            leave_type      TEXT DEFAULT 'CL',  -- CL|SL|EL|LWP
            reason          TEXT,
            status          TEXT DEFAULT 'PENDING',  -- PENDING|APPROVED|REJECTED
            applied_at      TIMESTAMPTZ DEFAULT NOW(),
            approved_by     TEXT,
            approved_at     TIMESTAMPTZ,
            remarks         TEXT
        )
    """)

    # Indexes
    _w("CREATE INDEX IF NOT EXISTS idx_att_emp_date ON attendance_logs(employee_id, log_date DESC)")
    _w("CREATE INDEX IF NOT EXISTS idx_att_date     ON attendance_logs(log_date DESC)")
    _w("CREATE INDEX IF NOT EXISTS idx_leave_emp    ON leave_requests(employee_id, leave_from)")


# ══════════════════════════════════════════════════════════════════════════════
# GEO HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return distance in metres between two GPS coordinates."""
    R = 6_371_000  # earth radius metres
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lng2 - lng1)
    a  = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def get_office_location() -> Optional[Dict]:
    rows = _q("SELECT * FROM office_locations WHERE is_active=TRUE ORDER BY created_at LIMIT 1")
    return rows[0] if rows else None


def check_within_fence(lat: float, lng: float) -> Tuple[bool, float, Optional[Dict]]:
    """
    Returns (is_within, distance_m, office_row).
    is_within = True if within office radius.
    """
    office = get_office_location()
    if not office:
        return True, 0.0, None   # no fence configured → allow all

    dist = haversine_m(lat, lng,
                       float(office["latitude"]),
                       float(office["longitude"]))
    radius = float(office.get("radius_m") or 150)
    return dist <= radius, round(dist, 1), office


# ══════════════════════════════════════════════════════════════════════════════
# EMPLOYEE QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_all_employees(active_only: bool = True) -> List[Dict]:
    af = "WHERE is_active=TRUE" if active_only else ""
    return _q(f"""
        SELECT id::text, emp_code, name, phone, role, department,
               salary_type, salary_amount,
               shift_start::text, shift_end::text,
               late_grace_min, weekly_off, join_date::text, is_active, notes
        FROM employees {af}
        ORDER BY name
    """)


def get_employee(emp_id: str) -> Optional[Dict]:
    rows = _q("SELECT * FROM employees WHERE id=%s::uuid LIMIT 1", (emp_id,))
    return rows[0] if rows else None


def save_employee(data: Dict) -> str:
    """Upsert employee. Returns id."""
    eid = data.get("id") or str(uuid.uuid4())
    _w("""
        INSERT INTO employees
            (id, emp_code, name, phone, role, department,
             salary_type, salary_amount, shift_start, shift_end,
             late_grace_min, weekly_off, join_date, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET
            emp_code=EXCLUDED.emp_code, name=EXCLUDED.name,
            phone=EXCLUDED.phone, role=EXCLUDED.role,
            department=EXCLUDED.department,
            salary_type=EXCLUDED.salary_type,
            salary_amount=EXCLUDED.salary_amount,
            shift_start=EXCLUDED.shift_start, shift_end=EXCLUDED.shift_end,
            late_grace_min=EXCLUDED.late_grace_min,
            weekly_off=EXCLUDED.weekly_off, join_date=EXCLUDED.join_date,
            notes=EXCLUDED.notes
    """, (
        eid, data.get("emp_code"), data["name"], data.get("phone"),
        data.get("role","STAFF"), data.get("department"),
        data.get("salary_type","MONTHLY"), float(data.get("salary_amount") or 0),
        data.get("shift_start","10:00"), data.get("shift_end","19:00"),
        int(data.get("late_grace_min") or 15), data.get("weekly_off","Sunday"),
        data.get("join_date", datetime.date.today()), data.get("notes"),
    ))
    return eid


# ══════════════════════════════════════════════════════════════════════════════
# ATTENDANCE
# ══════════════════════════════════════════════════════════════════════════════

def get_today_attendance(emp_id: str) -> Optional[Dict]:
    rows = _q("""
        SELECT * FROM attendance_logs
        WHERE employee_id=%s::uuid AND log_date=CURRENT_DATE LIMIT 1
    """, (emp_id,))
    return rows[0] if rows else None


def check_in(emp_id: str, lat: float, lng: float, acc: float) -> Tuple[bool, str, Dict]:
    """
    Record check-in. Returns (success, message, record).
    """
    # Already checked in today?
    existing = get_today_attendance(emp_id)
    if existing and existing.get("check_in_time"):
        return False, "Already checked in today.", existing

    within, dist, office = check_within_fence(lat, lng)
    emp = get_employee(emp_id)
    if not emp:
        return False, "Employee not found.", {}

    now = datetime.datetime.now()

    # Late check
    shift_start_str = str(emp.get("shift_start") or "10:00")[:5]
    grace = int(emp.get("late_grace_min") or 15)
    try:
        sh, sm  = map(int, shift_start_str.split(":"))
        deadline = now.replace(hour=sh, minute=sm+grace, second=0, microsecond=0)
        is_late  = now > deadline
    except Exception:
        is_late = False

    status = "PRESENT" if not is_late else "LATE"
    if not within and office:
        status = "REMOTE"

    log_id = str(uuid.uuid4())
    ok = _w("""
        INSERT INTO attendance_logs
            (id, employee_id, log_date, check_in_time,
             check_in_lat, check_in_lng, check_in_acc, check_in_dist,
             check_in_valid, status, is_late)
        VALUES (%s,%s::uuid,CURRENT_DATE,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (employee_id, log_date) DO UPDATE SET
            check_in_time=EXCLUDED.check_in_time,
            check_in_lat=EXCLUDED.check_in_lat,
            check_in_lng=EXCLUDED.check_in_lng,
            check_in_acc=EXCLUDED.check_in_acc,
            check_in_dist=EXCLUDED.check_in_dist,
            check_in_valid=EXCLUDED.check_in_valid,
            status=EXCLUDED.status,
            is_late=EXCLUDED.is_late
    """, (log_id, emp_id, now, lat, lng, acc, dist, within, status, is_late))

    geo_msg = f"✅ Within office ({dist:.0f}m)" if within else f"⚠️ Outside office ({dist:.0f}m)"
    late_msg = f" — {'On time' if not is_late else 'Late'}"
    msg = f"Checked in at {now.strftime('%H:%M')}  {geo_msg}{late_msg}"

    rec = get_today_attendance(emp_id) or {}
    return ok, msg, rec


def check_out(emp_id: str, lat: float, lng: float, acc: float) -> Tuple[bool, str, Dict]:
    """Record check-out and compute work hours."""
    existing = get_today_attendance(emp_id)
    if not existing:
        return False, "No check-in found for today.", {}
    if existing.get("check_out_time"):
        return False, "Already checked out today.", existing

    within, dist, _ = check_within_fence(lat, lng)
    now = datetime.datetime.now()

    # Work hours
    ci = existing.get("check_in_time")
    try:
        if hasattr(ci, 'timestamp'):
            work_hrs = round((now - ci).total_seconds() / 3600, 2)
        else:
            ci_dt = datetime.datetime.fromisoformat(str(ci))
            work_hrs = round((now - ci_dt).total_seconds() / 3600, 2)
    except Exception:
        work_hrs = 0.0

    # Half day if < 4.5 hours
    status = existing.get("status", "PRESENT")
    if work_hrs < 4.5 and status not in ("LEAVE", "HOLIDAY"):
        status = "HALF_DAY"

    ok = _w("""
        UPDATE attendance_logs SET
            check_out_time=%s,
            check_out_lat=%s, check_out_lng=%s,
            check_out_dist=%s, check_out_valid=%s,
            work_hours=%s, status=%s
        WHERE employee_id=%s::uuid AND log_date=CURRENT_DATE
    """, (now, lat, lng, dist, within, work_hrs, status, emp_id))

    geo_msg = f"✅ {dist:.0f}m from office" if within else f"⚠️ {dist:.0f}m from office"
    msg = f"Checked out at {now.strftime('%H:%M')}  {geo_msg}  Work: {work_hrs:.1f}h"
    rec = get_today_attendance(emp_id) or {}
    return ok, msg, rec


def mark_absent(emp_id: str, log_date: datetime.date,
                marked_by: str = "Admin") -> bool:
    """Mark absent for a date — admin override."""
    return _w("""
        INSERT INTO attendance_logs (employee_id, log_date, status, marked_by)
        VALUES (%s::uuid, %s, 'ABSENT', %s)
        ON CONFLICT (employee_id, log_date) DO UPDATE SET
            status='ABSENT', marked_by=EXCLUDED.marked_by
    """, (emp_id, log_date, marked_by))


# ══════════════════════════════════════════════════════════════════════════════
# MONTHLY SHEET
# ══════════════════════════════════════════════════════════════════════════════

def get_monthly_sheet(year: int, month: int) -> List[Dict]:
    """All employees × all days for a month."""
    return _q("""
        SELECT
            e.id::text AS emp_id,
            e.name, e.role,
            a.log_date::text,
            COALESCE(a.status,'ABSENT') AS status,
            a.is_late,
            ROUND(COALESCE(a.work_hours,0),1) AS work_hours,
            a.check_in_time::text,
            a.check_out_time::text,
            a.check_in_dist, a.check_in_valid
        FROM employees e
        CROSS JOIN generate_series(
            DATE_TRUNC('month', MAKE_DATE(%s,%s,1)),
            DATE_TRUNC('month', MAKE_DATE(%s,%s,1)) + INTERVAL '1 month' - INTERVAL '1 day',
            INTERVAL '1 day'
        ) AS d(log_date)
        LEFT JOIN attendance_logs a
            ON a.employee_id = e.id AND a.log_date = d.log_date::date
        WHERE e.is_active = TRUE
          AND d.log_date::date <= CURRENT_DATE
        ORDER BY e.name, d.log_date
    """, (year, month, year, month))


def get_monthly_summary(year: int, month: int) -> List[Dict]:
    """Per-employee summary for payroll."""
    return _q("""
        SELECT
            e.id::text AS emp_id, e.name, e.role,
            e.salary_type, e.salary_amount,
            e.shift_start::text, e.shift_end::text,
            COUNT(CASE WHEN COALESCE(a.status,'ABSENT') IN ('PRESENT','LATE') THEN 1 END) AS present_days,
            COUNT(CASE WHEN a.status='ABSENT' OR a.status IS NULL THEN 1 END) AS absent_days,
            COUNT(CASE WHEN a.status='LATE' THEN 1 END) AS late_days,
            COUNT(CASE WHEN a.status='HALF_DAY' THEN 1 END) AS half_days,
            COUNT(CASE WHEN a.status='LEAVE' THEN 1 END) AS leave_days,
            ROUND(SUM(COALESCE(a.work_hours,0)),1) AS total_hours
        FROM employees e
        CROSS JOIN generate_series(
            DATE_TRUNC('month', MAKE_DATE(%s,%s,1)),
            DATE_TRUNC('month', MAKE_DATE(%s,%s,1)) + INTERVAL '1 month' - INTERVAL '1 day',
            INTERVAL '1 day'
        ) AS d(log_date)
        LEFT JOIN attendance_logs a
            ON a.employee_id = e.id AND a.log_date = d.log_date::date
        WHERE e.is_active = TRUE
          AND d.log_date::date <= CURRENT_DATE
        GROUP BY e.id, e.name, e.role, e.salary_type,
                 e.salary_amount, e.shift_start, e.shift_end
        ORDER BY e.name
    """, (year, month, year, month))


def compute_payroll(year: int, month: int) -> List[Dict]:
    """Compute payable salary for each employee."""
    rows = get_monthly_summary(year, month)
    import calendar
    total_days = calendar.monthrange(year, month)[1]

    result = []
    for r in rows:
        present  = int(r.get("present_days") or 0)
        half     = int(r.get("half_days") or 0)
        leave    = int(r.get("leave_days") or 0)
        salary   = float(r.get("salary_amount") or 0)
        stype    = r.get("salary_type", "MONTHLY")

        paid_days = present + leave + (half * 0.5)

        if stype == "MONTHLY":
            payable = round(salary / total_days * paid_days, 2)
        else:  # DAILY
            payable = round(salary * paid_days, 2)

        result.append({
            **r,
            "total_days":  total_days,
            "paid_days":   paid_days,
            "payable":     payable,
            "deduction":   round(salary - payable if stype == "MONTHLY" else 0, 2),
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# LEAVE
# ══════════════════════════════════════════════════════════════════════════════

def apply_leave(emp_id: str, leave_from: datetime.date,
                leave_to: datetime.date, leave_type: str,
                reason: str) -> str:
    lid = str(uuid.uuid4())
    _w("""
        INSERT INTO leave_requests
            (id, employee_id, leave_from, leave_to, leave_type, reason)
        VALUES (%s,%s::uuid,%s,%s,%s,%s)
    """, (lid, emp_id, leave_from, leave_to, leave_type, reason))
    return lid


def approve_leave(leave_id: str, approved_by: str,
                  approve: bool, remarks: str = "") -> bool:
    status = "APPROVED" if approve else "REJECTED"
    ok = _w("""
        UPDATE leave_requests SET
            status=%s, approved_by=%s,
            approved_at=NOW(), remarks=%s
        WHERE id=%s::uuid
    """, (status, approved_by, remarks, leave_id))

    if approve and ok:
        # Mark attendance as LEAVE for each day
        rows = _q("SELECT employee_id, leave_from, leave_to FROM leave_requests WHERE id=%s::uuid", (leave_id,))
        if rows:
            r  = rows[0]
            d  = r["leave_from"]
            if isinstance(d, str):
                d = datetime.date.fromisoformat(d)
            end = r["leave_to"]
            if isinstance(end, str):
                end = datetime.date.fromisoformat(end)
            while d <= end:
                _w("""
                    INSERT INTO attendance_logs (employee_id, log_date, status, marked_by)
                    VALUES (%s::uuid,%s,'LEAVE',%s)
                    ON CONFLICT (employee_id, log_date) DO UPDATE SET
                        status='LEAVE', marked_by=EXCLUDED.marked_by
                """, (str(r["employee_id"]), d, approved_by))
                d += datetime.timedelta(days=1)
    return ok


def get_pending_leaves() -> List[Dict]:
    return _q("""
        SELECT l.id::text, e.name, l.leave_from::text,
               l.leave_to::text, l.leave_type, l.reason,
               l.status, l.applied_at::text
        FROM leave_requests l
        JOIN employees e ON e.id = l.employee_id
        WHERE l.status = 'PENDING'
        ORDER BY l.applied_at
    """)
