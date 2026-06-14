"""
modules/online_store/store_admin.py
=====================================
Admin UI: set online_price, toggle active, upload images, manage promos.
Plugs into existing backoffice as a new tab.
"""
import streamlit as st
import base64, uuid, io
from urllib.parse import quote_plus


def _rq(sql, p=None):
    from modules.sql_adapter import run_query
    return run_query(sql, p) or []

def _rw(sql, p=None):
    from modules.sql_adapter import run_write
    return run_write(sql, p)


def render_online_store_admin():
    st.title("🌐 Online Orders")
    st.caption("Review online orders, notify customers, and push accepted orders to the central backoffice flow.")
    tabs = st.tabs(["📊 Orders", "🧪 Appointments", "📦 Products", "🏷️ Promo Codes", "⚙️ Settings"])

    with tabs[0]: _orders_tab()
    with tabs[1]: _appointments_tab()
    with tabs[2]: _products_tab()
    with tabs[3]: _promos_tab()
    with tabs[4]: _settings_tab()


# ── Products Tab ──────────────────────────────────────────────────────────

def _products_tab():
    st.subheader("Manage Online Products")
    col1, col2, col3 = st.columns([3, 2, 2])
    search = col1.text_input("Search products", placeholder="Name, brand...")
    cat    = col2.selectbox("Category", ["All"] + [r["main_group"] for r in _rq(
        "SELECT DISTINCT main_group FROM products WHERE is_active=TRUE ORDER BY main_group")])
    show   = col3.selectbox("Show", ["All", "Online Active", "Not Online"])

    where = ["p.is_active = TRUE"]
    params = {}
    if search:
        where.append("LOWER(p.product_name) LIKE %(s)s OR LOWER(p.brand) LIKE %(s)s")
        params["s"] = f"%{search.lower()}%"
    if cat != "All":
        where.append("p.main_group = %(cat)s"); params["cat"] = cat
    if show == "Online Active": where.append("p.online_active = TRUE")
    elif show == "Not Online":  where.append("(p.online_active IS NULL OR p.online_active = FALSE)")

    products = _rq(f"""
        SELECT p.id::text, p.product_name, p.brand, p.main_group,
               p.online_active, p.online_price,
               COALESCE(MAX(i.mrp), 0)                       AS mrp,
               COALESCE(SUM(i.quantity) FILTER (WHERE i.is_active=TRUE), 0) AS stock,
               (SELECT COUNT(*) FROM product_images WHERE product_id=p.id) AS img_count
        FROM products p
        LEFT JOIN inventory_stock i ON i.product_id=p.id
        WHERE {" AND ".join(where)}
        GROUP BY p.id, p.product_name, p.brand, p.main_group, p.online_active, p.online_price
        ORDER BY p.online_active DESC NULLS LAST, p.product_name
        LIMIT 100
    """, params)

    st.caption(f"{len(products)} products")

    for row in products:
        active = bool(row.get("online_active"))
        mrp    = float(row.get("mrp") or 0)
        op     = float(row.get("online_price") or 0)
        stock  = int(row.get("stock") or 0)
        imgs   = int(row.get("img_count") or 0)
        badge  = "🟢" if active else "⚪"

        with st.expander(
            f"{badge} {row['product_name']} — {row.get('brand','')} "
            f"| MRP ₹{mrp:,.0f} | Stock {stock} | {imgs} img(s)"
        ):
            c1, c2 = st.columns(2)
            new_active = c1.checkbox("Publish Online", value=active,
                                     key=f"oa_{row['id']}")
            new_price  = c2.number_input("Online Price ₹", value=op or mrp,
                                          min_value=0.0, step=10.0,
                                          key=f"op_{row['id']}")
            new_desc   = st.text_area("Online Description",
                                       value=row.get("online_desc") or "",
                                       key=f"od_{row['id']}", height=80)
            badge_opts = ["", "BESTSELLER", "NEW", "SALE", "FEATURED"]
            new_badge  = st.selectbox("Badge",  badge_opts,
                                       index=badge_opts.index(row.get("online_badge") or ""),
                                       key=f"ob_{row['id']}")

            # Image upload
            uploaded = st.file_uploader("Upload Image",
                                         type=["jpg","jpeg","png","webp"],
                                         key=f"img_{row['id']}")

            if st.button("💾 Save", key=f"save_{row['id']}", type="primary"):
                _rw("""
                    UPDATE products
                    SET online_active=%(a)s, online_price=%(p)s,
                        online_desc=%(d)s, online_badge=%(b)s
                    WHERE id=%(id)s::uuid
                """, {"a": new_active, "p": new_price, "d": new_desc,
                       "b": new_badge, "id": row["id"]})
                if uploaded:
                    import base64 as _b64
                    _mime = uploaded.type or "image/jpeg"
                    _b64_data = _b64.b64encode(uploaded.read()).decode()
                    # Store as data URL in image_url — image_b64 column does not exist in migration
                    _data_url = f"data:{_mime};base64,{_b64_data}"
                    _rw("""
                        INSERT INTO product_images
                            (id, product_id, image_url, alt_text, is_primary, sort_order)
                        VALUES (%(id)s::uuid, %(pid)s::uuid, %(url)s, %(alt)s,
                                NOT EXISTS (SELECT 1 FROM product_images WHERE product_id=%(pid)s::uuid AND is_primary=TRUE), 
                                COALESCE((SELECT MAX(sort_order)+1 FROM product_images WHERE product_id=%(pid)s::uuid), 0))
                    """, {"id": str(uuid.uuid4()), "pid": row["id"],
                           "url": _data_url, "alt": row["product_name"]})
                st.success("✅ Saved")
                st.rerun()


