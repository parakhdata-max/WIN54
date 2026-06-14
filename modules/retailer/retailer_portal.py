"""
modules/retailer/retailer_portal.py  v4
=========================================
Universal stock-led portal.
- Cached filters (5 min)
- Universal card renderer
- R/L eye grouping for CL
- Pagination (Load More)
- Reorder with stock validation
- Clean state management
"""
import streamlit as st
import pandas as pd

from modules.retailer.retailer_auth import (
    get_party_by_mobile, send_otp, verify_otp, validate_token, logout,
    has_password, verify_password, set_password, create_session,
)
from modules.retailer.retailer_orders import (
    get_portal_home, get_categories, get_category_type,
    get_dynamic_filters, get_payment_methods,
    get_matching_stock, lookup_by_sku,
    get_reorder_items, validate_reorder_item,
    get_favourites, toggle_favourite,
    build_item_detail,
    cart_add, cart_update_qty, cart_remove, cart_clear,
    cart_get, cart_totals, create_order,
    get_my_orders, get_order_lines,
    create_razorpay_order, update_payment_status,
)


# ── Cached filter fetch ────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def _cached_filters(category: str) -> dict:
    return get_dynamic_filters(category)

@st.cache_data(ttl=300, show_spinner=False)
def _cached_categories() -> list:
    return get_categories()


def render_retailer_portal():
    st.set_page_config(page_title="Order Portal", page_icon="🛍️",
                       layout="wide", initial_sidebar_state="collapsed")
    _css()
    token = st.session_state.get("retailer_token")
    party = validate_token(token) if token else None
    if not party:
        _login(); return

    home = st.session_state.get("_ph")
    if not home or st.session_state.get("_php") != party["id"]:
        home = get_portal_home(party["id"])
        st.session_state.update(_ph=home, _php=party["id"])

    if home.get("is_blocked"):
        st.error(f"⛔ Account blocked: {home.get('block_reason','Contact support')}")
        st.button("🚪 Logout", on_click=_logout); return

    _header(party, home)
    tabs = st.tabs(["⚡ Order","🔍 Scan","🔁 Reorder","🛒 Cart","📋 Orders"])
    with tabs[0]: _order_tab(party, home)
    with tabs[1]: _scan_tab(party, home)
    with tabs[2]: _reorder_tab(party)
    with tabs[3]: _cart_tab(party, home)
    with tabs[4]: _orders_tab(party)


# ══════════════════════════════════════════════════════════════
# LOGIN
# ══════════════════════════════════════════════════════════════

def _logout():
    logout(st.session_state.get("retailer_token",""))
    for k in list(st.session_state.keys()):
        if k.startswith(("retailer_","_ph","portal_","load_","pg_","dyn_","dev_otp","login_","verified_")):
            st.session_state.pop(k, None)

def _login():
    st.markdown("<div style='max-width:400px;margin:50px auto;text-align:center'>"
                "<h2>🛍️ Retailer Portal</h2>"
                "<p style='color:#666'>Enter your registered mobile</p></div>",
                unsafe_allow_html=True)
    col = st.columns([1,2,1])[1]
    with col:
        step = st.session_state.get("login_step","mobile")

        if step == "mobile":
            mob = st.text_input("📱 Mobile", placeholder="10 digits", key="li_mob")
            if st.button("Continue →", type="primary", use_container_width=True):
                if len(mob.strip()) < 10:
                    st.error("Enter 10-digit mobile")
                else:
                    p = get_party_by_mobile(mob)
                    if not p:
                        st.error("❌ Not registered. Contact your sales rep.")
                    elif not p.get("is_active"):
                        st.error("❌ Account inactive.")
                    else:
                        st.session_state.update(login_mobile=mob, login_party=p)
                        if has_password(p):
                            st.session_state.login_step = "password"
                        else:
                            r = send_otp(mob, p["party_name"])
                            if r["success"]:
                                st.session_state.login_step = "otp_first"
                                if r.get("otp"): st.session_state.dev_otp = r["otp"]
                            else: st.error(r["message"])
                        st.rerun()

        elif step == "password":
            p   = st.session_state.get("login_party",{})
            mob = st.session_state.get("login_mobile","")
            st.success(f"👋 {p.get('party_name','')}")
            pw = st.text_input("🔑 Password", type="password", key="li_pw")
            if st.button("Login →", type="primary", use_container_width=True):
                # Re-fetch from DB — ensures we get the latest portal_password hash
                fresh_party = get_party_by_mobile(mob)
                if not fresh_party:
                    st.error("Party not found. Try again.")
                elif verify_password(fresh_party, pw):
                    tok = create_session(fresh_party)
                    if tok:
                        st.session_state.update(retailer_token=tok,
                                                retailer_party=fresh_party)
                        st.session_state.login_step = "mobile"
                        cart_clear(); st.rerun()
                    else: st.error("Session error, try again")
                else: st.error("❌ Wrong password")
            st.markdown("---")
            if st.button("Forgot password?", use_container_width=True):
                r = send_otp(mob, p.get("party_name",""))
                if r["success"]:
                    st.session_state.login_step = "otp_reset"
                    if r.get("otp"): st.session_state.dev_otp = r["otp"]
                    st.rerun()
                else: st.error(r["message"])
            if st.button("← Change number", use_container_width=True):
                st.session_state.login_step = "mobile"; st.rerun()

        elif step in ("otp_first","otp_reset"):
            mob = st.session_state.get("login_mobile","")
            st.info("📲 Enter OTP" + (" — First login" if step=="otp_first" else " — Reset password"))
            dov = st.session_state.get("dev_otp")
            if dov: st.warning(f"🔑 **[DEV] OTP: `{dov}`**")
            otp = st.text_input("OTP", max_chars=6, key=f"li_{step}")
            c1,c2 = st.columns(2)
            with c1:
                if st.button("✅ Verify", type="primary", use_container_width=True):
                    r = verify_otp(mob, otp)
                    if r["success"]:
                        st.session_state.update(verified_party=r["party"])
                        st.session_state.login_step = "set_password"; st.rerun()
                    else: st.error(r["message"])
            with c2:
                if st.button("← Back", use_container_width=True):
                    st.session_state.login_step = "mobile" if step=="otp_first" else "password"; st.rerun()

        elif step == "set_password":
            p = st.session_state.get("verified_party",{})
            st.success(f"✅ Welcome {p.get('party_name','')} — set your password")
            p1 = st.text_input("New Password", type="password", key="sp1", placeholder="Min 6 chars")
            p2 = st.text_input("Confirm", type="password", key="sp2")
            if st.button("Set Password & Login", type="primary", use_container_width=True):
                if len(p1) < 6: st.error("Min 6 characters")
                elif p1 != p2: st.error("Passwords don't match")
                else:
                    if set_password(str(p["id"]), p1):
                        tok = create_session(p)
                        st.session_state.update(retailer_token=tok, retailer_party=p)
                        st.session_state.login_step = "mobile"
                        for k in ["verified_party","dev_otp"]: st.session_state.pop(k,None)
                        cart_clear(); st.rerun()
                    else: st.error("Failed to save password")


