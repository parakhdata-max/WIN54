"""
modules/ui/smart_loader_ui.py
==============================
Smart Loader UI — Replaces the current upload tab.

Two clear flows:
  ✏️  EDIT  — Download current data → Edit → Preview changes → AI advice → Approve → Apply
  ➕  ADD   — Download blank template → Fill → Upload → Straight to DB

No wrong files accepted. No accidental overwrites. Full audit trail.

Usage in your main app:
    from modules.ui.smart_loader_ui import render_smart_loader
    render_smart_loader()
"""

import io
import streamlit as st
import pandas as pd
from datetime import datetime


# ── Supported file types ──────────────────────────────────────────────────────
FILE_TYPES = {
    "OPHLENS":      {"label": "Ophthalmic Lens",      "icon": "🔍"},
    "CLENS":        {"label": "Contact Lens",          "icon": "👁️"},
    "PRODUCT":      {"label": "Product Master",        "icon": "📦"},
    "PRICE":        {"label": "Price Master",          "icon": "💰"},
    # FRAME intentionally removed — use 🕶️ Frame Loader in sidebar
    # Frame Loader handles your BatchData Excel format (multi-sheet, StartCode, etc.)
    # and writes to the correct tables (products + inventory_stock)
    "PARTY":        {"label": "Party / Supplier",      "icon": "🏢"},
    "PATIENT":      {"label": "Patient Records",       "icon": "🏥"},
    "SOL":          {"label": "Solution / Batch",      "icon": "💊"},
    "BLANK":        {"label": "Blank Inventory",       "icon": "⬜"},
    "MAIN_GROUPS":  {"label": "Main Groups (GST/HSN)", "icon": "🏷️"},
    "OPH_SPEC":     {"label": "Ophthalmic Specs",       "icon": "🔬"},
    "OPH_ADDON":    {"label": "Ophthalmic Add-ons",     "icon": "➕"},
}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════

def render_smart_loader():
    st.title("📥 Smart Data Loader")
    st.caption("Safe, audited imports — no accidental overwrites, full change preview before applying.")

    # ── Module selector ───────────────────────────────────────────────────────
    col1, col2 = st.columns([2, 1])
    with col1:
        type_options = [f"{v['icon']} {v['label']}" for v in FILE_TYPES.values()]
        type_keys    = list(FILE_TYPES.keys())
        selected_idx = st.selectbox(
            "Select Module",
            range(len(type_keys)),
            format_func=lambda i: type_options[i],
            key="smart_loader_type",
        )
        file_type = type_keys[selected_idx]
        cfg       = FILE_TYPES[file_type]

    with col2:
        st.write("")
        st.write("")
        st.info(f"**{cfg['icon']} {cfg['label']}**")

    st.divider()

    # ── Main Groups: special management UI (no import/export flow) ────────────
    if file_type == "MAIN_GROUPS":
        _render_main_groups()
        return

    # ── Two flow tabs ─────────────────────────────────────────────────────────
    tab_edit, tab_add = st.tabs(["✏️  Edit Existing Records", "➕  Add New Records"])

    with tab_edit:
        _render_edit_flow(file_type, cfg)

    with tab_add:
        _render_add_flow(file_type, cfg)


# ══════════════════════════════════════════════════════════════════════════════
# EDIT FLOW
# ══════════════════════════════════════════════════════════════════════════════

