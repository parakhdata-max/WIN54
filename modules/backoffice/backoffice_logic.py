"""
Backoffice Logic Module
Business logic for backoffice operations

Contains:
- Manufacturing power calculations
- Batch allocation updates
- Line billing/pricing
- Order total recalculation
- Status enums and constants
"""

import datetime
from typing import Dict, List, Optional, Tuple
from enum import Enum
from modules.batch_manager import get_available_stock
from modules.utils.power_normalizer import normalize_power
# ✅ Centralized price resolver — correct DB column per order type
from modules.core.price_qty_governor import (
    resolve_price            as resolve_price_for_order_type,
    normalize_to_pcs_price,
    is_box_product,
    get_pcs_price,
    compute_line_gst,        # single source of truth for GST calc
)


# ============================================================================
# STATUS ENUMS
# ============================================================================

class LineItemStatus(Enum):
    """Status for individual line items"""
    PENDING = "PENDING"
    ALLOCATED = "ALLOCATED"
    PARTIAL = "PARTIAL"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class AllocationStatus(Enum):
    """Batch allocation status"""
    PENDING = "PENDING"
    ALLOCATED = "ALLOCATED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ManufacturingRoute(Enum):
    """Manufacturing routing options"""
    STOCK = "STOCK"
    INHOUSE = "INHOUSE"
    VENDOR = "VENDOR"
    EXTERNAL_LAB = "EXTERNAL_LAB"


# ============================================================================
# MANUFACTURING POWER
# ============================================================================

def update_manufacturing_power(line: Dict) -> Dict:
    """
    Calculate manufacturing power (SPH OUT / CYL OUT / AXIS OUT) for a line.

    For contact lenses:
        Applies full vertex distance correction using contact_lens_converter.
        Auto-detects brand (Bausch & Lomb / CooperVision) from product name.
        Uses the correct formula F_CL = F_spec / (1 - d x F_spec) at 12.5mm VD.
        Snaps result to the nearest available power for the matched product.

    For spectacle lenses:
        Passes RX through unchanged (no vertex correction needed).
    """
    main_group   = str(line.get("main_group",   "")).lower()
    product_name = str(line.get("product_name", "")).lower()
    is_contact   = "contact" in main_group or "contact" in product_name

    sph  = float(line.get("sph",  0) or 0)
    cyl  = float(line.get("cyl",  0) or 0)
    axis = int(  line.get("axis", 0) or 0)

    if not is_contact or not line.get("use_effective_power"):
        # Spectacle lens OR effective power not requested — pass through
        line["sph_out"]  = sph
        line["cyl_out"]  = cyl
        line["axis_out"] = axis
        line["effectivity_applied"] = False
        return line

    # ── Contact lens: vertex correction via contact_lens_converter ────────────
    try:
        from modules.documents.contact_lens_converter import (
            convert_rx_to_cl, BRAND_CATALOG, get_product
        )
        from modules.power_engine import closest_toric

        # ── Auto-detect brand and best matching product from product_name ──
        pn_lower = product_name

        def _detect_brand_product(pn: str):
            """Return (brand, product_name) by matching product_name keywords."""
            # Brand detection
            if any(k in pn for k in ("bausch", "b&l", "bl ", "soflens", "pure vision",
                                      "purevision", "biotrue", "ultra", "infuse")):
                brand = "Bausch & Lomb"
            elif any(k in pn for k in ("cooper", "biofinity", "myday", "my day",
                                        "avaira", "clariti", "proclear", "misight")):
                brand = "CooperVision"
            else:
                brand = None

            if not brand:
                return None, None

            # Within brand, pick toric vs spherical
            is_toric = any(k in pn for k in ("toric", "astigmat"))

            # Product keyword matching
            products = BRAND_CATALOG.get(brand, [])
            # Score each product by keyword overlap
            best, best_score = None, -1
            for p in products:
                if is_toric and p.lens_type != "toric":
                    continue
                if not is_toric and p.lens_type == "toric":
                    continue
                score = sum(1 for w in p.name.lower().split() if w in pn)
                if score > best_score:
                    best, best_score = p, score

            if best:
                return brand, best.name

            # Fallback: first toric or spherical in brand
            for p in products:
                if is_toric and p.lens_type == "toric":
                    return brand, p.name
                if not is_toric and p.lens_type == "spherical":
                    return brand, p.name

            return brand, products[0].name if products else (None, None)

        brand, matched_product = _detect_brand_product(pn_lower)
        vd_mm = float(line.get("bvd") or 12.5)

        if brand and matched_product:
            # Override VD on matched product before conversion
            prod = get_product(brand, matched_product)
            if prod:
                prod.vertex_mm = vd_mm

            result = convert_rx_to_cl(sph, cyl, axis, brand, matched_product)

            if result.in_range:
                line["sph_out"]  = result.cl_sph
                line["cyl_out"]  = result.cl_cyl
                line["axis_out"] = result.cl_axis
                line["effectivity_applied"]  = True
                line["_cl_brand"]   = brand
                line["_cl_product"] = matched_product
                line["_cl_vd_mm"]   = vd_mm
                return line

        # ── Fallback: bare vertex formula (no brand snap) ─────────────────
        raise ValueError("No brand/product matched — using bare vertex formula")

    except Exception:
        # Bare vertex formula as last resort — correct formula, no step snapping
        d = float(line.get("bvd", 12.5) or 12.5) / 1000.0

        def _vc(F):
            denom = 1.0 - (F * d)
            return round(round(F / denom * 4) / 4, 2) if abs(denom) > 1e-12 else F

        if abs(cyl) < 0.01:
            line["sph_out"]  = _vc(sph)
            line["cyl_out"]  = 0.0
            line["axis_out"] = axis
        else:
            # Ensure minus-cyl
            if cyl > 0:
                sph, cyl, axis = sph + cyl, -cyl, (axis - 90) % 180
            m1, m2 = sph, sph + cyl
            line["sph_out"]  = _vc(m1)
            line["cyl_out"]  = round(_vc(m2) - _vc(m1), 2)
            line["axis_out"] = axis

        line["effectivity_applied"] = True
        line["_cl_brand"]   = "generic"
        line["_cl_product"] = "bare vertex"

    return line

