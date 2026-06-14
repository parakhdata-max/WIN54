"""
modules/online_store/store_app.py
===================================
OpticalWala.com — Main Streamlit app.
Responsive online optical store. Public-facing.

Run: streamlit run store_app.py --server.port 8502
"""
import streamlit as st

from modules.online_store.store_auth import (
    send_otp, verify_otp, get_or_create_customer,
    create_session, validate_token, logout,
    get_addresses, save_address,
)
from modules.online_store.store_catalog import (
    get_online_categories, get_featured_products,
    get_products, get_product_detail,
    get_brands, get_price_range,
)
from modules.online_store.store_cart import (
    cart_get, cart_add, cart_update_qty, cart_remove, cart_clear,
    cart_totals, validate_promo,
)
from modules.online_store.store_orders import (
    place_order, get_orders, get_order_lines, update_payment,
)


# ══════════════════════════════════════════════════════════════
# MAIN ENTRY
# ══════════════════════════════════════════════════════════════

def render_online_store():
    try:
        st.set_page_config(
            page_title="OpticalWala", page_icon="👓",
            layout="wide", initial_sidebar_state="collapsed",
        )
    except Exception:
        pass
    _css()

    token    = st.session_state.get("ow_token")
    customer = validate_token(token) if token else None

    _navbar(customer)

    page = st.session_state.get("ow_page", "home")

    if page == "home":        _page_home(customer)
    elif page == "shop":      _page_shop(customer)
    elif page == "product":   _page_product(customer)
    elif page == "cart":      _page_cart(customer)
    elif page == "checkout":  _page_checkout(customer)
    elif page == "orders":    _page_orders(customer)
    elif page == "account":   _page_account(customer)
    elif page == "appointment": _page_appointment(customer)
    elif page == "login":     _page_login()


# ══════════════════════════════════════════════════════════════
# NAVBAR
# ══════════════════════════════════════════════════════════════

