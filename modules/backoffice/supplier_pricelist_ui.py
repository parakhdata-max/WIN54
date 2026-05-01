"""
supplier_pricelist_ui.py
========================
Supplier product picker — used when placing supplier/lab orders.

FLOW:
  Select Supplier → Products from inventory_stock filtered by supplier
  → Select Product → Coating options auto-load
  → Purchase price + Selling price auto-fill from inventory_stock
  → Confirm → writes to order_lines lens_params (supplier_id, purchase_price)

ALSO:
  Standalone management tab — view/edit supplier↔product price mappings
  Link supplier to inventory_stock rows (supplier_id column)

CALLED FROM:
  production_page.py  — order placement panel
  backoffice_ui.py    — supplier assignment panel (future hook)
"""

import streamlit as st
import json
from typing import List, Dict, Optional


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"Query error: {e}")
        return []


def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        st.error(f"Write error: {e}")
        return False


# ── Load helpers ─────────────────────────────────────────────────────────────

def _load_suppliers():
    """All active suppliers from parties."""
    return _q("""
        SELECT id::text AS id, party_name, COALESCE(mobile,'') AS mobile
        FROM parties
        WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR')
          AND COALESCE(is_active, TRUE) = TRUE
        ORDER BY party_name
    """)


def _load_products_for_supplier(supplier_id: str) -> List[Dict]:
    """
    Products linked to this supplier via inventory_stock.supplier_id.
    Falls back to ALL products if supplier has none linked yet.
    """
    rows = _q("""
        SELECT DISTINCT
            p.id::text          AS product_id,
            p.product_name,
            p.category,
            p.main_group,
            COALESCE(s.coating,'')           AS coating,
            COALESCE(s.purchase_price, 0)    AS purchase_price,
            COALESCE(s.selling_price,  0)    AS selling_price,
            COALESCE(s.quantity,  0)    AS quantity,
            s.id::text                       AS stock_id
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE s.supplier_id = %(sid)s::uuid
          AND COALESCE(s.is_active, TRUE) = TRUE
        ORDER BY p.product_name
    """, {"sid": supplier_id})

    if rows:
        return rows

    # Fallback: no products linked to supplier yet — show all products
    return _q("""
        SELECT DISTINCT
            p.id::text          AS product_id,
            p.product_name,
            p.category,
            p.main_group,
            ''                  AS coating,
            0                   AS purchase_price,
            0                   AS selling_price,
            0                   AS quantity,
            NULL                AS stock_id
        FROM products p
        WHERE COALESCE(p.is_active, TRUE) = TRUE
        ORDER BY p.product_name
    """)


def _load_coatings_for_product(supplier_id: str, product_id: str) -> List[Dict]:
    """All coating variants of a product from this supplier."""
    return _q("""
        SELECT
            s.id::text                    AS stock_id,
            COALESCE(s.coating, 'Standard') AS coating,
            COALESCE(s.purchase_price, 0) AS purchase_price,
            COALESCE(s.selling_price,  0) AS selling_price,
            COALESCE(s.quantity,  0) AS quantity,
            s.batch_no
        FROM inventory_stock s
        WHERE s.product_id  = %(pid)s::uuid
          AND s.supplier_id = %(sid)s::uuid
          AND COALESCE(s.is_active, TRUE) = TRUE
        ORDER BY s.coating
    """, {"pid": product_id, "sid": supplier_id})


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCT PICKER — used inline when placing supplier order
# ══════════════════════════════════════════════════════════════════════════════

