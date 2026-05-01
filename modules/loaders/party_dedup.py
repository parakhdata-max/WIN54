"""
modules/loaders/party_dedup.py
================================
Wholesale party dedup — load, analyse, resolve, confirm, write.

CONFLICT TYPES (in order of severity):
  EXACT_NAME    — identical name (case-insensitive)
  SAME_MOBILE   — same mobile, different name (could be branch/rename)
  SAME_GSTIN    — same GSTIN, different name (definitely same entity)
  SPELL_SIMILAR — name differs by ≤2 chars (likely typo)
  SAME_BARCODE  — duplicate barcode assigned

RESOLUTION OPTIONS (per group, manual):
  MERGE         — keep one record, move all orders/history to it, delete others
  KEEP_BOTH     — legitimate separate entities (e.g. same owner, diff businesses)
  RENAME        — rename one to distinguish (e.g. "Shri Lenses - Nagpur")
  ASSIGN_NO     — just assign customer_no to all, no other change

AUTO actions (no confirmation needed):
  NEW           — no conflict, auto-assign customer_no

CUSTOMER NUMBER FORMAT:
  CUST + 6-digit zero-padded sequence → CUST000001, CUST000002...
  Never recycled. Once assigned stays forever even if party is deactivated.
"""

import re
import uuid
from typing import List, Dict, Tuple, Optional


# ── Normalisation ─────────────────────────────────────────────────────────────

def _n(s) -> str:
    """Normalise string for comparison."""
    return re.sub(r'[\s\.\,\-\_]+', ' ', (str(s) or '').strip()).lower()

def _nm(mobile) -> str:
    """Normalise mobile — digits only, last 10."""
    digits = re.sub(r'\D', '', str(mobile or ''))
    return digits[-10:] if len(digits) >= 10 else digits

def _similar(a: str, b: str) -> bool:
    """Spelling similarity — edit distance ≤ 2 for names ≥ 6 chars."""
    a, b = _n(a), _n(b)
    if a == b: return False
    if abs(len(a) - len(b)) > 3: return False
    if min(len(a), len(b)) < 6: return False
    diffs = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
    return diffs <= 2


# ── Next customer number ───────────────────────────────────────────────────────

