"""
modules/billing/smart_print.py
================================
Smart Invoice & Challan Print
- A4 portrait (A5 via browser Print → Paper size → A5)
- Tally-style vertical layout
- CGST+SGST (intra-state) / IGST (inter-state) from party state_code vs shop GSTIN
- Powers column: per-party preference (parties.print_with_powers)
- Print styles: Tax Invoice | Challan | Proforma | Delivery Note
- Single invoice or multi-invoice batch print
"""

import streamlit as st
from modules.core.price_qty_governor import (
    normalize_to_pcs_price,
    compute_line_gst,
    reverse_qty,
)
from datetime import date, datetime
from typing import Optional, List, Dict


# ── helpers ──────────────────────────────────────────────────────────────────

def _q(sql: str, params=None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}")
        return []


def _fc(v) -> str:
    try:
        return "Rs.{:,.2f}".format(float(v or 0))
    except Exception:
        return "Rs.0.00"


def _fd(d) -> str:
    if not d:
        return "-"
    try:
        if isinstance(d, (date, datetime)):
            return d.strftime("%d %b %Y")
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        return str(d)[:10]


def _get_shop() -> Dict:
    try:
        from modules.settings.shop_master import get_unit_info
        return get_unit_info("retail") or {}
    except Exception:
        return {}


def _our_state_code(shop: Dict) -> str:
    gstin = str(shop.get("shop_gstin") or "").strip()
    if len(gstin) >= 2 and gstin[:2].isdigit():
        return gstin[:2]
    return "27"


def _is_inter_state(party_gstin: str, party_state_code: str, our_code: str) -> bool:
    if party_state_code and str(party_state_code).strip():
        return str(party_state_code).strip()[:2] != our_code
    if party_gstin and len(str(party_gstin).strip()) >= 2:
        code = str(party_gstin).strip()[:2]
        if code.isdigit():
            return code != our_code
    return False


def _gst_split(total_tax: float, is_inter: bool) -> Dict:
    t = round(float(total_tax or 0), 2)
    if t == 0:
        return {"cgst": 0.0, "sgst": 0.0, "igst": 0.0}
    if is_inter:
        return {"cgst": 0.0, "sgst": 0.0, "igst": t}
    half = round(t / 2, 2)
    return {"cgst": half, "sgst": round(t - half, 2), "igst": 0.0}


def _power_str(sph, cyl, axis, add) -> str:
    def _f(v, dec=2):
        if v is None:
            return "-"
        try:
            n = float(v)
            sign = "+" if n >= 0 else ""
            return "{}{:.{}f}".format(sign, n, dec)
        except Exception:
            return str(v)
    parts = [_f(sph), _f(cyl)]
    if axis is not None:
        try:
            parts.append("x{}".format(int(float(axis))))
        except Exception:
            parts.append("-")
    else:
        parts.append("-")
    if add is not None:
        parts.append("Add {}".format(_f(add)))
    return "  ".join(parts)


# ── CSS ──────────────────────────────────────────────────────────────────────

_CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Inter:wght@400;500;600;700;800&display=swap');

@page {
    size: A4;
    margin: 8mm;
}

body {
    margin: 0;
    padding: 0;
}

.inv-wrap {
    font-family: 'Inter', sans-serif;
    width: 180mm;
    margin: 0 auto;
    background: #fff;
    color: #111;
    padding: 8mm;
    box-sizing: border-box;
}

@media print {
    body {
        margin: 0;
        padding: 0;
    }

    .print-page {
        page-break-after: always;
        break-after: page;
    }

    .print-page:last-child {
        page-break-after: auto;
        break-after: auto;
    }

    .inv-wrap {
        page-break-inside: avoid;
    }

    div[style*="text-align:center"] button {
        display: none;
    }
}

