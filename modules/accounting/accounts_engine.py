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
    try:
        from modules.sql_adapter import run_transaction
        run_transaction(steps)
        return True, None
    except Exception:
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
            CONSTRAINT jv_balanced CHECK (ABS(total_debit - total_credit) < 0.01)
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
    ("2001", "Sundry Debtors",          "Sundry Debtors",     "ASSET",     "PARTY"),
    ("2002", "Sundry Creditors",        "Sundry Creditors",   "LIABILITY", "PARTY"),
    ("3001", "Sales - Retail",          "Direct Income",      "INCOME",    "SALES"),
    ("3002", "Sales - Wholesale",       "Direct Income",      "INCOME",    "SALES"),
    ("3003", "Sales - Contact Lens",    "Direct Income",      "INCOME",    "SALES"),
    ("3004", "Consultation Fees",       "Direct Income",      "INCOME",    "SALES"),
    ("3005", "Other Income",            "Indirect Income",    "INCOME",    "OTHER"),
    ("4001", "Purchase - Frames",       "Purchase Accounts",  "EXPENSE",   "PURCHASE"),
    ("4002", "Purchase - Lenses",       "Purchase Accounts",  "EXPENSE",   "PURCHASE"),
    ("4003", "Purchase - Contact Lens", "Purchase Accounts",  "EXPENSE",   "PURCHASE"),
    ("4004", "Purchase - Accessories",  "Purchase Accounts",  "EXPENSE",   "PURCHASE"),
    ("5001", "Salaries",                "Direct Expenses",    "EXPENSE",   "EXPENSE"),
    ("5002", "Rent",                    "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5003", "Electricity",             "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5004", "Telephone & Internet",    "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5005", "Courier & Transport",     "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
    ("5006", "Miscellaneous Expense",   "Indirect Expenses",  "EXPENSE",   "EXPENSE"),
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

    total_dr = round(sum(float(l.get("debit",  0) or 0) for l in lines), 2)
    total_cr = round(sum(float(l.get("credit", 0) or 0) for l in lines), 2)

    if abs(total_dr - total_cr) > 0.01:
        return False, "", f"Journal not balanced — Dr ₹{total_dr:,.2f} ≠ Cr ₹{total_cr:,.2f}"

    # Resolve account IDs
    resolved = []
    for line in lines:
        acc = get_account(line.get("account_code") or line.get("account_name", ""))
        if not acc:
            return False, "", f"Account not found: {line.get('account_code') or line.get('account_name')}"
        resolved.append({**line, "account_id": acc["id"], "account_name": acc["account_name"]})

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

    ok, vno, err = post_journal(
        voucher_type="RECEIPT",
        voucher_date=voucher_date,
        narration=f"Payment received {payment_no} — {party_name} — {payment_mode}",
        lines=[
            {"account_code": acc_code, "debit": amount,  "credit": 0, "party_name": party_name},
            {"account_code": "2001",   "debit": 0, "credit": amount,  "party_name": party_name},
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
    # Map category to purchase account
    purchase_map = {
        "FRAMES":       "4001",
        "LENSES":       "4002",
        "CONTACT LENS": "4003",
        "ACCESSORIES":  "4004",
    }
    cat_upper    = purchase_category.upper() if purchase_category else ""
    purch_code   = next((v for k, v in purchase_map.items() if k in cat_upper), "4002")

    lines = [
        {"account_code": purch_code, "debit": taxable,     "credit": 0,           "party_name": supplier_name},
        {"account_code": "2002",     "debit": 0,           "credit": grand_total,  "party_name": supplier_name},
    ]
    # Only add GST input if there is tax
    if tax_amount > 0.01:
        lines.insert(1, {"account_code": "6001", "debit": tax_amount / 2, "credit": 0})  # CGST Input
        lines.insert(2, {"account_code": "6002", "debit": tax_amount / 2, "credit": 0})  # SGST Input
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
        SELECT
            a.account_code                          AS "Code",
            a.account_name                          AS "Account",
            g.name                                  AS "Group",
            a.nature                                AS "Nature",
            a.opening_balance                       AS "Opening (₹)",
            COALESCE(SUM(l.debit),  0)              AS "Period Dr (₹)",
            COALESCE(SUM(l.credit), 0)              AS "Period Cr (₹)",
            a.opening_balance
              + COALESCE(SUM(l.debit),  0)
              - COALESCE(SUM(l.credit), 0)          AS "Closing Dr (₹)",
            -(a.opening_balance
              + COALESCE(SUM(l.debit),  0)
              - COALESCE(SUM(l.credit), 0))         AS "Closing Cr (₹)"
        FROM chart_of_accounts a
        LEFT JOIN account_groups g ON g.id  = a.group_id
        LEFT JOIN journal_lines  l ON l.account_id = a.id
        LEFT JOIN journal_entries j ON j.id = l.journal_id
            AND j.voucher_date BETWEEN %s AND %s
        WHERE a.is_active = TRUE
        GROUP BY a.account_code, a.account_name, g.name,
                 a.nature, a.opening_balance
        ORDER BY a.account_code
    """, (date_from, date_to))


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
    stats  = {"invoices": 0, "payments": 0, "disbursements": 0, "errors": []}

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
          AND payment_type IN ('PAYMENT', 'RECEIPT', 'ADVANCE')
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

    stats["total"] = stats["invoices"] + stats["payments"] + stats["disbursements"]
    return stats
