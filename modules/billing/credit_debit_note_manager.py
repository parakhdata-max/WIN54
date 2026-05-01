"""
modules/billing/credit_debit_note_manager.py
=============================================
Credit Note (CN) and Debit Note (DN) service layer.

GST Compliance:
    Section 34, CGST Act 2017
    Rule 53, CGST Rules 2017
    GSTR-1 Table 9B (B2B CDN), Table 10 (B2C CDN)

Tally Compatibility:
    Output format matches Tally ERP 9 / Tally Prime voucher import structure.
    Tally XML export via export_cdn_for_tally().

Number Format:
    Credit Note : CN/<FY>/<NNNN>   e.g. CN/2526/0001
    Debit Note  : DN/<FY>/<NNNN>   e.g. DN/2526/0001
    FY = Indian financial year e.g. 2526 = Apr 2025 – Mar 2026

Tax Logic:
    Intra-state supply  → CGST + SGST (equal split of GST rate)
    Inter-state supply  → IGST (full GST rate)
    Supply type derived from place_of_supply vs SUPPLIER state.
    Supplier state code: configured in OUR_STATE_CODE constant.
"""

from datetime import date, datetime
from typing import Optional, List, Dict, Tuple
import uuid as _uuid
import logging
import csv
import io

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────
# Change OUR_STATE_CODE to match your GST registration state.
# 27 = Maharashtra. Full list: GST state codes India.
OUR_STATE_CODE = "27"
OUR_STATE_NAME = "Maharashtra"

# GST CN/DN reason codes (Section 34, CGST Act)
CN_REASONS = {
    "RETURN":        "Sales Return / Goods Returned by Buyer",
    "RATE_DIFF":     "Price Charged Higher Than Agreed Rate",
    "DEFICIENCY":    "Deficiency in Services Supplied",
    "POST_DISCOUNT": "Post-Sale Discount (not deducted at invoice)",
    "CANCELLATION":  "Cancellation of Supply",
    "OTHER":         "Other (specify in remarks)",
}

DN_REASONS = {
    "SHORT_BILL":    "Original Invoice Raised for Lower Amount",
    "RATE_REVISION": "Upward Revision in Agreed Rate",
    "EXTRA_CHARGES": "Additional Freight / Packing / Other Charges",
    "CORRECTION":    "Correction — Invoice Understated",
    "INTEREST":      "Interest on Delayed Payment",
    "OTHER":         "Other (specify in remarks)",
}


# ── DB helpers ────────────────────────────────────────────────────────

