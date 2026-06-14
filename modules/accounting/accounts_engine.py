"""
modules/accounting/accounts_engine.py
=======================================
Tally-equivalent double-entry accounting for DV ERP.

ARCHITECTURE:
  chart_of_accounts   → Ledger master (like Tally's Ledger)
  account_groups      → Group hierarchy (like Tally's Groups)
  journal_entries     → Voucher header (JV/2526/0001)
  journal_lines       → Debit/Credit legs of each voucher
  bank_transactions   → Bank statement entries (linked to journal)

AUTO-POSTING (triggered by existing flow):
  Invoice raised   → JV: Dr Debtors / Cr Sales / Cr GST Payable
  Payment received → JV: Dr Bank/Cash / Cr Debtors
  Disbursement     → JV: Dr Expense / Cr Bank/Cash
  Reversal         → JV: reverse of original

MANUAL ENTRY:
  Journal Voucher  → any Dr/Cr with narration
  Bank Entry       → bank statement line, links to JV
  Contra Voucher   → Cash↔Bank transfer
"""

from __future__ import annotations
from typing import List, Dict, Optional, Tuple
import uuid, datetime, logging

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
        _log.warning(f"[accounts._w] {e}")
        return False

def _tx(steps) -> Tuple[bool, Optional[str]]:
    """
    Execute a list of (sql, params) steps atomically.
    Uses run_transaction when available; falls back to individual run_write calls.

    IMPORTANT: run_transaction must NOT leave an open connection on failure.
    If it does (connection leak → idle in transaction locks), we force-terminate
    any idle-in-transaction sessions before falling back, to avoid lock contention.
    """
    try:
        from modules.sql_adapter import run_transaction
        run_transaction(steps)
        return True, None
    except Exception as _tx_err:
        _log.warning("[accounts._tx] run_transaction failed (%s) — falling back to run_write", _tx_err)
        # Terminate any connections this process left idle-in-transaction
        # so the fallback writes don't deadlock against our own leaked transaction.
        try:
            from modules.sql_adapter import run_write as _rw_clean
            _rw_clean(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE pid != pg_backend_pid() "
                "  AND state = 'idle in transaction' "
                "  AND application_name = current_setting('application_name', TRUE) "
                "  AND now() - state_change > interval '5 seconds'",
                {}
            )
        except Exception:
            pass
        ok, err = True, None
        for sql, p in steps:
            try:
                from modules.sql_adapter import run_write
                run_write(sql, p)
            except Exception as se:
                ok, err = False, str(se)
        return ok, err


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

