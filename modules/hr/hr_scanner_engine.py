"""
modules/hr/hr_scanner_engine.py
================================
Extensions to hr_engine for:
  1. Barcode-based attendance (scan staff card + IN/OUT barcode)
  2. Production stage tracking (scan order + stage barcode)
  3. Checkout enforcement (block next-day login if previous day unclosed)
  4. Admin clearance for unclosed days
"""
from __future__ import annotations
import uuid, datetime, logging
from typing import Optional, Dict, Tuple, List

_log = logging.getLogger(__name__)


def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []

def _w(sql, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or ())
        return True
    except Exception as e:
        _log.warning("[hr_scanner._w] %s", e)
        return False


def _scan_order_uuid_or_ref(value: str) -> str:
    """Normalize scanner/UI composite values like '<uuid>:R/2627/0017'."""
    raw = str(value or "").strip()
    if ":" not in raw:
        return raw
    left, right = raw.split(":", 1)
    try:
        return str(uuid.UUID(left.strip()))
    except Exception:
        return right.strip() or raw


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA EXTENSIONS
# ══════════════════════════════════════════════════════════════════════════════

def ensure_scanner_schema() -> None:
    """Add scanner-specific columns and tables — idempotent."""

    # Add staff_barcode to employees if not exists
    _w("""
        ALTER TABLE employees
        ADD COLUMN IF NOT EXISTS staff_barcode TEXT UNIQUE,
        ADD COLUMN IF NOT EXISTS production_stage_codes TEXT
    """)

    # Add admin_cleared + lan_verified flag to attendance_logs
    _w("""
        ALTER TABLE attendance_logs
        ADD COLUMN IF NOT EXISTS admin_cleared    BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS admin_cleared_by TEXT,
        ADD COLUMN IF NOT EXISTS admin_cleared_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS lan_verified     BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS lan_verified_at  TIMESTAMPTZ
    """)

    # Production stage log
    _w("""
        CREATE TABLE IF NOT EXISTS production_stage_log (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            order_no    TEXT NOT NULL,
            order_id    UUID,
            stage_code  TEXT NOT NULL,
            stage_label TEXT,
            employee_id UUID REFERENCES employees(id),
            emp_name    TEXT,
            emp_role    TEXT,
            emp_department TEXT,
            scanned_at  TIMESTAMPTZ DEFAULT NOW(),
            notes       TEXT
        )
    """)
    _w("""
        ALTER TABLE production_stage_log
        ADD COLUMN IF NOT EXISTS emp_role TEXT,
        ADD COLUMN IF NOT EXISTS emp_department TEXT
    """)
    _w("CREATE INDEX IF NOT EXISTS idx_psl_order  ON production_stage_log(order_no, scanned_at DESC)")
    _w("CREATE INDEX IF NOT EXISTS idx_psl_emp    ON production_stage_log(employee_id, scanned_at DESC)")
    _w("CREATE INDEX IF NOT EXISTS idx_psl_stage  ON production_stage_log(stage_code, scanned_at DESC)")

    # Admin clearance log
    _w("""
        CREATE TABLE IF NOT EXISTS hr_clearance_log (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id     UUID REFERENCES employees(id),
            log_date        DATE,
            cleared_by      TEXT,
            cleared_at      TIMESTAMPTZ DEFAULT NOW(),
            reason          TEXT
        )
    """)


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCTION STAGES — definitions
# ══════════════════════════════════════════════════════════════════════════════

PRODUCTION_STAGES = [
    ("PRODUCTION_PICKED", "Production In"),
    ("PRODUCTION_DONE",  "Production Done"),
    ("INSPECTION",       "Inspection Done"),
    ("HARDCOAT_PICKED",  "Hardcoat In"),
    ("HARDCOAT_DONE",    "Hardcoat Done"),
    ("INSPECTION_AFTER_HC", "Inspection after Hardcoat"),
]

STAGE_MAP = {code: label for code, label in PRODUCTION_STAGES}
STAGE_TARGETS = {
    "INSPECTION_AFTER_HC": "INSPECTION",
}
STAGE_SCAN_ALIASES = {
    "P": "PRODUCTION_PICKED",
    "PIN": "PRODUCTION_PICKED",
    "D": "PRODUCTION_DONE",
    "PDN": "PRODUCTION_DONE",
    "I": "INSPECTION",
    "INSP": "INSPECTION",
    "HI": "HARDCOAT_PICKED",
    "HCIN": "HARDCOAT_PICKED",
    "HD": "HARDCOAT_DONE",
    "HCDN": "HARDCOAT_DONE",
    "IH": "INSPECTION_AFTER_HC",
}
STAGE_PRINT_BARCODES = [
    ("ST:P", "Production In"),
    ("ST:D", "Production Done"),
    ("ST:I", "Inspection Done"),
    ("ST:HI", "Hardcoat In"),
    ("ST:HD", "Hardcoat Done"),
    ("ST:IH", "Inspection after Hardcoat"),
]

# Barcode values for stage barcodes (stuck on lab walls)
STAGE_BARCODES = {code: label for code, label in STAGE_PRINT_BARCODES}