# ============================================================================
# BATCH ALLOCATION
# ============================================================================

def update_batch_allocation(line: Dict, allocation: List[Dict]) -> Dict:
    """
    Update batch allocation for a line
    
    CRITICAL: This function ensures batch allocations include product metadata
    needed by the pricing engine (unit, box_size)
    
    Args:
        line: Line item dictionary
        allocation: List of batch allocation dicts
    
    Returns:
        Updated line with proper batch allocation
    """
    # Ensure each batch has product metadata for pricing
    enhanced_allocation = []
    
    for batch in allocation:
        enhanced_batch = dict(batch)
        
        #  FIX: Add product metadata to batch for pricing engine
        if 'unit' not in enhanced_batch:
            enhanced_batch['unit'] = line.get('unit', 'PCS')
        
        if 'box_size' not in enhanced_batch:
            enhanced_batch['box_size'] = line.get('box_size', 1)
        
        if 'product_id' not in enhanced_batch:
            enhanced_batch['product_id'] = line.get('product_id')
        
        if 'product_name' not in enhanced_batch:
            enhanced_batch['product_name'] = line.get('product_name')
        
        enhanced_allocation.append(enhanced_batch)
    
    line['batch_allocation'] = enhanced_allocation
    line['allocated_qty'] = sum(b.get('allocated_qty', 0) for b in enhanced_allocation)
    
    return line


# ============================================================================
# PRICING / BILLING
# ============================================================================

# ============================================================================
# PRICE MUTATION GUARD
# ============================================================================

def guard_price_mutation(line: Dict, caller: str = "unknown") -> bool:
    """
    Middleware guard — call before mutating unit_price on any line.

    Returns True  → mutation is allowed, proceed.
    Returns False → mutation is blocked, abort and keep existing price.

    Rules:
      - pricing_locked=True   → always blocked
      - pricing_applied_at set AND caller is not 'manual_allocation'
        AND caller is not 'product_change' → blocked (prevents silent reprice)
    """
    import logging
    log = logging.getLogger(__name__)

    if line.get("pricing_locked"):
        log.warning("[PriceGuard] BLOCKED by pricing_locked | caller=%s | line=%s",
                    caller, line.get("product_name"))
        return False

    REPRICE_CALLERS = {"manual_allocation", "product_change"}
    if line.get("pricing_applied_at") and caller not in REPRICE_CALLERS:
        log.warning(
            "[PriceGuard] BLOCKED silent reprice | caller=%s | line=%s | "
            "existing unit_price=%.2f | Use reprice_from_batch=True for intentional reprice.",
            caller, line.get("product_name"), float(line.get("unit_price") or 0)
        )
        return False

    return True