# ── Promos Tab ────────────────────────────────────────────────────────────

def _promos_tab():
    st.subheader("Promo Codes")

    promos = _rq("""
        SELECT * FROM promo_codes ORDER BY created_at DESC
    """)

    for p in promos:
        active = "✅" if p.get("is_active") else "❌"
        desc = f"{p['code']} — {p.get('disc_value')}{'%' if p.get('disc_type')=='PCT' else '₹'} off"
        st.markdown(f"{active} **{desc}** | Uses: {p.get('uses_count',0)}/{p.get('uses_limit','∞')} | Expires: {p.get('valid_to','—')}")

    st.divider()
    st.subheader("New Promo Code")
    nc1, nc2, nc3 = st.columns(3)
    new_code  = nc1.text_input("Code").upper()
    disc_type = nc2.selectbox("Type", ["PCT","FLAT"])
    disc_val  = nc3.number_input("Value", min_value=0.0, step=1.0)
    nc4, nc5, nc6 = st.columns(3)
    min_order = nc4.number_input("Min Order ₹", min_value=0.0, value=0.0)
    max_disc  = nc5.number_input("Max Discount ₹ (PCT cap)", min_value=0.0, value=0.0)
    valid_to  = nc6.date_input("Valid Until")
    uses_lim  = st.number_input("Max Uses (0=unlimited)", min_value=0, step=1)

    if st.button("➕ Create Promo", type="primary"):
        if new_code:
            _rw("""
                INSERT INTO promo_codes
                    (code, disc_type, disc_value, min_order, max_disc, valid_to, uses_limit)
                VALUES (%(c)s, %(dt)s, %(dv)s, %(mo)s, %(md)s, %(vt)s, %(ul)s)
            """, {"c": new_code, "dt": disc_type, "dv": disc_val,
                   "mo": min_order, "md": max_disc or None,
                   "vt": valid_to, "ul": uses_lim or None})
            st.success(f"✅ Promo {new_code} created")
            st.rerun()


# ── Orders Tab ────────────────────────────────────────────────────────────

