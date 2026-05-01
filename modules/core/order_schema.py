"""
modules/core/order_schema.py

Order Schema Contract Layer
===========================
Single source of truth for order line field names and types.

WHY THIS EXISTS
--------------
Punching plugins (retail, wholesale, lab) evolve independently.
Backoffice modules (helpers, logic, UI) expect a stable field contract.
Without this layer, any field rename in punching crashes the backoffice.

HOW IT WORKS
------------
1. Punching plugins produce lines in whatever shape they need.
2. finalize_engine calls normalize_cart() before any validator or save.
3. normalize_order_line() applies aliases, fills defaults, enforces types.
4. Backoffice always receives a schema-v2 compliant line.

ADDING NEW FIELDS
-----------------
- Add to DEFAULT_LINE with a safe default.
- Add to ALIASES if you're renaming an old field.
- Bump ORDER_SCHEMA_VERSION.
- No other file needs to change.
"""

from copy import deepcopy
import datetime

# ============================================================================
# SCHEMA VERSIONING
# ============================================================================

ORDER_SCHEMA_VERSION = 2
# ============================================================================
# SCHEMA MIGRATIONS
# ============================================================================

def _migrate_v1_to_v2(line: dict) -> dict:
    """
    v1 → v2:
    - add → add_power
    - gst → gst_percent
    - batches → batch_allocation
    - admin_note, pricing_source, pricing_applied_at fields added
    Redundant with ALIASES but keeps migration path explicit and auditable.
    """
    import copy
    out = copy.deepcopy(line)
    for old, new in [("add", "add_power"),("gst", "gst_percent"),("batches", "batch_allocation")]:
        if old in out and new not in out:
            out[new] = out.pop(old)
    out.setdefault("admin_note", None)
    out.setdefault("pricing_source", None)
    out.setdefault("pricing_applied_at", None)
    return out


SCHEMA_MIGRATIONS: dict = {
    1: _migrate_v1_to_v2,
    # Future: 2: _migrate_v2_to_v3,
}


def migrate_line(line: dict, from_version: int = None) -> dict:
    """
    Migrate a line from its detected schema version up to ORDER_SCHEMA_VERSION.
    Version detection: from_version arg → line["_schema_version"] → default 1.
    Migrations applied sequentially so v1→v3 runs v1→v2 then v2→v3.
    normalize_order_line() calls this automatically.
    """
    detected = from_version or line.get("_schema_version") or 1
    current  = detected
    import copy
    out = copy.deepcopy(line)
    while current < ORDER_SCHEMA_VERSION:
        fn = SCHEMA_MIGRATIONS.get(current)
        if fn:
            out = fn(out)
        current += 1
    return out



# ============================================================================
# STABLE FIELD CONTRACT
# ============================================================================

DEFAULT_LINE: dict = {

    # ── Identity ──────────────────────────────────────────────────────────────
    "line_id":          None,           # UUID string, assigned if missing

    # ── Product ───────────────────────────────────────────────────────────────
    "product_id":       "",
    "product_name":     "",
    "brand":            "",
    "main_group":       "",

    # ── Optical / Rx (None = non-optical product) ─────────────────────────────
    "eye_side":         "OTHER",        # "R" | "L" | "B" | "OTHER"
    "sph":              None,
    "cyl":              None,
    "axis":             None,
    "add_power":        None,

    # ── Quantities ────────────────────────────────────────────────────────────
    "requested_qty":    0,              # what user asked for
    "billing_qty":      0,              # what will be invoiced (allocated)
    "order_qty":        0,              # shortfall going to PO
    "display_qty":      "",             # human-readable (e.g. "2 Box / 12 Pcs")

    # ── Pricing (set by pricing engine, never by plugins) ─────────────────────
    "unit_price":       0.0,            # per-PCS, Decimal-safe
    "total_price":      0.0,            # unit_price × billing_qty
    "gst_percent":      0.0,            # ✅ NEVER default to a slab — 0 means "not set yet"
    "gst_amount":       0.0,            # GST extracted/added (set by tax_engine)
    "discount_percent": 0.0,            # discount % applied
    "discount_amount":  0.0,            # discount value applied

    # ── Pricing audit (set by pricing_engine.py) ──────────────────────────────
    "pricing_source":       None,       # "batch_weighted" | "manual" | None
    "pricing_applied_at":   None,       # ISO timestamp

    # ── Batch allocation ──────────────────────────────────────────────────────
    "batch_allocation":     [],         # list of {batch_no, allocated_qty, ...}
    "suggested_allocation": [],         # pre-allocation before user confirmation

    # ── Job-card params (lens-specific) ───────────────────────────────────────
    "lens_params":      {},
    "boxing_params":    {},

    # ── Status & metadata ─────────────────────────────────────────────────────
    "status":           "Draft",
    "created_at":       None,

    # ── Admin annotations (set by FinancialValidator) ─────────────────────────
    "admin_note":       None,
}