def update_line_billing(line: Dict, reprice_from_batch: bool = False) -> Dict:
    """
    Apply pricing to a line after allocation
    
    This is the CRITICAL MISSING PIECE that connects allocation  pricing
    
    Called by workflow engine after:
    - Batch allocation
    - Quantity changes  
    - Product changes
    
    Args:
        line: Line item dictionary
    
    Returns:
        Updated line with pricing applied
    """
    try:
        from modules.pricing.pricing_engine import apply_pricing_line, validate_pricing_line
    except ImportError:
        return line
    # 🔒 HARD PRICING LOCK (SINGLE SOURCE OF TRUTH)
    if line.get("pricing_locked"):
        return line

    # Qty safety guard
    billing_qty = int(line.get('billing_qty', 0))
    if billing_qty <= 0:
        line["unit_price"] = 0
        line["billing_total"] = 0
        return line
    
    # VENDOR/PENDING lines have no batch_allocation — but may already have
    # a unit_price resolved from product master. Don't zero it out.
    if not line.get('batch_allocation'):
        existing_up = float(line.get('unit_price') or 0)
        if existing_up > 0:
            _ot_v  = str(line.get("order_type") or "RETAIL").upper()
            _gst_v = float(line.get("gst_percent") or 0)
            _gst_r = compute_line_gst(existing_up, billing_qty, _gst_v, _ot_v)
            line['billing_total'] = _gst_r["grand_total"]
            line['total_price']   = _gst_r["grand_total"]
        elif not line.get("pricing_applied_at"):
            line['unit_price']    = 0
            line['billing_total'] = 0
        return line
    
    # If allocated_qty is zero, clear pricing
    allocated_qty = int(line.get('allocated_qty', 0))
    if allocated_qty == 0:
        # Avoid wiping price during workflow refresh
        if not line.get("pricing_applied_at"):
            line['unit_price'] = 0
            line['billing_total'] = 0
        return line
    
    try:
        #  CRITICAL FIX: Ensure batch allocations have product metadata
        line = update_batch_allocation(line, line['batch_allocation'])

        # Clear old pricing to allow re-pricing
        line.pop('pricing_applied_at', None)

        # ── PRICING RULE ──────────────────────────────────────────────────
        # reprice_from_batch=False (default):
        #   unit_price came from retail/wholesale punching → trust it.
        #   Only recompute billing_total = unit_price × qty.
        # reprice_from_batch=True:
        #   Operator explicitly chose a different batch in the allocation
        #   window → derive unit_price from that batch's price.
        # ─────────────────────────────────────────────────────────────────
        existing_unit_price = float(line.get('unit_price') or 0)

        if existing_unit_price > 0 and not reprice_from_batch:
            _ot_fast  = str(line.get("order_type") or "RETAIL").upper()
            _gst_fast = float(line.get("gst_percent") or 0)
            _gst_r    = compute_line_gst(existing_unit_price, billing_qty, _gst_fast, _ot_fast)
            line['billing_total'] = _gst_r["grand_total"]
            line['total_price']   = _gst_r["grand_total"]
            line.setdefault('price_source', 'retail_punching')
            return line

        # 
        # COMPUTE UNIT PRICE DIRECTLY FROM BATCH DATA
        # This is the single source of truth  do NOT rely on
        # apply_pricing_line's compute_weighted_price because it may
        # read raw BOX prices without knowing the box_size.
        #
        # Price priority per batch entry:
        #   1. selling_price  (already PCS-normalized if set by allocation window)
        #   2. unit_price     (fallback)
        #
        # If the price looks like a BOX price (> line.unit_price  box_size / 2),
        # normalize it by dividing by box_size.
        # 
        total_value = 0.0
        total_units = 0

        for b in line['batch_allocation']:
            qty = int(b.get('allocated_qty') or 0)
            if qty <= 0:
                continue

            # Get the best available price from the batch entry
            raw_price = float(
                b.get('selling_price') or
                b.get('unit_price') or
                0
            )

            if raw_price <= 0:
                continue

            #  Normalize BOX  PCS if needed 
            # Batch entries from retail store raw BOX price in selling_price.
            # Batch entries saved by allocation window store PCS price.
            # Detect which by checking if unit="PCS" was explicitly set
            # (allocation window sets "unit":"PCS" on every entry it writes).
            batch_unit = str(b.get("unit") or "").upper()
            if batch_unit == "PCS":
                # Already normalized by allocation window
                pcs_price = raw_price
            else:
                # Normalize BOX → PCS via governor (single source of truth)
                pcs_price = normalize_to_pcs_price(raw_price, line)

            total_value += qty * pcs_price
            total_units += qty

        if total_units > 0:
            computed_unit_price = round(total_value / total_units, 2)
        else:
            computed_unit_price = float(line.get('unit_price') or 0)

        # 
        # Set unit_price on line so apply_pricing_line uses it
        # 
        if computed_unit_price > 0:
            line['unit_price'] = computed_unit_price
            # Stamp price_source for debugging
            source = 'manual_batch_selection' if reprice_from_batch else 'backoffice_new_line'
            line['price_source'] = source

        # Apply pricing engine (uses selling_price from batches + our unit_price)
        line = apply_pricing_line(line)

        # Validate pricing
        is_valid, error = validate_pricing_line(line)
        if not is_valid:
            # Log but do not crash pricing flow
            line['pricing_validation_error'] = error

        # 
        # Guarantee billing_total is always correct.
        # Always recalculate from computed_unit_price  billing_qty.
        # Do NOT use engine's total_price  it may have used raw BOX
        # price in compute_weighted_price giving wrong total.
        # 
        final_unit_price = float(line.get('unit_price') or computed_unit_price)
        final_qty = billing_qty

        if final_unit_price > 0 and final_qty > 0:
            _ot_fin  = str(line.get("order_type") or "RETAIL").upper()
            _gst_fin = float(line.get("gst_percent") or 0)
            _gst_r   = compute_line_gst(final_unit_price, final_qty, _gst_fin, _ot_fin)
            line['unit_price']    = final_unit_price
            line['billing_total'] = _gst_r["grand_total"]
            line['total_price']   = _gst_r["grand_total"]  # keep in sync
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Set safe defaults
        line['unit_price'] = 0
        line['billing_total'] = 0
        line['pricing_error'] = str(e)
    
    return line