def _render_edit_flow(file_type: str, cfg: dict):
    st.markdown("#### ✏️ Edit Existing Records")
    st.caption(
        "Download the current data, make changes in Excel, upload back. "
        "Only system-downloaded files accepted. Full change preview before applying."
    )

    # ── Step 1: Download ──────────────────────────────────────────────────────
    with st.expander("📥 Step 1 — Download Current Data", expanded=True):
        st.write(f"Download current **{cfg['label']}** records. Filter by brand to get focused lists.")
        st.write("Edit the white/blue columns only. Grey (🔒) columns are locked.")

        # ── Brand / Sub-brand filters ─────────────────────────────────────────
        _dl_filters = {}
        if file_type in ("OPH_SPEC", "OPH_ADDON"):
            _desc = {
                "OPH_SPEC": (
                    "**🔬 Ophthalmic Specs** — index × coating × treatment price matrix.\n\n"
                    "Required: **Product**, **Index**, **Coating**, **WLP_per_pair**.\n"
                    "⚠️ Run `migration_ophthalmic_lenses.sql` and upload "
                    "`PRODUCT_OPHTHALMIC.xlsx` first."
                ),
                "OPH_ADDON": (
                    "**➕ Ophthalmic Add-ons** — brand/category/product-level upgrades.\n\n"
                    "Required: **Brand**, **AddonName**. "
                    "Upserts on Brand + AddonName + AppliesTo."
                ),
            }
            st.info(_desc[file_type])

        if file_type in ("PRODUCT", "CLENS", "OPHLENS", "PRICE", "OPH_SPEC", "OPH_ADDON"):
            try:
                from modules.sql_adapter import run_query as _rq
                _brands = _rq("SELECT DISTINCT brand FROM products WHERE brand IS NOT NULL ORDER BY brand") or []
                _brand_list = ["All Brands"] + [r['brand'] for r in _brands if r.get('brand')]
                _sub_brands = _rq("SELECT DISTINCT brand_group FROM products WHERE brand_group IS NOT NULL ORDER BY brand_group") or []
                _sub_list = ["All"] + [r['brand_group'] for r in _sub_brands if r.get('brand_group')]

                fc1, fc2 = st.columns([2, 2])
                with fc1:
                    _sel_brand = st.selectbox("🏷️ Brand", _brand_list,
                                              key=f"dl_brand_{file_type}")
                    if _sel_brand != "All Brands":
                        _dl_filters["brand"] = _sel_brand
                with fc2:
                    _sel_sub = st.selectbox("📦 Sub-brand / Group", _sub_list,
                                            key=f"dl_sub_{file_type}")
                    if _sel_sub != "All":
                        _dl_filters["brand_group"] = _sel_sub
            except Exception:
                pass

        st.markdown("")
        col1, col2 = st.columns([1, 2])
        with col1:
            if st.button("⬇️ Download for Editing", key=f"dl_edit_{file_type}", type="primary"):
                _do_edit_download(file_type, cfg, filters=_dl_filters)

        with col2:
            st.caption(
                "ℹ️ The downloaded file has a 72-hour expiry. "
                "Download a fresh copy if yours is older."
            )

    # ── Step 2: Upload & Preview ───────────────────────────────────────────────
    with st.expander("📤 Step 2 — Upload Your Edited File", expanded=True):
        uploaded = st.file_uploader(
            "Upload your edited file here",
            type=["xlsx"],
            key=f"ul_edit_{file_type}",
            help="Only files downloaded from this system are accepted.",
        )

        if uploaded:
            _handle_edit_upload(uploaded, file_type, cfg)


def _do_edit_download(file_type: str, cfg: dict, filters: dict = None):
    """Generate and serve the fingerprinted edit download."""
    try:
        from modules.loaders.smart.download_manager import build_edit_download, make_edit_filename
        user = _get_user()

        with st.spinner("Preparing download..."):
            excel_bytes, file_id = build_edit_download(file_type, user=user,
                                                        filters=filters or {})

        filename = make_edit_filename(file_type)
        st.download_button(
            label     = f"💾 Save {cfg['icon']} {cfg['label']} Edit File",
            data      = excel_bytes,
            file_name = filename,
            mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key       = f"save_edit_{file_type}_{file_id[:8]}",
        )
        st.success(f"✅ File ready. File ID: `{file_id[:8]}...` | Expires in 72 hours.")

    except Exception as e:
        st.error(f"Download failed: {e}")


