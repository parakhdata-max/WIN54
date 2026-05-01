"""
modules/procurement/product_inventory_manager.py
=================================================
Two-screen UI for contact lens and ophthalmic product management:

SCREEN 1 — Product + Power Range Creator
    Create a product in products table AND generate inventory_stock
    placeholder rows for each power in the range in one go.
    No batch/expiry yet — stock rows created with qty=0, batch=NULL.
    Powers are ready to receive stock when batches arrive.

SCREEN 2 — Batch & Expiry Manager
    Select product → see all its power rows in inventory_stock
    → assign batch_no + expiry_date + qty + prices per power or in bulk
    → save. Designed for fast entry when a delivery arrives.
"""

import uuid
import streamlit as st
from typing import List, Optional


# ── DB helpers ────────────────────────────────────────────────────────────────

def _rq(sql: str, params=None) -> list:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


def _rw(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception as e:
        st.error(f"DB error: {e}")
        return False


# ── Power range generator ─────────────────────────────────────────────────────

def _gen_range(from_v: float, to_v: float, step_v: float) -> List[float]:
    """Generate power values from from_v to to_v inclusive at step_v."""
    if step_v <= 0:
        return []
    lo, hi = min(from_v, to_v), max(from_v, to_v)
    vals, n = [], int(round((hi - lo) / step_v)) + 1
    for i in range(n):
        v = round(lo + i * step_v, 2)
        if v <= hi + 0.001:
            vals.append(v)
    return vals


# ══════════════════════════════════════════════════════════════════════════════
# SCREEN 1 — PRODUCT + POWER RANGE CREATOR
# ══════════════════════════════════════════════════════════════════════════════

def _render_product_creator():
    st.markdown("### 📦 Product + Power Range Creator")
    st.caption(
        "Create a new product and define its full power range in one step. "
        "Inventory rows are created for each power with qty = 0. "
        "Add batches and stock later in the **Batch & Expiry** tab."
    )

    # ── Load reference data ───────────────────────────────────────────
    main_groups = _rq("""
        SELECT DISTINCT main_group FROM products
        WHERE main_group IS NOT NULL AND main_group != ''
        ORDER BY main_group
    """)
    mg_opts = [r["main_group"] for r in main_groups]

    suppliers = _rq("""
        SELECT id::text AS id, party_name FROM parties
        WHERE UPPER(party_type) IN ('SUPPLIER','VENDOR')
          AND COALESCE(is_active,TRUE) = TRUE
        ORDER BY party_name
    """)
    sup_opts   = ["— None —"] + [r["party_name"] for r in suppliers]
    sup_ids    = [None] + [r["id"] for r in suppliers]

    # ── Section A: Product details ────────────────────────────────────
    st.markdown("#### A. Product Details")

    col1, col2 = st.columns(2)
    with col1:
        prod_name  = st.text_input("Product Name *", key="pc_name",
                                    placeholder="e.g. AirOptix Aqua Monthly SPH")
        brand      = st.text_input("Brand *", key="pc_brand",
                                    placeholder="e.g. Alcon")
        main_group = st.selectbox("Main Group *", [""] + mg_opts,
                                   key="pc_mg")
        category   = st.text_input("Category", key="pc_cat",
                                    placeholder="e.g. Monthly Disposable")

    with col2:
        gst_pct    = st.number_input("GST %", min_value=0.0, max_value=28.0,
                                      value=12.0, step=1.0, key="pc_gst")
        sup_idx    = st.selectbox("Preferred Supplier", range(len(sup_opts)),
                                   format_func=lambda i: sup_opts[i],
                                   key="pc_sup")
        tat_days   = st.number_input("TAT Days", min_value=1, value=2,
                                      key="pc_tat")
        wear_sched = st.selectbox("Wear Schedule",
                                   ["", "Daily", "Weekly", "Bi-Weekly",
                                    "Monthly", "Quarterly", "Annual"],
                                   key="pc_wear")

    st.markdown("---")

    # ── Section B: Power range ────────────────────────────────────────
    st.markdown("#### B. Power Range")
    st.caption("Define the power range. Inventory rows will be created for each combination.")

    # SPH
    st.markdown("**SPH**")
    r1, r2, r3 = st.columns(3)
    sph_from = r1.number_input("From", step=0.25, format="%.2f", value=-6.0, key="pc_sph_from")
    sph_to   = r2.number_input("To",   step=0.25, format="%.2f", value=0.25, key="pc_sph_to")
    sph_step = r3.number_input("Step", step=0.25, format="%.2f", value=0.25,
                                min_value=0.25, key="pc_sph_step")

    # CYL (optional)
    st.markdown("**CYL** *(leave both 0.00 to skip)*")
    r4, r5, r6 = st.columns(3)
    cyl_from = r4.number_input("From", step=0.25, format="%.2f", value=0.0, key="pc_cyl_from")
    cyl_to   = r5.number_input("To",   step=0.25, format="%.2f", value=0.0, key="pc_cyl_to")
    cyl_step = r6.number_input("Step", step=0.25, format="%.2f", value=0.25,
                                min_value=0.25, key="pc_cyl_step")

    r7, r8 = st.columns(2)
    axis_val = r7.number_input("AXIS (fixed, only if CYL used)",
                                step=1, min_value=0, max_value=180,
                                value=0, key="pc_axis")
    add_val  = r8.number_input("ADD (fixed, 0 = not applicable)",
                                step=0.25, format="%.2f",
                                value=0.0, key="pc_add")

    eye_side = st.selectbox("Eye Side", ["B", "R", "L"], key="pc_eye",
                             help="B=Both (most contact lenses), R/L for eye-specific")

    # Generate preview
    sph_vals = _gen_range(sph_from, sph_to, sph_step)
    cyl_vals = (_gen_range(cyl_from, cyl_to, cyl_step)
                if (cyl_from != 0 or cyl_to != 0) else [None])
    total_powers = len(sph_vals) * len(cyl_vals)

    if total_powers > 0:
        st.info(
            f"📋 Will create **{total_powers} power rows** "
            f"({len(sph_vals)} SPH × {len(cyl_vals)} CYL). "
            f"All with qty = 0 until batches are added."
        )
    else:
        st.warning("⚠️ No values in range — check From/To/Step.")

    # Preview toggle
    if total_powers > 0:
        if st.checkbox(f"👁️ Preview all {total_powers} powers", key="pc_preview"):
            import pandas as _pd
            rows = []
            for sv in sph_vals:
                for cv in cyl_vals:
                    ax = axis_val if (cv and cv != 0) else None
                    ad = add_val  if add_val != 0 else None
                    rows.append({
                        "SPH":  f"{sv:+.2f}",
                        "CYL":  f"{cv:+.2f}" if cv else "—",
                        "AXIS": str(ax) if ax else "—",
                        "ADD":  f"{float(ad):+.2f}" if ad else "—",
                        "Eye":  eye_side,
                        "Qty":  0,
                    })
            st.dataframe(_pd.DataFrame(rows), use_container_width=True,
                         hide_index=True,
                         height=min(400, (total_powers + 1) * 36))

    st.markdown("---")

    # ── Create button ─────────────────────────────────────────────────
    if st.button(
        f"✅ Create Product + {total_powers} Power Rows",
        type="primary", use_container_width=True,
        key="pc_create_btn",
        disabled=(not prod_name.strip() or not brand.strip()
                  or not main_group or total_powers == 0),
    ):
        _create_product_with_powers(
            prod_name  = prod_name.strip(),
            brand      = brand.strip(),
            main_group = main_group,
            category   = category.strip() or None,
            gst_pct    = gst_pct,
            supplier_id= sup_ids[sup_idx],
            tat_days   = tat_days,
            wear_sched = wear_sched or None,
            sph_vals   = sph_vals,
            cyl_vals   = cyl_vals,
            axis_val   = axis_val,
            add_val    = add_val,
            eye_side   = eye_side,
        )


def _create_product_with_powers(
    prod_name, brand, main_group, category, gst_pct,
    supplier_id, tat_days, wear_sched,
    sph_vals, cyl_vals, axis_val, add_val, eye_side,
):
    # Check if product already exists
    existing = _rq(
        "SELECT id::text FROM products WHERE LOWER(product_name) = LOWER(%s)",
        (prod_name,)
    )

    if existing:
        pid = existing[0]["id"]
        st.info(f"Product '{prod_name}' already exists — adding any missing power rows.")
    else:
        # Create product
        pid = str(uuid.uuid4())
        ok = _rw("""
            INSERT INTO products
              (id, product_code, product_name, brand, main_group, category,
               wear_schedule, gst_percent, preferred_supplier_id, supplier_tat_days,
               is_active, created_at, updated_at, created_source)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s,
               %s::uuid, %s, TRUE, NOW(), NOW(), 'MANUAL_UI')
        """, (
            pid, str(uuid.uuid4()), prod_name, brand, main_group,
            category, wear_sched, gst_pct,
            supplier_id, tat_days,
        ))
        if not ok:
            st.error("Failed to create product.")
            return

    # Create inventory_stock power rows
    created = 0
    skipped = 0

    for sv in sph_vals:
        for cv in cyl_vals:
            ax = axis_val if (cv and cv != 0) else None
            ad = add_val  if add_val != 0 else None

            # Check if power row already exists (any batch)
            exists = _rq("""
                SELECT id FROM inventory_stock
                WHERE product_id = %s::uuid
                  AND COALESCE(sph,       0) = COALESCE(%s::numeric, 0)
                  AND COALESCE(cyl,       0) = COALESCE(%s::numeric, 0)
                  AND COALESCE(axis,      0) = COALESCE(%s::integer, 0)
                  AND COALESCE(add_power, 0) = COALESCE(%s::numeric, 0)
                  AND eye_side = %s
                  AND batch_no IS NULL
                LIMIT 1
            """, (pid, sv, cv, ax, ad, eye_side))

            if exists:
                skipped += 1
                continue

            ok = _rw("""
                INSERT INTO inventory_stock
                  (id, product_id, sph, cyl, axis, add_power, eye_side,
                   batch_no, quantity, stock_type, item_type,
                   is_active, created_at, updated_at)
                VALUES
                  (%s, %s::uuid, %s, %s, %s, %s, %s,
                   NULL, 0, 'BATCH', 'STOCK',
                   TRUE, NOW(), NOW())
            """, (
                str(uuid.uuid4()), pid,
                sv, cv if cv else None,
                ax, ad, eye_side,
            ))
            if ok:
                created += 1

    if created or skipped:
        st.success(
            f"✅ Product **{prod_name}** ready — "
            f"{created} power rows created"
            + (f", {skipped} already existed" if skipped else "")
            + f". Go to **Batch & Expiry** tab to add stock."
        )
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SCREEN 2 — BATCH & EXPIRY MANAGER
# ══════════════════════════════════════════════════════════════════════════════

def _render_batch_manager():
    st.markdown("### 📋 Batch & Expiry Manager")
    st.caption(
        "Select a product, pick a power from the dropdown, "
        "enter batch number + expiry date + quantity + prices, and save. "
        "Designed for fast entry when a delivery arrives."
    )

    # ── Product selector ──────────────────────────────────────────────
    products = _rq("""
        SELECT DISTINCT
            p.id::text      AS product_id,
            p.product_name,
            COALESCE(p.brand,'') AS brand,
            COALESCE(p.main_group,'') AS main_group
        FROM products p
        JOIN inventory_stock i ON i.product_id = p.id
        WHERE COALESCE(p.is_active, TRUE) = TRUE
        ORDER BY p.product_name
    """)

    if not products:
        st.info("No products with inventory rows found. Create a product first.")
        return

    prod_labels = [f"{p['product_name']} · {p['brand']}" for p in products]

    col_p, col_refresh = st.columns([4, 1])
    with col_p:
        sel_prod_idx = st.selectbox(
            "Product", range(len(products)),
            format_func=lambda i: prod_labels[i],
            key="bm_product"
        )
    with col_refresh:
        st.write("")
        st.write("")
        if st.button("🔄", key="bm_refresh", help="Refresh"):
            st.rerun()

    pid = products[sel_prod_idx]["product_id"]
    pname = products[sel_prod_idx]["product_name"]

    # ── Load all power rows for this product ──────────────────────────
    power_rows = _rq("""
        SELECT
            id::text                        AS stock_id,
            sph, cyl, axis, add_power, eye_side,
            batch_no, expiry_date::text     AS expiry_date,
            COALESCE(quantity, 0)           AS quantity,
            COALESCE(mrp, 0)                AS mrp,
            COALESCE(selling_price, 0)      AS selling_price,
            COALESCE(purchase_rate, 0)      AS purchase_rate,
            COALESCE(location, '')          AS location
        FROM inventory_stock
        WHERE product_id = %s::uuid
          AND COALESCE(is_active, TRUE) = TRUE
        ORDER BY sph, cyl, axis, add_power, batch_no NULLS FIRST
    """, (pid,))

    if not power_rows:
        st.warning("No power rows found. Create power range first.")
        return

    # Build power labels for dropdown
    def _power_label(r):
        pp = []
        if r.get("sph") is not None: pp.append(f"SPH {float(r['sph']):+.2f}")
        if r.get("cyl") is not None: pp.append(f"CYL {float(r['cyl']):+.2f}")
        if r.get("axis"):             pp.append(f"AX {int(r['axis'])}")
        if r.get("add_power"):        pp.append(f"ADD {float(r['add_power']):+.2f}")
        if r.get("eye_side"):         pp.append(f"Eye:{r['eye_side']}")
        batch = r.get("batch_no")
        qty   = int(r.get("quantity") or 0)
        lbl   = " | ".join(pp) if pp else "No power"
        if batch:
            lbl += f"  [Batch: {batch} · Qty: {qty}]"
        else:
            lbl += "  [No batch yet]"
        return lbl

    # ── Mode selector ─────────────────────────────────────────────────
    mode = st.radio(
        "Entry mode",
        ["Single power", "Bulk — same batch for all powers"],
        horizontal=True,
        key="bm_mode"
    )

    st.markdown("---")

    if mode == "Single power":
        _render_single_batch_entry(pid, pname, power_rows, _power_label)
    else:
        _render_bulk_batch_entry(pid, pname, power_rows, _power_label)


def _render_single_batch_entry(pid, pname, power_rows, _power_label):
    """Single power entry — select one power, fill details, save."""

    power_labels = [_power_label(r) for r in power_rows]

    sel_pow_idx = st.selectbox(
        "Select Power", range(len(power_rows)),
        format_func=lambda i: power_labels[i],
        key="bm_power_sel"
    )
    r = power_rows[sel_pow_idx]

    st.markdown("#### Batch Details")

    c1, c2, c3 = st.columns(3)
    batch_no   = c1.text_input("Batch No *",
                                value=r.get("batch_no") or "",
                                key="bm_batch",
                                placeholder="e.g. AO2025A01")
    expiry     = c2.text_input("Expiry Date (YYYY-MM-DD) *",
                                value=r.get("expiry_date") or "",
                                key="bm_expiry",
                                placeholder="2026-06-30")
    qty        = c3.number_input("Quantity *",
                                  min_value=0,
                                  value=int(r.get("quantity") or 0),
                                  key="bm_qty")

    c4, c5, c6, c7 = st.columns(4)
    mrp        = c4.number_input("MRP ₹",           min_value=0.0, step=0.50,
                                  value=float(r.get("mrp") or 0),
                                  key="bm_mrp")
    sell_price = c5.number_input("Selling Price ₹", min_value=0.0, step=0.50,
                                  value=float(r.get("selling_price") or 0),
                                  key="bm_sp")
    purch_rate = c6.number_input("Purchase Rate ₹", min_value=0.0, step=0.50,
                                  value=float(r.get("purchase_rate") or 0),
                                  key="bm_pr")
    location   = c7.text_input("Location",
                                value=r.get("location") or "",
                                key="bm_loc",
                                placeholder="e.g. FRIDGE-1")

    if st.button("💾 Save", type="primary", use_container_width=True,
                 key="bm_save_single"):
        if not batch_no.strip():
            st.error("Batch No is required.")
            return
        if not expiry.strip():
            st.error("Expiry Date is required.")
            return

        # If this power row has no batch (qty=0, batch=NULL) → update it
        # If it already has a batch → check if same batch (update) or new batch (insert)
        existing_batch = r.get("batch_no")

        if not existing_batch:
            # Update the placeholder row
            ok = _rw("""
                UPDATE inventory_stock
                   SET batch_no       = %s,
                       expiry_date    = %s::date,
                       quantity       = %s,
                       mrp            = %s,
                       selling_price  = %s,
                       purchase_rate  = %s,
                       location       = %s,
                       updated_at     = NOW()
                 WHERE id = %s::uuid
            """, (
                batch_no.strip(), expiry.strip(), qty,
                mrp or None, sell_price or None, purch_rate or None,
                location.strip() or None,
                r["stock_id"],
            ))
        elif existing_batch == batch_no.strip():
            # Update existing batch row
            ok = _rw("""
                UPDATE inventory_stock
                   SET expiry_date    = %s::date,
                       quantity       = %s,
                       mrp            = %s,
                       selling_price  = %s,
                       purchase_rate  = %s,
                       location       = %s,
                       updated_at     = NOW()
                 WHERE id = %s::uuid
            """, (
                expiry.strip(), qty,
                mrp or None, sell_price or None, purch_rate or None,
                location.strip() or None,
                r["stock_id"],
            ))
        else:
            # New batch for same power — insert new row
            ok = _rw("""
                INSERT INTO inventory_stock
                  (id, product_id, sph, cyl, axis, add_power, eye_side,
                   batch_no, expiry_date, quantity, mrp, selling_price,
                   purchase_rate, location, stock_type, item_type,
                   is_active, created_at, updated_at)
                VALUES
                  (%s, %s::uuid, %s, %s, %s, %s, %s,
                   %s, %s::date, %s, %s, %s,
                   %s, %s, 'BATCH', 'STOCK',
                   TRUE, NOW(), NOW())
            """, (
                str(uuid.uuid4()), pid,
                r.get("sph"), r.get("cyl"), r.get("axis"),
                r.get("add_power"), r.get("eye_side"),
                batch_no.strip(), expiry.strip(), qty,
                mrp or None, sell_price or None,
                purch_rate or None, location.strip() or None,
            ))

        if ok:
            st.success(
                f"✅ Saved — {pname} SPH "
                f"{float(r['sph']):+.2f} | Batch: {batch_no} | "
                f"Qty: {qty} | Expiry: {expiry}"
            )
            st.rerun()


def _render_bulk_batch_entry(pid, pname, power_rows, _power_label):
    """
    Bulk mode — one batch number + expiry applies to ALL powers.
    Operator enters qty per power in a table.
    Useful when a delivery arrives with the same batch across all powers.
    """
    st.markdown(
        "<div style='background:#0d1a0d;border:1px solid #22c55e;"
        "border-radius:8px;padding:10px 14px;margin-bottom:10px'>"
        "<b style='color:#4ade80'>📦 Bulk Mode</b>"
        "<span style='color:#6b7280;font-size:0.8rem;margin-left:8px'>"
        "Same batch number and expiry applied to all powers. "
        "Set quantity to 0 to skip a power.</span></div>",
        unsafe_allow_html=True
    )

    c1, c2, c3, c4 = st.columns(4)
    batch_no   = c1.text_input("Batch No *",     key="bm_bulk_batch",
                                placeholder="e.g. AO2025A01")
    expiry     = c2.text_input("Expiry Date *",  key="bm_bulk_expiry",
                                placeholder="2026-06-30")
    mrp        = c3.number_input("MRP ₹ (all)",  min_value=0.0, step=0.50,
                                  key="bm_bulk_mrp")
    sell_price = c4.number_input("Selling ₹ (all)", min_value=0.0, step=0.50,
                                  key="bm_bulk_sp")

    c5, c6 = st.columns(2)
    purch_rate = c5.number_input("Purchase ₹ (all)", min_value=0.0, step=0.50,
                                  key="bm_bulk_pr")
    location   = c6.text_input("Location",          key="bm_bulk_loc",
                                placeholder="e.g. FRIDGE-1")

    st.markdown("---")
    st.markdown("**Set quantity per power:**")
    st.caption("Only powers with qty > 0 will be saved.")

    # Show unique powers (deduplicated — ignore existing batch rows)
    seen = set()
    unique_powers = []
    for r in power_rows:
        key = (r.get("sph"), r.get("cyl"), r.get("axis"),
               r.get("add_power"), r.get("eye_side"))
        if key not in seen:
            seen.add(key)
            unique_powers.append(r)

    qty_inputs = {}
    cols = st.columns(3)
    for i, r in enumerate(unique_powers):
        pp = []
        if r.get("sph") is not None: pp.append(f"SPH {float(r['sph']):+.2f}")
        if r.get("cyl") is not None: pp.append(f"CYL {float(r['cyl']):+.2f}")
        if r.get("axis"):             pp.append(f"AX {int(r['axis'])}")
        if r.get("add_power"):        pp.append(f"ADD {float(r['add_power']):+.2f}")
        power_lbl = " | ".join(pp) if pp else "—"

        with cols[i % 3]:
            qty_inputs[i] = st.number_input(
                power_lbl, min_value=0, value=0,
                key=f"bm_bulk_qty_{i}"
            )

    # Summary
    total_qty = sum(qty_inputs.values())
    powers_with_qty = sum(1 for q in qty_inputs.values() if q > 0)
    if total_qty > 0:
        st.info(f"📋 Will save {powers_with_qty} powers | Total qty: {total_qty}")

    if st.button(
        f"💾 Save Batch — {powers_with_qty} powers · {total_qty} units",
        type="primary", use_container_width=True,
        key="bm_bulk_save",
        disabled=(not batch_no.strip() or not expiry.strip() or total_qty == 0)
    ):
        saved = 0
        for i, r in enumerate(unique_powers):
            qty = qty_inputs.get(i, 0)
            if qty == 0:
                continue

            # Find placeholder row (batch=NULL) or existing batch row
            placeholder = _rq("""
                SELECT id::text FROM inventory_stock
                WHERE product_id = %s::uuid
                  AND COALESCE(sph,       0) = COALESCE(%s::numeric, 0)
                  AND COALESCE(cyl,       0) = COALESCE(%s::numeric, 0)
                  AND COALESCE(axis,      0) = COALESCE(%s::integer, 0)
                  AND COALESCE(add_power, 0) = COALESCE(%s::numeric, 0)
                  AND eye_side = %s
                  AND batch_no IS NULL
                LIMIT 1
            """, (
                pid, r.get("sph"), r.get("cyl"),
                r.get("axis"), r.get("add_power"), r.get("eye_side")
            ))

            if placeholder:
                # Update placeholder
                ok = _rw("""
                    UPDATE inventory_stock
                       SET batch_no      = %s,
                           expiry_date   = %s::date,
                           quantity      = %s,
                           mrp           = %s,
                           selling_price = %s,
                           purchase_rate = %s,
                           location      = %s,
                           updated_at    = NOW()
                     WHERE id = %s::uuid
                """, (
                    batch_no.strip(), expiry.strip(), qty,
                    mrp or None, sell_price or None,
                    purch_rate or None, location.strip() or None,
                    placeholder[0]["id"],
                ))
            else:
                # Insert new row for this batch
                ok = _rw("""
                    INSERT INTO inventory_stock
                      (id, product_id, sph, cyl, axis, add_power, eye_side,
                       batch_no, expiry_date, quantity, mrp, selling_price,
                       purchase_rate, location, stock_type, item_type,
                       is_active, created_at, updated_at)
                    VALUES
                      (%s, %s::uuid, %s, %s, %s, %s, %s,
                       %s, %s::date, %s, %s, %s,
                       %s, %s, 'BATCH', 'STOCK',
                       TRUE, NOW(), NOW())
                """, (
                    str(uuid.uuid4()), pid,
                    r.get("sph"), r.get("cyl"), r.get("axis"),
                    r.get("add_power"), r.get("eye_side"),
                    batch_no.strip(), expiry.strip(), qty,
                    mrp or None, sell_price or None,
                    purch_rate or None, location.strip() or None,
                ))
            if ok:
                saved += 1

        if saved:
            st.success(
                f"✅ Saved batch **{batch_no}** — "
                f"{saved} powers · {total_qty} total units | "
                f"Expiry: {expiry}"
            )
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER — called from app.py or procurement UI
# ══════════════════════════════════════════════════════════════════════════════

def render_product_inventory_manager():
    st.title("🔬 Product & Inventory Manager")
    st.caption("Create products with power ranges · Manage batches and expiry dates")

    tab1, tab2 = st.tabs([
        "📦 Create Product + Power Range",
        "📋 Batch & Expiry Manager",
    ])

    with tab1:
        _render_product_creator()

    with tab2:
        _render_batch_manager()