def recalculate_order_totals(order: Dict) -> Dict:
    """
    Recalculate all line pricing and order totals
    
    This function:
    1. Updates pricing for each line
    2. Calculates order-level totals
    3. Updates order metadata
    
    Called after:
    - Product changes
    - Quantity changes
    - Allocation changes
    - Manual price overrides
    
    Args:
        order: Order dictionary
    
    Returns:
        Updated order with recalculated totals
    """
    # Collect all lines  check both categorized lists AND raw lines list
    all_lines = []
    all_lines.extend(order.get('stock_lines', []))
    all_lines.extend(order.get('inhouse_lines', []))
    all_lines.extend(order.get('lab_order_lines', []))

    # If categorized lists are empty (not yet categorized), fall back to raw lines
    if not all_lines:
        all_lines = order.get('lines', [])

    # Update pricing for each line
    for line in all_lines:
        update_line_billing(line)

    # Calculate order totals (billing_qty must be int)
    total_amount    = sum(float(line.get('billing_total', 0) or 0) for line in all_lines)
    total_items     = sum(int(line.get('billing_qty', 0) or 0) for line in all_lines)
    total_allocated = sum(int(line.get('allocated_qty', 0) or 0) for line in all_lines)

    # Update order
    order['total_amount']      = round(total_amount, 2)
    order['total_items']       = total_items
    order['total_allocated']   = total_allocated
    order['billing_updated_at'] = datetime.datetime.now().isoformat()

    return order


# ============================================================================
# VALIDATION
# ============================================================================

