"""
modules/loaders/patient_dedup.py
==================================
Patient identity resolution — handles all permutations cleanly.

UNIQUE KEY: LOWER(TRIM(master_name)) + COALESCE(TRIM(mobile), '')
  → Same name + same mobile = same person (return visit)
  → Same name + different mobile = different person
  → Different name + same mobile = family member (son, wife etc.)
  → No mobile: suffix counter (-2, -3...) keeps names unique

SCENES HANDLED:
  A. Spelling variation + same mobile → find by mobile, flag spell diff, offer correction
  B. Family members sharing mobile → different name = new patient, relation stored
  C. Same name, no mobile → auto-suffix (Ramesh Gadhvi-2, -3...)
  D. Same name, different mobile → new patient, no conflict
  E. Mobile added later to no-mobile patient → update + strip suffix

PARTY DEDUP: Same logic — LOWER(party_name) + COALESCE(mobile,'') as composite key
"""

import re
import uuid
from typing import Optional, Tuple


# ── Schema guard ─────────────────────────────────────────────────────────────

def _ensure_patient_identity_columns() -> None:
    """Add optional identity fields used by Quick Add on older/live DBs."""
    try:
        from modules.sql_adapter import run_write
        run_write("ALTER TABLE patients ADD COLUMN IF NOT EXISTS relation TEXT DEFAULT 'Self'")
        run_write("ALTER TABLE patients ADD COLUMN IF NOT EXISTS gender TEXT")
        run_write("ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_mobile_key")
        run_write(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_name_mobile "
            "ON patients (LOWER(TRIM(master_name)), COALESCE(TRIM(mobile),'')) "
            "WHERE master_name IS NOT NULL"
        )
    except Exception:
        # Callers still have safe fallbacks for search; never block the page here.
        pass


# ── Normalisation helpers ─────────────────────────────────────────────────────

def _norm_name(name: str) -> str:
    """Lowercase + strip for comparison. Never mutates stored value."""
    return re.sub(r'\s+', ' ', (name or '').strip()).lower()

def _norm_mobile(mobile: str) -> str:
    """Digits only, last 10. Empty string if blank."""
    if not mobile:
        return ''
    digits = re.sub(r'\D', '', str(mobile))
    return digits[-10:] if len(digits) >= 10 else digits

def _is_suffix_name(name: str) -> Tuple[str, Optional[int]]:
    """
    Detect if name has our suffix: 'Ramesh Gadhvi-2' → ('Ramesh Gadhvi', 2)
    Returns (base_name, None) if no suffix.
    """
    m = re.match(r'^(.+?)-(\d+)$', name.strip())
    if m:
        return m.group(1).strip(), int(m.group(2))
    return name.strip(), None


# ── Core resolution ───────────────────────────────────────────────────────────

