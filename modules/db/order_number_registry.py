"""
modules/db/order_number_registry.py
=====================================
Gap-Free Transactional Order Numbering

WHY PostgreSQL SEQUENCE FAILS:
  nextval() is non-transactional by design (for performance).
  If the surrounding INSERT fails or the transaction rolls back,
  the sequence value is consumed and never returned → permanent gap.

THIS MODULE'S APPROACH:
  A table-based counter with SELECT ... FOR UPDATE.
  The counter row is locked for the duration of the transaction.
  If the transaction rolls back, the counter rollback too → ZERO GAPS.
  Because the lock is held at the DB row level, it serialises across
  ALL application servers — even if 10 Streamlit workers fire at once,
  only one proceeds at a time for number assignment.

TABLE SCHEMA (auto-created on first call):
  order_number_registry
    series        TEXT  PRIMARY KEY   -- 'RETAIL', 'WHOLESALE', 'CONSULTATION', 'PURCHASE'
    last_number   INT   NOT NULL      -- current highest assigned number
    prefix        TEXT                -- e.g. 'RET', 'WS', 'CONS', 'PO'
    fiscal_year   TEXT                -- '2627' compact 4-digit format (e.g. FY 2026-27)
    updated_at    TIMESTAMPTZ

USAGE (inside a transaction cursor):
    from modules.db.order_number_registry import next_order_number, ensure_registry

    # Call inside your existing transaction — BEFORE the INSERT
    cursor.execute("BEGIN")  # already in your transaction
    seq_no, display_no = next_order_number(cursor, series="RETAIL")
    # Now insert your order using display_no
    # Only commits when your outer transaction commits

CONCURRENT SAFETY:
    Two servers hitting next_order_number("RETAIL") simultaneously:
      Server A: SELECT ... FOR UPDATE → gets lock, reads 207, writes 208
      Server B: SELECT ... FOR UPDATE → WAITS until A commits
      Server B then: reads 208, writes 209
    Result: 208, 209 — perfectly sequential, zero gap.

CONSULTATION SEPARATION:
    Consultations use series="CONSULTATION" → own counter.
    They never consume numbers from the RETAIL / WHOLESALE series.
    Backoffice list shows RETAIL+WHOLESALE numbers gap-free.
"""

from __future__ import annotations
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SERIES CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# ALL DOCUMENT SERIES — one table, all documents
# ─────────────────────────────────────────────────────────────────────────────
# Format: prefix/FY/NNNN  e.g. CH/2526/0042  INV/2526/0042  CN/2526/0007
# FY = fiscal year suffix  FY 2026-27 → '2627'  (compact 4-digit, e.g. CH/2627/0014)
# NNNN = zero-padded sequential number
# ─────────────────────────────────────────────────────────────────────────────

SERIES_CONFIG = {
    # ── Sales / Billing ───────────────────────────────────────────────────────
    "RETAIL":           {"prefix": "R",    "start": 1, "pad": 4},
    "WHOLESALE":        {"prefix": "W",    "start": 1, "pad": 4},
    "CONSULTATION":     {"prefix": "CONS", "start": 1, "pad": 4},
    "CHALLAN":          {"prefix": "CH",   "start": 1, "pad": 4},
    "INVOICE":          {"prefix": "INV",  "start": 1, "pad": 4},
    "CREDIT_NOTE":      {"prefix": "CN",   "start": 1, "pad": 4},
    "DEBIT_NOTE":       {"prefix": "DN",   "start": 1, "pad": 4},
    "PAYMENT":          {"prefix": "PAY",  "start": 1, "pad": 4},
    # ── Purchase / Procurement ────────────────────────────────────────────────
    "PURCHASE_ORDER":   {"prefix": "PO",   "start": 1, "pad": 4},
    "PURCHASE_INVOICE": {"prefix": "PINV", "start": 1, "pad": 4},
    "PURCHASE_RETURN":  {"prefix": "PR",   "start": 1, "pad": 4},
    # ── Accounts ─────────────────────────────────────────────────────────────
    "JOURNAL":          {"prefix": "JV",   "start": 1, "pad": 4},
    "RECEIPT":          {"prefix": "REC",  "start": 1, "pad": 4},
    # ── Other ────────────────────────────────────────────────────────────────
    "RETURN":           {"prefix": "RET",  "start": 1, "pad": 4},
    "ADVANCE":          {"prefix": "ADV",  "start": 1, "pad": 4},
}

