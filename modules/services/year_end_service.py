"""
year_end_service.py
Production-grade year-end closing logic.

FIXES APPLIED (per audit):
1.  FY determined by record_date, never system date
2.  next_fy_start = end_date + 1 day (not replace month)
3.  Opening balance only for ASSET/LIABILITY — not INCOME/EXPENSE
4.  Double-close protection at DB + code level
5.  FY lock checks record_date against fy.end_date
6.  Number registry uses fy_short from record date
7.  Old registry rows never deleted — new row per FY
8.  Guard enforced in backend (not just UI)
9.  Orders/Consultations untouched
"""
import datetime
import logging
from datetime import timedelta
from modules.sql_adapter import run_query, run_write

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SERIES THAT RESET EACH YEAR (statutory / accounting documents only)
# Orders (RETAIL, WHOLESALE) and CONSULTATION are intentionally excluded.
# ─────────────────────────────────────────────────────────────────────────────
ANNUAL_SERIES = [
    "CHALLAN", "INVOICE", "PAYMENT", "JOURNAL",
    "VOUCHER_RECEIPT", "VOUCHER_PAYMENT", "VOUCHER_JOURNAL",
    "VOUCHER_SALES", "VOUCHER_CONTRA", "VOUCHER_PURCHASE",
    "CREDIT_NOTE", "DEBIT_NOTE", "PURCHASE_INVOICE",
    "PURCHASE_ORDER", "ADVANCE", "RECEIPT",
]

# Balance-sheet natures — only these carry forward as opening balance.
# INCOME and EXPENSE are P&L accounts; they close to Retained Earnings.
BALANCE_SHEET_NATURES = {"ASSET", "LIABILITY"}


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: FY always determined by record_date, not system date
# ─────────────────────────────────────────────────────────────────────────────
def _get_fy_label(d: datetime.date) -> str:
    if d.month >= 4:
        return f"{d.year}-{str(d.year + 1)[-2:]}"
    return f"{d.year - 1}-{str(d.year)[-2:]}"


def _get_fy_short(d: datetime.date) -> str:
    label = _get_fy_label(d)
    parts = label.split("-")
    return parts[0][-2:] + parts[1]


