"""
modules/patient_merge.py
=========================
Patient Record Cleanup / Merge / History Builder

Features:
  1. Search patients by name / case paper / mobile / alias
  2. Surface near-duplicate records (fuzzy name match, same approximate DOB, etc.)
  3. Correct spelling / add mobile / update old RX power / add notes
  4. Merge two records:
       - One becomes PRIMARY, the other SECONDARY
       - All patient_visits, orders, patient_aliases re-linked to PRIMARY
       - Secondary patient soft-deleted (is_deleted=TRUE)
       - patient_merge_log records who merged what and when
  5. Full combined history shown after merge
  6. Reachable from: sidebar "Patient Merge" page AND inline from Consultation

Called from app.py:
    from modules.patient_merge import render_patient_merge
    render_patient_merge()

Also exposes render_patient_cleanup_widget(pid) for inline use in consultation.py
"""

from __future__ import annotations
import streamlit as st
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

try:
    from modules.core.name_formatter import format_person_name
except Exception:
    def format_person_name(name):
        return " ".join(str(name or "").strip().split())

# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _rq(sql, params=()):
    from modules.sql_adapter import run_query
    return run_query(sql, params) or []


def _rw(sql, params=()):
    from modules.sql_adapter import run_write
    return run_write(sql, params)


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA BOOTSTRAP  (idempotent — safe to run every startup)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_patient_merge_schema():
    """
    Create patient_aliases and patient_merge_log tables if they don't exist.
    Also add soft-delete column to patients if missing.
    Safe to call repeatedly.
    """
    ddl_statements = [
        # Soft-delete flag on patients
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE",

        # Extra columns added lazily elsewhere — ensure they exist here too
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS alt_mobile       TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS email            TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS dob              DATE",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS anniversary_date DATE",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS occupation       TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS merge_primary_id UUID",  # set on secondary after merge

        # ── Medical / Systemic history ────────────────────────────────────
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS diabetes          BOOLEAN DEFAULT FALSE",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS hypertension      BOOLEAN DEFAULT FALSE",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS thyroid           BOOLEAN DEFAULT FALSE",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS cardiac_history   BOOLEAN DEFAULT FALSE",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS asthma            BOOLEAN DEFAULT FALSE",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS drug_allergy      TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS current_medication TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS surgery_history   TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS family_history    TEXT",
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS systemic_notes    TEXT",

        # patient_aliases — old spellings, old case paper numbers, old mobiles
        """
        CREATE TABLE IF NOT EXISTS patient_aliases (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            patient_id  UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            alias_type  TEXT NOT NULL,   -- 'name'|'mobile'|'case_no'|'email'
            alias_value TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            created_by  TEXT DEFAULT current_user,
            UNIQUE(alias_type, alias_value)
        )
        """,

        # Index for fast alias lookups
        "CREATE INDEX IF NOT EXISTS idx_patient_aliases_value ON patient_aliases(alias_value)",
        "CREATE INDEX IF NOT EXISTS idx_patient_aliases_pid   ON patient_aliases(patient_id)",

        # patient_merge_log — full audit of every merge
        """
        CREATE TABLE IF NOT EXISTS patient_merge_log (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            primary_id      UUID NOT NULL,
            secondary_id    UUID NOT NULL,
            primary_name    TEXT,
            secondary_name  TEXT,
            visits_relinked INT DEFAULT 0,
            orders_relinked INT DEFAULT 0,
            merged_by       TEXT DEFAULT current_user,
            merged_at       TIMESTAMPTZ DEFAULT NOW(),
            notes           TEXT
        )
        """,
        "ALTER TABLE patient_merge_log ADD COLUMN IF NOT EXISTS primary_name    TEXT",
        "ALTER TABLE patient_merge_log ADD COLUMN IF NOT EXISTS secondary_name  TEXT",
        "ALTER TABLE patient_merge_log ADD COLUMN IF NOT EXISTS visits_relinked INT DEFAULT 0",
        "ALTER TABLE patient_merge_log ADD COLUMN IF NOT EXISTS orders_relinked INT DEFAULT 0",
        "ALTER TABLE patient_merge_log ADD COLUMN IF NOT EXISTS merged_by       TEXT DEFAULT current_user",
        "ALTER TABLE patient_merge_log ADD COLUMN IF NOT EXISTS merged_at       TIMESTAMPTZ DEFAULT NOW()",
        "ALTER TABLE patient_merge_log ADD COLUMN IF NOT EXISTS notes           TEXT",
    ]
    for ddl in ddl_statements:
        try:
            _rw(ddl)
        except Exception as e:
            logger.warning(f"[PatientMerge] schema DDL skipped: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH
# ─────────────────────────────────────────────────────────────────────────────

def search_patients(query: str, limit: int = 20) -> list[dict]:
    """
    Search patients by name, mobile, record_no (case paper), or alias.
    Returns list of patient dicts with visit_count and last_visit_date.
    """
    if not query or len(query.strip()) < 2:
        return []
    q = query.strip()

    rows = _rq("""
        SELECT DISTINCT
            p.id::text          AS id,
            p.master_name       AS name,
            COALESCE(p.mobile,'')       AS mobile,
            COALESCE(p.alt_mobile,'')   AS alt_mobile,
            COALESCE(p.record_no,'')    AS record_no,
            COALESCE(p.email,'')        AS email,
            p.dob,
            COALESCE(p.is_deleted,FALSE) AS is_deleted,
            COUNT(DISTINCT pv.id)        AS visit_count,
            MAX(pv.visit_date)           AS last_visit
        FROM patients p
        LEFT JOIN patient_visits pv ON pv.patient_id = p.id
        LEFT JOIN patient_aliases pa ON pa.patient_id = p.id
        WHERE COALESCE(p.is_deleted, FALSE) = FALSE
          AND (
            p.master_name     ILIKE %s
            OR p.mobile       ILIKE %s
            OR p.alt_mobile   ILIKE %s
            OR p.record_no    ILIKE %s
            OR pa.alias_value ILIKE %s
          )
        GROUP BY p.id, p.master_name, p.mobile, p.alt_mobile,
                 p.record_no, p.email, p.dob, p.is_deleted
        ORDER BY MAX(pv.visit_date) DESC NULLS LAST, p.master_name
        LIMIT %s
    """, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", limit))
    return [dict(r) for r in rows]


def find_likely_duplicates(patient_id: str, limit: int = 10) -> list[dict]:
    """
    Find records that are likely duplicates of the given patient.
    Strategy:
      1. Similar name (trigram if pg_trgm available, else ILIKE first 4 chars)
      2. Same DOB
      3. Same mobile (could be a re-registered patient)
    Returns candidates ranked by similarity score.
    """
    patient = _rq(
        "SELECT master_name, mobile, dob FROM patients WHERE id=%s::uuid LIMIT 1",
        (patient_id,)
    )
    if not patient:
        return []
    p = patient[0]
    name = str(p.get("master_name") or "")
    mobile = str(p.get("mobile") or "")
    dob = p.get("dob")

    # Build name prefix (first 4 chars, lowercase)
    name_prefix = name[:4].lower() if len(name) >= 4 else name.lower()

    rows = _rq("""
        SELECT DISTINCT
            p.id::text          AS id,
            p.master_name       AS name,
            COALESCE(p.mobile,'')    AS mobile,
            COALESCE(p.record_no,'') AS record_no,
            p.dob,
            COALESCE(p.is_deleted,FALSE) AS is_deleted,
            COUNT(DISTINCT pv.id)  AS visit_count,
            MAX(pv.visit_date)     AS last_visit,
            CASE
                WHEN LOWER(p.mobile) = LOWER(%s) AND %s <> '' THEN 30
                ELSE 0
            END +
            CASE
                WHEN p.dob = %s AND %s IS NOT NULL THEN 25
                ELSE 0
            END +
            CASE
                WHEN LOWER(SUBSTRING(p.master_name,1,4)) = %s THEN 20
                ELSE 0
            END +
            CASE
                WHEN p.master_name ILIKE %s THEN 15
                ELSE 0
            END AS score
        FROM patients p
        LEFT JOIN patient_visits pv ON pv.patient_id = p.id
        WHERE p.id <> %s::uuid
          AND COALESCE(p.is_deleted, FALSE) = FALSE
          AND (
            LOWER(SUBSTRING(p.master_name,1,4)) = %s
            OR (p.mobile = %s AND %s <> '')
            OR (p.dob = %s AND %s IS NOT NULL)
          )
        GROUP BY p.id, p.master_name, p.mobile, p.record_no, p.dob, p.is_deleted
        HAVING (
            CASE WHEN LOWER(p.mobile)=%s AND %s<>'' THEN 30 ELSE 0 END +
            CASE WHEN p.dob=%s AND %s IS NOT NULL THEN 25 ELSE 0 END +
            CASE WHEN LOWER(SUBSTRING(p.master_name,1,4))=%s THEN 20 ELSE 0 END +
            CASE WHEN p.master_name ILIKE %s THEN 15 ELSE 0 END
        ) >= 15
        ORDER BY score DESC, p.master_name
        LIMIT %s
    """, (
        mobile, mobile,
        dob, dob,
        name_prefix,
        f"{name_prefix}%",
        patient_id,
        name_prefix, mobile, mobile, dob, dob,
        mobile, mobile, dob, dob, name_prefix, f"{name_prefix}%",
        limit,
    ))
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# PATIENT DETAIL + HISTORY
# ─────────────────────────────────────────────────────────────────────────────

def get_patient_full(patient_id: str) -> dict:
    rows = _rq("""
        SELECT p.*,
               p.id::text AS id_str,
               COALESCE(p.is_deleted,FALSE)       AS is_deleted,
               COALESCE(p.diabetes,FALSE)          AS diabetes,
               COALESCE(p.hypertension,FALSE)      AS hypertension,
               COALESCE(p.thyroid,FALSE)           AS thyroid,
               COALESCE(p.cardiac_history,FALSE)   AS cardiac_history,
               COALESCE(p.asthma,FALSE)            AS asthma,
               COALESCE(p.drug_allergy,'')         AS drug_allergy,
               COALESCE(p.current_medication,'')   AS current_medication,
               COALESCE(p.surgery_history,'')      AS surgery_history,
               COALESCE(p.family_history,'')       AS family_history,
               COALESCE(p.systemic_notes,'')       AS systemic_notes
        FROM patients p WHERE p.id=%s::uuid LIMIT 1
    """, (patient_id,))
    if not rows:
        return {}
    p = dict(rows[0])
    p["id"] = str(p.get("id") or p.get("id_str") or patient_id)
    # Load aliases
    p["aliases"] = _rq(
        "SELECT alias_type, alias_value, created_at FROM patient_aliases "
        "WHERE patient_id=%s::uuid ORDER BY created_at",
        (patient_id,)
    )
    return p


def get_patient_history(patient_id: str, limit: int = 20) -> list[dict]:
    """All visits + linked orders for this patient, newest first."""
    return _rq("""
        SELECT
            pv.id::text         AS visit_id,
            pv.visit_date,
            pv.visit_name,
            COALESCE(pv.right_sph,0)  AS rsph,
            COALESCE(pv.right_cyl,0)  AS rcyl,
            COALESCE(pv.right_axis,0) AS raxis,
            COALESCE(pv.right_add,0)  AS radd,
            COALESCE(pv.left_sph,0)   AS lsph,
            COALESCE(pv.left_cyl,0)   AS lcyl,
            COALESCE(pv.left_axis,0)  AS laxis,
            COALESCE(pv.left_add,0)   AS ladd,
            o.order_no,
            o.order_type,
            COALESCE(o.total_value,0) AS fee,
            o.status,
            pc.doctor_notes,
            pc.treatment_plan,
            pc.diagnosis
        FROM patient_visits pv
        LEFT JOIN orders o
            ON (o.customer_order_no = pv.id::text
                OR (o.party_id = pv.patient_id
                    AND o.created_at::date = pv.visit_date))
            AND COALESCE(o.is_deleted,FALSE)=FALSE
        LEFT JOIN patient_clinicals pc ON pc.visit_id = pv.id
        WHERE pv.patient_id = %s::uuid
        ORDER BY pv.visit_date DESC, pv.created_at DESC
        LIMIT %s
    """, (patient_id, limit))


# ─────────────────────────────────────────────────────────────────────────────
# CORRECTION
# ─────────────────────────────────────────────────────────────────────────────

def correct_patient(patient_id: str, updates: dict) -> bool:
    """
    Update patient master fields. Old name/mobile auto-saved as aliases.
    updates: {master_name, mobile, alt_mobile, email, dob, anniversary_date, occupation}
    """
    try:
        old = _rq(
            "SELECT master_name, mobile FROM patients WHERE id=%s::uuid LIMIT 1",
            (patient_id,)
        )
        if not old:
            return False
        old = old[0]

        # Save old name/mobile as aliases before overwriting
        old_name = str(old.get("master_name") or "").strip()
        old_mob  = str(old.get("mobile") or "").strip()
        new_name = format_person_name(updates.get("master_name"))
        new_mob  = str(updates.get("mobile") or "").strip()

        if old_name and old_name.lower() != new_name.lower() and new_name:
            _add_alias(patient_id, "name", old_name)
        if old_mob and old_mob != new_mob and new_mob:
            _add_alias(patient_id, "mobile", old_mob)

        # Ensure extra columns exist
        for col_def in ["alt_mobile TEXT", "email TEXT", "dob DATE",
                        "anniversary_date DATE", "occupation TEXT",
                        "diabetes BOOLEAN DEFAULT FALSE",
                        "hypertension BOOLEAN DEFAULT FALSE",
                        "thyroid BOOLEAN DEFAULT FALSE",
                        "cardiac_history BOOLEAN DEFAULT FALSE",
                        "asthma BOOLEAN DEFAULT FALSE",
                        "drug_allergy TEXT", "current_medication TEXT",
                        "surgery_history TEXT", "family_history TEXT",
                        "systemic_notes TEXT"]:
            try:
                _rw(f"ALTER TABLE patients ADD COLUMN IF NOT EXISTS {col_def}")
            except Exception:
                pass

        def _pd(s):
            if not s: return None
            try: return date.fromisoformat(str(s).strip()[:10])
            except Exception: return None

        _rw("""
            UPDATE patients SET
                master_name        = %s,
                mobile             = %s,
                alt_mobile         = NULLIF(%s,''),
                email              = NULLIF(%s,''),
                dob                = %s,
                anniversary_date   = %s,
                occupation         = NULLIF(%s,''),
                diabetes           = %s,
                hypertension       = %s,
                thyroid            = %s,
                cardiac_history    = %s,
                asthma             = %s,
                drug_allergy       = NULLIF(%s,''),
                current_medication = NULLIF(%s,''),
                surgery_history    = NULLIF(%s,''),
                family_history     = NULLIF(%s,''),
                systemic_notes     = NULLIF(%s,'')
            WHERE id = %s::uuid
        """, (
            new_name or old_name,
            new_mob  or old_mob,
            updates.get("alt_mobile",""),
            updates.get("email",""),
            _pd(updates.get("dob")),
            _pd(updates.get("anniversary_date")),
            updates.get("occupation",""),
            bool(updates.get("diabetes",        False)),
            bool(updates.get("hypertension",    False)),
            bool(updates.get("thyroid",         False)),
            bool(updates.get("cardiac_history", False)),
            bool(updates.get("asthma",          False)),
            updates.get("drug_allergy",""),
            updates.get("current_medication",""),
            updates.get("surgery_history",""),
            updates.get("family_history",""),
            updates.get("systemic_notes",""),
            patient_id,
        ))
        return True
    except Exception as e:
        logger.error(f"[PatientMerge] correct_patient failed: {e}")
        return False


def add_manual_rx(patient_id: str, visit_date: date, rx_r: tuple, rx_l: tuple,
                  notes: str = "", case_ref: str = "") -> bool:
    """Add a historical RX entry as a patient_visit row."""
    import uuid as _uuid
    try:
        def _sf(v):
            try: return float(v) if v not in (None,"","None") else None
            except: return None
        def _si(v):
            try: return int(float(v)) if v not in (None,"","None") else None
            except: return None

        vid = str(_uuid.uuid4())
        _rw("""
            INSERT INTO patient_visits (
                id, patient_id, record_no, visit_date, visit_name,
                right_sph, right_cyl, right_axis, right_add,
                left_sph,  left_cyl,  left_axis,  left_add,
                notes, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s, 'Manual Entry',
                %s,%s,%s,%s, %s,%s,%s,%s, %s, NOW()
            )
        """, (
            vid, patient_id,
            case_ref or "",
            visit_date,
            _sf(rx_r[0]), _sf(rx_r[1]), _si(rx_r[2]), _sf(rx_r[3]) if len(rx_r)>3 else None,
            _sf(rx_l[0]), _sf(rx_l[1]), _si(rx_l[2]), _sf(rx_l[3]) if len(rx_l)>3 else None,
            notes or None,
        ))
        if case_ref:
            _add_alias(patient_id, "case_no", case_ref)
        return True
    except Exception as e:
        logger.error(f"[PatientMerge] add_manual_rx failed: {e}")
        return False


def _add_alias(patient_id: str, alias_type: str, alias_value: str):
    """Add an alias (idempotent)."""
    if not alias_value or not alias_value.strip():
        return
    try:
        _rw("""
            INSERT INTO patient_aliases (patient_id, alias_type, alias_value)
            VALUES (%s::uuid, %s, %s)
            ON CONFLICT (alias_type, alias_value) DO NOTHING
        """, (patient_id, alias_type, alias_value.strip()))
    except Exception as e:
        logger.warning(f"[PatientMerge] add_alias failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MERGE
# ─────────────────────────────────────────────────────────────────────────────

def merge_patients(primary_id: str, secondary_id: str, notes: str = "",
                   merged_by: str = "staff") -> dict:
    """
    Merge secondary_id INTO primary_id.

    Steps:
      1. Re-link all patient_visits  (patient_id → primary_id)
      2. Re-link all orders          (party_id   → primary_id)
      3. Re-link all patient_aliases (patient_id → primary_id), skip conflicts
      4. Copy secondary name/mobile as aliases on primary
      5. Mark secondary is_deleted=TRUE, merge_primary_id=primary_id
      6. Write patient_merge_log row
      7. Clear Streamlit cache

    Returns {"ok": True, "visits": n, "orders": n} or {"error": str}
    """
    try:
        if primary_id == secondary_id:
            return {"error": "Primary and secondary are the same patient"}

        primary   = _rq("SELECT master_name,mobile FROM patients WHERE id=%s::uuid LIMIT 1", (primary_id,))
        secondary = _rq("SELECT master_name,mobile FROM patients WHERE id=%s::uuid LIMIT 1", (secondary_id,))
        if not primary or not secondary:
            return {"error": "One or both patients not found"}

        pname = str(primary[0].get("master_name",""))
        sname = str(secondary[0].get("master_name",""))
        smob  = str(secondary[0].get("mobile","") or "")

        # Re-link visits
        _vis = _rq("SELECT COUNT(*) AS n FROM patient_visits WHERE patient_id=%s::uuid", (secondary_id,))
        n_visits = int(_vis[0]["n"]) if _vis else 0
        _rw("UPDATE patient_visits SET patient_id=%s::uuid WHERE patient_id=%s::uuid",
            (primary_id, secondary_id))

        # Re-link orders
        _ords = _rq("SELECT COUNT(*) AS n FROM orders WHERE party_id=%s::uuid", (secondary_id,))
        n_orders = int(_ords[0]["n"]) if _ords else 0
        _rw("UPDATE orders SET party_id=%s::uuid WHERE party_id=%s::uuid",
            (primary_id, secondary_id))

        # Re-link patient_clinicals (if table exists)
        try:
            _rw("UPDATE patient_clinicals SET patient_id=%s::uuid WHERE patient_id=%s::uuid",
                (primary_id, secondary_id))
        except Exception:
            pass

        # Move secondary aliases to primary (skip conflicts)
        try:
            _rw("""
                UPDATE patient_aliases SET patient_id=%s::uuid
                WHERE patient_id=%s::uuid
                  AND (alias_type, alias_value) NOT IN (
                      SELECT alias_type, alias_value FROM patient_aliases
                      WHERE patient_id=%s::uuid
                  )
            """, (primary_id, secondary_id, primary_id))
            # Delete any remaining (conflicted) aliases on secondary
            _rw("DELETE FROM patient_aliases WHERE patient_id=%s::uuid", (secondary_id,))
        except Exception as _ae:
            logger.warning(f"[PatientMerge] alias re-link: {_ae}")

        # Save secondary name/mobile as aliases on primary
        _add_alias(primary_id, "name",   sname)
        if smob:
            _add_alias(primary_id, "mobile", smob)

        # Soft-delete secondary
        _rw("""
            UPDATE patients
               SET is_deleted=TRUE, merge_primary_id=%s::uuid
             WHERE id=%s::uuid
        """, (primary_id, secondary_id))

        # Audit log
        try:
            _rw("""
                INSERT INTO patient_merge_log
                    (primary_id, secondary_id, primary_name, secondary_name,
                     visits_relinked, orders_relinked, merged_by, notes)
                VALUES (%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s)
            """, (primary_id, secondary_id, pname, sname,
                  n_visits, n_orders, merged_by, notes or ""))
        except Exception as _le:
            logger.warning(f"[PatientMerge] merge_log insert: {_le}")

        # Invalidate Streamlit caches
        try:
            from modules.settings.shop_master import _load_all_flags
            _load_all_flags.clear()
        except Exception:
            pass

        return {"ok": True, "visits": n_visits, "orders": n_orders,
                "primary_name": pname, "secondary_name": sname}

    except Exception as e:
        logger.error(f"[PatientMerge] merge failed: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# SHARED UI HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_rx(rsph, rcyl, rax, lsph, lcyl, lax, radd=0, ladd=0):
    def _v(v):
        try:
            f = float(v or 0)
            return f"{f:+.2f}" if f != 0 else "Plano"
        except: return "—"
    def _a(v):
        try: return f"{int(float(v or 0))}°"
        except: return "—"
    r = f"R {_v(rsph)}/{_v(rcyl)}×{_a(rax)}"
    l = f"L {_v(lsph)}/{_v(lcyl)}×{_a(lax)}"
    if float(radd or 0) != 0 or float(ladd or 0) != 0:
        r += f" ADD{_v(radd)}"
        l += f" ADD{_v(ladd)}"
    return r, l


def _patient_card(p: dict, key_prefix: str = ""):
    """Render a compact patient summary card."""
    _pid  = str(p.get("id",""))
    _name = p.get("name","—") or "—"
    _mob  = p.get("mobile","") or ""
    _rec  = p.get("record_no","") or ""
    _vc   = int(p.get("visit_count",0) or 0)
    _lv   = p.get("last_visit")
    _lv_s = str(_lv)[:10] if _lv else "no visits"
    _del  = p.get("is_deleted", False)

    _bg   = "#1e1012" if _del else "#0f1623"
    _border = "#ef4444" if _del else "#334155"
    st.markdown(
        f"<div style='background:{_bg};border:1px solid {_border};"
        f"border-radius:6px;padding:8px 12px;margin-bottom:4px'>"
        f"<span style='font-weight:700;color:#e2e8f0'>{_name}</span>"
        + (f" <span style='color:#64748b;font-size:0.78rem'>#{_rec}</span>" if _rec else "")
        + (f"&nbsp;·&nbsp;<span style='color:#94a3b8;font-size:0.78rem'>{_mob}</span>" if _mob else
           "&nbsp;·&nbsp;<span style='color:#ef4444;font-size:0.75rem'>no mobile</span>")
        + f"<span style='float:right;color:#64748b;font-size:0.75rem'>{_vc} visit(s) · last {_lv_s}</span>"
        + ("&nbsp;<span style='color:#ef4444;font-size:0.72rem'>[MERGED/DELETED]</span>" if _del else "")
        + "</div>",
        unsafe_allow_html=True
    )


# ─────────────────────────────────────────────────────────────────────────────
# INLINE WIDGET  (used from consultation.py history panel)
# ─────────────────────────────────────────────────────────────────────────────

def render_patient_cleanup_widget(current_pid: str):
    """
    Compact cleanup widget for use inside the consultation history panel.
    Shows: likely duplicates for current patient + quick merge/correct actions.
    """
    if not current_pid or len(str(current_pid)) < 10:
        st.caption("No patient selected")
        return

    ensure_patient_merge_schema()

    _cp = get_patient_full(current_pid)
    if not _cp:
        st.caption("Patient not found")
        return

    st.markdown(
        "<div style='font-size:0.8rem;font-weight:700;color:#f59e0b;"
        "margin-bottom:6px'>🔍 Duplicate / Cleanup Check</div>",
        unsafe_allow_html=True
    )

    # Show current patient summary
    _patient_card(_cp, key_prefix="cw_cur")

    # Find duplicates
    _dups = find_likely_duplicates(current_pid, limit=5)
    if not _dups:
        st.caption("✅ No likely duplicates found for this patient")
        return

    st.markdown(
        f"<div style='font-size:0.75rem;color:#f59e0b;margin:6px 0 4px'>"
        f"⚠️ {len(_dups)} possible duplicate(s) found — review below</div>",
        unsafe_allow_html=True
    )

    for _dup in _dups:
        _dup_id   = str(_dup.get("id",""))
        _dup_name = _dup.get("name","—")
        _score    = int(_dup.get("score",0))

        with st.expander(
            f"{'🟡' if _score >= 30 else '🟠'} {_dup_name}  "
            f"{'· ' + _dup.get('mobile','') if _dup.get('mobile') else '· no mobile'}  "
            f"· {_dup.get('visit_count',0)} visit(s)  · score {_score}",
            expanded=False
        ):
            _patient_card(_dup, key_prefix=f"cw_dup_{_dup_id[:6]}")

            _mc1, _mc2 = st.columns(2)
            with _mc1:
                # Merge: current is PRIMARY (keep current patient)
                if st.button(
                    f"🔗 Merge — keep **current** patient as primary",
                    key=f"cw_merge_keep_cur_{_dup_id[:8]}",
                    use_container_width=True,
                    help=f"All {_dup_name}'s history moves to {_cp.get('name','current')}. "
                         f"{_dup_name} is soft-deleted."
                ):
                    _confirm_key = f"cw_confirm_{_dup_id[:8]}"
                    st.session_state[_confirm_key] = "keep_cur"

                # Merge: duplicate is PRIMARY (keep duplicate patient)
                if st.button(
                    f"🔗 Merge — keep **{_dup_name}** as primary",
                    key=f"cw_merge_keep_dup_{_dup_id[:8]}",
                    use_container_width=True,
                    help=f"All current patient's history moves to {_dup_name}. "
                         f"Current patient record is soft-deleted."
                ):
                    _confirm_key = f"cw_confirm_{_dup_id[:8]}"
                    st.session_state[_confirm_key] = "keep_dup"

            _confirm_state = st.session_state.get(f"cw_confirm_{_dup_id[:8]}", "")
            if _confirm_state:
                _keep_cur = _confirm_state == "keep_cur"
                _primary  = current_pid if _keep_cur else _dup_id
                _second   = _dup_id     if _keep_cur else current_pid
                _prim_name = _cp.get("name") if _keep_cur else _dup_name
                _sec_name  = _dup_name if _keep_cur else _cp.get("name")
                st.warning(
                    f"⚠️ Confirm merge: **{_sec_name}**'s history will move to "
                    f"**{_prim_name}**. This cannot be undone without admin help."
                )
                _cn1, _cn2 = st.columns(2)
                with _cn1:
                    if st.button("✅ Yes, merge", key=f"cw_do_merge_{_dup_id[:8]}",
                                 type="primary", use_container_width=True):
                        _result = merge_patients(_primary, _second,
                                                 notes="Merged from consultation widget")
                        if _result.get("ok"):
                            st.success(
                                f"✅ Merged {_sec_name} → {_prim_name}. "
                                f"{_result['visits']} visit(s), {_result['orders']} order(s) relinked. "
                                f"Old name saved as alias — search still works."
                            )
                            st.session_state.pop(f"cw_confirm_{_dup_id[:8]}", None)
                            st.rerun()
                        else:
                            st.error(f"Merge failed: {_result.get('error')}")
                with _cn2:
                    if st.button("❌ Cancel", key=f"cw_cancel_merge_{_dup_id[:8]}",
                                 use_container_width=True):
                        st.session_state.pop(f"cw_confirm_{_dup_id[:8]}", None)
                        st.rerun()

            with _mc2:
                if st.button(
                    f"👁️ View full history",
                    key=f"cw_view_{_dup_id[:8]}",
                    use_container_width=True
                ):
                    st.session_state[f"cw_view_hist_{_dup_id[:8]}"] = True

            if st.session_state.get(f"cw_view_hist_{_dup_id[:8]}"):
                _hist = get_patient_history(_dup_id, limit=10)
                if not _hist:
                    st.caption("No visit history")
                else:
                    for _h in _hist:
                        _r, _l = _fmt_rx(
                            _h["rsph"],_h["rcyl"],_h["raxis"],
                            _h["lsph"],_h["lcyl"],_h["laxis"],
                            _h["radd"],_h["ladd"]
                        )
                        _ono = _h.get("order_no","—") or "—"
                        _note = _h.get("doctor_notes","") or _h.get("diagnosis","") or ""
                        st.markdown(
                            f"**{_h['visit_date']}** `{_ono}`  \n"
                            f"`{_r}`  `{_l}`"
                            + (f"  \n_{_note[:80]}_" if _note else ""),
                            unsafe_allow_html=False
                        )


# ─────────────────────────────────────────────────────────────────────────────
# FULL PAGE  (sidebar "Patient Merge")
# ─────────────────────────────────────────────────────────────────────────────

def render_patient_merge():
    """Full-page patient cleanup / merge tool. Called from app.py."""
    ensure_patient_merge_schema()

    st.markdown(
        "<div style='background:#0f172a;border-left:4px solid #f59e0b;"
        "padding:10px 16px;border-radius:6px;margin-bottom:16px'>"
        "<b style='color:#f59e0b;font-size:1.05rem'>🔀 Patient Record Cleanup & Merge</b>"
        "<div style='color:#94a3b8;font-size:0.78rem;margin-top:3px'>"
        "Correct spelling · add missing mobile · link old case papers · "
        "merge duplicate records · add manual RX history"
        "</div></div>",
        unsafe_allow_html=True
    )

    _tabs = st.tabs([
        "🔍 Search & Correct",
        "🔗 Merge Duplicates",
        "📝 Add Manual RX",
        "📋 Merge Audit Log",
    ])

    # ── Tab 1: Search & Correct ────────────────────────────────────────────
    with _tabs[0]:
        _render_search_correct_tab()

    # ── Tab 2: Merge Duplicates ────────────────────────────────────────────
    with _tabs[1]:
        _render_merge_tab()

    # ── Tab 3: Add Manual RX ──────────────────────────────────────────────
    with _tabs[2]:
        _render_manual_rx_tab()

    # ── Tab 4: Audit Log ──────────────────────────────────────────────────
    with _tabs[3]:
        _render_audit_log_tab()


# ─────────────────────────────────────────────────────────────────────────────
# TAB RENDERERS
# ─────────────────────────────────────────────────────────────────────────────

def _render_search_correct_tab():
    st.markdown("Search by name, mobile, case paper number, or old spelling.")

    _q = st.text_input(
        "Search patient",
        key="pm_search_q",
        placeholder="Arya Tiwari / 9876543210 / Case 1234",
    )

    _results = search_patients(_q) if _q and len(_q) >= 2 else []

    if _q and not _results:
        st.info("No patients found. Try a different spelling or mobile number.")
        return

    if not _results:
        return

    st.caption(f"{len(_results)} result(s)")

    _sel_key = "pm_selected_patient_id"
    for _p in _results:
        _pid = _p["id"]
        _col1, _col2 = st.columns([5, 1])
        with _col1:
            _patient_card(_p)
        with _col2:
            st.markdown("<div style='padding-top:6px'></div>", unsafe_allow_html=True)
            if st.button("✏️ Edit", key=f"pm_sel_{_pid[:8]}", use_container_width=True):
                st.session_state[_sel_key] = _pid

    _edit_pid = st.session_state.get(_sel_key, "")
    if not _edit_pid:
        return

    st.markdown("---")
    st.markdown("**Editing patient record**")

    _full = get_patient_full(_edit_pid)
    if not _full:
        st.error("Patient not found")
        return

    # Show current aliases
    _aliases = _full.get("aliases", [])
    if _aliases:
        _alias_strs = [f"{a['alias_type']}: {a['alias_value']}" for a in _aliases]
        st.caption("Known aliases: " + " · ".join(_alias_strs))

    _ef1, _ef2 = st.columns(2)
    with _ef1:
        _new_name = st.text_input("Full name *",
            value=str(_full.get("master_name","") or ""), key="pm_edit_name")
        _new_mob  = st.text_input("Primary mobile",
            value=str(_full.get("mobile","") or ""), key="pm_edit_mob")
        _new_alt  = st.text_input("Alternate mobile",
            value=str(_full.get("alt_mobile","") or ""), key="pm_edit_alt")
    with _ef2:
        _new_email = st.text_input("Email",
            value=str(_full.get("email","") or ""), key="pm_edit_email")
        _new_occ   = st.text_input("Occupation",
            value=str(_full.get("occupation","") or ""), key="pm_edit_occ")

    _ef3, _ef4 = st.columns(2)
    with _ef3:
        _dob_str = str(_full.get("dob","") or "")[:10]
        _new_dob = st.text_input("Date of birth (YYYY-MM-DD)",
            value=_dob_str, key="pm_edit_dob", placeholder="1990-06-15")
    with _ef4:
        _ann_str = str(_full.get("anniversary_date","") or "")[:10]
        _new_ann = st.text_input("Anniversary (YYYY-MM-DD)",
            value=_ann_str, key="pm_edit_ann", placeholder="2015-02-20")

    # ── Medical / Systemic History ─────────────────────────────────────────
    st.markdown("**Medical / Systemic History**")
    _mh1, _mh2, _mh3, _mh4, _mh5 = st.columns(5)
    with _mh1:
        _new_dm  = st.checkbox("Diabetes",     value=bool(_full.get("diabetes",False)),     key="pm_edit_dm")
    with _mh2:
        _new_htn = st.checkbox("Hypertension", value=bool(_full.get("hypertension",False)), key="pm_edit_htn")
    with _mh3:
        _new_thy = st.checkbox("Thyroid",      value=bool(_full.get("thyroid",False)),      key="pm_edit_thy")
    with _mh4:
        _new_crd = st.checkbox("Cardiac",      value=bool(_full.get("cardiac_history",False)), key="pm_edit_crd")
    with _mh5:
        _new_ast = st.checkbox("Asthma",       value=bool(_full.get("asthma",False)),       key="pm_edit_ast")

    _mt1, _mt2 = st.columns(2)
    with _mt1:
        _new_allergy = st.text_input(
            "Drug allergy",
            value=_full.get("drug_allergy","") or "",
            key="pm_edit_allergy",
            placeholder="e.g. Penicillin, Sulfa drugs"
        )
        _new_meds = st.text_area(
            "Current medication",
            value=_full.get("current_medication","") or "",
            key="pm_edit_meds",
            placeholder="Metformin 500mg, Amlodipine 5mg...",
            height=80,
        )
    with _mt2:
        _new_surg = st.text_area(
            "Surgery history",
            value=_full.get("surgery_history","") or "",
            key="pm_edit_surg",
            placeholder="Cataract surgery 2018 RE...",
            height=80,
        )
        _new_fam = st.text_input(
            "Family ocular history",
            value=_full.get("family_history","") or "",
            key="pm_edit_fam",
            placeholder="e.g. Glaucoma in father"
        )
    _new_sysnotes = st.text_area(
        "Other systemic notes",
        value=_full.get("systemic_notes","") or "",
        key="pm_edit_sysnotes",
        placeholder="Any other relevant medical information...",
        height=68,
    )

    # Add alias manually
    st.markdown("**Add alias / old case paper number**")
    _aa1, _aa2, _aa3 = st.columns([1.5, 2, 1])
    with _aa1:
        _alias_type = st.selectbox("Alias type", ["name","mobile","case_no","email"],
                                   key="pm_alias_type")
    with _aa2:
        _alias_val = st.text_input("Alias value", key="pm_alias_val",
                                   placeholder="old name or case no")
    with _aa3:
        st.markdown("<div style='padding-top:28px'></div>", unsafe_allow_html=True)
        if st.button("➕ Add alias", key="pm_add_alias", use_container_width=True):
            if _alias_val.strip():
                _add_alias(_edit_pid, _alias_type, _alias_val.strip())
                st.success(f"Alias added: {_alias_type} = {_alias_val.strip()}")
                st.rerun()

    if st.button("💾 Save corrections", key="pm_save_edit", type="primary"):
        _ok = correct_patient(_edit_pid, {
            "master_name": _new_name, "mobile": _new_mob,
            "alt_mobile": _new_alt, "email": _new_email,
            "dob": _new_dob, "anniversary_date": _new_ann,
            "occupation": _new_occ,
            "diabetes":        _new_dm,
            "hypertension":    _new_htn,
            "thyroid":         _new_thy,
            "cardiac_history": _new_crd,
            "asthma":          _new_ast,
            "drug_allergy":       _new_allergy,
            "current_medication": _new_meds,
            "surgery_history":    _new_surg,
            "family_history":     _new_fam,
            "systemic_notes":     _new_sysnotes,
        })
        if _ok:
            st.success("✅ Saved — old name/mobile saved as aliases automatically")
            st.session_state.pop(_sel_key, None)
            st.rerun()
        else:
            st.error("Save failed — check logs")

    # History
    st.markdown("---")
    st.markdown("**Visit history**")
    _hist = get_patient_history(_edit_pid)
    if not _hist:
        st.caption("No visits recorded")
    else:
        for _h in _hist:
            _r, _l = _fmt_rx(
                _h["rsph"],_h["rcyl"],_h["raxis"],
                _h["lsph"],_h["lcyl"],_h["laxis"],
                _h["radd"],_h["ladd"]
            )
            _ono  = _h.get("order_no","—") or "—"
            _otype = _h.get("order_type","") or ""
            _fee  = float(_h.get("fee",0) or 0)
            _note = _h.get("doctor_notes","") or _h.get("diagnosis","") or ""
            _tx   = _h.get("treatment_plan","") or ""
            st.markdown(
                f"**{_h['visit_date']}** `{_ono}` "
                + (f"· 🩺" if _otype=="CONSULTATION" else f"· 🛍️" if _otype=="RETAIL" else "")
                + (f" ₹{_fee:.0f}" if _fee else "")
                + f"  \n`{_r}` &nbsp; `{_l}`"
                + (f"  \n_{_note[:100]}_" if _note else "")
                + (f"  \nTx: {_tx[:80]}" if _tx else ""),
                unsafe_allow_html=False
            )
            st.markdown("---")


def _render_merge_tab():
    st.markdown(
        "Search for two records to merge. "
        "The **primary** record is kept. The **secondary** is soft-deleted "
        "and all its history is moved to the primary."
    )

    _mc1, _mc2 = st.columns(2)

    with _mc1:
        st.markdown("**Primary record (keep this)**")
        _q1 = st.text_input("Search primary", key="pm_merge_q1",
                             placeholder="Name / mobile / case no")
        _r1 = search_patients(_q1) if _q1 and len(_q1) >= 2 else []
        _primary_id = st.session_state.get("pm_merge_primary_id","")
        for _p in _r1:
            _pid = _p["id"]
            _col1, _col2 = st.columns([4,1])
            with _col1:
                _patient_card(_p)
            with _col2:
                st.markdown("<div style='padding-top:6px'></div>", unsafe_allow_html=True)
                if st.button("✔ Select", key=f"pm_sel_prim_{_pid[:8]}",
                             type="primary", use_container_width=True):
                    st.session_state["pm_merge_primary_id"] = _pid
                    st.rerun()
        if _primary_id:
            _pp = get_patient_full(_primary_id)
            st.success(f"PRIMARY: {_pp.get('name','?')}")

    with _mc2:
        st.markdown("**Secondary record (merge into primary)**")
        _q2 = st.text_input("Search secondary", key="pm_merge_q2",
                             placeholder="Name / mobile / case no")
        _r2 = search_patients(_q2) if _q2 and len(_q2) >= 2 else []
        _secondary_id = st.session_state.get("pm_merge_secondary_id","")
        for _p in _r2:
            _pid = _p["id"]
            _col1, _col2 = st.columns([4,1])
            with _col1:
                _patient_card(_p)
            with _col2:
                st.markdown("<div style='padding-top:6px'></div>", unsafe_allow_html=True)
                if st.button("✔ Select", key=f"pm_sel_sec_{_pid[:8]}",
                             use_container_width=True):
                    st.session_state["pm_merge_secondary_id"] = _pid
                    st.rerun()
        if _secondary_id:
            _sp = get_patient_full(_secondary_id)
            st.warning(f"SECONDARY (will be merged): {_sp.get('name','?')}")

    _primary_id   = st.session_state.get("pm_merge_primary_id","")
    _secondary_id = st.session_state.get("pm_merge_secondary_id","")

    if _primary_id and _secondary_id:
        if _primary_id == _secondary_id:
            st.error("Primary and secondary cannot be the same patient")
            return

        _pp = get_patient_full(_primary_id)
        _sp = get_patient_full(_secondary_id)
        st.markdown("---")
        st.markdown(
            f"**Ready to merge:**  \n"
            f"**{_sp.get('name')}** ({_sp.get('visit_count',0)} visits, "
            f"{len(_sp.get('aliases',[]))} aliases)  \n"
            f"→ will move into →  \n"
            f"**{_pp.get('name')}** ({_pp.get('visit_count',0)} visits)"
        )

        _merge_notes = st.text_area("Notes (optional)", key="pm_merge_notes",
                                    placeholder="Reason for merge, staff initials, etc.")

        if st.button("🔗 Confirm Merge", key="pm_do_merge",
                     type="primary", use_container_width=True):
            _result = merge_patients(_primary_id, _secondary_id,
                                     notes=_merge_notes, merged_by="admin")
            if _result.get("ok"):
                st.success(
                    f"✅ Merged **{_result['secondary_name']}** into "
                    f"**{_result['primary_name']}**.  \n"
                    f"{_result['visits']} visit(s) and {_result['orders']} order(s) relinked.  \n"
                    f"Old name/mobile saved as searchable aliases — nothing lost."
                )
                st.session_state.pop("pm_merge_primary_id", None)
                st.session_state.pop("pm_merge_secondary_id", None)
                st.rerun()
            else:
                st.error(f"Merge failed: {_result.get('error')}")


def _render_manual_rx_tab():
    st.markdown(
        "Add a historical RX from old case papers / manual records.  \n"
        "Useful when a repeat patient brings in their old paper case record."
    )

    _q = st.text_input("Search patient", key="pm_rx_q",
                        placeholder="Name / mobile / case no")
    _results = search_patients(_q) if _q and len(_q) >= 2 else []
    _sel_pid = st.session_state.get("pm_rx_pid","")

    for _p in _results:
        _pid = _p["id"]
        _c1, _c2 = st.columns([5,1])
        with _c1: _patient_card(_p)
        with _c2:
            st.markdown("<div style='padding-top:6px'></div>", unsafe_allow_html=True)
            if st.button("Select", key=f"pm_rx_sel_{_pid[:8]}", use_container_width=True):
                st.session_state["pm_rx_pid"] = _pid
                st.rerun()

    if not _sel_pid:
        return

    _full = get_patient_full(_sel_pid)
    st.markdown(f"**Adding RX for: {_full.get('name','?')}**")
    st.markdown("---")

    _rx1, _rx2 = st.columns(2)
    with _rx1:
        st.markdown("**Right eye**")
        _r_sph  = st.number_input("SPH", value=0.0, step=0.25, format="%.2f", key="pm_rx_rsph")
        _r_cyl  = st.number_input("CYL", value=0.0, step=0.25, format="%.2f", key="pm_rx_rcyl")
        _r_axis = st.number_input("AXIS", value=0,  step=5,    key="pm_rx_raxis")
        _r_add  = st.number_input("ADD", value=0.0, step=0.25, format="%.2f", key="pm_rx_radd")
    with _rx2:
        st.markdown("**Left eye**")
        _l_sph  = st.number_input("SPH", value=0.0, step=0.25, format="%.2f", key="pm_rx_lsph")
        _l_cyl  = st.number_input("CYL", value=0.0, step=0.25, format="%.2f", key="pm_rx_lcyl")
        _l_axis = st.number_input("AXIS", value=0,  step=5,    key="pm_rx_laxis")
        _l_add  = st.number_input("ADD", value=0.0, step=0.25, format="%.2f", key="pm_rx_ladd")

    _rx3, _rx4 = st.columns(2)
    with _rx3:
        _vdate = st.date_input("Visit date", value=date.today(), key="pm_rx_date")
    with _rx4:
        _case_ref = st.text_input("Case paper / reference no",
                                   key="pm_rx_case", placeholder="e.g. 1234")

    _notes = st.text_area("Notes", key="pm_rx_notes",
                           placeholder="Diagnosis, prescription notes, etc.")

    if st.button("💾 Save historical RX", key="pm_rx_save", type="primary"):
        _ok = add_manual_rx(
            _sel_pid, _vdate,
            rx_r=(_r_sph, _r_cyl, _r_axis, _r_add),
            rx_l=(_l_sph, _l_cyl, _l_axis, _l_add),
            notes=_notes, case_ref=_case_ref,
        )
        if _ok:
            st.success(
                f"✅ RX saved for {_vdate}. "
                + (f"Case #{_case_ref} saved as alias — searchable." if _case_ref else "")
            )
            for k in ["pm_rx_pid","pm_rx_q"]: st.session_state.pop(k, None)
            st.rerun()
        else:
            st.error("Save failed — check logs")


def _render_audit_log_tab():
    st.markdown("**Recent merge operations**")
    try:
        ensure_patient_merge_schema()
        _log_rows = _rq("""
            SELECT primary_name, secondary_name, visits_relinked, orders_relinked,
                   merged_by, merged_at, notes
            FROM patient_merge_log
            ORDER BY merged_at DESC
            LIMIT 50
        """)
        if not _log_rows:
            st.caption("No merges recorded yet")
            return
        for _lr in _log_rows:
            _at = str(_lr.get("merged_at",""))[:16]
            _by = _lr.get("merged_by","?")
            _note = _lr.get("notes","") or ""
            st.markdown(
                f"**{_at}** &nbsp; by {_by}  \n"
                f"Merged **{_lr['secondary_name']}** → **{_lr['primary_name']}** "
                f"({_lr['visits_relinked']} visits, {_lr['orders_relinked']} orders)"
                + (f"  \n_{_note}_" if _note else ""),
                unsafe_allow_html=False
            )
            st.markdown("---")
    except Exception as _le:
        st.caption(f"Merge log unavailable: {_le}")