# ══════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════

def _header(party:dict, home:dict):
    cart   = cart_get()
    cart_n = sum(i["qty"] for i in cart.values())
    h1,h2,h3 = st.columns([3,1,1])
    with h1:
        st.markdown(f"### 🛍️ {party.get('party_name','')}")
        parts = []
        if home["credit_limit"]:
            pct = home["outstanding"]/home["credit_limit"]*100 if home["credit_limit"] else 0
            icon = "🔴" if pct>80 else ("🟡" if pct>50 else "🟢")
            parts.append(f"{icon} Credit: ₹{home['avail_credit']:,.0f} free")
        if home["cash_disc_pct"]:
            parts.append(f"💰 Cash = {home['cash_disc_pct']:.0f}% OFF")
        if parts: st.caption("  |  ".join(parts))
    with h2:
        if cart_n: st.markdown(f"**🛒 {cart_n}**")
    with h3:
        st.button("🚪 Logout", use_container_width=True, on_click=_logout)
    st.divider()


# ══════════════════════════════════════════════════════════════
# ORDER TAB — stock-led flow
# ══════════════════════════════════════════════════════════════

def _order_tab(party:dict, home:dict):
    categories = _cached_categories()
    if not categories:
        st.warning("No stock available."); return

    # Category buttons
    st.markdown("**Select Category**")
    cur_cat = st.session_state.get("portal_category","")
    cols = st.columns(min(len(categories), 4))
    for i, cat in enumerate(categories):
        with cols[i % 4]:
            selected = (cur_cat == cat)
            if st.button(("✅ " if selected else "") + cat, key=f"cat_{cat}",
                         use_container_width=True,
                         type="primary" if selected else "secondary"):
                if cur_cat == cat:
                    for k in ["portal_category","load_stock","pg_offset"]:
                        st.session_state.pop(k, None)
                else:
                    st.session_state.update(portal_category=cat)
                    st.session_state.pop("load_stock", None)
                    st.session_state.pop("pg_offset",  None)
                st.rerun()

    category = st.session_state.get("portal_category","")
    if not category:
        st.info("Select a category above."); return

    st.divider()
    cat_type = get_category_type(category)
    dyn      = _cached_filters(category)

    # Filters
    st.markdown("**Select Filters**")
    if cat_type == "CONTACT_LENS":
        filters = _cl_filters(dyn)
    elif cat_type == "FRAME":
        filters = _frame_filters(dyn)
    else:
        filters = _general_filters(dyn)

    st.divider()

    # Show Stock button
    b1,b2 = st.columns([1,3])
    with b1:
        if st.button("🔍 Show Available Stock", type="primary",
                     use_container_width=True):
            st.session_state["load_stock"]     = True
            st.session_state["active_filters"] = filters
            st.session_state["pg_offset"]      = 0
    with b2:
        if st.session_state.get("load_stock"):
            if st.button("🔄 Clear / New Search", use_container_width=True):
                for k in ["load_stock","active_filters","pg_offset"]:
                    st.session_state.pop(k, None)
                st.rerun()

    if not st.session_state.get("load_stock"):
        return

    af     = st.session_state.get("active_filters", filters)
    offset = st.session_state.get("pg_offset", 0)
    limit  = 60

    with st.spinner("Loading stock..."):
        rows = get_matching_stock(category, af, limit=limit, offset=offset)

    if not rows:
        st.warning("No matching stock found.")
        _suggest_alternatives(category, af); return

    st.success(f"✅ {len(rows)} SKU(s)  " +
               (f"(showing {offset+1}–{offset+len(rows)})" if offset else ""))
    st.divider()

    if cat_type == "CONTACT_LENS":
        _render_cl_results(rows, party)
    else:
        _render_universal_grid(rows, party)

    # Pagination
    st.markdown("---")
    pc1, pc2, pc3 = st.columns([1,2,1])
    with pc1:
        if offset > 0:
            if st.button("← Previous", use_container_width=True):
                st.session_state["pg_offset"] = max(0, offset - limit); st.rerun()
    with pc3:
        if len(rows) == limit:
            if st.button("Next →", use_container_width=True):
                st.session_state["pg_offset"] = offset + limit; st.rerun()