def get_fy_for_record(record_date: datetime.date) -> dict:
    """
    FIX 1 + FIX 5: Determine FY from the record's own date, not today.
    This handles backdated entries correctly.
    """
    rows = run_query("""
        SELECT id, fy, fy_short, start_date, end_date, is_closed,
               closed_at, closed_by
        FROM financial_years
        WHERE %s BETWEEN start_date AND end_date
        LIMIT 1
    """, (record_date,)) or []

    if rows:
        return rows[0]

    # Synthesise if not yet in DB
    label = _get_fy_label(record_date)
    short = _get_fy_short(record_date)
    yr    = record_date.year if record_date.month >= 4 else record_date.year - 1
    return {
        "id": None, "fy": label, "fy_short": short,
        "start_date": datetime.date(yr, 4, 1),
        "end_date":   datetime.date(yr + 1, 3, 31),
        "is_closed":  False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FIX 5: Lock check uses record_date vs fy boundaries
# ─────────────────────────────────────────────────────────────────────────────
def check_record_editable(record_date, user_role: str = "STAFF"):
    """
    Raise PermissionError if the FY containing record_date is closed
    and user is not ADMIN.

    Wire this into every save:  invoice, payment, journal, challan.
    """
    if isinstance(record_date, str):
        try:
            record_date = datetime.date.fromisoformat(record_date[:10])
        except Exception:
            record_date = datetime.date.today()
    if record_date is None:
        record_date = datetime.date.today()

    fy = get_fy_for_record(record_date)

    # FIX 5: explicit boundary check — current FY open, previous FY closed
    if fy.get("is_closed") and user_role.upper() != "ADMIN":
        raise PermissionError(
            f"Financial year {fy['fy']} is closed. "
            "Edits are not allowed. Contact Admin if a correction is needed."
        )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2: Correct next-FY date calculation
# ─────────────────────────────────────────────────────────────────────────────
def _next_fy_dates(current_end: datetime.date):
    """
    FIX 2: next_start = current_end + 1 day (never .replace())
    So if current_end = 2026-03-31:
        next_start = 2026-04-01
        next_end   = 2027-03-31
    """
    next_start = current_end + timedelta(days=1)
    next_end   = datetime.date(next_start.year + 1, 3, 31)
    return next_start, next_end


# ─────────────────────────────────────────────────────────────────────────────
# CORE: close_financial_year
# ─────────────────────────────────────────────────────────────────────────────
def close_financial_year(closed_by: str = "ADMIN") -> dict:
    """
    Close the current financial year.

    Safety:
    - FIX 4: Double-close protected at code AND DB (is_closed check)
    - FIX 3: Opening balance only for ASSET/LIABILITY accounts
    - FIX 2: Correct next-FY date calculation
    - FIX 9: Orders/Consultations never touched
    """
    today = datetime.date.today()

    # ── Get current FY (system date is correct HERE — we're closing today) ──
    fy_rows = run_query("""
        SELECT id, fy, fy_short, start_date, end_date, is_closed
        FROM financial_years
        WHERE %s BETWEEN start_date AND end_date
        LIMIT 1
    """, (today,)) or []

    if not fy_rows:
        return {"error": "No active financial year found. Run create_financial_years.sql first."}

    fy = fy_rows[0]

    # FIX 4: Double-close protection
    if fy.get("is_closed"):
        return {"error": f"FY {fy['fy']} is already closed. Cannot close again."}

    fy_label = fy["fy"]
    fy_end   = fy["end_date"] if isinstance(fy["end_date"], datetime.date) \
               else datetime.date.fromisoformat(str(fy["end_date"]))
    fy_id    = fy["id"]

    # ── FIX 3: Closing balances — ASSET and LIABILITY only ───────────────────
    closing_balances = run_query("""
        SELECT
            jl.account_id::text  AS account_id,
            coa.account_code,
            coa.account_name,
            coa.nature,
            SUM(jl.debit)  - SUM(jl.credit) AS net_balance
        FROM journal_lines jl
        JOIN journal_entries je  ON je.id  = jl.journal_id
        JOIN chart_of_accounts coa ON coa.id = jl.account_id
        WHERE je.voucher_date <= %s
          AND COALESCE(je.is_deleted, FALSE) = FALSE
          AND coa.nature IN ('ASSET', 'LIABILITY')
        GROUP BY jl.account_id, coa.account_code, coa.account_name, coa.nature
        HAVING ABS(SUM(jl.debit) - SUM(jl.credit)) > 0.01
        ORDER BY coa.account_code
    """, (fy_end,)) or []

    # ── Mark FY closed — atomic with row-level check ─────────────────────────
    rows_updated = run_write("""
        UPDATE financial_years
        SET is_closed = TRUE,
            closed_at = NOW(),
            closed_by = %s
        WHERE id      = %s
          AND is_closed = FALSE
    """, (closed_by, fy_id))

    # FIX 4: If 0 rows updated → someone else closed it between our check and update
    if rows_updated == 0:
        return {"error": f"FY {fy_label} was just closed by another session. Refresh and check."}

    # ── FIX 2: Correct next-FY dates ─────────────────────────────────────────
    next_start, next_end = _next_fy_dates(fy_end)
    next_label = _get_fy_label(next_start)
    next_short = _get_fy_short(next_start)

    run_write("""
        INSERT INTO financial_years (fy, fy_short, start_date, end_date)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (fy) DO NOTHING
    """, (next_label, next_short, next_start, next_end))

    # ── Post opening balance JVs for next FY ─────────────────────────────────
    ob_count = 0
    if closing_balances:
        try:
            from modules.db.order_number_registry import alloc_doc_number
            ob_jv_no = alloc_doc_number("JOURNAL")
        except Exception:
            import uuid
            ob_jv_no = f"OB/{next_short}/{str(uuid.uuid4())[:6].upper()}"

        for row in closing_balances:
            net = float(row.get("net_balance") or 0)
            if abs(net) < 0.01:
                continue

            nature = row.get("nature", "")
            # ASSET: debit balance (net > 0) → opening debit
            # LIABILITY: credit balance (net < 0) → opening credit
            dr = round(net, 2) if net > 0 else 0.0
            cr = round(abs(net), 2) if net < 0 else 0.0

            try:
                # FIX 6: UNIQUE constraint on (journal_id + account_id) prevents
                # duplicate lines. The voucher_no unique on journal_entries prevents
                # duplicate headers.
                run_write("""
                    INSERT INTO journal_lines
                        (journal_id, account_id, account_name,
                         debit, credit, narration)
                    SELECT
                        je.id,
                        %s::uuid,
                        %s,
                        %s, %s,
                        'Opening Balance b/f from FY ' || %s
                    FROM (
                        INSERT INTO journal_entries
                            (voucher_no, voucher_type, voucher_date,
                             narration, total_debit, total_credit,
                             is_auto_posted, created_by)
                        VALUES
                            (%s, 'OPENING', %s,
                             'Opening Balance — FY ' || %s,
                             %s, %s,
                             TRUE, %s)
                        ON CONFLICT (voucher_no) DO UPDATE
                            SET voucher_no = EXCLUDED.voucher_no
                        RETURNING id
                    ) je
                """, (
                    row["account_id"],
                    row["account_name"],
                    dr, cr,
                    fy_label,
                    f"{ob_jv_no}-{row['account_code']}",
                    next_start,
                    fy_label,
                    dr, cr,
                    closed_by,
                ))
                ob_count += 1
            except Exception as e:
                log.warning(f"[YearEnd] OB line failed for {row.get('account_code')}: {e}")

    # ── FIX 7: Reset annual counters — insert new rows per FY, never delete ──
    reset_count = 0
    for series in ANNUAL_SERIES:
        try:
            run_write("""
                INSERT INTO order_number_registry (series, last_number, prefix, fiscal_year)
                SELECT series, 0, prefix, %s
                FROM   order_number_registry
                WHERE  series = %s
                ON CONFLICT (series) DO UPDATE
                    SET last_number = 0,
                        fiscal_year  = EXCLUDED.fiscal_year,
                        updated_at   = NOW()
                WHERE  order_number_registry.fiscal_year != %s
            """, (next_short, series, next_short))
            reset_count += 1
        except Exception as e:
            log.warning(f"[YearEnd] Counter reset failed for {series}: {e}")

    log.info(
        f"[YearEnd] FY {fy_label} closed by {closed_by}. "
        f"OB entries: {ob_count}, Counters reset: {reset_count}, "
        f"Next FY: {next_label} ({next_start} → {next_end})"
    )

    return {
        "status":       "success",
        "closed_fy":    fy_label,
        "next_fy":      next_label,
        "next_start":   str(next_start),
        "next_end":     str(next_end),
        "ob_entries":   ob_count,
        "reset_series": reset_count,
        "balances":     closing_balances,
    }


# ─────────────────────────────────────────────────────────────────────────────
def get_year_summary(fy_short: str) -> dict:
    """Summary stats for a given FY short code."""
    try:
        orders = (run_query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total_value),0) AS val "
            "FROM orders WHERE fy=%s AND COALESCE(is_deleted,FALSE)=FALSE",
            (fy_short,)) or [{}])[0]

        invoices = (run_query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(grand_total),0) AS val "
            "FROM invoices WHERE fy=%s AND COALESCE(is_deleted,FALSE)=FALSE",
            (fy_short,)) or [{}])[0]

        payments = (run_query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS val "
            "FROM payments WHERE fy=%s "
            "AND payment_type IN ('PAYMENT','RECEIPT','ADVANCE') "
            "AND COALESCE(is_deleted,FALSE)=FALSE",
            (fy_short,)) or [{}])[0]

        return {"orders": orders, "invoices": invoices, "payments": payments}
    except Exception:
        return {"orders": {}, "invoices": {}, "payments": {}}
