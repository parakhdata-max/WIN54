"""
PO Management - Generate POs for orders, manage active POs.
Organized by: Supplier Direct | External Lab | Stock PO | In-house Blanks | Active POs
"""
import streamlit as st
import datetime
import pandas as pd


def render_po_management():
    """Main PO Management page with 5 sections by route type."""

    if "po_mgmt_cart" not in st.session_state:
        st.session_state.po_mgmt_cart = []

    def _q(sql, params=None):
        try:
            from modules.sql_adapter import run_query
            return run_query(sql, params or {}) or []
        except Exception as e:
            st.error(f"DB: {e}")
            return []

    def _w(sql, params=None):
        try:
            from modules.sql_adapter import run_write
            return run_write(sql, params or {})
        except Exception as e:
            st.error(f"Write: {e}")
            return False

    st.markdown("### 📤 PO Management")
    _tab1, _tab2, _tab3, _tab4, _tab5, _tab6 = st.tabs([
        "🏭 Supplier Direct",
        "🧪 External Lab",
        "📦 PO Stock",
        "🔬 In-house Blanks",
        "📋 Active POs",
        "🧾 Combined Invoice",
    ])

    # TAB 1: SUPPLIER DIRECT (VENDOR)
    with _tab1:
        _render_supplier_direct_orders(_q, _w)

    # TAB 2: EXTERNAL LAB
    with _tab2:
        _render_external_lab_orders(_q, _w)

    # TAB 3: PO STOCK
    with _tab3:
        _render_stock_orders(_q, _w)

    # TAB 4: IN-HOUSE BLANKS
    with _tab4:
        _render_inhouse_blanks(_q, _w)

    # TAB 5: ACTIVE POs
    with _tab5:
        _render_active_pos(_q, _w)

    # TAB 6: COMBINED INVOICE
    with _tab6:
        _render_combined_invoice(_q, _w)

    # PO Creation Section
    if st.session_state.po_mgmt_cart:
        st.markdown("---")
        _tot_val = sum(float(i.get("unit_price", 0)) * int(i.get("quantity", 1)) for i in st.session_state.po_mgmt_cart)
        st.markdown(f"**🛒 Cart:** {len(st.session_state.po_mgmt_cart)} items · **Value:** ₹{_tot_val:,.0f}")

        _c1, _c2, _c3 = st.columns([3, 1, 1])
        with _c1:
            _sups = _q("""
                SELECT id::text AS id, party_name
                FROM parties
                WHERE UPPER(COALESCE(party_type, '')) IN ('SUPPLIER', 'VENDOR', 'LAB')
                AND COALESCE(is_active, TRUE) = TRUE
                ORDER BY party_name
            """)
            _sup_opts = {s["id"]: s["party_name"] for s in _sups}
            _po_sup = st.selectbox("Select Supplier *", list(_sup_opts.keys()),
                                   format_func=lambda x: _sup_opts.get(x, x),
                                   key="pom_po_supplier")
        with _c2:
            if st.button("🗑️ Clear", use_container_width=True):
                st.session_state.po_mgmt_cart = []
                st.rerun()
        with _c3:
            if st.button("📤 Generate PO", type="primary", use_container_width=True):
                _create_po_from_cart(_q, _w, _po_sup)


def _render_supplier_direct_orders(_q, _w):
    """Tab 1: Supplier Direct (VENDOR/EXTERNAL_LAB route) order lines needing PO."""
    st.markdown("#### 🏭 Supplier Direct Orders")
    st.caption("Select lines → Generate PO → Send to supplier")

    _df = st.date_input("From Date", value=datetime.date.today() - datetime.timedelta(days=60),
                        key="sup_df", label_visibility="collapsed", format="DD/MM/YYYY")

    _rows = _q("""
        SELECT o.id::text AS order_id, o.order_no,
               COALESCE(o.patient_name, o.party_name, '—') AS patient,
               o.created_at::date AS order_date,
               ol.id::text AS line_id, ol.eye_side, ol.quantity,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               p.id::text AS product_id,
               p.product_name,
               COALESCE(ol.unit_price, 0) AS unit_price,
               COALESCE(ol.lens_params->>'supplier_name', '') AS supplier,
               COALESCE(ol.lens_params->>'supplier_id', '') AS supplier_id_lp,
               COALESCE(ol.lens_params->>'manufacturing_route', 'STOCK') AS route
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN products p ON p.id = ol.product_id
        WHERE DATE(o.created_at) >= %(df)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND UPPER(COALESCE(ol.eye_side, '')) NOT IN ('S', 'SERVICE')
          AND ol.lens_params->>'manufacturing_route' IN ('VENDOR', 'EXTERNAL_LAB')
          AND EXISTS (SELECT 1 FROM challan_lines cl JOIN challans c ON c.id = cl.challan_id
                      WHERE cl.order_line_id = ol.id AND c.status NOT IN ('CANCELLED','VOID'))
          AND NOT EXISTS (SELECT 1 FROM supplier_order_items soi
                          JOIN supplier_orders so ON so.id = soi.supplier_order_id
                          WHERE soi.customer_line_id::uuid = ol.id AND so.status NOT IN ('CANCELLED','VOID'))
          AND NOT EXISTS (SELECT 1 FROM purchase_acknowledgements pa
                          WHERE pa.order_line_id = ol.id AND COALESCE(pa.purchase_price, 0) > 0)
        ORDER BY o.created_at DESC, o.order_no
        LIMIT 150
    """, {"df": str(_df)})

    if not _rows:
        st.info("✅ No Supplier Direct orders pending PO")
        return

    _render_order_lines(_rows, "SUPPLIER", _q, _w)


def _render_external_lab_orders(_q, _w):
    """Tab 2: External Lab specific orders needing PO."""
    st.markdown("#### 🧪 External Lab Orders")
    st.caption("External Lab service items (coating, colouring, tinting)")

    _df = st.date_input("From Date", value=datetime.date.today() - datetime.timedelta(days=60),
                        key="lab_df", label_visibility="collapsed", format="DD/MM/YYYY")

    _rows = _q("""
        SELECT o.id::text AS order_id, o.order_no,
               COALESCE(o.patient_name, o.party_name, '—') AS patient,
               o.created_at::date AS order_date,
               ol.id::text AS line_id, ol.eye_side, ol.quantity,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               p.id::text AS product_id,
               p.product_name,
               COALESCE(ol.unit_price, 0) AS unit_price,
               COALESCE(ol.lens_params->>'supplier_name', '') AS supplier,
               COALESCE(ol.lens_params->>'supplier_id', '') AS supplier_id_lp,
               COALESCE(ol.lens_params->>'manufacturing_route', 'STOCK') AS route
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN products p ON p.id = ol.product_id
        WHERE DATE(o.created_at) >= %(df)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND UPPER(COALESCE(ol.eye_side, '')) NOT IN ('S', 'SERVICE')
          AND ol.lens_params->>'manufacturing_route' = 'EXTERNAL_LAB'
          AND EXISTS (SELECT 1 FROM challan_lines cl JOIN challans c ON c.id = cl.challan_id
                      WHERE cl.order_line_id = ol.id AND c.status NOT IN ('CANCELLED','VOID'))
          AND NOT EXISTS (SELECT 1 FROM supplier_order_items soi
                          JOIN supplier_orders so ON so.id = soi.supplier_order_id
                          WHERE soi.customer_line_id::uuid = ol.id AND so.status NOT IN ('CANCELLED','VOID'))
          AND NOT EXISTS (SELECT 1 FROM purchase_acknowledgements pa
                          WHERE pa.order_line_id = ol.id AND COALESCE(pa.purchase_price, 0) > 0)
        ORDER BY o.created_at DESC, o.order_no
        LIMIT 150
    """, {"df": str(_df)})

    if not _rows:
        st.info("✅ No External Lab orders pending PO")
        return

    _render_order_lines(_rows, "LAB", _q, _w)


def _render_stock_orders(_q, _w):
    """Tab 3: Stock items needing PO (STOCK route + NULL)."""
    st.markdown("#### 📦 PO Stock")
    st.caption("Contact lenses, solutions needing PO")

    _df = st.date_input("From Date", value=datetime.date.today() - datetime.timedelta(days=60),
                        key="stock_df", label_visibility="collapsed", format="DD/MM/YYYY")

    _rows = _q("""
        SELECT o.id::text AS order_id, o.order_no,
               COALESCE(o.patient_name, o.party_name, '—') AS patient,
               o.created_at::date AS order_date,
               ol.id::text AS line_id, ol.eye_side, ol.quantity,
               ol.sph, ol.cyl, ol.axis, ol.add_power,
               p.id::text AS product_id,
               p.product_name,
               p.main_group,
               p.category,
               COALESCE(ol.unit_price, 0) AS unit_price,
               COALESCE(ol.lens_params->>'supplier_name', p.brand) AS supplier,
               COALESCE(ol.lens_params->>'supplier_id', '') AS supplier_id_lp,
               COALESCE(ol.lens_params->>'manufacturing_route', 'STOCK') AS route
        FROM order_lines ol
        JOIN orders o ON o.id = ol.order_id
        JOIN products p ON p.id = ol.product_id
        WHERE DATE(o.created_at) >= %(df)s
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
          AND UPPER(COALESCE(ol.eye_side, '')) NOT IN ('S', 'SERVICE')
          -- Only Contact Lenses and Solutions (exclude frames, sunglasses, services)
          AND p.main_group IN ('Contact Lenses', 'Solution')
          AND EXISTS (SELECT 1 FROM challan_lines cl JOIN challans c ON c.id = cl.challan_id
                      WHERE cl.order_line_id = ol.id AND c.status NOT IN ('CANCELLED','VOID'))
          AND NOT EXISTS (SELECT 1 FROM supplier_order_items soi
                          JOIN supplier_orders so ON so.id = soi.supplier_order_id
                          WHERE soi.customer_line_id::uuid = ol.id AND so.status NOT IN ('CANCELLED','VOID'))
          AND NOT EXISTS (SELECT 1 FROM purchase_acknowledgements pa
                          WHERE pa.order_line_id = ol.id AND COALESCE(pa.purchase_price, 0) > 0)
        ORDER BY o.created_at DESC, o.order_no
        LIMIT 150
    """, {"df": str(_df)})

    if not _rows:
        st.info("✅ No Stock orders pending PO")
        return

    _render_order_lines(_rows, "STOCK", _q, _w)


def _render_inhouse_blanks(_q, _w):
    """Tab 4: In-house blanks needing purchase (from blank_inventory)."""
    st.markdown("#### 🔬 In-house Blanks")
    st.caption("Blanks to order from suppliers (low stock in blank_inventory)")

    # Query blank_inventory for low stock items
    _rows = _q("""
        SELECT id::text AS blank_id, brand, category, material, colour,
               add_power, qty_right, qty_left, qty_independent, batch_no
        FROM blank_inventory
        WHERE (COALESCE(qty_right, 0) + COALESCE(qty_left, 0) + COALESCE(qty_independent, 0)) < 10
          AND COALESCE(is_active, TRUE) = TRUE
        ORDER BY brand, category, add_power
        LIMIT 100
    """)

    if not _rows:
        st.info("✅ No blank inventory needs ordering")
        return

    # Group by supplier/brand
    from collections import defaultdict
    _by_brand = defaultdict(list)
    for _r in _rows:
        _by_brand[_r.get("brand", "Unknown")].append(_r)

    _cart_ids = {c.get("blank_id") for c in st.session_state.po_mgmt_cart if c.get("blank_id")}
    _sel_ids = set()

    st.markdown(f"**{len(_rows)} blank(s) need ordering from {len(_by_brand)} brand(s)**")

    for _brand, _blanks in _by_brand.items():
        with st.expander(f"🏭 {_brand} — {len(_blanks)} blank(s)", expanded=True):
            for _bn in _blanks:
                _bid = _bn.get("blank_id", "")
                _cat = _bn.get("category", "")
                _mat = _bn.get("material", "")
                _col = _bn.get("colour", "")
                _add = _bn.get("add_power")
                _qr = _bn.get("qty_right", 0)
                _ql = _bn.get("qty_left", 0)
                _qi = _bn.get("qty_independent", 0)
                _tot = _qr + _ql + _qi
                
                _lbl = f"{_cat} {_mat}"
                if _col:
                    _lbl += f" {_col}"
                if _add:
                    _lbl += f" ADD+{_add}"
                _lbl += f" · Stock: {_tot} pcs"

                _ckey = f"pom_blank_{str(_bid).replace('-', '')}"
                _is_sel = st.checkbox(_lbl, key=_ckey, disabled=_bid in _cart_ids)
                if _is_sel:
                    _sel_ids.add(_bid)

    if _sel_ids:
        st.markdown("---")
        if st.button(f"🛒 Add {len(_sel_ids)} to PO Cart", type="primary", use_container_width=True):
            for _r in _rows:
                if _r.get("blank_id") in _sel_ids and _r.get("blank_id") not in _cart_ids:
                    _r["route_type"] = "BLANK"
                    _r["line_id"] = _r.get("blank_id")
                    _r["product_name"] = f"{_r.get('category', '')} {_r.get('material', '')}"
                    _r["quantity"] = max(10, (_r.get("qty_right", 0) + _r.get("qty_left", 0) + _r.get("qty_independent", 0)))
                    _r["unit_price"] = 0  # Price to be set later
                    st.session_state.po_mgmt_cart.append(_r)
            st.rerun()



