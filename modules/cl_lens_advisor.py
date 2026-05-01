"""
modules/cl_lens_advisor.py
===========================
Contact Lens Advisor — Full Page

A standalone page (role-gated) for:
  1. Rx entry → VD-corrected toric + SE calculation for R and L
  2. Brand/Product comparison table — all 4 brands side by side
  3. Product feature comparison (water, material, replacement, BC, Dia)
  4. Price columns:  MRP (everyone), Selling Price (billing/manager/admin),
                     Purchase Price (admin/manager only)
  5. Product selection → carries power + product to punching via session
  6. Excel import/export of product catalog (admin only)

Price visibility rules:
  VIEWER / BILLING  → MRP only
  MANAGER           → MRP + Selling Price
  ADMIN             → MRP + Selling Price + Purchase Price
"""

from __future__ import annotations
import math
import streamlit as st
from modules.documents.contact_lens_converter import (
    ALL_PRODUCTS, BRAND_CATALOG, BRANDS, get_product,
    _eye_calc, _snap_eye, _rx_line, CLProduct,
    _vertex_sph, _vertex_toric, _nearest,
)
from modules.security.roles import has_role, ADMIN, MANAGER, BILLING, INVENTORY, VIEWER

# ─────────────────────────────────────────────────────────────────────────────
# PRICE VISIBILITY
# ─────────────────────────────────────────────────────────────────────────────

def _can_see_selling():  return has_role(ADMIN, MANAGER, BILLING)
def _can_see_purchase(): return has_role(ADMIN, MANAGER)
def _can_edit_prices():  return has_role(ADMIN)


# ─────────────────────────────────────────────────────────────────────────────
# DB PRICE FETCH
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def _fetch_prices(product_names: tuple) -> dict:
    """
    Fetch mrp / selling_price / purchase_rate from inventory_stock
    for contact lens products matching names.
    Returns {product_name: {mrp, selling_price, purchase_rate}}.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT p.product_name,
                   COALESCE(MAX(i.mrp), 0)            AS mrp,
                   COALESCE(MAX(i.selling_price), 0)  AS selling_price,
                   COALESCE(MAX(i.purchase_rate), 0)  AS purchase_rate
            FROM products p
            LEFT JOIN inventory_stock i ON i.product_id = p.id
                AND COALESCE(i.is_active, true) = true
            WHERE p.product_name = ANY(%(names)s)
            GROUP BY p.product_name
        """, {"names": list(product_names)}) or []
        return {r["product_name"]: r for r in rows}
    except Exception:
        return {}


def _price_str(price_map: dict, prod_name: str, field: str) -> str:
    row = price_map.get(prod_name, {})
    v = float(row.get(field) or 0)
    return f"₹{v:,.2f}" if v > 0 else "—"


# ─────────────────────────────────────────────────────────────────────────────
# COMPARISON TABLE
# ─────────────────────────────────────────────────────────────────────────────

