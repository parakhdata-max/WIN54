"""
modules/core/business_rules.py
===============================
SINGLE SOURCE OF TRUTH for all business rules in WIN41.

HOW IT WORKS
------------
Rules are declared here as plain Python constants or small functions.
The pipeline, UI layer, and validators all IMPORT from here — they never
hard-code a rule themselves.

If you want to change a rule → change it here ONLY.
The rest of the system picks it up automatically.

SECTIONS
--------
1.  ORDER STATUS FLOW          — allowed transitions, display labels
2.  ORDER RULES                — zero-line block, edit restrictions
3.  PRICING RULES              — price normalization, GST slabs
4.  PAYMENT RULES              — advance/balance, invoice gate
5.  BILLING RULES              — challan/invoice flow, retail gate
6.  CONSULTATION RULES         — fee, service line, conversion
7.  QUANTITY RULES             — box/loose, pair, finalization guard
8.  EYE SIDE RULES             — valid sides, SERVICE skip list
9.  ALLOCATION RULES           — what gets auto-allocated
10. VALIDATION HOOKS           — registered validators (pipeline enforced)
"""

from __future__ import annotations
from typing import Dict, List, Optional, Set


# ============================================================================
# 1. ORDER STATUS FLOW
# ============================================================================

# Statuses the system knows about — in lifecycle order
ORDER_STATUSES: List[str] = [
    "PENDING",
    "UNDER_REVIEW",
    "CONFIRMED",
    "IN_PRODUCTION",
    "READY",
    "BILLED",
    "DISPATCHED",
    "DELIVERED",
    "CLOSED",
    "CANCELLED",
    "RETURN_REQUESTED",
    "RETURN_APPROVED",
    "RETURN_IN_TRANSIT",
    "RETURNED",
    "REFUND_PENDING",
    "REFUND_PROCESSED",
]

# Allowed forward transitions per status
# RULE: An order can only move to statuses listed here
STATUS_TRANSITIONS: Dict[str, List[str]] = {
    "PENDING":            ["UNDER_REVIEW", "CONFIRMED", "CANCELLED"],
    "PROVISIONAL":        ["UNDER_REVIEW", "CONFIRMED", "CANCELLED"],
    "UNDER_REVIEW":       ["CONFIRMED",    "CANCELLED"],
    "CONFIRMED":          ["IN_PRODUCTION","READY",     "CANCELLED"],
    "IN_PRODUCTION":      ["CONFIRMED",    "READY",     "CANCELLED"],   # CONFIRMED = stage release
    "READY":              ["CONFIRMED",    "BILLED",    "DISPATCHED", "CANCELLED"],
    "BILLED":             ["DISPATCHED",   "CANCELLED"],
    "DISPATCHED":         ["DELIVERED",    "RETURN_REQUESTED"],
    "DELIVERED":          ["CLOSED",       "RETURN_REQUESTED"],
    "CLOSED":             ["RETURN_REQUESTED"],   # late return window
    "CANCELLED":          [],
    "RETURN_REQUESTED":   ["RETURN_APPROVED",    "CANCELLED"],
    "RETURN_APPROVED":    ["RETURN_IN_TRANSIT",  "RETURNED"],
    "RETURN_IN_TRANSIT":  ["RETURNED"],
    "RETURNED":           ["REFUND_PENDING"],
    "REFUND_PENDING":     ["REFUND_PROCESSED"],
    "REFUND_PROCESSED":   [],
}

# Terminal statuses — no further movement allowed
TERMINAL_STATUSES: Set[str] = {
    "DELIVERED", "CLOSED", "CANCELLED",
    "RETURNED", "REFUND_PROCESSED",
}

# RULE: New orders punched from retail/wholesale desk start as UNDER_REVIEW.
#        Backoffice must explicitly confirm before production starts.
INITIAL_ORDER_STATUS = "UNDER_REVIEW"

# RULE: The pipeline returns "CONFIRMED" as the caller success signal
#        (retail_punching and wholesale_punching check result["status"] == "CONFIRMED")
PIPELINE_SUCCESS_STATUS = "CONFIRMED"

