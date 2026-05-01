import streamlit as st
try:
    from modules.core.kb_helpers import autofocus_scan, enter_to_submit
except ImportError:
    def autofocus_scan(*a, **k): pass
    def enter_to_submit(): pass
import pandas as pd
from modules.sql_adapter import execute_query


# --------------------------------------------------
# REFERENCE PRICE LOOKUP — for out-of-stock products
# --------------------------------------------------

def get_reference_price_for_product(product_id: str) -> dict:
    """
    When a product has no current stock for a specific power/batch,
    fetch all batches from inventory_stock (including exhausted) to
    get a price reference for punching a vendor/order-to-supply line.

    Returns dict with keys: batches, max_mrp, max_selling_price, has_history
    Each batch: {batch_no, mrp, selling_price, purchase_rate, expiry_date, quantity}
    """
    if not product_id:
        return {"batches": [], "max_mrp": 0.0, "max_selling_price": 0.0, "has_history": False}
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                batch_no,
                COALESCE(mrp, 0)           AS mrp,
                COALESCE(selling_price, 0) AS selling_price,
                COALESCE(purchase_rate, 0) AS purchase_rate,
                COALESCE(quantity, 0)      AS quantity,
                expiry_date::text          AS expiry_date
            FROM inventory_stock
            WHERE product_id = %(pid)s::uuid
              AND COALESCE(is_active, TRUE) = TRUE
              AND (mrp > 0 OR selling_price > 0)
            ORDER BY created_at DESC
            LIMIT 10
        """, {"pid": str(product_id)}) or []

        if not rows:
            return {"batches": [], "max_mrp": 0.0, "max_selling_price": 0.0, "has_history": False}

        max_mrp = max(float(r.get("mrp") or 0) for r in rows)
        max_sp  = max(float(r.get("selling_price") or 0) for r in rows)
        return {
            "batches":           rows,
            "max_mrp":           max_mrp,
            "max_selling_price": max_sp,
            "has_history":       True,
        }
    except Exception:
        return {"batches": [], "max_mrp": 0.0, "max_selling_price": 0.0, "has_history": False}



def is_lens_category(category: str) -> bool:
    if not category:
        return False

    category = str(category).lower()

    lens_keywords = [
        "lens",
        "spectacle",
        "single vision",
        "bifocal",
        "progressive",
        "cr",
        "poly",
        "glass",
    ]

    return any(k in category for k in lens_keywords)


def is_contact_category(category: str) -> bool:
    if not category:
        return False

    category = str(category).lower()

    contact_keywords = [
        "contact",
        "contact lens",
        "soft lens",
        "cl lens",
    ]

    return any(k in category for k in contact_keywords)


def is_ophthalmic_category(category: str) -> bool:
    """True for spectacle / ophthalmic lenses (NOT contact lenses)."""
    if not category: return False
    c = str(category).lower()
    if "contact" in c: return False
    return any(k in c for k in [
        "ophthalmic", "spectacle", "single vision", "sv lens", "sv rx",
        "sv stock", "progressive", "bifocal", "reading", "rx lens",
        "stock sv", "stock lens", "photo", "photochromic",
        "office lens", "occupational", "degressive",
    ])


def is_frame_category(category: str) -> bool:
    """Frames and sunglasses — need SKU-level selection for billing."""
    if not category:
        return False
    return str(category).lower() in ("frames", "frame", "sunglasses")


# --------------------------------------------------
# SKU LOOKUP — for frames/scanners
# --------------------------------------------------

def _fetch_frame_skus(product_id: str) -> list:
    """Fetch all active SKUs for a frame product from inventory_stock."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                batch_no                            AS sku,
                COALESCE(quantity, 0)               AS qty,
                COALESCE(mrp, 0)                    AS mrp,
                COALESCE(selling_price, 0)          AS selling_price,
                COALESCE(purchase_rate, 0)          AS purchase_rate,
                COALESCE(location, '')              AS location,
                COALESCE(colour_mix, '')            AS colour_mix,
                COALESCE(frame_group, '')           AS frame_group
            FROM inventory_stock
            WHERE product_id = %s
              AND COALESCE(is_active, true) = true
              AND COALESCE(quantity, 0) > 0
            ORDER BY batch_no
        """, (product_id,))
        return rows or []
    except Exception:
        return []


def lookup_sku(barcode: str) -> dict:
    """
    Resolve a Barcode (scanned or typed) to a product + stock row.
    Search order:
      1. inventory_stock.batch_no  — frames, CLENS, OPHLENS, SOL (has quantity + price)
      2. products.barcode         — services, fitting, colouring (no stock row needed)
    """
    if not barcode or not barcode.strip():
        return None
    sku = barcode.strip().upper()

    try:
        from modules.sql_adapter import run_query

        # Common SELECT fragment for inventory_stock lookups
        _stock_select = """
            SELECT
                p.id::text                              AS product_id,
                p.product_name,
                COALESCE(p.brand,'')                    AS brand,
                COALESCE(p.main_group,'')               AS main_group,
                COALESCE(p.category,'')                 AS category,
                COALESCE(p.gst_percent,0)               AS gst_percent,
                COALESCE(p.unit,'PCS')                  AS unit,
                COALESCE(p.box_size,1)                  AS box_size,
                COALESCE(p.allow_loose,false)           AS allow_loose,
                s.batch_no                              AS batch_no,
                COALESCE(s.quantity, 0)                 AS available_qty,
                COALESCE(s.mrp, s.selling_price, 0)     AS mrp,
                COALESCE(s.selling_price, s.mrp, 0)     AS selling_price,
                COALESCE(s.purchase_rate,0)             AS purchase_rate,
                COALESCE(s.location,'')                 AS location,
                COALESCE(s.colour_mix,'')               AS colour_mix,
                COALESCE(s.frame_group,'')              AS frame_group,
                COALESCE(s.item_code,'')                AS item_code,
                COALESCE(s.sph::text,'')                AS sph,
                COALESCE(s.cyl::text,'')                AS cyl,
                COALESCE(s.axis::text,'')               AS axis,
                COALESCE(s.add_power::text,'')          AS add_power,
                COALESCE(s.eye_side,'')                 AS eye_side,
                COALESCE(s.expiry_date::text,'')        AS expiry_date
            FROM inventory_stock s
            JOIN products p ON p.id = s.product_id
        """

        # ── Search 1a: inventory_stock by barcode (FIFO — oldest expiry first) ──
        # Product barcode encodes product+power — one scan fills everything
        rows = run_query(
            _stock_select +
            """WHERE UPPER(TRIM(COALESCE(s.barcode,''))) = %s
                 AND COALESCE(s.quantity,0) > 0
               ORDER BY s.expiry_date ASC NULLS LAST
               LIMIT 1""",
            (sku,)
        )
        if rows:
            r = dict(rows[0])
            r["_resolved_by"] = "barcode"
            return r

        # ── Search 1b: inventory_stock by item_code (Tally alias) ──────────────────────
        rows = run_query(
            _stock_select +
            """WHERE UPPER(TRIM(COALESCE(s.item_code,''))) = %s
                 AND COALESCE(s.quantity,0) > 0
               ORDER BY s.expiry_date ASC NULLS LAST
               LIMIT 1""",
            (sku,)
        )
        if rows:
            r = dict(rows[0])
            r["_resolved_by"] = "item_code"
            return r

        # ── Search 1c: inventory_stock by batch_no (frame SKU, lot barcode) ────────────
        rows = run_query(
            _stock_select +
            "WHERE UPPER(TRIM(s.batch_no)) = %s LIMIT 1",
            (sku,)
        )
        if rows:
            r = dict(rows[0])
            r["_resolved_by"] = "batch_no"
            return r

        # ── Search 2: products.barcode — services, accessories ──────────────
        prod_rows = run_query("""
            SELECT
                id::text                                AS product_id,
                product_name,
                COALESCE(brand,'')                      AS brand,
                COALESCE(main_group,'')                 AS main_group,
                COALESCE(category,'')                   AS category,
                COALESCE(gst_percent,0)                 AS gst_percent,
                COALESCE(unit,'PCS')                    AS unit,
                COALESCE(box_size,1)                    AS box_size,
                COALESCE(allow_loose,false)             AS allow_loose,
                barcode                                AS batch_no,
                0                                       AS available_qty,
                0                                       AS mrp,
                0                                       AS selling_price,
                0                                       AS purchase_rate,
                ''                                      AS location,
                ''                                      AS colour_mix,
                ''                                      AS frame_group
            FROM products
            WHERE UPPER(TRIM(COALESCE(barcode,''))) = %s
              AND COALESCE(is_active, true) = true
            LIMIT 1
        """, (sku,))

        return prod_rows[0] if prod_rows else None

    except Exception as _ex:
        import streamlit as _st
        _st.error(f"SKU lookup error: {_ex}")
        return None


# --------------------------------------------------
# MAIN UI
# --------------------------------------------------

def _oph_indices_for_product(product_id: str) -> list:
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT DISTINCT index_value::text AS idx
            FROM ophthalmic_lens_specs
            WHERE product_id=%(pid)s AND is_active=TRUE AND wlp_per_pair IS NOT NULL
            ORDER BY index_value
        """, {"pid": product_id}) or []
        return [r["idx"] for r in rows if r.get("idx")]
    except Exception:
        return []


