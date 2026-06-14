"""
Phase-2 operational data reset for WIN54.

Keeps schema/master data alive:
- products, ophthalmic specs, inventory_stock price/catalog rows
- parties/suppliers, service providers/rates, fitters, GST/account masters
- product supplier maps and supplier invoice parser rules

Deletes transactional/test data:
- orders/order lines/status/history
- challans, invoices, payments, ledgers
- procurement, purchase acknowledgements/registers, supplier orders
- production/job/fitting/dispatch rows
- supplier/party schemes and scheme assignments

Usage:
    python scripts/phase2_operational_reset.py --dry-run
    python scripts/phase2_operational_reset.py --execute --confirm RESET_PHASE2
"""

from __future__ import annotations

import argparse
import datetime as dt
import re

from modules.sql_adapter import close_connection, get_transaction_connection


RESET_TABLES = [
    # Order / backoffice / production
    "arc_backstep_log",
    "blank_allocations",
    "blank_stock_ledger",
    "clinical_media",
    "document_ledger",
    "fitting_assignments",
    "fitting_jobs",
    "fitting_stage_events",
    "job_master",
    "job_rejection_log",
    "job_stage_events",
    "order_charges",
    "order_dispatch_lines",
    "order_dispatches",
    "order_lines",
    "order_status_history",
    "order_summary",
    "orders",
    "print_logs",
    # Billing / accounting transactions
    "bank_transactions",
    "billing_margin_alerts",
    "challan_lines",
    "challan_service_charges",
    "challans",
    "cost_ledger",
    "credit_note_lines",
    "credit_notes",
    "debit_note_lines",
    "debit_notes",
    "discount_applications",
    "discount_decisions",
    "discount_rule_audit",
    "doc_number_log",
    "invoice_lines",
    "invoices",
    "journal_entries",
    "journal_lines",
    "party_ledger",
    "payment_links",
    "payments",
    "promo_code_usage",
    # Procurement / purchase / supplier
    "fitter_payments",
    "gst_2b",
    "gst_recon_result",
    "inbound_stock",
    "invoice_match_audit",
    "po_approval_log",
    "procurement_order_items",
    "procurement_orders",
    "procurement_pa_audit_log",
    "procurement_receipts",
    "purchase_acknowledgements",
    "purchase_challan_lines",
    "purchase_challan_register",
    "purchase_invoice_lines",
    "purchase_invoice_register",
    "purchase_invoice_register_lines",
    "purchase_invoices",
    "purchase_order_items",
    "purchase_orders",
    "reorder_log",
    "stock_adjustments",
    "stock_recon_log",
    "supplier_invoice_matches",
    "supplier_invoice_uploads",
    "supplier_order_items",
    "supplier_order_status_history",
    "supplier_orders",
    # Online / retailer test orders
    "online_order_lines",
    "online_orders",
    "online_otps",
    "online_sessions",
    "retailer_order_lines",
    "retailer_orders",
    # Schemes/offers test layer
    "cash_schemes",
    "club_offers",
    "supplier_party_scheme_assignments",
    "supplier_party_scheme_rules",
    "supplier_party_schemes",
    # CRM activity, not party master
    "crm_followups",
    "crm_leads",
    "crm_touchpoints",
    # Runtime logs/test noise
    "audit_log",
    "audit_logs",
    "error_log",
    "erp_alerts",
    "field_change_backup",
    "field_change_log",
    "loader_import_log",
    "loader_row_history",
    "pricing_shadow_decisions",
]

RESET_SEQUENCES_TABLES = ["order_number_registry"]

RESET_INVENTORY_ALLOCATION_SQL = """
UPDATE inventory_stock
SET allocated_qty = 0,
    reserved_qty = 0,
    updated_at = NOW()
WHERE COALESCE(allocated_qty,0) <> 0
   OR COALESCE(reserved_qty,0) <> 0;
"""


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise ValueError(f"Unsafe identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def _existing_tables(cur, tables: list[str]) -> list[str]:
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='public'
          AND table_type='BASE TABLE'
          AND table_name = ANY(%s)
        ORDER BY table_name
        """,
        (tables,),
    )
    return [r[0] for r in (cur.fetchall() or [])]


def _counts(cur, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in tables:
        cur.execute(f"SELECT COUNT(*) FROM public.{_quote_ident(table)}")
        out[table] = int(cur.fetchone()[0] or 0)
    return out


def _backup_schema(cur, schema: str, tables: list[str]) -> None:
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema)}")
    for table in tables:
        cur.execute(
            f"CREATE TABLE {_quote_ident(schema)}.{_quote_ident(table)} "
            f"AS TABLE public.{_quote_ident(table)}"
        )


def run(execute: bool) -> None:
    conn = get_transaction_connection()
    cur = conn.cursor()
    try:
        reset_tables = _existing_tables(cur, RESET_TABLES)
        sequence_tables = _existing_tables(cur, RESET_SEQUENCES_TABLES)
        all_tables = sorted(set(reset_tables + sequence_tables))
        before = _counts(cur, all_tables)

        print("PHASE2 RESET TABLES")
        for table in all_tables:
            print(f"{table}\t{before.get(table, 0)}")

        if not execute:
            print("\nDRY RUN ONLY. No data changed.")
            conn.rollback()
            return

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_schema = f"reset_backup_phase2_{stamp}"
        _backup_schema(cur, backup_schema, all_tables)

        if reset_tables:
            table_sql = ", ".join(f"public.{_quote_ident(t)}" for t in reset_tables)
            cur.execute(f"TRUNCATE TABLE {table_sql} RESTART IDENTITY CASCADE")

        for table in sequence_tables:
            cur.execute(f"TRUNCATE TABLE public.{_quote_ident(table)} RESTART IDENTITY CASCADE")

        cur.execute(RESET_INVENTORY_ALLOCATION_SQL)

        after = _counts(cur, all_tables)
        conn.commit()

        print(f"\nBACKUP_SCHEMA\t{backup_schema}")
        print("AFTER RESET")
        for table in all_tables:
            print(f"{table}\t{after.get(table, 0)}")
        print("\nDONE")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        close_connection(conn)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.execute and args.confirm != "RESET_PHASE2":
        raise SystemExit("Refusing destructive reset without --confirm RESET_PHASE2")
    run(execute=bool(args.execute))
