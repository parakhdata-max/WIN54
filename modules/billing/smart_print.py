"""
modules/billing/smart_print.py
================================
Smart Invoice & Challan Print
- A5 portrait by default (A4 expanded via browser/driver paper selection)
- Tally-style vertical layout
- CGST+SGST (intra-state) / IGST (inter-state) from party state_code vs shop GSTIN
- Powers column: per-party preference (parties.print_with_powers)
- Print styles: Tax Invoice | Challan | Proforma | Delivery Note
- Single invoice or multi-invoice batch print
"""

import json
import re
import urllib.parse
import streamlit as st
from modules.core.price_qty_governor import (
    normalize_to_pcs_price,
    compute_line_gst,
    reverse_qty,
)
from datetime import date, datetime
from typing import Optional, List, Dict


# ── barcode helper — real Code128 SVG via patient_card_printer ────────────────
def _bc_svg(val: str, width: int = 160, height: int = 38) -> str:
    """Inline scannable Code128 SVG for invoice/challan corner."""
    val = str(val or "").strip()
    if not val:
        return ""
    try:
        from modules.printing.patient_card_printer import barcode_svg as _bsvg
        return _bsvg(val, width=width, height=height)
    except Exception:
        # Fallback: Libre Barcode 128 font (online only)
        return (
            f'<link href="https://fonts.googleapis.com/css2?family=Libre+Barcode+128&display=swap" rel="stylesheet">'
            f'<div style="font-family:\'Libre Barcode 128\',monospace;font-size:32px;'
            f'line-height:1;letter-spacing:-1px">{val}</div>'
            f'<div style="font-size:7px;font-family:monospace;text-align:center">{val}</div>'
        )


def _upi_pay_uri(upi_id: str, payee_name: str, amount: float = 0.0, ref: str = "") -> str:
    upi_id = str(upi_id or "").strip()
    if not upi_id:
        return ""
    params = {
        "pa": upi_id,
        "pn": str(payee_name or "DV Optical"),
        "cu": "INR",
    }
    try:
        amt = round(float(amount or 0), 2)
        if amt > 0:
            params["am"] = f"{amt:.2f}"
    except Exception:
        pass
    if ref:
        params["tn"] = str(ref)
    return "upi://pay?" + urllib.parse.urlencode(params)


# ── helpers ──────────────────────────────────────────────────────────────────

def _q(sql: str, params=None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}")
        return []


def _fc(v) -> str:
    """Format currency — Indian comma style, abbreviated above 1 lakh like Tally."""
    try:
        n = float(v or 0)
        # Indian comma format: 1,23,456.78
        neg = n < 0
        n = abs(n)
        s = "{:.2f}".format(n)
        int_part, dec_part = s.split(".")
        if len(int_part) > 3:
            # Indian grouping: last 3 then groups of 2
            int_fmt = int_part[-3:]
            remaining = int_part[:-3]
            while remaining:
                int_fmt = remaining[-2:] + "," + int_fmt
                remaining = remaining[:-2]
            int_fmt = int_fmt.lstrip(",")
        else:
            int_fmt = int_part
        result = "Rs.{}{}.{}".format("-" if neg else "", int_fmt, dec_part)
        return result
    except Exception:
        return "Rs.0.00"


def _as_dict(value) -> Dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _direct_or_browser_print(html: str, filename: str, job_name: str) -> None:
    try:
        from modules.printing.direct_print import spool_html_to_printer
        from modules.printing.print_opener import open_html_print
        from modules.printing.printer_config import load_printer_settings

        ok, msg = spool_html_to_printer(html, job_name=job_name)
        if ok:
            st.success(msg)
            return
        st.warning(f"Direct print unavailable: {msg}")
        if bool(load_printer_settings().get("html_fallback", True)):
            path = open_html_print(html, filename)
            st.info(f"Opened HTML standby: {path}")
    except Exception as exc:
        st.error(f"Print failed: {exc}")


def _cart_offer_note(line: Dict, qty: int, line_total: float) -> tuple[str, float | None]:
    lp = _as_dict(line.get("lens_params"))
    if lp.get("club_offer_status") == "APPLIED":
        scheme  = str(lp.get("club_offer_name") or "Club Offer").strip()
        nominal = float(lp.get("club_offer_nominal_billing_value") or 1)
        mode    = str(lp.get("club_offer_application_mode") or "SAME_ORDER")
        if mode == "FUTURE_ENTITLEMENT":
            return f"🎁 {scheme}: entitlement earned — reward redeemable within validity period", None
        return f"🎁 {scheme}: included free with scheme — billed at {_fc(nominal)}", None

    if lp.get("entitlement_consumed"):
        src   = str(lp.get("entitlement_source_product") or "qualifying product").strip()
        return f"🎁 Reward redeemed from {src} scheme — billed at ₹1", None

    if lp.get("supplier_scheme_status") == "APPLIED":
        sname = str(lp.get("supplier_scheme_name") or "Scheme").strip()
        return f"Scheme: {sname}", None
    if lp.get("cart_offer_status") != "APPLIED":
        return "", None
    scheme = str(lp.get("cart_offer_scheme") or "Scheme").strip()
    buy_q = float(lp.get("cart_offer_buy_qty") or 0)
    reward_q = float(lp.get("cart_offer_reward_qty") or lp.get("cart_offer_free_qty") or 0)
    free_q = float(lp.get("cart_offer_free_qty") or reward_q or 0)
    style = str(lp.get("cart_offer_print_style") or "TRANSPARENT").upper()
    nominal = float(lp.get("cart_offer_nominal_free_value") or 1)
    avg = round(line_total / qty, 2) if qty else 0.0
    if style == "AVERAGED":
        note = (
            f"{scheme}: {buy_q:g}+{reward_q:g} scheme adjusted across "
            f"{qty:g} box(es); average {_fc(avg)}/box"
        )
        return note, avg
    note = f"{scheme}: {buy_q:g}+{reward_q:g} — {free_q:g} box(es) billed at {_fc(nominal)}"
    return note, None


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
        try:
            if float(add) != 0:
                parts.append("Ad{}".format(_f(add)))
        except Exception:
            parts.append("Ad{}".format(_f(add)))
    return " ".join(parts)


# ── CSS ──────────────────────────────────────────────────────────────────────

_CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Inter:wght@400;500;600;700;800&display=swap');

@page {
    size: A5;
    margin: 5mm;
}  /* default — overridden at build */

body {
    margin: 0;
    padding: 0;
}

.inv-wrap {
    font-family: 'Inter', sans-serif;
    width: 138mm;
    margin: 0 auto;
    background: #fff;
    color: #111;
    padding: 3mm 5mm;
    box-sizing: border-box;
}

