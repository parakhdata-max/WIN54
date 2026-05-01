"""
Challan & Invoice Module
========================
Enterprise billing pipeline — Challan creation → Invoice generation → Payment tracking.
SAP/Oracle-grade UX: dark command-centre theme, contextual actions, zero wasted clicks.

Data flow:
  orders → challan_lines → challans → invoice_lines → invoices → payments
"""

import streamlit as st
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime, date, timedelta
import uuid as _uuid
from modules.core.price_qty_governor import (
    normalize_to_pcs_price,
    compute_line_gst,
    reverse_qty,
    PAIR_TO_PCS,
)


# ══════════════════════════════════════════════════════════════════════════
# THEME — injected once at module import
# ══════════════════════════════════════════════════════════════════════════

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap');

/* ── Base ─────────────────────────────────────────────────────────────── */
section.main > div { font-family: 'IBM Plex Sans', sans-serif !important; }

/* ── KPI cards ───────────────────────────────────────────────────────── */
.kpi-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }
.kpi { background:#0f172a; border:1px solid #1e293b; border-radius:10px;
       padding:16px 20px; border-top:3px solid var(--accent,#3b82f6); }
.kpi-label { color:#475569; font-size:0.68rem; letter-spacing:.08em; text-transform:uppercase; margin-bottom:4px; }
.kpi-value { color:#f1f5f9; font-size:1.5rem; font-weight:700; font-family:'IBM Plex Mono',monospace; }
.kpi-sub   { color:#64748b; font-size:0.65rem; margin-top:3px; }

/* ── Status pills ────────────────────────────────────────────────────── */
.pill { display:inline-block; padding:2px 10px; border-radius:20px;
        font-size:0.65rem; font-weight:700; letter-spacing:.04em; }
.pill-pending   { background:#f59e0b22; color:#f59e0b; border:1px solid #f59e0b44; }
.pill-invoiced  { background:#10b98122; color:#10b981; border:1px solid #10b98144; }
.pill-paid      { background:#10b98122; color:#10b981; border:1px solid #10b98144; }
.pill-unpaid    { background:#ef444422; color:#ef4444; border:1px solid #ef444444; }
.pill-partial   { background:#f59e0b22; color:#f59e0b; border:1px solid #f59e0b44; }
.pill-cancelled { background:#64748b22; color:#64748b; border:1px solid #64748b44; }
.pill-overdue   { background:#ef444422; color:#ef4444; border:1px solid #ef444444; }
.pill-confirmed { background:#3b82f622; color:#3b82f6; border:1px solid #3b82f644; }

/* ── Doc header ──────────────────────────────────────────────────────── */
.doc-header { background:#0f172a; border:1px solid #1e293b; border-radius:12px;
              padding:20px 24px; margin-bottom:16px; }
.doc-no { font-family:'IBM Plex Mono',monospace; font-size:1.1rem;
          font-weight:600; color:#38bdf8; letter-spacing:.04em; }
.doc-meta { color:#64748b; font-size:0.72rem; margin-top:6px; }
.doc-party { color:#e2e8f0; font-size:0.95rem; font-weight:600; }

/* ── Line table ──────────────────────────────────────────────────────── */
.line-table { width:100%; border-collapse:collapse; font-size:0.78rem; }
.line-table th { background:#0f172a; color:#64748b; font-weight:600;
                 letter-spacing:.06em; text-transform:uppercase; font-size:0.62rem;
                 padding:10px 12px; text-align:left; border-bottom:2px solid #1e293b; }
.line-table td { padding:9px 12px; border-bottom:1px solid #1e293b; color:#cbd5e1; }
.line-table tr:hover td { background:#0f172a88; }
.line-table .mono { font-family:'IBM Plex Mono',monospace; font-size:0.75rem; }
.line-table .num  { text-align:right; font-family:'IBM Plex Mono',monospace; }
.line-table tfoot td { background:#0f172a; color:#94a3b8; font-weight:600;
                       border-top:2px solid #1e293b; padding:10px 12px; }
.line-table .grand { color:#38bdf8 !important; font-size:0.9rem; }

/* ── Section headers ─────────────────────────────────────────────────── */
.sec-head { color:#94a3b8; font-size:0.65rem; letter-spacing:.12em;
            text-transform:uppercase; font-weight:600; margin:16px 0 8px; }

/* ── Action bar ──────────────────────────────────────────────────────── */
.action-bar { background:#0f172a; border:1px solid #1e293b; border-radius:8px;
              padding:12px 16px; display:flex; gap:10px; align-items:center;
              margin-top:16px; flex-wrap:wrap; }

/* ── Order row ───────────────────────────────────────────────────────── */
.order-row { background:#0f172a; border:1px solid #1e293b; border-radius:8px;
             padding:10px 14px; margin-bottom:6px; display:flex;
             justify-content:space-between; align-items:center; }
.order-no  { font-family:'IBM Plex Mono',monospace; color:#38bdf8; font-size:0.78rem; font-weight:600; }
.order-amt { font-family:'IBM Plex Mono',monospace; color:#10b981; font-size:0.82rem; font-weight:700; }

/* ── Party card ──────────────────────────────────────────────────────── */
.party-card { background:#0f172a; border:1px solid #1e293b; border-radius:10px; padding:16px 18px; }
.party-name { color:#f1f5f9; font-size:1rem; font-weight:600; margin-bottom:8px; }
.party-field { display:flex; gap:8px; margin-bottom:4px; }
.party-label { color:#475569; font-size:0.68rem; min-width:60px; }
.party-value { color:#94a3b8; font-size:0.72rem; }

/* ── Timeline ────────────────────────────────────────────────────────── */
.timeline { display:flex; align-items:center; gap:0; margin:12px 0 20px; }
.tl-step { flex:1; text-align:center; position:relative; }
.tl-dot  { width:28px; height:28px; border-radius:50%; margin:0 auto 4px;
           display:flex; align-items:center; justify-content:center;
           font-size:0.7rem; font-weight:700; }
.tl-done  { background:#10b981; color:#fff; }
.tl-active{ background:#3b82f6; color:#fff; box-shadow:0 0 0 4px #3b82f622; }
.tl-wait  { background:#1e293b; color:#475569; }
.tl-label { font-size:0.58rem; color:#475569; letter-spacing:.04em; }
.tl-line  { position:absolute; top:14px; left:50%; width:100%;
            height:2px; background:#1e293b; z-index:-1; }
.tl-line-done { background:#10b981; }

/* ── Divider ─────────────────────────────────────────────────────────── */
.ent-divider { border:none; border-top:1px solid #1e293b; margin:16px 0; }
</style>
"""


# ══════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════


# ── GST Split Utility ──────────────────────────────────────────────────────
# State code is read dynamically from shop_master GSTIN (first 2 digits)
# Fallback: 27 (Maharashtra)
def _get_our_state_code() -> str:
    try:
        from modules.settings.shop_master import get_unit_info
        d = get_unit_info("retail") or {}
        gstin = str(d.get("shop_gstin") or "").strip()
        if len(gstin) >= 2 and gstin[:2].isdigit():
            return gstin[:2]
    except Exception:
        pass
    return "27"

def _get_our_state_name() -> str:
    try:
        from modules.settings.shop_master import get_unit_info
        d = get_unit_info("retail") or {}
        return str(d.get("shop_state") or "Maharashtra")
    except Exception:
        return "Maharashtra"

OUR_STATE_CODE = _get_our_state_code()
OUR_STATE_NAME = _get_our_state_name()

def _gst_split(total_tax: float, party_gstin: str = "", place_of_supply: str = "") -> dict:
    """
    Split total GST into CGST+SGST (intra-state) or IGST (inter-state).

    Rules:
      - Party GSTIN starts with "27" → Maharashtra → intra-state → CGST+SGST
      - Party GSTIN starts with other code → inter-state → IGST
      - No GSTIN (retail/unregistered) → assume intra-state → CGST+SGST
      - place_of_supply override → compare with OUR_STATE_CODE

    Returns: {cgst, sgst, igst, supply_type: "INTRA"/"INTER"}
    """
    t = round(float(total_tax or 0), 2)
    if t == 0:
        return {"cgst": 0.0, "sgst": 0.0, "igst": 0.0, "supply_type": "INTRA"}

    # Determine supply type
    is_inter = False
    if place_of_supply and str(place_of_supply).strip()[:2] not in ("", OUR_STATE_CODE, OUR_STATE_NAME[:2]):
        is_inter = True
    elif party_gstin and len(str(party_gstin)) >= 2:
        gstin_state = str(party_gstin).strip()[:2]
        if gstin_state.isdigit() and gstin_state != OUR_STATE_CODE:
            is_inter = True

    if is_inter:
        return {"cgst": 0.0, "sgst": 0.0, "igst": t, "supply_type": "INTER"}
    else:
        half = round(t / 2, 2)
        # Handle odd penny
        return {"cgst": half, "sgst": round(t - half, 2), "igst": 0.0, "supply_type": "INTRA"}


def _q(sql: str, params: dict = None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"Database error: {e}")
        return []


def _write(sql: str, params: dict = None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as _we:
        import logging as _lg_w
        _lg_w.getLogger(__name__).error(f"_write FAILED: {_we} | SQL: {sql[:120]}")
        try:
            from modules.sql_adapter import execute_query
            execute_query(sql, params or {})
            return True
        except Exception as _we2:
            _lg_w.getLogger(__name__).error(f"_write fallback FAILED: {_we2}")
    return False


# ══════════════════════════════════════════════════════════════════════════
# BUSINESS LOGIC HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _is_order_billing_ready(order_id: str) -> Tuple[bool, str]:
    """
    Gate check: all lines must have completed their manufacturing/supply pipeline.
    STOCK        → allocated_qty >= quantity
    VENDOR       → supplier received >= ordered
    INHOUSE      → job stage in READY/DISPATCHED/CLOSED
    COUNTER_SALE → always ready (stock verified + FIFO-allocated at cart build time)
    Returns (ready: bool, reason: str)
    """
    lines = _q("""
        SELECT ol.id, ol.quantity, ol.allocated_qty,
               COALESCE(ol.ready_qty,  0)  AS ready_qty,
               COALESCE(ol.billed_qty, 0)  AS billed_qty,
               COALESCE(ol.lens_params->'manufacturing_route','STOCK') AS manufacturing_route,
               COALESCE(ol.lens_params->'order_source','')             AS line_order_source,
               ol.status AS line_status,
               COALESCE(ol.is_service_line, FALSE) AS is_service_line,
               COALESCE(ol.eye_side, '')            AS eye_side,
               COALESCE(o.order_source, '')          AS order_source
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        WHERE ol.order_id = %(oid)s::uuid
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
    """, {"oid": order_id})

    if not lines:
        return True, "No lines — allow billing"

    for ln in lines:
        qty   = int(ln.get("quantity")      or 0)
        alloc = int(ln.get("allocated_qty") or 0)
        ready = int(ln.get("ready_qty")     or 0)
        route = str(ln.get("manufacturing_route") or "STOCK").upper()

        # SERVICE lines (eye_side=S or is_service_line=TRUE) are always ready —
        # no stock, no supplier, no job card needed. Auto-allocated at save time.
        _is_svc_line = bool(ln.get("is_service_line")) or str(ln.get("eye_side","")).upper() in ("S","SERVICE")
        if _is_svc_line:
            continue   # always billing-ready

        # COUNTER_SALE lines — stock verified + FIFO-allocated at cart build time.
        # allocated_qty = quantity on insert; pipeline gate does not apply.
        # Falls back to lens_params for pre-fix orders (migration 004 corrects those).
        _order_src = str(ln.get("order_source") or ln.get("line_order_source") or "").upper()
        if _order_src == "COUNTER_SALE":
            continue   # always billing-ready

        if route == "STOCK":
            if alloc < qty:
                return False, f"Stock not fully allocated ({alloc}/{qty})"

        elif route in ("VENDOR", "EXTERNAL_LAB"):
            rcv = _q("""
                SELECT COALESCE(SUM(soi.received_qty),0) AS rcv,
                       COALESCE(SUM(soi.ordered_qty), 0) AS ord
                FROM supplier_order_items soi
                JOIN supplier_orders so ON so.id = soi.supplier_order_id
                WHERE so.order_id = %(oid)s::uuid
            """, {"oid": order_id})
            if rcv:
                r, o = int(rcv[0]["rcv"] or 0), int(rcv[0]["ord"] or 0)
                if o > 0 and r < o:
                    return False, f"Supplier delivery pending ({r}/{o})"

        elif route == "INHOUSE":
            jobs = _q("""
                SELECT current_stage, is_closed FROM job_master
                WHERE order_line_id = %(lid)s::uuid
            """, {"lid": ln["id"]})
            if not jobs:
                return False, "Job card not created yet"
            for j in jobs:
                if not j.get("is_closed") and str(j.get("current_stage") or "").upper() \
                        not in ("READY", "DISPATCHED", "CLOSED"):
                    return False, f"Production not complete (stage: {j.get('current_stage')})"

    return True, "All lines complete"


def _billing_ready_orders_sql() -> str:
    """Orders available for challan/invoice — excludes already-billed ones.
    Uses NOT EXISTS on challan_lines/invoice_lines instead of scanning
    the order_ids array columns, which becomes slow at 10k+ challans.
    """
    return """
        SELECT o.id, o.order_no, o.created_at, o.total_value,
               o.party_name, o.party_id, o.order_type, o.status,
               COUNT(ol.id) AS line_count
        FROM orders o
        LEFT JOIN order_lines ol ON ol.order_id = o.id
        WHERE o.status IN ('PENDING','CONFIRMED','UNDER_REVIEW','READY','READY_FOR_BILLING',
                           'IN_PRODUCTION','PARTIALLY_RECEIVED','BILLED')
          AND NOT EXISTS (
              SELECT 1
              FROM challan_lines cl
              JOIN challans c ON c.id = cl.challan_id
              WHERE cl.order_id = o.id
                AND c.status NOT IN ('CANCELLED','VOID')
          )
          AND NOT EXISTS (
              SELECT 1
              FROM invoice_lines il
              JOIN invoices i ON i.id = il.invoice_id
              WHERE il.order_id = o.id
                AND i.status NOT IN ('CANCELLED','VOID')
          )
        GROUP BY o.id, o.order_no, o.created_at, o.total_value,
                 o.party_name, o.party_id, o.order_type, o.status
        ORDER BY o.created_at DESC
    """


def _calc_billing_totals(order_ids: list) -> dict:
    """
    Compute billing totals from order_lines.
    Returns {subtotal, gst_total, grand_total, line_rows}

    GOVERNOR WIRED (2025):
    - Trusts ol.unit_price as set at punch time (RETAIL→mrp, WHOLESALE→selling_price)
    - Does NOT re-read inventory_stock — that was the source of price drift.
    - Normalizes BOX→PCS via governor.normalize_to_pcs_price()
    - GST computed via governor.compute_line_gst() for retail/wholesale consistency.
    """
    empty = {"subtotal": 0.0, "gst_total": 0.0, "grand_total": 0.0, "line_rows": []}
    if not order_ids:
        return empty

    rows = _q("""
        SELECT o.order_no, o.order_type,
               COALESCE(p.product_name, 'Service')           AS product_name,
               GREATEST(COALESCE(p.box_size, 1), 1)          AS box_size,
               COALESCE(p.unit, 'PCS')                       AS unit,
               COALESCE(ol.quantity, 0)                      AS quantity,
               ol.gst_percent,
               ol.unit_price,
               COALESCE(MAX(inv.selling_price), 0)           AS product_selling_price,
               COALESCE(MAX(inv.mrp), 0)                     AS product_mrp,
               COALESCE(ol.total_price, 0)                   AS stored_total,
               COALESCE(ol.is_service_line, FALSE)           AS is_service_line,
               COALESCE(ol.eye_side, '')                     AS eye_side
        FROM order_lines ol
        JOIN orders o        ON o.id  = ol.order_id
        LEFT JOIN products p ON p.id  = ol.product_id
        LEFT JOIN inventory_stock inv
               ON inv.product_id = ol.product_id
              AND COALESCE(inv.is_active, TRUE) = TRUE
        WHERE ol.order_id = ANY(%(ids)s::uuid[])
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        GROUP BY o.order_no, o.order_type,
                 p.product_name, p.box_size, p.unit,
                 ol.quantity, ol.gst_percent, ol.unit_price,
                 ol.total_price, ol.id,
                 ol.is_service_line, ol.eye_side
        ORDER BY o.order_no, ol.is_service_line, ol.id
    """, {"ids": order_ids})

    sub = gst = 0.0
    enriched_rows = []
    for r in rows:
        r = dict(r)
        order_type = str(r.get("order_type") or "WHOLESALE").upper()
        qty        = int(r.get("quantity") or 0)
        gst_pct    = float(r.get("gst_percent") or 0)

        # Governor: normalize BOX→PCS, trusting what was punched
        raw_up     = float(r.get("unit_price") or 0)
        unit_price_pcs = normalize_to_pcs_price(raw_up, r)

        # Governor: compute GST correctly per order_type
        gst_calc   = compute_line_gst(unit_price_pcs, qty, gst_pct, order_type)

        r["unit_price_pcs"] = unit_price_pcs
        r["line_subtotal"]  = gst_calc["subtotal"]
        r["gst_amount"]     = gst_calc["gst_amount"]
        r["grand_total"]    = gst_calc["grand_total"]

        # Price integrity flag: wholesale order but unit_price looks like MRP
        _sp  = float(r.get("product_selling_price") or 0)
        _mrp = float(r.get("product_mrp") or 0)
        _up  = float(r.get("unit_price") or 0)
        r["price_flag"] = (
            order_type == "WHOLESALE"
            and _sp <= 0            # no selling_price in product
            and _mrp > 0
            and abs(_up - _mrp) < 0.01   # unit_price = MRP → wrong
        )

        sub += gst_calc["gst_base"]
        gst += gst_calc["gst_amount"]
        enriched_rows.append(r)

    return {"subtotal": round(sub, 2), "gst_total": round(gst, 2),
            "grand_total": round(sub + gst, 2), "line_rows": enriched_rows}


def _fmt_currency(v) -> str:
    try:    return f"₹{float(v):,.2f}"
    except: return "₹0.00"


def _fmt_date(v) -> str:
    if not v: return "—"
    try:
        if isinstance(v, (date, datetime)):
            return v.strftime("%d %b %Y")
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except:
        return str(v)[:10]


def _fmt_qty(qty, box_size, unit) -> str:
    q = int(qty or 0)
    bs = int(box_size or 1)
    u = str(unit or "PCS").upper()
    if u == "BOX" and bs > 1:
        boxes, rem = divmod(q, bs)
        if rem == 0: return f"{boxes} BOX"
        return f"{boxes} BOX + {rem} PCS"
    return f"{q} PCS"


def _pill(status: str) -> str:
    s = str(status or "").upper()
    cls = {
        "PENDING":   "pill-pending",
        "INVOICED":  "pill-invoiced",
        "PAID":      "pill-paid",
        "UNPAID":    "pill-unpaid",
        "PARTIAL":   "pill-partial",
        "CANCELLED": "pill-cancelled",
        "OVERDUE":   "pill-overdue",
        "CONFIRMED": "pill-confirmed",
    }.get(s, "pill-pending")
    return f"<span class='pill {cls}'>{s}</span>"


def _inject_css():
    st.markdown(_CSS, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# CHALLAN PREVIEW
# ══════════════════════════════════════════════════════════════════════════


# ── Document number helpers — use central registry ────────────────────────────
def _alloc_inv_no() -> str:
    from modules.db.order_number_registry import alloc_doc_number
    return alloc_doc_number("INVOICE")

def _alloc_ch_no() -> str:
    from modules.db.order_number_registry import alloc_doc_number
    return alloc_doc_number("CHALLAN")
# ─────────────────────────────────────────────────────────────────────────────

def render_challan_preview(challan_no: str):
    _inject_css()

    challan_data = _q("""
        SELECT c.*,
               COALESCE(p.party_name,
                   (SELECT o2.party_name FROM orders o2
                    WHERE o2.id::text = ANY(c.order_ids) LIMIT 1), '') AS party_name,
               COALESCE(p.mobile, '')  AS mobile,
               COALESCE(p.address, '') AS address,
               COALESCE(p.gstin, '')   AS gstin,
               COALESCE(p.email, '')           AS email,
               COALESCE(p.contact_person, '')  AS contact_person,
               COALESCE(p.city, '')            AS city
        FROM challans c
        LEFT JOIN parties p ON p.id = c.party_id
        WHERE c.challan_no = %(n)s
    """, {"n": challan_no})

    if not challan_data:
        st.error(f"Challan {challan_no} not found")
        return

    ch = challan_data[0]

    lines = _q("""
        SELECT
            cl.id,
            cl.order_line_id,
            COALESCE(o.order_no, '')                    AS order_no,
            UPPER(COALESCE(o.order_type, 'WHOLESALE'))  AS order_type,
            o.created_at                                AS order_date,
            COALESCE(cl.product_name, pr.product_name, '') AS product_name,
            COALESCE(cl.brand, pr.brand, '')            AS brand,
            COALESCE(pr.box_size, 1)                    AS box_size,
            COALESCE(pr.unit, 'PCS')                    AS unit,
            COALESCE(cl.eye_side, ol.eye_side, '')      AS eye_side,
            ol.sph                                      AS sph,
            ol.cyl                                      AS cyl,
            ol.axis                                     AS axis,
            ol.add_power                                AS add_power,
            cl.quantity,
            cl.unit_price,
            cl.line_total,
            COALESCE(ol.gst_percent, cl.gst_percent, 0) AS gst_percent
        FROM challan_lines cl
        LEFT JOIN orders o      ON o.id  = cl.order_id
        LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
        LEFT JOIN products pr   ON pr.id = ol.product_id
        WHERE cl.challan_id = %(id)s
          AND COALESCE(cl.is_deleted, FALSE) = FALSE
        ORDER BY cl.id
    """, {"id": ch["id"]})

    # ── Header ────────────────────────────────────────────────────────────
    st.markdown(f"""
    <div class='doc-header'>
      <div style='display:flex;justify-content:space-between;align-items:flex-start'>
        <div>
          <div style='color:#475569;font-size:0.62rem;letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px'>Delivery Challan</div>
          <div class='doc-no'>{ch['challan_no']}</div>
          <div class='doc-meta'>Date: {_fmt_date(ch.get('challan_date'))} &nbsp;·&nbsp; Created by: {ch.get('created_by','—')}</div>
        </div>
        <div style='text-align:right'>
          {_pill(ch.get('status','PENDING'))}
          <div class='doc-meta' style='margin-top:6px'>Grand Total</div>
          <div style='font-family:"IBM Plex Mono",monospace;color:#10b981;font-size:1.3rem;font-weight:700'>{_fmt_currency(ch.get('grand_total',0))}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Party + Challan info ──────────────────────────────────────────────
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"""
        <div class='party-card'>
          <div class='party-name'>🏢 {ch['party_name']}</div>
          {'<div class="party-field"><span class="party-label">Mobile</span><span class="party-value">'+ch['mobile']+'</span></div>' if ch.get('mobile') else ''}
          {'<div class="party-field"><span class="party-label">GST No</span><span class="party-value">'+ch["gstin"]+'</span></div>' if ch.get("gstin") else ''}
          {'<div class="party-field"><span class="party-label">Address</span><span class="party-value">'+str(ch.get('address',''))[:60]+'</span></div>' if ch.get('address') else ''}
          {'<div class="party-field"><span class="party-label">Email</span><span class="party-value">'+ch['email']+'</span></div>' if ch.get('email') else ''}
        </div>
        """, unsafe_allow_html=True)

    with c2:
        remarks = ch.get('remarks','') or ''
        st.markdown(f"""
        <div class='party-card'>
          <div class='sec-head'>Challan Details</div>
          <div class='party-field'><span class='party-label'>Challan No</span><span class='party-value' style='font-family:"IBM Plex Mono",monospace;color:#38bdf8'>{ch['challan_no']}</span></div>
          <div class='party-field'><span class='party-label'>Date</span><span class='party-value'>{_fmt_date(ch.get('challan_date'))}</span></div>
          <div class='party-field'><span class='party-label'>Status</span><span class='party-value'>{ch.get('status','—')}</span></div>
          <div class='party-field'><span class='party-label'>Orders</span><span class='party-value'>{len(ch.get('order_ids') or [])}</span></div>
          {'<div class="party-field"><span class="party-label">Remarks</span><span class="party-value">'+remarks[:80]+'</span></div>' if remarks else ''}
        </div>
        """, unsafe_allow_html=True)

    # ── Service charges (fitting/colouring/courier) ─────────────────────
    svc_charges_display = _q("""
        SELECT charge_type, description, base_amount,
               gst_percent, gst_amount, total_amount
        FROM   challan_service_charges
        WHERE  challan_id = %(cid)s::uuid
        ORDER  BY charge_type, description
    """, {"cid": str(ch["id"])}) or []

    # ── Line items ────────────────────────────────────────────────────────
    st.markdown("<div class='sec-head'>Line Items</div>", unsafe_allow_html=True)

    if lines:
        import math as _math
        is_retail = (str(lines[0].get("order_type", "WHOLESALE")).upper() == "RETAIL")
        _ch_gstin = str(ch.get("gstin") or "")
        _ch_gst   = _gst_split(float(ch.get("total_tax") or 0), party_gstin=_ch_gstin)
        _ch_inter = (_ch_gst["supply_type"] == "INTER")
        _gst_col_hdr = "IGST" if _ch_inter else "CGST / SGST"

        def _rx(v):
            try:
                f = float(v)
                return f"{f:+.2f}" if not _math.isnan(f) else "—"
            except: return "—"

        def _desc_html(ln):
            """Tally-style: product name + eye tag + power string in one cell."""
            eye   = str(ln.get("eye_side") or "").upper()
            pname = str(ln.get("product_name") or "—")
            brand = str(ln.get("brand") or "")
            _ecol = {"R":"#3b82f6","L":"#10b981","B":"#a855f7","S":"#f59e0b"}.get(eye, "#64748b")
            _etxt = {"R":"👁 Right Eye","L":"👁 Left Eye","B":"👁👁 Both","S":"⚙️ Service"}.get(eye, eye)
            # Power string
            pw_parts = []
            if ln.get("sph") is not None: pw_parts.append(f"SPH {_rx(ln['sph'])}")
            if ln.get("cyl") is not None: pw_parts.append(f"CYL {_rx(ln['cyl'])}")
            if ln.get("axis"):
                try: pw_parts.append(f"AX {int(float(ln['axis']))}")
                except: pass
            if ln.get("add_power"):
                try:
                    av = float(ln["add_power"])
                    if abs(av) > 0.001: pw_parts.append(f"ADD {av:+.2f}")
                except: pass
            pw_str = "  ".join(pw_parts)
            out  = f"<b style='color:#e2e8f0'>{pname}</b>"
            if brand: out += f"<span style='color:#475569;font-size:0.63rem'> &nbsp;{brand}</span>"
            if eye and eye not in ("","O","OTHER"):
                out += f"<br><span style='background:{_ecol}22;color:{_ecol};font-size:0.63rem;padding:1px 6px;border-radius:3px;font-weight:700'>{_etxt}</span>"
            if pw_str:
                out += f"<br><span style='font-family:monospace;font-size:0.65rem;color:#94a3b8'>{pw_str}</span>"
            return out

        from itertools import groupby as _grp
        rows_html = ""
        sno = 0
        for order_no, grp_lines in _grp(lines, key=lambda x: x.get("order_no", "—")):
            rows_html += (
                f"<tr><td colspan='7' style='background:#0f172a;color:#38bdf8;"
                f"font-size:0.68rem;font-family:monospace;padding:4px 12px;"
                f"letter-spacing:.06em;border-left:3px solid #38bdf8'>📦 {order_no}</td></tr>"
            )
            for ln in grp_lines:
                sno += 1
                qty_d = _fmt_qty(ln.get("quantity"), ln.get("box_size"), ln.get("unit"))
                up    = float(ln.get("unit_price") or 0)
                lt    = float(ln.get("line_total") or 0)
                gp    = float(ln.get("gst_percent") or 0)
                # Per-line tax
                _ln_tax = float(lt * gp / (100 + gp)) if is_retail else float(lt * gp / 100)
                _ln_gst = _gst_split(_ln_tax, party_gstin=_ch_gstin)
                if _ch_inter:
                    _tax_disp = f"<span style='color:#f59e0b'>{_fmt_currency(_ln_gst['igst'])}</span>"
                else:
                    _tax_disp = (
                        f"<span style='color:#f59e0b;font-size:0.68rem'>"
                        f"C:{_fmt_currency(_ln_gst['cgst'])}<br>"
                        f"S:{_fmt_currency(_ln_gst['sgst'])}</span>"
                    )
                _ln_otype = str(ln.get("order_type","WHOLESALE")).upper()
                _ln_sp  = float(ln.get("product_selling_price") or ln.get("selling_price") or 0)
                _ln_mrp = float(ln.get("product_mrp") or ln.get("mrp") or 0)
                _price_warn = (_ln_otype=="WHOLESALE" and _ln_sp<=0 and _ln_mrp>0 and abs(up-_ln_mrp)<0.01)
                rate_lbl = ("⚠️ " if _price_warn else "") + _fmt_currency(up)
                rows_html += (
                    f"<tr>"
                    f"<td style='text-align:center;color:#475569;font-size:0.7rem'>{sno}</td>"
                    f"<td>{_desc_html(ln)}</td>"
                    f"<td class='num'>{qty_d}</td>"
                    f"<td class='num'>{rate_lbl}</td>"
                    f"<td class='num'>{_fmt_currency(lt)}</td>"
                    f"<td class='num' style='font-size:0.68rem'>{gp:.0f}%</td>"
                    f"<td class='num'>{_tax_disp}</td>"
                    f"</tr>"
                )

        # Service charge rows
        svc_rows_html = ""
        for sc in svc_charges_display:
            sno += 1
            _svc_ico  = {"FITTING":"🔧","COLOURING":"🎨","COURIER":"📦","CONSULTATION":"👁️"}.get(
                (sc.get("charge_type") or "").upper(), "➕")
            _svc_name = sc.get("description") or sc.get("charge_type") or "Service"
            _svc_gst  = float(sc.get("gst_percent") or 0)
            _svc_base = float(sc.get("base_amount") or 0)
            _svc_tot  = float(sc.get("total_amount") or 0)
            _svc_tax  = float(sc.get("gst_amount") or 0)
            _svc_gst_split = _gst_split(_svc_tax, party_gstin=_ch_gstin)
            _svc_tax_disp = (
                f"<span style='color:#f59e0b'>{_fmt_currency(_svc_gst_split['igst'])}</span>"
                if _ch_inter else
                f"<span style='color:#f59e0b;font-size:0.68rem'>"
                f"C:{_fmt_currency(_svc_gst_split['cgst'])}<br>"
                f"S:{_fmt_currency(_svc_gst_split['sgst'])}</span>"
            )
            svc_rows_html += (
                f"<tr style='background:#0a0f1a'>"
                f"<td style='text-align:center;color:#475569;font-size:0.7rem'>{sno}</td>"
                f"<td><b style='color:#c4b5fd'>{_svc_ico} {_svc_name}</b>"
                f"<br><span style='color:#475569;font-size:0.63rem'>Service Charge</span></td>"
                f"<td class='num' style='color:#a78bfa'>1</td>"
                f"<td class='num' style='color:#a78bfa'>{_fmt_currency(_svc_base)}</td>"
                f"<td class='num' style='color:#a78bfa;font-weight:600'>{_fmt_currency(_svc_tot)}</td>"
                f"<td class='num' style='color:#a78bfa;font-size:0.68rem'>{_svc_gst:.0f}%</td>"
                f"<td class='num'>{_svc_tax_disp}</td>"
                f"</tr>"
            )

        # Totals
        line_total_sum = sum(float(ln.get("line_total") or 0) for ln in lines)

        # Compute tax from per-line gst_percent (don't trust ch.total_tax which may be stale)
        def _ln_tax_from_line(ln):
            lt  = float(ln.get("line_total") or 0)
            gp  = float(ln.get("gst_percent") or 0)
            if gp <= 0: return 0.0
            if is_retail:
                return round(lt * gp / (100 + gp), 2)
            else:
                return round(lt * gp / 100, 2)

        tax_computed = round(sum(_ln_tax_from_line(ln) for ln in lines), 2)
        # Add service charge GST
        tax_computed += round(sum(float(sc.get("gst_amount") or 0) for sc in svc_charges_display), 2)

        if is_retail:
            gnd = float(ch.get("grand_total") or line_total_sum)
            # For retail, line_total is MRP-inclusive; back out tax
            sub = round(gnd - tax_computed, 2)
            tax = tax_computed
        else:
            sub = round(line_total_sum - tax_computed, 2)
            tax = tax_computed
            gnd = float(ch.get("grand_total") or round(sub + tax, 2))

        if _ch_inter:
            _tax_footer = (
                f"<tr><td colspan='4'></td>"
                f"<td style='text-align:right;color:#f59e0b'>IGST</td>"
                f"<td colspan='2' class='num' style='color:#f59e0b'>{_fmt_currency(tax)}</td></tr>"
            )
        else:
            _half = round(tax / 2, 2)
            _tax_footer = (
                f"<tr><td colspan='4'></td>"
                f"<td style='text-align:right;color:#f59e0b'>CGST</td>"
                f"<td colspan='2' class='num' style='color:#f59e0b'>{_fmt_currency(_half)}</td></tr>"
                f"<tr><td colspan='4'></td>"
                f"<td style='text-align:right;color:#f59e0b'>SGST</td>"
                f"<td colspan='2' class='num' style='color:#f59e0b'>{_fmt_currency(round(tax-_half,2))}</td></tr>"
            )

        _supply_badge_col = "#f59e0b" if _ch_inter else "#10b981"
        _supply_text = f"Inter-state · IGST" if _ch_inter else f"Intra-state {OUR_STATE_NAME} · CGST+SGST"
        _gstin_note  = f" · GSTIN: {_ch_gstin}" if _ch_gstin else " · Unregistered / Retail"

        st.markdown(f"""
        <div style='margin-bottom:6px;display:flex;align-items:center;gap:10px'>
          <span style='background:{_supply_badge_col}22;color:{_supply_badge_col};
                        font-size:0.65rem;font-weight:700;padding:3px 10px;
                        border-radius:10px;letter-spacing:.05em'>
            {"🌐 INTER-STATE (IGST)" if _ch_inter else f"🏠 INTRA-STATE (CGST+SGST)"}
          </span>
          <span style='color:#475569;font-size:0.65rem'>{_supply_text}{_gstin_note}</span>
        </div>
        <table class='line-table'>
          <thead><tr>
            <th style='width:30px'>#</th>
            <th>Description</th>
            <th style='text-align:right'>Qty</th>
            <th style='text-align:right'>{"MRP/Unit" if is_retail else "Rate"}</th>
            <th style='text-align:right'>{"MRP Total" if is_retail else "Amount"}</th>
            <th style='text-align:right'>GST%</th>
            <th style='text-align:right'>{_gst_col_hdr}</th>
          </tr></thead>
          <tbody>{rows_html}{svc_rows_html}</tbody>
          <tfoot>
            <tr><td colspan='3'></td><td style='text-align:right;color:#94a3b8'>{"Base (excl. GST)" if is_retail else "Subtotal"}</td>
                <td class='num'>{_fmt_currency(sub)}</td><td></td><td></td></tr>
            {_tax_footer}
            <tr><td colspan='3'></td>
                <td style='text-align:right;color:#38bdf8;font-weight:700'>Grand Total</td>
                <td class='num grand'>{_fmt_currency(gnd)}</td>
                <td colspan='2'></td></tr>
          </tfoot>
        </table>
        """, unsafe_allow_html=True)
    else:
        st.info("No line items found for this challan")

    # ── Remarks ───────────────────────────────────────────────────────────
    if ch.get("remarks"):
        st.markdown(f"""
        <div style='background:#1e293b;border-left:3px solid #3b82f6;
                    border-radius:0 8px 8px 0;padding:10px 14px;margin-top:12px;
                    color:#94a3b8;font-size:0.78rem'>
          📝 {ch['remarks']}
        </div>""", unsafe_allow_html=True)

    # ── Smart Print ──────────────────────────────────────────────────────
    st.markdown("---")
    _sp_key = f"_show_ch_print_{ch['id']}"
    if st.button("🖨️ Smart Print / PDF", key=f"chprint_{ch['id']}",
                 type="secondary", use_container_width=True,
                 help="A4 challan with CGST/SGST or IGST split, lens powers, signature"):
        st.session_state[_sp_key] = not st.session_state.get(_sp_key, False)
    if st.session_state.get(_sp_key):
        try:
            from modules.billing.smart_print import render_smart_challan
            render_smart_challan(ch["challan_no"])
        except Exception as _spe:
            import traceback
            st.error(f"Print error: {_spe}")
            with st.expander("Traceback"): st.code(traceback.format_exc())

    # ── Create Invoice from this challan ─────────────────────────────────
    if ch.get("status") == "PENDING":
        st.markdown("---")
        _inv_exists = _q(
            "SELECT invoice_no FROM invoices WHERE challan_id=%(cid)s AND COALESCE(is_deleted,FALSE)=FALSE LIMIT 1",
            {"cid": str(ch["id"])}
        )
        if _inv_exists:
            st.info(f"📄 Invoice already raised: **{_inv_exists[0]['invoice_no']}**")
        else:
            _ci1, _ci2, _ci3 = st.columns([3, 1, 1])
            with _ci1:
                st.markdown(
                    f"<div style='background:#0d1f0d;border:1px solid #10b98144;border-radius:8px;"
                    f"padding:10px 14px;color:#94a3b8;font-size:0.82rem'>"
                    f"💡 Challan <b style='color:#38bdf8'>{ch['challan_no']}</b> is pending — "
                    f"raise invoice directly from here</div>",
                    unsafe_allow_html=True)

            with _ci3:
                try:
                    from modules.security.roles import has_role as _hr_cp
                    _can_recall_cp = _hr_cp("admin", "manager")
                except Exception:
                    _can_recall_cp = False
                if _can_recall_cp:
                    _recall_cp_key = f"_recall_cp_{ch['id']}"
                    if not st.session_state.get(_recall_cp_key):
                        if st.button("↩️ Recall", key=f"recall_cp_btn_{ch['id']}",
                                     use_container_width=True,
                                     help="Void this challan and recall order to CONFIRMED"):
                            st.session_state[_recall_cp_key] = True
                            st.rerun()
                    else:
                        st.warning(f"Void challan **{ch['challan_no']}** and recall order to CONFIRMED?")
                        _rcp1, _rcp2 = st.columns(2)
                        with _rcp1:
                            if st.button("✅ Yes", key=f"recall_cp_yes_{ch['id']}",
                                         type="primary", use_container_width=True):
                                try:
                                    from modules.sql_adapter import run_write as _rw_cp, run_query as _rq_cp
                                    _rw_cp("UPDATE challans SET status='VOID', updated_at=NOW() WHERE id=%(id)s::uuid",
                                           {"id": str(ch["id"])})
                                    _chl_rows = _rq_cp("""
                                        SELECT order_line_id::text, quantity
                                        FROM challan_lines WHERE challan_id=%(cid)s::uuid
                                          AND NOT COALESCE(is_deleted,FALSE)
                                    """, {"cid": str(ch["id"])}) or []
                                    for _cl in _chl_rows:
                                        _rw_cp("""UPDATE order_lines
                                            SET billed_qty=GREATEST(0,COALESCE(billed_qty,0)-%(qty)s)
                                            WHERE id=%(lid)s::uuid""",
                                            {"qty": int(_cl.get("quantity") or 0),
                                             "lid": str(_cl.get("order_line_id") or "")})
                                    for _oid in (ch.get("order_ids") or []):
                                        import re as _re_uid
                                        if _re_uid.match(r'^[0-9a-f-]{36}$', str(_oid), _re_uid.I):
                                            _rw_cp("UPDATE orders SET status='CONFIRMED', updated_at=NOW() WHERE id=%(id)s::uuid AND status NOT IN ('BILLED','DISPATCHED','DELIVERED','CLOSED')", {"id": _oid})
                                    st.success(f"✅ Challan {ch['challan_no']} voided. Order recalled to CONFIRMED.")
                                    st.session_state.pop(_recall_cp_key, None)
                                    st.rerun()
                                except Exception as _rce:
                                    st.error(f"Recall failed: {_rce}")
                        with _rcp2:
                            if st.button("← Cancel", key=f"recall_cp_no_{ch['id']}",
                                         use_container_width=True):
                                st.session_state.pop(_recall_cp_key, None)
                                st.rerun()

            with _ci2:
                # Retail rule: invoice only after full payment
                _ch_grand    = float(ch.get("grand_total") or 0)
                _ch_pmt_done = bool(ch.get("payment_complete"))
                _ch_otype    = str(lines[0].get("order_type","WHOLESALE")).upper() if lines else "WHOLESALE"
                _retail_pmt_ok = (
                    _ch_otype != "RETAIL"
                    or _ch_pmt_done
                    or _ch_grand == 0
                )
                if _retail_pmt_ok:
                    if st.button("🧾 Create Invoice", type="primary",
                                 key=f"mk_inv_{ch['challan_no']}",
                                 use_container_width=True):
                        # Create invoice directly — no redirect
                        try:
                            import uuid as _u_inv
                            from datetime import date as _dt_inv, timedelta as _td_inv
                            inv_no = _alloc_inv_no()
                            _inv_sub = float(ch.get("total_amount") or 0)
                            _inv_tax = float(ch.get("total_tax")    or 0)
                            _inv_gnd = float(ch.get("grand_total")  or 0)
                            if _inv_gnd == 0:
                                _lt = _q("SELECT COALESCE(SUM(line_total),0) AS t FROM challan_lines WHERE challan_id=%(cid)s::uuid", {"cid": str(ch["id"])})
                                _inv_gnd = float((_lt[0].get("t") if _lt else 0) or 0)
                                _inv_sub = _inv_gnd
                            _inv_party = str(ch.get("party_id") or "")
                            _inv_oids  = ch.get("order_ids") or []
                            ok = _write("""
                                INSERT INTO invoices
                                (id, invoice_no, challan_id, party_id, order_ids,
                                 invoice_date, due_date, total_amount, total_tax, grand_total,
                                 status, payment_status, created_by)
                                VALUES
                                (%(id)s, %(no)s, %(cid)s::uuid,
                                 %(pid)s::uuid, %(oids)s,
                                 %(idate)s, %(ddate)s,
                                 %(sub)s, %(tax)s, %(gnd)s,
                                 'PENDING','UNPAID', %(by)s)
                            """, {
                                "id":    str(_u_inv.uuid4()),
                                "no":    inv_no,
                                "cid":   str(ch["id"]),
                                "pid":   _inv_party or None,
                                "oids":  _inv_oids,
                                "idate": _dt_inv.today(),
                                "ddate": _dt_inv.today() + _td_inv(days=0),
                                "sub":   _inv_sub,
                                "tax":   _inv_tax,
                                "gnd":   _inv_gnd,
                                "by":    st.session_state.get("user_name","System"),
                            })
                            if ok:
                                _write("UPDATE challans SET status='INVOICED', updated_at=NOW() WHERE id=%(i)s", {"i": str(ch["id"])})
                                st.success(f"✅ Invoice **{inv_no}** created!")
                                st.rerun()
                            else:
                                st.error("Invoice insert failed — check logs")
                        except Exception as _inv_e:
                            st.error(f"Invoice error: {_inv_e}")
                else:
                    _ch_paid = float(ch.get("amount_paid") or 0)
                    _ch_bal  = round(max(_ch_grand - _ch_paid, 0), 2)
                    st.button("🔒 Invoice (Collect ₹{:.0f} first)".format(_ch_bal),
                              disabled=True, key=f"mk_inv_dis_{ch['challan_no']}",
                              use_container_width=True,
                              help="Retail orders require full payment before invoice")
                    st.caption("⚠️ Collect balance payment in Payment panel below")

    # ── Payment panel ──────────────────────────────────────────────────────
    st.markdown("---")
    # Detect order type from lines
    _is_retail_ch = (lines and lines[0].get("order_type","WHOLESALE") == "RETAIL")
    try:
        from modules.billing.payment_manager import (
            render_retail_payment_panel, render_wholesale_payment_panel)
        _ch_party_id   = str(ch["party_id"]) if ch.get("party_id") else None
        _ch_party_name = str(ch.get("party_name") or "")
        _ch_pmode      = str(ch.get("payment_mode") or
                             ("ADVANCE_BALANCE" if _is_retail_ch else "ON_COMPLETION"))
        if _is_retail_ch or _ch_pmode == "ADVANCE_BALANCE":
            st.markdown("#### 🛍️ Retail Payment Tracker")
            render_retail_payment_panel(
                challan_id  = str(ch["id"]),
                challan_no  = ch["challan_no"],
                party_id    = _ch_party_id,
                party_name  = _ch_party_name,
            )
        else:
            # Wholesale challan — show outstanding only if not yet invoiced
            if ch.get("status") != "INVOICED":
                st.markdown("#### 💰 Payment Summary")
                _inv_for_ch = _q(
                    "SELECT id, invoice_no, payment_status, grand_total, amount_paid, balance_due "
                    "FROM invoices WHERE challan_id=%(cid)s AND COALESCE(is_deleted,FALSE)=FALSE",
                    {"cid": str(ch["id"])}
                )
                if _inv_for_ch:
                    for _inv in _inv_for_ch:
                        render_wholesale_payment_panel(
                            invoice_id   = str(_inv["id"]),
                            invoice_no   = _inv["invoice_no"],
                            party_id     = _ch_party_id,
                            party_name   = _ch_party_name,
                            payment_mode = _ch_pmode,
                        )
                else:
                    st.info("Invoice not yet raised for this challan.")
    except Exception as _pe:
        st.caption(f"Payment panel: {_pe}")


# ══════════════════════════════════════════════════════════════════════════
# INVOICE PREVIEW
# ══════════════════════════════════════════════════════════════════════════

def render_invoice_preview(invoice_no: str):
    _inject_css()

    inv_data = _q("""
        SELECT i.*,
               COALESCE(p.party_name,
                   (SELECT o2.party_name FROM orders o2
                    WHERE o2.id::text = ANY(i.order_ids) LIMIT 1),
                   'Unknown') AS party_name,
               COALESCE(p.mobile, '')          AS mobile,
               COALESCE(p.address, '')         AS address,
               COALESCE(p.gstin, '')           AS gstin,
               COALESCE(p.email, '')           AS email,
               COALESCE(p.contact_person, '')  AS contact_person,
               COALESCE(p.city, '')            AS city,
               c.challan_no
        FROM invoices i
        LEFT JOIN parties p ON p.id = i.party_id
        LEFT JOIN challans c ON c.id = i.challan_id
        WHERE i.invoice_no = %(n)s
    """, {"n": invoice_no})

    if not inv_data:
        st.error(f"Invoice {invoice_no} not found")
        return

    inv = inv_data[0]

    # Pre-compute GSTIN for GST split (needed in line loop below)
    _gstin = str(inv.get("gstin") or "")

    # Try invoice_lines first; fallback to challan_lines if empty
    lines = _q("""
        SELECT
            il.id,
            COALESCE(
                o_uuid.order_no,
                o_ono.order_no,
                il.order_id::text
            ) AS order_no,
            COALESCE(o_uuid.created_at, o_ono.created_at) AS order_date,
            COALESCE(il.product_name, pr.product_name) AS product_name,
            COALESCE(il.brand, pr.brand)               AS brand,
            COALESCE(pr.box_size, 1)                   AS box_size,
            COALESCE(pr.unit, 'PCS')                   AS unit,
            COALESCE(ol.eye_side, il.eye_side)         AS eye_side,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            il.quantity,
            il.unit_price,
            il.total_price,
            COALESCE(il.tax_amount, 0)                 AS tax_amount,
            COALESCE(ol.gst_percent, il.gst_percent, 0) AS gst_percent
        FROM invoice_lines il
        -- Try UUID match first
        LEFT JOIN orders o_uuid ON o_uuid.id = il.order_id
        -- Fallback: order_id stored as order_no text (legacy data)
        LEFT JOIN orders o_ono  ON o_ono.order_no = il.order_id::text
                                AND o_uuid.id IS NULL
        LEFT JOIN order_lines ol   ON ol.id = il.order_line_id
        LEFT JOIN products pr      ON pr.id = ol.product_id
        WHERE il.invoice_id = %(id)s
          AND NOT COALESCE(il.is_deleted, FALSE)
        ORDER BY COALESCE(o_uuid.order_no, o_ono.order_no, ''), il.id
    """, {"id": str(inv["id"])})

    # If no invoice_lines found, try reading from challan_lines
    # Also resolve challan_id from order_ids if not set directly on invoice
    _challan_id = inv.get("challan_id")
    if not _challan_id and inv.get("order_ids"):
        # Try to find challan via order_ids
        _oids = inv.get("order_ids") or []
        if _oids:
            _ch_rows = _q("""
                SELECT id::text FROM challans c
                WHERE NOT COALESCE(c.is_deleted, FALSE)
                  AND (
                    %(oid)s = ANY(c.order_ids)
                  )
                ORDER BY c.created_at DESC LIMIT 1
            """, {"oid": str(_oids[0]) if _oids else ""})
            if _ch_rows:
                _challan_id = _ch_rows[0]["id"]

    if not lines and _challan_id:
        lines = _q("""
            SELECT
                cl.id,
                COALESCE(o.order_no, cl.order_id::text) AS order_no,
                o.created_at AS order_date,
                cl.product_name,
                cl.brand,
                1 AS box_size, 'PCS' AS unit,
                COALESCE(cl.eye_side, ol.eye_side) AS eye_side,
                ol.sph, ol.cyl, ol.axis, ol.add_power,
                cl.quantity,
                cl.unit_price,
                cl.total_price,
                COALESCE(
                    cl.total_price * COALESCE(ol.gst_percent,0) / 100,
                    0
                ) AS tax_amount
            FROM challan_lines cl
            LEFT JOIN orders o       ON o.id  = cl.order_id
            LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
            WHERE cl.challan_id = %(cid)s
              AND NOT COALESCE(cl.is_deleted, FALSE)
            ORDER BY COALESCE(o.order_no,''), cl.id
        """, {"cid": str(_challan_id)})

    # Payment status color
    pstatus = str(inv.get("payment_status") or "UNPAID").upper()
    pcolor  = {"PAID": "#10b981", "PARTIAL": "#f59e0b", "UNPAID": "#ef4444"}.get(pstatus, "#ef4444")

    # Overdue check
    due = inv.get("due_date")
    is_overdue = False
    if due and pstatus != "PAID":
        try:
            due_d = due if isinstance(due, date) else datetime.strptime(str(due)[:10], "%Y-%m-%d").date()
            is_overdue = due_d < date.today()
        except: pass

    # ── Header ────────────────────────────────────────────────────────────
    st.markdown(f"""
    <div class='doc-header'>
      <div style='display:flex;justify-content:space-between;align-items:flex-start'>
        <div>
          <div style='color:#475569;font-size:0.62rem;letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px'>Tax Invoice</div>
          <div class='doc-no'>{inv['invoice_no']}</div>
          <div class='doc-meta'>
            Date: {_fmt_date(inv.get('invoice_date'))} &nbsp;·&nbsp;
            Due: <span style='color:{"#ef4444" if is_overdue else "#94a3b8"}'>{_fmt_date(inv.get('due_date'))}</span>
            {'&nbsp;⚠️ OVERDUE' if is_overdue else ''}
            {f"&nbsp;·&nbsp; Challan: <span style='color:#38bdf8'>{inv['challan_no']}</span>" if inv.get('challan_no') else ''}
          </div>
        </div>
        <div style='text-align:right'>
          <div style='margin-bottom:6px'>{_pill(inv.get('status','PENDING'))} &nbsp; <span style='color:{pcolor};font-size:0.7rem;font-weight:700'>{pstatus}</span></div>
          <div class='doc-meta'>Grand Total</div>
          <div style='font-family:"IBM Plex Mono",monospace;color:#10b981;font-size:1.3rem;font-weight:700'>{_fmt_currency(inv.get('grand_total',0))}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Party + Invoice details ───────────────────────────────────────────
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"""
        <div class='party-card'>
          <div class='party-name'>🏢 {inv['party_name']}</div>
          {'<div class="party-field"><span class="party-label">Mobile</span><span class="party-value">'+inv['mobile']+'</span></div>' if inv.get('mobile') else ''}
          {'<div class="party-field"><span class="party-label">GST No</span><span class="party-value">'+inv["gstin"]+'</span></div>' if inv.get("gstin") else ''}
          {'<div class="party-field"><span class="party-label">Address</span><span class="party-value">'+str(inv.get('address',''))[:60]+'</span></div>' if inv.get('address') else ''}
        </div>
        """, unsafe_allow_html=True)

    with c2:
        st.markdown(f"""
        <div class='party-card'>
          <div class='sec-head'>Invoice Details</div>
          <div class='party-field'><span class='party-label'>Invoice No</span><span class='party-value' style='font-family:"IBM Plex Mono",monospace;color:#38bdf8'>{inv['invoice_no']}</span></div>
          <div class='party-field'><span class='party-label'>Date</span><span class='party-value'>{_fmt_date(inv.get('invoice_date'))}</span></div>
          <div class='party-field'><span class='party-label'>Due Date</span><span class='party-value' style='color:{"#ef4444" if is_overdue else "#94a3b8"}'>{_fmt_date(inv.get('due_date'))}</span></div>
          <div class='party-field'><span class='party-label'>Payment</span><span class='party-value' style='color:{pcolor};font-weight:600'>{pstatus}</span></div>
          <div class='party-field'><span class='party-label'>Created by</span><span class='party-value'>{inv.get('created_by','—')}</span></div>
        </div>
        """, unsafe_allow_html=True)

    # ── Service charges (from challan) ──────────────────────────────────
    _svc_challan_id = str(inv.get("challan_id") or "")
    if _svc_challan_id:
        svc_charges_display = _q("""
            SELECT charge_type, description, base_amount,
                   gst_percent, gst_amount, total_amount
            FROM   challan_service_charges
            WHERE  challan_id = %(cid)s::uuid
            ORDER  BY charge_type, description
        """, {"cid": _svc_challan_id}) or []
    else:
        svc_charges_display = []

    # ── Line items ────────────────────────────────────────────────────────
    st.markdown("<div class='sec-head'>Line Items</div>", unsafe_allow_html=True)

    # Compute GST split from invoice totals — needed before and inside the lines block
    _inv_tax = float(inv.get("total_tax") or 0)
    _gst = _gst_split(_inv_tax, party_gstin=_gstin)

    if lines:
        import math as _math_inv
        _is_inter  = (_gst["supply_type"] == "INTER")
        _gst_col_hdr = "IGST" if _is_inter else "CGST / SGST"

        def _rx_inv(v):
            try:
                f = float(v)
                return f"{f:+.2f}" if not _math_inv.isnan(f) else "—"
            except: return "—"

        def _desc_inv(ln):
            """Tally-style description: name + eye tag + power string."""
            eye   = str(ln.get("eye_side") or "").upper()
            pname = str(ln.get("product_name") or "—")
            brand = str(ln.get("brand") or "")
            _ecol = {"R":"#3b82f6","L":"#10b981","B":"#a855f7","S":"#f59e0b"}.get(eye,"#64748b")
            _etxt = {"R":"👁 Right Eye","L":"👁 Left Eye","B":"👁👁 Both","S":"⚙️ Service"}.get(eye, eye)
            pw_parts = []
            if ln.get("sph") is not None: pw_parts.append(f"SPH {_rx_inv(ln['sph'])}")
            if ln.get("cyl") is not None: pw_parts.append(f"CYL {_rx_inv(ln['cyl'])}")
            if ln.get("axis"):
                try: pw_parts.append(f"AX {int(float(ln['axis']))}")
                except: pass
            if ln.get("add_power"):
                try:
                    av = float(ln["add_power"])
                    if abs(av) > 0.001: pw_parts.append(f"ADD {av:+.2f}")
                except: pass
            pw_str = "  ".join(pw_parts)
            out = f"<b style='color:#e2e8f0'>{pname}</b>"
            if brand: out += f"<span style='color:#475569;font-size:0.63rem'> &nbsp;{brand}</span>"
            if eye and eye not in ("","O","OTHER"):
                out += (f"<br><span style='background:{_ecol}22;color:{_ecol};"
                        f"font-size:0.63rem;padding:1px 6px;border-radius:3px;font-weight:700'>{_etxt}</span>")
            if pw_str:
                out += f"<br><span style='font-family:monospace;font-size:0.65rem;color:#94a3b8'>{pw_str}</span>"
            return out

        from itertools import groupby as _grpinv
        rows_html = ""
        sno_inv   = 0
        for order_no, grp_lines in _grpinv(lines, key=lambda x: x.get("order_no","—")):
            rows_html += (
                f"<tr><td colspan='8' style='background:#0f172a;color:#38bdf8;"
                f"font-size:0.68rem;font-family:monospace;padding:4px 12px;"
                f"letter-spacing:.06em;border-left:3px solid #38bdf8'>📦 {order_no}</td></tr>"
            )
            for ln in grp_lines:
                sno_inv += 1
                qty_d = _fmt_qty(ln.get("quantity"), ln.get("box_size"), ln.get("unit"))
                tax_a = float(ln.get("tax_amount") or 0)
                _lg   = _gst_split(tax_a, party_gstin=_gstin)
                if _is_inter:
                    _td = (f"<span style='color:#f59e0b'>"
                           f"{_fmt_currency(_lg['igst'])}</span>")
                else:
                    _td = (f"<span style='color:#f59e0b;font-size:0.68rem'>"
                           f"C:{_fmt_currency(_lg['cgst'])}<br>"
                           f"S:{_fmt_currency(_lg['sgst'])}</span>")
                rows_html += (
                    f"<tr>"
                    f"<td style='text-align:center;color:#475569;font-size:0.7rem'>{sno_inv}</td>"
                    f"<td>{_desc_inv(ln)}</td>"
                    f"<td class='num'>{qty_d}</td>"
                    f"<td class='num'>{_fmt_currency(ln.get('unit_price',0))}</td>"
                    f"<td class='num'>{_fmt_currency(ln.get('total_price',0))}</td>"
                    f"<td class='num' style='font-size:0.68rem'>"
                    f"{float(ln.get('gst_percent') or 0):.0f}%</td>"
                    f"<td class='num'>{_td}</td>"
                    f"</tr>"
                )

        # Service charges
        svc_rows_html = ""
        try:
            from modules.backoffice.order_charges_panel import fetch_charges as _fc_inv
            _inv_order_ids = inv.get("order_ids") or []
            _all_svc = []
            for _oid_inv in _inv_order_ids:
                _all_svc.extend(_fc_inv(str(_oid_inv)) or [])
            for _sc in _all_svc:
                sno_inv += 1
                _ico = {"FITTING":"🔧","COLOURING":"🎨","COURIER":"📦"}.get(_sc.get("charge_type",""),"➕")
                _sc_tax = float(_sc.get("gst_amount") or 0)
                _sc_gp  = float(_sc.get("gst_percent") or 0)
                _sc_gst = _gst_split(_sc_tax, party_gstin=_gstin)
                _sc_td  = (
                    f"<span style='color:#f59e0b'>{_fmt_currency(_sc_gst['igst'])}</span>"
                    if _is_inter else
                    f"<span style='color:#f59e0b;font-size:0.68rem'>"
                    f"C:{_fmt_currency(_sc_gst['cgst'])}<br>"
                    f"S:{_fmt_currency(_sc_gst['sgst'])}</span>"
                )
                svc_rows_html += (
                    f"<tr style='background:#0d1f0d'>"
                    f"<td style='text-align:center;color:#475569;font-size:0.7rem'>{sno_inv}</td>"
                    f"<td><b style='color:#a78bfa'>{_ico} "
                    f"{_sc.get('description') or _sc.get('charge_type','')}</b>"
                    f"<br><span style='color:#475569;font-size:0.63rem'>Service Charge</span></td>"
                    f"<td class='num'>1</td>"
                    f"<td class='num'>{_fmt_currency(_sc.get('amount',0))}</td>"
                    f"<td class='num'>{_fmt_currency(_sc.get('amount',0))}</td>"
                    f"<td class='num' style='font-size:0.68rem'>{_sc_gp:.0f}%</td>"
                    f"<td class='num'>{_sc_td}</td>"
                    f"</tr>"
                )
        except Exception:
            pass

        sub = float(inv.get("total_amount") or 0)
        tax = float(inv.get("total_tax")    or 0)
        gnd = float(inv.get("grand_total")  or 0)

        if _is_inter:
            _tax_rows = (
                f"<tr><td colspan='3'></td>"
                f"<td style='text-align:right;color:#f59e0b'>IGST</td>"
                f"<td colspan='3' class='num' style='color:#f59e0b'>{_fmt_currency(_gst['igst'])}</td></tr>"
            )
        else:
            _tax_rows = (
                f"<tr><td colspan='3'></td>"
                f"<td style='text-align:right;color:#f59e0b'>CGST</td>"
                f"<td colspan='3' class='num' style='color:#f59e0b'>{_fmt_currency(_gst['cgst'])}</td></tr>"
                f"<tr><td colspan='3'></td>"
                f"<td style='text-align:right;color:#f59e0b'>SGST</td>"
                f"<td colspan='3' class='num' style='color:#f59e0b'>{_fmt_currency(_gst['sgst'])}</td></tr>"
            )

        _supply_badge_col = "#f59e0b" if _is_inter else "#10b981"
        _gstin_note = (f" · GSTIN: {_gstin}" if _gstin
                       else (" · B2B (GSTIN not recorded)" if inv.get("party_id")
                             else " · Unregistered / Retail"))

        st.markdown(f"""
        <div style='margin-bottom:6px;display:flex;align-items:center;gap:10px'>
          <span style='background:{_supply_badge_col}22;color:{_supply_badge_col};
                        font-size:0.65rem;font-weight:700;padding:3px 10px;
                        border-radius:10px;letter-spacing:.05em'>
            {"🌐 INTER-STATE (IGST)" if _is_inter else f"🏠 INTRA-STATE (CGST+SGST)"}
          </span>
          <span style='color:#475569;font-size:0.65rem'>
            {"Inter-state · IGST" if _is_inter else f"Intra-state {OUR_STATE_NAME} · CGST+SGST"}
            {_gstin_note}
          </span>
        </div>
        <table class='line-table'>
          <thead><tr>
            <th style='width:30px'>#</th>
            <th>Description</th>
            <th style='text-align:right'>Qty</th>
            <th style='text-align:right'>Rate</th>
            <th style='text-align:right'>Base Amt</th>
            <th style='text-align:right'>GST%</th>
            <th style='text-align:right'>{_gst_col_hdr}</th>
          </tr></thead>
          <tbody>{rows_html}{svc_rows_html}</tbody>
          <tfoot>
            <tr><td colspan='3'></td>
                <td style='text-align:right;color:#94a3b8'>Subtotal</td>
                <td colspan='3' class='num'>{_fmt_currency(sub)}</td></tr>
            {_tax_rows}
            <tr><td colspan='3'></td>
                <td style='text-align:right;color:#38bdf8;font-weight:700'>Grand Total</td>
                <td colspan='3' class='num grand'>{_fmt_currency(gnd)}</td></tr>
          </tfoot>
        </table>
        """, unsafe_allow_html=True)
    else:
        st.info("No line items found for this invoice")

    # ── Smart Print button ──────────────────────────────────────────────
    if st.button("🖨️ Smart Print / PDF", key=f"smartprint_{inv['id']}", type="secondary",
                 use_container_width=True,
                 help="Tally-style A4/A5 invoice with CGST/SGST/IGST split, power details, bank info"):
        st.session_state[f"_show_smart_print_{inv['id']}"] = True

    if st.session_state.get(f"_show_smart_print_{inv['id']}"):
        try:
            from modules.billing.smart_print import render_smart_invoice
            render_smart_invoice(inv["invoice_no"])
        except Exception as _spe:
            st.error(f"Smart print error: {_spe}")

    # ── Payment actions ───────────────────────────────────────────────────
    st.markdown("<hr class='ent-divider'>", unsafe_allow_html=True)
    pa1, pa2, pa3 = st.columns(3)
    with pa1:
        if pstatus != "PAID":
            if st.button("💳 Mark as Paid", key=f"pay_{inv['id']}", type="primary", use_container_width=True):
                if _write("UPDATE invoices SET payment_status='PAID',status='PAID',updated_at=NOW() WHERE id=%(id)s",
                          {"id": inv["id"]}):
                    st.success("✅ Marked as paid")
                    st.rerun()
    with pa2:
        pass  # payment handled by panel below
    with pa3:
        # Credit Note button — just sets flag, CDN renders below outside columns
        _cdn_key = f"show_cdn_{inv['id']}"
        if not st.session_state.get(_cdn_key):
            if st.button("📄 Issue Credit / Debit Note",
                         key=f"cdn_btn_{inv['id']}",
                         use_container_width=True,
                         help="Issue a Credit or Debit Note for this invoice"):
                st.session_state[_cdn_key] = True
                st.session_state["cdn_prefill_invoice_no"] = inv["invoice_no"]
                st.rerun()
        else:
            st.button("📄 Credit / Debit Note ▼", key=f"cdn_btn_{inv['id']}",
                      use_container_width=True, disabled=True)

    # ── Payment panel ──────────────────────────────────────────────────────
    st.markdown("---")
    try:
        from modules.billing.payment_manager import render_wholesale_payment_panel
        _inv_party_id = str(inv["party_id"]) if inv.get("party_id") else None
        _inv_pmode    = str(inv.get("payment_mode") or "ON_COMPLETION")
        if str(inv.get("payment_status","")).upper() != "PAID":
            render_wholesale_payment_panel(
                invoice_id   = str(inv["id"]),
                invoice_no   = inv["invoice_no"],
                party_id     = _inv_party_id,
                party_name   = str(inv.get("party_name") or ""),
                payment_mode = _inv_pmode,
            )
        else:
            st.success("✅ Invoice fully paid")
    except Exception as _pe:
        st.caption(f"Payment panel error: {_pe}")

    # ── Credit / Debit Note (full width, outside columns) ─────────────────
    _cdn_key    = f"show_cdn_{inv['id']}"
    _cdn_cn_key = f"cdn_issued_cn_{inv['id']}"   # CN number if just saved

    # Check if a CN was just issued for this invoice
    _just_issued = st.session_state.get(_cdn_cn_key)

    if _just_issued:
        st.markdown("---")
        st.success(f"✅ Credit Note **{_just_issued}** issued for {inv['invoice_no']}")
        if st.button("📄 Issue another CN", key=f"cdn_another_{inv['id']}"):
            st.session_state.pop(_cdn_cn_key, None)
            st.session_state[_cdn_key] = True
            st.session_state["cdn_prefill_invoice_no"] = inv["invoice_no"]
            st.rerun()
    elif st.session_state.get(_cdn_key):
        st.markdown("---")
        c_hdr, c_cls = st.columns([8, 1])
        with c_hdr:
            st.markdown(
                f"<div style='background:#0f172a;border-left:4px solid #3b82f6;"
                f"padding:8px 14px;border-radius:4px'>"
                f"<span style='color:#38bdf8;font-weight:700'>📄 Issue Credit / Debit Note</span>"
                f" <span style='color:#94a3b8;font-size:0.75rem'>— {inv['invoice_no']}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with c_cls:
            if st.button("✕ Close", key=f"cdn_close_{inv['id']}",
                         use_container_width=True):
                # Clear ALL CDN-related session state
                _cdn_keys = [k for k in list(st.session_state.keys())
                             if k.startswith("cn_") or k.startswith("dn_")
                             or k in ("cdn_prefill_invoice_no",)]
                for _k in _cdn_keys:
                    st.session_state.pop(_k, None)
                st.session_state.pop(_cdn_key, None)
                st.session_state.pop(_cdn_cn_key, None)
                st.rerun()
        try:
            from modules.billing.credit_debit_note_ui import render_cdn_module
            render_cdn_module(inline=True)
        except Exception as _cdne:
            st.error(f"CDN module error: {_cdne}")


# ══════════════════════════════════════════════════════════════════════════
# CHALLANS LIST
# ══════════════════════════════════════════════════════════════════════════

def render_challans_list():
    _inject_css()

    # ── Retail / Wholesale subtabs ────────────────────────────────────────
    sub_retail, sub_wholesale = st.tabs(["🛍️ Retail Challans", "🏭 Wholesale Challans"])
    for _tab, _otype in ((sub_retail, "RETAIL"), (sub_wholesale, "WHOLESALE")):
        with _tab:
            _render_challans_for_type(_otype)


def _render_challans_for_type(order_type_filter: str):
    """Render challan list filtered by order type (RETAIL or WHOLESALE)."""
    _inject_css()

    # ── Filters ───────────────────────────────────────────────────────────
    _k = order_type_filter.lower()
    fc1, fc2, fc3, fc4 = st.columns([2, 2, 1.5, 1.5])
    with fc1:
        search = st.text_input("🔍 Search", placeholder="Party or challan no…", label_visibility="collapsed", key=f"ch_search_{_k}")
    with fc2:
        status_f = st.selectbox("Status", ["All", "PENDING", "INVOICED", "CANCELLED"], label_visibility="collapsed", key=f"ch_status_{_k}")
    with fc3:
        from_d = st.date_input("From", value=date.today().replace(day=1), label_visibility="collapsed", key=f"ch_from_{_k}")
    with fc4:
        to_d = st.date_input("To", value=date.today(), label_visibility="collapsed", key=f"ch_to_{_k}")

    where = ["c.challan_date BETWEEN %(f)s AND %(t)s"]
    params = {"f": from_d, "t": to_d, "otype": order_type_filter}
    if status_f != "All":
        where.append("c.status = %(st)s"); params["st"] = status_f
    if search:
        where.append("""(
            COALESCE(p.party_name,
                (SELECT o2.party_name FROM orders o2
                 WHERE o2.id::text = ANY(c.order_ids) LIMIT 1)
            ) ILIKE %(s)s
            OR c.challan_no ILIKE %(s)s)""")
        params["s"] = f"%{search}%"

    # Filter by order_type via the first order in the challan's order_ids array
    where.append("""EXISTS (
        SELECT 1 FROM orders ox
        WHERE ox.id::text = ANY(c.order_ids)
          AND UPPER(COALESCE(ox.order_type,'WHOLESALE')) = %(otype)s
        LIMIT 1
    )""")

    challans = _q(f"""
        SELECT c.id, c.challan_no, c.challan_date, c.status,
               c.total_amount, c.total_tax, c.grand_total,
               c.order_ids, c.created_by,
               COALESCE(p.party_name,
                   (SELECT o2.party_name FROM orders o2
                    WHERE o2.id::text = ANY(c.order_ids) LIMIT 1),
                   'Unknown'
               ) AS party_name,
               COALESCE(p.mobile, '') AS mobile
        FROM challans c
        LEFT JOIN parties p ON p.id = c.party_id
        WHERE {' AND '.join(where)}
        ORDER BY c.challan_date DESC, c.created_at DESC
        LIMIT 200
    """, params)

    if not challans:
        st.info("No challans found for the selected filters.")
        return

    # ── Summary metrics ───────────────────────────────────────────────────
    total_val = sum(float(c.get("grand_total") or 0) for c in challans)
    pending_c = [c for c in challans if c["status"] == "PENDING"]
    st.markdown(f"""
    <div class='kpi-grid'>
      <div class='kpi' style='--accent:#3b82f6'><div class='kpi-label'>Total Challans</div><div class='kpi-value'>{len(challans)}</div></div>
      <div class='kpi' style='--accent:#f59e0b'><div class='kpi-label'>Pending</div><div class='kpi-value'>{len(pending_c)}</div></div>
      <div class='kpi' style='--accent:#10b981'><div class='kpi-label'>Invoiced</div><div class='kpi-value'>{sum(1 for c in challans if c["status"]=="INVOICED")}</div></div>
      <div class='kpi' style='--accent:#38bdf8'><div class='kpi-label'>Total Value</div><div class='kpi-value'>{_fmt_currency(total_val)}</div><div class='kpi-sub'>filtered period</div></div>
    </div>
    """, unsafe_allow_html=True)

    # ── State: which challan row is expanded ────────────────────────────
    _state_key = f"ch_open_{_k}"
    if _state_key not in st.session_state:
        st.session_state[_state_key] = None

    # ── Batch print bar ──────────────────────────────────────────────────
    _ch_sel_key = f"ch_sel_{_k}"
    if _ch_sel_key not in st.session_state:
        st.session_state[_ch_sel_key] = set()
    _ch_selected = st.session_state[_ch_sel_key]
    _batch_ch_nos = [c["challan_no"] for c in challans]

    _bp1, _bp2, _bp3, _bp4 = st.columns([2, 1.5, 1.5, 1.5])
    with _bp1:
        st.markdown(
            f"<span style='font-size:0.72rem;color:#64748b'>"
            f"{len(_ch_selected)} challan(s) selected</span>",
            unsafe_allow_html=True)
    with _bp2:
        if st.button("☑️ Select All", key=f"ch_selall_{_k}", use_container_width=True):
            st.session_state[_ch_sel_key] = set(_batch_ch_nos)
            st.rerun()
    with _bp3:
        if st.button("☐ Clear", key=f"ch_clrsel_{_k}", use_container_width=True,
                     disabled=not _ch_selected):
            st.session_state[_ch_sel_key] = set()
            st.rerun()
    with _bp4:
        if st.button("🖨️ Print Selected", key=f"ch_batchprint_{_k}",
                     type="primary", use_container_width=True,
                     disabled=not _ch_selected):
            st.session_state[f"ch_batch_show_{_k}"] = True
    if st.session_state.get(f"ch_batch_show_{_k}") and _ch_selected:
        try:
            from modules.billing.smart_print import render_smart_challan, _CSS
            html_parts = []
            for _bch_no in sorted(_ch_selected):
                html = render_smart_challan(_bch_no, return_html=True)
                if html:
                    html_parts.append(html)
            if html_parts:
                html_blocks = []
                for html in html_parts:
                    block = f'<div class="print-page">{html}</div>'
                    html_blocks.append(block)
                combined_html = ''.join(html_blocks)
                # Add print button at the top
                print_button_html = '<div style="text-align:center; padding:10px; background:#f0f0f0;"><button onclick="window.print()" style="padding:10px 20px; font-size:16px; background:#007bff; color:white; border:none; border-radius:5px;">🖨️ Print / Save as PDF</button></div>'
                combined_html = combined_html.replace('</style>', '</style>' + print_button_html, 1)
                st.components.v1.html(combined_html, height=920 * len(html_parts) + 50, scrolling=True)
        except Exception as _bpe:
            st.error(f"Batch print error: {_bpe}")
        if st.button("✕ Close Batch Print", key=f"ch_batch_close_{_k}"):
            st.session_state[f"ch_batch_show_{_k}"] = False
            st.session_state[_ch_sel_key] = set()
            st.rerun()

    st.markdown("<hr style='margin:6px 0;border:none;border-top:1px solid #1e293b'>",
                unsafe_allow_html=True)

    # ── Column header row ─────────────────────────────────────────────────
    _hrow = st.columns([0.4, 0.5, 1.8, 1.3, 2.2, 0.6, 1.1, 1.0, 1.1, 0.9, 0.7])
    for _hc, _hl in zip(_hrow, ["☑", "", "Challan No", "Date", "Party",
                                  "Orders", "Subtotal", "GST",
                                  "Grand Total", "Status", "Print"]):
        _hc.markdown(
            f"<div style='font-size:0.67rem;font-weight:700;color:#475569;"
            f"text-transform:uppercase;letter-spacing:.06em;padding:3px 0'>"
            f"{_hl}</div>",
            unsafe_allow_html=True,
        )
    st.markdown(
        "<hr style='margin:3px 0 6px;border:none;border-top:1px solid #1e293b'>",
        unsafe_allow_html=True,
    )

    # ── One row per challan ────────────────────────────────────────────────
    for c in challans:
        ono      = c["challan_no"]
        is_open  = (st.session_state[_state_key] == ono)
        orders_cnt = len(c.get("order_ids") or [])
        _status  = str(c.get("status") or "PENDING")
        _sc      = {"PENDING": "#f59e0b", "INVOICED": "#10b981",
                    "CANCELLED": "#ef4444"}.get(_status, "#64748b")
        _row_bg  = "#1e3a5f22" if is_open else "transparent"

        # Subtle highlight on the open row
        st.markdown(
            f"<div style='background:{_row_bg};border-radius:6px;margin:1px 0'>",
            unsafe_allow_html=True,
        )
        rcols = st.columns([0.4, 0.5, 1.8, 1.3, 2.2, 0.6, 1.1, 1.0, 1.1, 0.9, 0.7])

        with rcols[0]:
            _is_ch_sel = ono in st.session_state.get(_ch_sel_key, set())
            _new_ch_sel = st.checkbox("", value=_is_ch_sel, key=f"ch_chk_{_k}_{ono}",
                                      label_visibility="collapsed")
            if _new_ch_sel:
                st.session_state[_ch_sel_key].add(ono)
            else:
                st.session_state[_ch_sel_key].discard(ono)
        with rcols[1]:
            _arrow = "▼" if is_open else "▶"
            if st.button(_arrow, key=f"ch_arr_{_k}_{ono}",
                         use_container_width=True, help="Toggle preview"):
                st.session_state[_state_key] = None if is_open else ono
                st.rerun()

        with rcols[2]:
            # Challan No — primary click target
            _btn_type = "primary" if is_open else "secondary"
            if st.button(ono, key=f"ch_no_{_k}_{ono}",
                         use_container_width=True,
                         type=_btn_type, help="Click to open preview"):
                st.session_state[_state_key] = None if is_open else ono
                st.rerun()

        with rcols[3]:
            st.markdown(
                f"<div style='font-size:0.78rem;color:#94a3b8;padding:6px 2px'>"
                f"{_fmt_date(c.get('challan_date'))}</div>",
                unsafe_allow_html=True,
            )
        with rcols[3]:
            st.markdown(
                f"<div style='font-size:0.82rem;font-weight:600;color:#e2e8f0;"
                f"padding:4px 2px;line-height:1.3'>{c['party_name']}"
                f"<br><span style='font-size:0.63rem;font-weight:400;color:#475569'>"
                f"{c.get('mobile','')}</span></div>",
                unsafe_allow_html=True,
            )
        with rcols[4]:
            st.markdown(
                f"<div style='font-size:0.8rem;color:#94a3b8;text-align:center;"
                f"padding:6px 2px'>{orders_cnt}</div>",
                unsafe_allow_html=True,
            )
        with rcols[5]:
            st.markdown(
                f"<div style='font-size:0.8rem;color:#94a3b8;text-align:right;"
                f"padding:6px 2px'>{_fmt_currency(c.get('total_amount',0))}</div>",
                unsafe_allow_html=True,
            )
        with rcols[6]:
            st.markdown(
                f"<div style='font-size:0.8rem;color:#f59e0b;text-align:right;"
                f"padding:6px 2px'>{_fmt_currency(c.get('total_tax',0))}</div>",
                unsafe_allow_html=True,
            )
        with rcols[7]:
            st.markdown(
                f"<div style='font-size:0.88rem;font-weight:700;color:#10b981;"
                f"text-align:right;padding:6px 2px'>"
                f"{_fmt_currency(c.get('grand_total',0))}</div>",
                unsafe_allow_html=True,
            )
        with rcols[8]:
            st.markdown(
                f"<span style='background:{_sc}22;color:{_sc};padding:2px 8px;"
                f"border-radius:8px;font-size:0.65rem;font-weight:700'>"
                f"{_status}</span>",
                unsafe_allow_html=True,
            )
        with rcols[10]:
            if st.button("🖨️", key=f"ch_qprint_{_k}_{ono}",
                         use_container_width=True,
                         help="Quick Print / PDF"):
                st.session_state[f"_show_ch_print_{ono}_qk"] = True
                st.session_state[_state_key] = ono
                st.rerun()
        if st.session_state.get(f"_show_ch_print_{ono}_qk"):
            try:
                from modules.billing.smart_print import render_smart_challan
                render_smart_challan(ono)
            except Exception as _qpce:
                st.error(f"Print error: {_qpce}")

        st.markdown("</div>", unsafe_allow_html=True)

        # ── Inline preview — expands immediately below the clicked row ────
        if is_open:
            with st.container():
                st.markdown(
                    "<div style='background:#0f172a;border:1px solid #1e3a5f;"
                    "border-radius:10px;padding:16px 20px;margin:6px 0 14px'>",
                    unsafe_allow_html=True,
                )
                _ccol, _ = st.columns([1, 7])
                with _ccol:
                    if st.button("✕ Close", key=f"ch_cls_{_k}_{ono}",
                                 use_container_width=True):
                        st.session_state[_state_key] = None
                        st.rerun()
                render_challan_preview(ono)
                st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<hr class='ent-divider'>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# INVOICES LIST
# ══════════════════════════════════════════════════════════════════════════

def render_invoices_list():
    _inject_css()
    sub_retail, sub_wholesale = st.tabs(["🛍️ Retail Invoices", "🏭 Wholesale Invoices"])
    for _tab, _otype in ((sub_retail, "RETAIL"), (sub_wholesale, "WHOLESALE")):
        with _tab:
            _render_invoices_for_type(_otype)


def _render_invoices_for_type(order_type_filter: str):
    _inject_css()
    _k = order_type_filter.lower()

    fi1, fi2, fi3, fi4, fi5 = st.columns([2, 1.5, 1.5, 1.2, 1.2])
    with fi1:
        search = st.text_input("🔍 Search", placeholder="Party or invoice no…", label_visibility="collapsed", key=f"inv_search_{_k}")
    with fi2:
        status_f  = st.selectbox("Status",  ["All","PENDING","PAID","OVERDUE","CANCELLED"], label_visibility="collapsed", key=f"inv_status_{_k}")
    with fi3:
        payment_f = st.selectbox("Payment", ["All","UNPAID","PARTIAL","PAID"],              label_visibility="collapsed", key=f"inv_payment_{_k}")
    with fi4:
        from_d = st.date_input("From", value=date.today().replace(day=1), label_visibility="collapsed", key=f"inv_from_{_k}")
    with fi5:
        to_d   = st.date_input("To",   value=date.today(),                label_visibility="collapsed", key=f"inv_to_{_k}")

    where  = ["i.invoice_date BETWEEN %(f)s AND %(t)s"]
    params = {"f": from_d, "t": to_d, "otype": order_type_filter}
    if status_f  != "All": where.append("i.status = %(st)s");          params["st"] = status_f
    if payment_f != "All": where.append("i.payment_status = %(ps)s");  params["ps"] = payment_f
    if search:
        where.append("""(COALESCE(p.party_name,
            (SELECT o2.party_name FROM orders o2 WHERE o2.id::text = ANY(i.order_ids) LIMIT 1)
        ) ILIKE %(s)s OR i.invoice_no ILIKE %(s)s)""")
        params["s"] = f"%{search}%"

    where.append("""(
        SELECT UPPER(COALESCE(ox.order_type,'WHOLESALE'))
        FROM orders ox
        WHERE ox.id::text = ANY(i.order_ids)
        ORDER BY ox.created_at DESC
        LIMIT 1
    ) = %(otype)s""")

    invoices = _q(f"""
        SELECT i.id, i.invoice_no, i.invoice_date, i.due_date,
               i.status, i.payment_status,
               i.total_amount, i.total_tax, i.grand_total,
               i.order_ids, i.created_by,
               COALESCE(p.party_name,
                   (SELECT o2.party_name FROM orders o2
                    WHERE o2.id::text = ANY(i.order_ids) LIMIT 1),
                   'Unknown') AS party_name,
               COALESCE(p.mobile, '') AS mobile,
               c.challan_no
        FROM invoices i
        LEFT JOIN parties p     ON p.id = i.party_id
        LEFT JOIN challans c ON c.id = i.challan_id
        WHERE {' AND '.join(where)}
        ORDER BY i.invoice_date DESC, i.created_at DESC
        LIMIT 200
    """, params)

    if not invoices:
        st.info("No invoices found for the selected filters.")
        return

    total_val = sum(float(i.get("grand_total") or 0) for i in invoices)
    # ── Batch print selected invoices ────────────────────────────────────
    _batch_key = "invoice_batch_selected"
    if st.session_state.get(_batch_key):
        _sel_invs = list(st.session_state[_batch_key])
        if _sel_invs:
            _bc1, _bc2 = st.columns([3,1])
            _bc1.markdown(f"**{len(_sel_invs)} invoice(s) selected for batch print**")
            with _bc2:
                if st.button("🖨️ Batch Print Selected", type="primary",
                             key="do_batch_print", use_container_width=True):
                    st.session_state["_show_batch_print"] = True
            if st.session_state.pop("_show_batch_print", False):
                from modules.billing.smart_print import render_batch_print
                render_batch_print(_sel_invs)
                st.stop()
    st.markdown("---")

    unpaid    = [i for i in invoices if i.get("payment_status") == "UNPAID"]
    overdue   = [i for i in unpaid if i.get("due_date") and
                 (i["due_date"] if isinstance(i["due_date"], date)
                  else datetime.strptime(str(i["due_date"])[:10],"%Y-%m-%d").date()) < date.today()]

    st.markdown(f"""
    <div class='kpi-grid'>
      <div class='kpi' style='--accent:#3b82f6'><div class='kpi-label'>Total Invoices</div><div class='kpi-value'>{len(invoices)}</div></div>
      <div class='kpi' style='--accent:#ef4444'><div class='kpi-label'>Unpaid</div><div class='kpi-value'>{len(unpaid)}</div><div class='kpi-sub'>{_fmt_currency(sum(float(i.get("grand_total",0)) for i in unpaid))} outstanding</div></div>
      <div class='kpi' style='--accent:#f59e0b'><div class='kpi-label'>Overdue</div><div class='kpi-value'>{len(overdue)}</div></div>
      <div class='kpi' style='--accent:#10b981'><div class='kpi-label'>Total Value</div><div class='kpi-value'>{_fmt_currency(total_val)}</div><div class='kpi-sub'>filtered period</div></div>
    </div>
    """, unsafe_allow_html=True)

    # ── State: which invoice row is expanded ─────────────────────────────
    _inv_state = f"inv_open_{_k}"
    if _inv_state not in st.session_state:
        st.session_state[_inv_state] = None

    # ── Batch print bar ───────────────────────────────────────────────────
    _inv_sel_key = f"inv_sel_{_k}"
    if _inv_sel_key not in st.session_state:
        st.session_state[_inv_sel_key] = set()
    _inv_selected = st.session_state[_inv_sel_key]
    _batch_inv_nos = [i["invoice_no"] for i in invoices]

    _ibp1, _ibp2, _ibp3, _ibp4 = st.columns([2, 1.5, 1.5, 1.5])
    with _ibp1:
        st.markdown(
            f"<span style='font-size:0.72rem;color:#64748b'>"
            f"{len(_inv_selected)} invoice(s) selected</span>",
            unsafe_allow_html=True)
    with _ibp2:
        if st.button("☑️ Select All", key=f"inv_selall_{_k}", use_container_width=True):
            st.session_state[_inv_sel_key] = set(_batch_inv_nos)
            st.rerun()
    with _ibp3:
        if st.button("☐ Clear", key=f"inv_clrsel_{_k}", use_container_width=True,
                     disabled=not _inv_selected):
            st.session_state[_inv_sel_key] = set()
            st.rerun()
    with _ibp4:
        if st.button("🖨️ Print Selected", key=f"inv_batchprint_{_k}",
                     type="primary", use_container_width=True,
                     disabled=not _inv_selected):
            st.session_state[f"inv_batch_show_{_k}"] = True
    if st.session_state.get(f"inv_batch_show_{_k}") and _inv_selected:
        try:
            from modules.billing.smart_print import render_smart_invoice, _CSS
            html_parts = []
            for _bino in sorted(_inv_selected):
                html = render_smart_invoice(_bino, return_html=True)
                if html:
                    html_parts.append(html)
            if html_parts:
                html_blocks = []
                for html in html_parts:
                    block = f'<div class="print-page">{html}</div>'
                    html_blocks.append(block)
                combined_html = ''.join(html_blocks)
                # Add print button at the top
                print_button_html = '<div style="text-align:center; padding:10px; background:#f0f0f0;"><button onclick="window.print()" style="padding:10px 20px; font-size:16px; background:#007bff; color:white; border:none; border-radius:5px;">🖨️ Print / Save as PDF</button></div>'
                combined_html = combined_html.replace('</style>', '</style>' + print_button_html, 1)
                st.components.v1.html(combined_html, height=920 * len(html_parts) + 50, scrolling=True)
        except Exception as _ibpe:
            st.error(f"Batch print error: {_ibpe}")
        if st.button("✕ Close Batch Print", key=f"inv_batch_close_{_k}"):
            st.session_state[f"inv_batch_show_{_k}"] = False
            st.session_state[_inv_sel_key] = set()
            st.rerun()

    st.markdown("<hr style='margin:6px 0;border:none;border-top:1px solid #1e293b'>",
                unsafe_allow_html=True)

    # ── Column headers ────────────────────────────────────────────────────
    _irow = st.columns([0.4, 0.5, 1.8, 1.2, 1.2, 2.2, 1.1, 1.2, 0.9, 0.9, 0.7])
    for _hc, _hl in zip(_irow, ["☑", "", "Invoice No", "Date", "Due Date",
                                  "Party", "Challan", "Grand Total",
                                  "Status", "Payment", "Print"]):
        _hc.markdown(
            f"<div style='font-size:0.67rem;font-weight:700;color:#475569;"
            f"text-transform:uppercase;letter-spacing:.06em;padding:3px 0'>"
            f"{_hl}</div>",
            unsafe_allow_html=True,
        )
    st.markdown(
        "<hr style='margin:3px 0 6px;border:none;border-top:1px solid #1e293b'>",
        unsafe_allow_html=True,
    )

    for i in invoices:
        ino      = i["invoice_no"]
        is_open  = (st.session_state[_inv_state] == ino)
        pstatus  = str(i.get("payment_status") or "UNPAID").upper()
        pcolor   = {"PAID":"#10b981","PARTIAL":"#f59e0b","UNPAID":"#ef4444"}.get(pstatus,"#ef4444")
        overdue_flag = ""
        if pstatus != "PAID" and i.get("due_date"):
            try:
                dd = i["due_date"] if isinstance(i["due_date"], date) else                      datetime.strptime(str(i["due_date"])[:10],"%Y-%m-%d").date()
                if dd < date.today(): overdue_flag = " ⚠️"
            except: pass
        _row_bg = "#1e3a5f22" if is_open else "transparent"

        st.markdown(f"<div style='background:{_row_bg};border-radius:6px;margin:1px 0'>",
                    unsafe_allow_html=True)
        icols = st.columns([0.4, 0.5, 1.8, 1.2, 1.2, 2.2, 1.1, 1.2, 0.9, 0.9, 0.7])

        with icols[0]:
            _is_inv_sel = ino in st.session_state.get(_inv_sel_key, set())
            _new_inv_sel = st.checkbox("", value=_is_inv_sel, key=f"inv_chk_{_k}_{ino}",
                                       label_visibility="collapsed")
            if _new_inv_sel:
                st.session_state[_inv_sel_key].add(ino)
            else:
                st.session_state[_inv_sel_key].discard(ino)
        with icols[1]:
            _arrow = "▼" if is_open else "▶"
            if st.button(_arrow, key=f"inv_arr_{_k}_{ino}",
                         use_container_width=True, help="Toggle preview"):
                st.session_state[_inv_state] = None if is_open else ino
                st.rerun()
        with icols[2]:
            _btn_type = "primary" if is_open else "secondary"
            if st.button(ino, key=f"inv_no_{_k}_{ino}",
                         use_container_width=True,
                         type=_btn_type, help="Click to open preview"):
                st.session_state[_inv_state] = None if is_open else ino
                st.rerun()
        with icols[3]:
            st.markdown(
                f"<div style='font-size:0.78rem;color:#94a3b8;padding:6px 2px'>"
                f"{_fmt_date(i.get('invoice_date'))}</div>",
                unsafe_allow_html=True)
        with icols[4]:
            _due_c = "#ef4444" if overdue_flag else "#94a3b8"
            st.markdown(
                f"<div style='font-size:0.78rem;color:{_due_c};padding:6px 2px'>"
                f"{_fmt_date(i.get('due_date'))}{overdue_flag}</div>",
                unsafe_allow_html=True)
        with icols[5]:
            st.markdown(
                f"<div style='font-size:0.82rem;font-weight:600;color:#e2e8f0;"
                f"padding:4px 2px'>{i['party_name']}</div>",
                unsafe_allow_html=True)
        with icols[6]:
            st.markdown(
                f"<div style='font-size:0.72rem;font-family:monospace;color:#64748b;"
                f"padding:6px 2px'>{i.get('challan_no') or '—'}</div>",
                unsafe_allow_html=True)
        with icols[7]:
            st.markdown(
                f"<div style='font-size:0.88rem;font-weight:700;color:#10b981;"
                f"text-align:right;padding:6px 2px'>"
                f"{_fmt_currency(i.get('grand_total',0))}</div>",
                unsafe_allow_html=True)
        with icols[8]:
            _sc = {"PENDING":"#f59e0b","PAID":"#10b981","CANCELLED":"#ef4444"}.get(
                   str(i.get("status") or ""), "#64748b")
            st.markdown(
                f"<span style='background:{_sc}22;color:{_sc};padding:2px 7px;"
                f"border-radius:8px;font-size:0.65rem;font-weight:700'>"
                f"{i.get('status','')}</span>",
                unsafe_allow_html=True)
        with icols[9]:
            st.markdown(
                f"<span style='color:{pcolor};font-size:0.72rem;font-weight:700'>"
                f"{pstatus}</span>",
                unsafe_allow_html=True)
        with icols[10]:
            if st.button("🖨️", key=f"inv_qprint_{_k}_{ino}",
                         use_container_width=True,
                         help="Quick Print / PDF"):
                st.session_state[f"_show_smart_print_{ino}_qk"] = True
                st.session_state[_inv_state] = ino
                st.rerun()
        if st.session_state.get(f"_show_smart_print_{ino}_qk"):
            try:
                from modules.billing.smart_print import render_smart_invoice
                render_smart_invoice(ino)
            except Exception as _qpe:
                st.error(f"Print error: {_qpe}")

        st.markdown("</div>", unsafe_allow_html=True)

        # ── Inline invoice preview ─────────────────────────────────────────
        if is_open:
            with st.container():
                st.markdown(
                    "<div style='background:#0f172a;border:1px solid #1e3a5f;"
                    "border-radius:10px;padding:16px 20px;margin:6px 0 14px'>",
                    unsafe_allow_html=True)
                _ccol, _ = st.columns([1, 7])
                with _ccol:
                    if st.button("✕ Close", key=f"inv_cls_{_k}_{ino}",
                                 use_container_width=True):
                        st.session_state[_inv_state] = None
                        st.rerun()
                render_invoice_preview(ino)
                st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<hr class='ent-divider'>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# CREATE CHALLAN (delegates to challan_invoice_manager)
# ══════════════════════════════════════════════════════════════════════════

def render_challan_creation():
    try:
        from modules.billing.challan_invoice_manager import (
            render_challan_creation as _mgr
        )
        _mgr()
    except Exception as e:
        st.error(f"Challan creation error: {e}")
        import traceback; st.code(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════════
# CREATE INVOICE
# ══════════════════════════════════════════════════════════════════════════

def render_invoice_creation():
    _inject_css()
    st.markdown("### 🧾 Create Invoice")

    tab_ch, tab_direct = st.tabs(["📋 From Challan", "🎯 Direct Invoice"])

    # ── From Challan ──────────────────────────────────────────────────────
    with tab_ch:
        pending = _q("""
            SELECT c.id, c.challan_no, c.challan_date, c.grand_total,
                   c.order_ids, c.party_id,
                   COALESCE(p.party_name,
                       (SELECT o2.party_name FROM orders o2
                        WHERE o2.id::text = ANY(c.order_ids) LIMIT 1),
                       'Unknown') AS party_name,
                   COALESCE(p.mobile,'') AS mobile
            FROM challans c
            LEFT JOIN parties p ON p.id = c.party_id
            WHERE c.status = 'PENDING'
            ORDER BY c.challan_date DESC
        """)

        # Pre-select from "Create Invoice" button in challan preview
        _preseed = st.session_state.pop("create_invoice_from_challan", None)

        if not pending:
            st.info("No pending challans available to invoice.")
            return

        # ── Left: party list | Right: challans for selected party ──────
        # Group by party
        from collections import defaultdict as _dd
        _by_party = _dd(list)
        for ch in pending:
            _by_party[(ch["party_id"], ch["party_name"], ch.get("mobile",""))].append(ch)

        _party_list = sorted(_by_party.keys(), key=lambda x: x[1] or "")

        # ── Party slug — unique key even when party_id is None (retail walk-ins) ──
        def _pslug(pid, pname, pmobile=""):
            """Unique slug per party row — handles None party_id (retail walk-ins)."""
            if pid:
                return str(pid)
            safe = (pname or "unknown").lower().replace(" ", "_")[:30]
            mob  = (pmobile or "").replace(" ", "")[-6:]
            return f"nopid_{safe}_{mob}"

        _sel_party_key = "inv_create_party"
        # Auto-select party if coming from challan preview
        if _preseed:
            _pre_row = next((ch for ch in pending if str(ch["id"]) == str(_preseed)), None)
            if _pre_row:
                _pre_slug = _pslug(
                    _pre_row.get("party_id"),
                    _pre_row.get("party_name", ""),
                    _pre_row.get("mobile", "")
                )
                st.session_state[_sel_party_key] = _pre_slug

        _pc_left, _pc_right = st.columns([1, 2.5])

        with _pc_left:
            st.markdown("<div class='sec-head'>Parties</div>", unsafe_allow_html=True)
            for pid, pname, pmobile in _party_list:
                _slug      = _pslug(pid, pname, pmobile)
                _pchallans = _by_party[(pid, pname, pmobile)]
                _pamt      = sum(float(ch.get("grand_total") or 0) for ch in _pchallans)
                _is_active = st.session_state.get(_sel_party_key) == _slug
                _bg  = "#0d2818" if _is_active else "#0f172a"
                _brd = "#10b981" if _is_active else "#1e293b"
                st.markdown(
                    f"<div style='background:{_bg};border:1px solid {_brd};border-radius:8px;"
                    f"padding:8px 12px;margin-bottom:5px'>",
                    unsafe_allow_html=True)
                if st.button(
                    f"{{'\u25b6 ' if _is_active else ''}}{pname}",
                    key=f"inv_party_{_slug}",   # slug never None — no duplicate keys
                    use_container_width=True,
                    type="primary" if _is_active else "secondary",
                ):
                    st.session_state[_sel_party_key] = _slug
                    st.rerun()
                st.markdown(
                    f"<div style='color:#64748b;font-size:0.65rem;padding:2px 4px'>"
                    f"{len(_pchallans)} challans \u00b7 {_fmt_currency(_pamt)}</div>",
                    unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)

        with _pc_right:
            _active_slug = st.session_state.get(_sel_party_key)
            if not _active_slug:
                st.info("\u2190 Select a party to see their pending challans")
                return

            # Resolve slug back to the matching (pid, pname, pmobile) tuple
            _active_tuple = next(
                (t for t in _party_list if _pslug(*t) == _active_slug),
                None
            )
            if not _active_tuple:
                st.info("\u2190 Select a party to see their pending challans")
                return
            _active_pid, _pname_active, _active_mobile = _active_tuple

            _party_challans = _by_party[_active_tuple]
            if not _party_challans:
                st.info("No pending challans for this party")
                return

            st.markdown(f"<div class='sec-head'>Pending Challans \u2014 {_pname_active}</div>",
                        unsafe_allow_html=True)

            # Check/uncheck all — key uses slug so None-party rows stay isolated
            _sel_key = f"inv_sel_chs_{_active_slug}"
            if _sel_key not in st.session_state:
                # Auto-select preseed challan if coming from preview
                if _preseed:
                    st.session_state[_sel_key] = {str(_preseed)}
                else:
                    st.session_state[_sel_key] = set()

            _ca, _cb = st.columns([1,3])
            with _ca:
                if st.button("☑ Select All", key=f"inv_selall_{_active_slug}",
                             use_container_width=True):
                    st.session_state[_sel_key] = {str(ch["id"]) for ch in _party_challans}
                    st.rerun()
            with _cb:
                if st.button("☐ Clear All", key=f"inv_clrall_{_active_slug}",
                             use_container_width=True):
                    st.session_state[_sel_key] = set()
                    st.rerun()

            for ch in _party_challans:
                _cid = str(ch["id"])
                _checked = _cid in st.session_state.get(_sel_key, set())
                _bg2  = "#0d2030" if _checked else "#0f172a"
                _brd2 = "#38bdf8" if _checked else "#1e293b"
                with st.container():
                    st.markdown(
                        f"<div style='background:{_bg2};border:1px solid {_brd2};"
                        f"border-radius:8px;padding:8px 12px;margin-bottom:5px'>",
                        unsafe_allow_html=True)
                    _cx1, _cx2, _cx3, _cx4 = st.columns([0.5, 2, 1.5, 1.5])
                    with _cx1:
                        if st.checkbox("", value=_checked,
                                       key=f"inv_ch_{_cid}",
                                       label_visibility="collapsed"):
                            st.session_state.setdefault(_sel_key, set()).add(_cid)
                        else:
                            st.session_state.get(_sel_key, set()).discard(_cid)
                    with _cx2:
                        st.markdown(
                            f"<div style='color:#38bdf8;font-weight:700;font-size:0.85rem'>"
                            f"{ch['challan_no']}</div>"
                            f"<div style='color:#475569;font-size:0.65rem'>"
                            f"{_fmt_date(ch.get('challan_date'))}</div>",
                            unsafe_allow_html=True)
                    with _cx3:
                        _n_orders = len(ch.get("order_ids") or [])
                        st.markdown(
                            f"<div style='color:#94a3b8;font-size:0.72rem'>{_n_orders} order(s)</div>",
                            unsafe_allow_html=True)
                    with _cx4:
                        st.markdown(
                            f"<div style='color:#10b981;font-weight:700;text-align:right'>"
                            f"{_fmt_currency(ch.get('grand_total',0))}</div>",
                            unsafe_allow_html=True)
                    st.markdown("</div>", unsafe_allow_html=True)

            sel_chs = list(st.session_state.get(_sel_key, set()))
            if not sel_chs:
                st.info("☝ Tick challans above to include in invoice")
                return

            sel_details = [ch for ch in _party_challans if str(ch["id"]) in sel_chs]
            # Verify all same party (already filtered, but double-check)
            parties = {c["party_id"] for c in sel_details}
            if len(parties) > 1:
                st.error("⚠️ All challans must belong to the same party.")
                return

        total_amt = sum(float(_q("SELECT grand_total FROM challans WHERE id=%(i)s", {"i": c["id"]})[0].get("grand_total",0)) for c in sel_details)
        all_order_nos = [ono for c in sel_details for ono in (c.get("order_ids") or [])]

        st.markdown(f"""
        <div class='kpi-grid'>
          <div class='kpi' style='--accent:#3b82f6'><div class='kpi-label'>Challans</div><div class='kpi-value'>{len(sel_chs)}</div></div>
          <div class='kpi' style='--accent:#10b981'><div class='kpi-label'>Grand Total</div><div class='kpi-value'>{_fmt_currency(total_amt)}</div></div>
        </div>
        """, unsafe_allow_html=True)

        due_days = st.number_input("Credit days", min_value=0, value=30, key="inv_due_days")
        remarks  = st.text_area("Remarks", key="inv_remarks", placeholder="Optional…")

        if st.button("🧾 Create Invoice", type="primary", use_container_width=True, key="btn_create_inv_ch"):
            try:
                inv_no = _alloc_inv_no()
                party_id = sel_details[0]["party_id"]
                # Compute sub + tax properly from challan line snapshots
                # Compute totals from challan header (already correct after fix_retail_challan_amounts)
                _ch_totals = _q("""
                    SELECT COALESCE(SUM(total_amount), 0) AS sub,
                           COALESCE(SUM(total_tax),    0) AS tax,
                           COALESCE(SUM(grand_total),  0) AS gnd,
                           -- detect retail: if grand = sum of line_totals (no separate GST add)
                           MAX(EXISTS(
                               SELECT 1 FROM orders ox
                               WHERE ox.id::text = ANY(c.order_ids)
                                 AND UPPER(COALESCE(ox.order_type,'WHOLESALE')) = 'RETAIL'
                           )::int) AS is_retail
                    FROM challans c
                    WHERE c.id = ANY(%(ids)s::uuid[])
                """, {"ids": sel_chs})
                _ct = _ch_totals[0] if _ch_totals else {}
                inv_sub = float(_ct.get("sub") or 0)
                inv_tax = float(_ct.get("tax") or 0)
                inv_gnd = float(_ct.get("gnd") or 0)
                if inv_gnd == 0:
                    # Fallback: sum line_totals directly
                    _lt = _q("SELECT COALESCE(SUM(line_total),0) AS t FROM challan_lines WHERE challan_id = ANY(%(ids)s::uuid[])", {"ids": sel_chs})
                    inv_gnd = float((_lt[0].get("t") if _lt else 0) or 0)
                    inv_sub = inv_gnd; inv_tax = 0.0
                ok = _write("""
                    INSERT INTO invoices
                    (id, invoice_no, challan_id, party_id, order_ids,
                     invoice_date, due_date, total_amount, total_tax, grand_total,
                     status, payment_status, created_by, remarks)
                    VALUES
                    (%(id)s, %(no)s, %(cid)s, %(pid)s, %(oids)s,
                     %(idate)s, %(ddate)s, %(sub)s, %(tax)s, %(gnd)s,
                     'PENDING','UNPAID', %(by)s, %(rmk)s)
                """, {
                    "id":    str(_uuid.uuid4()), "no": inv_no,
                    "cid":   sel_chs[0], "pid": party_id,
                    "oids":  all_order_nos,
                    "idate": date.today(),
                    "ddate": date.today() + timedelta(days=int(due_days)),
                    "sub":   inv_sub, "tax": inv_tax, "gnd": inv_gnd,
                    "by":    st.session_state.get("user_name","System"),
                    "rmk":   remarks,
                })
                if ok:
                    for cid in sel_chs:
                        _write("UPDATE challans SET status='INVOICED',updated_at=NOW() WHERE id=%(i)s", {"i": cid})
                    st.success(f"✅ Invoice **{inv_no}** created")
                    st.rerun()
                else:
                    # Show the actual DB error for diagnosis
                    try:
                        from modules.sql_adapter import run_write as _rw_inv_dbg
                        import uuid as _u_dbg
                        _rw_inv_dbg("""
                            INSERT INTO invoices
                            (id, invoice_no, challan_id, party_id, order_ids,
                             invoice_date, due_date, total_amount, total_tax, grand_total,
                             status, payment_status, created_by, remarks)
                            VALUES
                            (%(id)s, %(no)s, %(cid)s, %(pid)s, %(oids)s,
                             %(idate)s, %(ddate)s, %(sub)s, %(tax)s, %(gnd)s,
                             'PENDING','UNPAID', %(by)s, %(rmk)s)
                        """, {
                            "id":    str(_u_dbg.uuid4()), "no": inv_no,
                            "cid":   sel_chs[0], "pid": party_id,
                            "oids":  all_order_nos,
                            "idate": date.today(),
                            "ddate": date.today() + timedelta(days=int(due_days)),
                            "sub":   inv_sub, "tax": inv_tax, "gnd": inv_gnd,
                            "by":    st.session_state.get("user_name","System"),
                            "rmk":   remarks,
                        })
                        st.success(f"✅ Invoice **{inv_no}** created (retry)")
                        for cid in sel_chs:
                            _write("UPDATE challans SET status='INVOICED',updated_at=NOW() WHERE id=%(i)s", {"i": cid})
                        st.rerun()
                    except Exception as _inv_dbg_e:
                        st.error(f"Invoice insert failed: {_inv_dbg_e}")
            except Exception as e:
                st.error(f"Invoice creation error: {e}")

    # ── Direct Invoice ────────────────────────────────────────────────────
    with tab_direct:
        raw = _q("""
            SELECT o.id, o.order_no, o.created_at, o.total_value,
                   o.party_name, o.party_id, o.status
            FROM orders o
            JOIN parties p ON p.id = o.party_id
            WHERE o.status IN ('PENDING','CONFIRMED','UNDER_REVIEW','READY','READY_FOR_BILLING','IN_PRODUCTION','BILLED')
              AND COALESCE(p.doc_preference, 'C') = 'I'
              AND NOT EXISTS (
                  SELECT 1 FROM invoices i2
                  WHERE i2.status NOT IN ('CANCELLED','VOID')
                    AND i2.order_ids IS NOT NULL
                    AND o.id::text = ANY(i2.order_ids)
              )
            ORDER BY o.created_at DESC
        """)

        direct = [o for o in raw if o.get("status") == "READY_FOR_BILLING"
                  or _is_order_billing_ready(str(o["id"]))[0]]

        if not direct:
            st.info("No direct-invoice orders ready.")
            return

        parties = list({(o["party_id"], o["party_name"]) for o in direct})
        sel_party = st.selectbox("Party", options=parties,
                                 format_func=lambda x: x[1], key="dinv_party")
        party_orders = [o for o in direct if o["party_id"] == sel_party[0]]

        o_opts = {o["id"]: f"{o['order_no']}  ·  {_fmt_currency(o.get('total_value',0))}  ·  {str(o.get('created_at',''))[:10]}"
                  for o in party_orders}
        sel_ords = st.multiselect("Select orders", options=list(o_opts.keys()),
                                  format_func=lambda x: o_opts.get(x,""), key="dinv_orders")
        if not sel_ords:
            return

        tots = _calc_billing_totals(sel_ords)
        st.markdown(f"""
        <div class='kpi-grid'>
          <div class='kpi' style='--accent:#3b82f6'><div class='kpi-label'>Orders</div><div class='kpi-value'>{len(sel_ords)}</div></div>
          <div class='kpi' style='--accent:#f59e0b'><div class='kpi-label'>Subtotal</div><div class='kpi-value'>{_fmt_currency(tots['subtotal'])}</div></div>
          <div class='kpi' style='--accent:#10b981'><div class='kpi-label'>Grand Total</div><div class='kpi-value'>{_fmt_currency(tots['grand_total'])}</div></div>
        </div>
        """, unsafe_allow_html=True)

        # ── Price integrity warning ───────────────────────────────────────
        _bad2 = [r for r in tots.get("line_rows", []) if r.get("price_flag")]
        if _bad2:
            _bad2_names = list({r.get("product_name","?") for r in _bad2})
            st.error(
                "⚠️ **Wholesale Price Issue** — MRP being used instead of trade price for: "
                + ", ".join(_bad2_names)
                + ". Set selling_price in Product Master before invoicing."
            )

        due_d2  = st.number_input("Credit days", min_value=0, value=30, key="dinv_due")
        rmk2    = st.text_area("Remarks", key="dinv_rmk", placeholder="Optional…")
        order_nos = [o["order_no"] for o in party_orders if o["id"] in sel_ords]

        if st.button("🧾 Create Direct Invoice", type="primary", use_container_width=True, key="btn_dinv"):
            try:
                inv_no = _alloc_inv_no()
                ok = _write("""
                    INSERT INTO invoices
                    (id, invoice_no, party_id, order_ids,
                     invoice_date, due_date, total_amount, total_tax, grand_total,
                     status, payment_status, created_by, remarks)
                    VALUES
                    (%(id)s, %(no)s, %(pid)s, %(oids)s,
                     %(idate)s, %(ddate)s, %(sub)s, %(tax)s, %(gnd)s,
                     'PENDING','UNPAID', %(by)s, %(rmk)s)
                """, {
                    "id":    str(_uuid.uuid4()), "no": inv_no,
                    "pid":   sel_party[0], "oids": order_nos,
                    "idate": date.today(),
                    "ddate": date.today() + timedelta(days=int(due_d2)),
                    "sub":   tots["subtotal"], "tax": tots["gst_total"],
                    "gnd":   tots["grand_total"],
                    "by":    st.session_state.get("user_name","System"),
                    "rmk":   rmk2,
                })
                if ok:
                    for oid in sel_ords:
                        _write("UPDATE orders SET status='BILLED',updated_at=NOW() WHERE id=%(i)s::uuid", {"i": oid})
                    st.success(f"✅ Invoice **{inv_no}** created")
                    st.rerun()
                else:
                    # Show the actual DB error for diagnosis
                    try:
                        from modules.sql_adapter import run_write as _rw_inv_dbg
                        import uuid as _u_dbg
                        _rw_inv_dbg("""
                            INSERT INTO invoices
                            (id, invoice_no, challan_id, party_id, order_ids,
                             invoice_date, due_date, total_amount, total_tax, grand_total,
                             status, payment_status, created_by, remarks)
                            VALUES
                            (%(id)s, %(no)s, %(cid)s, %(pid)s, %(oids)s,
                             %(idate)s, %(ddate)s, %(sub)s, %(tax)s, %(gnd)s,
                             'PENDING','UNPAID', %(by)s, %(rmk)s)
                        """, {
                            "id":    str(_u_dbg.uuid4()), "no": inv_no,
                            "cid":   sel_chs[0], "pid": party_id,
                            "oids":  all_order_nos,
                            "idate": date.today(),
                            "ddate": date.today() + timedelta(days=int(due_days)),
                            "sub":   inv_sub, "tax": inv_tax, "gnd": inv_gnd,
                            "by":    st.session_state.get("user_name","System"),
                            "rmk":   remarks,
                        })
                        st.success(f"✅ Invoice **{inv_no}** created (retry)")
                        for cid in sel_chs:
                            _write("UPDATE challans SET status='INVOICED',updated_at=NOW() WHERE id=%(i)s", {"i": cid})
                        st.rerun()
                    except Exception as _inv_dbg_e:
                        st.error(f"Invoice insert failed: {_inv_dbg_e}")
            except Exception as e:
                st.error(f"Invoice creation error: {e}")


# ══════════════════════════════════════════════════════════════════════════
# ANALYTICS
# ══════════════════════════════════════════════════════════════════════════

def render_analytics():
    _inject_css()
    st.markdown("### 📈 Billing Analytics")

    # ── KPIs ──────────────────────────────────────────────────────────────
    summary = _q("""
        SELECT
            COUNT(DISTINCT c.id) FILTER (WHERE c.status != 'CANCELLED') AS challan_count,
            COALESCE(SUM(c.grand_total) FILTER (WHERE c.status != 'CANCELLED'), 0) AS challan_value,
            COUNT(DISTINCT i.id) FILTER (WHERE i.status != 'CANCELLED') AS invoice_count,
            COALESCE(SUM(i.grand_total) FILTER (WHERE i.payment_status = 'UNPAID'), 0) AS outstanding
        FROM challans c
        FULL OUTER JOIN invoices i ON FALSE
    """)
    row = summary[0] if summary else {}

    st.markdown(f"""
    <div class='kpi-grid'>
      <div class='kpi' style='--accent:#3b82f6'><div class='kpi-label'>Total Challans</div><div class='kpi-value'>{row.get('challan_count',0)}</div></div>
      <div class='kpi' style='--accent:#38bdf8'><div class='kpi-label'>Challan Value</div><div class='kpi-value'>{_fmt_currency(row.get('challan_value',0))}</div></div>
      <div class='kpi' style='--accent:#10b981'><div class='kpi-label'>Total Invoices</div><div class='kpi-value'>{row.get('invoice_count',0)}</div></div>
      <div class='kpi' style='--accent:#ef4444'><div class='kpi-label'>Outstanding</div><div class='kpi-value'>{_fmt_currency(row.get('outstanding',0))}</div></div>
    </div>
    """, unsafe_allow_html=True)

    # ── Monthly revenue ───────────────────────────────────────────────────
    monthly = _q("""
        SELECT DATE_TRUNC('month', challan_date) AS month,
               COUNT(*) AS cnt, SUM(grand_total) AS val
        FROM challans WHERE status != 'CANCELLED'
          AND challan_date >= NOW() - INTERVAL '6 months'
        GROUP BY 1 ORDER BY 1
    """)

    if monthly:
        c1, c2 = st.columns(2)
        with c1:
            st.caption("Challans per month")
            st.bar_chart({r["month"].strftime("%b %y"): r["cnt"] for r in monthly})
        with c2:
            st.caption("Revenue per month (₹)")
            st.bar_chart({r["month"].strftime("%b %y"): float(r["val"] or 0) for r in monthly})

    # ── Top parties ───────────────────────────────────────────────────────
    top = _q("""
        SELECT p.party_name, COUNT(c.id) AS cnt, SUM(c.grand_total) AS rev
        FROM challans c JOIN parties p ON p.id = c.party_id
        WHERE c.status != 'CANCELLED'
        GROUP BY p.party_name ORDER BY rev DESC LIMIT 10
    """)
    if top:
        st.caption("Top parties by revenue")
        st.bar_chart({r["party_name"]: float(r["rev"] or 0) for r in top})

    # ── Payment status ────────────────────────────────────────────────────
    p1, p2 = st.columns(2)
    with p1:
        cs = _q("SELECT status, COUNT(*) AS n FROM challans GROUP BY status")
        if cs:
            st.caption("Challan status")
            st.bar_chart({r["status"]: r["n"] for r in cs})
    with p2:
        ps = _q("SELECT payment_status, COUNT(*) AS n FROM invoices GROUP BY payment_status")
        if ps:
            st.caption("Invoice payment status")
            st.bar_chart({r["payment_status"]: r["n"] for r in ps})


# ══════════════════════════════════════════════════════════════════════════
# BULK ACTIONS
# ══════════════════════════════════════════════════════════════════════════

def render_bulk_actions():
    _inject_css()
    st.markdown("### 🚀 Bulk Billing")
    st.caption("Select multiple orders → create challan or invoice in one click.")

    raw = _q(_billing_ready_orders_sql())

    # ── Billing gate classification ───────────────────────────────────────
    ready, blocked = [], []
    for o in raw:
        if o.get("status") == "READY_FOR_BILLING":
            o["billing_gate"]  = "READY"
            ready.append(o)
        else:
            ok, reason = _is_order_billing_ready(str(o["id"]))
            if ok:
                o["billing_gate"] = "READY"
                ready.append(o)
            else:
                o["billing_gate"]  = "BLOCKED"
                o["block_reason"]  = reason
                blocked.append(o)

    # ── Blocked panel ─────────────────────────────────────────────────────
    if blocked:
        with st.expander(f"⛔ {len(blocked)} orders blocked by pipeline"):
            for bo in blocked:
                st.caption(
                    f"🔴 **{bo.get('order_no')}**  ·  {bo.get('party_name')}  "
                    f"·  {bo.get('block_reason', 'Pipeline incomplete')}"
                )

    if not ready:
        st.info("No orders ready for bulk billing.")
        return

    # Party filter
    all_parties = sorted({(o["party_id"], o["party_name"]) for o in ready}, key=lambda x: x[1])
    sel_party = st.selectbox("Filter by party",
                             options=[("ALL","All Parties")] + all_parties,
                             format_func=lambda x: x[1], key="bulk_party_filter")

    filtered = ready if sel_party[0] == "ALL" else [o for o in ready if o["party_id"] == sel_party[0]]

    # ── Order options with gate icons ─────────────────────────────────────
    o_opts = {}
    for o in filtered:
        gate_icon = "🟢" if o.get("billing_gate") == "READY" else "🔴"
        reason    = f" | {o['block_reason']}" if o.get("billing_gate") == "BLOCKED" else ""
        o_opts[o["id"]] = (
            f"{gate_icon}  {o['order_no']}  ·  {o['party_name']}"
            f"  ·  {_fmt_currency(o.get('total_value', 0))}{reason}"
        )

    sel_ords = st.multiselect("Select orders", options=list(o_opts.keys()),
                              format_func=lambda x: o_opts.get(x,""), key="bulk_order_sel")

    if not sel_ords:
        return

    sel_details = [o for o in filtered if o["id"] in sel_ords]
    tots = _calc_billing_totals(sel_ords)
    parties_in_sel = {o["party_id"] for o in sel_details}

    st.markdown(f"""
    <div class='kpi-grid'>
      <div class='kpi' style='--accent:#3b82f6'><div class='kpi-label'>Orders</div><div class='kpi-value'>{len(sel_ords)}</div></div>
      <div class='kpi' style='--accent:#f59e0b'><div class='kpi-label'>Parties</div><div class='kpi-value'>{len(parties_in_sel)}</div></div>
      <div class='kpi' style='--accent:#10b981'><div class='kpi-label'>Grand Total</div><div class='kpi-value'>{_fmt_currency(tots['grand_total'])}</div></div>
    </div>
    """, unsafe_allow_html=True)

    # ── Price integrity warning ───────────────────────────────────────────
    _bad_price_lines = [r for r in tots.get("line_rows", []) if r.get("price_flag")]
    if _bad_price_lines:
        _bad_names = list({r.get("product_name","?") for r in _bad_price_lines})
        _bad_msg = "⚠️ Wholesale Price Issue Detected — Products using MRP instead of trade price: " + ", ".join(_bad_names) + ". Set selling_price in Product Master, then re-punch the order."
        st.error(_bad_msg)

    if len(parties_in_sel) > 1:
        st.warning("⚠️ Multiple parties selected. A separate challan will be created for each party.")

    remarks = st.text_area("Remarks (applied to all challans)", key="bulk_rmk", placeholder="Optional…")

    if st.button("📋 Create Challan(s)", type="primary", use_container_width=True, key="bulk_create"):
        created = []
        for pid in parties_in_sel:
            party_orders = [o for o in sel_details if o["party_id"] == pid]
            party_ids    = [o["id"] for o in party_orders]
            order_nos    = [o["order_no"] for o in party_orders]
            p_tots       = _calc_billing_totals(party_ids)
            try:
                ch_no = _alloc_ch_no()
                # Generate UUID here so we can reuse it for challan_lines
                # without a second SELECT (perf fix #4)
                ch_id = str(_uuid.uuid4())
                ok = _write("""
                    INSERT INTO challans
                    (id, challan_no, party_id, order_ids, challan_date,
                     total_amount, total_tax, grand_total, status, created_by, remarks)
                    VALUES
                    (%(id)s, %(no)s, %(pid)s, %(oids)s, %(dt)s,
                     %(sub)s, %(tax)s, %(gnd)s, 'PENDING', %(by)s, %(rmk)s)
                """, {
                    "id": ch_id, "no": ch_no, "pid": pid,
                    "oids": order_nos, "dt": date.today(),
                    "sub": p_tots["subtotal"], "tax": p_tots["gst_total"],
                    "gnd": p_tots["grand_total"],
                    "by":  st.session_state.get("user_name","System"),
                    "rmk": remarks,
                })
                if ok:
                    created.append(ch_no)
                    # Write challan_lines snapshot — ch_id reused, no extra SELECT
                    # Fetch all lines for this party's orders in one query (perf fix)
                    _bulk_lines_all = _q("""
                        SELECT ol.id,
                               ol.order_id,
                               COALESCE(p.product_name, ol.product_name, 'Service') AS product_name,
                               COALESCE(p.brand, ol.brand)               AS brand,
                               COALESCE(ol.billing_qty, ol.quantity)     AS quantity,
                               ol.unit_price,
                               COALESCE(ol.billing_total, ol.total_price) AS total_price,
                               ol.gst_percent,
                               COALESCE(ol.gst_amount, 0)                AS gst_amount,
                               ol.sph, ol.cyl, ol.axis, ol.add_power,
                               ol.eye_side,
                               COALESCE(ol.is_service_line, FALSE)       AS is_service_line
                        FROM order_lines ol
                        LEFT JOIN products p ON p.id = ol.product_id
                        WHERE ol.order_id = ANY(%(oids)s::uuid[])
                          AND COALESCE(ol.is_deleted,FALSE) = FALSE
                        ORDER BY ol.is_service_line, ol.id
                    """, {"oids": party_ids})
                    for _bl in _bulk_lines_all:
                            line_qty   = int(_bl.get("quantity") or 0)
                            base_price = float(_bl.get("total_price") or 0)
                            gst_amt    = float(_bl.get("gst_amount")  or 0)
                            line_total = round(base_price + gst_amt, 2)
                            _write("""
                                INSERT INTO challan_lines
                                (challan_id, order_id, order_line_id,
                                 product_name, brand, eye_side,
                                 quantity, unit_price, total_price,
                                 gst_percent, gst_amount, line_total,
                                 sph, cyl, axis, add_power)
                                VALUES (%(cid)s, %(oid)s::uuid, %(olid)s::uuid,
                                        %(pn)s, %(br)s, %(es)s,
                                        %(qty)s, %(up)s, %(tp)s,
                                        %(gp)s, %(ga)s, %(lt)s,
                                        %(sph)s, %(cyl)s, %(axis)s, %(add)s)
                                ON CONFLICT (challan_id, order_line_id) DO NOTHING
                            """, {
                                "cid": ch_id, "oid": _bl["order_id"], "olid": _bl["id"],
                                "pn": _bl.get("product_name") or "",
                                "br": _bl.get("brand") or "",
                                "es": str(_bl.get("eye_side") or "")[:1] or None,
                                "qty": line_qty,
                                "up":  float(_bl.get("unit_price") or 0),
                                "tp":  base_price,
                                "gp":  float(_bl.get("gst_percent") or 0),
                                "ga":  gst_amt,
                                "lt":  line_total,
                                "sph": _bl.get("sph"), "cyl": _bl.get("cyl"),
                                "axis": _bl.get("axis"), "add": _bl.get("add_power"),
                            })
                            # Over-billing guard: use RETURNING to detect 0-row updates.
                            # _write() only returns True/False — cannot detect blocked updates.
                            # Service lines: billed_qty guard uses allocated_qty as ceiling
                            # (SERVICE lines have allocated_qty = quantity set at save time)
                            _is_svc_bl = bool(_bl.get("is_service_line")) or                                          str(_bl.get("eye_side","")).upper() in ("S","SERVICE")
                            _guard_col = "COALESCE(allocated_qty, quantity)" if _is_svc_bl else "quantity"
                            guard = _q(f"""
                                UPDATE order_lines
                                SET billed_qty = COALESCE(billed_qty, 0) + %(qty)s
                                WHERE id = %(line_id)s::uuid
                                  AND COALESCE(billed_qty, 0) + %(qty)s <= {_guard_col}
                                RETURNING id
                            """, {"qty": line_qty, "line_id": _bl["id"]})
                            if not guard:
                                # Skip over-billed line silently (already billed elsewhere)
                                import streamlit as _st_ob
                                _st_ob.warning(
                                    f"⚠️ Line {_bl.get('product_name','')} "
                                    f"(eye: {_bl.get('eye_side','')}) — "
                                    f"already billed or over-billing prevented. Skipped."
                                )
                    for oid in party_ids:
                        _write("UPDATE orders SET status='IN_CHALLAN',updated_at=NOW() WHERE id=%(i)s::uuid", {"i": oid})
            except Exception as e:
                st.error(f"Failed for party {pid}: {e}")

        if created:
            st.success(f"✅ Created: {', '.join(created)}")
            try:
                from modules.wa_hub import wa_panel, wa_challan_made, _shop
                _s = _shop()
                for _cno in created:
                    _ch_row = _q("SELECT challan_no, grand_total, order_ids FROM challans WHERE challan_no=%s LIMIT 1", (_cno,))
                    if _ch_row:
                        _cr = _ch_row[0]
                        _party_mob = ""
                        try:
                            _oids = _cr.get("order_ids") or []
                            if _oids:
                                _orow = _q("SELECT party_name, patient_name, patient_mobile FROM orders WHERE order_no=%s LIMIT 1", (_oids[0],))
                                if _orow:
                                    _party_mob = str(_orow[0].get("patient_mobile") or "")
                        except Exception:
                            pass
                        _msg_ch = wa_challan_made(
                            party=str(sel_details[0].get("party_name","") if sel_details else ""),
                            order_no=", ".join(order_nos[:2]),
                            challan_no=_cno,
                            grand_total=float(_cr.get("grand_total") or 0),
                            shop_name=_s.get("shop_name","DV Optical"),
                            phone=_s.get("shop_phone",""),
                        )
                        wa_panel(_party_mob, _msg_ch,
                                 key="ch_created_wa_" + _cno.replace("/","_"),
                                 title="📲 Send Challan WhatsApp — " + _cno,
                                 expanded=True)
            except Exception:
                pass
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════

def render_challan_invoice_dashboard():
    _inject_css()

    # ── Header KPIs ───────────────────────────────────────────────────────
    stats = _q("""
        SELECT
            COUNT(*) FILTER (WHERE c.status != 'CANCELLED') AS ch_total,
            COUNT(*) FILTER (WHERE c.status = 'PENDING')    AS ch_pending,
            COALESCE(SUM(c.grand_total) FILTER (WHERE c.status = 'PENDING'), 0) AS ch_pending_val,
            0 AS inv_total, 0 AS inv_unpaid, 0.0 AS inv_outstanding
        FROM challans c
    """)
    istats = _q("""
        SELECT COUNT(*) AS inv_total,
               COUNT(*) FILTER (WHERE payment_status='UNPAID') AS inv_unpaid,
               COALESCE(SUM(grand_total) FILTER (WHERE payment_status='UNPAID'), 0) AS outstanding
        FROM invoices WHERE status != 'CANCELLED'
    """)
    s  = stats[0]  if stats  else {}
    si = istats[0] if istats else {}

    st.markdown(f"""
    <div class='kpi-grid'>
      <div class='kpi' style='--accent:#3b82f6'>
        <div class='kpi-label'>Challans — Pending</div>
        <div class='kpi-value'>{s.get('ch_pending',0)}</div>
        <div class='kpi-sub'>{_fmt_currency(s.get('ch_pending_val',0))} pending value</div>
      </div>
      <div class='kpi' style='--accent:#38bdf8'>
        <div class='kpi-label'>Total Challans</div>
        <div class='kpi-value'>{s.get('ch_total',0)}</div>
      </div>
      <div class='kpi' style='--accent:#ef4444'>
        <div class='kpi-label'>Invoices — Unpaid</div>
        <div class='kpi-value'>{si.get('inv_unpaid',0)}</div>
        <div class='kpi-sub'>{_fmt_currency(si.get('outstanding',0))} outstanding</div>
      </div>
      <div class='kpi' style='--accent:#10b981'>
        <div class='kpi-label'>Total Invoices</div>
        <div class='kpi-value'>{si.get('inv_total',0)}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Navigation tabs ───────────────────────────────────────────────────
    tab_ch, tab_inv, tab_new_ch, tab_new_inv, tab_pay, tab_bulk, tab_analytics = st.tabs([
        "📋 Challans", "🧾 Invoices",
        "➕ Create Challan", "➕ Create Invoice",
        "💰 Payments", "🚀 Bulk Actions", "📈 Analytics"
    ])

    with tab_ch:      render_challans_list()
    with tab_inv:     render_invoices_list()
    with tab_new_ch:  render_challan_creation()
    with tab_new_inv: render_invoice_creation()
    with tab_pay:
        try:
            from modules.billing.payment_manager import render_payments_dashboard
            render_payments_dashboard()
        except Exception as _e:
            st.error(f"Payment module error: {_e}")
            import traceback; st.code(traceback.format_exc())
    with tab_bulk:    render_bulk_actions()
    with tab_analytics: render_analytics()

    # Print preview
    if st.session_state.get("show_print_preview"):
        st.markdown("---")
        try:
            from modules.billing.document_templates import render_print_preview
            render_print_preview(
                st.session_state.print_document_type,
                st.session_state.print_document_no
            )
            if st.button("🔙 Back"):
                for k in ("show_print_preview","print_document_type","print_document_no"):
                    st.session_state.pop(k, None)
                st.rerun()
        except ImportError:
            st.error("Print templates not available")


if __name__ == "__main__":
    render_challan_invoice_dashboard()