# Display labels and colours per status
STATUS_DISPLAY: Dict[str, Dict] = {
    "PENDING":       {"icon": "⏳", "color": "#64748b", "label": "Pending"},
    "PROVISIONAL":   {"icon": "📝", "color": "#64748b", "label": "Provisional"},
    "UNDER_REVIEW":  {"icon": "🔍", "color": "#f59e0b", "label": "Under Review"},
    "CONFIRMED":     {"icon": "✅", "color": "#3b82f6", "label": "Confirmed"},
    "IN_PRODUCTION": {"icon": "⚙️", "color": "#8b5cf6", "label": "In Production"},
    "READY":         {"icon": "📦", "color": "#10b981", "label": "Ready"},
    "BILLED":        {"icon": "🧾", "color": "#059669", "label": "Billed"},
    "DISPATCHED":    {"icon": "🚚", "color": "#0891b2", "label": "Dispatched"},
    "DELIVERED":     {"icon": "✅", "color": "#166534", "label": "Delivered"},
    "CLOSED":        {"icon": "🔒", "color": "#475569", "label": "Closed"},
    "CANCELLED":        {"icon": "❌", "color": "#ef4444", "label": "Cancelled"},
    "RETURN_REQUESTED": {"icon": "↩️", "color": "#f97316", "label": "Return Requested"},
    "RETURN_APPROVED":  {"icon": "✅", "color": "#f97316", "label": "Return Approved"},
    "RETURN_IN_TRANSIT":{"icon": "🚚", "color": "#f97316", "label": "Return In Transit"},
    "RETURNED":         {"icon": "📦", "color": "#94a3b8", "label": "Returned"},
    "REFUND_PENDING":   {"icon": "💰", "color": "#eab308", "label": "Refund Pending"},
    "REFUND_PROCESSED": {"icon": "✅", "color": "#10b981", "label": "Refund Processed"},
}


# ============================================================================
# 2. ORDER RULES
# ============================================================================

# RULE: An order must have at least 1 active line before saving
MIN_ORDER_LINES = 1

# RULE: A consultation order that has been converted to a retail order
#        must not be re-opened or edited — button is dead
CONVERTED_CONSULTATION_EDITABLE = False

# RULE: Converted consultation status after conversion
CONSULTATION_POST_CONVERSION_STATUS = "CLOSED"

# RULE: Orders in these statuses can still be edited from backoffice
EDITABLE_STATUSES: Set[str] = {"PENDING", "UNDER_REVIEW", "CONFIRMED"}

# RULE: Statuses where challan creation is allowed
CHALLAN_ALLOWED_STATUSES: Set[str] = {"CONFIRMED", "READY", "IN_PRODUCTION", "BILLED"}


# ============================================================================
# 3. PRICING RULES
# ============================================================================

# RULE: Retail orders use MRP as the price source
# RULE: Wholesale orders use selling_price as the price source
PRICE_FIELD_BY_ORDER_TYPE: Dict[str, str] = {
    "RETAIL":    "mrp",
    "WHOLESALE": "selling_price",
    "ONLINE":    "online_price",
    "LAB":       "selling_price",
}

# Fallback chain if primary price field is zero
PRICE_FALLBACK_CHAIN: Dict[str, List[str]] = {
    "RETAIL":    ["mrp", "selling_price", "unit_price", "price"],
    "WHOLESALE": ["selling_price", "mrp", "unit_price", "price"],
    "ONLINE":    ["online_price", "mrp", "selling_price", "unit_price"],
}

# RULE: GST must be in valid Indian slab
VALID_GST_SLABS: List[float] = [0.0, 5.0, 12.0, 18.0, 28.0]

# RULE: GST calculation method per order type
#   RETAIL    → GST is INCLUSIVE in MRP (back-calculate from total)
#   WHOLESALE → GST is EXCLUSIVE (add on top of base price)
GST_INCLUSIVE_ORDER_TYPES: Set[str] = {"RETAIL"}
GST_EXCLUSIVE_ORDER_TYPES: Set[str] = {"WHOLESALE", "LAB", "ONLINE"}

# RULE: price_mismatch threshold — difference > this % = flag as wrong price
PRICE_MISMATCH_THRESHOLD_PCT = 15.0   # 15% — avoids false positives on rounding

# RULE: unit_price in order_lines is ALWAYS stored as per-PCS price
#        normalize_to_pcs_price() must NOT be called on already-stored prices
UNIT_PRICE_STORED_AS_PCS = True


# ============================================================================
# 4. PAYMENT RULES
# ============================================================================

# RULE: Advance cannot exceed order total
ADVANCE_CANNOT_EXCEED_TOTAL = True

# RULE: Minimum advance amount (0 = no minimum enforced)
MIN_ADVANCE_AMOUNT = 0.0

# RULE: Payment modes allowed
PAYMENT_MODES: List[str] = ["CASH", "UPI", "NEFT", "CARD", "CHEQUE"]

# RULE: Default payment mode for retail orders
DEFAULT_PAYMENT_MODE_RETAIL = "ADVANCE_BALANCE"