def _handle_edit_upload(uploaded, file_type: str, cfg: dict):
    """Full edit upload flow: guard → detect → advise → confirm → apply."""
    from modules.loaders.smart.upload_guard import check_upload
    from modules.loaders.smart.change_detector import detect_changes
    from modules.loaders.smart.ai_change_advisor import advise, answer_question
    from modules.loaders.smart.change_approver import apply_changes

    file_bytes = uploaded.read()
    user       = _get_user()

    # ── Guard check ───────────────────────────────────────────────────────────
    with st.spinner("Verifying file..."):
        guard = check_upload(file_bytes, expected_type=file_type, user=user)

    if not guard.allowed:
        for issue in guard.issues:
            st.error(issue)
        st.stop()
        return

    if guard.flow != "EDIT":
        st.error("⛔ This looks like an ADD template, not an EDIT file. Use the 'Add New Records' tab instead.")
        st.stop()
        return

    for w in guard.warnings:
        st.warning(w)

    st.success("✅ File verified — this is a valid system-downloaded edit file.")

    # ── Detect changes ────────────────────────────────────────────────────────
    with st.spinner("Scanning for changes..."):
        report = detect_changes(guard.df, file_type)

    if not report.has_changes and not report.has_blocked:
        if report.rows_not_found:
            st.warning(
                f"⚠️ No changes detected — but **{len(report.rows_not_found)} row(s) could not be matched** "
                f"to any record in the database. This is usually a key-matching bug.\n\n"
                f"Rows not found: `{'`, `'.join(report.rows_not_found[:10])}`\n\n"
                f"Please report this to support with your file attached."
            )
        else:
            st.info("ℹ️ No changes detected. The uploaded file matches the current database. Nothing to update.")
        return

    # ── Show blocked changes ──────────────────────────────────────────────────
    if report.has_blocked:
        st.warning(
            f"⛔ {len(report.blocked)} change(s) to locked fields will be **ignored** "
            "(identity fields cannot be changed)."
        )
        with st.expander("See ignored locked-field changes"):
            blocked_df = pd.DataFrame([{
                "Row": b.row_index, "Field": b.field_name,
                "Old": b.old_value, "Attempted New": b.new_value
            } for b in report.blocked])
            st.dataframe(blocked_df, use_container_width=True, hide_index=True)

    # ── AI Advisor ────────────────────────────────────────────────────────────
    advice = advise(report)

    # Propagate backup decision from advisor → report (change_approver reads report.backup_required)
    report.backup_required = advice.backup_required

    # Warn if any rows could not be matched to DB records
    if report.rows_not_found:
        st.warning(
            f"⚠️ {len(report.rows_not_found)} row(s) not found in DB — "
            "they will be skipped. This can happen if a record was deleted since download. "
            f"First few: {', '.join(report.rows_not_found[:5])}"
        )

    st.markdown("---")
    st.markdown("### 🔍 Change Preview")

    # Summary metrics
    rc = report.risk_counts
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Changes",   len(report.changes))
    c2.metric("🟢 Safe",          rc.get("SAFE", 0))
    c3.metric("🟡 Caution",       rc.get("CAUTION", 0))
    c4.metric("🔴 Warning",       rc.get("WARNING", 0))

    # Summary text
    _render_advice_box(advice)

    # Detailed change table
    with st.expander(f"📋 See all {len(report.changes)} field changes", expanded=report.risk_counts.get("WARNING", 0) > 0):
        change_df = report.to_dataframe()
        if not change_df.empty:
            st.dataframe(
                change_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Risk": st.column_config.TextColumn("Risk", width="small"),
                    "Old Value": st.column_config.TextColumn("Old Value"),
                    "New Value": st.column_config.TextColumn("New Value"),
                }
            )

    # ── Ask AI section ────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 💬 Ask About These Changes")
    question = st.text_input(
        "Type your question (e.g. 'what will change?', 'is it safe?', 'explain box_size')",
        key=f"ai_question_{file_type}",
        placeholder="what will change? | is it safe? | will old records change? | explain box_size",
    )
    if question:
        answer = answer_question(question, report)
        st.info(f"💡 {answer}")

    # ── Approval section ──────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### ✅ Approve Changes")
    st.markdown(advice.prompt_text)

    if advice.backup_required:
        st.info("💾 A backup snapshot will be taken automatically before applying.")

    # ── Collapsible change preview with highlights ────────────────────────────
    with st.expander(
        f"📝 Review Changes Before Approving ({len(report.changes)} field change(s))",
        expanded=True,
    ):
        change_df = report.to_dataframe()
        if not change_df.empty:

            # Colour-code rows by risk level
            _RISK_BG = {
                "⛔ BLOCKED": "#ffd6d6",   # deeper red  — blocked
                "🔴 WARNING": "#ffe0e0",   # light red   — high risk
                "🟡 CAUTION": "#fff7d6",   # light amber — medium risk
                "🟢 SAFE":    "#eafaf1",   # light green — safe
            }

            def _highlight_row(row):
                risk_val = str(row.get("Risk", ""))
                # Match on the emoji prefix used in to_dataframe()
                bg = "#ffe0e0"  # default: light red
                for key, colour in _RISK_BG.items():
                    if key in risk_val or risk_val in key:
                        bg = colour
                        break
                # Always highlight the New Value cell a slightly deeper red
                # to make what's changing immediately obvious
                styles = [f"background-color: {bg}"] * len(row)
                try:
                    nv_idx = row.index.get_loc("New Value")
                    styles[nv_idx] = "background-color: #ffb3b3; font-weight: bold"
                except (KeyError, Exception):
                    pass
                return styles

            styled = change_df.style.apply(_highlight_row, axis=1)

            st.dataframe(
                styled,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Risk":      st.column_config.TextColumn("Risk",      width="small"),
                    "Field":     st.column_config.TextColumn("Field",     width="medium"),
                    "Old Value": st.column_config.TextColumn("Old Value", width="medium"),
                    "New Value": st.column_config.TextColumn("New Value", width="medium"),
                },
            )

            # Legend
            st.caption(
                "🟥 **Bold red** = new value being written &nbsp;|&nbsp; "
                "🔴 WARNING = high-risk field &nbsp;|&nbsp; "
                "🟡 CAUTION = financial field &nbsp;|&nbsp; "
                "🟢 SAFE = low-risk change"
            )
        else:
            st.info("No field-level changes to preview.")

    # ── Dry run option ────────────────────────────────────────────────────────
    key_prefix = f"approve_{file_type}_{uploaded.name}"

    dry_run_clicked = st.button(
        "🔍 Dry Run — Simulate Without Saving",
        key=f"dry_{key_prefix}",
        help="Runs the full apply logic without writing to DB. Safe to run anytime.",
    )
    if dry_run_clicked:
        with st.spinner("Running dry run..."):
            dry_result = apply_changes(report, user=user, dry_run=True)
        st.success(f"🔍 Dry run complete — {dry_result.applied} field change(s) would be applied.")
        if dry_result.errors:
            for e in dry_result.errors:
                st.warning(f"⚠️ {e}")

    st.markdown("---")

    if advice.requires_typed:
        typed = st.text_input(
            "Type **CONFIRM** to proceed with high-risk changes:",
            key=f"typed_{key_prefix}",
        )
        can_proceed = typed.strip().upper() == "CONFIRM"
        if typed and not can_proceed:
            st.warning("Type exactly: CONFIRM")
    else:
        can_proceed = True

    col_yes, col_no, col_ask = st.columns([1, 1, 2])

    with col_yes:
        yes_clicked = st.button(
            "✅ Yes — Apply Changes",
            type="primary",
            disabled=not can_proceed,
            key=f"yes_{key_prefix}",
            use_container_width=True,
        )

    with col_no:
        no_clicked = st.button(
            "❌ No — Cancel",
            key=f"no_{key_prefix}",
            use_container_width=True,
        )

    if no_clicked:
        st.warning("❌ Import cancelled. No changes were made.")
        st.stop()
        return

    if yes_clicked and can_proceed:
        with st.spinner("Applying changes..."):
            result = apply_changes(report, user=user, dry_run=False)

        if result.success:
            guard.consume(user)   # ← mark fingerprint as used — blocks re-upload
            st.success(
                f"✅ Done! {result.applied} field change(s) applied successfully."
                + (f" | Backup ID: `{result.backup_id[:8]}...`" if result.backup_id else "")
            )
            st.info("ℹ️ This file has been consumed. Download a fresh copy to make further changes.")
            if result.errors:
                for e in result.errors:
                    st.warning(f"⚠️ {e}")
        else:
            st.error(f"❌ Import failed: {'; '.join(result.errors)}")