# ── Filter UIs ────────────────────────────────────────────────

def _cl_filters(dyn:dict) -> dict:
    brands   = ["All"] + (dyn.get("brands") or [])
    products = ["All"] + (dyn.get("products") or [])
    c1,c2 = st.columns(2)
    brand   = c1.selectbox("Brand",   brands,   key="cl_brand")
    product = c2.selectbox("Product", products, key="cl_product",
                           on_change=lambda: st.session_state.pop("load_stock",None))

    st.markdown("**👁️ Right Eye (OD)**")
    r1,r2,r3,r4 = st.columns(4)
    sph_list = [""] + [str(v) for v in (dyn.get("sph") or [])]
    cyl_list = [""] + [str(v) for v in (dyn.get("cyl") or [])]
    ax_list  = [""] + [str(v) for v in (dyn.get("axis") or [])]
    add_list = [""] + [str(v) for v in (dyn.get("add_power") or [])]

    r_sph  = r1.selectbox("SPH",  sph_list, key="cl_r_sph",
                           on_change=lambda: st.session_state.pop("load_stock",None))
    r_cyl  = r2.selectbox("CYL",  cyl_list, key="cl_r_cyl",
                           on_change=lambda: st.session_state.pop("load_stock",None))
    r_axis = r3.selectbox("AXIS", ax_list,  key="cl_r_axis", disabled=not r_cyl,
                           on_change=lambda: st.session_state.pop("load_stock",None))
    r_add  = r4.selectbox("ADD",  add_list, key="cl_r_add",
                           on_change=lambda: st.session_state.pop("load_stock",None))

    st.markdown("**👁️ Left Eye (OS)**")
    l1,l2,l3,l4 = st.columns(4)
    l_sph  = l1.selectbox("SPH",  sph_list, key="cl_l_sph",
                           on_change=lambda: st.session_state.pop("load_stock",None))
    l_cyl  = l2.selectbox("CYL",  cyl_list, key="cl_l_cyl",
                           on_change=lambda: st.session_state.pop("load_stock",None))
    l_axis = l3.selectbox("AXIS", ax_list,  key="cl_l_axis", disabled=not l_cyl,
                           on_change=lambda: st.session_state.pop("load_stock",None))
    l_add  = l4.selectbox("ADD",  add_list, key="cl_l_add",
                           on_change=lambda: st.session_state.pop("load_stock",None))

    return {
        "brand":   brand   if brand   != "All" else "",
        "product": product if product != "All" else "",
        "r_sph":  float(r_sph)  if r_sph  else None,
        "r_cyl":  float(r_cyl)  if r_cyl  else None,
        "r_axis": int(r_axis)   if r_axis else None,
        "r_add":  float(r_add)  if r_add  else None,
        "l_sph":  float(l_sph)  if l_sph  else None,
        "l_cyl":  float(l_cyl)  if l_cyl  else None,
        "l_axis": int(l_axis)   if l_axis else None,
        "l_add":  float(l_add)  if l_add  else None,
    }


def _frame_filters(dyn:dict) -> dict:
    mk = lambda k, d: ["All"] + (d.get(k) or [])
    c1,c2,c3 = st.columns(3)
    brand    = c1.selectbox("Brand",    mk("brands",dyn),    key="fr_brand",   on_change=lambda: st.session_state.pop("load_stock",None))
    colour   = c2.selectbox("Colour",   mk("colours",dyn),   key="fr_colour",  on_change=lambda: st.session_state.pop("load_stock",None))
    shape    = c3.selectbox("Shape",    mk("shapes",dyn),    key="fr_shape",   on_change=lambda: st.session_state.pop("load_stock",None))
    c4,c5    = st.columns(2)
    material = c4.selectbox("Material", mk("materials",dyn), key="fr_material",on_change=lambda: st.session_state.pop("load_stock",None))
    finish   = c5.selectbox("Finish",   mk("finishes",dyn),  key="fr_finish",  on_change=lambda: st.session_state.pop("load_stock",None))
    return {k: v if v != "All" else "" for k,v in
            {"brand":brand,"colour":colour,"shape":shape,"material":material,"finish":finish}.items()}


def _general_filters(dyn:dict) -> dict:
    c1,c2 = st.columns(2)
    brand   = c1.selectbox("Brand",   ["All"]+(dyn.get("brands") or []),   key="gen_brand",  on_change=lambda: st.session_state.pop("load_stock",None))
    product = c2.selectbox("Product", ["All"]+(dyn.get("products") or []), key="gen_product",on_change=lambda: st.session_state.pop("load_stock",None))
    return {"brand":   brand   if brand   != "All" else "",
            "product": product if product != "All" else ""}


# ── Result renderers ─────────────────────────────────────────

