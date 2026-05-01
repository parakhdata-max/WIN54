"""
ophthalmic_addon_manager.py
────────────────────────────
Full CRUD UI for managing ophthalmic add-ons.
Deploy to: modules/ophthalmic_addon_manager.py

Accessed via: Masters & Stock → 🔬 Ophthalmic Add-ons
"""
import streamlit as st
from typing import Optional


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB error: {e}"); return []


def _exec(sql, params=None) -> bool:
    try:
        from modules.sql_adapter import execute_query
        execute_query(sql, params or {})
        return True
    except Exception as e:
        st.error(f"DB error: {e}"); return False


# ── helpers ───────────────────────────────────────────────────────────────────

def _all_brands() -> list[str]:
    rows = _q("SELECT DISTINCT brand FROM ophthalmic_addons ORDER BY brand")
    return [r["brand"] for r in rows]

def _all_products() -> list[dict]:
    return _q("""
        SELECT id::text AS id, product_name, brand
        FROM products
        WHERE LOWER(main_group) LIKE '%ophthalmic%' AND is_active=TRUE
        ORDER BY brand, product_name
    """)

def _load_addons(brand_filter=None, search=None) -> list[dict]:
    where = "WHERE a.is_active = TRUE"
    params = {}
    if brand_filter and brand_filter != "All":
        where += " AND a.brand = %(brand)s"; params["brand"] = brand_filter
    if search:
        where += " AND (LOWER(a.addon_name) LIKE %(s)s OR LOWER(a.notes) LIKE %(s)s)"
        params["s"] = f"%{search.lower()}%"
    return _q(f"""
        SELECT a.id::text AS id, a.brand, a.addon_name, a.addon_category,
               a.applies_to, a.wlp_addon, a.srp_addon, a.is_percentage,
               a.sort_order, a.notes,
               p.product_name AS product_name,
               a.product_id::text AS product_id
        FROM ophthalmic_addons a
        LEFT JOIN products p ON p.id = a.product_id
        {where}
        ORDER BY a.brand, a.sort_order, a.addon_name
    """, params)


def _upsert_addon(data: dict) -> bool:
    pid = data.get("product_id") or None
    return _exec("""
        INSERT INTO ophthalmic_addons
            (brand, addon_name, addon_category, applies_to,
             wlp_addon, srp_addon, is_percentage, sort_order, notes,
             product_id, is_active)
        VALUES (%(brand)s, %(addon_name)s, %(addon_category)s, %(applies_to)s,
                %(wlp)s, %(srp)s, %(is_pct)s, %(sort)s, %(notes)s,
                %(pid)s::uuid, TRUE)
        ON CONFLICT (brand, addon_name, applies_to, COALESCE(product_id::text,'ALL'))
        DO UPDATE SET
            addon_category = EXCLUDED.addon_category,
            wlp_addon      = EXCLUDED.wlp_addon,
            srp_addon      = EXCLUDED.srp_addon,
            is_percentage  = EXCLUDED.is_percentage,
            sort_order     = EXCLUDED.sort_order,
            notes          = EXCLUDED.notes,
            product_id     = EXCLUDED.product_id,
            is_active      = TRUE
    """, {**data, "pid": pid})


def _deactivate(addon_id: str) -> bool:
    return _exec(
        "UPDATE ophthalmic_addons SET is_active=FALSE WHERE id=%(id)s::uuid",
        {"id": addon_id}
    )


# ── UI ────────────────────────────────────────────────────────────────────────