# Special system barcodes
SYSTEM_BARCODES = {
    "SYS:CHECKIN":    "Check IN",
    "SYS:CHECKOUT":   "Check OUT",
    "SYS:CLEARANCE":  "Admin Clearance",
}


# ══════════════════════════════════════════════════════════════════════════════
# EMPLOYEE LOOKUP BY BARCODE
# ══════════════════════════════════════════════════════════════════════════════

def _staff_barcode_candidates(barcode: str) -> list[str]:
    """Normalize common manual/mobile scans into the employee code used in HR."""
    raw = (barcode or "").strip()
    if not raw:
        return []

    values: list[str] = []

    def add(v: str) -> None:
        v = (v or "").strip()
        if v and v not in values:
            values.append(v)

    add(raw)
    cleaned = raw.replace("\r", "").replace("\n", "").strip()
    add(cleaned)

    upper = cleaned.upper()
    for prefix in ("STAFF:", "EMP:", "EMPLOYEE:", "STAFF-", "EMP-", "EMPLOYEE-"):
        if upper.startswith(prefix):
            add(cleaned[len(prefix):].strip())

    upper = values[-1].upper() if values else upper
    if upper.startswith("EMP") and upper[3:].isdigit():
        add("P" + upper[3:].zfill(3))
    if upper.isdigit():
        add("P" + upper.zfill(3))

    return values


def get_employee_by_barcode(barcode: str) -> Optional[Dict]:
    """Find employee by staff_barcode first, then emp_code as a fallback."""
    for candidate in _staff_barcode_candidates(barcode):
        rows = _q("""
            SELECT id::text, emp_code, name, phone, role, department,
                   production_stage_codes,
                   shift_start::text, shift_end::text,
                   late_grace_min, is_active, staff_barcode
            FROM employees
            WHERE (
                    UPPER(TRIM(COALESCE(staff_barcode,''))) = UPPER(TRIM(%s))
                 OR UPPER(TRIM(COALESCE(emp_code,'')))      = UPPER(TRIM(%s))
            )
              AND is_active = TRUE
            ORDER BY CASE
                WHEN UPPER(TRIM(COALESCE(staff_barcode,''))) = UPPER(TRIM(%s)) THEN 0
                ELSE 1
            END
            LIMIT 1
        """, (candidate, candidate, candidate))
        if rows:
            return rows[0]
    return None


def save_staff_barcode(emp_id: str, barcode: str) -> bool:
    return _w("""
        UPDATE employees SET staff_barcode = %s
        WHERE id = %s::uuid
    """, (barcode.strip(), emp_id))


# ══════════════════════════════════════════════════════════════════════════════
# CHECKOUT ENFORCEMENT
# ══════════════════════════════════════════════════════════════════════════════

def has_unclosed_previous_day(emp_id: str) -> Optional[Dict]:
    """
    Returns the unclosed attendance record if employee forgot to check out
    on a previous working day. Returns None if all clear.
    """
    rows = _q("""
        SELECT log_date::text, check_in_time::text, check_out_time::text,
               status, admin_cleared
        FROM attendance_logs
        WHERE employee_id = %s::uuid
          AND log_date < CURRENT_DATE
          AND check_in_time IS NOT NULL
          AND check_out_time IS NULL
          AND COALESCE(admin_cleared, FALSE) = FALSE
          AND status NOT IN ('LEAVE','HOLIDAY','ABSENT')
        ORDER BY log_date DESC
        LIMIT 1
    """, (emp_id,))
    return rows[0] if rows else None


def admin_clear_unclosed(emp_id: str, log_date_str: str,
                          cleared_by: str, reason: str = "") -> bool:
    """Admin/manager marks an unclosed day as cleared so employee can log in."""
    ok = _w("""
        UPDATE attendance_logs
        SET admin_cleared    = TRUE,
            admin_cleared_by = %s,
            admin_cleared_at = NOW(),
            check_out_time   = (log_date + INTERVAL '19 hours'),
            work_hours       = EXTRACT(EPOCH FROM (
                                 (log_date + INTERVAL '19 hours') - check_in_time
                               )) / 3600.0,
            status           = 'PRESENT',
            note             = COALESCE(note,'') || ' [Auto-cleared by ' || %s || ']'
        WHERE employee_id = %s::uuid
          AND log_date    = %s::date
    """, (cleared_by, cleared_by, emp_id, log_date_str))

    if ok:
        _w("""
            INSERT INTO hr_clearance_log
                (employee_id, log_date, cleared_by, reason)
            VALUES (%s::uuid, %s::date, %s, %s)
        """, (emp_id, log_date_str, cleared_by, reason or "Admin clearance"))

    return ok