# ══════════════════════════════════════════════════════════════════════════════
# ADD FLOW
# ══════════════════════════════════════════════════════════════════════════════

def _render_add_flow(file_type: str, cfg: dict):
    st.markdown("#### ➕ Add New Records")
    st.caption(
        "Download a blank template, fill in new records, upload. "
        "This flow ONLY adds new records — it cannot modify existing data."
    )

    # OPH_SPEC / OPH_ADDON: allow direct upload of external price list files
    if file_type in ("OPH_SPEC", "OPH_ADDON"):
        st.info(
            "💡 You can upload files directly from brand price lists "
            f"(e.g. OPHTHALMIC_SPEC_PRICES.xlsx) — no system download required. "
            "Just make sure columns match: "
            + ("**Product, Index, Coating, WLP_per_pair**"
               if file_type == "OPH_SPEC"
               else "**Brand, AddonName**")
        )




    # ── Step 1: Download template ─────────────────────────────────────────────
    with st.expander("📥 Step 1 — Download Blank Template", expanded=True):
        st.write(f"Download a blank **{cfg['label']}** template with all required columns.")
        st.write("Delete the orange example row before filling in your data.")

        if st.button("⬇️ Download Blank Template", key=f"dl_add_{file_type}", type="primary"):
            _do_add_download(file_type, cfg)

    # ── Step 2: Upload ────────────────────────────────────────────────────────
    with st.expander("📤 Step 2 — Upload Filled Template", expanded=True):
        uploaded = st.file_uploader(
            "Upload your filled template",
            type=["xlsx"],
            key=f"ul_add_{file_type}",
            help="Upload the blank template after filling in new records.",
        )

        if uploaded:
            _handle_add_upload(uploaded, file_type, cfg)