def validate_line_for_billing(line: Dict) -> Tuple[bool, Optional[str]]:
    """
    Validate that a line is ready for billing
    
    Checks:
    - Has product
    - Has quantity
    - Has allocation (or route to vendor/lab)
    - Has pricing
    
    Returns:
        (is_valid, error_message)
    """
    # Check product
    if not line.get('product_id'):
        return False, "Missing product"
    
    # Check quantity
    billing_qty = int(line.get('billing_qty', 0))
    if billing_qty <= 0:
        return False, "Invalid billing quantity"
    
    # Check route
    route = line.get('manufacturing_route')
    if not route:
        return False, "Manufacturing route not set"
    
    # For stock/inhouse routes, need allocation
    if route in ['STOCK', 'INHOUSE']:
        if not line.get('batch_allocation'):
            return False, "Missing batch allocation for stock/inhouse route"
        
        allocated_qty = int(line.get('allocated_qty', 0))
        if allocated_qty <= 0:
            return False, "No quantity allocated"
    
    # Check pricing (only for allocated items)
    if line.get('allocated_qty', 0) > 0:
        if line.get('billing_total', 0) <= 0:
            return False, "Billing total is zero or missing"
        
        if line.get('unit_price', 0) <= 0:
            return False, "Unit price is zero or missing"
    
    return True, None


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def get_line_billing_summary(line: Dict) -> Dict:
    """
    Get billing summary for a line
    
    Returns formatted billing information for display
    """
    billing_qty = int(line.get('billing_qty', 0))
    allocated_qty = int(line.get('allocated_qty', 0))
    pending_qty = max(0, billing_qty - allocated_qty)
    
    unit_price = float(line.get('unit_price', 0))
    billing_total = float(line.get('billing_total', 0))
    
    return {
        'product_name': line.get('product_name', 'N/A'),
        'billing_qty': billing_qty,
        'allocated_qty': allocated_qty,
        'pending_qty': pending_qty,
        'unit_price': unit_price,
        'billing_total': billing_total,
        'route': line.get('manufacturing_route', 'PENDING'),
        'eye_side': line.get('eye_side', 'N/A'),
        'has_pricing': bool(line.get('pricing_applied_at'))
    }


def format_currency(amount: float) -> str:
    """Format amount as currency string"""
    return f"{amount:,.2f}"


def get_pending_qty(line: Dict) -> int:
    """Calculate pending quantity for a line"""
    billing_qty = int(line.get('billing_qty', 0))
    allocated_qty = int(line.get('allocated_qty', 0))
    return max(0, billing_qty - allocated_qty)