def _q(sql: str, params: dict = None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        log.error("_q failed: %s", e)
        try:
            import streamlit as _st
            _st.caption(f"Query error: {e}")
        except Exception:
            pass
        return []


def _write(sql: str, params: dict = None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        log.error("_write failed: %s", e)
        return False


def _run_tx(steps: list) -> bool:
    try:
        from modules.sql_adapter import run_transaction
        run_transaction(steps)
        return True
    except ImportError:
        pass
    except Exception as e:
        log.error("run_transaction failed: %s", e)
        raise  # Re-raise so caller sees actual error

    # Fallback: run each step individually
    try:
        from modules.sql_adapter import run_write
        for sql, params in steps:
            run_write(sql, params or {})
        return True
    except Exception as e:
        log.error("Transaction fallback failed: %s", e)
        raise  # Re-raise so caller sees actual error
    except Exception as e:
        log.error("Transaction failed: %s", e)
        return False


# ── Tax Split ─────────────────────────────────────────────────────────

def split_gst(taxable: float, gst_percent: float, place_of_supply: str) -> Dict:
    """
    Split GST into CGST+SGST (intra-state) or IGST (inter-state).

    Args:
        taxable         : base amount before tax
        gst_percent     : total GST rate e.g. 12 (means 12%)
        place_of_supply : state code or state name of buyer

    Returns:
        {
            cgst_percent, sgst_percent, igst_percent,
            cgst_amount,  sgst_amount,  igst_amount,
            total_tax,    grand_total
        }
    """
    taxable = round(float(taxable or 0), 2)
    gst_pct = round(float(gst_percent or 0), 2)

    # Determine inter vs intra state
    pos_code = _state_name_to_code(str(place_of_supply or ""))
    is_inter  = (pos_code != OUR_STATE_CODE) if pos_code else False

    if is_inter:
        igst   = round(taxable * gst_pct / 100, 2)
        cgst   = 0.0
        sgst   = 0.0
        c_pct  = 0.0
        s_pct  = 0.0
        i_pct  = gst_pct
    else:
        igst   = 0.0
        half   = gst_pct / 2
        cgst   = round(taxable * half / 100, 2)
        sgst   = round(taxable * half / 100, 2)
        c_pct  = half
        s_pct  = half
        i_pct  = 0.0

    total_tax   = round(cgst + sgst + igst, 2)
    grand_total = round(taxable + total_tax, 2)

    return {
        "cgst_percent":  c_pct,
        "sgst_percent":  s_pct,
        "igst_percent":  i_pct,
        "cgst_amount":   cgst,
        "sgst_amount":   sgst,
        "igst_amount":   igst,
        "total_tax":     total_tax,
        "grand_total":   grand_total,
    }


def _state_name_to_code(value: str) -> Optional[str]:
    """Map state name or numeric code string to 2-digit code."""
    # If already a 2-digit code
    v = value.strip()
    if v.isdigit() and len(v) <= 2:
        return v.zfill(2)

    # Common state name map
    _MAP = {
        "maharashtra": "27", "delhi": "07", "karnataka": "29",
        "tamil nadu": "33", "telangana": "36", "gujarat": "24",
        "rajasthan": "08", "uttar pradesh": "09", "west bengal": "19",
        "madhya pradesh": "23", "andhra pradesh": "37", "kerala": "32",
        "haryana": "06", "punjab": "03", "odisha": "21",
        "assam": "18", "jharkhand": "20", "uttarakhand": "05",
        "chhattisgarh": "22", "himachal pradesh": "02", "goa": "30",
        "bihar": "10",
    }
    return _MAP.get(v.lower())


# ── Invoice lookup ────────────────────────────────────────────────────

def get_invoice_for_cdn(invoice_no: str) -> Optional[Dict]:
    """
    Look up an invoice by number. Returns header + line totals.
    Used to pre-fill CDN form.
    """
    _lookup_no = invoice_no.strip().upper()
    try:
        from modules.sql_adapter import run_query as _rq_direct
        rows = _rq_direct("""
            SELECT i.id,
                   i.invoice_no,
                   i.invoice_date,
                   i.party_id,
                   COALESCE(p.party_name, 'Customer') AS party_name,
                   COALESCE(p.gstin, '')              AS party_gstin,
                   COALESCE(p.party_name, '')         AS tally_ledger,
                   i.total_amount,
                   i.total_tax,
                   i.grand_total,
                   i.status,
                   i.order_ids
            FROM invoices i
            LEFT JOIN parties p ON p.id = i.party_id
            WHERE i.invoice_no = %(no)s
              AND COALESCE(i.is_deleted, FALSE) = FALSE
            LIMIT 1
        """, {"no": _lookup_no}) or []
    except Exception as _e:
        log.error("get_invoice_for_cdn failed for %s: %s", _lookup_no, _e)
        try:
            import streamlit as _st
            _st.error(f"Invoice lookup error: {_e}")
        except Exception:
            pass
        return None

    # Enrich party_name from orders for retail invoices
    if rows and rows[0].get("party_name") in ("Customer", ""):
        order_ids = rows[0].get("order_ids") or []
        if order_ids:
            pname_rows = _q("""
                SELECT COALESCE(party_name, patient_name, 'Customer') AS pname
                FROM orders
                WHERE order_no = ANY(%(oids)s)
                   OR id::text = ANY(%(oids)s)
                LIMIT 1
            """, {"oids": order_ids})
            if pname_rows:
                rows[0]["party_name"] = pname_rows[0]["pname"]
                rows[0]["tally_ledger"] = pname_rows[0]["pname"]
    return rows[0] if rows else None


def get_invoice_lines_for_cdn(invoice_id: str) -> List[Dict]:
    """Return line items of an invoice for partial CDN selection."""
    return _q("""
        SELECT il.id,
               COALESCE(il.product_name, 'Item')                  AS product_name,
               COALESCE(il.quantity, 1)                           AS quantity,
               COALESCE(il.unit_price, 0)                         AS unit_price,
               -- taxable_amount = base EXCLUDING GST (for CDN computation)
               -- if line_total > total_price: total_price is excl, line_total is incl
               -- if line_total = total_price: price is GST-inclusive, back-calc base
               CASE
                   WHEN COALESCE(il.line_total, 0) > COALESCE(il.total_price, 0) + 0.01
                   THEN COALESCE(il.total_price, 0)
                   WHEN COALESCE(il.tax_rate, il.gst_percent, ol.gst_percent, p.gst_percent, 0) > 0
                   THEN ROUND(COALESCE(il.total_price,0) /
                        (1 + COALESCE(il.tax_rate, il.gst_percent,
                                      ol.gst_percent, p.gst_percent, 0) / 100), 2)
                   ELSE COALESCE(il.total_price, 0)
               END                                                AS taxable_amount,
               COALESCE(il.tax_rate, il.gst_percent, ol.gst_percent, p.gst_percent, 0) AS gst_percent,
               COALESCE(il.tax_amount, 0)                         AS tax_amount,
               COALESCE(il.line_total, il.total_price, 0)         AS line_total,
               COALESCE(p.hsn_code, '')                           AS hsn_sac_code,
               COALESCE(il.eye_side, '')                          AS eye_side
        FROM invoice_lines il
        LEFT JOIN order_lines ol ON ol.id = il.order_line_id
        LEFT JOIN products p     ON p.id  = ol.product_id
        WHERE il.invoice_id = %(iid)s::uuid
          AND COALESCE(il.is_deleted, FALSE) = FALSE
        ORDER BY il.id
    """, {"iid": invoice_id})


def search_invoices(query: str, party_id: Optional[str] = None) -> List[Dict]:
    """
    Search invoices by number or party name (for CDN form autocomplete).
    """
    params: dict = {"q": f"%{query.upper()}%"}
    party_clause  = ""
    if party_id:
        party_clause = "AND i.party_id = %(pid)s::uuid"
        params["pid"] = party_id

    return _q(f"""
        SELECT i.invoice_no,
               i.id,
               i.invoice_date,
               i.grand_total,
               COALESCE(p.party_name, '') AS party_name
        FROM invoices i
        LEFT JOIN parties p ON p.id = i.party_id
        WHERE (UPPER(i.invoice_no) LIKE %(q)s
               OR UPPER(COALESCE(p.party_name,'')) LIKE %(q)s)
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND i.status NOT IN ('CANCELLED')
          {party_clause}
        ORDER BY i.invoice_date DESC
        LIMIT 30
    """, params)


# ── Number generators ─────────────────────────────────────────────────

def _gen_cn_number() -> str:
    try:
        from modules.db.order_number_registry import alloc_doc_number
        return alloc_doc_number("CREDIT_NOTE")
    except Exception:
        return f"CN/MANUAL/{_uuid.uuid4().hex[:6].upper()}"


def _gen_dn_number() -> str:
    try:
        from modules.db.order_number_registry import alloc_doc_number
        return alloc_doc_number("DEBIT_NOTE")
    except Exception:
        return f"DN/MANUAL/{_uuid.uuid4().hex[:6].upper()}"


# ── Credit Note ───────────────────────────────────────────────────────

def create_credit_note(
    *,
    invoice_no:    str,
    invoice_id:    Optional[str],
    order_id:      Optional[str],
    party_id:      Optional[str],
    party_name:    str,
    party_gstin:   str,
    place_of_supply: str,
    supply_type:   str,
    reason:        str,
    reason_detail: str = "",
    lines:         List[Dict],         # [{product_name, qty, unit_price, taxable_amount, gst_percent, hsn_sac_code, invoice_line_id?}]
    original_invoice_date: Optional[date] = None,
    remarks:       str = "",
    created_by:    str = "System",
) -> Tuple[bool, str]:
    """
    Create a GST Credit Note atomically.

    Args:
        lines : Each line must have:
                  product_name, quantity, unit_price,
                  taxable_amount (base excl. GST),
                  gst_percent    (total GST %)
                  hsn_sac_code   (optional)

    Returns:
        (success: bool, cn_number_or_error_message: str)
    """
    if not lines:
        return False, "At least one line item is required."
    if reason not in CN_REASONS:
        return False, f"Invalid reason '{reason}'. Must be one of: {list(CN_REASONS)}"

    cn_id     = str(_uuid.uuid4())
    cn_number = _gen_cn_number()

    # Aggregate tax across all lines
    agg = _aggregate_lines(lines, place_of_supply)

    narration = _build_tally_narration("CN", cn_number, invoice_no, party_name, reason)

    tx: list = []

    # 1. Credit note header
    tx.append(("""
        INSERT INTO credit_notes (
            id, cn_number, invoice_id, invoice_no, order_id,
            party_id, party_name, party_gstin,
            place_of_supply, supply_type,
            cn_date, original_invoice_date,
            reason, reason_detail,
            taxable_amount, cgst_amount, sgst_amount, igst_amount,
            cess_amount, total_tax_amount, grand_total,
            tally_ledger_name, tally_narration,
            status, created_by, remarks
        ) VALUES (
            %(id)s, %(cn_number)s, %(invoice_id)s, %(invoice_no)s, %(order_id)s,
            %(party_id)s, %(party_name)s, %(party_gstin)s,
            %(pos)s, %(supply_type)s,
            %(cn_date)s, %(orig_date)s,
            %(reason)s, %(reason_detail)s,
            %(taxable)s, %(cgst)s, %(sgst)s, %(igst)s,
            0, %(total_tax)s, %(grand_total)s,
            %(tally_ledger)s, %(narration)s,
            'CONFIRMED', %(created_by)s, %(remarks)s
        )
    """, {
        "id":            cn_id,
        "cn_number":     cn_number,
        "invoice_id":    invoice_id,
        "invoice_no":    invoice_no.strip().upper(),
        "order_id":      order_id,
        "party_id":      party_id,
        "party_name":    party_name,
        "party_gstin":   party_gstin,
        "pos":           place_of_supply,
        "supply_type":   supply_type.upper(),
        "cn_date":       date.today(),
        "orig_date":     original_invoice_date,
        "reason":        reason,
        "reason_detail": reason_detail,
        "taxable":       agg["taxable_amount"],
        "cgst":          agg["cgst_amount"],
        "sgst":          agg["sgst_amount"],
        "igst":          agg["igst_amount"],
        "total_tax":     agg["total_tax_amount"],
        "grand_total":   agg["grand_total"],
        "tally_ledger":  party_name,
        "narration":     narration,
        "created_by":    created_by,
        "remarks":       remarks,
    }))

    # 2. Credit note lines
    for line in lines:
        split = split_gst(
            taxable        = float(line.get("taxable_amount") or 0),
            gst_percent    = float(line.get("gst_percent") or 0),
            place_of_supply= place_of_supply,
        )
        tx.append(("""
            INSERT INTO credit_note_lines (
                cn_id, invoice_line_id, order_line_id, product_id,
                product_name, hsn_sac_code, quantity, unit_price,
                taxable_amount,
                gst_percent, cgst_percent, sgst_percent, igst_percent,
                cgst_amount, sgst_amount, igst_amount, line_total
            ) VALUES (
                %(cn_id)s, %(il_id)s, %(ol_id)s, %(prod_id)s,
                %(name)s, %(hsn)s, %(qty)s, %(uprice)s,
                %(taxable)s,
                %(gst_pct)s, %(c_pct)s, %(s_pct)s, %(i_pct)s,
                %(c_amt)s, %(s_amt)s, %(i_amt)s, %(line_total)s
            )
        """, {
            "cn_id":    cn_id,
            "il_id":    line.get("invoice_line_id"),
            "ol_id":    line.get("order_line_id"),
            "prod_id":  line.get("product_id"),
            "name":     line.get("product_name") or "",
            "hsn":      line.get("hsn_sac_code") or "",
            "qty":      float(line.get("quantity") or 0),
            "uprice":   float(line.get("unit_price") or 0),
            "taxable":  float(line.get("taxable_amount") or 0),
            "gst_pct":  float(line.get("gst_percent") or 0),
            "c_pct":    split["cgst_percent"],
            "s_pct":    split["sgst_percent"],
            "i_pct":    split["igst_percent"],
            "c_amt":    split["cgst_amount"],
            "s_amt":    split["sgst_amount"],
            "i_amt":    split["igst_amount"],
            "line_total": split["grand_total"],
        }))

    # 3. Document ledger — skip if no order_id (invoice-level CN has no direct order)
    if order_id:
        tx.append(("""
            INSERT INTO document_ledger
                (doc_type, doc_id, doc_no, order_id, order_line_id,
                 party_id, product_id, quantity, base_amount, tax_amount, total_amount)
            VALUES
                ('CN', %(did)s::uuid, %(dno)s, %(oid)s::uuid, NULL,
                 %(pid)s, NULL, 0, %(base)s, %(tax)s, %(total)s)
        """, {
            "did":   cn_id,
            "dno":   cn_number,
            "oid":   order_id,
            "pid":   party_id,
            "base":  agg["taxable_amount"],
            "tax":   agg["total_tax_amount"],
            "total": agg["grand_total"],
        }))

    try:
        ok = _run_tx(tx)
    except Exception as _tx_err:
        return False, f"Save failed: {_tx_err}"
    if ok:
        log.info("Credit Note created: %s  party=%s  total=%.2f",
                 cn_number, party_name, agg["grand_total"])
        return True, cn_number
    return False, "Database transaction failed — credit note not saved."


# ── Debit Note ────────────────────────────────────────────────────────

def create_debit_note(
    *,
    invoice_no:    str,
    invoice_id:    Optional[str],
    order_id:      Optional[str],
    party_id:      Optional[str],
    party_name:    str,
    party_gstin:   str,
    place_of_supply: str,
    supply_type:   str,
    reason:        str,
    reason_detail: str = "",
    lines:         List[Dict],
    original_invoice_date: Optional[date] = None,
    remarks:       str = "",
    created_by:    str = "System",
) -> Tuple[bool, str]:
    """
    Create a GST Debit Note atomically.
    Same signature as create_credit_note().
    """
    if not lines:
        return False, "At least one line item is required."
    if reason not in DN_REASONS:
        return False, f"Invalid reason '{reason}'. Must be one of: {list(DN_REASONS)}"

    dn_id     = str(_uuid.uuid4())
    dn_number = _gen_dn_number()

    agg      = _aggregate_lines(lines, place_of_supply)
    narration = _build_tally_narration("DN", dn_number, invoice_no, party_name, reason)

    tx: list = []

    tx.append(("""
        INSERT INTO debit_notes (
            id, dn_number, invoice_id, invoice_no, order_id,
            party_id, party_name, party_gstin,
            place_of_supply, supply_type,
            dn_date, original_invoice_date,
            reason, reason_detail,
            taxable_amount, cgst_amount, sgst_amount, igst_amount,
            cess_amount, total_tax_amount, grand_total,
            tally_ledger_name, tally_narration,
            status, created_by, remarks
        ) VALUES (
            %(id)s, %(dn_number)s, %(invoice_id)s, %(invoice_no)s, %(order_id)s,
            %(party_id)s, %(party_name)s, %(party_gstin)s,
            %(pos)s, %(supply_type)s,
            %(dn_date)s, %(orig_date)s,
            %(reason)s, %(reason_detail)s,
            %(taxable)s, %(cgst)s, %(sgst)s, %(igst)s,
            0, %(total_tax)s, %(grand_total)s,
            %(tally_ledger)s, %(narration)s,
            'CONFIRMED', %(created_by)s, %(remarks)s
        )
    """, {
        "id":            dn_id,
        "dn_number":     dn_number,
        "invoice_id":    invoice_id,
        "invoice_no":    invoice_no.strip().upper(),
        "order_id":      order_id,
        "party_id":      party_id,
        "party_name":    party_name,
        "party_gstin":   party_gstin,
        "pos":           place_of_supply,
        "supply_type":   supply_type.upper(),
        "dn_date":       date.today(),
        "orig_date":     original_invoice_date,
        "reason":        reason,
        "reason_detail": reason_detail,
        "taxable":       agg["taxable_amount"],
        "cgst":          agg["cgst_amount"],
        "sgst":          agg["sgst_amount"],
        "igst":          agg["igst_amount"],
        "total_tax":     agg["total_tax_amount"],
        "grand_total":   agg["grand_total"],
        "tally_ledger":  party_name,
        "narration":     narration,
        "created_by":    created_by,
        "remarks":       remarks,
    }))

    for line in lines:
        split = split_gst(
            taxable        = float(line.get("taxable_amount") or 0),
            gst_percent    = float(line.get("gst_percent") or 0),
            place_of_supply= place_of_supply,
        )
        tx.append(("""
            INSERT INTO debit_note_lines (
                dn_id, invoice_line_id, order_line_id, product_id,
                product_name, hsn_sac_code, quantity, unit_price,
                taxable_amount,
                gst_percent, cgst_percent, sgst_percent, igst_percent,
                cgst_amount, sgst_amount, igst_amount, line_total
            ) VALUES (
                %(dn_id)s, %(il_id)s, %(ol_id)s, %(prod_id)s,
                %(name)s, %(hsn)s, %(qty)s, %(uprice)s,
                %(taxable)s,
                %(gst_pct)s, %(c_pct)s, %(s_pct)s, %(i_pct)s,
                %(c_amt)s, %(s_amt)s, %(i_amt)s, %(line_total)s
            )
        """, {
            "dn_id":    dn_id,
            "il_id":    line.get("invoice_line_id"),
            "ol_id":    line.get("order_line_id"),
            "prod_id":  line.get("product_id"),
            "name":     line.get("product_name") or "",
            "hsn":      line.get("hsn_sac_code") or "",
            "qty":      float(line.get("quantity") or 0),
            "uprice":   float(line.get("unit_price") or 0),
            "taxable":  float(line.get("taxable_amount") or 0),
            "gst_pct":  float(line.get("gst_percent") or 0),
            "c_pct":    split["cgst_percent"],
            "s_pct":    split["sgst_percent"],
            "i_pct":    split["igst_percent"],
            "c_amt":    split["cgst_amount"],
            "s_amt":    split["sgst_amount"],
            "i_amt":    split["igst_amount"],
            "line_total": split["grand_total"],
        }))

    tx.append(("""
        INSERT INTO document_ledger
            (doc_type, doc_id, doc_no, order_id, order_line_id,
             party_id, product_id, quantity, base_amount, tax_amount, total_amount)
        VALUES
            ('DN', %(did)s::uuid, %(dno)s, %(oid)s, NULL,
             %(pid)s, NULL, 0, %(base)s, %(tax)s, %(total)s)
    """, {
        "did":   dn_id,
        "dno":   dn_number,
        "oid":   order_id,
        "pid":   party_id,
        "base":  agg["taxable_amount"],
        "tax":   agg["total_tax_amount"],
        "total": agg["grand_total"],
    }))

    ok = _run_tx(tx)
    if ok:
        log.info("Debit Note created: %s  party=%s  total=%.2f",
                 dn_number, party_name, agg["grand_total"])
        return True, dn_number
    return False, "Database transaction failed — debit note not saved."


# ── Query / Listing ───────────────────────────────────────────────────

def get_credited_line_ids(invoice_id: str) -> dict:
    """
    Returns dict of {invoice_line_id: cn_number} for lines already credited.
    Used to disable checkboxes for already-credited lines in CDN form.
    """
    rows = _q("""
        SELECT cnl.invoice_line_id::text, cn.cn_number
        FROM credit_note_lines cnl
        JOIN credit_notes cn ON cn.id = cnl.cn_id
        WHERE cn.invoice_id = %(iid)s::uuid
          AND COALESCE(cn.is_deleted, FALSE) = FALSE
          AND cn.status != 'CANCELLED'
          AND cnl.invoice_line_id IS NOT NULL
        ORDER BY cn.created_at DESC
    """, {"iid": invoice_id})
    return {r["invoice_line_id"]: r["cn_number"] for r in rows}


def get_credit_notes_for_invoice(invoice_id: str) -> list:
    """Get all active credit notes issued against a specific invoice."""
    return _q("""
        SELECT cn_number, cn_date, grand_total, status, reason
        FROM credit_notes
        WHERE invoice_id = %(iid)s::uuid
          AND COALESCE(is_deleted, FALSE) = FALSE
          AND status != 'CANCELLED'
        ORDER BY created_at DESC
    """, {"iid": invoice_id})


def list_credit_notes(
    party_id:  Optional[str] = None,
    status:    Optional[str] = None,
    from_date: Optional[date] = None,
    to_date:   Optional[date] = None,
    limit:     int = 200,
) -> List[Dict]:
    """List credit notes with optional filters."""
    clauses  = ["COALESCE(cn.is_deleted, FALSE) = FALSE"]
    params: dict = {}

    if party_id:
        clauses.append("cn.party_id = %(pid)s::uuid")
        params["pid"] = party_id
    if status:
        clauses.append("cn.status = %(status)s")
        params["status"] = status.upper()
    if from_date:
        clauses.append("cn.cn_date >= %(from_d)s")
        params["from_d"] = from_date
    if to_date:
        clauses.append("cn.cn_date <= %(to_d)s")
        params["to_d"] = to_date

    where = " AND ".join(clauses)
    return _q(f"""
        SELECT cn.id, cn.cn_number, cn.cn_date,
               cn.invoice_no, cn.party_name, cn.party_gstin,
               cn.reason, cn.taxable_amount,
               cn.cgst_amount, cn.sgst_amount, cn.igst_amount,
               cn.total_tax_amount, cn.grand_total,
               cn.supply_type, cn.place_of_supply,
               cn.status, cn.tally_exported_at, cn.created_by
        FROM credit_notes cn
        WHERE {where}
        ORDER BY cn.cn_date DESC, cn.cn_number DESC
        LIMIT {int(limit)}
    """, params)


def list_debit_notes(
    party_id:  Optional[str] = None,
    status:    Optional[str] = None,
    from_date: Optional[date] = None,
    to_date:   Optional[date] = None,
    limit:     int = 200,
) -> List[Dict]:
    """List debit notes with optional filters."""
    clauses  = ["COALESCE(dn.is_deleted, FALSE) = FALSE"]
    params: dict = {}

    if party_id:
        clauses.append("dn.party_id = %(pid)s::uuid")
        params["pid"] = party_id
    if status:
        clauses.append("dn.status = %(status)s")
        params["status"] = status.upper()
    if from_date:
        clauses.append("dn.dn_date >= %(from_d)s")
        params["from_d"] = from_date
    if to_date:
        clauses.append("dn.dn_date <= %(to_d)s")
        params["to_d"] = to_date

    where = " AND ".join(clauses)
    return _q(f"""
        SELECT dn.id, dn.dn_number, dn.dn_date,
               dn.invoice_no, dn.party_name, dn.party_gstin,
               dn.reason, dn.taxable_amount,
               dn.cgst_amount, dn.sgst_amount, dn.igst_amount,
               dn.total_tax_amount, dn.grand_total,
               dn.supply_type, dn.place_of_supply,
               dn.status, dn.tally_exported_at, dn.created_by
        FROM debit_notes dn
        WHERE {where}
        ORDER BY dn.dn_date DESC, dn.dn_number DESC
        LIMIT {int(limit)}
    """, params)


def get_cn_detail(cn_id: str) -> Optional[Dict]:
    rows = _q("SELECT * FROM credit_notes WHERE id = %(id)s::uuid", {"id": cn_id})
    if not rows:
        return None
    doc = rows[0]
    doc["lines"] = _q("SELECT * FROM credit_note_lines WHERE cn_id = %(id)s::uuid ORDER BY id", {"id": cn_id})
    return doc


def get_dn_detail(dn_id: str) -> Optional[Dict]:
    rows = _q("SELECT * FROM debit_notes WHERE id = %(id)s::uuid", {"id": dn_id})
    if not rows:
        return None
    doc = rows[0]
    doc["lines"] = _q("SELECT * FROM debit_note_lines WHERE dn_id = %(id)s::uuid ORDER BY id", {"id": dn_id})
    return doc


def cancel_cdn(doc_type: str, doc_id: str, cancelled_by: str) -> bool:
    """
    Soft-cancel a credit or debit note.
    doc_type: 'CN' or 'DN'
    A cancelled note cannot be Tally-exported.
    """
    table = "credit_notes" if doc_type == "CN" else "debit_notes"
    return _write(f"""
        UPDATE {table}
        SET status     = 'CANCELLED',
            deleted_at = NOW(),
            deleted_by = %(by)s
        WHERE id = %(id)s::uuid
          AND status != 'CANCELLED'
    """, {"by": cancelled_by, "id": doc_id})


# ── Tally Export ──────────────────────────────────────────────────────

def export_cdn_for_tally(
    doc_type:   str,              # 'CN' | 'DN' | 'BOTH'
    from_date:  Optional[date],
    to_date:    Optional[date],
    mark_exported: bool = True,
) -> str:
    """
    Generate a Tally-compatible CSV string for credit/debit notes.

    Tally Prime accepts CSV import via the Data Exchange feature.
    Format: Voucher Type, Date, Voucher No, Party Ledger, Amount,
            CGST, SGST, IGST, Reference Invoice, Narration

    Returns: CSV string (UTF-8, BOM for Excel compatibility)
    """
    rows: List[Dict] = []

    if doc_type in ("CN", "BOTH"):
        cns = list_credit_notes(from_date=from_date, to_date=to_date)
        for r in cns:
            if r.get("status") == "CONFIRMED":
                rows.append({**r, "_voucher_type": "Credit Note"})

    if doc_type in ("DN", "BOTH"):
        dns = list_debit_notes(from_date=from_date, to_date=to_date)
        for r in dns:
            if r.get("status") == "CONFIRMED":
                rows.append({**r, "_voucher_type": "Debit Note"})

    if not rows:
        return ""

    output = io.StringIO()
    output.write("\ufeff")  # UTF-8 BOM for Excel

    fieldnames = [
        "Voucher Type", "Voucher Date", "Voucher Number",
        "Party Ledger", "Party GSTIN",
        "Ref Invoice No", "Ref Invoice Date",
        "Place of Supply", "Supply Type", "Reason",
        "Taxable Amount", "CGST", "SGST", "IGST",
        "Total Tax", "Grand Total",
        "Narration",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    export_ids: Dict[str, List[str]] = {"CN": [], "DN": []}

    for r in rows:
        vtype  = r.get("_voucher_type", "")
        doc_no = r.get("cn_number") or r.get("dn_number") or ""
        dtype  = "CN" if "Credit" in vtype else "DN"
        export_ids[dtype].append(str(r["id"]))

        writer.writerow({
            "Voucher Type":     vtype,
            "Voucher Date":     str(r.get("cn_date") or r.get("dn_date") or ""),
            "Voucher Number":   doc_no,
            "Party Ledger":     r.get("party_name", ""),
            "Party GSTIN":      r.get("party_gstin", ""),
            "Ref Invoice No":   r.get("invoice_no", ""),
            "Ref Invoice Date": str(r.get("original_invoice_date", "") or ""),
            "Place of Supply":  r.get("place_of_supply", ""),
            "Supply Type":      r.get("supply_type", ""),
            "Reason":           CN_REASONS.get(r.get("reason",""), r.get("reason",""))
                                if "Credit" in vtype else
                                DN_REASONS.get(r.get("reason",""), r.get("reason","")),
            "Taxable Amount":   f"{float(r.get('taxable_amount') or 0):.2f}",
            "CGST":             f"{float(r.get('cgst_amount') or 0):.2f}",
            "SGST":             f"{float(r.get('sgst_amount') or 0):.2f}",
            "IGST":             f"{float(r.get('igst_amount') or 0):.2f}",
            "Total Tax":        f"{float(r.get('total_tax_amount') or 0):.2f}",
            "Grand Total":      f"{float(r.get('grand_total') or 0):.2f}",
            "Narration":        r.get("tally_narration", ""),
        })

    # Mark as exported
    if mark_exported:
        now = datetime.now().isoformat()
        for cn_id in export_ids["CN"]:
            _write("UPDATE credit_notes SET tally_exported_at = %(t)s WHERE id = %(id)s::uuid",
                   {"t": now, "id": cn_id})
        for dn_id in export_ids["DN"]:
            _write("UPDATE debit_notes SET tally_exported_at = %(t)s WHERE id = %(id)s::uuid",
                   {"t": now, "id": dn_id})

    return output.getvalue()


def generate_gstr1_cdn_data(from_date: date, to_date: date) -> Dict:
    """
    Generate GSTR-1 Table 9B data (B2B credit/debit notes).
    Returns a dict ready for JSON export or filing.
    """
    rows = _q("""
        SELECT * FROM v_cdn_summary
        WHERE doc_date BETWEEN %(fd)s AND %(td)s
          AND supply_type = 'B2B'
          AND status = 'CONFIRMED'
        ORDER BY doc_date, doc_number
    """, {"fd": from_date, "td": to_date})

    return {
        "period": f"{from_date} to {to_date}",
        "table":  "9B",
        "count":  len(rows),
        "records": [
            {
                "doc_type":          r["doc_type"],
                "doc_number":        r["doc_number"],
                "doc_date":          str(r["doc_date"]),
                "ref_invoice_no":    r["ref_invoice_no"],
                "ref_invoice_date":  str(r.get("ref_invoice_date") or ""),
                "party_gstin":       r["party_gstin"],
                "place_of_supply":   r["place_of_supply"],
                "taxable_value":     float(r["taxable_amount"]),
                "cgst":              float(r["cgst_amount"]),
                "sgst":              float(r["sgst_amount"]),
                "igst":              float(r["igst_amount"]),
            }
            for r in rows
        ]
    }


# ── Internal helpers ──────────────────────────────────────────────────

def _aggregate_lines(lines: List[Dict], place_of_supply: str) -> Dict:
    """
    Aggregate tax totals across all CDN lines.
    Uses pre-calculated tax_amount from invoice_lines where available
    to avoid floating-point rounding discrepancies.
    """
    total_taxable = 0.0
    total_tax_raw = 0.0  # sum of actual tax_amounts from invoice
    total_cgst    = 0.0
    total_sgst    = 0.0
    total_igst    = 0.0

    pos_code = _state_name_to_code(str(place_of_supply or ""))
    is_inter  = (pos_code != OUR_STATE_CODE) if pos_code else False

    for line in lines:
        taxable   = float(line.get("taxable_amount") or 0)
        gst_pct   = float(line.get("gst_percent") or 0)
        # Prefer stored tax_amount (avoids recalculation rounding)
        tax_amt   = float(line.get("tax_amount") or 0)
        if tax_amt == 0 and gst_pct > 0:
            tax_amt = round(taxable * gst_pct / 100, 2)

        total_taxable += taxable
        total_tax_raw += tax_amt

        if is_inter:
            total_igst += tax_amt
        else:
            # Split CGST/SGST: half each, penny to CGST
            half = round(tax_amt / 2, 2)
            total_cgst += half
            total_sgst += round(tax_amt - half, 2)

    total_tax   = round(total_tax_raw, 2)
    total_cgst  = round(total_cgst, 2)
    total_sgst  = round(total_sgst, 2)
    total_igst  = round(total_igst, 2)
    grand_total = round(total_taxable + total_tax, 2)

    return {
        "taxable_amount":   round(total_taxable, 2),
        "cgst_amount":      total_cgst if not is_inter else 0.0,
        "sgst_amount":      total_sgst if not is_inter else 0.0,
        "igst_amount":      total_igst if is_inter else 0.0,
        "total_tax_amount": total_tax,
        "grand_total":      grand_total,
    }


def _build_tally_narration(
    doc_type:   str,
    doc_number: str,
    invoice_no: str,
    party_name: str,
    reason:     str,
) -> str:
    reason_text = CN_REASONS.get(reason, DN_REASONS.get(reason, reason))
    return (
        f"{doc_type} {doc_number} against Invoice {invoice_no} "
        f"for {party_name}. Reason: {reason_text}."
    )


def get_cdn_summary_stats(from_date: date, to_date: date) -> Dict:
    """Dashboard summary stats for credit/debit notes."""
    rows = _q("""
        SELECT
            COUNT(*) FILTER (WHERE doc_type = 'CREDIT')::int AS cn_count,
            COUNT(*) FILTER (WHERE doc_type = 'DEBIT')::int  AS dn_count,
            SUM(grand_total) FILTER (WHERE doc_type = 'CREDIT') AS cn_total,
            SUM(grand_total) FILTER (WHERE doc_type = 'DEBIT')  AS dn_total,
            COUNT(*) FILTER (WHERE tally_exported_at IS NULL AND status = 'CONFIRMED')::int AS pending_tally_export
        FROM v_cdn_summary
        WHERE doc_date BETWEEN %(fd)s AND %(td)s
    """, {"fd": from_date, "td": to_date})
    r = rows[0] if rows else {}
    return {
        "cn_count":            int(r.get("cn_count") or 0),
        "dn_count":            int(r.get("dn_count") or 0),
        "cn_total":            float(r.get("cn_total") or 0),
        "dn_total":            float(r.get("dn_total") or 0),
        "pending_tally_export": int(r.get("pending_tally_export") or 0),
    }
