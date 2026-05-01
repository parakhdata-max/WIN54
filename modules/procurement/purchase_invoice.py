"""
Purchase Invoice Module (GRN — Goods Receipt Note)
====================================================

This is Step 2 of the purchase lifecycle:

  Smart Purchase Module  →  creates supplier_order [DRAFT]
          ↓  you send PO to supplier
  Purchase Invoice (GRN) →  goods arrive, you enter actual qty + price
          ↓
  inventory_stock updated, supplier_order → RECEIVED

Tables written:
  supplier_orders       → status updated to RECEIVED
  supplier_order_items  → received_qty, pending_qty updated
  supplier_order_status_history → audit trail
  inventory_stock       → new stock rows inserted (actual received)
  purchase_invoices     → invoice header (invoice no, date, amounts)
  purchase_invoice_lines → line-level receipt record

Run standalone:  streamlit run purchase_invoice.py
Embedded in app: call render_purchase_invoice()
"""

import streamlit as st
import pandas as pd
from datetime import datetime, date
import uuid
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from modules.sql_adapter import (
        get_connection,
        get_transaction_connection,
        execute_query,
        fetch_supplier_orders,
        update_supplier_order_status,
    )
    DB_CONNECTED = True
except ImportError:
    DB_CONNECTED = False

# =============================================================================
# CSS
# =============================================================================

_CSS = """
<style>
    .grn-card {
        background: #f8fafc; border: 1px solid #e2e8f0;
        border-radius: 10px; padding: 16px 20px; margin: 8px 0;
    }
    .grn-card-blue  { background:#eff6ff; border-color:#93c5fd; }
    .grn-card-green { background:#f0fdf4; border-color:#86efac; }
    .grn-card-amber { background:#fffbeb; border-color:#fcd34d; }
    .grn-card-red   { background:#fef2f2; border-color:#fca5a5; }

    .status-badge {
        display:inline-block; padding:3px 12px; border-radius:20px;
        font-size:12px; font-weight:700;
    }
    .s-draft    { background:#f3f4f6; color:#374151; }
    .s-sent     { background:#dbeafe; color:#1e40af; }
    .s-partial  { background:#fef3c7; color:#92400e; }
    .s-received { background:#d1fae5; color:#065f46; }

    .step-bar { display:flex; gap:0; margin:16px 0 20px; }
    .step-item {
        flex:1; text-align:center; padding:9px 4px;
        background:#f8fafc; border:1px solid #e2e8f0;
        font-size:13px; font-weight:500; color:#94a3b8;
    }
    .step-item:first-child { border-radius:8px 0 0 8px; }
    .step-item:last-child  { border-radius:0 8px 8px 0; }
    .step-active { background:#1e40af; color:#fff; border-color:#1e40af; }
    .step-done   { background:#d1fae5; color:#065f46; border-color:#6ee7b7; }

    .stButton > button { border-radius:8px; font-weight:600; }
</style>
"""


# =============================================================================
# DATA ACCESS
# =============================================================================

@st.cache_data(ttl=60)
def load_pending_pos() -> list:
    """Load POs that are DRAFT or SENT — eligible to receive against."""
    if not DB_CONNECTED:
        return _mock_pos()
    try:
        all_orders = fetch_supplier_orders()
        return [o for o in all_orders
                if o.get("status") in ("DRAFT", "SENT", "CONFIRMED", "PARTIAL")]
    except Exception as e:
        st.error(f"Could not load purchase orders: {e}")
        return _mock_pos()


@st.cache_data(ttl=60)
def load_invoice_history() -> pd.DataFrame:
    """Load past purchase invoices."""
    if not DB_CONNECTED:
        return _mock_invoice_history()
    sql = """
        SELECT
            pi.invoice_no,
            pi.supplier_order_id,
            pi.supplier_name,
            pi.invoice_date,
            pi.supplier_invoice_no,
            pi.total_items,
            pi.total_qty_received,
            pi.subtotal,
            pi.gst_amount,
            pi.invoice_total,
            pi.payment_status,
            pi.created_at
        FROM purchase_invoices pi
        ORDER BY pi.created_at DESC
        LIMIT 200
    """
    try:
        return execute_query(sql, "purchase_invoices")
    except Exception:
        return _mock_invoice_history()


