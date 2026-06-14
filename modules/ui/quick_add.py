"""
modules/ui/quick_add.py
========================
Quick Add — add one record at a time, Tally-style.

Rules enforced:
  1. Product-first — Frame/CL/Ophthalmic/Solution tabs require product to exist in master
     If not found → guided message: "Add product first in 📦 Product tab"
  2. Scan Code / Item Code unique — checked live before save, rejected with clear error
  3. Batch_no is a true batch/lot field for batch stock, not the frame scanning code

Save flow: fill → save → green summary → form clears → ready for next
"""

import uuid
import logging
import streamlit as st

logger = logging.getLogger(__name__)


# ── Tiny helpers ──────────────────────────────────────────────────────────────

def _gst_for_group(main_group: str) -> tuple:
    """
    Look up GST% and HSN for a main_group from main_groups master table.
    Returns (gst_percent, hsn_code) — falls back to (12, '') if not found.
    """
    if not main_group:
        return 12, ''
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT gst_percent, hsn_code FROM main_groups "
            "WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s)) LIMIT 1",
            (main_group,)
        ) or []
        if rows:
            return float(rows[0].get('gst_percent') or 12), str(rows[0].get('hsn_code') or '')
    except Exception:
        pass
    # Sensible defaults per group even if main_groups table is empty
    _defaults = {
        'frames':           (5,  '90030000'),
        'sunglasses':       (18, '90041000'),
        'ophthalmic lenses':(12, '90015000'),
        'contact lenses':   (12, '90015000'),
        'solution':         (18, '30049099'),
        'accessories':      (18, '90049090'),
        'service':          (18, '999319'),
    }
    gst, hsn = _defaults.get(main_group.lower().strip(), (12, ''))
    return gst, hsn


def render_party_name_warning(name: str, current_id: str = None):
    """
    Show live similar-party warning below any party name input.
    Call this immediately after the name text_input.
    current_id: pass existing party id to exclude self from results (edit mode).
    """
    if not name or len(name.strip()) < 3:
        return

    try:
        from modules.loaders.party_dedup import find_similar_parties, suggest_distinguishing_names
        similar = find_similar_parties(name.strip(), threshold=0.40)
        # Exclude self if editing
        if current_id:
            similar = [r for r in similar if r['id'] != current_id]
        if not similar:
            return

        exact   = [r for r in similar if r['conflict_type'] == 'EXACT']
        fuzzy   = [r for r in similar if r['conflict_type'] == 'SIMILAR']

        if exact:
            st.error(
                f"❌ **Exact match** — **{exact[0]['party_name']}** already exists "
                f"(Customer#: {exact[0].get('customer_no','—')} | "
                f"📞 {exact[0].get('mobile','—')} | 🏙️ {exact[0].get('city','—')}). "
                "Do NOT save — use existing record or add a distinguishing suffix."
            )

        if fuzzy:
            with st.expander(
                f"⚠️ {len(fuzzy)} similar name(s) found — click to review before saving",
                expanded=True
            ):
                for r in fuzzy:
                    pct = int(r['similarity'] * 100)
                    st.markdown(
                        f"**{r['party_name']}** ({pct}% similar) | "
                        f"📞 {r.get('mobile','—')} | "
                        f"🏙️ {r.get('city','—')} | "
                        f"GSTIN: {r.get('gstin','—') or '—'} | "
                        f"Customer#: {r.get('customer_no','—') or '—'}"
                    )

                st.markdown("**Suggested names to distinguish:**")
                suggestions = suggest_distinguishing_names(name.strip(), fuzzy)
                for s in suggestions:
                    col_use, col_desc = st.columns([2, 3])
                    col_use.code(s['name'])
                    col_desc.caption(s['reason'])

    except Exception:
        pass  # Never block entry due to similarity check failure


def _f(v, d=0.0):
    try: return float(v) if str(v).strip() not in ("","None") else d
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return d

def _i(v, d=0):
    try: return int(float(v)) if str(v).strip() not in ("","None") else d
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return d

def _saved(key, data):
    st.session_state[f"_qa_last_{key}"] = {k:v for k,v in data.items() if v}

def _show_summary(key):
    rec = st.session_state.get(f"_qa_last_{key}")
    if rec:
        parts = " · ".join(f"**{k}:** {v}" for k,v in rec.items())
        st.success(f"✅ Saved — {parts}")


# ── Uniqueness checks (live, before save) ─────────────────────────────────────
def _barcode_exists(barcode: str, table: str, exclude_id: str = None) -> bool:
    """Return True if barcode already used in given table."""
    if not barcode or not barcode.strip():
        return False
    try:
        from modules.sql_adapter import run_query
        sql = f"SELECT id FROM {table} WHERE UPPER(TRIM(COALESCE(barcode,'')))=UPPER(TRIM(%s))"
        params = [barcode.strip()]
        if exclude_id:
            sql += " AND id != %s"
            params.append(exclude_id)
        return bool(run_query(sql + " LIMIT 1", params))
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return False

def _item_code_exists(item_code: str, exclude_id: str = None) -> bool:
    if not item_code or not item_code.strip():
        return False
    try:
        from modules.sql_adapter import run_query
        sql = "SELECT id FROM inventory_stock WHERE UPPER(TRIM(COALESCE(item_code,'')))=UPPER(TRIM(%s))"
        params = [item_code.strip()]
        if exclude_id:
            sql += " AND id != %s"; params.append(exclude_id)
        return bool(run_query(sql + " LIMIT 1", params))
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return False

def _batch_exists(product_id: str, batch_no: str) -> bool:
    if not batch_no or not batch_no.strip():
        return False
    try:
        from modules.sql_adapter import run_query
        return bool(run_query(
            "SELECT id FROM inventory_stock WHERE product_id=%s AND UPPER(TRIM(batch_no))=UPPER(TRIM(%s)) LIMIT 1",
            [product_id, batch_no.strip()]
        ))
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return False

def _product_name_exists(name: str) -> bool:
    try:
        from modules.sql_adapter import run_query
        return bool(run_query(
            "SELECT id FROM products WHERE LOWER(TRIM(product_name))=LOWER(TRIM(%s)) LIMIT 1",
            [name.strip()]
        ))
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        return False