def ensure_accounting_schema() -> None:
    """Create all accounting tables — idempotent, safe to run on every startup."""

    # Account Groups (like Tally primary groups)
    _w("""
        CREATE TABLE IF NOT EXISTS account_groups (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL UNIQUE,
            parent_id   UUID REFERENCES account_groups(id),
            nature      TEXT NOT NULL,  -- ASSET | LIABILITY | INCOME | EXPENSE
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Chart of Accounts / Ledger Master
    _w("""
        CREATE TABLE IF NOT EXISTS chart_of_accounts (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_code    TEXT UNIQUE,
            account_name    TEXT NOT NULL UNIQUE,
            group_id        UUID REFERENCES account_groups(id),
            nature          TEXT NOT NULL,  -- ASSET | LIABILITY | INCOME | EXPENSE
            account_type    TEXT NOT NULL,  -- BANK | CASH | PARTY | SALES | EXPENSE | TAX | OTHER
            opening_balance NUMERIC(16,2) DEFAULT 0,
            is_active       BOOLEAN DEFAULT TRUE,
            notes           TEXT,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Journal Entry Header (Voucher)
    _w("""
        CREATE TABLE IF NOT EXISTS journal_entries (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            voucher_no      TEXT NOT NULL UNIQUE,
            voucher_type    TEXT NOT NULL,  -- SALES | RECEIPT | PAYMENT | JOURNAL | CONTRA | PURCHASE
            voucher_date    DATE NOT NULL DEFAULT CURRENT_DATE,
            narration       TEXT,
            ref_doc_type    TEXT,           -- INVOICE | PAYMENT | CHALLAN | DISBURSEMENT
            ref_doc_id      TEXT,
            ref_doc_no      TEXT,
            total_debit     NUMERIC(16,2) DEFAULT 0,
            total_credit    NUMERIC(16,2) DEFAULT 0,
            is_auto_posted  BOOLEAN DEFAULT FALSE,  -- system-generated vs manual
            created_by      TEXT,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT jv_balanced CHECK (total_debit = total_credit)
        )
    """)

    # Journal Entry Lines (Dr/Cr legs)
    _w("""
        CREATE TABLE IF NOT EXISTS journal_lines (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            journal_id      UUID NOT NULL REFERENCES journal_entries(id),
            account_id      UUID NOT NULL REFERENCES chart_of_accounts(id),
            account_name    TEXT,           -- denormalized for display
            debit           NUMERIC(16,2) DEFAULT 0,
            credit          NUMERIC(16,2) DEFAULT 0,
            narration       TEXT,
            party_name      TEXT,           -- for party ledger reconciliation
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Bank Transactions (bank statement)
    _w("""
        CREATE TABLE IF NOT EXISTS bank_transactions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            bank_account_id UUID NOT NULL REFERENCES chart_of_accounts(id),
            txn_date        DATE NOT NULL,
            description     TEXT,
            debit           NUMERIC(16,2) DEFAULT 0,
            credit          NUMERIC(16,2) DEFAULT 0,
            balance         NUMERIC(16,2),
            ref_no          TEXT,           -- bank ref / UTR / cheque no
            journal_id      UUID REFERENCES journal_entries(id),
            is_reconciled   BOOLEAN DEFAULT FALSE,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Indexes
    _w("CREATE INDEX IF NOT EXISTS idx_jv_date    ON journal_entries(voucher_date DESC)")
    _w("CREATE INDEX IF NOT EXISTS idx_jv_type    ON journal_entries(voucher_type)")
    _w("CREATE INDEX IF NOT EXISTS idx_jv_ref     ON journal_entries(ref_doc_id)")
    _w("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_auto_ref_id
        ON journal_entries (
            UPPER(COALESCE(ref_doc_type, '')),
            COALESCE(ref_doc_id, '')
        )
        WHERE is_auto_posted = TRUE
          AND COALESCE(ref_doc_type, '') <> ''
          AND COALESCE(ref_doc_id, '') <> ''
    """)
    _w("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_auto_ref_no_when_no_id
        ON journal_entries (
            UPPER(COALESCE(ref_doc_type, '')),
            COALESCE(ref_doc_no, '')
        )
        WHERE is_auto_posted = TRUE
          AND COALESCE(ref_doc_type, '') <> ''
          AND COALESCE(ref_doc_id, '') = ''
          AND COALESCE(ref_doc_no, '') <> ''
    """)
    _w("CREATE INDEX IF NOT EXISTS idx_jl_jv      ON journal_lines(journal_id)")
    _w("CREATE INDEX IF NOT EXISTS idx_jl_account ON journal_lines(account_id)")
    _w("CREATE INDEX IF NOT EXISTS idx_bank_date  ON bank_transactions(txn_date DESC)")
    _w("CREATE INDEX IF NOT EXISTS idx_bank_recon ON bank_transactions(is_reconciled)")

    # Seed default chart of accounts
    _seed_default_accounts()


# ══════════════════════════════════════════════════════════════════════════════
# DEFAULT CHART OF ACCOUNTS (Tally-equivalent for optical business)
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_GROUPS = [
    # (name, nature, parent_name)
    ("Capital Account",        "LIABILITY", None),
    ("Loans (Liability)",      "LIABILITY", None),
    ("Current Liabilities",    "LIABILITY", None),
    ("Duties & Taxes",         "LIABILITY", "Current Liabilities"),
    ("Sundry Creditors",       "LIABILITY", "Current Liabilities"),
    ("Fixed Assets",           "ASSET",     None),
    ("Current Assets",         "ASSET",     None),
    ("Bank Accounts",          "ASSET",     "Current Assets"),
    ("Cash-in-Hand",           "ASSET",     "Current Assets"),
    ("Sundry Debtors",         "ASSET",     "Current Assets"),
    ("Stock-in-Hand",          "ASSET",     "Current Assets"),
    ("Direct Income",          "INCOME",    None),
    ("Indirect Income",        "INCOME",    None),
    ("Direct Expenses",        "EXPENSE",   None),
    ("Indirect Expenses",      "EXPENSE",   None),
    ("Purchase Accounts",      "EXPENSE",   None),
]

DEFAULT_ACCOUNTS = [
    # (code, name, group, nature, account_type)
    ("1001", "Cash",                    "Cash-in-Hand",       "ASSET",     "CASH"),
    ("1002", "Bank - SBI",              "Bank Accounts",      "ASSET",     "BANK"),
    ("1003", "Bank - HDFC",             "Bank Accounts",      "ASSET",     "BANK"),
    ("1004", "Petty Cash",              "Cash-in-Hand",       "ASSET",     "CASH"),
    ("1101", "Inventory Stock",         "Stock-in-Hand",      "ASSET",     "STOCK"),
    ("1501", "Furniture & Fixtures",    "Fixed Assets",       "ASSET",     "FIXED_ASSET"),
    ("1502", "Computer & Equipment",    "Fixed Assets",       "ASSET",     "FIXED_ASSET"),
    ("1599", "Accumulated Depreciation","Fixed Assets",       "ASSET",     "CONTRA_ASSET"),
    ("2001", "Sundry Debtors",          "Sundry Debtors",     "ASSET",     "PARTY"),
    ("2002", "Sundry Creditors",        "Sundry Creditors",   "LIABILITY", "PARTY"),
    ("3001", "Sales - Retail",          "Direct Income",      "INCOME",    "SALES"),
    ("3002", "Sales - Wholesale",       "Direct Income",      "INCOME",    "SALES"),
    ("3003", "Sales - Contact Lens",    "Direct Income",      "INCOME",    "SALES"),
    ("3004", "Consultation Fees",       "Direct Income",      "INCOME",    "SALES"),
    ("3005", "Other Income",            "Indirect Income",    "INCOME",    "OTHER"),
    ("3006", "Stock Adjustment Gain",   "Indirect Income",    "INCOME",    "OTHER"),
    ("4001", "Purchase - Frames",       "Purchase Accounts",  "EXPENSE",   "PURCHASE"),
    ("4002", "Purchase - Lenses",       "Purchase Accounts",  "EXPENSE",   "PURCHASE"),
    ("4003", "Purchase - Contact Lens", "Purchase Accounts",  "EXPENSE",   "PURCHASE"),
    ("4004", "Purchase - Accessories",  "Purchase Accounts",  "EXPENSE",   "PURCHASE"),
    ("4101", "Cost of Goods Sold",      "Direct Expenses",    "EXPENSE",   "COGS"),
    ("5001", "Salaries",                "Direct Expenses",    "EXPENSE",   "EXPENSE"),
    ("5002", "Rent",                    "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5003", "Electricity",             "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5004", "Telephone & Internet",    "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5005", "Courier & Transport",     "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5006", "Miscellaneous Expense",   "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5101", "Depreciation Expense",    "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("6001", "CGST Payable",            "Duties & Taxes",     "LIABILITY", "TAX"),
    ("6002", "SGST Payable",            "Duties & Taxes",     "LIABILITY", "TAX"),
    ("6003", "IGST Payable",            "Duties & Taxes",     "LIABILITY", "TAX"),
    ("6004", "TDS Payable",             "Duties & Taxes",     "LIABILITY", "TAX"),
    ("7001", "Capital",                 "Capital Account",    "LIABILITY", "OTHER"),
    ("7002", "Drawings",                "Capital Account",    "LIABILITY", "OTHER"),
]

EXPENSE_ACCOUNT_MAP = {
    "SUPPLIER":  "5006",
    "EXPENSE":   "5006",
    "SALARY":    "5001",
    "RENT":      "5002",
    "ELECTRICITY": "5003",
    "TELEPHONE": "5004",
    "COURIER":   "5005",
    "OTHER":     "5006",
}


def _seed_default_accounts() -> None:
    """Insert default groups and accounts if not already present."""
    # Groups
    group_map = {}
    for name, nature, parent_name in DEFAULT_GROUPS:
        existing = _q("SELECT id::text FROM account_groups WHERE name=%s LIMIT 1", (name,))
        if existing:
            group_map[name] = existing[0]["id"]
            continue
        parent_id = group_map.get(parent_name) if parent_name else None
        gid = str(uuid.uuid4())
        _w("""
            INSERT INTO account_groups (id, name, nature, parent_id)
            VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING
        """, (gid, name, nature, parent_id))
        group_map[name] = gid

    # Accounts
    for code, name, group_name, nature, atype in DEFAULT_ACCOUNTS:
        existing = _q("SELECT id FROM chart_of_accounts WHERE account_code=%s LIMIT 1", (code,))
        if existing:
            continue
        gid = group_map.get(group_name)
        _w("""
            INSERT INTO chart_of_accounts
                (account_code, account_name, group_id, nature, account_type)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (account_code) DO NOTHING
        """, (code, name, gid, nature, atype))


def get_account(code_or_name: str) -> Optional[Dict]:
    """Fetch account by code or name."""
    rows = _q("""
        SELECT id::text, account_code, account_name, nature, account_type
        FROM chart_of_accounts
        WHERE account_code = %s OR account_name = %s LIMIT 1
    """, (code_or_name, code_or_name))
    return rows[0] if rows else None


def get_all_accounts(nature: str = None) -> List[Dict]:
    nf = "WHERE a.nature = %s AND a.is_active=TRUE" if nature else "WHERE a.is_active=TRUE"
    params = (nature,) if nature else ()
    return _q(f"""
        SELECT a.id::text, a.account_code, a.account_name, a.nature, a.account_type,
               g.name AS group_name
        FROM chart_of_accounts a
        LEFT JOIN account_groups g ON g.id = a.group_id
        {nf}
        ORDER BY a.account_code
    """, params)


# ══════════════════════════════════════════════════════════════════════════════
# VOUCHER NUMBER GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def _gen_voucher_no(vtype: str) -> str:
    prefix_map = {
        "SALES":     "SV",
        "RECEIPT":   "RV",
        "PAYMENT":   "PV",
        "JOURNAL":   "JV",
        "CONTRA":    "CV",
        "PURCHASE":  "PIV",
        "CREDIT_NOTE": "CNV",
        "DEBIT_NOTE":  "DNV",
        "STOCK_JOURNAL": "STJV",
        "DEPRECIATION": "DPV",
    }
    prefix = prefix_map.get(vtype, "JV")
    try:
        from modules.db.order_number_registry import alloc_doc_number
        series_key = f"VOUCHER_{vtype}"
        return alloc_doc_number(series_key)
    except Exception:
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%y%m%d%H%M%S")
        return f"{prefix}/{ts}"


# ══════════════════════════════════════════════════════════════════════════════
# JOURNAL POSTING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def post_journal(
    voucher_type: str,
    voucher_date: datetime.date,
    narration: str,
    lines: List[Dict],           # [{"account_code": "1001", "debit": 0, "credit": 100, "narration": ""}]
    ref_doc_type: str = "",
    ref_doc_id: str = "",
    ref_doc_no: str = "",
    created_by: str = "System",
    is_auto: bool = False,
    bank_ref: str = "",
    mirror_to_payments: bool = True,
    mirror_to_party_ledger: bool = True,
) -> Tuple[bool, str, str]:
    """
    Post a balanced journal entry.
    Returns (success, voucher_no, error_msg).

    Validates:
      - At least 2 lines
      - Total debit = total credit (balanced)
      - All account codes exist
    """
    if len(lines) < 2:
        return False, "", "Journal must have at least 2 lines (one Dr, one Cr)."

    normalized_lines = []
    for line in lines:
        normalized_lines.append({
            **line,
            "debit": round(float(line.get("debit", 0) or 0), 2),
            "credit": round(float(line.get("credit", 0) or 0), 2),
        })
    lines = normalized_lines

    total_dr = round(sum(float(l.get("debit",  0) or 0) for l in lines), 2)
    total_cr = round(sum(float(l.get("credit", 0) or 0) for l in lines), 2)

    if total_dr != total_cr:
        return False, "", f"Journal not balanced — Dr ₹{total_dr:,.2f} ≠ Cr ₹{total_cr:,.2f}"

    if is_auto and ref_doc_type and (ref_doc_id or ref_doc_no):
        if ref_doc_id:
            existing = _q("""
                SELECT voucher_no
                FROM journal_entries
                WHERE is_auto_posted = TRUE
                  AND UPPER(COALESCE(ref_doc_type,'')) = UPPER(%s)
                  AND COALESCE(ref_doc_id,'') = %s
                ORDER BY created_at ASC
                LIMIT 1
            """, (ref_doc_type, ref_doc_id))
        else:
            existing = _q("""
                SELECT voucher_no
                FROM journal_entries
                WHERE is_auto_posted = TRUE
                  AND UPPER(COALESCE(ref_doc_type,'')) = UPPER(%s)
                  AND COALESCE(ref_doc_id,'') = ''
                  AND COALESCE(ref_doc_no,'') = %s
                ORDER BY created_at ASC
                LIMIT 1
            """, (ref_doc_type, ref_doc_no))
        if existing:
            vno = existing[0].get("voucher_no") or ""
            _log.info("[accounts] skipped duplicate auto JV for %s %s/%s -> %s",
                      ref_doc_type, ref_doc_id, ref_doc_no, vno)
            return True, vno, ""

    # Resolve account IDs
    resolved = []
    for line in lines:
        acc = get_account(line.get("account_code") or line.get("account_name", ""))
        if not acc:
            return False, "", f"Account not found: {line.get('account_code') or line.get('account_name')}"
        resolved.append({
            **line,
            "account_id": acc["id"],
            "account_name": acc["account_name"],
            "account_type": acc.get("account_type", ""),
        })

    try:
        from modules.core.date_guard import is_future_date
        if is_future_date(voucher_date) and any(
            str(l.get("account_type") or "").upper() in ("BANK", "CASH")
            and (float(l.get("debit", 0) or 0) > 0 or float(l.get("credit", 0) or 0) > 0)
            for l in resolved
        ):
            return False, "", (
                "Cash/Bank voucher date cannot be in the future. "
                "Only provisional advance cheques may be post-dated from the payment screen."
            )
    except Exception as _dg_e:
        return False, "", f"Voucher date validation failed: {_dg_e}"

    jid     = str(uuid.uuid4())
    vno     = _gen_voucher_no(voucher_type)
    steps   = []

    steps.append(("""
        INSERT INTO journal_entries
            (id, voucher_no, voucher_type, voucher_date, narration,
             ref_doc_type, ref_doc_id, ref_doc_no,
             total_debit, total_credit, is_auto_posted, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (jid, vno, voucher_type, voucher_date, narration,
          ref_doc_type or None, ref_doc_id or None, ref_doc_no or None,
          total_dr, total_cr, is_auto, created_by)))

    for line in resolved:
        steps.append(("""
            INSERT INTO journal_lines
                (journal_id, account_id, account_name, debit, credit, narration, party_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (jid, line["account_id"], line["account_name"],
              float(line.get("debit",  0) or 0),
              float(line.get("credit", 0) or 0),
              line.get("narration", narration),
              line.get("party_name", ""))))

    # Bank/cash attachment: every voucher leg touching cash/bank also creates
    # a bank_transactions row, so Accounts > Bank Book has ref/reconcile detail.
    bank_cash_lines = [
        l for l in resolved
        if str(l.get("account_type") or "").upper() in ("BANK", "CASH")
        and (float(l.get("debit", 0) or 0) > 0 or float(l.get("credit", 0) or 0) > 0)
    ]
    for line in bank_cash_lines:
        dr = float(line.get("debit", 0) or 0)
        cr = float(line.get("credit", 0) or 0)
        steps.append(("""
            INSERT INTO bank_transactions
                (bank_account_id, txn_date, description, debit, credit,
                 ref_no, journal_id, is_reconciled)
            VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE)
        """, (
            line["account_id"], voucher_date,
            line.get("narration") or narration,
            dr, cr,
            bank_ref or ref_doc_no or vno,
            jid,
        )))

    # Manual Journal/Contra mirror: put cash/bank legs into payments with a
    # direction-specific type. Registers can show them without treating them as
    # customer receipts or disbursements.
    manual_mirror = (not is_auto) and str(voucher_type or "").upper() in ("JOURNAL", "CONTRA")
    if manual_mirror and mirror_to_payments:
        for idx, line in enumerate(bank_cash_lines, 1):
            dr = float(line.get("debit", 0) or 0)
            cr = float(line.get("credit", 0) or 0)
            amount = round(dr or cr, 2)
            if amount <= 0:
                continue
            direction = "IN" if dr > 0 else "OUT"
            ptype = f"{str(voucher_type).upper()}_{direction}"
            pno = vno if len(bank_cash_lines) == 1 else f"{vno}-{idx}"
            mode = "CASH" if str(line.get("account_type") or "").upper() == "CASH" else "BANK"
            steps.append(("""
                INSERT INTO payments
                    (id, payment_no, party_name, amount, method, payment_date,
                     payment_mode, reference_no, bank_name, remarks, payment_type,
                     is_advance, created_by, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,NOW())
            """, (
                str(uuid.uuid4()), pno,
                line.get("party_name") or "",
                amount,
                mode, voucher_date, mode,
                bank_ref or ref_doc_no or vno,
                line.get("account_name") or "",
                line.get("narration") or narration,
                ptype,
                created_by,
            )))

    if manual_mirror and mirror_to_party_ledger:
        for line in resolved:
            party_name = str(line.get("party_name") or "").strip()
            if not party_name:
                continue
            dr = float(line.get("debit", 0) or 0)
            cr = float(line.get("credit", 0) or 0)
            if dr <= 0 and cr <= 0:
                continue
            steps.append(("""
                INSERT INTO party_ledger
                    (party_name, entry_date, entry_type, ref_id, ref_no,
                     debit, credit, running_balance, narration, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s,%s)
            """, (
                party_name, voucher_date, str(voucher_type).upper(),
                jid, vno, dr, cr,
                line.get("narration") or narration,
                created_by,
            )))

    ok, err = _tx(steps)
    if not ok:
        return False, "", f"Journal post failed: {err}"

    _log.info(f"[accounts] Posted {vno} | {voucher_type} | Dr={total_dr} Cr={total_cr}")
    return True, vno, ""


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-POSTING HOOKS (called by existing flow)
# ══════════════════════════════════════════════════════════════════════════════

def post_invoice_jv(
    invoice_no: str, invoice_id: str,
    party_name: str, grand_total: float,
    taxable: float, tax_amount: float,
    order_type: str, voucher_date: datetime.date,
    created_by: str = "System",
) -> Tuple[bool, str]:
    """
    Sales Invoice → JV:
      Dr  Sundry Debtors          grand_total
      Cr  Sales - Retail/Wholesale taxable
      Cr  CGST Payable             tax/2
      Cr  SGST Payable             tax/2
    """
    sales_code = "3002" if "WHOLESALE" in order_type.upper() else "3001"
    cgst = round(tax_amount / 2, 2)
    sgst = round(tax_amount - cgst, 2)

    # Ensure Dr = Cr exactly — taxable+tax must equal grand_total
    # Adjust taxable to absorb any rounding difference
    cgst     = round(tax_amount / 2, 2)
    sgst     = round(tax_amount - cgst, 2)
    tax_total = cgst + sgst
    # taxable is the plug — make it balance exactly
    _taxable = round(grand_total - tax_total, 2)

    lines = [
        {"account_code": "2001",      "debit": grand_total, "credit": 0,          "party_name": party_name},
        {"account_code": sales_code,  "debit": 0,           "credit": _taxable,   "party_name": party_name},
    ]
    if cgst > 0:
        lines.append({"account_code": "6001", "debit": 0, "credit": cgst})
    if sgst > 0:
        lines.append({"account_code": "6002", "debit": 0, "credit": sgst})

    ok, vno, err = post_journal(
        voucher_type="SALES",
        voucher_date=voucher_date,
        narration=f"Sales Invoice {invoice_no} — {party_name}",
        lines=lines,
        ref_doc_type="INVOICE", ref_doc_id=invoice_id, ref_doc_no=invoice_no,
        created_by=created_by, is_auto=True,
    )
    return ok, vno if ok else err


def post_payment_receipt_jv(
    payment_no: str, payment_id: str,
    party_name: str, amount: float,
    payment_mode: str, bank_account: str,
    voucher_date: datetime.date,
    created_by: str = "System",
) -> Tuple[bool, str]:
    """
    Payment Receipt → JV:
      Dr  Bank / Cash       amount
      Cr  Sundry Debtors    amount
    """
    # Map mode to account
    # CASH → Cash (1001) | UPI/NEFT/RTGS/CHEQUE/CARD → Bank-SBI (1002)
    acc_code = "1001" if (payment_mode or "").upper().strip() == "CASH" else "1002"
    is_consultation_receipt = str(payment_no or "").upper().startswith("CPR")
    credit_line = (
        {"account_code": "3004", "debit": 0, "credit": amount, "party_name": party_name}
        if is_consultation_receipt
        else {"account_code": "2001", "debit": 0, "credit": amount, "party_name": party_name}
    )

    ok, vno, err = post_journal(
        voucher_type="RECEIPT",
        voucher_date=voucher_date,
        narration=f"Payment received {payment_no} — {party_name} — {payment_mode}",
        lines=[
            {"account_code": acc_code, "debit": amount,  "credit": 0, "party_name": party_name},
            credit_line,
        ],
        ref_doc_type="PAYMENT", ref_doc_id=payment_id, ref_doc_no=payment_no,
        created_by=created_by, is_auto=True,
    )
    return ok, vno if ok else err


def post_disbursement_jv(
    payment_no: str, payment_id: str,
    payee: str, amount: float,
    category: str, payment_mode: str,
    voucher_date: datetime.date,
    created_by: str = "System",
) -> Tuple[bool, str]:
    """
    Disbursement → JV:
      Dr  Expense account   amount
      Cr  Bank / Cash       amount
    """
    expense_code = EXPENSE_ACCOUNT_MAP.get(category.upper(), "5006")
    cash_modes   = {"CASH"}
    bank_code    = "1001" if payment_mode.upper() in cash_modes else "1002"

    ok, vno, err = post_journal(
        voucher_type="PAYMENT",
        voucher_date=voucher_date,
        narration=f"Payment to {payee} — {category}",
        lines=[
            {"account_code": expense_code, "debit": amount,  "credit": 0},
            {"account_code": bank_code,    "debit": 0, "credit": amount},
        ],
        ref_doc_type="DISBURSEMENT", ref_doc_id=payment_id, ref_doc_no=payment_no,
        created_by=created_by, is_auto=True,
    )
    return ok, vno if ok else err


def post_purchase_invoice_jv(
    invoice_no: str, invoice_id: str,
    supplier_name: str, grand_total: float,
    taxable: float, tax_amount: float,
    purchase_category: str,
    voucher_date: datetime.date,
    created_by: str = "System",
) -> Tuple[bool, str]:
    """
    Purchase Invoice → JV:
      Dr  Purchase Account     taxable
      Dr  GST Input Credit     tax_amount  (if GST registered)
      Cr  Sundry Creditors     grand_total
    """
    try:
        from modules.core.date_guard import validate_not_future
        _ok_dt, _msg_dt = validate_not_future(voucher_date, "Purchase invoice date")
        if not _ok_dt:
            return False, _msg_dt
    except Exception as _dg_e:
        return False, f"Purchase date validation failed: {_dg_e}"

    # Map category to purchase account
    purchase_map = {
        "FRAMES":       "4001",
        "LENSES":       "4002",
        "CONTACT LENS": "4003",
        "ACCESSORIES":  "4004",
    }
    cat_upper    = purchase_category.upper() if purchase_category else ""
    purch_code   = next((v for k, v in purchase_map.items() if k in cat_upper), "4002")

    _grand_total = round(float(grand_total or 0), 2)
    _tax_amount = round(max(float(tax_amount or 0), 0), 2)
    _taxable = round(_grand_total - _tax_amount, 2)
    if _taxable < 0:
        _taxable = round(float(taxable or 0), 2)
        _tax_amount = round(max(_grand_total - _taxable, 0), 2)

    lines = [
        {"account_code": purch_code, "debit": _taxable,     "credit": 0,            "party_name": supplier_name},
        {"account_code": "2002",     "debit": 0,            "credit": _grand_total,  "party_name": supplier_name},
    ]
    # Only add GST input if there is tax
    if _tax_amount > 0.01:
        cgst = round(_tax_amount / 2, 2)
        sgst = round(_tax_amount - cgst, 2)
        lines.insert(1, {"account_code": "6001", "debit": cgst, "credit": 0})  # CGST Input / set-off
        lines.insert(2, {"account_code": "6002", "debit": sgst, "credit": 0})  # SGST Input / set-off
        # Re-balance: Dr total must = Cr total
        # Dr = taxable + tax_amount, Cr = grand_total — already balanced if grand_total = taxable + tax
        pass

    ok, vno, err = post_journal(
        voucher_type = "PURCHASE",
        voucher_date = voucher_date,
        narration    = f"Purchase Invoice {invoice_no} — {supplier_name}",
        lines        = lines,
        ref_doc_type = "PURCHASE_INVOICE",
        ref_doc_id   = invoice_id,
        ref_doc_no   = invoice_no,
        created_by   = created_by,
        is_auto      = True,
    )
    return ok, vno if ok else err


def post_credit_note_jv(
    cn_number: str,
    cn_id: str,
    party_name: str,
    grand_total: float,
    taxable: float,
    cgst_amount: float = 0,
    sgst_amount: float = 0,
    igst_amount: float = 0,
    order_type: str = "RETAIL",
    voucher_date: datetime.date | None = None,
    created_by: str = "System",
) -> Tuple[bool, str]:
    """Credit Note -> Dr Sales/GST, Cr Sundry Debtors."""
    sales_code = "3002" if "WHOLESALE" in str(order_type or "").upper() else "3001"
    _grand_total = round(float(grand_total or 0), 2)
    _tax = round(float(cgst_amount or 0) + float(sgst_amount or 0) + float(igst_amount or 0), 2)
    _taxable = round(_grand_total - _tax, 2)
    if _taxable < 0:
        _taxable = round(float(taxable or 0), 2)
    lines = [
        {"account_code": sales_code, "debit": _taxable, "credit": 0, "party_name": party_name},
        {"account_code": "2001", "debit": 0, "credit": _grand_total, "party_name": party_name},
    ]
    if float(cgst_amount or 0) > 0.005:
        lines.insert(1, {"account_code": "6001", "debit": round(float(cgst_amount or 0), 2), "credit": 0})
    if float(sgst_amount or 0) > 0.005:
        lines.insert(2, {"account_code": "6002", "debit": round(float(sgst_amount or 0), 2), "credit": 0})
    if float(igst_amount or 0) > 0.005:
        lines.insert(1, {"account_code": "6003", "debit": round(float(igst_amount or 0), 2), "credit": 0})
    ok, vno, err = post_journal(
        voucher_type="CREDIT_NOTE",
        voucher_date=voucher_date or datetime.date.today(),
        narration=f"Credit Note {cn_number} — {party_name}",
        lines=lines,
        ref_doc_type="CREDIT_NOTE",
        ref_doc_id=cn_id,
        ref_doc_no=cn_number,
        created_by=created_by,
        is_auto=True,
    )
    return ok, vno if ok else err


def post_debit_note_jv(
    dn_number: str,
    dn_id: str,
    party_name: str,
    grand_total: float,
    taxable: float,
    cgst_amount: float = 0,
    sgst_amount: float = 0,
    igst_amount: float = 0,
    order_type: str = "RETAIL",
    voucher_date: datetime.date | None = None,
    created_by: str = "System",
) -> Tuple[bool, str]:
    """Debit Note -> Dr Sundry Debtors, Cr Sales/GST."""
    sales_code = "3002" if "WHOLESALE" in str(order_type or "").upper() else "3001"
    _grand_total = round(float(grand_total or 0), 2)
    _tax = round(float(cgst_amount or 0) + float(sgst_amount or 0) + float(igst_amount or 0), 2)
    _taxable = round(_grand_total - _tax, 2)
    if _taxable < 0:
        _taxable = round(float(taxable or 0), 2)
    lines = [
        {"account_code": "2001", "debit": _grand_total, "credit": 0, "party_name": party_name},
        {"account_code": sales_code, "debit": 0, "credit": _taxable, "party_name": party_name},
    ]
    if float(cgst_amount or 0) > 0.005:
        lines.append({"account_code": "6001", "debit": 0, "credit": round(float(cgst_amount or 0), 2)})
    if float(sgst_amount or 0) > 0.005:
        lines.append({"account_code": "6002", "debit": 0, "credit": round(float(sgst_amount or 0), 2)})
    if float(igst_amount or 0) > 0.005:
        lines.append({"account_code": "6003", "debit": 0, "credit": round(float(igst_amount or 0), 2)})
    ok, vno, err = post_journal(
        voucher_type="DEBIT_NOTE",
        voucher_date=voucher_date or datetime.date.today(),
        narration=f"Debit Note {dn_number} — {party_name}",
        lines=lines,
        ref_doc_type="DEBIT_NOTE",
        ref_doc_id=dn_id,
        ref_doc_no=dn_number,
        created_by=created_by,
        is_auto=True,
    )
    return ok, vno if ok else err


def post_stock_journal(
    *,
    amount: float,
    movement_type: str,
    voucher_date: datetime.date | None = None,
    narration: str = "",
    ref_doc_type: str = "STOCK_JOURNAL",
    ref_doc_id: str = "",
    ref_doc_no: str = "",
    created_by: str = "System",
) -> Tuple[bool, str]:
    """
    Stock Journal / inventory accounting:
      ISSUE / CONSUME / SALE_COST  -> Dr Cost of Goods Sold, Cr Inventory Stock
      INCREASE / FOUND / OPENING   -> Dr Inventory Stock, Cr Stock Adjustment Gain

    Use this only when a reliable stock valuation amount is available.
    Quantity-only stock movement must remain operational until valued.
    """
    amt = round(float(amount or 0), 2)
    if amt <= 0:
        return False, "Stock journal amount must be greater than zero."
    mtype = str(movement_type or "").upper().strip()
    if mtype in ("ISSUE", "CONSUME", "SALE_COST", "SHORTAGE", "DAMAGE"):
        lines = [
            {"account_code": "4101", "debit": amt, "credit": 0},
            {"account_code": "1101", "debit": 0, "credit": amt},
        ]
    elif mtype in ("INCREASE", "FOUND", "OPENING", "RECEIVE_ADJUSTMENT"):
        lines = [
            {"account_code": "1101", "debit": amt, "credit": 0},
            {"account_code": "3006", "debit": 0, "credit": amt},
        ]
    else:
        return False, "Unknown stock journal type. Use ISSUE/CONSUME or INCREASE/OPENING."
    ok, vno, err = post_journal(
        voucher_type="STOCK_JOURNAL",
        voucher_date=voucher_date or datetime.date.today(),
        narration=narration or f"Stock Journal {mtype}",
        lines=lines,
        ref_doc_type=ref_doc_type,
        ref_doc_id=ref_doc_id,
        ref_doc_no=ref_doc_no,
        created_by=created_by,
        is_auto=bool(ref_doc_id or ref_doc_no),
    )
    return ok, vno if ok else err


def post_depreciation_jv(
    *,
    asset_name: str,
    amount: float,
    voucher_date: datetime.date | None = None,
    ref_doc_id: str = "",
    ref_doc_no: str = "",
    created_by: str = "System",
) -> Tuple[bool, str]:
    """Depreciation -> Dr Depreciation Expense, Cr Accumulated Depreciation."""
    amt = round(float(amount or 0), 2)
    if amt <= 0:
        return False, "Depreciation amount must be greater than zero."
    label = str(asset_name or "Fixed Asset").strip()
    ok, vno, err = post_journal(
        voucher_type="DEPRECIATION",
        voucher_date=voucher_date or datetime.date.today(),
        narration=f"Depreciation — {label}",
        lines=[
            {"account_code": "5101", "debit": amt, "credit": 0},
            {"account_code": "1599", "debit": 0, "credit": amt},
        ],
        ref_doc_type="DEPRECIATION",
        ref_doc_id=ref_doc_id,
        ref_doc_no=ref_doc_no,
        created_by=created_by,
        is_auto=bool(ref_doc_id or ref_doc_no),
    )
    return ok, vno if ok else err


def post_reversal_jv(
    original_vno: str, original_jv_id: str,
    reversal_reason: str, voucher_date: datetime.date,
    created_by: str = "System",
) -> Tuple[bool, str]:
    """Reverse a journal — swap Dr↔Cr on all lines."""
    orig_lines = _q("""
        SELECT account_id::text, account_name, debit, credit
        FROM journal_lines WHERE journal_id = %s
    """, (original_jv_id,))

    if not orig_lines:
        return False, "Original journal lines not found."

    rev_lines = [
        {
            "account_code": line["account_name"],  # use name since we have it
            "debit":  float(line["credit"] or 0),
            "credit": float(line["debit"]  or 0),
        }
        for line in orig_lines
    ]

    ok, vno, err = post_journal(
        voucher_type="JOURNAL",
        voucher_date=voucher_date,
        narration=f"Reversal of {original_vno} — {reversal_reason}",
        lines=rev_lines,
        ref_doc_type="REVERSAL", ref_doc_id=original_jv_id, ref_doc_no=original_vno,
        created_by=created_by, is_auto=True,
    )
    return ok, vno if ok else err


# ══════════════════════════════════════════════════════════════════════════════
# TRIAL BALANCE & LEDGER QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_trial_balance(date_from: str, date_to: str) -> List[Dict]:
    """
    Trial Balance — all accounts with Dr/Cr totals for period.
    Opening balance + period movement = closing balance.
    """
    return _q("""
        WITH pre AS (
            SELECT l.account_id,
                   SUM(l.debit) - SUM(l.credit) AS pre_net
            FROM journal_lines l
            JOIN journal_entries j ON j.id = l.journal_id
            WHERE j.voucher_date < %s
            GROUP BY l.account_id
        ),
        period AS (
            SELECT l.account_id,
                   SUM(l.debit) AS period_dr,
                   SUM(l.credit) AS period_cr
            FROM journal_lines l
            JOIN journal_entries j ON j.id = l.journal_id
            WHERE j.voucher_date BETWEEN %s AND %s
            GROUP BY l.account_id
        )
        SELECT
            a.account_code AS "Code",
            a.account_name AS "Account",
            g.name AS "Group",
            a.nature AS "Nature",
            a.opening_balance AS "Opening (₹)",
            COALESCE(p.period_dr, 0) AS "Period Dr (₹)",
            COALESCE(p.period_cr, 0) AS "Period Cr (₹)",
            CASE
                WHEN (
                    CASE
                        WHEN a.nature IN ('ASSET', 'EXPENSE')
                        THEN a.opening_balance
                        ELSE -a.opening_balance
                    END
                    + COALESCE(pr.pre_net, 0)
                    + COALESCE(p.period_dr, 0) - COALESCE(p.period_cr, 0)
                ) >= 0
                THEN (
                    CASE
                        WHEN a.nature IN ('ASSET', 'EXPENSE')
                        THEN a.opening_balance
                        ELSE -a.opening_balance
                    END
                    + COALESCE(pr.pre_net, 0)
                    + COALESCE(p.period_dr, 0) - COALESCE(p.period_cr, 0)
                )
                ELSE 0
            END AS "Closing Dr (₹)",
            CASE
                WHEN (
                    CASE
                        WHEN a.nature IN ('ASSET', 'EXPENSE')
                        THEN a.opening_balance
                        ELSE -a.opening_balance
                    END
                    + COALESCE(pr.pre_net, 0)
                    + COALESCE(p.period_dr, 0) - COALESCE(p.period_cr, 0)
                ) < 0
                THEN -(
                    CASE
                        WHEN a.nature IN ('ASSET', 'EXPENSE')
                        THEN a.opening_balance
                        ELSE -a.opening_balance
                    END
                    + COALESCE(pr.pre_net, 0)
                    + COALESCE(p.period_dr, 0) - COALESCE(p.period_cr, 0)
                )
                ELSE 0
            END AS "Closing Cr (₹)"
        FROM chart_of_accounts a
        LEFT JOIN account_groups g ON g.id = a.group_id
        LEFT JOIN pre pr ON pr.account_id = a.id
        LEFT JOIN period p ON p.account_id = a.id
        WHERE a.is_active = TRUE
        ORDER BY a.account_code
    """, (date_from, date_from, date_to))


def get_account_ledger(account_code: str, date_from: str, date_to: str) -> List[Dict]:
    """All journal entries for one account — like Tally's account ledger."""
    return _q("""
        SELECT
            j.voucher_date::text    AS "Date",
            j.voucher_no            AS "Voucher No",
            j.voucher_type          AS "Type",
            j.narration             AS "Narration",
            l.debit                 AS "Dr (₹)",
            l.credit                AS "Cr (₹)",
            l.party_name            AS "Party"
        FROM journal_lines    l
        JOIN journal_entries  j ON j.id = l.journal_id
        JOIN chart_of_accounts a ON a.id = l.account_id
        WHERE a.account_code = %s
          AND j.voucher_date BETWEEN %s AND %s
        ORDER BY j.voucher_date ASC, j.created_at ASC
    """, (account_code, date_from, date_to))


def get_party_control_summary(account_code: str, date_from: str | None = None, date_to: str | None = None) -> List[Dict]:
    """Party-wise view for Sundry Debtors/Creditors control accounts."""
    acc = get_account(account_code)
    nature = (acc or {}).get("nature", "ASSET")
    sign_expr = "COALESCE(l.debit,0) - COALESCE(l.credit,0)"
    if nature == "LIABILITY":
        sign_expr = "COALESCE(l.credit,0) - COALESCE(l.debit,0)"

    return _q(f"""
        WITH control_lines AS (
            SELECT
                COALESCE(NULLIF(TRIM(l.party_name), ''), 'Unmapped Party') AS party_name,
                j.voucher_date,
                j.voucher_no,
                j.ref_doc_type,
                j.ref_doc_no,
                COALESCE(l.debit, 0) AS debit,
                COALESCE(l.credit, 0) AS credit,
                {sign_expr} AS signed_amount
            FROM journal_lines l
            JOIN journal_entries j ON j.id = l.journal_id
            JOIN chart_of_accounts a ON a.id = l.account_id
            WHERE a.account_code = %s
        ),
        agg AS (
            SELECT
                party_name,
                COALESCE(SUM(signed_amount) FILTER (WHERE voucher_date < %s), 0) AS opening,
                COALESCE(SUM(debit) FILTER (WHERE voucher_date BETWEEN %s AND %s), 0) AS period_dr,
                COALESCE(SUM(credit) FILTER (WHERE voucher_date BETWEEN %s AND %s), 0) AS period_cr,
                COALESCE(SUM(signed_amount), 0) AS closing,
                MAX(voucher_date) AS last_txn,
                COUNT(*) FILTER (WHERE voucher_date BETWEEN %s AND %s) AS entries
            FROM control_lines
            GROUP BY party_name
        )
        SELECT
            party_name AS "Party",
            ROUND(opening, 2) AS "Opening (₹)",
            ROUND(period_dr, 2) AS "Dr (₹)",
            ROUND(period_cr, 2) AS "Cr (₹)",
            ROUND(closing, 2) AS "Closing (₹)",
            last_txn::text AS "Last Transaction",
            entries AS "Entries"
        FROM agg
        WHERE ABS(opening) > 0.005
           OR ABS(period_dr) > 0.005
           OR ABS(period_cr) > 0.005
           OR ABS(closing) > 0.005
        ORDER BY ABS(closing) DESC, party_name
    """, (account_code, date_from, date_from, date_to, date_from, date_to, date_from, date_to))


def get_party_control_ledger(
    account_code: str,
    party_name: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> List[Dict]:
    """One debtor/creditor party ledger inside the control account."""
    acc = get_account(account_code)
    nature = (acc or {}).get("nature", "ASSET")
    sign_expr = "COALESCE(l.debit,0) - COALESCE(l.credit,0)"
    if nature == "LIABILITY":
        sign_expr = "COALESCE(l.credit,0) - COALESCE(l.debit,0)"

    blank_filter = party_name == "Unmapped Party"
    party_clause = (
        "COALESCE(NULLIF(TRIM(l.party_name), ''), 'Unmapped Party') = %s"
    )

    return _q(f"""
        WITH control_lines AS (
            SELECT
                j.voucher_date,
                j.voucher_no,
                j.voucher_type,
                COALESCE(NULLIF(j.ref_doc_type, ''), j.voucher_type) AS ref_doc_type,
                COALESCE(NULLIF(j.ref_doc_no, ''), j.voucher_no) AS ref_doc_no,
                j.narration,
                COALESCE(NULLIF(TRIM(l.party_name), ''), 'Unmapped Party') AS party_name,
                COALESCE(l.debit, 0) AS debit,
                COALESCE(l.credit, 0) AS credit,
                {sign_expr} AS signed_amount,
                j.created_at
            FROM journal_lines l
            JOIN journal_entries j ON j.id = l.journal_id
            JOIN chart_of_accounts a ON a.id = l.account_id
            WHERE a.account_code = %s
              AND {party_clause}
        ),
        opening AS (
            SELECT COALESCE(SUM(signed_amount), 0) AS opening
            FROM control_lines
            WHERE voucher_date < %s
        ),
        period AS (
            SELECT *
            FROM control_lines
            WHERE voucher_date BETWEEN %s AND %s
        )
        SELECT
            p.voucher_date::text AS "Date",
            p.voucher_no AS "Voucher No",
            p.voucher_type AS "Type",
            p.ref_doc_type AS "Doc Type",
            p.ref_doc_no AS "Ref No",
            p.narration AS "Narration",
            p.party_name AS "Party",
            ROUND(p.debit, 2) AS "Dr (₹)",
            ROUND(p.credit, 2) AS "Cr (₹)",
            ROUND(
                (SELECT opening FROM opening)
                + SUM(p.signed_amount) OVER (ORDER BY p.voucher_date, p.created_at, p.voucher_no),
                2
            ) AS "Balance (₹)"
        FROM period p
        ORDER BY p.voucher_date ASC, p.created_at ASC, p.voucher_no ASC
    """, (account_code, party_name, date_from, date_from, date_to))


def get_transaction_book(date_from: str | None = None, date_to: str | None = None, mode: str = "ALL") -> List[Dict]:
    """Combined/Cash/Bank transaction book from cash and bank account legs."""
    mode = (mode or "ALL").upper()
    mode_filter = ""
    params: tuple = (date_from, date_to)
    if mode == "CASH":
        mode_filter = "AND a.account_type = 'CASH'"
    elif mode == "BANK":
        mode_filter = "AND a.account_type = 'BANK'"

    return _q(f"""
        SELECT
            j.voucher_date::text AS "Date",
            j.voucher_no AS "Voucher No",
            j.voucher_type AS "Type",
            a.account_name AS "Account",
            COALESCE(NULLIF(l.party_name, ''), '') AS "Party",
            COALESCE(NULLIF(j.ref_doc_no, ''), bt.ref_no, j.voucher_no) AS "Ref",
            j.narration AS "Narration",
            ROUND(COALESCE(l.debit, 0), 2) AS "Receipts (₹)",
            ROUND(COALESCE(l.credit, 0), 2) AS "Payments (₹)",
            bt.ref_no AS "Bank Ref",
            COALESCE(bt.is_reconciled, FALSE) AS "Reconciled"
        FROM journal_lines l
        JOIN journal_entries j ON j.id = l.journal_id
        JOIN chart_of_accounts a ON a.id = l.account_id
        LEFT JOIN bank_transactions bt ON bt.journal_id = j.id
                                  AND bt.bank_account_id = l.account_id
        WHERE a.account_type IN ('CASH', 'BANK')
          AND (%s IS NULL OR j.voucher_date >= %s::date)
          AND (%s IS NULL OR j.voucher_date <= %s::date)
          {mode_filter}
        ORDER BY j.voucher_date ASC, j.created_at ASC, j.voucher_no ASC
    """, (date_from, date_from, date_to, date_to))


def get_bank_book(account_code: str, date_from: str, date_to: str) -> List[Dict]:
    """Bank/Cash book for a specific account."""
    return _q("""
        SELECT
            j.voucher_date::text    AS "Date",
            j.voucher_no            AS "Voucher No",
            j.voucher_type          AS "Type",
            j.narration             AS "Particulars",
            l.debit                 AS "Receipts (₹)",
            l.credit                AS "Payments (₹)",
            bt.ref_no               AS "Bank Ref",
            bt.is_reconciled        AS "Reconciled"
        FROM journal_lines     l
        JOIN journal_entries   j  ON j.id = l.journal_id
        JOIN chart_of_accounts a  ON a.id = l.account_id
        LEFT JOIN bank_transactions bt ON bt.journal_id     = j.id
                                      AND bt.bank_account_id = l.account_id
        WHERE a.account_code = %s
          AND j.voucher_date BETWEEN %s AND %s
        ORDER BY j.voucher_date ASC, j.created_at ASC
    """, (account_code, date_from, date_to))


def get_all_vouchers(
    date_from: str, date_to: str,
    voucher_type: str = "",
    limit: int = 200,
) -> List[Dict]:
    tf = "AND j.voucher_type = %s" if voucher_type else ""
    params = (date_from, date_to, voucher_type, limit) if voucher_type \
             else (date_from, date_to, limit)
    return _q(f"""
        SELECT
            j.voucher_date::text    AS "Date",
            j.voucher_no            AS "Voucher No",
            j.voucher_type          AS "Type",
            j.narration             AS "Narration",
            j.total_debit           AS "Amount (₹)",
            j.ref_doc_no            AS "Ref Doc",
            j.is_auto_posted        AS "Auto",
            j.created_by            AS "User"
        FROM journal_entries j
        WHERE j.voucher_date BETWEEN %s AND %s {tf}
        ORDER BY j.voucher_date DESC, j.created_at DESC
        LIMIT %s
    """, params)


# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL — POST JVs FOR ALL EXISTING TRANSACTIONS
# ══════════════════════════════════════════════════════════════════════════════

def backfill_journal_entries(created_by: str = "Migration") -> Dict:
    """
    One-time backfill — create JVs for all existing invoices, payments,
    disbursements that were recorded before the accounting module was added.

    Safe to run multiple times — skips docs that already have a JV via ref_doc_id.

    Returns: {"invoices": n, "payments": n, "disbursements": n, "errors": [...]}
    """
    stats  = {
        "invoices": 0, "payments": 0, "disbursements": 0,
        "purchases": 0, "credit_notes": 0, "debit_notes": 0, "errors": []
    }

    # ── Already-posted ref_doc_ids (skip these) ───────────────────────────
    posted = {r["ref_doc_id"] for r in
              _q("SELECT ref_doc_id FROM journal_entries WHERE ref_doc_id IS NOT NULL")}

    # ── Backfill invoices ─────────────────────────────────────────────────
    # NOTE: order_ids may contain order numbers (PO-xxx), not UUIDs
    # Use challan_id FK for order_type lookup to avoid uuid cast error
    try:
      invoices = _q("""
        SELECT
            i.id::text                              AS id,
            i.invoice_no,
            COALESCE(p.party_name, '')              AS party_name,
            COALESCE(i.grand_total, 0)              AS grand_total,
            COALESCE(i.total_amount, 0)             AS taxable,
            COALESCE(i.total_tax, 0)                AS tax_amount,
            COALESCE(i.invoice_date, CURRENT_DATE)  AS invoice_date,
            -- Derive order_type from challan or default WHOLESALE
            COALESCE((
                SELECT o.order_type FROM challans c
                JOIN orders o ON o.id::text = ANY(c.order_ids)
                WHERE c.id = i.challan_id LIMIT 1
            ), 'WHOLESALE') AS order_type
        FROM invoices i
        LEFT JOIN parties p ON p.id = i.party_id
        WHERE COALESCE(i.is_deleted, FALSE) = FALSE
          AND UPPER(COALESCE(i.status, '')) != 'CANCELLED'
        ORDER BY i.invoice_date
    """)
    except Exception as _inv_q_err:
        stats["errors"].append(f"Invoice query failed: {_inv_q_err}")
        invoices = []
    for inv in invoices:
        if str(inv.get("id","")) in posted:
            continue
        if float(inv.get("grand_total") or 0) <= 0:
            continue   # skip zero-value invoices
        try:
            ok, _ = post_invoice_jv(
                invoice_no   = inv["invoice_no"] or "",
                invoice_id   = inv["id"],
                party_name   = inv["party_name"],
                grand_total  = float(inv["grand_total"] or 0),
                taxable      = float(inv["taxable"] or 0),
                tax_amount   = float(inv["tax_amount"] or 0),
                order_type   = inv["order_type"] or "WHOLESALE",
                voucher_date = inv["invoice_date"],
                created_by   = created_by,
            )
            if ok: stats["invoices"] += 1
        except Exception as e:
            stats["errors"].append(f"Invoice {inv.get('invoice_no')}: {e}")

    # ── Backfill payment receipts ─────────────────────────────────────────
    # is_cancelled column added by reversal migration — may not exist yet
    _has_cancelled = bool(_q("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name='payments' AND column_name='is_cancelled' LIMIT 1
    """))
    _cancelled_filter = "AND NOT COALESCE(is_cancelled, FALSE)" if _has_cancelled else ""

    payments = _q(f"""
        SELECT id::text, payment_no, party_name,
               amount, payment_mode, payment_date, payment_type
        FROM payments
        WHERE COALESCE(is_deleted, FALSE) = FALSE
          AND payment_type IN ('PAYMENT', 'RECEIPT', 'ADVANCE', 'OPENING')
          {_cancelled_filter}
    """)
    for pay in payments:
        if str(pay.get("id","")) in posted:
            continue
        if float(pay.get("amount") or 0) <= 0:
            continue   # skip zero-amount payments
        try:
            ok, _ = post_payment_receipt_jv(
                payment_no   = pay["payment_no"] or "",
                payment_id   = pay["id"],
                party_name   = pay["party_name"] or "",
                amount       = float(pay["amount"] or 0),
                payment_mode = pay["payment_mode"] or "CASH",
                bank_account = "",
                voucher_date = pay["payment_date"],
                created_by   = created_by,
            )
            if ok: stats["payments"] += 1
        except Exception as e:
            stats["errors"].append(f"Payment {pay.get('payment_no')}: {e}")

    # ── Backfill disbursements ────────────────────────────────────────────
    disbs = _q("""
        SELECT id::text, payment_no, party_name,
               amount, payment_mode, payment_date, remarks
        FROM payments
        WHERE COALESCE(is_deleted, FALSE)  = FALSE
          AND payment_type = 'DISBURSEMENT'
    """)
    for disb in disbs:
        if str(disb.get("id","")) in posted:
            continue
        try:
            ok, _ = post_disbursement_jv(
                payment_no   = disb["payment_no"] or "",
                payment_id   = disb["id"],
                payee        = disb["party_name"] or "Misc",
                amount       = float(disb["amount"] or 0),
                category     = "EXPENSE",
                payment_mode = disb["payment_mode"] or "CASH",
                voucher_date = disb["payment_date"],
                created_by   = created_by,
            )
            if ok: stats["disbursements"] += 1
        except Exception as e:
            stats["errors"].append(f"Disbursement {disb.get('payment_no')}: {e}")

    # ── Backfill supplier purchase invoices ───────────────────────────────
    try:
        purchases = _q("""
            SELECT
                COALESCE(NULLIF(invoice_no, ''), supplier_invoice_no, '') AS invoice_no,
                COALESCE(NULLIF(invoice_no, ''), supplier_invoice_no, '') AS id,
                COALESCE(NULLIF(supplier_name, ''), 'Unknown Supplier') AS supplier_name,
                COALESCE(invoice_total, 0) AS grand_total,
                COALESCE(subtotal, 0) AS taxable,
                COALESCE(gst_amount, 0) AS tax_amount,
                COALESCE(invoice_date, CURRENT_DATE) AS invoice_date,
                'LENSES' AS purchase_category
            FROM purchase_invoices
            WHERE COALESCE(is_deleted, FALSE) = FALSE
              AND COALESCE(invoice_total, 0) > 0
            ORDER BY invoice_date
        """)
    except Exception as _pur_q_err:
        stats["errors"].append(f"Purchase query failed: {_pur_q_err}")
        purchases = []

    for pur in purchases:
        pur_id = str(pur.get("id") or pur.get("invoice_no") or "")
        if not pur_id or pur_id in posted:
            continue
        try:
            ok, _ = post_purchase_invoice_jv(
                invoice_no=pur.get("invoice_no") or pur_id,
                invoice_id=pur_id,
                supplier_name=pur.get("supplier_name") or "Unknown Supplier",
                grand_total=float(pur.get("grand_total") or 0),
                taxable=float(pur.get("taxable") or 0),
                tax_amount=float(pur.get("tax_amount") or 0),
                purchase_category=pur.get("purchase_category") or "LENSES",
                voucher_date=pur.get("invoice_date"),
                created_by=created_by,
            )
            if ok:
                stats["purchases"] += 1
            else:
                stats["errors"].append(f"Purchase {pur.get('invoice_no')}: {_}")
        except Exception as e:
            stats["errors"].append(f"Purchase {pur.get('invoice_no')}: {e}")

    # ── Backfill credit notes / debit notes ───────────────────────────────
    try:
        credit_notes = _q("""
            SELECT id::text, cn_number, party_name,
                   COALESCE(grand_total, 0) AS grand_total,
                   COALESCE(taxable_amount, 0) AS taxable_amount,
                   COALESCE(cgst_amount, 0) AS cgst_amount,
                   COALESCE(sgst_amount, 0) AS sgst_amount,
                   COALESCE(igst_amount, 0) AS igst_amount,
                   COALESCE(cn_date, CURRENT_DATE) AS note_date
            FROM credit_notes
            WHERE COALESCE(is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(status, '')) != 'CANCELLED'
              AND COALESCE(grand_total, 0) > 0
            ORDER BY cn_date
        """)
    except Exception:
        credit_notes = []
    for cn in credit_notes:
        cn_id = str(cn.get("id") or "")
        if not cn_id or cn_id in posted:
            continue
        ok, msg = post_credit_note_jv(
            cn_number=cn.get("cn_number") or "",
            cn_id=cn_id,
            party_name=cn.get("party_name") or "",
            grand_total=float(cn.get("grand_total") or 0),
            taxable=float(cn.get("taxable_amount") or 0),
            cgst_amount=float(cn.get("cgst_amount") or 0),
            sgst_amount=float(cn.get("sgst_amount") or 0),
            igst_amount=float(cn.get("igst_amount") or 0),
            voucher_date=cn.get("note_date"),
            created_by=created_by,
        )
        if ok:
            stats["credit_notes"] += 1
        else:
            stats["errors"].append(f"Credit Note {cn.get('cn_number')}: {msg}")

    try:
        debit_notes = _q("""
            SELECT id::text, dn_number, party_name,
                   COALESCE(grand_total, 0) AS grand_total,
                   COALESCE(taxable_amount, 0) AS taxable_amount,
                   COALESCE(cgst_amount, 0) AS cgst_amount,
                   COALESCE(sgst_amount, 0) AS sgst_amount,
                   COALESCE(igst_amount, 0) AS igst_amount,
                   COALESCE(dn_date, CURRENT_DATE) AS note_date
            FROM debit_notes
            WHERE COALESCE(is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(status, '')) != 'CANCELLED'
              AND COALESCE(grand_total, 0) > 0
            ORDER BY dn_date
        """)
    except Exception:
        debit_notes = []
    for dn in debit_notes:
        dn_id = str(dn.get("id") or "")
        if not dn_id or dn_id in posted:
            continue
        ok, msg = post_debit_note_jv(
            dn_number=dn.get("dn_number") or "",
            dn_id=dn_id,
            party_name=dn.get("party_name") or "",
            grand_total=float(dn.get("grand_total") or 0),
            taxable=float(dn.get("taxable_amount") or 0),
            cgst_amount=float(dn.get("cgst_amount") or 0),
            sgst_amount=float(dn.get("sgst_amount") or 0),
            igst_amount=float(dn.get("igst_amount") or 0),
            voucher_date=dn.get("note_date"),
            created_by=created_by,
        )
        if ok:
            stats["debit_notes"] += 1
        else:
            stats["errors"].append(f"Debit Note {dn.get('dn_number')}: {msg}")

    stats["total"] = (
        stats["invoices"] + stats["payments"] + stats["disbursements"]
        + stats["purchases"] + stats["credit_notes"] + stats["debit_notes"]
    )
    return stats