def _render_comparison(r_calc, l_calc, vd_mm, price_map):
    """
    Full comparison table: all products, all brands, R+L result, features, prices.
    """
    st.markdown(
        "<div style='font-size:1rem;font-weight:700;color:#e2e8f0;"
        "margin:8px 0 4px'>📊 Full Brand Comparison</div>",
        unsafe_allow_html=True
    )

    show_selling  = _can_see_selling()
    show_purchase = _can_see_purchase()

    # Build column headers
    _cols = ["Product", "Type", "Replace", "Material", "Water", "BC", "Dia",
             "R — CL Power", "L — CL Power", "MRP"]
    if show_selling:  _cols.append("Selling")
    if show_purchase: _cols.append("Purchase")
    _cols.append("Select")

    # Widths
    _widths = [3.5, 0.7, 0.8, 1.8, 0.6, 0.6, 0.6, 2.2, 2.2, 1.2]
    if show_selling:  _widths.append(1.2)
    if show_purchase: _widths.append(1.2)
    _widths.append(1.0)

    # Header row
    hcols = st.columns(_widths)
    for i, h in enumerate(_cols):
        hcols[i].markdown(
            f"<div style='font-size:0.7rem;color:#6b7280;font-weight:700'>{h}</div>",
            unsafe_allow_html=True
        )

    st.markdown("<hr style='margin:3px 0;border-color:#1e293b'>", unsafe_allow_html=True)

    selected = st.session_state.get("_cl_page_selected_product")

    for brand in BRANDS:
        # Brand header
        st.markdown(
            f"<div style='background:#1e293b;border-radius:4px;padding:3px 10px;"
            f"margin:6px 0 2px;font-size:0.75rem;font-weight:700;color:#6366f1'>"
            f"{brand}</div>",
            unsafe_allow_html=True
        )

        for prod in BRAND_CATALOG[brand]:
            r_snap = _snap_eye(r_calc, prod)
            l_snap = _snap_eye(l_calc, prod)

            # Highlight if in range
            _r_ok = r_snap["in_range"]
            _l_ok = l_snap["in_range"]
            _any_ok = _r_ok or _l_ok
            _row_bg = "#0f172a" if _any_ok else "transparent"
            _is_sel = (selected == prod.name)

            _type_icon = "🌀" if prod.lens_type == "toric" else "○"
            _replace_short = {"daily":"Daily","monthly":"Monthly",
                              "fortnightly":"2-Wk","quarterly":"Qtrly"}.get(prod.replace, prod.replace)

            row_cols = st.columns(_widths)

            # Product name
            _name_col = "#6366f1" if _is_sel else ("#e2e8f0" if _any_ok else "#334155")
            row_cols[0].markdown(
                f"<div style='font-size:0.78rem;color:{_name_col};font-weight:"
                f"{'700' if _is_sel else '400'}'>{prod.name}</div>",
                unsafe_allow_html=True
            )
            row_cols[1].markdown(
                f"<div style='font-size:0.78rem;color:#94a3b8'>{_type_icon}</div>",
                unsafe_allow_html=True
            )
            row_cols[2].markdown(
                f"<div style='font-size:0.72rem;color:#94a3b8'>{_replace_short}</div>",
                unsafe_allow_html=True
            )
            row_cols[3].markdown(
                f"<div style='font-size:0.68rem;color:#64748b'>{prod.material}</div>",
                unsafe_allow_html=True
            )
            row_cols[4].markdown(
                f"<div style='font-size:0.78rem;color:#64748b'>{prod.water}%</div>",
                unsafe_allow_html=True
            )
            row_cols[5].markdown(
                f"<div style='font-size:0.72rem;color:#64748b'>"
                f"{'/'.join(str(b) for b in prod.bc)}</div>",
                unsafe_allow_html=True
            )
            row_cols[6].markdown(
                f"<div style='font-size:0.72rem;color:#64748b'>"
                f"{'/'.join(str(d) for d in prod.dia)}</div>",
                unsafe_allow_html=True
            )

            # R result
            _r_c = "#10b981" if (r_snap.get("lens_type_used")=="toric") else "#f59e0b"
            row_cols[7].markdown(
                f"<div style='font-size:0.75rem;color:{_r_c if _r_ok else '#334155'}'>"
                f"{_rx_line(r_snap) if _r_ok else '—'}</div>",
                unsafe_allow_html=True
            )
            # L result
            _l_c = "#10b981" if (l_snap.get("lens_type_used")=="toric") else "#f59e0b"
            row_cols[8].markdown(
                f"<div style='font-size:0.75rem;color:{_l_c if _l_ok else '#334155'}'>"
                f"{_rx_line(l_snap) if _l_ok else '—'}</div>",
                unsafe_allow_html=True
            )

            # Prices
            _ci = 9
            row_cols[_ci].markdown(
                f"<div style='font-size:0.75rem;color:#10b981'>"
                f"{_price_str(price_map, prod.name, 'mrp')}</div>",
                unsafe_allow_html=True
            )
            _ci += 1
            if show_selling:
                row_cols[_ci].markdown(
                    f"<div style='font-size:0.75rem;color:#6366f1'>"
                    f"{_price_str(price_map, prod.name, 'selling_price')}</div>",
                    unsafe_allow_html=True
                )
                _ci += 1
            if show_purchase:
                row_cols[_ci].markdown(
                    f"<div style='font-size:0.75rem;color:#f59e0b'>"
                    f"{_price_str(price_map, prod.name, 'purchase_rate')}</div>",
                    unsafe_allow_html=True
                )
                _ci += 1

            # Select button
            _btn_label = "✓" if _is_sel else "Select"
            _btn_type  = "primary" if _is_sel else "secondary"
            if _any_ok:
                if row_cols[_ci].button(
                    _btn_label, key=f"cl_sel_{prod.brand}_{prod.name}",
                    use_container_width=True, type=_btn_type
                ):
                    st.session_state["_cl_page_selected_product"] = prod.name
                    st.session_state["_cl_page_selected_brand"]   = prod.brand
                    st.session_state["_cl_page_r_snap"] = r_snap
                    st.session_state["_cl_page_l_snap"] = l_snap
                    st.rerun()
            else:
                row_cols[_ci].markdown(
                    "<div style='color:#334155;font-size:0.72rem'>—</div>",
                    unsafe_allow_html=True
                )