def save_purchase_invoice(po: dict, lines: list,
                          invoice_meta: dict) -> tuple:
    """
    One atomic transaction:
      1. INSERT purchase_invoices (header)
      2. INSERT purchase_invoice_lines (one per received item)
      3. UPDATE supplier_order_items → received_qty, pending_qty, item_status
      4. INSERT inventory_stock rows (actual qty at actual price)
      5. UPDATE supplier_orders → status (RECEIVED or PARTIAL)
      6. INSERT supplier_order_status_history

    Returns (success: bool, invoice_no: str, message: str)
    """
    if not DB_CONNECTED:
        inv_no = f"PINV-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        return True, inv_no, "Demo mode"

    conn = cur = None
    now  = datetime.now()
    inv_no = invoice_meta["invoice_no"]

    try:
        conn = get_transaction_connection()
        cur  = conn.cursor()

        # ── Totals ───────────────────────────────────────────────────────
        total_qty  = sum(l["received_qty"] for l in lines)
        subtotal   = sum(l["received_qty"] * l["actual_price"] for l in lines)
        gst_total  = sum(
            l["received_qty"] * l["actual_price"] * l.get("gst_percent", 18) / 100
            for l in lines
        )
        inv_total  = subtotal + gst_total

        # ── 1. Invoice header ─────────────────────────────────────────────
        cur.execute("""
            INSERT INTO purchase_invoices (
                invoice_no, supplier_order_id,
                supplier_id, supplier_name,
                supplier_invoice_no, invoice_date,
                total_items, total_qty_received,
                subtotal, gst_amount, invoice_total,
                payment_terms, payment_status,
                notes, created_by, created_at, updated_at
            ) VALUES (
                %s,%s,
                %s,%s,
                %s,%s,
                %s,%s,
                %s,%s,%s,
                %s,'UNPAID',
                %s,%s,%s,%s
            )
            ON CONFLICT (invoice_no) DO NOTHING
        """, (
            inv_no,                          po["supplier_order_id"],
            po.get("supplier_id",""),        po.get("supplier_name",""),
            invoice_meta.get("supplier_invoice_no",""),
            invoice_meta["invoice_date"],
            len(lines),                      total_qty,
            subtotal,                        gst_total,    inv_total,
            po.get("payment_terms","NET30"),
            invoice_meta.get("notes",""),
            "purchase_invoice",              now,          now,
        ))

        # ── 2–4. Per line: invoice line + stock update + inventory ────────
        all_received = True

        for item_no, line in enumerate(lines, 1):
            rqty   = int(line["received_qty"])
            aprice = float(line["actual_price"])
            gst_p  = float(line.get("gst_percent", 18))
            line_total = rqty * aprice * (1 + gst_p / 100)

            # 2. Invoice line
            cur.execute("""
                INSERT INTO purchase_invoice_lines (
                    id, invoice_no, item_no,
                    supplier_order_id, supplier_order_item_no,
                    product_id, product_name, brand,
                    eye_side, sph, cyl, axis, add_power,
                    batch_no, expiry_date,
                    ordered_qty, received_qty,
                    actual_price, gst_percent, line_total,
                    created_at
                ) VALUES (
                    %s,%s,%s,
                    %s,%s,
                    %s,%s,%s,
                    %s,%s,%s,%s,%s,
                    %s,%s,
                    %s,%s,
                    %s,%s,%s,
                    %s
                )
            """, (
                str(uuid.uuid4()), inv_no, item_no,
                po["supplier_order_id"], line.get("item_no", item_no),
                line.get("product_id"),  line["product_name"],
                line.get("brand",""),
                line.get("eye_side"),
                line.get("sph"),   line.get("cyl"),
                line.get("axis"),  line.get("add_power"),
                line.get("batch_no",""),
                line.get("expiry_date"),
                line.get("ordered_qty", 0), rqty,
                aprice, gst_p, line_total,
                now,
            ))

            # 3. Update supplier_order_items
            cur.execute("""
                UPDATE supplier_order_items
                SET received_qty = COALESCE(received_qty, 0) + %s,
                    pending_qty  = GREATEST(0, ordered_qty
                                   - COALESCE(received_qty, 0) - %s),
                    item_status  = CASE
                        WHEN ordered_qty <= COALESCE(received_qty, 0) + %s
                        THEN 'RECEIVED'
                        ELSE 'PARTIAL'
                    END
                WHERE supplier_order_id = %s
                  AND item_no = %s
            """, (
                rqty, rqty, rqty,
                po["supplier_order_id"],
                line.get("item_no", item_no),
            ))

            # Check if fully received
            ordered = int(line.get("ordered_qty", 0))
            already = int(line.get("already_received", 0))
            if already + rqty < ordered:
                all_received = False

            # 4. Add to inventory_stock
            if rqty > 0 and line.get("product_id"):
                cur.execute("""
                    INSERT INTO inventory_stock (
                        product_id,
                        sph, cyl, axis, add_power, eye_side,
                        batch_no, expiry_date,
                        quantity,
                        purchase_rate,
                        selling_price,
                        mrp,
                        stock_type,
                        is_active,
                        created_at,
                        updated_at
                    ) VALUES (
                        %s,
                        %s,%s,%s,%s,%s,
                        %s,%s,
                        %s,
                        %s,
                        %s,
                        %s,
                        'PURCHASE',
                        true,
                        %s,%s
                    )
                """, (
                    line["product_id"],
                    line.get("sph"),   line.get("cyl"),
                    line.get("axis"),  line.get("add_power"),
                    line.get("eye_side", "OTHER"),
                    line.get("batch_no") or f"GRN-{inv_no}-{item_no}",
                    line.get("expiry_date"),
                    rqty,
                    aprice,
                    line.get("selling_price", aprice * 1.3),  # 30% markup default
                    line.get("mrp", aprice * 1.5),
                    now, now,
                ))

        # ── 4b. Stamp purchase_rate as cost_price on open order_lines ────────
        # When we receive stock at actual_price, backfill cost_price on
        # any PENDING/CONFIRMED order_lines for the same product
        # so billing_status_ui can compute accurate margin.
        for line in lines:
            _pid_cp = str(line.get("product_id") or "")
            _aprice_cp = float(line.get("actual_price") or 0)
            if _pid_cp and _aprice_cp > 0:
                try:
                    cur.execute("""
                        UPDATE order_lines
                        SET cost_price = %(cp)s,
                            updated_at = NOW()
                        WHERE product_id::text = %(pid)s
                          AND (cost_price IS NULL OR cost_price = 0)
                          AND COALESCE(is_deleted, FALSE) = FALSE
                    """, {"cp": _aprice_cp, "pid": _pid_cp})
                except Exception:
                    pass  # non-fatal — cost_price remains 0

        # ── 5. Update PO status ───────────────────────────────────────────
        new_status = "RECEIVED" if all_received else "PARTIAL"
        cur.execute("""
            UPDATE supplier_orders
            SET status     = %s,
                updated_at = %s
            WHERE supplier_order_id = %s
        """, (new_status, now, po["supplier_order_id"]))

        # ── 6. Status history ─────────────────────────────────────────────
        cur.execute("""
            INSERT INTO supplier_order_status_history (
                supplier_order_id, status, timestamp, notes, changed_by
            ) VALUES (%s,%s,%s,%s,%s)
        """, (
            po["supplier_order_id"],
            new_status, now,
            f"Invoice {inv_no} — {total_qty} units received",
            "purchase_invoice",
        ))

        conn.commit()
        return True, inv_no, f"{'Fully' if all_received else 'Partially'} received"

    except Exception as e:
        if conn: conn.rollback()
        return False, "", str(e)
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def generate_invoice_no() -> str:
    """Purchase invoice number from transactional registry."""
    try:
        from modules.db.order_number_registry import alloc_doc_number
        return alloc_doc_number("PURCHASE_INVOICE")
    except Exception:
        pass
    now = datetime.now()
    if not DB_CONNECTED:
        import random
        return f"PINV-{now.strftime('%Y%m%d')}-{random.randint(1000,9999)}"
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT nextval('seq_purchase_invoice')")
            n = cur.fetchone()[0]
        conn.close()
        return f"PINV-{now.strftime('%Y%m%d')}-{n:04d}"
    except Exception:
        import random
        return f"PINV-{now.strftime('%Y%m%d')}-{random.randint(1000,9999)}"