def refresh_line_state(line: dict):
    """
     MASTER WORKFLOW ENGINE
    Single source of truth  used by Retail + Backoffice.

    Flow:
      1. Normalize powers (sph_out / sph fallback)
      2. Fetch available stock
      3. Reset allocation state
      4. Auto-allocate from best batch (FIFO)
      5. Set manufacturing_route (STOCK / VENDOR)
      6. Run pricing engine
    """
    #  All imports inside to avoid circular dependency
    from modules.batch_manager import get_available_stock
    from modules.utils.power_normalizer import normalize_power

    billing_qty = int(line.get("billing_qty", 0))
    product_id = line.get("product_id")

    if not product_id or billing_qty <= 0:
        return line

    # -------------------------------------------------
    # 1. Normalize powers
    # -------------------------------------------------
    sph       = normalize_power(line.get("sph_out") or line.get("sph"))
    cyl       = normalize_power(line.get("cyl_out") or line.get("cyl"))
    axis      = normalize_power(line.get("axis_out") or line.get("axis"))
    add_power = normalize_power(line.get("add_power"))
    eye_side  = line.get("eye_side")

    # axis == 0 must behave as NULL in DB query
    if axis == 0:
        axis = None

    # -------------------------------------------------
    # 2. Fetch available stock
    # -------------------------------------------------
    stock_df = get_available_stock(
        product_id=str(product_id),
        sph=sph,
        cyl=cyl,
        axis=axis,
        add_power=add_power,
        eye_side=eye_side
    )

    # -------------------------------------------------
    # 3. Reset allocation state (always start clean)
    # -------------------------------------------------
    # Preserve explicit routes set at order punching time.
    # EXTERNAL_LAB and INHOUSE are intentional — do NOT let the stock
    # engine overwrite them with VENDOR/STOCK just because no batch exists.
    _lp_route = None
    _lp = line.get("lens_params") or {}
    if isinstance(_lp, str):
        try:
            import json as _jlp; _lp = _jlp.loads(_lp)
        except Exception: _lp = {}
    _lp_route = str(_lp.get("manufacturing_route") or "").upper()
    _pinned_route = _lp_route if _lp_route in ("EXTERNAL_LAB", "INHOUSE") else None

    line["batch_allocation"]    = []
    line["allocated_qty"]       = 0
    line["batch_status"]        = "PENDING"
    line["manufacturing_route"] = _pinned_route  # None means stock engine will set it

    # -------------------------------------------------
    # 4. Auto-allocate from best available batch
    # -------------------------------------------------
    if not stock_df.empty:
        row       = stock_df.iloc[0]
        available = int(row.get("available_qty", 0) or 0)

        if available > 0:
            alloc = min(available, billing_qty)

            # ✅ Use centralized resolver — correct price column per order type:
            #   RETAIL    → mrp first  (GST-inclusive counter price)
            #   WHOLESALE → selling_price first  (trade price)
            #   PURCHASE  → purchase_rate first  (cost price)
            #   ONLINE    → online_price first (e-commerce price)
            _ot = str(line.get("order_type") or "RETAIL").upper()
            raw_price = resolve_price_for_order_type(dict(row), _ot)

            # Normalize BOX → PCS via governor
            pcs_price = normalize_to_pcs_price(raw_price, line) if raw_price > 0 else 0.0

            #  Fallback to existing line unit_price if still 0 
            if pcs_price == 0:
                pcs_price = float(line.get("unit_price") or 0)

            # Only update unit_price from inventory if line has no valid price.
            # COUNTER_SALE orders (bulk_order) write the correct price at punch time
            # — do NOT overwrite it with inventory_stock price which may be MRP.
            # Only fix missing/zero prices from older orders.
            _existing_up = float(line.get("unit_price") or 0)
            _order_src   = str(line.get("order_source") or
                               (line.get("lens_params") or {}).get("order_source") or
                               "").upper()
            _should_refresh = (
                pcs_price > 0 and (
                    _existing_up <= 0 or          # no price at all
                    _order_src not in ("COUNTER_SALE",)  # not a bulk order
                )
            )
            if _should_refresh:
                line["unit_price"]   = pcs_price
                line["price_source"] = "inventory_refresh"
            elif pcs_price > 0:
                line["price_source"] = "order_preserved"  # kept original

            line["batch_allocation"] = [{
                "batch_no":      row.get("batch_no"),
                "allocated_qty": alloc,
                "unit":          "PCS",
                "box_size":      1,
                "product_id":    str(product_id),
                "product_name":  line.get("product_name"),
            }]

            line["allocated_qty"] = alloc

            # -------------------------------------------------
            # 5. Set route
            # -------------------------------------------------
            if alloc == billing_qty:
                line["batch_status"]        = "ALLOCATED"
                line["manufacturing_route"] = "STOCK"
            else:
                line["batch_status"]        = "PARTIAL"
                if not _pinned_route:
                    line["manufacturing_route"] = "VENDOR"

    # -------------------------------------------------
    # No stock  send to vendor (unless route is pinned)
    # -------------------------------------------------
    if line["allocated_qty"] == 0 and not _pinned_route:
        line["batch_status"]        = "PENDING"
        line["manufacturing_route"] = "VENDOR"

        # ── Resolve price from product master for VENDOR/PENDING lines ──────
        # These lines have no batch — price must come from product master.
        # This handles both: old orders saved with unit_price=0, and new
        # Pending/RX lines where stock wasn't available at punching time.
        if float(line.get("unit_price") or 0) == 0:
            try:
                from modules.sql_adapter import run_query as _rq
                _ot  = str(line.get("order_type") or "RETAIL").upper()
                _pid = str(line.get("product_id") or "")
                if _pid:
                    _prod = _rq(
                        "SELECT selling_price, mrp, unit_price, price "
                        "FROM products WHERE id=%s::uuid LIMIT 1",
                        (_pid,)
                    )
                    if _prod:
                        _resolved = get_pcs_price(_prod[0], _ot)
                        if _resolved > 0:
                            line["unit_price"]   = _resolved
                            line["price_source"] = "product_master_fallback"
            except Exception:
                pass
        # ────────────────────────────────────────────────────────────────────

    # -------------------------------------------------
    # 6. Pricing engine (update_line_billing is in this same file  no circular import)
    # -------------------------------------------------
    update_line_billing(line)

    return line




# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # Functions
    'update_manufacturing_power',
    'update_batch_allocation',
    'update_line_billing',
    'recalculate_order_totals',
    'validate_line_for_billing',
    'get_line_billing_summary',
    'format_currency',
    'get_pending_qty',
    'refresh_line_state',       #  Master workflow engine
    # Enums
    'LineItemStatus',
    'AllocationStatus',
    'ManufacturingRoute',
]