# ══════════════════════════════════════════════════════════════════════════════
# BARCODE SCAN — UNIFIED ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def process_scan(staff_barcode: str, action_barcode: str) -> Dict:
    """
    Process a two-scan sequence:
      staff_barcode  — employee's personal barcode
      action_barcode — stage barcode (STAGE:xxx) or system (SYS:xxx) or order no

    Returns dict with:
      success: bool
      message: str
      action:  "CHECKIN" | "CHECKOUT" | "STAGE" | "ERROR"
      data:    dict (attendance record or stage log)
    """
    ensure_scanner_schema()

    # Identify employee
    emp = get_employee_by_barcode(staff_barcode)
    if not emp:
        return {
            "success": False,
            "message": f"❌ Staff barcode not recognised: {staff_barcode}",
            "action": "ERROR", "data": {}
        }

    emp_id   = emp["id"]
    emp_name = emp["name"]
    ab       = action_barcode.strip().upper()

    # ── System barcodes ───────────────────────────────────────────────────────
    if ab == "SYS:CHECKIN":
        return _do_checkin(emp_id, emp_name)

    if ab == "SYS:CHECKOUT":
        return _do_checkout(emp_id, emp_name)

    if ab == "SYS:CLEARANCE":
        return {
            "success": False,
            "message": "⚠️ Admin clearance required — contact manager.",
            "action": "ERROR", "data": {"emp_name": emp_name}
        }

    # ── Stage barcodes ────────────────────────────────────────────────────────
    stage_code = _normalise_stage_scan(ab)
    if stage_code:
        return _do_stage(emp_id, emp_name, stage_code, action_barcode)

    # ── Order barcode — auto-detect if it looks like an order number ──────────
    if "/" in action_barcode or action_barcode.upper().startswith(("R/","W/","O")):
        # Treat as order + stage needs second scan — return prompt
        return {
            "success": True,
            "message": f"✅ Order {action_barcode} scanned. Now scan stage barcode.",
            "action":  "ORDER_SCANNED",
            "data":    {"emp_name": emp_name, "order_no": action_barcode}
        }

    return {
        "success": False,
        "message": f"❌ Unknown barcode: {action_barcode}",
        "action":  "ERROR", "data": {}
    }


def process_stage_scan(emp_id: str, emp_name: str,
                       order_no: str, stage_barcode: str) -> Dict:
    """Second scan when order_no is already known."""
    ensure_scanner_schema()
    ab = stage_barcode.strip().upper()
    stage_code = _normalise_stage_scan(ab)
    if stage_code:
        return _do_stage(emp_id, emp_name, stage_code, order_no=order_no)
    return {
        "success": False,
        "message": f"❌ Expected stage barcode, got: {stage_barcode}",
        "action":  "ERROR", "data": {}
    }


def _normalise_stage_scan(barcode: str) -> str:
    code = str(barcode or "").strip().upper()
    if code.startswith("STAGE:"):
        code = code.replace("STAGE:", "", 1)
    elif code.startswith("ST:"):
        code = code.replace("ST:", "", 1)
    else:
        return ""
    return STAGE_SCAN_ALIASES.get(code, code)