def render_supplier_product_picker(
    supplier_id: str,
    supplier_name: str,
    order_line_id: str,       # order_lines.id to update
    current_product_id: str,  # pre-selected from order line
    session_key: str,         # unique key for this line's picker state
) -> Optional[Dict]:
    """
    Inline product picker for a supplier order line.
    Returns selected dict with product_id, coating, purchase_price, selling_price
    or None if nothing selected yet.
    """
    st.markdown(
        f"<div style='background:#0f172a;border:1px solid #1e293b;"
        f"border-radius:8px;padding:10px 14px;margin:6px 0'>"
        f"<span style='color:#f59e0b;font-weight:700;font-size:0.8rem'>"
        f"🏭 {supplier_name} — Product Selection</span></div>",
        unsafe_allow_html=True
    )

    products = _load_products_for_supplier(supplier_id)
    if not products:
        st.warning("No products found. Add inventory linked to this supplier first.")
        return None

    # Product search/filter
    _search = st.text_input(
        "Search product", placeholder="Type to filter...",
        key=f"{session_key}_search", label_visibility="collapsed"
    )
    if _search.strip():
        products = [p for p in products
                    if _search.strip().lower() in p["product_name"].lower()
                    or _search.strip().lower() in (p.get("category") or "").lower()]

    if not products:
        st.caption("No products match.")
        return None

    # Product dropdown
    _prod_ids   = [p["product_id"] for p in products]
    _prod_names = {p["product_id"]: p["product_name"] for p in products}
    _default    = _prod_ids.index(current_product_id) if current_product_id in _prod_ids else 0

    _sel_pid = st.selectbox(
        "Product",
        _prod_ids,
        index=_default,
        format_func=lambda x: _prod_names.get(x, x),
        key=f"{session_key}_product"
    )
    _sel_product = next((p for p in products if p["product_id"] == _sel_pid), None)
    if not _sel_product:
        return None

    # Coating variants
    coatings = _load_coatings_for_product(supplier_id, _sel_pid)

    if coatings:
        _coat_labels = [
            f"{c['coating']}" +
            (f" — ₹{float(c['purchase_price']):,.0f} buy / ₹{float(c['selling_price']):,.0f} sell"
             if float(c.get("purchase_price") or 0) > 0 else "")
            for c in coatings
        ]
        _coat_idx = st.selectbox(
            "Coating",
            range(len(coatings)),
            format_func=lambda i: _coat_labels[i],
            key=f"{session_key}_coating"
        )
        _sel_coat = coatings[_coat_idx]
    else:
        # No coating variants — manual entry
        _coat_input = st.text_input(
            "Coating (if any)", placeholder="e.g. AR, HC, BLAR",
            key=f"{session_key}_coat_manual"
        )
        _sel_coat = {
            "coating":        _coat_input.strip() or "Standard",
            "purchase_price": 0,
            "selling_price":  0,
            "quantity":  0,
            "stock_id":       None,
        }

    # Price fields — auto-filled, editable
    pc1, pc2 = st.columns(2)
    with pc1:
        _buy_price = st.number_input(
            "Purchase Price ₹",
            value=float(_sel_coat.get("purchase_price") or 0),
            min_value=0.0, step=1.0, format="%.2f",
            key=f"{session_key}_buy"
        )
    with pc2:
        _sell_price = st.number_input(
            "Selling Price ₹",
            value=float(_sel_coat.get("selling_price") or 0),
            min_value=0.0, step=1.0, format="%.2f",
            key=f"{session_key}_sell"
        )

    # Margin indicator
    if _buy_price > 0 and _sell_price > 0:
        _margin = ((_sell_price - _buy_price) / _sell_price) * 100
        _margin_color = "#22c55e" if _margin > 20 else "#f59e0b" if _margin > 10 else "#ef4444"
        st.markdown(
            f"<div style='font-size:0.72rem;color:{_margin_color}'>"
            f"Margin: {_margin:.1f}% · "
            f"Profit: ₹{_sell_price - _buy_price:,.2f}/pc</div>",
            unsafe_allow_html=True
        )

    return {
        "product_id":     _sel_pid,
        "product_name":   _prod_names.get(_sel_pid, ""),
        "coating":        _sel_coat.get("coating", ""),
        "purchase_price": _buy_price,
        "selling_price":  _sell_price,
        "stock_id":       _sel_coat.get("stock_id"),
        "quantity":  int(_sel_coat.get("quantity") or 0),
    }


