"""
modules/validators/fulfillment_validator.py
============================================
FulfillmentValidator — three responsibilities:

1. SUPPLIER_ASSIGNED
   Every VENDOR/EXTERNAL_LAB order line must have a preferred_supplier_id
   on the product. If not set → WARNING (block supplier order population).

2. BILLING_GATE
   At challan/invoice time: VENDOR/EXTERNAL_LAB lines must have batch
   allocation. No purchase = no billing. CRITICAL block.

3. AUTO_FULFILLMENT_MATCH
   For products with auto_fulfillment = TRUE:
   - Contact lens  : product_id + sph + cyl + axis matches inventory batch
   - RX ophthalmic : same match on job card completion
   If matched → sets _auto_billing_ready = True on the line (caller triggers
   challan generation).
   Tolerance: exact match by default. ±0.25D tolerance enabled per line if
   line carries  _rx_tolerance = True  (set by operator at punching time).
"""

from typing import Dict, List
from .base import BaseValidator, ValidationResult

# Routes that require purchase before billing
_VENDOR_ROUTES = {"VENDOR", "EXTERNAL_LAB"}

# Product categories where power match is relevant
_POWER_CATS = {"contact", "ophthalmic"}


def _run_query(sql: str, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _product_category(main_group: str) -> str:
    mg = (main_group or "").lower()
    if "contact" in mg:
        return "contact"
    if "lens" in mg or "ophthalmic" in mg or "spectacle" in mg:
        return "ophthalmic"
    if "frame" in mg:
        return "frame"
    return "other"


def _powers_match(line: dict, batch: dict, tolerance: bool = False) -> bool:
    """
    Compare SPH/CYL/AXIS/ADD between order line and inventory batch.
    tolerance=True allows ±0.25D on SPH and CYL.
    """
    def _f(val):
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    sph_l = _f(line.get("sph"))
    cyl_l = _f(line.get("cyl"))
    ax_l  = _f(line.get("axis"))
    add_l = _f(line.get("add_power"))

    sph_b = _f(batch.get("sph"))
    cyl_b = _f(batch.get("cyl"))
    ax_b  = _f(batch.get("axis"))
    add_b = _f(batch.get("add_power"))

    delta = 0.26 if tolerance else 0.001   # exact vs ±0.25

    def _close(a, b):
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return abs(a - b) < delta

    if not _close(sph_l, sph_b):
        return False
    if not _close(cyl_l, cyl_b):
        return False
    # AXIS only matters when CYL is present
    if cyl_l and abs(cyl_l) > 0.001:
        if not _close(ax_l, ax_b):
            return False
    if not _close(add_l, add_b):
        return False
    return True


class FulfillmentValidator(BaseValidator):
    """
    Runs three fulfillment checks on every order line.
    Returns a list of ValidationResult — one per issue found.
    Caller (engine.py) handles lists via the  elif isinstance(result, list)  branch.
    """

    name = "FULFILLMENT"

    def __init__(self, config=None):
        self.config = config or {}

    def validate(self, order: Dict) -> List[ValidationResult]:
        results = []
        lines   = order.get("lines") or []

        # ── Pre-fetch product metadata in one query ───────────────────────────
        product_ids = list({
            str(l.get("product_id") or "")
            for l in lines
            if l.get("product_id")
        })

        product_meta = {}   # product_id → {preferred_supplier_id, auto_fulfillment, main_group}
        if product_ids:
            rows = _run_query("""
                SELECT
                    id::text                        AS product_id,
                    COALESCE(main_group, '')        AS main_group,
                    preferred_supplier_id::text     AS preferred_supplier_id
                FROM products
                WHERE id = ANY(%(ids)s::uuid[])
            """, {"ids": product_ids})
            for r in rows:
                product_meta[r["product_id"]] = r

        for idx, line in enumerate(lines, 1):
            pid      = str(line.get("product_id") or "")
            route    = str(line.get("manufacturing_route") or
                           (line.get("lens_params") or {}).get("manufacturing_route") or
                           "").upper()
            batches  = line.get("batch_allocation") or []
            meta     = product_meta.get(pid, {})
            mg       = meta.get("main_group", "")
            cat      = _product_category(mg)
            supplier = meta.get("preferred_supplier_id")
            # auto_fulfillment is power-specific — read from product_stock_minimum
            auto_ok = False
            if pid:
                _psm = _run_query("""
                    SELECT auto_fulfillment FROM product_stock_minimum
                    WHERE product_id = %(pid)s::uuid
                      AND COALESCE(sph,       0) = COALESCE(%(sph)s::numeric, 0)
                      AND COALESCE(cyl,       0) = COALESCE(%(cyl)s::numeric, 0)
                      AND COALESCE(axis,      0) = COALESCE(%(axis)s::integer, 0)
                      AND COALESCE(add_power, 0) = COALESCE(%(add)s::numeric, 0)
                    LIMIT 1
                """, {
                    "pid":  pid,
                    "sph":  line.get("sph"),
                    "cyl":  line.get("cyl"),
                    "axis": line.get("axis"),
                    "add":  line.get("add_power"),
                })
                auto_ok = bool(_psm and _psm[0].get("auto_fulfillment"))
            pname    = str(line.get("product_name") or f"Line {idx}")
            eye      = str(line.get("eye_side") or "")

            is_vendor = route in _VENDOR_ROUTES

            # ── 1. SUPPLIER_ASSIGNED ─────────────────────────────────────────
            # Check effective supplier — override takes priority over preferred
            _eff_supplier = None
            _is_override  = False
            if is_vendor and pid:
                try:
                    from modules.procurement.po_engine import get_effective_supplier
                    _eff = get_effective_supplier(pid)
                    _eff_supplier = _eff.get("supplier_id")
                    _is_override  = _eff.get("is_override", False)
                except Exception:
                    _eff_supplier = supplier   # fallback to product meta

            if is_vendor and not _eff_supplier:
                results.append(ValidationResult(
                    rule     = "SUPPLIER_NOT_ASSIGNED",
                    passed   = False,
                    severity = "WARNING",
                    message  = (
                        f"Line {idx} ({pname} {eye}): no preferred supplier assigned "
                        f"and no override active. Set Preferred Supplier in Product "
                        f"Master, or use Supplier Override for this product."
                    ),
                    details  = {"line_idx": idx, "product_id": pid, "route": route},
                ))
            elif is_vendor and _is_override:
                results.append(ValidationResult(
                    rule     = "SUPPLIER_OVERRIDE_ACTIVE",
                    passed   = True,
                    severity = "WARNING",
                    message  = (
                        f"Line {idx} ({pname} {eye}): routed to alternate supplier "
                        f"via manual override. Verify before approving PO."
                    ),
                    details  = {"line_idx": idx, "product_id": pid,
                                "override_supplier": _eff_supplier},
                ))

            # ── 2. BILLING_GATE ──────────────────────────────────────────────
            # Block challan/invoice if vendor line has no batch allocation.
            # Check context flag — only run at billing time.
            at_billing = order.get("_context") == "BILLING"
            if at_billing and is_vendor and not batches:
                results.append(ValidationResult(
                    rule     = "PURCHASE_REQUIRED",
                    passed   = False,
                    severity = "CRITICAL",
                    message  = (
                        f"Line {idx} ({pname} {eye}): purchase not received. "
                        f"Complete the supplier purchase first, then bill."
                    ),
                    details  = {"line_idx": idx, "product_id": pid, "route": route},
                ))

            # ── 3. AUTO_FULFILLMENT_MATCH ────────────────────────────────────
            # Only for products opted into automation.
            # Only runs when batch allocation exists (purchase done).
            if auto_ok and batches and cat in _POWER_CATS:
                tolerance = bool(line.get("_rx_tolerance", False))
                matched   = False

                for b in batches:
                    batch_no = str(b.get("batch_no") or "")
                    if not batch_no:
                        continue

                    # Fetch the actual batch row to compare powers
                    batch_rows = _run_query("""
                        SELECT
                            sph, cyl, axis, add_power
                        FROM inventory_stock
                        WHERE product_id = %(pid)s::uuid
                          AND batch_no   = %(bno)s
                        LIMIT 1
                    """, {"pid": pid, "bno": batch_no})

                    if batch_rows and _powers_match(line, batch_rows[0], tolerance):
                        matched = True
                        break

                if matched:
                    # Signal to caller that this line is cleared for auto-billing
                    line["_auto_billing_ready"] = True
                    results.append(ValidationResult(
                        rule     = "AUTO_FULFILLMENT_MATCHED",
                        passed   = True,
                        severity = "INFO",
                        message  = (
                            f"Line {idx} ({pname} {eye}): power matched — "
                            f"auto billing cleared."
                            + (" [±0.25D tolerance]" if tolerance else "")
                        ),
                        details  = {"line_idx": idx, "product_id": pid,
                                    "tolerance": tolerance},
                    ))
                else:
                    results.append(ValidationResult(
                        rule     = "AUTO_FULFILLMENT_MISMATCH",
                        passed   = False,
                        severity = "WARNING",
                        message  = (
                            f"Line {idx} ({pname} {eye}): power mismatch between "
                            f"order and received batch. Manual review required."
                        ),
                        details  = {"line_idx": idx, "product_id": pid,
                                    "tolerance": tolerance},
                    ))

        # If no issues found at all, return a single PASS
        if not results:
            results.append(ValidationResult(
                rule     = "FULFILLMENT_OK",
                passed   = True,
                severity = "INFO",
                message  = "All lines fulfillment checks passed.",
            ))

        return results
