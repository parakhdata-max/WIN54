from difflib import get_close_matches

# ─────────────────────────────────────────────
# TABLE-SPECIFIC SCHEMAS (NO CROSS-DOMAIN)
# ─────────────────────────────────────────────

TABLE_SCHEMAS = {
    "products": [
        "productname",
        "brand",
        "brandproductgroup",
        "maingroup",
        "type",
        "lenscategory",
        "index",
        "material",
        "coating",
        "coatingtype",
        "colour",
        "unit",
        "wearschedule",
        "gender",
        "boxsize",
        "allowloose",
        "isbatchapplicable",
        "iseyespecific",
        "isactive",
        "hsncode",
        "gstpercent",
    ],
    # ✅ inventory_stock — CLENS and OPHLENS both map here
    # iseyespecific is NOT in this schema — it belongs to products only.
    # eyeside IS the correct field for inventory_stock rows.
    "inventory_stock": [
        "productname",
        "sph",
        "cyl",
        "axis",
        "addpower",
        "eyeside",          # ← correct field for stock rows
        "batchno",
        "expirydate",
        "quantity",
        "purchaserate",
        "sellingprice",
        "mrp",
        "itemtype",
        "lensdesign",
        "location",
        "isactive",
    ],
}

CRITICAL_FIELDS = {
    "gstpercent",
    "hsncode",
    "mrp",
    "costprice",
    "sellingprice",
    "discount",
    "discountpercent",
    # ✅ Power fields are medical/financial — NEVER fuzzy-map these
    "sph",
    "cyl",
    "axis",
    "addpower",
    "eyeside",
}


# ─────────────────────────────────────────────
# TABLE DETECTOR
# ─────────────────────────────────────────────

def detect_table_context(df):
    cols = set(df.columns)

    # inventory_stock: has sph or batchno — detect BEFORE products
    # (contact lens files can also have productname/brand which would trigger products)
    if "sph" in cols or "batchno" in cols or "expirydate" in cols:
        return "inventory_stock"

    if "productname" in cols or "brand" in cols:
        return "products"

    return "unknown"


# ─────────────────────────────────────────────
# SAFE AI MAPPER
# ─────────────────────────────────────────────

def intelligent_ai_mapping(df, report=None):
    table = detect_table_context(df)

    if table == "unknown":
        return df  # no AI mapping for unknown tables

    valid_cols = TABLE_SCHEMAS[table]
    new_cols = []

    for col in df.columns:
        clean = col.lower()

        # NEVER remap critical fields — powers, financial, medical data
        if clean in CRITICAL_FIELDS:
            new_cols.append(clean)
            continue

        match = get_close_matches(clean, valid_cols, n=1, cutoff=0.85)

        if match:
            mapped = match[0]
            new_cols.append(mapped)

            if report and mapped != clean:
                report.add_warning(f"AI mapped '{col}' → '{mapped}'")

        else:
            new_cols.append(clean)

    df.columns = new_cols
    return df