_TABLES_CREATED = False   # module-level guard — runs once per process, never again

def _ensure_invoice_tables_once():
    """
    Create purchase_invoices tables the first time this module is imported.
    Uses a module-level flag so it never runs more than once per server process
    — not on every Streamlit rerun.
    """
    global _TABLES_CREATED
    if _TABLES_CREATED or not DB_CONNECTED:
        return
    sql = """
        CREATE TABLE IF NOT EXISTS purchase_invoices (
            invoice_no          TEXT PRIMARY KEY,
            supplier_order_id   TEXT NOT NULL,
            supplier_id         TEXT,
            supplier_name       TEXT NOT NULL,
            supplier_invoice_no TEXT,
            invoice_date        DATE NOT NULL DEFAULT CURRENT_DATE,
            total_items         INTEGER DEFAULT 0,
            total_qty_received  INTEGER DEFAULT 0,
            subtotal            NUMERIC(12,2) DEFAULT 0,
            gst_amount          NUMERIC(10,2) DEFAULT 0,
            invoice_total       NUMERIC(12,2) DEFAULT 0,
            payment_terms       TEXT DEFAULT 'NET30',
            payment_status      TEXT DEFAULT 'UNPAID',
            notes               TEXT,
            created_by          TEXT DEFAULT 'system',
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS purchase_invoice_lines (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            invoice_no              TEXT NOT NULL,
            item_no                 INTEGER NOT NULL,
            supplier_order_id       TEXT,
            supplier_order_item_no  INTEGER,
            product_id              TEXT,
            product_name            TEXT NOT NULL,
            brand                   TEXT,
            eye_side                TEXT,
            sph                     NUMERIC(5,2),
            cyl                     NUMERIC(5,2),
            axis                    INTEGER,
            add_power               NUMERIC(5,2),
            batch_no                TEXT,
            expiry_date             DATE,
            ordered_qty             INTEGER DEFAULT 0,
            received_qty            INTEGER NOT NULL,
            actual_price            NUMERIC(10,2) DEFAULT 0,
            gst_percent             NUMERIC(5,2)  DEFAULT 18,
            line_total              NUMERIC(12,2) DEFAULT 0,
            created_at              TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE SEQUENCE IF NOT EXISTS seq_purchase_invoice START 1;

        CREATE INDEX IF NOT EXISTS idx_pinv_po_id
            ON purchase_invoices(supplier_order_id);
        CREATE INDEX IF NOT EXISTS idx_pinvl_inv_no
            ON purchase_invoice_lines(invoice_no);
        CREATE INDEX IF NOT EXISTS idx_pinvl_product
            ON purchase_invoice_lines(product_id);
    """
    try:
        conn = get_transaction_connection()
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        conn.close()
        _TABLES_CREATED = True
    except Exception:
        _TABLES_CREATED = True   # don't retry on every rerun even if it fails