.inv-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px;border-bottom:2px solid #0f172a;padding-bottom:10px}
.shop-name{font-size:1.25rem;font-weight:800;color:#0f172a}
.shop-sub{font-size:0.7rem;color:#475569;margin-top:3px;line-height:1.6}
.doc-badge{text-align:right}
.doc-type{font-size:0.58rem;text-transform:uppercase;letter-spacing:.14em;color:#64748b;font-weight:700}
.doc-no{font-family:'IBM Plex Mono',monospace;font-size:1.05rem;font-weight:700;color:#0f172a;margin-top:2px}
.doc-meta{font-size:0.68rem;color:#64748b;margin-top:3px}
.party-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:12px;padding:9px 12px;background:#f8fafc;border-radius:6px;border:1px solid #e2e8f0}
.p-lbl{font-size:0.58rem;text-transform:uppercase;letter-spacing:.1em;color:#94a3b8;font-weight:600}
.p-val{font-size:0.8rem;font-weight:700;color:#0f172a;margin-top:2px}
.p-sub{font-size:0.68rem;color:#64748b;margin-top:1px;line-height:1.4}
.badge{display:inline-block;font-size:0.58rem;font-weight:700;padding:2px 7px;border-radius:10px;letter-spacing:.06em;margin-top:5px}
.badge-intra{background:#dcfce7;color:#166534}
.badge-inter{background:#fef3c7;color:#92400e}
.inv-table{width:100%;border-collapse:collapse;font-size:0.76rem;margin:12px 0}
.inv-table th{background:#0f172a;color:#e2e8f0;padding:6px 7px;font-size:0.62rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.inv-table th.r{text-align:right}
.inv-table td{padding:5px 7px;border-bottom:1px solid #f1f5f9;vertical-align:top}
.inv-table td.r{text-align:right;font-family:'IBM Plex Mono',monospace;font-size:0.73rem}
.inv-table tr:nth-child(even) td{background:#fafafa}
.inv-table tr.sub td{font-weight:700;border-top:2px solid #0f172a;background:#f8fafc}
.inv-table tr.grand td{font-weight:800;font-size:0.88rem;background:#0f172a;color:#fff}
.inv-table tr.grand td.r{color:#34d399}
.pw{font-family:'IBM Plex Mono',monospace;font-size:0.65rem;color:#64748b;margin-top:2px}
.eye-R{display:inline-block;padding:1px 5px;border-radius:3px;font-size:0.6rem;font-weight:700;background:#dbeafe;color:#1e40af}
.eye-L{display:inline-block;padding:1px 5px;border-radius:3px;font-size:0.6rem;font-weight:700;background:#fce7f3;color:#9d174d}
.eye-B{display:inline-block;padding:1px 5px;border-radius:3px;font-size:0.6rem;font-weight:700;background:#d1fae5;color:#065f46}
.tax-box{background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:9px 12px;margin-bottom:10px}
.tx{display:flex;justify-content:space-between;padding:3px 0;font-size:0.76rem}
.tx.g{font-weight:800;font-size:0.9rem;padding-top:6px;border-top:1px solid #e2e8f0;margin-top:3px}
.adv-box{background:#f0fdf4;border:1px solid #86efac;border-radius:6px;padding:7px 12px;margin-bottom:7px;font-size:0.76rem}
.bal-box{background:#fff7ed;border:1px solid #fdba74;border-radius:6px;padding:7px 12px;margin-bottom:10px;font-size:0.76rem;font-weight:700}
.bank-box{background:#f0f9ff;border:1px solid #bae6fd;border-radius:6px;padding:7px 12px;margin-bottom:8px;font-size:0.7rem}
.sign-row{display:flex;justify-content:space-between;margin-top:18px;padding-top:8px;border-top:1px solid #e2e8f0;font-size:0.68rem;color:#64748b}
.sig-line{border-top:1px solid #94a3b8;width:130px;padding-top:4px;margin-top:26px;font-size:0.62rem}
.footer{font-size:0.65rem;color:#94a3b8;text-align:center;margin-top:12px;padding-top:6px;border-top:1px solid #e2e8f0}
@media print{
  .inv-wrap{padding:8px;max-width:100%}
  .no-print{display:none!important}
  @page{size:A4 portrait;margin:10mm}
}
</style>"""


# ── build one invoice as HTML ─────────────────────────────────────────────────

def build_invoice_html(
    inv: Dict,
    lines: List[Dict],
    shop: Dict,
    show_powers: bool = True,
    doc_type: str = "TAX INVOICE",
    show_bank: bool = True,
    advance_paid: float = 0.0,
    include_script: bool = True,
) -> str:

    our_code    = _our_state_code(shop)
    party_gstin = str(inv.get("gstin") or "").strip()
    party_sc    = str(inv.get("state_code") or "").strip()
    is_inter    = _is_inter_state(party_gstin, party_sc, our_code)
    state_name  = str(shop.get("shop_state") or "Maharashtra")
    supply_text = "INTER-STATE (IGST)" if is_inter else "INTRA-STATE {} (CGST+SGST)".format(state_name.upper())
    supply_cls  = "badge-inter" if is_inter else "badge-intra"

    # Pre-build shop header sub-lines (avoid nested f-strings)
    shop_name   = str(shop.get("shop_name") or "DV Optical")
    shop_addr   = ", ".join(filter(None, [
        str(shop.get("shop_address") or ""),
        str(shop.get("shop_address2") or ""),
        str(shop.get("shop_city") or ""),
        str(shop.get("shop_state") or ""),
        str(shop.get("shop_pincode") or ""),
    ]))
    shop_phone  = str(shop.get("shop_phone") or "")
    shop_email  = str(shop.get("shop_email") or "")
    shop_gstin  = str(shop.get("shop_gstin") or "")
    shop_pan    = str(shop.get("shop_pan") or "")
    shop_drug   = str(shop.get("shop_drug_lic") or "")
    shop_upi    = str(shop.get("shop_upi_id") or "")

    sub_parts = [shop_addr]
    if shop_phone:
        sub_parts.append("Ph: " + shop_phone)
    if shop_email:
        sub_parts.append(shop_email)
    if shop_gstin and len(shop_gstin) > 4:
        sub_parts.append("GSTIN: " + shop_gstin)
    if shop_drug and shop_drug not in ("(if applicable)", ""):
        sub_parts.append("Drug Lic: " + shop_drug)
    shop_sub_html = "<br>".join(sub_parts)

    # Party block
    party_name  = str(inv.get("party_name") or "—")
    party_addr  = str(inv.get("address") or "")
    party_city  = str(inv.get("city") or "")
    party_mob   = str(inv.get("mobile") or "")
    party_note  = str(inv.get("invoice_note") or "")

    party_sub_parts = []
    if party_addr:
        party_sub_parts.append(party_addr)
    city_mob = ""
    if party_city:
        city_mob += party_city
    if party_mob:
        city_mob += ("  |  Mob: " if city_mob else "Mob: ") + party_mob
    if city_mob:
        party_sub_parts.append(city_mob)
    party_sub_html = "<br>".join(party_sub_parts)
    party_gstin_html = ("<div class='p-sub'>GSTIN: {}</div>".format(party_gstin)) if party_gstin else ""

    our_gstin_html = ("<div class='p-sub'>GSTIN: {}</div>".format(shop_gstin)) if shop_gstin else ""
    our_pan_html   = ("<div class='p-sub'>PAN: {}</div>".format(shop_pan)) if shop_pan and shop_pan not in ("XXXXX0000X", "") else ""

    # Document meta
    inv_no      = str(inv.get("invoice_no") or inv.get("challan_no") or "—")
    inv_date    = _fd(inv.get("invoice_date") or inv.get("challan_date"))
    due_date    = _fd(inv.get("due_date")) if inv.get("due_date") else ""
    due_html    = "  &middot;  Due: {}".format(due_date) if due_date else ""
    challan_ref = "  &middot;  Challan: {}".format(inv["challan_no"]) if inv.get("challan_no") else ""

    # Build line rows
    rows_html     = ""
    subtotal      = 0.0
    total_tax     = 0.0
    gst_buckets: Dict[float, float] = {}

    # order_type drives GST inclusive/exclusive — stored in inv or defaulted
    _otype = str(inv.get("order_type") or "WHOLESALE").upper()

    for ln in lines:
        qty   = int(ln.get("quantity") or 0)
        # Governor: normalize BOX→PCS price
        up    = normalize_to_pcs_price(float(ln.get("unit_price") or 0), ln)
        gst_p = float(ln.get("gst_percent") or 0)
        # Governor: compute GST consistently (retail=inclusive, wholesale=exclusive)
        _gc   = compute_line_gst(up, qty, gst_p, _otype)
        base     = _gc["gst_base"]
        tax_a    = _gc["gst_amount"]
        line_tot = _gc["grand_total"]
        subtotal  += base
        total_tax += tax_a
        if gst_p > 0:
            gst_buckets[gst_p] = round(gst_buckets.get(gst_p, 0.0) + tax_a, 2)

        pname  = str(ln.get("product_name") or "")
        brand  = str(ln.get("brand") or "")
        eye    = str(ln.get("eye_side") or "").upper()

        # Tally-style: eye full label + power string inside description cell
        _eye_label = {"R": "Right Eye", "L": "Left Eye", "B": "Both Eyes", "S": "Service"}.get(eye, "")
        _eye_cls   = {"R": "eye-R", "L": "eye-L", "B": "eye-B"}.get(eye, "")

        desc_html = "<b>{}</b>".format(pname)
        if brand:
            desc_html += "<div style='font-size:0.63rem;color:#64748b'>{}</div>".format(brand)
        if _eye_cls:
            desc_html += "<div style='margin-top:2px'><span class='{}'>{}</span></div>".format(_eye_cls, _eye_label)
        if show_powers and any(ln.get(k) is not None for k in ("sph", "cyl", "axis", "add_power")):
            pw = _power_str(ln.get("sph"), ln.get("cyl"), ln.get("axis"), ln.get("add_power"))
            desc_html += "<div class='pw' style='margin-top:2px'>{}</div>".format(pw)

        gst_col = "{}%".format(int(gst_p)) if gst_p else "—"
        # Per-line GST split (CGST/SGST or IGST)
        _ln_split = _gst_split(tax_a, is_inter)
        if is_inter:
            _tax_cell = "{}".format(_fc(_ln_split["igst"]))
        else:
            _tax_cell = "<span style='font-size:0.68rem'>C:{}<br>S:{}</span>".format(
                _fc(_ln_split["cgst"]), _fc(_ln_split["sgst"]))

        rows_html += (
            "<tr>"
            "<td>{}</td>"
            "<td class='r'>{}</td>"
            "<td class='r'>{}</td>"
            "<td class='r'>{}</td>"
            "<td class='r'>{}</td>"
            "<td class='r'>{}</td>"
            "<td class='r'><b>{}</b></td>"
            "</tr>"
        ).format(desc_html, qty, _fc(up), _fc(base), gst_col, _tax_cell, _fc(line_tot))

    subtotal  = round(subtotal, 2)
    total_tax = round(total_tax, 2)
    grand     = round(subtotal + total_tax, 2)

    # Tax breakdown rows
    tax_rows_html = ""
    if gst_buckets:
        for rate, amt in sorted(gst_buckets.items()):
            split = _gst_split(amt, is_inter)
            if is_inter:
                tax_rows_html += "<div class='tx'><span>IGST @{}%</span><span>{}</span></div>".format(int(rate), _fc(split["igst"]))
            else:
                tax_rows_html += "<div class='tx'><span>CGST @{}%</span><span>{}</span></div>".format(rate / 2, _fc(split["cgst"]))
                tax_rows_html += "<div class='tx'><span>SGST @{}%</span><span>{}</span></div>".format(rate / 2, _fc(split["sgst"]))
    else:
        split = _gst_split(total_tax, is_inter)
        if is_inter:
            tax_rows_html = "<div class='tx'><span>IGST</span><span>{}</span></div>".format(_fc(split["igst"]))
        else:
            tax_rows_html  = "<div class='tx'><span>CGST</span><span>{}</span></div>".format(_fc(split["cgst"]))
            tax_rows_html += "<div class='tx'><span>SGST</span><span>{}</span></div>".format(_fc(split["sgst"]))

    # Advance / balance
    balance_due   = round(max(grand - advance_paid, 0), 2)
    advance_html  = ""
    if advance_paid > 0:
        advance_html = (
            "<div class='adv-box'>Previously Paid: <b>{}</b></div>"
            "<div class='bal-box'>Balance Due: {}</div>"
        ).format(_fc(advance_paid), _fc(balance_due))

    # Bank details
    bank_html = ""
    if show_bank and any([shop.get("bank_name"), shop.get("bank_account"), shop.get("shop_upi_id")]):
        bank_parts = []
        if shop.get("bank_name"):
            bank_parts.append("<b>Bank:</b> {}".format(shop["bank_name"]))
        if shop.get("bank_account"):
            bank_parts.append("<b>A/C:</b> {}".format(shop["bank_account"]))
        if shop.get("bank_ifsc"):
            bank_parts.append("<b>IFSC:</b> {}".format(shop["bank_ifsc"]))
        if shop_upi:
            bank_parts.append("<b>UPI:</b> {}".format(shop_upi))
        bank_html = "<div class='bank-box'>{}</div>".format("  &nbsp;|&nbsp;  ".join(bank_parts))

    # Footer
    footer_note = party_note or str(shop.get("print_footer") or "")
    city_juris  = str(shop.get("shop_city") or "local")
    footer_html = ""
    if footer_note:
        footer_html = "<div class='footer'>{}</div>".format(footer_note)
    footer_html += (
        "<div class='footer'>This is a computer generated document. "
        "Goods once sold will not be taken back. "
        "Subject to {} jurisdiction.</div>"
    ).format(city_juris)

    script_part = """
<script>
function printInvoice(){
var w=window.open('','_blank');
var c=document.querySelector('.inv-print').outerHTML;
var s=document.querySelector('style').outerHTML;
w.document.write('<!DOCTYPE html><html><head><meta charset=utf-8>'+s+'</head><body>'+c+'</body></html>');
w.document.close();w.focus();w.print();w.close();
}
</script>
""" if include_script else ""

    print_button_part = '<div style="text-align:center; padding:10px; background:#f0f0f0;"><button onclick="printInvoice()" style="padding:10px 20px; font-size:16px; background:#007bff; color:white; border:none; border-radius:5px;">🖨️ Print / Save as PDF</button></div>' if include_script else ''

    html = _CSS + (
        "<div class='inv-wrap inv-print'>"

        # HEADER
        "<div class='inv-header'>"
        "<div>"
        "<div class='shop-name'>{shop_name}</div>"
        "<div class='shop-sub'>{shop_sub}</div>"
        "</div>"
        "<div class='doc-badge'>"
        "<div class='doc-type'>{doc_type}</div>"
        "<div class='doc-no'>{inv_no}</div>"
        "<div class='doc-meta'>Date: {inv_date}{due_html}{challan_ref}</div>"
        "<div><span class='badge {supply_cls}'>{supply_text}</span></div>"
        "</div>"
        "{print_button_part}"
        "</div>"

        # BILL TO / SHIP TO
        "<div class='party-row'>"
        "<div>"
        "<div class='p-lbl'>Bill To</div>"
        "<div class='p-val'>{party_name}</div>"
        "<div class='p-sub'>{party_sub}</div>"
        "{party_gstin_html}"
        "</div>"
        "<div>"
        "<div class='p-lbl'>Supplied By</div>"
        "<div class='p-val'>{shop_name2}</div>"
        "{our_gstin_html}"
        "{our_pan_html}"
        "</div>"
        "</div>"

        # LINE ITEMS
        "<table class='inv-table'>"
        "<thead><tr>"
        "<th>Product / Description</th>"
        "<th class='r'>Qty</th>"
        "<th class='r'>Rate</th>"
        "<th class='r'>Base Amt</th>"
        "<th class='r'>GST%</th>"
        "<th class='r'>{tax_hdr}</th>"
        "<th class='r'>Total</th>"
        "</tr></thead>"
        "<tbody>"
        "{rows_html}"
        "<tr class='sub'>"
        "<td colspan='3'><b>Sub-total</b></td>"
        "<td class='r'>{subtotal}</td>"
        "<td class='r'>-</td>"
        "<td class='r'>{total_tax}</td>"
        "<td class='r'>{grand}</td>"
        "</tr>"
        "<tr class='grand'>"
        "<td colspan='6'><b>GRAND TOTAL</b></td>"
        "<td class='r'>{grand2}</td>"
        "</tr>"
        "</tbody></table>"

        # TAX SUMMARY
        "<div class='tax-box'>"
        "<div style='font-size:0.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:5px'>Tax Summary</div>"
        "<div class='tx'><span>Taxable Value</span><span>{subtotal2}</span></div>"
        "{tax_rows_html}"
        "<div class='tx g'><span>Total Tax</span><span>{total_tax2}</span></div>"
        "<div class='tx g'><span>Grand Total</span><span>{grand3}</span></div>"
        "</div>"

        "{advance_html}"
        "{bank_html}"

        # SIGNATURE
        "<div class='sign-row'>"
        "<div><div class='sig-line'>Receiver's Signature</div></div>"
        "<div style='text-align:right'>"
        "<div style='margin-bottom:24px'>{shop_name3}</div>"
        "<div class='sig-line' style='margin-left:auto'>Authorised Signatory</div>"
        "</div>"
        "</div>"

        "{footer_html}"
        "{script_part}"
        "</div>"  # inv-wrap
    ).format(
        shop_name=shop_name, shop_sub=shop_sub_html,
        doc_type=doc_type, inv_no=inv_no,
        inv_date=inv_date, due_html=due_html, challan_ref=challan_ref,
        supply_cls=supply_cls, supply_text=supply_text,
        party_name=party_name, party_sub=party_sub_html,
        party_gstin_html=party_gstin_html,
        shop_name2=shop_name,
        our_gstin_html=our_gstin_html, our_pan_html=our_pan_html,
        rows_html=rows_html,
        tax_hdr="IGST" if is_inter else "CGST / SGST",
        subtotal=_fc(subtotal), total_tax=_fc(total_tax),
        grand=_fc(grand), grand2=_fc(grand),
        subtotal2=_fc(subtotal), total_tax2=_fc(total_tax), grand3=_fc(grand),
        tax_rows_html=tax_rows_html,
        advance_html=advance_html, bank_html=bank_html,
        shop_name3=shop_name,
        footer_html=footer_html,
        print_button_part=print_button_part,
        script_part=script_part,
    )
    return html


# ── Streamlit: single invoice ─────────────────────────────────────────────────

def render_smart_invoice(invoice_no: str, return_html: bool = False, doc_type_override: str = None, show_powers_override: bool = None, show_bank_override: bool = None):
    shop = _get_shop()

    # Ensure columns exist
    try:
        from modules.sql_adapter import run_write
        run_write("ALTER TABLE parties ADD COLUMN IF NOT EXISTS print_with_powers BOOLEAN DEFAULT TRUE", ())
        run_write("ALTER TABLE parties ADD COLUMN IF NOT EXISTS invoice_note TEXT", ())
    except Exception:
        pass

    inv_rows = _q("""
        SELECT i.*,
               COALESCE(p.party_name,
                   (SELECT o2.party_name FROM orders o2
                    WHERE o2.id::text = ANY(i.order_ids) LIMIT 1),
                   'Unknown') AS party_name,
               COALESCE(p.mobile,'')             AS mobile,
               COALESCE(p.address,'')            AS address,
               COALESCE(p.city,'')               AS city,
               COALESCE(p.gstin,'')              AS gstin,
               COALESCE(p.state_code,'')         AS state_code,
               COALESCE(p.print_with_powers,TRUE) AS print_with_powers,
               COALESCE(p.invoice_note,'')       AS invoice_note,
               c.challan_no
        FROM invoices i
        LEFT JOIN parties p  ON p.id = i.party_id
        LEFT JOIN challans c ON c.id = i.challan_id
        WHERE i.invoice_no = %(n)s
    """, {"n": invoice_no})

    if not inv_rows:
        if return_html:
            return ""
        st.error("Invoice {} not found".format(invoice_no))
        return
    inv = inv_rows[0]

    lines = _fetch_lines(str(inv["id"]), inv.get("challan_id"))
    advance_paid = _fetch_advance(inv.get("order_ids") or [])

    # ── Print options ──────────────────────────────────────────────────────
    if not return_html:
        st.markdown("#### Print Options")
        c1, c2, c3 = st.columns(3)
        doc_type    = c1.selectbox("Document Type",
                                   ["TAX INVOICE", "CHALLAN", "PROFORMA INVOICE", "DELIVERY NOTE"],
                                   key="spi_dtype_" + invoice_no)
        show_powers = c2.checkbox("Show Lens Powers",
                                  value=bool(inv.get("print_with_powers", True)),
                                  key="spi_pw_" + invoice_no)
        show_bank   = c3.checkbox("Show Bank Details", value=True,
                                  key="spi_bank_" + invoice_no)

        # Persist power preference per party
        _pid = inv.get("party_id")
        if _pid and show_powers != bool(inv.get("print_with_powers", True)):
            try:
                from modules.sql_adapter import run_write
                run_write("UPDATE parties SET print_with_powers=%s WHERE id=%s::uuid",
                          (show_powers, str(_pid)))
            except Exception:
                pass
    else:
        doc_type = doc_type_override or "TAX INVOICE"
        show_powers = show_powers_override if show_powers_override is not None else bool(inv.get("print_with_powers", True))
        show_bank = show_bank_override if show_bank_override is not None else True

    # Build and render
    html = build_invoice_html(
        inv=inv, lines=lines, shop=shop,
        show_powers=show_powers, doc_type=doc_type,
        show_bank=show_bank, advance_paid=advance_paid,
        include_script = not return_html,
    )
    if return_html:
        return html
    st.components.v1.html(html, height=920, scrolling=True)


# ── Streamlit: batch print ─────────────────────────────────────────────────────

def render_batch_print(invoice_nos: List[str]):
    shop = _get_shop()
    st.markdown("#### Batch Print  -  {} Invoice(s)".format(len(invoice_nos)))

    c1, c2 = st.columns(2)
    show_powers = c1.checkbox("Show Lens Powers", value=True, key="bsp_pw")
    doc_type    = c2.selectbox("Document Type",
                               ["TAX INVOICE", "CHALLAN", "DELIVERY NOTE"],
                               key="bsp_dtype")

    all_html = _CSS
    for inv_no in invoice_nos:
        rows = _q("""
            SELECT i.*,
                   COALESCE(p.party_name,'Unknown') AS party_name,
                   COALESCE(p.mobile,'')     AS mobile,
                   COALESCE(p.address,'')    AS address,
                   COALESCE(p.city,'')       AS city,
                   COALESCE(p.gstin,'')      AS gstin,
                   COALESCE(p.state_code,'') AS state_code,
                   COALESCE(p.invoice_note,'') AS invoice_note,
                   c.challan_no
            FROM invoices i
            LEFT JOIN parties p  ON p.id = i.party_id
            LEFT JOIN challans c ON c.id = i.challan_id
            WHERE i.invoice_no = %(n)s
        """, {"n": inv_no})
        if not rows:
            continue
        inv          = rows[0]
        lines        = _fetch_lines(str(inv["id"]), inv.get("challan_id"))
        advance_paid = _fetch_advance(inv.get("order_ids") or [])
        body = build_invoice_html(
            inv=inv, lines=lines, shop=shop,
            show_powers=show_powers, doc_type=doc_type,
            advance_paid=advance_paid,
        )
        # Strip duplicate CSS from body
        body = body.replace(_CSS, "")
        all_html += body + "<div style='page-break-after:always'></div>"

    st.components.v1.html(all_html, height=1000, scrolling=True)
    if st.button("Print All  (Ctrl+P)", type="primary",
                 key="bsp_print_all", use_container_width=True):
        st.info("Ctrl+P  →  All Pages.  Paper Size A4 or A5.")


# ── shared fetch helpers ──────────────────────────────────────────────────────

def _fetch_lines(invoice_id: str, challan_id=None) -> List[Dict]:
    lines = _q("""
        SELECT il.quantity, il.unit_price, il.total_price,
               COALESCE(il.tax_amount, 0)   AS tax_amount,
               COALESCE(ol.gst_percent, 0)  AS gst_percent,
               COALESCE(il.product_name, pr.product_name, 'Lens') AS product_name,
               COALESCE(il.brand, pr.brand, '')  AS brand,
               COALESCE(ol.eye_side, il.eye_side) AS eye_side,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               COALESCE(pr.box_size, 1)      AS box_size,
               COALESCE(pr.unit, 'PCS')      AS unit
        FROM invoice_lines il
        LEFT JOIN order_lines ol ON ol.id = il.order_line_id
        LEFT JOIN products pr    ON pr.id = ol.product_id
        WHERE il.invoice_id = %(id)s
          AND NOT COALESCE(il.is_deleted, FALSE)
        ORDER BY COALESCE(il.eye_side,''), il.id
    """, {"id": invoice_id})

    if not lines and challan_id:
        lines = _q("""
            SELECT cl.quantity, cl.unit_price, cl.total_price,
                   COALESCE(cl.total_price * COALESCE(ol.gst_percent,0)/100, 0) AS tax_amount,
                   COALESCE(ol.gst_percent, 0) AS gst_percent,
                   cl.product_name, cl.brand,
                   COALESCE(cl.eye_side, ol.eye_side) AS eye_side,
                   ol.sph, ol.cyl, ol.axis, ol.add_power
            FROM challan_lines cl
            LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
            WHERE cl.challan_id = %(cid)s
              AND NOT COALESCE(cl.is_deleted, FALSE)
            ORDER BY COALESCE(cl.eye_side,''), cl.id
        """, {"cid": challan_id})
    return lines


def _fetch_advance(order_ids) -> float:
    if not order_ids:
        return 0.0
    oids = [str(x) for x in order_ids]
    rows = _q("""
        SELECT COALESCE(SUM(amount), 0) AS tot
        FROM payments
        WHERE advance_for_order_id::text = ANY(%(oids)s)
          AND payment_type = 'ADVANCE'
          AND NOT COALESCE(is_deleted, FALSE)
    """, {"oids": oids})
    return round(float((rows[0]["tot"] if rows else 0) or 0), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# CHALLAN SMART PRINT
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_challan_lines(challan_id: str) -> List[Dict]:
    return _q("""
        SELECT
            cl.quantity, cl.unit_price, cl.line_total  AS total_price,
            COALESCE(ol.gst_percent, 0)                 AS gst_percent,
            COALESCE(cl.product_name, pr.product_name, 'Lens') AS product_name,
            COALESCE(cl.brand, pr.brand, '')            AS brand,
            COALESCE(cl.eye_side, ol.eye_side, '')      AS eye_side,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            COALESCE(pr.box_size, 1)                    AS box_size,
            COALESCE(pr.unit, 'PCS')                    AS unit,
            COALESCE(o.order_no, '')                    AS order_no,
            UPPER(COALESCE(o.order_type, 'WHOLESALE'))  AS order_type
        FROM challan_lines cl
        LEFT JOIN order_lines ol ON ol.id  = cl.order_line_id
        LEFT JOIN orders o       ON o.id   = cl.order_id
        LEFT JOIN products pr    ON pr.id  = ol.product_id
        WHERE cl.challan_id = %(cid)s
          AND NOT COALESCE(cl.is_deleted, FALSE)
        ORDER BY COALESCE(cl.eye_side, ''), cl.id
    """, {"cid": challan_id})


def build_challan_html(
    challan: Dict,
    lines: List[Dict],
    svc_lines: List[Dict],
    shop: Dict,
    show_powers: bool = True,
    doc_type: str = "DELIVERY CHALLAN",
    include_script: bool = True,
) -> str:
    """
    Build A4-quality challan HTML with proper GST split:
      - Same state as company (or no GSTIN) → CGST + SGST
      - Different state → IGST
    """
    our_code    = _our_state_code(shop)
    party_gstin = str(challan.get("gstin") or "").strip()
    party_sc    = str(challan.get("state_code") or "").strip()
    is_inter    = _is_inter_state(party_gstin, party_sc, our_code)
    state_name  = str(shop.get("shop_state") or "Maharashtra")
    supply_text = "INTER-STATE (IGST)" if is_inter else "INTRA-STATE {} (CGST+SGST)".format(state_name.upper())
    supply_cls  = "badge-inter" if is_inter else "badge-intra"

    # Shop info
    shop_name  = str(shop.get("shop_name") or "DV Optical")
    shop_addr  = ", ".join(filter(None, [
        str(shop.get("shop_address")  or ""),
        str(shop.get("shop_address2") or ""),
        str(shop.get("shop_city")     or ""),
        str(shop.get("shop_state")    or ""),
        str(shop.get("shop_pincode")  or ""),
    ]))
    shop_phone  = str(shop.get("shop_phone") or "")
    shop_email  = str(shop.get("shop_email") or "")
    shop_gstin  = str(shop.get("shop_gstin") or "")
    shop_pan    = str(shop.get("shop_pan")   or "")
    shop_upi    = str(shop.get("shop_upi_id") or "")

    sub_parts = [shop_addr]
    if shop_phone: sub_parts.append("Ph: " + shop_phone)
    if shop_email: sub_parts.append(shop_email)
    if shop_gstin and len(shop_gstin) > 4: sub_parts.append("GSTIN: " + shop_gstin)
    shop_sub_html = "<br>".join(filter(None, sub_parts))

    # Party info
    party_name = str(challan.get("party_name") or "—")
    party_addr = str(challan.get("address") or "")
    party_city = str(challan.get("city")    or "")
    party_mob  = str(challan.get("mobile")  or "")

    party_sub_parts = []
    if party_addr: party_sub_parts.append(party_addr)
    city_mob = ""
    if party_city: city_mob += party_city
    if party_mob:  city_mob += ("  |  Mob: " if city_mob else "Mob: ") + party_mob
    if city_mob:   party_sub_parts.append(city_mob)
    party_sub_html    = "<br>".join(party_sub_parts)
    party_gstin_html  = "<div class='p-sub'>GSTIN: {}</div>".format(party_gstin) if party_gstin else ""
    our_gstin_html    = "<div class='p-sub'>GSTIN: {}</div>".format(shop_gstin) if shop_gstin else ""
    our_pan_html      = "<div class='p-sub'>PAN: {}</div>".format(shop_pan) if shop_pan and shop_pan not in ("XXXXX0000X", "") else ""

    ch_no   = str(challan.get("challan_no") or "—")
    ch_date = _fd(challan.get("challan_date"))
    remarks = str(challan.get("remarks") or "")

    # ── Build line rows ───────────────────────────────────────────────────────
    rows_html      = ""
    subtotal       = 0.0
    total_tax      = 0.0
    gst_buckets: Dict[float, float] = {}
    _otype = "RETAIL"
    if lines:
        _otype = str(lines[0].get("order_type") or "WHOLESALE").upper()

    for ln in lines:
        qty   = int(ln.get("quantity") or 0)
        up    = normalize_to_pcs_price(float(ln.get("unit_price") or 0), ln)
        gst_p = float(ln.get("gst_percent") or 0)
        _gc   = compute_line_gst(up, qty, gst_p, _otype)
        base     = _gc["gst_base"]
        tax_a    = _gc["gst_amount"]
        line_tot = _gc["grand_total"]
        subtotal  += base
        total_tax += tax_a
        if gst_p > 0:
            gst_buckets[gst_p] = round(gst_buckets.get(gst_p, 0.0) + tax_a, 2)

        pname   = str(ln.get("product_name") or "")
        brand   = str(ln.get("brand") or "")
        eye     = str(ln.get("eye_side") or "").upper()
        eye_html = ""
        if eye in ("R", "L", "B"):
            eye_html = "<span class='eye-{}'>{}</span> ".format(eye, eye)

        # Tally-style: full eye label + power string inside description
        _eye_label = {"R": "Right Eye", "L": "Left Eye", "B": "Both Eyes", "S": "Service"}.get(eye, "")
        _eye_cls   = {"R": "eye-R", "L": "eye-L", "B": "eye-B"}.get(eye, "")

        desc_html = "<b>{}</b>".format(pname)
        if brand:
            desc_html += "<div style='font-size:0.63rem;color:#64748b'>{}</div>".format(brand)
        if _eye_cls:
            desc_html += "<div style='margin-top:2px'><span class='{}'>{}</span></div>".format(_eye_cls, _eye_label)
        if show_powers and any(ln.get(k) is not None for k in ("sph", "cyl", "axis", "add_power")):
            pw = _power_str(ln.get("sph"), ln.get("cyl"), ln.get("axis"), ln.get("add_power"))
            desc_html += "<div class='pw' style='margin-top:2px'>{}</div>".format(pw)

        # Per-line GST split
        _ln_split_ch = _gst_split(tax_a, is_inter)
        gst_col = "{}%".format(int(gst_p)) if gst_p else "—"
        if is_inter:
            _tax_cell_ch = "{}".format(_fc(_ln_split_ch["igst"]))
        else:
            _tax_cell_ch = "<span style='font-size:0.68rem'>C:{}<br>S:{}</span>".format(
                _fc(_ln_split_ch["cgst"]), _fc(_ln_split_ch["sgst"]))

        rows_html += (
            "<tr>"
            "<td>{desc}</td>"
            "<td class='r'>{qty}</td>"
            "<td class='r'>{up}</td>"
            "<td class='r'>{base}</td>"
            "<td class='r'>{gst}</td>"
            "<td class='r'>{tax}</td>"
            "<td class='r'><b>{tot}</b></td>"
            "</tr>"
        ).format(
            desc=desc_html, qty=qty,
            up=_fc(up), base=_fc(base), gst=gst_col,
            tax=_tax_cell_ch, tot=_fc(line_tot)
        )

    # ── Service charge rows ────────────────────────────────────────────────────
    svc_total = 0.0
    for sc in (svc_lines or []):
        _ico   = {"FITTING":"🔧","COLOURING":"🎨","COURIER":"📦","CONSULTATION":"👁️"}.get(
            str(sc.get("charge_type") or "").upper(), "➕")
        _name  = str(sc.get("description") or sc.get("charge_type") or "Service")
        _base  = float(sc.get("base_amount") or 0)
        _gst_p = float(sc.get("gst_percent") or 0)
        _gtax  = float(sc.get("gst_amount")  or 0)
        _tot   = float(sc.get("total_amount") or 0)
        svc_total  += _tot
        total_tax  += _gtax
        if _gst_p > 0 and _gtax > 0:
            gst_buckets[_gst_p] = round(gst_buckets.get(_gst_p, 0.0) + _gtax, 2)
        rows_html += (
            "<tr style='background:#fafaf7'>"
            "<td><b>{ico} {name}</b><div style='font-size:0.63rem;color:#64748b'>Service Charge</div></td>"
            "<td class='r'>1</td>"
            "<td class='r'>{base}</td>"
            "<td class='r'>{base}</td>"
            "<td class='r'>{gst}%</td>"
            "<td class='r'>{tax}</td>"
            "<td class='r'><b>{tot}</b></td>"
            "</tr>"
        ).format(ico=_ico, name=_name, base=_fc(_base),
                 gst=int(_gst_p), tax=_fc(_gtax), tot=_fc(_tot))

    subtotal  = round(subtotal, 2)
    total_tax = round(total_tax, 2)
    grand     = round(subtotal + total_tax + svc_total - sum(
        float(sc.get("base_amount") or 0) for sc in (svc_lines or [])
    ), 2)
    # Recalculate grand cleanly
    grand = round(subtotal + total_tax, 2)

    # ── Tax breakdown ─────────────────────────────────────────────────────────
    tax_rows_html = ""
    if gst_buckets:
        for rate, amt in sorted(gst_buckets.items()):
            split = _gst_split(amt, is_inter)
            if is_inter:
                tax_rows_html += "<div class='tx'><span>IGST @{}%</span><span>{}</span></div>".format(int(rate), _fc(split["igst"]))
            else:
                tax_rows_html += "<div class='tx'><span>CGST @{:.1f}%</span><span>{}</span></div>".format(rate/2, _fc(split["cgst"]))
                tax_rows_html += "<div class='tx'><span>SGST @{:.1f}%</span><span>{}</span></div>".format(rate/2, _fc(split["sgst"]))
    else:
        split = _gst_split(total_tax, is_inter)
        if is_inter:
            tax_rows_html = "<div class='tx'><span>IGST</span><span>{}</span></div>".format(_fc(split["igst"]))
        else:
            tax_rows_html  = "<div class='tx'><span>CGST</span><span>{}</span></div>".format(_fc(split["cgst"]))
            tax_rows_html += "<div class='tx'><span>SGST</span><span>{}</span></div>".format(_fc(split["sgst"]))

    remarks_html = ""
    if remarks:
        remarks_html = "<div class='bank-box' style='margin-top:8px'>📝 <b>Remarks:</b> {}</div>".format(remarks)

    city_juris = str(shop.get("shop_city") or "local")
    footer_note = str(shop.get("print_footer") or "")
    footer_html = ""
    if footer_note:
        footer_html = "<div class='footer'>{}</div>".format(footer_note)
    footer_html += (
        "<div class='footer'>This is a computer generated document. "
        "Subject to {} jurisdiction.</div>"
    ).format(city_juris)

    script_part = """
<script>
function printInvoice(){
var w=window.open('','_blank');
var c=document.querySelector('.inv-print').outerHTML;
var s=document.querySelector('style').outerHTML;
w.document.write('<!DOCTYPE html><html><head><meta charset=utf-8>'+s+'</head><body>'+c+'</body></html>');
w.document.close();w.focus();w.print();w.close();
}
</script>
""" if include_script else ""

    print_button_part = '<div style="text-align:center; padding:10px; background:#f0f0f0;"><button onclick="printInvoice()" style="padding:10px 20px; font-size:16px; background:#007bff; color:white; border:none; border-radius:5px;">🖨️ Print / Save as PDF</button></div>' if include_script else ''

    html = _CSS + (
        "<div class='inv-wrap inv-print'>"

        # HEADER
        "<div class='inv-header'>"
        "<div>"
        "<div class='shop-name'>{shop_name}</div>"
        "<div class='shop-sub'>{shop_sub}</div>"
        "</div>"
        "<div class='doc-badge'>"
        "<div class='doc-type'>{doc_type}</div>"
        "<div class='doc-no'>{ch_no}</div>"
        "<div class='doc-meta'>Date: {ch_date}</div>"
        "<div><span class='badge {supply_cls}'>{supply_text}</span></div>"
        "</div>"
        "{print_button_part}"
        "</div>"

        # BILL TO / SUPPLIED BY
        "<div class='party-row'>"
        "<div>"
        "<div class='p-lbl'>Billed To</div>"
        "<div class='p-val'>{party_name}</div>"
        "<div class='p-sub'>{party_sub}</div>"
        "{party_gstin_html}"
        "</div>"
        "<div>"
        "<div class='p-lbl'>Supplied By</div>"
        "<div class='p-val'>{shop_name2}</div>"
        "{our_gstin_html}"
        "{our_pan_html}"
        "</div>"
        "</div>"

        # LINE TABLE
        "<table class='inv-table'>"
        "<thead><tr>"
        "<th>Product / Description</th>"
        "<th class='r'>Qty</th>"
        "<th class='r'>Rate</th>"
        "<th class='r'>Base Amt</th>"
        "<th class='r'>GST%</th>"
        "<th class='r'>{tax_hdr_ch}</th>"
        "<th class='r'>Total</th>"
        "</tr></thead>"
        "<tbody>"
        "{rows_html}"
        "<tr class='sub'>"
        "<td colspan='3'><b>Sub-total</b></td>"
        "<td class='r'>{subtotal}</td>"
        "<td class='r'>-</td>"
        "<td class='r'>{total_tax}</td>"
        "<td class='r'>{grand}</td>"
        "</tr>"
        "<tr class='grand'>"
        "<td colspan='6'><b>GRAND TOTAL</b></td>"
        "<td class='r'>{grand2}</td>"
        "</tr>"
        "</tbody></table>"

        # TAX SUMMARY
        "<div class='tax-box'>"
        "<div style='font-size:0.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:5px'>Tax Summary</div>"
        "<div class='tx'><span>Taxable Value</span><span>{subtotal2}</span></div>"
        "{tax_rows_html}"
        "<div class='tx g'><span>Total Tax</span><span>{total_tax2}</span></div>"
        "<div class='tx g'><span>Grand Total</span><span>{grand3}</span></div>"
        "<div style='font-size:0.62rem;color:#94a3b8;margin-top:4px'>"
        "Supply type: {supply_text2}"
        "{gstin_note}"
        "</div>"
        "</div>"

        "{remarks_html}"

        # SIGNATURE
        "<div class='sign-row'>"
        "<div><div class='sig-line'>Receiver's Signature</div></div>"
        "<div style='text-align:right'>"
        "<div style='margin-bottom:24px'>{shop_name3}</div>"
        "<div class='sig-line' style='margin-left:auto'>Authorised Signatory</div>"
        "</div>"
        "</div>"

        "{footer_html}"
        "{script_part}"
        "</div>"  # inv-wrap
    ).format(
        shop_name=shop_name, shop_sub=shop_sub_html,
        doc_type=doc_type, ch_no=ch_no, ch_date=ch_date,
        supply_cls=supply_cls, supply_text=supply_text,
        party_name=party_name, party_sub=party_sub_html,
        party_gstin_html=party_gstin_html,
        shop_name2=shop_name, our_gstin_html=our_gstin_html, our_pan_html=our_pan_html,
        rows_html=rows_html,
        tax_hdr_ch="IGST" if is_inter else "CGST / SGST",
        subtotal=_fc(subtotal), total_tax=_fc(total_tax),
        grand=_fc(grand), grand2=_fc(grand),
        subtotal2=_fc(subtotal), total_tax2=_fc(total_tax), grand3=_fc(grand),
        tax_rows_html=tax_rows_html,
        supply_text2=supply_text,
        gstin_note=(" · GSTIN: " + party_gstin) if party_gstin else (" · Unregistered / Retail" if not challan.get("party_id") else " · GSTIN not recorded"),
        remarks_html=remarks_html,
        shop_name3=shop_name,
        footer_html=footer_html,
        print_button_part=print_button_part,
        script_part=script_part,
    )
    return html


def render_smart_challan(challan_no: str, return_html: bool = False, doc_type_override: str = None, show_powers_override: bool = None):
    """Streamlit: render smart print for a challan."""
    shop = _get_shop()

    ch_rows = _q("""
        SELECT c.*,
               COALESCE(p.party_name,
                   (SELECT o2.party_name FROM orders o2
                    WHERE o2.id::text = ANY(c.order_ids) LIMIT 1), '') AS party_name,
               COALESCE(p.mobile, '')       AS mobile,
               COALESCE(p.address, '')      AS address,
               COALESCE(p.city, '')         AS city,
               COALESCE(p.gstin, '')        AS gstin,
               COALESCE(p.state_code, '')   AS state_code,
               COALESCE(p.print_with_powers, TRUE) AS print_with_powers
        FROM challans c
        LEFT JOIN parties p ON p.id = c.party_id
        WHERE c.challan_no = %(n)s
    """, {"n": challan_no})

    if not ch_rows:
        if return_html:
            return ""
        st.error("Challan {} not found".format(challan_no))
        return

    ch = ch_rows[0]
    lines = _fetch_challan_lines(str(ch["id"]))

    svc_lines = _q("""
        SELECT charge_type, description, base_amount,
               gst_percent, gst_amount, total_amount
        FROM challan_service_charges
        WHERE challan_id = %(cid)s::uuid
        ORDER BY charge_type, description
    """, {"cid": str(ch["id"])}) or []

    # ── Print options ──────────────────────────────────────────────────────
    if not return_html:
        st.markdown("#### 🖨️ Challan Print Options")
        c1, c2 = st.columns(2)
        doc_type    = c1.selectbox("Document Type",
                                   ["DELIVERY CHALLAN", "TAX INVOICE", "PROFORMA INVOICE"],
                                   key="spc_dtype_" + challan_no)
        show_powers = c2.checkbox("Show Lens Powers",
                                  value=bool(ch.get("print_with_powers", True)),
                                  key="spc_pw_" + challan_no)

        # Persist power preference
        _pid = ch.get("party_id")
        if _pid and show_powers != bool(ch.get("print_with_powers", True)):
            try:
                from modules.sql_adapter import run_write
                run_write("ALTER TABLE parties ADD COLUMN IF NOT EXISTS print_with_powers BOOLEAN DEFAULT TRUE", ())
                run_write("UPDATE parties SET print_with_powers=%s WHERE id=%s::uuid",
                          (show_powers, str(_pid)))
            except Exception:
                pass
    else:
        doc_type = doc_type_override or "DELIVERY CHALLAN"
        show_powers = show_powers_override if show_powers_override is not None else bool(ch.get("print_with_powers", True))

    html = build_challan_html(
        challan=ch, lines=lines, svc_lines=svc_lines,
        shop=shop, show_powers=show_powers, doc_type=doc_type,
        include_script = not return_html,
    )
    if return_html:
        return html
    st.components.v1.html(html, height=920, scrolling=True)