# ── Product picker helper — used by all stock tabs ───────────────────────────
def _pick_product(key: str, main_groups: list, label: str = "Product *") -> tuple:
    """
    Returns (product_name, product_id) or (None, None).
    Shows guidance if no products exist for the group.
    """
    try:
        from modules.sql_adapter import run_query
        placeholders = ",".join(["%s"] * len(main_groups))
        prods = run_query(f"""
            SELECT product_name, id::text AS id FROM products
            WHERE LOWER(COALESCE(main_group,'')) IN ({placeholders})
              AND COALESCE(is_active,true)=true
            ORDER BY product_name
        """, [g.lower() for g in main_groups]) or []
    except Exception as _e:
        logger.warning("Suppressed error: %s", _e)
        prods = []

    if not prods:
        st.warning(
            f"⚠️ No products found for this category. "
            f"**Add the product first** in the 📦 Product tab, then come back here."
        )
        return None, None

    names = [r["product_name"] for r in prods]
    id_map = {r["product_name"]: r["id"] for r in prods}

    sel = st.selectbox(label, ["— select product —"] + names, key=f"qa_prod_{key}")
    if sel == "— select product —":
        st.info("👆 Select a product from the list above to continue.")
        return None, None

    return sel, id_map[sel]


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════
def render_quick_add(default_tab: int = 0, render_id: str = "qa"):
    """
    default_tab: 0=Product, 1=Frame, 2=CL, 3=Oph, 4=Solution, 5=Blank, 6=Party, 7=Patient
    render_id: unique suffix for widget keys — use "sf" for Scan Frame page
    """
    _tab_idx = st.session_state.get("quick_add_tab", default_tab)

    st.markdown(
        "<div style='background:#0f172a;border-left:4px solid #4ade80;"
        "padding:10px 16px;border-radius:6px;margin-bottom:12px'>"
        "<b style='color:#4ade80;font-size:1rem'>➕ Quick Add</b>"
        "<span style='color:#94a3b8;font-size:0.78rem;margin-left:10px'>"
        "One record at a time · Scan Code / Item Code must be unique</span>"
        "</div>", unsafe_allow_html=True
    )

    TAB_NAMES = [
        "📦 Product", "🕶️ Frame", "👁️ Contact Lens",
        "🔍 Ophthalmic", "💊 Solution", "⬜ Blank",
        "🏢 Party", "🏥 Patient", "🪪 Print Cards"
    ]
    TAB_FNS = [
        _tab_product, _tab_frame, _tab_clens,
        _tab_ophlens, _tab_solution, _tab_blank,
        _tab_party, _tab_patient, _tab_print_cards
    ]

    tabs = st.tabs(TAB_NAMES)
    for i, (tab, fn) in enumerate(zip(tabs, TAB_FNS)):
        with tab:
            if fn is _tab_print_cards:
                fn(rid=render_id)
            else:
                fn()

    # Auto-scroll to default tab via JS
    if _tab_idx > 0:
        st.components.v1.html(
            f"<script>"
            f"var tabs = window.parent.document.querySelectorAll('button[role=tab]');"
            f"if(tabs[{_tab_idx}]) tabs[{_tab_idx}].click();"
            f"</script>",
            height=0
        )
        st.session_state.pop("quick_add_tab", None)