# ============================================================================
# FIELD ALIASES  (old_name → new_name)
# ============================================================================

ALIASES: dict = {
    # Historical renames in punching code
    "add":              "add_power",
    "power_add":        "add_power",
    "qty":              "billing_qty",
    "final_qty":        "billing_qty",
    "gst":              "gst_percent",
    "batches":          "batch_allocation",
    "allocation":       "batch_allocation",
    "product_row":      None,           # None = strip from output (computed UI field)
}


# ============================================================================
# NORMALIZER — single line
# ============================================================================

def normalize_order_line(line: dict) -> dict:
    """
    Produce a schema-v2-compliant order line from any punching output.

    Operations (in order):
        1. Deep-copy — never mutate caller's dict.
        2. Apply field aliases (rename legacy keys).
        3. Strip keys mapped to None in ALIASES (UI-only computed fields).
        4. Fill missing fields from DEFAULT_LINE.
        5. Apply derived safety rules (qty fallbacks, price recalc).

    Returns a new dict safe for validators, pricing engine, and backoffice.
    """
    if not isinstance(line, dict):
        return deepcopy(DEFAULT_LINE)

    # ── Step 1: Schema migration (version-aware) ──────────────────────────────
    # Detect line's schema version and migrate up to ORDER_SCHEMA_VERSION.
    # This runs BEFORE alias resolution so migrations can rename fields freely.
    line = migrate_line(line)

    src  = deepcopy(line)
    safe = deepcopy(DEFAULT_LINE)

    # ── Step 2+3: Aliases ────────────────────────────────────────────────────
    for old, new in ALIASES.items():
        if old in src:
            val = src.pop(old)
            if new is not None and new not in src:
                src[new] = val
            # if new is None → field is stripped (don't re-add)

    # ── Step 4: Fill known fields ─────────────────────────────────────────────
    for key in safe:
        if key in src:
            safe[key] = src[key]

    # ── Step 5: Derived safety rules ──────────────────────────────────────────

    # Line ID — assign if missing
    if not safe["line_id"]:
        import uuid
        safe["line_id"] = str(uuid.uuid4())

    # Qty fallback: if billing_qty absent, use requested_qty
    if safe["billing_qty"] <= 0 and safe["requested_qty"] > 0:
        safe["billing_qty"] = safe["requested_qty"]

    # Price fallback: recalculate total if total_price is zero but unit_price set
    if safe["total_price"] == 0.0 and safe["unit_price"] > 0 and safe["billing_qty"] > 0:
        safe["total_price"] = round(safe["unit_price"] * safe["billing_qty"], 2)

    # Batch allocation: ensure list, never None
    if safe["batch_allocation"] is None:
        safe["batch_allocation"] = []

    # Created timestamp: stamp now if missing
    if not safe["created_at"]:
        safe["created_at"] = datetime.datetime.now().isoformat()

    return safe


# ============================================================================
# BULK NORMALIZER — full cart
# ============================================================================

def normalize_cart(cart_lines: list) -> list:
    """
    Normalize every line in a cart. Safe to call on empty or None carts.
    This is the single call finalize_engine makes before any validator runs.
    """
    if not cart_lines:
        return []
    return [normalize_order_line(line) for line in cart_lines]


# ============================================================================
# HEADER UTILITIES
# ============================================================================

def attach_schema_version(order_info: dict) -> dict:
    """
    Stamp the schema version onto the order header.
    Backoffice can use this for multi-version compatibility later.
    """
    order_info = order_info or {}
    order_info["schema_version"] = ORDER_SCHEMA_VERSION
    return order_info


# ============================================================================
# BACKOFFICE SAFETY ADAPTER
# ============================================================================

def safe_load_lines(raw_lines: list) -> list:
    """
    Entry point for backoffice to normalize lines from the database.

    Use in backoffice_management._lazy_import() or any loader that
    reads order lines from DB. Heals old schema rows transparently.

    Usage:
        lines = safe_load_lines(db_order["lines"])
    """
    return normalize_cart(raw_lines)