# Unified series: RETAIL + WHOLESALE share one counter
UNIFIED_SERIES = True


def _sp(cursor, name, sql, *args):
    """
    Execute SQL inside a named SAVEPOINT so a failure doesn't abort
    the calling transaction. Silently rolls back the savepoint on error.
    Returns True on success, False on failure.
    """
    try:
        cursor.execute(f"SAVEPOINT {name}")
        if args:
            cursor.execute(sql, *args)
        else:
            cursor.execute(sql)
        cursor.execute(f"RELEASE SAVEPOINT {name}")
        return True
    except Exception:
        try: cursor.execute(f"ROLLBACK TO SAVEPOINT {name}")
        except Exception: pass
        return False


def ensure_registry(cursor) -> bool:
    """
    Idempotent — creates order_number_registry table and seeds rows.
    Every DDL statement runs inside its own SAVEPOINT so a failure
    (e.g. column/constraint already exists) rolls back only that DDL,
    never the outer transaction. This is critical when called from
    inside an order-save transaction.
    """
    try:
        # Main registry table — SAVEPOINT in case table already exists with
        # different constraints (rare, but possible on upgrade)
        _sp(cursor, "_er_tbl", """
            CREATE TABLE IF NOT EXISTS order_number_registry (
                series        TEXT         PRIMARY KEY,
                last_number   INTEGER      NOT NULL DEFAULT 0,
                prefix        TEXT         NOT NULL DEFAULT '',
                fiscal_year   TEXT         NOT NULL DEFAULT '',
                fy_start      INTEGER,
                fy_end        INTEGER,
                updated_at    TIMESTAMPTZ  DEFAULT NOW()
            )
        """)
        # Additive column migrations
        _sp(cursor, "_er_fys", "ALTER TABLE order_number_registry ADD COLUMN IF NOT EXISTS fy_start INTEGER")
        _sp(cursor, "_er_fye", "ALTER TABLE order_number_registry ADD COLUMN IF NOT EXISTS fy_end INTEGER")
        # CHECK constraint — fails silently if already present
        _sp(cursor, "_er_chk", """
            ALTER TABLE order_number_registry
            ADD CONSTRAINT chk_last_number_positive CHECK (last_number >= 0)
        """)

        # Seed each series
        _fy   = _current_fiscal_year()
        _fyss = _fy_integers()
        for series, cfg in SERIES_CONFIG.items():
            _sp(cursor, f"_er_seed_{series[:8]}", """
                INSERT INTO order_number_registry
                    (series, last_number, prefix, fiscal_year, fy_start, fy_end)
                VALUES (%(s)s, %(n)s, %(p)s, %(fy)s, %(fys)s, %(fye)s)
                ON CONFLICT (series) DO UPDATE
                    SET fy_start = EXCLUDED.fy_start,
                        fy_end   = EXCLUDED.fy_end
                    WHERE order_number_registry.fy_start IS NULL
            """, {
                "s":   series,
                "n":   cfg["start"] - 1,
                "p":   cfg["prefix"],
                "fy":  _fy,
                "fys": _fyss[0],
                "fye": _fyss[1],
            })

        # Audit log table
        _sp(cursor, "_er_log", """
            CREATE TABLE IF NOT EXISTS doc_number_log (
                id           BIGSERIAL    PRIMARY KEY,
                series       TEXT         NOT NULL,
                doc_number   TEXT         NOT NULL,
                seq_number   INTEGER      NOT NULL,
                fiscal_year  TEXT         NOT NULL,
                allocated_by TEXT,
                allocated_at TIMESTAMPTZ  DEFAULT NOW(),
                status       TEXT         NOT NULL DEFAULT 'USED',
                voided_at    TIMESTAMPTZ,
                voided_by    TEXT,
                void_reason  TEXT
            )
        """)
        _sp(cursor, "_er_idx1", "CREATE INDEX IF NOT EXISTS idx_doc_number_log_series ON doc_number_log (series, allocated_at DESC)")
        _sp(cursor, "_er_idx2", "CREATE INDEX IF NOT EXISTS idx_doc_number_log_status ON doc_number_log (status) WHERE status != 'USED'")

        # Additive log column migrations
        for _col, _def in [
            ("status",      "TEXT NOT NULL DEFAULT 'USED'"),
            ("voided_at",   "TIMESTAMPTZ"),
            ("voided_by",   "TEXT"),
            ("void_reason", "TEXT"),
        ]:
            _sp(cursor, f"_er_lc_{_col[:6]}", f"ALTER TABLE doc_number_log ADD COLUMN IF NOT EXISTS {_col} {_def}")

        return True

    except Exception as e:
        logger.warning(f"[OrderNumberRegistry] ensure_registry failed: {e}")
        return False