# ══════════════════════════════════════════════════════════════════════════════
# FRAME QUICK ADD — manual entry form, no Excel required
# ══════════════════════════════════════════════════════════════════════════════

def _render_frame_quick_add():
    """
    Single-frame entry form.  Writes directly to the frames table via
    universal_loader_core so dedup and audit are identical to Excel upload.
    """
    from modules.sql_adapter import run_query, run_write
    import uuid as _uuid

    st.markdown(
        "<div style='background:#f0f9ff;border:1px solid #bae6fd;border-radius:9px;"
        "padding:14px 18px;margin-bottom:10px'>"
        "<b style='color:#0369a1'>⚡ Quick Add — Enter Frame Directly</b>"
        "<span style='color:#64748b;font-size:0.78rem;margin-left:8px'>"
        "No Excel needed for single frames</span></div>",
        unsafe_allow_html=True
    )

    with st.form("frame_quick_add_form", clear_on_submit=True):
        # Row 1 — Identity (product_name links to existing product in products table)
        c1, c2, c3 = st.columns([4, 2, 2])
        product_name = c1.text_input("Product Name *", placeholder="e.g. Butler 8305 Black")
        brand        = c2.text_input("Brand", placeholder="e.g. Butler")
        sku          = c3.text_input("Batch / SKU", placeholder="e.g. FR-BUT-8305 (auto if blank)")

        # Row 2 — Pricing + Stock
        c4, c5, c6, c7, c8 = st.columns([2, 2, 2, 1.5, 1.5])
        cost_price    = c4.number_input("Cost Price ₹", min_value=0.0, step=0.5, format="%.2f")
        selling_price = c5.number_input("Selling Price ₹", min_value=0.0, step=0.5, format="%.2f")
        mrp           = c6.number_input("MRP ₹ *", min_value=0.0, step=0.5, format="%.2f")
        gst_pct       = c7.selectbox("GST %", [0, 5, 12, 18, 28], index=0)
        qty           = c8.number_input("Qty *", min_value=0, step=1, value=1)

        submitted = st.form_submit_button("➕ Add Frame", type="primary", use_container_width=False)

    if submitted:
        errors = []
        if not product_name.strip(): errors.append("Product Name is required")
        if mrp <= 0:                 errors.append("MRP must be > 0")
        if qty < 0:                  errors.append("Qty cannot be negative")

        if errors:
            for e in errors:
                st.error(f"❌ {e}")
            return

        # Frames live in inventory_stock JOIN products (category='frame')
        # Step 1: find or create the product record
        try:
            prod_rows = run_query(
                "SELECT id FROM products WHERE product_name=%s AND LOWER(category)='frame' LIMIT 1",
                (product_name.strip(),)
            )
            if prod_rows:
                prod_id = str(prod_rows[0]["id"])
            else:
                # Create product master entry for this frame
                prod_id = str(_uuid.uuid4())
                run_write("""
                    INSERT INTO products
                    (id, product_name, brand, category, main_group,
                     unit, is_active, gst_percent, created_at)
                    VALUES (%s,%s,%s,'Frame','Frame',%s,true,%s,NOW())
                    ON CONFLICT DO NOTHING
                """, (prod_id, product_name.strip(), brand.strip(), 'PCS', round(float(gst_pct), 2)))
                # re-fetch in case ON CONFLICT hit
                prod_rows2 = run_query(
                    "SELECT id FROM products WHERE product_name=%s AND LOWER(category)='frame' LIMIT 1",
                    (product_name.strip(),)
                )
                if prod_rows2:
                    prod_id = str(prod_rows2[0]["id"])
        except Exception as ex:
            st.error(f"❌ Product lookup failed: {ex}")
            return

        # Step 2: check for existing inventory_stock row for this product
        batch_key = sku.strip().upper() if sku.strip() else f"FRAME-{product_name.strip()[:10].upper().replace(' ','-')}"
        try:
            stock_rows = run_query(
                "SELECT id, quantity FROM inventory_stock WHERE product_id=%s AND batch_no=%s LIMIT 1",
                (prod_id, batch_key)
            )
            if stock_rows:
                old_qty = int(stock_rows[0].get("quantity") or 0)
                new_qty = old_qty + qty
                run_write("""
                    UPDATE inventory_stock SET
                        quantity=%s, purchase_rate=%s,
                        selling_price=%s, mrp=%s,
                        is_active=true, updated_at=NOW()
                    WHERE product_id=%s AND batch_no=%s
                """, (new_qty, round(float(cost_price), 2),
                      round(float(selling_price), 2), round(float(mrp), 2),
                      prod_id, batch_key))
                st.success(f"✅ Stock updated — **{product_name.strip()}** qty: {old_qty} → **{new_qty}** | MRP ₹{mrp:,.2f}")
            else:
                run_write("""
                    INSERT INTO inventory_stock
                    (id, product_id, batch_no, quantity,
                     purchase_rate, selling_price, mrp,
                     stock_type, item_type,
                     is_active, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'SIMPLE','STOCK',true,NOW(),NOW())
                """, (
                    str(_uuid.uuid4()), prod_id, batch_key,
                    qty,
                    round(float(cost_price), 2),
                    round(float(selling_price), 2),
                    round(float(mrp), 2)
                ))
                st.success(f"✅ Frame added — **{product_name.strip()}** | Batch: {batch_key} | MRP ₹{mrp:,.2f} | Qty: {qty}")
        except Exception as ex:
            st.error(f"❌ Stock insert failed: {ex}")

    # ── Live frame stock preview ──────────────────────────────────────────────
    with st.expander("📋 Current Frame Stock", expanded=False):
        try:
            from modules.sql_adapter import run_query as _rq
            rows = _rq("""
                SELECT
                    p.product_name, p.brand,
                    s.batch_no,
                    s.quantity              AS qty,
                    s.purchase_rate         AS cost_price,
                    s.selling_price,
                    s.mrp,
                    p.gst_percent,
                    s.is_active
                FROM inventory_stock s
                JOIN products p ON p.id = s.product_id
                WHERE LOWER(p.category) = 'frame'
                  AND COALESCE(s.is_active, true) = true
                ORDER BY p.brand, p.product_name
                LIMIT 200
            """) or []
            if rows:
                import pandas as _pd
                df = _pd.DataFrame(rows)
                st.dataframe(df, use_container_width=True, hide_index=True,
                             column_config={
                                 "mrp":           st.column_config.NumberColumn("MRP ₹",           format="₹%.2f"),
                                 "cost_price":    st.column_config.NumberColumn("Cost ₹",          format="₹%.2f"),
                                 "selling_price": st.column_config.NumberColumn("Selling ₹",       format="₹%.2f"),
                                 "gst_percent":   st.column_config.NumberColumn("GST %",           format="%.0f%%"),
                                 "qty":           st.column_config.NumberColumn("Qty"),
                                 "is_active":     st.column_config.CheckboxColumn("Active"),
                             })
                st.caption(f"{len(rows)} frame(s) in stock")
            else:
                st.info("No frames in stock yet.")
        except Exception as ex:
            st.error(f"Could not load frame stock: {ex}")


