"""
SECTION GUARD — WIRING GUIDE
════════════════════════════════════════════════════════════════════════
Shows exactly what to change in each page file.
Only the tab block changes — everything else stays identical.
This is a reference document, not a runnable script.

Place this file at: WIN54/modules/docs/SECTION_GUARD_WIRING.py
"""


# ══════════════════════════════════════════════════════════════════════════
# 1.  WIN54/modules/reports/reports_ui.py
#     Function: render_reports()
# ══════════════════════════════════════════════════════════════════════════

# ── BEFORE ────────────────────────────────────────────────────────────────
"""
def render_reports():
    st.markdown("## 📊 Reports")

    tabs = st.tabs([
        "📒 Ledger",
        "📦 Product Sales",
        "📋 Columnar",
        "⏰ Credit Days",
        "🚚 Challan Register",
        "🏭 Stock Value",
        "💰 Party Outstanding",
        "📅 Aging Report",
        "💵 Cash Flow",
        "🧾 GST Summary",
        "🔍 Audit Trail",
    ])
    with tabs[0]:  _tab_ledger()
    with tabs[1]:  _tab_product_sales()
    with tabs[2]:  _tab_columnar()
    with tabs[3]:  _tab_credit_days()
    with tabs[4]:  _tab_challan_register()
    with tabs[5]:  _tab_stock_value()
    with tabs[6]:  _tab_outstanding()
    with tabs[7]:  _tab_aging()
    with tabs[8]:  _tab_cashflow()
    with tabs[9]:  _tab_gst()
    with tabs[10]: _tab_audit()
"""

# ── AFTER ─────────────────────────────────────────────────────────────────
"""
def render_reports():
    st.markdown("## 📊 Reports")

    from modules.security.section_guard import render_guarded_tabs
    render_guarded_tabs("reports", [
        ("view_ledger",           "📒 Ledger",           _tab_ledger),
        ("view_product_sales",    "📦 Product Sales",    _tab_product_sales),
        ("view_columnar",         "📋 Columnar",         _tab_columnar),
        ("view_credit_days",      "⏰ Credit Days",       _tab_credit_days),
        ("view_challan_register", "🚚 Challan Register", _tab_challan_register),
        ("view_stock_value",      "🏭 Stock Value",       _tab_stock_value),
        ("view_outstanding",      "💰 Party Outstanding", _tab_outstanding),
        ("view_aging",            "📅 Aging Report",      _tab_aging),
        ("view_cashflow",         "💵 Cash Flow",         _tab_cashflow),
        ("view_gst",              "🧾 GST Summary",       _tab_gst),
        ("view_audit_trail",      "🔍 Audit Trail",       _tab_audit),
    ])
"""


# ══════════════════════════════════════════════════════════════════════════
# 2.  WIN54/modules/reports/registers.py
#     Function: render_registers()  (or wherever st.tabs is called)
# ══════════════════════════════════════════════════════════════════════════

# ── BEFORE ────────────────────────────────────────────────────────────────
"""
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
"""

# ── AFTER ─────────────────────────────────────────────────────────────────
"""
    from modules.security.section_guard import render_guarded_tabs
    render_guarded_tabs("registers", [
        ("view_sales_register",    "🧾 Sales Register",     render_sales_register),
        ("view_purchase_register", "🛒 Purchase Register",  render_purchase_register),
        ("view_receipt_book",      "💵 Receipt Book",        render_payment_receipt_book),
        ("view_disbursement_book", "💸 Disbursement Book",   render_disbursement_book),
        ("view_cash_book",         "💰 Cash Book",           render_cash_book),
        ("view_bank_book",         "🏦 Bank Book",           render_bank_book),
        ("view_party_ledger",      "👤 Party Ledger",        render_party_ledger),
        ("view_debtors_register",  "📥 Debtors Register",   render_debtors_register),
        ("view_order_register",    "📦 Order Register",      render_order_register),
        ("view_journal_register",  "📋 Journal Register",    render_journal_register),
    ])
"""