def _current_fiscal_year() -> str:
    """
    Returns fiscal year as 4-digit string e.g. '2627' for FY 2026-27.

    Stored as TEXT in DB but always in compact 4-digit format:
        April 2026 – March 2027  →  '2627'
        April 2027 – March 2028  →  '2728'

    Avoids ambiguity of '25-26' / '2025-26' / '2026-27' formats
    that caused registry mismatches in earlier versions.
    """
    import datetime
    today = datetime.date.today()
    if today.month >= 4:
        fy_start, fy_end = today.year, today.year + 1
    else:
        fy_start, fy_end = today.year - 1, today.year
    return f"{str(fy_start)[2:]}{str(fy_end)[2:]}"


def _fy_integers() -> tuple:
    """
    Returns (fy_start, fy_end) as integers.
    Example: April 2026 → (2026, 2027)
    Stored in fy_start / fy_end columns — unambiguous, no string parsing needed.
    """
    import datetime
    today = datetime.date.today()
    if today.month >= 4:
        return (today.year, today.year + 1)
    return (today.year - 1, today.year)


def _sync_from_existing(cursor):
    """
    If orders table already has display_order_no values from the old
    PostgreSQL sequence, seed our registry to the current max so we
    continue numbering from where we left off with no overlap.
    """
    try:
        # Sync unified counter (RETAIL covers both RETAIL+WHOLESALE)
        cursor.execute("""
            SELECT COALESCE(MAX(display_order_no), 0)
            FROM orders
            WHERE display_order_no IS NOT NULL
              AND order_type NOT IN ('CONSULTATION','PURCHASE','RETURN')
        """)
        row = cursor.fetchone()
        max_no = int(row[0]) if row and row[0] else 0
        if max_no > 0:
            series = "RETAIL" if UNIFIED_SERIES else "RETAIL"
            cursor.execute("""
                UPDATE order_number_registry
                SET last_number = GREATEST(last_number, %(m)s)
                WHERE series = %(s)s
            """, {"m": max_no, "s": series})

        # Sync CONSULTATION
        cursor.execute("""
            SELECT COALESCE(MAX(display_order_no), 0)
            FROM orders
            WHERE display_order_no IS NOT NULL
              AND order_type = 'CONSULTATION'
        """)
        row = cursor.fetchone()
        cons_max = int(row[0]) if row and row[0] else 0
        if cons_max > 0:
            cursor.execute("""
                UPDATE order_number_registry
                SET last_number = GREATEST(last_number, %(m)s)
                WHERE series = 'CONSULTATION'
            """, {"m": cons_max})

    except Exception as e:
        logger.debug(f"[OrderNumberRegistry] _sync_from_existing skipped: {e}")


