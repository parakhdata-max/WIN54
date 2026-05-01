"""
modules/accounting/accounts_ui.py
===================================
Tally-equivalent accounting UI for DV ERP.

Tabs:
  1. Chart of Accounts  — create / edit ledger accounts
  2. Journal Entry      — manual voucher (JV/Contra/Purchase)
  3. Bank Book          — bank/cash statement + reconciliation
  4. Voucher Register   — all posted vouchers
  5. Trial Balance      — Dr/Cr summary all accounts
  6. P&L               — Income - Expense
  7. Balance Sheet      — Assets = Liabilities + Capital
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta


def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []


def _check_db() -> bool:
    """Quick DB ping — returns True if connected, shows error and returns False if not."""
    try:
        _q("SELECT 1 AS ok")
        return True
    except Exception as e:
        st.error(
            f"**Database not connected** — `{e}`\n\n"
            "Check DB config in `modules/sql_adapter.py`."
        )
        return False


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _date_range(key="ac"):
    presets = {
        "This month":    (date.today().replace(day=1), date.today()),
        "Last month":    ((date.today().replace(day=1) - timedelta(days=1)).replace(day=1),
                          date.today().replace(day=1) - timedelta(days=1)),
        "This year":     (date.today().replace(month=4, day=1)
                          if date.today().month >= 4
                          else date.today().replace(year=date.today().year-1, month=4, day=1),
                          date.today()),
        "All time":      (date(2020, 1, 1), date.today()),
    }
    c1, c2, c3 = st.columns([1, 1, 1])
    preset = c3.selectbox("Period", list(presets.keys()), index=0, key=f"{key}_pre")
    df, dt = presets[preset]
    fd = c1.date_input("From", value=df, key=f"{key}_fd")
    td = c2.date_input("To",   value=dt, key=f"{key}_td")
    return fd, td


def _download(df, title, key):
    st.download_button("⬇ Export CSV",
        df.to_csv(index=False).encode(),
        file_name=f"{title.replace(' ','_')}.csv",
        mime="text/csv", key=key, width='stretch')


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — CHART OF ACCOUNTS
# ══════════════════════════════════════════════════════════════════════════════

def _tab_chart_of_accounts():
    if not _check_db():
        return
    from modules.accounting.accounts_engine import (
        ensure_accounting_schema, get_all_accounts,
    )
    ensure_accounting_schema()

    st.caption("Ledger master — same as Tally's Chart of Accounts")

    # ── Add new account ───────────────────────────────────────────────────
    with st.expander("➕ Create New Ledger Account", expanded=False):
        groups = _q("""
            SELECT id::text, name, nature FROM account_groups ORDER BY nature, name
        """)
        if not groups:
            st.warning("Run the accounting schema first — groups not found.")
            return

        c1, c2 = st.columns(2)
        code    = c1.text_input("Account Code", key="coa_code", placeholder="e.g. 5007")
        name    = c2.text_input("Account Name", key="coa_name", placeholder="e.g. Advertising")
        group_opts = {g["name"]: g["id"] for g in groups}
        group   = st.selectbox("Under Group", list(group_opts.keys()), key="coa_group")
        nature_map = {g["name"]: g["nature"] for g in groups}

        atype_opts = ["BANK","CASH","PARTY","SALES","PURCHASE","EXPENSE","TAX","OTHER"]
        atype   = st.selectbox("Account Type", atype_opts, index=6, key="coa_type")
        opening = st.number_input("Opening Balance ₹", value=0.0, step=100.0, key="coa_ob")
        notes   = st.text_input("Notes (optional)", key="coa_notes")

        if st.button("✅ Create Account", type="primary", key="coa_save"):
            if not name.strip():
                st.error("Account name is required.")
            else:
                gid     = group_opts[group]
                nature  = nature_map.get(group, "EXPENSE")
                exists  = _q("SELECT 1 FROM chart_of_accounts WHERE account_name=%s LIMIT 1",
                             (name.strip(),))
                if exists:
                    st.error(f"Account '{name}' already exists.")
                else:
                    from modules.sql_adapter import run_write
                    run_write("""
                        INSERT INTO chart_of_accounts
                            (account_code, account_name, group_id, nature,
                             account_type, opening_balance, notes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (account_name) DO NOTHING
                    """, (code.strip() or None, name.strip(), gid,
                          nature, atype, opening, notes.strip() or None))
                    st.success(f"✅ Account '{name}' created under {group}")
                    st.rerun()

    # ── Account list ──────────────────────────────────────────────────────
    nature_filter = st.radio("Filter by Nature",
                             ["All", "ASSET", "LIABILITY", "INCOME", "EXPENSE"],
                             horizontal=True, key="coa_nat")
    rows = get_all_accounts(nature=None if nature_filter == "All" else nature_filter)

    if not rows:
        st.info("No accounts yet — click Create above.")
        return

    df = _df(rows)
    st.caption(f"{len(df)} accounts")
    st.dataframe(df[[c for c in
        ["account_code","account_name","group_name","nature","account_type"]
        if c in df.columns]],
        width='stretch', hide_index=True)
    _download(df, "Chart_of_Accounts", "coa_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — MANUAL JOURNAL ENTRY
# ══════════════════════════════════════════════════════════════════════════════

def _tab_journal_entry():
    if not _check_db():
        return
    from modules.accounting.accounts_engine import (
        ensure_accounting_schema, get_all_accounts, post_journal,
    )
    ensure_accounting_schema()

    st.caption("Manual voucher entry — Journal, Contra, Purchase Invoice")

    vtype = st.radio("Voucher Type",
                     ["Journal Voucher", "Contra Voucher", "Purchase Invoice"],
                     horizontal=True, key="jv_type")

    vtype_map = {
        "Journal Voucher":  "JOURNAL",
        "Contra Voucher":   "CONTRA",
        "Purchase Invoice": "PURCHASE",
    }

    # Purchase Invoice: pre-fill standard lines
    if vtype == "Purchase Invoice":
        st.caption("Dr Purchase Account + Dr GST Input / Cr Sundry Creditors")
        c1, c2, c3 = st.columns(3)
        supplier  = c1.text_input("Supplier Name", key="pi_supplier",
                                   placeholder="Select from parties or type name")
        pi_amount = c2.number_input("Taxable Amount ₹", min_value=0.0,
                                     step=100.0, key="pi_taxable")
        pi_gst    = c3.number_input("GST % (0/5/12/18/28)", min_value=0.0,
                                     max_value=28.0, step=1.0, value=12.0, key="pi_gst")
        cat       = st.selectbox("Purchase Category",
                                  ["Lenses","Frames","Contact Lens","Accessories","Other"],
                                  key="pi_cat")
        pi_tax    = round(pi_amount * pi_gst / 100, 2)
        pi_total  = round(pi_amount + pi_tax, 2)
        pi_inv_no = st.text_input("Supplier Invoice No", key="pi_invno")

        st.markdown(
            f"<div style='background:#0a1628;border:1px solid #1e3a5f;border-radius:6px;"
            f"padding:8px 14px;margin:8px 0;font-size:.8rem'>"
            f"Dr Purchase ({cat}): <b>₹{pi_amount:,.2f}</b>"
            f"{'  +  Dr GST Input: <b>₹' + f'{pi_tax:,.2f}</b>' if pi_tax > 0 else ''}"
            f"  &nbsp;→&nbsp;  Cr Sundry Creditors: <b>₹{pi_total:,.2f}</b>"
            f"</div>",
            unsafe_allow_html=True
        )

        vdate = st.date_input("Date", value=date.today(), key="pi_date")
        pi_narr = st.text_input("Narration", key="pi_narr",
                                 placeholder=f"Purchase from {supplier or 'supplier'}")
        user = st.session_state.get("user_name", "Staff")

        if st.button("📋 Post Purchase Invoice", type="primary", key="pi_post",
                      disabled=(pi_amount <= 0 or not supplier)):
            try:
                from modules.accounting.accounts_engine import post_purchase_invoice_jv
                ok, vno = post_purchase_invoice_jv(
                    invoice_no        = pi_inv_no or f"PI-{date.today()}",
                    invoice_id        = "",
                    supplier_name     = supplier,
                    grand_total       = pi_total,
                    taxable           = pi_amount,
                    tax_amount        = pi_tax,
                    purchase_category = cat,
                    voucher_date      = vdate,
                    created_by        = user,
                )
                if ok:
                    st.success(f"✅ {vno} posted — Dr Purchase / Cr {supplier}")
                    st.rerun()
                else:
                    st.error(f"❌ {vno}")
            except Exception as e:
                st.error(f"Error: {e}")
        return  # don't show generic journal form for purchase invoice

    vdate   = st.date_input("Date", value=date.today(), key="jv_date")
    narr    = st.text_input("Narration", key="jv_narr",
                            placeholder="e.g. Monthly rent paid, Depreciation, Salary advance…")

    all_accounts = get_all_accounts()
    if not all_accounts:
        st.warning("Chart of Accounts is empty. Create accounts first.")
        return

    acct_opts  = {f"{a['account_code']} — {a['account_name']}": a["account_name"]
                  for a in all_accounts}
    acct_names = list(acct_opts.keys())

    # ── Line items (Dr/Cr rows) ───────────────────────────────────────────
    st.markdown("**Debit / Credit Lines** *(minimum 2 lines, must balance)*")

    n_lines = st.number_input("Number of lines", min_value=2, max_value=10,
                               value=2, step=1, key="jv_nlines")

    lines = []
    total_dr = total_cr = 0.0

    for i in range(int(n_lines)):
        c1, c2, c3, c4 = st.columns([3, 1.2, 1.2, 2])
        acct_sel  = c1.selectbox(f"Account {i+1}", [""] + acct_names,
                                  key=f"jv_acct_{i}")
        dr_amt    = c2.number_input("Debit ₹",  min_value=0.0, step=0.01,
                                     key=f"jv_dr_{i}", value=0.0)
        cr_amt    = c3.number_input("Credit ₹", min_value=0.0, step=0.01,
                                     key=f"jv_cr_{i}", value=0.0)
        line_narr = c4.text_input("Line narration", key=f"jv_lnarr_{i}",
                                   placeholder="optional")

        if acct_sel and (dr_amt > 0 or cr_amt > 0):
            lines.append({
                "account_name": acct_opts.get(acct_sel, acct_sel),
                "account_code": acct_sel.split(" — ")[0] if " — " in acct_sel else acct_sel,
                "debit":  dr_amt,
                "credit": cr_amt,
                "narration": line_narr,
            })
            total_dr += dr_amt
            total_cr += cr_amt

    # Balance indicator
    bal_diff = round(total_dr - total_cr, 2)
    bal_color = "#10b981" if abs(bal_diff) < 0.01 else "#ef4444"
    st.markdown(
        f"<div style='padding:8px 14px;background:#0d1929;"
        f"border:1px solid {bal_color};border-radius:6px;margin:8px 0;"
        f"display:flex;justify-content:space-between'>"
        f"<span style='color:#94a3b8'>Total Dr: <b style='color:#60a5fa'>₹{total_dr:,.2f}</b>"
        f"  &nbsp;  Total Cr: <b style='color:#60a5fa'>₹{total_cr:,.2f}</b></span>"
        f"<span style='color:{bal_color};font-weight:700'>"
        f"{'✅ Balanced' if abs(bal_diff)<0.01 else f'⚠️ Difference: ₹{abs(bal_diff):,.2f}'}"
        f"</span></div>",
        unsafe_allow_html=True
    )

    user = st.session_state.get("user_name", "Staff")

    if st.button("📋 Post Voucher", type="primary", key="jv_post",
                  disabled=(len(lines) < 2 or abs(bal_diff) > 0.01)):
        ok, vno, err = post_journal(
            voucher_type = vtype_map[vtype],
            voucher_date = vdate,
            narration    = narr,
            lines        = lines,
            created_by   = user,
            is_auto      = False,
        )
        if ok:
            st.success(f"✅ {vno} posted successfully")
            # Clear form
            for k in list(st.session_state.keys()):
                if k.startswith("jv_"):
                    st.session_state.pop(k, None)
            st.rerun()
        else:
            st.error(f"❌ {err}")

    # Recent manual vouchers
    with st.expander("📋 Recent Manual Vouchers", expanded=False):
        recent = _q("""
            SELECT voucher_date::text AS "Date", voucher_no AS "Voucher",
                   voucher_type AS "Type", narration AS "Narration",
                   total_debit AS "Amount (₹)", created_by AS "By"
            FROM journal_entries
            WHERE is_auto_posted = FALSE
            ORDER BY voucher_date DESC, created_at DESC LIMIT 20
        """)
        if recent:
            st.dataframe(_df(recent), width='stretch', hide_index=True,
                column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")})
        else:
            st.info("No manual vouchers yet.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — BANK BOOK
# ══════════════════════════════════════════════════════════════════════════════

def _tab_bank_book():
    if not _check_db():
        return
    from modules.accounting.accounts_engine import (
        ensure_accounting_schema, get_all_accounts, get_bank_book,
    )
    ensure_accounting_schema()

    st.caption("Bank / Cash book — all receipts and payments through bank/cash accounts")

    # Select bank/cash account
    bank_accts = [a for a in get_all_accounts()
                  if a["account_type"] in ("BANK", "CASH")]
    if not bank_accts:
        st.info("No Bank or Cash accounts in Chart of Accounts.")
        return

    acct_opts = {f"{a['account_code']} — {a['account_name']}": a["account_code"]
                 for a in bank_accts}
    selected  = st.selectbox("Account", list(acct_opts.keys()), key="bb_acct")
    acct_code = acct_opts[selected]
    fd, td    = _date_range("bb")

    rows = get_bank_book(acct_code, str(fd), str(td))

    if not rows:
        st.info("No entries for this account in selected period.")
        return

    df = _df(rows)
    for c in ["Receipts (₹)", "Payments (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    total_in  = df["Receipts (₹)"].sum()
    total_out = df["Payments (₹)"].sum()
    closing   = total_in - total_out

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Receipts",    f"₹{total_in:,.0f}")
    m2.metric("Payments",    f"₹{total_out:,.0f}")
    m3.metric("Net Balance", f"₹{closing:,.0f}")
    m4.metric("Entries",     str(len(df)))

    st.dataframe(df, width='stretch', hide_index=True,
        column_config={
            "Receipts (₹)":  st.column_config.NumberColumn(format="₹%.2f"),
            "Payments (₹)":  st.column_config.NumberColumn(format="₹%.2f"),
            "Reconciled":    st.column_config.CheckboxColumn("Recon?"),
        })

    # Bank reconciliation
    with st.expander("🔄 Mark Entries as Reconciled", expanded=False):
        st.caption("Match with bank statement — tick entries that appear in bank statement")
        unrecon = _q("""
            SELECT bt.id::text, bt.txn_date::text AS "Date",
                   bt.description AS "Description",
                   bt.debit AS "Dr (₹)", bt.credit AS "Cr (₹)",
                   bt.ref_no AS "Bank Ref"
            FROM bank_transactions bt
            JOIN chart_of_accounts a ON a.id = bt.bank_account_id
            WHERE a.account_code = %s
              AND bt.is_reconciled = FALSE
              AND bt.txn_date BETWEEN %s AND %s
            ORDER BY bt.txn_date
        """, (acct_code, str(fd), str(td)))

        if unrecon:
            for row in unrecon:
                col1, col2 = st.columns([5, 1])
                col1.markdown(
                    f"**{row['Date']}** — {row['Description']}  "
                    f"Dr: ₹{row.get('Dr (₹)',0):,.2f}  Cr: ₹{row.get('Cr (₹)',0):,.2f}  "
                    f"Ref: {row.get('Bank Ref','—')}"
                )
                if col2.button("✅", key=f"recon_{row['id']}"):
                    from modules.sql_adapter import run_write
                    run_write("UPDATE bank_transactions SET is_reconciled=TRUE WHERE id=%s::uuid",
                              (row["id"],))
                    st.rerun()
        else:
            st.success("✅ All entries reconciled for this period.")

    _download(df, f"Bank_Book_{selected.split('—')[1].strip()}", "bb_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — VOUCHER REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def _tab_voucher_register():
    if not _check_db():
        return
    from modules.accounting.accounts_engine import (
        ensure_accounting_schema, get_all_vouchers,
    )
    ensure_accounting_schema()

    st.caption("All posted vouchers — auto and manual")

    fd, td = _date_range("vr")
    vtype_filter = st.selectbox("Voucher Type",
        ["All","SALES","RECEIPT","PAYMENT","JOURNAL","CONTRA","PURCHASE"],
        key="vr_type")
    source = st.radio("Source", ["All", "Auto-posted", "Manual"],
                      horizontal=True, key="vr_src")

    rows = get_all_vouchers(str(fd), str(td),
                             voucher_type=("" if vtype_filter=="All" else vtype_filter),
                             limit=500)

    if source == "Auto-posted":
        rows = [r for r in rows if r.get("Auto")]
    elif source == "Manual":
        rows = [r for r in rows if not r.get("Auto")]

    if not rows:
        st.info("No vouchers found.")
        return

    df = _df(rows)
    if "Amount (₹)" in df.columns:
        df["Amount (₹)"] = pd.to_numeric(df["Amount (₹)"], errors="coerce").fillna(0)

    m1, m2 = st.columns(2)
    m1.metric("Vouchers",     str(len(df)))
    m2.metric("Total Amount", f"₹{df['Amount (₹)'].sum():,.0f}" if "Amount (₹)" in df.columns else "—")

    st.dataframe(df, width='stretch', hide_index=True,
        column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                       "Auto": st.column_config.CheckboxColumn("System?")})

    # Drill down into a voucher
    if rows:
        vno_sel = st.selectbox("View voucher details",
                                ["—"] + [r["Voucher No"] for r in rows],
                                key="vr_drill")
        if vno_sel and vno_sel != "—":
            lines = _q("""
                SELECT l.account_name AS "Account",
                       l.debit  AS "Dr (₹)", l.credit AS "Cr (₹)",
                       l.narration AS "Narration", l.party_name AS "Party"
                FROM journal_lines l
                JOIN journal_entries j ON j.id = l.journal_id
                WHERE j.voucher_no = %s
                ORDER BY l.debit DESC
            """, (vno_sel,))
            if lines:
                ldf = _df(lines)
                for c in ["Dr (₹)", "Cr (₹)"]:
                    if c in ldf.columns:
                        ldf[c] = pd.to_numeric(ldf[c], errors="coerce").fillna(0)
                st.dataframe(ldf, width='stretch', hide_index=True,
                    column_config={"Dr (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                                   "Cr (₹)": st.column_config.NumberColumn(format="₹%.2f")})

    _download(df, f"Voucher_Register", "vr_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — TRIAL BALANCE
# ══════════════════════════════════════════════════════════════════════════════

def _tab_trial_balance():
    if not _check_db():
        return
    from modules.accounting.accounts_engine import (
        ensure_accounting_schema, get_trial_balance,
    )
    ensure_accounting_schema()

    st.caption("All account balances for the period — Dr side = Cr side must match")

    fd, td = _date_range("tb")
    rows   = get_trial_balance(str(fd), str(td))

    if not rows:
        st.info("No journal entries in this period.")
        return

    df = _df(rows)
    for c in ["Opening (₹)", "Period Dr (₹)", "Period Cr (₹)",
              "Closing Dr (₹)", "Closing Cr (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    total_dr = df["Closing Dr (₹)"].clip(lower=0).sum()
    total_cr = df["Closing Cr (₹)"].clip(lower=0).sum()
    balanced = abs(total_dr - total_cr) < 1.0

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Dr",  f"₹{total_dr:,.2f}")
    m2.metric("Total Cr",  f"₹{total_cr:,.2f}")
    m3.metric("Difference", f"₹{abs(total_dr-total_cr):,.2f}",
              delta="✅ Balanced" if balanced else "⚠️ Not balanced",
              delta_color="normal" if balanced else "inverse")

    st.dataframe(df[[c for c in
        ["Code","Account","Group","Nature","Period Dr (₹)","Period Cr (₹)",
         "Closing Dr (₹)","Closing Cr (₹)"]
        if c in df.columns]],
        width='stretch', hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Period Dr (₹)","Period Cr (₹)",
                                 "Closing Dr (₹)","Closing Cr (₹)"]})
    _download(df, "Trial_Balance", "tb_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — P&L
# ══════════════════════════════════════════════════════════════════════════════

def _tab_pl():
    if not _check_db():
        return
    from modules.accounting.accounts_engine import ensure_accounting_schema
    ensure_accounting_schema()

    st.caption("Profit & Loss — Income minus Expenses for the period")

    fd, td = _date_range("pl")

    rows = _q("""
        SELECT
            g.name                      AS "Group",
            a.account_name              AS "Account",
            a.nature                    AS "Nature",
            COALESCE(SUM(l.debit),  0)  AS total_dr,
            COALESCE(SUM(l.credit), 0)  AS total_cr
        FROM chart_of_accounts a
        LEFT JOIN account_groups  g ON g.id = a.group_id
        LEFT JOIN journal_lines   l ON l.account_id = a.id
        LEFT JOIN journal_entries j ON j.id = l.journal_id
            AND j.voucher_date BETWEEN %s AND %s
        WHERE a.nature IN ('INCOME', 'EXPENSE')
          AND a.is_active = TRUE
        GROUP BY g.name, a.account_name, a.nature
        ORDER BY a.nature DESC, g.name, a.account_name
    """, (str(fd), str(td)))

    if not rows:
        st.info("No income/expense entries in this period.")
        return

    df = _df(rows)
    df["total_dr"] = pd.to_numeric(df["total_dr"], errors="coerce").fillna(0)
    df["total_cr"] = pd.to_numeric(df["total_cr"], errors="coerce").fillna(0)
    df["Amount"]   = df.apply(
        lambda r: r["total_cr"] - r["total_dr"] if r["Nature"] == "INCOME"
                  else r["total_dr"] - r["total_cr"],
        axis=1
    )

    income  = df[df["Nature"] == "INCOME"]
    expense = df[df["Nature"] == "EXPENSE"]
    total_income  = income["Amount"].sum()
    total_expense = expense["Amount"].sum()
    net_profit    = total_income - total_expense

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Income",   f"₹{total_income:,.2f}")
    m2.metric("Total Expense",  f"₹{total_expense:,.2f}")
    m3.metric("Net Profit",     f"₹{net_profit:,.2f}",
              delta_color="normal" if net_profit >= 0 else "inverse")

    st.markdown("**Income**")
    if not income.empty:
        st.dataframe(income[["Group","Account","Amount"]].assign(
            Amount=income["Amount"].map(lambda x: f"₹{x:,.2f}")
        ), width='stretch', hide_index=True)

    st.markdown("**Expenses**")
    if not expense.empty:
        st.dataframe(expense[["Group","Account","Amount"]].assign(
            Amount=expense["Amount"].map(lambda x: f"₹{x:,.2f}")
        ), width='stretch', hide_index=True)

    _download(df[["Group","Account","Nature","Amount"]], "PL_Statement", "pl_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — BALANCE SHEET
# ══════════════════════════════════════════════════════════════════════════════

def _tab_balance_sheet():
    if not _check_db():
        return
    from modules.accounting.accounts_engine import ensure_accounting_schema
    ensure_accounting_schema()

    st.caption("Balance Sheet — Assets = Liabilities + Capital (as on date)")

    _, td = _date_range("bs")
    fd    = "2000-01-01"      # cumulative from beginning

    rows = _q("""
        SELECT
            g.name                      AS "Group",
            a.account_name              AS "Account",
            a.nature                    AS "Nature",
            a.opening_balance           AS opening,
            COALESCE(SUM(l.debit),  0)  AS total_dr,
            COALESCE(SUM(l.credit), 0)  AS total_cr
        FROM chart_of_accounts a
        LEFT JOIN account_groups  g ON g.id = a.group_id
        LEFT JOIN journal_lines   l ON l.account_id = a.id
        LEFT JOIN journal_entries j ON j.id = l.journal_id
            AND j.voucher_date <= %s
        WHERE a.nature IN ('ASSET', 'LIABILITY')
          AND a.is_active = TRUE
        GROUP BY g.name, a.account_name, a.nature, a.opening_balance
        ORDER BY a.nature DESC, g.name, a.account_name
    """, (str(td),))

    if not rows:
        st.info("No balance sheet entries yet.")
        return

    df = _df(rows)
    for c in ["opening","total_dr","total_cr"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    df["Balance"] = df.apply(
        lambda r: r["opening"] + r["total_dr"] - r["total_cr"]
        if r["Nature"] == "ASSET"
        else r["opening"] + r["total_cr"] - r["total_dr"],
        axis=1
    )
    df = df[df["Balance"].abs() > 0.01]

    assets      = df[df["Nature"] == "ASSET"]
    liabilities = df[df["Nature"] == "LIABILITY"]
    total_assets = assets["Balance"].sum()
    total_liab   = liabilities["Balance"].sum()

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Assets",      f"₹{total_assets:,.2f}")
    m2.metric("Total Liabilities", f"₹{total_liab:,.2f}")
    m3.metric("Difference",        f"₹{abs(total_assets-total_liab):,.2f}",
              delta="✅ Balanced" if abs(total_assets-total_liab) < 1.0 else "⚠️ Check entries",
              delta_color="normal" if abs(total_assets-total_liab) < 1.0 else "inverse")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Assets**")
        if not assets.empty:
            for grp, gdf in assets.groupby("Group"):
                st.markdown(f"*{grp}*")
                for _, r in gdf.iterrows():
                    st.markdown(
                        f"&nbsp;&nbsp;&nbsp;{r['Account']}"
                        f"<span style='float:right'>₹{r['Balance']:,.2f}</span>",
                        unsafe_allow_html=True
                    )
            st.markdown(f"**Total Assets: ₹{total_assets:,.2f}**")

    with c2:
        st.markdown("**Liabilities**")
        if not liabilities.empty:
            for grp, gdf in liabilities.groupby("Group"):
                st.markdown(f"*{grp}*")
                for _, r in gdf.iterrows():
                    st.markdown(
                        f"&nbsp;&nbsp;&nbsp;{r['Account']}"
                        f"<span style='float:right'>₹{r['Balance']:,.2f}</span>",
                        unsafe_allow_html=True
                    )
            st.markdown(f"**Total Liabilities: ₹{total_liab:,.2f}**")

    _download(df[["Group","Account","Nature","Balance"]], "Balance_Sheet", "bs_dl")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# TAB 8 — BACKFILL (one-time migration for existing data)
# ══════════════════════════════════════════════════════════════════════════════

def _tab_backfill():
    if not _check_db():
        return
    st.caption("One-time migration — post journal entries for all existing invoices, payments and disbursements")

    # ── Live DB state check ───────────────────────────────────────────────
    with st.expander("🔎 Check DB State (what is actually stored)", expanded=True):
        try:
            c1,c2,c3 = st.columns(3)

            # Payments
            pay_rows = _q("""
                SELECT payment_type, payment_mode, COUNT(*) AS n,
                       ROUND(SUM(amount),2) AS total
                FROM payments
                WHERE COALESCE(is_deleted,FALSE)=FALSE
                GROUP BY payment_type, payment_mode
                ORDER BY payment_type, n DESC
            """)
            c1.markdown("**Payments table**")
            if pay_rows:
                for r in pay_rows:
                    c1.caption(
                        f"{r['payment_type']} / {r['payment_mode']}: "
                        f"**{r['n']}** entries  ₹{float(r['total'] or 0):,.2f}"
                    )
            else:
                c1.caption("No payments found")

            # Journal entries
            jv_rows = _q("""
                SELECT voucher_type, COUNT(*) AS n,
                       ROUND(SUM(total_debit),2) AS total
                FROM journal_entries
                GROUP BY voucher_type ORDER BY n DESC
            """)
            c2.markdown("**Journal entries**")
            if jv_rows:
                for r in jv_rows:
                    c2.caption(
                        f"{r['voucher_type']}: **{r['n']}** JVs  "
                        f"₹{float(r['total'] or 0):,.2f}"
                    )
            else:
                c2.caption("No journal entries yet")

            # Invoices + accounts
            inv_r = (_q("SELECT COUNT(*) AS n, ROUND(SUM(grand_total),2) AS t FROM invoices WHERE COALESCE(is_deleted,FALSE)=FALSE") or [{}])[0]
            acc_r = (_q("SELECT COUNT(*) AS n FROM chart_of_accounts") or [{}])[0]
            c3.markdown("**Other**")
            c3.caption(f"Invoices: **{inv_r.get('n',0)}**  ₹{float(inv_r.get('t') or 0):,.2f}")
            c3.caption(f"Chart of Accounts: **{acc_r.get('n',0)}** ledgers")

            # Check specific payment_types
            pt_check = _q("SELECT DISTINCT payment_type FROM payments")
            types = [r['payment_type'] for r in pt_check]
            c3.caption(f"Payment types in DB: {', '.join(types) if types else 'none'}")

        except Exception as e:
            st.error(f"DB check error: {e}")

    st.markdown("---")

    try:
        # Show what exists vs what's already journalised
        total_inv  = (_q("SELECT COUNT(*) AS n FROM invoices WHERE COALESCE(is_deleted,FALSE)=FALSE") or [{}])[0].get("n",0)
        _has_col = bool(_q("""SELECT 1 FROM information_schema.columns
            WHERE table_name='payments' AND column_name='is_cancelled' LIMIT 1"""))
        _cf = "AND NOT COALESCE(is_cancelled,FALSE)" if _has_col else ""
        total_pay  = (_q(f"SELECT COUNT(*) AS n FROM payments WHERE COALESCE(is_deleted,FALSE)=FALSE AND payment_type IN ('PAYMENT','RECEIPT') {_cf}") or [{}])[0].get("n",0)
        total_disb = (_q("SELECT COUNT(*) AS n FROM payments WHERE COALESCE(is_deleted,FALSE)=FALSE AND payment_type='DISBURSEMENT'") or [{}])[0].get("n",0)
        total_jv   = (_q("SELECT COUNT(*) AS n FROM journal_entries") or [{}])[0].get("n",0)

        posted_inv  = (_q("SELECT COUNT(*) AS n FROM journal_entries WHERE ref_doc_type='INVOICE'") or [{}])[0].get("n",0)
        posted_pay  = (_q("SELECT COUNT(*) AS n FROM journal_entries WHERE ref_doc_type IN ('PAYMENT','RECEIPT')") or [{}])[0].get("n",0)
        posted_disb = (_q("SELECT COUNT(*) AS n FROM journal_entries WHERE ref_doc_type='DISBURSEMENT'") or [{}])[0].get("n",0)

        m1,m2,m3,m4 = st.columns(4)
        m1.metric("Total Invoices",      str(total_inv),  delta=f"{int(total_inv)-int(posted_inv)} unposted", delta_color="inverse" if int(total_inv)>int(posted_inv) else "off")
        m2.metric("Total Payments",      str(total_pay),  delta=f"{int(total_pay)-int(posted_pay)} unposted",  delta_color="inverse" if int(total_pay)>int(posted_pay) else "off")
        m3.metric("Total Disbursements", str(total_disb), delta=f"{int(total_disb)-int(posted_disb)} unposted",delta_color="inverse" if int(total_disb)>int(posted_disb) else "off")
        m4.metric("Journal Entries",     str(total_jv))

    except Exception as e:
        st.error(f"Count error: {e}")
        return

    st.markdown("---")
    st.markdown(
        "<div style='background:#0a1a0a;border:1px solid #22c55e;border-radius:8px;"
        "padding:10px 16px;margin-bottom:12px'>"
        "<b style='color:#86efac'>✅ Safe to run multiple times</b> — skips any document "
        "that already has a journal entry. Will only post missing ones."
        "</div>",
        unsafe_allow_html=True
    )

    user = st.session_state.get("user_name", "Staff")

    if st.button("🔄 Run Backfill Now", type="primary", key="backfill_run",
                  width='stretch'):
        with st.spinner("Posting journal entries for all existing transactions…"):
            try:
                from modules.accounting.accounts_engine import backfill_journal_entries
                stats = backfill_journal_entries(created_by=f"Backfill by {user}")

                m1,m2,m3 = st.columns(3)
                m1.metric("Invoices posted",      stats["invoices"])
                m2.metric("Payments posted",      stats["payments"])
                m3.metric("Disbursements posted", stats["disbursements"])

                if stats["errors"]:
                    st.warning(f"⚠️ {len(stats['errors'])} entries could not be posted:")
                    with st.expander("Show errors", expanded=True):
                        for err in stats["errors"]:
                            st.caption(f"• {err}")
                    st.caption("Fix errors above then run backfill again — already-posted entries are skipped.")
                else:
                    st.success("✅ All entries posted successfully!")

                if stats.get("total", 0) > 0:
                    st.info("📊 Refresh Trial Balance, P&L and Balance Sheet to see updated figures.")
                st.rerun()
            except Exception as e:
                st.error(f"Backfill failed: {e}")
                import traceback; st.code(traceback.format_exc())



# ══════════════════════════════════════════════════════════════════════════════
# TAB — ACCOUNT LEDGER (drill-down from any account)
# ══════════════════════════════════════════════════════════════════════════════

def _tab_account_ledger():
    if not _check_db():
        return
    from modules.accounting.accounts_engine import (
        ensure_accounting_schema, get_all_accounts, get_account_ledger,
    )
    ensure_accounting_schema()

    st.caption("Click any account to open its full register — daily / monthly / yearly view")

    # ── Account selector ──────────────────────────────────────────────────
    all_accounts = get_all_accounts()
    if not all_accounts:
        st.info("No accounts yet.")
        return

    # Group by nature for clean display
    nature_icons = {"INCOME": "📈", "EXPENSE": "📉", "ASSET": "🏦", "LIABILITY": "⚖️"}

    # Filter by nature
    nat_filter = st.radio(
        "Category",
        ["All", "📈 Income", "📉 Expense", "🏦 Asset", "⚖️ Liability"],
        horizontal=True, key="al_nat",
    )
    nat_map = {
        "All": None, "📈 Income": "INCOME", "📉 Expense": "EXPENSE",
        "🏦 Asset": "ASSET", "⚖️ Liability": "LIABILITY",
    }
    nat = nat_map[nat_filter]
    filtered = [a for a in all_accounts if nat is None or a["nature"] == nat]

    # Build display options with group
    def _acct_label(a):
        icon = nature_icons.get(a["nature"], "📋")
        return f"{icon} {a['account_code']} — {a['account_name']}  [{a.get('group_name','')}]"

    labels    = [_acct_label(a) for a in filtered]
    acct_map  = {_acct_label(a): a for a in filtered}

    # Search filter
    def _on_acct_search():
        st.session_state["al_search_term"] = st.session_state.get("al_search_input", "")

    st.text_input(
        "🔍 Filter accounts",
        key="al_search_input",
        placeholder="Type account name…",
        on_change=_on_acct_search,
    )
    term = st.session_state.get("al_search_term", "")
    if term:
        labels   = [l for l in labels if term.lower() in l.lower()]
        if not labels:
            st.caption(f"No accounts matching '{term}'")
            return

    placeholder = f"— Select account ({len(labels)}) —"
    chosen = st.selectbox("Select Account", [placeholder] + labels, key="al_acct_sel")

    if not chosen or chosen == placeholder:
        # Show account summary cards when nothing selected
        st.markdown("---")
        _render_account_summary_cards(all_accounts)
        return

    acct = acct_map[chosen]

    # ── Date range + grouping ─────────────────────────────────────────────
    st.markdown("---")
    c1, c2, c3 = st.columns([1, 1, 1])
    fd, td   = _date_range("al")
    grouping = c3.radio("View", ["Detail", "Daily", "Monthly", "Yearly"],
                        horizontal=True, key="al_grp")

    # ── Fetch ledger ──────────────────────────────────────────────────────
    rows = get_account_ledger(acct["account_code"], str(fd), str(td))

    if not rows:
        st.info(f"No entries for **{acct['account_name']}** in this period.")
        # Show opening balance if any
        if float(acct.get("opening_balance") or 0) != 0:
            st.metric("Opening Balance", f"₹{float(acct['opening_balance']):,.2f}")
        return

    df = _df(rows)
    for c in ["Dr (₹)", "Cr (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Running balance
    if acct["nature"] in ("ASSET", "EXPENSE"):
        df["Balance (₹)"] = (df["Dr (₹)"] - df["Cr (₹)"]).cumsum()
    else:
        df["Balance (₹)"] = (df["Cr (₹)"] - df["Dr (₹)"]).cumsum()

    # Opening balance
    opening = float(acct.get("opening_balance") or 0)
    df["Balance (₹)"] = df["Balance (₹)"] + opening

    total_dr = df["Dr (₹)"].sum()
    total_cr = df["Cr (₹)"].sum()
    closing  = opening + (total_dr - total_cr if acct["nature"] in ("ASSET","EXPENSE")
                          else total_cr - total_dr)

    # ── Metrics ───────────────────────────────────────────────────────────
    icon = nature_icons.get(acct["nature"], "📋")
    st.markdown(
        f"<div style='background:#0a1628;border:1px solid #1e3a5f;border-radius:8px;"
        f"padding:10px 16px;margin-bottom:12px'>"
        f"<b style='color:#60a5fa;font-size:1rem'>{icon} {acct['account_name']}</b>"
        f"  <span style='color:#475569;font-size:.8rem'>{acct.get('group_name','')}"
        f"  ·  {acct['account_code']}</span></div>",
        unsafe_allow_html=True
    )

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Opening",   f"₹{opening:,.2f}")
    m2.metric("Total Dr",  f"₹{total_dr:,.2f}")
    m3.metric("Total Cr",  f"₹{total_cr:,.2f}")
    m4.metric("Closing",   f"₹{closing:,.2f}")
    m5.metric("Entries",   str(len(df)))

    # ── Grouped view ──────────────────────────────────────────────────────
    if grouping == "Detail":
        st.dataframe(
            df[["Date","Voucher No","Type","Narration","Party","Dr (₹)","Cr (₹)","Balance (₹)"]],
            width='stretch', hide_index=True,
            column_config={
                "Dr (₹)":      st.column_config.NumberColumn(format="₹%.2f"),
                "Cr (₹)":      st.column_config.NumberColumn(format="₹%.2f"),
                "Balance (₹)": st.column_config.NumberColumn(format="₹%.2f"),
            }
        )

    elif grouping == "Daily":
        grp = df.groupby("Date").agg(
            Entries=("Dr (₹)", "count"),
            **{"Dr (₹)":  ("Dr (₹)",  "sum"),
               "Cr (₹)":  ("Cr (₹)",  "sum")}
        ).reset_index()
        grp["Net (₹)"] = grp["Dr (₹)"] - grp["Cr (₹)"]
        st.dataframe(grp, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in ["Dr (₹)","Cr (₹)","Net (₹)"]})

    elif grouping == "Monthly":
        df["Month"] = pd.to_datetime(df["Date"]).dt.to_period("M").astype(str)
        grp = df.groupby("Month").agg(
            Entries=("Dr (₹)", "count"),
            **{"Dr (₹)":  ("Dr (₹)",  "sum"),
               "Cr (₹)":  ("Cr (₹)",  "sum")}
        ).reset_index()
        grp["Net (₹)"] = grp["Dr (₹)"] - grp["Cr (₹)"]
        grp = grp.sort_values("Month")
        st.dataframe(grp, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in ["Dr (₹)","Cr (₹)","Net (₹)"]})

    elif grouping == "Yearly":
        df["Year"] = pd.to_datetime(df["Date"]).dt.year.astype(str)
        grp = df.groupby("Year").agg(
            Entries=("Dr (₹)", "count"),
            **{"Dr (₹)":  ("Dr (₹)",  "sum"),
               "Cr (₹)":  ("Cr (₹)",  "sum")}
        ).reset_index()
        grp["Net (₹)"] = grp["Dr (₹)"] - grp["Cr (₹)"]
        st.dataframe(grp, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in ["Dr (₹)","Cr (₹)","Net (₹)"]})

    _download(df, f"Ledger_{acct['account_name']}", "al_dl")


def _render_account_summary_cards(all_accounts: list):
    """Show clickable summary cards grouped by nature — like Tally's account list."""
    st.caption("Select an account above to open its register")

    nature_order = ["INCOME", "EXPENSE", "ASSET", "LIABILITY"]
    nature_labels = {
        "INCOME":    ("📈 Income Accounts",    "#10b981", "#0a2a1a"),
        "EXPENSE":   ("📉 Expense Accounts",   "#f97316", "#2a1a0a"),
        "ASSET":     ("🏦 Asset Accounts",     "#3b82f6", "#0a1628"),
        "LIABILITY": ("⚖️ Liability Accounts", "#a855f7", "#1a0a2a"),
    }

    # Fetch current period balances
    try:
        bal_rows = _q("""
            SELECT
                a.account_code,
                a.account_name,
                a.nature,
                a.opening_balance,
                COALESCE(SUM(l.debit),  0) AS total_dr,
                COALESCE(SUM(l.credit), 0) AS total_cr
            FROM chart_of_accounts a
            LEFT JOIN journal_lines   l ON l.account_id = a.id
            LEFT JOIN journal_entries j ON j.id = l.journal_id
                AND j.voucher_date >= date_trunc('month', CURRENT_DATE)
            WHERE a.is_active = TRUE
            GROUP BY a.account_code, a.account_name, a.nature, a.opening_balance
            ORDER BY a.account_code
        """)
    except Exception:
        bal_rows = []

    bal_map = {r["account_code"]: r for r in bal_rows}

    for nat in nature_order:
        accts = [a for a in all_accounts if a["nature"] == nat]
        if not accts:
            continue
        label, color, bg = nature_labels[nat]
        st.markdown(f"**{label}**")
        cols = st.columns(4)
        for i, a in enumerate(accts):
            b    = bal_map.get(a["account_code"], {})
            dr   = float(b.get("total_dr", 0) or 0)
            cr   = float(b.get("total_cr", 0) or 0)
            bal  = dr - cr if nat in ("ASSET","EXPENSE") else cr - dr
            with cols[i % 4]:
                st.markdown(
                    f"<div style='background:{bg};border:1px solid {color}33;"
                    f"border-radius:8px;padding:10px 12px;margin-bottom:8px;cursor:pointer'>"
                    f"<div style='color:{color};font-weight:700;font-size:.8rem'>"
                    f"{a['account_code']} — {a['account_name']}</div>"
                    f"<div style='color:#94a3b8;font-size:.7rem'>{a.get('group_name','')}</div>"
                    f"<div style='color:#e2e8f0;font-size:.95rem;font-weight:700;margin-top:4px'>"
                    f"₹{bal:,.0f}</div>"
                    f"<div style='color:#475569;font-size:.65rem'>this month</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )
        st.markdown("")


def render_accounts():
    st.markdown("## 📒 Accounts")

    # ── DB connection check ────────────────────────────────────────────────
    if not _check_db():
        st.info("💡 Check that PostgreSQL is running and `modules/sql_adapter.py` has correct credentials.")
        return

    # ── Create/migrate accounting tables ──────────────────────────────────
    try:
        from modules.accounting.accounts_engine import ensure_accounting_schema
        ensure_accounting_schema()
    except Exception as e:
        st.error(f"Schema setup failed: {e}")
        st.caption("Tables may already exist — continuing anyway.")

    # ── Verify tables exist ────────────────────────────────────────────────
    try:
        tables = _q("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public'
              AND table_name IN ('account_groups','chart_of_accounts',
                                 'journal_entries','journal_lines','bank_transactions')
            ORDER BY table_name
        """)
        existing = {r["table_name"] for r in tables}
        expected = {'account_groups','chart_of_accounts','journal_entries',
                    'journal_lines','bank_transactions'}
        missing  = expected - existing
        if missing:
            st.warning(
                f"⚠️ Tables not yet created: `{'`, `'.join(sorted(missing))}`  \n"
                "Run the app once with DB connected — `ensure_accounting_schema()` "
                "creates them automatically on first load."
            )
    except Exception as e:
        st.error(f"Table check failed: {e}")
        return

    # ── Show account count ─────────────────────────────────────────────────
    try:
        cnt = _q("SELECT COUNT(*) AS n FROM chart_of_accounts")
        n = int(cnt[0]["n"]) if cnt else 0
        if n == 0:
            st.info("📋 No accounts yet — Chart of Accounts will be seeded with defaults on first load.")
        else:
            st.caption(f"✅ Connected · {n} accounts in Chart of Accounts")
    except Exception:
        pass

    tabs = st.tabs([
        "📋 Chart of Accounts",
        "📖 Account Ledger",
        "✏️ Journal Entry",
        "🏦 Bank Book",
        "📄 Voucher Register",
        "⚖️ Trial Balance",
        "📈 P&L",
        "🏛️ Balance Sheet",
        "🔄 Backfill",
    ])

    with tabs[0]: _tab_chart_of_accounts()
    with tabs[1]: _tab_account_ledger()
    with tabs[2]: _tab_journal_entry()
    with tabs[3]: _tab_bank_book()
    with tabs[4]: _tab_voucher_register()
    with tabs[5]: _tab_trial_balance()
    with tabs[6]: _tab_pl()
    with tabs[7]: _tab_balance_sheet()
    with tabs[8]: _tab_backfill()
