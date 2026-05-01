"""
modules/ui/inventory_search.py
================================
Smart Inventory Search — all products table fields as live dropdowns.
Flat filter layout (no tabs, no expanders to open).
All filter values loaded directly from DB — nothing hardcoded.
"""

import streamlit as st
import pandas as pd


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCH  (cached 60s)
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=60, show_spinner=False)
def _load_inventory() -> pd.DataFrame:
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                -- Product master fields (all searchable)
                p.id::text                              AS product_id,
                COALESCE(p.product_name,'')             AS product_name,
                COALESCE(p.brand,'')                    AS brand,
                -- Normalise main_group: collapse ALLCAPS / mixed-case duplicates
                CASE
                    WHEN UPPER(TRIM(p.main_group)) = 'OPHTHALMIC LENSES' THEN 'Ophthalmic Lenses'
                    WHEN UPPER(TRIM(p.main_group)) = 'CONTACT LENSES'    THEN 'Contact Lenses'
                    WHEN UPPER(TRIM(p.main_group)) = 'SERVICE'           THEN 'Service'
                    WHEN UPPER(TRIM(p.main_group)) = 'FRAMES'            THEN 'Frames'
                    WHEN UPPER(TRIM(p.main_group)) = 'SUNGLASSES'        THEN 'Sunglasses'
                    WHEN UPPER(TRIM(p.main_group)) = 'SOLUTION'          THEN 'Solution'
                    WHEN UPPER(TRIM(p.main_group)) = 'ACCESSORIES'       THEN 'Accessories'
                    WHEN UPPER(TRIM(p.main_group)) = 'CLOTH'             THEN 'Accessories'
                    ELSE INITCAP(TRIM(COALESCE(p.main_group,'')))
                END                                         AS main_group,
                COALESCE(p.category,'')                 AS category,
                COALESCE(p.lens_category,'')            AS lens_category,
                COALESCE(p.material,'')                 AS material,
                COALESCE(p.colour,'')                   AS colour,
                COALESCE(p.coating,'')                  AS coating,
                COALESCE(p.coating_type,'')             AS coating_type,
                COALESCE(p.index_value::text,'')        AS lens_index,
                COALESCE(p.gender,'')                   AS gender,
                COALESCE(p.wear_schedule,'')            AS wear_schedule,
                COALESCE(p.unit,'PCS')                  AS unit,
                COALESCE(p.box_size,1)                  AS box_size,
                COALESCE(p.gst_percent,0)               AS gst_percent,
                COALESCE(p.hsn_code,'')                 AS hsn_code,

                -- Stock fields
                COALESCE(s.id::text, '')                AS stock_id,
                COALESCE(s.batch_no,'')                 AS batch_no,
                COALESCE(s.location,'')                 AS location,
                COALESCE(s.quantity,0)                  AS qty,
                COALESCE(s.mrp, s.selling_price, 0)     AS mrp,
                COALESCE(s.selling_price,0)             AS selling_price,
                COALESCE(s.purchase_rate,0)             AS purchase_rate,
                COALESCE(s.sph::text,'')                AS sph,
                COALESCE(s.cyl::text,'')                AS cyl,
                COALESCE(s.axis::text,'')               AS axis,
                COALESCE(s.add_power::text,'')          AS add_power,
                COALESCE(s.eye_side,'')                 AS eye_side,
                COALESCE(s.expiry_date::text,'')        AS expiry_date,
                COALESCE(s.barcode,'')                  AS barcode,
                COALESCE(s.item_code,'')                AS item_code,
                COALESCE(p.barcode,'')                  AS product_barcode

            FROM products p
            LEFT JOIN inventory_stock s
                ON s.product_id = p.id
               AND COALESCE(s.is_active, true) = true
            WHERE COALESCE(p.is_active, true) = true
            ORDER BY p.main_group, p.category, p.product_name
            LIMIT 100000
        """)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        # Ensure numeric
        for col in ["qty", "mrp", "selling_price", "purchase_rate", "gst_percent", "box_size"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df
    except Exception as ex:
        st.error(f"Inventory load error: {ex}")
        return pd.DataFrame()


def _opts(df: pd.DataFrame, col: str) -> list:
    """Get sorted unique non-empty values for a column — for dropdown."""
    if col not in df.columns:
        return []
    return sorted(
        df[col].dropna().astype(str).str.strip()
        .replace("", pd.NA).dropna().unique().tolist()
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════

def _normalise_main_groups():
    """One-time DB fix: collapse all ALLCAPS/mixed-case main_group variants."""
    try:
        from modules.sql_adapter import run_write
        norm_cases = [
            ('OPHTHALMIC LENSES', 'Ophthalmic Lenses'),
            ('Ophthalmic Lenses', 'Ophthalmic Lenses'),
            ('CONTACT LENSES',    'Contact Lenses'),
            ('Contact Lenses',    'Contact Lenses'),
            ('SERVICE',           'Service'),
            ('FRAMES',            'Frames'),
            ('Frames',            'Frames'),
            ('SUNGLASSES',        'Sunglasses'),
            ('Sunglasses',        'Sunglasses'),
            ('SOLUTION',          'Solution'),
            ('Solution',          'Solution'),
            ('ACCESSORIES',       'Accessories'),
            ('Cloth',             'Accessories'),
            ('CLOTH',             'Accessories'),
        ]
        total = 0
        for raw, clean in norm_cases:
            run_write(
                "UPDATE products SET main_group=%s WHERE main_group=%s AND main_group != %s",
                (clean, raw, clean)
            )
            total += 1
        st.cache_data.clear()
        st.success(f"✅ main_group normalised — {len(norm_cases)} rules applied. Refreshing...")
        st.rerun()
    except Exception as ex:
        st.error(f"Normalisation failed: {ex}")


def render_inventory_search():
    with st.spinner("Loading inventory..."):
        df = _load_inventory()

    if df.empty:
        st.warning("No inventory data found.")
        return

    total_products = df["product_id"].nunique()

    # ── Top action bar ────────────────────────────────────────────────────────
    tb1, tb2, tb3, tb4, tb5 = st.columns([1, 1, 1, 3, 1])

    with tb1:
        if st.button("📦 All", key="inv_btn_all", use_container_width=True,
                     help="Show all products"):
            for k in ["inv_mg","inv_cat","inv_brand","inv_lcat","inv_wear",
                      "inv_gender","inv_colour","inv_mat","inv_coat","inv_idx",
                      "inv_loc","inv_box","inv_kw","inv_scan_val"]:
                st.session_state.pop(k, None)
            st.session_state["inv_stock"] = "All"
            st.rerun()

    with tb2:
        if st.button("✅ In Stock", key="inv_btn_stock", use_container_width=True,
                     help="Show only products with qty > 0"):
            for k in ["inv_mg","inv_cat","inv_brand","inv_lcat","inv_wear",
                      "inv_gender","inv_colour","inv_mat","inv_coat","inv_idx",
                      "inv_loc","inv_box","inv_kw","inv_scan_val"]:
                st.session_state.pop(k, None)
            st.session_state["inv_stock"] = "In Stock"
            st.rerun()

    with tb3:
        if st.button("🔄 Refresh", key="inv_refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    with tb4:
        keyword = st.text_input(
            "Search",
            placeholder="Product · Brand · SKU · Barcode · Colour · Location · Power...",
            key="inv_kw",
            label_visibility="collapsed"
        )

    with tb5:
        if st.button("🔧 Fix DB", key="inv_fix_mg", use_container_width=True,
                     help="Normalise main_group casing in DB"):
            _normalise_main_groups()

    # ── Scanner — scan any barcode to jump directly to that item ─────────────
    sc1, sc2 = st.columns([5, 1])
    with sc1:
        _scan_typed = st.text_input(
            "📷 Scan barcode",
            placeholder="Scan product barcode / SKU / batch / item code → jumps directly to that row",
            key="inv_scanner_input",
            label_visibility="collapsed",
        ).strip().upper()
        if _scan_typed:
            st.session_state["inv_scan_val"] = _scan_typed
    with sc2:
        if st.button("✕ Clear scan", key="inv_scan_clear", use_container_width=True):
            st.session_state.pop("inv_scan_val", None)
            st.session_state.pop("inv_scanner_input", None)
            st.rerun()

    _scan_val = st.session_state.get("inv_scan_val", "")


    # ══════════════════════════════════════════════════════════════════════════
    # FILTER PANEL — flat, all dropdowns from DB, no expander needed to open
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown(
        "<div style='background:#f8fafc;border:1px solid #e2e8f0;"
        "border-radius:9px;padding:12px 16px;margin:6px 0 10px 0'>",
        unsafe_allow_html=True
    )
    st.markdown(
        "<span style='font-size:0.72rem;font-weight:700;color:#94a3b8;"
        "text-transform:uppercase;letter-spacing:.06em'>Filters</span>",
        unsafe_allow_html=True
    )

    # Row 1 — Product classification (from products table)
    r1c1, r1c2, r1c3, r1c4, r1c5, r1c6 = st.columns(6)

    main_groups = ["All"] + _opts(df, "main_group")
    sel_mg = r1c1.selectbox("Main Group", main_groups, key="inv_mg")

    # Category — filtered by main_group selection
    mg_df = df if sel_mg == "All" else df[df["main_group"] == sel_mg]
    categories = ["All"] + _opts(mg_df, "category")
    sel_cat = r1c2.selectbox("Category", categories, key="inv_cat")

    cat_df = mg_df if sel_cat == "All" else mg_df[mg_df["category"].str.lower() == sel_cat.lower()]

    brands = ["All"] + _opts(cat_df, "brand")
    sel_brand = r1c3.selectbox("Brand", brands, key="inv_brand")

    lens_cats = ["All"] + _opts(cat_df, "lens_category")
    sel_lcat = r1c4.selectbox("Lens Category", lens_cats, key="inv_lcat")

    wear_opts = ["All"] + _opts(cat_df, "wear_schedule")
    sel_wear = r1c5.selectbox("Wear Schedule", wear_opts, key="inv_wear")

    gender_opts = ["All"] + _opts(cat_df, "gender")
    sel_gender = r1c6.selectbox("Gender", gender_opts, key="inv_gender")

    # Row 2 — Physical attributes
    r2c1, r2c2, r2c3, r2c4, r2c5, r2c6 = st.columns(6)

    colours = ["All"] + _opts(cat_df, "colour")
    sel_colour = r2c1.selectbox("Colour", colours, key="inv_colour")

    mats = ["All"] + _opts(cat_df, "material")
    sel_mat = r2c2.selectbox("Material", mats, key="inv_mat")

    coatings = ["All"] + _opts(cat_df, "coating_type")
    sel_coat = r2c3.selectbox("Coating", coatings, key="inv_coat")

    indices = ["All"] + _opts(cat_df, "lens_index")
    sel_idx = r2c4.selectbox("Lens Index", indices, key="inv_idx")

    locs = ["All"] + _opts(df, "location")
    sel_loc = r2c5.selectbox("Location / Box", locs, key="inv_loc")

    stock_filter = r2c6.radio("Stock", ["In Stock", "All"], key="inv_stock", horizontal=True)

    # Row 3 — Price range + box size
    r3c1, r3c2, r3c3 = st.columns([3, 3, 2])
    mrp_vals = df["mrp"][df["mrp"] > 0]
    if not mrp_vals.empty:
        mrp_min, mrp_max = int(mrp_vals.min()), int(mrp_vals.max())
        with r3c1:
            mrp_range = st.slider(
                "MRP Range ₹",
                min_value=mrp_min, max_value=mrp_max,
                value=(mrp_min, mrp_max), key="inv_mrp"
            )
    else:
        mrp_range = (0, 9999999)

    box_sizes = ["All"] + [str(int(x)) for x in sorted(df["box_size"].dropna().unique()) if x > 0]
    with r3c2:
        sel_box = st.selectbox("Box Size / Pack", box_sizes, key="inv_box",
                               help="e.g. 3 = 3-pack contact lenses, 6 = 6-pack")

    with r3c3:
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        if st.button("🗑️ Clear All Filters", key="inv_clear", use_container_width=True):
            for k in ["inv_mg","inv_cat","inv_brand","inv_lcat","inv_wear","inv_gender",
                      "inv_colour","inv_mat","inv_coat","inv_idx","inv_loc","inv_box",
                      "inv_stock","inv_kw"]:
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)

    # ══════════════════════════════════════════════════════════════════════════
    # APPLY FILTERS
    # ══════════════════════════════════════════════════════════════════════════
    result = df.copy()

    # ── Direct barcode scan — exact match, jumps straight to that row ────────
    if _scan_val:
        sv = _scan_val.upper().strip()
        scan_mask = (
            (result["batch_no"].str.upper().str.strip()        == sv) |
            (result["barcode"].str.upper().str.strip()         == sv) |
            (result["item_code"].str.upper().str.strip()       == sv) |
            (result["product_barcode"].str.upper().str.strip() == sv)
        )
        scan_result = result[scan_mask]
        if not scan_result.empty:
            st.success(
                f"✅ Barcode **{_scan_val}** → "
                f"**{scan_result.iloc[0]['product_name']}** | "
                f"SKU: {scan_result.iloc[0]['batch_no']} | "
                f"📍 {scan_result.iloc[0]['location']} | "
                f"Qty: {int(scan_result.iloc[0]['qty'])} | "
                f"₹{scan_result.iloc[0]['mrp']:.0f}"
            )
            result = scan_result
        else:
            st.warning(f"⚠️ Barcode **{_scan_val}** not found — showing all results")

    # Keyword — searches all text fields including barcode/item_code
    if keyword:
        kw = keyword.lower().strip()
        mask = (
            result["product_name"].str.lower().str.contains(kw, na=False) |
            result["brand"].str.lower().str.contains(kw, na=False) |
            result["batch_no"].str.lower().str.contains(kw, na=False) |
            result["barcode"].str.lower().str.contains(kw, na=False) |
            result["item_code"].str.lower().str.contains(kw, na=False) |
            result["colour"].str.lower().str.contains(kw, na=False) |
            result["location"].str.lower().str.contains(kw, na=False) |
            result["category"].str.lower().str.contains(kw, na=False) |
            result["main_group"].str.lower().str.contains(kw, na=False) |
            result["material"].str.lower().str.contains(kw, na=False) |
            result["sph"].str.contains(kw, na=False) |
            result["cyl"].str.contains(kw, na=False) |
            result["hsn_code"].str.lower().str.contains(kw, na=False)
        )
        result = result[mask]

    if sel_mg    != "All": result = result[result["main_group"] == sel_mg]
    if sel_cat   != "All": result = result[result["category"].str.lower() == sel_cat.lower()]
    if sel_brand != "All": result = result[result["brand"] == sel_brand]
    if sel_lcat  != "All": result = result[result["lens_category"] == sel_lcat]
    if sel_wear  != "All": result = result[result["wear_schedule"] == sel_wear]
    if sel_gender!= "All": result = result[result["gender"] == sel_gender]
    if sel_colour!= "All": result = result[result["colour"].str.strip().str.lower() == sel_colour.lower()]
    if sel_mat   != "All": result = result[result["material"] == sel_mat]
    if sel_coat  != "All": result = result[result["coating_type"] == sel_coat]
    if sel_idx   != "All": result = result[result["lens_index"] == sel_idx]
    if sel_loc   != "All": result = result[result["location"] == sel_loc]
    if sel_box   != "All": result = result[result["box_size"] == float(sel_box)]
    if stock_filter == "In Stock": result = result[result["qty"] > 0]
    result = result[(result["mrp"] >= mrp_range[0]) & (result["mrp"] <= mrp_range[1])]

    # ══════════════════════════════════════════════════════════════════════════
    # RESULTS HEADER — summary metrics
    # ══════════════════════════════════════════════════════════════════════════
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Results", len(result))
    m2.metric("Unique Products", result["product_id"].nunique())
    m3.metric("Total Qty", f"{int(result['qty'].sum()):,}")
    m4.metric("Avg MRP", f"₹{result['mrp'][result['mrp']>0].mean():.0f}" if (result["mrp"] > 0).any() else "—")
    m5.metric("Locations", result["location"][result["location"] != ""].nunique())

    if result.empty:
        st.info("No results match your filters. Try clearing some filters.")
        return

    st.markdown("---")

    # ── Category summary bar ──────────────────────────────────────────────────
    cat_summary = (
        result.groupby(["main_group", "category"])
        .agg(SKUs=("batch_no", "nunique"), Qty=("qty", "sum"))
        .reset_index()
        .sort_values("Qty", ascending=False)
    )
    if len(cat_summary) > 1:
        badges = ""
        for _, row in cat_summary.head(6).iterrows():
            grp = row["main_group"] or row["category"]
            badges += (
                f"<span style='background:#e0f2fe;color:#0369a1;padding:3px 10px;"
                f"border-radius:20px;font-size:0.75rem;font-weight:600;margin:2px'>"
                f"{grp} · {int(row['SKUs'])} SKUs · {int(row['Qty'])} pcs</span>"
            )
        st.markdown(f"<div style='margin-bottom:8px'>{badges}</div>",
                    unsafe_allow_html=True)

    # ══════════════════════════════════════════════════════════════════════════
    # RESULTS TABLE — full flat view, no tabs
    # ══════════════════════════════════════════════════════════════════════════
    # Dynamically choose which columns to show
    has_power    = (result["sph"].str.strip() != "").any()
    has_location = (result["location"].str.strip() != "").any()
    has_expiry   = (result["expiry_date"].str.strip() != "").any()
    has_index    = (result["lens_index"].str.strip() != "").any()
    has_coating  = (result["coating_type"].str.strip() != "").any()
    has_wear     = (result["wear_schedule"].str.strip() != "").any()
    has_box      = (result["box_size"] > 1).any()

    display_cols = ["product_name", "brand", "main_group", "category"]
    if has_wear:     display_cols.append("wear_schedule")
    display_cols += ["colour", "material"]
    if has_index:    display_cols.append("lens_index")
    if has_coating:  display_cols.append("coating_type")
    if has_location: display_cols.append("location")
    display_cols.append("batch_no")
    if has_box:      display_cols.append("box_size")
    if has_power:    display_cols += ["sph", "cyl", "axis", "add_power", "eye_side"]
    if has_expiry:   display_cols.append("expiry_date")
    display_cols   += ["qty", "purchase_rate", "selling_price", "mrp", "gst_percent"]

    display_cols = [c for c in display_cols if c in result.columns]

    col_config = {
        "product_name":  st.column_config.TextColumn("Product",         width="large"),
        "brand":         st.column_config.TextColumn("Brand"),
        "main_group":    st.column_config.TextColumn("Group"),
        "category":      st.column_config.TextColumn("Category"),
        "wear_schedule": st.column_config.TextColumn("Wear"),
        "colour":        st.column_config.TextColumn("Colour"),
        "material":      st.column_config.TextColumn("Material"),
        "lens_index":    st.column_config.TextColumn("Index"),
        "coating_type":  st.column_config.TextColumn("Coating"),
        "location":      st.column_config.TextColumn("Location"),
        "batch_no":      st.column_config.TextColumn("SKU/Batch"),
        "box_size":      st.column_config.NumberColumn("Box/Pack", format="%d"),
        "sph":           st.column_config.TextColumn("SPH"),
        "cyl":           st.column_config.TextColumn("CYL"),
        "axis":          st.column_config.TextColumn("AXIS"),
        "add_power":     st.column_config.TextColumn("ADD"),
        "eye_side":      st.column_config.TextColumn("Eye"),
        "expiry_date":   st.column_config.TextColumn("Expiry"),
        "qty":           st.column_config.NumberColumn("Qty",           format="%d"),
        "purchase_rate": st.column_config.NumberColumn("Cost ₹",        format="₹%.0f"),
        "selling_price": st.column_config.NumberColumn("Selling ₹",     format="₹%.0f"),
        "mrp":           st.column_config.NumberColumn("MRP ₹",         format="₹%.0f"),
        "gst_percent":   st.column_config.NumberColumn("GST %",         format="%.0f%%"),
    }

    st.dataframe(
        result[display_cols].sort_values(["main_group", "qty"], ascending=[True, False]),
        use_container_width=True,
        hide_index=True,
        column_config=col_config,
        height=min(600, max(300, len(result) * 36 + 40)),
    )

    # ── Actions bar: Export + Print ──────────────────────────────────────────
    dc1, dc2, dc3 = st.columns([1, 1, 3])
    with dc1:
        csv = result[display_cols].to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Export CSV",
            data=csv,
            file_name="inventory_results.csv",
            mime="text/csv",
            key="inv_csv"
        )
    with dc2:
        if st.button("🖨️ Print Labels", key="inv_print_open", use_container_width=True,
                     help="Print MRP stickers for selected results"):
            st.session_state["inv_show_print"] = not st.session_state.get("inv_show_print", False)

    # ── Print panel (toggled) ─────────────────────────────────────────────────
    if st.session_state.get("inv_show_print"):
        # Build items list — only rows that have a batch_no (scannable)
        printable = result[result["batch_no"].astype(str).str.strip() != ""].copy()
        if printable.empty:
            st.warning("No items with batch/SKU codes in current results — nothing to print.")
        else:
            print_items = []
            for _, row in printable.head(50).iterrows():
                print_items.append({
                    "code":         str(row.get("batch_no","")).strip(),
                    "product_name": str(row.get("product_name","")),
                    "mrp":          float(row.get("mrp",0)),
                    "location":     str(row.get("location","")),
                })
            from modules.printing.label_print_ui import render_batch_print_ui
            render_batch_print_ui(print_items, key="inv_results")

    # ── Location drill-down (inline, no tab needed) ───────────────────────────
    if has_location:
        st.markdown("---")
        st.markdown(
            "<span style='font-size:0.78rem;font-weight:700;color:#64748b;"
            "text-transform:uppercase;letter-spacing:.05em'>📦 Frames by Box / Location</span>",
            unsafe_allow_html=True
        )
        loc_grouped = (
            result[result["location"] != ""]
            .groupby("location")
            .agg(
                SKUs=("batch_no","nunique"),
                Qty=("qty","sum"),
                Products=("product_name", lambda x: " · ".join(x.astype(str).unique()[:3])
                           + (" ..." if x.nunique() > 3 else ""))
            )
            .reset_index()
            .sort_values("location")
        )
        lc1, lc2 = st.columns([1, 2])
        with lc1:
            st.dataframe(
                loc_grouped, use_container_width=True, hide_index=True,
                height=300,
                column_config={
                    "location": st.column_config.TextColumn("Box/Location"),
                    "SKUs":     st.column_config.NumberColumn("SKUs"),
                    "Qty":      st.column_config.NumberColumn("Total Qty"),
                    "Products": st.column_config.TextColumn("Products"),
                }
            )
        with lc2:
            drill = st.selectbox("📦 View frames in box", ["—"] + loc_grouped["location"].tolist(),
                                 key="inv_drill")
            if drill != "—":
                sub = result[result["location"] == drill]
                st.caption(f"{len(sub)} frame(s) in box **{drill}**")
                sub_cols = [c for c in ["product_name","brand","colour","batch_no",
                                        "qty","selling_price","mrp"] if c in sub.columns]
                st.dataframe(sub[sub_cols], use_container_width=True, hide_index=True,
                             column_config={
                                 "mrp":           st.column_config.NumberColumn("MRP ₹",  format="₹%.0f"),
                                 "selling_price": st.column_config.NumberColumn("Sell ₹", format="₹%.0f"),
                                 "qty":           st.column_config.NumberColumn("Qty"),
                             })
