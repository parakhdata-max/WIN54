"""
modules/billing/services/challan_service.py
============================================
Challan & Invoice service — pure business logic.
NO streamlit, NO session state, NO SQL.

Entry points:
    create_challan(party_id, order_ids, ...) → challan_id | None
    create_invoice(challan_id, party_id, ...) → invoice_no | None
    get_party_billing_preference(party_id) → str
"""

from __future__ import annotations
from typing import List, Dict, Optional, Tuple
import uuid
import datetime
import logging

_log = logging.getLogger(__name__)


def get_party_billing_preference(party_id: str) -> str:
    """
    Returns billing category: ON_COMPLETION | FULL_ADVANCE | ADVANCE_BALANCE |
    PRE_PAYMENT | ON_ACCOUNT | DIRECT_INVOICE
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT COALESCE(billing_category, payment_mode, 'ON_COMPLETION') AS cat
            FROM parties WHERE id = %s::uuid LIMIT 1
        """, (party_id,)) or []
        return rows[0]["cat"] if rows else "ON_COMPLETION"
    except Exception:
        return "ON_COMPLETION"


def get_pending_orders_for_party(party_id: str) -> List[Dict]:
    """Orders ready for challan/invoice creation."""
    try:
        from modules.sql_adapter import run_query
        return run_query("""
            SELECT o.id::text, o.order_no, o.total_value, o.status,
                   o.order_type, o.created_at::date AS order_date,
                   COALESCE(p.product_name,'—') AS product_name
            FROM orders o
            LEFT JOIN order_lines ol ON ol.order_id = o.id
            LEFT JOIN products p    ON p.id = ol.product_id
            WHERE o.party_id = %s::uuid
              AND o.status IN ('CONFIRMED','READY','READY_FOR_BILLING','IN_PRODUCTION')
              AND COALESCE(o.is_deleted,FALSE) = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM challans c
                  WHERE %s::uuid = ANY(c.order_ids::uuid[])
                    AND COALESCE(c.is_deleted,FALSE) = FALSE
              )
            GROUP BY o.id, o.order_no, o.total_value, o.status, o.order_type, o.created_at, p.product_name
            ORDER BY o.created_at
        """, (party_id, party_id)) or []
    except Exception as e:
        _log.warning(f"[challan_service] get_pending_orders: {e}")
        return []


def validate_challan_gate(
    party_id: str,
    billing_category: str,
    advance_paid: float = 0.0,
    credit_limit: float = 0.0,
    current_outstanding: float = 0.0,
) -> Tuple[bool, str]:
    """
    Business rule gate before challan creation.
    Returns (allowed, reason).
    """
    if billing_category == "FULL_ADVANCE" and advance_paid <= 0:
        return False, "FULL_ADVANCE party — payment must be recorded before challan."

    if billing_category == "ON_ACCOUNT" and credit_limit > 0:
        if current_outstanding >= credit_limit:
            return False, (
                f"Credit limit ₹{credit_limit:,.0f} reached. "
                f"Outstanding: ₹{current_outstanding:,.0f}. Collect payment first."
            )

    return True, ""


def record_debit_on_invoice(
    party_id: str,
    invoice_id: str,
    invoice_no: str,
    grand_total: float,
    invoice_date,
    created_by: str = "System",
) -> None:
    """
    Write DR entry to party_ledger when invoice is raised.
    Called from challan_invoice_manager.create_invoice().
    """
    from modules.billing.db.billing_queries import insert_ledger_debit, ensure_ledger_table
    ensure_ledger_table()

    # Resolve party_name for ledger
    party_name = ""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("SELECT party_name FROM parties WHERE id=%s::uuid LIMIT 1",
                         (party_id,)) or []
        party_name = rows[0]["party_name"] if rows else ""
    except Exception:
        pass

    insert_ledger_debit(
        party_id=party_id,
        party_name=party_name,
        doc_type="INVOICE",
        doc_id=invoice_id,
        doc_no=invoice_no,
        amount=grand_total,
        doc_date=invoice_date,
        narration=f"Invoice raised — {invoice_no}",
        created_by=created_by,
    )