def _render_cl_results(rows: list, party: dict):
    """Show R eye and L eye results in separate sections."""
    r_rows = [r for r in rows if r.get("matched_eye") == "R"]
    l_rows = [r for r in rows if r.get("matched_eye") == "L"]
    both   = [r for r in rows if r.get("matched_eye") not in ("R","L")]

    if r_rows:
        st.markdown("#### 👁️ Right Eye (OD)")
        _cl_section(r_rows, party)
    if l_rows:
        st.markdown("#### 👁️ Left Eye (OS)")
        _cl_section(l_rows, party)
    if both:
        st.markdown("#### 👁️ Results")
        _cl_section(both, party)


def _cl_section(rows: list, party: dict):
    from collections import defaultdict
    by_prod: dict = defaultdict(list)
    for r in rows: by_prod[r["product_name"]].append(r)

    for prod_name, skus in by_prod.items():
        sample = skus[0]
        total  = sum(int(s.get("stock_qty",0)) for s in skus)
        trade  = float(sample.get("trade_price",0))

        with st.expander(
            f"**{prod_name}** — {sample.get('brand','')} | ₹{trade:,.0f} | "
            f"{'✅ '+str(total)+' boxes' if total>0 else '⚠️ Out'}",
            expanded=(total > 0)
        ):
            # Product specs
            specs=[]
            if sample.get("water_content"): specs.append(f"💧{sample['water_content']}%")
            if sample.get("base_curve"):    specs.append(f"BC {sample['base_curve']}")
            if sample.get("diameter"):      specs.append(f"Ø{sample['diameter']}mm")
            if sample.get("replacement_schedule"): specs.append(sample["replacement_schedule"])
            if sample.get("uv_blocking"):   specs.append("UV✓")
            if sample.get("coating"):       specs.append(sample["coating"])
            if specs: st.caption("  |  ".join(specs))

            m1,m2,m3 = st.columns(3)
            m1.metric("Trade",f"₹{trade:,.0f}")
            if sample.get("mrp"): m2.metric("MRP",f"₹{float(sample['mrp']):,.0f}")
            if sample.get("gst_percent"): m3.metric("GST",f"{float(sample['gst_percent']):.0f}%")
            st.markdown("---")

            for sku in skus:
                _render_stock_row(sku, party, prod_name)


def _render_cl_row_compact(sku:dict, party:dict, prod_name:str=""):
    """Compact row for CL (inside expander)."""
    _render_stock_row(sku, party, prod_name)


def _render_universal_grid(rows: list, party: dict):
    """3-col grid for frames, solutions, general."""
    for i in range(0, len(rows), 3):
        cols = st.columns(3)
        for col, row in zip(cols, rows[i:i+3]):
            with col:
                _render_stock_card(row, party)


def _render_stock_card(row: dict, party: dict):
    """Universal card for non-CL products."""
    ckey  = str(row.get("stock_id") or row.get("product_id",""))
    stock = int(row.get("stock_qty",0))
    trade = float(row.get("trade_price",0))
    mrp   = float(row.get("mrp",0))
    cart  = cart_get()

    with st.container(border=True):
        if row.get("image_url"): st.image(row["image_url"],use_container_width=True)
        st.markdown(f"**{row['product_name']}**")
        st.caption(f"{row.get('brand','')}  |  {row.get('sku_code','')}")
        detail = build_item_detail(row)
        if detail: st.caption(detail)
        p1,p2 = st.columns(2)
        p1.metric("Trade",f"₹{trade:,.0f}")
        if mrp and mrp != trade: p2.metric("MRP",f"₹{mrp:,.0f}")
        if stock <= 0:
            st.error("Out of stock")
            st.button("—",key=f"sc_{ckey}",disabled=True,use_container_width=True)
        elif stock <= 3:
            st.warning(f"⚠️ Only {stock} left")
            _add_btn(row, ckey, stock, cart)
        else:
            st.caption(f"✅ {stock} available")
            _add_btn(row, ckey, stock, cart)


def _render_stock_row(sku: dict, party: dict, prod_name: str = ""):
    """Compact row for CL power entries."""
    ckey  = str(sku.get("stock_id") or sku.get("product_id",""))
    stock = int(sku.get("stock_qty",0))
    trade = float(sku.get("trade_price",0))
    cart  = cart_get()

    detail = build_item_detail(sku)
    if stock == 0: stock_lbl = "❌ Out"
    elif stock <= 3: stock_lbl = f"⚠️ {stock}"
    else: stock_lbl = f"✅ {stock}"

    c1,c2,c3,c4 = st.columns([4,2,1,2])
    with c1:
        st.write(f"**{detail or sku.get('sku_code','')}**")
        st.caption(f"₹{trade:,.0f}/box" + (f"  Exp:{str(sku.get('expiry_date',''))[:7]}" if sku.get('expiry_date') else ""))
    with c2: st.caption(stock_lbl)
    with c3:
        qty = st.number_input("",min_value=1,max_value=max(stock,1),value=1,
                              key=f"sq_{ckey}",label_visibility="collapsed",disabled=(stock==0))
    with c4:
        if stock == 0:
            st.button("Out",key=f"sa_{ckey}",disabled=True,use_container_width=True)
        elif ckey in cart:
            if st.button("✅ Cart",key=f"sa_{ckey}",use_container_width=True,type="secondary"):
                cart_update_qty(ckey,qty); st.rerun()
        else:
            if st.button("🛒 Add",key=f"sa_{ckey}",use_container_width=True,type="primary"):
                name = f"{prod_name or sku.get('product_name','')} ({detail})" if detail else sku.get("product_name","")
                cart_add({**sku,"product_name":name},qty)
                st.toast("Added ✅"); st.rerun()


