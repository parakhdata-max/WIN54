"""
modules/billing/payment_collection.py
=======================================
Payment Collection — DV ERP v1.0
SAP-style checkbox-based multi-document clearance.

TABS: Retail | Wholesale | All
FEATURES:
  • Live search — parties (wholesale/retail) + patients
  • All open invoices, challans, on-account orders shown as checkboxes
  • Smart allocation: challan inside invoice → auto-skip (no double-count)
  • Amount auto-fills from selected docs; editable for partial payment
  • Partial payment waterfall: fills docs top-to-bottom, stops when money runs out
  • Excess → On Account with narration
  • Post-save: cleared list, WA Receipt, WA+Balance, WA Custom, Print Receipt
  • Monthly statement (6-month ledger WA)
  • Payment disbursement tab (pay supplier / expense)
"""

import streamlit as st
import uuid
import datetime
import urllib.parse
import logging
from typing import List, Dict, Optional, Tuple

try:
    from rapidfuzz import process as _rfprocess, fuzz as _rffuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False

_log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _q(sql: str, params=None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as _qe:
        import logging as _ql
        _ql.getLogger(__name__).warning(f"[pc._q] {_qe}")
        try: st.error(f"SQL ERROR: {_qe}")
        except: pass
        return []


def _w(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception:
        return False


def _tx(steps: list) -> Tuple[bool, Optional[str]]:
    try:
        from modules.sql_adapter import run_transaction
        run_transaction(steps)
        return True, None
    except Exception:
        ok, err = True, None
        for sql, params in steps:
            try:
                from modules.sql_adapter import run_write
                run_write(sql, params)
            except Exception as se:
                ok, err = False, str(se)
        return ok, err


def _fc(v) -> str:
    try:
        return "₹{:,.2f}".format(float(v or 0))
    except Exception:
        return "₹0.00"


def _fd(v) -> str:
    if not v:
        return "—"
    try:
        if hasattr(v, "strftime"):
            return v.strftime("%d %b %Y")
        return datetime.datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        return str(v)[:10]


def _shop():
    try:
        from modules.settings.shop_master import get_unit_info
        return get_unit_info("retail") or {}
    except Exception:
        return {}


def _gen_pno() -> str:
    """
    Generate next payment number via central registry.
    The registry uses SELECT FOR UPDATE — duplicates are impossible
    under the registry design, so the old duplicate-guard check is removed.
    """
    try:
        from modules.db.order_number_registry import alloc_doc_number
        return alloc_doc_number("PAYMENT")
    except Exception:
        return "PAY/{}/{}".format(
            datetime.date.today().strftime("%y%m%d"),
            uuid.uuid4().hex[:6].upper()
        )


def _ensure_tables():
    _w("""CREATE TABLE IF NOT EXISTS party_ledger (
        id            BIGSERIAL PRIMARY KEY,
        party_id      UUID,
        party_name    TEXT,
        entry_date    DATE    DEFAULT CURRENT_DATE,
        entry_type    TEXT,
        ref_id        TEXT,
        ref_no        TEXT,
        credit        NUMERIC(14,2) DEFAULT 0,
        debit         NUMERIC(14,2) DEFAULT 0,
        narration     TEXT,
        created_by    TEXT,
        created_at    TIMESTAMPTZ   DEFAULT NOW()
    )""")
    # Add created_by column if missing (migration for existing tables)
    _w("""ALTER TABLE party_ledger ADD COLUMN IF NOT EXISTS created_by TEXT""")
    # Reversal columns
    try:
        from modules.billing.services.reversal_service import ensure_reversal_columns
        ensure_reversal_columns()
    except Exception:
        pass
    # Indexes for fast ledger queries
    _w("""CREATE INDEX IF NOT EXISTS idx_party_ledger_party
          ON party_ledger(party_id) WHERE party_id IS NOT NULL""")
    _w("""CREATE INDEX IF NOT EXISTS idx_party_ledger_name
          ON party_ledger(party_name)""")
    _w("""CREATE INDEX IF NOT EXISTS idx_party_ledger_date
          ON party_ledger(entry_date DESC)""")


# ══════════════════════════════════════════════════════════════════════════════
# CACHED PARTY LOADER + FUZZY SEARCH
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def _load_all_parties():
    """Load ALL active parties + patients separately, build search_key, cache 5 min."""
    # Load parties (no shared limit with patients)
    rows = []
    try:
        rows = _q("""
            SELECT id::text, party_name, COALESCE(mobile,'') AS mobile,
                   COALESCE(city,'') AS city,
                   COALESCE(party_type,'') AS party_type,
                   COALESCE(gstin,'') AS gstin, 'PARTY' AS record_type
            FROM parties WHERE COALESCE(is_active,TRUE)=TRUE
            ORDER BY party_name
        """)
    except Exception:
        pass
    # Load patients separately (own query, no shared limit)
    try:
        pts = _q("""
            SELECT id::text, COALESCE(master_name,'') AS party_name,
                   COALESCE(mobile,'') AS mobile, '' AS city,
                   'RETAIL' AS party_type, '' AS gstin, 'PATIENT' AS record_type
            FROM patients ORDER BY master_name
        """)
        seen = {r["id"] for r in rows}
        rows += [r for r in pts if r["id"] not in seen]
    except Exception:
        pass
    for r in rows:
        r["search_key"] = " ".join(filter(None, [
            str(r.get("party_name","") or ""),
            str(r.get("mobile","") or ""),
            str(r.get("city","") or ""),
            str(r.get("gstin","") or ""),
        ])).lower()
    return rows


def _fuzzy_search(term: str, ptype: str = "All", limit: int = 12) -> List[Dict]:
    """
    RapidFuzz-powered search — typo-tolerant, instant, no DB hit.
    Falls back to substring match if rapidfuzz unavailable.
    """
    t = (term or "").strip()
    if not t:
        return []

    all_p = _load_all_parties()

    # Type filter
    if ptype == "Wholesale":
        candidates = [p for p in all_p
                      if str(p.get("party_type","")).upper()
                      not in ("RETAIL","DOCTOR","PATIENT","")]
    elif ptype == "Retail":
        candidates = [p for p in all_p
                      if str(p.get("party_type","")).upper()
                      in ("RETAIL","DOCTOR","PATIENT","")
                      or p.get("record_type") == "PATIENT"]
    else:
        candidates = all_p

    if not candidates:
        return []

    if _HAS_RAPIDFUZZ:
        keys    = [p["search_key"] for p in candidates]
        matches = _rfprocess.extract(
            t.lower(), keys,
            scorer=_rffuzz.WRatio,
            limit=limit,
            score_cutoff=35,
        )
        return [candidates[m[2]] for m in matches]

    # Fallback substring
    tl = t.lower()
    return [p for p in candidates if tl in p["search_key"]][:limit]


# ══════════════════════════════════════════════════════════════════════════════
# CSS — PROFESSIONAL DARK ENTERPRISE THEME
# ══════════════════════════════════════════════════════════════════════════════

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');

/* ── Scoped variables ── */
.pc-root {
    --bg-card:    #0d1929;
    --bg-panel:   #0a1120;
    --border:     #1e3a5f;
    --border-hi:  #2563eb;
    --text-pri:   #e2e8f0;
    --text-sec:   #64748b;
    --text-muted: #334155;
    --green:      #10b981;
    --blue:       #3b82f6;
    --amber:      #f59e0b;
    --red:        #ef4444;
    --purple:     #8b5cf6;
    --cyan:       #06b6d4;
    font-family: 'Plus Jakarta Sans', sans-serif;
}

/* ── Party search card ── */
.pc-search-wrap {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 12px;
}

/* ── Party banner ── */
.pc-party-banner {
    display: flex;
    align-items: center;
    gap: 14px;
    background: linear-gradient(135deg, #0d1f3c 0%, #0a1629 100%);
    border: 1px solid #1e3a5f;
    border-left: 4px solid #2563eb;
    border-radius: 8px;
    padding: 12px 18px;
    margin: 6px 0 14px;
}
.pc-party-avatar {
    width: 42px; height: 42px;
    background: linear-gradient(135deg, #1d4ed8, #7c3aed);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.1rem; font-weight: 700; color: #fff;
    flex-shrink: 0;
}
.pc-party-name   { font-size: 1rem; font-weight: 700; color: #e2e8f0; }
.pc-party-meta   { font-size: 0.7rem; color: #64748b; margin-top: 2px; }
.pc-party-type   {
    font-size: 0.6rem; font-weight: 700;
    padding: 2px 8px; border-radius: 10px;
    background: #1e3a5f; color: #93c5fd;
    letter-spacing: .06em; text-transform: uppercase;
    margin-left: 10px;
}

/* ── Metrics row ── */
.pc-metrics {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 10px; margin: 12px 0;
}
.pc-metric {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
}
.pc-metric-label { font-size: 0.62rem; color: var(--text-sec); text-transform: uppercase; letter-spacing: .08em; }
.pc-metric-value { font-size: 1.3rem; font-weight: 700; font-family: 'IBM Plex Mono', monospace; margin-top: 3px; }
.pc-metric-sub   { font-size: 0.65rem; color: var(--text-sec); margin-top: 2px; }

/* ── Document list ── */
.pc-doc-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78rem;
    margin: 8px 0;
}
.pc-doc-table thead tr {
    background: #0a1120;
    color: #475569;
    font-size: 0.62rem;
    text-transform: uppercase;
    letter-spacing: .08em;
}
.pc-doc-table th { padding: 9px 10px; font-weight: 600; }
.pc-doc-table td { padding: 9px 10px; border-bottom: 1px solid #1e293b; vertical-align: middle; }
.pc-doc-table tbody tr:hover { background: #0d1f3c; }
.pc-doc-table tbody tr.covered { opacity: 0.45; }

/* ── Doc type pills ── */
.pill {
    display: inline-block;
    padding: 2px 8px; border-radius: 10px;
    font-size: 0.6rem; font-weight: 700;
    letter-spacing: .06em; text-transform: uppercase;
}
.pill-inv   { background: #10b98122; color: #10b981; border: 1px solid #10b98144; }
.pill-chal  { background: #3b82f622; color: #3b82f6; border: 1px solid #3b82f644; }
.pill-oa    { background: #8b5cf622; color: #8b5cf6; border: 1px solid #8b5cf644; }
.pill-warn  { background: #f59e0b22; color: #f59e0b; border: 1px solid #f59e0b44; }

/* ── Allocation summary card ── */
.pc-alloc-card {
    background: #0a1829;
    border: 1px solid #1e3a5f;
    border-radius: 10px;
    padding: 18px 22px;
    margin: 16px 0;
}
.pc-alloc-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid #1e293b;
    font-size: 0.78rem;
}
.pc-alloc-row:last-child { border-bottom: none; }
.pc-alloc-total {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 0 4px;
    font-size: 1rem; font-weight: 700;
    border-top: 2px solid #2563eb;
    margin-top: 6px;
    color: #e2e8f0;
}
.pc-alloc-amount { font-family: 'IBM Plex Mono', monospace; font-weight: 600; }

/* ── Payment form ── */
.pc-form-section {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
    margin: 12px 0;
}
.pc-form-title {
    font-size: 0.7rem; font-weight: 700; color: #475569;
    text-transform: uppercase; letter-spacing: .1em;
    margin-bottom: 14px;
    display: flex; align-items: center; gap: 6px;
}

/* ── Mode buttons ── */
.pc-mode-grid {
    display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0;
}
.pc-mode-btn {
    padding: 5px 12px; border-radius: 6px;
    border: 1px solid #1e3a5f;
    background: #0a1120; color: #64748b;
    font-size: 0.72rem; font-weight: 600; cursor: pointer;
    transition: all .15s;
}
.pc-mode-btn.active {
    background: #1d4ed8; color: #fff;
    border-color: #2563eb;
}

/* ── Cleared summary ── */
.pc-cleared-card {
    background: linear-gradient(135deg, #064e3b 0%, #0a2d1f 100%);
    border: 1px solid #065f46;
    border-radius: 10px;
    padding: 18px 22px;
    margin: 12px 0;
}
.pc-cleared-title { font-size: 0.78rem; font-weight: 700; color: #34d399; margin-bottom: 10px; }
.pc-cleared-item  { font-size: 0.74rem; color: #a7f3d0; padding: 3px 0; display: flex; gap: 8px; align-items: center; }

/* ── WA / Print buttons ── */
.pc-action-btn {
    display: inline-block; padding: 8px 14px;
    border-radius: 6px; font-size: 0.72rem; font-weight: 700;
    text-decoration: none; text-align: center; cursor: pointer;
}
.pc-wa-rcpt   { background: #25d366; color: #fff; }
.pc-wa-bal    { background: #128c7e; color: #fff; }
.pc-wa-cust   { background: #075e54; color: #fff; }

/* ── Section divider ── */
.pc-divider {
    border: none; border-top: 1px solid #1e293b; margin: 18px 0;
}

/* ── No docs state ── */
.pc-empty {
    text-align: center; padding: 32px 20px;
    color: #475569; font-size: 0.8rem;
}
.pc-empty-icon { font-size: 2rem; margin-bottom: 8px; }
</style>
"""




# ══════════════════════════════════════════════════════════════════════════════
# OPEN DOCUMENTS
# ══════════════════════════════════════════════════════════════════════════════

def _open_docs(party_id: str) -> Dict:
    """
    Single CTE query — invoices + challans + orders in one DB roundtrip.
    Handles:
      - party_id::text comparison (no uuid cast failure)
      - Challans marked INVOICED are shown (covered_challan_ids handles dedup)
      - Name-based fallback for patient orders with NULL party_id
    """
    pid = str(party_id or "").strip()
    if not pid:
        return {"docs":[],"invoices":[],"challans":[],"orders":[],
                "covered_challan_ids":set(),"total":0,"net_total":0}

    # Resolve name for patient fallback
    pname = ""
    nr = _q("SELECT master_name AS n FROM patients WHERE id::text = %s LIMIT 1", (pid,))
    if nr: pname = nr[0].get("n","")
    if not pname:
        nr = _q("SELECT party_name AS n FROM parties WHERE id::text = %s LIMIT 1", (pid,))
        if nr: pname = nr[0].get("n","")

    pn = "%" + pname + "%" if pname else "__none__"

    # Collect all order refs for patient invoices (NULL party_id on invoice)
    order_rows = _q("""
        SELECT id::text, order_no::text FROM orders
        WHERE (party_id::text = %s OR party_name ILIKE %s OR patient_name ILIKE %s)
          AND COALESCE(is_deleted,FALSE) = FALSE LIMIT 200
    """, (pid, pn, pn))
    all_refs = list(set(
        [r["id"] for r in order_rows if r.get("id")] +
        [r["order_no"] for r in order_rows if r.get("order_no")]
    ))

    # ── Single CTE: relational balance (derived from payments FK, not stored status) ──
    rows = _q("""
        WITH inv AS (
            SELECT
                i.id::text                      AS id,
                i.invoice_no                    AS doc_no,
                'INVOICE'                       AS doc_type,
                i.invoice_date                  AS doc_date,
                COALESCE(i.grand_total,0)       AS grand_total,
                i.challan_id::text              AS challan_id,
                -- amount_paid: sum of payments.invoice_id FK (relational truth)
                COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.invoice_id = i.id
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0) AS amount_paid,
                -- balance_due: derived, never trust stored value
                GREATEST(COALESCE(i.grand_total,0) - COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.invoice_id = i.id
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0), 0) AS balance_due,
                -- payment_status: derived from payments JOIN, not stored field
                CASE
                    WHEN GREATEST(COALESCE(i.grand_total,0) - COALESCE((
                        SELECT SUM(p.amount) FROM payments p
                        WHERE p.invoice_id = i.id
                          AND NOT COALESCE(p.is_deleted,FALSE)
                    ), 0), 0) <= 0.01 THEN 'PAID'
                    WHEN COALESCE((
                        SELECT SUM(p.amount) FROM payments p
                        WHERE p.invoice_id = i.id
                          AND NOT COALESCE(p.is_deleted,FALSE)
                    ), 0) > 0 THEN 'PARTIAL'
                    ELSE 'UNPAID'
                END AS payment_status
            FROM invoices i
            WHERE i.party_id::text = %s
              AND COALESCE(i.is_deleted,FALSE) = FALSE
        ),
        chl AS (
            SELECT
                c.id::text                          AS id,
                c.challan_no                        AS doc_no,
                'CHALLAN'                           AS doc_type,
                c.challan_date                      AS doc_date,
                COALESCE(c.grand_total,c.total_amount,0) AS grand_total,
                NULL::text                          AS challan_id,
                -- amount_paid: from payments.challan_id FK (relational truth)
                COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.challan_id = c.id
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0) AS amount_paid,
                GREATEST(COALESCE(c.grand_total,c.total_amount,0) - COALESCE((
                    SELECT SUM(p.amount) FROM payments p
                    WHERE p.challan_id = c.id
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ), 0), 0) AS balance_due,
                -- INVOICED status: derived from invoices.challan_id FK (not stored status)
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM invoices inv
                        WHERE inv.challan_id = c.id
                          AND COALESCE(inv.is_deleted,FALSE) = FALSE
                    ) THEN 'INVOICED'
                    ELSE COALESCE(c.status,'PENDING')
                END AS payment_status
            FROM challans c
            WHERE c.party_id::text = %s
              AND COALESCE(c.is_deleted,FALSE) = FALSE
              AND UPPER(COALESCE(c.status,'PENDING')) NOT IN ('PAID','CANCELLED')
        )
        SELECT * FROM inv WHERE payment_status != 'PAID'
        UNION ALL
        SELECT * FROM chl
        ORDER BY doc_date DESC
    """, (pid, pid))

    invoices = [r for r in rows if r.get("doc_type") == "INVOICE"]
    challans = [r for r in rows if r.get("doc_type") == "CHALLAN"]

    # Extra invoices via order_ids (patient invoices with NULL party_id)
    if all_refs:
        try:
            from modules.sql_adapter import run_query as _rqp
            extra = _rqp("""
                SELECT i.id::text, i.invoice_no AS doc_no, 'INVOICE' AS doc_type,
                       i.invoice_date AS doc_date,
                       COALESCE(i.grand_total,0) AS grand_total,
                       i.challan_id::text AS challan_id,
                       -- relational amount_paid
                       COALESCE((SELECT SUM(p.amount) FROM payments p
                                 WHERE p.invoice_id = i.id
                                   AND NOT COALESCE(p.is_deleted,FALSE)), 0) AS amount_paid,
                       -- relational balance_due
                       GREATEST(COALESCE(i.grand_total,0) - COALESCE(
                           (SELECT SUM(p.amount) FROM payments p
                            WHERE p.invoice_id = i.id
                              AND NOT COALESCE(p.is_deleted,FALSE)), 0
                       ), 0) AS balance_due,
                       -- relational payment_status
                       CASE WHEN GREATEST(COALESCE(i.grand_total,0) - COALESCE(
                           (SELECT SUM(p.amount) FROM payments p
                            WHERE p.invoice_id = i.id
                              AND NOT COALESCE(p.is_deleted,FALSE)), 0
                       ), 0) <= 0.01 THEN 'PAID' ELSE 'UNPAID' END AS payment_status
                FROM invoices i
                WHERE EXISTS (
                    SELECT 1 FROM unnest(i.order_ids) AS oid WHERE oid = ANY(%s)
                )
                  AND COALESCE(i.is_deleted,FALSE) = FALSE
            """, (all_refs,)) or []
            extra = [r for r in extra if r.get("payment_status") != "PAID"]
            seen = {r["id"] for r in invoices}
            invoices += [r for r in extra if r["id"] not in seen]
        except Exception as _ie:
            pass

    # Orders with balance (not yet invoiced)
    orders = _q("""
        SELECT
            o.id::text              AS id,
            o.order_no              AS doc_no,
            'ON_ACCOUNT'            AS doc_type,
            o.created_at::date      AS doc_date,
            COALESCE(o.total_value,0) AS grand_total,
            NULL::text              AS challan_id,
            COALESCE(SUM(p.amount),0) AS amount_paid,
            GREATEST(COALESCE(o.total_value,0)-COALESCE(SUM(p.amount),0),0) AS balance_due,
            o.status                AS payment_status
        FROM orders o
        LEFT JOIN payments p ON p.advance_for_order_id=o.id
                             AND NOT COALESCE(p.is_deleted,FALSE)
        WHERE (o.party_id::text = %s OR o.party_name ILIKE %s OR o.patient_name ILIKE %s)
          AND COALESCE(o.is_deleted,FALSE)=FALSE
          AND o.order_type IN ('RETAIL','WHOLESALE')
          AND o.status NOT IN ('PAID','CANCELLED','BILLED','CLOSED')
        GROUP BY o.id, o.order_no, o.total_value, o.created_at, o.status
        HAVING COALESCE(o.total_value,0)-COALESCE(SUM(p.amount),0) > 0.50
        ORDER BY o.created_at DESC LIMIT 20
    """, (pid, pn, pn))

    # Build covered-challan map
    covered_challan_ids = set()
    for inv in invoices:
        if inv.get("challan_id"):
            covered_challan_ids.add(inv["challan_id"])

    for ch in challans:
        ch["_covered_by_invoice"] = ch["id"] in covered_challan_ids

    all_docs  = invoices + challans + orders
    total     = round(sum(float(d.get("balance_due") or 0) for d in all_docs), 2)
    net_total = round(sum(
        float(d.get("balance_due") or 0)
        for d in all_docs if not d.get("_covered_by_invoice")
    ), 2)

    return {
        "docs": all_docs, "invoices": invoices, "challans": challans,
        "orders": orders, "covered_challan_ids": covered_challan_ids,
        "total": total, "net_total": net_total,
    }


def _search(term: str, ptype: str = "All") -> List[Dict]:
    t = (term or "").strip()
    if not t:
        return []
    s = "%" + t + "%"

    # Wholesale = not retail. Use non-blank coalesce so NULL type doesn't become retail
    if ptype == "Wholesale":
        tc = "AND UPPER(COALESCE(party_type,'WHOLESALE')) NOT IN ('RETAIL','DOCTOR') "
    elif ptype == "Retail":
        tc = "AND UPPER(COALESCE(party_type,'RETAIL')) IN ('RETAIL','DOCTOR','') "
    else:
        tc = ""

    rows = []
    try:
        rows = _q("""
            SELECT id::text, party_name,
                   COALESCE(mobile,'')     AS mobile,
                   COALESCE(city,'')       AS city,
                   COALESCE(party_type,'') AS party_type,
                   COALESCE(gstin,'')      AS gstin,
                   'PARTY'                 AS record_type
            FROM parties
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND (party_name ILIKE %s
                   OR COALESCE(mobile,'') ILIKE %s
                   OR COALESCE(city,'')   ILIKE %s)
            {}ORDER BY party_name LIMIT 25
        """.format(tc), (s, s, s))
    except Exception:
        try:
            rows = _q("""
                SELECT id::text, party_name,
                       COALESCE(mobile,'') AS mobile, '' AS city,
                       COALESCE(party_type,'') AS party_type,
                       '' AS gstin, 'PARTY' AS record_type
                FROM parties
                WHERE COALESCE(is_active,TRUE)=TRUE AND party_name ILIKE %s
                {}ORDER BY party_name LIMIT 25
            """.format(tc), (s,))
        except Exception:
            pass

    if ptype in ("Retail", "All"):
        try:
            pts = _q("""
                SELECT id::text, COALESCE(master_name,'') AS party_name,
                       COALESCE(mobile,'') AS mobile, '' AS city,
                       'RETAIL' AS party_type, '' AS gstin, 'PATIENT' AS record_type
                FROM patients
                WHERE COALESCE(master_name,'') ILIKE %s OR COALESCE(mobile,'') ILIKE %s
                ORDER BY master_name LIMIT 10
            """, (s, s))
            seen = {r["id"] for r in rows}
            rows += [r for r in pts if r["id"] not in seen]
        except Exception:
            pass

    try:
        ords = _q("""
            SELECT DISTINCT COALESCE(party_id::text,'') AS id,
                   COALESCE(party_name, patient_name,'') AS party_name,
                   '' AS mobile, '' AS city, order_type AS party_type,
                   '' AS gstin, 'ORDER' AS record_type
            FROM orders
            WHERE order_no::text ILIKE %s AND COALESCE(is_deleted,FALSE)=FALSE
            LIMIT 5
        """, (s,))
        seen = {r["id"] for r in rows}
        rows += [r for r in ords if r.get("id") and r["id"] not in seen]
    except Exception:
        pass

    return rows



# ══════════════════════════════════════════════════════════════════════════════
# SMART ALLOCATION — SAP-style waterfall
# ══════════════════════════════════════════════════════════════════════════════

def _allocate(selected_docs: List[Dict], payment_amount: float,
              discount: float = 0.0) -> List[Dict]:
    """
    SAP-style allocation:
      1. Sort by date ascending (oldest first)
      2. If challan is covered by a selected invoice, skip (balance = 0 effectively)
      3. Fill each doc's balance_due from the payment pool
      4. Stop when money runs out
      5. Return list of allocation entries with allocated_amount per doc

    Returns:
        List of dicts: {doc, allocated_amount, cleared: bool}
    """
    # Build set of selected invoice's challan_ids
    sel_invoice_challan_ids = set()
    for d in selected_docs:
        if d.get("doc_type") == "INVOICE" and d.get("challan_id"):
            sel_invoice_challan_ids.add(d["challan_id"])

    # Sort: oldest doc first
    sorted_docs = sorted(
        selected_docs,
        key=lambda d: str(d.get("doc_date") or "9999-12-31")
    )

    pool = round(float(payment_amount), 2)
    disc_pool = round(float(discount or 0), 2)
    allocations = []

    for doc in sorted_docs:
        bal = round(float(doc.get("balance_due") or 0), 2)
        if bal <= 0:
            continue

        # Challan covered by a selected invoice → skip (already inside invoice total)
        if doc.get("_covered_by_invoice") and doc["id"] in sel_invoice_challan_ids:
            allocations.append({
                "doc": doc,
                "allocated_amount": 0.0,
                "allocated_discount": 0.0,
                "cleared": False,
                "skipped": True,
                "skip_reason": "Included in invoice",
            })
            continue

        if pool <= 0 and disc_pool <= 0:
            # No money left — show as pending
            allocations.append({
                "doc": doc,
                "allocated_amount": 0.0,
                "allocated_discount": 0.0,
                "cleared": False,
                "skipped": False,
            })
            continue

        # Apply discount first (on oldest doc)
        disc_applied = 0.0
        if disc_pool > 0:
            disc_applied = min(disc_pool, bal)
            disc_pool = round(disc_pool - disc_applied, 2)
            bal = round(bal - disc_applied, 2)

        # Apply payment
        pay_applied = min(pool, bal)
        pool = round(pool - pay_applied, 2)
        bal_after = round(bal - pay_applied, 2)

        allocations.append({
            "doc": doc,
            "allocated_amount": pay_applied,
            "allocated_discount": disc_applied,
            "cleared": bal_after <= 0.01,
            "skipped": False,
        })

    return allocations, round(pool, 2)  # (allocations, excess)


# ══════════════════════════════════════════════════════════════════════════════
# RECORD PAYMENT
# ══════════════════════════════════════════════════════════════════════════════

def _record_allocation(party_id: str, party_name: str,
                       allocations: List[Dict], excess: float,
                       mode: str, ref_no: str, narration: str,
                       pay_date: datetime.date) -> Dict:
    """
    Write one payment record per allocated doc + one excess On Account if needed.
    """
    pno = _gen_pno()
    steps = []
    saved_pnos = []

    for alloc in allocations:
        if alloc.get("skipped"):
            continue
        amt = alloc["allocated_amount"]
        disc = alloc["allocated_discount"]
        if amt <= 0 and disc <= 0:
            continue

        doc = alloc["doc"]
        dtype = doc.get("doc_type")
        did = doc.get("id")
        dno = doc.get("doc_no", "")
        pid_rec = str(uuid.uuid4())

        inv_id = did if dtype == "INVOICE"    else None
        chl_id = did if dtype == "CHALLAN"    else None
        ord_id = did if dtype == "ON_ACCOUNT" else None

        steps.append((_PAYMENTS_INSERT, {
            "id": pid_rec, "pno": pno if not saved_pnos else pno + "-" + str(len(saved_pnos) + 1),
            "pid": party_id or None, "pn": party_name,
            "iid": inv_id, "cid": chl_id, "oid": ord_id,
            "dt": pay_date, "mode": mode,
            "amt": amt, "ref": ref_no or None,
            "nar": narration or "Payment received",
            "by": st.session_state.get("user_name", "Staff"),
        }))
        steps.append((_LEDGER_INSERT, {
            "pid": party_id or None, "pn": party_name,
            "dt": pay_date, "rid": pid_rec,
            "rno": pno, "amt": amt,
            "nar": "{} — {}".format(mode, ref_no or dno or narration or "Payment"),
            "by":  st.session_state.get("user_name", "Staff"),
        }))

        if disc > 0:
            steps.append((_LEDGER_DISC_INSERT, {
                "pid": party_id or None, "pn": party_name,
                "dt": pay_date, "rid": str(uuid.uuid4()),
                "rno": pno + "-DISC", "amt": disc,
                "nar": "Discount on {}".format(dno or pno),
            }))

        if inv_id:
            steps.append((_INV_UPDATE, {"a": amt, "d": disc, "id": inv_id}))
        if chl_id:
            steps.append((_CHAL_UPDATE, {"a": amt + disc, "id": chl_id}))

        saved_pnos.append(dno)

    # Excess → On Account
    if excess > 0.01:
        exc_id = str(uuid.uuid4())
        exc_pno = pno + "-OA"
        steps.append((_PAYMENTS_INSERT, {
            "id": exc_id, "pno": exc_pno,
            "pid": party_id or None, "pn": party_name,
            "iid": None, "cid": None, "oid": None,
            "dt": pay_date, "mode": mode,
            "amt": excess, "ref": ref_no or None,
            "nar": "Excess credit — On Account",
            "by": st.session_state.get("user_name", "Staff"),
        }))
        steps.append((_LEDGER_INSERT, {
            "pid": party_id or None, "pn": party_name,
            "dt": pay_date, "rid": exc_id,
            "rno": exc_pno, "amt": excess,
            "nar": "Excess credit — On Account ({})".format(mode),
        }))
        saved_pnos.append("ON-ACCOUNT-EXCESS")

    ok, err = _tx(steps)
    if not ok:
        return {"error": err}

    # ── Auto-post accounting JV for each payment ──────────────────────
    try:
        from modules.accounting.accounts_engine import post_payment_receipt_jv
        import datetime as _dt
        post_payment_receipt_jv(
            payment_no   = pno,
            payment_id   = str(uuid.uuid4()),   # transaction already saved
            party_name   = party_name or "",
            amount       = float(pay_amount or 0),
            payment_mode = mode or "CASH",
            bank_account = "",
            voucher_date = pay_date if isinstance(pay_date, _dt.date)
                           else _dt.date.today(),
            created_by   = created_by or "Staff",
        )
    except Exception as _jve:
        import logging; logging.getLogger(__name__).warning(f"[JV] receipt: {_jve}")

    return {
        "pno": pno,
        "party_name": party_name,
        "mode": mode,
        "ref_no": ref_no or "",
        "narration": narration or "",
        "date": _fd(pay_date),
        "allocations": allocations,
        "excess": excess,
        "cleared_docs": [
            a["doc"]["doc_no"]
            for a in allocations
            if a.get("cleared") and not a.get("skipped")
        ],
    }


# ── SQL templates ─────────────────────────────────────────────────────────

_PAYMENTS_INSERT = """
    INSERT INTO payments
        (id, payment_no, party_id, party_name,
         invoice_id, challan_id, order_id,
         payment_date, payment_mode, amount,
         reference_no, remarks, payment_type,
         is_advance, advance_for_order_id, created_by)
    VALUES
        (%(id)s, %(pno)s, %(pid)s, %(pn)s,
         %(iid)s, %(cid)s, %(oid)s,
         %(dt)s, %(mode)s, %(amt)s,
         %(ref)s, %(nar)s, 'PAYMENT',
         FALSE, %(oid)s, %(by)s)
"""

_LEDGER_INSERT = """
    INSERT INTO party_ledger
        (party_id, party_name, entry_date, entry_type, ref_id, ref_no, credit, narration, created_by)
    VALUES
        (%(pid)s, %(pn)s, %(dt)s, 'PAYMENT', %(rid)s, %(rno)s, %(amt)s, %(nar)s, %(by)s)
"""

_LEDGER_DISC_INSERT = """
    INSERT INTO party_ledger
        (party_id, party_name, entry_date, entry_type, ref_id, ref_no, credit, narration)
    VALUES
        (%(pid)s, %(pn)s, %(dt)s, 'DISCOUNT', %(rid)s, %(rno)s, %(amt)s, %(nar)s)
"""

_INV_UPDATE = """
    UPDATE invoices
    SET
        -- Keep denormalized columns in sync for legacy queries/reports
        amount_paid    = COALESCE((
            SELECT SUM(p.amount) FROM payments p
            WHERE p.invoice_id = %(id)s
              AND NOT COALESCE(p.is_deleted,FALSE)
        ), 0),
        balance_due    = GREATEST(COALESCE(grand_total,0) - COALESCE((
            SELECT SUM(p.amount) FROM payments p
            WHERE p.invoice_id = %(id)s
              AND NOT COALESCE(p.is_deleted,FALSE)
        ), 0), 0),
        payment_status = CASE
            WHEN GREATEST(COALESCE(grand_total,0) - COALESCE((
                SELECT SUM(p.amount) FROM payments p
                WHERE p.invoice_id = %(id)s
                  AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0), 0) <= 0.01 THEN 'PAID'
            WHEN COALESCE((
                SELECT SUM(p.amount) FROM payments p
                WHERE p.invoice_id = %(id)s
                  AND NOT COALESCE(p.is_deleted,FALSE)
            ), 0) > 0 THEN 'PARTIAL'
            ELSE 'UNPAID'
        END,
        updated_at = NOW()
    WHERE id = %(id)s
"""

_CHAL_UPDATE = """
    UPDATE challans
    SET
        amount_paid = COALESCE((
            SELECT SUM(p.amount) FROM payments p
            WHERE p.challan_id = %(id)s
              AND NOT COALESCE(p.is_deleted,FALSE)
        ), 0),
        balance_due = GREATEST(COALESCE(grand_total,total_amount,0) - COALESCE((
            SELECT SUM(p.amount) FROM payments p
            WHERE p.challan_id = %(id)s
              AND NOT COALESCE(p.is_deleted,FALSE)
        ), 0), 0),
        -- Covered by invoice? Derive from invoices.challan_id FK
        status = CASE
            WHEN EXISTS (SELECT 1 FROM invoices inv
                         WHERE inv.challan_id = %(id)s
                           AND COALESCE(inv.is_deleted,FALSE)=FALSE)
            THEN 'INVOICED'
            ELSE status
        END,
        updated_at = NOW()
    WHERE id = %(id)s
"""


# ══════════════════════════════════════════════════════════════════════════════
# RECEIPT + WA HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _wa_link(mobile: str, msg: str) -> str:
    """Route via central wa_hub."""
    try:
        from modules.wa_hub import wa_link as _hub_link
        return _hub_link(mobile, msg)
    except Exception:
        c = "".join(x for x in (mobile or "") if x.isdigit())
        if len(c) == 10:
            c = "91" + c
        return "https://wa.me/{}?text={}".format(c, urllib.parse.quote(msg)) if c else ""


def _wa_receipt(rec: Dict, shop: Dict) -> str:
    nl = "\n"
    sn = shop.get("shop_name", "DV Optical")
    total_collected = sum(
        a["allocated_amount"]
        for a in rec.get("allocations", [])
        if not a.get("skipped")
    )
    m  = "Hello {} 👋".format(rec["party_name"]) + nl + nl
    m += "✅ *Payment Received*" + nl
    m += "🏪 " + sn + nl
    m += "📋 Receipt: *{}*".format(rec["pno"]) + nl
    m += "📅 Date: " + rec["date"] + nl
    m += "💳 Mode: " + rec["mode"] + nl
    m += "💰 Amount: *{}*".format(_fc(total_collected)) + nl
    if rec.get("ref_no"):
        m += "🔖 Ref: " + rec["ref_no"] + nl
    if rec.get("cleared_docs"):
        m += "🧾 Cleared: " + ", ".join(rec["cleared_docs"]) + nl
    if rec.get("excess", 0) > 0.01:
        m += "📌 Excess {} credited to account.".format(_fc(rec["excess"])) + nl
    m += nl + "Thank you! 🙏 " + sn
    return m


def _wa_with_balance(rec: Dict, shop: Dict, balance_after: float) -> str:
    nl = "\n"
    m  = _wa_receipt(rec, shop) + nl + nl
    m += "─────────────────" + nl
    if balance_after > 0.5:
        m += "⏳ *Outstanding Balance: {}*".format(_fc(balance_after)) + nl
        m += "Request you to kindly clear at earliest." + nl
    else:
        m += "✅ *Account fully settled! Thank you!*" + nl
    return m


def _receipt_html(rec: Dict, shop: Dict, bal_after: float) -> str:
    sn   = shop.get("shop_name", "DV Optical")
    addr = ", ".join(filter(None, [shop.get("shop_city", ""), shop.get("shop_state", "")]))
    total_paid = sum(
        a["allocated_amount"]
        for a in rec.get("allocations", [])
        if not a.get("skipped")
    )
    alloc_rows = ""
    for a in rec.get("allocations", []):
        if a.get("skipped") or a["allocated_amount"] <= 0:
            continue
        dno = a["doc"]["doc_no"]
        alloc_rows += "<tr><td>{}</td><td class=r>{}</td></tr>".format(
            dno, _fc(a["allocated_amount"])
        )
    bal_row = (
        "<tr><td style='color:#ef4444;font-weight:700'>Balance Due</td>"
        "<td class='r' style='color:#ef4444;font-weight:700'>{}</td></tr>".format(_fc(bal_after))
    ) if bal_after > 0.5 else (
        "<tr><td colspan=2 style='color:#10b981;font-weight:700;text-align:center'>"
        "✓ Account Settled</td></tr>"
    )
    css = (
        "<style>body{font-family:Arial,sans-serif;margin:0;background:#fff}"
        ".w{max-width:300px;margin:0 auto;padding:16px}"
        ".sn{font-size:14px;font-weight:800;text-align:center}"
        ".su{font-size:10px;color:#64748b;text-align:center;margin-bottom:6px}"
        ".pn{font-family:monospace;background:#0f172a;color:#34d399;padding:3px 8px;"
        "border-radius:4px;display:block;text-align:center;margin:6px 0}"
        "table{width:100%;border-collapse:collapse;font-size:12px}"
        "td{padding:3px 2px;border-bottom:1px solid #f1f5f9}"
        ".r{text-align:right}"
        ".am td{font-weight:800;border-top:2px solid #0f172a;border-bottom:2px solid #0f172a}"
        ".ft{font-size:9px;color:#94a3b8;text-align:center;"
        "margin-top:6px;border-top:1px solid #e2e8f0;padding-top:4px}"
        "@media print{@page{size:80mm auto;margin:3mm}}"
        "</style>"
    )
    body = (
        "<div class=w>"
        "<div class=sn>{sn}</div>"
        "<div class=su>{addr} {ph}</div>"
        "<span class=pn>{pno}</span>"
        "<table>"
        "<tr><td>Party</td><td class=r><b>{pn}</b></td></tr>"
        "<tr><td>Date</td><td class=r>{dt}</td></tr>"
        "<tr><td>Mode</td><td class=r>{mode}</td></tr>"
        "{alloc_rows}"
        "<tr class=am><td>Total Received</td><td class=r>{total}</td></tr>"
        "{bal_row}"
        "</table>"
        "<div class=ft>Thank you for your payment</div>"
        "</div>"
        "<script>window.onload=function(){{window.print();}}</script>"
    ).format(
        sn=sn, addr=addr, ph=shop.get("shop_phone", ""),
        pno=rec["pno"], pn=rec["party_name"],
        dt=rec["date"], mode=rec["mode"],
        alloc_rows=alloc_rows, total=_fc(total_paid),
        bal_row=bal_row,
    )
    return "<!DOCTYPE html><html><head><meta charset=utf-8>{css}</head><body>{body}</body></html>".format(
        css=css, body=body
    )


# ══════════════════════════════════════════════════════════════════════════════
# MONTHLY WA STATEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _ledger_monthly_wa(party_id: str, party_name: str,
                       mobile: str, shop: Dict) -> Tuple[str, float]:
    monthly = _q("""
        SELECT DATE_TRUNC('month', entry_date)::date AS month_start,
               SUM(credit) AS total_credit
        FROM party_ledger
        WHERE (party_id::text = %s
               OR (party_id IS NULL AND party_name ILIKE %s))
          AND entry_type IN ('PAYMENT', 'DISCOUNT')
          AND entry_date >= NOW() - INTERVAL '6 months'
        GROUP BY 1 ORDER BY 1 DESC LIMIT 6
    """, (party_id, "%" + party_name + "%"))

    ost_rows = _q("""
        SELECT
          COALESCE((SELECT SUM(GREATEST(grand_total - COALESCE(amount_paid,0), 0))
           FROM invoices WHERE party_id::text = %s
           AND COALESCE(is_deleted, FALSE) = FALSE
           AND UPPER(COALESCE(payment_status,'UNPAID')) != 'PAID'), 0)
          +
          COALESCE((SELECT SUM(GREATEST(grand_total - COALESCE(amount_paid,0), 0))
           FROM challans WHERE party_id::text = %s
           AND COALESCE(is_deleted, FALSE) = FALSE
           AND UPPER(COALESCE(status,'PENDING')) NOT IN ('PAID','CANCELLED')), 0)
          AS total_outstanding
    """, (party_id, party_id))

    outstanding_now = float((ost_rows[0]["total_outstanding"] if ost_rows else 0) or 0)
    nl = "\n"
    sn = shop.get("shop_name", "DV Optical")
    m  = "Hello {} 👋".format(party_name) + nl + nl
    m += "📊 *Account Statement — {}*".format(sn) + nl
    m += "─────────────────" + nl + nl

    months_order = list(reversed(monthly))
    if months_order:
        m += "💳 *Payments Received (Last 6 Months):*" + nl
        month_names = []
        for row in months_order:
            ms    = row["month_start"]
            mname = ms.strftime("%b %Y") if hasattr(ms, "strftime") else str(ms)[:7]
            amt   = float(row["total_credit"] or 0)
            month_names.append((mname, amt))
            m += "  • {}: {}".format(mname, _fc(amt)) + nl
        period_sum    = sum(a for _, a in month_names)
        oldest_month  = month_names[0][0] if month_names else ""
        m += nl + "💰 *Total Outstanding Now: {}*".format(_fc(outstanding_now)) + nl + nl
        pre_period_bal = round(outstanding_now - period_sum, 2)
        m += "📌 *Balance from before {} = {}*".format(oldest_month, _fc(pre_period_bal)) + nl
    else:
        m += "No payment records found in last 6 months." + nl
        m += "💰 *Current Outstanding: {}*".format(_fc(outstanding_now)) + nl

    m += nl + "For queries please contact us." + nl + "Thank you! 🙏 " + sn
    return m, outstanding_now


# ══════════════════════════════════════════════════════════════════════════════
# POST-PAYMENT PANEL
# ══════════════════════════════════════════════════════════════════════════════

def _can_reverse() -> bool:
    """Check if current user can reverse payments."""
    try:
        from modules.security.module_permissions import can
        return can("reverse_payment")
    except ImportError:
        return True  # fail open if module not deployed yet


def _post_payment_panel(result: Dict, mobile: str, shop: Dict,
                        bal_after: float, sk: str):
    if not result:
        return

    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown("<div class='pc-root'>", unsafe_allow_html=True)

    # ── Cleared summary ────────────────────────────────────────────────────
    cleared = result.get("cleared_docs", [])
    if cleared:
        items_html = "".join(
            "<div class='pc-cleared-item'>✓ <span>{}</span></div>".format(d)
            for d in cleared
        )
        excess_line = ""
        if result.get("excess", 0) > 0.01:
            excess_line = (
                "<div class='pc-cleared-item' style='color:#f59e0b'>"
                "→ Excess {} credited to On Account</div>"
            ).format(_fc(result["excess"]))
        st.markdown(
            "<div class='pc-cleared-card'>"
            "<div class='pc-cleared-title'>✅ Payment Recorded — {pno}</div>"
            "{items}{excess}"
            "</div>".format(
                pno=result["pno"], items=items_html, excess=excess_line
            ),
            unsafe_allow_html=True
        )

    # ── WA + Print buttons ──────────────────────────────────────────────
    msg_rcpt = _wa_receipt(result, shop)
    msg_bal  = _wa_with_balance(result, shop, bal_after)

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(
        "<a href='{url}' target='_blank' class='pc-action-btn pc-wa-rcpt' "
        "style='display:block;padding:8px 4px;border-radius:6px;font-size:.72rem;"
        "font-weight:700;text-decoration:none;text-align:center;background:#25d366;color:#fff'>"
        "📲 WA Receipt</a>".format(url=_wa_link(mobile, msg_rcpt)),
        unsafe_allow_html=True
    )
    c2.markdown(
        "<a href='{url}' target='_blank' style='display:block;padding:8px 4px;"
        "border-radius:6px;font-size:.72rem;font-weight:700;text-decoration:none;"
        "text-align:center;background:#128c7e;color:#fff'>"
        "📲 WA + Balance</a>".format(url=_wa_link(mobile, msg_bal)),
        unsafe_allow_html=True
    )

    # Editable custom WA
    wa_key = "pc_wa_cust_" + sk
    if wa_key not in st.session_state:
        st.session_state[wa_key] = msg_bal
    edited = c3.text_area("", key=wa_key, height=68, label_visibility="collapsed")
    c3.markdown(
        "<a href='{url}' target='_blank' style='display:block;padding:6px 4px;"
        "border-radius:6px;font-size:.72rem;font-weight:700;text-decoration:none;"
        "text-align:center;background:#075e54;color:#fff;margin-top:4px'>"
        "📲 WA Custom</a>".format(url=_wa_link(mobile, edited)),
        unsafe_allow_html=True
    )

    if c4.button("🖨️ Print Receipt", key="pc_print_" + sk, width='stretch'):
        import base64
        html = _receipt_html(result, shop, bal_after)
        b64  = base64.b64encode(html.encode()).decode()
        st.components.v1.html(
            "<script>var w=window.open('about:blank','_blank');"
            "w.document.write(atob('{}'));w.document.close();</script>".format(b64),
            height=0
        )

    # ── Cancel / Reverse payment ─────────────────────────────────────────
    with st.expander("⚠️ Cancel / Reverse This Payment", expanded=False):
        st.caption("Creates a compensating ledger entry. No data is deleted. Invoice balance is recalculated.")
        rev_reason = st.text_input(
            "Reversal reason *",
            key="pc_rev_rsn_" + sk,
            placeholder="e.g. Wrong amount, duplicate entry, customer cancelled…",
        )
        if st.button("🔄 Reverse Payment", type="secondary",
                     key="pc_rev_btn_" + sk, width='stretch'):
            if not rev_reason.strip():
                st.error("Please enter a reason before reversing.")
            else:
                try:
                    from modules.billing.services.reversal_service import reverse_payment
                    _pay_rows = _q("""
                        SELECT id::text FROM payments
                        WHERE payment_no = %s
                          AND NOT COALESCE(is_deleted, FALSE) LIMIT 1
                    """, (result.get("pno",""),))
                    if _pay_rows:
                        _ok, _msg = reverse_payment(
                            payment_id  = _pay_rows[0]["id"],
                            reversed_by = st.session_state.get("user_name", "Staff"),
                            reason      = rev_reason,
                        )
                        if _ok:
                            st.success(_msg)
                            for k in ["_pc_result_"+sk, "_pc_bal_after_"+sk,
                                      "_pc_party_"+sk, "_pc_mob_"+sk, wa_key]:
                                st.session_state.pop(k, None)
                            st.rerun()
                        else:
                            st.error(f"❌ {_msg}")
                    else:
                        st.error("Payment record not found in DB.")
                except Exception as _re:
                    st.error(f"Reversal error: {_re}")

    if st.button("✖ New Entry / Clear", key="pc_clear_" + sk,
                 width='stretch'):
        for k in ["_pc_result_" + sk, "_pc_bal_after_" + sk,
                  "_pc_party_" + sk, "_pc_mob_" + sk, wa_key]:
            st.session_state.pop(k, None)
        st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENT CHECKLIST RENDERER
# ══════════════════════════════════════════════════════════════════════════════

def _render_doc_checklist(docs: List[Dict], covered_ids: set,
                          sk: str) -> Tuple[List[Dict], float]:
    """
    Render the SAP-style document checklist.
    Returns (selected_docs, net_selected_balance).
    """
    if not docs:
        st.markdown(
            "<div class='pc-empty'><div class='pc-empty-icon'>🎉</div>"
            "No outstanding documents — account is clear!</div>",
            unsafe_allow_html=True
        )
        return [], 0.0

    # Section header
    st.markdown(
        "<div style='font-size:0.7rem;font-weight:700;color:#475569;"
        "text-transform:uppercase;letter-spacing:.1em;margin:16px 0 8px'>"
        "📋 Open Documents — Select to Clear"
        "</div>",
        unsafe_allow_html=True
    )

    # Table header
    header_html = """
    <table class="pc-doc-table" style="width:100%;border-collapse:collapse;
           font-size:0.76rem;background:#0d1929">
    <thead><tr style="background:#0a1120;color:#475569;font-size:0.62rem;
                      text-transform:uppercase;letter-spacing:.08em">
      <th style="padding:9px 12px;text-align:left;width:28px"></th>
      <th style="padding:9px 10px;text-align:left">Document</th>
      <th style="padding:9px 10px;text-align:left">Type</th>
      <th style="padding:9px 10px;text-align:left">Date</th>
      <th style="padding:9px 10px;text-align:right">Total</th>
      <th style="padding:9px 10px;text-align:right">Paid</th>
      <th style="padding:9px 10px;text-align:right">Balance</th>
      <th style="padding:9px 10px;text-align:left">Status</th>
    </tr></thead></table>
    """
    st.markdown(header_html, unsafe_allow_html=True)

    PILL = {
        "INVOICE":    "<span class='pill pill-inv'>INVOICE</span>",
        "CHALLAN":    "<span class='pill pill-chal'>CHALLAN</span>",
        "ON_ACCOUNT": "<span class='pill pill-oa'>ON ACCOUNT</span>",
    }

    selected_docs = []
    net_balance   = 0.0

    # Select-all checkbox
    all_key = "pc_chk_all_" + sk
    if st.checkbox("Select All", key=all_key, value=True):
        _select_all = True
    else:
        _select_all = False

    for idx, doc in enumerate(docs):
        doc_type = doc.get("doc_type", "")
        doc_no   = doc.get("doc_no", "—")
        bal      = float(doc.get("balance_due") or 0)
        paid     = float(doc.get("amount_paid") or 0)
        total    = float(doc.get("grand_total") or 0)
        covered  = doc.get("_covered_by_invoice", False)

        chk_key = "pc_chk_{}_{}".format(sk, idx)

        # Default: invoices + on-account checked; covered challans unchecked
        default_checked = (not covered) and _select_all

        col_chk, col_info = st.columns([0.5, 9.5])
        with col_chk:
            checked = st.checkbox(
                "",
                key=chk_key,
                value=st.session_state.get(chk_key, default_checked),
                label_visibility="collapsed",
                disabled=False,
            )

        with col_info:
            # Build row HTML
            pill_html = PILL.get(doc_type, "<span class='pill pill-oa'>{}</span>".format(doc_type))
            warn_badge = ""
            if covered:
                warn_badge = " <span class='pill pill-warn' style='font-size:0.55rem'>⚠ In Invoice</span>"

            row_style = "opacity:0.45;" if covered else ""
            st.markdown(
                "<div style='{row_style}display:flex;align-items:center;gap:12px;"
                "padding:7px 4px;border-bottom:1px solid #1e293b;font-size:0.76rem;"
                "color:#e2e8f0'>"
                "<b style='min-width:120px;color:#93c5fd'>{dno}</b>"
                "<span style='min-width:90px'>{pill}{warn}</span>"
                "<span style='min-width:80px;color:#64748b'>{dt}</span>"
                "<span style='min-width:80px;text-align:right;font-family:\"IBM Plex Mono\",monospace'>{gt}</span>"
                "<span style='min-width:80px;text-align:right;color:#10b981;"
                "font-family:\"IBM Plex Mono\",monospace'>{pd}</span>"
                "<span style='min-width:80px;text-align:right;color:#ef4444;font-weight:700;"
                "font-family:\"IBM Plex Mono\",monospace'>{bl}</span>"
                "</div>".format(
                    row_style=row_style,
                    dno=doc_no,
                    pill=pill_html, warn=warn_badge,
                    dt=_fd(doc.get("doc_date")),
                    gt=_fc(total), pd=_fc(paid), bl=_fc(bal),
                ),
                unsafe_allow_html=True
            )

        if checked:
            selected_docs.append(doc)
            if not covered:  # only count non-covered in net balance
                net_balance = round(net_balance + bal, 2)

    return selected_docs, net_balance


# ══════════════════════════════════════════════════════════════════════════════
# ALLOCATION PREVIEW TABLE
# ══════════════════════════════════════════════════════════════════════════════

def _render_allocation_preview(allocations: List[Dict], excess: float,
                                payment_amount: float, discount: float):
    if not allocations:
        return

    st.markdown(
        "<div style='font-size:0.7rem;font-weight:700;color:#475569;"
        "text-transform:uppercase;letter-spacing:.1em;margin:16px 0 6px'>"
        "🧮 Allocation Preview"
        "</div>",
        unsafe_allow_html=True
    )

    rows_html = ""
    for a in allocations:
        if a.get("skipped"):
            icon  = "⤷"
            color = "#334155"
            note  = a.get("skip_reason", "skipped")
            rows_html += (
                "<tr style='opacity:.5'>"
                "<td style='padding:7px 10px;color:#334155'>{icon} {dno}</td>"
                "<td style='padding:7px 10px'><span class='pill pill-chal'>{dt}</span></td>"
                "<td style='padding:7px 10px;color:#334155;font-size:.62rem'>{note}</td>"
                "<td style='padding:7px 10px;text-align:right;color:#334155'>"
                "<i>skipped</i></td>"
                "</tr>"
            ).format(
                icon=icon,
                dno=a["doc"]["doc_no"],
                dt=a["doc"].get("doc_type", ""),
                note=note,
            )
            continue

        amt  = a["allocated_amount"]
        disc = a["allocated_discount"]
        if amt <= 0 and disc <= 0:
            status_html = "<span class='pill' style='background:#1e293b;color:#475569;border:none'>PENDING</span>"
        elif a["cleared"]:
            status_html = "<span class='pill' style='background:#10b98122;color:#10b981;border:1px solid #10b98144'>✓ CLEARED</span>"
        else:
            status_html = "<span class='pill' style='background:#f59e0b22;color:#f59e0b;border:1px solid #f59e0b44'>PARTIAL</span>"

        PILL_MAP = {
            "INVOICE":    "pill-inv",
            "CHALLAN":    "pill-chal",
            "ON_ACCOUNT": "pill-oa",
        }
        pill_cls = PILL_MAP.get(a["doc"].get("doc_type", ""), "pill-oa")

        disc_cell = (
            " <span style='color:#10b981;font-size:.65rem'>(+disc {})</span>".format(_fc(disc))
            if disc > 0 else ""
        )
        rows_html += (
            "<tr>"
            "<td style='padding:7px 10px;color:#93c5fd;font-weight:600'>{dno}</td>"
            "<td style='padding:7px 10px'><span class='pill {pc}'>{dt}</span></td>"
            "<td style='padding:7px 10px;text-align:right;"
            "font-family:\"IBM Plex Mono\",monospace;color:#f1f5f9'>{amt}{disc}</td>"
            "<td style='padding:7px 10px'>{status}</td>"
            "</tr>"
        ).format(
            dno=a["doc"]["doc_no"],
            pc=pill_cls,
            dt=a["doc"].get("doc_type", ""),
            amt=_fc(amt), disc=disc_cell,
            status=status_html,
        )

    if excess > 0.01:
        rows_html += (
            "<tr style='background:#1a2a1a'>"
            "<td style='padding:7px 10px;color:#f59e0b'>On Account</td>"
            "<td style='padding:7px 10px'><span class='pill pill-warn'>EXCESS</span></td>"
            "<td style='padding:7px 10px;text-align:right;"
            "font-family:\"IBM Plex Mono\",monospace;color:#f59e0b'>{}</td>"
            "<td style='padding:7px 10px;color:#f59e0b;font-size:.7rem'>→ credited to account</td>"
            "</tr>"
        ).format(_fc(excess))

    st.markdown(
        "<table style='width:100%;border-collapse:collapse;font-size:.76rem;"
        "background:#0d1929;border:1px solid #1e3a5f;border-radius:8px;overflow:hidden'>"
        "<thead><tr style='background:#0a1120;color:#475569;font-size:.62rem;"
        "text-transform:uppercase;letter-spacing:.08em'>"
        "<th style='padding:8px 10px;text-align:left'>Document</th>"
        "<th style='padding:8px 10px;text-align:left'>Type</th>"
        "<th style='padding:8px 10px;text-align:right'>Allocated</th>"
        "<th style='padding:8px 10px;text-align:left'>Status</th>"
        "</tr></thead><tbody>{rows}</tbody></table>".format(rows=rows_html),
        unsafe_allow_html=True
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

def _panel(shop: Dict, ptype: str):
    sk = ptype.replace(" ", "_")
    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown("<div class='pc-root'>", unsafe_allow_html=True)

    # ── If payment already recorded this session → show post-payment ───────
    result = st.session_state.get("_pc_result_" + sk)
    if result:
        party  = st.session_state.get("_pc_party_" + sk, {})
        mobile = st.session_state.get("_pc_mob_" + sk, "")
        bal    = st.session_state.get("_pc_bal_after_" + sk, 0.0)
        _post_payment_panel(result, mobile, shop, bal, sk)
        # Ledger expander
        pid   = party.get("id", "")
        pname = party.get("party_name", "")
        _ledger_section(pid, pname, mobile, shop, sk)
        st.markdown("</div>", unsafe_allow_html=True)
        return

    # ── Search ─────────────────────────────────────────────────────────────
    party = st.session_state.get("_pc_party_" + sk)

    if not party:
        # ── Barcode / card scan (instant resolve) ─────────────────────────
        sc1, sc2 = st.columns([4, 1])
        with sc1:
            scanned = st.text_input(
                "📷 Scan party card / barcode",
                key="pc_scan_" + sk,
                placeholder="Scan barcode or customer number…",
                label_visibility="collapsed",
            )
        with sc2:
            if st.button("✕", key="pc_scan_clr_" + sk, width='stretch'):
                st.session_state.pop("pc_scan_" + sk, None)
                st.rerun()

        if scanned and scanned.strip():
            _scan = scanned.strip().upper()
            _hit = _q("""
                SELECT id::text, party_name,
                       COALESCE(mobile,'') AS mobile,
                       COALESCE(city,'')   AS city,
                       COALESCE(party_type,'') AS party_type,
                       COALESCE(gstin,'')  AS gstin
                FROM parties
                WHERE UPPER(TRIM(COALESCE(mobile,'')))     = %s
                   OR UPPER(TRIM(COALESCE(alt_mobile,''))) = %s
                   OR UPPER(TRIM(COALESCE(barcode,'')))    = %s
                LIMIT 1
            """, (_scan, _scan, _scan))
            if _hit:
                st.session_state["_pc_party_" + sk] = _hit[0]
                st.session_state.pop("pc_scan_" + sk, None)
                st.rerun()
            else:
                st.caption(f"⚠️ '{scanned}' not found — search below")

        st.markdown(
            "<div style='font-size:0.7rem;font-weight:700;color:#475569;"
            "text-transform:uppercase;letter-spacing:.08em;margin:10px 0 6px'>or Search</div>",
            unsafe_allow_html=True
        )

        # ── Search text → filtered selectbox ────────────────────────────
        search_by = st.radio(
            "Search by",
            ["Name", "Mobile"],
            horizontal=True,
            key="pc_search_by_" + sk,
        )

        # Load all parties/patients for this tab type (cached)
        all_parties = _load_all_parties()
        _supplier_types = {"SUPPLIER","VENDOR","LAB"}
        if ptype == "Wholesale":
            # Wholesale = all parties except suppliers and patients (same as bulk_order)
            candidates = [p for p in all_parties
                          if p.get("record_type") == "PARTY"
                          and str(p.get("party_type","")).upper()
                          not in _supplier_types]
        elif ptype == "Retail":
            # Retail = patients ONLY (same as retail_punching screen)
            candidates = [p for p in all_parties
                          if p.get("record_type") == "PATIENT"]
        else:
            candidates = all_parties

        total = len(candidates)
        label = "patients / parties" if ptype == "All" else                 "patients" if ptype == "Retail" else "parties"

        # Search box filters the dropdown — dropdown always visible
        _term_key = "pc_term_" + sk

        def _on_search_change():
            st.session_state[_term_key] = st.session_state.get("pc_srch_" + sk, "")

        st.text_input(
            f"🔍 Filter {label}",
            key="pc_srch_" + sk,
            placeholder=f"Type name or mobile to filter…",
            on_change=_on_search_change,
        )

        # Filter candidates — if no search term, show ALL in dropdown
        term = st.session_state.get(_term_key, "")
        if term and len(term.strip()) >= 1:
            t = term.strip().lower()
            if search_by == "Mobile":
                filtered = [p for p in candidates
                            if t in str(p.get("mobile",""))]
            else:
                filtered = [p for p in candidates
                            if t in str(p.get("party_name","")).lower()]
        else:
            filtered = candidates  # full list always visible

        def _lbl(p):
            mob = f"  ·  {p['mobile']}" if p.get("mobile") else ""
            pt  = f"  [{p['party_type']}]" if p.get("party_type") else ""
            return f"{p['party_name']}{mob}{pt}"

        labels = [_lbl(p) for p in filtered]
        shown  = len(filtered)
        placeholder = f"-- Select {label} ({shown} of {total}) --"

        chosen = st.selectbox(
            f"Select {label.title()}",
            [placeholder] + labels,
            key="pc_sel_" + sk,
        )

        if chosen and chosen != placeholder:
            sel = filtered[labels.index(chosen)]
            if sel.get("record_type") == "PARTY":
                pr = _q(
                    "SELECT id::text, party_name, "
                    "COALESCE(mobile,'') AS mobile, "
                    "COALESCE(city,'') AS city, "
                    "COALESCE(party_type,'') AS party_type, "
                    "COALESCE(gstin,'') AS gstin "
                    "FROM parties WHERE id=%s::uuid LIMIT 1",
                    (sel["id"],)
                )
                if pr: sel = pr[0]
            st.session_state["_pc_party_" + sk] = sel
            st.session_state.pop(_term_key, None)
            st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)
        return

    # ── Party selected ──────────────────────────────────────────────────────
    pid    = party.get("id", "")
    pname  = str(party.get("party_name", ""))
    mobile = str(party.get("mobile", ""))
    ptype_label = str(party.get("party_type", ptype))

    # Party banner
    initials = "".join(w[0].upper() for w in pname.split()[:2]) or "?"
    ba, bb = st.columns([6, 1])
    with ba:
        sub = "  ·  ".join(filter(None, [mobile, str(party.get("city", "")), party.get("gstin", "")]))
        st.markdown(
            "<div class='pc-party-banner'>"
            "<div class='pc-party-avatar'>{ini}</div>"
            "<div>"
            "<div class='pc-party-name'>{nm}"
            "<span class='pc-party-type'>{pt}</span></div>"
            "<div class='pc-party-meta'>{sub}</div>"
            "</div>"
            "</div>".format(ini=initials, nm=pname, pt=ptype_label, sub=sub),
            unsafe_allow_html=True
        )
    with bb:
        if st.button("✕ Change", key="pc_clr_" + sk, width='stretch'):
            for k in ["_pc_party_" + sk, "_pc_result_" + sk,
                      "_pc_bal_after_" + sk, "_pc_mob_" + sk]:
                st.session_state.pop(k, None)
            st.rerun()

    # ── Outstanding docs ────────────────────────────────────────────────────
    ost = _open_docs(pid)
    docs      = ost["docs"]
    net_total = ost["net_total"]
    covered   = ost["covered_challan_ids"]

    # Metrics
    st.markdown(
        "<div class='pc-metrics'>"
        "<div class='pc-metric'>"
        "<div class='pc-metric-label'>Net Outstanding</div>"
        "<div class='pc-metric-value' style='color:#ef4444'>{nt}</div>"
        "<div class='pc-metric-sub'>excl. covered challans</div>"
        "</div>"
        "<div class='pc-metric'>"
        "<div class='pc-metric-label'>Open Invoices</div>"
        "<div class='pc-metric-value' style='color:#10b981'>{ni}</div>"
        "</div>"
        "<div class='pc-metric'>"
        "<div class='pc-metric-label'>Open Challans</div>"
        "<div class='pc-metric-value' style='color:#3b82f6'>{nc}</div>"
        "</div>"
        "<div class='pc-metric'>"
        "<div class='pc-metric-label'>On Account</div>"
        "<div class='pc-metric-value' style='color:#8b5cf6'>{no}</div>"
        "</div>"
        "</div>".format(
            nt=_fc(net_total),
            ni=len(ost["invoices"]),
            nc=len(ost["challans"]),
            no=len(ost["orders"]),
        ),
        unsafe_allow_html=True
    )

    # ── Document checklist ──────────────────────────────────────────────────
    selected_docs, net_sel_balance = _render_doc_checklist(docs, covered, sk)

    st.markdown("<hr class='pc-divider'>", unsafe_allow_html=True)

    # ── Payment form ────────────────────────────────────────────────────────
    st.markdown(
        "<div style='font-size:0.7rem;font-weight:700;color:#475569;"
        "text-transform:uppercase;letter-spacing:.1em;margin:16px 0 10px'>"
        "💳 Payment Details"
        "</div>",
        unsafe_allow_html=True
    )

    MODES = ["CASH", "UPI", "NEFT", "RTGS", "CHEQUE", "CARD", "OTHER"]

    r1, r2, r3 = st.columns([1.2, 2, 1.5])
    pay_date = r1.date_input("Date", value=datetime.date.today(),
                             key="pc_dt_" + sk)
    amount   = r2.number_input(
        "Amount ₹",
        min_value=0.0, step=1.0,
        value=float(net_sel_balance),
        key="pc_amt_" + sk,
        help="Auto-filled from selected documents. Edit for partial payment.",
    )
    mode = r3.selectbox("Mode", MODES, key="pc_mode_" + sk)

    r4, r5, r6 = st.columns([2, 1.5, 1.5])
    ref_no    = r4.text_input("Ref / UTR", key="pc_ref_" + sk,
                               placeholder="UTR / Cheque no / optional")
    discount  = r5.number_input("Discount ₹", min_value=0.0, step=1.0,
                                 key="pc_disc_" + sk)
    narration = r6.text_input("Narration", value="Payment received",
                               key="pc_nar_" + sk)

    # ── Live allocation preview ─────────────────────────────────────────────
    if amount > 0 or discount > 0:
        allocations, excess = _allocate(selected_docs, amount, discount)
        _render_allocation_preview(allocations, excess, amount, discount)

        total_collected = round(amount + discount, 2)
        bal_after       = round(max(net_total - total_collected, 0), 2)
        cleared_count   = sum(1 for a in allocations if a.get("cleared"))

        # Summary metrics
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Collecting",        _fc(amount))
        s2.metric("Discount",          _fc(discount))
        s3.metric("Balance After",     _fc(bal_after),
                  delta="✅ Settled" if bal_after <= 0.5 else None)
        s4.metric("Docs Cleared",      "{} / {}".format(cleared_count, len(selected_docs)))

        if excess > 0.01:
            st.info("💡 {} excess will be credited to On Account.".format(_fc(excess)))
    else:
        allocations, excess = [], 0.0
        bal_after = net_total
        st.info("Enter amount above — allocation preview will appear here.")

    # ── Record button ────────────────────────────────────────────────────────
    st.markdown("<hr class='pc-divider'>", unsafe_allow_html=True)

    if not selected_docs:
        st.warning("Select at least one document above before recording.")
    elif amount <= 0 and discount <= 0:
        st.info("Enter a payment amount or discount to proceed.")
    else:
        if st.button("✅ Record Payment", type="primary",
                     key="pc_record_" + sk, width='stretch'):
            if not allocations:
                allocations, excess = _allocate(selected_docs, amount, discount)

            result = _record_allocation(
                party_id=pid,
                party_name=pname,
                allocations=allocations,
                excess=excess,
                mode=mode,
                ref_no=ref_no,
                narration=narration,
                pay_date=pay_date,
            )

            if "error" in result:
                st.error("❌ Save failed: " + str(result["error"]))
            else:
                st.session_state["_pc_result_" + sk]    = result
                st.session_state["_pc_bal_after_" + sk] = bal_after
                st.session_state["_pc_mob_" + sk]       = mobile
                st.success("✅ Payment recorded — {}".format(result["pno"]))
                st.rerun()

    # ── Ledger ─────────────────────────────────────────────────────────────
    _ledger_section(pid, pname, mobile, shop, sk)
    st.markdown("</div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# LEDGER + MONTHLY WA
# ══════════════════════════════════════════════════════════════════════════════

def _ledger_section(pid: str, pname: str, mobile: str, shop: Dict, sk: str):
    with st.expander("📒 Party Ledger  +  📲 Monthly Statement", expanded=False):
        st.markdown(
            "<div style='font-size:0.7rem;font-weight:700;color:#475569;"
            "text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px'>"
            "📲 WhatsApp Monthly Statement"
            "</div>",
            unsafe_allow_html=True
        )
        wa_msg, ost_now = _ledger_monthly_wa(pid, pname, mobile, shop)
        mob_in   = st.text_input("Mobile", value=mobile,
                                  key="pc_lmob_" + sk, placeholder="10-digit")
        stmt_key = "pc_stmt_" + sk
        if stmt_key not in st.session_state:
            st.session_state[stmt_key] = wa_msg
        edited_stmt = st.text_area("Statement (edit if needed)",
                                   key=stmt_key, height=200)
        st.markdown(
            "<a href='{url}' target='_blank' style='display:inline-block;"
            "background:#25d366;color:#fff;padding:8px 20px;border-radius:6px;"
            "font-weight:700;font-size:.82rem;text-decoration:none'>"
            "📲 Send Monthly Statement</a>".format(
                url=_wa_link(mob_in, edited_stmt)
            ),
            unsafe_allow_html=True
        )

        st.markdown("<hr class='pc-divider'>", unsafe_allow_html=True)
        st.markdown(
            "<div style='font-size:0.7rem;font-weight:700;color:#475569;"
            "text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px'>"
            "Ledger — Last 60 Entries"
            "</div>",
            unsafe_allow_html=True
        )
        rows = _q("""
            SELECT entry_date, entry_type, ref_no,
                   COALESCE(debit, 0)  AS debit,
                   COALESCE(credit, 0) AS credit,
                   COALESCE(narration,'') AS narration,
                   COALESCE(created_by,'') AS created_by
            FROM party_ledger
            WHERE party_id::text = %s
               OR (party_id IS NULL AND party_name ILIKE %s)
            ORDER BY entry_date ASC, id ASC LIMIT 120
        """, (pid, "%" + pname + "%"))

        if rows:
            run   = 0.0   # running balance: debit - credit = outstanding
            trows = ""
            TYPE_COLOR = {
                "INVOICE":  ("#ef4444", "DR"),
                "CHALLAN":  ("#f97316", "DR"),
                "PAYMENT":  ("#10b981", "CR"),
                "DISCOUNT": ("#f59e0b", "CR"),
            }
            for row in rows:
                dr    = float(row.get("debit")  or 0)
                cr    = float(row.get("credit") or 0)
                run   = round(run + dr - cr, 2)
                etype = str(row.get("entry_type", ""))
                ec, dc = TYPE_COLOR.get(etype, ("#64748b", ""))
                amt_html = (
                    f"<td style='padding:5px 8px;text-align:right;color:#ef4444;"
                    f"font-family:monospace'>{_fc(dr)}</td>"
                    f"<td style='padding:5px 8px;text-align:right;color:#10b981;"
                    f"font-family:monospace'>{_fc(cr) if cr else '—'}</td>"
                )
                bal_color = "#ef4444" if run > 0.01 else "#10b981"
                trows += (
                    "<tr style='border-bottom:1px solid #1e293b'>"
                    f"<td style='padding:5px 8px;color:#64748b;white-space:nowrap'>"
                    f"{str(row.get('entry_date',''))[:10]}</td>"
                    f"<td style='padding:5px 8px'>"
                    f"<span style='color:{ec};font-size:.6rem;font-weight:700;"
                    f"background:{ec}22;padding:1px 5px;border-radius:4px'>{etype}</span>"
                    f"&nbsp;<span style='color:{ec};font-size:.58rem'>{dc}</span></td>"
                    f"<td style='padding:5px 8px;color:#93c5fd;font-size:.72rem'>"
                    f"{str(row.get('ref_no','') or '')}</td>"
                    + amt_html +
                    f"<td style='padding:5px 8px;text-align:right;font-weight:700;"
                    f"color:{bal_color};font-family:monospace'>{_fc(run)}</td>"
                    f"<td style='padding:5px 8px;font-size:.6rem;color:#475569;"
                    f"max-width:160px;overflow:hidden;text-overflow:ellipsis'>"
                    f"{str(row.get('narration',''))[:50]}</td>"
                    "</tr>"
                )
            st.markdown(
                "<table style='width:100%;border-collapse:collapse;font-size:.72rem;"
                "background:#0d1929'>"
                "<thead><tr style='background:#0a1120;color:#475569;font-size:.6rem;"
                "text-transform:uppercase;letter-spacing:.06em'>"
                "<th style='padding:6px 8px'>Date</th>"
                "<th style='padding:6px 8px'>Type</th>"
                "<th style='padding:6px 8px'>Ref</th>"
                "<th style='padding:6px 8px;text-align:right'>Debit</th>"
                "<th style='padding:6px 8px;text-align:right'>Credit</th>"
                "<th style='padding:6px 8px;text-align:right'>Balance</th>"
                "<th style='padding:6px 8px'>Narration</th>"
                "</tr></thead><tbody>" + trows + "</tbody></table>",
                unsafe_allow_html=True
            )
        else:
            st.caption("No ledger entries yet.")


# ══════════════════════════════════════════════════════════════════════════════
# DISBURSEMENT (Payment OUT) — stub for supplier/expense payments
# ══════════════════════════════════════════════════════════════════════════════

def _render_disbursement():
    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown("<div class='pc-root'>", unsafe_allow_html=True)

    st.markdown(
        "<div style='font-size:0.7rem;font-weight:700;color:#475569;"
        "text-transform:uppercase;letter-spacing:.1em;margin-bottom:14px'>"
        "💸 Payment Disbursement — Pay Supplier / Expense"
        "</div>",
        unsafe_allow_html=True
    )

    MODES = ["CASH", "UPI", "NEFT", "RTGS", "CHEQUE", "CARD", "OTHER"]

    r1, r2 = st.columns(2)

    # ── Supplier dropdown (always visible, search filters it) ───────────────
    with r1:
        category_for_search = st.session_state.get("pd_cat", "SUPPLIER")

        # Load all suppliers/parties from cache
        all_parties = _load_all_parties()
        if category_for_search == "SUPPLIER":
            supplier_rows = [p for p in all_parties
                             if p.get("record_type") == "PARTY"
                             and "SUPPLIER" in str(p.get("party_type","")).upper()]
            # Fallback: if no SUPPLIER-tagged parties, show all non-patients
            if not supplier_rows:
                supplier_rows = [p for p in all_parties
                                 if p.get("record_type") == "PARTY"]
        else:
            supplier_rows = [p for p in all_parties
                             if p.get("record_type") == "PARTY"]

        total_s = len(supplier_rows)

        # Filter box — reduces dropdown, doesn't hide it
        def _on_supp_change():
            st.session_state["pd_supp_term"] = st.session_state.get("pd_supp_search", "")

        st.text_input(
            "🔍 Filter Payee / Supplier",
            key="pd_supp_search",
            placeholder="Type name or mobile to filter…",
            on_change=_on_supp_change,
        )

        supp_term = st.session_state.get("pd_supp_term", "")
        if supp_term.strip():
            t = supp_term.strip().lower()
            filtered_s = [p for p in supplier_rows
                          if t in str(p.get("party_name","")).lower()
                          or t in str(p.get("mobile",""))]
        else:
            filtered_s = supplier_rows

        def _slbl(p):
            mob = f"  ·  {p['mobile']}" if p.get("mobile") else ""
            pt  = f"  [{p['party_type']}]" if p.get("party_type") else ""
            return f"{p['party_name']}{mob}{pt}"

        s_labels     = [_slbl(p) for p in filtered_s]
        s_placeholder = f"-- Select Payee ({len(filtered_s)} of {total_s}) --"

        chosen = st.selectbox(
            "Select Payee",
            [s_placeholder] + s_labels,
            key="pd_supp_sel",
        )

        payee = ""
        if chosen and chosen != s_placeholder:
            sel_row = filtered_s[s_labels.index(chosen)]
            payee   = sel_row["party_name"]
            if sel_row.get("mobile"):
                st.caption(f"📞 {sel_row['mobile']}  ·  {sel_row.get('party_type','')}")
        elif supp_term.strip():
            # Allow free-text payee (expense/misc not in party master)
            payee = supp_term.strip()
            st.caption("⚠️ Not in party master — will save as free-text")

    category = r2.selectbox("Category",
                             ["SUPPLIER", "EXPENSE", "SALARY", "RENT",
                              "UTILITY", "OTHER"],
                             key="pd_cat")
    r3, r4, r5 = st.columns([1.2, 1.5, 1.5])
    pay_date = r3.date_input("Date", value=datetime.date.today(), key="pd_dt")
    amount   = r4.number_input("Amount ₹", min_value=0.0, step=1.0, key="pd_amt")
    mode     = r5.selectbox("Mode", MODES, key="pd_mode")

    r6, r7 = st.columns(2)
    ref_no    = r6.text_input("Ref / UTR / Cheque", key="pd_ref",
                               placeholder="optional")
    narration = r7.text_input("Narration", value="Payment made",
                               key="pd_nar")

    if amount > 0 and payee:
        if st.button("💸 Record Disbursement", type="primary",
                     key="pd_rec", width='stretch'):
            pno    = _gen_pno()
            pid_rec = str(uuid.uuid4())
            ok = _w("""
                INSERT INTO payments
                    (id, payment_no, party_name, payment_date, payment_mode,
                     amount, reference_no, remarks, payment_type, is_advance,
                     created_by)
                VALUES (%(id)s, %(pno)s, %(pn)s, %(dt)s, %(mode)s,
                        %(amt)s, %(ref)s, %(nar)s, 'DISBURSEMENT', FALSE, %(by)s)
            """, {
                "id": pid_rec, "pno": pno, "pn": payee,
                "dt": pay_date, "mode": mode, "amt": amount,
                "ref": ref_no or None,
                "nar": "{} — {} — {}".format(category, narration, payee),
                "by": st.session_state.get("user_name", "Staff"),
            })
            if ok:
                # ── Auto-post accounting JV ─────────────────────────
                try:
                    from modules.accounting.accounts_engine import post_disbursement_jv
                    import datetime as _dt
                    post_disbursement_jv(
                        payment_no   = pno,
                        payment_id   = pid_rec,
                        payee        = payee,
                        amount       = float(amount),
                        category     = category,
                        payment_mode = mode,
                        voucher_date = pay_date if isinstance(pay_date, _dt.date)
                                       else _dt.date.today(),
                        created_by   = st.session_state.get("user_name","Staff"),
                    )
                except Exception as _jve:
                    import logging; logging.getLogger(__name__).warning(f"[JV] disb: {_jve}")

                st.success("✅ Disbursement recorded — {}  |  {} — {}".format(
                    pno, payee, _fc(amount)
                ))
            else:
                st.error("❌ Failed to record disbursement.")
    else:
        st.info("Fill payee name and amount to proceed.")

    st.markdown("</div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def render_payment_collection():
    _ensure_tables()
    shop = _shop()

    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown(
        "<div style='font-family:\"Plus Jakarta Sans\",sans-serif;"
        "font-size:1.4rem;font-weight:700;color:#e2e8f0;margin-bottom:4px'>"
        "💳 Payment Centre"
        "</div>"
        "<div style='font-size:.76rem;color:#475569;margin-bottom:18px'>"
        "DV ERP — Receivables &amp; Disbursements"
        "</div>",
        unsafe_allow_html=True
    )

    # ── Top-level tabs ─────────────────────────────────────────────────────
    tab_r, tab_w, tab_a, tab_d = st.tabs([
        "🛍️  Retail",
        "📦  Wholesale",
        "🌐  All Parties",
        "💸  Disbursement",
    ])

    with tab_r:
        _panel(shop, "Retail")

    with tab_w:
        _panel(shop, "Wholesale")

    with tab_a:
        _panel(shop, "All")

    with tab_d:
        _render_disbursement()
