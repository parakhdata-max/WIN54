"""
modules/loaders/db_schema_registry.py
=======================================
SINGLE SOURCE OF TRUTH — DV ERP Loader System

Every column definition for every importable table lives HERE.
The uploader (universal_loader_core), downloader (data_downloader),
UI column mapping panel (loader_ui), and blank template generator
all READ from this file.

When a new column is added to the DB:
  1. Add it to DB_SCHEMA below (one line)
  2. All three layers (upload / download / template) pick it up automatically
  3. Run: python generate_blank_template.py  (to regenerate blank CSV/XLSX templates)

Column definition fields:
  excel_header  : exact column name used in Excel / CSV templates
  db_column     : exact column name in PostgreSQL DB
  db_type       : DB data type ('text','numeric','integer','boolean','date','uuid')
  required      : True = must be present for import to succeed
  writable      : False = system-managed (id, created_at etc.) — excluded from import
  description   : shown in Guide sheet and column mapping panel
  example       : example value shown in blank template
  allowed_values: list of valid values (for dropdowns/validation), or None
  default       : default value injected when column is missing in Excel (or None)
  download      : True = included in downloader SELECT query
  notes         : extra notes shown in schema reference
"""

from typing import List, Optional, Dict, Any


# ─── Column descriptor ────────────────────────────────────────────────────────

class Col:
    __slots__ = (
        "excel_header", "db_column", "db_type", "required", "writable",
        "description", "example", "allowed_values", "default",
        "download", "notes",
    )

    def __init__(
        self,
        excel_header: str,
        db_column: str,
        db_type: str,
        required: bool = False,
        writable: bool = True,
        description: str = "",
        example: str = "",
        allowed_values: Optional[List[str]] = None,
        default: Any = None,
        download: bool = True,
        notes: str = "",
    ):
        self.excel_header   = excel_header
        self.db_column      = db_column
        self.db_type        = db_type
        self.required       = required
        self.writable       = writable
        self.description    = description
        self.example        = example
        self.allowed_values = allowed_values
        self.default        = default
        self.download       = download
        self.notes          = notes

    def __repr__(self):
        return f"Col({self.excel_header!r} → {self.db_column!r})"


# ═══════════════════════════════════════════════════════════════════════════════
# DB SCHEMA — all importable tables
# ═══════════════════════════════════════════════════════════════════════════════

