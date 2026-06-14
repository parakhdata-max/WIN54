"""
ophthalmic_billing.py
─────────────────────
UI layer for ophthalmic lens billing.
Used by retail_punching, wholesale_punching, bulk_order.

Deploy to: modules/ophthalmic_billing.py

Key exports:
  render_ophthalmic_selector()      — Index + Coating dropdowns + price
  render_availability_grid()         — Expandable full matrix with select
  ophthalmic_unit_price()            — Per-lens price for cart
"""
from __future__ import annotations
import streamlit as st


# ── Import data layer ─────────────────────────────────────────────────────────
try:
    from modules.ophthalmic_specs import (
        get_all_specs_for_product,
        get_index_options,
        get_coating_options,
        get_treatment_options,
        get_spec_price,
        check_stock,
        get_addons_for_product,
    )
    _HAS_SPECS = True
except ImportError:
    _HAS_SPECS = False
    def get_all_specs_for_product(pid): return []
    def get_index_options(pid): return []
    def get_coating_options(pid, idx): return []
    def get_treatment_options(pid, idx, coat): return ["Clear"]
    def get_spec_price(*a, **k): return {"wlp":0,"srp":0,"purchase":0,"selling":0,"found":False}
    def check_stock(*a, **k): return {"status":"RX_ORDER","qty_r":0,"qty_l":0,"batch_no":"","message":""}
    def get_addons_for_product(*a, **k): return []


# ── Session key helpers ───────────────────────────────────────────────────────

def _k(prefix: str, field: str) -> str:
    return f"oph_{prefix}_{field}"


def _read(prefix: str, field: str, default=None):
    return st.session_state.get(_k(prefix, field), default)


def _write(prefix: str, field: str, value) -> None:
    st.session_state[_k(prefix, field)] = value


def _has_rx_power(rx: dict) -> bool:
    if not rx:
        return False
    for key in ("sph", "cyl", "add", "add_power"):
        val = rx.get(key)
        if val not in (None, "", 0, 0.0, "0", "0.0", "0.00"):
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# AVAILABILITY GRID — expandable matrix of all index × coating combinations
# ══════════════════════════════════════════════════════════════════════════════