# RULE: Default payment mode for wholesale orders
DEFAULT_PAYMENT_MODE_WHOLESALE = "ON_COMPLETION"


# ============================================================================
# BILLING CATEGORIES — party-level billing policy
# ============================================================================
# Set on parties.billing_category column.
# Drives: order placement gate, challan gate, invoice gate, payment requirement.
#
# ─────────────────────────────────────────────────────────────────────────────
# FULL_ADVANCE
#   Who:    High-risk new customers, blacklisted parties, one-time cash buyers
#   Rule:   Order can be punched and edited freely in backoffice.
#           CONFIRM is blocked — order stays at PENDING_PAYMENT status
#           until full payment is received.
#           Once paid, operator confirms → billing pipeline opens normally.
#   Gate:   CONFIRM blocked (status → PENDING_PAYMENT) if balance_due > 0
#           Order can still be punched, viewed, edited before payment.
#   Price:  MRP (GST inclusive)
#
# ADVANCE_BALANCE (default for RETAIL)
#   Who:    Regular retail patients, walk-in customers
#   Rule:   Advance at order time is optional, but invoice cannot be
#           raised until full payment is received.
#           Typical flow: collect advance → order → deliver → collect balance → invoice
#   Gate:   INVOICE blocked if paid < grand_total
#   Price:  MRP (GST inclusive)
#
# PRE_PAYMENT
#   Who:    Cash-only wholesale customers, small dealers without credit
#   Rule:   Full payment required before invoice is generated.
#           Order and challan can proceed without payment.
#   Gate:   INVOICE blocked if paid < grand_total
#   Price:  Selling price (GST exclusive) or as negotiated
#
# ON_COMPLETION (default for WHOLESALE)
#   Who:    Standard wholesale dealers, distributors
#   Rule:   Standard credit billing. Challan on dispatch, invoice raised
#           immediately. Payment expected within credit_days.
#   Gate:   No payment gate — invoice allowed on credit
#   Credit: credit_limit and credit_days apply
#   Price:  Selling price (GST exclusive)
#
# ON_ACCOUNT
#   Who:    Large dealers, chain stores, periodic settlement parties
#   Rule:   Invoice raised immediately. Party maintains a running ledger.
#           Payment collected periodically (weekly/monthly statement).
#           No per-invoice payment tracking — only ledger balance matters.
#   Gate:   No payment gate — invoice allowed immediately
#   Gate:   BLOCKED only if party has exceeded credit_limit
#   Credit: credit_limit is the total outstanding cap
#   Price:  Negotiated / contract rate
# ─────────────────────────────────────────────────────────────────────────────