def _add_btn(row: dict, ckey: str, stock: int, cart: dict):
    c1,c2 = st.columns([1,2])
    qty = c1.number_input("Qty",min_value=1,max_value=stock,value=1,
                           key=f"cq_{ckey}",label_visibility="collapsed")
    with c2:
        if ckey in cart:
            if st.button("✅ In Cart",key=f"ca_{ckey}",use_container_width=True,type="secondary"):
                cart_update_qty(ckey,qty); st.rerun()
        else:
            if st.button("🛒 Add",key=f"ca_{ckey}",use_container_width=True,type="primary"):
                cart_add(row,qty); st.toast("Added ✅"); st.rerun()


def _suggest_alternatives(category: str, filters: dict):
    """Show nearby powers for CL, or relaxed filter tips for others."""
    cat_type = get_category_type(category)
    if cat_type != "CONTACT_LENS": 
        st.caption("💡 Try selecting 'All' for Brand or other filters.")
        return
    r_sph = filters.get("r_sph") or filters.get("l_sph")
    if r_sph is None: return
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT DISTINCT p.product_name,p.brand,s.sph,SUM(s.quantity) AS qty
            FROM inventory_stock s JOIN products p ON p.id=s.product_id
            WHERE LOWER(COALESCE(p.main_group,'')) LIKE '%contact%'
              AND ROUND(s.sph::numeric,2) IN (%s,%s,%s)
              AND COALESCE(s.quantity,0)>0
            GROUP BY p.product_name,p.brand,s.sph ORDER BY ABS(s.sph-%s) LIMIT 8
        """, (r_sph-0.25,r_sph,r_sph+0.25,r_sph)) or []
        if rows:
            st.caption("💡 Nearby powers available:")
            for r in rows:
                st.caption(f"• {r['product_name']} | SPH {float(r['sph']):+.2f} | {int(r['qty'])} boxes")
    except Exception: pass


# ══════════════════════════════════════════════════════════════
# SCAN TAB
# ══════════════════════════════════════════════════════════════

def _scan_tab(party:dict, home:dict):
    st.markdown("### ⚡ Scan / Type SKU")
    st.caption("Type or scan SKU code — finds any product type instantly")
    scan = st.text_input("📷 SKU / Barcode", key="scan_input",
                         placeholder="Type batch no and press Enter...")
    cart = cart_get()

    if scan and scan.strip():
        result = lookup_by_sku(scan.strip())
        if result:
            ckey  = str(result.get("stock_id") or result.get("product_id"))
            stock = int(result.get("stock_qty",0))
            trade = float(result.get("trade_price",0))
            detail = build_item_detail(result)

            with st.container(border=True):
                st.markdown(f"**{result['product_name']}**")
                st.caption(f"{result.get('brand','')}  |  {result.get('sku_code','')}")
                if detail: st.caption(detail)
                m1,m2,m3 = st.columns(3)
                m1.metric("Trade",f"₹{trade:,.0f}")
                if result.get("mrp"): m2.metric("MRP",f"₹{float(result['mrp']):,.0f}")
                m3.metric("Stock",str(stock) if stock>0 else "❌ Out")

                if stock > 0:
                    qc,bc,fc = st.columns([1,2,1])
                    qty = qc.number_input("Qty",min_value=1,max_value=stock,value=1,
                                          key="scan_qty",label_visibility="collapsed")
                    with bc:
                        if ckey in cart:
                            if st.button("Update Cart",key="scan_upd",use_container_width=True):
                                cart_update_qty(ckey,qty); st.rerun()
                        else:
                            if st.button("🛒 Add to Cart",key="scan_add",type="primary",use_container_width=True):
                                cart_add(result,qty); st.toast("Added ✅"); st.rerun()
                    with fc:
                        if st.button("⭐",key="scan_fav",help="Favourite"):
                            added = toggle_favourite(party["id"],str(result.get("stock_id","")),str(result.get("product_id","")))
                            st.toast("Saved ⭐" if added else "Removed")
                else:
                    st.error("Out of stock")
        else:
            st.warning(f"⚠️ SKU '{scan.strip()}' not found in inventory")

    # Mini cart summary in scan tab
    if cart:
        st.markdown("---")
        st.caption(f"**Cart: {sum(i['qty'] for i in cart.values())} items | "
                   f"₹{sum(float(i.get('trade_price',0))*i['qty'] for i in cart.values()):,.0f}**")


# ══════════════════════════════════════════════════════════════
# REORDER TAB
# ══════════════════════════════════════════════════════════════

def _reorder_tab(party:dict):
    st.markdown("### 🔁 Quick Reorder")
    tab_h, tab_f = st.tabs(["📦 Previously Ordered","⭐ Favourites"])

    with tab_h:
        items = get_reorder_items(party["id"])
        if not items:
            st.info("No previous orders yet."); return
        cart = cart_get()
        for item in items:
            old_sid = str(item.get("stock_id",""))
            # Validate stock still exists
            fresh = validate_reorder_item(old_sid) if old_sid else None
            ckey  = str(fresh.get("stock_id","") if fresh else old_sid)
            stock = int(fresh.get("stock_qty",0) if fresh else 0)
            trade = float(fresh.get("trade_price",0) if fresh else item.get("trade_price",0))
            detail = build_item_detail(item)

            c1,c2,c3 = st.columns([4,1,2])
            with c1:
                st.write(f"**{item['product_name']}**")
                if detail: st.caption(detail)
                st.caption(f"₹{trade:,.0f}  |  Ordered {item.get('times_ordered',1)}×" +
                           (f"  |  {'✅ '+str(stock)+' in stock' if stock>0 else '❌ Out of stock'}" ))
            with c2:
                qty = st.number_input("",min_value=1,max_value=max(stock,1),
                                      value=int(item.get("last_qty",1)),
                                      key=f"ro_{ckey}",label_visibility="collapsed",
                                      disabled=(stock==0))
            with c3:
                if stock == 0:
                    st.button("Out",key=f"rob_{ckey}",disabled=True,use_container_width=True)
                elif ckey in cart:
                    if st.button("✅ Cart",key=f"rob_{ckey}",use_container_width=True,type="secondary"):
                        cart_update_qty(ckey,qty); st.rerun()
                else:
                    if st.button("🔁 Add",key=f"rob_{ckey}",use_container_width=True,type="primary"):
                        base = fresh if fresh else item
                        cart_add({**base,"trade_price":trade},qty)
                        st.toast("Added ✅"); st.rerun()
            st.markdown("---")

    with tab_f:
        favs = get_favourites(party["id"])
        if not favs:
            st.info("No favourites. Tap ⭐ on Scan tab to save SKUs."); return
        cart = cart_get()
        for item in favs:
            ckey  = str(item.get("stock_id",""))
            stock = int(item.get("stock_qty",0))
            trade = float(item.get("trade_price",0))
            detail = build_item_detail(item)

            c1,c2,c3 = st.columns([4,1,2])
            with c1:
                st.write(f"**{item['product_name']}**")
                if detail: st.caption(detail)
                st.caption(f"₹{trade:,.0f}  |  {'✅ '+str(stock) if stock>0 else '❌ Out'}")
            with c2:
                qty=st.number_input("",min_value=1,max_value=max(stock,1),value=1,
                                    key=f"fv_{ckey}",label_visibility="collapsed",disabled=(stock==0))
            with c3:
                if stock==0:
                    st.button("Out",key=f"fvb_{ckey}",disabled=True,use_container_width=True)
                elif ckey in cart:
                    if st.button("✅ Cart",key=f"fvb_{ckey}",use_container_width=True,type="secondary"):
                        cart_update_qty(ckey,qty); st.rerun()
                else:
                    if st.button("🛒 Add",key=f"fvb_{ckey}",use_container_width=True,type="primary"):
                        cart_add(item,qty); st.toast("Added ✅"); st.rerun()
            st.markdown("---")


# ══════════════════════════════════════════════════════════════
# CART TAB
# ══════════════════════════════════════════════════════════════

def _cart_tab(party:dict, home:dict):
    cart = cart_get()
    st.subheader("🛒 Your Cart")
    if not cart:
        st.info("Cart is empty."); return

    # Detailed cart
    rows=[]
    for key,item in cart.items():
        rows.append({
            "Product":    item["product_name"],
            "Detail":     build_item_detail(item),
            "SKU":        item.get("sku_code",""),
            "Stock Left": int(item.get("stock_qty",0)),
            "Qty":        item["qty"],
            "Unit ₹":     f"₹{float(item.get('trade_price',0)):,.0f}",
            "Total ₹":    f"₹{float(item.get('trade_price',0))*item['qty']:,.0f}",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    with st.expander("✏️ Edit / Remove"):
        for key,item in list(cart.items()):
            ec1,ec2,ec3 = st.columns([3,1,1])
            with ec1: st.write(item["product_name"])
            with ec2:
                nq = st.number_input("",min_value=0,value=item["qty"],
                                     key=f"eq_{key}",label_visibility="collapsed")
                if nq != item["qty"]: cart_update_qty(key,nq); st.rerun()
            with ec3:
                if st.button("🗑️",key=f"rm_{key}"): cart_remove(key); st.rerun()

    # ── Service Add Section ─────────────────────────────────────────────────
    with st.expander("🔧 Add Service (Fitting / Colouring / Courier)", expanded=False):
        try:
            from modules.backoffice.service_master import (
                fetch_service_types as _fst_r,
                service_price as _sp_r,
            )
            _r_svc_rows = _fst_r(active_only=True)
        except Exception:
            _r_svc_rows = []
            _sp_r = lambda s, ot: 0.0

        _r_by_group = {}
        for _rr in _r_svc_rows:
            _r_by_group.setdefault(str(_rr.get("service_group","OTHER")).upper(), []).append(_rr)

        _r_order_type = str(party.get("party_type") or "WHOLESALE").upper()

        for _r_svc_t, _r_icon, _r_lbl, _r_gst_d in [
            ("COLOURING", "🎨", "Colouring/Tint", 18),
            ("FITTING",   "🔧", "Fitting",         18),
            ("COURIER",   "🚚", "Courier",          5),
        ]:
            _r_grow = _r_by_group.get(_r_svc_t, [])
            _r_opts = ["None"] + [r.get("service_name","") for r in _r_grow]
            _r_sel  = st.selectbox(
                f"{_r_icon} {_r_lbl}",
                _r_opts,
                key=f"r_svc_sel_{_r_svc_t}",
            )
            if _r_sel and _r_sel != "None":
                _r_def  = next((r for r in _r_grow if r.get("service_name")==_r_sel), {})
                _r_damt = float(_sp_r(_r_def, _r_order_type) if _r_def else 0)

                # Mandatory qty confirmation
                _r_qk  = f"r_svc_q_{_r_svc_t}"
                _r_qok = st.session_state.get(_r_qk + "_ok", False)
                # Auto-detect pair from current cart (non-service items)
                _r_cart_eyes = {str(v.get("eye_side","")).upper()[:1]
                                for v in cart.values()
                                if not v.get("is_service")}
                _r_auto_qty = 1.0 if ("R" in _r_cart_eyes and "L" in _r_cart_eyes) else (
                              0.5 if _r_cart_eyes else 1.0)
                _r_auto_idx = [0.5, 1.0, 1.5, 2.0].index(_r_auto_qty)                               if _r_auto_qty in [0.5, 1.0, 1.5, 2.0] else 1
                _r_qty = st.selectbox(
                    f"Qty for {_r_lbl} ⚠️",
                    [0.5, 1.0, 1.5, 2.0],
                    index=_r_auto_idx,
                    format_func=lambda v: (
                        f"{v:g} pair — 1 eye only" if v == 0.5 else
                        f"{v:g} pair — both eyes" if v == 1.0 else
                        f"{v:g} pair"
                    ),
                    key=_r_qk,
                    help="Auto-set from cart. Confirm to proceed.",
                )
                if not _r_qok:
                    if st.button(f"✅ Confirm {_r_qty:g} pair",
                                 key=f"r_svc_qc_{_r_svc_t}", use_container_width=True):
                        st.session_state[_r_qk + "_ok"] = True
                        st.rerun()
                    st.caption("⚠️ Confirm qty to proceed")
                else:
                    st.info(f"✅ {_r_qty:g} pair confirmed")
                    if st.button("↩️ Change qty", key=f"r_svc_qr_{_r_svc_t}"):
                        st.session_state[_r_qk + "_ok"] = False
                        st.rerun()
                    _ra1, _ra2 = st.columns(2)
                    _r_amt = _ra1.number_input(
                        f"₹ Amount ({_r_lbl})", min_value=0.0,
                        value=round(_r_damt * _r_qty, 2), step=10.0,
                        key=f"r_svc_amt_{_r_svc_t}_{_r_sel}")
                    _r_gst = _ra2.number_input(
                        "GST%", min_value=0.0, max_value=28.0,
                        value=float(_r_gst_d), step=0.5,
                        key=f"r_svc_gst_{_r_svc_t}")
                    _r_instr = st.text_input(
                        "Instruction (optional)",
                        key=f"r_svc_ins_{_r_svc_t}",
                        placeholder="Colour shade, fitting note...")
                    if st.button(
                        f"➕ Add {_r_lbl} — {_r_qty:g} pair to Cart",
                        key=f"r_svc_add_{_r_svc_t}",
                        use_container_width=True, type="primary"
                    ):
                        if _r_amt > 0:
                            import uuid as _r_uuid, json as _r_json
                            _r_direct = (_r_svc_t == "COURIER")
                            _r_ga  = round(_r_amt * _r_gst / 100, 2)
                            _r_tot = round(_r_amt + _r_ga, 2)
                            _svc_cart_key = f"svc_{_r_svc_t}_{_r_sel}"
                            cart[_svc_cart_key] = {
                                "product_name": f"{_r_lbl}: {_r_sel}",
                                "is_service": True,
                                "service_type": _r_svc_t,
                                "service_description": _r_sel,
                                "service_production_type": "" if _r_direct else _r_svc_t,
                                "manufacturing_route": "SERVICE",
                                "service_instruction": _r_instr,
                                "service_qty_factor": float(_r_qty),
                                "trade_price": _r_amt,
                                "gst_percent": _r_gst,
                                "gst_amount": _r_ga,
                                "total": _r_tot,
                                "qty": float(_r_qty),
                                "cart_key": _svc_cart_key,
                                "is_service_line": True,
                            }
                            st.session_state["retailer_cart"] = cart
                            st.session_state[_r_qk + "_ok"] = False
                            st.success(f"✅ {_r_lbl}: {_r_sel} — {_r_qty:g} pair added ₹{_r_tot:.0f}")
                            st.rerun()
                        else:
                            st.warning("Enter amount > 0")

    # Payment
    st.divider()
    pay_methods = get_payment_methods(home)
    cur_pm = st.session_state.get("checkout_payment","")
    pm_cols = st.columns(len(pay_methods))
    for col,pm in zip(pm_cols,pay_methods):
        with col:
            with st.container(border=True):
                st.markdown(f"**{pm['label']}**")
                st.markdown(f"### {pm['extra']}")
                if st.button("Select",key=f"pm_{pm['method']}",use_container_width=True,
                             type="primary" if cur_pm==pm["method"] else "secondary"):
                    st.session_state["checkout_payment"]=pm["method"]; st.rerun()

    pm = st.session_state.get("checkout_payment","")
    if not pm:
        st.info("Select payment method above.")
        st.button("🗑️ Clear Cart",key="cc1",on_click=lambda: (cart_clear(), st.rerun()))
        return

    totals = cart_totals(cart, pm, home)
    st.divider()
    tc1,tc2 = st.columns([2,1])
    with tc2:
        st.metric("Subtotal",  f"₹ {totals['subtotal']:,.2f}")
        if totals["disc_pct"]:
            st.metric(f"Discount {totals['disc_pct']:.0f}%",f"− ₹{totals['disc_amount']:,.2f}")
        st.metric("Net Payable",f"₹ {totals['net']:,.2f}")
        if totals["scheme_name"]: st.caption(f"🏷️ {totals['scheme_name']}")

    min_amt = home.get("min_order_amt",0)
    if min_amt and totals["subtotal"] < min_amt:
        st.warning(f"⚠️ Minimum order: ₹{min_amt:,.0f}")

    notes = st.text_area("Notes",key="order_notes",placeholder="Special instructions...")
    c1,c2 = st.columns(2)
    with c1:
        if pm == "CREDIT":
            avail = home.get("avail_credit",0)
            if home.get("credit_limit",0)>0 and totals["net"]>avail:
                st.error(f"❌ Exceeds credit ₹{avail:,.0f}"); return
            conf = st.checkbox("I confirm this order",key="cr_confirm")
            if st.button("✅ Place Credit Order",type="primary",
                         disabled=not conf,use_container_width=True):
                _place(party,cart,pm,home,notes)
        else:
            st.success(f"💰 Save ₹{totals['disc_amount']:,.2f} by paying now!")
            if st.button("⚡ Pay & Confirm",type="primary",use_container_width=True):
                _place(party,cart,pm,home,notes)
    with c2:
        st.button("🗑️ Clear Cart",key="cc2",use_container_width=True,
                  on_click=lambda: (cart_clear(), st.session_state.pop("checkout_payment",None)))


def _place(party,cart,pm,home,notes):
    result = create_order(party,cart,pm,home,notes)
    if result["success"]:
        st.success(f"✅ Order **{result['order_no']}** placed!")
        if pm == "CASH":
            rz = create_razorpay_order(result["net_amount"],result["order_no"])
            if rz.get("dev_mode"):
                update_payment_status(result["order_id"],rz["razorpay_order_id"],"dev_sim")
                st.success("[DEV] Payment simulated")
        st.balloons()
        cart_clear()
        st.session_state.pop("checkout_payment",None)
        st.session_state.pop("_ph",None)   # refresh credit balance
        st.rerun()
    else:
        st.error(result["message"])


# ══════════════════════════════════════════════════════════════
# ORDERS TAB
# ══════════════════════════════════════════════════════════════

def _orders_tab(party:dict):
    st.subheader("📋 My Orders")
    orders = get_my_orders(party["id"])
    if not orders:
        st.info("No orders yet."); return
    SIC = {"DRAFT":"⚪","SUBMITTED":"🔵","CONFIRMED":"🟢",
           "DISPATCHED":"🟡","DELIVERED":"✅","CANCELLED":"🔴"}
    for order in orders:
        icon = SIC.get(order.get("status",""),"⚪")
        paid = "✅" if order.get("payment_status")=="PAID" else "⏳"
        with st.expander(
            f"{icon} **{order['order_no']}** | "
            f"₹{float(order.get('net_amount',0)):,.0f} | "
            f"{order.get('status','')} | {paid} | {order.get('punched_display','')}",
            expanded=False
        ):
            c1,c2,c3 = st.columns(3)
            c1.metric("Status",  f"{icon} {order.get('status','')}")
            c2.metric("Payment", f"{order.get('payment_method','')} {paid}")
            c3.metric("Net",     f"₹{float(order.get('net_amount',0)):,.2f}")
            if order.get("scheme_applied"): st.caption(f"🏷️ {order['scheme_applied']}")
            lines = get_order_lines(order["order_no"],party["id"])
            if lines:
                df = pd.DataFrame(lines)
                if "sph" in df.columns:
                    def _pw(r):
                        p=[]
                        if r.get("sph"): p.append(f"SPH {float(r['sph']):+.2f}")
                        if r.get("cyl") and float(r.get("cyl",0))!=0: p.append(f"CYL {float(r['cyl']):+.2f}")
                        if r.get("eye_side"): p.append(r["eye_side"])
                        if r.get("colour"): p.append(r["colour"])
                        return " ".join(p)
                    df["Detail"] = df.apply(_pw,axis=1)
                st.dataframe(df,use_container_width=True,hide_index=True)

            if st.button("🔁 Reorder",key=f"reorder_{order['order_no']}"):
                for l in (lines or []):
                    fake = {"product_name":l.get("product_name",""),
                            "brand":l.get("brand",""),"sku_code":l.get("sku_code",""),
                            "stock_id":str(l.get("stock_id","")),"trade_price":float(l.get("unit_price",0)),
                            "sph":l.get("sph"),"cyl":l.get("cyl"),"axis":l.get("axis"),
                            "add_power":l.get("add_power"),"eye_side":l.get("eye_side",""),
                            "colour":l.get("colour",""),"shape":l.get("shape","")}
                    cart_add(fake,int(l.get("qty",1)))
                st.success("Items added to cart!"); st.rerun()


# ══════════════════════════════════════════════════════════════
# CSS — mobile first
# ══════════════════════════════════════════════════════════════

def _css():
    st.markdown("""<style>
    .stButton>button{border-radius:10px;font-weight:600;min-height:44px}
    [data-testid="metric-container"]{background:#f8fafc;padding:10px;border-radius:8px}
    .stExpander{border-radius:10px}
    @media(max-width:640px){
        .stColumns{flex-direction:column!important}
        .stButton>button{min-height:52px;font-size:1rem}
        [data-testid="metric-container"]{padding:6px}
    }
    </style>""", unsafe_allow_html=True)
