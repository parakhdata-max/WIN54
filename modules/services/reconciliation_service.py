"""
modules/services/reconciliation_service.py
============================================
Detect-only reconciliation — finds issues, never auto-fixes.
Results shown in System Health page.

Checks:
  1. Journal balance        — SUM(dr) must equal SUM(cr)
  2. Invoice vs ledger      — invoice total must match party_ledger debit
  3. Negative stock         — blank_inventory qty must never be < 0
  4. Orphan ledger entries  — party_ledger rows with no matching invoice/payment
  5. Unposted invoices      — invoices with no journal entry (backfill needed)
  6. Unposted payments      — payments with no journal entry (backfill needed)

All queries are read-only. Cache result for 5 minutes to avoid UI lag.
"""

from __future__ import annotations
import logging
import time
from typing import List, Dict, Tuple

_log = logging.getLogger(__name__)

# ── 5-minute cache ────────────────────────────────────────────────────────────
_cache: Dict = {"ts": 0, "result": None}
_CACHE_TTL   = 300   # seconds


def _q(sql: str, params=None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or ()) or []
    except Exception as e:
        _log.warning(f"[recon._q] {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# INDIVIDUAL CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def check_journal_balance() -> Dict:
    """
    Journal DR must equal CR.
    A non-zero difference means a broken JV was posted.
    """
    rows = _q("""
        SELECT
            ROUND(SUM(debit),  2) AS total_dr,
            ROUND(SUM(credit), 2) AS total_cr,
            ROUND(ABS(SUM(debit) - SUM(credit)), 2) AS diff
        FROM journal_lines
    """)
    if not rows:
        return {"status": "ok", "label": "Journal Balance",
                "detail": "No journal entries yet.", "diff": 0}

    r    = rows[0]
    dr   = float(r.get("total_dr")  or 0)
    cr   = float(r.get("total_cr")  or 0)
    diff = float(r.get("diff")      or 0)

    return {
        "status":  "ok" if diff < 0.01 else "error",
        "label":   "Journal Balance",
        "detail":  f"DR ₹{dr:,.2f}  CR ₹{cr:,.2f}  Diff ₹{diff:,.2f}",
        "diff":    diff,
        "total_dr": dr,
        "total_cr": cr,
    }


def check_invoice_ledger_mismatch() -> Dict:
    """
    Each invoice's grand_total should match the debit posted in party_ledger.
    Mismatch = backfill not run, or partial posting.
    """
    rows = _q("""
        SELECT
            i.invoice_no,
            ROUND(i.grand_total, 2)                  AS invoice_total,
            ROUND(COALESCE(pl.ledger_dr, 0), 2)      AS ledger_dr,
            ROUND(ABS(i.grand_total -
                  COALESCE(pl.ledger_dr, 0)), 2)      AS diff
        FROM invoices i
        LEFT JOIN (
            SELECT ref_no,
                   SUM(debit) AS ledger_dr
            FROM   party_ledger
            WHERE  entry_type = 'INVOICE'
            GROUP BY ref_no
        ) pl ON pl.ref_no = i.invoice_no
        WHERE COALESCE(i.is_deleted, FALSE) = FALSE
          AND UPPER(COALESCE(i.status,'')) != 'CANCELLED'
          AND ABS(i.grand_total - COALESCE(pl.ledger_dr, 0)) > 0.01
        ORDER BY diff DESC
        LIMIT 50
    """)

    return {
        "status":  "ok" if not rows else "warning",
        "label":   "Invoice vs Ledger",
        "detail":  f"{len(rows)} invoices with ledger mismatch"
                   if rows else "All invoices match ledger",
        "rows":    rows,
        "count":   len(rows),
    }


def check_negative_stock() -> Dict:
    """
    No blank inventory qty should be < 0.
    If it is, the FOR UPDATE lock failed or was bypassed.
    """
    rows = _q("""
        SELECT
            b.batch_no,
            b.material,
            b.coating,
            b.qty_right       AS "qty_right",
            b.qty_left        AS "qty_left",
            b.qty_independent AS "qty_independent"
        FROM blank_inventory b
        WHERE b.qty_right < 0
           OR b.qty_left  < 0
           OR b.qty_independent < 0
        ORDER BY b.batch_no
    """)

    return {
        "status":  "ok" if not rows else "error",
        "label":   "Negative Stock",
        "detail":  f"{len(rows)} blanks with negative quantity"
                   if rows else "All stock quantities ≥ 0",
        "rows":    rows,
        "count":   len(rows),
    }


def check_orphan_ledger_entries() -> Dict:
    """
    party_ledger entries referencing invoices or payments that no longer exist.
    Indicates a partial delete or DB corruption.
    """
    orphan_invoices = _q("""
        SELECT pl.id::text, pl.ref_no, pl.party_name,
               ROUND(pl.debit, 2) AS debit, pl.entry_date::text AS date
        FROM   party_ledger pl
        LEFT JOIN invoices i ON i.invoice_no = pl.ref_no
        WHERE  pl.entry_type = 'INVOICE'
          AND  i.id IS NULL
        LIMIT 20
    """)

    orphan_payments = _q("""
        SELECT pl.id::text, pl.ref_no, pl.party_name,
               ROUND(pl.credit, 2) AS credit, pl.entry_date::text AS date
        FROM   party_ledger pl
        LEFT JOIN payments p ON p.payment_no = pl.ref_no
        WHERE  pl.entry_type = 'PAYMENT'
          AND  p.id IS NULL
        LIMIT 20
    """)

    total = len(orphan_invoices) + len(orphan_payments)
    return {
        "status":          "ok" if total == 0 else "warning",
        "label":           "Orphan Ledger Entries",
        "detail":          f"{total} orphan entries found"
                           if total else "No orphan entries",
        "orphan_invoices": orphan_invoices,
        "orphan_payments": orphan_payments,
        "count":           total,
    }


def check_unposted_documents() -> Dict:
    """
    Invoices and payments that exist but have no journal entry.
    These need the Backfill to run.
    """
    # Unposted invoices
    unposted_inv = _q("""
        SELECT COUNT(*) AS n
        FROM   invoices i
        WHERE  COALESCE(i.is_deleted, FALSE) = FALSE
          AND  UPPER(COALESCE(i.status,'')) != 'CANCELLED'
          AND  NOT EXISTS (
              SELECT 1 FROM journal_entries j
              WHERE j.ref_doc_id = i.id::text
                AND j.ref_doc_type = 'INVOICE'
          )
    """)

    # Unposted payments
    unposted_pay = _q("""
        SELECT COUNT(*) AS n
        FROM   payments p
        WHERE  COALESCE(p.is_deleted, FALSE) = FALSE
          AND  p.payment_type IN ('PAYMENT','RECEIPT')
          AND  NOT EXISTS (
              SELECT 1 FROM journal_entries j
              WHERE j.ref_doc_id = p.id::text
                AND j.ref_doc_type IN ('PAYMENT','RECEIPT')
          )
    """)

    ni = int((unposted_inv[0].get("n") if unposted_inv else 0) or 0)
    np = int((unposted_pay[0].get("n") if unposted_pay else 0) or 0)
    total = ni + np

    return {
        "status":            "ok" if total == 0 else "warning",
        "label":             "Unposted Documents",
        "detail":            f"{ni} invoices, {np} payments not in journal"
                             if total else "All documents posted to journal",
        "unposted_invoices": ni,
        "unposted_payments": np,
        "count":             total,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RUN — all checks, cached
# ══════════════════════════════════════════════════════════════════════════════

def run_reconciliation(force: bool = False) -> Dict:
    """
    Run all checks. Returns cached result if < 5 minutes old.
    Set force=True to bypass cache.
    """
    global _cache

    if not force and _cache["result"] and (time.time() - _cache["ts"]) < _CACHE_TTL:
        return {**_cache["result"], "from_cache": True,
                "cache_age_s": int(time.time() - _cache["ts"])}

    results = []
    errors  = 0
    warnings = 0

    for fn in [
        check_journal_balance,
        check_invoice_ledger_mismatch,
        check_negative_stock,
        check_orphan_ledger_entries,
        check_unposted_documents,
    ]:
        try:
            r = fn()
            results.append(r)
            if r["status"] == "error":   errors   += 1
            if r["status"] == "warning": warnings += 1
        except Exception as e:
            results.append({
                "status": "error",
                "label":  fn.__name__,
                "detail": str(e),
                "count":  0,
            })
            errors += 1

    overall = (
        "error"   if errors   > 0 else
        "warning" if warnings > 0 else
        "ok"
    )

    result = {
        "overall":   overall,
        "errors":    errors,
        "warnings":  warnings,
        "checks":    results,
        "run_at":    time.strftime("%d %b %Y %H:%M:%S"),
        "from_cache": False,
        "cache_age_s": 0,
    }

    _cache = {"ts": time.time(), "result": result}
    return result