def resolve_patient(
    name: str,
    mobile: str = None,
    relation: str = 'Self'
) -> dict:
    """
    Resolve a patient to one of:
      - {'action': 'found',   'patient': {...}}            → exact match
      - {'action': 'spell',   'candidates': [...]}         → possible spelling variant
      - {'action': 'family',  'siblings': [...]}           → family on same mobile
      - {'action': 'new',     'stored_name': str}          → create new patient
      - {'action': 'suffix',  'stored_name': str}          → create with suffix
      - {'action': 'error',   'message': str}              → cannot proceed

    Never raises. Always returns a dict with 'action' key.
    """
    try:
        from modules.sql_adapter import run_query
    except Exception as ex:
        return {'action': 'error', 'message': f'DB unavailable: {ex}'}
    _ensure_patient_identity_columns()

    name = (name or '').strip()
    if not name:
        return {'action': 'error', 'message': 'Patient name is required'}

    mobile_norm = _norm_mobile(mobile)
    name_norm   = _norm_name(name)

    # ── Step 1: Exact match (name + mobile) ──────────────────────────────────
    exact = run_query("""
        SELECT id, master_name, mobile, relation, record_no, barcode
        FROM patients
        WHERE LOWER(TRIM(master_name)) = %s
          AND COALESCE(TRIM(mobile), '') = %s
        LIMIT 1
    """, (name_norm, mobile_norm)) or []

    if exact:
        return {'action': 'found', 'patient': exact[0]}

    # ── Step 2: Mobile match but different name = family member ──────────────
    if mobile_norm:
        same_mobile = run_query("""
            SELECT id, master_name, mobile, relation, record_no
            FROM patients
            WHERE COALESCE(TRIM(mobile), '') = %s
            ORDER BY created_at
        """, (mobile_norm,)) or []

        if same_mobile:
            # Different names on same mobile → they're family or a spelling variant
            # Check if any name is very similar (Levenshtein-style: differ by ≤2 chars)
            spell_matches = [
                r for r in same_mobile
                if _similar(_norm_name(r['master_name']), name_norm)
            ]
            if spell_matches:
                return {
                    'action':     'spell',
                    'input_name': name,
                    'candidates': spell_matches,
                    'mobile':     mobile_norm,
                }
            else:
                # Genuinely different name = family member
                return {
                    'action':    'family',
                    'siblings':  same_mobile,
                    'new_name':  name,
                    'relation':  relation,
                    'mobile':    mobile_norm,
                }

    # ── Step 3: Name match but no mobile (or different mobile) ───────────────
    same_name_no_mobile = run_query("""
        SELECT id, master_name, mobile, record_no
        FROM patients
        WHERE LOWER(TRIM(master_name)) = %s
          AND (mobile IS NULL OR TRIM(mobile) = '')
        ORDER BY created_at
    """, (name_norm,)) or []

    # Also check base name (strip suffix) for no-mobile patients
    base_name, _ = _is_suffix_name(name)
    suffix_variants = run_query("""
        SELECT id, master_name, mobile, record_no
        FROM patients
        WHERE LOWER(TRIM(master_name)) LIKE %s
          AND (mobile IS NULL OR TRIM(mobile) = '')
        ORDER BY created_at
    """, (f"{_norm_name(base_name)}%",)) or []

    if not mobile_norm and (same_name_no_mobile or suffix_variants):
        # Need a suffix — find the highest existing number
        all_variants = suffix_variants or same_name_no_mobile
        max_suffix = 1
        for r in all_variants:
            _, n = _is_suffix_name(r['master_name'])
            if n and n > max_suffix:
                max_suffix = n
        next_suffix = max_suffix + 1
        stored_name = f"{base_name}-{next_suffix}"
        return {
            'action':      'suffix',
            'stored_name': stored_name,
            'existing':    all_variants,
            'reason':      f'Name already exists without mobile — stored as {stored_name}',
        }

    # ── Step 4: Truly new patient ─────────────────────────────────────────────
    return {
        'action':      'new',
        'stored_name': name,
        'mobile':      mobile_norm or None,
    }


def _similar(a: str, b: str) -> bool:
    """
    True if strings are probably spelling variants.
    Uses simple edit-distance heuristic: ≤2 char differences for names ≥5 chars.
    """
    if a == b:
        return False  # exact match handled upstream
    if abs(len(a) - len(b)) > 3:
        return False
    min_len = min(len(a), len(b))
    if min_len < 4:
        return False
    # Count differing chars at same positions
    diffs = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
    return diffs <= 2 and min_len >= 5


# ── Save helpers ──────────────────────────────────────────────────────────────

