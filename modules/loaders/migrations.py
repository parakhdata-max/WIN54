"""
modules/loaders/migrations.py
================================
SINGLE SOURCE OF TRUTH for all DB schema changes.

Architecture:
  1. Developer adds a migration here (one ALTER TABLE per entry)
  2. App startup runs run_all_migrations() — idempotent, safe to repeat
  3. schema_sync detects new columns in DB → updates db_schema_registry.py
  4. download_manager reads registry → Excel download auto-includes new columns
  5. universal_loader reads registry column map → upload auto-writes new columns
  6. Deleted from registry → download/upload stop using that column

ADDING A NEW COLUMN — three steps only:
  Step 1: Add ALTER TABLE here under the right table
  Step 2: Add Col() entry in db_schema_registry.py
  Step 3: Deploy + restart — everything else is automatic

REMOVING A COLUMN — two steps:
  Step 1: Remove Col() from db_schema_registry.py
  Step 2: Deploy — download/upload stop using it (DB column stays, data preserved)
  (Optional Step 3: DROP COLUMN in DB when sure no data needed)
"""

import logging
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# MIGRATION REGISTRY
# Each entry: (table, column, sql_type, description)
# ADD here when you need a new column in ANY table.
# ══════════════════════════════════════════════════════════════════════════════