# ─────────────────────────────────────────────────────────────────────────────
# SELECTED PRODUCT DETAIL CARD
# ─────────────────────────────────────────────────────────────────────────────

def _render_selected_card(r_calc, l_calc, vd_mm, price_map):
    sel_name  = st.session_state.get("_cl_page_selected_product")
    sel_brand = st.session_state.get("_cl_page_selected_brand")
    r_snap    = st.session_state.get("_cl_page_r_snap", {})
    l_snap    = st.session_state.get("_cl_page_l_snap", {})

    if not sel_name or not sel_brand:
        return

    prod = get_product(sel_brand, sel_name)
    if not prod:
        return

    st.markdown("---")
    st.markdown(
        f"<div style='background:#0f172a;border:1px solid #6366f155;"
        f"border-radius:10px;padding:16px 20px;margin:8px 0'>"
        f"<div style='font-size:1.05rem;font-weight:700;color:#6366f1'>"
        f"{'🌀' if prod.lens_type=='toric' else '○'} {prod.name}</div>"
        f"<div style='color:#64748b;font-size:0.78rem;margin-top:2px'>{sel_brand}</div>"
        f"</div>",
        unsafe_allow_html=True
    )

    # Features grid
    f_cols = st.columns(5)
    f_cols[0].metric("Replacement", {"daily":"Daily","monthly":"Monthly",
                                      "fortnightly":"2-Week","quarterly":"Quarterly"}.get(prod.replace,""))
    f_cols[1].metric("Material",    prod.material)
    f_cols[2].metric("Water",       f"{prod.water}%")
    f_cols[3].metric("Base Curve",  " / ".join(str(b) for b in prod.bc))
    f_cols[4].metric("Diameter",    " / ".join(str(d) for d in prod.dia))

    if prod.notes:
        st.caption(f"ℹ️ {prod.notes}")

    # CYL/AXIS range if toric
    if prod.lens_type == "toric":
        _cyls = ", ".join(str(c) for c in (prod.cyl_steps or []))
        st.caption(f"Available CYL: {_cyls}  ·  Axis: 10–180° in 10° steps")

    # SPH range
    st.caption(f"SPH range: {prod.sph_range[0]:+.2f} to {prod.sph_range[1]:+.2f}")

    # Price
    show_selling  = _can_see_selling()
    show_purchase = _can_see_purchase()
    p_row = price_map.get(prod.name, {})

    pc1, pc2, pc3 = st.columns(3)
    pc1.metric("MRP / Box",      _price_str(price_map, prod.name, "mrp"))
    if show_selling:
        pc2.metric("Selling Price", _price_str(price_map, prod.name, "selling_price"))
    if show_purchase:
        pc3.metric("Purchase Rate", _price_str(price_map, prod.name, "purchase_rate"))

    # Power result
    st.markdown("##### Calculated CL Power")
    pw1, pw2 = st.columns(2)

    _r_type = "🌀 Toric" if r_snap.get("lens_type_used")=="toric" else "○ SE"
    _l_type = "🌀 Toric" if l_snap.get("lens_type_used")=="toric" else "○ SE"

    pw1.markdown(
        f"<div style='background:#1e293b;border-radius:8px;padding:10px 14px'>"
        f"<div style='font-size:0.7rem;color:#6b7280'>👁️ RIGHT EYE &nbsp;{_r_type}</div>"
        f"<div style='font-size:1.1rem;font-weight:700;color:#10b981;margin-top:4px'>"
        f"{_rx_line(r_snap) if r_snap.get('in_range') else '⚠️ Out of range'}</div>"
        f"</div>",
        unsafe_allow_html=True
    )
    pw2.markdown(
        f"<div style='background:#1e293b;border-radius:8px;padding:10px 14px'>"
        f"<div style='font-size:0.7rem;color:#6b7280'>👁️ LEFT EYE &nbsp;{_l_type}</div>"
        f"<div style='font-size:1.1rem;font-weight:700;color:#10b981;margin-top:4px'>"
        f"{_rx_line(l_snap) if l_snap.get('in_range') else '⚠️ Out of range'}</div>"
        f"</div>",
        unsafe_allow_html=True
    )

    st.markdown("")

    # Radio: toric or SE (if applicable)
    _any_cyl   = (r_calc.get("toric") is not None) or (l_calc.get("toric") is not None)
    force_sph  = False
    if _any_cyl and prod.lens_type == "toric" and prod.cyl_steps:
        rx_choice = st.radio(
            "Prescribe as",
            ["🌀 Toric — full CYL correction", "○ Spherical Equivalent"],
            index=0, horizontal=True, key="cl_page_rx_type"
        )
        force_sph = rx_choice.startswith("○")
        if force_sph:
            r_snap = _snap_eye(r_calc, prod, force_sph=True)
            l_snap = _snap_eye(l_calc, prod, force_sph=True)
    elif _any_cyl and prod.lens_type != "toric":
        st.caption("○ Spherical product — spherical equivalent applied for CYL eyes")

    # Send to punching
    _bc1, _bc2 = st.columns([2, 1])
    with _bc1:
        if st.button("➡️ Send to Retail Punching", type="primary",
                     use_container_width=True, key="cl_page_send_retail"):
            _save_and_send(sel_brand, prod.name, vd_mm, r_snap, l_snap, "Retail Order")
    with _bc2:
        if st.button("➡️ Wholesale", use_container_width=True, key="cl_page_send_ws"):
            _save_and_send(sel_brand, prod.name, vd_mm, r_snap, l_snap, "Wholesale Order")