def _do_add_download(file_type: str, cfg: dict):
    try:
        from modules.loaders.smart.download_manager import build_add_template, make_add_filename

        with st.spinner("Preparing template..."):
            excel_bytes = build_add_template(file_type)

        filename = make_add_filename(file_type)
        st.download_button(
            label     = f"💾 Save {cfg['icon']} {cfg['label']} Blank Template",
            data      = excel_bytes,
            file_name = filename,
            mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key       = f"save_add_{file_type}_{datetime.now().strftime('%H%M%S')}",
        )
        st.success("✅ Template ready. Fill in your data and upload.")

    except Exception as e:
        st.error(f"Template generation failed: {e}")


def _handle_add_upload(uploaded, file_type: str, cfg: dict):
    """ADD flow upload: guard → preview → confirm → run_loader_safe ADD mode."""
    from modules.loaders.smart.upload_guard import check_upload
    from modules.loaders.patches.loader_transaction_wrapper import run_loader_safe
    import tempfile, os

    file_bytes = uploaded.read()
    user       = _get_user()

    # Guard check
    with st.spinner("Verifying file..."):
        guard = check_upload(file_bytes, expected_type=file_type, user=user)

    if not guard.allowed:
        for issue in guard.issues:
            st.error(issue)
        return

    if guard.flow != "ADD":
        st.error("⛔ This looks like an EDIT file, not a blank template. Use the 'Edit Existing Records' tab instead.")
        return

    df = guard.df
    # Strip example row (orange row with "EXAMPLE ROW" text)
    if not df.empty:
        first_val = str(df.iloc[0, 0]).upper()
        if "EXAMPLE" in first_val or "DELETE" in first_val:
            df = df.iloc[1:].reset_index(drop=True)
            st.info("ℹ️ Example row detected and automatically removed.")

    if df.empty:
        st.warning("⚠️ No data rows found after removing example row.")
        return

    # Preview
    st.markdown("### 📋 Preview — New Records to Add")
    st.info(f"**{len(df)} new record(s)** ready to add. Review below before confirming.")
    st.dataframe(df.head(20), use_container_width=True, hide_index=True)
    if len(df) > 20:
        st.caption(f"Showing first 20 of {len(df)} rows.")

    # Confirm
    st.markdown("---")
    col1, col2 = st.columns(2)
    with col1:
        confirm = st.button(
            f"✅ Add {len(df)} Records to Database",
            type="primary",
            key=f"confirm_add_{file_type}_{uploaded.name}",
            use_container_width=True,
        )
    with col2:
        cancel = st.button(
            "❌ Cancel",
            key=f"cancel_add_{file_type}_{uploaded.name}",
            use_container_width=True,
        )

    if cancel:
        st.warning("❌ Cancelled. No records were added.")
        return

    if confirm:
        with st.spinner("Adding records..."):
            # Save to temp file for loader
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
            tmp.write(file_bytes)
            tmp.close()

            try:
                result = run_loader_safe(
                    file_path   = tmp.name,
                    mode        = "LIVE",
                    stock_mode  = "ADD",
                    force_type  = file_type,
                    user        = user,
                    skip_dedup  = False,
                )
                if result.inserted > 0 or result.updated > 0:
                    st.success(
                        f"✅ Done! {result.inserted} new record(s) added, "
                        f"{result.updated} existing record(s) updated."
                    )
                else:
                    st.warning(
                        f"⚠️ {result.skipped} row(s) skipped. "
                        "Records may already exist. Check errors below."
                    )
                if result.errors:
                    with st.expander("⚠️ Errors"):
                        for e in result.errors:
                            st.write(f"• {e}")
            except Exception as e:
                st.error(f"Import failed: {e}")
            finally:
                os.unlink(tmp.name)