MIGRATIONS = [

    # ── products table ────────────────────────────────────────────────────────
    ("products", "shape",           "TEXT",         "Frame/lens shape"),
    ("products", "model",           "TEXT",         "Model number / variant"),
    ("products", "created_source",  "TEXT",         "How product was created (LOADER/MANUAL)"),
    ("products", "barcode",        "TEXT",         "Product Barcode for scanner billing (services, accessories)"),
    # Ensure gst_percent always has a default — ALTER TABLE ADD COLUMN may not inherit CREATE TABLE default
    # This is a SET DEFAULT statement, not ADD COLUMN — handled specially in runner below

    # ── inventory_stock table ─────────────────────────────────────────────────
    # Core columns (likely already exist from OPHLENS/CLENS loaders — IF NOT EXISTS is safe)
    ("inventory_stock", "stock_type",    "TEXT",            "BATCH or POWER — how stock is tracked"),
    ("inventory_stock", "item_type",     "TEXT",            "STOCK or RX — physical vs made-to-order"),
    ("inventory_stock", "lens_design",   "TEXT",            "SPHERICAL / TORIC / MULTIFOCAL"),
    ("inventory_stock", "location",      "TEXT",            "Storage location / box code"),
    ("inventory_stock", "selling_price", "NUMERIC(12,2)",   "Wholesale / trade price"),
    ("inventory_stock", "purchase_rate", "NUMERIC(12,2)",   "Cost / inward price"),
    ("inventory_stock", "mrp",           "NUMERIC(12,2)",   "Maximum retail price"),
    # Frame dimension columns — needed since frames now live in inventory_stock
    ("inventory_stock", "size_a",        "NUMERIC(6,1)",    "A measurement — lens width (mm)"),
    ("inventory_stock", "size_b",        "NUMERIC(6,1)",    "B measurement — lens height (mm)  NEW"),
    ("inventory_stock", "dbl",           "NUMERIC(6,1)",    "Distance between lenses (mm)"),
    ("inventory_stock", "temple_length", "NUMERIC(6,1)",    "Temple arm length (mm)"),
    ("inventory_stock", "base_material", "TEXT",            "Frame material (Plastic/Metal/TR90 etc.)"),
    ("inventory_stock", "finish",        "TEXT",            "Surface finish (Matt/Glossy)"),
    # Barcode + item code for scanner billing
    # barcode = what the scanner reads (e.g. 8711600085956 on Alcon box)
    # item_code       = alias for reporting/Tally (e.g. AOAQ-M200)
    ("inventory_stock", "barcode", "TEXT",  "Scanner barcode on individual box — unique per product+power"),
    ("inventory_stock", "item_code",       "TEXT",  "Item alias code for Tally/reporting (e.g. AOAQ-M200)"),
    # Frame-specific columns
    ("inventory_stock", "colour",        "TEXT",            "Primary colour (per SKU — can differ from product colour)"),
    ("inventory_stock", "shape",         "TEXT",            "Frame shape per SKU"),
    ("inventory_stock", "colour_mix",    "TEXT",            "Secondary / accent colour  NEW"),
    ("inventory_stock", "temple_colour", "TEXT",            "Temple arm colour  NEW"),
    ("inventory_stock", "frame_group",   "TEXT",            "Dynamic group tag (Near Dead, Sale, Premium etc.)  NEW"),
    ("inventory_stock", "expiry_date",   "DATE",            "Expiry date (contact lenses / solutions)"),

    # ── parties table ─────────────────────────────────────────────────────────
    ("parties", "barcode",         "TEXT",         "Party barcode — scan sticker to auto-fill party in billing/PO/challan"),
    ("parties", "customer_no",     "TEXT",         "Unique customer number — auto-assigned (CUST000001). Never changes."),
    ("parties", "credit_limit",    "NUMERIC(12,2) DEFAULT 0", "Credit limit for ON_ACCOUNT parties"),
    ("parties", "credit_days",     "INTEGER DEFAULT 0",        "Credit period in days"),
    ("parties", "billing_category","TEXT DEFAULT 'ON_COMPLETION'",
                "Payment category: ON_COMPLETION | FULL_ADVANCE | ADVANCE_BALANCE | PRE_PAYMENT | ON_ACCOUNT | DIRECT_INVOICE"),
    ("parties", "pan_no",          "TEXT",         "PAN number"),
    ("parties", "tan_no",          "TEXT",         "TAN number"),
    ("parties", "cin_no",          "TEXT",         "CIN number"),
    ("parties", "opening_balance", "NUMERIC(14,2)","Opening balance amount"),
    ("parties", "balance_type",    "TEXT",         "DR or CR"),
    ("parties", "tally_group",     "TEXT",         "Tally ledger group"),
    ("parties", "notes",           "TEXT",         "Free-form notes"),
    ("parties", "state_code",      "TEXT",         "2-digit state code for GST"),
    ("parties", "print_with_powers", "BOOLEAN DEFAULT TRUE", "Show lens powers on invoice/challan"),
    ("parties", "invoice_note",   "TEXT",         "Custom footer note on invoice"),
    ("parties", "alt_mobile",      "TEXT",         "Alternate mobile number"),

    # ── orders table ──────────────────────────────────────────────────────────
    ("orders", "is_converted", "BOOLEAN DEFAULT FALSE", "Consultation converted to billing order"),
    ("orders", "expected_supply_date", "DATE", "Expected date of supply — set in backoffice"),
    ("orders", "expected_supply_window", "TEXT", "Planned supply time window — rule based / backoffice"),
    ("orders", "cs_expected_supply_date", "DATE", "Customer-service updated expected supply date"),
    ("orders", "cs_expected_supply_window", "TEXT", "Customer-service updated supply time window"),

    # ── Supplier fulfillment automation ─────────────────────────────────────────
    ("products", "preferred_supplier_id", "UUID",
     "FK → parties.id — default supplier for this product"),
    ("products", "supplier_tat_days",     "INTEGER DEFAULT 1",
     "Default TAT (days) from preferred supplier for this product"),
    # NOTE: min_stock_qty, reorder_enabled, auto_fulfillment are power-specific
    # and live in product_stock_minimum table — not on products.

    # ── Supplier scheduling ───────────────────────────────────────────────────
    ("parties", "supplier_closed_days",   "TEXT[]",
     "Days supplier does not process orders e.g. {Sunday,Saturday}"),
    ("parties", "order_cutoff_time",      "TIME",
     "Order cutoff time — orders after this time count as next business day e.g. 15:00:00"),

    # ── Supplier orders tracking ──────────────────────────────────────────────
    ("supplier_orders", "source_order_ids", "TEXT[]",
     "Array of sales order IDs that triggered this PO — used for dedup on auto-populate"),
    ("supplier_orders", "expected_delivery_date", "DATE",
     "Calculated expected delivery based on TAT + supplier schedule"),
    # order_no is printed as barcode on job card / challan / dispatch slip
    # Scan → lookup by order_no → pulls full order + status

    # ── patients table ────────────────────────────────────────────────────────
    ("patients", "is_temporary",   "BOOLEAN",      "Walk-in / temporary record"),
    ("patients", "record_no",      "TEXT",         "Case / record number"),

    # ── blank_inventory table ─────────────────────────────────────────────────
    ("blank_inventory", "min_stock",   "INTEGER",      "Minimum stock alert level"),
    ("blank_inventory", "cost_price",  "NUMERIC(12,2)","Cost per piece"),
    ("blank_inventory", "batch_no",    "TEXT",         "Batch/lot number"),
    ("blank_inventory", "location",    "TEXT",         "Storage location"),
    ("blank_inventory", "barcode",     "TEXT",         "Scanner barcode for this blank — staff scans to auto-select in job card"),
    ("blank_inventory", "item_code",   "TEXT",         "Item alias for reporting / Tally"),
]


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER  — called from app.py on every startup
# ══════════════════════════════════════════════════════════════════════════════