BILLING_CATEGORIES: Dict[str, Dict] = {
    "FULL_ADVANCE": {
        "label":              "💵 Full Advance",
        "description":        "Full payment required before order is CONFIRMED",
        "color":              "#ef4444",
        "order_gate":         False,   # Order can be punched freely
        "confirm_gate":       True,    # CONFIRM blocked until fully paid → PENDING_PAYMENT
        "challan_gate":       False,   # Challan proceeds normally after confirm
        "invoice_gate":       False,   # Invoice proceeds normally after confirm
        "requires_full_pay_before_order":   False,
        "requires_full_pay_before_confirm": True,
        "requires_full_pay_before_invoice": False,
        "credit_allowed":     False,
        "gst_inclusive":      True,    # MRP pricing
        "default_for":        [],
    },
    "ADVANCE_BALANCE": {
        "label":              "🛍️ Advance + Balance",
        "description":        "Advance at booking, balance before invoice",
        "color":              "#8b5cf6",
        "order_gate":         False,   # Order proceeds without payment
        "challan_gate":       False,
        "invoice_gate":       True,    # Invoice blocked until fully paid
        "requires_full_pay_before_order":   False,
        "requires_full_pay_before_invoice": True,
        "credit_allowed":     False,
        "gst_inclusive":      True,    # MRP pricing
        "default_for":        ["RETAIL"],
    },
    "PRE_PAYMENT": {
        "label":              "💳 Pre-Payment",
        "description":        "Full payment before invoice (cash wholesale)",
        "color":              "#f59e0b",
        "order_gate":         False,
        "challan_gate":       False,
        "invoice_gate":       True,    # Invoice blocked until fully paid
        "requires_full_pay_before_order":   False,
        "requires_full_pay_before_invoice": True,
        "credit_allowed":     False,
        "gst_inclusive":      False,   # Selling price ex-GST
        "default_for":        [],
    },
    "ON_COMPLETION": {
        "label":              "📦 On Completion",
        "description":        "Invoice on dispatch, pay within credit days",
        "color":              "#10b981",
        "order_gate":         False,
        "challan_gate":       False,
        "invoice_gate":       False,   # Invoice allowed on credit
        "requires_full_pay_before_order":   False,
        "requires_full_pay_before_invoice": False,
        "credit_allowed":     True,
        "gst_inclusive":      False,   # Selling price ex-GST
        "default_for":        ["WHOLESALE"],
    },
    "ON_ACCOUNT": {
        "label":              "📒 On Account",
        "description":        "Running ledger, periodic settlement",
        "color":              "#3b82f6",
        "order_gate":         False,
        "challan_gate":       False,
        "invoice_gate":       False,   # Invoice allowed immediately
        "requires_full_pay_before_order":   False,
        "requires_full_pay_before_invoice": False,
        "credit_allowed":     True,    # Blocked only if credit_limit exceeded
        "credit_limit_gate":  True,    # Block if outstanding > credit_limit
        "gst_inclusive":      False,
        "default_for":        [],
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Helper functions — import these in bulk_order, billing, validators
# ─────────────────────────────────────────────────────────────────────────────

def get_billing_category(category: str) -> Dict:
    """Return billing category rules dict. Falls back to ON_COMPLETION if unknown."""
    return BILLING_CATEGORIES.get(
        str(category or "ON_COMPLETION").upper(),
        BILLING_CATEGORIES["ON_COMPLETION"]
    )


def billing_blocks_order(billing_category: str, paid: float, total: float) -> bool:
    """
    Returns True if order PLACEMENT should be blocked.
    NOTE: FULL_ADVANCE no longer blocks order placement — it blocks CONFIRM.
    This function is kept for backwards compatibility but returns False for all categories.
    The CONFIRM gate is handled by order_status_live.check_confirm_gate().
    """
    return False   # Order placement always allowed — confirm gate handles FULL_ADVANCE


def billing_blocks_invoice(billing_category: str, paid: float, total: float,
                            outstanding: float = 0, credit_limit: float = 0) -> tuple:
    """
    Returns (blocked: bool, reason: str).
    Checks invoice gate and credit limit for ON_ACCOUNT.
    """
    bc = get_billing_category(billing_category)
    if bc.get("requires_full_pay_before_invoice") and paid < total - 0.01:
        balance = total - paid
        return True, f"Payment incomplete — ₹{balance:,.2f} remaining"
    if bc.get("credit_limit_gate") and credit_limit > 0:
        if outstanding + total > credit_limit:
            return True, (f"Credit limit exceeded — outstanding ₹{outstanding:,.2f} + "
                         f"this order ₹{total:,.2f} > limit ₹{credit_limit:,.2f}")
    return False, ""


def get_billing_category_label(category: str) -> str:
    """Return display label for a billing category."""
    bc = get_billing_category(category)
    return bc.get("label", category)


# Backwards compatibility — existing code uses these
RETAIL_INVOICE_REQUIRES_FULL_PAYMENT = True
WHOLESALE_INVOICE_REQUIRES_FULL_PAYMENT = False


# ============================================================================
# 5. BILLING RULES
# ============================================================================

# RULE: Challans cannot be hard-deleted — only soft-deleted (is_deleted=TRUE)
CHALLAN_HARD_DELETE_ALLOWED = False
CHALLAN_DELETE_MESSAGE = (
    "Challans cannot be deleted. "
    "Issue a Credit Note against the invoice instead."
)

# RULE: Invoices cannot be hard-deleted
INVOICE_HARD_DELETE_ALLOWED = False
INVOICE_DELETE_MESSAGE = (
    "Invoices cannot be deleted. "
    "Use Credit & Debit Notes module instead."
)

# RULE: All challans in one invoice must belong to the same party
INVOICE_SINGLE_PARTY_ONLY = True

# RULE: Challan status after invoice is created
CHALLAN_STATUS_AFTER_INVOICE = "INVOICED"

# RULE: When a retail challan has outstanding balance, invoice button is locked
RETAIL_INVOICE_LOCKED_IF_BALANCE = True

# RULE: Soft-delete applies to all these tables (never hard DELETE)
SOFT_DELETE_TABLES: Set[str] = {
    "orders", "order_lines", "challans", "challan_lines",
    "invoices", "invoice_lines", "payments",
}


# ============================================================================
# 6. CONSULTATION RULES
# ============================================================================

# RULE: Consultation fee is stored as a SERVICE order_line (not just total_value)
CONSULTATION_FEE_AS_ORDER_LINE = True

# RULE: Consultation fee line has GST = 0
CONSULTATION_FEE_GST_PERCENT = 0.0

# RULE: eye_side for consultation fee lines
CONSULTATION_FEE_EYE_SIDE = "SERVICE"

# RULE: Consultation fee line is auto-allocated (no stock picking needed)
CONSULTATION_FEE_AUTO_ALLOCATED = True

# RULE: Service product name used if no matching product found
CONSULTATION_FEE_PRODUCT_NAME = "Consultation Fee"
CONSULTATION_FEE_PRODUCT_GROUP = "Services"
CONSULTATION_FEE_PRODUCT_UNIT  = "SERVICE"

# RULE: A consultation order stores status = CLOSED and is_converted = TRUE after conversion
CONSULTATION_CONVERTED_STATUS     = "CLOSED"
CONSULTATION_CONVERTED_FLAG_COL   = "is_converted"

# RULE: The retail order stores reference to source consultation in customer_order_no
RETAIL_ORDER_CONSULTATION_REF_COL = "customer_order_no"


# ============================================================================
# 7. QUANTITY RULES
# ============================================================================

# RULE: Box products — loose pieces not allowed unless allow_loose=TRUE
LOOSE_PIECES_DEFAULT_ALLOWED = False

# RULE: Ophthalmic RX lens — always 1 piece per eye (never > 2 in one line)
RX_LENS_MAX_QTY_PER_LINE = 1

# RULE: Once an eye is finalized and added to cart for a product selection,
#        adding the same eye again shows a warning (duplicate guard)
FINALIZE_DUPLICATE_EYE_WARNING = True

# RULE: Minimum qty for any line
MIN_BILLING_QTY = 1


# ============================================================================
# 8. EYE SIDE RULES
# ============================================================================

# Valid eye_side values (DB stores char(1) compact form)
VALID_EYE_SIDES_COMPACT: Set[str]  = {"R", "L", "B", "O", "S"}
VALID_EYE_SIDES_EXPANDED: Set[str] = {"R", "L", "B", "OTHER", "SERVICE"}

# RULE: SERVICE lines skip allocation, production, and qty checks
SERVICE_EYE_SIDES: Set[str] = {"SERVICE", "S"}

# RULE: Lines with these eye_sides are excluded from allocation % calculations
ALLOCATION_SKIP_EYE_SIDES: Set[str] = {"SERVICE", "S"}

# RULE: Lines with these eye_sides are excluded from production routing
PRODUCTION_SKIP_EYE_SIDES: Set[str] = {"SERVICE", "S"}


# ============================================================================
# 9. ALLOCATION RULES
# ============================================================================

# RULE: SERVICE lines are always considered fully allocated at save time
SERVICE_LINES_AUTO_ALLOCATED = True

# RULE: An order is "ready for billing" when all non-SERVICE lines are allocated
def is_ready_for_billing(lines: List[dict]) -> bool:
    """
    Central check: is this order ready to raise a challan?
    SERVICE lines are excluded — they're always auto-allocated.
    """
    product_lines = [
        l for l in lines
        if str(l.get("eye_side", "")).upper() not in ALLOCATION_SKIP_EYE_SIDES
        and not l.get("is_deleted")
    ]
    if not product_lines:
        return True   # Consultation-only order: always ready

    return all(
        int(l.get("allocated_qty") or 0) >= int(l.get("billing_qty") or l.get("quantity") or 0)
        for l in product_lines
    ) and all(
        float(l.get("unit_price") or 0) > 0
        for l in product_lines
    )


# ============================================================================
# 10. VALIDATION HOOKS — registered into finalize_engine pipeline
# ============================================================================
# These run automatically on EVERY order save via validators_builtin.py.
# Add new rules here using @register_global or @register_for_mode.
# DO NOT duplicate these checks in retail_punching.py or wholesale_punching.py.

def register_all_business_rules():
    """
    Call this once at app startup (or import into validators_builtin.py).
    Registers all business rules into the finalize_engine pipeline.
    """
    from modules.core.validators_builtin import (
        register_global, register_for_mode,
    )
    from modules.core.validation_result import error, warning, advisory

    # ── RULE: No zero-qty lines (except SERVICE) ──────────────────────────
    @register_global
    def no_zero_qty_non_service(line: dict, ctx: dict):
        if str(line.get("eye_side", "")).upper() in SERVICE_EYE_SIDES:
            return []   # SERVICE lines skip this check
        if int(line.get("billing_qty") or 0) < MIN_BILLING_QTY:
            return [error("NO_QTY", f"billing_qty must be ≥ {MIN_BILLING_QTY}", line)]
        return []

    # ── RULE: SERVICE lines must have GST = 0 ─────────────────────────────
    @register_global
    def service_line_gst_zero(line: dict, ctx: dict):
        if str(line.get("eye_side", "")).upper() in SERVICE_EYE_SIDES:
            if float(line.get("gst_percent") or 0) != 0.0:
                return [error(
                    "SERVICE_GST_NONZERO",
                    f"SERVICE lines must have GST = 0 (found {line.get('gst_percent')}%)",
                    line,
                )]
        return []

    # ── RULE: Retail price must use MRP (not selling_price) ───────────────
    @register_for_mode("RETAIL")
    def retail_price_is_mrp(line: dict, ctx: dict):
        # Advisory only — pricing engine already enforces MRP, this just logs
        if float(line.get("unit_price") or 0) <= 0:
            return [error("ZERO_RETAIL_PRICE", "Retail line has zero unit price", line)]
        return []

    # ── RULE: Wholesale price must use selling_price (not MRP) ───────────
    @register_for_mode("WHOLESALE")
    def wholesale_price_not_mrp(line: dict, ctx: dict):
        _sp  = float(ctx.get("cost_map", {}).get(str(line.get("product_id")), 0) or 0)
        _mrp = float(line.get("mrp") or 0)
        _up  = float(line.get("unit_price") or 0)
        if _sp > 0 and _mrp > 0 and abs(_up - _mrp) < 0.01:
            return [warning(
                "WS_USING_MRP",
                f"Wholesale line appears to use MRP (₹{_mrp}) instead of trade price "
                f"(₹{_sp}). Set selling_price in Product Master.",
                line,
            )]
        return []

    # ── RULE: Advance cannot exceed order total ───────────────────────────
    # (order-level, not line-level — checked in context)
    @register_global
    def advance_not_exceeds_total(line: dict, ctx: dict):
        # Only check once on first line to avoid duplicate issues
        if line is not ctx.get("_first_line"):
            return []
        advance = float(ctx.get("advance_amount") or 0)
        total   = float(ctx.get("order_total") or 0)
        if ADVANCE_CANNOT_EXCEED_TOTAL and advance > total > 0:
            return [error(
                "ADVANCE_EXCEEDS_TOTAL",
                f"Advance ₹{advance:,.2f} exceeds order total ₹{total:,.2f}",
                line,
            )]
        return []

    # ── RULE: GST slab must be a valid Indian slab ────────────────────────
    @register_global
    def valid_gst_slab(line: dict, ctx: dict):
        gst = float(line.get("gst_percent") or 0)
        eye = str(line.get("eye_side", "")).upper()
        if eye in SERVICE_EYE_SIDES:
            return []   # SERVICE lines always 0 — already checked above
        if gst not in VALID_GST_SLABS:
            return [warning(
                "UNUSUAL_GST_SLAB",
                f"GST {gst}% is not a standard Indian slab "
                f"({', '.join(str(s) for s in VALID_GST_SLABS)})",
                line,
            )]
        return []



# ============================================================================
# 11. STAGE LOCK RULES — edit gating by order stage
# ============================================================================

# RULE: Orders can only be edited (from retail/wholesale punching) if they
#        are in one of these statuses. Once past CONFIRMED, the order is
#        locked for retail editing — backoffice must release it first.
RETAIL_EDIT_LOCKED_AFTER: Set[str] = {
    # CONFIRMED and beyond → locked in retail punching view.
    # Backoffice is the ONLY edit authority after CONFIRMED.
    # Admin/Manager can still open in punching via OrderGuard override.
    "CONFIRMED", "IN_PRODUCTION", "READY", "BILLED",
    "DISPATCHED", "DELIVERED", "CLOSED", "CANCELLED", "RETURNED",
}

# RULE: To unlock an IN_PRODUCTION order for editing, backoffice must
#        move it back to CONFIRMED. This is the "release from stage" action.
STAGE_RELEASE_TARGET_STATUS = "CONFIRMED"

# RULE: If a backoffice job is blank/unassigned when release is triggered,
#        the order automatically returns to CONFIRMED (no manual step needed)
BLANK_JOB_AUTO_RELEASES_TO_CONFIRMED = True

# RULE: Which statuses trigger the "release from stage" button in backoffice
STAGE_RELEASE_ALLOWED_FROM: Set[str] = {"IN_PRODUCTION", "READY"}

# RULE: Reason required when releasing an order back for editing
STAGE_RELEASE_REASONS: List[str] = [
    "— Select reason —",
    "Power change required",
    "Product change required",
    "Customer requested change",
    "Incorrect entry at punching",
    "Other",
]


# ============================================================================
# 12. CANCELLATION RULES
# ============================================================================

# RULE: Cancellation reasons — shown as dropdown, logged in history
CANCELLATION_REASONS: List[str] = [
    "— Select reason —",
    "End customer cancelled order",         # customer changed mind
    "Order cancelled due to technical issue", # system/entry error
    "Order cancelled — stock not available",  # OOS after order
    "Manual cancellation",                   # staff override
    "Duplicate / wrong entry",               # punching mistake
    "Other",                                 # free text
]

# RULE: Cancellation is only allowed in these statuses
CANCELLATION_ALLOWED_STATUSES: Set[str] = {
    "PENDING", "PROVISIONAL", "UNDER_REVIEW", "CONFIRMED",
}

# RULE: These statuses are too far along to cancel directly
#        (must raise Credit Note if billed, or release from stage first)
CANCELLATION_BLOCKED_STATUSES: Set[str] = {
    "IN_PRODUCTION", "READY", "BILLED",
    "DISPATCHED", "DELIVERED", "CLOSED", "CANCELLED", "RETURNED",
}

# RULE: Cancellation requires a reason (no blank reasons)
CANCELLATION_REASON_REQUIRED = True

# RULE: Two-step confirm required before cancellation (show → confirm)
CANCELLATION_TWO_STEP_CONFIRM = True

# RULE: Cancellation reason is stored in orders.remarks and order_status_history.remarks
CANCELLATION_REASON_STORED_IN = ["orders.remarks", "order_status_history.remarks"]


# ============================================================================
# 13. RETURN PIPELINE RULES
# ============================================================================

# RULE: Return statuses in lifecycle order
RETURN_STATUSES: List[str] = [
    "RETURN_REQUESTED",   # customer/staff raises return request
    "RETURN_APPROVED",    # backoffice approves return
    "RETURN_IN_TRANSIT",  # item physically on its way back (optional)
    "RETURNED",           # item received back at store
    "REFUND_PENDING",     # payment needs to be reversed
    "REFUND_PROCESSED",   # refund issued, case closed
]

# RULE: Return reasons
RETURN_REASONS: List[str] = [
    "— Select reason —",
    "Wrong product delivered",
    "Defective / damaged product",
    "Power mismatch",
    "Customer dissatisfied",
    "Frame fitting issue",
    "Lens quality issue",
    "Delivered too late",
    "Other",
]

# RULE: Returns are only allowed on orders in these statuses
RETURN_ALLOWED_FROM_STATUSES: Set[str] = {
    "DISPATCHED", "DELIVERED", "CLOSED",
}

# RULE: Partial returns allowed (return specific lines, not full order)
PARTIAL_RETURN_ALLOWED = True

# RULE: Return window in days (0 = no time limit enforced)
RETURN_WINDOW_DAYS = 30

# RULE: Stock is restored to inventory on return approval
RETURN_RESTORES_STOCK = True

# RULE: Credit note is generated automatically on RETURN_APPROVED
RETURN_GENERATES_CREDIT_NOTE = True

# RULE: Refund modes allowed
REFUND_MODES: List[str] = ["CASH", "UPI", "NEFT", "CARD", "STORE_CREDIT"]

# RULE: Return transitions
RETURN_STATUS_TRANSITIONS: Dict[str, List[str]] = {
    "RETURN_REQUESTED":   ["RETURN_APPROVED", "CANCELLED"],
    "RETURN_APPROVED":    ["RETURN_IN_TRANSIT", "RETURNED"],
    "RETURN_IN_TRANSIT":  ["RETURNED"],
    "RETURNED":           ["REFUND_PENDING"],
    "REFUND_PENDING":     ["REFUND_PROCESSED"],
    "REFUND_PROCESSED":   [],   # terminal
}


# ============================================================================
# 14. CREDIT NOTE RULES
# ============================================================================

# RULE: Credit notes can be raised against invoices only
CREDIT_NOTE_REQUIRES_INVOICE = True

# RULE: Credit note amount cannot exceed the original invoice amount
CREDIT_NOTE_MAX_EQUALS_INVOICE = True

# RULE: Credit note statuses
CREDIT_NOTE_STATUSES: List[str] = ["DRAFT", "APPROVED", "APPLIED", "CANCELLED"]

# RULE: Credit note can be applied against future invoices or paid as refund
CREDIT_NOTE_APPLY_MODES: List[str] = ["APPLY_TO_INVOICE", "CASH_REFUND", "STORE_CREDIT"]

# ── Helper functions used by UI and pipeline ─────────────────────────────────

def status_color(status: str) -> str:
    """Return hex colour for a given order status."""
    return STATUS_DISPLAY.get(status.upper(), {}).get("color", "#64748b")


def status_icon(status: str) -> str:
    """Return emoji icon for a given order status."""
    return STATUS_DISPLAY.get(status.upper(), {}).get("icon", "•")


def can_edit(status: str) -> bool:
    """Returns True if an order in this status can be edited."""
    return status.upper() in EDITABLE_STATUSES


def allowed_transitions(status: str) -> List[str]:
    """Returns list of statuses this order can transition to."""
    return STATUS_TRANSITIONS.get(status.upper(), [])


def is_terminal(status: str) -> bool:
    """Returns True if no further status movement is possible."""
    return status.upper() in TERMINAL_STATUSES


def price_field(order_type: str) -> str:
    """Returns the primary price field name for this order type."""
    return PRICE_FIELD_BY_ORDER_TYPE.get(order_type.upper(), "selling_price")


def is_gst_inclusive(order_type: str) -> bool:
    """Returns True if GST is included in the price (retail MRP model)."""
    return order_type.upper() in GST_INCLUSIVE_ORDER_TYPES


def invoice_requires_full_payment(order_type: str) -> bool:
    """Returns True if invoice can only be raised after full payment."""
    if order_type.upper() == "RETAIL":
        return RETAIL_INVOICE_REQUIRES_FULL_PAYMENT
    return WHOLESALE_INVOICE_REQUIRES_FULL_PAYMENT


def is_service_line(line: dict) -> bool:
    """Returns True if this line is a SERVICE/consultation fee line."""
    return str(line.get("eye_side", "")).upper() in SERVICE_EYE_SIDES


def skip_allocation(line: dict) -> bool:
    """Returns True if this line should be excluded from allocation tracking."""
    return str(line.get("eye_side", "")).upper() in ALLOCATION_SKIP_EYE_SIDES


def skip_production(line: dict) -> bool:
    """Returns True if this line should not enter production routing."""
    return str(line.get("eye_side", "")).upper() in PRODUCTION_SKIP_EYE_SIDES


# ============================================================================
# 15. SERVICE CHARGE TYPES  — all service charges across backoffice
# ============================================================================
# RULE: This is the SINGLE definition of service charge types.
#        order_charges_panel.py imports SERVICE_CHARGE_TYPES — never hardcodes.
#        To add a new service type: add one entry here. Nothing else to change.

SERVICE_CHARGE_TYPES: Dict[str, Dict] = {
    # ── Clinical / Professional services — shown FIRST in UI ──────────────
    # CONSULTATION is always the anchor charge (prefilled from retail).
    # It must appear at the top of every charges panel so staff see it
    # loaded before adding Fitting / Colouring below it.
    "CONSULTATION": {
        "icon":        "🩺",
        "label":       "Consultation Fee",
        "default_gst": 0,
        "color":       "#4ade80",
        "category":    "CLINICAL",
        "description": "Consultation / doctor visit fee",
    },
    # ── Optical lab services ──────────────────────────────────────────────
    "FITTING": {
        "icon":        "🔧",
        "label":       "Fitting",
        "default_gst": 18,
        "color":       "#8b5cf6",
        "category":    "LAB",
        "description": "Frame fitting charge",
    },
    "COLOURING": {
        "icon":        "🎨",
        "label":       "Colouring",
        "default_gst": 18,
        "color":       "#ec4899",
        "category":    "LAB",
        "description": "Lens tinting / colouring charge",
    },
    # ── Logistics ─────────────────────────────────────────────────────────
    "COURIER": {
        "icon":        "📦",
        "label":       "Courier",
        "default_gst": 18,
        "color":       "#0ea5e9",
        "category":    "LOGISTICS",
        "description": "Courier / delivery charge",
        "has_tracking": True,        # shows courier company + tracking fields
    },
    "EYE_TESTING": {
        "icon":        "👁️",
        "label":       "Eye Testing",
        "default_gst": 0,
        "color":       "#10b981",
        "category":    "CLINICAL",
        "description": "Eye examination / refraction charge",
    },
    # ── Miscellaneous ─────────────────────────────────────────────────────
    "MISC": {
        "icon":        "➕",
        "label":       "Misc / Other",
        "default_gst": 18,
        "color":       "#64748b",
        "category":    "MISC",
        "description": "Other service charge",
    },
}

# Helper: get charge type config with fallback to MISC
def get_charge_type(charge_type: str) -> Dict:
    return SERVICE_CHARGE_TYPES.get(
        charge_type.upper(), SERVICE_CHARGE_TYPES["MISC"]
    )