# Run once at import time — zero cost on every subsequent rerun
_ensure_invoice_tables_once()


# =============================================================================
# MOCK DATA
# =============================================================================

def _mock_pos():
    return [
        {
            "supplier_order_id": "PO-20240215-001",
            "supplier_name": "OptiCo Suppliers",
            "supplier_id": "S1",
            "status": "SENT",
            "order_date": "2024-02-15",
            "expected_delivery_date": "2024-03-01",
            "total_items": 3,
            "total_qty": 95,
            "total_value": 18750,
            "payment_terms": "NET30",
            "items": [
                {"item_no": 1, "product_id": "P1",
                 "product_name": "Spherical CL -0.50 LE",
                 "brand": "AquaLens", "eye_side": "R",
                 "sph": -0.50, "cyl": None, "axis": None, "add_power": None,
                 "ordered_qty": 50, "received_qty": 0, "pending_qty": 50,
                 "unit_price": 110, "item_status": "PENDING"},
                {"item_no": 2, "product_id": "P2",
                 "product_name": "Toric Lens -2.75 CYL 180",
                 "brand": "ToricPro", "eye_side": "L",
                 "sph": -2.75, "cyl": -0.75, "axis": 180, "add_power": None,
                 "ordered_qty": 30, "received_qty": 0, "pending_qty": 30,
                 "unit_price": 190, "item_status": "PENDING"},
                {"item_no": 3, "product_id": "P4",
                 "product_name": "Blue Light Blocking Glasses",
                 "brand": "ShieldX", "eye_side": "OTHER",
                 "sph": None, "cyl": None, "axis": None, "add_power": None,
                 "ordered_qty": 15, "received_qty": 0, "pending_qty": 15,
                 "unit_price": 420, "item_status": "PENDING"},
            ]
        },
        {
            "supplier_order_id": "PO-20240210-002",
            "supplier_name": "LensWorld Pvt Ltd",
            "supplier_id": "S2",
            "status": "DRAFT",
            "order_date": "2024-02-10",
            "expected_delivery_date": "2024-02-25",
            "total_items": 2,
            "total_qty": 60,
            "total_value": 14700,
            "payment_terms": "NET45",
            "items": [
                {"item_no": 1, "product_id": "P3",
                 "product_name": "Multifocal +1.50 ADD",
                 "brand": "FocusPro", "eye_side": "OTHER",
                 "sph": None, "cyl": None, "axis": None, "add_power": 1.50,
                 "ordered_qty": 40, "received_qty": 0, "pending_qty": 40,
                 "unit_price": 245, "item_status": "PENDING"},
                {"item_no": 2, "product_id": "P6",
                 "product_name": "Lens Cleaning Solution 120ml",
                 "brand": "CleanClear", "eye_side": "OTHER",
                 "sph": None, "cyl": None, "axis": None, "add_power": None,
                 "ordered_qty": 20, "received_qty": 0, "pending_qty": 20,
                 "unit_price": 75, "item_status": "PENDING"},
            ]
        }
    ]


def _mock_invoice_history():
    return pd.DataFrame([
        {"invoice_no": "PINV-20240201-0001",
         "supplier_order_id": "PO-20240128-001",
         "supplier_name": "OptiCo Suppliers",
         "invoice_date": "2024-02-01",
         "supplier_invoice_no": "OC/2024/1234",
         "total_items": 2, "total_qty_received": 60,
         "subtotal": 9200, "gst_amount": 1104, "invoice_total": 10304,
         "payment_status": "PAID", "created_at": "2024-02-01 11:30"},
        {"invoice_no": "PINV-20240205-0002",
         "supplier_order_id": "PO-20240201-002",
         "supplier_name": "LensWorld Pvt Ltd",
         "invoice_date": "2024-02-05",
         "supplier_invoice_no": "LW/INV/845",
         "total_items": 3, "total_qty_received": 80,
         "subtotal": 14600, "gst_amount": 1752, "invoice_total": 16352,
         "payment_status": "UNPAID", "created_at": "2024-02-05 15:00"},
    ])


# =============================================================================
# SESSION STATE
# =============================================================================