# ══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _render_advice_box(advice):
    """Render the AI advisor summary with appropriate styling."""
    colour = {
        "PROCEED": "success",
        "REVIEW":  "warning",
        "STOP":    "error",
    }.get(advice.recommendation, "info")

    if colour == "success":
        st.success(advice.explanation)
    elif colour == "warning":
        st.warning(advice.explanation)
    else:
        st.error(advice.explanation)

    for w in advice.warnings:
        if w.startswith("⛔") or w.startswith("🔴"):
            st.error(w)
        elif w.startswith("⚠️") or w.startswith("🟡"):
            st.warning(w)
        else:
            st.info(w)

    if advice.field_advice:
        with st.expander("📖 Field-level guidance"):
            for fname, guidance in advice.field_advice.items():
                st.markdown(f"**{fname}:** {guidance}")


def _get_user() -> str:
    """Get current user from session state."""
    user = st.session_state.get("user", "system")
    if isinstance(user, dict):
        return user.get("username", user.get("name", "system"))
    return str(user) if user else "system"


# ══════════════════════════════════════════════════════════════════════════════
# SQL SETUP HELPER
# ══════════════════════════════════════════════════════════════════════════════

SETUP_SQL = """
-- Run this once in your database to enable the Smart Loader audit system

CREATE TABLE IF NOT EXISTS field_change_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id     UUID,
    file_type     TEXT,
    entity_id     UUID,
    entity_key    TEXT,
    field_name    TEXT,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    TEXT,
    changed_at    TIMESTAMPTZ DEFAULT NOW(),
    risk_level    TEXT,
    backup_id     UUID
);

CREATE TABLE IF NOT EXISTS field_change_backup (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    backup_id     UUID,
    file_type     TEXT,
    entity_id     UUID,
    entity_key    TEXT,
    snapshot      JSONB,
    backed_up_at  TIMESTAMPTZ DEFAULT NOW(),
    backed_up_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_fcl_import_id  ON field_change_log(import_id);
CREATE INDEX IF NOT EXISTS idx_fcl_entity_id  ON field_change_log(entity_id);
CREATE INDEX IF NOT EXISTS idx_fcl_changed_at ON field_change_log(changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_fcb_backup_id  ON field_change_backup(backup_id);
"""


# ══════════════════════════════════════════════════════════════════════════════
# MAIN GROUPS — GST % + HSN master
# ══════════════════════════════════════════════════════════════════════════════