def render_availability_grid(
    product_id: str,
    product_name: str,
    rx_r: dict = None,
    rx_l: dict = None,
    key_prefix: str = "main",
    order_type: str = "RETAIL",
) -> None:
    """
    Expandable grid showing ALL index × coating × treatment combinations
    with live prices and stock status for the entered powers.

    When user clicks a row → it pre-fills the main dropdowns via session state.
    """
    if not product_id:
        return

    has_rx_r = _has_rx_power(rx_r)
    has_rx_l = _has_rx_power(rx_l) or (not rx_l and has_rx_r)

    sph_r  = float((rx_r or {}).get("sph") or 0)
    cyl_r  = float((rx_r or {}).get("cyl") or 0)
    axis_r = int((rx_r or {}).get("axis") or 0)
    add_r  = float((rx_r or {}).get("add") or 0)
    sph_l  = float((rx_l or {}).get("sph") or sph_r)
    cyl_l  = float((rx_l or {}).get("cyl") or cyl_r)
    axis_l = int((rx_l or {}).get("axis") or axis_r)
    add_l  = float((rx_l or {}).get("add") or add_r)

    with st.expander(
        f"📊 All Available Combinations — {product_name}",
        expanded=False
    ):
        # Cache specs in session to avoid re-query on every render
        _cache_key = f"_oph_specs_{product_id}"
        if _cache_key not in st.session_state:
            st.session_state[_cache_key] = get_all_specs_for_product(product_id)
        all_specs = st.session_state[_cache_key]

        if not all_specs:
            st.warning("No spec data found. Run migration and load price list first.")
            return

        # Stock check is lazy — only run when user explicitly requests it
        _stock_key = f"_oph_grid_stock_{product_id}"
        _show_stock = st.toggle("🔍 Check stock for entered powers", key=f"grid_stock_toggle_{key_prefix}")

        st.caption(
            "Click **Select** on any row to auto-fill the dropdowns."
            + (" 🟢 = in stock | 📋 = RX order basis" if _show_stock else
               " Toggle 'Check stock' above to see availability.")
        )

        # ── Headers — differ by order_type ──────────────────────────────────
        _is_ws = (order_type == "WHOLESALE")
        if _is_ws:
            # Wholesale: show WLP (selling to shop) + MRP both
            _col_widths  = [1.5, 3.0, 1.5, 1.8, 1.8, 1.5, 1.5, 1.5]
            _col_headers = ["Index","Coating","Treatment",
                            "Selling/pair (WLP)","MRP / pair",
                            "👁️ RIGHT","👁️ LEFT",""]
        else:
            # Retail: MRP only — WLP never visible to customer
            _col_widths  = [1.5, 3.5, 1.5, 2.2, 1.5, 1.5, 1.5]
            _col_headers = ["Index","Coating","Treatment",
                            "MRP / pair",
                            "👁️ RIGHT","👁️ LEFT",""]

        hcols = st.columns(_col_widths)
        for col, label in zip(hcols, _col_headers):
            col.markdown(f"**{label}**")
        st.divider()

        # ── Rows ──────────────────────────────────────────────────────────────
        for i, spec in enumerate(all_specs):
            idx   = spec.get("index_value", "")
            coat  = spec.get("coating", "")
            treat = spec.get("treatment", "Clear")
            wlp   = float(spec.get("wlp_per_pair") or 0)
            srp   = float(spec.get("srp_per_pair")  or 0)
            cst   = float(spec.get("purchase_rate") or 0)

            # Stock check only when toggle is on
            if _show_stock and has_rx_r:
                stk_r = check_stock(product_id, sph_r, cyl_r, axis_r, add_r, idx, coat, "R")
            else:
                stk_r = {"status": "RX_ORDER", "qty_r": 0, "qty_l": 0, "batch_no": "", "message": ""}
            if _show_stock and has_rx_l:
                stk_l = check_stock(product_id, sph_l, cyl_l, axis_l, add_l, idx, coat, "L")
            else:
                stk_l = {"status": "RX_ORDER", "qty_r": 0, "qty_l": 0, "batch_no": "", "message": ""}
            if _show_stock:
                r_icon = f"🟢 {stk_r['qty_r']}" if stk_r["status"] == "STOCK" else "📋"
                l_icon = f"🟢 {stk_l['qty_l']}" if stk_l["status"] == "STOCK" else "📋"
                in_stock = stk_r["status"] == "STOCK" or stk_l["status"] == "STOCK"
            else:
                r_icon = "—"; l_icon = "—"; in_stock = False

            rcols = st.columns(_col_widths)
            rcols[0].write(f"**{idx}**")
            rcols[1].write(coat)
            rcols[2].write(treat)
            if _is_ws:
                rcols[3].write(f"₹{wlp:,.0f}" if wlp else "—")
                rcols[4].write(f"₹{srp:,.0f}" if srp else "—")
                rcols[5].write(r_icon)
                rcols[6].write(l_icon)
            else:
                # Retail: show SRP if available, else WLP (some indices have no SRP)
                _retail_price = srp if srp else wlp
                rcols[3].write(f"₹{_retail_price:,.0f}" if _retail_price else "—")
                rcols[4].write(r_icon)
                rcols[5].write(l_icon)

            btn_label = "✅ Select" if in_stock else "Select"
            _btn_col = 7 if _is_ws else 6
            if rcols[_btn_col].button(
                btn_label,
                key=f"oph_grid_sel_{key_prefix}_{i}",
                type="primary" if in_stock else "secondary",
                use_container_width=True,
            ):
                _write(key_prefix, "index",     idx)
                _write(key_prefix, "coating",   coat)
                _write(key_prefix, "treatment", treat)
                st.rerun()

            if in_stock:
                st.divider()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN OPHTHALMIC SELECTOR
# ══════════════════════════════════════════════════════════════════════════════