def next_order_number(
    cursor,
    series: str = "RETAIL",
    order_type: str = "",
) -> Tuple[int, int]:
    """
    Atomically claim the next sequential number for the given series.

    MUST be called inside an open transaction — the lock is held until
    the transaction commits or rolls back.

    Returns (last_number, display_order_no) where:
      last_number    = raw integer counter (e.g. 212)
      display_order_no = same as last_number (used as the visible number)

    Thread / server safety:
      SELECT ... FOR UPDATE acquires a row-level lock.
      Any concurrent call on the same series WAITS here until this
      transaction completes. Zero gap guaranteed.
    """
    # Normalise series
    _series = _resolve_series(series, order_type)

    # Ensure table and seed row exist (idempotent)
    ensure_registry(cursor)

    # ── THE CRITICAL SECTION ──────────────────────────────────────────────
    # FOR UPDATE locks the row. Concurrent calls block here until commit.
    cursor.execute("""
        SELECT last_number
        FROM   order_number_registry
        WHERE  series = %(s)s
        FOR UPDATE
    """, {"s": _series})

    row = cursor.fetchone()
    if not row:
        # ── Self-heal: series row missing — create it now ─────────────────
        # This handles: missing seed, manual delete, new series added to config.
        # Safe: ON CONFLICT DO NOTHING means concurrent workers won't double-insert.
        _fy   = _current_fiscal_year()
        _fyss = _fy_integers()
        cursor.execute("""
            INSERT INTO order_number_registry
                (series, last_number, prefix, fiscal_year, fy_start, fy_end)
            VALUES (%(s)s, 0, %(p)s, %(fy)s, %(fys)s, %(fye)s)
            ON CONFLICT (series) DO NOTHING
        """, {
            "s":   _series,
            "p":   SERIES_CONFIG.get(_series, {}).get("prefix", ""),
            "fy":  _fy,
            "fys": _fyss[0],
            "fye": _fyss[1],
        })
        # Re-read with lock after insert
        cursor.execute("""
            SELECT last_number FROM order_number_registry
            WHERE series = %(s)s FOR UPDATE
        """, {"s": _series})
        row = cursor.fetchone() or (0,)
        logger.warning(f"[OrderNumberRegistry] Self-healed missing series: {_series}")

    next_no = int(row[0]) + 1

    # Write the incremented value — still inside the same transaction
    cursor.execute("""
        UPDATE order_number_registry
        SET    last_number = %(n)s,
               updated_at  = NOW()
        WHERE  series = %(s)s
    """, {"n": next_no, "s": _series})

    logger.debug(f"[OrderNumberRegistry] {_series} → {next_no}")

    # ── Audit log: write inside same transaction — rolls back on failure ──
    # allocated_by: try to get Streamlit session user, fall back to process
    _alloc_by = "system"
    try:
        import streamlit as _st
        _alloc_by = _st.session_state.get("user_name") or "system"
    except Exception:
        pass
    try:
        _doc_no_preview = format_doc_number(_series, next_no)
        cursor.execute("""
            INSERT INTO doc_number_log
                (series, doc_number, seq_number, fiscal_year, allocated_by)
            VALUES (%s, %s, %s, %s, %s)
        """, (_series, _doc_no_preview, next_no, _fiscal_year_short(), _alloc_by))
    except Exception as _log_e:
        logger.debug(f"[OrderNumberRegistry] audit log write skipped: {_log_e}")
        # Non-fatal — number allocation still succeeds

    return next_no, next_no