def _oph_coatings_for_product(product_id: str, index_value: str) -> list:
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT DISTINCT coating
            FROM ophthalmic_lens_specs
            WHERE product_id=%(pid)s AND index_value=%(idx)s::numeric
              AND is_active=TRUE AND wlp_per_pair IS NOT NULL
            ORDER BY coating
        """, {"pid": product_id, "idx": index_value}) or []
        return [r["coating"] for r in rows if r.get("coating")]
    except Exception:
        return []


def _oph_spec_price(product_id: str, index_value: str, coating: str,
                    treatment: str = "Clear", order_type: str = "RETAIL") -> dict:
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT wlp_per_pair, srp_per_pair, purchase_rate
            FROM ophthalmic_lens_specs
            WHERE product_id=%(pid)s AND index_value=%(idx)s::numeric
              AND coating=%(coat)s AND COALESCE(treatment,'Clear')=%(treat)s
              AND is_active=TRUE LIMIT 1
        """, {"pid": product_id, "idx": index_value,
               "coat": coating, "treat": treatment or "Clear"}) or []
        if not rows:
            return {"wlp":0,"srp":0,"purchase":0,"selling":0,"found":False}
        r = rows[0]
        wlp = float(r.get("wlp_per_pair") or 0)
        srp = float(r.get("srp_per_pair")  or 0)
        cst = float(r.get("purchase_rate") or 0)
        sel = srp if order_type == "RETAIL" else wlp
        return {"wlp":wlp,"srp":srp,"purchase":cst,"selling":sel,"found":True}
    except Exception:
        return {"wlp":0,"srp":0,"purchase":0,"selling":0,"found":False}