# ══════════════════════════════════════════════════════════════════════════════
# 📦 PRODUCT
# ══════════════════════════════════════════════════════════════════════════════
def _tab_product():
    st.markdown("#### 📦 Add / Edit Product Master")
    st.caption(
        "Add a product here first. Then use the other tabs to add stock/scan codes for it. "
        "Product Name and Barcode must be unique."
    )
    _show_summary("product")

    # Print button if last saved product had a barcode/MRP
    _last_print = st.session_state.get("_qa_last_product_print")
    if _last_print and _last_print.get("price",0) > 0:
        from modules.printing.label_print_ui import render_print_button
        render_print_button(
            code=_last_print["code"],
            price=_last_print["price"],
            label=f"🖨️ Print label for {_last_print['name']}",
            key_suffix="prod",
        )

    # ── Main Group selected OUTSIDE form so GST auto-fills on change ──────────
    _GST_OPTIONS = [0, 5, 18]
    _MG_OPTIONS  = ["Contact Lenses","Frames","Ophthalmic Lenses",
                    "Sunglasses","Solution","Service","Accessories"]

    # Only Main Group outside form — drives GST auto-load
    mg = st.selectbox("Main Group *", _MG_OPTIONS, key="qa_prod_mg")
    _auto_gst, _auto_hsn = _gst_for_group(mg)
    _gst_idx = _GST_OPTIONS.index(_auto_gst) if _auto_gst in _GST_OPTIONS else 2
    st.caption(
        f"ℹ️ GST auto-set to **{_auto_gst}%** from Main Groups master for *{mg}*. "
        "Override below if needed."
    )

    st.info(
        "**For frames with stickers already on them** — go to 🕶️ **Frame tab** directly. "
        "Scan the sticker there. The Product tab is for setting up the product name/brand once."
    )

    # ── Camera scanner for product barcode ───────────────────────────────────
    _PROD_BC_LABEL = "Product barcode scan"
    try:
        from modules.printing.camera_scanner import render_camera_scanner
        _prod_bc_scanned = st.text_input(
            _PROD_BC_LABEL,
            key="qa_prod_cam_input",
            placeholder="📷 Scan barcode or type here — auto-fills Barcode field in form below",
        ).strip()
        if _prod_bc_scanned:
            st.session_state["qa_prod_bc_val"] = _prod_bc_scanned.upper()
        render_camera_scanner(target_label=_PROD_BC_LABEL, height=280)
    except Exception as _cam_ex:
        st.caption(f"Camera scanner unavailable: {_cam_ex}")
        _prod_bc_scanned = ""

    _prod_bc_val = st.session_state.get("qa_prod_bc_val", "")
    if _prod_bc_val:
        st.success(f"✅ Barcode captured: **{_prod_bc_val}** — will fill form below")

    with st.form("qa_product", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name     = c1.text_input("Product Name *", placeholder="e.g. Butler 8305 Black")
        brand    = c2.text_input("Brand", placeholder="Parakh / Alcon")
        c3, c4 = st.columns(2)
        category = c3.text_input("Category", placeholder="Single Vision / Progressive")
        barcode  = c4.text_input("Product Barcode",
                                  value=_prod_bc_val,
                                  placeholder="Auto-filled from camera scan above")

        c6, c7, c8, c9, c10 = st.columns(5)
        gst      = c6.selectbox("GST %", _GST_OPTIONS, index=_gst_idx)
        hsn      = c7.text_input("HSN Code", placeholder="900150", value=_auto_hsn)
        unit     = c8.selectbox("Unit", ["PCS","BOX","PAIR","SET"])
        box_size = c9.number_input("Box Size", min_value=1, value=1)
        mrp_prod = c10.number_input(
            "MRP ₹ (print only)",
            min_value=0.0, step=0.50,
            help=(
                "For barcode sticker printing only — NOT saved to DB, "
                "NOT used in billing. "
                "To set the billing price, add stock in the 🕶️ Frame tab."
            )
        )

        st.caption(
            "ℹ️ **MRP ₹ (print only)** — entered above is used only for printing a barcode sticker. "
            "It is NOT saved and does NOT affect billing. "
            "To set the billing price, use the 🕶️ **Frame** tab to add stock with MRP."
        )
        force_new = st.checkbox(
            "Create even if a similar product name already exists",
            value=False,
            help="Tick only if this is genuinely a different product. This prevents accidental misspelled duplicates.",
        )
        submitted = st.form_submit_button("💾 Save Product", type="primary", use_container_width=True)

    if submitted:
        import re as _re
        errors = []
        if not name.strip():
            errors.append("Product Name is required")
        # Guard: reject if name looks like a barcode (all digits/letters, no spaces, ≤12 chars)
        elif _re.match(r'^[A-Z0-9]{4,15}$', name.strip().upper()) and ' ' not in name.strip():
            errors.append(
                f"Product Name **{name.strip()}** looks like a barcode was scanned into the name field. "
                "Please type the actual product name (e.g. 'Butler 8305 Black')."
            )
        if barcode.strip() and _barcode_exists(barcode.strip(), "products"):
            errors.append(f"Barcode **{barcode.strip()}** already used by another product — must be unique")
        if float(gst) > 0 and not hsn.strip():
            errors.append(
                "HSN Code is required for taxable products (GST > 0). Enter HSN, "
                "or set GST to 0 only if the product is genuinely GST-exempt."
            )
        if name.strip() and not force_new:
            try:
                import difflib as _difflib
                from modules.sql_adapter import run_query as _rq_dup
                candidate = (brand.strip() + " " + name.strip()).strip().lower()
                rows = _rq_dup(
                    "SELECT product_name, COALESCE(brand,'') AS brand FROM products WHERE is_active = true"
                ) or []
                near = []
                for row in rows:
                    existing = (
                        str(row.get("brand") or "") + " " + str(row.get("product_name") or "")
                    ).strip().lower()
                    if existing and existing != candidate:
                        if _difflib.SequenceMatcher(None, candidate, existing).ratio() >= 0.88:
                            near.append(str(row.get("product_name") or ""))
                    if len(near) >= 5:
                        break
                if near:
                    errors.append(
                        "Similar product(s) already exist: " + ", ".join(near[:5]) +
                        ". If this is genuinely new, tick the similar-name override and save again."
                    )
            except Exception:
                pass
        if errors:
            for e in errors: st.error(e)
            return
        try:
            from modules.sql_adapter import run_write
            from modules.loaders.universal_loader_core import _canonical_main_group
            run_write("""
                INSERT INTO products
                (id, product_name, brand, main_group, category,
                 gst_percent, hsn_code, unit, box_size, barcode,
                 is_active, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,NOW())
                ON CONFLICT (product_name) DO UPDATE SET
                    brand        = EXCLUDED.brand,
                    main_group   = EXCLUDED.main_group,
                    gst_percent  = EXCLUDED.gst_percent,
                    hsn_code     = COALESCE(EXCLUDED.hsn_code, products.hsn_code),
                    barcode      = COALESCE(EXCLUDED.barcode,  products.barcode)
            """, (str(uuid.uuid4()), name.strip(), brand.strip() or None,
                  _canonical_main_group(mg), category.strip() or None,
                  float(gst), hsn.strip() or None, unit, _i(box_size),
                  barcode.strip() or None))
            st.session_state["_qa_last_product_print"] = {
                "code":  barcode.strip() or name.strip()[:20],
                "price": mrp_prod,
                "name":  name.strip(),
            }
            st.session_state.pop("qa_prod_bc_val", None)
            _saved("product", {"Name": name.strip(), "Group": mg, "GST": f"{gst}%",
                               "Barcode": barcode.strip() or "—"})
            st.rerun()
        except Exception as ex:
            st.error(f"Save failed: {ex}")


# ══════════════════════════════════════════════════════════════════════════════
# 🕶️ FRAME
# ══════════════════════════════════════════════════════════════════════════════
def _render_old_frame_mrp_reprint():
    """Update MRP for an existing frame Scan Code / Item Code and reprint its jewellery sticker."""
    with st.expander("🏷️ Old Frame Sticker — Change MRP & Print", expanded=False):
        st.caption("Use for old frame stock already in DB. Product comes from Product master; Scan Code / Item Code + MRP come from Frame Inventory.")

        try:
            from modules.sql_adapter import run_query
            products = run_query("""
                SELECT p.id::text AS product_id,
                       COALESCE(p.product_name, '') AS product_name,
                       COALESCE(p.brand, '') AS brand,
                       COUNT(s.id) AS sku_count
                FROM products p
                JOIN inventory_stock s ON s.product_id = p.id
                WHERE COALESCE(p.is_active, TRUE) = TRUE
                  AND COALESCE(s.is_active, TRUE) = TRUE
                  AND COALESCE(NULLIF(s.item_code, ''), NULLIF(s.batch_no, '')) IS NOT NULL
                  AND (
                      LOWER(COALESCE(p.main_group,'')) LIKE '%%frame%%'
                      OR LOWER(COALESCE(p.main_group,'')) LIKE '%%sunglass%%'
                  )
                GROUP BY p.id, p.product_name, p.brand
                ORDER BY p.product_name
            """, []) or []
        except Exception as ex:
            st.error(f"Frame product list failed: {ex}")
            products = []

        if not products:
            st.info("No frame inventory found yet.")
            return

        product_labels = [
            f"{p['product_name']}"
            + (f" · {p['brand']}" if p.get("brand") else "")
            + f" · {int(p.get('sku_count') or 0)} scan code(s)"
            for p in products
        ]
        product_label = st.selectbox(
            "Product",
            product_labels,
            key="qa_old_frame_product_select",
        )
        product = products[product_labels.index(product_label)]

        try:
            from modules.sql_adapter import run_query
            rows = run_query("""
                SELECT s.id::text AS stock_id,
                       COALESCE(NULLIF(s.item_code, ''), NULLIF(s.batch_no, '')) AS sku,
                       COALESCE(s.barcode, '') AS barcode,
                       GREATEST(0, COALESCE(s.quantity, 0) - COALESCE(s.allocated_qty, 0)) AS qty,
                       COALESCE(s.mrp, 0) AS mrp,
                       COALESCE(s.selling_price, 0) AS selling_price,
                       COALESCE(s.purchase_rate, 0) AS purchase_rate,
                       COALESCE(s.location, '') AS location,
                       COALESCE(s.colour_mix, '') AS colour_mix,
                       COALESCE(s.frame_group, '') AS frame_group,
                       COALESCE(s.colour, '') AS colour,
                       COALESCE(p.product_name, '') AS product_name,
                       COALESCE(p.brand, '') AS brand
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE s.product_id = %s::uuid
                  AND COALESCE(s.is_active, TRUE) = TRUE
                  AND COALESCE(NULLIF(s.item_code, ''), NULLIF(s.batch_no, '')) IS NOT NULL
                ORDER BY COALESCE(NULLIF(s.item_code, ''), NULLIF(s.batch_no, ''))
            """, [product["product_id"]]) or []
        except Exception as ex:
            st.error(f"Scan code list failed: {ex}")
            rows = []

        if not rows:
            st.info("No scan code rows found for this product.")
            return

        scanned = st.text_input(
            "Scan or type Scan Code / Item Code",
            placeholder="e.g. D10007",
            key="qa_old_frame_sku_scan",
        ).strip().upper()

        sku_match = None
        if scanned:
            sku_match = next((s for s in rows if str(s.get("sku") or "").upper() == scanned), None)
            if not sku_match:
                st.warning(f"Scan Code / Item Code {scanned!r} not found under selected product")

        sku_labels = [
            f"{s['sku']}  |  📍{s['location']}  |  ₹{float(s['mrp'] or 0):.0f}"
            + (f"  |  {s['colour_mix']}" if s.get("colour_mix") else "")
            + (f"  [{s['frame_group']}]" if s.get("frame_group") else "")
            for s in rows
        ]
        default_idx = 0
        if sku_match:
            default_idx = next((i for i, s in enumerate(rows) if str(s.get("sku") or "").upper() == scanned), 0)

        sku_label = st.selectbox(
            f"Select Scan Code / Item Code ({len(rows)} in stock)",
            sku_labels,
            index=default_idx,
            key="qa_old_frame_sku_select",
        )
        row = rows[sku_labels.index(sku_label)]
        print_code = str(row.get("sku") or "").strip()
        current_mrp = float(row.get("mrp") or 0)

        st.success(
            f"🕶️ {row.get('product_name','')} | {row.get('brand','')} | "
            f"Scan Code: {print_code} | 📍{row.get('location','')} | ₹{current_mrp:.0f}"
        )

        c1, c2, c3 = st.columns([1, 1, 1])
        c1.metric("Current MRP", f"₹{current_mrp:.0f}")
        c2.metric("Qty", f"{float(row.get('qty') or 0):g}")
        c3.metric("Sticker Code", print_code or "—")

        new_mrp = st.number_input(
            "New MRP ₹",
            min_value=0.0,
            step=0.50,
            value=current_mrp,
            key=f"qa_old_frame_new_mrp_{row['stock_id']}",
        )

        b1, b2 = st.columns([1, 1])
        with b1:
            if st.button(
                "💾 Save MRP",
                key=f"qa_old_frame_save_{row['stock_id']}",
                type="primary",
                use_container_width=True,
            ):
                if new_mrp <= 0:
                    st.error("MRP must be > 0")
                else:
                    try:
                        from modules.sql_adapter import run_write
                        run_write("""
                            UPDATE inventory_stock
                            SET mrp = %s,
                                updated_at = NOW()
                            WHERE id = %s::uuid
                        """, [float(new_mrp), row["stock_id"]])
                        st.session_state["_qa_old_frame_print"] = {
                            "code": print_code,
                            "price": float(new_mrp),
                            "name": row.get("product_name") or "",
                        }
                        st.success(f"MRP updated to ₹{float(new_mrp):.0f}")
                        st.rerun()
                    except Exception as ex:
                        st.error(f"MRP update failed: {ex}")
        with b2:
            if print_code and st.button(
                "🖨️ Print with Current MRP",
                key=f"qa_old_frame_print_now_{row['stock_id']}",
                use_container_width=True,
            ):
                st.session_state["_qa_old_frame_print"] = {
                    "code": print_code,
                    "price": float(new_mrp),
                    "name": row.get("product_name") or "",
                }
                st.rerun()

        old_print = st.session_state.get("_qa_old_frame_print")
        if old_print:
            st.success(
                f"Ready to print — **{old_print.get('name','')}** · "
                f"{old_print.get('code','')} · MRP ₹{float(old_print.get('price') or 0):.0f}"
            )
            if old_print.get("code") and float(old_print.get("price") or 0) > 0:
                from modules.printing.label_print_ui import get_frame_barcode_print_name
                _frame_label_name = get_frame_barcode_print_name()
                try:
                    from modules.printing.label_preview import render_label_preview
                    render_label_preview(
                        code=old_print["code"],
                        shop=_frame_label_name,
                        price=f"Rs.{float(old_print['price']):.0f}",
                    )
                except Exception:
                    pass
                from modules.printing.label_print_ui import render_print_button
                render_print_button(
                    code=old_print["code"],
                    price=old_print["price"],
                    label=f"🖨️ Print updated sticker ({old_print['code']})",
                    key_suffix="old_frame_mrp",
                    shop=_frame_label_name,
                    compact=False,
                )
            if st.button("Clear old-frame print panel", key="qa_old_frame_clear", use_container_width=True):
                st.session_state.pop("_qa_old_frame_print", None)
                st.rerun()


def _tab_frame():
    st.markdown("#### 🕶️ Add Frame Scan Code")
    st.caption(
        "Product must exist in 📦 Product tab first. Each Scan Code / Item Code = one physical frame. "
        "Saved in inventory_stock.item_code for scanner use; Batch No is not used for frame scanning."
    )
    _render_old_frame_mrp_reprint()

    # ── Step 1: Show print + done flow AFTER a successful save ───────────────
    _last_frame = st.session_state.get("_qa_last_frame_print")
    if _last_frame:
        st.success(
            f"✅ Saved — **{_last_frame['name']}** | "
            f"Scan Code: {_last_frame['code']} | MRP ₹{_last_frame.get('price',0):.0f}"
        )
        col_print, col_done = st.columns(2)
        with col_print:
            if _last_frame.get("price", 0) > 0:
                from modules.printing.label_print_ui import get_frame_barcode_print_name
                _frame_label_name = get_frame_barcode_print_name()
                # Show live preview first
                try:
                    from modules.printing.label_preview import render_label_preview
                    render_label_preview(
                        code  = _last_frame["code"],
                        shop  = _frame_label_name,
                        price = f"Rs.{_last_frame['price']:.0f}",
                    )
                except Exception:
                    pass
                from modules.printing.label_print_ui import render_print_button
                render_print_button(
                    code=_last_frame["code"],
                    price=_last_frame["price"],
                    label=f"🖨️ Print sticker ({_last_frame['code']})",
                    key_suffix="frame_post",
                    shop=_frame_label_name,
                    compact=False,
                )
            else:
                st.caption("No MRP set — nothing to print")
        with col_done:
            if st.button("✅ Done — Add Another Frame", key="frame_done_btn",
                         use_container_width=True, type="primary"):
                # Full reset — clear saved state AND product selection
                st.session_state.pop("_qa_last_frame_print", None)
                st.session_state.pop("qa_prod_frame", None)   # clears product picker
                st.rerun()
        st.markdown("---")
        return   # ← block the form until Done is clicked

    # ── Step 2: Product picker ────────────────────────────────────────────────
    prod_name, prod_id = _pick_product("frame", ["frames","frame","sunglasses"])
    if not prod_id:
        return

    # ── Step 3: Scan first, then form ────────────────────────────────────────
    st.markdown("**Step 1 — Scan the frame sticker**")
    _bc_typed = st.text_input(
        "📷 Scan frame Scan Code / Item Code",
        placeholder="Point scanner here and scan the frame sticker",
        key="qa_frame_bc_scan",
    ).strip()
    if _bc_typed:
        st.session_state["qa_frame_bc_val"]      = _bc_typed
        st.session_state["qa_frame_sku_prefill"] = _bc_typed
    _bc_val  = st.session_state.get("qa_frame_bc_val", "")
    _sku_pre = st.session_state.get("qa_frame_sku_prefill", "")

    if _bc_val:
        st.success(f"✅ Scanned: **{_bc_val}** — now fill MRP and details below, then Save")
    else:
        st.caption("Or skip and type the Scan Code / Item Code manually in the form below")

    st.markdown("**Step 2 — Fill details and save**")

    # ── Step 4: Entry form ───────────────────────────────────────────────────
    with st.form("qa_frame", clear_on_submit=True):
        c1, c2 = st.columns(2)
        item_code = c1.text_input("Scan Code / Item Code *", value=_sku_pre, placeholder="D10007")
        barcode = c2.text_input("Product Barcode (optional)", value="",
                                 placeholder="Usually blank for frames")

        c3, c4, c5 = st.columns(3)
        colour   = c3.text_input("Colour", placeholder="Black")
        col_mix  = c4.text_input("Colour Mix", placeholder="Gold")
        temple_c = c5.text_input("Temple Colour", placeholder="Silver")

        c6, c7, c8 = st.columns(3)
        mrp  = c6.number_input("MRP ₹ *", min_value=0.0, step=0.50,
                                help="Required — used for billing and sticker printing")
        sell = c7.number_input("Selling ₹", min_value=0.0, step=0.50)
        cost = c8.number_input("Cost ₹",   min_value=0.0, step=0.50)

        c9, c10, c11, c12 = st.columns(4)
        size_a   = c9.number_input("A Size (mm)",  min_value=0.0, step=0.5)
        size_b   = c10.number_input("B Size (mm)", min_value=0.0, step=0.5)
        dbl      = c11.number_input("DBL (mm)",    min_value=0.0, step=0.5)
        temple_l = c12.number_input("Temple (mm)", min_value=0.0, step=0.5)

        c13, c14 = st.columns(2)
        location  = c13.text_input("Location / Box", placeholder="D1")
        frame_grp = c14.text_input("Frame Group", placeholder="Near Dead / Sale / Premium")

        submitted = st.form_submit_button("💾 Save Frame Scan Code", type="primary", use_container_width=True)

    if submitted:
        errors = []
        if not item_code.strip():
            errors.append("Scan Code / Item Code is required")
        if mrp <= 0:
            errors.append("MRP must be > 0 — required for billing")
        if item_code.strip() and _item_code_exists(item_code.strip()):
            errors.append(f"Scan Code / Item Code **{item_code.strip()}** already exists — use ✏️ Edit flow to update prices")
        if barcode.strip() and _barcode_exists(barcode.strip(), "inventory_stock"):
            errors.append(f"Barcode **{barcode.strip()}** already used — must be unique across all stock")
        if errors:
            for e in errors: st.error(e)
            return
        try:
            from modules.sql_adapter import run_write
            run_write("""
                INSERT INTO inventory_stock
                (id, product_id, item_code, batch_no, quantity, mrp, selling_price, purchase_rate,
                 barcode, location, colour, colour_mix, temple_colour,
                 size_a, size_b, dbl, temple_length, frame_group,
                 stock_type, is_active, created_at, updated_at)
                VALUES (%s,%s,%s,NULL,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'BATCH',true,NOW(),NOW())
            """, (str(uuid.uuid4()), prod_id, item_code.strip().upper(),
                  mrp, sell or None, cost or None,
                  barcode.strip() or None, location.strip() or None,
                  colour.strip() or None, col_mix.strip() or None, temple_c.strip() or None,
                  size_a or None, size_b or None, dbl or None, temple_l or None,
                  frame_grp.strip() or None))
            # Store for print/done step — clears form and scanner
            st.session_state["_qa_last_frame_print"] = {
                "code":  item_code.strip().upper(),
                "price": mrp,
                "name":  prod_name,
            }
            st.session_state.pop("qa_frame_bc_val", None)
            st.session_state.pop("qa_frame_sku_prefill", None)
            st.rerun()
        except Exception as ex:
            st.error(f"Save failed: {ex}")


# ══════════════════════════════════════════════════════════════════════════════
# 👁️ CONTACT LENS
# ══════════════════════════════════════════════════════════════════════════════
def _tab_clens():
    st.markdown("#### 👁️ Add Contact Lens Batch")
    st.caption("Product must exist in 📦 Product tab first. Barcode and Batch+Product combination must be unique.")
    _show_summary("clens")

    prod_name, prod_id = _pick_product("clens", ["contact lenses"])
    if not prod_id:
        return

    with st.form("qa_clens", clear_on_submit=True):
        c1, c2 = st.columns(2)
        batch_no = c1.text_input("Batch No *", placeholder="AO2024A01")
        barcode  = c2.text_input("Barcode", placeholder="Scan box barcode — unique")

        c3, c4, c5, c6 = st.columns(4)
        sph  = c3.text_input("SPH", placeholder="-2.00")
        cyl  = c4.text_input("CYL", placeholder="0.00")
        axis = c5.text_input("AXIS", placeholder="0")
        eye  = c6.selectbox("Eye", ["B","R","L"])

        c7, c8, c9, c10 = st.columns(4)
        qty    = c7.number_input("Qty *", min_value=1, value=1)
        mrp    = c8.number_input("MRP ₹ *", min_value=0.0, step=0.50)
        sell   = c9.number_input("Selling ₹", min_value=0.0, step=0.50)
        cost   = c10.number_input("Cost ₹",   min_value=0.0, step=0.50)

        c11, c12 = st.columns(2)
        expiry   = c11.text_input("Expiry (YYYY-MM)", placeholder="2026-06")
        location = c12.text_input("Location", placeholder="FRIDGE-1")

        submitted = st.form_submit_button("💾 Save CL Batch", type="primary", use_container_width=True)

    if submitted:
        errors = []
        if not batch_no.strip(): errors.append("Batch No is required")
        if mrp <= 0:             errors.append("MRP must be > 0")
        if batch_no.strip() and _batch_exists(prod_id, batch_no.strip()):
            errors.append(f"Batch **{batch_no.strip()}** already exists for **{prod_name}** — "
                          "use Data Loader → Edit to update qty/prices")
        if barcode.strip() and _barcode_exists(barcode.strip(), "inventory_stock"):
            errors.append(f"Barcode **{barcode.strip()}** already used — must be unique")
        if errors:
            for e in errors: st.error(e); return
        try:
            from modules.sql_adapter import run_write
            exp = expiry.strip()+"-01" if expiry.strip() and len(expiry.strip())==7 else None
            run_write("""
                INSERT INTO inventory_stock
                (id, product_id, batch_no, sph, cyl, axis, eye_side,
                 quantity, mrp, selling_price, purchase_rate,
                 expiry_date, barcode, location,
                 stock_type, is_active, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'BATCH',true,NOW(),NOW())
            """, (str(uuid.uuid4()), prod_id, batch_no.strip(),
                  _f(sph) or None, _f(cyl) or None, _i(axis) or None, eye,
                  _i(qty), mrp, sell or None, cost or None,
                  exp, barcode.strip() or None, location.strip() or None))
            _saved("clens", {"Product": prod_name, "Batch": batch_no.strip(),
                             "SPH": sph, "Qty": qty, "MRP": f"₹{mrp:.0f}"})
            st.rerun()
        except Exception as ex:
            st.error(f"Save failed: {ex}")


# ══════════════════════════════════════════════════════════════════════════════
# 🔍 OPHTHALMIC LENS
# ══════════════════════════════════════════════════════════════════════════════
def _tab_ophlens():
    st.markdown("#### 🔍 Add Ophthalmic Lens Stock")
    st.caption("Product must exist in 📦 Product tab first. Barcode must be unique.")
    _show_summary("ophlens")

    prod_name, prod_id = _pick_product("ophlens", ["ophthalmic lenses","ophthalmic"])
    if not prod_id:
        return

    with st.form("qa_ophlens", clear_on_submit=True):
        c1, c2 = st.columns(2)
        item_type = c1.selectbox("Type", ["STOCK","RX"])
        barcode   = c2.text_input("Barcode", placeholder="Scan envelope — unique")

        c3, c4, c5, c6, c7 = st.columns(5)
        sph  = c3.text_input("SPH *", placeholder="-2.00")
        cyl  = c4.text_input("CYL",   placeholder="0.00")
        axis = c5.text_input("AXIS",  placeholder="0")
        add  = c6.text_input("ADD",   placeholder="1.50")
        eye  = c7.selectbox("Eye", ["B","R","L"])

        c8, c9, c10 = st.columns(3)
        qty      = c8.number_input("Qty", min_value=0, value=1)
        mrp      = c9.number_input("MRP ₹", min_value=0.0, step=0.50)
        sell     = c10.number_input("Selling ₹", min_value=0.0, step=0.50)

        location = st.text_input("Location", placeholder="RACK-A1")

        submitted = st.form_submit_button("💾 Save Ophthalmic Stock", type="primary", use_container_width=True)

    if submitted:
        errors = []
        if not sph.strip(): errors.append("SPH is required")
        if barcode.strip() and _barcode_exists(barcode.strip(), "inventory_stock"):
            errors.append(f"Barcode **{barcode.strip()}** already used — must be unique")
        if errors:
            for e in errors: st.error(e); return
        try:
            from modules.sql_adapter import run_write
            _add_v = _f(add); _cyl_v = _f(cyl)
            design = "MULTIFOCAL" if _add_v else ("TORIC" if _cyl_v else "SPHERICAL")
            run_write("""
                INSERT INTO inventory_stock
                (id, product_id, sph, cyl, axis, add_power, eye_side,
                 quantity, mrp, selling_price, barcode, location,
                 lens_design, item_type, stock_type, is_active, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'POWER',true,NOW(),NOW())
            """, (str(uuid.uuid4()), prod_id,
                  _f(sph), _cyl_v or None, _i(axis) or None, _add_v or None, eye,
                  _i(qty), mrp, sell or None,
                  barcode.strip() or None, location.strip() or None,
                  design, item_type))
            _saved("ophlens", {"Product": prod_name, "SPH": sph,
                               "CYL": cyl, "Type": item_type, "Qty": qty})
            st.rerun()
        except Exception as ex:
            st.error(f"Save failed: {ex}")


# ══════════════════════════════════════════════════════════════════════════════
# 💊 SOLUTION / ACCESSORY
# ══════════════════════════════════════════════════════════════════════════════
def _tab_solution():
    st.markdown("#### 💊 Add Solution / Accessory Batch")
    st.caption("Product must exist in 📦 Product tab first. Barcode must be unique.")
    _show_summary("solution")

    prod_name, prod_id = _pick_product(
        "solution", ["solution","solutions","accessories","service"]
    )
    if not prod_id:
        return

    with st.form("qa_solution", clear_on_submit=True):
        c1, c2 = st.columns(2)
        batch_no = c1.text_input("Batch No *", placeholder="SOL2024A")
        barcode  = c2.text_input("Barcode", placeholder="Scan product barcode — unique")

        c3, c4, c5, c6 = st.columns(4)
        qty    = c3.number_input("Qty *", min_value=1, value=1)
        mrp    = c4.number_input("MRP ₹ *", min_value=0.0, step=0.50)
        sell   = c5.number_input("Selling ₹", min_value=0.0, step=0.50)
        cost   = c6.number_input("Cost ₹",   min_value=0.0, step=0.50)

        c7, c8 = st.columns(2)
        expiry   = c7.text_input("Expiry (YYYY-MM)", placeholder="2026-12")
        location = c8.text_input("Location", placeholder="SHELF-B")

        submitted = st.form_submit_button("💾 Save Batch", type="primary", use_container_width=True)

    if submitted:
        errors = []
        if not batch_no.strip(): errors.append("Batch No is required")
        if mrp <= 0:             errors.append("MRP must be > 0")
        if batch_no.strip() and _batch_exists(prod_id, batch_no.strip()):
            errors.append(f"Batch **{batch_no.strip()}** already exists for **{prod_name}**")
        if barcode.strip() and _barcode_exists(barcode.strip(), "inventory_stock"):
            errors.append(f"Barcode **{barcode.strip()}** already used — must be unique")
        if errors:
            for e in errors: st.error(e); return
        try:
            from modules.sql_adapter import run_write
            exp = expiry.strip()+"-01" if expiry.strip() and len(expiry.strip())==7 else None
            run_write("""
                INSERT INTO inventory_stock
                (id, product_id, batch_no, quantity, mrp, selling_price,
                 purchase_rate, expiry_date, barcode, location,
                 stock_type, is_active, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'BATCH',true,NOW(),NOW())
            """, (str(uuid.uuid4()), prod_id, batch_no.strip(),
                  _i(qty), mrp, sell or None, cost or None,
                  exp, barcode.strip() or None, location.strip() or None))
            _saved("solution", {"Product": prod_name, "Batch": batch_no.strip(),
                                "Qty": qty, "MRP": f"₹{mrp:.0f}"})
            st.rerun()
        except Exception as ex:
            st.error(f"Save failed: {ex}")


# ══════════════════════════════════════════════════════════════════════════════
# ⬜ BLANK INVENTORY
# ══════════════════════════════════════════════════════════════════════════════
def _tab_blank():
    st.markdown("#### ⬜ Add Blank Inventory Entry")
    st.caption("Barcode must be unique across blank inventory.")
    _show_summary("blank")

    with st.form("qa_blank", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        brand    = c1.text_input("Brand *", placeholder="Essilor")
        category = c2.selectbox("Category *", ["Single Vision","Progressive","Bifocal","Toric","Reading"])
        material = c3.text_input("Material / Coating *", placeholder="CR39 / Blue Cut")
        add_pow  = c4.number_input("Add Power", min_value=0.0, step=0.25)

        c5, c6, c7 = st.columns(3)
        qty_r   = c5.number_input("Qty Right",       min_value=0, value=0)
        qty_l   = c6.number_input("Qty Left",        min_value=0, value=0)
        qty_ind = c7.number_input("Qty Independent", min_value=0, value=0)

        c8, c9, c10, c11 = st.columns(4)
        cost     = c8.number_input("Cost ₹", min_value=0.0, step=0.50)
        min_stk  = c9.number_input("Min Stock", min_value=0, value=10)
        barcode  = c10.text_input("Barcode", placeholder="Unique — scan label")
        location = c11.text_input("Location", placeholder="RACK-B2")

        c12, c13 = st.columns(2)
        batch_no = c12.text_input("Batch No", placeholder="LOT2024A")
        base_rec = c13.number_input("Recommended Base", min_value=0.0, step=0.5)

        submitted = st.form_submit_button("💾 Save Blank Entry", type="primary", use_container_width=True)

    if submitted:
        errors = []
        if not brand.strip():    errors.append("Brand is required")
        if not material.strip(): errors.append("Material is required")
        if barcode.strip() and _barcode_exists(barcode.strip(), "blank_inventory"):
            errors.append(f"Barcode **{barcode.strip()}** already used in blank inventory — must be unique")
        if errors:
            for e in errors: st.error(e); return
        try:
            from modules.sql_adapter import run_write
            run_write("""
                INSERT INTO blank_inventory
                (id, brand, category, material, add_power,
                 qty_right, qty_left, qty_independent, cost_price,
                 min_stock, base_recommended, barcode, batch_no, location,
                 is_active, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,NOW())
            """, (str(uuid.uuid4()), brand.strip(), category, material.strip(),
                  add_pow or None, _i(qty_r), _i(qty_l), _i(qty_ind),
                  cost or None, _i(min_stk), base_rec or None,
                  barcode.strip() or None, batch_no.strip() or None,
                  location.strip() or None))
            _saved("blank", {"Brand": brand.strip(), "Category": category,
                             "Material": material.strip(),
                             "R/L/Ind": f"{qty_r}/{qty_l}/{qty_ind}"})
            st.rerun()
        except Exception as ex:
            st.error(f"Save failed: {ex}")


# ══════════════════════════════════════════════════════════════════════════════
# 🏢 PARTY
# ══════════════════════════════════════════════════════════════════════════════
def _tab_party():
    st.markdown("#### 🏢 Add / Edit Party")
    st.caption("Uses CRM module — full party profiles with credit limits, GSTIN, barcode etc.")
    try:
        from modules.crm.crm import render_supplier_quick_add
        render_supplier_quick_add()
    except Exception as ex:
        st.warning(f"CRM module unavailable ({ex}). Using fallback form.")
        _show_summary("party")
        # Live name similarity check — fires outside form so it re-renders on typing
        _party_name_live = st.text_input(
            "Party Name *",
            key="qa_party_name_live",
            placeholder="Type name — similar parties shown instantly"
        )
        render_party_name_warning(_party_name_live)

        with st.form("qa_party_fb", clear_on_submit=True):
            c1, c2 = st.columns(2)
            name   = c1.text_input("Party Name *", value=_party_name_live)
            ptype  = c2.selectbox("Type", ["Retail","Doctor","Optician","Supplier","Wholesale"])
            c3, c4 = st.columns(2)
            mobile = c3.text_input("Mobile")
            gstin  = c4.text_input("GSTIN")
            barcode = st.text_input("Barcode *", placeholder="Unique — for party sticker scanning")
            submitted = st.form_submit_button("💾 Save Party", type="primary", use_container_width=True)
        if submitted:
            errors = []
            if not name.strip(): errors.append("Party Name required")
            if barcode.strip() and _barcode_exists(barcode.strip(), "parties"):
                errors.append(f"Barcode **{barcode.strip()}** already used by another party")
            if errors:
                for e in errors: st.error(e); return
            try:
                from modules.sql_adapter import run_query, run_write
                existing = run_query("""
                    SELECT id::text AS id
                    FROM parties
                    WHERE LOWER(TRIM(party_name)) = LOWER(TRIM(%s))
                    LIMIT 1
                """, (name.strip(),)) or []
                if existing:
                    run_write("""
                        UPDATE parties
                        SET party_type = %s,
                            mobile = COALESCE(NULLIF(%s,''), mobile),
                            gstin = COALESCE(NULLIF(%s,''), gstin),
                            barcode = COALESCE(NULLIF(%s,''), barcode),
                            is_active = TRUE
                        WHERE id = %s::uuid
                    """, (
                        ptype,
                        mobile.strip(),
                        gstin.strip().upper(),
                        barcode.strip(),
                        existing[0]["id"],
                    ))
                else:
                    run_write("""
                        INSERT INTO parties
                        (id, party_name, party_type, mobile, gstin, barcode, is_active, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,true,NOW())
                    """, (str(uuid.uuid4()), name.strip(), ptype,
                          mobile.strip() or None, gstin.strip().upper() or None,
                          barcode.strip() or None))
                _saved("party", {"Name": name.strip(), "Type": ptype})
                st.rerun()
            except Exception as e:
                st.error(f"Save failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 🏥 PATIENT
# ══════════════════════════════════════════════════════════════════════════════
def _tab_patient():
    st.markdown("#### 🏥 Add Patient")
    try:
        from modules.loaders.patient_dedup import _ensure_patient_identity_columns
        _ensure_patient_identity_columns()
    except Exception:
        pass
    st.caption(
        "Identity = **Name + Mobile together** (composite key). "
        "Same mobile, different name = different patient (son, wife etc.). "
        "Same name, different mobile = different person."
    )

    # ── Identity model explainer ─────────────────────────────────────────────
    with st.expander("ℹ️ How patient identity works", expanded=False):
        st.markdown("""
| Scenario | Name | Mobile | Result |
|---|---|---|---|
| Father visits | **Ramesh Kumar** | 9876543210 | New patient ✅ |
| Son visits, gives father's number | **Arjun Kumar** | 9876543210 | **New patient** (different name) ✅ |
| Another Ramesh, different mobile | **Ramesh Kumar** | 8765432100 | **New patient** (different mobile) ✅ |
| Father returns | **Ramesh Kumar** | 9876543210 | **Same patient** found ✅ |

**Rule:** Name + Mobile = unique identity. Family members sharing a mobile are separate patients.
Set **Relation** to "Son of Ramesh" so you know who is who.
        """)

    _show_summary("patient")

    # ── Search existing first ─────────────────────────────────────────────────
    st.markdown("**Search existing patient first** (avoid duplicates)")
    search_col, _ = st.columns([3,2])
    search_term = search_col.text_input(
        "Search by name or mobile",
        placeholder="Type name or mobile number",
        key="qa_patient_search"
    )
    if search_term:
        try:
            from modules.sql_adapter import search_patients
            results = search_patients(search_term)
            if not results.empty:
                st.success(f"Found {len(results)} patient(s):")
                for _, row in results.iterrows():
                    rel = row.get("relation","Self") or "Self"
                    st.markdown(
                        f"✅ **{row['patient_name']}** | "
                        f"📞 {row.get('mobile','—')} | "
                        f"🔗 {rel} | "
                        f"Rec# {row.get('record_no','—')}"
                    )
                st.info("Patient exists — no need to add again. Select from billing screen.")
                st.markdown("---")
            else:
                st.warning(f"No patient found for '{search_term}' — fill form below to add.")
        except Exception as ex:
            st.warning(f"Search unavailable: {ex}")

    st.markdown("**Add new patient**")
    with st.form("qa_patient_new", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name   = c1.text_input("Patient Name *", placeholder="First + Last name")
        mobile = c2.text_input("Mobile *", placeholder="Primary contact number")

        c3, c4, c5 = st.columns(3)
        relation = c3.text_input(
            "Relation / Self",
            placeholder="Self / Son of / Wife of / Daughter of",
            value="Self"
        )
        gender = c4.selectbox("Gender", ["—","Male","Female","Other"])
        dob    = c5.text_input("Date of Birth", placeholder="YYYY-MM-DD (optional)")

        ref_mobile = st.text_input(
            "Primary family mobile (if this patient uses someone else's number)",
            placeholder="e.g. Father's mobile — leave blank if mobile above is their own"
        )

        submitted = st.form_submit_button("💾 Save Patient", type="primary", use_container_width=True)

    if submitted:
        errors = []
        if not name.strip():   errors.append("Patient Name is required")
        if not mobile.strip(): errors.append("Mobile number is required")
        if errors:
            for e in errors: st.error(e)
            return

        # Check composite uniqueness
        try:
            from modules.loaders.patient_dedup import resolve_patient, save_patient

            resolution = resolve_patient(name.strip(), mobile.strip(), relation.strip())
            action = resolution["action"]

            if action == "found":
                st.warning(
                    f"Patient **{name.strip()}** with mobile **{mobile.strip()}** "
                    "already exists — no duplicate created. Select from billing screen."
                )
                return

            if action == "spell":
                cands = resolution["candidates"]
                st.warning(
                    f"⚠️ Possible spelling match: **{cands[0]['master_name']}** "
                    f"is on the same mobile ({mobile.strip()}). "
                    "Is this a different person or a spelling correction?"
                )
                col_new, col_fix = st.columns(2)
                if col_new.button("➕ Different person — create new", key="qa_pat_new"):
                    pass  # fall through to save below
                if col_fix.button("✏️ Fix spelling of existing patient", key="qa_pat_fix"):
                    from modules.sql_adapter import run_write
                    run_write("UPDATE patients SET master_name=%s WHERE id=%s",
                              (name.strip(), cands[0]["id"]))
                    _saved("patient", {"Updated": cands[0]["master_name"],
                                       "New name": name.strip()})
                    st.rerun()
                return  # wait for user choice

            if action == "family":
                sibs = resolution["siblings"]
                sib_list = ", ".join(r["master_name"] for r in sibs)
                st.info(
                    f"ℹ️ Mobile {mobile.strip()} already registered for: **{sib_list}**. "
                    f"**{name.strip()}** will be added as a separate patient (family member)."
                )

            if action == "suffix":
                st.info(
                    f"ℹ️ {resolution['reason']} — "
                    f"will be stored as **{resolution['stored_name']}**"
                )

            # All other actions → save
            _dob = dob.strip() if dob.strip() and len(dob.strip()) >= 8 else None
            _gender = gender if gender != "—" else None
            pid, err = save_patient(
                name       = name.strip(),
                mobile     = mobile.strip(),
                relation   = relation.strip() or "Self",
                gender     = _gender,
                dob        = _dob,
                ref_mobile = ref_mobile.strip() or None,
            )
            if err:
                st.error(f"Save failed: {err}")
                return
            stored = resolution.get("stored_name", name.strip())
            _saved("patient", {
                "Name":     stored,
                "Mobile":   mobile.strip() or "—",
                "Relation": relation.strip(),
            })
            st.rerun()

        except Exception as ex:
            st.error(f"Save failed: {ex}")


# ══════════════════════════════════════════════════════════════════════════════
# 🪪 PRINT CARDS
# ══════════════════════════════════════════════════════════════════════════════
def _tab_print_cards(rid: str = "qa"):
    st.markdown("#### 🪪 Print Barcode Cards")
    st.caption(
        "Print credit-card-size barcode cards. Each person gets a unique card. "
        "Scan at billing, backoffice, or any screen to pull their record instantly."
    )

    shop_name = st.text_input("Shop / Clinic Name on card", value="DV Optical",
                              key=f"pc_shop_name_{rid}")

    card_type = st.radio("Card type", ["Patient cards", "Party cards", "Stage cards"],
                         horizontal=True, key=f"pc_type_{rid}")

    if card_type == "Patient cards":
        st.markdown("**Search patients to print cards for:**")
        search = st.text_input("Search by name, mobile, or PAT barcode", key=f"pc_pat_search_{rid}")
        patients = []
        if search:
            try:
                from modules.sql_adapter import search_patients
                df = search_patients(search)
                if not df.empty:
                    patients = df.to_dict("records")
                    st.success(f"Found {len(patients)} patient(s)")
                    for p in patients:
                        rel = p.get("relation","Self") or "Self"
                        st.markdown(
                            f"✅ **{p['patient_name']}** | "
                            f"📞 {p.get('mobile','—')} | "
                            f"{rel} | "
                            f"Barcode: `{p.get('barcode','—')}`"
                        )
                else:
                    st.warning("No patients found")
            except Exception as ex:
                st.error(f"Search failed: {ex}")

        if patients and st.button("🖨️ Generate Patient Cards PDF",
                                   type="primary", key=f"pc_gen_pat_{rid}"):
            try:
                from modules.documents.card_generator import generate_patient_cards
                # Map search_patients columns to card_generator expected keys
                mapped = []
                for p in patients:
                    mapped.append({
                        "barcode":     p.get("barcode") or f"PAT{str(p.get('patient_id',''))[:6]}",
                        "master_name": p.get("patient_name",""),
                        "mobile":      p.get("mobile",""),
                        "relation":    p.get("relation","Self"),
                        "gender":      p.get("gender",""),
                        "dob":         p.get("dob",""),
                    })
                pdf_bytes = generate_patient_cards(mapped, shop_name=shop_name)
                st.download_button(
                    label=f"💾 Download Patient Cards ({len(mapped)} cards)",
                    data=pdf_bytes,
                    file_name=f"patient_cards_{len(mapped)}.pdf",
                    mime="application/pdf",
                    key=f"pc_dl_pat_{rid}",
                    use_container_width=True,
                )
            except Exception as ex:
                st.error(f"Card generation failed: {ex}")

    elif card_type == "Party cards":
        st.markdown("**Search parties to print cards for:**")
        search = st.text_input("Search party name or mobile", key=f"pc_party_search_{rid}")
        parties = []
        if search:
            try:
                from modules.sql_adapter import run_query
                rows = run_query("""
                    SELECT id::text, party_name, mobile, party_type,
                           city, gstin, barcode
                    FROM parties
                    WHERE party_name ILIKE %s OR mobile ILIKE %s
                    ORDER BY party_name LIMIT 20
                """, (f"%{search}%", f"%{search}%")) or []
                parties = rows
                if parties:
                    st.success(f"Found {len(parties)} part(ies)")
                    for p in parties:
                        st.markdown(
                            f"✅ **{p['party_name']}** | {p.get('party_type','')} | "
                            f"Barcode: `{p.get('barcode','—')}`"
                        )
                else:
                    st.warning("No parties found")
            except Exception as ex:
                st.error(f"Search failed: {ex}")

        if parties and st.button("🖨️ Generate Party Cards PDF",
                                  type="primary", key=f"pc_gen_party_{rid}"):
            try:
                from modules.documents.card_generator import generate_party_cards
                pdf_bytes = generate_party_cards(parties, shop_name=shop_name)
                st.download_button(
                    label=f"💾 Download Party Cards ({len(parties)} cards)",
                    data=pdf_bytes,
                    file_name=f"party_cards_{len(parties)}.pdf",
                    mime="application/pdf",
                    key=f"pc_dl_party_{rid}",
                    use_container_width=True,
                )
            except Exception as ex:
                st.error(f"Card generation failed: {ex}")

    elif card_type == "Stage cards":
        st.markdown("**Generate laminated stage cards for production workflow:**")
        st.caption("Print once, laminate, keep near each workstation. Scan to update order status.")

        from modules.backoffice.scanner_panel import STAGE_CARDS
        selected = st.multiselect(
            "Select stages to print",
            options=[c["barcode"] for c in STAGE_CARDS],
            default=[c["barcode"] for c in STAGE_CARDS],
            format_func=lambda x: next(
                (f"{c['icon']} {c['label']}" for c in STAGE_CARDS if c["barcode"]==x), x
            ),
            key=f"pc_stages_{rid}"
        )
        copies = st.number_input("Copies of each card", min_value=1, max_value=10, value=2,
                                  key=f"pc_copies_{rid}")

        if selected and st.button("🖨️ Generate Stage Cards PDF",
                                   type="primary", key=f"pc_gen_stage_{rid}"):
            try:
                from modules.documents.card_generator import generate_blank_cards
                from reportlab.lib.pagesizes import A4
                from reportlab.pdfgen import canvas
                import io

                # Merge all stage cards into one PDF
                buf = io.BytesIO()
                c = canvas.Canvas(buf, pagesize=A4)

                from modules.documents.card_generator import (
                    _page_positions, _draw_card, COLS, ROWS
                )
                positions = _page_positions()
                slot = 0

                for barcode in selected:
                    card = next((x for x in STAGE_CARDS if x["barcode"]==barcode), None)
                    if not card: continue
                    for _ in range(int(copies)):
                        if slot > 0 and slot % (COLS * ROWS) == 0:
                            c.showPage()
                        x, y = positions[slot % (COLS * ROWS)]
                        _draw_card(c, x, y,
                                   card["barcode"],
                                   f"{card['icon']} {card['label']}",
                                   f"Scan to mark order → {card['status']}",
                                   "",
                                   shop_name, "STAGE")
                        slot += 1

                c.save()
                pdf_bytes = buf.getvalue()

                st.download_button(
                    label=f"💾 Download Stage Cards ({len(selected)} types × {copies} copies)",
                    data=pdf_bytes,
                    file_name="stage_cards.pdf",
                    mime="application/pdf",
                    key=f"pc_dl_stage_{rid}",
                    use_container_width=True,
                )
            except Exception as ex:
                st.error(f"Stage card generation failed: {ex}")

    st.markdown("---")
    st.caption(
        "💡 Print on thick paper (200gsm+) · Cut along marks · Laminate · "
        "Punch hole in corner for lanyard/ring if needed"
    )