def _attendance_gate_for_stage(emp_id: str, emp_name: str) -> Optional[Dict]:
    """Allow production stage scan only after today's CHECK IN and before CHECK OUT."""
    unclosed = has_unclosed_previous_day(emp_id)
    if unclosed:
        return {
            "success": False,
            "message": (
                f"⛔ {emp_name} — Previous day not closed!\n"
                f"Date: {unclosed['log_date']}  Check-in: {str(unclosed.get('check_in_time',''))[:16]}\n"
                f"Manager clearance required before production scan."
            ),
            "action": "BLOCKED",
            "data": {"unclosed": unclosed, "emp_name": emp_name, "emp_id": emp_id},
        }

    rows = _q("""
        SELECT check_in_time::text, check_out_time::text,
               COALESCE(check_in_valid, FALSE)  AS check_in_valid,
               COALESCE(lan_verified, FALSE)     AS lan_verified,
               COALESCE(marked_by, '')           AS marked_by
        FROM attendance_logs
        WHERE employee_id=%s::uuid AND log_date=CURRENT_DATE
        LIMIT 1
    """, (emp_id,))

    if not rows or not rows[0].get("check_in_time"):
        return {
            "success": False,
            "message": (
                f"⛔ {emp_name} — not checked IN today.\n"
                f"Step 1: Check in on mobile (any network).\n"
                f"Step 2: Scan on office LAN to verify presence."
            ),
            "action": "BLOCKED",
            "data": {"emp_name": emp_name, "emp_id": emp_id},
        }

    marked_by    = str(rows[0].get("marked_by") or "").upper()
    lan_verified = rows[0].get("lan_verified")
    is_barcode   = "BARCODE" in marked_by

    # Stage work requires LAN verification OR direct barcode check-in on LAN
    if not lan_verified and not is_barcode:
        return {
            "success": False,
            "message": (
                f"⛔ {emp_name} — checked in via mobile, but office LAN not verified.\n"
                f"Open scanner on office WiFi → Attend tab → tap 🏢 LAN Verify."
            ),
            "action": "NEEDS_LAN",
            "data": {"emp_name": emp_name, "emp_id": emp_id},
        }

    if rows[0].get("check_out_time"):
        return {
            "success": False,
            "message": f"⛔ {emp_name} is already checked OUT today.",
            "action": "BLOCKED",
            "data": {"emp_name": emp_name, "emp_id": emp_id},
        }
    return None


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL ACTION HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def _do_checkin(emp_id: str, emp_name: str) -> Dict:
    # Block if previous day unclosed
    unclosed = has_unclosed_previous_day(emp_id)
    if unclosed:
        return {
            "success": False,
            "message": (
                f"⛔ {emp_name} — Previous day not closed!\n"
                f"Date: {unclosed['log_date']}  Check-in: {str(unclosed.get('check_in_time',''))[:16]}\n"
                f"Contact manager for clearance before logging in."
            ),
            "action": "BLOCKED",
            "data":   {"unclosed": unclosed, "emp_name": emp_name, "emp_id": emp_id}
        }

    # Check already checked in today
    rows = _q("""
        SELECT check_in_time::text, check_out_time::text, status
        FROM attendance_logs
        WHERE employee_id=%s::uuid AND log_date=CURRENT_DATE
    """, (emp_id,))

    if rows and rows[0].get("check_in_time"):
        ci = str(rows[0]["check_in_time"])[:16]
        return {
            "success": True,
            "message": f"✅ {emp_name} already checked in at {ci}",
            "action":  "CHECKIN", "data": rows[0]
        }

    # Do check-in (no GPS needed for barcode scan — WiFi = same premises)
    now  = datetime.datetime.now()
    from modules.hr.hr_engine import get_employee
    emp_full = get_employee(emp_id) or {}
    shift_s  = str(emp_full.get("shift_start","10:00"))[:5]
    grace    = int(emp_full.get("late_grace_min") or 15)
    try:
        sh, sm   = map(int, shift_s.split(":"))
        deadline = now.replace(hour=sh, minute=sm+grace, second=0, microsecond=0)
        is_late  = now > deadline
    except Exception:
        is_late  = False

    status = "LATE" if is_late else "PRESENT"
    log_id = str(uuid.uuid4())
    _w("""
        INSERT INTO attendance_logs
            (id, employee_id, log_date, check_in_time,
             check_in_valid, status, is_late, marked_by)
        VALUES (%s, %s::uuid, CURRENT_DATE, %s, TRUE, %s, %s, 'BARCODE')
        ON CONFLICT (employee_id, log_date) DO UPDATE SET
            check_in_time  = EXCLUDED.check_in_time,
            check_in_valid = TRUE,
            status         = EXCLUDED.status,
            is_late        = EXCLUDED.is_late,
            marked_by      = 'BARCODE'
    """, (log_id, emp_id, now, status, is_late))

    late_str = " — ⏰ LATE" if is_late else " — ✅ On time"
    return {
        "success": True,
        "message": f"✅ {emp_name} checked IN at {now.strftime('%H:%M')}{late_str}",
        "action":  "CHECKIN",
        "data":    {"time": now.strftime("%H:%M"), "status": status}
    }


def _do_lan_verify(emp_id: str, emp_name: str) -> Dict:
    """
    LAN verification — called when staff scans on office WiFi scanner.
    Confirms physical presence in office.
    Sets lan_verified = TRUE on today's attendance record.
    Works whether check-in was done via mobile GPS or directly on LAN.
    """
    ensure_scanner_schema()

    # Must have checked in today first
    rows = _q("""
        SELECT check_in_time::text, check_out_time::text,
               COALESCE(lan_verified, FALSE) AS lan_verified,
               status
        FROM attendance_logs
        WHERE employee_id = %s::uuid AND log_date = CURRENT_DATE
        LIMIT 1
    """, (emp_id,))

    if not rows or not rows[0].get("check_in_time"):
        # No check-in record — do checkin AND set lan_verified in one shot
        now  = datetime.datetime.now()
        from modules.hr.hr_engine import get_employee
        emp_full = get_employee(emp_id) or {}
        shift_s  = str(emp_full.get("shift_start","10:00"))[:5]
        grace    = int(emp_full.get("late_grace_min") or 15)
        try:
            sh, sm   = map(int, shift_s.split(":"))
            deadline = now.replace(hour=sh, minute=sm+grace, second=0, microsecond=0)
            is_late  = now > deadline
        except Exception:
            is_late  = False
        status = "LATE" if is_late else "PRESENT"
        import uuid as _uuid
        log_id = str(_uuid.uuid4())
        _w("""
            INSERT INTO attendance_logs
                (id, employee_id, log_date, check_in_time,
                 check_in_valid, lan_verified, lan_verified_at,
                 status, is_late, marked_by)
            VALUES (%s, %s::uuid, CURRENT_DATE, %s,
                    TRUE, TRUE, NOW(),
                    %s, %s, 'BARCODE+LAN')
            ON CONFLICT (employee_id, log_date) DO UPDATE SET
                check_in_valid  = TRUE,
                lan_verified    = TRUE,
                lan_verified_at = NOW(),
                marked_by       = 'BARCODE+LAN'
        """, (log_id, emp_id, now, status, is_late))
        late_str = " — ⏰ LATE" if is_late else " — ✅ On time"
        return {
            "success":      True,
            "message":      f"✅ {emp_name} checked IN + LAN verified at {now.strftime('%H:%M')}{late_str}\n"
                            f"Stage work unlocked.",
            "action":       "LAN_VERIFY",
            "lan_verified": True,
            "data":         {"time": now.strftime("%H:%M"), "status": status}
        }

    if rows[0].get("check_out_time"):
        return {
            "success": False,
            "message": f"⛔ {emp_name} is already checked OUT today.",
            "action":  "ERROR", "data": {}
        }

    if rows[0].get("lan_verified"):
        return {
            "success": True,
            "message": f"✅ {emp_name} — LAN already verified. You're good to work.",
            "action":  "LAN_VERIFY", "data": rows[0]
        }

    # Set lan_verified = TRUE
    _w("""
        UPDATE attendance_logs
        SET lan_verified    = TRUE,
            lan_verified_at = NOW(),
            check_in_valid  = TRUE,
            marked_by       = COALESCE(marked_by, '') || '+LAN'
        WHERE employee_id = %s::uuid AND log_date = CURRENT_DATE
    """, (emp_id,))

    import datetime as _dt
    return {
        "success":      True,
        "message":      f"✅ {emp_name} — LAN verified at {_dt.datetime.now().strftime('%H:%M')}\n"
                        f"Office presence confirmed. Stage work unlocked.",
        "action":       "LAN_VERIFY",
        "lan_verified": True,
        "data":         {"time": _dt.datetime.now().strftime("%H:%M")}
    }