def render_ophthalmic_selector(
    product_id: str,
    product_name: str,
    rx_r: dict = None,
    rx_l: dict = None,
    order_type: str = "RETAIL",
    key_prefix: str = "main",
) -> dict:
    """
    Render:
      1. Index → Coating → Treatment dropdowns (chained)
      2. Price from ophthalmic_lens_specs
      3. Stock status for R and L eyes
      4. Expandable availability grid

    Returns:
    {
      'complete':   bool,
      'index':      str,
      'coating':    str,
      'treatment':  str,
      'price':      dict,   # {wlp, srp, purchase, selling, found}
      'stock_r':    dict,   # {status, qty_r, qty_l, batch_no}
      'stock_l':    dict,
    }
    """
    if not product_id:
        return {"complete": False}
    order_type = "WHOLESALE" if str(order_type or "").upper() == "WHOLESALE" else "RETAIL"

    st.markdown("---")
    st.markdown("#### 🔬 Lens Specification")

    # ── Pre-filled value from grid selection (via session state) ─────────────
    pre_idx   = _read(key_prefix, "index",     None)
    pre_coat  = _read(key_prefix, "coating",   None)
    pre_treat = _read(key_prefix, "treatment", "Clear")

    # ── Dropdowns ────────────────────────────────────────────────────────────
    col1, col2, col3 = st.columns([2, 3, 2])

    with col1:
        _idx_cache_key = f"_oph_idx_{product_id}"
        if _idx_cache_key not in st.session_state:
            st.session_state[_idx_cache_key] = get_index_options(product_id)
        idx_opts = st.session_state[_idx_cache_key]
        if not idx_opts:
            st.warning("⚠️ No specs loaded. Run `migration_ophthalmic_lenses.sql` then load price list.")
            return {"complete": False}
        idx_list = ["Select..."] + idx_opts
        idx_default = idx_list.index(pre_idx) if pre_idx in idx_list else 0
        _idx_key = _k(key_prefix, "index")
        _idx_kwargs = {"key": _idx_key}
        if _idx_key not in st.session_state:
            _idx_kwargs["index"] = idx_default
        sel_idx = st.selectbox("🔢 Index", idx_list, **_idx_kwargs)

    with col2:
        coat_opts = get_coating_options(product_id, sel_idx) if sel_idx != "Select..." else []
        coat_list = ["Select..."] + coat_opts
        coat_default = coat_list.index(pre_coat) if pre_coat in coat_list else 0
        _coat_key = _k(key_prefix, "coating")
        _coat_kwargs = {"key": _coat_key, "disabled": not coat_opts}
        if _coat_key not in st.session_state:
            _coat_kwargs["index"] = coat_default
        sel_coat = st.selectbox("🛡️ Coating", coat_list, **_coat_kwargs)

    with col3:
        treat_opts = (
            get_treatment_options(product_id, sel_idx, sel_coat)
            if sel_idx != "Select..." and sel_coat != "Select..." else ["Clear"]
        )
        treat_default = treat_opts.index(pre_treat) if pre_treat in treat_opts else 0
        _treat_key = _k(key_prefix, "treatment")
        if len(treat_opts) > 1:
            _treat_kwargs = {"key": _treat_key}
            if _treat_key not in st.session_state:
                _treat_kwargs["index"] = treat_default
            sel_treat = st.selectbox("🌿 Add-on (Photochromic/Tinted)", treat_opts, **_treat_kwargs)
        else:
            sel_treat = treat_opts[0]
            st.selectbox("✨ Treatment", [sel_treat], key=_treat_key, disabled=True)

    # ── Availability grid (always shown, collapsed by default) ───────────────
    render_availability_grid(
        product_id, product_name,
        rx_r=rx_r, rx_l=rx_l,
        key_prefix=key_prefix,
        order_type=order_type,
    )

    # ── Not complete yet ──────────────────────────────────────────────────────
    if sel_idx == "Select..." or sel_coat == "Select..." or not sel_coat:
        st.info("⬆️ Select Index and Coating to see price and stock.")
        return {"complete": False, "index": None, "coating": None, "treatment": "Clear"}

    # ── Price from spec table ─────────────────────────────────────────────────
    price = get_spec_price(product_id, sel_idx, sel_coat, sel_treat, order_type)

    # ── Stock for R and L ─────────────────────────────────────────────────────
    has_rx_r = _has_rx_power(rx_r)
    has_rx_l = _has_rx_power(rx_l) or (not rx_l and has_rx_r)

    sph_r  = float((rx_r or {}).get("sph") or 0)
    cyl_r  = float((rx_r or {}).get("cyl") or 0)
    axis_r = int((rx_r or {}).get("axis") or 0)
    add_r  = float((rx_r or {}).get("add") or 0)
    sph_l  = float((rx_l or {}).get("sph") or sph_r)
    cyl_l  = float((rx_l or {}).get("cyl") or cyl_r)
    axis_l = int((rx_l or {}).get("axis") or axis_r)
    add_l  = float((rx_l or {}).get("add") or add_r)

    stk_r = (
        check_stock(product_id, sph_r, cyl_r, axis_r, add_r, sel_idx, sel_coat, "R")
        if has_rx_r else
        {"status": "RX_ORDER", "qty_r": 0, "qty_l": 0, "batch_no": "", "message": "Enter power"}
    )
    stk_l = (
        check_stock(product_id, sph_l, cyl_l, axis_l, add_l, sel_idx, sel_coat, "L")
        if has_rx_l else
        {"status": "RX_ORDER", "qty_r": 0, "qty_l": 0, "batch_no": "", "message": "Enter power"}
    )

    # ── Price + Stock dashboard ───────────────────────────────────────────────
    st.markdown("---")
    m1, m2, m3, m4 = st.columns(4)

    # Resolve price via governor:
    # RETAIL  → selling = SRP (MRP), fallback to WLP if SRP null
    # WHOLESALE → selling = WLP (trade price), show SRP separately
    _is_ws = (order_type == "WHOLESALE")
    _wlp   = float(price.get("wlp") or 0)
    _srp   = float(price.get("srp") or 0)

    if _is_ws:
        _sell = _wlp if _wlp > 0 else _srp   # WS: show WLP, never override with SRP
    else:
        _sell = _srp if _srp > 0 else _wlp   # Retail: SRP first, WLP fallback

    with m1:
        if price["found"] and _sell > 0:
            label = "💰 WLP / pair" if _is_ws else "💰 Selling / pair"
            st.metric(label, f"₹{_sell:,.0f}")
        elif price["found"]:
            st.warning("Price not set — check spec file")
        else:
            st.error("Price not found")

    with m2:
        if price["found"] and _srp > 0:
            st.metric("🏷️ MRP / pair", f"₹{_srp:,.0f}")
        elif price["found"] and _is_ws:
            st.metric("🏷️ MRP / pair", "Not set")

    with m3:
        if stk_r["status"] == "STOCK":
            st.metric("👁️ RIGHT", f"✅ {stk_r['qty_r']} pcs",
                      help=f"Batch: {stk_r['batch_no']}")
        else:
            st.metric("👁️ RIGHT", "📋 RX Order")

    with m4:
        if stk_l["status"] == "STOCK":
            st.metric("👁️ LEFT", f"✅ {stk_l['qty_l']} pcs",
                      help=f"Batch: {stk_l['batch_no']}")
        else:
            st.metric("👁️ LEFT", "📋 RX Order")

    # ── Add-ons (optional upgrades) ──────────────────────────────────────────
    # Get brand + lens_category from spec table for add-on lookup
    _all_specs = get_all_specs_for_product(product_id)
    _brand_from_spec = next((s.get("brand","") for s in _all_specs), "")
    _lens_cat = next(
        (s.get("lens_category","ALL") for s in _all_specs
         if str(s.get("index_value","")) == str(sel_idx)
         and s.get("coating") == sel_coat),
        "ALL"
    )
    # Pass corrected selling price (WLP for wholesale, SRP for retail)
    _base_for_addon = dict(price)
    _base_for_addon["selling"] = _sell  # _sell is already resolved above
    _addon_result = render_addon_selector(
        brand        = _brand_from_spec,
        lens_category= _lens_cat,
        base_price   = _base_for_addon,
        order_type   = order_type,
        key_prefix   = key_prefix,
        product_id   = product_id,
    )

    # ── Status banner ─────────────────────────────────────────────────────────
    r_ok = stk_r["status"] == "STOCK"
    l_ok = stk_l["status"] == "STOCK"

    spec_label = f"**{sel_idx}** | {sel_coat}"
    if sel_treat and sel_treat != "Clear":
        spec_label += f" | {sel_treat}"

    if r_ok and l_ok:
        st.success(f"✅ Both lenses in stock — {spec_label}")
    elif r_ok:
        st.warning(f"⚡ RIGHT in stock — LEFT → RX order | {spec_label}")
    elif l_ok:
        st.warning(f"⚡ LEFT in stock — RIGHT → RX order | {spec_label}")
    else:
        st.info(f"📋 RX Order basis — {spec_label}")

    # ── Bake addon into final price ──────────────────────────────────────────
    _final_price = dict(price)
    if _addon_result and _addon_result.get("selected"):
        _final_price["selling"] = _addon_result["final_selling"]
        _final_price["wlp"]     = _addon_result["final_wlp"]
        _final_price["srp"]     = _addon_result["final_srp"]
        _final_price["addon_label"] = _addon_result["addon_label"]

    return {
        "complete":         True,
        "index":            sel_idx,
        "coating":          sel_coat,
        "treatment":        sel_treat,
        "price":            _final_price,   # already includes add-on
        "stock_r":          stk_r,
        "stock_l":          stk_l,
        "addons":           _addon_result,
        # Convenience: full display label for product name in cart
        "display_suffix":   (f" + {sel_idx} {sel_coat}"
                             + (_addon_result.get("addon_label") or "")
                             + (f" ({sel_treat})" if sel_treat != "Clear" else "")),
    }