def save_patient(
    name: str,
    mobile: str = None,
    relation: str = 'Self',
    gender: str = None,
    dob: str = None,
    ref_mobile: str = None,
    record_no: str = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve + save patient. Returns (patient_id, error_msg).
    
    Handles all scenes automatically:
      - Exact match → returns existing id (no insert)
      - Spelling variant → caller must confirm (returns None, 'SPELL_CONFIRM_REQUIRED')
      - Family member → inserts new patient with relation
      - No-mobile duplicate → inserts with suffix
      - Truly new → inserts normally
    """
    resolution = resolve_patient(name, mobile, relation)
    action = resolution['action']

    if action == 'found':
        return resolution['patient']['id'], None

    if action == 'error':
        return None, resolution['message']

    if action == 'spell':
        # Caller must present candidates to user for confirmation
        return None, 'SPELL_CONFIRM_REQUIRED'

    # All other actions → insert new patient
    stored_name = resolution.get('stored_name', name)
    mobile_norm = _norm_mobile(mobile) or None

    try:
        from modules.sql_adapter import run_write, run_query
        _ensure_patient_identity_columns()

        # Generate barcode: P + 6-digit sequence
        seq = run_query("SELECT COUNT(*) AS n FROM patients") or [{'n': 0}]
        n = int((seq[0].get('n') or 0)) + 1
        barcode = f"PAT{n:06d}"

        pid = str(uuid.uuid4())
        run_write("""
            INSERT INTO patients
            (id, master_name, mobile, relation, gender, dob, ref_mobile,
             record_no, barcode, is_active, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,true,NOW())
            ON CONFLICT DO NOTHING
        """, (
            pid, stored_name, mobile_norm,
            relation or 'Self', gender or None, dob or None,
            _norm_mobile(ref_mobile) or None,
            record_no or None, barcode
        ))

        # Verify insert
        inserted = run_query("SELECT id FROM patients WHERE id=%s", (pid,)) or []
        if inserted:
            return pid, None
        # ON CONFLICT DO NOTHING fired — try to find the existing row
        found = run_query("""
            SELECT id FROM patients
            WHERE LOWER(TRIM(master_name))=%s AND COALESCE(TRIM(mobile),'')=%s
        """, (_norm_name(stored_name), mobile_norm or '')) or []
        return (found[0]['id'] if found else None), None

    except Exception as ex:
        return None, str(ex)


# ── Party dedup (same model) ──────────────────────────────────────────────────

def resolve_party(name: str, mobile: str = None) -> dict:
    """
    Same logic as resolve_patient but for parties table.
    party_name + mobile = composite key.
    """
    try:
        from modules.sql_adapter import run_query
    except Exception as ex:
        return {'action': 'error', 'message': f'DB unavailable: {ex}'}

    name = (name or '').strip()
    if not name:
        return {'action': 'error', 'message': 'Party name is required'}

    mobile_norm = _norm_mobile(mobile)
    name_norm   = _norm_name(name)

    # Exact match
    exact = run_query("""
        SELECT id, party_name, mobile, party_type, barcode
        FROM parties
        WHERE LOWER(TRIM(party_name))=%s
          AND COALESCE(TRIM(mobile),'')=%s
        LIMIT 1
    """, (name_norm, mobile_norm)) or []

    if exact:
        return {'action': 'found', 'party': exact[0]}

    # Same mobile, different name
    if mobile_norm:
        same_mob = run_query("""
            SELECT id, party_name, mobile FROM parties
            WHERE COALESCE(TRIM(mobile),'')=%s ORDER BY party_name
        """, (mobile_norm,)) or []

        if same_mob:
            spell = [r for r in same_mob if _similar(_norm_name(r['party_name']), name_norm)]
            if spell:
                return {'action': 'spell', 'candidates': spell, 'input_name': name}
            return {'action': 'family', 'siblings': same_mob, 'new_name': name}

    # Same name, no mobile
    same_name = run_query("""
        SELECT id, party_name, mobile FROM parties
        WHERE LOWER(TRIM(party_name)) LIKE %s
          AND (mobile IS NULL OR TRIM(mobile)='')
        ORDER BY party_name
    """, (f"{name_norm}%",)) or []

    if not mobile_norm and same_name:
        max_n = 1
        for r in same_name:
            _, n = _is_suffix_name(r['party_name'])
            if n and n > max_n: max_n = n
        stored = f"{name}-{max_n+1}"
        return {'action': 'suffix', 'stored_name': stored, 'existing': same_name}

    return {'action': 'new', 'stored_name': name, 'mobile': mobile_norm or None}