def render_product_selector():

    st.subheader("🔍 Product Selection")

    # ── Scanner / SKU quick entry ─────────────────────────────────────────────
    # Use session_state to persist typed value across rerenders
    scanner_col, clear_col = st.columns([3, 1])
    with scanner_col:
        autofocus_scan("Scan")
        typed = st.text_input(
            "📷 Scan SKU / Barcode",
            placeholder="Scan or type SKU → press Enter",
            key="ps_scanner_input",
            label_visibility="collapsed",
        )
        if typed and typed.strip():
            st.session_state["ps_scanner_val"] = typed.strip().upper()

    with clear_col:
        if st.button("✕ Clear", key="ps_scanner_clear", use_container_width=True):
            st.session_state.pop("ps_scanner_val", None)
            st.session_state.pop("ps_scanner_input", None)
            st.rerun()

    scanned_sku = st.session_state.get("ps_scanner_val", "")

    if scanned_sku:
        hit = lookup_sku(scanned_sku)
        if hit:
            mg        = str(hit.get("main_group") or "")
            is_frame  = is_frame_category(mg)
            is_lens   = is_lens_category(mg)
            is_contact = is_contact_category(mg)
            sku_val   = str(hit.get("batch_no") or "")
            # Build display line — include power if present (contact/ophthalmic lens)
            _parts = [f"✅ {hit['product_name']}"]
            if hit.get('brand'):        _parts.append(hit['brand'])
            if hit.get('sph'):          _parts.append(f"SPH {hit['sph']}")
            if hit.get('cyl'):          _parts.append(f"CYL {hit['cyl']}")
            if hit.get('axis'):         _parts.append(f"AX {hit['axis']}")
            if hit.get('add_power'):    _parts.append(f"ADD {hit['add_power']}")
            if hit.get('eye_side'):     _parts.append(f"👁 {hit['eye_side']}")
            _mrp = float(hit.get('mrp') or 0)
            if _mrp:                    _parts.append(f"₹{_mrp:.0f}")
            if hit.get('batch_no'):     _parts.append(f"Batch:{hit['batch_no']}")
            if hit.get('expiry_date'):  _parts.append(f"Exp:{hit['expiry_date'][:7]}")
            if hit.get('location'):     _parts.append(f"📍{hit['location']}")
            st.success(" | ".join(_parts))
            # Normalise product_row to match what retail_punching expects
            product_row = {
                "product_id":    str(hit.get("product_id") or ""),
                "product_name":  hit.get("product_name",""),
                "brand":         hit.get("brand",""),
                "main_group":    mg,
                "category":      hit.get("category",""),
                "gst_percent":   float(hit.get("gst_percent") or 0),
                "unit":          hit.get("unit","PCS"),
                "box_size":      int(hit.get("box_size") or 1),
                "allow_loose":   hit.get("allow_loose", False),
                "mrp":           float(hit.get("mrp") or 0),
                "selling_price": float(hit.get("selling_price") or 0),
                "purchase_rate": float(hit.get("purchase_rate") or 0),
                "batch_no":      sku_val,
                "location":      hit.get("location",""),
                "available_qty": int(hit.get("available_qty") or 0),
                "stock_status":  "READY" if int(hit.get("available_qty") or 0) > 0 else "OUT",
            }
            return {
                "product_row":   product_row,
                "is_lens":       is_lens,
                "is_contact":    is_contact,
                "is_frame":      is_frame,
                "stock_status":  product_row["stock_status"],
                "available_qty": product_row["available_qty"],
                "selected_sku":  sku_val,
            }
        else:
            st.warning(
                f"⚠️ SKU **{scanned_sku}** not found in inventory. "
                "Check Barcode or use the product dropdown below."
            )

    st.markdown("---")

    # ----------------------------------
    # Load ALL Active Products + Stock
    # ----------------------------------

    sql = """
        SELECT
            p.id AS product_id,
            p.product_name,
            p.brand,
            p.main_group,
            p.category,
            p.category AS type,
            p.material,
            p.colour,
            p.coating_type,
            p.index_value AS lens_index,
            p.lens_category,
            COALESCE(p.unit,'PCS') AS unit,
            COALESCE(p.box_size,1) AS box_size,
            COALESCE(p.allow_loose,false) AS allow_loose,
            COALESCE(p.gst_percent,0) AS gst_percent,

            -- Stock qty
            COALESCE(SUM(i.quantity),0) AS available_qty,

            -- Prices: pulled from inventory_stock (where actual per-batch prices live)
            -- RETAIL  → mrp   (counter / sticker price)
            -- WHOLESALE → selling_price  (trade price)
            -- PURCHASE → purchase_rate  (cost)
            COALESCE(MAX(i.mrp), MAX(i.selling_price), 0)           AS mrp,
            COALESCE(MAX(i.selling_price), MAX(i.mrp), 0)           AS selling_price,
            COALESCE(MAX(i.purchase_rate), 0)                       AS purchase_rate,

            CASE
                WHEN COALESCE(SUM(i.quantity),0) > 0 THEN 'READY'
                ELSE 'READY_TO_PROCESS'
            END AS stock_status

        FROM products p

        LEFT JOIN inventory_stock i
            ON p.id = i.product_id
           AND COALESCE(i.is_active, true) = true

        WHERE COALESCE(p.is_active,true)=true

        GROUP BY
            p.id, p.product_name, p.brand, p.main_group,
            p.category, p.material, p.colour,
            p.coating_type, p.index_value,
            p.lens_category, p.unit, p.gst_percent
    """

    products_df = execute_query(sql, "products_all")

    if products_df.empty:
        st.warning("⚠️ No products found")
        return None

    # ----------------------------------
    # Session Init
    # ----------------------------------

    for k in [
        "ps_main_group", "ps_brand", "ps_material",
        "ps_type", "ps_lens_index", "ps_coating_type",
        "ps_colour", "ps_unit", "ps_product"
    ]:
        st.session_state.setdefault(k, "")

    # ----------------------------------
    # Helpers
    # ----------------------------------

    def clear_product():
        st.session_state["ps_product"] = ""

    def on_main_group_change():
        for k in [
            "ps_brand","ps_material","ps_type",
            "ps_lens_index","ps_coating_type",
            "ps_colour","ps_unit","ps_product"
        ]:
            st.session_state[k] = ""

    # ----------------------------------
    # FILTER ROW 1
    # ----------------------------------

    c1, c2, c3, c4 = st.columns(4)

    with c1:
        st.selectbox(
            "Main Group",
            [""] + sorted(products_df["main_group"].dropna().astype(str).unique()),
            key="ps_main_group",
            on_change=on_main_group_change
        )

    with c2:
        st.selectbox(
            "Brand",
            [""] + sorted(products_df["brand"].dropna().astype(str).unique()),
            key="ps_brand",
            on_change=clear_product
        )

    with c3:
        st.selectbox(
            "Material",
            [""] + sorted(products_df["material"].dropna().astype(str).unique()),
            key="ps_material",
            on_change=clear_product
        )

    with c4:
        st.selectbox(
            "Type",
            [""] + sorted(products_df["type"].dropna().astype(str).unique()),
            key="ps_type",
            on_change=clear_product
        )

    # ----------------------------------
    # FILTER ROW 2
    # ----------------------------------

    c5, c6, c7, c8 = st.columns(4)

    with c5:
        _mg_sel = st.session_state.get("ps_main_group","")
        _is_oph_filter = is_ophthalmic_category(_mg_sel) if _mg_sel else False
        if not _is_oph_filter:
            st.selectbox(
                "Lens Index",
                [""] + sorted(products_df["lens_index"].dropna().astype(str).unique()),
                key="ps_lens_index", on_change=clear_product,
                help="Index selected after product for ophthalmic lenses"
            )
        else:
            st.session_state["ps_lens_index"] = ""
            st.markdown("**🔢 Index**"); st.caption("after product ↓")

    with c6:
        if not _is_oph_filter:
            st.selectbox(
                "Coating",
                [""] + sorted(products_df["coating_type"].dropna().astype(str).unique()),
                key="ps_coating_type", on_change=clear_product,
            )
        else:
            st.session_state["ps_coating_type"] = ""
            st.markdown("**🛡️ Coating**"); st.caption("after product ↓")

    with c7:
        st.selectbox(
            "Colour",
            [""] + sorted(products_df["colour"].dropna().astype(str).unique()),
            key="ps_colour",
            on_change=clear_product
        )

    with c8:
        st.selectbox(
            "Unit",
            [""] + sorted(products_df["unit"].dropna().astype(str).unique()),
            key="ps_unit",
            on_change=clear_product
        )

    # ----------------------------------
    # Apply Filters
    # ----------------------------------

    df = products_df.copy()

    for col, key in [
        ("main_group","ps_main_group"),
        ("brand","ps_brand"),
        ("material","ps_material"),
        ("type","ps_type"),
        ("lens_index","ps_lens_index"),
        ("coating_type","ps_coating_type"),
        ("colour","ps_colour"),
        ("unit","ps_unit"),
    ]:

        val = st.session_state.get(key)

        if val:
            df = df[df[col].astype(str) == str(val)]

    # ----------------------------------
    # Product Dropdown
    # ----------------------------------

    product_list = sorted(df["product_name"].dropna().astype(str).unique())

    st.selectbox(
        "Product *",
        [""] + product_list,
        key="ps_product"
    )

    sel = st.session_state.get("ps_product")

    if not sel:
        st.info("👆 Select product")
        return None

    # ----------------------------------
    # Selected Product
    # ----------------------------------

    row = df[df["product_name"] == sel].iloc[0].to_dict()

    category   = str(row.get("main_group") or "")
    is_lens    = is_lens_category(category)
    is_contact = is_contact_category(category)
    is_ophthal = is_ophthalmic_category(category) and not is_contact

    # ── Ophthalmic: dynamic Index + Coating from ophthalmic_lens_specs ───────
    _oph_spec_result = None
    if is_ophthal:
        pid = str(row.get("product_id") or "")
        try:
            from modules.ophthalmic_billing import (
                render_ophthalmic_selector as _oph_sel_fn,
                render_availability_grid   as _oph_grid_fn,
            )
        except ImportError:
            _oph_sel_fn = None; _oph_grid_fn = None

        if _oph_sel_fn:
            _order_type = (st.session_state.get("_current_order_type")
                           or st.session_state.get("pricing_mode","RETAIL"))
            _oph_spec_result = _oph_sel_fn(
                product_id   = pid,
                product_name = sel,
                rx_r         = st.session_state.get("retail_new_rx_r")
                               or st.session_state.get("wholesale_rx_r", {}),
                rx_l         = st.session_state.get("retail_new_rx_l")
                               or st.session_state.get("wholesale_rx_l", {}),
                order_type   = _order_type,
                key_prefix   = f"ps_{pid[:8]}",
            )
            if _oph_spec_result and _oph_spec_result.get("complete"):
                # Store spec in session_state for fast-path in wholesale_punching
                st.session_state[f"_oph_spec_{pid}"] = _oph_spec_result
                row = dict(row)
                price = _oph_spec_result["price"]
                row["mrp"]           = price["srp"]  / 2  # per lens
                row["selling_price"] = price["selling"] / 2
                row["purchase_rate"] = price["purchase"] / 2
                row["lens_index"]    = _oph_spec_result["index"]
                row["coating_type"]  = _oph_spec_result["coating"]
                row["treatment"]     = _oph_spec_result["treatment"]
                row["_price_source"] = "ophthalmic_spec"

    # Stock Status Display
    status = row.get("stock_status", "READY_TO_PROCESS")
    qty = int(row.get("available_qty", 0))

    is_frame = is_frame_category(str(row.get("main_group") or ""))

    # ── Frame: show SKU picker (each SKU = one physical frame) ──────────────
    if is_frame:
        skus = _fetch_frame_skus(str(row["product_id"]))

        if not skus:
            st.warning(f"⚠️ {row['product_name']} — no stock available")
            return None

        # SKU scan / type box
        scanned = st.text_input(
            "📷 Scan or type SKU",
            placeholder="e.g. D10007",
            key="ps_frame_sku_scan"
        ).strip().upper()

        # If scanned SKU matches one of this product's SKUs — auto-select
        sku_match = None
        if scanned:
            sku_match = next((s for s in skus if s["sku"].upper() == scanned), None)
            if not sku_match:
                st.warning(f"SKU {scanned!r} not found in stock for this product")

        # SKU dropdown — label shows SKU + location + price
        sku_labels = [
            f"{s['sku']}  |  📍{s['location']}  |  ₹{s['mrp']:.0f}"
            + (f"  |  {s['colour_mix']}" if s.get("colour_mix") else "")
            + (f"  [{s['frame_group']}]" if s.get("frame_group") else "")
            for s in skus
        ]
        default_idx = 0
        if sku_match:
            default_idx = next((i for i, s in enumerate(skus) if s["sku"].upper() == scanned), 0)

        sel_label = st.selectbox(
            f"Select SKU ({len(skus)} in stock)",
            sku_labels,
            index=default_idx,
            key="ps_frame_sku_select"
        )
        sel_sku = skus[sku_labels.index(sel_label)]

        st.success(
            f"🕶️ {row['product_name']} | {row['brand']} | "
            f"SKU: {sel_sku['sku']} | 📍{sel_sku['location']} | ₹{sel_sku['mrp']:.0f}"
        )

        # Merge SKU-level prices into product row
        product_row = dict(row)
        product_row["mrp"]           = sel_sku["mrp"]
        product_row["selling_price"] = sel_sku["selling_price"]
        product_row["purchase_rate"] = sel_sku["purchase_rate"]
        product_row["batch_no"]      = sel_sku["sku"]
        product_row["available_qty"] = sel_sku["qty"]

        return {
            "product_row":  product_row,
            "is_lens":      False,
            "is_contact":   False,
            "is_frame":     True,
            "stock_status": "READY",
            "available_qty": sel_sku["qty"],
            "selected_sku": sel_sku["sku"],
        }

    # ── Other non-lens products (solutions, accessories etc.) ────────────────
    if not is_lens and not is_contact:
        if status == "READY":
            st.success(f"📦 {row['product_name']} | {row['brand']} | ✅ In Stock ({qty})")
        else:
            st.warning(f"📦 {row['product_name']} | {row['brand']} | ⚠️ To Order")

    # ── Out-of-stock: show batch price history as reference ──────────────────
    # When product has no current stock, look up all batches in inventory_stock
    # (including exhausted ones) and let operator pick a reference MRP/price.
    # This ensures price is never ₹0 when punching a vendor/order-to-supply line.
    if status == "READY_TO_PROCESS" and not is_frame:
        pid = str(row.get("product_id") or "")
        _ref_batches = []
        if pid:
            try:
                from modules.sql_adapter import run_query as _rq_ps
                _ref_batches = _rq_ps("""
                    SELECT
                        batch_no,
                        COALESCE(mrp, 0)           AS mrp,
                        COALESCE(selling_price, 0) AS selling_price,
                        COALESCE(purchase_rate, 0) AS purchase_rate,
                        COALESCE(quantity, 0)      AS quantity,
                        expiry_date::text          AS expiry_date
                    FROM inventory_stock
                    WHERE product_id = %(pid)s::uuid
                      AND COALESCE(is_active, TRUE) = TRUE
                      AND (mrp > 0 OR selling_price > 0)
                    ORDER BY created_at DESC
                    LIMIT 10
                """, {"pid": pid}) or []
            except Exception:
                _ref_batches = []

        if _ref_batches:
            st.markdown(
                "<div style='background:#1a1200;border-left:3px solid #f59e0b;"
                "border-radius:0 8px 8px 0;padding:8px 14px;margin:6px 0;"
                "color:#fbbf24;font-size:0.8rem;font-weight:600'>"
                "⚠️ No current stock — select a reference price from previous batches"
                "</div>",
                unsafe_allow_html=True,
            )

            # Build label → batch map
            _batch_labels = []
            for _b in _ref_batches:
                _mrp  = float(_b.get("mrp") or 0)
                _sp   = float(_b.get("selling_price") or 0)
                _pr   = float(_b.get("purchase_rate") or 0)
                _bn   = _b.get("batch_no") or "—"
                _exp  = (str(_b.get("expiry_date") or "")[:7]) or ""
                _lbl  = f"Batch {_bn}"
                if _exp:
                    _lbl += f"  exp {_exp}"
                if _mrp:
                    _lbl += f"  MRP ₹{_mrp:,.2f}"
                # Show Trade/Cost only in wholesale — hide in retail
                _is_retail_mode = (
                    st.session_state.get("pricing_mode","RETAIL") == "RETAIL"
                    or st.session_state.get("_current_order_type","RETAIL") == "RETAIL"
                )
                if not _is_retail_mode:
                    if _sp and _sp != _mrp:
                        _lbl += f"  Trade ₹{_sp:,.2f}"
                    if _pr:
                        _lbl += f"  Cost ₹{_pr:,.2f}"
                _batch_labels.append(_lbl)

            _sel_batch_label = st.selectbox(
                "📦 Reference batch (price only — no stock allocated)",
                _batch_labels,
                key=f"ps_ref_batch_{pid}",
            )
            _sel_batch = _ref_batches[_batch_labels.index(_sel_batch_label)]
            _ref_mrp  = float(_sel_batch.get("mrp") or 0)
            _ref_sp   = float(_sel_batch.get("selling_price") or 0)
            _ref_pr   = float(_sel_batch.get("purchase_rate") or 0)

            # Allow manual override if needed
            _override_price = st.number_input(
                "💰 Override MRP (optional — leave 0 to use batch price)",
                min_value=0.0,
                value=0.0,
                step=0.50,
                format="%.2f",
                key=f"ps_price_override_{pid}",
                label_visibility="visible",
            )

            _final_mrp = _override_price if _override_price > 0 else _ref_mrp
            _final_sp  = _override_price if _override_price > 0 else _ref_sp

            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #3b82f6;"
                f"border-radius:8px;padding:8px 14px;margin-top:6px;"
                f"display:flex;gap:20px;align-items:center'>"
                f"<span style='color:#93c5fd;font-size:0.78rem'>Reference price:</span>"
                f"<span style='color:#60a5fa;font-weight:700'>MRP ₹{_final_mrp:,.2f}</span>"
                f"{'<span style=\"color:#94a3b8;font-size:0.75rem\">Trade ₹' + f'{_final_sp:,.2f}</span>' if (_final_sp and _final_sp != _final_mrp and not _is_retail_mode) else ''}"
                f"<span style='color:#475569;font-size:0.72rem;margin-left:auto'>"
                f"Route → VENDOR / Supplier Order</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

            # Inject reference price into the row before returning
            row = dict(row)
            row["mrp"]           = _final_mrp
            row["selling_price"] = _final_sp if _final_sp else _final_mrp
            row["purchase_rate"] = _ref_pr
            row["_price_source"] = "batch_reference"

        else:
            # No batch history — for ophthalmic, price comes from spec table (already set above)
            if is_ophthal and _oph_spec_result and _oph_spec_result.get("complete"):
                # Price already injected from ophthalmic_lens_specs — nothing needed
                row = dict(row)
                if row.get("_price_source") != "ophthalmic_spec":
                    # Ensure price is set from spec result
                    _sp = _oph_spec_result.get("price", {})
                    row["selling_price"] = float(_sp.get("selling") or _sp.get("wlp") or 0) / 2
                    row["mrp"]           = float(_sp.get("srp") or row["selling_price"] * 2) / 2
                    row["_price_source"] = "ophthalmic_spec"
            else:
                # Non-ophthalmic: show manual price entry
                st.markdown(
                    "<div style='background:#1a0a0a;border-left:3px solid #ef4444;"
                    "border-radius:0 8px 8px 0;padding:8px 14px;margin:6px 0;"
                    "color:#fca5a5;font-size:0.8rem;font-weight:600'>"
                    "🔴 No stock and no price history — enter price manually"
                    "</div>",
                    unsafe_allow_html=True,
                )
                _manual_price = st.number_input(
                    "💰 Enter MRP / Price ₹",
                    min_value=0.0, value=0.0, step=0.50, format="%.2f",
                    key=f"ps_manual_price_{pid}",
                    label_visibility="visible",
                )
                if _manual_price > 0:
                    row = dict(row)
                    row["mrp"]           = _manual_price
                    row["selling_price"] = _manual_price
                    row["_price_source"] = "manual_entry"
                else:
                    st.caption("⚠️ Price required to punch this item — enter MRP above")

    # ----------------------------------
    # Return to Retail
    # ----------------------------------

    return {
        "product_row":  row,
        "is_lens":      is_lens,
        "is_contact":   is_contact,
        "is_ophthal":   is_ophthal if 'is_ophthal' in dir() else False,
        "is_frame":     False,
        "stock_status": status,
        "available_qty": qty,
        "oph_spec":     _oph_spec_result if '_oph_spec_result' in dir() else None,
    }