def _do_checkout(emp_id: str, emp_name: str) -> Dict:
    """Barcode/LAN checkout without GPS."""

    rows = _q("""
        SELECT check_in_time, check_out_time::text, status
        FROM attendance_logs
        WHERE employee_id=%s::uuid AND log_date=CURRENT_DATE
    """, (emp_id,))

    if not rows or not rows[0].get("check_in_time"):
        return {
            "success": False,
            "message": f"❌ {emp_name} has not checked in today.",
            "action":  "ERROR", "data": {}
        }

    if rows[0].get("check_out_time"):
        co = str(rows[0]["check_out_time"])[:16]
        return {
            "success": True,
            "message": f"✅ {emp_name} already checked out at {co}",
            "action":  "CHECKOUT", "data": rows[0]
        }

    now = datetime.datetime.now()
    ci  = rows[0]["check_in_time"]
    try:
        if hasattr(ci, 'timestamp'):
            work_hrs = round((now - ci).total_seconds() / 3600, 2)
        else:
            ci_dt    = datetime.datetime.fromisoformat(str(ci))
            work_hrs = round((now - ci_dt).total_seconds() / 3600, 2)
    except Exception:
        work_hrs = 0.0

    status = rows[0].get("status","PRESENT")
    if work_hrs < 4.5 and status not in ("LEAVE","HOLIDAY"):
        status = "HALF_DAY"

    _w("""
        UPDATE attendance_logs SET
            check_out_time  = %s,
            check_out_valid = TRUE,
            work_hours      = %s,
            status          = %s,
            marked_by       = 'BARCODE'
        WHERE employee_id=%s::uuid AND log_date=CURRENT_DATE
    """, (now, work_hrs, status, emp_id))

    hrs_str  = f"{work_hrs:.1f}h"
    half_str = " (HALF DAY)" if status == "HALF_DAY" else ""
    return {
        "success": True,
        "message": f"✅ {emp_name} checked OUT at {now.strftime('%H:%M')}  Work: {hrs_str}{half_str}",
        "action":  "CHECKOUT",
        "data":    {"time": now.strftime("%H:%M"), "work_hours": work_hrs, "status": status}
    }