DB_SCHEMA: Dict[str, List[Col]] = {

    # ── PRODUCT MASTER  (table: products) ────────────────────────────────────
    "PRODUCT": [
        Col("Product",            "product_name",         "text",    required=True,  description="Full product name",                        example="Essilor Crizal Alize"),
        Col("MainGroup",          "main_group",           "text",    description="Top-level category",                       example="Ophthalmic Lenses"),
        Col("Type",               "category",             "text",    description="Sub-category / type",                      example="Single Vision"),
        Col("LensCategory",       "lens_category",        "text",    description="Lens category",                            example="CR39"),
        Col("Brand",              "brand",                "text",    description="Brand name",                               example="Essilor"),
        Col("BrandProductGroup",  "brand_group",          "text",    description="Brand product group",                      example="Crizal"),
        Col("Material",           "material",             "text",    description="Lens material",                            example="CR39"),
        Col("Index",              "index_value",          "numeric", description="Refractive index",                         example="1.50"),
        Col("Coating",            "coating",              "text",    description="Coating type",                             example="AR"),
        Col("coating_type",       "coating_type",         "text",    description="Coating subtype",                          example="Anti-Reflective"),
        Col("Colour",             "colour",               "text",    description="Lens colour / tint",                       example="Clear"),
        Col("Gender",             "gender",               "text",    description="Gender target",                            example="Unisex",  allowed_values=["Unisex", "Male", "Female"]),
        Col("WearSchedule",       "wear_schedule",        "text",    description="Wear schedule (contact lenses)",           example="Daily",   allowed_values=["Daily", "Monthly", "Annual", "Quarterly"]),
        # pack_size/uom_entry/uom_db removed — columns dropped from DB in fix_pack_size_column.sql
        Col("unit",               "unit",                 "text",    description="Unit of measure",                         example="PCS",     default="PCS"),
        Col("IsBatchApplicable",  "is_batch_applicable",  "boolean", description="Does this product use batch tracking?",    example="NO",      allowed_values=["YES", "NO"], default=False),
        Col("IsEyeSpecific",      "is_eye_specific",      "boolean", description="Is stock tracked per eye (R/L)?",          example="YES",     allowed_values=["YES", "NO"], default=False),
        Col("HSNCode",            "hsn_code",             "text",    description="HSN/SAC code for GST",                     example="900150"),
        Col("Box Size",           "box_size",             "integer", description="Units per box",                            example="1",       default=1),
        Col("Allow Loose",        "allow_loose",          "boolean", description="Can be sold as individual piece?",         example="YES",     allowed_values=["YES", "NO"], default=True),
        Col("GSTPercent",         "gst_percent",          "numeric", description="GST rate (%)",                             example="12"),
        Col("Barcode",            "barcode",             "text",    description="Product Barcode — for scanner billing", example="SVC-FIT-001",
            notes="Set for services (fitting, colouring) and any product scanned at billing"),
        Col("IsActive",           "is_active",            "boolean", description="Active in system?",                        example="YES",     allowed_values=["YES", "NO"], default=True),
        Col("PreferredSupplier",  "preferred_supplier_id", "text",    description="Supplier party name — resolved to party id by loader", example="Alcon India",
            notes="Set to enable auto supplier order population and auto-fulfillment pipeline"),
        Col("SupplierTATDays",    "supplier_tat_days",      "integer", description="Days from order to delivery for preferred supplier", example="2", default=1),
        # NOTE: AutoFulfillment, MinStockQty, ReorderEnabled are set per-power
        # in the Stock Minimum Manager UI (Procurement → Reorder Monitor)
        # not via the product loader.
        Col("Created Source", "created_source", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Model", "model", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Sku Code", "sku_code", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Auto Fulfillment", "auto_fulfillment", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Min Stock Qty", "min_stock_qty", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Reorder Enabled", "reorder_enabled", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Base Curve", "base_curve", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Diameter", "diameter", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Dk Value", "dk_value", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Dk T Value", "dk_t_value", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Water Content", "water_content", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Ct Value", "ct_value", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Modulus", "modulus", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Replacement Schedule", "replacement_schedule", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Uv Blocking", "uv_blocking", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Box Description", "box_description", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Modulus Mpa", "modulus_mpa", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Online Price", "online_price", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Online Active", "online_active", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Online Sort", "online_sort", "integer", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Online Tags", "online_tags", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Online Desc", "online_desc", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Online Badge", "online_badge", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Normal Procurement Discount Pct", "normal_procurement_discount_pct", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Scheme Procurement Discount Pct", "scheme_procurement_discount_pct", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Discount Percent", "discount_percent", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Is Gst Exempt", "is_gst_exempt", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        # System cols — not writable via import
        Col("",  "id",            "uuid",      writable=False, download=False),
        Col("",  "product_code",  "text",      writable=False, download=False, notes="Auto-generated UUID"),
        Col("",  "created_at",    "timestamp", writable=False, download=False),
        Col("",  "updated_at",    "timestamp", writable=False, download=False),
    ],

    # ── FRAME STOCK  (table: inventory_stock, JOIN products WHERE category=Frame)
    # Key: batch_no = Barcode. All frame-specific columns live in inventory_stock.
    # GST% lives on products table — not per-batch here.
    "FRAME": [
        Col("Barcode",       "batch_no",       "text",    required=True,  description="Unique Barcode — dedup key",         example="D10001"),
        Col("Product",       "product_name",   "text",    required=True,  description="Full product name",                   example="Butler 8305 Black",
            notes="Matches to products.product_name WHERE category=Frame"),
        Col("Brand",         "brand",          "text",    description="Frame brand",                         example="Parakh"),
        Col("Colour",        "colour",         "text",    description="Primary frame colour",                example="Black"),
        Col("ColourMix",     "colour_mix",     "text",    description="Secondary / accent colour",           example="Gold"),
        Col("TempleColour",  "temple_colour",  "text",    description="Temple arm colour",                   example="Silver"),
        Col("BaseMaterial",  "base_material",  "text",    description="Frame material",                      example="Plastic",
            notes="Stored on products.material"),
        Col("shape",         "shape",          "text",    description="Frame shape",                         example="Square",
            notes="Stored on products.shape"),
        Col("Finish",        "finish",         "text",    description="Surface finish (Matt/Glossy)",        example="Matt"),
        Col("ASize",         "size_a",         "numeric", description="A measurement — lens width (mm)",     example="52"),
        Col("BSize",         "size_b",         "numeric", description="B measurement — lens height (mm)",    example="38"),
        Col("DBL",           "dbl",            "numeric", description="Distance between lenses (mm)",        example="18"),
        Col("TempleLength",  "temple_length",  "numeric", description="Temple arm length (mm)",              example="135"),
        Col("StartCode",     "location",       "text",    description="Box / tray location code",            example="D1"),
        Col("FrameGroup",    "frame_group",    "text",    description="Dynamic group tag for pricing/filtering", example="Near Dead",
            notes="Examples: Near Dead, Sale, Premium, Kids, New Arrival — leave blank for standard"),
        Col("Qty",           "quantity",       "numeric", required=True,  description="Stock quantity",      example="1"),
        Col("Purchase price","purchase_rate",  "numeric", description="Cost / inward price ₹",              example="400.00"),
        Col("selling_price", "selling_price",  "numeric", description="Wholesale / trade price ₹",          example="650.00"),
        Col("MRP",           "mrp",            "numeric", required=True,  description="Retail / sticker price ₹", example="790.00"),
        Col("IsActive",      "is_active",      "boolean", description="Active in system?",                   example="YES",
            allowed_values=["YES","NO","Y","N","1","0"], default=True),
        Col("Qty", "qty", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Image Path", "image_path", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Frame Type", "frame_type", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Frame Seq", "frame_seq", "integer", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        # System — not in Excel
        Col("", "id",         "uuid",      writable=False, download=False),
        Col("", "created_at", "timestamp", writable=False, download=False),
        Col("", "updated_at", "timestamp", writable=False, download=False),
        Col("", "product_id", "uuid",      writable=False, download=False, notes="FK to products.id — resolved by loader"),
        Col("", "stock_type", "text",      writable=False, download=False, notes="Always BATCH for frames"),
    ],

    # ── PARTY MASTER  (table: parties) ───────────────────────────────────────
    "PARTY": [
        # ── Identity ──────────────────────────────────────────────────────────
        Col("PARTYNAME",      "party_name",     "text",    required=True,
            description="Full party/company name — conflict key if no mobile",
            example="Shri Lenses Pvt Ltd"),
        Col("Barcode",        "barcode",        "text",
            description="Party barcode — scan sticker to identify party at billing/PO/challan/dispatch",
            example="PARTY-SLP-001",
            notes="Print as barcode sticker and stick on party file/account card"),
        Col("CustomerNo",     "customer_no",    "text",
            description="Unique customer number — auto-assigned (CUST000001). Never changes. Use for wholesale account tracking.",
            example="CUST000001",
            notes="Set once on first load. Used as permanent account ID across Tally, ERP, billing."),
        Col("ROLETYPE",       "party_type",     "text",
            description="Party role / ledger type",  example="Supplier",
            allowed_values=["Retail","Doctor","Optician","Supplier","Fitter","Wholesale"]),
        Col("MOBILE",         "mobile",         "text",
            description="Primary mobile — conflict key", example="9876543210"),
        Col("ALTMOBILE",      "alt_mobile",     "text",
            description="Alternate / WhatsApp number",  example="9876543211"),
        Col("EMAIL",          "email",          "text",
            description="Email address",               example="contact@shrilenses.com"),
        Col("CONTACTPERSON",  "contact_person", "text",
            description="Name of contact person",      example="Ramesh Kumar"),
        # ── Address ───────────────────────────────────────────────────────────
        Col("ADDRESS",        "address",        "text",
            description="Full billing address",        example="123 MG Road, Dharampeth"),
        Col("CITY",           "city",           "text",    description="City",  example="Nagpur"),
        Col("AREA",           "area",           "text",    description="Area / locality",  example="Dharampeth"),
        Col("PINCODE",        "pincode",        "text",    description="6-digit PIN code", example="440010"),
        Col("STATE",          "state_name",     "text",    description="State name",       example="Maharashtra"),
        Col("STATECODE",      "state_code",     "text",    description="2-digit GST state code", example="27"),
        # ── GST / Compliance ─────────────────────────────────────────────────
        Col("GSTIN",          "gstin",          "text",
            description="15-character GSTIN",          example="27AABCU9603R1ZX"),
        Col("PAN",            "pan_no",         "text",
            description="10-char PAN number",          example="AABCU9603R"),
        Col("TAN",            "tan_no",         "text",
            description="TAN number (if applicable)",  example="PNEA00101B"),
        Col("CIN",            "cin_no",         "text",
            description="CIN for companies",           example="U12345MH2000PTC123456"),
        Col("GSTRATE",        "gst_rate",       "numeric",
            description="Default GST rate (%)",        example="18",
            allowed_values=["0","5","12","18","28"],    default=0),
        # ── Credit ────────────────────────────────────────────────────────────
        Col("CREDITLIMIT",    "credit_limit",   "numeric",
            description="Credit limit in ₹",          example="50000", default=0),
        Col("CREDITDAYS",     "credit_days",    "integer",
            description="Credit period in days",       example="30",    default=0),
        Col("BILLINGCAT",    "billing_category", "text",
            description="Payment category (ON_COMPLETION/FULL_ADVANCE/ON_ACCOUNT etc)",
            example="ON_ACCOUNT", default="ON_COMPLETION"),
        Col("OPENINGBALANCE", "opening_balance","numeric",
            description="Opening balance in ₹",       example="0",     default=0),
        Col("BALANCETYPE",    "balance_type",   "text",
            description="Dr (receivable) or Cr (payable)", example="Dr",
            allowed_values=["Dr","Cr"], default="Dr"),
        # ── Tally / ERP ───────────────────────────────────────────────────────
        Col("TALLYGROUP",     "tally_group",    "text",
            description="Tally ledger group",          example="Sundry Debtors"),
        Col("NOTES",          "notes",          "text",
            description="Internal notes",              example="Preferred vendor for lenses"),
        # ── Status ────────────────────────────────────────────────────────────
        Col("ISACTIVE",       "is_active",      "boolean",
            description="Active in system?",           example="YES",
            allowed_values=["YES","NO"],                default=True),
           Col("Billing Preference", "billing_preference", "text",
            description="CHALLAN = challan-first then invoice later (default). DIRECT_INVOICE = wholesale party gets invoice immediately (no challan step). Retail orders always use CHALLAN regardless of this field.",
            example="CHALLAN",
            allowed_values=["CHALLAN", "DIRECT_INVOICE"],
            default="CHALLAN",
            download=True),
        # ^ auto-added by schema_sync on startup
        Col("Payment Mode", "payment_mode", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Print With Powers", "print_with_powers", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Invoice Note", "invoice_note", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Supplier Closed Days", "supplier_closed_days", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Order Cutoff Time", "order_cutoff_time", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Price Tier", "price_tier", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Portal Password", "portal_password", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Doc Preference", "doc_preference", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Requires Payment Before Invoice", "requires_payment_before_invoice", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Preferred Courier Provider Id", "preferred_courier_provider_id", "uuid", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Preferred Courier Name", "preferred_courier_name", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Whatsapp", "whatsapp", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        # System — not writable via import
        Col("", "id",         "uuid",      writable=False, download=False),
        Col("", "status",     "boolean",   writable=False, download=False,
            notes="Legacy boolean mirror of is_active — never import directly"),
        Col("", "created_at", "timestamp", writable=False, download=False),
    ],

    # ── PATIENT DATA  (tables: patients + patient_visits joined) ─────────────
    "PATIENT": [
        Col("Client Name",     "master_name",  "text",    required=True,  description="Patient full name",           example="Ramesh Kumar"),
        Col("Mobile Number",   "mobile",       "text",    description="Mobile — conflict key (or use Record No)",  example="9876543210"),
        Col("Record No",       "record_no",    "text",    description="Record number — alternate conflict key",    example="P00123"),
        Col("Date",            "visit_date",   "date",    description="Visit date (YYYY-MM-DD)",                   example="2024-01-15"),
        Col("Right Sph",       "right_sph",    "numeric", description="Right eye spherical power",                 example="-2.50"),
        Col("Right CYL",       "right_cyl",    "numeric", description="Right eye cylinder",                        example="-0.50"),
        Col("Right AXIS",      "right_axis",   "integer", description="Right eye axis (0–180°)",                   example="180"),
        Col("Right Add Power", "right_add",    "numeric", description="Right eye addition power",                  example="1.50"),
        Col("Left SPH",        "left_sph",     "numeric", description="Left eye spherical power",                  example="-2.00"),
        Col("Left CYL",        "left_cyl",     "numeric", description="Left eye cylinder",                         example="-0.25"),
        Col("Left AXIS",       "left_axis",    "integer", description="Left eye axis (0–180°)",                    example="170"),
        Col("Left Add Power",  "left_add",     "numeric", description="Left eye addition power",                   example="1.50"),
        # Additional patient fields
        Col("Barcode",         "barcode",       "text",    description="Patient card barcode",    example="PAT-001"),
        Col("Visit Name",      "visit_name",    "text",    description="Visit label/tag",         example="Annual Check"),
        Col("Is Deleted", "is_deleted", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Dob", "dob", "date", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Anniversary Date", "anniversary_date", "date", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Occupation", "occupation", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Merge Primary Id", "merge_primary_id", "uuid", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Diabetes", "diabetes", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Hypertension", "hypertension", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Thyroid", "thyroid", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Cardiac History", "cardiac_history", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Asthma", "asthma", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Drug Allergy", "drug_allergy", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Current Medication", "current_medication", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Surgery History", "surgery_history", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Family History", "family_history", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Systemic Notes", "systemic_notes", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Ref Mobile", "ref_mobile", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Relation", "relation", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        # System (download only, not writable)
        Col("", "id",              "uuid",      writable=False, download=False),
        Col("", "patient_id",      "uuid",      writable=False, download=False),
        Col("", "is_temporary",    "boolean",   writable=False, download=False),
        Col("", "created_at",      "timestamp", writable=False, download=False),
    ],

    # ── OPHTHALMIC LENS STOCK  (table: inventory_stock, stock_type='POWER') ──
    "OPHLENS": [
        Col("Brand",        "brand",         "text",    required=False, download=True,
            description="Brand name (from product master — read-only here)",
            example="Essilor",
            notes="Stored on products"),
        Col("Product",      "product_name",  "text",    required=True,  description="Product name — must exist in products table", example="Essilor Alize 1.5"),
        Col("Coating",     "coating",     "text",    description="Lens coating brand", example="Murk Vision"),
        Col("IndexValue",  "index_value", "numeric", description="Lens index (1.50/1.56/1.60/1.67/1.74)", example="1.56"),
        Col("SPH",          "sph",           "numeric", description="Spherical power (e.g. -4.00 to +6.00)",  example="-2.50"),
        Col("CYL",          "cyl",           "numeric", description="Cylinder power (leave blank for spherical)", example="-0.50"),
        Col("AXIS",         "axis",          "integer", description="Axis 1–180° (required when CYL is filled)", example="180"),
        Col("ADD",          "add_power",     "numeric", description="Add power (bifocal/progressive only)",    example="1.50"),
        Col("EyeSide",      "eye_side",      "text",    description="Which eye this stock is for — R=Right, L=Left, B=Both. NOT the same as IsEyeSpecific (product setting). EyeSide = per stock row. IsEyeSpecific = product master flag.",  example="B",
            allowed_values=["R", "L", "B"], default="B"),
        Col("ItemType",     "item_type",     "text",    description="STOCK=physical shelf stock, RX=made to order", example="STOCK",
            allowed_values=["STOCK", "RX"], default="STOCK"),
        Col("StockType",    "stock_type",    "text",    description="Always POWER for ophthalmic lenses",     example="POWER",
            allowed_values=["POWER"], default="POWER", download=True,
            notes="Set automatically by loader — do not change"),
        Col("Quantity",     "quantity",      "integer", required=False, description="Stock quantity — OR use qty_right/qty_left/qty_independent", example="24"),
        Col("R qty",        "qty_right",     "integer", required=False, download=False, description="Right eye quantity (upload only)", example="4"),
        Col("L qty",        "qty_left",      "integer", required=False, download=False, description="Left eye quantity (upload only)", example="4"),
        Col("Non R/L qty",  "qty_independent","integer",required=False, download=False, description="Non-eye-specific qty (upload only)", example="10"),
        Col("PurchaseRate", "purchase_rate", "numeric", description="Purchase/cost price",                    example="450.00"),
        Col("SellingPrice", "selling_price", "numeric", description="Selling price",                          example="750.00"),
        Col("MRP",          "mrp",           "numeric", description="Maximum retail price",                   example="950.00"),
        # GSTPercent comes from products table (JOIN p) — shown in download, NOT re-imported here
        # To change GST rate use the PRODUCT loader.
        Col("GSTPercent",   "gst_percent",   "numeric",  download=False, writable=False, description="GST % (from product master — read-only here; edit via Product loader)",
            example="12"),
        Col("lens_design",  "lens_design",   "text",    description="Lens design — auto-detected if blank",   example="SPHERICAL",
            allowed_values=["SPHERICAL", "TORIC", "MULTIFOCAL"]),
        Col("BatchNo",     "batch_no",      "text",    description="Batch number for this stock row", example="OPH-ESS-001",
            notes="Optional. If set, scanner in retail billing resolves directly to this power/product"),
        Col("Barcode","barcode","text",  description="Barcode on lens envelope — scan to auto-fill product+power at billing", example="4003994112305"),
        Col("ItemCode",     "item_code",     "text",  description="Item alias for Tally/reporting — same as Tally alias field", example="ESS-CRIZ-156-ARC"),
        Col("Location",     "location",      "text",    description="Storage location/rack",                  example="RACK-A1"),
        Col("IsActive",     "is_active",     "boolean", description="Active in system?",                      example="YES",
            allowed_values=["YES", "NO"], default=True),
        Col("Product Barcode", "product_barcode", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Purchase Price", "purchase_price", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Supplier Id", "supplier_id", "uuid", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Supplier Name", "supplier_name", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Effective From", "effective_from", "date", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Price Source", "price_source", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Is Price Current", "is_price_current", "boolean", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Lens Side", "lens_side", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Allocated Qty", "allocated_qty", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Reserved Qty", "reserved_qty", "numeric", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Batch Id", "batch_id", "uuid", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        Col("Bin No", "bin_no", "text", description="Auto-synced from DB", download=True),
        # ^ auto-added by schema_sync on startup
        # System
        Col("", "id",          "uuid",      writable=False, download=False),
        Col("", "product_id",  "uuid",      writable=False, download=False, notes="Resolved from product_name by loader"),
        Col("", "batch_no",    "text",      writable=False, download=False, notes="Not used for OPHLENS — power-tracked"),
        Col("", "expiry_date", "date",      writable=False, download=False, notes="Not applicable — ophthalmic lenses don't expire"),
        Col("", "created_at",  "timestamp", writable=False, download=False),
        Col("", "updated_at",  "timestamp", writable=False, download=False),
    ],

    # ── CONTACT LENS STOCK  (table: inventory_stock, stock_type='BATCH') ─────
    "CLENS": [
        Col("Brand",        "brand",         "text",    required=False, download=True,
            description="Brand name (from product master — read-only here)",
            example="Acuvue",
            notes="Stored on products"),
        Col("Product",      "product_name",  "text",    required=True,  description="Product name — must exist in products table", example="AirOptix Aqua SPH"),
        Col("Unit",         "unit",          "text",    required=False, download=True,
            description="PCS or PAIR — determines how qty_display is shown (from product master)",
            example="PCS",
            notes="Stored on products"),
        Col("BatchNo",      "batch_no",      "text",    required=True,  description="Batch/lot number. Combined with Product + SPH + CYL + AXIS + ADD + EyeSide as the dedup key — each unique power within a batch is a separate stock row.", example="AO2024A01"),
        Col("Barcode","barcode","text",   description="Barcode on individual box — unique per product+power — scan this to bill", example="08711600085956"),
        Col("ItemCode",     "item_code",     "text",   description="Item alias for Tally/reporting (e.g. AOAQ-M200) — same as Tally alias field", example="AOAQ-M200"),
        Col("ExpiryDate",   "expiry_date",   "date",    required=True,  description="Expiry date (YYYY-MM-DD)",  example="2026-06-30"),
        Col("SPH",          "sph",           "numeric", description="Spherical power",    example="-3.00"),
        Col("CYL",          "cyl",           "numeric", description="Cylinder power",     example="-0.75"),
        Col("AXIS",         "axis",          "integer", description="Axis (required if CYL filled)", example="180"),
        Col("ADD",          "add_power",     "numeric", description="Add power",          example=""),
        Col("EyeSide",      "eye_side",      "text",    description="Which eye this stock is for — R=Right, L=Left, B=Both. NOT the same as IsEyeSpecific (product setting). EyeSide = per stock row. IsEyeSpecific = product master flag.",  example="B",
            allowed_values=["R", "L", "B"], default="B"),
        Col("Quantity",     "quantity",      "integer", required=True,  description="Stock quantity (must be > 0)", example="12"),
        Col("PurchaseRate", "purchase_rate", "numeric", description="Purchase/cost price", example="180.00"),
        Col("SellingPrice", "selling_price", "numeric", description="Selling price",       example="280.00"),
        Col("MRP",          "mrp",           "numeric", description="MRP",                example="350.00"),
        # GSTPercent comes from products table (JOIN p) — shown in download, NOT re-imported here
        Col("GSTPercent",   "gst_percent",   "numeric", description="GST % (from product master — read-only here; edit via Product loader)",
            example="12",  writable=False, download=True),
        Col("ItemType",     "item_type",     "text",    description="Always STOCK for contact lenses", example="STOCK",
            allowed_values=["STOCK"], default="STOCK"),
        Col("lens_design",  "lens_design",   "text",    description="Lens design",        example="SPHERICAL",
            allowed_values=["SPHERICAL", "TORIC", "MULTIFOCAL"]),
        Col("Location",     "location",      "text",    description="Storage location",   example="FRIDGE-1"),
        Col("IsActive",     "is_active",     "boolean", description="Active?",            example="YES",
            allowed_values=["YES", "NO"], default=True),
                Col("AlconItemName", "alcon_item_name", "text", description="Alcon full item name for Tally sync", example="AIROPT ASTG HG 3P 870 145 -01.50 075 090", download=True),
        Col("MaterialCode",  "material_code",   "text", description="Alcon SAP material number for invoice matching", example="100154806", download=True),
        # System
        Col("", "id",         "uuid",      writable=False, download=False),
        Col("", "product_id", "uuid",      writable=False, download=False),
        Col("", "stock_type", "text",      writable=False, download=False, notes="Always 'BATCH' — set by loader"),
        Col("", "created_at", "timestamp", writable=False, download=False),
        Col("", "updated_at", "timestamp", writable=False, download=False),
    ],

    # ── SOLUTION BATCHES  (table: batches) ───────────────────────────────────
    # ── OPHTHALMIC SPEC PRICES  (table: ophthalmic_lens_specs) ────────────────
    "OPH_SPEC": [
        Col("Brand",        "brand",         "text",    required=True,  download=True,
            description="Essilor / Hoya / Shamir / Zeiss", example="Essilor"),
        Col("Product",      "product_name",  "text",    required=True,  download=True,
            description="Base product name (must exist in products table)",
            example="Varilux X Series"),
        Col("LensCategory", "lens_category", "text",    required=False, download=True,
            description="Progressive / SV RX / SV Stock / Bifocal", example="Progressive"),
        Col("Index",        "index_value",   "decimal", required=True,  download=True,
            description="Refractive index e.g. 1.50 / 1.60 / 1.67",
            example="1.60"),
        Col("Coating",      "coating",       "text",    required=True,  download=True,
            description="Coating name exactly as from brand price list",
            example="Crizal Prevencia"),
        Col("Treatment",    "treatment",     "text",    required=False, download=True,
            description="Clear / Photochromic / Tinted", example="Clear",
            allowed_values=["Clear","Photochromic","Tinted"]),
        Col("WLP_per_pair", "wlp_per_pair",  "decimal", required=True,  download=True,
            description="Wholesale / dealer price per pair (WLP/DP/WSP)",
            example="14500"),
        Col("SRP_per_pair", "srp_per_pair",  "decimal", required=False, download=True,
            description="SRP / MRP per pair", example="38000"),
        Col("PurchaseRate", "purchase_rate", "decimal", required=False, download=True,
            description="Our cost price (auto-calculated as WLP × discount if blank)",
            example="13050"),
    ],
    # ── OPHTHALMIC ADD-ONS  (table: ophthalmic_addons) ─────────────────────
    "OPH_ADDON": [
        Col("Brand",         "brand",          "text",    required=True,  download=True,
            description="Essilor / Hoya / Shamir / Zeiss", example="Essilor"),
        Col("Product",       "product_name",   "text",    required=False, download=True,
            description="Leave blank for brand/category level", example="Varilux X Series"),
        Col("AddonName",     "addon_name",     "text",    required=True,  download=True,
            description="Unique name per brand+scope", example="Blue UV Capture"),
        Col("AddonCategory", "addon_category", "text",    required=False, download=True,
            description="Protection / Photochromic / Coating / Tint / Personalisation",
            example="Protection",
            allowed_values=["Protection","Photochromic","Coating","Tint","Personalisation","General"]),
        Col("AppliesTo",     "applies_to",     "text",    required=False, download=True,
            description="ALL / Progressive / SV RX / SV Stock / Bifocal",
            example="ALL",
            allowed_values=["ALL","Progressive","SV RX","SV Stock","Bifocal","Reading"]),
        Col("WLP_Addon",     "wlp_addon",      "decimal", required=False, download=True,
            description="WLP add-on amount per pair (₹)", example="250"),
        Col("SRP_Addon",     "srp_addon",      "decimal", required=False, download=True,
            description="SRP/MRP add-on amount per pair (₹)", example="250"),
        Col("IsPercentage",  "is_percentage",  "text",    required=False, download=True,
            description="YES if values are % not fixed ₹", example="NO",
            allowed_values=["YES","NO"]),
        Col("SortOrder",     "sort_order",     "integer", required=False, download=True,
            description="Display order in billing (1=top)", example="1"),
        Col("Notes",         "notes",          "text",    required=False, download=True,
            description="Tooltip shown in billing screen",
            example="+₹250/pair over base WLP"),
    ],

    # ── PRICE MASTER  (Contact Lenses + Solutions/Cleaners price update) ───────
    # Scope  : Contact Lenses (inventory_stock stock_type=BATCH) +
    #          Solutions / Cleaners (batches table).
    # NOT for: Ophthalmic lenses (→ OPH_SPEC) | Frames (→ FRAME loader).
    # Upload : updates mrp + selling_price + purchase_rate per product.
    # Download: one row per product showing latest batch price.
    "PRICE": [
        Col("Product",       "product_name",  "text",    required=True,  download=True,
            description="Product name — Contact Lens or Solution/Cleaner (must exist in products table)",
            example="Acuvue Oasys 1-Day"),
        Col("Category",      "category",      "text",    required=False, download=True,
            description="Product category from product master (read-only reference)",
            example="Daily Contact Lens"),
        Col("MRP",           "mrp",           "decimal", required=True,  download=True,
            description="Maximum Retail Price / SRP per box/unit",
            example="1200"),
        Col("SellingPrice",  "selling_price", "decimal", required=False, download=True,
            description="Our selling price per box/unit (defaults to MRP if blank)",
            example="1100"),
        Col("PurchaseRate",  "purchase_rate", "decimal", required=False, download=True,
            description="Our purchase / cost price per box/unit",
            example="850"),
        Col("Notes",         "notes",         "text",    required=False, download=True,
            description="Optional notes for this price update",
            example="J&J 2026-27 rate card"),
        Col("CompanyProductName", "company_product_name", "text",
            required=False, download=True,
            description="Supplier/distributor invoice name. "
                        "Alias lookup: inventory_stock first, then price list for bill matching.",
            example="AIR OPTIX HG 8.6/-1.50"),
    ],
    "SOL": [
        Col("Brand",        "brand",         "text",    required=False, download=True,
            description="Brand name (from product master — read-only here)",
            example="Opti-Free",
            notes="Stored on products"),
        Col("Product",      "product_name",  "text",    required=True,  description="Product name — must exist in products table", example="Opti-Free Pure Moist"),
        Col("BatchNo",      "batch_no",      "text",    description="Batch number",      example="SOL2024A"),
        Col("ExpiryDate",   "expiry_date",   "date",    description="Expiry date (YYYY-MM-DD)", example="2026-12-31"),
        Col("Qty",          "qty_available", "numeric", description="Quantity available", example="50"),
        Col("CostPrice",    "cost_price",    "numeric", description="Cost/purchase price", example="120.00"),
        Col("SellingPrice", "selling_price", "numeric", description="Selling price",       example="185.00"),
        Col("MRP",          "mrp",           "numeric", description="Maximum retail price", example="220.00"),
        # GSTPercent comes from products table (JOIN p) — shown in download, NOT re-imported here
        Col("GSTPercent",   "gst_percent",   "numeric", description="GST % (from product master — read-only here; edit via Product loader)",
            example="12",  writable=False, download=True),
        Col("IsActive",     "is_active",     "boolean", description="Active?",             example="YES",
            allowed_values=["YES", "NO"], default=True),
        # System
        Col("", "id",         "uuid",      writable=False, download=False),
        Col("CompanyProductName", "company_product_name", "text",
            description="Supplier product name on invoice. Used for OCR bill auto-matching.",
            example="RENU MULTI-PURPOSE 360ML"),
        Col("", "product_id", "uuid",      writable=False, download=False),
        Col("", "created_at", "timestamp", writable=False, download=False),
    ],

    # ── BLANK INVENTORY  (table: blank_inventory) ─────────────────────────────
    "BLANK": [
        Col("brand",            "brand",            "text",    required=True,  description="Lens blank brand",         example="Essilor"),
        Col("Category",         "category",         "text",    required=True,  description="Blank category",           example="Single Vision",
            allowed_values=["Single Vision", "Progressive", "Bifocal", "Toric", "Reading"]),
        Col("Material",         "material",         "text",    required=True,  description="Lens material",            example="CR39",
            allowed_values=["CR39", "Polycarbonate", "Trivex", "1.56", "1.60", "1.67", "1.74"]),
        Col("Add",              "add_power",        "numeric", required=True,  description="Add power (0 for SV, 1.00–3.50 for PAP)", example="0"),
        Col("COLOUR",           "colour",           "text",    description="Lens colour / tint",    example="Clear"),
        Col("qty_Right",        "qty_right",        "integer", description="Qty for Right eye",     example="50"),
        Col("qty_left",         "qty_left",         "integer", description="Qty for Left eye",      example="50"),
        Col("qty_independent",  "qty_independent",  "integer", description="Qty not eye-specific",  example="0"),
        Col("min_stock",        "min_stock",        "integer", description="Min stock alert level", example="10"),
        Col("cost_price",       "cost_price",       "numeric", description="Cost per piece",        example="45.00"),
        Col("batch_no",         "batch_no",         "text",    description="Batch/lot number",      example="LOT2024A"),
        Col("location",         "location",         "text",    description="Storage location",      example="RACK-B2"),
        Col("Barcode",          "barcode",          "text",    description="Scanner barcode — scan laminated card to auto-select this blank in job card", example="BLK-ESS-CR-SV"),
        Col("ItemCode",         "item_code",        "text",    description="Item alias for Tally/reporting", example="ESS-CR39-SV"),
        Col("company_billing_name", "company_billing_name", "text",
            description="Supplier's own name for this product on their invoice (used for OCR bill matching). "
                        "E.g. 'CR KT WT 1.50' or 'V2 PG GREY 2.00'. Stored as alias for auto-purchase matching.",
            example="CR KT WT ADD 2.00"),
        Col("Recomended Base",  "base_recommended", "numeric", required=True, description="Recommended base curve; part of blank inventory identity", example="6.0"),
        Col("Base 1 P",         "base_1",           "numeric", description="Base curve option 1",   example="4.0"),
        Col("Base 2 P",         "base_2",           "numeric", description="Base curve option 2",   example="6.0"),
        Col("Base 3P",          "base_3",           "numeric", description="Base curve option 3",   example="8.0"),
        Col("IsActive",         "is_active",        "boolean", description="Active?",               example="YES",
            allowed_values=["YES", "NO"], default=True),
        # System
        Col("", "id",         "uuid",      writable=False, download=False),
        Col("", "created_at", "timestamp", writable=False, download=False),
        Col("", "updated_at", "timestamp", writable=False, download=False),
        Col("", "created_by", "text",      writable=False, download=False, notes="Auto-set to LOADER by system"),
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
# DERIVED HELPERS  — all other modules call these, never access DB_SCHEMA directly
# ═══════════════════════════════════════════════════════════════════════════════

def get_writable_cols(file_type: str) -> List[Col]:
    """All columns that can be written via import (excludes system cols)."""
    return [c for c in DB_SCHEMA.get(file_type, []) if c.writable]


def get_required_cols(file_type: str) -> List[str]:
    """DB column names that are required for import."""
    return [c.db_column for c in get_writable_cols(file_type) if c.required]


def get_download_cols(file_type: str) -> List[Col]:
    """Columns included in downloader SELECT query."""
    return [c for c in get_writable_cols(file_type) if c.download]


def get_excel_headers(file_type: str) -> List[str]:
    """Ordered list of Excel column headers for the import template."""
    return [c.excel_header for c in get_writable_cols(file_type)]


def get_column_map(file_type: str) -> Dict[str, str]:
    """
    Build normalized excel_header → db_column mapping dict.
    Normalisation: strip, lower, remove spaces and underscores.
    Used by universal_loader_core.apply_column_map().
    """
    def _norm(s: str) -> str:
        return s.strip().lower().replace(" ", "").replace("_", "")

    result = {}
    for col in get_writable_cols(file_type):
        if col.excel_header:
            result[_norm(col.excel_header)] = col.db_column
    return result


def get_db_to_excel_map(file_type: str) -> Dict[str, str]:
    """db_column → excel_header mapping (for downloader SELECT aliases)."""
    return {c.db_column: c.excel_header
            for c in get_download_cols(file_type)
            if c.excel_header}


def get_allowed_values(file_type: str) -> Dict[str, List[str]]:
    """excel_header → allowed_values for all columns that have restrictions."""
    return {c.excel_header: c.allowed_values
            for c in get_writable_cols(file_type)
            if c.allowed_values}


def get_defaults(file_type: str) -> Dict[str, Any]:
    """Normalized column name → default value for auto-fix injection."""
    def _norm(s): return s.strip().lower().replace(" ", "").replace("_", "")
    return {_norm(c.excel_header): c.default
            for c in get_writable_cols(file_type)
            if c.default is not None and c.excel_header}


def get_schema_for_ui(file_type: str) -> List[Dict]:
    """
    Serialise schema for the column mapping panel in loader_ui.py.
    Returns list of dicts with keys: excel_header, db_column, db_type,
    required, description, example, allowed_values.
    """
    return [
        {
            "excel_header":   c.excel_header,
            "db_column":      c.db_column,
            "db_type":        c.db_type,
            "required":       c.required,
            "description":    c.description,
            "example":        c.example,
            "allowed_values": c.allowed_values,
            "notes":          c.notes,
        }
        for c in get_writable_cols(file_type)
    ]


def build_select_fragments(file_type: str, table_alias: str = "") -> List[str]:
    """
    Build SQL SELECT column fragments from the registry.
    Used by data_downloader to auto-generate SELECT queries.
    e.g. ['p.product_name AS "Product"', 's.sph AS "SPH"', ...]
    """
    prefix = f"{table_alias}." if table_alias else ""
    frags = []
    for col in get_download_cols(file_type):
        if not col.excel_header:
            continue
        db = col.db_column
        hdr = col.excel_header

        # Special handling for boolean → YES/NO
        if col.db_type == "boolean":
            frags.append(
                f'CASE WHEN {prefix}{db} THEN \'YES\' ELSE \'NO\' END AS "{hdr}"'
            )
        # Special handling for date → formatted string
        elif col.db_type == "date" and not col.required:
            frags.append(
                f'TO_CHAR({prefix}{db}, \'YYYY-MM-DD\') AS "{hdr}"'
            )
        else:
            frags.append(f'{prefix}{db} AS "{hdr}"')
    return frags


# ── Quick lookup for UI panel ─────────────────────────────────────────────────
ALL_FILE_TYPES = list(DB_SCHEMA.keys())

# System columns excluded from every import
SYSTEM_COLUMNS = {"id", "created_at", "updated_at", "product_id", "patient_id",
                  "created_by", "product_code", "status"}