def render_ophthalmic_addon_manager():
    st.title("🔬 Ophthalmic Add-ons")
    st.caption("Manage brand / category / product-level add-ons (Transitions, Blue Capture, EyeCode…)")

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_view, tab_add, tab_edit = st.tabs([
        "📋 View / Delete",
        "➕ Add New",
        "✏️ Edit Existing",
    ])

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1 — VIEW
    # ══════════════════════════════════════════════════════════════════════════
    with tab_view:
        fc1, fc2 = st.columns([2,3])
        brands = ["All"] + _all_brands()
        brand_f = fc1.selectbox("Filter by Brand", brands, key="adm_brand_f")
        search  = fc2.text_input("Search name / notes", placeholder="Blue Capture…", key="adm_search")

        addons = _load_addons(brand_f if brand_f != "All" else None, search or None)

        if not addons:
            st.info("No add-ons found. Add one using the ➕ Add New tab.")
            return

        # Group by brand
        from itertools import groupby
        key_fn = lambda a: a["brand"]
        for brand, group in groupby(sorted(addons, key=key_fn), key_fn):
            group = list(group)
            st.markdown(f"#### {brand}  <span style='color:#888;font-size:.85rem'>({len(group)} add-ons)</span>",
                        unsafe_allow_html=True)

            for a in group:
                scope = (
                    f"🟢 **Product:** {a['product_name']}" if a.get("product_name")
                    else f"🔵 **Category:** {a['applies_to']}" if a["applies_to"] != "ALL"
                    else "⚪ **Brand level** (ALL)"
                )
                wlp_str = f"WLP +₹{a['wlp_addon']:,.0f}" if a.get("wlp_addon") else "WLP —"
                srp_str = f"SRP +₹{a['srp_addon']:,.0f}" if a.get("srp_addon") else "SRP —"

                with st.container():
                    c1, c2, c3, c4 = st.columns([3,2,2,1])
                    c1.markdown(f"**{a['addon_name']}** · _{a.get('addon_category','')}_  \n{scope}")
                    c2.caption(wlp_str)
                    c3.caption(srp_str)
                    if c4.button("🗑️", key=f"del_{a['id']}", help="Deactivate"):
                        if _deactivate(a["id"]):
                            st.success(f"'{a['addon_name']}' deactivated.")
                            st.rerun()
                    if a.get("notes"):
                        st.caption(f"  💬 {a['notes']}")
                st.divider()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2 — ADD NEW
    # ══════════════════════════════════════════════════════════════════════════
    with tab_add:
        st.markdown("#### New Add-on")

        all_products = _all_products()
        product_options = ["— Brand / Category level (no product) —"] + \
                          [f"{p['product_name']} ({p['brand']})" for p in all_products]
        product_map = {f"{p['product_name']} ({p['brand']})": p["id"] for p in all_products}

        with st.form("add_addon_form", clear_on_submit=True):
            ac1, ac2 = st.columns(2)
            brand      = ac1.text_input("Brand *", placeholder="Essilor / Hoya / Shamir / Zeiss")
            addon_name = ac2.text_input("Add-on Name *", placeholder="Blue UV Capture")

            bc1, bc2, bc3 = st.columns(3)
            cat        = bc1.selectbox("Category",
                            ["Protection","Photochromic","Coating","Tint","Personalisation","General"])
            applies_to = bc2.selectbox("Applies To",
                            ["ALL","Progressive","SV RX","SV Stock","Bifocal","Reading"])
            sort_order = bc3.number_input("Sort Order", value=99, min_value=1, step=1)

            prod_sel   = st.selectbox("Product (optional — leave blank for brand/category level)",
                                       product_options, key="add_prod_sel")

            pc1, pc2 = st.columns(2)
            wlp_addon = pc1.number_input("WLP Add-on (₹/pair)", min_value=0.0,
                                          step=50.0, value=0.0,
                                          help="Leave 0 if not applicable")
            srp_addon = pc2.number_input("SRP/MRP Add-on (₹/pair)", min_value=0.0,
                                          step=50.0, value=0.0)
            is_pct    = st.checkbox("Values are percentages (not fixed ₹)")
            notes     = st.text_input("Notes / tooltip", placeholder="+₹250/pair over base WLP")

            submitted = st.form_submit_button("💾 Save Add-on", type="primary",
                                               use_container_width=True)

        if submitted:
            if not brand or not addon_name:
                st.error("Brand and Add-on Name are required.")
            else:
                pid = product_map.get(prod_sel) if prod_sel != product_options[0] else None
                data = {
                    "brand":        brand.strip(),
                    "addon_name":   addon_name.strip(),
                    "addon_category": cat,
                    "applies_to":   applies_to,
                    "wlp":          wlp_addon if wlp_addon > 0 else None,
                    "srp":          srp_addon if srp_addon > 0 else None,
                    "is_pct":       is_pct,
                    "sort":         int(sort_order),
                    "notes":        notes.strip() or None,
                    "product_id":   pid,
                }
                if _upsert_addon(data):
                    scope_label = (f"product: {prod_sel}" if pid
                                   else f"{applies_to} · {brand}")
                    st.success(f"✅ '{addon_name}' saved ({scope_label})")
                    st.balloons()
                    st.rerun()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 3 — EDIT EXISTING
    # ══════════════════════════════════════════════════════════════════════════
    with tab_edit:
        addons_all = _load_addons()
        if not addons_all:
            st.info("No add-ons to edit.")
        else:
            addon_labels = [
                f"{a['brand']} — {a['addon_name']}"
                + (f" [{a['applies_to']}]" if a['applies_to'] != 'ALL' else "")
                + (f" 📌{a['product_name']}" if a.get('product_name') else "")
                for a in addons_all
            ]
            sel_idx = st.selectbox("Select add-on to edit", range(len(addon_labels)),
                                   format_func=lambda i: addon_labels[i],
                                   key="edit_addon_sel")
            a = addons_all[sel_idx]

            all_products_e = _all_products()
            prod_options_e = ["— Brand / Category level —"] + \
                             [f"{p['product_name']} ({p['brand']})" for p in all_products_e]
            prod_map_e = {f"{p['product_name']} ({p['brand']})": p["id"] for p in all_products_e}
            current_prod = (f"{a['product_name']} ({a['brand']})" if a.get("product_name") else None)
            prod_default_e = (prod_options_e.index(current_prod)
                              if current_prod and current_prod in prod_options_e else 0)

            with st.form("edit_addon_form"):
                ec1, ec2 = st.columns(2)
                e_brand      = ec1.text_input("Brand",      value=a["brand"])
                e_name       = ec2.text_input("Add-on Name",value=a["addon_name"])
                ebc1,ebc2,ebc3 = st.columns(3)
                cats = ["Protection","Photochromic","Coating","Tint","Personalisation","General"]
                cat_def = cats.index(a["addon_category"]) if a.get("addon_category") in cats else 0
                e_cat  = ebc1.selectbox("Category", cats, index=cat_def)
                apps   = ["ALL","Progressive","SV RX","SV Stock","Bifocal","Reading"]
                app_def = apps.index(a["applies_to"]) if a.get("applies_to") in apps else 0
                e_apps = ebc2.selectbox("Applies To", apps, index=app_def)
                e_sort = ebc3.number_input("Sort Order", value=int(a.get("sort_order") or 99), step=1)

                e_prod = st.selectbox("Product (optional)", prod_options_e,
                                      index=prod_default_e, key="edit_prod_sel")

                epc1, epc2 = st.columns(2)
                e_wlp = epc1.number_input("WLP Add-on (₹/pair)",
                                           value=float(a.get("wlp_addon") or 0), step=50.0)
                e_srp = epc2.number_input("SRP Add-on (₹/pair)",
                                           value=float(a.get("srp_addon") or 0), step=50.0)
                e_pct  = st.checkbox("Is percentage", value=bool(a.get("is_percentage")))
                e_note = st.text_input("Notes", value=a.get("notes") or "")

                save_edit = st.form_submit_button("💾 Update", type="primary",
                                                   use_container_width=True)

            if save_edit:
                epid = prod_map_e.get(e_prod) if e_prod != prod_options_e[0] else None
                data_e = {
                    "brand":        e_brand.strip(),
                    "addon_name":   e_name.strip(),
                    "addon_category": e_cat,
                    "applies_to":   e_apps,
                    "wlp":          e_wlp if e_wlp > 0 else None,
                    "srp":          e_srp if e_srp > 0 else None,
                    "is_pct":       e_pct,
                    "sort":         int(e_sort),
                    "notes":        e_note.strip() or None,
                    "product_id":   epid,
                }
                if _upsert_addon(data_e):
                    st.success(f"✅ '{e_name}' updated.")
                    st.rerun()