def _do_stage(emp_id: str, emp_name: str,
              stage_code: str, order_no: str = "") -> Dict:
    gate = _attendance_gate_for_stage(emp_id, emp_name)
    if gate:
        return gate

    if stage_code not in STAGE_MAP:
        return {
            "success": False,
            "message": f"❌ Unknown stage: {stage_code}",
            "action":  "ERROR", "data": {}
        }

    stage_label = STAGE_MAP[stage_code]
    target_stage = STAGE_TARGETS.get(stage_code, stage_code)
    order_no    = order_no.replace("STAGE:", "").strip()
    emp_ctx = _employee_stage_context(emp_id)
    perm = _stage_permission_gate(stage_code, emp_ctx)
    if perm:
        return perm

    resolved = _resolve_order_jobs_for_scan(order_no)
    try:
        order_id = str(uuid.UUID(str(resolved.get("order_id") or "")))
    except Exception:
        order_id = None
    display_order_no = resolved.get("order_no") or order_no
    jobs = resolved.get("jobs") or []

    if not order_no:
        return {
            "success": False,
            "message": "❌ Scan order barcode first, then scan/tap production stage.",
            "action": "ERROR", "data": {}
        }
    if not order_id:
        return {
            "success": False,
            "message": f"❌ Order not found for scan: {order_no}",
            "action": "ERROR", "data": {"order_no": order_no}
        }
    if not jobs:
        return {
            "success": False,
            "message": f"❌ No active production job found for {display_order_no}. Create/assign job card first.",
            "action": "ERROR", "data": {"order_no": display_order_no}
        }

    advance_result = _advance_scanned_jobs(jobs, target_stage, emp_id, emp_name)
    if not advance_result["success"]:
        return {
            "success": False,
            "message": advance_result["message"],
            "action": "ERROR",
            "data": {"order_no": display_order_no, "stage_code": target_stage}
        }

    log_id = str(uuid.uuid4())
    _w("""
        INSERT INTO production_stage_log
            (id, order_no, order_id, stage_code, stage_label,
             employee_id, emp_name, emp_role, emp_department)
        VALUES (%s, %s, %s::uuid, %s, %s, %s::uuid, %s, %s, %s)
    """, (
        log_id, display_order_no, order_id, target_stage, stage_label,
        emp_id, emp_name, emp_ctx.get("role"), emp_ctx.get("department")
    ))

    # Update order_lines lens_params with current stage
    if order_id:
        _w("""
            UPDATE order_lines
            SET lens_params = COALESCE(lens_params, '{}'::jsonb)
                           || jsonb_build_object(
                                'production_stage',      %s,
                                'production_stage_label', %s,
                                'production_stage_by',   %s,
                                'production_stage_role', %s,
                                'production_stage_department', %s,
                                'production_stage_at',   NOW()::text
                              )
            WHERE order_id = %s::uuid
              AND COALESCE(is_deleted, FALSE) = FALSE
    """, (
            target_stage, stage_label, emp_name,
            emp_ctx.get("role"), emp_ctx.get("department"), order_id
        ))

    now = datetime.datetime.now()
    return {
        "success": True,
        "message": (
            f"✅ {stage_label}\n"
            f"   Order: {display_order_no or '—'}  |  {emp_name}"
            f"{' · ' + emp_ctx.get('department') if emp_ctx.get('department') else ''}\n"
            f"   Advanced: {advance_result['advanced']} job(s)\n"
            f"   {now.strftime('%d %b %Y  %H:%M')}"
        ),
        "action":  "STAGE",
        "data":    {
            "order_no":    display_order_no,
            "stage_code":  target_stage,
            "stage_label": stage_label,
            "emp_name":    emp_name,
            "time":        now.strftime("%H:%M")
        }
    }


def _employee_stage_context(emp_id: str) -> Dict:
    rows = _q("""
        SELECT COALESCE(role,'') AS role,
               COALESCE(department,'') AS department,
               COALESCE(production_stage_codes,'') AS production_stage_codes,
               COALESCE(name,'') AS name
        FROM employees
        WHERE id = %s::uuid
        LIMIT 1
    """, (emp_id,))
    return dict(rows[0]) if rows else {"role": "", "department": "", "production_stage_codes": "", "name": ""}


_STAGE_ALLOWED_WORDS = {
    "PRODUCTION_PICKED": ("PRODUCTION", "SURFACING", "LAB", "LENS"),
    "PRODUCTION_DONE":  ("PRODUCTION", "SURFACING", "LAB", "LENS"),
    "INSPECTION":       ("INSPECTION", "QC", "QUALITY", "LAB"),
    "INSPECTION_AFTER_HC": ("INSPECTION", "QC", "QUALITY", "LAB"),
    "HARDCOAT_PICKED":  ("HARDCOAT", "HARD COAT", "COATING", "HC", "LAB"),
    "HARDCOAT_DONE":    ("HARDCOAT", "HARD COAT", "COATING", "HC", "LAB"),
}


def allowed_stage_codes_for_context(emp_ctx: Dict) -> list:
    """Return production stage buttons/queue actions visible to this employee."""
    explicit = str(emp_ctx.get("production_stage_codes") or "").strip()
    if explicit:
        allowed_explicit = []
        valid = {code for code, _ in PRODUCTION_STAGES}
        for part in explicit.replace("|", ",").replace(";", ",").split(","):
            code = part.strip().upper()
            if code in valid:
                allowed_explicit.append(code)
        if allowed_explicit:
            return list(dict.fromkeys(allowed_explicit))

    role = str(emp_ctx.get("role") or "").upper()
    dept = str(emp_ctx.get("department") or "").upper()
    text = f"{role} {dept}"
    if any(x in text for x in ("ADMIN", "MANAGER", "OWNER", "SUPERVISOR")):
        return [code for code, _ in PRODUCTION_STAGES]
    out = []
    for code, _label in PRODUCTION_STAGES:
        allowed = _STAGE_ALLOWED_WORDS.get(code, ())
        if not allowed or any(word in text for word in allowed):
            out.append(code)
    return out


def allowed_stage_codes_for_staff_barcode(barcode: str) -> list:
    emp = get_employee_by_barcode(barcode or "")
    if not emp:
        return []
    return allowed_stage_codes_for_context(emp)