def _resolve_series(series: str, order_type: str = "") -> str:
    """
    Normalise series name. Handles order_type aliases.
    """
    ot = order_type.upper()
    s  = series.upper()

    # Explicit alias map
    _ALIAS = {
        "CONSULTATION":     "CONSULTATION",
        "CHALLAN":          "CHALLAN",
        "INVOICE":          "INVOICE",
        "CREDIT_NOTE":      "CREDIT_NOTE",
        "DEBIT_NOTE":       "DEBIT_NOTE",
        "PAYMENT":          "PAYMENT",
        "PURCHASE_ORDER":   "PURCHASE_ORDER",
        "PURCHASE_INVOICE": "PURCHASE_INVOICE",
        "PURCHASE_RETURN":  "PURCHASE_RETURN",
        "JOURNAL":          "JOURNAL",
        "RECEIPT":          "RECEIPT",
        "RETURN":           "RETURN",
        "ADVANCE":          "ADVANCE",
        "WHOLESALE":        "RETAIL" if UNIFIED_SERIES else "WHOLESALE",
    }

    # Check explicit series first
    if s in _ALIAS:
        return _ALIAS[s]
    if s in SERIES_CONFIG:
        return s

    # Fall through order_type
    if ot in _ALIAS:
        return _ALIAS[ot]

    return "RETAIL"


def format_doc_number(series: str, number: int) -> str:
    """
    Format a document number in the standard format:
        PREFIX/FY/NNNN
    Examples:
        CH/2526/0042   INV/2526/0042   CN/2526/0007   JV/2526/0001
    """
    _series = _resolve_series(series)
    cfg     = SERIES_CONFIG.get(_series, {"prefix": series, "pad": 4})
    prefix  = cfg["prefix"]
    pad     = cfg.get("pad", 4)
    fy      = _fiscal_year_short()
    return f"{prefix}/{fy}/{str(number).zfill(pad)}"


def _fiscal_year_short() -> str:
    """
    Returns 4-digit FY string for document number formatting.
    Delegates to _current_fiscal_year() — single source of truth.
    Example: April 2026 → '2627'  (used in CH/2627/0014)
    """
    return _current_fiscal_year()


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP SYNC — call ONCE from app.py on boot, NOT inside order transactions
# ─────────────────────────────────────────────────────────────────────────────

