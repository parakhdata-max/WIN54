"""
dispatch_panel.py
=================
Dispatch UI for billed orders.

RULES:
  1. HARD GATE: No dispatch without at least one active challan or invoice.
     Gate is enforced by logistics_manager.billing_gate_check().
  2. Partial dispatch is supported — operator selects qty per line.
     Order stays DISPATCHED; remaining qty tracked in order_dispatch_lines.
  3. Logistics route is captured (carrier / route_code) — full UI stub.
     Route design window will be added later (placeholder exists).
  4. Order status transitions:
       BILLED → DISPATCHED  (on first dispatch, partial or full)
       DISPATCHED → DELIVERED  (on delivery confirmation when all lines done)

USED BY:
  billing_status_ui.py  — shown after billing documents confirmed
  order_status_window.py — shown in Update Status tab
"""

import streamlit as st
import datetime
import re
from typing import Dict, List

from .logistics_manager import (
    LogisticsRoute,
    billing_gate_check,
    get_billed_lines_for_order,
    get_dispatch_history,
    get_dispatch_summary,
    create_dispatch_event,
    confirm_delivery,
)

try:
    from modules.backoffice.service_master import ensure_service_schema
except Exception:
    ensure_service_schema = None



def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}"); return []


def _rw(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("[dispatch_rw] %s", e)
        return False


def _scan_norm(value: str) -> str:
    s = "".join(ch for ch in str(value or "") if ch.isalnum()).lower()
    if s.startswith("o") and len(s) > 1:
        s = s[1:]
    return s


def _scan_match(needle: str, *hay_values) -> bool:
    raw = str(needle or "").strip().lower()
    norm = _scan_norm(raw)
    if not raw:
        return True
    for value in hay_values:
        text = str(value or "").lower()
        if raw in text:
            return True
        hnorm = _scan_norm(value)
        if norm and (norm in hnorm or hnorm in norm):
            return True
    return False


def _ensure_dispatch_service_schema() -> None:
    try:
        if ensure_service_schema:
            ensure_service_schema()
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_provider_id UUID")
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_charge NUMERIC(12,2) DEFAULT 0")
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_rate_option_id UUID")
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_rate_option_label TEXT")
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_parcel_size TEXT")
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_gst_percent NUMERIC(5,2) DEFAULT 0")
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_gst_amount NUMERIC(12,2) DEFAULT 0")
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_total_amount NUMERIC(12,2) DEFAULT 0")
        _rw("ALTER TABLE order_dispatches ADD COLUMN IF NOT EXISTS courier_billed BOOLEAN DEFAULT FALSE")
        _rw("""
            CREATE TABLE IF NOT EXISTS dispatch_courier_costs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                order_id UUID,
                order_no TEXT,
                challan_id UUID,
                invoice_id UUID,
                courier_provider_id UUID,
                courier_provider_name TEXT,
                courier_rate_option_id UUID,
                courier_rate_option_label TEXT,
                courier_parcel_size TEXT,
                tracking_no TEXT,
                dispatch_date DATE,
                charge_base NUMERIC(12,2) DEFAULT 0,
                gst_percent NUMERIC(5,2) DEFAULT 0,
                gst_amount NUMERIC(12,2) DEFAULT 0,
                total_amount NUMERIC(12,2) DEFAULT 0,
                billing_added BOOLEAN DEFAULT FALSE,
                payout_status TEXT DEFAULT 'PENDING',
                remarks TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        _rw("ALTER TABLE dispatch_courier_costs ADD COLUMN IF NOT EXISTS courier_rate_option_id UUID")
        _rw("ALTER TABLE dispatch_courier_costs ADD COLUMN IF NOT EXISTS courier_rate_option_label TEXT")
        _rw("ALTER TABLE dispatch_courier_costs ADD COLUMN IF NOT EXISTS courier_parcel_size TEXT")
    except Exception:
        pass


def _order_billing_coverage(order_id: str) -> Dict:
    """Return order-level billing coverage across all active challans/invoices."""
    rows = _q("""
        WITH billable AS (
            SELECT
                ol.id AS line_id,
                GREATEST(COALESCE(ol.billing_qty, ol.quantity, 0), 0)::numeric AS required_qty
            FROM order_lines ol
            WHERE ol.order_id = %(oid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND GREATEST(COALESCE(ol.billing_qty, ol.quantity, 0), 0) > 0
              AND (
                    COALESCE(ol.total_price, 0) > 0
                 OR COALESCE(ol.unit_price, 0) > 0
              )
        ),
        covered_raw AS (
            SELECT il.order_line_id AS line_id, COALESCE(il.quantity, 0)::numeric AS qty
            FROM invoice_lines il
            JOIN invoices inv ON inv.id = il.invoice_id
            WHERE il.order_line_id IN (SELECT line_id FROM billable)
              AND COALESCE(inv.status, '') NOT IN ('CANCELLED','VOID','DELETED')
              AND COALESCE(il.quantity, 0) > 0

            UNION ALL

            SELECT cl.order_line_id AS line_id, COALESCE(cl.quantity, 0)::numeric AS qty
            FROM challan_lines cl
            JOIN challans c ON c.id = cl.challan_id
            WHERE cl.order_line_id IN (SELECT line_id FROM billable)
              AND COALESCE(c.status, '') NOT IN ('CANCELLED','VOID','DELETED')
              AND NOT COALESCE(cl.is_deleted, FALSE)
              AND COALESCE(cl.quantity, 0) > 0
        ),
        covered AS (
            SELECT
                b.line_id,
                b.required_qty,
                LEAST(b.required_qty, COALESCE(SUM(cr.qty), 0)) AS covered_qty
            FROM billable b
            LEFT JOIN covered_raw cr ON cr.line_id = b.line_id
            GROUP BY b.line_id, b.required_qty
        )
        SELECT
            COALESCE(SUM(required_qty), 0) AS order_required_qty,
            COALESCE(SUM(covered_qty), 0) AS order_covered_qty,
            COUNT(*) FILTER (WHERE covered_qty < required_qty) AS unbilled_line_count
        FROM covered
    """, {"oid": order_id}) or []
    row = rows[0] if rows else {}
    required = int(float(row.get("order_required_qty") or 0))
    covered = int(float(row.get("order_covered_qty") or 0))
    return {
        "order_required_qty": required,
        "order_covered_qty": covered,
        "unbilled_line_count": int(row.get("unbilled_line_count") or 0),
        "is_fully_billed": required > 0 and covered >= required,
    }


# ── Shared filter state key prefix ───────────────────────────────────────────
_FK = "dlp_"   # dispatch logistics page


def _build_dispatch_wa_message(
    party_name, order_no, carrier, tracking,
    items, route_code, order_type="",
    shop_name="DV Optical", shop_phone="",
):
    is_hand = (
        str(route_code or "").upper() in ("HAND","HAND_DELIVERY","IN_STORE")
        or str(order_type or "").upper() == "RETAIL"
        or str(carrier or "").upper() in ("HAND","HAND DELIVERY","STORE")
    )
    name_parts = str(party_name or "").split()
    salutation = "Mr/Ms " + (name_parts[0] if name_parts else str(party_name))
    contact = shop_phone or shop_name

    if is_hand:
        parts = [
            "Hi " + salutation + ",",
            "",
            "Your order *" + order_no + "* has been delivered by hand from our store. \U0001f64f",
            "",
            "Hope you are enjoying wearing your Specs / Contact Lenses. \U0001f453",
            "",
            "We are always here to help with any issue.",
            "Feel free to call or WhatsApp us: \U0001f4de *" + contact + "*",
            "",
            "Thank you for choosing *" + shop_name + "*! \U0001f60a",
        ]
    else:
        parts = [
            "Dear *" + party_name + "*,",
            "",
            "Your order *" + order_no + "* has been dispatched. \U0001f4e6",
            "",
        ]
        if items:
            parts.append("*Order Details:*")
            for item in items:
                parts.append("  \u2022 " + str(item))
            parts.append("")
        parts += [
            "\U0001f69a *Courier:* " + str(carrier),
        ]
        if tracking:
            parts.append("\U0001f522 *Tracking No:* " + str(tracking))
        parts += [
            "",
            "For any queries, contact us:",
            "\U0001f4de *" + contact + "*",
            "",
            "Thank you for shopping with *" + shop_name + "*!",
        ]
    return "\n".join(parts)


def _build_dispatch_items_text(dispatchable: list, line_qtys: dict) -> list:
    """
    Build human-readable item lines for WA message from dispatched lines.
    Format: Eye Product Brand · SPH +x.xx CYL -x.xx AX xxx · Index 1.56 · Coating
    """
    items = []
    for line in dispatchable:
        qty = int(line_qtys.get(str(line.get("id","") or ""), 0))
        if qty <= 0:
            continue
        eye    = str(line.get("eye_side") or "")
        pname  = str(line.get("product_name") or "Lens")
        brand  = str(line.get("brand") or "")
        sph    = line.get("sph")
        cyl    = line.get("cyl")
        axis   = line.get("axis")
        add    = line.get("add_power")
        idx    = line.get("lens_index") or ""
        coat   = line.get("coating") or ""
        colour = line.get("colour") or ""

        eye_lbl = {"R":"Right Eye","L":"Left Eye","S":"Service","BOTH":"Both Eyes"}.get(
            eye.upper(), eye)

        # Build power string
        pwr = ""
        if sph is not None:
            pwr = f"SPH {float(sph):+.2f}"
            if cyl is not None and abs(float(cyl)) > 0.01:
                pwr += f"  CYL {float(cyl):+.2f}"
            if axis and int(float(axis)) > 0:
                pwr += f"  AX {int(float(axis))}"
            if add is not None and abs(float(add)) > 0.01:
                pwr += f"  ADD {float(add):+.2f}"
        if colour:
            pwr = colour  # for colour CL, show colour instead of power

        # Build spec string
        spec_parts = []
        if idx:   spec_parts.append(f"Index {idx}")
        if coat:  spec_parts.append(coat)
        spec = "  ·  ".join(spec_parts)

        line_text = f"{eye_lbl}: {pname}"
        if brand and brand.lower() not in pname.lower():
            line_text += f" ({brand})"
        if pwr:
            line_text += "\n    " + pwr
        if spec:
            line_text += "\n    " + spec
        items.append(line_text)
    return items



# ═══════════════════════════════════════════════════════════════════════════════
# COURIER AUDIT TAB
# ═══════════════════════════════════════════════════════════════════════════════

def _issue_cn_inline(dispatch_no: str, order_id: str, order_no: str, party_name: str) -> None:
    """Inline credit note form for duplicate courier charges."""
    with st.container(border=True):
        st.markdown("#### 📋 Issue Credit Note for Courier Charge")
        _dcc = _q("""
            SELECT id::text, courier_provider_name, total_amount, gst_amount,
                   charge_base, gst_percent
            FROM dispatch_courier_costs
            WHERE order_id = %(oid)s::uuid AND billing_added = TRUE
            ORDER BY created_at DESC LIMIT 5
        """, {"oid": order_id}) or []
        if not _dcc:
            st.info("No invoiced courier charges found for this order.")
            return
        labels = [
            f"{d.get('courier_provider_name','Courier')} — ₹{float(d.get('total_amount',0)):,.2f}"
            for d in _dcc
        ]
        sel = st.selectbox("Select courier charge to credit", range(len(labels)),
                            format_func=lambda i: labels[i],
                            key=f"cn_sel_{dispatch_no}")
        reason = st.text_input("Reason for credit note",
                                value="Duplicate courier charge",
                                key=f"cn_reason_{dispatch_no}")
        if st.button("✅ Issue Credit Note", key=f"cn_issue_{dispatch_no}", type="primary"):
            try:
                ch = _dcc[int(sel)]
                ok, msg = _issue_courier_credit_note(
                    ch, order_id=order_id, created_by="dispatch_history"
                )
                if ok:
                    st.success(f"✅ Credit note issued: {msg}")
                    st.session_state.pop(f"show_cn_{dispatch_no}", None)
                    st.rerun()
                else:
                    st.error(msg)
            except Exception as e:
                st.error(f"Credit note failed: {e}")
        if st.button("✕ Cancel", key=f"cn_cancel_{dispatch_no}"):
            st.session_state.pop(f"show_cn_{dispatch_no}", None)
            st.rerun()


def _render_courier_audit() -> None:
    """
    Courier Audit tab — shows dispatches with and without courier charges.
    Allows raising courier invoices or credit notes in bulk.
    """
    import datetime as _cadt

    st.markdown("### 🧾 Courier Charges Audit")
    st.caption("Dispatches grouped by courier billing status. Raise missing invoices or credit duplicates.")

    with st.expander("🔍 Filters", expanded=True):
        ca1, ca2, ca3 = st.columns(3)
        today   = _cadt.date.today()
        ca_from = ca1.date_input("From", value=today - _cadt.timedelta(days=30), key="ca_from")
        ca_to   = ca2.date_input("To",   value=today, key="ca_to")
        ca_search = ca3.text_input("Party / Order", key="ca_search", placeholder="Search…")

    # ── Main audit query ──────────────────────────────────────────────────────
    rows = _q("""
        SELECT
            d.id::text            AS dispatch_id,
            d.dispatch_no,
            d.carrier_name,
            d.tracking_no,
            d.dispatched_at::text AS dispatched_at,
            d.order_id::text      AS order_id,
            o.order_no,
            COALESCE(o.party_name, o.patient_name, '—') AS party_name,
            COALESCE(dcc.total_amount, 0)::numeric       AS courier_charge,
            COALESCE(dcc.gst_amount, 0)::numeric         AS courier_gst,
            COALESCE(dcc.billing_added, FALSE)           AS invoiced,
            COALESCE(dcc.courier_provider_name, '')      AS provider_name,
            dcc.id::text                                 AS dcc_id
        FROM order_dispatches d
        JOIN orders o ON o.id = d.order_id
        LEFT JOIN dispatch_courier_costs dcc ON dcc.order_id = d.order_id
            AND COALESCE(dcc.tracking_no,'') = COALESCE(d.tracking_no,'')
        WHERE d.dispatched_at BETWEEN %(df)s AND %(dt)s
          AND d.status != 'CANCELLED'
        ORDER BY d.dispatched_at DESC
        LIMIT 300
    """, {
        "df": ca_from.isoformat(),
        "dt": ca_to.isoformat(),
    }) or []

    # Filter
    if ca_search.strip():
        rows = [r for r in rows
                if _scan_match(ca_search, r.get("order_no"), r.get("party_name"), r.get("dispatch_no"))]

    if not rows:
        st.info("No dispatches found for the selected filters.")
        return

    courier_rows  = [r for r in rows if float(r.get("courier_charge") or 0) > 0]
    no_courier    = [r for r in rows if not (float(r.get("courier_charge") or 0) > 0)]
    invoiced      = [r for r in courier_rows if r.get("invoiced")]
    not_invoiced  = [r for r in courier_rows if not r.get("invoiced")]

    # ── Summary metrics ────────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Dispatches",     len(rows))
    m2.metric("With Courier Charge",  len(courier_rows))
    m3.metric("✅ Invoiced",           len(invoiced),
              delta=f"₹{sum(float(r.get('courier_charge',0)) for r in invoiced):,.0f}")
    m4.metric("⚠️ Not Invoiced",       len(not_invoiced),
              delta=f"₹{sum(float(r.get('courier_charge',0)) for r in not_invoiced):,.0f}",
              delta_color="inverse")

    # ── Sub-tabs ──────────────────────────────────────────────────────────────
    sub1, sub2, sub3 = st.tabs([
        f"⚠️ Pending ({len(not_invoiced)})",
        f"✅ Invoiced ({len(invoiced)})",
        f"📦 No Courier ({len(no_courier)})",
    ])

    with sub1:
        st.markdown("#### ⚠️ Courier charged but invoice not raised")
        if not not_invoiced:
            st.success("All courier charges are invoiced. Nothing pending.")
        else:
            # Bulk raise option
            if st.button(f"📄 Raise All {len(not_invoiced)} Pending Courier Invoices",
                         key="ca_raise_all", type="primary",
                         disabled=len(not_invoiced) == 0):
                raised = 0
                for r in not_invoiced:
                    try:
                        _dcc_rows = _q("""
                            SELECT courier_provider_name, tracking_no,
                                   charge_base, gst_percent, gst_amount, total_amount
                            FROM dispatch_courier_costs WHERE id=%(id)s::uuid LIMIT 1
                        """, {"id": r.get("dcc_id","")}) or []
                        if _dcc_rows:
                            d = _dcc_rows[0]
                            _create_courier_service_invoice(
                                order_id=r["order_id"], order_no=r["order_no"],
                                party_id="", party_name=r["party_name"],
                                carrier_name=str(d.get("courier_provider_name") or r.get("carrier_name","")),
                                tracking_no=str(d.get("tracking_no") or r.get("tracking_no","")),
                                base_amount=float(d.get("charge_base") or 0),
                                gst_percent=float(d.get("gst_percent") or 0),
                                gst_amount=float(d.get("gst_amount") or 0),
                                total_amount=float(d.get("total_amount") or 0),
                                created_by="courier_audit",
                            )
                            _rw("UPDATE dispatch_courier_costs SET billing_added=TRUE WHERE id=%(id)s::uuid",
                                {"id": r.get("dcc_id","")})
                            raised += 1
                    except Exception:
                        pass
                st.success(f"✅ {raised} courier invoice(s) raised.")
                st.rerun()

            st.markdown("---")
            for r in not_invoiced:
                _render_courier_audit_row(r, mode="pending")

    with sub2:
        st.markdown("#### ✅ Courier charges invoiced")
        if not invoiced:
            st.info("No invoiced courier charges in this period.")
        for r in invoiced:
            _render_courier_audit_row(r, mode="invoiced")

    with sub3:
        st.markdown("#### 📦 Dispatches with no courier charge recorded")
        st.caption("These are hand/local deliveries or orders where courier charge was not added at dispatch time.")
        if not no_courier:
            st.info("All dispatches in this period have courier charges.")
        for r in no_courier:
            carrier = r.get("carrier_name") or "—"
            is_hand = any(h in carrier.upper() for h in ("HAND","HOME","LOCAL","STORE","PORTER"))
            if is_hand:
                continue  # skip hand deliveries — expected to have no courier
            with st.container(border=False):
                st.markdown(
                    f"<div style='border-left:3px solid #334155;padding:4px 10px;margin:2px 0;"
                    f"font-size:0.78rem;color:#94a3b8'>"
                    f"📦 {r.get('order_no')}  ·  {r.get('party_name')}  ·  "
                    f"🚚 {carrier}  ·  {str(r.get('dispatched_at',''))[:10]}"
                    f"</div>",
                    unsafe_allow_html=True,
                )


def _render_courier_audit_row(r: Dict, mode: str = "pending") -> None:
    order_no  = r.get("order_no") or "—"
    party     = r.get("party_name") or "—"
    carrier   = r.get("provider_name") or r.get("carrier_name") or "—"
    tracking  = r.get("tracking_no") or ""
    amount    = float(r.get("courier_charge") or 0)
    dis_at    = str(r.get("dispatched_at") or "")[:10]
    order_id  = r.get("order_id") or ""
    dcc_id    = r.get("dcc_id") or ""
    disp_no   = r.get("dispatch_no") or ""

    with st.container(border=True):
        row1, row2 = st.columns([6, 3])
        with row1:
            st.markdown(
                f"<div style='font-size:0.82rem;color:#e2e8f0;font-weight:600'>"
                f"📦 {order_no}  <span style='color:#94a3b8;font-weight:400'>{party}</span>"
                f"</div>"
                f"<div style='font-size:0.70rem;color:#64748b'>"
                f"🚚 {carrier}"
                f"{'  ·  🔢 ' + tracking if tracking else ''}"
                f"  ·  {dis_at}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with row2:
            if mode == "pending":
                if st.button(f"📄 Raise ₹{amount:,.0f}",
                             key=f"ca_raise_{disp_no}",
                             type="primary", use_container_width=True):
                    try:
                        _dcc_rows = _q(
                            "SELECT * FROM dispatch_courier_costs WHERE id=%(id)s::uuid LIMIT 1",
                            {"id": dcc_id}
                        ) or []
                        if _dcc_rows:
                            d = _dcc_rows[0]
                            _inv = _create_courier_service_invoice(
                                order_id=order_id, order_no=order_no,
                                party_id="", party_name=party,
                                carrier_name=str(d.get("courier_provider_name") or carrier),
                                tracking_no=str(d.get("tracking_no") or tracking),
                                base_amount=float(d.get("charge_base") or 0),
                                gst_percent=float(d.get("gst_percent") or 0),
                                gst_amount=float(d.get("gst_amount") or 0),
                                total_amount=float(d.get("total_amount") or 0),
                                created_by="courier_audit",
                            )
                            _rw("UPDATE dispatch_courier_costs SET billing_added=TRUE WHERE id=%(id)s::uuid",
                                {"id": dcc_id})
                            st.success(f"✅ {_inv}")
                            st.rerun()
                    except Exception as e:
                        st.error(str(e))
            else:
                if st.button("📋 Credit Note", key=f"ca_cn_{disp_no}",
                             use_container_width=True):
                    st.session_state[f"show_cn_{disp_no}"] = True
                if st.session_state.get(f"show_cn_{disp_no}"):
                    _issue_cn_inline(disp_no, order_id, order_no, party)


def render_dispatch_queue_tab() -> None:
    """
    Main entry — full-page Dispatch & Logistics centre.
    Called from app.py sidebar (Dispatch page) and from
    production_page.py (🚚 Dispatch radio tab).
    """
    _ensure_dispatch_service_schema()
    st.markdown("## 🚚 Dispatch & Logistics")
    st.caption(
        "Manage order dispatch, track shipments, confirm delivery, "
        "and print address stickers."
    )

    scan_tab, delivery_scan_tab, main_tab, history_tab, tracking_tab, courier_tab = st.tabs([
        "⚡ Scan Dispatch",
        "✅ Scan Delivery",
        "📦 Dispatch Queue",
        "📋 Dispatch History",
        "🔍 Track Shipment",
        "🧾 Courier Audit",
    ])

    with scan_tab:
        _render_scan_dispatch_tab()

    with delivery_scan_tab:
        _render_scan_delivery_tab()

    with main_tab:
        _render_queue()

    with history_tab:
        _render_history()

    with tracking_tab:
        _render_tracking()

    with courier_tab:
        _render_courier_audit()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. DISPATCH QUEUE
# ═══════════════════════════════════════════════════════════════════════════════

def _scan_norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _scan_tokens(raw: str) -> list:
    return [p.strip() for p in re.split(r"[\s,;\n\r\t]+", str(raw or "")) if p.strip()]


def _party_scan_match(scan: str, row: Dict) -> bool:
    n = _scan_norm(scan)
    return bool(n) and any(
        n in _scan_norm(row.get(k))
        for k in ("party_name", "mobile", "party_id")
    )


def _order_scan_match(token: str, row: Dict) -> bool:
    n = _scan_norm(token)
    if not n:
        return False
    fields = [
        row.get("order_no"),
        row.get("challan_no"),
        row.get("invoice_no"),
        row.get("dispatch_key"),
    ]
    if any(n == _scan_norm(v) or n in _scan_norm(v) for v in fields):
        return True
    m = re.search(r"(\d{1,5})(?:\s*[-_/]?\s*(FIT|FITTING|COL|COLOUR|COLOR|COLOURING|C))?$", str(token or ""), re.I)
    if not m:
        return False
    num = m.group(1).zfill(4)
    suffix = (m.group(2) or "").upper()
    ono = str(row.get("order_no") or "").upper()
    if f"/{num}" not in ono and not ono.endswith(num) and num not in ono:
        return False
    if suffix.startswith("FIT"):
        return ono.endswith("-F") or "FIT" in ono
    if suffix.startswith(("COL", "COLOUR", "COLOR")) or suffix == "C":
        return ono.endswith("-C") or "COL" in ono
    return True


def _pending_dispatch_docs_for_scan() -> list:
    rows = _q("""
        WITH dispatch_docs AS (
            SELECT o.id AS order_id, inv.id AS invoice_id, inv.challan_id AS challan_id,
                   COALESCE(c.challan_no, '')::text AS challan_no,
                   inv.invoice_no::text AS invoice_no, inv.created_at
            FROM orders o
            JOIN invoices inv
              ON inv.order_ids::text[] @> ARRAY[o.id::text]
             AND inv.status NOT IN ('CANCELLED','VOID')
            LEFT JOIN challans c ON c.id = inv.challan_id
            WHERE o.status IN ('BILLED','CHALLANED','INVOICED','INVOICED_BILLED',
                               'READY_TO_DISPATCH','CHALLAN_ONLY','DISPATCHED')

            UNION ALL

            SELECT o.id AS order_id, NULL::uuid AS invoice_id, c.id AS challan_id,
                   c.challan_no::text AS challan_no, ''::text AS invoice_no, c.created_at
            FROM orders o
            JOIN challans c
              ON c.order_ids::text[] @> ARRAY[o.id::text]
             AND c.status NOT IN ('CANCELLED','VOID')
            WHERE o.status IN ('BILLED','CHALLANED','INVOICED','INVOICED_BILLED',
                               'READY_TO_DISPATCH','CHALLAN_ONLY','DISPATCHED')
              AND NOT EXISTS (
                  SELECT 1 FROM invoices inv2
                  WHERE inv2.status NOT IN ('CANCELLED','VOID')
                    AND inv2.order_ids::text[] @> ARRAY[o.id::text]
                    AND inv2.challan_id = c.id
              )
        ),
        doc_lines AS (
            SELECT dd.*, il.order_line_id, COALESCE(il.quantity,0)::numeric AS billed_qty
            FROM dispatch_docs dd
            JOIN invoice_lines il ON il.invoice_id = dd.invoice_id
             AND il.order_line_id IS NOT NULL AND COALESCE(il.quantity,0) > 0

            UNION ALL

            SELECT dd.*, cl.order_line_id, COALESCE(cl.quantity,0)::numeric AS billed_qty
            FROM dispatch_docs dd
            JOIN challan_lines cl ON cl.challan_id = dd.challan_id
             AND dd.invoice_id IS NOT NULL
             AND dd.challan_id IS NOT NULL
             AND cl.order_line_id IS NOT NULL
             AND NOT COALESCE(cl.is_deleted,FALSE)
             AND COALESCE(cl.quantity,0) > 0
            WHERE NOT EXISTS (
                SELECT 1 FROM invoice_lines il
                WHERE il.invoice_id = dd.invoice_id
                  AND il.order_line_id IS NOT NULL
                  AND COALESCE(il.quantity,0) > 0
            )

            UNION ALL

            SELECT dd.*, cl.order_line_id, COALESCE(cl.quantity,0)::numeric AS billed_qty
            FROM dispatch_docs dd
            JOIN challan_lines cl ON cl.challan_id = dd.challan_id
             AND dd.invoice_id IS NULL
             AND cl.order_line_id IS NOT NULL
             AND NOT COALESCE(cl.is_deleted,FALSE)
             AND COALESCE(cl.quantity,0) > 0
        ),
        doc_totals AS (
            SELECT dl.order_id, dl.invoice_id, dl.challan_id, dl.challan_no, dl.invoice_no,
                   SUM(dl.billed_qty) AS total_billed,
                   COALESCE(SUM((
                       SELECT COALESCE(SUM(odl.dispatched_qty), 0)
                       FROM order_dispatch_lines odl
                       JOIN order_dispatches d ON d.id = odl.dispatch_id
                       WHERE d.order_id = dl.order_id
                         AND d.status != 'CANCELLED'
                         AND odl.order_line_id = dl.order_line_id
                   )), 0) AS total_dispatched
            FROM doc_lines dl
            GROUP BY dl.order_id, dl.invoice_id, dl.challan_id, dl.challan_no, dl.invoice_no
        )
        SELECT o.id::text AS order_id, o.order_no, o.status,
               o.party_id::text AS party_id,
               COALESCE(o.party_name, o.patient_name, '—') AS party_name,
               COALESCE(o.patient_mobile, '') AS mobile,
               COALESCE(p.address, '') AS address,
               COALESCE(p.preferred_courier_provider_id::text, '') AS preferred_courier_provider_id,
               COALESCE(p.preferred_courier_name, '') AS preferred_courier_name,
               COALESCE(dt.challan_no, '') AS challan_no,
               COALESCE(dt.invoice_no, '') AS invoice_no,
               dt.challan_id::text AS challan_id,
               dt.invoice_id::text AS invoice_id,
               COALESCE(dt.total_billed, 0) AS total_billed,
               COALESCE(dt.total_dispatched, 0) AS total_dispatched
        FROM doc_totals dt
        JOIN orders o ON o.id = dt.order_id
        LEFT JOIN parties p ON p.id = o.party_id
        WHERE COALESCE(dt.total_billed,0) > COALESCE(dt.total_dispatched,0)
        ORDER BY o.created_at DESC
        LIMIT 500
    """)
    merged = {}
    for row in rows or []:
        oid = str(row.get("order_id") or "")
        key = f"{oid}|INV|{row.get('invoice_no') or ''}" if row.get("invoice_no") else f"{oid}|CH|{row.get('challan_no') or ''}"
        if oid and key not in merged:
            r = dict(row)
            r["dispatch_key"] = key
            merged[key] = r
    return list(merged.values())


def _render_scan_dispatch_tab() -> None:
    st.markdown("### ⚡ Scan Dispatch")
    st.caption("Scan party, scan billed order/document refs, then save one dispatch note per order.")
    pending = _pending_dispatch_docs_for_scan()
    if not pending:
        st.info("No billed documents pending dispatch.")
        return

    p1, p2 = st.columns([2, 3])
    party_scan = p1.text_input("Scan / type party", key=f"{_FK}scan_party_text", placeholder="Party barcode / name / mobile")
    party_options = sorted({str(r.get("party_name") or "").strip() for r in pending if str(r.get("party_name") or "").strip()})
    matched = next((r.get("party_name") for r in pending if _party_scan_match(party_scan, r)), "")
    idx = party_options.index(matched) + 1 if matched in party_options else 0
    party = p2.selectbox("Party", [""] + party_options, index=idx, key=f"{_FK}scan_party")
    if not party:
        return

    party_docs = [r for r in pending if str(r.get("party_name") or "").strip() == party]
    st.caption(f"{len(party_docs)} pending dispatch document(s) for {party}.")
    raw = st.text_area(
        "Scan orders / invoice / challan",
        key=f"{_FK}scan_orders_text",
        height=110,
        placeholder="R-002-Fit  R002-Col  R-009  INV/2627/0010  CH/2627/0010",
    )
    tokens = _scan_tokens(raw)
    selected, missing, seen = [], [], set()
    for tok in tokens:
        match = next((r for r in party_docs if _order_scan_match(tok, r)), None)
        if not match:
            missing.append(tok)
            continue
        dkey = str(match.get("dispatch_key") or "")
        if dkey and dkey not in seen:
            seen.add(dkey)
            selected.append(match)

    if missing:
        st.error("Not pending dispatch for this party: " + ", ".join(missing))
    if selected:
        st.dataframe([
            {
                "Order": r.get("order_no"),
                "Challan": r.get("challan_no"),
                "Invoice": r.get("invoice_no"),
                "Remaining": int(float(r.get("total_billed") or 0) - float(r.get("total_dispatched") or 0)),
            }
            for r in selected
        ], use_container_width=True, hide_index=True)

    c1, c2, c3 = st.columns(3)
    carrier = c1.text_input("Carrier / Method", value="Hand Delivery", key=f"{_FK}scan_carrier")
    tracking = c2.text_input("Tracking / Note", key=f"{_FK}scan_tracking")
    dispatch_date = c3.date_input("Dispatch Date", value=datetime.date.today(), key=f"{_FK}scan_date")
    by = st.text_input("Dispatched By", value=st.session_state.get("user_name", ""), key=f"{_FK}scan_by")
    remarks = st.text_input("Remarks", key=f"{_FK}scan_remarks")

    if st.button("🚚 Save Scan Dispatch", type="primary", use_container_width=True,
                 key=f"{_FK}scan_go", disabled=not selected or bool(missing)):
        if not carrier.strip():
            st.error("Carrier / Method is required.")
            return
        if not by.strip():
            st.error("Dispatched By is required.")
            return
        successes, failures = 0, []
        grouped = {}
        for r in selected:
            grouped.setdefault(str(r.get("order_id") or ""), []).append(r)
        for oid, docs in grouped.items():
            first = docs[0]
            line_qtys, refs = {}, []
            for doc in docs:
                lines = _dispatch_doc_lines(oid, doc) or get_billed_lines_for_order(oid) or _fallback_billed_lines_for_order(oid)
                for line in lines:
                    lid = str(line.get("id") or "")
                    qty = int(line.get("remaining_qty") or 0)
                    if lid and qty > 0:
                        line_qtys[lid] = line_qtys.get(lid, 0) + qty
                ref = doc.get("invoice_no") or doc.get("challan_no") or ""
                if ref and ref not in refs:
                    refs.append(ref)
            if not line_qtys:
                failures.append(f"{first.get('order_no')}: no remaining billed lines")
                continue
            ok, msg = create_dispatch_event(
                order_id=oid,
                order_no=str(first.get("order_no") or ""),
                route_code="HAND" if "hand" in carrier.lower() else "COURIER",
                carrier_name=carrier.strip(),
                tracking_no=tracking.strip(),
                dispatched_by=by.strip(),
                dispatch_date=dispatch_date,
                line_qtys=line_qtys,
                billing_doc_ref=", ".join(refs),
                remarks=remarks.strip() or "Fast scan dispatch",
            )
            if ok:
                successes += 1
                if "hand" in carrier.lower():
                    _mark_latest_local_dispatch_delivered(
                        oid,
                        confirmed_by=by.strip(),
                        notes=tracking.strip() or "Fast scan hand dispatch",
                    )
            else:
                failures.append(f"{first.get('order_no')}: {msg}")
        if successes:
            st.success(f"Dispatch saved for {successes} order(s).")
        for f in failures:
            st.error(f)


def _pending_delivery_events_for_scan() -> list:
    rows = _q("""
        SELECT
            d.id::text AS dispatch_id,
            d.order_id::text AS order_id,
            d.dispatch_no,
            d.carrier_name,
            d.tracking_no,
            d.dispatched_at::text AS dispatched_at,
            COALESCE(o.order_no, '') AS order_no,
            COALESCE(o.party_name, o.patient_name, '—') AS party_name,
            COALESCE(o.patient_mobile, '') AS mobile,
            COALESCE(c.challan_no, '') AS challan_no,
            COALESCE(i.invoice_no, '') AS invoice_no,
            COALESCE(SUM(odl.dispatched_qty), 0) AS dispatched_qty
        FROM order_dispatches d
        JOIN orders o ON o.id = d.order_id
        LEFT JOIN order_dispatch_lines odl ON odl.dispatch_id = d.id
        LEFT JOIN challans c ON c.order_ids::text[] @> ARRAY[o.id::text]
          AND c.status NOT IN ('CANCELLED','VOID')
        LEFT JOIN invoices i ON i.order_ids::text[] @> ARRAY[o.id::text]
          AND i.status NOT IN ('CANCELLED','VOID')
        WHERE d.status = 'DISPATCHED'
        GROUP BY d.id, d.order_id, d.dispatch_no, d.carrier_name, d.tracking_no,
                 d.dispatched_at, o.order_no, o.party_name, o.patient_name,
                 o.patient_mobile, c.challan_no, i.invoice_no
        ORDER BY d.dispatched_at DESC, d.dispatch_no DESC
        LIMIT 500
    """) or []
    merged = {}
    for r in rows:
        dispatch_id = str(r.get("dispatch_id") or "")
        if dispatch_id and dispatch_id not in merged:
            merged[dispatch_id] = dict(r)
    return list(merged.values())


def _render_scan_delivery_tab() -> None:
    st.markdown("### ✅ Scan Delivery")
    st.caption("Scan party, scan dispatch/order/document refs, then mark delivered in one batch.")
    events = _pending_delivery_events_for_scan()
    if not events:
        st.success("No dispatches awaiting delivery confirmation.")
        return

    p1, p2 = st.columns([2, 3])
    party_scan = p1.text_input(
        "Scan / type party",
        key=f"{_FK}del_scan_party_text",
        placeholder="Party barcode / name / mobile",
    )
    party_options = sorted({
        str(e.get("party_name") or "").strip()
        for e in events
        if str(e.get("party_name") or "").strip()
    })
    matched = next((e.get("party_name") for e in events if _party_scan_match(party_scan, e)), "")
    idx = party_options.index(matched) + 1 if matched in party_options else 0
    party = p2.selectbox(
        "Party",
        [""] + party_options,
        index=idx,
        key=f"{_FK}del_scan_party",
    )
    if not party:
        return

    party_events = [e for e in events if str(e.get("party_name") or "").strip() == party]
    st.caption(f"{len(party_events)} dispatched document(s) awaiting delivery for {party}.")
    raw = st.text_area(
        "Scan dispatch / order / invoice / challan",
        key=f"{_FK}del_scan_refs",
        height=110,
        placeholder="DISP/2026/00012  R/2627/0010  INV/2627/0010  CH/2627/0010",
    )
    tokens = _scan_tokens(raw)
    selected, missing, seen = [], [], set()
    for tok in tokens:
        match = next((e for e in party_events if _order_scan_match(tok, {
            **e,
            "dispatch_key": e.get("dispatch_no"),
        })), None)
        if not match:
            missing.append(tok)
            continue
        dispatch_id = str(match.get("dispatch_id") or "")
        if dispatch_id and dispatch_id not in seen:
            seen.add(dispatch_id)
            selected.append(match)

    if missing:
        st.error("Not awaiting delivery for this party: " + ", ".join(missing))
    if selected:
        st.dataframe([
            {
                "Dispatch": e.get("dispatch_no"),
                "Order": e.get("order_no"),
                "Challan": e.get("challan_no"),
                "Invoice": e.get("invoice_no"),
                "Carrier": e.get("carrier_name"),
                "Tracking": e.get("tracking_no"),
                "Qty": int(float(e.get("dispatched_qty") or 0)),
            }
            for e in selected
        ], use_container_width=True, hide_index=True)

    c1, c2 = st.columns(2)
    delivery_date = c1.date_input(
        "Delivery Date",
        value=datetime.date.today(),
        key=f"{_FK}del_scan_date",
    )
    delivered_by = c2.text_input(
        "Delivered / Confirmed By",
        value=st.session_state.get("user_name", ""),
        key=f"{_FK}del_scan_by",
    )
    notes = st.text_input(
        "Delivery Notes",
        key=f"{_FK}del_scan_notes",
        placeholder="Optional proof / receiver / remark",
    )

    if st.button(
        "✅ Mark Scan Delivery",
        type="primary",
        use_container_width=True,
        key=f"{_FK}del_scan_go",
        disabled=not selected or bool(missing),
    ):
        if not delivered_by.strip():
            st.error("Delivered / Confirmed By is required.")
            return
        ok_count, failures = 0, []
        for ev in selected:
            ok, msg = confirm_delivery(
                order_id=str(ev.get("order_id") or ""),
                dispatch_id=str(ev.get("dispatch_id") or ""),
                delivery_date=delivery_date,
                confirmed_by=delivered_by.strip(),
                notes=notes.strip() or "Fast scan delivery confirmation",
            )
            if ok:
                ok_count += 1
            else:
                failures.append(f"{ev.get('dispatch_no')}: {msg}")
        if ok_count:
            st.success(f"Delivery confirmed for {ok_count} dispatch(es).")
        for f in failures:
            st.error(f)


def _render_queue() -> None:
    st.markdown("### 📦 Orders Pending Dispatch")

    # ── Filters ───────────────────────────────────────────────────────────────
    with st.expander("🔍 Filters", expanded=True):
        f1, f2, f3 = st.columns(3)
        today = datetime.date.today()
        date_from = f1.date_input("From", value=today - datetime.timedelta(days=30),
                                   key=f"{_FK}q_from")
        date_to   = f2.date_input("To",   value=today, key=f"{_FK}q_to")
        search    = f3.text_input("Party / Order No / Challan",
                                   key=f"{_FK}q_search",
                                   placeholder="Search…")

        f4, f5 = st.columns(2)
        status_filter = f4.multiselect(
            "Status",
            ["BILLED","CHALLANED","INVOICED","INVOICED_BILLED",
             "READY_TO_DISPATCH","CHALLAN_ONLY","DISPATCHED"],
            default=["BILLED","CHALLANED","INVOICED","INVOICED_BILLED",
                     "READY_TO_DISPATCH","CHALLAN_ONLY","DISPATCHED"],
            key=f"{_FK}q_status",
        )
        route_filter = f5.selectbox(
            "Show", ["All pending", "Never dispatched", "Partially dispatched"],
            key=f"{_FK}q_mode",
        )

    # ── Query ─────────────────────────────────────────────────────────────────
    pending = _q("""
        WITH dispatch_docs AS (
            SELECT
                o.id AS order_id,
                inv.id AS invoice_id,
                inv.challan_id AS challan_id,
                COALESCE(c.challan_no, '')::text AS challan_no,
                inv.invoice_no::text AS invoice_no,
                inv.created_at
            FROM orders o
            JOIN invoices inv
              ON inv.order_ids::text[] @> ARRAY[o.id::text]
             AND inv.status NOT IN ('CANCELLED','VOID')
            LEFT JOIN challans c ON c.id = inv.challan_id
            WHERE o.status = ANY(%(statuses)s)
              AND o.created_at::date BETWEEN %(df)s AND %(dt)s

            UNION ALL

            SELECT
                o.id AS order_id,
                NULL::uuid AS invoice_id,
                c.id AS challan_id,
                c.challan_no::text AS challan_no,
                ''::text AS invoice_no,
                c.created_at
            FROM orders o
            JOIN challans c
              ON c.order_ids::text[] @> ARRAY[o.id::text]
             AND c.status NOT IN ('CANCELLED','VOID')
            WHERE o.status = ANY(%(statuses)s)
              AND o.created_at::date BETWEEN %(df)s AND %(dt)s
              AND NOT EXISTS (
                  SELECT 1
                  FROM invoices inv2
                  WHERE inv2.status NOT IN ('CANCELLED','VOID')
                    AND inv2.order_ids::text[] @> ARRAY[o.id::text]
                    AND (
                          inv2.challan_id = c.id
                       OR EXISTS (
                            SELECT 1
                            FROM invoice_lines il2
                            JOIN challan_lines cl2
                              ON cl2.order_line_id = il2.order_line_id
                             AND cl2.challan_id = c.id
                            WHERE il2.invoice_id = inv2.id
                       )
                    )
              )
        ),
        doc_lines AS (
            SELECT
                dd.order_id,
                dd.invoice_id,
                dd.challan_id,
                dd.challan_no,
                dd.invoice_no,
                il.order_line_id,
                COALESCE(il.quantity, 0)::numeric AS billed_qty
            FROM dispatch_docs dd
            JOIN invoice_lines il
              ON dd.invoice_id IS NOT NULL
             AND il.invoice_id = dd.invoice_id
             AND il.order_line_id IS NOT NULL
             AND COALESCE(il.quantity, 0) > 0

            UNION ALL

            SELECT
                dd.order_id,
                dd.invoice_id,
                dd.challan_id,
                dd.challan_no,
                dd.invoice_no,
                cl.order_line_id,
                COALESCE(cl.quantity, 0)::numeric AS billed_qty
            FROM dispatch_docs dd
            JOIN challan_lines cl
              ON dd.invoice_id IS NOT NULL
             AND dd.challan_id IS NOT NULL
             AND cl.challan_id = dd.challan_id
             AND cl.order_line_id IS NOT NULL
             AND COALESCE(cl.is_deleted, FALSE) = FALSE
             AND COALESCE(cl.quantity, 0) > 0
            WHERE NOT EXISTS (
                SELECT 1
                FROM invoice_lines il
                WHERE il.invoice_id = dd.invoice_id
                  AND il.order_line_id IS NOT NULL
                  AND COALESCE(il.quantity, 0) > 0
            )

            UNION ALL

            SELECT
                dd.order_id,
                dd.invoice_id,
                dd.challan_id,
                dd.challan_no,
                dd.invoice_no,
                cl.order_line_id,
                COALESCE(cl.quantity, 0)::numeric AS billed_qty
            FROM dispatch_docs dd
            JOIN challan_lines cl
              ON dd.challan_id IS NOT NULL
             AND dd.invoice_id IS NULL
             AND cl.challan_id = dd.challan_id
             AND cl.order_line_id IS NOT NULL
             AND COALESCE(cl.is_deleted, FALSE) = FALSE
             AND COALESCE(cl.quantity, 0) > 0
        ),
        doc_totals AS (
            SELECT
                dl.order_id,
                dl.invoice_id,
                dl.challan_id,
                dl.challan_no,
                dl.invoice_no,
                SUM(dl.billed_qty) AS total_billed,
                COALESCE(SUM((
                    SELECT COALESCE(SUM(odl.dispatched_qty), 0)
                    FROM order_dispatch_lines odl
                    JOIN order_dispatches d ON d.id = odl.dispatch_id
                    WHERE d.order_id = dl.order_id
                      AND d.status != 'CANCELLED'
                      AND odl.order_line_id = dl.order_line_id
                )), 0) AS total_dispatched
            FROM doc_lines dl
            GROUP BY dl.order_id, dl.invoice_id, dl.challan_id, dl.challan_no, dl.invoice_no
        ),
        order_billable AS (
            SELECT
                ol.order_id,
                ol.id AS order_line_id,
                GREATEST(COALESCE(ol.billing_qty, ol.quantity, 0), 0)::numeric AS required_qty
            FROM order_lines ol
            WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
              AND GREATEST(COALESCE(ol.billing_qty, ol.quantity, 0), 0) > 0
              AND (
                    COALESCE(ol.total_price, 0) > 0
                 OR COALESCE(ol.unit_price, 0) > 0
              )
        ),
        order_covered_raw AS (
            SELECT il.order_line_id, COALESCE(il.quantity, 0)::numeric AS qty
            FROM invoice_lines il
            JOIN invoices inv ON inv.id = il.invoice_id
            WHERE COALESCE(inv.status, '') NOT IN ('CANCELLED','VOID','DELETED')
              AND il.order_line_id IS NOT NULL
              AND COALESCE(il.quantity, 0) > 0

            UNION ALL

            SELECT cl.order_line_id, COALESCE(cl.quantity, 0)::numeric AS qty
            FROM challan_lines cl
            JOIN challans c ON c.id = cl.challan_id
            WHERE COALESCE(c.status, '') NOT IN ('CANCELLED','VOID','DELETED')
              AND cl.order_line_id IS NOT NULL
              AND NOT COALESCE(cl.is_deleted, FALSE)
              AND COALESCE(cl.quantity, 0) > 0
        ),
        order_coverage AS (
            SELECT
                ob.order_id,
                COALESCE(SUM(ob.required_qty), 0) AS order_required_qty,
                COALESCE(SUM(LEAST(ob.required_qty, COALESCE(ocr.covered_qty, 0))), 0) AS order_covered_qty,
                COUNT(*) FILTER (WHERE COALESCE(ocr.covered_qty, 0) < ob.required_qty) AS unbilled_line_count
            FROM order_billable ob
            LEFT JOIN (
                SELECT order_line_id, SUM(qty) AS covered_qty
                FROM order_covered_raw
                GROUP BY order_line_id
            ) ocr ON ocr.order_line_id = ob.order_line_id
            GROUP BY ob.order_id
        )
        SELECT
            o.id::text            AS order_id,
            o.order_no,
            o.status,
            o.party_id::text       AS party_id,
            COALESCE(o.party_name, o.patient_name, '—') AS party_name,
            COALESCE(o.patient_mobile, '')               AS mobile,
            COALESCE(p.address, '')                      AS address,
            p.preferred_courier_provider_id::text        AS preferred_courier_provider_id,
            COALESCE(p.preferred_courier_name, '')       AS preferred_courier_name,
            o.created_at::date::text                     AS order_date,
            o.total_value,
            COALESCE(dt.challan_no, '')                  AS challan_no,
            COALESCE(dt.invoice_no, '')                  AS invoice_no,
            dt.challan_id::text                          AS challan_id,
            dt.invoice_id::text                          AS invoice_id,
            COALESCE(dt.total_billed, 0)                 AS total_billed,
            COALESCE(dt.total_dispatched, 0)             AS total_dispatched,
            COALESCE(oc.order_required_qty, 0)           AS order_required_qty,
            COALESCE(oc.order_covered_qty, 0)            AS order_covered_qty,
            COALESCE(oc.unbilled_line_count, 0)          AS unbilled_line_count,
            o.created_at AS created_at_sort
        FROM doc_totals dt
        JOIN orders o ON o.id = dt.order_id
        LEFT JOIN parties p ON p.id = o.party_id
        LEFT JOIN order_coverage oc ON oc.order_id = o.id
        WHERE COALESCE(dt.total_billed, 0) > COALESCE(dt.total_dispatched, 0)
        ORDER BY o.created_at DESC
        LIMIT 300
    """, {
        "statuses": status_filter or ["BILLED"],
        "df": date_from.isoformat(),
        "dt": date_to.isoformat(),
    })

    # Filter: only orders with remaining qty > 0
    pending = [
        o for o in pending
        if int(o.get("total_billed") or 0) > int(o.get("total_dispatched") or 0)
    ]

    # One dispatch queue card per billing document. Same order may appear
    # multiple times when it has partial invoices, but duplicate JOIN rows for
    # the same order+doc are collapsed.
    _merged_pending = {}
    for _o in pending:
        _oid = str(_o.get("order_id") or "")
        _dispatch_key = (
            f"{_oid}|INV|{str(_o.get('invoice_no') or '').strip()}"
            if str(_o.get("invoice_no") or "").strip()
            else f"{_oid}|CH|{str(_o.get('challan_no') or '').strip()}"
        )
        if not _oid:
            continue
        if _dispatch_key not in _merged_pending:
            _merged_pending[_dispatch_key] = dict(_o)
            _merged_pending[_dispatch_key]["dispatch_key"] = _dispatch_key
    pending = list(_merged_pending.values())

    party_options = sorted({
        str(o.get("party_name") or "").strip()
        for o in pending
        if str(o.get("party_name") or "").strip()
    })
    party_filter = st.selectbox(
        "Party filter",
        ["All parties"] + party_options,
        key=f"{_FK}q_party",
        help="Filter pending dispatch documents to one party for bulk dispatch.",
    )
    if party_filter != "All parties":
        pending = [
            o for o in pending
            if str(o.get("party_name") or "").strip() == party_filter
        ]

    # Apply route_filter
    if route_filter == "Never dispatched":
        pending = [o for o in pending if int(o.get("total_dispatched") or 0) == 0]
    elif route_filter == "Partially dispatched":
        pending = [o for o in pending if int(o.get("total_dispatched") or 0) > 0]

    # Apply search
    if search.strip():
        pending = [
            o for o in pending
            if _scan_match(search, o.get("order_no"), o.get("party_name"), o.get("challan_no"), o.get("invoice_no"))
        ]

    if not pending:
        st.info("✅ No orders pending dispatch for the selected filters.")
        return

    # ── Summary metrics ────────────────────────────────────────────────────────
    m1, m2, m3 = st.columns(3)
    m1.metric("Pending Dispatch Docs", len(pending))
    m2.metric("Never Dispatched",
              sum(1 for o in pending if int(o.get("total_dispatched") or 0) == 0))
    m3.metric("Partially Dispatched",
              sum(1 for o in pending if int(o.get("total_dispatched") or 0) > 0))

    # ── Multi-select state ─────────────────────────────────────────────────────
    if f"{_FK}sticker_sel" not in st.session_state:
        st.session_state[f"{_FK}sticker_sel"] = set()
    sel_set = st.session_state[f"{_FK}sticker_sel"]
    visible_keys = {
        str(o.get("dispatch_key") or o.get("order_id") or "")
        for o in pending
        if str(o.get("dispatch_key") or o.get("order_id") or "")
    }
    if party_filter != "All parties":
        sel_set.intersection_update(visible_keys)

    if party_filter != "All parties" and pending:
        pc1, pc2 = st.columns([2, 5])
        if pc1.button("Select party pending docs", key=f"{_FK}sel_party_docs", use_container_width=True):
            sel_set.update(visible_keys)
            st.rerun()
        pc2.caption(f"{len(visible_keys)} pending dispatch document(s) for {party_filter}")

    # Bulk sticker bar
    if sel_set:
        sb1, sb2, sb3 = st.columns([4, 2, 2])
        sb1.info(f"✅ {len(sel_set)} dispatch document(s) selected")
        if sb2.button("🖨️ Print All Stickers", type="primary",
                       key=f"{_FK}bulk_print", use_container_width=True):
            _print_bulk_stickers(pending, sel_set)
        if sb3.button("✕ Clear", key=f"{_FK}clr_sel", use_container_width=True):
            st.session_state[f"{_FK}sticker_sel"] = set()
            st.rerun()
        _render_bulk_dispatch_selected(pending, sel_set)

    selected_order = st.session_state.get(f"{_FK}sel_order")

    _seen_cards = {}
    for _idx, o in enumerate(pending):
        _dkey = str(o.get("dispatch_key") or o.get("order_id") or "")
        _seen_cards[_dkey] = _seen_cards.get(_dkey, 0) + 1
        _render_queue_card(o, selected_order, sel_set, _idx, _seen_cards[_dkey])

    # Persist sel_set back
    st.session_state[f"{_FK}sticker_sel"] = sel_set


def _render_queue_card(o: Dict, selected_order: str, sel_set: set, row_idx: int = 0, dup_idx: int = 1) -> None:
    oid       = o["order_id"]
    ono       = o["order_no"]
    party     = o["party_name"]
    status    = o["status"]
    challan   = o.get("challan_no") or ""
    invoice   = o.get("invoice_no") or ""
    dispatch_key = str(o.get("dispatch_key") or f"{oid}|{challan}|{invoice}")
    total     = float(o.get("total_value") or 0)
    odate     = o.get("order_date") or ""
    tot_bil   = int(o.get("total_billed") or 0)
    tot_dis   = int(o.get("total_dispatched") or 0)
    remaining = tot_bil - tot_dis
    order_req = int(float(o.get("order_required_qty") or 0))
    order_cov = int(float(o.get("order_covered_qty") or 0))
    unbilled_lines = int(o.get("unbilled_line_count") or 0)
    order_part_billed = order_req > 0 and order_cov < order_req
    doc_ref   = (f"📋 {challan}" if challan else "") + (f"  🧾 {invoice}" if invoice else "")
    partial   = tot_dis > 0
    is_sel    = selected_order == dispatch_key
    in_stk    = dispatch_key in sel_set

    chk_col, card_col, btn_col = st.columns([0.5, 5.5, 2])

    with chk_col:
        _chk_key = f"{_FK}chk_{dispatch_key}_{row_idx}_{dup_idx}"
        tick = st.checkbox("", value=in_stk, key=_chk_key,
                            label_visibility="collapsed")
        if tick and dispatch_key not in sel_set:
            sel_set.add(dispatch_key); st.rerun()
        elif not tick and dispatch_key in sel_set:
            sel_set.discard(dispatch_key); st.rerun()

    with card_col:
        border_col = "#6366f1" if is_sel else ("#f59e0b" if partial else "#0ea5e9")
        partial_badge = (
            f"  <span style='color:#f59e0b'>⚡ {tot_dis}/{tot_bil} sent</span>"
            if partial else ""
        )
        _partial_bill_warn_html = (
            f"<div style='margin-top:5px;background:#f59e0b18;border:1px solid #f59e0b55;"
            f"border-radius:5px;padding:5px 8px;color:#fbbf24;font-size:0.72rem;font-weight:700'>"
            f"⚠️ Partial billing only: {order_cov}/{order_req} order unit(s) billed. "
            f"{unbilled_lines} line(s) still not invoiced/challaned. Dispatch this document only."
            f"</div>"
            if order_part_billed else ""
        )
        st.markdown(
            f"<div style='background:{'#1e3a5f' if is_sel else '#1e293b'};"
            f"border:1px solid {'#6366f1' if is_sel else '#334155'};"
            f"border-left:3px solid {border_col};"
            f"border-radius:6px;padding:7px 12px;margin:2px 0'>"
            f"<div style='font-weight:700;color:#e2e8f0;font-size:0.85rem'>"
            f"📦 {ono}  "
            f"<span style='color:#94a3b8;font-weight:400;font-size:0.72rem'>{party}</span>"
            f"</div>"
            f"<div style='font-size:0.70rem;color:#64748b;margin-top:2px'>"
            f"{doc_ref}  ·  ₹{total:,.0f}  ·  {odate}  ·  "
            f"<span style='color:#f59e0b'>{status}</span>  ·  "
            f"<b style='color:#e2e8f0'>{remaining} remaining</b>"
            f"{partial_badge}"
            f"</div>"
            f"{_partial_bill_warn_html}"
            f"</div>",
            unsafe_allow_html=True,
        )

    with btn_col:
        _card_suffix = f"{dispatch_key}_{row_idx}_{dup_idx}"
        if st.button(
            "🚚 Dispatch" if not is_sel else "✕ Close",
            key=f"{_FK}sel_{_card_suffix}",
            type="primary" if not is_sel else "secondary",
            use_container_width=True,
        ):
            if is_sel:
                st.session_state.pop(f"{_FK}sel_order", None)
            else:
                st.session_state[f"{_FK}sel_order"] = dispatch_key
            st.rerun()

    # Inline panel for selected order
    if is_sel:
        order_dict = {
            "id": oid, "order_no": ono, "party_name": party,
            "mobile": o.get("mobile", ""),
            "party_address": o.get("address", ""),  # from parties.address via join
            "party_id": o.get("party_id", ""),
            "preferred_courier_provider_id": o.get("preferred_courier_provider_id", ""),
            "preferred_courier_name": o.get("preferred_courier_name", ""),
            "status": status,
            "dispatch_key": dispatch_key,
            "dispatch_challan_no": challan,
            "dispatch_invoice_no": invoice,
            "dispatch_challan_id": o.get("challan_id") or "",
            "dispatch_invoice_id": o.get("invoice_id") or "",
            "order_required_qty": order_req,
            "order_covered_qty": order_cov,
            "unbilled_line_count": unbilled_lines,
        }
        with st.container(border=True):
            from modules.backoffice.dispatch_panel import render_dispatch_panel
            render_dispatch_panel(order_dict)
            st.markdown("---")
            _render_single_sticker_print(o, challan or invoice or "")


def _render_single_sticker_print(o: Dict, doc_ref: str) -> None:
    st.markdown("#### 🏷️ Address Sticker — 75×65mm")
    try:
        from modules.printing.print_templates import (
            dispatch_address_sticker_html,
            dispatch_address_sticker_tspl,
        )
        sticker = dispatch_address_sticker_html(
            party_name=o.get("party_name", ""),
            address=o.get("address", ""),
            phone=o.get("mobile", ""),
            order_no=o.get("order_no", ""),
            doc_ref=doc_ref,
            extra_line=f"Courier: {o.get('preferred_courier_name')}" if o.get("preferred_courier_name") else "",
        )
        st.markdown(sticker, unsafe_allow_html=True)
        pc1, pc2 = st.columns(2)
        print_key = str(o.get("dispatch_key") or o.get("order_id") or "")
        if pc1.button("🖨️ Print Sticker", key=f"{_FK}print_{print_key}"):
            _print_in_new_tab(sticker, key=f"sticker_{print_key}")
        tspl = dispatch_address_sticker_tspl(
            party_name=o.get("party_name", ""),
            address=o.get("address", ""),
            phone=o.get("mobile", ""),
            order_no=o.get("order_no", ""),
            doc_ref=doc_ref,
            extra_line=f"Courier: {o.get('preferred_courier_name')}" if o.get("preferred_courier_name") else "",
        )
        pc2.download_button(
            "⬇️ TSPL (thermal)", data=tspl,
            file_name=f"sticker_{o.get('order_no','')}.tspl",
            mime="text/plain",
            key=f"{_FK}tspl_{print_key}",
        )
    except Exception as e:
        st.caption(f"Sticker unavailable: {e}")


def _print_bulk_stickers(pending: List[Dict], sel_set: set) -> None:
    try:
        from modules.printing.print_templates import dispatch_address_sticker_html
        html_all = ""
        for o in pending:
            if str(o.get("dispatch_key") or o.get("order_id") or "") not in sel_set:
                continue
            challan = o.get("challan_no") or ""
            invoice = o.get("invoice_no") or ""
            html_all += dispatch_address_sticker_html(
                party_name=o.get("party_name", ""),
                address=o.get("address", ""),
                phone=o.get("mobile", ""),
                order_no=o.get("order_no", ""),
                doc_ref=challan or invoice or "",
                extra_line=f"Courier: {o.get('preferred_courier_name')}" if o.get("preferred_courier_name") else "",
            )
        _print_in_new_tab(html_all, key="bulk_sticker")
    except Exception as e:
        st.error(f"Bulk print error: {e}")


def _render_bulk_dispatch_selected(pending: List[Dict], sel_set: set) -> None:
    selected = [o for o in pending if str(o.get("dispatch_key") or o.get("order_id") or "") in sel_set]
    if not selected:
        return
    party_keys = {
        str(o.get("party_id") or "").strip() or f"NAME::{str(o.get('party_name') or '').strip().upper()}"
        for o in selected
    }
    if len(party_keys) > 1:
        st.warning("Bulk dispatch is allowed only for one party at a time. Untick other parties first.")
        return

    with st.expander("🚚 Bulk Dispatch Selected — same party", expanded=False):
        st.caption("Applies the same courier/dispatch details to every selected order. Courier billing can be added once only.")
        partial_billed_docs = []
        for _sel_o in selected:
            _req = int(float(_sel_o.get("order_required_qty") or 0))
            _cov = int(float(_sel_o.get("order_covered_qty") or 0))
            if _req > 0 and _cov < _req:
                partial_billed_docs.append(
                    f"{_sel_o.get('order_no')} ({_cov}/{_req} billed)"
                )
        if partial_billed_docs:
            st.warning(
                "⚠️ Some selected dispatch documents belong to partially billed orders: "
                + ", ".join(partial_billed_docs)
                + ". Bulk dispatch will dispatch only the selected billed document lines."
            )
        _couriers = _fetch_courier_providers()
        _provider_by_id = {str(c.get("id")): c for c in _couriers}
        first = selected[0]
        preferred_provider_id = str(first.get("preferred_courier_provider_id") or "")

        hand_options = ["🤝 Hand Delivery (Store)", "🏠 Staff Delivery", "🛵 Porter / Local Delivery"]
        labels = {
            str(c.get("id")): f"{c.get('provider_name') or ''}"
            + (" · GST" if c.get("gst_registered") else " · Non-GST")
            for c in _couriers
        }
        opts = ["— Select Courier / Method —"] + hand_options + (["─────────────────"] if _couriers else []) + [str(c.get("id")) for c in _couriers]
        idx = opts.index(preferred_provider_id) if preferred_provider_id in opts else 0

        def fmt(v):
            return labels.get(v, v)

        b1, b2, b3 = st.columns(3)
        selected_key = b1.selectbox("Courier / Method", opts, index=idx, format_func=fmt, key=f"{_FK}bulk_courier")
        selected_provider = _provider_by_id.get(selected_key) or {}
        provider_id = str(selected_provider.get("id") or "")
        carrier = selected_provider.get("provider_name") if provider_id else selected_key
        is_hand = selected_key in hand_options
        tracking = b2.text_input("Common Tracking / Note", key=f"{_FK}bulk_tracking")
        dispatch_date = b3.date_input("Dispatch Date", value=datetime.date.today(), key=f"{_FK}bulk_date")

        rinfo = _courier_rate_for_provider(provider_id)
        default_cost = float(rinfo.get("purchase_rate") or 0)
        gst_reg = bool(selected_provider.get("gst_registered"))
        default_gst = float(selected_provider.get("default_gst_percent") or rinfo.get("gst_percent") or 0) if gst_reg else 0.0
        selected_rate_option_id = ""
        selected_rate_option_label = ""
        selected_parcel_size = ""
        rate_options = _courier_rate_options_for_provider(provider_id)
        if provider_id and not is_hand:
            rate_keys = [""] + [str(r.get("id") or "") for r in rate_options]
            _bulk_ai_lines = _courier_ai_lines_for_orders([str(r.get("order_id") or "") for r in selected])
            _bulk_ai_hint = _courier_ai_pack_hint(_bulk_ai_lines, rate_options)
            _bulk_ai_rate_id = str(_bulk_ai_hint.get("option_id") or "")
            _bulk_rate_index = rate_keys.index(_bulk_ai_rate_id) if _bulk_ai_rate_id in rate_keys else 0

            def fmt_rate(v):
                if not v:
                    return "Provider default / manual"
                r = next((x for x in rate_options if str(x.get("id") or "") == v), {})
                code = str(r.get("parcel_size_code") or "").strip()
                label = str(r.get("option_label") or "").strip()
                amt = float(r.get("charge_base") or 0)
                gst = float(r.get("gst_percent") or 0)
                return f"{label}{' · ' + code if code else ''} — ₹{amt:,.2f} + GST {gst:.2f}%"

            selected_rate_option_id = st.selectbox(
                "Courier charge slab / parcel size",
                rate_keys,
                index=_bulk_rate_index,
                format_func=fmt_rate,
                key=f"{_FK}bulk_rate_option_{selected_key}",
                help="Maintain these options in Service Management → Providers & Rates.",
            )
            if _bulk_ai_hint.get("reason"):
                st.caption(_bulk_ai_hint["reason"])
            selected_rate_option = next(
                (x for x in rate_options if str(x.get("id") or "") == selected_rate_option_id),
                {},
            )
            if selected_rate_option:
                selected_rate_option_label = str(selected_rate_option.get("option_label") or "")
                selected_parcel_size = str(selected_rate_option.get("parcel_size_code") or "")
                default_cost = float(selected_rate_option.get("charge_base") or 0)
                default_gst = float(selected_rate_option.get("gst_percent") or default_gst or 0)
            elif not rate_options:
                st.caption("No parcel slabs defined for this courier yet. Using provider default/manual charge.")
        c1, c2, c3 = st.columns(3)
        cost = c1.number_input(
            "Courier Cost ₹",
            min_value=0.0,
            value=default_cost,
            step=1.0,
            key=f"{_FK}bulk_cost_{selected_key}",
            help="Auto-filled from Service Master rate for the selected courier.",
        )
        gst_pct = c2.number_input(
            "Courier GST %",
            min_value=0.0,
            max_value=28.0,
            value=default_gst,
            step=0.5,
            key=f"{_FK}bulk_gst_{selected_key}",
        )
        _bulk_order_ids = [str(r.get("order_id") or "") for r in selected]
        _bulk_existing_courier = _existing_courier_billing_for_orders(_bulk_order_ids)
        if _bulk_existing_courier and not is_hand:
            st.error(
                "🚨 COURIER ALREADY ADDED ON ONE OR MORE SELECTED ORDERS / INVOICES. "
                "Do not add courier again unless this is a genuine second parcel."
            )
            with st.expander("Existing courier entries found", expanded=True):
                for _ec in _bulk_existing_courier[:12]:
                    st.caption(
                        f"{_ec.get('source')} · {_ec.get('order_no') or ''} · "
                        f"₹{float(_ec.get('amount') or 0):,.2f} · {_ec.get('description') or ''}"
                    )
        add_once = c3.checkbox(
            "Add courier to billing once",
            value=bool(provider_id and cost > 0 and not _bulk_existing_courier),
            key=f"{_FK}bulk_bill_once",
        )
        bulk_duplicate_confirmed = False
        if add_once and _bulk_existing_courier and not is_hand:
            bulk_duplicate_confirmed = st.checkbox(
                "Yes, add another courier charge and record my confirmation in audit log",
                key=f"{_FK}bulk_confirm_duplicate_courier",
            )
        dispatched_by = st.text_input("Dispatched By", value=st.session_state.get("user_name", ""), key=f"{_FK}bulk_by")
        remarks = st.text_input("Remarks", key=f"{_FK}bulk_remarks", placeholder="Common remarks for selected dispatches")

        if st.button("🚚 Dispatch All Selected", type="primary", use_container_width=True, key=f"{_FK}bulk_dispatch_go"):
            if not carrier or carrier == "— Select Courier / Method —" or str(carrier).startswith("─"):
                st.error("Select courier / delivery method.")
                return
            if not is_hand and not tracking.strip():
                st.error("Tracking number is required for courier bulk dispatch.")
                return
            if not dispatched_by.strip():
                st.error("Dispatched By is required.")
                return
            if add_once and _bulk_existing_courier and not bulk_duplicate_confirmed:
                st.error("Courier already exists on selected orders. Confirm the duplicate/extra courier charge before dispatch.")
                return
            if add_once and _bulk_existing_courier and bulk_duplicate_confirmed:
                _log_courier_duplicate_confirmation(
                    _bulk_order_ids,
                    amount=float(cost or 0),
                    provider_name=carrier,
                    reason="Bulk dispatch user confirmed another courier charge for selected orders with existing courier.",
                    user=dispatched_by.strip() or st.session_state.get("user_name", "Dispatch"),
                )

            successes, failures = 0, []
            billed_charge_used = False
            order_groups = {}
            for o in selected:
                oid = str(o.get("order_id") or "")
                if not oid:
                    continue
                order_groups.setdefault(oid, []).append(o)

            for oid, docs_for_order in order_groups.items():
                first_doc = docs_for_order[0]
                ono = str(first_doc.get("order_no") or "")
                line_qtys = {}
                doc_refs = []
                selected_doc_ids = {"invoice": [], "challan": []}
                for o in docs_for_order:
                    lines = _dispatch_doc_lines(oid, o)
                    if not lines:
                        lines = get_billed_lines_for_order(oid)
                    if not lines:
                        lines = _fallback_billed_lines_for_order(oid)
                    for l in lines:
                        lid = str(l.get("id") or "")
                        qty = int(l.get("remaining_qty") or 0)
                        if lid and qty > 0:
                            line_qtys[lid] = line_qtys.get(lid, 0) + qty
                    doc = _dispatch_doc_context(o)
                    doc_ref = doc.get("ref") or doc.get("doc_no") or ""
                    if doc_ref and doc_ref not in doc_refs:
                        doc_refs.append(doc_ref)
                    if doc.get("invoice_id"):
                        selected_doc_ids["invoice"].append(doc.get("invoice_id"))
                    if doc.get("challan_id"):
                        selected_doc_ids["challan"].append(doc.get("challan_id"))
                if not line_qtys:
                    continue
                doc_ref = ", ".join(doc_refs)
                track_for_order = tracking.strip()
                if not is_hand and len(order_groups) > 1:
                    track_for_order = f"{tracking.strip()}-{ono.replace('/', '')}"
                ok, msg = create_dispatch_event(
                    order_id=oid,
                    order_no=ono,
                    route_code="HAND" if is_hand else "COURIER",
                    carrier_name=carrier,
                    tracking_no=track_for_order,
                    dispatched_by=dispatched_by.strip(),
                    dispatch_date=dispatch_date,
                    line_qtys=line_qtys,
                    billing_doc_ref=doc_ref,
                    remarks=remarks.strip(),
                )
                if not ok:
                    failures.append(f"{ono} ({doc_ref or 'selected docs'}): {msg}")
                    continue
                successes += 1
                if is_hand:
                    _mark_latest_local_dispatch_delivered(
                        oid,
                        confirmed_by=dispatched_by.strip(),
                        notes=tracking.strip() or carrier,
                    )
                docs = _latest_billing_doc_ids(oid)
                chal_id = (selected_doc_ids["challan"] or [docs.get("challan_id")])[0]
                inv_id = (selected_doc_ids["invoice"] or [docs.get("invoice_id")])[0]
                gst_amt = round(float(cost or 0) * float(gst_pct or 0) / 100.0, 2)
                total = round(float(cost or 0) + gst_amt, 2)
                billing_added = False
                if add_once and not billed_charge_used and chal_id and cost > 0 and not is_hand:
                    _rw("""
                        INSERT INTO challan_service_charges
                            (id, challan_id, order_id, charge_type, description,
                             base_amount, gst_percent, gst_amount, total_amount, created_at)
                        VALUES (
                            gen_random_uuid(), %(cid)s::uuid, %(oid)s::uuid, 'COURIER',
                            %(desc)s, %(base)s, %(gpct)s, %(gamt)s, %(total)s, NOW()
                        )
                    """, {
                        "cid": chal_id, "oid": oid,
                        "desc": f"Bulk courier: {carrier} | Tracking: {tracking.strip()} | {len(selected)} order(s)",
                        "base": round(float(cost or 0), 2),
                        "gpct": round(float(gst_pct or 0), 2),
                        "gamt": gst_amt,
                        "total": total,
                    })
                    _refresh_challan_invoice_totals(chal_id)
                    billed_charge_used = True
                    billing_added = True
                if provider_id or cost > 0:
                    _record_dispatch_courier_cost(
                        order_id=oid,
                        order_no=ono,
                        challan_id=chal_id or "",
                        invoice_id=inv_id or "",
                        provider_id=provider_id,
                        provider_name=carrier,
                        rate_option_id=selected_rate_option_id,
                        rate_option_label=selected_rate_option_label,
                        parcel_size=selected_parcel_size,
                        tracking_no=track_for_order,
                        dispatch_date=dispatch_date,
                        charge_base=float(cost or 0) if billing_added else 0.0,
                        gst_percent=float(gst_pct or 0) if billing_added else 0.0,
                        gst_amount=gst_amt if billing_added else 0.0,
                        total_amount=total if billing_added else 0.0,
                        billing_added=billing_added,
                        remarks=remarks.strip(),
                    )
            if successes:
                st.success(f"✅ {successes} selected dispatch document(s) dispatched.")
                st.session_state[f"{_FK}sticker_sel"] = set()
            for f in failures:
                st.error(f)
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DISPATCH HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

def _render_history() -> None:
    st.markdown("### 📋 Dispatch History")

    with st.expander("🔍 Filters", expanded=True):
        h1, h2, h3 = st.columns(3)
        today = datetime.date.today()
        h_from  = h1.date_input("From", value=today - datetime.timedelta(days=30),
                                  key=f"{_FK}h_from")
        h_to    = h2.date_input("To",   value=today, key=f"{_FK}h_to")
        h_search = h3.text_input("Order / Docket / Party",
                                  key=f"{_FK}h_search",
                                  placeholder="e.g. DISP/2026/00001")

        h4, h5 = st.columns(2)
        h_status = h4.multiselect(
            "Dispatch Status",
            ["DISPATCHED", "DELIVERED", "CANCELLED"],
            default=["DISPATCHED", "DELIVERED"],
            key=f"{_FK}h_status",
        )
        h_route = h5.selectbox(
            "Carrier / Route",
            ["All"] + LogisticsRoute.labels(),
            key=f"{_FK}h_route",
        )

    events = _q("""
        SELECT
            d.id::text            AS dispatch_id,
            d.order_id::text      AS order_id,
            d.dispatch_no,
            d.dispatch_type,
            d.route_code,
            d.carrier_name,
            d.tracking_no,
            d.dispatched_at::text AS dispatched_at,
            d.dispatched_by,
            d.delivered_at::text  AS delivered_at,
            d.remarks,
            d.is_partial,
            d.billing_doc_ref,
            COALESCE(c.challan_no, '') AS challan_no,
            COALESCE(inv.invoice_no, '') AS invoice_no,
            COALESCE(dcc.total_amount, 0)::numeric AS courier_total_amount,
            COALESCE(dcc.billing_added, FALSE) AS courier_billing_added,
            d.status,
            o.order_no,
            COALESCE(o.party_name, o.patient_name, '—') AS party_name,
            COALESCE(o.patient_mobile, '')               AS mobile
        FROM order_dispatches d
        JOIN orders o ON o.id = d.order_id
        LEFT JOIN parties p ON p.id = o.party_id
        LEFT JOIN challans c ON c.id = (
            SELECT ch.id FROM challans ch
            WHERE COALESCE(ch.is_deleted, FALSE) = FALSE
              AND ch.order_ids::text LIKE '%%' || d.order_id::text || '%%'
            ORDER BY ch.created_at DESC
            LIMIT 1
        )
        LEFT JOIN invoices inv ON inv.id = (
            SELECT i.id FROM invoices i
            WHERE COALESCE(i.is_deleted, FALSE) = FALSE
              AND (
                    i.order_ids::text LIKE '%%' || d.order_id::text || '%%'
                 OR (c.id IS NOT NULL AND i.challan_id = c.id)
              )
            ORDER BY i.created_at DESC
            LIMIT 1
        )
        LEFT JOIN dispatch_courier_costs dcc ON dcc.id = (
            SELECT dc.id FROM dispatch_courier_costs dc
            WHERE dc.order_id = d.order_id
              AND COALESCE(dc.tracking_no,'') = COALESCE(d.tracking_no,'')
            ORDER BY dc.created_at DESC
            LIMIT 1
        )
        WHERE d.dispatched_at BETWEEN %(df)s AND %(dt)s
          AND d.status = ANY(%(statuses)s)
        ORDER BY d.dispatched_at DESC, d.created_at DESC
        LIMIT 200
    """, {
        "df": h_from.isoformat(),
        "dt": h_to.isoformat(),
        "statuses": h_status or ["DISPATCHED"],
    })

    # Apply search filter
    if h_search.strip():
        events = [
            e for e in events
            if _scan_match(h_search, e.get("dispatch_no"), e.get("order_no"), e.get("tracking_no"), e.get("party_name"))
        ]
    # Apply route filter
    if h_route != "All":
        route_code = LogisticsRoute.code_for_label(h_route)
        events = [e for e in events if e.get("route_code") == route_code]

    if not events:
        st.info("No dispatch events found for the selected filters.")
        return

    # Metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Dispatches", len(events))
    m2.metric("Delivered",
              sum(1 for e in events if e.get("status") == "DELIVERED"))
    m3.metric("In Transit",
              sum(1 for e in events if e.get("status") == "DISPATCHED"))
    m4.metric("Partial",
              sum(1 for e in events if e.get("is_partial")))
    st.markdown("---")

    for ev in events:
        _render_history_card(ev)


def _render_history_card(ev: Dict) -> None:
    status     = (ev.get("status") or "DISPATCHED").upper()
    dispatch_no = ev.get("dispatch_no") or "—"
    order_no   = ev.get("order_no") or "—"
    party      = ev.get("party_name") or "—"
    carrier    = ev.get("carrier_name") or ev.get("route_code") or "—"
    tracking   = ev.get("tracking_no") or ""
    dis_at     = str(ev.get("dispatched_at") or "")[:10]
    del_at     = str(ev.get("delivered_at") or "")[:10]
    is_partial = bool(ev.get("is_partial"))
    remarks    = ev.get("remarks") or ""
    billing    = ev.get("billing_doc_ref") or ""
    route_code = ev.get("route_code") or ""

    clr = {"DISPATCHED":"#3b82f6","DELIVERED":"#10b981","CANCELLED":"#ef4444"}.get(status,"#64748b")
    icon = {"DISPATCHED":"🚚","DELIVERED":"✅","CANCELLED":"❌"}.get(status,"•")

    with st.container(border=True):
        c1, c2, c3 = st.columns([4, 4, 2])
        with c1:
            st.markdown(
                f"<div style='font-weight:700;font-family:monospace;font-size:0.88rem'>"
                f"{dispatch_no}</div>"
                f"<div style='font-size:0.75rem;color:#94a3b8;margin-top:2px'>"
                f"📦 {order_no}  ·  {party}"
                f"</div>",
                unsafe_allow_html=True,
            )
            if billing:
                st.caption(f"📋 {billing}")
        with c2:
            st.markdown(
                f"<div style='font-size:0.78rem;color:#e2e8f0'>"
                f"🚚 {carrier}"
                f"{'  ·  🔢 ' + tracking if tracking else ''}"
                f"</div>"
                f"<div style='font-size:0.70rem;color:#64748b;margin-top:2px'>"
                f"Sent: {dis_at}"
                f"{'  ·  ✅ ' + del_at if del_at else ''}"
                f"{'  ·  ⚡ Partial' if is_partial else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
            # Tracking URL
            if tracking and route_code:
                t_url = LogisticsRoute.tracking_url(route_code, tracking)
                if t_url:
                    st.markdown(
                        f"<div style='font-size:0.70rem'>"
                        f"🔗 <a href='{t_url}' target='_blank'>Track shipment</a>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
        with c3:
            partial_lbl = " ⚡ PARTIAL" if (is_partial and status == "DISPATCHED") else ""
            st.markdown(
                f"<span style='background:{clr};color:#fff;padding:3px 10px;"
                f"border-radius:12px;font-size:0.72rem;font-weight:700'>"
                f"{icon} {status}{partial_lbl}</span>",
                unsafe_allow_html=True,
            )
        if remarks:
            st.caption(f"📝 {remarks}")

        # ── Courier charge actions ────────────────────────────────────────
        _courier_amt  = float(ev.get("courier_total_amount") or 0)
        _courier_billed = bool(ev.get("courier_billing_added"))
        _order_id_h   = ev.get("order_id") or ""
        _order_no_h   = ev.get("order_no") or ""
        _party_h      = ev.get("party_name") or ""

        if _courier_amt > 0:
            if _courier_billed:
                st.markdown(
                    f"<span style='background:#052e16;color:#86efac;padding:2px 8px;"
                    f"border-radius:10px;font-size:0.70rem'>✅ Courier invoiced ₹{_courier_amt:,.2f}</span>",
                    unsafe_allow_html=True,
                )
                # Credit note option
                _cn_key = f"h_cn_{dispatch_no}"
                if st.button("📋 Credit Note for Courier", key=_cn_key,
                             help="Issue credit note if courier was charged in error"):
                    st.session_state[f"show_cn_{dispatch_no}"] = True
                if st.session_state.get(f"show_cn_{dispatch_no}"):
                    _issue_cn_inline(dispatch_no, _order_id_h, _order_no_h, _party_h)
            else:
                st.markdown(
                    f"<span style='background:#1a1a00;color:#fbbf24;padding:2px 8px;"
                    f"border-radius:10px;font-size:0.70rem'>⚠️ Courier ₹{_courier_amt:,.2f} not invoiced</span>",
                    unsafe_allow_html=True,
                )
                _raise_key = f"h_raise_{dispatch_no}"
                if st.button(f"📄 Raise Courier Invoice ₹{_courier_amt:,.2f}",
                             key=_raise_key, type="primary",
                             help="Create standalone courier invoice now"):
                    _dcc = _q("""
                        SELECT courier_provider_name, tracking_no,
                               charge_base, gst_percent, gst_amount, total_amount
                        FROM dispatch_courier_costs
                        WHERE order_id = %(oid)s::uuid
                        ORDER BY created_at DESC LIMIT 1
                    """, {"oid": _order_id_h}) or []
                    if _dcc:
                        _d = _dcc[0]
                        try:
                            _inv = _create_courier_service_invoice(
                                order_id=_order_id_h, order_no=_order_no_h,
                                party_id="", party_name=_party_h,
                                carrier_name=str(_d.get("courier_provider_name") or carrier),
                                tracking_no=str(_d.get("tracking_no") or tracking),
                                base_amount=float(_d.get("charge_base") or 0),
                                gst_percent=float(_d.get("gst_percent") or 0),
                                gst_amount=float(_d.get("gst_amount") or 0),
                                total_amount=float(_d.get("total_amount") or 0),
                                created_by="dispatch_history",
                            )
                            _rw("UPDATE dispatch_courier_costs SET billing_added=TRUE WHERE order_id=%(oid)s::uuid",
                                {"oid": _order_id_h})
                            st.success(f"✅ Courier invoice raised: **{_inv}**")
                            st.rerun()
                        except Exception as _ce:
                            st.error(f"Failed: {_ce}")
                    else:
                        st.warning("No courier cost record found for this dispatch.")
        _render_history_print_actions(ev)
        _wa_sent   = str(ev.get("wa_sent_at") or "")[:10]
        _wa_to     = ev.get("wa_sent_to") or ""
        _order_id  = ev.get("order_id") or ""
        _h_mobile  = ev.get("mobile") or ""
        _disp_id   = ev.get("dispatch_id") or ""

        if status == "DISPATCHED" and _disp_id and _order_id:
            with st.expander("✅ Mark Delivered", expanded=False):
                d1, d2 = st.columns(2)
                hist_delivery_date = d1.date_input(
                    "Delivery Date",
                    value=datetime.date.today(),
                    key=f"hist_del_date_{_disp_id}",
                )
                hist_confirmed_by = d2.text_input(
                    "Confirmed By",
                    value=st.session_state.get("user_name", ""),
                    key=f"hist_del_by_{_disp_id}",
                )
                hist_notes = st.text_input(
                    "Delivery Notes",
                    key=f"hist_del_notes_{_disp_id}",
                    placeholder="Recipient name, POD note, condition…",
                )
                if st.button(
                    f"✅ Confirm Delivered — {dispatch_no}",
                    key=f"hist_del_go_{_disp_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    ok, msg = confirm_delivery(
                        order_id=_order_id,
                        dispatch_id=_disp_id,
                        delivery_date=hist_delivery_date,
                        confirmed_by=hist_confirmed_by.strip() or "dispatch",
                        notes=hist_notes.strip(),
                    )
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

        if _wa_sent:
            st.markdown(
                f"<span style='background:#052e16;color:#86efac;padding:2px 8px;"
                f"border-radius:10px;font-size:0.68rem'>"
                f"📲 WA sent {_wa_sent}"
                f"{' → ' + _wa_to if _wa_to else ''}</span>",
                unsafe_allow_html=True,
            )

        # WA send / resend button in history
        _wa_hist_key = f"wa_hist_{_disp_id or dispatch_no}"
        if st.button(
            "📲 Send WA" if not _wa_sent else "📲 Resend WA",
            key=f"wa_btn_{_wa_hist_key}",
            use_container_width=False,
        ):
            st.session_state[f"wa_hist_open_{_wa_hist_key}"] = True

        if st.session_state.get(f"wa_hist_open_{_wa_hist_key}"):
            _mob_input = st.text_input(
                "Mobile",
                value=_wa_to or _h_mobile,
                key=f"wa_mob_hist_{_wa_hist_key}",
                placeholder="10-digit number",
            )
            try:
                from modules.wa_hub import wa_panel, wa_dispatched
                from modules.settings.shop_master import get_unit_info
                _sh  = get_unit_info("wholesale")
                _wm = wa_dispatched(
                    party      = party,
                    order_no   = order_no,
                    courier    = carrier,
                    tracking   = tracking,
                    items      = [],
                    order_type = "dispatch",
                    hand_delivery = str(route_code or "").upper() == "HAND",
                    shop_name  = _sh.get("shop_name","DV Optical"),
                    phone      = _sh.get("shop_phone",""),
                )
                wa_panel(
                    _mob_input, _wm,
                    key=f"wa_send_hist_{_wa_hist_key}",
                    title="📲 WhatsApp — Dispatch",
                    expanded=True,
                    party_name=party,
                    order_id=str(_order_id or ""),
                    on_sent_callback=lambda: _mark_wa_sent(_order_id, order_no, _mob_input),
                )
            except Exception:
                _clean = (_mob_input or "").strip().lstrip("+91").lstrip("0")
                _txt = (
                    "Dear " + party + ",\n"
                    "Your order " + order_no + " has been dispatched.\n"
                    "Courier: " + carrier + "\n"
                    "Tracking: " + (tracking or "—")
                )
                st.text_area("Copy & send manually", value=_txt,
                              height=120, key=f"wa_copy_{_wa_hist_key}")
                if _clean:
                    st.markdown(
                        f"[📲 Open WhatsApp](https://wa.me/91{_clean})",
                        unsafe_allow_html=False,
                    )
            if st.button("✕ Close WhatsApp", key=f"wa_close_{_wa_hist_key}"):
                st.session_state.pop(f"wa_hist_open_{_wa_hist_key}", None)
                st.rerun()


def _print_in_new_tab(html: str, key: str) -> None:
    """
    Opens print HTML through the shared temp-file opener.

    Dispatch used to build a large data: URL here. After smart invoice/challan
    prints gained QR/barcode/CSS, some browsers opened that data URL as a blank
    tab. The file opener is the same stable route used by billing/registers.
    """
    if not str(html or "").strip():
        st.error("Print document is blank. Please reopen this dispatch record and try again.")
        return
    if "window.print" not in html:
        html = html.replace("</body>", "<script>window.onload=function(){window.print()}</script></body>")
    try:
        from modules.printing.print_opener import open_html_print
        safe_key = re.sub(r'[/\\:*?"<>|]+', "-", str(key or "dispatch_print"))
        path = open_html_print(html, f"{safe_key}.html")
        st.success(f"Opened print document: {path}")
    except Exception as exc:
        st.error(f"Could not open print document: {exc}")


def _render_history_print_actions(ev: Dict) -> None:
    challan_no = ev.get("challan_no") or ""
    invoice_no = ev.get("invoice_no") or ""
    courier_total = float(ev.get("courier_total_amount") or 0)
    courier_billed = bool(ev.get("courier_billing_added"))
    dispatch_no = ev.get("dispatch_no") or ev.get("dispatch_id") or ""
    if not (challan_no or invoice_no or courier_total > 0):
        return

    st.markdown("##### Print Documents")
    cols = st.columns(3)
    if challan_no:
        if cols[0].button("🖨️ Challan", key=f"hist_print_ch_{dispatch_no}_{challan_no}", use_container_width=True):
            try:
                from modules.billing.smart_print import render_smart_challan
                html = render_smart_challan(challan_no, return_html=True)
                _print_in_new_tab(html, key=f"ch_{dispatch_no}")
            except Exception as exc:
                st.error(f"Challan print failed: {exc}")
    else:
        cols[0].caption("No challan")

    if invoice_no:
        if cols[1].button("🖨️ Invoice", key=f"hist_print_inv_{dispatch_no}_{invoice_no}", use_container_width=True):
            try:
                from modules.billing.smart_print import render_smart_invoice
                html = render_smart_invoice(invoice_no, return_html=True)
                _print_in_new_tab(html, key=f"inv_{dispatch_no}")
            except Exception as exc:
                st.error(f"Invoice print failed: {exc}")
    else:
        cols[1].caption("No invoice")

    if courier_total > 0:
        label = "🖨️ Courier Invoice" if courier_billed else "🖨️ Courier Cost Slip"
        if cols[2].button(label, key=f"hist_print_courier_{dispatch_no}", use_container_width=True):
            try:
                html = _dispatch_courier_slip_html(ev)
                _print_in_new_tab(html, key=f"courier_{dispatch_no}")
            except Exception as exc:
                st.error(f"Courier print failed: {exc}")
    else:
        cols[2].caption("No courier charge")


def _dispatch_courier_slip_html(ev: Dict) -> str:
    party = ev.get("party_name") or ""
    order_no = ev.get("order_no") or ""
    challan_no = ev.get("challan_no") or ""
    invoice_no = ev.get("invoice_no") or ""
    carrier = ev.get("carrier_name") or ""
    tracking = ev.get("tracking_no") or ""
    total = float(ev.get("courier_total_amount") or 0)
    dispatch_no = ev.get("dispatch_no") or ""
    billed = bool(ev.get("courier_billing_added"))
    title = "COURIER SERVICE INVOICE" if billed else "COURIER COST SLIP"
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
    <style>
    body{{font-family:Arial,sans-serif;color:#111;margin:24px}}
    .box{{max-width:720px;margin:auto;border:1px solid #222;padding:18px}}
    h2{{margin:0 0 12px;font-size:20px}}
    .muted{{color:#555;font-size:12px}}
    table{{width:100%;border-collapse:collapse;margin-top:14px}}
    td,th{{border:1px solid #999;padding:8px;text-align:left}}
    .total{{font-size:18px;font-weight:800;text-align:right;margin-top:16px}}
    @media print{{@page{{size:A4;margin:12mm}}}}
    </style></head><body>
    <div class='box'>
      <h2>{title}</h2>
      <div class='muted'>Dispatch: {dispatch_no}</div>
      <table>
        <tr><th>Party</th><td>{party}</td></tr>
        <tr><th>Order</th><td>{order_no}</td></tr>
        <tr><th>Challan / Invoice</th><td>{challan_no} {invoice_no}</td></tr>
        <tr><th>Courier</th><td>{carrier}</td></tr>
        <tr><th>Tracking</th><td>{tracking or '-'}</td></tr>
        <tr><th>Status</th><td>{'Added to customer billing' if billed else 'Provider cost only'}</td></tr>
      </table>
      <div class='total'>Courier Amount: Rs {total:,.2f}</div>
    </div>
    <script>window.onload=function(){{window.print()}}</script>
    </body></html>"""


def _fallback_dispatch_summary(order_id: str) -> Dict:
    rows = _q("""
        SELECT
            COALESCE(SUM(COALESCE(ol.billed_qty, ol.billing_qty, ol.quantity, 0)),0) AS total_billed,
            COALESCE(SUM(COALESCE(ol.dispatched_qty, 0)),0) AS total_dispatched
        FROM order_lines ol
        WHERE ol.order_id = %(oid)s::uuid
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND COALESCE(ol.billed_qty, ol.billing_qty, ol.quantity, 0) > 0
    """, {"oid": order_id}) or []
    row = rows[0] if rows else {}
    billed = int(float(row.get("total_billed") or 0))
    dispatched = int(float(row.get("total_dispatched") or 0))
    remaining = max(0, billed - dispatched)
    return {
        "total_billed": billed,
        "total_dispatched": dispatched,
        "total_remaining": remaining,
        "is_fully_dispatched": remaining == 0 and billed > 0,
        "is_partial": 0 < dispatched < billed,
        "line_count": 0,
    }


def _fallback_billed_lines_for_order(order_id: str) -> List[Dict]:
    rows = _q("""
        SELECT
            ol.id::text AS id,
            COALESCE(ol.billed_qty, ol.billing_qty, ol.quantity, 0) AS dispatch_billed_qty,
            COALESCE((
                SELECT SUM(dl.dispatched_qty)
                FROM order_dispatch_lines dl
                JOIN order_dispatches d ON d.id = dl.dispatch_id
                WHERE dl.order_line_id = ol.id AND d.status != 'CANCELLED'
            ), 0) AS already_dispatched,
            COALESCE(p.product_name,
                     ol.lens_params->>'product_name',
                     ol.lens_params->>'display_product_name', '—') AS product_name,
            COALESCE(p.brand,
                     ol.lens_params->>'brand', '')                  AS brand,
            COALESCE(ol.eye_side, '') AS eye_side,
            ol.unit_price, ol.gst_percent,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            COALESCE(ol.lens_params->>'lens_index',  '') AS lens_index,
            COALESCE(ol.lens_params->>'coating',     '') AS coating,
            COALESCE(ol.lens_params->>'treatment',   '') AS treatment,
            COALESCE(ol.lens_params->>'bc',          '') AS bc,
            COALESCE(ol.lens_params->>'dia',         '') AS dia,
            COALESCE(ol.lens_params->>'colour',      '') AS colour,
            COALESCE(ol.lens_params->>'manufacturing_route', '') AS route
        FROM order_lines ol
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE ol.order_id = %(oid)s::uuid
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND COALESCE(ol.billed_qty, ol.billing_qty, ol.quantity, 0) > 0
        ORDER BY ol.eye_side, ol.id
    """, {"oid": order_id}) or []
    out = []
    for r in rows:
        r = dict(r)
        billed = int(float(r.get("dispatch_billed_qty") or 0))
        sent = int(float(r.get("already_dispatched") or 0))
        r["billing_qty"] = billed
        r["remaining_qty"] = max(0, billed - sent)
        out.append(r)
    return out


def _dispatch_doc_context(order: Dict) -> Dict:
    invoice_no = str(order.get("dispatch_invoice_no") or order.get("invoice_no") or "").strip()
    challan_no = str(order.get("dispatch_challan_no") or order.get("challan_no") or "").strip()
    invoice_id = str(order.get("dispatch_invoice_id") or order.get("invoice_id") or "").strip()
    challan_id = str(order.get("dispatch_challan_id") or order.get("challan_id") or "").strip()
    if invoice_no or invoice_id:
        doc_type = "INVOICE"
        doc_no = invoice_no
        doc_id = invoice_id
    elif challan_no or challan_id:
        doc_type = "CHALLAN"
        doc_no = challan_no
        doc_id = challan_id
    else:
        doc_type = ""
        doc_no = ""
        doc_id = ""
    return {
        "doc_type": doc_type,
        "doc_no": doc_no,
        "doc_id": doc_id,
        "invoice_no": invoice_no,
        "challan_no": challan_no,
        "invoice_id": invoice_id,
        "challan_id": challan_id,
        "ref": f"{doc_type} {doc_no}".strip() if doc_no else "",
    }


def _summary_from_dispatch_lines(lines: List[Dict]) -> Dict:
    total_billed = sum(int(float(l.get("billing_qty") or l.get("dispatch_billed_qty") or 0)) for l in lines)
    total_dispatched = sum(int(float(l.get("already_dispatched") or 0)) for l in lines)
    total_remaining = sum(int(float(l.get("remaining_qty") or 0)) for l in lines)
    return {
        "total_billed": total_billed,
        "total_dispatched": total_dispatched,
        "total_remaining": total_remaining,
        "is_fully_dispatched": total_remaining == 0 and total_billed > 0,
        "is_partial": 0 < total_dispatched < total_billed,
        "line_count": len(lines),
    }


def _dispatch_doc_lines(order_id: str, order: Dict) -> List[Dict]:
    doc = _dispatch_doc_context(order)
    doc_no = doc.get("doc_no") or ""
    if not doc_no and not doc.get("doc_id"):
        return []

    common_select = """
            src.order_line_id::text AS id,
            COALESCE(src.quantity, 0) AS dispatch_billed_qty,
            COALESCE((
                SELECT SUM(dl.dispatched_qty)
                FROM order_dispatch_lines dl
                JOIN order_dispatches d ON d.id = dl.dispatch_id
                WHERE dl.order_line_id = src.order_line_id
                  AND d.order_id = %(oid)s::uuid
                  AND d.status != 'CANCELLED'
            ), 0) AS already_dispatched,
            COALESCE(src.product_name,
                     p.product_name,
                     ol.lens_params->>'product_name',
                     ol.lens_params->>'display_product_name', '—') AS product_name,
            COALESCE(src.brand, p.brand, ol.lens_params->>'brand', '') AS brand,
            COALESCE(src.eye_side, ol.eye_side, '') AS eye_side,
            COALESCE(src.unit_price, ol.unit_price) AS unit_price,
            __TAX_EXPR__ AS gst_percent,
            ol.sph, ol.cyl, ol.axis, ol.add_power,
            COALESCE(ol.lens_params->>'lens_index',  '') AS lens_index,
            COALESCE(ol.lens_params->>'coating',     '') AS coating,
            COALESCE(ol.lens_params->>'treatment',   '') AS treatment,
            COALESCE(ol.lens_params->>'bc',          '') AS bc,
            COALESCE(ol.lens_params->>'dia',         '') AS dia,
            COALESCE(ol.lens_params->>'colour',      '') AS colour,
            COALESCE(ol.lens_params->>'manufacturing_route', '') AS route
    """
    if doc.get("doc_type") == "INVOICE":
        select_sql = common_select.replace("__TAX_EXPR__", "COALESCE(src.tax_rate, ol.gst_percent)")
        rows = _q(f"""
            SELECT {select_sql}
            FROM invoice_lines src
            LEFT JOIN order_lines ol ON ol.id = src.order_line_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE src.order_line_id IS NOT NULL
              AND (%(doc_id)s = '' OR src.invoice_id = NULLIF(%(doc_id)s, '')::uuid)
              AND (%(doc_id)s <> '' OR src.invoice_id = (
                    SELECT id FROM invoices WHERE invoice_no = %(doc_no)s LIMIT 1
              ))
              AND (src.order_id = %(oid)s::uuid OR ol.order_id = %(oid)s::uuid)
              AND COALESCE(src.quantity, 0) > 0
            ORDER BY src.eye_side, src.id
        """, {"oid": order_id, "doc_id": doc.get("doc_id") or "", "doc_no": doc_no}) or []
        if not rows:
            # Older invoice conversion sometimes created the invoice header but
            # left invoice_lines empty. Dispatch must still use the challan
            # lines behind that invoice, otherwise paid orders vanish from the
            # dispatch queue.
            select_sql = common_select.replace("__TAX_EXPR__", "COALESCE(src.gst_percent, ol.gst_percent)")
            rows = _q(f"""
                SELECT {select_sql}
                FROM invoices inv
                JOIN challan_lines src ON src.challan_id = inv.challan_id
                LEFT JOIN order_lines ol ON ol.id = src.order_line_id
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE src.order_line_id IS NOT NULL
                  AND (%(doc_id)s = '' OR inv.id = NULLIF(%(doc_id)s, '')::uuid)
                  AND (%(doc_id)s <> '' OR inv.invoice_no = %(doc_no)s)
                  AND (src.order_id = %(oid)s::uuid OR ol.order_id = %(oid)s::uuid)
                  AND COALESCE(src.is_deleted, FALSE) = FALSE
                  AND COALESCE(src.quantity, 0) > 0
                ORDER BY src.eye_side, src.id
            """, {"oid": order_id, "doc_id": doc.get("doc_id") or "", "doc_no": doc_no}) or []
    else:
        select_sql = common_select.replace("__TAX_EXPR__", "COALESCE(src.gst_percent, ol.gst_percent)")
        rows = _q(f"""
            SELECT {select_sql}
            FROM challan_lines src
            LEFT JOIN order_lines ol ON ol.id = src.order_line_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE src.order_line_id IS NOT NULL
              AND (%(doc_id)s = '' OR src.challan_id = NULLIF(%(doc_id)s, '')::uuid)
              AND (%(doc_id)s <> '' OR src.challan_id = (
                    SELECT id FROM challans WHERE challan_no = %(doc_no)s LIMIT 1
              ))
              AND (src.order_id = %(oid)s::uuid OR ol.order_id = %(oid)s::uuid)
              AND COALESCE(src.is_deleted, FALSE) = FALSE
              AND COALESCE(src.quantity, 0) > 0
            ORDER BY src.eye_side, src.id
        """, {"oid": order_id, "doc_id": doc.get("doc_id") or "", "doc_no": doc_no}) or []

    out = []
    for r in rows:
        r = dict(r)
        billed = int(float(r.get("dispatch_billed_qty") or 0))
        sent = int(float(r.get("already_dispatched") or 0))
        r["billing_qty"] = billed
        r["remaining_qty"] = max(0, billed - sent)
        out.append(r)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TRACK SHIPMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _render_tracking() -> None:
    st.markdown("### 🔍 Track Shipment")
    st.caption("Search by docket number, order number, AWB, or party name.")

    tk_col1, tk_col2 = st.columns([4, 2])
    docket = tk_col1.text_input(
        "Docket / AWB / Order No",
        key=f"{_FK}track_no",
        placeholder="e.g. DISP/2026/00001 or R/2627/0125 or 123456789",
    )
    if tk_col2.button("🔍 Search", type="primary", key=f"{_FK}track_btn"):
        st.session_state[f"{_FK}track_query"] = docket.strip()

    query = st.session_state.get(f"{_FK}track_query", "")
    if not query:
        return

    results = _q("""
        SELECT
            d.id::text            AS dispatch_id,
            d.dispatch_no,
            d.route_code,
            d.carrier_name,
            d.tracking_no,
            d.dispatched_at::text AS dispatched_at,
            d.dispatched_by,
            d.delivered_at::text  AS delivered_at,
            d.delivery_notes,
            d.remarks,
            d.is_partial,
            d.billing_doc_ref,
            d.status,
            COALESCE(d.wa_sent_at::text, '') AS wa_sent_at,
            COALESCE(d.wa_sent_to, '')        AS wa_sent_to,
            o.order_no,
            COALESCE(o.party_name, o.patient_name, '—') AS party_name,
            COALESCE(o.patient_mobile, '')               AS mobile,
            COALESCE(p.address, '')                      AS address
        FROM order_dispatches d
        JOIN orders o ON o.id = d.order_id
        LEFT JOIN parties p ON p.id = o.party_id
        WHERE d.dispatch_no ILIKE %(q)s
           OR d.tracking_no ILIKE %(q)s
           OR o.order_no    ILIKE %(q)s
           OR COALESCE(o.party_name, o.patient_name) ILIKE %(q)s
        ORDER BY d.dispatched_at DESC
        LIMIT 20
    """, {"q": f"%{query}%"})

    if not results:
        st.warning(f"No dispatch found for '{query}'.")
        return

    for ev in results:
        status    = (ev.get("status") or "DISPATCHED").upper()
        dis_no    = ev.get("dispatch_no") or "—"
        ord_no    = ev.get("order_no") or "—"
        party     = ev.get("party_name") or "—"
        carrier   = ev.get("carrier_name") or "—"
        tracking  = ev.get("tracking_no") or ""
        route     = ev.get("route_code") or ""
        dis_at    = str(ev.get("dispatched_at") or "")[:10]
        del_at    = str(ev.get("delivered_at") or "")[:10]
        del_notes = ev.get("delivery_notes") or ""
        remarks   = ev.get("remarks") or ""
        billing   = ev.get("billing_doc_ref") or ""
        mobile    = ev.get("mobile") or ""
        address   = ev.get("address") or ""
        dispatch_id = ev.get("dispatch_id") or ""
        is_partial  = bool(ev.get("is_partial"))

        clr = {"DISPATCHED":"#3b82f6","DELIVERED":"#10b981","CANCELLED":"#ef4444"}.get(status,"#64748b")
        icon = {"DISPATCHED":"🚚","DELIVERED":"✅","CANCELLED":"❌"}.get(status,"•")

        with st.container(border=True):
            st.markdown(
                f"<div style='font-weight:700;font-size:0.95rem;color:#e2e8f0'>"
                f"{icon} {dis_no}"
                f"  <span style='background:{clr};color:#fff;padding:2px 8px;"
                f"border-radius:10px;font-size:0.72rem'>{status}</span>"
                f"{'  <span style=\"color:#f59e0b\">⚡ Partial</span>' if is_partial else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"**Order:** {ord_no}  ·  **Party:** {party}"
                + (f"  ·  📞 {mobile}" if mobile else "")
            )
            if address:
                st.caption(f"📍 {address}")
            if billing:
                st.caption(f"📋 {billing}")
            st.markdown(
                f"**Carrier:** {carrier}"
                + (f"  ·  **Tracking:** `{tracking}`" if tracking else "")
            )
            st.markdown(f"**Dispatched:** {dis_at}" + (f"  ·  **Delivered:** {del_at}" if del_at else ""))
            if del_notes:
                st.caption(f"Delivery notes: {del_notes}")
            if remarks:
                st.caption(f"Remarks: {remarks}")

            # Live tracking URL
            if tracking and route:
                t_url = LogisticsRoute.tracking_url(route, tracking)
                if t_url:
                    st.markdown(
                        f"🔗 [**Track live on carrier website**]({t_url})",
                        unsafe_allow_html=False,
                    )

            # Delivery confirmation inline (if still in transit)
            if status == "DISPATCHED":
                with st.expander("✅ Confirm Delivery", expanded=False):
                    cv1, cv2 = st.columns(2)
                    del_date = cv1.date_input("Delivery Date",
                                               value=datetime.date.today(),
                                               key=f"{_FK}del_d_{dispatch_id}")
                    conf_by  = cv2.text_input("Confirmed By",
                                               value=st.session_state.get("user_name",""),
                                               key=f"{_FK}del_by_{dispatch_id}")
                    del_note = st.text_input("Notes",
                                              key=f"{_FK}del_note_{dispatch_id}",
                                              placeholder="Recipient, condition…")
                    order_id_for_del = _q(
                        "SELECT order_id::text FROM order_dispatches WHERE id=%s::uuid LIMIT 1",
                        (dispatch_id,)
                    )
                    oid_del = (order_id_for_del[0].get("order_id") or "") if order_id_for_del else ""
                    if st.button(f"✅ Mark Delivered",
                                  key=f"{_FK}del_btn_{dispatch_id}",
                                  type="primary"):
                        ok, msg = confirm_delivery(
                            order_id=oid_del,
                            dispatch_id=dispatch_id,
                            delivery_date=del_date,
                            confirmed_by=conf_by.strip() or "staff",
                            notes=del_note.strip(),
                        )
                        if ok:
                            st.success(msg)
                            st.session_state.pop(f"{_FK}track_query", None)
                            st.rerun()
                        else:
                            st.error(msg)


# ──────────────────────────────────────────────────────────────
# MAIN ENTRY
# ──────────────────────────────────────────────────────────────

def _mark_wa_sent(
    order_id: str,
    order_no: str,
    mobile: str = "",
    mark_delivered: bool = False,
    delivered_by: str = "",
    delivery_notes: str = "",
) -> None:
    """Stamp wa_sent_at on the most recent dispatch for this order."""
    try:
        from modules.sql_adapter import run_write
        run_write("""
            UPDATE order_dispatches
            SET wa_sent_at = NOW(),
                wa_sent_to  = %(mob)s
            WHERE id = (
                SELECT id FROM order_dispatches
                WHERE order_id = %(oid)s::uuid
                  AND status != 'CANCELLED'
                ORDER BY created_at DESC
                LIMIT 1
            )
        """, {"oid": order_id, "mob": (mobile or "")[:20]})
        if mark_delivered:
            rows = _q("""
                SELECT id::text
                FROM order_dispatches
                WHERE order_id = %(oid)s::uuid
                  AND status = 'DISPATCHED'
                ORDER BY created_at DESC
                LIMIT 1
            """, {"oid": order_id}) or []
            dispatch_id = str((rows[0] or {}).get("id") or "") if rows else ""
            if dispatch_id:
                confirm_delivery(
                    order_id=order_id,
                    dispatch_id=dispatch_id,
                    delivery_date=datetime.date.today(),
                    confirmed_by=delivered_by or st.session_state.get("user_name", "") or "dispatch",
                    notes=delivery_notes or "Confirmed after WhatsApp dispatch notification",
                )
    except Exception:
        pass  # non-fatal


def _mark_latest_local_dispatch_delivered(
    order_id: str,
    confirmed_by: str = "",
    notes: str = "",
) -> None:
    rows = _q("""
        SELECT id::text
        FROM order_dispatches
        WHERE order_id = %(oid)s::uuid
          AND status = 'DISPATCHED'
        ORDER BY created_at DESC
        LIMIT 1
    """, {"oid": order_id}) or []
    dispatch_id = str((rows[0] or {}).get("id") or "") if rows else ""
    if dispatch_id:
        confirm_delivery(
            order_id=order_id,
            dispatch_id=dispatch_id,
            delivery_date=datetime.date.today(),
            confirmed_by=confirmed_by or st.session_state.get("user_name", "") or "dispatch",
            notes=notes or "Local delivery confirmed at dispatch",
        )


def render_dispatch_panel(order: Dict) -> None:
    """
    Full dispatch panel.
    Always call this AFTER billing documents are confirmed.
    Hard gate is re-checked here as a safety layer.
    """
    order_id = str(order.get("id") or "")
    order_no = order.get("order_no") or "—"
    status   = order.get("status") or "PENDING"

    st.markdown("---")
    st.markdown("### 🚚 Dispatch & Logistics")

    # ── HARD GATE: must have billing document ─────────────────────────────
    is_billed, gate_msg, billing_docs = billing_gate_check(order_id)
    if not is_billed:
        st.error(
            "🔒 **Dispatch is locked.** "
            "Create a Challan or Invoice before dispatching this order."
        )
        st.caption(gate_msg)
        return

    doc_lines = _dispatch_doc_lines(order_id, order)

    # Show which billing document is covering this dispatch
    _render_billing_docs_badge(billing_docs)

    # ── Dispatch summary bar ───────────────────────────────────────────────
    summary = get_dispatch_summary(order_id)
    if doc_lines:
        summary = _summary_from_dispatch_lines(doc_lines)
    if int(summary.get("total_billed") or 0) == 0:
        summary = _fallback_dispatch_summary(order_id)
    _render_dispatch_summary_bar(summary)

    _coverage = {
        "order_required_qty": int(float(order.get("order_required_qty") or 0)),
        "order_covered_qty": int(float(order.get("order_covered_qty") or 0)),
        "unbilled_line_count": int(order.get("unbilled_line_count") or 0),
    }
    if not _coverage["order_required_qty"]:
        _coverage = _order_billing_coverage(order_id)
    if (
        int(_coverage.get("order_required_qty") or 0) > 0
        and int(_coverage.get("order_covered_qty") or 0) < int(_coverage.get("order_required_qty") or 0)
    ):
        st.warning(
            "⚠️ **Partial billing dispatch warning:** this billing document covers only "
            f"{int(_coverage.get('order_covered_qty') or 0)}/"
            f"{int(_coverage.get('order_required_qty') or 0)} order unit(s). "
            f"{int(_coverage.get('unbilled_line_count') or 0)} order line(s) are still not challaned/invoiced. "
            "Dispatch only if this document's goods are actually going out now. "
            "If you want to wait for the remaining part, cancel/close this dispatch and finish billing first."
        )

    # ── Read-only for terminal states ─────────────────────────────────────
    if status in ("DELIVERED", "CLOSED"):
        _render_dispatch_history(order_id, order_no, read_only=True)
        return

    tab_new, tab_history, tab_deliver = st.tabs([
        "📦 New Dispatch",
        "📋 Dispatch History",
        "✅ Confirm Delivery",
    ])

    with tab_new:
        _render_new_dispatch_form(order, billing_docs, summary, doc_lines)

    with tab_history:
        _render_dispatch_history(order_id, order_no)

    with tab_deliver:
        _render_delivery_confirmation(order_id, order_no)


# ──────────────────────────────────────────────────────────────
# BILLING DOCS BADGE
# ──────────────────────────────────────────────────────────────

def _render_billing_docs_badge(billing_docs: List[Dict]) -> None:
    pills = ""
    for doc in billing_docs:
        t     = doc.get("doc_type", "DOC")
        no    = doc.get("doc_no", "—")
        amt   = float(doc.get("amount") or 0)
        color = "#059669" if t == "INVOICE" else "#0891b2"
        icon  = "🧾" if t == "INVOICE" else "📋"
        pills += (
            f"<span style='background:{color}22;border:1px solid {color}55;"
            f"color:{color};padding:3px 12px;border-radius:12px;"
            f"font-size:0.72rem;font-weight:700;margin-right:6px'>"
            f"{icon} {t} {no}  ₹{amt:,.2f}</span>"
        )
    st.markdown(
        f"<div style='margin-bottom:10px'>"
        f"<span style='color:#64748b;font-size:0.65rem;margin-right:6px'>"
        f"🔓 Billing verified:</span>{pills}</div>",
        unsafe_allow_html=True,
    )


# ──────────────────────────────────────────────────────────────
# SUMMARY BAR
# ──────────────────────────────────────────────────────────────

def _render_dispatch_summary_bar(summary: Dict) -> None:
    billed     = summary.get("total_billed", 0)
    dispatched = summary.get("total_dispatched", 0)
    remaining  = summary.get("total_remaining", 0)
    pct = int(100 * dispatched / billed) if billed else 0

    status_color = (
        "#10b981" if remaining == 0 and billed > 0
        else "#f59e0b" if dispatched > 0
        else "#3b82f6"
    )
    status_label = (
        "✅ Fully Dispatched"
        if remaining == 0 and billed > 0
        else f"⚡ Partial — {remaining} unit(s) still pending"
        if dispatched > 0
        else "📦 Not yet dispatched"
    )

    c1, c2, c3, c4 = st.columns(4)
    for col, val, label, color in [
        (c1, str(billed),     "Billed Qty",    "#3b82f6"),
        (c2, str(dispatched), "Dispatched",    "#8b5cf6"),
        (c3, str(remaining),  "Remaining",     "#f59e0b" if remaining else "#10b981"),
        (c4, f"{pct}%",       "Dispatch %",    status_color),
    ]:
        col.markdown(
            f"<div style='background:#1e293b;border-radius:8px;padding:8px 12px;"
            f"text-align:center;border-top:3px solid {color}'>"
            f"<div style='color:{color};font-size:1.1rem;font-weight:800'>{val}</div>"
            f"<div style='color:#64748b;font-size:0.65rem'>{label}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    st.markdown(
        f"<div style='color:{status_color};font-size:0.78rem;font-weight:600;"
        f"margin:6px 0 10px'>{status_label}</div>",
        unsafe_allow_html=True,
    )


def _fetch_courier_providers() -> List[Dict]:
    """Courier master comes only from Service Management providers."""
    try:
        from modules.backoffice.service_master import fetch_providers
        rows = fetch_providers("COURIER", active_only=True) or []
        if rows:
            return rows
    except Exception:
        pass
    try:
        return _q("""
            SELECT sp.id::text,
                   sp.provider_name,
                   COALESCE(sp.provider_type, 'COURIER') AS provider_type,
                   COALESCE(sp.contact, '') AS contact,
                   COALESCE(sp.address, '') AS address,
                   COALESCE(sp.gstin, '') AS gstin,
                   COALESCE(sp.gst_registered, FALSE) AS gst_registered,
                   COALESCE(sp.default_gst_percent, 0)::numeric AS default_gst_percent,
                   COALESCE(sp.is_active, TRUE) AS is_active,
                   COALESCE(sp.notes, '') AS notes
            FROM service_providers sp
            WHERE COALESCE(sp.is_active, TRUE) = TRUE
              AND (
                    UPPER(COALESCE(sp.provider_type,'')) LIKE '%%COURIER%%'
                 OR EXISTS (
                    SELECT 1
                    FROM service_provider_rates spr
                    JOIN service_types st ON st.service_code = spr.service_code
                    WHERE spr.provider_id = sp.id
                      AND UPPER(COALESCE(st.service_group,'')) = 'COURIER'
                      AND COALESCE(spr.is_active, TRUE) = TRUE
                 )
              )
            ORDER BY sp.provider_name
        """) or []
    except Exception as exc:
        st.caption(f"Courier provider lookup failed: {exc}")
        return []


def _courier_rate_for_provider(provider_id: str) -> Dict:
    if not provider_id:
        return {"purchase_rate": 0.0, "gst_percent": 0.0, "service_code": ""}
    rows = _q("""
        SELECT spr.purchase_rate,
               COALESCE(st.gst_percent, 0) AS gst_percent,
               COALESCE(st.service_code, '') AS service_code
        FROM service_provider_rates spr
        JOIN service_types st ON st.service_code = spr.service_code
        WHERE spr.provider_id = %(pid)s::uuid
          AND st.service_group = 'COURIER'
          AND COALESCE(spr.is_active, TRUE) = TRUE
          AND COALESCE(st.is_active, TRUE) = TRUE
          AND (spr.effective_to IS NULL OR spr.effective_to >= CURRENT_DATE)
        ORDER BY spr.effective_from DESC NULLS LAST
        LIMIT 1
    """, {"pid": provider_id}) or []
    if not rows:
        return {"purchase_rate": 0.0, "gst_percent": 0.0, "service_code": ""}
    row = rows[0]
    return {
        "purchase_rate": float(row.get("purchase_rate") or 0),
        "gst_percent": float(row.get("gst_percent") or 0),
        "service_code": row.get("service_code") or "",
    }


def _courier_rate_options_for_provider(provider_id: str) -> List[Dict]:
    if not provider_id:
        return []
    try:
        from modules.backoffice.service_master import fetch_courier_rate_options
        return fetch_courier_rate_options(provider_id, active_only=True) or []
    except Exception:
        return []


def _courier_ai_pack_hint(lines: List[Dict], rate_options: List[Dict]) -> Dict:
    """Suggest a courier slab from dispatch contents.

    This is intentionally deterministic for now. It gives staff a useful
    default without hiding the manual slab dropdown, and can be replaced later
    by a learned model using the same return shape.
    """
    if not lines or not rate_options:
        return {}

    total_qty = 0.0
    lens_qty = 0.0
    solution_qty = 0.0
    box_like = False
    has_solution = False
    has_lens = False
    product_words = []

    for line in lines:
        qty = float(
            line.get("remaining_qty")
            or line.get("billing_qty")
            or line.get("dispatch_billed_qty")
            or line.get("quantity")
            or 0
        )
        total_qty += max(qty, 0.0)
        text = " ".join(
            str(line.get(k) or "")
            for k in ("product_name", "brand", "main_group", "category", "unit")
        ).lower()
        product_words.append(text)
        if any(w in text for w in ("solution", "cleaner", "lens care", "360ml", "360 ml")):
            has_solution = True
            solution_qty += max(qty, 0.0)
        if any(w in text for w in ("ophthalmic", "contact lens", "lens", "rx", "progressive", "single vision")):
            has_lens = True
            lens_qty += max(qty, 0.0)
        if any(w in text for w in ("box", "carton", "15pc", "15 pc", "15pcs", "15 pcs", "x15", "*15")) or qty >= 12:
            box_like = True

    all_text = " ".join(product_words)
    reason = ""
    wanted_groups: List[List[str]] = []

    if has_solution and (box_like or solution_qty >= 12):
        wanted_groups = [["box", "carton", "large", "heavy", "15"], ["medium"]]
        reason = "AI suggestion: solution full box/carton dispatch."
    elif has_solution and solution_qty > 1:
        wanted_groups = [["medium", "parcel"], ["small"]]
        reason = f"AI suggestion: {solution_qty:g} solution item(s)."
    elif has_solution:
        wanted_groups = [["small", "packet"], ["medium"]]
        reason = "AI suggestion: one solution bottle."
    elif has_lens and lens_qty >= 6:
        wanted_groups = [["medium", "parcel"], ["large"]]
        reason = "AI suggestion: about 3 lens pairs or more."
    elif has_lens and lens_qty >= 3:
        wanted_groups = [["small", "packet"], ["medium"]]
        reason = "AI suggestion: multiple lens pieces/pairs."
    elif total_qty >= 8 or any(w in all_text for w in ("frame", "sunglass", "sunglasses")):
        wanted_groups = [["medium", "parcel"], ["large"]]
        reason = "AI suggestion: larger parcel contents."
    else:
        wanted_groups = [["small", "packet"], ["local"]]
        reason = "AI suggestion: light dispatch."

    for group in wanted_groups:
        matches = []
        for opt in rate_options:
            hay = " ".join(
                str(opt.get(k) or "")
                for k in ("option_label", "parcel_size_code", "notes")
            ).lower()
            if any(word in hay for word in group):
                matches.append(opt)
        if matches:
            best = min(matches, key=lambda r: float(r.get("charge_base") or 0))
            return {
                "option_id": str(best.get("id") or ""),
                "reason": reason + " Lowest matching charge auto-selected.",
            }

    cheapest = min(rate_options, key=lambda r: float(r.get("charge_base") or 0))
    return {
        "option_id": str(cheapest.get("id") or ""),
        "reason": reason + " No exact slab name found; lowest charge auto-selected.",
    }


def _courier_ai_lines_for_orders(order_ids: List[str]) -> List[Dict]:
    ids = [str(x or "").strip() for x in order_ids if str(x or "").strip()]
    if not ids:
        return []
    try:
        return _q(
            """
            SELECT
                ol.id::text AS id,
                ol.order_id::text AS order_id,
                COALESCE(ol.billed_qty, ol.billing_qty, ol.quantity, 0) AS remaining_qty,
                COALESCE(p.product_name,
                         ol.lens_params->>'product_name',
                         ol.lens_params->>'display_product_name', '') AS product_name,
                COALESCE(p.brand, ol.lens_params->>'brand', '') AS brand,
                COALESCE(ol.lens_params->>'main_group', ol.lens_params->>'category', '') AS main_group,
                COALESCE(ol.lens_params->>'unit', '') AS unit
            FROM order_lines ol
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE ol.order_id::text = ANY(%(ids)s)
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.billed_qty, ol.billing_qty, ol.quantity, 0) > 0
            """,
            {"ids": ids},
        ) or []
    except Exception:
        return []


def _existing_courier_billing_for_orders(order_ids: List[str]) -> List[Dict]:
    ids = [str(x or "").strip() for x in order_ids if str(x or "").strip()]
    if not ids:
        return []
    rows: List[Dict] = []
    try:
        rows.extend(_q(
            """
            SELECT 'ORDER_LINE' AS source,
                   COALESCE(o.order_no, '') AS order_no,
                   COALESCE(ol.lens_params->>'service_display_name',
                            ol.lens_params->>'service_description',
                            ol.lens_params->>'display_product_name',
                            'Courier service line') AS description,
                   COALESCE(ol.billing_total, ol.total_price, ol.unit_price, 0)::numeric AS amount
            FROM order_lines ol
            LEFT JOIN orders o ON o.id = ol.order_id
            WHERE ol.order_id::text = ANY(%(ids)s)
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(ol.lens_params->>'charge_type',
                                 ol.lens_params->>'service_type',
                                 ol.lens_params->>'service_production_type','')) = 'COURIER'
              AND COALESCE(ol.billing_total, ol.total_price, ol.unit_price, 0) > 0
            """,
            {"ids": ids},
        ) or [])
    except Exception:
        pass
    try:
        rows.extend(_q(
            """
            SELECT 'CHALLAN_SERVICE' AS source,
                   COALESCE(o.order_no, '') AS order_no,
                   COALESCE(csc.description, 'Courier charge in challan/invoice') AS description,
                   COALESCE(csc.total_amount, csc.base_amount, 0)::numeric AS amount
            FROM challan_service_charges csc
            LEFT JOIN orders o ON o.id = csc.order_id
            WHERE csc.order_id::text = ANY(%(ids)s)
              AND UPPER(COALESCE(csc.charge_type,'')) = 'COURIER'
              AND COALESCE(csc.total_amount, csc.base_amount, 0) > 0
            """,
            {"ids": ids},
        ) or [])
    except Exception:
        pass
    try:
        rows.extend(_q(
            """
            SELECT 'DISPATCH_COURIER' AS source,
                   COALESCE(order_no, '') AS order_no,
                   COALESCE(courier_provider_name, 'Courier at dispatch') AS description,
                   COALESCE(total_amount, charge_base, 0)::numeric AS amount
            FROM dispatch_courier_costs
            WHERE order_id::text = ANY(%(ids)s)
              AND COALESCE(total_amount, charge_base, 0) > 0
            """,
            {"ids": ids},
        ) or [])
    except Exception:
        pass
    return [dict(r) for r in rows if float(r.get("amount") or 0) > 0]


def _log_courier_duplicate_confirmation(
    order_ids: List[str],
    *,
    amount: float,
    provider_name: str,
    reason: str,
    user: str,
) -> None:
    for oid in [str(x or "").strip() for x in order_ids if str(x or "").strip()]:
        try:
            from modules.backoffice.audit_logger import audit
            audit(
                "courier_duplicate_confirmed",
                entity="dispatch",
                entity_id=oid,
                order_id=oid,
                user_id=user or "Dispatch",
                payload={
                    "amount": round(float(amount or 0), 2),
                    "reason": reason or "User confirmed duplicate courier charge",
                    "party_name": provider_name or "",
                },
            )
        except Exception:
            pass


def _latest_billing_doc_ids(order_id: str) -> Dict:
    out = {"challan_id": None, "invoice_id": None}
    try:
        ch = _q("""
            SELECT id::text
            FROM challans
            WHERE COALESCE(is_deleted, FALSE) = FALSE
              AND status NOT IN ('CANCELLED','VOID')
              AND order_ids::text LIKE %(needle)s
            ORDER BY created_at DESC
            LIMIT 1
        """, {"needle": f"%{order_id}%"}) or []
        if ch:
            out["challan_id"] = ch[0].get("id")
    except Exception:
        pass
    try:
        inv = _q("""
            SELECT id::text
            FROM invoices
            WHERE COALESCE(is_deleted, FALSE) = FALSE
              AND status NOT IN ('CANCELLED','VOID')
              AND order_ids::text LIKE %(needle)s
            ORDER BY created_at DESC
            LIMIT 1
        """, {"needle": f"%{order_id}%"}) or []
        if inv:
            out["invoice_id"] = inv[0].get("id")
    except Exception:
        pass
    return out


def _refresh_challan_invoice_totals(challan_id: str) -> None:
    if not challan_id:
        return
    _rw("""
        WITH line_tot AS (
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN COALESCE(gst_percent,0) > 0
                            THEN COALESCE(line_total,total_price,0) / (1 + gst_percent / 100.0)
                        ELSE COALESCE(line_total,total_price,0)
                    END
                ),0) AS line_base,
                COALESCE(SUM(
                    CASE
                        WHEN COALESCE(gst_percent,0) > 0
                            THEN COALESCE(line_total,total_price,0)
                                 - (COALESCE(line_total,total_price,0) / (1 + gst_percent / 100.0))
                        ELSE 0
                    END
                ),0) AS line_gst,
                COALESCE(SUM(COALESCE(line_total,total_price,0)),0) AS line_gross
            FROM challan_lines
            WHERE challan_id = %(cid)s::uuid
              AND COALESCE(is_deleted, FALSE) = FALSE
        ),
        svc_tot AS (
            SELECT
                COALESCE(SUM(base_amount),0) AS svc_base,
                COALESCE(SUM(gst_amount),0) AS svc_gst,
                COALESCE(SUM(total_amount),0) AS svc_gross
            FROM challan_service_charges
            WHERE challan_id = %(cid)s::uuid
        ),
        calc AS (
            SELECT
                ROUND((line_base + svc_base)::numeric, 2) AS total_amount,
                ROUND((line_gst + svc_gst)::numeric, 2) AS total_tax,
                ROUND((line_gross + svc_gross)::numeric, 2) AS raw_total,
                ROUND((line_gross + svc_gross)::numeric, 0) AS rounded_total
            FROM line_tot, svc_tot
        ),
        upd_ch AS (
            UPDATE challans c
               SET total_amount = calc.total_amount,
                   total_tax = calc.total_tax,
                   grand_total = calc.rounded_total,
                   round_off_amount = ROUND((calc.rounded_total - calc.raw_total)::numeric, 2),
                   balance_due = GREATEST(0, calc.rounded_total - COALESCE(c.amount_paid,0)),
                   payment_complete = COALESCE(c.amount_paid,0) >= calc.rounded_total
              FROM calc
             WHERE c.id = %(cid)s::uuid
             RETURNING c.id
        )
        SELECT id FROM upd_ch
    """, {"cid": challan_id})



def _create_courier_service_invoice(
    *,
    order_id: str,
    order_no: str,
    party_id: str,
    party_name: str,
    carrier_name: str,
    tracking_no: str,
    base_amount: float,
    gst_percent: float,
    gst_amount: float,
    total_amount: float,
    created_by: str = "dispatch",
) -> str:
    """
    Create a standalone service invoice for courier charge.
    NEVER modifies existing challan or product invoice.
    Returns invoice_no on success, raises on failure.
    """
    import uuid as _cuuid

    # Allocate invoice number
    try:
        from modules.db.order_number_registry import alloc_doc_number
        inv_no = alloc_doc_number("INVOICE")
    except Exception:
        import datetime as _cdt
        inv_no = f"INV/COURIER/{_cdt.date.today().strftime('%y%m%d')}/{_cuuid.uuid4().hex[:5].upper()}"

    # GST Rule: courier service to customer always attracts 18% GST
    # (CGST 9% + SGST 9% intrastate, IGST 18% interstate)
    # regardless of whether the courier provider gives us a GST invoice.
    # We are billing our OWN service to the customer.
    if gst_percent <= 0:
        gst_percent = 18.0
        gst_amount  = round(base_amount * gst_percent / 100, 2)
        total_amount = round(base_amount + gst_amount, 2)

    resolved_party_id = str(party_id or "").strip()
    if not resolved_party_id and str(party_name or "").strip():
        try:
            party_rows = _q("""
                SELECT id::text
                FROM parties
                WHERE LOWER(TRIM(party_name)) = LOWER(TRIM(%(pn)s))
                  AND COALESCE(is_active, TRUE) = TRUE
                ORDER BY party_name
                LIMIT 1
            """, {"pn": str(party_name or "").strip()}) or []
            if party_rows:
                resolved_party_id = str(party_rows[0].get("id") or "").strip()
        except Exception:
            resolved_party_id = ""

    grand_total = round(base_amount + gst_amount, 2)
    # Round to nearest rupee
    rounded    = round(grand_total)
    round_off  = round(rounded - grand_total, 2)

    _inv_uuid = str(_cuuid.uuid4())
    ok = _rw("""
        INSERT INTO invoices (
            id, invoice_no, party_id, order_ids,
            invoice_date, due_date,
            total_amount, total_tax, grand_total,
            amount_paid, balance_due,
            round_off_amount,
            status, payment_status,
            remarks, created_by, created_at, updated_at,
            gst_included, is_partial_billing
        ) VALUES (
            %(id)s::uuid,
            %(inv_no)s,
            NULLIF(%(pid)s,'')::uuid,
            '{}'::text[],
            CURRENT_DATE,
            CURRENT_DATE,
            %(base)s, %(gst)s, %(grand)s,
            0, %(grand)s,
            %(roff)s,
            'PENDING', 'UNPAID',
            %(remarks)s, %(by)s, NOW(), NOW(),
            TRUE, FALSE
        )
    """, {
        "id":      _inv_uuid,
        "inv_no":  inv_no,
        "pid":     resolved_party_id,
        "base":    base_amount,
        "gst":     gst_amount,
        "grand":   float(rounded),
        "roff":    round_off,
        "remarks": f"Courier service — {carrier_name}"
                   + (f" | Tracking: {tracking_no}" if tracking_no else "")
                   + f" | Order: {order_no}",
        "by":      created_by,
    })
    if not ok:
        raise RuntimeError(f"Invoice INSERT failed for courier charge on {order_no}")

    # Insert single courier service line using the UUID we generated
    line_ok = _rw("""
        INSERT INTO invoice_lines (
            id, invoice_id, product_name, quantity,
            unit_price, gst_percent, tax_amount, line_total,
            total_price, created_at
        ) VALUES (
            gen_random_uuid(), %(iid)s::uuid, %(pname)s, 1,
            %(base)s, %(gpct)s, %(gamt)s, %(total)s,
            %(total)s, NOW()
        )
    """, {
        "iid":   _inv_uuid,
        "pname": f"Courier — {carrier_name}"
                 + (f" (Tracking: {tracking_no})" if tracking_no else "")
                 + f" | Ref: {order_no}",
        "base":  base_amount,
        "gpct":  gst_percent,
        "gamt":  gst_amount,
        "total": float(rounded),
    })
    if not line_ok:
        import logging as _cl
        _cl.getLogger(__name__).warning("[courier_inv] invoice_lines INSERT failed for %s", inv_no)

    return inv_no


def _record_dispatch_courier_cost(
    *,
    order_id: str,
    order_no: str,
    challan_id: str,
    invoice_id: str,
    provider_id: str,
    provider_name: str,
    rate_option_id: str = "",
    rate_option_label: str = "",
    parcel_size: str = "",
    tracking_no: str = "",
    dispatch_date=None,
    charge_base: float = 0.0,
    gst_percent: float = 0.0,
    gst_amount: float = 0.0,
    total_amount: float = 0.0,
    billing_added: bool = False,
    remarks: str = "",
) -> None:
    if not provider_name and charge_base <= 0:
        return
    _invoice_uuid = ""
    try:
        from modules.sql_adapter import as_uuid_or_none
        _invoice_uuid = as_uuid_or_none(invoice_id) or ""
        if not _invoice_uuid and str(invoice_id or "").strip():
            _inv_rows = _q(
                "SELECT id::text AS id FROM invoices WHERE invoice_no=%(n)s LIMIT 1",
                {"n": str(invoice_id or "").strip()},
            ) or []
            _invoice_uuid = str(_inv_rows[0].get("id") or "") if _inv_rows else ""
    except Exception:
        _invoice_uuid = ""
    _rw("""
        INSERT INTO dispatch_courier_costs (
            id, order_id, order_no, challan_id, invoice_id,
            courier_provider_id, courier_provider_name,
            courier_rate_option_id, courier_rate_option_label, courier_parcel_size,
            tracking_no,
            dispatch_date, charge_base, gst_percent, gst_amount, total_amount,
            billing_added, payout_status, remarks, created_at
        ) VALUES (
            gen_random_uuid(), NULLIF(%(oid)s,'')::uuid, %(ono)s,
            NULLIF(%(cid)s,'')::uuid, NULLIF(%(iid)s,'')::uuid,
            NULLIF(%(pid)s,'')::uuid, %(pname)s,
            NULLIF(%(roid)s,'')::uuid, %(rlabel)s, %(parcel)s,
            %(track)s,
            %(ddate)s, %(base)s, %(gpct)s, %(gamt)s, %(total)s,
            %(billed)s, 'PENDING', %(remarks)s, NOW()
        )
    """, {
        "oid": order_id or "",
        "ono": order_no or "",
        "cid": challan_id or "",
        "iid": _invoice_uuid,
        "pid": provider_id or "",
        "pname": provider_name or "",
        "roid": rate_option_id or "",
        "rlabel": rate_option_label or "",
        "parcel": parcel_size or "",
        "track": tracking_no or "",
        "ddate": dispatch_date,
        "base": round(float(charge_base or 0), 2),
        "gpct": round(float(gst_percent or 0), 2),
        "gamt": round(float(gst_amount or 0), 2),
        "total": round(float(total_amount or 0), 2),
        "billed": bool(billing_added),
        "remarks": remarks or "",
    })


def _recent_party_courier_charges(
    party_id: str,
    order_id: str = "",
    party_name: str = "",
    days: int = 30,
) -> List[Dict]:
    if not party_id and not party_name:
        return []
    return _q("""
        SELECT
            csc.id::text AS charge_id,
            csc.challan_id::text AS challan_id,
            csc.order_id::text AS order_id,
            COALESCE(ch.challan_no, '') AS challan_no,
            inv.id::text AS invoice_id,
            COALESCE(inv.invoice_no, '') AS invoice_no,
            COALESCE(csc.description, '') AS description,
            COALESCE(csc.base_amount, 0)::numeric AS base_amount,
            COALESCE(csc.gst_percent, 0)::numeric AS gst_percent,
            COALESCE(csc.gst_amount, 0)::numeric AS gst_amount,
            COALESCE(csc.total_amount, 0)::numeric AS total_amount,
            COALESCE(ch.created_at, csc.created_at)::date::text AS doc_date,
            CASE WHEN csc.order_id::text = %(oid)s THEN TRUE ELSE FALSE END AS same_order
        FROM challan_service_charges csc
        JOIN challans ch ON ch.id = csc.challan_id
        -- Join orders to get party_name (challans may not store party_name directly)
        LEFT JOIN orders o_ch ON o_ch.id::text = ANY(ch.order_ids::text[])
        LEFT JOIN invoices inv ON inv.order_ids::text[] @> ARRAY[o_ch.id::text]
             AND COALESCE(inv.is_deleted, FALSE) = FALSE
             AND COALESCE(inv.status, '') NOT IN ('CANCELLED','VOID')
        WHERE (
              (%(pid)s <> '' AND ch.party_id = NULLIF(%(pid)s,'')::uuid)
           OR (%(pname)s <> '' AND LOWER(COALESCE(
                   o_ch.party_name, o_ch.patient_name, '')) = LOWER(%(pname)s))
        )
          AND UPPER(COALESCE(csc.charge_type,'')) = 'COURIER'
          AND COALESCE(ch.is_deleted, FALSE) = FALSE
          AND COALESCE(ch.status,'') NOT IN ('CANCELLED','VOID')
          AND COALESCE(ch.created_at, csc.created_at) >= NOW() - (%(days)s || ' days')::interval
        ORDER BY same_order DESC, COALESCE(ch.created_at, csc.created_at) DESC
        LIMIT 20
    """, {
        "pid": party_id or "",
        "pname": party_name or "",
        "oid": order_id or "",
        "days": int(days or 30),
    }) or []


def _party_doc_context(order_id: str) -> Dict:
    rows = _q("""
        SELECT o.id::text AS order_id,
               o.party_id::text AS party_id,
               COALESCE(o.party_name, p.party_name, '') AS party_name,
               COALESCE(p.gstin, '') AS party_gstin,
               COALESCE(p.state_name, '') AS state_name
        FROM orders o
        LEFT JOIN parties p ON p.id = o.party_id
        WHERE o.id = %(oid)s::uuid
        LIMIT 1
    """, {"oid": order_id}) or []
    return rows[0] if rows else {}


def _issue_courier_credit_note(charge: Dict, order_id: str, created_by: str) -> tuple:
    invoice_id = charge.get("invoice_id")
    invoice_no = charge.get("invoice_no")
    if not invoice_id or not invoice_no:
        return False, "Courier charge is not linked to an invoice yet. Convert to invoice first, then issue credit note."
    ctx = _party_doc_context(order_id)
    taxable = float(charge.get("base_amount") or 0)
    gst_pct = float(charge.get("gst_percent") or 0)
    if taxable <= 0:
        return False, "Selected courier charge has no taxable amount."
    try:
        from modules.billing.credit_debit_note_manager import create_credit_note
        ok, result = create_credit_note(
            invoice_no=invoice_no,
            invoice_id=invoice_id,
            order_id=order_id,
            party_id=ctx.get("party_id"),
            party_name=ctx.get("party_name") or "",
            party_gstin=ctx.get("party_gstin") or "",
            place_of_supply=ctx.get("state_name") or "",
            supply_type="B2B" if ctx.get("party_gstin") else "B2C",
            reason="RATE_DIFF",
            reason_detail="Duplicate courier charge credited from dispatch",
            lines=[{
                "product_name": "Courier Charge Credit",
                "quantity": 1,
                "unit_price": taxable,
                "taxable_amount": taxable,
                "gst_percent": gst_pct,
                "hsn_sac_code": "996812",
            }],
            remarks=f"Courier duplicate credit against {charge.get('challan_no') or ''} / {invoice_no}: {charge.get('description') or ''}",
            created_by=created_by or "Dispatch",
        )
        return ok, result
    except Exception as exc:
        return False, str(exc)


# ──────────────────────────────────────────────────────────────
# NEW DISPATCH FORM
# ──────────────────────────────────────────────────────────────

def _render_new_dispatch_form(
    order: Dict,
    billing_docs: List[Dict],
    summary: Dict,
    doc_lines: List[Dict] = None,
) -> None:
    order_id = str(order.get("id") or "")
    order_no = order.get("order_no") or "—"
    doc = _dispatch_doc_context(order)
    key_suffix = str(order.get("dispatch_key") or order_id or "order")

    # ── WhatsApp notification panel (shown immediately after dispatch save) ──
    # Must be checked BEFORE the early return so it shows even when
    # total_remaining = 0 (i.e. after a full dispatch just completed).
    _wa_data_early = st.session_state.get(f"wa_dispatch_ready_{order_no}")
    if _wa_data_early:
        st.markdown("---")
        st.markdown("#### 📲 Send WhatsApp Dispatch Notification")
        _default_mob = str(_wa_data_early.get("mobile") or "")
        _wa_mob = st.text_input(
            "Mobile Number",
            value=_default_mob,
            key=f"wa_mob_edit_{order_no}",
            placeholder="Enter 10-digit mobile number",
            help="Pre-filled from order. Change if needed.",
        )
        try:
            from modules.wa_hub import wa_panel, wa_dispatched
            from modules.settings.shop_master import get_unit_info
            _sh   = get_unit_info("wholesale")
            _wmsg = wa_dispatched(
                party         = _wa_data_early.get("party_name",""),
                order_no      = _wa_data_early.get("order_no",""),
                courier       = _wa_data_early.get("carrier",""),
                tracking      = _wa_data_early.get("tracking",""),
                items         = _wa_data_early.get("items",[]),
                order_type    = "dispatch",
                hand_delivery = _wa_data_early.get("hand_delivery", False),
                shop_name     = _sh.get("shop_name","DV Optical"),
                phone         = _sh.get("shop_phone",""),
            )
            def _after_wa_sent():
                _mark_wa_sent(
                    order_id,
                    order_no,
                    _wa_mob,
                    mark_delivered=bool(_wa_data_early.get("hand_delivery", False)),
                    delivery_notes=_wa_data_early.get("tracking") or "Hand delivery confirmed",
                )
                st.session_state.pop(f"wa_dispatch_ready_{order_no}", None)

            wa_panel(
                _wa_mob, _wmsg,
                key=f"wa_dispatch_{order_no}",
                title="📲 WhatsApp — Order Dispatched",
                expanded=True,
                party_name=str(_wa_data_early.get("party_name","") or ""),
                order_id=str(order_id or ""),
                on_sent_callback=_after_wa_sent,
            )
        except Exception as _we:
            _item_text = "\n".join(_wa_data_early.get("items",[]))
            _fallback = "Dear " + _wa_data_early.get("party_name","Customer") + ",\n"
            _fallback += "Your order " + order_no + " has been dispatched.\n"
            _fallback += "Courier: " + _wa_data_early.get("carrier","") + "\n"
            _fallback += "Tracking: " + _wa_data_early.get("tracking","") + "\n"
            _fallback += ("Items:\n" + _item_text) if _item_text else ""
            st.text_area("WhatsApp Message (copy manually)",
                         value=_fallback, height=140,
                         key=f"wa_fallback_{order_no}")
            if _wa_mob:
                _clean = _wa_mob.strip().lstrip("+").lstrip("91").lstrip("0")
                st.markdown(f"[📲 Open WhatsApp](https://wa.me/91{_clean})",
                            unsafe_allow_html=False)
        if st.button("✕ Dismiss WhatsApp panel",
                       key=f"wa_dismiss_{order_no}"):
            st.session_state.pop(f"wa_dispatch_ready_{order_no}", None)
            st.rerun()
        st.markdown("---")

    if summary.get("total_remaining", 0) == 0 and summary.get("total_billed", 0) > 0:
        if not _wa_data_early:
            st.success("✅ All billed quantities have been dispatched.")
            st.caption("Use the **Confirm Delivery** tab to mark delivery received.")
        return

    billed_lines = list(doc_lines or [])
    if not billed_lines:
        billed_lines = get_billed_lines_for_order(order_id)
    if not billed_lines:
        billed_lines = _fallback_billed_lines_for_order(order_id)
    dispatchable = [l for l in billed_lines if int(l.get("remaining_qty") or 0) > 0]

    if not dispatchable:
        st.info("No lines with pending dispatch qty.")
        return

    st.markdown("#### 🚚 Courier / Delivery Details")

    _ensure_dispatch_service_schema()
    _couriers = _fetch_courier_providers()
    _provider_by_id = {str(c.get("id")): c for c in _couriers}
    _provider_by_name = {str(c.get("provider_name") or "").upper(): c for c in _couriers}
    preferred_provider_id = str(order.get("preferred_courier_provider_id") or "")
    preferred_provider_name = str(order.get("preferred_courier_name") or "")
    if preferred_provider_id and preferred_provider_id not in _provider_by_id:
        preferred_provider_id = ""
    if not preferred_provider_id and preferred_provider_name:
        _pref = _provider_by_name.get(preferred_provider_name.upper())
        preferred_provider_id = str(_pref.get("id")) if _pref else ""

    _HAND_OPTIONS = [
        "🤝 Hand Delivery (Store)",
        "🏠 Staff Delivery",
        "🛵 Porter / Local Delivery",
    ]
    _courier_labels = {
        str(c.get("id")): (
            f"{c.get('provider_name') or ''}"
            + (" · GST" if c.get("gst_registered") else " · Non-GST")
        )
        for c in _couriers
    }
    _courier_opts = (
        ["— Select Courier / Method —"]
        + _HAND_OPTIONS
        + (["─────────────────"] if _couriers else [])
        + [str(c.get("id")) for c in _couriers]
    )

    def _fmt_courier_option(v: str) -> str:
        return _courier_labels.get(v, v)

    _default_idx = 0
    if preferred_provider_id in _courier_opts:
        _default_idx = _courier_opts.index(preferred_provider_id)

    col_r1, col_r2 = st.columns(2)
    with col_r1:
        selected_courier_key = st.selectbox(
            "Courier Company *",
            _courier_opts,
            index=_default_idx,
            format_func=_fmt_courier_option,
            key="dp_courier_sel",
            help="Select from service management registered couriers.",
        )
        selected_provider = _provider_by_id.get(selected_courier_key) or {}
        courier_company_id = str(selected_provider.get("id") or "")
        carrier_name = (
            selected_provider.get("provider_name")
            if courier_company_id
            else selected_courier_key
            if selected_courier_key != "— Select Courier / Method —"
            else ""
        )

        _rate_info = _courier_rate_for_provider(courier_company_id)
        _courier_rate = float(_rate_info.get("purchase_rate") or 0)
        _provider_gst_registered = bool(selected_provider.get("gst_registered"))
        _default_gst_pct = float(
            selected_provider.get("default_gst_percent")
            if selected_provider.get("default_gst_percent") not in (None, "")
            else _rate_info.get("gst_percent") or 0
        )
        if not _provider_gst_registered:
            _default_gst_pct = 0.0

        _sel_upper = str(selected_courier_key or "").upper()
        is_hand_delivery = (
            selected_courier_key in _HAND_OPTIONS
            or _sel_upper.startswith("HAND")
            or _sel_upper.startswith("STAFF")
            or _sel_upper.startswith("PORTER")
        )
        route_code = "HAND" if is_hand_delivery else "COURIER"
        selected_route_label = carrier_name or selected_courier_key
        delivery_method_selected = (
            bool(selected_courier_key)
            and selected_courier_key != "— Select Courier / Method —"
            and not str(selected_courier_key).startswith("─")
        )
        needs_tracking = delivery_method_selected and not is_hand_delivery

        if preferred_provider_id and courier_company_id and courier_company_id != preferred_provider_id:
            st.warning(
                f"Preferred courier for this party is {preferred_provider_name or _provider_by_id[preferred_provider_id].get('provider_name')}. "
                "You can change it for this dispatch, but it will be recorded."
            )
        if courier_company_id:
            st.caption(
                f"Provider GST: {'Yes' if _provider_gst_registered else 'No'}"
                + (f" · default {float(_default_gst_pct):.2f}%" if _provider_gst_registered else "")
            )
        elif not _couriers:
            st.caption("Create COURIER providers in Service Management to make them available here.")

    with col_r2:
        tracking_no = st.text_input(
            "Tracking / Docket No" if not is_hand_delivery else "Delivery Notes (optional)",
            key="dp_tracking",
            placeholder="AWB / docket number" if not is_hand_delivery
                        else "e.g. Delivered to reception, Left with neighbour…",
            disabled=False,
        )

    selected_rate_option = {}
    selected_rate_option_id = ""
    selected_rate_option_label = ""
    selected_parcel_size = ""
    _rate_options = _courier_rate_options_for_provider(courier_company_id)
    if courier_company_id and not is_hand_delivery:
        _rate_keys = [""] + [str(r.get("id") or "") for r in _rate_options]
        _ai_hint = _courier_ai_pack_hint(dispatchable, _rate_options)
        _ai_rate_id = str(_ai_hint.get("option_id") or "")
        _rate_index = _rate_keys.index(_ai_rate_id) if _ai_rate_id in _rate_keys else 0

        def _fmt_rate_option(v: str) -> str:
            if not v:
                return "Provider default / manual"
            r = next((x for x in _rate_options if str(x.get("id") or "") == v), {})
            code = str(r.get("parcel_size_code") or "").strip()
            label = str(r.get("option_label") or "").strip()
            amt = float(r.get("charge_base") or 0)
            gst = float(r.get("gst_percent") or 0)
            return f"{label}{' · ' + code if code else ''} — ₹{amt:,.2f} + GST {gst:.2f}%"

        selected_rate_option_id = st.selectbox(
            "Courier charge slab / parcel size",
            _rate_keys,
            index=_rate_index,
            format_func=_fmt_rate_option,
            key=f"dp_courier_rate_option_{key_suffix}_{selected_courier_key}",
            help="Maintain these options in Service Management → Providers & Rates.",
        )
        if _ai_hint.get("reason"):
            st.caption(_ai_hint["reason"])
        selected_rate_option = next(
            (x for x in _rate_options if str(x.get("id") or "") == selected_rate_option_id),
            {},
        )
        if selected_rate_option:
            selected_rate_option_label = str(selected_rate_option.get("option_label") or "")
            selected_parcel_size = str(selected_rate_option.get("parcel_size_code") or "")
            _courier_rate = float(selected_rate_option.get("charge_base") or 0)
            _default_gst_pct = float(selected_rate_option.get("gst_percent") or _default_gst_pct or 0)
        elif not _rate_options:
            st.caption("No parcel slabs defined for this courier yet. Using provider default/manual charge.")

    col_c1, col_c2 = st.columns(2)
    with col_c1:
        dispatch_date = st.date_input(
            "Dispatch Date *",
            value=datetime.date.today(),
            key="dp_date",
        )
    with col_c2:
        courier_charge = st.number_input(
            "Courier Cost / Charge ₹",
            min_value=0.0,
            value=_courier_rate,
            step=1.0,
            format="%.2f",
            key=f"dp_courier_charge_{key_suffix}_{selected_courier_key}",
            help="Auto-filled from service rate. Edit if actual charge differs.",
        )

    col_c3, col_c4 = st.columns(2)
    with col_c3:
        # GST Law: courier service to customer always attracts 18% GST.
        # Even if provider doesn't give GST invoice, WE are supplying a service.
        # Default always 18%. Staff can reduce to 0 only if truly exempt.
        _bill_gst_default = 0.0 if is_hand_delivery else (
            float(_default_gst_pct) if float(_default_gst_pct) > 0 else 18.0
        )
        courier_gst_percent = st.number_input(
            "Courier GST % (charged to customer)",
            min_value=0.0,
            max_value=28.0,
            value=_bill_gst_default,
            step=0.5,
            format="%.2f",
            key=f"dp_courier_gst_pct_{key_suffix}_{selected_courier_key}",
            help="GST law: courier service = 18% GST. Reduce to 0 only if truly exempt.",
            disabled=is_hand_delivery,
        )
    courier_gst_amount = round(float(courier_charge or 0) * float(courier_gst_percent or 0) / 100.0, 2)
    courier_total_amount = round(float(courier_charge or 0) + courier_gst_amount, 2)
    _order_existing_courier = _existing_courier_billing_for_orders([order_id])
    with col_c4:
        _existing_courier_charges = _recent_party_courier_charges(
            str(order.get("party_id") or ""),
            order_id=order_id,
            party_name=str(order.get("party_name") or ""),
            days=30,
        )
        _has_existing_courier = bool(_existing_courier_charges)
        if _order_existing_courier and not is_hand_delivery:
            st.error(
                "COURIER ALREADY ADDED FOR THIS ORDER. Do not add courier again unless this is a second parcel/extra courier charge."
            )
            for _ec in _order_existing_courier[:4]:
                st.caption(
                    f"{_ec.get('source')} · {(_ec.get('order_no') or order_no)} · "
                    f"₹{float(_ec.get('amount') or 0):,.2f} · {_ec.get('description') or ''}"
                )
        add_courier_to_billing = st.checkbox(
            "Add courier charge to billing",
            value=bool(courier_company_id and courier_charge > 0 and not _has_existing_courier and not _order_existing_courier),
            key="dp_add_courier_billing",
            help="If unticked, courier cost is stored for provider payout only and is not added to challan/invoice totals.",
            disabled=is_hand_delivery or courier_charge <= 0,
        )
        st.caption(f"Courier total: ₹{courier_total_amount:,.2f}")

    courier_credit_charge = None
    courier_billing_action = "NO_CHARGE"
    if not is_hand_delivery and delivery_method_selected:
        st.markdown("##### Courier Billing Check")
        if _existing_courier_charges:
            total_existing = sum(float(r.get("total_amount") or 0) for r in _existing_courier_charges)
            st.warning(
                f"{len(_existing_courier_charges)} courier charge(s) already found for this party in the last 30 days "
                f"(₹{total_existing:,.2f}). Charge only once if these orders are going in one parcel."
            )
            labels = []
            for i, r in enumerate(_existing_courier_charges):
                labels.append(
                    f"{i+1}. {r.get('doc_date') or ''} · {r.get('challan_no') or ''}"
                    + (f" / {r.get('invoice_no')}" if r.get("invoice_no") else " / no invoice")
                    + f" · ₹{float(r.get('total_amount') or 0):,.2f}"
                    + (" · same order" if r.get("same_order") else "")
                )
            with st.expander("View existing courier charges", expanded=True):
                for label, row in zip(labels, _existing_courier_charges):
                    st.caption(label + (f" · {row.get('description')}" if row.get("description") else ""))
            action_labels = [
                "Do not add another courier charge",
                "Add courier charge to this order also",
                "Issue credit note for one duplicate courier charge",
            ]
            action = st.radio(
                "Courier accounting action",
                action_labels,
                index=0,
                key="dp_courier_billing_action",
                help="Use credit note only when courier was wrongly charged twice on invoices.",
            )
            if action.startswith("Add"):
                courier_billing_action = "ADD_CHARGE"
                add_courier_to_billing = True
            elif action.startswith("Issue"):
                courier_billing_action = "CREDIT_DUPLICATE"
                idx = st.selectbox(
                    "Select courier charge to credit",
                    range(len(labels)),
                    format_func=lambda i: labels[int(i)],
                    key="dp_courier_credit_pick",
                )
                courier_credit_charge = _existing_courier_charges[int(idx)]
                add_courier_to_billing = False
            else:
                add_courier_to_billing = False
        else:
            st.success("No recent courier charge found for this party. You may add courier to billing if required.")
            courier_billing_action = "ADD_CHARGE" if add_courier_to_billing else "NO_CHARGE"
    _duplicate_courier_rows = list(_order_existing_courier or []) + list(_existing_courier_charges or [])
    courier_duplicate_confirmed = False
    if add_courier_to_billing and _duplicate_courier_rows and not is_hand_delivery:
        st.error(
            "🚨 COURIER CHARGE ALREADY EXISTS. Tick confirmation only if this is genuinely a new parcel / extra courier."
        )
        courier_duplicate_confirmed = st.checkbox(
            "Yes, add another courier charge and record my confirmation in audit log",
            key=f"dp_confirm_duplicate_courier_{key_suffix}",
        )

    col_d1, col_d2 = st.columns(2)
    with col_d1:
        dispatched_by = st.text_input(
            "Dispatched By *",
            value=st.session_state.get("user_name", ""),
            key=f"dp_by_{key_suffix}",
        )
    with col_d2:
        remarks = st.text_input(
            "Remarks",
            key=f"dp_remarks_{key_suffix}",
            placeholder="Packing notes, special instructions…",
        )

    # Billing doc ref (auto-pick first active doc)
    billing_doc_ref = doc.get("ref") or ""
    if not billing_doc_ref and billing_docs:
        billing_doc_ref = f"{billing_docs[0].get('doc_type','')} {billing_docs[0].get('doc_no','')}".strip()

    # ── Dispatch mode ──────────────────────────────────────────────────────
    st.markdown("#### 📋 Lines to Dispatch")
    dispatch_mode = st.radio(
        "Mode",
        ["📦 Full dispatch (all remaining)", "⚡ Partial dispatch (custom qty per line)"],
        key=f"dp_mode_{key_suffix}",
        horizontal=True,
    )
    is_partial_mode = "Partial" in dispatch_mode

    line_qtys: Dict[str, int] = {}

    for line in dispatchable:
        lid       = str(line["id"])
        pname     = line.get("product_name") or "Lens"
        eye       = str(line.get("eye_side") or "")
        brand     = line.get("brand") or ""
        sph_val   = line.get("sph")
        remaining = int(line.get("remaining_qty") or 0)
        billed_q  = int(line.get("billing_qty") or 0)
        already   = int(line.get("already_dispatched") or 0)

        # ── Full product detail for dispatch line ──────────────────────
        cyl_val    = line.get("cyl")
        axis_val   = line.get("axis")
        add_val    = line.get("add_power")
        index_val  = line.get("lens_index") or ""
        coating    = line.get("coating") or ""
        treatment  = line.get("treatment") or ""
        bc_val     = line.get("bc") or ""
        dia_val    = line.get("dia") or ""
        colour_val = line.get("colour") or ""
        order_date = line.get("order_date") or ""
        route_val  = line.get("route") or ""

        eye_icon = {"R":"👁️R","RIGHT":"👁️R","L":"👁️L","LEFT":"👁️L",
                    "S":"⚙️","BOTH":"👁️R+L"}.get(eye.upper(), f"👁️{eye}" if eye else "")

        # Power string
        pwr_parts = []
        if sph_val is not None:  pwr_parts.append(f"SPH {float(sph_val):+.2f}")
        if cyl_val is not None:  pwr_parts.append(f"CYL {float(cyl_val):+.2f}")
        if axis_val:             pwr_parts.append(f"AX {int(axis_val)}")
        if add_val is not None:  pwr_parts.append(f"ADD {float(add_val):+.2f}")
        if bc_val:               pwr_parts.append(f"BC {bc_val}")
        if dia_val:              pwr_parts.append(f"Dia {dia_val}")
        if colour_val:           pwr_parts.append(f"Colour: {colour_val}")
        pwr_str = "  ·  ".join(pwr_parts)

        # Spec string: index + coating + treatment
        spec_parts = []
        if index_val: spec_parts.append(f"Index {index_val}")
        if coating:   spec_parts.append(coating)
        if treatment: spec_parts.append(treatment)
        spec_str = "  ·  ".join(spec_parts)

        detail_html = (
            f"<div style='background:#0f1e35;border:1px solid #1e3a5f;"
            f"border-left:3px solid #6366f1;border-radius:6px;padding:7px 12px;margin:3px 0'>"
            f"<div style='font-size:0.83rem;font-weight:700;color:#e2e8f0'>"
            f"{eye_icon}  {pname}  "
            f"<span style='color:#94a3b8;font-weight:400'>{brand}</span></div>"
            + (f"<div style='font-size:0.72rem;color:#a5b4fc;margin-top:2px'>"
               f"💊 {pwr_str}</div>" if pwr_str else "")
            + (f"<div style='font-size:0.70rem;color:#67e8f9;margin-top:1px'>"
               f"🔬 {spec_str}</div>" if spec_str else "")
            + (f"<div style='font-size:0.68rem;color:#64748b;margin-top:2px'>"
               f"Billed: {billed_q}  ·  Sent: {already}  ·  Remaining: {remaining}"
               f"{'  ·  📅 ' + order_date if order_date else ''}"
               f"{'  ·  ' + route_val if route_val else ''}"
               f"</div>")
            + f"</div>"
        )

        if is_partial_mode:
            with st.container(border=True):
                ll, lr = st.columns([5, 3])
                with ll:
                    st.markdown(detail_html, unsafe_allow_html=True)
                with lr:
                    qty = st.number_input(
                        "Qty", min_value=0, max_value=remaining,
                        value=remaining, step=1, key=f"dp_qty_{key_suffix}_{lid}",
                        label_visibility="collapsed",
                    )
        else:
            qty = remaining
            st.markdown(detail_html, unsafe_allow_html=True)
        line_qtys[lid] = qty

    # Preview totals
    total_dispatching = sum(line_qtys.values())
    remaining_after   = summary.get("total_remaining", 0) - total_dispatching
    will_be_partial   = remaining_after > 0

    if total_dispatching > 0:
        disp_color = "#f59e0b" if will_be_partial else "#10b981"
        st.markdown(
            f"<div style='background:{disp_color}18;border:1px solid {disp_color}44;"
            f"border-radius:8px;padding:8px 14px;margin:8px 0;"
            f"color:{disp_color};font-weight:700;font-size:0.82rem'>"
            f"{'⚡ Partial dispatch' if will_be_partial else '📦 Full dispatch'}:  "
            f"{total_dispatching} unit(s)"
            + (f"  ·  {remaining_after} unit(s) will remain pending" if will_be_partial else "  ·  all billed qty dispatched")
            + "</div>",
            unsafe_allow_html=True,
        )

    # Tracking URL preview
    if tracking_no and route_code:
        t_url = LogisticsRoute.tracking_url(route_code, tracking_no.strip())
        if t_url:
            st.markdown(
                f"<div style='font-size:0.72rem;color:#3b82f6'>"
                f"🔗 <a href='{t_url}' target='_blank' rel='noopener'>Track this shipment</a></div>",
                unsafe_allow_html=True,
            )

    # ── Submit ─────────────────────────────────────────────────────────────
    if st.button(
        "🚚 Save Dispatch",
        type="primary",
        width='stretch',
        key=f"dp_submit_{key_suffix}",
        disabled=(total_dispatching == 0),
    ):
        errors = []
        if not delivery_method_selected:
            errors.append("Courier / delivery method is required")
        if not dispatched_by.strip():
            errors.append("Dispatched By is required")
        if needs_tracking and not tracking_no.strip():
            errors.append(f"Tracking number is required for {selected_route_label}")
        if total_dispatching <= 0:
            errors.append("Dispatch quantity must be at least 1")
        if add_courier_to_billing and _duplicate_courier_rows and not courier_duplicate_confirmed:
            errors.append("Courier already exists. Confirm the duplicate/extra courier charge before saving.")

        if errors:
            for e in errors:
                st.error(f"❌ {e}")
        else:
            if add_courier_to_billing and _duplicate_courier_rows and courier_duplicate_confirmed:
                _log_courier_duplicate_confirmation(
                    [order_id],
                    amount=float(courier_total_amount or 0),
                    provider_name=carrier_name,
                    reason="Dispatch user confirmed another courier charge for order with existing courier.",
                    user=dispatched_by.strip() or st.session_state.get("user_name", "Dispatch"),
                )
            ok, msg = create_dispatch_event(
                order_id      = order_id,
                order_no      = order_no,
                route_code    = route_code,
                carrier_name  = carrier_name.strip() or selected_route_label,
                tracking_no   = tracking_no.strip(),
                dispatched_by = dispatched_by.strip(),
                dispatch_date = dispatch_date,
                line_qtys     = {k: v for k, v in line_qtys.items() if v > 0},
                billing_doc_ref = billing_doc_ref,
                remarks       = remarks.strip(),
            )
            if ok:
                st.success(msg)
                if is_hand_delivery:
                    _mark_latest_local_dispatch_delivered(
                        order_id,
                        confirmed_by=dispatched_by.strip(),
                        notes=tracking_no.strip() or selected_route_label,
                    )
                # ── WhatsApp notification ─────────────────────────────
                # Store dispatch result in session so WA panel shows
                # below with editable mobile number
                # ── Record courier provider cost and optional customer billing charge ──
                if carrier_name and carrier_name != "— Select Courier / Method —":
                    try:
                        _docs = _latest_billing_doc_ids(order_id)
                        _chal_id = doc.get("challan_id") or _docs.get("challan_id")
                        _inv_id = doc.get("invoice_id") or _docs.get("invoice_id")
                        _billing_added = False
                        if (
                            courier_charge > 0
                            and add_courier_to_billing
                            and not is_hand_delivery
                        ):
                            # Create standalone courier service invoice
                            # NEVER modify the existing challan/invoice — payments already reconciled
                            try:
                                _courier_inv_no = _create_courier_service_invoice(
                                    order_id     = order_id,
                                    order_no     = order_no,
                                    party_id     = str(order.get("party_id") or ""),
                                    party_name   = str(order.get("party_name") or ""),
                                    carrier_name = carrier_name,
                                    tracking_no  = tracking_no.strip(),
                                    base_amount  = round(float(courier_charge or 0), 2),
                                    gst_percent  = round(float(courier_gst_percent or 0), 2),
                                    gst_amount   = round(float(courier_gst_amount or 0), 2),
                                    total_amount = round(float(courier_total_amount or 0), 2),
                                    created_by   = dispatched_by.strip() or "dispatch",
                                )
                                if _courier_inv_no:
                                    st.info(f"📋 Courier invoice created: **{_courier_inv_no}**")
                                    _billing_added = True
                                    _inv_id = _courier_inv_no
                            except Exception as _ci_e:
                                st.warning(f"Courier invoice not created: {_ci_e}")

                        if courier_company_id or courier_charge > 0:
                            _record_dispatch_courier_cost(
                                order_id=order_id,
                                order_no=order_no,
                                challan_id=_chal_id or "",
                                invoice_id=_inv_id or "",
                                provider_id=courier_company_id,
                                provider_name=carrier_name,
                                rate_option_id=selected_rate_option_id,
                                rate_option_label=selected_rate_option_label,
                                parcel_size=selected_parcel_size,
                                tracking_no=tracking_no.strip(),
                                dispatch_date=dispatch_date,
                                charge_base=float(courier_charge or 0),
                                gst_percent=float(courier_gst_percent or 0),
                                gst_amount=float(courier_gst_amount or 0),
                                total_amount=float(courier_total_amount or 0),
                                billing_added=_billing_added,
                                remarks=remarks.strip(),
                            )
                        if courier_billing_action == "CREDIT_DUPLICATE" and courier_credit_charge:
                            _cn_ok, _cn_msg = _issue_courier_credit_note(
                                courier_credit_charge,
                                order_id=order_id,
                                created_by=dispatched_by.strip() or st.session_state.get("user_name", "Dispatch"),
                            )
                            if _cn_ok:
                                st.success(f"✅ Courier duplicate credited: {_cn_msg}")
                            else:
                                st.warning(f"Courier credit note not created: {_cn_msg}")
                        _rw("""
                            UPDATE order_dispatches
                               SET courier_provider_id = NULLIF(%(pid)s,'')::uuid,
                                   courier_rate_option_id = NULLIF(%(roid)s,'')::uuid,
                                   courier_rate_option_label = %(rlabel)s,
                                   courier_parcel_size = %(parcel)s,
                                   courier_charge = %(base)s,
                                   courier_gst_percent = %(gpct)s,
                                   courier_gst_amount = %(gamt)s,
                                   courier_total_amount = %(total)s,
                                   courier_billed = %(billed)s
                             WHERE ctid IN (
                                SELECT ctid
                                  FROM order_dispatches
                                 WHERE order_id = %(oid)s::uuid
                                   AND COALESCE(status,'DISPATCHED') <> 'CANCELLED'
                                   AND COALESCE(tracking_no,'') = COALESCE(%(track)s,'')
                                 ORDER BY created_at DESC
                                 LIMIT 1
                             )
                        """, {
                            "pid": courier_company_id or "",
                            "roid": selected_rate_option_id or "",
                            "rlabel": selected_rate_option_label or "",
                            "parcel": selected_parcel_size or "",
                            "base": round(float(courier_charge or 0), 2),
                            "gpct": round(float(courier_gst_percent or 0), 2),
                            "gamt": round(float(courier_gst_amount or 0), 2),
                            "total": round(float(courier_total_amount or 0), 2),
                            "billed": bool(_billing_added),
                            "oid": order_id,
                            "track": tracking_no.strip(),
                        })
                    except Exception as _ce:
                        import logging
                        logging.getLogger(__name__).warning(
                            "[dispatch] courier charge insert failed: %s", _ce
                        )

                _wa_items = _build_dispatch_items_text(dispatchable, line_qtys)
                st.session_state[f"wa_dispatch_ready_{order_no}"] = {
                    "order_no":     order_no,
                    "party_name":   order.get("party_name",""),
                    "mobile":       order.get("mobile","") or order.get("patient_mobile",""),
                    "carrier":      carrier_name.strip(),
                    "tracking":     tracking_no.strip(),
                    "route_code":   route_code,
                    "order_type":   order.get("order_type",""),
                    "hand_delivery": is_hand_delivery,
                    "items":        _wa_items,
                }
                st.rerun()
            else:
                st.error(f"❌ {msg}")




# ──────────────────────────────────────────────────────────────
# DISPATCH HISTORY
# ──────────────────────────────────────────────────────────────

def _render_dispatch_history(
    order_id: str,
    order_no: str,
    read_only: bool = False,
) -> None:
    history = get_dispatch_history(order_id)

    if not history:
        st.info("No dispatch events recorded yet.")
        return

    for ev in history:
        ev_status   = (ev.get("status") or "DISPATCHED").upper()
        is_partial  = bool(ev.get("is_partial"))
        carrier     = ev.get("carrier_name") or ev.get("route_code") or "—"
        tracking    = ev.get("tracking_no") or "—"
        dispatch_no = ev.get("dispatch_no") or "—"
        dispatch_id = str(ev.get("id") or "")
        dispatched_at = str(ev.get("dispatched_at") or "")[:10]
        dispatched_by = ev.get("dispatched_by") or "system"
        billing_ref = ev.get("billing_doc_ref") or ""
        remarks     = ev.get("remarks") or ""
        delivered_at = str(ev.get("delivered_at") or "")[:10]

        ev_color = {
            "DISPATCHED": "#3b82f6",
            "DELIVERED":  "#10b981",
            "CANCELLED":  "#ef4444",
        }.get(ev_status, "#64748b")

        ev_icon = {
            "DISPATCHED": "🚚",
            "DELIVERED":  "✅",
            "CANCELLED":  "❌",
        }.get(ev_status, "•")

        with st.container(border=True):
            h1, h2 = st.columns([5, 3])
            with h1:
                st.markdown(
                    f"<div style='font-weight:700;font-family:monospace;font-size:0.9rem'>"
                    f"{dispatch_no}</div>"
                    f"<div style='color:#94a3b8;font-size:0.75rem;margin-top:2px'>"
                    f"🚚 {carrier}  ·  "
                    f"{'🔢 ' + str(tracking) if tracking != '—' else 'no tracking'}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                if billing_ref:
                    st.caption(f"📋 {billing_ref}")
                if delivered_at:
                    st.caption(f"✅ Delivered: {delivered_at}")
            with h2:
                partial_badge = " ⚡ PARTIAL" if (is_partial and ev_status == "DISPATCHED") else ""
                st.markdown(
                    f"<span style='background:{ev_color};color:#fff;padding:2px 10px;"
                    f"border-radius:12px;font-size:0.72rem;font-weight:700'>"
                    f"{ev_icon} {ev_status}{partial_badge}</span>",
                    unsafe_allow_html=True,
                )
                st.caption(f"{dispatched_at} · {dispatched_by}")

            # Tracking URL
            route_code_ev = ev.get("route_code") or ""
            if route_code_ev and tracking != "—":
                t_url = LogisticsRoute.tracking_url(route_code_ev, str(tracking))
                if t_url:
                    st.markdown(
                        f"<div style='font-size:0.72rem'>"
                        f"🔗 <a href='{t_url}' target='_blank' rel='noopener'>Track shipment</a></div>",
                        unsafe_allow_html=True,
                    )

            lines = ev.get("lines") or []
            if lines:
                with st.expander(f"📋 {len(lines)} line(s)", expanded=False):
                    for dl in lines:
                        eye   = dl.get("eye_side") or ""
                        pname = dl.get("product_name") or "Lens"
                        brand = dl.get("brand") or ""
                        dqty  = int(dl.get("dispatched_qty") or 0)
                        rqty  = int(dl.get("remaining_qty") or 0)
                        sph   = dl.get("sph") or ""
                        eye_icon = {"R": "👁R", "RIGHT": "👁R",
                                    "L": "👁L", "LEFT": "👁L"}.get(str(eye).upper(), eye)
                        st.markdown(
                            f"<div style='font-size:0.78rem;padding:2px 0;color:#cbd5e1'>"
                            f"{eye_icon}  {pname}  {brand}"
                            f"{'  SPH ' + str(sph) if sph else ''}"
                            f"  — <b>{dqty} dispatched</b>"
                            + (f"  ·  {rqty} remaining" if rqty else "")
                            + "</div>",
                            unsafe_allow_html=True,
                        )

            if remarks:
                st.caption(f"📝 {remarks}")

            if not read_only and ev_status == "DISPATCHED" and dispatch_id:
                with st.expander("✅ Mark Delivered", expanded=False):
                    cdel1, cdel2 = st.columns(2)
                    delivery_date = cdel1.date_input(
                        "Delivery Date",
                        value=datetime.date.today(),
                        key=f"ord_hist_del_date_{dispatch_id}",
                    )
                    confirmed_by = cdel2.text_input(
                        "Confirmed By",
                        value=st.session_state.get("user_name", ""),
                        key=f"ord_hist_del_by_{dispatch_id}",
                    )
                    notes = st.text_input(
                        "Delivery Notes",
                        key=f"ord_hist_del_notes_{dispatch_id}",
                        placeholder="Recipient name, POD note, condition…",
                    )
                    if st.button(
                        f"✅ Confirm Delivered — {dispatch_no}",
                        key=f"ord_hist_del_go_{dispatch_id}",
                        type="primary",
                        use_container_width=True,
                    ):
                        ok, msg = confirm_delivery(
                            order_id=order_id,
                            dispatch_id=dispatch_id,
                            delivery_date=delivery_date,
                            confirmed_by=confirmed_by.strip() or "dispatch",
                            notes=notes.strip(),
                        )
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)


# ──────────────────────────────────────────────────────────────
# DELIVERY CONFIRMATION
# ──────────────────────────────────────────────────────────────

def _render_delivery_confirmation(order_id: str, order_no: str) -> None:
    history = get_dispatch_history(order_id)
    pending = [
        ev for ev in history
        if (ev.get("status") or "").upper() == "DISPATCHED"
    ]

    if not pending:
        st.info("No dispatches awaiting delivery confirmation.")
        return

    st.markdown("#### ✅ Confirm Delivery")
    st.caption(
        "Mark a dispatch event as delivered. "
        "When all lines are confirmed, order advances to DELIVERED."
    )

    for ev in pending:
        dispatch_no = ev.get("dispatch_no") or "—"
        carrier     = ev.get("carrier_name") or ev.get("route_code") or "—"
        tracking    = ev.get("tracking_no") or "—"
        is_partial  = bool(ev.get("is_partial"))
        dispatch_id = str(ev.get("id") or "")
        dispatched_at = str(ev.get("dispatched_at") or "")[:10]

        with st.container(border=True):
            st.markdown(
                f"**{dispatch_no}** · {carrier}"
                + (f" · {tracking}" if tracking != "—" else "")
                + ("  ⚡ *partial shipment*" if is_partial else "")
                + f"  <span style='color:#64748b;font-size:0.72rem'> sent {dispatched_at}</span>",
                unsafe_allow_html=True,
            )
            c1, c2 = st.columns(2)
            with c1:
                delivery_date = st.date_input(
                    "Delivery Date",
                    value=datetime.date.today(),
                    key=f"del_date_{dispatch_id}",
                )
            with c2:
                confirmed_by = st.text_input(
                    "Confirmed By",
                    value=st.session_state.get("user_name", ""),
                    key=f"del_by_{dispatch_id}",
                )
            notes = st.text_input(
                "Notes",
                key=f"del_notes_{dispatch_id}",
                placeholder="Recipient name, condition of goods, etc.",
            )

            if st.button(
                f"✅ Mark Delivered — {dispatch_no}",
                key=f"del_confirm_{dispatch_id}",
                width='stretch',
                type="primary",
            ):
                ok, msg = confirm_delivery(
                    order_id    = order_id,
                    dispatch_id = dispatch_id,
                    delivery_date = delivery_date,
                    confirmed_by  = confirmed_by.strip() or "system",
                    notes         = notes.strip(),
                )
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