def _orders_tab():
    st.subheader("Online Orders")
    c1, c2 = st.columns([2, 1])
    search = c1.text_input(
        "Search online order",
        placeholder="Order no, customer, mobile...",
        key="online_order_search",
    ).strip()
    status_filter = c2.selectbox(
        "Status",
        ["All", "PLACED", "UNDER_REVIEW", "CONFIRMED", "PROCESSING", "SHIPPED", "DELIVERED", "CANCELLED"],
        key="online_order_status_filter",
    )

    where = ["1=1"]
    params = {}
    if search:
        where.append("""
            (
                LOWER(COALESCE(o.order_no,'')) LIKE %(s)s
                OR LOWER(COALESCE(c.name,'')) LIKE %(s)s
                OR COALESCE(c.mobile,'') LIKE %(s)s
            )
        """)
        params["s"] = f"%{search.lower()}%"
    if status_filter != "All":
        where.append("o.status = %(status)s")
        params["status"] = status_filter

    orders = _rq(f"""
        SELECT o.*, c.name AS customer_name, c.mobile
        FROM online_orders o
        JOIN online_customers c ON c.id = o.customer_id
        WHERE {" AND ".join(where)}
        ORDER BY o.created_at DESC LIMIT 100
    """, params)

    if not orders:
        st.info("No online orders found for selected filters.")
        return

    for o in orders:
        status_icon = {"PLACED":"📦","UNDER_REVIEW":"🔎","CONFIRMED":"✅","PROCESSING":"⚙️","SHIPPED":"🚚",
                        "DELIVERED":"🎉","CANCELLED":"❌"}.get(o.get("status",""),"•")
        with st.expander(
            f"{status_icon} {o['order_no']} — {o.get('customer_name') or 'Customer'} "
            f"({o.get('mobile') or '—'}) | ₹{float(o.get('total') or 0):,.2f} | "
            f"{o.get('payment_method') or '—'} | {o.get('status') or 'PLACED'}",
            expanded=False,
        ):
            lines = _rq("""
                SELECT product_name, eye_side, sph, cyl, axis, add_power,
                       qty, unit_price, total_price
                FROM online_order_lines
                WHERE order_id=%(oid)s::uuid
                ORDER BY eye_side NULLS LAST, product_name
            """, {"oid": str(o["id"])})

            if lines:
                st.markdown("**Items**")
                for line in lines:
                    rx_bits = []
                    if line.get("sph") is not None: rx_bits.append(f"S{line.get('sph')}")
                    if line.get("cyl") is not None: rx_bits.append(f"C{line.get('cyl')}")
                    if line.get("axis") is not None: rx_bits.append(f"AX{line.get('axis')}")
                    if line.get("add_power") is not None: rx_bits.append(f"ADD{line.get('add_power')}")
                    eye = line.get("eye_side") or "B"
                    st.markdown(
                        f"- **{eye}** {line.get('product_name') or 'Item'} "
                        f"{' · ' + ' '.join(rx_bits) if rx_bits else ''} "
                        f"· Qty {line.get('qty') or 1} · ₹{float(line.get('total_price') or 0):,.2f}"
                    )

            status_options = ["PLACED", "UNDER_REVIEW", "CONFIRMED", "PROCESSING", "SHIPPED", "DELIVERED", "CANCELLED"]
            current_status = o.get("status") or "PLACED"
            idx = status_options.index(current_status) if current_status in status_options else 0
            new_status = st.selectbox(
                "Order status",
                status_options,
                index=idx,
                key=f"online_status_{o['id']}",
            )
            notes = st.text_area(
                "Internal correction / review note",
                value=o.get("notes") or "",
                height=70,
                key=f"online_notes_{o['id']}",
            )

            b1, b2, b3 = st.columns([1, 1, 1])
            if b1.button("💾 Save Review", key=f"online_save_{o['id']}"):
                _rw("""
                    UPDATE online_orders
                    SET status=%(status)s, notes=%(notes)s, updated_at=NOW()
                    WHERE id=%(id)s::uuid
                """, {"status": new_status, "notes": notes, "id": str(o["id"])})
                st.success("Online order updated")
                st.rerun()

            mobile = "".join(ch for ch in str(o.get("mobile") or "") if ch.isdigit())
            if mobile and len(mobile) == 10:
                mobile = "91" + mobile
            msg = _online_under_review_message(o, lines)
            wa_url = f"https://wa.me/{mobile}?text={quote_plus(msg)}" if mobile else ""
            if wa_url:
                b2.link_button("💬 WhatsApp Under Review", wa_url, use_container_width=True)
            else:
                b2.warning("Mobile missing")

            can_push = (o.get("payment_method") == "COD" or o.get("payment_status") == "PAID")
            if not o.get("erp_order_id") and can_push:
                if b3.button("↪ Send to Backoffice", key=f"mirror_{o['id']}"):
                    try:
                        _rw("""
                            UPDATE online_orders
                            SET status='UNDER_REVIEW', notes=%(notes)s, updated_at=NOW()
                            WHERE id=%(id)s::uuid
                        """, {"notes": notes, "id": str(o["id"])})
                        from modules.online_store.store_orders import _mirror_to_erp
                        _mirror_to_erp(str(o["id"]))
                        st.success("Sent to central backoffice")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Backoffice push failed: {e}")
            elif o.get("erp_order_id"):
                b3.success("Already in Backoffice")
            else:
                b3.warning("Payment pending")