# ══════════════════════════════════════════════════════════════════════════════
# MANAGEMENT PAGE — link suppliers to inventory_stock rows
# ══════════════════════════════════════════════════════════════════════════════

def render_supplier_pricelist_manager():
    """
    Standalone management page.
    View and edit which inventory_stock rows are linked to which supplier.
    Set purchase prices per supplier per product per coating.
    """
    st.markdown("### 🏷️ Supplier Pricelist")
    st.caption(
        "Link products to suppliers and set purchase prices. "
        "These prices auto-fill when placing supplier orders and recording purchases."
    )

    suppliers = _load_suppliers()
    if not suppliers:
        st.info("No suppliers found. Add suppliers in CRM → Parties.")
        return

    # Supplier selector
    _sup_ids   = [s["id"] for s in suppliers]
    _sup_names = {s["id"]: s["party_name"] for s in suppliers}
    _sel_sup   = st.selectbox(
        "Select Supplier",
        _sup_ids,
        format_func=lambda x: _sup_names.get(x, x),
        key="spl_supplier"
    )
    _sel_sup_name = _sup_names.get(_sel_sup, "")

    st.markdown("---")

    # Tabs: View linked products / Link new product
    _t1, _t2 = st.tabs(["📋 Linked Products", "➕ Link Product / Set Price"])

    with _t1:
        _linked = _q("""
            SELECT
                s.id::text            AS stock_id,
                p.product_name,
                p.category,
                COALESCE(s.coating,'Standard') AS coating,
                COALESCE(s.purchase_price, 0)  AS purchase_price,
                COALESCE(s.selling_price, 0)   AS selling_price,
                COALESCE(s.quantity, 0)   AS quantity,
                s.batch_no
            FROM inventory_stock s
            JOIN products p ON p.id = s.product_id
            WHERE s.supplier_id = %(sid)s::uuid
              AND COALESCE(s.is_active, TRUE) = TRUE
            ORDER BY p.product_name, s.coating
        """, {"sid": _sel_sup})

        if not _linked:
            st.info(f"No products linked to {_sel_sup_name} yet. Use 'Link Product' tab.")
        else:
            st.caption(f"{len(_linked)} product(s) linked to {_sel_sup_name}")
            for row in _linked:
                c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
                with c1:
                    st.markdown(
                        f"<div style='font-size:0.82rem;color:#e2e8f0'>"
                        f"<b>{row['product_name']}</b></div>"
                        f"<div style='font-size:0.7rem;color:#64748b'>"
                        f"{row.get('category','')} · {row['coating']}</div>",
                        unsafe_allow_html=True
                    )
                with c2:
                    _new_buy = st.number_input(
                        "Buy ₹", value=float(row["purchase_price"]),
                        min_value=0.0, step=1.0, format="%.2f",
                        key=f"buy_{row['stock_id']}",
                        label_visibility="collapsed"
                    )
                with c3:
                    _new_sell = st.number_input(
                        "Sell ₹", value=float(row["selling_price"]),
                        min_value=0.0, step=1.0, format="%.2f",
                        key=f"sell_{row['stock_id']}",
                        label_visibility="collapsed"
                    )
                with c4:
                    st.markdown(
                        f"<div style='font-size:0.72rem;color:#64748b;padding-top:8px'>"
                        f"Stock: {int(row['quantity'])}</div>",
                        unsafe_allow_html=True
                    )
                with c5:
                    if st.button("💾", key=f"save_{row['stock_id']}",
                                 help="Save prices"):
                        if _w("""
                            UPDATE inventory_stock
                            SET purchase_price = %(buy)s,
                                selling_price  = %(sell)s
                            WHERE id = %(sid)s::uuid
                        """, {"buy": _new_buy, "sell": _new_sell, "sid": row["stock_id"]}):
                            st.toast("✅ Saved")
                            st.rerun()

    with _t2:
        st.markdown(f"**Link a product to {_sel_sup_name}**")

        # Search all products
        _all_products = _q("""
            SELECT id::text AS id, product_name, category
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
            ORDER BY product_name
        """)
        if not _all_products:
            st.warning("No products in master.")
            return

        _ap_search = st.text_input(
            "Search product", placeholder="Type product name...",
            key="spl_search", label_visibility="collapsed"
        )
        _filtered = [p for p in _all_products
                     if not _ap_search.strip()
                     or _ap_search.strip().lower() in p["product_name"].lower()]

        if not _filtered:
            st.caption("No match.")
            return

        _ap_ids   = [p["id"] for p in _filtered]
        _ap_names = {p["id"]: p["product_name"] for p in _filtered}
        _sel_ap   = st.selectbox(
            "Product",
            _ap_ids,
            format_func=lambda x: _ap_names.get(x, x),
            key="spl_prod"
        )

        lc1, lc2, lc3, lc4 = st.columns([2, 2, 2, 2])
        with lc1:
            _coating = st.text_input(
                "Coating", placeholder="AR / HC / BLAR / UV / Standard",
                key="spl_coating"
            )
        with lc2:
            _lbuy = st.number_input(
                "Purchase Price ₹", min_value=0.0, step=1.0,
                format="%.2f", key="spl_buy"
            )
        with lc3:
            _lsell = st.number_input(
                "Selling Price ₹", min_value=0.0, step=1.0,
                format="%.2f", key="spl_sell"
            )
        with lc4:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if st.button("➕ Link", type="primary",
                         use_container_width=True, key="spl_link"):
                # Check if already linked (same product + supplier + coating)
                _exists = _q("""
                    SELECT id FROM inventory_stock
                    WHERE product_id  = %(pid)s::uuid
                      AND supplier_id = %(sid)s::uuid
                      AND UPPER(COALESCE(coating,'')) = UPPER(%(coat)s)
                    LIMIT 1
                """, {"pid": _sel_ap, "sid": _sel_sup, "coat": _coating.strip() or "Standard"})

                if _exists:
                    # Update existing
                    if _w("""
                        UPDATE inventory_stock
                        SET purchase_price = %(buy)s,
                            selling_price  = %(sell)s,
                            supplier_name  = %(sname)s
                        WHERE product_id  = %(pid)s::uuid
                          AND supplier_id = %(sid)s::uuid
                          AND UPPER(COALESCE(coating,'')) = UPPER(%(coat)s)
                    """, {
                        "buy":   _lbuy, "sell": _lsell,
                        "pid":   _sel_ap, "sid": _sel_sup,
                        "sname": _sel_sup_name,
                        "coat":  _coating.strip() or "Standard"
                    }):
                        st.success(f"✅ Updated {_ap_names.get(_sel_ap)} prices for {_sel_sup_name}")
                        st.rerun()
                else:
                    # Insert new stock row linking product to supplier
                    if _w("""
                        INSERT INTO inventory_stock (
                            product_id, supplier_id, supplier_name,
                            coating, purchase_price, selling_price,
                            quantity, is_active
                        ) VALUES (
                            %(pid)s::uuid, %(sid)s::uuid, %(sname)s,
                            %(coat)s, %(buy)s, %(sell)s,
                            0, TRUE
                        )
                    """, {
                        "pid":   _sel_ap, "sid":   _sel_sup,
                        "sname": _sel_sup_name,
                        "coat":  _coating.strip() or "Standard",
                        "buy":   _lbuy, "sell":  _lsell,
                    }):
                        st.success(
                            f"✅ Linked {_ap_names.get(_sel_ap)} "
                            f"({_coating or 'Standard'}) to {_sel_sup_name} "
                            f"— Buy ₹{_lbuy:,.2f} · Sell ₹{_lsell:,.2f}"
                        )
                        st.rerun()