def _ensure_purchase_invoices_table(_w):
    """Create purchase_invoices if it doesn't exist yet."""
    _w("""
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
            payment_status      TEXT DEFAULT 'UNPAID',
            notes               TEXT,
            created_by          TEXT DEFAULT 'system',
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def _render_active_pos(_q, _w):
    """
    Procurement Pipeline Hub — Tab 5
    Full pipeline from any source:
      Pipeline advance  -> auto-created PO  (SENT)
      PO Management     -> user-created PO  (DRAFT -> SENT -> ...)
      Direct Purchase   -> purchase_acknowledgements only (no PO)
    Stages: DRAFT -> SENT -> CONFIRMED -> RECEIVED -> Invoice -> CLOSED
    """
    st.markdown("### 🏭 Procurement Pipeline")

    # ── Load ALL POs (every status except VOID/CANCELLED) ────────────────────
    _all_pos = _q("""
        SELECT id,
               COALESCE(supplier_order_id, 'PO-' || id::text) AS po_no,
               supplier_name, supplier_id,
               COALESCE(created_by, '') AS source,
               order_date::date             AS order_date,
               expected_delivery_date::date AS exp_date,
               status,
               COALESCE(total_items, 0) AS total_items,
               COALESCE(total_qty,   0) AS total_qty,
               COALESCE(total_value, 0) AS total_value,
               COALESCE(special_instructions, '') AS notes,
               created_at
        FROM supplier_orders
        WHERE status NOT IN ('CANCELLED','VOID')
        ORDER BY
            CASE status
                WHEN 'RECEIVED'  THEN 0
                WHEN 'PARTIAL'   THEN 1
                WHEN 'SENT'      THEN 2
                WHEN 'CONFIRMED' THEN 3
                WHEN 'DRAFT'     THEN 4
                WHEN 'CLOSED'    THEN 5
                ELSE 6
            END,
            created_at DESC
        LIMIT 200
    """)

    # ── Direct Purchases: PA records with NO supplier_orders link ─────────────
    _direct_pas = _q("""
        SELECT
            pa.id::text                              AS pa_id,
            pa.order_no,
            COALESCE(o.patient_name, o.party_name, '—') AS patient,
            pa.supplier_name,
            pa.product_name,
            pa.eye_side,
            pa.qty,
            COALESCE(pa.purchase_price, 0) AS purchase_price,
            COALESCE(pa.total_value,   0)  AS total_value,
            pa.challan_no,
            pa.invoice_no,
            pa.document_date::text         AS doc_date,
            COALESCE(pa.transport, '')     AS transport,
            COALESCE(pa.lr_no, '')         AS lr_no,
            ol.sph, ol.cyl, ol.axis, ol.add_power
        FROM purchase_acknowledgements pa
        LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
        LEFT JOIN orders o       ON o.id  = ol.order_id
        WHERE NOT EXISTS (
            SELECT 1 FROM supplier_order_items soi
            WHERE soi.customer_line_id::uuid = pa.order_line_id
              AND soi.customer_line_id IS NOT NULL
        )
          AND COALESCE(pa.purchase_price, 0) > 0
        ORDER BY pa.acknowledged_at DESC
        LIMIT 100
    """)

    # ── Stage buckets ─────────────────────────────────────────────────────────
    _STAGES = ["RECEIVED", "PARTIAL", "SENT", "CONFIRMED", "DRAFT", "CLOSED"]
    _SM = {
        "DRAFT":    {"e": "📝", "c": "#64748b", "lbl": "Draft"},
        "SENT":     {"e": "📤", "c": "#3b82f6", "lbl": "Sent"},
        "CONFIRMED":{"e": "✅", "c": "#8b5cf6", "lbl": "Confirmed"},
        "PARTIAL":  {"e": "🔶", "c": "#f59e0b", "lbl": "Partial"},
        "RECEIVED": {"e": "📦", "c": "#f59e0b", "lbl": "Received"},
        "CLOSED":   {"e": "✅", "c": "#22c55e", "lbl": "Closed"},
    }
    _by = {s: [] for s in _STAGES}
    for _po in _all_pos:
        _st = str(_po.get("status", "DRAFT")).upper()
        if _st in _by:
            _by[_st].append(_po)

    # ── Pipeline status bar ───────────────────────────────────────────────────
    _bar_parts = []
    for _s in _STAGES:
        _cnt = len(_by[_s])
        _m   = _SM[_s]
        _bg  = f"{_m['c']}22" if _cnt else "#0d1b2a"
        _br  = _m["c"]        if _cnt else "#1e293b"
        _tc  = _m["c"]        if _cnt else "#334155"
        _bar_parts.append(
            f"<div style='flex:1;background:{_bg};border:1px solid {_br};"
            f"border-radius:6px;padding:8px 4px;text-align:center'>"
            f"<div style='font-size:1.1rem'>{_m['e']}</div>"
            f"<div style='font-size:0.68rem;font-weight:700;color:{_tc}'>{_m['lbl']}</div>"
            f"<div style='font-size:1.1rem;font-weight:800;color:{_tc}'>{_cnt}</div>"
            f"</div>"
        )
    if _direct_pas:
        _bar_parts.append(
            f"<div style='flex:1;background:#06b6d422;border:1px solid #06b6d4;"
            f"border-radius:6px;padding:8px 4px;text-align:center'>"
            f"<div style='font-size:1.1rem'>💳</div>"
            f"<div style='font-size:0.68rem;font-weight:700;color:#06b6d4'>Direct</div>"
            f"<div style='font-size:1.1rem;font-weight:800;color:#06b6d4'>{len(_direct_pas)}</div>"
            f"</div>"
        )
    st.markdown(
        "<div style='display:flex;gap:4px;margin-bottom:12px'>"
        + "".join(_bar_parts) + "</div>",
        unsafe_allow_html=True
    )

    # Summary metrics
    _m1, _m2, _m3, _m4 = st.columns(4)
    _m1.metric("Total POs",        len(_all_pos))
    _m2.metric("⚠️ Need Invoice",   len(_by["RECEIVED"]) + len(_by["PARTIAL"]))
    _m3.metric("PO Value",          f"₹{sum(float(p.get('total_value',0)) for p in _all_pos):,.0f}")
    _m4.metric("Direct Purchases",  len(_direct_pas))
    st.markdown("---")

    # ── RECEIVED / PARTIAL — urgent, invoice pending ──────────────────────────
    for _urg in ("RECEIVED", "PARTIAL"):
        if _by[_urg]:
            st.markdown(
                f"<div style='background:#1a1200;border:1px solid #f59e0b44;"
                f"border-left:4px solid #f59e0b;border-radius:8px;"
                f"padding:8px 14px;margin:8px 0'>"
                f"<b style='color:#fbbf24'>⚠️ {_SM[_urg]['lbl']} — Purchase Invoice Pending</b>"
                f"<span style='color:#64748b;font-size:0.78rem;margin-left:8px'>"
                f"Record invoice to complete the procurement cycle</span></div>",
                unsafe_allow_html=True
            )
            for _po in _by[_urg]:
                _render_po_pipeline_card(_po, _q, _w, "INVOICE", _SM)
            st.markdown("---")

    # ── SENT / CONFIRMED — awaiting goods ────────────────────────────────────
    _in_flight = _by["SENT"] + _by["CONFIRMED"]
    if _in_flight:
        st.markdown(
            "<div style='color:#3b82f6;font-weight:700;font-size:0.9rem;"
            "margin:6px 0 2px'>📤 In Progress — Awaiting Goods</div>",
            unsafe_allow_html=True
        )
        for _po in _in_flight:
            _render_po_pipeline_card(_po, _q, _w, "TRACK", _SM)
        st.markdown("---")

    # ── DRAFT — not yet sent ──────────────────────────────────────────────────
    if _by["DRAFT"]:
        st.markdown(
            "<div style='color:#64748b;font-weight:700;font-size:0.9rem;"
            "margin:6px 0 2px'>📝 Draft — Send to Supplier</div>",
            unsafe_allow_html=True
        )
        for _po in _by["DRAFT"]:
            _render_po_pipeline_card(_po, _q, _w, "TRACK", _SM)
        st.markdown("---")

    # ── DIRECT PURCHASES — PA with no PO ─────────────────────────────────────
    if _direct_pas:
        st.markdown(
            "<div style='background:#012030;border:1px solid #06b6d444;"
            "border-left:4px solid #06b6d4;border-radius:8px;"
            "padding:8px 14px;margin:8px 0'>"
            "<b style='color:#06b6d4'>💳 Direct Purchase Recorded</b>"
            "<span style='color:#475569;font-size:0.78rem;margin-left:8px'>"
            "Purchased directly — price & invoice already on record. No PO raised.</span></div>",
            unsafe_allow_html=True
        )
        from collections import defaultdict as _ddict
        _dp_by_sup = _ddict(list)
        for _dp in _direct_pas:
            _dp_by_sup[_dp.get("supplier_name", "—")].append(_dp)

        for _sn, _drows in _dp_by_sup.items():
            _dpval = sum(float(r.get("total_value", 0)) for r in _drows)
            with st.expander(
                f"🏭 {_sn} — {len(_drows)} line(s) · ₹{_dpval:,.0f}",
                expanded=(len(_dp_by_sup) == 1)
            ):
                for _dp in _drows:
                    _inv   = _dp.get("invoice_no", "") or ""
                    _chl   = _dp.get("challan_no",  "") or ""
                    _dref  = (f"🧾 {_inv}" if _inv
                              else f"📋 {_chl}" if _chl
                              else "⚠️ No doc")
                    _dclr  = "#22c55e" if _inv else ("#3b82f6" if _chl else "#ef4444")
                    _pwr   = _fmt_pwr_po(_dp)
                    _eye   = str(_dp.get("eye_side", "")).upper()
                    st.markdown(
                        f"<div style='background:#080f1a;border:1px solid #1e293b;"
                        f"border-left:4px solid #06b6d4;border-radius:6px;"
                        f"padding:6px 12px;margin:3px 0'>"
                        f"<div style='display:flex;justify-content:space-between'>"
                        f"<span style='color:#e2e8f0;font-weight:700'>"
                        f"{_dp.get('order_no','—')} · {_eye} · "
                        f"{(_dp.get('product_name') or '')[:35]}"
                        + (f" <code style='font-size:0.68rem'>{_pwr}</code>" if _pwr else "")
                        + f"</span>"
                        f"<span style='color:{_dclr};font-size:0.72rem;font-weight:700'>"
                        f"✅ Purchase Recorded · {_dref}</span>"
                        f"</div>"
                        f"<div style='color:#475569;font-size:0.72rem;margin-top:2px'>"
                        f"Qty: {_dp.get('qty',0)}"
                        f" · ₹{float(_dp.get('purchase_price',0)):,.2f}/pc"
                        f" · Total ₹{float(_dp.get('total_value',0)):,.0f}"
                        + (f" · 📅 {str(_dp.get('doc_date',''))[:10]}" if _dp.get("doc_date") else "")
                        + (f" · 🚚 {_dp.get('transport','')}" if _dp.get("transport") else "")
                        + f"</div></div>",
                        unsafe_allow_html=True
                    )
        st.markdown("---")

    # ── CLOSED — read-only history ────────────────────────────────────────────
    if _by["CLOSED"]:
        _cval = sum(float(p.get("total_value", 0)) for p in _by["CLOSED"])
        with st.expander(
            f"✅ Closed — {len(_by['CLOSED'])} PO(s) · ₹{_cval:,.0f}",
            expanded=False
        ):
            for _po in _by["CLOSED"]:
                _render_po_pipeline_card(_po, _q, _w, "CLOSED", _SM)

    if not _all_pos and not _direct_pas:
        st.info("No procurement records yet.")


def _render_po_pipeline_card(_po, _q, _w, mode="TRACK", stage_meta=None):
    """
    Pipeline card for a single PO.
    mode TRACK   -> Send / Confirm / Received actions + WhatsApp
    mode INVOICE -> invoice recording form (auto-expanded)
    mode CLOSED  -> read-only, show linked invoice details
    """
    _sm       = stage_meta or {}
    _po_id    = _po.get("id")
    _po_no    = _po.get("po_no",  "—")
    _sup      = _po.get("supplier_name", "—")
    _st       = str(_po.get("status",   "DRAFT")).upper()
    _val      = float(_po.get("total_value", 0))
    _source   = str(_po.get("source","") or "")
    _ak       = f"ppc_{str(_po_id).replace('-','')[-12:]}"
    _clr      = _sm.get(_st, {}).get("c", "#475569")
    _emoji    = _sm.get(_st, {}).get("e", "📋")

    _src_label = {
        "pipeline":           "🔗 Pipeline",
        "po_management":      "📋 PO Mgmt",
        "orders_to_purchase": "🔄 Direct",
        "procurement_pipeline": "🧾 Invoice",
    }.get(_source, _source[:12] if _source else "")

    # Card header
    st.markdown(
        f"<div style='background:#0d1b2a;border:1px solid {_clr}44;"
        f"border-left:4px solid {_clr};border-radius:8px;"
        f"padding:8px 14px;margin:3px 0'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span style='color:#f1f5f9;font-weight:800;font-family:monospace'>"
        f"{_emoji} {_po_no}"
        + (f" <span style='background:#1e293b;color:#64748b;font-size:0.65rem;"
           f"padding:1px 6px;border-radius:4px;font-weight:400'>{_src_label}</span>"
           if _src_label else "")
        + f"</span>"
        f"<span style='background:{_clr}22;color:{_clr};"
        f"font-size:0.68rem;font-weight:700;padding:2px 10px;border-radius:8px'>"
        f"{_st}</span></div>"
        f"<div style='color:#475569;font-size:0.74rem;margin-top:3px'>"
        f"🏭 {_sup} · {_po.get('total_items',0)} item(s) · ₹{_val:,.0f}"
        + (f" · 📅 {_po.get('order_date','')}"    if _po.get("order_date") else "")
        + (f" · 📦 Exp: {_po.get('exp_date','')}" if _po.get("exp_date")   else "")
        + f"</div></div>",
        unsafe_allow_html=True
    )

    _exp_lbl = {"INVOICE": "🧾 Record Purchase Invoice",
                "TRACK":   "📋 Detail / Actions",
                "CLOSED":  "✅ Invoice Details"}.get(mode, "📋 Detail")

    with st.expander(_exp_lbl, expanded=(mode == "INVOICE")):

        # Items
        _items = _q("""
            SELECT soi.item_no, soi.product_name, soi.eye_side,
                   soi.sph, soi.cyl, soi.axis, soi.add_power,
                   COALESCE(soi.ordered_qty,  0) AS ordered_qty,
                   COALESCE(soi.received_qty, 0) AS received_qty,
                   COALESCE(soi.unit_price,   0) AS unit_price,
                   soi.item_status,
                   COALESCE(soi.customer_line_id,'') AS line_id,
                   COALESCE(o.order_no,'')            AS order_no,
                   COALESCE(o.patient_name, o.party_name, '—') AS patient
            FROM supplier_order_items soi
            LEFT JOIN order_lines ol ON ol.id::text = soi.customer_line_id
            LEFT JOIN orders o ON o.id = ol.order_id
            WHERE CAST(soi.supplier_order_id AS TEXT) = %(id_str)s
            ORDER BY soi.item_no
        """, {"id_str": str(_po_id)})

        # ── Fallback 1: recover from order_lines via lens_params stamp ────────
        if not _items:
            _items = _q("""
                SELECT p.product_name, ol.eye_side,
                       ol.sph, ol.cyl, ol.axis, ol.add_power,
                       COALESCE(ol.quantity, 1)   AS ordered_qty,
                       0                          AS received_qty,
                       COALESCE(ol.unit_price, 0) AS unit_price,
                       'PENDING'                  AS item_status,
                       ol.id::text                AS line_id,
                       COALESCE(o.order_no, '')   AS order_no,
                       COALESCE(o.patient_name, o.party_name, '—') AS patient
                FROM order_lines ol
                JOIN products p ON p.id = ol.product_id
                JOIN orders o   ON o.id = ol.order_id
                WHERE ol.lens_params->>'supplier_order_no' = %(po_no)s
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                ORDER BY ol.eye_side
            """, {"po_no": _po_no})

            # If recovered from lens_params, re-insert into supplier_order_items so it works going forward
            if _items:
                for _idx_r, _it_r in enumerate(_items):
                    _null_u = "00000000-0000-0000-0000-000000000000"
                    _lid_r  = _it_r.get("line_id","") or ""
                    _w("""
                        INSERT INTO supplier_order_items (
                            supplier_order_id, item_no, product_name,
                            eye_side, sph, cyl, axis, add_power,
                            ordered_qty, unit_price, total_price,
                            customer_line_id, item_status
                        )
                        SELECT %(soid)s, %(itno)s, %(pname)s,
                               %(eye)s, ol.sph, ol.cyl, ol.axis, ol.add_power,
                               %(qty)s, %(up)s, %(tot)s,
                               %(clid)s, 'PENDING'
                        FROM order_lines ol
                        WHERE ol.id = %(clid)s::uuid
                        ON CONFLICT DO NOTHING
                    """, {
                        "soid":  _po_id,
                        "itno":  _idx_r + 1,
                        "pname": _it_r.get("product_name",""),
                        "eye":   _it_r.get("eye_side",""),
                        "qty":   int(_it_r.get("ordered_qty",1)),
                        "up":    float(_it_r.get("unit_price",0)),
                        "tot":   float(_it_r.get("unit_price",0)) * int(_it_r.get("ordered_qty",1)),
                        "clid":  _lid_r or _null_u,
                    })

        if _items:
            st.markdown("**📦 Items:**")
            for _it in _items:
                _eye  = str(_it.get("eye_side","")).upper()
                _pwr  = _fmt_pwr_po(_it)
                _pn   = (_it.get("product_name") or "")[:45]
                _oq   = int(_it.get("ordered_qty", 0))
                _rq   = int(_it.get("received_qty",0))
                _up   = float(_it.get("unit_price", 0))
                _ist  = _it.get("item_status","PENDING")
                _ie   = {"PENDING":"🔴","PARTIAL":"🟡","RECEIVED":"🟢"}.get(_ist,"⚪")
                _ono  = _it.get("order_no","")
                _pat  = _it.get("patient","")
                st.markdown(
                    f"<div style='padding:5px 10px;border-left:2px solid #1e293b;"
                    f"margin:2px 0;font-size:0.78rem'>"
                    + (f"<div style='color:#475569;font-size:0.7rem'>📋 {_ono}"
                       + (f" · {_pat}" if _pat and _pat != "—" else "")
                       + "</div>" if _ono else "")
                    + f"<div><b style='color:#e2e8f0'>{_eye}</b> · {_pn}"
                    + (f" <code style='color:#a78bfa;font-size:0.68rem'>{_pwr}</code>" if _pwr else "")
                    + f" · Ordered:<b style='color:#e2e8f0'>{_oq}</b>"
                    + (f" · Recv:<b style='color:#22c55e'>{_rq}</b>" if _rq else "")
                    + (f" · ₹{_up:,.0f}/pc" if _up else "")
                    + f" {_ie}</div></div>",
                    unsafe_allow_html=True
                )
        else:
            # ── Fallback 2: Re-link order lines (recovery for broken/old POs) ─
            _exp_total = int(_po.get("total_items", 0))
            if _exp_total > 0:
                st.markdown(
                    f"<div style='background:#1a0e00;border:1px solid #f59e0b44;"
                    f"border-left:4px solid #f59e0b;border-radius:6px;"
                    f"padding:8px 12px;margin:4px 0'>"
                    f"<b style='color:#fbbf24'>⚠️ {_exp_total} item(s) not linked</b>"
                    f"<div style='color:#64748b;font-size:0.75rem;margin-top:2px'>"
                    f"Items were not saved when this PO was created. "
                    f"Re-link the order lines below.</div></div>",
                    unsafe_allow_html=True
                )
            else:
                st.caption("No line items.")

            # Re-link: show billed order lines from this supplier not yet on another active PO
            with st.expander("🔗 Re-link Order Lines to this PO", expanded=(_exp_total > 0)):
                if _exp_total > 0:
                    st.markdown(
                        f"<div style='background:#0d2137;border:1px solid #3b82f644;"
                        f"border-radius:6px;padding:6px 12px;margin-bottom:8px;"
                        f"font-size:0.8rem;color:#94a3b8'>"
                        f"This PO expects <b style='color:#e2e8f0'>{_exp_total} line(s)</b>. "
                        f"Select exactly those that were originally placed on this PO. "
                        f"Already-linked or already-purchased lines are excluded.</div>",
                        unsafe_allow_html=True
                    )
                else:
                    st.caption("Select the order lines that belong to this PO")

                _sup_id_relink = str(_po.get("supplier_id") or "")
                _sup_nm_relink = _sup or ""
                # Narrow window: within 30 days of PO creation date
                _po_dt = str(_po.get("order_date") or "")

                _candidates = _q("""
                    SELECT ol.id::text AS line_id,
                           o.order_no, o.created_at::date AS order_date,
                           COALESCE(o.patient_name, o.party_name, '—') AS patient,
                           p.product_name, ol.eye_side,
                           ol.sph, ol.cyl, ol.axis, ol.add_power,
                           COALESCE(ol.quantity, 1)   AS quantity,
                           COALESCE(ol.unit_price, 0) AS unit_price,
                           EXISTS (
                               SELECT 1 FROM purchase_acknowledgements pa
                               WHERE pa.order_line_id = ol.id
                                 AND COALESCE(pa.purchase_price, 0) > 0
                           ) AS already_purchased
                    FROM order_lines ol
                    JOIN orders o   ON o.id = ol.order_id
                    JOIN products p ON p.id = ol.product_id
                    WHERE (
                            ol.lens_params->>'supplier_name' ILIKE %(sup_nm)s
                         OR ol.lens_params->>'supplier_id'   = %(sup_id)s
                         OR (
                              %(sup_id)s != '00000000-0000-0000-0000-000000000000'
                              AND ol.lens_params->>'supplier_id' = %(sup_id)s
                            )
                    )
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                      AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
                      AND NOT EXISTS (
                            SELECT 1 FROM supplier_order_items soi2
                            JOIN supplier_orders so2
                              ON CAST(soi2.supplier_order_id AS TEXT) = CAST(so2.id AS TEXT)
                            WHERE soi2.customer_line_id = ol.id::text
                              AND so2.status NOT IN ('CANCELLED','VOID')
                              AND so2.id != %(po_id)s
                      )
                    ORDER BY already_purchased, o.created_at DESC
                    LIMIT 60
                """, {
                    "sup_nm": f"%{_sup_nm_relink}%",
                    "sup_id": _sup_id_relink or "00000000-0000-0000-0000-000000000000",
                    "po_id":  _po_id,
                })

                # If still nothing, try broader match: billed lines for any EXTERNAL_LAB/VENDOR route
                if not _candidates and _sup_nm_relink:
                    _candidates = _q("""
                        SELECT ol.id::text AS line_id,
                               o.order_no, o.created_at::date AS order_date,
                               COALESCE(o.patient_name, o.party_name, '—') AS patient,
                               p.product_name, ol.eye_side,
                               ol.sph, ol.cyl, ol.axis, ol.add_power,
                               COALESCE(ol.quantity, 1)   AS quantity,
                               COALESCE(ol.unit_price, 0) AS unit_price,
                               EXISTS (
                                   SELECT 1 FROM purchase_acknowledgements pa
                                   WHERE pa.order_line_id = ol.id
                                     AND COALESCE(pa.purchase_price, 0) > 0
                               ) AS already_purchased
                        FROM order_lines ol
                        JOIN orders o   ON o.id = ol.order_id
                        JOIN products p ON p.id = ol.product_id
                        WHERE ol.lens_params->>'manufacturing_route' IN ('VENDOR','EXTERNAL_LAB')
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                          AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
                          AND DATE(o.created_at) >= (%(po_dt)s::date - interval '60 days')
                          AND NOT EXISTS (
                                SELECT 1 FROM supplier_order_items soi2
                                JOIN supplier_orders so2
                                  ON CAST(soi2.supplier_order_id AS TEXT) = CAST(so2.id AS TEXT)
                                WHERE soi2.customer_line_id = ol.id::text
                                  AND so2.status NOT IN ('CANCELLED','VOID')
                                  AND so2.id != %(po_id)s
                          )
                        ORDER BY already_purchased, o.created_at DESC
                        LIMIT 40
                    """, {
                        "po_id":  _po_id,
                        "po_dt":  _po_dt or str(datetime.date.today()),
                    })
                    if _candidates:
                        st.info("ℹ️ Supplier name not found in order lines — showing all vendor/lab lines from around this PO date.")

                if not _candidates:
                    st.info("No linkable order lines found for this supplier.")
                else:
                    _relink_sel = []
                    for _c in _candidates:
                        _cpwr  = _fmt_pwr_po(_c)
                        _ceye  = str(_c.get("eye_side","")).upper()
                        _is_pa = bool(_c.get("already_purchased"))
                        _lbl   = (f"**{_c.get('order_no','—')}** · {_c.get('patient','—')} · "
                                  f"{_ceye} · {(_c.get('product_name') or '')[:30]}")
                        if _cpwr: _lbl += f" · {_cpwr}"
                        _lbl += f" · Qty {_c.get('quantity',1)}"
                        if _is_pa: _lbl += " ✅ *(purchase already recorded)*"
                        if st.checkbox(_lbl, key=f"rl_{_ak}_{_c['line_id'][:8]}"):
                            _relink_sel.append(_c)

                    if _relink_sel:
                        if st.button("🔗 Link Selected Lines to this PO",
                                     key=f"rl_save_{_ak}", type="primary",
                                     use_container_width=True):
                            _null_u = "00000000-0000-0000-0000-000000000000"
                            for _ri, _rc in enumerate(_relink_sel):
                                _w("""
                                    INSERT INTO supplier_order_items (
                                        supplier_order_id, item_no,
                                        product_name, eye_side,
                                        sph, cyl, axis, add_power,
                                        ordered_qty, unit_price, total_price,
                                        customer_line_id, item_status
                                    )
                                    SELECT %(soid)s, %(itno)s,
                                           %(pname)s, %(eye)s,
                                           ol.sph, ol.cyl, ol.axis, ol.add_power,
                                           %(qty)s, %(up)s, %(tot)s,
                                           %(clid)s, 'PENDING'
                                    FROM order_lines ol
                                    WHERE ol.id = %(clid)s::uuid
                                    ON CONFLICT DO NOTHING
                                """, {
                                    "soid":  _po_id,
                                    "itno":  _ri + 1,
                                    "pname": _rc.get("product_name",""),
                                    "eye":   _rc.get("eye_side",""),
                                    "qty":   int(_rc.get("quantity",1)),
                                    "up":    float(_rc.get("unit_price",0)),
                                    "tot":   float(_rc.get("unit_price",0)) * int(_rc.get("quantity",1)),
                                    "clid":  _rc.get("line_id","") or _null_u,
                                })
                                # Stamp lens_params so future lookups work
                                try:
                                    from modules.sql_adapter import run_write as _rw_rl
                                    _rw_rl("""
                                        UPDATE order_lines
                                           SET lens_params = COALESCE(lens_params,'{}')
                                               || jsonb_build_object('supplier_order_no', %(po_no)s,
                                                                     'supplier_stage', 'ORDER_PLACED')
                                         WHERE id = %(lid)s::uuid
                                    """, {"po_no": _po_no, "lid": _rc.get("line_id","")})
                                except Exception: pass
                            # Update header count
                            _w("""
                                UPDATE supplier_orders
                                   SET total_items = %(n)s,
                                       total_qty   = %(q)s,
                                       total_value = %(v)s,
                                       updated_at  = NOW()
                                 WHERE id = %(id)s
                            """, {
                                "n":  len(_relink_sel),
                                "q":  sum(int(r.get("quantity",1)) for r in _relink_sel),
                                "v":  sum(float(r.get("unit_price",0)) * int(r.get("quantity",1))
                                          for r in _relink_sel),
                                "id": _po_id,
                            })
                            st.success(f"✅ {len(_relink_sel)} line(s) linked to {_po_no}")
                            st.rerun()

        if _po.get("notes"):
            st.caption(f"📝 {_po['notes']}")
        st.markdown("---")

        # ── INVOICE mode ──────────────────────────────────────────────────────
        if mode == "INVOICE" and _items:
            st.markdown("**🧾 Record Purchase Invoice**")

            # ── Supplier confirmation ─────────────────────────────────────────
            _po_sup_id   = str(_po.get("supplier_id") or "")
            _po_sup_name = str(_po.get("supplier_name") or "")

            # Load all suppliers for the selectbox
            _all_sups = _q("""
                SELECT id::text AS id, party_name AS name
                FROM parties
                WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR','LAB')
                  AND COALESCE(is_active, TRUE) = TRUE
                ORDER BY party_name
            """)
            _sup_opts   = {s["id"]: s["name"] for s in _all_sups}
            _sup_ids    = list(_sup_opts.keys())
            _def_idx    = _sup_ids.index(_po_sup_id) if _po_sup_id in _sup_ids else 0

            # Show supplier selector — pre-selected from PO, editable if wrong
            _sel_sup_col, _val_col = st.columns([3, 2])
            with _sel_sup_col:
                _confirmed_sup_id = st.selectbox(
                    "🏭 Purchasing From (Supplier)",
                    _sup_ids,
                    index=_def_idx,
                    format_func=lambda x: _sup_opts.get(x, x),
                    key=f"{_ak}_sup_sel",
                    help="Pre-filled from PO. Change if incorrect."
                )
            _confirmed_sup_name = _sup_opts.get(_confirmed_sup_id, _po_sup_name)
            with _val_col:
                st.markdown(
                    f"<div style='background:#0d2137;border:1px solid #3b82f644;"
                    f"border-radius:6px;padding:8px 12px;margin-top:26px'>"
                    f"<span style='color:#3b82f6;font-size:0.8rem'>📦 PO: </span>"
                    f"<b style='color:#e2e8f0'>{_po_no}</b>"
                    f"<br><span style='color:#475569;font-size:0.74rem'>"
                    f"₹{_val:,.0f} · {_po.get('total_items',0)} item(s)</span>"
                    f"</div>",
                    unsafe_allow_html=True
                )
            st.markdown("---")

            _ic1, _ic2, _ic3 = st.columns(3)
            with _ic1:
                _inv_no  = st.text_input("Supplier Invoice No.", key=f"{_ak}_inv",
                                          placeholder="INV/2024/001")
                _chal_no = st.text_input("Challan No.",          key=f"{_ak}_chal",
                                          placeholder="CH-001")
            with _ic2:
                _doc_dt  = st.date_input("Invoice Date", key=f"{_ak}_dt",
                                          value=datetime.date.today(),
                                          format="DD/MM/YYYY")
                _transport = st.text_input("Transport", key=f"{_ak}_trans",
                                            placeholder="DTDC / Delhivery")
            with _ic3:
                _lr_no   = st.text_input("LR / AWB", key=f"{_ak}_lr",
                                          placeholder="LR-12345")
                _gst_pct = st.number_input("GST %", min_value=0.0, max_value=28.0,
                                            value=18.0, step=0.5, key=f"{_ak}_gst")

            st.markdown("**Per-line Purchase Prices:**")
            _ncols     = min(len(_items), 3)
            _pcols     = st.columns(_ncols)
            _inv_data  = {}
            for _i, _it in enumerate(_items):
                with _pcols[_i % _ncols]:
                    _eye = str(_it.get("eye_side","")).upper()
                    _pn  = (_it.get("product_name") or "")[:22]
                    _inv_data[_i] = {
                        "price":   st.number_input(
                            f"{_eye} · {_pn}",
                            min_value=0.0, step=0.5, format="%.2f",
                            value=float(_it.get("unit_price",0)),
                            key=f"{_ak}_p_{_i}",
                            help=f"Qty: {_it.get('ordered_qty',0)}"
                        ),
                        "line_id": _it.get("line_id",""),
                        "qty":     int(_it.get("ordered_qty",1)),
                        "eye":     _eye,
                        "product": _it.get("product_name",""),
                    }

            _sub  = sum(d["price"] * d["qty"] for d in _inv_data.values())
            _gst  = round(_sub * _gst_pct / 100, 2)
            _tot  = round(_sub + _gst, 2)
            st.markdown(
                f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
                f"border-radius:6px;padding:8px 14px;margin:6px 0'>"
                f"Subtotal: <b style='color:#e2e8f0'>₹{_sub:,.2f}</b>"
                f" · GST {_gst_pct:.0f}%: <b style='color:#e2e8f0'>₹{_gst:,.2f}</b>"
                f" · <b style='color:#22c55e;font-size:1rem'>Total ₹{_tot:,.2f}</b>"
                f"</div>",
                unsafe_allow_html=True
            )

            if st.button("💾 Record Invoice & Close PO",
                         key=f"{_ak}_save_inv", type="primary",
                         use_container_width=True):
                from modules.core.date_guard import validate_not_future
                _ok_dt, _msg_dt = validate_not_future(_doc_dt, "Purchase invoice date")
                if not _ok_dt:
                    st.error(_msg_dt)
                    return
                _ok_all = True
                for _i, _d in _inv_data.items():
                    if not _d.get("line_id"):
                        continue
                    _ok_all &= bool(_w("""
                        INSERT INTO purchase_acknowledgements (
                            order_line_id, order_no, supplier_name, supplier_id,
                            product_name, eye_side, qty,
                            purchase_price, total_value,
                            challan_no, invoice_no, document_date,
                            transport, lr_no,
                            acknowledged_at, created_by
                        )
                        SELECT
                            %(lid)s::uuid,
                            COALESCE(o.order_no,''),
                            %(sup)s, so.supplier_id::text,
                            %(prod)s, %(eye)s, %(qty)s,
                            %(price)s, %(tot)s,
                            %(chal)s, %(inv)s, %(dt)s::date,
                            %(trans)s, %(lr)s,
                            NOW(), 'procurement_pipeline'
                        FROM supplier_orders so
                        LEFT JOIN order_lines ol ON ol.id = %(lid)s::uuid
                        LEFT JOIN orders o ON o.id = ol.order_id
                        WHERE so.id = %(po_id)s LIMIT 1
                        ON CONFLICT (order_line_id) DO UPDATE SET
                            purchase_price  = EXCLUDED.purchase_price,
                            total_value     = EXCLUDED.total_value,
                            challan_no      = EXCLUDED.challan_no,
                            invoice_no      = EXCLUDED.invoice_no,
                            document_date   = EXCLUDED.document_date,
                            transport       = EXCLUDED.transport,
                            lr_no           = EXCLUDED.lr_no,
                            acknowledged_at = NOW()
                    """, {
                        "lid":   _d["line_id"],
                        "sup":   _confirmed_sup_name,
                        "prod":  _d["product"],
                        "eye":   _d["eye"],
                        "qty":   _d["qty"],
                        "price": _d["price"],
                        "tot":   round(_d["price"] * _d["qty"], 2),
                        "chal":  (_chal_no.strip() or None),
                        "inv":   (_inv_no.strip()  or None),
                        "dt":    str(_doc_dt),
                        "trans": (_transport.strip() or None),
                        "lr":    (_lr_no.strip()   or None),
                        "po_id": _po_id,
                    }))

                _inv_num = (_inv_no.strip()
                            or f"PINV-{_po_no}-{datetime.date.today().strftime('%d%m%y')}")
                _ensure_purchase_invoices_table(_w)
                _w("""
                    INSERT INTO purchase_invoices (
                        invoice_no, supplier_name, supplier_id,
                        supplier_order_id, supplier_invoice_no,
                        invoice_date, total_items, total_qty_received,
                        subtotal, gst_amount, invoice_total,
                        payment_status, created_at
                    ) VALUES (
                        %(ino)s, %(sup)s, %(sid)s::uuid,
                        %(pono)s, %(sinv)s,
                        %(dt)s::date, %(items)s, %(qty)s,
                        %(sub)s, %(gst)s, %(tot)s,
                        'UNPAID', NOW()
                    )
                    ON CONFLICT (invoice_no) DO NOTHING
                """, {
                    "ino":   _inv_num,
                    "sup":   _confirmed_sup_name,
                    "sid":   _confirmed_sup_id or str(_po.get("supplier_id","00000000-0000-0000-0000-000000000000")),
                    "pono":  _po_no,
                    "sinv":  _inv_no.strip() or None,
                    "dt":    str(_doc_dt),
                    "items": len(_inv_data),
                    "qty":   sum(d["qty"] for d in _inv_data.values()),
                    "sub":   _sub,
                    "gst":   _gst,
                    "tot":   _tot,
                })
                _w("""
                    UPDATE supplier_order_items
                       SET item_status='RECEIVED', received_qty=ordered_qty, pending_qty=0
                     WHERE supplier_order_id=%(id)s AND item_status != 'RECEIVED'
                """, {"id": _po_id})
                _w("UPDATE supplier_orders SET status='CLOSED', updated_at=NOW() WHERE id=%(id)s",
                   {"id": _po_id})
                if _ok_all:
                    st.success(
                        f"✅ Invoice **{_inv_num}** recorded · "
                        f"PO **{_po_no}** closed · "
                        f"Visible in Purchase Register → GRNs"
                    )
                    st.rerun()
                else:
                    st.warning("Partial save — check individual lines")

            st.markdown("---")

            # ── Alternative actions ───────────────────────────────────────────
            _alt1, _alt2 = st.columns(2)

            # NEGLECT PURCHASE — close without recording invoice
            with _alt1:
                _neglect_key = f"{_ak}_neglect"
                if f"{_neglect_key}_confirm" not in st.session_state:
                    st.session_state[f"{_neglect_key}_confirm"] = False

                if not st.session_state[f"{_neglect_key}_confirm"]:
                    if st.button("🚫 Neglect Purchase",
                                 key=_neglect_key,
                                 use_container_width=True,
                                 help="Close this PO without recording any purchase invoice. "
                                      "Use for frames, cancelled jobs, or items not purchased."):
                        st.session_state[f"{_neglect_key}_confirm"] = True
                        st.rerun()
                else:
                    st.warning("Close PO with no purchase recorded?")
                    _nc1, _nc2 = st.columns(2)
                    if _nc1.button("✅ Yes, Neglect", key=f"{_neglect_key}_yes",
                                   type="primary", use_container_width=True):
                        _w("""UPDATE supplier_order_items
                               SET item_status='RECEIVED', received_qty=ordered_qty, pending_qty=0
                             WHERE supplier_order_id=%(id)s""", {"id": _po_id})
                        if _w("""UPDATE supplier_orders
                                    SET status='CLOSED',
                                        special_instructions = COALESCE(special_instructions,'')
                                                               || ' [PURCHASE NEGLECTED]',
                                        updated_at=NOW()
                                  WHERE id=%(id)s""", {"id": _po_id}):
                            st.session_state[f"{_neglect_key}_confirm"] = False
                            st.success("PO closed — no purchase recorded.")
                            st.rerun()
                    if _nc2.button("↩ Cancel", key=f"{_neglect_key}_no",
                                   use_container_width=True):
                        st.session_state[f"{_neglect_key}_confirm"] = False
                        st.rerun()

            # SECONDARY PURCHASE — extra charge from lab (coating, tinting, etc.)
            with _alt2:
                _sec_key = f"{_ak}_secondary"
                if f"{_sec_key}_open" not in st.session_state:
                    st.session_state[f"{_sec_key}_open"] = False

                if not st.session_state[f"{_sec_key}_open"]:
                    if st.button("➕ Secondary Purchase",
                                 key=_sec_key,
                                 use_container_width=True,
                                 help="Record an additional charge from the lab — "
                                      "coating, tinting, re-work, courier, etc."):
                        st.session_state[f"{_sec_key}_open"] = True
                        st.rerun()
                else:
                    st.markdown("**➕ Secondary Purchase / Lab Extra Charge**")
                    _sp1, _sp2 = st.columns(2)
                    with _sp1:
                        _sec_desc  = st.text_input("Description",
                                                    key=f"{_sec_key}_desc",
                                                    placeholder="Coating / Tinting / Courier")
                        _sec_price = st.number_input("Amount ₹",
                                                      min_value=0.0, step=10.0,
                                                      format="%.2f",
                                                      key=f"{_sec_key}_price")
                    with _sp2:
                        _sec_inv = st.text_input("Invoice / Ref No.",
                                                  key=f"{_sec_key}_inv",
                                                  placeholder="INV/LAB/001")
                        _sec_dt  = st.date_input("Date",
                                                  key=f"{_sec_key}_dt",
                                                  value=datetime.date.today(),
                                                  format="DD/MM/YYYY")
                    _sb1, _sb2 = st.columns(2)
                    if _sb1.button("💾 Save Secondary", key=f"{_sec_key}_save",
                                   type="primary", use_container_width=True):
                        if _sec_price > 0:
                            from modules.core.date_guard import validate_not_future
                            _ok_dt, _msg_dt = validate_not_future(_sec_dt, "Secondary purchase date")
                            if not _ok_dt:
                                st.error(_msg_dt)
                                return
                            # Record as a purchase_acknowledgement with no order_line_id
                            # (secondary / misc charge, linked to PO by order_no)
                            _w("""
                                INSERT INTO purchase_acknowledgements (
                                    order_no, supplier_name, supplier_id,
                                    product_name, qty,
                                    purchase_price, total_value,
                                    invoice_no, document_date,
                                    acknowledged_at, created_by
                                ) VALUES (
                                    %(ono)s, %(sup)s, %(sid)s::uuid,
                                    %(desc)s, 1,
                                    %(price)s, %(price)s,
                                    %(inv)s, %(dt)s::date,
                                    NOW(), 'secondary_purchase'
                                )
                            """, {
                                "ono":   _po_no,
                                "sup":   _confirmed_sup_name,
                                "sid":   _confirmed_sup_id or "00000000-0000-0000-0000-000000000000",
                                "desc":  (_sec_desc.strip() or "Secondary Charge"),
                                "price": _sec_price,
                                "inv":   (_sec_inv.strip() or None),
                                "dt":    str(_sec_dt),
                            })
                            st.session_state[f"{_sec_key}_open"] = False
                            st.success(f"Secondary purchase ₹{_sec_price:,.2f} recorded.")
                            st.rerun()
                        else:
                            st.warning("Enter an amount greater than 0")
                    if _sb2.button("↩ Cancel", key=f"{_sec_key}_cancel",
                                   use_container_width=True):
                        st.session_state[f"{_sec_key}_open"] = False
                        st.rerun()

        # ── TRACK mode ────────────────────────────────────────────────────────
        elif mode == "TRACK":
            _b1, _b2, _b3, _b4, _b5 = st.columns(5)

            if _st == "DRAFT":
                if _b1.button("📤 Send to Supplier", key=f"{_ak}_send",
                              type="primary", use_container_width=True):
                    if _w("UPDATE supplier_orders SET status='SENT',updated_at=NOW() WHERE id=%(id)s",
                          {"id": _po_id}):
                        try:
                            from modules.sql_adapter import run_write as _rw_sent
                            _lnk2 = _q("""
                                SELECT COALESCE(customer_line_id,'') AS lid
                                FROM supplier_order_items
                                WHERE CAST(supplier_order_id AS TEXT)=%(id_str)s
                                  AND customer_line_id IS NOT NULL AND customer_line_id != ''
                            """, {"id_str": str(_po_id)})
                            for _lk2 in _lnk2:
                                if _lk2.get("lid"):
                                    _rw_sent("""
                                        UPDATE order_lines
                                           SET lens_params = COALESCE(lens_params,'{}')
                                               || jsonb_build_object('supplier_stage','ORDER_PLACED',
                                                                     'supplier_order_no', %(pno)s)
                                         WHERE id = %(lid)s::uuid
                                    """, {"lid": _lk2["lid"], "pno": _po_no})
                        except Exception: pass
                        st.success("Marked Sent"); st.rerun()

            if _st in ("DRAFT","SENT"):
                if _b2.button("✅ Confirm", key=f"{_ak}_conf",
                              use_container_width=True):
                    if _w("UPDATE supplier_orders SET status='CONFIRMED',updated_at=NOW() WHERE id=%(id)s",
                          {"id": _po_id}):
                        st.success("Confirmed"); st.rerun()

            if _st in ("SENT","CONFIRMED","PARTIAL"):
                if _b3.button("📦 Mark Received", key=f"{_ak}_recv",
                              type="primary", use_container_width=True):
                    _w("""UPDATE supplier_order_items
                              SET item_status='RECEIVED', received_qty=ordered_qty, pending_qty=0
                            WHERE CAST(supplier_order_id AS TEXT)=%(id_str)s
                              AND item_status != 'RECEIVED'
                       """, {"id_str": str(_po_id)})
                    if _w("UPDATE supplier_orders SET status='RECEIVED',updated_at=NOW() WHERE id=%(id)s",
                          {"id": _po_id}):
                        try:
                            from modules.sql_adapter import run_write as _rw_recv
                            _lnk = _q("""
                                SELECT COALESCE(customer_line_id,'') AS lid
                                FROM supplier_order_items
                                WHERE CAST(supplier_order_id AS TEXT)=%(id_str)s
                                  AND customer_line_id IS NOT NULL AND customer_line_id != ''
                            """, {"id_str": str(_po_id)})
                            for _lk in _lnk:
                                if _lk.get("lid"):
                                    _rw_recv("""
                                        UPDATE order_lines
                                           SET lens_params = COALESCE(lens_params,'{}')
                                               || jsonb_build_object('supplier_stage','RECEIVED',
                                                                     'ready_qty', quantity)
                                         WHERE id = %(lid)s::uuid
                                    """, {"lid": _lk["lid"]})
                        except Exception: pass
                        st.success("📦 Goods received — record invoice now")
                        st.rerun()

            # ── Direct Invoice: receive + open invoice in one step ───────────
            if _st in ("DRAFT","SENT","CONFIRMED","PARTIAL") and _items:
                if _b4.button("🧾 Record Invoice", key=f"{_ak}_direct_inv",
                              use_container_width=True,
                              help="Goods already arrived — receive and record invoice in one step"):
                    # Mark items and PO as received
                    _w("""UPDATE supplier_order_items
                              SET item_status='RECEIVED', received_qty=ordered_qty, pending_qty=0
                            WHERE CAST(supplier_order_id AS TEXT)=%(id_str)s
                       """, {"id_str": str(_po_id)})
                    _w("UPDATE supplier_orders SET status='RECEIVED',updated_at=NOW() WHERE id=%(id)s",
                       {"id": _po_id})
                    try:
                        from modules.sql_adapter import run_write as _rw_di
                        _lnk_di = _q("""
                            SELECT COALESCE(customer_line_id,'') AS lid FROM supplier_order_items
                            WHERE CAST(supplier_order_id AS TEXT)=%(id_str)s
                              AND customer_line_id IS NOT NULL AND customer_line_id != ''
                        """, {"id_str": str(_po_id)})
                        for _lk_di in _lnk_di:
                            if _lk_di.get("lid"):
                                _rw_di("""
                                    UPDATE order_lines
                                       SET lens_params = COALESCE(lens_params,'{}')
                                           || jsonb_build_object('supplier_stage','RECEIVED',
                                                                 'ready_qty', quantity)
                                     WHERE id = %(lid)s::uuid
                                """, {"lid": _lk_di["lid"]})
                    except Exception: pass
                    st.success("📦 Marked received — fill invoice details above ↑")
                    st.rerun()

            if _b5.button("❌ Cancel", key=f"{_ak}_cancel",
                          use_container_width=True):
                if _w("UPDATE supplier_orders SET status='CANCELLED',updated_at=NOW() WHERE id=%(id)s",
                      {"id": _po_id}):
                    st.warning("Cancelled"); st.rerun()

            # WhatsApp — rich message with powers + order context
            if _items and _st in ("DRAFT","SENT","CONFIRMED"):
                _mr = _q("""
                    SELECT COALESCE(whatsapp,mobile,'') AS mob
                    FROM parties WHERE id=%(sid)s::uuid LIMIT 1
                """, {"sid": str(_po.get("supplier_id",""))})
                _raw  = (_mr[0].get("mob","") if _mr else "").replace(" ","")
                _digs = "".join(c for c in _raw if c.isdigit())
                if _digs.startswith("91") and len(_digs)==12: _digs = _digs[2:]
                _wa   = f"91{_digs}" if len(_digs)==10 else ""
                if _wa:
                    import urllib.parse as _up
                    _lines = []
                    _lines.append(f"*Purchase Order: {_po_no}*")
                    _lines.append(f"Supplier: {_sup}")
                    _lines.append(f"Date: {_po.get('order_date','')}")
                    _lines.append("")
                    _lines.append("Items:")
                    _last_ono = None
                    for _it in _items:
                        _eye = str(_it.get("eye_side","")).upper()
                        _pn  = (_it.get("product_name") or "")[:35]
                        _pwr = _fmt_pwr_po(_it)
                        _ono = _it.get("order_no","")
                        _pat = _it.get("patient","")
                        if _ono and _ono != _last_ono:
                            _lines.append("")
                            _hdr = f"*{_ono}*"
                            if _pat and _pat != "—": _hdr += f" - {_pat}"
                            _lines.append(_hdr)
                            _last_ono = _ono
                        _lines.append(f"  - {_eye} Eye: {_pn}")
                        if _pwr: _lines.append(f"    Rx: {_pwr}")
                        _qty_line = f"    Qty: {_it.get('ordered_qty',0)}"
                        if float(_it.get("unit_price",0)):
                            _qty_line += f" - Rs.{float(_it.get('unit_price',0)):,.0f}/pc"
                        _lines.append(_qty_line)
                    _lines.append("")
                    _lines.append(f"Total: Rs.{_val:,.0f}")
                    _lines.append("Kindly confirm and share reference number.")
                    _msg = "\n".join(_lines)
                    _wc1, _wc2 = st.columns([3,1])
                    _wc1.link_button("Send Order via WhatsApp",
                                     f"https://wa.me/{_wa}?text={_up.quote(_msg)}",
                                     use_container_width=True)
                    with _wc2:
                        if st.button("Copy Msg", key=f"{_ak}_wa_copy"):
                            st.toast("See message below")
                        st.caption(_msg[:120]+"...")

        # ── CLOSED mode ───────────────────────────────────────────────────────
        elif mode == "CLOSED":
            """
            A CLOSED PO means goods were received and purchase was recorded.
            Show: invoice ref, date, per-line prices, payment status.

            Data sources (in priority order):
              1. purchase_invoices table  (recorded via "Record Invoice & Close PO")
              2. purchase_acknowledgements via soi.customer_line_id  (older path)
              3. purchase_acknowledgements via order_no match  (LAB/backoffice path)
              4. Friendly "no invoice" message with reopen option
            """
            _found_invoice = False

            # ── Source 1: purchase_invoices table ─────────────────────────────
            _tbl_ok = _q("""
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'purchase_invoices' LIMIT 1
            """)
            if _tbl_ok:
                _pinv = _q("""
                    SELECT invoice_no, supplier_invoice_no,
                           invoice_date::text AS inv_date,
                           subtotal, gst_amount, invoice_total, payment_status
                    FROM purchase_invoices
                    WHERE supplier_order_id = %(pono)s
                    ORDER BY created_at DESC LIMIT 1
                """, {"pono": _po_no})
                if _pinv:
                    _found_invoice = True
                    _pi   = _pinv[0]
                    _pst  = str(_pi.get("payment_status","UNPAID")).upper()
                    _pclr = "#22c55e" if _pst == "PAID" else "#f59e0b"
                    st.markdown(
                        f"<div style='background:#071a0e;border:1px solid #22c55e33;"
                        f"border-left:4px solid #22c55e;border-radius:6px;"
                        f"padding:8px 14px;margin:4px 0'>"
                        f"<b style='color:#22c55e'>🧾 Invoice: {_pi.get('invoice_no','—')}</b>"
                        f"<div style='color:#475569;font-size:0.76rem;margin-top:3px'>"
                        f"Date: {str(_pi.get('inv_date',''))[:10]}"
                        f" · Subtotal ₹{float(_pi.get('subtotal',0)):,.2f}"
                        f" · GST ₹{float(_pi.get('gst_amount',0)):,.2f}"
                        f" · <b style='color:#e2e8f0'>Total ₹{float(_pi.get('invoice_total',0)):,.2f}</b>"
                        f" · <span style='color:{_pclr}'>{_pst}</span>"
                        f"</div></div>",
                        unsafe_allow_html=True
                    )
                    if _pst == "UNPAID":
                        if st.button("✅ Mark as Paid", key=f"{_ak}_paid",
                                     type="primary", use_container_width=True):
                            if _w("UPDATE purchase_invoices SET payment_status='PAID' WHERE invoice_no=%(n)s",
                                  {"n": _pi["invoice_no"]}):
                                st.success("Marked Paid"); st.rerun()

            # ── Source 2 & 3: purchase_acknowledgements ────────────────────────
            if not _found_invoice:
                # Try via supplier_order_items.customer_line_id (proper link)
                _pa_rows = _q("""
                    SELECT pa.invoice_no, pa.challan_no,
                           pa.document_date::text AS inv_date,
                           pa.purchase_price, pa.total_value, pa.qty,
                           pa.transport, pa.lr_no,
                           pa.product_name, pa.eye_side,
                           ol.sph, ol.cyl, ol.axis, ol.add_power
                    FROM purchase_acknowledgements pa
                    JOIN supplier_order_items soi
                      ON soi.customer_line_id::uuid = pa.order_line_id
                    LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
                    WHERE CAST(soi.supplier_order_id AS TEXT) = %(po_id)s
                    ORDER BY pa.acknowledged_at DESC
                """, {"po_id": _po_id})

                # Fallback: match by order_no from customer_order_id on the PO
                if not _pa_rows and _po.get("order_ref"):
                    _pa_rows = _q("""
                        SELECT pa.invoice_no, pa.challan_no,
                               pa.document_date::text AS inv_date,
                               pa.purchase_price, pa.total_value, pa.qty,
                               pa.transport, pa.lr_no,
                               pa.product_name, pa.eye_side,
                               ol.sph, ol.cyl, ol.axis, ol.add_power
                        FROM purchase_acknowledgements pa
                        LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
                        WHERE pa.order_no = %(ono)s
                        ORDER BY pa.acknowledged_at DESC
                    """, {"ono": _po.get("order_ref","")})

                if _pa_rows:
                    _found_invoice = True
                    _inv_ref  = next((r.get("invoice_no") or r.get("challan_no")
                                      for r in _pa_rows
                                      if r.get("invoice_no") or r.get("challan_no")), "—")
                    _inv_dt   = str(_pa_rows[0].get("inv_date","") or "")[:10]
                    _pa_total = sum(float(r.get("total_value",0)) for r in _pa_rows)
                    _transport= next((r.get("transport","") for r in _pa_rows if r.get("transport")), "")
                    st.markdown(
                        f"<div style='background:#071a0e;border:1px solid #22c55e33;"
                        f"border-left:4px solid #22c55e;border-radius:6px;"
                        f"padding:8px 14px;margin:4px 0'>"
                        f"<b style='color:#22c55e'>✅ Purchase Recorded · Ref: {_inv_ref}</b>"
                        f"<div style='color:#475569;font-size:0.76rem;margin-top:3px'>"
                        f"Date: {_inv_dt} · Total ₹{_pa_total:,.2f}"
                        + (f" · 🚚 {_transport}" if _transport else "")
                        + f"</div></div>",
                        unsafe_allow_html=True
                    )
                    for _par in _pa_rows:
                        _pwr = _fmt_pwr_po(_par)
                        _eye = str(_par.get("eye_side","")).upper()
                        _pn  = (_par.get("product_name") or "")[:40]
                        _up  = float(_par.get("purchase_price",0))
                        _qty = int(_par.get("qty",0))
                        st.markdown(
                            f"<div style='padding:3px 10px;border-left:2px solid #22c55e44;"
                            f"margin:2px 0;font-size:0.76rem;color:#64748b'>"
                            f"<b style='color:#e2e8f0'>{_eye}</b>"
                            f" · {_pn}"
                            + (f" <code style='font-size:0.68rem'>{_pwr}</code>" if _pwr else "")
                            + f" · Qty {_qty} · ₹{_up:,.2f}/pc"
                            f" · Total ₹{_up*_qty:,.2f}"
                            f"</div>",
                            unsafe_allow_html=True
                        )

            # ── No invoice found — was closed without proper recording ─────────
            if not _found_invoice:
                st.markdown(
                    "<div style='background:#1a0e00;border:1px solid #f59e0b44;"
                    "border-left:4px solid #f59e0b;border-radius:6px;"
                    "padding:8px 14px;margin:4px 0'>"
                    "<b style='color:#fbbf24'>⚠️ No purchase invoice on record</b>"
                    "<div style='color:#64748b;font-size:0.76rem;margin-top:3px'>"
                    "This PO was closed without a recorded purchase. "
                    "Reopen it to record the invoice, or record it directly in "
                    "Stock Pipeline → Record Purchase."
                    "</div></div>",
                    unsafe_allow_html=True
                )
                # Offer to reopen so invoice can be recorded
                if st.button("🔓 Reopen PO to Record Invoice",
                             key=f"{_ak}_reopen", use_container_width=True):
                    if _w("UPDATE supplier_orders SET status='RECEIVED',updated_at=NOW() WHERE id=%(id)s",
                          {"id": _po_id}):
                        st.success("PO reopened — record the invoice above")
                        st.rerun()



def _render_order_lines(rows, route_type, _q, _w):
    """Common renderer for order lines with add to cart."""
    from collections import defaultdict
    _by_sup = defaultdict(list)
    for _r in rows:
        _by_sup[_r.get("supplier", "Unknown")].append(_r)

    _cart_ids = {c.get("line_id") for c in st.session_state.po_mgmt_cart}
    _sel_ids = set()

    st.markdown(f"**{len(rows)} line(s) from {len(_by_sup)} supplier(s)**")

    for _sup, _lines in _by_sup.items():
        with st.expander(f"🏭 {_sup} — {len(_lines)} line(s)", expanded=True):
            for _ln in _lines:
                _lid   = _ln.get("line_id", "")
                _eye   = str(_ln.get("eye_side", "")).upper()
                _pn    = (_ln.get("product_name") or "")[:40]
                _qty   = int(_ln.get("quantity", 1))
                _price = float(_ln.get("unit_price", 0))
                _pwr   = _fmt_pwr_po(_ln)

                _ckey   = f"pom_{route_type}_{str(_lid).replace('-', '')}"
                _is_sel = st.checkbox(
                    f"{_eye} · {_pn} {_pwr} · Qty {_qty} · ₹{_price:,.0f}",
                    key=_ckey,
                    disabled=_lid in _cart_ids
                )
                if _is_sel:
                    _sel_ids.add(_lid)

    if _sel_ids:
        st.markdown("---")
        if st.button(f"🛒 Add {len(_sel_ids)} to PO Cart",
                     type="primary", use_container_width=True):
            for _r in rows:
                if _r.get("line_id") in _sel_ids and _r.get("line_id") not in _cart_ids:
                    _r["route_type"] = route_type
                    st.session_state.po_mgmt_cart.append(_r)
            st.rerun()


def _render_combined_invoice(_q, _w):
    """Tab 6 hub — Place Combined Order + Combined Invoice."""
    st.markdown("### 🔗 Combined PO — Order & Invoice")
    _cord_tab, _cinv_tab = st.tabs([
        "📤 Place Combined Order",
        "🧾 Combined Invoice",
    ])
    with _cord_tab:
        _render_combined_order_tab(_q, _w)
    with _cinv_tab:
        _render_combined_invoice_tab(_q, _w)


def _render_combined_order_tab(_q, _w):
    """Select DRAFT/SENT POs → combined WA order → mark SENT."""
    import urllib.parse as _upco
    import datetime as _dtco
    st.markdown("#### 📤 Place Combined Order to Supplier")
    st.caption("Select pending POs from the same supplier, review combined items, send one WhatsApp.")

    _sups_co = _q("""
        SELECT DISTINCT supplier_name FROM supplier_orders
        WHERE status IN ('DRAFT','SENT') AND supplier_name IS NOT NULL
        ORDER BY supplier_name
    """)
    if not _sups_co:
        st.info("No pending POs to combine.")
        return

    _sup_co = st.selectbox("🏭 Supplier", [s["supplier_name"] for s in _sups_co], key="cord_sup")
    _co_pos = _q("""
        SELECT id, COALESCE(supplier_order_id,'PO-'||id::text) AS po_no,
               status, COALESCE(total_value,0) AS total_value,
               order_date::text AS order_date, COALESCE(total_items,0) AS total_items
        FROM supplier_orders
        WHERE supplier_name=%(sup)s AND status IN ('DRAFT','SENT')
        ORDER BY created_at DESC LIMIT 30
    """, {"sup": _sup_co})
    if not _co_pos:
        st.info("No DRAFT/SENT POs for this supplier.")
        return

    _sel_co = []
    st.markdown("**Select POs to combine:**")
    for _p in _co_pos:
        _eb = "📝" if _p["status"]=="DRAFT" else "📤"
        if st.checkbox(
            f"{_eb} **{_p['po_no']}** [{_p['status']}] · {_p['total_items']} item(s) "
            f"· ₹{float(_p['total_value']):,.0f}"
            + (f" · {str(_p.get('order_date',''))[:10]}" if _p.get('order_date') else ""),
            key=f"cord_{_p['id']}", value=True
        ):
            _sel_co.append(int(_p["id"]))

    if not _sel_co:
        return

    _co_items = _q("""
        SELECT soi.product_name, soi.eye_side,
               soi.sph, soi.cyl, soi.axis, soi.add_power,
               COALESCE(soi.ordered_qty,1) AS qty,
               COALESCE(soi.unit_price,0)  AS unit_price,
               COALESCE(so.supplier_order_id,'PO-'||so.id::text) AS po_no,
               COALESCE(o.order_no,'')     AS order_no,
               COALESCE(o.patient_name, o.party_name,'—') AS patient,
               COALESCE(soi.customer_line_id,'') AS line_id
        FROM supplier_order_items soi
        JOIN supplier_orders so ON CAST(soi.supplier_order_id AS TEXT) = CAST(so.id AS TEXT)
        LEFT JOIN order_lines ol ON ol.id::text = soi.customer_line_id
        LEFT JOIN orders o ON o.id = ol.order_id
        WHERE so.id = ANY(%(ids)s::int[])
        ORDER BY so.id, soi.item_no
    """, {"ids": _sel_co})

    if not _co_items:
        st.markdown(
            "<div style='background:#1a0e00;border:1px solid #f59e0b44;"
            "border-left:4px solid #f59e0b;border-radius:6px;"
            "padding:10px 14px;margin:8px 0'>"
            "<b style='color:#fbbf24'>⚠️ No line items found in selected POs</b>"
            "<div style='color:#94a3b8;font-size:0.8rem;margin-top:4px'>"
            "Items were not saved when these POs were created (data issue).<br>"
            "→ Open each PO in <b>Active POs</b> tab and use "
            "<b>🔗 Re-link Order Lines</b> to restore the items first.<br>"
            "→ Or cancel these POs and create new ones from Tabs 1–4."
            "</div></div>",
            unsafe_allow_html=True
        )
        return

    st.markdown(f"**📦 {len(_co_items)} items across {len(_sel_co)} PO(s):**")
    _lpo = None
    for _it in _co_items:
        if _it["po_no"] != _lpo:
            st.markdown(
                f"<div style='color:#3b82f6;font-weight:700;font-size:0.78rem;"
                f"padding:2px 8px;background:#0d1b2a;border-radius:4px;"
                f"margin:6px 0 2px'>📤 {_it['po_no']}</div>",
                unsafe_allow_html=True)
            _lpo = _it["po_no"]
        _eye = str(_it.get("eye_side","")).upper()
        _pwr = _fmt_pwr_po(_it)
        _pn  = (_it.get("product_name") or "")[:40]
        _ono = _it.get("order_no","")
        _pat = _it.get("patient","")
        st.markdown(
            f"<div style='padding:4px 12px;border-left:2px solid #1e3a5f;margin:2px 0;font-size:0.78rem'>"
            + (f"<span style='color:#64748b;font-size:0.7rem'>📋 {_ono}"
               + (f" · {_pat}" if _pat and _pat != "—" else "") + "</span><br>" if _ono else "")
            + f"<b style='color:#e2e8f0'>{_eye}</b> · {_pn}"
            + (f" <code style='color:#a78bfa;font-size:0.68rem'>{_pwr}</code>" if _pwr else "")
            + f" · Qty <b>{_it.get('qty',1)}</b>"
            + (f" · ₹{float(_it.get('unit_price',0)):,.0f}/pc" if _it.get("unit_price") else "")
            + "</div>",
            unsafe_allow_html=True
        )

    st.markdown("---")

    # Build WhatsApp message
    _po_labels = [next((p["po_no"] for p in _co_pos if int(p["id"])==pid), str(pid)) for pid in _sel_co]
    _wa_lines = [
        f"*Purchase Order - {_sup_co}*",
        f"Date: {_dtco.date.today().strftime('%d/%m/%Y')}",
        f"PO Ref: {', '.join(_po_labels)}",
        "",
        "Items:",
    ]
    _lpo_wa = None
    for _it in _co_items:
        if _it["po_no"] != _lpo_wa:
            _wa_lines.append("")
            _hdr = f"*{_it['po_no']}*"
            if _it.get("order_no"): _hdr += f" | {_it['order_no']}"
            _pat = _it.get("patient","")
            if _pat and _pat != "—": _hdr += f" - {_pat}"
            _wa_lines.append(_hdr)
            _lpo_wa = _it["po_no"]
        _eye = str(_it.get("eye_side","")).upper()
        _pwr = _fmt_pwr_po(_it)
        _wa_lines.append(f"  - {_eye} Eye: {(_it.get('product_name') or '')[:30]}")
        if _pwr: _wa_lines.append(f"    Rx: {_pwr}")
        _qty_str = f"    Qty: {_it.get('qty',1)}"
        if float(_it.get("unit_price",0)): _qty_str += f" - Rs.{float(_it.get('unit_price',0)):,.0f}/pc"
        _wa_lines.append(_qty_str)
    _wa_lines.extend(["", f"Est. Value: Rs.{_tot_co:,.0f}",
                       "Kindly confirm and share your reference number."])
    _msg = "\n".join(_wa_lines)

    # Get supplier mobile
    _mob_co = _q("""
        SELECT COALESCE(p.whatsapp, p.mobile,'') AS mob
        FROM supplier_orders so
        JOIN parties p ON p.id::text = so.supplier_id::text
        WHERE so.id = %(fid)s LIMIT 1
    """, {"fid": _sel_co[0]})
    _raw_co = (_mob_co[0].get("mob","") if _mob_co else "").replace(" ","")
    _dco    = "".join(c for c in _raw_co if c.isdigit())
    if _dco.startswith("91") and len(_dco)==12: _dco = _dco[2:]
    _wa_co  = f"91{_dco}" if len(_dco)==10 else ""

    _wca, _wcb = st.columns([2,1])
    if _wa_co:
        _wca.link_button("Send Combined Order via WhatsApp",
                          f"https://wa.me/{_wa_co}?text={_upco.quote(_msg)}",
                          use_container_width=True)
    else:
        _wca.warning("No WhatsApp number in Parties for this supplier.")

    if _wcb.button("Mark All SENT", key="cord_sent",
                   type="primary", use_container_width=True):
        for _pid in _sel_co:
            _pno = next((p["po_no"] for p in _co_pos if int(p["id"])==_pid),"")
            _w("UPDATE supplier_orders SET status='SENT',updated_at=NOW() WHERE id=%(id)s",{"id":_pid})
            try:
                from modules.sql_adapter import run_write as _rw_cs
                _lnks = _q("""
                    SELECT COALESCE(customer_line_id,'') AS lid FROM supplier_order_items
                    WHERE CAST(supplier_order_id AS TEXT)=%(id_str)s
                      AND customer_line_id IS NOT NULL AND customer_line_id != ''
                """, {"id_str": str(_pid)})
                for _lk in _lnks:
                    if _lk.get("lid"):
                        _rw_cs("""
                            UPDATE order_lines
                               SET lens_params = COALESCE(lens_params,'{}')
                                   || jsonb_build_object('supplier_stage','ORDER_PLACED',
                                                         'supplier_order_no',%(pno)s)
                             WHERE id=%(lid)s::uuid
                        """, {"lid": _lk["lid"], "pno": _pno})
            except Exception: pass
        st.success(f"{len(_sel_co)} PO(s) marked SENT. Pipeline updated.")
        st.rerun()

def _render_combined_invoice_tab(_q, _w):
    """Select RECEIVED POs → edit prices → combined invoice → close POs."""
    st.markdown("#### 🧾 Combined Purchase Invoice")
    st.caption("Combine RECEIVED POs into one purchase invoice.")

    # ── Step 1: Pick supplier ─────────────────────────────────────────────────
    _sups = _q("""
        SELECT DISTINCT supplier_name
        FROM supplier_orders
        WHERE status = 'RECEIVED'
          AND supplier_name IS NOT NULL
        UNION
        SELECT DISTINCT supplier_name
        FROM purchase_acknowledgements
        WHERE COALESCE(purchase_price, 0) = 0
          AND supplier_name IS NOT NULL
        ORDER BY supplier_name
    """)

    if not _sups:
        st.info("No suppliers with RECEIVED POs or pending purchases.")
        return

    _sup_names = [s["supplier_name"] for s in _sups]
    _sel_sup   = st.selectbox("🏭 Select Supplier", _sup_names,
                              key="cinv_supplier")
    if not _sel_sup:
        return

    st.markdown("---")

    # ── Step 2: Load RECEIVED POs for this supplier ───────────────────────────
    _recv_pos = _q("""
        SELECT id,
               COALESCE(supplier_order_id, 'PO-' || id::text) AS po_no,
               COALESCE(total_value, 0) AS total_value,
               order_date::text AS order_date,
               COALESCE(total_items, 0) AS total_items
        FROM supplier_orders
        WHERE supplier_name = %(sup)s
          AND status = 'RECEIVED'
        ORDER BY created_at DESC
        LIMIT 50
    """, {"sup": _sel_sup})

    # ── Step 3: Load unrecorded PA lines for this supplier ───────────────────
    _pend_pa = _q("""
        SELECT pa.id::text AS pa_id, pa.order_no,
               pa.product_name, pa.eye_side, pa.qty,
               COALESCE(pa.purchase_price, 0) AS purchase_price,
               pa.order_line_id::text AS line_id,
               ol.sph, ol.cyl, ol.axis, ol.add_power
        FROM purchase_acknowledgements pa
        LEFT JOIN order_lines ol ON ol.id = pa.order_line_id
        WHERE pa.supplier_name = %(sup)s
          AND COALESCE(pa.purchase_price, 0) = 0
        ORDER BY pa.acknowledged_at DESC
        LIMIT 50
    """, {"sup": _sel_sup})

    if not _recv_pos and not _pend_pa:
        st.info(f"No RECEIVED POs or pending lines for **{_sel_sup}**.")
        return

    # ── Step 4: Select POs and PA lines to include ───────────────────────────
    _sel_po_ids = []
    _sel_pa_ids = []

    if _recv_pos:
        st.markdown("**📤 RECEIVED POs — select to include:**")
        for _po in _recv_pos:
            _chk = st.checkbox(
                f"**{_po['po_no']}** · {_po['total_items']} item(s) · "
                f"₹{float(_po['total_value']):,.0f} · {str(_po.get('order_date',''))[:10]}",
                key=f"ci_po_{_po['id']}",
                value=True
            )
            if _chk:
                _sel_po_ids.append(int(_po["id"]))

    if _pend_pa:
        st.markdown("**📋 Pending Purchase Lines (no price recorded):**")
        for _pa in _pend_pa:
            _pwr = _fmt_pwr_po(_pa)
            _lbl = (f"{_pa.get('order_no','—')} · "
                    f"{str(_pa.get('eye_side','')).upper()} · "
                    f"{(_pa.get('product_name') or '')[:30]}")
            if _pwr:
                _lbl += f" {_pwr}"
            _chk = st.checkbox(_lbl, key=f"ci_pa_{_pa['pa_id']}", value=True)
            if _chk:
                _sel_pa_ids.append(_pa["pa_id"])

    if not _sel_po_ids and not _sel_pa_ids:
        st.warning("Select at least one PO or line to include.")
        return

    # ── Step 5: Load all items from selected POs ──────────────────────────────
    st.markdown("---")
    st.markdown("**📦 Lines to Invoice:**")

    _all_lines = []   # unified list: {source, po_no, line_id, eye, product, qty, price, pwr}

    for _po_id_sel in _sel_po_ids:
        _po_info = next((p for p in _recv_pos if int(p["id"]) == _po_id_sel), {})
        _po_items = _q("""
            SELECT COALESCE(customer_line_id,'') AS line_id,
                   product_name, eye_side,
                   sph, cyl, axis, add_power,
                   COALESCE(ordered_qty, 1)  AS qty,
                   COALESCE(unit_price,  0)  AS unit_price
            FROM supplier_order_items
            WHERE CAST(supplier_order_id AS TEXT) = %(id_str)s
            ORDER BY item_no
        """, {"id_str": str(_po_id_sel)})

        for _it in _po_items:
            _all_lines.append({
                "source":   "PO",
                "po_id":    _po_id_sel,
                "po_no":    _po_info.get("po_no",""),
                "line_id":  _it.get("line_id",""),
                "eye":      str(_it.get("eye_side","")).upper(),
                "product":  (_it.get("product_name") or "")[:45],
                "qty":      int(_it.get("qty",1)),
                "price":    float(_it.get("unit_price",0)),
                "sph":      _it.get("sph"), "cyl": _it.get("cyl"),
                "axis":     _it.get("axis"), "add": _it.get("add_power"),
            })

    for _pa_id_sel in _sel_pa_ids:
        _pa = next((p for p in _pend_pa if p["pa_id"] == _pa_id_sel), None)
        if _pa:
            _all_lines.append({
                "source":  "PA",
                "po_id":   None,
                "po_no":   _pa.get("order_no",""),
                "line_id": _pa.get("line_id",""),
                "eye":     str(_pa.get("eye_side","")).upper(),
                "product": (_pa.get("product_name") or "")[:45],
                "qty":     int(_pa.get("qty",1)),
                "price":   float(_pa.get("purchase_price",0)),
                "sph":     _pa.get("sph"), "cyl": _pa.get("cyl"),
                "axis":    _pa.get("axis"), "add": _pa.get("add_power"),
            })

    if not _all_lines:
        st.info("No line items found in selected POs.")
        return

    # ── Step 6: Editable price grid ──────────────────────────────────────────
    _edited = []
    _ncols  = min(len(_all_lines), 3)
    _cols   = st.columns(_ncols)
    for _i, _ln in enumerate(_all_lines):
        with _cols[_i % _ncols]:
            _pwr  = _fmt_pwr_po(_ln)
            _lbl  = f"{_ln['eye']} · {_ln['product'][:20]}"
            if _pwr:
                _lbl += f"\n{_pwr}"
            _lbl  += f"\n[{_ln['po_no']}]"
            _new_price = st.number_input(
                _lbl,
                min_value=0.0, step=0.5, format="%.2f",
                value=float(_ln["price"]),
                key=f"ci_price_{_i}"
            )
            _new_qty = st.number_input(
                f"Qty",
                min_value=1, value=int(_ln["qty"]),
                key=f"ci_qty_{_i}"
            )
            _edited.append({**_ln, "price": _new_price, "qty": _new_qty})

    # ── Step 7: Invoice header ────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("**🧾 Invoice Details:**")
    _hi1, _hi2, _hi3, _hi4 = st.columns(4)
    with _hi1:
        _inv_no  = st.text_input("Supplier Invoice No.", key="ci_inv",
                                  placeholder="INV/2024/001")
        _chal_no = st.text_input("Challan No.",          key="ci_chal",
                                  placeholder="CH-001")
    with _hi2:
        _inv_dt   = st.date_input("Invoice Date", key="ci_dt",
                                   value=datetime.date.today(), format="DD/MM/YYYY")
        _transport= st.text_input("Transport",    key="ci_trans",
                                   placeholder="DTDC / Delhivery")
    with _hi3:
        _lr_no   = st.text_input("LR / AWB No.", key="ci_lr",
                                  placeholder="LR-12345")
        _gst_pct = st.number_input("GST %", min_value=0.0, max_value=28.0,
                                    value=18.0, step=0.5, key="ci_gst")
    with _hi4:
        _sub  = sum(l["price"] * l["qty"] for l in _edited)
        _gst  = round(_sub * _gst_pct / 100, 2)
        _tot  = round(_sub + _gst, 2)
        st.markdown(
            f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
            f"border-radius:8px;padding:10px 14px;margin-top:26px'>"
            f"<div style='color:#64748b;font-size:0.76rem'>Subtotal</div>"
            f"<div style='color:#e2e8f0;font-size:1rem;font-weight:700'>₹{_sub:,.2f}</div>"
            f"<div style='color:#64748b;font-size:0.72rem;margin-top:4px'>"
            f"GST {_gst_pct:.0f}%: ₹{_gst:,.2f}</div>"
            f"<div style='color:#22c55e;font-size:1.1rem;font-weight:800'>"
            f"Total ₹{_tot:,.2f}</div>"
            f"</div>",
            unsafe_allow_html=True
        )

    # ── Step 8: Save ─────────────────────────────────────────────────────────
    st.markdown("---")
    _sv1, _sv2 = st.columns([3, 1])
    with _sv1:
        if st.button("💾 Record Combined Invoice & Close Selected POs",
                     key="ci_save", type="primary", use_container_width=True):
            from modules.core.date_guard import validate_not_future
            _ok_dt, _msg_dt = validate_not_future(_inv_dt, "Purchase invoice date")
            if not _ok_dt:
                st.error(_msg_dt)
                return

            _inv_num = (_inv_no.strip()
                        or f"CINV-{_sel_sup[:8]}-{datetime.date.today().strftime('%d%m%y')}")

            # 1. Write purchase_acknowledgement for every line
            for _ln in _edited:
                if not _ln.get("line_id"):
                    continue
                _w("""
                    INSERT INTO purchase_acknowledgements (
                        order_line_id, order_no, supplier_name,
                        product_name, eye_side, qty,
                        purchase_price, total_value,
                        challan_no, invoice_no, document_date,
                        transport, lr_no,
                        acknowledged_at, created_by
                    ) VALUES (
                        %(lid)s::uuid, %(ono)s, %(sup)s,
                        %(prod)s, %(eye)s, %(qty)s,
                        %(price)s, %(tot)s,
                        %(chal)s, %(inv)s, %(dt)s::date,
                        %(trans)s, %(lr)s,
                        NOW(), 'combined_invoice'
                    )
                    ON CONFLICT (order_line_id) DO UPDATE SET
                        purchase_price  = EXCLUDED.purchase_price,
                        total_value     = EXCLUDED.total_value,
                        challan_no      = EXCLUDED.challan_no,
                        invoice_no      = EXCLUDED.invoice_no,
                        document_date   = EXCLUDED.document_date,
                        transport       = EXCLUDED.transport,
                        lr_no           = EXCLUDED.lr_no,
                        acknowledged_at = NOW()
                """, {
                    "lid":   _ln["line_id"],
                    "ono":   _ln["po_no"],
                    "sup":   _sel_sup,
                    "prod":  _ln["product"],
                    "eye":   _ln["eye"],
                    "qty":   _ln["qty"],
                    "price": _ln["price"],
                    "tot":   round(_ln["price"] * _ln["qty"], 2),
                    "chal":  (_chal_no.strip() or None),
                    "inv":   (_inv_no.strip()  or None),
                    "dt":    str(_inv_dt),
                    "trans": (_transport.strip() or None),
                    "lr":    (_lr_no.strip()    or None),
                })

            # 2. Write purchase_invoices header (one combined record)
            _ensure_purchase_invoices_table(_w)
            _w("""
                INSERT INTO purchase_invoices (
                    invoice_no, supplier_name,
                    supplier_order_id, supplier_invoice_no,
                    invoice_date, total_items, total_qty_received,
                    subtotal, gst_amount, invoice_total,
                    payment_status, created_at
                ) VALUES (
                    %(ino)s, %(sup)s,
                    %(pono)s, %(sinv)s,
                    %(dt)s::date, %(items)s, %(qty)s,
                    %(sub)s, %(gst)s, %(tot)s,
                    'UNPAID', NOW()
                )
                ON CONFLICT (invoice_no) DO NOTHING
            """, {
                "ino":   _inv_num,
                "sup":   _sel_sup,
                "pono":  ", ".join(
                    p["po_no"] for p in _recv_pos if int(p["id"]) in _sel_po_ids
                ),
                "sinv":  _inv_no.strip() or None,
                "dt":    str(_inv_dt),
                "items": len(_edited),
                "qty":   sum(l["qty"] for l in _edited),
                "sub":   _sub,
                "gst":   _gst,
                "tot":   _tot,
            })

            # 3. Close all selected POs
            for _po_id_close in _sel_po_ids:
                _w("""UPDATE supplier_order_items
                          SET item_status='RECEIVED', received_qty=ordered_qty, pending_qty=0
                        WHERE supplier_order_id=%(id)s AND item_status != 'RECEIVED'
                   """, {"id": _po_id_close})
                _w("""UPDATE supplier_orders
                          SET status='CLOSED', updated_at=NOW()
                        WHERE id=%(id)s
                   """, {"id": _po_id_close})

            st.success(
                f"✅ Combined invoice **{_inv_num}** recorded · "
                f"{len(_sel_po_ids)} PO(s) closed · "
                f"Total ₹{_tot:,.2f}"
            )
            st.rerun()

    with _sv2:
        if st.button("🚫 Neglect All Selected",
                     key="ci_neglect", use_container_width=True,
                     help="Close all selected POs without recording invoice"):
            for _po_id_n in _sel_po_ids:
                _w("""UPDATE supplier_orders
                          SET status='CLOSED',
                              special_instructions = COALESCE(special_instructions,'')
                                                     || ' [PURCHASE NEGLECTED]',
                              updated_at=NOW()
                        WHERE id=%(id)s""", {"id": _po_id_n})
            st.warning(f"Closed {len(_sel_po_ids)} PO(s) — no purchase recorded.")
            st.rerun()


def _render_order_lines(rows, route_type, _q, _w):
    """Common renderer for order lines with add to cart."""
    from collections import defaultdict
    _by_sup = defaultdict(list)
    for _r in rows:
        _by_sup[_r.get("supplier", "Unknown")].append(_r)

    _cart_ids = {c.get("line_id") for c in st.session_state.po_mgmt_cart}
    _sel_ids = set()

    st.markdown(f"**{len(rows)} line(s) from {len(_by_sup)} supplier(s)**")

    for _sup, _lines in _by_sup.items():
        with st.expander(f"🏭 {_sup} — {len(_lines)} line(s)", expanded=True):
            for _ln in _lines:
                _lid = _ln.get("line_id", "")
                _eye = str(_ln.get("eye_side", "")).upper()
                _pn = (_ln.get("product_name") or "")[:40]
                _qty = int(_ln.get("quantity", 1))
                _price = float(_ln.get("unit_price", 0))
                _pwr = _fmt_pwr_po(_ln)

                _ckey = f"pom_{route_type}_{str(_lid).replace('-', '')}"
                _is_sel = st.checkbox(
                    f"{_eye} · {_pn} {_pwr} · Qty {_qty} · ₹{_price:,.0f}",
                    key=_ckey,
                    disabled=_lid in _cart_ids
                )
                if _is_sel:
                    _sel_ids.add(_lid)

    if _sel_ids:
        st.markdown("---")
        if st.button(f"🛒 Add {len(_sel_ids)} to PO Cart", type="primary", use_container_width=True):
            for _r in rows:
                if _r.get("line_id") in _sel_ids and _r.get("line_id") not in _cart_ids:
                    _r["route_type"] = route_type
                    st.session_state.po_mgmt_cart.append(_r)
            st.rerun()


def _create_po_from_cart(_q, _w, supplier_id):
    """
    Create PO from cart items.
    - PO number via alloc_doc_number("PURCHASE_ORDER") → PO/2526/0001 format
    - UUID of new PO via INSERT ... RETURNING id  (no fragile MAX hack)
    - product_id written correctly to supplier_order_items
    - order_lines.lens_params stamped with po_number + stage=ORDER_PLACED
    """
    if not st.session_state.po_mgmt_cart:
        st.warning("Cart is empty")
        return

    # ── Supplier details ──────────────────────────────────────────────────────
    _sup_row = _q("SELECT party_name FROM parties WHERE id=%(id)s::uuid", {"id": supplier_id})
    _sup_name = _sup_row[0].get("party_name", "") if _sup_row else ""

    # ── Allocate proper PO number from registry ───────────────────────────────
    try:
        from modules.db.order_number_registry import alloc_doc_number
        _po_number = alloc_doc_number("PURCHASE_ORDER")   # → "PO/2526/0001"
    except Exception as _e:
        import datetime as _dt2
        _fy = _dt2.date.today().strftime("%y") + str(int(_dt2.date.today().strftime("%y")) + 1)
        _po_number = f"PO/{_fy}/{datetime.date.today().strftime('%H%M%S')}"
        st.warning(f"Registry unavailable — fallback PO number: {_po_number}")

    _titems = len(st.session_state.po_mgmt_cart)
    _tqty   = sum(int(i.get("quantity", 1)) for i in st.session_state.po_mgmt_cart)
    _tval   = sum(float(i.get("unit_price", 0)) * int(i.get("quantity", 1))
                  for i in st.session_state.po_mgmt_cart)

    # ── Insert PO header — use run_query so RETURNING works ──────────────────
    try:
        from modules.sql_adapter import run_query as _rq
        _new_po = _rq("""
            INSERT INTO supplier_orders (
                supplier_order_id,
                supplier_id, supplier_name,
                order_date, status,
                total_items, total_qty, total_value,
                created_by, created_at
            ) VALUES (
                %(pon)s,
                %(sid)s::uuid, %(sname)s,
                CURRENT_DATE, 'DRAFT',
                %(titems)s, %(tqty)s, %(tval)s,
                'po_management', NOW()
            )
            RETURNING id AS po_id
        """, {
            "pon":    _po_number,
            "sid":    supplier_id,
            "sname":  _sup_name,
            "titems": _titems,
            "tqty":   _tqty,
            "tval":   _tval,
        })
    except Exception as _ie:
        st.error(f"Failed to create PO header: {_ie}")
        return

    if not _new_po:
        st.error("DB insert returned no rows — PO not created")
        return

    _po_id = int(_new_po[0]["po_id"])  # integer PK of supplier_orders

    # ── Insert line items ─────────────────────────────────────────────────────
    _stamped_line_ids = []    # order_line UUIDs to update in lens_params
    _null_uuid = "00000000-0000-0000-0000-000000000000"

    for _idx, _it in enumerate(st.session_state.po_mgmt_cart):
        _pid  = _it.get("product_id") or _null_uuid
        _lid  = _it.get("line_id")    or ""
        _qty  = int(_it.get("quantity", 1))
        _up   = float(_it.get("unit_price", 0))

        _ok_item = _w("""
            INSERT INTO supplier_order_items (
                supplier_order_id,
                item_no, product_id, product_name,
                eye_side, sph, cyl, axis, add_power,
                ordered_qty, unit_price, total_price,
                customer_line_id, item_status
            ) VALUES (
                %(soid)s,
                %(itno)s, %(pid)s::uuid, %(pname)s,
                %(eye)s, %(sph)s, %(cyl)s, %(axis)s, %(add)s,
                %(qty)s, %(up)s, %(tot)s,
                %(clid)s,
                'PENDING'
            )
        """, {
            "soid":  _po_id,
            "itno":  _idx + 1,
            "pid":   _pid,
            "pname": _it.get("product_name", ""),
            "eye":   _it.get("eye_side", ""),
            "sph":   _it.get("sph"),
            "cyl":   _it.get("cyl"),
            "axis":  _it.get("axis"),
            "add":   _it.get("add_power"),
            "qty":   _qty,
            "up":    _up,
            "tot":   round(_up * _qty, 2),
            "clid":  _lid or None,
        })
        if _lid and _lid != _null_uuid and _ok_item:
            _stamped_line_ids.append(_lid)

    # ── Stamp order_lines.lens_params with PO number + stage=ORDER_PLACED ─────
    # This makes the supplier pipeline card show the PO badge immediately
    _stamped = 0
    for _lid in _stamped_line_ids:
        try:
            _ok_lp = _w("""
                UPDATE order_lines
                   SET lens_params = COALESCE(lens_params, '{}'::jsonb)
                                     || jsonb_build_object(
                                            'supplier_order_no', %(po_no)s,
                                            'supplier_stage',    'ORDER_PLACED'
                                        )
                 WHERE id = %(lid)s::uuid
            """, {"po_no": _po_number, "lid": _lid})
            if _ok_lp:
                _stamped += 1
        except Exception:
            pass

    # ── Done ──────────────────────────────────────────────────────────────────
    st.session_state.po_mgmt_cart = []
    st.success(
        f"✅ **PO {_po_number}** created for **{_sup_name}** — "
        f"{_titems} item(s), ₹{_tval:,.0f}. "
        f"Pipeline updated for {_stamped} line(s)."
    )
    st.rerun()


def _fmt_pwr_po(line: dict) -> str:
    """Format power string."""
    parts = []
    try:
        if line.get("sph") is not None and line.get("sph") != "":
            parts.append(f"SPH {float(line['sph']):+.2f}")
        if line.get("cyl") and abs(float(line.get("cyl", 0))) > 0.01:
            parts.append(f"CYL {float(line['cyl']):+.2f}")
        if line.get("axis"):
            parts.append(f"AX {int(line['axis'])}")
        if line.get("add_power") and float(line.get("add_power", 0)) > 0:
            parts.append(f"ADD +{float(line['add_power']):.2f}")
    except:
        pass
    return "  ".join(parts)