def sync_registry_from_existing_orders() -> bool:
    """
    One-time startup sync: seeds registry counters from existing DB data.

    Call this from app.py / startup, not from inside order-save transactions.
    It's safe to call on every app boot — GREATEST() means it never goes backward.

    Why separated from ensure_registry():
      - Running MAX() scans inside an order-save transaction is dangerous.
        If that transaction rolls back, the registry update could be inconsistent.
      - This runs in its own short transaction, safely isolated.
    """
    try:
        from modules.sql_adapter import get_transaction_connection, close_connection
        conn   = get_transaction_connection()
        cursor = conn.cursor()
        try:
            ensure_registry(cursor)      # create table/seed rows if missing
            _sync_from_existing(cursor)  # align counters with existing data
            conn.commit()
            logger.info("[OrderNumberRegistry] Startup sync complete")
            return True
        except Exception as e:
            conn.rollback()
            logger.warning(f"[OrderNumberRegistry] Startup sync failed (non-fatal): {e}")
            return False
        finally:
            cursor.close()
            close_connection(conn)
    except Exception as e:
        logger.warning(f"[OrderNumberRegistry] Startup sync outer error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — one function per document type
# ─────────────────────────────────────────────────────────────────────────────

def next_challan_no(cursor) -> str:
    """Returns next challan number e.g. CH/2526/0042"""
    _, n = next_order_number(cursor, "CHALLAN")
    return format_doc_number("CHALLAN", n)


def next_invoice_no(cursor) -> str:
    """Returns next invoice number e.g. INV/2526/0042"""
    _, n = next_order_number(cursor, "INVOICE")
    return format_doc_number("INVOICE", n)


def next_credit_note_no(cursor) -> str:
    """Returns next credit note number e.g. CN/2526/0007"""
    _, n = next_order_number(cursor, "CREDIT_NOTE")
    return format_doc_number("CREDIT_NOTE", n)


def next_debit_note_no(cursor) -> str:
    """Returns next debit note number e.g. DN/2526/0003"""
    _, n = next_order_number(cursor, "DEBIT_NOTE")
    return format_doc_number("DEBIT_NOTE", n)


def next_payment_no(cursor) -> str:
    """Returns next payment number e.g. PAY/2526/0018"""
    _, n = next_order_number(cursor, "PAYMENT")
    return format_doc_number("PAYMENT", n)


def next_purchase_order_no(cursor) -> str:
    """Returns next purchase order number e.g. PO/2526/0009"""
    _, n = next_order_number(cursor, "PURCHASE_ORDER")
    return format_doc_number("PURCHASE_ORDER", n)


def next_purchase_invoice_no(cursor) -> str:
    """Returns next purchase invoice number e.g. PINV/2526/0009"""
    _, n = next_order_number(cursor, "PURCHASE_INVOICE")
    return format_doc_number("PURCHASE_INVOICE", n)


def next_journal_no(cursor) -> str:
    """Returns next journal voucher number e.g. JV/2526/0001"""
    _, n = next_order_number(cursor, "JOURNAL")
    return format_doc_number("JOURNAL", n)


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE HELPERS (no cursor needed — use their own transaction)
# ─────────────────────────────────────────────────────────────────────────────

def alloc_doc_number(series: str, cursor=None) -> str:
    """
    Allocate a document number.

    TWO MODES:
    ── cursor provided (PREFERRED): number allocation joins the caller's transaction.
       Number only commits when the caller commits. If caller rolls back → no gap.
       Example:
           conn = get_transaction_connection()
           cur  = conn.cursor()
           doc_no = alloc_doc_number("CHALLAN", cursor=cur)
           cur.execute("INSERT INTO challans ...")
           conn.commit()   # number + insert commit together

    ── no cursor (LEGACY / standalone): opens its own short transaction.
       Number commits before the document INSERT — two-commit pattern.
       Gaps can occur if the INSERT later fails.
       Use only when refactoring to pass cursor is not yet done.
    """
    if cursor is not None:
        # ── Cursor provided: join caller's transaction ────────────────────
        try:
            _, n = next_order_number(cursor, series)
            return format_doc_number(series, n)
        except Exception as e:
            logger.error(f"[DocNumberRegistry] alloc_doc_number({series}) with cursor failed: {e}")
            raise

    # ── No cursor: own transaction (legacy path) ──────────────────────────
    try:
        from modules.sql_adapter import get_transaction_connection, close_connection
        conn   = get_transaction_connection()
        cur    = conn.cursor()
        try:
            _, n = next_order_number(cur, series)
            num  = format_doc_number(series, n)
            conn.commit()
            return num
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            close_connection(conn)
    except Exception as e:
        logger.error(f"[DocNumberRegistry] alloc_doc_number({series}) failed: {e}")
        import uuid as _u
        cfg = SERIES_CONFIG.get(_resolve_series(series), {"prefix": series})
        return f"{cfg['prefix']}/ERR/{_u.uuid4().hex[:6].upper()}"


# ─────────────────────────────────────────────────────────────────────────────
# VOID TRACKING — mark a number as voided when its transaction failed
# ─────────────────────────────────────────────────────────────────────────────

def void_doc_number(doc_number: str, reason: str = "", voided_by: str = "system") -> bool:
    """
    Mark an allocated number as VOID in doc_number_log.

    Call this when a document is cancelled or its save failed after number
    allocation. The number gap is preserved (ERP standard — gaps are auditable)
    but now the gap is explained in the log rather than silently missing.

    Example:
        void_doc_number("CH/2627/0015", reason="User cancelled before save")
    """
    try:
        from modules.sql_adapter import run_write
        run_write("""
            UPDATE doc_number_log
            SET    status      = 'VOID',
                   voided_at   = NOW(),
                   voided_by   = %(by)s,
                   void_reason = %(reason)s
            WHERE  doc_number  = %(dn)s
              AND  status      = 'USED'
        """, {"dn": doc_number, "by": voided_by, "reason": reason or "Cancelled"})
        logger.info(f"[DocNumberLog] Voided: {doc_number} — {reason}")
        return True
    except Exception as e:
        logger.warning(f"[DocNumberLog] void_doc_number failed: {e}")
        return False


def voided_numbers(series: str = None) -> list:
    """
    Return list of voided numbers — for audit / gap explanation report.
    Pass series=None to get all, or series='CHALLAN' to filter.
    """
    try:
        from modules.sql_adapter import run_query
        if series:
            rows = run_query("""
                SELECT series, doc_number, seq_number, fiscal_year,
                       allocated_by, allocated_at, voided_at, void_reason
                FROM   doc_number_log
                WHERE  status = 'VOID' AND series = %(s)s
                ORDER  BY allocated_at DESC
            """, {"s": series.upper()}) or []
        else:
            rows = run_query("""
                SELECT series, doc_number, seq_number, fiscal_year,
                       allocated_by, allocated_at, voided_at, void_reason
                FROM   doc_number_log
                WHERE  status = 'VOID'
                ORDER  BY allocated_at DESC
                LIMIT  200
            """) or []
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[DocNumberLog] voided_numbers query failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-BRANCH READINESS
# ─────────────────────────────────────────────────────────────────────────────
# Current design: global sequence per series.
# When multi-branch is needed, the registry PRIMARY KEY changes to:
#   (series, fy_start, branch_id)
# and SERIES_CONFIG gains a branch_id dimension.
#
# Migration path (zero-downtime):
#   1. Add branch_id TEXT column with DEFAULT 'HQ'
#   2. Change PK to (series, branch_id)
#   3. Seed new branch rows
#   4. Pass branch_id through next_order_number()
#
# Format becomes: PREFIX/FY/BRANCH/NNNN  e.g. CH/2627/NGP/0042
# All existing rows default to branch_id='HQ' — no data loss.
#
# This stub documents the design so the migration is not a surprise.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# AUDIT / HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────

def audit_gaps(order_type_filter: str = "RETAIL") -> dict:
    """
    Scan orders table and report any gaps in display_order_no.
    Returns dict with: {gaps: [(from, to)], total_orders, max_no, missing_count}

    Use from admin / system health page.
    """
    try:
        from modules.sql_adapter import run_query

        if order_type_filter == "RETAIL":
            rows = run_query("""
                SELECT display_order_no
                FROM orders
                WHERE order_type NOT IN ('CONSULTATION','PURCHASE','RETURN')
                  AND display_order_no IS NOT NULL
                  AND display_order_no > 0
                ORDER BY display_order_no
            """) or []
        else:
            rows = run_query("""
                SELECT display_order_no
                FROM orders
                WHERE order_type = %(ot)s
                  AND display_order_no IS NOT NULL
                  AND display_order_no > 0
                ORDER BY display_order_no
            """, {"ot": order_type_filter}) or []

        numbers = [r["display_order_no"] for r in rows]
        if not numbers:
            return {"gaps": [], "total_orders": 0, "max_no": 0, "missing_count": 0}

        gaps = []
        for i in range(len(numbers) - 1):
            if numbers[i + 1] != numbers[i] + 1:
                gaps.append((numbers[i], numbers[i + 1]))

        return {
            "gaps":          gaps,
            "total_orders":  len(numbers),
            "max_no":        max(numbers),
            "min_no":        min(numbers),
            "missing_count": sum(b - a - 1 for a, b in gaps),
            "gap_count":     len(gaps),
        }

    except Exception as e:
        return {"error": str(e), "gaps": [], "total_orders": 0,
                "max_no": 0, "missing_count": 0}


def registry_status() -> list[dict]:
    """
    Return current state of all series counters.
    Use from admin / system health page.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT series, last_number, prefix, fiscal_year, updated_at
            FROM order_number_registry
            ORDER BY series
        """) or []
        return [dict(r) for r in rows]
    except Exception:
        return []