def _online_under_review_message(order: dict, lines: list[dict]) -> str:
    name = order.get("customer_name") or "Customer"
    order_no = order.get("order_no") or "your order"
    total = float(order.get("total") or 0)
    msg = [
        f"Hello {name},",
        "",
        "Your online order is under review.",
        f"Order No: {order_no}",
        f"Total: Rs {total:,.2f}",
        "",
        "Items:",
    ]
    if lines:
        for line in lines[:8]:
            eye = line.get("eye_side") or "B"
            rx_bits = []
            if line.get("sph") is not None: rx_bits.append(f"S{line.get('sph')}")
            if line.get("cyl") is not None: rx_bits.append(f"C{line.get('cyl')}")
            if line.get("axis") is not None: rx_bits.append(f"AX{line.get('axis')}")
            if line.get("add_power") is not None: rx_bits.append(f"ADD{line.get('add_power')}")
            msg.append(f"- {eye}: {line.get('product_name') or 'Item'} {' '.join(rx_bits)} Qty {line.get('qty') or 1}")
    else:
        msg.append("- Order items are being checked.")
    msg += [
        "",
        "Our team will confirm availability and call/message you if any correction is needed.",
        "Parakh Eye Care",
    ]
    return "\n".join(msg)


# ── Appointments Tab ──────────────────────────────────────────────────────

def _appointments_tab():
    st.subheader("Eye Test / Consultation Bookings")

    c1, c2 = st.columns([2, 1])
    search = c1.text_input(
        "Search appointment",
        placeholder="Name, mobile, area...",
        key="online_appt_search",
    ).strip()
    status = c2.selectbox(
        "Status",
        ["All", "REQUESTED", "CONFIRMED", "DONE", "CANCELLED"],
        key="online_appt_status",
    )

    where = ["1=1"]
    params = {}
    if search:
        where.append("""
            (
                LOWER(COALESCE(name,'')) LIKE %(s)s
                OR COALESCE(mobile,'') LIKE %(s)s
                OR LOWER(COALESCE(area,'')) LIKE %(s)s
            )
        """)
        params["s"] = f"%{search.lower()}%"
    if status != "All":
        where.append("status = %(status)s")
        params["status"] = status

    rows = _rq(f"""
        SELECT id::text, name, mobile, appointment_type,
               preferred_date::text AS preferred_date,
               preferred_slot, area, notes, status,
               created_at::text AS created_at
        FROM online_appointments
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC
        LIMIT 200
    """, params)

    if not rows:
        st.info("No appointments found.")
        return

    st.caption(f"{len(rows)} appointment(s)")
    status_options = ["REQUESTED", "CONFIRMED", "DONE", "CANCELLED"]
    for row in rows:
        label = (
            f"{row.get('status') or 'REQUESTED'} · {row.get('name') or 'Customer'} "
            f"· {row.get('mobile') or '—'} · {row.get('preferred_date') or '—'}"
        )
        with st.expander(label):
            st.markdown(
                f"**Type:** {row.get('appointment_type') or '—'}  \n"
                f"**Slot:** {row.get('preferred_slot') or '—'}  \n"
                f"**Area:** {row.get('area') or '—'}  \n"
                f"**Notes:** {row.get('notes') or '—'}  \n"
                f"**Booked:** {str(row.get('created_at') or '')[:16]}"
            )
            current_status = row.get("status") or "REQUESTED"
            idx = status_options.index(current_status) if current_status in status_options else 0
            new_status = st.selectbox(
                "Update status",
                status_options,
                index=idx,
                key=f"appt_status_{row['id']}",
            )
            if st.button("💾 Save Appointment Status", key=f"appt_save_{row['id']}"):
                _rw("""
                    UPDATE online_appointments
                    SET status=%(status)s, updated_at=NOW()
                    WHERE id=%(id)s::uuid
                """, {"status": new_status, "id": row["id"]})
                st.success("Appointment updated")
                st.rerun()


# ── Settings Tab ──────────────────────────────────────────────────────────

def _settings_tab():
    st.subheader("Store Settings")
    settings = {r["key"]: r["value"] for r in _rq(
        "SELECT key, value FROM system_settings WHERE key LIKE 'ONLINE_%' OR key LIKE 'RAZORPAY_%'"
    )}
    keys_to_show = [
        ("ONLINE_STORE_NAME",     "Store Name",       "OpticalWala"),
        ("ONLINE_FREE_DELIVERY",  "Free Delivery Above ₹", "500"),
        ("ONLINE_DELIVERY_CHARGE","Delivery Charge ₹", "50"),
        ("ONLINE_SUPPORT_PHONE",  "Support Phone",    ""),
        ("RAZORPAY_KEY_ID",       "Razorpay Key ID",  ""),
        ("RAZORPAY_KEY_SECRET",   "Razorpay Secret",  ""),
    ]
    for key, label, default in keys_to_show:
        val = st.text_input(label, value=settings.get(key, default), key=f"s_{key}")
        if val != settings.get(key, default):
            _rw("""
                INSERT INTO system_settings (key, value) VALUES (%(k)s, %(v)s)
                ON CONFLICT (key) DO UPDATE SET value=%(v)s
            """, {"k": key, "v": val})
