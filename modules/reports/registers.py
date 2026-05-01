"""
modules/reports/registers.py
==============================
Business Registers — Tally-equivalent day books and registers.

All registers share:
  - Date range filter (presets + custom)
  - Party / account filter
  - Daily / Monthly / Yearly grouping
  - Print + CSV export

Registers:
  1.  Sales Register         — invoices raised, line-wise
  2.  Purchase Register      — purchase invoices
  3.  Payment Receipt Book   — all receipts (party-wise, mode-wise)
  4.  Payment Disbursement   — all outgoing payments
  5.  Cash Book              — cash in / out daily
  6.  Bank Book              — bank account statement
  7.  Party Ledger           — individual party account
  8.  Debtors Register       — all debtors outstanding
  9.  Creditors Register     — all creditors outstanding
  10. Order Register         — all orders by party / status
  11. Challan Register       — all challans
  12. Stock Movement         — stock in / out
  13. Journal Register       — all JV entries
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta
import calendar


# ── Helpers ───────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _fmt(v):
    try: return f"₹{float(v or 0):,.2f}"
    except: return "₹0.00"


def _date_filter(key="reg", default_preset="This month"):
    presets = {
        "Today":         (date.today(), date.today()),
        "This week":     (date.today() - timedelta(days=date.today().weekday()), date.today()),
        "This month":    (date.today().replace(day=1), date.today()),
        "Last month":    ((date.today().replace(day=1) - timedelta(days=1)).replace(day=1),
                          date.today().replace(day=1) - timedelta(days=1)),
        "This quarter":  (date(date.today().year, ((date.today().month-1)//3)*3+1, 1), date.today()),
        "This year":     (date(date.today().year if date.today().month >= 4
                               else date.today().year - 1, 4, 1), date.today()),
        "All time":      (date(2020, 1, 1), date.today()),
    }
    c1, c2, c3 = st.columns([1, 1, 1])
    idx = list(presets.keys()).index(default_preset) if default_preset in presets else 2
    preset = c3.selectbox("Period", list(presets.keys()), index=idx, key=f"{key}_pre")
    df, dt = presets[preset]
    fd = c1.date_input("From", value=df, key=f"{key}_fd")
    td = c2.date_input("To",   value=dt, key=f"{key}_td")
    return fd, td


def _party_filter(key="reg", label="All Parties", include_patients=False):
    """Filter box + selectbox — always visible."""
    @st.cache_data(ttl=120, show_spinner=False)
    def _load():
        rows = _q("SELECT party_name FROM parties WHERE COALESCE(is_active,TRUE)=TRUE ORDER BY party_name")
        names = [r["party_name"] for r in rows]
        if include_patients:
            pts = _q("SELECT COALESCE(master_name,'') AS party_name FROM patients ORDER BY master_name")
            seen = set(names)
            names += [r["party_name"] for r in pts if r["party_name"] not in seen]
        return names
    all_names = _load()

    def _on_change():
        st.session_state[f"{key}_fterm"] = st.session_state.get(f"{key}_finput", "")

    st.text_input("🔍 Filter party", key=f"{key}_finput", placeholder="Type to filter…",
                  on_change=_on_change)
    term = st.session_state.get(f"{key}_fterm", "")
    filtered = [n for n in all_names if term.lower() in n.lower()] if term else all_names
    opts = [f"— {label} ({len(filtered)}) —"] + filtered
    chosen = st.selectbox("Party", opts, key=f"{key}_party_sel")
    return "" if chosen.startswith("—") else chosen


def _grouping(key="reg"):
    return st.radio("Group by", ["Detail","Daily","Monthly","Yearly"],
                    horizontal=True, key=f"{key}_grp")


def _export(df, title, key):
    st.download_button(f"⬇ Export {title}",
        df.to_csv(index=False).encode(),
        file_name=f"{title.replace(' ','_')}.csv",
        mime="text/csv", key=key)


def _metrics(*args):
    cols = st.columns(len(args))
    for col, (label, value) in zip(cols, args):
        col.metric(label, value)


def _apply_grouping(df, grouping, dr_col, cr_col, date_col="Date"):
    """Collapse detail rows into daily/monthly/yearly summary."""
    if grouping == "Detail" or date_col not in df.columns:
        return df

    df = df.copy()
    try:
        df["_dt"] = pd.to_datetime(df[date_col], errors="coerce")
        if grouping == "Daily":
            df["Period"] = df["_dt"].dt.strftime("%Y-%m-%d")
        elif grouping == "Monthly":
            df["Period"] = df["_dt"].dt.strftime("%Y-%m")
        else:
            df["Period"] = df["_dt"].dt.year.astype(str)

        agg = {"Entries": (dr_col, "count")}
        if dr_col in df.columns: agg["Dr (₹)"] = (dr_col, "sum")
        if cr_col in df.columns: agg["Cr (₹)"] = (cr_col, "sum")
        grp = df.groupby("Period").agg(**agg).reset_index()
        if "Dr (₹)" in grp and "Cr (₹)" in grp:
            grp["Net (₹)"] = grp["Dr (₹)"] - grp["Cr (₹)"]
        return grp.sort_values("Period")
    except Exception:
        return df


# ══════════════════════════════════════════════════════════════════════════════
# 1. SALES REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_sales_register():
    st.caption("All invoices raised — line-wise with GST breakup")
    fd, td   = _date_filter("sr")
    party    = _party_filter("sr", "All Parties")
    grouping = _grouping("sr")

    pf  = "AND p.party_name = %(pty)s" if party else ""
    rows = _q(f"""
        SELECT
            i.invoice_date::text        AS "Date",
            i.invoice_no                AS "Invoice No",
            COALESCE(p.party_name, '')  AS "Party",
            COALESCE(p.city,'')         AS "City",
            COALESCE(p.gstin,'')        AS "GSTIN",
            ROUND(i.total_amount, 2)    AS "Taxable (₹)",
            ROUND(i.total_tax/2, 2)     AS "CGST (₹)",
            ROUND(i.total_tax/2, 2)     AS "SGST (₹)",
            ROUND(i.total_tax, 2)       AS "Total Tax (₹)",
            ROUND(i.grand_total, 2)     AS "Invoice Amt (₹)",
            ROUND(COALESCE((
                SELECT SUM(pm.amount) FROM payments pm
                WHERE pm.invoice_id = i.id
                  AND NOT COALESCE(pm.is_deleted,FALSE)
            ), 0), 2)                   AS "Paid (₹)",
            ROUND(GREATEST(i.grand_total - COALESCE((
                SELECT SUM(pm.amount) FROM payments pm
                WHERE pm.invoice_id = i.id
                  AND NOT COALESCE(pm.is_deleted,FALSE)
            ), 0), 0), 2)               AS "Balance (₹)",
            COALESCE(i.payment_status,'UNPAID') AS "Status"
        FROM invoices i
        LEFT JOIN parties p ON p.id = i.party_id
        WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND UPPER(COALESCE(i.status,'')) != 'CANCELLED'
          {pf}
        ORDER BY i.invoice_date DESC, i.invoice_no
    """, {"fd": fd, "td": td, "pty": party})

    if not rows:
        st.info("No invoices in this period."); return

    df = _df(rows)
    for c in ["Taxable (₹)","CGST (₹)","SGST (₹)","Total Tax (₹)","Invoice Amt (₹)","Paid (₹)","Balance (₹)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    _metrics(
        ("Invoices",    str(len(df))),
        ("Taxable",     _fmt(df["Taxable (₹)"].sum())),
        ("Total Tax",   _fmt(df["Total Tax (₹)"].sum())),
        ("Invoice Amt", _fmt(df["Invoice Amt (₹)"].sum())),
        ("Collected",   _fmt(df["Paid (₹)"].sum())),
        ("Outstanding", _fmt(df["Balance (₹)"].sum())),
    )

    display = _apply_grouping(df, grouping, "Invoice Amt (₹)", "Paid (₹)")
    st.dataframe(display, width='stretch', hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in display.select_dtypes("number").columns})
    _export(df, f"Sales_Register_{fd}_{td}", "sr_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 2. PURCHASE REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_purchase_register():
    st.caption("All purchase invoices — from procurement")
    fd, td   = _date_filter("pr")
    party    = _party_filter("pr", "All Suppliers")
    grouping = _grouping("pr")

    pf = "AND s.party_name ILIKE %(pty)s" if party else ""

    # Try purchase_invoices table first; fallback to disbursement payments
    rows = []
    try:
        rows = _q(f"""
            SELECT
                pi.invoice_date::text        AS "Date",
                pi.invoice_no                AS "Invoice No",
                COALESCE(s.party_name,'')    AS "Supplier",
                ROUND(pi.taxable_amount,2)   AS "Taxable (₹)",
                ROUND(pi.tax_amount,2)       AS "Tax (₹)",
                ROUND(pi.grand_total,2)      AS "Total (₹)",
                pi.payment_status            AS "Status"
            FROM purchase_invoices pi
            LEFT JOIN parties s ON s.id = pi.supplier_id
            WHERE pi.invoice_date BETWEEN %(fd)s AND %(td)s
              AND COALESCE(pi.is_deleted,FALSE) = FALSE
              {pf}
            ORDER BY pi.invoice_date DESC
        """, {"fd": fd, "td": td, "pty": f"%{party}%"})
    except Exception:
        rows = []  # table doesn't exist yet — use fallback below

    if not rows:
        rows = _q(f"""
            SELECT
                p.payment_date::text     AS "Date",
                p.payment_no             AS "Invoice No",
                COALESCE(p.party_name,'') AS "Supplier",
                0                        AS "Taxable (₹)",
                0                        AS "Tax (₹)",
                ROUND(p.amount,2)        AS "Total (₹)",
                'PAID'                   AS "Status"
            FROM payments p
            WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
              AND p.payment_type = 'DISBURSEMENT'
              AND COALESCE(p.is_deleted,FALSE) = FALSE
              {"AND p.party_name ILIKE %(pty)s" if party else ""}
            ORDER BY p.payment_date DESC
        """, {"fd": fd, "td": td, "pty": f"%{party}%"})
        if rows:
            st.caption("ℹ️ Showing disbursements (purchase_invoices table not found)")

    if not rows:
        st.info("No purchase invoices in this period."); return

    df = _df(rows)
    for c in ["Taxable (₹)","Tax (₹)","Total (₹)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    _metrics(
        ("Entries",   str(len(df))),
        ("Taxable",   _fmt(df["Taxable (₹)"].sum()) if "Taxable (₹)" in df.columns else "—"),
        ("Tax",       _fmt(df["Tax (₹)"].sum()) if "Tax (₹)" in df.columns else "—"),
        ("Total",     _fmt(df["Total (₹)"].sum())),
    )
    display = _apply_grouping(df, grouping, "Total (₹)", "Taxable (₹)")
    st.dataframe(display, width='stretch', hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in display.select_dtypes("number").columns})
    _export(df, f"Purchase_Register_{fd}_{td}", "pr_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 3. PAYMENT RECEIPT BOOK
# ══════════════════════════════════════════════════════════════════════════════

def render_payment_receipt_book():
    st.caption("All money received — party-wise, mode-wise")
    fd, td   = _date_filter("prb")
    party    = _party_filter("prb", "All Parties", include_patients=True)
    grouping = _grouping("prb")
    mode_opts = ["All Modes","CASH","UPI","NEFT","CHEQUE","RTGS","CARD","OTHER"]
    mode     = st.selectbox("Payment Mode", mode_opts, key="prb_mode")

    pf  = "AND p.party_name ILIKE %(pty)s" if party else ""
    mf  = "" if mode == "All Modes" else "AND p.payment_mode = %(mode)s"

    rows = _q(f"""
        SELECT
            p.payment_date::text         AS "Date",
            p.payment_no                 AS "Receipt No",
            COALESCE(p.party_name,'')    AS "Party",
            p.payment_mode               AS "Mode",
            COALESCE(p.reference_no,'')  AS "Ref / UTR",
            ROUND(p.amount, 2)           AS "Amount (₹)",
            COALESCE(i.invoice_no,'—')   AS "Against Invoice",
            COALESCE(c.challan_no,'—')   AS "Against Challan",
            COALESCE(p.remarks,'')       AS "Narration"
        FROM payments p
        LEFT JOIN invoices i ON i.id = p.invoice_id
        LEFT JOIN challans c ON c.id = p.challan_id
        WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
          AND p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE')
          AND NOT COALESCE(p.is_deleted,FALSE)
          {pf} {mf}
        ORDER BY p.payment_date DESC, p.payment_no
    """, {"fd": fd, "td": td, "pty": f"%{party}%", "mode": mode})

    if not rows:
        st.info("No receipts in this period."); return

    df = _df(rows)
    df["Amount (₹)"] = pd.to_numeric(df["Amount (₹)"], errors="coerce").fillna(0)

    # Mode breakdown
    if "Mode" in df.columns:
        mode_sum = df.groupby("Mode")["Amount (₹)"].sum()
        cols = st.columns(min(len(mode_sum), 5))
        for i, (m, v) in enumerate(mode_sum.items()):
            cols[i % len(cols)].metric(m, _fmt(v))
        st.markdown("---")

    _metrics(
        ("Receipts",      str(len(df))),
        ("Total Received",_fmt(df["Amount (₹)"].sum())),
    )
    display = _apply_grouping(df, grouping, "Amount (₹)", "Amount (₹)")
    st.dataframe(display, width='stretch', hide_index=True,
        column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")})
    _export(df, f"Receipt_Book_{fd}_{td}", "prb_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 4. PAYMENT DISBURSEMENT BOOK
# ══════════════════════════════════════════════════════════════════════════════

def render_disbursement_book():
    st.caption("All outgoing payments — supplier payments and expenses")
    fd, td   = _date_filter("db")
    party    = _party_filter("db", "All Payees")
    grouping = _grouping("db")
    cat_opts = ["All","SUPPLIER","EXPENSE","SALARY","RENT","ELECTRICITY","OTHER"]
    cat      = st.selectbox("Category", cat_opts, key="db_cat")

    pf = "AND p.party_name ILIKE %(pty)s" if party else ""
    cf = "" if cat == "All" else "AND UPPER(COALESCE(p.remarks,'')) LIKE %(cat)s"

    rows = _q(f"""
        SELECT
            p.payment_date::text         AS "Date",
            p.payment_no                 AS "Voucher No",
            COALESCE(p.party_name,'')    AS "Payee",
            p.payment_mode               AS "Mode",
            COALESCE(p.reference_no,'')  AS "Ref",
            ROUND(p.amount,2)            AS "Amount (₹)",
            COALESCE(p.remarks,'')       AS "Narration"
        FROM payments p
        WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
          AND p.payment_type = 'DISBURSEMENT'
          AND NOT COALESCE(p.is_deleted,FALSE)
          {pf} {cf}
        ORDER BY p.payment_date DESC
    """, {"fd": fd, "td": td, "pty": f"%{party}%", "cat": f"%{cat}%"})

    if not rows:
        st.info("No disbursements in this period."); return

    df = _df(rows)
    df["Amount (₹)"] = pd.to_numeric(df["Amount (₹)"], errors="coerce").fillna(0)

    _metrics(("Entries", str(len(df))), ("Total Paid Out", _fmt(df["Amount (₹)"].sum())))
    display = _apply_grouping(df, grouping, "Amount (₹)", "Amount (₹)")
    st.dataframe(display, width='stretch', hide_index=True,
        column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")})
    _export(df, f"Disbursement_Book_{fd}_{td}", "db_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 5. CASH BOOK
# ══════════════════════════════════════════════════════════════════════════════

def render_cash_book():
    st.caption("Cash receipts and payments — daily running balance")
    fd, td   = _date_filter("cb")
    grouping = _grouping("cb")

    rows = _q("""
        SELECT
            p.payment_date::text         AS "Date",
            p.payment_no                 AS "Ref No",
            COALESCE(p.party_name,'')    AS "Party",
            CASE WHEN p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE') THEN ROUND(p.amount,2) ELSE 0 END AS "Receipts (₹)",
            CASE WHEN p.payment_type = 'DISBURSEMENT' THEN ROUND(p.amount,2) ELSE 0 END AS "Payments (₹)",
            COALESCE(p.remarks,'')       AS "Narration"
        FROM payments p
        WHERE p.payment_date BETWEEN %s AND %s
          AND p.payment_mode = 'CASH'
          AND NOT COALESCE(p.is_deleted,FALSE)
        ORDER BY p.payment_date ASC, p.created_at ASC
    """, (fd, td))

    if not rows:
        st.info("No cash transactions in this period."); return

    df = _df(rows)
    for c in ["Receipts (₹)","Payments (₹)"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Running balance
    df["Balance (₹)"] = (df["Receipts (₹)"] - df["Payments (₹)"]).cumsum()

    total_in  = df["Receipts (₹)"].sum()
    total_out = df["Payments (₹)"].sum()
    _metrics(
        ("Cash In",  _fmt(total_in)),
        ("Cash Out", _fmt(total_out)),
        ("Balance",  _fmt(total_in - total_out)),
        ("Entries",  str(len(df))),
    )

    if grouping == "Detail":
        st.dataframe(df, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in ["Receipts (₹)","Payments (₹)","Balance (₹)"]})
    else:
        display = _apply_grouping(df, grouping, "Receipts (₹)", "Payments (₹)")
        st.dataframe(display, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in display.select_dtypes("number").columns})
    _export(df, f"Cash_Book_{fd}_{td}", "cb_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 6. BANK BOOK
# ══════════════════════════════════════════════════════════════════════════════

def render_bank_book():
    st.caption("Bank receipts and payments — UPI, NEFT, CHEQUE, RTGS")
    fd, td   = _date_filter("bb2")
    bank_modes = ["All Bank Modes","UPI","NEFT","CHEQUE","RTGS","CARD"]
    mode     = st.selectbox("Bank Mode", bank_modes, key="bb2_mode")
    grouping = _grouping("bb2")

    mf = "" if mode == "All Bank Modes" else "AND p.payment_mode = %(mode)s"

    rows = _q(f"""
        SELECT
            p.payment_date::text         AS "Date",
            p.payment_no                 AS "Ref No",
            COALESCE(p.party_name,'')    AS "Party",
            p.payment_mode               AS "Mode",
            COALESCE(p.reference_no,'')  AS "UTR / Cheque No",
            CASE WHEN p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE') THEN ROUND(p.amount,2) ELSE 0 END AS "Receipts (₹)",
            CASE WHEN p.payment_type = 'DISBURSEMENT' THEN ROUND(p.amount,2) ELSE 0 END AS "Payments (₹)",
            COALESCE(p.remarks,'')       AS "Narration"
        FROM payments p
        WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
          AND p.payment_mode != 'CASH'
          AND NOT COALESCE(p.is_deleted,FALSE)
          {mf}
        ORDER BY p.payment_date ASC, p.created_at ASC
    """, {"fd": fd, "td": td, "mode": mode})

    if not rows:
        st.info("No bank transactions in this period."); return

    df = _df(rows)
    for c in ["Receipts (₹)","Payments (₹)"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["Balance (₹)"] = (df["Receipts (₹)"] - df["Payments (₹)"]).cumsum()

    _metrics(
        ("Bank In",  _fmt(df["Receipts (₹)"].sum())),
        ("Bank Out", _fmt(df["Payments (₹)"].sum())),
        ("Balance",  _fmt(df["Receipts (₹)"].sum() - df["Payments (₹)"].sum())),
    )
    if grouping == "Detail":
        st.dataframe(df, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in ["Receipts (₹)","Payments (₹)","Balance (₹)"]})
    else:
        st.dataframe(_apply_grouping(df, grouping, "Receipts (₹)", "Payments (₹)"),
                     width='stretch', hide_index=True)
    _export(df, f"Bank_Book_{fd}_{td}", "bb2_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 7. PARTY LEDGER
# ══════════════════════════════════════════════════════════════════════════════

def render_party_ledger():
    st.caption("Individual party account — all DR/CR with running balance")
    fd, td = _date_filter("pl2")

    # Party selection — required
    @st.cache_data(ttl=120, show_spinner=False)
    def _all_parties():
        r = _q("SELECT party_name FROM parties WHERE COALESCE(is_active,TRUE)=TRUE ORDER BY party_name")
        return [x["party_name"] for x in r]

    def _on_chg():
        st.session_state["pl2_term"] = st.session_state.get("pl2_input","")

    st.text_input("🔍 Search party", key="pl2_input", placeholder="Type name…", on_change=_on_chg)
    term     = st.session_state.get("pl2_term","")
    names    = _all_parties()
    filtered = [n for n in names if term.lower() in n.lower()] if term else names
    placeholder = f"— Select Party ({len(filtered)}) —"
    chosen   = st.selectbox("Party *", [placeholder] + filtered, key="pl2_sel")

    if chosen == placeholder:
        st.info("Select a party to view its ledger.")
        return

    rows = _q("""
        SELECT
            pl.entry_date::text   AS "Date",
            pl.entry_type         AS "Type",
            pl.ref_no             AS "Ref No",
            ROUND(pl.debit,2)     AS "Dr (₹)",
            ROUND(pl.credit,2)    AS "Cr (₹)",
            pl.narration          AS "Narration",
            pl.created_by         AS "By"
        FROM party_ledger pl
        WHERE pl.party_name = %s
          AND pl.entry_date BETWEEN %s AND %s
        ORDER BY pl.entry_date ASC, pl.id ASC
    """, (chosen, fd, td))

    # Opening balance (before period)
    op_rows = _q("""
        SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c
        FROM party_ledger WHERE party_name=%s AND entry_date < %s
    """, (chosen, fd))
    op_dr = float((op_rows[0]["d"] if op_rows else 0) or 0)
    op_cr = float((op_rows[0]["c"] if op_rows else 0) or 0)
    opening = op_dr - op_cr

    if not rows:
        _metrics(("Opening Balance", _fmt(opening)), ("Period Entries", "0"), ("Closing", _fmt(opening)))
        st.info("No entries in this period.")
        return

    df = _df(rows)
    df["Dr (₹)"] = pd.to_numeric(df["Dr (₹)"], errors="coerce").fillna(0)
    df["Cr (₹)"] = pd.to_numeric(df["Cr (₹)"], errors="coerce").fillna(0)
    df["Balance (₹)"] = (df["Dr (₹)"] - df["Cr (₹)"]).cumsum() + opening

    total_dr  = df["Dr (₹)"].sum()
    total_cr  = df["Cr (₹)"].sum()
    closing   = opening + total_dr - total_cr

    _metrics(
        ("Opening",  _fmt(opening)),
        ("Period Dr",_fmt(total_dr)),
        ("Period Cr",_fmt(total_cr)),
        ("Closing",  _fmt(closing)),
    )

    st.markdown(
        f"<div style='background:{'#0a2a1a' if closing <= 0 else '#2a0a0a'};"
        f"border:1px solid {'#22c55e' if closing <= 0 else '#ef4444'};"
        f"border-radius:6px;padding:8px 14px;margin:8px 0'>"
        f"{'✅ Settled' if abs(closing) < 0.01 else ('Cr Balance: ' + _fmt(abs(closing))) if closing < 0 else ('Dr Balance (Receivable): ' + _fmt(closing))}"
        f"</div>", unsafe_allow_html=True)

    st.dataframe(df, width='stretch', hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Dr (₹)","Cr (₹)","Balance (₹)"]})
    _export(df, f"Ledger_{chosen}_{fd}_{td}", "pl2_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 8. DEBTORS REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_debtors_register():
    st.caption("All parties with outstanding receivables — invoice-wise")
    fd, td    = _date_filter("dr2", "All time")
    min_bal   = st.number_input("Min outstanding ₹", value=1.0, step=100.0, key="dr2_min")

    rows = _q("""
        SELECT * FROM (
            SELECT
                p.party_name                 AS "Party",
                COALESCE(p.mobile,'')        AS "Mobile",
                COALESCE(p.city,'')          AS "City",
                i.invoice_no                 AS "Invoice",
                i.invoice_date::text         AS "Invoice Date",
                ROUND(i.grand_total,2)       AS "Invoice Amt (₹)",
                ROUND(COALESCE((
                    SELECT SUM(pm.amount) FROM payments pm
                    WHERE pm.invoice_id = i.id
                      AND NOT COALESCE(pm.is_deleted,FALSE)
                ),0),2)                      AS "Paid (₹)",
                ROUND(GREATEST(i.grand_total - COALESCE((
                    SELECT SUM(pm.amount) FROM payments pm
                    WHERE pm.invoice_id = i.id
                      AND NOT COALESCE(pm.is_deleted,FALSE)
                ),0),0),2)                   AS "Balance (₹)",
                CASE
                    WHEN i.due_date IS NULL OR i.due_date >= CURRENT_DATE THEN 'Current'
                    WHEN (CURRENT_DATE - i.due_date) <= 30  THEN '1-30 days'
                    WHEN (CURRENT_DATE - i.due_date) <= 60  THEN '31-60 days'
                    WHEN (CURRENT_DATE - i.due_date) <= 90  THEN '61-90 days'
                    ELSE '90+ days'
                END                          AS "Aging"
            FROM invoices i
            JOIN parties p ON p.id = i.party_id
            WHERE COALESCE(i.is_deleted,FALSE) = FALSE
              AND UPPER(COALESCE(i.status,'')) != 'CANCELLED'
              AND i.invoice_date BETWEEN %s AND %s
        ) sub
        WHERE "Balance (₹)" >= %s
        ORDER BY "Party", "Invoice Date"
    """, (fd, td, min_bal))

    if not rows:
        st.success("✅ No outstanding debtors!"); return

    df = _df(rows)
    for c in ["Invoice Amt (₹)","Paid (₹)","Balance (₹)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Party summary
    party_sum = df.groupby("Party")["Balance (₹)"].sum().sort_values(ascending=False)
    _metrics(
        ("Parties",   str(len(party_sum))),
        ("Invoices",  str(len(df))),
        ("Total Due", _fmt(df["Balance (₹)"].sum())),
    )

    # Aging buckets
    bucket_order = ["Current","1-30 days","31-60 days","61-90 days","90+ days"]
    buckets = df.groupby("Aging")["Balance (₹)"].sum().reindex(bucket_order, fill_value=0)
    cols = st.columns(5)
    for i, (bk, amt) in enumerate(buckets.items()):
        cols[i].metric(bk, _fmt(amt))

    st.dataframe(df, width='stretch', hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Invoice Amt (₹)","Paid (₹)","Balance (₹)"]})
    _export(df, f"Debtors_Register_{fd}_{td}", "dr2_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 9. ORDER REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_order_register():
    st.caption("All orders — retail, wholesale, consultation")
    fd, td   = _date_filter("or2")
    party    = _party_filter("or2", "All Parties", include_patients=True)
    grouping = _grouping("or2")

    status_opts = ["All","PENDING","CONFIRMED","IN_PRODUCTION","READY","BILLED","DELIVERED","CLOSED","CANCELLED"]
    otype_opts  = ["All","RETAIL","WHOLESALE","CONSULTATION"]
    c1, c2 = st.columns(2)
    status = c1.selectbox("Status", status_opts, key="or2_st")
    otype  = c2.selectbox("Type",   otype_opts,  key="or2_type")

    pf = "AND COALESCE(o.party_name, o.patient_name,'') ILIKE %(pty)s" if party else ""
    sf = "" if status == "All" else "AND o.status = %(st)s"
    tf = "" if otype  == "All" else "AND o.order_type = %(ot)s"

    rows = _q(f"""
        SELECT
            o.created_at::date::text     AS "Date",
            o.order_no                   AS "Order No",
            o.order_type                 AS "Type",
            COALESCE(o.party_name, o.patient_name,'') AS "Party / Patient",
            o.total_items                AS "Items",
            ROUND(COALESCE(o.total_value,0),2) AS "Value (₹)",
            o.status                     AS "Status"
        FROM orders o
        WHERE o.created_at::date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(o.is_deleted,FALSE) = FALSE
          {pf} {sf} {tf}
        ORDER BY o.created_at DESC
        LIMIT 1000
    """, {"fd": fd, "td": td, "pty": f"%{party}%", "st": status, "ot": otype})

    if not rows:
        st.info("No orders in this period."); return

    df = _df(rows)
    if "Value (₹)" in df.columns:
        df["Value (₹)"] = pd.to_numeric(df["Value (₹)"], errors="coerce").fillna(0)

    _metrics(
        ("Orders",      str(len(df))),
        ("Total Value", _fmt(df["Value (₹)"].sum()) if "Value (₹)" in df.columns else "—"),
    )

    if grouping != "Detail":
        df["_dt"] = pd.to_datetime(df["Date"], errors="coerce")
        if   grouping == "Daily":   df["Period"] = df["_dt"].dt.strftime("%Y-%m-%d")
        elif grouping == "Monthly": df["Period"] = df["_dt"].dt.strftime("%Y-%m")
        else:                       df["Period"] = df["_dt"].dt.year.astype(str)
        display = df.groupby("Period").agg(
            Orders=("Order No","count"),
            **{"Value (₹)": ("Value (₹)","sum")}
        ).reset_index().sort_values("Period")
        st.dataframe(display, width='stretch', hide_index=True,
            column_config={"Value (₹)": st.column_config.NumberColumn(format="₹%.2f")})
    else:
        st.dataframe(df, width='stretch', hide_index=True,
            column_config={"Value (₹)": st.column_config.NumberColumn(format="₹%.2f")})
    _export(df, f"Order_Register_{fd}_{td}", "or2_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 10. JOURNAL REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_journal_register():
    st.caption("All journal vouchers — manual and auto-posted")
    fd, td   = _date_filter("jr")
    grouping = _grouping("jr")
    vtype_opts = ["All","SALES","RECEIPT","PAYMENT","JOURNAL","CONTRA","PURCHASE"]
    src_opts   = ["All","Auto-posted","Manual"]
    c1, c2 = st.columns(2)
    vtype  = c1.selectbox("Voucher Type", vtype_opts, key="jr_vtype")
    src    = c2.selectbox("Source",       src_opts,   key="jr_src")

    tf = "" if vtype == "All" else "AND j.voucher_type = %(vt)s"
    af = "" if src   == "All" else \
         "AND j.is_auto_posted = TRUE" if src == "Auto-posted" else \
         "AND j.is_auto_posted = FALSE"

    rows = _q(f"""
        SELECT
            j.voucher_date::text    AS "Date",
            j.voucher_no            AS "Voucher No",
            j.voucher_type          AS "Type",
            j.narration             AS "Narration",
            ROUND(j.total_debit,2)  AS "Dr (₹)",
            ROUND(j.total_credit,2) AS "Cr (₹)",
            j.ref_doc_no            AS "Ref Doc",
            j.created_by            AS "By",
            j.is_auto_posted        AS "Auto"
        FROM journal_entries j
        WHERE j.voucher_date BETWEEN %(fd)s AND %(td)s
          {tf} {af}
        ORDER BY j.voucher_date DESC, j.created_at DESC
        LIMIT 500
    """, {"fd": fd, "td": td, "vt": vtype})

    if not rows:
        st.info("No journal entries. Run Backfill in Accounts to generate."); return

    df = _df(rows)
    for c in ["Dr (₹)","Cr (₹)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    _metrics(
        ("Vouchers",     str(len(df))),
        ("Total Dr",     _fmt(df["Dr (₹)"].sum())),
        ("Total Cr",     _fmt(df["Cr (₹)"].sum())),
    )
    display = _apply_grouping(df, grouping, "Dr (₹)", "Cr (₹)")
    st.dataframe(display, width='stretch', hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in display.select_dtypes("number").columns
                       if c not in ["Auto"]})
    _export(df, f"Journal_Register_{fd}_{td}", "jr_dl")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════

def render_registers():
    st.markdown("## 📚 Registers")
    st.caption("Day books, account registers, and transaction logs")

    tabs = st.tabs([
        "🧾 Sales Register",
        "🛒 Purchase Register",
        "💵 Receipt Book",
        "💸 Disbursement Book",
        "💰 Cash Book",
        "🏦 Bank Book",
        "👤 Party Ledger",
        "📥 Debtors Register",
        "📦 Order Register",
        "📋 Journal Register",
    ])

    with tabs[0]: render_sales_register()
    with tabs[1]: render_purchase_register()
    with tabs[2]: render_payment_receipt_book()
    with tabs[3]: render_disbursement_book()
    with tabs[4]: render_cash_book()
    with tabs[5]: render_bank_book()
    with tabs[6]: render_party_ledger()
    with tabs[7]: render_debtors_register()
    with tabs[8]: render_order_register()
    with tabs[9]: render_journal_register()