def run_all_migrations(silent: bool = True) -> dict:
    """
    Execute all pending migrations.
    Uses IF NOT EXISTS — completely safe to run on every app startup.

    Returns:
        {"applied": [...], "skipped": [...], "errors": [...]}
    """
    try:
        from modules.sql_adapter import run_write
    except ImportError:
        logger.warning("[migrations] sql_adapter not available — skipping")
        return {"applied": [], "skipped": [], "errors": ["sql_adapter unavailable"]}

    applied = []
    skipped = []
    errors  = []

    for table, column, sql_type, description in MIGRATIONS:
        col_id = f"{table}.{column}"
        try:
            run_write(
                f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {sql_type}'
            )
            applied.append(col_id)
            logger.debug(f"[migrations] OK: {col_id}")
        except Exception as ex:
            err_str = str(ex).lower()
            if "already exists" in err_str:
                skipped.append(col_id)
            else:
                errors.append(f"{col_id}: {ex}")
                logger.warning(f"[migrations] FAILED {col_id}: {ex}")

    # ── Fix gst_percent DEFAULT ────────────────────────────────────────────────
    try:
        run_write("ALTER TABLE products ALTER COLUMN gst_percent SET DEFAULT 12")
        run_write("UPDATE products SET gst_percent = 12 WHERE gst_percent IS NULL")
        logger.info("[migrations] gst_percent DEFAULT 12 ensured")
    except Exception as ex:
        logger.warning(f"[migrations] gst_percent default fix failed: {ex}")

    # ── Unique constraints — barcode/alias must be unique like Tally ──────────
    # CREATE UNIQUE INDEX IF NOT EXISTS is idempotent and safe to run every time.
    _unique_indexes = [
        # Barcode unique per table
        ("uq_products_barcode",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_products_barcode "
         "ON products (barcode) WHERE barcode IS NOT NULL AND barcode <> ''"),

        ("uq_inventory_barcode",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_inventory_barcode "
         "ON inventory_stock (barcode) WHERE barcode IS NOT NULL AND barcode <> ''"),

        ("uq_blank_barcode",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_blank_barcode "
         "ON blank_inventory (barcode) WHERE barcode IS NOT NULL AND barcode <> ''"),

        ("uq_parties_barcode",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_parties_barcode "
         "ON parties (barcode) WHERE barcode IS NOT NULL AND barcode <> ''"),

        # item_code unique per table
        ("uq_inventory_item_code",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_inventory_item_code "
         "ON inventory_stock (item_code) WHERE item_code IS NOT NULL AND item_code <> ''"),

        # batch_no is a lookup key, not a unique identity. Contact lens and
        # ophthalmic imports can legitimately split one supplier batch across
        # powers/eyes/expiry rows, so uniqueness here blocks valid stock data.
        ("idx_inventory_batch_per_product",
         "CREATE INDEX IF NOT EXISTS idx_inventory_batch_per_product "
         "ON inventory_stock (product_id, batch_no) WHERE batch_no IS NOT NULL"),

        # product_name unique in products (already enforced by ON CONFLICT but make explicit)
        ("uq_products_name",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_products_name "
         "ON products (LOWER(TRIM(product_name)))"),

        # party_name unique in parties
        ("uq_parties_name",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_parties_name "
         "ON parties (LOWER(TRIM(party_name)))"),

        # Customer number — unique, never changes once assigned
        ("uq_parties_customer_no",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_parties_customer_no "
         "ON parties (customer_no) WHERE customer_no IS NOT NULL AND customer_no <> ''"),

        # Patient: name+mobile composite unique
        # Same name + same mobile = same person (return visit)
        # Same name + no mobile = needs suffix (Ramesh Gadhvi-2)
        # Different name + same mobile = family member (ok, new patient)
        ("uq_patients_name_mobile",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_name_mobile "
         "ON patients (LOWER(TRIM(master_name)), COALESCE(TRIM(mobile),'')) "
         "WHERE master_name IS NOT NULL"),

        # Patient barcode unique
        ("uq_patients_barcode",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_barcode "
         "ON patients (barcode) WHERE barcode IS NOT NULL AND barcode <> ''"),
    ]

    # ── Create new tables if not exist ───────────────────────────────────────
    _new_tables = [
        (
            "product_stock_minimum",
            """
            CREATE TABLE IF NOT EXISTS product_stock_minimum (
                id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id       UUID NOT NULL REFERENCES products(id),
                sph              NUMERIC(6,2),
                cyl              NUMERIC(6,2),
                axis             INTEGER,
                add_power        NUMERIC(6,2),
                eye_side         TEXT DEFAULT 'B',
                min_qty          INTEGER NOT NULL DEFAULT 1,
                reorder_qty      INTEGER NOT NULL DEFAULT 1,
                auto_fulfillment BOOLEAN DEFAULT FALSE,
                reorder_enabled  BOOLEAN DEFAULT FALSE,
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                updated_at       TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (product_id, sph, cyl, axis, add_power, eye_side)
            )
            """
        ),
        (
            "product_stock_minimum_advisory_cols",
            """
            DO $$
            BEGIN
                -- system_suggested_min: what data says min_qty should be
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='product_stock_minimum'
                      AND column_name='system_suggested_min'
                ) THEN
                    ALTER TABLE product_stock_minimum
                    ADD COLUMN system_suggested_min  INTEGER,
                    ADD COLUMN suggested_reorder_qty INTEGER,
                    ADD COLUMN avg_daily_sales       NUMERIC(8,3),
                    ADD COLUMN last_advisory_at      TIMESTAMPTZ,
                    ADD COLUMN advisory_accepted      BOOLEAN DEFAULT FALSE,
                    ADD COLUMN auto_order_enabled     BOOLEAN DEFAULT FALSE;
                END IF;
            END$$;
            """
        ),
        (
            "supplier_product_override",
            """
            CREATE TABLE IF NOT EXISTS supplier_product_override (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id          UUID NOT NULL REFERENCES products(id),
                override_supplier_id UUID NOT NULL REFERENCES parties(id),
                reason              TEXT,
                is_active           BOOLEAN DEFAULT TRUE,
                created_by          TEXT,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            )
            """
        ),
        (
            "reorder_log",
            """
            CREATE TABLE IF NOT EXISTS reorder_log (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id          UUID NOT NULL REFERENCES products(id),
                supplier_id         UUID REFERENCES parties(id),
                source_order_id     TEXT,
                supplier_order_id   INTEGER REFERENCES supplier_orders(id),
                triggered_at        TIMESTAMPTZ DEFAULT NOW(),
                expected_delivery   DATE,
                status              TEXT DEFAULT 'OPEN',
                resolved_at         TIMESTAMPTZ,
                notes               TEXT
            )
            """
        ),
    ]

    for tbl_name, tbl_sql in _new_tables:
        try:
            run_write(tbl_sql)
            logger.debug(f"[migrations] table OK: {tbl_name}")
        except Exception as ex:
            logger.warning(f"[migrations] table {tbl_name}: {ex}")
            errors.append(f"table {tbl_name}: {ex}")

    try:
        run_write("DROP INDEX IF EXISTS uq_inventory_batch_per_product")
        logger.debug("[migrations] removed obsolete strict batch uniqueness index")
    except Exception as ex:
        logger.warning(f"[migrations] obsolete batch unique index cleanup failed: {ex}")

    for idx_name, idx_sql in _unique_indexes:
        try:
            run_write(idx_sql)
            logger.debug(f"[migrations] index OK: {idx_name}")
        except Exception as ex:
            logger.warning(f"[migrations] index {idx_name}: {ex}")

    summary = (
        f"[migrations] Done — "
        f"{len(applied)} applied, {len(skipped)} already existed, {len(errors)} errors"
    )
    logger.info(summary)

    if not silent and errors:
        import streamlit as st
        for e in errors:
            st.warning(f"⚠️ Migration issue: {e}")

    return {"applied": applied, "skipped": skipped, "errors": errors}