def _init():
    defaults = {
        "inv_step":         1,
        "inv_selected_po":  None,
        "inv_lines":        [],
        "inv_meta":         {},
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# =============================================================================
# HELPERS
# =============================================================================

def step_bar(current, labels):
    html = '<div class="step-bar">'
    for i, label in enumerate(labels, 1):
        if i < current:
            cls = "step-item step-done"
            prefix = "✓ "
        elif i == current:
            cls = "step-item step-active"
            prefix = ""
        else:
            cls = "step-item"
            prefix = ""
        html += f'<div class="{cls}">{prefix}{label}</div>'
    st.markdown(html + "</div>", unsafe_allow_html=True)


STATUS_CSS = {
    "DRAFT":    "s-draft",
    "SENT":     "s-sent",
    "CONFIRMED":"s-sent",
    "PARTIAL":  "s-partial",
    "RECEIVED": "s-received",
}


def status_badge(status):
    css = STATUS_CSS.get(status, "s-draft")
    return f'<span class="status-badge {css}">{status}</span>'


# =============================================================================
# MAIN RENDER FUNCTION  (called by app.py)
# =============================================================================

def render_purchase_invoice():
    """Entry point — called by app.py router."""
    if not st.session_state.get("_grn_css_injected"):
        st.markdown(_CSS, unsafe_allow_html=True)
        st.session_state["_grn_css_injected"] = True
    _init()

    st.title("📥 Purchase Invoice (GRN)")
    st.caption(
        "Receive goods against a Purchase Order — "
        "actual quantities and prices are recorded and stock is updated immediately."
    )

    tab_grn, tab_history = st.tabs(["📥 Receive Goods", "🧾 Invoice History"])

    # =========================================================================
    # TAB 1 — GRN FLOW
    # =========================================================================
    with tab_grn:

        step_bar(st.session_state.inv_step,
                 ["Select Purchase Order", "Enter Received Qty & Price", "Invoice Details & Save"])

        st.markdown("")

        # ─────────────────────────────────────────────────────────────────────
        # STEP 1 — SELECT PO
        # ─────────────────────────────────────────────────────────────────────
        if st.session_state.inv_step == 1:

            st.markdown("#### 📋 Select the Purchase Order goods arrived against")
            st.caption("Only DRAFT / SENT / PARTIAL orders are shown.")

            _grn_r1, _grn_r2 = st.columns([3, 2])
            with _grn_r1:
                _grn_search = st.text_input("Search", placeholder="🔍 Supplier / PO number",
                                             key="grn_po_search", label_visibility="collapsed")
            with _grn_r2:
                if st.button("🔄 Refresh", key="grn_refresh", use_container_width=True):
                    load_pending_pos.clear()
                    st.rerun()

            pos = load_pending_pos()
            if _grn_search:
                _gs = _grn_search.lower()
                pos = [p for p in pos if
                       _gs in str(p.get("supplier_order_id","")).lower() or
                       _gs in str(p.get("supplier_name","")).lower()]

            if not pos:
                st.info("No pending purchase orders found. Create one from the Procurement tab.")
            else:
                st.caption(f"{len(pos)} order(s) pending receipt")
                for po in pos:
                    _gpo_id  = str(po.get("supplier_order_id","—"))
                    _gpo_sup = str(po.get("supplier_name","—"))
                    _gpo_st  = str(po.get("status","DRAFT")).upper()
                    _gpo_val = float(po.get("total_value",0))
                    _gpo_its = int(po.get("total_items",0))
                    _gpo_qty = int(po.get("total_qty",0))
                    _gpo_ord = str(po.get("order_date",""))[:10]
                    _gpo_exp = str(po.get("expected_delivery_date",""))[:10]

                    _gst_col = {"DRAFT":"#64748b","SENT":"#3b82f6",
                                "CONFIRMED":"#8b5cf6","PARTIAL":"#f59e0b"}.get(_gpo_st,"#475569")

                    _gc1, _gc2 = st.columns([7, 3])
                    with _gc1:
                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid #1e293b;"
                            f"border-left:4px solid {_gst_col};border-radius:6px;"
                            f"padding:8px 14px;margin-bottom:3px'>"
                            f"<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>"
                            f"<span style='color:#f1f5f9;font-weight:800;font-size:0.88rem;"
                            f"font-family:monospace'>{_gpo_id}</span>"
                            f"<span style='color:#cbd5e1;font-size:0.82rem'>{_gpo_sup}</span>"
                            f"<span style='background:{_gst_col}22;color:{_gst_col};"
                            f"font-size:0.68rem;font-weight:700;padding:2px 9px;"
                            f"border-radius:8px'>{_gpo_st}</span>"
                            f"</div>"
                            f"<div style='margin-top:3px;color:#475569;font-size:0.7rem'>"
                            f"{_gpo_its} items &nbsp;·&nbsp; {_gpo_qty} pcs "
                            f"&nbsp;·&nbsp; &#8377;{_gpo_val:,.0f}"
                            + (f" &nbsp;|&nbsp; Ordered: {_gpo_ord}" if _gpo_ord else "")
                            + (f" &rarr; {_gpo_exp}" if _gpo_exp else "")
                            + "</div></div>",
                            unsafe_allow_html=True
                        )
                    with _gc2:
                        if st.button("📥 Receive Against This PO",
                                     key=f"recv_{_gpo_id}",
                                     use_container_width=True, type="primary"):
                            st.session_state.inv_selected_po = po
                            st.session_state.inv_lines = [
                                {
                                    "item_no":          item.get("item_no", i+1),
                                    "product_id":       item.get("product_id",""),
                                    "product_name":     item.get("product_name",""),
                                    "brand":            item.get("brand",""),
                                    "eye_side":         item.get("eye_side","OTHER"),
                                    "sph":              item.get("sph"),
                                    "cyl":              item.get("cyl"),
                                    "axis":             item.get("axis"),
                                    "add_power":        item.get("add_power"),
                                    "ordered_qty":      int(item.get("ordered_qty", 0)),
                                    "already_received": int(item.get("received_qty", 0)),
                                    "pending_qty":      int(item.get("pending_qty", 0)),
                                    "received_qty":     int(item.get("pending_qty", 0)),
                                    "po_price":         float(item.get("unit_price", 0)),
                                    "actual_price":     float(item.get("unit_price", 0)),
                                    "gst_percent":      18.0,
                                    "batch_no":         "",
                                    "expiry_date":      None,
                                    "selling_price":    float(item.get("unit_price", 0)) * 1.3,
                                    "mrp":              float(item.get("unit_price", 0)) * 1.5,
                                }
                                for i, item in enumerate(po.get("items", []))
                            ]
                            st.session_state.inv_step = 2
                            st.rerun()

                    st.markdown("<div style='height:1px;background:#1e293b;margin:2px 0'></div>",
                                unsafe_allow_html=True)

        # ─────────────────────────────────────────────────────────────────────
        # STEP 2 — ENTER QUANTITIES & PRICES
        # ─────────────────────────────────────────────────────────────────────
        elif st.session_state.inv_step == 2:

            po = st.session_state.inv_selected_po
            st.markdown(
                f'<div class="grn-card grn-card-blue">'
                f'<b>PO:</b> {po["supplier_order_id"]} &nbsp;·&nbsp; '
                f'<b>Supplier:</b> {po["supplier_name"]} &nbsp;·&nbsp; '
                f'<b>PO Date:</b> {str(po.get("order_date",""))[:10]}'
                f'</div>',
                unsafe_allow_html=True
            )

            st.markdown("#### 📦 Confirm Received Quantities & Actual Prices")
            st.caption(
                "Actual price may differ from PO price — enter what's on the supplier's invoice. "
                "Batch number and expiry are required for lenses."
            )

            lines = st.session_state.inv_lines
            subtotal = 0.0

            # Column headers
            hc = st.columns([3, 1, 1, 1, 1, 1, 1])
            for h, lbl in zip(hc, ["Product", "Ordered", "Pending", "Receive Qty",
                                    "Actual Price ₹", "Batch No", "Expiry"]):
                h.markdown(f"**{lbl}**")
            st.divider()

            for idx, ln in enumerate(lines):
                # Power string for lenses
                eye = f"({ln['eye_side']})" if ln.get("eye_side") and ln["eye_side"] != "OTHER" else ""
                pwrs = []
                if ln.get("sph")  is not None: pwrs.append(f"SPH {ln['sph']:+.2f}")
                if ln.get("cyl")  is not None: pwrs.append(f"CYL {ln['cyl']:+.2f}")
                if ln.get("axis") is not None: pwrs.append(f"AX {ln['axis']}°")
                pwr_str = "  ·  " + "  ".join(pwrs) if pwrs else ""

                c1, c2, c3, c4, c5, c6, c7 = st.columns([3, 1, 1, 1, 1, 1, 1])

                c1.markdown(
                    f"**{ln['product_name']}** {eye}  \n"
                    f"<span style='font-size:12px;color:#64748b'>"
                    f"{ln.get('brand','')}{pwr_str}</span>",
                    unsafe_allow_html=True
                )
                c2.markdown(f"**{ln['ordered_qty']}**")
                c3.markdown(f"**{ln['pending_qty']}**")

                # Editable: received qty
                rqty = c4.number_input(
                    "", min_value=0, max_value=max(ln["pending_qty"], 1),
                    value=ln["received_qty"], step=1,
                    key=f"rqty_{idx}", label_visibility="collapsed"
                )
                lines[idx]["received_qty"] = rqty

                # Editable: actual price
                aprice = c5.number_input(
                    "", min_value=0.0, value=float(ln["actual_price"]),
                    step=0.50, format="%.2f",
                    key=f"aprice_{idx}", label_visibility="collapsed"
                )
                lines[idx]["actual_price"] = aprice

                # Batch number
                batch = c6.text_input(
                    "", value=ln.get("batch_no",""),
                    placeholder="e.g. B2024001",
                    key=f"batch_{idx}", label_visibility="collapsed"
                )
                lines[idx]["batch_no"] = batch

                # Expiry date
                exp_val = c7.date_input(
                    "", value=ln.get("expiry_date") or date.today().replace(year=date.today().year+1),
                    key=f"exp_{idx}", label_visibility="collapsed"
                )
                lines[idx]["expiry_date"] = exp_val

                # Running subtotal
                subtotal += rqty * aprice

                st.divider()

            st.session_state.inv_lines = lines

            # Price variance warning
            for ln in lines:
                if ln["received_qty"] > 0 and abs(ln["actual_price"] - ln["po_price"]) > 0.5:
                    variance_pct = ((ln["actual_price"] - ln["po_price"]) / ln["po_price"] * 100) if ln["po_price"] else 0
                    direction = "higher" if ln["actual_price"] > ln["po_price"] else "lower"
                    st.markdown(
                        f'<div class="grn-card grn-card-amber">⚠️ <b>{ln["product_name"]}</b>: '
                        f'Actual price ₹{ln["actual_price"]:.2f} is {abs(variance_pct):.1f}% '
                        f'{direction} than PO price ₹{ln["po_price"]:.2f}</div>',
                        unsafe_allow_html=True
                    )

            gst_est = subtotal * 0.12   # rough estimate for display
            st.markdown(
                f'<div class="grn-card grn-card-green">'
                f'<b>Subtotal:</b> ₹{subtotal:,.2f} &nbsp;·&nbsp; '
                f'<b>GST ~12%:</b> ₹{gst_est:,.2f} &nbsp;·&nbsp; '
                f'<b>Est. Total:</b> ₹{subtotal+gst_est:,.2f}'
                f'</div>',
                unsafe_allow_html=True
            )

            cb, _, cn = st.columns([1, 2, 1])
            if cb.button("← Back"):
                st.session_state.inv_step = 1
                st.rerun()
            if cn.button("Continue →", type="primary",
                         disabled=not any(l["received_qty"] > 0 for l in lines)):
                st.session_state.inv_step = 3
                st.rerun()

        # ─────────────────────────────────────────────────────────────────────
        # STEP 3 — INVOICE DETAILS & SAVE
        # ─────────────────────────────────────────────────────────────────────
        elif st.session_state.inv_step == 3:

            po    = st.session_state.inv_selected_po
            lines = [l for l in st.session_state.inv_lines if l["received_qty"] > 0]

            # Compute final totals with per-line GST
            subtotal  = sum(l["received_qty"] * l["actual_price"] for l in lines)
            gst_total = sum(
                l["received_qty"] * l["actual_price"] * l.get("gst_percent", 18) / 100
                for l in lines
            )
            inv_total = subtotal + gst_total
            total_qty = sum(l["received_qty"] for l in lines)

            st.markdown(
                f'<div class="grn-card grn-card-blue">'
                f'<b>PO:</b> {po["supplier_order_id"]} &nbsp;·&nbsp; '
                f'<b>Supplier:</b> {po["supplier_name"]} &nbsp;·&nbsp; '
                f'<b>Items:</b> {len(lines)} &nbsp;·&nbsp; '
                f'<b>Total Qty:</b> {total_qty}'
                f'</div>',
                unsafe_allow_html=True
            )

            st.markdown("#### 🧾 Invoice Details")

            c_l, c_r = st.columns(2)
            with c_l:
                supplier_inv_no = st.text_input(
                    "Supplier's Invoice Number *",
                    placeholder="e.g. OC/2024/5678",
                    help="The invoice number printed on the physical invoice from the supplier"
                )
                invoice_date = st.date_input("Invoice Date", value=date.today())
                payment_terms = st.selectbox(
                    "Payment Terms",
                    ["NET30", "NET45", "NET60", "IMMEDIATE", "ADVANCE"],
                    index=["NET30","NET45","NET60","IMMEDIATE","ADVANCE"].index(
                        po.get("payment_terms","NET30")
                        if po.get("payment_terms","NET30") in ["NET30","NET45","NET60","IMMEDIATE","ADVANCE"]
                        else "NET30"
                    )
                )

            with c_r:
                # Per-line GST override
                st.markdown("**GST Rate per Item**")
                for idx, ln in enumerate(lines):
                    gst_p = st.number_input(
                        f"{ln['product_name'][:30]}",
                        min_value=0.0, max_value=28.0,
                        value=float(ln.get("gst_percent", 18)),
                        step=0.5, format="%.1f",
                        key=f"gst_rate_{idx}",
                        help="GST % for this item"
                    )
                    lines[idx]["gst_percent"] = gst_p

                notes = st.text_area("Notes", placeholder="Any receiving notes...", height=80)

            # Recompute with updated GST
            gst_total = sum(
                l["received_qty"] * l["actual_price"] * l.get("gst_percent", 18) / 100
                for l in lines
            )
            inv_total = subtotal + gst_total

            # ── Invoice summary ──────────────────────────────────────────
            st.markdown("#### 📊 Invoice Summary")
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Items",       len(lines))
            mc2.metric("Subtotal",    f"₹{subtotal:,.2f}")
            mc3.metric("GST",         f"₹{gst_total:,.2f}")
            mc4.metric("Total Payable", f"₹{inv_total:,.2f}")

            # ── Line preview ─────────────────────────────────────────────
            st.markdown("#### 📋 Items Being Received")
            preview = []
            for l in lines:
                eye = f"({l['eye_side']})" if l.get("eye_side") and l["eye_side"] != "OTHER" else ""
                gst_amt = l["received_qty"] * l["actual_price"] * l.get("gst_percent",18) / 100
                total_w_gst = l["received_qty"] * l["actual_price"] + gst_amt
                preview.append({
                    "Product":       l["product_name"] + " " + eye,
                    "Batch":         l.get("batch_no","—"),
                    "Qty Received":  l["received_qty"],
                    "Unit Price":    f"₹{l['actual_price']:.2f}",
                    "GST %":        f"{l.get('gst_percent',18):.0f}%",
                    "Line Total":   f"₹{total_w_gst:,.2f}",
                    "Stock Update": "✅ Will add to inventory",
                })
            st.dataframe(pd.DataFrame(preview), use_container_width=True, hide_index=True)

            st.markdown(
                f'<div class="grn-card grn-card-green">'
                f'✅ On save: <b>{total_qty} units</b> will be added to '
                f'<code>inventory_stock</code> at actual purchase price. '
                f'PO status → <b>RECEIVED</b>.'
                f'</div>',
                unsafe_allow_html=True
            )

            cb2, _, csave = st.columns([1, 2, 1])
            if cb2.button("← Back"):
                st.session_state.inv_step = 2
                st.rerun()

            if csave.button("✅ Save Invoice & Update Stock",
                            type="primary", use_container_width=True):

                if not supplier_inv_no.strip():
                    st.warning("⚠️ Please enter the supplier's invoice number.")
                else:
                    inv_no = generate_invoice_no()
                    meta   = {
                        "invoice_no":          inv_no,
                        "supplier_invoice_no": supplier_inv_no.strip(),
                        "invoice_date":        invoice_date,
                        "payment_terms":       payment_terms,
                        "notes":               notes,
                    }

                    with st.spinner("Saving invoice and updating inventory..."):
                        ok, final_inv_no, msg = save_purchase_invoice(po, lines, meta)

                    if ok:
                        st.success(f"✅ Invoice **{final_inv_no}** saved! ({msg})")
                        st.info(
                            f"📦 **{total_qty} units** added to `inventory_stock`  \n"
                            f"🧾 **{final_inv_no}** recorded against "
                            f"supplier invoice **{supplier_inv_no}**  \n"
                            f"📋 PO **{po['supplier_order_id']}** marked **{msg.split()[0].upper()}**"
                        )
                        st.balloons()
                        # Reset
                        load_pending_pos.clear()
                        load_invoice_history.clear()
                        for k in ["inv_step","inv_selected_po","inv_lines","inv_meta"]:
                            st.session_state.pop(k, None)
                        _init()
                        st.rerun()
                    else:
                        st.error(f"❌ Save failed: {msg}")

    # =========================================================================
    # TAB 2 — INVOICE HISTORY
    # =========================================================================
    with tab_history:
        st.markdown("### 🧾 Purchase Invoice History")

        if st.button("🔄 Refresh", key="inv_hist_refresh"):
            load_invoice_history.clear()
            st.rerun()

        hist = load_invoice_history()
        if hist.empty:
            st.info("No invoices recorded yet.")
        else:
            # Summary metrics
            hc1, hc2, hc3, hc4 = st.columns(4)
            hc1.metric("Total Invoices", len(hist))
            hc2.metric("Total Value",    f"₹{float(hist['invoice_total'].sum()):,.0f}")
            unpaid = hist[hist["payment_status"] == "UNPAID"]
            hc3.metric("Unpaid",         len(unpaid))
            hc4.metric("Unpaid Value",   f"₹{float(unpaid['invoice_total'].sum() if not unpaid.empty else 0):,.0f}")

            st.markdown("")

            # Filter
            pay_filter = st.selectbox("Payment Status", ["All", "UNPAID", "PAID"])
            filt = hist if pay_filter == "All" else hist[hist["payment_status"] == pay_filter]

            for _, row in filt.iterrows():
                pay_css = "grn-card-green" if row.get("payment_status") == "PAID" else "grn-card-amber"
                with st.expander(
                    f"🧾 {row['invoice_no']}  ·  {row['supplier_name']}  ·  "
                    f"₹{float(row.get('invoice_total',0)):,.0f}  ·  "
                    f"{'✅ PAID' if row.get('payment_status')=='PAID' else '⏳ UNPAID'}"
                ):
                    ic1, ic2, ic3, ic4 = st.columns(4)
                    ic1.metric("Qty Received",   row.get("total_qty_received",0))
                    ic2.metric("Subtotal",       f"₹{float(row.get('subtotal',0)):,.0f}")
                    ic3.metric("GST",            f"₹{float(row.get('gst_amount',0)):,.0f}")
                    ic4.metric("Total",          f"₹{float(row.get('invoice_total',0)):,.0f}")

                    st.markdown(
                        f"**PO Ref:** {row.get('supplier_order_id','—')} &nbsp;·&nbsp; "
                        f"**Supplier Invoice:** {row.get('supplier_invoice_no','—')} &nbsp;·&nbsp; "
                        f"**Date:** {str(row.get('invoice_date',''))[:10]}"
                    )

                    if row.get("payment_status") == "UNPAID":
                        if st.button(f"💵 Mark as Paid",
                                     key=f"pay_{row['invoice_no']}"):
                            if DB_CONNECTED:
                                try:
                                    conn = get_transaction_connection()
                                    with conn.cursor() as cur:
                                        cur.execute(
                                            "UPDATE purchase_invoices SET payment_status='PAID', updated_at=NOW() WHERE invoice_no=%s",
                                            (row["invoice_no"],)
                                        )
                                    conn.commit()
                                    conn.close()
                                except Exception as e:
                                    st.error(f"Update failed: {e}")
                            load_invoice_history.clear()
                            st.rerun()