def _save_and_send(brand, product, vd_mm, r_snap, l_snap, target_page):
    st.session_state["_last_cl_result"] = {
        "brand":   brand,
        "product": product,
        "vd_mm":   vd_mm,
        "R": {"sph": r_snap["sph"], "cyl": r_snap["cyl"],
               "axis": r_snap["axis"], "ok": r_snap["in_range"]},
        "L": {"sph": l_snap["sph"], "cyl": l_snap["cyl"],
               "axis": l_snap["axis"], "ok": l_snap["in_range"]},
    }
    st.session_state["_cl_hint_dismissed"] = False
    # Use exact sidebar key — must match what pages.append() used in app.py
    _page_key_map = {
        "Retail Order":    "🛍️  Retail Order",
        "Wholesale Order": "📦  Wholesale Order",
    }
    st.session_state["_sidebar_page"] = _page_key_map.get(target_page, target_page)
    st.success(f"✅ Power saved — switching to {target_page}")
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN PRICE EDITOR
# ─────────────────────────────────────────────────────────────────────────────

def _render_price_admin():
    if not _can_edit_prices():
        return

    with st.expander("🔒 Admin — Update Contact Lens Prices", expanded=False):
        st.caption("Set MRP, Selling Price, and Purchase Rate per product. "
                   "Prices are stored in inventory_stock and applied across all modules.")

        from modules.sql_adapter import run_query, run_write

        all_names = [p.name for p in ALL_PRODUCTS]
        sel_name  = st.selectbox("Product", all_names, key="cl_admin_price_prod")
        prod      = next((p for p in ALL_PRODUCTS if p.name == sel_name), None)

        if not prod:
            return

        # Load current prices
        rows = run_query("""
            SELECT p.id::text AS pid,
                   COALESCE(i.mrp,0) AS mrp,
                   COALESCE(i.selling_price,0) AS sp,
                   COALESCE(i.purchase_rate,0) AS pr
            FROM products p
            LEFT JOIN inventory_stock i ON i.product_id=p.id
              AND COALESCE(i.is_active,true)=true
            WHERE p.product_name = %(n)s
            ORDER BY i.updated_at DESC NULLS LAST
            LIMIT 1
        """, {"n": sel_name}) or []

        current = rows[0] if rows else {}
        pid     = current.get("pid", "")

        ac1, ac2, ac3 = st.columns(3)
        new_mrp = ac1.number_input("MRP (per box/unit)", min_value=0.0,
                                    value=float(current.get("mrp") or 0),
                                    step=1.0, key="cl_admin_mrp")
        new_sp  = ac2.number_input("Selling Price",       min_value=0.0,
                                    value=float(current.get("sp") or 0),
                                    step=1.0, key="cl_admin_sp")
        new_pr  = ac3.number_input("Purchase Rate",       min_value=0.0,
                                    value=float(current.get("pr") or 0),
                                    step=1.0, key="cl_admin_pr")

        if st.button("💾 Save Prices", key="cl_admin_save", type="primary"):
            if not pid:
                st.warning("Product not found in DB — add it via Data Loader first.")
                return
            try:
                run_write("""
                    UPDATE inventory_stock
                    SET mrp=%(mrp)s, selling_price=%(sp)s, purchase_rate=%(pr)s,
                        updated_at=NOW()
                    WHERE product_id=%(pid)s::uuid
                      AND COALESCE(is_active,true)=true
                """, {"pid": pid, "mrp": new_mrp, "sp": new_sp, "pr": new_pr})
                _fetch_prices.clear()
                st.success(f"✅ Prices updated for {sel_name}")
            except Exception as e:
                st.error(f"Save failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PAGE RENDERER
# ─────────────────────────────────────────────────────────────────────────────

def render_cl_lens_advisor():
    st.markdown(
        "<h2 style='color:#6366f1;margin-bottom:4px'>👁️ Contact Lens Advisor</h2>"
        "<div style='color:#64748b;font-size:0.85rem;margin-bottom:16px'>"
        "Enter prescription → compare all brands → select product → send to punching"
        "</div>",
        unsafe_allow_html=True
    )

    # ── Rx Entry ─────────────────────────────────────────────────────────────
    with st.container():
        vd_col, _ = st.columns([2, 5])
        with vd_col:
            vd_mm = st.select_slider(
                "Vertex Distance (mm)",
                options=[11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0],
                value=st.session_state.get("cl_adv_vd", 12.5),
                key="cl_adv_vd_slider",
                help="Distance from back of spectacle lens to cornea."
            )
            st.session_state["cl_adv_vd"] = vd_mm

        st.markdown(
            "<div style='background:#0f172a;border:1px solid #1e293b;"
            "border-radius:8px;padding:12px 16px;margin:8px 0'>"
            "<div style='font-size:0.85rem;font-weight:700;color:#e2e8f0;"
            "margin-bottom:8px'>📋 Spectacle Prescription</div>"
            "</div>",
            unsafe_allow_html=True
        )

        r_c1, r_c2, r_c3, r_c4, _sp, l_c1, l_c2, l_c3, l_c4 = st.columns(
            [1, 1, 1, 1, 0.3, 1, 1, 1, 1]
        )
        r_c1.markdown("<small style='color:#94a3b8'>👁️ R — SPH</small>", unsafe_allow_html=True)
        r_c2.markdown("<small style='color:#94a3b8'>CYL</small>", unsafe_allow_html=True)
        r_c3.markdown("<small style='color:#94a3b8'>AXIS</small>", unsafe_allow_html=True)
        r_c4.markdown("<small style='color:#94a3b8'>ADD</small>", unsafe_allow_html=True)

        r_sph  = r_c1.number_input("R SPH",  -30.0, 30.0, 0.0, 0.25, format="%.2f",
                                    key="cl_adv_r_sph", label_visibility="collapsed")
        r_cyl  = r_c2.number_input("R CYL",  -10.0, 10.0, 0.0, 0.25, format="%.2f",
                                    key="cl_adv_r_cyl", label_visibility="collapsed")
        r_axis = r_c3.number_input("R AXIS",  0, 180, 0, 1,
                                    key="cl_adv_r_axis", label_visibility="collapsed")
        r_add  = r_c4.number_input("R ADD",   0.0, 4.0, 0.0, 0.25, format="%.2f",
                                    key="cl_adv_r_add", label_visibility="collapsed")

        l_c1.markdown("<small style='color:#94a3b8'>👁️ L — SPH</small>", unsafe_allow_html=True)
        l_c2.markdown("<small style='color:#94a3b8'>CYL</small>", unsafe_allow_html=True)
        l_c3.markdown("<small style='color:#94a3b8'>AXIS</small>", unsafe_allow_html=True)
        l_c4.markdown("<small style='color:#94a3b8'>ADD</small>", unsafe_allow_html=True)

        l_sph  = l_c1.number_input("L SPH",  -30.0, 30.0, 0.0, 0.25, format="%.2f",
                                    key="cl_adv_l_sph", label_visibility="collapsed")
        l_cyl  = l_c2.number_input("L CYL",  -10.0, 10.0, 0.0, 0.25, format="%.2f",
                                    key="cl_adv_l_cyl", label_visibility="collapsed")
        l_axis = l_c3.number_input("L AXIS",  0, 180, 0, 1,
                                    key="cl_adv_l_axis", label_visibility="collapsed")
        l_add  = l_c4.number_input("L ADD",   0.0, 4.0, 0.0, 0.25, format="%.2f",
                                    key="cl_adv_l_add", label_visibility="collapsed")

        btn_col, info_col = st.columns([2, 5])
        with btn_col:
            calc_clicked = st.button(
                "🔄 Calculate & Compare",
                type="primary", use_container_width=True,
                key="cl_adv_calc"
            )

    # ── Calculate ─────────────────────────────────────────────────────────────
    if calc_clicked:
        r_calc = _eye_calc(r_sph, r_cyl, int(r_axis), vd_mm)
        l_calc = _eye_calc(l_sph, l_cyl, int(l_axis), vd_mm)
        st.session_state["_cl_adv_r_calc"] = r_calc
        st.session_state["_cl_adv_l_calc"] = l_calc
        st.session_state["_cl_adv_done"]   = True
        st.session_state.pop("_cl_page_selected_product", None)

    if not st.session_state.get("_cl_adv_done"):
        st.info("Enter prescription and click **Calculate & Compare** to see all options.")
        return

    r_calc = st.session_state["_cl_adv_r_calc"]
    l_calc = st.session_state["_cl_adv_l_calc"]

    # ── VD correction summary ─────────────────────────────────────────────────
    with st.expander("🔬 VD Correction Working (transparent)", expanded=False):
        w1, w2 = st.columns(2)
        with w1:
            st.markdown("**Right Eye**")
            if r_calc.get("toric"):
                t = r_calc["toric"]
                st.caption(f"Toric: mer1={t['vc_sph']:+.4f} / mer2 diff={t['vc_cyl']:+.4f} @ {t['vc_axis']}°")
            s = r_calc["sph"]
            st.caption(f"SE: {r_sph:+.2f} + ({r_cyl:+.2f}/2) = {s['se_spec']:+.2f} → VD {s['vc_se']:+.4f}")
        with w2:
            st.markdown("**Left Eye**")
            if l_calc.get("toric"):
                t = l_calc["toric"]
                st.caption(f"Toric: mer1={t['vc_sph']:+.4f} / mer2 diff={t['vc_cyl']:+.4f} @ {t['vc_axis']}°")
            s = l_calc["sph"]
            st.caption(f"SE: {l_sph:+.2f} + ({l_cyl:+.2f}/2) = {s['se_spec']:+.2f} → VD {s['vc_se']:+.4f}")

    # ── Fetch prices ──────────────────────────────────────────────────────────
    all_names = tuple(p.name for p in ALL_PRODUCTS)
    price_map = _fetch_prices(all_names)

    # ── Comparison table ──────────────────────────────────────────────────────
    _render_comparison(r_calc, l_calc, vd_mm, price_map)

    # ── Selected product detail ───────────────────────────────────────────────
    _render_selected_card(r_calc, l_calc, vd_mm, price_map)

    # ── Admin price editor ────────────────────────────────────────────────────
    _render_price_admin()

    # ── Legend ───────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(
        "<div style='font-size:0.72rem;color:#475569'>"
        "🟢 Green = Toric CL power &nbsp;·&nbsp; 🟡 Amber = Spherical Equivalent &nbsp;·&nbsp; "
        "— = Out of range for this prescription"
        "</div>",
        unsafe_allow_html=True
    )