def _next_customer_no() -> str:
    """Get next available CUST number."""
    try:
        from modules.sql_adapter import run_query
        row = run_query("""
            SELECT COALESCE(MAX(
                CASE WHEN customer_no ~ '^CUST[0-9]+$'
                     THEN CAST(SUBSTRING(customer_no, 5) AS INTEGER)
                     ELSE 0 END
            ), 0) AS max_n
            FROM parties
        """) or [{'max_n': 0}]
        return f"CUST{int(row[0]['max_n']) + 1:06d}"
    except:
        return f"CUST{str(uuid.uuid4().int)[:6]}"


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse_parties(parties_df=None) -> Dict:
    """
    Analyse all parties in DB (or provided DataFrame) for conflicts.

    Returns:
    {
      'clean':    [party_rows without any conflict],
      'groups':   [
          {
            'conflict_type': 'EXACT_NAME' | 'SAME_MOBILE' | 'SAME_GSTIN' | 'SPELL_SIMILAR' | 'SAME_BARCODE',
            'parties': [party_row, ...],
            'description': str,
            'suggested_resolution': 'MERGE' | 'KEEP_BOTH' | 'RENAME',
          }
      ],
      'stats': { 'total': N, 'clean': N, 'conflicts': N, 'groups': N }
    }
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                id::text, party_name, party_type, mobile,
                COALESCE(alt_mobile,'')  AS alt_mobile,
                COALESCE(gstin,'')       AS gstin,
                COALESCE(city,'')        AS city,
                COALESCE(barcode,'')     AS barcode,
                COALESCE(customer_no,'') AS customer_no,
                COALESCE(is_active,true) AS is_active,
                created_at::text         AS created_at
            FROM parties
            ORDER BY party_name
        """) or []
    except Exception as ex:
        return {'error': str(ex), 'clean': [], 'groups': [], 'stats': {}}

    if not rows:
        return {'clean': [], 'groups': [], 'stats': {'total': 0, 'clean': 0, 'conflicts': 0, 'groups': 0}}

    in_conflict = set()   # ids already assigned to a group
    groups = []

    # ── Pass 1: Exact name ────────────────────────────────────────────────────
    name_map = {}
    for r in rows:
        k = _n(r['party_name'])
        name_map.setdefault(k, []).append(r)

    for k, grp in name_map.items():
        if len(grp) > 1:
            ids = [r['id'] for r in grp]
            in_conflict.update(ids)
            groups.append({
                'conflict_type': 'EXACT_NAME',
                'parties': grp,
                'description': f'Identical name "{grp[0]["party_name"]}" — {len(grp)} records',
                'suggested_resolution': 'MERGE',
            })

    # ── Pass 2: Same GSTIN (different names) ─────────────────────────────────
    gstin_map = {}
    for r in rows:
        if r['gstin'] and r['gstin'].strip():
            gstin_map.setdefault(r['gstin'].upper().strip(), []).append(r)

    for gstin, grp in gstin_map.items():
        if len(grp) > 1:
            ids = [r['id'] for r in grp]
            new_ids = [i for i in ids if i not in in_conflict]
            if new_ids:
                in_conflict.update(ids)
                groups.append({
                    'conflict_type': 'SAME_GSTIN',
                    'parties': grp,
                    'description': f'Same GSTIN {gstin} — {len(grp)} different names',
                    'suggested_resolution': 'MERGE',
                })

    # ── Pass 3: Same mobile (different names) ─────────────────────────────────
    mobile_map = {}
    for r in rows:
        m = _nm(r['mobile'])
        if m:
            mobile_map.setdefault(m, []).append(r)

    for mob, grp in mobile_map.items():
        if len(grp) > 1:
            ids = [r['id'] for r in grp]
            new_ids = [i for i in ids if i not in in_conflict]
            if new_ids:
                in_conflict.update(ids)
                groups.append({
                    'conflict_type': 'SAME_MOBILE',
                    'parties': grp,
                    'description': f'Same mobile {mob} — {len(grp)} different names (branch / rename?)',
                    'suggested_resolution': 'KEEP_BOTH',
                })

    # ── Pass 4: Spelling similar (not already grouped) ───────────────────────
    remaining = [r for r in rows if r['id'] not in in_conflict]
    checked = set()
    for i, r1 in enumerate(remaining):
        for r2 in remaining[i+1:]:
            pair_key = tuple(sorted([r1['id'], r2['id']]))
            if pair_key in checked:
                continue
            if _similar(r1['party_name'], r2['party_name']):
                checked.add(pair_key)
                in_conflict.update([r1['id'], r2['id']])
                groups.append({
                    'conflict_type': 'SPELL_SIMILAR',
                    'parties': [r1, r2],
                    'description': f'Possible spelling variant: "{r1["party_name"]}" vs "{r2["party_name"]}"',
                    'suggested_resolution': 'RENAME',
                })

    # ── Pass 5: Duplicate barcodes ─────────────────────────────────────────────
    bc_map = {}
    for r in rows:
        if r['barcode'] and r['barcode'].strip():
            bc_map.setdefault(r['barcode'].strip(), []).append(r)

    for bc, grp in bc_map.items():
        if len(grp) > 1:
            ids = [r['id'] for r in grp]
            new_ids = [i for i in ids if i not in in_conflict]
            if new_ids:
                in_conflict.update(ids)
                groups.append({
                    'conflict_type': 'SAME_BARCODE',
                    'parties': grp,
                    'description': f'Duplicate barcode "{bc}" — must be unique',
                    'suggested_resolution': 'RENAME',
                })

    clean = [r for r in rows if r['id'] not in in_conflict]

    return {
        'clean':  clean,
        'groups': groups,
        'stats': {
            'total':     len(rows),
            'clean':     len(clean),
            'conflicts': len(in_conflict),
            'groups':    len(groups),
        }
    }


# ── Resolution actions ────────────────────────────────────────────────────────

def assign_customer_numbers(party_ids: List[str]) -> Dict[str, str]:
    """
    Assign customer_no to parties that don't have one yet.
    Returns {party_id: customer_no} map.
    """
    from modules.sql_adapter import run_query, run_write
    assigned = {}
    for pid in party_ids:
        row = run_query(
            "SELECT customer_no FROM parties WHERE id=%s LIMIT 1", (pid,)
        ) or []
        if row and row[0].get('customer_no'):
            assigned[pid] = row[0]['customer_no']
            continue
        cno = _next_customer_no()
        run_write(
            "UPDATE parties SET customer_no=%s WHERE id=%s",
            (cno, pid)
        )
        assigned[pid] = cno
    return assigned


def merge_parties(keep_id: str, delete_ids: List[str]) -> Tuple[bool, str]:
    """
    Merge: reassign all orders/history from delete_ids to keep_id, then deactivate delete_ids.
    Returns (success, message).
    """
    try:
        from modules.sql_adapter import run_write
        for did in delete_ids:
            # Reassign orders
            run_write("UPDATE orders SET party_id=%s WHERE party_id=%s",    (keep_id, did))
            run_write("UPDATE orders SET party_name=(SELECT party_name FROM parties WHERE id=%s) WHERE party_id=%s", (keep_id, keep_id))
            # Deactivate duplicate
            run_write("UPDATE parties SET is_active=false, notes=COALESCE(notes,'') || ' [MERGED into ' || %s || ']' WHERE id=%s", (keep_id, did))
        return True, f"Merged {len(delete_ids)} record(s) into {keep_id}"
    except Exception as ex:
        return False, str(ex)


def rename_party(party_id: str, new_name: str) -> Tuple[bool, str]:
    """Rename a party."""
    try:
        from modules.sql_adapter import run_write
        run_write("UPDATE parties SET party_name=%s WHERE id=%s", (new_name.strip(), party_id))
        return True, f"Renamed to '{new_name.strip()}'"
    except Exception as ex:
        return False, str(ex)