def _render_main_groups():
    """Manage main_groups master — canonical GST% and HSN code per product group."""
    from modules.sql_adapter import run_query, run_write

    st.markdown(
        "Set the canonical **GST rate** and **HSN code** for each product group. "
        "When you upload a PRODUCT Excel with a blank GST% or HSN, these values are **auto-filled** from here. "
        "Products still store their own values — this only fills blanks on upload."
    )
    st.caption("⚠️ GST rates change by government notification. Confirm current rates with your CA before saving.")

    # ── Ensure table exists ───────────────────────────────────────────────────
    try:
        run_write("""
            CREATE TABLE IF NOT EXISTS main_groups (
                id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                name        TEXT         NOT NULL UNIQUE,
                gst_percent NUMERIC(5,2) NOT NULL DEFAULT 12,
                hsn_code    TEXT         NOT NULL DEFAULT '',
                description TEXT,
                created_at  TIMESTAMP    DEFAULT NOW(),
                updated_at  TIMESTAMP    DEFAULT NOW()
            )
        """)
    except Exception as e:
        st.error(f"Cannot create main_groups table: {e}")
        return

    rows = run_query(
        "SELECT id, name, gst_percent, hsn_code, description FROM main_groups ORDER BY name"
    ) or []

    GST_OPTIONS = [0, 5, 12, 18, 28]

    # ── Existing groups ───────────────────────────────────────────────────────
    if not rows:
        st.info("No groups yet. Add your first group below — confirm rates with your CA.")
    else:
        st.markdown(f"#### {len(rows)} Group{'s' if len(rows) != 1 else ''}")
        for row in rows:
            gst_val = int(row["gst_percent"])
            hsn_val = row["hsn_code"] or "—"
            rid     = str(row["id"])
            with st.expander(f"**{row['name']}** — {gst_val}% GST | HSN: {hsn_val}", expanded=False):
                c1, c2, c3, c4 = st.columns([3, 1, 2, 2])
                new_name = c1.text_input("Group Name", value=row["name"],          key="mg_name_" + rid)
                gst_idx  = GST_OPTIONS.index(gst_val) if gst_val in GST_OPTIONS else 2
                new_gst  = c2.selectbox("GST %", GST_OPTIONS, index=gst_idx,       key="mg_gst_"  + rid)
                new_hsn  = c3.text_input("HSN Code", value=row["hsn_code"] or "",  key="mg_hsn_"  + rid)
                new_desc = c4.text_input("Note",     value=row["description"] or "",key="mg_desc_" + rid)
                sc1, sc2 = st.columns([1, 5])
                if sc1.button("💾 Save", key="mg_save_" + rid):
                    try:
                        run_write(
                            "UPDATE main_groups SET name=%s, gst_percent=%s, hsn_code=%s, "
                            "description=%s, updated_at=NOW() WHERE id=%s",
                            (new_name.strip(), round(float(new_gst), 2), new_hsn.strip(), new_desc.strip(), rid)
                        )
                        st.success("✅ Saved.")
                        st.session_state["_mg_render_n"] = st.session_state.get("_mg_render_n", 0) + 1
                        st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")
                if sc2.button("🗑️ Delete", key="mg_del_" + rid):
                    try:
                        run_write("DELETE FROM main_groups WHERE id=%s", (rid,))
                        st.success("Deleted.")
                        st.session_state["_mg_render_n"] = st.session_state.get("_mg_render_n", 0) + 1
                        st.rerun()
                    except Exception as e:
                        st.error(f"Delete failed: {e}")

    # ── Add new group ─────────────────────────────────────────────────────────
    # _mg_render_n counter: bumped on every rerun so Streamlit sees fresh keys.
    # Without this, static keys like "mg_new_name" register twice on re-render → crash.
    _k = st.session_state.get("_mg_render_n", 0)
    st.markdown("---")
    st.markdown("#### ➕ Add New Group")
    a1, a2, a3, a4 = st.columns([3, 1, 2, 2])
    add_name = a1.text_input("Group Name", placeholder="e.g. Ophthalmic Lenses", key=f"mg_new_name_{_k}")
    add_gst  = a2.selectbox("GST %", GST_OPTIONS, index=2,                        key=f"mg_new_gst_{_k}")
    add_hsn  = a3.text_input("HSN Code", placeholder="e.g. 90015000",             key=f"mg_new_hsn_{_k}")
    add_desc = a4.text_input("Note", placeholder="optional",                      key=f"mg_new_desc_{_k}")

    if st.button("➕ Add Group", key=f"mg_add_btn_{_k}", type="primary"):
        if not add_name.strip():
            st.warning("Group name is required.")
        else:
            try:
                run_write(
                    "INSERT INTO main_groups (name, gst_percent, hsn_code, description) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                    (add_name.strip(), round(float(add_gst), 2), add_hsn.strip(), add_desc.strip())
                )
                st.success(f"✅ '{add_name}' added.")
                st.session_state["_mg_render_n"] = _k + 1   # bump → fresh keys next render
                st.rerun()
            except Exception as e:
                st.error(f"Add failed: {e}")

