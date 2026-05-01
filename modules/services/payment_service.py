"""
modules/billing/services/payment_service.py
============================================
Payment allocation service — pure business logic.
NO streamlit, NO session state, NO SQL.

Depends only on:
  - modules/billing/db/billing_queries.py  (DB layer)
  - modules/core/price_qty_governor.py     (price rules)

Entry points:
    allocate_payment(selected_docs, amount, discount)  → allocations + excess
    record_payment(party_id, party_name, ...)          → PaymentResult
    get_open_docs(party_id)                            → OpenDocsResult
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
import uuid
import datetime
import logging

_log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Allocation:
    doc: Dict
    allocated_amount: float = 0.0
    allocated_discount: float = 0.0
    cleared: bool = False
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class PaymentResult:
    success: bool
    payment_no: str = ""
    error: str = ""
    allocations: List[Allocation] = field(default_factory=list)
    excess: float = 0.0
    cleared_docs: List[str] = field(default_factory=list)


@dataclass
class OpenDocsResult:
    docs: List[Dict] = field(default_factory=list)
    invoices: List[Dict] = field(default_factory=list)
    challans: List[Dict] = field(default_factory=list)
    orders: List[Dict] = field(default_factory=list)
    covered_challan_ids: set = field(default_factory=set)
    total: float = 0.0
    net_total: float = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# OPEN DOCS SERVICE
# ══════════════════════════════════════════════════════════════════════════════

def get_open_docs(party_id: str) -> OpenDocsResult:
    """
    Single entry point for all open documents of a party.
    Balances derived from payments FK — never from stored status fields.
    """
    from modules.billing.db.billing_queries import (
        resolve_name_for_party_or_patient,
        get_order_refs_for_party,
        get_open_invoices_for_party,
        get_invoices_by_order_refs,
        get_open_challans_for_party,
        get_orders_with_balance,
    )

    pid = str(party_id or "").strip()
    if not pid:
        return OpenDocsResult()

    # Resolve display name for patient fallback
    pname = resolve_name_for_party_or_patient(pid)

    # Order refs for patient invoice lookup
    uuids, nos = get_order_refs_for_party(pid, pname)
    all_refs = list(set(uuids + nos))

    # Invoices (by party_id + by order_ids array for patients)
    invoices = get_open_invoices_for_party(pid)
    invoices = [i for i in invoices if i.get("payment_status") != "PAID"]

    if all_refs:
        extra = get_invoices_by_order_refs(all_refs)
        seen = {i["id"] for i in invoices}
        invoices += [i for i in extra if i["id"] not in seen and i.get("payment_status") != "PAID"]

    # Challans (INVOICED status derived from FK, not stored status)
    challans = get_open_challans_for_party(pid)

    # Orders with balance
    orders = get_orders_with_balance(pid, pname)

    # Build covered-challan map (challan included in invoice)
    covered_challan_ids = {
        inv["challan_id"] for inv in invoices if inv.get("challan_id")
    }
    for ch in challans:
        ch["_covered_by_invoice"] = ch["id"] in covered_challan_ids

    all_docs = invoices + challans + orders
    total = round(sum(float(d.get("balance_due") or 0) for d in all_docs), 2)
    net_total = round(sum(
        float(d.get("balance_due") or 0)
        for d in all_docs if not d.get("_covered_by_invoice")
    ), 2)

    return OpenDocsResult(
        docs=all_docs, invoices=invoices, challans=challans, orders=orders,
        covered_challan_ids=covered_challan_ids,
        total=total, net_total=net_total,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ALLOCATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def allocate_payment(
    selected_docs: List[Dict],
    payment_amount: float,
    discount: float = 0.0,
) -> Tuple[List[Allocation], float]:
    """
    SAP-style waterfall allocation.
    - Oldest doc first
    - Challan covered by selected invoice → skip (avoid double-count)
    - Returns (allocations, excess)
    """
    # Build challan_ids absorbed by selected invoices
    sel_inv_challan_ids = {
        d["challan_id"]
        for d in selected_docs
        if d.get("doc_type") == "INVOICE" and d.get("challan_id")
    }

    # Sort oldest first
    sorted_docs = sorted(selected_docs, key=lambda d: str(d.get("doc_date") or "9999-12-31"))

    pool      = round(float(payment_amount), 2)
    disc_pool = round(float(discount or 0), 2)
    allocations: List[Allocation] = []

    for doc in sorted_docs:
        bal = round(float(doc.get("balance_due") or 0), 2)
        if bal <= 0:
            continue

        # Challan already in a selected invoice → skip
        if doc.get("_covered_by_invoice") and doc.get("id") in sel_inv_challan_ids:
            allocations.append(Allocation(
                doc=doc, allocated_amount=0.0, skipped=True,
                skip_reason="Included in invoice",
            ))
            continue

        if pool <= 0 and disc_pool <= 0:
            allocations.append(Allocation(doc=doc))
            continue

        # Apply discount first (on oldest doc)
        disc_applied = 0.0
        if disc_pool > 0:
            disc_applied = min(disc_pool, bal)
            disc_pool    = round(disc_pool - disc_applied, 2)
            bal          = round(bal - disc_applied, 2)

        # Apply payment
        pay_applied = min(pool, bal)
        pool        = round(pool - pay_applied, 2)
        bal_after   = round(bal - pay_applied, 2)

        allocations.append(Allocation(
            doc=doc,
            allocated_amount=pay_applied,
            allocated_discount=disc_applied,
            cleared=(bal_after <= 0.01),
        ))

    return allocations, round(pool, 2)  # pool remainder = excess


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENT RECORDING SERVICE
# ══════════════════════════════════════════════════════════════════════════════

def record_payment(
    party_id: str,
    party_name: str,
    allocations: List[Allocation],
    excess: float,
    mode: str,
    ref_no: str,
    narration: str,
    pay_date: datetime.date,
    created_by: str = "Staff",
) -> PaymentResult:
    """
    Persist one payment record per allocation + excess On Account.
    Updates invoice/challan balances via FK recalculation.
    Writes credit entry to party_ledger.
    All steps in a transaction.
    """
    from modules.billing.db.billing_queries import (
        insert_payment, update_invoice_balance,
        update_challan_balance, insert_ledger_credit,
        ensure_ledger_table,
    )

    ensure_ledger_table()

    pno = _gen_pno()
    steps = []
    cleared_docs = []
    pno_suffix = 0

    for alloc in allocations:
        if alloc.skipped:
            continue
        amt  = alloc.allocated_amount
        disc = alloc.allocated_discount
        if amt <= 0 and disc <= 0:
            continue

        doc   = alloc.doc
        dtype = doc.get("doc_type", "")
        did   = doc.get("id")
        dno   = doc.get("doc_no", "")
        pid_r = str(uuid.uuid4())
        pno_suffix += 1
        this_pno = pno if pno_suffix == 1 else f"{pno}-{pno_suffix}"

        inv_id = did if dtype == "INVOICE"    else None
        chl_id = did if dtype == "CHALLAN"    else None
        ord_id = did if dtype == "ON_ACCOUNT" else None

        pay_params = {
            "id":   pid_r,       "pno":  this_pno,
            "pid":  party_id or None, "pn": party_name,
            "iid":  inv_id,      "cid":  chl_id,    "oid": ord_id,
            "dt":   pay_date,    "mode": mode,
            "amt":  amt,         "ref":  ref_no or None,
            "nar":  narration or "Payment received",
            "by":   created_by,
        }

        ledger_params = {
            "pid": party_id or None, "pn": party_name,
            "dt":  pay_date,   "et": "PAYMENT",
            "rid": pid_r,      "rno": this_pno,
            "amt": amt,
            "nar": f"{mode} — {ref_no or dno or narration or 'Payment'}",
            "by":  created_by,
        }

        if not insert_payment(pay_params):
            return PaymentResult(success=False, error=f"Failed to insert payment for {dno}")
        insert_ledger_credit(ledger_params)

        # Discount ledger entry
        if disc > 0:
            insert_ledger_credit({
                "pid": party_id or None, "pn": party_name,
                "dt": pay_date, "et": "DISCOUNT",
                "rid": str(uuid.uuid4()), "rno": this_pno + "-DISC",
                "amt": disc,
                "nar": f"Discount on {dno or this_pno}",
                "by": created_by,
            })

        # Recalculate balance from FK
        if inv_id:
            update_invoice_balance(inv_id)
        if chl_id:
            update_challan_balance(chl_id)

        if alloc.cleared:
            cleared_docs.append(dno)

    # Excess → On Account
    if excess > 0.01:
        exc_id  = str(uuid.uuid4())
        exc_pno = pno + "-OA"
        insert_payment({
            "id": exc_id, "pno": exc_pno,
            "pid": party_id or None, "pn": party_name,
            "iid": None, "cid": None, "oid": None,
            "dt": pay_date, "mode": mode,
            "amt": excess, "ref": ref_no or None,
            "nar": "Excess credit — On Account",
            "by": created_by,
        })
        insert_ledger_credit({
            "pid": party_id or None, "pn": party_name,
            "dt": pay_date, "et": "PAYMENT",
            "rid": exc_id, "rno": exc_pno,
            "amt": excess,
            "nar": f"Excess On Account — {mode}",
            "by": created_by,
        })

    return PaymentResult(
        success=True,
        payment_no=pno,
        allocations=allocations,
        excess=excess,
        cleared_docs=cleared_docs,
    )


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _gen_pno() -> str:
    try:
        from modules.db.order_number_registry import alloc_doc_number
        return alloc_doc_number("PAYMENT")
    except Exception:
        return "PAY/" + datetime.datetime.now().strftime("%y%m%d%H%M%S")