def _stage_permission_gate(stage_code: str, emp_ctx: Dict) -> Optional[Dict]:
    if stage_code in allowed_stage_codes_for_context(emp_ctx):
        return None
    role = str(emp_ctx.get("role") or "").upper()
    dept = str(emp_ctx.get("department") or "").upper()
    return {
        "success": False,
        "message": (
            f"⛔ Not authorised for {STAGE_MAP.get(stage_code, stage_code)}.\n"
            f"Staff role/department: {role or '—'} / {dept or '—'}.\n"
            "Ask admin to correct employee department/role in HR."
        ),
        "action": "BLOCKED",
        "data": {"stage_code": stage_code, "role": role, "department": dept},
    }


def _compact_scan_value(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _resolve_order_jobs_for_scan(scanned_order: str) -> Dict:
    raw = _scan_order_uuid_or_ref(scanned_order)
    compact = _compact_scan_value(raw)
    eye_filter = ""
    compact_base = compact
    if len(compact) > 2 and compact[-1:] in ("R", "L"):
        eye_filter = compact[-1:]
        compact_base = compact[:-1]

    rows = _q("""
        SELECT
            o.id::text AS order_id,
            o.order_no,
            jm.id::text AS job_id,
            jm.current_stage,
            COALESCE(jm.blank_allocated_qty, 0) AS blank_allocated_qty,
            COALESCE(jm.blank_required_qty, 0) AS blank_required_qty,
            EXISTS (
                SELECT 1
                FROM blank_allocations ba
                WHERE ba.order_line_id = ol.id
                LIMIT 1
            ) AS has_blank_allocation,
            COALESCE(jm.is_closed, FALSE) AS is_closed,
            ol.id::text AS line_id,
            COALESCE(ol.eye_side,'') AS eye_side,
            COALESCE(ol.production_ref,'') AS production_ref
        FROM orders o
        JOIN order_lines ol ON ol.order_id = o.id
        JOIN job_master jm ON jm.order_line_id = ol.id
        WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
          AND (
                o.id::text = %(raw)s
             OR o.order_no = %(raw)s
             OR COALESCE(ol.production_ref,'') = %(raw)s
             OR regexp_replace(UPPER(o.order_no), '[^A-Z0-9]', '', 'g') = %(compact)s
             OR regexp_replace(UPPER(COALESCE(ol.production_ref,'')), '[^A-Z0-9]', '', 'g') = %(compact)s
             OR regexp_replace(UPPER(o.order_no), '[^A-Z0-9]', '', 'g') = %(compact_base)s
             OR regexp_replace(UPPER(COALESCE(ol.production_ref,'')), '[^A-Z0-9]', '', 'g') = %(compact_base)s
          )
          AND (
                %(eye)s = ''
             OR UPPER(COALESCE(ol.eye_side,'')) = %(eye)s
          )
        ORDER BY CASE WHEN UPPER(COALESCE(ol.eye_side,'')) = 'R' THEN 0
                      WHEN UPPER(COALESCE(ol.eye_side,'')) = 'L' THEN 1
                      ELSE 2 END
    """, {"raw": raw, "compact": compact, "compact_base": compact_base, "eye": eye_filter})
    if not rows:
        return {"order_id": None, "order_no": raw, "jobs": []}
    return {
        "order_id": rows[0].get("order_id"),
        "order_no": rows[0].get("order_no") or raw,
        "jobs": rows,
    }


_MOBILE_STAGE_PRECEDENCE = {
    "JOB_CREATED": 0,
    "PRINTED": 0,
    "JOB_PRINTED": 0,
    "BLANK_ALLOCATED": 0,
    "PRODUCTION_PICKED": 1,
    "PRODUCTION_DONE": 2,
    "INSPECTION": 3,
    "HARDCOAT_PICKED": 4,
    "HARDCOAT_DONE": 5,
    "READY_FOR_PACK": 6,
    "READY_TO_BILL": 7,
    "READY_FOR_BILLING": 7,
    "BILLED": 8,
}

_MOBILE_ALLOWED_PREVIOUS = {
    "PRODUCTION_PICKED": {"JOB_CREATED", "PRINTED", "JOB_PRINTED", "BLANK_ALLOCATED", ""},
    "PRODUCTION_DONE": {"PRODUCTION_PICKED"},
    "INSPECTION": {"PRODUCTION_DONE", "HARDCOAT_DONE"},
    "HARDCOAT_PICKED": {"INSPECTION"},
    "HARDCOAT_DONE": {"HARDCOAT_PICKED"},
}


def _mobile_stage_guard(job: Dict, target_stage: str) -> Optional[str]:
    """Fail-closed guard for mobile scans; mirrors the desktop production flow."""
    eye = str(job.get("eye_side") or "").upper() or "JOB"
    cur = str(job.get("current_stage") or "").upper().strip()
    target = str(target_stage or "").upper().strip()

    if str(job.get("is_closed") or "").lower() in ("true", "1", "yes"):
        return f"{eye}: job already closed/billed"

    cur_rank = _MOBILE_STAGE_PRECEDENCE.get(cur, -1)
    target_rank = _MOBILE_STAGE_PRECEDENCE.get(target, -1)
    is_hc_final_inspection = cur == "HARDCOAT_DONE" and target == "INSPECTION"
    if cur_rank >= 2 and target_rank >= 0 and target_rank < cur_rank and not is_hc_final_inspection:
        return (
            f"{eye}: rollback blocked. Current stage is {cur or '—'}; "
            "after Production Done use Reject flow only."
        )

    allowed_prev = _MOBILE_ALLOWED_PREVIOUS.get(target)
    if allowed_prev is not None and cur not in allowed_prev:
        return f"{eye}: cannot move from {cur or 'Not started'} to {target}"

    if target == "PRODUCTION_PICKED":
        allocated_qty = float(job.get("blank_allocated_qty") or 0)
        has_alloc = bool(job.get("has_blank_allocation"))
        if allocated_qty <= 0 and not has_alloc:
            return f"{eye}: blank assignment not saved. Assign blank before Production In."

    return None


def _advance_scanned_jobs(jobs: List[Dict], target_stage: str, emp_id: str, emp_name: str) -> Dict:
    advanced = 0
    blocked = []
    try:
        from modules.sql_adapter import run_query
    except Exception as e:
        return {"success": False, "advanced": 0, "message": f"❌ DB unavailable: {e}"}

    for job in jobs:
        job_id = str(job.get("job_id") or "")
        eye = str(job.get("eye_side") or "").upper() or "JOB"
        cur = str(job.get("current_stage") or "").upper()
        if not job_id:
            blocked.append(f"{eye}: missing job id")
            continue
        if cur == target_stage:
            advanced += 1
            continue
        guard_msg = _mobile_stage_guard(job, target_stage)
        if guard_msg:
            blocked.append(guard_msg)
            continue
        try:
            rows = run_query(
                "SELECT public.advance_job_stage(%(j)s::uuid, %(s)s, %(u)s::uuid) AS result",
                {"j": job_id, "s": target_stage, "u": emp_id},
            ) or []
            result = str((rows[0] or {}).get("result") or "OK") if rows else "OK"
            if result.upper().startswith("ERROR"):
                blocked.append(f"{eye}: {result}")
            else:
                advanced += 1
                try:
                    from modules.sql_adapter import run_write
                    run_write("""
                        UPDATE job_stage_events
                        SET remarks = COALESCE(NULLIF(remarks,''),'HR scanner'),
                            performed_by = COALESCE(performed_by, %(emp_id)s::uuid)
                        WHERE ctid IN (
                            SELECT ctid FROM job_stage_events
                            WHERE job_id = %(j)s::uuid
                              AND stage_code = %(s)s
                            ORDER BY created_at DESC
                            LIMIT 1
                        )
                    """, {"j": job_id, "s": target_stage, "emp_id": emp_id})
                except Exception:
                    pass
        except Exception as e:
            blocked.append(f"{eye}: {e}")

    if blocked:
        return {
            "success": False,
            "advanced": advanced,
            "message": (
                "❌ Stage blocked.\n"
                "Previous stage is not complete or this move is not allowed.\n"
                + "\n".join(blocked[:4])
            )
        }
    return {
        "success": True,
        "advanced": advanced,
        "message": f"✅ Advanced {advanced} job(s) to {target_stage}"
    }


# ══════════════════════════════════════════════════════════════════════════════
# QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_order_stage_history(order_no: str) -> List[Dict]:
    return _q("""
        SELECT stage_label, emp_name,
               scanned_at::text AS scanned_at
        FROM production_stage_log
        WHERE order_no = %s
        ORDER BY scanned_at DESC
    """, (order_no,))


def get_current_stage(order_no: str) -> Optional[Dict]:
    rows = _q("""
        SELECT stage_code, stage_label, emp_name,
               scanned_at::text AS scanned_at
        FROM production_stage_log
        WHERE order_no = %s
        ORDER BY scanned_at DESC LIMIT 1
    """, (order_no,))
    return rows[0] if rows else None


def get_today_stage_log(emp_id: str = None) -> List[Dict]:
    where = "WHERE DATE(scanned_at) = CURRENT_DATE"
    params: tuple = ()
    if emp_id:
        where += " AND employee_id = %s::uuid"
        params = (emp_id,)
    return _q(f"""
        SELECT order_no, stage_label, emp_name,
               scanned_at::text AS scanned_at
        FROM production_stage_log
        {where}
        ORDER BY scanned_at DESC
        LIMIT 100
    """, params)


def get_unclosed_staff() -> List[Dict]:
    """Admin view — who forgot to check out yesterday."""
    return _q("""
        SELECT e.id::text AS id, e.name, e.phone, e.role,
               a.log_date::text,
               a.check_in_time::text,
               a.id::text AS log_id
        FROM attendance_logs a
        JOIN employees e ON e.id = a.employee_id
        WHERE a.log_date < CURRENT_DATE
          AND a.check_in_time IS NOT NULL
          AND a.check_out_time IS NULL
          AND COALESCE(a.admin_cleared, FALSE) = FALSE
          AND a.status NOT IN ('LEAVE','HOLIDAY','ABSENT')
        ORDER BY a.log_date DESC, e.name
    """)