# ══════════════════════════════════════════════════════════════════════════
# 3.  WIN54/modules/hr/hr_ui.py
# ══════════════════════════════════════════════════════════════════════════

# ── BEFORE ────────────────────────────────────────────────────────────────
"""
    tabs = st.tabs([
        "📍 My Attendance",
        "📋 Today's Roster",
        "📅 Monthly Sheet",
        "🏖️ Leave",
        "👤 Employees",
        "🏢 Office Setup",
        "💰 Payroll",
    ])
    with tabs[0]: _tab_my_attendance()
    with tabs[1]: _tab_roster()
    with tabs[2]: _tab_monthly()
    with tabs[3]: _tab_leave()
    with tabs[4]: _tab_employees()
    with tabs[5]: _tab_office_setup()
    with tabs[6]: _tab_payroll()
"""

# ── AFTER ─────────────────────────────────────────────────────────────────
"""
    from modules.security.section_guard import render_guarded_tabs
    render_guarded_tabs("hr_attendance", [
        ("view_my_attendance",  "📍 My Attendance",   _tab_my_attendance),
        ("view_roster",         "📋 Today's Roster",  _tab_roster),
        ("view_monthly_sheet",  "📅 Monthly Sheet",   _tab_monthly),
        ("view_leave",          "🏖️ Leave",            _tab_leave),
        ("view_employees",      "👤 Employees",        _tab_employees),
        ("view_office_setup",   "🏢 Office Setup",     _tab_office_setup),
        ("view_payroll",        "💰 Payroll",          _tab_payroll),
    ])
"""


# ══════════════════════════════════════════════════════════════════════════
# 4.  WIN54/modules/accounting/accounts_ui.py
# ══════════════════════════════════════════════════════════════════════════

# ── BEFORE ────────────────────────────────────────────────────────────────
"""
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
"""

# ── AFTER ─────────────────────────────────────────────────────────────────
"""
    from modules.security.section_guard import render_guarded_tabs
    render_guarded_tabs("accounts", [
        ("view_chart_of_accounts", "📋 Chart of Accounts", _tab_chart_of_accounts),
        ("view_account_ledger",    "📖 Account Ledger",    _tab_account_ledger),
        ("view_journal_entry",     "✏️ Journal Entry",     _tab_journal_entry),
        ("view_bank_book",         "🏦 Bank Book",          _tab_bank_book),
        ("view_voucher_register",  "📄 Voucher Register",  _tab_voucher_register),
        ("view_trial_balance",     "⚖️ Trial Balance",      _tab_trial_balance),
        ("view_pl",                "📈 P&L",                _tab_pl),
        ("view_balance_sheet",     "🏛️ Balance Sheet",     _tab_balance_sheet),
        ("view_backfill",          "🔄 Backfill",           _tab_backfill),
    ])
"""


# ══════════════════════════════════════════════════════════════════════════
# HOW IT WORKS FOR ADMIN
# ══════════════════════════════════════════════════════════════════════════
#
# In Permission Designer → 🔐 Permissions tab:
#   1. Select role (e.g. BILLING)
#   2. Click "📊 Reports" in left panel
#   3. Right panel now shows:
#
#      ☑ View reports                ← module-level action (already had this)
#      ☑ Export / download data      ← module-level action
#      ─────────────────────────────
#      ☑ Tab: Ledger
#      ☑ Tab: Product Sales
#      ☐ Tab: Credit Days            ← admin unticked — billing won't see this
#      ☐ Tab: Stock Value
#      ☐ Tab: Aging Report
#      ☐ Tab: Audit Trail            ← sensitive — manager only
#      ☑ Tab: Challan Register
#      ...
#
#   4. Save → BILLING user now sees only their allowed tabs
#
# ══════════════════════════════════════════════════════════════════════════