def render_addon_selector(
    brand: str,
    lens_category: str,
    base_price: dict,
    order_type: str = "RETAIL",
    key_prefix: str = "main",
    product_id: str = None,
) -> dict:
    """
    Multiselect dropdown for optional add-ons.
    Add-on amounts BAKED INTO the returned final_selling price.
    No separate line needed in billing/challan/invoice.
    """
    empty = {
        "selected": [], "addon_label": "",
        "final_wlp":     float(base_price.get("wlp")     or 0) if base_price else 0,
        "final_srp":     float(base_price.get("srp")      or 0) if base_price else 0,
        "final_selling": float(base_price.get("selling")  or 0) if base_price else 0,
        "total_wlp_addon": 0, "total_srp_addon": 0,
    }

    addons = get_addons_for_product(brand, lens_category, product_id)
    if not addons:
        return empty

    base_wlp  = float(base_price.get("wlp")     or 0) if base_price else 0
    base_srp  = float(base_price.get("srp")      or 0) if base_price else 0
    base_sell = float(base_price.get("selling")  or 0) if base_price else 0

    _is_ws = order_type == "WHOLESALE"

    def _opt_label(a):
        amt = float(a.get("wlp_addon") or 0) if _is_ws else float(a.get("srp_addon") or 0)
        price_str = f"  +₹{amt:,.0f}/pair" if amt else ""
        return f"{a['addon_name']}{price_str}"

    opt_labels  = [_opt_label(a) for a in addons]
    addon_names = [a["addon_name"] for a in addons]
    addon_map   = {a["addon_name"]: a for a in addons}

    st.markdown("---")
    st.markdown("**➕ Optional Add-ons** (Transitions, Blue Capture, EyeCode…)")
    sel_labels = st.multiselect(
        "Select add-ons to include in price",
        options=opt_labels,
        default=[],
        key=f"addon_ms_{key_prefix}",
        placeholder="Select add-ons to include in price…",
        help="Selected add-ons are baked into the product price. Single line in billing.",
    )

    selected_names = [addon_names[opt_labels.index(lbl)]
                      for lbl in sel_labels if lbl in opt_labels]

    total_wlp = sum(float(addon_map[n].get("wlp_addon") or 0) for n in selected_names)
    total_srp = sum(float(addon_map[n].get("srp_addon") or 0) for n in selected_names)

    final_wlp  = base_wlp  + total_wlp
    final_srp  = base_srp  + total_srp
    final_sell = base_sell + (total_wlp if _is_ws else total_srp)

    addon_label = (" + " + " + ".join(selected_names)) if selected_names else ""

    if selected_names:
        addon_amt = total_wlp if _is_ws else total_srp
        st.caption(
            f"Base ₹{base_sell:,.0f} + add-ons ₹{addon_amt:,.0f}"
            f" = **₹{final_sell:,.0f} / pair** ✅ baked into product price"
        )

    return {
        "selected":          selected_names,
        "addon_label":       addon_label,
        "final_wlp":         final_wlp,
        "final_srp":         final_srp,
        "final_selling":     final_sell,
        "total_wlp_addon":   total_wlp,
        "total_srp_addon":   total_srp,
    }


def ophthalmic_unit_price(spec_result: dict, order_type: str = "RETAIL") -> float:
    """
    Per-lens price (pair ÷ 2). Add-ons already baked into price.selling.
    One clean price — no downstream add-on logic needed.
    """
    if not spec_result or not spec_result.get("complete"):
        return 0.0
    price = spec_result.get("price", {})
    pair_price = float(price.get("selling") or price.get("wlp") or 0)
    return round(pair_price / 2, 2)


def ophthalmic_display_name(product_name: str, spec_result: dict) -> str:
    """
    Full display name: "Varilux X Series 1.60 Crizal Prevencia + Blue Capture"
    Used in cart / billing / challan / invoice — no special processing downstream.
    """
    if not spec_result or not spec_result.get("complete"):
        return product_name
    suffix = spec_result.get("display_suffix", "")
    return f"{product_name}{suffix}"
