#!/usr/bin/env python3
"""
clean_and_reset.py
==================
Resets all transactional data — orders, invoices, payments, etc.
Keeps master data: parties, patients, products, inventory, settings.

ORDER NUMBERS restart from:
  Orders:   OR01-25-26, OR02-25-26 ...
  Invoices: INV/2526/0001
  Payments: PAY/2526/0001
  Challans: CHL/2526/0001

Run: python clean_and_reset.py
"""

import sys
sys.path.insert(0, ".")

from modules.sql_adapter import run_write, run_query

def confirm():
    print("\n" + "="*60)
    print("  ⚠️  THIS WILL DELETE ALL TRANSACTIONAL DATA  ⚠️")
    print("="*60)
    print("  Deletes: orders, order_lines, payments, invoices,")
    print("           challans, party_ledger, journal_entries,")
    print("           attendance, delivery sessions, etc.")
    print("  Keeps:   parties, patients, products, inventory,")
    print("           system_flags, users, settings")
    print("="*60)
    ans = input("\n  Type YES to proceed: ").strip()
    return ans == "YES"

def step(label, sql, params=None):
    try:
        run_write(sql, params or ())
        print(f"  ✅  {label}")
    except Exception as e:
        print(f"  ⚠️  {label}: {e}")

def query(label, sql, params=None):
    try:
        r = run_query(sql, params or ()) or []
        print(f"  📊  {label}: {r[0].get('n', r) if r else 0}")
    except Exception as e:
        print(f"  ❌  {label}: {e}")

def run():
    if not confirm():
        print("\nAborted.")
        return

    print("\n── Step 1: Show current counts ──────────────────────")
    for tbl in ["orders","invoices","challans","payments","party_ledger",
                "journal_entries","journal_lines"]:
        try:
            r = run_query(f"SELECT COUNT(*) AS n FROM {tbl}")
            n = r[0]["n"] if r else "?"
            print(f"  {tbl}: {n}")
        except Exception as e:
            print(f"  {tbl}: {e}")

    print("\n── Step 2: Delete transactional data ────────────────")
    # Accounting
    step("journal_lines",            "TRUNCATE TABLE journal_lines CASCADE")
    step("journal_entries",          "TRUNCATE TABLE journal_entries CASCADE")
    step("bank_transactions",        "TRUNCATE TABLE bank_transactions CASCADE")

    # Billing
    step("invoice_lines",            "TRUNCATE TABLE invoice_lines CASCADE")
    step("challan_lines",            "TRUNCATE TABLE challan_lines CASCADE")
    step("challan_service_charges",  "TRUNCATE TABLE challan_service_charges CASCADE")
    step("order_charges",            "TRUNCATE TABLE order_charges CASCADE")
    step("invoices",                 "TRUNCATE TABLE invoices CASCADE")
    step("challans",                 "TRUNCATE TABLE challans CASCADE")
    step("payment_links",            "TRUNCATE TABLE payment_links CASCADE")
    step("payments",                 "TRUNCATE TABLE payments CASCADE")
    step("party_ledger",             "TRUNCATE TABLE party_ledger CASCADE")
    step("document_ledger",          "TRUNCATE TABLE document_ledger CASCADE")
    step("credit_debit_notes",       "TRUNCATE TABLE credit_debit_notes CASCADE")

    # Orders
    step("order_status_history",     "TRUNCATE TABLE order_status_history CASCADE")
    step("order_dispatch_lines",     "TRUNCATE TABLE order_dispatch_lines CASCADE")
    step("order_dispatches",         "TRUNCATE TABLE order_dispatches CASCADE")
    step("job_master",               "TRUNCATE TABLE job_master CASCADE")
    step("blank_allocations",        "TRUNCATE TABLE blank_allocations CASCADE")
    step("order_lines",              "TRUNCATE TABLE order_lines CASCADE")
    step("orders",                   "TRUNCATE TABLE orders CASCADE")

    # HR / Delivery (if exists)
    for tbl in ["attendance_logs","leave_requests","delivery_waypoints",
                "delivery_orders","delivery_routes"]:
        step(f"{tbl}", f"TRUNCATE TABLE {tbl} CASCADE")

    # Discount / audit logs
    for tbl in ["discount_applications","discount_decisions","discount_rule_audit",
                "edit_log","audit_log","crash_snapshots"]:
        step(f"{tbl}", f"TRUNCATE TABLE {tbl} CASCADE")

    print("\n── Step 3: Reset order number series ────────────────")
    # Reset order_number_registry to start from 0
    series = [
        ("RETAIL",            "OR",    "25-26"),
        ("WHOLESALE",         "OR",    "25-26"),
        ("CONSULTATION",      "CONS",  "25-26"),
        ("INVOICE",           "INV",   "2526"),
        ("CHALLAN",           "CHL",   "2526"),
        ("PAYMENT",           "PAY",   "2526"),
        ("JOURNAL",           "JV",    "2526"),
        ("PURCHASE_INVOICE",  "PINV",  "2526"),
        ("VOUCHER_SALES",     "SV",    "2526"),
        ("VOUCHER_RECEIPT",   "RV",    "2526"),
        ("VOUCHER_PAYMENT",   "PV",    "2526"),
        ("VOUCHER_JOURNAL",   "JV",    "2526"),
        ("VOUCHER_CONTRA",    "CV",    "2526"),
        ("VOUCHER_PURCHASE",  "PIV",   "2526"),
    ]
    for series_key, prefix, fy in series:
        try:
            run_write("""
                INSERT INTO order_number_registry (series, last_number, prefix, fiscal_year)
                VALUES (%s, 0, %s, %s)
                ON CONFLICT (series) DO UPDATE SET
                    last_number = 0,
                    prefix = EXCLUDED.prefix,
                    fiscal_year = EXCLUDED.fiscal_year
            """, (series_key, prefix, fy))
            print(f"  ✅  {series_key} → 0 (next: {prefix}01-25-26 or {prefix}/2526/0001)")
        except Exception as e:
            print(f"  ⚠️  {series_key}: {e}")

    # Reset display order sequence
    try:
        run_write("ALTER SEQUENCE IF EXISTS orders_display_seq RESTART WITH 1")
        print("  ✅  orders_display_seq → 1")
    except Exception as e:
        print(f"  ⚠️  orders_display_seq: {e}")

    print("\n── Step 4: Verify clean ─────────────────────────────")
    for tbl in ["orders","invoices","challans","payments","party_ledger",
                "journal_entries"]:
        try:
            r = run_query(f"SELECT COUNT(*) AS n FROM {tbl}")
            n = int(r[0]["n"]) if r else "?"
            status = "✅" if n == 0 else f"⚠️  still has {n} rows"
            print(f"  {status}  {tbl}: {n}")
        except Exception as e:
            print(f"  ❌  {tbl}: {e}")

    print("\n── Step 5: Verify master data preserved ─────────────")
    for tbl in ["parties","patients","products","inventory_stock","system_flags"]:
        try:
            r = run_query(f"SELECT COUNT(*) AS n FROM {tbl}")
            n = r[0]["n"] if r else "?"
            print(f"  ✅  {tbl}: {n} rows kept")
        except Exception as e:
            print(f"  ❌  {tbl}: {e}")

    print("\n" + "="*60)
    print("  ✅  RESET COMPLETE")
    print("  Next order will be: OR01-25-26")
    print("  Next invoice will be: INV/2526/0001")
    print("="*60 + "\n")

if __name__ == "__main__":
    run()