def auto_assign_clean(clean_parties: List[Dict]) -> int:
    """
    Assign customer_no to all clean parties that don't have one.
    Returns count assigned.
    """
    ids = [p['id'] for p in clean_parties if not p.get('customer_no')]
    if not ids:
        return 0
    assigned = assign_customer_numbers(ids)
    return len(assigned)


# ── Live name similarity — used at entry time ─────────────────────────────────

def _token_similarity(a: str, b: str) -> float:
    """Token-based Jaccard similarity, ignoring common stop words."""
    import re
    STOP = {'the','and','or','of','pvt','ltd','co','&','india','enterprises',
            'traders','agency','agencies','optical','opticals','optics'}
    def tokens(s):
        return set(re.sub(r'[^\w\s]', '', s.lower()).split()) - STOP

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    jaccard = len(inter) / len(union)
    subset = 0.2 if (ta <= tb or tb <= ta) else 0
    return min(1.0, jaccard + subset)


def _token_fuzzy(a: str, b: str) -> float:
    """Average fuzzy match score across content tokens."""
    import re
    def tokens(s):
        return [t for t in re.sub(r'[^\w\s]', '', s.lower()).split() if len(t) > 2]

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0

    def edit(x, y):
        if len(x) < len(y): return edit(y, x)
        if not y: return len(x)
        prev = list(range(len(y)+1))
        for ca in x:
            curr = [prev[0]+1]
            for j, cb in enumerate(y):
                curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(ca!=cb)))
            prev = curr
        return prev[-1]

    scores = []
    for t in ta:
        best = min(edit(t, s) / max(len(t), len(s)) for s in tb)
        scores.append(1 - best)
    return sum(scores) / len(scores)


def name_similarity(a: str, b: str) -> float:
    """Combined similarity score — 0.0 (different) to 1.0 (exact)."""
    if _n(a) == _n(b):
        return 1.0
    return max(_token_similarity(a, b), _token_fuzzy(a, b) * 0.8)


def find_similar_parties(name: str, threshold: float = 0.40,
                          limit: int = 6) -> list:
    """
    Find existing parties with names similar to `name`.
    Returns list of dicts: {id, party_name, mobile, city, gstin,
                             customer_no, similarity, conflict_type}

    conflict_type:
      EXACT       — identical (case/space insensitive)
      SAME_MOBILE — returned if mobile matches (checked separately)
      SIMILAR     — fuzzy name match above threshold
    """
    if not name or not name.strip():
        return []

    try:
        from modules.sql_adapter import run_query
        # Pull all active parties — we need to score client-side
        # (SQL trigram would be ideal but may not be available)
        rows = run_query("""
            SELECT id::text, party_name, mobile,
                   COALESCE(city,'')        AS city,
                   COALESCE(gstin,'')       AS gstin,
                   COALESCE(customer_no,'') AS customer_no,
                   COALESCE(barcode,'')     AS barcode
            FROM parties
            WHERE COALESCE(is_active, true) = true
            ORDER BY party_name
        """) or []
    except Exception:
        return []

    results = []
    for r in rows:
        score = name_similarity(name, r['party_name'])
        if score >= threshold:
            ct = 'EXACT' if score >= 0.99 else 'SIMILAR'
            results.append({**r, 'similarity': round(score, 2), 'conflict_type': ct})

    results.sort(key=lambda x: -x['similarity'])
    return results[:limit]


def suggest_distinguishing_names(name: str, similar: list) -> list:
    """
    Given a new party name and similar existing parties,
    suggest distinguishing suffixes.
    e.g. "Shree Krishna Opticals" might suggest:
      - "Shree Krishna Opticals - Nashik"
      - "Shree Krishna Opticals - 2"
      - "Shree Krishna Opticals - [Owner Name]"
    """
    suggestions = []
    base = name.strip()

    # Count how many already exist with this base
    exact_count = sum(1 for r in similar if r['similarity'] >= 0.99)

    # Suggestion 1: City suffix (if any similar party has a city)
    cities = [r['city'] for r in similar if r.get('city')]
    if cities:
        suggestions.append({
            'name':   f"{base} - [Your City]",
            'reason': f"Existing: {similar[0]['party_name']} ({cities[0]}). Add city to distinguish.",
            'type':   'city'
        })
    else:
        suggestions.append({
            'name':   f"{base} - [City Name]",
            'reason': "Add city/area to make the name unique.",
            'type':   'city'
        })

    # Suggestion 2: Numeric suffix
    n = exact_count + 1
    suggestions.append({
        'name':   f"{base} - {n}",
        'reason': f"{n-1} similar name(s) already exist. Numbered suffix keeps them separate.",
        'type':   'number'
    })

    # Suggestion 3: Owner/contact name
    suggestions.append({
        'name':   f"{base} - [Owner Name]",
        'reason': "Add owner/contact name to distinguish.",
        'type':   'owner'
    })

    # Suggestion 4: Branch
    suggestions.append({
        'name':   f"{base} (Branch)",
        'reason': "If this is a branch of an existing party.",
        'type':   'branch'
    })

    return suggestions