@media print {
    body {
        margin: 0;
        padding: 0;
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
        color-adjust: exact !important;
    }

    img {
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
        max-width: 100% !important;
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

.inv-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:5px;border-bottom:2px solid #0f172a;padding-bottom:5px}
.shop-name{font-size:1.0rem;font-weight:800;color:#0f172a}
.shop-sub{font-size:0.62rem;color:#475569;margin-top:2px;line-height:1.35}
.doc-badge{text-align:right;min-width:0;max-width:52%}
.doc-type{font-size:0.54rem;text-transform:uppercase;letter-spacing:.14em;color:#64748b;font-weight:700}
.doc-no{font-family:'IBM Plex Mono',monospace;font-size:0.88rem;font-weight:700;color:#0f172a;margin-top:1px}
.doc-meta{font-size:0.6rem;color:#64748b;margin-top:2px}
.p-lbl{font-size:0.54rem;text-transform:uppercase;letter-spacing:.1em;color:#94a3b8;font-weight:600}
.p-val{font-size:0.74rem;font-weight:700;color:#0f172a}
.p-sub{font-size:0.62rem;color:#64748b;line-height:1.3}
.badge{display:inline-block;font-size:0.52rem;font-weight:700;padding:1px 5px;border-radius:10px;letter-spacing:.06em;margin-top:2px}
.badge-intra{background:#dcfce7;color:#166534}
.badge-inter{background:#fef3c7;color:#92400e}
.inv-table{width:100%;border-collapse:collapse;font-size:0.72rem;margin:5px 0}
.inv-table th{background:#0f172a;color:#e2e8f0;padding:3px 5px;font-size:0.57rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.inv-table th.r{text-align:right}
.inv-table td{padding:4px 5px;border-bottom:1px solid #f1f5f9;vertical-align:top}
.inv-table td.r{text-align:right;font-family:'IBM Plex Mono',monospace;font-size:0.68rem}
.inv-table td:first-child{width:52%}
.inv-table td:nth-child(2){width:13%}
.inv-table td:nth-child(3){width:13%}
.inv-table td:nth-child(4){width:11%}
.inv-table td:nth-child(5){width:11%}
.inv-table tr:nth-child(even) td{background:#fafafa}
.inv-table tr.sub td{font-weight:700;border-top:2px solid #0f172a;background:#f8fafc}
.inv-table tr.grand td{font-weight:800;font-size:0.82rem;background:#0f172a;color:#fff}
.inv-table tr.grand td.r{color:#34d399}
.pw{font-family:'IBM Plex Mono',monospace;font-size:0.58rem;color:#64748b;margin-top:1px}
.eye-R{display:inline-block;padding:1px 4px;border-radius:3px;font-size:0.6rem;font-weight:800;background:#1e40af;color:#fff}
.eye-L{display:inline-block;padding:1px 4px;border-radius:3px;font-size:0.6rem;font-weight:800;background:#9d174d;color:#fff}
.eye-B{display:inline-block;padding:1px 4px;border-radius:3px;font-size:0.6rem;font-weight:800;background:#065f46;color:#fff}
.tax-box{background:#f8fafc;border:1px solid #e2e8f0;border-radius:5px;padding:5px 10px;margin-bottom:6px}
.tx{display:flex;justify-content:space-between;padding:2px 0;font-size:0.7rem}
.tx.g{font-weight:800;font-size:0.84rem;padding-top:4px;border-top:1px solid #e2e8f0;margin-top:2px}
.adv-box{background:#f0fdf4;border:1px solid #86efac;border-radius:5px;padding:4px 10px;margin-bottom:4px;font-size:0.7rem}
.bal-box{background:#fff7ed;border:1px solid #fdba74;border-radius:5px;padding:4px 10px;margin-bottom:6px;font-size:0.7rem;font-weight:700}
.bank-box{background:#f0f9ff;border:1px solid #bae6fd;border-radius:5px;padding:4px 10px;margin-bottom:5px;font-size:0.64rem}
.sign-row{display:flex;justify-content:flex-end;margin-top:8px;padding-top:5px;border-top:1px solid #e2e8f0;font-size:0.62rem;color:#64748b}
.sig-line{border-top:1px solid #94a3b8;width:110px;padding-top:3px;margin-top:20px;font-size:0.58rem}
.footer{font-size:0.58rem;color:#94a3b8;text-align:center;margin-top:6px;padding-top:4px;border-top:1px solid #e2e8f0}
@media print{
  body{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important;color-adjust:exact!important}
  img{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}
  .inv-wrap{padding:3px 5px;max-width:100%}
  .no-print{display:none!important}
  @page{size:A5 portrait;margin:5mm}
}
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
    paper_size: str = "A5",
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
    _ch_refs = str(inv.get("converted_challans") or inv.get("challan_no") or "").strip()
    challan_ref = "  &middot;  Challan(s): {}".format(_ch_refs) if _ch_refs else ""

    # Build line rows
    rows_html     = ""
    subtotal      = 0.0
    total_tax     = 0.0
    gst_buckets: Dict[float, float] = {}

    # order_type drives GST inclusive/exclusive — stored in inv or defaulted
    _otype = str(
        inv.get("order_type")
        or (lines[0].get("order_type") if lines else "")
        or "WHOLESALE"
    ).upper()

    for ln in lines:
        eye   = str(ln.get("eye_side") or "").upper()
        pname = str(ln.get("product_name") or "")
        # Prices stored per-PCS in DB — use directly
        up_raw = float(ln.get("unit_price") or 0)
        gst_p  = float(ln.get("gst_percent") or 0)

        # ── R+L pair: club same product+price into one row ──────────────
        _partner_eye = {"R": "L", "L": "R"}.get(eye)
        _partner = None
        if _partner_eye:
            for _pl in lines:
                if (str(_pl.get("eye_side") or "").upper() == _partner_eye
                        and str(_pl.get("product_name") or "") == pname
                        and abs(float(_pl.get("unit_price") or 0) - up_raw) < 0.01):
                    _partner = _pl
                    break
        if _partner and eye == "L":
            continue  # already rendered as part of R row

        qty = int(ln.get("quantity") or 0)
        if _partner:
            qty += int(_partner.get("quantity") or 0)

        up       = up_raw
        base     = round(float(ln.get("taxable_amount") or ln.get("total_price") or 0), 2)
        line_tot = round(float(ln.get("line_total") or 0), 2)
        if _partner:
            base = round(base + float(_partner.get("taxable_amount") or _partner.get("total_price") or 0), 2)
            line_tot = round(line_tot + float(_partner.get("line_total") or 0), 2)
        if line_tot <= 0:
            _gc = compute_line_gst(up, qty, gst_p, _otype)
            base = _gc["gst_base"]
            line_tot = _gc["grand_total"]
        tax_a    = round(line_tot - base, 2)
        if gst_p > 0 and tax_a <= 0.01 and line_tot > 0:
            if _otype == "RETAIL":
                base = round(line_tot * 100 / (100 + gst_p), 2)
                tax_a = round(line_tot - base, 2)
            elif base > 0:
                tax_a = round(base * gst_p / 100, 2)
                line_tot = round(base + tax_a, 2)
        subtotal  += base
        total_tax += tax_a
        if gst_p > 0:
            gst_buckets[gst_p] = round(gst_buckets.get(gst_p, 0.0) + tax_a, 2)

        display_up = up
        _offer_note, _avg_up = _cart_offer_note(ln, qty, line_tot)
        if _avg_up is not None:
            display_up = _avg_up

        brand   = str(ln.get("brand") or "")
        coating = str(ln.get("coating_type") or ln.get("coating") or "").strip()
        idx_val = str(ln.get("lens_index") or ln.get("index_value") or "").strip()
        spec_bits = []
        if idx_val and idx_val.lower() not in ("none", "nan", "0", "0.0"):
            spec_bits.append(idx_val)
        if coating and coating.lower() not in ("none", "nan", "no", ""):
            spec_bits.append(coating)

        _eye_label = {"R": "Right Eye", "L": "Left Eye", "B": "Both Eyes", "S": "Service"}.get(eye, "")
        _eye_cls   = {"R": "eye-R", "L": "eye-L", "B": "eye-B"}.get(eye, "")

        desc_html = "<b style='font-size:0.8rem'>{}</b>".format(pname)
        # Spec line: index · coating · brand all on one teal line
        spec_bits = []
        if idx_val and idx_val.lower() not in ("none", "nan", "0", "0.0"):
            spec_bits.append(idx_val)
        if coating and coating.lower() not in ("none", "nan", "no", ""):
            spec_bits.append(coating)
        if brand and brand.lower() not in ("none", "nan", ""):
            spec_bits.append(brand)
        if spec_bits:
            desc_html += "<div style='font-size:0.63rem;color:#0f766e;font-weight:600;margin-top:1px'>{}</div>".format(" · ".join(spec_bits))
        if _offer_note:
            desc_html += (
                "<div style='font-size:0.62rem;color:#7c2d12;font-weight:700;"
                "margin-top:2px'>Scheme: {}</div>".format(_offer_note)
            )

        # Batch / expiry
        _batch   = str(ln.get("batch_no") or "").strip()
        _expiry  = ln.get("expiry_date")
        _exp_str = ""
        if _expiry:
            try:
                _ep = str(_expiry)[:7].split("-")
                _exp_str = "{}/{}".format(_ep[1], _ep[0]) if len(_ep) == 2 else str(_expiry)[:10]
            except Exception:
                _exp_str = str(_expiry)[:10]
        if _batch or _exp_str:
            _be_parts = []
            if _batch:   _be_parts.append("Batch: {}".format(_batch))
            if _exp_str: _be_parts.append("Exp: {}".format(_exp_str))
            desc_html += (
                "<div style='font-size:0.62rem;color:#0f766e;margin-top:1px'>"
                + "  ·  ".join(_be_parts) + "</div>"
            )

        # Power line — R/L badge inline, no separate order_no div
        _ord_no_inv = str(ln.get("order_no") or ln.get("cust_order_no") or "").strip()
        _cust_ord   = str(ln.get("cust_order_no") or "").strip()
        # Power + order no — full-width sub-row so R and L always on one line
        _power_sub_row = ""
        _cust_ord   = str(ln.get("cust_order_no") or "").strip()
        # Hide if it looks like a UUID (auto-generated, not human-entered)
        import re as _re_uuid
        _is_uuid = bool(_re_uuid.match(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            _cust_ord, _re_uuid.IGNORECASE
        ))
        _ord_parts  = []
        if _ord_no_inv: _ord_parts.append("#" + _ord_no_inv)
        if _cust_ord and _cust_ord != _ord_no_inv and not _is_uuid:
            _ord_parts.append("Ref:" + _cust_ord)
        _ord_bit = ("<span style='font-size:0.6rem;color:#94a3b8;margin-right:8px'>{}</span>".format("  ".join(_ord_parts))) if _ord_parts else ""
        if _partner:
            if show_powers:
                _r_pw = _power_str(ln.get("sph"), ln.get("cyl"), ln.get("axis"), ln.get("add_power"))
                _l_pw = _power_str(_partner.get("sph"), _partner.get("cyl"),
                                   _partner.get("axis"), _partner.get("add_power"))
                _power_sub_row = (
                    "<tr style='background:#f8faff'>"
                    "<td colspan='5' style='padding:2px 5px 5px 8px;border-bottom:1px solid #e8eef8'>"
                    + _ord_bit +
                    "<span style='white-space:nowrap'>"
                    "<span class='eye-R'>R</span>"
                    "<span class='pw'>&nbsp;" + _r_pw + "</span>"
                    "</span>"
                    "<span style='color:#cbd5e1;font-size:0.65rem'>&nbsp;&nbsp;|&nbsp;&nbsp;</span>"
                    "<span style='white-space:nowrap'>"
                    "<span class='eye-L'>L</span>"
                    "<span class='pw'>&nbsp;" + _l_pw + "</span>"
                    "</span>"
                    "</td></tr>"
                )
            else:
                desc_html += "<div style='margin-top:2px'><span class='eye-R'>R</span>&nbsp;<span class='eye-L'>L</span></div>"
        else:
            if show_powers and any(ln.get(k) is not None for k in ("sph","cyl","axis","add_power")):
                pw = _power_str(ln.get("sph"), ln.get("cyl"), ln.get("axis"), ln.get("add_power"))
                _badge = "<span class='{}'>{}</span>&nbsp;".format(_eye_cls, eye) if _eye_cls else ""
                _power_sub_row = (
                    "<tr style='background:#f8faff'>"
                    "<td colspan='5' style='padding:2px 5px 5px 8px;border-bottom:1px solid #e8eef8'>"
                    + _ord_bit + _badge +
                    "<span class='pw'>" + pw + "</span>"
                    "</td></tr>"
                )
            elif _eye_cls:
                desc_html += "<div style='margin-top:2px'><span class='{}'>{}</span></div>".format(_eye_cls, eye)

        gst_col = "{}%".format(int(gst_p)) if gst_p else "—"
        # Qty and rate display — box products: BOX as main unit, rate per box
        _bsz_d1 = int(ln.get('box_size') or 1)
        if _bsz_d1 > 1:
            _boxes_d = qty // _bsz_d1
            _loose_d = qty % _bsz_d1
            _qty_display = ("<span style='white-space:nowrap'><b>{}</b> Box "
                "<span style='font-size:0.57rem;color:#94a3b8'>({}p)</span></span>").format(_boxes_d, qty)
            # Rate shown as per-box (unit_price is per-pcs in DB)
            display_up = round(display_up * _bsz_d1, 2)
        else:
            _qty_display = str(qty)

        # GST column — percentage only, full breakdown in Tax Summary
        _tax_cell = "<span style='font-size:0.7rem;font-weight:600'>{}</span>".format(gst_col)
        _up_note = ""
        if not _partner and eye in ("R", "L") and qty > 1 and _bsz_d1 <= 1:
            _up_note = "<div style='font-size:0.6rem;color:#94a3b8'>×{} pcs @ {}/pc</div>".format(
                qty, _fc(display_up))

        # Order No in Rate cell — small, below rate
        _ord_note_cell = ""  # order no now in power sub-row

        rows_html += (
            "<tr>"
            "<td>{}</td>"
            "<td class='r'>{}</td>"
            "<td class='r'>{}{}{}</td>"
            "<td class='r'>{}</td>"
            "<td class='r'><b>{}</b></td>"
            "</tr>"
        ).format(desc_html, _qty_display,
                 _fc(display_up), _up_note, _ord_note_cell,
                 _tax_cell, _fc(line_tot))
        rows_html += _power_sub_row

    subtotal  = round(subtotal, 2)
    total_tax = round(total_tax, 2)
    grand     = round(subtotal + total_tax, 2)
    round_off = float(inv.get("round_off_amount") or 0)
    grand_display = float(inv.get("grand_total") or 0)
    if grand_display <= 0:
        grand_display = round(grand)
        round_off = round(grand_display - grand, 2)
    elif abs(round_off) < 0.005 and abs(grand_display - grand) >= 0.005:
        round_off = round(grand_display - grand, 2)
    roundoff_row_html = ""
    if abs(round_off) >= 0.005:
        roundoff_row_html = (
            "<tr class='sub'>"
            "<td colspan='4'><b>Round Off</b></td>"
            "<td class='r'>{}</td>"
            "</tr>"
        ).format(_fc(round_off))

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
    balance_due   = round(max(grand_display - advance_paid, 0), 2)
    advance_html  = ""
    if advance_paid > 0:
        advance_html = (
            "<div class='adv-box'>Previously Paid: <b>{}</b></div>"
            "<div class='bal-box'>Balance Due: {}</div>"
        ).format(_fc(advance_paid), _fc(balance_due))

    # Bank details + UPI QR code
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

        # UPI QR code — generated from shop_upi_id
        _qr_html = ""
        if shop_upi:
            try:
                import qrcode, io, base64
                _upi_str = _upi_pay_uri(
                    shop_upi, shop_name,
                    balance_due if balance_due > 0 else grand_display,
                    inv_no,
                )
                _qr = qrcode.QRCode(
                    version=None,
                    error_correction=qrcode.constants.ERROR_CORRECT_M,
                    box_size=3,
                    border=2,
                )
                _qr.add_data(_upi_str)
                _qr.make(fit=True)
                _img = _qr.make_image(fill_color="black", back_color="white")
                _buf = io.BytesIO()
                _img.save(_buf, format="PNG")
                _b64 = base64.b64encode(_buf.getvalue()).decode()
                _qr_html = (
                    "<div style='text-align:center;margin-left:10px'>"
                    "<div style='width:55px;height:55px;display:block;margin:0 auto;"
                    "background-image:url(data:image/png;base64,{});"
                    "background-size:contain;background-repeat:no-repeat;"
                    "-webkit-print-color-adjust:exact;print-color-adjust:exact;color-adjust:exact'></div>"
                    "<div style='font-size:0.52rem;color:#64748b;margin-top:1px'>Scan to Pay</div>"
                    "</div>"
                ).format(_b64)
            except Exception:
                _qr_html = ""

        bank_html = (
            "<div class='bank-box' style='display:flex;align-items:center;justify-content:space-between'>"
            "<div style='font-size:0.64rem'>{}</div>"
            "{}"
            "</div>"
        ).format("&nbsp;|&nbsp;".join(bank_parts), _qr_html)

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

    # ── Barcode SVGs for invoice corner ──────────────────────────────
    _inv_bc_svg    = _bc_svg(inv_no, width=160, height=26)
    _party_bc_val  = str(inv.get("party_barcode") or inv.get("patient_barcode") or "").strip()
    _party_bc_svg  = _bc_svg(_party_bc_val, width=120, height=22) if _party_bc_val else ""
    _bc_corner_html = (
        "<div style='margin-top:5px;text-align:right'>"
        "<div style='font-size:5pt;color:#94a3b8;letter-spacing:.05em;margin-bottom:1px'>SCAN</div>"
        f"{_inv_bc_svg}"
        + (f"<div style='margin-top:2px'>{_party_bc_svg}</div>" if _party_bc_svg else "")
        + "</div>"
    )

    # Paper size overrides
    _is_a4 = str(paper_size).upper() == 'A4'
    if _is_a4:
        _CSS_USE = (_CSS
            .replace('@page {\n    size: A5;\n    margin: 5mm;\n}  /* default — overridden at build */',
                     '@page { size: A4 portrait; margin: 10mm 12mm; }')
            .replace(
                    '.inv-wrap {\n    font-family:',
                    '.inv-wrap {\n    width: 186mm;\n    font-size: 0.82rem;\n    font-family:'
                )
            .replace('.inv-table{width:100%', '.inv-table{width:100%;font-size:0.8rem')
        )
    else:
        _CSS_USE = _CSS

    html = _CSS_USE + (
        "<div class='inv-wrap inv-print'>"

        # ── HEADER: shop left · doc+barcode right ────────────────────
        "<div class='inv-header'>"
        "<div style='flex:1;min-width:0'>"
        "<div class='shop-name'>{shop_name}</div>"
        "<div class='shop-sub'>{shop_sub}</div>"
        "</div>"
        "<div class='doc-badge' style='flex-shrink:0;margin-left:10px;text-align:right'>"
        "<div class='doc-type'>{doc_type}</div>"
        "<div class='doc-no'>{inv_no}</div>"
        "<div class='doc-meta'>Date: {inv_date}{due_html}{challan_ref}</div>"
        "<div><span class='badge {supply_cls}'>{supply_text}</span></div>"
        # Barcode inside doc-badge, compact
        "{bc_corner_html}"
        "</div>"
        "{print_button_part}"
        "</div>"

        # ── BILL TO — full width, compact single row ─────────────────
        "<div style='padding:5px 10px;background:#f8fafc;border:1px solid #e2e8f0;"
        "border-radius:5px;margin-bottom:8px;display:flex;gap:20px;align-items:baseline'>"
        "<div style='min-width:0;flex:1'>"
        "<span class='p-lbl'>Bill To &nbsp;</span>"
        "<span class='p-val'>{party_name}</span>"
        "<span class='p-sub' style='margin-left:6px'>{party_sub}</span>"
        "{party_gstin_html}"
        "</div>"
        "</div>"

        # ── LINE ITEMS ───────────────────────────────────────────────
        "<table class='inv-table'>"
        "<thead><tr>"
        "<th style='text-align:left'>Product / Description</th>"
        "<th style='text-align:center'>Qty</th>"
        "<th style='text-align:right'>Rate</th>"
        "<th style='text-align:center'>GST%</th>"
        "<th style='text-align:right'>Total</th>"
        "</tr></thead>"
        "<tbody>"
        "{rows_html}"
        "</tbody></table>"

        # TAX SUMMARY
        "<div class='tax-box'>"
        "<div style='font-size:0.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:5px'>Tax Summary</div>"
        "<div class='tx'><span>Taxable Value</span><span>{subtotal2}</span></div>"
        "{tax_rows_html}"
        ""
        "{roundoff_tax_html}"
        "<div class='tx g'><span>Grand Total</span><span>{grand3}</span></div>"
        "</div>"

        "{advance_html}"
        "{bank_html}"

        # SIGNATURE
        "<div class='sign-row'>"
        "<div>"
        "<div style='margin-bottom:22px;font-size:0.72rem;font-weight:600'>{shop_name3}</div>"
        "<div class='sig-line'>Authorised Signatory</div>"
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
        grand=_fc(grand), roundoff_row_html=roundoff_row_html,
        grand2=_fc(grand_display),
        subtotal2=_fc(subtotal), total_tax2=_fc(total_tax), grand3=_fc(grand_display),
        roundoff_tax_html=(
            "<div class='tx'><span>Round Off</span><span>{}</span></div>".format(_fc(round_off))
            if abs(round_off) >= 0.005 else ""
        ),
        tax_rows_html=tax_rows_html,
        advance_html=advance_html, bank_html=bank_html,
        shop_name3=shop_name,
        footer_html=footer_html,
        print_button_part=print_button_part,
        script_part=script_part,
        bc_corner_html=_bc_corner_html,
    )
    return html


# ── Streamlit: single invoice ─────────────────────────────────────────────────

def render_smart_invoice(invoice_no: str, return_html: bool = False, doc_type_override: str = None, show_powers_override: bool = None, show_bank_override: bool = None, paper_size_override: str = None):
    shop = _get_shop()

    inv_rows = _q("""
        SELECT i.*,
               COALESCE(
                   NULLIF(p.party_name, ''),
                   NULLIF((
                       SELECT COALESCE(NULLIF(o2.party_name, ''), NULLIF(o2.patient_name, ''), '')
                       FROM orders o2
                       WHERE o2.id::text = ANY(COALESCE(i.order_ids, c.order_ids))
                          OR o2.order_no = ANY(COALESCE(i.order_ids, c.order_ids))
                       LIMIT 1
                   ), ''),
                   '—'
               ) AS party_name,
               COALESCE(p.mobile,'')             AS mobile,
               COALESCE(p.address,'')            AS address,
               COALESCE(p.city,'')               AS city,
               COALESCE(p.gstin,'')              AS gstin,
               COALESCE(p.state_code,'')         AS state_code,
               COALESCE(p.print_with_powers,TRUE) AS print_with_powers,
               COALESCE(p.invoice_note,'')       AS invoice_note,
               c.challan_no,
               COALESCE(c.challan_no, '') AS converted_challans
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
        _spi_sfx = re.sub(r'[^a-zA-Z0-9_]', '_', str(invoice_no))
        st.markdown("#### Print Options")
        c1, c2, c3, c4 = st.columns(4)
        doc_type    = c1.selectbox("Document Type",
                                   ["TAX INVOICE", "CHALLAN", "PROFORMA INVOICE", "DELIVERY NOTE"],
                                   key="spi_dtype_" + _spi_sfx)
        show_powers = c2.checkbox("Show Lens Powers",
                                  value=bool(inv.get("print_with_powers", True)),
                                  key="spi_pw_" + _spi_sfx)
        show_bank   = c3.checkbox("Show Bank Details", value=True,
                                  key="spi_bank_" + _spi_sfx)
        paper_size  = c4.radio("Paper", ["A5", "A4"], horizontal=True,
                               key="spi_paper_" + _spi_sfx)

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
        paper_size = str(paper_size_override or "A5").upper()

    # Build and render
    html = build_invoice_html(
        inv=inv, lines=lines, shop=shop,
        show_powers=show_powers, doc_type=doc_type,
        show_bank=show_bank, advance_paid=advance_paid,
        include_script = not return_html,
        paper_size=paper_size,
    )
    if return_html:
        return html
    _p1, _p2 = st.columns(2)
    if _p1.button("Direct Print", key="spi_direct_" + _spi_sfx, use_container_width=True):
        _safe_inv = re.sub(r'[/\\:*?"<>|]', '-', str(invoice_no))
        _direct_or_browser_print(html, f"invoice_{_safe_inv}.html", f"Invoice_{_safe_inv}")
    if _p2.button("Open Print in Browser", key="spi_open_" + _spi_sfx, use_container_width=True):
        try:
            from modules.printing.print_opener import open_html_print

            _safe_inv = re.sub(r'[/\\:*?"<>|]', '-', str(invoice_no))
            path = open_html_print(html, f"invoice_{_safe_inv}.html")
            st.success(f"Invoice opened: {path}")
        except Exception as exc:
            st.error(f"Invoice print open failed: {exc}")
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

    _bp1, _bp2 = st.columns(2)
    if _bp1.button("Direct Batch Print", key="bsp_direct_all", use_container_width=True):
        _direct_or_browser_print(all_html, "invoice_batch_print.html", "Invoice_Batch_Print")
    if _bp2.button("Open Batch Print in Browser", key="bsp_open_all", use_container_width=True):
        try:
            from modules.printing.print_opener import open_html_print

            path = open_html_print(all_html, "invoice_batch_print.html")
            st.success(f"Batch print opened: {path}")
        except Exception as exc:
            st.error(f"Batch print open failed: {exc}")
    st.components.v1.html(all_html, height=1000, scrolling=True)
    if st.button("Print All  (Ctrl+P)", type="primary",
                 key="bsp_print_all", use_container_width=True):
        st.info("Ctrl+P  →  All Pages.  Paper Size A4 or A5.")


# ── shared fetch helpers ──────────────────────────────────────────────────────

def _fetch_lines(invoice_id: str, challan_id=None) -> List[Dict]:
    """Fetch invoice lines, falling back to challan lines when requested.

    Important: do not reference products.lens_index / products.coating_type here.
    Those columns are not guaranteed in older deployments. The final product
    print label must be derived from order_lines.lens_params JSONB instead.
    """
    lines = _q("""
        SELECT il.quantity, il.unit_price,
               COALESCE(il.total_price, 0) AS taxable_amount,
               COALESCE(il.line_total, il.total_price + COALESCE(il.tax_amount,0), il.total_price, 0) AS line_total,
               COALESCE(il.line_total, il.total_price + COALESCE(il.tax_amount,0), il.total_price, 0) AS total_price,
               COALESCE(il.tax_amount, COALESCE(il.line_total, il.total_price, 0) - COALESCE(il.total_price, 0), 0) AS tax_amount,
               COALESCE(ol.gst_percent, 0)  AS gst_percent,
               UPPER(COALESCE(o.order_type, 'WHOLESALE')) AS order_type,
               COALESCE(il.product_name, pr.product_name, 'Lens') AS product_name,
               COALESCE(il.brand, pr.brand, '')  AS brand,
               COALESCE(ol.eye_side, il.eye_side) AS eye_side,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               COALESCE(pr.box_size, 1)      AS box_size,
               COALESCE(pr.unit, 'PCS')      AS unit,
               COALESCE(ol.lens_params->>'coating_type', ol.lens_params->>'coating', '') AS coating_type,
               COALESCE(ol.lens_params->>'index_value', ol.lens_params->>'lens_index',
                        ol.lens_params->>'refractive_index', '') AS lens_index,
               ol.lens_params AS lens_params,
               COALESCE(NULLIF(pa.batch_no,''), NULLIF(poi.batch_no,''))  AS batch_no,
               COALESCE(pa.expiry_date, poi.expiry_date)                   AS expiry_date,
               COALESCE(o.order_no, '')                                    AS order_no,
               COALESCE(o.customer_order_no, '')                           AS cust_order_no
        FROM invoice_lines il
        LEFT JOIN order_lines ol ON ol.id = il.order_line_id
        LEFT JOIN orders o       ON o.id  = il.order_id
        LEFT JOIN products pr    ON pr.id = ol.product_id
        LEFT JOIN purchase_acknowledgements pa  ON pa.order_line_id = ol.id
        LEFT JOIN procurement_order_items   poi ON poi.order_line_id = ol.id
        WHERE il.invoice_id = %(id)s
          AND NOT COALESCE(il.is_deleted, FALSE)
        ORDER BY COALESCE(il.eye_side,''), il.id
    """, {"id": invoice_id})

    _needs_challan_snapshot = False
    if challan_id:
        if not lines:
            _needs_challan_snapshot = True
        else:
            for _ln in lines:
                _taxable = float(_ln.get("taxable_amount") or _ln.get("total_price") or 0)
                _final = float(_ln.get("line_total") or 0)
                _tax = float(_ln.get("tax_amount") or 0)
                if _final <= 0 or (_tax <= 0 and _final <= _taxable + 0.01):
                    _needs_challan_snapshot = True
                    break

    if _needs_challan_snapshot:
        lines = _q("""
            SELECT cl.quantity, cl.unit_price,
                   COALESCE(cl.total_price, 0) AS taxable_amount,
                   COALESCE(cl.line_total, cl.total_price, 0) AS line_total,
                   COALESCE(cl.line_total, cl.total_price, 0) AS total_price,
                   ROUND(COALESCE(cl.line_total, cl.total_price, 0) - COALESCE(cl.total_price, 0), 2) AS tax_amount,
                   COALESCE(ol.gst_percent, 0) AS gst_percent,
                   UPPER(COALESCE(o.order_type, 'WHOLESALE')) AS order_type,
                   COALESCE(cl.product_name, pr.product_name, 'Lens') AS product_name,
                   COALESCE(cl.brand, pr.brand, '') AS brand,
                   COALESCE(cl.eye_side, ol.eye_side) AS eye_side,
                   ol.sph, ol.cyl, ol.axis, ol.add_power,
                   COALESCE(pr.box_size, 1) AS box_size,
                   COALESCE(pr.unit, 'PCS') AS unit,
                   COALESCE(ol.lens_params->>'coating_type', ol.lens_params->>'coating', '') AS coating_type,
                   COALESCE(ol.lens_params->>'index_value', ol.lens_params->>'lens_index',
                            ol.lens_params->>'refractive_index', '') AS lens_index,
                   ol.lens_params AS lens_params,
                   COALESCE(NULLIF(pa.batch_no,''), NULLIF(poi.batch_no,''))  AS batch_no,
                   COALESCE(pa.expiry_date, poi.expiry_date)                   AS expiry_date
            FROM challan_lines cl
            LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
            LEFT JOIN orders o       ON o.id  = cl.order_id
            LEFT JOIN products pr    ON pr.id = ol.product_id
            LEFT JOIN purchase_acknowledgements pa  ON pa.order_line_id = ol.id
            LEFT JOIN procurement_order_items   poi ON poi.order_line_id = ol.id
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
            cl.quantity,
            cl.unit_price,
            COALESCE(cl.total_price, 0) AS taxable_amount,
            COALESCE(cl.line_total, cl.total_price, 0) AS line_total,
            COALESCE(cl.line_total, cl.total_price, 0) AS total_price,
            COALESCE(ol.gst_percent, 0)                 AS gst_percent,
            COALESCE(cl.product_name, pr.product_name, 'Lens') AS product_name,
            COALESCE(cl.brand, pr.brand, '')            AS brand,
            COALESCE(cl.eye_side, ol.eye_side, '')      AS eye_side,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            COALESCE(pr.box_size, 1)                    AS box_size,
            COALESCE(pr.unit, 'PCS')                    AS unit,
            COALESCE(o.order_no, '')                    AS order_no,
            COALESCE(o.customer_order_no, '')           AS cust_order_no,
            UPPER(COALESCE(o.order_type, 'WHOLESALE'))  AS order_type,
            COALESCE(ol.lens_params->>'coating_type', ol.lens_params->>'coating', '') AS coating_type,
            COALESCE(ol.lens_params->>'index_value', ol.lens_params->>'lens_index',
                     ol.lens_params->>'refractive_index', '') AS lens_index,
            ol.lens_params AS lens_params,
            -- Batch & expiry from procurement (contact lenses / batch-tracked)
            COALESCE(NULLIF(pa.batch_no,''), NULLIF(poi.batch_no,''))       AS batch_no,
            COALESCE(pa.expiry_date, poi.expiry_date)                        AS expiry_date
        FROM challan_lines cl
        LEFT JOIN order_lines ol ON ol.id  = cl.order_line_id
        LEFT JOIN orders o       ON o.id   = cl.order_id
        LEFT JOIN products pr    ON pr.id  = ol.product_id
        LEFT JOIN purchase_acknowledgements pa  ON pa.order_line_id = ol.id
        LEFT JOIN procurement_order_items   poi ON poi.order_line_id = ol.id
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
    paper_size: str = "A5",
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

    # ── Barcode SVGs for challan corner ──────────────────────────────
    _ch_bc_svg      = _bc_svg(ch_no, width=160, height=36)
    _ch_party_bc_v  = str(challan.get("party_barcode") or challan.get("patient_barcode") or "").strip()
    _ch_party_bc    = _bc_svg(_ch_party_bc_v, width=130, height=30) if _ch_party_bc_v else ""
    _bc_corner_html = (
        f"<div style='text-align:center;margin-top:4px'>"
        f"<div style='font-size:6.5pt;color:#64748b;letter-spacing:.05em;margin-bottom:1px'>SCAN CHALLAN</div>"
        f"{_ch_bc_svg}"
        + (f"<div style='font-size:6pt;color:#94a3b8;margin-top:3px'>Party</div>{_ch_party_bc}" if _ch_party_bc else "")
        + "</div>"
    )

    # ── Build line rows ───────────────────────────────────────────────────────
    rows_html      = ""
    subtotal       = 0.0
    total_tax      = 0.0
    gst_buckets: Dict[float, float] = {}
    _otype = "RETAIL"
    if lines:
        _otype = str(lines[0].get("order_type") or "WHOLESALE").upper()

    for ln in lines:
        eye   = str(ln.get("eye_side") or "").upper()
        pname = str(ln.get("product_name") or "")
        # Prices are stored per-PCS in DB — use directly, do NOT divide by box_size
        up_raw = float(ln.get("unit_price") or 0)
        gst_p  = float(ln.get("gst_percent") or 0)

        # ── R+L pairing: find partner with same product+price ──────────
        _partner_eye = {"R": "L", "L": "R"}.get(eye)
        _partner = None
        if _partner_eye:
            for _pl in lines:
                if (str(_pl.get("eye_side") or "").upper() == _partner_eye and
                        str(_pl.get("product_name") or "") == pname and
                        abs(float(_pl.get("unit_price") or 0) - up_raw) < 0.01):
                    _partner = _pl
                    break

        if _partner and eye == "L":
            continue  # Already rendered+counted as part of R row

        # Qty and base: double when grouped R+L
        qty  = int(ln.get("quantity") or 0)
        if _partner:
            qty += int(_partner.get("quantity") or 0)

        up       = up_raw  # alias used in HTML format string below
        base     = round(float(ln.get("taxable_amount") or 0), 2)
        line_tot = round(float(ln.get("line_total") or 0), 2)
        if _partner:
            base = round(base + float(_partner.get("taxable_amount") or 0), 2)
            line_tot = round(line_tot + float(_partner.get("line_total") or 0), 2)
        if line_tot <= 0:
            _gc = compute_line_gst(up_raw, qty, gst_p, _otype)
            base = _gc["gst_base"]
            line_tot = _gc["grand_total"]
        tax_a    = round(line_tot - base, 2)
        if gst_p > 0 and tax_a <= 0.01 and line_tot > 0:
            if _otype == "RETAIL":
                base = round(line_tot * 100 / (100 + gst_p), 2)
                tax_a = round(line_tot - base, 2)
            elif base > 0:
                tax_a = round(base * gst_p / 100, 2)
                line_tot = round(base + tax_a, 2)
        subtotal  += base
        total_tax += tax_a
        if gst_p > 0:
            gst_buckets[gst_p] = round(gst_buckets.get(gst_p, 0.0) + tax_a, 2)

        display_up = up
        _offer_note, _avg_up = _cart_offer_note(ln, qty, line_tot)
        if _avg_up is not None:
            display_up = _avg_up

        coating = str(ln.get("coating_type") or "").strip()
        idx_val = str(ln.get("lens_index") or "").strip()
        brand   = str(ln.get("brand") or "")
        _eye_label = {"R": "Right Eye", "L": "Left Eye", "B": "Both Eyes", "S": "Service"}.get(eye, "")
        _eye_cls   = {"R": "eye-R", "L": "eye-L", "B": "eye-B"}.get(eye, "")

        # Product name + index + coating sub-line
        _prod_sub = ""
        if idx_val and idx_val not in pname:
            _prod_sub += " Index {}".format(idx_val)
        if coating and coating not in pname:
            _prod_sub += " {}".format(coating)

        desc_html = "<b>{}</b>".format(pname)
        if _prod_sub:
            desc_html += "<div style='font-size:0.72rem;color:#334155'>{}</div>".format(_prod_sub.strip())
        if brand and brand not in pname:
            desc_html += "<div style='font-size:0.63rem;color:#64748b'>{}</div>".format(brand)
        if _offer_note:
            desc_html += (
                "<div style='font-size:0.65rem;color:#7c2d12;font-weight:700;"
                "margin-top:2px'>Scheme: {}</div>".format(_offer_note)
            )

        # Batch / expiry — show for contact lenses and batch-tracked products
        _batch_c   = str(ln.get("batch_no") or "").strip()
        _expiry_c  = ln.get("expiry_date")
        _exp_str_c = ""
        if _expiry_c:
            try:
                _ep_c = str(_expiry_c)[:7].split("-")
                if len(_ep_c) == 2:
                    _exp_str_c = "{}/{}".format(_ep_c[1], _ep_c[0])
                else:
                    _exp_str_c = str(_expiry_c)[:10]
            except Exception:
                _exp_str_c = str(_expiry_c)[:10]
        if _batch_c or _exp_str_c:
            _be_parts_c = []
            if _batch_c:   _be_parts_c.append("Batch: {}".format(_batch_c))
            if _exp_str_c: _be_parts_c.append("Exp: {}".format(_exp_str_c))
            desc_html += (
                "<div style='font-size:0.65rem;color:#0f766e;font-weight:600;"
                "margin-top:2px'>" + "  ·  ".join(_be_parts_c) + "</div>"
            )

        # Show box count when product is sold in multi-packs
        _bsz = int(ln.get("box_size") or 1)
        if _bsz > 1:
            _total_pcs = qty  # qty already summed R+L if paired
            _boxes = _total_pcs // _bsz
            _loose = _total_pcs % _bsz
            _box_str = f"{_boxes} × {_bsz}PK" + (f" + {_loose} PCS" if _loose else "")
            desc_html += "<div style='font-size:0.65rem;color:#475569'>({} = {} PCS)</div>".format(_box_str, _total_pcs)

        # Power + order no — full-width sub-row
        _power_sub_row_ch = ""
        _ord_no_ch   = str(ln.get("order_no") or "").strip()
        _cust_ord_ch2 = str(ln.get("cust_order_no") or "").strip()
        import re as _re_uuid2
        _is_uuid_ch = bool(_re_uuid2.match(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            _cust_ord_ch2, _re_uuid2.IGNORECASE
        ))
        _ord_parts_ch = []
        if _ord_no_ch: _ord_parts_ch.append("#" + _ord_no_ch)
        if _cust_ord_ch2 and _cust_ord_ch2 != _ord_no_ch and not _is_uuid_ch:
            _ord_parts_ch.append("Ref:" + _cust_ord_ch2)
        _ord_bit_ch  = ("<span style='font-size:0.6rem;color:#94a3b8;margin-right:8px'>{}</span>".format("  ".join(_ord_parts_ch))) if _ord_parts_ch else ""
        if _partner:
            if show_powers:
                _r_pw = _power_str(ln.get("sph"), ln.get("cyl"), ln.get("axis"), ln.get("add_power"))
                _l_pw = _power_str(_partner.get("sph"), _partner.get("cyl"),
                                    _partner.get("axis"), _partner.get("add_power"))
                _power_sub_row_ch = (
                    "<tr style='background:#f8faff'>"
                    "<td colspan='5' style='padding:2px 5px 5px 8px;border-bottom:1px solid #e8eef8'>"
                    + _ord_bit_ch +
                    "<span style='white-space:nowrap'>"
                    "<span class='eye-R'>R</span>"
                    "<span class='pw'>&nbsp;" + _r_pw + "</span>"
                    "</span>"
                    "<span style='color:#cbd5e1;font-size:0.65rem'>&nbsp;&nbsp;|&nbsp;&nbsp;</span>"
                    "<span style='white-space:nowrap'>"
                    "<span class='eye-L'>L</span>"
                    "<span class='pw'>&nbsp;" + _l_pw + "</span>"
                    "</span>"
                    "</td></tr>"
                )
            else:
                desc_html += "<div style='margin-top:2px'><span class='eye-R'>R</span>&nbsp;<span class='eye-L'>L</span></div>"
        else:
            if show_powers and any(ln.get(k) is not None for k in ("sph","cyl","axis","add_power")):
                pw = _power_str(ln.get("sph"), ln.get("cyl"), ln.get("axis"), ln.get("add_power"))
                _badge_c = "<span class='{}'>{}</span>&nbsp;".format(_eye_cls, eye) if _eye_cls else ""
                _power_sub_row_ch = (
                    "<tr style='background:#f8faff'>"
                    "<td colspan='5' style='padding:2px 5px 5px 8px;border-bottom:1px solid #e8eef8'>"
                    + _ord_bit_ch + _badge_c +
                    "<span class='pw'>" + pw + "</span>"
                    "</td></tr>"
                )
            elif _eye_cls:
                desc_html += "<div style='margin-top:2px'><span class='{}'>{}</span></div>".format(_eye_cls, eye)

        # GST column — percentage only
        _tax_cell_ch = "<span style='font-size:0.7rem;font-weight:600'>{}%</span>".format(
            int(gst_p) if gst_p else "—")

        # Qty and rate display — box products: BOX as main unit, rate per box
        _bsz_d2 = int(ln.get('box_size') or 1)
        if _bsz_d2 > 1:
            _boxes_d2 = qty // _bsz_d2
            _loose_d2 = qty % _bsz_d2
            _qty_display = ("<span style='white-space:nowrap'><b>{}</b> Box "
                "<span style='font-size:0.57rem;color:#94a3b8'>({}p)</span></span>").format(_boxes_d2, qty)
            display_up = round(display_up * _bsz_d2, 2)
        else:
            _qty_display = str(qty)

        _up_note_ch = ""
        if not _partner and eye in ("R", "L") and qty > 1:
            _up_note_ch = "<div style='font-size:0.6rem;color:#94a3b8'>×{} pcs @ {}/pc</div>".format(
                qty, _fc(display_up))

        # Order No below rate
        _ord_no_ch   = str(ln.get("order_no") or "").strip()
        _cust_ord_ch = str(ln.get("cust_order_no") or "").strip()
        _ord_note_ch = ""
        if _ord_no_ch:
            _ord_note_ch = "<div style='font-size:0.58rem;color:#94a3b8;margin-top:2px'>#{}</div>".format(_ord_no_ch)
            if _cust_ord_ch and _cust_ord_ch != _ord_no_ch:
                _ord_note_ch += "<div style='font-size:0.56rem;color:#b0bec5'>{}</div>".format(_cust_ord_ch)

        rows_html += (
            "<tr>"
            "<td>{desc}</td>"
            "<td class='r'>{qty}</td>"
            "<td class='r'>{up}{note}{ord}</td>"
            "<td class='r'>{tax}</td>"
            "<td class='r'><b>{tot}</b></td>"
            "</tr>"
        ).format(
            desc=desc_html, qty=_qty_display,
            up=_fc(display_up), note=_up_note_ch, ord=_ord_note_ch,
            tax=_tax_cell_ch, tot=_fc(line_tot)
        )
        rows_html += _power_sub_row_ch

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
            "<td class='r'><span style='font-size:0.65rem'>{gst}%<br>{tax}</span></td>"
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
    round_off = float(challan.get("round_off_amount") or 0)
    grand_display = float(challan.get("grand_total") or 0)
    if grand_display <= 0:
        grand_display = round(grand)
        round_off = round(grand_display - grand, 2)
    elif abs(round_off) < 0.005 and abs(grand_display - grand) >= 0.005:
        round_off = round(grand_display - grand, 2)
    roundoff_row_html = ""
    if abs(round_off) >= 0.005:
        roundoff_row_html = (
            "<tr class='sub'>"
            "<td colspan='4'><b>Round Off</b></td>"
            "<td class='r'>{roundoff}</td>"
            "</tr>"
        ).format(roundoff=_fc(round_off))

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
    

    # Bank + UPI QR for challan
    _ch_bank_parts = []
    if shop.get("bank_name"):    _ch_bank_parts.append("<b>Bank:</b> {}".format(shop["bank_name"]))
    if shop.get("bank_account"): _ch_bank_parts.append("<b>A/C:</b> {}".format(shop["bank_account"]))
    if shop.get("bank_ifsc"):    _ch_bank_parts.append("<b>IFSC:</b> {}".format(shop["bank_ifsc"]))
    if shop_upi:                 _ch_bank_parts.append("<b>UPI:</b> {}".format(shop_upi))
    _ch_qr_html = ""
    if shop_upi:
        try:
            import qrcode as _qr_mod, io as _io_qr, base64 as _b64_qr
            _upi_str = _upi_pay_uri(shop_upi, shop_name, grand_display, ch_no)
            _qr2 = _qr_mod.QRCode(version=None, error_correction=_qr_mod.constants.ERROR_CORRECT_M, box_size=3, border=2)
            _qr2.add_data(_upi_str); _qr2.make(fit=True)
            _img2 = _qr2.make_image(fill_color="black", back_color="white")
            _buf2 = _io_qr.BytesIO(); _img2.save(_buf2, format="PNG")
            _b64v = _b64_qr.b64encode(_buf2.getvalue()).decode()
            _ch_qr_html = (
                "<div style='text-align:center;margin-left:10px'>"
                "<div style='width:55px;height:55px;display:block;margin:0 auto;background-image:url(data:image/png;base64,{});background-size:contain;background-repeat:no-repeat;-webkit-print-color-adjust:exact;print-color-adjust:exact;color-adjust:exact'></div>"
                "<div style='font-size:0.52rem;color:#64748b;margin-top:1px'>Scan to Pay</div>"
                "</div>"
            ).format(_b64v)
        except Exception:
            _ch_qr_html = ""
    ch_bank_html = (
        "<div class='bank-box' style='display:flex;align-items:center;justify-content:space-between'>"
        "<div style='font-size:0.64rem'>{}</div>{}</div>"
    ).format("&nbsp;|&nbsp;".join(_ch_bank_parts), _ch_qr_html) if _ch_bank_parts else ""

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

    _is_a4_ch = str(paper_size).upper() == 'A4'
    if _is_a4_ch:
        _CSS_USE_CH = (_CSS
            .replace('@page {\n    size: A5;\n    margin: 5mm;\n}  /* default — overridden at build */',
                     '@page { size: A4 portrait; margin: 10mm 12mm; }')
            .replace(
                    '.inv-wrap {\n    font-family:',
                    '.inv-wrap {\n    width: 186mm;\n    font-size: 0.82rem;\n    font-family:'
                )
            .replace('.inv-table{width:100%', '.inv-table{width:100%;font-size:0.8rem')
        )
    else:
        _CSS_USE_CH = _CSS

    html = _CSS_USE_CH + (
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
        "{bc_corner_html}"
        "</div>"
        "{print_button_part}"
        "</div>"

        # ── BILL TO — full width compact row ─────────────────────────
        "<div style='padding:5px 10px;background:#f8fafc;border:1px solid #e2e8f0;"
        "border-radius:5px;margin-bottom:8px;display:flex;gap:20px;align-items:baseline'>"
        "<div style='min-width:0;flex:1'>"
        "<span class='p-lbl'>Bill To &nbsp;</span>"
        "<span class='p-val'>{party_name}</span>"
        "<span class='p-sub' style='margin-left:6px'>{party_sub}</span>"
        "{party_gstin_html}"
        "</div>"
        "<div style='text-align:right;flex-shrink:0'>"
        "</div>"
        "</div>"

        # LINE TABLE
        "<table class='inv-table'>"
        "<thead><tr>"
        "<th style='text-align:left'>Product / Description</th>"
        "<th style='text-align:center'>Qty</th>"
        "<th style='text-align:right'>Rate</th>"
        "<th style='text-align:center'>GST%</th>"
        "<th style='text-align:right'>Total</th>"
        "</tr></thead>"
        "<tbody>"
        "{rows_html}"
        "</tbody></table>"

        # TAX SUMMARY
        "<div class='tax-box'>"
        "<div style='font-size:0.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:5px'>Tax Summary</div>"
        "<div class='tx'><span>Taxable Value</span><span>{subtotal2}</span></div>"
        "{tax_rows_html}"
        ""
        "{roundoff_tax_html}"
        "<div class='tx g'><span>Grand Total</span><span>{grand3}</span></div>"
        "<div style='font-size:0.62rem;color:#94a3b8;margin-top:4px'>"
        "Supply type: {supply_text2}"
        "{gstin_note}"
        "</div>"
        "</div>"

        "{remarks_html}"
        "{ch_bank_html}"

        # SIGNATURE
        "<div class='sign-row'>"
        "<div>"
        "<div style='margin-bottom:22px;font-size:0.72rem;font-weight:600'>{shop_name3}</div>"
        "<div class='sig-line'>Authorised Signatory</div>"
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
        grand_raw=_fc(grand), roundoff_row_html=roundoff_row_html,
        grand2=_fc(grand_display),
        subtotal2=_fc(subtotal), total_tax2=_fc(total_tax), grand3=_fc(grand_display),
        roundoff_tax_html=(
            "<div class='tx'><span>Round Off</span><span>{}</span></div>".format(_fc(round_off))
            if abs(round_off) >= 0.005 else ""
        ),
        tax_rows_html=tax_rows_html,
        supply_text2=supply_text,
        gstin_note=(" · GSTIN: " + party_gstin) if party_gstin else (" · Unregistered / Retail" if not challan.get("party_id") else " · GSTIN not recorded"),
        remarks_html=remarks_html,
        ch_bank_html=ch_bank_html,
        shop_name3=shop_name,
        footer_html=footer_html,
        print_button_part=print_button_part,
        script_part=script_part,
        bc_corner_html=_bc_corner_html,
    )
    return html


def render_smart_challan(challan_no: str, return_html: bool = False, doc_type_override: str = None, show_powers_override: bool = None, paper_size_override: str = None):
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
    _ch_btn_key = re.sub(r'[^a-zA-Z0-9_]', '_', str(challan_no))
    if not return_html:
        st.markdown("#### 🖨️ Challan Print Options")
        c1, c2, c3 = st.columns(3)
        doc_type    = "DELIVERY CHALLAN"  # Challan always prints as Delivery Challan
        show_powers = c2.checkbox("Show Lens Powers",
                                  value=bool(ch.get("print_with_powers", True)),
                                  key="spc_pw_" + _ch_btn_key)
        paper_size = c3.radio("Paper", ["A5", "A4"], horizontal=True,
                              key="spc_paper_" + _ch_btn_key)

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
        paper_size = str(paper_size_override or "A5").upper()

    html = build_challan_html(
        challan=ch, lines=lines, svc_lines=svc_lines,
        shop=shop, show_powers=show_powers, doc_type=doc_type,
        include_script = not return_html,
        paper_size=paper_size,
    )
    if return_html:
        return html
    _c1, _c2 = st.columns(2)
    if _c1.button("Direct Print", key="spc_direct_" + _ch_btn_key, use_container_width=True):
        _safe_chal = re.sub(r'[/\\:*?"<>|]', '-', str(challan_no))
        _direct_or_browser_print(html, f"challan_{_safe_chal}.html", f"Challan_{_safe_chal}")
    if _c2.button("Open Print in Browser", key="spc_open_" + _ch_btn_key, use_container_width=True):
        try:
            from modules.printing.print_opener import open_html_print

            _safe_chal = re.sub(r'[/\\:*?"<>|]', '-', str(challan_no))
            path = open_html_print(html, f"challan_{_safe_chal}.html")
            st.success(f"Challan opened: {path}")
        except Exception as exc:
            st.error(f"Challan print open failed: {exc}")
    st.components.v1.html(html, height=920, scrolling=True)