def _navbar(customer):
    cart  = cart_get()
    n_cart = sum(int(v.get("qty",1)) for v in cart.values())
    cart_badge = f" ({n_cart})" if n_cart else ""

    c0, c1, c2, c3, c4, c5, c6, c7 = st.columns([2, 1.3, 1.3, 1.7, 1.3, 1.3, 1, 1])
    with c0:
        st.markdown("### 👓 OpticalWala")
    with c1:
        if st.button("🏠 Home"):
            st.session_state["ow_page"] = "home"; st.rerun()
    with c2:
        if st.button("🛍️ Shop"):
            st.session_state["ow_page"] = "shop"; st.rerun()
    with c3:
        if st.button("🧪 Book Eye Test"):
            st.session_state["ow_page"] = "appointment"; st.rerun()
    with c4:
        if st.button(f"🛒 Cart{cart_badge}"):
            st.session_state["ow_page"] = "cart"; st.rerun()
    with c5:
        if customer:
            if st.button("📋 Orders"):
                st.session_state["ow_page"] = "orders"; st.rerun()
        else:
            if st.button("📋 Orders"):
                st.session_state["ow_page"] = "login"; st.rerun()
    with c6:
        if customer:
            if st.button("👤 Account"):
                st.session_state["ow_page"] = "account"; st.rerun()
    with c7:
        if customer:
            if st.button("🚪 Logout"):
                logout(token); st.session_state.clear(); st.rerun()
        else:
            if st.button("🔐 Login"):
                st.session_state["ow_page"] = "login"; st.rerun()

    st.markdown("<hr style='margin:0 0 12px 0;border:1px solid #1e293b'>",
                unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
# PAGES
# ══════════════════════════════════════════════════════════════

def _page_home(customer):
    # Hero
    st.markdown("""
    <div style='background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);
         padding:40px 30px;border-radius:12px;margin-bottom:24px;text-align:center'>
      <h1 style='color:#f8fafc;font-size:2.4rem;margin:0'>👓 OpticalWala</h1>
      <p style='color:#93c5fd;font-size:1.1rem;margin:8px 0 20px'>
        Premium Eyewear & Contact Lenses — Delivered to Your Door</p>
    </div>
    """, unsafe_allow_html=True)

    # Category cards
    st.subheader("Shop by Category")
    cats = get_online_categories()
    cat_icons = {"Contact Lens":"👁","Frame":"🕶️","Sunglass":"😎",
                 "Solution":"💧","Accessories":"🔧"}
    if not cats:
        st.info("No products are live yet. Ask your admin to mark products as Online Active.")
    else:
        cols = st.columns(min(len(cats), 5))
        for i, cat in enumerate(cats[:5]):
            name = cat.get("name","")
            icon = cat_icons.get(name, "📦")
            with cols[i]:
                if st.button(f"{icon}\n**{name}**\n{cat.get('product_count',0)} products",
                              use_container_width=True, key=f"cat_{i}"):
                    st.session_state.update(ow_page="shop", ow_filter_cat=name)
                    st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    # Featured products
    st.subheader("⭐ Featured Products")
    featured = get_featured_products(limit=8)
    _product_grid(featured.get("items",[]), cols=4)


def _page_shop(customer):
    st.subheader("🛍️ Shop")
    cats   = ["All"] + [c["name"] for c in get_online_categories()]
    brands = ["All"] + get_brands()

    # Filters sidebar
    with st.sidebar:
        st.markdown("### 🔍 Filters")
        search   = st.text_input("Search", value=st.session_state.get("ow_search",""))
        cat      = st.selectbox("Category", cats,
                                 index=cats.index(st.session_state.get("ow_filter_cat","All"))
                                 if st.session_state.get("ow_filter_cat","All") in cats else 0)
        brand    = st.selectbox("Brand", brands)
        pr       = get_price_range(cat if cat!="All" else None)
        price_r  = st.slider("Price Range ₹", pr["min"], max(pr["max"],1),
                              (pr["min"], pr["max"]))
        sort_opt = st.selectbox("Sort By",
                                 ["popular","price_asc","price_desc","newest"],
                                 format_func=lambda x: {
                                     "popular":"Popularity","price_asc":"Price: Low→High",
                                     "price_desc":"Price: High→Low","newest":"Newest"
                                 }[x])

    # Fetch
    results = get_products(
        category=cat if cat!="All" else None,
        brand=brand if brand!="All" else None,
        search=search or None,
        min_price=price_r[0], max_price=price_r[1],
        sort=sort_opt,
        offset=st.session_state.get("shop_offset",0),
        limit=20,
    )
    items = results.get("items",[])
    st.caption(f"Showing {len(items)} products")
    _product_grid(items, cols=4)

    if results.get("has_more"):
        if st.button("Load More"):
            st.session_state["shop_offset"] = results.get("offset",0)
            st.rerun()


def _page_product(customer):
    pid = st.session_state.get("ow_product_id")
    if not pid:
        st.session_state["ow_page"] = "shop"; st.rerun()

    prod = get_product_detail(pid)
    if not prod:
        st.error("Product not found"); return

    st.button("← Back to Shop", on_click=lambda: st.session_state.update(ow_page="shop"))
    st.markdown(f"## {prod['product_name']}")
    st.caption(f"{prod.get('brand','')} · {prod.get('main_group','')}")

    col_img, col_info = st.columns([2, 3])

    with col_img:
        images = prod.get("images", [])
        if images:
            primary = next((i for i in images if i.get("is_primary")), images[0])
            img_src = primary.get("image_url") or primary.get("image_b64")
            if img_src:
                st.image(img_src)
            else:
                st.markdown("<div style='height:300px;background:#1e293b;border-radius:8px;"
                            "display:flex;align-items:center;justify-content:center;"
                            "font-size:4rem'>👓</div>", unsafe_allow_html=True)
        else:
            st.markdown("<div style='height:300px;background:#1e293b;border-radius:8px;"
                        "display:flex;align-items:center;justify-content:center;"
                        "font-size:4rem'>👓</div>", unsafe_allow_html=True)

    with col_info:
        price = float(prod.get("online_price") or 0)
        mrp   = float(prod.get("mrp") or 0)
        if mrp > price > 0:
            saving_pct = round((mrp - price) / mrp * 100)
            st.markdown(
                f"<div style='font-size:2rem;font-weight:800;color:#10b981'>₹{price:,.0f}</div>"
                f"<div style='color:#64748b'><s>₹{mrp:,.0f}</s> · You save {saving_pct}%</div>",
                unsafe_allow_html=True
            )
        else:
            st.markdown(f"<div style='font-size:2rem;font-weight:800'>₹{price:,.0f}</div>",
                        unsafe_allow_html=True)

        badge = prod.get("online_badge","")
        if badge:
            colors = {"BESTSELLER":"#f59e0b","NEW":"#10b981","SALE":"#ef4444","FEATURED":"#8b5cf6"}
            c = colors.get(badge,"#64748b")
            st.markdown(f"<span style='background:{c};color:white;padding:2px 10px;"
                        f"border-radius:20px;font-size:0.75rem;font-weight:700'>{badge}</span>",
                        unsafe_allow_html=True)

        # Power selection for contact lenses
        powers = prod.get("powers", [])
        selected_eye = "B"
        selected_power = {}
        if powers:
            st.markdown("**Select Power:**")
            has_eye = any(p.get("eye_side") in ("R","L") for p in powers)
            if has_eye:
                eye = st.radio("Eye", ["Both","Right (R)","Left (L)"],
                               horizontal=True, key="ow_eye")
                selected_eye = "B" if eye=="Both" else eye[eye.index("(")+1]

            sph_opts = sorted(set(float(p["sph"]) for p in powers if p.get("sph") is not None))
            sph = st.selectbox("SPH", sph_opts,
                               format_func=lambda v: f"{v:+.2f}", key="ow_sph")
            selected_power["sph"] = sph

            cyl = None
            cyl_opts = sorted(set(float(p["cyl"]) for p in powers if p.get("cyl") is not None))
            if cyl_opts:
                cyl = st.selectbox("CYL", cyl_opts,
                                   format_func=lambda v: f"{v:+.2f}", key="ow_cyl")
                selected_power["cyl"] = cyl

            axis = None
            axis_opts = sorted(set(int(p["axis"]) for p in powers
                                   if p.get("axis") is not None
                                   and (p.get("sph") is None or float(p.get("sph")) == float(sph))
                                   and (cyl is None or p.get("cyl") is None or float(p.get("cyl")) == float(cyl))))
            if axis_opts:
                axis = st.selectbox("AXIS", axis_opts, key="ow_axis")
                selected_power["axis"] = axis

            add_power = None
            add_opts = sorted(set(float(p["add_power"]) for p in powers
                                  if p.get("add_power") is not None
                                  and (p.get("sph") is None or float(p.get("sph")) == float(sph))
                                  and (cyl is None or p.get("cyl") is None or float(p.get("cyl")) == float(cyl))))
            if add_opts:
                add_power = st.selectbox("ADD", add_opts,
                                         format_func=lambda v: f"{v:+.2f}", key="ow_add")
                selected_power["add_power"] = add_power

            # Carry exact inventory_stock id into cart/order so ERP mirror can reserve stock.
            matching_power = next((p for p in powers
                if (p.get("sph") is None or float(p.get("sph")) == float(sph))
                and (cyl is None or p.get("cyl") is None or float(p.get("cyl")) == float(cyl))
                and (axis is None or p.get("axis") is None or int(p.get("axis")) == int(axis))
                and (add_power is None or p.get("add_power") is None or float(p.get("add_power")) == float(add_power))
                and (selected_eye == "B" or not p.get("eye_side") or p.get("eye_side") == selected_eye)
            ), None)
            if matching_power and matching_power.get("stock_id"):
                selected_power["stock_id"] = matching_power.get("stock_id")

        qty = st.number_input("Qty", min_value=1, value=1, step=1, key="ow_qty")

        if st.button("🛒 Add to Cart", type="primary", use_container_width=True):
            cart_add({
                "product_id":   prod["id"],
                "product_name": prod["product_name"],
                "price":        price,
                "gst_percent":  float(prod.get("gst_percent") or 0),
                "qty":          qty,
                "eye_side":     selected_eye,
                **selected_power,
                "image_url": (prod["images"][0].get("image_url") if prod.get("images") else None),
            })
            st.success("✅ Added to cart!")
            st.session_state["ow_page"] = "cart"; st.rerun()

        # Description
        desc = prod.get("online_desc","")
        if desc:
            st.markdown("---")
            st.markdown(desc)

        # Specs table
        st.markdown("---")
        specs = {k:v for k,v in {
            "Material": prod.get("material"), "Coating": prod.get("coating_type"),
            "Index": prod.get("index_value"), "Colour": prod.get("colour"),
            "Wear": prod.get("wear_schedule"), "Unit": prod.get("unit"),
            "Box Size": prod.get("box_size"),
        }.items() if v}
        if specs:
            for k,v in specs.items():
                st.markdown(f"**{k}:** {v}")


def _page_cart(customer):
    st.subheader("🛒 Your Cart")
    cart = cart_get()
    if not cart:
        st.info("Your cart is empty")
        if st.button("🛍️ Continue Shopping"):
            st.session_state["ow_page"] = "shop"; st.rerun()
        return

    for ck, item in list(cart.items()):
        c1, c2, c3, c4 = st.columns([4, 2, 2, 1])
        with c1:
            name = item.get("product_name","")
            eye  = item.get("eye_side","")
            sph  = item.get("sph")
            power_str = f" | {eye} eye | SPH {sph:+.2f}" if sph else (" | "+eye+" eye" if eye and eye!="B" else "")
            st.markdown(f"**{name}**{power_str}")
        with c2:
            new_qty = st.number_input("Qty", min_value=1, value=int(item.get("qty",1)),
                                       step=1, key=f"qty_{ck}")
            if new_qty != item.get("qty",1):
                cart_update_qty(ck, new_qty); st.rerun()
        with c3:
            price = float(item.get("price",0)) * int(item.get("qty",1))
            st.markdown(f"₹{price:,.2f}")
        with c4:
            if st.button("🗑️", key=f"rm_{ck}"):
                cart_remove(ck); st.rerun()

    st.markdown("---")

    # Promo code
    pc1, pc2 = st.columns([3,1])
    promo_input = pc1.text_input("Promo Code", key="ow_promo_input").upper()
    if pc2.button("Apply"):
        tots = cart_totals()
        result = validate_promo(promo_input, tots["subtotal"])
        if result.get("valid"):
            st.session_state["ow_promo"] = result
            st.success(f"✅ Promo applied: {promo_input}")
        else:
            st.error(result.get("message","Invalid code"))

    promo = st.session_state.get("ow_promo")
    tots  = cart_totals(promo)

    # Totals
    st.markdown(f"""
    | | |
    |---|---|
    | Subtotal | ₹{tots['subtotal']:,.2f} |
    | Discount | -₹{tots['discount']:,.2f} |
    | Delivery | ₹{tots['delivery']:,.2f} |
    | **Total** | **₹{tots['total']:,.2f}** |
    """)

    cc1, cc2 = st.columns(2)
    with cc1:
        if st.button("🗑️ Clear Cart"): cart_clear(); st.rerun()
    with cc2:
        if st.button("💳 Checkout", type="primary", use_container_width=True):
            if not customer:
                st.session_state["ow_page"] = "login"
            else:
                st.session_state["ow_page"] = "checkout"
            st.rerun()


def _page_checkout(customer):
    if not customer:
        st.session_state["ow_page"] = "login"; st.rerun()

    st.subheader("💳 Checkout")
    addresses = get_addresses(customer["id"])

    # Address selection
    st.markdown("**Delivery Address**")
    if addresses:
        addr_opts = {a["id"]: f"{a['label']}: {a['recipient']}, {a['line1']}, {a['city']} — {a['pincode']}"
                     for a in addresses}
        sel_addr = st.selectbox("Select Address", list(addr_opts.keys()),
                                 format_func=lambda k: addr_opts[k])
    else:
        sel_addr = None

    with st.expander("➕ Add New Address"):
        na1, na2 = st.columns(2)
        new_addr = {
            "label":     na1.text_input("Label", "Home"),
            "recipient": na2.text_input("Recipient Name"),
            "line1":     st.text_input("Address Line 1"),
            "line2":     st.text_input("Address Line 2 (optional)"),
            "city":      na1.text_input("City"),
            "state":     na2.text_input("State"),
            "pincode":   na1.text_input("Pincode"),
            "phone":     na2.text_input("Phone"),
            "is_default": st.checkbox("Set as Default"),
        }
        if st.button("Save Address") and new_addr["recipient"] and new_addr["line1"]:
            aid = save_address(customer["id"], new_addr)
            sel_addr = aid
            st.success("Address saved"); st.rerun()

    # Payment method
    st.markdown("**Payment Method**")
    pm = st.radio("", ["Prepaid (Online Payment)", "Cash on Delivery"],
                  horizontal=True)
    pay_method = "PREPAID" if "Prepaid" in pm else "COD"

    # Order summary
    promo = st.session_state.get("ow_promo")
    tots  = cart_totals(promo)
    st.markdown(f"""
    **Order Total: ₹{tots['total']:,.2f}**  
    Items: {tots['item_count']} | Delivery: ₹{tots['delivery']:,.2f}
    """)

    if st.button("✅ Place Order", type="primary", use_container_width=True):
        if not sel_addr:
            st.error("Select or add a delivery address"); return
        result = place_order(
            customer=customer,
            cart=cart_get(),
            address_id=sel_addr,
            payment_method=pay_method,
            promo_code=st.session_state.get("ow_promo",{}).get("code"),
            totals=tots,
        )
        if result.get("success"):
            cart_clear()
            st.session_state.pop("ow_promo", None)
            st.success(f"🎉 Order placed! #{result['order_no']}")
            if result.get("razorpay_order_id"):
                st.info("Complete payment to confirm your order")
                # Razorpay JS would be injected here in production
            st.session_state["ow_page"] = "orders"; st.rerun()
        else:
            st.error(f"❌ {result.get('message','Order failed')}")


def _page_orders(customer):
    if not customer:
        st.session_state["ow_page"] = "login"; st.rerun()
    st.subheader("📋 My Orders")
    orders = get_orders(customer["id"])
    if not orders:
        st.info("No orders yet")
        return
    status_icon = {"PLACED":"📦","CONFIRMED":"✅","PROCESSING":"⚙️",
                   "SHIPPED":"🚚","DELIVERED":"🎉","CANCELLED":"❌"}
    for o in orders:
        s = o.get("status","")
        with st.expander(
            f"{status_icon.get(s,'•')} {o['order_no']} · ₹{o.get('total',0):,.2f} · {s} · {str(o.get('created_at',''))[:10]}"
        ):
            lines = get_order_lines(str(o["id"]), customer["id"])
            for l in lines:
                sph = l.get("sph")
                pwr = f" | SPH {sph:+.2f}" if sph else ""
                st.markdown(f"- {l.get('product_name','')} × {l.get('qty',1)}{pwr} — ₹{l.get('total_price',0):,.2f}")
            if o.get("tracking_no"):
                st.info(f"Tracking: {o['tracking_no']}")


def _page_account(customer):
    if not customer:
        st.session_state["ow_page"] = "login"; st.rerun()
    st.subheader("👤 My Account")
    st.markdown(f"**{customer.get('name','')}** · {customer.get('mobile','')}")
    if customer.get("email"):
        st.caption(customer["email"])
    st.markdown("---")
    st.subheader("Saved Addresses")
    for a in get_addresses(customer["id"]):
        dflt = "⭐ " if a.get("is_default") else ""
        st.markdown(f"{dflt}**{a.get('label')}**: {a.get('recipient')}, {a.get('line1')}, "
                    f"{a.get('city')} — {a.get('pincode')}")


def _page_appointment(customer):
    st.subheader("🧪 Book Eye Test / Trial Appointment")
    st.caption("For frame trials, contact lens guidance, prescription checks, and home consultation requests.")

    c1, c2 = st.columns(2)
    with c1:
        name = st.text_input("Name", value=(customer or {}).get("name", ""))
        mobile = st.text_input("Mobile", value=(customer or {}).get("mobile", ""), max_chars=15)
        appt_type = st.selectbox(
            "Appointment Type",
            ["Store Eye Test", "Home Consultation", "Contact Lens Trial", "Frame Trial", "Repair / Adjustment"],
        )
    with c2:
        preferred_date = st.date_input("Preferred Date")
        preferred_slot = st.selectbox("Preferred Slot", ["Morning", "Afternoon", "Evening", "Anytime"])
        area = st.text_input("Area / Locality")
    notes = st.text_area("Notes", placeholder="Power concern, contact lens type, frame style, urgency...")

    if st.button("✅ Request Appointment", type="primary", use_container_width=True):
        if not name or not mobile:
            st.error("Enter name and mobile number.")
            return
        try:
            from modules.sql_adapter import run_write
            run_write("""
                INSERT INTO online_appointments
                    (customer_id, name, mobile, appointment_type, preferred_date,
                     preferred_slot, area, notes, status, created_at)
                VALUES
                    (%(cid)s::uuid, %(name)s, %(mobile)s, %(typ)s, %(dt)s,
                     %(slot)s, %(area)s, %(notes)s, 'REQUESTED', NOW())
            """, {
                "cid": (customer or {}).get("id"),
                "name": name,
                "mobile": mobile,
                "typ": appt_type,
                "dt": str(preferred_date),
                "slot": preferred_slot,
                "area": area,
                "notes": notes,
            })
            st.success("Appointment request received. Our team will call and confirm.")
            st.session_state["ow_page"] = "home"
            st.rerun()
        except Exception as e:
            st.error(f"Could not save appointment: {e}")


def _page_login():
    st.subheader("🔐 Login / Register")
    st.caption("We'll send an OTP to your mobile number")
    mobile = st.text_input("Mobile Number", max_chars=15, placeholder="+91XXXXXXXXXX")
    if st.button("Send OTP") and mobile:
        otp = send_otp(mobile)
        st.session_state["ow_otp_mobile"] = mobile
        st.session_state["ow_otp_sent"] = True
        # In production: send via SMS gateway
        st.info(f"OTP sent (dev: {otp})")

    if st.session_state.get("ow_otp_sent"):
        otp_input = st.text_input("Enter OTP", max_chars=6)
        name_input = st.text_input("Your Name (for new accounts)")
        if st.button("Verify & Login", type="primary"):
            mob = st.session_state.get("ow_otp_mobile","")
            if verify_otp(mob, otp_input):
                cust = get_or_create_customer(mob, name_input)
                token = create_session(cust["id"])
                st.session_state.update(ow_token=token, ow_otp_sent=False, ow_page="home")
                st.success(f"✅ Welcome, {cust.get('name','')}!")
                st.rerun()
            else:
                st.error("Invalid or expired OTP")


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def _product_grid(items: list, cols: int = 4):
    if not items:
        st.info("No products found")
        return
    indexed = list(enumerate(items))
    rows = [indexed[i:i+cols] for i in range(0, len(indexed), cols)]
    for row in rows:
        columns = st.columns(cols)
        for col, (pos, prod) in zip(columns, row):
            with col:
                _product_card(prod, key_suffix=f"{pos}_{prod.get('id')}")


def _product_card(prod: dict, key_suffix: str | None = None):
    price = float(prod.get("price") or 0)
    mrp   = float(prod.get("mrp") or 0)
    name  = prod.get("product_name","")
    brand = prod.get("brand","")
    badge = prod.get("online_badge","")
    stock = int(prod.get("stock_qty") or 0)

    img_html = ""
    img_src = prod.get("image_url") or prod.get("image_b64")
    if img_src:
        img_html = f"<img src='{img_src}' style='width:100%;border-radius:6px;aspect-ratio:1;object-fit:cover'>"
    else:
        img_html = "<div style='width:100%;aspect-ratio:1;background:#1e293b;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:2.5rem'>👓</div>"

    badge_html = ""
    if badge:
        colors = {"BESTSELLER":"#f59e0b","NEW":"#10b981","SALE":"#ef4444","FEATURED":"#8b5cf6"}
        bc = colors.get(badge,"#64748b")
        badge_html = f"<div style='position:absolute;top:6px;right:6px;background:{bc};color:white;font-size:0.6rem;padding:2px 6px;border-radius:10px;font-weight:700'>{badge}</div>"

    disc_html = ""
    if mrp > price > 0:
        pct = round((mrp - price)/mrp*100)
        disc_html = f"<span style='color:#64748b;font-size:0.7rem'><s>₹{mrp:,.0f}</s> -{pct}%</span>"

    stock_html = ""
    if stock == 0:
        stock_html = "<div style='color:#ef4444;font-size:0.65rem'>Out of stock</div>"

    st.markdown(f"""
<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;
     padding:10px;margin-bottom:8px;position:relative'>
  {badge_html}
  {img_html}
  <div style='font-size:0.78rem;font-weight:700;color:#f1f5f9;
       margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>{name}</div>
  <div style='font-size:0.7rem;color:#64748b;margin-bottom:4px'>{brand}</div>
  <div style='font-size:1rem;font-weight:800;color:#10b981'>₹{price:,.0f}</div>
  {disc_html}
  {stock_html}
</div>
""", unsafe_allow_html=True)

    if stock > 0:
        _key = key_suffix or str(prod.get("id") or "")
        if st.button("Add to Cart", key=f"addcart_{_key}",
                     use_container_width=True):
            # Open product page for power selection
            st.session_state.update(ow_page="product", ow_product_id=prod["id"])
            st.rerun()


def _css():
    st.markdown("""
<style>
  .stApp { background: #020817; }
  .stApp * { color: #e2e8f0; }
  section[data-testid="stSidebar"] { background: #0f172a; }
  .stButton button { background:#1e3a8a;color:white;border:none;border-radius:6px; }
  .stButton button:hover { background:#1d4ed8; }
  div[data-testid="metric-container"] { background:#0f172a;border-radius:8px;padding:8px; }
  .stTextInput input, .stSelectbox select, .stNumberInput input {
    background:#0f172a !important; color:#e2e8f0 !important;
    border:1px solid #1e293b !important; border-radius:6px;
  }
  h1,h2,h3 { color:#f8fafc !important; }
</style>
""", unsafe_allow_html=True)


if __name__ == "__main__":
    render_online_store()
